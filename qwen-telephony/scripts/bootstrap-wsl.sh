#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP="$ROOT/qwen-telephony"
VENV="$APP/.venv"

cd "$APP"

python3 -m venv "$VENV"
source "$VENV/bin/activate"
python -m pip install --upgrade pip
python -m pip install -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com -r requirements-dev.txt
python -m pip check

if [[ ! -f "$APP/config/local.env" ]]; then
  cp "$APP/config/local.env.example" "$APP/config/local.env"
fi

echo "Bootstrap complete: $VENV"
