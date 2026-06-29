import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PROJECT_DATA_ROOT = next(
    (
        parent / "data"
        for parent in Path(__file__).resolve().parents
        if (parent / "docker-compose.yml").exists()
    ),
    Path("/app/data"),
)
LOG_PATH = Path(os.getenv("SECURITY_LOG_PATH", PROJECT_DATA_ROOT / "logs" / "security-events.txt"))
LOG_TIMEZONE = ZoneInfo(os.getenv("SECURITY_LOG_TIMEZONE", "Asia/Seoul"))


SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def append_security_event(event: str, **details: Any) -> None:
    now = datetime.now(LOG_TIMEZONE)
    log_path = _daily_log_path(now)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "event": event,
        "details": details,
    }
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def read_recent_events(limit: int = 8) -> list[dict[str, Any]]:
    events = []
    for log_path in _list_daily_logs():
        lines = log_path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines[-200:]):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(events) >= limit:
                return events
    return events


def security_status() -> dict[str, Any]:
    today_log_path = _daily_log_path(datetime.now(LOG_TIMEZONE))
    return {
        "log_path": str(today_log_path),
        "log_files": [str(path) for path in _list_daily_logs(limit=7)],
        "recent_events": read_recent_events(),
        "headers": list(SECURITY_HEADERS.keys()),
        "file_policy": {
            "max_upload_mb": int(os.getenv("FILE_MAX_UPLOAD_MB", "50")),
            "blocked_extensions": sorted(_blocked_extensions()),
            "allowed_extensions": sorted(_allowed_extensions()),
        },
    }


def _allowed_extensions() -> set[str]:
    raw = os.getenv("FILE_ALLOWED_EXTENSIONS", "").strip()
    if not raw:
        return set()
    return {extension.strip().lower().lstrip(".") for extension in raw.split(",") if extension.strip()}


def _blocked_extensions() -> set[str]:
    raw = os.getenv(
        "FILE_BLOCKED_EXTENSIONS",
        "app,bat,cmd,com,dll,dmg,exe,jar,js,msi,php,ps1,sh,vbs",
    )
    return {extension.strip().lower().lstrip(".") for extension in raw.split(",") if extension.strip()}


def _daily_log_path(target: datetime) -> Path:
    suffix = target.strftime("%Y-%m-%d")
    if LOG_PATH.suffix:
        return LOG_PATH.with_name(f"{LOG_PATH.stem}-{suffix}{LOG_PATH.suffix}")
    return LOG_PATH / f"security-events-{suffix}.txt"


def _list_daily_logs(limit: int | None = None) -> list[Path]:
    if not LOG_PATH.parent.exists():
        return []

    if LOG_PATH.suffix:
        pattern = f"{LOG_PATH.stem}-*{LOG_PATH.suffix}"
    else:
        pattern = "security-events-*.txt"

    logs = sorted(LOG_PATH.parent.glob(pattern), reverse=True)
    return logs[:limit] if limit else logs
