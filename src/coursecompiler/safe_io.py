"""Workspace paths and non-overwriting publication.

The selected root may itself be a platform alias (e.g. macOS /var). Links below
that root are forbidden. POSIX reads walk directory descriptors with NOFOLLOW;
Windows additionally rejects reparse points and verifies the opened identity.
These checks do not sandbox a process with arbitrary OS access.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import unicodedata
import uuid
import sys
from functools import wraps


from .safe_io_core import (relative_path, role_path, safe_path, read_bytes, read_json, resource_path, publish_bytes, publish_exclusive, sha256_file)

_local = threading.local()


def locked_workspace(function):
    @wraps(function)
    def wrapped(workspace, *args, **kwargs):
        from .organization import resolve_workspace
        workspace = resolve_workspace(workspace)
        with workspace_lock(Path(workspace)):
            from .transactions import recover_refresh
            recover_refresh(Path(workspace))
            return function(workspace, *args, **kwargs)
    return wrapped


def locked_output_root(function):
    @wraps(function)
    def wrapped(workspace_root, *args, **kwargs):
        root = Path(workspace_root)
        root.mkdir(parents=True, exist_ok=True)
        with workspace_lock(root):
            return function(workspace_root, *args, **kwargs)
    return wrapped


def publish_directory(source: Path, target: Path) -> None:
    """Native exclusive directory rename; unsupported platforms fail closed."""
    if os.name == 'nt':
        os.rename(source, target)  # Windows rename refuses an existing target.
        return
    import ctypes
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        result = library.renamex_np(os.fsencode(source), os.fsencode(target), 4)  # RENAME_EXCL
    elif sys.platform.startswith('linux') and hasattr(library, 'renameat2'):
        result = library.renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    else:
        raise OSError('此平台不支持排他目录发布')
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))


@contextmanager
def workspace_lock(root: Path):
    """OS advisory lock. A leftover file is not a live lock; never steal by age."""
    root = Path(root).resolve(strict=True)
    held = getattr(_local, 'held', set())
    if root in held:
        yield
        return
    path = safe_path(root, '.handouter.lock', missing=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    try:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b' ')
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError('讲次正在被另一任务占用') from exc
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError('讲次正在被另一任务占用') from exc
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({'pid': os.getpid()}).encode('ascii'))
        _local.held = held | {root}
        yield
    finally:
        _local.held = held
        os.close(fd)
