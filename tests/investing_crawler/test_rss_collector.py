import io
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from investing_crawler.app.rss_collector import (
    INVESTING_SOURCE,
    build_google_fallback_rss_url,
    collect_investing_news,
    build_investing_rss_feed_urls,
)


class RssCollectorTests(unittest.TestCase):
    def _make_response(self):
        response = io.BytesIO(b"")
        response.__enter__ = lambda: response
        response.__exit__ = lambda *args: None
        return response

    def _make_feedparser(self, entries):
        return SimpleNamespace(
            parse=lambda _response: SimpleNamespace(entries=entries)
        )

    def _make_entry(self, title, link, published="", source="Investing.com"):
        entry = {
            "title": title,
            "link": link,
            "source": {"title": source},
        }
        if published:
            entry["published"] = published
        return SimpleNamespace(**entry)

    def test_default_feed_targets_official_korean_investing_rss(self):
        url = build_investing_rss_feed_urls()
        expected_urls = [
            "https://kr.investing.com/rss/news.rss",
            "https://kr.investing.com/rss/news_25.rss",
            "https://kr.investing.com/rss/news_1.rss",
            "https://kr.investing.com/rss/news_11.rss",
            "https://kr.investing.com/rss/news_95.rss",
            "https://kr.investing.com/rss/news_14.rss",
            "https://kr.investing.com/rss/news_477.rss",
            "https://kr.investing.com/rss/news_462.rss",
            "https://kr.investing.com/rss/news_450.rss",
            "https://kr.investing.com/rss/news_357.rss",
            "https://kr.investing.com/rss/news_1065.rss",
            "https://kr.investing.com/rss/news_1064.rss",
            "https://kr.investing.com/rss/news_1063.rss",
            "https://kr.investing.com/rss/news_1062.rss",
            "https://kr.investing.com/rss/news_1061.rss",
        ]
        self.assertEqual(url.split(","), expected_urls)

    def test_google_fallback_is_restricted_to_investing_domain(self):
        url = build_google_fallback_rss_url()
        self.assertIn("site%3Akr.investing.com%2Fnews", url)
        self.assertIn("hl=ko", url)

    def test_build_url_targets_korean_investing_news(self):
        url = build_investing_rss_feed_urls()
        self.assertIn("https://kr.investing.com/rss/news.rss", url)
        self.assertIn("https://kr.investing.com/rss/news_1061.rss", url)

    @patch("investing_crawler.app.rss_collector.urlopen")
    def test_collect_filters_source_and_malformed_titles(self, urlopen):
        now = datetime.now(timezone.utc)
        now_gmt = now.strftime("%a, %d %b %Y %H:%M:%S GMT")
        urlopen.return_value = self._make_response()
        feedparser = self._make_feedparser(
            [
                self._make_entry(
                    "나스닥 최신 Investing 뉴스",
                    "https://news.google.com/rss/articles/latest",
                    now_gmt,
                    "Investing.com 한국어",
                ),
                self._make_entry(
                    "By Investing.com - Investing.com 한국어",
                    "https://news.google.com/rss/articles/bad-title",
                    now_gmt,
                    "Investing.com 한국어",
                ),
                self._make_entry(
                    "다른 출처 뉴스",
                    "https://news.google.com/rss/articles/other",
                    source="다른 출처",
                ),
            ]
        )

        with patch.dict("sys.modules", {"feedparser": feedparser}):
            items = collect_investing_news(
                limit=10,
                feed_url="https://example.com/test.rss",
            )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "나스닥 최신 Investing 뉴스")
        self.assertEqual(items[0]["source"], INVESTING_SOURCE)
        self.assertEqual(items[0]["url"], "https://news.google.com/rss/articles/latest")
        self.assertEqual(items[0]["published_at"], now.replace(microsecond=0).isoformat())

    @patch("investing_crawler.app.rss_collector.urlopen")
    def test_collect_keeps_only_target_market_topics(self, urlopen):
        now = datetime.now(timezone.utc)
        now_gmt = now.strftime("%a, %d %b %Y %H:%M:%S GMT")
        urlopen.return_value = self._make_response()
        feedparser = self._make_feedparser(
            [
                self._make_entry("나스닥 선물 상승", "https://kr.investing.com/nasdaq", now_gmt),
                self._make_entry("일본 증시 닛케이 동향", "https://kr.investing.com/japan", now_gmt),
                self._make_entry("국제유가 WTI 급등", "https://kr.investing.com/oil", now_gmt),
                self._make_entry("금값 상승, 골드 가격 주목", "https://kr.investing.com/gold", now_gmt),
                self._make_entry("유럽 증시 마감", "https://kr.investing.com/europe", now_gmt),
            ]
        )

        with patch.dict("sys.modules", {"feedparser": feedparser}):
            items = collect_investing_news(
                limit=10,
                feed_url="https://example.com/test.rss",
            )

        self.assertEqual(
            [item["title"] for item in items],
            [
                "나스닥 선물 상승",
                "일본 증시 닛케이 동향",
                "국제유가 WTI 급등",
                "금값 상승, 골드 가격 주목",
                "유럽 증시 마감",
            ],
        )

    @patch("investing_crawler.app.rss_collector.urlopen")
    def test_collect_skips_failed_feed_and_continues(self, urlopen):
        now = datetime.now(timezone.utc)
        now_gmt = now.strftime("%a, %d %b %Y %H:%M:%S GMT")
        response = self._make_response()
        feedparser = self._make_feedparser(
            [
                self._make_entry(
                    "두 번째 피드 뉴스",
                    "https://news.google.com/rss/articles/second",
                    now_gmt,
                )
            ]
        )

        def fake_urlopen(request, timeout=20):
            if "news.rss" in request.full_url:
                raise OSError("boom")
            return response

        urlopen.side_effect = fake_urlopen

        with patch.dict("sys.modules", {"feedparser": feedparser}):
            items = collect_investing_news(
                limit=10,
                feed_url="https://kr.investing.com/rss/news.rss,https://kr.investing.com/rss/news_25.rss",
            )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "두 번째 피드 뉴스")


if __name__ == "__main__":
    unittest.main()
