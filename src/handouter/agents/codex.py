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
    falls back to read-only and cannot write the note), so Windows uses
    Codex's unsandboxed mode; other platforms keep workspace-write.

    This is a real loss of isolation, not an equivalent: the throw-away copy
    the runner starts the agent in only decides what it is handed and what is
    audited afterwards. An unsandboxed Codex can still reach the network and
    any path the user can write. The TUI labels the choice "no sandbox on
    Windows" and the README states it; switch back once Codex ships a usable
    Windows sandbox."""
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
