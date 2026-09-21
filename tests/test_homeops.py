import tempfile
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import patch
import os
import threading
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

    def test_observation_counter_resets_after_healthy_sample(self):
        self.service.create_diagnosis("crawler-worker")
        self.service.create_diagnosis("crawler-worker")
        self.executor.diagnostics = lambda service: {
            "service": service, "container": {"status": "running", "health": "healthy"}, "logs": []
        }
        self.service.create_diagnosis("crawler-worker", record_healthy=False)
        with self.service._connect() as conn:
            row = conn.execute("SELECT consecutive_unhealthy, last_status FROM service_observations WHERE service=?", ("crawler-worker",)).fetchone()
        self.assertEqual(row, (0, "healthy"))

    def test_auto_reservation_is_atomic_and_second_concurrent_scan_cannot_reserve(self):
        barrier = threading.Barrier(2)
        original_diagnostics = self.executor.diagnostics
        results, errors = [], []

        def scan():
            try:
                results.append(self.service.create_diagnosis("crawler-worker"))
            except Exception as exc:
                errors.append(exc)

        for _ in range(2):
            self.service.create_diagnosis("crawler-worker")
        def diagnostics(service):
            barrier.wait(timeout=2)
            return original_diagnostics(service)
        self.executor.diagnostics = diagnostics
        workers = [threading.Thread(target=scan) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(timeout=3)
        self.assertFalse(errors)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(len(self.executor.restart_calls), 1)

    def test_scheduler_scan_allows_missing_origin_only_on_exact_path_with_valid_secret(self):
        from fastapi.testclient import TestClient
        original = os.environ.get("HOMEOPS_SCHEDULER_SECRET")
        os.environ["HOMEOPS_SCHEDULER_SECRET"] = "fixture-secret"
        try:
            app = self._portal_app()
            with patch("app.routers.admin.get_homeops_service", return_value=self.service), patch(
                "app.routers.admin.get_dashboard_status", return_value={"host": {}}
            ):
                with TestClient(app) as client:
                    response = client.post("/internal/homeops/scan", headers={"X-HomeOps-Scheduler-Secret": "fixture-secret"})
                    wrong_path = client.post("/internal/homeops/scan/extra", headers={"X-HomeOps-Scheduler-Secret": "fixture-secret"})
                    invalid = client.post("/internal/homeops/scan", headers={"X-HomeOps-Scheduler-Secret": "wrong"})
        finally:
            if original is None: os.environ.pop("HOMEOPS_SCHEDULER_SECRET", None)
            else: os.environ["HOMEOPS_SCHEDULER_SECRET"] = original
        self.assertEqual(response.status_code, 200)
        self.assertEqual(wrong_path.status_code, 403)
        self.assertEqual(invalid.status_code, 403)

    def test_scheduler_origin_and_method_variants_are_rejected(self):
        from fastapi.testclient import TestClient
        original = os.environ.get("HOMEOPS_SCHEDULER_SECRET")
        os.environ["HOMEOPS_SCHEDULER_SECRET"] = "fixture-secret"
        try:
            app = self._portal_app()
            with patch("app.routers.admin.get_homeops_service", return_value=self.service), patch("app.routers.admin.get_dashboard_status", return_value={"host": {}}):
                with TestClient(app) as client:
                    evil = client.post("/internal/homeops/scan", headers={"Origin": "https://evil.example", "X-HomeOps-Scheduler-Secret": "fixture-secret"})
                    empty = client.post("/internal/homeops/scan", headers={"Origin": "", "X-HomeOps-Scheduler-Secret": "fixture-secret"})
                    wrong_method = client.get("/internal/homeops/scan", headers={"X-HomeOps-Scheduler-Secret": "fixture-secret"})
                    same_origin_wrong_secret = client.post("/internal/homeops/scan", headers={"Origin": "http://testserver", "X-HomeOps-Scheduler-Secret": "wrong"})
        finally:
            if original is None: os.environ.pop("HOMEOPS_SCHEDULER_SECRET", None)
            else: os.environ["HOMEOPS_SCHEDULER_SECRET"] = original
        self.assertEqual([response.status_code for response in (evil, empty, wrong_method, same_origin_wrong_secret)], [403, 403, 405, 403])
        from app.routers.admin import homeops_scheduler_secret_valid
        self.assertFalse(homeops_scheduler_secret_valid(b"fixture-secret", "fixture-secret"))

    def test_scheduler_scan_rejects_missing_or_malformed_credentials_without_calling_handler(self):
        from fastapi.testclient import TestClient

        original = os.environ.get("HOMEOPS_SCHEDULER_SECRET")
        os.environ["HOMEOPS_SCHEDULER_SECRET"] = "fixture-secret"
        try:
            app = self._portal_app()
            with patch.object(self.service, "create_diagnosis", wraps=self.service.create_diagnosis) as handler, patch(
                "app.routers.admin.get_homeops_service", return_value=self.service
            ), patch("app.routers.admin.get_dashboard_status", return_value={"host": {}}):
                with TestClient(app) as client:
                    missing = client.post("/internal/homeops/scan")
                    null_origin = client.post(
                        "/internal/homeops/scan",
                        headers={"Origin": "null", "X-HomeOps-Scheduler-Secret": "fixture-secret"},
                    )
                    unsafe_put = client.put(
                        "/internal/homeops/scan",
                        headers={"X-HomeOps-Scheduler-Secret": "fixture-secret"},
                    )
                    non_ascii = client.post(
                        "/internal/homeops/scan",
                        headers={"X-HomeOps-Scheduler-Secret": "\\xff"},
                    )
            os.environ["HOMEOPS_SCHEDULER_SECRET"] = ""
            with TestClient(app) as client:
                unconfigured = client.post(
                    "/internal/homeops/scan",
                    headers={"X-HomeOps-Scheduler-Secret": "fixture-secret"},
                )
        finally:
            if original is None:
                os.environ.pop("HOMEOPS_SCHEDULER_SECRET", None)
            else:
                os.environ["HOMEOPS_SCHEDULER_SECRET"] = original

        self.assertEqual(
            [response.status_code for response in (missing, null_origin, unsafe_put, non_ascii, unconfigured)],
            [403, 403, 403, 403, 403],
        )
        self.assertEqual(handler.call_count, 0)

    def test_existing_database_keeps_approval_token_and_adds_observation_schema(self):
        from app.services.homeops import HomeOpsService
        with self.service._connect() as conn:
            conn.execute("INSERT INTO incidents VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ("legacy", "crawler-worker", "approved", self.service._now(), "{}", "{}", "admin", None))
            conn.execute("INSERT INTO approval_tokens VALUES (?, ?, ?, ?)", ("legacy", "legacy-hash", self.service._now(), None))
        reloaded = HomeOpsService(self.service.db_path, self.executor, verification_interval_seconds=0)
        with reloaded._connect() as conn:
            self.assertEqual(conn.execute("SELECT token_hash FROM approval_tokens WHERE incident_id='legacy'").fetchone()[0], "legacy-hash")
            self.assertIsNotNone(conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='service_observations'").fetchone())

    def test_expired_approved_incident_does_not_block_new_manual_approval(self):
        first = self.service.create_diagnosis("crawler-worker")
        self.service.approve_incident(first["incident_id"], "admin")
        with self.service._connect() as conn:
            conn.execute("UPDATE approval_tokens SET expires_at=? WHERE incident_id=?", ("2000-01-01T00:00:00+00:00", first["incident_id"]))
        second = self.service.create_diagnosis("crawler-worker")
        result = self.service.approve_incident(second["incident_id"], "admin")
        self.assertEqual(result["status"], "approved")

    def test_approval_expiring_between_read_and_consumption_never_restarts(self):
        incident = self.service.create_diagnosis("crawler-worker")
        self.service.approve_incident(incident["incident_id"], "admin")
        before_expiry = "2026-09-21T00:00:00+00:00"
        after_expiry = "2026-09-21T00:00:01+00:00"
        with self.service._connect() as conn:
            conn.execute(
                "UPDATE approval_tokens SET expires_at=? WHERE incident_id=?",
                (before_expiry, incident["incident_id"]),
            )

        with patch.object(self.service, "_now", side_effect=[before_expiry, after_expiry]):
            result = self.service.execute_approved_incident(incident["incident_id"])

        self.assertEqual(result, {"status": "failed", "reason": "approval_not_available"})
        self.assertEqual(self.executor.restart_calls, [])
        with self.service._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM incidents WHERE incident_id=?", (incident["incident_id"],)).fetchone()[0],
                "approved",
            )
            self.assertIsNone(
                conn.execute("SELECT consumed_at FROM approval_tokens WHERE incident_id=?", (incident["incident_id"],)).fetchone()[0]
            )

    def test_auto_limit_notification_runs_after_transaction_commits(self):
        with self.service._connect() as conn:
            conn.execute("CREATE TABLE notifier_writes (value TEXT)")
            old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            for index in range(2):
                conn.execute("INSERT INTO incidents VALUES (?, ?, 'verified', ?, '{}', '{}', 'homeops-policy', ?)", (f"prior-{index}", "crawler-worker", old, old))

        class WritingNotifier:
            def send(_, event_type, details):
                with self.service._connect() as conn:
                    conn.execute("INSERT INTO notifier_writes VALUES (?)", (event_type,))

        self.service.notifier = WritingNotifier()
        self.service.create_diagnosis("crawler-worker")
        self.service.create_diagnosis("crawler-worker")
        self.service.create_diagnosis("crawler-worker")
        with self.service._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifier_writes").fetchone()[0], 1)

    def test_same_approval_concurrent_execute_across_service_instances_consumes_once(self):
        from app.services.homeops import HomeOpsService

        incident = self.service.create_diagnosis("crawler-worker")
        self.service.approve_incident(incident["incident_id"], "admin")
        second = HomeOpsService(self.service.db_path, self.executor, verification_interval_seconds=0)

        lock_holder = sqlite3.connect(self.service.db_path, timeout=2)
        lock_holder.execute("BEGIN IMMEDIATE")
        entered = [threading.Event(), threading.Event()]
        originals = [self.service._connect, second._connect]

        def traced_connect(original_connect, entered_write):
            def connect():
                conn = original_connect()
                conn.set_trace_callback(
                    lambda statement: entered_write.set() if statement.strip().upper().startswith("BEGIN") else None
                )
                return conn

            return connect

        self.service._connect = traced_connect(originals[0], entered[0])
        second._connect = traced_connect(originals[1], entered[1])
        results, errors = [], []

        def execute(service):
            try:
                results.append(service.execute_approved_incident(incident["incident_id"]))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=execute, args=(service,)) for service in (self.service, second)]
        try:
            for thread in threads:
                thread.start()
            self.assertTrue(entered[0].wait(timeout=2))
            self.assertTrue(entered[1].wait(timeout=2))
        finally:
            lock_holder.rollback()
            lock_holder.close()
            for thread in threads:
                thread.join(timeout=3)

        self.assertFalse(errors)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(self.executor.restart_calls), 1)
        self.assertEqual(sorted(result["status"] for result in results), ["failed", "verified"])

    def test_executor_response_loss_keeps_consumed_executing_and_blocks_retry(self):
        calls = []

        def response_lost(*args):
            calls.append(args)
            raise OSError("response lost")

        self.executor.restart = response_lost
        incident = self.service.create_diagnosis("crawler-worker")
        self.service.approve_incident(incident["incident_id"], "admin")
        with self.assertRaises(OSError):
            self.service.execute_approved_incident(incident["incident_id"])
        reloaded = type(self.service)(self.service.db_path, self.executor, verification_interval_seconds=0)
        self.assertEqual(reloaded.execute_approved_incident(incident["incident_id"])["status"], "failed")
        for _ in range(3):
            reloaded.create_diagnosis("crawler-worker")
        self.assertEqual(len(calls), 1)
        with reloaded._connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM incidents WHERE incident_id=?", (incident["incident_id"],)).fetchone()[0], "executing")
            self.assertIsNotNone(conn.execute("SELECT consumed_at FROM approval_tokens WHERE incident_id=?", (incident["incident_id"],)).fetchone()[0])

    def test_expired_approved_incident_allows_auto_reservation_after_three_samples(self):
        incident = self.service.create_diagnosis("crawler-worker")
        self.service.approve_incident(incident["incident_id"], "admin")
        with self.service._connect() as conn:
            conn.execute(
                "UPDATE approval_tokens SET expires_at=? WHERE incident_id=?",
                ("2000-01-01T00:00:00+00:00", incident["incident_id"]),
            )

        for _ in range(3):
            self.service.create_diagnosis("crawler-worker")

        self.assertEqual(len(self.executor.restart_calls), 1)

    def test_unhealthy_healthy_unhealthy_healthy_unhealthy_never_restarts(self):
        for index in range(5):
            if index in (1, 3):
                self.executor.diagnostics = lambda service: {"service": service, "container": {"status": "running", "health": "healthy"}, "logs": []}
            else:
                self.executor.diagnostics = lambda service: {"service": service, "container": {"status": "running", "health": "unhealthy"}, "logs": ["error"]}
            self.service.create_diagnosis("crawler-worker", record_healthy=False)
            if index == 2:
                from app.services.homeops import HomeOpsService
                self.service = HomeOpsService(self.service.db_path, self.executor, verification_interval_seconds=0)
        self.assertEqual(self.executor.restart_calls, [])

    def test_legacy_only_database_migrates_without_changing_existing_rows(self):
        from app.services.homeops import HomeOpsService

        legacy_path = Path(self.tempdir.name) / "legacy.sqlite3"
        conn = sqlite3.connect(legacy_path)
        conn.execute("CREATE TABLE incidents (incident_id TEXT PRIMARY KEY, service TEXT, status TEXT, created_at TEXT, diagnostics TEXT, proposal TEXT, approved_by TEXT, completed_at TEXT)")
        conn.execute("CREATE TABLE approval_tokens (incident_id TEXT PRIMARY KEY, token_hash TEXT, expires_at TEXT, consumed_at TEXT)")
        conn.execute("INSERT INTO incidents VALUES ('legacy','crawler-worker','failed','2020-01-01T00:00:00+00:00','{}','{}','admin','2020-01-01T00:00:00+00:00')")
        conn.execute("INSERT INTO approval_tokens VALUES ('legacy','hash','2020-01-01T00:00:00+00:00','2020-01-01T00:00:00+00:00')")
        conn.commit(); conn.close()
        reloaded = HomeOpsService(legacy_path, self.executor, verification_interval_seconds=0)
        with sqlite3.connect(legacy_path) as conn:
            self.assertEqual(
                conn.execute("SELECT * FROM incidents").fetchall(),
                [("legacy", "crawler-worker", "failed", "2020-01-01T00:00:00+00:00", "{}", "{}", "admin", "2020-01-01T00:00:00+00:00")],
            )
            self.assertEqual(
                conn.execute("SELECT * FROM approval_tokens").fetchall(),
                [("legacy", "hash", "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00")],
            )
            self.assertIsNotNone(
                conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='service_observations'").fetchone()
            )

        healthy = {"service": "crawler-worker", "container": {"status": "running", "health": "healthy"}, "logs": []}
        reloaded.executor.diagnostics = lambda service: healthy | {"service": service}
        reloaded.create_diagnosis("crawler-worker", record_healthy=False)
        reloaded.create_diagnosis("book-memo", record_healthy=False)
        reloaded = HomeOpsService(legacy_path, self.executor, verification_interval_seconds=0)
        with reloaded._connect() as conn:
            rows = conn.execute(
                "SELECT service, consecutive_unhealthy, last_status FROM service_observations ORDER BY service"
            ).fetchall()
        self.assertEqual(rows, [("book-memo", 0, "healthy"), ("crawler-worker", 0, "healthy")])

    def test_auto_restart_respects_cooldown_then_reserves_once_after_expiry(self):
        completed_at = self.service._now()
        with self.service._connect() as conn:
            conn.execute(
                "INSERT INTO incidents VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("cooldown", "crawler-worker", "verified", completed_at, "{}", "{}", "homeops-policy", completed_at),
            )

        for _ in range(3):
            self.service.create_diagnosis("crawler-worker")
        self.assertEqual(self.executor.restart_calls, [])
        with self.service._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT consecutive_unhealthy FROM service_observations WHERE service='crawler-worker'").fetchone()[0],
                3,
            )
            elapsed = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
            conn.execute("UPDATE incidents SET completed_at=? WHERE incident_id='cooldown'", (elapsed,))

        self.service.create_diagnosis("crawler-worker")

        self.assertEqual(len(self.executor.restart_calls), 1)
        self.assertEqual(self.service._consecutive_unhealthy("crawler-worker"), 0)

    def test_failed_auto_health_waits_for_three_new_samples_and_respects_hourly_limit(self):
        self.executor.health_ok = False
        for _ in range(3):
            self.service.create_diagnosis("crawler-worker")
        self.assertEqual(len(self.executor.restart_calls), 1)

        for _ in range(2):
            self.service.create_diagnosis("crawler-worker")
        self.assertEqual(len(self.executor.restart_calls), 1)

        self.service.create_diagnosis("crawler-worker")
        self.assertEqual(len(self.executor.restart_calls), 2)

        for _ in range(3):
            self.service.create_diagnosis("crawler-worker")
        self.assertEqual(len(self.executor.restart_calls), 2)

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

    def test_executor_client_rejects_admin_password_when_shared_secret_is_missing(self):
        from app.services.homeops import ExecutorClient

        with patch.dict(
            os.environ,
            {"HOMEOPS_EXECUTOR_SHARED_SECRET": "", "ADMIN_STATUS_PASSWORD": "admin-secret"},
            clear=False,
        ):
            client = ExecutorClient()
            with patch("app.services.homeops.urlopen") as urlopen:
                with self.assertRaisesRegex(OSError, "homeops_executor_shared_secret_not_configured"):
                    client.all_diagnostics()

        self.assertEqual(client.secret, "")
        urlopen.assert_not_called()

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

    def test_operation_history_links_unhealthy_diagnosis_restart_and_recovery(self):
        unhealthy = [
            {
                "service": service,
                "container": {
                    "status": "exited" if service == "caddy" else "running",
                    "health": "none" if service == "caddy" else "healthy",
                },
                "logs": [],
            }
            for service in sorted({"portal-web", "system-agent", "crawler-worker", "youtube-memo", "book-memo", "caddy", "homeops-executor"})
        ]
        healthy = [
            {"service": service, "container": {"status": "running", "health": "healthy"}, "logs": []}
            for service in sorted({"portal-web", "system-agent", "crawler-worker", "youtube-memo", "book-memo", "caddy", "homeops-executor"})
        ]
        self.executor.all_diagnostics_results = [unhealthy]

        diagnosis = self.service.diagnose_all()
        operation_id = diagnosis["operation_id"]
        self.executor.restart_all_result = {"status": "accepted"}
        pending = self.service.restart_all(operation_id)

        self.assertEqual(pending["kind"], "restart_pending")
        self.assertEqual(pending["operation_id"], operation_id)
        self.assertEqual(self.service.operation_history(limit=1)[0]["status"], "restart_pending")

        with self.service._connect() as conn:
            conn.execute(
                "UPDATE homeops_operation_runs SET restart_requested_at=? WHERE operation_id=?",
                ((datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(), operation_id),
            )
        self.executor.all_diagnostics_results = [healthy]

        completed = self.service.latest_summary()
        history = self.service.operation_history(limit=1)[0]

        self.assertEqual(completed["kind"], "restart")
        self.assertEqual(history["operation_id"], operation_id)
        self.assertEqual(history["status"], "recovered")
        self.assertEqual(history["initial_result"]["unhealthy"], [{"service": "caddy", "reason": "중지됨"}])
        self.assertEqual(history["final_result"]["failed"], [])
        self.assertEqual(
            [event["event_type"] for event in history["events"]],
            ["diagnosis_completed", "restart_requested", "verification_completed"],
        )

    def test_pending_restart_retries_incomplete_verification_before_recording_failure(self):
        services = sorted({"portal-web", "system-agent", "crawler-worker", "youtube-memo", "book-memo", "caddy", "homeops-executor"})
        unhealthy = [
            {"service": service, "container": {"status": "exited" if service == "caddy" else "running", "health": "healthy"}, "logs": []}
            for service in services
        ]
        still_starting = [
            {"service": service, "container": {"status": "exited" if service == "caddy" else "running", "health": "healthy"}, "logs": []}
            for service in services
        ]
        self.executor.all_diagnostics_results = [unhealthy]
        operation_id = self.service.diagnose_all()["operation_id"]
        self.executor.restart_all_result = {"status": "accepted"}
        self.service.restart_all(operation_id)
        with self.service._connect() as conn:
            conn.execute(
                "UPDATE homeops_operation_runs SET restart_requested_at=? WHERE operation_id=?",
                ((datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(), operation_id),
            )
        self.executor.all_diagnostics_results = [still_starting]

        self.service.latest_summary()

        history = self.service.operation_history(limit=1)[0]
        self.assertEqual(history["status"], "restart_pending")
        self.assertEqual(history["events"][-1]["event_type"], "verification_pending")

    def test_pending_restart_verifies_every_due_operation(self):
        services = sorted({"portal-web", "system-agent", "crawler-worker", "youtube-memo", "book-memo", "caddy", "homeops-executor"})
        unhealthy = [
            {"service": service, "container": {"status": "exited" if service == "caddy" else "running", "health": "healthy"}, "logs": []}
            for service in services
        ]
        healthy = [{"service": service, "container": {"status": "running", "health": "healthy"}, "logs": []} for service in services]
        operation_ids = []
        for _ in range(2):
            self.executor.all_diagnostics_results = [unhealthy]
            operation_id = self.service.diagnose_all()["operation_id"]
            self.executor.restart_all_result = {"status": "accepted"}
            self.service.restart_all(operation_id)
            operation_ids.append(operation_id)
        with self.service._connect() as conn:
            conn.executemany(
                "UPDATE homeops_operation_runs SET restart_requested_at=? WHERE operation_id=?",
                [((datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(), operation_id) for operation_id in operation_ids],
            )
        self.executor.all_diagnostics_results = [healthy, healthy]

        self.service.latest_summary()

        history = {item["operation_id"]: item for item in self.service.operation_history(limit=5)}
        self.assertEqual(history[operation_ids[0]]["status"], "recovered")
        self.assertEqual(history[operation_ids[1]]["status"], "recovered")

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

    def test_admin_status_renders_operation_history_for_action_required_diagnosis(self):
        from fastapi.testclient import TestClient

        self.executor.all_diagnostics_results = [
            [
                {
                    "service": service,
                    "container": {"status": "exited" if service == "caddy" else "running", "health": "healthy"},
                    "logs": [],
                }
                for service in sorted({"portal-web", "system-agent", "crawler-worker", "youtube-memo", "book-memo", "caddy", "homeops-executor"})
            ]
        ]
        self.service.diagnose_all()
        original = os.environ.get("ADMIN_STATUS_PASSWORD")
        os.environ["ADMIN_STATUS_PASSWORD"] = "secret"
        try:
            app = self._portal_app()
            with patch("app.routers.admin.get_homeops_service", return_value=self.service):
                with TestClient(app) as client:
                    client.post("/admin/status", data={"password": "secret"}, headers={"Origin": "http://testserver"})
                    page = client.get("/admin/status")
        finally:
            if original is None:
                os.environ.pop("ADMIN_STATUS_PASSWORD", None)
            else:
                os.environ["ADMIN_STATUS_PASSWORD"] = original

        self.assertIn("최근 운영 이력", page.text)
        self.assertIn("점검 완료 · 조치 필요", page.text)
        self.assertIn("원인: caddy", page.text)

    def test_admin_status_displays_operation_history_timestamp_in_kst(self):
        from fastapi.testclient import TestClient

        self.executor.all_diagnostics_results = [[]]
        operation_id = self.service.diagnose_all()["operation_id"]
        with self.service._connect() as conn:
            conn.execute(
                "UPDATE homeops_operation_runs SET created_at=? WHERE operation_id=?",
                ("2026-08-24T07:21:18.895511+00:00", operation_id),
            )

        original = os.environ.get("ADMIN_STATUS_PASSWORD")
        os.environ["ADMIN_STATUS_PASSWORD"] = "secret"
        try:
            app = self._portal_app()
            with patch("app.routers.admin.get_homeops_service", return_value=self.service):
                with TestClient(app) as client:
                    client.post("/admin/status", data={"password": "secret"}, headers={"Origin": "http://testserver"})
                    page = client.get("/admin/status")
        finally:
            if original is None:
                os.environ.pop("ADMIN_STATUS_PASSWORD", None)
            else:
                os.environ["ADMIN_STATUS_PASSWORD"] = original

        self.assertIn("<span>2026-08-24 16:21</span>", page.text)
        self.assertNotIn("2026-08-24 16:21:18 KST", page.text)
        self.assertNotIn("2026-08-24T07:21:18.895511+00:00", page.text)

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
