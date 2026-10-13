#!/usr/bin/env python3
"""Explicit DB-only encrypted backup and remote restore check; no scheduler changes.

Use pre-provisioned age/rclone credential files. --check is read-only and never
contacts the remote. --go uploads one unique artifact and downloads that exact
artifact for validation. OAuth volumes and vehicle credentials are excluded.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import resource
import shlex
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    'car_backup_restore_helpers', ROOT / 'infra/k8s/tools/portal-backup-restore-check.py')
HELPERS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPERS)
REMOTE = 'gdrive:PersonalServer-encrypted-backups/car-care'
TABLES = {'maintenance_records', 'vehicle_snapshots', 'alert_states',
          'tire_changes', 'telegram_command_results'}
MAX_BYTES = 512 * 1024 * 1024


def require(condition):
    if not condition:
        raise ValueError('backup validation failed')


def private_directory(path):
    require(path.is_dir() and not path.is_symlink())
    info = path.stat()
    require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700)


def inspect_database(path):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_BYTES)
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as db:
        require(db.execute('PRAGMA quick_check').fetchall() == [('ok',)])
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        require(tables - {'sqlite_sequence'} == TABLES)


def snapshot_database(source, target):
    """SQLite backup includes committed WAL content without copying live files."""
    inspect_database(source)
    require(not target.exists())
    deadline = time.monotonic() + 60
    with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as reader:
        page_size = reader.execute('PRAGMA page_size').fetchone()[0]
    def progress(status, remaining, total):
        require(time.monotonic() < deadline and total * page_size <= MAX_BYTES)
    with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as reader:
        with sqlite3.connect(target) as writer:
            reader.backup(writer, pages=128, progress=progress, sleep=0.05)
    target.chmod(0o600)
    inspect_database(target)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def limit_output_size():
    inherited = resource.getrlimit(resource.RLIMIT_FSIZE)
    cap = MAX_BYTES + 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, tuple(cap if value == resource.RLIM_INFINITY
                          else min(cap, value) for value in inherited))


def run_command(args, **kwargs):
    # Never reveal provider stderr, tokens, identity material, or database values.
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=120, preexec_fn=limit_output_size,
                   env={key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}, **kwargs)


def preflight(database, credentials, scratch, evidence):
    require(database.name == 'car-care.sqlite3' and database.resolve() == database.absolute())
    inspect_database(database)
    for name in ('age-recipient', 'age-identity', 'rclone-config', 'rclone-config-passphrase'):
        require((credentials / name).is_file())
    HELPERS.encrypted_config_snapshot(credentials / 'rclone-config', allow_symlink=True)
    for directory in (scratch, evidence):
        private_directory(directory)
    # Scratch cleanup must never remove credentials or the evidence journal.
    require(not credentials.resolve().is_relative_to(scratch.resolve()))
    require(not scratch.resolve().is_relative_to(credentials.resolve()))
    require(not evidence.resolve().is_relative_to(scratch.resolve()))
    require(not scratch.resolve().is_relative_to(evidence.resolve()))
    require(not database.resolve().is_relative_to(scratch.resolve()))
    # Only encrypted rclone config copies are allowed; their mutable refresh
    # state lives in memory and is discarded, never persisted to the original.
    require(HELPERS.memory_backed(scratch.resolve()))
    require(all(shutil.which(binary) for binary in ('age', 'rclone')))


def execute(database, credentials, scratch, evidence, runner=run_command):
    backup_id = datetime.now(timezone.utc).strftime('car-care-%Y%m%dT%H%M%SZ-') + secrets.token_hex(8)
    record = {'schema_version': 1, 'scope': 'car-care-db-only', 'backup_id': backup_id,
              'status': 'failure', 'stage': 'preflight', 'remote_uploaded': False,
              'remote_upload_status': 'not_attempted',
              'restore_verified': False}
    # Evidence directory is checked before creating any evidence file.
    private_directory(evidence)
    evidence_path = evidence / (backup_id + '.json')
    try:
        preflight(database, credentials, scratch, evidence)
        with tempfile.TemporaryDirectory(prefix='car-care-', dir=scratch) as temporary:
            work = Path(temporary)
            source = work / 'car-care.sqlite3'
            artifact = work / (backup_id + '.sqlite3.age')
            downloaded = work / 'downloaded.age'
            restored = work / 'restored.sqlite3'
            record['stage'] = 'snapshot'
            snapshot_database(database, source)
            source_hash = digest(source)
            record['stage'] = 'encrypt'
            runner(['age', '-R', str(credentials / 'age-recipient'), '-o', str(artifact), str(source)])
            require(0 < artifact.stat().st_size <= MAX_BYTES + 1024 * 1024)
            artifact_hash = digest(artifact)
            remote = REMOTE + '/' + artifact.name
            common = ['rclone', '--config', str(credentials / 'rclone-config'),
                      '--password-command', '/usr/bin/cat ' + shlex.quote(str(credentials / 'rclone-config-passphrase')),
                      '--retries', '1', '--low-level-retries', '1']
            for stage, args in (
                ('upload', [*common, 'copyto', '--immutable', str(artifact), remote]),
                ('download', [*common, 'copyto', '--max-transfer', str(MAX_BYTES + 1024 * 1024), remote, str(downloaded)]),
            ):
                record['stage'] = stage
                if stage == 'upload':
                    record['remote_upload_status'] = 'uncertain'
                HELPERS.download_with_memory_config(args, runner, scratch, credentials)
                if stage == 'upload':
                    record['remote_uploaded'] = True
                    record['remote_upload_status'] = 'confirmed'
            require(0 < downloaded.stat().st_size <= MAX_BYTES + 1024 * 1024)
            require(digest(downloaded) == artifact_hash)
            record['stage'] = 'restore'
            runner(['age', '-d', '-i', str(credentials / 'age-identity'), '-o', str(restored), str(downloaded)])
            inspect_database(restored)
            require(digest(restored) == source_hash)
            record.update(status='success', stage='complete', restore_verified=True,
                          artifact_sha256=artifact_hash, snapshot_sha256=source_hash)
    finally:
        record['finished_at'] = datetime.now(timezone.utc).isoformat()
        # Each attempt has its own exclusive evidence: failed attempts cannot
        # leave an earlier PASS under the current attempt's identifier.
        descriptor = os.open(evidence_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(record, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(evidence, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--go', action='store_true')
    for name in ('database', 'credentials', 'scratch', 'evidence'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    previous_umask = os.umask(0o077)
    try:
        if args.check:
            preflight(args.database, args.credentials, args.scratch, args.evidence)
            print('car_care_backup=CHECK remote=not_contacted')
        else:
            execute(args.database, args.credentials, args.scratch, args.evidence)
            print('car_care_backup=PASS remote_restore=verified')
        return 0
    except Exception:
        print('car_care_backup=FAIL')
        return 1
    finally:
        os.umask(previous_umask)


if __name__ == '__main__':
    raise SystemExit(main())
