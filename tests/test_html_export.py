"""Exercise real Markdown/math rendering and portable output boundaries."""

import base64
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from handouter.html_export import export_html

READY = all(importlib.util.find_spec(name) for name in ('markdown_it', 'mdit_py_plugins', 'latex2mathml'))


@unittest.skipUnless(READY, 'HTML extra not installed')
class HTMLExportTests(unittest.TestCase):
    def test_loaded_chinese_examples_render_folds_math_and_literal_figure_syntax(self):
        from handouter.handoff import default_skill_path
        from handouter.html_export import render_note
        examples = default_skill_path().parent / 'references/examples'
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rendered = {}
            for mode in ('full', 'deep', 'verbatim', 'summary', 'complete', 'visual'):
                note = root / (mode + '.md')
                note.write_bytes((examples / (mode + '.md')).read_bytes())
                rendered[mode] = render_note(note, root)
                self.assertNotIn('<details open', rendered[mode])
                self.assertIn('<h1', rendered[mode])
            self.assertIn('complete-detail', rendered['complete'])
            self.assertIn('complete-verbatim', rendered['complete'])
            self.assertIn('原转写节选：等待间隔的当堂更正', rendered['complete'])
            self.assertIn('<math', rendered['visual'])
            # Example figure paths are literal syntax, never fake course assets.
            self.assertNotIn('<img src="../slides/', rendered['visual'])
            self.assertIn('verified-one-by-one.png', rendered['visual'])

    def test_handoff_includes_offline_dual_delivery_rules(self):
        from handouter.service import prepare_lecture
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            transcript = root / 'transcript.txt'
            transcript.write_text('这是一段测试课程内容。', encoding='utf-8')
            result = prepare_lecture(root / 'output', lecture_id='test-html', course_title='测试',
                                     transcript=transcript, modes=['deep', 'summary'])
            prompt = (result.workspace / 'handoff' / 'PROMPT.md').read_text(encoding='utf-8')
            # Existing opt-in policy keeps an unrequested HTML file out of the
            # delivery requirements while retaining the offline rendering rules.
            self.assertIn('只有用户请求 HTML 时才由 Course Compiler', prompt)
            self.assertIn('未请求 HTML 不视为缺失或失败', prompt)
            sources = json.loads((result.workspace / 'handoff/sources.json').read_text(encoding='utf-8'))
            relative = 'handoff/skill/references/common/delivery.md'
            self.assertIn(relative, sources['skill']['modules'])
            delivery = (result.workspace / relative).read_text(encoding='utf-8')
            self.assertIn('HTML 待本地导出/补图', delivery)
            self.assertIn('无 CDN/远程脚本', delivery)
            self.assertIn('公式离线呈现', delivery)

    def test_agent_command_reports_html_failure_without_losing_markdown(self):
        from handouter.cli import main
        from handouter.service import prepare_lecture
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / 'source.txt'
            source.write_text('Synthetic 中文。', encoding='utf-8')
            prepared = prepare_lecture(Path(td) / 'output', lecture_id='html-retry',
                course_title='Synthetic', transcript=source, use_slides_as_source=False)
            root = prepared.workspace
            note = root / prepared.handoff.output_path
            note.write_text('# Complete Markdown\n\n$x$\n', encoding='utf-8')
            original = note.read_bytes()
            with patch('handouter.cli.run_cli_agent', side_effect=AssertionError('HTML retry must not call Agent')), \
                 patch('handouter.html_export.render_note', side_effect=ValueError('unsupported formula')), \
                 contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
                code = main(['agent-run', str(root), '--agent', 'codex'])
            self.assertNotEqual(code, 0)
            self.assertEqual(json.loads(out.getvalue())['html_delivery']['status'], 'failed')
            self.assertIn('HTML 导出失败', err.getvalue())
            self.assertEqual(note.read_bytes(), original)

    def test_cli_exports_json_and_real_html(self):
        from handouter.cli import main
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture(root)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(['export-html', str(root)]), 0)
            paths = json.loads(out.getvalue())['html']
            self.assertEqual(len(paths), 1)
            self.assertIn('<math', Path(paths[0]).read_text(encoding='utf-8'))

    def test_agent_exports_only_this_runs_outputs(self):
        from handouter.agents.base import AgentRunResult
        from handouter.html_export import export_agent_html
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture(root)
            (root / 'notes' / 'old.md').write_text('old', encoding='utf-8')
            result = AgentRunResult('codex', 0, {'deep': 'notes/deep.md'}, True, ())
            delivery = export_agent_html(root, result)
            self.assertEqual(delivery['status'], 'ready')
            self.assertEqual(set(delivery['outputs']), {'deep'})
            self.assertFalse((root / 'notes' / 'old.html').exists())
            failed = AgentRunResult('codex', 1, result.outputs, False, ('failed',))
            self.assertEqual(export_agent_html(root, failed)['status'], 'skipped')

    def fixture(self, root):
        (root / 'notes').mkdir()
        images = root / 'slides' / 'images'
        images.mkdir(parents=True)
        (images / 'PPT 01.png').write_bytes(base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='))
        note = root / 'notes' / 'deep.md'
        note.write_text('# 测试讲义\n\n> [!IMPORTANT]\n> 下周小测\n\n'
                        '<details><summary>课程信息</summary>教师</details>\n\n'
                        '## 第一章\n\n<details><summary>本章时间与来源</summary>00:00–10:00</details>\n\n'
                        '行内 $E=mc^2$\n\n$$\n\\frac{1}{2}+\\sqrt{x}\n$$\n\n'
                        '![PPT](<../slides/images/PPT 01.png>)\n\n'
                        '<details><summary>待核对</summary>术语</details>', encoding='utf-8')
        return note

    def test_offline_images_math_and_folds(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture(root)
            output = export_html(root)[0].read_text(encoding='utf-8')
            self.assertIn('data:image/png;base64,', output)
            self.assertIn('<math', output)
            self.assertIn('<mfrac>', output)
            self.assertIn('<msqrt>', output)
            self.assertIn('<summary>本章时间与来源</summary>', output)
            self.assertIn('<summary>待核对</summary>', output)
            self.assertNotIn('<script', output)
            self.assertNotIn('../slides/', output)

    def test_centered_slide_figures_and_panel_groups_survive_offline_export(self):
        for layout, count in [('ppt-full', 1), ('ppt-half', 1), ('ppt-third', 1),
                              ('ppt-cols-2', 2), ('ppt-cols-3', 3), ('ppt-grid-2x2', 4)]:
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                note = self.fixture(root)
                panels = ''.join(
                    f'<figure class="ppt-figure{(" " + layout) if count == 1 else ""}">'
                    '<img src="../slides/images/PPT 01.png" alt="核验过的状态 &amp; 关系">'
                    f'<figcaption>{i}. 步骤说明</figcaption></figure>'
                    for i in range(1, count + 1))
                block = panels if count == 1 else f'<div class="ppt-grid {layout}">{panels}</div>'
                note.write_text('# 图文示例\n\n### 一组局部步骤\n\n' + block
                    + '\n\n解释每一步的变化与原因。\n\n1. 第一步\n2. 第二步\n\n'
                      '$$\n\\frac{1}{2}\n$$\n', encoding='utf-8')
                output = export_html(note)[0].read_text(encoding='utf-8')
                self.assertEqual(output.count('<figure '), count)
                self.assertEqual(output.count('<figcaption>'), count)
                self.assertEqual(output.count('data:image/png;base64,'), count)
                self.assertIn(layout, output)
                self.assertLess(output.index('<figure '), output.index('解释每一步'))
                self.assertIn('<ol>', output)
                self.assertIn('<mfrac>', output)
                self.assertNotIn('../slides/', output)
                self.assertNotIn('<script', output)
                self.assertIn('核验过的状态 &amp; 关系', output)

    def test_slide_groups_inside_closed_details_keep_order_and_captions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            note = self.fixture(root)
            note.write_text('# 讲义\n\n<details>\n<summary>课堂步骤</summary>\n\n'
                '<div class="ppt-grid ppt-grid-2x2">\n'
                + ''.join('<figure class="ppt-figure">'
                          '<img src="../slides/images/PPT 01.png" alt="状态">'
                          f'<figcaption>步骤{i}</figcaption></figure>\n' for i in range(1, 5))
                + '</div>\n\n依次解释 $x$ 的变化。\n\n</details>\n', encoding='utf-8')
            output = export_html(note)[0].read_text(encoding='utf-8')
            self.assertNotIn('<details open', output)
            positions = [output.index(f'<figcaption>步骤{i}</figcaption>') for i in range(1, 5)]
            self.assertEqual(positions, sorted(positions))
            self.assertLess(output.index('<details>'), positions[0])
            self.assertLess(positions[-1], output.index('</details>'))
            self.assertIn('<math', output)

    def test_slide_layout_does_not_allow_style_scripts_or_missing_resources(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            note = self.fixture(root)
            invalid = [
                '<figure class="ppt-figure" style="width:50%"><img src="../slides/images/PPT 01.png"></figure>',
                '<figure class="ppt-figure"><img src="../slides/images/PPT 01.png" onload="alert(1)"></figure>',
                '<figure class="ppt-figure"><img src="../slides/images/missing.png"></figure>',
                '<div class="ppt-grid ppt-cols-2"><figure><img src="https://example.invalid/image.png"></figure></div>',
            ]
            for content in invalid:
                with self.subTest(content=content):
                    note.write_text('# 图\n\n' + content, encoding='utf-8')
                    with self.assertRaises(ValueError):
                        export_html(note)
            self.assertEqual(list((root / 'notes').glob('*.html')), [])

    def test_layout_css_preserves_aspect_ratio_and_responsive_reading_order(self):
        from handouter.html_export import STYLE
        for selector in ('.ppt-figure', '.ppt-half', '.ppt-third', '.ppt-cols-2', '.ppt-cols-3', '.ppt-grid-2x2'):
            self.assertIn(selector, STYLE)
        self.assertIn('height:auto', STYLE)
        self.assertIn('grid-template-columns:1fr', STYLE)
        self.assertIn('@media print', STYLE)
        self.assertNotIn('grid-auto-flow:dense', STYLE)
        self.assertNotIn('object-fit:cover', STYLE)

    def test_repeat_export_preserves_previous_html_and_markdown(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            note = self.fixture(root)
            original = note.read_bytes()
            first = export_html(note)[0]
            first.write_text('user edited HTML', encoding='utf-8')
            second = export_html(note)[0]
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_text(), 'user edited HTML')
            self.assertEqual(note.read_bytes(), original)

    def test_active_html_rejected_without_partial_output(self):
        with tempfile.TemporaryDirectory() as td:
            note = Path(td) / 'note.md'
            note.write_text('<script>alert(1)</script><iframe src="https://example.com"></iframe>'
                            '<details open onclick="alert(1)"><summary>X</summary>Y</details>'
                            '<a href="javascript:alert(1)">bad</a>', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'HTML'):
                export_html(note)
            self.assertFalse(note.with_suffix('.html').exists())

    def test_complete_layers_and_context_before_figures_survive_export(self):
        from handouter.validation import _VisibleNoteText
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            note = self.fixture(root)
            figure = ('<figure class="ppt-figure ppt-half">'
                      '<img src="../slides/images/PPT 01.png" alt="synthetic state">'
                      '<figcaption>LOCAL_FIGURE</figcaption></figure>')
            text = ('# Complete synthetic\n\n## Unit\n\nCONTEXT_BEFORE_FIGURE\n\n'
                    + figure + '\n\nVISIBLE_EXPLANATION $x+1$\n\n'
                    '<details class="complete-detail">\n<summary>课堂细节：具体过程</summary>\n\n'
                    'DETAIL_ONLY\n\n- Extra step\n\n$$\n\\frac{1}{\\sqrt{x}}\n$$\n\n'
                    + figure.replace('LOCAL_FIGURE', 'DETAIL_FIGURE')
                    + '\n\nDETAIL_EXPLANATION\n\n</details>\n\n'
                    '<details class="complete-verbatim">\n<summary>清晰逐字记录：合成片段</summary>\n\n'
                    'TRANSCRIPT_ONLY\n\n</details>\n\n## Next unit\n\nVISIBLE_NEXT_UNIT\n')
            note.write_text(text, encoding='utf-8')
            html = export_html(note)[0].read_text(encoding='utf-8')
            self.assertLess(html.index('CONTEXT_BEFORE_FIGURE'), html.index('LOCAL_FIGURE'))
            self.assertLess(html.index('LOCAL_FIGURE'), html.index('VISIBLE_EXPLANATION'))
            self.assertLess(html.index('DETAIL_FIGURE'), html.index('DETAIL_EXPLANATION'))
            self.assertIn('<details class="complete-detail">', html)
            self.assertIn('<details class="complete-verbatim">', html)
            self.assertIn('<li>Extra step</li>', html)
            self.assertIn('<mfrac>', html)
            self.assertIn('<msqrt>', html)
            self.assertEqual(html.count('data:image/png;base64,'), 2)
            self.assertNotIn('../slides/', html)
            self.assertNotIn(' open', html)
            visible = _VisibleNoteText()
            visible.feed(html)
            visible.close()
            body = ''.join(visible.parts)
            self.assertIn('VISIBLE_EXPLANATION', body)
            self.assertIn('VISIBLE_NEXT_UNIT', body)
            self.assertNotIn('DETAIL_ONLY', body)
            self.assertNotIn('TRANSCRIPT_ONLY', body)
            self.assertEqual(note.read_text(encoding='utf-8'), text)

    def test_two_figure_pairs_stay_with_their_text_under_one_heading(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            note = self.fixture(root)
            def figure(label):
                return ('<figure class="ppt-figure ppt-half">'
                        '<img src="../slides/images/PPT 01.png" alt="Synthetic state">'
                        f'<figcaption>{label}</figcaption></figure>')
            note.write_text('# Synthetic handout\n\n## One teaching unit\n\n'
                            'CONTEXT_BEFORE_FIRST_FIGURE\n\n' + figure('FIGURE_A')
                            + '\n\nEXPLANATION_A: use the first state here.\n\n'
                            'TRANSITION_TO_SECOND_STAGE\n\n' + figure('FIGURE_B')
                            + '\n\nEXPLANATION_B: explain the second state here.\n\n'
                            '<details><summary>本章时间与来源</summary>'
                            '<p>Synthetic evidence only.</p></details>\n', encoding='utf-8')
            original = note.read_bytes()
            html = export_html(note)[0].read_text(encoding='utf-8')
            labels = ('CONTEXT_BEFORE_FIRST_FIGURE', 'FIGURE_A', 'EXPLANATION_A',
                      'TRANSITION_TO_SECOND_STAGE', 'FIGURE_B', 'EXPLANATION_B', '本章时间与来源')
            positions = [html.index(label) for label in labels]
            self.assertEqual(positions, sorted(positions))
            self.assertEqual(html.count('<h2>'), 1)
            self.assertEqual(html.count('<figure '), 2)
            self.assertEqual(html.count('data:image/png;base64,'), 2)
            self.assertNotIn('<details', html[positions[1]:positions[2]])
            self.assertNotIn('<details', html[positions[4]:positions[5]])
            self.assertEqual(note.read_bytes(), original)

    def test_complete_hidden_missing_image_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            note = self.fixture(root)
            note.write_text('# Visible tutorial\n\nMain explanation.\n\n'
                            '<details class="complete-detail"><summary>More</summary>\n\n'
                            '<figure class="ppt-figure"><img src="../slides/images/missing.png" alt="missing"></figure>'
                            '\n\n</details>\n', encoding='utf-8')
            with self.assertRaises(ValueError):
                export_html(note)
            self.assertFalse(note.with_suffix('.html').exists())

    def test_bad_image_stops_export_without_partial_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture(root)
            bad = root / 'notes' / 'summary.md'
            bad.write_text('![bad](https://example.com/private.png)', encoding='utf-8')
            with self.assertRaises(ValueError):
                export_html(root)
            self.assertEqual(list((root / 'notes').glob('*.html')), [])

    def test_symlink_cannot_embed_outside_materials(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as other:
            root = Path(td)
            note = self.fixture(root)
            outside = Path(other) / 'outside.png'
            outside.write_bytes(b'\x89PNG\r\n\x1a\n')
            link = root / 'slides' / 'images' / 'outside.png'
            try:
                link.symlink_to(outside)
            except OSError:
                self.skipTest('symlinks unavailable')
            note.write_text('![bad](../slides/images/outside.png)', encoding='utf-8')
            with self.assertRaises(ValueError):
                export_html(root)
