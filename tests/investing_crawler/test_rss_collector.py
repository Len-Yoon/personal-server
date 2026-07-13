import io
import unittest
from unittest.mock import patch

from investing_crawler.app.rss_collector import (
    INVESTING_SOURCE,
    build_google_fallback_rss_url,
    collect_investing_news,
    build_investing_google_news_rss_url,
)


RSS = """<?xml version='1.0'?>
<rss version='2.0'><channel>
  <item>
    <title>나스닥 최신 Investing 뉴스</title>
    <link>https://news.google.com/rss/articles/latest</link>
    <pubDate>Fri, 10 Jul 2026 15:02:00 GMT</pubDate>
    <source url='https://kr.investing.com'>Investing.com 한국어</source>
  </item>
  <item>
    <title>By Investing.com - Investing.com 한국어</title>
    <link>https://news.google.com/rss/articles/bad-title</link>
    <pubDate>Fri, 10 Jul 2026 14:02:00 GMT</pubDate>
    <source url='https://kr.investing.com'>Investing.com 한국어</source>
  </item>
  <item>
    <title>다른 출처 뉴스</title>
    <link>https://news.google.com/rss/articles/other</link>
    <source url='https://example.com'>다른 출처</source>
  </item>
</channel></rss>""".encode("utf-8")


class RssCollectorTests(unittest.TestCase):
    def test_default_feed_targets_official_korean_investing_rss(self):
        url = build_investing_google_news_rss_url()
        self.assertIn("https://kr.investing.com/rss/news.rss", url)
        self.assertIn("https://kr.investing.com/rss/news_25.rss", url)
        self.assertIn("https://kr.investing.com/rss/news_1.rss", url)
        self.assertIn("https://kr.investing.com/rss/news_11.rss", url)

    def test_google_fallback_is_restricted_to_investing_domain(self):
        url = build_google_fallback_rss_url()
        self.assertIn("site%3Akr.investing.com%2Fnews", url)
        self.assertIn("hl=ko", url)

    def test_build_url_targets_korean_investing_news(self):
        url = build_investing_google_news_rss_url()
        self.assertIn("https://kr.investing.com/rss/news.rss", url)

    @patch("investing_crawler.app.rss_collector.urlopen")
    def test_collect_filters_source_and_malformed_titles(self, urlopen):
        response = io.BytesIO(RSS)
        response.__enter__ = lambda: response
        response.__exit__ = lambda *args: None
        urlopen.return_value = response

        items = collect_investing_news(
            limit=10,
            feed_url="https://example.com/test.rss",
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "나스닥 최신 Investing 뉴스")
        self.assertEqual(items[0]["source"], INVESTING_SOURCE)
        self.assertEqual(items[0]["url"], "https://news.google.com/rss/articles/latest")
        self.assertEqual(items[0]["published_at"], "2026-07-10T15:02:00+00:00")

    @patch("investing_crawler.app.rss_collector.urlopen")
    def test_collect_keeps_only_target_market_topics(self, urlopen):
        rss = """<?xml version='1.0'?>
        <rss version='2.0'><channel>
          <item><title>나스닥 선물 상승</title><link>https://kr.investing.com/nasdaq</link><source url='https://kr.investing.com'>Investing.com</source></item>
          <item><title>일본 증시 닛케이 동향</title><link>https://kr.investing.com/japan</link><source url='https://kr.investing.com'>Investing.com</source></item>
          <item><title>국제유가 WTI 급등</title><link>https://kr.investing.com/oil</link><source url='https://kr.investing.com'>Investing.com</source></item>
          <item><title>금값 상승, 골드 가격 주목</title><link>https://kr.investing.com/gold</link><source url='https://kr.investing.com'>Investing.com</source></item>
          <item><title>유럽 증시 마감</title><link>https://kr.investing.com/europe</link><source url='https://kr.investing.com'>Investing.com</source></item>
        </channel></rss>""".encode("utf-8")
        response = io.BytesIO(rss)
        response.__enter__ = lambda: response
        response.__exit__ = lambda *args: None
        urlopen.return_value = response

        items = collect_investing_news(
            limit=10,
            feed_url="https://example.com/test.rss",
        )

        self.assertEqual(
            [item["title"] for item in items],
            ["나스닥 선물 상승", "일본 증시 닛케이 동향", "국제유가 WTI 급등", "금값 상승, 골드 가격 주목"],
        )


if __name__ == "__main__":
    unittest.main()
