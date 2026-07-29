#!/usr/bin/env bash

set -euo pipefail

DEV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEV_APP="$DEV_ROOT/qwen-telephony"
DEV_ENV_FILE="$DEV_APP/config/dev.env"
DEV_ENV_EXAMPLE="$DEV_APP/config/dev.env.example"
DEV_VENV="$DEV_APP/.venv"

load_dev_env() {
  set -a
  [[ -f "$DEV_ROOT/.env" ]] && source <(sed '1s/^\xEF\xBB\xBF//;s/\r$//' "$DEV_ROOT/.env")
  [[ -f "$DEV_APP/config/local.env" ]] && source <(sed '1s/^\xEF\xBB\xBF//;s/\r$//' "$DEV_APP/config/local.env")
  [[ -f "$DEV_ENV_FILE" ]] && source <(sed '1s/^\xEF\xBB\xBF//;s/\r$//' "$DEV_ENV_FILE")
  set +a
}

ensure_dev_env() {
  if [[ ! -f "$DEV_ENV_FILE" ]]; then
    cp "$DEV_ENV_EXAMPLE" "$DEV_ENV_FILE"
    echo "Created $DEV_ENV_FILE from the development template."
  fi
}

ensure_dev_venv() {
  if [[ ! -x "$DEV_VENV/bin/python" ]]; then
    echo "Python environment is missing; bootstrapping it now."
    "$DEV_APP/scripts/bootstrap-wsl.sh"
  fi
}

compose_dev() {
  docker compose --env-file "$DEV_ENV_FILE" -f "$DEV_ROOT/docker-compose.dev.yml" "$@"
}
