"""Synthetic course packages, stable identity and immutable delivery boundaries."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from handouter.service import prepare_lecture, refresh_handoff
from _gates import requires_html


class OrganizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.text = self.root / 'source.txt'
        self.text.write_text('合成课程，x > 0。', encoding='utf-8')

    def prepare(self, identity='one', **kwargs):
        return prepare_lecture(self.root / 'out', lecture_id=identity, course_title='课程 / A',
            transcript=self.text, use_slides_as_source=False, organized=True, **kwargs)

    def test_named_package_keeps_stable_id_and_named_versioned_outputs(self):
        result = self.prepare(lecture_date='2026-09-10')
        self.assertEqual(result.workspace.parent.name, '资料与交接')
        self.assertEqual(result.workspace.parent.parent.name, '课程 _ A-09-10')
        self.assertTrue((result.workspace.parent.parent / '成果').is_dir())
        self.assertTrue((result.workspace.parent.parent / '中间产物_可清理').is_dir())
        self.assertEqual(json.loads((result.workspace / 'manifest.json').read_text())['lecture_id'], 'one')
        output = result.handoff.output_path
        self.assertEqual(output, 'notes/课程 _ A_2026-09-10_deep-001.md')
        (result.workspace / output).write_text('# 保留\n')
        refreshed = refresh_handoff(result.workspace)
        self.assertTrue(refreshed.handoff.output_path.endswith('_deep-002.md'))

    def test_unknown_date_never_uses_today_and_invalid_dates_fail(self):
        self.assertIn('日期待确认', self.prepare().workspace.parent.parent.name)
        with self.assertRaises(ValueError):
            self.prepare('two', lecture_date='2026-02-30')

    def test_same_day_collision_and_lookup_are_bound_to_lecture_id(self):
        from handouter.organization import find_workspace, resolve_workspace
        first = self.prepare('one', lecture_date='2026-09-10')
        second = self.prepare('two', lecture_date='2026-09-10')
        self.assertNotEqual(first.workspace, second.workspace)
        self.assertEqual(find_workspace(self.root / 'out', 'one'), first.workspace)
        self.assertEqual(resolve_workspace(first.workspace.parent.parent), first.workspace)
        with self.assertRaises(FileExistsError):
            self.prepare('one', lecture_date='2026-09-10')

    def test_legacy_workspace_is_not_moved(self):
        from handouter.organization import find_workspace, resolve_workspace
        old = prepare_lecture(self.root / 'out', lecture_id='old', course_title='Old', transcript=self.text)
        before = (old.workspace / 'manifest.json').read_bytes()
        self.assertEqual(find_workspace(self.root / 'out', 'old'), old.workspace)
        self.assertEqual(resolve_workspace(old.workspace), old.workspace)
        self.assertEqual((old.workspace / 'manifest.json').read_bytes(), before)

    def test_old_v1_package_name_still_resolves_after_new_short_folder_rule(self):
        from handouter.organization import resolve_workspace, MATERIALS
        package = self.root / 'out' / '旧课程_2026-09-10'
        workspace = package / MATERIALS / 'legacy-id'
        workspace.mkdir(parents=True)
        marker = {'version': 1, 'lecture_id': 'legacy-id', 'course_title': '旧课程',
                  'lecture_date': '2026-09-10', 'base_name': '旧课程_2026-09-10',
                  'name_prefix': '旧课程_2026-09-10'}
        (package / '.handouter-collection.json').write_text(json.dumps(marker, ensure_ascii=False))
        self.assertEqual(resolve_workspace(package), workspace.resolve())

    def test_auto_folder_name_omits_course_when_root_is_course_or_alias(self):
        from handouter.organization import package_name
        course_root = self.root / '计算机组成'
        alias_root = self.root / '计组'
        other_root = self.root / '课程'
        self.assertEqual(package_name('计算机组成', '2026-09-17', course_root,
                                      aliases=['计组'], folder_name_mode='auto'), ('09-17', 'date_only'))
        self.assertEqual(package_name('计算机组成', '2026-09-17', alias_root,
                                      aliases=['计组'], folder_name_mode='auto'), ('09-17', 'date_only'))
        self.assertEqual(package_name('计算机组成', '2026-09-17', other_root,
                                      aliases=['计组'], folder_name_mode='auto'), ('计算机组成-09-17', 'course_date'))
        self.assertEqual(package_name('计算机组成', '2026-09-17', alias_root,
                                      aliases=['计组'], folder_name_mode='always'), ('计算机组成-09-17', 'course_date'))
        self.assertEqual(package_name('计算机组成', '2026-09-17', other_root,
                                      aliases=['计组'], folder_name_mode='never'), ('09-17', 'date_only'))

    def test_export_date_is_not_lecture_date(self):
        import zipfile
        from handouter.product import inspect_asset_zip
        asset = self.root / 'asset.zip'
        with zipfile.ZipFile(asset, 'w') as archive:
            archive.writestr('course_info.json', json.dumps({'courseName': '课', 'exportDate': '2026-09-17'}))
        self.assertIsNone(inspect_asset_zip(asset).lecture_date)
        with zipfile.ZipFile(asset, 'w') as archive:
            archive.writestr('course_info.json', json.dumps({'courseName': '课', 'lectureDate': '2026-09-10'}))
        self.assertEqual(inspect_asset_zip(asset).lecture_date, '2026-09-10')

    def test_failed_empty_package_does_not_block_corrected_date(self):
        with patch('handouter.service.create_workspace', side_effect=ValueError('synthetic invalid input')):
            with self.assertRaises(ValueError):
                self.prepare(lecture_date='2026-09-10')
        result = self.prepare(lecture_date='2026-09-11')
        self.assertIn('09-11', str(result.workspace.parent.parent))

    def test_cli_prepare_uses_saved_course_output_when_no_explicit_root(self):
        import contextlib
        import io
        from handouter import cli
        course_root = self.root / 'course-root'
        output = io.StringIO()
        with patch('handouter.settings.resolve_output_directory', return_value=str(course_root)) as resolve, \
             contextlib.redirect_stdout(output):
            code = cli.main(['prepare', '--lecture-id', 'course-dir', '--course-title', '课程 / A',
                             '--lecture-date', '2026-09-10', '--transcript', str(self.text),
                             '--ignore-slides'])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(resolve.call_args.args, ('课程 / A',))
        self.assertEqual(resolve.call_args.kwargs['explicit'], None)
        self.assertIn('settings', resolve.call_args.kwargs)
        workspace = Path(json.loads(output.getvalue())['workspace'])
        self.assertTrue(workspace.is_relative_to(course_root.resolve()))

    @requires_html

    def test_cli_defaults_import_html_and_movable_results(self):
        import base64
        import contextlib
        import io
        import shutil
        from handouter import cli
        from handouter.tasks import plan_task
        from handouter.html_export import export_html
        images = self.root / 'images'
        images.mkdir()
        (images / 'one.png').write_bytes(base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='))
        def command(args):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(args)
            self.assertEqual(code, 0, output.getvalue())
            return json.loads(output.getvalue())
        defaults = {'input_dir': str(self.root / 'input'), 'output_dir': str(self.root / 'out'), 'configured': True}
        with patch('handouter.settings.load_directory_settings', return_value=defaults):
            created = command(['prepare', '--lecture-id', 'cli', '--course-title', '中文课',
                '--lecture-date', '2026-09-10', '--transcript', str(self.text), '--slides-dir', str(images), '--embed-slides'])
        workspace = Path(created['workspace'])
        package = workspace.parent.parent
        returned = self.root / 'returned.md'
        returned.write_text('# 中文课\n\n$\\frac{1}{2}$\n\n![图](../slides/images/one.png)\n', encoding='utf-8')
        result = command(['import-note', str(package), str(returned), '--mode', 'deep', '--task-id', plan_task(workspace).task_id])
        published = result['publication']['outputs']['deep']
        self.assertIn('成果', published['markdown'])
        self.assertIn('中文课_2026-09-10_deep-001', published['html'])
        self.assertEqual(command(['task-status', str(package)])['action'], 'complete')
        public_image = package / '成果/slides/images/one.png'
        public_image.unlink()
        self.assertEqual(plan_task(workspace).action, 'publish_only')
        with patch('handouter.cli.run_cli_agent', side_effect=AssertionError('must not run')):
            command(['continue', str(package), '--agent', 'codex'])
        self.assertTrue(public_image.is_file())
        bundle = command(['bundle', str(package)])
        self.assertTrue(Path(bundle['bundle']).name.startswith('中文课_2026-09-10_gui-handoff'))
        moved = self.root / 'shared'
        shutil.copytree(package / '成果', moved)
        # The copied result has no original evidence/Prompt dependency.
        shutil.rmtree(package)
        exported = export_html(moved)
        self.assertTrue(exported)
        self.assertIn('data:image/png', exported[0].read_text())

    def test_collision_normalization_and_linked_destination_refuse_overwrite(self):
        from handouter.organization import course_name
        self.assertEqual(course_name('e\u0301', None), course_name('é', None))
        self.assertNotIn('/', course_name('CON/考试:*?', None))
        first = self.prepare('first', lecture_date='2026-09-10')
        package = first.workspace.parent.parent
        outside = self.root / 'outside'
        outside.mkdir()
        (package / '成果').rmdir()
        (package / '成果').symlink_to(outside, target_is_directory=True)
        (first.workspace / first.handoff.output_path).write_text('# 合成稿\n')
        from handouter.delivery import publish_deliverables
        self.assertEqual(publish_deliverables(first.workspace)['status'], 'failed')
        self.assertEqual(list(outside.iterdir()), [])

    def test_actual_unicode_and_case_collisions_get_separate_directories(self):
        packages = []
        for identity, title in [('a', 'é'), ('b', 'e\u0301'), ('c', 'Lesson'), ('d', 'lesson')]:
            result = prepare_lecture(self.root / 'out', lecture_id=identity, course_title=title,
                transcript=self.text, organized=True, lecture_date='2026-09-10')
            packages.append(result.workspace.parent.parent)
        self.assertEqual(len(set(packages)), 4)
        self.assertTrue(all(path.is_dir() for path in packages))

    def test_cleanup_refuses_a_unique_cached_material_even_via_inner_path(self):
        from handouter.tasks import clean_cache
        result = self.prepare()
        cache = result.workspace.parent.parent / '中间产物_可清理'
        entry = 'one/' + 'a' * 64
        target = cache / '.handouter-cache' / entry
        target.mkdir(parents=True)
        unique = target / 'audio.m4a'
        unique.write_bytes(b'unique synthetic audio, not in formal evidence')
        with self.assertRaises(ValueError):
            clean_cache(cache, entry=entry, confirm=True)
        self.assertTrue(unique.is_file())

    def test_successful_stages_are_in_intermediate_and_cleanup_preserves_evidence(self):
        from handouter.service import build_from_media
        from handouter.asr import ASRResult
        from handouter.media import AudioExtractResult
        from handouter.tasks import inspect_cache, clean_cache
        media = self.root / 'synthetic.mp4'
        media.write_bytes(b'synthetic input')
        def extract(source, output):
            Path(output).write_bytes(b'synthetic decoded audio')
            return AudioExtractResult(Path(output), 1000, 23, 'copy')
        def transcribe(source, output, **kwargs):
            output = Path(output)
            output.mkdir()
            (output / 'raw.json').write_text('{}')
            (output / 'segments.json').write_text('[]')
            (output / 'transcript.txt').write_text('Synthetic 中文课程。', encoding='utf-8')
            return ASRResult(output, output / 'raw.json', output / 'segments.json', output / 'transcript.txt', 0, 'unavailable', 'cpu', 0.1)
        with patch('handouter.doctor.require_features'), patch('handouter.service.extract_audio', side_effect=extract), \
             patch('handouter.service.transcribe_sensevoice', side_effect=transcribe):
            result = build_from_media(self.root / 'out', lecture_id='media', course_title='课程',
                media_source=media, organized=True, lecture_date='2026-09-10', use_slides_as_source=False)
        workspace = result.prepare.workspace
        package = workspace.parent.parent
        before = {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob('*') if p.is_file()}
        entries = inspect_cache(package)['entries']
        self.assertEqual(len(entries), 1)
        self.assertFalse(clean_cache(package, entry=entries[0]['entry'])['deleted'])
        self.assertTrue(clean_cache(package, entry=entries[0]['entry'], confirm=True)['deleted'])
        self.assertEqual(before, {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob('*') if p.is_file()})

    def test_published_history_is_in_agent_protected_inventory(self):
        from handouter.agents.runner import _formal_inventory
        result = self.prepare()
        historical = result.workspace.parent.parent / '成果/old.html'
        historical.write_text('old result')
        before = _formal_inventory(result.workspace)
        historical.write_text('external change')
        self.assertNotEqual(before, _formal_inventory(result.workspace))

    def test_tui_results_find_course_named_upload_directories(self):
        from handouter.agents.web import create_web_handoff
        from handouter.tui import _result_rows
        result = self.prepare(lecture_date='2026-09-10')
        web = create_web_handoff(result.workspace)
        self.assertTrue(any(str(web.directory) in row for row in _result_rows(result.workspace)))
