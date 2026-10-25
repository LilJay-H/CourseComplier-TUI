"""Manual/GUI Agent handoff bundle creation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .base import expected_outputs, read_handoff, scope_pending
from ..safe_io import relative_path, safe_path, read_bytes, role_path, publish_exclusive, locked_workspace
from ..credentials import scan_credentials


@dataclass(frozen=True)
class ManualBundleResult:
    workspace: Path
    bundle: Path
    prompt: Path
    expected_outputs: dict[str, str]
    included_files: tuple[str, ...]


def _safe_relative(value: str) -> str:
    return relative_path(value)


def _next_bundle_path(root: Path) -> Path:
    from ..organization import artifact_prefix
    prefix = artifact_prefix(root)
    handoff = root / "handoff"
    for index in range(1, 10_000):
        target = handoff / f"{prefix}gui-handoff-{index:03d}.zip"
        if not target.exists():
            return target
    raise RuntimeError("too many GUI handoff bundles")


def _add_file(root: Path, relative: str, files: dict[str, Path]) -> None:
    safe = _safe_relative(relative)
    path = safe_path(root, safe)
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError(f"handoff file missing or unsafe: {safe}")
    files[safe] = path


def _publish_exclusive_file(source: Path, target: Path) -> None:
    """Publish a verified file without overwriting an existing destination."""
    publish_exclusive(source, target, allow_copy=True)


def selected_files(root: Path, sources: dict) -> dict[str, bytes]:
    """One role-restricted material snapshot for all Agent transports."""
    roles = {'handoff/PROMPT.md': None}
    skill = sources.get('skill', {})
    if not skill.get('entry'):
        raise ValueError('请先刷新 Prompt，缺少 Skill module plan')
    roles[role_path(skill['entry'], 'skill')] = 'skill'
    roles.update({role_path(p, 'skill'): 'skill' for p in skill.get('modules', [])})
    transcript = sources.get('transcript', {})
    for key, role in [('path', 'transcript'), ('segments_path', 'transcript_segments')]:
        if transcript.get(key):
            roles[role_path(transcript[key], role)] = role
    if sources.get('options', {}).get('use_slides_as_source'):
        slides = sources.get('slides', {})
        for key, role in [('index_path', 'slide_index'), ('alignment_path', 'slide_alignment')]:
            if slides.get(key):
                roles[role_path(slides[key], role)] = role
        if slides.get('index_path'):
            events = json.loads(read_bytes(root, slides['index_path'], role='slide_index'))
            for event in events:
                if event.get('image_path'):
                    roles[role_path(event['image_path'], 'slide_image')] = 'slide_image'
    snapshot = {p: read_bytes(root, p, role=role) for p, role in roles.items()}
    from ..authoring import RESUME_PATH, latest_progress
    resume = latest_progress(root, sources)
    if resume is not None:
        snapshot[RESUME_PATH] = resume
    for relative, data in snapshot.items():
        if relative.endswith(('.md', '.txt', '.json')):
            report = scan_credentials(data.decode('utf-8'))
            if report.errors:
                raise ValueError('; '.join(report.errors))
    report = scan_credentials(json.dumps(sources, ensure_ascii=False))
    if report.errors:
        raise ValueError('; '.join(report.errors))
    return snapshot


@locked_workspace
def create_manual_bundle(workspace: str | Path, *, output: str | Path | None = None, modes=None) -> ManualBundleResult:
    """Create a portable, credential-free bundle for GUI/Web agents.

    The archive mirrors workspace-relative paths so the same PROMPT.md works
    after upload/extraction. It intentionally excludes audio, raw ASR, private
    URLs, manifest/state, and unselected existing notes. An explicitly submitted
    note is included as a read-only revision snapshot inside sources.json.
    """
    root, sources, _prompt_text = read_handoff(workspace)
    if sources.get('authoring_stages'):
        raise ValueError('STAGE_TRANSPORT_UNSUPPORTED：manual 未实现阶段导入；使用 CLI 或显式刷新关闭分阶段候选')
    outputs = expected_outputs(sources)
    snapshot = selected_files(root, sources)
    if modes is not None:
        outputs = {mode: outputs[mode] for mode in modes}
    sources_for_bundle, scoped_prompt = scope_pending(sources, _prompt_text, outputs)
    snapshot['handoff/PROMPT.md'] = scoped_prompt.encode('utf-8')
    warnings = scan_credentials('\n'.join(data.decode('utf-8') for relative, data in snapshot.items()
                                         if relative.endswith(('.md', '.txt', '.json')))).warnings
    sources_for_bundle['privacy_warnings'] = list(warnings)
    transcript_for_bundle = sources_for_bundle.get("transcript")
    if isinstance(transcript_for_bundle, dict):
        transcript_for_bundle["raw_asr_path"] = None

    target = Path(output).expanduser().absolute() if output is not None else _next_bundle_path(root)
    # Explicit external output directories are trusted selections; within this
    # workspace, every parent must obey the workspace link policy.
    lexical_root = Path(workspace).expanduser().absolute()
    if target.is_relative_to(lexical_root):
        target = safe_path(root, target.relative_to(lexical_root).as_posix(), missing=True)
    elif target.is_relative_to(root):
        target = safe_path(root, target.relative_to(root).as_posix(), missing=True)
    else:
        target = target.parent.resolve() / target.name
    if target.exists():
        raise FileExistsError(f"bundle already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    bundled_sources = (json.dumps(sources_for_bundle, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    readme = (
        "# Course Compiler GUI Agent handoff\n\n"
        "1. Upload this archive to the GUI/Web agent.\n"
        "2. Ask the agent to read `handoff/PROMPT.md` first.\n"
        "3. The prompt references only files included in this archive.\n"
        "4. Expected outputs: " + ", ".join(outputs.values()) + "\n"
        "5. This bundle intentionally excludes audio, private URLs/cookies, raw ASR, state, and old notes.\n"
    ).encode("utf-8")

    temporary = target.with_name(f".{target.name}.tmp-{uuid.uuid4().hex}")
    try:
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("BUNDLE_README.md", readme)
            archive.writestr("handoff/sources.json", bundled_sources)
            for relative, data in sorted(snapshot.items()):
                archive.writestr(relative, data)
            checksums = {
                relative: hashlib.sha256(data).hexdigest()
                for relative, data in sorted(snapshot.items())
            }
            checksums["handoff/sources.json"] = hashlib.sha256(bundled_sources).hexdigest()
            archive.writestr(
                "BUNDLE_MANIFEST.json",
                json.dumps({"files": checksums, "expected_outputs": outputs}, ensure_ascii=False, indent=2) + "\n",
            )
        with zipfile.ZipFile(temporary, "r") as archive:
            broken = archive.testzip()
            if broken is not None:
                raise RuntimeError(f"GUI handoff bundle verification failed at: {broken}")
        # Publish only a verified archive, never overwriting an existing bundle.
        _publish_exclusive_file(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)

    included = ("BUNDLE_README.md", "BUNDLE_MANIFEST.json", "handoff/sources.json", *sorted(snapshot))
    return ManualBundleResult(root, target, root / "handoff" / "PROMPT.md", outputs, tuple(included))
