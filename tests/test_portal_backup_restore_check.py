"""Isolated restore checks: never exercise a real provider or production mount."""
import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'infra/k8s/tools/portal-backup-restore-check.py'


class RestoreCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('portal_restore_check', SCRIPT)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / 'scratch'
        self.work.mkdir()
        self.creds = self.root / 'credentials'
        self.creds.mkdir()
        for name in ('rclone-config', 'rclone-config-passphrase', 'age-identity'):
            (self.creds / name).write_text('PRIVATE-CREDENTIAL')
        self.source = self.root / 'source'
        (self.source / 'data/files').mkdir(parents=True)
        (self.source / 'data/portal-web-state').mkdir()
        (self.source / 'data/files/note.txt').write_text('preserved note')
        db = sqlite3.connect(self.source / 'data/portal-web-state/homeops.sqlite3')
        db.execute('CREATE TABLE sample (value TEXT)')
        db.commit()
        db.close()
        self.now = datetime(2026, 10, 7, 1, tzinfo=timezone.utc)
        self.values = dict(schema_version='1', scope='portal', backup_status='success', encrypted='true',
                           restore_status='success', backup_id='portal-20261007T000000Z-42',
                           source_runtime='k3s-pvc', restore_check='sqlite_quick_check', restore_path_check='success',
                           backup_completed_at='2026-10-07T00:00:00Z', restore_verified_at='2026-10-07T00:01:00Z',
                           evidence_expires_at='2026-10-08T00:00:00Z', artifact_digest='sha256:' + hashlib.sha256(b'ciphertext').hexdigest(),
                           source_digest=self.mod.source_digest(self.source / 'data'))
        self.evidence = self.root / 'evidence'
        self.archive = self.root / 'source.tar'
        self.make_archive()
        self.write_evidence()
        self.calls = []

    def write_evidence(self):
        self.evidence.write_text(''.join(f'{k}={v}\n' for k, v in self.values.items()))

    def make_archive(self):
        (self.source / 'manifest.txt').write_text('source_runtime=k3s-pvc\nsource_digest=' + self.values['source_digest'] + '\n')
        with tarfile.open(self.archive, 'w') as archive:
            archive.add(self.source / 'data', arcname='data')
            archive.add(self.source / 'manifest.txt', arcname='manifest.txt')

    def provider(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command[0] == 'rclone':
            Path(command[-1]).write_bytes(b'ciphertext')
        elif command[0] == 'age':
            Path(command[command.index('-o') + 1]).write_bytes(self.archive.read_bytes())
        else:
            raise AssertionError(command)

    def run_check(self):
        return self.mod.check_restore(self.evidence, self.work, self.creds, now=self.now, runner=self.provider)

    def test_success_downloads_only_and_preserves_authoritative_inputs(self):
        before = self.evidence.read_bytes()
        self.run_check()
        self.assertEqual(self.evidence.read_bytes(), before)
        self.assertEqual(list(self.work.iterdir()), [])
        command, kwargs = self.calls[0]
        self.assertEqual(command[-2], 'gdrive:PersonalServer-encrypted-backups/' + self.values['backup_id'] + '.tar.age')
        self.assertIn('--password-command', command)
        self.assertNotIn('PRIVATE-CREDENTIAL', repr(self.calls))
        self.assertEqual(kwargs['env'].keys(), {'PATH', 'HOME', 'LANG'})
        self.assertTrue(all('copyto' in c or c[0] == 'age' for c, _ in self.calls))

    def test_schema_fields_and_strict_freshness(self):
        changes = {'schema_version': '2', 'scope': 'book', 'source_runtime': 'compose-local',
                   'encrypted': 'false', 'restore_check': 'other', 'restore_path_check': 'failure',
                   'backup_id': '../outside', 'artifact_digest': 'sha256:abc',
                   'backup_completed_at': '2026-10-7T00:00:00Z',
                   'restore_verified_at': '2026-10-07T02:00:00Z',
                   'evidence_expires_at': '2026-10-07T01:00:00Z'}
        for key, value in changes.items():
            with self.subTest(key=key):
                values = self.values | {key: value}
                with self.assertRaises(self.mod.RestoreError):
                    self.mod.validate_evidence(values, self.now)
        for key in self.values:
            with self.subTest(missing=key):
                values = dict(self.values)
                del values[key]
                with self.assertRaises(self.mod.RestoreError):
                    self.mod.validate_evidence(values, self.now)
        for now in (self.now + timedelta(days=2), self.now - timedelta(days=2)):
            with self.assertRaises(self.mod.RestoreError):
                self.mod.validate_evidence(self.values, now)

    def test_producer_expiry_clock_boundary_preserves_evidence(self):
        # Producer records completion and expiry with separate clock reads.
        # One second between reads must not invalidate otherwise fresh evidence.
        self.values['evidence_expires_at'] = '2026-10-08T00:00:01Z'
        self.write_evidence()
        original = self.evidence.read_bytes()
        self.run_check()
        self.assertEqual(self.evidence.read_bytes(), original)
        # A distant expiry never extends the 24-hour backup freshness limit.
        self.values['evidence_expires_at'] = '2026-10-10T00:00:00Z'
        self.write_evidence()
        with self.assertRaises(self.mod.RestoreError):
            self.mod.validate_evidence(self.values, self.now + timedelta(days=1))

    def test_duplicate_and_unknown_evidence_fail_before_network(self):
        for suffix in ('scope=portal\n', 'unknown=value\n', '\n'):
            self.write_evidence()
            with self.evidence.open('a') as stream:
                stream.write(suffix)
            with self.assertRaises(self.mod.RestoreError):
                self.run_check()
        self.assertEqual(self.calls, [])

    def test_hash_mismatch_blocks_decrypt(self):
        self.values['artifact_digest'] = 'sha256:' + '0' * 64
        self.write_evidence()
        with self.assertRaises(self.mod.RestoreError):
            self.run_check()
        self.assertEqual(len(self.calls), 1)

    def test_manifest_and_source_mismatch(self):
        for kind in ('manifest', 'content'):
            with self.subTest(kind=kind):
                self.make_archive()
                if kind == 'manifest':
                    (self.source / 'manifest.txt').write_text('source_runtime=compose-local\nsource_digest=' + self.values['source_digest'] + '\n')
                else:
                    (self.source / 'data/files/note.txt').write_text('tampered')
                with tarfile.open(self.archive, 'w') as archive:
                    archive.add(self.source / 'data', arcname='data')
                    archive.add(self.source / 'manifest.txt', arcname='manifest.txt')
                with self.assertRaises(self.mod.RestoreError):
                    self.run_check()

    def test_corrupt_sqlite_even_with_matching_digests(self):
        (self.source / 'data/portal-web-state/homeops.sqlite3').write_bytes(b'not sqlite')
        self.values['source_digest'] = self.mod.source_digest(self.source / 'data')
        self.make_archive()
        self.write_evidence()
        with self.assertRaises(self.mod.RestoreError):
            self.run_check()

    def wal_snapshot(self, corrupt=False):
        database = self.source / 'data/portal-web-state/homeops.sqlite3'
        writer = sqlite3.connect(database)
        self.addCleanup(writer.close)
        self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone(), ('wal',))
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('CREATE TABLE wal_only (value TEXT)')
        writer.execute("INSERT INTO wal_only VALUES ('committed in WAL')")
        writer.commit()
        if corrupt:
            writer.execute('PRAGMA writable_schema=ON')
            writer.execute("UPDATE sqlite_schema SET rootpage=2147483647 WHERE name='wal_only'")
            writer.commit()
        self.assertTrue(database.with_name(database.name + '-wal').is_file())
        self.values['source_digest'] = self.mod.source_digest(self.source / 'data')
        self.make_archive()
        self.write_evidence()
        return database

    def test_committed_wal_only_table_is_included_in_restore_check(self):
        database = self.wal_snapshot()
        original_connect = sqlite3.connect
        # The base DB has no wal_only table: a successful SELECT therefore proves
        # the restore checker sees the copied committed WAL, not only the DB file.
        with original_connect(database.as_uri() + '?mode=ro&immutable=1', uri=True) as base:
            self.assertEqual(base.execute("SELECT name FROM sqlite_schema WHERE name='wal_only'").fetchall(), [])
        rows = []
        def observe_connect(database_uri, **kwargs):
            connection = original_connect(database_uri, **kwargs)
            rows.extend(connection.execute('SELECT value FROM wal_only').fetchall())
            return connection
        with patch.object(self.mod.sqlite3, 'connect', side_effect=observe_connect):
            self.run_check()
        self.assertEqual(rows, [('committed in WAL',)])

    def test_wal_only_schema_corruption_fails_with_matching_source_digest(self):
        self.wal_snapshot(corrupt=True)
        with self.assertRaises(self.mod.RestoreError):
            self.run_check()
        self.assertEqual(list(self.work.iterdir()), [])

    def test_tar_rejects_unsafe_members_before_writes(self):
        for name, kind in (('/outside', tarfile.REGTYPE), ('../outside', tarfile.REGTYPE),
                           ('data/../outside', tarfile.REGTYPE), ('data/link', tarfile.SYMTYPE),
                           ('data/hard', tarfile.LNKTYPE), ('data/device', tarfile.CHRTYPE),
                           ('extra', tarfile.REGTYPE)):
            with self.subTest(name=name, kind=kind):
                with tarfile.open(self.archive, 'w') as archive:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = '/outside'
                    archive.addfile(member)
                with self.assertRaises(self.mod.RestoreError):
                    self.run_check()
                self.assertEqual(list(self.work.iterdir()), [])

    def test_duplicate_and_bounded_tar(self):
        with tarfile.open(self.archive, 'w') as archive:
            for name in ('manifest.txt', './manifest.txt'):
                member = tarfile.TarInfo(name)
                archive.addfile(member)
        with self.assertRaises(self.mod.RestoreError):
            self.run_check()
        self.make_archive()
        with patch.object(self.mod, 'MAX_EXTRACT_BYTES', 1):
            with self.assertRaises(self.mod.RestoreError):
                self.run_check()

    def test_missing_credentials_blocks_network(self):
        (self.creds / 'age-identity').unlink()
        with self.assertRaises(self.mod.RestoreError):
            self.run_check()
        self.assertEqual(self.calls, [])

    def test_provider_failure_cli_outputs_only_safe_status(self):
        args = ['--evidence', str(self.evidence), '--work-dir', str(self.work), '--credential-dir', str(self.creds)]
        output = io.StringIO()
        with patch.object(self.mod, 'check_restore', side_effect=RuntimeError('PRIVATE-CREDENTIAL /private/path')):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                self.assertEqual(self.mod.main(args), 1)
        self.assertEqual(output.getvalue(), 'portal_backup_restore_check=FAIL\n')

    def test_provider_failure_cleans_private_scratch(self):
        def failed(command, **kwargs):
            Path(command[-1]).write_bytes(b'partial')
            raise RuntimeError('PRIVATE-CREDENTIAL')
        with self.assertRaises(RuntimeError):
            self.mod.check_restore(self.evidence, self.work, self.creds, now=self.now, runner=failed)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_tar_member_limit(self):
        with patch.object(self.mod, 'MAX_MEMBERS', 1):
            with self.assertRaises(self.mod.RestoreError):
                self.run_check()

    def test_cli_invalid_arguments_are_private(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(self.mod.main(['--unknown', '/private/path']), 1)
        self.assertEqual(output.getvalue(), 'portal_backup_restore_check=FAIL\n')

    def test_tree_digest_matches_gnu_sha256sum_escaping(self):
        directory = self.root / 'digest'
        directory.mkdir()
        for name in ('a', 'b\\c', 'd\ne'):
            (directory / name).write_bytes(b'x')
        digest = hashlib.sha256(b'x').hexdigest()
        rows = f'{digest}  ./a\n\\{digest}  ./b\\\\c\n\\{digest}  ./d\\ne\n'
        self.assertEqual(self.mod.tree_digest(directory), hashlib.sha256(rows.encode()).hexdigest())


if __name__ == '__main__':
    unittest.main()
