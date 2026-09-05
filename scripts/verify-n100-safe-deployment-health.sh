#!/usr/bin/env bash
set -euo pipefail

readonly SAFE_SERVICES=(crawler-worker youtube-memo book-memo car-care-worker)
readonly MAX_ATTEMPTS="${N100_SAFE_DEPLOY_HEALTH_MAX_ATTEMPTS:-45}"
readonly INTERVAL_SECONDS="${N100_SAFE_DEPLOY_HEALTH_INTERVAL_SECONDS:-2}"

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

is_positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

is_nonnegative_integer() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

wait_for_service_health() {
  local service="$1"
  local attempt=1
  local health_status

  while (( attempt <= MAX_ATTEMPTS )); do
    health_status="$(docker inspect --format '{{.State.Health.Status}}' "$service" 2>/dev/null || true)"
    if [[ "$health_status" == 'healthy' ]] && curl --fail --silent --show-error --max-time 5 "$(health_url "$service")" >/dev/null; then
      return 0
    fi
    if (( attempt < MAX_ATTEMPTS )); then
      sleep "$INTERVAL_SECONDS"
    fi
    ((attempt++))
  done
  return 1
}

main() {
  local service

  [[ "$#" -gt 0 ]] || {
    printf '%s\n' 'safe_cd_health=FAIL reason=no_service' >&2
    return 1
  }
  is_positive_integer "$MAX_ATTEMPTS" && is_nonnegative_integer "$INTERVAL_SECONDS" || {
    printf '%s\n' 'safe_cd_health=FAIL reason=invalid_poll_config' >&2
    return 1
  }

  for service in "$@"; do
    is_safe_service "$service" || {
      printf '%s\n' 'safe_cd_health=FAIL reason=unsupported_service' >&2
      return 1
    }
  done

  for service in "$@"; do
    wait_for_service_health "$service" || {
      printf '%s\n' 'safe_cd_health=FAIL reason=health_timeout' >&2
      return 1
    }
  done

  printf '%s\n' 'safe_cd_health=PASS' >&2
}

main "$@"
