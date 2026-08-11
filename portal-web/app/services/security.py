import json
import os
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any
from zoneinfo import ZoneInfo

import fcntl


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
AUTH_RATE_LIMIT_MAX_FAILURES = int(os.getenv("AUTH_RATE_LIMIT_MAX_FAILURES", "5"))
AUTH_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("AUTH_RATE_LIMIT_WINDOW_SECONDS", "300"))
AUTH_RATE_LIMIT_STATE_PATH = Path(
    os.getenv("AUTH_RATE_LIMIT_STATE_PATH", "").strip()
    or LOG_PATH.parent / "auth-rate-limit-state.json"
)
AUTH_SESSION_MAX_ENTRIES = max(1, int(os.getenv("AUTH_SESSION_MAX_ENTRIES", "1024")))

_AUTH_FAILURES: dict[tuple[str, str], list[datetime]] = {}
_AUTH_SESSIONS: dict[str, tuple[str, datetime]] = {}
_AUTH_SESSIONS_LOCK = RLock()
_ALLOWED_USER_EVENTS = {
    "global_search_submitted",
    "search_result_opened",
    "security_modal_closed",
    "security_modal_opened",
    "service_opened",
}


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


def append_user_event(event: str, **details: Any) -> None:
    if event not in _ALLOWED_USER_EVENTS:
        append_security_event("user_event_blocked", reason="event_not_allowed", event=event)
        return

    append_security_event(
        f"user_{event}",
        **{
            key: _clean_detail(value)
            for key, value in details.items()
            if key in {"path", "target", "href", "client", "query"}
        },
    )


def auth_rate_limited(scope: str, identifier: str) -> bool:
    with _auth_rate_limit_lock():
        _reload_auth_failures()
        if _prune_expired_auth_failures():
            _persist_auth_failures()
        return len(_AUTH_FAILURES.get((scope, identifier), [])) >= AUTH_RATE_LIMIT_MAX_FAILURES


def record_auth_failure(scope: str, identifier: str) -> None:
    with _auth_rate_limit_lock():
        _reload_auth_failures()
        _prune_expired_auth_failures()
        key = (scope, identifier)
        failures = _AUTH_FAILURES.get(key, [])
        failures.append(datetime.now(LOG_TIMEZONE))
        _AUTH_FAILURES[key] = failures
        _persist_auth_failures()


def clear_auth_failures(scope: str, identifier: str) -> None:
    with _auth_rate_limit_lock():
        _reload_auth_failures()
        changed = _prune_expired_auth_failures()
        if _AUTH_FAILURES.pop((scope, identifier), None) is not None:
            changed = True
        if changed:
            _persist_auth_failures()


def create_auth_session(scope: str, max_age_seconds: int) -> str:
    with _AUTH_SESSIONS_LOCK:
        _prune_expired_auth_sessions()
        while len(_AUTH_SESSIONS) >= AUTH_SESSION_MAX_ENTRIES:
            oldest_token = min(_AUTH_SESSIONS, key=lambda token: _AUTH_SESSIONS[token][1])
            _AUTH_SESSIONS.pop(oldest_token)

        token = secrets.token_urlsafe(32)
        while token in _AUTH_SESSIONS:
            token = secrets.token_urlsafe(32)
        _AUTH_SESSIONS[token] = (
            scope,
            datetime.now(LOG_TIMEZONE) + timedelta(seconds=max_age_seconds),
        )
    return token


def has_auth_session(scope: str, token: str) -> bool:
    with _AUTH_SESSIONS_LOCK:
        _prune_expired_auth_sessions()
        session = _AUTH_SESSIONS.get(token)
        return bool(session and session[0] == scope)


def is_production_environment() -> bool:
    return os.getenv("APP_ENV", "").strip().lower() == "production"


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


@contextmanager
def _auth_rate_limit_lock():
    lock_path = AUTH_RATE_LIMIT_STATE_PATH.with_name(f".{AUTH_RATE_LIMIT_STATE_PATH.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _reload_auth_failures() -> None:
    _AUTH_FAILURES.clear()
    _AUTH_FAILURES.update(_read_auth_failures())


def _read_auth_failures() -> dict[tuple[str, str], list[datetime]]:
    try:
        raw_state = json.loads(AUTH_RATE_LIMIT_STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}

    if not isinstance(raw_state, dict):
        return {}

    failures: dict[tuple[str, str], list[datetime]] = {}
    for scope, identifiers in raw_state.items():
        if not isinstance(scope, str) or not isinstance(identifiers, dict):
            continue
        for identifier, timestamps in identifiers.items():
            if not isinstance(identifier, str) or not isinstance(timestamps, list):
                continue
            parsed_timestamps = [_parse_auth_failure_timestamp(timestamp) for timestamp in timestamps]
            valid_timestamps = [timestamp for timestamp in parsed_timestamps if timestamp is not None]
            if valid_timestamps:
                failures[(scope, identifier)] = valid_timestamps
    return failures


def _parse_auth_failure_timestamp(timestamp: object) -> datetime | None:
    if not isinstance(timestamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=LOG_TIMEZONE)
    return parsed.astimezone(LOG_TIMEZONE)


def _prune_expired_auth_failures() -> bool:
    cutoff = datetime.now(LOG_TIMEZONE) - timedelta(seconds=AUTH_RATE_LIMIT_WINDOW_SECONDS)
    pruned = {
        key: [failed_at for failed_at in failures if failed_at >= cutoff]
        for key, failures in _AUTH_FAILURES.items()
    }
    active = {key: failures for key, failures in pruned.items() if failures}
    if active != _AUTH_FAILURES:
        _AUTH_FAILURES.clear()
        _AUTH_FAILURES.update(active)
        return True
    return False


def _persist_auth_failures() -> None:
    state: dict[str, dict[str, list[str]]] = {}
    for (scope, identifier), failures in _AUTH_FAILURES.items():
        state.setdefault(scope, {})[identifier] = [failed_at.isoformat() for failed_at in failures]

    temporary_path = AUTH_RATE_LIMIT_STATE_PATH.with_name(
        f".{AUTH_RATE_LIMIT_STATE_PATH.name}.{secrets.token_hex(8)}.tmp"
    )
    try:
        AUTH_RATE_LIMIT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with temporary_path.open("w", encoding="utf-8") as state_file:
            json.dump(state, state_file, ensure_ascii=False, separators=(",", ":"))
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary_path, AUTH_RATE_LIMIT_STATE_PATH)
    finally:
        temporary_path.unlink(missing_ok=True)


def _prune_expired_auth_sessions() -> None:
    now = datetime.now(LOG_TIMEZONE)
    expired_tokens = [
        token
        for token, (_, expires_at) in _AUTH_SESSIONS.items()
        if expires_at < now
    ]
    for token in expired_tokens:
        _AUTH_SESSIONS.pop(token, None)


def _clean_detail(value: Any) -> str:
    cleaned = str(value).replace("\n", " ").replace("\r", " ").strip()
    return cleaned[:300]


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
