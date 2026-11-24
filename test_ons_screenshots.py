import ast
import asyncio
from copy import deepcopy
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image
from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

from ons_discovery import validate_config
from ons_screenshots import (
    CaptureSummary, ChartCaptureError, Measurement, ScreenshotFailures,
    canonical_charts, capture_chart, capture_screenshots, choose_size, finish_screenshots,
    legacy_screenshot_filename, load_index, measure_chart, migrate_screenshot_filenames,
    needs_capture, screenshot_filename, select_publications, write_image,
)


DOC = 'https://www.ons.gov.uk/economy/articles/example/2026'
OTHER_DOC = 'https://www.ons.gov.uk/economy/articles/other/2026'
URL = 'https://www.ons.gov.uk/visualisations/example/fig01/index.html'
OTHER_URL = 'https://www.ons.gov.uk/visualisations/example/fig02/index.html'
DVC_URL = 'https://www.ons.gov.uk/visualisations/dvc3682/Fig01/index.html'


def publication(doc=DOC, day='2026-11-01', urls=None):
    return {'doc_uri': doc, 'release_date': day, 'title': 'Example',
            'vis_urls': urls if urls is not None else [URL]}


def settings(mode='new', **kwargs):
    return validate_config({'screenshots': {'mode': mode, **kwargs}})['screenshots']


def successful_entry(url=URL):
    return {'filename': screenshot_filename(url), 'publications': [DOC],
            'last_success': {'width': 700, 'height': 700, 'viewport_width': 700,
                             'captured_at': '2026-11-01T00:00:00+00:00', 'target_reached': True},
            'last_attempt': {'at': '2026-11-01T00:00:00+00:00', 'status': 'success'}}


def jpeg():
    image = BytesIO()
    Image.new('RGB', (700, 700), 'blue').save(image, format='JPEG')
    return image.getvalue()


def chart_html(height='700', width='innerWidth'):
    return f"""<!doctype html><style>
        body {{margin:0}} svg {{display:block}}
        #legend,#source {{height:30px;background:yellow}}
        </style><div id="legend">Separate legend</div><div id="graphic"></div>
        <div id="source">Separate source</div>
        <script>
        const w={width}, h={height};
        document.querySelector('#graphic').innerHTML = `<svg width="${{w}}" height="${{h}}">
        <rect width="100%" height="100%" fill="white"/>
        <rect x="10" y="10" width="${{w-20}}" height="${{h-20}}" fill="steelblue"/>
        <path d="M1 0V${{h}}H${{w}}" stroke="black" fill="none"/>
        </svg>`;
        </script>"""


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.index = {'version': 1, 'charts': {}}

    def test_defaults_and_validation(self):
        config = validate_config({})
        self.assertEqual(config['discovery'], {
            'mode': 'new', 'limit': None, 'url': None, 'start_date': None})
        self.assertEqual(config['catalogue'], {'mode': 'new', 'limit': None})
        self.assertEqual(config['screenshots'], {
            'mode': 'off', 'limit': None, 'url': None, 'start_date': None})
        for value in (None, [], {'mode': 'unknown'}, {'limit': True}, {'limit': 0},
                      {'limit': '2'}, {'extra': 1}, {'mode': 'recent'},
                      {'mode': 'url', 'url': URL},
                      {'mode': 'url', 'url': 'https://example.com/articles/test'},
                      {'mode': 'datestart', 'start_date': '2026-02-30'},
                      {'mode': 'datestart', 'start_date': '20261101'}):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                validate_config({'screenshots': value})

    def test_modes_dates_and_publication_limits(self):
        records = [publication(OTHER_DOC, '2026-10-01', [OTHER_URL]),
                   publication(urls=[URL, OTHER_URL, '/visualisations/test/fallback.png'])]
        for mode, extras, expected in (
                ('off', {}, []),
                ('new', {'limit': 1}, [DOC]),
                ('recent', {'limit': 1}, [DOC]),
                ('all', {}, [DOC, OTHER_DOC]),
                ('datestart', {'start_date': '2026-10-01'}, [DOC]),
                ('url', {'url': OTHER_DOC}, [OTHER_DOC])):
            with self.subTest(mode=mode):
                result = select_publications(records, settings(mode, **extras), self.index, self.folder)
                self.assertEqual([item['doc_uri'] for item in result], expected)
        self.assertEqual(canonical_charts(records[1]), [URL, OTHER_URL])
        with self.assertRaisesRegex(ValueError, 'not cached'):
            select_publications([], settings('url', url=DOC), self.index, self.folder)

    def test_missing_images_and_failed_refresh_are_pending(self):
        self.index['charts'][URL] = successful_entry()
        self.assertTrue(needs_capture(URL, self.index, self.folder))
        (self.folder / screenshot_filename(URL)).write_bytes(jpeg())
        self.assertFalse(needs_capture(URL, self.index, self.folder))
        self.index['charts'][URL]['last_attempt'] = {
            'at': '2026-11-02', 'status': 'failed', 'error': 'Timeout'}
        self.assertTrue(needs_capture(URL, self.index, self.folder))

    def test_simple_filenames_and_url_collision_fallback(self):
        owners = {}
        names = []
        for url in (DVC_URL, DVC_URL + '?view=2', DVC_URL + '#view', DVC_URL.lower()):
            filename = screenshot_filename(url, owners)
            owners[filename] = url
            names.append(filename)
            self.assertEqual(screenshot_filename(url, owners), filename)
        self.assertEqual(names[0], 'dvc3682-fig01.jpg')
        self.assertEqual(len(set(names)), 4)
        for filename in names[1:]:
            self.assertRegex(filename, r'^dvc3682-fig01-[0-9a-f]{8}\.jpg$')
        self.assertEqual(screenshot_filename(DVC_URL.replace('Fig01/index.html', 'figure1.html')),
                         'dvc3682-fig01.jpg')
        self.assertEqual(screenshot_filename('https://www.ons.gov.uk/visualisations/CON/index.html'),
                         'visualisation-con.jpg')

    def test_hash_collision_uses_an_additional_counter(self):
        owners = {}
        names = []
        with patch('ons_screenshots.hashlib.sha256') as digest:
            digest.return_value.hexdigest.return_value = 'a' * 64
            for url in (DVC_URL, DVC_URL + '?view=2', DVC_URL + '#view'):
                filename = screenshot_filename(url, owners)
                owners[filename] = url
                names.append(filename)
        self.assertEqual(names, ['dvc3682-fig01.jpg', 'dvc3682-fig01-aaaaaaaa.jpg',
                                 'dvc3682-fig01-aaaaaaaa-2.jpg'])

    def test_invalid_index(self):
        for entry in ({'filename': '../outside.jpg'}, successful_entry()):
            index = {'version': 1, 'charts': {URL: deepcopy(entry)}}
            if entry.get('last_success'):
                index['charts'][URL]['last_success']['width'] = 701
            (self.folder / 'index.json').write_text(json.dumps(index), encoding='utf-8')
            with self.assertRaises(ValueError):
                load_index(self.folder)

    def test_duplicate_filename_in_index_is_rejected(self):
        index = {'version': 1, 'charts': {
            DVC_URL: successful_entry(DVC_URL),
            DVC_URL + '?view=2': successful_entry(DVC_URL + '?view=2'),
        }}
        (self.folder / 'index.json').write_text(json.dumps(index), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Duplicate screenshot filename'):
            load_index(self.folder)

    def test_legacy_images_and_index_are_migrated_without_changing_content(self):
        orphan = DVC_URL.replace('Fig01', 'Fig02')
        entry = successful_entry(DVC_URL)
        entry['filename'] = legacy_screenshot_filename(DVC_URL)
        index = {'version': 1, 'charts': {DVC_URL: entry}}
        (self.folder / 'index.json').write_text(json.dumps(index), encoding='utf-8')
        (self.folder / entry['filename']).write_bytes(b'indexed image')
        (self.folder / legacy_screenshot_filename(orphan)).write_bytes(b'unindexed image')
        associations = {DVC_URL: {DOC}, orphan: {DOC, OTHER_DOC}}
        assigned = migrate_screenshot_filenames(associations, index, self.folder)
        self.assertEqual((self.folder / assigned[DVC_URL]).read_bytes(), b'indexed image')
        self.assertEqual((self.folder / assigned[orphan]).read_bytes(), b'unindexed image')
        self.assertEqual(index['charts'][DVC_URL]['last_success'], successful_entry(DVC_URL)['last_success'])
        self.assertEqual(index['charts'][orphan]['publications'], sorted([DOC, OTHER_DOC]))
        self.assertIsNone(index['charts'][orphan]['last_success'])
        self.assertTrue(needs_capture(orphan, index, self.folder))
        self.assertEqual(load_index(self.folder), index)
        self.assertFalse((self.folder / legacy_screenshot_filename(DVC_URL)).exists())
        with patch('ons_screenshots.atomic_write_json', side_effect=AssertionError('unexpected rewrite')):
            self.assertEqual(migrate_screenshot_filenames(associations, index, self.folder), assigned)

    def test_migration_does_not_overwrite_an_unknown_existing_filename(self):
        entry = successful_entry(DVC_URL)
        entry['filename'] = legacy_screenshot_filename(DVC_URL)
        index = {'version': 1, 'charts': {DVC_URL: entry}}
        (self.folder / 'index.json').write_text(json.dumps(index), encoding='utf-8')
        (self.folder / entry['filename']).write_bytes(b'actual image')
        occupied = self.folder / 'DVC3682-FIG01.JPG'
        occupied.write_bytes(b'unknown image')
        assigned = migrate_screenshot_filenames({DVC_URL: {DOC}}, index, self.folder)
        self.assertRegex(assigned[DVC_URL], r'^dvc3682-fig01-[0-9a-f]{8}\.jpg$')
        self.assertEqual(occupied.read_bytes(), b'unknown image')
        self.assertEqual((self.folder / assigned[DVC_URL]).read_bytes(), b'actual image')
        self.assertEqual(load_index(self.folder), index)

    def test_migration_restores_old_filenames_if_index_write_fails(self):
        entry = successful_entry(DVC_URL)
        entry['filename'] = legacy_screenshot_filename(DVC_URL)
        index = {'version': 1, 'charts': {DVC_URL: entry}}
        original = deepcopy(index)
        (self.folder / 'index.json').write_text(json.dumps(index), encoding='utf-8')
        source = self.folder / entry['filename']
        source.write_bytes(b'original')
        with patch('ons_screenshots.atomic_write_json', side_effect=OSError('disk error')):
            with self.assertRaisesRegex(OSError, 'disk error'):
                migrate_screenshot_filenames({DVC_URL: {DOC}}, index, self.folder)
        self.assertEqual(source.read_bytes(), b'original')
        self.assertEqual(index, original)
        self.assertEqual(load_index(self.folder), original)
        self.assertFalse((self.folder / screenshot_filename(DVC_URL)).exists())

    def test_write_failure_cleans_only_temporary_image_and_preserves_old(self):
        path = self.folder / 'old.jpg'
        path.write_bytes(b'old')
        with patch('ons_screenshots.os.replace', side_effect=OSError('disk error')):
            with self.assertRaisesRegex(OSError, 'disk error'):
                write_image(path, b'new')
        self.assertEqual(path.read_bytes(), b'old')
        self.assertEqual(list(self.folder.iterdir()), [path])

    def test_failure_status_is_written_before_exception(self):
        output = self.folder / 'workflow-output'
        with patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}):
            with self.assertRaises(ScreenshotFailures):
                finish_screenshots(CaptureSummary(1, [{'url': URL, 'error': 'timeout'}]))
        self.assertIn('screenshots_completed=true\nscreenshots_failed=1\n',
                      output.read_text(encoding='utf-8'))
        with patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}):
            finish_screenshots(CaptureSummary())
        self.assertTrue(output.read_text(encoding='utf-8').endswith('screenshots_failed=0\n'))


class SizingTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_initial_height_does_not_adjust_width(self):
        measure = AsyncMock(return_value=Measurement(700, 0, 0, 700, 700))
        result = await choose_size(measure)
        self.assertEqual(result.viewport_width, 700)
        measure.assert_awaited_once_with(700)

    async def test_achievable_impossible_and_breakpoint_heights(self):
        for formula, expected_width, expected_height in (
                (lambda width: width + 200, 500, 700),
                (lambda width: width // 2, 700, 350),
                (lambda width: 900 if width > 576 else 700, 576, 700)):
            measured = []

            async def measure(width):
                measured.append(width)
                return Measurement(width, 0, 0, width, formula(width))

            result = await choose_size(measure)
            self.assertEqual((result.viewport_width, result.height), (expected_width, expected_height))
            self.assertTrue(all(320 <= width <= 700 for width in measured))
            self.assertLessEqual(len(measured), 40)

    async def test_overflow_is_not_cropped(self):
        async def overflow(width):
            raise ChartCaptureError('overflow')
        with self.assertRaisesRegex(ChartCaptureError, 'No complete chart'):
            await choose_size(overflow)


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch()
        self.page = await self.browser.new_page(device_scale_factor=1)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def route(self, html):
        await self.page.route('https://www.ons.gov.uk/**',
                              lambda route: route.fulfill(content_type='text/html', body=html))

    async def test_body_fallback_includes_legend_chart_and_source(self):
        await self.route(chart_html('640'))
        image, measured = await capture_chart(self.page, URL)
        with Image.open(BytesIO(image)) as decoded:
            self.assertEqual(decoded.format, 'JPEG')
            self.assertEqual(decoded.size, (700, 700))
            self.assertLessEqual(decoded.width, 700)
            red, green, blue = decoded.getpixel((350, 350))
            self.assertGreater(blue, red)
            self.assertGreater(decoded.getpixel((350, 15))[0], 200)
            self.assertGreater(decoded.getpixel((350, 685))[0], 200)
            self.assertLess(decoded.getpixel((350, 15))[2], 80)
            self.assertLess(decoded.getpixel((350, 685))[2], 80)
        self.assertEqual(measured.y, 0)
        self.assertTrue(measured.target_reached)

    async def test_reload_required_and_delayed_render(self):
        html = chart_html('innerWidth + 200').replace(
            "document.querySelector('#graphic').innerHTML =", "setTimeout(() => {"
            "document.querySelector('#graphic').innerHTML =")
        html = html.replace('</script>', """
            document.querySelector('#graphic svg').innerHTML +=
                '<rect x="50" y="50" width="100" height="100" fill="red"/>';
            }, 100);</script>""")
        await self.route(html)
        measured = await measure_chart(self.page, URL, 500)
        self.assertEqual((measured.width, measured.height), (500, 760))
        measured = await measure_chart(self.page, URL, 320)
        self.assertEqual((measured.width, measured.height), (320, 580))

    async def test_map_canvas_waits_for_ready_state(self):
        await self.route("""<style>body{margin:0}canvas{display:block}</style>
            <div id="graphic" style="position:relative">
            <canvas class="maplibregl-canvas" width="700" height="700"></canvas>
            <button style="position:absolute;top:80px;left:80px;background:red">Zoom</button></div>
            <script>
            let ready=false;
            globalThis.map={loaded:()=>ready,areTilesLoaded:()=>ready};
            setTimeout(()=>{
                const c=document.querySelector('canvas').getContext('2d');
                c.fillStyle='steelblue';c.fillRect(0,0,700,700);
                c.fillStyle='green';c.fillRect(50,50,400,400);ready=true;
            },800);
            </script>""")
        baseline = await measure_chart(self.page, URL, 700)
        with patch('ons_screenshots.choose_size', AsyncMock(return_value=baseline)):
            image, measured = await capture_chart(self.page, URL)
        self.assertEqual(measured.width, 700)
        self.assertGreaterEqual(measured.height, 700)
        with Image.open(BytesIO(image)) as decoded:
            self.assertGreater(decoded.getpixel((100, 100))[1], 70)
            control = decoded.crop((80, 80, 125, 103))
            self.assertTrue(any(red > 150 and green < 80 and blue < 80
                                for red, green, blue in control.get_flattened_data()))
        self.assertEqual(await self.page.locator('button').evaluate(
            '(element) => getComputedStyle(element).visibility'), 'visible')

    async def test_wrapper_includes_multiple_charts_controls_and_notes(self):
        await self.route("""<style>body{margin:0}svg{display:block}</style>
            <div id="visualisation"><div id="legend" style="height:30px">Legend</div>
            <div id="graphic"><svg width="700" height="350">
            <rect width="700" height="350" fill="red" stroke="black" stroke-width="2"/></svg></div>
            <div id="chart">
            <svg width="700" height="350"><rect width="700" height="350" fill="blue"
            stroke="black" stroke-width="2"/></svg></div>
            <div id="controls" style="height:30px"><button>Choose dataset</button></div>
            <div id="notes" style="height:40px">Notes and source</div></div>
            <p>Outside the visualisation wrapper</p>""")
        with patch('ons_screenshots.choose_size',
                   AsyncMock(return_value=Measurement(700, 0, 0, 700, 800))):
            image, measured = await capture_chart(self.page, URL)
        self.assertEqual((measured.width, measured.height), (700, 800))
        with Image.open(BytesIO(image)) as decoded:
            self.assertGreater(decoded.getpixel((100, 100))[0], 200)
            self.assertGreater(decoded.getpixel((100, 600))[2], 200)

    async def test_div_parent_preserves_padding_and_contents_outside_svg(self):
        await self.route("""<style>body{margin:0}svg{display:block}</style>
            <div id="visualisation" style="padding:20px;box-sizing:border-box">
            <div style="height:30px;background:yellow">Legend</div>
            <svg id="graphic" width="660" height="600">
            <rect width="660" height="600" fill="steelblue" stroke="black" stroke-width="2"/></svg>
            <div style="height:30px"><button>Interactive control</button></div></div>""")
        image, measured = await capture_chart(self.page, URL)
        self.assertEqual((measured.width, measured.height), (700, 700))
        with Image.open(BytesIO(image)) as decoded:
            self.assertEqual(decoded.getpixel((5, 5)), (255, 255, 255))
            self.assertLess(decoded.getpixel((100, 30))[2], 80)
            self.assertGreater(decoded.getpixel((100, 100))[2], 100)

    async def test_cold_legend_metrics_are_warmed_before_comparing_layouts(self):
        html = chart_html('640').replace('</script>', """
            const cold = localStorage.getItem('legend-warmed') === null;
            localStorage.setItem('legend-warmed', 'true');
            document.querySelector('#legend').style.height = cold ? '33px' : '36px';
            </script>""")
        await self.route(html)

        async def initial_size(measure):
            return await measure(700)

        with patch('ons_screenshots.choose_size', initial_size):
            image, measured = await capture_chart(self.page, URL)
        self.assertEqual((measured.width, measured.height), (700, 706))
        with Image.open(BytesIO(image)) as decoded:
            self.assertEqual(decoded.size, (700, 706))

    async def test_blank_chart_is_not_saved_as_success(self):
        html = chart_html('640').replace('fill="steelblue"', 'fill="white"').replace(
            'stroke="black"', 'stroke="white"')
        await self.route(html)
        with self.assertRaisesRegex(ChartCaptureError, 'blank'):
            await capture_chart(self.page, URL)

    async def test_tall_chart_is_not_cropped_to_browser_viewport(self):
        await self.route(chart_html('1200'))
        with patch('ons_screenshots.choose_size',
                   AsyncMock(return_value=Measurement(700, 0, 0, 700, 1260))):
            image, measured = await capture_chart(self.page, URL)
        self.assertEqual(measured.height, 1260)
        with Image.open(BytesIO(image)) as decoded:
            self.assertEqual(decoded.size, (700, 1260))
            self.assertGreater(decoded.getpixel((100, 1150))[2], 100)

    async def test_overflow_and_failed_render_are_explicit(self):
        with patch('ons_screenshots.TIMEOUT_MS', 1000):
            for html in (
                    chart_html(width='800'),
                    '<div id="graphic"></div>'):
                with self.subTest(html=html[:40]):
                    await self.page.unroute_all()
                    await self.route(html)
                    with self.assertRaises((ChartCaptureError, PlaywrightTimeoutError)) as caught:
                        await measure_chart(self.page, URL, 700)
                    self.assertRegex(str(caught.exception), 'overflow|exceed|Timeout')


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)

    async def test_off_does_not_read_or_create_outputs(self):
        with patch('ons_screenshots.load_index', side_effect=AssertionError('unexpected read')):
            result = await capture_screenshots([], settings('off'), self.folder / 'absent')
        self.assertEqual(result.succeeded, 0)
        self.assertFalse((self.folder / 'absent').exists())

    async def test_partial_results_shared_urls_and_new_retry(self):
        index = {'version': 1, 'charts': {URL: successful_entry()}}
        (self.folder / 'index.json').write_text(json.dumps(index), encoding='utf-8')
        old_image = self.folder / screenshot_filename(URL)
        old_image.write_bytes(b'previous image')
        records = [publication(urls=[URL, OTHER_URL]),
                   publication(OTHER_DOC, '2026-10-01', [URL])]
        captured = AsyncMock(side_effect=[
            ChartCaptureError('broken chart'), (jpeg(), Measurement(700, 0, 0, 700, 700))])
        with patch('ons_screenshots.capture_chart', captured):
            summary = await capture_screenshots(records, settings('all', limit=1), self.folder)
        self.assertEqual(summary.succeeded, 1)
        self.assertEqual(len(summary.failures), 1)
        self.assertEqual(captured.await_count, 2)
        self.assertEqual(old_image.read_bytes(), b'previous image')
        index = load_index(self.folder)
        self.assertEqual(index['charts'][URL]['publications'], sorted([DOC, OTHER_DOC]))
        self.assertEqual(index['charts'][URL]['last_success'], successful_entry()['last_success'])
        self.assertEqual(index['charts'][URL]['last_attempt']['status'], 'failed')
        self.assertTrue((self.folder / screenshot_filename(OTHER_URL)).is_file())
        with patch('ons_screenshots.capture_chart',
                   AsyncMock(return_value=(jpeg(), Measurement(700, 0, 0, 700, 700)))) as retry:
            await capture_screenshots(records, settings('new'), self.folder)
        self.assertEqual(retry.await_count, 1)
        self.assertEqual(retry.await_args.args[1], URL)
        self.assertEqual(load_index(self.folder)['charts'][URL]['last_attempt']['status'], 'success')

    async def test_disk_failures_are_fatal_not_partial_chart_failures(self):
        with patch('ons_screenshots.capture_chart',
                   AsyncMock(return_value=(jpeg(), Measurement(700, 0, 0, 700, 700)))):
            with patch('ons_screenshots.write_image', side_effect=OSError('disk full')):
                with self.assertRaisesRegex(OSError, 'disk full'):
                    await capture_screenshots([publication()], settings('all'), self.folder)
        self.assertFalse((self.folder / 'index.json').exists())

    async def test_url_variants_keep_separate_images_and_stable_index_names(self):
        variant = DVC_URL + '?view=2'
        captured = AsyncMock(return_value=(jpeg(), Measurement(700, 0, 0, 700, 700)))
        with patch('ons_screenshots.capture_chart', captured):
            await capture_screenshots([publication(urls=[DVC_URL, variant])], settings('all'), self.folder)
        index = load_index(self.folder)
        first = index['charts'][DVC_URL]['filename']
        second = index['charts'][variant]['filename']
        self.assertEqual(first, 'dvc3682-fig01.jpg')
        self.assertNotEqual(first, second)
        self.assertTrue((self.folder / first).is_file())
        self.assertTrue((self.folder / second).is_file())
        with patch('ons_screenshots.capture_chart', side_effect=AssertionError('unexpected capture')):
            await capture_screenshots([publication(urls=[variant, DVC_URL])], settings('new'), self.folder)
        self.assertEqual(load_index(self.folder), index)

    async def test_completed_capture_ownership_is_saved_before_next_chart(self):
        captured = AsyncMock(side_effect=[
            (jpeg(), Measurement(700, 0, 0, 700, 700)), RuntimeError('interrupted')])
        with patch('ons_screenshots.capture_chart', captured):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                await capture_screenshots([publication(urls=[DVC_URL, OTHER_URL])],
                                          settings('all'), self.folder)
        index = load_index(self.folder)
        self.assertEqual(index['charts'][DVC_URL]['filename'], 'dvc3682-fig01.jpg')
        self.assertEqual(index['charts'][DVC_URL]['last_attempt']['status'], 'success')
        self.assertNotIn(OTHER_URL, index['charts'])
        self.assertTrue((self.folder / 'dvc3682-fig01.jpg').is_file())

    async def test_capture_timeout_is_reported_and_left_pending(self):
        async def stalled(page, url):
            await asyncio.sleep(60)

        with patch('ons_screenshots.capture_chart', stalled):
            with patch('ons_screenshots.CAPTURE_TIMEOUT_SECONDS', 0.01):
                summary = await capture_screenshots([publication()], settings('all'), self.folder)
        self.assertEqual(len(summary.failures), 1)
        self.assertIn('exceeded', summary.failures[0]['error'])
        index = load_index(self.folder)
        self.assertTrue(needs_capture(URL, index, self.folder))
        self.assertIsNone(index['charts'][URL]['last_success'])

    async def test_notebook_screenshots_only_and_all_off_preserve_other_outputs(self):
        notebook = json.loads(Path('catalogue.ipynb').read_text(encoding='utf-8'))
        for cell in notebook['cells']:
            if cell['cell_type'] == 'code':
                compile(''.join(cell['source']), '<notebook>', 'exec',
                        flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        final_cell = ''.join(notebook['cells'][-1]['source'])
        records = [publication()]
        for mode, failed in (('off', False), ('all', False), ('all', True)):
            config = validate_config({
                'discovery': {'mode': 'off'}, 'catalogue': {'mode': 'off'},
                'screenshots': {'mode': mode}})
            writes = Mock()
            client = SimpleNamespace(session=Mock())
            capture = AsyncMock(return_value=CaptureSummary(
                1, [{'url': URL, 'error': 'timeout'}] if failed else []))
            namespace = {
                'pages': None, 'config': config, 'cached_publications': records, 'seed': records,
                'next_state': None, 'state': None, 'client': client, 'write_json_files': writes,
                'capture_screenshots': capture, 'finish_screenshots': finish_screenshots,
            }
            code = compile(final_cell, '<notebook>', 'exec', flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
            with patch.dict(os.environ, {'GITHUB_OUTPUT': ''}):
                if failed:
                    with self.assertRaises(ScreenshotFailures):
                        await eval(code, namespace)
                else:
                    await eval(code, namespace)
            writes.assert_called_once_with([])
            capture.assert_awaited_once_with(records, config['screenshots'])
            client.session.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
