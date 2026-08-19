import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests._test_support import prepare_service_import


class PortalDashboardTests(unittest.TestCase):
    def reload_system_status(self, demo_mode: str = ""):
        prepare_service_import("portal-web")
        os.environ["DEMO_MODE"] = demo_mode
        import app.services.system_status as system_status

        return importlib.reload(system_status)

    def load_app(self):
        prepare_service_import("portal-web")
        import app.main as main

        return importlib.reload(main).app

    def test_demo_mode_returns_sample_status(self):
        system_status = self.reload_system_status("true")

        status = system_status.get_dashboard_status()

        self.assertTrue(status["demo_mode"])
        self.assertEqual(status["overall_status"], "ok")
        self.assertEqual(status["host"]["source"], "demo")

    def test_agent_failure_returns_unavailable_status(self):
        system_status = self.reload_system_status("")

        with patch("app.services.system_status.urlopen", side_effect=OSError("down")):
            status = system_status.get_dashboard_status(agent_url="http://system-agent:8010", timeout=0.01)

        self.assertFalse(status["demo_mode"])
        self.assertEqual(status["overall_status"], "unavailable")
        self.assertIn("system_agent_unavailable", status["warnings"])

    def test_search_result_relative_urls_are_prefixed(self):
        prepare_service_import("portal-web")
        from app.services.global_search import _normalize_result_url

        result = _normalize_result_url(
            "youtube",
            {"title": "memo", "url": "/videos/1"},
            public_base_urls={
                "news": "https://news.len.pe.kr",
                "youtube": "https://memo.len.pe.kr",
                "books": "https://books.len.pe.kr",
            },
            local_base_urls={
                "news": "http://127.0.0.1:8001",
                "youtube": "http://127.0.0.1:8002",
                "books": "http://127.0.0.1:8003",
            },
            prefer_local=True,
        )

        self.assertEqual(result["url"], "http://127.0.0.1:8002/videos/1")

    def test_search_result_relative_urls_use_public_domain_outside_local(self):
        prepare_service_import("portal-web")
        from app.services.global_search import _normalize_result_url

        result = _normalize_result_url(
            "books",
            {"title": "memo", "url": "/books/1"},
            public_base_urls={
                "news": "https://news.len.pe.kr",
                "youtube": "https://memo.len.pe.kr",
                "books": "https://books.len.pe.kr",
            },
            local_base_urls={
                "news": "http://127.0.0.1:8001",
                "youtube": "http://127.0.0.1:8002",
                "books": "http://127.0.0.1:8003",
            },
            prefer_local=False,
        )

        self.assertEqual(result["url"], "https://books.len.pe.kr/books/1")

    def test_demo_mode_returns_service_health_samples(self):
        system_status = self.reload_system_status("true")

        services = system_status.get_service_health()

        self.assertTrue(services)
        self.assertTrue(all(service["status"] == "ok" for service in services))
        self.assertTrue(all(service["demo_mode"] for service in services))

    def test_service_health_failure_is_unavailable(self):
        system_status = self.reload_system_status("")

        with patch("app.services.system_status.urlopen", side_effect=OSError("down")):
            services = system_status.get_service_health(timeout=0.01)

        self.assertIn(
            {"name": "뉴스 허브", "status": "unavailable", "url": "http://crawler-worker:8001/health"},
            services,
        )

    def test_demo_search_results_include_metadata(self):
        prepare_service_import("portal-web")
        os.environ["DEMO_MODE"] = "true"
        from app.services import global_search

        results = global_search.search_all("테스트")

        self.assertIn("meta", results["youtube"][0])
        self.assertIn("snippet", results["youtube"][0])

    def test_portal_home_url_uses_local_address_on_localhost(self):
        prepare_service_import("book-memo")
        os.environ.pop("PORTAL_HOME_URL", None)
        from app.services.host_urls import portal_home_url

        self.assertEqual(portal_home_url("127.0.0.1"), "http://127.0.0.1:8000/")

    def test_portal_home_url_uses_public_address_outside_local(self):
        prepare_service_import("youtube-memo")
        os.environ.pop("PORTAL_HOME_URL", None)
        from app.services.host_urls import portal_home_url

        self.assertEqual(portal_home_url("memo.len.pe.kr"), "https://len.pe.kr/")

    def test_portal_home_url_uses_env_public_address(self):
        prepare_service_import("youtube-memo")
        os.environ["PORTAL_HOME_URL"] = "https://example.com/"
        from app.services.host_urls import portal_home_url

        try:
            self.assertEqual(portal_home_url("memo.len.pe.kr"), "https://example.com/")
        finally:
            os.environ.pop("PORTAL_HOME_URL", None)

    def test_dashboard_service_urls_follow_host_mode(self):
        prepare_service_import("portal-web")
        from app.services.host_urls import service_url

        self.assertEqual(service_url("NEWS_SERVICE_URL", "127.0.0.1", ""), "http://127.0.0.1:8001")
        self.assertEqual(service_url("YOUTUBE_MEMO_URL", "127.0.0.1", ""), "http://127.0.0.1:8002")
        self.assertEqual(service_url("BOOK_MEMO_URL", "127.0.0.1", ""), "http://127.0.0.1:8003")
        self.assertEqual(service_url("NEWS_SERVICE_URL", "portal.len.pe.kr", ""), "https://news.len.pe.kr")
        self.assertEqual(service_url("YOUTUBE_MEMO_URL", "portal.len.pe.kr", ""), "https://memo.len.pe.kr")
        self.assertEqual(service_url("BOOK_MEMO_URL", "portal.len.pe.kr", ""), "https://books.len.pe.kr")

    def test_dashboard_keeps_original_service_hub_with_existing_entry_points(self):
        """Fails if a visual refactor replaces the original hub or disconnects routes."""
        app = self.load_app()

        with TestClient(app) as client:
            response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("<h1>Len의 개인서버</h1>", response.text)
        self.assertIn('class="service-grid three"', response.text)
        for url in ("/news", "/memo", "/books", "/files", "/admin/status"):
            self.assertIn(f'href="{url}"', response.text)
        self.assertIn('class="service-card disabled" href="#"', response.text)
        self.assertIn('class="global-search" method="get" action="/"', response.text)
        self.assertIn('name="q"', response.text)
        self.assertIn('data-track-event="global_search_submitted"', response.text)
        self.assertIn('data-track-event="service_opened"', response.text)

    def test_admin_status_keeps_original_title_and_login_form_action(self):
        """Fails if the original admin page or its authentication route is disconnected."""
        app = self.load_app()

        with TestClient(app) as client:
            response = client.get("/admin/status")

        self.assertEqual(response.status_code, 200)
        self.assertIn("<h1>관리자 상태</h1>", response.text)
        self.assertIn('class="admin-login" action="/admin/status" method="post"', response.text)
        self.assertIn('name="password"', response.text)

    def test_admin_status_context_combines_server_and_security_data(self):
        prepare_service_import("portal-web")
        from app.services.admin_status import build_admin_status_context

        context = build_admin_status_context(
            system_status={"overall_status": "warning", "warnings": ["backup_missing"]},
            service_health=[{"name": "뉴스 허브", "status": "ok"}],
            security={"headers": ["X-Frame-Options"], "recent_events": []},
        )

        self.assertEqual(context["system_status"]["overall_status"], "warning")
        self.assertEqual(context["service_health"][0]["name"], "뉴스 허브")
        self.assertEqual(context["security_status"]["headers"], ["X-Frame-Options"])
        self.assertTrue(context["has_warnings"])

    def test_admin_status_checked_at_is_formatted_for_display(self):
        prepare_service_import("portal-web")
        from app.services.admin_status import format_status_checked_at

        self.assertEqual(
            format_status_checked_at("2026-07-09T01:02:03+00:00"),
            "2026-07-09 10:02:03 KST",
        )
        self.assertEqual(format_status_checked_at(""), "unknown")
        self.assertEqual(format_status_checked_at("not-a-timestamp"), "unknown")

    def test_admin_status_login_disables_cache(self):
        app = self.load_app()

        with TestClient(app) as client:
            response = client.get("/admin/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store, no-cache, must-revalidate, max-age=0")
        self.assertEqual(response.headers["pragma"], "no-cache")
        self.assertEqual(response.headers["expires"], "0")

    def test_admin_status_failure_disables_cache(self):
        os.environ["FILE_MANAGER_PASSWORD"] = "secret"
        try:
            app = self.load_app()

            with TestClient(app) as client:
                response = client.post(
                    "/admin/status",
                    data={"password": "wrong"},
                    headers={"Origin": "http://testserver"},
                )

            self.assertIn(response.status_code, {401, 403, 429})
            self.assertEqual(response.headers["cache-control"], "no-store, no-cache, must-revalidate, max-age=0")
            self.assertEqual(response.headers["pragma"], "no-cache")
            self.assertEqual(response.headers["expires"], "0")
        finally:
            os.environ.pop("FILE_MANAGER_PASSWORD", None)

    def test_admin_status_uses_dedicated_password_when_configured(self):
        environment_keys = (
            "ADMIN_STATUS_PASSWORD",
            "AUTH_RATE_LIMIT_STATE_PATH",
            "DELETE_PASSWORD",
            "DEMO_MODE",
            "FILE_MANAGER_PASSWORD",
            "SECURITY_LOG_PATH",
        )
        original_environment = {key: os.environ.get(key) for key in environment_keys}
        try:
            with tempfile.TemporaryDirectory() as tempdir:
                os.environ["ADMIN_STATUS_PASSWORD"] = "dedicated-admin-password"
                os.environ["FILE_MANAGER_PASSWORD"] = "file-manager-password"
                os.environ["DELETE_PASSWORD"] = "delete-password"
                os.environ["AUTH_RATE_LIMIT_STATE_PATH"] = str(Path(tempdir) / "auth-rate-limit.json")
                os.environ["SECURITY_LOG_PATH"] = str(Path(tempdir) / "security-events.txt")
                os.environ["DEMO_MODE"] = "true"
                app = self.load_app()

                with TestClient(app) as client:
                    delete_password = client.post(
                        "/admin/status",
                        data={"password": "delete-password"},
                        headers={"Origin": "http://testserver"},
                    )
                    file_manager_password = client.post(
                        "/admin/status",
                        data={"password": "file-manager-password"},
                        headers={"Origin": "http://testserver"},
                    )
                    dedicated_password = client.post(
                        "/admin/status",
                        data={"password": "dedicated-admin-password"},
                        headers={"Origin": "http://testserver"},
                    )

            self.assertEqual(delete_password.status_code, 401)
            self.assertEqual(file_manager_password.status_code, 401)
            self.assertEqual(dedicated_password.status_code, 200)
        finally:
            for key, value in original_environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_admin_status_page_renders_actual_host_collection_time(self):
        """Fails if the page substitutes the dashboard request time for the host sample time."""
        os.environ["FILE_MANAGER_PASSWORD"] = "secret"
        try:
            app = self.load_app()
            system_status = {
                "captured_at": "2026-07-09T01:02:03+00:00",
                "overall_status": "ok",
                "host": {
                    "captured_at": "2026-07-09T00:40:00+00:00",
                    "cpu_percent": 12,
                    "memory_percent": 34,
                    "disk_percent": 56,
                    "source": "agent",
                },
                "disk": {"percent": 56, "level": "ok"},
                "files": {"file_count": 2, "total_bytes": 100},
                "backup": {"latest_name": "", "status": "ok", "status_reason": "backup_recent"},
                "containers": [],
                "status_checks": [],
                "warnings": [],
            }
            security = {
                "headers": [],
                "file_policy": {
                    "max_upload_mb": 50,
                    "blocked_extensions": [],
                    "allowed_extensions": [],
                },
                "log_files": [],
                "recent_events": [],
                "log_path": "/tmp/security.log",
            }

            with patch(
                "app.routers.dashboard.get_dashboard_status",
                return_value=system_status,
            ), patch("app.routers.dashboard.get_service_health", return_value=[]), patch(
                "app.routers.dashboard.security_status",
                return_value=security,
            ):
                with TestClient(app) as client:
                    response = client.post(
                        "/admin/status",
                        data={"password": "secret"},
                        headers={"Origin": "http://testserver"},
                    )

            self.assertEqual(response.status_code, 200)
            self.assertIn("호스트 수집 시각:", response.text)
            self.assertIn(
                '<time datetime="2026-07-09T00:40:00+00:00">2026-07-09 09:40:00 KST</time>',
                response.text,
            )
            self.assertNotIn("2026-07-09 10:02:03 KST", response.text)
        finally:
            os.environ.pop("FILE_MANAGER_PASSWORD", None)

    def test_admin_status_page_shows_unknown_without_host_collection_time(self):
        """Fails if missing host timestamps are presented as a misleading request timestamp."""
        os.environ["FILE_MANAGER_PASSWORD"] = "secret"
        try:
            app = self.load_app()
            system_status = {
                "captured_at": "2026-07-09T01:02:03+00:00",
                "overall_status": "unavailable",
                "host": {
                    "captured_at": None,
                    "cpu_percent": None,
                    "memory_percent": None,
                    "disk_percent": None,
                    "source": "unavailable",
                },
                "disk": {"percent": None, "level": "unknown"},
                "files": {"file_count": 0, "total_bytes": 0},
                "backup": {"latest_name": "", "status": "ok", "status_reason": "backup_recent"},
                "containers": [],
                "status_checks": [],
                "warnings": ["system_agent_unavailable"],
            }
            security = {
                "headers": [],
                "file_policy": {
                    "max_upload_mb": 50,
                    "blocked_extensions": [],
                    "allowed_extensions": [],
                },
                "log_files": [],
                "recent_events": [],
                "log_path": "/tmp/security.log",
            }

            with patch(
                "app.routers.dashboard.get_dashboard_status",
                return_value=system_status,
            ), patch("app.routers.dashboard.get_service_health", return_value=[]), patch(
                "app.routers.dashboard.security_status",
                return_value=security,
            ):
                with TestClient(app) as client:
                    response = client.post(
                        "/admin/status",
                        data={"password": "secret"},
                        headers={"Origin": "http://testserver"},
                    )
                    system_status["host"]["captured_at"] = "not-a-timestamp"
                    invalid_response = client.post(
                        "/admin/status",
                        data={"password": "secret"},
                        headers={"Origin": "http://testserver"},
                    )

            for status_response in (response, invalid_response):
                self.assertEqual(status_response.status_code, 200)
                self.assertIn("호스트 수집 시각:", status_response.text)
                self.assertIn("<time>unknown</time>", status_response.text)
                self.assertNotIn("datetime=", status_response.text)
                self.assertNotIn("2026-07-09 10:02:03 KST", status_response.text)
        finally:
            os.environ.pop("FILE_MANAGER_PASSWORD", None)

    def test_admin_status_renders_homeops_diagnosis_controls_as_an_operation_panel(self):
        """Fails if the HomeOps form falls back to an unstyled browser-default control."""
        os.environ["ADMIN_STATUS_PASSWORD"] = "secret"
        try:
            app = self.load_app()

            with TestClient(app) as client:
                response = client.post(
                    "/admin/status",
                    data={"password": "secret"},
                    headers={"Origin": "http://testserver"},
                )

            self.assertEqual(response.status_code, 200)
            self.assertIn("homeops-diagnosis-controls", response.text)
            self.assertIn('class="homeops-service-select"', response.text)
            self.assertIn('class="homeops-diagnose-button"', response.text)
            self.assertIn('class="homeops-policy-grid"', response.text)
            self.assertIn("전체 서비스 스캔", response.text)
        finally:
            os.environ.pop("ADMIN_STATUS_PASSWORD", None)


if __name__ == "__main__":
    unittest.main()
