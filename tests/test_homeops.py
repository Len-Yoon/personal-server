import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import os
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError

from tests._test_support import prepare_service_import


class FakeExecutor:
    def __init__(self):
        self.restart_calls = []
        self.health_ok = True
        self.health_results = None
        self.all_diagnostics_results = []
        self.restart_all_result = []
        self.restart_all_error = None
        self.restart_all_calls = 0
        self.all_diagnostics_calls = 0

    def diagnostics(self, service):
        return {"service": service, "container": {"status": "running", "health": "unhealthy"}, "logs": ["error"]}

    def restart(self, incident_id, approval_token, service):
        self.restart_calls.append((incident_id, approval_token, service))
        return {"service": service, "status": "running", "container": {"status": "running", "health": "healthy"}}

    def health(self, service):
        if self.health_results:
            return self.health_results.pop(0)
        return self.health_ok

    def all_diagnostics(self):
        self.all_diagnostics_calls += 1
        if self.all_diagnostics_results:
            result = self.all_diagnostics_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return []

    def restart_all(self):
        self.restart_all_calls += 1
        if self.restart_all_error:
            raise self.restart_all_error
        return self.restart_all_result


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

    def test_executor_client_uses_whole_fleet_endpoints(self):
        from app.services.homeops import ExecutorClient

        class RecordingClient(ExecutorClient):
            def __init__(self):
                self.requests = []

            def _request(self, path, payload=None, method=None):
                self.requests.append((path, payload, method))
                return {"path": path}

        client = RecordingClient()

        diagnostics = client.all_diagnostics()
        restarts = client.restart_all()

        self.assertEqual(diagnostics, {"path": "/v1/diagnostics"})
        self.assertEqual(restarts, {"path": "/v1/restarts/all"})
        self.assertEqual(
            client.requests,
            [("/v1/diagnostics", None, None), ("/v1/restarts/all", None, "POST")],
        )

    def test_diagnose_all_groups_healthy_and_normalized_unhealthy_services(self):
        self.executor.all_diagnostics_results = [[
            {"service": "crawler-worker", "container": {"status": "running", "health": "healthy"}, "logs": []},
            {"service": "portal-web", "container": {"status": "running", "health": "none"}, "logs": []},
            {"service": "caddy", "container": {"status": "running", "health": "unhealthy"}, "logs": []},
            {"service": "book-memo", "container": {"status": "running", "health": "starting"}, "logs": []},
            {"service": "youtube-memo", "container": {"status": "exited", "health": "none"}, "logs": []},
        ]]

        summary = self.service.diagnose_all()

        self.assertEqual(summary["healthy"], ["crawler-worker", "portal-web"])
        self.assertEqual(
            summary["unhealthy"],
            [
                {"service": "caddy", "reason": "healthcheck 비정상"},
                {"service": "book-memo", "reason": "healthcheck 비정상"},
                {"service": "youtube-memo", "reason": "중지됨"},
            ],
        )

    def test_diagnose_all_normalizes_executor_failure_for_every_service(self):
        self.executor.all_diagnostics_results = [HTTPError(
            "http://executor/v1/diagnostics", 403, "forbidden", {}, None
        )]

        summary = self.service.diagnose_all()

        self.assertEqual(summary["healthy"], [])
        self.assertEqual(
            summary["unhealthy"],
            [
                {"service": service, "reason": "실행기 인증 설정 확인 필요"}
                for service in sorted({"portal-web", "system-agent", "crawler-worker", "youtube-memo", "book-memo", "caddy", "homeops-executor"})
            ],
        )

    def test_executor_client_uses_admin_password_as_internal_secret_fallback(self):
        from app.services.homeops import ExecutorClient

        with patch.dict(
            os.environ,
            {"HOMEOPS_EXECUTOR_SHARED_SECRET": "", "ADMIN_STATUS_PASSWORD": "admin-secret"},
            clear=False,
        ):
            self.assertEqual(ExecutorClient().secret, "admin-secret")

    def test_latest_summary_replaces_the_previous_singleton_record(self):
        first = [{"service": "crawler-worker", "container": {"status": "running", "health": "healthy"}, "logs": []}]
        second = [{"service": "caddy", "container": {"status": "exited", "health": "none"}, "logs": []}]
        self.executor.all_diagnostics_results = [first, second]

        self.service.diagnose_all()
        expected = self.service.diagnose_all()

        from app.services.homeops import HomeOpsService
        reloaded = HomeOpsService(self.service.db_path, self.executor, verification_interval_seconds=0)
        self.assertEqual(reloaded.latest_summary(), expected)
        with self.service._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM latest_homeops_summary").fetchone()[0], 1)

    def test_restart_all_records_recovered_and_failed_services(self):
        services = sorted({"portal-web", "system-agent", "crawler-worker", "youtube-memo", "book-memo", "caddy", "homeops-executor"})
        self.executor.restart_all_result = [
            {
                "service": service,
                "status": "exited" if service == "caddy" else "running",
                "container": {
                    "status": "exited" if service == "caddy" else "running",
                    "health": "none" if service == "caddy" else "healthy",
                },
            }
            for service in services
        ]

        summary = self.service.restart_all()

        self.assertEqual(summary["recovered"], [service for service in services if service != "caddy"])
        self.assertEqual(summary["failed"], [{"service": "caddy", "reason": "중지됨"}])
        self.assertEqual(self.service.latest_summary(), summary)

    def test_restart_all_records_accepted_request_before_portal_is_restarted(self):
        self.executor.restart_all_result = {"status": "accepted"}

        summary = self.service.restart_all()

        self.assertEqual(summary["kind"], "restart_pending")
        self.assertEqual(summary["healthy"], [])
        self.assertEqual(summary["recovered"], [])
        self.assertEqual(summary["failed"], [])
        self.assertEqual(self.service.latest_summary(), summary)

    def test_restart_all_recovers_from_executor_connection_close(self):
        healthy = [
            {"service": service, "container": {"status": "running", "health": "healthy"}, "logs": []}
            for service in sorted({"portal-web", "system-agent", "crawler-worker", "youtube-memo", "book-memo", "caddy", "homeops-executor"})
        ]
        self.executor.restart_all_error = ConnectionResetError("connection reset")
        self.executor.all_diagnostics_results = [OSError("still starting"), healthy]

        summary = self.service.restart_all()

        self.assertEqual(summary["failed"], [])
        self.assertEqual(summary["recovered"], [item["service"] for item in healthy])
        self.assertEqual(self.executor.all_diagnostics_calls, 2)

    def test_restart_all_recovers_from_wrapped_connection_reset(self):
        healthy = [
            {"service": service, "container": {"status": "running", "health": "healthy"}, "logs": []}
            for service in sorted({"portal-web", "system-agent", "crawler-worker", "youtube-memo", "book-memo", "caddy", "homeops-executor"})
        ]
        self.executor.restart_all_error = URLError(ConnectionResetError("connection reset"))
        self.executor.all_diagnostics_results = [healthy]

        summary = self.service.restart_all()

        self.assertEqual(summary["failed"], [])
        self.assertEqual(self.executor.all_diagnostics_calls, 1)

    def test_restart_all_stops_polling_and_records_executor_response_failure(self):
        self.executor.restart_all_error = ConnectionResetError("connection reset")
        self.executor.all_diagnostics_results = [OSError("still starting")] * 5

        summary = self.service.restart_all()

        self.assertEqual(self.executor.all_diagnostics_calls, 5)
        self.assertEqual(
            summary["failed"],
            [
                {"service": service, "reason": "실행기 연결 실패"}
                for service in sorted({"portal-web", "system-agent", "crawler-worker", "youtube-memo", "book-memo", "caddy", "homeops-executor"})
            ],
        )

    def test_restart_all_does_not_poll_after_http_error(self):
        self.executor.restart_all_error = HTTPError(
            "http://executor/v1/restarts/all", 500, "server error", {}, None
        )

        summary = self.service.restart_all()

        self.assertEqual(self.executor.all_diagnostics_calls, 0)
        self.assertEqual(len(summary["failed"]), 7)
        self.assertEqual({item["reason"] for item in summary["failed"]}, {"실행기 내부 오류"})

    def test_restart_all_does_not_poll_after_generic_os_error(self):
        self.executor.restart_all_error = OSError("network unreachable")

        summary = self.service.restart_all()

        self.assertEqual(self.executor.all_diagnostics_calls, 0)
        self.assertEqual(len(summary["failed"]), 7)

    def test_restart_all_polling_waits_until_every_service_is_healthy(self):
        services = sorted({"portal-web", "system-agent", "crawler-worker", "youtube-memo", "book-memo", "caddy", "homeops-executor"})
        starting = [
            {
                "service": service,
                "container": {
                    "status": "running",
                    "health": "starting" if service == "homeops-executor" else "healthy",
                },
                "logs": [],
            }
            for service in services
        ]
        healthy = [
            {"service": service, "container": {"status": "running", "health": "healthy"}, "logs": []}
            for service in services
        ]
        self.executor.restart_all_error = ConnectionResetError("connection reset")
        self.executor.all_diagnostics_results = [starting, healthy]

        summary = self.service.restart_all()

        self.assertEqual(self.executor.all_diagnostics_calls, 2)
        self.assertEqual(summary["failed"], [])
        self.assertEqual(summary["recovered"], services)

    def test_restart_all_polling_records_missing_target_as_failed(self):
        partial = [
            {"service": "crawler-worker", "container": {"status": "running", "health": "healthy"}, "logs": []}
        ]
        self.executor.restart_all_error = ConnectionResetError("connection reset")
        self.executor.all_diagnostics_results = [partial] * 5

        summary = self.service.restart_all()

        self.assertEqual(self.executor.all_diagnostics_calls, 5)
        self.assertEqual(summary["recovered"], ["crawler-worker"])
        self.assertEqual(
            {item["service"] for item in summary["failed"]},
            {"portal-web", "system-agent", "youtube-memo", "book-memo", "caddy", "homeops-executor"},
        )

    def test_restart_all_polling_records_http_error_without_further_recovery(self):
        healthy = [
            {"service": service, "container": {"status": "running", "health": "healthy"}, "logs": []}
            for service in sorted({"portal-web", "system-agent", "crawler-worker", "youtube-memo", "book-memo", "caddy", "homeops-executor"})
        ]
        self.executor.restart_all_error = ConnectionResetError("connection reset")
        self.executor.all_diagnostics_results = [
            HTTPError("http://executor/v1/diagnostics", 403, "forbidden", {}, None),
            healthy,
        ]

        summary = self.service.restart_all()

        self.assertEqual(self.executor.all_diagnostics_calls, 1)
        self.assertEqual(len(summary["failed"]), 7)

    def test_diagnose_all_route_renders_latest_compact_summary_after_redirect(self):
        from fastapi.testclient import TestClient

        self.executor.all_diagnostics_results = [[
            {"service": "crawler-worker", "container": {"status": "running", "health": "healthy"}, "logs": []},
            {"service": "caddy", "container": {"status": "running", "health": "unhealthy"}, "logs": []},
        ]]
        original = os.environ.get("ADMIN_STATUS_PASSWORD")
        os.environ["ADMIN_STATUS_PASSWORD"] = "secret"
        try:
            app = self._portal_app()
            with patch("app.routers.admin.get_homeops_service", return_value=self.service):
                with TestClient(app) as client:
                    client.post("/admin/status", data={"password": "secret"}, headers={"Origin": "http://testserver"})
                    response = client.post(
                        "/admin/homeops/diagnose",
                        headers={"Origin": "http://testserver"},
                        follow_redirects=False,
                    )
                    page = client.get(response.headers["location"])
        finally:
            if original is None:
                os.environ.pop("ADMIN_STATUS_PASSWORD", None)
            else:
                os.environ["ADMIN_STATUS_PASSWORD"] = original

        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.executor.all_diagnostics_calls, 1)
        self.assertEqual(page.status_code, 200)
        self.assertIn("<strong>정상:</strong> crawler-worker", page.text)
        self.assertIn("<strong>비정상:</strong> caddy", page.text)
        self.assertIn("healthcheck 비정상", page.text)
        self.assertIn('class="homeops-summary-separator">—</span>', page.text)
        self.assertNotIn("최근 조치 이력", page.text)

    def test_restart_all_route_renders_recovery_summary_after_redirect(self):
        from fastapi.testclient import TestClient

        services = sorted({"portal-web", "system-agent", "crawler-worker", "youtube-memo", "book-memo", "caddy", "homeops-executor"})
        self.executor.restart_all_result = [
            {
                "service": service,
                "container": {
                    "status": "exited" if service == "caddy" else "running",
                    "health": "healthy",
                },
                "logs": [],
            }
            for service in services
        ]
        original = os.environ.get("ADMIN_STATUS_PASSWORD")
        os.environ["ADMIN_STATUS_PASSWORD"] = "secret"
        try:
            app = self._portal_app()
            with patch("app.routers.admin.get_homeops_service", return_value=self.service):
                with TestClient(app) as client:
                    client.post("/admin/status", data={"password": "secret"}, headers={"Origin": "http://testserver"})
                    response = client.post(
                        "/admin/homeops/restart-all",
                        headers={"Origin": "http://testserver"},
                        follow_redirects=False,
                    )
                    redirect_location = response.headers.get("location")
                    page = client.get(redirect_location) if redirect_location else response
        finally:
            if original is None:
                os.environ.pop("ADMIN_STATUS_PASSWORD", None)
            else:
                os.environ["ADMIN_STATUS_PASSWORD"] = original

        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.executor.restart_all_calls, 1)
        self.assertIn("복구됨:", page.text)
        self.assertIn("<strong>복구 확인 실패:</strong> caddy", page.text)
        self.assertIn("중지됨", page.text)

    def test_restart_all_route_renders_pending_summary_after_redirect(self):
        from fastapi.testclient import TestClient

        self.executor.restart_all_result = {"status": "accepted"}
        original = os.environ.get("ADMIN_STATUS_PASSWORD")
        os.environ["ADMIN_STATUS_PASSWORD"] = "secret"
        try:
            app = self._portal_app()
            with patch("app.routers.admin.get_homeops_service", return_value=self.service):
                with TestClient(app) as client:
                    client.post("/admin/status", data={"password": "secret"}, headers={"Origin": "http://testserver"})
                    response = client.post(
                        "/admin/homeops/restart-all",
                        headers={"Origin": "http://testserver"},
                        follow_redirects=False,
                    )
                    page = client.get(response.headers["location"])
        finally:
            if original is None:
                os.environ.pop("ADMIN_STATUS_PASSWORD", None)
            else:
                os.environ["ADMIN_STATUS_PASSWORD"] = original

        self.assertEqual(response.status_code, 303)
        self.assertIn("전체 재시작 요청이 접수되었습니다.", page.text)

    def test_homeops_global_actions_require_authentication_and_same_origin(self):
        from fastapi.testclient import TestClient

        original = os.environ.get("ADMIN_STATUS_PASSWORD")
        os.environ["ADMIN_STATUS_PASSWORD"] = "secret"
        try:
            app = self._portal_app()
            with patch("app.routers.admin.get_homeops_service", return_value=self.service):
                with TestClient(app) as client:
                    unauthenticated = client.post(
                        "/admin/homeops/diagnose",
                        headers={"Origin": "http://testserver"},
                    )
                    client.post("/admin/status", data={"password": "secret"}, headers={"Origin": "http://testserver"})
                    cross_origin = client.post(
                        "/admin/homeops/restart-all",
                        headers={"Origin": "https://evil.example"},
                    )
        finally:
            if original is None:
                os.environ.pop("ADMIN_STATUS_PASSWORD", None)
            else:
                os.environ["ADMIN_STATUS_PASSWORD"] = original

        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(cross_origin.status_code, 403)
        self.assertEqual(self.executor.all_diagnostics_calls, 0)
        self.assertEqual(self.executor.restart_all_calls, 0)

    def _portal_app(self):
        prepare_service_import("portal-web")
        import app.main as main
        return main.app


if __name__ == "__main__":
    unittest.main()
