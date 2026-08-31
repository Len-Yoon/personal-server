#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${1:-$(pwd)}"
cd "$PROJECT_ROOT"

compose() {
  docker compose -f docker-compose.yml -f docker-compose.n100.yml "$@"
}

portal_runtime_marker=data/portal-runtime.mode
portal_runtime_mode=compose
if [ -f "$portal_runtime_marker" ]; then
  portal_runtime_mode=$(tr -d '[:space:]' < "$portal_runtime_marker")
fi

for service in system-agent crawler-worker youtube-memo book-memo car-care-worker caddy homeops-executor; do
  compose ps --status running --services | grep -Fx -- "$service"
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
