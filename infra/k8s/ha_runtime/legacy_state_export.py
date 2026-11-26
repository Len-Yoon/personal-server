"""Stage a reviewed subset of frozen HA shared state in legacy formats.

The caller supplies an exact JSON projection from an independently verified
restored backup. This module does not open a database connection, prove that
restoration, install any files, or authorize writers. The final receipt covers
only the listed shared-state keys.
"""
import os
from pathlib import Path
import sqlite3
import stat

from .seven_capture import _module, _write, encoded, parse, rate_limit, sha, trusted, utc
from .seven_migration import HEX, need, private_file, read_verified, validate_state


SCOPE = 'legacy-shared-state-subset-v1'
ARCHIVE_VERSION = '2026-08-20-market-focus-v3'
SERVICE_KEYS = {
    'book-memo': {'auth-rate-limit', 'write-sessions'},
    'youtube-memo': {'auth-rate-limit', 'write-sessions'},
    'crawler-worker': {'archive', 'collection_status'},
    'homeops-executor': {'restart_claims'},
}
STATUS_KEYS = {'initialized', 'first_attempt_at', 'last_attempt_at', 'last_success_at',
               'last_failure_at', 'failures_total', 'consecutive_failures'}
COUNTERS = {'failures_total', 'consecutive_failures'}
RESTORE_SQL = """
CREATE TABLE restart_claims (
    request_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, created_at REAL NOT NULL,
    state TEXT NOT NULL, result TEXT
)
"""


def _load_projection(source, snapshot_sha256, backup_sha256):
    source = private_file(source)
    trusted(source.parent, directory=True)
    raw = read_verified(source, snapshot_sha256, source.lstat().st_size)
    value = parse(raw)
    need(type(backup_sha256) is str and HEX.fullmatch(backup_sha256)
         and type(value) is dict and set(value) == {'schema_version', 'scope', 'backup_sha256', 'services'}
         and type(value['schema_version']) is int and value['schema_version'] == 1
         and value['scope'] == SCOPE and value['backup_sha256'] == backup_sha256,
         'legacy_state_snapshot_binding_invalid')
    services = value['services']
    need(type(services) is dict and set(services) == set(SERVICE_KEYS), 'legacy_state_service_scope_invalid')
    for service, keys in SERVICE_KEYS.items():
        need(type(services[service]) is dict and set(services[service]) == keys,
             'legacy_state_key_scope_invalid')
        for key, payload in services[service].items():
            validate_state(service, key, payload)
    return raw, services


def _crawler_outputs(states):
    archive = states['archive']
    storage = _module('crawler-worker/app/services/news_archive_storage.py')
    notices = _module('crawler-worker/app/services/news_archive_notifications.py')
    need(archive.get('schema_version', ARCHIVE_VERSION) == ARCHIVE_VERSION,
         'legacy_archive_version_unsupported')
    try:
        storage.validate_archive_payload(archive, ARCHIVE_VERSION)
    except ValueError:
        need(False, 'legacy_archive_shape_invalid')
    outbox = archive.get('telegram_outbox', [])
    pending = archive.get('telegram_pending_articles', [])
    need(type(outbox) is list and not pending and all(
         type(event) is dict and event.get('status') == 'sent' for event in outbox),
         'legacy_crawler_effects_unsettled')
    # The pre-HA reader discards uncertain deliveries and malformed entries.
    # Reject them instead of creating an output whose legacy reader loses them.
    need(outbox == notices.notification_outbox(outbox), 'legacy_archive_outbox_shape_invalid')
    need(type(archive.get('telegram_notifications_initialized', False)) is bool
         and all(type(archive.get(key, '')) is str for key in ('updated_at', 'telegram_last_digest_at'))
         and all(type(value) is str for value in archive.get('telegram_topic_last_sent_at', {}).values()),
         'legacy_archive_shape_invalid')
    status = states['collection_status']
    need(set(status) == STATUS_KEYS and type(status['initialized']) is bool
         and all(type(status[key]) is int and status[key] >= 0 for key in COUNTERS)
         and status['consecutive_failures'] <= status['failures_total'],
         'legacy_collection_status_invalid')
    for field in STATUS_KEYS - COUNTERS - {'initialized'}:
        if status[field] is not None:
            utc(status[field])
    return {'news_archive.json': encoded(archive), 'news_collection_status.json': encoded(status)}


def _homeops_rows(state):
    rows = []
    for request_id, item in sorted(state.items()):
        need(set(item) == {'payload_hash', 'created_at', 'state', 'result'}
             and bool(request_id.strip()) and len(request_id) <= 128
             and 0 <= item['created_at'] <= 253402300799, 'legacy_restart_claim_invalid')
        need(item['state'] in {'completed', 'expired'}, 'legacy_restart_claim_unsettled')
        result = item['result']
        need((item['state'] == 'completed' and type(result) is dict)
             or (item['state'] == 'expired' and result is None), 'legacy_restart_claim_result_invalid')
        rows.append((request_id, item['payload_hash'], item['created_at'], item['state'],
                     encoded(result).decode('utf-8') if result is not None else None))
    return rows


def _homeops_database(path, rows):
    # Reserve a new private inode before sqlite opens it. No SQLite sidecars
    # or original files are imported, and the SQL schema is fixed.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    connection = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True)
    try:
        connection.execute('PRAGMA synchronous=FULL')
        with connection:
            connection.execute(RESTORE_SQL)
            connection.executemany('INSERT INTO restart_claims VALUES (?, ?, ?, ?, ?)', rows)
        need(connection.execute('PRAGMA quick_check').fetchall() == [('ok',)]
             and connection.execute(
                 'SELECT request_id,payload_hash,created_at,state,result FROM restart_claims ORDER BY request_id'
             ).fetchall() == rows, 'legacy_restart_claim_round_trip_failed')
    finally:
        connection.close()
    need(not any(os.path.lexists(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal')),
         'legacy_restart_claim_sidecar_rejected')
    raw = path.read_bytes()
    return {'file': path.name, 'sha256': sha(raw), 'bytes': len(raw)}


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def stage_legacy_states(source, *, snapshot_sha256, backup_sha256, output, allow_memo_relogin=False):
    """Create a private, new stage and publish its hash receipt last.

    Memo write sessions are SHA-256 digests in HA and raw opaque token keys in
    legacy RAM. They are never reconstructed or exported to the legacy files.
    """
    need(type(allow_memo_relogin) is bool, 'legacy_memo_relogin_disposition_invalid')
    raw, states = _load_projection(source, snapshot_sha256, backup_sha256)
    crawler = _crawler_outputs(states['crawler-worker'])
    claims = _homeops_rows(states['homeops-executor']['restart_claims'])
    files = {'crawler-worker': crawler}
    relogins = {}
    for service in ('book-memo', 'youtube-memo'):
        sessions = states[service]['write-sessions']
        need(not sessions or allow_memo_relogin, 'legacy_memo_relogin_disposition_required')
        relogins[service] = len(sessions)
        failures = rate_limit(states[service]['auth-rate-limit'], portal=False)
        files[service] = {'auth-rate-limit-state.json': encoded(failures)}

    output = Path(output)
    need(output.is_absolute() and '..' not in output.parts, 'legacy_state_private_stage_required')
    trusted(output.parent, directory=True)
    for parent in (output.parent, *output.parent.parents):
        info = parent.lstat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid in {0, os.geteuid()}
             and (not info.st_mode & 0o022 or (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)),
             'legacy_state_private_stage_required')
    need(not os.path.lexists(output), 'legacy_state_stage_exists')
    os.mkdir(output, 0o700)
    artifacts = {}
    for service, outputs in files.items():
        directory = output / service
        directory.mkdir(mode=0o700)
        artifacts[service] = [_write(directory / name, data) for name, data in sorted(outputs.items())]
        _sync_directory(directory)
    directory = output / 'homeops-executor'
    directory.mkdir(mode=0o700)
    artifacts['homeops-executor'] = [_homeops_database(directory / 'restarts.sqlite3', claims)]
    _sync_directory(directory)
    need(read_verified(source, snapshot_sha256, len(raw)) == raw, 'legacy_state_snapshot_changed')
    receipt = {'schema_version': 1, 'scope': SCOPE, 'backup_sha256': backup_sha256,
               'snapshot_sha256': snapshot_sha256, 'artifacts': artifacts,
               'memo_sessions_requiring_relogin': relogins, 'homeops_claims': len(claims)}
    receipt_artifact = _write(output / 'legacy-state-receipt.json', encoded(receipt))
    _sync_directory(output)
    _sync_directory(output.parent)
    return {'receipt_sha256': receipt_artifact['sha256'], 'staged_services': sorted(artifacts),
            'homeops_claims': len(claims), 'memo_sessions_requiring_relogin': relogins}
