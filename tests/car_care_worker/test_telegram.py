from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.models import VehicleSnapshot
from app.services.store import CarCareStore
from app.services.telegram import CommandHandler, TelegramClient, TelegramUpdate


class CommandHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.store = CarCareStore(Path(self.temp_dir.name) / "car-care.sqlite3")
        self.store.initialize()
        self.handler = CommandHandler(self.store, allowed_chat_id="123")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_rejects_update_from_unconfigured_chat_id(self) -> None:
        self.assertIsNone(self.handler.handle_update(TelegramUpdate("999", "/차량")))

    def test_complete_transmission_oil_records_command_odometer(self) -> None:
        response = self.handler.handle_update(TelegramUpdate("123", "/정비완료 미션오일 52340"))

        self.assertIn("미션오일 정비 완료", response)
        self.assertEqual(self.store.get_maintenance("transmission_oil").odometer_km, 52340)

    def test_complete_engine_oil_accepts_user_facing_alias(self) -> None:
        self.handler.handle_update(TelegramUpdate("123", "/정비완료 엔진오일 52340"))

        self.assertEqual(self.store.get_maintenance("engine_oil").odometer_km, 52340)

    def test_manual_odometer_updates_snapshot(self) -> None:
        response = self.handler.handle_update(TelegramUpdate("123", "/주행거리 52340"))

        self.assertIn("52,340km", response)
        self.assertEqual(self.store.load_last_snapshot().odometer_km, 52340)

    def test_vehicle_status_marks_manual_mode_without_snapshot(self) -> None:
        response = self.handler.handle_update(TelegramUpdate("123", "/차량"))

        self.assertIn("수동 모드", response)

    def test_maintenance_list_and_alert_test_are_supported(self) -> None:
        self.assertIn("엔진오일", self.handler.handle_update(TelegramUpdate("123", "/정비목록")))
        self.assertIn("알림 테스트", self.handler.handle_update(TelegramUpdate("123", "/알림테스트")))

    def test_invalid_command_does_not_change_maintenance_history(self) -> None:
        response = self.handler.handle_update(TelegramUpdate("123", "/정비완료 브레이크 100"))

        self.assertIn("사용법", response)
        self.assertIsNone(self.store.get_maintenance("engine_oil"))


class TelegramClientTests(unittest.TestCase):
    def test_poll_uses_long_polling_and_parses_text_updates(self) -> None:
        payload = json.dumps({"ok": True, "result": [
            {"update_id": 7, "message": {"chat": {"id": 123}, "text": "/차량"}},
            {"update_id": 8, "message": {"chat": {"id": 123}}},
        ]}).encode()

        class Response:
            def read(self):
                return payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        with patch("app.services.telegram.urlopen", return_value=Response()) as mocked_urlopen:
            updates = TelegramClient("bot-token", "123").poll(offset=7)

        self.assertEqual(updates, [TelegramUpdate("123", "/차량", update_id=7)])
        request = mocked_urlopen.call_args.args[0]
        self.assertIn("getUpdates", request.full_url)
        self.assertEqual(json.loads(request.data), {"offset": 7, "timeout": 5})
        self.assertEqual(mocked_urlopen.call_args.kwargs["timeout"], 10)

    def test_poll_ignores_malformed_results_and_keeps_valid_updates(self) -> None:
        payloads = [
            json.dumps({"ok": True, "result": {"unexpected": "object"}}).encode(),
            json.dumps({"ok": True, "result": [
                "unexpected entry",
                {"update_id": 1, "message": "unexpected message"},
                {"update_id": 2, "message": {"chat": {"id": 123}, "text": "/차량"}},
            ]}).encode(),
        ]

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def read(self):
                return self.payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        with patch("app.services.telegram.urlopen", side_effect=[Response(payload) for payload in payloads]):
            client = TelegramClient("bot-token", "123")
            self.assertEqual(client.poll(offset=None), [])
            updates = client.poll(offset=None)

        self.assertEqual(updates, [TelegramUpdate("123", "/차량", update_id=2)])

    def test_send_posts_to_configured_chat_with_eight_second_timeout(self) -> None:
        class Response:
            def read(self):
                return b'{"ok": true}'

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        with patch("app.services.telegram.urlopen", return_value=Response()) as mocked_urlopen:
            self.assertTrue(TelegramClient("bot-token", "123").send("점검 완료"))

        request = mocked_urlopen.call_args.args[0]
        self.assertIn("sendMessage", request.full_url)
        self.assertEqual(json.loads(request.data), {"chat_id": "123", "text": "점검 완료"})
        self.assertEqual(mocked_urlopen.call_args.kwargs["timeout"], 8)


if __name__ == "__main__":
    unittest.main()
