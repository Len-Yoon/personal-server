"""Isolated service tests for authentication, audit and policy boundaries."""

import importlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests._test_support import prepare_service_import


class SecurityFullPathsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        environment = patch.dict(os.environ, {
            "SECURITY_LOG_PATH": str(self.root / "logs" / "audit.txt"),
            "SECURITY_LOG_TIMEZONE": "Asia/Seoul",
            "AUTH_RATE_LIMIT_STATE_PATH": str(self.root / "auth" / "state.json"),
            "AUTH_RATE_LIMIT_MAX_FAILURES": "2",
            "AUTH_RATE_LIMIT_WINDOW_SECONDS": "300",
            "AUTH_SESSION_MAX_ENTRIES": "2",
            "APP_ENV": "test",
            "ADMIN_STATUS_PASSWORD": "",
            "FILE_MANAGER_PASSWORD": "",
            "DELETE_PASSWORD": "",
            "FILE_ALLOWED_EXTENSIONS": "",
            "FILE_BLOCKED_EXTENSIONS": "EXE, .Sh, ,",
            "FILE_MAX_UPLOAD_MB": "7",
        })
        environment.start()
        self.addCleanup(environment.stop)
        prepare_service_import("portal-web")
        self.security = importlib.import_module("app.services.security")

    def write_state(self, value):
        path = self.security.AUTH_RATE_LIMIT_STATE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_rate_limit_persists_scopes_and_stops_at_limit(self):
        security = self.security
        self.assertFalse(security.auth_rate_limited("files", "client"))
        self.assertFalse(security.record_auth_failure("files", "client"))
        self.assertFalse(security.record_auth_failure("files", "client"))
        before = security.AUTH_RATE_LIMIT_STATE_PATH.read_bytes()
        self.assertTrue(security.record_auth_failure("files", "client"))
        self.assertEqual(security.AUTH_RATE_LIMIT_STATE_PATH.read_bytes(), before)
        self.assertTrue(security.auth_rate_limited("files", "client"))
        self.assertFalse(security.auth_rate_limited("admin", "client"))
        self.assertFalse(security.auth_rate_limited("files", "other"))
        security._AUTH_FAILURES.clear()
        self.assertTrue(security.auth_rate_limited("files", "client"))

    def test_clear_failure_preserves_other_scopes_and_skips_unchanged_write(self):
        security = self.security
        security.record_auth_failure("files", "client")
        security.record_auth_failure("admin", "client")
        security.clear_auth_failures("files", "client")
        self.assertEqual(set(security._read_auth_failures()), {("admin", "client")})
        with patch.object(security, "_persist_auth_failures") as persist:
            security.clear_auth_failures("files", "missing")
            persist.assert_not_called()

    def test_expired_failures_are_pruned_and_persisted_by_reads_and_clear(self):
        security = self.security
        old = (datetime.now(security.LOG_TIMEZONE) - timedelta(seconds=301)).isoformat()
        current = datetime.now(security.LOG_TIMEZONE).isoformat()
        self.write_state({"files": {"old": [old], "active": [old, current]}})
        self.assertFalse(security.auth_rate_limited("files", "old"))
        self.assertEqual(json.loads(security.AUTH_RATE_LIMIT_STATE_PATH.read_text()), {
            "files": {"active": [current]},
        })
        self.write_state({"files": {"old": [old]}})
        security.clear_auth_failures("files", "absent")
        self.assertEqual(json.loads(security.AUTH_RATE_LIMIT_STATE_PATH.read_text()), {})

    def test_state_reader_rejects_corrupt_and_malformed_entries(self):
        security = self.security
        path = security.AUTH_RATE_LIMIT_STATE_PATH
        self.assertEqual(security._read_auth_failures(), {})
        path.parent.mkdir(parents=True)
        path.write_text("{corrupt", encoding="utf-8")
        self.assertEqual(security._read_auth_failures(), {})
        self.write_state([])
        self.assertEqual(security._read_auth_failures(), {})
        aware = "2026-01-01T00:00:00+00:00"
        naive = "2026-01-01T09:00:00"
        self.write_state({"invalid": [], "files": {
            "not-list": "bad", "empty": [], "bad": [None, 1, "not-a-date"],
            "valid": [aware, naive, "bad"],
        }})
        result = security._read_auth_failures()
        expected = datetime(2026, 1, 1, 9, tzinfo=security.LOG_TIMEZONE)
        self.assertEqual(result, {("files", "valid"): [expected, expected]})
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            self.assertEqual(security._read_auth_failures(), {})

    def test_reader_defensively_ignores_non_string_mapping_keys(self):
        # JSON keys are strings; exercise defensive handling through the decoder seam.
        security = self.security
        self.write_state({})
        with patch.object(security.json, "loads", return_value={1: {}, "files": {2: []}}):
            self.assertEqual(security._read_auth_failures(), {})

    def test_persistence_failure_preserves_existing_state_and_removes_temporary_file(self):
        security = self.security
        security.record_auth_failure("files", "existing")
        path = security.AUTH_RATE_LIMIT_STATE_PATH
        original = path.read_bytes()
        with patch.object(security.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                security.record_auth_failure("admin", "new")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob("*.tmp")), [])
        self.assertEqual(list(path.parent.glob(".*.tmp")), [])
        # An exception inside the context must release the real advisory lock.
        with patch.object(security.fcntl, "flock", wraps=security.fcntl.flock) as flock:
            with self.assertRaisesRegex(RuntimeError, "abort"):
                with security._auth_rate_limit_lock():
                    raise RuntimeError("abort")
            self.assertEqual([call.args[1] for call in flock.call_args_list], [
                security.fcntl.LOCK_EX, security.fcntl.LOCK_UN,
            ])
        self.assertFalse(security.record_auth_failure("admin", "new"))
        self.assertEqual(set(security._read_auth_failures()), {
            ("files", "existing"), ("admin", "new"),
        })

    def test_sessions_retry_collision_check_scope_expiry_and_evict_oldest(self):
        security = self.security
        now = datetime.now(security.LOG_TIMEZONE)
        security._AUTH_SESSIONS["retained"] = ("files", now + timedelta(seconds=30))
        with patch.object(security.secrets, "token_urlsafe", side_effect=["retained", "fresh"]) as issue:
            self.assertEqual(security.create_auth_session("admin", 60), "fresh")
            self.assertEqual(issue.call_count, 2)
        self.assertTrue(security.has_auth_session("admin", "fresh"))
        self.assertFalse(security.has_auth_session("files", "fresh"))
        self.assertFalse(security.has_auth_session("admin", "unknown"))
        with patch.object(security.secrets, "token_urlsafe", return_value="latest"):
            security.create_auth_session("files", 90)
        self.assertFalse(security.has_auth_session("files", "retained"))
        self.assertTrue(security.has_auth_session("admin", "fresh"))
        security._AUTH_SESSIONS["expired"] = ("files", now - timedelta(seconds=1))
        self.assertFalse(security.has_auth_session("files", "expired"))
        self.assertNotIn("expired", security._AUTH_SESSIONS)

    def test_session_expiry_boundary_is_inclusive(self):
        security = self.security
        now = datetime.now(security.LOG_TIMEZONE)
        security._AUTH_SESSIONS["boundary"] = ("files", now)
        with patch.object(security, "datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            self.assertTrue(security.has_auth_session("files", "boundary"))
            clock.now.return_value = now + timedelta(microseconds=1)
            self.assertFalse(security.has_auth_session("files", "boundary"))

    def test_password_fallbacks_and_encoding_failures(self):
        security = self.security
        self.assertEqual(security.admin_password(), "")
        with patch.dict(os.environ, {"DELETE_PASSWORD": " delete "}):
            self.assertEqual(security.admin_password(), "delete")
            with patch.dict(os.environ, {"FILE_MANAGER_PASSWORD": " files "}):
                self.assertEqual(security.admin_password(), "files")
                with patch.dict(os.environ, {"ADMIN_STATUS_PASSWORD": " admin "}):
                    self.assertEqual(security.admin_password(), "admin")
        self.assertTrue(security.password_matches("테스트", "테스트"))
        self.assertFalse(security.password_matches("wrong", "expected"))
        self.assertFalse(security.password_matches(None, "expected"))
        self.assertFalse(security.password_matches("\ud800", "expected"))

    def test_cookie_policy_covers_proxy_public_hosts_and_local_http(self):
        security = self.security
        for scheme, host, expected in [
            ("http", None, False), ("http", "localhost", False),
            ("https", "localhost", True), ("http", "LEN.PE.KR", True),
            ("http", "portal.len.pe.kr", True), ("http", "evillen.pe.kr", False),
            ("http", "len.pe.kr.attacker.invalid", False),
        ]:
            with self.subTest(scheme=scheme, host=host):
                request = SimpleNamespace(url=SimpleNamespace(scheme=scheme, hostname=host))
                self.assertEqual(security.requires_secure_cookie(request), expected)
        with patch.dict(os.environ, {"APP_ENV": " Production "}):
            self.assertTrue(security.is_production_environment())
            self.assertTrue(security.requires_secure_cookie(SimpleNamespace(
                url=SimpleNamespace(scheme="http", hostname="localhost"),
            )))

    def test_user_events_clean_details_filter_fields_and_preserve_append_history(self):
        security = self.security
        security.append_security_event("first", unicode="한글", when=datetime(2026, 1, 1))
        security.append_user_event("service_opened", target="\r\n target \r", query="x" * 301,
                                   secret="must not be logged")
        security.append_user_event("unknown\n event", path="ignored")
        events = security.read_recent_events()
        self.assertEqual([event["event"] for event in events], [
            "user_event_blocked", "user_service_opened", "first",
        ])
        self.assertEqual(events[0]["details"], {
            "reason": "event_not_allowed", "requested_event": "unknown  event",
        })
        self.assertEqual(events[1]["details"], {"target": "target", "query": "x" * 300})
        self.assertEqual(events[2]["details"], {"unicode": "한글", "when": "2026-01-01 00:00:00"})
        stored = security._daily_log_path(datetime.now(security.LOG_TIMEZONE)).read_text()
        self.assertIn("한글", stored)
        self.assertNotIn("must not be logged", stored)
        self.assertRegex(json.loads(stored.splitlines()[0])["timestamp"], r" KST$")

    def test_recent_events_skip_corruption_bound_reads_and_keep_non_object_records(self):
        security = self.security
        self.assertEqual(security.read_recent_events(), [])
        security.LOG_PATH.parent.mkdir(parents=True)
        older = security.LOG_PATH.with_name("audit-2026-01-01.txt")
        newer = security.LOG_PATH.with_name("audit-2026-01-02.txt")
        older.write_text(json.dumps({"event": "older"}) + "\n")
        newer.write_text("\n".join([
            json.dumps({"event": "outside-window"}), *(["bad"] * 198),
            json.dumps({"timestamp": "2026-01-02 09:00:00 KST", "event": "valid"}),
            json.dumps(["legacy"]),
        ]))
        self.assertEqual(security.read_recent_events(limit=1), [["legacy"]])
        events = security.read_recent_events(limit=9)
        self.assertEqual(events, [["legacy"], {
            "timestamp": "2026-01-02 09:00", "event": "valid",
        }, {"event": "older", "timestamp": "unknown"}])
        self.assertEqual(security._list_daily_logs(limit=1), [newer])
        self.assertEqual(security._list_daily_logs(), [newer, older])

    def test_log_directory_mode_and_missing_parent(self):
        security = self.security
        security.LOG_PATH = self.root / "absent" / "events"
        self.assertEqual(security._list_daily_logs(), [])
        self.assertEqual(security._daily_log_path(datetime(2026, 1, 2)),
                         security.LOG_PATH / "security-events-2026-01-02.txt")
        security.LOG_PATH.parent.mkdir(parents=True)
        decoy = security.LOG_PATH.parent / "security-events-9999-01-01.txt"
        decoy.write_text(json.dumps({"event": "outside-configured-directory"}))
        original_decoy = decoy.read_bytes()
        security.append_security_event("directory-event", target="한글")
        generated = security._daily_log_path(datetime.now(security.LOG_TIMEZONE))
        self.assertEqual(security._list_daily_logs(), [generated])
        self.assertEqual(security._list_daily_logs(limit=1), [generated])
        events = security.read_recent_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "directory-event")
        self.assertEqual(events[0]["details"], {"target": "한글"})
        self.assertEqual(decoy.read_bytes(), original_decoy)

    def test_security_status_reports_policy_and_header_names(self):
        security = self.security
        self.assertEqual(security._allowed_extensions(), set())
        with patch.dict(os.environ, {"FILE_ALLOWED_EXTENSIONS": " .TXT, jpg, ,TXT "}):
            status = security.security_status()
        self.assertEqual(status["file_policy"], {
            "max_upload_mb": 7, "blocked_extensions": ["exe", "sh"],
            "allowed_extensions": ["jpg", "txt"],
        })
        self.assertEqual(status["headers"], list(security.SECURITY_HEADERS))
        self.assertEqual(status["recent_events"], [])
        self.assertEqual(status["log_files"], [])
        self.assertEqual(status["log_path"], str(security._daily_log_path(
            datetime.now(security.LOG_TIMEZONE),
        )))
