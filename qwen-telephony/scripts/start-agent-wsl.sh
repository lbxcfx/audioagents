#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP="$ROOT/qwen-telephony"
VENV="$APP/.venv"

if [[ ! -d "$VENV" ]]; then
  "$APP/scripts/bootstrap-wsl.sh"
fi

set -a
[[ -f "$ROOT/.env" ]] && source <(sed 's/\r$//' "$ROOT/.env")
[[ -f "$APP/config/local.env" ]] && source <(sed 's/\r$//' "$APP/config/local.env")
set +a

export LIVEKIT_API_KEY="${LIVEKIT_API_KEY:-devkey}"
export LIVEKIT_API_SECRET="${LIVEKIT_API_SECRET:-secret}"
export LIVEKIT_URL="${LIVEKIT_URL:-ws://127.0.0.1:7880}"
export PYTHONPATH="$APP/agent:${PYTHONPATH:-}"

source "$VENV/bin/activate"
cd "$APP/agent"
python -u phone_agent.py start
