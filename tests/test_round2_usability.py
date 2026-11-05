"""Round-2 Windows usability fixes: Agent heartbeat, cached-collection reuse,
stale temp cleanup, audio de-duplication and TUI workspace/mode handling.
Synthetic fixtures only; no terminal, plugin, network or model."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from handouter import service, tui
from handouter.agents import diagnosis
from handouter.media import AudioExtractResult
from handouter.organization import MATERIALS
from handouter.service import prepare_lecture
from handouter.stage_cache import MaterialCache, SourceAlias
from handouter.workspace import _copy_cached

FINGERPRINT = 'a' * 64
PARAMS = {'device': 'auto', 'language': 'auto', 'model_path': None}


class AgentHeartbeatTests(unittest.TestCase):
    def test_heartbeat_reports_elapsed_time_on_a_fresh_line(self):
        captured = io.TextIOWrapper(io.BytesIO(), encoding='utf-8', newline='\n')
        child = [sys.executable, '-c', "import sys,time; sys.stdout.write('partial'); sys.stdout.flush(); time.sleep(1.2)"]
        with patch.object(sys, 'stderr', captured):
            code, tail = diagnosis.run_forwarding(child, input_text='', cwd=None, heartbeat=0.4)
        captured.flush()
        output = captured.buffer.getvalue().decode('utf-8')
        self.assertEqual(code, 0)
        self.assertIn('partial\n[progress] Agent/交付', output)
        self.assertIn('已用', output)
        self.assertEqual(tail, 'partial')  # Heartbeats never enter the diagnostic tail.

    def test_heartbeat_line_uses_cli_progress_format(self):
        line = diagnosis.heartbeat_line(125)
        self.assertTrue(line.startswith('[progress] Agent/交付'))
        self.assertIn('2 分 05 秒', line)

    def test_tui_progress_row_hides_machine_prefix(self):
        self.assertTrue(tui._diagnostic_line(diagnosis.heartbeat_line(5)).startswith('[progress]'))


class FailureDiagnosisTests(unittest.TestCase):
    def test_only_the_end_of_output_decides_the_cause(self):
        lecture = '本节讲 rate limit 和 unauthorized 访问控制。' + 'x' * 3000
        self.assertIsNone(diagnosis.diagnose('codex', 1, lecture + '\nError: something else'))
        self.assertIn('未登录', diagnosis.diagnose('claude', 1, lecture + '\nNot logged in · Please run /login'))

    def test_reset_time_echoes_only_a_time_shape(self):
        hint = diagnosis.diagnose('claude', 1, "You've hit your usage limit · resets at 3pm (Asia/Shanghai) secret")
        self.assertIn('3pm', hint)
        self.assertNotIn('secret', hint)
        self.assertNotIn('Asia', hint)

    def test_login_status_does_not_hang_and_reports_unknown_for_odd_output(self):
        started = time.monotonic()
        self.assertEqual(diagnosis.login_status('codex', sys.executable, timeout=10), 'unknown')
        self.assertLess(time.monotonic() - started, 10)


class CollectionReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.slides = self.root / 'standard' / 'slides' / 'images'
        self.slides.mkdir(parents=True)
        (self.slides / 'slide_0001.jpg').write_bytes(b'\xff\xd8synthetic')
        self.meta = self.root / 'standard' / 'slides' / 'slides_meta.json'
        self.meta.write_text('[{"filename": "slide_0001.jpg", "start_ms": 0}]', encoding='utf-8')

    def _cached_audio(self, identity='b' * 64):
        cache = MaterialCache(self.root, 'lecture', identity, PARAMS)
        def extract(_source, output):
            output.write_bytes(b'synthetic audio')
            return AudioExtractResult(output, 1000, output.stat().st_size, 'copy')
        cache.audio(self.root / 'unused.mp4', extract)
        return identity

    def test_alias_round_trip_and_reuse_requires_cached_audio(self):
        identity = 'b' * 64
        SourceAlias(self.root, 'lecture', FINGERPRINT).save(identity, self.slides, self.meta)
        self.assertIsNone(service._reusable_collection(self.root, 'lecture', FINGERPRINT, PARAMS))
        self._cached_audio(identity)
        reused = service._reusable_collection(self.root, 'lecture', FINGERPRINT, PARAMS)
        self.assertIsNotNone(reused)
        media_identity, audio, images, meta = reused
        self.assertEqual(media_identity, identity)
        self.assertEqual(audio.read_bytes(), b'synthetic audio')
        self.assertEqual((images / 'slide_0001.jpg').read_bytes(), b'\xff\xd8synthetic')
        self.assertEqual(meta.read_text(encoding='utf-8'), self.meta.read_text(encoding='utf-8'))
        # Other ASR settings use another cache key: download again.
        self.assertIsNone(service._reusable_collection(self.root, 'lecture', FINGERPRINT, {**PARAMS, 'device': 'cpu'}))

    def test_tampered_snapshot_is_not_reused(self):
        identity = self._cached_audio()
        alias = SourceAlias(self.root, 'lecture', FINGERPRINT)
        alias.save(identity, self.slides, self.meta)
        images, _meta = alias.slides(alias.load())
        (images / 'slide_0001.jpg').write_bytes(b'changed')
        self.assertIsNone(alias.load())
        self.assertIsNone(service._reusable_collection(self.root, 'lecture', FINGERPRINT, PARAMS))

    def test_unrecorded_extra_image_blocks_reuse(self):
        alias = SourceAlias(self.root, 'lecture', FINGERPRINT)
        alias.save('b' * 64, self.slides, self.meta)
        images, _meta = alias.slides(alias.load())
        (images / 'slide_9999.jpg').write_bytes(b'extra')
        self.assertIsNone(alias.load())

    def test_alias_stores_no_url(self):
        SourceAlias(self.root, 'lecture', FINGERPRINT).save('b' * 64, self.slides, self.meta)
        for path in (self.root / '.handouter-cache').rglob('source-alias.json'):
            self.assertNotIn('://', path.read_text(encoding='utf-8'))

    def test_browser_export_with_cached_audio_skips_the_plugin_download(self):
        details = {'source_fingerprint': FINGERPRINT, 'source_sha256': 'c' * 64, 'collector_binding': 'd' * 64,
                   'media_hosts': ['media.example.org'], 'course_title': '合成', 'lecture_date': None}
        reused = ('b' * 64, self.root / 'audio.m4a', self.slides, self.meta)
        events = []
        with patch('handouter.external_collector.inspect_browser_export', return_value=details), \
             patch('handouter.external_collector.convert_browser_export') as convert, \
             patch.object(service, '_reusable_collection', return_value=reused), \
             patch.object(service, 'build_from_media', return_value='built') as build:
            result = service.build_from_asset_zip(self.root / 'out', lecture_id='lecture', course_title='合成',
                asset_zip=self.root / 'browser.zip', allowed_media_hosts=['media.example.org'],
                cache_root=self.root, progress=lambda *event: events.append(event))
        self.assertEqual(result, 'built')
        convert.assert_not_called()
        kwargs = build.call_args.kwargs
        self.assertEqual(kwargs['cache_identity'], 'b' * 64)
        self.assertEqual(kwargs['input_fingerprint'], FINGERPRINT)
        self.assertEqual(kwargs['slides_dir'], self.slides)
        self.assertTrue(any('跳过视频下载' in str(event) for event in events))


    def test_reuse_still_refuses_an_export_changed_after_confirmation(self):
        details = {'source_fingerprint': FINGERPRINT, 'source_sha256': 'c' * 64, 'collector_binding': 'd' * 64,
                   'media_hosts': ['media.example.org'], 'course_title': '合成', 'lecture_date': None}
        reused = ('b' * 64, self.root / 'audio.m4a', self.slides, self.meta)
        with patch('handouter.external_collector.inspect_browser_export', return_value=details), \
             patch.object(service, '_reusable_collection', return_value=reused), \
             patch.object(service, 'build_from_media') as build:
            with self.assertRaisesRegex(ValueError, 'SOURCE_CHANGED'):
                service.build_from_asset_zip(self.root / 'out', lecture_id='lecture', course_title='合成',
                    asset_zip=self.root / 'browser.zip', allowed_media_hosts=['media.example.org'],
                    collector_source_sha256='e' * 64, cache_root=self.root)
        build.assert_not_called()


class CollectorByteProgressTests(unittest.TestCase):
    def test_byte_frames_fill_the_download_part_of_the_bar(self):
        from handouter.external_collector import _progress_display
        mb = 1024 * 1024
        self.assertEqual(_progress_display({'stage': 'downloading'})[0], 10)
        half = _progress_display({'stage': 'downloading', 'completed_bytes': 600 * mb, 'total_bytes': 1200 * mb})
        self.assertEqual(half, (44, '下载媒体 600/1200 MB'))
        self.assertEqual(_progress_display({'stage': 'downloading', 'completed_bytes': 2 * mb, 'total_bytes': mb})[0], 79)
        self.assertIn('已收到 5 MB', _progress_display({'stage': 'downloading', 'completed_bytes': 5 * mb, 'total_bytes': None})[1])
        self.assertEqual(_progress_display({'stage': 'packaging'})[0], 80)
        for bad in ({'completed_bytes': -1, 'total_bytes': None}, {'completed_bytes': '1', 'total_bytes': 2},
                    {'completed_bytes': True, 'total_bytes': 2}, {'completed_bytes': 1, 'total_bytes': 0}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'COLLECTOR_PROTOCOL'):
                _progress_display({'stage': 'downloading', **bad})

    def test_switch_is_sent_only_to_plugins_that_declare_it(self):
        from handouter import external_collector as host
        details = {'source_sha256': 'c' * 64, 'media_hosts': ['media.example.org']}
        for capabilities, expected in ((['inspect', 'convert'], False), (['inspect', 'convert', 'progress_bytes'], True)):
            with tempfile.TemporaryDirectory() as directory, self.subTest(capabilities=capabilities):
                target = Path(directory) / 'standard.zip'
                calls = []
                def invoke(_config, method, params, **_kwargs):
                    calls.append((method, params))
                    return {'output_path': str(target)}
                with patch.object(host, 'load_configuration', return_value={}), \
                     patch.object(host, 'inspect_browser_export', return_value=details), \
                     patch.object(host, '_identity', return_value=capabilities), \
                     patch.object(host, '_invoke', side_effect=invoke), \
                     patch('handouter_local.assets.inspect', return_value=SimpleNamespace(source_kind='asset_zip')):
                    host.convert_browser_export(Path(directory) / 'browser.zip', target,
                                                allowed_media_hosts=['media.example.org'])
                params = calls[-1][1]
                self.assertEqual('progress_bytes' in params, expected)
                if expected:
                    self.assertIs(params['progress_bytes'], True)


class RelativeInputTests(unittest.TestCase):
    def test_explicit_input_dir_wins_and_cwd_is_only_a_fallback(self):
        from handouter.product import _relative_input
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / 'inputs'
            inputs.mkdir()
            (inputs / 'a.mp3').write_bytes(b'x')
            (root / 'a.mp3').write_bytes(b'y')
            (root / 'only-here.zip').write_bytes(b'z')
            previous = os.getcwd()
            os.chdir(root)
            try:
                self.assertEqual(_relative_input(inputs, Path('a.mp3'), cwd_first='fallback'), inputs / 'a.mp3')
                self.assertEqual(_relative_input(inputs, Path('only-here.zip'), cwd_first='fallback').resolve(),
                                 (root / 'only-here.zip').resolve())
                self.assertEqual(_relative_input(inputs, Path('a.mp3'), cwd_first=True).resolve(),
                                 (root / 'a.mp3').resolve())
            finally:
                os.chdir(previous)


class TemporarySweepTests(unittest.TestCase):
    def test_only_old_own_staging_dirs_are_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            old = base / 'handouter-collection-old'
            fresh = base / 'handouter-collection-new'
            other = base / 'someone-else-old'
            for path in (old, fresh, other):
                path.mkdir()
                (path / 'lecture.mp4').write_bytes(b'x')
            past = time.time() - 48 * 3600
            for path in (old, other):
                os.utime(path / 'lecture.mp4', (past, past))
                os.utime(path, (past, past))
            with patch.object(service.tempfile, 'gettempdir', return_value=directory):
                removed = service.sweep_stale_temporaries()
            self.assertEqual(removed, ['handouter-collection-old'])
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(other.exists())


class CachedAudioLinkTests(unittest.TestCase):
    def test_cache_audio_is_linked_and_user_audio_is_copied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cached = root / '.handouter-cache' / 'lecture' / 'audio.m4a'
            cached.parent.mkdir(parents=True)
            cached.write_bytes(b'audio')
            own = root / 'my.m4a'
            own.write_bytes(b'audio')
            _copy_cached(cached, root / 'linked.m4a')
            _copy_cached(own, root / 'copied.m4a')
            self.assertEqual((root / 'linked.m4a').read_bytes(), b'audio')
            self.assertFalse(os.path.samefile(own, root / 'copied.m4a'))
            try:
                linked = os.path.samefile(cached, root / 'linked.m4a')
            except OSError:
                linked = False
            if os.stat(cached).st_nlink > 1:
                self.assertTrue(linked)


class TUIWorkspaceInputTests(unittest.TestCase):
    def setUp(self):
        tui.set_language('zh')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        transcript = self.root / 'lecture.txt'
        transcript.write_text('合成课程', encoding='utf-8')
        self.workspace = prepare_lecture(self.root / 'out', lecture_id='course', course_title='合成',
                                         transcript=transcript, use_slides_as_source=False).workspace.resolve()
        self.state = {'output_dir': str(self.workspace.parent), 'lecture': 'course', 'title': '合成',
                      'notes_deep': True, 'notes_summary': True, 'use_slides': False, 'embed_slides': False,
                      'allow_web': False, 'format_profile': 'clean', 'agent': 'codex'}

    def test_w_accepts_lecture_folder_and_explains_bad_input(self):
        self.assertEqual(tui._workspace_from_input(self.state, str(self.workspace)), self.workspace)
        with self.assertRaisesRegex(ValueError, '完整路径'):
            tui._workspace_from_input(self.state, 'w')
        with self.assertRaisesRegex(ValueError, '不存在'):
            tui._workspace_from_input(self.state, str(self.root / 'missing'))
        with self.assertRaisesRegex(ValueError, '不是讲次'):
            tui._workspace_from_input(self.state, str(self.root))

    def test_w_maps_the_original_zip_to_its_lecture(self):
        archive = self.root / 'lesson.zip'
        archive.write_bytes(b'PK')
        with patch.object(tui, 'inspect_input_source', return_value=SimpleNamespace(lecture_id='course', course_title=None)):
            self.assertEqual(tui._workspace_from_input(self.state, str(archive)), self.workspace)
        with patch.object(tui, 'inspect_input_source', return_value=SimpleNamespace(lecture_id='other', course_title=None)):
            with self.assertRaisesRegex(ValueError, '还没有处理过'):
                tui._workspace_from_input(self.state, str(archive))

    def test_prefill_only_offers_existing_lecture(self):
        self.assertEqual(tui._workspace_prefill(self.state), str(self.workspace))
        self.assertEqual(tui._workspace_prefill({**self.state, 'output_dir': 'w'}), '')

    def test_output_dir_must_be_absolute(self):
        with self.assertRaisesRegex(ValueError, '完整路径'):
            tui._commit_text_value(dict(self.state), 'output_dir', 'w')

    def test_new_lecture_shows_planned_organized_location(self):
        state = {'output_dir': str(self.root / 'output'), 'lecture': 'new-lecture', 'title': '合成课',
                 'lecture_date': '2026-10-08'}
        settings = {'input_dir': str(self.root), 'output_dir': str(self.root / 'output'), 'course_output_dirs': {},
                    'course_aliases': {}, 'course_folder_modes': {}, 'configured': True}
        with patch.object(tui, 'load_directory_settings', return_value=settings):
            shown = tui._workspace_display(state)
        self.assertIn(MATERIALS, shown)
        self.assertIn('new-lecture', shown)
        self.assertIn('运行后创建', shown)

    def test_adding_a_mode_offers_to_keep_the_finished_one(self):
        plan = SimpleNamespace(completed={'deep': 'notes/deep-001.md'}, pending={})
        with patch('handouter.tasks.plan_task', return_value=plan):
            done, remaining = tui._reusable_modes(self.state, self.workspace)
            self.assertEqual(done, {'deep': 'notes/deep-001.md'})
            self.assertEqual(remaining, ['summary'])
            # Different options: the old Deep does not match, nothing to keep.
            self.assertEqual(tui._reusable_modes({**self.state, 'format_profile': 'traceable'}, self.workspace), ({}, []))
        state = dict(self.state)
        tui._keep_finished_modes(state, done)
        self.assertFalse(state['notes_deep'])
        self.assertTrue(state['notes_summary'])
        # Keeping Deep for this task must not change the saved default for new courses.
        with tempfile.TemporaryDirectory() as storage:
            tui._save_preferences(state, Path(storage))
            self.assertTrue(tui._load_preferences(Path(storage))['notes_deep'])

    def test_reuse_offer_checks_the_selected_materials(self):
        seen = {}
        def plan_task(_workspace, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(action='refresh_required', completed={'deep': 'notes/deep-001.md'})
        with patch('handouter.tasks.plan_task', side_effect=plan_task), \
             patch.object(tui, '_selected_input_fingerprint', return_value='f' * 64):
            self.assertEqual(tui._reusable_modes({**self.state, 'asset': 'x.zip'}, self.workspace), ({}, []))
        self.assertEqual(seen.get('input_fingerprint'), 'f' * 64)

    def test_lone_w_in_empty_source_field_opens_workspace_dialog(self):
        import curses
        screen = MagicMock()
        screen.getmaxyx.return_value = (32, 112)
        screen.getch.side_effect = [ord('W')]
        screen.get_wch.side_effect = curses.error('no input')
        with patch('curses.wrapper', side_effect=lambda callback: callback(screen)), patch('curses.curs_set'), \
             patch('curses.napms'), patch.object(tui, '_load_preferences', return_value={}), \
             patch.object(tui, 'available_agent_choices', return_value=(['web'], {'web': 'Web'})), \
             patch.object(tui, '_edit_value', side_effect=RuntimeError('stop-at-editor')) as edit:
            with self.assertRaisesRegex(RuntimeError, 'stop-at-editor'):
                tui._run_curses()
        self.assertEqual(edit.call_args.args[1], tui._t('prompt_workspace'))

    def test_paste_into_prefilled_editor_replaces_value(self):
        import curses
        screen = MagicMock()
        screen.getmaxyx.return_value = (32, 112)
        screen.get_wch.side_effect = ['E', *':\\x', curses.error('idle'), curses.error('idle'), '\n']
        with patch('curses.curs_set'), patch('curses.napms'):
            value, committed = tui._edit_value(screen, 'W', 'old-prefill')
        self.assertTrue(committed)
        self.assertEqual(value, 'E:\\x')


if __name__ == '__main__':
    unittest.main()
