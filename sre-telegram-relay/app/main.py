"""Internal-only Telegram relay for K3s status and Alertmanager notifications."""

from __future__ import annotations

import hmac
import hashlib
import json
import logging
import os
import re
import ssl
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable, Protocol
from urllib.error import URLError
from urllib.request import Request, urlopen


KUBERNETES_API_URL = "https://kubernetes.default.svc"
PROMETHEUS_API_URL = "http://personal-server-monitoring-prometheus.monitoring.svc:9090"
SERVICE_ACCOUNT_DIRECTORY = "/var/run/secrets/kubernetes.io/serviceaccount"
DEFAULT_NAMESPACES = ("monitoring", "personal-server")
RELAY_NAMESPACE = "monitoring"
RELAY_STATE_CONFIGMAP = "sre-telegram-relay-state"
BACKUP_STATUS_CONFIGMAP = "sre-telegram-backup-status"
MAX_ALERT_ITEMS = 4
MAX_REQUEST_BODY_BYTES = 1_048_576
CONFIGMAP_OFFSET_KEY = "telegram_next_update_id"
CONFIGMAP_ALERT_STATE_KEY = "alert_state"
CONFIGMAP_BACKUP_DELIVERED_RUN_IDS_KEY = "backup_delivered_run_ids"
ALERT_STATE_TTL_SECONDS = 4 * 60 * 60
ALERT_PRESENTATIONS = {
    "PodRestartIncrease": ("서비스가 반복 재시작됨", "서비스 기능이 불안정할 수 있음"),
    "PortalUnavailable": ("Portal 접속 불가", "웹사이트가 열리지 않을 수 있음"),
    "DeploymentUnavailable": ("서비스 실행 수 부족", "일부 기능이 정상 동작하지 않을 수 있음"),
    "PVCNotBound": ("데이터 저장소 연결 실패", "저장된 데이터에 접근하지 못할 수 있음"),
    "PrometheusTargetDown": ("상태 수집 대상 응답 없음", "해당 서비스의 상태를 확인하지 못할 수 있음"),
}
BACKUP_STATUS_MESSAGES = {
    "completed": "[백업 완료]\n상태: 암호화 백업과 복원 검증을 완료했습니다.\n대상: Portal 데이터",
    "unchanged": "[백업 확인] 변경 없음\n상태: 백업 대상에 변경이 없습니다.\n대상: Portal 데이터",
    "failed": "[백업 실패]\n상태: 백업 실행에 실패했습니다.\n대상: Portal 데이터",
    "restore_failed": "[복원 검증 실패]\n상태: 복원 검증 또는 Portal 준비 상태 확인에 실패했습니다.\n대상: Portal 데이터",
}
BACKUP_REPORT_KEYS = frozenset({"run_id", "status", "completed_at", "stage"})
SAFE_BACKUP_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_BACKUP_STAGE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
LOGGER = logging.getLogger(__name__)


class TelegramPollingError(RuntimeError):
    """Telegram did not confirm a successful getUpdates response."""


class OffsetStore(Protocol):
    """Stores the next Telegram update ID to consume across restarts."""

    def load(self) -> int | None: ...

    def save(self, offset: int) -> None: ...


class AlertStateStore(Protocol):
    """Stores one alert fingerprint state for a short, bounded period."""

    def load(self, fingerprint: str) -> tuple[str, float] | None: ...

    def save(self, fingerprint: str, status: str, expires_at: float) -> None: ...


class BackupDeliveryStore(Protocol):
    """Persists backup run IDs already delivered to Telegram."""

    def contains(self, run_id: str) -> bool: ...

    def save(self, run_id: str) -> None: ...


class MemoryOffsetStore:
    """Small state holder used when a persistent ConfigMap-backed store is injected later."""

    def __init__(self, offset: int | None = None) -> None:
        self._offset = offset

    def load(self) -> int | None:
        return self._offset

    def save(self, offset: int) -> None:
        self._offset = offset


class MemoryAlertStateStore:
    """Small injectable alert state store used across relay instances in tests/runtime."""

    def __init__(self) -> None:
        self._states: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def load(self, fingerprint: str) -> tuple[str, float] | None:
        with self._lock:
            state = self._states.get(fingerprint)
            if state is None:
                return None
            if state[1] <= time.time():
                self._states.pop(fingerprint, None)
                return None
            return state

    def save(self, fingerprint: str, status: str, expires_at: float) -> None:
        with self._lock:
            self._states[fingerprint] = (status, expires_at)


class MemoryBackupDeliveryStore:
    """In-memory backup delivery state for isolated tests only."""

    def __init__(self) -> None:
        self._run_ids: set[str] = set()

    def contains(self, run_id: str) -> bool:
        return run_id in self._run_ids

    def save(self, run_id: str) -> None:
        self._run_ids.add(run_id)


class KubernetesClient:
    """Read-only in-cluster Kubernetes API client for the relay status summary."""

    def __init__(
        self,
        api_url: str = KUBERNETES_API_URL,
        token_file: str = f"{SERVICE_ACCOUNT_DIRECTORY}/token",
        ca_file: str = f"{SERVICE_ACCOUNT_DIRECTORY}/ca.crt",
        timeout_seconds: int = 10,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._token_file = token_file
        self._ca_file = ca_file
        self._timeout_seconds = timeout_seconds

    def list_nodes(self) -> list[dict[str, Any]]:
        return self._list_items("/api/v1/nodes")

    def list_pods(self, namespace: str) -> list[dict[str, Any]]:
        return self._list_items(f"/api/v1/namespaces/{namespace}/pods")

    def list_deployments(self, namespace: str) -> list[dict[str, Any]]:
        return self._list_items(f"/apis/apps/v1/namespaces/{namespace}/deployments")

    def list_pvcs(self, namespace: str) -> list[dict[str, Any]]:
        return self._list_items(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims")

    def get_config_map(self, namespace: str, name: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/namespaces/{namespace}/configmaps/{name}")

    def patch_config_map(self, namespace: str, name: str, data: dict[str, str]) -> None:
        self._request_json(
            f"/api/v1/namespaces/{namespace}/configmaps/{name}",
            method="PATCH",
            body={"data": data},
        )

    def _list_items(self, path: str) -> list[dict[str, Any]]:
        payload = self._get_json(path)
        items = payload.get("items")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ValueError("Kubernetes API returned an invalid list response")
        return items

    def _get_json(self, path: str) -> dict[str, Any]:
        return self._request_json(path)

    def _request_json(
        self, path: str, *, method: str = "GET", body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        token = _read_file(self._token_file)
        context = ssl.create_default_context(cafile=self._ca_file)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        request_body = None
        if body is not None:
            request_body = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/merge-patch+json"
        request = Request(
            f"{self._api_url}{path}",
            data=request_body,
            headers=headers,
            method=method,
        )
        with urlopen(request, timeout=self._timeout_seconds, context=context) as response:
            return _decode_object(response.read())


class ConfigMapOffsetStore:
    """Persists the next Telegram update ID in the relay's dedicated ConfigMap."""

    def __init__(self, k8s_client: KubernetesClient, *, namespace: str, name: str) -> None:
        if namespace != RELAY_NAMESPACE or name != RELAY_STATE_CONFIGMAP:
            raise ValueError("Telegram offset storage must use the dedicated relay ConfigMap")
        self._k8s_client = k8s_client
        self._namespace = namespace
        self._name = name

    def load(self) -> int | None:
        config_map = self._k8s_client.get_config_map(self._namespace, self._name)
        data = config_map.get("data")
        value = data.get(CONFIGMAP_OFFSET_KEY) if isinstance(data, dict) else None
        if value is None:
            return None
        try:
            offset = int(value)
        except (TypeError, ValueError):
            raise ValueError("relay ConfigMap has an invalid Telegram offset") from None
        if offset < 0:
            raise ValueError("relay ConfigMap has a negative Telegram offset")
        return offset

    def save(self, offset: int) -> None:
        if offset < 0:
            raise ValueError("Telegram offset must be non-negative")
        self._k8s_client.patch_config_map(
            self._namespace,
            self._name,
            {CONFIGMAP_OFFSET_KEY: str(offset)},
        )


class ConfigMapAlertStateStore:
    """Persists bounded, secret-free alert fingerprint state in the relay ConfigMap."""

    def __init__(self, k8s_client: KubernetesClient, *, namespace: str, name: str) -> None:
        if namespace != RELAY_NAMESPACE or name != RELAY_STATE_CONFIGMAP:
            raise ValueError("Alert state storage must use the dedicated relay ConfigMap")
        self._k8s_client = k8s_client
        self._namespace = namespace
        self._name = name

    def load(self, fingerprint: str) -> tuple[str, float] | None:
        states = self._read_states()
        record = states.get(_alert_state_key(fingerprint))
        if not isinstance(record, dict):
            return None
        status = record.get("status")
        expires_at = record.get("expires_at")
        if status not in {"firing", "resolved"} or not isinstance(expires_at, (int, float)):
            raise ValueError("relay ConfigMap has invalid alert state")
        if expires_at <= time.time():
            return None
        return status, float(expires_at)

    def save(self, fingerprint: str, status: str, expires_at: float) -> None:
        if status not in {"firing", "resolved"} or expires_at <= 0:
            raise ValueError("invalid alert state")
        states = self._read_states()
        now = time.time()
        states = {
            key: value
            for key, value in states.items()
            if isinstance(value, dict)
            and isinstance(value.get("expires_at"), (int, float))
            and value["expires_at"] > now
        }
        states[_alert_state_key(fingerprint)] = {
            "status": status,
            "expires_at": expires_at,
        }
        self._k8s_client.patch_config_map(
            self._namespace,
            self._name,
            {CONFIGMAP_ALERT_STATE_KEY: json.dumps(states, separators=(",", ":"), sort_keys=True)},
        )

    def _read_states(self) -> dict[str, Any]:
        config_map = self._k8s_client.get_config_map(self._namespace, self._name)
        data = config_map.get("data")
        raw = data.get(CONFIGMAP_ALERT_STATE_KEY) if isinstance(data, dict) else None
        if raw is None:
            return {}
        try:
            states = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("relay ConfigMap has invalid alert state data") from None
        if not isinstance(states, dict):
            raise ValueError("relay ConfigMap has invalid alert state data")
        return states


class ConfigMapBackupDeliveryStore:
    """Persists delivered backup run IDs in the relay's existing state ConfigMap."""

    def __init__(self, k8s_client: KubernetesClient, *, namespace: str, name: str) -> None:
        if namespace != RELAY_NAMESPACE or name != RELAY_STATE_CONFIGMAP:
            raise ValueError("Backup delivery storage must use the dedicated relay ConfigMap")
        self._k8s_client = k8s_client
        self._namespace = namespace
        self._name = name

    def contains(self, run_id: str) -> bool:
        return run_id in self._read_run_ids()

    def save(self, run_id: str) -> None:
        if not SAFE_BACKUP_RUN_ID.fullmatch(run_id):
            raise ValueError("invalid backup run ID")
        run_ids = self._read_run_ids()
        if run_id in run_ids:
            return
        run_ids.append(run_id)
        self._k8s_client.patch_config_map(
            self._namespace,
            self._name,
            {CONFIGMAP_BACKUP_DELIVERED_RUN_IDS_KEY: json.dumps(run_ids, separators=(",", ":"))},
        )

    def _read_run_ids(self) -> list[str]:
        config_map = self._k8s_client.get_config_map(self._namespace, self._name)
        data = config_map.get("data")
        raw = data.get(CONFIGMAP_BACKUP_DELIVERED_RUN_IDS_KEY) if isinstance(data, dict) else None
        if raw is None:
            return []
        try:
            run_ids = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("relay ConfigMap has invalid backup delivery state") from None
        if (
            not isinstance(run_ids, list)
            or not all(isinstance(run_id, str) and SAFE_BACKUP_RUN_ID.fullmatch(run_id) for run_id in run_ids)
            or len(set(run_ids)) != len(run_ids)
        ):
            raise ValueError("relay ConfigMap has invalid backup delivery state")
        return run_ids


class PrometheusClient:
    """Read-only Prometheus client limited to active scrape targets."""

    def __init__(self, api_url: str = PROMETHEUS_API_URL, timeout_seconds: int = 10) -> None:
        self._api_url = api_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def active_targets(self) -> list[dict[str, Any]]:
        request = Request(f"{self._api_url}/api/v1/targets", headers={"Accept": "application/json"})
        with urlopen(request, timeout=self._timeout_seconds) as response:
            payload = _decode_object(response.read())
        data = payload.get("data")
        targets = data.get("activeTargets") if isinstance(data, dict) else None
        if not isinstance(targets, list) or not all(isinstance(item, dict) for item in targets):
            raise ValueError("Prometheus returned an invalid target response")
        return targets


def build_status_summary(k8s_client: KubernetesClient, prometheus_client: PrometheusClient) -> str:
    """Return aggregate-only K3s and Prometheus health information."""
    lines = ["[K3s 상태]"]
    reasons: list[str] = []

    try:
        nodes = k8s_client.list_nodes()
        lines.append(_count_line("Node Ready", _count_nodes_ready(nodes), len(nodes)))

        pods_by_namespace = {namespace: k8s_client.list_pods(namespace) for namespace in DEFAULT_NAMESPACES}
        for namespace, pods in pods_by_namespace.items():
            lines.append(_count_line(f"{namespace} Pod Ready", _count_pods_ready(pods), len(pods)))

        deployments = [
            deployment
            for namespace in DEFAULT_NAMESPACES
            for deployment in k8s_client.list_deployments(namespace)
        ]
        lines.append(f"Deployment 미가용: {_count_unavailable_deployments(deployments)}")

        pvcs = [pvc for namespace in DEFAULT_NAMESPACES for pvc in k8s_client.list_pvcs(namespace)]
        lines.append(_count_line("PVC Bound", _count_bound_pvcs(pvcs), len(pvcs)))

        if (
            _count_nodes_ready(nodes) != len(nodes)
            or any(_count_pods_ready(pods) != len(pods) for pods in pods_by_namespace.values())
            or _count_unavailable_deployments(deployments) > 0
            or _count_bound_pvcs(pvcs) != len(pvcs)
        ):
            reasons.append("일부 대상 비정상")
    except (OSError, URLError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        reasons.append("K3s API 조회 실패")

    try:
        targets = prometheus_client.active_targets()
        up_targets = sum(target.get("health") == "up" for target in targets)
        lines.append(_count_line("Prometheus UP", up_targets, len(targets)))
        if up_targets != len(targets):
            reasons.append("일부 대상 비정상")
    except (OSError, URLError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        reasons.append("Prometheus 조회 실패")

    for reason in dict.fromkeys(reasons):
        lines.append(reason)
    return "\n".join(lines)


class RelayService:
    """Authorizes Telegram commands and Alertmanager webhooks without remediation actions."""

    def __init__(
        self,
        *,
        allowed_chat_id: str = "",
        alertmanager_auth_token: str = "",
        k8s_client: KubernetesClient,
        prometheus_client: PrometheusClient,
        offset_store: OffsetStore | None = None,
        alert_state_store: AlertStateStore | None = None,
        backup_delivery_store: BackupDeliveryStore | None = None,
        alert_callback: Callable[[str], bool] | None = None,
    ) -> None:
        self._allowed_chat_id = str(allowed_chat_id)
        self._alertmanager_auth_token = alertmanager_auth_token
        self._k8s_client = k8s_client
        self._prometheus_client = prometheus_client
        self._offset_store = offset_store or MemoryOffsetStore()
        self._alert_state_store = alert_state_store or MemoryAlertStateStore()
        self._backup_delivery_store = backup_delivery_store
        self._alert_state_lock = threading.Lock()
        self._backup_delivery_lock = threading.Lock()
        self._alert_callback = alert_callback
        self._healthy = True

    def handle_update(self, update: dict[str, Any]) -> str | None:
        if self.is_update_processed(update):
            return None

        message = update.get("message")
        reply: str | None = None
        if isinstance(message, dict):
            chat = message.get("chat")
            chat_id = chat.get("id") if isinstance(chat, dict) else None
            text = message.get("text")
            if str(chat_id) == self._allowed_chat_id and text == "/상태":
                reply = build_status_summary(self._k8s_client, self._prometheus_client)

        return self._redact(reply) if reply else None

    def current_offset(self) -> int | None:
        return self._offset_store.load()

    def is_update_processed(self, update: dict[str, Any]) -> bool:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            return False
        saved_offset = self._offset_store.load()
        return saved_offset is not None and update_id < saved_offset

    def acknowledge_update(self, update: dict[str, Any]) -> None:
        update_id = update.get("update_id")
        if not isinstance(update_id, int) or update_id < 0:
            return
        saved_offset = self._offset_store.load()
        next_offset = update_id + 1
        if saved_offset is None or next_offset > saved_offset:
            self._offset_store.save(next_offset)

    def is_healthy(self) -> bool:
        return self._healthy

    def mark_unhealthy(self) -> None:
        self._healthy = False

    def mark_healthy(self) -> None:
        self._healthy = True

    def deliver_backup_report(self, send_message: Callable[[str, str], bool]) -> bool:
        """Send one validated backup report and persist its run ID after delivery."""
        if self._backup_delivery_store is None:
            return True
        with self._backup_delivery_lock:
            report = _read_backup_report(self._k8s_client)
            if report is None or self._backup_delivery_store.contains(report["run_id"]):
                return True
            if not send_message(self._allowed_chat_id, BACKUP_STATUS_MESSAGES[report["status"]]):
                return False
            self._backup_delivery_store.save(report["run_id"])
            return True

    def handle_alert(self, payload: dict[str, Any], authorization: str) -> tuple[int, str]:
        expected_authorization = f"Bearer {self._alertmanager_auth_token}"
        if not self._alertmanager_auth_token or not hmac.compare_digest(authorization, expected_authorization):
            return 401, "unauthorized"

        status = payload.get("status")
        if status not in {"firing", "resolved"}:
            return 400, "unsupported alert status"

        alerts = payload.get("alerts")
        if not isinstance(alerts, list):
            return 400, "invalid alert payload"

        with self._alert_state_lock:
            try:
                deliverable_alerts, state_records = self._new_alerts(status, alerts)
            except Exception as exc:
                self.mark_unhealthy()
                LOGGER.warning(
                    "alert_state_read_failed error_type=%s",
                    type(exc).__name__,
                )
                return 503, "delivery failed"

            if any(isinstance(alert, dict) for alert in alerts) and not deliverable_alerts:
                return 200, "duplicate suppressed"

            reply = self._format_alert(status, deliverable_alerts or alerts)
            if self._alert_callback is not None:
                try:
                    delivered = self._alert_callback(reply)
                except Exception as exc:
                    self.mark_unhealthy()
                    LOGGER.warning(
                        "alert_delivery_failed error_type=%s",
                        type(exc).__name__,
                    )
                    return 503, "delivery failed"
                if not delivered:
                    self.mark_unhealthy()
                    LOGGER.warning("alert_delivery_failed reason=callback_rejected")
                    return 503, "delivery failed"

            try:
                for fingerprint, state in state_records:
                    self._alert_state_store.save(
                        fingerprint,
                        state,
                        time.time() + ALERT_STATE_TTL_SECONDS,
                    )
            except Exception as exc:
                self.mark_unhealthy()
                LOGGER.warning(
                    "alert_state_write_failed error_type=%s",
                    type(exc).__name__,
                )
                return 503, "delivery failed"
            return 200, reply

    def _new_alerts(self, status: str, alerts: list[Any]) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
        deliverable: list[dict[str, Any]] = []
        state_records: list[tuple[str, str]] = []
        now = time.time()
        for alert in alerts:
            if not isinstance(alert, dict):
                continue
            fingerprint = _alert_fingerprint(alert)
            previous = self._alert_state_store.load(fingerprint)
            if previous is not None and previous[0] == status and previous[1] > now:
                continue
            deliverable.append(alert)
            state_records.append((fingerprint, status))
        return deliverable, state_records

    def _format_alert(self, status: str, alerts: list[Any]) -> str:
        heading = "[장애 감지]" if status == "firing" else "[복구 확인]"
        entries: list[str] = []
        for alert in alerts[:MAX_ALERT_ITEMS]:
            if not isinstance(alert, dict):
                continue
            labels = alert.get("labels")
            safe_labels = labels if isinstance(labels, dict) else {}
            alert_name = safe_labels.get("alertname")
            presentation = ALERT_PRESENTATIONS.get(alert_name) if isinstance(alert_name, str) else None
            problem, impact = presentation or ("운영 상태 경고", "상태를 자동으로 확인 중입니다.")
            target = _format_alert_target(safe_labels)
            state = "자동 복구를 확인 중입니다." if status == "firing" else "정상으로 돌아왔습니다."
            lines = [f"문제: {problem}", f"영향: {impact}", f"대상: {target}", f"상태: {state}"]
            entries.append("\n".join(lines))
        if not entries:
            entries.append("문제: 운영 상태 경고\n대상: 확인 대상 없음\n상태: 자동 확인이 필요합니다.")
        suffix = f"\n\n총 {len(alerts)}건" if len(alerts) > len(entries) else ""
        return self._redact(f"{heading}\n" + "\n\n".join(entries) + suffix)

    def _redact(self, value: str | None) -> str:
        if value is None:
            return ""
        redacted = value
        for sensitive_value in (self._alertmanager_auth_token,):
            if sensitive_value:
                redacted = redacted.replace(sensitive_value, "[비공개]")
        return redacted


class TelegramClient:
    """Minimal Telegram Bot API client used only for outbound long polling and replies."""

    def __init__(self, token: str, timeout_seconds: int = 30) -> None:
        self._token = token
        self._timeout_seconds = timeout_seconds

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": 25}
        if offset is not None:
            payload["offset"] = offset
        response = self._post("getUpdates", payload)
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise TelegramPollingError("Telegram getUpdates failed")
        result = response.get("result")
        if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
            raise TelegramPollingError("Telegram getUpdates returned an invalid result")
        return result

    def send_message(self, chat_id: str, text: str) -> bool:
        response = self._post("sendMessage", {"chat_id": chat_id, "text": text})
        return bool(isinstance(response, dict) and response.get("ok"))

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        request = Request(
            f"https://api.telegram.org/bot{self._token}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                return _decode_object(response.read())
        except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
            LOGGER.warning(
                "telegram_http_request_failed method=%s error_type=%s",
                method,
                type(exc).__name__,
            )
            return None


def _format_alert_target(labels: dict[str, Any]) -> str:
    """Return a compact, allow-listed workload target for a Telegram message."""
    values: list[str] = []
    for key in ("namespace", "deployment", "pod", "persistentvolumeclaim", "job", "instance"):
        value = labels.get(key)
        if isinstance(value, str) and value:
            values.append(value)
    return " / ".join(values) if values else "확인 대상 없음"


def handle_http_request(
    relay: RelayService,
    *,
    method: str,
    path: str,
    authorization: str = "",
    content_length: str | None = None,
    body: bytes = b"",
) -> tuple[int, bytes]:
    """Apply the relay's HTTP contract independently from the socket server."""
    if method == "GET" and path == "/healthz":
        return (200, b"ok\n") if relay.is_healthy() else (503, b"unavailable\n")
    if method != "POST" or path != "/alertmanager":
        return 404, b"not found\n"

    invalid_response = _invalid_body_response(content_length)
    if invalid_response is not None:
        return invalid_response
    if len(body) != int(content_length):
        return 400, b"invalid alert payload\n"
    try:
        payload = _decode_object(body)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return 400, b"invalid alert payload\n"
    try:
        status, response = relay.handle_alert(payload, authorization)
    except Exception:
        return 500, b"internal error\n"
    return status, f"{response}\n".encode("utf-8")


def create_http_handler(relay: RelayService) -> type[BaseHTTPRequestHandler]:
    """Create the private HTTP boundary used by the relay's ClusterIP Service."""

    class RelayRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            status, body = handle_http_request(relay, method="GET", path=self.path)
            self._respond(status, body, "text/plain; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/alertmanager":
                status, body = handle_http_request(relay, method="POST", path=self.path)
                self._respond(status, body, "text/plain; charset=utf-8")
                return
            content_length = self.headers.get("Content-Length")
            invalid_response = _invalid_body_response(content_length)
            if invalid_response is not None:
                status, body = invalid_response
                self._respond(status, body, "text/plain; charset=utf-8")
                return
            body_size = int(content_length)
            status, body = handle_http_request(
                relay,
                method="POST",
                path=self.path,
                authorization=self.headers.get("Authorization", ""),
                content_length=content_length,
                body=self.rfile.read(body_size),
            )
            self._respond(status, body, "text/plain; charset=utf-8")

        def log_message(self, format: str, *args: object) -> None:
            return

        def _respond(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return RelayRequestHandler


def _invalid_body_response(content_length: str | None) -> tuple[int, bytes] | None:
    try:
        body_size = int(content_length) if content_length is not None else 0
    except ValueError:
        return 400, b"invalid content length\n"
    if body_size < 1 or body_size > MAX_REQUEST_BODY_BYTES:
        return 400, b"invalid request body\n"
    return None


def run_polling(
    relay: RelayService,
    telegram_client: TelegramClient,
    allowed_chat_id: str,
    *,
    max_cycles: int | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Run outbound Telegram polling; errors remain local and never trigger remediation."""
    consecutive_failures = 0
    cycles = 0
    while True:
        polling_failed = False
        try:
            delivered = _poll_once(relay, telegram_client, allowed_chat_id)
        except Exception as exc:
            LOGGER.warning(
                "telegram_polling_failed error_type=%s consecutive_failures=%d",
                type(exc).__name__,
                consecutive_failures + 1,
            )
            polling_failed = True
            delivered = False
        cycles += 1
        if delivered:
            relay.mark_healthy()
            consecutive_failures = 0
            delay = 1
        else:
            if not polling_failed and consecutive_failures == 0:
                LOGGER.warning("telegram_delivery_failed reason=send_message_rejected")
            relay.mark_unhealthy()
            consecutive_failures += 1
            delay = min(2 ** (consecutive_failures - 1), 30)
        if max_cycles is not None and cycles >= max_cycles:
            return
        sleep_fn(delay)


def _poll_once(relay: RelayService, telegram_client: TelegramClient, allowed_chat_id: str) -> bool:
    offset = relay.current_offset()
    for update in telegram_client.get_updates(offset):
        if relay.is_update_processed(update):
            continue
        reply = relay.handle_update(update)
        if reply is not None and not telegram_client.send_message(allowed_chat_id, reply):
            return False
        relay.acknowledge_update(update)
    return relay.deliver_backup_report(telegram_client.send_message)


def main() -> None:
    from http.server import ThreadingHTTPServer

    token = _read_configured_secret("TELEGRAM_BOT_TOKEN_FILE")
    allowed_chat_id = _read_configured_secret("ALLOWED_CHAT_ID_FILE")
    alertmanager_auth_token = _read_configured_secret("ALERTMANAGER_AUTH_TOKEN_FILE")
    telegram_client = TelegramClient(token)
    k8s_client = KubernetesClient()
    relay = RelayService(
        allowed_chat_id=allowed_chat_id,
        alertmanager_auth_token=alertmanager_auth_token,
        k8s_client=k8s_client,
        prometheus_client=PrometheusClient(),
        offset_store=ConfigMapOffsetStore(
            k8s_client,
            namespace=RELAY_NAMESPACE,
            name=RELAY_STATE_CONFIGMAP,
        ),
        alert_state_store=ConfigMapAlertStateStore(
            k8s_client,
            namespace=RELAY_NAMESPACE,
            name=RELAY_STATE_CONFIGMAP,
        ),
        backup_delivery_store=ConfigMapBackupDeliveryStore(
            k8s_client,
            namespace=RELAY_NAMESPACE,
            name=RELAY_STATE_CONFIGMAP,
        ),
        alert_callback=lambda message: telegram_client.send_message(allowed_chat_id, message),
    )
    polling_thread = threading.Thread(
        target=run_polling,
        args=(relay, telegram_client, allowed_chat_id),
        daemon=True,
    )
    polling_thread.start()
    port = int(os.getenv("RELAY_HTTP_PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), create_http_handler(relay)).serve_forever()


def _count_line(label: str, ready: int, total: int) -> str:
    return f"{label}: {ready}/{total}"


def _count_nodes_ready(nodes: list[dict[str, Any]]) -> int:
    return sum(_has_true_condition(node, "Ready") for node in nodes)


def _count_pods_ready(pods: list[dict[str, Any]]) -> int:
    return sum(_has_true_condition(pod, "Ready") for pod in pods)


def _has_true_condition(resource: dict[str, Any], condition_type: str) -> bool:
    status = resource.get("status")
    conditions = status.get("conditions") if isinstance(status, dict) else None
    if not isinstance(conditions, list):
        return False
    return any(
        isinstance(condition, dict)
        and condition.get("type") == condition_type
        and condition.get("status") == "True"
        for condition in conditions
    )


def _count_unavailable_deployments(deployments: list[dict[str, Any]]) -> int:
    unavailable = 0
    for deployment in deployments:
        spec = deployment.get("spec")
        status = deployment.get("status")
        desired = spec.get("replicas", 1) if isinstance(spec, dict) else 1
        available = status.get("availableReplicas", 0) if isinstance(status, dict) else 0
        if not isinstance(desired, int) or not isinstance(available, int) or available < desired:
            unavailable += 1
    return unavailable


def _count_bound_pvcs(pvcs: list[dict[str, Any]]) -> int:
    return sum(
        isinstance(pvc.get("status"), dict) and pvc["status"].get("phase") == "Bound"
        for pvc in pvcs
    )


def _read_file(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        value = handle.read().strip()
    if not value:
        raise ValueError("configured secret file is empty")
    return value


def _read_configured_secret(environment_name: str) -> str:
    path = os.getenv(environment_name)
    if not path:
        raise RuntimeError(f"{environment_name} must point to a mounted secret file")
    return _read_file(path)


def _decode_object(body: bytes) -> dict[str, Any]:
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("response must be a JSON object")
    return payload


def _read_backup_report(k8s_client: KubernetesClient) -> dict[str, str] | None:
    """Return only a complete, allow-listed backup status report from its fixed ConfigMap."""
    config_map = k8s_client.get_config_map(RELAY_NAMESPACE, BACKUP_STATUS_CONFIGMAP)
    data = config_map.get("data")
    if not isinstance(data, dict) or set(data) != BACKUP_REPORT_KEYS:
        LOGGER.warning("backup_report_ignored reason=invalid_keys")
        return None
    if not all(isinstance(data[key], str) for key in BACKUP_REPORT_KEYS):
        LOGGER.warning("backup_report_ignored reason=invalid_value_type")
        return None
    run_id = data["run_id"]
    status = data["status"]
    completed_at = data["completed_at"]
    stage = data["stage"]
    if not SAFE_BACKUP_RUN_ID.fullmatch(run_id):
        LOGGER.warning("backup_report_ignored reason=invalid_run_id")
        return None
    if status not in BACKUP_STATUS_MESSAGES:
        LOGGER.warning("backup_report_ignored reason=invalid_status")
        return None
    if not _is_utc_timestamp(completed_at):
        LOGGER.warning("backup_report_ignored reason=invalid_completed_at")
        return None
    if not SAFE_BACKUP_STAGE.fullmatch(stage):
        LOGGER.warning("backup_report_ignored reason=invalid_stage")
        return None
    return {"run_id": run_id, "status": status, "completed_at": completed_at, "stage": stage}


def _is_utc_timestamp(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _alert_fingerprint(alert: dict[str, Any]) -> str:
    fingerprint = alert.get("fingerprint")
    if isinstance(fingerprint, str) and fingerprint:
        return fingerprint
    identity = alert.get("labels") if isinstance(alert.get("labels"), dict) else alert
    digest_input = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return f"derived:{hashlib.sha256(digest_input).hexdigest()}"


def _alert_state_key(fingerprint: str) -> str:
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    main()
