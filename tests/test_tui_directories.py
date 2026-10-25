import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from handouter import tui


class TUIDirectoryTests(unittest.TestCase):
    def test_relative_asset_uses_saved_input_and_metadata_date(self):
        state = {'asset': 'lesson.zip', 'input_dir': '/synthetic/input'}
        directories = {'input_dir': '/synthetic/input', 'output_dir': '/synthetic/default', 'course_output_dirs': {}}
        with patch.object(tui, 'resolve_input_source', return_value=Path('/synthetic/input/lesson.zip')) as resolve, patch.object(tui, 'inspect_input_source', return_value=SimpleNamespace(course_title='Course', lecture_id='id', lecture_date='2026-01-02', source_kind='zhiyun_asset_zip', has_slides=True)), patch.object(tui, 'load_directory_settings', return_value=directories):
            tui._load_asset_into_state(state)
        resolve.assert_called_once_with('/synthetic/input', 'lesson.zip')
        self.assertEqual(state['lecture_date'], '2026-01-02')

    def test_reload_same_asset_keeps_user_confirmed_date(self):
        state = {'asset': '/synthetic.zip', 'lecture': 'id', 'lecture_date': '2026-02-03'}
        directories = {'input_dir': '/synthetic/input', 'output_dir': '/synthetic/default', 'course_output_dirs': {}}
        with patch.object(tui, 'resolve_input_source', return_value=Path('/synthetic.zip')), patch.object(tui, 'inspect_input_source', return_value=SimpleNamespace(course_title='Course', lecture_id='id', lecture_date=None, source_kind='zhiyun_asset_zip', has_slides=True)), patch.object(tui, 'load_directory_settings', return_value=directories):
            tui._load_asset_into_state(state)
        self.assertEqual(state['lecture_date'], '2026-02-03')

    def test_reload_same_asset_keeps_user_title_id_and_date(self):
        state = {'asset': '/synthetic.zip', '_identified_asset': str(Path('/synthetic.zip')),
                 'title': '用户课程名', 'lecture': 'user-id', 'lecture_date': '2026-02-03'}
        directories = {'input_dir': '/synthetic/input', 'output_dir': '/synthetic/default', 'course_output_dirs': {}}
        with patch.object(tui, 'resolve_input_source', return_value=Path('/synthetic.zip')), \
             patch.object(tui, 'inspect_input_source', return_value=SimpleNamespace(course_title='Detected', lecture_id='auto-id', lecture_date=None, source_kind='zhiyun_asset_zip', has_slides=True)), \
             patch.object(tui, 'load_directory_settings', return_value=directories):
            tui._load_asset_into_state(state)
        self.assertEqual((state['title'], state['lecture'], state['lecture_date']), ('用户课程名', 'user-id', '2026-02-03'))

    def test_loading_alias_title_canonicalizes_course_and_uses_shared_output(self):
        from handouter.settings import course_settings_key
        key = course_settings_key('高级数据结构')
        directories = {'input_dir': '/synthetic/input', 'output_dir': '/synthetic/default',
                       'course_output_dirs': {key: {'course_title': '高级数据结构', 'output_dir': '/synthetic/ADS'}},
                       'course_aliases': {key: {'course_title': '高级数据结构', 'aliases': ['ADS']}},
                       'course_folder_modes': {key: {'course_title': '高级数据结构', 'mode': 'auto'}}}
        state = {'asset': '/synthetic.zip', 'input_dir': '/synthetic/input', '_output_dir_manual': False}
        with patch.object(tui, 'resolve_input_source', return_value=Path('/synthetic.zip')), \
             patch.object(tui, 'inspect_input_source', return_value=SimpleNamespace(course_title='ADS', lecture_id='id', lecture_date=None, source_kind='zhiyun_asset_zip', has_slides=True)), \
             patch.object(tui, 'load_directory_settings', return_value=directories):
            tui._load_asset_into_state(state)
        self.assertEqual(state['title'], '高级数据结构')
        self.assertEqual(state['output_dir'], '/synthetic/ADS')
        self.assertTrue(state['course_output_configured'])

    def test_loading_course_applies_saved_course_output_override(self):
        state = {'asset': '/synthetic.zip', 'input_dir': '/synthetic/input',
                 'output_dir': '/synthetic/default', '_output_dir_manual': False}
        directories = {'input_dir': '/synthetic/input', 'output_dir': '/synthetic/default',
                       'course_output_dirs': {'key': {'course_title': 'Course', 'output_dir': '/synthetic/course'}}}
        with patch.object(tui, 'resolve_input_source', return_value=Path('/synthetic.zip')), \
             patch.object(tui, 'inspect_input_source', return_value=SimpleNamespace(course_title='Course', lecture_id='id', lecture_date=None, source_kind='zhiyun_asset_zip', has_slides=True)), \
             patch.object(tui, 'load_directory_settings', return_value=directories), \
             patch.object(tui, 'configured_course_output', return_value='/synthetic/course'), \
             patch.object(tui, 'resolve_output_directory', return_value='/synthetic/course'):
            tui._load_asset_into_state(state)
        self.assertEqual(state['output_dir'], '/synthetic/course')
        self.assertTrue(state['course_output_configured'])

    def test_manual_output_choice_is_not_replaced_when_course_is_loaded(self):
        state = {'asset': '/synthetic.zip', 'input_dir': '/synthetic/input',
                 'output_dir': '/one-shot', '_output_dir_manual': True}
        directories = {'input_dir': '/synthetic/input', 'output_dir': '/synthetic/default',
                       'course_output_dirs': {}, 'course_aliases': {}, 'course_folder_modes': {}}
        with patch.object(tui, 'resolve_input_source', return_value=Path('/synthetic.zip')), \
             patch.object(tui, 'inspect_input_source', return_value=SimpleNamespace(course_title='Course', lecture_id='id', lecture_date=None, source_kind='zhiyun_asset_zip', has_slides=True)), \
             patch.object(tui, 'load_directory_settings', return_value=directories):
            tui._load_asset_into_state(state)
        self.assertEqual(state['output_dir'], '/one-shot')

    def test_date_is_passed_and_validated(self):
        cfg = {'kind': 'product', 'lecture': 'id', 'title': 'Course', 'asset': '/synthetic.zip', 'lecture_date': '2026-01-02'}
        self.assertIn('--lecture-date', tui.build_cli_args(cfg))
        with self.assertRaises(ValueError):
            tui._commit_text_value(cfg, 'lecture_date', '2026-02-30')
        self.assertEqual(cfg['lecture_date'], '2026-01-02')

    def test_workspace_lookup_and_prompt_use_organized_workspace(self):
        cfg = {'output_dir': '/synthetic/output', 'lecture': 'id', 'kind': 'prompt'}
        inner = Path('/synthetic/output/Course_2026-01-02/资料与交接/id')
        with patch.object(tui, 'find_workspace', return_value=inner):
            self.assertEqual(tui._workspace_for_state(cfg), inner)
            self.assertEqual(tui.build_cli_args(cfg)[1], str(inner))

    def test_directory_dialog_requires_explicit_save_and_cancels_without_write(self):
        screen = MagicMock()
        screen.getmaxyx.return_value = (32, 112)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        state = {'input_dir': str(Path(temporary.name) / 'in'), 'output_dir': str(Path(temporary.name) / 'out')}
        loaded = {'input_dir': state['input_dir'], 'output_dir': state['output_dir'], 'course_output_dirs': {}, 'configured': False}
        with patch.object(tui, 'load_directory_settings', return_value=loaded), patch.object(tui, 'save_directory_settings') as save:
            screen.getch.side_effect = [27]
            self.assertFalse(tui._directory_dialog(screen, state))
            save.assert_not_called()
            screen.getch.side_effect = [9, 9, 10]
            save.return_value = {**state, 'course_output_dirs': {}, 'configured': True}
            self.assertTrue(tui._directory_dialog(screen, state))
            save.assert_called_once_with(state['input_dir'], state['output_dir'])
        self.assertTrue(state['directories_configured'])
        self.assertTrue(Path(state['input_dir']).is_dir())

    def test_open_course_package_retains_true_output_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary).resolve()
            package = output / 'Course_2026-01-02'
            workspace = package / '资料与交接' / 'stable-id'
            (workspace / 'handoff').mkdir(parents=True)
            (package / '.handouter-collection.json').write_text(json.dumps({'lecture_date': '2026-01-02'}))
            (workspace / 'handoff/sources.json').write_text(json.dumps({'course_title': 'Course', 'lecture_id': 'stable-id', 'options': {}, 'modes': ['deep']}))
            with patch.object(tui, 'resolve_workspace', return_value=workspace), patch.object(tui, 'package_for_workspace', return_value=package):
                state = {}
                tui._load_workspace_into_state(state, str(package))
            self.assertEqual(state['output_dir'], str(output))
            self.assertEqual(state['lecture'], 'stable-id')
