"""Offline Portal attachment export preserves active files and legacy trash."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'infra/k8s'))
from ha_runtime.config import DatabaseUnavailable
from ha_runtime.legacy_portal_export import export_portal_files, export_portal_state
from ha_runtime.seven_capture import auth_sessions


class LegacyPortalExportTests(unittest.TestCase):
    data = b'archived file contents'
    pin = hashlib.sha256(data).hexdigest()
    token = 'a' * 32

    def setUp(self):
        storage = tempfile.TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        self.destination = Path(storage.name).resolve() / 'portal-copy'
        folder = {'type': 'folder', 'modified_at': 1_700_000_000.5}
        file = {'type': 'file', 'modified_at': 1_700_000_001.0, 'size': len(self.data),
                'sha256': self.pin, 'content_type': 'text/plain'}
        self.snapshot = {
            'format': 1, 'version': 4,
            'entries': {'메모': folder, '메모/note.txt': file},
            'trash': {self.token: {'path': 'old/subfolder', 'trashed_at': '2026-11-20T00:00:00+00:00',
                                   'entries': {'old/subfolder': folder, 'old/subfolder/removed.txt': file}}},
            'confirmations': {}, 'audit': [{'event': 'trash', 'version': 4}],
        }

    def read_blob(self, digest, length):
        self.assertEqual((digest, length), (self.pin, len(self.data)))
        return self.data

    def test_restores_legacy_file_tree_and_preserves_nonlegacy_namespace_history(self):
        result = export_portal_files(self.snapshot, self.read_blob, self.destination)
        self.assertEqual((result['files'], result['trash_entries']), (2, 1))
        tree = self.destination / 'files'
        active = tree / '메모' / 'note.txt'
        self.assertEqual(active.read_bytes(), self.data)
        self.assertEqual(active.stat().st_mtime, self.snapshot['entries']['메모/note.txt']['modified_at'])
        trash = tree / '.trash' / self.token
        self.assertEqual((trash / 'item' / 'removed.txt').read_bytes(), self.data)
        self.assertEqual(json.loads((trash / 'metadata.json').read_bytes()),
                         {'path': 'old/subfolder', 'trashed_at': '2026-11-20T00:00:00+00:00'})
        self.assertEqual(json.loads((self.destination / 'file-namespace.json').read_bytes()), self.snapshot)
        self.assertEqual(json.loads((self.destination / 'portal-export-receipt.json').read_bytes()), result)
        with self.assertRaises(FileExistsError):
            export_portal_files(self.snapshot, self.read_blob, self.destination)

    def test_refuses_missing_parent_and_outside_trash_entry_before_writing(self):
        file = self.snapshot['entries']['메모/note.txt']
        self.snapshot['trash'][self.token]['entries']['elsewhere.txt'] = file
        with self.assertRaises(DatabaseUnavailable):
            export_portal_files(self.snapshot, self.read_blob, self.destination)
        self.assertFalse(self.destination.exists())
        self.snapshot['trash'][self.token]['entries'].pop('elsewhere.txt')
        self.snapshot['entries'].pop('메모')
        with self.assertRaises(DatabaseUnavailable):
            export_portal_files(self.snapshot, self.read_blob, self.destination)
        self.assertFalse(self.destination.exists())

    def test_rejects_changed_blob_without_publication_receipt(self):
        with self.assertRaises(DatabaseUnavailable):
            export_portal_files(self.snapshot, lambda *_: b'x' * len(self.data), self.destination)
        self.assertFalse((self.destination / 'portal-export-receipt.json').exists())

    def test_restores_hashed_sessions_failures_and_portfolio_in_legacy_formats(self):
        snapshot = {
            'auth_sessions': {'sessions': {self.pin: {'scope': 'viewer', 'payload_hash': self.pin,
                                                       'expires_at': 1_700_000_000.75}}, 'confirmations': {}},
            'auth_failures': {'files': {'synthetic': ['2026-11-20T00:00:00+00:00']}},
            'portfolio': {'content': '# 포트폴리오\n'},
        }
        result = export_portal_state(snapshot, self.destination)
        self.assertEqual(result['sessions'], 1)
        self.assertEqual(auth_sessions(self.destination / 'auth-sessions.sqlite3'), snapshot['auth_sessions'])
        self.assertEqual(json.loads((self.destination / 'auth-failures.json').read_text()), snapshot['auth_failures'])
        self.assertEqual((self.destination / 'portfolio.md').read_text(), snapshot['portfolio']['content'])
        self.assertEqual(json.loads((self.destination / 'portal-state-export-receipt.json').read_text()), result)

    def test_rejects_private_parent_inside_an_untrusted_writable_ancestor(self):
        ancestor = self.destination.parent / 'unsafe'
        ancestor.mkdir()
        ancestor.chmod(0o777)
        private = ancestor / 'private'
        private.mkdir(mode=0o700)
        with self.assertRaises(DatabaseUnavailable):
            export_portal_files(self.snapshot, self.read_blob, private / 'files')
        self.assertEqual(list(private.iterdir()), [])

    def test_rejects_trash_metadata_longer_than_legacy_reader_can_parse(self):
        long_path = '/'.join(['x' * 200] * 50)
        record = self.snapshot['trash'][self.token]
        record['path'] = long_path
        record['entries'] = {long_path: self.snapshot['entries']['메모']}
        with self.assertRaises(DatabaseUnavailable):
            export_portal_files(self.snapshot, self.read_blob, self.destination)
        self.assertFalse(self.destination.exists())
