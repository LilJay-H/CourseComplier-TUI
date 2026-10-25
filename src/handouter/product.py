"""User-facing product helpers for simple input/output directory workflows."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shlex
import zipfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit


@dataclass(frozen=True)
class AssetIdentity:
    course_title: str | None
    lecture_id: str
    lecture_date: str | None = None
    source_kind: str = "zhiyun_asset_zip"
    has_slides: bool | None = True


@dataclass(frozen=True)
class ProductArtifacts:
    workspace: Path
    exporter_script: Path | None
    prompt: Path
    transcript: Path
    slides: Path
    notes: Path
    expected_notes: dict[str, Path]

    @property
    def expected_note(self) -> Path:
        """Compatibility alias for the first selected deliverable."""
        return next(iter(self.expected_notes.values()))

    def as_dict(self) -> dict[str, Any]:
        from .organization import package_for_workspace
        package = package_for_workspace(self.workspace)
        return {
            "collection": str(package) if package else None,
            "exporter_script": str(self.exporter_script) if self.exporter_script else None,
            "prompt": str(self.prompt),
            "transcript": str(self.transcript),
            "slides": str(self.slides),
            "notes": str(self.notes),
            "expected_note": str(self.expected_note),
            "expected_notes": {mode: str(path) for mode, path in self.expected_notes.items()},
        }


def exporter_script_path() -> Path | None:
    """Return the userscript path in a checkout or an installed wheel."""
    checkout = Path(__file__).resolve().parents[2] / "zhiyun_exporter.user.js"
    if checkout.is_file():
        return checkout
    packaged = Path(__file__).resolve().parent / "assets" / "zhiyun_exporter.user.js"
    return packaged if packaged.is_file() else None


def _read_small_json(archive: zipfile.ZipFile, name: str, *, limit: int = 1024 * 1024) -> dict[str, Any]:
    try:
        info = archive.getinfo(name)
    except KeyError:
        return {}
    if info.file_size > limit or info.flag_bits & 1:
        return {}
    with archive.open(info) as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        return {}
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _page_query(page_url: str) -> dict[str, list[str]]:
    split = urlsplit(page_url)
    query = split.query
    if not query and "?" in split.fragment:
        query = split.fragment.split("?", 1)[1]
    return parse_qs(query)


def inspect_asset_zip(asset_zip: str | Path) -> AssetIdentity:
    """Extract safe display identity without exposing private signed URLs."""
    path = Path(asset_zip).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"资产 ZIP 不存在: {path}")
    with zipfile.ZipFile(path, "r") as archive:
        course_info = _read_small_json(archive, "course_info.json")
        private_info = _read_small_json(archive, "private/source.json")
    title_value = course_info.get("courseName")
    title = title_value.strip() if isinstance(title_value, str) and title_value.strip() else None
    page_url = private_info.get("pageUrl") or course_info.get("pageUrl")
    page_url = page_url.strip() if isinstance(page_url, str) else ""
    query = _page_query(page_url) if page_url else {}
    tenant = (query.get("tenant_code") or [""])[0]
    course = (query.get("course_id") or [""])[0]
    sub = (query.get("sub_id") or [""])[0]
    parts = [part for part in (tenant, course, sub) if re.fullmatch(r"[A-Za-z0-9_-]+", part or "")]
    if course and sub and len(parts) >= 2:
        identity = "zhiyun-" + "-".join(parts)
    else:
        seed = page_url or path.name
        identity = "zhiyun-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    from .organization import lecture_date
    # Export timestamps and filenames are not classroom dates. Only explicitly
    # typed lecture-date metadata (or a user override) has this meaning.
    day = lecture_date(course_info.get('lectureDate', course_info.get('lecture_date')))
    return AssetIdentity(title, identity, day)


def inspect_input_source(source: str | Path, *, source_kind: str | None = None) -> AssetIdentity:
    """Inspect a site ZIP or a direct local audio/media/transcript input."""
    candidate = Path(source).expanduser()
    if candidate.is_symlink():
        raise FileNotFoundError(f"输入不存在或不是普通文件: {candidate}")
    path = candidate.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"输入不存在或不是普通文件: {path}")
    from .importers import (LOCAL_ASSET_MANIFEST, LOCAL_AUDIO_SUFFIXES, LOCAL_MEDIA_SUFFIXES,
                            LOCAL_TRANSCRIPT_SUFFIXES, inspect_local_asset_zip)
    suffix = path.suffix.lower()
    kind = source_kind
    if kind is None and suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            kind = "local_asset_package" if LOCAL_ASSET_MANIFEST in archive.namelist() else "zhiyun_asset_zip"
    elif kind is None and suffix in LOCAL_AUDIO_SUFFIXES:
        kind = "local_audio"
    elif kind is None and suffix in LOCAL_MEDIA_SUFFIXES:
        kind = "local_media"
    elif kind is None and suffix in LOCAL_TRANSCRIPT_SUFFIXES:
        kind = "local_transcript"
    if kind == "zhiyun_asset_zip":
        return inspect_asset_zip(path)
    if kind == "local_asset_package":
        metadata = inspect_local_asset_zip(path)
        from .stage_cache import asset_fingerprint
        fingerprint = asset_fingerprint(path)
        return AssetIdentity(metadata["course_title"], metadata["lecture_id"] or "local-" + fingerprint[:12],
                             metadata["lecture_date"], kind, metadata["has_slides"])
    allowed = {
        "local_audio": LOCAL_AUDIO_SUFFIXES,
        "local_media": LOCAL_MEDIA_SUFFIXES | LOCAL_AUDIO_SUFFIXES,
        "local_transcript": LOCAL_TRANSCRIPT_SUFFIXES,
    }
    if kind not in allowed or suffix not in allowed[kind]:
        raise ValueError("输入类型与文件扩展名不匹配；请使用受支持的音频、视频、UTF-8 TXT 或资产 ZIP")
    from .stage_cache import source_fingerprint
    fingerprint = source_fingerprint(path)
    return AssetIdentity(path.stem.strip() or None, "local-" + fingerprint[:12], None, kind, False)


def resolve_input_source(input_dir: str | Path, source: str | Path, *, source_kind: str | None = None) -> Path:
    normalized = normalize_terminal_path(source)
    candidate = Path(normalized)
    if candidate.is_absolute():
        unresolved = candidate
    else:
        directory = Path(input_dir).expanduser().resolve()
        if not directory.is_dir():
            raise NotADirectoryError(f"输入目录不存在或不是目录: {directory}")
        unresolved = directory / candidate
    if unresolved.is_symlink():
        raise FileNotFoundError(f"输入不存在或不是普通文件: {unresolved}")
    candidate = unresolved.resolve()
    inspect_input_source(candidate, source_kind=source_kind)
    return candidate


def _strip_matching_quotes(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _normalize_windows_terminal_path(text: str) -> str:
    text = _strip_matching_quotes(text.strip())
    if text.lower().startswith("file://"):
        split = urlsplit(text)
        decoded = unquote(split.path)
        if split.netloc and split.netloc.lower() not in {"", "localhost"}:
            return str(PureWindowsPath(f"//{split.netloc}{decoded}"))
        if re.match(r"^/[A-Za-z]:/", decoded):
            decoded = decoded[1:]
        return str(PureWindowsPath(decoded))
    return str(PureWindowsPath(text))


def _normalize_posix_terminal_path(text: str) -> str:
    if text.startswith("file://"):
        split = urlsplit(text)
        text = unquote(split.path)
    else:
        try:
            parsed = shlex.split(text)
        except ValueError:
            parsed = []
        if len(parsed) == 1 and (text[:1] in {"'", '"'} or "\\" in text):
            text = parsed[0]
        else:
            text = _strip_matching_quotes(text)
    return posixpath.expanduser(text)


def normalize_terminal_path(value: str | Path, *, platform_name: str | None = None) -> str:
    """Normalize a path pasted or dropped by common macOS/Windows terminals."""
    text = str(value).strip()
    if not text:
        return ""
    platform_key = (platform_name or os.name).lower()
    if platform_key in {"nt", "windows", "win32"}:
        return _normalize_windows_terminal_path(text)
    return _normalize_posix_terminal_path(text)


def normalize_dropped_path(value: str | Path) -> str:
    """Backward-compatible alias for older callers/tests."""
    return normalize_terminal_path(value)


def discover_asset_zips(input_dir: str | Path) -> list[Path]:
    directory = Path(input_dir).expanduser()
    if not directory.is_dir():
        raise NotADirectoryError(f"输入目录不存在或不是目录: {directory}")
    return sorted(
        (path for path in directory.iterdir() if path.is_file() and path.suffix.lower() == ".zip"),
        key=lambda path: (-path.stat().st_mtime_ns, path.name.casefold()),
    )


def resolve_asset_zip(input_dir: str | Path = ".", asset: str | Path | None = None) -> Path:
    if asset is not None and str(asset).strip():
        normalized = normalize_terminal_path(asset)
        candidate = Path(normalized)
        if candidate.is_absolute():
            candidate = candidate.resolve()
        else:
            directory = Path(input_dir).expanduser().resolve()
            if not directory.is_dir():
                raise NotADirectoryError(f"输入目录不存在或不是目录: {directory}")
            candidate = (directory / candidate).resolve()
        if not candidate.is_file() or candidate.suffix.lower() != ".zip":
            raise FileNotFoundError(f"找不到资产 ZIP: {candidate}")
        return candidate

    directory = Path(input_dir).expanduser().resolve()
    if not directory.is_dir():
        raise NotADirectoryError(f"输入目录不存在或不是目录: {directory}")
    candidates = discover_asset_zips(directory)
    if not candidates:
        raise FileNotFoundError(f"输入目录没有 ZIP: {directory}")
    if len(candidates) > 1:
        names = ", ".join(path.name for path in candidates[:8])
        extra = " …" if len(candidates) > 8 else ""
        raise ValueError(f"输入目录有多个 ZIP，请明确选择资产文件: {names}{extra}")
    return candidates[0]


def product_artifacts(workspace: str | Path) -> ProductArtifacts:
    from .organization import resolve_workspace, package_for_workspace
    root = resolve_workspace(workspace)
    package = package_for_workspace(root)
    state_path = root / "state.json"
    if not state_path.is_file():
        raise FileNotFoundError(f"工作区缺少 state.json: {root}")
    state: dict[str, Any] = json.loads(state_path.read_text(encoding="utf-8"))
    handoff = state.get("handoff")
    if not isinstance(handoff, dict) or handoff.get("status") != "ready":
        raise ValueError("工作区还没有可用 Prompt")
    raw_outputs = handoff.get("expected_outputs")
    if isinstance(raw_outputs, dict) and raw_outputs:
        expected_outputs = {
            str(mode): root / str(relative)
            for mode, relative in raw_outputs.items()
            if isinstance(mode, str) and isinstance(relative, str) and relative
        }
    else:
        expected = handoff.get("expected_output")
        if not isinstance(expected, str) or not expected:
            raise ValueError("handoff 缺少 expected_output(s)")
        expected_outputs = {str(handoff.get("mode") or "deep"): root / expected}
    if not expected_outputs:
        raise ValueError("handoff 没有有效 expected_outputs")
    return ProductArtifacts(
        workspace=root,
        exporter_script=exporter_script_path(),
        prompt=root / str(handoff.get("prompt_path", "handoff/PROMPT.md")),
        transcript=root / "transcript" / "transcript.txt",
        slides=root / "slides" / "images",
        notes=(package / '成果/notes') if package else root / "notes",
        expected_notes=({mode: package / '成果' / path.relative_to(root)
                         for mode, path in expected_outputs.items()} if package else expected_outputs),
    )
