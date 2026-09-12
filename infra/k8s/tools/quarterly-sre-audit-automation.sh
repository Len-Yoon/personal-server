#!/usr/bin/env bash
set -Eeuo pipefail

usage() { printf '%s\n' "usage: $0 --preflight|--install|--run|--status" >&2; }

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../../.." && pwd)
STATE_DIR=${QUARTERLY_SRE_AUDIT_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/personal-server/quarterly-sre-audit}
UNIT_DIR=${QUARTERLY_SRE_AUDIT_UNIT_DIR:-$HOME/.config/systemd/user}
BACKUP_CREDENTIAL_DIR=${QUARTERLY_SRE_AUDIT_BACKUP_CREDENTIAL_DIR:-$HOME/.config/systemd/user/credentials/personal-server-portal-pvc-backup}
SERVICE_NAME=personal-server-quarterly-sre-audit.service
TIMER_NAME=personal-server-quarterly-sre-audit.timer
SERVICE_TEMPLATE=$REPO_ROOT/infra/k8s/sre-audit-automation/$SERVICE_NAME.tmpl
TIMER_TEMPLATE=$REPO_ROOT/infra/k8s/sre-audit-automation/$TIMER_NAME.tmpl

if [[ $# -ne 1 ]]; then
  usage
  exit 2
fi

safe_dir() {
  local directory=$1
  case "$directory" in /mnt/c|/mnt/c/*|/tmp|/tmp/*) return 1 ;; esac
  [[ ! -L "$directory" ]] || return 1
  mkdir -p -- "$directory" || return 1
  chmod 700 "$directory" || return 1
  local owner
  owner=$(stat -c %u "$directory" 2>/dev/null || stat -f %u "$directory")
  [[ "$owner" == "$(id -u)" ]] || return 1
  [[ "$(findmnt -n -o FSTYPE -T "$directory" 2>/dev/null)" == ext4 ]]
}

require_commands() {
  local command_name
  for command_name in systemctl systemd-analyze sudo k3s findmnt sed install stat id; do
    command -v "$command_name" >/dev/null || return 1
  done
}

require_backup_credentials() {
  local credential_name
  for credential_name in rclone-config rclone-config-passphrase; do
    [[ -f "$BACKUP_CREDENTIAL_DIR/$credential_name.cred" && -r "$BACKUP_CREDENTIAL_DIR/$credential_name.cred" ]] || return 1
  done
}

preflight() {
  require_commands || return 1
  safe_dir "$STATE_DIR" || return 1
  [[ -r "$SERVICE_TEMPLATE" && -r "$TIMER_TEMPLATE" ]] || return 1
  require_backup_credentials || return 1
  systemctl --user show-environment >/dev/null || return 1
  sudo -n k3s kubectl get nodes --no-headers >/dev/null || return 1
  printf '%s\n' 'quarterly_sre_audit_preflight=PASS'
}

render_template() {
  local template=$1 output=$2
  sed -e "s|@REPO_ROOT@|$REPO_ROOT|g" -e "s|@STATE_DIR@|$STATE_DIR|g" \
    -e "s|@BACKUP_CREDENTIAL_DIR@|$BACKUP_CREDENTIAL_DIR|g" \
    "$template" >"$output"
  chmod 600 "$output"
}

install_units() {
  preflight || return 1
  safe_dir "$UNIT_DIR" || return 1
  render_template "$SERVICE_TEMPLATE" "$UNIT_DIR/$SERVICE_NAME"
  render_template "$TIMER_TEMPLATE" "$UNIT_DIR/$TIMER_NAME"
  systemd-analyze --user verify "$UNIT_DIR/$SERVICE_NAME" "$UNIT_DIR/$TIMER_NAME" || return 1
  systemctl --user daemon-reload || return 1
  systemctl --user enable --now "$TIMER_NAME" || return 1
  printf '%s\n' 'quarterly_sre_audit_install=PASS'
}

status() {
  systemctl --user status "$TIMER_NAME" --no-pager
  sudo -n k3s kubectl -n monitoring get configmap sre-telegram-quarterly-audit-status
}

run_once() {
safe_dir "$STATE_DIR" || return 1
command -v flock >/dev/null || return 1
exec 9>"$STATE_DIR/quarterly-sre-audit.lock"
if ! flock --exclusive --nonblock 9; then
  printf '%s\n' 'quarterly_sre_audit=locked' >&2
  return 1
fi

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
COMPLETED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
health_audit=failed
backup_check=failed
recovery_lab=failed

run_check() {
  local name="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    printf -v "$name" '%s' passed
  else
    printf -v "$name" '%s' failed
  fi
}

run_check health_audit "$REPO_ROOT/infra/k8s/tools/sre-health-audit.sh"
run_check backup_check "$REPO_ROOT/infra/k8s/tools/portal-pvc-backup-verify.sh" --check
run_check recovery_lab "$REPO_ROOT/infra/k8s/tools/sre-pod-recovery-lab.sh" --run

status=passed
[[ "$health_audit" == passed && "$backup_check" == passed && "$recovery_lab" == passed ]] || status=failed

if ! sudo -n k3s kubectl -n monitoring apply -f - >/dev/null 2>&1 <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: sre-telegram-quarterly-audit-status
  namespace: monitoring
data:
  run_id: "$RUN_ID"
  status: "$status"
  completed_at: "$COMPLETED_AT"
  health_audit: "$health_audit"
  backup_check: "$backup_check"
  recovery_lab: "$recovery_lab"
EOF
then
  printf '%s\n' 'quarterly_sre_audit=failed'
  exit 1
fi

printf 'quarterly_sre_audit=%s\n' "$status"
[[ "$status" == passed ]]
}

case "$1" in
  --preflight) preflight ;;
  --install) install_units ;;
  --run) run_once ;;
  --status) status ;;
  *) usage; exit 2 ;;
esac
