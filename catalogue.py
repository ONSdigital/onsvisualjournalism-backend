import asyncio
from datetime import datetime
import json
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
import urllib3

from ons_discovery import (DiscoveryClient, classify_visualisations, is_png,
                           prepare_catalogue, prepare_run, validate_config,
                           write_json_files)
from ons_screenshots import capture_screenshots, finish_screenshots


MONTH = (
    r"(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)"
)
YEAR = r"(?:19|20)\d{2}"
DATE_POINT = rf"(?:(?:[0-3]?\d\s+)?{MONTH}\s+)?{YEAR}"
DATE_PERIOD = (
    rf"(?:{MONTH}\s+to\s+{MONTH}\s+{YEAR}|"
    rf"{DATE_POINT}(?:\s+to\s+{DATE_POINT})?)"
)
DATE_SUFFIX = re.compile(
    rf"\s*[:,]\s*(?:(?:between|(?:financial\s+)?year\s+ending)\s+)?"
    rf"{DATE_PERIOD}(?:\s+and\s+{DATE_PERIOD})*\s*$|"
    rf"\s*[:,]\s*mid-{YEAR}\s*$",
    re.IGNORECASE,
)


def derive_series(title):
    cleaned_title = title.strip()
    series = DATE_SUFFIX.sub("", cleaned_title).strip()
    return series or cleaned_title


def extract_edition(html, title):
    heading = BeautifulSoup(html, "lxml").find("h1")
    if heading is None:
        return None

    heading_text = " ".join(heading.get_text(" ", strip=True).split())
    title_prefix = " ".join(title.split()).rstrip(":") + ":"
    if heading_text.startswith(title_prefix):
        return heading_text[len(title_prefix):].strip() or None

    suffix = DATE_SUFFIX.search(heading_text)
    if suffix is not None:
        return suffix.group().lstrip(" :,").strip() or None
    return None


def publication_date(article):
    value = article.get('release_date')
    message = f"Invalid release_date for {article.get('doc_uri', 'unknown release')}: {value!r}"
    if not isinstance(value, str):
        raise ValueError(message)
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).date()
    except ValueError as error:
        raise ValueError(message) from error


def mark_latest(records):
    newest_dates = {}
    dated_records = []
    for article in records:
        released = publication_date(article)
        series = article['series']
        if series not in newest_dates or released > newest_dates[series]:
            newest_dates[series] = released
        dated_records.append((article, released))

    for article, released in dated_records:
        article['latest'] = released == newest_dates[article['series']]


def extract_themes(doc_uri):
    segments = [segment for segment in urlsplit(doc_uri).path.split('/') if segment]
    boundary = next((index for index, segment in enumerate(segments)
                     if segment in ('articles', 'bulletins', 'methodologies')), None)
    if boundary is None or not 1 <= boundary <= 3:
        raise ValueError(f"Unsupported theme hierarchy in doc_uri: {doc_uri}")

    hierarchy = segments[:boundary]
    return {name: hierarchy[index] if index < len(hierarchy) else None
            for index, name in enumerate(('theme', 'topic', 'subtopic'))}


async def main():
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    with open('config.json', 'r', encoding='utf-8') as config_file:
        config = validate_config(json.load(config_file))

    print(f"Discovery mode: {config['discovery']['mode']}; "
          f"catalogue mode: {config['catalogue']['mode']}; "
          f"screenshots mode: {config['screenshots']['mode']}", flush=True)

    try:
        with open('articles-and-dvcs.json', 'r', encoding='utf-8') as input_file:
            seed = json.load(input_file)
    except FileNotFoundError:
        seed = []

    state = None
    if config['discovery']['mode'] != 'off':
        try:
            with open('discovery-state.json', 'r', encoding='utf-8') as state_file:
                state = json.load(state_file)
        except FileNotFoundError:
            pass

    client = DiscoveryClient()
    try:
        cached_publications, results, next_state = prepare_run(seed, config, state, client)

        existing_data = []
        if config['catalogue']['mode'] != 'off':
            try:
                with open('data.json', 'r', encoding='utf-8') as existing_file:
                    existing_data = json.load(existing_file)
            except FileNotFoundError:
                pass

        pages = prepare_catalogue(cached_publications, existing_data, config, results,
                                  client, extract_edition)

        files = []
        if pages is not None:
            merged = sorted(pages, key=publication_date, reverse=True)
            for article in merged:
                themes = extract_themes(article['doc_uri'])
                for field in themes:
                    article.pop(field, None)
                article.update(themes)
                article['series'] = derive_series(article['title'])
                article.setdefault('edition', None)
                article['vis'] = classify_visualisations(
                    [vis for vis in article.get('vis', []) if not is_png(vis['url'])])
                article.pop('vis_urls', None)
            mark_latest(merged)
            files.append(('data.json', merged))

        if (config['discovery']['mode'] != 'off' or pages is not None) and cached_publications != seed:
            files.append(('articles-and-dvcs.json', cached_publications))
        if config['discovery']['mode'] in ('new', 'all') and next_state != state:
            files.append(('discovery-state.json', next_state))
        write_json_files(files)
        screenshot_summary = await capture_screenshots(cached_publications, config['screenshots'])
        print(f"Completed: {len(pages) if pages is not None else 'disabled'} catalogue records; "
              f"{len(cached_publications)} cached publications; "
              f"written files: {[item[0] for item in files]}", flush=True)
    finally:
        client.session.close()

    finish_screenshots(screenshot_summary)


if __name__ == '__main__':
    asyncio.run(main())