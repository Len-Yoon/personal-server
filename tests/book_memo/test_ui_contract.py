import importlib
import json
import multiprocessing
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

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


def _record_book_auth_failure_in_process(
    state_path: str,
    ready,
    write_started,
    release_write,
) -> None:
    """Exercise the real rate-limit mutation from a separate interpreter."""
    service_dir = Path(__file__).resolve().parents[2] / "book-memo"
    previous_cwd = Path.cwd()
    prepare_service_import("book-memo")
    os.environ["AUTH_RATE_LIMIT_STATE_PATH"] = state_path
    os.chdir(service_dir)
    try:
        import app.main as main

        main = importlib.reload(main)
        if hasattr(main, "_persist_auth_failures"):
            persist = main._persist_auth_failures

            def delayed_persist() -> None:
                write_started.set()
                release_write.wait(timeout=5)
                persist()

            main._persist_auth_failures = delayed_persist

        ready.wait(timeout=5)
        main._record_auth_failure("203.0.113.18")
    finally:
        os.chdir(previous_cwd)


class BookMemoUiContractTests(unittest.TestCase):
    def assert_portal_security_headers(self, response):
        for name, value in PORTAL_SECURITY_HEADERS.items():
            self.assertEqual(response.headers[name], value)

    @contextmanager
    def loaded_app(self, tempdir: str):
        """Load the real app with an isolated library database."""
        service_dir = Path(__file__).resolve().parents[2] / "book-memo"
        previous_cwd = Path.cwd()
        previous_db_path = os.environ.get("BOOK_MEMO_DB_PATH")
        os.environ["BOOK_MEMO_DB_PATH"] = str(Path(tempdir) / "book_memo.sqlite3")
        prepare_service_import("book-memo")
        os.chdir(service_dir)
        try:
            import app.main as main

            yield importlib.reload(main).app
        finally:
            os.chdir(previous_cwd)
            if previous_db_path is None:
                os.environ.pop("BOOK_MEMO_DB_PATH", None)
            else:
                os.environ["BOOK_MEMO_DB_PATH"] = previous_db_path

    def test_home_keeps_original_title_book_search_and_portal_return(self):
        """Fails when the original home layout or search route is disconnected."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn('class="app-header"', response.text)
        self.assertIn("<h1>책 메모장</h1>", response.text)
        self.assertIn('<main class="container">', response.text)
        self.assertIn('href="https://len.pe.kr/"', response.text)
        self.assertIn('action="/" method="get"', response.text)
        self.assertIn('name="q"', response.text)
        self.assertIn('class="library-grid"', response.text)

    def test_unauthenticated_write_forms_redirect_to_login_before_submitting(self):
        """Fails if browser form submissions still end on a raw 401 response."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            import app.services.book_service as book_service

            book = book_service.create_or_get_book({"isbn": "9780000000018", "title": "로그인 이동 테스트 책"})
            with TestClient(app, base_url="https://books.len.pe.kr") as client:
                home = client.get("/")
                detail = client.get(f"/books/{book['id']}")
                login = client.get("/auth/login")

        for response, expected_redirects in ((home, 2), (detail, 1)):
            self.assertIn('const redirectToWriteLogin = () =>', response.text)
            self.assertGreaterEqual(response.text.count('if (response.status === 401)'), expected_redirects)
            self.assertIn('next_path=${encodeURIComponent(currentPath)}', response.text)
        self.assertNotIn('const redirectToWriteLogin = () =>', login.text)

    def test_unauthenticated_browser_write_redirects_to_login_with_current_path(self):
        """Fails if an expired session can still leave a browser on a raw 401 page."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            with TestClient(app, base_url="https://books.len.pe.kr") as client:
                response = client.post(
                    "/books",
                    data={"isbn": "9780000000018", "title": "로그인 이동 테스트 책"},
                    headers={
                        "Accept": "text/html",
                        "Origin": "https://books.len.pe.kr",
                        "Referer": "https://books.len.pe.kr/?view=library",
                    },
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/auth/login?{urlencode({'next_path': '/?view=library'})}")

    def test_browser_write_redirect_rejects_cross_origin_referer(self):
        """Fails if a hostile Referer can choose the post-login destination."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            with TestClient(app, base_url="https://books.len.pe.kr") as client:
                response = client.post(
                    "/books",
                    data={"isbn": "9780000000018", "title": "로그인 이동 테스트 책"},
                    headers={
                        "Accept": "text/html",
                        "Origin": "https://books.len.pe.kr",
                        "Referer": "https://attacker.example/redirect",
                    },
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/auth/login?next_path=%2F")

    def test_health_response_has_browser_security_headers(self):
        """Fails if the book service stops applying its common browser protections."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            with TestClient(app) as client:
                response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assert_portal_security_headers(response)

    def test_csp_allows_known_book_cover_hosts(self):
        """Fails if CSP blocks covers returned by the supported book providers."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            with TestClient(app) as client:
                response = client.get("/health")

        policy = response.headers["content-security-policy"]
        self.assertIn("https://image.aladin.co.kr", policy)
        self.assertIn("https://books.google.com", policy)
        self.assertIn("https://covers.openlibrary.org", policy)

    def test_cross_origin_book_creation_is_rejected(self):
        """Fails if another site can submit the book creation form."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            with TestClient(app) as client:
                response = client.post(
                    "/books",
                    data={"isbn": "9780000000018", "title": "교차 출처 테스트"},
                    headers={"Origin": "https://attacker.example"},
                )

        self.assertEqual(response.status_code, 403)

    def test_every_book_write_route_rejects_a_request_without_a_login_session(self):
        """Fails if any book, progress, chapter, or memo mutation bypasses the write-login session."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            import app.services.book_service as book_service

            book = book_service.create_or_get_book({"isbn": "9780000000018", "title": "쓰기 인증 테스트 책"})
            book_service.create_chapter(book["id"], "기존 장")
            chapter = book_service.list_chapters(book["id"])[0]
            book_service.create_memo(book["id"], chapter_id=chapter["id"], title="기존 메모", content="기존 내용", page=1)
            memo = book_service.list_memos(book["id"])[0]
            with TestClient(app, base_url="https://books.len.pe.kr") as client:
                headers = {"Origin": "https://books.len.pe.kr"}
                responses = (
                    client.post("/books", data={"isbn": "9780000000019", "title": "새 책"}, headers=headers),
                    client.post(f"/books/{book['id']}/progress", data={"reading_status": "읽는 중"}, headers=headers),
                    client.post(f"/books/{book['id']}/chapters", data={"title": "새 장"}, headers=headers),
                    client.post(f"/books/{book['id']}/chapters/bulk", data={"titles": "여러 장"}, headers=headers),
                    client.post(f"/books/{book['id']}/chapter-statuses", data={"done_chapter_ids": str(chapter['id'])}, headers=headers),
                    client.post(f"/chapters/{chapter['id']}", data={"is_done": "1"}, headers=headers),
                    client.post(f"/chapters/{chapter['id']}/comment", data={"comment": "코멘트"}, headers=headers),
                    client.post(f"/books/{book['id']}/memos", data={"content": "새 메모"}, headers=headers),
                    client.post(f"/books/{book['id']}/delete", data={"delete_password": "secret"}, headers=headers),
                    client.post(f"/chapters/{chapter['id']}/delete", data={"delete_password": "secret"}, headers=headers),
                    client.post(f"/memos/{memo['id']}/delete", data={"delete_password": "secret"}, headers=headers),
                )

        self.assertEqual([response.status_code for response in responses], [401] * 11)

    def test_book_login_session_allows_writes_until_logout(self):
        """Fails if the DELETE_PASSWORD login does not grant and revoke a book write session."""
        previous_password = os.environ.get("DELETE_PASSWORD")
        os.environ["DELETE_PASSWORD"] = "session-password"
        try:
            with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
                import app.services.book_service as book_service

                book = book_service.create_or_get_book({"isbn": "9780000000018", "title": "세션 인증 테스트 책"})
                with TestClient(app, base_url="https://books.len.pe.kr") as client:
                    headers = {"Origin": "https://books.len.pe.kr"}
                    login = client.post(
                        "/auth/login",
                        data={"password": "session-password", "next_path": f"/books/{book['id']}?view=library"},
                        headers=headers,
                        follow_redirects=False,
                    )
                    created = client.post(
                        f"/books/{book['id']}/memos",
                        data={"memo_title": "세션 메모", "content": "로그인 후 작성"},
                        headers=headers,
                        follow_redirects=False,
                    )
                    memo = book_service.list_memos(book["id"])[0]
                    deleted = client.post(
                        f"/memos/{memo['id']}/delete",
                        headers=headers,
                        follow_redirects=False,
                    )
                    logout = client.post("/auth/logout", headers=headers, follow_redirects=False)
                    rejected = client.post(
                        f"/books/{book['id']}/memos",
                        data={"content": "로그아웃 후 작성"},
                        headers=headers,
                    )
        finally:
            if previous_password is None:
                os.environ.pop("DELETE_PASSWORD", None)
            else:
                os.environ["DELETE_PASSWORD"] = previous_password

        self.assertEqual(login.status_code, 303)
        self.assertIn("book_memo_write_session", login.headers["set-cookie"])
        self.assertEqual(login.headers["location"], f"/books/{book['id']}?view=library")
        self.assertEqual(created.status_code, 303)
        self.assertEqual(deleted.status_code, 303)
        self.assertEqual(logout.status_code, 303)
        self.assertEqual(rejected.status_code, 401)

    def test_write_auth_failures_survive_service_restart(self):
        """Fails if failed write-password attempts are retained only in process memory."""
        previous_password = os.environ.get("DELETE_PASSWORD")
        previous_state_path = os.environ.get("AUTH_RATE_LIMIT_STATE_PATH")
        os.environ["DELETE_PASSWORD"] = "rate-limit-password"
        try:
            with tempfile.TemporaryDirectory() as tempdir:
                state_path = Path(tempdir) / "book-rate-limit.json"
                os.environ["AUTH_RATE_LIMIT_STATE_PATH"] = str(state_path)
                with self.loaded_app(tempdir) as app:
                    with TestClient(app, base_url="https://books.len.pe.kr") as client:
                        headers = {"Origin": "https://books.len.pe.kr", "X-Forwarded-For": "203.0.113.18"}
                        responses = [
                            client.post("/auth/login", data={"password": "wrong"}, headers=headers)
                            for _ in range(6)
                        ]

                with self.loaded_app(tempdir) as restarted_app:
                    with TestClient(restarted_app, base_url="https://books.len.pe.kr") as client:
                        blocked_after_restart = client.post(
                            "/auth/login",
                            data={"password": "wrong"},
                            headers={"Origin": "https://books.len.pe.kr", "X-Forwarded-For": "203.0.113.18"},
                        )
                state_exists = state_path.exists()
        finally:
            if previous_password is None:
                os.environ.pop("DELETE_PASSWORD", None)
            else:
                os.environ["DELETE_PASSWORD"] = previous_password
            if previous_state_path is None:
                os.environ.pop("AUTH_RATE_LIMIT_STATE_PATH", None)
            else:
                os.environ["AUTH_RATE_LIMIT_STATE_PATH"] = previous_state_path

        self.assertEqual([response.status_code for response in responses], [403, 403, 403, 403, 403, 429])
        self.assertTrue(state_exists)
        self.assertEqual(blocked_after_restart.status_code, 429)

    def test_concurrent_processes_keep_all_write_auth_failures(self):
        """Fails if concurrent processes overwrite one another's persisted failures."""
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "book-rate-limit.json"
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            write_started = context.Event()
            release_write = context.Event()
            process_count = 4
            workers = [
                context.Process(
                    target=_record_book_auth_failure_in_process,
                    args=(str(state_path), ready, write_started, release_write),
                )
                for _ in range(process_count)
            ]

            for worker in workers:
                worker.start()
            try:
                ready.set()
                self.assertTrue(write_started.wait(timeout=10))
                time.sleep(0.2)
                release_write.set()
                for worker in workers:
                    worker.join(timeout=10)
                self.assertEqual([worker.exitcode for worker in workers], [0] * process_count)
            finally:
                release_write.set()
                for worker in workers:
                    if worker.is_alive():
                        worker.terminate()
                    worker.join()

            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(len(state["203.0.113.18"]), process_count)

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
            with TestClient(app, base_url="http://books.len.pe.kr") as client:
                response = client.post("/health", headers={"Origin": "https://books.len.pe.kr"})

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

    def test_detail_keeps_original_layout_and_session_guarded_book_crud(self):
        """Fails if the detail layout keeps obsolete per-delete password prompts."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            import app.services.book_service as book_service

            book = book_service.create_or_get_book({"isbn": "9780000000018", "title": "계약 테스트 책"})
            book_service.create_chapter(book["id"], "첫 번째 장")
            chapter = book_service.list_chapters(book["id"])[0]
            book_service.create_memo(book["id"], chapter_id=chapter["id"], title="핵심 메모", content="메모 내용", page=0)
            memo = book_service.list_memos(book["id"])[0]

            with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                response = client.get(f"/books/{book['id']}")

        self.assertEqual(response.status_code, 200)
        self.assertIn('class="app-header"', response.text)
        self.assertIn('<main class="container">', response.text)
        self.assertIn(f'action="/books/{book["id"]}/chapters"', response.text)
        self.assertIn(f'action="/books/{book["id"]}/chapters/bulk"', response.text)
        self.assertIn(f'action="/books/{book["id"]}/memos"', response.text)
        self.assertIn(f'action="/books/{book["id"]}/delete"', response.text)
        self.assertIn(f'action="/chapters/{chapter["id"]}"', response.text)
        self.assertIn(f'action="/chapters/{chapter["id"]}/delete"', response.text)
        self.assertIn(f'action="/memos/{memo["id"]}/delete"', response.text)
        self.assertNotIn('name="delete_password"', response.text)
        self.assertNotIn("삭제 비밀번호를 입력해주세요.", response.text)
        self.assertIn('id="toc-fetch-button"', response.text)
        self.assertIn('class="chapter-list"', response.text)
        self.assertIn('class="memo-list"', response.text)

    def test_detail_formats_stored_utc_memo_timestamp_as_compact_kst_datetime(self):
        """Fails if a stored UTC memo timestamp is rendered without KST conversion."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            import app.services.book_service as book_service

            book = book_service.create_or_get_book({"isbn": "9780000000020", "title": "시간 표시 테스트 책"})
            book_service.create_memo(book["id"], chapter_id=None, title="시간 메모", content="표시 형식 확인", page=0)
            memo = book_service.list_memos(book["id"])[0]
            raw_utc_timestamp = "2026-07-09 01:02:03"
            with sqlite3.connect(book_service.DB_PATH) as connection:
                connection.execute(
                    "UPDATE book_memos SET created_at = ? WHERE id = ?",
                    (raw_utc_timestamp, memo["id"]),
                )

            with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                response = client.get(f"/books/{book['id']}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("2026-07-09 10:02", response.text)
        self.assertNotIn(raw_utc_timestamp, response.text)
        self.assertNotIn("KST", response.text)
        self.assertNotIn("10:02:03", response.text)

    def test_display_datetime_hides_unparsable_values_and_keeps_valid_kst_values(self):
        """Fails if a malformed memo date is exposed instead of being hidden."""
        from app.services.datetime_format import format_display_datetime

        cases = (
            ("2026-07-09 01:02:03", "2026-07-09 10:02"),
            ("2026-07-09T01:02:03+00:00", "2026-07-09 10:02"),
            ("2026-07-09T10:02:03+09:00", "2026-07-09 10:02"),
            (None, ""),
            ("", ""),
            ("not-a-datetime", ""),
            ("2026-07-09 01:02:03 UTC", ""),
        )

        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(format_display_datetime(value), expected)

    def test_detail_hides_unparsable_utc_like_memo_timestamp(self):
        """Fails if an invalid UTC-like stored timestamp leaks into the book detail HTML."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            import app.services.book_service as book_service

            book = book_service.create_or_get_book({"isbn": "9780000000021", "title": "오류 시간 표시 테스트 책"})
            book_service.create_memo(book["id"], chapter_id=None, title="오류 시간 메모", content="비표시 확인", page=0)
            memo = book_service.list_memos(book["id"])[0]
            raw_invalid_timestamp = "2026-07-09 01:02:03 UTC"
            with sqlite3.connect(book_service.DB_PATH) as connection:
                connection.execute(
                    "UPDATE book_memos SET created_at = ? WHERE id = ?",
                    (raw_invalid_timestamp, memo["id"]),
                )

            with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                response = client.get(f"/books/{book['id']}")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(raw_invalid_timestamp, response.text)
        self.assertNotIn("UTC", response.text)
        self.assertNotIn("01:02:03", response.text)

    def test_search_api_keeps_saved_book_and_memo_results_available(self):
        """Fails if a presentation-only change accidentally removes search results."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            import app.services.book_service as book_service

            book = book_service.create_or_get_book({"isbn": "9780000000019", "title": "검색 대상 책"})
            book_service.create_memo(book["id"], chapter_id=None, title="검색 제목", content="검색 가능한 고유 메모 내용", page=0)

            with TestClient(app) as client:
                response = client.get("/api/search", params={"q": "고유 메모"})

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            {"title": "검색 제목", "description": "검색 대상 책", "snippet": "검색 가능한 고유 메모 내용", "meta": "책 ·  · 진행률 0%", "url": f"/books/{book['id']}"},
            response.json()["results"],
        )


if __name__ == "__main__":
    unittest.main()
