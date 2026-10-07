import concurrent.futures
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._test_support import prepare_service_import


class RestartIdempotencyTests(unittest.TestCase):
    def setUp(self):
        prepare_service_import("homeops-executor")
        from app import main
        from fastapi.testclient import TestClient
        self.main = main
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "restarts.sqlite3"
        env = patch.dict("os.environ", {"HOMEOPS_EXECUTOR_SHARED_SECRET": "fixture-secret", "HOMEOPS_IDEMPOTENCY_DB_PATH": str(self.path)}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(main.app)
        self.headers = {"X-HomeOps-Executor-Secret": "fixture-secret"}
        self.payload = {"incident_id": "fixture-incident", "approval_token": "secret-approval-fixture", "action": "restart_container", "service": "system-agent"}

    def test_durable_exact_replay_returns_original_result_without_second_restart(self):
        result = {"service": "system-agent", "status": "running", "container": {"health": "healthy"}}
        with patch.object(self.main.docker_ops, "restart_service", return_value=result) as operation:
            first = self.client.post("/v1/restarts", headers=self.headers, json=self.payload)
            # The API constructs a fresh store every request, exercising SQLite replay.
            second = self.client.post("/v1/restarts", headers=self.headers, json=self.payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        operation.assert_called_once_with("system-agent")
        self.assertNotIn(b"secret-approval-fixture", self.path.read_bytes())
        self.assertNotIn(b"fixture-secret", self.path.read_bytes())

    def test_same_id_cannot_change_service_or_approval(self):
        with patch.object(self.main.docker_ops, "restart_service", return_value={"status": "running"}) as operation:
            self.client.post("/v1/restarts", headers=self.headers, json=self.payload)
            for change in ({"service": "caddy"}, {"approval_token": "another-approval"}):
                response = self.client.post("/v1/restarts", headers=self.headers, json=dict(self.payload, **change))
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["detail"], "restart_request_conflict")
        operation.assert_called_once()

    def test_uncertain_operation_and_crash_claim_never_repeat(self):
        with patch.object(self.main.docker_ops, "restart_service", side_effect=OSError("private error")) as operation:
            first = self.client.post("/v1/restarts", headers=self.headers, json=self.payload)
            second = self.client.post("/v1/restarts", headers=self.headers, json=self.payload)
        self.assertEqual(first.status_code, 503)
        self.assertEqual(second.status_code, 409)
        self.assertNotIn("private", first.text)
        operation.assert_called_once()
        from app.services.idempotency import RestartStore, payload_hash
        self.assertIsNone(RestartStore().claim("crashed", payload_hash(dict(self.payload, incident_id="crashed"))))
        with patch.object(self.main.docker_ops, "restart_service") as operation:
            replay = self.client.post("/v1/restarts", headers=self.headers, json=dict(self.payload, incident_id="crashed"))
        self.assertEqual(replay.status_code, 409)
        operation.assert_not_called()

    def test_missing_approval_auth_and_unavailable_store_do_not_restart(self):
        with patch.object(self.main.docker_ops, "restart_service") as operation:
            self.assertEqual(self.client.post("/v1/restarts", json=self.payload).status_code, 403)
            self.assertEqual(self.client.post("/v1/restarts", headers=self.headers, json=dict(self.payload, approval_token=" ")).status_code, 422)
            with patch.dict("os.environ", {"HOMEOPS_IDEMPOTENCY_DB_PATH": str(self.path / "missing.sqlite3")}):
                self.assertEqual(self.client.post("/v1/restarts", headers=self.headers, json=self.payload).status_code, 503)
        operation.assert_not_called()

    def test_restart_all_requires_id_and_completed_replay_keeps_accepted_contract(self):
        headers = dict(self.headers, **{"X-HomeOps-Request-Id": "all-operation"})
        with patch.object(self.main.docker_ops, "restart_all_services", return_value=[{"status": "running"}]) as operation:
            self.assertEqual(self.client.post("/v1/restarts/all", headers=self.headers).status_code, 422)
            first = self.client.post("/v1/restarts/all", headers=headers)
            second = self.client.post("/v1/restarts/all", headers=headers)
        self.assertEqual(first.json(), {"status": "accepted"})
        self.assertEqual(second.json(), first.json())
        operation.assert_called_once()

    def test_restart_all_partial_failure_fails_closed_on_replay(self):
        headers = dict(self.headers, **{"X-HomeOps-Request-Id": "partial-operation"})
        with patch.object(self.main.docker_ops, "restart_all_services", return_value=[{"status": "failed"}]) as operation:
            self.assertEqual(self.client.post("/v1/restarts/all", headers=headers).status_code, 200)
            self.assertEqual(self.client.post("/v1/restarts/all", headers=headers).status_code, 409)
        operation.assert_called_once()

    def test_claim_is_atomic_across_connections_and_unknown_survives_ttl(self):
        from app.services.idempotency import ClaimError, RestartStore
        def claim():
            try:
                return RestartStore().claim("race", "digest")
            except ClaimError as exc:
                return exc.detail
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: claim(), range(4)))
        self.assertEqual(results.count(None), 1)
        self.assertEqual(results.count("restart_outcome_unknown"), 3)
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE restart_claims SET created_at=0")
        with self.assertRaises(ClaimError) as exc:
            RestartStore(ttl=1).claim("race", "digest")
        self.assertEqual(exc.exception.detail, "restart_outcome_unknown")
        with self.assertRaises(ClaimError) as exc:
            RestartStore(capacity=1).claim("new", "digest")
        self.assertEqual(exc.exception.status_code, 503)

    def test_completed_result_expires_to_tombstone_and_capacity_fails_closed(self):
        from app.services.idempotency import ClaimError, RestartStore
        store = RestartStore(ttl=1, capacity=1)
        store.claim("completed", "digest")
        store.complete("completed", {"status": "running"})
        self.assertFalse(store.ready())
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE restart_claims SET created_at=0")
        with self.assertRaises(ClaimError) as exc:
            store.claim("completed", "digest")
        self.assertEqual(exc.exception.detail, "restart_request_expired")
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT state, result FROM restart_claims").fetchone(), ("expired", None))
        with self.assertRaises(ClaimError) as exc:
            store.claim("new", "digest")
        self.assertEqual(exc.exception.detail, "restart_state_capacity")
        with self.assertRaises(ClaimError):
            store.claim("completed", "digest")

    def test_result_persistence_failure_after_restart_never_restarts_again(self):
        from app.services.idempotency import ClaimError, RestartStore
        with patch.object(self.main.docker_ops, "restart_service", return_value={"status": "running"}) as operation, patch.object(RestartStore, "complete", side_effect=ClaimError("restart_state_unavailable", 503)):
            first = self.client.post("/v1/restarts", headers=self.headers, json=self.payload)
        with patch.object(self.main.docker_ops, "restart_service") as repeated:
            replay = self.client.post("/v1/restarts", headers=self.headers, json=self.payload)
        self.assertEqual(first.status_code, 503)
        self.assertEqual(replay.status_code, 409)
        operation.assert_called_once()
        repeated.assert_not_called()

    def test_ready_is_local_secret_free_and_does_not_create_state(self):
        with patch.object(self.main.docker_ops, "_docker_client", side_effect=AssertionError("no network")):
            response = self.client.get("/ready")
            self.assertEqual(response.status_code, 200)
            self.assertFalse(self.path.exists())
            with patch.dict("os.environ", {"HOMEOPS_EXECUTOR_SHARED_SECRET": ""}):
                self.assertEqual(self.client.get("/health").status_code, 200)
                unavailable = self.client.get("/ready")
                self.assertEqual(unavailable.status_code, 503)
            with patch.dict("os.environ", {"HOMEOPS_RUNTIME_STATE_PATH": str(Path(self.tmp.name) / "absent.state")}):
                self.assertEqual(self.client.get("/ready").status_code, 503)
        self.assertNotIn("fixture-secret", response.text)
