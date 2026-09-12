#!/usr/bin/env bash
set -Eeuo pipefail

# Host-side installer for the quarterly K3s SRE audit. It only controls the
# CronJob and legacy user timer; the runner reports audit results through its
# existing ConfigMap contract and this controller never reads or creates Secret
# values.
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../../.." && pwd)
MANIFEST=${QUARTERLY_SRE_AUDIT_MANIFEST:-$REPO_ROOT/infra/k8s/sre-audit-automation/quarterly-sre-audit-cronjob.yaml}
NAMESPACE=monitoring
CRONJOB_NAME=quarterly-sre-audit
STATUS_CONFIGMAP=sre-telegram-quarterly-audit-status
LEGACY_TIMER=${QUARTERLY_SRE_AUDIT_LEGACY_TIMER:-personal-server-quarterly-sre-audit.timer}
LEGACY_SERVICE=${QUARTERLY_SRE_AUDIT_LEGACY_SERVICE:-personal-server-quarterly-sre-audit.service}
MANUAL_JOB_TIMEOUT=${QUARTERLY_SRE_AUDIT_MANUAL_JOB_TIMEOUT:-20m}
INSTALL_LOCK_FILE=${QUARTERLY_SRE_AUDIT_INSTALL_LOCK_FILE:-${XDG_RUNTIME_DIR:-/tmp}/personal-server-quarterly-sre-audit-install.lock}

usage() {
  printf '%s\n' "usage: $0 --preflight|--render|--install|--status" >&2
}

kctl() {
  sudo -n k3s kubectl "$@"
}

require_commands() {
  local command_name
  for command_name in sudo k3s systemctl date flock grep python3; do
    command -v "$command_name" >/dev/null || return 1
  done
}

acquire_install_lock() {
  exec 9>"$INSTALL_LOCK_FILE" || return 1
  flock -n 9 || {
    printf '%s\n' 'Quarterly SRE audit installer is already running; retry is blocked.' >&2
    return 1
  }
}

assert_no_active_audit_jobs() {
  local jobs name active conditions
  jobs=$(kctl -n "$NAMESPACE" get jobs -o 'jsonpath={range .items[*]}{.metadata.name}{"\t"}{.status.active}{"\t"}{range .status.conditions[*]}{.type}{"="}{.status}{","}{end}{"\n"}{end}') || {
    printf '%s\n' 'Quarterly SRE audit Job state could not be read; installation is blocked.' >&2
    return 1
  }
  while IFS=$'\t' read -r name active conditions; do
    [[ "$name" == "${CRONJOB_NAME}-"* ]] || continue
    case "$conditions" in
      *Complete=True,*|*Failed=True,*) continue ;;
    esac
    printf '%s\n' 'A quarterly SRE audit Job is not terminal; installation is blocked.' >&2
    return 1
  done <<< "$jobs"
}

format_completed_at() {
  python3 - "$1" <<'PY'
from datetime import datetime
from zoneinfo import ZoneInfo
import sys

try:
    timestamp = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("timezone is required")
    print(timestamp.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M"))
except (ValueError, IndexError):
    raise SystemExit(1)
PY
}

verify_manifest() {
  [ -r "$MANIFEST" ] || {
    printf '%s\n' 'Quarterly SRE audit CronJob manifest is unavailable.' >&2
    return 1
  }
  grep -Fq 'kind: CronJob' "$MANIFEST" && grep -Eq '^  suspend: true$' "$MANIFEST" || {
    printf '%s\n' 'Quarterly SRE audit CronJob manifest must install suspended.' >&2
    return 1
  }
}

preflight() {
  require_commands || return 1
  verify_manifest || return 1
  kctl apply --dry-run=client -f "$MANIFEST" >/dev/null || return 1
  kctl get nodes --no-headers >/dev/null || return 1
  assert_no_active_audit_jobs || return 1
  printf '%s\n' 'quarterly_sre_audit_preflight=PASS'
}

assert_cronjob_suspended() {
  local suspended
  suspended=$(kctl -n "$NAMESPACE" get cronjob "$CRONJOB_NAME" -o 'jsonpath={.spec.suspend}') || return 1
  [ "$suspended" = true ] || {
    printf '%s\n' 'Quarterly SRE audit CronJob is not suspended; installation is blocked.' >&2
    return 1
  }
}

legacy_timer_load_state() {
  systemctl --user show "$LEGACY_TIMER" --property=LoadState --value
}

legacy_timer_is_inactive() {
  local load_state active_state
  load_state=$(legacy_timer_load_state) || return 1
  [ "$load_state" = not-found ] && return 0
  active_state=$(systemctl --user show "$LEGACY_TIMER" --property=ActiveState --value) || return 1
  [ "$active_state" = inactive ] || {
    printf '%s\n' 'Legacy quarterly SRE audit timer is not inactive; CronJob activation is blocked.' >&2
    return 1
  }
}

disable_legacy_timer_if_present() {
  local load_state
  load_state=$(legacy_timer_load_state) || return 1
  [ "$load_state" = not-found ] && return 0
  systemctl --user disable --now "$LEGACY_TIMER" || return 1
  legacy_timer_is_inactive
}

legacy_service_is_inactive() {
  local load_state active_state
  load_state=$(systemctl --user show "$LEGACY_SERVICE" --property=LoadState --value) || return 1
  [ "$load_state" = not-found ] && return 0
  active_state=$(systemctl --user show "$LEGACY_SERVICE" --property=ActiveState --value) || return 1
  [ "$active_state" = inactive ] || {
    printf '%s\n' 'Legacy quarterly SRE audit service is active; installation is blocked without stopping it.' >&2
    return 1
  }
}

ensure_status_configmap_if_absent() {
  local existing
  existing=$(kctl -n "$NAMESPACE" get configmap "$STATUS_CONFIGMAP" --ignore-not-found -o name) || {
    printf '%s\n' 'Quarterly SRE audit status ConfigMap could not be read.' >&2
    return 1
  }
  [ -n "$existing" ] && return 0
  if kctl -n "$NAMESPACE" create configmap "$STATUS_CONFIGMAP" \
    --from-literal=run_id= \
    --from-literal=status= \
    --from-literal=completed_at= \
    --from-literal=health_audit= \
    --from-literal=backup_check= \
    --from-literal=recovery_lab=; then
    return 0
  fi
  existing=$(kctl -n "$NAMESPACE" get configmap "$STATUS_CONFIGMAP" --ignore-not-found -o name) || {
    printf '%s\n' 'Quarterly SRE audit status ConfigMap could not be re-read after create.' >&2
    return 1
  }
  [ -n "$existing" ] || {
    printf '%s\n' 'Quarterly SRE audit status ConfigMap was not created.' >&2
    return 1
  }
}

create_manual_job() {
  local timestamp job_name
  timestamp=$(date -u +%Y%m%d%H%M%S) || return 1
  job_name="${CRONJOB_NAME}-manual-${timestamp}-$$"
  kctl -n "$NAMESPACE" create job "$job_name" --from="cronjob/$CRONJOB_NAME" || return 1
  kctl -n "$NAMESPACE" wait --for=condition=complete "job/$job_name" --timeout="$MANUAL_JOB_TIMEOUT" || return 1
  [ "$(kctl -n "$NAMESPACE" get job "$job_name" -o 'jsonpath={.status.succeeded}')" = 1 ] || {
    printf '%s\n' 'Quarterly SRE audit manual Job did not report success.' >&2
    return 1
  }
}

verify_status_reporting() {
  local reported_status
  reported_status=$(kctl -n "$NAMESPACE" get configmap "$STATUS_CONFIGMAP" -o 'jsonpath={.data.status}') || return 1
  [ "$reported_status" = passed ] || {
    printf '%s\n' 'Quarterly SRE audit status reporting is not confirmed passed; CronJob activation is blocked.' >&2
    return 1
  }
}

render() {
  verify_manifest
  kctl apply --dry-run=client -f "$MANIFEST"
}

install() {
  acquire_install_lock || return 1
  preflight || return 1
  kctl apply -f "$MANIFEST" || return 1
  assert_cronjob_suspended || return 1
  ensure_status_configmap_if_absent || return 1
  disable_legacy_timer_if_present || return 1
  legacy_service_is_inactive || return 1
  assert_no_active_audit_jobs || return 1
  create_manual_job || return 1
  verify_status_reporting || return 1
  kctl -n "$NAMESPACE" patch cronjob "$CRONJOB_NAME" --type merge -p '{"spec":{"suspend":false}}' || return 1
  printf '%s\n' 'quarterly_sre_audit_install=PASS'
}

status() {
  local suspended report run_id audit_status completed_at display_completed_at health_audit backup_check recovery_lab
  suspended=$(kctl -n "$NAMESPACE" get cronjob "$CRONJOB_NAME" -o 'jsonpath={.spec.suspend}') || return 1
  report=$(kctl -n "$NAMESPACE" get configmap "$STATUS_CONFIGMAP" -o 'jsonpath={.data.run_id}{"\t"}{.data.status}{"\t"}{.data.completed_at}{"\t"}{.data.health_audit}{"\t"}{.data.backup_check}{"\t"}{.data.recovery_lab}') || return 1
  IFS=$'\t' read -r run_id audit_status completed_at health_audit backup_check recovery_lab \
    <<< "$report" || return 1
  display_completed_at=$(format_completed_at "$completed_at") || return 1
  printf 'cronjob_suspended=%s\nrun_id=%s\nstatus=%s\ncompleted_at=%s\nhealth_audit=%s\nbackup_check=%s\nrecovery_lab=%s\n' \
    "$suspended" "$run_id" "$audit_status" "$display_completed_at" "$health_audit" "$backup_check" "$recovery_lab"
  legacy_timer_is_inactive
  legacy_service_is_inactive
}

case "${1:-}" in
  --preflight) preflight ;;
  --render) render ;;
  --install) install ;;
  --status) status ;;
  *) usage; exit 2 ;;
esac
