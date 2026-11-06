"""Exercise optional HA boundaries through each application's real entry points."""
import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.book_memo import test_ui_contract


class HaBackendTests(unittest.TestCase):
    @contextmanager
    def application(self):
        with tempfile.TemporaryDirectory() as directory, test_ui_contract.BookMemoUiContractTests().loaded_app(directory):
            import app.main as main
            from app.services import book_service as service, ha_backend, readiness, write_safety
            yield main, service, ha_backend, readiness, write_safety

    def test_default_does_not_import_or_call_pg_runtime(self):
        with self.application() as (_, service, backend, _, _):
            with patch.dict(os.environ, {}, clear=False), patch.object(backend, "runtime", side_effect=AssertionError("PG runtime used")):
                os.environ.pop("HA_PG_CONFIG", None)
                service.init_db()
                self.assertTrue(service.DB_PATH.is_file())

    def test_empty_pg_configuration_cannot_fall_back_to_sqlite(self):
        with self.application() as (_, service, backend, _, _):
            with patch.dict(os.environ, {"HA_PG_CONFIG": ""}), patch.object(backend, "runtime", side_effect=sqlite3.OperationalError("invalid configuration")):
                with self.assertRaises(sqlite3.OperationalError):
                    service.init_db()
                self.assertFalse(service.DB_PATH.exists())

    def test_pg_startup_checks_schema_without_creating_sqlite(self):
        with self.application() as (_, service, backend, readiness, _):
            runtime = MagicMock()
            runtime.connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (1,)
            with patch.dict(os.environ, {"HA_PG_CONFIG": "fixture"}), patch.object(backend, "runtime", return_value=runtime):
                service.init_db()
                self.assertFalse(service.DB_PATH.exists())
                runtime.require_schema.assert_called_with("book-memo", "main", backend.REQUIRED_TABLES)
                self.assertTrue(readiness.database_ready(service.DB_PATH, service._schema_ready))
                runtime.require_schema.side_effect = sqlite3.OperationalError("unavailable")
                self.assertFalse(readiness.database_ready(service.DB_PATH, service._schema_ready))

    def test_pg_native_search_and_write_transaction(self):
        with self.application() as (_, _, backend, _, _):
            connection = MagicMock()
            with patch.dict(os.environ, {"HA_PG_CONFIG": "fixture"}):
                backend.begin_write(connection)
                connection.execute.assert_called_once_with("BEGIN")
                query = backend.search_sql("books.title = ? COLLATE NOCASE OR books.title LIKE ?")
                self.assertEqual(query, "LOWER(books.title) = LOWER(?) OR books.title ILIKE ?")

    def test_repository_search_uses_native_pg_query(self):
        with self.application() as (_, service, backend, _, _):
            runtime = MagicMock()
            connection = runtime.connect.return_value.__enter__.return_value
            connection.execute.return_value.fetchall.return_value = []
            with patch.dict(os.environ, {"HA_PG_CONFIG": "fixture"}), patch.object(backend, "runtime", return_value=runtime), patch.object(backend, "require_schema"):
                self.assertEqual(service.search_books_and_memos("MiXeD"), [])
                query = connection.execute.call_args.args[0]
                self.assertNotIn("COLLATE NOCASE", query)
                self.assertIn(" ILIKE ", query)
                runtime.connect.assert_called_with("book-memo")

    def test_sessions_and_failures_survive_local_cache_loss(self):
        with self.application() as (main, _, backend, _, _):
            shared = {}
            @contextmanager
            def state_transaction(service, key, default):
                self.assertEqual(service, "book-memo")
                value = copy.deepcopy(shared.get(key, default))
                yield value
                shared[key] = copy.deepcopy(value)
            runtime = SimpleNamespace(state_transaction=state_transaction)
            with patch.dict(os.environ, {"HA_PG_CONFIG": "fixture"}), patch.object(backend, "runtime", return_value=runtime):
                token = main._create_write_session()
                serialized = json.dumps(shared)
                self.assertNotIn(token, serialized)
                self.assertIn(hashlib.sha256(token.encode()).hexdigest(), shared["write-sessions"])
                self.assertNotIn(token, main._WRITE_SESSIONS)
                main._WRITE_SESSIONS.clear()
                self.assertTrue(main._has_write_session(SimpleNamespace(cookies={main.WRITE_AUTH_COOKIE: token})))
                main._revoke_write_session(token)
                main._WRITE_SESSIONS.clear()
                self.assertFalse(main._has_write_session(SimpleNamespace(cookies={main.WRITE_AUTH_COOKIE: token})))
                for _ in range(main.AUTH_RATE_LIMIT_MAX_FAILURES):
                    main._record_auth_failure("fixture-client")
                    main._AUTH_FAILURES.clear()
                self.assertTrue(main._auth_rate_limited("fixture-client"))
                main._clear_auth_failures("fixture-client")
                self.assertFalse(main._auth_rate_limited("fixture-client"))
                self.assertFalse(main.AUTH_RATE_LIMIT_STATE_PATH.exists())

    def test_replay_requires_fresh_protection_proof(self):
        with self.application() as (_, _, _, _, safety):
            connection = MagicMock()
            connection.execute.return_value.fetchone.return_value = {
                "operation": "fixture", "payload_hash": safety.payload_hash(["payload"]), "result_id": 42,
            }
            with patch.dict(os.environ, {"HA_PG_CONFIG": "fixture"}):
                self.assertEqual(safety.replay_result(connection, "00000000-0000-0000-0000-000000000001", "fixture", ["payload"]), 42)
                connection.protect_replay.assert_called_once()
                connection.protect_replay.side_effect = sqlite3.OperationalError("no protection proof")
                with self.assertRaises(sqlite3.OperationalError):
                    safety.replay_result(connection, "00000000-0000-0000-0000-000000000001", "fixture", ["payload"])
