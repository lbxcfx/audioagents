#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP="$ROOT/qwen-telephony"
VENV="$APP/.venv"

if [[ ! -d "$VENV" ]]; then
  "$APP/scripts/bootstrap-wsl.sh"
fi

source "$VENV/bin/activate"
cd "$APP"
python -m uvicorn server.main:app --host 127.0.0.1 --port "${OPS_PORT:-8090}"
