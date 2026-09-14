#!/usr/bin/env bash
set -Eeuo pipefail

STATUS_NAMESPACE=monitoring
STATUS_CONFIGMAP=sre-telegram-quarterly-audit-status
REPORT_MODE=${QUARTERLY_SRE_AUDIT_REPORT_MODE:-official}
DIAGNOSTICS_CONFIGMAP=${QUARTERLY_SRE_AUDIT_DIAGNOSTICS_CONFIGMAP:-sre-quarterly-audit-diagnostics}
PORTAL_NAMESPACE=personal-server
RECOVERY_NAMESPACE=sre-recovery-lab
RECOVERY_DEPLOYMENT=sre-pod-recovery
RECOVERY_TRIGGER_CONFIGMAP=sre-pod-recovery-trigger
BACKUP_MAX_AGE_SECONDS=${QUARTERLY_SRE_AUDIT_BACKUP_MAX_AGE_SECONDS:-86400}
RECOVERY_TIMEOUT_SECONDS=${QUARTERLY_SRE_AUDIT_RECOVERY_TIMEOUT_SECONDS:-90}
RECOVERY_POLL_INTERVAL_SECONDS=${QUARTERLY_SRE_AUDIT_RECOVERY_POLL_INTERVAL_SECONDS:-2}
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

health_audit=failed
backup_check=failed
recovery_lab=failed
validation_failure_stage=none
cleanup_status=not_run
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

patch_recovery_trigger() {
  local trigger=$1
  [[ "$trigger" == true || "$trigger" == false ]] || return 1
  kubectl -n "$RECOVERY_NAMESPACE" patch configmap "$RECOVERY_TRIGGER_CONFIGMAP" \
    --type merge --patch "{\"data\":{\"trigger\":\"${trigger}\"}}" --request-timeout=10s >/dev/null 2>&1 || return 1
}

cleanup_recovery_deployment() {
  local replicas
  kubectl -n "$RECOVERY_NAMESPACE" scale deployment "$RECOVERY_DEPLOYMENT" --replicas=0 || return 1
  replicas=$(kubectl -n "$RECOVERY_NAMESPACE" get deployment "$RECOVERY_DEPLOYMENT" -o jsonpath='{.spec.replicas}') || return 1
  # Kubernetes terminates Pods asynchronously after this verified cleanup request.
  [[ "$replicas" == 0 ]] || return 1
}

wait_for_recovery_available() {
  local state available deadline
  deadline=$((SECONDS + 90))
  while (( SECONDS < deadline )); do
    state=$(kubectl -n "$RECOVERY_NAMESPACE" get deployment "$RECOVERY_DEPLOYMENT" -o jsonpath='{.status.conditions[?(@.type=="Available")].status}:{.status.availableReplicas}') || return 1
    available=${state#*:}
    if [[ "${state%%:*}" == True && "$available" =~ ^[1-9][0-9]*$ ]]; then
      return 0
    fi
    sleep 2 || return 1
  done
  return 1
}

RECOVERY_POD_NAME=
RECOVERY_POD_UID=
RECOVERY_POD_READY=
RECOVERY_CONTAINER_ID=
RECOVERY_RESTART_COUNT=

read_recovery_pod_snapshot() {
  local pods_text raw candidate uid phase deletion_timestamp ready statuses status_name status_id status_restarts extra
  local selected_pod= selected_uid= selected_ready= selected_statuses= selected_phase= selected_deletion_timestamp= live_count=0
  local -a pods=()

  pods_text=$(kubectl -n "$RECOVERY_NAMESPACE" get pods -l app.kubernetes.io/name=sre-pod-recovery -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}') || return 1
  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] && pods+=("$candidate")
  done <<<"$pods_text"
  for candidate in "${pods[@]}"; do
    raw=$(kubectl -n "$RECOVERY_NAMESPACE" get pod "$candidate" -o jsonpath='{.metadata.uid}{"|"}{.status.phase}{"|"}{.metadata.deletionTimestamp}{"|"}{.status.conditions[?(@.type=="Ready")].status}{"|"}{range .status.containerStatuses[*]}{.name}{"="}{.containerID}{"="}{.restartCount}{";"}{end}') || return 1
    IFS='|' read -r uid phase deletion_timestamp ready statuses extra <<<"$raw"
    [[ -z "$extra" && -n "$uid" ]] || return 1
    [[ "$phase" == Running && -z "$deletion_timestamp" ]] || continue
    live_count=$((live_count + 1))
    selected_pod=$candidate
    selected_uid=$uid
    selected_ready=$ready
    selected_statuses=$statuses
    selected_phase=$phase
    selected_deletion_timestamp=$deletion_timestamp
  done
  (( live_count <= 1 )) || return 1
  [[ "$live_count" == 1 ]] || return 2
  pod=$selected_pod
  uid=$selected_uid
  ready=$selected_ready
  statuses=$selected_statuses
  [[ "$selected_phase" == Running && -z "$selected_deletion_timestamp" ]] || return 1
  [[ "$ready" == True || "$ready" == False ]] || return 2

  statuses=${statuses%;}
  [[ -n "$statuses" && "$statuses" != *';'* ]] || return 2
  IFS='=' read -r status_name status_id status_restarts extra <<<"$statuses"
  [[ -z "$extra" && -n "$status_name" && -n "$status_id" && "$status_restarts" =~ ^[0-9]+$ ]] || return 2

  RECOVERY_POD_NAME=$pod
  RECOVERY_POD_UID=$uid
  RECOVERY_POD_READY=$ready
  RECOVERY_CONTAINER_ID=$status_id
  RECOVERY_RESTART_COUNT=$status_restarts
}

wait_for_recovery_pod_baseline() {
  local deadline
  deadline=$((SECONDS + RECOVERY_TIMEOUT_SECONDS))
  while (( SECONDS < deadline )); do
    if read_recovery_pod_snapshot && [[ "$RECOVERY_POD_READY" == True ]]; then
      return 0
    fi
    sleep "$RECOVERY_POLL_INTERVAL_SECONDS" || return 1
  done
  return 1
}

report_status() {
  local overall=failed completed_at payload
  [[ "$run_id_ready" == true ]] || return 1
  if [[ "$health_audit" == passed && "$backup_check" == passed && "$recovery_lab" == passed ]]; then
    overall=passed
  fi
  completed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ) || return 1
  case "$REPORT_MODE" in
    official)
      payload=$(printf '{"data":{"run_id":"%s","status":"%s","completed_at":"%s","health_audit":"%s","backup_check":"%s","recovery_lab":"%s","health_check":null,"backup_evidence":null}}' "$RUN_ID" "$overall" "$completed_at" "$health_audit" "$backup_check" "$recovery_lab") || return 1
      kubectl -n "$STATUS_NAMESPACE" patch configmap "$STATUS_CONFIGMAP" --type merge --patch "$payload" >/dev/null 2>&1 || return 1
      ;;
    validation)
      payload=$(printf '{"data":{"run_id":"%s","result":"%s","failure_stage":"%s","cleanup_status":"%s","completed_at":"%s"}}' "$RUN_ID" "$overall" "$validation_failure_stage" "$cleanup_status" "$completed_at") || return 1
      kubectl -n "$STATUS_NAMESPACE" patch configmap "$DIAGNOSTICS_CONFIGMAP" --type merge --patch "$payload" >/dev/null 2>&1 || return 1
      ;;
    *)
      return 1
      ;;
  esac
}

finalize() {
  local result=$1
  trap - EXIT INT TERM
  cleanup_status=passed
  if ! patch_recovery_trigger false; then
    cleanup_status=failed
    recovery_lab=failed
    validation_failure_stage=cleanup_trigger_reset
    printf 'quarterly_sre_audit_check=recovery_lab result=failed stage=cleanup_trigger_reset\n' || true
    result=1
  fi
  if ! cleanup_recovery_deployment; then
    cleanup_status=failed
    recovery_lab=failed
    validation_failure_stage=cleanup_scale_down
    printf 'quarterly_sre_audit_check=recovery_lab result=failed stage=cleanup_scale_down\n' || true
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
  check_failure_stage=check
  if "$@" >/dev/null 2>&1; then
    printf -v "$check_name" '%s' passed
  else
    validation_failure_stage="${check_name}:${check_failure_stage}"
    printf 'quarterly_sre_audit_check=%s result=failed stage=%s\n' "$check_name" "$check_failure_stage" || true
  fi
  return 0
}

check_k3s_and_portal() {
  kubectl get nodes --no-headers | grep -q ' Ready ' || return 1
  kubectl -n "$PORTAL_NAMESPACE" get deployment portal-web -o jsonpath='{.status.availableReplicas}' | grep -Eq '^[1-9][0-9]*$' || return 1
}

check_backup_evidence() {
  local evidence_file runtime_count
  evidence_file=$(mktemp /tmp/quarterly-sre-audit-evidence.XXXXXX) || return 1
  chmod 0600 "$evidence_file" || { rm -f -- "$evidence_file"; return 1; }
  if ! kubectl -n "$PORTAL_NAMESPACE" get configmap portal-pvc-backup-evidence -o jsonpath='{.data.evidence}' >"$evidence_file"; then
    rm -f -- "$evidence_file"
    return 1
  fi
  if ! python3 "$SCRIPT_DIR/validate-backup-evidence.py" --evidence "$evidence_file" --max-age-seconds "$BACKUP_MAX_AGE_SECONDS" >/dev/null; then
    rm -f -- "$evidence_file"
    return 1
  fi
  runtime_count=$(grep -cx 'source_runtime=k3s-pvc' "$evidence_file" || true)
  rm -f -- "$evidence_file"
  [[ "$runtime_count" == 1 ]]
}

check_recovery_lab() {
  local pod uid container_id before deadline snapshot_result
  local observed_restart=false
  check_failure_stage=trigger_reset
  patch_recovery_trigger false || return 1
  check_failure_stage=scale
  kubectl -n "$RECOVERY_NAMESPACE" scale deployment "$RECOVERY_DEPLOYMENT" --replicas=1 || return 1
  check_failure_stage=availability
  wait_for_recovery_available || return 1
  check_failure_stage=pod_selection
  wait_for_recovery_pod_baseline || return 1
  pod=$RECOVERY_POD_NAME
  uid=$RECOVERY_POD_UID
  container_id=$RECOVERY_CONTAINER_ID
  before=$RECOVERY_RESTART_COUNT
  check_failure_stage=trigger_activate
  patch_recovery_trigger true || return 1
  check_failure_stage=recovery_transition
  deadline=$((SECONDS + RECOVERY_TIMEOUT_SECONDS))
  while (( SECONDS < deadline )); do
    if read_recovery_pod_snapshot; then
      [[ "$RECOVERY_POD_NAME" == "$pod" && "$RECOVERY_POD_UID" == "$uid" ]] || return 1
      if (( RECOVERY_RESTART_COUNT > before )) && [[ "$RECOVERY_CONTAINER_ID" != "$container_id" ]]; then
        observed_restart=true
      fi
      if [[ "$observed_restart" == true && "$RECOVERY_POD_READY" == True ]]; then
        check_failure_stage=events
        kubectl -n "$RECOVERY_NAMESPACE" get events --field-selector "involvedObject.name=${pod}" >/dev/null || return 1
        return 0
      fi
    else
      snapshot_result=$?
      # Container status data is briefly empty while kubelet publishes a restart.
      [[ "$snapshot_result" == 2 ]] || return 1
    fi
    sleep "$RECOVERY_POLL_INTERVAL_SECONDS" || return 1
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
