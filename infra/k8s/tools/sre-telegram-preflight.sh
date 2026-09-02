#!/usr/bin/env bash
set -u

RELEASE="personal-server-monitoring"
NAMESPACE="monitoring"
RUNTIME_SECRET="sre-telegram-relay-runtime"
ALERTMANAGER_SECRET="sre-telegram-alertmanager-config"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../../.." && pwd)
IMAGE_DIR="$REPO_ROOT/sre-telegram-relay"
overall=0

fail_check() {
  overall=1
  printf 'check=%s status=FAIL reason=%s\n' "$1" "$2"
}

secret_keys_present() {
  local secret_name="$1"
  shift
  local description key
  description=$(sudo k3s kubectl -n "$NAMESPACE" describe secret "$secret_name" 2>/dev/null) || return 1
  for key in "$@"; do
    printf '%s\n' "$description" | awk -v key="$key" '$1 == key ":" && $2 ~ /^[0-9]+$/ && $3 == "bytes" { found=1 } END { exit(found ? 0 : 1) }' || return 1
  done
}

main() {
  if [ "$#" -ne 0 ]; then
    printf 'usage: %s\n' "$0" >&2
    printf 'sre_telegram_preflight=FAIL\n'
    return 2
  fi

  local nodes grafana_ready prometheus_ready
  if ! nodes=$(sudo k3s kubectl get nodes --no-headers 2>/dev/null); then
    fail_check k3s_nodes unavailable
  elif ! printf '%s\n' "$nodes" | awk 'NF < 2 || $2 !~ /^Ready(,SchedulingDisabled)?$/ { bad=1 } END { exit(NR > 0 && !bad ? 0 : 1) }'; then
    fail_check k3s_nodes not_ready
  fi

  if ! helm status "$RELEASE" --namespace "$NAMESPACE" >/dev/null 2>&1; then
    fail_check monitoring_release unavailable
  fi

  if ! grafana_ready=$(sudo k3s kubectl -n "$NAMESPACE" get deployment "${RELEASE}-grafana" -o jsonpath='{.status.availableReplicas}' 2>/dev/null) || [ "${grafana_ready:-0}" -lt 1 ]; then
    fail_check grafana unavailable
  fi
  if ! prometheus_ready=$(sudo k3s kubectl -n "$NAMESPACE" get statefulset "prometheus-${RELEASE}-kube-prometheus-prometheus" -o jsonpath='{.status.readyReplicas}' 2>/dev/null) || [ "${prometheus_ready:-0}" -lt 1 ]; then
    fail_check prometheus unavailable
  fi

  if ! command -v docker >/dev/null 2>&1 || [ ! -r "$IMAGE_DIR/Dockerfile" ] || [ ! -r "$IMAGE_DIR/app/main.py" ]; then
    fail_check image_build_prerequisites unavailable
  fi
  if ! sudo k3s ctr version >/dev/null 2>&1; then
    fail_check image_import_prerequisites unavailable
  fi

  if ! secret_keys_present "$RUNTIME_SECRET" telegram_bot_token allowed_chat_id alertmanager_auth_token; then
    fail_check relay_runtime_secret missing_keys
  fi
  if ! secret_keys_present "$ALERTMANAGER_SECRET" alertmanager.yaml; then
    fail_check alertmanager_config_secret missing_keys
  fi

  if [ "$overall" -eq 0 ]; then
    printf 'sre_telegram_preflight=PASS\n'
    return 0
  fi
  printf 'sre_telegram_preflight=FAIL\n'
  return 1
}

main "$@"
