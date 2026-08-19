from __future__ import annotations

from typing import Any


ALLOWED_SERVICES = frozenset({"portal-web", "system-agent", "crawler-worker", "youtube-memo", "book-memo", "caddy", "homeops-executor"})
MAX_LOG_BYTES = 32 * 1024


def collect_diagnostics(service: str, client: Any | None = None) -> dict[str, object]:
    container = _get_container(service, client)
    logs = _decode_logs(container.logs(tail=100, timestamps=False))
    return {
        "service": service,
        "container": _container_snapshot(container),
        "logs": logs,
    }


def restart_service(service: str, client: Any | None = None) -> dict[str, object]:
    container = _get_container(service, client)
    container.restart(timeout=10)
    container.reload()
    snapshot = _container_snapshot(container)
    return {"service": service, "status": snapshot["status"], "container": snapshot}


def _get_container(service: str, client: Any | None) -> Any:
    _require_allowed_service(service)
    return (client or _docker_client()).containers.get(service)


def _docker_client() -> Any:
    import docker

    return docker.from_env()


def _require_allowed_service(service: str) -> None:
    if service not in ALLOWED_SERVICES:
        raise ValueError("service_not_allowed")


def _container_snapshot(container: Any) -> dict[str, object]:
    state = container.attrs.get("State", {})
    health = state.get("Health", {})
    return {
        "status": state.get("Status", getattr(container, "status", "unknown")),
        "health": health.get("Status", "none"),
        "exit_code": state.get("ExitCode"),
        "started_at": state.get("StartedAt", ""),
    }


def _decode_logs(raw_logs: bytes) -> list[str]:
    safe_bytes = raw_logs[-MAX_LOG_BYTES:]
    return [line for line in safe_bytes.decode("utf-8", errors="replace").splitlines() if line]
