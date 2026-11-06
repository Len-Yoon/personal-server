"""Exercise HA dispatch in the code-only quality fixture; real PG is tested separately."""
import copy
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

from tests._test_support import prepare_service_import


class ProtectedUnavailable(sqlite3.OperationalError):
    pass


class SharedBackend:
    """Commit-or-rollback state double, without importing a driver or using a socket."""
    DatabaseUnavailable = ProtectedUnavailable

    def __init__(self):
        self.rows = {}
        self.fail_commit = False

    @contextmanager
    def state_transaction(self, service, key, default):
        assert service == "portal-web"
        value = copy.deepcopy(self.rows.get(key, default))
        yield value
        if self.fail_commit:
            raise ProtectedUnavailable("protected_result_unavailable")
        self.rows[key] = copy.deepcopy(value)

    def read_state(self, service, key, default):
        assert service == "portal-web"
        return copy.deepcopy(self.rows.get(key, default))


class HaQualityPathsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.backend = SharedBackend()
        environment = patch.dict(os.environ, {
            "APP_ENV": "test", "HA_LOCAL_RUNTIME": "1", "HA_PG_CONFIG": "synthetic-fixture",
            "HA_FILE_OBJECT_ROOT": str(self.root / "objects"),
            "FILE_STORAGE_PATH": str(self.root / "legacy-files"),
            "SECURITY_LOG_PATH": str(self.root / "legacy-audit.log"),
            "AUTH_RATE_LIMIT_STATE_PATH": str(self.root / "legacy-auth.json"),
            "AUTH_SESSION_STATE_PATH": "", "AUTH_RATE_LIMIT_MAX_FAILURES": "2",
        })
        environment.start()
        self.addCleanup(environment.stop)
        modules = patch.dict(sys.modules, {"ha_runtime": self.backend})
        modules.start()
        self.addCleanup(modules.stop)
        prepare_service_import("portal-web")
        self.security = importlib.import_module("app.services.security")
        self.store = importlib.import_module("app.services.file_store")
        self.files = importlib.import_module("app.services.ha_files")
        # Config/TLS/PG admission is exercised by the separate real-PG fixture.
        # These tests keep the application's actual file/security dispatch and
        # immutable object operations, replacing only the unavailable PG boundary.
        guard = patch.object(self.files, "_guard")
        guard.start()
        self.addCleanup(guard.stop)

    def test_shared_auth_sessions_and_failures_do_not_use_local_state(self):
        security = self.security
        self.assertTrue(getattr(security.AUTH_SESSION_STORE, "is_postgres", False))
        token = security.create_auth_session("files", 300)
        self.assertTrue(security.has_auth_session("files", token))
        self.assertFalse(security.has_auth_session("admin", token))
        self.assertNotIn(token, json.dumps(self.backend.rows))
        self.assertIn(hashlib.sha256(token.encode()).hexdigest(), self.backend.rows["auth_sessions"]["sessions"])
        self.assertFalse(security.record_auth_failure("files", "fixture-client"))
        security._AUTH_FAILURES.clear()
        self.assertFalse(security.record_auth_failure("files", "fixture-client"))
        self.assertTrue(security.auth_rate_limited("files", "fixture-client"))
        self.assertEqual(len(security._read_auth_failures()["files", "fixture-client"]), 2)
        security.clear_auth_failures("files", "fixture-client")
        self.assertFalse(security.auth_rate_limited("files", "fixture-client"))
        self.assertIsNone(security._HA_AUTH_STATE)
        self.assertFalse(security.AUTH_RATE_LIMIT_STATE_PATH.exists())
        self.assertFalse(security.LOG_PATH.exists())

    def test_shared_security_events_are_bounded_and_read_newest_first(self):
        self.backend.rows["security_events"] = {"events": [
            {"event": "retained", "details": {"index": index}} for index in range(500)
        ]}
        self.security.append_security_event("fixture_event", path="fixture.txt")
        rows = self.backend.rows["security_events"]["events"]
        self.assertEqual(len(rows), 500)
        self.assertEqual(rows[0]["details"]["index"], 1)
        recent = self.security.read_recent_events(2)
        self.assertEqual(recent[0]["event"], "fixture_event")
        self.assertEqual(recent[0]["details"], {"path": "fixture.txt"})
        self.assertEqual(recent[1]["details"]["index"], 499)
        self.assertFalse(self.security.LOG_PATH.exists())

    def test_shared_auth_requires_transaction_and_restores_state_after_failure(self):
        security = self.security
        with self.assertRaisesRegex(ProtectedUnavailable, "auth_state_transaction_required"):
            security._persist_auth_failures()
        with self.assertRaisesRegex(RuntimeError, "synthetic_abort"):
            with security._auth_rate_limit_lock():
                self.assertIsNotNone(security._HA_AUTH_STATE)
                raise RuntimeError("synthetic_abort")
        self.assertIsNone(security._HA_AUTH_STATE)
        self.assertNotIn("auth_failures", self.backend.rows)
        self.backend.fail_commit = True
        with self.assertRaises(ProtectedUnavailable):
            security.record_auth_failure("files", "fixture-client")
        self.assertIsNone(security._HA_AUTH_STATE)
        self.assertNotIn("auth_failures", self.backend.rows)
        self.assertFalse(security.AUTH_RATE_LIMIT_STATE_PATH.exists())

    def test_file_dispatch_preserves_bytes_namespace_and_protected_failure(self):
        store = self.store
        store.ensure_storage()
        with store.storage_mutation_lock() as state:
            self.assertEqual(state["entries"], {})
        store.create_directory("", "documents")
        upload = lambda name, value: SimpleNamespace(filename=name, content_type="text/plain", file=io.BytesIO(value))
        self.assertEqual(store.save_upload("documents", upload("first.txt", b"first")), 5)
        self.assertEqual(store.save_uploads("documents", [upload("second.txt", b"second")]), 1)
        self.assertEqual({item["name"] for item in store.get_directory("documents")["files"]}, {"first.txt", "second.txt"})
        store.validate_download_limits(["documents"])
        directory = store.get_download_item_path("documents")
        self.assertEqual(len(list(store.iter_download_files(directory))), 2)
        downloaded = store.get_download_path("documents/first.txt")
        try:
            self.assertEqual(downloaded.read_bytes(), b"first")
        finally:
            downloaded.close()
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            store.write_download_archive(archive, ["documents"])
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.read("documents/second.txt"), b"second")
        self.assertEqual(store.validate_delete_items(["documents/first.txt"]), ["documents/first.txt"])
        with self.assertRaises(ProtectedUnavailable):
            with store._open_storage_item("documents/first.txt"):
                self.fail("HA dispatch must not open the legacy namespace")
        self.backend.fail_commit = True
        with self.assertRaises(ProtectedUnavailable):
            store.delete_item("documents/first.txt")
        self.backend.fail_commit = False
        self.assertEqual(len(store.get_directory("documents")["files"]), 2)
        store.delete_item("documents/first.txt")
        self.assertEqual([item["name"] for item in store.get_directory("documents")["files"]], ["second.txt"])
        self.assertFalse(store.STORAGE_PATH.exists())
