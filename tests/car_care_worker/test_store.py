from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import threading
import unittest
from unittest.mock import patch

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

    def test_initialize_again_adds_command_results_without_losing_existing_data(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as db:
            db.executescript(
                "CREATE TABLE maintenance_records ("
                "item TEXT PRIMARY KEY, odometer_km INTEGER, completed_at TEXT NOT NULL);"
                "CREATE TABLE vehicle_snapshots ("
                "id INTEGER PRIMARY KEY CHECK (id = 1), observed_at TEXT NOT NULL, "
                "odometer_km INTEGER NOT NULL, dte_km INTEGER, warnings_json TEXT NOT NULL);"
                "CREATE TABLE alert_states (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                "CREATE TABLE tire_changes ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, tire_type TEXT NOT NULL, "
                "odometer_km INTEGER, completed_at TEXT NOT NULL);"
            )
            db.execute("INSERT INTO maintenance_records VALUES (?, ?, ?)", ("engine_oil", 52340, "2026-08-22"))
            db.execute(
                "INSERT INTO vehicle_snapshots VALUES (?, ?, ?, ?, ?)",
                (1, "2026-08-22T01:30:00+00:00", 52340, 401, '["fuel"]'),
            )
            db.execute("INSERT INTO alert_states VALUES (?, ?)", ("warning:fuel", "active"))
            db.execute(
                "INSERT INTO tire_changes (tire_type, odometer_km, completed_at) VALUES (?, ?, ?)",
                ("winter_tires", 52340, "2026-08-22"),
            )

        legacy_store = CarCareStore(legacy_path)
        with sqlite3.connect(legacy_path) as db:
            self.assertIsNone(
                db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'telegram_command_results'"
                ).fetchone()
            )
            before = {
                table: db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                for table in ("maintenance_records", "vehicle_snapshots", "alert_states", "tire_changes")
            }

        legacy_store.initialize()
        legacy_store.initialize()
        with sqlite3.connect(legacy_path) as db:
            self.assertIsNotNone(
                db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'telegram_command_results'"
                ).fetchone()
            )
            after = {
                table: db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                for table in before
            }

        self.assertEqual(after, before)
        self.assertEqual(
            legacy_store.get_maintenance("engine_oil"),
            MaintenanceRecord("engine_oil", 52340, date(2026, 8, 22)),
        )
        self.assertEqual(legacy_store.get_alert_state("warning:fuel"), "active")

    def test_process_command_reuses_reply_and_runs_callback_once(self) -> None:
        calls = []

        def callback(transaction):
            calls.append(transaction)
            transaction.set_alert_state("command:test", "done")
            return "처리 완료"

        self.assertEqual(self.store.process_command(7, callback), "처리 완료")
        self.assertEqual(self.store.process_command(7, callback), "처리 완료")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.get_alert_state("command:test"), "done")

    def test_process_command_rolls_back_business_change_when_reply_record_fails(self) -> None:
        def callback(transaction):
            transaction.set_alert_state("command:rollback", "changed")
            return "응답"

        with patch.object(self.store, "_insert_command_result", side_effect=RuntimeError("record failed")):
            with self.assertRaises(RuntimeError):
                self.store.process_command(8, callback)

        self.assertIsNone(self.store.get_alert_state("command:rollback"))

    def test_process_command_same_id_from_two_store_instances_runs_one_callback(self) -> None:
        first = CarCareStore(self.path)
        second = CarCareStore(self.path)
        self.assertEqual(first.process_command(9, lambda _tx: "동일 응답"), "동일 응답")
        self.assertEqual(second.process_command(9, lambda _tx: "다른 응답"), "동일 응답")

    def test_process_command_same_id_concurrent_callbacks_are_serialized(self) -> None:
        first = CarCareStore(self.path)
        second = CarCareStore(self.path)
        calls = []
        lock = threading.Lock()

        def callback(transaction):
            with lock:
                calls.append(transaction)
            return "동시 응답"

        results = []
        workers = [
            threading.Thread(target=lambda store=store: results.append(store.process_command(10, callback)))
            for store in (first, second)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)

        self.assertEqual([worker.is_alive() for worker in workers], [False, False])
        self.assertEqual(results, ["동시 응답", "동시 응답"])
        self.assertEqual(len(calls), 1)

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
