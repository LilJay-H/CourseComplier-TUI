"""Chinese (default) / English TUI wording, persistence and 94x30 layout."""
import re
import string
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from handouter import tui, tui_text


def _fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


class TUILanguageTests(unittest.TestCase):
    def tearDown(self):
        tui.set_language('zh')

    def test_chinese_is_default_and_every_key_has_both_languages(self):
        self.assertEqual(tui_text.DEFAULT_LANGUAGE, 'zh')
        self.assertEqual(tui_text.normalize_language('fr'), 'zh')
        for key, (zh, en) in tui_text._TEXT.items():
            with self.subTest(key=key):
                self.assertTrue(zh.strip() and en.strip())
                self.assertEqual(_fields(zh), _fields(en))

    def test_hotkey_letters_stay_the_same_in_chinese(self):
        for key, letter in (('button_workspace', 'W'), ('button_results', 'R'), ('button_import', 'I'),
                            ('button_html', 'H'), ('button_prompt', 'P'), ('button_quit', 'Q')):
            self.assertIn(letter, tui_text.text(key, 'zh'))
            self.assertIn(letter, tui_text.text(key, 'en'))

    def test_language_preference_round_trips_and_rejects_unknown_values(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory)
            tui._save_preferences({'language': 'en', 'agent': 'web'}, storage)
            self.assertEqual(tui._load_preferences(storage)['language'], 'en')
        self.assertNotIn('language', tui._safe_preferences({'language': 'fr'}))

    def _render(self, preferences: dict, keys: list[int], size=(30, 94)):
        screen = MagicMock()
        screen.getmaxyx.return_value = size
        screen.getch.side_effect = keys
        saved = []
        with patch('curses.wrapper', side_effect=lambda callback: callback(screen)), patch('curses.curs_set'), \
             patch.object(tui, '_load_preferences', return_value=dict(preferences)), \
             patch.object(tui, '_save_preferences', side_effect=lambda state, *a: saved.append(dict(state))), \
             patch.object(tui, 'load_directory_settings', return_value={
                 'input_dir': r'C:\输入', 'output_dir': r'C:\输出', 'course_output_dirs': {}, 'configured': True}):
            tui._run_curses()
        return screen, saved

    def test_main_screen_fits_94_columns_in_both_languages(self):
        for language, title in (('zh', '课程讲义编译器'), ('en', 'COURSE COMPILER')):
            with self.subTest(language=language):
                screen, _saved = self._render({'language': language}, [353, 353, ord('q')])
                drawn = [c.args for c in screen.addstr.call_args_list]
                self.assertTrue(any(title in args[2] for args in drawn))
                for row, col, text, *_ in drawn:
                    self.assertLessEqual(col + tui._display_width(text), 93, (row, text))

    def test_chinese_screen_shows_chinese_labels_and_keeps_buttons_on_their_rows(self):
        screen, _saved = self._render({}, [353, 353, ord('q')])
        rows = {}
        for row, col, text, *_ in (c.args for c in screen.addstr.call_args_list):
            rows.setdefault(row, []).append((col, text))
        joined = {row: ''.join(text for _, text in sorted(items)) for row, items in rows.items()}
        self.assertIn('资料文件', joined[5])
        self.assertIn('界面语言', joined[12])
        self.assertIn('中文', joined[12])
        self.assertIn('[ 运行 F5 ]', joined[21])
        self.assertIn('[ W 打开已有讲次 ]', joined[22])
        self.assertIn('[ H 导出 HTML ]', joined[22])
        self.assertIn('目录设置', joined[27])

    def test_language_option_switches_and_is_saved(self):
        # Tab order: 8 source/deliverable rows, Complete, 6 options, then Language.
        screen, saved = self._render({}, [*[9] * 15, ord(' '), ord('q')])
        self.assertEqual(saved[0]['language'], 'en')
        later = [c.args[2] for c in screen.addstr.call_args_list]
        self.assertTrue(any('COURSE COMPILER' in text for text in later))

    def test_result_rows_follow_the_language(self):
        workspace = Path(tempfile.mkdtemp())
        plan = MagicMock(action='partial', task_id='t1', completed={}, pending={'deep': 'x'}, errors=(), warnings=(),
                         quality={})
        with patch('handouter.tasks.plan_task', return_value=plan), \
             patch.object(tui, 'read_bytes', side_effect=[b'{}', b'{"expected_outputs": {"deep": "notes/a.md"}}']), \
             patch.object(tui, 'package_for_workspace', return_value=None):
            tui.set_language('zh')
            rows = '\n'.join(tui._result_rows(workspace))
        self.assertIn('下一步', rows)
        self.assertIn('待生成', rows)
        self.assertFalse(re.search(r'\bNext\b', rows))


if __name__ == '__main__':
    unittest.main()
