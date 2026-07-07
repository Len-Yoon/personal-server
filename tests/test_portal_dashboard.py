import importlib
import os
import unittest
from unittest.mock import patch

from tests._test_support import prepare_service_import


class PortalDashboardTests(unittest.TestCase):
    def reload_system_status(self, demo_mode: str = ""):
        prepare_service_import("portal-web")
        os.environ["DEMO_MODE"] = demo_mode
        import app.services.system_status as system_status

        return importlib.reload(system_status)

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
        from shared.host_urls import portal_home_url

        self.assertEqual(portal_home_url("127.0.0.1"), "http://127.0.0.1:8000/")

    def test_portal_home_url_uses_public_address_outside_local(self):
        prepare_service_import("youtube-memo")
        from shared.host_urls import portal_home_url

        self.assertEqual(portal_home_url("memo.len.pe.kr"), "https://portal.len.pe.kr/")

    def test_file_and_admin_entry_urls_follow_host_mode(self):
        prepare_service_import("portal-web")
        from shared.host_urls import admin_entry_url, file_entry_url

        self.assertEqual(file_entry_url("127.0.0.1"), "http://127.0.0.1:8000/files")
        self.assertEqual(admin_entry_url("127.0.0.1"), "http://127.0.0.1:8000/admin/status")
        self.assertEqual(file_entry_url("portal.len.pe.kr"), "https://file.len.pe.kr/")
        self.assertEqual(admin_entry_url("portal.len.pe.kr"), "https://admin.len.pe.kr/")

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


if __name__ == "__main__":
    unittest.main()
