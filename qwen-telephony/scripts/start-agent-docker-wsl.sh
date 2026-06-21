#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP="$ROOT/qwen-telephony"
ENV_FILE="$APP/config/local.env"
DOCKER_CONFIG_DIR="$APP/.docker"
WIN_ROOT="$(wslpath -w "$ROOT")"
WIN_APP="$(wslpath -w "$APP")"
WIN_ENV_FILE="$(wslpath -w "$ENV_FILE")"
WIN_DOTENV="$(wslpath -w "$ROOT/.env")"
WIN_LOGS="$(wslpath -w "$APP/logs")"

export PATH="$HOME/.local/bin:$PATH"

mkdir -p "$APP/logs" "$DOCKER_CONFIG_DIR"
if [[ ! -f "$DOCKER_CONFIG_DIR/config.json" ]]; then
  printf '{}\n' > "$DOCKER_CONFIG_DIR/config.json"
fi
export DOCKER_CONFIG="$DOCKER_CONFIG_DIR"

if ! docker network inspect qwen-livekit-net >/dev/null 2>&1; then
  echo "qwen-livekit-net not found. Start infrastructure first." >&2
  exit 1
fi

docker build -f "$WIN_APP\\Dockerfile.agent" -t qwen-telephony-agent "$WIN_ROOT"
docker rm -f qwen-telephony-agent >/dev/null 2>&1 || true

ENV_ARGS=()
if [[ -f "$ROOT/.env" ]]; then
  ENV_ARGS+=(--env-file "$WIN_DOTENV")
fi
if [[ -f "$ENV_FILE" ]]; then
  ENV_ARGS+=(--env-file "$WIN_ENV_FILE")
fi

docker run -d \
  --name qwen-telephony-agent \
  --network qwen-livekit-net \
  "${ENV_ARGS[@]}" \
  -e LIVEKIT_URL=ws://qwen-livekit:7880 \
  -v "$WIN_LOGS:/app/qwen-telephony/logs" \
  qwen-telephony-agent

echo "Agent container started."
echo "Logs:"
echo "  docker logs -f qwen-telephony-agent"
