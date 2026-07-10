from datetime import datetime, timezone
from typing import Any


def format_status_checked_at(value: str) -> str:
    raw_value = value.strip()
    if not raw_value:
        return "unknown"

    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return raw_value

    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


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
