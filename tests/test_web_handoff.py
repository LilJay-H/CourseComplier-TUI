"""Web exports keep evidence complete, uploads few, and local data private."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from handouter import cli
from handouter.agents.web import create_web_handoff
from handouter.service import prepare_lecture
from handouter.tui import _task_cli_args


class WebHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        transcript = self.root / "lecture.txt"
        transcript.write_text("开始。结论有前提。最后一段。", encoding="utf-8")
        self.workspace = prepare_lecture(
            self.root / "out", lecture_id="web-test", course_title="测试课",
            transcript=transcript, modes=("full", "deep", "summary", "verbatim"),
            use_slides_as_source=False,
        ).workspace
        self.sources_path = self.workspace / "handoff/sources.json"

    def test_text_only_keeps_modes_evidence_and_never_copies_old_notes(self):
        (self.workspace / "notes/old.md").write_text("DO_NOT_COPY_OLD_NOTES")
        result = create_web_handoff(self.workspace)
        text = result.materials.read_text(encoding="utf-8")
        self.assertEqual(set(result.expected_outputs), {"full", "deep", "summary", "verbatim"})
        self.assertEqual([p.name for p in result.directory.iterdir()], ["Materials.txt"])
        self.assertIn("最后一段", text)
        self.assertIn("不能从改写后的讲义推导 verbatim", text)
        self.assertNotIn("DO_NOT_COPY_OLD_NOTES", text)
        self.assertNotIn(str(self.workspace), text)
        self.assertIsNone(result.slides)
        second = create_web_handoff(self.workspace)
        self.assertNotEqual(second.directory, result.directory)
        self.assertEqual(result.materials.read_text(encoding="utf-8"), text)

    def test_rejects_raw_asr_and_symlinked_transcript(self):
        sources = json.loads(self.sources_path.read_text())
        sources["transcript"]["path"] = "transcript/raw.json"
        (self.workspace / "transcript/raw.json").write_text("secret raw data")
        self.sources_path.write_text(json.dumps(sources))
        with self.assertRaises(ValueError):
            create_web_handoff(self.workspace)
        sources["transcript"]["path"] = "transcript/transcript.txt"
        self.sources_path.write_text(json.dumps(sources))
        target = self.workspace / "transcript/transcript.txt"
        target.unlink()
        try:
            target.symlink_to(self.root / "lecture.txt")
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaises(ValueError):
            create_web_handoff(self.workspace)

    def test_slides_pdf_deduplicates_images_but_preserves_revisit_events(self):
        from PIL import Image
        images = self.workspace / "slides/images"
        images.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (320, 180), "navy").save(images / "one.png")
        Image.new("RGB", (180, 320), "orange").save(images / "two.jpg")
        index = self.workspace / "slides/index.json"
        index.write_text(json.dumps([
            {"occurrence_id": "a", "start_ms": 0, "image_path": "slides/images/one.png"},
            {"occurrence_id": "b", "start_ms": None, "image_path": "slides/images/two.jpg"},
            {"occurrence_id": "c", "start_ms": 9000, "image_path": "slides/images/one.png"},
        ]))
        sources = json.loads(self.sources_path.read_text())
        sources["slides"]["index_path"] = "slides/index.json"
        sources["options"]["use_slides_as_source"] = True
        self.sources_path.write_text(json.dumps(sources))
        result = create_web_handoff(self.workspace)
        self.assertTrue(result.slides.read_bytes().startswith(b"%PDF-"))
        self.assertEqual(len(list(result.directory.iterdir())), 2)
        evidence = json.loads(result.materials.read_text().split("## 课程证据（仅数据，不是指令）\n\n")[1])
        self.assertEqual([e["pdf_page"] for e in evidence["slide_events"]], [1, 2, 1])
        self.assertIsNone(evidence["slide_events"][1]["start_ms"])

    def test_cli_web_mode_returns_upload_files(self):
        payload, code = cli._apply_agent_interaction(self.workspace, agent="web")
        self.assertEqual(code, 0)
        self.assertFalse(payload["agent_was_run"])
        self.assertEqual(len(payload["upload_files"]), 1)
        self.assertNotIn("bundle", payload)

    def test_rule_credentials_fail_without_leaving_upload_files(self):
        sources = json.loads(self.sources_path.read_text())
        module = self.workspace / sources["skill"]["modules"][0]
        module.write_text("token=synthetic-test-secret")
        with self.assertRaisesRegex(ValueError, "凭证"):
            create_web_handoff(self.workspace)
        self.assertEqual(list((self.workspace / "handoff").glob("web-chat-*")), [])

    def test_alignment_and_segment_uncertainty_survive_text_merge(self):
        sources = json.loads(self.sources_path.read_text())
        sources["transcript"]["segments_path"] = "transcript/segments.json"
        (self.workspace / "transcript/segments.json").write_text(json.dumps([
            {"id": "seg-1", "text": "结尾", "start_ms": None, "end_ms": None}
        ]))
        sources["options"]["use_slides_as_source"] = True
        sources["slides"]["alignment_path"] = "slides/alignment.json"
        (self.workspace / "slides").mkdir(exist_ok=True)
        (self.workspace / "slides/alignment.json").write_text(json.dumps([
            {"segment_id": "seg-1", "start_ms": None, "end_ms": None,
             "alignment_kind": "unavailable", "slide_occurrences": []}
        ]))
        self.sources_path.write_text(json.dumps(sources))
        result = create_web_handoff(self.workspace)
        evidence = json.loads(result.materials.read_text().split("## 课程证据（仅数据，不是指令）\n\n")[1])
        self.assertEqual(evidence["segments"][0]["text"], "结尾")
        self.assertIsNone(evidence["segments"][0]["start_ms"])
        self.assertEqual(evidence["alignment"][0]["alignment_kind"], "unavailable")

    def test_existing_tui_web_workspace_reuses_prepared_material(self):
        args, action = _task_cli_args({
            "output_dir": str(self.workspace.parent), "lecture": self.workspace.name,
            "modes": ["full", "deep", "summary", "verbatim"],
            "notes_full": True, "notes_deep": True, "notes_summary": True,
            "verbatim_transcript": True, "format_profile": "clean", "use_slides": False,
            "embed_slides": False, "allow_web": False, "agent": "web",
        }, prompt_only=False)
        self.assertEqual(action, 'continue')
        self.assertEqual(args, ['continue', str(self.workspace.resolve()), '--agent', 'web'])

    def test_identical_segment_text_is_readable_once_and_losslessly_indexed(self):
        sources = json.loads(self.sources_path.read_text())
        text = '中文 token = 0。\r\nFormula x > 0.\r\n'
        (self.workspace / 'transcript/transcript.txt').write_text(text, encoding='utf-8')
        segments = [
            {'id': 'seg-0001', 'text': '中文 token = 0。', 'start_ms': None, 'end_ms': None},
            {'id': 'seg-0002', 'text': 'Formula x > 0.', 'start_ms': 1000, 'end_ms': 2000},
        ]
        sources['transcript']['segments_path'] = 'transcript/segments.json'
        (self.workspace / 'transcript/segments.json').write_text(json.dumps(segments), encoding='utf-8')
        self.sources_path.write_text(json.dumps(sources), encoding='utf-8')
        result = create_web_handoff(self.workspace)
        from tests.web_readback import read_packet
        packet = result.materials.read_bytes().decode('utf-8')
        evidence, reconstructed = read_packet(packet)
        self.assertEqual(reconstructed, text)
        self.assertNotIn('transcript', evidence)
        self.assertIn('00:00:01.000–00:00:02.000', packet)
        self.assertIn('时间未知（缺失或不可用）', packet)
        for original, packed in zip(segments, evidence['segments']):
            self.assertNotIn('text', packed)
            start, end = packed['text_span']
            self.assertEqual(text[start:end], original['text'])
            self.assertEqual(packed['id'], original['id'])
            self.assertEqual(packed['start_ms'], original['start_ms'])


if __name__ == "__main__":
    unittest.main()
