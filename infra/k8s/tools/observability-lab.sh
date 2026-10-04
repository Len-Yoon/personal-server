#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
LAB_MANIFEST="$SCRIPT_DIR/../observability-lab/loki-lab.yaml"
DATASOURCE_MANIFEST="$SCRIPT_DIR/../monitoring/loki-lab-datasource.yaml"
DASHBOARD_MANIFEST="$SCRIPT_DIR/../monitoring/loki-lab-dashboard.yaml"
LAB_NAMESPACE=observability-lab
MONITORING_NAMESPACE=monitoring

usage() {
  printf 'usage: %s --check|--apply|--verify|--rollback [--delete-data] --context CURRENT_CONTEXT\n' "$0" >&2
}

fail() {
  printf 'observability_lab=FAIL\n'
  printf '%s\n' "$1" >&2
  exit 1
}

mode=""
expected_context=""
delete_data=false
while (($#)); do
  case "$1" in
    --check|--apply|--verify|--rollback)
      [[ -z "$mode" ]] || { usage; fail 'mode must be supplied once'; }
      mode="$1"
      ;;
    --delete-data) delete_data=true ;;
    --context)
      shift
      (($#)) || { usage; fail 'context value is required'; }
      expected_context="$1"
      ;;
    *) usage; fail 'unsupported argument' ;;
  esac
  shift
done

[[ -n "$mode" && -n "$expected_context" ]] || { usage; fail 'mode and context are required'; }
[[ "$expected_context" =~ ^[A-Za-z0-9][A-Za-z0-9._:/@-]*$ ]] || fail 'invalid context value'
[[ "$delete_data" == false || "$mode" == --rollback ]] || { usage; fail 'delete-data requires rollback'; }

# The context is checked before all reads that could precede a mutation.
active_context="$(sudo -n k3s kubectl --request-timeout=15s config current-context 2>/dev/null)" || fail 'cannot determine active Kubernetes context'
[[ "$active_context" == "$expected_context" ]] || fail 'active Kubernetes context differs from requested context'
kubectl=(sudo -n k3s kubectl --context "$expected_context" --request-timeout=15s)

require_files() {
  [[ -r "$LAB_MANIFEST" && -r "$DATASOURCE_MANIFEST" && -r "$DASHBOARD_MANIFEST" ]] \
    || fail 'Loki lab manifest is missing or unreadable'
}

preflight() {
  require_files
  "${kubectl[@]}" get storageclass local-path >/dev/null 2>&1 || fail 'local-path storageclass unavailable'
  "${kubectl[@]}" -n "$MONITORING_NAMESPACE" get deployment personal-server-monitoring-grafana >/dev/null 2>&1 \
    || fail 'Grafana deployment unavailable'
  # An absent lab namespace is expected on first installation. Never touch another namespace.
  "${kubectl[@]}" get namespace "$LAB_NAMESPACE" --ignore-not-found=true >/dev/null 2>&1 \
    || fail 'lab namespace preflight failed'
}

apply_manifest() {
  local manifest="$1"
  "${kubectl[@]}" apply --dry-run=server -f "$manifest" >/dev/null 2>&1 \
    || fail 'Loki lab server dry-run failed'
  "${kubectl[@]}" apply -f "$manifest" >/dev/null 2>&1 \
    || fail 'Loki lab apply failed; inspect live state before retry'
}

apply_lab() {
  preflight
  # A dry-run Namespace cannot establish a namespace for subsequent namespaced dry-runs.
  # Create only this fixed namespace, then dry-run each complete manifest before applying it.
  local namespace_name
  namespace_name="$("${kubectl[@]}" get namespace "$LAB_NAMESPACE" --ignore-not-found=true -o name 2>/dev/null)" \
    || fail 'lab namespace lookup failed'
  if [[ -z "$namespace_name" ]]; then
    "${kubectl[@]}" create namespace "$LAB_NAMESPACE" --dry-run=server >/dev/null 2>&1 \
      || fail 'lab namespace server dry-run failed'
    "${kubectl[@]}" create namespace "$LAB_NAMESPACE" >/dev/null 2>&1 \
      || fail 'lab namespace creation failed; inspect live state before retry'
  elif [[ "$namespace_name" != "namespace/$LAB_NAMESPACE" ]]; then
    fail 'unexpected lab namespace lookup result'
  fi
  apply_manifest "$LAB_MANIFEST"
  apply_manifest "$DATASOURCE_MANIFEST"
  apply_manifest "$DASHBOARD_MANIFEST"
}

verify_lab() {
  preflight
  "${kubectl[@]}" get namespace "$LAB_NAMESPACE" >/dev/null 2>&1 || fail 'lab namespace unavailable'
  local deployment
  for deployment in loki-lab loki-lab-alloy loki-lab-sample; do
    "${kubectl[@]}" --request-timeout=100s -n "$LAB_NAMESPACE" wait --for=condition=Available "deployment/$deployment" --timeout=90s >/dev/null 2>&1 \
      || fail 'lab deployment unavailable'
  done
  local pvc_phase
  pvc_phase="$("${kubectl[@]}" -n "$LAB_NAMESPACE" get pvc loki-lab-data -o jsonpath='{.status.phase}' 2>/dev/null)" \
    || fail 'lab PVC unavailable'
  [[ "$pvc_phase" == Bound ]] || fail 'lab PVC is not Bound'
  "${kubectl[@]}" -n "$MONITORING_NAMESPACE" get configmap loki-lab-datasource >/dev/null 2>&1 \
    || fail 'lab datasource unavailable'
  "${kubectl[@]}" -n "$MONITORING_NAMESPACE" get configmap loki-lab-dashboard >/dev/null 2>&1 \
    || fail 'lab dashboard unavailable'

  local proxy='/api/v1/namespaces/observability-lab/services/http:loki-lab:3100/proxy'
  retry_probe loki_ready || fail 'Loki readiness failed after 3 attempts'
  retry_probe sample_query || fail 'allowlisted sample log query failed after 3 attempts'
}

# Bound both individual API calls (15s) and startup retries (3 calls, two 10s gaps).
retry_probe() {
  local probe="$1" attempt
  for attempt in 1 2 3; do
    if "$probe"; then return 0; fi
    if ((attempt < 3)); then sleep 10; fi
  done
  return 1
}

loki_ready() {
  "${kubectl[@]}" get --raw "$proxy/ready" >/dev/null 2>&1
}

sample_query() {
  "${kubectl[@]}" get --raw "$proxy/loki/api/v1/query_range?query=%7Bapp%3D%22loki-lab-sample%22%2Cnamespace%3D%22observability-lab%22%7D&limit=1" 2>/dev/null \
    | python3 -c 'import json, sys; p=json.load(sys.stdin); sys.exit(0 if p.get("status")=="success" and any(s.get("values") for s in p.get("data",{}).get("result",[])) else 1)' >/dev/null 2>&1
}

delete_named() {
  local namespace="$1" kind="$2" name="$3"
  "${kubectl[@]}" -n "$namespace" delete "$kind" "$name" --ignore-not-found=true >/dev/null 2>&1 \
    || fail 'named lab resource rollback failed; inspect live state before retry'
}

rollback_lab() {
  # Namespace deletion would also delete retained data or an unrelated resource.
  delete_named "$MONITORING_NAMESPACE" configmap loki-lab-dashboard
  delete_named "$MONITORING_NAMESPACE" configmap loki-lab-datasource
  local name
  for name in loki-lab-alloy loki-lab-sample loki-lab; do
    delete_named "$LAB_NAMESPACE" deployment "$name"
  done
  delete_named "$LAB_NAMESPACE" service loki-lab
  delete_named "$LAB_NAMESPACE" networkpolicy loki-lab-ingress
  delete_named "$LAB_NAMESPACE" rolebinding loki-lab-alloy
  delete_named "$LAB_NAMESPACE" role loki-lab-alloy
  delete_named "$LAB_NAMESPACE" serviceaccount loki-lab-alloy
  delete_named "$LAB_NAMESPACE" configmap loki-lab-alloy
  delete_named "$LAB_NAMESPACE" configmap loki-lab
  if [[ "$delete_data" == true ]]; then
    delete_named "$LAB_NAMESPACE" pvc loki-lab-data
  fi
}

case "$mode" in
  --check) preflight ;;
  --apply) apply_lab ;;
  --verify) verify_lab ;;
  --rollback) rollback_lab ;;
esac
printf 'observability_lab=PASS\n'
