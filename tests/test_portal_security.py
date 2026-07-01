import importlib
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


class PortalSecurityTests(unittest.TestCase):
    def reload_security(self, tempdir: str):
        os.environ["SECURITY_LOG_PATH"] = str(Path(tempdir) / "security-events.txt")
        os.environ["SECURITY_LOG_TIMEZONE"] = "Asia/Seoul"
        import app.services.security as security

        return importlib.reload(security)

    def reload_file_store(self, tempdir: str):
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

    def test_append_user_event_allows_known_click_events(self):
        with tempfile.TemporaryDirectory() as tempdir:
            security = self.reload_security(tempdir)

            security.append_user_event(
                "service_opened",
                path="/",
                target="유튜브 메모장",
                href="http://memo.lenserver.com",
                client="127.0.0.1",
            )

            events = security.read_recent_events()
            self.assertEqual(events[0]["event"], "user_service_opened")
            self.assertEqual(events[0]["details"]["target"], "유튜브 메모장")


if __name__ == "__main__":
    unittest.main()
