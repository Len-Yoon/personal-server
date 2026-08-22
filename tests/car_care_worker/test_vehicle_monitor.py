from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.models import MaintenanceRecord, VehicleSnapshot
from app.services.store import CarCareStore
from app.services.vehicle_monitor import VehicleMonitor


class VehicleMonitorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        path = Path(self.temp_dir.name) / "car-care.sqlite3"
        self.store = CarCareStore(path)
        self.store.initialize()
        self.monitor = VehicleMonitor(self.store)
        self.started_at = datetime(2026, 8, 22, 1, 30, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def snapshot(
        self, odometer_km: int, warnings: frozenset[str], minutes: int = 0, dte_km: int | None = 401
    ) -> VehicleSnapshot:
        return VehicleSnapshot(
            observed_at=self.started_at + timedelta(minutes=minutes),
            odometer_km=odometer_km,
            dte_km=dte_km,
            warnings=warnings,
        )

    def test_warning_is_emitted_only_when_it_becomes_active(self) -> None:
        self.monitor.observe(self.snapshot(52340, frozenset()))

        activated = self.monitor.observe(self.snapshot(52340, frozenset({"tire_pressure"})))
        self.monitor.acknowledge(activated[0])

        self.assertEqual([alert.key for alert in activated], ["warning:tire_pressure"])
        self.assertEqual(
            self.monitor.observe(self.snapshot(52340, frozenset({"tire_pressure"}))), []
        )

    def test_failed_warning_delivery_is_retried_until_the_alert_is_acknowledged(self) -> None:
        self.monitor.observe(self.snapshot(52340, frozenset()))

        failed_delivery = self.monitor.observe(
            self.snapshot(52340, frozenset({"tire_pressure"}))
        )
        retry = self.monitor.observe(
            self.snapshot(52340, frozenset({"tire_pressure"}), minutes=1)
        )
        self.monitor.acknowledge(retry[0])

        self.assertEqual([alert.key for alert in failed_delivery], ["warning:tire_pressure"])
        self.assertEqual([alert.key for alert in retry], ["warning:tire_pressure"])
        self.assertEqual(
            self.monitor.observe(self.snapshot(52340, frozenset({"tire_pressure"}), minutes=2)),
            [],
        )

    def test_warning_reemits_once_after_it_is_cleared_then_reactivates(self) -> None:
        first_alert = self.monitor.observe(self.snapshot(52340, frozenset({"tire_pressure"})))
        self.monitor.acknowledge(first_alert[0])
        self.monitor.observe(self.snapshot(52340, frozenset(), minutes=1))

        reactivated = self.monitor.observe(
            self.snapshot(52340, frozenset({"tire_pressure"}), minutes=2)
        )
        self.monitor.acknowledge(reactivated[0])

        self.assertEqual([alert.key for alert in reactivated], ["warning:tire_pressure"])
        self.assertEqual(
            self.monitor.observe(
                self.snapshot(52340, frozenset({"tire_pressure"}), minutes=3)
            ),
            [],
        )

    def test_due_maintenance_remains_suppressed_after_corrected_odometer(self) -> None:
        self.store.complete_maintenance("engine_oil", 50000, date(2026, 1, 1))

        due = self.monitor.observe(self.snapshot(59000, frozenset()))
        self.monitor.acknowledge(due[0])
        corrected = self.monitor.observe(self.snapshot(58000, frozenset(), minutes=1))
        due_again = self.monitor.observe(self.snapshot(59000, frozenset(), minutes=2))

        self.assertEqual([alert.key for alert in due], ["maintenance:engine_oil"])
        self.assertEqual(corrected, [])
        self.assertEqual(due_again, [])

    def test_completed_maintenance_allows_the_next_due_cycle_to_alert(self) -> None:
        self.store.complete_maintenance("engine_oil", 50000, date(2026, 1, 1))
        due = self.monitor.observe(self.snapshot(59000, frozenset()))
        self.monitor.acknowledge(due[0])
        self.store.complete_maintenance("engine_oil", 59000, date(2026, 8, 22))

        alerts = self.monitor.observe(self.snapshot(68000, frozenset(), minutes=1))

        self.assertEqual([alert.key for alert in alerts], ["maintenance:engine_oil"])

    def test_idle_after_distance_increase_emits_one_trip_summary(self) -> None:
        self.monitor.observe(self.snapshot(52320, frozenset()))
        self.monitor.observe(self.snapshot(52340, frozenset(), minutes=1))

        alerts = self.monitor.observe(self.snapshot(52340, frozenset(), minutes=16))

        self.assertEqual([alert.key for alert in alerts], ["trip:summary"])
        self.assertIn("이번 운행: 20km", alerts[0].text)
        self.assertIn("주행거리: 52,340km", alerts[0].text)
        self.assertIn("주행 가능 거리: 401km", alerts[0].text)
        self.monitor.acknowledge(alerts[0])
        self.assertEqual(
            self.monitor.observe(self.snapshot(52340, frozenset(), minutes=31)), []
        )

    def test_idle_summary_waits_15_minutes_across_five_minute_polls(self) -> None:
        self.monitor.observe(self.snapshot(52320, frozenset()))
        self.monitor.observe(self.snapshot(52340, frozenset(), minutes=1))
        self.assertEqual(self.monitor.observe(self.snapshot(52340, frozenset(), minutes=6)), [])
        self.assertEqual(self.monitor.observe(self.snapshot(52340, frozenset(), minutes=11)), [])

        alerts = self.monitor.observe(self.snapshot(52340, frozenset(), minutes=16))

        self.assertEqual([alert.key for alert in alerts], ["trip:summary"])
        self.monitor.acknowledge(alerts[0])
        self.assertEqual(self.monitor.observe(self.snapshot(52340, frozenset(), minutes=21)), [])

    def test_trip_summary_includes_engine_oil_remaining_distance_when_recorded(self) -> None:
        self.store.complete_maintenance("engine_oil", 50000, date(2026, 8, 1))
        self.monitor.observe(self.snapshot(52320, frozenset()))
        self.monitor.observe(self.snapshot(52340, frozenset(), minutes=1))

        alerts = self.monitor.observe(self.snapshot(52340, frozenset(), minutes=16))

        self.assertIn("엔진오일 잔여: 7,660km", alerts[0].text)


if __name__ == "__main__":
    unittest.main()
