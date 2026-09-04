#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${1:-$(pwd)}"
cd "$PROJECT_ROOT"
source "$SCRIPT_DIR/runtime-service-state.sh"

runtime_state="$(load_service_runtime_state "$PROJECT_ROOT")" || exit 1
CRAWLER_WORKER_RUNTIME_MODE=compose
YOUTUBE_MEMO_RUNTIME_MODE=compose
BOOK_MEMO_RUNTIME_MODE=compose

runtime_service_mode() {
  if [ "$1" = crawler-worker ]; then printf '%s\n' "$CRAWLER_WORKER_RUNTIME_MODE"
  elif [ "$1" = youtube-memo ]; then printf '%s\n' "$YOUTUBE_MEMO_RUNTIME_MODE"
  elif [ "$1" = book-memo ]; then printf '%s\n' "$BOOK_MEMO_RUNTIME_MODE"
  else printf '%s\n' compose
  fi
}
while IFS='=' read -r service mode; do
  if [[ "$service" == crawler-worker || "$service" == youtube-memo || "$service" == book-memo ]]; then
    case "$service" in
      crawler-worker) CRAWLER_WORKER_RUNTIME_MODE="$mode" ;;
      youtube-memo) YOUTUBE_MEMO_RUNTIME_MODE="$mode" ;;
      book-memo) BOOK_MEMO_RUNTIME_MODE="$mode" ;;
    esac
  else
    echo "Invalid crawler runtime state" >&2
    exit 1
  fi
done <<< "$runtime_state"

compose() {
  docker compose -f docker-compose.yml -f docker-compose.n100.yml "$@"
}

portal_runtime_marker=data/portal-runtime.mode
portal_runtime_mode=compose
if [ -f "$portal_runtime_marker" ]; then
  portal_runtime_mode=$(tr -d '[:space:]' < "$portal_runtime_marker")
fi

for service in system-agent crawler-worker youtube-memo book-memo car-care-worker caddy homeops-executor; do
  if [[ "$(runtime_service_mode "$service")" == k3s ]]; then
    if compose ps --status running --services | grep -Fx -- "$service"; then
      echo "Compose writer is running during K3s mode: $service" >&2
      exit 1
    fi
    k3s kubectl -n "${K3S_NAMESPACE:-personal-server}" rollout status "deployment/$service" --timeout="${K3S_ROLLOUT_TIMEOUT:-120s}"
  else
    compose ps --status running --services | grep -Fx -- "$service"
  fi
done

case "$portal_runtime_mode" in
  compose)
    compose ps --status running --services | grep -Fx -- portal-web
    curl --fail --silent --show-error --retry-all --retry-connrefused --retry 6 --retry-delay 5 http://127.0.0.1:8000/health
    ;;
  k3s)
    if compose ps --status running --services | grep -Fx -- portal-web; then
      echo "Compose Portal is running during K3s mode" >&2
      exit 1
    fi
    curl --fail --silent --show-error --retry-all --retry-connrefused --retry 6 --retry-delay 5 http://127.0.0.1:30080/health
    ;;
  cutover)
    if compose ps --status running --services | grep -Fx -- portal-web; then
      echo "Compose Portal is running during cutover mode" >&2
      exit 1
    fi
    ;;
  *)
    echo "Invalid portal runtime marker" >&2
    exit 1
    ;;
esac

for url in \
  http://127.0.0.1:18010/health \
  http://127.0.0.1:8001/health \
  http://127.0.0.1:8002/health \
  http://127.0.0.1:8003/health \
  http://127.0.0.1:8015/health; do
  curl --fail --silent --show-error --retry-all --retry-connrefused --retry 6 --retry-delay 5 "$url"
done

for attempt in $(seq 1 18); do
  docker inspect --format '{{.State.Health.Status}}' homeops-executor | grep -Fx -- healthy && exit 0
  sleep 5
done

exit 1
