import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from ons_discovery import atomic_write_json, eligible_vis_urls, normalize_date, ons_url


CAPTURE_WIDTH = 700
TIMEOUT_MS = 30_000
READINESS_TIMEOUT_MS = 90_000
PIXEL_QUIET_SECONDS = 2
MAP_QUIET_SECONDS = 10
CAPTURE_TIMEOUT_SECONDS = 300


class ChartCaptureError(RuntimeError):
    pass


class ScreenshotFailures(RuntimeError):
    pass


@dataclass
class CaptureSummary:
    succeeded: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class Measurement:
    viewport_width: int
    x: int
    y: int
    width: int
    height: int

def legacy_screenshot_filename(url):
    parsed = urlsplit(url)
    prefix = re.sub(r'[^A-Za-z0-9_-]+', '-', parsed.path).strip('-')[-64:]
    return f'{prefix}-{hashlib.sha256(url.encode("utf-8")).hexdigest()}.jpg'


def screenshot_filename(url, owners=None):
    parts = [Path(part).stem for part in urlsplit(url).path.split('/') if part]
    dvc = next((part.lower() for part in parts if re.fullmatch(r'dvc\d+', part, re.I)), None)
    figure = next((match for part in parts
                   if (match := re.fullmatch(r'fig(?:ure)?[-_]?(\d+)([a-z]?)', part, re.I))), None)
    if dvc and figure:
        number = figure[1].lstrip('0').zfill(2)
        stem = f'{dvc}-fig{number}{figure[2].lower()}'
    else:
        stem = '-'.join(part for part in parts if part.lower() not in ('visualisations', 'index'))
        stem = re.sub(r'[^a-z0-9_-]+', '-', stem.lower()).strip('-')[-80:] or 'visualisation'
        if re.fullmatch(r'con|prn|aux|nul|com[1-9]|lpt[1-9]', stem):
            stem = f'visualisation-{stem}'
    owners = owners if owners is not None else {}
    candidate = f'{stem}.jpg'
    digest = hashlib.sha256(url.encode('utf-8')).hexdigest()[:8]
    counter = 1
    while candidate in owners and owners[candidate] != url:
        suffix = f'-{counter}' if counter > 1 else ''
        candidate = f'{stem}-{digest}{suffix}.jpg'
        counter += 1
    return candidate


def valid_screenshot_filename(url, filename):
    if not isinstance(filename, str):
        return False
    base = screenshot_filename(url)
    digest = hashlib.sha256(url.encode('utf-8')).hexdigest()[:8]
    collision = rf'{re.escape(base[:-4])}-{digest}(?:-\d+)?\.jpg'
    return filename in (base, legacy_screenshot_filename(url)) or re.fullmatch(collision, filename) is not None


def canonical_charts(publication):
    return [ons_url(path, keep_fragment=True)
            for path in eligible_vis_urls(publication.get('vis_urls', []), publication['doc_uri'])]


def load_index(folder):
    path = folder / 'index.json'
    if not path.exists():
        return {'version': 1, 'charts': {}}
    with path.open(encoding='utf-8') as source:
        index = json.load(source)
    if (not isinstance(index, dict) or set(index) != {'version', 'charts'}
            or type(index['version']) is not int or index['version'] != 1
            or not isinstance(index['charts'], dict)):
        raise ValueError('Invalid screenshot index')
    filenames = set()
    for url, entry in index['charts'].items():
        if (ons_url(url, keep_fragment=True) != url
                or not isinstance(entry, dict)
                or not valid_screenshot_filename(url, entry.get('filename'))
                or not isinstance(entry.get('publications'), list)
                or any(not isinstance(doc, str) or ons_url(doc) != doc
                       for doc in entry['publications'])):
            raise ValueError(f'Invalid screenshot index entry: {url}')
        filename = entry['filename'].casefold()
        if filename in filenames:
            raise ValueError(f'Duplicate screenshot filename: {filename}')
        filenames.add(filename)
        attempt = entry.get('last_attempt')
        if (not isinstance(attempt, dict) or attempt.get('status') not in ('success', 'failed')
                or not isinstance(attempt.get('at'), str)
                or attempt['status'] == 'failed' and not isinstance(attempt.get('error'), str)):
            raise ValueError(f'Invalid screenshot attempt: {url}')
        success = entry.get('last_success')
        if success is not None:
            if (not isinstance(success, dict)
                    or any(type(success.get(key)) is not int or success[key] < 1
                           for key in ('width', 'height', 'viewport_width'))
                    or success['width'] > CAPTURE_WIDTH
                    or success['viewport_width'] > CAPTURE_WIDTH
                    or not isinstance(success.get('captured_at'), str)):
                raise ValueError(f'Invalid screenshot success metadata: {url}')
        elif attempt['status'] == 'success':
            raise ValueError(f'Screenshot success metadata missing: {url}')
    return index


def migrate_screenshot_filenames(associations, index, folder):
    urls = sorted(set(associations) | set(index['charts']))
    owners = {entry['filename'].casefold(): url for url, entry in index['charts'].items()}
    if folder.exists():
        for path in folder.iterdir():
            owners.setdefault(path.name.casefold(), None)
    for url in urls:
        legacy = legacy_screenshot_filename(url).casefold()
        if legacy in owners:
            owners[legacy] = url
    assigned = {url: entry['filename'] for url, entry in index['charts'].items()
                if entry['filename'] != legacy_screenshot_filename(url)}
    for url in urls:
        if url not in assigned:
            assigned[url] = screenshot_filename(url, owners)
            owners[assigned[url]] = url
    updated = deepcopy(index)
    renames = []
    recovered = 0
    for url in urls:
        source = folder / legacy_screenshot_filename(url)
        destination = folder / assigned[url]
        if source.is_file() and source.name != destination.name:
            if destination.exists():
                raise FileExistsError(f'Both legacy and renamed screenshot exist: {source}, {destination}')
            renames.append((source, destination))
            if url not in updated['charts']:
                updated['charts'][url] = {
                    'filename': assigned[url], 'publications': sorted(associations.get(url, [])),
                    'last_success': None,
                    'last_attempt': {
                        'at': datetime.now(timezone.utc).isoformat(), 'status': 'failed',
                        'error': 'Image recovered from an interrupted run; capture metadata is missing. Recapture required.',
                    },
                }
                recovered += 1
        if url in updated['charts']:
            updated['charts'][url]['filename'] = assigned[url]
    renamed = []
    try:
        for source, destination in renames:
            source.rename(destination)
            renamed.append((source, destination))
        if updated != index:
            atomic_write_json(folder / 'index.json', updated)
    except OSError:
        for source, destination in reversed(renamed):
            destination.rename(source)
        raise
    index.clear()
    index.update(updated)
    if renamed:
        print(f'Screenshots: renamed {len(renamed)} existing JPGs', flush=True)
    if recovered:
        print(f'Screenshots: recovered {recovered} interrupted captures; '
              'missing capture metadata requires recapture', flush=True)
    return assigned


def needs_capture(url, index, folder):
    entry = index['charts'].get(url)
    return (entry is None or entry.get('last_success') is None
            or entry['last_attempt']['status'] == 'failed'
            or not (folder / entry['filename']).is_file())


def select_publications(publications, settings, index, folder):
    mode = settings['mode']
    if mode == 'off':
        return []
    ordered = sorted(publications, key=lambda record: (
        -datetime.fromisoformat(normalize_date(record['release_date'])).toordinal(), record['doc_uri']))
    if mode == 'url' and not any(record['doc_uri'] == settings['url'] for record in ordered):
        raise ValueError('Screenshot publication is not cached; run discovery mode url first or alongside screenshots')
    selected = []
    for publication in ordered:
        urls = canonical_charts(publication)
        if not urls:
            continue
        if mode == 'url' and publication['doc_uri'] != settings['url']:
            continue
        if mode == 'datestart' and normalize_date(publication['release_date']) <= settings['start_date']:
            continue
        if mode == 'new' and not any(needs_capture(url, index, folder) for url in urls):
            continue
        selected.append(publication)
        if settings['limit'] is not None and len(selected) >= settings['limit']:
            break
    return selected


FIND_CHART = r"""() => {
    const visible = e => {
        const box = e.getBoundingClientRect(), style = getComputedStyle(e);
        return box.width > 0 && box.height > 0 && style.display !== 'none'
            && style.visibility !== 'hidden' && style.opacity !== '0';
    };
    const rendered = e => {
        const surfaces = e.matches('svg,canvas,table,img') ? [e]
            : [...e.querySelectorAll('svg,canvas,table,img')];
        return surfaces.some(s => visible(s) && (
            s.matches('svg') && s.querySelector('path,rect,circle,line,text,polygon,image')
            || s.matches('canvas') && s.width > 0 && s.height > 0
            || s.matches('table') && s.rows.length > 0
            || s.matches('img') && s.complete && s.naturalWidth > 0
                && !/fallback/i.test(s.getAttribute('src') || '')
        ));
    };
    let candidates = [...document.querySelectorAll(
        '#graphic, #chart, #map, #map-container, #mapContainer'
    )].filter(e => visible(e) && rendered(e));
    if (!candidates.length) {
        candidates = [...document.querySelectorAll('body > svg, body > img, body > table')]
            .filter(e => visible(e) && rendered(e));
        if (document.documentElement.tagName.toLowerCase() === 'svg'
                && rendered(document.documentElement)) candidates = [document.documentElement];
    }
    candidates = candidates.filter(e => !candidates.some(other => other !== e && other.contains(e)));
    if (!candidates.length) return {error: 'No supported rendered plotting container'};
    let element = candidates[0].parentElement?.closest('div')
        || document.body || document.documentElement;
    const components = [...document.querySelectorAll(
        '#legend, #source, #controls, #notes, #keycontainer, #timeline-container'
    )].filter(visible);
    for (const component of [...candidates, ...components]) {
        while (!element.contains(component)) {
            element = element.parentElement?.closest('div')
                || document.body || document.documentElement;
        }
    }
    const box = element.getBoundingClientRect();
    const surfaces = [...new Set(candidates.flatMap(e => e.matches('svg,canvas,table,img')
        ? [e] : [...e.querySelectorAll('svg,canvas,table,img')]))]
        .filter(s => visible(s) && !s.closest(
            'button, [role="button"], #legend, .legend, .maplibregl-ctrl, .mapboxgl-ctrl'
        ));
    if (!surfaces.length) return {error: 'No supported rendered plotting container'};
    if (element.scrollWidth > Math.ceil(box.width) + 1
            || element.scrollHeight > Math.ceil(box.height) + 1
            || surfaces.some(s => {
                const r = s.getBoundingClientRect();
                return r.left < box.left - 1 || r.right > box.right + 1
                    || r.top < box.top - 1 || r.bottom > box.bottom + 1;
            })) return {error: 'Visualisation content overflows its container'};
    return {x: box.x + scrollX, y: box.y + scrollY, width: box.width, height: box.height,
        map: !!element.querySelector(
            '.maplibregl-canvas, .mapboxgl-canvas, .leaflet-container, .ol-viewport'),
        plots: surfaces.map(s => {
            const r = s.getBoundingClientRect();
            return {x:r.x + scrollX, y:r.y + scrollY, width:r.width, height:r.height};
        })};
}"""


# Module-scoped map instances are not available through window.map.
TRACK_MAPS = r"""(() => {
    if (globalThis.__onsScreenshotMaps) return;
    const maps = globalThis.__onsScreenshotMaps = new Set();
    const constructors = new WeakMap();
    const wrap = library => library && new Proxy(library, {
        get(target, key, receiver) {
            const value = Reflect.get(target, key, receiver);
            if (key !== 'Map' || typeof value !== 'function') return value;
            if (!constructors.has(value)) constructors.set(value, new Proxy(value, {
                construct(target, args, newTarget) {
                    const instance = Reflect.construct(target, args, newTarget);
                    maps.add(instance);
                    if (typeof instance.on === 'function')
                        instance.on('remove', () => maps.delete(instance));
                    return instance;
                }
            }));
            return constructors.get(value);
        }
    });
    for (const name of ['maplibregl', 'mapboxgl']) {
        let library = wrap(globalThis[name]);
        Object.defineProperty(globalThis, name, {
            configurable: true, enumerable: true,
            get: () => library,
            set: value => { library = wrap(value); }
        });
    }
})()"""


MAPS_READY = r"""() => {
    const maps = [...(globalThis.__onsScreenshotMaps || []),
        globalThis.map, globalThis.map1, globalThis.map2]
        .filter(m => m && typeof m.loaded === 'function');
    return maps.every(m => m.loaded()
        && (typeof m.isStyleLoaded !== 'function' || m.isStyleLoaded())
        && (typeof m.areTilesLoaded !== 'function' || m.areTilesLoaded())
        && (typeof m.isMoving !== 'function' || !m.isMoving()));
}"""


CAPTURE_READY = r"""() => {
    const visible = element => {
        const box = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return box.width > 0 && box.height > 0
            && style.display !== 'none' && style.visibility !== 'hidden';
    };
    return document.fonts.status === 'loaded'
        && [...document.images].filter(visible).every(img => img.complete && img.naturalWidth > 0)
        && ![...document.querySelectorAll('[aria-busy="true"], [role="progressbar"]')]
            .some(visible)
        && (""" + MAPS_READY + r""")();
}"""


async def measure_chart(page, url, width):
    await page.set_viewport_size({'width': width, 'height': 1000})
    response = await page.goto(url, wait_until='load', timeout=TIMEOUT_MS)
    if response is not None and not response.ok:
        raise ChartCaptureError(f'HTTP {response.status}: {url}')
    if ons_url(page.url, keep_fragment=True) is None:
        raise ChartCaptureError(f'Non-ONS redirect rejected: {page.url}')
    await page.wait_for_function("() => document.fonts.status === 'loaded'",
                                 timeout=READINESS_TIMEOUT_MS)
    await page.wait_for_function(
        f'() => ({FIND_CHART})().error !== "No supported rendered plotting container"',
        timeout=READINESS_TIMEOUT_MS)
    await page.wait_for_load_state('networkidle', timeout=READINESS_TIMEOUT_MS)
    box = await page.evaluate(FIND_CHART)
    if box.get('map'):
        await page.wait_for_function(MAPS_READY, timeout=READINESS_TIMEOUT_MS)
    previous = None
    stable = 0
    for _ in range(20):
        box = await page.evaluate(FIND_CHART)
        if box.get('error'):
            raise ChartCaptureError(box['error'])
        bounds = tuple(round(box[key], 2) for key in ('x', 'y', 'width', 'height'))
        stable = stable + 1 if bounds == previous else 0
        if stable >= 2:
            break
        previous = bounds
        await asyncio.sleep(0.15)
    else:
        raise ChartCaptureError('Chart layout did not stabilise')
    left, top = math.floor(box['x']), math.floor(box['y'])
    right, bottom = math.ceil(box['x'] + box['width']), math.ceil(box['y'] + box['height'])
    if left < 0 or top < 0 or not 1 <= right - left or right > width:
        raise ChartCaptureError('Complete chart bounds exceed the permitted width')
    return Measurement(width, left, top, right - left, bottom - top)


async def settled_image(page, final):
    box = await page.evaluate(FIND_CHART)
    quiet_seconds = MAP_QUIET_SECONDS if box.get('map') else PIXEL_QUIET_SECONDS
    loop = asyncio.get_running_loop()
    deadline = loop.time() + READINESS_TIMEOUT_MS / 1000
    last_request = loop.time()
    pending = set()

    def requested(request):
        nonlocal last_request
        pending.add(request)
        last_request = loop.time()

    def finished(request):
        nonlocal last_request
        pending.discard(request)
        last_request = loop.time()

    page.on('request', requested)
    page.on('requestfinished', finished)
    page.on('requestfailed', finished)
    previous = None
    stable_since = loop.time()
    try:
        while loop.time() < deadline:
            image = await page.screenshot(type='png', animations='disabled', full_page=True,
                                         omit_background=False,
                                         clip={'x': final.x, 'y': final.y,
                                               'width': final.width, 'height': final.height},
                                         timeout=TIMEOUT_MS)
            ready = await page.evaluate(CAPTURE_READY)
            now = loop.time()
            if image != previous or pending or not ready:
                stable_since = now
            elif now - max(stable_since, last_request) >= quiet_seconds:
                return image
            previous = image
            await asyncio.sleep(0.5)
        raise ChartCaptureError(
            f'Chart data, map tiles or pixels did not settle within '
            f'{READINESS_TIMEOUT_MS / 1000:g} seconds')
    finally:
        page.remove_listener('request', requested)
        page.remove_listener('requestfinished', finished)
        page.remove_listener('requestfailed', finished)


async def capture_chart(page, url):
    from PIL import Image, ImageStat

    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    await page.add_init_script(TRACK_MAPS)
    warmed = False

    async def measure(width):
        nonlocal warmed
        # SVG legends can retain fallback-font dimensions on a cold navigation.
        for _ in range(1 if warmed else 2):
            errors.clear()
            result = await measure_chart(page, url, width)
            if errors:
                raise ChartCaptureError('Visualisation script error: ' + '; '.join(dict.fromkeys(errors)))
        warmed = True
        return result

    best = await measure(CAPTURE_WIDTH)
    final = await measure(best.viewport_width)
    if abs(final.height - best.height) > 1 or final.width != best.width:
        raise ChartCaptureError('Chart dimensions changed before capture')
    frame = Measurement(CAPTURE_WIDTH, 0, final.y, CAPTURE_WIDTH, final.height)
    image = await settled_image(page, frame)
    bounds = await page.evaluate(FIND_CHART)
    if bounds.get('error') or any(abs(bounds[key] - getattr(final, key)) > 1
                                  for key in ('x', 'y', 'width', 'height')):
        raise ChartCaptureError('Chart bounds changed during capture')
    if errors:
        raise ChartCaptureError('Visualisation script error: ' + '; '.join(dict.fromkeys(errors)))
    with Image.open(BytesIO(image)) as decoded:
        if decoded.format != 'PNG' or decoded.size != (frame.width, frame.height):
            raise ChartCaptureError('Screenshot dimensions do not match measured container bounds')
        variation = []
        for plot in bounds['plots']:
            rectangle = (math.ceil(plot['x']) - frame.x, math.ceil(plot['y']) - frame.y,
                         math.floor(plot['x'] + plot['width']) - frame.x,
                         math.floor(plot['y'] + plot['height']) - frame.y)
            if rectangle[2] <= rectangle[0] or rectangle[3] <= rectangle[1]:
                raise ChartCaptureError('Rendered plot is too small to validate')
            variation.append(max(ImageStat.Stat(decoded.crop(rectangle).convert('RGB')).stddev))
        if max(variation) < 1:
            raise ChartCaptureError('Rendered chart is blank')
        output = BytesIO()
        decoded.convert('RGB').save(output, format='JPEG', quality=90)
    return output.getvalue(), frame


def write_image(path, image):
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.screenshot-',
                                         suffix='.tmp', delete=False) as output:
            temporary_path = Path(output.name)
            output.write(image)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


async def capture_screenshots(publications, settings, folder=Path('screenshots')):
    summary = CaptureSummary()
    if settings['mode'] == 'off':
        return summary
    folder = Path(folder)
    index = load_index(folder)
    associations = {}
    for publication in publications:
        for url in canonical_charts(publication):
            associations.setdefault(url, set()).add(publication['doc_uri'])
    filenames = migrate_screenshot_filenames(associations, index, folder)
    previous = deepcopy(index)
    selected = select_publications(publications, settings, index, folder)
    for url, entry in index['charts'].items():
        entry['publications'] = sorted(associations.get(url, []))
    urls = list(dict.fromkeys(url for publication in selected for url in canonical_charts(publication)
                             if settings['mode'] != 'new' or needs_capture(url, index, folder)))
    print(f"Screenshots: {settings['mode']}, publications={len(selected)}, charts={len(urls)}", flush=True)
    if urls:
        from playwright.async_api import Error as PlaywrightError, async_playwright

        folder.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                context = await browser.new_context(device_scale_factor=1, color_scheme='light')
                for position, url in enumerate(urls, start=1):
                    now = datetime.now(timezone.utc).isoformat()
                    entry = index['charts'].setdefault(url, {
                        'filename': filenames[url], 'publications': sorted(associations[url]),
                        'last_success': None,
                    })
                    page = await context.new_page()
                    try:
                        image, measurement = await asyncio.wait_for(
                            capture_chart(page, url), timeout=CAPTURE_TIMEOUT_SECONDS)
                    except (ChartCaptureError, PlaywrightError, asyncio.TimeoutError) as error:
                        if not browser.is_connected():
                            raise
                        message = (f'Capture exceeded {CAPTURE_TIMEOUT_SECONDS} seconds'
                                   if isinstance(error, asyncio.TimeoutError) else str(error))
                        entry['last_attempt'] = {'at': now, 'status': 'failed', 'error': message}
                        summary.failures.append({'url': url, 'error': message})
                        print(f'Screenshots FAILED: {url}: {message}', flush=True)
                    else:
                        write_image(folder / entry['filename'], image)
                        entry['last_success'] = {
                            'width': measurement.width, 'height': measurement.height,
                            'viewport_width': measurement.viewport_width,
                            'captured_at': datetime.now(timezone.utc).isoformat(),
                        }
                        entry['last_attempt'] = {'at': now, 'status': 'success'}
                        summary.succeeded += 1
                        print(f'Screenshots: {position}/{len(urls)}: {url}: '
                              f'{measurement.width}x{measurement.height}', flush=True)
                    finally:
                        await page.close()
                    atomic_write_json(folder / 'index.json', index)
            finally:
                await browser.close()
    if not urls and index != previous:
        folder.mkdir(parents=True, exist_ok=True)
        atomic_write_json(folder / 'index.json', index)
    print(f'Screenshots complete: {summary.succeeded} saved, {len(summary.failures)} failed', flush=True)
    for failure in summary.failures:
        print(f"  {failure['url']}: {failure['error']}", flush=True)
    return summary


def finish_screenshots(summary):
    output = os.environ.get('GITHUB_OUTPUT')
    if output:
        with open(output, 'a', encoding='utf-8') as stream:
            stream.write(f'screenshots_completed=true\nscreenshots_failed={len(summary.failures)}\n')
    if summary.failures:
        raise ScreenshotFailures(
            f'{len(summary.failures)} visualisation screenshot(s) failed; successful outputs were saved')
