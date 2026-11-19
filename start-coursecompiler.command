#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo 'Run bash setup-coursecompiler.command first.'
  exit 1
fi
exec .venv/bin/python setup_coursecompiler.py --start
