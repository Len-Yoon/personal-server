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


class PortalContinuityIntegrationTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {'APP_ENV': 'test'})
        environment.start()
        self.addCleanup(environment.stop)
        prepare_service_import('portal-web')
        from app.services import security
        logs = tempfile.TemporaryDirectory()
        self.addCleanup(logs.cleanup)
        fixture = patch.object(security, 'LOG_PATH', Path(logs.name) / 'events')
        fixture.start()
        self.addCleanup(fixture.stop)

    def test_purge_confirmation_survives_worker_key_change_and_is_single_use(self):
        from app.services import file_management, file_store, security
        from app.services.session_store import SQLiteSessionStore
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            files = root / 'files'
            files.mkdir()
            (files / 'saved.txt').write_text('acknowledged')
            store = SQLiteSessionStore(root / 'auth' / 'session-state.sqlite3')
            with patch.object(file_store, 'STORAGE_PATH', files), patch.object(security, 'AUTH_SESSION_STORE', store), patch.object(file_management, '_PURGE_KEY', b'worker-A'):
                token = file_management.trash('saved.txt')
                preview = file_management.preview_purge(item_id=token)
                with patch.object(file_management, '_PURGE_KEY', b'worker-B'):
                    result = file_management.confirm_purge(preview['confirmation'])
                    self.assertTrue(result['ok'])
                    self.assertEqual(result['deleted'], [token])
                    with self.assertRaises(ValueError):
                        file_management.confirm_purge(preview['confirmation'])

    def test_online_backup_preserves_preexisting_trash_confirmation(self):
        import sqlite3
        import subprocess
        import sys
        from app.services import file_management, file_store, security
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            files, state = root / 'files', root / 'state'
            files.mkdir(); state.mkdir()
            (files / 'saved.txt').write_text('acknowledged')
            with sqlite3.connect(state / 'homeops.sqlite3') as connection:
                connection.execute('CREATE TABLE accepted(value TEXT)')
                connection.execute("INSERT INTO accepted VALUES ('committed')")
            with patch.object(file_store, 'STORAGE_PATH', files), patch.object(security, 'AUTH_SESSION_STORE', None):
                token = file_management.trash('saved.txt')
                preview = file_management.preview_purge(item_id=token)
                script = Path(__file__).resolve().parents[1] / 'infra/k8s/tools/portal-online-snapshot.py'
                (root / 'snapshot').mkdir()
                result = subprocess.run([sys.executable, str(script), 'capture', '--files-root', str(files), '--state-root', str(state), '--output', str(root / 'snapshot')], capture_output=True, timeout=15, env={'PATH': os.defpath})
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertTrue(file_management.confirm_purge(preview['confirmation'])['ok'])
                self.assertEqual((root / 'snapshot/files/.trash' / token / 'item').read_text(), 'acknowledged')

    def test_real_http_processes_continue_work_during_snapshot_and_shutdown(self):
        import json
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'evidence.json'
            script = Path(__file__).resolve()
            result = _run_continuity_command([sys.executable, str(script), '--continuity-worker', '--output', str(output)], env={'PATH': os.defpath, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])})
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            evidence = json.loads(output.read_text())
            self.assertEqual(evidence['status'], 'PASS')
            self.assertGreaterEqual(evidence['requests_success'], 79)
            for name in ['unexpected_401', 'unexpected_5xx', 'connection_errors', 'acknowledgement_loss']:
                self.assertEqual(evidence[name], 0)
            self.assertGreaterEqual(evidence['foreground_writes_during_snapshot'], 1)


    def test_http_worker_timeout_terminates_its_owned_process_group(self):
        import signal
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'worker-pid'
            # A cooperative fixture reaps its child, as the actual worker does.
            code = """import os,signal,subprocess,sys,time
from pathlib import Path
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
def stop(*args):
    child.terminate()
    child.wait(timeout=3)
    raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(60)
"""
            with self.assertRaises(subprocess.TimeoutExpired):
                _run_continuity_command([sys.executable, '-c', code, str(marker)], env={'PATH': os.defpath}, timeout=1)
            self.assertTrue(marker.exists())
            with self.assertRaises(ProcessLookupError):
                os.killpg(int(marker.read_text()), signal.SIGCONT)


    def test_explicit_revocation_rejects_all_scopes_and_grants_across_processes(self):
        import json
        import subprocess
        import sys
        from app.services.session_store import SQLiteSessionStore
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'auth/session-state.sqlite3'
            store = SQLiteSessionStore(path)
            tokens = {scope: store.create(scope, 60) for scope in ['file_access', 'homeops_admin', 'portfolio_admin']}
            grant = store.create_confirmation('file_purge', 'preview', 60)
            store.revoke_all()
            child = "import json,sys;from pathlib import Path;from app.services.session_store import SQLiteSessionStore;s=SQLiteSessionStore(Path(sys.argv[1]));tokens=json.loads(sys.argv[2]);print(json.dumps({'sessions':any(s.validate(k,v) for k,v in tokens.items()),'grant':s.consume_confirmation('file_purge','preview',sys.argv[3])}))"
            result = subprocess.run([sys.executable, '-c', child, str(path), json.dumps(tokens), grant], capture_output=True, timeout=10, env={'PATH': os.defpath, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'portal-web')})
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(json.loads(result.stdout), {'sessions': False, 'grant': False})
            self.assertTrue(store.validate('file_access', store.create('file_access', 60)))

    def test_revocation_busy_or_second_delete_failure_never_reports_partial_success(self):
        import sqlite3
        from app.services.session_store import SQLiteSessionStore
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'auth/session-state.sqlite3'
            store = SQLiteSessionStore(path)
            token = store.create('file_access', 60)
            grant = store.create_confirmation('file_purge', 'preview', 60)
            connection = sqlite3.connect(path)
            try:
                connection.execute('BEGIN IMMEDIATE')
                with self.assertRaises(sqlite3.OperationalError): store.revoke_all()
                connection.rollback()
                connection.execute("CREATE TRIGGER preserve_grants BEFORE DELETE ON confirmations BEGIN SELECT RAISE(ABORT, 'fixture denial'); END")
                connection.commit()
                with self.assertRaises(sqlite3.IntegrityError): store.revoke_all()
                self.assertTrue(store.validate('file_access', token))
                self.assertTrue(store.consume_confirmation('wrong_scope', 'preview', grant) is False)
                connection.execute('DROP TRIGGER preserve_grants'); connection.commit()
                self.assertTrue(store.consume_confirmation('file_purge', 'preview', grant))
            finally:
                connection.close()

    def test_unconfigured_revocation_cli_does_not_create_storage(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-m', 'app.services.session_store', '--revoke-all'], capture_output=True, timeout=5, cwd=directory, env={'PATH': os.defpath, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'portal-web')})
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stderr.strip(), b'portal_session_revocation=FAIL')
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_application_lifespan_keeps_idle_wal_available_and_closes_it(self):
        import asyncio
        from app import main
        from app.services import security
        from app.services.session_store import SQLiteSessionStore
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'auth/session-state.sqlite3'
            store = SQLiteSessionStore(path)
            async def run():
                with patch.object(security, 'AUTH_SESSION_STORE', store):
                    async with main.portal_lifespan(main.app):
                        self.assertTrue(Path(str(path) + '-wal').exists())
                        self.assertTrue(Path(str(path) + '-shm').exists())
                        self.assertTrue(store.validate('file_access', store.create('file_access', 60)))
                    self.assertIsNone(store._keeper)
                    self.assertFalse(Path(str(path) + '-shm').exists())
            asyncio.run(run())


    def test_unpatched_production_and_unknown_environments_cannot_start_shared_wal(self):
        import asyncio
        import sqlite3
        from types import SimpleNamespace
        from unittest.mock import Mock
        from app import main
        from app.services import file_store, security
        from app.services.session_store import sqlite_wal_is_patched
        for version in [(3, 44, 6), (3, 50, 7), (3, 51, 3), (3, 52, 0)]:
            with patch.object(sqlite3, 'sqlite_version_info', version): self.assertTrue(sqlite_wal_is_patched())
        for version in [(3, 44, 5), (3, 45, 1), (3, 50, 6), (3, 51, 2)]:
            with patch.object(sqlite3, 'sqlite_version_info', version): self.assertFalse(sqlite_wal_is_patched())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for environment in ['', 'prod', 'staging', 'production']:
                store = SimpleNamespace(start=Mock(), close=Mock(), ready=Mock(return_value=True))
                async def run():
                    async with main.portal_lifespan(main.app): pass
                with self.subTest(environment=environment), patch.dict(os.environ, {'APP_ENV': environment, 'ADMIN_STATUS_PASSWORD': 'fixture', 'FILE_MANAGER_ACCESS_PASSWORD': 'fixture', 'HOMEOPS_EXECUTOR_SHARED_SECRET': 'fixture'}), patch.object(file_store, 'STORAGE_PATH', root), patch.object(security, 'AUTH_RATE_LIMIT_STATE_PATH', root / 'auth.json'), patch.object(security, 'AUTH_SESSION_STORE', store), patch.object(sqlite3, 'sqlite_version_info', (3, 45, 1)):
                    with self.assertRaises(RuntimeError): asyncio.run(run())
                    store.start.assert_not_called()
                    self.assertEqual(main.ready().status_code, 503)
                with patch.dict(os.environ, {'APP_ENV': environment}), patch.object(security, 'AUTH_SESSION_STORE', store), patch.object(sqlite3, 'sqlite_version_info', (3, 51, 3)):
                    asyncio.run(run())
                    store.start.assert_called_once()
                    store.close.assert_called_once()


# HTTP continuity runs only through the existing Portal test suite.
import argparse
from contextlib import closing
import http.client
from http.cookies import SimpleCookie
import json
from pathlib import Path
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlencode


_CONTINUITY_ROOT = Path(__file__).resolve().parents[1]


class _ContinuityFailure(RuntimeError):
    pass


def _require_continuity(condition, message):
    if not condition:
        raise _ContinuityFailure(message)


def _reserve_continuity_port():
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]



def _run_continuity_command(command, *, env, timeout=35):
    # Own a new process group so a stuck worker cannot leave fixture servers alive.
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=env, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=28)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=3)


def _run_continuity_lab(evidence=None):
    initial = {"kind": "two-process-loopback-http-lab", "status": "FAIL", "requests_success": 0,
                "unexpected_401": 0, "unexpected_5xx": 0, "connection_errors": 0,
                "acknowledgement_loss": 0,
                "limitations": ["Kubernetes rollout and ingress are not exercised", "Read-only source mounts are not verified", "Physical host failure is not exercised"]}
    evidence = evidence if evidence is not None else {}
    evidence.update(initial)
    def require_ack(condition, message):
        if not condition:
            evidence["acknowledgement_loss"] += 1
        _require_continuity(condition, message)
    children = []
    with tempfile.TemporaryDirectory(prefix="portal-continuity-") as temporary:
        base = Path(temporary).resolve()
        files, state, output = (base / name for name in ("files", "state", "snapshot"))
        for path in (files, state, output, state / "auth"):
            path.mkdir(mode=0o700)
        # Fixture-only environment: no inherited credentials, proxy settings or paths.
        env = {"APP_ENV": "development", "PYTHONUNBUFFERED": "1", "HOME": str(base),
               "FILE_MANAGER_AUTH_REQUIRED": "true", "FILE_MANAGER_ACCESS_PASSWORD": "lab-access-only",
               "DELETE_PASSWORD": "lab-delete-only", "ADMIN_STATUS_PASSWORD": "lab-admin-only",
               "FILE_STORAGE_PATH": str(files), "HOMEOPS_DB_PATH": str(state / "homeops.sqlite3"),
               "NEWS_BOOKMARK_DB_PATH": str(state / "news-bookmarks.sqlite3"),
               "AUTH_RATE_LIMIT_STATE_PATH": str(state / "auth-rate-limit-state.json"),
               "AUTH_SESSION_STATE_PATH": str(state / "auth/session-state.sqlite3"),
               "SECURITY_LOG_PATH": str(state / "security-events.txt"),
               "PORTFOLIO_CONTENT_PATH": str(files / ".portfolio/portfolio.md")}
        logs = []
        cookie = ""
        def start(extra=None):
            port = _reserve_continuity_port()
            log = tempfile.TemporaryFile()
            logs.append(log)
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log", "--timeout-graceful-shutdown", "5"],
                                       cwd=_CONTINUITY_ROOT / "portal-web", env={**env, **(extra or {})}, stdout=log, stderr=log)
            children.append(process)
            return process, port
        def request(port, method, path, data=None, *, expected=200, content_type=None, authenticate=True):
            body = urlencode(data).encode() if isinstance(data, dict) else data
            headers = {"Origin": f"http://127.0.0.1:{port}"}
            if authenticate and cookie:
                headers["Cookie"] = cookie
            if body is not None:
                headers["Content-Type"] = content_type or "application/x-www-form-urlencoded"
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                connection.request(method, path, body, headers)
                response = connection.getresponse()
                payload, response_headers = response.read(), dict(response.getheaders())
                if response.status == 401 and expected != 401:
                    evidence["unexpected_401"] += 1
                if response.status >= 500 and expected < 500:
                    evidence["unexpected_5xx"] += 1
                _require_continuity(response.status == expected, f"unexpected HTTP status for {method} {path}")
                evidence["requests_success"] += 1
                return payload, response_headers
            except (OSError, http.client.HTTPException):
                evidence["connection_errors"] += 1
                raise _ContinuityFailure("HTTP transport failed") from None
            finally:
                connection.close()
        def ready(process, port, expected=200):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                _require_continuity(process.poll() is None, "server exited before readiness")
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=.25)
                try:
                    conn.request("GET", "/ready")
                    result = conn.getresponse()
                    result.read()
                    if result.status == expected:
                        return
                except (OSError, http.client.HTTPException):
                    pass
                finally:
                    conn.close()
                time.sleep(.025)
            raise _ContinuityFailure("readiness deadline exceeded")
        def upload(port, name, payload):
            boundary = "portal-lab-fixture-boundary"
            body = (f'--{boundary}\r\nContent-Disposition: form-data; name="upload"; filename="{name}"\r\nContent-Type: text/plain\r\n\r\n'.encode()
                    + payload + f"\r\n--{boundary}--\r\n".encode())
            request(port, "POST", "/files/upload", body, expected=303, content_type=f"multipart/form-data; boundary={boundary}")
        try:
            a, port_a = start()
            ready(a, port_a)
            _, headers = request(port_a, "POST", "/files/login", {"password": "lab-access-only"}, expected=303, authenticate=False)
            jar = SimpleCookie()
            jar.load(headers.get("set-cookie", ""))
            _require_continuity("file_manager_access" in jar, "login did not issue a cookie")
            cookie = "file_manager_access=" + jar["file_manager_access"].value
            b, port_b = start()
            ready(b, port_b)
            for port in (port_a, port_b):
                request(port, "GET", "/files/tools")
            evidence["same_cookie_accepted_by_both"] = True
            bad, port_bad = start({"FILE_STORAGE_PATH": str(base / "missing-storage")})
            ready(bad, port_bad, expected=503)
            request(port_a, "GET", "/files/tools")
            evidence["unready_replacement_keeps_old_serving"] = True
            bad.terminate()
            bad.wait(timeout=6)
            upload(port_a, "pre-capture.txt", b"acknowledged before capture")
            request(port_b, "POST", "/files/bookmarks", {"url": "https://example.invalid/lab", "title": "acknowledged before capture", "note": "fixture only"}, expected=303)
            # Bound the fixture to 96 MiB; sufficiently long to observe real copy overlap.
            block = b"fixture snapshot payload\n" * 8192
            for index in range(48):
                with (files / f"fixture-{index:02d}.txt").open("wb") as stream:
                    for _ in range(10):
                        stream.write(block)
            snapshot = subprocess.Popen([sys.executable, str(_CONTINUITY_ROOT / "infra/k8s/tools/portal-online-snapshot.py"), "capture", "--files-root", str(files), "--state-root", str(state), "--output", str(output)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            children.append(snapshot)
            accepted = []
            for index in range(16):
                port = port_a if index % 2 == 0 else port_b
                started = time.time()
                upload(port, f"live-{index}.txt", f"acknowledged-{index}".encode())
                request(port_b if port == port_a else port_a, "POST", "/files/move", {"path": f"live-{index}.txt", "name": f"edited-{index}.txt"}, expected=303)
                request(port, "GET", "/files/tools?q=edited")
                accepted.append((started, time.time(), index))
            _require_continuity(snapshot.wait(timeout=8) == 0, "online snapshot failed")
            manifest = json.loads((output / "snapshot-manifest.json").read_text())
            from datetime import datetime
            first = datetime.fromisoformat(manifest["started_at"]).timestamp()
            last = datetime.fromisoformat(manifest["finished_at"]).timestamp()
            overlapping = sum(start >= first and finish <= last for start, finish, _ in accepted)
            _require_continuity(overlapping > 0, "no foreground write completed inside actual snapshot interval")
            evidence["foreground_writes_during_snapshot"] = overlapping
            require_ack((output / "files/pre-capture.txt").read_bytes() == b"acknowledged before capture", "pre-capture upload lost")
            for db in (output / "portal-web-state").rglob("*.sqlite3"):
                with closing(sqlite3.connect(db)) as restored:
                    _require_continuity(restored.execute("PRAGMA quick_check").fetchone() == ("ok",), "restored database integrity failed")
            with closing(sqlite3.connect(output / "portal-web-state/news-bookmarks.sqlite3")) as restored:
                require_ack(restored.execute("SELECT title FROM bookmarks").fetchone()[0] == "acknowledged before capture", "pre-capture bookmark lost")
            evidence["pre_capture_acknowledgements_restored"] = 2
            for _, _, index in accepted:
                body, _ = request(port_b, "GET", f"/files/download?path=edited-{index}.txt")
                require_ack(body == f"acknowledged-{index}".encode(), "foreground acknowledgement lost")
            # Cross-process one-use confirmation: preview on A, consume on B.
            upload(port_a, "purge-fixture.txt", b"temporary")
            payload, _ = request(port_a, "POST", "/files/trash", {"paths": "purge-fixture.txt", "delete_password": "lab-delete-only"})
            token = json.loads(payload)["moved"][0]["id"]
            html, _ = request(port_a, "POST", "/files/trash/preview", {"item_id": token})
            match = re.search(rb'name="confirmation" value="([^"]+)"', html)
            _require_continuity(match is not None, "purge preview token missing")
            request(port_b, "POST", "/files/trash/purge", {"confirmation": match[1].decode(), "confirm": "영구 삭제", "delete_password": "lab-delete-only"}, expected=303)
            evidence["cross_process_preview_consumed"] = True
            # Hold an actual A upload request open while SIGTERM initiates drain.
            sent = threading.Event()
            outcome = []
            def draining_upload():
                connection = http.client.HTTPConnection("127.0.0.1", port_a, timeout=5)
                boundary = "drain-fixture"
                prefix = f'--{boundary}\r\nContent-Disposition: form-data; name="upload"; filename="drained.txt"\r\n\r\n'.encode()
                data = b"drain acknowledged" * 65536
                suffix = f"\r\n--{boundary}--\r\n".encode()
                try:
                    connection.putrequest("POST", "/files/upload")
                    connection.putheader("Origin", f"http://127.0.0.1:{port_a}")
                    connection.putheader("Cookie", cookie)
                    connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
                    connection.putheader("Content-Length", str(len(prefix) + len(data) + len(suffix)))
                    connection.endheaders()
                    connection.send(prefix + data[:1024])
                    time.sleep(.05)
                    sent.set()
                    time.sleep(.15)
                    connection.send(data[1024:] + suffix)
                    response = connection.getresponse()
                    response.read()
                    if response.status == 401:
                        evidence["unexpected_401"] += 1
                    if response.status >= 500:
                        evidence["unexpected_5xx"] += 1
                    outcome.append(response.status == 303)
                except (OSError, http.client.HTTPException):
                    evidence["connection_errors"] += 1
                    outcome.append(False)
                finally:
                    connection.close()
            thread = threading.Thread(target=draining_upload, daemon=True)
            thread.start()
            _require_continuity(sent.wait(timeout=2), "drain request did not start")
            a.send_signal(signal.SIGTERM)
            for _ in range(3):
                request(port_b, "GET", "/files/tools")
            thread.join(timeout=6)
            require_ack(outcome == [True], "in-flight request was lost during drain")
            evidence["requests_success"] += 1
            exit_code = a.wait(timeout=6)
            # Uvicorn may re-raise the captured SIGTERM after graceful shutdown.
            _require_continuity(exit_code in (0, -signal.SIGTERM), "old server drain failed")
            evidence["old_server_exit_code"] = exit_code
            body, _ = request(port_b, "GET", "/files/download?path=drained.txt")
            require_ack(body == b"drain acknowledged" * 65536, "drained upload acknowledgement lost")
            evidence["sigterm_inflight_upload_acknowledged"] = True
            evidence["new_server_served_during_drain"] = True
            evidence["status"] = "PASS"
        finally:
            for process in reversed(children):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=6)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
            for log in logs:
                log.close()
    return evidence


def _continuity_worker_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    def cancel(_signum, _frame):
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, signal.SIG_IGN)
        raise _ContinuityFailure("local lab cancelled")
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, cancel)
    result = {}
    try:
        _run_continuity_lab(result)
    except Exception as error:
        result.update({"kind": "two-process-loopback-http-lab", "status": "FAIL", "failure_type": type(error).__name__, "reason": str(error) if isinstance(error, _ContinuityFailure) else "local lab execution failed"})
    # Evidence includes counts and outcomes only, never cookies, paths or secrets.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("portal_continuity_lab=" + result["status"])
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__" and "--continuity-worker" in sys.argv:
    sys.argv.remove("--continuity-worker")
    raise SystemExit(_continuity_worker_main())
