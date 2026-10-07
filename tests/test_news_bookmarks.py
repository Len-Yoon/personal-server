import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tests._test_support import prepare_service_import


class BookmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        env = patch.dict(os.environ, {'APP_ENV':'development', 'FILE_MANAGER_AUTH_REQUIRED':'true', 'FILE_MANAGER_ACCESS_PASSWORD':'password', 'NEWS_BOOKMARK_DB_PATH':str(root/'bookmarks.sqlite3'), 'AUTH_RATE_LIMIT_STATE_PATH':str(root/'auth.json'), 'SECURITY_LOG_PATH':str(root/'events.txt'), 'FILE_STORAGE_PATH':str(root/'files')})
        env.start()
        self.addCleanup(env.stop)
        prepare_service_import('portal-web')
        self.service = importlib.import_module('app.services.news_bookmarks')
        from fastapi.testclient import TestClient
        self.client = TestClient(importlib.import_module('app.main').app)
        self.addCleanup(self.client.close)

    def test_private_routes_auth_and_csrf(self):
        self.assertEqual(self.client.get('/files/bookmarks').status_code, 401)
        self.assertEqual(self.client.post('/files/bookmarks', data={'url':'https://example.org/news', 'title':'Private'}, headers={'Origin':'http://testserver'}).status_code, 401)
        self.client.post('/files/login', data={'password':'password'}, headers={'Origin':'http://testserver'})
        self.assertEqual(self.client.post('/files/bookmarks', data={'url':'https://example.org/news','title':'Private'}).status_code,403)
        response=self.client.post('/files/bookmarks',data={'url':'https://example.org/news','title':'Private title','note':'<script>secret</script>'},headers={'Origin':'http://testserver'})
        self.assertEqual(response.status_code,200)
        self.assertIn('&lt;script&gt;secret&lt;/script&gt;', response.text)
        self.assertNotIn('Private title', self.client.get('/health').text)

    def test_url_snapshot_survives_update_and_rejects_unsafe_links(self):
        self.service.save('https://example.org/article','Original','Note')
        self.service.save('https://example.org/article','Edited','Changed')
        items=self.service.listing()
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['note'],'Changed')
        for url in ['javascript:alert(1)','file:///etc/passwd','https://user:pass@example.org','https://example.org/\nsecret']:
            with self.assertRaises(ValueError):
                self.service.save(url,'Title','Note')
        self.service.delete(items[0]['id'])
        self.assertEqual(self.service.listing(),[])
