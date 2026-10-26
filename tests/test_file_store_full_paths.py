"""Filesystem boundary and failure tests using isolated, disposable storage."""
import importlib
import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests._test_support import prepare_service_import


class FileStoreFullPathsTests(unittest.TestCase):
    def setUp(self):
        old_path = sys.path[:]
        old_modules = {k: v for k, v in sys.modules.items() if k == 'app' or k.startswith('app.')}
        def restore_imports():
            sys.path[:] = old_path
            for key in list(sys.modules):
                if key == 'app' or key.startswith('app.'):
                    del sys.modules[key]
            sys.modules.update(old_modules)
        self.addCleanup(restore_imports)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / 'files'
        environment = patch.dict(os.environ, {
            'FILE_STORAGE_PATH': str(self.root), 'FILE_ALLOWED_EXTENSIONS': 'txt, , .csv',
            'FILE_BLOCKED_EXTENSIONS': 'exe, , .sh',
            'SECURITY_LOG_PATH': str(Path(temporary.name) / 'events.jsonl'),
        })
        environment.start()
        self.addCleanup(environment.stop)
        prepare_service_import('portal-web')
        self.store = importlib.import_module('app.services.file_store')
        self.store.ensure_storage()
        audit = patch.object(self.store, 'append_security_event')
        self.audit = audit.start()
        self.addCleanup(audit.stop)

    def file(self, name='memo.txt', data=b'preserved'):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def upload(self, name='upload.txt', data=b'payload'):
        return SimpleNamespace(filename=name, file=io.BytesIO(data), content_type='text/plain')

    def archive(self, names):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            self.store.write_download_archive(archive, names)
        buffer.seek(0)
        return zipfile.ZipFile(buffer)

    def test_directory_listing_paths_names_and_sizes(self):
        self.file('folder/deep.txt')
        self.file('memo.txt')
        self.file('.hidden.txt')
        (self.root / 'link').symlink_to(self.root / 'memo.txt')
        os.mkfifo(self.root / 'pipe')
        listing = self.store.get_directory()
        self.assertEqual([x['name'] for x in listing['directories']], ['folder'])
        self.assertEqual([x['name'] for x in listing['files']], ['memo.txt'])
        self.assertEqual(listing['files'][0]['size'], 9)
        self.assertIsNone(listing['parent_path'])
        listing = self.store.get_directory('folder')
        self.assertEqual(listing['parent_path'], '')
        self.assertEqual(listing['breadcrumbs'], [{'name': 'folder', 'path': 'folder'}])
        with self.assertRaises(FileNotFoundError):
            self.store.get_directory('missing')
        with self.assertRaises(NotADirectoryError):
            self.store.get_directory('memo.txt')
        for value, expected in [(10, '10 B'), (1024, '1.0 KB'), (1024**5, '1024.0 TB')]:
            self.assertEqual(self.store.format_size(value), expected)
        self.assertEqual(self.store._safe_name(' folder/name.txt '), 'name.txt')
        self.assertEqual(self.store._safe_name('..'), '')
        with self.assertRaises(ValueError):
            self.store._safe_name('.hidden')

    def test_upload_success_validation_and_existing_data(self):
        self.assertEqual(self.store.save_upload('', self.upload()), 7)
        self.assertEqual((self.root / 'upload.txt').read_bytes(), b'payload')
        self.audit.assert_called_once_with('file_uploaded', path='upload.txt', size=7, content_type='text/plain')
        with self.assertRaises(FileExistsError):
            self.store.save_upload('', self.upload(data=b'overwrite'))
        self.assertEqual((self.root / 'upload.txt').read_bytes(), b'payload')
        for name in ['', 'noextension', 'x.exe']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.save_upload('', self.upload(name))
        with patch.object(self.store, 'ALLOWED_EXTENSIONS', set()):
            with self.assertRaisesRegex(ValueError, '차단'):
                self.store._validate_upload_name('RUN.SH')
            self.store._validate_upload_name('photo.png')
        with self.assertRaises(NotADirectoryError):
            self.store.save_upload('upload.txt', self.upload())
        self.assertEqual(self.store.save_uploads('', [self.upload('a.csv'), self.upload('b.txt')]), 2)

    def test_upload_failures_remove_only_new_files(self):
        sentinel = self.file()
        with patch.object(self.store, 'MAX_UPLOAD_BYTES', 1):
            with self.assertRaisesRegex(ValueError, '이하'):
                self.store.save_upload('', self.upload())
        self.assertFalse((self.root / 'upload.txt').exists())
        self.audit.side_effect = OSError('audit unavailable')
        with self.assertRaises(OSError):
            self.store.save_upload('', self.upload())
        self.assertFalse((self.root / 'upload.txt').exists())
        self.audit.side_effect = None
        with patch.object(self.store, 'MAX_UPLOAD_TOTAL_BYTES', 1):
            with self.assertRaises(ValueError):
                self.store.save_uploads('', [self.upload('a.txt')])
        self.assertFalse((self.root / 'a.txt').exists())
        with patch.object(self.store, 'MAX_UPLOAD_FILES', 0):
            for function in [lambda: self.store.save_uploads('', [self.upload()]),
                             lambda: self.store._save_uploads('', [self.upload()], [0])]:
                with self.assertRaises(ValueError):
                    function()
        self.assertEqual(sentinel.read_bytes(), b'preserved')

    def test_upload_read_failure_preserves_replacement_entries(self):
        for kind in ('file', 'directory', 'symlink'):
            with self.subTest(kind=kind):
                name = f'{kind}.txt'
                target = self.root / name
                moved = self.root / f'moved-{name}'
                sentinel = self.file(f'sentinel-{name}', b'valuable')
                stream = io.BytesIO(b'partial upload')

                def read(size):
                    chunk = stream.read(size)
                    if chunk:
                        return chunk
                    target.rename(moved)
                    if kind == 'file':
                        target.write_bytes(b'replacement')
                    elif kind == 'directory':
                        target.mkdir()
                        (target / 'keep.txt').write_bytes(b'replacement')
                    else:
                        target.symlink_to(sentinel)
                    raise OSError('stream failed')

                upload = self.upload(name)
                upload.file = SimpleNamespace(read=read)
                with self.assertRaisesRegex(OSError, 'stream failed'):
                    self.store.save_upload('', upload)
                self.assertEqual(moved.read_bytes(), b'partial upload')
                self.assertEqual(sentinel.read_bytes(), b'valuable')
                if kind == 'directory':
                    self.assertEqual((target / 'keep.txt').read_bytes(), b'replacement')
                else:
                    self.assertEqual(target.read_bytes(), b'valuable' if kind == 'symlink' else b'replacement')

    def test_upload_audit_failure_preserves_replacement_file(self):
        target = self.root / 'upload.txt'
        moved = self.root / 'moved.txt'

        def fail_audit(*args, **kwargs):
            target.rename(moved)
            target.write_bytes(b'replacement')
            raise OSError('audit failed')

        self.audit.side_effect = fail_audit
        with self.assertRaisesRegex(OSError, 'audit failed'):
            self.store.save_upload('', self.upload())
        self.assertEqual(target.read_bytes(), b'replacement')
        self.assertEqual(moved.read_bytes(), b'payload')

    def test_bulk_failure_preserves_prior_upload_replacement(self):
        for when in ('audit', 'next_read'):
            with self.subTest(when=when):
                first_name = f'{when}.txt'
                target = self.root / first_name
                moved = self.root / f'moved-{first_name}'

                def replace():
                    target.rename(moved)
                    target.write_bytes(b'valuable replacement')

                def audit(*args, **kwargs):
                    if when == 'audit':
                        replace()

                def fail_read(size):
                    if when == 'next_read':
                        replace()
                    raise OSError('second stream failed')

                self.audit.side_effect = audit
                second = self.upload(f'second-{when}.txt')
                second.file = SimpleNamespace(read=fail_read)
                with self.assertRaisesRegex(OSError, 'second stream failed'):
                    self.store.save_uploads('', [self.upload(first_name), second])
                self.assertEqual(target.read_bytes(), b'valuable replacement')
                self.assertEqual(moved.read_bytes(), b'payload')
                self.assertFalse((self.root / second.filename).exists())

    def test_upload_failure_uses_original_parent_directory(self):
        parent = self.root / 'folder'
        parent.mkdir()
        moved = self.root / 'moved-folder'

        def fail_read(size):
            parent.rename(moved)
            parent.mkdir()
            (parent / 'upload.txt').write_bytes(b'valuable replacement')
            raise OSError('stream failed')

        upload = self.upload()
        upload.file = SimpleNamespace(read=fail_read)
        with self.assertRaisesRegex(OSError, 'stream failed'):
            self.store.save_upload('folder', upload)
        self.assertEqual((parent / 'upload.txt').read_bytes(), b'valuable replacement')
        self.assertFalse((moved / 'upload.txt').exists())

    def test_upload_descriptors_close_after_success_and_failure(self):
        original_open = os.open
        descriptors = []

        def track_open(path, flags, *args, **kwargs):
            descriptor = original_open(path, flags, *args, **kwargs)
            if flags & os.O_CREAT:
                descriptors.append(descriptor)
            return descriptor

        with patch.object(self.store.os, 'open', side_effect=track_open):
            self.store.save_upload('', self.upload('one.txt'))
            self.store.save_uploads('', [self.upload('two.txt'), self.upload('three.txt')])
            with patch.object(self.store, 'MAX_UPLOAD_TOTAL_BYTES', 1):
                with self.assertRaises(ValueError):
                    self.store.save_uploads('', [self.upload('failed.txt')])
        self.assertEqual(len(descriptors), 4)
        for descriptor in set(descriptors):
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self.assertFalse((self.root / 'failed.txt').exists())

    def test_bulk_keeps_unlinked_upload_inode_open_until_rollback(self):
        original_open = os.open
        descriptors = []
        created = []

        def track_open(path, flags, *args, **kwargs):
            descriptor = original_open(path, flags, *args, **kwargs)
            if flags & os.O_CREAT:
                descriptors.append(descriptor)
            return descriptor

        def audit(*args, **kwargs):
            created.append(os.fstat(descriptors[0]))
            (self.root / 'first.txt').unlink()
            (self.root / 'first.txt').write_bytes(b'replacement')

        def fail_read(size):
            current = os.fstat(descriptors[0])
            self.assertEqual((current.st_dev, current.st_ino), (created[0].st_dev, created[0].st_ino))
            self.assertEqual(current.st_nlink, 0)
            self.assertNotEqual(descriptors[0], descriptors[1])
            raise OSError('second stream failed')

        self.audit.side_effect = audit
        second = self.upload('second.txt')
        second.file = SimpleNamespace(read=fail_read)
        with patch.object(self.store.os, 'open', side_effect=track_open):
            with self.assertRaisesRegex(OSError, 'second stream failed'):
                self.store.save_uploads('', [self.upload('first.txt'), second])
        self.assertEqual((self.root / 'first.txt').read_bytes(), b'replacement')
        self.assertFalse((self.root / 'second.txt').exists())
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_upload_wrapper_failures_cleanup_owned_file_and_close_descriptor(self):
        original_open = os.open
        original_fdopen = os.fdopen
        sentinel = self.file()
        for failure in ('construction', 'close'):
            with self.subTest(failure=failure):
                descriptors = []

                def track_open(path, flags, *args, **kwargs):
                    descriptor = original_open(path, flags, *args, **kwargs)
                    if flags & os.O_CREAT:
                        descriptors.append(descriptor)
                    return descriptor

                class CloseFailure:
                    def __init__(self, output):
                        self.output = output

                    def __enter__(self):
                        return self.output.__enter__()

                    def __exit__(self, *args):
                        self.output.__exit__(*args)
                        raise OSError('buffer close failed')

                def fdopen(*args, **kwargs):
                    if failure == 'construction':
                        raise OSError('buffer construction failed')
                    return CloseFailure(original_fdopen(*args, **kwargs))

                with patch.object(self.store.os, 'open', side_effect=track_open), patch.object(self.store.os, 'fdopen', side_effect=fdopen):
                    with self.assertRaisesRegex(OSError, f'buffer {failure} failed'):
                        self.store.save_upload('', self.upload())
                self.assertFalse((self.root / 'upload.txt').exists())
                self.assertEqual(sentinel.read_bytes(), b'preserved')
                self.audit.assert_not_called()
                self.assertEqual(len(descriptors), 1)
                with self.assertRaises(OSError):
                    os.fstat(descriptors[0])

    def test_storage_budget_counts_hidden_nested_files_and_rejects_unsafe_entries(self):
        self.file('.trash/nested.txt', b'abc')
        with self.store._upload_budget() as budget:
            self.assertEqual(budget, [3])
        with patch.object(self.store, '_QUOTA_SCAN_LIMIT', 0):
            with self.assertRaises(ValueError):
                with self.store._upload_budget():
                    pass
        os.mkfifo(self.root / 'pipe')
        with self.assertRaises(ValueError):
            with self.store._upload_budget():
                pass
        (self.root / 'pipe').unlink()
        with patch.object(self.store.os, 'listdir', side_effect=OSError('scan denied')):
            with self.assertRaisesRegex(ValueError, '저장 공간'):
                with self.store._upload_budget():
                    pass
        budget = [3]
        with patch.object(self.store, 'STORAGE_QUOTA_BYTES', 3):
            with self.assertRaisesRegex(ValueError, '전체 용량'):
                self.store._reserve_upload_bytes(budget, 1)
        with patch.object(self.store.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(ValueError, '여유 공간'):
                self.store._reserve_upload_bytes(budget, 1)
        self.assertEqual(budget, [3])

    def test_download_archives_contents_limits_and_timestamp(self):
        target = self.file('folder/sub/a.txt', b'abc')
        self.file('b.txt', b'def')
        os.utime(target, (0, 0))
        self.store.validate_download_limits(['folder', 'b.txt'])
        with self.archive(['folder', 'b.txt']) as archive:
            self.assertEqual(sorted(archive.namelist()), ['b.txt', 'folder/sub/a.txt'])
            self.assertEqual(archive.read('folder/sub/a.txt'), b'abc')
            self.assertEqual(archive.getinfo('folder/sub/a.txt').date_time, (1980, 1, 1, 0, 0, 0))
        self.assertEqual(self.store.get_download_path('b.txt'), self.root / 'b.txt')
        for name in ['missing', 'folder']:
            with self.assertRaises(FileNotFoundError):
                self.store.get_download_path(name)
        with self.assertRaises(FileNotFoundError):
            self.store.get_download_item_path('missing')
        for setting in ['MAX_DOWNLOAD_FILES', 'MAX_DOWNLOAD_TOTAL_BYTES']:
            with patch.object(self.store, setting, 0):
                with self.assertRaises(ValueError):
                    self.store.validate_download_limits(['b.txt'])
                with self.assertRaises(ValueError):
                    self.archive(['b.txt'])
        # A writer can grow a file after fstat: enforce the byte budget while streaming too.
        real_fstat = self.store.os.fstat
        def stale_size(fd):
            info = real_fstat(fd)
            return SimpleNamespace(st_mode=info.st_mode, st_size=0, st_mtime=info.st_mtime)
        with patch.object(self.store.os, 'fstat', side_effect=stale_size), patch.object(self.store, 'MAX_DOWNLOAD_TOTAL_BYTES', 1):
            with self.assertRaises(ValueError):
                self.archive(['b.txt'])
        self.assertEqual((self.root / 'b.txt').read_bytes(), b'def')

    def test_delete_preflight_nested_cleanup_and_audit_failure(self):
        self.file('folder/sub/a.txt')
        self.file('folder/b.txt')
        sentinel = self.file()
        self.assertEqual(self.store.validate_delete_items(['folder', 'folder']), ['folder'])
        self.assertEqual(self.store.validate_delete_items(['memo.txt']), ['memo.txt'])
        for names in [[], ['/'], ['.'], ['folder', 'folder/sub/a.txt']]:
            with self.subTest(names=names), self.assertRaises(ValueError):
                self.store.validate_delete_items(names)
        self.store.delete_item('folder')
        self.assertFalse((self.root / 'folder').exists())
        self.audit.assert_called_with('file_deleted', path='folder', item_type='folder')
        for name, error in [('', ValueError), ('missing', FileNotFoundError)]:
            with self.assertRaises(error):
                self.store.delete_item(name)
        self.audit.side_effect = OSError('audit unavailable')
        with self.assertRaises(self.store.DeletionAuditError):
            self.store.delete_item('memo.txt')
        self.assertFalse(sentinel.exists())
        self.audit.side_effect = None
        self.store.create_directory('', 'new')
        self.assertTrue((self.root / 'new').is_dir())
        with self.assertRaises(ValueError):
            self.store.create_directory('', '')

    def test_special_files_and_symlinks_preserve_unrelated_data(self):
        sentinel = self.file()
        self.file('folder/keep.txt')
        os.mkfifo(self.root / 'folder/pipe')
        for function in [lambda: self.store.validate_delete_items(['folder']),
                         lambda: self.store.delete_item('folder')]:
            with self.assertRaises(ValueError):
                function()
            self.assertEqual((self.root / 'folder/keep.txt').read_bytes(), b'preserved')
            self.assertTrue((self.root / 'folder/pipe').exists())
        with self.store._open_storage_item('folder') as fd:
            with self.assertRaises(ValueError):
                self.store._delete_directory_fd(fd)
        os.mkfifo(self.root / 'pipe')
        for function in [lambda: self.store.validate_delete_items(['pipe']),
                         lambda: self.store.delete_item('pipe'), lambda: self.archive(['pipe'])]:
            with self.assertRaises(ValueError):
                function()
        (self.root / 'link').symlink_to(sentinel)
        with self.assertRaises(ValueError):
            self.store._safe_path('link')
        with self.store._open_storage_item('') as fd:
            with self.assertRaises(ValueError):
                self.store._open_child_fd(fd, 'link')
            with self.assertRaises(FileNotFoundError):
                self.store._open_child_fd(fd, 'absent')
        (self.root / 'folder/link').symlink_to(sentinel)
        with self.assertRaises(ValueError):
            list(self.store.iter_download_files(self.root / 'folder'))
        for name in ['../escape', '.hidden']:
            with self.assertRaises(ValueError):
                self.store._safe_path(name)
            with self.assertRaises(ValueError):
                with self.store._open_storage_item(name):
                    pass
        self.assertEqual(sentinel.read_bytes(), b'preserved')

    def test_upload_read_error_after_external_removal_keeps_other_data(self):
        sentinel = self.file()
        upload = self.upload()
        def removed_during_read(_size):
            (self.root / 'upload.txt').unlink()
            raise OSError('file removed while receiving upload')
        upload.file.read = removed_during_read
        with self.assertRaisesRegex(OSError, 'removed'):
            self.store.save_upload('', upload)
        self.assertFalse((self.root / 'upload.txt').exists())
        self.assertEqual(sentinel.read_bytes(), b'preserved')
        self.audit.assert_not_called()

    def test_path_resolution_rechecks_containment_after_symlink_race(self):
        outside = self.root.parent / 'outside'
        outside.mkdir()
        sentinel = outside / 'keep.txt'
        sentinel.write_bytes(b'outside preserved')
        candidate = self.root / 'raced'
        candidate.mkdir()
        original = Path.is_symlink
        def replace_after_check(path):
            result = original(path)
            if path == candidate:
                candidate.rmdir()
                candidate.symlink_to(outside, target_is_directory=True)
            return result
        with patch.object(Path, 'is_symlink', replace_after_check):
            with self.assertRaisesRegex(ValueError, '허용되지 않는 경로'):
                self.store._safe_path('raced')
        self.assertTrue(candidate.is_symlink())
        self.assertEqual(sentinel.read_bytes(), b'outside preserved')
