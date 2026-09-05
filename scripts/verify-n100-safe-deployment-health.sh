#!/usr/bin/env bash
set -euo pipefail

readonly SAFE_SERVICES=(crawler-worker youtube-memo book-memo car-care-worker)

is_safe_service() {
  local candidate="$1"
  local service
  for service in "${SAFE_SERVICES[@]}"; do
    [[ "$candidate" == "$service" ]] && return 0
  done
  return 1
}

health_url() {
  case "$1" in
    crawler-worker) printf '%s\n' 'http://127.0.0.1:8001/health' ;;
    youtube-memo) printf '%s\n' 'http://127.0.0.1:8002/health' ;;
    book-memo) printf '%s\n' 'http://127.0.0.1:8003/health' ;;
    car-care-worker) printf '%s\n' 'http://127.0.0.1:8015/health' ;;
    *) return 1 ;;
  esac
}

main() {
  local service health_status

  [[ "$#" -gt 0 ]] || {
    printf '%s\n' 'safe_cd_health=FAIL reason=no_service' >&2
    return 1
  }

  for service in "$@"; do
    is_safe_service "$service" || {
      printf '%s\n' 'safe_cd_health=FAIL reason=unsupported_service' >&2
      return 1
    }
  done

  for service in "$@"; do
    health_status="$(docker inspect --format '{{.State.Health.Status}}' "$service")" || {
      printf '%s\n' 'safe_cd_health=FAIL reason=container_inspect' >&2
      return 1
    }
    [[ "$health_status" == 'healthy' ]] || {
      printf '%s\n' 'safe_cd_health=FAIL reason=container_unhealthy' >&2
      return 1
    }
    curl --fail --silent --show-error --max-time 5 "$(health_url "$service")" >/dev/null || {
      printf '%s\n' 'safe_cd_health=FAIL reason=loopback_health' >&2
      return 1
    }
  done

  printf '%s\n' 'safe_cd_health=PASS' >&2
}

main "$@"
