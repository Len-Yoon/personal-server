import importlib
import os
import tempfile
import unittest
from pathlib import Path

from tests._test_support import prepare_service_import


class FileAccessTests(unittest.TestCase):
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
