import importlib
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from jinja2 import Environment, FileSystemLoader
from tests._test_support import prepare_service_import


class NewsFailureBoundaryTests(unittest.TestCase):
    def test_real_source_transport_failure_preserves_archive_and_reports_error(self):
        prepare_service_import("crawler-worker")
        try:
            importlib.import_module("feedparser")
        except ImportError:
            self.skipTest("requires crawler-worker dependency environment")
        from app.services import news_archive
        archive = importlib.reload(news_archive)
        now = datetime(2026, 10, 6, 4, tzinfo=timezone.utc)
        article = {"url": "https://example.test/a", "title": "기존 IT 기사", "category": "KR_IT",
                   "published_at_sort": "2026-10-06T02:00:00+00:00",
                   "collected_at": "2026-10-06T02:00:00+00:00", "expires_at": "2026-10-13T02:00:00+00:00"}
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(Path(tmp) / "archive.json")}):
            archive._save_archive({"articles": [article], "updated_at": article["collected_at"]})
            archive._load_archive()
            before = archive._archive_path().read_bytes()
            with patch.object(archive, "_now", return_value=now), \
                 patch("app.crawlers.rss_news.urlopen", side_effect=URLError("fixture offline")):
                result = archive.collect_korean_news("KR_IT", force_refresh=True)
            self.assertEqual(archive._archive_path().read_bytes(), before)
        self.assertEqual(result["collection"]["status"], "error")
        self.assertTrue(result["cache"]["stale"])
        self.assertEqual(result["cache"]["age_seconds"], 7200)

    def test_failed_collection_preserves_metadata_and_explains_stale_result(self):
        prepare_service_import("crawler-worker")
        from app.services import news_archive
        archive = importlib.reload(news_archive)
        now = datetime(2026, 10, 6, 4, tzinfo=timezone.utc)
        old = now - timedelta(hours=2)
        article = {"url": "https://example.test/a", "title": "기사", "category": "KR_WORLD",
                   "published_at_sort": old.isoformat(), "collected_at": old.isoformat(),
                   "expires_at": (old + timedelta(days=7)).isoformat()}
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(Path(tmp) / "archive.json")}):
            with patch.object(archive, "_now", return_value=now):
                archive._save_archive({"articles": [article], "updated_at": old.isoformat(),
                                       "telegram_notifications_initialized": True})
                archive._load_archive()  # Existing sanitization is independent of collection failure.
                before = archive._archive_path().read_bytes()
                with patch.object(archive, "collect_korean_news_from_sources", side_effect=OSError("fixture timeout")), patch.object(archive, "notify_new_investing_articles", side_effect=AssertionError("must not send")), patch.object(archive, "notify_market_news_digest", side_effect=AssertionError("must not send")):
                    result = archive.collect_korean_news("KR_WORLD", force_refresh=True)
                after = archive._archive_path().read_bytes()
        self.assertEqual(before, after)
        self.assertTrue(result["cache"]["hit"])
        self.assertTrue(result["cache"]["stale"])
        self.assertEqual(result["cache"]["age_seconds"], 7200)
        self.assertEqual(result["collection"]["status"], "error")
        template_root = Path(__file__).resolve().parents[2] / "crawler-worker/app/templates"
        env = Environment(loader=FileSystemLoader(template_root))
        env.filters["news_datetime"] = lambda value: value
        env.globals["portal_home_url"] = lambda request: "/"
        rendered = env.get_template("search.html").render(result=result, categories=[], category_path="/category", refresh_url="/category", request=None)
        initial_view = rendered.split('<script id="news-auto-refresh">')[0]
        self.assertIn("수집 실패", initial_view)
        self.assertNotIn("방금 새로 수집", initial_view)

    def test_empty_success_is_not_reported_as_collection_failure(self):
        prepare_service_import("crawler-worker")
        from app.services import news_archive
        archive = importlib.reload(news_archive)
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(Path(tmp) / "archive.json")}):
            with patch.object(archive, "collect_korean_news_from_sources", return_value=[]):
                result = archive.collect_korean_news("KR_IT", force_refresh=True)
        self.assertEqual(result.get("collection", {}).get("status"), "success")
        self.assertFalse(result["cache"]["hit"])

    def test_stale_cached_articles_are_distinct_from_fresh_cache(self):
        prepare_service_import("crawler-worker")
        from app.services import news_archive
        archive = importlib.reload(news_archive)
        now = datetime(2026, 10, 6, 4, tzinfo=timezone.utc)
        article = {"url": "https://example.test/a", "title": "기사", "category": "KR_IT",
                   "published_at_sort": "2026-10-06T02:00:00+00:00",
                   "collected_at": "2026-10-06T02:00:00+00:00", "expires_at": "2026-10-13T02:00:00+00:00"}
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"NEWS_ARCHIVE_PATH": str(Path(tmp) / "archive.json")}):
            archive._save_archive({"articles": [article], "updated_at": article["collected_at"]})
            with patch.object(archive, "_now", return_value=now), patch.object(archive, "_schedule_refresh"):
                result = archive.collect_korean_news("KR_IT")
        self.assertTrue(result["cache"]["stale"])
        self.assertEqual(result["collection"]["status"], "cached")
