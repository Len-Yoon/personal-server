import importlib
import os
import unittest
from unittest.mock import patch


class PortalDashboardTests(unittest.TestCase):
    def reload_system_status(self, demo_mode: str = ""):
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
        from app.services.global_search import _normalize_result_url

        result = _normalize_result_url("youtube", {"title": "memo", "url": "/videos/1"})

        self.assertEqual(result["url"], "http://memo.lenserver.com/videos/1")


if __name__ == "__main__":
    unittest.main()
