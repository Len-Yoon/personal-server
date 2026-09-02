"""Internal-only Telegram relay for K3s status and Alertmanager notifications."""

from __future__ import annotations

import hmac
import json
import os
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable, Protocol
from urllib.error import URLError
from urllib.request import Request, urlopen


KUBERNETES_API_URL = "https://kubernetes.default.svc"
PROMETHEUS_API_URL = "http://prometheus-operated.monitoring.svc:9090"
SERVICE_ACCOUNT_DIRECTORY = "/var/run/secrets/kubernetes.io/serviceaccount"
DEFAULT_NAMESPACES = ("monitoring", "personal-server")
RELAY_NAMESPACE = "monitoring"
RELAY_STATE_CONFIGMAP = "sre-telegram-relay-state"
MAX_ALERT_ITEMS = 4
MAX_REQUEST_BODY_BYTES = 1_048_576
CONFIGMAP_OFFSET_KEY = "telegram_next_update_id"


class TelegramPollingError(RuntimeError):
    """Telegram did not confirm a successful getUpdates response."""


class OffsetStore(Protocol):
    """Stores the next Telegram update ID to consume across restarts."""

    def load(self) -> int | None: ...

    def save(self, offset: int) -> None: ...


class MemoryOffsetStore:
    """Small state holder used when a persistent ConfigMap-backed store is injected later."""

    def __init__(self, offset: int | None = None) -> None:
        self._offset = offset

    def load(self) -> int | None:
        return self._offset

    def save(self, offset: int) -> None:
        self._offset = offset


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
        alert_callback: Callable[[str], bool] | None = None,
    ) -> None:
        self._allowed_chat_id = str(allowed_chat_id)
        self._alertmanager_auth_token = alertmanager_auth_token
        self._k8s_client = k8s_client
        self._prometheus_client = prometheus_client
        self._offset_store = offset_store or MemoryOffsetStore()
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

        reply = self._format_alert(status, alerts)
        if self._alert_callback is not None:
            try:
                delivered = self._alert_callback(reply)
            except Exception:
                return 503, "delivery failed"
            if not delivered:
                return 503, "delivery failed"
        return 200, reply

    def _format_alert(self, status: str, alerts: list[Any]) -> str:
        heading = "[K3s 경고 발생]" if status == "firing" else "[K3s 경고 복구]"
        names: list[str] = []
        for alert in alerts[:MAX_ALERT_ITEMS]:
            if not isinstance(alert, dict):
                continue
            labels = alert.get("labels")
            alert_name = labels.get("alertname") if isinstance(labels, dict) else None
            if isinstance(alert_name, str) and alert_name:
                names.append(alert_name)
        detail = ", ".join(names) if names else "상세 항목 없음"
        return self._redact(f"{heading}\n{detail}")

    def _redact(self, value: str | None) -> str:
        if value is None:
            return ""
        redacted = value
        for sensitive_value in (self._allowed_chat_id, self._alertmanager_auth_token):
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
        except (OSError, URLError, ValueError, json.JSONDecodeError):
            return None


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
        try:
            delivered = _poll_once(relay, telegram_client, allowed_chat_id)
        except Exception:
            delivered = False
        cycles += 1
        if delivered:
            relay.mark_healthy()
            consecutive_failures = 0
            delay = 1
        else:
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
    return True


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


if __name__ == "__main__":
    main()
