from typing import Any


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
