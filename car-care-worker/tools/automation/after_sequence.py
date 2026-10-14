#!/usr/bin/env python3
"""Start car backup only after today's four PVC backup stages passed."""
import argparse
from datetime import datetime, time, timedelta, timezone
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from schedule import SEQUENCE


def validate_completed(state, now):
    SEQUENCE.validate_state_identity(state)
    local = now.astimezone(SEQUENCE.KST)
    data = state.get('data', {})
    if (data.get('status') != 'completed' or data.get('current') != 'none'
            or data.get('run_date') != local.date().isoformat()
            or SEQUENCE.load_results(state, data['run_date']) != {s:'passed' for s in SEQUENCE.ORDER}):
        raise ValueError('PVC sequence did not complete successfully today')
    updated = SEQUENCE.utc_time(data.get('updated_at'))
    earliest = datetime.combine(local.date(), time(2,30), tzinfo=SEQUENCE.KST)
    if not earliest <= updated <= now.astimezone(timezone.utc)+timedelta(seconds=30):
        raise ValueError('PVC sequence completion time mismatch')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--go', action='store_true')
    args = parser.parse_args()
    try:
        validate_completed(SEQUENCE.KubectlAPI().get_state(), datetime.now(timezone.utc))
        if args.go:
            subprocess.run(['/usr/bin/systemctl','--user','start','personal-server-car-care-backup.service'],
                           check=True, timeout=14460, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print('car_after_sequence=PASS')
        return 0
    except (ValueError, TypeError, KeyError, SEQUENCE.APIError, SEQUENCE.SafetyError, OSError, subprocess.SubprocessError):
        print('car_after_sequence=BLOCKED')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
