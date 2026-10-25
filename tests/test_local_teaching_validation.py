"""Local warnings use source-safe numeric evidence without rewriting drafts."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from handouter.note_validation import validate_note_output


class LocalTeachingValidationTests(unittest.TestCase):
    def test_mismatch_is_advisory_and_original_file_and_source_are_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / 'notes').mkdir()
            (root / 'handoff').mkdir()
            (root / 'state.json').write_text('{}')
            sources = {'mode': 'deep', 'expected_output': 'notes/deep-001.md',
                       'options': {'format_profile': 'clean', 'use_slides_as_source': False, 'embed_slides': False}}
            (root / 'handoff/sources.json').write_text(json.dumps(sources))
            note = root / 'notes/deep-001.md'
            body = '# 合成例子\n\n$$25+2\\times35+15=145$$\n'
            note.write_text(body, encoding='utf-8')
            before = {p: p.read_bytes() for p in (note, root / 'state.json', root / 'handoff/sources.json')}
            result = validate_note_output(root, update_state=False)
            self.assertTrue(result.ok, result.errors)
            self.assertTrue(any('可复算' in text for text in result.warnings))
            self.assertEqual(result.sha256, hashlib.sha256(before[note]).hexdigest())
            self.assertTrue(all(p.read_bytes() == data for p, data in before.items()))
            note.write_text('# 原话示例\n\n> 25+2*35+15=145\n', encoding='utf-8')
            quoted = validate_note_output(root, update_state=False)
            self.assertFalse(any('可复算' in text for text in quoted.warnings))


if __name__ == '__main__':
    unittest.main()
