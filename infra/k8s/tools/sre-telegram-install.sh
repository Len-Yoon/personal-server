#!/usr/bin/env bash
set -u

RELEASE="personal-server-monitoring"
CHART="prometheus-community/kube-prometheus-stack"
NAMESPACE="monitoring"
VERSION="88.6.1"
IMAGE="personal-server-sre-telegram-relay:latest"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../../.." && pwd)
BASE_VALUES="$REPO_ROOT/infra/k8s/monitoring/values.n100.yaml"
ALERTMANAGER_VALUES="$REPO_ROOT/infra/k8s/sre-telegram/alertmanager-values.yaml"
RELAY_BASE="$REPO_ROOT/infra/k8s/sre-telegram/base.yaml"
PROMETHEUS_RULE="$REPO_ROOT/infra/k8s/sre-telegram/prometheus-rule.yaml"
PREFLIGHT_SCRIPT="${SRE_TELEGRAM_PREFLIGHT_SCRIPT:-$SCRIPT_DIR/sre-telegram-preflight.sh}"
created_resources=()

fail() {
  printf 'sre_telegram_install=FAIL\n'
  return 1
}

usage() {
  printf 'usage: %s [--render|--apply]\n' "$0" >&2
  printf '%s\n' 'default is --render; --apply is required to change the N100 cluster.' >&2
}

render() {
  helm template "$RELEASE" "$CHART" --namespace "$NAMESPACE" --version "$VERSION" \
    --values "$BASE_VALUES" --values "$ALERTMANAGER_VALUES" >/dev/null || return 1
  sudo k3s kubectl apply --dry-run=client -f "$RELAY_BASE" >/dev/null || return 1
  sudo k3s kubectl apply --dry-run=client -f "$PROMETHEUS_RULE" >/dev/null
}

require_secret_contract() {
  local secret_name="$1"
  shift
  local description key
  description=$(sudo k3s kubectl -n "$NAMESPACE" describe secret "$secret_name" 2>/dev/null) || return 1
  for key in "$@"; do
    printf '%s\n' "$description" | awk -v key="$key" '$1 == key ":" && $2 ~ /^[0-9]+$/ && $3 == "bytes" { found=1 } END { exit(found ? 0 : 1) }' || return 1
  done
}

record_created() {
  local reference="$1"
  case "$reference" in
    configmap/sre-telegram-relay-state|serviceaccount/sre-telegram-relay|clusterrole.rbac.authorization.k8s.io/sre-telegram-relay-node-reader|clusterrole.rbac.authorization.k8s.io/sre-telegram-relay-workload-reader|role.rbac.authorization.k8s.io/sre-telegram-relay-state|clusterrolebinding.rbac.authorization.k8s.io/sre-telegram-relay-node-reader|rolebinding.rbac.authorization.k8s.io/sre-telegram-relay-workload-reader|rolebinding.rbac.authorization.k8s.io/sre-telegram-relay-state|deployment.apps/sre-telegram-relay|service/sre-telegram-relay|prometheusrule.monitoring.coreos.com/sre-telegram-k3s-alerts)
      created_resources+=("$reference")
      ;;
  esac
}

apply_file() {
  local file="$1" output reference action
  output=$(sudo k3s kubectl -n "$NAMESPACE" apply -f "$file" 2>&1) || {
    while read -r reference action _; do
      [ "$action" = "created" ] && record_created "$reference"
    done <<< "$output"
    return 1
  }
  while read -r reference action _; do
    [ "$action" = "created" ] && record_created "$reference"
  done <<< "$output"
}

rollback_created_resources() {
  local index
  for ((index=${#created_resources[@]} - 1; index>=0; index--)); do
    sudo k3s kubectl -n "$NAMESPACE" delete "${created_resources[index]}" --ignore-not-found >/dev/null 2>&1 || true
  done
}

apply() {
  "$PREFLIGHT_SCRIPT" >/dev/null 2>&1 || return 1
  require_secret_contract sre-telegram-relay-runtime telegram_bot_token allowed_chat_id alertmanager_auth_token || return 1
  require_secret_contract sre-telegram-alertmanager-config alertmanager.yaml || return 1
  docker build --tag "$IMAGE" "$REPO_ROOT/sre-telegram-relay" >/dev/null || return 1
  docker save "$IMAGE" | sudo k3s ctr -n k8s.io images import - || return 1
  helm upgrade "$RELEASE" "$CHART" --namespace "$NAMESPACE" --version "$VERSION" \
    --values "$BASE_VALUES" --values "$ALERTMANAGER_VALUES" --wait --timeout 10m || return 1
  apply_file "$RELAY_BASE" || { rollback_created_resources; return 1; }
  apply_file "$PROMETHEUS_RULE" || { rollback_created_resources; return 1; }
}

main() {
  [ "$#" -le 1 ] || { usage; fail; return 2; }
  local mode="${1:---render}"
  case "$mode" in
    --render)
      render || { fail; return 1; }
      printf 'sre_telegram_install=PASS\n'
      ;;
    --apply)
      apply || { fail; return 1; }
      printf 'sre_telegram_install=PASS\n'
      ;;
    *)
      usage
      fail
      return 2
      ;;
  esac
}

main "$@"
