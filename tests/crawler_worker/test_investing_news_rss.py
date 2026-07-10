import importlib
import sys
import types
import unittest
from unittest.mock import patch

from tests._test_support import prepare_service_import


class InvestingNewsRssTests(unittest.TestCase):
    def reload_module(self):
        prepare_service_import("crawler-worker")
        sys.modules.setdefault("feedparser", types.ModuleType("feedparser"))
        import app.crawlers.investing_news_rss as module

        return importlib.reload(module)

    def tearDown(self):
        sys.modules.pop("feedparser", None)

    def test_search_investing_news_rss_keeps_only_investing_korean_source(self):
        module = self.reload_module()
        with patch.object(
            module,
            "search_rss_news",
            return_value=[
                {
                    "title": "최신 Investing 뉴스 - Investing.com 한국어",
                    "url": "https://news.google.com/rss/articles/1",
                    "source": "Investing.com 한국어",
                },
                {
                    "title": "다른 출처 뉴스",
                    "url": "https://example.com/2",
                    "source": "Reuters",
                },
                {
                    "title": "By Investing.com",
                    "url": "https://news.google.com/rss/articles/3",
                    "source": "Investing.com 한국어",
                },
            ]
        ) as mocked_search:
            articles = module.search_investing_news_rss(limit=10)

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["title"], "최신 Investing 뉴스")
        self.assertEqual(articles[0]["title_ko"], "최신 Investing 뉴스")
        mocked_search.assert_called_once()
        self.assertIn("site%3Akr.investing.com%2Fnews", mocked_search.call_args.kwargs["feed_urls"][0])


if __name__ == "__main__":
    unittest.main()
