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

    def test_it_news_excludes_ai_and_stack_articles(self):
        news_quality = self.load_quality()

        articles = news_quality.filter_high_quality_articles(
            [
                {
                    "url": "https://example.com/ai",
                    "title": "생성형 AI 플랫폼 업데이트 소식",
                    "summary": "새로운 인공지능 모델과 AI 플랫폼이 공개됐다.",
                    "source": "Google News",
                },
                {
                    "url": "https://example.com/stack",
                    "title": "React 프레임워크 새 버전 업데이트",
                    "summary": "React 개발 생태계의 새 릴리즈가 공개됐다.",
                    "source": "Google News",
                },
                {
                    "url": "https://example.com/cloud",
                    "title": "클라우드 플랫폼 엔지니어링 동향",
                    "summary": "기업의 클라우드 인프라 운영과 개발자 플랫폼 도입이 늘고 있다.",
                    "source": "Google News",
                },
            ],
            category="KR_IT",
            limit=5,
        )

        self.assertEqual([article["url"] for article in articles], ["https://example.com/cloud"])


if __name__ == "__main__":
    unittest.main()
