import importlib
import unittest
from unittest.mock import patch

from tests._test_support import prepare_service_import


class InvestingNewsAggregatorTests(unittest.TestCase):
    def reload_aggregator(self):
        prepare_service_import("crawler-worker")
        import app.crawlers.investing_news as investing_news

        return importlib.reload(investing_news)

    def test_browser_results_take_priority(self):
        aggregator = self.reload_aggregator()

        with patch(
            "app.crawlers.investing_news.search_investing_news_browser",
            return_value=[
                {
                    "url": "https://example.com/browser",
                    "title": "금값, 연준 발언 앞두고 강보합",
                    "summary": "달러와 미국 국채금리 변동 속에 금 가격이 제한적인 범위에서 움직였다.",
                    "source": "Investing.com",
                }
            ],
        ) as mocked_browser, patch(
            "app.crawlers.investing_news.search_investing_news_html",
            return_value=[{"url": "https://example.com/html", "source": "Investing.com"}],
        ) as mocked_html:
            result = aggregator.search_investing_news("GOLD", limit=1)

        self.assertEqual(result[0]["url"], "https://example.com/browser")
        mocked_browser.assert_called_once()
        mocked_html.assert_not_called()

    def test_html_fallback_used_when_browser_empty(self):
        aggregator = self.reload_aggregator()

        with patch(
            "app.crawlers.investing_news.search_investing_news_browser",
            return_value=[],
        ) as mocked_browser, patch(
            "app.crawlers.investing_news.search_investing_news_html",
            return_value=[
                {
                    "url": "https://example.com/html",
                    "title": "금값, 연준 발언 앞두고 강보합",
                    "summary": "달러와 미국 국채금리 변동 속에 금 가격이 제한적인 범위에서 움직였다.",
                    "source": "Investing.com",
                }
            ],
        ) as mocked_html:
            result = aggregator.search_investing_news("GOLD", limit=1)

        self.assertEqual(result[0]["url"], "https://example.com/html")
        mocked_browser.assert_called_once()
        mocked_html.assert_called_once()

    def test_low_quality_browser_results_fall_back_to_html(self):
        aggregator = self.reload_aggregator()

        with patch(
            "app.crawlers.investing_news.search_investing_news_browser",
            return_value=[
                {
                    "url": "https://example.com/browser",
                    "title": "짧은 제목",
                    "summary": "",
                    "source": "",
                }
            ],
        ) as mocked_browser, patch(
            "app.crawlers.investing_news.search_investing_news_html",
            return_value=[
                {
                    "url": "https://example.com/html",
                    "title": "금값, 연준 발언 앞두고 강보합",
                    "summary": "달러와 미국 국채금리 변동 속에 금 가격이 제한적인 범위에서 움직였다.",
                    "source": "Investing.com",
                }
            ],
        ) as mocked_html:
            result = aggregator.search_investing_news("GOLD", limit=1)

        self.assertEqual(result[0]["url"], "https://example.com/html")
        mocked_browser.assert_called_once()
        mocked_html.assert_called_once()

    def test_rendered_parser_handles_split_byline_lines(self):
        prepare_service_import("crawler-worker")
        import app.crawlers.investing_news_browser as investing_news_browser

        crawler = importlib.reload(investing_news_browser)
        body_text = """
        광고
        상품과 선물 뉴스
        호르무즈 해협 충돌에 유럽 가스 가격 급등, LNG 공급 우려 증폭
        Investing.com — 수요일(현지시간) 유럽 도매 천연가스 가격이 급등했습니다. 미국과 이란 간의 새로운 군사...
        By
        Investing.com
        •
        29분 전
        """
        cards = [
            {
                "title": "호르무즈 해협 충돌에 유럽 가스 가격 급등, LNG 공급 우려 증폭",
                "href": "https://kr.investing.com/news/commodities-news/article-2008593",
            }
        ]

        articles = crawler._parse_rendered_articles(
            body_text=body_text,
            cards=cards,
            category="GOLD",
            page_url="https://kr.investing.com/news/commodities-news",
        )

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["source"], "Investing.com")
        self.assertEqual(articles[0]["published_at"], "29분 전")
        self.assertEqual(articles[0]["summary"], "Investing.com — 수요일(현지시간) 유럽 도매 천연가스 가격이 급등했습니다. 미국과 이란 간의 새로운 군사...")

    def test_quality_filter_removes_duplicate_and_junk_articles(self):
        prepare_service_import("crawler-worker")
        import app.crawlers.news_quality as news_quality

        quality = importlib.reload(news_quality)
        articles = [
            {
                "url": "https://example.com/a",
                "title": "금값, 연준 발언 앞두고 강보합",
                "summary": "달러와 미국 국채금리 변동 속에 금 가격이 제한적인 범위에서 움직였다.",
                "source": "Investing.com",
            },
            {
                "url": "https://example.com/a?dup=1",
                "title": "금값, 연준 발언 앞두고 강보합",
                "summary": "중복 기사",
                "source": "Investing.com",
            },
            {
                "url": "https://example.com/b",
                "title": "광고",
                "summary": "더보기",
                "source": "Investing.com",
            },
        ]

        filtered = quality.filter_high_quality_articles(articles, category="GOLD", limit=10)

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["url"], "https://example.com/a")


if __name__ == "__main__":
    unittest.main()
