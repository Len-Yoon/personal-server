"""Isolated restore checks: never exercise a real provider or production mount."""
import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
import sqlite3
import subprocess
import sys
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
        self.config_work = self.root / 'memory'
        self.config_work.mkdir()
        self.memory_patch = patch.object(self.mod, 'memory_backed', return_value=True)
        self.memory_patch.start()
        self.addCleanup(self.memory_patch.stop)
        self.creds = self.root / 'credentials'
        self.creds.mkdir()
        for name in ('rclone-config', 'rclone-config-passphrase', 'age-identity'):
            (self.creds / name).write_text('PRIVATE-CREDENTIAL')
        (self.creds / 'rclone-config').write_bytes(b'# Encrypted rclone configuration File\n\nRCLONE_ENCRYPT_V0:\ndummy-encrypted-fixture')
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
        return self.mod.check_restore(self.evidence, self.work, self.creds, config_work_dir=self.config_work, now=self.now, runner=self.provider)

    def test_success_downloads_only_and_preserves_authoritative_inputs(self):
        before = self.evidence.read_bytes()
        original_config = (self.creds / 'rclone-config').read_bytes()
        self.run_check()
        self.assertEqual(self.evidence.read_bytes(), before)
        self.assertEqual((self.creds / 'rclone-config').read_bytes(), original_config)
        self.assertEqual(list(self.config_work.iterdir()), [])
        self.assertEqual(list(self.work.iterdir()), [])
        command, kwargs = self.calls[0]
        self.assertEqual(command[-2], 'gdrive:PersonalServer-encrypted-backups/' + self.values['backup_id'] + '.tar.age')
        self.assertIn('--password-command', command)
        self.assertNotIn('PRIVATE-CREDENTIAL', repr(self.calls))
        self.assertEqual(kwargs['env'].keys(), {'PATH', 'HOME', 'LANG'})
        self.assertTrue(all('copyto' in c or c[0] == 'age' for c, _ in self.calls))

    def test_single_file_copy_avoids_size_filter_and_keeps_hard_limits(self):
        # Exact v1.60.1 single-file setup rejects any active size filter.
        # The N100 local fixture reproduces that fatal path independently.
        def compatible_provider(command, **kwargs):
            if command[0] == 'rclone' and '--max-size' in command:
                raise RuntimeError("can't limit to single files when using filters")
            self.provider(command, **kwargs)
        self.mod.check_restore(self.evidence, self.work, self.creds,
                               config_work_dir=self.config_work, now=self.now,
                               runner=compatible_provider)
        command, options = self.calls[0]
        self.assertNotIn('--max-size', command)
        self.assertEqual(command[command.index('--max-transfer') + 1],
                         str(self.mod.MAX_EXTRACT_BYTES + self.mod.MAX_MEMBERS * 1024))
        self.assertEqual(command[command.index('--cutoff-mode') + 1], 'hard')
        self.assertEqual(command[command.index('--buffer-size') + 1], '0')
        self.assertEqual(command[command.index('--multi-thread-streams') + 1], '0')
        self.assertIs(options['preexec_fn'], self.mod.limit_download_file_size)
        self.assertNotIn('preexec_fn', self.calls[1][1])

    def test_download_budget_failure_preserves_inputs_and_never_decrypts(self):
        before_evidence = self.evidence.read_bytes()
        before_credentials = {path.name: path.read_bytes() for path in self.creds.iterdir()}
        calls = []
        def limited_provider(command, **kwargs):
            calls.append(command)
            self.assertIs(kwargs['preexec_fn'], self.mod.limit_download_file_size)
            Path(command[-1]).write_bytes(b'partial-cipher')
            raise subprocess.CalledProcessError(1, command)
        with self.assertRaises(subprocess.CalledProcessError):
            self.mod.check_restore(self.evidence, self.work, self.creds,
                                   config_work_dir=self.config_work, now=self.now,
                                   runner=limited_provider)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'rclone')
        self.assertEqual(self.evidence.read_bytes(), before_evidence)
        self.assertEqual({path.name: path.read_bytes() for path in self.creds.iterdir()}, before_credentials)
        self.assertEqual(list(self.work.iterdir()), [])
        self.assertEqual(list(self.config_work.iterdir()), [])

    def test_child_file_limit_preserves_stricter_inherited_soft_and_hard_limits(self):
        unlimited = self.mod.resource.RLIM_INFINITY
        cap = self.mod.MAX_DOWNLOAD_BYTES
        for inherited, expected in [((unlimited, unlimited), (cap, cap)),
                ((cap + 1, cap + 2), (cap, cap)), ((1024, unlimited), (1024, cap)),
                ((1024, 2048), (1024, 2048)), ((0, 0), (0, 0))]:
            with self.subTest(inherited=inherited):
                with patch.object(self.mod.resource, 'getrlimit', return_value=inherited), \
                     patch.object(self.mod.resource, 'setrlimit') as setter:
                    self.mod.limit_download_file_size()
                setter.assert_called_once_with(self.mod.resource.RLIMIT_FSIZE, expected)

    @unittest.skipUnless(sys.platform.startswith(('linux', 'darwin')), 'POSIX child file limits')
    def test_real_child_file_budget_accepts_equal_and_rejects_over_limit(self):
        parent_limit = self.mod.resource.getrlimit(self.mod.resource.RLIMIT_FSIZE)
        script = ('import pathlib,sys; p=pathlib.Path(sys.argv[1]); '
                  'p.write_bytes(b"x"*int(sys.argv[2]))')
        for size in (1024, 1025):
            with self.subTest(size=size):
                destination = self.root / ('bounded-' + str(size))
                with patch.object(self.mod, 'MAX_DOWNLOAD_BYTES', 1024):
                    result = subprocess.run([sys.executable, '-c', script, str(destination), str(size)],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=10,
                        preexec_fn=self.mod.limit_download_file_size)
                self.assertEqual(result.returncode == 0, size == 1024)
                self.assertLessEqual(destination.stat().st_size, 1024)
                self.assertEqual(self.mod.resource.getrlimit(self.mod.resource.RLIMIT_FSIZE), parent_limit)

    @unittest.skipUnless(sys.platform.startswith(('linux', 'darwin')), 'POSIX child file limits')
    def test_real_child_keeps_stricter_inherited_file_limit(self):
        parent_limit = self.mod.resource.getrlimit(self.mod.resource.RLIMIT_FSIZE)
        destination = self.root / 'stricter-budget'
        def child_setup():
            self.mod.resource.setrlimit(self.mod.resource.RLIMIT_FSIZE, (512, 512))
            self.mod.limit_download_file_size()
        result = subprocess.run([sys.executable, '-c',
                'import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(b"x"*513)', str(destination)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, preexec_fn=child_setup)
        self.assertNotEqual(result.returncode, 0)
        self.assertLessEqual(destination.stat().st_size, 512)
        self.assertEqual(self.mod.resource.getrlimit(self.mod.resource.RLIMIT_FSIZE), parent_limit)

    def test_encrypted_config_refresh_is_memory_only_and_preserves_source(self):
        original = (self.creds / 'rclone-config').read_bytes()
        def refresh(command, **kwargs):
            if command[0] == 'rclone':
                config = Path(command[command.index('--config') + 1])
                self.assertTrue(config.is_relative_to(self.config_work.resolve()))
                self.assertEqual(config.stat().st_mode & 0o777, 0o600)
                self.assertEqual(config.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(config.read_bytes(), original)
                self.assertIn(str(self.creds / 'rclone-config-passphrase'), command[command.index('--password-command') + 1])
                config.write_bytes(b'RCLONE_ENCRYPT_V0:\nrefreshed-encrypted-fixture')
            self.provider(command, **kwargs)
        self.mod.check_restore(self.evidence, self.work, self.creds,
                               config_work_dir=self.config_work, now=self.now, runner=refresh)
        self.assertEqual((self.creds / 'rclone-config').read_bytes(), original)
        self.assertEqual(list(self.config_work.iterdir()), [])

    def test_plaintext_or_large_config_blocks_provider(self):
        for raw in (b'[gdrive]\ntype=drive', b'RCLONE_ENCRYPT_V0:\n' + b'x' * self.mod.MAX_CONFIG_BYTES):
            with self.subTest(size=len(raw)):
                (self.creds / 'rclone-config').write_bytes(raw)
                with self.assertRaises(self.mod.RestoreError):
                    self.run_check()
        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.config_work.iterdir()), [])

    def test_provider_plaintext_permission_or_symlink_rewrite_is_rejected(self):
        for kind in ('plaintext', 'permissions', 'symlink'):
            with self.subTest(kind=kind):
                def rewrite(command, **kwargs):
                    config = Path(command[command.index('--config') + 1])
                    if kind == 'plaintext':
                        config.write_bytes(b'[gdrive]\ntoken=PRIVATE-CREDENTIAL')
                    elif kind == 'permissions':
                        config.chmod(0o644)
                    else:
                        config.unlink()
                        config.symlink_to(self.creds / 'rclone-config')
                with self.assertRaises(self.mod.RestoreError):
                    self.mod.check_restore(self.evidence, self.work, self.creds,
                                           config_work_dir=self.config_work, now=self.now, runner=rewrite)
                self.assertEqual(list(self.config_work.iterdir()), [])
                self.assertEqual(list(self.work.iterdir()), [])

    def test_secret_transition_copies_validated_snapshot_then_fails_closed(self):
        original = (self.creds / 'rclone-config').read_bytes()
        read = self.mod.encrypted_config_snapshot
        transitioned = False
        def transition(path, **kwargs):
            nonlocal transitioned
            raw = read(path, **kwargs)
            if path == self.creds / 'rclone-config' and not transitioned:
                transitioned = True
                replacement = self.creds / 'next'; replacement.write_bytes(b'plaintext-second-generation')
                replacement.replace(path)
            return raw
        copied = []
        def observe(command, **kwargs):
            copied.append(Path(command[command.index('--config') + 1]).read_bytes())
            self.provider(command, **kwargs)
        with patch.object(self.mod, 'encrypted_config_snapshot', side_effect=transition):
            with self.assertRaises(self.mod.RestoreError):
                self.mod.check_restore(self.evidence, self.work, self.creds,
                                       config_work_dir=self.config_work, now=self.now, runner=observe)
        self.assertEqual(copied, [original])
        self.assertEqual(list(self.config_work.iterdir()), [])

    def test_projected_secret_symlink_can_be_read(self):
        source = self.creds / 'rclone-config'
        target = self.creds / 'versioned-config'; source.replace(target); source.symlink_to(target.name)
        self.run_check()
        self.assertEqual(list(self.config_work.iterdir()), [])

    def test_non_memory_or_overlapping_config_work_directory_blocks_provider(self):
        with patch.object(self.mod, 'memory_backed', return_value=False):
            with self.assertRaises(self.mod.RestoreError):self.run_check()
        for directory in (self.work, self.creds):
            with self.assertRaises(self.mod.RestoreError):
                self.mod.check_restore(self.evidence, self.work, self.creds,
                                       config_work_dir=directory, now=self.now, runner=self.provider)
        self.assertEqual(self.calls, [])

    def test_memory_mount_selection_rejects_nested_disk_and_parses_escapes(self):
        mountinfo = self.root / 'mountinfo'
        mountinfo.write_text('1 0 0:1 / / rw - ext4 root rw\n'
                            '2 1 0:2 / /run/rclone\\040state rw - tmpfs tmpfs rw\n'
                            '3 2 0:3 / /run/rclone\\040state/disk rw - ext4 disk rw\n')
        # Call the unpatched function from a fresh module, not the test fixture's stub.
        spec = importlib.util.spec_from_file_location('mount_check', SCRIPT)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        self.assertTrue(module.memory_backed(Path('/run/rclone state/private'), mountinfo))
        self.assertFalse(module.memory_backed(Path('/run/rclone state/disk/private'), mountinfo))
        self.assertFalse(module.memory_backed(Path('/work'), mountinfo))

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
        args = ['--evidence', str(self.evidence), '--work-dir', str(self.work), '--credential-dir', str(self.creds), '--config-work-dir', str(self.config_work)]
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
            self.mod.check_restore(self.evidence, self.work, self.creds, config_work_dir=self.config_work, now=self.now, runner=failed)
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
