"""Restore Portal files from an isolated PostgreSQL backup into a private tree."""

import hashlib
import math
import os
from pathlib import Path, PurePosixPath
import sqlite3
from contextlib import closing

from . import blobs
from .legacy_sqlite_export import _private_directory
from .seven_capture import _write, auth_sessions, encoded, inspect_tree, need, parse, rate_limit, utc
from .seven_migration import _portal_validator, validate_state

ERROR = 'legacy_portal_export_unconfirmed'
LEGACY_TRASH_METADATA_CHARS = 8193


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def export_portal_files(snapshot, read_blob, destination):
    """Materialize active files and legacy trash without changing source state.

    The caller supplies the namespace and blob reader from one verified offline
    snapshot. A destination is ready only after its receipt has been written.
    """
    destination = Path(destination)
    _private_directory(destination.parent)
    need(destination.is_absolute() and '..' not in destination.parts and callable(read_blob), ERROR)
    namespace_raw = encoded(snapshot)
    snapshot = parse(namespace_raw)
    validator = _portal_validator(source_only_preflight=True)
    validator._validate(snapshot)
    need(set(snapshot) == {'format', 'version', 'entries', 'trash', 'confirmations', 'audit'}
         and type(snapshot['audit']) is list, ERROR)

    entries = {}
    metadata = {}

    def add_group(group, original=None, target=None):
        for path, item in group.items():
            original_path = PurePosixPath(path)
            relative = original_path if original is None else target / original_path.relative_to(original)
            parent = original_path.parent
            need(relative not in entries and
                 (original_path == original or str(parent) == '.'
                  or group.get(parent.as_posix(), {}).get('type') == 'folder'), ERROR)
            modified = item.get('modified_at')
            need(type(modified) in (int, float) and math.isfinite(modified) and 0 <= modified <= 2**32, ERROR)
            entries[relative] = item

    add_group(snapshot['entries'])
    for token, record in snapshot['trash'].items():
        need(set(record) == {'path', 'trashed_at', 'entries'}, ERROR)
        original = PurePosixPath(validator._path(record['path']))
        utc(record['trashed_at'])
        group = record['entries']
        need(original.as_posix() in group and
             all(PurePosixPath(path).is_relative_to(original) for path in group), ERROR)
        target = PurePosixPath('.trash') / token
        raw = encoded({'path': record['path'], 'trashed_at': record['trashed_at']})
        need(len(raw.decode('utf-8')) <= LEGACY_TRASH_METADATA_CHARS, ERROR)
        metadata[target / 'metadata.json'] = raw
        add_group(group, original, target / 'item')

    files = {path: item for path, item in entries.items() if item['type'] == 'file'}
    need(all(item['size'] <= blobs.MAX_BLOB_BYTES for item in files.values()) and
         sum(item['size'] for item in files.values()) <= blobs.MAX_NAMESPACE_BYTES, ERROR)
    destination.mkdir(mode=0o700)
    tree = destination / 'files'
    tree.mkdir(mode=0o700)
    if metadata:
        (tree / '.trash').mkdir(mode=0o700)
    for path, raw in metadata.items():
        output = tree / path
        output.parent.mkdir(mode=0o700)
        _write(output, raw)
    folders = [path for path in entries if path not in files]
    for path in sorted(folders, key=lambda value: len(value.parts)):
        (tree / path).mkdir(mode=0o700)
    for path, item in files.items():
        raw = read_blob(item['sha256'], item['size'])
        need(type(raw) is bytes and len(raw) == item['size'] and
             hashlib.sha256(raw).hexdigest() == item['sha256'], ERROR)
        _write(tree / path, raw)
    for path, item in sorted(entries.items(), key=lambda row: -len(row[0].parts)):
        timestamp = item['modified_at']
        os.utime(tree / path, (timestamp, timestamp), follow_symlinks=False)
        if path in files:
            descriptor = os.open(tree / path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                need(abs(os.fstat(descriptor).st_mtime - timestamp) < 1e-5, ERROR)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    projected = {path.as_posix(): {'type': 'folder'} for path in folders}
    projected.update({path.as_posix(): {'type': 'file', 'sha256': item['sha256'], 'bytes': item['size']}
                      for path, item in files.items()})
    for path, raw in metadata.items():
        projected['.trash'] = {'type': 'folder'}
        projected[path.parent.as_posix()] = {'type': 'folder'}
        projected[path.as_posix()] = {'type': 'file', 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
    manifest = [{'path': path, **projected[path]} for path in sorted(projected, key=lambda name: PurePosixPath(name).parts)]
    observed = inspect_tree(tree)
    need(observed == hashlib.sha256(encoded(manifest)).hexdigest(), ERROR)
    for row in reversed(manifest):
        if row['type'] == 'folder':
            _sync_directory(tree / row['path'])
    _sync_directory(tree)
    namespace = _write(destination / 'file-namespace.json', namespace_raw)
    receipt = {'schema_version': 1, 'files': len(files), 'trash_entries': len(metadata),
               'tree_sha256': observed, 'namespace_sha256': namespace['sha256']}
    _sync_directory(destination)
    _sync_directory(destination.parent)
    _write(destination / 'portal-export-receipt.json', encoded(receipt))
    _sync_directory(destination)
    return receipt


def export_portal_state(snapshot, destination):
    """Stage the legacy Portal login state and portfolio source from one backup."""
    destination = Path(destination)
    _private_directory(destination.parent)
    need(destination.is_absolute() and '..' not in destination.parts, ERROR)
    snapshot = parse(encoded(snapshot))
    need(set(snapshot) == {'auth_sessions', 'auth_failures', 'portfolio'}, ERROR)
    for key, payload in snapshot.items():
        validate_state('portal-web', key, payload)
    sessions = snapshot['auth_sessions']
    need(set(sessions) == {'sessions', 'confirmations'} and
         all(set(row) == {'scope', 'payload_hash', 'expires_at'}
             and math.isfinite(row['expires_at']) for group in sessions.values() for row in group.values()), ERROR)
    failures = rate_limit(snapshot['auth_failures'], portal=True)
    need(set(snapshot['portfolio']) == {'content'}, ERROR)
    portfolio = snapshot['portfolio']['content'].encode('utf-8')
    failure_bytes = encoded(failures)
    destination.mkdir(mode=0o700)
    database = destination / 'auth-sessions.sqlite3'
    descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(database)) as connection, connection:
        for table, group in sessions.items():
            # Both identifiers are checked against the legacy schema above.
            connection.execute(f'CREATE TABLE {table} (token_hash TEXT PRIMARY KEY, scope TEXT NOT NULL, '
                               'payload_hash TEXT NOT NULL, expires_at REAL NOT NULL)')
            connection.executemany(f'INSERT INTO {table} VALUES (?,?,?,?)',
                [(token, row['scope'], row['payload_hash'], row['expires_at']) for token, row in group.items()])
        need(connection.execute('PRAGMA quick_check').fetchall() == [('ok',)], ERROR)
    need(auth_sessions(database) == sessions, ERROR)
    failures_artifact = _write(destination / 'auth-failures.json', failure_bytes)
    portfolio_artifact = _write(destination / 'portfolio.md', portfolio)
    receipt = {'schema_version': 1, 'sessions': len(sessions['sessions']), 'confirmations': len(sessions['confirmations']),
               'auth_sha256': hashlib.sha256(encoded(sessions)).hexdigest(),
               'failures_sha256': failures_artifact['sha256'], 'portfolio_sha256': portfolio_artifact['sha256']}
    _sync_directory(destination)
    _sync_directory(destination.parent)
    _write(destination / 'portal-state-export-receipt.json', encoded(receipt))
    _sync_directory(destination)
    return receipt
