import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._test_support import prepare_service_import


class NewsCollectionStatusStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._baseline_path = list(sys.path)
        cls._baseline_modules = {
            name: module for name, module in sys.modules.items()
            if name == "app" or name.startswith("app.")
        }

    def tearDown(self):
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.modules.update(self._baseline_modules)
        sys.path[:] = self._baseline_path

    def load_module(self):
        prepare_service_import("crawler-worker")
        sys.modules.pop("app.services.news_collection_status", None)
        return importlib.import_module("app.services.news_collection_status")

    def test_status_is_restart_safe_and_secret_free(self):
        module = self.load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "news_collection_status.json"
            first = module.NewsCollectionStatusStore(path)
            first.record_attempt()
            first.record_failure()
            first.record_failure()

            second = module.NewsCollectionStatusStore(path)
            snapshot = second.snapshot()

            self.assertEqual(snapshot["failures_total"], 2)
            self.assertEqual(snapshot["consecutive_failures"], 2)
            self.assertTrue(snapshot["initialized"])
            self.assertIsNotNone(snapshot["first_attempt_at"])
            self.assertNotIn("token", json.dumps(snapshot).lower())
            self.assertNotIn("url", json.dumps(snapshot).lower())
            self.assertNotIn("exception", json.dumps(snapshot).lower())

    def test_success_records_empty_collection_as_success_and_resets_consecutive_failures(self):
        module = self.load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            store = module.NewsCollectionStatusStore(Path(tmpdir) / "status.json")
            store.record_failure()
            store.record_success()
            snapshot = store.snapshot()

            self.assertEqual(snapshot["failures_total"], 1)
            self.assertEqual(snapshot["consecutive_failures"], 0)
            self.assertIsNotNone(snapshot["last_success_at"])


class NewsCollectionStatusIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._baseline_path = list(sys.path)
        cls._baseline_modules = {
            name: module for name, module in sys.modules.items()
            if name == "app" or name.startswith("app.")
        }

    def tearDown(self):
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.modules.update(self._baseline_modules)
        sys.path[:] = self._baseline_path

    def test_source_exception_uses_public_fallback_and_records_failure(self):
        prepare_service_import("crawler-worker")
        import app.services.news_archive as news_archive

        with tempfile.TemporaryDirectory() as tmpdir:
            status_path = Path(tmpdir) / "status.json"
            with patch.dict(
                "os.environ",
                {
                    "NEWS_ARCHIVE_PATH": str(Path(tmpdir) / "archive.json"),
                    "NEWS_COLLECTION_STATUS_PATH": str(status_path),
                },
                clear=False,
            ), patch.object(
                news_archive,
                "collect_korean_news_from_sources",
                side_effect=RuntimeError("secret article URL and token"),
            ):
                result = news_archive.collect_korean_news("KR_WORLD", force_refresh=True)
                self.assertEqual(result["articles"], [])

            payload = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["failures_total"], 1)
            self.assertEqual(payload["consecutive_failures"], 1)
            self.assertNotIn("secret", status_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
