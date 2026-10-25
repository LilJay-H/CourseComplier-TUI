"""Synthetic planner/UI regressions, without an interactive terminal or models."""
from collections import deque
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from handouter import tui
from handouter.service import prepare_lecture


class TUIWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        transcript = self.root / 'lecture.txt'
        transcript.write_text('合成课程')
        self.workspace = prepare_lecture(self.root / 'out', lecture_id='course', course_title='合成',
                                         transcript=transcript, use_slides_as_source=False).workspace.resolve()
        self.cfg = {'output_dir': str(self.workspace.parent), 'lecture': 'course',
                    'notes_deep': True, 'use_slides': False, 'embed_slides': False,
                    'allow_web': False, 'format_profile': 'clean', 'agent': 'codex'}

    def planner(self, action):
        plan = SimpleNamespace(action=action, completed={'deep': 'notes/deep-001.md'},
                               pending={}, errors=(), warnings=(), task_id='synthetic-task')
        return patch.dict(sys.modules, {'handouter.tasks': SimpleNamespace(plan_task=lambda *args, **kwargs: plan)})

    def test_completed_run_shows_results_without_agent_rerun(self):
        with self.planner('complete'):
            args, action = tui._task_cli_args(self.cfg, prompt_only=False)
        self.assertEqual(args, ['task-status', str(self.workspace)])
        self.assertEqual(action, 'complete')

    def test_html_retry_and_partial_continuation_use_canonical_cli(self):
        with self.planner('html_only'):
            self.assertEqual(tui._task_cli_args(self.cfg, prompt_only=False)[0], ['retry-html', str(self.workspace)])
        with self.planner('partial'):
            self.assertEqual(tui._task_cli_args(self.cfg, prompt_only=False)[0],
                             ['continue', str(self.workspace), '--agent', 'codex'])

    def test_selection_change_requires_explicit_refresh(self):
        changed = {**self.cfg, 'notes_summary': True}
        with self.planner('refresh_required'):
            with self.assertRaisesRegex(ValueError, 'P|刷新'):
                tui._task_cli_args(changed, prompt_only=False)
        self.assertEqual(tui._task_cli_args(changed, prompt_only=True)[0][0], 'prompt')

    def test_existing_workspace_entry_does_not_require_asset(self):
        self.assertTrue(hasattr(tui, '_load_workspace_into_state'))
        state = {'agent': 'web'}
        tui._load_workspace_into_state(state, str(self.workspace))
        self.assertEqual(tui._workspace_for_state(state), self.workspace)
        self.assertFalse(state.get('asset'))
        self.assertTrue(state['notes_deep'])
        self.assertFalse(state['use_slides'])

    def test_printable_shortcut_letters_start_text_editor_first(self):
        screen = MagicMock()
        screen.getmaxyx.return_value = (32, 112)
        for char in 'HNGPQ':
            with self.subTest(char=char), patch('curses.wrapper', side_effect=lambda callback: callback(screen)), \
                 patch('curses.curs_set'), patch.object(tui, 'available_agent_choices', return_value=(['web'], {'web': 'Web'})), \
                 patch.object(tui, '_load_preferences', return_value={}, create=True), \
                 patch.object(tui, '_edit_value', side_effect=RuntimeError('stop-at-editor')) as edit:
                screen.getch.side_effect = [ord(char), RuntimeError('stop-no-editor')]
                with self.assertRaisesRegex(RuntimeError, 'stop-at-editor'):
                    tui._run_curses()
                self.assertEqual(edit.call_args.args[1], 'Source File')
                self.assertEqual(edit.call_args.kwargs.get('initial'), char)

    def test_preferences_are_local_allowlisted_and_round_trip(self):
        self.assertTrue(hasattr(tui, '_save_preferences'))
        storage = self.root / 'preferences'
        state = {**self.cfg, 'asset': 'https://example.invalid/?auth_key=synthetic-secret',
                 'source_note': 'private-note.md', 'cookie': 'synthetic-secret', 'title': 'private transcript'}
        tui._save_preferences(state, storage)
        serialized = (storage / '.handouter-preferences.json').read_text()
        self.assertNotIn('synthetic-secret', serialized)
        self.assertNotIn('private-note', serialized)
        self.assertNotIn('private transcript', serialized)
        loaded = tui._load_preferences(storage)
        self.assertEqual(loaded['output_dir'], state['output_dir'])
        self.assertEqual(loaded['lecture'], 'course')
        self.assertTrue(loaded['notes_deep'])

    def test_logs_are_bounded_and_do_not_echo_model_or_credentials(self):
        self.assertTrue(hasattr(tui, '_append_task_log'))
        lines = deque(maxlen=200)
        for index in range(300):
            tui._append_task_log(lines, '[progress] step ' + str(index))
        tui._append_task_log(lines, 'entire model body should never become diagnostics')
        tui._append_task_log(lines, 'handouter: error: auth_key=synthetic-secret')
        tui._append_task_log(lines, 'course-compiler: error: auth_key=synthetic-secret')
        self.assertLessEqual(len(lines), 200)
        self.assertNotIn('synthetic-secret', '\n'.join(lines))
        self.assertNotIn('entire model body', '\n'.join(lines))

    def test_current_and_legacy_cli_errors_remain_visible_without_credentials(self):
        lines = deque(maxlen=8)
        tui._append_task_log(lines, 'handouter: error: old failure')
        tui._append_task_log(lines, 'course-compiler: error: new failure')
        self.assertEqual(list(lines), ['course-compiler: error: old failure',
                                       'course-compiler: error: new failure'])
        self.assertEqual(tui._task_error_summary(list(lines), 2), 'course-compiler: error: new failure')
        tui._append_task_log(lines, 'course-compiler: error: auth_key=synthetic-secret')
        self.assertEqual(lines[-1], 'course-compiler: error: diagnostic contained credential-like data; details hidden')

    def test_chinese_columns_stay_inside_panel_and_resize(self):
        self.assertTrue(hasattr(tui, '_panel_add'))
        for width in (20, 40, 112):
            screen = MagicMock()
            screen.getmaxyx.return_value = (32, width)
            tui._panel_add(screen, 4, 3, min(width - 2, 18), '中文课程标题' * 20)
            row, col, text, _attr = screen.addstr.call_args.args
            self.assertLessEqual(col + tui._display_width(text), min(width - 2, 18))

    def test_result_rows_separate_modes_html_and_uploads(self):
        self.assertTrue(hasattr(tui, '_result_rows'))
        with self.planner('partial'):
            rows = tui._result_rows(self.workspace)
        text = '\n'.join(rows)
        self.assertIn('Markdown', text)
        self.assertIn('HTML', text)
        self.assertIn('Uploads', text)
        self.assertIn('Next', text)

    def test_edit_paste_chinese_cursor_and_resize_preserve_buffer(self):
        import curses
        screen = MagicMock()
        dimensions = [(32, 112)]
        screen.getmaxyx.side_effect = lambda: dimensions[0]
        keys = iter(['中', '文', curses.KEY_LEFT, 'A', curses.KEY_RESIZE, '\n'])
        def key():
            value = next(keys)
            if value == 'A':
                dimensions[0] = (1, 4)
            elif value == curses.KEY_RESIZE:
                dimensions[0] = (32, 112)
            return value
        screen.get_wch.side_effect = key
        with patch('curses.curs_set'):
            value, committed = tui._edit_value(screen, 'Asset ZIP', '', initial='/tmp/')
        self.assertTrue(committed)
        self.assertEqual(value, '/tmp/中A文')

    def test_f5_continue_workspace_never_reloads_zip(self):
        import curses
        screen = MagicMock()
        screen.getmaxyx.return_value = (32, 112)
        screen.getch.side_effect = [curses.KEY_F5, ord('q')]
        with patch('curses.wrapper', side_effect=lambda callback: callback(screen)), patch('curses.curs_set'), \
             patch.object(tui, '_load_preferences', return_value={**self.cfg, 'workspace': str(self.workspace)}), \
             patch.object(tui, '_save_preferences'), patch.object(tui, '_load_asset_into_state') as asset, \
             patch.object(tui, '_run_task', return_value=(True, 'done')) as run, \
             patch.object(tui, '_show_results') as results:
            tui._run_curses()
        asset.assert_not_called()
        run.assert_called_once()
        results.assert_called_once_with(screen, self.workspace)

    def test_visible_workspace_button_loads_workspace_and_runs_without_zip(self):
        import curses
        screen = MagicMock()
        screen.getmaxyx.return_value = (32, 112)
        # Complete adds one visible deliverable before the existing option/actions.
        screen.getch.side_effect = [*[9] * 19, 10, curses.KEY_F5, ord('q')]
        with patch('curses.wrapper', side_effect=lambda callback: callback(screen)), patch('curses.curs_set'), \
             patch.object(tui, '_load_preferences', return_value={}), patch.object(tui, '_save_preferences'), \
             patch.object(tui, '_edit_value', return_value=(str(self.workspace), True)), \
             patch.object(tui, '_load_asset_into_state') as asset, \
             patch.object(tui, '_run_task', return_value=(True, 'done')) as run, patch.object(tui, '_show_results'):
            tui._run_curses()
        asset.assert_not_called()
        self.assertEqual(run.call_args.args[1]['workspace'], str(self.workspace))

    def test_complete_toggle_follows_verbatim_in_visible_focus_order(self):
        for key in (10, ord(' ')):
            with self.subTest(key=key):
                screen = MagicMock()
                screen.getmaxyx.return_value = (32, 112)
                screen.getch.side_effect = [*[9] * 8, key, ord('q')]
                with patch('curses.wrapper', side_effect=lambda callback: callback(screen)), patch('curses.curs_set'), \
                     patch.object(tui, '_load_preferences', return_value={}), \
                     patch.object(tui, '_save_preferences') as save, \
                     patch.object(tui, '_run_task') as run:
                    tui._run_curses()
                self.assertTrue(save.call_args.args[0]['notes_complete'])
                run.assert_not_called()

    def test_import_button_binds_explicit_mode_and_current_task_id(self):
        from handouter.tasks import plan_task
        task_id = plan_task(self.workspace).task_id
        returned = self.root / 'returned.md'
        returned.write_text('# 合成返回稿')
        screen = MagicMock()
        screen.getmaxyx.return_value = (32, 112)
        screen.getch.side_effect = [*[9] * 4, ord('i'), ord('q')]
        with patch('curses.wrapper', side_effect=lambda callback: callback(screen)), patch('curses.curs_set'), \
             patch.object(tui, '_load_preferences', return_value={**self.cfg, 'workspace': str(self.workspace)}), \
             patch.object(tui, '_save_preferences'), \
             patch.object(tui, '_edit_value', side_effect=[('deep', True), (str(returned), True)]), \
             patch.object(tui, '_run_task', return_value=(True, 'done')) as run, patch.object(tui, '_show_results'):
            tui._run_curses()
        command = run.call_args.kwargs['command']
        self.assertEqual(command, ['import-note', str(self.workspace), str(returned), '--mode', 'deep', '--task-id', task_id])

    def test_malformed_preferences_do_not_change_field_types(self):
        storage = self.root / 'preferences'
        storage.mkdir()
        (storage / '.handouter-preferences.json').write_text(json.dumps({
            'notes_deep': 'false', 'agent': 'untrusted', 'format_profile': 'unknown',
            'output_dir': 'https://example.invalid/?token=synthetic-secret'}))
        self.assertEqual(tui._load_preferences(storage), {})

    def test_unchanged_submitted_revision_continues_without_prompt_refresh(self):
        from handouter.service import refresh_handoff
        note = self.root / 'submitted.md'
        note.write_text('# 已有讲义\n\n有条件的结论。')
        refresh_handoff(self.workspace, source_note=note, modes=['deep'])
        before = (self.workspace / 'handoff/PROMPT.md').read_bytes()
        cfg = {**self.cfg, 'source_note': str(note), 'revision_action': 'derive'}
        args, action = tui._task_cli_args(cfg, prompt_only=False)
        self.assertEqual(args, ['continue', str(self.workspace), '--agent', 'codex'])
        self.assertEqual(action, 'continue')
        self.assertEqual((self.workspace / 'handoff/PROMPT.md').read_bytes(), before)
        self.assertEqual(tui._task_cli_args(cfg, prompt_only=True)[0][0], 'prompt')
        note.write_text('# Explicitly changed submitted note')
        self.assertEqual(tui._task_cli_args(cfg, prompt_only=False)[0][0], 'prompt')

    def test_missing_note_can_continue_explicit_workspace_snapshot(self):
        from handouter.service import refresh_handoff
        note = self.root / 'submitted.md'
        note.write_text('# 已提交的合成讲义')
        refresh_handoff(self.workspace, source_note=note, modes=['deep'])
        note.unlink()
        cfg = {**self.cfg, 'source_note': str(note), 'revision_action': 'derive'}
        with self.assertRaisesRegex(ValueError, 'W.*快照'):
            tui._task_cli_args(cfg, prompt_only=False)
        tui._load_workspace_into_state(cfg, str(self.workspace))
        args, action = tui._task_cli_args(cfg, prompt_only=False)
        self.assertEqual(args[0], 'continue')
        self.assertFalse(cfg['source_note'])
        sources = json.loads((self.workspace / 'handoff/sources.json').read_text())
        self.assertIn('已提交的合成讲义', sources['revision']['text'])

    def test_revision_action_change_remains_explicit_and_illustrate_is_single_mode(self):
        from handouter.service import refresh_handoff
        note = self.root / 'submitted.md'
        note.write_text('# 已提交的合成讲义')
        refresh_handoff(self.workspace, source_note=note, modes=['deep'])
        cfg = {**self.cfg, 'source_note': str(note), 'revision_action': 'illustrate'}
        self.assertEqual(tui._task_cli_args(cfg, prompt_only=False)[0][0], 'prompt')
        cfg['notes_summary'] = True
        with self.assertRaisesRegex(ValueError, '一个|单'):
            tui._task_cli_args(cfg, prompt_only=False)

    def test_materials_not_claimed_prepared_when_busy_or_invalid(self):
        for action in ('busy', 'invalid', 'recovery_required'):
            with self.subTest(action=action), self.planner(action):
                materials = next(row for row in tui._result_rows(self.workspace) if row.startswith('Materials:'))
                self.assertNotIn('prepared', materials)
