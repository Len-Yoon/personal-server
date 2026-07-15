import importlib
import unittest

from tests._test_support import prepare_service_import


class NewsQualityTests(unittest.TestCase):
    def load_quality(self):
        prepare_service_import("crawler-worker")
        import app.crawlers.news_quality as news_quality

        return importlib.reload(news_quality)

    def test_ai_article_is_not_treated_as_junk_text(self):
        news_quality = self.load_quality()

        articles = news_quality.filter_high_quality_articles(
            [
                {
                    "url": "https://example.com/ai",
                    "title": "생성형 AI 에이전트 시장이 빠르게 성장하고 있다",
                    "summary": "기업들이 인공지능과 AI 에이전트를 업무에 도입하며 관련 시장이 확대되고 있다.",
                    "source": "Google News",
                }
            ],
            category="KR_AI",
            limit=1,
        )

        self.assertEqual(len(articles), 1)


if __name__ == "__main__":
    unittest.main()
