#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"

find_python() {
  local candidate
  for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && \
       "$candidate" -c 'import sys; raise SystemExit(0 if (3,11) <= sys.version_info[:2] < (4,0) else 1)' >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

refresh_brew_path() {
  if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [[ -x /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
}

ensure_homebrew() {
  refresh_brew_path
  if command -v brew >/dev/null 2>&1; then
    return 0
  fi
  echo "Homebrew is needed to install a missing supported Python (or FFmpeg in a source/developer checkout without bundled runtime)."
  echo "Course Compiler will use Homebrew's official installer from brew.sh."
  if [[ ! -t 0 ]]; then
    echo "Homebrew is missing. Re-run setup-course-compiler.command interactively, or install a supported Python manually first."
    exit 1
  fi
  read -r -p "Install Homebrew now? [Y/n] " answer
  case "${answer:-Y}" in
    [Nn]*) echo "Setup cancelled. Install Homebrew/Python manually, then run this file again."; exit 1 ;;
  esac
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  refresh_brew_path
  command -v brew >/dev/null 2>&1 || { echo "Homebrew installation finished but brew is not on PATH."; exit 1; }
}

PYTHON="$(find_python || true)"
if [[ -z "$PYTHON" ]]; then
  echo "Python >=3.11,<4 was not found. Preparing Python 3.11..."
  ensure_homebrew
  brew install python@3.11
  refresh_brew_path
  PYTHON="$(find_python || true)"
fi
if [[ -z "$PYTHON" ]]; then
  echo "Could not find a supported Python after installation."
  exit 1
fi

if [[ ! -x runtime/ffmpeg/ffmpeg || ! -x runtime/ffmpeg/ffprobe ]]; then
  if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
    ensure_homebrew
  fi
fi

echo "Using Python: $PYTHON"
if "$PYTHON" setup_handouter.py "$@"; then
  :
else
  status=$?
  echo
  echo "Course Compiler setup failed. Review the message above."
  exit "$status"
fi

echo
echo "Course Compiler is ready. You can now double-click start-course-compiler.command."
if [[ -t 0 ]]; then
  read -r -p "Press Enter to close this window..." _
fi
