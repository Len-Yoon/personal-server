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

    def test_home_keeps_original_title_video_creation_and_portal_return(self):
        """Fails if the original home layout or its real routes are disconnected."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn('class="app-header"', response.text)
        self.assertIn("<h1>유튜브 메모장</h1>", response.text)
        self.assertIn('href="https://len.pe.kr/"', response.text)
        self.assertIn('<main class="container">', response.text)
        self.assertIn('action="/videos"', response.text)
        self.assertIn('name="url"', response.text)
        self.assertIn('class="video-grid"', response.text)

    def test_health_response_has_browser_security_headers(self):
        """Fails if the YouTube service stops applying its common browser protections."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            with TestClient(app) as client:
                response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["content-security-policy"],
            "default-src 'self'; base-uri 'self'; object-src 'none'; "
            "frame-ancestors 'none'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
            "frame-src https://www.youtube.com https://www.youtube-nocookie.com; "
            "connect-src 'self'",
        )
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["referrer-policy"], "strict-origin-when-cross-origin")
        self.assertEqual(response.headers["permissions-policy"], "geolocation=(), microphone=(), camera=()")

    def test_cross_origin_video_creation_is_rejected(self):
        """Fails if another site can submit the video creation form."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            with TestClient(app) as client:
                response = client.post(
                    "/videos",
                    data={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
                    headers={"Origin": "https://attacker.example"},
                )

        self.assertEqual(response.status_code, 403)

    def test_https_origin_forwarded_by_proxy_is_accepted(self):
        """Fails if Caddy's internal HTTP hop rejects a browser's same-origin HTTPS form."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            with TestClient(app, base_url="http://memo.len.pe.kr") as client:
                response = client.post(
                    "/health",
                    headers={
                        "Origin": "https://memo.len.pe.kr",
                        "X-Forwarded-Proto": "https",
                    },
                )

        self.assertEqual(response.status_code, 405)

    def test_detail_keeps_original_layout_memo_crud_and_password_forms(self):
        """Fails if the original detail layout or protected memo routes are disconnected."""
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
        self.assertIn('class="app-header"', response.text)
        self.assertIn('<main class="container">', response.text)
        self.assertIn(f'action="/videos/{video["id"]}/memos"', response.text)
        self.assertIn(f'action="/videos/{video["id"]}/delete"', response.text)
        self.assertIn(f'action="/memos/{memo["id"]}"', response.text)
        self.assertIn(f'action="/memos/{memo["id"]}/delete"', response.text)
        self.assertIn('name="edit_password"', response.text)
        self.assertIn('name="delete_password"', response.text)
        self.assertIn('class="memo-list"', response.text)

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
