#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SERVER_DIR=$(dirname "$SCRIPT_DIR")
cd "$SERVER_DIR"

docker compose ps
echo
docker compose exec -T control-plane python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8091/api/platform/health/ready', timeout=5).read().decode())"
docker compose exec -T dispatcher python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9091/ready', timeout=5).read().decode())"
