"""Workspace paths and non-overwriting publication.

The selected root may itself be a platform alias (e.g. macOS /var). Links below
that root are forbidden. POSIX reads walk directory descriptors with NOFOLLOW;
Windows additionally rejects reparse points and verifies the opened identity.
These checks do not sandbox a process with arbitrary OS access.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import unicodedata
import uuid


def relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError('路径必须是安全相对路径')
    parts = value.split('/')
    for part in parts:
        if (part in {'', '.', '..'} or part.endswith((' ', '.'))
                or re.search(r'[\x00-\x1f<>:"|?*]', part)
                or re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part)):
            raise ValueError('不安全的相对路径组件')
    return '/'.join(parts)


def role_path(value: str, role: str) -> str:
    value = relative_path(value)
    exact = {'transcript': 'transcript/transcript.txt', 'transcript_segments': 'transcript/segments.json',
             'asr_raw': 'transcript/raw.json', 'slide_index': 'slides/index.json',
             'slide_alignment': 'slides/alignment.json'}
    prefixes = {'skill': 'handoff/skill/', 'slide_image': 'slides/images/',
                'note': 'notes/', 'audio': 'audio/'}
    if role in exact and value == exact[role]:
        return value
    if role in prefixes and value.startswith(prefixes[role]):
        if role != 'note' or value.lower().endswith('.md'):
            return value
    raise ValueError(f'材料路径不符合角色 {role}')


def safe_path(root: Path, value: str, *, role: str | None = None, missing: bool = False) -> Path:
    root = Path(root).resolve(strict=True)
    value = role_path(value, role) if role else relative_path(value)
    current = root
    for component in value.split('/'):
        # Reject case/normalization ambiguity even on a case-sensitive host.
        if current.is_dir():
            key = unicodedata.normalize('NFC', component).casefold()
            matches = [p.name for p in current.iterdir()
                       if unicodedata.normalize('NFC', p.name).casefold() == key]
            if len(matches) > 1 or (matches and matches[0] != component):
                raise ValueError('路径存在大小写或 Unicode 歧义')
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('工作区路径不能包含链接或 reparse point')
    if not current.resolve(strict=not missing).is_relative_to(root):
        raise ValueError('路径越出工作区')
    return current


@contextlib.contextmanager
def _open_regular(root: Path, value: str, *, role: str | None = None):
    """Open a workspace file with the link/identity checks shared by every reader."""
    root = Path(root).resolve(strict=True)
    path = safe_path(root, value, role=role)
    if os.name == 'posix':
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            parts = relative_path(value).split('/')
            for i, component in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW
                if i < len(parts) - 1:
                    flags |= os.O_DIRECTORY
                next_fd = os.open(component, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('材料不是普通文件')
            with os.fdopen(os.dup(fd), 'rb') as handle:
                yield handle
        finally:
            os.close(fd)
    else:
        before = path.lstat()
        with path.open('rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode) or not os.path.samestat(before, os.fstat(handle.fileno())):
                raise ValueError('材料在读取期间发生变化')
            yield handle
        safe_path(root, value, role=role)
        if not os.path.samestat(before, path.lstat()):
            raise ValueError('材料在读取期间被替换')


def read_bytes(root: Path, value: str, *, role: str | None = None) -> bytes:
    with _open_regular(root, value, role=role) as handle:
        return handle.read()


def sha256_file(root: Path, value: str, *, role: str | None = None) -> str:
    """Stream a workspace file's SHA-256 without loading it (audio can be large)."""
    digest = hashlib.sha256()
    with _open_regular(root, value, role=role) as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def read_json(root: Path, value: str):
    return json.loads(read_bytes(root, value).decode('utf-8'))


def resource_path(root: Path, note: Path, link: str) -> str:
    """Normalize relative image navigation before applying the root boundary."""
    from urllib.parse import unquote, urlsplit
    import posixpath
    decoded = unquote(link)
    if urlsplit(decoded).scheme or decoded.startswith(('/', '\\')) or '\\' in decoded:
        raise ValueError('图片必须是工作区内本地资源')
    note_relative = note.absolute().relative_to(Path(root).resolve()).as_posix()
    relative = posixpath.normpath(posixpath.join(posixpath.dirname(note_relative), decoded))
    relative_path(relative)
    if not relative.startswith(('slides/images/', 'notes/')):
        raise ValueError('图片不属于允许的资源目录')
    safe_path(root, relative)
    return relative


def publish_bytes(root: Path, relative: str, data: bytes, *, replace: bool = False) -> Path:
    """Publish an immutable byte snapshot; POSIX destination ancestors are pinned."""
    root = Path(root).resolve(strict=True)
    target = safe_path(root, relative, missing=True)
    if os.name != 'posix':
        temporary = target.with_name('.publish-' + uuid.uuid4().hex)
        try:
            temporary.write_bytes(data)
            safe_path(root, relative, missing=True)
            if replace:
                temporary.replace(target)
            else:
                publish_exclusive(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    temporary_name = '.publish-' + uuid.uuid4().hex
    created = False
    try:
        parts = relative_path(relative).split('/')
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        file_fd = os.open(temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
        created = True
        with os.fdopen(file_fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary_name, parts[-1], src_dir_fd=fd, dst_dir_fd=fd)
            created = False
        else:
            os.link(temporary_name, parts[-1], src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
    finally:
        if created:
            os.unlink(temporary_name, dir_fd=fd)
        os.close(fd)
    return target


def publish_exclusive(source: Path, target: Path, *, allow_copy: bool = False) -> None:
    """Hard-link publication is atomic. Optional portable copy uses a completion marker.

    Readers of portable fallback must require <target>.complete. Unsupported
    filesystems fail closed by default rather than claiming atomic publication.
    """
    try:
        os.link(source, target)
        return
    except FileExistsError:
        raise
    except OSError:
        if not allow_copy:
            raise
    created_stat = None
    try:
        with source.open('rb') as src, target.open('xb') as dst:
            created_stat = os.fstat(dst.fileno())
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        with target.with_name(target.name + '.complete').open('x', encoding='ascii') as marker:
            marker.write(hashlib.sha256(source.read_bytes()).hexdigest() + '\n')
    except BaseException:
        if created_stat is not None:
            try:
                if os.path.samestat(created_stat, target.lstat()):
                    target.unlink()
            except FileNotFoundError:
                pass
        raise

