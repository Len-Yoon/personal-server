from __future__ import annotations

from typing import Any


ALLOWED_SERVICES = frozenset({"portal-web", "system-agent", "crawler-worker", "youtube-memo", "book-memo", "caddy", "homeops-executor"})
MAX_LOG_BYTES = 32 * 1024


def collect_diagnostics(
    service: str,
    client: Any | None = None,
    include_runtime_evidence: bool = True,
) -> dict[str, object]:
    container = _get_container(service, client)
    logs = _decode_logs(container.logs(tail=100, timestamps=False)) if include_runtime_evidence else []
    return {
        "service": service,
        "container": _container_snapshot(container, include_runtime_evidence=include_runtime_evidence),
        "logs": logs,
    }


def collect_all_diagnostics(client: Any | None = None) -> list[dict[str, object]]:
    diagnostics: list[dict[str, object]] = []
    for service in sorted(ALLOWED_SERVICES):
        try:
            diagnostics.append(
                collect_diagnostics(service, client=client, include_runtime_evidence=False)
            )
        except ValueError as exc:
            diagnostics.append(_unavailable_diagnostics(service, str(exc)))
        except Exception:
            diagnostics.append(_unavailable_diagnostics(service, "diagnostic_unavailable"))
    return diagnostics


def restart_service(service: str, client: Any | None = None) -> dict[str, object]:
    container = _get_container(service, client)
    container.restart(timeout=10)
    container.reload()
    snapshot = _container_snapshot(container)
    return {"service": service, "status": snapshot["status"], "container": snapshot}


def restart_all_services(client: Any | None = None) -> list[dict[str, object]]:
    services = sorted(ALLOWED_SERVICES - {"portal-web", "caddy", "homeops-executor"}) + ["portal-web", "caddy", "homeops-executor"]
    results: list[dict[str, object]] = []
    for service in services:
        try:
            results.append(restart_service(service, client=client))
        except Exception as exc:
            results.append({"service": service, "status": "failed", "error": str(exc)})
    return results


def _get_container(service: str, client: Any | None) -> Any:
    _require_allowed_service(service)
    containers = (client or _docker_client()).containers.list(
        all=True,
        filters={"label": f"com.docker.compose.service={service}"},
    )
    if len(containers) != 1:
        raise ValueError("service_container_not_found")
    return containers[0]


def _unavailable_diagnostics(service: str, error: str) -> dict[str, object]:
    return {
        "service": service,
        "container": {"status": "unknown", "health": "unknown"},
        "logs": [],
        "error": error,
    }


def _docker_client() -> Any:
    import docker

    return docker.from_env()


def _require_allowed_service(service: str) -> None:
    if service not in ALLOWED_SERVICES:
        raise ValueError("service_not_allowed")


def _container_snapshot(container: Any, include_runtime_evidence: bool = True) -> dict[str, object]:
    state = container.attrs.get("State", {})
    health = state.get("Health", {})
    stats = (
        container.stats(stream=False)
        if include_runtime_evidence and state.get("Status") == "running"
        else {}
    )
    memory = stats.get("memory_stats", {})
    memory_limit = memory.get("limit") or 0
    memory_percent = round((memory.get("usage", 0) / memory_limit) * 100, 1) if memory_limit else 0.0
    cpu_percent = _cpu_percent(stats)
    return {
        "status": state.get("Status", getattr(container, "status", "unknown")),
        "health": health.get("Status", "none"),
        "exit_code": state.get("ExitCode"),
        "started_at": state.get("StartedAt", ""),
        "cpu_percent": cpu_percent,
        "memory_percent": memory_percent,
    }


def _cpu_percent(stats: dict[str, Any]) -> float:
    cpu_stats = stats.get("cpu_stats", {})
    previous_cpu_stats = stats.get("precpu_stats", {})
    cpu_delta = (cpu_stats.get("cpu_usage", {}).get("total_usage") or 0) - (previous_cpu_stats.get("cpu_usage", {}).get("total_usage") or 0)
    system_delta = (cpu_stats.get("system_cpu_usage") or 0) - (previous_cpu_stats.get("system_cpu_usage") or 0)
    online_cpus = len(cpu_stats.get("cpu_usage", {}).get("percpu_usage") or []) or cpu_stats.get("online_cpus") or 1
    return round((cpu_delta / system_delta) * online_cpus * 100, 1) if cpu_delta > 0 and system_delta > 0 else 0.0


def _decode_logs(raw_logs: bytes) -> list[str]:
    safe_bytes = raw_logs[-MAX_LOG_BYTES:]
    return [line for line in safe_bytes.decode("utf-8", errors="replace").splitlines() if line]
