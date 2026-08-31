#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${1:-$(pwd)}"

if [[ ! -d "$PROJECT_ROOT" ]]; then
  echo "배포 디렉터리가 없습니다: $PROJECT_ROOT" >&2
  exit 1
fi

PROJECT_ROOT="$(cd "$PROJECT_ROOT" && pwd)"

test -d "$PROJECT_ROOT/.git" || { echo "Git 저장소가 아닙니다: $PROJECT_ROOT" >&2; exit 1; }
test -f "$PROJECT_ROOT/.env" || { echo ".env가 없습니다: $PROJECT_ROOT/.env" >&2; exit 1; }
test -d "$PROJECT_ROOT/data" || { echo "data 디렉터리가 없습니다: $PROJECT_ROOT/data" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "docker 명령을 찾을 수 없습니다." >&2; exit 1; }

cd "$PROJECT_ROOT"

DOCKER_WAIT_ATTEMPTS="${DOCKER_WAIT_ATTEMPTS:-18}"
DOCKER_WAIT_SECONDS="${DOCKER_WAIT_SECONDS:-10}"

wait_for_docker() {
  local attempt
  for ((attempt = 1; attempt <= DOCKER_WAIT_ATTEMPTS; attempt++)); do
    if docker info >/dev/null 2>&1; then
      return 0
    fi
    echo "Docker daemon is not ready; waiting ${DOCKER_WAIT_SECONDS}s (attempt ${attempt}/${DOCKER_WAIT_ATTEMPTS})" >&2
    sleep "$DOCKER_WAIT_SECONDS"
  done

  echo "Docker daemon did not become ready after ${DOCKER_WAIT_ATTEMPTS} attempts" >&2
  return 1
}

require_portal_state_ready() {
  local state_database="$PROJECT_ROOT/data/portal-web-state/homeops.sqlite3"
  if [ ! -f "$state_database" ] || ! python3 -c 'import sqlite3,sys; connection=sqlite3.connect(sys.argv[1]); result=connection.execute("PRAGMA quick_check").fetchone()[0]; connection.close(); sys.exit(result != "ok")' "$state_database" >/dev/null 2>&1; then
    echo "Portal state migration is required before Compose Portal can start" >&2
    return 1
  fi
}

PORTAL_RUNTIME_MARKER="${PORTAL_RUNTIME_MARKER:-$PROJECT_ROOT/data/portal-runtime.mode}"
PORTAL_RUNTIME_MODE="compose"
PORTAL_BRIDGE_COMPOSE_FILE="${PORTAL_BRIDGE_COMPOSE_FILE:-$PROJECT_ROOT/docker-compose.portal-bridge.yml}"

load_portal_runtime_mode() {
  local mode
  if [ ! -f "$PORTAL_RUNTIME_MARKER" ]; then
    return 0
  fi
  mode=$(tr -d '[:space:]' < "$PORTAL_RUNTIME_MARKER")
  case "$mode" in
    compose|cutover|k3s) PORTAL_RUNTIME_MODE="$mode" ;;
    *)
      echo "Invalid portal runtime marker; refusing to select a Portal writer" >&2
      return 1
      ;;
  esac
}

validate_docker_bridge_gateway() {
  local actual_gateway
  [ -f "$PORTAL_BRIDGE_COMPOSE_FILE" ] || {
    echo "Portal bridge Compose override is missing: $PORTAL_BRIDGE_COMPOSE_FILE" >&2
    return 1
  }
  actual_gateway=$(docker network inspect bridge --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}') || return 1
  python3 -c 'import ipaddress,sys; ipaddress.IPv4Address(sys.argv[1])' "$actual_gateway" >/dev/null 2>&1 || {
    echo "Docker bridge gateway is not a valid IPv4 address" >&2
    return 1
  }
  if [ -n "${DOCKER_BRIDGE_GATEWAY:-}" ] && [ "$DOCKER_BRIDGE_GATEWAY" != "$actual_gateway" ]; then
    echo "Configured Docker bridge gateway does not match the active bridge network" >&2
    return 1
  fi
  export DOCKER_BRIDGE_GATEWAY="$actual_gateway"
}

deploy_runtime_services() {
  local bridge_compose=(docker compose -f docker-compose.yml -f docker-compose.n100.yml -f "$PORTAL_BRIDGE_COMPOSE_FILE")
  local bridge_services="homeops-executor system-agent crawler-worker youtube-memo book-memo car-care-worker"

  case "$PORTAL_RUNTIME_MODE" in
    compose)
      require_portal_state_ready
      docker compose -f docker-compose.yml -f docker-compose.n100.yml config --quiet
      docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build portal-web homeops-executor system-agent crawler-worker youtube-memo book-memo car-care-worker caddy
      ;;
    cutover|k3s)
      validate_docker_bridge_gateway
      export HOMEOPS_DOCKER_MANAGED_SERVICES="system-agent,crawler-worker,youtube-memo,book-memo,caddy,homeops-executor"
      export EXPECTED_CONTAINERS="crawler-worker,youtube-memo,book-memo,system-agent"
      "${bridge_compose[@]}" config --quiet
      "${bridge_compose[@]}" up -d --build --no-deps --force-recreate $bridge_services
      "${bridge_compose[@]}" up -d --no-deps caddy
      ;;
  esac
}

wait_for_docker
docker compose version >/dev/null

git fetch --prune origin
git reset --hard origin/main
load_portal_runtime_mode
deploy_runtime_services
docker compose -f docker-compose.yml -f docker-compose.n100.yml ps
