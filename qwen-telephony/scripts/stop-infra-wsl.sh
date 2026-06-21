#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP="$ROOT/qwen-telephony"
export DOCKER_CONFIG="$APP/.docker"

docker rm -f qwen-livekit-sip qwen-livekit qwen-livekit-redis >/dev/null 2>&1 || true
docker network rm qwen-livekit-net >/dev/null 2>&1 || true
echo "Infrastructure stopped."
