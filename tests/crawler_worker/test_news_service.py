import importlib
import sys
import types
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from tests._test_support import prepare_service_import


class CrawlerWorkerNewsServiceTests(unittest.TestCase):
    def reload_news_service(self):
        prepare_service_import("crawler-worker")
        sys.modules.setdefault("feedparser", types.ModuleType("feedparser"))
        import app.services.news_service as news_service

        return importlib.reload(news_service)

    def reload_news_archive(self):
        prepare_service_import("crawler-worker")
        import app.services.news_archive as news_archive

        return importlib.reload(news_archive)

    def tearDown(self):
        sys.modules.pop("feedparser", None)

    def test_collect_market_news_uses_cache_for_repeat_requests(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "news_archive.json"
            with patch.dict(
                "os.environ",
                {
                    "NEWS_ARCHIVE_PATH": str(archive_path),
                    "NEWS_REFRESH_INTERVAL_SECONDS": "3600",
                    "NEWS_RETENTION_DAYS": "7",
                },
                clear=False,
            ):
                news_archive = self.reload_news_archive()

                with patch(
                    "app.services.news_sources.search_reuters_news_rss",
                    return_value=[{"url": "https://example.com/r", "title": "R"}],
                ) as mocked_reuters, patch(
                    "app.services.news_sources.search_investing_news",
                    return_value=[{"url": "https://example.com/a", "title": "A"}],
                ) as mocked_investing, patch(
                    "app.services.news_sources.search_ap_news_rss",
                    return_value=[{"url": "https://example.com/b", "title": "B"}],
                ) as mocked_ap, patch(
                    "app.services.news_sources.search_marketwatch_news_rss",
                    return_value=[{"url": "https://example.com/c", "title": "C"}],
                ) as mocked_marketwatch:
                    first = news_archive.collect_market_news("world", limit=1)
                    second = news_archive.collect_market_news("world", limit=1)

        self.assertFalse(first["cache"]["hit"])
        self.assertTrue(second["cache"]["hit"])
        self.assertEqual(mocked_reuters.call_count, 1)
        self.assertEqual(mocked_investing.call_count, 1)
        self.assertEqual(mocked_ap.call_count, 1)
        self.assertEqual(mocked_marketwatch.call_count, 1)

    def test_collect_market_news_uses_investing_news(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "news_archive.json"
            with patch.dict(
                "os.environ",
                {
                    "NEWS_ARCHIVE_PATH": str(archive_path),
                    "NEWS_REFRESH_INTERVAL_SECONDS": "3600",
                    "NEWS_RETENTION_DAYS": "7",
                },
                clear=False,
            ):
                news_archive = self.reload_news_archive()

                with patch(
                    "app.services.news_sources.search_reuters_news_rss",
                    return_value=[],
                ), patch(
                    "app.services.news_sources.search_investing_news",
                    return_value=[
                        {
                            "url": "https://kr.investing.com/news/commodities-news/gold-1",
                            "title": "금 가격, 연준 의사록 앞두고 보합세",
                            "title_ko": "금 가격, 연준 의사록 앞두고 보합세",
                            "title_original": "금 가격, 연준 의사록 앞두고 보합세",
                            "source": "Investing.com",
                            "published_at": "2시간 전",
                            "provider": "Investing.com KR",
                        }
                    ],
                ) as mocked_search, patch(
                    "app.services.news_sources.search_ap_news_rss",
                    return_value=[],
                ), patch(
                    "app.services.news_sources.search_marketwatch_news_rss",
                    return_value=[],
                ):
                    result = news_archive.collect_market_news("gold", limit=1, force_refresh=True)

        self.assertEqual(result["articles"][0]["provider"], "Investing.com KR")
        mocked_search.assert_called_once()

    def test_collect_market_news_returns_stale_cache_and_refreshes_in_background(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "news_archive.json"
            with patch.dict(
                "os.environ",
                {
                    "NEWS_ARCHIVE_PATH": str(archive_path),
                    "NEWS_REFRESH_INTERVAL_SECONDS": "3600",
                    "NEWS_RETENTION_DAYS": "7",
                },
                clear=False,
            ):
                news_archive = self.reload_news_archive()
                archive_path.parent.mkdir(parents=True, exist_ok=True)
                archive_path.write_text(
                    """
                    {
                      "updated_at": "2026-07-09T00:00:00+00:00",
                      "articles": [
                        {
                          "category": "WORLD",
                          "title": "A",
                          "title_ko": "A",
                          "title_original": "A",
                          "url": "https://example.com/a",
                          "source": "Investing.com",
                          "published_at": "",
                          "summary": "",
                          "provider": "Investing.com KR",
                          "collected_at": "2026-07-08T00:00:00+00:00",
                          "expires_at": "2026-07-15T00:00:00+00:00"
                        }
                      ]
                    }
                    """.strip(),
                    encoding="utf-8",
                )

                with patch.object(news_archive, "_schedule_refresh") as mocked_refresh, patch(
                    "app.services.news_sources.search_reuters_news_rss",
                    return_value=[],
                ) as mocked_reuters, patch(
                    "app.services.news_sources.search_investing_news",
                    return_value=[],
                ) as mocked_investing, patch(
                    "app.services.news_sources.search_ap_news_rss",
                    return_value=[],
                ) as mocked_ap, patch(
                    "app.services.news_sources.search_marketwatch_news_rss",
                    return_value=[],
                ) as mocked_marketwatch:
                    result = news_archive.collect_market_news("world", limit=1)

        self.assertTrue(result["cache"]["hit"])
        self.assertGreater(result["cache"]["age_seconds"], 3600)
        mocked_refresh.assert_called_once_with("WORLD", 1)
        mocked_reuters.assert_not_called()
        mocked_investing.assert_not_called()
        mocked_ap.assert_not_called()
        mocked_marketwatch.assert_not_called()

    def test_list_recent_news_orders_by_collection_time(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "news_archive.json"
            with patch.dict(
                "os.environ",
                {
                    "NEWS_ARCHIVE_PATH": str(archive_path),
                    "NEWS_REFRESH_INTERVAL_SECONDS": "3600",
                    "NEWS_RETENTION_DAYS": "7",
                },
                clear=False,
            ):
                news_archive = self.reload_news_archive()
                archive_path.parent.mkdir(parents=True, exist_ok=True)
                archive_path.write_text(
                    """
                    {
                      "updated_at": "2026-07-09T00:00:00+00:00",
                      "articles": [
                        {
                          "category": "WORLD",
                          "title": "A",
                          "title_ko": "A",
                          "title_original": "A",
                          "url": "https://example.com/a",
                          "source": "Investing.com",
                          "published_at": "",
                          "summary": "",
                          "provider": "Investing.com KR",
                          "collected_at": "2026-07-09T00:00:00+00:00",
                          "expires_at": "2026-07-16T00:00:00+00:00"
                        },
                        {
                          "category": "GOLD",
                          "title": "B",
                          "title_ko": "B",
                          "title_original": "B",
                          "url": "https://example.com/b",
                          "source": "Investing.com",
                          "published_at": "",
                          "summary": "",
                          "provider": "Investing.com KR",
                          "collected_at": "2026-07-09T01:00:00+00:00",
                          "expires_at": "2026-07-16T01:00:00+00:00"
                        }
                      ]
                    }
                    """.strip(),
                    encoding="utf-8",
                )

                recent = news_archive.list_recent_news(limit=10)

        self.assertEqual([item["url"] for item in recent], ["https://example.com/b", "https://example.com/a"])


if __name__ == "__main__":
    unittest.main()
