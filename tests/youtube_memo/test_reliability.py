import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from tests.youtube_memo import test_ui_contract


class YoutubeReliabilityTests(unittest.TestCase):
    def test_old_schema_and_parallel_retry_rollback_preserve_original_rows(self):
        with tempfile.TemporaryDirectory() as tmp, test_ui_contract.YoutubeMemoUiContractTests().loaded_app(tmp):
            from app.services import memo_service as service
            video = service.create_or_get_video('https://youtu.be/dQw4w9WgXcQ', title_fetcher=lambda *_: '기존 영상')
            original = service.create_memo(video['id'], '기존 메모', '보존할 내용', tags='기존')
            with service._connect() as connection:
                connection.execute('ALTER TABLE memos DROP COLUMN version')
                connection.execute('DROP TABLE write_requests')
            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(lambda _: service.init_db(), range(4)))
            self.assertEqual(service.list_memos(video['id'])[0]['content'], '보존할 내용')
            self.assertEqual(service.list_memos(video['id'])[0]['version'], 1)
            with patch.object(service, 'record_result', side_effect=RuntimeError('injected transaction failure')):
                with self.assertRaises(RuntimeError):
                    service.create_memo(video['id'], '신규', '신규 내용', tags='신규', request_id='a'*32)
            self.assertEqual(len(service.list_memos(video['id'])), 1)
            with service._connect() as connection:
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM memo_tags').fetchone()[0], 1)
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM write_requests').fetchone()[0], 0)
            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(lambda _: service.create_memo(video['id'], '신규', '신규 내용', request_id='a'*32), range(4)))
            self.assertEqual(len(service.list_memos(video['id'])), 2)
            def edit(content):
                try:
                    service.update_memo(original['id'], '수정', content, expected_version=1)
                    return 'saved'
                except service.WriteConflict:
                    return 'conflict'
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(edit, ['A', 'B']))
            self.assertCountEqual(outcomes, ['saved', 'conflict'])
            self.assertEqual(next(m for m in service.list_memos(video['id']) if m['id'] == original['id'])['version'], 2)

    def test_retry_once_and_stale_edit_preserve_latest_content(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DELETE_PASSWORD': 'test', 'AUTH_RATE_LIMIT_STATE_PATH': str(Path(tmp)/'auth.json')}), test_ui_contract.YoutubeMemoUiContractTests().loaded_app(tmp) as app:
            from app.services import memo_service as service
            video = service.create_or_get_video('https://youtu.be/dQw4w9WgXcQ', title_fetcher=lambda *_: '영상')
            with TestClient(app, base_url='https://youtube.len.pe.kr') as client:
                headers = {'Origin': 'https://youtube.len.pe.kr'}
                client.post('/auth/login', data={'password': 'test'}, headers=headers)
                data = {'content': '원본', 'request_id': 'a'*32}
                for _ in range(2):
                    self.assertEqual(client.post(f"/videos/{video['id']}/memos", data=data, headers=headers, follow_redirects=False).status_code, 303)
                memos = service.list_memos(video['id'])
                self.assertEqual(len(memos), 1)
                memo_id = memos[0]['id']
                html = client.get(f"/videos/{video['id']}").text
                hidden = re.search(r'name="expected_version" value="([0-9]+)"', html)
                self.assertIsNotNone(hidden, 'memo edit must submit its loaded revision')
                altered = client.post(f"/videos/{video['id']}/memos", data={**data, 'content': '변경'}, headers=headers, follow_redirects=False)
                self.assertEqual(altered.status_code, 409)
                edit = {'memo_title': '최신', 'content': '최신 본문', 'expected_version': '1', 'request_id': 'b'*32}
                for _ in range(2):
                    self.assertEqual(client.post(f'/memos/{memo_id}', data=edit, headers=headers, follow_redirects=False).status_code, 303)
                for version in ['1', '0', '-1']:
                    stale = client.post(f'/memos/{memo_id}', data={'content': '과거', 'expected_version': version}, headers=headers, follow_redirects=False)
                    self.assertEqual(stale.status_code, 409)
                missing = client.post(f'/memos/{memo_id}', data={'content': '과거'}, headers=headers, follow_redirects=False)
                self.assertEqual(missing.status_code, 409)
                self.assertEqual(service.list_memos(video['id'])[0]['content'], '최신 본문')

    def test_tag_retry_does_not_reapply_older_tags_or_advance_revision_twice(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DELETE_PASSWORD': 'test'}), test_ui_contract.YoutubeMemoUiContractTests().loaded_app(tmp) as app:
            from app.services import memo_service as service
            video = service.create_or_get_video('https://youtu.be/dQw4w9WgXcQ', title_fetcher=lambda *_: '영상')
            memo = service.create_memo(video['id'], '메모', '원본')
            with TestClient(app, base_url='https://youtube.len.pe.kr') as client:
                headers = {'Origin': 'https://youtube.len.pe.kr'}
                client.post('/auth/login', data={'password': 'test'}, headers=headers)
                path = f"/memos/{memo['id']}/tags"
                first = {'tags': '이전', 'request_id': 'a'*32}
                newer = {'tags': '최신', 'request_id': 'b'*32}
                for body in [first, newer, first]:
                    self.assertEqual(client.post(path, data=body, headers=headers, follow_redirects=False).status_code, 303)
                final = service.list_memos(video['id'])[0]
                self.assertEqual(final['tags'], ['최신'])
                self.assertEqual(final['version'], 3)
