import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from app.models import MaintenanceRecord, TireChangeRecord, VehicleSnapshot


ReplyT = TypeVar("ReplyT")


class CarCareStore:
    _MAINTENANCE_ITEMS = frozenset({"engine_oil", "transmission_oil", "fuel_filter"})

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
                "CREATE TABLE IF NOT EXISTS tire_changes ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, tire_type TEXT NOT NULL, "
                "odometer_km INTEGER, completed_at TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS telegram_command_results ("
                "update_id INTEGER PRIMARY KEY, reply_text TEXT NOT NULL, "
                "processed_at TEXT NOT NULL);"
            )

    def process_command(self, update_id: int, callback: Callable[["_TransactionStore"], ReplyT]) -> ReplyT:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT reply_text FROM telegram_command_results WHERE update_id = ?",
                (update_id,),
            ).fetchone()
            if row is not None:
                return row[0]
            reply = callback(_TransactionStore(self, db))
            self._insert_command_result(db, update_id, reply)
            return reply

    def _insert_command_result(self, db: sqlite3.Connection, update_id: int, reply_text: str) -> None:
        db.execute(
            "INSERT INTO telegram_command_results (update_id, reply_text, processed_at) VALUES (?, ?, ?)",
            (update_id, reply_text, datetime.now(timezone.utc).isoformat()),
        )

    def complete_maintenance(
        self, item: str, odometer_km: int | None, completed_at: date, *, _connection=None
    ) -> None:
        self._validate_item(item)
        self._validate_odometer(odometer_km)
        with self._connection_scope(_connection) as db:
            db.execute(
                "INSERT INTO maintenance_records (item, odometer_km, completed_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(item) DO UPDATE SET "
                "odometer_km = excluded.odometer_km, completed_at = excluded.completed_at",
                (item, odometer_km, completed_at.isoformat()),
            )
            db.execute(
                "INSERT INTO alert_states (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (f"maintenance:{item}", "inactive"),
            )

    def get_maintenance(self, item: str, *, _connection=None) -> MaintenanceRecord | None:
        self._validate_item(item)
        with self._connection_scope(_connection) as db:
            row = db.execute(
                "SELECT item, odometer_km, completed_at FROM maintenance_records WHERE item = ?",
                (item,),
            ).fetchone()
        if row is None:
            return None
        return MaintenanceRecord(row[0], row[1], date.fromisoformat(row[2]))

    def record_tire_change(
        self, tire_type: str, odometer_km: int | None, completed_at: date, *, _connection=None
    ) -> None:
        if tire_type not in {"winter_tires", "all_season_tires"}:
            raise ValueError(f"Unsupported tire type: {tire_type}")
        self._validate_odometer(odometer_km)
        with self._connection_scope(_connection) as db:
            db.execute(
                "INSERT INTO tire_changes (tire_type, odometer_km, completed_at) VALUES (?, ?, ?)",
                (tire_type, odometer_km, completed_at.isoformat()),
            )

    def get_latest_tire_change(self, *, _connection=None) -> TireChangeRecord | None:
        with self._connection_scope(_connection) as db:
            row = db.execute(
                "SELECT tire_type, odometer_km, completed_at FROM tire_changes "
                "ORDER BY completed_at DESC, id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return TireChangeRecord(row[0], row[1], date.fromisoformat(row[2]))

    def save_snapshot(self, snapshot: VehicleSnapshot, *, _connection=None) -> None:
        self._validate_odometer(snapshot.odometer_km)
        if snapshot.observed_at.tzinfo is None or snapshot.observed_at.utcoffset() is None:
            raise ValueError("Observed timestamp must include a timezone")
        observed_at = snapshot.observed_at.astimezone(timezone.utc).isoformat()
        with self._connection_scope(_connection) as db:
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

    def load_last_snapshot(self, *, _connection=None) -> VehicleSnapshot | None:
        with self._connection_scope(_connection) as db:
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

    def get_alert_state(self, key: str, *, _connection=None) -> str | None:
        with self._connection_scope(_connection) as db:
            row = db.execute("SELECT value FROM alert_states WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]

    def set_alert_state(self, key: str, value: str, *, _connection=None) -> None:
        with self._connection_scope(_connection) as db:
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

    @contextmanager
    def _connection_scope(self, connection: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
        if connection is not None:
            yield connection
            return
        with self._connect() as db:
            yield db


class _TransactionStore:
    def __init__(self, owner: CarCareStore, connection: sqlite3.Connection) -> None:
        self._owner = owner
        self._connection = connection

    def complete_maintenance(self, item: str, odometer_km: int | None, completed_at: date) -> None:
        self._owner.complete_maintenance(item, odometer_km, completed_at, _connection=self._connection)

    def get_maintenance(self, item: str) -> MaintenanceRecord | None:
        return self._owner.get_maintenance(item, _connection=self._connection)

    def record_tire_change(self, tire_type: str, odometer_km: int | None, completed_at: date) -> None:
        self._owner.record_tire_change(tire_type, odometer_km, completed_at, _connection=self._connection)

    def get_latest_tire_change(self) -> TireChangeRecord | None:
        return self._owner.get_latest_tire_change(_connection=self._connection)

    def save_snapshot(self, snapshot: VehicleSnapshot) -> None:
        self._owner.save_snapshot(snapshot, _connection=self._connection)

    def load_last_snapshot(self) -> VehicleSnapshot | None:
        return self._owner.load_last_snapshot(_connection=self._connection)

    def get_alert_state(self, key: str) -> str | None:
        return self._owner.get_alert_state(key, _connection=self._connection)

    def set_alert_state(self, key: str, value: str) -> None:
        self._owner.set_alert_state(key, value, _connection=self._connection)
