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
    namespace="${K3S_NAMESPACE:-personal-server}"
    desired=$(sudo k3s kubectl -n "$namespace" get "deployment/$service" -o jsonpath='{.spec.replicas}')
    ready=$(sudo k3s kubectl -n "$namespace" get "deployment/$service" -o jsonpath='{.status.readyReplicas}')
    available=$(sudo k3s kubectl -n "$namespace" get "deployment/$service" -o jsonpath='{.status.availableReplicas}')
    [[ "$desired" =~ ^[0-9]+$ && "$desired" -ge 1 && "$ready" == "$desired" && "$available" == "$desired" ]] || {
      echo "K3s Deployment is not ready: $service" >&2
      exit 1
    }
    sudo k3s kubectl -n "$namespace" rollout status "deployment/$service" --timeout="${K3S_ROLLOUT_TIMEOUT:-120s}"
    if [ "$service" = book-memo ] || [ "$service" = youtube-memo ] || [ "$service" = crawler-worker ]; then
      case "$service" in
        crawler-worker) expected_port=8001; pvc_name=crawler-worker-data ;;
        book-memo) expected_port=8003; pvc_name=book-memo-data ;;
        youtube-memo) expected_port=8002; pvc_name=youtube-memo-data ;;
      esac
      service_selector=$(sudo k3s kubectl -n "$namespace" get "service/$service" -o jsonpath='{.spec.selector.app\.kubernetes\.io/name}')
      service_cluster_ip=$(sudo k3s kubectl -n "$namespace" get "service/$service" -o jsonpath='{.spec.clusterIP}')
      service_port=$(sudo k3s kubectl -n "$namespace" get "service/$service" -o jsonpath='{.spec.ports[0].port}')
      service_target_port=$(sudo k3s kubectl -n "$namespace" get "service/$service" -o jsonpath='{.spec.ports[0].targetPort}')
      endpoint_addresses=$(sudo k3s kubectl -n "$namespace" get "endpoints/$service" -o jsonpath='{.subsets[*].addresses[*].ip}')
      endpoint_ports=$(sudo k3s kubectl -n "$namespace" get "endpoints/$service" -o jsonpath='{.subsets[*].ports[*].port}')
      pvc_phase=$(sudo k3s kubectl -n "$namespace" get "pvc/$pvc_name" -o jsonpath='{.status.phase}')
      [[ "$service_selector" == "$service" && -n "$service_cluster_ip" && "$service_cluster_ip" != None && "$service_port" == "$expected_port" && "$service_target_port" == http && -n "$endpoint_addresses" && "$endpoint_ports" == "$expected_port" && "$pvc_phase" == Bound ]] || {
        echo "K3s Service endpoint is not ready: $service" >&2
        exit 1
      }
    fi
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
  http://127.0.0.1:18010/health; do
  curl --fail --silent --show-error --retry-all --retry-connrefused --retry 6 --retry-delay 5 "$url"
done

if [[ "$CRAWLER_WORKER_RUNTIME_MODE" != k3s ]]; then
  curl --fail --silent --show-error --retry-all --retry-connrefused --retry 6 --retry-delay 5 http://127.0.0.1:8001/health
fi

if [[ "$YOUTUBE_MEMO_RUNTIME_MODE" != k3s ]]; then
  curl --fail --silent --show-error --retry-all --retry-connrefused --retry 6 --retry-delay 5 http://127.0.0.1:8002/health
fi

if [[ "$BOOK_MEMO_RUNTIME_MODE" != k3s ]]; then
  curl --fail --silent --show-error --retry-all --retry-connrefused --retry 6 --retry-delay 5 http://127.0.0.1:8003/health
fi

for url in \
  http://127.0.0.1:8015/health; do
  curl --fail --silent --show-error --retry-all --retry-connrefused --retry 6 --retry-delay 5 "$url"
done

for attempt in $(seq 1 18); do
  docker inspect --format '{{.State.Health.Status}}' homeops-executor | grep -Fx -- healthy && exit 0
  sleep 5
done

exit 1
