#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
STATE_DIR=${RECOVERY_DRILL_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/personal-server/recovery-drills}
BACKUP_TOOL=${MONTHLY_RECOVERY_DRILL_BACKUP_TOOL:-$SCRIPT_DIR/portal-pvc-backup-verify.sh}
TELEGRAM_TOOL=${MONTHLY_RECOVERY_DRILL_TELEGRAM_TOOL:-$SCRIPT_DIR/sre-telegram-verify.sh}
POD_TOOL=${MONTHLY_RECOVERY_DRILL_POD_TOOL:-$SCRIPT_DIR/sre-pod-recovery-lab.sh}
RUN_ID=${RECOVERY_DRILL_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}
POD_RUN_ID=${RECOVERY_DRILL_POD_RUN_ID:-sre-drill-$(date -u +%Y%m%d%H%M%S)-$$}

if [ "$#" -ne 0 ]; then printf '%s\n' "사용법: $0" >&2; exit 2; fi
if [[ ! "$RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,62}$ ]]; then
  printf '%s\n' '복구 훈련 run id가 유효하지 않음' >&2; exit 2
fi
mkdir -p -- "$STATE_DIR"
chmod 700 "$STATE_DIR"
EVIDENCE="$STATE_DIR/$RUN_ID.json"
if [ -e "$EVIDENCE" ]; then
  printf '%s\n' '동일 run id의 복구 훈련 증적이 이미 존재함' >&2
  exit 1
fi
if [[ ! "$POD_RUN_ID" =~ ^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$ ]]; then
  printf '%s\n' 'Pod 실습 run id가 유효하지 않음' >&2
  exit 2
fi
TMP_OUTPUT=$(mktemp "${TMPDIR:-/tmp}/recovery-drill-output.XXXXXX")
chmod 600 "$TMP_OUTPUT"
TMP_EVIDENCE=$(mktemp "$STATE_DIR/.${RUN_ID}.XXXXXX")
chmod 600 "$TMP_EVIDENCE"
cleanup_tmp() { rm -f -- "$TMP_OUTPUT" "$TMP_EVIDENCE"; }
trap cleanup_tmp EXIT

started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
failed_stage=''
backup_status=not_run
telegram_status=not_run
pod_status=not_run
pod_cleanup=false

run_quiet() {
  : > "$TMP_OUTPUT"
  "$@" >"$TMP_OUTPUT" 2>&1
}

if run_quiet "$BACKUP_TOOL" --check; then backup_status=success; else backup_status=failed; failed_stage=backup; fi
if [ -z "$failed_stage" ]; then
  if run_quiet "$TELEGRAM_TOOL"; then telegram_status=success; else telegram_status=failed; failed_stage=telegram; fi
fi
if [ -z "$failed_stage" ]; then
  if SRE_RECOVERY_LAB_RUN_ID="$POD_RUN_ID" run_quiet "$POD_TOOL" --run; then
    pod_status=success
    # The lab tool owns cleanup after it creates the namespace. Do not issue a
    # second cleanup: an AlreadyExists failure means this run owns nothing.
    pod_cleanup=true
  else
    pod_status=failed
    failed_stage=pod
  fi
fi

completed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
status=success
[ -n "$failed_stage" ] && status=failed
python3 - "$TMP_EVIDENCE" "$RUN_ID" "$POD_RUN_ID" "$started_at" "$completed_at" "$status" "${failed_stage:-}" "$backup_status" "$telegram_status" "$pod_status" "$pod_cleanup" <<'PY'
import json, os, sys
path, run_id, pod_run_id, started, completed, status, failed, backup, telegram, pod, cleanup = sys.argv[1:]
payload = {
    "run_id": run_id,
    "pod_run_id": pod_run_id,
    "started_at": started,
    "completed_at": completed,
    "status": status,
    "failed_stage": failed or None,
    "stages": [
        {"name": "backup", "status": backup},
        {"name": "telegram", "status": telegram},
        {"name": "pod", "status": pod, "cleanup": cleanup == "true"},
    ],
}
with open(path, "w", encoding="utf-8") as stream:
    json.dump(payload, stream, ensure_ascii=False, indent=2)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
PY
if ln "$TMP_EVIDENCE" "$EVIDENCE"; then
  rm -f -- "$TMP_EVIDENCE"
else
  printf '%s\n' '복구 훈련 증적 publish 충돌; 기존 증적을 보존함' >&2
  exit 1
fi
if [ "$status" = success ]; then
  printf 'recovery_drill=PASS\nrecovery_drill_run_id=%s\n' "$RUN_ID"
  exit 0
fi
printf 'recovery_drill=FAIL\nrecovery_drill_run_id=%s\nrecovery_drill_stage=%s\n' "$RUN_ID" "$failed_stage" >&2
exit 1
