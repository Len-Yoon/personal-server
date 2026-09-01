#!/usr/bin/env bash
set -u

usage() {
  cat <<'USAGE'
usage: sre-health-audit.sh

Read-only N100 K3s node and Docker Compose health audit.
USAGE
}

if [ "$#" -gt 0 ]; then
  case "$1" in
    --help|-h)
      [ "$#" -eq 1 ] || { printf 'unknown argument: %s\n' "$2" >&2; usage >&2; exit 2; }
      usage
      exit 0
      ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_FILES=("$REPO_ROOT/docker-compose.yml" "$REPO_ROOT/docker-compose.n100.yml")

k3s_nodes_check() {
  local output
  if ! output="$(sudo k3s kubectl get nodes --no-headers 2>&1)"; then
    printf 'k3s_nodes=FAIL detail=unable_to_read_nodes\n'
    printf 'k3s_nodes_detail=%s\n' "$output"
    return 1
  fi
  if printf '%s\n' "$output" | awk '$2 ~ /^Ready(,|$)/ { found=1 } END { exit(found ? 0 : 1) }'; then
    printf 'k3s_nodes=PASS detail=at_least_one_ready\n'
    return 0
  fi
  printf 'k3s_nodes=FAIL detail=no_ready_node\n'
  printf 'k3s_nodes_detail=%s\n' "$output"
  return 1
}

compose_check() {
  local services_output ps_output service state health
  local -a expected_services=()
  local ok=0
  if ! services_output="$(docker compose -f "${COMPOSE_FILES[0]}" -f "${COMPOSE_FILES[1]}" config --services)"; then
    printf 'compose_containers=FAIL detail=unable_to_read_compose_services\n'
    printf 'compose_containers_detail=%s\n' "$services_output"
    return 1
  fi
  while IFS= read -r service; do
    [ -n "$service" ] && expected_services+=("$service")
  done <<< "$services_output"
  if [ "${#expected_services[@]}" -eq 0 ]; then
    printf 'compose_containers=FAIL detail=no_compose_services\n'
    return 1
  fi
  if ! ps_output="$(docker compose -f "${COMPOSE_FILES[0]}" -f "${COMPOSE_FILES[1]}" ps --all --format '{{.Service}}|{{.State}}|{{.Health}}')"; then
    printf 'compose_containers=FAIL detail=unable_to_read_compose_status\n'
    printf 'compose_containers_detail=%s\n' "$ps_output"
    return 1
  fi
  for service in "${expected_services[@]}"; do
    state="$(printf '%s\n' "$ps_output" | awk -F '|' -v wanted="$service" '$1 == wanted { print $2; exit }')"
    health="$(printf '%s\n' "$ps_output" | awk -F '|' -v wanted="$service" '$1 == wanted { print $3; exit }')"
    if [[ "$state" != "running" && "$state" != "Running" && "$state" != "up" && "$state" != "Up" ]]; then
      printf 'compose_service=%s FAIL detail=not_running state=%s\n' "$service" "${state:-missing}"
      ok=1
    elif [ -n "$health" ] && [ "$health" != "healthy" ]; then
      printf 'compose_service=%s FAIL detail=unhealthy health=%s\n' "$service" "$health"
      ok=1
    else
      printf 'compose_service=%s PASS state=%s health=%s\n' "$service" "$state" "${health:-none}"
    fi
  done
  if [ "$ok" -eq 0 ]; then
    printf 'compose_containers=PASS detail=all_expected_services_running\n'
    return 0
  fi
  printf 'compose_containers=FAIL detail=service_check_failed\n'
  return 1
}

overall=0
k3s_nodes_check || overall=1
compose_check || overall=1
if [ "$overall" -eq 0 ]; then
  printf 'sre_health=PASS\n'
  exit 0
fi
printf 'sre_health=FAIL\n'
exit 1
