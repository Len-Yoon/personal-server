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
MANUAL_JOB_TIMEOUT=${QUARTERLY_SRE_AUDIT_MANUAL_JOB_TIMEOUT:-20m}

usage() {
  printf '%s\n' "usage: $0 --preflight|--render|--install|--status" >&2
}

kctl() {
  sudo -n k3s kubectl "$@"
}

require_commands() {
  local command_name
  for command_name in sudo k3s systemctl date grep; do
    command -v "$command_name" >/dev/null || return 1
  done
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
  preflight || return 1
  kctl apply -f "$MANIFEST" || return 1
  assert_cronjob_suspended || return 1
  disable_legacy_timer_if_present || return 1
  create_manual_job || return 1
  verify_status_reporting || return 1
  kctl -n "$NAMESPACE" patch cronjob "$CRONJOB_NAME" --type merge -p '{"spec":{"suspend":false}}' || return 1
  printf '%s\n' 'quarterly_sre_audit_install=PASS'
}

status() {
  kctl -n "$NAMESPACE" get cronjob "$CRONJOB_NAME"
  kctl -n "$NAMESPACE" get configmap "$STATUS_CONFIGMAP"
  legacy_timer_is_inactive
}

case "${1:-}" in
  --preflight) preflight ;;
  --render) render ;;
  --install) install ;;
  --status) status ;;
  *) usage; exit 2 ;;
esac
