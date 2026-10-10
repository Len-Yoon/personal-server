import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tests._test_support import prepare_service_import


class FileManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'APP_ENV':'development', 'FILE_MANAGER_AUTH_REQUIRED':'true', 'FILE_MANAGER_ACCESS_PASSWORD':'fixture-password', 'DELETE_PASSWORD':'fixture-delete', 'FILE_STORAGE_PATH':str(Path(self.temp.name)/'files'), 'AUTH_RATE_LIMIT_STATE_PATH':str(Path(self.temp.name)/'auth.json'), 'SECURITY_LOG_PATH':str(Path(self.temp.name)/'events.txt')})
        env.start()
        self.addCleanup(env.stop)
        prepare_service_import('portal-web')
        self.store = importlib.import_module('app.services.file_store')
        self.manage = importlib.import_module('app.services.file_management')
        self.root = Path(self.temp.name)/'files'
        self.root.mkdir()
        self.store.STORAGE_PATH = self.root
        (self.root/'memo.txt').write_text('preserved')
        (self.root/'folder').mkdir()

    def test_recursive_search_excludes_links_and_hidden_data(self):
        (self.root/'folder'/'deep.txt').write_text('inside')
        (self.root/'.private').mkdir()
        (self.root/'.private'/'deep-secret.txt').write_text('secret')
        outside = Path(self.temp.name)/'outside'
        outside.mkdir()
        (outside/'deep-other.txt').write_text('outside')
        (self.root/'link').symlink_to(outside)
        result = self.manage.search('deep')
        self.assertEqual([i['path'] for i in result['items']], ['folder/deep.txt'])

    def test_exclusive_move_and_rename_preserve_data(self):
        self.assertEqual(self.manage.move('memo.txt', 'folder', 'renamed.txt'), 'folder/renamed.txt')
        self.assertEqual((self.root/'folder/renamed.txt').read_text(), 'preserved')
        (self.root/'memo.txt').write_text('another')
        with self.assertRaises(FileExistsError):
            self.manage.move('memo.txt', 'folder', 'renamed.txt')
        self.assertEqual((self.root/'memo.txt').read_text(), 'another')

    def test_blocked_extension_and_descendant_moves_are_rejected(self):
        with self.assertRaises(ValueError):
            self.manage.move('memo.txt', '', 'run.sh')
        with self.assertRaises(ValueError):
            self.manage.move('folder', 'folder')
        for path in ['../memo.txt', '.trash/item', '/memo.txt']:
            with self.assertRaises(ValueError):
                self.manage.move(path)

    def test_trash_restore_conflict_does_not_overwrite(self):
        token = self.manage.trash('memo.txt')
        self.assertFalse((self.root/'memo.txt').exists())
        self.assertEqual(self.manage.list_trash()[0]['path'], 'memo.txt')
        (self.root/'memo.txt').write_text('new')
        with self.assertRaises(FileExistsError):
            self.manage.restore(token)
        self.manage.restore(token, 'restored.txt')
        self.assertEqual((self.root/'restored.txt').read_text(), 'preserved')
        self.assertEqual((self.root/'memo.txt').read_text(), 'new')
        self.assertEqual(self.manage.list_trash(), [])
        with self.assertRaises(FileNotFoundError):
            self.manage.restore(token)

    def test_trash_metadata_failure_preserves_source(self):
        with patch.object(self.manage.json, 'dump', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.manage.trash('memo.txt')
        self.assertEqual((self.root/'memo.txt').read_text(), 'preserved')
        self.assertEqual(self.manage.list_trash(), [])

    def test_hidden_trash_is_inaccessible_through_existing_apis(self):
        self.manage.trash('memo.txt')
        for call in (self.store.get_directory, self.store.get_download_item_path, self.store.delete_item):
            with self.assertRaises(ValueError):
                call('.trash')
        self.assertEqual(self.store.get_directory()['directories'][0]['name'], 'folder')

    def test_parent_symlink_replacement_does_not_escape(self):
        outside = Path(self.temp.name)/'outside'
        outside.mkdir()
        original = self.manage._exclusive_move
        def replace_parent(source_fd, name, destination_fd, new_name):
            (self.root/'folder').rename(self.root/'original-folder')
            (self.root/'folder').symlink_to(outside)
            original(source_fd, name, destination_fd, new_name)
        with patch.object(self.manage, '_exclusive_move', side_effect=replace_parent):
            self.manage.move('memo.txt', 'folder')
        self.assertFalse((outside/'memo.txt').exists())
        self.assertEqual((self.root/'original-folder/memo.txt').read_text(), 'preserved')

    def test_folder_roundtrip(self):
        (self.root/'folder/a.txt').write_text('a')
        token = self.manage.trash('folder')
        self.manage.restore(token)
        self.assertEqual((self.root/'folder/a.txt').read_text(), 'a')

    def test_post_move_sync_failure_reports_completed_token(self):
        with patch.object(self.manage.os, 'fsync', side_effect=[None, OSError('disk full')]):
            with self.assertRaises(self.manage.TrashCompletedError) as caught:
                self.manage.trash('memo.txt')
        self.assertFalse((self.root/'memo.txt').exists())
        self.assertEqual(self.manage.list_trash()[0]['id'], caught.exception.token)
        self.manage.restore(caught.exception.token)
        self.assertEqual((self.root/'memo.txt').read_text(), 'preserved')

    def test_malformed_trash_metadata_does_not_hide_valid_items(self):
        token = self.manage.trash('memo.txt')
        broken=self.root/'.trash'/('a'*32)
        broken.mkdir()
        (broken/'metadata.json').write_text('[]')
        (broken/'item').write_text('bad')
        self.assertEqual([i['id'] for i in self.manage.list_trash()], [token])
        with self.assertRaises(ValueError):
            self.manage.restore('a'*32)

    def test_file_tools_routes_require_auth_and_preserve_conflicted_restore(self):
        from fastapi.testclient import TestClient
        app=importlib.import_module('app.main').app
        with TestClient(app) as client:
            headers={'Origin':'http://testserver'}
            self.assertEqual(client.get('/files/tools').status_code,401)
            self.assertEqual(client.post('/files/move',data={'path':'memo.txt'},headers=headers).status_code,401)
            client.post('/files/login',data={'password':'fixture-password'},headers=headers)
            self.assertEqual(client.get('/files/tools?q=memo').status_code,200)
            self.assertEqual(client.post('/files/move',data={'path':'memo.txt','destination':'folder'},headers=headers).status_code,200)
            result=client.post('/files/trash',data={'paths':'folder/memo.txt','delete_password':'fixture-delete'},headers=headers)
            self.assertTrue(result.json()['ok'])
            token=result.json()['moved'][0]['id']
            (self.root/'folder/memo.txt').write_text('new')
            self.assertEqual(client.post('/files/restore',data={'item_id':token},headers=headers).status_code,409)
            self.assertEqual(client.post('/files/restore',data={'item_id':token,'name':'restored.txt'},headers=headers).status_code,200)
            self.assertEqual((self.root/'folder/restored.txt').read_text(),'preserved')

    def test_route_post_move_sync_failure_is_partial_success(self):
        from fastapi.testclient import TestClient
        app=importlib.import_module('app.main').app
        with TestClient(app) as client:
            headers={'Origin':'http://testserver'}
            client.post('/files/login',data={'password':'fixture-password'},headers=headers)
            with patch.object(self.manage.os,'fsync',side_effect=[None,OSError('full')]):
                response=client.post('/files/trash',data={'paths':'memo.txt','delete_password':'fixture-delete'},headers=headers)
            self.assertEqual(response.status_code,207)
            self.assertFalse(response.json()['ok'])
            self.assertEqual(response.json()['moved'][0]['path'],'memo.txt')
            self.assertEqual(response.json()['failed'],[])

    def test_trash_details_pagination_preview_confirmation_and_staleness(self):
        token = self.manage.trash('memo.txt')
        result = self.manage.trash_page(page=999, page_size=1)
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['size_bytes'], 9)
        self.assertGreaterEqual(result['items'][0]['age_days'], 0)
        preview = self.manage.preview_purge(item_id=token)
        self.assertEqual(preview['count'], 1)
        self.assertTrue((self.root/'.trash'/token/'item').exists())
        with self.assertRaises(ValueError):
            self.manage.confirm_purge(preview['confirmation'] + 'bad')
        (self.root/'.trash'/token/'item').write_text('changed')
        with self.assertRaises(ValueError):
            self.manage.confirm_purge(preview['confirmation'])
        fresh = self.manage.preview_purge(item_id=token)
        self.assertEqual(self.manage.confirm_purge(fresh['confirmation'])['deleted'], [token])
        self.assertFalse((self.root/'.trash'/token).exists())
        self.assertEqual(self.manage.list_trash(), [])

    def test_purge_age_cutoff_and_symlink_fail_closed(self):
        token = self.manage.trash('memo.txt')
        self.assertEqual(self.manage.preview_purge(older_than_days=30)['count'], 0)
        target = self.root/'.trash'/token
        outside = Path(self.temp.name)/'outside-secret'
        outside.write_text('preserved')
        (target/'item').unlink()
        (target/'item').symlink_to(outside)
        self.assertEqual(self.manage.list_trash(), [])
        with self.assertRaises((ValueError, OSError)):
            self.manage.preview_purge(item_id=token)
        self.assertEqual(outside.read_text(), 'preserved')
        for value in ('../memo.txt', '.trash', ''):
            with self.assertRaises(ValueError):
                self.manage.preview_purge(item_id=value)

    def test_purge_routes_require_auth_csrf_password_and_explicit_confirmation(self):
        from fastapi.testclient import TestClient
        token = self.manage.trash('memo.txt')
        with TestClient(importlib.import_module('app.main').app) as client:
            headers = {'Origin': 'http://testserver'}
            self.assertEqual(client.post('/files/trash/preview', data={'item_id': token}, headers=headers).status_code, 401)
            client.post('/files/login', data={'password': 'fixture-password'}, headers=headers)
            self.assertEqual(client.post('/files/trash/preview', data={'item_id': token}).status_code, 403)
            response = client.post('/files/trash/preview', data={'item_id': token}, headers=headers)
            self.assertEqual(response.status_code, 200)
            confirmation = self.manage.preview_purge(item_id=token)['confirmation']
            data = {'confirmation': confirmation, 'delete_password': 'fixture-delete'}
            self.assertEqual(client.post('/files/trash/purge', data=data, headers=headers).status_code, 400)
            data['confirm'] = '영구 삭제'
            data['delete_password'] = 'wrong'
            self.assertEqual(client.post('/files/trash/purge', data=data, headers=headers).status_code, 403)
            self.assertTrue((self.root/'.trash'/token/'item').exists())
            data['delete_password'] = 'fixture-delete'
            self.assertEqual(client.post('/files/trash/purge', data=data, headers=headers).status_code, 200)
            self.assertEqual(self.manage.list_trash(), [])

    def test_all_trash_pages_are_accessible_above_1000_entries(self):
        import json
        trash = self.root/'.trash'
        trash.mkdir()
        for i in range(1005):
            entry = trash/f'{i:032x}'
            entry.mkdir()
            (entry/'metadata.json').write_text(json.dumps({'path': f'{i}.txt', 'trashed_at': '2026-01-01T00:00:00+00:00'}))
            (entry/'item').write_text('x')
        result = self.manage.trash_page(page=21)
        self.assertEqual(result['total'], 1005)
        self.assertEqual(len(result['items']), 5)
        self.assertEqual(len(self.manage.list_trash()), 1005)
        self.assertEqual(self.manage.preview_purge(older_than_days=1)['count'], 1005)

    def test_purge_directory_size_expiration_and_nested_symlink(self):
        (self.root/'folder/a.txt').write_text('abc')
        (self.root/'folder/.hidden.txt').write_text('hidden')
        token = self.manage.trash('folder')
        self.assertEqual(self.manage.list_trash()[0]['size_bytes'], 9)
        preview = self.manage.preview_purge(item_id=token)
        with patch.object(self.manage.time, 'time', return_value=self.manage.time.time()+601):
            with self.assertRaises(ValueError):
                self.manage.confirm_purge(preview['confirmation'])
        target = self.root/'.trash'/token/'item'
        outside = Path(self.temp.name)/'outside'
        outside.mkdir()
        (outside/'private.txt').write_text('secret')
        (target/'link').symlink_to(outside)
        with self.assertRaises((ValueError, OSError)):
            self.manage.preview_purge(item_id=token)
        self.assertTrue((target/'a.txt').exists())
        (target/'link').unlink()
        preview = self.manage.preview_purge(item_id=token)
        self.assertTrue(self.manage.confirm_purge(preview['confirmation'])['ok'])
        self.assertEqual((outside/'private.txt').read_text(), 'secret')
        self.assertFalse(target.exists())

    def test_purge_partial_failure_is_not_reported_as_complete(self):
        token = self.manage.trash('memo.txt')
        preview = self.manage.preview_purge(item_id=token)
        with patch.object(self.manage.os, 'unlink', side_effect=OSError('cannot delete')):
            result = self.manage.confirm_purge(preview['confirmation'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['deleted'], [])
        self.assertEqual(result['failed'], [token])
        self.assertEqual((self.root/'.trash'/token/'item').read_text(), 'preserved')

    def test_trash_metadata_cannot_override_token_or_escape_purge(self):
        import json
        token = self.manage.trash('memo.txt')
        metadata = self.root/'.trash'/token/'metadata.json'
        value = json.loads(metadata.read_text())
        value.update({'id': '../../folder', 'size_bytes': 0})
        metadata.write_text(json.dumps(value))
        preview = self.manage.preview_purge(item_id=token)
        self.assertEqual(preview['items'][0]['id'], token)
        self.assertEqual(preview['size_bytes'], 9)
        self.assertTrue(self.manage.confirm_purge(preview['confirmation'])['ok'])
        self.assertTrue((self.root/'folder').is_dir())

    def test_hidden_metadata_path_is_never_listed_or_purged(self):
        import json
        token = self.manage.trash('memo.txt')
        metadata = self.root/'.trash'/token/'metadata.json'
        value = json.loads(metadata.read_text())
        value['path'] = '.private/secret.txt'
        metadata.write_text(json.dumps(value))
        self.assertEqual(self.manage.list_trash(), [])
        self.assertEqual(self.manage.preview_purge()['count'], 0)
        self.assertTrue((self.root/'.trash'/token/'item').exists())

    def test_purge_never_deletes_unrecognized_trash_siblings(self):
        token = self.manage.trash('memo.txt')
        sibling = self.root/'.trash'/token/'unrecognized'
        sibling.write_text('preserve')
        preview = self.manage.preview_purge(item_id=token)
        result = self.manage.confirm_purge(preview['confirmation'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['deleted'], [token])
        self.assertEqual(result['failed'], [])
        self.assertEqual(sibling.read_text(), 'preserve')

    def test_purge_replacement_symlink_cannot_follow_outside_trash(self):
        (self.root/'folder/a.txt').write_text('inside')
        token = self.manage.trash('folder')
        preview = self.manage.preview_purge(item_id=token)
        target = self.root/'.trash'/token/'item'
        outside = Path(self.temp.name)/'outside'
        outside.mkdir()
        (outside/'a.txt').write_text('outside')
        original = self.store._delete_directory_fd
        def replace_item(fd):
            target.rename(target.with_name('retained-item'))
            target.symlink_to(outside)
            original(fd)
        with patch.object(self.store, '_delete_directory_fd', side_effect=replace_item):
            result = self.manage.confirm_purge(preview['confirmation'])
        self.assertFalse(result['ok'])
        self.assertEqual((outside/'a.txt').read_text(), 'outside')
        self.assertTrue(target.is_symlink())

    def test_tree_snapshot_stops_enumeration_at_remaining_budget(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        visited = []
        @contextmanager
        def entries(_):
            def generate():
                for index in range(3):
                    visited.append(index)
                    if index == 2:
                        raise AssertionError('Enumeration continued beyond the budget')
                    yield SimpleNamespace(name=f'{index}.txt')
            yield generate()
        with self.store._open_storage_item('folder') as fd:
            with patch.object(self.manage.os, 'scandir', entries):
                with self.assertRaises(ValueError):
                    self.manage._tree_snapshot(fd, budget=[9998])
        self.assertEqual(visited, [0, 1])

    def test_tree_snapshot_keeps_normal_name_order_and_size(self):
        (self.root/'folder/b.txt').write_text('bb')
        (self.root/'folder/a.txt').write_text('a')
        with self.store._open_storage_item('folder') as fd:
            size, snapshot = self.manage._tree_snapshot(fd)
        self.assertEqual(size, 3)
        self.assertEqual([child[0] for child in snapshot[1]], ['a.txt', 'b.txt'])

    def test_oversized_trash_stays_visible_restorable_but_not_purgeable(self):
        for index in range(10000):
            (self.root/'folder'/f'{index}.txt').write_text('x')
        token = self.manage.trash('folder')
        result = self.manage.trash_page()
        self.assertEqual(result['total'], 1)
        self.assertFalse(result['size_complete'])
        self.assertIsNone(result['items'][0]['size_bytes'])
        self.assertFalse(result['items'][0]['purge_allowed'])
        with self.assertRaises(self.manage.SnapshotLimitError):
            self.manage.preview_purge(item_id=token)
        self.assertEqual(self.manage.preview_purge()['count'], 0)
        from fastapi.testclient import TestClient
        with TestClient(importlib.import_module('app.main').app) as client:
            client.post('/files/login', data={'password': 'fixture-password'}, headers={'Origin': 'http://testserver'})
            response = client.get('/files/tools')
            self.assertEqual(response.status_code, 200)
            self.assertIn('크기 확인 필요', response.text)
            self.assertIn('확인된 항목만 합산', response.text)
            self.assertIn('action="/files/restore"', response.text)
            self.assertNotIn('이 항목 영구 삭제 미리보기', response.text)
        self.manage.restore(token)
        self.assertEqual((self.root/'folder/9999.txt').read_text(), 'x')
