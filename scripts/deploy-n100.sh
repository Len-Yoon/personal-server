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

docker compose version >/dev/null

git fetch --prune origin
git reset --hard origin/main
docker compose -f docker-compose.yml -f docker-compose.n100.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.n100.yml ps
