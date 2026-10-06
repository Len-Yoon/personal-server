import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from urllib.error import HTTPError, URLError

from app.main import _observe_vehicle
from app.models import VehicleSnapshot
from app.services.hyundai import HyundaiClient
from app.services.store import CarCareStore
from app.services.vehicle_monitor import VehicleMonitor
from app.services.telegram import CommandHandler, TelegramUpdate


class FailureBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.token_path = Path(self.tmp.name) / "token.json"
        self.store = CarCareStore(Path(self.tmp.name) / "car.sqlite3")
        self.store.initialize()
        self.monitor = VehicleMonitor(self.store)

    def client(self):
        return HyundaiClient("fixture-client", "fixture-secret", "fixture-car",
                             redirect_uri="https://example.test/callback", token_store_path=self.token_path)

    def test_existing_connection_refresh_failure_notifies_once_and_retries(self):
        client = self.client()
        client._save_refresh_token("fixture-refresh")
        sent = []
        class Telegram:
            def send(self, text):
                sent.append(text)
                return True
        with patch.object(client, "_token_request", return_value=None), patch.object(self.monitor, "observe_seasonal_reminders", return_value=[]):
            result = client.fetch_snapshot()
            self.assertEqual(result.status, "error")
            _observe_vehicle(Telegram(), client, self.monitor)
            _observe_vehicle(Telegram(), client, self.monitor)
        self.assertEqual(len(sent), 1)
        self.assertIn("/현대연결", sent[0])

    def test_wrong_or_missing_callback_does_not_consume_valid_state(self):
        for invalid in ("wrong-state", None):
            with self.subTest(invalid=invalid):
                client = self.client()
                state = parse_qs(urlparse(client.begin_authorization()).query)["state"][0]
                with self.assertRaises(ValueError):
                    client._consume_authorization_state(invalid)
                try:
                    client._consume_authorization_state(state)
                except ValueError:
                    self.fail("invalid callback consumed the valid pending state")
                with self.assertRaises(ValueError):
                    client._consume_authorization_state(state)

    def test_two_clients_cannot_consume_same_state_twice(self):
        first, second = self.client(), self.client()
        state = parse_qs(urlparse(first.begin_authorization()).query)["state"][0]
        barrier = Barrier(2)
        def consume(client):
            barrier.wait()
            try:
                client._consume_authorization_state(state)
                return "consumed"
            except ValueError:
                return "rejected"
            except OSError:
                return "file-race"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(consume, (first, second)))
        self.assertEqual(sorted(results), ["consumed", "rejected"])

    def test_unavailable_warning_does_not_emit_false_recovery(self):
        active = VehicleSnapshot(datetime(2026, 10, 6, tzinfo=timezone.utc), 1000, 300, frozenset({"engine_oil"}))
        for alert in self.monitor.observe(active):
            self.monitor.acknowledge(alert)
        client = self.client()
        responses = [{"odometers": [{"value": 1001, "unit": 1}]}, {"value": 299, "unit": 1},
                     {"msgId": "unavailable"}, {"status": False}, {"status": False}, {"status": False}]
        with patch.object(client, "_valid_access_token", return_value="fixture-access"), patch.object(client, "_get_json", side_effect=responses):
            result = client.fetch_snapshot()
        self.assertEqual(result.status, "success")
        self.assertEqual(self.monitor.observe(result.snapshot), [])
        self.assertEqual(self.store.get_alert_state("warning:engine_oil"), "active")
        self.assertEqual(self.store.load_last_snapshot().odometer_km, 1001)
        restored_store = CarCareStore(self.store._path)
        restored_monitor = VehicleMonitor(restored_store)
        self.assertIn("engine_oil", restored_store.load_last_snapshot().unknown_warnings)
        self.assertEqual(restored_monitor.observe(restored_store.load_last_snapshot()), [])
        confirmed_clear = VehicleSnapshot(datetime.now(timezone.utc), 1001, 299, frozenset())
        self.assertEqual([a.text for a in self.monitor.observe(confirmed_clear)], ["경고등 해제: 엔진오일"])

    def test_legacy_warning_array_and_unknown_warning_survive_store_reload(self):
        with sqlite3.connect(self.store._path) as db:
            db.execute("INSERT INTO vehicle_snapshots (id, observed_at, odometer_km, dte_km, warnings_json) VALUES (1, ?, 1000, 300, ?)",
                       ("2026-10-06T00:00:00+00:00", json.dumps(["engine_oil"])))
        legacy = self.store.load_last_snapshot()
        self.assertEqual(getattr(legacy, "unknown_warnings", None), frozenset())
        snapshot = VehicleSnapshot(legacy.observed_at, 1001, 299, frozenset(), unknown_warnings=frozenset({"engine_oil"}))
        self.store.save_snapshot(snapshot)
        restored = CarCareStore(self.store._path).load_last_snapshot()
        self.assertEqual(restored.unknown_warnings, frozenset({"engine_oil"}))

    def test_expired_callback_preserves_state_for_diagnosis(self):
        client = self.client()
        with patch("app.services.hyundai.time.time", return_value=1000):
            state = parse_qs(urlparse(client.begin_authorization()).query)["state"][0]
        before = self.token_path.read_bytes()
        with patch("app.services.hyundai.time.time", return_value=1601):
            with self.assertRaises(ValueError):
                client._consume_authorization_state(state)
        self.assertEqual(self.token_path.read_bytes(), before)

    def test_warning_not_queried_by_provider_is_not_confirmed_clear(self):
        active = VehicleSnapshot(datetime.now(timezone.utc), 1000, 300, frozenset({"tire_pressure"}))
        for alert in self.monitor.observe(active):
            self.monitor.acknowledge(alert)
        client = self.client()
        responses = [{"odometers": [{"value": 1000, "unit": 1}]}, {"value": 300, "unit": 1},
                     *[{"status": False} for _ in range(4)]]
        with patch.object(client, "_valid_access_token", return_value="fixture-access"), patch.object(client, "_get_json", side_effect=responses):
            result = client.fetch_snapshot()
        self.assertEqual(self.monitor.observe(result.snapshot), [])
        self.assertEqual(self.store.get_alert_state("warning:tire_pressure"), "active")

    def test_refresh_connection_failure_is_distinct_from_rejected_authorization(self):
        for error, expected in ((URLError("fixture offline"), "request"),
                                (HTTPError("https://example.test", 401, "rejected", {}, None), "auth")):
            with self.subTest(expected=expected):
                client = self.client()
                client._save_refresh_token("fixture-refresh")
                with patch("app.services.hyundai.urlopen", side_effect=error):
                    result = client.fetch_snapshot()
                self.assertEqual(result.status, "error")
                self.assertEqual(result.error, expected)

    def test_rejected_refresh_authorization_guides_user_to_reconnect(self):
        client = self.client()
        client._save_refresh_token("fixture-refresh")
        sent = []
        class Telegram:
            def send(self, text):
                sent.append(text)
                return True
        with patch("app.services.hyundai.urlopen", side_effect=HTTPError("https://example.test", 401, "rejected", {}, None)), patch.object(self.monitor, "observe_seasonal_reminders", return_value=[]):
            _observe_vehicle(Telegram(), client, self.monitor)
        self.assertEqual(len(sent), 1)
        self.assertIn("/현대연결", sent[0])

    def test_vehicle_command_discloses_unknown_warning_without_false_clear(self):
        self.store.save_snapshot(VehicleSnapshot(datetime.now(timezone.utc), 1000, 300,
                                                frozenset(), frozenset({"engine_oil"})))
        reply = CommandHandler(self.store, "fixture-chat").handle_update(TelegramUpdate("fixture-chat", "/차량"))
        self.assertIn("경고등 확인 필요: 엔진오일", reply)

    def test_refresh_token_persistence_failure_is_an_error_without_worker_crash(self):
        client = self.client()
        client._save_refresh_token("fixture-refresh")
        payload = {"access_token": "fixture-access", "refresh_token": "fixture-new-refresh", "expires_in": 3600}
        with patch.object(client, "_token_request", return_value=payload), \
             patch.object(client, "_write_token_file", side_effect=OSError("fixture unavailable storage")):
            try:
                result = client.fetch_snapshot()
            except OSError:
                self.fail("refresh token write failure escaped fetch_snapshot")
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error, "request")
        self.assertIsNone(client._access_token)
