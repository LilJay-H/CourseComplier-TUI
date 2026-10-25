"""Locate bundled or system runtime executables without mutating global PATH."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys

from . import branded_env

_RUNTIME_ENV = "HANDOUTER_RUNTIME_DIR"


def _tool_filename(name: str) -> str:
    return name + ".exe" if os.name == "nt" and not name.lower().endswith(".exe") else name


def _is_usable(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    return os.name == "nt" or os.access(path, os.X_OK)


def bundled_runtime_dirs() -> tuple[Path, ...]:
    """Return explicit/private runtime locations before falling back to PATH.

    Release setup copies bundled tools beside the venv Python. Source/release
    checkouts can also keep them under ``runtime/ffmpeg``. An explicit env
    override is useful for portable/test launches and never modifies PATH.
    """
    candidates: list[Path] = []
    explicit = branded_env(_RUNTIME_ENV)
    if explicit:
        candidates.append(Path(explicit).expanduser())
    # Keep the venv-facing executable path. Resolving it may follow the Python
    # symlink back to a system/framework installation and skip private tools
    # copied beside ``.venv/bin/python`` or ``.venv/Scripts/python.exe``.
    candidates.append(Path(sys.executable).parent)
    candidates.append(Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin"))
    try:
        checkout = Path(__file__).resolve().parents[2]
        candidates.append(checkout / "runtime" / "ffmpeg")
    except IndexError:  # pragma: no cover - defensive for unusual loaders
        pass
    result: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = os.path.normcase(str(path.resolve(strict=False)))
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return tuple(result)


def find_executable(name: str) -> str | None:
    """Prefer Handouter's private runtime, then use the user's system PATH."""
    filename = _tool_filename(name)
    for directory in bundled_runtime_dirs():
        candidate = directory / filename
        if _is_usable(candidate):
            return str(candidate)
    return shutil.which(name)
