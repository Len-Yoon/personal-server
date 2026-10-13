from datetime import datetime, timezone
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


def backup_status_for_display(backup: Any, now: datetime | None = None) -> dict[str, str]:
    """Display only the existing Portal evidence; never infer success from archives."""
    result = {"label": "확인 필요 · 유효한 복원 증적 없음", "backup_at": "미제공",
              "restore_at": "미제공", "expires_at": "미제공"}
    if not isinstance(backup, dict) or backup.get("scope") != "portal":
        return result
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
    now = now or datetime.now(timezone.utc)
    completed, restored, expires = times
    if not completed <= restored <= now or expires <= restored:
        return result
    result.update(formatted)
    if expires <= now:
        result["label"] = "확인 필요 · 복원 증적 만료"
    elif backup.get("verified") is True and backup.get("status_reason") == "backup_verified":
        result["label"] = "정상 · 백업 및 복원 검증 완료"
    return result


def build_admin_status_context(
    system_status: dict[str, Any],
    service_health: list[dict[str, Any]],
    security: dict[str, Any],
) -> dict[str, Any]:
    warnings = system_status.get("warnings") or []
    return {
        "system_status": system_status,
        "backup_status": backup_status_for_display(system_status.get("backup")),
        "service_health": service_health,
        "security_status": security,
        "has_warnings": bool(warnings),
    }
