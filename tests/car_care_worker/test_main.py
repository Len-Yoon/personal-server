import os
from pathlib import Path
from threading import Event
import unittest
from unittest.mock import patch

from app.main import DEFAULT_DATABASE_PATH, _database_path, run_once
from app.services.telegram import TelegramUpdate
from app.services.maintenance import Alert


class _TelegramFake:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def poll(self, _offset=None) -> list[TelegramUpdate]:
        return [TelegramUpdate("123", "/차량", update_id=7)]

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return True


class _HandlerFake:
    def handle_update(self, update: TelegramUpdate) -> str | None:
        return "[차량 상태]" if update.text == "/차량" else None


class _HyundaiFake:
    def fetch_snapshot(self) -> object:
        return object()


class _MonitorFake:
    def __init__(self) -> None:
        self.snapshots: list[object] = []

    def observe(self, _snapshot: object) -> list[Alert]:
        self.snapshots.append(_snapshot)
        return [Alert("trip", "trip:summary", "[운행 결과]")]


class _StoppingHandler(_HandlerFake):
    def __init__(self, stopped: Event) -> None:
        self._stopped = stopped

    def handle_update(self, update: TelegramUpdate) -> str | None:
        self._stopped.set()
        return super().handle_update(update)


class RunOnceTests(unittest.TestCase):
    def test_run_once_sends_command_response_and_monitor_alerts(self) -> None:
        telegram = _TelegramFake()

        run_once(_HandlerFake(), telegram, _HyundaiFake(), _MonitorFake())

        self.assertEqual(telegram.sent, ["[차량 상태]", "[운행 결과]"])

    def test_run_once_skips_vehicle_observation_after_stop_requested_by_update(self) -> None:
        stopped = Event()
        telegram = _TelegramFake()
        monitor = _MonitorFake()

        run_once(_StoppingHandler(stopped), telegram, _HyundaiFake(), monitor, stopped=stopped)

        self.assertTrue(stopped.is_set())
        self.assertEqual(telegram.sent, ["[차량 상태]"])
        self.assertEqual(monitor.snapshots, [])

    def test_blank_database_path_uses_the_car_care_default(self) -> None:
        with patch.dict(os.environ, {"CAR_CARE_DB_PATH": ""}, clear=True):
            database_path = _database_path()

        self.assertEqual(database_path, Path(DEFAULT_DATABASE_PATH))


if __name__ == "__main__":
    unittest.main()
