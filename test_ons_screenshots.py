import asyncio
from copy import deepcopy
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image
from playwright.async_api import Error as PlaywrightError, async_playwright

import catalogue
from ons_discovery import validate_config
from ons_screenshots import (
    CaptureSummary, ChartCaptureError, Measurement,
    canonical_charts, capture_chart, capture_screenshots, finish_screenshots,
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


def scoped_map_html(delay):
    return f"""<!doctype html><style>body{{margin:0}}canvas{{display:block}}</style>
        <div id="graphic"><canvas class="maplibregl-canvas"
            width="700" height="700"></canvas></div>
        <script>
        window.maplibregl = {{Map: class {{
            constructor() {{
                this.ready = false;
                const context = document.querySelector('canvas').getContext('2d');
                context.fillStyle = 'steelblue';
                context.fillRect(0, 0, 700, 700);
                context.fillStyle = 'grey';
                context.fillRect(500, 500, 100, 100);
                setTimeout(() => {{
                    context.fillStyle = 'green';
                    context.fillRect(50, 50, 400, 400);
                    this.ready = true;
                }}, {delay});
            }}
            loaded() {{ return this.ready; }}
            areTilesLoaded() {{ return this.ready; }}
        }}}};
        </script>
        <script type="module">const state = {{map: new maplibregl.Map()}};</script>"""


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

    def test_index_accepts_fixed_width_and_historical_height_target_metadata(self):
        for width, legacy in ((380, True), (700, False)):
            with self.subTest(legacy=legacy):
                entry = successful_entry()
                entry['last_success'].update(width=width, viewport_width=width, height=1260)
                if not legacy:
                    entry['last_success'].pop('target_reached')
                index = {'version': 1, 'charts': {URL: entry}}
                (self.folder / 'index.json').write_text(json.dumps(index), encoding='utf-8')
                self.assertEqual(load_index(self.folder), index)

    def test_chart_failures_publish_counts_and_escaped_warnings_without_raising(self):
        output = self.folder / 'workflow-output'
        with patch.dict(os.environ, {'GITHUB_OUTPUT': str(output), 'GITHUB_ACTIONS': 'true'}), \
                patch('sys.stdout', new_callable=StringIO) as log:
            finish_screenshots(CaptureSummary(1, [{'url': URL, 'error': 'timeout 50%\nretry\rnow'}]))
        self.assertIn('screenshots_completed=true\nscreenshots_failed=1\n',
                      output.read_text(encoding='utf-8'))
        self.assertIn(f'::warning::{URL}: timeout 50%25%0Aretry%0Dnow', log.getvalue())
        with patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}):
            finish_screenshots(CaptureSummary())
        self.assertTrue(output.read_text(encoding='utf-8').endswith('screenshots_failed=0\n'))


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
        self.assertEqual(measured.viewport_width, 700)

    async def test_fixed_width_keeps_short_and_responsive_charts_at_native_height(self):
        widths = []
        self.page.on('domcontentloaded', lambda: widths.append(self.page.viewport_size['width']))
        for formula, height in (('240', 300), ('innerWidth + 200', 960)):
            with self.subTest(height=height):
                await self.page.unroute_all()
                await self.route(chart_html(formula))
                image, measured = await capture_chart(self.page, URL)
                self.assertEqual((measured.width, measured.height, measured.viewport_width), (700, height, 700))
                with Image.open(BytesIO(image)) as decoded:
                    self.assertEqual(decoded.size, (700, height))
        self.assertEqual(set(widths), {700})

    async def test_narrow_chart_keeps_native_size_in_700px_wide_image(self):
        await self.route("""<style>body{margin:0}svg{display:block}</style>
            <div style="width:400px;margin-left:100px"><div id="graphic">
            <svg width="400" height="1200"><rect width="400" height="1200" fill="steelblue"/>
            <path d="M0 0V1200H400" stroke="black" fill="none"/></svg></div></div>""")
        image, measured = await capture_chart(self.page, URL)
        self.assertEqual((measured.width, measured.height, measured.viewport_width), (700, 1200, 700))
        with Image.open(BytesIO(image)) as decoded:
            self.assertEqual(decoded.size, (700, 1200))
            self.assertGreater(decoded.getpixel((20, 1100))[0], 240)
            self.assertGreater(decoded.getpixel((200, 1100))[2], 100)

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

    async def test_module_scoped_map_waits_for_data_not_just_basemap_pixels(self):
        await self.route(scoped_map_html(1800))
        image, _ = await capture_chart(self.page, URL)
        with Image.open(BytesIO(image)) as decoded:
            red, green, blue = decoded.getpixel((100, 100))
            self.assertLess(red, 30)
            self.assertGreater(green, 100)
            self.assertLess(blue, 30)
        self.assertTrue(await self.page.evaluate('globalThis.map === undefined'))

    async def test_delayed_data_request_resets_pixel_and_network_quiet_period(self):
        html = chart_html('640').replace('</script>', """
            setTimeout(async () => {
                await fetch('./chart-data.json');
                document.querySelector('#graphic rect:nth-child(2)')
                    .setAttribute('fill', 'green');
            }, 1200);
            </script>""")
        await self.route(html)

        async def data(route):
            await asyncio.sleep(2.5)
            await route.fulfill(content_type='application/json', body='{}')

        await self.page.route('**/chart-data.json', data)
        image, _ = await capture_chart(self.page, URL)
        with Image.open(BytesIO(image)) as decoded:
            self.assertLess(decoded.getpixel((100, 100))[2], 30)

    async def test_unready_scoped_map_times_out_instead_of_saving_basemap(self):
        await self.route(scoped_map_html(60_000))
        with patch('ons_screenshots.READINESS_TIMEOUT_MS', 1000):
            with self.assertRaisesRegex(ChartCaptureError, 'Map/frame readiness timed out'):
                await capture_chart(self.page, URL)

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
        image, measured = await capture_chart(self.page, URL)
        self.assertEqual(measured.height, 1260)
        with Image.open(BytesIO(image)) as decoded:
            self.assertEqual(decoded.size, (700, 1260))
            self.assertGreater(decoded.getpixel((100, 1150))[2], 100)

    async def test_fractional_container_at_document_edge_keeps_complete_bottom(self):
        await self.route("""<style>body{margin:0}svg{display:block}</style>
            <div style="height:10px"></div>
            <div style="height:1160.28125px;background:lime">
            <div id="graphic"><svg width="700" height="1158">
            <rect width="700" height="1158" fill="steelblue"/>
            <rect y="1150" width="700" height="8" fill="red"/></svg></div></div>""")
        image, measured = await capture_chart(self.page, URL)
        self.assertEqual((measured.y, measured.width, measured.height), (10, 700, 1161))
        with Image.open(BytesIO(image)) as decoded:
            self.assertEqual(decoded.size, (700, 1161))
            self.assertGreater(decoded.getpixel((350, 1154))[0], 200)
            self.assertGreater(decoded.getpixel((350, 1159))[1], 150)

    async def test_overflowing_flex_panels_wrap_without_scaling_or_hiding(self):
        await self.route("""<style>body{margin:0}svg{display:block}</style>
            <div><div id="graphic"><div style="display:flex;gap:20px">
            <div><svg width="350" height="200"><rect width="350" height="200" fill="red"
            stroke="black" stroke-width="2"/>
            </svg></div><div><svg width="350" height="200">
            <rect width="350" height="200" fill="blue" stroke="black" stroke-width="2"/>
            </svg></div></div></div>
            <div id="notes" style="height:30px;background:lime">Notes</div></div>""")
        image, measured = await capture_chart(self.page, URL)
        self.assertEqual((measured.width, measured.height), (700, 450))
        self.assertEqual(await self.page.locator('svg').evaluate_all(
            '(elements) => elements.map(e => e.getBoundingClientRect().width)'), [350, 350])
        with Image.open(BytesIO(image)) as decoded:
            self.assertGreater(decoded.getpixel((100, 100))[0], 200)
            self.assertGreater(decoded.getpixel((100, 300))[2], 200)
            self.assertGreater(decoded.getpixel((100, 435))[1], 150)

    async def test_nested_iframe_dashboard_includes_delayed_plots_and_surroundings(self):
        documents = {
            URL: """<style>body{margin:0}iframe{display:block;border:0;width:700px;height:240px}</style>
                <div><div id="legend" style="height:30px;background:yellow">Legend</div>
                <iframe src="./nested.html"></iframe>
                <div id="source" style="height:30px;background:lime">Source</div></div>
                <iframe style="display:none" src="https://example.com/tracker"></iframe>""",
            URL.replace('index.html', 'nested.html'):
                """<style>body{margin:0}iframe{display:block;border:0;width:700px;height:240px}</style>
                <iframe src="./plot.html"></iframe>""",
            URL.replace('index.html', 'plot.html'):
                """<style>body{margin:16px 0 0}svg{display:block}</style><div id="graphic">
                <svg width="700" height="240"><rect width="700" height="240" fill="steelblue"
                stroke="black" stroke-width="2"/>
                </svg></div><script>setTimeout(()=>{
                document.querySelector('rect').setAttribute('fill','green')},900)</script>""",
        }
        await self.page.route('https://www.ons.gov.uk/**', lambda route: route.fulfill(
            content_type='text/html', body=documents[route.request.url]))
        image, measured = await capture_chart(self.page, URL)
        self.assertEqual((measured.width, measured.height), (700, 316))
        with Image.open(BytesIO(image)) as decoded:
            self.assertLess(decoded.getpixel((100, 15))[2], 60)
            self.assertLess(decoded.getpixel((100, 120))[2], 60)
            self.assertGreater(decoded.getpixel((100, 301))[1], 150)

    async def test_visible_body_overflow_within_viewport_is_captured(self):
        await self.route("""<!doctype html><style>body{margin:0;width:690px}svg{display:block}</style>
            <div id="graphic"><svg width="695" height="240">
            <rect width="695" height="240" fill="steelblue"/>
            <rect x="690" width="5" height="240" fill="red"/></svg></div>""")
        image, measured = await capture_chart(self.page, URL)
        self.assertEqual((measured.width, measured.height), (700, 240))
        with Image.open(BytesIO(image)) as decoded:
            self.assertGreater(decoded.getpixel((692, 120))[0], 150)

    async def test_visible_external_frame_is_rejected(self):
        await self.page.route('https://example.com/**', lambda route: route.fulfill(
            content_type='text/html', body=chart_html('240')))
        await self.route('<iframe src="https://example.com/chart"></iframe>')
        with self.assertRaisesRegex(ChartCaptureError, 'Unsupported or inaccessible.*example.com'):
            await capture_chart(self.page, URL)

    async def test_plot_labels_outside_viewport_are_not_silently_clipped(self):
        await self.route("""<!doctype html><style>body{margin:0}svg{display:block}</style>
            <div id="graphic"><svg width="700" height="240">
            <rect width="700" height="240" fill="steelblue"/>
            <text x="20" y="40" text-anchor="end">Long country name</text></svg></div>""")
        with self.assertRaisesRegex(ChartCaptureError, 'Visible plot labels exceed'):
            await capture_chart(self.page, URL)

    async def test_fully_offscreen_axis_ticks_do_not_block_capture(self):
        html = chart_html('240').replace('</svg>',
            '<text x="-200" y="40" text-anchor="end">Old date tick</text></svg>')
        await self.route(html)
        await capture_chart(self.page, URL)

    async def test_navigation_network_error_is_an_explicit_chart_failure(self):
        await self.page.route('https://www.ons.gov.uk/**', lambda route: route.abort('failed'))
        with self.assertRaisesRegex(ChartCaptureError, 'Navigation failed:.*net::'):
            await capture_chart(self.page, URL)

    async def test_blank_iframe_plot_is_not_masked_by_successful_sibling(self):
        documents = {
            URL: """<!doctype html><style>
                body{margin:0}iframe{display:block;border:0;width:700px;height:300px}</style>
                <iframe src="./good.html"></iframe><iframe src="./blank.html"></iframe>""",
            URL.replace('index.html', 'good.html'): chart_html('240'),
            URL.replace('index.html', 'blank.html'): chart_html('240').replace(
                'fill="steelblue"', 'fill="white"').replace('stroke="black"', 'stroke="white"'),
        }
        await self.page.route('https://www.ons.gov.uk/**', lambda route: route.fulfill(
            content_type='text/html', body=documents[route.request.url]))
        with self.assertRaisesRegex(ChartCaptureError, 'blank plot'):
            await capture_chart(self.page, URL)

    async def test_overflow_and_failed_render_are_explicit(self):
        with patch('ons_screenshots.READINESS_TIMEOUT_MS', 1000):
            for html in (
                    chart_html(width='800'),
                    '<div id="graphic"></div>'):
                with self.subTest(html=html[:40]):
                    await self.page.unroute_all()
                    await self.route(html)
                    with self.assertRaises(ChartCaptureError) as caught:
                        await measure_chart(self.page, URL, 700)
                    self.assertRegex(str(caught.exception), 'overflow|exceed|Plot discovery timed out')


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
        self.assertNotIn('target_reached', index['charts'][OTHER_URL]['last_success'])
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

    async def test_browser_startup_and_disconnection_remain_fatal(self):
        with patch('playwright.async_api.BrowserType.launch',
                   AsyncMock(side_effect=PlaywrightError('browser startup failed'))):
            with self.assertRaisesRegex(PlaywrightError, 'browser startup failed'):
                await capture_screenshots([publication()], settings('all'), self.folder)

        async def disconnected(page, url):
            await page.context.browser.close()
            raise PlaywrightError('browser disconnected')

        with patch('ons_screenshots.capture_chart', disconnected):
            with self.assertRaisesRegex(PlaywrightError, 'browser disconnected'):
                await capture_screenshots([publication()], settings('all'), self.folder)
        self.assertFalse((self.folder / 'index.json').exists())

    async def test_unexpected_browser_evaluation_errors_are_fatal(self):
        with patch('ons_screenshots.capture_chart', AsyncMock(
                side_effect=PlaywrightError('Page.evaluate: TypeError: broken capture code'))):
            with self.assertRaisesRegex(PlaywrightError, 'broken capture code'):
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

    async def test_script_screenshots_only_and_all_off_preserve_other_outputs(self):
        records = [publication()]
        for mode, failed in (('off', False), ('all', False), ('all', True)):
            supplied = {
                'discovery': {'mode': 'off'}, 'catalogue': {'mode': 'off'},
                'screenshots': {'mode': mode}}
            config = validate_config(supplied)
            writes = Mock()
            client = SimpleNamespace(session=Mock())
            capture = AsyncMock(return_value=CaptureSummary(
                1, [{'url': URL, 'error': 'timeout'}] if failed else []))
            inputs = {'config.json': json.dumps(supplied),
                      'articles-and-dvcs.json': json.dumps(records)}

            def read_input(filename, *args, **kwargs):
                return StringIO(inputs[filename])

            with patch.dict(os.environ, {'GITHUB_OUTPUT': ''}), \
                    patch('catalogue.open', side_effect=read_input, create=True), \
                    patch('catalogue.DiscoveryClient', return_value=client), \
                    patch('catalogue.prepare_run', return_value=(records, [], None)), \
                    patch('catalogue.prepare_catalogue', return_value=None), \
                    patch('catalogue.write_json_files', writes), \
                    patch('catalogue.capture_screenshots', capture):
                await catalogue.main()
            writes.assert_called_once_with([])
            capture.assert_awaited_once_with(records, config['screenshots'])
            client.session.close.assert_called_once()

    async def test_script_cached_catalogue_preserves_derivations_without_http(self):
        supplied = {'discovery': {'mode': 'off'}, 'catalogue': {'mode': 'all'},
                    'screenshots': {'mode': 'off'}}
        records = [publication(), publication(OTHER_DOC, '2026-10-01', [OTHER_URL])]
        existing = []
        for record, chart_type in zip(records, ('column-and-line-chart', 'line-chart-with-dropdown')):
            article = {key: record[key] for key in ('doc_uri', 'title', 'release_date')}
            article.update(edition=None, vis=[{'url': record['vis_urls'][0], 'chart_type': chart_type}])
            existing.append(article)
        inputs = {'config.json': json.dumps(supplied),
                  'articles-and-dvcs.json': json.dumps(records), 'data.json': json.dumps(existing)}

        def read_input(filename, *args, **kwargs):
            return StringIO(inputs[filename])

        client = SimpleNamespace(session=Mock(), html_cache={},
                                 get_text=Mock(side_effect=AssertionError('unexpected HTTP')))
        capture = AsyncMock(return_value=CaptureSummary(0, []))
        with patch.dict(os.environ, {'GITHUB_OUTPUT': ''}), \
                patch('catalogue.open', side_effect=read_input, create=True), \
                patch('catalogue.DiscoveryClient', return_value=client), \
                patch('catalogue.write_json_files') as writes, \
                patch('catalogue.capture_screenshots', capture):
            await catalogue.main()
        files = writes.call_args.args[0]
        self.assertEqual(files[0][0], 'data.json')
        self.assertNotIn('discovery-state.json', dict(files))
        data = dict(files)['data.json']
        self.assertEqual([article['latest'] for article in data], [True, False])
        self.assertEqual([article['series'] for article in data], ['Example', 'Example'])
        self.assertTrue(all(article['theme'] == 'economy' for article in data))
        self.assertEqual(data[0]['vis'][0]['chart_families'], ['bar', 'line'])
        self.assertEqual(data[1]['vis'][0]['chart_features'], ['dropdown'])
        self.assertTrue(all('vis_urls' not in article for article in data))
        client.get_text.assert_not_called()
        client.session.close.assert_called_once()
        capture.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
