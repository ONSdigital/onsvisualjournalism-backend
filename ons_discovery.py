import json
import os
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


ONS_URL = 'https://www.ons.gov.uk'
SEARCH_URL = 'https://api.beta.ons.gov.uk/v1/search/releases'
DOCUMENT_TYPES = ('articles', 'bulletins', 'methodologies')
DEFAULT_DISCOVERY = {'page_size': 50, 'max_pages': 15, 'lookback_days': 7}


def validate_config(config):
    allowed = {'mode', 'update_limit', 'discovery'}
    if not isinstance(config, dict) or set(config) - allowed or 'mode' not in config:
        raise ValueError('Config requires mode and accepts discovery settings or legacy update_limit')
    if config['mode'] not in ('update', 'rebuild'):
        raise ValueError('mode must be update or rebuild')
    if 'update_limit' in config and (type(config['update_limit']) is not int or config['update_limit'] < 1):
        raise ValueError('Legacy update_limit must be a positive integer')
    supplied = config.get('discovery', {})
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULT_DISCOVERY):
        raise ValueError('Unsupported discovery settings')
    settings = {**DEFAULT_DISCOVERY, **supplied}
    for name, value in settings.items():
        if type(value) is not int or value < (0 if name == 'lookback_days' else 1):
            raise ValueError(f'Invalid discovery setting: {name}')
    return {'mode': config['mode'], 'discovery': settings}


def normalize_date(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'Missing publication date: {value!r}')
    value = value.strip()
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).date().isoformat()
    except ValueError:
        for pattern in ('%d %B %Y', '%d %b %Y', '%Y/%m/%d'):
            try:
                return datetime.strptime(value, pattern).date().isoformat()
            except ValueError:
                continue
    raise ValueError(f'Unrecognized publication date: {value!r}')


def ons_url(href, base=ONS_URL, keep_fragment=False):
    if not isinstance(href, str) or not href.strip():
        return None
    parsed = urlsplit(urljoin(base, href.strip()))
    host = (parsed.hostname or '').lower()
    if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
        return None
    if host != 'ons.gov.uk' and not host.endswith('.ons.gov.uk'):
        return None
    return urlunsplit(('https', parsed.netloc.lower(), parsed.path, parsed.query,
                      parsed.fragment if keep_fragment else ''))


def is_png(url):
    return urlsplit(url).path.lower().endswith('.png')


def visualisation_path(href, base=ONS_URL):
    absolute = ons_url(href, base, keep_fragment=True)
    if absolute is None or is_png(absolute):
        return None
    parsed = urlsplit(absolute)
    if not parsed.path.startswith('/visualisations/'):
        return None
    return urlunsplit(('', '', parsed.path, parsed.query, parsed.fragment))


def extract_doc_urls(html, base):
    soup = BeautifulSoup(html, 'lxml')
    containers = soup.select('#related-links, [id*="related"]')
    related_links = [link for container in containers for link in container.select('a[href]')]
    for links in (related_links, soup.select('a[href]')):
        documents = []
        for link in links:
            url = ons_url(link.get('href'), base)
            if url is None:
                continue
            path = urlsplit(url).path
            if Path(path).suffix.lower() in ('.pdf', '.xls', '.xlsx', '.csv', '.zip', '.svg', '.png', '.jpg', '.jpeg'):
                continue
            if any(segment in DOCUMENT_TYPES for segment in path.split('/')):
                documents.append(url)
        if documents:
            return list(dict.fromkeys(documents))
    return []


def extract_vis_urls(html, base):
    soup = BeautifulSoup(html, 'lxml')
    candidates = [node.get('data-url') for node in soup.select('div.pym-interactive[data-url]')]
    urls = [path for candidate in candidates if (path := visualisation_path(candidate, base))]
    if not urls:
        for node in soup.select('[src], a[href], [data-url]'):
            candidate = node.get('data-url') or node.get('src') or node.get('href')
            path = visualisation_path(candidate, base)
            if path:
                urls.append(path)
    if not urls:
        for candidate in re.findall(r'/visualisations/[^\s"\'<>\)\u201c\u201d]+', html):
            path = visualisation_path(candidate, base)
            if path:
                urls.append(path)
    return list(dict.fromkeys(urls))


def extract_release_date(html, soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            pending = [json.loads(script.get_text())]
        except (ValueError, TypeError):
            continue
        while pending:
            item = pending.pop()
            if isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, dict):
                if 'datePublished' in item:
                    try:
                        return normalize_date(item['datePublished'])
                    except ValueError:
                        pass
                pending.extend(value for value in item.values() if isinstance(value, (list, dict)))
    analytics = re.search(r'dataLayer\[0\]\["releaseDate"\]\s*=\s*"([^"]+)"', html)
    if analytics:
        return normalize_date(analytics.group(1))
    legacy = re.search(r'"datePublished"\s*:\s*"([^"]+)"', html)
    if legacy:
        return normalize_date(legacy.group(1))
    for label in soup.find_all('span', string=re.compile(r'^\s*Release date:\s*$', re.I)):
        text = label.parent.get_text(' ', strip=True)
        match = re.search(r'Release date:\s*(\d{1,2} [A-Za-z]+ \d{4})', text, re.I)
        if match:
            return normalize_date(match.group(1))
    raise ValueError('No valid publication date found in page metadata')


def parse_publication(doc_uri, html):
    soup = BeautifulSoup(html, 'lxml')
    heading = soup.find('h1') or soup.find('title')
    title = ' '.join(heading.get_text(' ', strip=True).split()) if heading else ''
    if not title:
        raise ValueError(f'No publication title found: {doc_uri}')
    try:
        released = extract_release_date(html, soup)
    except ValueError as error:
        raise ValueError(f'Invalid release date for {doc_uri}: {error}') from error
    return {'doc_uri': doc_uri, 'release_date': released, 'title': title,
            'vis_urls': extract_vis_urls(html, doc_uri)}


class DiscoveryClient:
    def __init__(self, session=None, request_interval=1):
        self.session = session or requests.Session()
        if session is None:
            retry = Retry(total=3, backoff_factor=1, backoff_max=30,
                          status_forcelist=(429, 500, 502, 503, 504),
                          allowed_methods=frozenset(('GET',)), respect_retry_after_header=False)
            self.session.mount('https://', HTTPAdapter(max_retries=retry))
        self.html_cache = {}
        self.request_interval = request_interval
        self.last_request = None

    def request(self, url, **kwargs):
        normalized = ons_url(url)
        if normalized is None:
            raise ValueError(f'Non-ONS URL rejected: {url}')
        if self.last_request is not None:
            remaining = self.request_interval - (time.monotonic() - self.last_request)
            if remaining > 0:
                time.sleep(remaining)
        self.last_request = time.monotonic()
        response = self.session.get(normalized, timeout=30, verify=False, **kwargs)
        response.raise_for_status()
        if ons_url(response.url) is None:
            raise ValueError(f'Non-ONS redirect rejected: {response.url}')
        return response

    def get_text(self, url):
        normalized = ons_url(url)
        if normalized is None:
            raise ValueError(f'Non-ONS URL rejected: {url}')
        if normalized not in self.html_cache:
            self.html_cache[normalized] = self.request(normalized).text
        return self.html_cache[normalized]

    def get_releases(self, page_size, offset):
        payload = self.request(SEARCH_URL, params={'q': '', 'sort': 'release_date_desc',
                                                 'limit': page_size, 'offset': offset}).json()
        if not isinstance(payload, dict) or not isinstance(payload.get('releases'), list):
            raise ValueError('Invalid ONS release search response')
        return payload['releases']


@dataclass
class DiscoveryResult:
    publications: list
    last_release_date: str


def discover_publications(client, cutoff, settings, baseline):
    publications = {}
    newest = baseline
    previous_day = None
    for page in range(settings['max_pages']):
        releases = client.get_releases(settings['page_size'], page * settings['page_size'])
        if not releases:
            return DiscoveryResult(list(publications.values()), newest.isoformat())
        page_days = []
        for release in releases:
            description = release.get('description', {})
            if description.get('published') is not True or description.get('cancelled') is True:
                continue
            released = date.fromisoformat(normalize_date(description.get('release_date')))
            if previous_day is not None and released > previous_day:
                raise ValueError('ONS release search is not ordered newest first')
            previous_day = released
            page_days.append(released)
            if released < cutoff:
                continue
            newest = max(newest, released)
            landing = ons_url(release.get('uri'))
            if landing is None:
                raise ValueError('Invalid release landing URL')
            for doc_uri in extract_doc_urls(client.get_text(landing), landing):
                if doc_uri not in publications:
                    publications[doc_uri] = parse_publication(doc_uri, client.get_text(doc_uri))
        if len(releases) < settings['page_size'] or page_days and max(page_days) < cutoff:
            return DiscoveryResult(list(publications.values()), newest.isoformat())
    raise RuntimeError('Discovery page cap reached before coverage completed; checkpoint not advanced')


def prepare_run(seed, config, state, client):
    cache = {}
    for record in sorted(seed, key=lambda item: normalize_date(item.get('release_date'))):
        doc_uri = ons_url(record.get('doc_uri'))
        if doc_uri is None:
            raise ValueError(f'Invalid cached doc_uri: {record.get("doc_uri")}')
        cache[doc_uri] = {**record, 'doc_uri': doc_uri,
                          'vis_urls': list(dict.fromkeys(path for url in record.get('vis_urls', [])
                                                        if (path := visualisation_path(url, doc_uri))))}
    today = date.today()
    if state is not None:
        if not isinstance(state, dict) or set(state) != {'last_release_date'}:
            raise ValueError('Invalid discovery state')
        baseline = date.fromisoformat(normalize_date(state['last_release_date']))
        if baseline > today:
            raise ValueError('Discovery checkpoint is in the future')
    else:
        known_days = [date.fromisoformat(normalize_date(record['release_date'])) for record in cache.values()]
        baseline = min(max(known_days, default=today), today)
    cutoff = baseline - timedelta(days=config['discovery']['lookback_days'])
    discovery = discover_publications(client, cutoff, config['discovery'], baseline)
    selected = {record['doc_uri']: record for record in discovery.publications}
    cache.update(selected)
    if config['mode'] == 'rebuild':
        for doc_uri in cache:
            if doc_uri not in selected:
                selected[doc_uri] = parse_publication(doc_uri, client.get_text(doc_uri))
        cache.update(selected)
    ordered_cache = sorted(cache.values(), key=lambda item: normalize_date(item['release_date']), reverse=True)
    ordered_selected = sorted(deepcopy(list(selected.values())),
                              key=lambda item: normalize_date(item['release_date']), reverse=True)
    return ordered_cache, ordered_selected, {'last_release_date': discovery.last_release_date}


def write_json_files(files):
    staged = []
    try:
        for filename, value in files:
            path = Path(filename)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                             prefix='.catalogue-', suffix='.json', delete=False) as output:
                staged.append((output.name, path))
                json.dump(value, output, indent=6)
        for temporary_path, path in staged:
            os.replace(temporary_path, path)
    finally:
        for temporary_path, path in staged:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)


def atomic_write_json(path, value):
    write_json_files([(path, value)])