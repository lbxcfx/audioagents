#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP="$ROOT/qwen-telephony"

"$APP/scripts/start-infra-wsl.sh"
"$APP/scripts/init-sip-wsl.sh"
echo "Start the agent in another WSL terminal:"
echo "  $APP/scripts/start-agent-wsl.sh"
