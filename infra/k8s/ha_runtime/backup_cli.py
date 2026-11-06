"""Explicit whole HA backup/restore utility; emits only static status and hashes."""
import argparse
import json
import sys

from . import backup
from .config import DatabaseUnavailable, _private


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("backup", "restore"))
    parser.add_argument("--admin-json", required=True)
    parser.add_argument("--objects", required=True)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--expected-cluster", required=True)
    args = parser.parse_args(argv)
    try:
        admin = json.loads(_private(args.admin_json).read_text())
        operation = backup.create_backup if args.command == "backup" else backup.restore_backup
        result = operation(admin, args.objects, args.artifact, args.key, expected_cluster=args.expected_cluster)
        print(json.dumps({"status": "PASS", "checks": result}, sort_keys=True))
        return 0
    except Exception as error:
        reason = str(error) if isinstance(error, DatabaseUnavailable) and str(error).replace("_", "").isalnum() else "whole_backup_operation_failed"
        print(json.dumps({"status": "BLOCKED", "reason": reason}, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
