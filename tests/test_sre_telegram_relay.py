import json
import sys
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "sre-telegram-relay"))

from app.main import (  # noqa: E402
    BACKUP_STATUS_CONFIGMAP,
    BOOK_BACKUP_STATUS_CONFIGMAP,
    BOOK_BACKUP_NAMESPACE,
    CONFIGMAP_BOOK_BACKUP_DELIVERED_RUN_IDS_KEY,
    CONFIGMAP_YOUTUBE_BACKUP_DELIVERED_RUN_IDS_KEY,
    CONFIGMAP_CRAWLER_BACKUP_DELIVERED_RUN_IDS_KEY,
    YOUTUBE_BACKUP_STATUS_CONFIGMAP,
    CRAWLER_BACKUP_STATUS_CONFIGMAP,
    QUARTERLY_AUDIT_STATUS_CONFIGMAP,
    MAX_BACKUP_DELIVERED_RUN_IDS,
    MAX_QUARTERLY_AUDIT_DELIVERED_RUN_IDS,
    ConfigMapBackupDeliveryStore,
    ConfigMapQuarterlyAuditDeliveryStore,
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
    _read_pvc_backup_job_failures,
    _read_pvc_backup_missed_schedules,
    _read_pvc_backup_report,
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


class FakeBackupStatusK8s(FakeConfigMapK8s):
    def __init__(self, backup_data):
        super().__init__()
        self.backup_data = backup_data

    def get_config_map(self, namespace, name):
        if name == BACKUP_STATUS_CONFIGMAP:
            return {"data": dict(self.backup_data)}
        return super().get_config_map(namespace, name)


class FakeBookBackupStatusK8s(FakeConfigMapK8s):
    def __init__(self, backup_data, name=BOOK_BACKUP_STATUS_CONFIGMAP):
        super().__init__()
        self.backup_data = backup_data
        self.name = name

    def get_config_map(self, namespace, name):
        if namespace == BOOK_BACKUP_NAMESPACE and name == self.name:
            return {"data": dict(self.backup_data)}
        return super().get_config_map(namespace, name)


class FakePvcJobK8s(FakeConfigMapK8s):
    def __init__(self, cronjobs, jobs):
        super().__init__()
        self.cronjobs = cronjobs
        self.jobs = jobs
        self.cronjob_reads = []
        self.job_lists = []

    def get_cron_job(self, namespace, name):
        self.cronjob_reads.append((namespace, name))
        if name not in self.cronjobs:
            raise HTTPError("https://kubernetes.invalid", 404, "missing", {}, None)
        return self.cronjobs[name]

    def list_jobs(self, namespace):
        self.job_lists.append(namespace)
        return self.jobs


def backup_cronjob(name, uid):
    return {
        "apiVersion": "batch/v1", "kind": "CronJob",
        "metadata": {"name": name, "namespace": "personal-server", "uid": uid},
    }


def scheduled_backup_cronjob(name, uid, schedule, *, last_schedule=None, suspend=False):
    cronjob = backup_cronjob(name, uid)
    cronjob["spec"] = {
        "schedule": schedule,
        "timeZone": "Asia/Seoul",
        "startingDeadlineSeconds": 300,
        "suspend": suspend,
    }
    cronjob["status"] = {} if last_schedule is None else {"lastScheduleTime": last_schedule}
    return cronjob


def backup_job(name, uid, owner_name, owner_uid, *, conditions=None, owner_kind="CronJob",
               controller=True, namespace="personal-server"):
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {
            "name": name, "namespace": namespace, "uid": uid,
            "creationTimestamp": "2026-09-27T01:00:00Z",
            "labels": {"batch.kubernetes.io/job-name": name},
            "ownerReferences": [{
                "apiVersion": "batch/v1", "kind": owner_kind, "name": owner_name,
                "uid": owner_uid, "controller": controller,
            }],
        },
        "status": {
            "failed": 1,
            "conditions": conditions if conditions is not None else [{
                "type": "Failed", "status": "True", "reason": "BackoffLimitExceeded",
            }],
        },
    }


class FakeQuarterlyAuditStatusK8s(FakeConfigMapK8s):
    def __init__(self, audit_data):
        super().__init__()
        self.audit_data = audit_data

    def get_config_map(self, namespace, name):
        if name == QUARTERLY_AUDIT_STATUS_CONFIGMAP:
            return {"data": dict(self.audit_data)}
        return super().get_config_map(namespace, name)


class UnavailableBackupStatusK8s(FakeConfigMapK8s):
    def __init__(self, error):
        super().__init__()
        self._error = error

    def get_config_map(self, namespace, name):
        if name == BACKUP_STATUS_CONFIGMAP:
            raise self._error
        return super().get_config_map(namespace, name)


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


class FailOnceBackupDeliveryStore:
    def __init__(self):
        self.run_ids = set()
        self.save_attempts = 0

    def contains(self, run_id):
        return run_id in self.run_ids

    def save(self, run_id):
        self.save_attempts += 1
        if self.save_attempts == 1:
            raise OSError("ConfigMap write unavailable")
        self.run_ids.add(run_id)


class FailOnceQuarterlyAuditDeliveryStore:
    def __init__(self):
        self.run_ids = set()
        self.save_attempts = 0

    def contains(self, run_id):
        return run_id in self.run_ids

    def save(self, run_id):
        self.save_attempts += 1
        if self.save_attempts == 1:
            raise OSError("ConfigMap write unavailable")
        self.run_ids.add(run_id)


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


class SequencePollingTelegram(FakePollingTelegram):
    def __init__(self, updates, send_results):
        super().__init__(updates)
        self._send_results = iter(send_results)

    def send_message(self, chat_id, text):
        self.sent_messages.append((chat_id, text))
        return next(self._send_results)


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
    def test_quarterly_audit_saves_delivery_only_after_sending(self):
        run_id = "20260912T010203Z-reserved-audit"
        k8s = FakeQuarterlyAuditStatusK8s(
            {
                "run_id": run_id,
                "status": "passed",
                "completed_at": "2026-09-12T01:02:03Z",
                "health_audit": "passed",
                "backup_check": "passed",
                "recovery_lab": "passed",
            }
        )
        store = ConfigMapQuarterlyAuditDeliveryStore(
            k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP
        )
        relay = RelayService(
            allowed_chat_id="123",
            k8s_client=k8s,
            prometheus_client=FakePrometheus(),
            quarterly_audit_delivery_store=store,
        )

        def send_message(_chat_id, _message):
            self.assertFalse(store.contains(run_id))
            return True

        self.assertTrue(relay.deliver_quarterly_audit_report(send_message))
        self.assertTrue(store.contains(run_id))

    def test_quarterly_audit_retries_after_post_send_state_write_failure_and_allows_duplicate(self):
        k8s = FakeQuarterlyAuditStatusK8s(
            {
                "run_id": "20260912T010203Z-reservation-failure",
                "status": "passed",
                "completed_at": "2026-09-12T01:02:03Z",
                "health_audit": "passed",
                "backup_check": "passed",
                "recovery_lab": "passed",
            }
        )
        store = FailOnceQuarterlyAuditDeliveryStore()
        relay = RelayService(
            allowed_chat_id="123",
            k8s_client=k8s,
            prometheus_client=FakePrometheus(),
            quarterly_audit_delivery_store=store,
        )
        telegram = SequencePollingTelegram([], [True, True])
        delays = []

        run_polling(relay, telegram, "123", max_cycles=2, sleep_fn=delays.append)

        self.assertTrue(relay.is_healthy())
        self.assertEqual(len(telegram.sent_messages), 2)
        self.assertEqual(delays, [1])
        self.assertTrue(store.contains("20260912T010203Z-reservation-failure"))

    def test_quarterly_audit_failed_send_is_retried_by_polling_backoff(self):
        run_id = "20260912T010203Z-send-failure"
        k8s = FakeQuarterlyAuditStatusK8s(
            {
                "run_id": run_id,
                "status": "passed",
                "completed_at": "2026-09-12T01:02:03Z",
                "health_audit": "passed",
                "backup_check": "passed",
                "recovery_lab": "passed",
            }
        )
        store = ConfigMapQuarterlyAuditDeliveryStore(
            k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP
        )
        relay = RelayService(
            allowed_chat_id="123",
            k8s_client=k8s,
            prometheus_client=FakePrometheus(),
            quarterly_audit_delivery_store=store,
        )
        telegram = SequencePollingTelegram([], [False, True])
        delays = []

        run_polling(relay, telegram, "123", max_cycles=2, sleep_fn=delays.append)

        self.assertTrue(store.contains(run_id))
        self.assertEqual(len(telegram.sent_messages), 2)
        self.assertEqual(delays, [1])

    def test_quarterly_audit_delivery_state_keeps_only_bounded_recent_run_ids(self):
        k8s = FakeConfigMapK8s()
        store = ConfigMapQuarterlyAuditDeliveryStore(
            k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP
        )

        for index in range(MAX_QUARTERLY_AUDIT_DELIVERED_RUN_IDS + 1):
            store.save(f"audit-{index}")

        persisted = json.loads(k8s.data["quarterly_audit_delivered_run_ids"])
        self.assertEqual(len(persisted), MAX_QUARTERLY_AUDIT_DELIVERED_RUN_IDS)
        self.assertEqual(persisted[0], "audit-1")
        self.assertTrue(store.contains(f"audit-{MAX_QUARTERLY_AUDIT_DELIVERED_RUN_IDS}"))
        self.assertFalse(store.contains("audit-0"))

    def test_quarterly_audit_report_delivers_safe_summaries_once_without_raw_report_values(self):
        reports = (
            (
                {
                    "run_id": "20260912T010203Z-audit",
                    "status": "passed",
                    "completed_at": "2026-09-12T01:02:03Z",
                    "health_audit": "passed",
                    "backup_check": "passed",
                    "recovery_lab": "passed",
                },
                "[분기 SRE 점검 완료]",
            ),
            (
                {
                    "run_id": "20260912T010203Z-failed-audit",
                    "status": "failed",
                    "completed_at": "2026-09-12T01:02:03Z",
                    "health_audit": "passed",
                    "backup_check": "failed",
                    "recovery_lab": "passed",
                },
                "[분기 SRE 점검 실패]",
            ),
        )
        for audit_data, heading in reports:
            with self.subTest(status=audit_data["status"]):
                k8s = FakeQuarterlyAuditStatusK8s(audit_data)
                telegram = FakePollingTelegram([])
                relay = RelayService(
                    allowed_chat_id="123",
                    k8s_client=k8s,
                    prometheus_client=FakePrometheus(),
                    quarterly_audit_delivery_store=ConfigMapQuarterlyAuditDeliveryStore(
                        k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP
                    ),
                )

                run_polling(relay, telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
                restarted_relay = RelayService(
                    allowed_chat_id="123",
                    k8s_client=k8s,
                    prometheus_client=FakePrometheus(),
                    quarterly_audit_delivery_store=ConfigMapQuarterlyAuditDeliveryStore(
                        k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP
                    ),
                )
                run_polling(restarted_relay, telegram, "123", max_cycles=1, sleep_fn=lambda _: None)

                self.assertEqual(len(telegram.sent_messages), 1)
                message = telegram.sent_messages[0][1]
                self.assertIn(heading, message)
                self.assertIn("상태 점검: 통과", message)
                self.assertIn("백업 검증 상태: " + ("실패" if audit_data["backup_check"] == "failed" else "통과"), message)
                self.assertIn("격리 Pod 복구 훈련: 통과", message)
                self.assertNotIn(audit_data["run_id"], message)
                self.assertNotIn(audit_data["completed_at"], message)

    def test_quarterly_audit_accepts_complete_slo_summary_and_preserves_delivery_deduplication(self):
        for result, recorded, ok, unknown, label in (
            ("passed", "30", "30", "0", "통과"),
            ("failed", "30", "29", "0", "실패"),
            ("unobservable", "30", "29", "1", "관측 불가"),
            # Stale, gapped, or future dates invalidate coverage despite all records being healthy.
            ("unobservable", "30", "30", "0", "관측 불가"),
            ("unobservable", "3", "3", "0", "관측 불가"),
            ("unobservable", "0", "0", "0", "관측 불가"),
        ):
            with self.subTest(result=result, recorded=recorded, unknown=unknown):
                audit_data = {
                    "run_id": "20260912T010203Z-slo-audit",
                    "status": "passed",
                    "completed_at": "2026-09-12T01:02:03Z",
                    "health_audit": "passed", "backup_check": "passed", "recovery_lab": "passed",
                    "slo_evidence": result, "slo_days_recorded": recorded,
                    "slo_days_ok": ok, "slo_days_unobservable": unknown,
                }
                k8s = FakeQuarterlyAuditStatusK8s(audit_data)
                telegram = FakePollingTelegram([])
                relay = RelayService(
                    allowed_chat_id="123", k8s_client=k8s, prometheus_client=FakePrometheus(),
                    quarterly_audit_delivery_store=ConfigMapQuarterlyAuditDeliveryStore(
                        k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP
                    ),
                )
                self.assertTrue(relay.deliver_quarterly_audit_report(telegram.send_message))
                relay.deliver_quarterly_audit_report(telegram.send_message)
                self.assertEqual(len(telegram.sent_messages), 1)
                message = telegram.sent_messages[0][1]
                self.assertIn("[분기 SRE 점검 완료]", message)
                self.assertIn(f"SLO 증적: {label}", message)
                self.assertIn(f"수집 {recorded}/30일, 정상 {ok}일, 관측 불가 {unknown}일", message)
                self.assertNotIn(audit_data["run_id"], message)
                self.assertNotIn(audit_data["completed_at"], message)

    def test_quarterly_audit_rejects_partial_unknown_and_malformed_slo_summaries(self):
        valid = {
            "run_id": "20260912T010203Z-slo-audit", "status": "passed",
            "completed_at": "2026-09-12T01:02:03Z",
            "health_audit": "passed", "backup_check": "passed", "recovery_lab": "passed",
            "slo_evidence": "passed", "slo_days_recorded": "30",
            "slo_days_ok": "30", "slo_days_unobservable": "0",
        }
        invalid_reports = [{key: value for key, value in valid.items() if key != missing} for missing in valid]
        invalid_reports += [dict(valid, **change) for change in (
            {"records.json": "private-raw-record"}, {"slo_evidence": "private-raw-record"},
            {"slo_days_recorded": "31"}, {"slo_days_ok": "-1"}, {"slo_days_ok": "1.0"},
            {"slo_days_ok": "03"}, {"slo_days_ok": "３０"}, {"slo_days_ok": " 30"},
            {"slo_days_ok": 30}, {"slo_days_unobservable": "1"},
            {"slo_days_recorded": "29"}, {"slo_days_ok": "29"},
            {"slo_evidence": "failed"},
            {"slo_evidence": "unobservable", "slo_days_recorded": "29"},
            {"slo_evidence": "unobservable", "slo_days_unobservable": "1"},
            {"slo_evidence": "unobservable", "slo_days_recorded": "31"},
            {"slo_evidence": "failed", "slo_days_ok": "28", "slo_days_unobservable": "1"},
        )]
        for index, audit_data in enumerate(invalid_reports):
            with self.subTest(case=index):
                k8s = FakeQuarterlyAuditStatusK8s(audit_data)
                telegram = FakePollingTelegram([])
                relay = RelayService(
                    allowed_chat_id="123", k8s_client=k8s, prometheus_client=FakePrometheus(),
                    quarterly_audit_delivery_store=ConfigMapQuarterlyAuditDeliveryStore(
                        k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP
                    ),
                )
                with self.assertLogs("app.main", level="WARNING") as captured:
                    relay.deliver_quarterly_audit_report(telegram.send_message)
                self.assertEqual(telegram.sent_messages, [])
                self.assertEqual(k8s.data, {})
                self.assertNotIn("private-raw-record", "\n".join(captured.output))

    def test_malformed_quarterly_audit_report_is_ignored_without_delivery_or_state_write(self):
        invalid_reports = (
            {"run_id": "20260912T010203Z-audit", "status": "passed", "completed_at": "2026-09-12T01:02:03Z", "health_audit": "passed", "backup_check": "passed"},
            {"run_id": "../../unsafe", "status": "passed", "completed_at": "2026-09-12T01:02:03Z", "health_audit": "passed", "backup_check": "passed", "recovery_lab": "passed"},
            {"run_id": "20260912T010203Z-audit", "status": "unexpected", "completed_at": "2026-09-12T01:02:03Z", "health_audit": "passed", "backup_check": "passed", "recovery_lab": "passed"},
            {"run_id": "20260912T010203Z-audit", "status": "passed", "completed_at": "2026-09-12T01:02:03Z", "health_audit": "unsafe;output", "backup_check": "passed", "recovery_lab": "passed"},
            {"run_id": "20260912T010203Z-audit", "status": "passed", "completed_at": "2026-09-12T01:02:03Z", "health_audit": "failed", "backup_check": "passed", "recovery_lab": "passed"},
            {"run_id": "20260912T010203Z-audit", "status": "failed", "completed_at": "2026-09-12T01:02:03Z", "health_audit": "passed", "backup_check": "passed", "recovery_lab": "passed"},
            {"run_id": "20260912T010203Z-audit", "status": "passed", "completed_at": "2026-9-12T01:02:03Z", "health_audit": "passed", "backup_check": "passed", "recovery_lab": "passed"},
            {"run_id": "20260912T010203Z-audit", "status": "passed", "completed_at": "2026-09-2T01:02:03Z", "health_audit": "passed", "backup_check": "passed", "recovery_lab": "passed"},
        )
        for audit_data in invalid_reports:
            with self.subTest(audit_data=audit_data):
                k8s = FakeQuarterlyAuditStatusK8s(audit_data)
                telegram = FakePollingTelegram([])
                relay = RelayService(
                    allowed_chat_id="123",
                    k8s_client=k8s,
                    prometheus_client=FakePrometheus(),
                    quarterly_audit_delivery_store=ConfigMapQuarterlyAuditDeliveryStore(
                        k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP
                    ),
                )

                run_polling(relay, telegram, "123", max_cycles=1, sleep_fn=lambda _: None)

                self.assertEqual(telegram.sent_messages, [])
                self.assertEqual(k8s.data, {})

    def test_news_collection_alert_has_korean_secret_free_presentation(self):
        relay = RelayService(
            allowed_chat_id="123",
            k8s_client=FakeK8s(),
            prometheus_client=FakePrometheus(),
        )
        message = relay._format_alert(
            "firing",
            [
                {
                    "labels": {
                        "alertname": "NewsCollectionStale",
                        "job": "compose-crawler",
                        "instance": "172.17.0.1:18001",
                    }
                }
            ],
        )
        self.assertIn("뉴스 수집 지연 또는 실패", message)
        self.assertIn("최신 뉴스가 갱신되지 않을 수 있음", message)
        self.assertIn("대상: News Hub 뉴스 수집기", message)
        self.assertIn("상태: 다음 수집 상태를 확인 중입니다.", message)
        self.assertNotIn("상태: 자동 복구를 확인 중입니다.", message)
        self.assertNotIn("crawler-worker", message)
        self.assertNotIn("compose-crawler", message)
        self.assertNotIn("172.17.0.1", message)
        self.assertNotIn("token", message.lower())
        self.assertNotIn("http", message.lower())

        resolved_message = relay._format_alert(
            "resolved",
            [{"labels": {"alertname": "NewsCollectionStale"}}],
        )
        self.assertIn("상태: 정상으로 돌아왔습니다.", resolved_message)

    def test_absent_or_unreadable_backup_status_configmap_keeps_relay_healthy(self):
        for error in (
            HTTPError("https://kubernetes.invalid/backup-status", 404, "not found", None, None),
            OSError("forbidden"),
        ):
            with self.subTest(error=type(error).__name__):
                k8s = UnavailableBackupStatusK8s(error)
                relay = RelayService(
                    allowed_chat_id="123",
                    k8s_client=k8s,
                    prometheus_client=FakePrometheus(),
                    backup_delivery_store=ConfigMapBackupDeliveryStore(
                        k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP
                    ),
                )

                run_polling(relay, FakePollingTelegram([]), "123", max_cycles=1, sleep_fn=lambda _: None)
                status, body = handle_http_request(relay, method="GET", path="/healthz")

                self.assertTrue(relay.is_healthy())
                self.assertEqual((status, body), (200, b"ok\n"))

    def test_backup_delivery_state_keeps_only_bounded_recent_run_ids(self):
        k8s = FakeConfigMapK8s()
        store = ConfigMapBackupDeliveryStore(k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP)

        for index in range(MAX_BACKUP_DELIVERED_RUN_IDS + 1):
            store.save(f"run-{index}")

        persisted = json.loads(k8s.data["backup_delivered_run_ids"])
        self.assertEqual(len(persisted), MAX_BACKUP_DELIVERED_RUN_IDS)
        self.assertEqual(persisted[0], "run-1")
        self.assertTrue(store.contains(f"run-{MAX_BACKUP_DELIVERED_RUN_IDS}"))
        self.assertFalse(store.contains("run-0"))

    def test_backup_report_delivers_each_allowed_status_once_and_persists_run_id(self):
        expected_messages = {
            "completed": "[백업 완료]\n상태: 암호화 백업과 복원 검증을 완료했습니다.\n대상: Portal 데이터",
            "unchanged": "[백업 확인] 변경 없음\n상태: 백업 대상에 변경이 없습니다.\n대상: Portal 데이터",
            "failed": "[백업 실패]\n상태: 백업 실행에 실패했습니다.\n대상: Portal 데이터",
            "restore_failed": "[복원 검증 실패]\n상태: 복원 검증 또는 Portal 준비 상태 확인에 실패했습니다.\n대상: Portal 데이터",
        }
        for status, expected_message in expected_messages.items():
            with self.subTest(status=status):
                k8s = FakeBackupStatusK8s(
                    {
                        "run_id": f"20260905T010203Z-{status}",
                        "status": status,
                        "completed_at": "2026-09-05T01:02:03Z",
                        "stage": "restore-verify",
                    }
                )
                telegram = FakePollingTelegram([])
                relay = RelayService(
                    allowed_chat_id="123",
                    k8s_client=k8s,
                    prometheus_client=FakePrometheus(),
                    backup_delivery_store=ConfigMapBackupDeliveryStore(
                        k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP
                    ),
                )

                run_polling(relay, telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
                restarted_relay = RelayService(
                    allowed_chat_id="123",
                    k8s_client=k8s,
                    prometheus_client=FakePrometheus(),
                    backup_delivery_store=ConfigMapBackupDeliveryStore(
                        k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP
                    ),
                )
                run_polling(restarted_relay, telegram, "123", max_cycles=1, sleep_fn=lambda _: None)

                self.assertEqual(telegram.sent_messages, [("123", expected_message)])

    def test_missed_daily_backups_alert_once_after_grace_across_restart(self):
        schedules = {
            "book-pvc-backup": "0 4 * * *",
            "youtube-pvc-backup": "30 8 * * *",
            "crawler-pvc-backup": "0 13 * * *",
        }
        k8s = FakePvcJobK8s({
            name: scheduled_backup_cronjob(
                name, f"00000000-0000-4000-8000-00000000000{index}", schedule,
                last_schedule="2026-09-28T04:00:00Z",
            )
            for index, (name, schedule) in enumerate(schedules.items(), start=1)
        }, [])
        k8s.data["pvc_backup_monitor_active_from"] = "2026-09-29"
        store = ConfigMapBackupDeliveryStore(
            k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP,
            storage_key="pvc_backup_missed_ids",
        )
        telegram = FakePollingTelegram([])
        now = datetime(2026, 9, 29, 4, 31, tzinfo=timezone.utc)

        def relay():
            return RelayService(
                allowed_chat_id="123", k8s_client=k8s, prometheus_client=FakePrometheus(),
                missed_schedule_delivery_store=store, now_fn=lambda: now,
            )

        run_polling(relay(), telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
        run_polling(relay(), telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
        self.assertEqual(len(telegram.sent_messages), 3)
        self.assertTrue(all("백업 미실행" in message for _, message in telegram.sent_messages))
        self.assertEqual(len(json.loads(k8s.data["pvc_backup_missed_ids"])), 3)

    def test_missed_backup_check_waits_for_grace_and_skips_suspended_or_scheduled(self):
        cronjobs = {
            "book-pvc-backup": scheduled_backup_cronjob(
                "book-pvc-backup", "00000000-0000-4000-8000-000000000001", "0 4 * * *",
                last_schedule="2026-09-28T19:00:00Z",
            ),
            "youtube-pvc-backup": scheduled_backup_cronjob(
                "youtube-pvc-backup", "00000000-0000-4000-8000-000000000002", "30 8 * * *",
                suspend=True,
            ),
            "crawler-pvc-backup": scheduled_backup_cronjob(
                "crawler-pvc-backup", "00000000-0000-4000-8000-000000000003", "0 13 * * *",
                last_schedule="2026-09-28T04:00:00Z",
            ),
        }
        k8s = FakePvcJobK8s(cronjobs, [])
        k8s.data["pvc_backup_monitor_active_from"] = "2026-09-29"
        telegram = FakePollingTelegram([])
        relay = RelayService(
            allowed_chat_id="123", k8s_client=k8s, prometheus_client=FakePrometheus(),
            missed_schedule_delivery_store=ConfigMapBackupDeliveryStore(
                k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP,
                storage_key="pvc_backup_missed_ids",
            ),
            now_fn=lambda: datetime(2026, 9, 29, 4, 29, tzinfo=timezone.utc),
        )
        run_polling(relay, telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
        self.assertEqual(telegram.sent_messages, [])

    def test_missed_backup_check_runs_immediately_after_relay_start(self):
        k8s = FakePvcJobK8s({
            name: scheduled_backup_cronjob(name, f"00000000-0000-4000-8000-00000000000{index}", schedule,
                                           last_schedule="2026-09-28T04:00:00Z")
            for index, (name, schedule) in enumerate((
                ("book-pvc-backup", "0 4 * * *"),
                ("youtube-pvc-backup", "30 8 * * *"),
                ("crawler-pvc-backup", "0 13 * * *"),
            ), start=1)
        }, [])
        k8s.data["pvc_backup_monitor_active_from"] = "2026-09-29"
        telegram = FakePollingTelegram([])
        relay = RelayService(
            allowed_chat_id="123", k8s_client=k8s, prometheus_client=FakePrometheus(),
            missed_schedule_delivery_store=ConfigMapBackupDeliveryStore(
                k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP,
                storage_key="pvc_backup_missed_ids",
            ),
            now_fn=lambda: datetime(2026, 9, 29, 4, 31, tzinfo=timezone.utc),
        )
        with patch("app.main.time.monotonic", return_value=0):
            run_polling(relay, telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
        self.assertEqual(len(telegram.sent_messages), 3)

    def test_missed_monitor_matches_approved_production_cronjob_state(self):
        target = json.loads((REPO_ROOT / "infra/k8s/backup-automation/production-cronjob-state.json").read_text())
        cronjobs = {}
        for index, entry in enumerate(target["cronJobs"], start=1):
            cronjob = scheduled_backup_cronjob(
                entry["name"], f"00000000-0000-4000-8000-00000000000{index}", entry["schedule"],
                suspend=entry["suspend"], last_schedule="2026-09-28T04:00:00Z",
            )
            cronjob["spec"]["timeZone"] = entry["timeZone"]
            cronjob["spec"]["startingDeadlineSeconds"] = entry["startingDeadlineSeconds"]
            cronjobs[entry["name"]] = cronjob
        k8s = FakePvcJobK8s(cronjobs, [])
        k8s.data["pvc_backup_monitor_active_from"] = "2026-09-29"
        telegram = FakePollingTelegram([])
        relay = RelayService(
            allowed_chat_id="123", k8s_client=k8s, prometheus_client=FakePrometheus(),
            missed_schedule_delivery_store=ConfigMapBackupDeliveryStore(
                k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP,
                storage_key="pvc_backup_missed_ids",
            ),
            now_fn=lambda: datetime(2026, 9, 29, 4, 31, tzinfo=timezone.utc),
        )
        run_polling(relay, telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
        self.assertTrue(relay.is_healthy())
        self.assertEqual(len(telegram.sent_messages), 3)

    def test_missed_backup_from_previous_day_is_detected_after_restart(self):
        cronjobs = {
            name: scheduled_backup_cronjob(name, f"00000000-0000-4000-8000-00000000000{index}", schedule,
                                           last_schedule="2026-09-26T19:00:00Z")
            for index, (name, schedule) in enumerate((
                ("book-pvc-backup", "0 4 * * *"),
                ("youtube-pvc-backup", "30 8 * * *"),
                ("crawler-pvc-backup", "0 13 * * *"),
            ), start=1)
        }
        k8s = FakePvcJobK8s(cronjobs, [])
        missed = _read_pvc_backup_missed_schedules(
            k8s, datetime(2026, 9, 29, 3, 59, tzinfo=timezone.utc), date(2026, 9, 28)
        )
        self.assertIn(("book", "2026-09-28"), missed)

    def test_first_monitor_start_does_not_retroactively_alert_suspended_history(self):
        cronjobs = {
            name: scheduled_backup_cronjob(name, f"00000000-0000-4000-8000-00000000000{index}", schedule)
            for index, (name, schedule) in enumerate((
                ("book-pvc-backup", "0 4 * * *"),
                ("youtube-pvc-backup", "30 8 * * *"),
                ("crawler-pvc-backup", "0 13 * * *"),
            ), start=1)
        }
        k8s = FakePvcJobK8s(cronjobs, [])
        telegram = FakePollingTelegram([])

        def relay(now):
            return RelayService(
                allowed_chat_id="123", k8s_client=k8s, prometheus_client=FakePrometheus(),
                missed_schedule_delivery_store=ConfigMapBackupDeliveryStore(
                    k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP,
                    storage_key="pvc_backup_missed_ids",
                ), now_fn=lambda: now,
            )

        run_polling(relay(datetime(2026, 9, 28, 7, 0, tzinfo=timezone.utc)), telegram, "123",
                    max_cycles=1, sleep_fn=lambda _: None)
        self.assertEqual(k8s.data["pvc_backup_monitor_active_from"], "2026-09-29")
        self.assertEqual(telegram.sent_messages, [])
        run_polling(relay(datetime(2026, 9, 29, 4, 31, tzinfo=timezone.utc)), telegram, "123",
                    max_cycles=1, sleep_fn=lambda _: None)
        self.assertEqual(len(telegram.sent_messages), 3)
        self.assertEqual(len(json.loads(k8s.data["pvc_backup_missed_ids"])), 3)

    def test_missing_cronjob_is_monitor_error_not_missed_delivery(self):
        k8s = FakePvcJobK8s({}, [])
        with self.assertRaises(ValueError):
            _read_pvc_backup_missed_schedules(
                k8s, datetime(2026, 9, 29, 4, 31, tzinfo=timezone.utc), date(2026, 9, 29)
            )

    def test_fixed_pvc_cronjob_terminal_failures_alert_once_across_relay_restart(self):
        cron_uids = {
            "book-pvc-backup": "00000000-0000-4000-8000-000000000001",
            "youtube-pvc-backup": "00000000-0000-4000-8000-000000000002",
            "crawler-pvc-backup": "00000000-0000-4000-8000-000000000003",
        }
        job_uids = (
            "10000000-0000-4000-8000-000000000001",
            "10000000-0000-4000-8000-000000000002",
            "10000000-0000-4000-8000-000000000003",
        )
        k8s = FakePvcJobK8s(
            {name: backup_cronjob(name, uid) for name, uid in cron_uids.items()},
            [backup_job(f"{name}-29840760", uid, name, cron_uids[name])
             for (name, uid) in zip(cron_uids, job_uids)],
        )
        telegram = FakePollingTelegram([])

        def relay():
            return RelayService(
                allowed_chat_id="123", k8s_client=k8s, prometheus_client=FakePrometheus(),
                job_failure_delivery_store=ConfigMapBackupDeliveryStore(
                    k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP,
                    storage_key="pvc_backup_job_failed_ids",
                ),
            )

        run_polling(relay(), telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
        run_polling(relay(), telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
        self.assertEqual(len(telegram.sent_messages), 3)
        for target, (_, message) in zip(("Book Memo", "YouTube Memo", "News Hub 뉴스 수집기"), telegram.sent_messages):
            self.assertIn(target, message)
            self.assertIn("백업 작업 실패", message)
            self.assertNotIn("29840760", message)
            self.assertNotIn("10000000-", message)
        self.assertEqual(len(json.loads(k8s.data["pvc_backup_job_failed_ids"])), 3)
        self.assertNotIn("backup_delivered_run_ids", k8s.data)
        self.assertEqual(sorted(set(k8s.cronjob_reads)), [
            ("personal-server", "book-pvc-backup"),
            ("personal-server", "crawler-pvc-backup"),
            ("personal-server", "youtube-pvc-backup"),
        ])
        self.assertEqual(k8s.job_lists, ["personal-server", "personal-server"])

    def test_job_list_items_without_type_fields_still_report_failed_backup(self):
        cron_uid = "00000000-0000-4000-8000-000000000001"
        job = backup_job("crawler-pvc-backup-29840760", "10000000-0000-4000-8000-000000000001",
                         "crawler-pvc-backup", cron_uid)
        job.pop("apiVersion")
        job.pop("kind")
        forged = backup_job("crawler-pvc-backup-29840761", "10000000-0000-4000-8000-000000000002",
                            "crawler-pvc-backup", cron_uid)
        forged["kind"] = "Deployment"
        k8s = FakePvcJobK8s({"crawler-pvc-backup": backup_cronjob("crawler-pvc-backup", cron_uid)},
                            [job, forged])
        self.assertEqual(_read_pvc_backup_job_failures(k8s), [("crawler", job["metadata"]["uid"])])

    def test_job_failure_reader_rejects_forged_owner_nonterminal_and_invalid_uid(self):
        cron_uid = "00000000-0000-4000-8000-000000000001"
        valid_uid = "10000000-0000-4000-8000-000000000001"
        valid = backup_job("book-pvc-backup-29840760", valid_uid, "book-pvc-backup", cron_uid)
        wrong_owner_uid = backup_job("book-pvc-backup-29840761", "10000000-0000-4000-8000-000000000002",
                                     "book-pvc-backup", "00000000-0000-4000-8000-000000000099")
        wrong_owner_kind = backup_job("book-pvc-backup-29840762", "10000000-0000-4000-8000-000000000003",
                                      "book-pvc-backup", cron_uid, owner_kind="Deployment")
        non_controller = backup_job("book-pvc-backup-29840763", "10000000-0000-4000-8000-000000000004",
                                    "book-pvc-backup", cron_uid, controller=False)
        manual_name = backup_job("manual-backup", "10000000-0000-4000-8000-000000000005",
                                 "book-pvc-backup", cron_uid)
        nonterminal = backup_job("book-pvc-backup-29840764", "10000000-0000-4000-8000-000000000006",
                                 "book-pvc-backup", cron_uid, conditions=[])
        completed = backup_job("book-pvc-backup-29840765", "10000000-0000-4000-8000-000000000007",
                               "book-pvc-backup", cron_uid, conditions=[
                                   {"type": "Failed", "status": "True"}, {"type": "Complete", "status": "True"},
                               ])
        invalid_uid = backup_job("book-pvc-backup-29840766", "../unsafe", "book-pvc-backup", cron_uid)
        k8s = FakePvcJobK8s(
            {"book-pvc-backup": backup_cronjob("book-pvc-backup", cron_uid)},
            [valid, wrong_owner_uid, wrong_owner_kind, non_controller, manual_name,
             nonterminal, completed, invalid_uid],
        )
        telegram = FakePollingTelegram([])
        relay = RelayService(
            allowed_chat_id="123", k8s_client=k8s, prometheus_client=FakePrometheus(),
            job_failure_delivery_store=ConfigMapBackupDeliveryStore(
                k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP,
                storage_key="pvc_backup_job_failed_ids",
            ),
        )
        run_polling(relay, telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
        self.assertEqual(len(telegram.sent_messages), 1)
        self.assertTrue(relay.is_healthy())
        self.assertEqual(len(json.loads(k8s.data["pvc_backup_job_failed_ids"])), 1)

    def test_job_failure_owner_uid_rotation_ignores_old_job_and_alerts_new_failure(self):
        first_cron_uid = "00000000-0000-4000-8000-000000000001"
        next_cron_uid = "00000000-0000-4000-8000-000000000002"
        old_job = backup_job("book-pvc-backup-29840760", "10000000-0000-4000-8000-000000000001",
                             "book-pvc-backup", first_cron_uid)
        new_job = backup_job("book-pvc-backup-29840761", "10000000-0000-4000-8000-000000000002",
                             "book-pvc-backup", next_cron_uid)
        k8s = FakePvcJobK8s({"book-pvc-backup": backup_cronjob("book-pvc-backup", first_cron_uid)}, [old_job])
        telegram = FakePollingTelegram([])
        store = ConfigMapBackupDeliveryStore(
            k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP,
            storage_key="pvc_backup_job_failed_ids",
        )

        def relay():
            return RelayService(allowed_chat_id="123", k8s_client=k8s,
                                prometheus_client=FakePrometheus(), job_failure_delivery_store=store)

        run_polling(relay(), telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
        k8s.cronjobs["book-pvc-backup"] = backup_cronjob("book-pvc-backup", next_cron_uid)
        k8s.jobs.append(new_job)
        run_polling(relay(), telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
        self.assertEqual(len(telegram.sent_messages), 2)
        self.assertEqual(len(json.loads(k8s.data["pvc_backup_job_failed_ids"])), 2)

    def test_job_failure_rejected_delivery_and_state_save_failure_are_retryable(self):
        cron_uid = "00000000-0000-4000-8000-000000000001"
        job = backup_job("book-pvc-backup-29840760", "10000000-0000-4000-8000-000000000001",
                         "book-pvc-backup", cron_uid)
        k8s = FakePvcJobK8s({"book-pvc-backup": backup_cronjob("book-pvc-backup", cron_uid)}, [job])
        store = FailOnceBackupDeliveryStore()

        def relay():
            return RelayService(allowed_chat_id="123", k8s_client=k8s,
                                prometheus_client=FakePrometheus(), job_failure_delivery_store=store)

        rejected = FakePollingTelegram([], send_result=False)
        run_polling(relay(), rejected, "123", max_cycles=1, sleep_fn=lambda _: None)
        self.assertEqual(len(rejected.sent_messages), 1)
        self.assertEqual(store.save_attempts, 0)

        accepted = FakePollingTelegram([])
        run_polling(relay(), accepted, "123", max_cycles=1, sleep_fn=lambda _: None)
        self.assertEqual(len(accepted.sent_messages), 1)
        self.assertEqual(store.save_attempts, 1)
        run_polling(relay(), accepted, "123", max_cycles=1, sleep_fn=lambda _: None)
        self.assertEqual(len(accepted.sent_messages), 2)
        self.assertEqual(store.save_attempts, 2)
        run_polling(relay(), accepted, "123", max_cycles=1, sleep_fn=lambda _: None)
        self.assertEqual(len(accepted.sent_messages), 2)

    def test_malformed_fixed_backup_cronjob_marks_job_monitor_unhealthy(self):
        k8s = FakePvcJobK8s({
            "book-pvc-backup": backup_cronjob("book-pvc-backup", "invalid-uid"),
        }, [])
        telegram = FakePollingTelegram([])
        relay = RelayService(
            allowed_chat_id="123", k8s_client=k8s, prometheus_client=FakePrometheus(),
            job_failure_delivery_store=ConfigMapBackupDeliveryStore(
                k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP,
                storage_key="pvc_backup_job_failed_ids",
            ),
        )
        run_polling(relay, telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
        self.assertFalse(relay.is_healthy())
        self.assertEqual(telegram.sent_messages, [])

    def test_book_backup_failure_then_completed_run_alerts_once_each_across_restart(self):
        data = {
            "lock_run_id": "", "evidence": "", "run_id": "20260927T010203Z-101",
            "status": "failed", "completed_at": "2026-09-27T01:02:03Z", "stage": "remote-restore",
        }
        k8s = FakeBookBackupStatusK8s(data)
        telegram = FakePollingTelegram([])

        def relay():
            return RelayService(
                allowed_chat_id="123", k8s_client=k8s, prometheus_client=FakePrometheus(),
                book_backup_delivery_store=ConfigMapBackupDeliveryStore(
                    k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP,
                    storage_key=CONFIGMAP_BOOK_BACKUP_DELIVERED_RUN_IDS_KEY,
                ),
            )

        run_polling(relay(), telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
        run_polling(relay(), telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
        self.assertEqual(len(telegram.sent_messages), 1)
        self.assertIn("Book Memo", telegram.sent_messages[0][1])
        self.assertIn("백업 실패", telegram.sent_messages[0][1])
        self.assertNotIn("remote-restore", telegram.sent_messages[0][1])

        k8s.backup_data = {
            "lock_run_id": "", "evidence": "schema_version=1\nscope=book-memo\nbackup_status=success\nrestore_status=success\n",
            "run_id": "20260928T010203Z-102", "status": "completed",
            "completed_at": "2026-09-28T01:02:03Z", "stage": "completed",
        }
        run_polling(relay(), telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
        run_polling(relay(), telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
        self.assertEqual(len(telegram.sent_messages), 2)
        self.assertIn("Book Memo", telegram.sent_messages[1][1])
        self.assertIn("백업 완료", telegram.sent_messages[1][1])
        self.assertNotIn("schema_version", telegram.sent_messages[1][1])
        self.assertIn(CONFIGMAP_BOOK_BACKUP_DELIVERED_RUN_IDS_KEY, k8s.data)
        self.assertNotIn("backup_delivered_run_ids", k8s.data)

    def test_book_backup_running_and_malformed_report_do_not_alert(self):
        base = {
            "lock_run_id": "20260927T010203Z-101", "evidence": "", "run_id": "20260927T010203Z-101",
            "status": "running", "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "stage": "preflight",
        }
        invalid = [
            base,
            {**base, "status": "failed", "evidence": "raw-secret"},
            {**base, "status": "completed", "lock_run_id": "", "evidence": ""},
            {**base, "status": "failed", "run_id": "../../unsafe"},
            {**base, "status": "failed", "extra": "private"},
        ]
        for report in invalid:
            with self.subTest(report=report):
                k8s = FakeBookBackupStatusK8s(report)
                telegram = FakePollingTelegram([])
                relay = RelayService(
                    allowed_chat_id="123", k8s_client=k8s, prometheus_client=FakePrometheus(),
                    book_backup_delivery_store=ConfigMapBackupDeliveryStore(
                        k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP,
                        storage_key=CONFIGMAP_BOOK_BACKUP_DELIVERED_RUN_IDS_KEY,
                    ),
                )
                run_polling(relay, telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
                self.assertEqual(telegram.sent_messages, [])
                self.assertEqual(k8s.data, {})

    def test_stale_running_book_backup_alerts_once_then_final_state_alerts(self):
        for final_status in ("failed", "completed"):
            with self.subTest(final_status=final_status):
                run_id = "20260927T010203Z-105"
                k8s = FakeBookBackupStatusK8s({
                    "lock_run_id": run_id, "evidence": "", "run_id": run_id,
                    "status": "running",
                    "completed_at": (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "stage": "remote-upload",
                })
                telegram = FakePollingTelegram([])

                def relay():
                    return RelayService(
                        allowed_chat_id="123", k8s_client=k8s, prometheus_client=FakePrometheus(),
                        book_backup_delivery_store=ConfigMapBackupDeliveryStore(
                            k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP,
                            storage_key=CONFIGMAP_BOOK_BACKUP_DELIVERED_RUN_IDS_KEY,
                        ),
                    )

                run_polling(relay(), telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
                run_polling(relay(), telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
                self.assertEqual(len(telegram.sent_messages), 1)
                self.assertIn("장기 실행", telegram.sent_messages[0][1])

                k8s.backup_data = {
                    "lock_run_id": "", "run_id": run_id, "status": final_status,
                    "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "stage": "completed" if final_status == "completed" else "remote-upload",
                    "evidence": (
                        "schema_version=1\nscope=book-memo\nbackup_status=success\nrestore_status=success\n"
                        if final_status == "completed" else ""
                    ),
                }
                run_polling(relay(), telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
                run_polling(relay(), telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
                self.assertEqual(len(telegram.sent_messages), 2)
                self.assertIn("백업 완료" if final_status == "completed" else "백업 실패", telegram.sent_messages[1][1])

    def test_youtube_and_crawler_stale_failure_recovery_are_independently_deduplicated(self):
        services = (
            ("youtube", YOUTUBE_BACKUP_STATUS_CONFIGMAP, CONFIGMAP_YOUTUBE_BACKUP_DELIVERED_RUN_IDS_KEY, "YouTube Memo", "youtube-memo"),
            ("crawler", CRAWLER_BACKUP_STATUS_CONFIGMAP, CONFIGMAP_CRAWLER_BACKUP_DELIVERED_RUN_IDS_KEY, "News Hub 뉴스 수집기", "crawler-worker"),
        )
        for service, configmap, state_key, target, scope in services:
            with self.subTest(service=service):
                run_id = "20260927T010203Z-106"
                k8s = FakeBookBackupStatusK8s({
                    "lock_run_id": run_id, "evidence": "", "run_id": run_id, "status": "running",
                    "completed_at": (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "stage": "remote-upload",
                }, name=configmap)
                telegram = FakePollingTelegram([])

                def relay():
                    return RelayService(
                        allowed_chat_id="123", k8s_client=k8s, prometheus_client=FakePrometheus(),
                        **{f"{service}_backup_delivery_store": ConfigMapBackupDeliveryStore(
                            k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP, storage_key=state_key,
                        )},
                    )

                for _ in range(2):
                    run_polling(relay(), telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
                k8s.backup_data = {
                    "lock_run_id": "", "evidence": "", "run_id": run_id, "status": "failed",
                    "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "stage": "remote-upload",
                }
                for _ in range(2):
                    run_polling(relay(), telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
                k8s.backup_data = {
                    "lock_run_id": "", "run_id": "20260928T010203Z-107", "status": "completed",
                    "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "stage": "completed",
                    "evidence": f"schema_version=1\nscope={scope}\nbackup_status=success\nrestore_status=success\n",
                }
                for _ in range(2):
                    run_polling(relay(), telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
                self.assertEqual(len(telegram.sent_messages), 3)
                self.assertEqual(["장기 실행", "백업 실패", "백업 완료"], [
                    next(label for label in ("장기 실행", "백업 실패", "백업 완료") if label in message)
                    for _, message in telegram.sent_messages
                ])
                self.assertTrue(all(target in message for _, message in telegram.sent_messages))
                self.assertIn(state_key, k8s.data)
                self.assertNotIn(CONFIGMAP_BOOK_BACKUP_DELIVERED_RUN_IDS_KEY, k8s.data)

    def test_book_backup_retries_rejected_delivery_without_marking_run_delivered(self):
        k8s = FakeBookBackupStatusK8s({
            "lock_run_id": "", "evidence": "", "run_id": "20260927T010203Z-103",
            "status": "failed", "completed_at": "2026-09-27T01:02:03Z", "stage": "writer-recovery",
        })

        def relay():
            return RelayService(
                allowed_chat_id="123", k8s_client=k8s, prometheus_client=FakePrometheus(),
                book_backup_delivery_store=ConfigMapBackupDeliveryStore(
                    k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP,
                    storage_key=CONFIGMAP_BOOK_BACKUP_DELIVERED_RUN_IDS_KEY,
                ),
            )

        rejected = FakePollingTelegram([], send_result=False)
        run_polling(relay(), rejected, "123", max_cycles=1, sleep_fn=lambda _: None)
        self.assertEqual(len(rejected.sent_messages), 1)
        self.assertNotIn(CONFIGMAP_BOOK_BACKUP_DELIVERED_RUN_IDS_KEY, k8s.data)
        accepted = FakePollingTelegram([])
        run_polling(relay(), accepted, "123", max_cycles=1, sleep_fn=lambda _: None)
        self.assertEqual(len(accepted.sent_messages), 1)
        self.assertIn(CONFIGMAP_BOOK_BACKUP_DELIVERED_RUN_IDS_KEY, k8s.data)

    def test_book_backup_state_write_failure_is_retried_and_keeps_portal_state(self):
        k8s = FakeBookBackupStatusK8s({
            "lock_run_id": "", "evidence": "", "run_id": "20260927T010203Z-104",
            "status": "failed", "completed_at": "2026-09-27T01:02:03Z", "stage": "remote-upload",
        })
        k8s.data["backup_delivered_run_ids"] = '["portal-run"]'
        store = FailOnceBackupDeliveryStore()
        telegram = FakePollingTelegram([])
        for _ in range(2):
            relay = RelayService(
                allowed_chat_id="123", k8s_client=k8s, prometheus_client=FakePrometheus(),
                book_backup_delivery_store=store,
            )
            run_polling(relay, telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
        self.assertEqual(len(telegram.sent_messages), 2)
        self.assertEqual(store.save_attempts, 2)
        self.assertEqual(k8s.data["backup_delivered_run_ids"], '["portal-run"]')

    def test_book_backup_delivery_store_rejects_unrelated_state_key(self):
        with self.assertRaises(ValueError):
            ConfigMapBackupDeliveryStore(
                FakeConfigMapK8s(), namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP,
                storage_key="telegram_next_update_id",
            )

    def test_pvc_backup_reader_refuses_non_allowlisted_service_before_api_call(self):
        k8s = FakeConfigMapK8s()
        with patch.object(k8s, "get_config_map") as read:
            with self.assertRaises(ValueError):
                _read_pvc_backup_report(k8s, "arbitrary-service")
        read.assert_not_called()

    def test_unavailable_pvc_backup_state_warning_is_rate_limited_and_recovers(self):
        k8s = FakeBookBackupStatusK8s({
            "lock_run_id": "", "evidence": "", "run_id": "", "status": "",
            "completed_at": "", "stage": "",
        })
        missing = HTTPError("https://kubernetes.invalid", 404, "missing", {}, None)
        with patch("app.main._PVC_BACKUP_WARNING_AT", {}), patch("app.main.time.monotonic") as now, \
                patch("app.main.LOGGER.warning") as warning:
            with patch.object(k8s, "get_config_map", side_effect=missing):
                now.return_value = 100.0
                self.assertIsNone(_read_pvc_backup_report(k8s, "book"))
                now.return_value = 101.0
                self.assertIsNone(_read_pvc_backup_report(k8s, "book"))
                self.assertEqual(warning.call_count, 1)
                now.return_value = 3701.0
                self.assertIsNone(_read_pvc_backup_report(k8s, "book"))
                self.assertEqual(warning.call_count, 2)
            self.assertIsNone(_read_pvc_backup_report(k8s, "book"))
            with patch.object(k8s, "get_config_map", side_effect=missing):
                now.return_value = 3702.0
                self.assertIsNone(_read_pvc_backup_report(k8s, "book"))
                self.assertEqual(warning.call_count, 3)

    def test_backup_report_remains_eligible_when_delivery_state_write_fails(self):
        k8s = FakeBackupStatusK8s(
            {
                "run_id": "20260905T010203Z-retry",
                "status": "completed",
                "completed_at": "2026-09-05T01:02:03Z",
                "stage": "restore-verify",
            }
        )
        store = FailOnceBackupDeliveryStore()
        telegram = FakePollingTelegram([])

        relay = RelayService(
            allowed_chat_id="123",
            k8s_client=k8s,
            prometheus_client=FakePrometheus(),
            backup_delivery_store=store,
        )
        run_polling(relay, telegram, "123", max_cycles=1, sleep_fn=lambda _: None)
        restarted_relay = RelayService(
            allowed_chat_id="123",
            k8s_client=k8s,
            prometheus_client=FakePrometheus(),
            backup_delivery_store=store,
        )
        run_polling(restarted_relay, telegram, "123", max_cycles=1, sleep_fn=lambda _: None)

        self.assertEqual(len(telegram.sent_messages), 2)
        self.assertEqual(store.save_attempts, 2)

    def test_malformed_backup_report_is_ignored_without_delivery_or_state_write(self):
        invalid_reports = (
            {"run_id": "20260905T010203Z-ok", "status": "completed", "completed_at": "2026-09-05 01:02:03Z", "stage": "restore-verify"},
            {"run_id": "../../unsafe", "status": "completed", "completed_at": "2026-09-05T01:02:03Z", "stage": "restore-verify"},
            {"run_id": "20260905T010203Z-ok", "status": "unexpected", "completed_at": "2026-09-05T01:02:03Z", "stage": "restore-verify"},
            {"run_id": "20260905T010203Z-ok", "status": "completed", "completed_at": "2026-09-05T01:02:03Z", "stage": "shell;unsafe"},
        )
        for backup_data in invalid_reports:
            with self.subTest(backup_data=backup_data):
                k8s = FakeBackupStatusK8s(backup_data)
                telegram = FakePollingTelegram([])
                relay = RelayService(
                    allowed_chat_id="123",
                    k8s_client=k8s,
                    prometheus_client=FakePrometheus(),
                    backup_delivery_store=ConfigMapBackupDeliveryStore(
                        k8s, namespace=RELAY_NAMESPACE, name=RELAY_STATE_CONFIGMAP
                    ),
                )

                run_polling(relay, telegram, "123", max_cycles=1, sleep_fn=lambda _: None)

                self.assertEqual(telegram.sent_messages, [])
                self.assertEqual(k8s.data, {})
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
        self.assertIn("대상: Portal", reply)
        self.assertNotIn("personal-server", reply)
        self.assertNotIn("portal-web", reply)
        self.assertIn("상태: 상태를 확인 중입니다.", reply)
        self.assertNotIn("상태: 다음 수집 상태를 확인 중입니다.", reply)
        self.assertNotIn("PortalUnavailable", reply)

    def test_portal_http_alerts_use_portal_specific_presentations(self):
        relay = RelayService(
            alertmanager_auth_token="expected",
            k8s_client=FakeK8s(),
            prometheus_client=FakePrometheus(),
        )

        for alert_name, problem in (
            ("PortalHttp5xxErrorRateHigh", "Portal HTTP 5xx 오류율 높음"),
            ("PortalHttpP95LatencyHigh", "Portal HTTP p95 응답 지연"),
        ):
            with self.subTest(alert_name=alert_name):
                status, reply = relay.handle_alert(
                    {"status": "firing", "alerts": [{"labels": {"alertname": alert_name}}]},
                    "Bearer expected",
                )

                self.assertEqual(status, 200)
                self.assertIn(f"문제: {problem}", reply)
                self.assertIn("대상: Portal", reply)
                self.assertNotIn("문제: 운영 상태 경고", reply)
                self.assertIn("상태: 상태를 확인 중입니다.", reply)

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

    def test_prometheus_target_alert_hides_internal_instance_and_uses_service_name(self):
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
        self.assertIn("대상: Kubernetes 상태 수집", reply)
        self.assertNotIn("kubelet", reply)
        self.assertNotIn("172.19.121.162", reply)

    def test_workload_alert_maps_pod_and_pvc_identifiers_to_user_facing_service_name(self):
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
                            "alertname": "PVCNotBound",
                            "namespace": "personal-server",
                            "persistentvolumeclaim": "book-memo-data",
                            "pod": "book-memo-7d8f7b8d5c-x9lq2",
                        },
                    }
                ],
            },
            "Bearer expected",
        )

        self.assertEqual(status, 200)
        self.assertIn("대상: Book Memo", reply)
        self.assertNotIn("personal-server", reply)
        self.assertNotIn("book-memo-data", reply)
        self.assertNotIn("book-memo-7d8f7b8d5c-x9lq2", reply)

    def test_workload_alert_maps_news_and_youtube_identifiers_to_user_facing_service_names(self):
        relay = RelayService(
            alertmanager_auth_token="expected",
            k8s_client=FakeK8s(),
            prometheus_client=FakePrometheus(),
        )

        for workload, expected_target in (
            ("crawler-worker-7d8f7b8d5c-x9lq2", "News Hub"),
            ("youtube-memo-data", "YouTube Memo"),
        ):
            with self.subTest(workload=workload):
                status, reply = relay.handle_alert(
                    {
                        "status": "firing",
                        "alerts": [
                            {
                                "fingerprint": workload,
                                "labels": {
                                    "alertname": "PodRestartIncrease",
                                    "namespace": "personal-server",
                                    "pod": workload,
                                },
                            }
                        ],
                    },
                    "Bearer expected",
                )

                self.assertEqual(status, 200)
                self.assertIn(f"대상: {expected_target}", reply)
                self.assertNotIn("personal-server", reply)
                self.assertNotIn(workload, reply)

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
