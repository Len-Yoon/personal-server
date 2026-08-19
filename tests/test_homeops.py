import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import os
from datetime import datetime, timedelta, timezone

from tests._test_support import prepare_service_import


class FakeExecutor:
    def __init__(self):
        self.restart_calls = []
        self.health_ok = True
        self.health_results = None

    def diagnostics(self, service):
        return {"service": service, "container": {"status": "running", "health": "unhealthy"}, "logs": ["error"]}

    def restart(self, incident_id, approval_token, service):
        self.restart_calls.append((incident_id, approval_token, service))
        return {"service": service, "status": "running", "container": {"status": "running", "health": "healthy"}}

    def health(self, service):
        if self.health_results:
            return self.health_results.pop(0)
        return self.health_ok


class FakeNotifier:
    def __init__(self):
        self.events = []

    def send(self, event_type, details):
        self.events.append((event_type, details))


class HomeOpsTests(unittest.TestCase):
    def setUp(self):
        prepare_service_import("portal-web")
        from app.services.homeops import HomeOpsService

        self.tempdir = tempfile.TemporaryDirectory()
        self.executor = FakeExecutor()
        self.notifier = FakeNotifier()
        self.service = HomeOpsService(
            Path(self.tempdir.name) / "homeops.sqlite3",
            self.executor,
            notifier=self.notifier,
            verification_interval_seconds=0,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_unapproved_incident_cannot_restart(self):
        incident = self.service.create_diagnosis("crawler-worker")

        result = self.service.execute_approved_incident(incident["incident_id"])

        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.executor.restart_calls, [])

    def test_approval_is_single_use_and_health_is_verified(self):
        incident = self.service.create_diagnosis("crawler-worker")
        self.service.approve_incident(incident["incident_id"], "admin")

        result = self.service.execute_approved_incident(incident["incident_id"])
        repeat = self.service.execute_approved_incident(incident["incident_id"])

        self.assertEqual(result["status"], "verified")
        self.assertEqual(len(self.executor.restart_calls), 1)
        self.assertEqual(repeat["status"], "failed")

    def test_failed_health_is_recorded_without_retry(self):
        self.executor.health_ok = False
        incident = self.service.create_diagnosis("crawler-worker")
        self.service.approve_incident(incident["incident_id"], "admin")

        result = self.service.execute_approved_incident(incident["incident_id"])

        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.executor.restart_calls), 1)

    def test_recovery_waits_until_health_becomes_ready(self):
        self.executor.health_results = [False, True]
        incident = self.service.create_diagnosis("crawler-worker")
        self.service.approve_incident(incident["incident_id"], "admin")

        result = self.service.execute_approved_incident(incident["incident_id"])

        self.assertEqual(result["status"], "verified")

    def test_three_consecutive_unhealthy_diagnoses_restart_once(self):
        self.service.create_diagnosis("crawler-worker")
        self.service.create_diagnosis("crawler-worker")
        self.service.create_diagnosis("crawler-worker")

        self.assertEqual(len(self.executor.restart_calls), 1)

    def test_three_resource_pressure_diagnoses_with_fatal_log_restart_once(self):
        self.executor.diagnostics = lambda service: {
            "service": service,
            "container": {"status": "running", "health": "healthy", "cpu_percent": 86.0, "memory_percent": 45.0},
            "logs": ["FATAL worker cannot accept connections"],
        }

        self.service.create_diagnosis("crawler-worker")
        self.service.create_diagnosis("crawler-worker")
        self.service.create_diagnosis("crawler-worker")

        self.assertEqual(len(self.executor.restart_calls), 1)

    def test_high_resource_usage_without_error_signal_does_not_restart(self):
        self.executor.diagnostics = lambda service: {
            "service": service,
            "container": {"status": "running", "health": "healthy", "cpu_percent": 96.0, "memory_percent": 92.0},
            "logs": ["worker processing scheduled items"],
        }

        for _ in range(3):
            self.service.create_diagnosis("crawler-worker")

        self.assertEqual(self.executor.restart_calls, [])

    def test_scheduled_healthy_diagnosis_does_not_store_normal_history(self):
        self.executor.diagnostics = lambda service: {
            "service": service,
            "container": {"status": "running", "health": "healthy", "cpu_percent": 1.0, "memory_percent": 1.0},
            "logs": ["ready"],
        }

        result = self.service.create_diagnosis("crawler-worker", record_healthy=False)

        self.assertEqual(result["proposal"]["action"], "no_action")
        self.assertEqual(self.service.list_incidents(), [])

    def test_auto_restart_stops_after_two_policy_restarts_in_one_hour(self):
        completed_at = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        with self.service._connect() as conn:
            for index in range(2):
                conn.execute(
                    "INSERT INTO incidents VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"prior-{index}", "crawler-worker", "verified", completed_at, "{}", "{}", "homeops-policy", completed_at),
                )

        self.service.create_diagnosis("crawler-worker")
        self.service.create_diagnosis("crawler-worker")
        self.service.create_diagnosis("crawler-worker")

        self.assertEqual(self.executor.restart_calls, [])
        self.assertIn("auto_restart_limit_reached", [event_type for event_type, _ in self.notifier.events])

    def test_restart_lifecycle_sends_started_and_verified_notifications(self):
        incident = self.service.create_diagnosis("crawler-worker")
        self.service.approve_incident(incident["incident_id"], "admin")

        self.service.execute_approved_incident(incident["incident_id"])

        self.assertEqual(
            [event_type for event_type, _ in self.notifier.events],
            ["container_restart_started", "container_recovery_verified"],
        )

    def test_host_memory_alert_is_sent_once_after_three_consecutive_samples(self):
        self.service.observe_host_memory(90.0)
        self.service.observe_host_memory(91.2)
        self.service.observe_host_memory(92.8)
        self.service.observe_host_memory(93.1)
        self.service.observe_host_memory(72.0)

        self.assertEqual(
            [event_type for event_type, _ in self.notifier.events],
            ["host_memory_high", "host_memory_recovered"],
        )

    def test_secret_is_masked_before_persistence(self):
        self.executor.diagnostics = lambda service: {"service": service, "container": {}, "logs": ["Authorization: Bearer secret-value"]}

        incident = self.service.create_diagnosis("crawler-worker")

        self.assertNotIn("secret-value", str(incident))

    def test_admin_page_renders_homeops_and_execute_requires_same_origin(self):
        from fastapi.testclient import TestClient
        original = os.environ.get("ADMIN_STATUS_PASSWORD")
        os.environ["ADMIN_STATUS_PASSWORD"] = "secret"
        try:
            app = self._portal_app()
            with patch("app.routers.dashboard.get_homeops_service", return_value=self.service):
                with TestClient(app) as client:
                    page = client.post("/admin/status", data={"password": "secret"}, headers={"Origin": "http://testserver"})
                    blocked = client.post("/admin/homeops/one/execute", headers={"Origin": "https://evil.example", "X-HomeOps-Password": "secret"})
        finally:
            if original is None:
                os.environ.pop("ADMIN_STATUS_PASSWORD", None)
            else:
                os.environ["ADMIN_STATUS_PASSWORD"] = original

        self.assertEqual(page.status_code, 200)
        self.assertIn("HomeOps 운영 보조", page.text)
        self.assertIn('action="/admin/homeops/diagnose"', page.text)
        self.assertEqual(blocked.status_code, 403)

    def test_diagnosis_redirect_shows_a_result_notice(self):
        from fastapi.testclient import TestClient

        original = os.environ.get("ADMIN_STATUS_PASSWORD")
        os.environ["ADMIN_STATUS_PASSWORD"] = "secret"
        try:
            app = self._portal_app()
            with patch("app.routers.dashboard.get_homeops_service", return_value=self.service):
                with TestClient(app) as client:
                    client.post("/admin/status", data={"password": "secret"}, headers={"Origin": "http://testserver"})
                    response = client.post(
                        "/admin/homeops/diagnose",
                        data={"service": "crawler-worker"},
                        headers={"Origin": "http://testserver"},
                    )
        finally:
            if original is None:
                os.environ.pop("ADMIN_STATUS_PASSWORD", None)
            else:
                os.environ["ADMIN_STATUS_PASSWORD"] = original

        self.assertEqual(response.status_code, 200)
        self.assertIn("1개 서비스 진단을 기록했습니다.", response.text)

    def test_admin_page_formats_homeops_incident_timestamp_in_kst(self):
        from fastapi.testclient import TestClient

        self.service.create_diagnosis("crawler-worker")
        original = os.environ.get("ADMIN_STATUS_PASSWORD")
        os.environ["ADMIN_STATUS_PASSWORD"] = "secret"
        try:
            app = self._portal_app()
            with patch("app.routers.dashboard.get_homeops_service", return_value=self.service):
                with TestClient(app) as client:
                    page = client.post("/admin/status", data={"password": "secret"}, headers={"Origin": "http://testserver"})
        finally:
            if original is None:
                os.environ.pop("ADMIN_STATUS_PASSWORD", None)
            else:
                os.environ["ADMIN_STATUS_PASSWORD"] = original

        self.assertIn("KST", page.text)
        self.assertNotIn("+00:00", page.text)

    def _portal_app(self):
        prepare_service_import("portal-web")
        import app.main as main
        return main.app


if __name__ == "__main__":
    unittest.main()
