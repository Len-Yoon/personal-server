import importlib
import sys
import types
import unittest
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from tests._test_support import prepare_service_import


class InvestingNewsRssTests(unittest.TestCase):
    def reload_module(self):
        prepare_service_import("crawler-worker")
        sys.modules.setdefault("feedparser", types.ModuleType("feedparser"))
        import app.crawlers.investing_news_rss as module

        return importlib.reload(module)

    def tearDown(self):
        sys.modules.pop("feedparser", None)

    def test_today_filter_uses_korean_date(self):
        module = self.reload_module()

        self.assertTrue(module._is_today("2026-07-13 03:59:20", today=date(2026, 7, 13)))
        self.assertFalse(module._is_today("2026-07-12 12:00:00", today=date(2026, 7, 13)))

    def test_search_investing_news_rss_keeps_only_investing_korean_source(self):
        module = self.reload_module()
        today_kst = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
        direct_articles = [
            {
                "title": "나스닥 최신 Investing 뉴스 - Investing.com 한국어",
                "url": "https://kr.investing.com/news/articles/1",
                "source": "Investing.com 한국어",
                "provider": "Investing.com RSS",
                "published_at": f"{today_kst} 12:00:00",
            },
            {
                "title": "By Investing.com",
                "url": "https://kr.investing.com/news/articles/3",
                "source": "Investing.com 한국어",
            },
        ]
        fallback_articles = [
            {
                "title": "일본 증시 보충 뉴스 - Investing.com 한국어",
                "url": "https://news.google.com/rss/articles/4",
                "source": "Investing.com 한국어",
                "provider": "Google News RSS",
                "published_at": f"{today_kst} 13:10:20",
            },
        ]
        with patch.object(
            module,
            "search_rss_news",
            side_effect=[direct_articles, fallback_articles],
        ) as mocked_search:
            articles = module.search_investing_news_rss(limit=10)

        self.assertEqual(len(articles), 2)
        self.assertEqual(articles[0]["title"], "나스닥 최신 Investing 뉴스")
        self.assertEqual(articles[0]["title_ko"], "나스닥 최신 Investing 뉴스")
        self.assertEqual(articles[1]["provider"], "Google News RSS")
        self.assertEqual(mocked_search.call_count, 2)
        direct_call = mocked_search.call_args_list[0].kwargs
        fallback_call = mocked_search.call_args_list[1].kwargs
        self.assertIn("https://kr.investing.com/rss/news.rss", direct_call["feed_urls"])
        self.assertIn("https://news.google.com/rss/search?", fallback_call["feed_urls"][0])
        self.assertIn("site%3Akr.investing.com", fallback_call["feed_urls"][0])
        self.assertIn("site%3Akr.investing.com%2Fnews%2Fcryptocurrency-news", fallback_call["feed_urls"][1])


if __name__ == "__main__":
    unittest.main()
