#!/usr/bin/env bash
set -Eeuo pipefail

# Start the source/development deployment only. The standalone server/ Compose
# package is intentionally not read or invoked by this script.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$ROOT_DIR/qwen-telephony"
SCRIPT_DIR="$APP_DIR/scripts"
COMMON_SCRIPT="$SCRIPT_DIR/dev-common.sh"

CONTROL_UNIT="audioagents-control-plane.service"
DISPATCHER_UNIT="audioagents-dispatcher.service"
VOICE_UNIT="audioagents-voice-agent.service"
FRONTEND_UNIT="audioagents-frontend.service"
HERMES_UNIT="hermes-gateway.service"

RESTART=false

usage() {
  cat <<'EOF'
Usage: ./start.sh [--restart]

Starts the complete source deployment:
  PostgreSQL, Redis, LiveKit, LiveKit SIP, LiveKit Egress, MinIO,
  AudioAgent control plane, dispatcher, voice worker, web console,
  and the Hermes Weixin gateway.

Options:
  --restart  Restart application processes after ensuring infrastructure.
  -h, --help Show this help text.

The independent server/ deployment package is never used.
EOF
}

while (($#)); do
  case "$1" in
    --restart)
      RESTART=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

die() {
  log "ERROR: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command is missing: $1"
}

wait_for_url() {
  local name="$1"
  local url="$2"
  local attempts="${3:-60}"
  local delay="${4:-1}"
  local attempt

  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if curl --noproxy '*' -fsS --max-time 3 "$url" >/dev/null 2>&1; then
      log "$name is ready: $url"
      return 0
    fi
    sleep "$delay"
  done

  die "$name did not become ready: $url"
}

unit_exists() {
  [[ "$(systemctl --user show "$1" --property=LoadState --value 2>/dev/null || true)" != "not-found" ]]
}

ensure_transient_service() {
  local unit="$1"
  local description="$2"
  local command_path="$3"

  if systemctl --user is-active --quiet "$unit"; then
    if [[ "$RESTART" == true ]]; then
      log "Restarting $description"
      systemctl --user restart "$unit"
    else
      log "$description is already running"
    fi
    return
  fi

  if unit_exists "$unit"; then
    log "Starting existing $description unit"
    systemctl --user reset-failed "$unit" >/dev/null 2>&1 || true
    systemctl --user start "$unit"
    return
  fi

  log "Creating and starting $description unit"
  systemd-run --user \
    --unit="$unit" \
    --description="$description" \
    --property=Type=simple \
    --property=Restart=on-failure \
    --property=RestartSec=3s \
    --collect \
    "$command_path" >/dev/null
}

start_hermes() {
  if ! unit_exists "$HERMES_UNIT"; then
    die "Hermes gateway service is not installed. Run 'hermes gateway install' first."
  fi

  if systemctl --user is-active --quiet "$HERMES_UNIT"; then
    if [[ "$RESTART" == true ]]; then
      log "Restarting Hermes Weixin gateway"
      systemctl --user restart "$HERMES_UNIT"
    else
      log "Hermes Weixin gateway is already running"
    fi
  else
    log "Starting Hermes Weixin gateway"
    systemctl --user start "$HERMES_UNIT"
  fi
}

print_failure_context() {
  local exit_code=$?
  if ((exit_code == 0)); then
    return
  fi

  printf '\nStartup failed. Recent application status:\n' >&2
  systemctl --user --no-pager --full status \
    "$CONTROL_UNIT" "$DISPATCHER_UNIT" "$VOICE_UNIT" "$FRONTEND_UNIT" "$HERMES_UNIT" \
    2>/dev/null | tail -n 100 >&2 || true
  printf '\nUse these commands for more detail:\n' >&2
  printf '  docker compose --env-file qwen-telephony/config/dev.env -f docker-compose.dev.yml logs --tail=200\n' >&2
  printf '  journalctl --user -u audioagents-control-plane -u audioagents-dispatcher -u audioagents-voice-agent -u audioagents-frontend -u hermes-gateway -n 200\n' >&2
  exit "$exit_code"
}
trap print_failure_context ERR

main() {
  cd "$ROOT_DIR"

  [[ -f "$COMMON_SCRIPT" ]] || die "Missing startup helper: $COMMON_SCRIPT"
  # shellcheck source=qwen-telephony/scripts/dev-common.sh
  source "$COMMON_SCRIPT"

  require_command docker
  require_command curl
  require_command npm
  require_command systemctl
  require_command systemd-run

  ensure_dev_env
  load_dev_env
  ensure_dev_venv

  [[ -n "${DASHSCOPE_API_KEY:-}" ]] || die "DASHSCOPE_API_KEY is missing from $ROOT_DIR/.env"
  [[ -n "${CLOUD_PARITY_TELEPHONY_PROJECT_IDS:-}" ]] || \
    die "CLOUD_PARITY_TELEPHONY_PROJECT_IDS is missing from $DEV_ENV_FILE"

  docker info >/dev/null 2>&1 || die "Docker Engine is not available"

  log "Starting Docker infrastructure"
  "$SCRIPT_DIR/dev-infra.sh" up

  log "Ensuring the local SIP trunk and dispatch rule exist"
  "$SCRIPT_DIR/dev-init-sip.sh"

  ensure_transient_service \
    "$CONTROL_UNIT" "AudioAgents control plane" "$SCRIPT_DIR/dev-control-plane.sh"
  wait_for_url \
    "Control plane" \
    "http://127.0.0.1:${CLOUD_PARITY_PORT:-8091}/api/platform/health/ready"

  ensure_transient_service \
    "$DISPATCHER_UNIT" "AudioAgents dispatcher" "$SCRIPT_DIR/dev-dispatcher.sh"
  ensure_transient_service \
    "$VOICE_UNIT" "AudioAgents voice worker" "$SCRIPT_DIR/dev-agent.sh"
  ensure_transient_service \
    "$FRONTEND_UNIT" "AudioAgents web console" "$SCRIPT_DIR/dev-frontend.sh"

  wait_for_url \
    "Dispatcher" \
    "http://127.0.0.1:${CLOUD_PARITY_TELEPHONY_HEALTH_PORT:-9091}/ready"
  wait_for_url \
    "Voice worker" \
    "http://127.0.0.1:${QWEN_AGENT_PORT:-18081}/worker" \
    120
  wait_for_url "Web console" "http://127.0.0.1:5173" 120

  start_hermes
  systemctl --user is-active --quiet "$HERMES_UNIT" || die "Hermes gateway failed to start"

  printf '\n'
  log "All project services are ready"
  printf '  Web console:  http://127.0.0.1:5173\n'
  printf '  Control API:  http://127.0.0.1:%s\n' "${CLOUD_PARITY_PORT:-8091}"
  printf '  LiveKit:      %s\n' "${LIVEKIT_HTTP_URL:-http://127.0.0.1:7880}"
  printf '  Dispatcher:   http://127.0.0.1:%s/ready\n' "${CLOUD_PARITY_TELEPHONY_HEALTH_PORT:-9091}"
  printf '  Voice worker: http://127.0.0.1:%s/worker\n' "${QWEN_AGENT_PORT:-18081}"
  printf '\nApplication logs:\n'
  printf '  journalctl --user -f -u audioagents-control-plane -u audioagents-dispatcher -u audioagents-voice-agent -u audioagents-frontend -u hermes-gateway\n'
  printf 'Docker logs:\n'
  printf '  qwen-telephony/scripts/dev-infra.sh logs\n'
}

main
