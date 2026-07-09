import importlib
import unittest


class RssNewsTests(unittest.TestCase):
    def reload_rss_news(self):
        import app.crawlers.rss_news as rss_news

        return importlib.reload(rss_news)

    def test_html_summary_is_converted_to_plain_text(self):
        rss_news = self.reload_rss_news()

        cleaned = rss_news._html_to_text(
            '<a href="https://example.com">Korean chip stocks</a>&nbsp;&nbsp;<font color="#6f6f6f">네이트</font>'
        )

        self.assertEqual(cleaned, "Korean chip stocks 네이트")


if __name__ == "__main__":
    unittest.main()
