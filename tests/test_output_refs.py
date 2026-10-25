"""Output location evidence, never a fuzzy match or semantic verdict."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from handouter.output_refs import resolve_output_ref


class OutputRefTests(unittest.TestCase):
    def setUp(self):
        self.text = '重复 😀 e\u0301\r\n重复\n尾'
        self.data = self.text.encode()
        self.digest = hashlib.sha256(self.data).hexdigest()

    def ref(self, kind='output_span', start=0, end=2):
        return dict(kind=kind, output_sha256=self.digest, start=start, end=end)

    def test_exact_unicode_crlf_repeated_and_lines(self):
        second = self.text.index('重复', 1)
        value = resolve_output_ref(self.data, self.ref(start=second, end=second + 2))
        self.assertEqual((value.start, value.text), (second, '重复'))
        value = resolve_output_ref(self.data, self.ref('output_lines', 1, 2))
        self.assertEqual(value.text, self.text[:second + 2])
        self.assertEqual(resolve_output_ref(self.data, self.ref(start=3, end=4)).text, '😀')
        self.assertEqual(resolve_output_ref(self.data, self.ref(start=2, end=2)).text, '')

    def test_hash_bounds_bool_and_unknown(self):
        with self.assertRaisesRegex(ValueError, 'OUTPUT_CHANGED'):
            resolve_output_ref(self.data + b'!', self.ref())
        for ref in [self.ref(start=True), self.ref(start=-1), self.ref(end=999),
                    self.ref('output_lines', 0, 1), self.ref('output_lines', 1, 999),
                    {**self.ref(), 'extra': 1}, self.ref('paragraph')]:
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                resolve_output_ref(self.data, ref)

    def test_composer_requires_checked_sidecar_and_unique_object(self):
        ref = dict(kind='composer_object', output_sha256=self.digest, object_id='e1')
        kwargs = dict(root='root', sources={}, mode='complete', plan={})
        entry = dict(id='e1', kind='excerpt', output_span=[0, 2])
        with patch('handouter.composition_store.checked_composition', return_value={'objects': [entry]}) as checker:
            self.assertEqual(resolve_output_ref(self.data, ref, **kwargs).text, '重复')
            checker.assert_called_once_with('root', {}, self.data, 'complete', {})
        with patch('handouter.composition_store.checked_composition', side_effect=ValueError('COMPOSER_COMPOSITION_MISMATCH')):
            with self.assertRaises(ValueError):
                resolve_output_ref(self.data, ref, **kwargs)
        with patch('handouter.composition_store.checked_composition', return_value={'objects': [entry, entry]}):
            with self.assertRaisesRegex(ValueError, 'AMBIGUOUS'):
                resolve_output_ref(self.data, ref, **kwargs)

    def test_actual_host_sidecar_roundtrip_and_tamper(self):
        from handouter.composer import compose_with_manifest
        from handouter.composition_store import save_composition, manifest_path
        from handouter.source_refs import source_hashes
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'transcript').mkdir()
            (root / 'transcript/transcript.txt').write_bytes(self.data)
            sources = {'transcript': {'path': 'transcript/transcript.txt'},
                       'authoring': {'task_key': 'synthetic', 'task_modes': ['complete']}}
            sources['authoring']['source_hashes'] = source_hashes(root, sources)
            objects = {'excerpts': {'e1': {'ref': 'transcript:L1-L1'}},
                       'figures': {}, 'sources': {}, 'details': {}}
            raw = '前言。\n\n[[excerpt:e1]]\n'
            result, manifest = compose_with_manifest(raw, root, sources, objects,
                                                     plan_revision=1, plan_hash='a' * 64)
            save_composition(root, sources, raw.encode(), manifest)
            ref = dict(kind='composer_object', output_sha256=manifest['output_sha256'], object_id='e1')
            kwargs = dict(root=root, sources=sources, mode='complete',
                          plan={'revision': 1, 'plan_hash': 'a' * 64, 'objects': objects})
            resolved = resolve_output_ref(result.encode(), ref, **kwargs)
            self.assertEqual(resolved.text, result[slice(*manifest['objects'][0]['output_span'])])
            path = root / manifest_path(sources, manifest['output_sha256'])
            manifest['objects'][0]['output_span'][0] += 1
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'COMPOSITION_MISMATCH'):
                resolve_output_ref(result.encode(), ref, **kwargs)
