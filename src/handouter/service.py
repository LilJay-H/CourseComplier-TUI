"""Application service for the M1: existing materials -> workspace -> handoff."""

from __future__ import annotations

import shutil
import json
import uuid
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ProgressCallback = Callable[[str, int, str], None]


def _progress(callback: ProgressCallback | None, stage: str, percent: int, detail: str = "") -> None:
    if callback is not None:
        callback(stage, percent, detail)

from .asr import ASRResult, transcribe_sensevoice
from .handoff import HandoffSummary, create_handoff
from .importers import import_local_asset_zip, import_slides_zip
from .media import AudioExtractResult, extract_audio
from .validation import ValidationReport, validate_workspace
from .workspace import WorkspaceSummary, create_workspace, read_json, validate_lecture_id
from .revision import read_revision_note
from .safe_io import locked_workspace, locked_output_root, publish_directory, read_json as read_safe_json
from .organization import organized_operation


@dataclass(frozen=True)
class PrepareResult:
    workspace: Path
    materials: WorkspaceSummary
    handoff: HandoffSummary
    validation: ValidationReport


@dataclass(frozen=True)
class RefreshHandoffResult:
    workspace: Path
    handoff: HandoffSummary
    validation: ValidationReport
    archived_prompt: Path


@dataclass(frozen=True)
class BuildResult:
    prepare: PrepareResult
    asr: ASRResult
    media: AudioExtractResult | None


def preflight_build(root: Path, *, lecture_id: str, course_title: str, media: bool,
                    progress: ProgressCallback | None = None, **options) -> None:
    """Exercise the real importer/handoff in disposable storage before ASR.

    This also probes output writability. The disk estimate is a lower bound,
    not a prediction of remote media length, model downloads or peak ASR use.
    """
    _progress(progress, '材料预检', 0, '检查课件、输出目录与任务配置')
    with tempfile.TemporaryDirectory(prefix='.handouter-preflight-', dir=root) as temporary:
        scratch = Path(temporary)
        transcript = scratch / 'synthetic.txt'
        transcript.write_text('Synthetic preflight 中文材料检查。', encoding='utf-8')
        prepare_lecture(scratch, lecture_id=lecture_id, course_title=course_title,
                        transcript=transcript, **options)
    available = shutil.disk_usage(root).free
    if available < 128 * 1024 * 1024:
        raise OSError('可用空间不足 128 MiB；请更换输出目录。该下限不保证长课所需空间。')
    _progress(progress, '材料预检', 100, '材料结构可用；磁盘检查仅为下限估计，不保证长课空间')


@organized_operation
@locked_output_root
def prepare_lecture(
    workspace_root: str | Path,
    *,
    lecture_id: str,
    course_title: str,
    transcript: str | Path,
    segments: str | Path | None = None,
    raw_asr: str | Path | None = None,
    audio: str | Path | None = None,
    slides_dir: str | Path | None = None,
    slides_meta: str | Path | None = None,
    slides_zip: str | Path | None = None,
    mode: str = "deep",
    modes: tuple[str, ...] | list[str] | None = None,
    use_slides_as_source: bool | None = None,
    embed_slides: bool = False,
    allow_web: bool = False,
    format_profile: str = "clean",
    skill_path: str | Path | None = None,
    source_type: str = "local_existing_materials",
    transcript_origin: str | None = None,
    revision: dict | None = None,
    input_fingerprint: str | None = None,
    output_organization: dict | None = None,
    cache_root: Path | None = None,
    authoring_version: int = 1,
    staged_authoring: bool = False,
    compose_blocks: bool = False,
) -> PrepareResult:
    """Atomically prepare one new lecture workspace and validated handoff bundle."""

    lecture_id = validate_lecture_id(lecture_id)
    root = Path(workspace_root)
    if root.exists() and not root.is_dir():
        raise NotADirectoryError(f"workspace_root 不是目录: {root}")
    root.mkdir(parents=True, exist_ok=True)
    target = root / lecture_id
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"讲次工作区已存在，拒绝覆盖: {target}")
    if embed_slides and use_slides_as_source is False:
        raise ValueError("正文嵌图时不能禁用 PPT 来源")
    if slides_zip is not None and (slides_dir is not None or slides_meta is not None):
        raise ValueError("slides_zip 不能与 slides_dir/slides_meta 同时使用")

    imported_slides: Path | None = None
    if slides_zip is not None:
        imported_slides = root / f".{lecture_id}.slides-{uuid.uuid4().hex}"
        import_slides_zip(slides_zip, imported_slides)
        slides_dir = imported_slides
        slides_meta = imported_slides / "slides_meta.json"

    staging = root / f".{lecture_id}.tmp-{uuid.uuid4().hex}"
    try:
        materials = create_workspace(
            staging,
            lecture_id=lecture_id,
            course_title=course_title,
            transcript=transcript,
            segments=segments,
            raw_asr=raw_asr,
            audio=audio,
            slides_dir=slides_dir,
            slides_meta=slides_meta,
            source_type=source_type,
            transcript_origin=transcript_origin,
            output_organization=output_organization,
        )
        manifest = read_json(staging / "manifest.json")
        if input_fingerprint:
            manifest['input_fingerprint'] = input_fingerprint
            (staging / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        has_slides = int(manifest.get("slides", {}).get("image_count", 0)) > 0
        resolved_use_slides = has_slides if use_slides_as_source is None else bool(use_slides_as_source)
        if embed_slides:
            resolved_use_slides = True

        handoff = create_handoff(
            staging,
            mode=mode,
            modes=modes,
            use_slides_as_source=resolved_use_slides,
            embed_slides=embed_slides,
            allow_web=allow_web,
            format_profile=format_profile,
            skill_path=skill_path,
            revision=revision,
            authoring_version=authoring_version,
            staged_authoring=staged_authoring,
            compose_blocks=compose_blocks,
        )
        validation = validate_workspace(staging, update_state=True)
        if not validation.ok:
            details = "; ".join(validation.errors)
            raise RuntimeError(f"交接工作区验证失败: {details}")
        publish_directory(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        if imported_slides is not None:
            shutil.rmtree(imported_slides, ignore_errors=True)

    materials = WorkspaceSummary(
        lecture_id=materials.lecture_id,
        course_title=materials.course_title,
        workspace=target,
        transcript_path=materials.transcript_path,
        slide_event_count=materials.slide_event_count,
        slide_image_count=materials.slide_image_count,
        slide_missing_count=materials.slide_missing_count,
        slide_unknown_time_count=materials.slide_unknown_time_count,
    )
    return PrepareResult(target, materials, handoff, validation)


@locked_workspace
def refresh_handoff(
    workspace: str | Path,
    *,
    mode: str | None = None,
    modes: tuple[str, ...] | list[str] | None = None,
    use_slides_as_source: bool | None = None,
    embed_slides: bool | None = None,
    allow_web: bool | None = None,
    format_profile: str | None = None,
    skill_path: str | Path | None = None,
    source_note: str | Path | None = None,
    revision_action: str | None = None,
    clear_source_note: bool = False,
    authoring_version: int | None = None,
    staged_authoring: bool | None = None,
    compose_blocks: bool | None = None,
) -> RefreshHandoffResult:
    """Create a new Prompt for an existing lecture without rerunning media/ASR.

    The previous Prompt and sources index are archived under ``handoff/history``.
    If regeneration fails, the previous handoff/state are restored.
    """
    root = Path(workspace)
    if not root.is_dir():
        raise NotADirectoryError(f"讲次工作区不存在: {root}")
    prompt = root / "handoff" / "PROMPT.md"
    sources_path = root / "handoff" / "sources.json"
    state_path = root / "state.json"
    if not prompt.is_file() or not sources_path.is_file() or not state_path.is_file():
        raise FileNotFoundError("工作区缺少现有 handoff/state，无法刷新 Prompt")

    preflight = validate_workspace(root)
    if not preflight.ok:
        raise ValueError('刷新前材料验证失败: ' + '; '.join(preflight.errors))
    current = read_safe_json(root, 'handoff/sources.json')
    revision = None if clear_source_note else current.get("revision")
    if source_note is not None:
        revision = read_revision_note(source_note, action=revision_action or "derive")
    elif revision_action is not None:
        if not revision:
            raise ValueError("请先提交已有讲义 --source-note")
        revision = {**revision, "action": revision_action}
    options = current.get("options", {}) if isinstance(current, dict) else {}
    current_modes = current.get("modes") if isinstance(current.get("modes"), list) else None
    if modes is not None:
        resolved_modes = list(modes)
    elif mode is not None:
        resolved_modes = [mode]
    else:
        resolved_modes = current_modes or [str(current.get("mode") or "deep")]
    resolved_mode = resolved_modes[0]
    resolved_use_slides = bool(options.get("use_slides_as_source")) if use_slides_as_source is None else bool(use_slides_as_source)
    resolved_embed = bool(options.get("embed_slides")) if embed_slides is None else bool(embed_slides)
    if revision and revision["action"] == "illustrate":
        resolved_embed = True
    resolved_web = bool(options.get("allow_web")) if allow_web is None else bool(allow_web)
    resolved_format = format_profile or str(options.get("format_profile") or "clean")
    if resolved_embed:
        resolved_use_slides = True

    def build(staging):
        from .safe_io import read_bytes
        return create_handoff(
            staging,
            mode=resolved_mode,
            modes=resolved_modes,
            use_slides_as_source=resolved_use_slides,
            embed_slides=resolved_embed,
            allow_web=resolved_web,
            format_profile=resolved_format,
            skill_path=skill_path,
            revision=revision,
            material_text=read_bytes(root, current["transcript"]["path"]).decode("utf-8-sig"),
            authoring_version=(authoring_version if authoring_version is not None else
                               (current.get('authoring') or {}).get('schema_version', 1)),
            authoring_source_root=root,
            staged_authoring=(staged_authoring if staged_authoring is not None else bool(current.get('authoring_stages'))),
            compose_blocks=(compose_blocks if compose_blocks is not None else bool(current.get('composer'))),
        )
    from .transactions import refresh_transaction
    handoff, validation, archived_prompt = refresh_transaction(root, build)
    return RefreshHandoffResult(root, handoff, validation, archived_prompt)


@organized_operation
@locked_output_root
def build_from_audio(
    workspace_root: str | Path,
    *,
    lecture_id: str,
    course_title: str,
    audio: str | Path,
    slides_dir: str | Path | None = None,
    slides_meta: str | Path | None = None,
    slides_zip: str | Path | None = None,
    mode: str = "deep",
    modes: tuple[str, ...] | list[str] | None = None,
    use_slides_as_source: bool | None = None,
    embed_slides: bool = False,
    allow_web: bool = False,
    format_profile: str = "clean",
    skill_path: str | Path | None = None,
    device: str = "auto",
    language: str = "auto",
    model_path: str | None = None,
    progress: ProgressCallback | None = None,
    output_organization: dict | None = None,
    cache_root: Path | None = None,
    source_type: str = "local_audio_asr",
    input_fingerprint: str | None = None,
) -> BuildResult:
    """ASR an existing local audio file, then prepare a validated handoff workspace."""
    lecture_id = validate_lecture_id(lecture_id)
    root = Path(workspace_root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / lecture_id
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"讲次工作区已存在，拒绝覆盖: {target}")
    preflight_build(root, lecture_id=lecture_id, course_title=course_title, media=False,
                    progress=progress, slides_dir=slides_dir, slides_meta=slides_meta, slides_zip=slides_zip,
                    mode=mode, modes=modes, use_slides_as_source=use_slides_as_source,
                    embed_slides=embed_slides, allow_web=allow_web, format_profile=format_profile, skill_path=skill_path)
    from .stage_cache import MaterialCache, source_fingerprint
    params = {'device': device, 'language': language, 'model_path': model_path}
    identity = source_fingerprint(audio)
    cache = MaterialCache(cache_root or root, lecture_id, identity, params)
    from .doctor import require_features
    needs_asr = not cache.has_complete('asr')
    require_features(['core', *(['asr'] if needs_asr else [])], native_asr=needs_asr)
    _progress(progress, 'ASR 转写', 0, '复用已完成阶段或运行 SenseVoice')
    asr = cache.asr(audio, transcribe_sensevoice, **params)
    prepared = prepare_lecture(root, lecture_id=lecture_id, course_title=course_title,
        transcript=asr.transcript_path, segments=asr.segments_path, raw_asr=asr.raw_path, audio=audio,
        slides_dir=slides_dir, slides_meta=slides_meta, slides_zip=slides_zip, mode=mode, modes=modes,
        use_slides_as_source=use_slides_as_source, embed_slides=embed_slides, allow_web=allow_web,
        format_profile=format_profile, skill_path=skill_path, source_type=source_type,
        input_fingerprint=input_fingerprint or identity,
        output_organization=output_organization)
    _progress(progress, '生成工作区', 100, '完成；成功 ASR 缓存已保留')
    return _persisted_result(prepared, asr, None)


@organized_operation
def build_from_asset_zip(
    workspace_root: str | Path,
    *,
    lecture_id: str,
    course_title: str,
    asset_zip: str | Path,
    mode: str = "deep",
    modes: tuple[str, ...] | list[str] | None = None,
    use_slides_as_source: bool | None = None,
    embed_slides: bool = False,
    allow_web: bool = False,
    format_profile: str = "clean",
    skill_path: str | Path | None = None,
    device: str = "auto",
    language: str = "auto",
    model_path: str | None = None,
    progress: ProgressCallback | None = None,
    output_organization: dict | None = None,
    cache_root: Path | None = None,
    allowed_media_hosts: list[str] | None = None,
    collector_source_sha256: str | None = None,
    collector_binding: str | None = None,
) -> PrepareResult | BuildResult:
    """External collection -> complete standard-package check -> local preparation."""
    from .external_collector import convert_browser_export, inspect_browser_export
    details = inspect_browser_export(asset_zip)
    with tempfile.TemporaryDirectory(prefix="handouter-collection-") as temporary:
        standard = Path(temporary) / "standard.zip"
        checked = convert_browser_export(asset_zip, standard, allowed_media_hosts=allowed_media_hosts or [],
            expected_source_sha256=collector_source_sha256 or details["source_sha256"],
            expected_collector_binding=collector_binding or details["collector_binding"], progress=progress)
        return build_from_local_asset_zip(workspace_root, lecture_id=lecture_id,
            course_title=course_title, asset_zip=standard, mode=mode, modes=modes,
            use_slides_as_source=use_slides_as_source, embed_slides=embed_slides,
            allow_web=allow_web, format_profile=format_profile, skill_path=skill_path,
            device=device, language=language, model_path=model_path, progress=progress,
            output_organization=output_organization, cache_root=cache_root, checked_asset=checked,
            input_fingerprint=details.get("source_fingerprint") or details["source_sha256"])


def build_from_local_asset_zip(
    workspace_root: str | Path,
    *,
    lecture_id: str,
    course_title: str,
    asset_zip: str | Path,
    mode: str = "deep",
    modes: tuple[str, ...] | list[str] | None = None,
    use_slides_as_source: bool | None = None,
    embed_slides: bool = False,
    allow_web: bool = False,
    format_profile: str = "clean",
    skill_path: str | Path | None = None,
    device: str = "auto",
    language: str = "auto",
    model_path: str | None = None,
    progress: ProgressCallback | None = None,
    organized: bool = False,
    lecture_date: str | None = None,
    cache_root: Path | None = None,
    output_organization: dict | None = None,
    course_aliases: tuple[str, ...] = (),
    folder_name_mode: str = 'always',
    checked_asset=None,
    input_fingerprint: str | None = None,
) -> PrepareResult | BuildResult:
    """Site-neutral local asset ZIP -> existing preparation/ASR pipelines."""
    from .stage_cache import asset_fingerprint
    identity = input_fingerprint or asset_fingerprint(asset_zip)
    with tempfile.TemporaryDirectory(prefix="handouter-local-assets-") as temporary:
        if checked_asset is None:
            package = import_local_asset_zip(asset_zip, Path(temporary) / "input")
        else:
            from handouter_local.assets import extract
            from .importers import LocalAssetPackage
            if checked_asset.source != Path(asset_zip).resolve():
                raise ValueError("预检标准包与所选资源不匹配")
            paths = extract(checked_asset, Path(temporary) / "input")
            package = LocalAssetPackage(Path(temporary) / "input", checked_asset.course_title, None,
                checked_asset.lecture_date, paths.get("transcript"), checked_asset.roles.get("transcript_origin"),
                paths.get("segments_path"), paths.get("raw_asr_path"), paths.get("audio"), paths.get("media"),
                paths.get("images_dir"), paths.get("metadata_path"))
        slides = package.slides_dir
        common = dict(lecture_id=lecture_id, course_title=course_title,
            slides_dir=slides, slides_meta=package.slides_meta,
            mode=mode, modes=modes, use_slides_as_source=use_slides_as_source,
            embed_slides=embed_slides, allow_web=allow_web,
            format_profile=format_profile, skill_path=skill_path,
            organized=organized, lecture_date=lecture_date,
            course_aliases=course_aliases, folder_name_mode=folder_name_mode)
        if output_organization is not None:
            common["output_organization"] = output_organization
        if package.transcript is not None:
            _progress(progress, "读取本地资产", 100, "使用包内转写；不重复运行 ASR")
            return prepare_lecture(workspace_root, transcript=package.transcript,
                transcript_origin=package.transcript_origin, segments=package.segments,
                raw_asr=package.raw_asr, audio=package.audio,
                source_type="local_asset_package", input_fingerprint=identity, **common)
        if package.audio is not None:
            _progress(progress, "读取本地资产", 100, "使用包内音频并运行本地 ASR")
            return build_from_audio(workspace_root, audio=package.audio, device=device,
                language=language, model_path=model_path, progress=progress,
                cache_root=cache_root, source_type="local_asset_package",
                input_fingerprint=identity, **common)
        _progress(progress, "读取本地资产", 100, "从包内媒体提取音频并运行本地 ASR")
        return build_from_media(workspace_root, media_source=package.media, device=device,
            language=language, model_path=model_path, progress=progress,
            cache_root=cache_root, source_type="local_asset_package",
            input_fingerprint=identity, **common)


@organized_operation
@locked_output_root
def build_from_media(
    workspace_root: str | Path,
    *,
    lecture_id: str,
    course_title: str,
    media_source: str | Path,
    slides_dir: str | Path | None = None,
    slides_meta: str | Path | None = None,
    slides_zip: str | Path | None = None,
    mode: str = "deep",
    modes: tuple[str, ...] | list[str] | None = None,
    use_slides_as_source: bool | None = None,
    embed_slides: bool = False,
    allow_web: bool = False,
    format_profile: str = "clean",
    skill_path: str | Path | None = None,
    device: str = "auto",
    language: str = "auto",
    model_path: str | None = None,
    source_type: str = "media_asr",
    progress: ProgressCallback | None = None,
    output_organization: dict | None = None,
    cache_root: Path | None = None,
    input_fingerprint: str | None = None,
) -> BuildResult:
    """Extract audio from local/authorized media, transcribe it, then prepare handoff."""
    lecture_id = validate_lecture_id(lecture_id)
    root = Path(workspace_root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / lecture_id
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"讲次工作区已存在，拒绝覆盖: {target}")
    preflight_build(root, lecture_id=lecture_id, course_title=course_title, media=True,
                    progress=progress, slides_dir=slides_dir, slides_meta=slides_meta, slides_zip=slides_zip,
                    mode=mode, modes=modes, use_slides_as_source=use_slides_as_source,
                    embed_slides=embed_slides, allow_web=allow_web, format_profile=format_profile, skill_path=skill_path)
    from .stage_cache import MaterialCache, source_fingerprint, asset_fingerprint
    params = {'device': device, 'language': language, 'model_path': model_path}
    identity = source_fingerprint(media_source, asset=slides_zip)
    cache = MaterialCache(cache_root or root, lecture_id, identity, params)
    from .doctor import require_features
    needs_audio, needs_asr = not cache.has_complete('audio'), not cache.has_complete('asr')
    require_features(['core', *(['media'] if needs_audio else []), *(['asr'] if needs_asr else [])], native_asr=needs_asr)
    _progress(progress, '提取音频', 0, '复用已完成音频或调用 ffmpeg')
    media = cache.audio(media_source, extract_audio)
    _progress(progress, '提取音频', 100, '音频完成并保存校验记录')
    _progress(progress, 'ASR 转写', 0, '复用已完成 ASR 或运行 SenseVoice')
    asr = cache.asr(media.output, transcribe_sensevoice, **params)
    _progress(progress, 'ASR 转写', 100, f'完成（{asr.segment_count} 个片段）')
    prepared = prepare_lecture(root, lecture_id=lecture_id, course_title=course_title,
        transcript=asr.transcript_path, segments=asr.segments_path, raw_asr=asr.raw_path, audio=media.output,
        slides_dir=slides_dir, slides_meta=slides_meta, slides_zip=slides_zip, mode=mode, modes=modes,
        use_slides_as_source=use_slides_as_source, embed_slides=embed_slides, allow_web=allow_web,
        format_profile=format_profile, skill_path=skill_path, source_type=source_type,
        input_fingerprint=input_fingerprint or (asset_fingerprint(slides_zip) if slides_zip else identity),
        output_organization=output_organization)
    _progress(progress, '生成工作区', 100, '完成；成功音频与 ASR 缓存已保留')
    return _persisted_result(prepared, asr, media)


def _persisted_result(prepared: PrepareResult, asr: ASRResult, media: AudioExtractResult | None) -> BuildResult:
    root = prepared.workspace
    transcript = root / 'transcript'
    persisted_asr = ASRResult(transcript, transcript / 'raw.json', transcript / 'segments.json',
                             transcript / 'transcript.txt', asr.segment_count, asr.timestamp_kind,
                             asr.device, asr.elapsed_seconds)
    persisted_media = None if media is None else AudioExtractResult(root / 'audio/source.m4a',
        media.duration_ms, (root / 'audio/source.m4a').stat().st_size, media.mode)
    return BuildResult(prepared, persisted_asr, persisted_media)
