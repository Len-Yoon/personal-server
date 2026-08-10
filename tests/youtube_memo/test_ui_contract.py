import importlib
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from tests._test_support import prepare_service_import


class YoutubeMemoUiContractTests(unittest.TestCase):
    @contextmanager
    def loaded_app(self, tempdir: str):
        """Load the real application with an isolated memo database."""
        service_dir = Path(__file__).resolve().parents[2] / "youtube-memo"
        previous_cwd = Path.cwd()
        previous_db_path = os.environ.get("YOUTUBE_MEMO_DB_PATH")
        os.environ["YOUTUBE_MEMO_DB_PATH"] = str(Path(tempdir) / "youtube_memo.sqlite3")
        prepare_service_import("youtube-memo")
        os.chdir(service_dir)
        try:
            import app.main as main

            yield importlib.reload(main).app
        finally:
            os.chdir(previous_cwd)
            if previous_db_path is None:
                os.environ.pop("YOUTUBE_MEMO_DB_PATH", None)
            else:
                os.environ["YOUTUBE_MEMO_DB_PATH"] = previous_db_path

    def test_home_keeps_video_creation_and_portal_return_inside_accessible_atlas_layout(self):
        """Fails if the modernized home loses its real route or keyboard landmarks."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn('class="memo-atlas home-page"', response.text)
        self.assertIn('href="https://len.pe.kr/"', response.text)
        self.assertIn('href="#main-content"', response.text)
        self.assertIn('<main id="main-content"', response.text)
        self.assertIn('<label for="video-url">YouTube 영상 링크</label>', response.text)
        self.assertIn('action="/videos"', response.text)
        self.assertIn('name="url"', response.text)
        self.assertIn('class="collection-header"', response.text)

    def test_detail_keeps_memo_crud_and_password_forms_inside_accessible_atlas_layout(self):
        """Fails if a layout edit disconnects the existing memo and protected-delete routes."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            import app.services.memo_service as memo_service

            video = memo_service.create_or_get_video(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                title_fetcher=lambda _youtube_id, _url: "계약 테스트 영상",
            )
            memo = memo_service.create_memo(video["id"], "핵심 장면", "메모 내용")

            with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                response = client.get(f"/videos/{video['id']}")

        self.assertEqual(response.status_code, 200)
        self.assertIn('class="memo-atlas detail-page"', response.text)
        self.assertIn('<main id="main-content"', response.text)
        self.assertIn('title="YouTube video player"', response.text)
        self.assertIn(f'action="/videos/{video["id"]}/memos"', response.text)
        self.assertIn(f'action="/videos/{video["id"]}/delete"', response.text)
        self.assertIn(f'action="/memos/{memo["id"]}"', response.text)
        self.assertIn(f'action="/memos/{memo["id"]}/delete"', response.text)
        self.assertIn('name="edit_password"', response.text)
        self.assertIn('name="delete_password"', response.text)
        self.assertIn('class="memo-timeline"', response.text)

    def test_search_api_keeps_saved_video_and_memo_results_available(self):
        """Fails if a UI-only change accidentally removes the public search contract."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            import app.services.memo_service as memo_service

            video = memo_service.create_or_get_video(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                title_fetcher=lambda _youtube_id, _url: "검색 대상 영상",
            )
            memo_service.create_memo(video["id"], "검색 제목", "검색 가능한 고유 메모 내용")

            with TestClient(app) as client:
                response = client.get("/api/search", params={"q": "고유 메모"})

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            {"title": "검색 제목", "description": "검색 대상 영상", "snippet": "검색 가능한 고유 메모 내용", "meta": "YouTube 메모", "url": f"/videos/{video['id']}"},
            response.json()["results"],
        )


if __name__ == "__main__":
    unittest.main()
