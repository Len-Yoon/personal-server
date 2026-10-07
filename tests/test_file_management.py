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
