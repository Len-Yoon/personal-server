import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from tests.youtube_memo import test_ui_contract


class MemoRecoveryTests(unittest.TestCase):
    def loaded(self, path):
        return test_ui_contract.YoutubeMemoUiContractTests().loaded_app(path)

    def seed(self, service):
        parent = service.create_or_get_video('dQw4w9WgXcQ', title_fetcher=lambda *_: '영상')
        service.create_memo(parent['id'], '원제목', '원본문', tags='태그')
        return parent, service.list_memos(parent['id'])[0]

    def edit(self, service, memo, content, version, request_id=''):
        return service.update_memo(memo['id'], '새제목', content, expected_version=version, request_id=request_id)

    def test_history_limit_retries_conflicts_and_recovery_preserve_data(self):
        with tempfile.TemporaryDirectory() as tmp, self.loaded(tmp):
            from app.services import memo_service as service
            parent, memo = self.seed(service)
            token = uuid4().hex
            self.edit(service, memo, '수정본문', 1, token)
            self.edit(service, memo, '수정본문', 1, token)
            history, deleted = service.memo_recovery_state(parent['id'])
            self.assertEqual(len(history[memo['id']]), 1)
            self.assertEqual(history[memo['id']][0]['content'], '원본문')
            with self.assertRaises(service.WriteConflict):
                self.edit(service, memo, '과거 수정', 1)
            with self.assertRaises(service.WriteConflict):
                service.delete_memo(memo['id'], expected_version=1)
            self.assertEqual(service.memo_recovery_state(parent['id'])[1], [])
            for version in range(2, 53):
                self.edit(service, memo, f'본문 {version}', version)
            history, _ = service.memo_recovery_state(parent['id'])
            self.assertEqual(len(history[memo['id']]), 50)
            before = service.list_memos(parent['id'])[0]
            token = uuid4().hex
            service.delete_memo(memo['id'], expected_version=53, request_id=token)
            service.delete_memo(memo['id'], expected_version=53, request_id=token)
            self.assertEqual(service.list_memos(parent['id']), [])
            self.assertEqual(service.list_export_records()[0]['memos'], [])
            history, deleted = service.memo_recovery_state(parent['id'])
            self.assertEqual(deleted[0]['content'], before['content'])
            with self.assertRaises(service.WriteConflict):
                service.restore_memo(memo['id'], expected_version=53)
            with self.assertRaises(service.WriteConflict):
                service.restore_memo(memo['id'], expected_version=None)
            token = uuid4().hex
            service.restore_memo(memo['id'], expected_version=54, request_id=token)
            service.restore_memo(memo['id'], expected_version=54, request_id=token)
            after = service.list_memos(parent['id'])[0]
            for field in ['id', 'title', 'content', 'tags', 'created_at']:
                self.assertEqual(after[field], before[field])
            self.assertEqual(after['version'], 55)
            self.assertEqual(service.memo_recovery_state(parent['id'])[1], [])
            with self.assertRaises(service.WriteConflict):
                self.edit(service, memo, '삭제 전 열린 화면', 53)
            # A stale restore cannot replace a later deletion.
            service.delete_memo(memo['id'], expected_version=55)
            with self.assertRaises(service.WriteConflict):
                service.restore_memo(memo['id'], expected_version=54)
            self.assertEqual(service.memo_recovery_state(parent['id'])[1][0]['version'], 56)

    def test_restore_auth_origin_version_and_html_escaping(self):
        origin = 'https://memo.len.pe.kr'
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DELETE_PASSWORD': 'test'}), self.loaded(tmp) as app:
            from app.services import memo_service as service
            parent, memo = self.seed(service)
            self.edit(service, memo, '<script>danger</script>', 1)
            with TestClient(app, base_url=origin) as client:
                detail = client.get(f'/videos/{parent["id"]}')
                self.assertEqual(detail.status_code, 200)
                self.assertNotIn('수정 이력', detail.text)
                service.delete_memo(memo['id'], expected_version=2)
                url = f'/memos/{memo["id"]}/restore'
                data = {'expected_version': 3, 'request_id': uuid4().hex}
                headers = {'Origin': origin}
                self.assertEqual(client.post(url, data=data, headers=headers).status_code, 401)
                client.post('/auth/login', data={'password': 'test'}, headers=headers)
                self.assertEqual(client.post(url, data=data, headers={'Origin': 'https://evil.invalid'}).status_code, 403)
                self.assertEqual(client.post(url, data={'expected_version': 2}, headers=headers).status_code, 409)
                self.assertEqual(client.post(url, headers=headers).status_code, 409)
                deleted_page = client.get(f'/videos/{parent["id"]}')
                self.assertIn('&lt;script&gt;danger&lt;/script&gt;', deleted_page.text)
                self.assertIn('메모 복구', deleted_page.text)
                self.assertEqual(client.post(url, data=data, headers=headers, follow_redirects=False).status_code, 303)

    def test_additive_migration_preserves_data_and_readiness_reads_without_writer(self):
        with tempfile.TemporaryDirectory() as tmp, self.loaded(tmp):
            from app.services import memo_service as service
            parent, memo = self.seed(service)
            with sqlite3.connect(service.DB_PATH) as db:
                db.execute('DROP TABLE memo_revisions')
                db.execute('DROP TABLE deleted_memos')
            service.init_db()
            self.assertEqual(service.list_memos(parent['id'])[0]['content'], memo['content'])
            with sqlite3.connect(service.DB_PATH) as writer:
                writer.execute('BEGIN IMMEDIATE')
                self.assertEqual(service.memo_recovery_state(parent['id']), ({}, []))
                service.init_db()

    def test_delete_storage_failure_is_atomic_and_parent_delete_is_outside_recovery(self):
        with tempfile.TemporaryDirectory() as tmp, self.loaded(tmp):
            from app.services import memo_service as service
            parent, memo = self.seed(service)
            with sqlite3.connect(service.DB_PATH) as db:
                db.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON memos BEGIN SELECT RAISE(ABORT, 'test failure'); END")
            with self.assertRaises(sqlite3.IntegrityError):
                service.delete_memo(memo['id'], expected_version=1)
            self.assertEqual(service.list_memos(parent['id'])[0]['tags'], ['태그'])
            self.assertEqual(service.memo_recovery_state(parent['id'])[1], [])
            with sqlite3.connect(service.DB_PATH) as db:
                db.execute('DROP TRIGGER reject_delete')
            self.edit(service, memo, '수정', 1)
            service.delete_memo(memo['id'], expected_version=2)
            with service._connect() as db:
                db.execute('DELETE FROM videos WHERE id = ?', (parent['id'],))
            self.assertEqual(service.memo_recovery_state(parent['id']), ({}, []))
            self.assertIsNone(service.restore_memo(memo['id'], expected_version=3))

    def test_previous_body_restore_is_versioned_retry_safe_and_preserves_current_body(self):
        origin = 'https://memo.len.pe.kr'
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DELETE_PASSWORD': 'test'}), self.loaded(tmp) as app:
            from app.services import memo_service as service
            parent, memo = self.seed(service)
            self.edit(service, memo, '복원 전 본문', 1)
            revision = service.memo_recovery_state(parent['id'])[0][memo['id']][0]
            with self.assertRaises(service.WriteConflict):
                service.restore_memo_revision(memo['id'], revision['revision_id'], expected_version=None)
            with TestClient(app, base_url=origin) as client:
                url = f'/memos/{memo["id"]}/revisions/{revision["revision_id"]}/restore'
                data = {'expected_version': 2, 'request_id': uuid4().hex}
                headers = {'Origin': origin}
                self.assertEqual(client.post(url, data=data, headers=headers).status_code, 401)
                client.post('/auth/login', data={'password': 'test'}, headers=headers)
                self.assertEqual(client.post(url, data=data, headers={'Origin':'https://evil.invalid'}).status_code, 403)
                self.assertEqual(client.post(url, headers=headers).status_code, 409)
                wrong_url = f'/memos/{memo["id"] + 1}/revisions/{revision["revision_id"]}/restore'
                self.assertEqual(client.post(wrong_url, data=data, headers=headers).status_code, 404)
                self.assertEqual(client.post(url, data=data, headers=headers, follow_redirects=False).status_code, 303)
                self.assertEqual(client.post(url, data=data, headers=headers, follow_redirects=False).status_code, 303)
                self.assertEqual(client.post(url, data={**data, 'request_id':uuid4().hex}, headers=headers).status_code, 409)
                restored = service.list_memos(parent['id'])[0]
                self.assertEqual(restored['content'], '원본문')
                self.assertEqual(restored['title'], '원제목')
                self.assertEqual(restored['tags'], ['태그'])
                self.assertEqual(restored['version'], 3)
                history = service.memo_recovery_state(parent['id'])[0][memo['id']]
                self.assertEqual(len(history), 2)
                self.assertEqual(history[0]['content'], '복원 전 본문')
                self.assertEqual(client.get(f'/videos/{parent["id"]}').status_code, 200)
            # An existing different memo cannot use this memo's revision.
            service.create_memo(parent["id"], "다른 메모", "내용")
            other = service.list_memos(parent['id'])[0]
            self.assertIsNone(service.restore_memo_revision(other['id'], revision['revision_id'], expected_version=1))
            self.assertEqual(service.list_memos(parent['id'])[0]['content'], '내용')

    def test_recovery_contents_are_only_loaded_and_rendered_for_authenticated_readers(self):
        origin = 'https://memo.len.pe.kr'
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DELETE_PASSWORD':'test'}), self.loaded(tmp) as app:
            from app.services import memo_service as service
            from app import main
            parent, memo = self.seed(service)
            self.edit(service, memo, 'private-history-marker', 1)
            self.edit(service, memo, 'public-current-marker', 2)
            service.create_memo(parent["id"], "삭제전용", "private-deleted-marker")
            deleted = service.list_memos(parent['id'])[0]
            service.delete_memo(deleted['id'], expected_version=1)
            url = f'/videos/{parent["id"]}'
            with TestClient(app, base_url=origin) as client:
                with patch.object(main, 'memo_recovery_state', wraps=service.memo_recovery_state) as recovery:
                    public = client.get(url)
                    self.assertEqual(public.status_code, 200)
                    recovery.assert_not_called()
                    self.assertIn('public-current-marker', public.text)
                    for marker in ['private-history-marker', 'private-deleted-marker', '수정 이력', '삭제한 메모 복구']:
                        self.assertNotIn(marker, public.text)
                    client.post('/auth/login', data={'password':'test'}, headers={'Origin':origin})
                    authenticated = client.get(url)
                    self.assertEqual(authenticated.status_code, 200)
                    recovery.assert_called_once_with(parent['id'])
                    self.assertIn('private-history-marker', authenticated.text)
                    self.assertIn('private-deleted-marker', authenticated.text)
                    client.post('/auth/logout', headers={'Origin':origin})
                    public_again = client.get(url)
                    self.assertNotIn('private-history-marker', public_again.text)
                    self.assertNotIn('private-deleted-marker', public_again.text)

    def test_trash_pages_measure_stored_utf8_json_and_cleanup_is_atomic_retry_safe(self):
        with tempfile.TemporaryDirectory() as tmp, self.loaded(tmp):
            from app.services import memo_service as service
            parent, memo = self.seed(service)
            self.edit(service, memo, '수정 내용', 1)
            service.delete_memo(memo['id'], expected_version=2)
            with service._connect() as db:
                original = db.execute('SELECT * FROM deleted_memos').fetchone()
                # Exercise UTF-8 byte counts with unescaped Korean stored JSON.
                import json
                snapshot = json.dumps(json.loads(original['snapshot']), ensure_ascii=False)
                db.execute('UPDATE deleted_memos SET snapshot = ?', (snapshot,))
                for number in range(100, 122):
                    body = json.loads(snapshot)
                    body['id'] = number
                    db.execute('INSERT INTO deleted_memos (memo_id, video_id, snapshot, tags, version) VALUES (?, ?, ?, ?, ?)',
                               (number, parent['id'], json.dumps(body, ensure_ascii=False), original['tags'], 3))
                expected_bytes = db.execute('SELECT SUM(length(CAST(snapshot AS BLOB)) + length(CAST(tags AS BLOB))) FROM deleted_memos').fetchone()[0]
            summary = service.memo_trash_summary(parent['id'], page=99)
            self.assertEqual((summary['total_count'], summary['page'], summary['pages'], summary['total_bytes']), (23, 2, 2, expected_bytes))
            self.assertEqual(len(service.memo_recovery_state(parent['id'])[1]), 20)
            self.assertEqual(len(service.memo_recovery_state(parent['id'], trash_page=2)[1]), 3)
            with self.assertRaises(service.WriteConflict):
                service.purge_deleted_memos(parent['id'], [(memo['id'], 3), (100, 2)], uuid4().hex)
            self.assertEqual(service.memo_trash_summary(parent['id'])['total_count'], 23)
            for selected in [[], [(memo['id'], 3)] * 2, [(100, 3)] * 21, [(True, 3)], [(100, None)], [(999, 3)]]:
                with self.assertRaises(ValueError):
                    service.purge_deleted_memos(parent['id'], selected, uuid4().hex)
            other_parent = service.create_or_get_video('abcdefghijk', title_fetcher=lambda *_: '다른 영상')
            with self.assertRaises(service.WriteConflict):
                service.purge_deleted_memos(other_parent['id'], [(memo['id'], 3)], uuid4().hex)
            self.assertEqual(service.memo_trash_summary(other_parent['id']),
                             {'total_count': 0, 'total_bytes': 0, 'page': 1, 'pages': 1, 'page_size': 20})
            with self.assertRaises(ValueError):
                service.purge_deleted_memos(parent['id'], [(memo['id'], 3)], '')
            with patch.object(service, 'record_result', side_effect=RuntimeError('failure after deletion')):
                with self.assertRaises(RuntimeError):
                    service.purge_deleted_memos(parent['id'], [(memo['id'], 3)], uuid4().hex)
            self.assertEqual(service.memo_trash_summary(parent['id'])['total_count'], 23)
            token = uuid4().hex
            self.assertEqual(service.purge_deleted_memos(parent['id'], [(memo['id'], 3)], token), parent['id'])
            self.assertEqual(service.purge_deleted_memos(parent['id'], [(memo['id'], 3)], token), parent['id'])
            with self.assertRaises(service.WriteConflict):
                service.purge_deleted_memos(parent['id'], [(100, 3)], token)
            with service._connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM memo_revisions WHERE memo_id = ?', (memo['id'],)).fetchone()[0], 0)
            self.assertEqual(service.memo_trash_summary(parent['id'])['total_count'], 22)

    def test_trash_cleanup_requires_auth_origin_and_valid_selection(self):
        import json
        origin = 'https://video.len.pe.kr'
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DELETE_PASSWORD': 'test'}), self.loaded(tmp) as app:
            from app.services import memo_service as service
            parent, memo = self.seed(service)
            service.delete_memo(memo['id'], expected_version=1)
            url = f'/videos/{parent["id"]}/trash/purge'
            data = {'selection': json.dumps([[memo['id'], 2]]), 'request_id': uuid4().hex}
            headers = {'Origin': origin}
            with TestClient(app, base_url=origin) as client:
                self.assertEqual(client.post(url, data=data, headers=headers).status_code, 401)
                client.post('/auth/login', data={'password': 'test'}, headers=headers)
                self.assertEqual(client.post(url, data=data, headers={'Origin': 'https://evil.invalid'}).status_code, 403)
                for selection in ['{}', 'null', '[[1]]', '[[true, 2]]', 'not-json']:
                    self.assertEqual(client.post(url, data={**data, 'selection': selection}, headers=headers).status_code, 400)
                self.assertEqual(client.post(url, data={'selection': data['selection']}, headers=headers).status_code, 400)
                self.assertEqual(client.post(url, data={**data, 'selection': json.dumps([[memo['id'], 1]])}, headers=headers).status_code, 409)
                html = client.get(f'/videos/{parent["id"]}').text
                self.assertIn('영구 삭제', html)
                self.assertIn('JSON 저장 내용', html)
                self.assertEqual(client.post(url, data=data, headers=headers, follow_redirects=False).status_code, 303)
                self.assertEqual(client.post(url, data=data, headers=headers, follow_redirects=False).status_code, 303)
