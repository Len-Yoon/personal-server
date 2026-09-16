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
# Allow ConfigMap volume projection to propagate before liveness recovery completes.
RECOVERY_TIMEOUT_SECONDS=${QUARTERLY_SRE_AUDIT_RECOVERY_TIMEOUT_SECONDS:-180}
RECOVERY_POLL_INTERVAL_SECONDS=${QUARTERLY_SRE_AUDIT_RECOVERY_POLL_INTERVAL_SECONDS:-2}
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

health_audit=failed
backup_check=failed
recovery_lab=failed
slo_evidence=unobservable
slo_days_recorded=0
slo_days_ok=0
slo_days_unobservable=0
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
      payload=$(printf '{"data":{"run_id":"%s","status":"%s","completed_at":"%s","health_audit":"%s","backup_check":"%s","recovery_lab":"%s","slo_evidence":"%s","slo_days_recorded":"%s","slo_days_ok":"%s","slo_days_unobservable":"%s","health_check":null,"backup_evidence":null}}' "$RUN_ID" "$overall" "$completed_at" "$health_audit" "$backup_check" "$recovery_lab" "$slo_evidence" "$slo_days_recorded" "$slo_days_ok" "$slo_days_unobservable") || return 1
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

read_slo_evidence() {
  local evidence_file summary
  evidence_file=$(mktemp /tmp/monthly-slo-evidence.XXXXXX) || return 1
  chmod 0600 "$evidence_file" || { rm -f -- "$evidence_file"; return 1; }
  if ! kubectl -n monitoring get configmap slo-daily-evidence \
    -o jsonpath='{.data.records\.json}' --request-timeout=10s >"$evidence_file" 2>/dev/null; then
    rm -f -- "$evidence_file"
    return 1
  fi
  if ! summary=$(python3 - "$evidence_file" 2>/dev/null <<'PY'
from datetime import date, datetime, timedelta, timezone
import json
import math
import re
import sys
from zoneinfo import ZoneInfo

FIELDS = {"date", "collected_at", "overall", "public_health", "portal_ready", "portal_http", "crawler_freshness", "missing"}
SOURCES = {"public_health", "portal_ready", "portal_http", "crawler_freshness"}
STATUS_FIELDS = ("public_health", "portal_ready", "crawler_freshness")
HTTP_FIELDS = {"requests", "server_errors", "server_error_ratio", "p95_seconds"}


def require(condition):
    if not condition:
        raise ValueError("invalid evidence")


def number(value):
    require(type(value) in (int, float) and math.isfinite(value) and value >= 0)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        raw = stream.read(1048577)
    require(len(raw) <= 1048576)
    records = json.loads(raw, object_pairs_hook=unique_object)
    require(isinstance(records, list) and len(records) <= 30)
    dates = set()
    ok_days = unknown_days = 0
    for record in records:
        require(isinstance(record, dict) and set(record) == FIELDS)
        day = record["date"]
        require(isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day))
        date.fromisoformat(day)
        require(day not in dates)
        dates.add(day)
        timestamp = record["collected_at"]
        require(isinstance(timestamp, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)", timestamp))
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        require(parsed.utcoffset() == timezone.utc.utcoffset(parsed))
        require(record["overall"] in ("ok", "unobservable"))
        for field in STATUS_FIELDS:
            require(record[field] in ("ok", "failed", "unobservable"))
        missing = record["missing"]
        require(isinstance(missing, list) and all(isinstance(source, str) and source in SOURCES for source in missing))
        require(len(set(missing)) == len(missing))
        require((record["overall"] == "ok") == (not missing))
        for field in STATUS_FIELDS:
            require((record[field] == "unobservable") == (field in missing))
        http = record["portal_http"]
        require((http is None) == ("portal_http" in missing))
        if http is not None:
            require(isinstance(http, dict) and set(http) == HTTP_FIELDS)
            number(http["requests"])
            number(http["server_errors"])
            require(http["server_errors"] <= http["requests"])
            if http["requests"] == 0:
                require(http["server_error_ratio"] is None and http["p95_seconds"] is None)
            else:
                number(http["server_error_ratio"])
                number(http["p95_seconds"])
                require(http["server_error_ratio"] <= 1)
        unknown_days += bool(missing)
        ok_days += not missing and all(record[field] == "ok" for field in STATUS_FIELDS)
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    required_dates = {(today - timedelta(days=offset)).isoformat() for offset in range(30)}
    result = "unobservable" if dates != required_dates or unknown_days else "passed" if ok_days == 30 else "failed"
    print(result, len(records), ok_days, unknown_days)
except (ValueError, TypeError, KeyError, OSError, OverflowError, RecursionError):
    sys.exit(1)
PY
  ); then
    rm -f -- "$evidence_file"
    return 1
  fi
  rm -f -- "$evidence_file"
  read -r slo_evidence slo_days_recorded slo_days_ok slo_days_unobservable <<<"$summary"
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
  # Coverage is reporting evidence, independent of existing audit success gates.
  if [[ "$REPORT_MODE" == official ]]; then
    read_slo_evidence || true
  fi
  run_check health_audit check_k3s_and_portal
  run_check backup_check check_backup_evidence
  run_check recovery_lab check_recovery_lab
  [[ "$health_audit" == passed && "$backup_check" == passed && "$recovery_lab" == passed ]] || return 1
}

main_result=0
main || main_result=$?
exit "$main_result"
