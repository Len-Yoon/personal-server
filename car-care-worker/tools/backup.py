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
POLICY_SPEC = importlib.util.spec_from_file_location('car_backup_policy', Path(__file__).with_name('backup_policy.py'))
POLICY = importlib.util.module_from_spec(POLICY_SPEC)
POLICY_SPEC.loader.exec_module(POLICY)
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
    subprocess.run(args, check=True, stdout=kwargs.pop('stdout', subprocess.DEVNULL),
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


def _execute(database, credentials, scratch, evidence, runner, if_changed):
    backup_id = datetime.now(timezone.utc).strftime('car-care-%Y%m%dT%H%M%SZ-') + secrets.token_hex(8)
    record = {'schema_version': 1, 'scope': 'car-care-db-only', 'backup_id': backup_id,
              'status': 'failure', 'stage': 'preflight', 'remote_uploaded': False,
              'remote_upload_status': 'not_attempted',
              'restore_verified': False}
    # Evidence directory is checked before creating any evidence file.
    private_directory(evidence)
    evidence_path = evidence / (backup_id + '.json')
    POLICY.persist(evidence_path, record, initial=True)
    try:
        preflight(database, credentials, scratch, evidence)
        record['stage'] = 'unresolved_remote'
        previous = POLICY.read_records(evidence)
        POLICY.require_resolved(previous)
        with tempfile.TemporaryDirectory(prefix='car-care-', dir=scratch) as temporary:
            work = Path(temporary)
            source = work / 'car-care.sqlite3'
            artifact = work / (backup_id + '.sqlite3.age')
            downloaded = work / 'downloaded.age'
            restored = work / 'restored.sqlite3'
            record['stage'] = 'snapshot'
            snapshot_database(database, source)
            source_hash = digest(source)
            content_hash = POLICY.fingerprint(source)
            record['content_sha256'] = content_hash
            proofs = POLICY.successful(previous)
            if if_changed and proofs and proofs[0].get('content_sha256') == content_hash:
                record.update(status='skipped_unchanged', stage='complete',
                              previous_backup_id=proofs[0]['backup_id'],
                              previous_restore_verified_at=proofs[0].get('restore_verified_at', proofs[0]['finished_at']))
                return record
            record['stage'] = 'encrypt'
            runner(['age', '-R', str(credentials / 'age-recipient'), '-o', str(artifact), str(source)])
            require(0 < artifact.stat().st_size <= MAX_BYTES + 1024 * 1024)
            artifact_hash = digest(artifact)
            remote = REMOTE + '/' + artifact.name
            common = rclone_common(credentials)
            for stage, args in (
                ('upload', [*common, 'copyto', '--immutable', str(artifact), remote]),
                ('download', [*common, 'copyto', '--max-transfer', str(MAX_BYTES + 1024 * 1024), remote, str(downloaded)]),
            ):
                record['stage'] = stage
                if stage == 'upload':
                    record['remote_upload_status'] = 'uncertain'
                    POLICY.persist(evidence_path, record)
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
                          artifact_sha256=artifact_hash, snapshot_sha256=source_hash,
                          restore_verified_at=datetime.now(timezone.utc).isoformat())
    finally:
        record['finished_at'] = datetime.now(timezone.utc).isoformat()
        POLICY.persist(evidence_path, record)
    return record


def execute(database, credentials, scratch, evidence, runner=run_command, *, if_changed=False):
    private_directory(evidence)
    with POLICY.execution_lock(evidence):
        return _execute(database, credentials, scratch, evidence, runner, if_changed)


def rclone_common(credentials):
    return ['rclone', '--config', str(credentials / 'rclone-config'),
            '--password-command', '/usr/bin/cat ' + shlex.quote(str(credentials / 'rclone-config-passphrase')),
            '--retries', '1', '--low-level-retries', '1']


def remote_inventory(credentials, scratch, work, runner):
    output = work / 'inventory.json'
    with output.open('wb') as stream:
        HELPERS.download_with_memory_config([*rclone_common(credentials), 'lsjson', REMOTE],
                                            runner, scratch, credentials, stdout=stream)
    require(output.stat().st_size <= 4 * 1024 * 1024)
    items = json.loads(output.read_text())
    require(isinstance(items, list) and len(items) <= 10000)
    inventory = {}
    identities = set()
    for item in items:
        require(isinstance(item, dict))
        name = item.get('Name')
        require(isinstance(name, str) and name.endswith('.sqlite3.age'))
        POLICY.created(name[:-12])
        require(item.get('Path') == name and item.get('IsDir') is False and name not in inventory)
        require(type(item.get('Size')) is int and 0 < item['Size'] <= MAX_BYTES + 1024 * 1024)
        identity = item.get('ID')
        require(isinstance(identity, str) and identity and identity not in identities)
        require(POLICY.timestamp(item.get('ModTime')) <= datetime.now(timezone.utc))
        identities.add(identity)
        inventory[name] = {key: item[key] for key in ('ID', 'Size', 'ModTime')}
    return inventory


def verified_download(name, proof, credentials, scratch, work, runner, *, restore=False):
    require(name == proof['backup_id'] + '.sqlite3.age')
    downloaded = work / 'retention-download.age'
    HELPERS.download_with_memory_config([*rclone_common(credentials), 'copyto', '--max-transfer',
        str(MAX_BYTES + 1024 * 1024), REMOTE + '/' + name, str(downloaded)], runner, scratch, credentials)
    require(0 < downloaded.stat().st_size <= MAX_BYTES + 1024 * 1024)
    require(digest(downloaded) == proof['artifact_sha256'])
    if restore:
        restored = work / 'retention-restored.sqlite3'
        runner(['age', '-d', '-i', str(credentials / 'age-identity'), '-o', str(restored), str(downloaded)])
        inspect_database(restored)
        require(digest(restored) == proof['snapshot_sha256'])
        restored.unlink()
    downloaded.unlink()


def retain(database, credentials, scratch, evidence, *, delete=False,
           expected_plan_sha256=None, confirm_remote=None, runner=run_command):
    """Plan offline by default. Destruction needs exact plan and scope confirmation."""
    private_directory(evidence)
    with POLICY.execution_lock(evidence):
        preflight(database, credentials, scratch, evidence)
        records = POLICY.read_records(evidence)
        POLICY.require_resolved(records)
        now = datetime.now(timezone.utc)
        plan = POLICY.retention_plan(records, now=now)
        if not delete:
            return plan
        require(confirm_remote == REMOTE and expected_plan_sha256 == plan['plan_sha256'])
        proofs = {r['backup_id']+'.sqlite3.age': r for r in POLICY.successful(records, now)}
        protected = proofs[plan['protected']]
        if plan['delete']:
            require((now - POLICY.timestamp(protected.get('restore_verified_at', protected['finished_at']))).total_seconds() <= 86400)
        backup_id = now.strftime('car-care-%Y%m%dT%H%M%SZ-') + secrets.token_hex(8)
        path = evidence / (backup_id + '.json')
        record = dict(schema_version=1, scope=POLICY.SCOPE, backup_id=backup_id, operation='retention',
                      status='failure', stage='inventory', deleted=[], remote_delete_status='not_attempted',
                      plan_sha256=plan['plan_sha256'], restore_verified=False, kept_count=len(plan['keep']))
        POLICY.persist(path, record, initial=True)
        if not plan['delete']:
            record.update(status='no_op', stage='complete', finished_at=datetime.now(timezone.utc).isoformat())
            POLICY.persist(path, record)
            return dict(plan, deleted=[])
        try:
            with tempfile.TemporaryDirectory(prefix='car-care-retention-', dir=scratch) as temporary:
                work = Path(temporary)
                inventory = remote_inventory(credentials, scratch, work, runner)
                # Unknown files, duplicate names, missing known files, or changed
                # inventory block deletion. Never expand beyond recorded car scope.
                require(set(inventory) == set(proofs))
                record['stage'] = 'protected_restore'
                verified_download(plan['protected'], protected, credentials, scratch, work, runner, restore=True)
                record['protected_restore_verified_at'] = datetime.now(timezone.utc).isoformat()
                for name in plan['delete']:
                    require(name != plan['protected'] and len(inventory) > 1)
                    record['stage'] = 'candidate_digest'
                    verified_download(name, proofs[name], credentials, scratch, work, runner)
                    require(remote_inventory(credentials, scratch, work, runner) == inventory)
                    record.update(stage='delete', remote_delete_status='uncertain', pending_delete=name)
                    POLICY.persist(path, record)
                    HELPERS.download_with_memory_config([*rclone_common(credentials), 'deletefile', REMOTE+'/'+name],
                                                        runner, scratch, credentials)
                    # A successful command alone is insufficient deletion proof.
                    expected = dict(inventory)
                    del expected[name]
                    require(remote_inventory(credentials, scratch, work, runner) == expected)
                    inventory = expected
                    record['deleted'].append(name)
                    record.update(remote_delete_status='confirmed')
                    record.pop('pending_delete', None)
                    POLICY.persist(path, record)
                record.update(status='success', stage='complete')
        finally:
            record['finished_at'] = datetime.now(timezone.utc).isoformat()
            POLICY.persist(path, record)
        return dict(plan, deleted=record['deleted'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--go', action='store_true')
    mode.add_argument('--retention-plan', action='store_true', help='Print offline local-evidence plan; never delete')
    mode.add_argument('--retention-go', action='store_true', help='Explicit remote retention execution')
    for name in ('database', 'credentials', 'scratch', 'evidence'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--if-changed', action='store_true', help='With --go, skip unchanged logical snapshot content')
    parser.add_argument('--expected-plan-sha256')
    parser.add_argument('--confirm-retention-remote')
    args = parser.parse_args()
    if args.if_changed and not args.go:
        parser.error('--if-changed requires --go')
    if (args.expected_plan_sha256 or args.confirm_retention_remote) and not args.retention_go:
        parser.error('retention confirmation requires --retention-go')
    previous_umask = os.umask(0o077)
    try:
        if args.check:
            preflight(args.database, args.credentials, args.scratch, args.evidence)
            print('car_care_backup=CHECK remote=not_contacted')
        elif args.retention_plan or args.retention_go:
            plan = retain(args.database, args.credentials, args.scratch, args.evidence,
                          delete=args.retention_go, expected_plan_sha256=args.expected_plan_sha256,
                          confirm_remote=args.confirm_retention_remote)
            print(json.dumps(plan, sort_keys=True))
        else:
            record = execute(args.database, args.credentials, args.scratch, args.evidence, if_changed=args.if_changed)
            print('car_care_backup=SKIP_UNCHANGED remote=not_contacted' if record['status'] == 'skipped_unchanged'
                  else 'car_care_backup=PASS remote_restore=verified')
        return 0
    except Exception:
        print('car_care_backup=FAIL')
        return 1
    finally:
        os.umask(previous_umask)


if __name__ == '__main__':
    raise SystemExit(main())
