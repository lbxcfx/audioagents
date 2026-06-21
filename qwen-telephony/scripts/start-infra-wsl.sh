#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP="$ROOT/qwen-telephony"
ENV_FILE="$APP/config/local.env"
DOCKER_CONFIG_DIR="$APP/.docker"

export PATH="$HOME/.local/bin:$PATH"

mkdir -p "$DOCKER_CONFIG_DIR"
if [[ ! -f "$DOCKER_CONFIG_DIR/config.json" ]]; then
  printf '{}\n' > "$DOCKER_CONFIG_DIR/config.json"
fi
export DOCKER_CONFIG="$DOCKER_CONFIG_DIR"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source <(sed 's/\r$//' "$ENV_FILE")
  set +a
fi

LIVEKIT_API_KEY="${LIVEKIT_API_KEY:-devkey}"
LIVEKIT_API_SECRET="${LIVEKIT_API_SECRET:-secret}"
LIVEKIT_NODE_IP="${LIVEKIT_NODE_IP:-127.0.0.1}"
SIP_PORT="${SIP_PORT:-5060}"
SIP_RTP_PORT_RANGE="${SIP_RTP_PORT_RANGE:-10000-10100}"
SIP_RTP_START="${SIP_RTP_PORT_RANGE%-*}"
SIP_RTP_END="${SIP_RTP_PORT_RANGE#*-}"

docker rm -f qwen-livekit qwen-livekit-sip qwen-livekit-redis >/dev/null 2>&1 || true
docker network rm qwen-livekit-net >/dev/null 2>&1 || true
docker network create qwen-livekit-net >/dev/null

docker run -d --name qwen-livekit-redis --network qwen-livekit-net redis:7-alpine redis-server --save "" --appendonly no

docker run -d --name qwen-livekit --network qwen-livekit-net \
  -p 7880:7880 \
  -p 7881:7881 \
  -p 7882:7882/udp \
  livekit/livekit-server:latest \
  --dev \
  --bind 0.0.0.0 \
  --keys "$LIVEKIT_API_KEY: $LIVEKIT_API_SECRET" \
  --node-ip "$LIVEKIT_NODE_IP" \
  --redis-host qwen-livekit-redis:6379

SIP_CONFIG_BODY="$(cat <<YAML
api_key: "$LIVEKIT_API_KEY"
api_secret: "$LIVEKIT_API_SECRET"
ws_url: "ws://qwen-livekit:7880"
redis:
  address: "qwen-livekit-redis:6379"
sip_port: ${SIP_PORT}
sip_port_listen: ${SIP_PORT}
sip_hostname: "127.0.0.1"
rtp_port: ${SIP_RTP_PORT_RANGE}
use_external_ip: false
nat_1_to_1_ip: "127.0.0.1"
media_nat_1_to_1_ip: "127.0.0.1"
symmetric_rtp: true
ignore_local_addr_in_sdp: true
logging:
  level: debug
YAML
)"

docker run -d --name qwen-livekit-sip --network qwen-livekit-net \
  -p ${SIP_PORT}:${SIP_PORT}/udp \
  -p ${SIP_PORT}:${SIP_PORT}/tcp \
  -p ${SIP_RTP_START}-${SIP_RTP_END}:${SIP_RTP_START}-${SIP_RTP_END}/udp \
  -e SIP_CONFIG_BODY="$SIP_CONFIG_BODY" \
  livekit/sip:latest

echo "Waiting for LiveKit HTTP health..."
for _ in {1..40}; do
  if curl -fsS http://127.0.0.1:7880 >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo "Infrastructure started."
echo "LiveKit: ws://127.0.0.1:7880"
echo "SIP: udp://127.0.0.1:${SIP_PORT}"
echo "Logs:"
echo "  docker logs -f qwen-livekit"
echo "  docker logs -f qwen-livekit-sip"
