import importlib
import os
import tempfile
import threading
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from tests._test_support import prepare_service_import


class _FileAreaStructureParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.class_stack: list[set[str]] = []
        self.role_stack: list[str | None] = []
        self.empty_state_inside_drop_zone = False
        self.has_file_browser = False
        self.listbox_inside_drop_zone = False
        self.listbox_has_non_option_child = False

    def handle_starttag(self, tag, attrs):
        classes = set(dict(attrs).get("class", "").split())
        role = dict(attrs).get("role")
        if "file-browser" in classes:
            self.has_file_browser = True
        if self.role_stack and self.role_stack[-1] == "listbox" and role != "option":
            self.listbox_has_non_option_child = True
        if "empty-state" in classes:
            self.empty_state_inside_drop_zone = any(
                "drop-zone" in ancestor_classes
                for ancestor_classes in self.class_stack
            )
        if role == "listbox":
            self.listbox_inside_drop_zone = any(
                "drop-zone" in ancestor_classes
                for ancestor_classes in self.class_stack
            )
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.class_stack.append(classes)
            self.role_stack.append(role)

    def handle_endtag(self, tag):
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.class_stack.pop()
            self.role_stack.pop()


class FileAccessTests(unittest.TestCase):
    _ENV_KEYS = (
        "APP_ENV",
        "FILE_MANAGER_ACCESS_PASSWORD",
        "FILE_MANAGER_AUTH_REQUIRED",
        "FILE_STORAGE_PATH",
        "AUTH_RATE_LIMIT_STATE_PATH",
        "SECURITY_LOG_PATH",
    )

    def setUp(self):
        self._environment = {key: os.environ.get(key) for key in self._ENV_KEYS}

    def tearDown(self):
        for key, value in self._environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_empty_file_area_keeps_drop_message_inside_event_zone(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("portal-web")
            os.environ["FILE_MANAGER_ACCESS_PASSWORD"] = "test-file-password"
            os.environ["FILE_MANAGER_AUTH_REQUIRED"] = "true"
            storage_path = Path(tempdir) / "files"
            os.environ["FILE_STORAGE_PATH"] = str(storage_path)
            storage_path.mkdir()

            import app.main as main
            from fastapi.testclient import TestClient

            app = importlib.reload(main).app
            with TestClient(app) as client:
                client.post(
                    "/files/login",
                    data={"password": "test-file-password", "next_path": ""},
                    follow_redirects=False,
                )
                files_page = client.get("/files")

            parser = _FileAreaStructureParser()
            parser.feed(files_page.text)

            self.assertEqual(files_page.status_code, 200)
            self.assertTrue(parser.empty_state_inside_drop_zone)
            self.assertTrue(parser.has_file_browser)
            self.assertTrue(parser.listbox_inside_drop_zone)
            self.assertFalse(parser.listbox_has_non_option_child)

    def test_authenticated_file_area_exposes_explorer_controls(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("portal-web")
            os.environ["FILE_MANAGER_ACCESS_PASSWORD"] = "test-file-password"
            os.environ["FILE_MANAGER_AUTH_REQUIRED"] = "true"
            storage_path = Path(tempdir) / "files"
            os.environ["FILE_STORAGE_PATH"] = str(storage_path)
            storage_path.mkdir()
            (storage_path / "계획서.txt").write_text("test", encoding="utf-8")
            (storage_path / "자료").mkdir()

            import app.main as main
            from fastapi.testclient import TestClient

            app = importlib.reload(main).app
            with TestClient(app) as client:
                client.post(
                    "/files/login",
                    data={"password": "test-file-password", "next_path": ""},
                    follow_redirects=False,
                )

                files_page = client.get("/files")

            self.assertEqual(files_page.status_code, 200)
            self.assertIn('role="toolbar"', files_page.text)
            self.assertIn('aria-label="파일 명령"', files_page.text)
            self.assertIn('role="group" aria-label="선택 항목 작업"', files_page.text)
            self.assertIn('id="file-search"', files_page.text)
            self.assertIn('id="file-sort"', files_page.text)
            self.assertIn('data-view-mode="icons"', files_page.text)
            self.assertIn('data-view-mode="list"', files_page.text)
            parser = _FileAreaStructureParser()
            parser.feed(files_page.text)
            self.assertTrue(parser.has_file_browser)
            self.assertIn('class="drop-overlay" aria-hidden="true"', files_page.text)
            self.assertIn('role="option"', files_page.text)
            self.assertIn('data-modified="', files_page.text)

    def test_file_area_requires_separate_password_and_sets_session_cookie(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("portal-web")
            os.environ["FILE_MANAGER_ACCESS_PASSWORD"] = "test-file-password"
            os.environ["FILE_MANAGER_AUTH_REQUIRED"] = "true"
            os.environ["FILE_STORAGE_PATH"] = str(Path(tempdir) / "files")
            import app.main as main
            from fastapi.testclient import TestClient

            app = importlib.reload(main).app
            with TestClient(app) as client:
                login_page = client.get("/files")
                self.assertEqual(login_page.status_code, 200)
                self.assertIn("FILE VAULT", login_page.text)

                failed = client.post(
                    "/files/login",
                    data={"password": "wrong", "next_path": ""},
                    follow_redirects=False,
                )
                self.assertEqual(failed.status_code, 403)

                logged_in = client.post(
                    "/files/login",
                    data={"password": "test-file-password", "next_path": ""},
                    follow_redirects=False,
                )
                self.assertEqual(logged_in.status_code, 303)
                self.assertIn("file_manager_access", logged_in.headers["set-cookie"])

                files_page = client.get("/files")
                self.assertEqual(files_page.status_code, 200)
                self.assertIn("저장소", files_page.text)

    def test_production_file_login_uses_secure_server_side_session_cookie(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("portal-web")
            os.environ["APP_ENV"] = "production"
            os.environ["FILE_MANAGER_ACCESS_PASSWORD"] = "test-file-password"
            os.environ["FILE_STORAGE_PATH"] = str(Path(tempdir) / "files")

            import app.main as main
            from fastapi.testclient import TestClient

            app = importlib.reload(main).app
            with TestClient(app, base_url="https://len.pe.kr") as client:
                first_login = client.post(
                    "/files/login",
                    data={"password": "test-file-password", "next_path": ""},
                    follow_redirects=False,
                )
                second_login = client.post(
                    "/files/login",
                    data={"password": "test-file-password", "next_path": ""},
                    follow_redirects=False,
                )

            first_cookie = first_login.headers["set-cookie"].lower()
            second_cookie = second_login.headers["set-cookie"].lower()
            self.assertIn("file_manager_access=", first_cookie)
            self.assertIn("httponly", first_cookie)
            self.assertIn("secure", first_cookie)
            self.assertIn("samesite=lax", first_cookie)
            self.assertIn("path=/files", first_cookie)
            self.assertNotEqual(first_cookie.split(";", 1)[0], second_cookie.split(";", 1)[0])

    def test_file_session_is_rejected_after_security_service_restart(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("portal-web")
            os.environ["FILE_MANAGER_ACCESS_PASSWORD"] = "test-file-password"
            os.environ["FILE_MANAGER_AUTH_REQUIRED"] = "true"
            os.environ["FILE_STORAGE_PATH"] = str(Path(tempdir) / "files")

            import app.main as main
            import app.services.security as security
            from fastapi.testclient import TestClient

            app = importlib.reload(main).app
            with TestClient(app) as client:
                client.post(
                    "/files/login",
                    data={"password": "test-file-password", "next_path": ""},
                    follow_redirects=False,
                )
                self.assertEqual(client.get("/files").status_code, 200)

                importlib.reload(security)
                after_restart = client.get("/files")

            self.assertEqual(after_restart.status_code, 200)
            self.assertIn("FILE VAULT", after_restart.text)

    def test_file_manager_policy_allows_passwordless_local_development_only(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("portal-web")
            os.environ.pop("APP_ENV", None)
            os.environ.pop("FILE_MANAGER_AUTH_REQUIRED", None)
            os.environ["FILE_STORAGE_PATH"] = str(Path(tempdir) / "files")

            import app.main as main
            from fastapi.testclient import TestClient

            app = importlib.reload(main).app
            with TestClient(app) as client:
                local_response = client.get("/files")

            os.environ["APP_ENV"] = "production"
            app = importlib.reload(main).app
            with TestClient(app) as client:
                production_response = client.get("/files")

            self.assertEqual(local_response.status_code, 200)
            self.assertIn("저장소", local_response.text)
            self.assertEqual(production_response.status_code, 403)
            self.assertIn("파일함 비밀번호가 설정되지 않았습니다.", production_response.text)

    def test_concurrent_failed_logins_reject_attempts_after_rate_limit(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("portal-web")
            os.environ["FILE_MANAGER_ACCESS_PASSWORD"] = "test-file-password"
            os.environ["FILE_MANAGER_AUTH_REQUIRED"] = "true"
            os.environ["FILE_STORAGE_PATH"] = str(Path(tempdir) / "files")
            os.environ["AUTH_RATE_LIMIT_STATE_PATH"] = str(Path(tempdir) / "auth-rate-limit.json")
            os.environ["SECURITY_LOG_PATH"] = str(Path(tempdir) / "security-events.txt")

            import app.main as main
            import app.routers.files as files
            from fastapi.testclient import TestClient

            app = importlib.reload(main).app
            concurrent_attempts = 6
            simultaneous_precheck = threading.Barrier(concurrent_attempts)
            response_codes: list[int] = []
            response_lock = threading.Lock()
            original_rate_limited = files.auth_rate_limited

            def delayed_rate_limited(scope: str, identifier: str) -> bool:
                simultaneous_precheck.wait(timeout=5)
                return original_rate_limited(scope, identifier)

            def submit_wrong_password() -> None:
                with TestClient(app) as client:
                    response = client.post(
                        "/files/login",
                        data={"password": "wrong", "next_path": ""},
                        headers={"x-forwarded-for": "203.0.113.7"},
                        follow_redirects=False,
                    )
                with response_lock:
                    response_codes.append(response.status_code)

            with patch.object(files, "auth_rate_limited", side_effect=delayed_rate_limited):
                attempts = [threading.Thread(target=submit_wrong_password) for _ in range(concurrent_attempts)]
                for attempt in attempts:
                    attempt.start()
                for attempt in attempts:
                    attempt.join(timeout=10)

            self.assertTrue(all(not attempt.is_alive() for attempt in attempts))
            self.assertEqual(response_codes.count(403), 5)
            self.assertEqual(response_codes.count(429), 1)


if __name__ == "__main__":
    unittest.main()
