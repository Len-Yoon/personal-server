import importlib
import json
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests._test_support import prepare_service_import


def _record_auth_failure_in_process(
    state_path: str,
    ready,
    write_started,
    release_write,
) -> None:
    prepare_service_import("portal-web")
    os.environ["AUTH_RATE_LIMIT_STATE_PATH"] = state_path
    os.environ["SECURITY_LOG_PATH"] = str(Path(state_path).with_name("security-events.txt"))
    import app.services.security as security

    security = importlib.reload(security)
    persist = security._persist_auth_failures

    def delayed_persist() -> None:
        write_started.set()
        release_write.wait(timeout=5)
        persist()

    security._persist_auth_failures = delayed_persist
    ready.wait(timeout=5)
    security.record_auth_failure("files", "127.0.0.1")


class PortalSecurityTests(unittest.TestCase):
    _ENV_KEYS = (
        "AUTH_RATE_LIMIT_STATE_PATH",
        "FILE_STORAGE_PATH",
        "SECURITY_LOG_PATH",
        "SECURITY_LOG_TIMEZONE",
    )

    def setUp(self):
        self._environment = {key: os.environ.get(key) for key in self._ENV_KEYS}

    def tearDown(self):
        for key, value in self._environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def reload_security(self, tempdir: str):
        prepare_service_import("portal-web")
        os.environ["SECURITY_LOG_PATH"] = str(Path(tempdir) / "security-events.txt")
        os.environ["SECURITY_LOG_TIMEZONE"] = "Asia/Seoul"
        import app.services.security as security

        return importlib.reload(security)

    def reload_file_store(self, tempdir: str):
        prepare_service_import("portal-web")
        os.environ["FILE_STORAGE_PATH"] = str(Path(tempdir) / "files")
        os.environ["SECURITY_LOG_PATH"] = str(Path(tempdir) / "security-events.txt")
        os.environ["FILE_MAX_UPLOAD_MB"] = "1"
        os.environ["FILE_BLOCKED_EXTENSIONS"] = "exe,sh"
        os.environ["FILE_ALLOWED_EXTENSIONS"] = ""

        import app.services.security as security
        import app.services.file_store as file_store

        importlib.reload(security)
        return importlib.reload(file_store)

    def test_daily_log_path_includes_date(self):
        with tempfile.TemporaryDirectory() as tempdir:
            security = self.reload_security(tempdir)
            target = security._daily_log_path(datetime(2026, 6, 30))

            self.assertEqual(target.name, "security-events-2026-06-30.txt")

    def test_upload_extension_policy_blocks_script(self):
        with tempfile.TemporaryDirectory() as tempdir:
            file_store = self.reload_file_store(tempdir)

            with self.assertRaises(ValueError):
                file_store._validate_upload_name("deploy.sh")

    def test_safe_path_blocks_escape(self):
        with tempfile.TemporaryDirectory() as tempdir:
            file_store = self.reload_file_store(tempdir)
            file_store.ensure_storage()

            with self.assertRaises(ValueError):
                file_store._safe_path("../outside.txt")

    def test_save_upload_rejects_existing_file(self):
        with tempfile.TemporaryDirectory() as tempdir:
            file_store = self.reload_file_store(tempdir)
            file_store.ensure_storage()
            destination = Path(tempdir) / "files" / "memo.txt"
            destination.write_text("already here", encoding="utf-8")

            upload = SimpleNamespace(
                filename="memo.txt",
                file=SimpleNamespace(read=lambda size=-1: b"new content"),
                content_type="text/plain",
            )

            with self.assertRaises(FileExistsError):
                file_store.save_upload("", upload)

    def test_auth_rate_limit_blocks_repeated_failures(self):
        with tempfile.TemporaryDirectory() as tempdir:
            security = self.reload_security(tempdir)

            for _ in range(5):
                self.assertFalse(security.auth_rate_limited("files", "127.0.0.1"))
                security.record_auth_failure("files", "127.0.0.1")

            self.assertTrue(security.auth_rate_limited("files", "127.0.0.1"))

    def test_auth_rate_limit_records_survive_security_service_restart(self):
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "auth-rate-limit.json"
            os.environ["AUTH_RATE_LIMIT_STATE_PATH"] = str(state_path)
            security = self.reload_security(tempdir)

            for _ in range(5):
                security.record_auth_failure("files", "127.0.0.1")

            self.assertTrue(state_path.exists())
            restarted_security = importlib.reload(security)
            self.assertTrue(restarted_security.auth_rate_limited("files", "127.0.0.1"))

    def test_auth_sessions_evict_oldest_entry_at_configured_bound(self):
        with tempfile.TemporaryDirectory() as tempdir:
            os.environ["AUTH_SESSION_MAX_ENTRIES"] = "1"
            self.addCleanup(os.environ.pop, "AUTH_SESSION_MAX_ENTRIES", None)
            security = self.reload_security(tempdir)

            first_session = security.create_auth_session("files", 60)
            second_session = security.create_auth_session("files", 60)

            self.assertFalse(security.has_auth_session("files", first_session))
            self.assertTrue(security.has_auth_session("files", second_session))

    def test_auth_session_cap_holds_during_concurrent_session_creation(self):
        with tempfile.TemporaryDirectory() as tempdir:
            os.environ["AUTH_SESSION_MAX_ENTRIES"] = "1"
            self.addCleanup(os.environ.pop, "AUTH_SESSION_MAX_ENTRIES", None)
            security = self.reload_security(tempdir)
            token_issuance_started = threading.Event()
            release_token_issuance = threading.Event()
            token_issuance_count = 0
            count_lock = threading.Lock()
            original_token_urlsafe = security.secrets.token_urlsafe

            def delayed_token_urlsafe(size: int) -> str:
                nonlocal token_issuance_count
                with count_lock:
                    token_issuance_count += 1
                    should_wait = token_issuance_count == 1
                if should_wait:
                    token_issuance_started.set()
                    release_token_issuance.wait(timeout=5)
                return original_token_urlsafe(size)

            issued_sessions: list[str] = []
            with patch.object(security.secrets, "token_urlsafe", side_effect=delayed_token_urlsafe):
                first_login = threading.Thread(
                    target=lambda: issued_sessions.append(security.create_auth_session("files", 60))
                )
                second_login = threading.Thread(
                    target=lambda: issued_sessions.append(security.create_auth_session("files", 60))
                )
                first_login.start()
                self.assertTrue(token_issuance_started.wait(timeout=2))
                second_login.start()
                time.sleep(0.1)
                release_token_issuance.set()
                first_login.join(timeout=2)
                second_login.join(timeout=2)

            self.assertFalse(first_login.is_alive())
            self.assertFalse(second_login.is_alive())
            self.assertEqual(
                sum(security.has_auth_session("files", token) for token in issued_sessions),
                1,
            )

    def test_auth_rate_limit_keeps_all_concurrent_process_failures(self):
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "auth-rate-limit.json"
            process_count = 5
            context = multiprocessing.get_context("spawn")
            ready = context.Barrier(process_count)
            write_started = context.Event()
            release_write = context.Event()
            workers = [
                context.Process(
                    target=_record_auth_failure_in_process,
                    args=(str(state_path), ready, write_started, release_write),
                )
                for _ in range(process_count)
            ]

            for worker in workers:
                worker.start()
            try:
                self.assertTrue(write_started.wait(timeout=10))
                time.sleep(0.2)
                release_write.set()
                for worker in workers:
                    worker.join(timeout=10)
                self.assertEqual([worker.exitcode for worker in workers], [0] * process_count)
            finally:
                release_write.set()
                for worker in workers:
                    if worker.is_alive():
                        worker.terminate()
                    worker.join()

            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(len(state["files"]["127.0.0.1"]), process_count)

    def test_append_user_event_allows_known_click_events(self):
        with tempfile.TemporaryDirectory() as tempdir:
            security = self.reload_security(tempdir)

            security.append_user_event(
                "service_opened",
                path="/",
                target="유튜브 메모장",
                href="http://127.0.0.1:8002",
                client="127.0.0.1",
            )

            events = security.read_recent_events()
            self.assertEqual(events[0]["event"], "user_service_opened")
            self.assertEqual(events[0]["details"]["target"], "유튜브 메모장")


if __name__ == "__main__":
    unittest.main()
