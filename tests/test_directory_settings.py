import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from handouter import platform_support
from handouter import settings


class DirectorySettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = self.root / 'config' / 'directories.json'
        self.patchers = [patch.object(settings, 'directory_settings_path', return_value=self.config),
                         patch.object(settings, 'desktop_directory', return_value=self.root / 'Desktop')]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_defaults_do_not_create_any_directories(self):
        result = settings.load_directory_settings()
        self.assertEqual(result, {'input_dir': str(self.root / 'Desktop/Handouter/输入'),
                                 'output_dir': str(self.root / 'Desktop/Handouter/输出'),
                                 'course_output_dirs': {}, 'course_aliases': {},
                                 'course_folder_modes': {}, 'configured': False})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_save_and_load_only_creates_configuration(self):
        incoming, outgoing = self.root / '输入', self.root / '输出'
        settings.save_directory_settings(incoming, outgoing)
        self.assertEqual(settings.load_directory_settings(), {
            'input_dir': str(incoming), 'output_dir': str(outgoing),
            'course_output_dirs': {}, 'course_aliases': {},
            'course_folder_modes': {}, 'configured': True})
        self.assertFalse(incoming.exists())
        self.assertFalse(outgoing.exists())
        self.assertEqual(set(json.loads(self.config.read_text())),
                         {'schema_version', 'input_dir', 'output_dir', 'course_output_dirs',
                          'course_aliases', 'course_folder_modes'})

    def test_old_two_field_settings_remain_compatible(self):
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({'input_dir': str(self.root / 'old-in'),
                                           'output_dir': str(self.root / 'old-out')}), encoding='utf-8')
        self.assertEqual(settings.load_directory_settings()['output_dir'], str(self.root / 'old-out'))
        self.assertEqual(settings.load_directory_settings()['course_output_dirs'], {})

    def test_default_output_can_change_without_respecifying_input(self):
        incoming = self.root / 'input'
        settings.save_directory_settings(incoming, self.root / 'first-output')
        result = settings.save_directory_settings(output_dir=self.root / 'second-output')
        self.assertEqual(result['input_dir'], str(incoming))
        self.assertEqual(result['output_dir'], str(self.root / 'second-output'))
        self.assertEqual(settings.resolve_output_directory('Any Course'), str(self.root / 'second-output'))

    def test_course_output_override_precedence_matching_and_clear(self):
        settings.save_directory_settings(self.root / 'input', self.root / 'default-output')
        saved = settings.save_course_output_directory(' 数据 结构 ', self.root / 'course-output')
        self.assertEqual(settings.configured_course_output('数据   结构', settings=saved), str(self.root / 'course-output'))
        self.assertEqual(settings.configured_course_output('数据 结构'.upper(), settings=saved), str(self.root / 'course-output'))
        self.assertEqual(settings.resolve_output_directory('数据 结构', settings=saved), str(self.root / 'course-output'))
        self.assertEqual(settings.resolve_output_directory('数据 结构', explicit=self.root / 'one-shot', settings=saved),
                         str((self.root / 'one-shot').resolve()))
        cleared = settings.clear_course_output_directory('数据 结构')
        self.assertIsNone(settings.configured_course_output('数据 结构', settings=cleared))
        self.assertEqual(settings.resolve_output_directory('数据 结构', settings=cleared), str(self.root / 'default-output'))

    def test_aliases_resolve_to_one_canonical_course_and_share_output(self):
        settings.save_directory_settings(self.root / 'input', self.root / 'default-output')
        saved = settings.save_course_aliases('计算机组成', ['计组', 'Computer Organization'])
        saved = settings.save_course_output_directory('计组', self.root / 'course-output')
        self.assertEqual(settings.resolve_course_title('计组', settings=saved), '计算机组成')
        self.assertEqual(settings.resolve_course_title('computer organization', settings=saved), '计算机组成')
        self.assertEqual(settings.aliases_for_course('计算机组成', settings=saved), ['计组', 'Computer Organization'])
        self.assertEqual(settings.configured_course_output('计组', settings=saved), str(self.root / 'course-output'))

    def test_alias_collision_and_folder_modes_are_explicit(self):
        settings.save_directory_settings(self.root / 'input', self.root / 'default-output')
        saved = settings.save_course_aliases('高级数据结构', ['ADS'])
        saved = settings.save_course_folder_mode('ADS', 'never')
        self.assertEqual(settings.course_folder_mode('高级数据结构', settings=saved), 'never')
        with self.assertRaises(ValueError):
            settings.save_course_aliases('另一门课', ['ads'])
        with self.assertRaises(ValueError):
            settings.save_course_folder_mode('高级数据结构', 'sometimes')

    def test_v2_settings_migrate_on_next_save(self):
        self.config.parent.mkdir(parents=True)
        key = settings.course_settings_key('Course')
        self.config.write_text(json.dumps({'schema_version': 2, 'input_dir': str(self.root / 'in'),
            'output_dir': str(self.root / 'out'), 'course_output_dirs': {
                key: {'course_title': 'Course', 'output_dir': str(self.root / 'course')}}}), encoding='utf-8')
        loaded = settings.load_directory_settings()
        self.assertEqual(loaded['course_aliases'], {})
        settings.save_course_aliases('Course', ['C'])
        self.assertEqual(json.loads(self.config.read_text())['schema_version'], 3)

    def test_course_output_cannot_overlap_input(self):
        settings.save_directory_settings(self.root / 'input', self.root / 'default-output')
        for output in (self.root / 'input', self.root / 'input/course', self.root):
            with self.subTest(output=output), self.assertRaises(ValueError):
                settings.save_course_output_directory('Course', output)

    def test_directories_cli_sets_default_and_course_output_independently(self):
        import contextlib
        import io
        from handouter import cli
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['directories', '--input-dir', str(self.root / 'input'),
                                       '--output-dir', str(self.root / 'default')]), 0)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['directories', '--course-title', 'Course A',
                                       '--course-output-dir', str(self.root / 'course-a')]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload['course_output_dir'], str(self.root / 'course-a'))
        self.assertEqual(payload['effective_output_dir'], str(self.root / 'course-a'))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['directories', '--course-title', 'Course A', '--clear-course-output']), 0)
        self.assertEqual(json.loads(output.getvalue())['effective_output_dir'], str(self.root / 'default'))

    def test_directories_cli_alias_and_folder_policy_round_trip(self):
        import contextlib
        import io
        from handouter import cli
        settings.save_directory_settings(self.root / 'input', self.root / 'default')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(['directories', '--course-title', '计算机组成', '--course-alias', '计组',
                             '--course-alias', 'CO', '--course-folder-name', 'auto',
                             '--course-output-dir', str(self.root / '计组')])
        self.assertEqual(code, 0, output.getvalue())
        payload = json.loads(output.getvalue())
        self.assertEqual(payload['course_title'], '计算机组成')
        self.assertEqual(payload['course_aliases_for_title'], ['计组', 'CO'])
        self.assertEqual(payload['course_folder_name'], 'auto')
        self.assertEqual(payload['effective_output_dir'], str(self.root / '计组'))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['directories', '--course-title', '计组']), 0)
        self.assertEqual(json.loads(output.getvalue())['course_title'], '计算机组成')

    def test_run_uses_course_override_when_output_is_not_explicit(self):
        import contextlib
        import io
        from handouter import cli
        source = self.root / 'lecture.txt'
        source.write_text('Synthetic lecture.', encoding='utf-8')
        settings.save_directory_settings(self.root / 'input', self.root / 'default')
        settings.save_course_output_directory('Course A', self.root / 'course-a')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(['run', '--transcript', str(source), '--course-title', 'Course A',
                             '--lecture-id', 'lecture-a', '--ignore-slides', '--agent', 'none'])
        self.assertEqual(code, 0, output.getvalue())
        payload = json.loads(output.getvalue())
        self.assertEqual(payload['output_directory'], str(self.root / 'course-a'))
        self.assertTrue(Path(payload['workspace']).is_relative_to(self.root / 'course-a'))

    def test_run_alias_uses_canonical_course_and_auto_avoids_duplicate_folder_name(self):
        import contextlib
        import io
        from handouter import cli
        source = self.root / 'lecture-alias.txt'
        source.write_text('Synthetic lecture.', encoding='utf-8')
        settings.save_directory_settings(self.root / 'input', self.root / 'default')
        settings.save_course_aliases('计算机组成', ['计组'])
        settings.save_course_output_directory('计组', self.root / '计组')
        settings.save_course_folder_mode('计算机组成', 'auto')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(['run', '--transcript', str(source), '--course-title', '计组',
                             '--lecture-id', 'alias-lecture', '--lecture-date', '2026-09-17',
                             '--ignore-slides', '--agent', 'none'])
        self.assertEqual(code, 0, output.getvalue())
        payload = json.loads(output.getvalue())
        workspace = Path(payload['workspace'])
        self.assertEqual(payload['output_directory'], str(self.root / '计组'))
        self.assertEqual(workspace.parent.parent.name, '09-17')
        sources = json.loads((workspace / 'handoff/sources.json').read_text())
        self.assertEqual(sources['course_title'], '计算机组成')

    def test_rejects_relative_nested_equal_files_and_credentials(self):
        file = self.root / 'course.md'
        file.write_text('preserve')
        for incoming, outgoing in [('relative', self.root / 'out'),
                                   (self.root / 'in', self.root / 'in'),
                                   (self.root / 'in', self.root / 'in/out'),
                                   (self.root / 'out/in', self.root / 'out'),
                                   (file, self.root / 'out'),
                                   (self.root / 'auth_key=synthetic', self.root / 'out')]:
            with self.subTest(incoming=incoming, outgoing=outgoing), self.assertRaises(ValueError):
                settings.save_directory_settings(incoming, outgoing)
        self.assertEqual(file.read_text(), 'preserve')
        self.assertFalse(self.config.exists())

    def test_rejects_symlink_configuration_and_parent(self):
        target = self.root / 'course.md'
        target.write_text('preserve')
        self.config.parent.mkdir()
        self.config.symlink_to(target)
        with self.assertRaises(ValueError):
            settings.save_directory_settings(self.root / 'in', self.root / 'out')
        with self.assertRaises(ValueError):
            settings.load_directory_settings()
        self.config.unlink()
        self.config.parent.rmdir()
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        self.config.parent.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(ValueError):
            settings.save_directory_settings(self.root / 'in', self.root / 'out')
        self.assertEqual(target.read_text(), 'preserve')

    def test_corrupt_configuration_is_explicit(self):
        self.config.parent.mkdir()
        self.config.write_text('{broken')
        with self.assertRaises(ValueError):
            settings.load_directory_settings()

    def test_corrupt_configuration_can_be_reset_with_both_global_paths(self):
        self.config.parent.mkdir()
        self.config.write_text('{broken')
        result = settings.save_directory_settings(self.root / 'new-in', self.root / 'new-out')
        self.assertEqual(result['input_dir'], str(self.root / 'new-in'))
        self.assertEqual(result['output_dir'], str(self.root / 'new-out'))
        self.assertEqual(result['course_output_dirs'], {})
        self.assertEqual(settings.load_directory_settings()['output_dir'], str(self.root / 'new-out'))

    def test_failed_atomic_replace_preserves_previous_settings(self):
        settings.save_directory_settings(self.root / 'in', self.root / 'out')
        original = self.config.read_bytes()
        with patch.object(settings.os, 'replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                settings.save_directory_settings(self.root / 'other-in', self.root / 'other-out')
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(list(self.config.parent.iterdir()), [self.config])

    def test_platform_configuration_locations(self):
        # Undo only the path stub; no load or save touches these locations.
        self.patchers[0].stop()
        with patch.object(Path, 'home', return_value=self.root):
            for platform, environment, expected in [
                ('darwin', {}, self.root / 'Library/Application Support'),
                ('win32', {'LOCALAPPDATA': str(self.root / 'Local')}, self.root / 'Local'),
                ('linux', {'XDG_CONFIG_HOME': str(self.root / 'XDG')}, self.root / 'XDG'),
                ('linux', {'XDG_CONFIG_HOME': 'relative'}, self.root / '.config'),
            ]:
                with self.subTest(platform=platform, environment=environment), patch.object(settings.sys, 'platform', platform), patch.dict(os.environ, environment):
                    self.assertEqual(settings.directory_settings_path(), expected / 'Handouter/directories.json')

    def test_macos_desktop_is_home_desktop(self):
        with patch.object(platform_support.sys, 'platform', 'darwin'), patch.object(Path, 'home', return_value=self.root):
            self.assertEqual(platform_support.desktop_directory(), self.root / 'Desktop')

    def test_linux_xdg_desktop_is_parsed_without_execution(self):
        config = self.root / 'xdg'
        config.mkdir()
        file = config / 'user-dirs.dirs'
        file.write_text('XDG_DESKTOP_DIR="$HOME/桌面"\n')
        with patch.object(platform_support.sys, 'platform', 'linux'), patch.object(Path, 'home', return_value=self.root), patch.dict(os.environ, {'XDG_CONFIG_HOME': str(config)}):
            self.assertEqual(platform_support.desktop_directory(), self.root / '桌面')
            file.write_text('XDG_DESKTOP_DIR="$(touch hacked)"\n')
            self.assertEqual(platform_support.desktop_directory(), self.root / 'Desktop')
        self.assertFalse((self.root / 'hacked').exists())

    def test_windows_desktop_uses_known_folder(self):
        with patch.object(platform_support.sys, 'platform', 'win32'), patch.object(platform_support, '_windows_desktop_directory', return_value=self.root / 'OneDrive/Desktop'):
            self.assertEqual(platform_support.desktop_directory(), self.root / 'OneDrive/Desktop')


if __name__ == '__main__':
    unittest.main()
