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

    def test_pagination_search_and_literal_sql_characters(self):
        with self.service._connect() as conn:
            conn.executemany('INSERT INTO bookmarks(url,title,note,saved_at) VALUES(?,?,?,?)',
                [(f'https://example.org/{i}', f'기사 {i}', '100%_literal' if i == 0 else 'memo', '2026-01-01T00:00:00+00:00') for i in range(1005)])
        result = self.service.page(page=21, page_size=50)
        self.assertEqual(result['total'], 1005)
        self.assertEqual(len(result['items']), 5)
        self.assertEqual(self.service.page(q='%_')['total'], 1)
        self.assertEqual(self.service.page(q="' OR 1=1 --")['total'], 0)
        self.assertEqual(self.service.page(page=999)['page'], 21)
        for kwargs in ({'page': 0}, {'page_size': 201}, {'q': 'x'*201}):
            with self.assertRaises(ValueError):
                self.service.page(**kwargs)
        self.client.post('/files/login', data={'password': 'password'}, headers={'Origin': 'http://testserver'})
        response = self.client.get('/files/bookmarks?page=21')
        self.assertEqual(response.status_code, 200)
        self.assertIn('1005', response.text)
        self.assertIn('기사 0', response.text)

    def test_page_count_and_rows_use_one_read_snapshot(self):
        from contextlib import contextmanager
        import sqlite3
        self.service.save('https://example.org/original', 'Original', '')
        with self.service._connect() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
        original_connect = self.service._connect
        class CountCursor:
            def __init__(inner, cursor):
                inner.cursor = cursor
            def fetchone(inner):
                count = inner.cursor.fetchone()
                # Commit a concurrent write after COUNT is consumed, before rows.
                writer = sqlite3.connect(os.environ['NEWS_BOOKMARK_DB_PATH'])
                try:
                    writer.execute('INSERT INTO bookmarks(url,title,note,saved_at) VALUES(?,?,?,?)', ('https://example.org/concurrent', 'Concurrent', '', '2026-01-01T00:00:00+00:00'))
                    writer.commit()
                finally:
                    writer.close()
                return count
        class Reader:
            def __init__(inner, conn):
                inner.conn = conn
            def execute(inner, sql, params=()):
                cursor = inner.conn.execute(sql, params)
                return CountCursor(cursor) if sql.startswith('SELECT COUNT') else cursor
        @contextmanager
        def reader():
            with original_connect() as conn:
                yield Reader(conn)
        with patch.object(self.service, '_connect', reader):
            result = self.service.page()
        self.assertEqual(result['total'], 1)
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['title'], 'Original')
        self.assertEqual(len(self.service.listing()), 2)
