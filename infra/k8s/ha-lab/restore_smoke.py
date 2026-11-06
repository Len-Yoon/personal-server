"""Whole seven-service restore evidence; only disposable authenticated PG."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ha_runtime import backup
from ha_runtime.config import DatabaseUnavailable, _private


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("backup", "restore"), required=True)
    parser.add_argument("--admin", default="/fixture/admin.json")
    parser.add_argument("--objects", default="/fixture/objects")
    parser.add_argument("--artifact", default="/fixture/results/full-backup.aesgcm")
    parser.add_argument("--key", default="/fixture/results/full-backup.key")
    parser.add_argument("--cluster-file", default="/fixture/results/full-backup-cluster.json")
    args = parser.parse_args()
    try:
        admin = json.loads(_private(args.admin).read_text())
        backup._guard(admin)
        if args.phase == "backup":
            backup._write_new(Path(args.key), secrets.token_bytes(32))
            with backup._connect(admin) as connection:
                cluster = connection.execute("SELECT cluster FROM ha.control WHERE singleton=1").fetchone()[0]
            backup._write_new(Path(args.cluster_file), json.dumps({"cluster": cluster}).encode())
            # Prove missing/corrupt referenced objects are refused before backup
            # publication. Originals are immutable and never modified here.
            from tempfile import TemporaryDirectory
            with backup._connect(admin) as connection:
                references = backup.object_references(connection)
            if not references:
                raise DatabaseUnavailable("restore_fixture_requires_attachment")
            sha, size = next(iter(references.items()))
            with TemporaryDirectory(dir=Path(args.artifact).parent) as temporary:
                root = Path(temporary)
                for payload in (None, b"corrupted synthetic blob"):
                    target = root / sha
                    if payload is not None:
                        backup._write_new(target, payload, 0o400)
                    try:
                        backup._blob(root, sha, size)
                    except DatabaseUnavailable:
                        continue
                    raise DatabaseUnavailable("restore_corruption_guard_failed")
            result = backup.create_backup(admin, args.objects, args.artifact, args.key)
            # Authentication failure is checked against the real encrypted
            # artifact with a modified tag, before any database mutation.
            encrypted = _private(args.artifact).read_bytes()
            try:
                backup._open(encrypted[:-1] + bytes([encrypted[-1] ^ 1]), backup._key(args.key))
            except DatabaseUnavailable:
                result["tampered_artifact_refused"] = True
            else:
                raise DatabaseUnavailable("restore_authentication_guard_failed")
            result.update(missing_blob_refused=True, corrupt_blob_refused=True,
                          encrypted_artifact=True, artifact_sha256=hashlib.sha256(encrypted).hexdigest())
        else:
            cluster = json.loads(_private(args.cluster_file).read_text())["cluster"]
            result = backup.restore_backup(admin, args.objects, args.artifact, args.key, expected_cluster=cluster)
        print(json.dumps({"status": "PASS", "phase": args.phase, "checks": result}, sort_keys=True))
        return 0
    except Exception as error:
        # No libpq SQL, paths, credentials or source/business values escape.
        reason = str(error) if isinstance(error, DatabaseUnavailable) and str(error).replace("_", "").isalnum() else "isolated_restore_check_failed"
        print(json.dumps({"status": "FAIL", "phase": args.phase, "reason": reason}, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
