"""Submitted notes are explicit evidence snapshots, never overwrite targets."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from handouter import cli
from handouter.agents.manual import create_manual_bundle
from handouter.agents.web import create_web_handoff
from handouter.revision import read_revision_note, note_text_preserved
from handouter.service import prepare_lecture, refresh_handoff
from handouter.tui import _task_cli_args
from handouter.validation import validate_note_output


class NoteRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.note = self.root / "已有深度讲义.md"
        self.note.write_text("# 深度讲义\n\n结论有适用条件。\n\n## 第二章\n\n最后一段。", encoding="utf-8")
        self.transcript = self.root / "original.txt"
        self.transcript.write_text("原课堂里还有一个容易遗漏的例外。", encoding="utf-8")

    def workspace(self):
        return prepare_lecture(self.root / "output", lecture_id="existing", course_title="Test",
                               transcript=self.transcript, use_slides_as_source=False).workspace

    def test_existing_workspace_reuses_evidence_and_embeds_explicit_snapshot_in_all_adapters(self):
        root = self.workspace()
        before = (root / "transcript/transcript.txt").read_bytes()
        (root / "notes/deep-001.md").write_text("unselected old note")
        result = refresh_handoff(root, modes=["summary"], source_note=self.note)
        sources = json.loads((root / "handoff/sources.json").read_text())
        self.assertEqual(sources["revision"]["text"], self.note.read_text())
        self.assertEqual(sources["revision"]["basis"], "note_with_original_materials")
        prompt = (root / "handoff/PROMPT.md").read_text()
        self.assertIn("用户提交的已有讲义", prompt)
        self.assertEqual((root / "transcript/transcript.txt").read_bytes(), before)
        self.assertTrue(result.archived_prompt.exists())
        web = create_web_handoff(root).materials.read_text()
        self.assertIn("最后一段", web)
        self.assertNotIn("unselected old note", web)
        with zipfile.ZipFile(create_manual_bundle(root).bundle) as archive:
            self.assertEqual(json.loads(archive.read("handoff/sources.json"))["revision"], sources["revision"])
        self.note.write_text("changed externally")
        refresh_handoff(root, modes=["full"])
        self.assertEqual(json.loads((root / "handoff/sources.json").read_text())["revision"], sources["revision"])
        refresh_handoff(root, clear_source_note=True)
        self.assertIsNone(json.loads((root / "handoff/sources.json").read_text())["revision"])

    def test_note_only_cli_never_downloads_or_transcribes_and_marks_evidence_limit(self):
        before = self.note.read_bytes()
        with patch("handouter.service.extract_audio") as download, patch("handouter.service.transcribe_sensevoice") as asr:
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                code = cli.main(["revise", str(self.note), "--lecture-id", "derived", "--output-dir", str(self.root / "out"),
                                 "--modes", "summary", "--agent", "web"])
            self.assertEqual(code, 0)
            download.assert_not_called()
            asr.assert_not_called()
        payload = json.loads(stream.getvalue())
        root = Path(payload["workspace"])
        self.assertEqual(json.loads((root / "manifest.json").read_text())["source_type"], "provided_note")
        self.assertIn("不是 ASR 或课堂原文", Path(payload["agent_interaction"]["upload_files"][0]).read_text())
        self.assertEqual(self.note.read_bytes(), before)
        with self.assertRaises(ValueError):
            refresh_handoff(root, modes=["verbatim"])
        with self.assertRaises(ValueError):
            refresh_handoff(root, clear_source_note=True)

    def test_illustration_requires_real_slides_and_restores_existing_handoff_on_failure(self):
        root = self.workspace()
        prompt = (root / "handoff/PROMPT.md").read_bytes()
        with self.assertRaisesRegex(ValueError, "PPT"):
            refresh_handoff(root, source_note=self.note, revision_action="illustrate", modes=["deep"])
        self.assertEqual((root / "handoff/PROMPT.md").read_bytes(), prompt)

    def test_illustration_keeps_text_scope_with_real_images(self):
        from PIL import Image
        slides = self.root / "slides"
        slides.mkdir()
        Image.new("RGB", (80, 60), "blue").save(slides / "one.png")
        root = prepare_lecture(self.root / "out", lecture_id="illustrate", course_title="Test",
                               transcript=self.transcript, slides_dir=slides).workspace
        result = refresh_handoff(root, source_note=self.note, revision_action="illustrate", modes=["deep"])
        self.assertTrue(result.handoff.embed_slides)
        self.assertIn("不要顺便摘要、扩写或重写正文", (root / "handoff/PROMPT.md").read_text())
        self.assertTrue(create_web_handoff(root).slides.is_file())
        output = root / result.handoff.output_path
        output.write_text("# Rewritten\n\nDifferent content.")
        self.assertTrue(any("原讲义正文" in error for error in validate_note_output(root).errors))
        output.write_text(self.note.read_text())
        self.assertFalse(any("原讲义正文" in error for error in validate_note_output(root).errors))
        with self.assertRaisesRegex(ValueError, "一个"):
            refresh_handoff(root, modes=["deep", "summary"])

    def test_additive_body_guard_accepts_insertions_and_rejects_rewrite_or_reordering(self):
        original = "# Title\n\nFirst paragraph.\n\nSecond paragraph."
        self.assertTrue(note_text_preserved(original, "# Title\n\nFirst paragraph.\n\n![PPT](slide.png)\n\nSecond paragraph."))
        self.assertFalse(note_text_preserved(original, "# Title\n\nSecond paragraph.\n\nFirst paragraph."))
        self.assertFalse(note_text_preserved(original, "<!--" + original + "-->\n# Rewritten"))

    def test_note_only_uses_one_snapshot_even_when_original_changes_after_read(self):
        revision = read_revision_note(self.note, note_only=True)
        def read_then_change(*args, **kwargs):
            self.note.write_text("Changed after snapshot")
            return revision
        with patch("handouter.cli.read_revision_note", side_effect=read_then_change), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["revise", str(self.note), "--lecture-id", "snapshot", "--output-dir", str(self.root / "out"), "--agent", "none"])
        self.assertEqual(code, 0)
        from handouter.organization import find_workspace
        workspace = find_workspace(self.root / 'out', 'snapshot')
        self.assertEqual((workspace / 'transcript/transcript.txt').read_text(), revision['text'])

    def test_secret_and_unsupported_note_are_rejected(self):
        self.note.write_text("auth_key=synthetic-test-secret")
        with self.assertRaisesRegex(ValueError, "凭证"):
            read_revision_note(self.note)
        pdf = self.root / "note.pdf"
        pdf.write_bytes(b"not a text note")
        with self.assertRaises(ValueError):
            read_revision_note(pdf)

    def test_tui_routes_submitted_note_without_asset_or_asr(self):
        cfg = {"source_note": str(self.note), "output_dir": str(self.root / "out"),
               "lecture": "new", "title": "Test", "notes_summary": True,
               "agent": "web", "use_slides": False}
        args, action = _task_cli_args(cfg, prompt_only=False)
        self.assertEqual(args[0], "revise")
        self.assertNotIn("run", args)
        root = self.workspace()
        cfg.update(output_dir=str(root.parent), lecture=root.name)
        args, action = _task_cli_args(cfg, prompt_only=False)
        self.assertEqual(action, "refresh")
        self.assertEqual(args[0], "prompt")
        self.assertIn("--source-note", args)


if __name__ == "__main__":
    unittest.main()
