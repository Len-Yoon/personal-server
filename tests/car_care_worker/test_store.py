from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.models import MaintenanceRecord, VehicleSnapshot
from app.services.store import CarCareStore


class CarCareStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "car-care.sqlite3"
        self.store = CarCareStore(self.path)
        self.store.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_completion_persists_engine_oil_distance_and_date(self) -> None:
        self.store.set_alert_state("maintenance:engine_oil", "active")

        self.store.complete_maintenance("engine_oil", 52340, date(2026, 8, 22))

        self.assertEqual(
            self.store.get_maintenance("engine_oil"),
            MaintenanceRecord("engine_oil", 52340, date(2026, 8, 22)),
        )
        self.assertEqual(self.store.get_alert_state("maintenance:engine_oil"), "inactive")

    def test_snapshot_and_alert_state_survive_new_store_instance(self) -> None:
        snapshot = VehicleSnapshot(
            observed_at=datetime(2026, 8, 22, 1, 30, tzinfo=timezone.utc),
            odometer_km=52340,
            dte_km=401,
            warnings=frozenset({"fuel", "tire_pressure"}),
        )
        self.store.save_snapshot(snapshot)
        self.store.set_alert_state("warning:fuel", "active")

        restored = CarCareStore(self.path)
        self.assertEqual(restored.load_last_snapshot(), snapshot)
        self.assertEqual(restored.get_alert_state("warning:fuel"), "active")

    def test_missing_maintenance_and_alert_state_return_none(self) -> None:
        self.assertIsNone(self.store.get_maintenance("engine_oil"))
        self.assertIsNone(self.store.get_alert_state("warning:fuel"))

    def test_rejects_negative_odometer_and_unsupported_item(self) -> None:
        with self.assertRaises(ValueError):
            self.store.complete_maintenance("brakes", 100, date(2026, 8, 22))
        with self.assertRaises(ValueError):
            self.store.complete_maintenance("engine_oil", -1, date(2026, 8, 22))
        with self.assertRaises(ValueError):
            self.store.save_snapshot(
                VehicleSnapshot(
                    observed_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
                    odometer_km=-1,
                    dte_km=None,
                    warnings=frozenset(),
                )
            )

    def test_rejects_snapshot_with_timezone_naive_observed_at(self) -> None:
        with self.assertRaises(ValueError):
            self.store.save_snapshot(
                VehicleSnapshot(
                    observed_at=datetime(2026, 8, 22, 1, 30),
                    odometer_km=52340,
                    dte_km=401,
                    warnings=frozenset(),
                )
            )


if __name__ == "__main__":
    unittest.main()
