import unittest

from app.main import run_once
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
    def observe(self, _snapshot: object) -> list[Alert]:
        return [Alert("trip", "trip:summary", "[운행 결과]")]


class RunOnceTests(unittest.TestCase):
    def test_run_once_sends_command_response_and_monitor_alerts(self) -> None:
        telegram = _TelegramFake()

        run_once(_HandlerFake(), telegram, _HyundaiFake(), _MonitorFake())

        self.assertEqual(telegram.sent, ["[차량 상태]", "[운행 결과]"])


if __name__ == "__main__":
    unittest.main()
