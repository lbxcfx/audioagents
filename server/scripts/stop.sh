#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SERVER_DIR=$(dirname "$SCRIPT_DIR")
cd "$SERVER_DIR"

# Intentionally keeps all named volumes and WeChat login state.
docker compose down
