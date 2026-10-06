import unittest
from unittest.mock import patch
from tests._test_support import prepare_service_import
from tests.homeops_executor.test_docker_ops import FakeDockerClient


class ManagedScopeApiTests(unittest.TestCase):
    def setUp(self):
        prepare_service_import("homeops-executor")

    def test_authenticated_diagnostics_header_matches_single_runtime_scope_snapshot(self):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.services import docker_ops
        managed = frozenset({"system-agent", "caddy", "homeops-executor"})
        # Later ownership reads may change: response membership must use the
        # original snapshot; collect_diagnostics still independently enforces ownership.
        snapshots = iter([managed, frozenset({"caddy"})])
        with patch.dict("os.environ", {"HOMEOPS_EXECUTOR_SHARED_SECRET": "fixture-secret"}), \
             patch.object(docker_ops, "allowed_services", side_effect=lambda: next(snapshots, managed)), \
             patch.object(docker_ops, "_docker_client", return_value=FakeDockerClient()):
            response = TestClient(app).get("/v1/diagnostics", headers={"X-HomeOps-Executor-Secret": "fixture-secret"})
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json(), list)
        self.assertEqual(response.headers.get("X-HomeOps-Managed-Services"), "caddy,homeops-executor,system-agent")
        self.assertEqual({item["service"] for item in response.json()}, managed)

    def test_untrusted_caller_does_not_receive_runtime_scope(self):
        from fastapi.testclient import TestClient
        from app.main import app
        with patch.dict("os.environ", {"HOMEOPS_EXECUTOR_SHARED_SECRET": "fixture-secret"}):
            response = TestClient(app).get("/v1/diagnostics")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("X-HomeOps-Managed-Services", response.headers)
