"""Local-only car backup identity, journal, lock, and tiered retention policy."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile

SCOPE = 'car-care-db-only'
ID = re.compile(r'car-care-(\d{8}T\d{6}Z)-[a-f0-9]{16}')
SHA = re.compile(r'[a-f0-9]{64}')
# Modern backup timestamps use Korea's fixed UTC+09; minimal images need no tzdata.
KST = timezone(timedelta(hours=9), 'Asia/Seoul')


def require(value):
    if not value:
        raise ValueError('backup policy validation failed')


def created(backup_id):
    require(isinstance(backup_id, str) and ID.fullmatch(backup_id))
    return datetime.strptime(ID.fullmatch(backup_id)[1], '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)


def timestamp(value):
    require(isinstance(value, str))
    result = datetime.fromisoformat(value)
    require(result.tzinfo is not None)
    return result.astimezone(timezone.utc)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def fingerprint(path):
    """Hash schema and typed row multisets; page layout/WAL counters are irrelevant."""
    result = hashlib.sha256()
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        schema = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
        result.update(canonical(schema))
        for pragma in ('user_version', 'application_id'):
            result.update(canonical([pragma, db.execute('PRAGMA '+pragma).fetchone()[0]]))
        for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            rows = []
            quoted = '"' + name.replace('"', '""') + '"'
            for row in db.execute('SELECT * FROM ' + quoted):
                typed = [(type(v).__name__, v.hex() if isinstance(v, bytes) else repr(v)) for v in row]
                rows.append(hashlib.sha256(canonical(typed)).digest())
            result.update(canonical([name, len(rows)]))
            for row_hash in sorted(rows):
                result.update(row_hash)
    return result.hexdigest()


@contextmanager
def execution_lock(evidence):
    descriptor = os.open(evidence / '.car-care-backup.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def persist(path, record, *, initial=False):
    """Durable intent before remote mutation; atomic final evidence after it."""
    if initial:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(canonical(record) + b'\n')
            stream.flush()
            os.fsync(stream.fileno())
    else:
        descriptor, temporary = tempfile.mkstemp(prefix='.journal-', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(canonical(record) + b'\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_records(evidence):
    records = []
    for path in sorted(evidence.glob('car-care-*.json')):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                    and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= 1024 * 1024)
            record = json.load(stream)
        require(record.get('scope') == SCOPE and path.name == record.get('backup_id', '') + '.json')
        created(record['backup_id'])
        records.append(record)
    return records


def require_resolved(records):
    require(not any(r.get('remote_upload_status') == 'uncertain' or
                    r.get('remote_delete_status') == 'uncertain' for r in records))


def successful(records, now=None):
    now = now or datetime.now(timezone.utc)
    require(now.tzinfo is not None)
    removed = set()
    for record in records:
        deleted = record.get('deleted', [])
        require(isinstance(deleted, list) and len(deleted) <= 10000 and
                (not deleted or record.get('operation') == 'retention'))
        require(all(isinstance(name, str) and name.endswith('.sqlite3.age') and
                    ID.fullmatch(name[:-12]) for name in deleted))
        require(len(set(deleted)) == len(deleted))
        removed.update(deleted)
    result = []
    for record in records:
        require(record.get('scope') == SCOPE)
        date = created(record.get('backup_id'))
        require(date <= now)
        if record.get('status') != 'success' or record.get('operation', 'backup') != 'backup':
            continue
        require(record.get('restore_verified') is True and record.get('remote_upload_status') == 'confirmed')
        require(all(isinstance(record.get(key), str) and SHA.fullmatch(record[key])
                    for key in ('artifact_sha256', 'snapshot_sha256')))
        verified = timestamp(record.get('restore_verified_at', record.get('finished_at')))
        require(date <= verified <= now)
        if record['backup_id'] + '.sqlite3.age' not in removed:
            result.append(record)
    return sorted(result, key=lambda r: (created(r['backup_id']), r['backup_id']), reverse=True)


def retention_plan(records, *, now=None):
    now = now or datetime.now(timezone.utc)
    proofs = successful(records, now)
    require(proofs)
    today = now.astimezone(KST).date()
    # Keep at least the newest seven, including every tie at their boundary.
    floor = created(proofs[min(6, len(proofs)-1)]['backup_id'])
    kept_seconds = {created(r['backup_id']) for r in proofs if created(r['backup_id']) >= floor}
    buckets = set()
    for record in proofs:
        second = created(record['backup_id'])
        day = second.astimezone(KST).date()
        age = (today-day).days
        month_age = (today.year-day.year)*12 + today.month-day.month
        bucket = (('daily', age) if 0 <= age < 7 else
                  ('weekly', (age-7)//7) if 7 <= age < 35 else
                  ('monthly', month_age) if age >= 35 and 1 <= month_age <= 3 else None)
        if bucket is not None and bucket not in buckets:
            buckets.add(bucket)
            kept_seconds.add(second)
    keep = sorted(r['backup_id']+'.sqlite3.age' for r in proofs if created(r['backup_id']) in kept_seconds)
    delete = sorted(r['backup_id']+'.sqlite3.age' for r in proofs if created(r['backup_id']) not in kept_seconds)
    require(keep and not set(keep) & set(delete))
    body = dict(scope=SCOPE, policy=dict(daily=7, weekly=4, monthly=3), keep=keep, delete=delete,
                protected=proofs[0]['backup_id']+'.sqlite3.age',
                digests={r['backup_id']+'.sqlite3.age': r['artifact_sha256'] for r in proofs})
    return dict(body, plan_sha256=hashlib.sha256(canonical(body)).hexdigest())
