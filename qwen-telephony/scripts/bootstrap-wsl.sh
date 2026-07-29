#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP="$ROOT/qwen-telephony"
VENV="$APP/.venv"

select_python() {
  local candidate
  if [[ -n "${QWEN_PYTHON:-}" ]]; then
    candidate="$QWEN_PYTHON"
    command -v "$candidate" >/dev/null 2>&1 || {
      echo "QWEN_PYTHON does not exist: $candidate" >&2
      return 1
    }
    "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' || {
      echo "QWEN_PYTHON must be Python 3.11 or newer." >&2
      return 1
    }
    printf '%s\n' "$candidate"
    return
  fi

  for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 \
      && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
      printf '%s\n' "$candidate"
      return
    fi
  done

  echo "Python 3.11+ is required (Python 3.12 is recommended)." >&2
  echo "Install it or set QWEN_PYTHON to a compatible interpreter." >&2
  return 1
}

cd "$APP"

PYTHON_BIN="$(select_python)"
echo "Using $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"
"$PYTHON_BIN" -m venv "$VENV"
source "$VENV/bin/activate"
python -m pip install --upgrade pip
python -m pip install -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com -r requirements-dev.txt
python -m pip check

if [[ ! -f "$APP/config/local.env" ]]; then
  cp "$APP/config/local.env.example" "$APP/config/local.env"
fi

echo "Bootstrap complete: $VENV"
