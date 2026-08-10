import importlib
import importlib.util
import unittest
from unittest.mock import patch

from tests._test_support import prepare_service_import


class CrawlerWorkerNewsRouteTests(unittest.TestCase):
    def load_app(self):
        if importlib.util.find_spec("fastapi") is None:
            self.skipTest("fastapi not available in this Python environment")
        prepare_service_import("crawler-worker")
        import app.main as main

        return importlib.reload(main).app

    def test_home_page_renders_korean_topic_cards(self):
        app = self.load_app()
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("뉴스 허브", response.text)
        self.assertIn("IT 동향", response.text)
        self.assertIn("AI 뉴스", response.text)

    def test_news_alias_redirects_to_main_news_page(self):
        app = self.load_app()
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            response = client.get("/news", follow_redirects=False)

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/")

    def test_category_route_uses_korean_collector(self):
        app = self.load_app()
        from fastapi.testclient import TestClient

        with patch("app.routers.news.collect_korean_news") as mocked_collect:
            mocked_collect.return_value = {
                "category": "KR_IT",
                "label": "IT 동향",
                "description": "설명",
                "count": 0,
                "articles": [],
                "cache": {"hit": False, "age_seconds": 0, "ttl_seconds": 300},
            }

            with TestClient(app) as client:
                response = client.get("/category?category=KR_IT")

        self.assertEqual(response.status_code, 200)
        mocked_collect.assert_called_once_with(category="KR_IT", limit=24, force_refresh=False)
        self.assertIn("IT 동향", response.text)

    def test_category_page_exposes_auto_refresh_contract(self):
        app = self.load_app()

        with patch("app.routers.news.collect_korean_news") as mocked_collect:
            mocked_collect.return_value = {
                "category": "KR_IT",
                "label": "IT 동향",
                "description": "설명",
                "count": 0,
                "articles": [],
                "cache": {"hit": True, "age_seconds": 12, "ttl_seconds": 300},
            }

            from fastapi.testclient import TestClient

            with TestClient(app) as client:
                response = client.get("/category?category=KR_IT")

        self.assertEqual(response.status_code, 200)
        self.assertIn('data-auto-refresh-seconds="60"', response.text)
        self.assertIn('data-category="KR_IT"', response.text)
        self.assertIn('data-news-list', response.text)
        self.assertIn("news-auto-refresh", response.text)
        self.assertIn('data-refresh-status', response.text)

    def test_korean_world_page_marks_alert_and_archive_articles(self):
        """Fails if the stored alert classification stops being visible on Investing news."""
        app = self.load_app()

        with patch("app.routers.news.collect_korean_news") as mocked_collect:
            mocked_collect.return_value = {
                "category": "KR_WORLD",
                "label": "Investing.com 뉴스",
                "description": "설명",
                "count": 2,
                "articles": [
                    {
                        "title_ko": "미 연준, 기준금리 동결 결정",
                        "provider": "Investing.com RSS",
                        "source": "Investing.com 한국어",
                        "nasdaq_relevance": {
                            "level": "alert",
                            "reasons": ["연준·금리"],
                        },
                    },
                    {
                        "title_ko": "엔비디아 목표주가 상향",
                        "provider": "Investing.com RSS",
                        "source": "Investing.com 한국어",
                        "nasdaq_relevance": {"level": "archive", "reasons": []},
                    },
                ],
                "cache": {"hit": True, "age_seconds": 12, "ttl_seconds": 300},
            }

            from fastapi.testclient import TestClient

            with TestClient(app) as client:
                response = client.get("/category?category=KR_WORLD")

        self.assertEqual(response.status_code, 200)
        self.assertIn('data-news-alert-status="alert"', response.text)
        self.assertIn('data-news-alert-status="archive"', response.text)
        self.assertIn("텔레그램 알림 대상", response.text)
        self.assertIn("보관 전용", response.text)
        self.assertIn("연준·금리", response.text)

    def test_saved_page_only_marks_investing_world_articles_with_alert_status(self):
        """Fails if alert presentation leaks from Investing world news to unrelated archive rows."""
        app = self.load_app()

        with patch("app.routers.news.list_recent_news") as mocked_recent_news:
            mocked_recent_news.return_value = [
                {
                    "category": "KR_WORLD",
                    "title_ko": "반도체 수출 제한 확대",
                    "source": "Investing.com 한국어",
                    "nasdaq_relevance": {
                        "level": "alert",
                        "reasons": ["반도체 영향"],
                    },
                },
                {
                    "category": "KR_IT",
                    "title_ko": "개발자 도구 업데이트",
                    "source": "Google News",
                    "nasdaq_relevance": {"level": "archive", "reasons": []},
                },
            ]

            from fastapi.testclient import TestClient

            with TestClient(app) as client:
                response = client.get("/saved")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text.count("data-news-alert-status="), 1)
        self.assertIn('data-news-alert-status="alert"', response.text)
        self.assertIn("반도체 영향", response.text)


if __name__ == "__main__":
    unittest.main()
