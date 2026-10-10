import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

import aiohttp
from fastapi.testclient import TestClient
from tests.book_memo import test_ui_contract


class BookReliabilityTests(unittest.TestCase):
    def test_old_schema_parallel_init_and_retry_preserve_data_atomically(self):
        with tempfile.TemporaryDirectory() as tmp, test_ui_contract.BookMemoUiContractTests().loaded_app(tmp):
            from app.services import book_service as service
            book = service.create_or_get_book({'isbn': 'old', 'title': '기존 책'})
            service.create_chapter(book['id'], '기존 목차')
            with service._connect() as connection:
                connection.execute('ALTER TABLE book_chapters DROP COLUMN version')
                connection.execute('DROP TABLE write_requests')
            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(lambda _: service.init_db(), range(4)))
            chapter = service.list_chapters(book['id'])[0]
            self.assertEqual((chapter['title'], chapter['version']), ('기존 목차', 1))
            with patch.object(service, 'record_result', side_effect=RuntimeError('injected transaction failure')):
                with self.assertRaises(RuntimeError):
                    service.create_memo(book['id'], None, '메모', '본문', 0, tags='태그', request_id='a'*32)
            self.assertEqual(service.list_memos(book['id']), [])
            with service._connect() as connection:
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM memo_tags').fetchone()[0], 0)
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM write_requests').fetchone()[0], 0)
            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(lambda _: service.create_memo(book['id'], None, '메모', '본문', 0, tags='태그', request_id='a'*32), range(4)))
            self.assertEqual(len(service.list_memos(book['id'])), 1)
            self.assertEqual(service.list_memos(book['id'])[0]['tags'], ['태그'])
            def edit(comment):
                try:
                    service.update_chapter(chapter['id'], False, comment, expected_version=1)
                    return 'saved'
                except service.WriteConflict:
                    return 'conflict'
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(edit, ['A', 'B']))
            self.assertCountEqual(outcomes, ['saved', 'conflict'])
            self.assertEqual(service.list_chapters(book['id'])[0]['version'], 2)

    def test_same_request_replays_once_and_different_payload_is_conflict(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DELETE_PASSWORD': 'test', 'AUTH_RATE_LIMIT_STATE_PATH': str(Path(tmp)/'auth.json')}), test_ui_contract.BookMemoUiContractTests().loaded_app(tmp) as app:
            from app.services import book_service as service
            book = service.create_or_get_book({'isbn': 'retry', 'title': '책'})
            with TestClient(app, base_url='https://books.len.pe.kr') as client:
                headers = {'Origin': 'https://books.len.pe.kr'}
                client.post('/auth/login', data={'password': 'test'}, headers=headers)
                data = {'memo_title': '메모', 'content': '본문', 'request_id': 'a'*32}
                for _ in range(2):
                    self.assertEqual(client.post(f"/books/{book['id']}/memos", data=data, headers=headers, follow_redirects=False).status_code, 303)
                self.assertEqual(len(service.list_memos(book['id'])), 1)
                conflict = client.post(f"/books/{book['id']}/memos", data={**data, 'content': '다른 본문'}, headers=headers, follow_redirects=False)
                self.assertEqual(conflict.status_code, 409)
                self.assertEqual(service.list_memos(book['id'])[0]['content'], '본문')
                chapter = {'title': '동일 목차', 'request_id': 'b'*32}
                for _ in range(2):
                    self.assertEqual(client.post(f"/books/{book['id']}/chapters", data=chapter, headers=headers, follow_redirects=False).status_code, 303)
                self.assertEqual(len(service.list_chapters(book['id'])), 1)

    def test_stale_chapter_and_bulk_save_do_not_overwrite_latest(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DELETE_PASSWORD': 'test'}), test_ui_contract.BookMemoUiContractTests().loaded_app(tmp) as app:
            from app.services import book_service as service
            book = service.create_or_get_book({'isbn': 'conflict', 'title': '책'})
            service.create_chapter(book['id'], '목차')
            chapter = service.list_chapters(book['id'])[0]
            with TestClient(app, base_url='https://books.len.pe.kr') as client:
                headers = {'Origin': 'https://books.len.pe.kr'}
                client.post('/auth/login', data={'password': 'test'}, headers=headers)
                path = f"/chapters/{chapter['id']}"
                html = client.get(f"/books/{book['id']}").text
                hidden = re.search(r'name="expected_version" value="([0-9]+)"', html)
                self.assertIsNotNone(hidden, 'chapter edit must submit its loaded revision')
                data = {'is_done': '0', 'comment': '최신', 'expected_version': '1', 'request_id': 'b'*32}
                self.assertEqual(client.post(path, data=data, headers=headers, follow_redirects=False).status_code, 303)
                self.assertEqual(client.post(path, data=data, headers=headers, follow_redirects=False).status_code, 303)
                for version in ['1', '0', '-1']:
                    stale = client.post(path, data={'is_done': '1', 'comment': '과거', 'expected_version': version}, headers=headers, follow_redirects=False)
                    self.assertEqual(stale.status_code, 409)
                missing = client.post(path, data={'comment': '구 HTML'}, headers=headers, follow_redirects=False)
                self.assertEqual(missing.status_code, 409)
                bulk = client.post(f"/books/{book['id']}/chapter-statuses", data={'expected_versions': '{"1":1}'}, headers=headers, follow_redirects=False)
                self.assertEqual(bulk.status_code, 409)
                final = service.list_chapters(book['id'])[0]
                self.assertEqual((final['comment'], final['is_done']), ('최신', 0))

    def test_home_bulk_revision_matches_the_same_displayed_chapter_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp, test_ui_contract.BookMemoUiContractTests().loaded_app(tmp) as app:
            from app.services import book_service as service
            book = service.create_or_get_book({'isbn': 'snapshot', 'title': '책'})
            service.create_chapter(book['id'], '목차')
            chapter = service.list_chapters(book['id'])[0]
            newer = {**chapter, 'is_done': 1, 'version': 2}
            with patch('app.main.list_chapters', side_effect=[[chapter], [newer]]):
                html = TestClient(app).get('/').text
            import html as html_module
            versions = re.search(r'name="expected_versions" value="([^"]+)"', html)
            self.assertEqual(html_module.unescape(versions.group(1)), '{"1": 1}')

    def test_all_search_failures_are_distinct_from_successful_empty_results(self):
        with tempfile.TemporaryDirectory() as tmp, test_ui_contract.BookMemoUiContractTests().loaded_app(tmp) as app:
            from app.services import book_search
            with patch.dict(os.environ, {'ALADIN_TTB_KEY': ''}), patch.object(book_search, '_get_json', side_effect=aiohttp.ClientConnectionError('upstream unavailable')):
                response = TestClient(app).get('/?q=책')
                self.assertIn('검색 실패', response.text)
                self.assertNotIn('검색 결과가 없습니다.', response.text)
            book_search._CACHE.clear()
            with patch.object(book_search, '_search_aladin', return_value=[]), patch.object(book_search, '_search_google_books', return_value=[]), patch.object(book_search, '_search_open_library', return_value=[]):
                self.assertEqual(book_search.search_books('책'), [])
            book_search._CACHE.clear()
            with patch.object(book_search, '_search_aladin', side_effect=aiohttp.ClientConnectionError()), patch.object(book_search, '_search_google_books', return_value=[{'title': '찾은 책'}]):
                self.assertEqual(book_search.search_books('책'), [{'title': '찾은 책'}])

    def test_tag_retry_does_not_reapply_an_older_successful_update(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DELETE_PASSWORD': 'test'}), test_ui_contract.BookMemoUiContractTests().loaded_app(tmp) as app:
            from app.services import book_service as service
            book = service.create_or_get_book({'isbn': 'tags', 'title': '책'})
            service.create_memo(book['id'], None, '메모', '본문', 0)
            memo_id = service.list_memos(book['id'])[0]['id']
            with TestClient(app, base_url='https://books.len.pe.kr') as client:
                headers = {'Origin': 'https://books.len.pe.kr'}
                client.post('/auth/login', data={'password': 'test'}, headers=headers)
                for body in [{'tags': '이전', 'request_id': 'a'*32}, {'tags': '최신', 'request_id': 'b'*32}, {'tags': '이전', 'request_id': 'a'*32}]:
                    self.assertEqual(client.post(f'/memos/{memo_id}/tags', data=body, headers=headers, follow_redirects=False).status_code, 303)
                self.assertEqual(service.list_memos(book['id'])[0]['tags'], ['최신'])

    def test_locked_database_returns_retry_form_without_losing_inputs_or_leaking_errors(self):
        import sqlite3
        import html
        import re
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DELETE_PASSWORD': 'test'}), test_ui_contract.BookMemoUiContractTests().loaded_app(tmp) as app:
            from app.services import book_service as service
            book = service.create_or_get_book({'isbn': 'busy', 'title': '책'}); item_id = book['id']
            with TestClient(app, base_url='https://books.len.pe.kr') as client:
                headers = {'Origin': 'https://books.len.pe.kr', 'Accept': 'text/html'}
                client.post('/auth/login', data={'password': 'test'}, headers=headers)
                original_connect = sqlite3.connect
                def short_connect(*args, **kwargs):
                    kwargs['timeout'] = 0.05
                    return original_connect(*args, **kwargs)
                lock = original_connect(service.DB_PATH)
                lock.execute('BEGIN IMMEDIATE')
                data = {'memo_title': '복구 제목', 'content': '<script>비공개 본문</script>', 'request_id': 'c'*32, 'password': 'never-echo'}
                with patch.object(service.sqlite3, 'connect', side_effect=short_connect):
                    response = client.post(f"/books/{item_id}/memos", data=data, headers=headers, follow_redirects=False)
                lock.rollback(); lock.close()
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.headers['retry-after'], '2')
                self.assertEqual(response.headers['cache-control'], 'no-store')
                self.assertIn('다시 저장', response.text)
                self.assertIn('&lt;script&gt;', response.text)
                self.assertNotIn('<script>비공개', response.text)
                self.assertNotIn('never-echo', response.text)
                self.assertNotIn('database is locked', response.text)
                fields = re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', response.text)
                retry = {html.unescape(k): html.unescape(v) for k,v in fields}
                self.assertEqual(retry['content'], data['content'])
                self.assertEqual(retry['request_id'], data['request_id'])
                for _ in range(2):
                    self.assertEqual(client.post(f"/books/{item_id}/memos", data=retry, headers=headers, follow_redirects=False).status_code, 303)
                self.assertEqual(len(service.list_memos(item_id)), 1)
                self.assertEqual(service.list_memos(item_id)[0]['content'], data['content'])

    def test_import_database_work_does_not_block_health_and_busy_is_retryable(self):
        import asyncio
        import threading
        import httpx
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DELETE_PASSWORD': 'test'}), test_ui_contract.BookMemoUiContractTests().loaded_app(tmp) as app:
            from app.services import import_service, export_service
            raw = __import__('json').dumps(export_service.export_records())
            with TestClient(app, base_url='https://books.len.pe.kr') as login_client:
                login_client.post('/auth/login', data={'password': 'test'}, headers={'Origin': 'https://books.len.pe.kr'})
                cookies = dict(login_client.cookies)
                async def check():
                    headers = {'Origin':'https://books.len.pe.kr', 'Content-Type':'application/json', 'Accept':'application/json'}
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://books.len.pe.kr', cookies=cookies) as client:
                        preview = await client.post('/api/import/preview', content=raw, headers=headers)
                        self.assertEqual(preview.status_code, 200)
                        entered, release = threading.Event(), threading.Event()
                        def slow_commit(payload):
                            entered.set()
                            release.wait(2)
                            return {'imported_count':0, 'skip_count':0}
                        with patch.object(import_service, 'commit_import', side_effect=slow_commit):
                            task = asyncio.create_task(client.post('/api/import/commit', content=raw, headers={**headers,'X-Import-Preview':preview.json()['preview_token']}))
                            try:
                                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                                self.assertFalse(task.done(), 'DB work must execute outside the event loop')
                                health = await asyncio.wait_for(client.get('/health'), timeout=0.5)
                                self.assertEqual(health.status_code, 200)
                            finally:
                                release.set()
                                await task
                        for endpoint, operation in [('preview','preview_import'),('commit','commit_import')]:
                            with patch.object(import_service, operation, side_effect=sqlite3.OperationalError('database is locked')):
                                response = await client.post('/api/import/'+endpoint, content=raw, headers={**headers,'X-Import-Preview':preview.json()['preview_token']})
                            self.assertEqual(response.status_code, 503)
                            self.assertEqual(response.json()['code'], 'database_busy')
                            self.assertEqual(response.headers['retry-after'], '2')
                asyncio.run(check())

    def test_busy_bulk_chapters_preserves_repeated_titles_and_delete_has_explicit_label(self):
        import html
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DELETE_PASSWORD':'test'}), test_ui_contract.BookMemoUiContractTests().loaded_app(tmp) as app:
            from app.services import book_service as service
            book = service.create_or_get_book({'isbn':'bulk-busy', 'title':'책'})
            with TestClient(app, base_url='https://books.len.pe.kr') as client:
                headers = {'Origin':'https://books.len.pe.kr', 'Accept':'text/html'}
                client.post('/auth/login', data={'password':'test'}, headers=headers)
                with patch('app.main.create_chapters', side_effect=sqlite3.OperationalError('database is locked')):
                    response = client.post(f"/books/{book['id']}/chapters/bulk", data={'titles':['첫 목차', '<둘째>']}, headers=headers)
                self.assertEqual(response.status_code, 503)
                self.assertIn('text/html', response.headers['content-type'])
                self.assertEqual(response.text.count('name="titles"'), 2)
                self.assertIn('value="&lt;둘째&gt;"', response.text)
                with patch('app.main.delete_book', side_effect=sqlite3.OperationalError('database is locked')):
                    response = client.post(f"/books/{book['id']}/delete", headers=headers)
                self.assertEqual(response.status_code, 503)
                self.assertIn('삭제 다시 시도', response.text)
                self.assertNotIn('같은 내용으로 다시 저장', response.text)
