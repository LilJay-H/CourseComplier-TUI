"""Installer orchestration without network or changes to the user's environment."""

import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
import os
import subprocess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import setup_handouter as setup


class SetupTests(unittest.TestCase):
    def test_release_python_range_is_explicit(self):
        self.assertTrue(setup.python_supported((3, 11)))
        self.assertTrue(setup.python_supported((3, 13)))
        self.assertFalse(setup.python_supported((3, 10)))
        self.assertTrue(setup.python_supported((3, 14)))
        self.assertTrue(setup.python_supported((3, 15)))
        self.assertFalse(setup.python_supported((4, 0)))

    def test_existing_314_environment_is_not_rejected_by_nested_probe(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            python = root / '.venv' / ('Scripts/python.exe' if setup.os.name == 'nt' else 'bin/python')
            python.parent.mkdir(parents=True)
            python.touch()
            with patch.object(setup, 'ROOT', root), patch.object(setup, 'run') as run, \
                 patch.object(setup, 'ensure_media'), patch.object(setup, 'check_runtime'), \
                 patch.object(setup, 'prepare_models'), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(setup.main([]), 0)
            probe = next(call.args[0][-1] for call in run.call_args_list
                         if '-c' in call.args[0] and 'sys.version_info' in call.args[0][-1])
            # Execute the actual child probe with a synthetic interpreter version.
            for version in ('(3, 14, 0)', '(3, 15, 0)'):
                result = subprocess.run([sys.executable, '-c', 'import sys; sys.version_info = ' + version + '; ' + probe],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipIf(os.name == 'nt', 'macOS Bash bootstrap integration')
    def test_macos_bootstrap_finds_explicit_314_without_installing_python(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Exercise only the real discovery function. Never execute the
            # bootstrap's package-manager fallback, including on regressions.
            discovery = (setup.ROOT / 'setup-handouter.command').read_text().split('\nrefresh_brew_path()', 1)[0]
            script = root / 'discover.command'
            script.write_text(discovery + '\nfind_python\n')
            binaries = root / 'bin'
            binaries.mkdir()
            (binaries / 'python3.14').symlink_to(sys.executable)
            # The default Python is deliberately unavailable.
            for name in ('python3', 'brew', 'curl'):
                blocked = binaries / name
                blocked.write_text('#!/bin/sh\nexit 91\n')
                blocked.chmod(0o755)
            result = subprocess.run(['/bin/bash', str(script)],
                env={**os.environ, 'PATH': str(binaries) + ':/usr/bin:/bin'},
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), str(binaries / 'python3.14'))

    def test_existing_environment_is_reused_without_forcing_dependencies(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            python = root / '.venv' / ('Scripts/python.exe' if setup.os.name == 'nt' else 'bin/python')
            python.parent.mkdir(parents=True)
            python.touch()
            with patch.object(setup, 'ROOT', root), patch.object(setup, 'run') as run, \
                 patch.object(setup, 'ensure_media'), patch.object(setup, 'check_runtime'), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(setup.main([]), 0)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn([str(python), '-m', 'pip', 'install', '.[asr,html]'], commands)
            self.assertFalse(any('venv' in command or '--force-reinstall' in command for command in commands))

    def test_incomplete_environment_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / '.venv').mkdir()
            with patch.object(setup, 'ROOT', root), patch.object(setup, 'run') as run, \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(setup.main([]), 1)
            run.assert_not_called()

    def test_existing_ffmpeg_is_verified_without_package_manager(self):
        with patch.object(setup, 'refresh_windows_path'), \
             patch.object(setup.shutil, 'which', side_effect=lambda name: '/tools/' + name), \
             patch.object(setup, 'run') as run:
            setup.ensure_media()
        self.assertEqual([c.args[0] for c in run.call_args_list],
                         [['/tools/ffmpeg', '-version'], ['/tools/ffprobe', '-version']])

    def test_bundled_ffmpeg_is_copied_into_private_venv_before_system_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / 'runtime' / 'ffmpeg'
            runtime.mkdir(parents=True)
            python = root / '.venv' / ('Scripts/python.exe' if setup.os.name == 'nt' else 'bin/python')
            python.parent.mkdir(parents=True)
            python.touch()
            suffix = '.exe' if setup.os.name == 'nt' else ''
            for name in ('ffmpeg', 'ffprobe'):
                path = runtime / (name + suffix)
                path.write_bytes((name + '-bundled').encode())
                if setup.os.name != 'nt':
                    path.chmod(0o755)
            with patch.object(setup, 'ROOT', root), patch.object(setup, 'refresh_windows_path'), \
                 patch.object(setup.shutil, 'which', return_value=None), patch.object(setup, 'run') as run:
                setup.ensure_media(python=python, offline=True)
            for name in ('ffmpeg', 'ffprobe'):
                copied = python.parent / (name + suffix)
                self.assertEqual(copied.read_bytes(), (name + '-bundled').encode())
            self.assertEqual(len(run.call_args_list), 2)

    def test_windows_install_rechecks_both_executables(self):
        found = {'winget': 'winget.exe'}
        def install(command, **kwargs):
            if command[0] == 'winget':
                found.update(ffmpeg='ffmpeg.exe', ffprobe='ffprobe.exe')
        with patch.object(setup.sys, 'platform', 'win32'), \
             patch.object(setup, 'refresh_windows_path') as refresh, \
             patch.object(setup.shutil, 'which', side_effect=found.get), \
             patch.object(setup, 'run', side_effect=install), contextlib.redirect_stdout(io.StringIO()):
            setup.ensure_media()
        self.assertEqual(refresh.call_count, 2)

    def test_models_are_prepared_by_default_and_can_be_explicitly_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            python = root / '.venv' / ('Scripts/python.exe' if setup.os.name == 'nt' else 'bin/python')
            python.parent.mkdir(parents=True)
            python.touch()
            with patch.object(setup, 'ROOT', root), patch.object(setup, 'run'), \
                 patch.object(setup, 'ensure_media'), patch.object(setup, 'check_runtime'), \
                 patch.object(setup, 'prepare_models') as prepare, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(setup.main([]), 0)
                prepare.assert_called_once_with(python)
            with patch.object(setup, 'ROOT', root), patch.object(setup, 'run'), \
                 patch.object(setup, 'ensure_media'), patch.object(setup, 'check_runtime'), \
                 patch.object(setup, 'prepare_models') as prepare, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(setup.main(['--skip-model-download']), 0)
                prepare.assert_not_called()

    def test_runtime_failure_cannot_report_ready(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(setup, 'ROOT', Path(td)), patch.object(setup, 'run'), \
                 patch.object(setup, 'ensure_media'), \
                 patch.object(setup, 'check_runtime', side_effect=RuntimeError('native import failed')), \
                 contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(setup.main([]), 1)
                self.assertNotIn('Ready.', output.getvalue())

    def test_without_asr_installs_reading_features_and_never_loads_models(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(setup, 'ROOT', Path(td)), patch.object(setup, 'run') as run, \
                 patch.object(setup, 'ensure_media'), patch.object(setup, 'check_runtime') as check, \
                 patch.object(setup, 'prepare_models') as models, contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(setup.main(['--without-asr']), 0)
            self.assertIn('.[html]', next(call.args[0] for call in run.call_args_list if 'install' in call.args[0]))
            check.assert_called_once_with(Path(td) / '.venv' / ('Scripts/python.exe' if setup.os.name == 'nt' else 'bin/python'), asr=False)
            models.assert_not_called()
            self.assertIn('ASR is not installed', output.getvalue())

    def test_without_asr_runtime_checks_do_not_import_native_stack(self):
        with patch.object(setup, 'run') as run:
            setup.check_runtime(Path('/synthetic/python'), asr=False)
        commands = str(run.call_args_list)
        self.assertNotIn('import torch', commands)
        self.assertIn('pip', commands)
        self.assertIn("'html'", commands)
