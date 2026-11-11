"""Local, offline capture of explicitly frozen seven-app sources.

Run ``python -m ha_runtime.seven_capture --plan PRIVATE_JSON --plan-sha256 SHA
--output NEW_PRIVATE_DIRECTORY``. This does not stop/inspect live writers, connect
remotely, read process memory, or activate a target. A trusted operator supplies
hash-bound external runtime, graph, closure and disposition receipts. Their
claims remain external claims; a successful capture is not live fencing proof.
Source-only preflight does not install release metadata or relax the packaged
release requirement of the later database apply operation.

Plan v1 fields: schema_version, source_commit, cluster, plan_sha256,
target_system_id, evidence and services. Evidence has runtime/writer_graph/
writer_closure/dispositions descriptors {path,sha256,bytes}. Each exact service
has databases and states. Every DATABASES component must be explicit: a SQLite
descriptor adds sidecars (exact present -wal/-shm/-journal descriptors), or an
optional DB has {path,absence} referring to dispositions.absences. Portal adds
files {path,tree_sha256}; Car adds private_tokens (JSON descriptor). Portal
states are auth_sessions (SQLite descriptor), auth_failures (JSON), portfolio
(UTF-8 text); file-namespace is generated. Book/YouTube states contain only
auth-rate-limit (JSON); write-sessions are explicitly invalidated by disposition.
Crawler states are archive/collection_status (JSON). HomeOps states are empty:
restart_claims are derived from its explicit main DB or proven local absence.
System Agent has empty databases/states and an explicit stateless disposition.

writer_graph v1 binds source_runtime_sha256 and exact services -> writer ID lists.
writer_closure v1 binds that graph SHA, runtime SHA, cluster/plan/target, exact
services, writers_closed_at and observations [{writer_id,state:'closed',
observed_at,evidence_sha256}]. dispositions v1 binds exact services and runtime
SHA, session_dispositions (Book/YouTube -> invalidate_and_require_relogin),
stateless_services and absences ({service.component:{source_path_sha256,
checked_at,inventory_sha256}}). No default empties stand in for missing evidence.
Source paths and payloads never enter the public CLI result.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time
import uuid

from . import blobs
from .config import SERVICES
from .seven_migration import (DATABASES, REQUIRED_DATABASES, FrozenSources,
                              MigrationRejected, _provider_secret, validate_state)

HEX = re.compile(r'[0-9a-f]{64}\Z')
MAX_INPUT_BYTES = 2 * 1024 * 1024 * 1024
MAX_JSON_BYTES = 32 * 1024 * 1024
EVIDENCE = {'runtime', 'writer_graph', 'writer_closure', 'dispositions'}
SIDECARS = {'-wal', '-shm', '-journal'}


def need(ok, reason):
    if not ok:
        raise MigrationRejected(reason)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def parse(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            need(key not in result, 'duplicate_source_json_key')
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique)
        encoded(value)
        need(b'\\u0000' not in encoded(value), 'invalid_source_json')
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise MigrationRejected('invalid_source_json') from None


def utc(value):
    need(isinstance(value, str), 'invalid_evidence_timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise MigrationRejected('invalid_evidence_timestamp') from None
    need(parsed.tzinfo is not None, 'invalid_evidence_timestamp')
    return parsed.astimezone(timezone.utc)


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_uid, info.st_nlink)


def trusted(path, *, directory=False):
    path = Path(path)
    need(path.is_absolute() and '..' not in path.parts, 'trusted_private_source_required')
    for parent in (path, *path.parents):
        need(not stat.S_ISLNK(parent.lstat().st_mode), 'source_symlink_rejected')
    info = path.lstat()
    need((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
         and info.st_uid in {0, os.geteuid()} and not info.st_mode & 0o077
         and (directory or info.st_nlink == 1), 'trusted_private_source_required')
    if not directory:
        trusted(path.parent, directory=True)
    return path


class Reader:
    """Hash + inode metadata verification before and after each read and capture."""
    def __init__(self):
        self.stamps = {}
        self.parents = {}
        self.missing = set()

    def watch(self, path, *, directory=False):
        path = trusted(path, directory=directory)
        current = stamp(path.lstat())
        need(self.stamps.get(path, current) == current, 'frozen_source_changed')
        self.stamps[path] = current
        if not directory:
            parent = path.parent
            info = parent.lstat()
            current_parent = (info.st_dev, info.st_ino, info.st_mode, info.st_uid)
            need(self.parents.get(parent, current_parent) == current_parent, 'frozen_source_changed')
            self.parents[parent] = current_parent
        return path

    def read(self, descriptor, *, limit=MAX_JSON_BYTES, sink=None):
        need(type(descriptor) is dict and set(descriptor) == {'path', 'sha256', 'bytes'},
             'source_descriptor_invalid')
        path = self.watch(descriptor['path'])
        size = descriptor['bytes']
        need(type(size) is int and 0 <= size <= limit and path.stat().st_size == size
             and isinstance(descriptor['sha256'], str) and HEX.fullmatch(descriptor['sha256']),
             'source_binding_invalid')
        before = self.stamps[path]
        digest = hashlib.sha256()
        payload = bytearray()
        consumed = 0
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
            need(stamp(os.fstat(stream.fileno())) == before, 'frozen_source_changed')
            for chunk in iter(lambda: stream.read(65536), b''):
                consumed += len(chunk)
                need(consumed <= size, 'frozen_source_changed')
                digest.update(chunk)
                if sink is None:
                    payload.extend(chunk)
                else:
                    sink.write(chunk)
            need(stamp(os.fstat(stream.fileno())) == before, 'frozen_source_changed')
        need(stamp(path.lstat()) == before and digest.hexdigest() == descriptor['sha256'],
             'frozen_source_changed')
        return bytes(payload)

    def unchanged(self):
        for path, value in self.parents.items():
            info = path.lstat()
            need((info.st_dev, info.st_ino, info.st_mode, info.st_uid) == value, 'frozen_source_changed')
        need(all(stamp(path.lstat()) == value for path, value in self.stamps.items()),
             'frozen_source_changed')
        need(all(not os.path.lexists(path) for path in self.missing), 'proven_absence_changed')


def descriptor(path):
    """Operator helper for an already private frozen file; does not log its name."""
    reader = Reader()
    path = reader.watch(path)
    need(path.stat().st_size <= MAX_INPUT_BYTES, 'source_size_limit')
    hashed = hashlib.sha256()
    before = stamp(path.lstat())
    consumed = 0
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        need(stamp(os.fstat(stream.fileno())) == before, 'frozen_source_changed')
        for chunk in iter(lambda: stream.read(65536), b''):
            consumed += len(chunk)
            need(consumed <= before[2], 'frozen_source_changed')
            hashed.update(chunk)
        need(stamp(os.fstat(stream.fileno())) == before, 'frozen_source_changed')
    reader.unchanged()
    return {'path': str(path), 'sha256': hashed.hexdigest(), 'bytes': path.stat().st_size}


def _write(path, raw):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return {'file': path.name, 'sha256': sha(raw), 'bytes': len(raw)}


def _module(relative):
    path = Path(__file__).resolve().parents[3] / relative
    need(path.is_file() and not path.is_symlink(), 'canonical_converter_source_required')
    spec = importlib.util.spec_from_file_location('_seven_capture_' + sha(relative.encode())[:12], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sqlite_backup(reader, entry, output):
    """Stage exact DB+WAL bytes, then use SQLite backup; never open the original DB."""
    need(type(entry) is dict and set(entry) == {'path', 'sha256', 'bytes', 'sidecars'}
         and type(entry['sidecars']) is dict and set(entry['sidecars']) <= SIDECARS,
         'sqlite_sidecar_inventory_required')
    main = {key: entry[key] for key in ('path', 'sha256', 'bytes')}
    original = reader.watch(main['path'])
    for suffix in SIDECARS:
        path = Path(str(original) + suffix)
        need(os.path.lexists(path) == (suffix in entry['sidecars']), 'sqlite_sidecar_inventory_mismatch')
        if suffix not in entry['sidecars']:
            reader.missing.add(path)
        else:
            need(entry['sidecars'][suffix].get('path') == str(path), 'sqlite_sidecar_path_mismatch')
    # Snapshotting a hot rollback journal cannot establish a committed freeze.
    need('-journal' not in entry['sidecars'], 'hot_rollback_journal_rejected')
    with tempfile.TemporaryDirectory(prefix='sqlite-stage-', dir=output.parent) as directory:
        stage = Path(directory) / 'source.sqlite3'
        with stage.open('xb') as sink:
            stage.chmod(0o600)
            reader.read(main, limit=MAX_INPUT_BYTES, sink=sink)
        for suffix, item in entry['sidecars'].items():
            if suffix == '-shm':
                reader.read(item, limit=MAX_INPUT_BYTES, sink=_NullSink())
            else:
                target = Path(str(stage) + suffix)
                with target.open('xb') as sink:
                    target.chmod(0o600)
                    reader.read(item, limit=MAX_INPUT_BYTES, sink=sink)
        _write(output, b'')  # exclusive destination before SQLite opens it
        deadline = time.monotonic() + 30
        def progress(status, remaining, total):
            need(time.monotonic() <= deadline, 'sqlite_backup_deadline')
        try:
            with sqlite3.connect(stage.as_uri() + '?mode=ro', uri=True, timeout=1) as source:
                source.execute('PRAGMA trusted_schema=OFF')
                need(source.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'source_integrity_failed')
                with sqlite3.connect(output) as target:
                    source.backup(target, pages=256, progress=progress, sleep=0.01)
                    target.execute('PRAGMA journal_mode=DELETE')
                    need(target.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
                         and not target.execute('PRAGMA foreign_key_check').fetchall(), 'source_integrity_failed')
        except sqlite3.Error:
            raise MigrationRejected('sqlite_snapshot_invalid') from None
    output.chmod(0o600)
    need(not any(os.path.lexists(str(output) + suffix) for suffix in SIDECARS), 'sqlite_snapshot_sidecar_rejected')
    reader.unchanged()
    with output.open('rb') as stream:
        os.fsync(stream.fileno())
    result = descriptor(output)
    return {'file': output.name, 'sha256': result['sha256'], 'bytes': result['bytes']}


class _NullSink:
    def write(self, chunk):
        pass


def rate_limit(payload, *, portal):
    need(type(payload) is dict, 'unsupported_rate_limit_shape')
    groups = payload.values() if portal else [payload]
    for group in groups:
        need(type(group) is dict, 'unsupported_rate_limit_shape')
        for identifier, values in group.items():
            need(isinstance(identifier, str) and type(values) is list, 'unsupported_rate_limit_shape')
            for value in values:
                # Both legacy readers accept naive strings; preserve their original zone semantics.
                need(isinstance(value, str), 'unsupported_rate_limit_shape')
                try:
                    datetime.fromisoformat(value)
                except ValueError:
                    raise MigrationRejected('unsupported_rate_limit_shape') from None
    return payload


def auth_sessions(path):
    result = {}
    with sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True) as db:
        need({row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
             == {'sessions', 'confirmations'}, 'unsupported_portal_auth_schema')
        for table in ('sessions', 'confirmations'):
            need([row[1] for row in db.execute('PRAGMA table_info(' + table + ')')]
                 == ['token_hash', 'scope', 'payload_hash', 'expires_at'], 'unsupported_portal_auth_schema')
            rows = db.execute('SELECT * FROM ' + table).fetchall()
            result[table] = {key: {'scope': scope, 'payload_hash': value, 'expires_at': expiry}
                             for key, scope, value, expiry in rows}
            need(len(result[table]) == len(rows), 'unsupported_portal_auth_schema')
    validate_state('portal-web', 'auth_sessions', result)
    return result


def restart_claims(path):
    with sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True) as db:
        need({row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
             == {'restart_claims'}, 'unsupported_homeops_schema')
        need([row[1] for row in db.execute('PRAGMA table_info(restart_claims)')]
             == ['request_id', 'payload_hash', 'created_at', 'state', 'result'], 'unsupported_homeops_schema')
        rows = db.execute('SELECT * FROM restart_claims').fetchall()
        result = {key: {'payload_hash': value, 'created_at': created, 'state': state,
                        'result': parse(raw.encode()) if raw is not None else None}
                  for key, value, created, state, raw in rows}
        need(len(result) == len(rows), 'unsupported_homeops_schema')
    validate_state('homeops-executor', 'restart_claims', result)
    return result


def _tree(reader, root):
    root = reader.watch(root, directory=True)
    records = []
    def walk(directory):
        reader.watch(directory, directory=True)
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            need(not stat.S_ISLNK(info.st_mode), 'source_symlink_rejected')
            if stat.S_ISDIR(info.st_mode):
                records.append({'path': relative, 'type': 'folder'})
                walk(path)
            else:
                item = descriptor(path)
                reader.watch(path)
                need(item['bytes'] <= blobs.MAX_BLOB_BYTES, 'portal_blob_size_limit')
                records.append({'path': relative, 'type': 'file', 'sha256': item['sha256'], 'bytes': item['bytes']})
            need(len(records) <= 30000, 'portal_tree_limit')
    walk(root)
    reader.unchanged()
    return records


def inspect_tree(path):
    """Return only a content inventory digest for a private frozen attachment tree."""
    return sha(encoded(_tree(Reader(), path)))


def portal_files(reader, entry, output, artifacts, objects):
    need(type(entry) is dict and set(entry) == {'path', 'tree_sha256'}, 'portal_tree_binding_required')
    root = reader.watch(entry['path'], directory=True)
    need(not output.is_relative_to(root) and not root.is_relative_to(output), 'capture_source_output_overlap')
    records = _tree(reader, root)
    need(HEX.fullmatch(entry['tree_sha256']) and sha(encoded(records)) == entry['tree_sha256'], 'portal_tree_hash_mismatch')
    validator = _module('portal-web/app/services/ha_files.py')
    state = validator._default()
    record_map = {row['path']: row for row in records}
    skip = set()
    for relative, row in record_map.items():
        if relative == '.trash':
            need(row['type'] == 'folder', 'unsupported_portal_trash')
            skip.add(relative)
        elif relative.startswith('.trash/'):
            skip.add(relative)
    tokens = {name.split('/')[1] for name in skip if '/' in name}
    for token in sorted(tokens):
        need(re.fullmatch(r'[0-9a-f]{32}', token), 'unsupported_portal_trash')
        base = '.trash/' + token
        need(record_map.get(base, {}).get('type') == 'folder'
             and set(name for name in record_map if name.startswith(base + '/') and '/' not in name[len(base) + 1:])
             == {base + '/metadata.json', base + '/item'}, 'unsupported_portal_trash')
        metadata = record_map[base + '/metadata.json']
        need(metadata['type'] == 'file', 'unsupported_portal_trash')
        raw = reader.read({'path': str(root / metadata['path']), 'sha256': metadata['sha256'], 'bytes': metadata['bytes']})
        value = parse(raw)
        need(type(value) is dict and set(value) == {'path', 'trashed_at'}, 'unsupported_portal_trash')
        validator._path(value['path'])
        utc(value['trashed_at'])
        group = {}
        for name, item in record_map.items():
            source_prefix = base + '/item'
            if name == source_prefix or name.startswith(source_prefix + '/'):
                destination = value['path'] + name[len(source_prefix):]
                group[destination] = _portal_entry(reader, root, item, destination, validator, output, artifacts, objects)
        state['trash'][token] = dict(value, entries=group)
    for relative, item in record_map.items():
        if relative not in skip:
            state['entries'][relative] = _portal_entry(reader, root, item, relative, validator, output, artifacts, objects)
    validator._validate(state)
    need(sum(artifacts[identifier]['bytes'] for identifier in objects.values()) <= blobs.MAX_NAMESPACE_BYTES,
         'portal_blob_total_limit')
    reader.unchanged()
    return state, {'sha256': sha(encoded(records)), 'entries': len(records)}


def _portal_entry(reader, root, row, destination, validator, output, artifacts, objects):
    validator._path(destination)
    path = root / row['path']
    if row['type'] == 'folder':
        return {'type': 'folder', 'modified_at': path.stat().st_mtime}
    raw = reader.read({'path': str(path), 'sha256': row['sha256'], 'bytes': row['bytes']}, limit=blobs.MAX_BLOB_BYTES)
    identifier = row['sha256']
    if identifier not in objects:
        artifact = 'portal-blob-' + identifier
        artifacts[artifact] = _write(output / (artifact + '.blob'), raw)
        objects[identifier] = artifact
    return {'type': 'file', 'sha256': identifier, 'size': len(raw), 'content_type': '',
            'modified_at': path.stat().st_mtime}


def _evidence(reader, plan, started):
    need(type(plan['evidence']) is dict and set(plan['evidence']) == EVIDENCE, 'external_capture_evidence_required')
    raw = {key: reader.read(item) for key, item in plan['evidence'].items()}
    values = {key: parse(item) for key, item in raw.items()}
    need(all(type(value) is dict and not _provider_secret(value) for value in values.values()), 'unsafe_capture_evidence')
    runtime_hash, graph_hash = sha(raw['runtime']), sha(raw['writer_graph'])
    graph, closure, dispositions = (values[key] for key in ('writer_graph', 'writer_closure', 'dispositions'))
    need(graph.get('schema_version') == 1 and graph.get('source_runtime_sha256') == runtime_hash
         and type(graph.get('services')) is dict and set(graph['services']) == set(SERVICES), 'writer_graph_inventory_invalid')
    writers = []
    for rows in graph['services'].values():
        need(type(rows) is list and rows and all(isinstance(row, str) and re.fullmatch(r'[a-zA-Z0-9_.:-]{1,160}', row) for row in rows),
             'writer_graph_inventory_invalid')
        need(len(rows) == len(set(rows)), 'writer_graph_inventory_invalid')
        writers.extend(rows)
    writers = sorted(set(writers))
    need(closure.get('schema_version') == 1 and closure.get('source_runtime_sha256') == runtime_hash
         and closure.get('writer_graph_sha256') == graph_hash and closure.get('services') == sorted(SERVICES)
         and all(closure.get(key) == plan[key] for key in ('cluster', 'plan_sha256', 'target_system_id')),
         'external_writer_closure_binding_invalid')
    closed = utc(closure.get('writers_closed_at'))
    need(closed <= utc(started), 'external_writer_closure_order_invalid')
    rows = closure.get('observations')
    need(type(rows) is list and len(rows) == len(writers) and {row.get('writer_id') for row in rows if type(row) is dict} == set(writers),
         'external_writer_closure_incomplete')
    for row in rows:
        need(row.get('state') == 'closed' and isinstance(row.get('evidence_sha256'), str)
             and HEX.fullmatch(row['evidence_sha256']) and closed <= utc(row.get('observed_at')) <= utc(started),
             'external_writer_closure_incomplete')
    need(dispositions.get('schema_version') == 1 and dispositions.get('source_runtime_sha256') == runtime_hash
         and dispositions.get('services') == sorted(SERVICES)
         and dispositions.get('session_dispositions') == {service: 'invalidate_and_require_relogin' for service in ('book-memo', 'youtube-memo')}
         and dispositions.get('stateless_services') == ['system-agent'] and type(dispositions.get('absences')) is dict,
         'explicit_source_dispositions_required')
    return raw, closure, dispositions


def capture(plan_path, expected_sha256, output):
    """Return a hash-bound manifest receipt; this API never certifies live closure."""
    reader = Reader()
    plan_file = descriptor(plan_path)
    need(plan_file['sha256'] == expected_sha256, 'capture_plan_hash_mismatch')
    plan = parse(reader.read(plan_file))
    fields = {'schema_version', 'source_commit', 'cluster', 'plan_sha256', 'target_system_id', 'evidence', 'services'}
    need(type(plan) is dict and set(plan) == fields and type(plan['schema_version']) is int and plan['schema_version'] == 1,
         'capture_plan_fields_invalid')
    need(isinstance(plan['source_commit'], str) and re.fullmatch('[0-9a-f]{40}', plan['source_commit'])
         and isinstance(plan['cluster'], str) and re.fullmatch('[a-z][a-z0-9_]{0,62}', plan['cluster'])
         and isinstance(plan['plan_sha256'], str) and HEX.fullmatch(plan['plan_sha256'])
         and isinstance(plan['target_system_id'], str) and re.fullmatch('[1-9][0-9]{0,19}', plan['target_system_id']),
         'capture_identity_invalid')
    need(type(plan['services']) is dict and set(plan['services']) == set(SERVICES), 'all_seven_sources_required')
    started = now()
    evidence, closure, dispositions = _evidence(reader, plan, started)
    output = Path(output)
    need(output.is_absolute() and '..' not in output.parts, 'private_capture_destination_required')
    trusted(output.parent, directory=True)
    need(not os.path.lexists(output), 'capture_destination_exists')
    os.mkdir(output, 0o700)
    output_stamp = stamp(output.lstat())
    manifest = {key: plan[key] for key in ('source_commit', 'cluster', 'plan_sha256', 'target_system_id')}
    manifest.update(schema_version=1, services={}, artifacts={}, portal_objects={}, private_states={})
    artifacts = manifest['artifacts']
    provenance = {}
    repository = Path(__file__).resolve().parents[3]
    for relative in ('infra/k8s/ha_runtime/seven_capture.py', 'portal-web/app/services/ha_files.py',
                     'portal-web/app/services/session_store.py', 'portal-web/app/services/security.py',
                     'book-memo/app/main.py', 'youtube-memo/app/main.py',
                     'crawler-worker/app/services/news_archive_storage.py',
                     'crawler-worker/app/services/news_collection_status.py',
                     'homeops-executor/app/services/idempotency.py', 'car-care-worker/app/services/hyundai.py'):
        source = repository / relative
        before = source.lstat()
        need(stat.S_ISREG(before.st_mode) and before.st_uid in {0, os.geteuid()}
             and not before.st_mode & 0o022 and not source.is_symlink(), 'trusted_converter_source_required')
        reader.stamps[source] = stamp(before)
        with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
            need(stamp(os.fstat(stream.fileno())) == stamp(before), 'converter_source_changed')
            provenance[relative] = sha(stream.read())
        need(stamp(source.lstat()) == stamp(before), 'converter_source_changed')
    audit = {'schema_version': 1, 'capture_plan_sha256': expected_sha256,
             'converter_sha256': provenance['infra/k8s/ha_runtime/seven_capture.py'],
             'canonical_sources': provenance, 'sources': {}, 'evidence': {}, 'dispositions': dispositions}
    for key, raw in evidence.items():
        audit['evidence'][key] = _write(output / ('evidence-' + key + '.json'), raw)
    absences_used = set()
    def state(service, key, payload, source=None, converter='json-v1'):
        validate_state(service, key, payload)
        identifier = service + '.' + key
        artifacts[identifier] = _write(output / (identifier + '.json'), encoded(payload))
        manifest['services'][service]['states'][key] = identifier
        audit['sources'][identifier] = {'source': source, 'converter': converter,
                                       'normalized_sha256': artifacts[identifier]['sha256']}
    for service in sorted(SERVICES):
        spec = plan['services'][service]
        expected_fields = {'databases', 'states'} | ({'files'} if service == 'portal-web' else {'private_tokens'} if service == 'car-care-worker' else set())
        need(type(spec) is dict and set(spec) == expected_fields and type(spec['databases']) is dict
             and set(spec['databases']) == DATABASES[service] and type(spec['states']) is dict, 'service_capture_coverage_invalid')
        expected_states = {'portal-web': {'auth_sessions', 'auth_failures', 'portfolio'},
                           'book-memo': {'auth-rate-limit'}, 'youtube-memo': {'auth-rate-limit'},
                           'crawler-worker': {'archive', 'collection_status'}}.get(service, set())
        need(set(spec['states']) == expected_states, 'service_capture_coverage_invalid')
        manifest['services'][service] = {'databases': {}, 'states': {}}
        databases = {}
        for component, item in spec['databases'].items():
            identifier = service + '.' + component
            if type(item) is dict and set(item) == {'path', 'absence'}:
                need(component not in REQUIRED_DATABASES[service] and item['absence'] == identifier,
                     'required_database_absence_rejected')
                path = Path(item['path'])
                need(path.is_absolute() and '..' not in path.parts and not os.path.lexists(path), 'proven_absence_required')
                reader.watch(path.parent, directory=True)
                reader.missing.add(path)
                for suffix in SIDECARS:
                    sidecar = Path(str(path) + suffix)
                    need(not os.path.lexists(sidecar), 'proven_absence_required')
                    reader.missing.add(sidecar)
                absence = dispositions['absences'].get(identifier)
                need(type(absence) is dict and absence.get('source_path_sha256') == sha(str(path).encode())
                     and absence.get('inventory_sha256') == sha(evidence['runtime'])
                     and utc(closure['writers_closed_at']) <= utc(absence.get('checked_at')) <= utc(started),
                     'proven_absence_required')
                absences_used.add(identifier)
                audit['sources'][identifier] = {'absence': absence}
                continue
            entry = sqlite_backup(reader, item, output / (identifier + '.sqlite3'))
            artifacts[identifier] = entry
            manifest['services'][service]['databases'][component] = identifier
            databases[component] = output / entry['file']
            audit['sources'][identifier] = {'sha256': item['sha256'], 'bytes': item['bytes'],
                'sidecars': {key: {'sha256': value['sha256'], 'bytes': value['bytes']} for key, value in item['sidecars'].items()},
                'converter': 'sqlite-backup-v1', 'normalized_sha256': entry['sha256']}
        for key, item in spec['states'].items():
            source_meta = {field: item[field] for field in ('sha256', 'bytes')}
            if service == 'portal-web' and key == 'auth_sessions':
                with tempfile.TemporaryDirectory(prefix='auth-stage-', dir=output) as directory:
                    path = Path(directory) / 'auth.sqlite3'
                    sqlite_backup(reader, item, path)
                    payload = auth_sessions(path)
                state(service, key, payload, source_meta, 'portal-auth-sqlite-v1')
                audit['sources'][service + '.' + key]['sidecars'] = {
                    suffix: {'sha256': value['sha256'], 'bytes': value['bytes']} for suffix, value in item['sidecars'].items()}
                continue
            raw = reader.read(item)
            if key == 'portfolio':
                payload = {'content': raw.decode('utf-8')}
            else:
                payload = parse(raw)
            if key in {'auth_failures', 'auth-rate-limit'}:
                payload = rate_limit(payload, portal=service == 'portal-web')
            if service == 'crawler-worker':
                if key == 'archive':
                    _module('crawler-worker/app/services/news_archive_storage.py').validate_archive_payload(payload, '2026-08-20-market-focus-v3')
                    need(payload.get('schema_version') != '2026-07-15-korean-news-v2', 'legacy_archive_converter_unavailable')
                    need('articles' in payload, 'unsupported_archive_shape')
                else:
                    expected = {'initialized', 'first_attempt_at', 'last_attempt_at', 'last_success_at', 'last_failure_at', 'failures_total', 'consecutive_failures'}
                    need(type(payload) is dict and set(payload) == expected and type(payload['initialized']) is bool
                         and all(type(payload[k]) is int and payload[k] >= 0 for k in ('failures_total', 'consecutive_failures')),
                         'unsupported_collection_status_shape')
                    for field in expected - {'initialized', 'failures_total', 'consecutive_failures'}:
                        need(payload[field] is None or isinstance(payload[field], str), 'unsupported_collection_status_shape')
            state(service, key, payload, source_meta)
        if service == 'portal-web':
            payload, tree = portal_files(reader, spec['files'], output, artifacts, manifest['portal_objects'])
            state(service, 'file-namespace', payload, tree, 'portal-files-trash-v1')
        if service in {'book-memo', 'youtube-memo'}:
            state(service, 'write-sessions', {}, {'disposition_sha256': sha(evidence['dispositions'])}, 'invalidate-and-relogin-v1')
        if service == 'homeops-executor':
            payload = restart_claims(databases['main']) if 'main' in databases else {}
            state(service, 'restart_claims', payload, {'database': 'main'} if databases else {'absence': service + '.main'}, 'homeops-restart-sqlite-v1')
        if service == 'car-care-worker':
            raw = reader.read(spec['private_tokens'])
            payload = parse(raw)
            allowed = {'refresh_token', 'authorization_state', 'authorization_state_expires_at', 'refresh_uncertain'}
            need(type(payload) is dict and set(payload) <= allowed and isinstance(payload.get('refresh_token'), str)
                 and bool(payload['refresh_token'].strip()), 'unsupported_private_hyundai_shape')
            identifier = 'private-hyundai'
            artifacts[identifier] = _write(output / (identifier + '.json'), encoded(payload))
            manifest['private_states'][service] = {'hyundai_tokens': identifier}
            audit['sources'][identifier] = {'sha256': spec['private_tokens']['sha256'], 'bytes': spec['private_tokens']['bytes'],
                                           'converter': 'private-hyundai-v1', 'normalized_sha256': artifacts[identifier]['sha256']}
    need(absences_used == set(dispositions['absences']), 'unconsumed_absence_evidence')
    reader.unchanged()
    receipt = {'schema_version': 1, 'capture_id': uuid.uuid4().hex,
               'source_runtime_sha256': sha(evidence['runtime']), 'writer_graph_sha256': sha(evidence['writer_graph']),
               'writer_fence_receipt_sha256': sha(evidence['writer_closure']), 'writers_closed_at': utc(closure['writers_closed_at']).isoformat().replace('+00:00', 'Z'),
               'capture_started_at': started, 'capture_finished_at': now(), 'services': sorted(SERVICES),
               **{key: plan[key] for key in ('target_system_id', 'cluster', 'plan_sha256')},
               'artifacts': {key: {'sha256': value['sha256'], 'bytes': value['bytes']} for key, value in artifacts.items()},
               'capture_audit': _write(output / 'capture-audit.json', encoded(audit))}
    manifest['freeze_receipt'] = _write(output / 'freeze-receipt.json', encoded(receipt))
    # Validate all normalized data, blob credentials and import contracts before publication.
    temporary_manifest = output / 'capture-pending.json'
    binding = _write(temporary_manifest, encoded(manifest))
    config = {key: plan[key] for key in ('source_commit', 'cluster', 'plan_sha256')}
    frozen = FrozenSources(temporary_manifest, binding['sha256'], config, source_only_preflight=True)
    frozen.unchanged()
    reader.unchanged()
    need(output.lstat().st_ino == output_stamp[1] and not output.is_symlink(), 'capture_destination_changed')
    final = output / 'manifest.json'
    os.link(temporary_manifest, final, follow_symlinks=False)
    temporary_manifest.unlink()
    directory_fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return {'status': 'captured', 'manifest_sha256': binding['sha256'],
            'capture_audit_sha256': receipt['capture_audit']['sha256'], 'live_fencing_verified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True)
    parser.add_argument('--plan-sha256', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        result = capture(args.plan, args.plan_sha256, args.output)
    except Exception:
        # Never echo underlying source data, private paths or SQLite row/error text.
        print(json.dumps({'status': 'rejected', 'reason': 'frozen_capture_rejected'}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
