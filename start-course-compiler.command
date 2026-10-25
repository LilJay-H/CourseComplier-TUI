#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd "$(dirname "$0")" && pwd)"
exec /bin/bash "$script_dir/start-handouter.command" "$@"
