"""Portable layout contracts; these do not claim native Typora UI validation."""
import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from handouter.typora_export import export_typora, typora_markdown
from handouter.resources import parse_resources
from handouter.cli import main


IMAGE = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


def figure(name='one.png', classes='ppt-figure', caption='状态 &amp; 条件'):
    return (f'<figure class="{classes}">\n<img src="{name}" alt="真实图注">\n'
            f'<figcaption>{caption}</figcaption>\n</figure>')


def grid(kind, count):
    return f'<div class="ppt-grid {kind}">\n' + '\n'.join(figure(f'{n}.png', caption=f'步骤{n}') for n in range(count)) + '\n</div>'


class TyporaExportTests(unittest.TestCase):
    def test_half_third_and_full_use_real_image_width_not_class(self):
        for cls, width in [('ppt-half', '50%'), ('ppt-third', '33.333333%'), ('ppt-full', '100%')]:
            with self.subTest(cls=cls):
                result = typora_markdown(figure(classes='ppt-figure ' + cls))
                self.assertIn(f'width="{width}"', result)
                self.assertIn('align="center"', result)
                self.assertNotIn('class=', result)
                self.assertIn('状态 &amp; 条件', result)

    def test_mathematical_caption_moves_outside_html_paragraph(self):
        from handouter.math_markup import math_spans
        from handouter.resources import markdown_parser
        source = figure(classes='ppt-figure ppt-half', caption=r'关系 $a\cup b$')
        result = typora_markdown(source)
        self.assertIn('width="50%"', result)
        self.assertIn(r'关系 $a\cup b$', result)
        self.assertNotIn('<small>', result)
        self.assertFalse([span for token in markdown_parser().parse(result)
                          if token.type == 'html_block' for span in math_spans(token.content)])

    def test_export_protects_math_table_tail_cells(self):
        from handouter.resources import markdown_parser
        with tempfile.TemporaryDirectory() as td:
            note = Path(td) / 'table.md'
            note.write_text('| name | formula | tail |\n| --- | --- | --- |\n'
                            + r'| example | $|x|$ | PRESERVE_TAIL |' + '\n', encoding='utf-8')
            result = export_typora(note).read_text(encoding='utf-8')
            self.assertIn('PRESERVE_TAIL', markdown_parser().render(result))
            self.assertEqual(markdown_parser().render(result).count('<td>'), 3)

    def test_parallel_two_three_and_four_keep_panel_order(self):
        for kind, count, rows in [('ppt-cols-2', 2, 1), ('ppt-cols-3', 3, 1), ('ppt-grid-2x2', 4, 2)]:
            result = typora_markdown(grid(kind, count))
            self.assertEqual(result.count('<tr>'), rows)
            self.assertEqual(result.count('<td '), count)
            self.assertNotIn('class=', result)
            self.assertEqual(parse_resources(result).images, tuple(f'{n}.png' for n in range(count)))
            for number in range(count - 1):
                self.assertLess(result.index(f'步骤{number}'), result.index(f'步骤{number + 1}'))

    def test_only_figures_change_context_math_quotes_and_code_preserved(self):
        prefix = '# 标题\n\n背景原句：期末40分。\n\n$\\frac{1}{\\sqrt{x}}$\n\n'
        middle = '\n\n第一组对应解释，不应移到别处。\n\n'
        suffix = '\n\n第二组解释。\n\n```html\n' + grid('ppt-cols-2', 2) + '\n```\n'
        source = prefix + figure() + middle + figure(classes='ppt-figure ppt-half') + suffix
        result = typora_markdown(source)
        self.assertTrue(result.startswith(prefix))
        self.assertTrue(result.endswith(suffix))
        self.assertIn(middle, result)
        self.assertEqual(result.count('<p align="center">'), 2)

    def test_grid_inside_complete_fold_is_rewritten_without_touching_other_layers(self):
        before = '<details class="complete-detail">\n<summary>课堂细节</summary>\n'
        after = '\n<p>讲解原文。</p>\n</details>\n\n<details class="complete-verbatim"><summary>原转写节选</summary><blockquote>原话不改。</blockquote></details>'
        source = before + grid('ppt-cols-2', 2) + after
        result = typora_markdown(source)
        self.assertTrue(result.startswith(before))
        self.assertTrue(result.endswith(after))
        self.assertIn('<table width="100%">', result)

    def test_unknown_complex_figure_and_bad_panel_count_refuse_not_drop_text(self):
        for source in [grid('ppt-cols-2', 3), figure().replace('<figcaption>', '<p>正文不能丢。</p><figcaption>'),
                       figure().replace('</figure>', ''), grid('unknown', 2)]:
            with self.subTest(source=source[:45]):
                with self.assertRaises(ValueError):
                    typora_markdown(source)

    def test_literal_figure_code_in_raw_html_is_not_transformed(self):
        source = '<pre><code>&lt;figure class="ppt-figure"&gt;</code></pre>\n'
        self.assertEqual(typora_markdown(source), source)
        source = '`<figure class="ppt-figure">`\n'
        self.assertEqual(typora_markdown(source), source)

    def test_crlf_and_source_quotes_not_normalized(self):
        source = '前文😀\r\n\r\n' + figure().replace('\n', '\r\n') + '\r\n\r\n后文原话。\r\n'
        result = typora_markdown(source)
        self.assertTrue(result.startswith('前文😀\r\n\r\n'))
        self.assertTrue(result.endswith('\r\n\r\n后文原话。\r\n'))

    def test_export_versions_original_image_and_plain_text_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'notes'
            root.mkdir()
            (root / 'one.png').write_bytes(IMAGE)
            note = root / '课程 原稿.md'
            source = '# 不改原文\n\n' + figure(classes='ppt-figure ppt-half') + '\n'
            note.write_text(source, encoding='utf-8')
            first = export_typora(note)
            second = export_typora(note)
            self.assertNotEqual(first, second)
            self.assertEqual(note.read_text(), source)
            self.assertEqual((root / 'one.png').read_bytes(), IMAGE)
            self.assertTrue(first.name.endswith('.typora.md'))
            self.assertTrue(second.name.endswith('.typora-001.md'))
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_cli_returns_actual_path_and_does_not_claim_semantics(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'notes'
            root.mkdir()
            (root / 'one.png').write_bytes(IMAGE)
            note = root / 'plain.md'
            note.write_text('# plain\n\n' + figure(), encoding='utf-8')
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(['export-typora', str(note)])
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertTrue(Path(payload['typora_markdown']).exists())
            self.assertEqual(payload['content_review'], 'not_performed')

    def test_missing_remote_and_active_resources_never_publish(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            note = root / 'unsafe.md'
            for source in [figure('missing.png'), figure('https://example.invalid/p.png'),
                           '<script>alert(1)</script>\n', figure('../outside.png')]:
                note.write_text(source, encoding='utf-8')
                with self.assertRaises(ValueError):
                    export_typora(note)
                self.assertEqual(list(root.glob('*.typora*.md')), [])

    def test_notes_relative_images_keep_same_base(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'notes').mkdir()
            (root / 'slides/images').mkdir(parents=True)
            (root / 'slides/images/one.png').write_bytes(IMAGE)
            note = root / 'notes/deep-001.md'
            note.write_text(figure('../slides/images/one.png'), encoding='utf-8')
            result = export_typora(note)
            self.assertEqual(result.parent.resolve(), note.parent.resolve())
            self.assertIn('../slides/images/one.png', result.read_text())


if __name__ == '__main__':
    unittest.main()
