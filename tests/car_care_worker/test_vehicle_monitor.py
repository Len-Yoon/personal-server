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

        self.assertEqual([alert.key for alert in activated], ["warning:tire_pressure"])
        self.assertEqual(
            self.monitor.observe(self.snapshot(52340, frozenset({"tire_pressure"}))), []
        )

    def test_idle_after_distance_increase_emits_one_trip_summary(self) -> None:
        self.monitor.observe(self.snapshot(52320, frozenset()))
        self.monitor.observe(self.snapshot(52340, frozenset(), minutes=1))

        alerts = self.monitor.observe(self.snapshot(52340, frozenset(), minutes=16))

        self.assertEqual([alert.key for alert in alerts], ["trip:summary"])
        self.assertIn("이번 운행: 20km", alerts[0].text)
        self.assertIn("주행거리: 52,340km", alerts[0].text)
        self.assertIn("주행 가능 거리: 401km", alerts[0].text)
        self.assertEqual(
            self.monitor.observe(self.snapshot(52340, frozenset(), minutes=31)), []
        )

    def test_trip_summary_includes_engine_oil_remaining_distance_when_recorded(self) -> None:
        self.store.complete_maintenance("engine_oil", 50000, date(2026, 8, 1))
        self.monitor.observe(self.snapshot(52320, frozenset()))
        self.monitor.observe(self.snapshot(52340, frozenset(), minutes=1))

        alerts = self.monitor.observe(self.snapshot(52340, frozenset(), minutes=16))

        self.assertIn("엔진오일 잔여: 7,660km", alerts[0].text)


if __name__ == "__main__":
    unittest.main()
