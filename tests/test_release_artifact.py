"""Committed Book release payloads and hostile artifact preflight."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_artifact():
    spec = importlib.util.spec_from_file_location('release_artifact', ROOT / 'book-memo/tools/release_artifact.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'user.name', 'Fixture')
        for name, content in {'app/main.py': 'COMMITTED = True\n', 'Dockerfile': 'FROM scratch\n', 'requirements.txt': 'example==1\n', '.env': 'DO_NOT_PACKAGE=secret\n'}.items():
            path = self.repo / 'book-memo' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')
        self.commit = self.git('rev-parse', 'HEAD').strip()
        self.owned = self.root / 'owned'
        self.owned.mkdir()
        (self.owned / '.release-lab-owner').write_text('test-owner')
        self.module = load_artifact()
        self.repo_patch = patch.object(self.module, 'ROOT', self.repo)
        self.repo_patch.start()
        self.addCleanup(self.repo_patch.stop)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True)

    def build(self, name='baseline'):
        destination = self.owned / name
        descriptor = self.module.build_release(destination, self.commit, name, '/ready')
        return destination, descriptor

    def test_committed_payload_and_exact_descriptor(self):
        (self.repo / 'book-memo/app/main.py').write_text('DIRTY = True\n')
        (self.repo / 'book-memo/app/untracked.py').write_text('untracked')
        destination, descriptor = self.build()
        self.assertEqual((destination / 'app/main.py').read_text(), 'COMMITTED = True\n')
        self.assertEqual(set(descriptor), {'schema_version', 'service', 'source_commit', 'release_id', 'readiness_path', 'files', 'payload_digest', 'release_digest'})
        self.assertEqual(set(descriptor['files']), {'app/main.py', 'Dockerfile', 'requirements.txt'})
        self.assertEqual(self.module.verify_release(destination, descriptor), descriptor)
        self.assertNotIn('DO_NOT_PACKAGE', (destination / 'release.json').read_text())

    def test_tracked_gitkeep_is_packaged(self):
        marker = self.repo / 'book-memo/app/data/logs/.gitkeep'
        marker.parent.mkdir(parents=True)
        marker.write_text('')
        self.git('add', '.')
        self.git('commit', '-qm', 'tracked placeholder')
        self.commit = self.git('rev-parse', 'HEAD').strip()
        destination, descriptor = self.build()
        self.assertIn('app/data/logs/.gitkeep', descriptor['files'])
        self.assertEqual(self.module.verify_release(destination, descriptor), descriptor)

    def test_configuration_has_distinct_release_digest_same_payload(self):
        _, baseline = self.build()
        _, candidate = self.build('candidate')
        self.assertEqual(baseline['payload_digest'], candidate['payload_digest'])
        self.assertNotEqual(baseline['release_digest'], candidate['release_digest'])

    def test_tamper_missing_extra_and_link_rejected(self):
        for operation in ('tamper', 'missing', 'extra', 'symlink', 'hardlink', 'directory'):
            with self.subTest(operation=operation):
                destination, descriptor = self.build()
                target = destination / 'app/main.py'
                if operation == 'tamper':
                    target.write_text('tampered')
                elif operation == 'missing':
                    target.unlink()
                elif operation == 'extra':
                    (destination / 'extra').write_text('x')
                elif operation == 'directory':
                    (destination / 'unexpected').mkdir()
                else:
                    other = destination / 'Dockerfile'
                    target.unlink()
                    if operation == 'symlink':
                        target.symlink_to(other)
                    else:
                        target.hardlink_to(other)
                with self.assertRaisesRegex(ValueError, '^Invalid release artifact$'):
                    self.module.verify_release(destination, descriptor)
                import shutil
                shutil.rmtree(destination)

    def test_expected_descriptor_and_manifest_must_match(self):
        destination, descriptor = self.build()
        altered = dict(descriptor, release_id='candidate')
        with self.assertRaises(ValueError):
            self.module.verify_release(destination, altered)
        (destination / 'release.json').write_text(json.dumps(altered))
        with self.assertRaises(ValueError):
            self.module.verify_release(destination, altered)

    def test_invalid_inputs_do_not_create_output(self):
        for commit, release_id, readiness in [('HEAD', 'baseline', '/ready'), ('a'*39, 'baseline', '/ready'), (self.commit, '../bad', '/ready'), (self.commit, 'baseline', '/health')]:
            with self.subTest(commit=commit, release_id=release_id, readiness=readiness):
                with self.assertRaises(ValueError):
                    self.module.build_release(self.owned / 'output', commit, release_id, readiness)
                self.assertFalse((self.owned / 'output').exists())

    def test_destination_preserved_and_owner_required(self):
        destination, _ = self.build()
        with self.assertRaises(ValueError):
            self.build()
        self.assertTrue((destination / 'app/main.py').is_file())
        (self.owned / '.release-lab-owner').unlink()
        with self.assertRaises(ValueError):
            self.module.build_release(self.owned / 'candidate', self.commit, 'candidate', '/ready')

    def test_ambient_git_environment_cannot_select_foreign_repository(self):
        foreign = self.root / 'foreign'
        foreign.mkdir()
        original_repo = self.repo
        self.repo = foreign
        try:
            self.git('init', '-q')
            self.git('config', 'user.email', 'fixture@example.invalid')
            self.git('config', 'user.name', 'Fixture')
            for name in ('app/main.py', 'Dockerfile', 'requirements.txt'):
                target = foreign / 'book-memo' / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('FOREIGN_SOURCE')
            self.git('add', '.')
            self.git('commit', '-qm', 'foreign')
            foreign_commit = self.git('rev-parse', 'HEAD').strip()
        finally:
            self.repo = original_repo
        with patch.dict(os.environ, {'GIT_DIR': str(foreign / '.git'), 'GIT_WORK_TREE': str(foreign)}):
            with self.assertRaisesRegex(ValueError, '^Invalid release artifact$'):
                self.module.build_release(self.owned / 'baseline', foreign_commit, 'baseline', '/ready')
        self.assertFalse((self.owned / 'baseline').exists())

    def test_git_environment_excludes_ambient_configuration(self):
        with patch.dict(os.environ, {'GIT_CONFIG_COUNT': 'invalid', 'GIT_CONFIG_GLOBAL': '/untrusted/config', 'HOME': '/untrusted/home', 'HTTPS_PROXY': 'untrusted'}):
            destination, descriptor = self.build()
            environment = self.module.git_environment()
        self.assertEqual(set(environment), {'PATH', 'GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM', 'GIT_TERMINAL_PROMPT', 'LC_ALL'})
        self.assertEqual(environment['PATH'], os.defpath)
        self.assertEqual(self.module.verify_release(destination, descriptor), descriptor)

    def test_deadline_and_interrupt_prevent_creation(self):
        for options in ({'deadline': time.monotonic() - 1}, {'deadline': float('nan')}, {'deadline': float('inf')}, {'deadline': 'invalid'}, {'interrupted': lambda: True}):
            with self.subTest(options=options):
                with patch.object(self.module.subprocess, 'Popen') as popen:
                    with self.assertRaisesRegex(ValueError, '^Invalid release artifact$'):
                        self.module.build_release(self.owned / 'baseline', self.commit, 'baseline', '/ready', **options)
                    popen.assert_not_called()
                self.assertFalse((self.owned / 'baseline').exists())

    def test_interrupt_reaps_real_git_process(self):
        original = self.module.subprocess.Popen
        processes = []
        def capture(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        def stop_after_start():
            return bool(processes)
        with patch.object(self.module.subprocess, 'Popen', side_effect=capture):
            with self.assertRaisesRegex(ValueError, '^Invalid release artifact$'):
                self.module.build_release(self.owned / 'baseline', self.commit, 'baseline', '/ready', interrupted=stop_after_start)
        self.assertTrue(processes)
        self.assertTrue(all(process.poll() is not None for process in processes))
        self.assertFalse((self.owned / 'baseline').exists())

    def test_verify_honors_budget_and_interrupt(self):
        destination, descriptor = self.build()
        for options in ({'deadline': time.monotonic() - 1}, {'interrupted': lambda: True}):
            with self.assertRaisesRegex(ValueError, '^Invalid release artifact$'):
                self.module.verify_release(destination, descriptor, **options)
        self.assertEqual(self.module.verify_release(destination, descriptor), descriptor)

    def test_stale_commit_and_symlink_owner_rejected(self):
        with self.assertRaises(ValueError):
            self.module.build_release(self.owned / 'baseline', '0' * 40, 'baseline', '/ready')
        marker = self.owned / '.release-lab-owner'
        marker.unlink()
        other = self.root / 'other-marker'
        other.write_text('owner')
        marker.symlink_to(other)
        with self.assertRaises(ValueError):
            self.build()
        self.assertFalse((self.owned / 'baseline').exists())

    def test_git_failure_preserves_output_absence(self):
        with patch.object(self.module.subprocess, 'Popen', side_effect=OSError('private path')):
            with self.assertRaisesRegex(ValueError, '^Invalid release artifact$'):
                self.build()
        self.assertFalse((self.owned / 'baseline').exists())

    def test_symlink_parent_rejected(self):
        link = self.root / 'linked'
        link.symlink_to(self.owned, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.module.build_release(link / 'candidate', self.commit, 'candidate', '/ready')

    def archive(self, name, kind=tarfile.REGTYPE, size=1):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as archive:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = size if kind == tarfile.REGTYPE else 0
            member.linkname = 'book-memo/app/main.py'
            archive.addfile(member, io.BytesIO(b'x' * member.size))
        return stream.getvalue()

    def test_malicious_tar_rejected_before_output(self):
        for name, kind in [('book-memo/app/../../escape', tarfile.REGTYPE), ('/book-memo/app/x', tarfile.REGTYPE), ('book-memo/.env', tarfile.REGTYPE), ('book-memo/app/x', tarfile.SYMTYPE), ('book-memo/app/x', tarfile.LNKTYPE), ('book-memo/app/x', tarfile.CHRTYPE), ('book-memo/app/.env', tarfile.REGTYPE), ('book-memo/app/bad name', tarfile.REGTYPE)]:
            with self.subTest(name=name, kind=kind):
                with patch.object(self.module, '_git_archive', return_value=self.archive(name, kind)):
                    with self.assertRaises(ValueError):
                        self.build()
                self.assertFalse((self.owned / 'baseline').exists())

    def test_payload_and_archive_size_caps(self):
        for archive in (b'x' * (8 * 1024 * 1024 + 1), self.archive('book-memo/app/large', size=4 * 1024 * 1024 + 1)):
            with patch.object(self.module, '_git_archive', return_value=archive):
                with self.assertRaises(ValueError):
                    self.build()
            self.assertFalse((self.owned / 'baseline').exists())

    def test_file_count_and_duplicates_rejected(self):
        for duplicate in (False, True):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode='w') as archive:
                for index in range(257 if not duplicate else 2):
                    member = tarfile.TarInfo('book-memo/app/' + ('same' if duplicate else str(index)))
                    archive.addfile(member)
            with patch.object(self.module, '_git_archive', return_value=stream.getvalue()):
                with self.assertRaises(ValueError):
                    self.build()


if __name__ == '__main__':
    unittest.main()
