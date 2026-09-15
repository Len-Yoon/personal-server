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
        "FILE_MAX_UPLOAD_FILES",
        "FILE_MAX_UPLOAD_TOTAL_MB",
        "FILE_MAX_DOWNLOAD_FILES",
        "FILE_MAX_DOWNLOAD_TOTAL_MB",
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
                    headers={"Origin": "http://testserver"},
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
                    headers={"Origin": "http://testserver"},
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
                    headers={"Origin": "http://testserver"},
                    follow_redirects=False,
                )
                self.assertEqual(failed.status_code, 403)

                logged_in = client.post(
                    "/files/login",
                    data={"password": "test-file-password", "next_path": ""},
                    headers={"Origin": "http://testserver"},
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
                    headers={"Origin": "https://len.pe.kr"},
                    follow_redirects=False,
                )
                second_login = client.post(
                    "/files/login",
                    data={"password": "test-file-password", "next_path": ""},
                    headers={"Origin": "https://len.pe.kr"},
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
                    headers={"Origin": "http://testserver"},
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
                        headers={"Origin": "http://testserver", "x-forwarded-for": "203.0.113.7"},
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

    def test_file_mutation_rejects_cross_origin_session_request_and_accepts_same_origin_form(self):
        """Fails if a session cookie can authorize a file mutation from another origin."""
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("portal-web")
            os.environ["FILE_MANAGER_ACCESS_PASSWORD"] = "test-file-password"
            os.environ["FILE_MANAGER_AUTH_REQUIRED"] = "true"
            storage_path = Path(tempdir) / "files"
            storage_path.mkdir()
            os.environ["FILE_STORAGE_PATH"] = str(storage_path)
            import app.main as main
            from fastapi.testclient import TestClient

            app = importlib.reload(main).app
            with TestClient(app, base_url="https://file.len.pe.kr") as client:
                same_origin = {"Origin": "https://file.len.pe.kr"}
                login = client.post(
                    "/files/login",
                    data={"password": "test-file-password", "next_path": ""},
                    headers=same_origin,
                    follow_redirects=False,
                )
                rejected = client.post(
                    "/files/folders",
                    data={"path": "", "name": "cross-origin"},
                    headers={"Origin": "https://attacker.example"},
                )
                accepted = client.post(
                    "/files/folders",
                    data={"path": "", "name": "same-origin"},
                    headers=same_origin,
                    follow_redirects=False,
                )

                same_origin_created = (storage_path / "same-origin").is_dir()
                cross_origin_created = (storage_path / "cross-origin").exists()

        self.assertEqual(login.status_code, 303)
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(accepted.status_code, 303)
        self.assertTrue(same_origin_created)
        self.assertFalse(cross_origin_created)

    def test_bulk_upload_rejects_too_many_files_without_saving_any(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("portal-web")
            os.environ.pop("APP_ENV", None)
            os.environ.pop("FILE_MANAGER_AUTH_REQUIRED", None)
            os.environ["FILE_STORAGE_PATH"] = str(Path(tempdir) / "files")
            os.environ["FILE_MAX_UPLOAD_FILES"] = "1"
            import app.main as main
            from fastapi.testclient import TestClient

            app = importlib.reload(main).app
            with TestClient(app) as client:
                response = client.post(
                    "/files/uploads",
                    files=[
                        ("uploads", ("one.txt", b"1", "text/plain")),
                        ("uploads", ("two.txt", b"2", "text/plain")),
                    ],
                    data={"path": ""},
                    headers={"Origin": "http://testserver"},
                )

            self.assertEqual(response.status_code, 400)
            self.assertFalse((Path(tempdir) / "files").exists())

    def test_bulk_upload_cleans_previous_files_when_total_limit_is_exceeded(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("portal-web")
            os.environ.pop("APP_ENV", None)
            os.environ.pop("FILE_MANAGER_AUTH_REQUIRED", None)
            storage_path = Path(tempdir) / "files"
            os.environ["FILE_STORAGE_PATH"] = str(storage_path)
            os.environ["FILE_MAX_UPLOAD_TOTAL_MB"] = "1"
            import app.main as main
            from fastapi.testclient import TestClient

            app = importlib.reload(main).app
            with TestClient(app) as client:
                response = client.post(
                    "/files/uploads",
                    files=[
                        ("uploads", ("one.txt", b"a" * (600 * 1024), "text/plain")),
                        ("uploads", ("two.txt", b"b" * (500 * 1024), "text/plain")),
                    ],
                    data={"path": ""},
                    headers={"Origin": "http://testserver"},
                )

            self.assertEqual(response.status_code, 400)
            self.assertEqual(list(storage_path.iterdir()), [])

    def test_bulk_download_rejects_file_count_before_creating_archive(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("portal-web")
            os.environ.pop("APP_ENV", None)
            os.environ.pop("FILE_MANAGER_AUTH_REQUIRED", None)
            storage_path = Path(tempdir) / "files"
            storage_path.mkdir()
            (storage_path / "one.txt").write_text("1")
            (storage_path / "two.txt").write_text("2")
            os.environ["FILE_STORAGE_PATH"] = str(storage_path)
            os.environ["FILE_MAX_DOWNLOAD_FILES"] = "1"
            archives_before = set(Path(tempfile.gettempdir()).glob("file-vault-*.zip"))
            import app.main as main
            from fastapi.testclient import TestClient

            app = importlib.reload(main).app
            with TestClient(app) as client:
                response = client.post(
                    "/files/download-bulk",
                    data={"paths": ["one.txt", "two.txt"]},
                    headers={"Origin": "http://testserver"},
                )

            self.assertEqual(response.status_code, 400)
            self.assertEqual(set(Path(tempfile.gettempdir()).glob("file-vault-*.zip")), archives_before)

    def test_bulk_download_rejects_original_total_size(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("portal-web")
            os.environ.pop("APP_ENV", None)
            os.environ.pop("FILE_MANAGER_AUTH_REQUIRED", None)
            storage_path = Path(tempdir) / "files"
            storage_path.mkdir()
            (storage_path / "large.txt").write_bytes(b"x" * (2 * 1024 * 1024))
            os.environ["FILE_STORAGE_PATH"] = str(storage_path)
            os.environ["FILE_MAX_DOWNLOAD_TOTAL_MB"] = "1"
            import app.main as main
            from fastapi.testclient import TestClient

            app = importlib.reload(main).app
            with TestClient(app) as client:
                response = client.post(
                    "/files/download-bulk",
                    data={"paths": "large.txt"},
                    headers={"Origin": "http://testserver"},
                )

            self.assertEqual(response.status_code, 400)

    def test_upload_removes_file_when_security_event_recording_fails(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("portal-web")
            os.environ["FILE_STORAGE_PATH"] = str(Path(tempdir) / "files")
            import app.services.file_store as file_store

            class _Upload:
                filename = "event-failure.txt"
                content_type = "text/plain"

                class _File:
                    _read = False

                    def read(self, size):
                        if self._read:
                            return b""
                        self._read = True
                        return b"content"

                file = _File()

            with patch.object(file_store, "append_security_event", side_effect=OSError("log unavailable")):
                with self.assertRaises(OSError):
                    file_store.save_upload("", _Upload())

            self.assertFalse((Path(tempdir) / "files" / "event-failure.txt").exists())

    def test_bulk_download_removes_archive_when_zip_creation_fails(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("portal-web")
            os.environ.pop("APP_ENV", None)
            os.environ.pop("FILE_MANAGER_AUTH_REQUIRED", None)
            storage_path = Path(tempdir) / "files"
            storage_path.mkdir()
            (storage_path / "one.txt").write_text("1")
            os.environ["FILE_STORAGE_PATH"] = str(storage_path)
            archives_before = set(Path(tempfile.gettempdir()).glob("file-vault-*.zip"))
            import app.main as main
            import app.routers.files as files_router
            from fastapi.testclient import TestClient

            app = importlib.reload(main).app
            with TestClient(app) as client:
                with patch.object(files_router.zipfile, "ZipFile", side_effect=OSError("disk full")):
                    response = client.post(
                        "/files/download-bulk",
                        data={"paths": "one.txt"},
                        headers={"Origin": "http://testserver"},
                    )

            self.assertEqual(response.status_code, 400)
            self.assertEqual(set(Path(tempfile.gettempdir()).glob("file-vault-*.zip")), archives_before)


if __name__ == "__main__":
    unittest.main()
