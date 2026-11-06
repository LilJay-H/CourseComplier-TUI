"""Site-neutral, bounded local input inspection and extraction.

The standard package is data only. No URL, code, update or model instruction in a
package can influence the runtime. Extraction always creates a new directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import unicodedata
import zipfile

from . import local_budget_env

from .contract import LocalError

MAX_MEMBERS = 10_000
MAX_UNCOMPRESSED = 8 * 1024**3
MAX_SINGLE_MEDIA = 7 * 1024**3
MAX_IMAGE = 32 * 1024**2
MAX_METADATA = 16 * 1024**2
MAX_RATIO = 2000
CHUNK = 1024 * 1024
IMAGES = {".png", ".jpg", ".jpeg", ".webp"}
AUDIO = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
MEDIA = {".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".mts", ".ts", ".webm"}
_IDENTITY = re.compile(r"[A-Za-z0-9_-]{1,120}\Z")
_BAD_NAME = re.compile(r'[\x00-\x1f\\:*?"<>|]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_JUNK = {".ds_store", "thumbs.db", "desktop.ini"}


def _junk(name: str) -> bool:
    """Files archivers add on their own (macOS Finder, Windows Explorer)."""
    parts = name.split("/")
    return "__MACOSX" in parts or parts[-1].casefold() in _JUNK


@dataclass(frozen=True)
class Asset:
    source: Path
    source_kind: str
    schema_version: int | None
    source_sha256: str
    source_bytes: int
    expanded_bytes: int
    course_title: str | None
    lecture_date: str | None
    has_transcript: bool
    needs_asr: bool
    slide_images: int
    roles: dict[str, str]
    slide_files: tuple[str, ...]
    # Digest of the audio/media bytes only. Packaging metadata (ZIP timestamps,
    # manifest order) does not change it, so it is the speech cache identity.
    speech_sha256: str | None = None
    lecture_id: str | None = None

    def public(self) -> dict:
        return {"local_protocol_version": 1, "source_sha256": self.source_sha256,
                "source_kind": self.source_kind, "schema_version": self.schema_version,
                "course_title": self.course_title, "lecture_date": self.lecture_date,
                "has_transcript": self.has_transcript, "needs_asr": self.needs_asr,
                "slide_images": self.slide_images, "source_bytes": self.source_bytes,
                "expanded_bytes": self.expanded_bytes, "ready": True}


def _local_budget() -> int:
    """Per-device policy; cloud request limits remain separate and unchanged."""
    value = local_budget_env()
    if value is None:
        return MAX_UNCOMPRESSED
    if not value.isascii() or not value.isdecimal():
        raise LocalError("INVALID_LOCAL_POLICY", "本地大小上限设置无效")
    result = int(value)
    if not 1024**3 <= result <= 64 * 1024**3:
        raise LocalError("INVALID_LOCAL_POLICY", "本地大小上限必须在 1–64 GiB 之间")
    return result


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > _local_budget():
            raise LocalError("INVALID_ASSET", "输入不是普通文件，或超过大小上限")
        for part in iter(lambda: stream.read(CHUNK), b""):
            digest.update(part)
        after = os.fstat(stream.fileno())
        if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
            raise LocalError("SOURCE_CHANGED", "预检期间输入文件发生了变化")
    return digest.hexdigest()


def _source(path: str | Path) -> Path:
    raw = Path(path).expanduser()
    if raw.is_symlink() or not raw.is_file():
        raise LocalError("INVALID_ASSET", "请选择本地的普通文件")
    resolved = raw.resolve(strict=True)
    if not resolved.is_file():
        raise LocalError("INVALID_ASSET", "所选输入不是文件")
    return resolved


def _safe_name(name: str) -> str:
    if not isinstance(name, str) or not name or _BAD_NAME.search(name):
        raise LocalError("INVALID_ASSET", "资料包内有不安全的文件名")
    body = name[:-1] if name.endswith("/") else name
    parts = body.split("/")
    if any(not part or part in {".", ".."} or part.endswith((" ", ".")) or part.split(".", 1)[0].casefold() in _RESERVED for part in parts):
        raise LocalError("INVALID_ASSET", "资料包内有不安全的路径")
    if unicodedata.normalize("NFC", body) != body:
        raise LocalError("INVALID_ASSET", "资料包内的路径必须是 NFC Unicode")
    return PurePosixPath(body).as_posix()


def _role_path(value: object, role: str, suffixes: set[str] | None = None) -> str:
    if not isinstance(value, str):
        raise LocalError("INVALID_ASSET", f"缺少 {role} 路径")
    path = _safe_name(value)
    if suffixes is not None and PurePosixPath(path).suffix.lower() not in suffixes:
        raise LocalError("INVALID_ASSET", f"{role} 的文件类型不受支持")
    if path == "handouter-input.json":
        raise LocalError("INVALID_ASSET", "清单文件本身不能作为资料")
    return path


def _date(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise LocalError("INVALID_ASSET", "授课日期必须是 YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise LocalError("INVALID_ASSET", "授课日期必须是 YYYY-MM-DD") from exc
    return value


def _manifest(data: object) -> tuple[int, str | None, str | None, dict[str, str], str | None]:
    if not isinstance(data, dict) or set(data) - {"schema_version", "lecture", "audio", "media", "transcript", "slides"}:
        raise LocalError("INVALID_ASSET", "handouter-input.json 不是对象，或包含未知字段")
    version = data.get("schema_version")
    if type(version) is not int or version not in (1, 2):
        raise LocalError("UNSUPPORTED_ASSET_VERSION", "资料包版本较新，请更新 Course Compiler")
    lecture = data.get("lecture") or {}
    if not isinstance(lecture, dict) or set(lecture) - {"id", "course_title", "lecture_date"}:
        raise LocalError("INVALID_ASSET", "lecture 字段无效")
    identity = lecture.get("id")
    if identity is not None and (not isinstance(identity, str) or not _IDENTITY.fullmatch(identity)):
        raise LocalError("INVALID_ASSET", "讲次 ID 只能包含字母、数字、下划线和连字符")
    title = lecture.get("course_title")
    if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 200):
        raise LocalError("INVALID_ASSET", "课程名无效（不能为空，最多 200 字）")
    roles: dict[str, str] = {}
    for group in ("audio", "media", "transcript", "slides"):
        item = data.get(group)
        if item is None:
            continue
        if not isinstance(item, dict):
            raise LocalError("INVALID_ASSET", f"{group} 声明无效")
        allowed = ({"path"} if group in {"audio", "media"} else
                   {"path", "origin", "segments_path", "raw_asr_path"} if group == "transcript" else
                   {"images_dir", "metadata_path"})
        if set(item) - allowed or ("path" not in item and group != "slides") or (group == "slides" and "images_dir" not in item):
            raise LocalError("INVALID_ASSET", f"{group} 字段无效")
        if group in {"audio", "media"}:
            roles[group] = _role_path(item["path"], group, AUDIO if group == "audio" else AUDIO | MEDIA)
        elif group == "transcript":
            if item.get("origin", "provided_transcript") not in {"provided_transcript", "asr_text"}:
                raise LocalError("INVALID_ASSET", "transcript.origin 只支持 provided_transcript 或 asr_text")
            roles["transcript"] = _role_path(item["path"], "transcript", {".txt"})
            for key in ("segments_path", "raw_asr_path"):
                if key in item:
                    roles[key] = _role_path(item[key], key, {".json"})
            roles["transcript_origin"] = item.get("origin", "provided_transcript")
        else:
            roles["images_dir"] = _role_path(item["images_dir"], "images_dir")
            if "metadata_path" in item:
                roles["metadata_path"] = _role_path(item["metadata_path"], "metadata_path", {".json"})
    if not any(k in roles for k in ("audio", "media", "transcript")):
        raise LocalError("INVALID_ASSET", "资料包至少需要 audio、media 或 transcript 之一")
    if version == 1 and "media" in roles and ("audio" in roles or "transcript" in roles):
        raise LocalError("INVALID_ASSET", "schema 1 的 media 不能和 audio、transcript 同时提供")
    if "audio" in roles and "media" in roles:
        raise LocalError("INVALID_ASSET", "audio 和 media 只能二选一")
    paths = [v for k, v in roles.items() if k != "transcript_origin"]
    if len(paths) != len(set(paths)):
        raise LocalError("INVALID_ASSET", "同一个文件不能同时承担多个资料角色")
    return version, title.strip() if title else None, _date(lecture.get("lecture_date")), roles, identity


def _entries(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS:
        raise LocalError("INVALID_ASSET", "资料包文件数过多")
    entries: dict[str, zipfile.ZipInfo] = {}
    seen: set[str] = set()
    total = 0
    for info in infos:
        name = _safe_name(info.orig_filename)
        folded = unicodedata.normalize("NFC", name).casefold()
        if folded in seen:
            raise LocalError("INVALID_ASSET", "资料包里有重名，或只差大小写、Unicode 写法的文件")
        seen.add(folded)
        mode = info.external_attr >> 16
        if info.flag_bits & 1 or stat.S_ISLNK(mode) or (mode and stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
            raise LocalError("INVALID_ASSET", "资料包不能包含链接、特殊文件或加密成员")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise LocalError("INVALID_ASSET", "资料包使用了不支持的压缩方式")
        if info.is_dir() or _junk(name):
            continue
        if info.file_size < 0 or info.file_size > min(MAX_SINGLE_MEDIA, _local_budget()):
            raise LocalError("INVALID_ASSET", "资料包里的单个文件超过大小上限")
        if info.compress_size == 0 and info.file_size > 0 or (info.compress_size and info.file_size > info.compress_size * MAX_RATIO):
            raise LocalError("INVALID_ASSET", "资料包压缩比异常")
        total += info.file_size
        if total > _local_budget():
            raise LocalError("INVALID_ASSET", "资料包解压后超过本地大小上限")
        entries[name] = info
    return entries


def _read_small(archive: zipfile.ZipFile, entry: zipfile.ZipInfo, limit: int) -> bytes:
    if entry.file_size > limit:
        raise LocalError("INVALID_ASSET", "资料包元数据过大")
    with archive.open(entry) as stream:
        data = stream.read(limit + 1)
    if len(data) != entry.file_size:
        raise LocalError("INVALID_ASSET", "资料包成员的实际大小与记录不符")
    return data


def _media_header(suffix: str, header: bytes) -> bool:
    if suffix in {".mp4", ".m4v", ".m4a", ".mov", ".3gp"}:
        return len(header) >= 8 and header[4:8] == b"ftyp"
    if suffix in {".mkv", ".webm"}:
        return header.startswith(b"\x1aE\xdf\xa3")
    if suffix in {".wav", ".avi"}:
        return header.startswith(b"RIFF")
    if suffix in {".ogg", ".opus"}:
        return header.startswith(b"OggS")
    if suffix == ".flac":
        return header.startswith(b"fLaC")
    if suffix == ".mp3":
        return header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xff and header[1] & 0xe0 == 0xe0)
    if suffix == ".aac":
        return len(header) >= 2 and header[0] == 0xff and header[1] & 0xf0 == 0xf0
    if suffix in {".ts", ".mts", ".m2ts"}:
        return header.startswith(b"\x47") or header[4:5] == b"\x47"
    if suffix in {".mpeg", ".mpg"}:
        return header.startswith(b"\x00\x00\x01")
    return False


def _image_header(suffix: str, header: bytes) -> bool:
    if suffix == ".png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return header.startswith(b"\xff\xd8\xff")
    if suffix == ".webp":
        return header.startswith(b"RIFF") and header[8:12] == b"WEBP"
    return False


def parse_slide_events(value: object, image_names: set[str]) -> list[dict]:
    if not isinstance(value, list) or len(value) > MAX_MEMBERS:
        raise LocalError("INVALID_ASSET", "课件时间列表无效")
    allowed = {"filename", "source_index", "index", "start_ms", "end_ms", "seconds",
               "timestamp", "time", "switchTime"}
    normalized = []
    for item in value:
        if not isinstance(item, dict) or set(item) - allowed or item.get("filename") not in image_names:
            raise LocalError("INVALID_ASSET", "课件时间记录无效，或引用了不存在的图片")
        times = []
        for key in ("start_ms", "end_ms"):
            raw = item.get(key)
            if raw is not None and (type(raw) is not int or raw < 0):
                raise LocalError("INVALID_ASSET", "课件毫秒时间无效")
        if item.get("start_ms") is not None:
            times.append(item["start_ms"])
        if item.get("seconds") is not None:
            try:
                milliseconds = Decimal(str(item["seconds"])) * 1000
            except InvalidOperation as exc:
                raise LocalError("INVALID_ASSET", "课件秒数无效") from exc
            if not milliseconds.is_finite() or milliseconds < 0 or milliseconds != milliseconds.to_integral_value():
                raise LocalError("INVALID_ASSET", "课件秒数无效")
            times.append(int(milliseconds))
        for key in ("timestamp", "time", "switchTime"):
            raw = item.get(key)
            if raw in (None, ""):
                continue
            if not isinstance(raw, str) or not re.fullmatch(r"\d{1,3}:\d{2}:\d{2}(?:\.\d{1,3})?", raw):
                raise LocalError("INVALID_ASSET", "课件时间格式无效（应为 时:分:秒）")
            hours, minutes, seconds = raw.split(":")
            if int(minutes) >= 60 or Decimal(seconds) >= 60:
                raise LocalError("INVALID_ASSET", "课件时间格式无效（应为 时:分:秒）")
            times.append(int(Decimal(hours) * 3_600_000 + Decimal(minutes) * 60_000 + Decimal(seconds) * 1000))
        if len(set(times)) > 1:
            raise LocalError("INVALID_ASSET", "课件时间互相矛盾")
        start = times[0] if times else None
        end = item.get("end_ms")
        if start is not None and end is not None and end < start:
            raise LocalError("INVALID_ASSET", "课件结束时间早于开始时间")
        index = item.get("source_index", item.get("index"))
        if index is not None and (type(index) is not int or index < 1):
            raise LocalError("INVALID_ASSET", "课件序号无效")
        normalized.append({"filename": item["filename"], "source_index": index,
                           "start_ms": start, "end_ms": end})
    return normalized


def _declared(archive: zipfile.ZipFile) -> tuple:
    entries = _entries(archive)
    manifest = entries.get("handouter-input.json")
    if manifest is None:
        raise LocalError("INVALID_ASSET", "资料包根目录缺少 handouter-input.json")
    try:
        version, title, day, roles, identity = _manifest(
            json.loads(_read_small(archive, manifest, MAX_METADATA).decode("utf-8-sig")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LocalError("INVALID_ASSET", "handouter-input.json 不是有效的 UTF-8 JSON") from exc
    required = {value for key, value in roles.items() if key not in {"images_dir", "transcript_origin"}}
    slide_files: tuple[str, ...] = ()
    if "images_dir" in roles:
        prefix = roles["images_dir"].rstrip("/") + "/"
        slide_files = tuple(sorted(name for name in entries if name.startswith(prefix)
                                   and "/" not in name[len(prefix):] and PurePosixPath(name).suffix.lower() in IMAGES))
        if not slide_files:
            raise LocalError("INVALID_ASSET", "声明的课件目录里没有 PNG/JPEG/WebP 图片")
    if not required <= set(entries) or set(entries) - (required | set(slide_files) | {"handouter-input.json"}):
        raise LocalError("INVALID_ASSET", "资料包缺少清单引用的文件，或含有清单没有声明的文件")
    return entries, version, title, day, roles, identity, required, slide_files


def peek(path: str | Path) -> dict:
    """Manifest metadata of a standard package, checking structure but not reading media.

    Used where the UI identifies a dropped file; `inspect` does the full pass
    (headers, sizes, CRCs) right before the package is processed.
    """
    source = _source(path)
    if source.stat().st_size > _local_budget():
        raise LocalError("INVALID_ASSET", "输入超过本地大小上限")
    with zipfile.ZipFile(source) as archive:
        _entries_, _version, title, day, _roles, identity, _required, slides = _declared(archive)
    return {"course_title": title, "lecture_id": identity, "lecture_date": day, "has_slides": bool(slides)}


def inspect(path: str | Path) -> Asset:
    source = _source(path)
    size = source.stat().st_size
    suffix = source.suffix.lower()
    if size > _local_budget() or suffix != ".zip" and size > min(MAX_SINGLE_MEDIA, _local_budget()):
        raise LocalError("INVALID_ASSET", "输入超过本地大小上限")
    if suffix == ".txt" and size > MAX_METADATA:
        raise LocalError("INVALID_ASSET", "转写文件过大")
    if suffix not in AUDIO | MEDIA | {".zip", ".txt"}:
        raise LocalError("INVALID_ASSET", "不支持的输入类型")
    digest = _file_digest(source)
    if suffix != ".zip":
        if suffix == ".txt":
            source.read_text(encoding="utf-8-sig")
            kind = "transcript"
        elif suffix in AUDIO | MEDIA:
            with source.open("rb") as stream:
                header = stream.read(16)
            if not _media_header(suffix, header):
                raise LocalError("INVALID_ASSET", "音视频文件的内容与扩展名不符")
            kind = "audio" if suffix in AUDIO else "media"
        else:
            raise LocalError("INVALID_ASSET", "不支持的输入类型")
        return Asset(source, kind, None, digest, size, size, source.stem[:200], None,
                     kind == "transcript", kind != "transcript", 0,
                     {kind: source.name}, (), None if kind == "transcript" else digest)
    with zipfile.ZipFile(source) as archive:
        entries, version, title, day, roles, identity, required, slide_files = _declared(archive)
        speech_member = roles.get("audio") or roles.get("media")
        speech_digest = None
        for name, info in entries.items():
            suffix = PurePosixPath(name).suffix.lower()
            if name in slide_files:
                if info.file_size > MAX_IMAGE or not _image_header(suffix, _read_small(archive, info, 16)[:12] if info.file_size <= 16 else _head(archive, info)):
                    raise LocalError("INVALID_ASSET", "课件图片无效（内容不是 PNG/JPEG/WebP，或超过 32 MB）")
            elif name in {roles.get("audio"), roles.get("media")}:
                if not _media_header(suffix, _head(archive, info)):
                    raise LocalError("INVALID_ASSET", "音视频文件的内容与扩展名不符")
            elif name in {roles.get("transcript"), roles.get("segments_path"), roles.get("raw_asr_path"), roles.get("metadata_path")}:
                if info.file_size > MAX_METADATA:
                    raise LocalError("INVALID_ASSET", "转写或元数据文件过大")
                body = _read_small(archive, info, MAX_METADATA).decode("utf-8-sig")
                if suffix == ".json":
                    parsed = json.loads(body)
                    if name == roles.get("metadata_path"):
                        parse_slide_events(parsed, {PurePosixPath(item).name for item in slide_files})
                    elif name == roles.get("segments_path") and (not isinstance(parsed, list) or len(parsed) > MAX_MEMBERS):
                        raise LocalError("INVALID_ASSET", "转写分段列表无效")
            # Read every entry fully during inspect; CRC/actual size are checked.
            # The speech member's digest is taken in the same pass.
            member_digest = hashlib.sha256() if name == speech_member else None
            with archive.open(info) as stream:
                actual = 0
                while block := stream.read(CHUNK):
                    actual += len(block)
                    if actual > info.file_size:
                        raise LocalError("INVALID_ASSET", "资料包成员的实际大小与记录不符")
                    if member_digest is not None:
                        member_digest.update(block)
                if actual != info.file_size:
                    raise LocalError("INVALID_ASSET", "资料包成员的实际大小与记录不符")
            if member_digest is not None:
                speech_digest = member_digest.hexdigest()
    return Asset(source, "asset_zip", version, digest, size,
                 sum(info.file_size for info in entries.values()), title, day,
                 "transcript" in roles, "transcript" not in roles, len(slide_files), roles, slide_files,
                 speech_digest, identity)


def _head(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    with archive.open(info) as stream:
        return stream.read(16)


def extract(asset: Asset, target: Path) -> dict[str, Path]:
    """Copy selected material to an exclusive directory, rejecting source drift."""
    if target.exists() or target.is_symlink():
        raise LocalError("DESTINATION_EXISTS", "解压目标已存在，拒绝覆盖")
    if _file_digest(asset.source) != asset.source_sha256:
        raise LocalError("SOURCE_CHANGED", "所选文件在预检后发生了变化，请重新选择")
    target.mkdir(parents=True, exist_ok=False, mode=0o700)
    paths: dict[str, Path] = {}
    try:
        if asset.source_kind != "asset_zip":
            role = asset.source_kind
            output = target / role / asset.source.name
            output.parent.mkdir(parents=True)
            _copy_file(asset.source, output, asset.source_bytes)
            paths[role] = output
            return paths
        with zipfile.ZipFile(asset.source) as archive:
            entries = _entries(archive)
            names = {value for key, value in asset.roles.items() if key not in {"images_dir", "transcript_origin"}} | set(asset.slide_files)
            for name in sorted(names):
                info = entries[name]
                dest = target.joinpath(*PurePosixPath(name).parts)
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as stream, dest.open("xb") as output:
                    actual = 0
                    while block := stream.read(CHUNK):
                        actual += len(block)
                        if actual > info.file_size:
                            raise LocalError("INVALID_ASSET", "资料包成员的实际大小与记录不符")
                        output.write(block)
                    if actual != info.file_size:
                        raise LocalError("INVALID_ASSET", "资料包成员的实际大小与记录不符")
            for role, name in asset.roles.items():
                if role not in {"transcript_origin"}:
                    paths[role] = target.joinpath(*PurePosixPath(name).parts)
            return paths
    except BaseException:
        import shutil
        shutil.rmtree(target)
        raise


def _copy_file(source: Path, target: Path, limit: int) -> None:
    with source.open("rb") as input_file, target.open("xb") as output:
        total = 0
        while block := input_file.read(CHUNK):
            total += len(block)
            if total > limit:
                raise LocalError("SOURCE_CHANGED", "复制期间输入文件发生了变化")
            output.write(block)
        if total != limit:
            raise LocalError("SOURCE_CHANGED", "复制期间输入文件发生了变化")
