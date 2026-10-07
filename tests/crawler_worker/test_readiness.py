import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._test_support import prepare_service_import


class CrawlerReadinessTests(unittest.TestCase):
    def test_readiness_detects_unavailable_and_corrupt_local_state_without_mutation(self):
        prepare_service_import("crawler-worker")
        from app.services.readiness import readiness_checks
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "archive.json"
            status = Path(tmp) / "status.json"
            with patch.dict(os.environ, {"NEWS_ARCHIVE_PATH": str(archive), "NEWS_COLLECTION_STATUS_PATH": str(status)}):
                self.assertTrue(all(readiness_checks().values()))
                self.assertFalse(archive.exists())
                self.assertFalse(status.exists())
                archive.write_text('{broken', encoding="utf-8")
                self.assertFalse(readiness_checks()["archive_state"])
                self.assertEqual(archive.read_text(), '{broken')
                archive.write_text('{"articles": [], "schema_version": "future"}')
                self.assertFalse(readiness_checks()["archive_state"])
                archive.write_text('{"articles": []}')
                status.write_text('[]')
                self.assertTrue(readiness_checks()["archive_state"])
                self.assertFalse(readiness_checks()["collection_state"])
                with patch("app.services.readiness.os.access", return_value=False):
                    self.assertFalse(any(readiness_checks().values()))

    def test_readiness_matches_loader_fail_closed_structure_and_preserves_original_bytes(self):
        prepare_service_import("crawler-worker")
        from app.services.readiness import readiness_checks
        from app.services.news_archive import ARCHIVE_SCHEMA_VERSION, _load_archive
        invalid_fields = (
            {"telegram_unknown": "opaque"},
            {"telegram_pending_articles": {}},
            {"telegram_recent_articles": ["invalid"]},
            {"telegram_topic_last_sent_at": []},
            {"telegram_outbox": [{"event_id": "fixture", "unknown_field": True}]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "archive.json"
            status = Path(tmp) / "status.json"
            with patch.dict(os.environ, {"NEWS_ARCHIVE_PATH": str(archive), "NEWS_COLLECTION_STATUS_PATH": str(status)}):
                for fields in invalid_fields:
                    with self.subTest(fields=fields):
                        before = json.dumps(dict({"schema_version": ARCHIVE_SCHEMA_VERSION, "articles": []}, **fields)).encode()
                        archive.write_bytes(before)
                        self.assertFalse(readiness_checks()["archive_state"])
                        self.assertEqual(archive.read_bytes(), before)
                        with self.assertRaises(ValueError):
                            _load_archive()
                        self.assertEqual(archive.read_bytes(), before)
                # Existing malformed-outbox tolerance is preserved by both paths.
                before = json.dumps({"schema_version": ARCHIVE_SCHEMA_VERSION, "articles": [], "telegram_outbox": ["invalid"]}).encode()
                archive.write_bytes(before)
                self.assertTrue(readiness_checks()["archive_state"])
                self.assertEqual(_load_archive()["telegram_outbox"], [])
                self.assertEqual(archive.read_bytes(), before)

    def test_ready_and_health_are_separate_and_check_no_upstream(self):
        prepare_service_import("crawler-worker")
        previous = Path.cwd()
        os.chdir(Path(__file__).resolve().parents[2] / "crawler-worker")
        self.addCleanup(os.chdir, previous)
        from app.main import app
        from fastapi.testclient import TestClient
        from app.services import news_archive
        with patch("app.services.readiness.readiness_checks", return_value={"archive_state": False, "collection_state": True}), patch.object(news_archive, "collect_korean_news_from_sources", side_effect=AssertionError("no upstream")):
            client = TestClient(app)
            self.assertEqual(client.get("/health").status_code, 200)
            response = client.get("/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "not_ready")
