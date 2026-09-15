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


def build_admin_status_context(
    system_status: dict[str, Any],
    service_health: list[dict[str, Any]],
    security: dict[str, Any],
) -> dict[str, Any]:
    warnings = system_status.get("warnings") or []
    return {
        "system_status": system_status,
        "service_health": service_health,
        "security_status": security,
        "has_warnings": bool(warnings),
    }
