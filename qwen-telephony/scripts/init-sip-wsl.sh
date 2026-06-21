#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP="$ROOT/qwen-telephony"
VENV="$APP/.venv"

if [[ ! -d "$VENV" ]]; then
  "$APP/scripts/bootstrap-wsl.sh"
fi

source "$VENV/bin/activate"
python "$APP/scripts/init-sip.py"
