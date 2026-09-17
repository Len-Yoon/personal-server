#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_STATE_HELPER="$SCRIPT_DIR/runtime-service-state.sh"
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

load_runtime_service_modes() {
  local state
  local service
  local mode
  local crawler_worker_seen=0
  local youtube_memo_seen=0
  local book_memo_seen=0

  [[ -f "$RUNTIME_STATE_HELPER" && ! -L "$RUNTIME_STATE_HELPER" ]] || return 1
  # shellcheck source=runtime-service-state.sh
  source "$RUNTIME_STATE_HELPER"
  state="$(load_service_runtime_state "$PWD")" || return 1
  while IFS='=' read -r service mode; do
    case "$service:$mode" in
      book-memo:compose|book-memo:k3s) [[ "$book_memo_seen" -eq 0 ]] || return 1; BOOK_MEMO_RUNTIME_MODE="$mode"; book_memo_seen=1 ;;
      crawler-worker:compose|crawler-worker:k3s) [[ "$crawler_worker_seen" -eq 0 ]] || return 1; crawler_worker_seen=1 ;;
      youtube-memo:compose|youtube-memo:k3s) [[ "$youtube_memo_seen" -eq 0 ]] || return 1; youtube_memo_seen=1 ;;
      *) return 1 ;;
    esac
  done <<< "$state"
  [[ "$crawler_worker_seen" -eq 1 && "$youtube_memo_seen" -eq 1 && "$book_memo_seen" -eq 1 ]]
}

service_uses_k3s() {
  [[ "$1" == book-memo && "$BOOK_MEMO_RUNTIME_MODE" == k3s ]]
}

report_health_diagnostic() {
  local service="$1"

  printf 'safe_cd_health_diagnostic service=%s\n' "$service" >&2
  docker inspect --format 'state={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} exit={{.State.ExitCode}}' "$service" >&2 2>/dev/null || true
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
  load_runtime_service_modes || {
    printf '%s\n' 'safe_cd_health=FAIL reason=runtime_state' >&2
    return 1
  }

  for service in "$@"; do
    is_safe_service "$service" || {
      printf '%s\n' 'safe_cd_health=FAIL reason=unsupported_service' >&2
      return 1
    }
    if service_uses_k3s "$service"; then
      printf '%s\n' 'safe_cd_health=FAIL reason=k3s_runtime_service' >&2
      return 1
    fi
  done

  for service in "$@"; do
    wait_for_service_health "$service" || {
      report_health_diagnostic "$service"
      printf '%s\n' 'safe_cd_health=FAIL reason=health_timeout' >&2
      return 1
    }
  done

  printf '%s\n' 'safe_cd_health=PASS' >&2
}

main "$@"
