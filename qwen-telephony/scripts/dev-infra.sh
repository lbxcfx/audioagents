#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/dev-common.sh"
ensure_dev_env
load_dev_env

action="${1:-up}"
case "$action" in
  up)
    docker info >/dev/null
    compose_dev up -d --wait
    compose_dev ps
    ;;
  down)
    compose_dev down
    ;;
  reset)
    echo "Refusing to delete development data without an explicit confirmation flag." >&2
    [[ "${2:-}" == "--delete-data" ]] || exit 2
    compose_dev down --volumes
    ;;
  status|ps)
    compose_dev ps
    ;;
  logs)
    shift
    compose_dev logs --tail=200 -f "$@"
    ;;
  *)
    echo "Usage: $0 {up|down|status|logs [service]|reset --delete-data}" >&2
    exit 2
    ;;
esac
