from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo


SEOUL_TIMEZONE = ZoneInfo("Asia/Seoul")


def format_status_checked_at(value: str) -> str:
    raw_value = value.strip()
    if not raw_value:
        return "unknown"

    try:
        if raw_value.endswith(" KST"):
            parsed = datetime.fromisoformat(raw_value.removesuffix(" KST")).replace(tzinfo=SEOUL_TIMEZONE)
        else:
            try:
                parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            except ValueError:
                parsed = parsedate_to_datetime(raw_value)
    except (IndexError, TypeError, ValueError):
        return "unknown"

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(SEOUL_TIMEZONE).strftime("%Y-%m-%d %H:%M")


def format_operation_history_for_display(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **operation,
            "created_at": format_status_checked_at(str(operation.get("created_at") or "")),
        }
        for operation in history
    ]


def format_recovery_events_for_display(events: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Keep the bridge's five permitted fields and convert timestamps for the UI."""
    fields = ("timestamp", "component", "event", "status", "action")
    formatted: list[dict[str, str]] = []
    for event in events[:10]:
        if not isinstance(event, dict) or not all(isinstance(event.get(field), str) for field in fields):
            continue
        formatted.append(
            {
                "timestamp": format_status_checked_at(event["timestamp"]),
                "component": event["component"],
                "event": event["event"],
                "status": event["status"],
                "action": event["action"],
            }
        )
    return formatted


def backup_status_for_display(backup: Any, now: datetime | None = None, expected_scope: str = "portal") -> dict[str, str]:
    """Display validated proof timestamps and allowlisted operation details."""
    result = {"label": "확인 필요 · 유효한 복원 증적 없음", "backup_at": "미제공",
              "restore_at": "미제공", "expires_at": "미제공"}
    if not isinstance(backup, dict) or backup.get("scope") != expected_scope:
        return result
    now = now or datetime.now(timezone.utc)
    if expected_scope == "car-care-db-only":
        result.update(_car_backup_operation_for_display(backup, now))
    fields = ("backup_completed_at", "restore_verified_at", "evidence_expires_at")
    times = []
    formatted = {}
    for field, display in zip(fields, ("backup_at", "restore_at", "expires_at")):
        raw = backup.get(field)
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00")) if isinstance(raw, str) else None
            if parsed is None or parsed.tzinfo is None:
                return result
            formatted[display] = parsed.astimezone(SEOUL_TIMEZONE).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OverflowError):
            return result
        times.append(parsed)
    completed, restored, expires = times
    ordered = restored <= completed <= now if expected_scope == "car-care-db-only" else completed <= restored <= now
    if not ordered or expires <= restored:
        return result
    if expected_scope == "car-care-db-only":
        try:
            if expires != restored + timedelta(days=8):
                return result
        except OverflowError:
            return result
    result.update(formatted)
    if expires <= now:
        result["label"] = "확인 필요 · 복원 증적 만료"
    elif backup.get("verified") is True and backup.get("status_reason") == "backup_verified":
        result["label"] = "정상 · 백업 및 복원 검증 완료"
    return result


def _car_backup_operation_for_display(backup: dict[str, Any], now: datetime) -> dict[str, str]:
    """Keep attempts separate from proof and do not echo arbitrary Relay values."""
    result: dict[str, str] = {}
    if "last_attempt_at" in backup or "last_attempt_status" in backup:
        result.update(attempt_at="확인 필요", attempt_status="확인 필요")
        try:
            raw = backup.get("last_attempt_at")
            attempt = datetime.fromisoformat(raw.replace("Z", "+00:00")) if isinstance(raw, str) else None
            if attempt is not None and attempt.tzinfo is not None and attempt <= now:
                result["attempt_at"] = attempt.astimezone(SEOUL_TIMEZONE).strftime("%Y-%m-%d %H:%M")
                status = backup.get("last_attempt_status")
                labels = {"skipped_unchanged": "변경 없음", "success": "완료", "failure": "실패",
                          "uncertain": "확인 필요", "retention_failed": "백업 완료 · 정리 실패"}
                if isinstance(status, str):
                    result["attempt_status"] = labels.get(status, "확인 필요")
        except (ValueError, OverflowError):
            pass
    if "retention_status" in backup or "retention_deleted_count" in backup:
        result.update(retention_status="확인 필요", retention_deleted_count="확인 필요")
        status = backup.get("retention_status")
        labels = {"success": "정상", "no_op": "대상 없음", "failure": "확인 필요",
                  "uncertain": "확인 필요", "not_run": "미실행"}
        if isinstance(status, str):
            result["retention_status"] = labels.get(status, "확인 필요")
        count = backup.get("retention_deleted_count")
        if type(count) is int and 0 <= count <= 10_000:
            result["retention_deleted_count"] = str(count)
    return result


def build_admin_status_context(
    system_status: dict[str, Any],
    service_health: list[dict[str, Any]],
    security: dict[str, Any],
    service_backups: dict[str, Any] | None = None,
) -> dict[str, Any]:
    warnings = system_status.get("warnings") or []
    return {
        "system_status": system_status,
        "backup_status": backup_status_for_display(system_status.get("backup")),
        "service_backups": [
            {"name": name, **backup_status_for_display((service_backups or {}).get(key), expected_scope=scope)}
            for key, name, scope in [("book", "Book", "book-memo"), ("youtube", "YouTube", "youtube-memo"), ("crawler", "Crawler", "crawler-worker"), ("car-care", "차량 DB", "car-care-db-only")]
        ],
        "service_health": service_health,
        "security_status": security,
        "has_warnings": bool(warnings),
    }
