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
            return_value=[{"url": "https://example.com/browser", "source": "Investing.com"}],
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
            return_value=[{"url": "https://example.com/html", "source": "Investing.com"}],
        ) as mocked_html:
            result = aggregator.search_investing_news("GOLD", limit=1)

        self.assertEqual(result[0]["url"], "https://example.com/html")
        mocked_browser.assert_called_once()
        mocked_html.assert_called_once()


if __name__ == "__main__":
    unittest.main()
