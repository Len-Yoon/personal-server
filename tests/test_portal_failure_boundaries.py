import importlib
import os
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from tests._test_support import prepare_service_import


class PortalFailureBoundaryTests(unittest.TestCase):
    @contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {
                "APP_ENV": "development", "FILE_STORAGE_PATH": str(root / "files"),
                "FILE_MANAGER_AUTH_REQUIRED": "false", "DELETE_PASSWORD": "fixture-delete",
                "SECURITY_LOG_PATH": str(root / "security.log"),
                "AUTH_RATE_LIMIT_STATE_PATH": str(root / "auth.json"),
            }):
                prepare_service_import("portal-web")
                from app.services import file_store, security
                from app import main
                importlib.reload(file_store)
                importlib.reload(security)
                from fastapi.testclient import TestClient
                file_store.ensure_storage()
                with TestClient(importlib.reload(main).app, raise_server_exceptions=False) as client:
                    yield client, file_store, root / "files"

    def test_bulk_delete_invalid_later_item_does_not_delete_earlier_item(self):
        with self.fixture() as (client, store, root):
            (root / "a.txt").write_text("a")
            response = client.post("/files/delete-bulk", data={
                "paths": ["a.txt", "missing.txt"], "delete_password": "fixture-delete",
            }, headers={"Origin": "http://testserver"})
            self.assertEqual(response.status_code, 400)
            self.assertTrue((root / "a.txt").exists())

    def test_bulk_delete_reports_race_after_first_deletion(self):
        with self.fixture() as (client, store, root):
            for name in ("a.txt", "b.txt"):
                (root / name).write_text(name)
            original = store.delete_item

            def delete(path):
                if path == "b.txt":
                    raise FileNotFoundError("fixture race")
                original(path)

            with patch.object(store, "delete_item", side_effect=delete):
                response = client.post("/files/delete-bulk", data={
                    "paths": ["a.txt", "b.txt"], "delete_password": "fixture-delete",
                }, headers={"Origin": "http://testserver"})
            self.assertEqual(response.status_code, 207)
            self.assertEqual(response.json()["deleted"], ["a.txt"])
            self.assertEqual(response.json()["failed"], ["b.txt"])
            self.assertTrue((root / "b.txt").exists())

    def test_missing_bulk_download_is_not_server_error(self):
        with self.fixture() as (client, _, __):
            response = client.post("/files/download-bulk", data={"paths": ["missing.txt"]},
                                   headers={"Origin": "http://testserver"})
            self.assertEqual(response.status_code, 404)

    def test_audit_log_failure_does_not_report_deleted_file_as_remaining(self):
        with self.fixture() as (client, store, root):
            (root / "a.txt").write_text("a")
            with patch.object(store, "append_security_event", side_effect=OSError("fixture log full")):
                response = client.post("/files/delete-bulk", data={
                    "paths": ["a.txt"], "delete_password": "fixture-delete",
                }, headers={"Origin": "http://testserver"})
            self.assertEqual(response.status_code, 207)
            self.assertFalse((root / "a.txt").exists())
            self.assertEqual(response.json()["deleted"], ["a.txt"])
            self.assertEqual(response.json()["failed"], [])
            self.assertFalse(response.json()["audit_recorded"])

    def test_hidden_upload_and_folder_names_are_rejected_before_write(self):
        with self.fixture() as (client, _, root):
            upload = client.post("/files/upload", files={"upload": (".report.txt", b"report")},
                                 headers={"Origin": "http://testserver"}, follow_redirects=False)
            folder = client.post("/files/folders", data={"name": ".draft"},
                                 headers={"Origin": "http://testserver"}, follow_redirects=False)
            self.assertEqual(upload.status_code, 400)
            self.assertEqual(folder.status_code, 400)
            self.assertFalse((root / ".report.txt").exists())
            self.assertFalse((root / ".draft").exists())

    def test_portfolio_editor_uses_draft_preserving_submit_handler(self):
        template = Path(__file__).resolve().parents[1] / "portal-web/app/templates/portfolio_editor.html"
        self.assertIn('/static/js/portfolio-drafts.js', template.read_text())

    def test_portfolio_draft_client_behavior_is_checked_in_portal_ci(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["node", "--test", "tests/portfolio_drafts_client.test.mjs"],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
