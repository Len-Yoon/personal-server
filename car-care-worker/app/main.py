"""Telegram vehicle-care worker lifecycle."""

from __future__ import annotations

from datetime import date, datetime
import os
from pathlib import Path
import signal
from threading import Event
import time
import uuid
from typing import Protocol
from zoneinfo import ZoneInfo

from app.services.hyundai import HyundaiClient, HyundaiFetchResult
from app.services.oauth_callback import HyundaiOAuthCallbackServer
from app.services.store import CarCareStore
from app.services.telegram import CommandHandler, TelegramClient, TelegramUpdate
from app.services.vehicle_monitor import VehicleMonitor
from app.services.ha_backend import runtime
from contextlib import nullcontext


POLL_INTERVAL_SECONDS = 5
HYUNDAI_INTERVAL_SECONDS = 10 * 60
DEFAULT_DATABASE_PATH = "/data/car-care/car-care.sqlite3"
KOREA_TIMEZONE = ZoneInfo("Asia/Seoul")


class _TelegramGateway(Protocol):
    def poll(self, offset: int | None = None) -> list[TelegramUpdate]: ...

    def send(self, text: str) -> bool: ...


class _CommandHandler(Protocol):
    def handle_update(self, update: TelegramUpdate) -> str | None: ...


class _HyundaiGateway(Protocol):
    def fetch_snapshot(self) -> HyundaiFetchResult: ...


class _VehicleMonitor(Protocol):
    def observe(self, snapshot: object) -> list[object]: ...

    def observe_seasonal_reminders(self, today: date) -> list[object]: ...

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
    backend = runtime()
    with backend.worker_scope("car-care-worker") if backend is not None else nullcontext():
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
    backend = runtime()
    if backend is not None:
        saved = backend.read_state("car-care-worker", "worker_offset", {})
        offset = saved.get("offset", offset)
    next_offset = offset
    for update in telegram.poll(offset):
        response = handler.handle_update(update)
        if response and not _send_command_reply(telegram, response, update.update_id):
            break
        if update.update_id is not None:
            candidate = update.update_id + 1
            next_offset = candidate if next_offset is None else max(next_offset, candidate)
            if backend is not None:
                backend.write_state("car-care-worker", "worker_offset", {"offset": next_offset})
        if stopped is not None and stopped.is_set():
            break
    return next_offset


def _send_command_reply(telegram, response: str, update_id: int | None) -> bool:
    backend = runtime()
    if backend is None:
        return telegram.send(response)
    if update_id is None:
        raise RuntimeError("HA command delivery requires update identity")
    key = f"reply:{update_id}"
    with backend.state_transaction("car-care-worker", key, {}) as state:
        if state.get("status") in {"uncertain", "sent"}:
            return True
        state["status"] = "uncertain"
    sent = telegram.send(response)
    if sent:
        backend.write_state("car-care-worker", key, {"status": "sent"})
    return sent


def _observe_vehicle(
    telegram: _TelegramGateway, hyundai: _HyundaiGateway, monitor: _VehicleMonitor
) -> None:
    for alert in monitor.observe_seasonal_reminders(_today_in_korea()):
        if runtime() is not None:
            _send_observation(telegram, alert.text, lambda: monitor.acknowledge(alert))
        elif telegram.send(alert.text):
            monitor.acknowledge(alert)
    result = hyundai.fetch_snapshot()
    if result.status == "disabled":
        return
    if result.status == "error":
        today = _today_in_korea()
        message = (
            "Hyundai 차량 연결 인증이 만료되었거나 거부되었습니다. /현대연결 명령으로 다시 연결하세요."
            if result.error == "auth"
            else "Hyundai 차량 상태 조회 오류: API 연결 또는 응답을 확인하세요."
        )
        if monitor.should_notify_hyundai_error(today):
            if runtime() is not None:
                _send_observation(telegram, message, lambda: monitor.acknowledge_hyundai_error(today))
            elif telegram.send(message):
                monitor.acknowledge_hyundai_error(today)
        return
    if result.snapshot is None:
        return
    for alert in monitor.observe(result.snapshot):
        if runtime() is not None:
            _send_observation(telegram, alert.text, lambda: monitor.acknowledge(alert))
        elif telegram.send(alert.text):
            monitor.acknowledge(alert)


def _send_observation(telegram, text: str, acknowledge) -> None:
    backend = runtime()
    key = f"observation_delivery:{uuid.uuid4().hex}"
    backend.write_state("car-care-worker", key, {"status": "uncertain"})
    # Advance the durable observation before the provider. Its delivery remains
    # uncertain on either a provider error or a missing database acknowledgement.
    acknowledge()
    if telegram.send(text):
        backend.write_state("car-care-worker", key, {"status": "sent"})


def _today_in_korea() -> date:
    return datetime.now(KOREA_TIMEZONE).date()


def main() -> None:
    database_path = _database_path()
    if runtime() is None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
    store = CarCareStore(database_path)
    schema_ready = runtime() is None
    if schema_ready:
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
            backend = runtime()
            try:
                with backend.worker_scope("car-care-worker") if backend is not None else nullcontext():
                    if not schema_ready:
                        store.initialize()
                        schema_ready = True
                    offset = _handle_updates(handler, telegram, offset, stopped)
                    if stopped.is_set():
                        break
                    if time.monotonic() >= next_hyundai_at:
                        _observe_vehicle(telegram, hyundai, monitor)
                        next_hyundai_at = time.monotonic() + HYUNDAI_INTERVAL_SECONDS
            except Exception as exc:
                if backend is None or not isinstance(exc, backend.DatabaseUnavailable):
                    raise
                # A standby remains alive, but never polls, refreshes or sends.
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
