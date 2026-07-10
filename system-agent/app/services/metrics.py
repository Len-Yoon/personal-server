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
STATUS_ORDER = {
    "ok": 0,
    "warning": 1,
    "critical": 2,
}


def collect_metrics(
    data_root: Path | None = None,
    host_metrics_path: Path | None = None,
    stale_after_seconds: int | None = None,
    backup_stale_after_seconds: int | None = None,
) -> dict[str, Any]:
    data_root = data_root or Path(os.getenv("DATA_ROOT", PROJECT_DATA_ROOT))
    host_metrics_path = host_metrics_path or Path(
        os.getenv("HOST_METRICS_PATH", DEFAULT_HOST_METRICS_PATH)
    )
    stale_after_seconds = stale_after_seconds or int(os.getenv("HOST_METRICS_STALE_SECONDS", "900"))
    backup_stale_after_seconds = backup_stale_after_seconds or int(
        os.getenv("BACKUP_STALE_SECONDS", "172800")
    )

    warnings: list[str] = []
    host = _read_host_metrics(host_metrics_path, stale_after_seconds, warnings)
    files = _files_status(data_root / "files")
    backup = _backup_status(data_root / "backups", warnings, backup_stale_after_seconds)
    disk = _disk_status(data_root, warnings)

    status_checks = [
        {
            "key": "host",
            "label": "호스트 수집",
            "status": host["status"],
            "detail": _host_status_detail(host),
        },
        {
            "key": "backup",
            "label": "백업",
            "status": backup["status"],
            "detail": _backup_status_detail(backup),
        },
        {
            "key": "disk",
            "label": "디스크",
            "status": disk["status"],
            "detail": _disk_status_detail(disk),
        },
        {
            "key": "files",
            "label": "파일함",
            "status": "ok",
            "detail": f"{files['file_count']}개, {files['total_bytes']} bytes",
        },
    ]
    overall_status = _highest_status(check["status"] for check in status_checks)

    return {
        "demo_mode": False,
        "overall_status": overall_status,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "host": host,
        "disk": disk,
        "files": files,
        "backup": backup,
        "containers": _container_status(),
        "status_checks": status_checks,
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
            "status": "ok",
            "status_reason": "backup_recent",
        },
        "containers": [
            {"name": "portal-web", "status": "running"},
            {"name": "youtube-memo", "status": "running"},
            {"name": "book-memo", "status": "running"},
            {"name": "system-agent", "status": "running"},
        ],
        "status_checks": [
            {"key": "host", "label": "호스트 수집", "status": "ok", "detail": "정상"},
            {"key": "backup", "label": "백업", "status": "ok", "detail": "최근 백업"},
            {"key": "disk", "label": "디스크", "status": "ok", "detail": "사용률 61.0%"},
            {"key": "files", "label": "파일함", "status": "ok", "detail": "12개"},
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
            "status": "warning",
            "status_reason": "host_metrics_missing",
            "cpu_percent": None,
            "memory_percent": None,
            "disk_percent": None,
            "uptime_seconds": None,
        }

    try:
        payload = json.loads(host_metrics_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        warnings.append("host_metrics_invalid")
        return {
            "source": "invalid",
            "status": "warning",
            "status_reason": "host_metrics_invalid",
            "cpu_percent": None,
            "memory_percent": None,
            "disk_percent": None,
            "uptime_seconds": None,
        }

    captured_at = _parse_datetime(str(payload.get("captured_at", "")))
    if not captured_at:
        warnings.append("host_metrics_invalid_timestamp")
        status = "warning"
        status_reason = "host_metrics_invalid_timestamp"
    elif (datetime.now(timezone.utc) - captured_at).total_seconds() > stale_after_seconds:
        warnings.append("host_metrics_stale")
        status = "warning"
        status_reason = "host_metrics_stale"
    else:
        status = "ok"
        status_reason = "host_metrics_ok"

    return {
        "source": "windows",
        "captured_at": payload.get("captured_at", ""),
        "status": status,
        "status_reason": status_reason,
        "cpu_percent": payload.get("cpu_percent"),
        "memory_percent": payload.get("memory_percent"),
        "disk_percent": payload.get("disk_percent"),
        "uptime_seconds": payload.get("uptime_seconds"),
    }


def _backup_status(
    backup_root: Path,
    warnings: list[str],
    stale_after_seconds: int,
) -> dict[str, Any]:
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
            "status": "warning",
            "status_reason": "backup_missing",
        }

    latest = backups[0]
    latest_modified_at = datetime.fromtimestamp(
        latest.stat().st_mtime,
        timezone.utc,
    )
    if (datetime.now(timezone.utc) - latest_modified_at).total_seconds() > stale_after_seconds:
        warnings.append("backup_stale")
        status = "warning"
        status_reason = "backup_stale"
    else:
        status = "ok"
        status_reason = "backup_recent"

    return {
        "exists": True,
        "latest_name": latest.name,
        "latest_modified_at": latest_modified_at.isoformat(),
        "status": status,
        "status_reason": status_reason,
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
    level = disk_level(percent)
    if level == "warning":
        warnings.append("disk_usage_warning")
    elif level == "critical":
        warnings.append("disk_usage_critical")
    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "percent": percent,
        "level": level,
        "status": "ok" if level == "ok" else level,
        "status_reason": "disk_usage_ok" if level == "ok" else f"disk_usage_{level}",
    }


def disk_level(percent: float) -> str:
    if percent >= 90:
        return "critical"
    if percent >= 80:
        return "warning"
    return "ok"


def _highest_status(statuses: Any) -> str:
    winner = "ok"
    for status in statuses:
        if STATUS_ORDER.get(str(status), -1) > STATUS_ORDER.get(winner, -1):
            winner = str(status)
    return winner


def _host_status_detail(host: dict[str, Any]) -> str:
    if host.get("status_reason") == "host_metrics_missing":
        return "메트릭 없음"
    if host.get("status_reason") == "host_metrics_invalid":
        return "형식 오류"
    if host.get("status_reason") == "host_metrics_invalid_timestamp":
        return "시각 오류"
    if host.get("status_reason") == "host_metrics_stale":
        return "오래됨"
    return "정상"


def _backup_status_detail(backup: dict[str, Any]) -> str:
    if backup.get("status_reason") == "backup_missing":
        return "백업 없음"
    if backup.get("status_reason") == "backup_stale":
        return "오래됨"
    return "정상"


def _disk_status_detail(disk: dict[str, Any]) -> str:
    level = disk.get("level", "ok")
    if level == "critical":
        return "매우 높음"
    if level == "warning":
        return "높음"
    return "정상"


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
