#!/usr/bin/env bash
set -Eeuo pipefail

# Host-only controller for the Portal PVC backup verifier.  Credentials stay in
# systemd's encrypted credential store; this script receives only their paths.
umask 077
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../../.." && pwd)
BACKUP_TOOL=${PORTAL_BACKUP_TOOL:-$SCRIPT_DIR/portal-pvc-backup-verify.sh}
STATE_DIR=${PORTAL_BACKUP_AUTOMATION_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/personal-server/portal-pvc-backup}
CREDENTIAL_DIR=${PORTAL_BACKUP_AUTOMATION_CREDENTIAL_DIR:-$HOME/.config/systemd/user/credentials/personal-server-portal-pvc-backup}
UNIT_DIR=${PORTAL_BACKUP_AUTOMATION_UNIT_DIR:-$HOME/.config/systemd/user}
SERVICE_NAME=personal-server-portal-pvc-backup.service
TIMER_NAME=personal-server-portal-pvc-backup.timer
CONFIGMAP_NAME=sre-telegram-backup-status
CONFIGMAP_NAMESPACE=monitoring
SERVICE_TEMPLATE=$REPO_ROOT/infra/k8s/backup-automation/personal-server-portal-pvc-backup.service.tmpl
TIMER_TEMPLATE=$REPO_ROOT/infra/k8s/backup-automation/personal-server-portal-pvc-backup.timer.tmpl
RUN_LOG=''

usage() {
  printf '%s\n' "usage: $0 --preflight|--enroll|--install|--run|--status|--uninstall" >&2
}

safe_state_dir() {
  case "$STATE_DIR" in
    /mnt/c|/mnt/c/*|/tmp|/tmp/*) return 1 ;;
  esac
  mkdir -p -- "$STATE_DIR" || return 1
  chmod 700 "$STATE_DIR" || return 1
  [ "$(findmnt -n -o FSTYPE -T "$STATE_DIR" 2>/dev/null)" = ext4 ]
}

require_commands() {
  local command_name
  for command_name in systemctl systemd-creds systemd-ask-password rclone sudo k3s findmnt; do
    command -v "$command_name" >/dev/null || return 1
  done
}

preflight() {
  require_commands || return 1
  safe_state_dir || return 1
  systemctl --user show-environment >/dev/null || return 1
  sudo -n k3s kubectl get nodes --no-headers >/dev/null || return 1
  printf '%s\n' 'portal_pvc_backup_automation_preflight=PASS'
}

credential_path() {
  printf '%s/%s.cred' "$CREDENTIAL_DIR" "$1"
}

enroll() {
  preflight || return 1
  [ -r "${PORTAL_RCLONE_SOURCE_CONFIG:-$HOME/.config/rclone/rclone.conf}" ] || return 1
  mkdir -p -- "$CREDENTIAL_DIR"
  chmod 700 "$CREDENTIAL_DIR"
  systemd-creds encrypt --with-key=host --name=rclone-config \
    "${PORTAL_RCLONE_SOURCE_CONFIG:-$HOME/.config/rclone/rclone.conf}" "$(credential_path rclone-config)"
  systemd-ask-password --id=personal-server-portal-pvc-backup-rclone-config-passphrase \
    'rclone 설정 암호를 입력하세요.' | \
    systemd-creds encrypt --with-key=host --name=rclone-config-passphrase - "$(credential_path rclone-config-passphrase)"
  chmod 600 "$(credential_path rclone-config)" "$(credential_path rclone-config-passphrase)"
  printf '%s\n' 'portal_pvc_backup_automation_enroll=PASS'
}

render_template() {
  local template=$1 output=$2
  sed \
    -e "s|@REPO_ROOT@|$REPO_ROOT|g" \
    -e "s|@STATE_DIR@|$STATE_DIR|g" \
    -e "s|@CREDENTIAL_DIR@|$CREDENTIAL_DIR|g" \
    "$template" > "$output"
  chmod 600 "$output"
}

install_units() {
  preflight || return 1
  [ -r "$(credential_path rclone-config)" ] || return 1
  [ -r "$(credential_path rclone-config-passphrase)" ] || return 1
  mkdir -p -- "$UNIT_DIR"
  chmod 700 "$UNIT_DIR"
  render_template "$SERVICE_TEMPLATE" "$UNIT_DIR/$SERVICE_NAME"
  render_template "$TIMER_TEMPLATE" "$UNIT_DIR/$TIMER_NAME"
  systemctl --user daemon-reload
  systemctl --user enable --now "$TIMER_NAME"
  printf '%s\n' 'portal_pvc_backup_automation_install=PASS'
}

utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
run_id() { printf '%s-%s\n' "$(date -u +%Y%m%dT%H%M%SZ)" "$$"; }

safe_stage() {
  local stage=${1//_/-}
  case "$stage" in
    [a-z]* ) ;;
    * ) return 1 ;;
  esac
  [[ "$stage" =~ ^[a-z][a-z0-9-]{0,63}$ ]] || return 1
  printf '%s' "$stage"
}

classify_backup() {
  local backup_status=$1 stage=$2 upload=$3
  if [ "$backup_status" -eq 0 ]; then
    if [ "$upload" = SKIPPED_UNCHANGED ]; then
      printf '%s %s\n' unchanged unchanged
    else
      printf '%s %s\n' completed completed
    fi
    return
  fi
  case "$stage" in
    restore_validation|remote_restore|portal_readiness|portal_health)
      printf '%s %s\n' restore_failed "$(safe_stage "$stage" || printf '%s' restore-failed)" ;;
    *)
      printf '%s %s\n' failed "$(safe_stage "$stage" || printf '%s' backup-failed)" ;;
  esac
}

report_status() {
  local report_run_id=$1 status=$2 completed_at=$3 stage=$4
  case "$status" in completed|unchanged|failed|restore_failed) ;; *) return 1 ;; esac
  [[ "$report_run_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || return 1
  [[ "$completed_at" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] || return 1
  safe_stage "$stage" >/dev/null || return 1
  sudo -n k3s kubectl -n "$CONFIGMAP_NAMESPACE" apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: $CONFIGMAP_NAME
  namespace: $CONFIGMAP_NAMESPACE
data:
  run_id: "$report_run_id"
  status: "$status"
  completed_at: "$completed_at"
  stage: "$stage"
EOF
}

cleanup_run_log() {
  [ -z "$RUN_LOG" ] || rm -f -- "$RUN_LOG"
}

run_once() {
  safe_state_dir || return 1
  RUN_LOG=$(mktemp "$STATE_DIR/.portal-pvc-backup-run.XXXXXX")
  trap cleanup_run_log RETURN
  local backup_exit stage upload status classified_stage current_run_id completed_at
  if "$BACKUP_TOOL" --go >"$RUN_LOG" 2>&1; then
    backup_exit=0
  else
    backup_exit=$?
  fi
  stage=$(awk -F= '/^portal_pvc_backup_stage=/{value=$2} END {print value}' "$RUN_LOG")
  upload=$(awk -F= '/^backup_upload=/{value=$2} END {print value}' "$RUN_LOG")
  read -r status classified_stage < <(classify_backup "$backup_exit" "$stage" "$upload")
  current_run_id="$(run_id)"
  completed_at=$(utc_now)
  report_status "$current_run_id" "$status" "$completed_at" "$classified_stage" || return 1
  printf '%s\n' "portal_pvc_backup_automation_status=$status"
  return "$backup_exit"
}

status() {
  systemctl --user status "$TIMER_NAME" --no-pager
  sudo -n k3s kubectl -n "$CONFIGMAP_NAMESPACE" get configmap "$CONFIGMAP_NAME"
}

stop_service_for_uninstall() {
  local load_state
  if systemctl --user stop "$SERVICE_NAME"; then
    return 0
  fi
  load_state=$(systemctl --user show "$SERVICE_NAME" --property=LoadState --value 2>/dev/null) || return 1
  [ "$load_state" = not-found ]
}

disable_timer_for_uninstall() {
  local load_state
  if systemctl --user disable --now "$TIMER_NAME"; then
    return 0
  fi
  load_state=$(systemctl --user show "$TIMER_NAME" --property=LoadState --value 2>/dev/null) || return 1
  [ "$load_state" = not-found ]
}

uninstall() {
  disable_timer_for_uninstall || return 1
  stop_service_for_uninstall || return 1
  rm -f -- "$UNIT_DIR/$SERVICE_NAME" "$UNIT_DIR/$TIMER_NAME"
  rm -f -- "$(credential_path rclone-config)" "$(credential_path rclone-config-passphrase)"
  systemctl --user daemon-reload
  sudo -n k3s kubectl -n "$CONFIGMAP_NAMESPACE" delete configmap "$CONFIGMAP_NAME" --ignore-not-found
  printf '%s\n' 'portal_pvc_backup_automation_uninstall=PASS'
}

case "${1:-}" in
  --preflight) preflight ;;
  --enroll) enroll ;;
  --install) install_units ;;
  --run) run_once ;;
  --status) status ;;
  --uninstall) uninstall ;;
  *) usage; exit 2 ;;
esac
