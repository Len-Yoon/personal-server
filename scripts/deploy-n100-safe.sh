#!/usr/bin/env bash
set -euo pipefail
umask 077

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="${N100_SAFE_DEPLOY_PROJECT_ROOT:-$(pwd)}"
readonly STATE_DIR="${N100_SAFE_DEPLOY_STATE_DIR:-$HOME/.local/state/personal-server/n100-safe-deploy}"
readonly STATE_FILE="$STATE_DIR/last-healthy-revision"
readonly SAFE_SERVICES=(crawler-worker youtube-memo book-memo car-care-worker)
readonly HEALTH_SCRIPT="$SCRIPT_DIR/verify-n100-safe-deployment-health.sh"

is_revision() {
  [[ "$1" =~ ^[0-9a-f]{40}$ ]]
}

is_safe_service() {
  local candidate="$1"
  local service
  for service in "${SAFE_SERVICES[@]}"; do
    [[ "$candidate" == "$service" ]] && return 0
  done
  return 1
}

validate_services() {
  local service
  [[ "$#" -gt 0 ]] || return 1
  for service in "$@"; do
    is_safe_service "$service" || return 1
  done
}

deploy_revision() {
  local revision="$1"
  shift

  printf '%s\n' 'safe_cd_stage=deploy' >&2
  git checkout --detach "$revision" || return 1
  docker compose -f docker-compose.yml -f docker-compose.n100.yml config --quiet || return 1
  docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build --no-deps "$@" || return 1
}

health_check() {
  printf '%s\n' 'safe_cd_stage=health' >&2
  "$HEALTH_SCRIPT" "$@"
}

record_healthy_revision() {
  mkdir -p "$STATE_DIR"
  [[ ! -L "$STATE_FILE" ]] || return 1
  printf '%s\n' "$1" > "$STATE_FILE"
}

main() {
  local expected_sha="$1"
  shift
  local previous_sha

  printf '%s\n' 'safe_cd_stage=preflight' >&2
  is_revision "$expected_sha" || return 1
  validate_services "$@" || return 1
  [[ -d "$PROJECT_ROOT/.git" ]] || return 1
  [[ -x "$HEALTH_SCRIPT" ]] || return 1
  command -v git >/dev/null
  command -v docker >/dev/null
  command -v curl >/dev/null

  cd "$PROJECT_ROOT"
  git fetch --prune origin
  git merge-base --is-ancestor "$expected_sha" origin/main

  if deploy_revision "$expected_sha" "$@" && health_check "$@"; then
    record_healthy_revision "$expected_sha"
    return 0
  fi

  [[ -f "$STATE_FILE" ]] || return 1
  previous_sha="$(<"$STATE_FILE")"
  is_revision "$previous_sha" || return 1
  [[ "$previous_sha" != "$expected_sha" ]] || return 1
  git merge-base --is-ancestor "$previous_sha" origin/main

  printf '%s\n' 'safe_cd_stage=rollback' >&2
  if deploy_revision "$previous_sha" "$@" && health_check "$@"; then
    return 0
  fi
  return 1
}

[[ "$#" -ge 2 ]] || {
  printf '%s\n' 'safe_cd_stage=preflight' >&2
  exit 1
}

main "$@"
