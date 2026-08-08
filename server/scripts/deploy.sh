#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SERVER_DIR=$(dirname "$SCRIPT_DIR")
cd "$SERVER_DIR"

if [ ! -f .env ]; then
  echo "Missing server/.env. Run ./scripts/prepare.sh first." >&2
  exit 1
fi

required_values="PUBLIC_IP DASHSCOPE_API_KEY DEEPSEEK_API_KEY LIVEKIT_SIP_TRUNK_ID"
for name in $required_values; do
  value=$(sed -n "s/^${name}=//p" .env | tail -n 1)
  case "$value" in
    ""|CHANGE_ME_*)
      echo "Set $name in server/.env before deployment." >&2
      exit 1
      ;;
  esac
done

docker compose config --quiet
docker compose build --pull control-plane dispatcher voice-agent
docker compose pull postgres redis livekit livekit-sip minio minio-init livekit-egress bootstrap hermes
docker compose up -d --remove-orphans

echo "AudioAgent stack started. Run ./scripts/health.sh to inspect readiness."
echo "For first-time WeChat login, run ./scripts/wechat-login.sh."
