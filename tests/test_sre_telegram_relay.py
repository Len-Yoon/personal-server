import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "sre-telegram-relay"))

from app.main import (  # noqa: E402
    ConfigMapOffsetStore,
    MemoryOffsetStore,
    RelayService,
    build_status_summary,
    create_http_handler,
)


class FakeK8s:
    def list_nodes(self):
        return [{"status": {"conditions": [{"type": "Ready", "status": "True"}]}}]

    def list_pods(self, namespace):
        if namespace == "monitoring":
            return [
                {"status": {"conditions": [{"type": "Ready", "status": "True"}]}},
                {"status": {"conditions": [{"type": "Ready", "status": "False"}]}},
            ]
        return [{"status": {"conditions": [{"type": "Ready", "status": "True"}]}}]

    def list_deployments(self, namespace):
        return [{"spec": {"replicas": 1}, "status": {"availableReplicas": 1}}]

    def list_pvcs(self, namespace):
        return [{"status": {"phase": "Bound"}}]


class FailingK8s(FakeK8s):
    def list_nodes(self):
        raise OSError("cluster unavailable")


class FakePrometheus:
    def active_targets(self):
        return [{"health": "up"}, {"health": "down"}]


class FailingPrometheus(FakePrometheus):
    def active_targets(self):
        raise OSError("prometheus unavailable")


class FakeConfigMapK8s:
    def __init__(self):
        self.data = {}

    def get_config_map(self, namespace, name):
        return {"data": dict(self.data)}

    def patch_config_map(self, namespace, name, data):
        self.data.update(data)


class RelayServiceTest(unittest.TestCase):
    def test_allowed_status_command_returns_redacted_summary(self):
        relay = RelayService(allowed_chat_id="123", k8s_client=FakeK8s(), prometheus_client=FakePrometheus())

        reply = relay.handle_update({"message": {"chat": {"id": 123}, "text": "/상태"}})

        self.assertTrue(reply.startswith("[K3s 상태]"))
        self.assertIn("Node Ready: 1/1", reply)
        self.assertIn("Prometheus UP: 1/2", reply)
        self.assertNotIn("123", reply)

    def test_other_chat_never_receives_status(self):
        relay = RelayService(allowed_chat_id="123", k8s_client=FakeK8s(), prometheus_client=FakePrometheus())

        self.assertIsNone(relay.handle_update({"message": {"chat": {"id": 999}, "text": "/상태"}}))

    def test_alert_webhook_rejects_wrong_bearer_token(self):
        relay = RelayService(alertmanager_auth_token="expected", k8s_client=FakeK8s(), prometheus_client=FakePrometheus())

        self.assertEqual(relay.handle_alert({"status": "firing", "alerts": []}, "Bearer wrong")[0], 401)

    def test_unsupported_command_does_not_return_status(self):
        relay = RelayService(allowed_chat_id="123", k8s_client=FakeK8s(), prometheus_client=FakePrometheus())

        self.assertIsNone(relay.handle_update({"message": {"chat": {"id": 123}, "text": "/삭제"}}))

    def test_status_summary_reports_read_failures_without_assuming_healthy(self):
        reply = build_status_summary(FailingK8s(), FailingPrometheus())

        self.assertIn("K3s API 조회 실패", reply)
        self.assertIn("Prometheus 조회 실패", reply)
        self.assertNotIn("Node Ready:", reply)
        self.assertNotIn("Prometheus UP:", reply)

    def test_firing_alert_is_formatted_without_bearer_token(self):
        relay = RelayService(
            alertmanager_auth_token="test-token-not-for-replies",
            k8s_client=FakeK8s(),
            prometheus_client=FakePrometheus(),
        )

        status, reply = relay.handle_alert(
            {"status": "firing", "alerts": [{"labels": {"alertname": "DeploymentUnavailable"}}]},
            "Bearer test-token-not-for-replies",
        )

        self.assertEqual(status, 200)
        self.assertIn("[K3s 경고 발생]", reply)
        self.assertIn("DeploymentUnavailable", reply)
        self.assertNotIn("test-token-not-for-replies", reply)

    def test_resolved_alert_is_formatted(self):
        relay = RelayService(alertmanager_auth_token="expected", k8s_client=FakeK8s(), prometheus_client=FakePrometheus())

        status, reply = relay.handle_alert(
            {"status": "resolved", "alerts": [{"labels": {"alertname": "PVCNotBound"}}]},
            "Bearer expected",
        )

        self.assertEqual(status, 200)
        self.assertIn("[K3s 경고 복구]", reply)
        self.assertIn("PVCNotBound", reply)

    def test_processed_update_is_not_replied_to_again_after_restart(self):
        offsets = MemoryOffsetStore()
        update = {"update_id": 41, "message": {"chat": {"id": 123}, "text": "/상태"}}
        first_relay = RelayService(
            allowed_chat_id="123", k8s_client=FakeK8s(), prometheus_client=FakePrometheus(), offset_store=offsets
        )

        self.assertIsNotNone(first_relay.handle_update(update))

        restarted_relay = RelayService(
            allowed_chat_id="123", k8s_client=FakeK8s(), prometheus_client=FakePrometheus(), offset_store=offsets
        )
        self.assertIsNone(restarted_relay.handle_update(update))

    def test_health_endpoint_returns_ok(self):
        relay = RelayService(allowed_chat_id="123", k8s_client=FakeK8s(), prometheus_client=FakePrometheus())
        server = ThreadingHTTPServer(("127.0.0.1", 0), create_http_handler(relay))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/healthz", timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b"ok\n")
        finally:
            server.shutdown()
            server.server_close()

    def test_configmap_offset_store_persists_the_next_update_id(self):
        k8s = FakeConfigMapK8s()
        store = ConfigMapOffsetStore(k8s, namespace="monitoring", name="relay-state")

        store.save(42)

        restarted_store = ConfigMapOffsetStore(k8s, namespace="monitoring", name="relay-state")
        self.assertEqual(restarted_store.load(), 42)


if __name__ == "__main__":
    unittest.main()
