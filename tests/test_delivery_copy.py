import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from handouter.delivery import publish_deliverables


class DeliveryCopyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.collection = self.base / '课程-日期待确认'
        self.workspace = self.collection / '资料与交接' / 'lecture-id'
        for path in ('notes', 'handoff', 'slides/images'):
            (self.workspace / path).mkdir(parents=True, exist_ok=True)
        from handouter.organization import course_name
        base = course_name('课程', None)
        self.organization = {'version': 2, 'lecture_id': 'lecture-id', 'course_title': '课程',
            'lecture_date': None, 'base_name': base, 'name_prefix': '课程_授课日期待确认'}
        self.write_json('manifest.json', {'lecture_id': 'lecture-id', 'output_organization': self.organization})
        (self.collection / '.handouter-collection.json').write_text(json.dumps(self.organization))
        self.write_json('state.json', {})
        self.write_json('handoff/sources.json', {'mode': 'deep', 'expected_outputs': {'deep': 'notes/课程-deep-001.md'},
            'options': {'use_slides_as_source': True, 'embed_slides': True}})
        self.note = self.workspace / 'notes/课程-deep-001.md'
        self.note.write_text('# 合成讲义\n\n![图](../slides/images/a.png)\n\n```md\n![不是图片](missing.png)\n```\n', encoding='utf-8')
        (self.workspace / 'slides/images/a.png').write_bytes(b'synthetic image')

    def write_json(self, relative, data):
        (self.workspace / relative).write_text(json.dumps(data), encoding='utf-8')

    def test_portable_copy_preserves_exact_markdown_and_only_used_resources(self):
        (self.workspace / 'slides/images/unused.png').write_bytes(b'unused')
        result = publish_deliverables(self.workspace)
        self.assertEqual(result['status'], 'ready', result)
        self.assertEqual(result['semantic_review'], 'required')
        copied = self.base / 'copied'
        shutil.copytree(self.collection / '成果', copied)
        self.assertEqual((copied / 'notes' / self.note.name).read_bytes(), self.note.read_bytes())
        self.assertEqual((copied / 'slides/images/a.png').read_bytes(), b'synthetic image')
        self.assertFalse((copied / 'slides/images/unused.png').exists())
        self.assertEqual(publish_deliverables(self.workspace)['status'], 'ready')

    def test_existing_different_bytes_never_overwritten(self):
        publish_deliverables(self.workspace)
        target = self.collection / '成果/notes' / self.note.name
        target.write_text('User edit')
        result = publish_deliverables(self.workspace)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(target.read_text(), 'User edit')
        self.assertTrue(self.note.read_text().startswith('# 合成'))

    def test_symlink_destination_and_source_are_rejected(self):
        outside = self.base / 'outside'
        outside.mkdir()
        try:
            (self.collection / '成果').symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            if getattr(exc, 'winerror', None) == 1314:
                self.skipTest('symlinks unavailable')
            raise
        self.assertEqual(publish_deliverables(self.workspace)['status'], 'failed')
        self.assertEqual(list(outside.iterdir()), [])
        (self.collection / '成果').unlink()
        image = self.workspace / 'slides/images/a.png'
        image.unlink()
        image.symlink_to(self.note)
        self.assertEqual(publish_deliverables(self.workspace)['status'], 'failed')

    def test_only_hash_bound_html_is_copied(self):
        html = b'<!doctype html><p>synthetic</p>'
        (self.workspace / 'notes/课程-deep-001.html').write_bytes(html)
        self.write_json('state.json', {'html_delivery': {'outputs': {'deep': 'notes/课程-deep-001.html'},
            'hashes': {'deep': hashlib.sha256(html).hexdigest()},
            'note_hashes': {'deep': hashlib.sha256(self.note.read_bytes()).hexdigest()}}})
        self.assertEqual(publish_deliverables(self.workspace)['status'], 'ready')
        self.assertEqual((self.collection / '成果/notes/课程-deep-001.html').read_bytes(), html)
        (self.workspace / 'notes/课程-deep-001.html').write_bytes(b'tampered')
        self.assertEqual(publish_deliverables(self.workspace)['status'], 'partial')

    def test_legacy_workspace_is_untouched(self):
        self.write_json('manifest.json', {})
        result = publish_deliverables(self.workspace)
        self.assertEqual(result['outputs'], {})
        self.assertFalse((self.collection / '成果').exists())

    def test_remote_image_cannot_claim_offline_delivery(self):
        self.note.write_text('# Synthetic\n![remote](https://example.org/image.png)')
        self.write_json('handoff/sources.json', {'mode': 'deep', 'expected_outputs': {'deep': 'notes/课程-deep-001.md'},
            'options': {'allow_web': True}})
        result = publish_deliverables(self.workspace)
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(result['offline_complete'])

    def test_collection_binding_and_note_local_images(self):
        local = self.workspace / 'notes/local.png'
        local.write_bytes(b'local synthetic')
        self.note.write_text('# Synthetic\n![local](local.png)')
        result = publish_deliverables(self.workspace)
        self.assertEqual(result['status'], 'ready', result)
        self.assertEqual((self.collection / '成果/notes/local.png').read_bytes(), local.read_bytes())
        (self.collection / '.handouter-collection.json').write_text(json.dumps({'version': 2, 'lecture_id': 'other', 'name_prefix': '课程_授课日期待确认'}))
        self.assertEqual(publish_deliverables(self.workspace)['status'], 'failed')

    def test_missing_modes_are_pending_without_copy_errors(self):
        self.write_json('handoff/sources.json', {'expected_outputs': {
            'deep': 'notes/课程-deep-001.md', 'summary': 'notes/课程-summary-001.md'},
            'options': {'use_slides_as_source': True, 'embed_slides': True}})
        result = publish_deliverables(self.workspace)
        self.assertEqual(result['status'], 'partial', result)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['pending'], {'summary': 'notes/课程-summary-001.md'})
        self.note.unlink()
        result = publish_deliverables(self.workspace)
        self.assertEqual(result['status'], 'partial', result)
        self.assertEqual(result['errors'], [])
        self.assertEqual(len(result['pending']), 2)

    def test_existing_invalid_note_is_not_pending(self):
        self.note.write_text('')
        result = publish_deliverables(self.workspace)
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['errors'])
        self.assertEqual(result['pending'], {})

    def test_incomplete_collection_marker_is_rejected_before_copy(self):
        marker = dict(self.organization)
        marker.pop('course_title')
        marker.pop('base_name')
        (self.collection / '.handouter-collection.json').write_text(json.dumps(marker))
        result = publish_deliverables(self.workspace)
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['outputs'], {})
        self.assertFalse((self.collection / '成果').exists())


if __name__ == '__main__':
    unittest.main()
