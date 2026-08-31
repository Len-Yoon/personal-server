#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${1:-/mnt/c/personal-server}"
cd "$PROJECT_ROOT"
{
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] bootstrap start"
  echo "pwd=$(pwd)"
  echo "cloudflared_path=$(command -v cloudflared || true)"
  echo "config_exists=$([ -f ~/.cloudflared/config.yml ] && echo yes || echo no)"
} >> /tmp/windows-bootstrap-trace.log

load_project_env_value() {
  local key="$1"
  local value
  if [ -n "${!key:-}" ] || [ ! -f .env ]; then
    return 0
  fi
  value="$(sed -n "s/^${key}=//p" .env | head -n 1)"
  if [ -n "$value" ]; then
    export "$key=$value"
  fi
}

load_project_env_value DATA_ROOT
load_project_env_value BACKUP_PATH
load_project_env_value BACKUP_RETENTION_DAYS
load_project_env_value BACKUP_INCLUDE_FILES
load_project_env_value BACKUP_STALE_SECONDS
load_project_env_value SECURITY_LOG_PATH
load_project_env_value SECURITY_LOG_TIMEZONE
load_project_env_value SECURITY_LOG_RETENTION_DAYS
load_project_env_value NEWS_ARCHIVE_PATH
load_project_env_value NEWS_RETENTION_DAYS
load_project_env_value HOST_METRICS_PATH
load_project_env_value HOST_METRICS_STALE_SECONDS
load_project_env_value HOMEOPS_SCHEDULER_SECRET

normalize_project_path() {
  local key="$1"
  local value="${!key:-}"
  case "$value" in
    /data/*)
      export "$key=$PROJECT_ROOT/data/${value#/data/}"
      ;;
    /app/data/*)
      export "$key=$PROJECT_ROOT/data/${value#/app/data/}"
      ;;
  esac
}

normalize_project_path DATA_ROOT
normalize_project_path BACKUP_PATH
normalize_project_path SECURITY_LOG_PATH
normalize_project_path NEWS_ARCHIVE_PATH
normalize_project_path HOST_METRICS_PATH

PORTAL_RUNTIME_MARKER="${PORTAL_RUNTIME_MARKER:-$PROJECT_ROOT/data/portal-runtime.mode}"
PORTAL_RUNTIME_MODE="compose"
PORTAL_SCAN_URL=""
RUN_MAINTENANCE=1
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
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Invalid portal runtime marker; preserving Portal writer boundary" >> /tmp/windows-bootstrap-trace.log
      PORTAL_RUNTIME_MODE="cutover"
      ;;
  esac
}

require_portal_state_ready() {
  local state_directory="$PROJECT_ROOT/data/portal-web-state"
  if [ -f "$state_directory/homeops.sqlite3" ] && python3 -c 'import sqlite3,sys; connection=sqlite3.connect(sys.argv[1]); result=connection.execute("PRAGMA quick_check").fetchone()[0]; connection.close(); sys.exit(result != "ok")' "$state_directory/homeops.sqlite3"; then
    return 0
  fi
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Portal state migration is required before Compose Portal can start" >> /tmp/windows-bootstrap-trace.log
  return 1
}

validate_docker_bridge_gateway() {
  local actual_gateway
  [ -f "$PORTAL_BRIDGE_COMPOSE_FILE" ] || {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Portal bridge Compose override is missing" >> /tmp/windows-bootstrap-trace.log
    return 1
  }
  actual_gateway=$(docker network inspect bridge --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}') || return 1
  if ! python3 -c 'import ipaddress,sys; ipaddress.IPv4Address(sys.argv[1])' "$actual_gateway" >/dev/null 2>&1; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Docker bridge gateway is invalid" >> /tmp/windows-bootstrap-trace.log
    return 1
  fi
  if [ -n "${DOCKER_BRIDGE_GATEWAY:-}" ] && [ "$DOCKER_BRIDGE_GATEWAY" != "$actual_gateway" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Configured Docker bridge gateway does not match active bridge" >> /tmp/windows-bootstrap-trace.log
    return 1
  fi
  export DOCKER_BRIDGE_GATEWAY="$actual_gateway"
}

start_runtime_services() {
  local compose_services
  local bridge_compose
  load_portal_runtime_mode
  case "$PORTAL_RUNTIME_MODE" in
    compose)
      require_portal_state_ready
      export HOMEOPS_DOCKER_MANAGED_SERVICES="${HOMEOPS_DOCKER_MANAGED_SERVICES:-portal-web,system-agent,crawler-worker,youtube-memo,book-memo,caddy,homeops-executor}"
      export EXPECTED_CONTAINERS="${EXPECTED_CONTAINERS:-portal-web,crawler-worker,youtube-memo,book-memo,system-agent}"
      docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d \
        portal-web homeops-executor system-agent crawler-worker youtube-memo book-memo car-care-worker caddy
      PORTAL_SCAN_URL="http://127.0.0.1:8000/internal/homeops/scan"
      ;;
    cutover)
      validate_docker_bridge_gateway
      export HOMEOPS_DOCKER_MANAGED_SERVICES="system-agent,crawler-worker,youtube-memo,book-memo,caddy,homeops-executor"
      export EXPECTED_CONTAINERS="crawler-worker,youtube-memo,book-memo,system-agent"
      compose_services="homeops-executor system-agent crawler-worker youtube-memo book-memo car-care-worker"
      bridge_compose=(docker compose -f docker-compose.yml -f docker-compose.n100.yml -f "$PORTAL_BRIDGE_COMPOSE_FILE")
      "${bridge_compose[@]}" up -d --no-deps --force-recreate $compose_services
      "${bridge_compose[@]}" up -d --no-deps caddy
      RUN_MAINTENANCE=0
      ;;
    k3s)
      validate_docker_bridge_gateway
      export HOMEOPS_DOCKER_MANAGED_SERVICES="system-agent,crawler-worker,youtube-memo,book-memo,caddy,homeops-executor"
      export EXPECTED_CONTAINERS="crawler-worker,youtube-memo,book-memo,system-agent"
      compose_services="homeops-executor system-agent crawler-worker youtube-memo book-memo car-care-worker"
      bridge_compose=(docker compose -f docker-compose.yml -f docker-compose.n100.yml -f "$PORTAL_BRIDGE_COMPOSE_FILE")
      "${bridge_compose[@]}" up -d --no-deps --force-recreate $compose_services
      "${bridge_compose[@]}" up -d --no-deps caddy
      PORTAL_SCAN_URL="http://127.0.0.1:30080/internal/homeops/scan"
      ;;
  esac
}

run_daily_maintenance() {
  local marker=/tmp/personal-server-maintenance.last
  local today
  today="$(date +%F)"
  if [ -f "$marker" ] && [ "$(cat "$marker")" = "$today" ]; then
    return 0
  fi

  if python3 scripts/maintenance.py all >>/tmp/personal-server-maintenance.log 2>&1; then
    printf '%s\n' "$today" > "$marker"
  else
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Maintenance cleanup failed" >> /tmp/windows-bootstrap-trace.log
  fi
}

start_runtime_services

if [ -n "${HOMEOPS_SCHEDULER_SECRET:-}" ] && [ -n "$PORTAL_SCAN_URL" ]; then
  curl --fail --silent --show-error --max-time 20 \
    -X POST "$PORTAL_SCAN_URL" \
    -H "X-HomeOps-Scheduler-Secret: ${HOMEOPS_SCHEDULER_SECRET}" \
    >> /tmp/homeops-scheduled-scan.log 2>&1 || echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] HomeOps scheduled scan failed" >> /tmp/windows-bootstrap-trace.log
fi

if [ "$RUN_MAINTENANCE" -eq 1 ]; then run_daily_maintenance; fi

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] cloudflared is managed by the Windows bootstrap process" >> /tmp/windows-bootstrap-trace.log
