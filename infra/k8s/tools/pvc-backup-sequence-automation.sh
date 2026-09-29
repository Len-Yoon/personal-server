#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
UNIT_DIR=${PVC_BACKUP_SEQUENCE_UNIT_DIR:-$HOME/.config/systemd/user}
SERVICE=personal-server-pvc-backup-sequence.service
TIMER=personal-server-pvc-backup-sequence.timer
TEMPLATE_DIR=$ROOT/infra/k8s/backup-automation
KCTL=(sudo -n k3s kubectl -n personal-server)

usage() {
  printf '%s\n' "usage: $0 --preflight|--install|--activate|--status|--deactivate" >&2
}

preflight() {
  local command_name linger
  for command_name in systemctl systemd-analyze loginctl python3 sudo k3s; do
    command -v "$command_name" >/dev/null || return 1
  done
  systemctl --user show-environment >/dev/null || return 1
  linger=$(loginctl show-user "$USER" -p Linger --value) || return 1
  [ "$linger" = yes ] || { printf '%s\n' 'user_linger=required' >&2; return 1; }
  "${KCTL[@]}" get nodes --no-headers >/dev/null || return 1
  printf '%s\n' 'pvc_backup_sequence_preflight=PASS'
}

render_unit() {
  local template=$1 output=$2
  sed "s|@REPO_ROOT@|$ROOT|g" "$TEMPLATE_DIR/$template" > "$output"
  chmod 600 "$output"
}

install_units() {
  local temporary_dir service_file timer_file
  preflight || return 1
  if systemctl --user is-active --quiet "$TIMER"; then
    printf '%s\n' 'active_timer_cannot_be_reinstalled' >&2
    return 1
  fi
  mkdir -p -- "$UNIT_DIR"
  chmod 700 "$UNIT_DIR"
  temporary_dir=$(mktemp -d "$UNIT_DIR/.pvc-backup-sequence.XXXXXX")
  trap 'rm -rf -- "$temporary_dir"' RETURN
  service_file=$temporary_dir/$SERVICE
  timer_file=$temporary_dir/$TIMER
  render_unit pvc-backup-sequence.service.tmpl "$service_file"
  render_unit pvc-backup-sequence.timer.tmpl "$timer_file"
  systemd-analyze --user verify "$service_file" "$timer_file" || return 1
  mv -- "$service_file" "$UNIT_DIR/$SERVICE"
  mv -- "$timer_file" "$UNIT_DIR/$TIMER"
  systemctl --user daemon-reload
  printf '%s\n' 'pvc_backup_sequence_install=PASS timer=disabled'
}

assert_safe_activation() {
  local name suspended deadline legacy_enabled legacy_active legacy_service_active legacy_service_pid existing_activation
  legacy_enabled=$(systemctl --user is-enabled personal-server-portal-pvc-backup.timer 2>/dev/null || true)
  legacy_active=$(systemctl --user is-active personal-server-portal-pvc-backup.timer 2>/dev/null || true)
  case "$legacy_enabled" in disabled|masked|not-found) ;; *) return 1 ;; esac
  case "$legacy_active" in inactive|unknown) ;; *) return 1 ;; esac
  legacy_service_active=$(systemctl --user show personal-server-portal-pvc-backup.service --property=ActiveState --value) || return 1
  legacy_service_pid=$(systemctl --user show personal-server-portal-pvc-backup.service --property=MainPID --value) || return 1
  [ "$legacy_service_active" = inactive ] || [ "$legacy_service_active" = failed ] || return 1
  [ "$legacy_service_pid" = 0 ] || return 1
  systemctl --user is-active --quiet "$SERVICE" && return 1
  "${KCTL[@]}" get configmap pvc-backup-sequence-state -o name >/dev/null || return 1
  existing_activation=$("${KCTL[@]}" get configmap pvc-backup-sequence-state -o 'jsonpath={.data.active_from}') || return 1
  [ -z "$existing_activation" ] || { printf '%s\n' 'sequence_already_activated' >&2; return 1; }
  for name in portal-pvc-backup book-pvc-backup youtube-pvc-backup crawler-pvc-backup; do
    suspended=$("${KCTL[@]}" get cronjob "$name" -o 'jsonpath={.spec.suspend}') || return 1
    [ "$suspended" = true ] || { printf 'backup_cronjob_still_active=%s\n' "$name" >&2; return 1; }
    deadline=$("${KCTL[@]}" get cronjob "$name" -o 'jsonpath={.spec.startingDeadlineSeconds}') || return 1
    [ "$deadline" = 300 ] || { printf 'backup_cronjob_start_deadline_mismatch=%s\n' "$name" >&2; return 1; }
  done
  assert_no_active_backup_jobs || return 1
  python3 "$ROOT/infra/k8s/tools/pvc-backup-sequence.py" --check || return 1
  python3 "$ROOT/infra/k8s/tools/pvc-backup-sequence.py" --check-lock || return 1
}

assert_no_active_backup_jobs() {
  "${KCTL[@]}" get jobs -o json | python3 -c '
import json, sys
data = json.load(sys.stdin)
names = ("portal-pvc-backup-", "book-pvc-backup-", "youtube-pvc-backup-", "crawler-pvc-backup-", "pvc-backup-seq-")
accounts = {"portal-pvc-backup", "book-pvc-backup", "youtube-pvc-backup", "crawler-pvc-backup"}
def is_backup(item):
    name = item.get("metadata", {}).get("name", "")
    service_account = item.get("spec", {}).get("template", {}).get("spec", {}).get("serviceAccountName")
    return name.startswith(names) or service_account in accounts
def is_terminal(item):
    return any(condition.get("type") in {"Complete", "Failed"} and condition.get("status") == "True"
               for condition in item.get("status", {}).get("conditions", []))
if any(not is_terminal(item) for item in data.get("items", []) if is_backup(item)):
    sys.exit("active backup Job exists")
' || return 1
}

activate() {
  local active_from
  preflight || return 1
  [ -f "$UNIT_DIR/$SERVICE" ] && [ -f "$UNIT_DIR/$TIMER" ] || return 1
  assert_safe_activation || return 1
  active_from=$(python3 -c '
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
now = datetime.now(ZoneInfo("Asia/Seoul"))
minute_of_day = now.hour * 60 + now.minute
if 25 <= minute_of_day < 36:
    raise SystemExit("activation is too close to the 00:30 backup window")
print((now.date() + timedelta(days=now.hour * 60 + now.minute >= 30)).isoformat())
') || return 1
  systemctl --user enable --now "$TIMER" || return 1
  if ! "${KCTL[@]}" patch configmap pvc-backup-sequence-state --type=merge \
      -p "{\"data\":{\"active_from\":\"$active_from\",\"deactivated_at\":null}}" >/dev/null; then
    systemctl --user disable --now "$TIMER" || true
    return 1
  fi
  if ! systemctl --user is-active --quiet "$TIMER"; then
    systemctl --user disable --now "$TIMER" || true
    local deactivated_at
    deactivated_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    "${KCTL[@]}" patch configmap pvc-backup-sequence-state --type=merge \
      -p "{\"data\":{\"active_from\":null,\"deactivated_at\":\"$deactivated_at\"}}" >/dev/null || true
    return 1
  fi
  printf '%s\n' 'pvc_backup_sequence_activation=PASS'
}

status() {
  systemctl --user is-enabled "$TIMER" || true
  systemctl --user is-active "$TIMER" || true
  systemctl --user list-timers "$TIMER" --no-pager
}

deactivate() {
  local deactivated_at service_active service_pid sequence_status
  systemctl --user disable --now "$TIMER"
  service_active=$(systemctl --user show "$SERVICE" --property=ActiveState --value) || return 1
  service_pid=$(systemctl --user show "$SERVICE" --property=MainPID --value) || return 1
  if { [ "$service_active" != inactive ] && [ "$service_active" != failed ]; } || \
      [ "$service_pid" != 0 ] || ! assert_no_active_backup_jobs; then
    printf '%s\n' 'sequence_still_running_timer_disabled_state_monitoring_preserved' >&2
    return 1
  fi
  sequence_status=$("${KCTL[@]}" get configmap pvc-backup-sequence-state -o 'jsonpath={.data.status}') || return 1
  case "$sequence_status" in ''|completed|blocked) ;; *)
    printf '%s\n' 'sequence_result_not_terminal_timer_disabled_state_monitoring_preserved' >&2
    return 1 ;;
  esac
  deactivated_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  "${KCTL[@]}" patch configmap pvc-backup-sequence-state --type=merge \
    -p "{\"data\":{\"active_from\":null,\"deactivated_at\":\"$deactivated_at\"}}" >/dev/null || return 1
  printf '%s\n' 'pvc_backup_sequence_activation=OFF existing_jobs_unchanged=true'
}

case "${1:-}" in
  --preflight) preflight ;;
  --install) install_units ;;
  --activate) activate ;;
  --status) status ;;
  --deactivate) deactivate ;;
  *) usage; exit 2 ;;
esac
