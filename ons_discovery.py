import json
import os
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
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
    if not isinstance(config, dict) or set(config) - {'discovery', 'catalogue', 'screenshots'}:
        raise ValueError('Unsupported pipeline configuration')
    supplied = config.get('discovery', {})
    if not isinstance(supplied, dict):
        raise ValueError('Unsupported discovery settings')
    discovery = {'mode': 'new', 'limit': None, 'url': None, 'start_date': None, **supplied}
    if set(discovery) - {'mode', 'limit', 'url', 'start_date'}:
        raise ValueError('Unsupported discovery settings')
    if discovery['mode'] not in ('off', 'new', 'recent', 'all', 'datestart', 'url'):
        raise ValueError('Unsupported discovery mode')
    validate_limit(discovery, 'Discovery')
    validate_source_selection(discovery, 'Discovery')

    supplied = config.get('screenshots', {})
    if not isinstance(supplied, dict):
        raise ValueError('Unsupported screenshots settings')
    screenshots = {'mode': 'off', 'limit': None, 'url': None, 'start_date': None, **supplied}
    if set(screenshots) - {'mode', 'limit', 'url', 'start_date'}:
        raise ValueError('Unsupported screenshots settings')
    if screenshots['mode'] not in ('off', 'new', 'recent', 'all', 'datestart', 'url'):
        raise ValueError('Unsupported screenshots mode')
    validate_limit(screenshots, 'Screenshots')
    validate_source_selection(screenshots, 'Screenshots')

    supplied = config.get('catalogue', {})
    if not isinstance(supplied, dict):
        raise ValueError('Unsupported catalogue settings')
    catalogue = {'mode': 'new', 'limit': None, **supplied}
    if set(catalogue) - {'mode', 'limit'}:
        raise ValueError('Unsupported catalogue settings')
    if catalogue['mode'] not in ('off', 'new', 'recent', 'all'):
        raise ValueError('Unsupported catalogue mode')
    validate_limit(catalogue, 'Catalogue')
    return {'discovery': discovery, 'catalogue': catalogue, 'screenshots': screenshots}


def validate_source_selection(settings, stage):
    if settings['mode'] == 'url':
        value = settings['url']
        url = ons_url(value) if isinstance(value, str) and value.startswith(('https://', 'http://')) else None
        if url is None or not extract_doc_urls(f'<a href="{url}"></a>', ONS_URL):
            raise ValueError(f'{stage} mode url requires an absolute ONS publication URL')
        settings['url'] = url
    if settings['mode'] == 'datestart':
        value = settings['start_date']
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError(f'{stage} mode datestart requires start_date as YYYY-MM-DD')


def validate_limit(settings, stage):
    limit = settings['limit']
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError(f'{stage} limit must be null or a positive integer')
    if settings['mode'] == 'recent' and limit is None:
        raise ValueError(f'{stage} mode recent requires a positive limit')


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
    coverage_complete: bool = True


def discover_publications(client, cutoff, settings, baseline, known=None, refresh_known=True):
    publications = {}
    inspected = set()
    known = known or {}
    newest = baseline
    previous_day = None
    page = 0
    previous_page = None
    while settings['mode'] == 'datestart' or page < settings['max_pages']:
        print(f'Discovery: search page {page + 1}', flush=True)
        releases = client.get_releases(settings['page_size'], page * settings['page_size'])
        if not releases:
            return DiscoveryResult(list(publications.values()), newest.isoformat())
        page_signature = json.dumps(releases, sort_keys=True)
        if page_signature == previous_page:
            raise RuntimeError('ONS release search repeated a page; checkpoint not advanced')
        previous_page = page_signature
        page += 1
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
                if doc_uri in inspected:
                    continue
                inspected.add(doc_uri)
                if refresh_known or doc_uri not in known:
                    publication = parse_publication(doc_uri, client.get_text(doc_uri))
                    if settings['mode'] != 'datestart' or normalize_date(publication['release_date']) > settings['start_date']:
                        publications[doc_uri] = publication
                if settings['limit'] is not None and len(publications) >= settings['limit']:
                    return DiscoveryResult(list(publications.values()), newest.isoformat(), False)
        if len(releases) < settings['page_size'] or page_days and max(page_days) < cutoff:
            return DiscoveryResult(list(publications.values()), newest.isoformat())
    raise RuntimeError('Discovery page cap reached before coverage completed; checkpoint not advanced')


def prepare_run(seed, config, state, client):
    settings = {**DEFAULT_DISCOVERY, **config['discovery']}
    mode = settings['mode']
    cache = {}
    for record in sorted(seed, key=lambda item: normalize_date(item.get('release_date'))):
        doc_uri = ons_url(record.get('doc_uri'))
        if doc_uri is None:
            raise ValueError(f'Invalid cached doc_uri: {record.get("doc_uri")}')
        cache[doc_uri] = {**deepcopy(record), 'doc_uri': doc_uri,
                          'vis_urls': eligible_vis_urls(record.get('vis_urls', []), doc_uri)}
    if mode in ('off', 'url'):
        selected = {}
        if mode == 'url':
            doc_uri = settings['url']
            selected[doc_uri] = parse_publication(doc_uri, client.get_text(doc_uri))
        merge_publications(cache, selected)
        return (sorted(cache.values(), key=lambda item: normalize_date(item['release_date']), reverse=True),
                deepcopy(list(selected.values())), deepcopy(state))
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
    cutoff = baseline - timedelta(days=settings['lookback_days'])
    if mode == 'datestart':
        cutoff = date.fromisoformat(settings['start_date']) + timedelta(days=1)
    elif mode == 'recent':
        cutoff = date.min
    refresh_known = mode != 'new'
    discovery = discover_publications(client, cutoff, settings, baseline, cache, refresh_known)
    selected = {record['doc_uri']: record for record in discovery.publications}
    merge_publications(cache, selected)
    if mode == 'all':
        ordered = sorted(cache, key=lambda doc_uri: normalize_date(cache[doc_uri]['release_date']), reverse=True)
        for index, doc_uri in enumerate(ordered, start=1):
            if doc_uri not in selected:
                if settings['limit'] is not None and len(selected) >= settings['limit']:
                    discovery.coverage_complete = False
                    break
                selected[doc_uri] = parse_publication(doc_uri, client.get_text(doc_uri))
            if index == 1 or index % 25 == 0:
                print(f'Discovery: refreshed {index}/{len(cache)} known publications', flush=True)
        merge_publications(cache, selected)
    ordered_cache = sorted(cache.values(), key=lambda item: normalize_date(item['release_date']), reverse=True)
    ordered_selected = sorted(deepcopy(list(selected.values())),
                              key=lambda item: normalize_date(item['release_date']), reverse=True)
    next_state = ({'last_release_date': discovery.last_release_date}
                  if mode in ('new', 'all') and discovery.coverage_complete else deepcopy(state))
    if mode in ('new', 'all') and state is None and not discovery.coverage_complete:
        next_state = {'last_release_date': baseline.isoformat()}
    return ordered_cache, ordered_selected, next_state


def merge_publications(cache, publications):
    for doc_uri, publication in publications.items():
        previous = cache.get(doc_uri)
        if previous is not None and source_signature(previous) == source_signature(publication):
            metadata = saved_metadata(previous)
            if metadata is not None:
                publication['enrichment'] = metadata
        cache[doc_uri] = publication


def eligible_vis_urls(urls, base=ONS_URL):
    return list(dict.fromkeys(path for url in urls
                              if (path := visualisation_path(url, base))
                              and 'datadownload.xlsx' not in path.lower()))


def source_signature(record):
    source = {'doc_uri': ons_url(record['doc_uri']),
              'title': record['title'], 'release_date': normalize_date(record['release_date']),
              'vis_urls': sorted(eligible_vis_urls(record.get('vis_urls', []), record['doc_uri']))}
    return hashlib.sha256(json.dumps(source, sort_keys=True).encode('utf-8')).hexdigest()


def saved_metadata(record, existing=None):
    signature = source_signature(record)
    metadata = record.get('enrichment')
    if not isinstance(metadata, dict) or metadata.get('source_signature') != signature:
        metadata = None
    if metadata is None and existing is not None:
        old_source = {**existing, 'vis_urls': [vis['url'] for vis in existing.get('vis', [])]}
        if source_signature(old_source) == signature:
            metadata = existing
    if metadata is None or 'edition' not in metadata or not isinstance(metadata.get('vis'), list):
        return None
    if metadata['edition'] is not None and not isinstance(metadata['edition'], str):
        return None
    charts = {}
    for chart in metadata['vis']:
        if not isinstance(chart, dict) or 'url' not in chart or 'chart_type' not in chart:
            return None
        if chart['chart_type'] is not None and not isinstance(chart['chart_type'], str):
            return None
        url = visualisation_path(chart['url'], record['doc_uri'])
        if url is not None:
            charts[url] = {'url': url, 'chart_type': chart['chart_type']}
    urls = eligible_vis_urls(record.get('vis_urls', []), record['doc_uri'])
    if set(charts) != set(urls):
        return None
    return {'source_signature': signature, 'edition': metadata['edition'],
            'vis': [charts[url] for url in urls]}


def extract_chart_type(html):
    soup = BeautifulSoup(html, 'lxml')
    meta = soup.find('meta', attrs={'name': 'template'})
    if meta is None:
        meta = soup.find('meta', attrs={'name': lambda value: value and 'template' in value.lower()})
    if meta is None:
        meta = soup.find('meta', attrs={'property': lambda value: value and 'template' in value.lower()})
    return meta.get('content') if meta else None


CHART_FAMILIES = ('bar', 'line', 'area', 'scatter', 'range', 'map', 'heatmap', 'table',
                  'population-pyramid')
CHART_VARIANTS = ('grouped', 'stacked', 'small-multiple', 'multi-series', 'bubble',
                  'beeswarm', 'choropleth', 'focus')
CHART_FEATURES = ('dropdown', 'confidence-interval', 'range-slider')
CHART_TYPE_CATEGORIES = {
    'bar-chart': (('bar',), ()),
    'column-chart': (('bar',), ()),
    'column-chart-sm': (('bar',), ()),
    'split-bar-chart': (('bar',), ()),
    'line-chart': (('line',), ()),
    'line-chart-with-area': (('line',), ()),
    'line-chart-multiline': (('line',), ('multi-series',)),
    'line-chart-focus': (('line',), ('focus',)),
    'area-chart': (('area',), ()),
    'stacked-area-chart': (('area',), ('stacked',)),
    'scatter-plot': (('scatter',), ()),
    'scatter-bubble-plot': (('scatter',), ('bubble',)),
    'dot-plot': (('scatter',), ()),
    'beeswarm': (('scatter',), ('beeswarm',)),
    'range-plot': (('range',), ()),
    'range-arrow-dot-bar-reference-plot': (('range',), ()),
    'choropleth': (('map',), ('choropleth',)),
    'map': (('map',), ()),
    'heatmap': (('heatmap',), ()),
    'grouped-row-table': (('table',), ('grouped',)),
    'table': (('table',), ()),
    'population-pyramid': (('population-pyramid',), ()),
    'static-population-pyramid': (('population-pyramid',), ()),
    'grouped-bar-chart': (('bar',), ('grouped',)),
    'clustered-bar-chart': (('bar',), ('grouped',)),
    'grouped-clustered-bar-chart': (('bar',), ('grouped',)),
    'stacked-bar-chart': (('bar',), ('stacked',)),
    'stacked-column-chart': (('bar',), ('stacked',)),
    'stacked-horizontal-bar-chart': (('bar',), ('stacked',)),
    'grouped-stacked-bar-chart': (('bar',), ('grouped', 'stacked')),
    'column-and-line-chart': (('bar', 'line'), ()),
    'column-and-line': (('bar', 'line'), ()),
}


def derive_chart_taxonomy(chart_type):
    empty = {'chart_families': [], 'chart_variants': [], 'chart_features': []}
    if not isinstance(chart_type, str):
        return empty
    tag = re.sub(r'[\s_]+', '-', chart_type.strip().lower())
    variants = set()
    features = set()
    if tag.startswith('small-multiple-'):
        tag = tag.removeprefix('small-multiple-')
        variants.add('small-multiple')
    suffixes = {
        '-small-multiple': ('variant', 'small-multiple'),
        '-with-dropdown': ('feature', 'dropdown'),
        '-with-range-slider': ('feature', 'range-slider'),
        '-with-confidence-intervals': ('feature', 'confidence-interval'),
        '-with-confidence-interval': ('feature', 'confidence-interval'),
        '-with-confidence-bands': ('feature', 'confidence-interval'),
        '-ci-bands': ('feature', 'confidence-interval'),
    }
    while True:
        suffix = next((value for value in suffixes if tag.endswith(value)), None)
        if suffix is None:
            break
        kind, value = suffixes[suffix]
        (variants if kind == 'variant' else features).add(value)
        tag = tag[:-len(suffix)]
    categories = CHART_TYPE_CATEGORIES.get(tag)
    if categories is None:
        return empty
    families, base_variants = categories
    variants.update(base_variants)
    return {'chart_families': [value for value in CHART_FAMILIES if value in families],
            'chart_variants': [value for value in CHART_VARIANTS if value in variants],
            'chart_features': [value for value in CHART_FEATURES if value in features]}


def classify_visualisations(visualisations):
    return [{**chart, **derive_chart_taxonomy(chart.get('chart_type'))} for chart in visualisations]


def prepare_catalogue(cached_publications, existing_data, config, refreshed, client, extract_edition):
    settings = config['catalogue']
    if settings['mode'] == 'off':
        return None
    existing = {}
    for article in sorted(existing_data, key=lambda item: normalize_date(item['release_date'])):
        doc_uri = ons_url(article['doc_uri'])
        if doc_uri is None:
            raise ValueError(f"Invalid existing doc_uri: {article['doc_uri']}")
        existing[doc_uri] = {**deepcopy(article), 'doc_uri': doc_uri}
    ordered = sorted(cached_publications,
                     key=lambda record: (-date.fromisoformat(normalize_date(record['release_date'])).toordinal(),
                                         record['doc_uri']))
    refreshed_urls = {record['doc_uri'] for record in refreshed}
    selected = []
    for record in ordered:
        doc_uri = record['doc_uri']
        old = existing.get(doc_uri)
        urls = eligible_vis_urls(record.get('vis_urls', []), doc_uri)
        if settings['mode'] == 'new' and doc_uri not in refreshed_urls:
            if not urls and old is None:
                continue
            metadata = saved_metadata(record, old)
            if old is not None and metadata is not None:
                old_source = {**old, 'vis_urls': [vis['url'] for vis in old.get('vis', [])]}
                old_charts = [{'url': vis['url'], 'chart_type': vis.get('chart_type')}
                          for vis in old.get('vis', [])]
                if (urls and source_signature(old_source) == source_signature(record)
                        and old.get('edition') == metadata['edition']
                    and old_charts == metadata['vis']):
                    continue
        selected.append(record)
        if settings['limit'] is not None and len(selected) >= settings['limit']:
            break
    metadata_lookup = {record['doc_uri']: (None if record['doc_uri'] in refreshed_urls
                                         else saved_metadata(record, existing.get(record['doc_uri'])))
                       for record in selected}
    replace_all = settings['mode'] == 'all' and len(selected) == len(cached_publications)
    output = {} if replace_all else deepcopy(existing)
    fetched = 0
    reused = 0
    print(f"Catalogue: {settings['mode']}, limit={settings['limit']}, candidates={len(selected)}", flush=True)
    for index, record in enumerate(selected, start=1):
        doc_uri = record['doc_uri']
        metadata = metadata_lookup[doc_uri]
        urls = eligible_vis_urls(record.get('vis_urls', []), doc_uri)
        if urls and metadata is None:
            html = client.get_text(doc_uri)
            record.update(parse_publication(doc_uri, html))
            urls = eligible_vis_urls(record['vis_urls'], doc_uri)
            record['vis_urls'] = urls
            metadata = None
            fetched += 1
        elif doc_uri in client.html_cache:
            html = client.html_cache[doc_uri]
        else:
            html = None
        if not urls:
            output.pop(doc_uri, None)
            record.pop('enrichment', None)
        else:
            if metadata is None:
                edition = extract_edition(html, record['title'])
                charts = [{'url': url, 'chart_type': extract_chart_type(client.get_text(ons_url(url)))}
                          for url in urls]
                metadata = {'source_signature': source_signature(record), 'edition': edition, 'vis': charts}
            else:
                reused += 1
                if html is not None:
                    metadata['edition'] = extract_edition(html, record['title'])
            record['enrichment'] = deepcopy(metadata)
            output[doc_uri] = {'doc_uri': doc_uri, 'release_date': record['release_date'],
                               'title': record['title'], 'edition': metadata['edition'],
                               'vis': deepcopy(metadata['vis'])}
        if index == 1 or index % 25 == 0 or index == len(selected):
            print(f'Catalogue: processed {index}/{len(selected)}, reused={reused}, '
                  f'publication fetches={fetched}', flush=True)
    return list(output.values())


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