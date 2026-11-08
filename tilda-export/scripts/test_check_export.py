"""Offline regression checks. Run: python3 -m unittest discover -s scripts -v"""
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from check_export import inspect, render_preview
from performance import audit


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / 'export'
        shutil.copytree(Path(__file__).resolve().parents[1] / 'assets/example', self.base)
        self.manifest = self.base / 'manifest.json'

    def rewrite(self, change):
        value = json.loads(self.manifest.read_text())
        change(value)
        self.manifest.write_text(json.dumps(value))

    def test_example_builds_with_popup_outside_visible_flow(self):
        report, sources = inspect(self.manifest)
        self.assertTrue(report['ok'], report['errors'])
        preview = render_preview(sources)
        self.assertEqual(preview.count('data-local-record='), 2)
        self.assertIn('<dialog data-local-popup="#popup:contact">', preview)
        self.assertEqual(report['visual_check'], 'not-performed')

    def test_file_escape_rejected(self):
        outside = self.base.parent / 'outside.html'
        outside.write_text('<section class="demo-hero"></section>')
        self.rewrite(lambda value: value['blocks'][0].update(file='../outside.html'))
        self.assertFalse(inspect(self.manifest)[0]['ok'])

    def test_symlink_escape_rejected(self):
        outside = self.base.parent / 'outside.html'
        outside.write_text('external')
        (self.base / 'outside-link.html').symlink_to(outside)
        self.rewrite(lambda value: value['blocks'][0].update(file='outside-link.html'))
        self.assertFalse(inspect(self.manifest)[0]['ok'])

    def test_undefined_popup_and_anchor_fail(self):
        path = self.base / 'tilda-static-blocks/01-hero.html'
        code = path.read_text().replace('#popup:contact', '#popup:missing').replace('#demo-about', '#missing')
        path.write_text(code)
        errors = inspect(self.manifest)[0]['errors']
        self.assertTrue(any('попап не объявлен' in item for item in errors))
        self.assertTrue(any('якорь не найден' in item for item in errors))

    def test_local_resource_and_duplicate_id_fail(self):
        path = self.base / 'tilda-static-blocks/02-details.html'
        path.write_text(path.read_text() + '<img src="./photo.png" id="demo-title">')
        errors = inspect(self.manifest)[0]['errors']
        self.assertTrue(any('HTTPS URL' in item for item in errors))
        self.assertTrue(any('повтор ID' in item for item in errors))

    def test_full_document_rejected(self):
        path = self.base / 'tilda-static-blocks/01-hero.html'
        path.write_text('<!doctype html><html><body>' + path.read_text() + '</body></html>')
        self.assertFalse(inspect(self.manifest)[0]['ok'])

    def test_invalid_json_reports_failure(self):
        self.manifest.write_text('{broken')
        self.assertFalse(inspect(self.manifest)[0]['ok'])

    def test_head_is_audited_and_rendered_before_blocks(self):
        head = '<link rel="preload" as="font" href="https://example.com/font.woff2">'
        (self.base / 'head.html').write_text(head)
        self.rewrite(lambda value: value.update(head_file='head.html'))
        report, sources = inspect(self.manifest)
        self.assertTrue(report['ok'])
        self.assertIn('font-preload', [item['code'] for item in report['performance']['findings']])
        self.assertEqual(report['performance']['measurement'], 'not-performed')
        preview = render_preview(sources, head)
        self.assertLess(preview.index(head), preview.index('</head>'))

    def test_head_path_escape_and_full_document_rejected(self):
        self.rewrite(lambda value: value.update(head_file='../private.html'))
        self.assertFalse(inspect(self.manifest)[0]['ok'])
        (self.base / 'head.html').write_text('<html><head></head></html>')
        self.rewrite(lambda value: value.update(head_file='head.html'))
        self.assertFalse(inspect(self.manifest)[0]['ok'])

    def test_lcp_rules_do_not_guess_first_image(self):
        code = '<img src="https://example.com/logo.webp" loading="lazy" width="40" height="40">'
        self.assertNotIn('lcp-lazy', [item['code'] for item in audit([('block', code)])['findings']])
        marked = audit([('block', code)], {'lcp': {'kind': 'image', 'src': 'https://example.com/logo.webp'}})
        self.assertIn('lcp-lazy', [item['code'] for item in marked['findings']])

    def test_duplicate_imports_report_declarations_not_requests(self):
        code = '<style>@import url("https://fonts.example.com/font.css");</style>'
        result = audit([('a', code), ('b', code)])
        self.assertEqual(result['inventory']['font_imports'], 2)
        self.assertIn('duplicate-resource', [item['code'] for item in result['findings']])
        self.assertEqual(result['measurement'], 'not-performed')

    def test_font_import_in_js_is_not_reported_as_css(self):
        result = audit([('script', '<script>const text = \'@import "https://fonts.example.com/x"\';</script>')])
        self.assertEqual(result['inventory']['font_imports'], 0)

    def test_dimensions_may_be_reserved_in_css(self):
        result = audit([('block', '<img src="https://example.com/a.webp" style="aspect-ratio:2/1">')])
        self.assertNotIn('image-space', [item['code'] for item in result['findings']])

    def test_bad_performance_config_is_validation_error(self):
        self.rewrite(lambda value: value.update(performance={'lcp': 'not-an-object'}))
        self.assertFalse(inspect(self.manifest)[0]['ok'])

    def test_empty_fragment_is_valid_top_of_document_link(self):
        file = self.base / 'tilda-static-blocks/01-hero.html'
        file.write_text(file.read_text() + '<a href="#">В начало</a>')
        self.assertTrue(inspect(self.manifest)[0]['ok'])


if __name__ == '__main__':
    unittest.main()
