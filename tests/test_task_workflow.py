"""Task reentry, returned notes and successful-stage reuse using synthetic inputs."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import patch

from handouter.asr import ASRResult
from handouter.media import AudioExtractResult
from handouter.service import prepare_lecture, build_from_media
from handouter.agents.base import AgentAvailability
from handouter.agents.runner import run_cli_agent
from _gates import requires_html


class TaskWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / 'source.txt'
        self.source.write_text('Synthetic 中文说明 x > 0。', encoding='utf-8')
        self.workspace = prepare_lecture(self.root / 'output', lecture_id='task', course_title='Synthetic',
            transcript=self.source, modes=['deep', 'summary'], use_slides_as_source=False).workspace

    def test_partial_cli_continuation_does_not_overwrite_completed_mode(self):
        completed = self.workspace / 'notes/deep-001.md'
        completed.write_text('# Completed\n\nRetain exact bytes.\n')
        before = completed.read_bytes()
        def fake_run(command, **kwargs):
            task = Path(kwargs['cwd'])
            sources = json.loads((task / 'handoff/sources.json').read_text())
            self.assertEqual(sources['expected_outputs'], {'summary': 'notes/summary-001.md'})
            self.assertFalse((task / 'notes/deep-001.md').exists())
            (task / 'notes/summary-001.md').write_text('# Summary\n\nNew.\n')
            return SimpleNamespace(returncode=0)
        with patch('handouter.agents.codex.availability', return_value=AgentAvailability('codex', True, '/fake/codex', 'cli')), \
             patch('handouter.agents.codex.build_command', return_value=['/fake/codex']), \
             patch('handouter.agents.runner.subprocess.run', side_effect=fake_run):
            result = run_cli_agent(self.workspace, agent='codex')
        self.assertTrue(result.validation_ok, result.validation_errors)
        self.assertEqual(completed.read_bytes(), before)

    def test_successful_audio_and_asr_survive_later_prepare_failure(self):
        media_source = self.root / 'video.mp4'
        media_source.write_bytes(b'synthetic media input')
        def audio(source, output):
            Path(output).write_bytes(b'synthetic audio')
            return AudioExtractResult(Path(output), 1000, 15, 'copy')
        def asr(source, output, **kwargs):
            output = Path(output)
            output.mkdir()
            (output / 'transcript.txt').write_text('Synthetic 中文。')
            (output / 'segments.json').write_text(json.dumps([{'id': 'seg-0001', 'text': 'Synthetic 中文。',
                'start_ms': 0, 'end_ms': 1000, 'timestamp_kind': 'sentence_or_vad', 'speaker': None}]))
            (output / 'raw.json').write_text('{}')
            return ASRResult(output, output / 'raw.json', output / 'segments.json', output / 'transcript.txt',
                             1, 'sentence_or_vad', 'cpu', 0.1)
        failed_once = False
        def prepare(*args, **kwargs):
            nonlocal failed_once
            if Path(kwargs['transcript']).name != 'synthetic.txt' and not failed_once:
                failed_once = True
                raise OSError('synthetic downstream failure')
            return prepare_lecture(*args, **kwargs)
        with patch('handouter.doctor.require_features'), \
             patch('handouter.service.extract_audio', side_effect=audio) as extract, \
             patch('handouter.service.transcribe_sensevoice', side_effect=asr) as transcribe, \
             patch('handouter.service.prepare_lecture', side_effect=prepare):
            kwargs = dict(lecture_id='resume', course_title='Synthetic', media_source=media_source,
                          use_slides_as_source=False)
            with self.assertRaises(OSError):
                build_from_media(self.root / 'resumed', **kwargs)
            result = build_from_media(self.root / 'resumed', **kwargs)
        self.assertTrue(result.prepare.workspace.is_dir())
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(transcribe.call_count, 1)

    def test_planner_partial_html_only_and_repeat_completion(self):
        from handouter.tasks import plan_task, retry_html
        from handouter.cli import _continue_payload
        self.assertEqual(plan_task(self.workspace).action, 'generate')
        (self.workspace / 'notes/deep-001.md').write_text('# Deep\n\nComplete.\n')
        self.assertEqual(plan_task(self.workspace).action, 'partial')
        (self.workspace / 'notes/summary-001.md').write_text('# Summary\n\nComplete.\n')
        self.assertEqual(plan_task(self.workspace).action, 'html_only')
        before = {p.name: p.read_bytes() for p in (self.workspace / 'notes').glob('*.md')}
        calls = []
        def export(note):
            calls.append(note.name)
            if len(calls) == 1:
                raise RuntimeError('synthetic HTML failure')
            target = note.with_suffix('.html')
            with target.open('x') as handle:
                handle.write('<!doctype html><html>synthetic</html>')
            return [target]
        with patch('handouter.doctor.require_features'), patch('handouter.html_export.export_html', side_effect=export):
            first = retry_html(self.workspace)
            self.assertEqual(first['status'], 'failed')
            self.assertEqual(plan_task(self.workspace).action, 'html_only')
            self.assertEqual(retry_html(self.workspace)['status'], 'ready')
            self.assertEqual(plan_task(self.workspace).action, 'complete')
            with patch('handouter.cli.run_cli_agent', side_effect=AssertionError('Agent must not run')):
                payload, code = _continue_payload(self.workspace, agent='codex')
            self.assertEqual(code, 0)
            self.assertFalse(payload['agent_was_run'])
        self.assertEqual(calls, ['deep-001.md', 'summary-001.md', 'deep-001.md'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.workspace / 'notes').glob('*.md')})

    def test_web_return_is_bound_to_task_and_preserves_existing_output(self):
        from handouter.tasks import plan_task, import_note
        source = self.root / 'returned.md'
        source.write_text('# Returned\n\nValid course text.\n')
        task_id = plan_task(self.workspace).task_id
        with self.assertRaises(ValueError):
            import_note(self.workspace, source, mode='deep', task_id='different-task', html=False)
        result = import_note(self.workspace, source, mode='deep', task_id=task_id, html=False)
        self.assertEqual(result['status'], 'imported')
        self.assertEqual(plan_task(self.workspace).action, 'partial')
        with self.assertRaises(FileExistsError):
            import_note(self.workspace, source, mode='deep', task_id=task_id, html=False)
        self.assertEqual((self.workspace / result['output']).read_bytes(), source.read_bytes())

    def test_web_placeholder_is_pending_not_a_completed_note(self):
        from handouter.tasks import plan_task, import_note
        source = self.root / 'pending.md'
        source.write_text('# Returned\n\n[插图位置：Slides.pdf 第 1 页]\n')
        result = import_note(self.workspace, source, mode='deep', task_id=plan_task(self.workspace).task_id, html=False)
        self.assertEqual(result['status'], 'needs_images')
        self.assertTrue((self.workspace / result['draft']).is_file())
        self.assertFalse((self.workspace / 'notes/deep-001.md').exists())

    def test_pdf_placement_placeholder_is_not_validated_as_final_output(self):
        from handouter.validation import validate_note_output
        (self.workspace / 'notes/deep-001.md').write_text('# Draft\n\n[插图位置：Slides.pdf 第 1 页]\n')
        self.assertFalse(validate_note_output(self.workspace, output='notes/deep-001.md').ok)

    def test_incomplete_agent_run_preserves_valid_mode_for_next_attempt(self):
        def fake_run(command, **kwargs):
            task = Path(kwargs['cwd'])
            (task / 'notes/deep-001.md').write_text('# Deep\n\nComplete.\n')
            return SimpleNamespace(returncode=0)
        with patch('handouter.agents.codex.availability', return_value=AgentAvailability('codex', True, '/fake/codex', 'cli')), \
             patch('handouter.agents.codex.build_command', return_value=['/fake/codex']), \
             patch('handouter.agents.runner.subprocess.run', side_effect=fake_run):
            result = run_cli_agent(self.workspace, agent='codex')
        self.assertFalse(result.validation_ok)
        self.assertTrue((self.workspace / 'notes/deep-001.md').is_file())
        self.assertEqual(json.loads((self.workspace / 'state.json').read_text())['agent_output']['status'], 'partial')

    def test_browser_asset_identity_is_provided_by_the_configured_plugin(self):
        from handouter.tasks import asset_fingerprint
        path = self.root / 'browser.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('opaque.dat', 'browser data')
        details = {'source_sha256': 'a' * 64, 'source_fingerprint': 'b' * 64}
        with patch('handouter.external_collector.inspect_browser_export', return_value=details) as inspect:
            self.assertEqual(asset_fingerprint(path), 'b' * 64)
            inspect.assert_called_once_with(path)

    def test_config_input_change_requires_explicit_new_task(self):
        from handouter.tasks import plan_task
        self.assertEqual(plan_task(self.workspace, selection={'modes': ['full']}).action, 'refresh_required')
        self.assertEqual(plan_task(self.workspace, input_fingerprint='new-material').action, 'refresh_required')

    def test_partial_web_and_manual_only_request_missing_mode(self):
        from handouter.cli import _continue_payload
        (self.workspace / 'notes/deep-001.md').write_text('# Existing\n\nKeep.\n')
        web, code = _continue_payload(self.workspace, agent='web')
        self.assertEqual(code, 0)
        self.assertEqual(web['agent_interaction']['expected_outputs'], {'summary': 'notes/summary-001.md'})
        manual, code = _continue_payload(self.workspace, agent='manual')
        self.assertEqual(code, 0)
        with zipfile.ZipFile(manual['agent_interaction']['bundle']) as archive:
            sources = json.loads(archive.read('handoff/sources.json'))
            self.assertEqual(sources['expected_outputs'], {'summary': 'notes/summary-001.md'})
            self.assertNotIn('notes/deep-001.md', archive.namelist())

    def test_cache_tampering_is_rejected_and_cleanup_is_explicit(self):
        from handouter.stage_cache import MaterialCache
        from handouter.tasks import inspect_cache, clean_cache
        root = self.root / 'cache-root'
        root.mkdir()
        cache = MaterialCache(root, 'lecture', 'synthetic', {'device': 'cpu'})
        def extract(source, output):
            Path(output).write_bytes(b'complete audio')
            return AudioExtractResult(Path(output), 1000, 14, 'copy')
        audio = cache.audio('unused', extract)
        entry = inspect_cache(root)['entries'][0]['entry']
        self.assertFalse(clean_cache(root, entry=entry)['deleted'])
        self.assertTrue(audio.output.exists())
        audio.output.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            cache.audio('unused', lambda *args: self.fail('corrupt cache must not be reused'))
        with self.assertRaises(ValueError):
            clean_cache(root, entry='../outside', confirm=True)
        self.assertTrue(clean_cache(root, entry=entry, confirm=True)['deleted'])
        self.assertFalse(audio.output.exists())

    def test_missing_html_dependency_preserves_import_and_records_retry(self):
        from handouter.tasks import plan_task, import_note, retry_html
        source = self.root / 'returned.md'
        source.write_text('# Returned\n\nValid content.\n')
        task_id = plan_task(self.workspace).task_id
        with patch('handouter.doctor.require_features', side_effect=RuntimeError('missing html extra')), \
             patch('handouter.html_export.export_html') as export:
            result = import_note(self.workspace, source, mode='deep', task_id=task_id)
            self.assertEqual(result['status'], 'imported')
            self.assertEqual(result['html_delivery']['status'], 'failed')
            self.assertIn('retry-html', result['html_delivery']['errors'][0])
            self.assertEqual(retry_html(self.workspace)['status'], 'failed')
            export.assert_not_called()
        self.assertEqual((self.workspace / result['output']).read_bytes(), source.read_bytes())
        state = json.loads((self.workspace / 'state.json').read_text())
        self.assertEqual(state['html_delivery']['status'], 'failed')
        self.assertEqual(plan_task(self.workspace).action, 'partial')

    @requires_html

    def test_actual_web_import_and_offline_html(self):
        from handouter.tasks import plan_task, import_note, retry_html
        source = self.root / 'returned.md'
        source.write_text('# Returned\n\n$\\frac{1}{\\sqrt{x}}$\n', encoding='utf-8')
        task_id = plan_task(self.workspace).task_id
        import_note(self.workspace, source, mode='deep', task_id=task_id, html=False)
        import_note(self.workspace, source, mode='summary', task_id=task_id, html=True)
        self.assertEqual(retry_html(self.workspace)['status'], 'ready')
        self.assertEqual(plan_task(self.workspace).action, 'complete')
        self.assertIn('<math', (self.workspace / 'notes/deep-001.html').read_text())
