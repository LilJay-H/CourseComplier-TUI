"""Codex CLI adapter."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .base import AgentAvailability

NAME = "codex"


def availability() -> AgentAvailability:
    executable = shutil.which("codex")
    return AgentAvailability(NAME, bool(executable), executable, "cli")


def sandbox_arguments(platform_name: str | None = None) -> list[str]:
    """Codex on native Windows has no workspace-write sandbox (it silently
    falls back to read-only and cannot write the note). The runner already
    executes the agent inside a throw-away copy of the workspace, so Windows
    uses Codex's unsandboxed mode there; other platforms keep workspace-write."""
    if (platform_name or sys.platform) == "win32":
        return ["--dangerously-bypass-approvals-and-sandbox"]
    return ["--sandbox", "workspace-write"]


def build_command(workspace: str | Path, *, model: str | None = None,
                  platform_name: str | None = None) -> list[str]:
    info = availability()
    if not info.executable:
        raise FileNotFoundError("Codex CLI not found on PATH")
    command = [
        info.executable,
        "exec",
        "-C",
        str(Path(workspace).resolve()),
        *sandbox_arguments(platform_name),
        "--skip-git-repo-check",
        "--ephemeral",
        "--color",
        "never",
    ]
    if model:
        command += ["--model", model]
    command.append("-")
    return command
