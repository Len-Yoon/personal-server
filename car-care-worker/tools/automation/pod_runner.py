#!/usr/bin/env python3
"""One non-restarting Pod execution; all persistent backup evidence stays on host."""
from datetime import datetime, timedelta, timezone
import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('scheduled_car_backup', ROOT/'car-care-worker/tools/backup.py')
BACKUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BACKUP)


def force_restore(records, now):
    proofs = BACKUP.POLICY.successful(records, now)
    if not proofs:
        return True
    verified = BACKUP.POLICY.timestamp(proofs[0].get('restore_verified_at', proofs[0]['finished_at']))
    return now.astimezone(BACKUP.POLICY.KST).weekday() == 6 or now-verified >= timedelta(days=7)


def main():
    os.umask(0o077)
    scratch = Path('/work/scratch')
    scratch.mkdir(mode=0o700)
    scratch.chmod(0o700)  # Clear inherited setgid from fsGroup-created tmpfs.
    evidence = Path('/state/evidence')
    BACKUP.preflight(Path('/input/car-care.sqlite3'), Path('/credentials'), scratch, evidence)
    records = BACKUP.POLICY.read_records(evidence)
    BACKUP.POLICY.require_resolved(records)
    record = BACKUP.execute(Path('/input/car-care.sqlite3'), Path('/credentials'), scratch, evidence,
                            if_changed=not force_restore(records, datetime.now(timezone.utc)))
    print('car_care_backup=SKIP_UNCHANGED' if record['status'] == 'skipped_unchanged'
          else 'car_care_backup=PASS remote_restore=verified')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('car_care_backup=FAIL')
        raise SystemExit(1)
