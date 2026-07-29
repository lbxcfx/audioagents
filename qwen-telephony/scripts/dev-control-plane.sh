#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/dev-common.sh"
ensure_dev_env
load_dev_env
ensure_dev_venv

export CLOUD_PARITY_DATABASE_URL="${CLOUD_PARITY_DATABASE_URL:-postgresql://cloud_parity:cloud-parity-dev-only@127.0.0.1:5432/cloud_parity}"
export CLOUD_PARITY_ENV="${CLOUD_PARITY_ENV:-development}"
export CLOUD_PARITY_AUTH_MODE="${CLOUD_PARITY_AUTH_MODE:-development}"
export CLOUD_PARITY_BUILD_DRIVER="${CLOUD_PARITY_BUILD_DRIVER:-disabled}"

cd "$DEV_APP"
exec "$DEV_VENV/bin/python" -m uvicorn server.main:app \
  --host 127.0.0.1 --port "${CLOUD_PARITY_PORT:-8091}" --reload
