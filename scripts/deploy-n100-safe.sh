#!/usr/bin/env bash
set -euo pipefail
umask 077

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="${N100_SAFE_DEPLOY_PROJECT_ROOT:-$(pwd)}"
readonly STATE_DIR="${N100_SAFE_DEPLOY_STATE_DIR:-$HOME/.local/state/personal-server/n100-safe-deploy}"
readonly STATE_FILE="$STATE_DIR/last-healthy-revision"
readonly RELEASES_DIR="$STATE_DIR/releases"
readonly OVERRIDES_DIR="$STATE_DIR/overrides"
readonly SAFE_SERVICES=(crawler-worker youtube-memo book-memo car-care-worker)
readonly HEALTH_SCRIPT="$SCRIPT_DIR/verify-n100-safe-deployment-health.sh"
readonly SERVICE_CSV_PATTERN='^(crawler-worker|youtube-memo|book-memo|car-care-worker)(,(crawler-worker|youtube-memo|book-memo|car-care-worker))*$'
PARSED_SERVICES=()
COMPOSE_OVERRIDE=''
COMPOSE_OVERRIDES=()

cleanup_generated_overrides() {
  local override
  for override in "${COMPOSE_OVERRIDES[@]}"; do
    if [[ -f "$override" && ! -L "$override" ]]; then
      unlink -- "$override"
    fi
  done
}

trap cleanup_generated_overrides EXIT INT TERM HUP

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
  local seen_services='|'

  [[ "$#" -gt 0 ]] || return 1
  for service in "$@"; do
    is_safe_service "$service" || return 1
    [[ "$seen_services" != *"|$service|"* ]] || return 1
    seen_services+="$service|"
  done
}

parse_services_csv() {
  local services_csv="$1"

  [[ "$services_csv" =~ $SERVICE_CSV_PATTERN ]] || return 1
  IFS=',' read -r -a PARSED_SERVICES <<< "$services_csv"
  validate_services "${PARSED_SERVICES[@]}"
}

yaml_quote() {
  local value="${1//\'/\'\'}"
  printf "'%s'" "$value"
}

prepare_state_directories() {
  mkdir -p -m 700 "$RELEASES_DIR" "$OVERRIDES_DIR"
  [[ ! -L "$STATE_DIR" && ! -L "$RELEASES_DIR" && ! -L "$OVERRIDES_DIR" ]]
}

create_release_source() {
  local revision="$1"
  shift
  local service
  local -a archive_paths=()

  RELEASE_SOURCE="$(mktemp -d "$RELEASES_DIR/${revision}.XXXXXX")" || return 1
  for service in "$@"; do
    archive_paths+=("$service/Dockerfile" "$service/requirements.txt" "$service/app")
  done
  git archive --format=tar "$revision" -- "${archive_paths[@]}" | tar -xf - -C "$RELEASE_SOURCE"
}

create_compose_override() {
  local service
  local service_source
  local quoted_source

  COMPOSE_OVERRIDE="$(mktemp "$OVERRIDES_DIR/compose.XXXXXX")" || return 1
  COMPOSE_OVERRIDES+=("$COMPOSE_OVERRIDE")
  {
    printf '%s\n' 'services:'
    for service in "$@"; do
      service_source="$RELEASE_SOURCE/$service"
      quoted_source="$(yaml_quote "$service_source")"
      printf '  %s:\n' "$service"
      printf '    build:\n'
      printf '      context: %s\n' "$quoted_source"
      printf '%s\n' '      dockerfile: Dockerfile'
      printf '%s\n' '    volumes:'
      printf '      - %s\n' "$(yaml_quote "$service_source/app:/app:ro")"
    done
  } > "$COMPOSE_OVERRIDE"
}

deploy_revision() {
  local revision="$1"
  shift

  printf '%s\n' 'safe_cd_stage=deploy' >&2
  create_release_source "$revision" "$@" || return 1
  create_compose_override "$@" || return 1
  docker compose \
    -f "$PROJECT_ROOT/docker-compose.yml" \
    -f "$PROJECT_ROOT/docker-compose.n100.yml" \
    -f "$COMPOSE_OVERRIDE" config --quiet || return 1
  docker compose \
    -f "$PROJECT_ROOT/docker-compose.yml" \
    -f "$PROJECT_ROOT/docker-compose.n100.yml" \
    -f "$COMPOSE_OVERRIDE" up -d --build --no-deps "$@"
}

health_check() {
  printf '%s\n' 'safe_cd_stage=health' >&2
  "$HEALTH_SCRIPT" "$@"
}

record_healthy_revision() {
  local state_temp

  [[ ! -L "$STATE_FILE" ]] || return 1
  state_temp="$(mktemp "$STATE_DIR/.last-healthy-revision.XXXXXX")" || return 1
  printf '%s\n' "$1" > "$state_temp"
  mv -f -- "$state_temp" "$STATE_FILE"
}

main() {
  local expected_sha="$1"
  local previous_sha
  local origin_main_sha

  printf '%s\n' 'safe_cd_stage=preflight' >&2
  is_revision "$expected_sha" || return 1
  parse_services_csv "$2" || return 1
  [[ -d "$PROJECT_ROOT/.git" ]] || return 1
  [[ -f "$PROJECT_ROOT/docker-compose.yml" ]] || return 1
  [[ -f "$PROJECT_ROOT/docker-compose.n100.yml" ]] || return 1
  [[ -f "$PROJECT_ROOT/.env" ]] || return 1
  [[ -d "$PROJECT_ROOT/data" ]] || return 1
  [[ -x "$HEALTH_SCRIPT" ]] || return 1
  command -v git >/dev/null
  command -v docker >/dev/null
  command -v curl >/dev/null
  command -v tar >/dev/null
  command -v mktemp >/dev/null
  command -v unlink >/dev/null

  cd "$PROJECT_ROOT"
  git fetch --prune origin
  git merge-base --is-ancestor "$expected_sha" origin/main
  origin_main_sha="$(git rev-parse origin/main)"
  [[ "$expected_sha" == "$origin_main_sha" ]] || return 1
  prepare_state_directories || return 1

  if deploy_revision "$expected_sha" "${PARSED_SERVICES[@]}" && health_check "${PARSED_SERVICES[@]}"; then
    record_healthy_revision "$expected_sha"
    return 0
  fi

  [[ -f "$STATE_FILE" ]] || return 1
  previous_sha="$(<"$STATE_FILE")"
  is_revision "$previous_sha" || return 1
  [[ "$previous_sha" != "$expected_sha" ]] || return 1
  git merge-base --is-ancestor "$previous_sha" origin/main

  printf '%s\n' 'safe_cd_stage=rollback' >&2
  if deploy_revision "$previous_sha" "${PARSED_SERVICES[@]}" && health_check "${PARSED_SERVICES[@]}"; then
    return 0
  fi
  return 1
}

[[ "$#" -eq 2 ]] || {
  printf '%s\n' 'safe_cd_stage=preflight' >&2
  exit 1
}

main "$@"
