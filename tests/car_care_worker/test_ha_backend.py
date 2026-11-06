import copy
from contextlib import contextmanager
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

class CarCareHATests(unittest.TestCase):
    def setUp(self):
        from app import main
        from app.services import hyundai, store
        self.main, self.hyundai, self.store = main, hyundai, store
        self.rows = {}

    @contextmanager
    def state_transaction(self, service, key, default):
        payload = copy.deepcopy(self.rows.get(key, default))
        yield payload
        self.rows[key] = copy.deepcopy(payload)

    def read_state(self, service, key, default):
        return copy.deepcopy(self.rows.get(key, default))

    @contextmanager
    def worker_scope(self, service):
        yield

    def test_shared_oauth_state_is_consumed_once_across_clients(self):
        with patch.object(self.hyundai, "runtime", return_value=self):
            first = self.hyundai.HyundaiClient(token_store_path="/not-used")
            second = self.hyundai.HyundaiClient(token_store_path="/not-used-either")
            first._save_authorization_state("fixture")
            second._consume_authorization_state("fixture")
            with self.assertRaises(ValueError):
                first._consume_authorization_state("fixture")

    def test_ambiguous_refresh_never_reuses_rotating_refresh_token(self):
        self.rows["hyundai_tokens"] = {"refresh_token": "fixture-token"}
        client = self.hyundai.HyundaiClient()
        with patch.object(self.hyundai, "runtime", return_value=self), patch.object(client, "_token_request", side_effect=OSError("ambiguous")) as exchange:
            with self.assertRaises(OSError):
                client._valid_access_token()
            with self.assertRaises(OSError):
                client._valid_access_token()
        exchange.assert_called_once()

    def test_reply_provider_crash_is_not_replayed(self):
        telegram = Mock()
        telegram.send.side_effect = OSError("ambiguous")
        with patch.object(self.main, "runtime", return_value=self):
            with self.assertRaises(OSError):
                self.main._send_command_reply(telegram, "fixture", 123)
            self.assertTrue(self.main._send_command_reply(telegram, "fixture", 123))
        telegram.send.assert_called_once()

    def test_pg_initialize_only_checks_schema_and_never_creates_local_db(self):
        backend = Mock()
        with patch.object(self.store, "runtime", return_value=backend):
            self.store.CarCareStore(Path("/not-used")).initialize()
        backend.require_schema.assert_called_once()
        backend.connect.assert_not_called()

    def test_pg_command_transaction_uses_native_sql_and_protects_replay(self):
        from unittest.mock import MagicMock
        backend = Mock()
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = ("fixture reply",)
        backend.connect.return_value.__enter__ = Mock(return_value=connection)
        backend.connect.return_value.__exit__ = Mock(return_value=False)
        with patch.object(self.store, "runtime", return_value=backend):
            result = self.store.CarCareStore(Path("/not-used")).process_command(123, lambda _: self.fail("replay callback"))
        self.assertEqual(result, "fixture reply")
        connection.protect_replay.assert_called_once()
        self.assertTrue(all(not call.args[0].startswith("BEGIN") for call in connection.execute.call_args_list))

    def test_standby_lifecycle_never_polls_or_creates_schema(self):
        from threading import Event
        stopped = Event()
        backend = Mock()

        class Unavailable(Exception):
            pass

        backend.DatabaseUnavailable = Unavailable

        @contextmanager
        def denied(service):
            stopped.set()
            raise Unavailable("not_worker_owner")
            yield

        backend.worker_scope = denied
        with patch.object(self.main, "runtime", return_value=backend), patch.object(self.main, "Event", return_value=stopped), patch.object(self.main, "_install_signal_handlers"), patch.object(self.main, "CarCareStore") as store, patch.object(self.main.TelegramClient, "from_environment") as telegram, patch.object(self.main.HyundaiClient, "from_environment"), patch.object(self.main, "HyundaiOAuthCallbackServer") as callback, patch.dict("os.environ", {"CAR_CARE_TELEGRAM_CHAT_ID": "fixture"}):
            self.main.main()
        store.return_value.initialize.assert_not_called()
        telegram.return_value.poll.assert_not_called()
        callback.return_value.stop.assert_called_once()
