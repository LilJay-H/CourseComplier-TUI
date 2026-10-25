"""Bounded, metadata-aware importers for local Handouter asset packages."""

from __future__ import annotations

import json
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath

from .platform_support import is_portable_path_component

MAX_ZIP_MEMBERS = 10_000
MAX_ZIP_BYTES = 512 * 1024 * 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_METADATA_BYTES = 8 * 1024 * 1024
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
LOCAL_AUDIO_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
LOCAL_MEDIA_SUFFIXES = {".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".mts", ".ts", ".webm"}
LOCAL_TRANSCRIPT_SUFFIXES = {".txt"}
LOCAL_ASSET_MANIFEST = "handouter-input.json"


@dataclass(frozen=True)
class LocalAssetPackage:
    root: Path
    course_title: str | None
    lecture_id: str | None
    lecture_date: str | None
    transcript: Path | None
    transcript_origin: str | None
    segments: Path | None
    raw_asr: Path | None
    audio: Path | None
    media: Path | None
    slides_dir: Path | None
    slides_meta: Path | None


def _optional_object(value, name):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须是对象或 null")
    return value


def _optional_text(value, name, *, identity=False):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串或 null")
    text = value.strip()
    if identity and not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", text):
        raise ValueError(f"{name} 只能包含字母、数字、下划线和连字符")
    return text


def _manifest_path(value, name):
    text = _optional_text(value, name)
    if text is None:
        return None
    path = _safe_zip_name(text)
    if path.as_posix() == LOCAL_ASSET_MANIFEST:
        raise ValueError(f"{name} 不能指向资产清单本身")
    return path.as_posix()


def _parse_local_asset_manifest(value: object) -> dict:
    if not isinstance(value, dict) or set(value) - {"schema_version", "lecture", "audio", "media", "transcript", "slides"}:
        raise ValueError("handouter-input.json 包含未知字段或不是对象")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("handouter-input.json schema_version 必须为 1")
    lecture = _optional_object(value.get("lecture"), "lecture") or {}
    if set(lecture) - {"id", "course_title", "lecture_date"}:
        raise ValueError("lecture 包含未知字段")
    day = _optional_text(lecture.get("lecture_date"), "lecture.lecture_date")
    if day is not None:
        from .organization import lecture_date
        day = lecture_date(day)
    audio = _optional_object(value.get("audio"), "audio")
    media = _optional_object(value.get("media"), "media")
    transcript = _optional_object(value.get("transcript"), "transcript")
    slides = _optional_object(value.get("slides"), "slides")
    if audio is not None and set(audio) != {"path"}:
        raise ValueError("audio 必须且只能包含 path")
    if media is not None and set(media) != {"path"}:
        raise ValueError("media 必须且只能包含 path")
    if transcript is not None and (set(transcript) - {"path", "origin", "segments_path", "raw_asr_path"} or "path" not in transcript):
        raise ValueError("transcript 字段无效")
    if slides is not None and (set(slides) - {"images_dir", "metadata_path"} or "images_dir" not in slides):
        raise ValueError("slides 字段无效")
    if transcript is None and audio is None and media is None:
        raise ValueError("本地资产包至少需要 transcript、audio 或 media 之一")
    if media is not None and (audio is not None or transcript is not None):
        raise ValueError("media 仅用于需要抽取音频的单一媒体输入；已有 transcript/audio 时不要同时提供")
    origin = transcript.get("origin", "provided_transcript") if transcript else None
    if origin not in {None, "provided_transcript", "asr_text"}:
        raise ValueError("transcript.origin 只支持 provided_transcript 或 asr_text")
    parsed = {
        "course_title": _optional_text(lecture.get("course_title"), "lecture.course_title"),
        "lecture_id": _optional_text(lecture.get("id"), "lecture.id", identity=True),
        "lecture_date": day,
        "audio": _manifest_path(audio.get("path"), "audio.path") if audio else None,
        "media": _manifest_path(media.get("path"), "media.path") if media else None,
        "transcript": _manifest_path(transcript.get("path"), "transcript.path") if transcript else None,
        "transcript_origin": origin,
        "segments": _manifest_path(transcript.get("segments_path"), "transcript.segments_path") if transcript else None,
        "raw_asr": _manifest_path(transcript.get("raw_asr_path"), "transcript.raw_asr_path") if transcript else None,
        "slides_dir": _manifest_path(slides.get("images_dir"), "slides.images_dir") if slides else None,
        "slides_meta": _manifest_path(slides.get("metadata_path"), "slides.metadata_path") if slides else None,
    }
    if parsed["audio"] and PurePosixPath(parsed["audio"]).suffix.lower() not in LOCAL_AUDIO_SUFFIXES:
        raise ValueError("audio.path 不是支持的音频扩展名")
    if parsed["media"] and PurePosixPath(parsed["media"]).suffix.lower() not in LOCAL_MEDIA_SUFFIXES | LOCAL_AUDIO_SUFFIXES:
        raise ValueError("media.path 不是支持的音频或视频扩展名")
    if parsed["transcript"] and PurePosixPath(parsed["transcript"]).suffix.lower() not in LOCAL_TRANSCRIPT_SUFFIXES:
        raise ValueError("transcript.path 必须是 UTF-8 .txt")
    if parsed["segments"] and PurePosixPath(parsed["segments"]).suffix.lower() != ".json":
        raise ValueError("transcript.segments_path 必须是 JSON")
    if parsed["raw_asr"] and PurePosixPath(parsed["raw_asr"]).suffix.lower() != ".json":
        raise ValueError("transcript.raw_asr_path 必须是 JSON")
    if (parsed["segments"] or parsed["raw_asr"]) and not parsed["transcript"]:
        raise ValueError("segments/raw_asr 必须与 transcript.path 一起提供")
    if parsed["slides_meta"] and not parsed["slides_dir"]:
        raise ValueError("slides.metadata_path 必须与 images_dir 一起提供")
    role_paths = [parsed[key] for key in ("audio", "media", "transcript", "segments", "raw_asr", "slides_dir", "slides_meta")
                  if parsed[key] is not None]
    if len(role_paths) != len(set(role_paths)):
        raise ValueError("同一文件不能同时承担多个本地资产角色")
    return parsed


def _local_manifest_from_archive(archive: zipfile.ZipFile) -> tuple[dict, dict[str, zipfile.ZipInfo]]:
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_MEMBERS or sum(info.file_size for info in infos) > MAX_ZIP_BYTES:
        raise ValueError("本地资产 ZIP 文件数量或总解压大小超过限制")
    files: dict[str, zipfile.ZipInfo] = {}
    seen: set[str] = set()
    for info in infos:
        name = _safe_zip_name(info.orig_filename)
        key = unicodedata.normalize("NFC", name.as_posix()).casefold()
        if key in seen:
            raise ValueError("本地资产 ZIP 存在重名或大小写/Unicode 歧义成员")
        seen.add(key)
        if stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
            raise ValueError("不接受 ZIP 符号链接或加密成员")
        if not info.is_dir() and "__MACOSX" not in name.parts:
            files[name.as_posix()] = info
    info = files.get(LOCAL_ASSET_MANIFEST)
    if info is None or info.file_size > MAX_METADATA_BYTES:
        raise ValueError(f"本地资产 ZIP 缺少根目录 {LOCAL_ASSET_MANIFEST} 或清单过大")
    try:
        manifest = json.loads(archive.read(info).decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("handouter-input.json 不是有效 UTF-8 JSON") from exc
    return _parse_local_asset_manifest(manifest), files


def inspect_local_asset_zip(zip_path: str | Path) -> dict:
    candidate = Path(zip_path).expanduser()
    if candidate.is_symlink():
        raise FileNotFoundError(f"本地资产 ZIP 不存在或不是普通文件: {candidate}")
    source = candidate.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"本地资产 ZIP 不存在或不是普通文件: {source}")
    with zipfile.ZipFile(source) as archive:
        manifest, _ = _local_manifest_from_archive(archive)
    return {**{key: manifest[key] for key in ("course_title", "lecture_id", "lecture_date")},
            "has_slides": manifest["slides_dir"] is not None}


def import_local_asset_zip(zip_path: str | Path, output_dir: str | Path) -> LocalAssetPackage:
    """Extract only manifest-declared local lecture assets into a new directory."""
    candidate = Path(zip_path).expanduser()
    destination = Path(output_dir)
    if candidate.is_symlink():
        raise FileNotFoundError(f"本地资产 ZIP 不存在或不是普通文件: {candidate}")
    source = candidate.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"本地资产 ZIP 不存在或不是普通文件: {source}")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("本地资产导入目录已存在；拒绝覆盖")
    with zipfile.ZipFile(source) as archive:
        manifest, files = _local_manifest_from_archive(archive)
        declared = {path for key, path in manifest.items() if key in {"audio", "media", "transcript", "segments", "raw_asr", "slides_meta"} and path}
        slide_names: list[str] = []
        if manifest["slides_dir"]:
            prefix = manifest["slides_dir"].rstrip("/") + "/"
            slide_names = [name for name in files if name.startswith(prefix)
                           and "/" not in name[len(prefix):]
                           and PurePosixPath(name).suffix.lower() in IMAGE_SUFFIXES]
            if not slide_names:
                raise ValueError("slides.images_dir 中没有支持的 PNG/JPEG/WebP 图片")
            declared.update(slide_names)
        missing = declared - set(files)
        if missing:
            raise ValueError("本地资产 ZIP 缺少清单引用文件: " + ", ".join(sorted(missing)))
        destination.mkdir(parents=True, exist_ok=False)
        try:
            for name in sorted(declared):
                info = files[name]
                if info.file_size > MAX_ZIP_BYTES:
                    raise ValueError("本地资产单文件超过大小限制")
                data = archive.read(info)
                if len(data) != info.file_size:
                    raise ValueError("本地资产实际解压大小异常")
                suffix = PurePosixPath(name).suffix.lower()
                if name in slide_names:
                    if len(data) > MAX_IMAGE_BYTES:
                        raise ValueError("单张 PPT 图片超过大小限制")
                    _image_extension(data[:12])
                elif name == manifest["transcript"]:
                    data.decode("utf-8-sig")
                elif name in {manifest["segments"], manifest["raw_asr"], manifest["slides_meta"]}:
                    if len(data) > MAX_METADATA_BYTES:
                        raise ValueError("本地资产 JSON 元数据超过大小限制")
                    json.loads(data.decode("utf-8-sig"))
                target = destination / PurePosixPath(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as handle:
                    handle.write(data)
        except BaseException:
            import shutil
            shutil.rmtree(destination, ignore_errors=True)
            raise
    def local(name):
        return destination / PurePosixPath(name) if name else None
    return LocalAssetPackage(destination, manifest["course_title"], manifest["lecture_id"],
        manifest["lecture_date"], local(manifest["transcript"]), manifest["transcript_origin"],
        local(manifest["segments"]), local(manifest["raw_asr"]), local(manifest["audio"]),
        local(manifest["media"]), local(manifest["slides_dir"]), local(manifest["slides_meta"]))


def _seconds_to_ms(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("seconds 必须是有限的非负数，单位为秒")
    try:
        seconds = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("seconds 字段不是有效数字") from exc
    if not seconds.is_finite() or not 0 <= seconds <= Decimal(2**53 - 1) / 1000:
        raise ValueError("seconds 超出有效范围")
    milliseconds = seconds * 1000
    if milliseconds != milliseconds.to_integral_value():
        raise ValueError("时间精度小于毫秒，不能无提示地舍入")
    return int(milliseconds)


def _clock_to_ms(value: str) -> int:
    if not isinstance(value, str):
        raise ValueError("时间字符串必须是 HH:MM:SS 或 MM:SS")
    parts = value.strip().split(":")
    if len(parts) not in (2, 3) or not all(re.fullmatch(r"\d+", part) for part in parts[:-1]):
        raise ValueError("无效的时间字符串")
    if not re.fullmatch(r"\d{1,2}(?:\.\d{1,3})?", parts[-1]):
        raise ValueError("秒字段必须在 0 到 60 之间，最多三位小数")
    seconds = _seconds_to_ms(parts[-1])
    if seconds >= 60_000 or (len(parts) == 3 and int(parts[1]) >= 60):
        raise ValueError("时间字符串的分/秒字段越界")
    minutes = int(parts[0]) if len(parts) == 2 else int(parts[0]) * 60 + int(parts[1])
    total = minutes * 60_000 + seconds
    if total > 2**53 - 1:
        raise ValueError("时间超出有效范围")
    return total


def _metadata_time_ms(item: dict) -> int | None:
    values: list[int] = []
    if item.get("seconds") is not None and item["seconds"] != "":
        values.append(_seconds_to_ms(item["seconds"]))
    for key in ("timestamp", "time", "switchTime"):
        if item.get(key) is not None and item[key] != "":
            values.append(_clock_to_ms(item[key]))
    if values and len(set(values)) != 1:
        raise ValueError("同一条 PPT 元数据的时间字段互相矛盾")
    return values[0] if values else None


def _format_timestamp(milliseconds: int | None) -> str | None:
    if milliseconds is None:
        return None
    seconds, fraction = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    suffix = f".{fraction:03d}".rstrip("0") if fraction else ""
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{suffix}"


def _safe_zip_name(name: str) -> PurePosixPath:
    if not isinstance(name, str) or not name or re.search(r'[\x00-\x1f\\:*?"<>|]', name):
        raise ValueError("ZIP 中包含不安全或跨平台歧义路径")
    body = name[:-1] if name.endswith("/") else name
    parts = body.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("ZIP 路径不得是绝对路径或包含空段、点、上级目录")
    if any(not is_portable_path_component(part) for part in parts):
        raise ValueError("ZIP 路径包含 Windows/macOS/Linux 间不可移植的文件名")
    return PurePosixPath(body)


def _natural_key(name: str):
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold()) for part in re.split(r"(\d+)", name))


def _image_extension(header: bytes) -> str:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    raise ValueError("图片头不是支持的 PNG/JPEG/WebP；可能下载到了登录页或错误页")


def _safe_timing_diagnostics(item: dict) -> dict:
    status = item.get("timeStatus")
    field = item.get("sourceField")
    raw = item.get("rawTimeValue")
    if status is not None and not isinstance(status, str):
        status = None
    if field is not None and not isinstance(field, str):
        field = None
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float, type(None))):
        raw = None
    return {"time_status": status, "source_time_field": field, "raw_time_value": raw}


def import_slides_zip(zip_path: str | Path, output_dir: str | Path) -> list[dict]:
    """Import only slide images and safe timing metadata into a NEW directory.

    ``private/`` and ``course_info.json`` are intentionally not extracted. The
    source ZIP remains private input and may contain signed media URLs.
    """
    source_zip = Path(zip_path)
    destination = Path(output_dir)
    if not source_zip.is_file() or source_zip.is_symlink():
        raise FileNotFoundError(f"ZIP 不存在或不是普通文件: {source_zip}")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("PPT 导入目录已存在；拒绝覆盖")

    with zipfile.ZipFile(source_zip, "r") as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_MEMBERS or sum(info.file_size for info in infos) > MAX_ZIP_BYTES:
            raise ValueError("ZIP 文件数量或总解压大小超过限制")
        files: dict[str, zipfile.ZipInfo] = {}
        seen: set[str] = set()
        for info in infos:
            name = _safe_zip_name(info.orig_filename)
            key = unicodedata.normalize("NFC", name.as_posix()).casefold()
            if key in seen:
                raise ValueError("ZIP 存在重名或大小写/Unicode 歧义成员")
            seen.add(key)
            if stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
                raise ValueError("不接受 ZIP 符号链接或加密成员")
            if not info.is_dir() and "__MACOSX" not in name.parts:
                files[name.as_posix()] = info

        images = {name: info for name, info in files.items() if PurePosixPath(name).suffix.lower() in IMAGE_SUFFIXES}
        if not images:
            raise ValueError("ZIP 中没有可导入的 PPT 图片")
        metadata_names = [name for name in files if PurePosixPath(name).name == "slides_meta.json"]
        if len(metadata_names) > 1:
            raise ValueError("ZIP 内有多份 slides_meta.json，不能猜测采用哪份")

        events: list[tuple[str | None, int | None, str, int | None, str, dict]] = []
        referenced: set[str] = set()
        if metadata_names:
            metadata_name = metadata_names[0]
            info = files[metadata_name]
            if info.file_size > MAX_METADATA_BYTES:
                raise ValueError("PPT 元数据超过大小限制")
            with archive.open(info) as handle:
                raw = handle.read(MAX_METADATA_BYTES + 1)
            if len(raw) > MAX_METADATA_BYTES:
                raise ValueError("PPT 元数据超过大小限制")
            try:
                records = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeError, ValueError) as exc:
                raise ValueError("slides_meta.json 不是有效 UTF-8 JSON") from exc
            if not isinstance(records, list) or len(records) > MAX_ZIP_MEMBERS:
                raise ValueError("PPT 元数据必须是数量受限的列表")
            base = PurePosixPath(metadata_name).parent
            for item in records:
                if not isinstance(item, dict):
                    raise ValueError("每条 PPT 元数据必须是对象")
                filename = _safe_zip_name(item.get("filename"))
                if filename.suffix.lower() not in IMAGE_SUFFIXES:
                    raise ValueError("元数据 filename 不是支持的图片类型")
                candidates = {str(base / filename), str(base / "slides" / filename)}
                matches = candidates.intersection(images)
                if len(matches) > 1:
                    raise ValueError("同一元数据图片对应多个文件，不能猜测")
                image = next(iter(matches), None)
                if image is not None:
                    referenced.add(image)
                source_index = item.get("index")
                if source_index is not None and (type(source_index) is not int or source_index < 1):
                    raise ValueError("PPT 原始 index 必须是正整数或 null")
                events.append((
                    image, _metadata_time_ms(item), "metadata", source_index,
                    filename.as_posix(), _safe_timing_diagnostics(item),
                ))

        for name in sorted(set(images) - referenced, key=_natural_key):
            milliseconds = None
            kind = "unreferenced_image" if metadata_names else "filename"
            if not metadata_names:
                match = re.search(r"__(\d+-\d{2}-\d{2}(?:\.\d{1,3})?)$", PurePosixPath(name).stem)
                if match:
                    milliseconds = _clock_to_ms(match.group(1).replace("-", ":"))
            events.append((name, milliseconds, kind, None, name, {}))

        targets: dict[str, str] = {}
        for image, *_ in events:
            if image is None or image in targets:
                continue
            if images[image].file_size > MAX_IMAGE_BYTES:
                raise ValueError("单张 PPT 图片超过大小限制")
            with archive.open(images[image]) as handle:
                extension = _image_extension(handle.read(12))
            targets[image] = f"slide_{len(targets) + 1:03d}{extension}"

        mapping: list[dict] = []
        for index, (image, milliseconds, origin, source_index, source_filename, diagnostics) in enumerate(events, 1):
            filename = targets.get(image)
            row = {
                "index": index,
                "source_index": source_index,
                "source_filename": source_filename,
                "occurrence_id": f"occ-{index:04d}",
                "filename": filename,
                "path": str(destination / filename) if filename else None,
                "timestamp": _format_timestamp(milliseconds),
                "seconds": milliseconds / 1000 if milliseconds is not None else None,
                "start_ms": milliseconds,
                "end_ms": None,
                "timestamp_kind": origin if milliseconds is not None else "unknown",
                "origin": origin,
                "status": "ok" if filename else "missing_image",
            }
            row.update(diagnostics)
            mapping.append(row)

        destination.mkdir(parents=True, exist_ok=False)
        created: list[Path] = []
        try:
            for image, filename in targets.items():
                with archive.open(images[image]) as handle:
                    data = handle.read(MAX_IMAGE_BYTES + 1)
                if len(data) > MAX_IMAGE_BYTES or len(data) != images[image].file_size:
                    raise ValueError("图片实际解压大小异常")
                target = destination / filename
                with target.open("xb") as output:
                    created.append(target)
                    output.write(data)
            metadata_path = destination / "slides_meta.json"
            with metadata_path.open("x", encoding="utf-8") as output:
                created.append(metadata_path)
                json.dump(mapping, output, ensure_ascii=False, indent=2, allow_nan=False)
        except BaseException:
            for path in reversed(created):
                try:
                    path.unlink()
                except OSError:
                    pass
            try:
                destination.rmdir()
            except OSError:
                pass
            raise
    return mapping
