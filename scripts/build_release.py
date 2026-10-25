"""Build ordinary-user Windows/macOS ZIP bundles with optional bundled FFmpeg."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_MANIFEST = ROOT / "scripts" / "third_party_runtime.json"
TARGETS = {
    "windows-x64": {"platform": "windows", "arch": "x64"},
    "macos-arm64": {"platform": "macos", "arch": "arm64"},
    "macos-x64": {"platform": "macos", "arch": "x64"},
}
MAX_RUNTIME_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_RUNTIME_MEMBER_BYTES = 256 * 1024 * 1024


def _version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def _runtime_manifest() -> dict:
    payload = json.loads(RUNTIME_MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("invalid third-party runtime manifest")
    return payload


def _platform_files(platform: str, relative: str) -> bool:
    """Keep one clear bootstrap path per user-facing platform bundle."""
    suffix = PurePosixPath(relative).suffix.lower()
    if platform == "macos":
        return suffix != ".cmd"
    if platform == "windows":
        return suffix != ".command"
    raise ValueError(f"unknown release platform: {platform}")


def _start_here(target: str, version: str, *, bundled_ffmpeg: bool) -> str:
    spec = TARGETS[target]
    platform = spec["platform"]
    runtime_line = (
        "FFmpeg/FFprobe are included in this archive and copied only into Course Compiler's private environment."
        if bundled_ffmpeg else
        "This developer/test bundle does not contain FFmpeg; setup may use a system package manager fallback."
    )
    if platform == "windows":
        steps = """1. Double-click setup-course-compiler.cmd.\n   - Existing Python >=3.11,<4 is reused (3.14 included; later versions require compatible dependencies); if absent and winget is available, Python 3.11 is installed.\n   - Python packages, bundled FFmpeg, SenseVoiceSmall and FSMN-VAD are then prepared automatically.\n2. When setup says Ready, double-click start-course-compiler.cmd.\n3. For browser ZIPs, install and configure an external collector first (docs/EXTERNAL_COLLECTOR.md); confirm its media domains in the TUI. TXT/media/standard ZIPs need no plugin.\n"""
    else:
        steps = """1. Double-click setup-course-compiler.command.\n   - Existing Python >=3.11,<4 is reused (3.14 included; later versions require compatible dependencies); if absent, the bootstrap can prepare Python through Homebrew after confirmation.\n   - Python packages, bundled FFmpeg, SenseVoiceSmall and FSMN-VAD are then prepared automatically.\n2. When setup says Ready, double-click start-course-compiler.command.\n3. For browser ZIPs, install and configure an external collector first (docs/EXTERNAL_COLLECTOR.md); confirm its media domains in the TUI. TXT/media/standard ZIPs need no plugin.\n"""
    title = f"Course Compiler {version} — {target}"
    return f"""{title}\n{'=' * len(title)}\n\n{steps}\n{runtime_line}\n\nInternet is required for the normal first-time Python/model setup. Models are downloaded during setup so the first transcription does not need a surprise model download.\n\nIf setup is interrupted, run the setup launcher again; existing course outputs are never intentionally overwritten.\nSee README.md and docs/RELEASE.md for advanced/offline installation, third-party notices and known validation boundaries.\n"""


def _zip_info(name: str, *, executable: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.create_system = 3  # Unix permissions are honored by macOS archive tools.
    info.date_time = (2026, 1, 1, 0, 0, 0)
    mode = 0o100755 if executable else 0o100644
    info.external_attr = mode << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(url: str, target: Path) -> None:
    """Download one pinned archive, falling back to curl for local CA issues."""
    temporary = target.with_suffix(target.suffix + ".part")
    temporary.unlink(missing_ok=True)
    try:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Handouter release builder"})
            with urllib.request.urlopen(request, timeout=90) as response, temporary.open("xb") as output:
                total = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_RUNTIME_ARCHIVE_BYTES:
                        raise ValueError("third-party runtime archive exceeds size limit")
                    output.write(chunk)
        except (OSError, urllib.error.URLError, ValueError):
            temporary.unlink(missing_ok=True)
            curl = shutil.which("curl")
            if not curl:
                raise RuntimeError("runtime download failed and curl is unavailable")
            subprocess.run([
                curl, "-fL", "--retry", "3", "--connect-timeout", "30", "--max-time", "300",
                "-o", str(temporary), url,
            ], check=True)
            if temporary.stat().st_size > MAX_RUNTIME_ARCHIVE_BYTES:
                raise ValueError("third-party runtime archive exceeds size limit")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _cached_archive(url: str, expected_sha256: str, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    suffix = PurePosixPath(urlsplit(url).path).suffix or ".archive"
    target = cache_dir / f"{expected_sha256}{suffix}"
    if target.is_file() and _sha256_file(target) == expected_sha256:
        return target
    target.unlink(missing_ok=True)
    _download(url, target)
    actual = _sha256_file(target)
    if actual != expected_sha256:
        target.unlink(missing_ok=True)
        raise RuntimeError("third-party runtime checksum mismatch")
    return target


def _read_zip_member(archive_path: Path, basename: str) -> bytes:
    with zipfile.ZipFile(archive_path, "r") as archive:
        matches = [info for info in archive.infolist() if not info.is_dir() and PurePosixPath(info.filename).name == basename]
        if len(matches) != 1:
            raise RuntimeError(f"runtime archive does not contain exactly one {basename}")
        info = matches[0]
        if info.file_size <= 0 or info.file_size > MAX_RUNTIME_MEMBER_BYTES:
            raise RuntimeError(f"runtime member has invalid size: {basename}")
        # Refuse Unix symlink members even though we only read bytes.
        if ((info.external_attr >> 16) & 0o170000) == 0o120000:
            raise RuntimeError(f"runtime member must not be a symlink: {basename}")
        data = archive.read(info)
        if len(data) != info.file_size:
            raise RuntimeError(f"runtime member size changed while reading: {basename}")
        return data


def _third_party_notice(target: str, runtime: dict) -> bytes:
    archives = "\n".join(f"- {item['url']}\n  SHA-256: {item['sha256']}" for item in runtime["archives"])
    return f"""Course Compiler bundled FFmpeg notice\n=====================================\n\nTarget: {target}\nProvider: {runtime['provider']}\nFFmpeg version: {runtime['version']}\nReported binary license: {runtime['license']}\nCorresponding FFmpeg source: {runtime['source_url']}\n\nVerified binary archives:\n{archives}\n\nThe bundled ffmpeg/ffprobe executables are separate third-party programs invoked as subprocesses.\nThey are not authored by the Course Compiler project. Preserve this notice when redistributing the user release bundle.\nThe official release checklist still requires the maintainer to review the selected FFmpeg build and all applicable third-party license obligations before public publication.\n""".encode("utf-8")


def _release_cache_dir(override: Path | None) -> Path:
    if override is not None:
        return override
    # Keep the old default path to reuse existing verified downloads. Presence,
    # not truthiness, sets precedence: an empty new value is an explicit error.
    for key in ("COURSE_COMPILER_RELEASE_CACHE", "HANDOUTER_RELEASE_CACHE"):
        if key in os.environ:
            value = os.environ[key]
            if not value.strip():
                raise ValueError(f"{key} must name a nonempty cache directory")
            return Path(value)
    return Path.home() / ".cache" / "handouter" / "release"


def _runtime_files(target: str, *, cache_dir: Path | None = None) -> tuple[dict[str, bytes], dict]:
    manifest = _runtime_manifest()
    runtime = manifest.get("ffmpeg", {}).get(target)
    if not isinstance(runtime, dict):
        raise ValueError(f"no pinned FFmpeg runtime for {target}")
    cache = _release_cache_dir(cache_dir)
    files: dict[str, bytes] = {}
    verified: list[dict] = []
    for item in runtime.get("archives", []):
        if not isinstance(item, dict):
            raise ValueError("invalid runtime archive entry")
        url = str(item.get("url") or "")
        expected = str(item.get("sha256") or "").lower()
        members = item.get("members")
        if not url.startswith("https://") or len(expected) != 64 or not isinstance(members, dict):
            raise ValueError("runtime archive must have https URL, SHA-256 and members")
        archive_path = _cached_archive(url, expected, cache)
        verified.append({"url": url, "sha256": expected, "bytes": archive_path.stat().st_size})
        for output_name, archive_basename in members.items():
            if output_name in files or PurePosixPath(output_name).name != output_name:
                raise ValueError("invalid or duplicate runtime output name")
            files[f"runtime/ffmpeg/{output_name}"] = _read_zip_member(archive_path, str(archive_basename))
    expected_names = {"ffmpeg.exe", "ffprobe.exe"} if target.startswith("windows-") else {"ffmpeg", "ffprobe"}
    actual_names = {PurePosixPath(name).name for name in files}
    if actual_names != expected_names:
        raise RuntimeError(f"runtime for {target} is incomplete: {sorted(actual_names)}")
    files["runtime/ffmpeg/THIRD_PARTY_FFMPEG.txt"] = _third_party_notice(target, runtime)
    return files, {
        "name": "ffmpeg",
        "version": runtime["version"],
        "provider": runtime["provider"],
        "license": runtime["license"],
        "source_url": runtime["source_url"],
        "archives": verified,
    }


def build_bundle(
    target_name: str,
    output_dir: str | Path,
    *,
    force: bool = False,
    include_runtime: bool = True,
    cache_dir: str | Path | None = None,
) -> Path:
    """Build one release ZIP without pulling local outputs, caches, tests or Git data."""
    if target_name not in TARGETS:
        raise ValueError("target must be one of: " + ", ".join(TARGETS))
    target_spec = TARGETS[target_name]
    platform = target_spec["platform"]
    version = _version()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"Course-Compiler-{version}-{target_name}.zip"
    if target.exists() and not force:
        raise FileExistsError(f"release bundle already exists: {target}")

    import sys
    sys.path.insert(0, str(ROOT))
    import handouter_build

    with tempfile.TemporaryDirectory() as td:
        temp = Path(td)
        sdist = temp / handouter_build.build_sdist(str(temp))
        files: dict[str, bytes] = {}
        with tarfile.open(sdist, "r:gz") as archive:
            members = [member for member in archive.getmembers() if member.isfile()]
            prefix = f"handouter-{version}/"
            for member in members:
                if not member.name.startswith(prefix):
                    raise ValueError(f"unexpected sdist member: {member.name}")
                relative = member.name[len(prefix):]
                posix = PurePosixPath(relative)
                # Keep the complete reviewed source tree, including both sets
                # of launchers: release-files.json must remain valid when the
                # source bootstrap ZIP is installed or rebuilt on its own.
                if posix.is_absolute() or ".." in posix.parts:
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ValueError(f"could not read sdist member: {relative}")
                files[relative] = extracted.read()

        runtime_metadata = None
        if include_runtime:
            runtime_files, runtime_metadata = _runtime_files(
                target_name,
                cache_dir=Path(cache_dir) if cache_dir is not None else None,
            )
            files.update(runtime_files)
        files["START_HERE.txt"] = _start_here(target_name, version, bundled_ffmpeg=include_runtime).encode("utf-8")
        manifest = {
            "schema_version": 1,
            "product": "Course Compiler",
            "version": version,
            "target": target_name,
            "platform": platform,
            "arch": target_spec["arch"],
            "bundled_runtime": runtime_metadata,
            "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
        }
        files["RELEASE_MANIFEST.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

        staging = target.with_suffix(target.suffix + ".tmp")
        staging.unlink(missing_ok=True)
        root_name = f"Course-Compiler-{version}-{target_name}"
        try:
            with zipfile.ZipFile(staging, "x") as archive:
                for relative, data in sorted(files.items()):
                    executable = (
                        platform == "macos"
                        and relative in {"setup-handouter.command", "start-handouter.command", "setup-course-compiler.command", "start-course-compiler.command", "runtime/ffmpeg/ffmpeg", "runtime/ffmpeg/ffprobe"}
                    )
                    archive.writestr(_zip_info(f"{root_name}/{relative}", executable=executable), data)
            with zipfile.ZipFile(staging, "r") as archive:
                broken = archive.testzip()
                if broken:
                    raise RuntimeError(f"release ZIP verification failed at {broken}")
            if force:
                target.unlink(missing_ok=True)
            staging.replace(target)
        finally:
            staging.unlink(missing_ok=True)
    return target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="dist", help="destination directory (default: dist)")
    parser.add_argument("--target", choices=("all", *TARGETS), default="all")
    parser.add_argument("--force", action="store_true", help="replace an existing bundle with the same version")
    parser.add_argument("--without-bundled-ffmpeg", action="store_true",
                        help="developer/CI structure check only; normal user releases should keep bundled FFmpeg")
    parser.add_argument("--cache-dir", help="optional cache for verified third-party archives")
    args = parser.parse_args(argv)
    targets = tuple(TARGETS) if args.target == "all" else (args.target,)
    outputs = [
        build_bundle(
            item,
            args.output_dir,
            force=args.force,
            include_runtime=not args.without_bundled_ffmpeg,
            cache_dir=args.cache_dir,
        )
        for item in targets
    ]
    for path in outputs:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
