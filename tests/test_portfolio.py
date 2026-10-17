import importlib
import os
import tempfile
import unittest
from pathlib import Path

from tests._test_support import prepare_service_import


class PortfolioStoreTests(unittest.TestCase):
    def _load_store(self, content_path: Path):
        prepare_service_import("portal-web")
        os.environ["PORTFOLIO_CONTENT_PATH"] = str(content_path)
        import app.services.portfolio_store as portfolio_store

        return importlib.reload(portfolio_store)

    def test_saves_and_loads_utf8_markdown_without_data_loss(self):
        with tempfile.TemporaryDirectory() as tempdir:
            content_path = Path(tempdir) / "portfolio" / "portfolio.md"
            store = self._load_store(content_path)
            content = "# 김길동\n\n백엔드 개발자 · 개인 서버 운영자\n\n- 한글 이력"

            store.save_portfolio_content(content, expected_revision=store.load_portfolio_snapshot()[1])

            self.assertEqual(store.load_portfolio_content(), content)
            self.assertEqual(content_path.read_text(encoding="utf-8"), content)

    def test_renders_raw_html_and_javascript_links_as_safe_content(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = self._load_store(Path(tempdir) / "portfolio.md")

            rendered = store.render_portfolio_markdown(
                '<script>alert("xss")</script>\n\n[unsafe](javascript:alert("xss"))'
            )

            self.assertNotIn("<script>", rendered)
            self.assertNotIn('href="javascript:', rendered.lower())
            self.assertIn("[unsafe](javascript:alert", rendered.lower())

    def test_returns_empty_content_when_portfolio_file_is_missing(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = self._load_store(Path(tempdir) / "missing" / "portfolio.md")

            self.assertEqual(store.load_portfolio_content(), "")

    def test_stale_revision_cannot_overwrite_another_save(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = self._load_store(Path(tempdir) / "portfolio.md")
            initial, revision = store.load_portfolio_snapshot()
            saved_revision = store.save_portfolio_content("first tab", expected_revision=revision)
            with self.assertRaises(store.PortfolioConflict):
                store.save_portfolio_content("old second tab", expected_revision=revision)
            self.assertEqual(store.load_portfolio_snapshot(), ("first tab", saved_revision))
            self.assertEqual(initial, "")

    def test_same_revision_racing_writers_have_one_winner(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as tempdir:
            store = self._load_store(Path(tempdir) / "portfolio.md")
            revision = store.load_portfolio_snapshot()[1]
            barrier = threading.Barrier(2)
            def save(content):
                barrier.wait(timeout=5)
                try:
                    store.save_portfolio_content(content, expected_revision=revision)
                    return "saved"
                except store.PortfolioConflict:
                    return "conflict"
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(save, ["writer one", "writer two"]))
            self.assertCountEqual(results, ["saved", "conflict"])

    @unittest.skipUnless(os.name == "posix", "production file locking uses POSIX")
    def test_separate_processes_cannot_both_save_the_same_revision(self):
        import multiprocessing
        with tempfile.TemporaryDirectory() as tempdir:
            store = self._load_store(Path(tempdir) / "portfolio.md")
            revision = store.load_portfolio_snapshot()[1]
            ctx = multiprocessing.get_context("fork")
            start, results = ctx.Event(), ctx.Queue()
            def save(content):
                start.wait(timeout=5)
                try:
                    store.save_portfolio_content(content, expected_revision=revision)
                    results.put("saved")
                except store.PortfolioConflict:
                    results.put("conflict")
            writers = [ctx.Process(target=save, args=(text,)) for text in ["process one", "process two"]]
            for writer in writers:
                writer.start()
            start.set()
            observed = [results.get(timeout=10) for _ in writers]
            for writer in writers:
                writer.join(timeout=10)
                if writer.is_alive():
                    writer.terminate()
                    writer.join(timeout=5)
                self.assertEqual(writer.exitcode, 0)
            self.assertCountEqual(observed, ["saved", "conflict"])
            results.close()

    def test_failed_replace_preserves_existing_content_and_removes_temporary_file(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "portfolio.md"
            store = self._load_store(path)
            revision = store.save_portfolio_content("original", expected_revision=store.load_portfolio_snapshot()[1])
            with patch.object(store.os, "replace", side_effect=OSError("disk write failed")):
                with self.assertRaises(OSError):
                    store.save_portfolio_content("new data", expected_revision=revision)
            self.assertEqual(store.load_portfolio_content(), "original")
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_response_loss_retry_of_identical_content_is_safe(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = self._load_store(Path(tempdir) / "portfolio.md")
            revision = store.load_portfolio_snapshot()[1]
            saved = store.save_portfolio_content("same submitted content", expected_revision=revision)
            retry = store.save_portfolio_content("same submitted content", expected_revision=revision)
            self.assertEqual(saved, retry)

    def test_multiline_newlines_keep_returned_revision_and_retry_consistent(self):
        for newline in ["\r\n", "\r", "\n"]:
            with self.subTest(newline=repr(newline)), tempfile.TemporaryDirectory() as tempdir:
                store = self._load_store(Path(tempdir) / "portfolio.md")
                initial = store.load_portfolio_snapshot()[1]
                source = f"first{newline}second"
                saved = store.save_portfolio_content(source, expected_revision=initial)
                self.assertEqual(store.load_portfolio_snapshot(), ("first\nsecond", saved))
                self.assertEqual(store.save_portfolio_content(source, expected_revision=initial), saved)
                store.save_portfolio_content(source + newline + "third", expected_revision=saved)
                self.assertEqual(store.load_portfolio_content(), "first\nsecond\nthird")

    def test_invalid_revision_never_replaces_content(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = self._load_store(Path(tempdir) / "portfolio.md")
            store.save_portfolio_content("original", expected_revision=store.load_portfolio_snapshot()[1])
            for revision in ["", "not-a-revision", "0" * 64]:
                with self.subTest(revision=revision), self.assertRaises(store.PortfolioConflict):
                    store.save_portfolio_content("overwrite", expected_revision=revision)
            self.assertEqual(store.load_portfolio_content(), "original")


class PortfolioRoutesTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self._environment = {
            key: os.environ.get(key)
            for key in ("APP_ENV", "PORTFOLIO_CONTENT_PATH", "PORTFOLIO_ADMIN_PASSWORD", "SECURITY_LOG_PATH")
        }
        prepare_service_import("portal-web")
        os.environ["APP_ENV"] = "production"
        os.environ["PORTFOLIO_CONTENT_PATH"] = str(Path(self.tempdir.name) / "portfolio.md")
        os.environ["PORTFOLIO_ADMIN_PASSWORD"] = "test-portfolio-password"
        os.environ["SECURITY_LOG_PATH"] = str(Path(self.tempdir.name) / "security-events.txt")

        import app.services.portfolio_store as portfolio_store
        import app.services.security as security
        import app.main as main

        importlib.reload(security)
        importlib.reload(portfolio_store)
        self.app = importlib.reload(main).app

    def tearDown(self):
        self.tempdir.cleanup()
        for key, value in self._environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _revision(self, client):
        import re
        page = client.get("/admin")
        match = re.search(r'name="expected_revision" value="([a-f0-9]{64})"', page.text)
        self.assertIsNotNone(match)
        return match.group(1)

    def test_conflict_preserves_input_and_reports_current_revision(self):
        from fastapi.testclient import TestClient
        with TestClient(self.app, base_url="https://portfolio.len.pe.kr") as client:
            headers = {"Origin": "https://portfolio.len.pe.kr"}
            client.post("/admin/login", data={"password": "test-portfolio-password"}, headers=headers)
            revision = self._revision(client)
            first = client.post("/admin/save", data={"content": "first saved", "expected_revision": revision}, headers=headers, follow_redirects=False)
            conflict = client.post("/admin/save", data={"content": "second draft <script>", "expected_revision": revision}, headers=headers, follow_redirects=False)
            report = client.post("/admin/save", data={"content": "second draft", "expected_revision": revision}, headers={**headers, "Accept": "application/json"})
            public = client.get("/")
        self.assertEqual(first.status_code, 303)
        self.assertEqual(conflict.status_code, 409)
        self.assertIn("second draft &lt;script&gt;", conflict.text)
        self.assertIn("first saved", conflict.text)
        self.assertIn('required', conflict.text)
        self.assertIn("no-store", conflict.headers['cache-control'])
        self.assertEqual(report.status_code, 409)
        self.assertEqual(report.json()['current_content'], "first saved")
        self.assertIn("first saved", public.text)
        self.assertNotIn("second draft", public.text)

    def test_missing_revision_rejected_and_json_success_returns_new_revision(self):
        from fastapi.testclient import TestClient
        with TestClient(self.app, base_url="https://portfolio.len.pe.kr") as client:
            headers = {"Origin": "https://portfolio.len.pe.kr", "Accept": "application/json"}
            client.post("/admin/login", data={"password": "test-portfolio-password"}, headers=headers)
            missing = client.post("/admin/save", data={"content": "must not save"}, headers=headers)
            revision = self._revision(client)
            saved = client.post("/admin/save", data={"content": "safe save", "expected_revision": revision}, headers=headers)
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(saved.status_code, 200)
        self.assertNotEqual(saved.json()['revision'], revision)

    def test_portfolio_host_renders_public_content_without_dashboard_links(self):
        from fastapi.testclient import TestClient

        with TestClient(self.app, base_url="https://portfolio.len.pe.kr") as client:
            response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("포트폴리오", response.text)
        self.assertNotIn('href="/admin"', response.text)
        self.assertNotIn('href="/files"', response.text)

    def test_non_portfolio_host_keeps_existing_dashboard_at_root(self):
        from fastapi.testclient import TestClient

        with TestClient(self.app, base_url="https://len.pe.kr") as client:
            response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Len의 개인서버", response.text)

    def test_admin_requires_login_and_disables_cache(self):
        from fastapi.testclient import TestClient

        with TestClient(self.app, base_url="https://portfolio.len.pe.kr") as client:
            response = client.get("/admin")

        self.assertEqual(response.status_code, 200)
        self.assertIn("포트폴리오 관리자", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store, no-cache, must-revalidate, max-age=0")

    def test_public_host_login_cookie_is_secure_without_production_flag(self):
        from fastapi.testclient import TestClient
        from unittest.mock import patch
        with patch.dict(os.environ, {"APP_ENV": ""}):
            with TestClient(self.app, base_url="http://portfolio.len.pe.kr") as client:
                response = client.post(
                    "/admin/login", data={"password": "test-portfolio-password"},
                    headers={"Origin": "https://portfolio.len.pe.kr"}, follow_redirects=False,
                )
        self.assertEqual(response.status_code, 303)
        self.assertIn("secure", response.headers["set-cookie"].lower())
        self.assertIn("httponly", response.headers["set-cookie"].lower())

    def test_production_login_sets_random_scoped_session_cookie_and_allows_save(self):
        from fastapi.testclient import TestClient

        with TestClient(self.app, base_url="https://portfolio.len.pe.kr") as client:
            login = client.post(
                "/admin/login",
                data={"password": "test-portfolio-password"},
                headers={"Origin": "https://portfolio.len.pe.kr"},
                follow_redirects=False,
            )
            second_login = client.post(
                "/admin/login",
                data={"password": "test-portfolio-password"},
                headers={"Origin": "https://portfolio.len.pe.kr"},
                follow_redirects=False,
            )
            save = client.post(
                "/admin/save",
                data={"content": "# 김길동\n\n개인 프로젝트", "expected_revision": self._revision(client)},
                headers={"Origin": "https://portfolio.len.pe.kr"},
                follow_redirects=False,
            )
            public = client.get("/")

        self.assertEqual(login.status_code, 303)
        cookie = login.headers["set-cookie"].lower()
        self.assertIn("portfolio_admin_access=", cookie)
        self.assertIn("httponly", cookie)
        self.assertIn("secure", cookie)
        self.assertIn("samesite=lax", cookie)
        self.assertIn("path=/admin", cookie)
        self.assertIn("max-age=28800", cookie)
        self.assertNotEqual(cookie.split(";", 1)[0], second_login.headers["set-cookie"].lower().split(";", 1)[0])
        self.assertEqual(save.status_code, 303)
        self.assertIn("김길동", public.text)

    def test_portfolio_session_is_rejected_after_security_service_restart(self):
        import app.services.security as security
        from fastapi.testclient import TestClient

        with TestClient(self.app, base_url="https://portfolio.len.pe.kr") as client:
            client.post(
                "/admin/login",
                data={"password": "test-portfolio-password"},
                headers={"Origin": "https://portfolio.len.pe.kr"},
                follow_redirects=False,
            )
            self.assertIn("포트폴리오 편집", client.get("/admin").text)

            importlib.reload(security)
            after_restart = client.get("/admin")

        self.assertEqual(after_restart.status_code, 200)
        self.assertIn("포트폴리오 관리자", after_restart.text)

    def test_save_rejects_unauthenticated_requests(self):
        from fastapi.testclient import TestClient

        with TestClient(self.app, base_url="https://portfolio.len.pe.kr") as client:
            response = client.post(
                "/admin/save",
                data={"content": "# 변경 시도"},
                headers={"Origin": "https://portfolio.len.pe.kr"},
            )

        self.assertEqual(response.status_code, 401)

    def test_admin_save_rejects_cross_origin_session_request_and_accepts_same_origin_form(self):
        """Fails if a portfolio admin session can be used by a foreign-origin form."""
        from fastapi.testclient import TestClient

        with TestClient(self.app, base_url="https://portfolio.len.pe.kr") as client:
            same_origin = {"Origin": "https://portfolio.len.pe.kr"}
            login = client.post(
                "/admin/login",
                data={"password": "test-portfolio-password"},
                headers=same_origin,
                follow_redirects=False,
            )
            rejected = client.post(
                "/admin/save",
                data={"content": "# 공격 시도"},
                headers={"Origin": "https://attacker.example"},
                follow_redirects=False,
            )
            accepted = client.post(
                "/admin/save",
                data={"content": "# 동일 출처 저장", "expected_revision": self._revision(client)},
                headers=same_origin,
                follow_redirects=False,
            )

        self.assertEqual(login.status_code, 303)
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(accepted.status_code, 303)

    def test_admin_routes_are_not_available_from_personal_server_host(self):
        from fastapi.testclient import TestClient

        with TestClient(self.app, base_url="https://len.pe.kr") as client:
            response = client.get("/admin")

        self.assertEqual(response.status_code, 404)

    def test_portfolio_host_blocks_existing_private_routes(self):
        from fastapi.testclient import TestClient

        with TestClient(self.app) as client:
            response = client.get("/admin/status", headers={"host": "portfolio.len.pe.kr"})

        self.assertEqual(response.status_code, 404)

    def test_spoofed_forwarded_host_does_not_bypass_portfolio_route_blocking(self):
        from fastapi.testclient import TestClient

        with TestClient(self.app) as client:
            response = client.get(
                "/admin/status",
                headers={"host": "portfolio.len.pe.kr", "x-forwarded-host": "len.pe.kr"},
            )

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
