#!/usr/bin/env bash
set -Eeuo pipefail

# Host-side controller for the suspended Portal PVC backup CronJob.  It never
# creates or reads Secret values: preflight inspects only the Secret metadata
# rendered by `kubectl describe` and fixed key names.
umask 077
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../../.." && pwd)
MANIFEST=${PORTAL_BACKUP_CRONJOB_MANIFEST:-$REPO_ROOT/infra/k8s/backup-automation/portal-pvc-backup-cronjob.yaml}
NAMESPACE=personal-server
SECRET_NAME=portal-pvc-backup-runtime
CRONJOB_NAME=portal-pvc-backup
LEGACY_TIMER=${PORTAL_BACKUP_LEGACY_TIMER:-personal-server-portal-pvc-backup.timer}
REQUIRED_SECRET_KEYS=(rclone-config rclone-config-passphrase age-recipient age-identity)
REQUIRED_PVCS=(portal-web-files-dynamic portal-web-state-dynamic)

usage() {
  printf '%s\n' "usage: $0 --preflight|--render|--apply|--activate|--status" >&2
}

kctl() {
  sudo -n k3s kubectl "$@"
}

require_commands() {
  command -v sudo >/dev/null
  command -v k3s >/dev/null
  command -v systemctl >/dev/null
}

verify_manifest() {
  [ -r "$MANIFEST" ] || {
    printf '%s\n' 'CronJob manifest is unavailable.' >&2
    return 1
  }
}

verify_secret_key_names() {
  local description key
  description=$(kctl -n "$NAMESPACE" describe secret "$SECRET_NAME") || {
    printf '%s\n' 'Backup runtime Secret metadata is unavailable.' >&2
    return 1
  }
  for key in "${REQUIRED_SECRET_KEYS[@]}"; do
    grep -Eq "^[[:space:]]*${key}:" <<<"$description" || {
      printf '%s\n' "Backup runtime Secret is missing required key: $key" >&2
      return 1
    }
  done
}

verify_readonly_pvc_mount_contract() {
  local pvc contract
  for pvc in "${REQUIRED_PVCS[@]}"; do
    contract=$(kctl -n "$NAMESPACE" get "pvc/$pvc" -o 'jsonpath={.status.phase} {.spec.accessModes[*]}') || return 1
    [[ "$contract" == Bound* ]] && [[ "$contract" == *ReadWriteOnce* ]] || {
      printf '%s\n' "Portal PVC does not meet the Bound ReadWriteOnce mount contract: $pvc" >&2
      return 1
    }
  done
}

preflight() {
  require_commands || return 1
  verify_manifest || return 1
  kctl -n "$NAMESPACE" get serviceaccount portal-pvc-backup >/dev/null 2>&1 || true
  verify_secret_key_names
  verify_readonly_pvc_mount_contract
  printf '%s\n' 'portal_pvc_backup_cronjob_preflight=PASS'
}

render() {
  verify_manifest
  kctl apply --dry-run=client -f "$MANIFEST"
}

apply_suspended() {
  preflight
  kctl apply -f "$MANIFEST"
  ensure_configmap_if_absent "$NAMESPACE" portal-pvc-backup-evidence --from-literal=evidence=
  ensure_configmap_if_absent monitoring sre-telegram-backup-status \
    --from-literal=run_id= \
    --from-literal=status= \
    --from-literal=completed_at= \
    --from-literal=stage=
  printf '%s\n' 'portal_pvc_backup_cronjob_apply=PASS'
}

ensure_configmap_if_absent() {
  local namespace=$1 name=$2 existing
  shift 2
  existing=$(kctl -n "$namespace" get configmap "$name" --ignore-not-found -o name) || {
    printf '%s\n' "ConfigMap state could not be read: $namespace/$name" >&2
    return 1
  }
  [ -n "$existing" ] && return 0
  if kctl -n "$namespace" create configmap "$name" "$@"; then
    return 0
  fi
  existing=$(kctl -n "$namespace" get configmap "$name" --ignore-not-found -o name) || {
    printf '%s\n' "ConfigMap state could not be re-read after create: $namespace/$name" >&2
    return 1
  }
  [ -n "$existing" ] || {
    printf '%s\n' "ConfigMap was not created: $namespace/$name" >&2
    return 1
  }
}

legacy_timer_is_inactive() {
  local load_state active_state
  load_state=$(systemctl --user show "$LEGACY_TIMER" --property=LoadState --value) || {
    printf '%s\n' 'Legacy Portal PVC backup timer state could not be read; CronJob activation is blocked.' >&2
    return 1
  }
  active_state=$(systemctl --user show "$LEGACY_TIMER" --property=ActiveState --value) || {
    printf '%s\n' 'Legacy Portal PVC backup timer state could not be read; CronJob activation is blocked.' >&2
    return 1
  }
  case "$load_state:$active_state" in
    loaded:inactive|loaded:failed|not-found:inactive) return 0 ;;
    *)
      printf '%s\n' 'Legacy Portal PVC backup timer is not confirmed inactive; CronJob activation is blocked.' >&2
      return 1
      ;;
  esac
}

activate() {
  legacy_timer_is_inactive
  preflight
  kctl -n "$NAMESPACE" patch cronjob "$CRONJOB_NAME" --type merge -p '{"spec":{"suspend":false}}'
  printf '%s\n' 'portal_pvc_backup_cronjob_activate=PASS'
}

status() {
  kctl -n "$NAMESPACE" get cronjob "$CRONJOB_NAME"
  systemctl --user is-active "$LEGACY_TIMER" || true
}

case "${1:-}" in
  --preflight) preflight ;;
  --render) render ;;
  --apply) apply_suspended ;;
  --activate) activate ;;
  --status) status ;;
  *) usage; exit 2 ;;
esac
