import tempfile
import unittest
from pathlib import Path

from tests._test_support import prepare_service_import


class FakeExecutor:
    def __init__(self):
        self.restart_calls = []
        self.health_ok = True

    def diagnostics(self, service):
        return {"service": service, "container": {"status": "running", "health": "unhealthy"}, "logs": ["error"]}

    def restart(self, incident_id, approval_token, service):
        self.restart_calls.append((incident_id, approval_token, service))
        return {"service": service, "status": "running", "container": {"status": "running", "health": "healthy"}}

    def health(self, service):
        return self.health_ok


class HomeOpsTests(unittest.TestCase):
    def setUp(self):
        prepare_service_import("portal-web")
        from app.services.homeops import HomeOpsService

        self.tempdir = tempfile.TemporaryDirectory()
        self.executor = FakeExecutor()
        self.service = HomeOpsService(Path(self.tempdir.name) / "homeops.sqlite3", self.executor)

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

    def test_secret_is_masked_before_persistence(self):
        self.executor.diagnostics = lambda service: {"service": service, "container": {}, "logs": ["Authorization: Bearer secret-value"]}

        incident = self.service.create_diagnosis("crawler-worker")

        self.assertNotIn("secret-value", str(incident))


if __name__ == "__main__":
    unittest.main()
