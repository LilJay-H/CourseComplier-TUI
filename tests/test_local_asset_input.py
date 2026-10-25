"""Site-neutral local input contract; all assets are synthetic and offline."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from handouter import cli, tui
from handouter.importers import import_local_asset_zip, inspect_local_asset_zip
from handouter.product import inspect_input_source
from handouter.service import BuildResult, build_from_local_asset_zip


class LocalAssetInputTests(unittest.TestCase):
    def test_real_cli_local_zip_forwards_course_directory_preferences(self):
        import io
        from contextlib import redirect_stdout
        archive = self.package({'schema_version': 1, 'transcript': {'path': 'text.txt'},
                                'lecture': {'id': 'local-cli', 'course_title': 'Course',
                                            'lecture_date': '2026-01-02'}}, {'text.txt': 'Synthetic course.'})
        output = self.root / 'Alias'
        with patch.object(cli, '_course_output_context', return_value=('Course', str(output), ('Alias',), 'auto')), \
             redirect_stdout(io.StringIO()):
            code = cli.main(['run', '--asset', str(archive), '--output-dir', str(output), '--agent', 'none'])
        self.assertEqual(code, 0)
        self.assertEqual(len(list((output / '01-02').rglob('handoff/sources.json'))), 1)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def package(self, manifest, files=None, name="local.zip"):
        path = self.root / name
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("handouter-input.json", json.dumps(manifest, ensure_ascii=False))
            for relative, content in (files or {}).items():
                archive.writestr(relative, content)
        return path

    def transcript_manifest(self):
        return {
            "schema_version": 1,
            "lecture": {"id": "portable-lecture", "course_title": "Portable Course",
                        "lecture_date": "2026-09-19"},
            "audio": {"path": "audio/lecture.m4a"},
            "transcript": {"path": "transcript/transcript.txt", "origin": "provided_transcript",
                           "segments_path": "transcript/segments.json"},
            "slides": {"images_dir": "slides/images", "metadata_path": "slides/slides_meta.json"},
        }

    def test_transcript_package_prepares_workspace_without_asr(self):
        manifest = self.transcript_manifest()
        files = {
            "transcript/transcript.txt": "用户提供的转写。",
            "audio/lecture.m4a": b"synthetic evidence audio",
            "transcript/segments.json": json.dumps([{"id": "seg-0001", "start_ms": 0,
                "end_ms": 1000, "text": "用户提供的转写。", "timestamp_kind": "provided", "speaker": None}]),
            "slides/images/slide_001.png": b"\x89PNG\r\n\x1a\nsynthetic",
            "slides/slides_meta.json": json.dumps([{"filename": "slide_001.png", "start_ms": 0}]),
        }
        archive = self.package(manifest, files)
        with patch("handouter.service.build_from_audio") as audio, patch("handouter.service.build_from_media") as media:
            result = build_from_local_asset_zip(self.root / "out", lecture_id="portable-lecture",
                course_title="Portable Course", asset_zip=archive, use_slides_as_source=True)
        audio.assert_not_called()
        media.assert_not_called()
        workspace_manifest = json.loads((result.workspace / "manifest.json").read_text())
        self.assertEqual(workspace_manifest["source_type"], "local_asset_package")
        self.assertEqual(workspace_manifest["transcript"]["origin"], "provided_transcript")
        self.assertEqual((result.workspace / "transcript/transcript.txt").read_text(), "用户提供的转写。")
        self.assertEqual((result.workspace / "audio/source.m4a").read_bytes(), b"synthetic evidence audio")
        self.assertTrue((result.workspace / "slides/images/slide_001.png").is_file())
        from handouter.agents.manual import selected_files
        self.assertNotIn("audio/source.m4a", selected_files(result.workspace,
            json.loads((result.workspace / "handoff/sources.json").read_text())))

    def test_audio_only_package_dispatches_to_existing_audio_pipeline(self):
        manifest = {"schema_version": 1, "audio": {"path": "audio/lesson.m4a"}}
        archive = self.package(manifest, {"audio/lesson.m4a": b"synthetic audio"})
        sentinel = object()
        captured = {}
        def consume(*_args, **kwargs):
            captured["audio"] = Path(kwargs["audio"]).read_bytes()
            return sentinel
        with patch("handouter.service.build_from_audio", side_effect=consume) as build:
            result = build_from_local_asset_zip(self.root / "out", lecture_id="audio-lecture",
                course_title="Audio", asset_zip=archive)
        self.assertIs(result, sentinel)
        self.assertEqual(captured["audio"], b"synthetic audio")
        self.assertEqual(build.call_args.kwargs["source_type"], "local_asset_package")

    def test_manifest_identity_and_tui_direct_audio_mapping(self):
        archive = self.package({"schema_version": 1, "lecture": {"course_title": "Course"},
            "transcript": {"path": "text.txt"}}, {"text.txt": "text"})
        with patch("handouter.external_collector.inspect_browser_export", side_effect=AssertionError("standard package requires no plugin")):
            identity = inspect_input_source(archive)
        self.assertEqual(identity.source_kind, "local_asset_package")
        self.assertEqual(identity.course_title, "Course")
        audio = self.root / "lesson.mp3"
        audio.write_bytes(b"synthetic")
        audio_identity = inspect_input_source(audio)
        args = tui.build_cli_args({"kind": "product", "asset": str(audio),
            "source_kind": audio_identity.source_kind, "output_dir": str(self.root / "out"),
            "lecture": audio_identity.lecture_id, "title": "Lesson", "notes_deep": True,
            "use_slides": False, "agent": "none"})
        self.assertIn("--audio", args)
        self.assertNotIn("--asset", args)

    def test_nested_manifest_name_does_not_impersonate_root_local_package(self):
        archive = self.root / "nested.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("nested/handouter-input.json", json.dumps({"schema_version": 1}))
            output.writestr("course_info.json", json.dumps({"courseName": "Zhiyun compatible"}))
            output.writestr("private/source.json", json.dumps({"pageUrl": "https://example.invalid/?course_id=1&sub_id=2"}))
        with patch("handouter.external_collector.inspect_browser_export", return_value={
                "course_title": "External compatible", "lecture_date": None, "source_sha256": "a" * 64}):
            identity = inspect_input_source(archive)
        self.assertEqual(identity.source_kind, "zhiyun_asset_zip")

    def test_cli_run_accepts_direct_transcript_without_asr(self):
        transcript = self.root / "lecture.txt"
        transcript.write_text("direct transcript")
        parser = cli._build_parser()
        args = parser.parse_args(["run", "--transcript", str(transcript), "--output-dir",
            str(self.root / "out"),
            "--course-title", "Direct", "--lecture-id", "direct-1"])
        with patch("handouter.cli.prepare_lecture") as prepare, \
             patch("handouter.cli._require_delivery"), \
             patch("handouter.cli._apply_agent_interaction", return_value=({"agent_was_run": False}, 0)), \
             patch("handouter.cli._prepare_payload", return_value={}), \
             patch("handouter.organization.find_workspace", return_value=None):
            prepare.return_value = type("Prepared", (), {"workspace": self.root / "workspace"})()
            code = cli._run_product(args)
        self.assertEqual(code, 0)
        self.assertEqual(prepare.call_args.kwargs["transcript_origin"], "provided_transcript")

    def test_cli_run_accepts_direct_audio_and_uses_existing_pipeline(self):
        audio = self.root / "lecture.mp3"
        audio.write_bytes(b"direct audio")
        args = cli._build_parser().parse_args(["run", "--audio", str(audio), "--output-dir",
            str(self.root / "out"), "--course-title", "Direct", "--lecture-id", "direct-audio"])
        prepared = type("Prepared", (), {"workspace": self.root / "workspace"})()
        result = BuildResult(prepared, None, None)
        with patch("handouter.cli.build_from_audio", return_value=result) as build, \
             patch("handouter.cli._require_delivery"), \
             patch("handouter.cli._apply_agent_interaction", return_value=({"agent_was_run": False}, 0)), \
             patch("handouter.cli._build_payload", return_value={}), \
             patch("handouter.organization.find_workspace", return_value=None):
            code = cli._run_product(args)
        self.assertEqual(code, 0)
        self.assertEqual(Path(build.call_args.kwargs["audio"]), audio.resolve())
        self.assertIsNotNone(build.call_args.kwargs["input_fingerprint"])

    def test_invalid_or_unsafe_packages_are_rejected(self):
        invalid = [
            {"schema_version": 1},
            {"schema_version": 1, "transcript": {"path": "../outside.txt"}},
            {"schema_version": 1, "audio": {"path": "audio.exe"}},
            {"schema_version": 1, "media": {"path": "video.mp4"}, "transcript": {"path": "text.txt"}},
            {"schema_version": 1, "transcript": {"path": "same.txt", "segments_path": "same.txt"}},
        ]
        for index, manifest in enumerate(invalid):
            with self.subTest(index=index):
                archive = self.package(manifest, name=f"bad-{index}.zip")
                with self.assertRaises(ValueError):
                    inspect_local_asset_zip(archive)
        missing = self.package({"schema_version": 1, "transcript": {"path": "missing.txt"}}, name="missing.zip")
        with self.assertRaises(ValueError):
            import_local_asset_zip(missing, self.root / "missing-out")
        link = self.root / "linked.zip"
        try:
            link.symlink_to(missing)
        except OSError:
            pass
        else:
            with self.assertRaises(FileNotFoundError):
                inspect_input_source(link)


if __name__ == "__main__":
    unittest.main()
