import importlib
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from tests._test_support import prepare_service_import


PORTAL_SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://img.youtube.com https://image.aladin.co.kr "
        "https://books.google.com https://covers.openlibrary.org; "
        "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'; frame-src 'self' https://www.youtube.com"
    ),
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}


class YoutubeMemoUiContractTests(unittest.TestCase):
    def assert_portal_security_headers(self, response):
        for name, value in PORTAL_SECURITY_HEADERS.items():
            self.assertEqual(response.headers[name], value)

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
        self.assert_portal_security_headers(response)

    def test_csp_allows_youtube_embed_and_thumbnail_sources(self):
        """Fails if CSP blocks the iframe or thumbnail rendered by the YouTube UI."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            with TestClient(app) as client:
                response = client.get("/health")

        policy = response.headers["content-security-policy"]
        self.assertIn("img-src 'self' data: https://img.youtube.com", policy)
        self.assertIn("frame-src 'self' https://www.youtube.com", policy)

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

    def test_untrusted_forwarded_headers_do_not_change_expected_origin(self):
        """Fails if a direct request can spoof forwarded headers to pass the Origin guard."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            with TestClient(app) as client:
                response = client.post(
                    "/health",
                    headers={
                        "Origin": "https://attacker.example",
                        "X-Forwarded-Proto": "https",
                        "X-Forwarded-Host": "attacker.example",
                    },
                )

        self.assertEqual(response.status_code, 403)

    def test_caddy_public_host_accepts_https_same_origin(self):
        """Fails if Caddy's internal HTTP hop rejects a browser's same-origin HTTPS form."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            with TestClient(app, base_url="http://memo.len.pe.kr") as client:
                response = client.post("/health", headers={"Origin": "https://memo.len.pe.kr"})

        self.assertEqual(response.status_code, 405)

    def test_security_headers_are_applied_to_forbidden_not_found_and_static_responses(self):
        """Fails if middleware protections are skipped for non-success or static responses."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            with TestClient(app) as client:
                responses = (
                    client.post("/health", headers={"Origin": "https://attacker.example"}),
                    client.get("/does-not-exist"),
                    client.get("/static/css/style.css"),
                )

        self.assertEqual([response.status_code for response in responses], [403, 404, 200])
        for response in responses:
            self.assert_portal_security_headers(response)

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
