import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from tests._test_support import prepare_service_import


SCOPE = frozenset({"system-agent", "caddy", "homeops-executor"})


def healthy(names=SCOPE):
    return [{"service": name, "container": {"status": "running", "health": "healthy"}, "logs": []}
            for name in sorted(names)]


class Response:
    def __init__(self, header=None, payload=None):
        self.headers = {} if header is None else {"X-HomeOps-Managed-Services": header}
        self.payload = healthy() if payload is None else payload
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return json.dumps(self.payload).encode()


class ManagedScopeTests(unittest.TestCase):
    def setUp(self):
        prepare_service_import("portal-web")
        from app.services import homeops
        self.module = homeops
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = homeops.ExecutorClient()
        self.client.secret = "fixture-secret"
        self.service = homeops.HomeOpsService(Path(self.tmp.name)/"homeops.db", self.client,
                                            verification_interval_seconds=0)

    def test_authenticated_scope_excludes_k3s_services_from_summary_and_polling(self):
        with patch("app.services.homeops.urlopen", return_value=Response(",".join(sorted(SCOPE)))):
            summary = self.service.diagnose_all()
            polled = self.service._poll_all_diagnostics()
            restart_summary = self.service._restart_summary(polled)
        self.assertEqual(summary["unhealthy"], [])
        self.assertEqual(set(summary["healthy"]), SCOPE)
        self.assertEqual(restart_summary["failed"], [])
        self.assertEqual(set(restart_summary["recovered"]), SCOPE)
        self.assertTrue(self.service._all_services_healthy(polled))

    def test_invalid_header_discards_previous_scope(self):
        for header in ("", "caddy,", "caddy,caddy", "caddy,unlisted", "caddy;system-agent"):
            with self.subTest(header=header):
                with patch("app.services.homeops.urlopen", return_value=Response(",".join(sorted(SCOPE)))):
                    self.client.all_diagnostics()
                with patch("app.services.homeops.urlopen", return_value=Response(header)):
                    summary = self.service.diagnose_all()
                self.assertEqual(summary["healthy"], [])
                self.assertEqual({item["service"] for item in summary["unhealthy"]}, self.module.ALLOWED_SERVICES)

    def test_old_executor_uses_full_allowlist_and_missing_items_are_unhealthy(self):
        with patch("app.services.homeops.urlopen", return_value=Response()):
            summary = self.service.diagnose_all()
        self.assertEqual({item["service"] for item in summary["unhealthy"]},
                         self.module.ALLOWED_SERVICES - SCOPE)
        self.assertFalse(self.service._all_services_healthy(healthy()))

    def test_missing_duplicate_or_extra_items_cannot_be_success(self):
        for payload in (healthy({"caddy"}), healthy()+healthy({"caddy"}), healthy()+healthy({"portal-web"})):
            with self.subTest(payload=payload):
                with patch("app.services.homeops.urlopen", return_value=Response(",".join(sorted(SCOPE)), payload)):
                    diagnostics = self.client.all_diagnostics()
                    summary = self.service._restart_summary(diagnostics)
                    diagnosis = self.service._diagnosis_summary(diagnostics)
                self.assertTrue(summary["failed"])
                self.assertTrue(diagnosis["unhealthy"])
                self.assertFalse(self.service._all_services_healthy(diagnostics))

    def test_diagnostics_scope_is_bound_to_response_when_another_request_changes_property(self):
        with patch("app.services.homeops.urlopen", return_value=Response(",".join(sorted(SCOPE)))):
            diagnostics = self.service._mask(self.client.all_diagnostics())
        with patch("app.services.homeops.urlopen", return_value=Response("caddy", healthy({"caddy"}))):
            self.client.all_diagnostics()
        self.assertEqual(self.service._restart_summary(diagnostics)["failed"], [])
        self.assertEqual(set(self.service._restart_summary(diagnostics)["recovered"]), SCOPE)

    def test_repeated_scope_header_fields_are_rejected(self):
        response = Response()
        response.headers = Message()
        response.headers.add_header("X-HomeOps-Managed-Services", "caddy")
        response.headers.add_header("X-HomeOps-Managed-Services", "system-agent")
        with patch("app.services.homeops.urlopen", return_value=response):
            summary = self.service.diagnose_all()
        self.assertEqual(summary["healthy"], [])
        self.assertEqual(len(summary["unhealthy"]), 7)

    def test_pending_restart_verifies_fresh_authenticated_scope(self):
        responses = [Response(",".join(sorted(SCOPE))),
                     Response(payload={"status": "accepted"}),
                     Response("caddy", healthy({"caddy"}))]
        with patch("app.services.homeops.urlopen", side_effect=responses), \
             patch.object(self.service, "_pending_restart_is_due", return_value=True):
            self.service.diagnose_all()
            pending = self.service.restart_all()
            final = self.service.latest_summary()
        self.assertEqual(pending["kind"], "restart_pending")
        self.assertEqual(final["recovered"], ["caddy"])
        self.assertEqual(final["failed"], [])

    def test_missing_health_is_not_a_confirmed_healthy_observation(self):
        payload = [{"service": "caddy", "container": {"status": "running"}, "logs": []}]
        with patch("app.services.homeops.urlopen", return_value=Response("caddy", payload)):
            summary = self.service.diagnose_all()
        self.assertEqual(summary["healthy"], [])
        self.assertEqual({item["service"] for item in summary["unhealthy"]}, {"caddy"})
