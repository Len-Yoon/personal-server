#!/usr/bin/env bash
set -Eeuo pipefail

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
TEMP_MANIFEST_DIR=""
APPLY_MODE=0
CLEANUP_DONE=0
INTERRUPTED=0
HELM_UPGRADE_STARTED=0
HELM_PREVIOUS_REVISION=""

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
  if ! description=$(sudo k3s kubectl -n "$NAMESPACE" describe secret "$secret_name" 2>/dev/null); then
    return 1
  fi
  for key in "$@"; do
    printf '%s\n' "$description" | awk -v key="$key" '$1 == key ":" && $2 ~ /^[1-9][0-9]*$/ && $3 == "bytes" { found=1 } END { exit(found ? 0 : 1) }' || return 1
  done
}

record_reference() {
  local reference="$1" action="$2" namespace="$3"
  case "$reference" in
    configmap/sre-telegram-relay-state|serviceaccount/sre-telegram-relay|role.rbac.authorization.k8s.io/sre-telegram-relay-state|role/sre-telegram-relay-state|deployment.apps/sre-telegram-relay|deployment/sre-telegram-relay|service/sre-telegram-relay|prometheusrule.monitoring.coreos.com/sre-telegram-k3s-alerts|prometheusrule/sre-telegram-k3s-alerts)
      ;;
    rolebinding.rbac.authorization.k8s.io/sre-telegram-relay-workload-reader|rolebinding/sre-telegram-relay-workload-reader)
      ;;
    clusterrole.rbac.authorization.k8s.io/sre-telegram-relay-node-reader|clusterrole/sre-telegram-relay-node-reader|clusterrole.rbac.authorization.k8s.io/sre-telegram-relay-workload-reader|clusterrole/sre-telegram-relay-workload-reader|clusterrolebinding.rbac.authorization.k8s.io/sre-telegram-relay-node-reader|clusterrolebinding/sre-telegram-relay-node-reader)
      ;;
    *)
      return 0
      ;;
  esac
  if [ "$action" = "created" ]; then
    created_resources+=("$namespace|$reference")
  fi
}

record_apply_output() {
  local output="$1" namespace="$2" reference action
  while read -r reference action _; do
    [ -n "${reference:-}" ] || continue
    record_reference "$reference" "${action:-}" "$namespace" || return 1
  done <<< "$output"
}

manifest_document() {
  local file="$1" document="$2"
  awk -v wanted="$document" '
    BEGIN { current = 1 }
    /^---[[:space:]]*$/ { current++; next }
    current == wanted { print }
  ' "$file"
}

manifest_document_count() {
  awk '/^---[[:space:]]*$/ { count++ } END { print count + 1 }' "$1"
}

manifest_namespace() {
  local file="$1" document="$2" namespace
  namespace=$(manifest_document "$file" "$document" | awk '$1 == "namespace:" { print $2; exit }')
  printf '%s\n' "${namespace:-cluster}"
}

manifest_resource_name() {
  local file="$1" document="$2" kind name
  kind=$(manifest_document "$file" "$document" | awk '$1 == "kind:" { print $2; exit }') || return 1
  name=$(manifest_document "$file" "$document" | awk '$1 == "name:" { print $2; exit }') || return 1
  [ -n "$kind" ] && [ -n "$name" ] || return 1
  printf '%s-%s\n' "$kind" "$name"
}

cleanup_manifest_dir() {
  if [ -n "$TEMP_MANIFEST_DIR" ] && [ -d "$TEMP_MANIFEST_DIR" ]; then
    rm -rf -- "$TEMP_MANIFEST_DIR"
  fi
  TEMP_MANIFEST_DIR=""
}

apply_file() {
  local file="$1" document_count document namespace resource_name document_path output result=0 had_existing=0
  TEMP_MANIFEST_DIR=$(mktemp -d "${TMPDIR:-/tmp}/sre-telegram-install.XXXXXX") || return 1
  if ! document_count=$(manifest_document_count "$file"); then
    cleanup_manifest_dir
    return 1
  fi
  for ((document = 1; document <= document_count; document++)); do
    if ! namespace=$(manifest_namespace "$file" "$document"); then
      result=1
      break
    fi
    if ! resource_name=$(manifest_resource_name "$file" "$document"); then
      result=1
      break
    fi
    document_path="$TEMP_MANIFEST_DIR/${namespace}-${resource_name}.yaml"
    if ! manifest_document "$file" "$document" > "$document_path"; then
      result=1
      break
    fi
    if [ "$namespace" = "cluster" ]; then
      if ! output=$(sudo k3s kubectl create -f "$document_path" 2>&1); then
        record_apply_output "$output" "$namespace" || { result=1; break; }
        if [[ "$output" == *AlreadyExists* ]]; then
          had_existing=1
          continue
        fi
        result=1
        break
      fi
    elif ! output=$(sudo k3s kubectl -n "$namespace" create -f "$document_path" 2>&1); then
      record_apply_output "$output" "$namespace" || { result=1; break; }
      if [[ "$output" == *AlreadyExists* ]]; then
        had_existing=1
        continue
      fi
      result=1
      break
    fi
    record_apply_output "$output" "$namespace" || { result=1; break; }
  done
  cleanup_manifest_dir
  if [ "$had_existing" -eq 1 ]; then
    result=1
  fi
  return "$result"
}

rollback_created_resources() {
  local index entry namespace reference
  for ((index=${#created_resources[@]} - 1; index>=0; index--)); do
    entry="${created_resources[index]}"
    namespace="${entry%%|*}"
    reference="${entry#*|}"
    if [ "$namespace" = "cluster" ]; then
      sudo k3s kubectl delete "$reference" --ignore-not-found >/dev/null 2>&1 || true
    else
      sudo k3s kubectl -n "$namespace" delete "$reference" --ignore-not-found >/dev/null 2>&1 || true
    fi
  done
}

verify_imported_image() {
  local image_listing
  image_listing=$(sudo k3s ctr -n k8s.io images list 2>/dev/null) || return 1
  printf '%s\n' "$image_listing" | awk -v image="$IMAGE" 'NR > 1 && $1 == image && $3 ~ /^sha256:[[:xdigit:]]+$/ { found=1 } END { exit(found ? 0 : 1) }'
}

capture_previous_helm_revision() {
  local status
  status=$(helm status "$RELEASE" --namespace "$NAMESPACE" --output json 2>/dev/null) || return 1
  [[ "$status" =~ \"status\"[[:space:]]*:[[:space:]]*\"deployed\" ]] || return 1
  if [[ "$status" =~ \"revision\"[[:space:]]*:[[:space:]]*\"?([0-9]+)\"? ]]; then
    HELM_PREVIOUS_REVISION="${BASH_REMATCH[1]}"
  else
    return 1
  fi
}

cleanup_apply() {
  local status=$?
  if [ "$CLEANUP_DONE" -eq 1 ]; then
    return "$status"
  fi
  CLEANUP_DONE=1
  cleanup_manifest_dir
  if [ "$APPLY_MODE" -eq 1 ] && [ "$status" -ne 0 ]; then
    rollback_created_resources
    if [ "$HELM_UPGRADE_STARTED" -eq 1 ] && [ "$INTERRUPTED" -eq 1 ] && [ -n "$HELM_PREVIOUS_REVISION" ]; then
      helm rollback "$RELEASE" "$HELM_PREVIOUS_REVISION" --namespace "$NAMESPACE" --wait --timeout 10m >/dev/null 2>&1 || true
    fi
  fi
  return "$status"
}

interrupt_apply() {
  INTERRUPTED=1
  exit 130
}

apply() {
  "$PREFLIGHT_SCRIPT" >/dev/null 2>&1 || return 1
  require_secret_contract sre-telegram-relay-runtime telegram_bot_token allowed_chat_id alertmanager_auth_token || return 1
  require_secret_contract sre-telegram-alertmanager-config alertmanager.yaml || return 1
  render || return 1
  docker build --tag "$IMAGE" "$REPO_ROOT/sre-telegram-relay" >/dev/null || return 1
  if ! docker save "$IMAGE" | sudo k3s ctr -n k8s.io images import -; then
    return 1
  fi
  verify_imported_image || return 1
  apply_file "$RELAY_BASE" || return 1
  apply_file "$PROMETHEUS_RULE" || return 1
  capture_previous_helm_revision || return 1
  HELM_UPGRADE_STARTED=1
  helm upgrade "$RELEASE" "$CHART" --namespace "$NAMESPACE" --version "$VERSION" \
    --values "$ALERTMANAGER_VALUES" --reuse-values --atomic --wait --timeout 10m || return 1
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
      APPLY_MODE=1
      trap cleanup_apply EXIT
      trap interrupt_apply INT TERM
      if ! apply; then
        fail
        return 1
      fi
      APPLY_MODE=0
      trap - EXIT INT TERM
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
