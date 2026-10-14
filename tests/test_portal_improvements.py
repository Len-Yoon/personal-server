import importlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from tests._test_support import prepare_service_import


class PortalImprovementsTests(unittest.TestCase):
    def setUp(self):
        prepare_service_import('portal-web')

    def test_missing_environment_does_not_disable_file_auth(self):
        from app.routers import files
        with patch.dict(os.environ, {'APP_ENV':'', 'FILE_MANAGER_AUTH_REQUIRED':'false'}, clear=False):
            self.assertTrue(files._file_access_required())

    def test_only_explicit_development_opt_out(self):
        from app.routers import files
        for mode, flag, required in [('development','false',False), ('test','0',False), ('production','false',True), ('typo','false',True), ('development','',True)]:
            with self.subTest(mode=mode, flag=flag), patch.dict(os.environ, {'APP_ENV':mode,'FILE_MANAGER_AUTH_REQUIRED':flag}):
                self.assertEqual(files._file_access_required(), required)

    def test_quota_includes_trash_and_preserves_existing_content(self):
        from app.services import file_store as store
        with tempfile.TemporaryDirectory() as t, patch.object(store,'STORAGE_PATH',Path(t)), patch.object(store,'STORAGE_QUOTA_BYTES',8), patch.object(store,'MIN_FREE_BYTES',0):
            trash=Path(t)/'.trash';trash.mkdir();(trash/'old').write_bytes(b'123456')
            upload=SimpleNamespace(filename='new.txt',file=io.BytesIO(b'abc'))
            with self.assertRaises(ValueError):store.save_upload('',upload)
            self.assertFalse((Path(t)/'new.txt').exists());self.assertEqual((trash/'old').read_bytes(),b'123456')

    def test_disk_reserve_blocks_and_cleans_partial_upload(self):
        from app.services import file_store as store
        with tempfile.TemporaryDirectory() as t, patch.object(store,'STORAGE_PATH',Path(t)), patch.object(store,'MIN_FREE_BYTES',100), patch.object(store.shutil,'disk_usage',return_value=SimpleNamespace(free=101)):
            with self.assertRaises(ValueError):store.save_upload('',SimpleNamespace(filename='new.txt',file=io.BytesIO(b'abc')))
            self.assertFalse((Path(t)/'new.txt').exists())

    def test_bulk_quota_failure_rolls_back_only_this_request(self):
        from app.services import file_store as store
        with tempfile.TemporaryDirectory() as t, patch.object(store,'STORAGE_PATH',Path(t)), patch.object(store,'STORAGE_QUOTA_BYTES',5), patch.object(store,'MIN_FREE_BYTES',0):
            (Path(t)/'existing.txt').write_bytes(b'1')
            uploads=[SimpleNamespace(filename=n,file=io.BytesIO(b'abc')) for n in ['a.txt','b.txt']]
            with self.assertRaises(ValueError):store.save_uploads('',uploads)
            self.assertEqual((Path(t)/'existing.txt').read_bytes(),b'1')
            self.assertFalse((Path(t)/'a.txt').exists());self.assertFalse((Path(t)/'b.txt').exists())

    def test_backup_display_is_service_scoped_and_missing_is_unknown(self):
        from app.services.admin_status import backup_status_for_display,build_admin_status_context
        from datetime import datetime, timezone
        proof={'scope':'book-memo','verified':True,'status_reason':'backup_verified','backup_completed_at':'2026-10-13T00:00:00Z','restore_verified_at':'2026-10-13T00:01:00Z','evidence_expires_at':'2026-10-14T00:01:00Z'}
        now=datetime(2026,10,13,1,tzinfo=timezone.utc)
        self.assertTrue(backup_status_for_display(proof,now,'book-memo')['label'].startswith('정상'))
        self.assertFalse(backup_status_for_display(proof,now,'youtube-memo')['label'].startswith('정상'))
        context=build_admin_status_context({},[],{})
        self.assertEqual([row['name'] for row in context['service_backups']], ['Book','YouTube','Crawler','차량 DB'])
        self.assertTrue(all('확인 필요' in row['label'] for row in context['service_backups']))

    def test_quota_rejects_symlinks_without_touching_target(self):
        from app.services import file_store as store
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)/'files';root.mkdir();target=Path(t)/'outside';target.write_bytes(b'private')
            (root/'link').symlink_to(target)
            with patch.object(store,'STORAGE_PATH',root), self.assertRaises(ValueError):
                store.save_upload('',SimpleNamespace(filename='new.txt',file=io.BytesIO(b'a')))
            self.assertEqual(target.read_bytes(),b'private');self.assertFalse((root/'new.txt').exists())

    def test_public_file_login_cookie_is_secure_with_missing_app_env(self):
        from app.routers import files
        request=SimpleNamespace(url=SimpleNamespace(scheme='http',hostname='file.len.pe.kr'))
        with patch.object(files,'_client_id',return_value='fixture'), patch.object(files,'auth_rate_limited',return_value=False), patch.object(files,'_file_access_password',return_value='fixture-password'), patch.object(files,'clear_auth_failures'), patch.object(files,'append_security_event'), patch.dict(os.environ,{'APP_ENV':'','FILE_MANAGER_AUTH_REQUIRED':''}):
            response=files.file_login(request,password='fixture-password',next_path='')
        self.assertEqual(response.status_code,303);self.assertIn('Secure',response.headers['set-cookie'])

    @unittest.skipUnless(os.name=='posix','process lock is a POSIX storage contract')
    def test_two_processes_cannot_exceed_storage_quota(self):
        import multiprocessing
        from app.services import file_store as store
        if 'fork' not in multiprocessing.get_all_start_methods():self.skipTest('fork unavailable')
        context=multiprocessing.get_context('fork')
        with tempfile.TemporaryDirectory() as t, patch.object(store,'STORAGE_PATH',Path(t)), patch.object(store,'STORAGE_QUOTA_BYTES',8), patch.object(store,'MIN_FREE_BYTES',0), patch.object(store,'append_security_event'):
            gate=context.Event();queue=context.Queue()
            def upload(name):
                gate.wait(5)
                try:
                    store.save_upload('',SimpleNamespace(filename=name,file=io.BytesIO(b'123456')));queue.put('saved')
                except ValueError:queue.put('quota')
            workers=[context.Process(target=upload,args=(name,)) for name in ['a.txt','b.txt']]
            for worker in workers:worker.start()
            gate.set()
            for worker in workers:worker.join(10)
            try:
                self.assertTrue(all(not w.is_alive() and w.exitcode==0 for w in workers))
                self.assertEqual(sorted(queue.get(timeout=2) for _ in workers),['quota','saved'])
                self.assertEqual(sum(p.stat().st_size for p in Path(t).iterdir()),6)
            finally:
                for worker in workers:
                    if worker.is_alive():worker.terminate();worker.join()
                queue.close()
