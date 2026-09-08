#!/usr/bin/env bash
set -euo pipefail
umask 077

PROJECT_ROOT="${N100_SAFE_DEPLOY_PROJECT_ROOT:-$(pwd -P)}"
PROJECT_ROOT="$(cd -- "$PROJECT_ROOT" && pwd -P)"
readonly PROJECT_ROOT
readonly STATE_DIR="${N100_SAFE_DEPLOY_STATE_DIR:-$HOME/.local/state/personal-server/n100-safe-deploy}"
readonly STATE_FILE="$STATE_DIR/last-healthy-revision"
readonly RELEASES_DIR="$STATE_DIR/releases"
readonly OVERRIDES_DIR="$STATE_DIR/overrides"
readonly SAFE_SERVICES=(crawler-worker youtube-memo book-memo car-care-worker)
readonly SERVICE_CSV_PATTERN='^(crawler-worker|youtube-memo|book-memo|car-care-worker)(,(crawler-worker|youtube-memo|book-memo|car-care-worker))*$'
PARSED_SERVICES=()
COMPOSE_OVERRIDE=''
COMPOSE_OVERRIDES=()
HEALTH_SCRIPT=''
HEALTH_SCRIPTS=()
OWNERSHIP_MUTATED_SERVICES=()
OWNERSHIP_SNAPSHOTS=()
OWNERSHIP_SNAPSHOTS_VERIFIED=0
PAUSED_SERVICES=()
TARGET_SERVICES_STARTED=0

cleanup_generated_resources() {
  local override health_script snapshot
  for override in "${COMPOSE_OVERRIDES[@]:-}"; do
    if [[ -f "$override" && ! -L "$override" ]]; then
      unlink -- "$override"
    fi
  done
  for health_script in "${HEALTH_SCRIPTS[@]:-}"; do
    if [[ -f "$health_script" && ! -L "$health_script" ]]; then
      unlink -- "$health_script"
    fi
  done
  if [[ "$OWNERSHIP_SNAPSHOTS_VERIFIED" -eq 1 ]]; then
    for snapshot in "${OWNERSHIP_SNAPSHOTS[@]:-}"; do
      if [[ -f "$snapshot" && ! -L "$snapshot" ]]; then
        unlink -- "$snapshot"
      fi
    done
  fi
}

trap cleanup_generated_resources EXIT INT TERM HUP

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

data_directory_for_service() {
  case "$1" in
    crawler-worker) printf '%s/data/crawler-worker\n' "$PROJECT_ROOT" ;;
    youtube-memo) printf '%s/data/youtube-memo\n' "$PROJECT_ROOT" ;;
    book-memo) printf '%s/data/book-memo\n' "$PROJECT_ROOT" ;;
    *) return 1 ;;
  esac
}

run_data_ownership_helper() {
  local service="$1"
  local data_directory="$2"
  shift 2

  docker run --rm \
    --network none \
    --read-only \
    --cap-drop ALL \
    --cap-add CHOWN \
    --cap-add FOWNER \
    --cap-add DAC_OVERRIDE \
    --user 0:0 \
    --mount "type=bind,src=$data_directory,dst=/data" \
    "personal-server-$service:latest" \
    "$@"
}

run_data_ownership_inspector() {
  local service="$1"
  local data_directory="$2"
  local user="$3"
  shift 3

  docker run --rm \
    --network none \
    --read-only \
    --cap-drop ALL \
    --user "$user" \
    --mount "type=bind,src=$data_directory,dst=/data" \
    "personal-server-$service:latest" \
    "$@"
}

data_is_owned_by() {
  local service="$1"
  local data_directory="$2"
  local uid="$3"
  local gid="$4"

  run_data_ownership_inspector "$service" "$data_directory" "$uid:$gid" \
    /bin/sh -ec "first=\"\$(find /data -xdev \\( ! -uid $uid -o ! -gid $gid \\) -print -quit)\" || exit \$?; test -z \"\$first\""
}

capture_data_ownership() {
  local service="$1"
  local data_directory="$2"
  local snapshot

  snapshot="$(mktemp "$STATE_DIR/ownership.XXXXXX")" || return 1
  chmod 600 "$snapshot"
  run_data_ownership_helper "$service" "$data_directory" python -c '
import os
import sys

root = "/data"
output = sys.stdout.buffer

def emit(path):
    metadata = os.lstat(path)
    relative = os.path.relpath(path, root)
    if relative == ".":
        relative = ""
    output.write(relative.encode("utf-8", "surrogateescape"))
    output.write(b"\0")
    output.write(str(metadata.st_uid).encode("ascii"))
    output.write(b"\0")
    output.write(str(metadata.st_gid).encode("ascii"))
    output.write(b"\0")

def raise_walk_error(error):
    raise error

emit(root)
for parent, directories, filenames in os.walk(root, topdown=True, followlinks=False, onerror=raise_walk_error):
    for name in directories + filenames:
        emit(os.path.join(parent, name))
' > "$snapshot" || {
    unlink -- "$snapshot"
    return 1
  }
  [[ -s "$snapshot" && ! -L "$snapshot" ]] || {
    unlink -- "$snapshot"
    return 1
  }
  OWNERSHIP_SNAPSHOTS+=("$snapshot")
}

restore_data_ownership_snapshot() {
  local service="$1"
  local data_directory="$2"
  local snapshot="$3"

  [[ -f "$snapshot" && ! -L "$snapshot" ]] || return 1
  docker run --rm \
    --network none \
    --read-only \
    --cap-drop ALL \
    --cap-add CHOWN \
    --cap-add FOWNER \
    --cap-add DAC_OVERRIDE \
    --user 0:0 \
    --mount "type=bind,src=$data_directory,dst=/data" \
    --mount "type=bind,src=$snapshot,dst=/state/ownership,readonly" \
    "personal-server-$service:latest" \
    python -c '
import os

root = "/data"
fields = open("/state/ownership", "rb").read().split(b"\0")
if not fields or fields[-1] != b"" or (len(fields) - 1) % 3:
    raise SystemExit(1)

for offset in range(0, len(fields) - 1, 3):
    relative = fields[offset].decode("utf-8", "surrogateescape")
    uid = fields[offset + 1]
    gid = fields[offset + 2]
    if not uid.isdigit() or not gid.isdigit():
        raise SystemExit(1)
    if relative:
        if relative.startswith("/") or any(part in ("", ".", "..") for part in relative.split("/")):
            raise SystemExit(1)
        target = os.path.join(root, relative)
    else:
        target = root
    try:
        os.lchown(target, int(uid), int(gid))
    except FileNotFoundError:
        continue
' || return 1
}

pause_service_writers() {
  local service
  local running

  printf '%s\n' 'safe_cd_stage=writer_pause' >&2
  for service in "$@"; do
    running="$(docker inspect --format '{{.State.Running}}' "$service")" || return 1
    [[ "$running" == true ]] || continue
    docker stop "$service" >/dev/null || return 1
    PAUSED_SERVICES+=("$service")
  done
}

resume_paused_service_writers() {
  local service

  [[ "${#PAUSED_SERVICES[@]}" -gt 0 ]] || return 0
  printf '%s\n' 'safe_cd_stage=writer_resume' >&2
  for service in "${PAUSED_SERVICES[@]}"; do
    docker start "$service" >/dev/null || return 1
  done
}

stop_target_service_writers() {
  local service

  [[ "$TARGET_SERVICES_STARTED" -eq 1 ]] || return 0
  printf '%s\n' 'safe_cd_stage=writer_stop' >&2
  for service in "$@"; do
    docker stop "$service" >/dev/null || return 1
  done
}

align_service_data_ownership() {
  local service
  local data_directory

  printf '%s\n' 'safe_cd_stage=data_ownership' >&2
  [[ -d "$PROJECT_ROOT/data" && ! -L "$PROJECT_ROOT/data" ]] || return 1
  for service in "$@"; do
    data_directory="$(data_directory_for_service "$service")" || continue
    [[ -d "$data_directory" && ! -L "$data_directory" ]] || return 1
    if data_is_owned_by "$service" "$data_directory" 10001 10001; then
      continue
    fi
    capture_data_ownership "$service" "$data_directory" || return 1
    OWNERSHIP_MUTATED_SERVICES+=("$service")
    run_data_ownership_helper "$service" "$data_directory" \
      chown --recursive --no-dereference 10001:10001 /data || return 1
  done
}

restore_root_owned_data_for_rollback() {
  local service
  local data_directory
  local snapshot
  local index

  [[ "${#OWNERSHIP_MUTATED_SERVICES[@]}" -gt 0 ]] || return 0
  printf '%s\n' 'safe_cd_stage=data_ownership_restore' >&2
  for ((index = 0; index < ${#OWNERSHIP_MUTATED_SERVICES[@]}; index++)); do
    service="${OWNERSHIP_MUTATED_SERVICES[$index]}"
    snapshot="${OWNERSHIP_SNAPSHOTS[$index]}"
    data_directory="$(data_directory_for_service "$service")" || return 1
    [[ -d "$data_directory" && ! -L "$data_directory" ]] || return 1
    restore_data_ownership_snapshot "$service" "$data_directory" "$snapshot" || return 1
  done
  OWNERSHIP_SNAPSHOTS_VERIFIED=1
}

deploy_revision() {
  local revision="$1"
  local align_data_ownership="$2"
  shift 2

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
    -f "$COMPOSE_OVERRIDE" build "$@" || return 1
  if [[ "$align_data_ownership" == true ]]; then
    pause_service_writers "$@" || return 1
    align_service_data_ownership "$@" || return 1
    TARGET_SERVICES_STARTED=1
  fi
  docker compose \
    -f "$PROJECT_ROOT/docker-compose.yml" \
    -f "$PROJECT_ROOT/docker-compose.n100.yml" \
    -f "$COMPOSE_OVERRIDE" up -d --no-build --no-deps "$@"
}

prepare_health_script() {
  local revision="$1"

  HEALTH_SCRIPT="$(mktemp "$STATE_DIR/verify-health.XXXXXX")" || return 1
  HEALTH_SCRIPTS+=("$HEALTH_SCRIPT")
  git show "$revision:scripts/verify-n100-safe-deployment-health.sh" > "$HEALTH_SCRIPT" || return 1
  chmod 700 "$HEALTH_SCRIPT"
}

health_check() {
  local revision="$1"
  shift

  printf '%s\n' 'safe_cd_stage=health' >&2
  prepare_health_script "$revision" || return 1
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

  if deploy_revision "$expected_sha" true "${PARSED_SERVICES[@]}" && health_check "$expected_sha" "${PARSED_SERVICES[@]}"; then
    record_healthy_revision "$expected_sha"
    OWNERSHIP_SNAPSHOTS_VERIFIED=1
    return 0
  fi

  stop_target_service_writers "${PARSED_SERVICES[@]}" || return 1
  restore_root_owned_data_for_rollback || return 1
  if [[ "$TARGET_SERVICES_STARTED" -eq 0 ]]; then
    resume_paused_service_writers || return 1
    return 1
  fi
  [[ -f "$STATE_FILE" ]] || return 1
  previous_sha="$(<"$STATE_FILE")"
  is_revision "$previous_sha" || return 1
  [[ "$previous_sha" != "$expected_sha" ]] || return 1
  git merge-base --is-ancestor "$previous_sha" origin/main

  printf '%s\n' 'safe_cd_stage=rollback' >&2
  if deploy_revision "$previous_sha" false "${PARSED_SERVICES[@]}" && health_check "$previous_sha" "${PARSED_SERVICES[@]}"; then
    return 0
  fi
  return 1
}

[[ "$#" -eq 2 ]] || {
  printf '%s\n' 'safe_cd_stage=preflight' >&2
  exit 1
}

main "$@"
