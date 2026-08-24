from datetime import date, datetime

from app.models import VehicleSnapshot
from app.services.maintenance import Alert, MAINTENANCE_RULES, evaluate_maintenance
from app.services.store import CarCareStore


SUPPORTED_WARNINGS = {"engine_oil", "brake_oil", "tire_pressure", "washer_fluid", "fuel"}
DTE_ALERT_THRESHOLDS = (100, 50)


class VehicleMonitor:
    def __init__(self, store: CarCareStore) -> None:
        self._store = store

    def observe(self, snapshot: VehicleSnapshot) -> list[Alert]:
        previous = self._store.load_last_snapshot()
        alerts = self._observe_warnings(snapshot)
        alerts.extend(self._observe_dte(snapshot))
        alerts.extend(self._observe_maintenance(snapshot))
        alerts.extend(self._observe_trip(snapshot, previous))
        self._store.save_snapshot(snapshot)
        return alerts

    def _observe_warnings(self, snapshot: VehicleSnapshot) -> list[Alert]:
        alerts: list[Alert] = []
        for warning in sorted(SUPPORTED_WARNINGS):
            key = f"warning:{warning}"
            active = warning in snapshot.warnings
            if active and self._store.get_alert_state(key) != "active":
                alerts.append(Alert("warning", key, f"경고등 점등: {_warning_name(warning)}"))
            if not active and self._store.get_alert_state(key) == "active":
                alerts.append(Alert("warning", f"{key}:cleared", f"경고등 해제: {_warning_name(warning)}"))
                self._store.set_alert_state(key, "recovery_pending")
            elif not active and self._store.get_alert_state(key) == "recovery_pending":
                alerts.append(Alert("warning", f"{key}:cleared", f"경고등 해제: {_warning_name(warning)}"))
            elif not active:
                self._store.set_alert_state(key, "inactive")
        return alerts

    def _observe_dte(self, snapshot: VehicleSnapshot) -> list[Alert]:
        if snapshot.dte_km is None:
            return []
        breached_thresholds = [
            threshold_km for threshold_km in DTE_ALERT_THRESHOLDS if snapshot.dte_km <= threshold_km
        ]
        if not breached_thresholds:
            for threshold_km in DTE_ALERT_THRESHOLDS:
                self._store.set_alert_state(f"dte:{threshold_km}", "armed")
            return []
        urgent_threshold_km = min(breached_thresholds)
        for threshold_km in DTE_ALERT_THRESHOLDS:
            key = f"dte:{threshold_km}"
            if threshold_km != urgent_threshold_km and snapshot.dte_km <= threshold_km:
                self._store.set_alert_state(key, "notified")
        key = f"dte:{urgent_threshold_km}"
        if self._store.get_alert_state(key) == "notified":
            return []
        return [Alert("dte", key, f"주유 필요: 주행 가능 거리 {snapshot.dte_km:,}km")]

    def _observe_maintenance(self, snapshot: VehicleSnapshot) -> list[Alert]:
        records = {item: self._store.get_maintenance(item) for item in MAINTENANCE_RULES}
        due_alerts = evaluate_maintenance(snapshot.odometer_km, snapshot.observed_at.date(), records)
        alerts: list[Alert] = []
        for alert in due_alerts:
            if self._store.get_alert_state(alert.key) != "active":
                alerts.append(alert)
        return alerts

    def acknowledge(self, alert: Alert) -> None:
        if alert.kind == "warning" and alert.key.endswith(":cleared"):
            self._store.set_alert_state(alert.key.removesuffix(":cleared"), "inactive")
        elif alert.kind in {"warning", "maintenance", "seasonal"}:
            self._store.set_alert_state(alert.key, "active")
        elif alert.kind == "dte":
            self._store.set_alert_state(alert.key, "notified")
        elif alert.kind == "trip":
            self._store.set_alert_state("trip:status", "emitted")

    def should_notify_hyundai_error(self, today: date) -> bool:
        return self._store.get_alert_state("hyundai:error_notified_on") != today.isoformat()

    def acknowledge_hyundai_error(self, today: date) -> None:
        self._store.set_alert_state("hyundai:error_notified_on", today.isoformat())

    def observe_seasonal_reminders(self, today: date) -> list[Alert]:
        reminders = (
            ("winter_tires", today >= date(today.year, 11, 15), "윈터타이어로 교체할 시기입니다."),
            (
                "all_season_tires",
                date(today.year, 4, 1) <= today < date(today.year, 11, 15),
                "사계절타이어로 교체할 시기입니다.",
            ),
        )
        alerts: list[Alert] = []
        for name, is_due, message in reminders:
            key = f"seasonal:{name}:{today.year}"
            if is_due and self._store.get_alert_state(key) != "active":
                alerts.append(Alert("seasonal", key, f"계절 타이어 알림: {message}"))
        return alerts

    def _observe_trip(
        self, snapshot: VehicleSnapshot, previous: VehicleSnapshot | None
    ) -> list[Alert]:
        if previous is None:
            return []
        if snapshot.odometer_km > previous.odometer_km:
            self._begin_or_continue_trip(previous.odometer_km, snapshot.observed_at)
            return []
        last_movement_at = self._last_movement_at()
        if (
            snapshot.odometer_km == previous.odometer_km
            and self._store.get_alert_state("trip:status") == "pending"
            and last_movement_at is not None
            and (snapshot.observed_at - last_movement_at).total_seconds() >= 15 * 60
        ):
            return [self._complete_trip(snapshot)]
        return []

    def _begin_or_continue_trip(self, previous_odometer_km: int, observed_at: datetime) -> None:
        if self._store.get_alert_state("trip:status") != "pending":
            self._store.set_alert_state("trip:start_odometer", str(previous_odometer_km))
        self._store.set_alert_state("trip:status", "pending")
        self._store.set_alert_state("trip:last_movement_at", observed_at.isoformat())

    def _last_movement_at(self) -> datetime | None:
        value = self._store.get_alert_state("trip:last_movement_at")
        return None if value is None else datetime.fromisoformat(value)

    def _complete_trip(self, snapshot: VehicleSnapshot) -> Alert:
        start_odometer = int(self._store.get_alert_state("trip:start_odometer") or snapshot.odometer_km)
        trip_distance = snapshot.odometer_km - start_odometer
        details = [
            f"이번 운행: {trip_distance:,}km",
            f"주행거리: {snapshot.odometer_km:,}km",
        ]
        if snapshot.dte_km is not None:
            details.append(f"주행 가능 거리: {snapshot.dte_km:,}km")
        engine_oil = self._store.get_maintenance("engine_oil")
        if engine_oil is not None and engine_oil.odometer_km is not None:
            remaining_km = 10_000 - (snapshot.odometer_km - engine_oil.odometer_km)
            details.append(f"엔진오일 잔여: {remaining_km:,}km")
        return Alert("trip", "trip:summary", " / ".join(details))


def _warning_name(warning: str) -> str:
    return {
        "engine_oil": "엔진오일",
        "brake_oil": "브레이크 오일",
        "tire_pressure": "타이어 공기압",
        "washer_fluid": "워셔액",
        "fuel": "연료",
    }[warning]
