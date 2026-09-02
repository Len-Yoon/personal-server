#!/usr/bin/env bash
set -Eeuo pipefail

RELEASE="personal-server-monitoring"
NAMESPACE="monitoring"
RUNTIME_SECRET="sre-telegram-relay-runtime"
ALERTMANAGER_SECRET="sre-telegram-alertmanager-config"
PROMETHEUS_SERVICE="personal-server-monitoring-prometheus"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../../.." && pwd)
IMAGE_DIR="$REPO_ROOT/sre-telegram-relay"
ALERTMANAGER_CONFIG_CONTRACT="${SRE_TELEGRAM_ALERTMANAGER_CONFIG_CONTRACT:-$REPO_ROOT/infra/k8s/sre-telegram/alertmanager-config.contract.yaml}"
ALERTMANAGER_CONFIG_FILE="${SRE_TELEGRAM_ALERTMANAGER_CONFIG_FILE:-}"
overall=0

fail_check() {
  overall=1
  printf 'check=%s status=FAIL reason=%s\n' "$1" "$2"
}

usage() {
  printf 'usage: %s [--alertmanager-config-file PATH]\n' "$0" >&2
  printf '%s\n' 'An N100 operator-supplied local Alertmanager config file is required; its contents are never printed.' >&2
}

secret_keys_present() {
  local secret_name="$1"
  shift
  local description key
  description=$(sudo k3s kubectl -n "$NAMESPACE" describe secret "$secret_name" 2>/dev/null) || return 1
  for key in "$@"; do
    printf '%s\n' "$description" | awk -v key="$key" '$1 == key ":" && $2 ~ /^[1-9][0-9]*$/ && $3 == "bytes" { found=1 } END { exit(found ? 0 : 1) }' || return 1
  done
}

alertmanager_contract_valid() {
  local contract="$ALERTMANAGER_CONFIG_CONTRACT"
  [ -r "$contract" ] || return 1
  local required
  for required in \
    'route:' \
    'group_by:' \
    'repeat_interval: 4h' \
    'sre_telegram="true"' \
    'receiver: sre-telegram-relay' \
    'url: http://sre-telegram-relay.monitoring.svc:8080/alertmanager' \
    'send_resolved: true' \
    'credentials_file: /etc/alertmanager/secrets/sre-telegram-relay-runtime/alertmanager_auth_token'; do
    grep -F -- "$required" "$contract" >/dev/null || return 1
  done
}

config_contains_non_comment_contract_text() {
  local config_file="$1" required="$2"
  awk -v required="$required" '
    /^[[:space:]]*#/ { next }
    {
      line = $0
      sub(/[[:space:]]+#.*/, "", line)
      if (index(line, required) > 0) {
        found = 1
      }
    }
    END { exit(found ? 0 : 1) }
  ' "$config_file"
}

alertmanager_effective_config_valid() {
  local config_file="$1" required
  [ -n "$config_file" ] && [ -f "$config_file" ] && [ -r "$config_file" ] || return 1
  command -v amtool >/dev/null 2>&1 || return 1
  amtool check-config "$config_file" >/dev/null 2>&1 || return 1

  for required in \
    'route:' \
    'group_by:' \
    'repeat_interval: 4h' \
    'sre_telegram="true"' \
    'receiver: sre-telegram-relay' \
    'url: http://sre-telegram-relay.monitoring.svc:8080/alertmanager' \
    'send_resolved: true' \
    'type: Bearer' \
    'credentials_file: /etc/alertmanager/secrets/sre-telegram-relay-runtime/alertmanager_auth_token'; do
    config_contains_non_comment_contract_text "$config_file" "$required" || return 1
  done
}

monitoring_release_deployed() {
  local status
  status=$(helm status "$RELEASE" --namespace "$NAMESPACE" --output json 2>/dev/null) || return 1
  [[ "$status" =~ \"status\"[[:space:]]*:[[:space:]]*\"deployed\" ]]
}

all_prometheus_replicas_ready() {
  local ready_replicas="$1"
  printf '%s\n' "$ready_replicas" | awk 'NF { count++; if ($1 !~ /^[1-9][0-9]*$/) { bad=1 } } END { exit(count > 0 && !bad ? 0 : 1) }'
}

positive_replica_count() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

main() {
  local alertmanager_config_file="$ALERTMANAGER_CONFIG_FILE"
  case "$#" in
    0)
      ;;
    2)
      if [ "$1" = "--alertmanager-config-file" ]; then
        alertmanager_config_file="$2"
      else
        usage
        printf 'sre_telegram_preflight=FAIL\n'
        return 2
      fi
      ;;
    *)
      usage
      printf 'sre_telegram_preflight=FAIL\n'
      return 2
      ;;
  esac

  if [ -z "$alertmanager_config_file" ]; then
    usage
    printf 'sre_telegram_preflight=FAIL\n'
    return 2
  fi

  local nodes grafana_ready prometheus_ready
  if ! nodes=$(sudo k3s kubectl get nodes --no-headers 2>/dev/null); then
    fail_check k3s_nodes unavailable
  elif ! printf '%s\n' "$nodes" | awk 'NF < 2 || $2 !~ /^Ready(,SchedulingDisabled)?$/ { bad=1 } END { exit(NR > 0 && !bad ? 0 : 1) }'; then
    fail_check k3s_nodes not_ready
  fi

  if ! monitoring_release_deployed; then
    fail_check monitoring_release not_deployed
  fi

  if ! grafana_ready=$(sudo k3s kubectl -n "$NAMESPACE" get deployment "${RELEASE}-grafana" -o jsonpath='{.status.availableReplicas}' 2>/dev/null) || ! positive_replica_count "${grafana_ready:-0}"; then
    fail_check grafana unavailable
  fi
  if ! prometheus_ready=$(sudo k3s kubectl -n "$NAMESPACE" get statefulset -l app.kubernetes.io/name=prometheus -o jsonpath='{range .items[*]}{.status.readyReplicas}{"\n"}{end}' 2>/dev/null) || ! all_prometheus_replicas_ready "$prometheus_ready"; then
    fail_check prometheus unavailable
  fi
  if ! sudo k3s kubectl -n "$NAMESPACE" get service "$PROMETHEUS_SERVICE" >/dev/null 2>&1; then
    fail_check prometheus_service unavailable
  fi

  if ! command -v docker >/dev/null 2>&1 || [ ! -r "$IMAGE_DIR/Dockerfile" ] || [ ! -r "$IMAGE_DIR/app/main.py" ]; then
    fail_check image_build_prerequisites unavailable
  fi
  if ! sudo k3s ctr version >/dev/null 2>&1; then
    fail_check image_import_prerequisites unavailable
  fi

  if ! alertmanager_contract_valid; then
    fail_check alertmanager_config_contract invalid
  fi
  if ! alertmanager_effective_config_valid "$alertmanager_config_file"; then
    fail_check alertmanager_effective_config invalid_or_unavailable
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
