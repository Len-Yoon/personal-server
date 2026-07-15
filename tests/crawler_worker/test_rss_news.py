import importlib
import sys
import types
import unittest
from datetime import date
from unittest.mock import patch

from tests._test_support import prepare_service_import


class RssNewsTests(unittest.TestCase):
    def reload_rss_news(self):
        prepare_service_import("crawler-worker")
        sys.modules.setdefault("feedparser", types.ModuleType("feedparser"))
        import app.crawlers.rss_news as rss_news

        return importlib.reload(rss_news)

    def tearDown(self):
        sys.modules.pop("feedparser", None)

    def test_html_summary_is_converted_to_plain_text(self):
        rss_news = self.reload_rss_news()

        cleaned = rss_news._html_to_text(
            '<a href="https://example.com">Korean chip stocks</a>&nbsp;&nbsp;<font color="#6f6f6f">네이트</font>'
        )

        self.assertEqual(cleaned, "Korean chip stocks 네이트")

    def test_parses_investing_local_datetime_for_latest_first_sorting(self):
        rss_news = self.reload_rss_news()

        self.assertEqual(
            rss_news._parse_published_at("2026-07-13 03:59:20"),
            "2026-07-13T03:59:20+00:00",
        )

    def test_builds_google_news_rss_url_for_korean_it_topics(self):
        rss_news = self.reload_rss_news()

        url = rss_news.build_google_news_rss_url(
            "IT 동향 OR 클라우드 OR 개발자 도구 OR 플랫폼 엔지니어링 OR 소프트웨어",
            freshness="1d",
        )

        self.assertIn("news.google.com/rss/search", url)
        self.assertIn("hl=ko", url)
        self.assertIn("gl=KR", url)
        self.assertIn("ceid=KR:ko", url)

    def test_korean_it_query_keeps_broad_it_news_terms(self):
        self.reload_rss_news()

        from app.crawlers.google_news_rss import GOOGLE_NEWS_QUERIES

        query = GOOGLE_NEWS_QUERIES["KR_IT"]

        self.assertIn("IT 동향", query)
        self.assertIn("클라우드", query)
        self.assertIn("소프트웨어", query)
        self.assertNotIn("-React", query)

    def test_google_news_uses_rss_freshness_instead_of_calendar_day_filter(self):
        self.reload_rss_news()

        from app.crawlers.google_news_rss import search_google_news_rss

        with patch(
            "app.crawlers.google_news_rss.search_rss_news",
            return_value=[{"url": "https://example.com/it"}],
        ) as mocked_search:
            result = search_google_news_rss("KR_IT", limit=1)

        self.assertEqual(result, [{"url": "https://example.com/it"}])
        self.assertFalse(mocked_search.call_args.kwargs["today_only"])

    def test_filters_korean_articles_only(self):
        rss_news = self.reload_rss_news()

        from app.crawlers.google_news_rss import filter_korean_articles

        filtered = filter_korean_articles(
            [
                {"title": "AI 업계, 한국어 기사", "summary": "", "source": "Google News"},
                {"title": "English headline", "summary": "No Korean text", "source": "Reuters"},
            ]
        )

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["title"], "AI 업계, 한국어 기사")

    def test_identifies_articles_published_today_in_korea(self):
        rss_news = self.reload_rss_news()

        self.assertTrue(
            rss_news._is_today("2026-07-15T00:30:00+00:00", today=date(2026, 7, 15))
        )
        self.assertFalse(
            rss_news._is_today("2026-07-14T14:59:59+00:00", today=date(2026, 7, 15))
        )


if __name__ == "__main__":
    unittest.main()
