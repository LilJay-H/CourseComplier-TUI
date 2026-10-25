"""Synthetic source provenance checks for acceptance cases D06–D14.

These checks prove exact location and input preservation, never semantic support.
The existing Complete quote tests retain ownership of D15's quote policy.
"""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from handouter.source_refs import SourceRefError, resolve_ref, source_hashes


class SourceRefsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'transcript').mkdir()
        (self.root / 'slides').mkdir()
        self.transcript = self.root / 'transcript/transcript.txt'
        self.transcript.write_text('第一行\nsecond line\n', encoding='utf-8')
        self.sources = {
            'transcript': {'path': 'transcript/transcript.txt',
                           'segments_path': 'transcript/segments.json',
                           'origin': 'provided_transcript'},
            'slides': {'index_path': 'slides/index.json'},
            'options': {'use_slides_as_source': True},
        }
        self.write_json('transcript/segments.json', [])
        self.write_json('slides/index.json', [])

    def write_json(self, path, data):
        (self.root / path).write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def resolve(self, ref, **kwargs):
        return resolve_ref(self.root, self.sources, ref, **kwargs)

    def assert_code(self, code, ref, **kwargs):
        with self.assertRaises(SourceRefError) as caught:
            self.resolve(ref, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    @staticmethod
    def span(start, end):
        return {'kind': 'transcript_span', 'source_id': 'transcript', 'start': start, 'end': end}

    @staticmethod
    def item(kind, identity):
        return {'kind': kind, 'source_id': 'segments' if kind == 'segment' else 'slides', 'id': identity}

    def test_unicode_codepoints_include_emoji_and_combining_marks(self):
        text = '中A😀e\u0301👩\u200d💻文'
        self.transcript.write_bytes(text.encode('utf-8'))
        for start, end, expected in [(0, 2, '中A'), (2, 3, '😀'), (3, 5, 'e\u0301'),
                                     (4, 5, '\u0301'), (5, 8, '👩\u200d💻')]:
            with self.subTest(start=start, end=end):
                self.assertEqual(self.resolve(self.span(start, end)).text, expected)

    def test_lines_bom_crlf_blank_lines_long_line_preserve_input_bytes(self):
        long_line = '长' * 70000
        data = ('\ufeff甲\r\n\r\n' + long_line + '\n尾\n').encode('utf-8')
        self.transcript.write_bytes(data)
        self.assertEqual(self.resolve('transcript:L1-L2').text, '甲\n')
        self.assertEqual(self.resolve('transcript:L3-L3').text, long_line)
        result = self.resolve('transcript:L4-L4')
        self.assertEqual(result.text, '尾')
        self.assertEqual(result.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(self.resolve(self.span(0, 5)).text, '甲\r\n\r\n')
        self.assertEqual(self.transcript.read_bytes(), data)

    def test_no_unicode_case_or_punctuation_normalization(self):
        text = 'é e\u0301 Ａ A a，,\r\n'
        self.transcript.write_bytes(text.encode('utf-8'))
        self.assertEqual(self.resolve(self.span(0, len(text))).text, text)
        self.assertEqual(self.resolve('transcript:L1-L1').text, text[:-2])

    def test_explicit_range_errors_for_bool_negative_reversed_and_bounds(self):
        for start, end in [(True, 2), (0, True), (-1, 2), (2, 1),
                           (0, 1000), ('0', 2), (0.0, 2)]:
            with self.subTest(start=start, end=end):
                self.assert_code('REF_OUT_OF_RANGE', self.span(start, end))
        for start, end in [(0, 1), (True, 1), (1, False), (2, 1), (1, 3)]:
            with self.subTest(line_start=start, line_end=end):
                self.assert_code('REF_OUT_OF_RANGE', {
                    'kind': 'transcript_lines', 'source_id': 'transcript', 'start': start, 'end': end})
        self.assert_code('REF_OUT_OF_RANGE', 'transcript:L2-L1')

    def test_segment_missing_and_duplicate_ids_are_not_synthesized(self):
        self.write_json('transcript/segments.json', [{'text': '没有 ID'},
                        {'id': 'same', 'text': '甲'}, {'id': 'same', 'text': '乙'}])
        self.assert_code('SOURCE_MISSING', self.item('segment', '1'))
        self.assert_code('REF_AMBIGUOUS', self.item('segment', 'same'))

    def test_txt_and_segments_keep_distinct_text_and_hashes(self):
        self.write_json('transcript/segments.json', [{'id': 'real-id', 'text': '段内另一个版本'}])
        text_ref = self.resolve('transcript:L1-L1')
        segment_ref = self.resolve(self.item('segment', 'real-id'))
        self.assertEqual(text_ref.text, '第一行')
        self.assertEqual(segment_ref.text, '段内另一个版本')
        self.assertNotEqual(text_ref.source_id, segment_ref.source_id)
        self.assertNotEqual(text_ref.sha256, segment_ref.sha256)

    def test_unknown_and_discrete_segment_times_are_not_interpolated(self):
        self.write_json('transcript/segments.json', [
            {'id': 'unknown', 'text': '未知'},
            {'id': 'early', 'text': '前段', 'start_ms': 0, 'end_ms': 100},
            {'id': 'later', 'text': '后段', 'start_ms': 900, 'end_ms': 1000},
            {'id': 'invalid', 'text': '错误时间', 'start_ms': True, 'end_ms': 100},
        ])
        self.assertEqual(self.resolve(self.item('segment', 'unknown')).time_intervals, ())
        self.assertEqual(self.resolve(self.item('segment', 'invalid')).time_intervals, ())
        intervals = tuple(interval for identity in ('early', 'later')
                          for interval in self.resolve(self.item('segment', identity)).time_intervals)
        self.assertEqual(intervals, ((0, 100), (900, 1000)))

    def test_repeated_image_occurrences_remain_separate_events(self):
        self.write_json('slides/index.json', [
            {'occurrence_id': 'first', 'image_path': 'slides/images/a.jpg', 'start_ms': 0, 'end_ms': 10},
            {'occurrence_id': 'return', 'image_path': 'slides/images/a.jpg', 'start_ms': 90, 'end_ms': 100},
        ])
        first = self.resolve(self.item('slide_occurrence', 'first'))
        returned = self.resolve(self.item('slide_occurrence', 'return'))
        self.assertEqual(first.image_path, returned.image_path)
        self.assertEqual(first.time_intervals, ((0, 10),))
        self.assertEqual(returned.time_intervals, ((90, 100),))

    def test_generated_note_is_usable_but_cannot_certify_original_speech(self):
        self.sources['transcript']['origin'] = 'generated_note'
        self.assertEqual(self.resolve('transcript:L1-L1').origin, 'generated_note')
        self.assert_code('SOURCE_IDENTITY_INVALID', 'transcript:L1-L1', require_transcript=True)
        self.assert_code('SOURCE_IDENTITY_INVALID', self.item('segment', 'any'), require_transcript=True)
        self.assert_code('SOURCE_IDENTITY_INVALID', self.item('slide_occurrence', 'any'), require_transcript=True)

    def test_missing_unsafe_paths_and_extra_path_fields_fail_closed(self):
        self.assert_code('REF_INVALID', dict(self.span(0, 1), path='../secret'))
        for path in ('../secret', '/private/secret', 'transcript/other.txt'):
            with self.subTest(path=path):
                self.sources['transcript']['path'] = path
                self.assert_code('SOURCE_UNSAFE', 'transcript:L1-L1')
        self.sources['transcript']['path'] = 'transcript/transcript.txt'
        self.transcript.unlink()
        self.assert_code('SOURCE_MISSING', 'transcript:L1-L1')

    def test_symlink_source_is_rejected(self):
        target = self.root / 'target.txt'
        target.write_text('not a registered source', encoding='utf-8')
        self.transcript.unlink()
        try:
            self.transcript.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')
        self.assert_code('SOURCE_UNSAFE', 'transcript:L1-L1')

    def test_hash_change_is_visible_without_relocating_repeated_or_similar_text(self):
        self.transcript.write_text('相同句\n相同句\n相似句\n', encoding='utf-8')
        hashes = source_hashes(self.root, self.sources)
        self.assertEqual(self.resolve('transcript:L2-L2', expected_hashes=hashes).text, '相同句')
        self.assertEqual(self.resolve('transcript:L3-L3', expected_hashes=hashes).text, '相似句')
        self.transcript.write_text('新增\n相同句\n相同句\n相似句\n', encoding='utf-8')
        self.assert_code('SOURCE_CHANGED', 'transcript:L2-L2', expected_hashes=hashes)

    def test_slides_not_registered_when_not_selected(self):
        sources = copy.deepcopy(self.sources)
        sources['options']['use_slides_as_source'] = False
        with self.assertRaises(SourceRefError) as caught:
            resolve_ref(self.root, sources, self.item('slide_occurrence', 'first'))
        self.assertEqual(caught.exception.code, 'SOURCE_MISSING')


if __name__ == '__main__':
    unittest.main()
