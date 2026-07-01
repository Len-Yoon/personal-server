import os
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen
import json


DEFAULT_AGENT_URL = "http://system-agent:8010"
SERVICE_HEALTH_TARGETS = [
    {"name": "뉴스 허브", "url": "http://crawler-worker:8001/health"},
    {"name": "유튜브 메모", "url": "http://youtube-memo:8002/health"},
    {"name": "책 메모", "url": "http://book-memo:8003/health"},
    {"name": "시스템 상태", "url": "http://system-agent:8010/health"},
]


def get_dashboard_status(agent_url: str | None = None, timeout: float = 1.5) -> dict[str, Any]:
    if _truthy(os.getenv("DEMO_MODE", "")):
        return _demo_status()

    agent_url = agent_url or os.getenv("SYSTEM_AGENT_URL", DEFAULT_AGENT_URL)

    try:
        with urlopen(f"{agent_url.rstrip('/')}/metrics", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError):
        return _unavailable_status()


def _demo_status() -> dict[str, Any]:
    return {
        "demo_mode": True,
        "overall_status": "ok",
        "host": {
            "source": "demo",
            "cpu_percent": 18.0,
            "memory_percent": 43.0,
            "disk_percent": 61.0,
            "uptime_seconds": 274200,
        },
        "disk": {"percent": 61.0, "level": "ok"},
        "files": {"file_count": 12, "total_bytes": 734003200},
        "backup": {"exists": True, "latest_name": "20260701-030000"},
        "containers": [
            {"name": "portal-web", "status": "running"},
            {"name": "youtube-memo", "status": "running"},
            {"name": "book-memo", "status": "running"},
            {"name": "system-agent", "status": "running"},
        ],
        "warnings": [],
    }


def get_service_health(timeout: float = 1.0) -> list[dict[str, Any]]:
    if _truthy(os.getenv("DEMO_MODE", "")):
        return [
            {
                "name": target["name"],
                "status": "ok",
                "url": target["url"],
                "demo_mode": True,
            }
            for target in SERVICE_HEALTH_TARGETS
        ]

    return [_check_service(target, timeout) for target in SERVICE_HEALTH_TARGETS]


def _check_service(target: dict[str, str], timeout: float) -> dict[str, Any]:
    try:
        with urlopen(target["url"], timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError):
        return {
            "name": target["name"],
            "status": "unavailable",
            "url": target["url"],
        }
    return {
        "name": target["name"],
        "status": payload.get("status", "unknown"),
        "url": target["url"],
    }


def _unavailable_status() -> dict[str, Any]:
    return {
        "demo_mode": False,
        "overall_status": "unavailable",
        "host": {
            "source": "unavailable",
            "cpu_percent": None,
            "memory_percent": None,
            "disk_percent": None,
            "uptime_seconds": None,
        },
        "disk": {"percent": None, "level": "unknown"},
        "files": {"file_count": 0, "total_bytes": 0},
        "backup": {"exists": False, "latest_name": ""},
        "containers": [],
        "warnings": ["system_agent_unavailable"],
    }


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}
