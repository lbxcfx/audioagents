#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SERVER_DIR=$(dirname "$SCRIPT_DIR")
cd "$SERVER_DIR"

docker compose up -d hermes
docker compose exec hermes hermes gateway setup
docker compose restart hermes

echo "WeChat credentials are persisted in this Compose deployment's hermes-data volume."
