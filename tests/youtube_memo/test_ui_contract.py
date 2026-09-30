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
from unittest.mock import patch

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


def _record_youtube_auth_failure_in_process(
    state_path: str,
    ready,
    write_started,
    release_write,
) -> None:
    """Exercise the real rate-limit mutation from a separate interpreter."""
    service_dir = Path(__file__).resolve().parents[2] / "youtube-memo"
    previous_cwd = Path.cwd()
    prepare_service_import("youtube-memo")
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
        main._record_auth_failure("203.0.113.17")
    finally:
        os.chdir(previous_cwd)


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

    def test_home_paginates_videos_with_total_and_stable_order(self):
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            import app.services.memo_service as memo_service

            memo_service.init_db()
            with memo_service._connect() as connection:
                connection.executemany(
                    "INSERT INTO videos (youtube_id, url, title, updated_at) VALUES (?, ?, ?, ?)",
                    [(f"video-{number}", f"https://example.com/{number}", f"영상 {number}", "2026-01-01 00:00:00") for number in range(26)],
                )
            with TestClient(app) as client:
                first = client.get("/")
                second = client.get("/?page=2")
                beyond = client.get("/?page=999")
                invalid = client.get("/?page=0")

        self.assertEqual((first.status_code, second.status_code, beyond.status_code, invalid.status_code), (200, 200, 200, 422))
        self.assertIn("26개의 영상이 저장되어 있습니다.", first.text)
        self.assertIn('href="/?page=2"', first.text)
        self.assertIn("영상 25", first.text)
        self.assertNotIn("영상 1</h3>", first.text)
        self.assertIn("영상 1", second.text)
        self.assertIn("영상 0", second.text)
        self.assertNotIn("영상 25", second.text)
        self.assertIn("영상 0", beyond.text)

    def test_home_indexes_serve_sort_and_memo_count(self):
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir):
            import app.services.memo_service as memo_service

            memo_service.init_db()
            with memo_service._connect() as connection:
                sort_plan = " ".join(row[3] for row in connection.execute(
                    "EXPLAIN QUERY PLAN SELECT id FROM videos ORDER BY updated_at DESC, id DESC LIMIT 24"
                ))
                memo_plan = " ".join(row[3] for row in connection.execute(
                    "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM memos WHERE video_id = 1"
                ))
        self.assertIn("USING COVERING INDEX idx_videos_home_order", sort_plan)
        self.assertNotIn("TEMP B-TREE", sort_plan)
        self.assertIn("USING COVERING INDEX idx_memos_video", memo_plan)

    def test_video_page_uses_one_snapshot_when_last_row_is_deleted_after_count(self):
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir):
            import app.services.memo_service as memo_service

            memo_service.init_db()
            with memo_service._connect() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executemany(
                    "INSERT INTO videos (youtube_id, url, title) VALUES (?, ?, ?)",
                    [(f"video-{number}", f"https://example.com/{number}", f"영상 {number}") for number in range(25)],
                )
            original_connect = memo_service._connect
            deleted = False

            class RacingConnection:
                def __init__(self, connection):
                    self.connection = connection

                def execute(self, sql, parameters=()):
                    nonlocal deleted
                    cursor = self.connection.execute(sql, parameters)
                    if sql == "SELECT COUNT(*) FROM videos" and not deleted:
                        deleted = True
                        with sqlite3.connect(memo_service.DB_PATH) as writer:
                            writer.execute("DELETE FROM videos WHERE youtube_id = 'video-0'")
                    return cursor

            @contextmanager
            def racing_connect():
                with original_connect() as connection:
                    yield RacingConnection(connection)

            with patch.object(memo_service, "_connect", racing_connect):
                rows, total, page = memo_service.list_videos_page(2)

        self.assertTrue(deleted)
        self.assertEqual((total, page, [video["title"] for video in rows]), (25, 2, ["영상 0"]))

    def test_video_delete_returns_to_current_page(self):
        previous_password = os.environ.get("DELETE_PASSWORD")
        os.environ["DELETE_PASSWORD"] = "session-password"
        try:
            with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
                import app.services.memo_service as memo_service

                video = memo_service.create_or_get_video(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    title_fetcher=lambda _youtube_id, _url: "삭제할 영상",
                )
                with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                    headers = {"Origin": "https://memo.len.pe.kr"}
                    client.post("/auth/login", data={"password": "session-password"}, headers=headers)
                    response = client.post(
                        f"/videos/{video['id']}/delete",
                        data={"redirect_to": "/?page=2"},
                        headers=headers,
                        follow_redirects=False,
                    )
        finally:
            if previous_password is None:
                os.environ.pop("DELETE_PASSWORD", None)
            else:
                os.environ["DELETE_PASSWORD"] = previous_password

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/?page=2")

    def test_unauthenticated_write_forms_redirect_to_login_before_submitting(self):
        """Fails if browser form submissions still end on a raw 401 response."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            import app.services.memo_service as memo_service

            video = memo_service.create_or_get_video(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                title_fetcher=lambda _youtube_id, _url: "로그인 이동 테스트 영상",
            )
            with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                home = client.get("/")
                detail = client.get(f"/videos/{video['id']}")
                login = client.get("/auth/login")

        for response, expected_redirects in ((home, 1), (detail, 2)):
            self.assertIn('const redirectToWriteLogin = () =>', response.text)
            self.assertGreaterEqual(response.text.count('if (response.status === 401)'), expected_redirects)
            self.assertIn('next_path=${encodeURIComponent(currentPath)}', response.text)
        self.assertNotIn('const redirectToWriteLogin = () =>', login.text)

    def test_unauthenticated_browser_write_redirects_to_login_with_current_path(self):
        """Fails if an expired session can still leave a browser on a raw 401 page."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                response = client.post(
                    "/videos",
                    data={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
                    headers={
                        "Accept": "text/html",
                        "Origin": "https://memo.len.pe.kr",
                        "Referer": "https://memo.len.pe.kr/?view=grid",
                    },
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/auth/login?{urlencode({'next_path': '/?view=grid'})}")

    def test_browser_write_redirect_rejects_cross_origin_referer(self):
        """Fails if a hostile Referer can choose the post-login destination."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                response = client.post(
                    "/videos",
                    data={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
                    headers={
                        "Accept": "text/html",
                        "Origin": "https://memo.len.pe.kr",
                        "Referer": "https://attacker.example/redirect",
                    },
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/auth/login?next_path=%2F")

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

    def test_every_youtube_write_route_rejects_a_request_without_a_login_session(self):
        """Fails if any video or memo mutation can bypass the write-login session."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            import app.services.memo_service as memo_service

            video = memo_service.create_or_get_video(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                title_fetcher=lambda _youtube_id, _url: "쓰기 인증 테스트 영상",
            )
            memo = memo_service.create_memo(video["id"], "기존 메모", "기존 내용")
            with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                headers = {"Origin": "https://memo.len.pe.kr"}
                responses = (
                    client.post("/videos", data={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}, headers=headers),
                    client.post(f"/videos/{video['id']}/memos", data={"content": "새 메모"}, headers=headers),
                    client.post(f"/memos/{memo['id']}", data={"content": "수정 메모"}, headers=headers),
                    client.post(f"/videos/{video['id']}/delete", data={"delete_password": "secret"}, headers=headers),
                    client.post(f"/memos/{memo['id']}/delete", data={"delete_password": "secret"}, headers=headers),
                )

        self.assertEqual([response.status_code for response in responses], [401, 401, 401, 401, 401])

    def test_youtube_login_session_allows_writes_until_logout(self):
        """Fails if the DELETE_PASSWORD login does not grant and revoke a write session."""
        previous_password = os.environ.get("DELETE_PASSWORD")
        os.environ["DELETE_PASSWORD"] = "session-password"
        try:
            with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
                import app.services.memo_service as memo_service

                video = memo_service.create_or_get_video(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    title_fetcher=lambda _youtube_id, _url: "세션 인증 테스트 영상",
                )
                with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                    headers = {"Origin": "https://memo.len.pe.kr"}
                    login = client.post(
                        "/auth/login",
                        data={"password": "session-password", "next_path": f"/videos/{video['id']}?view=grid"},
                        headers=headers,
                        follow_redirects=False,
                    )
                    created = client.post(
                        f"/videos/{video['id']}/memos",
                        data={"memo_title": "세션 메모", "content": "로그인 후 작성"},
                        headers=headers,
                        follow_redirects=False,
                    )
                    memo = memo_service.list_memos(video["id"])[0]
                    deleted = client.post(
                        f"/memos/{memo['id']}/delete",
                        headers=headers,
                        follow_redirects=False,
                    )
                    logout = client.post("/auth/logout", headers=headers, follow_redirects=False)
                    rejected = client.post(
                        f"/videos/{video['id']}/memos",
                        data={"content": "로그아웃 후 작성"},
                        headers=headers,
                    )
        finally:
            if previous_password is None:
                os.environ.pop("DELETE_PASSWORD", None)
            else:
                os.environ["DELETE_PASSWORD"] = previous_password

        self.assertEqual(login.status_code, 303)
        self.assertIn("youtube_memo_write_session", login.headers["set-cookie"])
        self.assertEqual(login.headers["location"], f"/videos/{video['id']}?view=grid")
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
                state_path = Path(tempdir) / "youtube-rate-limit.json"
                os.environ["AUTH_RATE_LIMIT_STATE_PATH"] = str(state_path)
                with self.loaded_app(tempdir) as app:
                    with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                        headers = {"Origin": "https://memo.len.pe.kr", "X-Forwarded-For": "203.0.113.17"}
                        responses = [
                            client.post("/auth/login", data={"password": "wrong"}, headers=headers)
                            for _ in range(6)
                        ]

                with self.loaded_app(tempdir) as restarted_app:
                    with TestClient(restarted_app, base_url="https://memo.len.pe.kr") as client:
                        blocked_after_restart = client.post(
                            "/auth/login",
                            data={"password": "wrong"},
                            headers={"Origin": "https://memo.len.pe.kr", "X-Forwarded-For": "203.0.113.17"},
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
            state_path = Path(tempdir) / "youtube-rate-limit.json"
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            write_started = context.Event()
            release_write = context.Event()
            process_count = 4
            workers = [
                context.Process(
                    target=_record_youtube_auth_failure_in_process,
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

        self.assertEqual(len(state["203.0.113.17"]), process_count)

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

    def test_detail_keeps_original_layout_and_session_guarded_memo_crud(self):
        """Fails if the detail layout keeps obsolete per-delete password prompts."""
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
        self.assertNotIn('name="edit_password"', response.text)
        self.assertNotIn('window.prompt("수정 비밀번호', response.text)
        self.assertNotIn('name="delete_password"', response.text)
        self.assertNotIn("삭제 비밀번호를 입력해주세요.", response.text)
        self.assertIn('class="memo-list"', response.text)

    def test_logged_in_writer_can_edit_memo_without_second_password(self):
        previous_password = os.environ.get("DELETE_PASSWORD")
        os.environ["DELETE_PASSWORD"] = "session-password"
        try:
            with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
                import app.services.memo_service as memo_service

                video = memo_service.create_or_get_video(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    title_fetcher=lambda _youtube_id, _url: "수정 인증 영상",
                )
                memo = memo_service.create_memo(video["id"], "처음 제목", "처음 내용")
                with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                    headers = {"Origin": "https://memo.len.pe.kr"}
                    client.post("/auth/login", data={"password": "session-password"}, headers=headers)
                    updated = client.post(
                        f"/memos/{memo['id']}",
                        data={"memo_title": "바뀐 제목", "content": "1:23 장면"},
                        headers=headers,
                        follow_redirects=False,
                    )
                    client.post("/auth/logout", headers=headers)
                    rejected = client.post(
                        f"/memos/{memo['id']}",
                        data={"content": "인증 없는 수정"},
                        headers=headers,
                    )

                saved = memo_service.list_memos(video["id"])[0]
        finally:
            if previous_password is None:
                os.environ.pop("DELETE_PASSWORD", None)
            else:
                os.environ["DELETE_PASSWORD"] = previous_password

        self.assertEqual(updated.status_code, 303)
        self.assertEqual(updated.headers["location"], f"/videos/{video['id']}")
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(saved["title"], "바뀐 제목")
        self.assertEqual(saved["content"], "1:23 장면")

    def test_detail_links_only_valid_timestamps_without_changing_saved_text(self):
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            import app.services.memo_service as memo_service

            video = memo_service.create_or_get_video(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                title_fetcher=lambda _youtube_id, _url: "타임스탬프 영상",
            )
            original = "1:23 · 01:02:03 · 23:59:59 · 24:00:00 · 1:60 · 1:02:60 · <img src=x onerror=alert(1)>"
            memo = memo_service.create_memo(video["id"], "시각 메모", original)

            with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                response = client.get(f"/videos/{video['id']}")

            saved = memo_service.list_memos(video["id"])[0]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(saved["content"], original)
        self.assertIn('id="youtube-player"', response.text)
        for seconds in (83, 3723, 86399):
            self.assertIn(f'data-start-seconds="{seconds}"', response.text)
            self.assertIn(f'&amp;t={seconds}s', response.text)
        self.assertEqual(response.text.count('class="memo-timestamp"'), 3)
        self.assertIn("24:00:00", response.text)
        self.assertIn("1:60", response.text)
        self.assertIn("1:02:60", response.text)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", response.text)
        self.assertNotIn("<img src=x onerror=alert(1)>", response.text)

    def test_timestamp_segments_reject_partial_and_embedded_matches(self):
        from app.services.memo_service import memo_timestamp_segments

        content = "x1:23 123:45:67 1:02:60 0:00 90:00"
        segments = memo_timestamp_segments(content)

        self.assertEqual(
            [(part["text"], part["seconds"]) for part in segments if part["seconds"] is not None],
            [("0:00", 0), ("90:00", 5400)],
        )
        self.assertEqual("".join(part["text"] for part in segments), content)

    def test_detail_formats_stored_utc_memo_timestamp_as_compact_kst_datetime(self):
        """Fails if a stored UTC memo timestamp is rendered without KST conversion."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            import app.services.memo_service as memo_service

            video = memo_service.create_or_get_video(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                title_fetcher=lambda _youtube_id, _url: "시간 표시 테스트 영상",
            )
            memo = memo_service.create_memo(video["id"], "시간 메모", "표시 형식 확인")
            raw_utc_timestamp = "2026-07-09 01:02:03"
            with sqlite3.connect(memo_service.DB_PATH) as connection:
                connection.execute(
                    "UPDATE memos SET created_at = ? WHERE id = ?",
                    (raw_utc_timestamp, memo["id"]),
                )

            with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                response = client.get(f"/videos/{video['id']}")

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

    def test_list_memos_prepares_compact_kst_display_timestamp_without_changing_raw_value(self):
        """Fails if the memo display value is not prepared in the service layer."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir):
            import app.services.memo_service as memo_service

            video = memo_service.create_or_get_video(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                title_fetcher=lambda _youtube_id, _url: "서비스 시간 표시 테스트 영상",
            )
            memo = memo_service.create_memo(video["id"], "서비스 시간 메모", "표시 값 확인")
            raw_utc_timestamp = "2026-07-09 01:02:03"
            with sqlite3.connect(memo_service.DB_PATH) as connection:
                connection.execute(
                    "UPDATE memos SET created_at = ? WHERE id = ?",
                    (raw_utc_timestamp, memo["id"]),
                )

            listed_memo = memo_service.list_memos(video["id"])[0]

        self.assertEqual(listed_memo["created_at"], raw_utc_timestamp)
        self.assertEqual(listed_memo["display_created_at"], "2026-07-09 10:02")

    def test_detail_hides_unparsable_utc_like_memo_timestamp(self):
        """Fails if an invalid UTC-like stored timestamp leaks into the video detail HTML."""
        with tempfile.TemporaryDirectory() as tempdir, self.loaded_app(tempdir) as app:
            import app.services.memo_service as memo_service

            video = memo_service.create_or_get_video(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                title_fetcher=lambda _youtube_id, _url: "오류 시간 표시 테스트 영상",
            )
            memo = memo_service.create_memo(video["id"], "오류 시간 메모", "비표시 확인")
            raw_invalid_timestamp = "2026-07-09 01:02:03 UTC"
            with sqlite3.connect(memo_service.DB_PATH) as connection:
                connection.execute(
                    "UPDATE memos SET created_at = ? WHERE id = ?",
                    (raw_invalid_timestamp, memo["id"]),
                )

            with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                response = client.get(f"/videos/{video['id']}")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(raw_invalid_timestamp, response.text)
        self.assertNotIn("UTC", response.text)
        self.assertNotIn("01:02:03", response.text)

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


@contextmanager
def isolated_service():
    with tempfile.TemporaryDirectory() as directory:
        previous_cwd = Path.cwd()
        previous_db = os.environ.get("YOUTUBE_MEMO_DB_PATH")
        service_dir = Path(__file__).resolve().parents[2] / "youtube-memo"
        os.environ["YOUTUBE_MEMO_DB_PATH"] = str(Path(directory) / "memo.sqlite3")
        prepare_service_import("youtube-memo")
        os.chdir(service_dir)
        try:
            import app.services.memo_service as memo_service
            import app.main as main
            yield importlib.reload(memo_service), importlib.reload(main).app
        finally:
            os.chdir(previous_cwd)
            if previous_db is None:
                os.environ.pop("YOUTUBE_MEMO_DB_PATH", None)
            else:
                os.environ["YOUTUBE_MEMO_DB_PATH"] = previous_db


def make_video(service, youtube_id: str, title: str):
    return service.create_or_get_video(
        f"https://www.youtube.com/watch?v={youtube_id}",
        title_fetcher=lambda _id, _url: title,
    )


class YoutubeTagsSearchTests(unittest.TestCase):
    def test_existing_database_migration_tags_and_cascade(self):
        with isolated_service() as (service, _app):
            with sqlite3.connect(service.DB_PATH) as connection:
                connection.executescript("""
                    CREATE TABLE videos (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, youtube_id TEXT NOT NULL UNIQUE,
                        url TEXT NOT NULL, title TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE memos (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, video_id INTEGER NOT NULL,
                        title TEXT NOT NULL, content TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(video_id) REFERENCES videos(id) ON DELETE CASCADE
                    );
                    INSERT INTO videos (youtube_id, url, title) VALUES
                        ('dQw4w9WgXcQ', 'https://www.youtube.com/watch?v=dQw4w9WgXcQ', '기존 영상');
                    INSERT INTO memos (video_id, title, content) VALUES (1, '기존 메모', '보존 내용');
                """)
            service.init_db()
            self.assertEqual(service.list_memos(1)[0]["tags"], [])
            self.assertEqual(service.list_memos(1)[0]["content"], "보존 내용")
            self.assertEqual(service.update_memo_tags(1, " Python, python, 공부 "), 1)
            self.assertEqual(service.list_memos(1)[0]["tags"], ["Python", "공부"])
            self.assertEqual(service.list_available_tags()[0]["video_count"], 1)
            service.delete_memo(1)
            with sqlite3.connect(service.DB_PATH) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM memo_tags").fetchone()[0], 0)

    def test_tag_validation_and_filtered_pagination(self):
        with isolated_service() as (service, _app):
            first = make_video(service, "dQw4w9WgXcQ", "첫 영상")
            second = make_video(service, "abcdefghijk", "둘째 영상")
            memo = service.create_memo(first["id"], "제목", "내용", tags="개발, 학습")
            service.create_memo(first["id"], "다른 메모", "내용", tags="개발")
            service.create_memo(second["id"], "제목", "내용", tags="독서")
            rows, total, page = service.list_videos_page(99, 1, tag="개발")
            self.assertEqual(([row["id"] for row in rows], total, page), ([first["id"]], 1, 1))
            self.assertEqual(service.list_videos_page(1, tag="없음")[1], 0)
            for invalid in ("a,b,c,d,e,f", "x" * 31, "정상,\x00오류", "보이지\u200b않음"):
                with self.assertRaises(ValueError):
                    service.update_memo_tags(memo["id"], invalid)
            self.assertEqual(service.list_memos(first["id"])[1]["tags"], ["개발", "학습"])
            service.update_memo_tags(memo["id"], "")
            self.assertEqual(service.list_memos(first["id"])[1]["tags"], [])

    def test_tag_route_requires_writer_and_home_filter_is_public(self):
        with isolated_service() as (service, app):
            video = make_video(service, "dQw4w9WgXcQ", "영상")
            memo = service.create_memo(video["id"], "메모", "내용", tags="기존")
            previous_password = os.environ.get("DELETE_PASSWORD")
            os.environ["DELETE_PASSWORD"] = "test-session-password"
            try:
                with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                    headers = {"Origin": "https://memo.len.pe.kr"}
                    self.assertEqual(client.get("/?tag=기존").status_code, 200)
                    self.assertEqual(client.post(f"/memos/{memo['id']}/tags", data={"tags": "새태그"}, headers=headers).status_code, 401)
                    client.post("/auth/login", data={"password": "test-session-password"}, headers=headers)
                    updated = client.post(f"/memos/{memo['id']}/tags", data={"tags": "새태그"}, headers=headers, follow_redirects=False)
                    self.assertEqual(updated.status_code, 303)
                    self.assertIn("#새태그", client.get(f"/videos/{video['id']}").text)
                    self.assertIn("영상", client.get("/?tag=새태그").text)
                    self.assertEqual(client.get("/?tag=기존").text.count('class="video-card"'), 0)
                    self.assertEqual(client.post(f"/memos/{memo['id']}/tags", data={"tags": "x,y,z,a,b,c"}, headers=headers).status_code, 400)
            finally:
                if previous_password is None:
                    os.environ.pop("DELETE_PASSWORD", None)
                else:
                    os.environ["DELETE_PASSWORD"] = previous_password

    def test_unicode_tag_filter_keeps_selected_chip_highlighted(self):
        with isolated_service() as (service, app):
            video = make_video(service, "dQw4w9WgXcQ", "독일어 영상")
            service.create_memo(video["id"], "어휘", "내용", tags="Straße")

            with TestClient(app) as client:
                response = client.get("/", params={"tag": "STRASSE"})

            self.assertEqual(response.status_code, 200)
            self.assertIn("독일어 영상", response.text)
            self.assertIn("#Straße", response.text)
            self.assertIn('href="/?tag=Stra%C3%9Fe" aria-current="page"', response.text)

    def test_search_ranking_deduplication_and_context(self):
        with isolated_service() as (service, _app):
            content = make_video(service, "dQw4w9WgXcQ", "다른 영상")
            titled = make_video(service, "abcdefghijk", "Python 튜토리얼")
            service.create_memo(content["id"], "두 번째 메모", "python 내용")
            service.create_memo(content["id"], "본문 메모", "앞" * 100 + "python 핵심 장면" + "뒤" * 100)
            service.create_memo(titled["id"], "제목 메모", "본문")
            results = service.search_videos_and_memos("PYTHON")
            self.assertEqual([result["url"] for result in results], [f"/videos/{titled['id']}", f"/videos/{content['id']}"])
            self.assertIn("python 핵심 장면", results[1]["snippet"])
            self.assertEqual(service.search_videos_and_memos("%"), [])
            self.assertEqual(service.search_videos_and_memos("_"), [])

    def test_exact_memo_title_precedes_partial_video_title(self):
        with isolated_service() as (service, _app):
            video_partial = make_video(service, "dQw4w9WgXcQ", "Python 튜토리얼")
            memo_exact = make_video(service, "abcdefghijk", "다른 영상")
            service.create_memo(video_partial["id"], "일반 메모", "본문")
            service.create_memo(memo_exact["id"], "Python", "본문")

            results = service.search_videos_and_memos("python")

            self.assertEqual(
                [result["url"] for result in results],
                [f"/videos/{memo_exact['id']}", f"/videos/{video_partial['id']}"],
            )

    def test_video_title_match_does_not_show_unrelated_memo_snippet(self):
        with isolated_service() as (service, _app):
            video = make_video(service, "dQw4w9WgXcQ", "Python 학습")
            service.create_memo(video["id"], "무관한 메모", "검색어와 관계없는 개인 기록")

            results = service.search_videos_and_memos("Python")

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["title"], "Python 학습")
            self.assertEqual(results[0]["snippet"], "")


@contextmanager
def isolated_export_service():
    previous_password = os.environ.get("DELETE_PASSWORD")
    os.environ["DELETE_PASSWORD"] = "export-test-password"
    try:
        with isolated_service() as (service, app):
            yield app, service
    finally:
        if previous_password is None:
            os.environ.pop("DELETE_PASSWORD", None)
        else:
            os.environ["DELETE_PASSWORD"] = previous_password


def login(client):
    response = client.post(
        "/auth/login",
        data={"password": "export-test-password"},
        headers={"Origin": "https://memo.len.pe.kr"},
        follow_redirects=False,
    )
    assert response.status_code == 303


class YoutubeExportTests(unittest.TestCase):
    def test_home_puts_video_registration_before_export_and_keeps_login_path(self):
        with isolated_export_service() as (app, _service):
            with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                home = client.get("/")
                login(client)
                authenticated_home = client.get("/")

        self.assertEqual((home.status_code, authenticated_home.status_code), (200, 200))
        for response in (home, authenticated_home):
            self.assertLess(response.text.index("영상 등록"), response.text.index("전체 기록 내보내기"))
            self.assertLess(response.text.index("전체 기록 내보내기"), response.text.index("저장한 영상"))
        self.assertIn('href="/auth/login?next_path=%2F">로그인 후 내보내기</a>', home.text)
        self.assertIn('href="/api/export?format=markdown">Markdown 다운로드</a>', authenticated_home.text)
        self.assertIn('href="/api/export?format=json">JSON 다운로드</a>', authenticated_home.text)

    def test_export_requires_session_and_does_not_expose_records(self):
        with isolated_export_service() as (app, service):
            service.create_or_get_video("dQw4w9WgXcQ", title_fetcher=lambda *_: "개인 영상")
            with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                response = client.get("/api/export?format=json")

        self.assertEqual(response.status_code, 401)
        self.assertNotIn("개인 영상", response.text)
        self.assertNotIn("attachment", response.headers.get("content-disposition", ""))

    def test_json_export_includes_all_videos_memos_tags_and_timestamps_in_stable_order(self):
        with isolated_export_service() as (app, service):
            first = service.create_or_get_video("dQw4w9WgXcQ", title_fetcher=lambda *_: "첫 영상")
            service.create_memo(first["id"], "한국어 메모", "00:15 장면\n01:02 다시 보기", "학습, 참고")
            second = service.create_or_get_video("abcdefghijk", title_fetcher=lambda *_: "메모 없는 영상")
            for number in range(25):
                service.create_or_get_video(f"id{number:09d}", title_fetcher=lambda *_: f"나머지 {number}")
            with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                login(client)
                response = client.get("/api/export")
                repeated = client.get("/api/export?format=json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, repeated.content)
        self.assertEqual(response.headers["content-type"], "application/json; charset=utf-8")
        self.assertIn('attachment; filename="youtube-memo-export.json"', response.headers["content-disposition"])
        payload = json.loads(response.content)
        self.assertEqual(payload["metadata"], {"service": "youtube-memo", "schema_version": 1, "video_count": 27, "memo_count": 1})
        self.assertEqual(len(payload["records"]), 27)
        self.assertEqual(payload["records"][0]["id"], first["id"])
        self.assertEqual(payload["records"][1]["id"], second["id"])
        self.assertEqual(payload["records"][1]["memos"], [])
        memo = payload["records"][0]["memos"][0]
        self.assertEqual(memo["content"], "00:15 장면\n01:02 다시 보기")
        self.assertEqual(memo["tags"], ["학습", "참고"])
        self.assertEqual(memo["timestamps"], [{"text": "00:15", "seconds": 15}, {"text": "01:02", "seconds": 62}])

    def test_markdown_export_preserves_content_and_escapes_untrusted_title(self):
        with isolated_export_service() as (app, service):
            video = service.create_or_get_video("dQw4w9WgXcQ", title_fetcher=lambda *_: "<script> # 영상")
            service.create_memo(video["id"], "# 제목", "줄 1\n```\n줄 2 00:30", "태그")
            with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                login(client)
                response = client.get("/api/export?format=markdown")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/markdown; charset=utf-8")
        self.assertIn('attachment; filename="youtube-memo-export.md"', response.headers["content-disposition"])
        self.assertIn("&lt;script&gt;", response.text)
        self.assertNotIn("## <script>", response.text)
        self.assertIn("줄 1\n```\n줄 2 00:30", response.text)
        self.assertIn("00:30", response.text)

    def test_invalid_format_is_rejected_after_authentication(self):
        with isolated_export_service() as (app, _service):
            with TestClient(app, base_url="https://memo.len.pe.kr") as client:
                login(client)
                response = client.get("/api/export?format=csv")

        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
