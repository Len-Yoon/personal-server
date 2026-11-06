import copy
from contextlib import contextmanager
import unittest
from unittest.mock import patch

from tests._test_support import prepare_service_import


class HomeOpsHATests(unittest.TestCase):
    def setUp(self):
        prepare_service_import("homeops-executor")
        from app import main
        from app.services import idempotency
        from fastapi.testclient import TestClient
        self.main, self.store_module = main, idempotency
        self.rows = {}
        self.depth = 0
        self.transaction_depths = []
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    @contextmanager
    def state_transaction(self, service, key, default):
        self.transaction_depths.append(self.depth)
        payload = copy.deepcopy(self.rows.get(key, default))
        yield payload
        self.rows[key] = copy.deepcopy(payload)

    def node_id(self):
        return "fixture-node"

    @contextmanager
    def worker_scope(self, service):
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1

    def test_shared_inflight_claim_survives_new_store_and_never_retries(self):
        with patch.object(self.store_module, "runtime", return_value=self):
            self.assertIsNone(self.store_module.RestartStore().claim("operation", "digest"))
            with self.assertRaises(self.store_module.ClaimError) as error:
                self.store_module.RestartStore().claim("operation", "digest")
            self.assertEqual(error.exception.detail, "restart_outcome_unknown")
            self.store_module.RestartStore().complete("operation", {"status": "running"})
            self.assertEqual(self.store_module.RestartStore().claim("operation", "digest"), {"status": "running"})

    def test_missing_or_other_node_never_restarts(self):
        headers = {"X-HomeOps-Executor-Secret": "fixture"}
        payload = {"incident_id": "fixture", "approval_token": "fixture", "action": "restart_container", "service": "system-agent"}
        with patch.dict("os.environ", {"HOMEOPS_EXECUTOR_SHARED_SECRET": "fixture"}), patch.object(self.main, "runtime", return_value=self), patch.object(self.main.docker_ops, "restart_service") as restart:
            for target in (None, "other-node"):
                response = self.client.post("/v1/restarts", headers=headers, json=dict(payload, target_node=target))
                self.assertEqual(response.status_code, 403)
            restart.assert_not_called()

    def test_worker_permit_covers_claim_effect_and_completion(self):
        def effect(service):
            self.assertGreater(self.depth, 0)
            self.assertEqual(self.rows["restart_claims"]["fixture"]["state"], "inflight")
            return {"status": "running"}

        payload = self.main.RestartRequest(incident_id="fixture", approval_token="fixture", action="restart_container", service="system-agent", target_node="fixture-node")
        with patch.dict("os.environ", {"HOMEOPS_EXECUTOR_SHARED_SECRET": "fixture"}), patch.object(self.main, "runtime", return_value=self), patch.object(self.store_module, "runtime", return_value=self), patch.object(self.main.docker_ops, "allowed_services", return_value={"system-agent"}), patch.object(self.main.docker_ops, "restart_service", side_effect=effect):
            result = self.main.restart(payload, "fixture")
        self.assertEqual(result, {"status": "running"})
        self.assertEqual(self.transaction_depths, [1, 1])
        self.assertEqual(self.rows["restart_claims"]["fixture"]["state"], "completed")
