#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP="$ROOT/qwen-telephony"
ENV_FILE="$APP/config/local.env"
VENV="$APP/.venv"
LOG_DIR="$APP/logs"
AGENT_LOG="$LOG_DIR/agent.log"

mkdir -p "$LOG_DIR"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source <(sed '1s/^\xEF\xBB\xBF//;s/\r$//' "$ROOT/.env")
  set +a
fi

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source <(sed '1s/^\xEF\xBB\xBF//;s/\r$//' "$ENV_FILE")
  set +a
fi

LIVEKIT_URL="${LIVEKIT_URL:-ws://127.0.0.1:7880}"
LIVEKIT_HTTP_URL="${LIVEKIT_HTTP_URL:-http://127.0.0.1:7880}"
LIVEKIT_API_KEY="${LIVEKIT_API_KEY:-devkey}"
LIVEKIT_API_SECRET="${LIVEKIT_API_SECRET:-secret}"
QWEN_AGENT_ROOM="${QWEN_AGENT_ROOM:-qwen-phone-room}"
SIP_TRUNK_NAME="${SIP_TRUNK_NAME:-microsip-local-inbound}"
SIP_DISPATCH_RULE_NAME="${SIP_DISPATCH_RULE_NAME:-microsip-to-qwen-agent}"

status() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

container_running() {
  local name="$1"
  [[ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || true)" == "true" ]]
}

livekit_http_ok() {
  curl -fsS "$LIVEKIT_HTTP_URL" >/dev/null 2>&1
}

agent_process_ok() {
  pgrep -f "python -u phone_agent.py start" >/dev/null 2>&1
}

agent_proxy_ok() {
  if [[ -z "${HTTP_PROXY:-${http_proxy:-}}" ]]; then
    return 0
  fi

  local pid
  pid="$(pgrep -f "python -u phone_agent.py start" | head -n 1 || true)"
  [[ -n "$pid" ]] || return 1
  tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep -Eq '^(HTTP_PROXY|http_proxy)='
}

agent_port_ok() {
  curl -fsS --max-time 2 http://127.0.0.1:18081 >/dev/null 2>&1 || nc -z 127.0.0.1 18081 >/dev/null 2>&1
}

agent_registered_recently_or_running() {
  agent_process_ok && [[ -f "$AGENT_LOG" ]] && strings "$AGENT_LOG" | tail -n 300 | grep -q '"registered worker"'
}

sip_config_ok() {
  [[ -d "$VENV" ]] || return 1
  (
    set +u
    source "$VENV/bin/activate"
    export LIVEKIT_URL LIVEKIT_API_KEY LIVEKIT_API_SECRET QWEN_AGENT_ROOM SIP_TRUNK_NAME SIP_DISPATCH_RULE_NAME
    python - <<'PY'
import asyncio
import os
import sys
import warnings
from livekit import api

warnings.filterwarnings("ignore", message="The HMAC key is .*")

async def main() -> int:
    async with api.LiveKitAPI(
        os.getenv("LIVEKIT_URL", "ws://127.0.0.1:7880"),
        os.getenv("LIVEKIT_API_KEY", "devkey"),
        os.getenv("LIVEKIT_API_SECRET", "secret"),
    ) as lkapi:
        trunks = await lkapi.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())
        rules = await lkapi.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())

    trunk_name = os.getenv("SIP_TRUNK_NAME", "microsip-local-inbound")
    rule_name = os.getenv("SIP_DISPATCH_RULE_NAME", "microsip-to-qwen-agent")
    room_name = os.getenv("QWEN_AGENT_ROOM", "qwen-phone-room")

    trunk_ids = {
        item.sip_trunk_id
        for item in trunks.items
        if item.name == trunk_name
    }
    if not trunk_ids:
        print(f"missing inbound trunk: {trunk_name}")
        return 1

    for item in rules.items:
        direct = item.rule.dispatch_rule_direct
        if item.name == rule_name and direct.room_name == room_name and any(tid in trunk_ids for tid in item.trunk_ids):
            print(f"sip config ok: trunk={trunk_name} rule={rule_name} room={room_name}")
            return 0

    print(f"missing dispatch rule: {rule_name} -> {room_name}")
    return 1

sys.exit(asyncio.run(main()))
PY
  )
}

ensure_bootstrap() {
  if [[ ! -d "$VENV" ]]; then
    status "Python venv missing; running bootstrap"
    "$APP/scripts/bootstrap-wsl.sh"
  fi
}

ensure_infra() {
  local bad=0

  if ! docker info >/dev/null 2>&1; then
    status "Docker is not available. Start Docker Desktop and rerun this script."
    exit 1
  fi

  for name in qwen-livekit-redis qwen-livekit qwen-livekit-sip; do
    if ! container_running "$name"; then
      status "Container unhealthy or missing: $name"
      bad=1
    fi
  done

  if ! livekit_http_ok; then
    status "LiveKit HTTP health failed: $LIVEKIT_HTTP_URL"
    bad=1
  fi

  if [[ "$bad" -eq 1 ]]; then
    status "Starting LiveKit/SIP infrastructure"
    "$APP/scripts/start-infra-wsl.sh"
  else
    status "Infrastructure containers and LiveKit HTTP are healthy"
  fi
}

ensure_sip_config() {
  ensure_bootstrap
  if sip_config_ok; then
    status "SIP trunk and dispatch rule are healthy"
    return
  fi

  status "SIP config missing or unhealthy; initializing SIP"
  "$APP/scripts/init-sip-wsl.sh"
}

ensure_agent() {
  local restart=0

  if ! agent_process_ok; then
    status "Agent process is not running"
    restart=1
  elif ! agent_proxy_ok; then
    status "Agent process exists but proxy environment is missing"
    restart=1
  elif ! agent_port_ok; then
    status "Agent process exists but port 18081 is not reachable"
    restart=1
  elif ! agent_registered_recently_or_running; then
    status "Agent has no registered worker marker in log"
    restart=1
  fi

  if [[ "$restart" -eq 1 ]]; then
    status "Starting agent"
    "$APP/scripts/start-agent-bg-wsl.sh"
  else
    status "Agent process, port, and worker registration look healthy"
  fi

  status "Waiting for agent worker registration"
  for _ in {1..120}; do
    if agent_registered_recently_or_running; then
      status "Agent worker registered"
      return
    fi
    sleep 1
  done

  status "Agent did not register within timeout. Check $AGENT_LOG"
  exit 1
}

main() {
  status "Health check started"
  ensure_infra
  ensure_sip_config
  ensure_agent

  status "System healthy"
  echo "LiveKit: $LIVEKIT_URL"
  echo "SIP: sip:1000@127.0.0.1:${SIP_PORT:-5066}"
  echo "Agent log: $AGENT_LOG"
}

main "$@"
