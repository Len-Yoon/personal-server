#!/usr/bin/env bash
set -Eeuo pipefail

# Operator-only encrypted backup verifier for the K3s PVC-backed Portal runtime.
# This tool never uses Docker or local bind-mounted source directories.
umask 077
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../../.." && pwd)
NAMESPACE=${PORTAL_NAMESPACE:-personal-server}
RUNTIME_MARKER=${PORTAL_RUNTIME_MARKER:-$REPO_ROOT/data/portal-runtime.mode}
EVIDENCE=${PORTAL_BACKUP_EVIDENCE:-$REPO_ROOT/.portal-backup-verified}
RECIPIENT=${PORTAL_AGE_RECIPIENT:-$HOME/.local/share/personal-server/age/recipient.txt}
IDENTITY=${PORTAL_AGE_IDENTITY:-$HOME/.local/share/personal-server/age/identity.txt}
REMOTE=${PORTAL_BACKUP_REMOTE:-gdrive:PersonalServer-encrypted-backups}
MAX_AGE=${PORTAL_BACKUP_MAX_AGE_SECONDS:-86400}
FILES_PVC='portal-web-files-dynamic'
STATE_PVC='portal-web-state-dynamic'
DEPLOYMENT='portal-web'
LEASE_NAME='portal-pvc-backup-lock'
RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)-$$
WORKDIR=$(mktemp -d "${TMPDIR:-/tmp}/portal-pvc-backup-${RUN_ID}.XXXXXX")
DIAGNOSTIC_FILE="$WORKDIR/diagnostics.log"
: > "$DIAGNOSTIC_FILE"
chmod 600 "$DIAGNOSTIC_FILE"
# Keep command diagnostics private; operator-facing output uses fixed labels.
exec 2>>"$DIAGNOSTIC_FILE"
MODE=''
ORIGINAL_REPLICAS=''
WRITERS_SCALED=0
READER_CREATED=0
READER_POD=''
LOCK_HELD=0
EVIDENCE_PENDING=0
BACKUP_UPLOAD_STATUS=''
FAILURE_STAGE=''

usage() { printf '%s\n' "usage: $0 --check|--go" >&2; }

run_timeout() { timeout "$@"; }
kctl() { run_timeout "${PORTAL_KUBECTL_TIMEOUT_SECONDS:-120}" sudo k3s kubectl "$@"; }
fail() { return 1; }

run_private() {
  "$@" >>"$DIAGNOSTIC_FILE" 2>&1
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
  [ -f "$RUNTIME_MARKER" ] && [ -r "$RUNTIME_MARKER" ] || return 1
  [ "$(tr -d '\r\n' < "$RUNTIME_MARKER")" = k3s ] || return 1
  [ -r "$RECIPIENT" ] && [ -r "$IDENTITY" ] || return 1
  [ -d "$(dirname -- "$EVIDENCE")" ] && [ -w "$(dirname -- "$EVIDENCE")" ] || return 1
  for command_name in age rclone sqlite3 python3 sudo k3s timeout tar find sha256sum awk grep xargs mktemp; do
    command -v "$command_name" >/dev/null || return 1
  done

  local nodes ready_count replicas files_phase state_phase
  nodes=$(kctl get nodes --no-headers) || return 1
  ready_count=$(printf '%s\n' "$nodes" | awk '$2 == "Ready" { count++ } END { print count + 0 }')
  [ "$ready_count" -eq 1 ] || return 1
  replicas=$(kctl -n "$NAMESPACE" get deployment "$DEPLOYMENT" -o jsonpath='{.spec.replicas}') || return 1
  [ "$replicas" = 1 ] || return 1
  files_phase=$(kctl -n "$NAMESPACE" get "pvc/$FILES_PVC" -o jsonpath='{.status.phase}') || return 1
  state_phase=$(kctl -n "$NAMESPACE" get "pvc/$STATE_PVC" -o jsonpath='{.status.phase}') || return 1
  [ "$files_phase" = Bound ] && [ "$state_phase" = Bound ]
}

assert_remote_access() {
  FAILURE_STAGE='remote_preflight'
  run_private rclone lsd --max-depth 1 --log-level ERROR "$REMOTE"
}

acquire_lock() {
  FAILURE_STAGE='lock'
  local holder_identity="portal-pvc-backup-$RUN_ID" observed_holder=''
  if ! kctl -n personal-server create -f - <<YAML >>"$DIAGNOSTIC_FILE" 2>&1
apiVersion: coordination.k8s.io/v1
kind: Lease
metadata:
  name: $LEASE_NAME
  namespace: personal-server
spec:
  holderIdentity: $holder_identity
  leaseDurationSeconds: 1800
YAML
  then
    observed_holder=$(kctl -n personal-server get lease "$LEASE_NAME" -o jsonpath='{.spec.holderIdentity}') || observed_holder=''
    if [ "$observed_holder" = "$holder_identity" ]; then
      kctl -n personal-server delete lease "$LEASE_NAME" --ignore-not-found >>"$DIAGNOSTIC_FILE" 2>&1 || true
    fi
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
  local mount_path=$1 destination=$2
  mkdir -p -- "$destination"
  run_timeout "${PORTAL_STREAM_TIMEOUT_SECONDS:-120}" sudo k3s kubectl -n "$NAMESPACE" exec -i "$READER_POD" -- tar -C "$mount_path" -cf - . >>"$DIAGNOSTIC_FILE" 2>&1 |
    tar -C "$destination" -xf - >>"$DIAGNOSTIC_FILE" 2>&1
}

evidence_is_current_k3s_pvc() {
  [ -f "$EVIDENCE" ] || return 1
  python3 "$SCRIPT_DIR/validate-backup-evidence.py" --evidence "$EVIDENCE" --max-age-seconds "$MAX_AGE" >/dev/null 2>&1 || return 1
  local evidence_digest evidence_runtime
  evidence_digest=$(awk -F= '$1 == "source_digest" { print $2 }' "$EVIDENCE")
  evidence_runtime=$(awk -F= '$1 == "source_runtime" { print $2 }' "$EVIDENCE")
  [ "$evidence_digest" = "$SOURCE_DIGEST" ] && [ "$evidence_runtime" = k3s-pvc ]
}

cleanup() {
  local status=$? restore_ok=1
  trap - EXIT INT TERM HUP
  if [ "$READER_CREATED" -eq 1 ]; then
    kctl -n personal-server delete pod "$READER_POD" --ignore-not-found --wait=true >>"$DIAGNOSTIC_FILE" 2>&1 || restore_ok=0
  fi
  if [ "$WRITERS_SCALED" -eq 1 ] && [ "${ORIGINAL_REPLICAS:-0}" -gt 0 ] 2>/dev/null; then
    if ! kctl -n personal-server scale "deployment/$DEPLOYMENT" --replicas="$ORIGINAL_REPLICAS" >>"$DIAGNOSTIC_FILE" 2>&1; then
      restore_ok=0
    elif ! kctl -n personal-server rollout status "deployment/$DEPLOYMENT" --timeout=120s >>"$DIAGNOSTIC_FILE" 2>&1; then
      FAILURE_STAGE='portal_readiness'
      restore_ok=0
    else
      PORTAL_POD=$(kctl -n personal-server get pod -l app.kubernetes.io/name=portal-web -o jsonpath='{.items[0].metadata.name}') || PORTAL_POD=''
      if [ -z "$PORTAL_POD" ] || ! kctl -n personal-server exec "$PORTAL_POD" -- python3 -c 'import urllib.request; response = urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=10); raise SystemExit(0 if response.status == 200 else 1)' >>"$DIAGNOSTIC_FILE" 2>&1; then
        FAILURE_STAGE='portal_health'
        restore_ok=0
      fi
    fi
    WRITERS_SCALED=0
  fi
  if [ "$LOCK_HELD" -eq 1 ]; then
    kctl -n personal-server delete lease "$LEASE_NAME" --ignore-not-found >>"$DIAGNOSTIC_FILE" 2>&1 || restore_ok=0
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
    fi
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
on_signal() { exit 130; }
trap cleanup EXIT
trap on_signal INT TERM HUP

assert_preflight || exit 1
assert_remote_access || exit 1
FAILURE_STAGE=''
[ "$MODE" = --check ] && exit 0

acquire_lock || exit 1
FAILURE_STAGE=''

stage="$WORKDIR/stage"
restore="$WORKDIR/restore"
mkdir -p -- "$stage/data" "$restore"
ORIGINAL_REPLICAS=$(kctl -n "$NAMESPACE" get deployment "$DEPLOYMENT" -o jsonpath='{.spec.replicas}')
case "$ORIGINAL_REPLICAS" in ''|*[!0-9]*) exit 1 ;; esac
# Mark restoration as required before the scale request: timeout/failure is ambiguous.
WRITERS_SCALED=1
kctl -n personal-server scale "deployment/$DEPLOYMENT" --replicas=0 >>"$DIAGNOSTIC_FILE" 2>&1
kctl -n personal-server wait --for=delete pod -l app.kubernetes.io/name=portal-web --timeout=120s >>"$DIAGNOSTIC_FILE" 2>&1
create_reader_pod
stream_pvc_tree /data/files "$stage/data/files"
stream_pvc_tree /data/portal-web-state "$stage/data/portal-web-state"
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
rclone copyto --immutable --log-level ERROR "$ciphertext" "$remote_object" >>"$DIAGNOSTIC_FILE" 2>&1
rclone copyto --log-level ERROR "$remote_object" "$WORKDIR/download.age" >>"$DIAGNOSTIC_FILE" 2>&1
[ "$artifact_digest" = "sha256:$(sha256sum "$WORKDIR/download.age" | awk '{print $1}')" ]
age -d -i "$IDENTITY" -o "$WORKDIR/restore.tar" "$WORKDIR/download.age" >>"$DIAGNOSTIC_FILE" 2>&1
tar -C "$restore" -xf "$WORKDIR/restore.tar" >>"$DIAGNOSTIC_FILE" 2>&1
assert_regular_tree "$restore/data/files"
assert_regular_tree "$restore/data/portal-web-state"
[ "$(tree_digest "$restore/data/files")" = "$files_digest" ]
[ "$(tree_digest "$restore/data/portal-web-state")" = "$state_digest" ]
grep -Fxq "source_runtime=k3s-pvc" "$restore/manifest.txt"
grep -Fxq "source_digest=$SOURCE_DIGEST" "$restore/manifest.txt"
sqlite3 "$restore/data/portal-web-state/homeops.sqlite3" 'PRAGMA quick_check;' 2>>"$DIAGNOSTIC_FILE" | grep -Fxq ok
ARTIFACT_DIGEST="$artifact_digest"
EVIDENCE_PENDING=1
BACKUP_UPLOAD_STATUS='UPLOADED'
