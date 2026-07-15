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

docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d \
  portal-web system-agent crawler-worker youtube-memo book-memo caddy

run_daily_maintenance

if pgrep -af "cloudflared tunnel run" >/dev/null 2>&1; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] cloudflared already running" >> /tmp/windows-bootstrap-trace.log
  exit 0
fi

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] starting cloudflared" >> /tmp/windows-bootstrap-trace.log
nohup cloudflared tunnel run >/tmp/cloudflared-personal-server.log 2>&1 </dev/null &
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] cloudflared launch issued" >> /tmp/windows-bootstrap-trace.log
