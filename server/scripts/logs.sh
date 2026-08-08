#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SERVER_DIR=$(dirname "$SCRIPT_DIR")
cd "$SERVER_DIR"

if [ "$#" -eq 0 ]; then
  docker compose logs --follow --tail 200
else
  docker compose logs --follow --tail 200 "$@"
fi
