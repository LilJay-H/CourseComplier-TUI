"""Synthetic content regressions: formulas, secrets, resources and ASR scope."""
import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from handouter.agents.web import _evidence_text, create_web_handoff
from handouter.asr import normalize_funasr_result
from handouter.html_export import export_html
from handouter.revision import read_revision_note
from handouter.service import prepare_lecture
from handouter.validation import validate_note_output, validate_workspace
from _gates import requires_html

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


class ContentValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        source = self.root / 'lecture.txt'
        source.write_text('课程 token = 0；GPT-4、CUDA 12 和 x² ≥ 0。', encoding='utf-8')
        self.workspace = prepare_lecture(self.root / 'out', lecture_id='content', course_title='合成测试',
                                         transcript=source, use_slides_as_source=False).workspace
        self.sources_path = self.workspace / 'handoff/sources.json'
        self.sources = json.loads(self.sources_path.read_text())
        self.note = self.workspace / self.sources['expected_output']
        self.images = self.workspace / 'slides/images'
        self.images.mkdir(parents=True, exist_ok=True)
        (self.images / 'PPT (01) image.png').write_bytes(PNG)

    def validate(self, text, *, slides=False, embed=False, web=False):
        self.sources['options'].update(use_slides_as_source=slides, embed_slides=embed, allow_web=web)
        self.sources_path.write_text(json.dumps(self.sources))
        self.note.write_text(text, encoding='utf-8')
        return validate_note_output(self.workspace)

    def test_nested_math_and_template_teaching_pass(self):
        for text in (r'$\frac{1}{\sqrt{x}}$', '模板示例 `{{ user.name }}`。',
                     '```jinja\n{{ transcript_path }}\n```'):
            with self.subTest(text=text):
                self.assertTrue(self.validate(text).ok)

    def test_known_unfilled_task_placeholder_still_fails(self):
        self.assertFalse(self.validate('课程材料：{{transcript_path}}').ok)

    def test_prompt_nested_math_passes_but_known_placeholder_fails(self):
        prompt = self.workspace / 'handoff/PROMPT.md'
        prompt.write_text(prompt.read_text() + '\n' + r'$\frac{1}{\sqrt{x}}$')
        self.assertTrue(validate_workspace(self.workspace).ok)
        prompt.write_text(prompt.read_text() + '\n{{expected_outputs_one_per_line}}')
        self.assertFalse(validate_workspace(self.workspace).ok)

    def test_teaching_assignments_pass_all_consumers(self):
        text = '```python\ntoken = 0\napi_key = "YOUR_API_KEY"\n```\n模板 token=<TOKEN>'
        self.assertTrue(self.validate(text).ok)
        self.assertEqual(read_revision_note(self.note)['text'], text)
        self.assertEqual(_evidence_text(text.encode()), text)
        self.assertIn('token = 0', create_web_handoff(self.workspace).materials.read_text())

    def test_uncertain_assignment_warns_without_echo(self):
        text = 'token = lesson_value'
        report = self.validate(text)
        self.assertTrue(report.ok)
        self.assertTrue(report.warnings)
        self.assertNotIn('lesson_value', str(report.warnings))
        revision = read_revision_note(self.note)
        self.assertTrue(revision.get('warnings'))

    def test_secrets_in_code_json_and_urls_block_without_echo(self):
        samples = ['```python\nauth_key = "synthetic-test-secret"\n```',
                   '{"api_key": "sk-test-AbC123xyZ987LongSyntheticSecret"}',
                   'https://example.invalid/media?token=AbC123xY987LongSyntheticSecret',
                   'Cookie: session=synthetic-test-secret; theme=light',
                   'Authorization: Bearer synthetic-test-secret']
        for text in samples:
            with self.subTest(text=text):
                report = self.validate(text)
                self.assertFalse(report.ok)
                self.assertNotIn('synthetic-test-secret', str(report.errors))
                with self.assertRaises(ValueError):
                    read_revision_note(self.note)
                with self.assertRaises(ValueError):
                    _evidence_text(text.encode())

    def test_url_credentials_are_explicit_sensitive_sources(self):
        for field in ('auth_key', 'signature', 'token', 'access_token'):
            with self.subTest(field=field):
                text = f'https://example.invalid/video?{field}=12345'
                report = self.validate(text)
                self.assertFalse(report.ok)
                self.assertNotIn('12345', str(report.errors))
                with self.assertRaises(ValueError):
                    _evidence_text(text.encode())

    def test_reference_and_html_missing_images_fail(self):
        for text in ('![PPT][slide]\n\n[slide]: ../slides/images/missing.png "Title"',
                     '<img src="../slides/images/missing.png">',
                     '<details><summary>PPT</summary><img src="../slides/images/missing.png"></details>'):
            with self.subTest(text=text):
                report = self.validate(text, slides=True, embed=True)
                self.assertFalse(report.ok)
                self.assertEqual(report.image_links, 1)

    @requires_html

    def test_legal_space_parentheses_and_title_paths_share_export(self):
        links = ['![PPT](<../slides/images/PPT (01) image.png> "图表标题")',
                 '![PPT][slide]\n\n[slide]: <../slides/images/PPT (01) image.png> "Title"',
                 '<details><summary>PPT</summary><img src="../slides/images/PPT%20(01)%20image.png"></details>']
        for text in links:
            with self.subTest(text=text):
                report = self.validate(text, slides=True, embed=True)
                self.assertTrue(report.ok, report.errors)
                self.assertEqual(report.image_links, 1)
                html = export_html(self.note)[0].read_text()
                self.assertIn('data:image/png;base64,', html)
                self.assertIn("default-src 'none'", html)

    def test_resource_examples_in_code_are_not_images(self):
        report = self.validate('```markdown\n![x](missing.png)\n<img src="missing.png">\n```\n`![x](missing.png)`')
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.image_links, 0)

    @requires_html

    def test_slide_switches_apply_to_all_syntaxes_and_export(self):
        for text in ('![PPT][p]\n\n[p]: <../slides/images/PPT (01) image.png>',
                     '<img src="../slides/images/PPT (01) image.png">'):
            for slides, embed in ((False, True), (True, False), (False, False)):
                with self.subTest(text=text, slides=slides, embed=embed):
                    self.assertFalse(self.validate(text, slides=slides, embed=embed).ok)
                    with self.assertRaises(ValueError):
                        export_html(self.note)

    @requires_html

    def test_active_or_unsupported_html_resources_rejected(self):
        for text in ('<iframe src="https://example.invalid"></iframe>', '<script>alert(1)</script>',
                     '<img src="x.png" srcset="other.png 2x">', '<svg><image href="x.png"/></svg>',
                     '<div style="background:url(x.png)">x</div>'):
            with self.subTest(text=text):
                self.assertFalse(self.validate(text).ok)
                with self.assertRaises(ValueError):
                    export_html(self.note)

    @requires_html

    def test_remote_images_need_web_permission_and_cannot_export_offline(self):
        text = '<img src="https://example.invalid/image.png">'
        self.assertFalse(self.validate(text).ok)
        self.assertTrue(self.validate(text, web=True).ok)
        with self.assertRaises(ValueError):
            export_html(self.note)
        self.assertFalse(self.validate('<img src="data:image/png;base64,eA==">', web=True).ok)

    def test_encoded_escape_and_private_resource_fail(self):
        (self.workspace / 'transcript/private.png').write_bytes(PNG)
        for link in ('../transcript/private.png', '%2e%2e/%2e%2e/outside.png'):
            with self.subTest(link=link):
                self.assertFalse(self.validate(f'<img src="{link}">').ok)

    @requires_html

    def test_encoded_filename_is_decoded_once_and_malformed_link_fails(self):
        (self.images / 'PPT #1.png').write_bytes(PNG)
        report = self.validate('![图](../slides/images/PPT%20%231.png)', slides=True, embed=True)
        self.assertTrue(report.ok, report.errors)
        self.assertIn('data:image/png;base64,', export_html(self.note)[0].read_text())
        self.assertFalse(self.validate('<a href="http://[">bad</a>').ok)

    def test_chinese_english_technical_information_survives_asr(self):
        items = ['中文说明 x² ≥ 0', 'CUDA 12 / GPT-4', 'API', '1024', 'O(n log n)', 'A = λI']
        segments, text, _ = normalize_funasr_result([{'text': '\n'.join(items), 'sentence_info': [
            {'text': value, 'start': i * 100, 'end': (i + 1) * 100} for i, value in enumerate(items)]}])
        self.assertEqual([item['text'] for item in segments], items)
        for value in items:
            self.assertIn(value, text)

    def test_control_json_must_be_objects(self):
        self.note.write_text('# 测试讲义')
        for relative in ('manifest.json', 'state.json', 'handoff/sources.json'):
            with self.subTest(relative=relative):
                path = self.workspace / relative
                before = path.read_bytes()
                try:
                    path.write_text('[]')
                    self.assertFalse(validate_workspace(self.workspace).ok)
                    if relative != 'manifest.json':
                        self.assertFalse(validate_note_output(self.workspace).ok)
                finally:
                    path.write_bytes(before)

    def test_broken_math_tab_and_details_blank_lines_warn(self):
        # 1. Broken math tab escape ($	o$) should warn
        report_tab = self.validate('# 标题\n\n流程 $	o$ 阶段。')
        self.assertTrue(any('制表符或转义损坏' in w for w in report_tab.warnings))

        # 2. Clean unicode arrow should not warn
        report_clean = self.validate('# 标题\n\n流程 → 阶段。')
        self.assertFalse(any('制表符或转义损坏' in w for w in report_clean.warnings))

        # 3. details blank lines in complete mode should warn
        self.sources['mode'] = 'complete'
        self.sources_path.write_text(json.dumps(self.sources))
        broken_fold = (
            '# 标题\n\n'
            '<details class="complete-detail">\n'
            '<summary>课堂细节</summary>\n\n'
            '段落内容。\n\n'
            '</details>\n'
        )
        report_fold = self.validate(broken_fold)
        self.assertTrue(any('打断 CommonMark/Typora' in w for w in report_fold.warnings))

        # 4. details cleanly wrapped in <p> should not trigger this warning
        clean_fold = (
            '# 标题\n\n'
            '<details class="complete-detail">\n'
            '<summary>课堂细节</summary>\n'
            '<p>段落内容。</p>\n'
            '</details>\n'
        )
        report_clean_fold = self.validate(clean_fold)
        self.assertFalse(any('打断 CommonMark/Typora' in w for w in report_clean_fold.warnings))
