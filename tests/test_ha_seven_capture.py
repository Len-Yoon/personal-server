"""Offline capture fixtures; no production sources, writers, networks or PG."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'infra/k8s'))
from ha_runtime import seven_capture as C
from ha_runtime import seven_migration as M


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class CaptureFixture:
    def __init__(self, root):
        self.root = root
        self.inputs = root / 'inputs'
        self.inputs.mkdir(mode=0o700)
        self.output = root / 'captured'
        self.services = {service: {'databases': {}, 'states': {}} for service in C.SERVICES}
        self.plan = {'schema_version': 1, 'source_commit': 'a' * 40, 'cluster': 'capture_fixture',
                     'plan_sha256': 'b' * 64, 'target_system_id': '123456',
                     'services': self.services, 'evidence': {}}
        self.time = datetime.now(timezone.utc) - timedelta(seconds=10)
        self.iso = lambda seconds: (self.time + timedelta(seconds=seconds)).isoformat().replace('+00:00', 'Z')
        for service in sorted(C.SERVICES):
            for component in sorted(C.DATABASES[service]):
                if service == 'homeops-executor':
                    self.services[service]['databases'][component] = {'path': str(self.inputs / 'missing-homeops.sqlite3'),
                                                                     'absence': 'homeops-executor.main'}
                else:
                    self.services[service]['databases'][component] = self.database(service + '.' + component)
        auth = self.inputs / 'portal-auth.sqlite3'
        with sqlite3.connect(auth) as db:
            for table in ('sessions', 'confirmations'):
                db.execute('CREATE TABLE ' + table + '(token_hash TEXT PRIMARY KEY, scope TEXT NOT NULL, payload_hash TEXT NOT NULL, expires_at REAL NOT NULL)')
                db.execute('INSERT INTO ' + table + ' VALUES(?,?,?,?)', (sha(table.encode()), 'file_access', sha(b''), 1900000000.0))
        auth.chmod(0o600)
        self.services['portal-web']['states'] = {
            'auth_sessions': dict(C.descriptor(auth), sidecars={}),
            'auth_failures': self.json('portal-rate', {'file_access': {'fixture-client': [self.iso(1)]}}),
            'portfolio': self.raw('portfolio.md', b'# retained portfolio\n'),
        }
        for service in ('book-memo', 'youtube-memo'):
            self.services[service]['states'] = {'auth-rate-limit': self.json(service + '-rates', {'fixture-client': [self.iso(1)]})}
        self.services['crawler-worker']['states'] = {
            'archive': self.json('archive', {'schema_version': '2026-08-20-market-focus-v3', 'articles': [{'title': 'retained article'}],
                                           'updated_at': self.iso(1), 'telegram_outbox': []}),
            'collection_status': self.json('collection', {'initialized': True, 'first_attempt_at': self.iso(1),
                'last_attempt_at': self.iso(1), 'last_success_at': self.iso(1), 'last_failure_at': None,
                'failures_total': 0, 'consecutive_failures': 0}),
        }
        self.services['car-care-worker']['private_tokens'] = self.json('private-provider', {'refresh_token': 'fixture-private-provider-token'})
        files = self.inputs / 'attachments'
        files.mkdir(mode=0o700)
        (files / 'folder').mkdir(mode=0o700)
        self.write(files / 'folder' / 'retained.txt', b'retained attachment')
        trash = files / '.trash'
        trash.mkdir(mode=0o700)
        token = trash / ('c' * 32)
        token.mkdir(mode=0o700)
        self.write(token / 'metadata.json', encoded({'path': 'old-folder', 'trashed_at': self.iso(1)}))
        (token / 'item').mkdir(mode=0o700)
        self.write(token / 'item' / 'old.txt', b'retained trash')
        self.services['portal-web']['files'] = {'path': str(files), 'tree_sha256': C.inspect_tree(files)}
        self.runtime = {'schema_version': 1, 'services': sorted(C.SERVICES), 'source': 'offline-test-fixture'}
        self.runtime_entry = self.json('runtime', self.runtime)
        self.graph = {'schema_version': 1, 'source_runtime_sha256': self.runtime_entry['sha256'],
                      'services': {service: [service + '.writer'] for service in sorted(C.SERVICES)}}
        self.graph_entry = self.json('graph', self.graph)
        self.closure = {'schema_version': 1, 'source_runtime_sha256': self.runtime_entry['sha256'],
                        'writer_graph_sha256': self.graph_entry['sha256'], 'cluster': self.plan['cluster'],
                        'plan_sha256': self.plan['plan_sha256'], 'target_system_id': self.plan['target_system_id'],
                        'services': sorted(C.SERVICES), 'writers_closed_at': self.iso(0),
                        'observations': [{'writer_id': service + '.writer', 'state': 'closed',
                                          'observed_at': self.iso(1), 'evidence_sha256': 'e' * 64}
                                         for service in sorted(C.SERVICES)]}
        self.dispositions = {'schema_version': 1, 'source_runtime_sha256': self.runtime_entry['sha256'],
                             'services': sorted(C.SERVICES), 'session_dispositions': {service: 'invalidate_and_require_relogin'
                                for service in ('book-memo', 'youtube-memo')}, 'stateless_services': ['system-agent'],
                             'absences': {'homeops-executor.main': {
                                 'source_path_sha256': sha(str(self.inputs / 'missing-homeops.sqlite3').encode()),
                                 'checked_at': self.iso(1), 'inventory_sha256': self.runtime_entry['sha256']}}}
        self.plan['evidence'] = {'runtime': self.runtime_entry, 'writer_graph': self.graph_entry,
                                 'writer_closure': self.json('closure', self.closure),
                                 'dispositions': self.json('dispositions', self.dispositions)}
        self.plan_path = self.inputs / 'capture-plan.json'
        self.save()

    @staticmethod
    def write(path, raw):
        path.write_bytes(raw)
        path.chmod(0o600)

    def raw(self, name, raw):
        path = self.inputs / name
        self.write(path, raw)
        return C.descriptor(path)

    def json(self, name, value):
        return self.raw(name + '.json', encoded(value))

    def database(self, name):
        path = self.inputs / (name + '.sqlite3')
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE records(id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL)')
            db.execute('INSERT INTO records(value) VALUES(?)', ('retained 한글',))
        path.chmod(0o600)
        return dict(C.descriptor(path), sidecars={})

    def save(self):
        self.write(self.plan_path, encoded(self.plan))
        self.expected = sha(self.plan_path.read_bytes())

    def run(self):
        self.save()
        return C.capture(self.plan_path, self.expected, self.output)

    def load(self, result):
        return M.FrozenSources(self.output / 'manifest.json', result['manifest_sha256'],
                               {key: self.plan[key] for key in ('source_commit', 'cluster', 'plan_sha256')},
                               source_only_preflight=True)


class FrozenCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.fixture = CaptureFixture(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def reject(self, reason):
        with self.assertRaises(M.MigrationRejected) as error:
            self.fixture.run()
        self.assertEqual(str(error.exception), reason)
        self.assertFalse((self.fixture.output / 'manifest.json').exists())

    def test_full_capture_is_accepted_and_preserves_actual_normalized_shapes(self):
        result = self.fixture.run()
        source = self.fixture.load(result)
        self.assertFalse(result['live_fencing_verified'])
        self.assertEqual(set(source.states), set(C.SERVICES))
        self.assertEqual(source.states['portal-web']['portfolio']['content'], '# retained portfolio\n')
        self.assertIn(sha(b'sessions'), source.states['portal-web']['auth_sessions']['sessions'])
        self.assertEqual(source.states['book-memo']['write-sessions'], {})
        self.assertEqual(source.states['youtube-memo']['write-sessions'], {})
        self.assertEqual(source.states['homeops-executor']['restart_claims'], {})
        self.assertEqual(source.states['crawler-worker']['archive']['articles'][0]['title'], 'retained article')
        namespace = source.states['portal-web']['file-namespace']
        self.assertIn('folder/retained.txt', namespace['entries'])
        self.assertIn('old-folder/old.txt', namespace['trash']['c' * 32]['entries'])
        self.assertEqual(len(source.objects), 2)
        self.assertEqual(source.private_payload['refresh_token'], 'fixture-private-provider-token')
        self.assertNotIn('hyundai_tokens', source.states['car-care-worker'])
        audit = json.loads((self.fixture.output / 'capture-audit.json').read_text())
        self.assertEqual(audit['converter_sha256'], sha((ROOT / 'infra/k8s/ha_runtime/seven_capture.py').read_bytes()))
        self.assertEqual(set(audit['evidence']), C.EVIDENCE)
        self.assertNotIn(str(self.fixture.inputs), json.dumps(audit))
        source.unchanged()

    def test_sqlite_wal_rows_are_backed_up_without_touching_original(self):
        entry = self.fixture.services['book-memo']['databases']['main']
        path = Path(entry['path'])
        with sqlite3.connect(path) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute("INSERT INTO records(value) VALUES('committed-in-wal')")
            writer.commit()
            for suffix in ('', '-wal', '-shm'):
                Path(str(path) + suffix).chmod(0o600)
            entry = dict(C.descriptor(path), sidecars={suffix: C.descriptor(str(path) + suffix) for suffix in ('-wal', '-shm')})
            self.fixture.services['book-memo']['databases']['main'] = entry
            before = {suffix: Path(str(path) + suffix).read_bytes() for suffix in ('', '-wal', '-shm')}
            result = self.fixture.run()
            source = self.fixture.load(result)
            with sqlite3.connect(source.databases['book-memo']['main']) as db:
                self.assertEqual(db.execute('SELECT value FROM records ORDER BY id').fetchall(), [('retained 한글',), ('committed-in-wal',)])
            self.assertEqual(before, {suffix: Path(str(path) + suffix).read_bytes() for suffix in before})

    def test_wal_cannot_be_silently_omitted_from_inventory(self):
        entry = self.fixture.services['book-memo']['databases']['main']
        self.fixture.write(Path(entry['path'] + '-wal'), b'unlisted-wal')
        self.reject('sqlite_sidecar_inventory_mismatch')

    def test_changed_source_hash_is_rejected(self):
        item = self.fixture.services['portal-web']['states']['portfolio']
        Path(item['path']).write_bytes(b'# changed source bytes\n')
        self.reject('source_binding_invalid')

    def test_absent_homeops_requires_explicit_bound_proof(self):
        self.fixture.dispositions['absences'].clear()
        self.fixture.plan['evidence']['dispositions'] = self.fixture.json('dispositions', self.fixture.dispositions)
        self.reject('proven_absence_required')

    def test_homeops_claims_are_converted_with_uncertain_outcomes_preserved(self):
        path = self.fixture.inputs / 'homeops-claims.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE restart_claims(request_id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,created_at REAL NOT NULL,state TEXT NOT NULL,result TEXT)')
            db.execute('INSERT INTO restart_claims VALUES(?,?,?,?,?)', ('fixture-request', 'd' * 64, 1.0, 'inflight', None))
        path.chmod(0o600)
        self.fixture.services['homeops-executor']['databases']['main'] = dict(C.descriptor(path), sidecars={})
        self.fixture.dispositions['absences'].clear()
        self.fixture.plan['evidence']['dispositions'] = self.fixture.json('dispositions', self.fixture.dispositions)
        source = self.fixture.load(self.fixture.run())
        self.assertEqual(source.states['homeops-executor']['restart_claims']['fixture-request']['state'], 'inflight')
        self.assertIsNone(source.states['homeops-executor']['restart_claims']['fixture-request']['result'])

    def test_memory_sessions_require_explicit_invalidate_relogin_disposition(self):
        self.fixture.dispositions['session_dispositions'].pop('book-memo')
        self.fixture.plan['evidence']['dispositions'] = self.fixture.json('dispositions', self.fixture.dispositions)
        self.reject('explicit_source_dispositions_required')

    def test_writer_closure_cannot_omit_a_graph_writer(self):
        self.fixture.closure['observations'].pop()
        self.fixture.plan['evidence']['writer_closure'] = self.fixture.json('closure', self.fixture.closure)
        self.reject('external_writer_closure_incomplete')

    def test_writer_closure_is_bound_to_runtime_graph_and_target(self):
        self.fixture.closure['writer_graph_sha256'] = 'f' * 64
        self.fixture.plan['evidence']['writer_closure'] = self.fixture.json('closure', self.fixture.closure)
        self.reject('external_writer_closure_binding_invalid')

    def test_file_symlink_is_rejected(self):
        root = Path(self.fixture.services['portal-web']['files']['path'])
        (root / 'link.txt').symlink_to(root / 'folder' / 'retained.txt')
        self.reject('source_symlink_rejected')

    def test_secret_provider_payload_cannot_cross_into_attachment(self):
        root = Path(self.fixture.services['portal-web']['files']['path'])
        self.fixture.write(root / 'token-note.txt', b'fixture-private-provider-token')
        self.fixture.services['portal-web']['files']['tree_sha256'] = C.inspect_tree(root)
        self.reject('private_credential_blob_copy_rejected')

    def test_nested_portal_and_flat_memo_rate_limit_shapes_are_not_interchangeable(self):
        self.fixture.services['book-memo']['states']['auth-rate-limit'] = self.fixture.json('wrong-rate-shape', {'scope': {'client': []}})
        self.reject('unsupported_rate_limit_shape')

    def test_missing_service_is_not_defaulted(self):
        self.fixture.services.pop('system-agent')
        self.reject('all_seven_sources_required')

    def test_existing_output_is_preserved(self):
        self.fixture.output.mkdir(mode=0o700)
        sentinel = self.fixture.output / 'keep.txt'
        sentinel.write_bytes(b'operator-owned')
        self.reject('capture_destination_exists')
        self.assertEqual(sentinel.read_bytes(), b'operator-owned')

    def test_private_source_mode_is_enforced(self):
        item = self.fixture.services['portal-web']['states']['portfolio']
        Path(item['path']).chmod(0o644)
        self.reject('trusted_private_source_required')

    def test_hardlinked_source_is_rejected(self):
        item = self.fixture.services['portal-web']['states']['portfolio']
        os.link(item['path'], self.fixture.inputs / 'aliased-portfolio.md')
        self.reject('trusted_private_source_required')

    def test_corrupt_sqlite_is_not_published(self):
        item = self.fixture.services['book-memo']['databases']['main']
        self.fixture.write(Path(item['path']), b'not-a-sqlite-database')
        self.fixture.services['book-memo']['databases']['main'] = dict(C.descriptor(item['path']), sidecars={})
        self.reject('sqlite_snapshot_invalid')

    def test_traversal_in_legacy_trash_is_not_normalized(self):
        files = Path(self.fixture.services['portal-web']['files']['path'])
        self.fixture.write(files / '.trash' / ('c' * 32) / 'metadata.json',
                           encoded({'path': '../outside', 'trashed_at': self.fixture.iso(1)}))
        self.fixture.services['portal-web']['files']['tree_sha256'] = C.inspect_tree(files)
        with self.assertRaises(ValueError):
            self.fixture.run()
        self.assertFalse((self.fixture.output / 'manifest.json').exists())

    def test_source_mutation_during_capture_is_not_published(self):
        original_write = C._write
        source = Path(self.fixture.services['portal-web']['states']['portfolio']['path'])
        def mutate(path, raw):
            result = original_write(path, raw)
            if path.name == 'portal-web.portfolio.json':
                self.fixture.write(source, b'# changed after source was read')
            return result
        with patch.object(C, '_write', side_effect=mutate):
            self.reject('frozen_source_changed')

    def test_unsupported_legacy_archive_is_never_silently_reset(self):
        self.fixture.services['crawler-worker']['states']['archive'] = self.fixture.json('old-archive',
            {'schema_version': '2026-07-15-korean-news-v2', 'articles': [{'retained': 'must-not-disappear'}]})
        self.reject('legacy_archive_converter_unavailable')

    def test_cli_rejection_does_not_print_private_path_or_data(self):
        with patch('builtins.print') as printed:
            code = C.main(['--plan', str(self.fixture.plan_path), '--plan-sha256', '0' * 64,
                           '--output', str(self.fixture.output)])
        self.assertEqual(code, 2)
        payload = json.loads(printed.call_args.args[0])
        self.assertEqual(payload, {'status': 'rejected', 'reason': 'frozen_capture_rejected'})


if __name__ == '__main__':
    unittest.main()
