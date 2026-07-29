#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/dev-common.sh"
ensure_dev_env
load_dev_env
ensure_dev_venv

if [[ -z "${DASHSCOPE_API_KEY:-}" ]]; then
  echo "DASHSCOPE_API_KEY is missing. Add it to $DEV_ROOT/.env." >&2
  exit 2
fi

export LIVEKIT_URL="${LIVEKIT_URL:-ws://127.0.0.1:7880}"
export LIVEKIT_API_KEY="${LIVEKIT_API_KEY:-devkey}"
export LIVEKIT_API_SECRET="${LIVEKIT_API_SECRET:-secret}"
export LIVEKIT_AGENT_NAME="${LIVEKIT_AGENT_NAME:-commercial-agent}"
export QWEN_AGENT_EXPLICIT_NAME="${QWEN_AGENT_EXPLICIT_NAME:-commercial-agent}"
export PYTHONPATH="$DEV_APP/agent:${PYTHONPATH:-}"

cd "$DEV_APP/agent"
exec "$DEV_VENV/bin/python" -u phone_agent.py dev
