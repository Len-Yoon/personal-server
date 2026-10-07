import copy
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from tests.youtube_memo import test_ui_contract


class YouTubeAuditEnhancementTests(unittest.TestCase):
    def loaded(self, path):
        return test_ui_contract.YoutubeMemoUiContractTests().loaded_app(path)

    def test_initialized_reads_do_not_take_writer_lock_and_support_path_overrides(self):
        with tempfile.TemporaryDirectory() as tmp, self.loaded(tmp):
            from app.services import memo_service as service
            video = service.create_or_get_video('dQw4w9WgXcQ', title_fetcher=lambda *_:'영상')
            with sqlite3.connect(service.DB_PATH) as writer:
                writer.execute('BEGIN IMMEDIATE')
                self.assertEqual(service.get_video(video['id'])['title'], '영상')
                self.assertEqual(service.list_videos_page(1)[1], 1)
                self.assertEqual(service.list_memos(video['id']), [])
                self.assertEqual(service.list_available_tags(), [])
                self.assertEqual(len(service.list_export_records()), 1)
                service.init_db()
            old_path = service.DB_PATH
            service.DB_PATH = Path(tmp) / 'override' / 'new.sqlite3'
            self.assertEqual(service.list_videos(), [])
            self.assertTrue(service.DB_PATH.exists())
            service.DB_PATH = old_path
            self.assertEqual(len(service.list_videos()), 1)

    def test_import_preview_commit_remaps_ids_skips_duplicates_and_preserves_originals(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DELETE_PASSWORD':'test'}), self.loaded(tmp) as app:
            from app.services import memo_service as service, export_service, import_service
            video = service.create_or_get_video('dQw4w9WgXcQ', title_fetcher=lambda *_:'원본')
            service.create_memo(video['id'], '메모', '원본 본문 01:23', tags='보존')
            original = json.loads(export_service.export_json(service.list_export_records()))
            new = copy.deepcopy(original['records'][0])
            new.update(id=91, youtube_id='abcdefghijk', url='https://www.youtube.com/watch?v=abcdefghijk', title='가져온 영상')
            new['memos'][0].update(id=92, video_id=91)
            payload = {**original, 'records':[original['records'][0],new],
                       'metadata':{**original['metadata'], 'video_count':2, 'memo_count':2}}
            raw = json.dumps(payload, ensure_ascii=False)
            with TestClient(app, base_url='https://memo.len.pe.kr') as client:
                headers = {'Origin':'https://memo.len.pe.kr', 'Content-Type':'application/json'}
                self.assertEqual(client.post('/api/import/preview', content=raw, headers=headers).status_code, 401)
                client.post('/auth/login', data={'password':'test'}, headers={'Origin':headers['Origin']})
                self.assertEqual(client.post('/api/import/commit', content=raw, headers=headers).status_code, 409)
                preview = client.post('/api/import/preview', content=raw, headers=headers).json()
                self.assertEqual((preview['new_count'],preview['skip_count']), (1,1))
                self.assertEqual(json.loads(export_service.export_json(service.list_export_records())), original)
                commit = client.post('/api/import/commit', content=raw, headers={**headers,'X-Import-Preview':preview['preview_token']})
                self.assertEqual(commit.status_code, 200)
                self.assertEqual(commit.json()['imported_count'], 1)
                records = service.list_export_records()
                self.assertEqual(records[0], original['records'][0])
                imported = records[1]
                self.assertNotEqual(imported['id'], 91)
                self.assertEqual(imported['memos'][0]['video_id'], imported['id'])
                self.assertEqual(imported['memos'][0]['tags'], ['보존'])
                self.assertEqual(client.post('/api/import/commit', content=raw, headers={**headers,'X-Import-Preview':preview['preview_token']}).json()['skip_count'], 2)
                before = service.list_export_records()
                bad = copy.deepcopy(payload)
                bad['records'][1]['memos'][0]['video_id'] = 1
                self.assertEqual(client.post('/api/import/preview', json=bad, headers=headers).status_code, 400)
                self.assertEqual(service.list_export_records(), before)
                self.assertEqual(client.post('/api/import/commit', content=raw+' ', headers={**headers,'X-Import-Preview':preview['preview_token']}).status_code, 409)
                with patch.object(import_service.time, 'time', return_value=import_service.time.time()+601):
                    self.assertEqual(client.post('/api/import/commit', content=raw, headers={**headers,'X-Import-Preview':preview['preview_token']}).status_code, 409)
                client.post('/auth/login', data={'password':'test'}, headers={'Origin':headers['Origin']})
                self.assertEqual(client.post('/api/import/commit', content=raw, headers={**headers,'X-Import-Preview':preview['preview_token']}).status_code, 409)
                self.assertEqual(client.post('/api/import/preview', content=' '*(import_service.MAX_IMPORT_BYTES+1), headers=headers).status_code, 413)

    def test_import_failure_and_invalid_duplicate_record_roll_back_every_record(self):
        with tempfile.TemporaryDirectory() as tmp, self.loaded(tmp):
            from app.services import memo_service as service, export_service, import_service
            video = service.create_or_get_video('dQw4w9WgXcQ', title_fetcher=lambda *_:'원본')
            service.create_memo(video['id'], '메모', '내용')
            original = json.loads(export_service.export_json(service.list_export_records()))
            records = []
            for n, youtube_id in enumerate(('abcdefghijk','lmnopqrstuv'),1):
                item = copy.deepcopy(original['records'][0])
                item.update(id=90+n, youtube_id=youtube_id, url=f'https://www.youtube.com/watch?v={youtube_id}')
                item['memos'][0].update(id=100+n, video_id=90+n, title='실패' if n==2 else '성공')
                records.append(item)
            payload = {**original,'records':records,'metadata':{**original['metadata'],'video_count':2,'memo_count':2}}
            with service._connect() as connection:
                connection.execute("CREATE TRIGGER reject_test_memo BEFORE INSERT ON memos WHEN NEW.title = '실패' BEGIN SELECT RAISE(ABORT, 'injected'); END")
            with self.assertRaises(sqlite3.IntegrityError):
                import_service.commit_import(payload)
            self.assertEqual(json.loads(export_service.export_json(service.list_export_records())), original)
            bad = copy.deepcopy(original)
            bad['records'][0]['memos'][0]['video_id'] = 999
            with self.assertRaises(ValueError):
                import_service.commit_import(bad)
            self.assertEqual(json.loads(export_service.export_json(service.list_export_records())), original)

    def test_home_import_link_guides_guest_to_login_and_authenticated_user_to_import(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DELETE_PASSWORD':'test'}), self.loaded(tmp) as app:
            with TestClient(app, base_url='https://memo.len.pe.kr') as client:
                guest = client.get('/').text
                self.assertIn('href="/auth/login?next_path=%2Fimport">로그인 후 가져오기</a>', guest)
                self.assertNotIn('href="/import">JSON 가져오기</a>', guest)
                self.assertEqual(client.get('/import').status_code, 401)
                client.post('/auth/login', data={'password':'test'}, headers={'Origin':'https://memo.len.pe.kr'})
                authenticated = client.get('/').text
                self.assertIn('href="/import">JSON 가져오기</a>', authenticated)
                self.assertNotIn('href="/auth/login?next_path=%2Fimport">로그인 후 가져오기</a>', authenticated)
                self.assertEqual(client.get('/import').status_code, 200)

    def test_startup_initializes_fresh_database_before_readiness_can_gate_traffic(self):
        with tempfile.TemporaryDirectory() as tmp, self.loaded(tmp) as app:
            from app.services import memo_service as service
            self.assertFalse(service.DB_PATH.exists())
            with TestClient(app) as client:
                self.assertTrue(service.DB_PATH.exists())
                self.assertEqual(client.get('/ready').status_code, 200)

    def test_startup_migrates_legacy_schema_preserving_rows_before_ready(self):
        with tempfile.TemporaryDirectory() as tmp, self.loaded(tmp) as app:
            from app.services import memo_service as service
            video = service.create_or_get_video('dQw4w9WgXcQ', title_fetcher=lambda *_:'보존할 영상')
            service.create_memo(video['id'], '보존할 메모', '보존할 내용', tags='보존')
            with service._connect() as connection:
                connection.execute('ALTER TABLE memos DROP COLUMN version')
                connection.execute('DROP TABLE write_requests')
            with TestClient(app) as client:
                self.assertEqual(client.get('/ready').status_code, 200)
                self.assertEqual(service.get_video(video['id'])['title'], '보존할 영상')
                memo = service.list_memos(video['id'])[0]
                self.assertEqual((memo['content'], memo['tags'], memo['version']), ('보존할 내용',['보존'],1))

    def test_schema_initialization_failure_aborts_startup(self):
        with tempfile.TemporaryDirectory() as tmp, self.loaded(tmp) as app:
            from app.services import memo_service as service
            with patch.object(service, 'init_db', side_effect=RuntimeError('injected migration failure')):
                with self.assertRaisesRegex(RuntimeError, 'migration failure'):
                    with TestClient(app):
                        self.fail('Startup must fail before accepting traffic')

    def test_readiness_checks_local_database_without_mutating_it_and_health_is_fixed(self):
        with tempfile.TemporaryDirectory() as tmp, self.loaded(tmp) as app:
            from app.services import memo_service as service
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
                connection.execute('DROP TABLE memos')
            self.assertEqual(client.get('/ready').status_code, 503)
            self.assertEqual(client.get('/health').status_code, 200)
            service.DB_PATH.write_bytes(b'corrupt database')
            self.assertEqual(client.get('/ready').status_code, 503)
            self.assertEqual(client.get('/health').status_code, 200)
