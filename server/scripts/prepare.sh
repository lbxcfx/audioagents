#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SERVER_DIR=$(dirname "$SCRIPT_DIR")
ENV_FILE="$SERVER_DIR/.env"
EXAMPLE_FILE="$SERVER_DIR/.env.example"

if [ -e "$ENV_FILE" ]; then
  echo "server/.env already exists; left unchanged."
  exit 0
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required to generate deployment secrets." >&2
  exit 1
fi

cp "$EXAMPLE_FILE" "$ENV_FILE"
chmod 600 "$ENV_FILE"

POSTGRES_PASSWORD=$(openssl rand -hex 24)
LIVEKIT_API_KEY="lk_$(openssl rand -hex 8)"
LIVEKIT_API_SECRET=$(openssl rand -hex 32)
MINIO_ROOT_PASSWORD=$(openssl rand -hex 24)
MASTER_KEY=$(openssl rand -base64 32 | tr '+/' '-_')
DISPATCH_KEY=$(openssl rand -base64 32 | tr '+/' '-_')
PHONE_HASH_KEY=$(openssl rand -hex 32)
METRICS_TOKEN=$(openssl rand -hex 32)

sed -i \
  -e "s|CHANGE_ME_POSTGRES_PASSWORD|$POSTGRES_PASSWORD|" \
  -e "s|CHANGE_ME_LIVEKIT_API_KEY|$LIVEKIT_API_KEY|" \
  -e "s|CHANGE_ME_LIVEKIT_API_SECRET|$LIVEKIT_API_SECRET|" \
  -e "s|CHANGE_ME_MINIO_ROOT_PASSWORD|$MINIO_ROOT_PASSWORD|" \
  -e "s|CHANGE_ME_MASTER_KEY|$MASTER_KEY|" \
  -e "s|CHANGE_ME_DISPATCH_KEY|$DISPATCH_KEY|" \
  -e "s|CHANGE_ME_PHONE_HASH_KEY|$PHONE_HASH_KEY|" \
  -e "s|CHANGE_ME_METRICS_TOKEN|$METRICS_TOKEN|" \
  "$ENV_FILE"

echo "Created server/.env with generated infrastructure secrets."
echo "Next: fill PUBLIC_IP, DASHSCOPE_API_KEY, DEEPSEEK_API_KEY and LIVEKIT_SIP_TRUNK_ID."
