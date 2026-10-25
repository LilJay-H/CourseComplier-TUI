"""The internal TUI consumes only safe metadata and a checked standard package."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from handouter import external_collector as bridge


@unittest.skipIf(os.name == "nt", "POSIX executable fixture; native Windows containment has its own tests")
class ExternalCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.configuration = self.root / "collector.json"
        self.source = self.root / "browser.zip"
        self.source.write_bytes(b"synthetic private input, host does not parse")
        self.plugin = self.root / "synthetic collector"
        self.plugin.write_text(f'''#!{sys.executable}
import hashlib,json,pathlib,sys,zipfile
r=json.loads(sys.stdin.buffer.readline())
m=r['method']; p=r['params']
if m=='identity':
 print(json.dumps({{'type':'identity','tool_id':'org.example.fixture','protocol_version':1,'tool_version':'1.0','capabilities':['inspect','convert']}}))
elif m=='inspect':
 print(json.dumps({{'type':'inspection','tool_id':'org.example.fixture','protocol_version':1,'tool_version':'1.0','course_title':'Synthetic course','lecture_date':None,'media_hosts':['media.example.test'],'source_sha256':hashlib.sha256(pathlib.Path(p['source_path']).read_bytes()).hexdigest()}}))
elif m=='convert':
 with zipfile.ZipFile(p['output_path'],'x') as a:
  a.writestr('handouter-input.json',json.dumps({{'schema_version':1,'lecture':{{'course_title':'Synthetic course','lecture_date':None}},'transcript':{{'path':'transcript.txt'}}}}))
  a.writestr('transcript.txt','Synthetic lesson')
 print(json.dumps({{'type':'result','output_path':p['output_path']}}))
''')
        self.plugin.chmod(0o700)
        self.digest = hashlib.sha256(self.plugin.read_bytes()).hexdigest()
        self.path_patch = patch.object(bridge, "configuration_path", return_value=self.configuration)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def configure(self):
        return bridge.configure(str(self.plugin), self.digest, "org.example.fixture")

    def test_registration_and_inspection_are_explicit_and_source_bound(self):
        with self.assertRaisesRegex(ValueError, "NOT_CONFIGURED"):
            bridge.inspect_browser_export(self.source)
        self.configure()
        details = bridge.inspect_browser_export(self.source)
        self.assertEqual(details["course_title"], "Synthetic course")
        self.assertEqual(details["media_hosts"], ["media.example.test"])
        self.assertEqual(details["source_sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertFalse((self.root / "standard.zip").exists())

    def test_only_exact_confirmation_can_convert_and_complete_inspect_is_required(self):
        self.configure()
        output = self.root / "standard.zip"
        with self.assertRaisesRegex(ValueError, "HOST_CONFIRMATION"):
            bridge.convert_browser_export(self.source, output, allowed_media_hosts=["wrong.example.test"])
        self.assertFalse(output.exists())
        value = bridge.convert_browser_export(self.source, output, allowed_media_hosts=["media.example.test"])
        self.assertTrue(value.public()["ready"])
        self.assertEqual(value.source_kind, "asset_zip")
        self.assertEqual(self.source.read_bytes(), b"synthetic private input, host does not parse")

    def test_changed_tool_is_rejected_before_execution(self):
        self.configure()
        self.plugin.write_text("changed")
        with self.assertRaisesRegex(ValueError, "HASH_MISMATCH"):
            bridge.inspect_browser_export(self.source)

    def test_conversion_keeps_the_same_tool_binding_through_preflight(self):
        self.configure()
        config = bridge.load_configuration()
        with patch.object(bridge, "load_configuration", side_effect=[config, AssertionError("tool configuration reloaded")]) as load:
            result = bridge.convert_browser_export(self.source, self.root / "standard.zip", allowed_media_hosts=["media.example.test"])
        self.assertEqual(result.source_kind, "asset_zip")
        self.assertEqual(load.call_count, 1)

    def test_changed_confirmed_tool_binding_does_not_invoke_replacement(self):
        self.configure()
        first = bridge.load_configuration()
        replacement = {**first, "tool_id": "org.example.replacement"}
        with patch.object(bridge, "load_configuration", return_value=replacement), \
                patch.object(bridge, "_invoke", side_effect=AssertionError("replacement ran")):
            with self.assertRaisesRegex(ValueError, "COLLECTOR_CHANGED"):
                bridge.convert_browser_export(self.source, self.root / "standard.zip", allowed_media_hosts=["media.example.test"], expected_collector_binding=bridge._binding(first))

    def test_display_credentials_are_rejected_without_returning_them(self):
        self.configure()
        frame = {"type": "inspection", "tool_id": "org.example.fixture", "protocol_version": 1,
                 "course_title": "auth_key=SYNTHETIC_SECRET", "lecture_date": None,
                 "media_hosts": ["media.example.test"], "source_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest()}
        with patch.object(bridge, "_identity"), patch.object(bridge, "_invoke", return_value=frame):
            with self.assertRaisesRegex(ValueError, "COLLECTOR_PROTOCOL") as raised:
                bridge.inspect_browser_export(self.source)
        self.assertNotIn("SYNTHETIC_SECRET", str(raised.exception))

    def test_timeout_cleans_the_child_and_its_descendant(self):
        pid_path = self.root / "descendant.pid"
        self.plugin.write_text(f'''#!{sys.executable}
import pathlib,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
pathlib.Path({str(pid_path)!r}).write_text(str(child.pid))
time.sleep(30)
''')
        self.plugin.chmod(0o700)
        config = {"executable": str(self.plugin), "sha256": hashlib.sha256(self.plugin.read_bytes()).hexdigest(),
                  "tool_id": "org.example.fixture", "exporter_script": None}
        with self.assertRaisesRegex(ValueError, "COLLECTOR_TIMEOUT"):
            bridge._invoke(config, "inspect", {}, timeout=1)
        self.assertTrue(pid_path.exists())
        pid = int(pid_path.read_text())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
            if state.returncode != 0 or state.stdout.strip().startswith("Z"):
                break
            time.sleep(0.05)
        else:
            self.fail("collector descendant survived cancellation")

    def test_darwin_permission_denial_needs_no_live_group_members(self):
        def probe(output="", code=0):
            return subprocess.CompletedProcess([], code, stdout=output, stderr="")
        with patch.object(bridge.subprocess, "run", return_value=probe("222 Z\n222 Z+\n999 S\n")):
            self.assertTrue(bridge._group_has_no_live_members(222))
        with patch.object(bridge.subprocess, "run", return_value=probe("999 S\n")):
            self.assertTrue(bridge._group_has_no_live_members(222))
        with patch.object(bridge.subprocess, "run", return_value=probe("222 S\n")):
            self.assertFalse(bridge._group_has_no_live_members(222))
        with patch.object(bridge.subprocess, "run", return_value=probe(code=1)):
            self.assertFalse(bridge._group_has_no_live_members(222))
        with patch.object(bridge.subprocess, "run", return_value=probe("invalid row fields\n")):
            self.assertFalse(bridge._group_has_no_live_members(222))
        with patch.object(bridge.subprocess, "run", side_effect=subprocess.TimeoutExpired("ps", 2)):
            self.assertFalse(bridge._group_has_no_live_members(222))

    def test_checked_standard_package_uses_streaming_budget_and_preserves_source_fingerprint(self):
        from handouter.service import build_from_asset_zip
        from handouter.tasks import plan_task
        self.configure()
        skill = self.root / "SKILL.md"
        skill.write_text("# Synthetic test skill\nUse only supplied evidence.\n")
        with patch("handouter.importers.MAX_ZIP_BYTES", 1), \
                patch("handouter.service.import_local_asset_zip", side_effect=AssertionError("legacy bounded importer used")), \
                patch.object(zipfile.ZipFile, "read", side_effect=AssertionError("whole ZIP member read")):
            result = build_from_asset_zip(self.root / "workspaces", asset_zip=self.source,
                lecture_id="synthetic-course", course_title="Synthetic course", skill_path=skill,
                use_slides_as_source=False, allowed_media_hosts=["media.example.test"])
        workspace = result.workspace
        fingerprint = hashlib.sha256(self.source.read_bytes()).hexdigest()
        manifest = json.loads((workspace / "manifest.json").read_text())
        self.assertEqual(manifest["input_fingerprint"], fingerprint)
        self.assertNotEqual(plan_task(workspace, input_fingerprint=fingerprint).action, "refresh_required")

    def test_second_cli_run_reuses_the_existing_checked_workspace(self):
        from handouter import cli
        from handouter.service import build_from_asset_zip
        with zipfile.ZipFile(self.source, "w") as archive:
            archive.writestr("opaque.dat", "synthetic browser data")
        self.configure()
        skill = self.root / "SKILL.md"
        skill.write_text("# Synthetic test skill\nUse only supplied evidence.\n")
        args = ["run", "--asset", str(self.source), "--allowed-media-host", "media.example.test",
                "--output-dir", str(self.root / "out"), "--lecture-id", "repeat-course",
                "--course-title", "Synthetic course", "--agent", "manual", "--ignore-slides",
                "--skill-path", str(skill), "--legacy-layout"]
        settings = {"input_dir":str(self.root),"output_dir":str(self.root / "out"),
                    "course_output_dirs":{},"course_aliases":{},"course_folder_modes":{}}
        with patch("handouter.settings.load_directory_settings", return_value=settings), \
                patch("handouter.cli.build_from_asset_zip", wraps=build_from_asset_zip) as build, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(args), 0)
            self.assertEqual(cli.main(args), 0)
        self.assertEqual(build.call_count, 1)

    def test_child_error_never_returns_its_message(self):
        self.plugin.write_text(f'#!{sys.executable}\nimport json\nprint(json.dumps({{"type":"error","code":"ERR","message":"auth_key=SYNTHETIC_SECRET"}}))\n')
        self.plugin.chmod(0o700)
        config = {"executable": str(self.plugin), "sha256": hashlib.sha256(self.plugin.read_bytes()).hexdigest(),
                  "tool_id": "org.example.fixture", "exporter_script": None}
        with self.assertRaisesRegex(ValueError, "COLLECTOR_FAILED") as raised:
            bridge._invoke(config, "inspect", {"source_path": str(self.source)})
        self.assertNotIn("SYNTHETIC_SECRET", str(raised.exception))

    def test_oversized_frame_is_rejected(self):
        self.plugin.write_text(f'#!{sys.executable}\nprint("x"*20000)\n')
        self.plugin.chmod(0o700)
        config = {"executable": str(self.plugin), "sha256": hashlib.sha256(self.plugin.read_bytes()).hexdigest(),
                  "tool_id": "org.example.fixture", "exporter_script": None}
        with self.assertRaisesRegex(ValueError, "COLLECTOR_PROTOCOL"):
            bridge._invoke(config, "inspect", {})
