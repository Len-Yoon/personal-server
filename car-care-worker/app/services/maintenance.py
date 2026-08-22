from dataclasses import dataclass
from datetime import date
from typing import Mapping

from app.models import MaintenanceRecord


@dataclass(frozen=True)
class Alert:
    kind: str
    key: str
    text: str


@dataclass(frozen=True)
class MaintenanceRule:
    interval_km: int
    interval_months: int | None
    warning_km: int
    warning_days: int | None


MAINTENANCE_RULES = {
    "engine_oil": MaintenanceRule(10_000, 12, 1_000, 30),
    "transmission_oil": MaintenanceRule(60_000, None, 5_000, None),
}


def evaluate_maintenance(
    current_odometer_km: int,
    today: date,
    records: Mapping[str, MaintenanceRecord | None],
) -> list[Alert]:
    alerts: list[Alert] = []
    for item, rule in MAINTENANCE_RULES.items():
        record = records.get(item)
        if record is None:
            continue
        remaining_km = (
            None
            if record.odometer_km is None
            else rule.interval_km - (current_odometer_km - record.odometer_km)
        )
        due_by_distance = remaining_km is not None and remaining_km <= rule.warning_km
        remaining_days = _remaining_days(today, record.completed_at, rule.interval_months)
        due_by_date = (
            remaining_days is not None
            and rule.warning_days is not None
            and remaining_days <= rule.warning_days
        )
        if not due_by_distance and not due_by_date:
            continue
        detail = _alert_detail(remaining_km, remaining_days, due_by_distance, due_by_date)
        alerts.append(
            Alert(
                kind="maintenance",
                key=f"maintenance:{item}",
                text=f"{_item_name(item)} 정비 알림: {detail}",
            )
        )
    return alerts


def _remaining_days(today: date, completed_at: date, interval_months: int | None) -> int | None:
    if interval_months is None:
        return None
    due_year = completed_at.year + (completed_at.month - 1 + interval_months) // 12
    due_month = (completed_at.month - 1 + interval_months) % 12 + 1
    due_day = min(completed_at.day, _days_in_month(due_year, due_month))
    return (date(due_year, due_month, due_day) - today).days


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return (date(year + 1, 1, 1) - date(year, month, 1)).days
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def _alert_detail(
    remaining_km: int | None,
    remaining_days: int | None,
    due_by_distance: bool,
    due_by_date: bool,
) -> str:
    details: list[str] = []
    if due_by_distance and remaining_km is not None:
        details.append(f"{remaining_km:,}km 남음")
    if due_by_date and remaining_days is not None:
        details.append(f"{remaining_days}일 남음")
    return ", ".join(details)


def _item_name(item: str) -> str:
    return {"engine_oil": "엔진오일", "transmission_oil": "미션오일"}[item]
