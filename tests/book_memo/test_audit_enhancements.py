import copy
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from tests.book_memo import test_ui_contract


class BookAuditEnhancementTests(unittest.TestCase):
    def loaded(self, path):
        return test_ui_contract.BookMemoUiContractTests().loaded_app(path)

    def test_initialized_reads_work_while_another_connection_holds_writer_lock(self):
        with tempfile.TemporaryDirectory() as tmp, self.loaded(tmp):
            from app.services import book_service as service
            book = service.create_or_get_book({'isbn': 'read', 'title': '기존 책'})
            with sqlite3.connect(service.DB_PATH) as writer:
                writer.execute('BEGIN IMMEDIATE')
                self.assertEqual(service.get_book(book['id'])['title'], '기존 책')
                self.assertEqual(service.list_books_page(1)[1], 1)
                self.assertEqual(service.list_memos(book['id']), [])
                self.assertEqual(service.list_available_tags(), [])
                service.init_db()
            old_path = service.DB_PATH
            service.DB_PATH = Path(tmp) / 'override' / 'new.sqlite3'
            self.assertEqual(service.list_books(), [])
            self.assertTrue(service.DB_PATH.exists())
            service.DB_PATH = old_path
            self.assertEqual(len(service.list_books()), 1)

    def test_progress_and_memo_edit_are_versioned_atomic_and_retry_safe(self):
        with tempfile.TemporaryDirectory() as tmp, self.loaded(tmp):
            from app.services import book_service as service
            book = service.create_or_get_book({'isbn': 'one', 'title': '책'})
            other = service.create_or_get_book({'isbn': 'two', 'title': '다른 책'})
            service.create_chapter(book['id'], '목차')
            service.create_chapter(other['id'], '다른 책 목차')
            chapter = service.list_chapters(book['id'])[0]
            other_chapter = service.list_chapters(other['id'])[0]
            version = service.get_book(book['id'])['version']
            args = (book['id'], '읽는 중', 22, '작성한 장', 30)
            service.update_progress(*args, expected_version=version, request_id='a'*32)
            service.update_progress(*args, expected_version=version, request_id='a'*32)
            with self.assertRaises(service.WriteConflict):
                service.update_progress(book['id'], '보류', 10, '과거', 10, expected_version=version)
            service.create_memo(book['id'], chapter['id'], '제목', '본문', 3, tags='보존, 태그')
            memo = service.list_memos(book['id'])[0]
            edit = (memo['id'], '새 제목', '새 본문', 8, chapter['id'])
            service.update_memo(*edit, expected_version=1, request_id='b'*32)
            service.update_memo(*edit, expected_version=1, request_id='b'*32)
            self.assertEqual(service.list_memos(book['id'])[0]['tags'], ['보존', '태그'])
            with self.assertRaises(service.WriteConflict):
                service.update_memo(memo['id'], '과거', '과거 본문', 0, None, expected_version=1)
            with self.assertRaises(ValueError):
                service.update_memo(memo['id'], '잘못된 연결', '본문', 9, other_chapter['id'], expected_version=2)
            saved = service.list_memos(book['id'])[0]
            self.assertEqual((saved['title'], saved['content'], saved['page'], saved['chapter_id'], saved['version']),
                             ('새 제목', '새 본문', 8, chapter['id'], 2))
            service.update_memo_tags(memo['id'], '최신 태그')
            with self.assertRaises(service.WriteConflict):
                service.update_memo(*edit, expected_version=2)

    def test_progress_route_requires_revision_and_edit_form_preserves_conflict_inputs(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DELETE_PASSWORD': 'test'}), self.loaded(tmp) as app:
            from app.services import book_service as service
            book = service.create_or_get_book({'isbn': 'forms', 'title': '책'})
            service.create_memo(book['id'], None, '메모', '본문', 0)
            memo = service.list_memos(book['id'])[0]
            with TestClient(app, base_url='https://books.len.pe.kr') as client:
                headers = {'Origin': 'https://books.len.pe.kr'}
                client.post('/auth/login', data={'password': 'test'}, headers=headers)
                html = client.get(f"/books/{book['id']}").text
                self.assertIn('data-draft-id="book-progress-', html)
                self.assertIn('data-draft-id="memo-edit-', html)
                self.assertIn('data-draft-fields="memo_title content page chapter_id"', html)
                self.assertEqual(client.post(f"/books/{book['id']}/progress", data={'reading_status':'읽는 중'}, headers=headers).status_code, 409)
                edit = {'memo_title':'최신', 'content':'유지할 본문', 'expected_version':1, 'request_id':'a'*32}
                self.assertEqual(client.post(f"/memos/{memo['id']}", data=edit, headers=headers, follow_redirects=False).status_code, 303)
                self.assertEqual(client.post(f"/memos/{memo['id']}", data={**edit, 'content':'과거 초안', 'request_id':'b'*32}, headers=headers).status_code, 409)
                self.assertEqual(service.list_memos(book['id'])[0]['content'], '유지할 본문')

    def test_import_preview_commit_remaps_ids_skips_duplicates_and_preserves_originals(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DELETE_PASSWORD': 'test'}), self.loaded(tmp) as app:
            from app.services import book_service as service, export_service, import_service
            book = service.create_or_get_book({'isbn':'original', 'title':'원본'})
            service.create_chapter(book['id'], '원본 목차')
            chapter = service.list_chapters(book['id'])[0]
            service.create_memo(book['id'], chapter['id'], '원본 메모', '원본 본문', 4, tags='보존')
            original = export_service.export_records()
            new = copy.deepcopy(original['records'][0])
            new.update(id=91, isbn='new', title='가져온 책')
            new['chapters'][0].update(id=92, book_id=91)
            new['memos'][0].update(id=93, book_id=91, chapter_id=92)
            payload = {**original, 'records':[original['records'][0], new]}
            raw = json.dumps(payload, ensure_ascii=False)
            with TestClient(app, base_url='https://books.len.pe.kr') as client:
                headers = {'Origin':'https://books.len.pe.kr', 'Content-Type':'application/json'}
                self.assertEqual(client.post('/api/import/preview', content=raw, headers=headers).status_code, 401)
                client.post('/auth/login', data={'password':'test'}, headers={'Origin':headers['Origin']})
                self.assertEqual(client.post('/api/import/commit', content=raw, headers=headers).status_code, 409)
                preview = client.post('/api/import/preview', content=raw, headers=headers).json()
                self.assertEqual((preview['new_count'], preview['skip_count']), (1,1))
                self.assertEqual(export_service.export_records(), original)
                commit = client.post('/api/import/commit', content=raw, headers={**headers, 'X-Import-Preview':preview['preview_token']})
                self.assertEqual(commit.status_code, 200)
                self.assertEqual(commit.json()['imported_count'], 1)
                records = export_service.export_records()['records']
                self.assertEqual(records[0], original['records'][0])
                imported = records[1]
                self.assertNotEqual(imported['id'], 91)
                self.assertEqual(imported['memos'][0]['chapter_id'], imported['chapters'][0]['id'])
                self.assertEqual(imported['memos'][0]['tags'], ['보존'])
                self.assertEqual(client.post('/api/import/commit', content=raw, headers={**headers, 'X-Import-Preview':preview['preview_token']}).json()['skip_count'], 2)
                self.assertEqual(len(service.list_books()), 2)
                bad = copy.deepcopy(payload)
                bad['records'][0]['memos'][0]['chapter_id'] = 92
                before = export_service.export_records()
                self.assertEqual(client.post('/api/import/preview', json=bad, headers=headers).status_code, 400)
                self.assertEqual(export_service.export_records(), before)
                changed_raw = json.dumps({**payload, 'records':[new]})
                self.assertEqual(client.post('/api/import/commit', content=changed_raw, headers={**headers, 'X-Import-Preview':preview['preview_token']}).status_code, 409)
                with patch.object(import_service.time, 'time', return_value=import_service.time.time()+601):
                    self.assertEqual(client.post('/api/import/commit', content=raw, headers={**headers, 'X-Import-Preview':preview['preview_token']}).status_code, 409)
                client.post('/auth/login', data={'password':'test'}, headers={'Origin':headers['Origin']})
                self.assertEqual(client.post('/api/import/commit', content=raw, headers={**headers, 'X-Import-Preview':preview['preview_token']}).status_code, 409)
                self.assertEqual(client.post('/api/import/preview', content=' '*(import_service.MAX_IMPORT_BYTES+1), headers=headers).status_code, 413)

    def test_import_invalid_records_and_transaction_failure_make_no_partial_writes(self):
        with tempfile.TemporaryDirectory() as tmp, self.loaded(tmp):
            from app.services import book_service as service, export_service, import_service
            book = service.create_or_get_book({'isbn':'seed', 'title':'원본'})
            service.create_memo(book['id'], None, '메모', '내용', 0)
            original = export_service.export_records()
            records = []
            for n in (1,2):
                item = copy.deepcopy(original['records'][0])
                item.update(id=90+n, isbn=f'new-{n}')
                item['memos'][0].update(id=100+n, book_id=90+n, title='실패' if n==2 else '성공')
                records.append(item)
            payload = {**original, 'records':records}
            with service._connect() as connection:
                connection.execute("CREATE TRIGGER reject_test_memo BEFORE INSERT ON book_memos WHEN NEW.title = '실패' BEGIN SELECT RAISE(ABORT, 'injected'); END")
            with self.assertRaises(sqlite3.IntegrityError):
                import_service.commit_import(payload)
            self.assertEqual(export_service.export_records(), original)
            invalid = copy.deepcopy(payload)
            invalid['records'][1]['memos'][0]['book_id'] = 91
            with self.assertRaises(ValueError):
                import_service.commit_import(invalid)
            self.assertEqual(export_service.export_records(), original)

    def test_home_import_link_guides_guest_to_login_and_authenticated_user_to_import(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DELETE_PASSWORD':'test'}), self.loaded(tmp) as app:
            with TestClient(app, base_url='https://books.len.pe.kr') as client:
                guest = client.get('/').text
                self.assertIn('href="/auth/login?next_path=%2Fimport">로그인 후 가져오기</a>', guest)
                self.assertNotIn('href="/import">JSON 가져오기</a>', guest)
                self.assertEqual(client.get('/import').status_code, 401)
                client.post('/auth/login', data={'password':'test'}, headers={'Origin':'https://books.len.pe.kr'})
                authenticated = client.get('/').text
                self.assertIn('href="/import">JSON 가져오기</a>', authenticated)
                self.assertNotIn('href="/auth/login?next_path=%2Fimport">로그인 후 가져오기</a>', authenticated)
                self.assertEqual(client.get('/import').status_code, 200)

    def test_startup_initializes_fresh_database_before_readiness_can_gate_traffic(self):
        with tempfile.TemporaryDirectory() as tmp, self.loaded(tmp) as app:
            from app.services import book_service as service
            self.assertFalse(service.DB_PATH.exists())
            with TestClient(app) as client:
                self.assertTrue(service.DB_PATH.exists())
                self.assertEqual(client.get('/ready').status_code, 200)

    def test_startup_migrates_legacy_schema_preserving_rows_before_ready(self):
        with tempfile.TemporaryDirectory() as tmp, self.loaded(tmp) as app:
            from app.services import book_service as service
            book = service.create_or_get_book({'isbn':'startup', 'title':'보존할 책'})
            service.create_chapter(book['id'], '보존할 목차')
            chapter = service.list_chapters(book['id'])[0]
            service.create_memo(book['id'], chapter['id'], '보존할 메모', '보존할 내용', 9, tags='보존')
            with service._connect() as connection:
                for table in ('books','book_chapters','book_memos'):
                    connection.execute(f'ALTER TABLE {table} DROP COLUMN version')
                connection.execute('DROP TABLE write_requests')
            with TestClient(app) as client:
                self.assertEqual(client.get('/ready').status_code, 200)
                self.assertEqual(service.get_book(book['id'])['title'], '보존할 책')
                self.assertEqual(service.list_chapters(book['id'])[0]['title'], '보존할 목차')
                memo = service.list_memos(book['id'])[0]
                self.assertEqual((memo['content'], memo['page'], memo['tags'], memo['version']), ('보존할 내용',9,['보존'],1))

    def test_schema_initialization_failure_aborts_startup(self):
        with tempfile.TemporaryDirectory() as tmp, self.loaded(tmp) as app:
            from app.services import book_service as service
            with patch.object(service, 'init_db', side_effect=RuntimeError('injected migration failure')):
                with self.assertRaisesRegex(RuntimeError, 'migration failure'):
                    with TestClient(app):
                        self.fail('Startup must fail before accepting traffic')

    def test_readiness_is_local_read_only_and_liveness_is_fixed(self):
        with tempfile.TemporaryDirectory() as tmp, self.loaded(tmp) as app:
            from app.services import book_service as service
            client = TestClient(app)
            self.assertEqual(client.get('/ready').status_code, 503)
            self.assertFalse(service.DB_PATH.exists())
            service.init_db()
            with sqlite3.connect(service.DB_PATH) as writer:
                writer.execute('BEGIN IMMEDIATE')
                self.assertEqual(client.get('/ready').status_code, 200)
            from app.services import readiness
            with patch.object(readiness.os, 'access', return_value=False):
                self.assertEqual(client.get('/ready').status_code, 503)
            with service._connect() as connection:
                connection.execute('DROP TABLE book_memos')
            self.assertEqual(client.get('/ready').status_code, 503)
            self.assertEqual(client.get('/health').status_code, 200)
            service.DB_PATH.write_bytes(b'corrupt database')
            self.assertEqual(client.get('/ready').status_code, 503)
            self.assertEqual(client.get('/health').status_code, 200)
