from datetime import date, datetime, timezone
import os
from pathlib import Path
from threading import Event
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.main import DEFAULT_DATABASE_PATH, HYUNDAI_INTERVAL_SECONDS, _database_path, run_once
from app.models import VehicleSnapshot
from app.services.hyundai import HyundaiFetchResult
from app.services.store import CarCareStore
from app.services.telegram import CommandHandler, TelegramUpdate
from app.services.maintenance import Alert
from app.services.vehicle_monitor import VehicleMonitor


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
    def fetch_snapshot(self) -> HyundaiFetchResult:
        return HyundaiFetchResult.success(
            VehicleSnapshot(
                observed_at=datetime.now(timezone.utc),
                odometer_km=52340,
                dte_km=401,
                warnings=frozenset(),
            )
        )


class _MonitorFake:
    def __init__(self) -> None:
        self.snapshots: list[object] = []

    def observe(self, _snapshot: object) -> list[Alert]:
        self.snapshots.append(_snapshot)
        return [Alert("trip", "trip:summary", "[운행 결과]")]

    def observe_seasonal_reminders(self, _today: date) -> list[Alert]:
        return []

    def acknowledge(self, _alert: Alert) -> None:
        return None

    def should_notify_hyundai_error(self, _today: date) -> bool:
        return True

    def acknowledge_hyundai_error(self, _today: date) -> None:
        return None


class _StoppingHandler(_HandlerFake):
    def __init__(self, stopped: Event) -> None:
        self._stopped = stopped

    def handle_update(self, update: TelegramUpdate) -> str | None:
        self._stopped.set()
        return super().handle_update(update)


class RunOnceTests(unittest.TestCase):
    def test_run_once_sends_seasonal_alert_without_waiting_for_hyundai_data(self) -> None:
        class SeasonalMonitor(_MonitorFake):
            def observe_seasonal_reminders(self, _today: date) -> list[Alert]:
                return [Alert("seasonal", "seasonal:winter_tires:2026", "[타이어 교체]")]

        telegram = _TelegramFake()
        monitor = SeasonalMonitor()

        run_once(_HandlerFake(), telegram, _HyundaiFake(), monitor)

        self.assertEqual(telegram.sent, ["[차량 상태]", "[타이어 교체]", "[운행 결과]"])

    def test_hyundai_vehicle_observation_runs_every_ten_minutes(self) -> None:
        self.assertEqual(HYUNDAI_INTERVAL_SECONDS, 10 * 60)

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

    def test_failed_alert_delivery_is_retried_before_alert_state_is_acknowledged(self) -> None:
        class EmptyTelegram:
            def __init__(self) -> None:
                self.attempts: list[str] = []

            def poll(self, _offset=None) -> list[TelegramUpdate]:
                return []

            def send(self, text: str) -> bool:
                self.attempts.append(text)
                return len(self.attempts) > 1

        class RetryMonitor(_MonitorFake):
            def __init__(self) -> None:
                super().__init__()
                self.acknowledged = False

            def observe(self, _snapshot: object) -> list[Alert]:
                return [] if self.acknowledged else [Alert("warning", "warning:fuel", "연료 경고")]

            def acknowledge(self, _alert: Alert) -> None:
                self.acknowledged = True

        telegram = EmptyTelegram()
        monitor = RetryMonitor()

        run_once(_HandlerFake(), telegram, _HyundaiFake(), monitor)
        run_once(_HandlerFake(), telegram, _HyundaiFake(), monitor)

        self.assertEqual(telegram.attempts, ["연료 경고", "연료 경고"])
        self.assertTrue(monitor.acknowledged)

    def test_hyundai_error_is_sent_once_per_day_after_successful_delivery(self) -> None:
        class EmptyTelegram:
            def __init__(self) -> None:
                self.sent: list[str] = []

            def poll(self, _offset=None) -> list[TelegramUpdate]:
                return []

            def send(self, text: str) -> bool:
                self.sent.append(text)
                return True

        class ErrorHyundai:
            def fetch_snapshot(self) -> HyundaiFetchResult:
                return HyundaiFetchResult.failure("request")

        with TemporaryDirectory() as directory:
            store = CarCareStore(Path(directory) / "car-care.sqlite3")
            store.initialize()
            monitor = VehicleMonitor(store)
            telegram = EmptyTelegram()

            run_once(_HandlerFake(), telegram, ErrorHyundai(), monitor)
            run_once(_HandlerFake(), telegram, ErrorHyundai(), monitor)

        self.assertEqual(
            telegram.sent.count("Hyundai 차량 상태 조회 오류: API 연결 또는 응답을 확인하세요."),
            1,
        )

    def test_failed_manual_odometer_response_keeps_update_pending_for_retry(self) -> None:
        class RetryingTelegram:
            def __init__(self) -> None:
                self.should_send = False
                self.attempts: list[str] = []

            def poll(self, _offset=None) -> list[TelegramUpdate]:
                return [TelegramUpdate("123", "/주행거리 59000", update_id=7)]

            def send(self, text: str) -> bool:
                self.attempts.append(text)
                return self.should_send

        class DisabledHyundai:
            def fetch_snapshot(self) -> HyundaiFetchResult:
                return HyundaiFetchResult.disabled()

        with TemporaryDirectory() as directory:
            store = CarCareStore(Path(directory) / "car-care.sqlite3")
            store.initialize()
            store.complete_maintenance("engine_oil", 50000, date.today())
            store.complete_maintenance("transmission_oil", 0, date.today())
            handler = CommandHandler(store, allowed_chat_id="123")
            telegram = RetryingTelegram()

            failed_offset = run_once(handler, telegram, DisabledHyundai(), _MonitorFake())
            telegram.should_send = True
            successful_offset = run_once(handler, telegram, DisabledHyundai(), _MonitorFake())

            self.assertIsNone(failed_offset)
            self.assertEqual(store.get_alert_state("maintenance:engine_oil"), "inactive")
            self.assertEqual(successful_offset, 8)
            self.assertEqual(len(telegram.attempts), 2)


if __name__ == "__main__":
    unittest.main()
