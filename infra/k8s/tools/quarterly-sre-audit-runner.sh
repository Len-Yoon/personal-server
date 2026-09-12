#!/usr/bin/env bash
set -Eeuo pipefail

STATUS_NAMESPACE=monitoring
STATUS_CONFIGMAP=sre-telegram-quarterly-audit-status
PORTAL_NAMESPACE=personal-server
RECOVERY_NAMESPACE=sre-recovery-lab
RECOVERY_DEPLOYMENT=sre-pod-recovery
BACKUP_MAX_AGE_SECONDS=${QUARTERLY_SRE_AUDIT_BACKUP_MAX_AGE_SECONDS:-86400}

health_audit=failed
backup_check=failed
recovery_lab=failed
RUN_ID="audit-$$"
run_id_ready=false

if generated_run_id=$(date -u +%Y%m%dT%H%M%SZ 2>/dev/null); then
  RUN_ID="${generated_run_id}-$$"
  run_id_ready=true
fi

configure_client() {
  [[ -n "${KUBERNETES_SERVICE_HOST:-}" ]] || return 1
  [[ -n "${KUBERNETES_SERVICE_PORT_HTTPS:-}" ]] || return 1
  export KUBECONFIG=/tmp/kubeconfig
  cat >"$KUBECONFIG" <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: in-cluster
    cluster:
      certificate-authority: /var/run/secrets/kubernetes.io/serviceaccount/ca.crt
      server: https://${KUBERNETES_SERVICE_HOST}:${KUBERNETES_SERVICE_PORT_HTTPS}
users:
  - name: runner
    user:
      tokenFile: /var/run/secrets/kubernetes.io/serviceaccount/token
contexts:
  - name: in-cluster
    context:
      cluster: in-cluster
      user: runner
current-context: in-cluster
EOF
  chmod 0600 "$KUBECONFIG" || return 1
}

cleanup_recovery_deployment() {
  local replicas pods
  kubectl -n "$RECOVERY_NAMESPACE" scale deployment "$RECOVERY_DEPLOYMENT" --replicas=0 || return 1
  replicas=$(kubectl -n "$RECOVERY_NAMESPACE" get deployment "$RECOVERY_DEPLOYMENT" -o jsonpath='{.spec.replicas}') || return 1
  [[ "$replicas" == 0 ]] || return 1
  kubectl -n "$RECOVERY_NAMESPACE" wait --for=delete pod -l app.kubernetes.io/name=sre-pod-recovery --timeout=30s || return 1
  pods=$(kubectl -n "$RECOVERY_NAMESPACE" get pods -l app.kubernetes.io/name=sre-pod-recovery -o jsonpath='{.items[*].metadata.name}') || return 1
  [[ -z "$pods" ]] || return 1
}

report_status() {
  local overall=failed completed_at payload
  [[ "$run_id_ready" == true ]] || return 1
  if [[ "$health_audit" == passed && "$backup_check" == passed && "$recovery_lab" == passed ]]; then
    overall=passed
  fi
  completed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ) || return 1
  payload=$(printf '{"data":{"run_id":"%s","status":"%s","completed_at":"%s","health_audit":"%s","backup_check":"%s","recovery_lab":"%s","health_check":null,"backup_evidence":null}}' "$RUN_ID" "$overall" "$completed_at" "$health_audit" "$backup_check" "$recovery_lab") || return 1
  kubectl -n "$STATUS_NAMESPACE" patch configmap "$STATUS_CONFIGMAP" --type merge --patch "$payload" >/dev/null 2>&1 || return 1
}

finalize() {
  local result=$1
  trap - EXIT INT TERM
  if ! cleanup_recovery_deployment; then
    recovery_lab=failed
    result=1
  fi
  if ! report_status; then
    result=1
  fi
  exit "$result"
}

trap 'finalize "$?"' EXIT
trap 'finalize 1' INT TERM

run_check() {
  local check_name=$1
  shift
  if "$@" >/dev/null 2>&1; then
    printf -v "$check_name" '%s' passed
  fi
  return 0
}

check_k3s_and_portal() {
  kubectl get nodes --no-headers | grep -q ' Ready ' || return 1
  kubectl -n "$PORTAL_NAMESPACE" get deployment portal-web -o jsonpath='{.status.availableReplicas}' | grep -Eq '^[1-9][0-9]*$' || return 1
}

check_backup_evidence() {
  local evidence completed_at completed_epoch now_epoch completed_count runtime_count
  evidence=$(kubectl -n "$PORTAL_NAMESPACE" get configmap portal-pvc-backup-evidence -o jsonpath='{.data.evidence}') || return 1
  runtime_count=$(printf '%s\n' "$evidence" | grep -cx 'source_runtime=k3s-pvc' || true)
  [[ "$runtime_count" == 1 ]] || return 1
  completed_count=$(printf '%s\n' "$evidence" | grep -c '^backup_completed_at=' || true)
  [[ "$completed_count" == 1 ]] || return 1
  completed_at=$(printf '%s\n' "$evidence" | sed -n 's/^backup_completed_at=//p') || return 1
  [[ -n "$completed_at" ]] || return 1
  completed_epoch=$(date -u -d "$completed_at" +%s) || return 1
  now_epoch=$(date -u +%s) || return 1
  [[ "$completed_epoch" =~ ^[0-9]+$ && "$now_epoch" =~ ^[0-9]+$ ]] || return 1
  (( completed_epoch <= now_epoch )) || return 1
  (( now_epoch - completed_epoch <= BACKUP_MAX_AGE_SECONDS )) || return 1
}

check_recovery_lab() {
  local pod before after deadline
  kubectl -n "$RECOVERY_NAMESPACE" scale deployment "$RECOVERY_DEPLOYMENT" --replicas=1 || return 1
  kubectl -n "$RECOVERY_NAMESPACE" wait --for=condition=Available "deployment/${RECOVERY_DEPLOYMENT}" --timeout=90s || return 1
  pod=$(kubectl -n "$RECOVERY_NAMESPACE" get pods -l app.kubernetes.io/name=sre-pod-recovery -o jsonpath='{.items[0].metadata.name}') || return 1
  [[ -n "$pod" ]] || return 1
  before=$(kubectl -n "$RECOVERY_NAMESPACE" get pod "$pod" -o jsonpath='{.status.containerStatuses[0].restartCount}') || return 1
  [[ "$before" =~ ^[0-9]+$ ]] || return 1
  kubectl -n "$RECOVERY_NAMESPACE" exec "$pod" -- rm /tmp/healthy || return 1
  deadline=$((SECONDS + 90))
  while (( SECONDS < deadline )); do
    pod=$(kubectl -n "$RECOVERY_NAMESPACE" get pods -l app.kubernetes.io/name=sre-pod-recovery -o jsonpath='{.items[0].metadata.name}') || return 1
    [[ -n "$pod" ]] || return 1
    after=$(kubectl -n "$RECOVERY_NAMESPACE" get pod "$pod" -o jsonpath='{.status.containerStatuses[0].restartCount}') || return 1
    [[ "$after" =~ ^[0-9]+$ ]] || return 1
    if (( after > before )); then
      kubectl -n "$RECOVERY_NAMESPACE" wait --for=condition=Ready "pod/${pod}" --timeout=5s >/dev/null || return 1
      kubectl -n "$RECOVERY_NAMESPACE" get events --field-selector "involvedObject.name=${pod}" >/dev/null || return 1
      return 0
    fi
    sleep 2 || return 1
  done
  return 1
}

main() {
  configure_client || return 1
  run_check health_audit check_k3s_and_portal
  run_check backup_check check_backup_evidence
  run_check recovery_lab check_recovery_lab
  [[ "$health_audit" == passed && "$backup_check" == passed && "$recovery_lab" == passed ]] || return 1
}

main_result=0
main || main_result=$?
exit "$main_result"
