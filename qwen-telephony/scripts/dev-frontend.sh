#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FRONTEND="$ROOT/app"

cd "$FRONTEND"
if [[ ! -d node_modules ]]; then
  npm ci --ignore-scripts
fi
exec npm run dev
