"""Telegram vehicle-care worker lifecycle."""

from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import signal
from threading import Event
import time
from typing import Protocol

from app.services.hyundai import HyundaiClient, HyundaiFetchResult
from app.services.oauth_callback import HyundaiOAuthCallbackServer
from app.services.store import CarCareStore
from app.services.telegram import CommandHandler, TelegramClient, TelegramUpdate
from app.services.vehicle_monitor import VehicleMonitor


POLL_INTERVAL_SECONDS = 5
HYUNDAI_INTERVAL_SECONDS = 10 * 60
DEFAULT_DATABASE_PATH = "/data/car-care/car-care.sqlite3"


class _TelegramGateway(Protocol):
    def poll(self, offset: int | None = None) -> list[TelegramUpdate]: ...

    def send(self, text: str) -> bool: ...


class _CommandHandler(Protocol):
    def handle_update(self, update: TelegramUpdate) -> str | None: ...


class _HyundaiGateway(Protocol):
    def fetch_snapshot(self) -> HyundaiFetchResult: ...


class _VehicleMonitor(Protocol):
    def observe(self, snapshot: object) -> list[object]: ...

    def acknowledge(self, alert: object) -> None: ...

    def should_notify_hyundai_error(self, today: date) -> bool: ...

    def acknowledge_hyundai_error(self, today: date) -> None: ...


def run_once(
    handler: _CommandHandler,
    telegram: _TelegramGateway,
    hyundai: _HyundaiGateway,
    monitor: _VehicleMonitor,
    offset: int | None = None,
    stopped: Event | None = None,
) -> int | None:
    """Process received commands and one Hyundai vehicle observation."""
    next_offset = _handle_updates(handler, telegram, offset, stopped)
    if stopped is not None and stopped.is_set():
        return next_offset
    _observe_vehicle(telegram, hyundai, monitor)
    return next_offset


def _handle_updates(
    handler: _CommandHandler,
    telegram: _TelegramGateway,
    offset: int | None,
    stopped: Event | None = None,
) -> int | None:
    next_offset = offset
    for update in telegram.poll(offset):
        response = handler.handle_update(update)
        if response and not telegram.send(response):
            break
        if update.update_id is not None:
            candidate = update.update_id + 1
            next_offset = candidate if next_offset is None else max(next_offset, candidate)
        if stopped is not None and stopped.is_set():
            break
    return next_offset


def _observe_vehicle(
    telegram: _TelegramGateway, hyundai: _HyundaiGateway, monitor: _VehicleMonitor
) -> None:
    result = hyundai.fetch_snapshot()
    if result.status == "disabled":
        return
    if result.status == "error":
        today = date.today()
        if monitor.should_notify_hyundai_error(today) and telegram.send(
            "Hyundai 차량 상태 조회 오류: API 연결 또는 응답을 확인하세요."
        ):
            monitor.acknowledge_hyundai_error(today)
        return
    if result.snapshot is None:
        return
    for alert in monitor.observe(result.snapshot):
        if telegram.send(alert.text):
            monitor.acknowledge(alert)


def main() -> None:
    database_path = _database_path()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    store = CarCareStore(database_path)
    store.initialize()

    telegram = TelegramClient.from_environment()
    if telegram is None:
        raise RuntimeError("CAR_CARE_TELEGRAM_BOT_TOKEN and CAR_CARE_TELEGRAM_CHAT_ID are required")

    hyundai = HyundaiClient.from_environment()
    handler = CommandHandler(store, os.environ["CAR_CARE_TELEGRAM_CHAT_ID"], hyundai)
    # Docker forwards N100 loopback 8015 to the container network interface.
    # External exposure remains restricted by the Compose host binding.
    callback_server = HyundaiOAuthCallbackServer(hyundai, host="0.0.0.0")
    callback_server.start()
    monitor = VehicleMonitor(store)
    stopped = Event()
    _install_signal_handlers(stopped)

    offset: int | None = None
    next_hyundai_at = time.monotonic()
    try:
        while not stopped.is_set():
            offset = _handle_updates(handler, telegram, offset, stopped)
            if stopped.is_set():
                break
            if time.monotonic() >= next_hyundai_at:
                _observe_vehicle(telegram, hyundai, monitor)
                next_hyundai_at = time.monotonic() + HYUNDAI_INTERVAL_SECONDS
            stopped.wait(POLL_INTERVAL_SECONDS)
    finally:
        callback_server.stop()


def _database_path() -> Path:
    configured_path = os.getenv("CAR_CARE_DB_PATH", "").strip()
    return Path(configured_path or DEFAULT_DATABASE_PATH)


def _install_signal_handlers(stopped: Event) -> None:
    def stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)


if __name__ == "__main__":
    main()
