"""Install and launch the checkout without activating a virtual environment."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
MIN_PYTHON = (3, 11)
MAX_PYTHON_EXCLUSIVE = (4, 0)
LATEST_CI_PYTHON = (3, 14)


def python_supported(version: tuple[int, int] | None = None) -> bool:
    """Allowed interpreter range; dependencies and runtime are checked separately."""
    value = version or (sys.version_info.major, sys.version_info.minor)
    return MIN_PYTHON <= value < MAX_PYTHON_EXCLUSIVE


def run(command: list[str], **kwargs) -> None:
    subprocess.run(command, check=True, cwd=ROOT, **kwargs)


def refresh_windows_path() -> None:
    """Pick up package-manager PATH additions without restarting the terminal."""
    if sys.platform != "win32":
        return
    import winreg

    paths = [os.environ.get("PATH", "")]
    for hive, key in (
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, r"Environment"),
    ):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value, _ = winreg.QueryValueEx(handle, "Path")
                paths.append(os.path.expandvars(value))
        except OSError:
            pass
    os.environ["PATH"] = os.pathsep.join(paths)


def _media_filename(name: str) -> str:
    return name + '.exe' if os.name == 'nt' else name


def _bundled_media(name: str) -> Path:
    return ROOT / 'runtime' / 'ffmpeg' / _media_filename(name)


def install_bundled_media(python: Path) -> bool:
    """Copy release-bundled FFmpeg tools into the private venv, if present."""
    sources = [_bundled_media(name) for name in ('ffmpeg', 'ffprobe')]
    if not all(path.is_file() and not path.is_symlink() for path in sources):
        return False
    destination = python.parent
    for source in sources:
        target = destination / source.name
        temporary = target.with_name('.' + target.name + '.handouter-tmp')
        temporary.unlink(missing_ok=True)
        shutil.copyfile(source, temporary)
        if os.name != 'nt':
            temporary.chmod(0o755)
        os.replace(temporary, target)
    return True


def _find_media(name: str, python: Path | None = None) -> str | None:
    filename = _media_filename(name)
    candidates = []
    if python is not None:
        candidates.append(python.parent / filename)
    candidates.append(_bundled_media(name))
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink() and (os.name == 'nt' or os.access(candidate, os.X_OK)):
            return str(candidate)
    return shutil.which(name)


def ensure_media(*, python: Path | None = None, offline: bool = False) -> None:
    refresh_windows_path()
    install_bundled_media(python) if python is not None else False
    if not all(_find_media(name, python) for name in ("ffmpeg", "ffprobe")):
        if offline:
            raise RuntimeError('Offline installation requires bundled FFmpeg or preinstalled ffmpeg/ffprobe.')
        print("Bundled FFmpeg was not found; installing a system fallback...", flush=True)
        if sys.platform == "win32" and shutil.which("winget"):
            run(["winget", "install", "--id", "Gyan.FFmpeg", "--exact",
                 "--accept-package-agreements", "--accept-source-agreements",
                 "--disable-interactivity"])
            refresh_windows_path()
        elif sys.platform == "darwin" and shutil.which("brew"):
            run(["brew", "install", "ffmpeg"])
        else:
            raise RuntimeError("FFmpeg is missing from this release and the system. "
                               "Windows: install App Installer (winget); macOS: install Homebrew; "
                               "Linux: install ffmpeg with your package manager, then retry.")
    for name in ("ffmpeg", "ffprobe"):
        executable = _find_media(name, python)
        if not executable:
            raise RuntimeError(f"{name} is still unavailable after setup.")
        run([executable, "-version"], stdout=subprocess.DEVNULL)


def prepare_models(python: Path) -> None:
    """Download and load the production SenseVoice/VAD models during setup.

    Loading on CPU keeps setup independent of optional GPU drivers while still
    proving that both model packages are present and readable. Runtime can later
    select MPS/CUDA automatically.
    """
    code = r'''from funasr import AutoModel
runner = AutoModel(
    model="iic/SenseVoiceSmall",
    vad_model="fsmn-vad",
    vad_kwargs={"max_single_segment_time": 30000},
    device="cpu",
    disable_update=True,
)
assert runner is not None
print("SenseVoiceSmall + FSMN-VAD ready")
'''
    run([str(python), "-c", code])


def check_runtime(python: Path, *, asr: bool = True) -> None:
    # Import the native extensions, rather than accepting package presence alone.
    if asr:
        run([str(python), "-c",
             "import torch, torchaudio, funasr, curses; "
             "from torchaudio.compliance.kaldi import fbank; "
             "assert callable(curses.initscr); "
             "fbank(torch.zeros(1, 16000), sample_frequency=16000); "
             "print('ASR imports OK:', torch.__version__, torchaudio.__version__)"])
    run([str(python), "-m", "pip", "check"])
    features = ['core', 'pdf', 'html', 'media', 'tui'] + (['asr'] if asr else [])
    run([str(python), '-c', f"from handouter.doctor import require_features; require_features({features!r})"])
    run([str(python), "-m", "handouter", "doctor"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", action="store_true", help="launch an existing installation")
    parser.add_argument('--wheelhouse', type=Path, help='install offline using a complete wheelhouse matching this platform and Python')
    parser.add_argument('--skip-model-download', action='store_true',
                        help='install the app without pre-downloading SenseVoice/VAD; first transcription will need network')
    parser.add_argument('--without-asr', action='store_true',
                        help='install only material/notes/HTML features; do not install ASR or download speech models')
    args = parser.parse_args(argv)
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    try:
        if args.start:
            if not python.is_file():
                raise RuntimeError("Run setup-course-compiler first to install Course Compiler.")
            refresh_windows_path()
            run([str(python), "-m", "handouter", "tui"])
            return 0
        if not python_supported():
            raise RuntimeError("Course Compiler requires Python >=3.11,<4.")
        if sys.version_info[:2] > LATEST_CI_PYTHON:
            print("This Python version is newer than the CI target range 3.11–3.14. "
                  "Installation still requires compatible dependencies and successful runtime checks.", flush=True)
        if args.wheelhouse is not None and not args.wheelhouse.expanduser().is_dir():
            raise RuntimeError('Wheelhouse directory does not exist.')
        print("[1/5] Preparing project .venv...", flush=True)
        if not python.is_file():
            if (ROOT / ".venv").exists():
                raise RuntimeError("Existing .venv is incomplete; inspect it before retrying. Nothing was deleted.")
            run([sys.executable, "-m", "venv", str(ROOT / ".venv")])
        run([str(python), "-c", "import sys; assert (3, 11) <= sys.version_info[:2] < (4, 0), 'Python >=3.11,<4 required'; "
             "print('Python', sys.version.split()[0], '(newer than CI target range)' if sys.version_info[:2] > (3, 14) else '')"])
        print("[2/5] Installing Course Compiler and selected dependencies...", flush=True)
        install = [str(python), '-m', 'pip', 'install']
        if args.wheelhouse is not None:
            install += ['--no-index', '--find-links', str(args.wheelhouse.expanduser().resolve())]
        run([*install, '.[html]' if args.without_asr else '.[asr,html]'])
        print("[3/5] Checking FFmpeg...", flush=True)
        if args.wheelhouse is None:
            ensure_media(python=python)
        else:
            ensure_media(python=python, offline=True)
        print("[4/5] Preparing SenseVoice speech models...", flush=True)
        if args.without_asr:
            print("ASR is not installed by this setup; use existing transcripts or notes.", flush=True)
        elif args.skip_model_download:
            print("Skipping model download by request; the first transcription will need network access.", flush=True)
        else:
            prepare_models(python)
        print("[5/5] Verifying installed runtime...", flush=True)
        if args.without_asr:
            check_runtime(python, asr=False)
        else:
            check_runtime(python)
        suffix = (" Material/notes/HTML features are ready; ASR was not installed or verified."
                  if args.without_asr else
                  " Speech models were not preloaded." if args.skip_model_download else " Speech models are preloaded.")
        print("Ready. Run start-course-compiler.cmd (Windows) or double-click start-course-compiler.command (macOS)." + suffix)
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Setup stopped: {exc}\nFix the reported error, then run setup again; existing files are preserved.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
