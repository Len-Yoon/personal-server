import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "sre-telegram-relay"))

from app.main import (  # noqa: E402
    ConfigMapAlertStateStore,
    ConfigMapOffsetStore,
    MemoryAlertStateStore,
    MemoryOffsetStore,
    PROMETHEUS_API_URL,
    PrometheusClient,
    RELAY_NAMESPACE,
    RELAY_STATE_CONFIGMAP,
    RelayService,
    TelegramClient,
    TelegramPollingError,
    build_status_summary,
    handle_http_request,
    run_polling,
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


class FailingOffsetStore:
    def load(self):
        raise OSError("ConfigMap unavailable")

    def save(self, offset):
        raise OSError("ConfigMap unavailable")


class WriteFailingOffsetStore:
    def load(self):
        return None

    def save(self, offset):
        raise OSError("ConfigMap write unavailable")


class FakePollingTelegram:
    def __init__(self, updates, send_result=True):
        self._updates = updates
        self._send_result = send_result
        self.sent_messages = []

    def get_updates(self, offset):
        updates, self._updates = self._updates, []
        return updates

    def send_message(self, chat_id, text):
        self.sent_messages.append((chat_id, text))
        return self._send_result


class FailingPollingTelegram:
    def __init__(self):
        self.calls = 0

    def get_updates(self, offset):
        self.calls += 1
        raise TelegramPollingError("getUpdates failed")

    def send_message(self, chat_id, text):
        raise AssertionError("failed poll must not send a message")


class SecretLeakingPollingTelegram(FailingPollingTelegram):
    def get_updates(self, offset):
        self.calls += 1
        raise TelegramPollingError("token=super-secret-token")


class ControlledTelegramClient(TelegramClient):
    def __init__(self, response):
        super().__init__("test-token")
        self._response = response

    def _post(self, method, payload):
        return self._response


class RelayServiceTest(unittest.TestCase):
    def test_default_prometheus_client_uses_verified_monitoring_service(self):
        with patch("app.main.urlopen") as opener:
            response = opener.return_value.__enter__.return_value
            response.read.return_value = b'{"status":"success","data":{"activeTargets":[]}}'

            PrometheusClient().active_targets()

        request = opener.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://personal-server-monitoring-prometheus.monitoring.svc:9090/api/v1/targets",
        )
        self.assertEqual(
            PROMETHEUS_API_URL,
            "http://personal-server-monitoring-prometheus.monitoring.svc:9090",
        )

    def test_allowed_status_command_returns_redacted_summary(self):
        relay = RelayService(allowed_chat_id="123", k8s_client=FakeK8s(), prometheus_client=FakePrometheus())

        reply = relay.handle_update({"message": {"chat": {"id": 123}, "text": "/상태"}})

        self.assertTrue(reply.startswith("[K3s 상태]"))
        self.assertIn("Node Ready: 1/1", reply)
        self.assertIn("Prometheus UP: 1/2", reply)
        self.assertNotIn("123", reply)

    def test_single_digit_chat_id_does_not_corrupt_status_counts(self):
        relay = RelayService(allowed_chat_id="1", k8s_client=FakeK8s(), prometheus_client=FakePrometheus())

        reply = relay.handle_update({"message": {"chat": {"id": 1}, "text": "/상태"}})

        self.assertIn("Node Ready: 1/1", reply)
        self.assertIn("Prometheus UP: 1/2", reply)

    def test_long_numeric_chat_id_is_never_returned_in_status(self):
        chat_id = "12345678901234567890"
        relay = RelayService(allowed_chat_id=chat_id, k8s_client=FakeK8s(), prometheus_client=FakePrometheus())

        reply = relay.handle_update({"message": {"chat": {"id": int(chat_id)}, "text": "/상태"}})

        self.assertNotIn(chat_id, reply)
        self.assertIn("Node Ready: 1/1", reply)

    def test_other_chat_never_receives_status(self):
        relay = RelayService(allowed_chat_id="123", k8s_client=FakeK8s(), prometheus_client=FakePrometheus())

        self.assertIsNone(relay.handle_update({"message": {"chat": {"id": 999}, "text": "/상태"}}))

    def test_alert_webhook_rejects_wrong_bearer_token(self):
        relay = RelayService(alertmanager_auth_token="expected", k8s_client=FakeK8s(), prometheus_client=FakePrometheus())

        self.assertEqual(relay.handle_alert({"status": "firing", "alerts": []}, "Bearer wrong")[0], 401)

    def test_unsupported_command_does_not_return_status_and_is_consumed(self):
        offsets = MemoryOffsetStore()
        relay = RelayService(
            allowed_chat_id="123", k8s_client=FakeK8s(), prometheus_client=FakePrometheus(), offset_store=offsets
        )
        update = {"update_id": 8, "message": {"chat": {"id": 123}, "text": "/삭제"}}
        telegram = FakePollingTelegram([update])

        self.assertIsNone(relay.handle_update(update))
        run_polling(relay, telegram, "123", max_cycles=1, sleep_fn=lambda _: None)

        self.assertEqual(offsets.load(), 9)
        self.assertEqual(telegram.sent_messages, [])

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
        self.assertIn("[장애 감지]", reply)
        self.assertIn("문제: 서비스 실행 수 부족", reply)
        self.assertNotIn("test-token-not-for-replies", reply)

    def test_firing_portal_alert_explains_problem_impact_and_target_in_korean(self):
        relay = RelayService(
            alertmanager_auth_token="expected",
            k8s_client=FakeK8s(),
            prometheus_client=FakePrometheus(),
        )

        status, reply = relay.handle_alert(
            {
                "status": "firing",
                "alerts": [
                    {
                        "labels": {
                            "alertname": "PortalUnavailable",
                            "namespace": "personal-server",
                            "deployment": "portal-web",
                        },
                    }
                ],
            },
            "Bearer expected",
        )

        self.assertEqual(status, 200)
        self.assertIn("[장애 감지]", reply)
        self.assertIn("문제: Portal 접속 불가", reply)
        self.assertIn("영향: 웹사이트가 열리지 않을 수 있음", reply)
        self.assertIn("대상: personal-server / portal-web", reply)
        self.assertIn("상태: 자동 복구를 확인 중입니다.", reply)
        self.assertNotIn("PortalUnavailable", reply)

    def test_resolved_alert_is_formatted(self):
        relay = RelayService(alertmanager_auth_token="expected", k8s_client=FakeK8s(), prometheus_client=FakePrometheus())

        status, reply = relay.handle_alert(
            {"status": "resolved", "alerts": [{"labels": {"alertname": "PVCNotBound"}}]},
            "Bearer expected",
        )

        self.assertEqual(status, 200)
        self.assertIn("[복구 확인]", reply)
        self.assertIn("문제: 데이터 저장소 연결 실패", reply)
        self.assertIn("영향: 저장된 데이터에 접근하지 못할 수 있음", reply)
        self.assertIn("대상: 확인 대상 없음", reply)
        self.assertIn("상태: 정상으로 돌아왔습니다.", reply)

    def test_prometheus_target_alert_uses_job_and_instance_as_the_target(self):
        relay = RelayService(
            alertmanager_auth_token="expected",
            k8s_client=FakeK8s(),
            prometheus_client=FakePrometheus(),
        )

        status, reply = relay.handle_alert(
            {
                "status": "firing",
                "alerts": [
                    {
                        "labels": {
                            "alertname": "PrometheusTargetDown",
                            "job": "kubelet",
                            "instance": "172.19.121.162:10250",
                        },
                    }
                ],
            },
            "Bearer expected",
        )

        self.assertEqual(status, 200)
        self.assertIn("문제: 상태 수집 대상 응답 없음", reply)
        self.assertIn("대상: kubelet / 172.19.121.162:10250", reply)

    def test_duplicate_firing_alert_with_same_fingerprint_is_suppressed(self):
        state = MemoryAlertStateStore()
        deliveries = []
        relay = RelayService(
            alertmanager_auth_token="expected",
            k8s_client=FakeK8s(),
            prometheus_client=FakePrometheus(),
            alert_state_store=state,
            alert_callback=lambda message: deliveries.append(message) or True,
        )
        payload = {"status": "firing", "alerts": [{"fingerprint": "fp-1", "labels": {"alertname": "PodRestartIncrease"}}]}

        first_status, _ = relay.handle_alert(payload, "Bearer expected")
        second_status, second_reply = relay.handle_alert(payload, "Bearer expected")

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(second_reply, "duplicate suppressed")
        self.assertEqual(len(deliveries), 1)

    def test_resolved_transition_after_firing_is_delivered_once(self):
        state = MemoryAlertStateStore()
        deliveries = []
        relay = RelayService(
            alertmanager_auth_token="expected",
            k8s_client=FakeK8s(),
            prometheus_client=FakePrometheus(),
            alert_state_store=state,
            alert_callback=lambda message: deliveries.append(message) or True,
        )
        firing = {"status": "firing", "alerts": [{"fingerprint": "fp-2", "labels": {"alertname": "PVCNotBound"}}]}
        resolved = {"status": "resolved", "alerts": [{"fingerprint": "fp-2", "labels": {"alertname": "PVCNotBound"}}]}

        relay.handle_alert(firing, "Bearer expected")
        resolved_status, resolved_reply = relay.handle_alert(resolved, "Bearer expected")
        duplicate_status, duplicate_reply = relay.handle_alert(resolved, "Bearer expected")

        self.assertEqual(resolved_status, 200)
        self.assertIn("[복구 확인]", resolved_reply)
        self.assertEqual(duplicate_status, 200)
        self.assertEqual(duplicate_reply, "duplicate suppressed")
        self.assertEqual(len(deliveries), 2)

    def test_firing_alert_state_suppression_survives_relay_restart(self):
        state = MemoryAlertStateStore()
        payload = {"status": "firing", "alerts": [{"fingerprint": "fp-3", "labels": {"alertname": "DeploymentUnavailable"}}]}
        first_relay = RelayService(
            alertmanager_auth_token="expected",
            k8s_client=FakeK8s(),
            prometheus_client=FakePrometheus(),
            alert_state_store=state,
        )
        restarted_relay = RelayService(
            alertmanager_auth_token="expected",
            k8s_client=FakeK8s(),
            prometheus_client=FakePrometheus(),
            alert_state_store=state,
        )

        self.assertEqual(first_relay.handle_alert(payload, "Bearer expected")[0], 200)
        self.assertEqual(restarted_relay.handle_alert(payload, "Bearer expected"), (200, "duplicate suppressed"))

    def test_configmap_alert_state_store_persists_fingerprint_status_without_secret_values(self):
        k8s = FakeConfigMapK8s()
        store = ConfigMapAlertStateStore(k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP)

        store.save("fp-persisted", "firing", 4_000_000_000)

        restarted_store = ConfigMapAlertStateStore(k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP)
        self.assertEqual(restarted_store.load("fp-persisted"), ("firing", 4_000_000_000))
        self.assertNotIn("secret", str(k8s.data).lower())

    def test_concurrent_duplicate_firing_alerts_only_deliver_once(self):
        state = MemoryAlertStateStore()
        deliveries = []
        relay = RelayService(
            alertmanager_auth_token="expected",
            k8s_client=FakeK8s(),
            prometheus_client=FakePrometheus(),
            alert_state_store=state,
            alert_callback=lambda message: deliveries.append(message) or True,
        )
        payload = {"status": "firing", "alerts": [{"fingerprint": "fp-concurrent", "labels": {"alertname": "PodRestartIncrease"}}]}
        barrier = threading.Barrier(2)
        results = []

        def deliver():
            barrier.wait()
            results.append(relay.handle_alert(payload, "Bearer expected"))

        threads = [threading.Thread(target=deliver) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(len(deliveries), 1)

    def test_processed_update_is_not_replied_to_again_after_restart(self):
        offsets = MemoryOffsetStore()
        update = {"update_id": 41, "message": {"chat": {"id": 123}, "text": "/상태"}}
        first_relay = RelayService(
            allowed_chat_id="123", k8s_client=FakeK8s(), prometheus_client=FakePrometheus(), offset_store=offsets
        )

        self.assertIsNotNone(first_relay.handle_update(update))
        self.assertIsNone(offsets.load())
        first_relay.acknowledge_update(update)

        restarted_relay = RelayService(
            allowed_chat_id="123", k8s_client=FakeK8s(), prometheus_client=FakePrometheus(), offset_store=offsets
        )
        self.assertIsNone(restarted_relay.handle_update(update))

    def test_health_endpoint_returns_ok(self):
        relay = RelayService(allowed_chat_id="123", k8s_client=FakeK8s(), prometheus_client=FakePrometheus())

        status, body = handle_http_request(relay, method="GET", path="/healthz")

        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok\n")

    def test_configmap_offset_store_persists_the_next_update_id(self):
        k8s = FakeConfigMapK8s()
        store = ConfigMapOffsetStore(k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP)

        store.save(42)

        restarted_store = ConfigMapOffsetStore(k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP)
        self.assertEqual(restarted_store.load(), 42)

    def test_configmap_offset_store_rejects_non_relay_target(self):
        with self.assertRaises(ValueError):
            ConfigMapOffsetStore(FakeConfigMapK8s(), namespace="other", name=RELAY_STATE_CONFIGMAP)

    def test_successful_status_delivery_commits_update_offset(self):
        offsets = MemoryOffsetStore()
        relay = RelayService(
            allowed_chat_id="123", k8s_client=FakeK8s(), prometheus_client=FakePrometheus(), offset_store=offsets
        )
        telegram = FakePollingTelegram(
            [{"update_id": 41, "message": {"chat": {"id": 123}, "text": "/상태"}}]
        )

        run_polling(relay, telegram, "123", max_cycles=1, sleep_fn=lambda _: None)

        self.assertEqual(offsets.load(), 42)
        self.assertEqual(len(telegram.sent_messages), 1)

    def test_failed_status_delivery_keeps_update_offset_for_retry(self):
        offsets = MemoryOffsetStore()
        relay = RelayService(
            allowed_chat_id="123", k8s_client=FakeK8s(), prometheus_client=FakePrometheus(), offset_store=offsets
        )
        telegram = FakePollingTelegram(
            [{"update_id": 41, "message": {"chat": {"id": 123}, "text": "/상태"}}], send_result=False
        )

        run_polling(relay, telegram, "123", max_cycles=1, sleep_fn=lambda _: None)

        self.assertIsNone(offsets.load())
        self.assertFalse(relay.is_healthy())

    def test_configmap_failure_marks_relay_unhealthy_and_uses_backoff(self):
        relay = RelayService(
            allowed_chat_id="123",
            k8s_client=FakeK8s(),
            prometheus_client=FakePrometheus(),
            offset_store=FailingOffsetStore(),
        )

        delays = []
        run_polling(relay, FakePollingTelegram([]), "123", max_cycles=3, sleep_fn=delays.append)

        self.assertFalse(relay.is_healthy())
        self.assertEqual(delays, [1, 2])

    def test_configmap_write_failure_marks_relay_unhealthy_after_delivery(self):
        relay = RelayService(
            allowed_chat_id="123",
            k8s_client=FakeK8s(),
            prometheus_client=FakePrometheus(),
            offset_store=WriteFailingOffsetStore(),
        )
        telegram = FakePollingTelegram(
            [{"update_id": 41, "message": {"chat": {"id": 123}, "text": "/상태"}}]
        )

        run_polling(relay, telegram, "123", max_cycles=1, sleep_fn=lambda _: None)

        self.assertEqual(len(telegram.sent_messages), 1)
        self.assertFalse(relay.is_healthy())

    def test_alert_callback_failure_returns_retryable_secret_free_response(self):
        relay = RelayService(
            alertmanager_auth_token="expected",
            k8s_client=FakeK8s(),
            prometheus_client=FakePrometheus(),
            alert_callback=lambda _: False,
        )

        status, reply = relay.handle_alert({"status": "firing", "alerts": []}, "Bearer expected")

        self.assertEqual(status, 503)
        self.assertEqual(reply, "delivery failed")
        self.assertNotIn("expected", reply)

    def test_alert_callback_exception_returns_secret_free_http_5xx(self):
        def raise_callback(_):
            raise RuntimeError("delivery error")

        relay = RelayService(
            alertmanager_auth_token="expected",
            k8s_client=FakeK8s(),
            prometheus_client=FakePrometheus(),
            alert_callback=raise_callback,
        )
        status, body = handle_http_request(
            relay,
            method="POST",
            path="/alertmanager",
            authorization="Bearer expected",
            content_length="31",
            body=b'{"status":"firing","alerts":[]}',
        )

        self.assertEqual(status, 503)
        self.assertEqual(body, b"delivery failed\n")

    def test_unhealthy_relay_health_endpoint_returns_503(self):
        relay = RelayService(allowed_chat_id="123", k8s_client=FakeK8s(), prometheus_client=FakePrometheus())
        relay.mark_unhealthy()

        status, body = handle_http_request(relay, method="GET", path="/healthz")

        self.assertEqual(status, 503)
        self.assertEqual(body, b"unavailable\n")

    def test_get_updates_transport_and_api_failures_are_not_empty_polls(self):
        self.assertEqual(ControlledTelegramClient({"ok": True, "result": []}).get_updates(None), [])
        for response in (None, {"ok": False}):
            with self.subTest(response=response):
                with self.assertRaises(TelegramPollingError):
                    ControlledTelegramClient(response).get_updates(None)

    def test_empty_successful_poll_keeps_relay_healthy(self):
        relay = RelayService(allowed_chat_id="123", k8s_client=FakeK8s(), prometheus_client=FakePrometheus())

        run_polling(relay, FakePollingTelegram([]), "123", max_cycles=1, sleep_fn=lambda _: None)

        self.assertTrue(relay.is_healthy())

    def test_get_updates_failure_marks_unhealthy_and_uses_bounded_backoff(self):
        relay = RelayService(allowed_chat_id="123", k8s_client=FakeK8s(), prometheus_client=FakePrometheus())
        telegram = FailingPollingTelegram()
        delays = []

        run_polling(relay, telegram, "123", max_cycles=7, sleep_fn=delays.append)

        self.assertEqual(telegram.calls, 7)
        self.assertFalse(relay.is_healthy())
        self.assertEqual(delays, [1, 2, 4, 8, 16, 30])

    def test_polling_failure_logs_only_secret_free_error_metadata(self):
        relay = RelayService(allowed_chat_id="1", k8s_client=FakeK8s(), prometheus_client=FakePrometheus())

        with self.assertLogs("app.main", level="WARNING") as logs:
            run_polling(relay, SecretLeakingPollingTelegram(), "1", max_cycles=1, sleep_fn=lambda _: None)

        output = "\n".join(logs.output)
        self.assertIn("telegram_polling_failed", output)
        self.assertIn("TelegramPollingError", output)
        self.assertNotIn("telegram_delivery_failed", output)
        self.assertNotIn("super-secret-token", output)

    def test_message_delivery_failure_logs_delivery_without_polling_failure(self):
        relay = RelayService(allowed_chat_id="123", k8s_client=FakeK8s(), prometheus_client=FakePrometheus())
        update = {"update_id": 12, "message": {"chat": {"id": 123}, "text": "/상태"}}

        with self.assertLogs("app.main", level="WARNING") as logs:
            run_polling(
                relay,
                FakePollingTelegram([update], send_result=False),
                "123",
                max_cycles=1,
                sleep_fn=lambda _: None,
            )

        output = "\n".join(logs.output)
        self.assertIn("telegram_delivery_failed", output)
        self.assertNotIn("telegram_polling_failed", output)

    def test_telegram_transport_failure_logs_without_token_or_response_body(self):
        with patch("app.main.urlopen", side_effect=OSError("bot-token=super-secret")):
            with self.assertLogs("app.main", level="WARNING") as logs:
                self.assertIsNone(TelegramClient("bot-token")._post("getUpdates", {}))

        output = "\n".join(logs.output)
        self.assertIn("telegram_http_request_failed", output)
        self.assertIn("OSError", output)
        self.assertNotIn("super-secret", output)

    def test_http_boundary_rejects_unauthorized_alertmanager_request(self):
        relay = RelayService(alertmanager_auth_token="expected", k8s_client=FakeK8s(), prometheus_client=FakePrometheus())

        status, body = handle_http_request(
            relay,
            method="POST",
            path="/alertmanager",
            authorization="Bearer wrong",
            content_length="31",
            body=b'{"status":"firing","alerts":[]}',
        )

        self.assertEqual(status, 401)
        self.assertEqual(body, b"unauthorized\n")
        self.assertNotIn(b"expected", body)

    def test_http_boundary_rejects_malformed_alertmanager_json(self):
        relay = RelayService(alertmanager_auth_token="expected", k8s_client=FakeK8s(), prometheus_client=FakePrometheus())

        status, body = handle_http_request(
            relay,
            method="POST",
            path="/alertmanager",
            authorization="Bearer expected",
            content_length="8",
            body=b"not-json",
        )

        self.assertEqual(status, 400)
        self.assertEqual(body, b"invalid alert payload\n")

    def test_http_boundary_rejects_oversized_alertmanager_body(self):
        relay = RelayService(alertmanager_auth_token="expected", k8s_client=FakeK8s(), prometheus_client=FakePrometheus())

        status, body = handle_http_request(
            relay,
            method="POST",
            path="/alertmanager",
            authorization="Bearer expected",
            content_length="1048577",
            body=b"x",
        )

        self.assertEqual(status, 400)
        self.assertEqual(body, b"invalid request body\n")


if __name__ == "__main__":
    unittest.main()
