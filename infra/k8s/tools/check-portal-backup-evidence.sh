#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [ -n "${PORTAL_BACKUP_EVIDENCE_KUBECTL:-}" ]; then
  KCTL=("$PORTAL_BACKUP_EVIDENCE_KUBECTL")
else
  KCTL=(sudo -n k3s kubectl)
fi
PORTAL_NAMESPACE=personal-server
EVIDENCE_CONFIGMAP=portal-pvc-backup-evidence
MAX_AGE_SECONDS=${PORTAL_BACKUP_EVIDENCE_MAX_AGE_SECONDS:-86400}

if [ "$#" -ne 0 ]; then
  printf '사용법: %s\n' "$0" >&2
  exit 2
fi

evidence_file=$(mktemp "${TMPDIR:-/tmp}/portal-backup-evidence.XXXXXX")
chmod 600 "$evidence_file"
cleanup() { rm -f -- "$evidence_file"; }
trap cleanup EXIT

if ! "${KCTL[@]}" -n "$PORTAL_NAMESPACE" get configmap "$EVIDENCE_CONFIGMAP" \
  -o jsonpath='{.data.evidence}' >"$evidence_file"; then
  printf 'portal_backup_evidence=FAIL\n' >&2
  exit 1
fi

if ! python3 "$SCRIPT_DIR/validate-backup-evidence.py" \
  --evidence "$evidence_file" --max-age-seconds "$MAX_AGE_SECONDS" >/dev/null; then
  printf 'portal_backup_evidence=FAIL\n' >&2
  exit 1
fi

printf 'portal_backup_evidence=PASS\n'
