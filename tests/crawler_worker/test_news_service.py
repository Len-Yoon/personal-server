import importlib
import sys
import types
import unittest
from unittest.mock import patch

from tests._test_support import prepare_service_import


class CrawlerWorkerNewsServiceTests(unittest.TestCase):
    def reload_news_service(self):
        prepare_service_import("crawler-worker")
        sys.modules.setdefault("feedparser", types.ModuleType("feedparser"))
        import app.services.news_service as news_service

        return importlib.reload(news_service)

    def tearDown(self):
        sys.modules.pop("feedparser", None)

    def test_merge_articles_removes_duplicates_and_missing_urls(self):
        news_service = self.reload_news_service()

        merged = news_service._merge_articles(
            [
                {"url": "https://example.com/a", "title": "A"},
                {"url": "https://example.com/a", "title": "Duplicate"},
                {"title": "Missing url"},
                {"url": "https://example.com/b", "title": "B"},
            ]
        )

        self.assertEqual([article["url"] for article in merged], ["https://example.com/a", "https://example.com/b"])

    def test_collect_market_news_uses_cache_for_repeat_requests(self):
        news_service = self.reload_news_service()
        news_service._news_cache.clear()

        with patch(
            "app.services.news_service.search_investing_news",
            return_value=[{"url": "https://example.com/a", "title": "A"}],
        ) as mocked_search:
            first = news_service.collect_market_news("world", limit=1)
            second = news_service.collect_market_news("world", limit=1)

        self.assertFalse(first["cache"]["hit"])
        self.assertTrue(second["cache"]["hit"])
        self.assertEqual(mocked_search.call_count, 1)

    def test_collect_market_news_uses_investing_news(self):
        news_service = self.reload_news_service()
        news_service._news_cache.clear()

        with patch(
            "app.services.news_service.search_investing_news",
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
        ) as mocked_search:
            result = news_service.collect_market_news("gold", limit=1, force_refresh=True)

        self.assertEqual(result["articles"][0]["provider"], "Investing.com KR")
        mocked_search.assert_called_once()


if __name__ == "__main__":
    unittest.main()
