#!/usr/bin/env bash
set -euo pipefail

# Operator-only Portal backup. Credentials and age private keys remain outside Git.
umask 077
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../../.." && pwd)
FILES_SOURCE=${PORTAL_FILES_SOURCE:-$REPO_ROOT/data/files}
STATE_SOURCE=${PORTAL_STATE_SOURCE:-$REPO_ROOT/data/portal-web-state}
RUNTIME_MARKER=${PORTAL_RUNTIME_MARKER:-$REPO_ROOT/data/portal-runtime.mode}
EVIDENCE=${PORTAL_BACKUP_EVIDENCE:-$REPO_ROOT/.portal-backup-verified}
RECIPIENT=${PORTAL_AGE_RECIPIENT:-$HOME/.local/share/personal-server/age/recipient.txt}
IDENTITY=${PORTAL_AGE_IDENTITY:-$HOME/.local/share/personal-server/age/identity.txt}
REMOTE=${PORTAL_BACKUP_REMOTE:-gdrive:PersonalServer-encrypted-backups}
MAX_AGE=${PORTAL_BACKUP_MAX_AGE_SECONDS:-86400}
COMPOSE_SOURCE_RUNTIME='compose-local'
RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)-$$
WORKDIR=$(mktemp -d "${TMPDIR:-/tmp}/portal-backup-${RUN_ID}.XXXXXX")
PORTAL_PAUSED=0

cleanup() {
  local status=$?
  if [ "$PORTAL_PAUSED" -eq 1 ]; then docker unpause portal-web >/dev/null 2>&1 || true; fi
  rm -rf -- "$WORKDIR"
  [ "$status" -eq 0 ] || rm -f -- "$EVIDENCE"
  return "$status"
}
trap cleanup EXIT INT TERM HUP
fail() { rm -f -- "$EVIDENCE"; printf '%s\n' 'portal_backup_verify=FAIL' >&2; exit 1; }
fail_reason() { rm -f -- "$EVIDENCE"; printf '%s\n' 'portal_backup_verify=FAIL' "reason=$1" >&2; exit 1; }
tree_digest() { (cd -- "$1" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum) | sha256sum | awk '{print $1}'; }
utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
assert_regular_tree() {
  local entry
  entry=$(find "$1" \( -type l -o -type b -o -type c -o -type p -o -type s \) -print -quit) || return 1
  if [ -n "$entry" ]; then
    printf '%s\n' 'unsupported filesystem entry' >&2
    return 1
  fi
}

[ "$MAX_AGE" -ge 1 ] 2>/dev/null || fail
if [ -e "$RUNTIME_MARKER" ] || [ -L "$RUNTIME_MARKER" ]; then
  [ -f "$RUNTIME_MARKER" ] && [ -r "$RUNTIME_MARKER" ] || fail_reason 'runtime marker is not a readable file'
  runtime_marker=$(<"$RUNTIME_MARKER")
  case "$runtime_marker" in
    k3s|cutover) fail_reason 'local source backup blocked while PVC-backed runtime is active' ;;
    compose) : ;;
    *) fail_reason 'unrecognized runtime marker; local source backup blocked' ;;
  esac
fi
[ -d "$FILES_SOURCE" ] && [ -d "$STATE_SOURCE" ] && [ -f "$STATE_SOURCE/homeops.sqlite3" ] || fail
[ -r "$RECIPIENT" ] && [ -r "$IDENTITY" ] || fail
assert_regular_tree "$FILES_SOURCE" && assert_regular_tree "$STATE_SOURCE" || fail
command -v age >/dev/null && command -v rclone >/dev/null && command -v sqlite3 >/dev/null && command -v docker >/dev/null || fail

stage="$WORKDIR/stage"
restore="$WORKDIR/restore"
mkdir -p -- "$stage/data" "$restore"
docker pause portal-web >/dev/null || fail
PORTAL_PAUSED=1
cp -a -- "$FILES_SOURCE" "$stage/data/files"
cp -a -- "$STATE_SOURCE" "$stage/data/portal-web-state"
sqlite3 "$STATE_SOURCE/homeops.sqlite3" "PRAGMA quick_check;" | grep -Fxq ok || fail
sqlite3 "$STATE_SOURCE/homeops.sqlite3" ".backup '$stage/data/portal-web-state/homeops.sqlite3'" || fail
docker unpause portal-web >/dev/null || fail
PORTAL_PAUSED=0

files_digest=$(tree_digest "$stage/data/files")
state_digest=$(tree_digest "$stage/data/portal-web-state")
source_digest="sha256:$(printf '%s\n%s\n' "$files_digest" "$state_digest" | sha256sum | awk '{print $1}')"
if [ -f "$EVIDENCE" ] && python3 "$SCRIPT_DIR/validate-backup-evidence.py" --evidence "$EVIDENCE" --max-age-seconds "$MAX_AGE" >/dev/null 2>&1; then
  evidence_source_digest=$(awk -F= '$1 == "source_digest" { print $2 }' "$EVIDENCE")
  evidence_source_runtime=$(awk -F= '$1 == "source_runtime" { print $2 }' "$EVIDENCE")
  if [ "$evidence_source_digest" = "$source_digest" ] && [ "$evidence_source_runtime" = "$COMPOSE_SOURCE_RUNTIME" ]; then
    printf '%s\n' 'portal_backup_verify=PASS' 'backup_upload=SKIPPED_UNCHANGED'
    exit 0
  fi
fi
archive="$WORKDIR/portal-${RUN_ID}.tar"
ciphertext="$archive.age"
tar -C "$stage" -cf "$archive" data || fail
age -R "$RECIPIENT" -o "$ciphertext" "$archive" || fail
artifact_digest="sha256:$(sha256sum "$ciphertext" | awk '{print $1}')"
remote_object="$REMOTE/portal-${RUN_ID}.tar.age"
rclone copyto --immutable --log-level ERROR "$ciphertext" "$remote_object" || fail
rclone copyto --log-level ERROR "$remote_object" "$WORKDIR/download.age" || fail
[ "$artifact_digest" = "sha256:$(sha256sum "$WORKDIR/download.age" | awk '{print $1}')" ] || fail
backup_completed_at=$(utc_now)
age -d -i "$IDENTITY" -o "$WORKDIR/restore.tar" "$WORKDIR/download.age" || fail
tar -C "$restore" -xf "$WORKDIR/restore.tar" || fail
[ "$(tree_digest "$restore/data/files")" = "$files_digest" ] || fail
[ "$(tree_digest "$restore/data/portal-web-state")" = "$state_digest" ] || fail
sqlite3 "$restore/data/portal-web-state/homeops.sqlite3" "PRAGMA quick_check;" | grep -Fxq ok || fail
restore_verified_at=$(utc_now)
evidence_expires_at=$(date -u -d "+${MAX_AGE} seconds" +%Y-%m-%dT%H:%M:%SZ) || fail

evidence_dir=$(dirname -- "$EVIDENCE")
mkdir -p -- "$evidence_dir"
tmp_evidence=$(mktemp "$evidence_dir/.portal-backup-verified.XXXXXX")
chmod 600 "$tmp_evidence"
printf '%s\n' \
  'schema_version=1' 'scope=portal' 'backup_status=success' 'encrypted=true' \
  "backup_completed_at=$backup_completed_at" 'restore_status=success' \
  "restore_verified_at=$restore_verified_at" "evidence_expires_at=$evidence_expires_at" \
  "backup_id=portal-$RUN_ID" "artifact_digest=$artifact_digest" \
  "source_digest=$source_digest" "source_runtime=$COMPOSE_SOURCE_RUNTIME" \
  'restore_check=sqlite_quick_check' 'restore_path_check=success' > "$tmp_evidence"
python3 "$SCRIPT_DIR/validate-backup-evidence.py" --evidence "$tmp_evidence" --max-age-seconds "$MAX_AGE" >/dev/null || { rm -f -- "$tmp_evidence"; fail; }
mv -- "$tmp_evidence" "$EVIDENCE"
printf '%s\n' 'portal_backup_verify=PASS'
