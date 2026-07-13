#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${1:-$(pwd)}"

cd "$PROJECT_ROOT"

git fetch --prune origin
git reset --hard origin/main
docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.n100.yml ps
