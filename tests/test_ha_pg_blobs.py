"""Actual in-memory SQL transactions, shared by namespace and PG blob adapter.

This is a SQLite SQL/storage test double, not PostgreSQL/replication evidence.
The root PG integration suite owns real roles, synchronous proof and restore.
"""
import copy
from contextlib import contextmanager
import hashlib
import importlib
import io
import json
import os
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from infra.k8s.ha_runtime import blobs
from infra.k8s.ha_runtime.config import DatabaseUnavailable
from tests._test_support import prepare_service_import


class Store:
    DatabaseUnavailable = DatabaseUnavailable

    def __init__(self):
        self.sql = sqlite3.connect(':memory:')
        self.sql.execute('CREATE TABLE ha_blob_objects(sha256 text PRIMARY KEY,size integer,chunk_count integer)')
        self.sql.execute('CREATE TABLE ha_blob_chunks(sha256 text,ordinal integer,payload blob,PRIMARY KEY(sha256,ordinal))')
        self.sql.execute('CREATE TABLE shared_state(key text PRIMARY KEY,payload text)')
        self.sql.commit(); self.active = False; self.ready = True; self.fail_commit = False; self.fail_after_commit = False
        self.statements = []; self.replay = 0; self.fail_chunk = None

    def execute(self, statement, parameters=()):
        if not self.active:
            raise DatabaseUnavailable('outside_state_transaction')
        self.statements.append((statement, parameters))
        if statement == 'SELECT ha.enforced_vm_blob_ready()':
            return SimpleNamespace(fetchone=lambda: (self.ready,))
        if statement.startswith('INSERT INTO ha_blob_chunks') and parameters[1] == self.fail_chunk:
            raise DatabaseUnavailable('synthetic_chunk_failure')
        return self.sql.execute(statement, parameters)

    def protect_replay(self):
        self.replay += 1

    def current(self, service):
        if service != 'portal-web' or not self.active:
            raise DatabaseUnavailable('outside_state_transaction')
        return self

    @contextmanager
    def state_transaction(self, service, key, default):
        if self.active:
            raise DatabaseUnavailable('test_nested_transaction')
        self.sql.execute('BEGIN'); self.active = True
        try:
            row = self.sql.execute('SELECT payload FROM shared_state WHERE key=?', (key,)).fetchone()
            payload = copy.deepcopy(json.loads(row[0]) if row else default)
            yield payload
            self.sql.execute('INSERT INTO shared_state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload',
                             (key, json.dumps(payload)))
            if self.fail_commit:
                raise DatabaseUnavailable('synthetic_precommit_failure')
            self.sql.commit()
            if self.fail_after_commit:
                raise DatabaseUnavailable('synthetic_postcommit_proof_failure')
        except BaseException:
            self.sql.rollback()
            raise
        finally:
            self.active = False


class PgBlobTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(); self.addCleanup(self.store.sql.close)
        self.current = patch.object(blobs.db, 'active_state_connection', self.store.current, create=True)
        self.current.start(); self.addCleanup(self.current.stop)

    @contextmanager
    def transaction(self):
        with self.store.state_transaction('portal-web', 'file-namespace', {}) as state:
            yield state

    def test_chunks_empty_and_short_stream_delivery_roundtrip(self):
        class Short(io.BytesIO):
            def read(self, count):
                return super().read(min(count, 7))
        for payload in [b'', b'a', b'content' * 20000]:
            with self.transaction():
                result = blobs.put('portal-web', Short(payload))
                self.assertEqual(result, {'sha256': hashlib.sha256(payload).hexdigest(), 'size': len(payload)})
                self.assertEqual(blobs.get('portal-web', **dict(identifier=result['sha256'], size=result['size'])), payload)
        self.assertEqual(self.store.replay, 3)

    def test_namespace_and_chunks_rollback_together_on_precommit_failure(self):
        self.store.fail_commit = True
        with self.assertRaises(DatabaseUnavailable):
            with self.transaction() as state:
                state['file'] = blobs.put('portal-web', io.BytesIO(b'proof-bound'))
        self.assertEqual(self.store.sql.execute('SELECT count(*) FROM ha_blob_objects').fetchone()[0], 0)
        self.assertEqual(self.store.sql.execute('SELECT count(*) FROM ha_blob_chunks').fetchone()[0], 0)
        self.assertEqual(self.store.sql.execute('SELECT count(*) FROM shared_state').fetchone()[0], 0)

    def test_lost_postcommit_proof_raises_but_keeps_complete_atomic_commit(self):
        self.store.fail_after_commit = True
        with self.assertRaises(DatabaseUnavailable):
            with self.transaction() as state:
                state['file'] = blobs.put('portal-web', io.BytesIO(b'committed-but-unacknowledged'))
        self.assertEqual(self.store.sql.execute('SELECT count(*) FROM ha_blob_objects').fetchone()[0], 1)
        self.assertEqual(self.store.sql.execute('SELECT count(*) FROM ha_blob_chunks').fetchone()[0], 1)
        self.assertEqual(self.store.sql.execute('SELECT count(*) FROM shared_state').fetchone()[0], 1)

    def test_partial_chunk_failure_rolls_back_object_and_namespace(self):
        self.store.fail_chunk = 1
        with self.assertRaises(DatabaseUnavailable):
            with self.transaction() as state:
                state['file'] = blobs.put('portal-web', io.BytesIO(b'x' * (blobs.CHUNK_BYTES + 1)))
        self.assertEqual(self.store.sql.execute('SELECT count(*) FROM ha_blob_objects').fetchone()[0], 0)

    def test_missing_reordered_size_or_hash_corruption_is_rejected(self):
        for kind in ['missing', 'ordinal', 'size', 'hash']:
            with self.subTest(kind=kind):
                payload = ('value-' + kind).encode()
                with self.transaction():
                    result = blobs.put('portal-web', io.BytesIO(payload))
                identifier = result['sha256']
                if kind == 'missing':
                    self.store.sql.execute('DELETE FROM ha_blob_chunks WHERE sha256=?', (identifier,))
                elif kind == 'ordinal':
                    self.store.sql.execute('UPDATE ha_blob_chunks SET ordinal=1 WHERE sha256=?', (identifier,))
                elif kind == 'size':
                    self.store.sql.execute('UPDATE ha_blob_objects SET size=size+1 WHERE sha256=?', (identifier,))
                else:
                    self.store.sql.execute('UPDATE ha_blob_chunks SET payload=? WHERE sha256=?', (b'z' * len(payload), identifier))
                self.store.sql.commit()
                with self.assertRaises(DatabaseUnavailable):
                    with self.transaction():
                        blobs.get('portal-web', identifier, len(payload))

    def test_dedup_verifies_existing_bytes_and_never_updates(self):
        with self.transaction():
            first = blobs.put('portal-web', io.BytesIO(b'same'))
            second = blobs.put('portal-web', io.BytesIO(b'same'))
        self.assertEqual(first, second)
        self.assertEqual(self.store.sql.execute('SELECT count(*) FROM ha_blob_chunks').fetchone()[0], 1)
        self.assertFalse(any(sql.startswith(('UPDATE', 'DELETE', 'TRUNCATE')) for sql, _ in self.store.statements))

    def test_no_active_transaction_wrong_service_and_false_readiness_fail_closed(self):
        with self.assertRaises(DatabaseUnavailable):
            blobs.put('portal-web', io.BytesIO(b'x'))
        with self.transaction():
            with self.assertRaises(DatabaseUnavailable):
                blobs.put('other-service', io.BytesIO(b'x'))
            self.store.ready = False
            with self.assertRaises(DatabaseUnavailable):
                blobs.put('portal-web', io.BytesIO(b'x'))

    def test_size_and_private_prefix_limits_precede_insert(self):
        for stream, maximum, callback in [(io.BytesIO(b'abc'), 2, None),
                                           (io.BytesIO(b'private'), 20, lambda _: (_ for _ in ()).throw(ValueError('private')) )]:
            with self.assertRaises(ValueError):
                with self.transaction():
                    blobs.put('portal-web', stream, max_bytes=maximum, validate_prefix=callback)
        self.assertFalse(any(sql.startswith('INSERT') for sql, _ in self.store.statements))

    def test_setup_only_portal_state_schema_and_no_grants_or_gc(self):
        calls = []; admin = SimpleNamespace(execute=lambda sql: calls.append(sql))
        blobs.setup_namespace(admin, 's_portal_web_state')
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(sql.startswith('CREATE TABLE') for sql in calls))
        self.assertIn('REFERENCES "s_portal_web_state".ha_blob_objects', calls[1])
        with self.assertRaises(DatabaseUnavailable):
            blobs.setup_namespace(admin, 'untrusted;DROP SCHEMA ha')

    def test_retained_orphan_bytes_and_object_count_bound_new_inserts(self):
        with self.transaction():
            first = blobs.put('portal-web', io.BytesIO(b'orphan'), max_namespace_bytes=6)
        with self.assertRaises(ValueError):
            with self.transaction():
                blobs.put('portal-web', io.BytesIO(b'new'), max_namespace_bytes=6)
        with self.transaction():
            self.assertEqual(blobs.put('portal-web', io.BytesIO(b'orphan'), max_namespace_bytes=6), first)
        with patch.object(blobs, 'MAX_OBJECTS', 1), self.assertRaises(ValueError):
            with self.transaction():
                blobs.put('portal-web', io.BytesIO(b'new'))

    def test_sql_backup_restore_on_fresh_store_preserves_blob_rows(self):
        payload = b'complete snapshot payload'
        with self.transaction() as state:
            state['reference'] = blobs.put('portal-web', io.BytesIO(payload))
        restored = Store(); self.addCleanup(restored.sql.close)
        self.store.sql.backup(restored.sql)
        with patch.object(blobs.db, 'active_state_connection', restored.current):
            with restored.state_transaction('portal-web', 'file-namespace', {}) as state:
                self.assertEqual(blobs.get('portal-web', state['reference']['sha256'], state['reference']['size']), payload)


class PortalPgBlobTests(unittest.TestCase):
    def setUp(self):
        PgBlobTests.setUp(self)
        prepare_service_import('portal-web')
        self.files = importlib.import_module('app.services.ha_files')
        self.file_store = importlib.import_module('app.services.file_store')
        self.guard = patch.object(self.files, '_guard', return_value={'profile': 'enforced-vm-v1'})
        self.guard.start(); self.addCleanup(self.guard.stop)
        runtime = patch.object(self.files, 'runtime', return_value=self.store)
        runtime.start(); self.addCleanup(runtime.stop)
        self.modules = patch.dict(sys.modules, {'ha_runtime': SimpleNamespace(blobs=blobs)})
        self.modules.start(); self.addCleanup(self.modules.stop)
        environment = patch.dict(os.environ, {'HA_PG_CONFIG': 'synthetic', 'APP_ENV': 'test', 'HA_LOCAL_RUNTIME': '1'})
        environment.start(); self.addCleanup(environment.stop)

    def upload(self, name='memo.txt', content=b'pg-content'):
        return SimpleNamespace(filename=name, content_type='text/plain', file=io.BytesIO(content))

    def test_real_portal_upload_download_trash_and_archive_without_object_root(self):
        import zipfile
        with patch.object(self.files, '_root', side_effect=AssertionError('filesystem backend must not run')):
            self.assertEqual(self.file_store.save_upload('', self.upload()), 10)
            path = self.file_store.get_download_path('memo.txt')
            self.addCleanup(path.close); self.assertEqual(path.read_bytes(), b'pg-content')
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w') as archive:
                self.file_store.write_download_archive(archive, ['memo.txt'])
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.read('memo.txt'), b'pg-content')
            token = self.files.trash('memo.txt'); self.files.restore(token, 'back.txt')
            self.assertEqual(self.file_store.get_directory()['files'][0]['name'], 'back.txt')

    def test_bulk_and_replication_failures_have_no_pg_orphans(self):
        for proof_failure in [False, True]:
            self.store.fail_commit = proof_failure
            expected = DatabaseUnavailable if proof_failure else FileExistsError
            with self.assertRaises(expected):
                self.files.save_uploads('', [self.upload(), self.upload()] if not proof_failure else [self.upload()])
            self.assertEqual(self.store.sql.execute('SELECT count(*) FROM ha_blob_objects').fetchone()[0], 0)
            self.assertEqual(self.store.sql.execute('SELECT count(*) FROM shared_state').fetchone()[0], 0)

    def test_production_requires_exact_profile_and_server_readiness(self):
        self.guard.stop()
        for profile in ['enforced-vm-v1', 'n100-approved', None]:
            config = SimpleNamespace(load=lambda service: {'profile': profile})
            with patch.dict(sys.modules, {'ha_runtime.config': config}), patch.dict(os.environ, {'APP_ENV': 'production'}):
                self.store.ready = False
                with self.assertRaises(DatabaseUnavailable):
                    self.files.create_directory('', 'blocked')
