#!/usr/bin/env bash
set -euo pipefail

cd /mnt/c/personal-server
{
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] bootstrap start"
  echo "pwd=$(pwd)"
  echo "cloudflared_path=$(command -v cloudflared || true)"
  echo "config_exists=$([ -f ~/.cloudflared/config.yml ] && echo yes || echo no)"
} >> /tmp/windows-bootstrap-trace.log

docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build portal-web system-agent

if pgrep -af "cloudflared tunnel run" >/dev/null 2>&1; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] cloudflared already running" >> /tmp/windows-bootstrap-trace.log
  exit 0
fi

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] starting cloudflared" >> /tmp/windows-bootstrap-trace.log
nohup cloudflared tunnel run >/tmp/cloudflared-personal-server.log 2>&1 </dev/null &
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] cloudflared launch issued" >> /tmp/windows-bootstrap-trace.log
