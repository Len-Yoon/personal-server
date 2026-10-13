import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('car_backup', ROOT / 'car-care-worker/tools/backup.py')
BACKUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BACKUP)


class CarCareBackupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name).resolve()
        self.database = self.base / 'car-care.sqlite3'
        self.credentials, self.scratch, self.evidence = [self.base / name for name in ('credentials', 'scratch', 'evidence')]
        for directory in (self.credentials, self.scratch, self.evidence):
            directory.mkdir(mode=0o700)
        for name in ('age-recipient', 'age-identity', 'rclone-config-passphrase'):
            (self.credentials / name).write_text('test-only')
        (self.credentials / 'rclone-config').write_text('RCLONE_ENCRYPT_V0:\nfixture-encrypted\n')
        self.connection = sqlite3.connect(self.database)
        self.addCleanup(self.connection.close)
        self.connection.execute('PRAGMA journal_mode=WAL')
        for name in BACKUP.TABLES:
            self.connection.execute(f'CREATE TABLE {name} (id INTEGER PRIMARY KEY, value TEXT)')
        self.connection.execute("INSERT INTO maintenance_records(value) VALUES ('committed-wal')")
        self.connection.commit()
        self.calls = []
        self.remote = {}

    def runner(self, args):
        self.calls.append(args)
        if args[0] == 'age':
            destination = Path(args[args.index('-o') + 1])
            raw = Path(args[-1]).read_bytes()
            destination.write_bytes(raw[4:] if '-d' in args else b'age:' + raw)
        elif 'copyto' in args:
            source, destination = args[-2:]
            if source.startswith('gdrive:'):
                Path(destination).write_bytes(self.remote[source])
            else:
                self.remote[destination] = Path(source).read_bytes()

    def execute(self, runner=None):
        with patch.object(BACKUP.HELPERS, 'memory_backed', return_value=True), patch.object(BACKUP.shutil, 'which', return_value='/fixture/bin'):
            return BACKUP.execute(self.database, self.credentials, self.scratch, self.evidence, runner or self.runner)

    def test_committed_wal_snapshot_and_remote_roundtrip_preserve_original(self):
        before = self.database.read_bytes()
        result = self.execute()
        self.assertEqual(result['status'], 'success')
        self.assertTrue(result['restore_verified'])
        self.assertEqual(self.database.read_bytes(), before)
        snapshot = self.base / 'download.sqlite3'
        snapshot.write_bytes(next(iter(self.remote.values()))[4:])
        with sqlite3.connect(snapshot) as db:
            self.assertEqual(db.execute('SELECT value FROM maintenance_records').fetchone(), ('committed-wal',))
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertEqual((self.credentials / 'rclone-config').read_text(), 'RCLONE_ENCRYPT_V0:\nfixture-encrypted\n')
        for command in self.calls:
            self.assertNotIn('token', ' '.join(command))

    def test_download_corruption_retains_failed_evidence_and_cleans_plaintext(self):
        def corrupt(args):
            self.runner(args)
            if args[0] == 'rclone' and args[-2].startswith('gdrive:'):
                Path(args[-1]).write_bytes(b'broken')
        with self.assertRaises(ValueError):
            self.execute(corrupt)
        record = json.loads(next(self.evidence.iterdir()).read_text())
        self.assertEqual(record['status'], 'failure')
        self.assertEqual(record['stage'], 'download')
        self.assertTrue(record['remote_uploaded'])
        self.assertFalse(record['restore_verified'])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_provider_failure_does_not_leak_private_details_into_evidence(self):
        def fail(args):
            raise subprocess.CalledProcessError(1, args, stderr='private-token')
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute(fail)
        evidence = next(self.evidence.iterdir()).read_text()
        self.assertNotIn('private-token', evidence)
        self.assertIn('"status": "failure"', evidence)

    def test_unknown_oauth_table_is_rejected_without_external_call(self):
        self.connection.execute('CREATE TABLE oauth_tokens(token TEXT)')
        self.connection.commit()
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(self.calls, [])

    def test_source_symlink_rejected(self):
        link = self.base / 'linked'
        link.mkdir()
        self.database = link / 'car-care.sqlite3'
        self.database.symlink_to(self.base / 'car-care.sqlite3')
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(self.calls, [])

    def test_plaintext_credential_config_rejected(self):
        (self.credentials / 'rclone-config').write_text('[gdrive]\ntoken = private-token\n')
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(self.calls, [])

    def test_check_does_not_create_evidence_or_contact_remote(self):
        with patch.object(BACKUP.HELPERS, 'memory_backed', return_value=True), patch.object(BACKUP.shutil, 'which', return_value='/fixture/bin'):
            BACKUP.preflight(self.database, self.credentials, self.scratch, self.evidence)
        self.assertEqual(list(self.evidence.iterdir()), [])
        self.assertEqual(self.calls, [])

    def test_disk_scratch_is_rejected(self):
        with patch.object(BACKUP.HELPERS, 'memory_backed', return_value=False), self.assertRaises(ValueError):
            BACKUP.preflight(self.database, self.credentials, self.scratch, self.evidence)

    def test_public_evidence_directory_rejected(self):
        self.evidence.chmod(0o755)
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(self.calls, [])

    def test_plaintext_refresh_is_rejected_and_records_uncertain_upload(self):
        def refresh(args):
            self.runner(args)
            if args[0] == 'rclone':
                Path(args[args.index('--config') + 1]).write_text('[gdrive]\ntoken = must-not-persist\n')
        with self.assertRaises(ValueError):
            self.execute(refresh)
        record = json.loads(next(self.evidence.iterdir()).read_text())
        self.assertEqual(record['status'], 'failure')
        self.assertEqual(record['remote_upload_status'], 'uncertain')
        self.assertFalse(record['restore_verified'])
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertEqual((self.credentials / 'rclone-config').read_text(), 'RCLONE_ENCRYPT_V0:\nfixture-encrypted\n')
