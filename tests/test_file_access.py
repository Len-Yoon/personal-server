import importlib
import os
import tempfile
import unittest
from pathlib import Path

from tests._test_support import prepare_service_import


class FileAccessTests(unittest.TestCase):
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
            self.assertIn('class="file-browser"', files_page.text)
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
