import importlib
import os
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

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
    def test_empty_file_area_keeps_drop_message_inside_event_zone(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prepare_service_import("portal-web")
            os.environ["FILE_MANAGER_ACCESS_PASSWORD"] = "test-file-password"
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


if __name__ == "__main__":
    unittest.main()
