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
ALERTMANAGER_CONFIG_CONTRACT="${SRE_TELEGRAM_ALERTMANAGER_CONFIG_CONTRACT:-$REPO_ROOT/infra/k8s/sre-telegram/alertmanager-config.contract.yaml}"
ALERTMANAGER_CONFIG_FILE="${SRE_TELEGRAM_ALERTMANAGER_CONFIG_FILE:-}"
PREFLIGHT_SCRIPT="${SRE_TELEGRAM_PREFLIGHT_SCRIPT:-$SCRIPT_DIR/sre-telegram-preflight.sh}"

created_resources=()
TEMP_MANIFEST_DIR=""
APPLY_MODE=0
CLEANUP_DONE=0
HELM_UPGRADE_STARTED=0
HELM_PREVIOUS_REVISION=""

fail() {
  printf 'sre_telegram_install=FAIL\n'
  return 1
}

usage() {
  printf 'usage: %s [--render|--apply [--alertmanager-config-file PATH]]\n' "$0" >&2
  printf '%s\n' 'default is --render; --apply requires an N100 operator-supplied local Alertmanager config file.' >&2
}

render() {
  require_alertmanager_contract || return 1
  helm template "$RELEASE" "$CHART" --namespace "$NAMESPACE" --version "$VERSION" \
    --values "$BASE_VALUES" --values "$ALERTMANAGER_VALUES" >/dev/null || return 1
  sudo k3s kubectl apply --dry-run=client -f "$RELAY_BASE" >/dev/null || return 1
  sudo k3s kubectl apply --dry-run=client -f "$PROMETHEUS_RULE" >/dev/null
}

require_alertmanager_contract() {
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
      namespace="cluster"
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
  local file="$1" document="$2" kind namespace
  kind=$(manifest_document "$file" "$document" | awk '$1 == "kind:" { print $2; exit }') || return 1
  case "$kind" in
    ClusterRole|ClusterRoleBinding)
      printf 'cluster\n'
      return 0
      ;;
  esac
  namespace=$(manifest_document "$file" "$document" | awk '
    /^metadata:[[:space:]]*$/ { in_metadata=1; next }
    in_metadata && /^[^[:space:]]/ { in_metadata=0 }
    in_metadata && $1 == "namespace:" { print $2; exit }
  ')
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
  local image_listing canonical_image
  case "$IMAGE" in
    */*) canonical_image="$IMAGE" ;;
    *) canonical_image="docker.io/library/$IMAGE" ;;
  esac
  image_listing=$(sudo k3s ctr -n k8s.io images list 2>/dev/null) || return 1
  printf '%s\n' "$image_listing" | awk -v image="$IMAGE" -v canonical_image="$canonical_image" 'NR > 1 && ($1 == image || $1 == canonical_image) && $3 ~ /^sha256:[[:xdigit:]]+$/ { found=1 } END { exit(found ? 0 : 1) }'
}

capture_previous_helm_state() {
  local status
  status=$(helm status "$RELEASE" --namespace "$NAMESPACE" --output json 2>/dev/null) || return 1
  [[ "$status" =~ \"status\"[[:space:]]*:[[:space:]]*\"deployed\" ]] || return 1
  if [[ "$status" =~ \"revision\"[[:space:]]*:[[:space:]]*\"?([0-9]+)\"? ]]; then
    HELM_PREVIOUS_REVISION="${BASH_REMATCH[1]}"
  else
    return 1
  fi
}

verify_or_restore_helm_release() {
  local status
  [ -n "$HELM_PREVIOUS_REVISION" ] || return 1

  helm rollback "$RELEASE" "$HELM_PREVIOUS_REVISION" --namespace "$NAMESPACE" --wait --timeout 10m >/dev/null 2>&1 || return 1
  status=$(helm status "$RELEASE" --namespace "$NAMESPACE" --output json 2>/dev/null) || return 1
  [[ "$status" =~ \"status\"[[:space:]]*:[[:space:]]*\"deployed\" ]]
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
    if [ "$HELM_UPGRADE_STARTED" -eq 1 ] && [ -n "$HELM_PREVIOUS_REVISION" ]; then
      if ! verify_or_restore_helm_release; then
        printf 'sre_telegram_helm_restore=UNVERIFIED\n' >&2
      fi
    fi
  fi
  return "$status"
}

interrupt_apply() {
  exit 130
}

apply() {
  local alertmanager_config_file="$1"
  [ -n "$alertmanager_config_file" ] || return 1
  "$PREFLIGHT_SCRIPT" --alertmanager-config-file "$alertmanager_config_file" >/dev/null 2>&1 || return 1
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
  capture_previous_helm_state || return 1
  HELM_UPGRADE_STARTED=1
  helm upgrade "$RELEASE" "$CHART" --namespace "$NAMESPACE" --version "$VERSION" \
    --values "$ALERTMANAGER_VALUES" --reuse-values --atomic --wait --timeout 10m || return 1
}

main() {
  local mode="${1:---render}"
  local alertmanager_config_file="$ALERTMANAGER_CONFIG_FILE"
  case "$mode" in
    --render)
      [ "$#" -eq 1 ] || { usage; fail; return 2; }
      render || { fail; return 1; }
      printf 'sre_telegram_install=PASS\n'
      ;;
    --apply)
      if [ "$#" -eq 3 ] && [ "$2" = "--alertmanager-config-file" ]; then
        alertmanager_config_file="$3"
      elif [ "$#" -ne 1 ]; then
        usage
        fail
        return 2
      fi
      APPLY_MODE=1
      trap cleanup_apply EXIT
      trap interrupt_apply INT TERM
      if ! apply "$alertmanager_config_file"; then
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
