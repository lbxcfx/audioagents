#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOG_DIR="$ROOT/qwen-telephony/logs"
mkdir -p "$LOG_DIR"

pgrep -f "python -u phone_agent.py start" | xargs -r kill >/dev/null 2>&1 || true
pgrep -f "python -u phone_agent.py dev" | xargs -r kill >/dev/null 2>&1 || true
pgrep -f "multiprocessing.forkserver.*livekit.plugins" | xargs -r kill >/dev/null 2>&1 || true
nohup "$ROOT/qwen-telephony/scripts/start-agent-wsl.sh" > "$LOG_DIR/agent.log" 2>&1 &
echo "$!" > "$LOG_DIR/agent.pid"
echo "Agent PID: $!"
