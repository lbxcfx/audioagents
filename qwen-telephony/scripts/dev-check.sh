#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/dev-common.sh"
ensure_dev_env
load_dev_env

failures=0
check_url() {
  local name="$1" url="$2"
  if curl -fsS --max-time 3 "$url" >/dev/null; then
    printf '[ok]   %s\n' "$name"
  else
    printf '[down] %s (%s)\n' "$name" "$url"
    failures=$((failures + 1))
  fi
}

if docker info >/dev/null 2>&1; then
  printf '[ok]   Docker\n'
else
  printf '[down] Docker\n'
  failures=$((failures + 1))
fi

compose_dev ps
check_url "LiveKit" "${LIVEKIT_HTTP_URL:-http://127.0.0.1:7880}"
check_url "Control plane" "http://127.0.0.1:${CLOUD_PARITY_PORT:-8091}/api/platform/health/ready"
check_url "Frontend" "http://127.0.0.1:5173"

if [[ -n "${CLOUD_PARITY_TELEPHONY_PROJECT_IDS:-}" ]]; then
  check_url "Dispatcher" "http://127.0.0.1:${CLOUD_PARITY_TELEPHONY_HEALTH_PORT:-9091}/ready"
else
  printf '[skip] Dispatcher (project UUID not configured)\n'
fi

if [[ -n "${DASHSCOPE_API_KEY:-}" ]]; then
  check_url "Phone agent" "http://127.0.0.1:${QWEN_AGENT_PORT:-18081}/worker"
else
  printf '[skip] Phone agent (DASHSCOPE_API_KEY not configured)\n'
fi

exit "$failures"
