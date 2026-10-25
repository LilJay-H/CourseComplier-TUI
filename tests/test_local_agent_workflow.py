"""Local CLI-agent workflow module integration tests."""

from __future__ import annotations

import json
import base64
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from handouter.agents.base import AgentAvailability
from handouter.agents.runner import run_cli_agent
from handouter.service import prepare_lecture, refresh_handoff
from handouter.skill_plan import plan_modules
from handouter.tasks import plan_task


MODULE = "handoff/skill/references/execution/local-agent.md"


class LocalAgentWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.transcript = self.root / "lecture.txt"
        self.transcript.write_text("本地流程测试课程内容。", encoding="utf-8")
        self.skill = ROOT / ".agents/skills/zhiyun-lecture-notes/SKILL.md"

    def prepare(self, name="local-agent", **kwargs):
        return prepare_lecture(
            self.root / name,
            lecture_id=name,
            course_title="本地 Agent 测试",
            transcript=self.transcript,
            mode=kwargs.pop("mode", "deep"),
            use_slides_as_source=False,
            embed_slides=False,
            skill_path=kwargs.pop("skill_path", self.skill),
            **kwargs,
        )

    def sources(self, workspace):
        return json.loads((workspace / "handoff/sources.json").read_text(encoding="utf-8"))

    def test_builtin_handoff_materializes_and_prompts_for_local_agent_module(self):
        result = self.prepare()
        sources = self.sources(result.workspace)
        self.assertIn(MODULE, sources["skill"]["modules"])
        from handouter.local_agent_workflow import INSTRUCTIONS

        self.assertEqual((result.workspace / MODULE).read_bytes(), INSTRUCTIONS.encode("utf-8"))
        prompt = (result.workspace / "handoff/PROMPT.md").read_text(encoding="utf-8")
        self.assertIn(MODULE, prompt)

    def test_base_authoring_v2_injects_module_but_shared_plan_does_not(self):
        result = self.prepare("authoring-v2", authoring_version=2)
        sources = self.sources(result.workspace)
        self.assertIn(MODULE, sources["skill"]["modules"])
        self.assertIn("handoff/skill/references/execution/authoring-v2.md", sources["skill"]["modules"])

        shared = plan_modules(
            self.skill, modes=("deep",), format_profile="clean", use_slides_as_source=False,
            embed_slides=False,
        )
        self.assertNotIn("references/execution/local-agent.md", shared)

    def test_refresh_adds_module_and_archives_old_snapshot(self):
        result = self.prepare("refresh")
        old_module = (result.workspace / MODULE).read_bytes()

        refreshed = refresh_handoff(result.workspace)
        self.assertTrue(refreshed.archived_prompt.is_file())
        current = self.sources(result.workspace)
        self.assertIn(MODULE, current["skill"]["modules"])
        archived_module = result.workspace / "handoff/history/task-001-skill/references/execution/local-agent.md"
        self.assertEqual(archived_module.read_bytes(), old_module)
        self.assertTrue((result.workspace / MODULE).is_file())

    def test_module_content_is_bound_into_authoring_task_hash(self):
        result = self.prepare("authoring-hash")
        sources = self.sources(result.workspace)
        policy = sources["authoring"]
        import hashlib

        rule_hashes = {
            path: hashlib.sha256((result.workspace / path).read_bytes()).hexdigest()
            for path in [sources["skill"]["entry"], *sources["skill"]["modules"]]
        }
        self.assertEqual(rule_hashes[MODULE], hashlib.sha256((result.workspace / MODULE).read_bytes()).hexdigest())
        from handouter.authoring import make_contract

        unbound = dict(sources)
        unbound.pop("authoring")
        manifest = json.loads((result.workspace / "manifest.json").read_text(encoding="utf-8"))
        expected = make_contract(
            unbound,
            [*manifest.get("files", []), {"rule_hashes": rule_hashes}],
            version=policy["schema_version"],
            root=result.workspace,
        )
        self.assertEqual(policy["task_key"], expected["task_key"])

    def test_plan_task_warns_legacy_module_can_be_refreshed_without_mutating_snapshot(self):
        result = self.prepare("legacy")
        sources_path = result.workspace / "handoff/sources.json"
        sources = self.sources(result.workspace)
        sources["skill"]["modules"] = [m for m in sources["skill"]["modules"] if m != MODULE]
        sources_path.write_text(json.dumps(sources, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        prompt_path = result.workspace / "handoff/PROMPT.md"
        prompt_path.write_text(prompt_path.read_text(encoding="utf-8").replace(f"- `{MODULE}`\n", ""), encoding="utf-8")
        (result.workspace / MODULE).unlink()
        before = (prompt_path.read_bytes(), sources_path.read_bytes())

        plan = plan_task(result.workspace)

        self.assertTrue(any("刷新" in warning for warning in plan.warnings))
        self.assertEqual((prompt_path.read_bytes(), sources_path.read_bytes()), before)

    def test_custom_and_staged_handoffs_do_not_inject_module(self):
        custom = self.root / "CUSTOM_SKILL.md"
        custom.write_text("# Custom skill\n", encoding="utf-8")
        custom_result = self.prepare("custom", skill_path=custom)
        self.assertNotIn(MODULE, self.sources(custom_result.workspace)["skill"]["modules"])

        staged_result = self.prepare("staged", mode="complete", staged_authoring=True, authoring_version=2)
        staged_sources = self.sources(staged_result.workspace)
        self.assertNotIn(MODULE, staged_sources["skill"]["modules"])
        self.assertIn("handoff/skill/references/execution/stages.md", staged_sources["skill"]["modules"])

    def test_illustration_revision_does_not_inject_local_agent_module(self):
        slides = self.root / "slides"
        slides.mkdir()
        (slides / "slide_001.png").write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j0ioAAAAASUVORK5CYII="
        ))
        note = self.root / "submitted.md"
        note.write_text("# Existing note\n\nKeep this text.\n", encoding="utf-8")
        result = prepare_lecture(
            self.root / "illustrate", lecture_id="illustrate", course_title="本地 Agent 测试",
            transcript=self.transcript, slides_dir=slides, mode="deep", use_slides_as_source=True,
            embed_slides=True, skill_path=self.skill,
            revision={"action": "illustrate", "basis": "note_with_original_materials",
                      "sha256": "a" * 64, "text": note.read_text(encoding="utf-8"), "warnings": []},
        )
        self.assertEqual(self.sources(result.workspace)["revision"]["action"], "illustrate")
        self.assertNotIn(MODULE, self.sources(result.workspace)["skill"]["modules"])

    def test_manual_bundle_contains_local_agent_module(self):
        from handouter.agents.manual import create_manual_bundle

        result = self.prepare("manual")
        bundle = create_manual_bundle(result.workspace)
        import zipfile

        with zipfile.ZipFile(bundle.bundle) as archive:
            self.assertIn(MODULE, archive.namelist())
            self.assertIn(MODULE.encode(), archive.read("handoff/PROMPT.md"))

    def test_web_handoff_materials_contains_local_agent_module_bytes(self):
        from handouter.agents.web import create_web_handoff

        result = self.prepare("web")
        web = create_web_handoff(result.workspace)
        from handouter.local_agent_workflow import INSTRUCTIONS

        self.assertIn(INSTRUCTIONS.encode("utf-8"), web.materials.read_bytes())

    def test_cli_runner_receives_module_bytes_and_publishes_expected_output(self):
        result = self.prepare("runner")
        observed = {}

        def fake_run(command, **kwargs):
            task = Path(kwargs["cwd"])
            sources = json.loads((task / "handoff/sources.json").read_text(encoding="utf-8"))
            observed["module"] = (task / MODULE).read_bytes()
            observed["prompt"] = kwargs["input"]
            for relative in sources["expected_outputs"].values():
                (task / relative).write_text("# 本地讲义\n\n依据课程材料整理。\n", encoding="utf-8")
            return SimpleNamespace(returncode=0)

        with patch("handouter.agents.codex.availability", return_value=AgentAvailability("codex", True, "/fake/codex", "cli")), \
             patch("handouter.agents.codex.build_command", return_value=["/fake/codex", "exec", "-"]), \
             patch("handouter.agents.runner.subprocess.run", side_effect=fake_run):
            run = run_cli_agent(result.workspace, agent="codex")

        from handouter.local_agent_workflow import INSTRUCTIONS
        output = result.workspace / run.outputs["deep"]
        self.assertTrue(run.validation_ok, run.validation_errors)
        self.assertEqual(observed["module"], INSTRUCTIONS.encode("utf-8"))
        self.assertIn(MODULE, observed["prompt"])
        self.assertEqual(output.read_text(encoding="utf-8"), "# 本地讲义\n\n依据课程材料整理。\n")


if __name__ == "__main__":
    unittest.main()
