import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json


PROJECT_DATA_ROOT = next(
    (
        parent / "data"
        for parent in Path(__file__).resolve().parents
        if (parent / "docker-compose.yml").exists()
    ),
    Path("/data"),
)
DEFAULT_HOST_METRICS_PATH = PROJECT_DATA_ROOT / "system" / "host-metrics.json"


def collect_metrics(
    data_root: Path | None = None,
    host_metrics_path: Path | None = None,
    stale_after_seconds: int | None = None,
) -> dict[str, Any]:
    data_root = data_root or Path(os.getenv("DATA_ROOT", PROJECT_DATA_ROOT))
    host_metrics_path = host_metrics_path or Path(
        os.getenv("HOST_METRICS_PATH", DEFAULT_HOST_METRICS_PATH)
    )
    stale_after_seconds = stale_after_seconds or int(os.getenv("HOST_METRICS_STALE_SECONDS", "300"))

    warnings: list[str] = []
    host = _read_host_metrics(host_metrics_path, stale_after_seconds, warnings)
    files = _files_status(data_root / "files")
    backup = _backup_status(data_root / "backups", warnings)
    disk = _disk_status(data_root, warnings)

    overall_status = "warning" if warnings else "ok"

    return {
        "demo_mode": False,
        "overall_status": overall_status,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "host": host,
        "disk": disk,
        "files": files,
        "backup": backup,
        "containers": _container_status(),
        "warnings": warnings,
    }


def demo_metrics() -> dict[str, Any]:
    return {
        "demo_mode": True,
        "overall_status": "ok",
        "captured_at": "2026-07-01T09:00:00+00:00",
        "host": {
            "source": "demo",
            "cpu_percent": 18.0,
            "memory_percent": 43.0,
            "disk_percent": 61.0,
            "uptime_seconds": 274200,
        },
        "disk": {
            "total_bytes": 512 * 1024 * 1024 * 1024,
            "used_bytes": 312 * 1024 * 1024 * 1024,
            "percent": 61.0,
        },
        "files": {
            "file_count": 12,
            "total_bytes": 734003200,
        },
        "backup": {
            "exists": True,
            "latest_name": "20260701-030000",
            "latest_modified_at": "2026-07-01T03:00:00+09:00",
        },
        "containers": [
            {"name": "portal-web", "status": "running"},
            {"name": "youtube-memo", "status": "running"},
            {"name": "book-memo", "status": "running"},
            {"name": "system-agent", "status": "running"},
        ],
        "warnings": [],
    }


def _read_host_metrics(
    host_metrics_path: Path,
    stale_after_seconds: int,
    warnings: list[str],
) -> dict[str, Any]:
    if not host_metrics_path.exists():
        warnings.append("host_metrics_missing")
        return {
            "source": "missing",
            "cpu_percent": None,
            "memory_percent": None,
            "disk_percent": None,
            "uptime_seconds": None,
        }

    try:
        payload = json.loads(host_metrics_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        warnings.append("host_metrics_invalid")
        return {
            "source": "invalid",
            "cpu_percent": None,
            "memory_percent": None,
            "disk_percent": None,
            "uptime_seconds": None,
        }

    captured_at = _parse_datetime(str(payload.get("captured_at", "")))
    if not captured_at:
        warnings.append("host_metrics_invalid_timestamp")
    elif (datetime.now(timezone.utc) - captured_at).total_seconds() > stale_after_seconds:
        warnings.append("host_metrics_stale")

    return {
        "source": "windows",
        "captured_at": payload.get("captured_at", ""),
        "cpu_percent": payload.get("cpu_percent"),
        "memory_percent": payload.get("memory_percent"),
        "disk_percent": payload.get("disk_percent"),
        "uptime_seconds": payload.get("uptime_seconds"),
    }


def _backup_status(backup_root: Path, warnings: list[str]) -> dict[str, Any]:
    backups = sorted(
        [path for path in backup_root.iterdir() if path.is_dir()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ) if backup_root.exists() else []

    if not backups:
        warnings.append("backup_missing")
        return {
            "exists": False,
            "latest_name": "",
            "latest_modified_at": "",
        }

    latest = backups[0]
    return {
        "exists": True,
        "latest_name": latest.name,
        "latest_modified_at": datetime.fromtimestamp(
            latest.stat().st_mtime,
            timezone.utc,
        ).isoformat(),
    }


def _files_status(files_root: Path) -> dict[str, Any]:
    total_bytes = 0
    file_count = 0

    if files_root.exists():
        for path in files_root.rglob("*"):
            if path.is_file():
                file_count += 1
                total_bytes += path.stat().st_size

    return {
        "file_count": file_count,
        "total_bytes": total_bytes,
    }


def _disk_status(data_root: Path, warnings: list[str]) -> dict[str, Any]:
    target = data_root if data_root.exists() else data_root.parent
    usage = shutil.disk_usage(target)
    percent = round((usage.used / usage.total) * 100, 1) if usage.total else 0.0
    if percent >= 85:
        warnings.append("disk_usage_high")
    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "percent": percent,
    }


def _container_status() -> list[dict[str, str]]:
    raw = os.getenv("EXPECTED_CONTAINERS", "portal-web,crawler-worker,youtube-memo,book-memo,system-agent")
    return [
        {
            "name": name.strip(),
            "status": "unknown",
        }
        for name in raw.split(",")
        if name.strip()
    ]


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
