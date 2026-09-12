#!/usr/bin/env bash
set -Eeuo pipefail

# Operator-only encrypted backup verifier for the K3s PVC-backed Portal runtime.
# This tool never uses Docker or local bind-mounted source directories.
umask 077
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../../.." && pwd)
NAMESPACE=${PORTAL_NAMESPACE:-personal-server}
EXECUTION_MODE=${PORTAL_BACKUP_EXECUTION_MODE:-host}
case "$EXECUTION_MODE" in
  host|in-cluster) ;;
  *) printf '%s\n' 'portal_pvc_backup=FAIL' >&2; exit 2 ;;
esac
if [ "$EXECUTION_MODE" = in-cluster ]; then
  RUNTIME_MARKER=${PORTAL_RUNTIME_MARKER:-/work/data/portal-runtime.mode}
  EVIDENCE=${PORTAL_BACKUP_EVIDENCE:-/work/.portal-backup-verified}
  RECIPIENT=${PORTAL_AGE_RECIPIENT:-/run/secrets/portal-backup/age-recipient}
  IDENTITY=${PORTAL_AGE_IDENTITY:-/run/secrets/portal-backup/age-identity}
else
  RUNTIME_MARKER=${PORTAL_RUNTIME_MARKER:-$REPO_ROOT/data/portal-runtime.mode}
  EVIDENCE=${PORTAL_BACKUP_EVIDENCE:-$REPO_ROOT/.portal-backup-verified}
  RECIPIENT=${PORTAL_AGE_RECIPIENT:-$HOME/.local/share/personal-server/age/recipient.txt}
  IDENTITY=${PORTAL_AGE_IDENTITY:-$HOME/.local/share/personal-server/age/identity.txt}
fi
REMOTE=${PORTAL_BACKUP_REMOTE:-gdrive:PersonalServer-encrypted-backups}
MAX_AGE=${PORTAL_BACKUP_MAX_AGE_SECONDS:-86400}
READINESS_TIMEOUT_SECONDS=${PORTAL_READINESS_TIMEOUT_SECONDS:-300}
RCLONE_PREFLIGHT_TIMEOUT_SECONDS=${PORTAL_RCLONE_TIMEOUT_SECONDS:-30}
RCLONE_PREFLIGHT_RETRY_COUNT=${PORTAL_RCLONE_PREFLIGHT_RETRY_COUNT:-1}
RCLONE_PREFLIGHT_RETRY_BACKOFF_SECONDS=${PORTAL_RCLONE_PREFLIGHT_RETRY_BACKOFF_SECONDS:-5}
FILES_PVC='portal-web-files-dynamic'
STATE_PVC='portal-web-state-dynamic'
DEPLOYMENT='portal-web'
EVIDENCE_CONFIGMAP='portal-pvc-backup-evidence'
STATUS_CONFIGMAP='sre-telegram-backup-status'
STATUS_NAMESPACE='monitoring'
if [ "$EXECUTION_MODE" = in-cluster ]; then
  STATE_DIR=${PORTAL_BACKUP_STATE_DIR:-/work/portal-pvc-backup}
else
  STATE_DIR=${PORTAL_BACKUP_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/personal-server/portal-pvc-backup}
fi
LOCK_FILE=${PORTAL_BACKUP_LOCK_FILE:-$STATE_DIR/portal-pvc-backup.lock}
FILES_MOUNT=${PORTAL_BACKUP_FILES_MOUNT:-/data/files}
STATE_MOUNT=${PORTAL_BACKUP_STATE_MOUNT:-/data/portal-web-state}
RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)-$$
WORKDIR=$(mktemp -d "${TMPDIR:-/tmp}/portal-pvc-backup-${RUN_ID}.XXXXXX")
DIAGNOSTIC_FILE="$WORKDIR/diagnostics.log"
: > "$DIAGNOSTIC_FILE"
chmod 600 "$DIAGNOSTIC_FILE"
# Keep command diagnostics private; operator-facing output uses fixed labels.
exec 3>&2
exec 2>>"$DIAGNOSTIC_FILE"
MODE=''
ORIGINAL_REPLICAS=''
WRITERS_SCALED=0
READER_CREATED=0
READER_POD=''
LOCK_HELD=0
LOCK_FD=9
EVIDENCE_PENDING=0
BACKUP_UPLOAD_STATUS=''
FAILURE_STAGE=''
ACTIVE_TIMEOUT_PID=''
RCLONE_CREDENTIAL_ARGS=()

usage() { printf '%s\n' "usage: $0 --check|--go" >&2; }

ensure_sudo_access() {
  sudo -n k3s kubectl version --client >/dev/null 2>&1
}

# The advisory lock belongs only to this controller process.  Long-running
# children (kubectl exec/rclone) must not inherit it, otherwise an interrupted
# backup can leave the lock held after this process exits.
# Replace the controller's lock descriptor for child commands instead of simply
# closing it.  rclone can stall when it inherits a closed descriptor here; a
# /dev/null descriptor keeps the lock out of the child without that failure.
run_unlocked() { "$@" 9</dev/null; }
TIMEOUT_SUPERVISOR=$'import ctypes\nimport os\nimport signal\nimport subprocess\nimport sys\n\nPR_SET_PDEATHSIG = 1\nif sys.platform.startswith("linux"):\n    libc = ctypes.CDLL(None, use_errno=True)\n    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM) != 0:\n        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG)")\n\nseconds = int(sys.argv[1])\ntermination_grace_seconds = int(sys.argv[2])\ncommand = sys.argv[3:]\nprocess = None\n\ndef terminate_group(signal_to_send):\n    if process is None:\n        return\n    try:\n        os.killpg(process.pid, signal_to_send)\n    except ProcessLookupError:\n        pass\n\ndef wait_after_termination():\n    if process is None:\n        return\n    try:\n        process.wait(timeout=termination_grace_seconds)\n    except subprocess.TimeoutExpired:\n        terminate_group(signal.SIGKILL)\n        process.wait()\n\ndef on_signal(signum, _frame):\n    terminate_group(signal.SIGTERM)\n    wait_after_termination()\n    raise SystemExit(128 + signum)\n\nsignal.signal(signal.SIGINT, on_signal)\nsignal.signal(signal.SIGTERM, on_signal)\nsignal.signal(signal.SIGHUP, on_signal)\n# A separate process group keeps cancellation scoped without detaching the TTY.\nprocess = subprocess.Popen(command, process_group=0)\ntry:\n    raise SystemExit(process.wait(timeout=seconds))\nexcept subprocess.TimeoutExpired:\n    terminate_group(signal.SIGTERM)\n    wait_after_termination()\n    raise SystemExit(124)\n'
run_timeout() {
  local seconds=$1
  shift
  # Python owns the child session and kills the whole process group on timeout.
  # Unlike `timeout setsid`, it also preserves command output and stdin.
  python3 -c "$TIMEOUT_SUPERVISOR" "$seconds" 10 "$@" 9>&-
}
run_timeout_at_deadline() {
  local seconds=$1
  shift
  # Readiness polling must use the shared deadline without extra grace time.
  python3 -c "$TIMEOUT_SUPERVISOR" "$seconds" 0 "$@" 9>&-
}
run_timeout_tracked() {
  local seconds=$1 status
  shift
  # Streaming does not consume stdin, so run it in the background only to let
  # the controller's signal trap terminate the supervisor immediately.
  python3 -c "$TIMEOUT_SUPERVISOR" "$seconds" 10 "$@" 9>&- &
  ACTIVE_TIMEOUT_PID=$!
  if wait "$ACTIVE_TIMEOUT_PID"; then
    status=0
  else
    status=$?
  fi
  ACTIVE_TIMEOUT_PID=''
  return "$status"
}
if [ "$EXECUTION_MODE" = host ]; then
  KCTL=(sudo -n k3s kubectl)
else
  KCTL=(kubectl)
fi
kctl_with_timeout() {
  local seconds=$1
  shift
  run_timeout "$seconds" "${KCTL[@]}" "$@"
}
kctl_with_deadline_timeout() {
  local seconds=$1
  shift
  run_timeout_at_deadline "$seconds" "${KCTL[@]}" "$@"
}
kctl() { kctl_with_timeout "${PORTAL_KUBECTL_TIMEOUT_SECONDS:-120}" "$@"; }
fail() { return 1; }
progress() { printf '%s\n' "portal_pvc_backup_stage=$1"; }

run_private() {
  run_unlocked "$@" >>"$DIAGNOSTIC_FILE" 2>&1
}

tree_digest() {
  (cd -- "$1" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum) |
    sha256sum | awk '{print $1}'
}

utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
expiry_now() {
  if date -u -v+1S +%Y-%m-%dT%H:%M:%SZ >/dev/null 2>&1; then
    date -u -v+"${MAX_AGE}"S +%Y-%m-%dT%H:%M:%SZ
  else
    date -u -d "+${MAX_AGE} seconds" +%Y-%m-%dT%H:%M:%SZ
  fi
}

assert_regular_tree() {
  local entry
  entry=$(find "$1" \( -type l -o -type b -o -type c -o -type p -o -type s \) -print -quit) || return 1
  [ -z "$entry" ]
}

assert_preflight() {
  [ "$NAMESPACE" = personal-server ] || return 1
  [ "$MAX_AGE" -ge 1 ] 2>/dev/null || return 1
  case "$READINESS_TIMEOUT_SECONDS" in ''|*[!0-9]*|0[0-9]*) return 1 ;; esac
  [ "$READINESS_TIMEOUT_SECONDS" -ge 120 ] && [ "$READINESS_TIMEOUT_SECONDS" -le 600 ] || return 1
  case "$RCLONE_PREFLIGHT_TIMEOUT_SECONDS" in ''|*[!0-9]*|0[0-9]*) return 1 ;; esac
  [ "$RCLONE_PREFLIGHT_TIMEOUT_SECONDS" -ge 1 ] && [ "$RCLONE_PREFLIGHT_TIMEOUT_SECONDS" -le 30 ] || return 1
  case "$RCLONE_PREFLIGHT_RETRY_COUNT" in ''|*[!0-9]*|0[0-9]*) return 1 ;; esac
  [ "$RCLONE_PREFLIGHT_RETRY_COUNT" -ge 0 ] && [ "$RCLONE_PREFLIGHT_RETRY_COUNT" -le 1 ] || return 1
  case "$RCLONE_PREFLIGHT_RETRY_BACKOFF_SECONDS" in ''|*[!0-9]*|0[0-9]*) return 1 ;; esac
  [ "$RCLONE_PREFLIGHT_RETRY_BACKOFF_SECONDS" -ge 1 ] && [ "$RCLONE_PREFLIGHT_RETRY_BACKOFF_SECONDS" -le 30 ] || return 1
  if [ "$EXECUTION_MODE" = host ]; then
    [ -f "$RUNTIME_MARKER" ] && [ -r "$RUNTIME_MARKER" ] || return 1
    [ "$(tr -d '\r\n' < "$RUNTIME_MARKER")" = k3s ] || return 1
  fi
  [ -r "$RECIPIENT" ] && [ -r "$IDENTITY" ] || return 1
  [ -d "$(dirname -- "$EVIDENCE")" ] && [ -w "$(dirname -- "$EVIDENCE")" ] || return 1
  mkdir -p -- "$STATE_DIR" || return 1
  chmod 700 "$STATE_DIR" || return 1
  required_commands=(age rclone sqlite3 python3 flock tar find sha256sum awk grep xargs mktemp)
  if [ "$EXECUTION_MODE" = host ]; then
    required_commands+=(sudo k3s)
  else
    required_commands+=(kubectl)
  fi
  for command_name in "${required_commands[@]}"; do
    command -v "$command_name" >/dev/null || return 1
  done
  if [ "$EXECUTION_MODE" = in-cluster ]; then
    load_in_cluster_evidence || return 1
  fi

  local replicas files_phase state_phase
  if [ "$EXECUTION_MODE" = host ]; then
    local nodes ready_count
    nodes=$(kctl get nodes --no-headers) || return 1
    ready_count=$(printf '%s\n' "$nodes" | awk '$2 == "Ready" { count++ } END { print count + 0 }')
    [ "$ready_count" -eq 1 ] || return 1
  fi
  replicas=$(kctl -n "$NAMESPACE" get deployment "$DEPLOYMENT" -o jsonpath='{.spec.replicas}') || return 1
  [ "$replicas" = 1 ] || return 1
  files_phase=$(kctl -n "$NAMESPACE" get "pvc/$FILES_PVC" -o jsonpath='{.status.phase}') || return 1
  state_phase=$(kctl -n "$NAMESPACE" get "pvc/$STATE_PVC" -o jsonpath='{.status.phase}') || return 1
  [ "$files_phase" = Bound ] && [ "$state_phase" = Bound ]
}

wait_for_writer_termination() {
  local deadline=$((SECONDS + ${PORTAL_WRITER_TERMINATION_TIMEOUT_SECONDS:-120})) available
  while [ "$SECONDS" -lt "$deadline" ]; do
    available=$(kctl -n "$NAMESPACE" get deployment "$DEPLOYMENT" -o jsonpath='{.status.availableReplicas}') || return 1
    available=${available:-0}
    [ "$available" = 0 ] && return 0
    sleep 1
  done
  return 1
}

wait_for_portal_availability() {
  local deadline=$((SECONDS + READINESS_TIMEOUT_SECONDS)) available remaining query_status sleep_seconds
  while [ "$SECONDS" -lt "$deadline" ]; do
    remaining=$((deadline - SECONDS))
    [ "$remaining" -gt 0 ] || break
    if available=$(kctl_with_deadline_timeout "$remaining" -n "$NAMESPACE" get deployment "$DEPLOYMENT" -o jsonpath='{.status.availableReplicas}'); then
      :
    else
      query_status=$?
      [ "$query_status" -eq 124 ] && return 1
      return 2
    fi
    available=${available:-0}
    [ "$available" = "$ORIGINAL_REPLICAS" ] && return 0
    remaining=$((deadline - SECONDS))
    [ "$remaining" -gt 0 ] || break
    sleep_seconds=2
    [ "$remaining" -lt "$sleep_seconds" ] && sleep_seconds=$remaining
    sleep "$sleep_seconds"
  done
  return 1
}

rclone_with_credentials() {
  if [ "${#RCLONE_CREDENTIAL_ARGS[@]}" -gt 0 ]; then
    run_unlocked rclone "${RCLONE_CREDENTIAL_ARGS[@]}" "$@"
  else
    run_unlocked rclone "$@"
  fi
}

rclone_with_credentials_timeout() {
  local seconds=$1
  shift
  if [ "${#RCLONE_CREDENTIAL_ARGS[@]}" -gt 0 ]; then
    run_timeout "$seconds" rclone "${RCLONE_CREDENTIAL_ARGS[@]}" "$@"
  else
    run_timeout "$seconds" rclone "$@"
  fi
}

prepare_rclone_credentials() {
  if [ -n "${PORTAL_RCLONE_CONFIG_FILE:-}" ] || [ -n "${PORTAL_RCLONE_PASSWORD_COMMAND:-}" ]; then
    [ -n "${PORTAL_RCLONE_CONFIG_FILE:-}" ] && [ -r "$PORTAL_RCLONE_CONFIG_FILE" ] || return 1
    [ -n "${PORTAL_RCLONE_PASSWORD_COMMAND:-}" ] || return 1
    RCLONE_CREDENTIAL_ARGS=(--config "$PORTAL_RCLONE_CONFIG_FILE" --password-command "$PORTAL_RCLONE_PASSWORD_COMMAND")
    return
  fi
  # Non-automated terminal use keeps rclone's own masked prompt as a fallback.
  # A caller without systemd credentials intentionally delegates prompting to rclone.
  RCLONE_CREDENTIAL_ARGS=()
}

classify_remote_access_failure() {
  if grep -Eiq 'decrypt.*config|config.*decrypt|bad password|password.*incorrect' "$DIAGNOSTIC_FILE"; then
    printf '%s\n' remote-config-password
  elif grep -Eiq 'directory not found|path not found|object not found|does not exist' "$DIAGNOSTIC_FILE"; then
    printf '%s\n' remote-path
  elif grep -Eiq 'invalid_grant|unauthorized|permission denied|http.*(401|403)' "$DIAGNOSTIC_FILE"; then
    printf '%s\n' remote-auth
  elif grep -Eiq 'no such host|network is unreachable|connection refused|i/o timeout' "$DIAGNOSTIC_FILE"; then
    printf '%s\n' remote-network
  else
    printf '%s\n' remote-access
  fi
}

assert_remote_access() {
  local status retry_attempt=0
  FAILURE_STAGE='remote_preflight'
  prepare_rclone_credentials || {
    FAILURE_STAGE='remote-credentials'
    return 1
  }
  while :; do
    if rclone_with_credentials_timeout "$RCLONE_PREFLIGHT_TIMEOUT_SECONDS" lsd --max-depth 1 --log-level ERROR "$REMOTE" >>"$DIAGNOSTIC_FILE" 2>&1; then
      return 0
    else
      status=$?
    fi
    if [ "$status" -ne 124 ]; then
      FAILURE_STAGE=$(classify_remote_access_failure)
      return 1
    fi
    if [ "$retry_attempt" -ge "$RCLONE_PREFLIGHT_RETRY_COUNT" ]; then
      FAILURE_STAGE='remote-timeout'
      return 1
    fi
    retry_attempt=$((retry_attempt + 1))
    sleep "$RCLONE_PREFLIGHT_RETRY_BACKOFF_SECONDS"
  done
}

acquire_lock() {
  FAILURE_STAGE='lock'
  exec 9>"$LOCK_FILE" || return 1
  if ! flock -n "$LOCK_FD" >>"$DIAGNOSTIC_FILE" 2>&1; then
    exec 9>&-
    return 1
  fi
  LOCK_HELD=1
}

create_reader_pod() {
  READER_POD="portal-pvc-backup-reader-$(date -u +%Y%m%d%H%M%S)-$$"
  # Reserve cleanup before create: an API timeout may leave the Pod created.
  READER_CREATED=1
  kctl -n personal-server create -f - <<YAML >>"$DIAGNOSTIC_FILE" 2>&1
apiVersion: v1
kind: Pod
metadata:
  name: $READER_POD
  labels:
    app.kubernetes.io/name: portal-pvc-backup-reader
spec:
  restartPolicy: Never
  containers:
    - name: reader
      image: busybox:1.36
      command: ["sh", "-c", "sleep 3600"]
      volumeMounts:
        - name: portal-files
          mountPath: /data/files
          readOnly: true
        - name: portal-state
          mountPath: /data/portal-web-state
          readOnly: true
  volumes:
    - name: portal-files
      persistentVolumeClaim:
        claimName: $FILES_PVC
    - name: portal-state
      persistentVolumeClaim:
        claimName: $STATE_PVC
YAML
  kctl -n personal-server wait --for=condition=Ready "pod/$READER_POD" --timeout=120s >>"$DIAGNOSTIC_FILE" 2>&1
}

stream_pvc_tree() {
  local mount_path=$1 destination=$2 stream_archive
  mkdir -p -- "$destination"
  stream_archive="$destination/.pvc-stream.tar"
  if [ "$EXECUTION_MODE" = in-cluster ]; then
    tar -C "$mount_path" -cf "$stream_archive" . >>"$DIAGNOSTIC_FILE" 2>&1 || {
      rm -f -- "$stream_archive"
      return 1
    }
    tar -C "$destination" -xf "$stream_archive" >>"$DIAGNOSTIC_FILE" 2>&1
    rm -f -- "$stream_archive"
    return
  fi
  if ! run_timeout_tracked "${PORTAL_STREAM_TIMEOUT_SECONDS:-120}" "${KCTL[@]}" -n "$NAMESPACE" exec -i "$READER_POD" -- tar -C "$mount_path" -cf - . >"$stream_archive" 2>>"$DIAGNOSTIC_FILE"; then
    rm -f -- "$stream_archive"
    return 1
  fi
  tar -C "$destination" -xf "$stream_archive" >>"$DIAGNOSTIC_FILE" 2>&1
  rm -f -- "$stream_archive"
}

evidence_is_current_k3s_pvc() {
  [ -f "$EVIDENCE" ] || return 1
  python3 "$SCRIPT_DIR/validate-backup-evidence.py" --evidence "$EVIDENCE" --max-age-seconds "$MAX_AGE" >/dev/null 2>&1 || return 1
  local evidence_digest evidence_runtime
  evidence_digest=$(awk -F= '$1 == "source_digest" { print $2 }' "$EVIDENCE")
  evidence_runtime=$(awk -F= '$1 == "source_runtime" { print $2 }' "$EVIDENCE")
  [ "$evidence_digest" = "$SOURCE_DIGEST" ] && [ "$evidence_runtime" = k3s-pvc ]
}

load_in_cluster_evidence() {
  [ "$EXECUTION_MODE" = in-cluster ] || return 0
  kctl -n "$NAMESPACE" get configmap "$EVIDENCE_CONFIGMAP" -o jsonpath='{.data.evidence}' >"$EVIDENCE" 2>>"$DIAGNOSTIC_FILE"
}

patch_in_cluster_evidence() {
  local patch
  [ "$EXECUTION_MODE" = in-cluster ] || return 0
  patch=$(python3 - "$EVIDENCE" <<'PY'
import json
import sys
from pathlib import Path

print(json.dumps([{
    "op": "add",
    "path": "/data/evidence",
    "value": Path(sys.argv[1]).read_text(encoding="utf-8"),
}], separators=(",", ":")))
PY
)
  kctl -n "$NAMESPACE" patch configmap "$EVIDENCE_CONFIGMAP" --type=json --patch "$patch" >>"$DIAGNOSTIC_FILE" 2>&1
}

safe_status_stage() {
  local stage=${1//_/-}
  [[ "$stage" =~ ^[a-z][a-z0-9-]{0,63}$ ]] || return 1
  printf '%s' "$stage"
}

patch_in_cluster_status() {
  local report_status=$1 report_stage=$2 completed_at patch
  case "$report_status" in completed|unchanged|failed|restore_failed) ;; *) return 1 ;; esac
  completed_at=$(utc_now)
  report_stage=$(safe_status_stage "$report_stage") || return 1
  patch=$(python3 - "$RUN_ID" "$report_status" "$completed_at" "$report_stage" <<'PY'
import json
import sys

keys = ("run_id", "status", "completed_at", "stage")
print(json.dumps([
    {"op": "add", "path": f"/data/{key}", "value": value}
    for key, value in zip(keys, sys.argv[1:])
], separators=(",", ":")))
PY
)
  kctl -n "$STATUS_NAMESPACE" get configmap "$STATUS_CONFIGMAP" -o json >/dev/null 2>>"$DIAGNOSTIC_FILE" || return 1
  kctl -n "$STATUS_NAMESPACE" patch configmap "$STATUS_CONFIGMAP" --type=json --patch "$patch" >>"$DIAGNOSTIC_FILE" 2>&1
}

report_in_cluster_status() {
  local exit_code=$1 restore_ok=$2 report_status report_stage
  [ "$EXECUTION_MODE" = in-cluster ] && [ "$MODE" = --go ] || return 0
  if [ "$exit_code" -eq 0 ] && [ "$restore_ok" -eq 1 ]; then
    if [ "$BACKUP_UPLOAD_STATUS" = SKIPPED_UNCHANGED ]; then
      report_status=unchanged
      report_stage=unchanged
    else
      report_status=completed
      report_stage=completed
    fi
  else
    case "$FAILURE_STAGE" in
      remote_restore|restore_validation|portal_readiness|portal-rollout-timeout|portal-rollout-command|portal_health)
        report_status=restore_failed ;;
      *)
        report_status=failed ;;
    esac
    report_stage=${FAILURE_STAGE:-backup_failed}
  fi
  patch_in_cluster_status "$report_status" "$report_stage"
}

cleanup() {
  local status=$? restore_ok=1 availability_status
  trap - EXIT
  # A follow-up Ctrl+C must not interrupt reader deletion or Portal restoration.
  trap '' INT TERM HUP
  if [ "$READER_CREATED" -eq 1 ]; then
    kctl -n personal-server delete pod "$READER_POD" --ignore-not-found --wait=true >>"$DIAGNOSTIC_FILE" 2>&1 || restore_ok=0
  fi
  if [ "$WRITERS_SCALED" -eq 1 ] && [ "${ORIGINAL_REPLICAS:-0}" -gt 0 ] 2>/dev/null; then
    if ! kctl -n personal-server scale "deployment/$DEPLOYMENT" --replicas="$ORIGINAL_REPLICAS" >>"$DIAGNOSTIC_FILE" 2>&1; then
      FAILURE_STAGE='portal_readiness'
      restore_ok=0
    elif wait_for_portal_availability; then
      :
    else
      availability_status=$?
      case "$availability_status" in
        1) FAILURE_STAGE='portal-rollout-timeout' ;;
        *) FAILURE_STAGE='portal-rollout-command' ;;
      esac
      restore_ok=0
    fi
    if [ "$restore_ok" -eq 1 ] && [ "$EXECUTION_MODE" = host ]; then
      PORTAL_POD=$(kctl -n personal-server get pod -l app.kubernetes.io/name=portal-web -o jsonpath='{.items[0].metadata.name}') || PORTAL_POD=''
      if [ -z "$PORTAL_POD" ] || ! kctl -n personal-server exec "$PORTAL_POD" -- python3 -c 'import urllib.request; response = urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=10); raise SystemExit(0 if response.status == 200 else 1)' >>"$DIAGNOSTIC_FILE" 2>&1; then
        FAILURE_STAGE='portal_health'
        restore_ok=0
      fi
    fi
    WRITERS_SCALED=0
  fi
  if [ "$LOCK_HELD" -eq 1 ]; then
    exec 9>&-
    LOCK_HELD=0
  fi
  if [ "$status" -eq 0 ] && [ "$restore_ok" -eq 1 ] && [ "$EVIDENCE_PENDING" -eq 1 ]; then
    FAILURE_STAGE='evidence'
    evidence_dir=$(dirname -- "$EVIDENCE")
    mkdir -p -- "$evidence_dir"
    tmp_evidence=$(mktemp "$evidence_dir/.portal-pvc-backup-verified.XXXXXX")
    chmod 600 "$tmp_evidence"
    backup_completed_at=$(utc_now)
    restore_verified_at=$(utc_now)
    evidence_expires_at=$(expiry_now)
    printf '%s\n' \
      'schema_version=1' 'scope=portal' 'backup_status=success' 'encrypted=true' \
      "backup_completed_at=$backup_completed_at" 'restore_status=success' \
      "restore_verified_at=$restore_verified_at" "evidence_expires_at=$evidence_expires_at" \
      "backup_id=portal-$RUN_ID" "artifact_digest=$ARTIFACT_DIGEST" \
      "source_digest=$SOURCE_DIGEST" 'source_runtime=k3s-pvc' \
      'restore_check=sqlite_quick_check' 'restore_path_check=success' > "$tmp_evidence"
    if ! python3 "$SCRIPT_DIR/validate-backup-evidence.py" --evidence "$tmp_evidence" --max-age-seconds "$MAX_AGE" >>"$DIAGNOSTIC_FILE" 2>&1; then
      rm -f -- "$tmp_evidence"
      restore_ok=0
    else
      mv -- "$tmp_evidence" "$EVIDENCE"
      if ! patch_in_cluster_evidence; then
        FAILURE_STAGE='evidence'
        restore_ok=0
      fi
    fi
  fi
  if ! report_in_cluster_status "$status" "$restore_ok"; then
    [ -n "$FAILURE_STAGE" ] || FAILURE_STAGE='reporting'
    restore_ok=0
  fi
  if [ "$status" -ne 0 ] || [ "$restore_ok" -ne 1 ]; then
    [ "$MODE" = --check ] || rm -f -- "$EVIDENCE"
    [ -z "$FAILURE_STAGE" ] || printf '%s\n' "portal_pvc_backup_stage=$FAILURE_STAGE"
    printf '%s\n' 'portal_pvc_backup=FAIL'
    rm -rf -- "$WORKDIR"
    exit 1
  fi
  rm -rf -- "$WORKDIR"
  [ "$MODE" = --check ] || [ -z "$BACKUP_UPLOAD_STATUS" ] || printf '%s\n' "backup_upload=$BACKUP_UPLOAD_STATUS"
  printf '%s\n' 'portal_pvc_backup=PASS'
  exit 0
}

case "${1:-}" in
  --check|--go) MODE=$1 ;;
  *) usage; exit 2 ;;
esac
on_signal() {
  if [ -n "$ACTIVE_TIMEOUT_PID" ]; then
    kill -TERM "$ACTIVE_TIMEOUT_PID" 2>/dev/null || true
  fi
  exit 130
}
trap cleanup EXIT
trap on_signal INT TERM HUP

if [ "$EXECUTION_MODE" = host ]; then
  ensure_sudo_access || exit 1
fi
assert_preflight || exit 1
assert_remote_access || exit 1
FAILURE_STAGE=''
if [ "$MODE" = --check ]; then
  exit 0
fi

acquire_lock || exit 1
FAILURE_STAGE=''

stage="$WORKDIR/stage"
restore="$WORKDIR/restore"
mkdir -p -- "$stage/data" "$restore"
ORIGINAL_REPLICAS=$(kctl -n "$NAMESPACE" get deployment "$DEPLOYMENT" -o jsonpath='{.spec.replicas}')
case "$ORIGINAL_REPLICAS" in ''|*[!0-9]*) exit 1 ;; esac
# Mark restoration as required before the scale request: timeout/failure is ambiguous.
WRITERS_SCALED=1
progress writer_pause
kctl -n personal-server scale "deployment/$DEPLOYMENT" --replicas=0 >>"$DIAGNOSTIC_FILE" 2>&1
  if [ "$EXECUTION_MODE" = host ]; then
    kctl -n personal-server wait --for=delete pod -l app.kubernetes.io/name=portal-web --timeout=120s >>"$DIAGNOSTIC_FILE" 2>&1
  else
    wait_for_writer_termination
  fi
progress pvc_snapshot
if [ "$EXECUTION_MODE" = host ]; then
  create_reader_pod
fi
stream_pvc_tree "$FILES_MOUNT" "$stage/data/files"
stream_pvc_tree "$STATE_MOUNT" "$stage/data/portal-web-state"
sqlite3 "$stage/data/portal-web-state/homeops.sqlite3" 'PRAGMA quick_check;' 2>>"$DIAGNOSTIC_FILE" | grep -Fxq ok
assert_regular_tree "$stage/data/files"
assert_regular_tree "$stage/data/portal-web-state"

files_digest=$(tree_digest "$stage/data/files")
state_digest=$(tree_digest "$stage/data/portal-web-state")
SOURCE_DIGEST="sha256:$(printf '%s\n%s\n' "$files_digest" "$state_digest" | sha256sum | awk '{print $1}')"
if evidence_is_current_k3s_pvc; then
  BACKUP_UPLOAD_STATUS='SKIPPED_UNCHANGED'
  exit 0
fi

printf '%s\n' "source_runtime=k3s-pvc" "source_digest=$SOURCE_DIGEST" > "$stage/manifest.txt"
archive="$WORKDIR/portal-${RUN_ID}.tar"
ciphertext="$archive.age"
tar -C "$stage" -cf "$archive" data manifest.txt >>"$DIAGNOSTIC_FILE" 2>&1
age -R "$RECIPIENT" -o "$ciphertext" "$archive" >>"$DIAGNOSTIC_FILE" 2>&1
artifact_digest="sha256:$(sha256sum "$ciphertext" | awk '{print $1}')"
remote_object="$REMOTE/portal-${RUN_ID}.tar.age"
progress remote_upload
rclone_with_credentials copyto --immutable --log-level ERROR "$ciphertext" "$remote_object" >>"$DIAGNOSTIC_FILE" 2>&1
progress remote_restore
FAILURE_STAGE='remote_restore'
rclone_with_credentials copyto --log-level ERROR "$remote_object" "$WORKDIR/download.age" >>"$DIAGNOSTIC_FILE" 2>&1
[ "$artifact_digest" = "sha256:$(sha256sum "$WORKDIR/download.age" | awk '{print $1}')" ]
age -d -i "$IDENTITY" -o "$WORKDIR/restore.tar" "$WORKDIR/download.age" >>"$DIAGNOSTIC_FILE" 2>&1
tar -C "$restore" -xf "$WORKDIR/restore.tar" >>"$DIAGNOSTIC_FILE" 2>&1
assert_regular_tree "$restore/data/files"
assert_regular_tree "$restore/data/portal-web-state"
[ "$(tree_digest "$restore/data/files")" = "$files_digest" ]
[ "$(tree_digest "$restore/data/portal-web-state")" = "$state_digest" ]
grep -Fxq "source_runtime=k3s-pvc" "$restore/manifest.txt"
grep -Fxq "source_digest=$SOURCE_DIGEST" "$restore/manifest.txt"
progress restore_validation
FAILURE_STAGE='restore_validation'
sqlite3 "$restore/data/portal-web-state/homeops.sqlite3" 'PRAGMA quick_check;' 2>>"$DIAGNOSTIC_FILE" | grep -Fxq ok
ARTIFACT_DIGEST="$artifact_digest"
EVIDENCE_PENDING=1
BACKUP_UPLOAD_STATUS='UPLOADED'
