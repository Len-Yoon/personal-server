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

    def test_classifies_investing_topics_from_title_without_mixing_interest_rate_with_gold(self):
        module = self.reload_module()

        self.assertEqual(
            module._classify_topics("금 가격, 미국 금리 인하 기대에 상승", ""),
            ["금"],
        )
        self.assertEqual(
            module._classify_topics("국제유가 WTI와 일본 엔화 동향", ""),
            ["원유", "일본"],
        )
        self.assertEqual(
            module._classify_topics("미국 증시와 유럽 경제 동향", ""),
            ["세계동향"],
        )

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
        with patch.object(
            module,
            "search_rss_news",
            return_value=direct_articles,
        ) as mocked_search:
            articles = module.search_investing_news_rss(limit=10)

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["title"], "나스닥 최신 Investing 뉴스")
        self.assertEqual(articles[0]["title_ko"], "나스닥 최신 Investing 뉴스")
        self.assertEqual(articles[0]["topics"], ["세계동향"])
        self.assertEqual(mocked_search.call_count, 1)
        direct_call = mocked_search.call_args_list[0].kwargs
        self.assertEqual(
            direct_call["feed_urls"],
            [
                "https://kr.investing.com/rss/news.rss",
                "https://kr.investing.com/rss/news_25.rss",
                "https://kr.investing.com/rss/news_1.rss",
                "https://kr.investing.com/rss/news_11.rss",
                "https://kr.investing.com/rss/news_14.rss",
            ],
        )


if __name__ == "__main__":
    unittest.main()
