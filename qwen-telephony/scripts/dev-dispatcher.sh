#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/dev-common.sh"
ensure_dev_env
load_dev_env
ensure_dev_venv

if [[ -z "${CLOUD_PARITY_TELEPHONY_PROJECT_IDS:-}" ]]; then
  echo "Set CLOUD_PARITY_TELEPHONY_PROJECT_IDS in $DEV_ENV_FILE before starting the dispatcher." >&2
  exit 2
fi

export LIVEKIT_URL="${LIVEKIT_URL:-ws://127.0.0.1:7880}"
export CLOUD_PARITY_CONTROL_URL="${CLOUD_PARITY_CONTROL_URL:-http://127.0.0.1:8091}"
export CLOUD_PARITY_SERVICE_USER_ID="${CLOUD_PARITY_SERVICE_USER_ID:-telephony-worker}"
exec "$DEV_VENV/bin/python" -u "$DEV_APP/scripts/telephony-dispatcher.py"
