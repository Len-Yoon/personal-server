import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from app.models import MaintenanceRecord, VehicleSnapshot


class CarCareStore:
    _MAINTENANCE_ITEMS = frozenset({"engine_oil", "transmission_oil"})

    def __init__(self, path: Path) -> None:
        self._path = path

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS maintenance_records ("
                "item TEXT PRIMARY KEY, odometer_km INTEGER, completed_at TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS vehicle_snapshots ("
                "id INTEGER PRIMARY KEY CHECK (id = 1), observed_at TEXT NOT NULL, "
                "odometer_km INTEGER NOT NULL, dte_km INTEGER, warnings_json TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS alert_states ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            )

    def complete_maintenance(
        self, item: str, odometer_km: int | None, completed_at: date
    ) -> None:
        self._validate_item(item)
        self._validate_odometer(odometer_km)
        with self._connect() as db:
            db.execute(
                "INSERT INTO maintenance_records (item, odometer_km, completed_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(item) DO UPDATE SET "
                "odometer_km = excluded.odometer_km, completed_at = excluded.completed_at",
                (item, odometer_km, completed_at.isoformat()),
            )

    def get_maintenance(self, item: str) -> MaintenanceRecord | None:
        self._validate_item(item)
        with self._connect() as db:
            row = db.execute(
                "SELECT item, odometer_km, completed_at FROM maintenance_records WHERE item = ?",
                (item,),
            ).fetchone()
        if row is None:
            return None
        return MaintenanceRecord(row[0], row[1], date.fromisoformat(row[2]))

    def save_snapshot(self, snapshot: VehicleSnapshot) -> None:
        self._validate_odometer(snapshot.odometer_km)
        observed_at = snapshot.observed_at.astimezone(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                "INSERT INTO vehicle_snapshots "
                "(id, observed_at, odometer_km, dte_km, warnings_json) "
                "VALUES (1, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET observed_at = excluded.observed_at, "
                "odometer_km = excluded.odometer_km, dte_km = excluded.dte_km, "
                "warnings_json = excluded.warnings_json",
                (
                    observed_at,
                    snapshot.odometer_km,
                    snapshot.dte_km,
                    json.dumps(sorted(snapshot.warnings)),
                ),
            )

    def load_last_snapshot(self) -> VehicleSnapshot | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT observed_at, odometer_km, dte_km, warnings_json "
                "FROM vehicle_snapshots WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        return VehicleSnapshot(
            observed_at=datetime.fromisoformat(row[0]).astimezone(timezone.utc),
            odometer_km=row[1],
            dte_km=row[2],
            warnings=frozenset(json.loads(row[3])),
        )

    def get_alert_state(self, key: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT value FROM alert_states WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]

    def set_alert_state(self, key: str, value: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO alert_states (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def _validate_item(self, item: str) -> None:
        if item not in self._MAINTENANCE_ITEMS:
            raise ValueError(f"Unsupported maintenance item: {item}")

    @staticmethod
    def _validate_odometer(odometer_km: int | None) -> None:
        if odometer_km is not None and odometer_km < 0:
            raise ValueError("Odometer value cannot be negative")
