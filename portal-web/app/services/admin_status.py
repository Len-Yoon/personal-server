from datetime import datetime, timezone
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
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SEOUL_TIMEZONE)

    return parsed.astimezone(SEOUL_TIMEZONE).strftime("%Y-%m-%d %H:%M")


def format_operation_history_for_display(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **operation,
            "created_at": format_status_checked_at(str(operation.get("created_at") or "")),
        }
        for operation in history
    ]


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
