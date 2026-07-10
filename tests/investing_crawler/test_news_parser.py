import unittest

from investing_crawler.app.news_parser import parse_news_html


class NewsParserTests(unittest.TestCase):
    def test_parse_news_html_extracts_metadata(self):
        html = """
        <article>
          <a href="/news/stock-market-news/article-1">첫 번째 뉴스</a>
          <span class="byline">By Investing.com</span>
          <time datetime="2026-07-10T05:10:00Z">5분 전</time>
        </article>
        """

        self.assertEqual(
            parse_news_html(html),
            [{
                "title": "첫 번째 뉴스",
                "url": "https://kr.investing.com/news/stock-market-news/article-1",
                "published_label": "5분 전",
                "published_at": "2026-07-10T05:10:00Z",
                "source": "Investing.com",
            }],
        )

    def test_parse_news_html_deduplicates_urls_and_honors_limit(self):
        html = (
            '<a href="/news/most-popular-news">많이 본 뉴스</a>'
            '<a href="/news/stock-market-news/article-10">A</a>'
            '<a href="/news/stock-market-news/article-10">A again</a>'
            '<a href="/news/stock-market-news/article-11">B</a>'
        )

        self.assertEqual(
            [item["title"] for item in parse_news_html(html, limit=1)],
            ["A"],
        )

    def test_parse_news_html_extracts_time_from_an_ancestor_card(self):
        html = """
        <div class="card">
          <div><a href="/news/stock-market-news/article-2">전망이 바뀐 뉴스</a></div>
          <div>By Reuters • 23분 전</div>
        </div>
        """

        item = parse_news_html(html)[0]
        self.assertEqual(item["published_label"], "23분 전")
        self.assertEqual(item["source"], "Reuters")


if __name__ == "__main__":
    unittest.main()
