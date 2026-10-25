"""Per-mode HTML requests, using only temporary synthetic course packages."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from handouter.cli import _continue_payload
from handouter.delivery import publication_current, publish_deliverables
from handouter.service import prepare_lecture
from handouter.tasks import import_note, plan_task, record_html_exports, retry_html


class HTMLRequestPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        source = self.root / 'source.txt'
        source.write_text('Synthetic course text.', encoding='utf-8')
        self.workspace = prepare_lecture(
            self.root / 'out', lecture_id='html-policy', course_title='Synthetic',
            transcript=source, modes=['deep', 'summary'],
            use_slides_as_source=False, organized=True).workspace
        self.outputs = json.loads(
            (self.workspace / 'handoff/sources.json').read_text())['expected_outputs']
        self.returned = self.root / 'returned.md'
        self.returned.write_text('# Synthetic\n\nCourse content.\n', encoding='utf-8')

    def state(self):
        return json.loads((self.workspace / 'state.json').read_text())

    def import_without_html(self, mode):
        return import_note(self.workspace, self.returned, mode=mode,
                           task_id=plan_task(self.workspace).task_id, html=False)

    def write_note(self, mode):
        note = self.workspace / self.outputs[mode]
        note.write_bytes(self.returned.read_bytes())
        return note

    def fake_export(self, note):
        target = note.with_suffix('.html')
        target.write_text('<!doctype html><p>Synthetic</p>', encoding='utf-8')
        return [target]

    def test_all_opted_out_complete_without_dependencies_or_export(self):
        with patch('handouter.doctor.require_features', side_effect=AssertionError('No HTML dependencies')), \
             patch('handouter.html_export.export_html', side_effect=AssertionError('No export')), \
             patch('handouter.cli.run_cli_agent', side_effect=AssertionError('No model')):
            for mode in self.outputs:
                result = self.import_without_html(mode)
                self.assertEqual(result['html_delivery']['status'], 'not_requested')
                self.assertEqual(result['semantic_review'], 'required')
            self.assertEqual(self.state()['html_delivery']['requested'],
                             {'deep': False, 'summary': False})
            self.assertEqual(plan_task(self.workspace).action, 'complete')
            payload, code = _continue_payload(self.workspace)
            self.assertEqual(code, 0, payload)
            self.assertEqual(payload['status'], 'complete')
            self.assertEqual(payload['semantic_review'], 'required')
            self.assertFalse(payload['agent_was_run'])
            publication = publish_deliverables(self.workspace)
            self.assertEqual(publication['status'], 'ready', publication)
            self.assertEqual(publication['semantic_review'], 'required')
            self.assertTrue(publication_current(self.workspace))
        self.assertEqual(self.state()['agent_output']['semantic_review'], 'required')
        self.assertFalse(list(self.workspace.rglob('*.html')))
        for mode, relative in self.outputs.items():
            self.assertEqual(set(publication['outputs'][mode]), {'markdown'})
            published = self.workspace.parent.parent / '成果' / relative
            self.assertEqual(published.read_bytes(), self.returned.read_bytes())
        published.write_text('User edit', encoding='utf-8')
        self.assertFalse(publication_current(self.workspace))
        self.assertEqual(plan_task(self.workspace).action, 'publish_only')

    def test_opt_out_preserves_other_mode_request_and_export_records(self):
        note = self.write_note('summary')
        html = self.fake_export(note)[0]
        record_html_exports(self.workspace, [(note, html)])
        before = self.state()['html_delivery']
        result = self.import_without_html('deep')
        after = self.state()['html_delivery']
        self.assertEqual(after['requested'], {'summary': True, 'deep': False})
        for key in ('outputs', 'hashes', 'note_hashes'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(result['publication']['status'], 'ready', result)
        self.assertTrue(publication_current(self.workspace))
        self.assertEqual(plan_task(self.workspace).action, 'complete')

    def test_opt_out_ignores_stale_html_without_deleting_records(self):
        for mode in self.outputs:
            self.import_without_html(mode)
        state = self.state()
        state['html_delivery'].update(
            outputs={'deep': 'notes/missing.html'}, hashes={'deep': 'stale'},
            note_hashes={'deep': 'stale'})
        (self.workspace / 'state.json').write_text(json.dumps(state), encoding='utf-8')
        self.assertEqual(publish_deliverables(self.workspace)['status'], 'ready')
        self.assertTrue(publication_current(self.workspace))
        self.assertEqual(plan_task(self.workspace).action, 'complete')
        self.assertEqual(self.state()['html_delivery'], state['html_delivery'])

    def test_unrelated_reading_copy_export_preserves_task_state(self):
        for mode in self.outputs:
            self.import_without_html(mode)
        before = (self.workspace / 'state.json').read_bytes()
        note = self.workspace / 'notes/reading.typora-001.md'
        note.write_text('# Separate reading copy\n', encoding='utf-8')
        html = self.fake_export(note)[0]
        record_html_exports(self.workspace, [(note, html)])
        self.assertEqual((self.workspace / 'state.json').read_bytes(), before)

    def test_record_exports_reopens_only_matching_mode(self):
        for mode in self.outputs:
            self.import_without_html(mode)
        note = self.workspace / self.outputs['deep']
        html = self.fake_export(note)[0]
        record_html_exports(self.workspace, [(note, html)])
        delivery = self.state()['html_delivery']
        self.assertEqual(delivery['requested'], {'deep': True, 'summary': False})
        self.assertEqual(delivery['hashes']['deep'], hashlib.sha256(html.read_bytes()).hexdigest())
        self.assertEqual(plan_task(self.workspace).action, 'complete')
        html.write_text('Changed', encoding='utf-8')
        self.assertEqual(plan_task(self.workspace).action, 'html_only')

    def test_explicit_retry_reopens_only_selected_mode_even_if_dependencies_fail(self):
        for mode in self.outputs:
            self.import_without_html(mode)
        with patch('handouter.doctor.require_features', side_effect=RuntimeError('Synthetic missing dependency')), \
             patch('handouter.html_export.export_html') as export:
            result = retry_html(self.workspace, modes=['deep'])
        export.assert_not_called()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.state()['html_delivery']['requested'],
                         {'deep': True, 'summary': False})
        self.assertEqual(plan_task(self.workspace).action, 'html_only')
        self.assertFalse(publication_current(self.workspace))

    def test_legacy_without_policy_still_requires_all_html(self):
        for mode in self.outputs:
            self.write_note(mode)
        self.assertNotIn('requested', self.state().get('html_delivery', {}))
        self.assertEqual(plan_task(self.workspace).action, 'html_only')
        with patch('handouter.doctor.require_features'), \
             patch('handouter.html_export.export_html', side_effect=self.fake_export) as export:
            result = retry_html(self.workspace)
        self.assertEqual(result['status'], 'ready', result)
        self.assertEqual({call.args[0].relative_to(self.workspace).as_posix()
                          for call in export.call_args_list}, set(self.outputs.values()))
        self.assertEqual(plan_task(self.workspace).action, 'complete')

    def test_default_import_still_requests_html(self):
        with patch('handouter.doctor.require_features'), \
             patch('handouter.html_export.export_html', side_effect=self.fake_export) as export:
            result = import_note(self.workspace, self.returned, mode='deep',
                                 task_id=plan_task(self.workspace).task_id)
        self.assertEqual(result['html_delivery']['status'], 'ready', result)
        export.assert_called_once_with(self.workspace / self.outputs['deep'])
        self.assertTrue(self.state()['html_delivery']['requested']['deep'])

    def test_automatic_retry_exports_only_requested_mode(self):
        self.import_without_html('deep')
        self.write_note('summary')
        self.assertEqual(plan_task(self.workspace).action, 'html_only')
        with patch('handouter.doctor.require_features'), \
             patch('handouter.html_export.export_html', side_effect=self.fake_export) as export, \
             patch('handouter.cli.run_cli_agent', side_effect=AssertionError('No model')):
            result = retry_html(self.workspace, requested_only=True)
        self.assertEqual(result['status'], 'ready', result)
        export.assert_called_once_with(self.workspace / self.outputs['summary'])
        self.assertEqual(self.state()['html_delivery']['requested'],
                         {'deep': False, 'summary': True})
        self.assertFalse((self.workspace / self.outputs['deep']).with_suffix('.html').exists())
        self.assertEqual(plan_task(self.workspace).action, 'complete')

    def test_cli_mixed_mode_continuation_respects_opt_out(self):
        self.import_without_html('deep')
        self.write_note('summary')
        with patch('handouter.doctor.require_features'), \
             patch('handouter.html_export.export_html', side_effect=self.fake_export) as export, \
             patch('handouter.cli.run_cli_agent', side_effect=AssertionError('No model')):
            payload, code = _continue_payload(self.workspace)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload['status'], 'complete')
        export.assert_called_once_with(self.workspace / self.outputs['summary'])
        self.assertEqual(self.state()['html_delivery']['requested'], {'deep': False, 'summary': True})

    def test_malformed_request_policy_fails_closed(self):
        for mode in self.outputs:
            self.write_note(mode)
        state = self.state()
        state['html_delivery'] = {'requested': []}
        (self.workspace / 'state.json').write_text(json.dumps(state), encoding='utf-8')
        self.assertEqual(plan_task(self.workspace).action, 'invalid')
        self.assertFalse(publication_current(self.workspace))

    def test_empty_retry_does_not_check_dependencies_or_generate_html(self):
        for mode in self.outputs:
            self.import_without_html(mode)
        with patch('handouter.doctor.require_features', side_effect=AssertionError('No HTML dependencies')), \
             patch('handouter.html_export.export_html', side_effect=AssertionError('No export')):
            result = retry_html(self.workspace, requested_only=True)
        self.assertEqual(result['status'], 'not_requested', result)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['requested'], {'deep': False, 'summary': False})
        self.assertEqual(plan_task(self.workspace).action, 'complete')

    def test_explicit_retry_preserves_other_export_and_opt_out(self):
        for mode in self.outputs:
            self.import_without_html(mode)
        summary = self.workspace / self.outputs['summary']
        summary_html = self.fake_export(summary)[0]
        record_html_exports(self.workspace, [(summary, summary_html)])
        before = self.state()['html_delivery']

        def export_and_record(note):
            html = self.fake_export(note)[0]
            record_html_exports(self.workspace, [(note, html)])
            return [html]

        with patch('handouter.doctor.require_features'), \
             patch('handouter.html_export.export_html', side_effect=export_and_record) as export:
            result = retry_html(self.workspace, modes=['deep'])
        self.assertEqual(result['status'], 'ready', result)
        export.assert_called_once_with(self.workspace / self.outputs['deep'])
        after = self.state()['html_delivery']
        self.assertEqual(after['requested'], {'deep': True, 'summary': True})
        for key in ('outputs', 'hashes', 'note_hashes'):
            self.assertEqual(after[key]['summary'], before[key]['summary'])
        self.assertEqual(plan_task(self.workspace).action, 'complete')

    def test_explicit_retry_without_modes_reopens_all_modes(self):
        for mode in self.outputs:
            self.import_without_html(mode)
        with patch('handouter.doctor.require_features'), \
             patch('handouter.html_export.export_html', side_effect=self.fake_export) as export:
            result = retry_html(self.workspace)
        self.assertEqual(result['status'], 'ready', result)
        self.assertEqual(export.call_count, 2)
        self.assertEqual(self.state()['html_delivery']['requested'],
                         {'deep': True, 'summary': True})


if __name__ == '__main__':
    unittest.main()
