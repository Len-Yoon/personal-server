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

docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d \
  portal-web system-agent crawler-worker youtube-memo book-memo caddy

if pgrep -af "cloudflared tunnel run" >/dev/null 2>&1; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] cloudflared already running" >> /tmp/windows-bootstrap-trace.log
  exit 0
fi

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] starting cloudflared" >> /tmp/windows-bootstrap-trace.log
nohup cloudflared tunnel run >/tmp/cloudflared-personal-server.log 2>&1 </dev/null &
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] cloudflared launch issued" >> /tmp/windows-bootstrap-trace.log
