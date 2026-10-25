"""Complete routing, source identity and task contracts; not model-quality tests."""

import base64
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT))

from handouter.cli import _build_parser, main
from handouter.handoff import default_skill_path
from handouter.service import prepare_lecture, refresh_handoff
from handouter.skill_plan import RULES_VERSION, plan_modules
from handouter.agents.manual import create_manual_bundle
from handouter.agents.web import create_web_handoff
from handouter.tasks import plan_task
from handouter.tui import selected_modes, build_cli_args, _load_workspace_into_state, _safe_preferences
from handouter.validation import validate_workspace, validate_note_output

PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


class CompleteModeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.transcript = self.root / 'source.txt'
        self.transcript.write_text('合成课堂：先介绍问题，再展示两种状态。\n学生提问，教师说明限制。', encoding='utf-8')
        self.slides = self.root / 'source-slides'
        self.slides.mkdir()
        (self.slides / '状态 01.png').write_bytes(PNG)
        self.skill = default_skill_path()

    def prepare(self, name='complete-course', **kwargs):
        kwargs.setdefault('modes', ['complete'])
        return prepare_lecture(self.root / 'output', lecture_id=name, course_title='合成课堂',
            transcript=self.transcript, skill_path=self.skill, **kwargs).workspace.resolve()

    def sources(self, workspace):
        return json.loads((workspace / 'handoff/sources.json').read_text(encoding='utf-8'))

    def test_complete_is_one_output_with_scoped_layer_dependencies(self):
        workspace = self.prepare()
        sources = self.sources(workspace)
        self.assertEqual(sources['modes'], ['complete'])
        self.assertEqual(sources['expected_outputs'], {'complete': 'notes/complete-001.md'})
        self.assertEqual(sources['skill']['rules_version'], RULES_VERSION)
        modules = sources['skill']['modules']
        for mode in ('complete', 'deep', 'full', 'verbatim'):
            self.assertIn(f'handoff/skill/references/modes/{mode}.md', modules)
        self.assertNotIn('handoff/skill/references/modes/summary.md', modules)
        self.assertNotIn('handoff/skill/references/execution/multi-output.md', modules)
        self.assertIn('handoff/skill/references/common/delivery.md', modules)
        self.assertEqual(len(modules), len(set(modules)))
        self.assertTrue(validate_workspace(workspace).ok)
        prompt = (workspace / 'handoff/PROMPT.md').read_text(encoding='utf-8')
        self.assertIn('必须生成 1 个独立 Markdown', prompt)

    def test_other_modes_do_not_load_complete_or_extra_layers(self):
        modules = plan_modules(self.skill, modes=['deep'], format_profile='clean',
            use_slides_as_source=False, embed_slides=False)
        for mode in ('full', 'verbatim', 'complete'):
            self.assertNotIn(f'references/modes/{mode}.md', modules)
        workspace = self.prepare(name='deep-only', modes=['deep'])
        self.assertNotIn('## Complete 的分层约束', (workspace / 'handoff/PROMPT.md').read_text(encoding='utf-8'))

    def test_complete_and_full_remain_separate_selected_outputs(self):
        workspace = self.prepare(modes=['complete', 'full'])
        sources = self.sources(workspace)
        self.assertEqual(sources['modes'], ['full', 'complete'])
        self.assertEqual(set(sources['expected_outputs']), {'full', 'complete'})
        self.assertIn('handoff/skill/references/execution/multi-output.md', sources['skill']['modules'])

    def test_generated_note_basis_does_not_authorize_fictional_transcript_layer(self):
        workspace = self.prepare(transcript_origin='generated_note')
        prompt = (workspace / 'handoff/PROMPT.md').read_text(encoding='utf-8')
        self.assertEqual(self.sources(workspace)['transcript']['origin'], 'generated_note')
        self.assertIn('禁止生成或宣称逐字层', prompt)
        self.assertTrue(validate_workspace(workspace).ok)
        with self.assertRaises(ValueError):
            self.prepare(name='invalid-verbatim', modes=['verbatim'], transcript_origin='generated_note')

    def test_generated_policy_and_transport_keep_layout_and_layer_scope(self):
        workspace = self.prepare(slides_dir=self.slides, embed_slides=True)
        prompt = (workspace / 'handoff/PROMPT.md').read_text(encoding='utf-8')
        self.assertIn('slides/layout', prompt)
        layout = (workspace / 'handoff/skill/references/slides/layout.md').read_text(encoding='utf-8')
        bundle = create_manual_bundle(workspace)
        with zipfile.ZipFile(bundle.bundle) as archive:
            self.assertEqual(archive.read('handoff/PROMPT.md').decode('utf-8'), prompt)
            self.assertIn('handoff/skill/references/modes/complete.md', archive.namelist())
        web = create_web_handoff(workspace)
        self.assertEqual(set(web.expected_outputs), {'complete'})
        text = web.materials.read_text(encoding='utf-8')
        self.assertIn(layout, text)
        self.assertTrue(web.slides.is_file())

    def test_refresh_versions_preserve_sources_old_notes_and_snapshots(self):
        workspace = self.prepare(modes=['deep'])
        old_prompt = (workspace / 'handoff/PROMPT.md').read_bytes()
        source = (workspace / 'transcript/transcript.txt').read_bytes()
        old_note = workspace / 'notes/deep-001.md'
        old_note.write_text('# Prior user note\n\nKeep this.', encoding='utf-8')
        refresh_handoff(workspace, modes=['complete'])
        self.assertEqual(self.sources(workspace)['expected_outputs'], {'complete': 'notes/complete-001.md'})
        complete = workspace / 'notes/complete-001.md'
        complete.write_text('# Existing Complete\n\nPreserve this too.', encoding='utf-8')
        refresh_handoff(workspace, modes=['complete'])
        self.assertEqual(self.sources(workspace)['expected_outputs'], {'complete': 'notes/complete-002.md'})
        self.assertEqual((workspace / 'transcript/transcript.txt').read_bytes(), source)
        self.assertEqual(old_note.read_text(encoding='utf-8'), '# Prior user note\n\nKeep this.')
        self.assertEqual(complete.read_text(encoding='utf-8'), '# Existing Complete\n\nPreserve this too.')
        self.assertTrue(any(path.read_bytes() == old_prompt for path in (workspace / 'handoff/history').glob('*.md')))

    def test_cli_all_mode_selectors_accept_complete(self):
        parser = _build_parser()
        for args in (
            ['run', '--mode', 'complete'],
            ['prompt', '/placeholder/workspace', '--modes', 'complete'],
            ['prepare', '--lecture-id', 'synthetic', '--course-title', 'Synthetic', '--transcript', '/placeholder/source.txt', '--mode', 'complete'],
            ['revise', '/placeholder/note.md', '--lecture-id', 'synthetic', '--mode', 'complete'],
            ['import-note', '/placeholder/workspace', '/placeholder/returned.md', '--task-id', 'synthetic', '--mode', 'complete'],
        ):
            with self.subTest(command=args[0]):
                parsed = parser.parse_args(args)
                self.assertIn('complete', parsed.modes if getattr(parsed, 'modes', None) else [parsed.mode])

    def test_tui_complete_selection_preferences_and_workspace_reload(self):
        cfg = {'notes_deep': False, 'notes_full': False, 'notes_summary': False,
               'notes_complete': True, 'verbatim_transcript': False, 'use_slides': False}
        self.assertEqual(selected_modes(cfg), ['complete'])
        args = build_cli_args({**cfg, 'kind': 'product', 'output_dir': str(self.root / 'output')})
        self.assertEqual(args[args.index('--modes') + 1:args.index('--format-profile')], ['complete'])
        self.assertEqual(_safe_preferences({'notes_complete': True, 'unknown': 'drop'}), {'notes_complete': True})
        state = {}
        _load_workspace_into_state(state, str(self.prepare()))
        self.assertTrue(state['notes_complete'])
        self.assertFalse(state['notes_deep'])
        self.assertEqual(selected_modes(state), ['complete'])

    def test_returned_complete_import_is_task_bound_and_does_not_create_other_modes(self):
        workspace = self.prepare()
        returned = self.root / 'returned.md'
        returned.write_text('# 合成完整讲义\n\n## 问题\n\n先说明问题与限制。\n\n'
                            '<details class="complete-detail">\n<summary>课堂细节：提问</summary>\n\n'
                            '只补充本单元实际已有的信息。\n\n</details>\n', encoding='utf-8')
        task = plan_task(workspace)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = main(['import-note', str(workspace), str(returned), '--mode', 'complete',
                         '--task-id', task.task_id, '--no-html'])
        self.assertEqual(code, 0)
        self.assertTrue(validate_note_output(workspace).ok)
        self.assertEqual(set(plan_task(workspace).completed), {'complete'})
        self.assertEqual([path.name for path in (workspace / 'notes').glob('*.md')], ['complete-001.md'])
        self.assertEqual(returned.read_bytes(), (workspace / 'notes/complete-001.md').read_bytes())

    def test_delivery_rules_keep_application_io_separate_from_writing(self):
        workspace = self.prepare()
        rules = (workspace / 'handoff/skill/references/common/delivery.md').read_text(encoding='utf-8')
        self.assertIn('expected_outputs', rules)
        self.assertEqual(self.sources(workspace)['expected_outputs']['complete'], 'notes/complete-001.md')
