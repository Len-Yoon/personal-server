import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._test_support import prepare_service_import


class NewsCollectionMetricsTests(unittest.TestCase):
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

    def load_modules(self):
        prepare_service_import("crawler-worker")
        for name in (
            "app.services.news_collection_metrics",
            "app.services.news_collection_status",
        ):
            sys.modules.pop(name, None)
        return (
            importlib.import_module("app.services.news_collection_metrics"),
            importlib.import_module("app.services.news_collection_status"),
        )

    def test_exposition_contains_fixed_metrics_without_sensitive_values(self):
        metrics, status = self.load_modules()
        with tempfile.TemporaryDirectory() as tmpdir:
            store = status.NewsCollectionStatusStore(Path(tmpdir) / "status.json")
            store.record_attempt()
            store.record_success()
            body = metrics.render_metrics(store.snapshot())
            self.assertIn("crawler_news_collection_initialized 1", body)
            self.assertIn("crawler_news_collection_consecutive_failures 0", body)
            self.assertNotIn("url", body.lower())
            self.assertNotIn("token", body.lower())

    def test_internal_endpoint_requires_exact_bearer_token(self):
        previous_path = list(sys.path)
        previous_modules = {
            name: module for name, module in sys.modules.items()
            if name == "app" or name.startswith("app.")
        }
        prepare_service_import("crawler-worker")
        import importlib
        from fastapi.testclient import TestClient

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                with patch.dict(
                    os.environ,
                    {
                        "NEWS_METRICS_BEARER_TOKEN": "metrics-secret",
                        "NEWS_COLLECTION_STATUS_PATH": str(Path(tmpdir) / "status.json"),
                    },
                    clear=False,
                ):
                    sys.modules.pop("app.main", None)
                    previous_cwd = Path.cwd()
                    os.chdir(Path(__file__).resolve().parents[2] / "crawler-worker")
                    try:
                        app = importlib.import_module("app.main").app
                    finally:
                        os.chdir(previous_cwd)
                    with TestClient(app) as client:
                        self.assertEqual(client.get("/internal/metrics").status_code, 404)
                        self.assertEqual(
                            client.get("/internal/metrics", headers={"Authorization": "Bearer wrong"}).status_code,
                            404,
                        )
                        response = client.get(
                            "/internal/metrics", headers={"Authorization": "Bearer metrics-secret"}
                        )
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.headers["content-type"], "text/plain; version=0.0.4")
        finally:
            for name in list(sys.modules):
                if name == "app" or name.startswith("app."):
                    del sys.modules[name]
            sys.modules.update(previous_modules)
            sys.path[:] = previous_path

    def test_metrics_and_collection_share_archive_parent_status_path(self):
        metrics, status = self.load_modules()
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "news_archive.json"
            with patch.dict(
                "os.environ",
                {"NEWS_ARCHIVE_PATH": str(archive_path)},
                clear=False,
            ):
                store = status.NewsCollectionStatusStore()
                store.record_attempt()
                store.record_success()
                self.assertEqual(
                    store.snapshot()["initialized"],
                    1,
                )
                self.assertIn(
                    "crawler_news_collection_initialized 1",
                    metrics.render_metrics(store.snapshot()),
                )
                self.assertTrue((archive_path.parent / "news_collection_status.json").exists())


if __name__ == "__main__":
    unittest.main()
