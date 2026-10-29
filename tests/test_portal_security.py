import importlib
from io import BytesIO
import json
import multiprocessing
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

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


def _create_shared_session(path: str, output) -> None:
    prepare_service_import("portal-web")
    from app.services.session_store import SQLiteSessionStore

    output.put(SQLiteSessionStore(Path(path), 2).create("file_access", 60))


class PortalSecurityTests(unittest.TestCase):
    _ENV_KEYS = (
        "AUTH_SESSION_STATE_PATH",
        "AUTH_SESSION_MAX_ENTRIES",
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

    def load_app(self):
        prepare_service_import("portal-web")
        import app.main as main

        return importlib.reload(main).app

    def test_admin_security_route_returns_json_and_disables_cache(self):
        environment_keys = (
            "ADMIN_STATUS_PASSWORD",
            "AUTH_RATE_LIMIT_STATE_PATH",
            "SECURITY_LOG_PATH",
        )
        original_environment = {key: os.environ.get(key) for key in environment_keys}
        try:
            with tempfile.TemporaryDirectory() as tempdir:
                os.environ["ADMIN_STATUS_PASSWORD"] = "security-password"
                os.environ["AUTH_RATE_LIMIT_STATE_PATH"] = str(Path(tempdir) / "auth-rate-limit.json")
                os.environ["SECURITY_LOG_PATH"] = str(Path(tempdir) / "security-events.txt")
                app = self.load_app()

                with TestClient(app) as client:
                    response = client.get(
                        "/admin/security",
                        headers={"X-Security-Password": "security-password"},
                    )

            self.assertEqual(response.status_code, 200)
            self.assertIn("recent_events", response.json())
            self.assertIn("file_policy", response.json())
            self.assertEqual(response.headers["cache-control"], "no-store, no-cache, must-revalidate, max-age=0")
            self.assertEqual(response.headers["pragma"], "no-cache")
            self.assertEqual(response.headers["expires"], "0")
        finally:
            for key, value in original_environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_admin_security_route_returns_auth_errors_without_server_error(self):
        environment_keys = (
            "ADMIN_STATUS_PASSWORD",
            "AUTH_RATE_LIMIT_STATE_PATH",
            "SECURITY_LOG_PATH",
            "FILE_MANAGER_PASSWORD",
            "DELETE_PASSWORD",
        )
        original_environment = {key: os.environ.get(key) for key in environment_keys}
        try:
            with tempfile.TemporaryDirectory() as tempdir:
                os.environ["ADMIN_STATUS_PASSWORD"] = "security-password"
                os.environ["AUTH_RATE_LIMIT_STATE_PATH"] = str(Path(tempdir) / "auth-rate-limit.json")
                os.environ["SECURITY_LOG_PATH"] = str(Path(tempdir) / "security-events.txt")
                app = self.load_app()

                with TestClient(app) as client:
                    invalid = client.get(
                        "/admin/security",
                        headers={"X-Security-Password": "wrong"},
                    )
                    os.environ.pop("ADMIN_STATUS_PASSWORD", None)
                    missing = client.get("/admin/security")

            self.assertEqual(invalid.status_code, 401)
            self.assertEqual(missing.status_code, 403)
        finally:
            for key, value in original_environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_admin_events_ignores_unknown_event_and_records_requested_event_detail(self):
        with tempfile.TemporaryDirectory() as tempdir:
            security = self.reload_security(tempdir)
            app = self.load_app()

            with TestClient(app) as client:
                response = client.post(
                    "/admin/events",
                    json={"event": "unknown\n<script>", "path": "/dashboard"},
                    headers={"Origin": "http://testserver"},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"ok": True})
            events = security.read_recent_events()
            self.assertEqual(events[0]["event"], "user_event_blocked")
            self.assertEqual(events[0]["details"]["reason"], "event_not_allowed")
            self.assertEqual(events[0]["details"]["requested_event"], "unknown <script>")

    def test_daily_log_path_includes_date(self):
        with tempfile.TemporaryDirectory() as tempdir:
            security = self.reload_security(tempdir)
            target = security._daily_log_path(datetime(2026, 6, 30))

            self.assertEqual(target.name, "security-events-2026-06-30.txt")

    def test_security_headers_allow_only_known_external_media_hosts(self):
        with tempfile.TemporaryDirectory() as tempdir:
            security = self.reload_security(tempdir)

        self.assertEqual(
            security.SECURITY_HEADERS["Content-Security-Policy"],
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https://img.youtube.com https://image.aladin.co.kr "
            "https://books.google.com https://covers.openlibrary.org; "
            "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'; frame-src 'self' https://www.youtube.com",
        )

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

    def test_safe_path_rejects_symlink_component_inside_storage(self):
        with tempfile.TemporaryDirectory() as tempdir:
            file_store = self.reload_file_store(tempdir)
            file_store.ensure_storage()
            storage = Path(tempdir) / "files"
            (storage / "actual").mkdir()
            (storage / "actual" / "memo.txt").write_text("inside")
            (storage / "alias").symlink_to(storage / "actual", target_is_directory=True)

            with self.assertRaises(ValueError):
                file_store.get_download_path("alias/memo.txt")

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

            self.assertEqual(destination.read_text(encoding="utf-8"), "already here")

    def test_concurrent_same_name_upload_has_one_winner_and_preserves_winner_payload(self):
        with tempfile.TemporaryDirectory() as tempdir:
            file_store = self.reload_file_store(tempdir)
            file_store.ensure_storage()
            destination = Path(tempdir) / "files" / "memo.txt"
            payloads = [b"first payload", b"second payload"]
            start_gate = threading.Barrier(2)
            successes = []
            conflicts = []
            worker_errors = []
            open_calls = []
            original_open = file_store.os.open

            def synchronized_open(path, flags, *args, **kwargs):
                if path == "memo.txt" and flags & os.O_CREAT:
                    open_calls.append((flags, os.fstat(kwargs["dir_fd"]).st_ino))
                    # Upload budget serializes the complete write operation.
                return original_open(path, flags, *args, **kwargs)

            def upload_worker(payload):
                upload = SimpleNamespace(
                    filename="memo.txt",
                    file=BytesIO(payload),
                    content_type="text/plain",
                )
                try:
                    start_gate.wait(timeout=5)
                    file_store.save_upload("", upload)
                    successes.append(payload)
                except FileExistsError as error:
                    conflicts.append(error)
                except BaseException as error:  # pragma: no cover - diagnostic guard
                    worker_errors.append(error)

            with patch.object(file_store, "append_security_event"):
                with patch.object(file_store.os, "open", synchronized_open):
                    workers = [threading.Thread(target=upload_worker, args=(payload,)) for payload in payloads]
                    for worker in workers:
                        worker.start()
                    for worker in workers:
                        worker.join(timeout=5)

            self.assertFalse(worker_errors)
            self.assertEqual(len(open_calls), 2, open_calls)
            self.assertTrue(all(flags & os.O_EXCL and flags & os.O_NOFOLLOW for flags, _ in open_calls))
            self.assertTrue(all(inode == destination.parent.stat().st_ino for _, inode in open_calls))
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(destination.read_bytes(), successes[0])

    def test_upload_size_failure_removes_only_new_file(self):
        with tempfile.TemporaryDirectory() as tempdir:
            file_store = self.reload_file_store(tempdir)
            upload = SimpleNamespace(
                filename="oversized.txt",
                file=BytesIO(b"x" * (file_store.MAX_UPLOAD_BYTES + 1)),
                content_type="text/plain",
            )

            with self.assertRaises(ValueError):
                file_store.save_upload("", upload)

            self.assertFalse((Path(tempdir) / "files" / "oversized.txt").exists())

    def test_upload_read_failure_removes_only_new_file(self):
        with tempfile.TemporaryDirectory() as tempdir:
            file_store = self.reload_file_store(tempdir)

            class _FailingFile:
                def read(self, size):
                    raise OSError("read failed")

            upload = SimpleNamespace(
                filename="read-failure.txt",
                file=_FailingFile(),
                content_type="text/plain",
            )

            with self.assertRaises(OSError):
                file_store.save_upload("", upload)

            self.assertFalse((Path(tempdir) / "files" / "read-failure.txt").exists())

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

    def test_shared_auth_sessions_survive_reload_and_processes_without_plaintext(self):
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir).resolve() / "auth" / "sessions.sqlite3"
            os.environ["AUTH_SESSION_STATE_PATH"] = str(state_path)
            security = self.reload_security(tempdir)
            token = security.create_auth_session("file_access", 60)
            self.assertTrue(importlib.reload(security).has_auth_session("file_access", token))
            self.assertFalse(security.has_auth_session("portfolio_admin", token))
            self.assertFalse(security.has_auth_session("file_access", "unknown"))
            context = multiprocessing.get_context("spawn")
            output = context.Queue()
            workers = [context.Process(target=_create_shared_session, args=(str(state_path), output)) for _ in range(3)]
            for worker in workers:
                worker.start()
            tokens = [output.get(timeout=10) for _ in workers]
            for worker in workers:
                worker.join(timeout=10)
                self.assertEqual(worker.exitcode, 0)
            output.close()
            self.assertEqual(sum(security.has_auth_session("file_access", issued) for issued in tokens), 2)
            for path in state_path.parent.iterdir():
                data = path.read_bytes()
                for issued in [token, *tokens]:
                    self.assertNotIn(issued.encode(), data)
            self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)

    def test_shared_auth_sessions_preserve_fixed_ttl_boundary_and_prune(self):
        with tempfile.TemporaryDirectory() as tempdir:
            os.environ["AUTH_SESSION_STATE_PATH"] = str(Path(tempdir).resolve() / "sessions.sqlite3")
            security = self.reload_security(tempdir)
            from app.services import session_store
            with patch.object(session_store.time, "time", return_value=100):
                token = security.create_auth_session("file_access", 10)
            with patch.object(session_store.time, "time", return_value=110):
                self.assertTrue(security.has_auth_session("file_access", token))
            with patch.object(session_store.time, "time", return_value=110.001):
                self.assertFalse(security.has_auth_session("file_access", token))
                security.create_auth_session("homeops_admin", 20)
            with sqlite3.connect(security.AUTH_SESSION_STORE.path) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)

    def test_shared_confirmations_are_scoped_payload_bound_single_use_and_separate_cap(self):
        with tempfile.TemporaryDirectory() as tempdir:
            os.environ["AUTH_SESSION_STATE_PATH"] = str(Path(tempdir).resolve() / "sessions.sqlite3")
            os.environ["AUTH_SESSION_MAX_ENTRIES"] = "1"
            security = self.reload_security(tempdir)
            store = security.AUTH_SESSION_STORE
            login = security.create_auth_session("file_access", 60)
            old = store.create_confirmation("purge", "one", 60)
            token = store.create_confirmation("purge", "two", 60)
            self.assertTrue(security.has_auth_session("file_access", login))
            self.assertFalse(store.consume_confirmation("purge", "one", old))
            self.assertFalse(store.consume_confirmation("other", "two", token))
            self.assertFalse(store.consume_confirmation("purge", "tampered", token))
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(list(pool.map(lambda _: store.consume_confirmation("purge", "two", token), range(2))).count(True), 1)
            from app.services import session_store
            with patch.object(session_store.time, "time", return_value=100):
                expired = store.create_confirmation("purge", "two", 1)
            with patch.object(session_store.time, "time", return_value=102):
                self.assertFalse(store.consume_confirmation("purge", "two", expired))
            for path in store.path.parent.iterdir():
                self.assertNotIn(token.encode(), path.read_bytes())
                self.assertNotIn(b"two", path.read_bytes())

    def test_shared_session_failures_never_fall_back_to_memory(self):
        with tempfile.TemporaryDirectory() as tempdir:
            os.environ["AUTH_SESSION_STATE_PATH"] = str(Path(tempdir).resolve() / "sessions.sqlite3")
            security = self.reload_security(tempdir)
            store = security.AUTH_SESSION_STORE
            self.assertFalse(security.has_auth_session("file_access", "missing"))
            self.assertFalse(store.consume_confirmation("purge", "payload", "missing"))
            self.assertFalse(store.path.exists())
            token = security.create_auth_session("file_access", 60)
            for invalid in (None, "", "x" * 513, "\ud800"):
                self.assertFalse(store.validate("file_access", invalid))
                self.assertFalse(store.consume_confirmation("purge", "payload", invalid))
            with patch.object(store, "_connect", side_effect=sqlite3.OperationalError("locked")):
                self.assertFalse(security.has_auth_session("file_access", token))
                self.assertFalse(store.consume_confirmation("purge", "payload", token))
                with self.assertRaises(sqlite3.OperationalError):
                    security.create_auth_session("file_access", 60)
            self.assertEqual(security._AUTH_SESSIONS, {})
            store.path.write_bytes(b"corrupt")
            self.assertFalse(store.validate("file_access", token))

    def test_shared_session_collision_rollback_and_real_writer_lock_fail_closed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("portal-web")
            from app.services import session_store
            store = session_store.SQLiteSessionStore(Path(tempdir).resolve() / "sessions.sqlite3", 2)
            with patch.object(session_store.secrets, "token_urlsafe", return_value="same"):
                first = store.create("file_access", 60)
            with patch.object(session_store.secrets, "token_urlsafe", side_effect=["same", "fresh"]):
                self.assertEqual(store.create("file_access", 60), "fresh")
            # A failed transaction must roll back its cap eviction as well.
            with patch.object(session_store.secrets, "token_urlsafe", return_value="fresh"):
                with self.assertRaises(RuntimeError):
                    store.create("file_access", 60)
            self.assertTrue(store.validate("file_access", first))
            with sqlite3.connect(store.path, timeout=0) as lock:
                lock.execute("BEGIN IMMEDIATE")
                self.assertTrue(store.validate("file_access", first))
                self.assertFalse(store.consume_confirmation("purge", "payload", "token"))
                with self.assertRaises(sqlite3.OperationalError):
                    store.create("file_access", 60)
            self.assertTrue(store.validate("file_access", first))

    def test_shared_session_readiness_checks_writer_schema_and_private_path(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("portal-web")
            from app.services.session_store import SQLiteSessionStore
            store = SQLiteSessionStore(Path(tempdir).resolve() / "auth" / "sessions.sqlite3")
            self.assertTrue(store.ready())
            self.assertTrue(store.ready())
            with sqlite3.connect(store.path, timeout=0) as lock:
                lock.execute("BEGIN IMMEDIATE")
                self.assertFalse(store.ready())
            with sqlite3.connect(store.path) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
                connection.execute("DROP TABLE confirmations")
                connection.execute("CREATE TABLE confirmations (wrong TEXT)")
            self.assertFalse(store.ready())
            store.path.chmod(0o644)
            self.assertFalse(store.ready())

    def test_shared_session_rejects_untrusted_sidecars_and_writable_parent(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("portal-web")
            from app.services.session_store import SQLiteSessionStore
            root = Path(tempdir).resolve()
            store = SQLiteSessionStore(root / "sessions.sqlite3")
            self.assertTrue(store.ready())
            target = root / "unrelated"
            target.write_bytes(b"preserve")
            for suffix in ("-wal", "-shm", "-journal"):
                sidecar = Path(f"{store.path}{suffix}")
                sidecar.symlink_to(target)
                self.assertFalse(store.ready())
                self.assertFalse(store.validate("file_access", "token"))
                sidecar.unlink()
            root.chmod(0o777)
            self.assertFalse(store.ready())
            root.chmod(0o700)
            self.assertEqual(target.read_bytes(), b"preserve")

    def test_shared_session_path_rejects_symlinks_public_files_and_relative_paths(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("portal-web")
            from app.services.session_store import SQLiteSessionStore
            root = Path(tempdir).resolve()
            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            public = root / "public.sqlite3"
            public.touch(mode=0o644)
            file_link = root / "file-link"
            file_link.symlink_to(public)
            for path in (link / "sessions.sqlite3", public, file_link, Path("relative.sqlite3"), public / "child"):
                store = SQLiteSessionStore(path)
                with self.assertRaises((ValueError, OSError)):
                    store.create("file_access", 60)
                self.assertFalse(store.validate("file_access", "token"))
            self.assertEqual(list(target.iterdir()), [])
            self.assertEqual(public.read_bytes(), b"")

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

    def test_security_event_preserves_full_kst_timestamp_in_log_and_returns_compact_display(self):
        """Fails if audit-log storage is reduced to the compact UI timestamp."""
        with tempfile.TemporaryDirectory() as tempdir:
            security = self.reload_security(tempdir)

            security.append_security_event("test_event")

            stored_event = json.loads(
                security._daily_log_path(datetime.now(security.LOG_TIMEZONE)).read_text(
                    encoding="utf-8"
                )
            )
            events = security.read_recent_events()

            self.assertRegex(
                stored_event["timestamp"],
                r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} KST$",
            )
            self.assertRegex(events[0]["timestamp"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_read_recent_events_normalizes_legacy_kst_timestamp(self):
        with tempfile.TemporaryDirectory() as tempdir:
            security = self.reload_security(tempdir)
            legacy_event = {
                "timestamp": "2026-07-09 10:02:03 KST",
                "event": "legacy_event",
                "details": {},
            }
            security._daily_log_path(datetime.now(security.LOG_TIMEZONE)).write_text(
                json.dumps(legacy_event) + "\n",
                encoding="utf-8",
            )

            events = security.read_recent_events()

            self.assertEqual(events[0]["timestamp"], "2026-07-09 10:02")


if __name__ == "__main__":
    unittest.main()
