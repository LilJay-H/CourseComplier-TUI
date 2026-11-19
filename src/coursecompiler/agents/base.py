"""Shared types/helpers for Agent interaction adapters."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from ..safe_io import read_bytes, role_path


@dataclass(frozen=True)
class AgentAvailability:
    name: str
    available: bool
    executable: str | None
    mode: str


@dataclass(frozen=True)
class AgentRunResult:
    agent: str
    returncode: int
    outputs: dict[str, str]
    validation_ok: bool
    validation_errors: tuple[str, ...]


def read_handoff(workspace: str | Path) -> tuple[Path, dict[str, Any], str]:
    from ..organization import resolve_workspace
    root = resolve_workspace(workspace)
    sources_path = root / "handoff" / "sources.json"
    prompt_path = root / "handoff" / "PROMPT.md"
    if not sources_path.is_file() or not prompt_path.is_file():
        raise FileNotFoundError("workspace does not contain a ready handoff")
    sources = json.loads(read_bytes(root, 'handoff/sources.json'))
    if not isinstance(sources, dict):
        raise ValueError("handoff/sources.json must be an object")
    for field in ('skill', 'transcript', 'slides', 'options'):
        if not isinstance(sources.get(field, {}), dict):
            raise ValueError(f'handoff.sources.{field} 必须是对象')
    if not isinstance(sources.get('skill', {}).get('modules', []), list):
        raise ValueError('handoff.sources.skill.modules 必须是列表')
    prompt = read_bytes(root, 'handoff/PROMPT.md').decode('utf-8')
    if not prompt.strip():
        raise ValueError("handoff/PROMPT.md is empty")
    return root, sources, prompt


def expected_outputs(sources: dict[str, Any]) -> dict[str, str]:
    raw = sources.get("expected_outputs")
    if isinstance(raw, dict) and raw:
        items = raw.items()
    else:
        mode = str(sources.get("mode") or "deep")
        relative = sources.get("expected_output")
        items = [(mode, relative)]
    result: dict[str, str] = {}
    seen = set()
    for mode, relative in items:
        if not isinstance(mode, str) or not isinstance(relative, str) or not relative:
            raise ValueError("invalid expected_outputs in sources.json")
        relative = role_path(relative, 'note')
        canonical = unicodedata.normalize('NFC', relative).casefold()
        if mode not in {'deep', 'full', 'verbatim', 'summary', 'complete'} or canonical in seen:
            raise ValueError('expected_outputs 包含无效模式或重复目标')
        seen.add(canonical)
        posix = PurePosixPath(relative)
        if posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts):
            raise ValueError(f"unsafe expected output path: {relative!r}")
        if not posix.as_posix().startswith("notes/"):
            raise ValueError("Agent outputs must stay under notes/")
        result[mode] = posix.as_posix()
    if not result:
        raise ValueError("handoff has no expected outputs")
    return result


def scope_pending(sources: dict, prompt: str, outputs: dict) -> tuple[dict, str]:
    """A transient continuation keeps its original rule snapshot and only missing targets."""
    copied = json.loads(json.dumps(sources))
    all_outputs = expected_outputs(sources)
    if not outputs or any(all_outputs.get(mode) != path for mode, path in outputs.items()):
        raise ValueError('续作目标不属于原任务')
    if outputs != all_outputs:
        completed_paths = set(all_outputs.values()) - set(outputs.values())
        prompt = '\n'.join(line for line in prompt.splitlines()
                           if not (line.lstrip().startswith('- ') and any('`' + p + '`' in line for p in completed_paths)))
        prompt = prompt.replace(f'必须生成 {len(all_outputs)} 个独立 Markdown 文件',
                                f'必须生成 {len(outputs)} 个独立 Markdown 文件')
        prompt += '\n本次为部分成果续作：只生成 sources.json.expected_outputs 中剩余的文件。已完成成果未提供，不重新生成、不读取或覆盖。\n'
        copied.update(expected_outputs=outputs, expected_output=next(iter(outputs.values())),
                      modes=list(outputs), mode=next(iter(outputs)))
    return copied, prompt
