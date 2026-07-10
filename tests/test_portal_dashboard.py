import importlib
import os
import unittest
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
            "2026-07-09 01:02:03 UTC",
        )
        self.assertEqual(format_status_checked_at(""), "unknown")

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
                response = client.post("/admin/status", data={"password": "wrong"})

            self.assertIn(response.status_code, {401, 403, 429})
            self.assertEqual(response.headers["cache-control"], "no-store, no-cache, must-revalidate, max-age=0")
            self.assertEqual(response.headers["pragma"], "no-cache")
            self.assertEqual(response.headers["expires"], "0")
        finally:
            os.environ.pop("FILE_MANAGER_PASSWORD", None)

    def test_admin_status_page_renders_formatted_collection_time(self):
        os.environ["FILE_MANAGER_PASSWORD"] = "secret"
        try:
            app = self.load_app()

            with patch("app.routers.dashboard.get_dashboard_status") as get_dashboard_status, patch(
                "app.routers.dashboard.get_service_health",
                return_value=[],
            ), patch("app.routers.dashboard.security_status", return_value={"headers": [], "file_policy": {"max_upload_mb": 50, "blocked_extensions": [], "allowed_extensions": []}, "log_files": [], "recent_events": [], "log_path": "/tmp/security.log"}):
                get_dashboard_status.return_value = {
                    "captured_at": "2026-07-09T01:02:03+00:00",
                    "overall_status": "ok",
                    "host": {"cpu_percent": None, "memory_percent": None, "disk_percent": None, "source": "demo"},
                    "disk": {"percent": None, "level": "unknown"},
                    "files": {"file_count": 0, "total_bytes": 0},
                    "backup": {"latest_name": "", "status": "ok", "status_reason": "backup_recent"},
                    "containers": [],
                    "status_checks": [
                        {"key": "host", "label": "호스트 수집", "status": "ok", "detail": "정상 수집 중"},
                        {"key": "backup", "label": "백업", "status": "ok", "detail": "최근 백업 확인됨"},
                        {"key": "disk", "label": "디스크", "status": "ok", "detail": "디스크 사용률 0%"},
                        {"key": "files", "label": "파일함", "status": "ok", "detail": "0개, 0 bytes"},
                    ],
                    "warnings": [],
                }

                with TestClient(app) as client:
                    response = client.post("/admin/status", data={"password": "secret"})

            self.assertEqual(response.status_code, 200)
            self.assertIn("2026-07-09 01:02:03 UTC", response.text)
            self.assertIn("호스트 수집", response.text)
            self.assertIn("백업", response.text)
            self.assertIn("디스크", response.text)
            self.assertIn("파일함", response.text)
        finally:
            os.environ.pop("FILE_MANAGER_PASSWORD", None)


if __name__ == "__main__":
    unittest.main()
