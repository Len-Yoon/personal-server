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

            store.save_portfolio_content(content)

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


class PortfolioRoutesTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        prepare_service_import("portal-web")
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
        for key in ("PORTFOLIO_CONTENT_PATH", "PORTFOLIO_ADMIN_PASSWORD", "SECURITY_LOG_PATH"):
            os.environ.pop(key, None)

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

    def test_login_sets_scoped_secure_hmac_cookie_and_allows_save(self):
        from fastapi.testclient import TestClient

        with TestClient(self.app, base_url="https://portfolio.len.pe.kr") as client:
            login = client.post(
                "/admin/login",
                data={"password": "test-portfolio-password"},
                follow_redirects=False,
            )
            save = client.post(
                "/admin/save",
                data={"content": "# 김길동\n\n개인 프로젝트"},
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
        self.assertEqual(save.status_code, 303)
        self.assertIn("김길동", public.text)

    def test_save_rejects_unauthenticated_requests(self):
        from fastapi.testclient import TestClient

        with TestClient(self.app, base_url="https://portfolio.len.pe.kr") as client:
            response = client.post("/admin/save", data={"content": "# 변경 시도"})

        self.assertEqual(response.status_code, 401)

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
