"""Structural validation for prepared Handouter workspaces and handoff bundles."""

from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any

from .safe_io import relative_path, safe_path, read_bytes, role_path, publish_bytes, sha256_file
from .resources import has_task_placeholders
from .note_validation import NoteValidationReport, _VisibleNoteText, _pending_check_warnings


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    errors: tuple[str, ...]
    checked_files: int
    slide_events: int



def _safe_relative(value: Any) -> str:
    return relative_path(value)


def _dump(path: Path, data: Any) -> None:
    publish_bytes(path.parent, path.name,
                  (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8'), replace=True)


def validate_workspace(workspace: str | Path, *, update_state: bool = False, _transaction: bool = False) -> ValidationReport:
    workspace = Path(workspace)
    if not _transaction and (workspace / '.refresh-transaction').exists():
        return ValidationReport(False, ('Prompt 刷新事务待恢复，请重新进入任务',), 0, 0)
    errors: list[str] = []
    checked_files = 0
    slide_events = 0

    try:
        manifest = json.loads(read_bytes(workspace, 'manifest.json'))
        state = json.loads(read_bytes(workspace, 'state.json'))
        sources = json.loads(read_bytes(workspace, 'handoff/sources.json'))
        prompt = read_bytes(workspace, 'handoff/PROMPT.md').decode('utf-8')
        if not all(isinstance(value, dict) for value in (manifest, state, sources)):
            raise ValueError('manifest/state/sources 必须是 JSON 对象')
        from .agents.manual import selected_files
        selected_files(workspace, sources)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return ValidationReport(False, (f"核心交接文件不可读: {exc}",), 0, 0)

    if has_task_placeholders(prompt, prompt=True):
        errors.append("PROMPT.md 仍含未填写模板占位符")
    if manifest.get("lecture_id") != sources.get("lecture_id"):
        errors.append("manifest 与 sources 的 lecture_id 不一致")
    if manifest.get("course_title") != sources.get("course_title"):
        errors.append("manifest 与 sources 的 course_title 不一致")

    handoff_state = state.get("handoff")
    if not isinstance(handoff_state, dict) or handoff_state.get("status") != "ready":
        errors.append("state.json 未标记 handoff ready")
    else:
        if handoff_state.get("mode") != sources.get("mode"):
            errors.append("state 与 sources 的 mode 不一致")
        if handoff_state.get("expected_output") != sources.get("expected_output"):
            errors.append("state 与 sources 的 expected_output 不一致")
        if handoff_state.get("modes", [handoff_state.get("mode")]) != sources.get("modes", [sources.get("mode")]):
            errors.append("state 与 sources 的 modes 不一致")
        if handoff_state.get("expected_outputs", {handoff_state.get("mode"): handoff_state.get("expected_output")}) != sources.get("expected_outputs", {sources.get("mode"): sources.get("expected_output")}):
            errors.append("state 与 sources 的 expected_outputs 不一致")
        if handoff_state.get("format_profile", "clean") != sources.get("options", {}).get("format_profile", "clean"):
            errors.append("state 与 sources 的 format_profile 不一致")

    for entry in manifest.get("files", []):
        try:
            relative = _safe_relative(entry.get("path"))
            path = safe_path(workspace, relative, role=entry.get('role'))
            if not path.is_file() or path.is_symlink():
                errors.append(f"manifest 文件缺失或不是普通文件: {relative}")
                continue
            checked_files += 1
            expected = entry.get("sha256")
            if not isinstance(expected, str) or sha256_file(workspace, relative) != expected:
                errors.append(f"文件哈希与 manifest 不一致: {relative}")
        except (ValueError, OSError) as exc:
            errors.append(str(exc))

    transcript_manifest = manifest.get("transcript", {})
    try:
        transcript_rel = _safe_relative(transcript_manifest["path"])
        if not safe_path(workspace, transcript_rel, role='transcript').is_file():
            errors.append("转写文件不存在")
    except (KeyError, TypeError, ValueError):
        errors.append("manifest.transcript.path 无效")

    segments_data: list[dict[str, Any]] = []
    segments_rel = transcript_manifest.get("segments_path")
    if segments_rel:
        try:
            raw_segments = json.loads(read_bytes(workspace, segments_rel, role='transcript_segments'))
            if not isinstance(raw_segments, list) or not all(isinstance(item, dict) for item in raw_segments):
                raise ValueError("transcript/segments.json 必须是对象列表")
            segments_data = raw_segments
            for idx, segment in enumerate(segments_data, 1):
                start, end = segment.get("start_ms"), segment.get("end_ms")
                if start is not None and (type(start) is not int or start < 0):
                    errors.append(f"ASR 第 {idx} 段 start_ms 无效")
                if end is not None and (type(end) is not int or end < 0):
                    errors.append(f"ASR 第 {idx} 段 end_ms 无效")
                if type(start) is int and type(end) is int and end < start:
                    errors.append(f"ASR 第 {idx} 段结束时间早于开始时间")
            if int(transcript_manifest.get("segment_count", -1)) != len(segments_data):
                errors.append("manifest.transcript.segment_count 与实际不一致")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"ASR 分段不可读: {exc}")
    elif int(transcript_manifest.get("segment_count", 0)):
        errors.append("manifest 声称有 ASR 分段但 segments_path 为空")

    slides_manifest = manifest.get("slides", {})
    index_rel = slides_manifest.get("index_path")
    slide_index: list[dict[str, Any]] = []
    if index_rel:
        try:
            raw = json.loads(read_bytes(workspace, index_rel, role='slide_index'))
            if not isinstance(raw, list):
                raise ValueError("slides/index.json 必须是列表")
            slide_index = raw
            slide_events = len(raw)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"PPT 索引不可读: {exc}")
    elif int(slides_manifest.get("event_count", 0)):
        errors.append("manifest 声称存在 PPT 事件但没有 index_path")

    ok_image_paths: set[str] = set()
    missing_images = 0
    unknown_times = 0
    for idx, event in enumerate(slide_index, 1):
        if not isinstance(event, dict):
            errors.append(f"PPT 第 {idx} 条事件不是对象")
            continue
        start_ms = event.get("start_ms")
        if start_ms is None:
            unknown_times += 1
        elif isinstance(start_ms, bool) or not isinstance(start_ms, int) or start_ms < 0:
            errors.append(f"PPT 第 {idx} 条 start_ms 无效")
        status = event.get("status")
        image_path = event.get("image_path")
        if status == "ok":
            try:
                rel = _safe_relative(image_path)
                path = safe_path(workspace, rel, role='slide_image')
                if not path.is_file() or path.is_symlink():
                    errors.append(f"PPT 图片缺失: {rel}")
                else:
                    ok_image_paths.add(rel)
            except (TypeError, ValueError, OSError):
                errors.append(f"PPT 第 {idx} 条有效事件缺少安全 image_path")
        elif status == "missing_image":
            missing_images += 1
            if image_path is not None:
                errors.append(f"PPT 第 {idx} 条标为缺图但仍有 image_path")
        else:
            errors.append(f"PPT 第 {idx} 条 status 无效")

    alignment_rel = slides_manifest.get("alignment_path")
    if alignment_rel:
        try:
            alignment = json.loads(read_bytes(workspace, alignment_rel, role='slide_alignment'))
            if not isinstance(alignment, list):
                raise ValueError("slides/alignment.json 必须是列表")
            if segments_data and len(alignment) != len(segments_data):
                errors.append("alignment 条数与 ASR 分段数不一致")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"PPT 对齐索引不可读: {exc}")

    expected_counts = {
        "event_count": len(slide_index),
        "image_count": len(ok_image_paths),
        "missing_image_count": missing_images,
        "unknown_time_count": unknown_times,
    }
    for key, actual in expected_counts.items():
        if int(slides_manifest.get(key, 0)) != actual:
            errors.append(f"manifest.slides.{key} 与实际索引不一致")

    raw_expected_outputs = sources.get("expected_outputs")
    expected_outputs = raw_expected_outputs if isinstance(raw_expected_outputs, dict) and raw_expected_outputs else {sources.get("mode"): sources.get("expected_output")}
    for mode_name, raw_path in expected_outputs.items():
        try:
            expected_output = _safe_relative(raw_path)
            safe_path(workspace, expected_output, role='note', missing=True)
            if mode_name not in {"verbatim", "full", "deep", "summary", "complete"}:
                errors.append(f"expected_outputs 包含无效模式: {mode_name}")
            if not expected_output.startswith("notes/"):
                errors.append(f"{mode_name} 输出必须位于 notes/ 下")
        except ValueError as exc:
            errors.append(str(exc))

    skill_plan = sources.get("skill")
    if isinstance(skill_plan, dict):
        entry = skill_plan.get("entry")
        modules = skill_plan.get("modules")
        try:
            entry_rel = _safe_relative(entry)
            entry_path = safe_path(workspace, entry_rel, role='skill')
            if not entry_path.is_file() or entry_path.is_symlink():
                errors.append("sources.skill.entry 不存在或不是普通文件")
        except (TypeError, ValueError, OSError):
            errors.append("sources.skill.entry 无效")
        if not isinstance(modules, list):
            errors.append("sources.skill.modules 必须是列表")
        else:
            for idx, module in enumerate(modules, 1):
                try:
                    module_rel = _safe_relative(module)
                    module_path = safe_path(workspace, module_rel, role='skill')
                    if not module_path.is_file() or module_path.is_symlink():
                        errors.append(f"Skill 模块缺失: {module_rel}")
                    if not module_rel.startswith("handoff/skill/"):
                        errors.append(f"Skill 模块必须位于 handoff/skill/: {module_rel}")
                except (TypeError, ValueError, OSError):
                    errors.append(f"Skill 第 {idx} 个模块路径无效")

    from .authoring import contract as authoring_contract
    try:
        authoring_contract(sources)
        from .authoring_stages import policy as stage_policy
        stage_policy(sources)
    except ValueError as exc:
        errors.append(str(exc))

    if sources.get('transcript', {}).get('path') != transcript_manifest.get('path'):
        errors.append('sources 与 manifest 的 transcript path 不一致')
    origin = transcript_manifest.get("origin")
    if origin is not None and sources.get("transcript", {}).get("origin") != origin:
        errors.append("sources 与 manifest 的 transcript origin 不一致")
    if sources.get("transcript", {}).get("origin") == "generated_note" and "verbatim" in expected_outputs:
        errors.append("生成讲义不能作为原始转写生成逐字版")
    if sources.get("transcript", {}).get("segments_path") != transcript_manifest.get("segments_path"):
        errors.append("sources 与 manifest 的 segments_path 不一致")
    if sources.get("slides", {}).get("alignment_path") != slides_manifest.get("alignment_path"):
        errors.append("sources 与 manifest 的 alignment_path 不一致")

    source_options = sources.get("options", {})
    if source_options.get("embed_slides") and not source_options.get("use_slides_as_source"):
        errors.append("sources 配置矛盾：嵌图但未允许 PPT 来源")
    if source_options.get("use_slides_as_source") and not ok_image_paths:
        errors.append("sources 要求使用 PPT，但工作区没有可用图片")

    report = ValidationReport(not errors, tuple(errors), checked_files, slide_events)
    if update_state:
        state["validation"] = {
            "status": "passed" if report.ok else "failed",
            "checked_files": checked_files,
            "slide_events": slide_events,
            "errors": list(report.errors),
        }
        _dump(workspace / "state.json", state)
    return report



def validate_note_output(workspace, *, output=None, update_state=False):
    """Preserve local validation for all authored output modes."""
    from .note_validation import validate_note_output as validate_core
    from .revision import note_text_preserved
    from .source_quotes import check_complete_quotes
    return validate_core(workspace, output=output, update_state=update_state,
                         revision_guard=note_text_preserved,
                         complete_quote_check=check_complete_quotes)
