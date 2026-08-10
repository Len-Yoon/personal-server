import importlib
import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._test_support import prepare_service_import


class CrawlerWorkerNewsRouteTests(unittest.TestCase):
    def load_app(self):
        if importlib.util.find_spec("fastapi") is None:
            self.skipTest("fastapi not available in this Python environment")
        previous_cwd = Path.cwd()
        service_dir = Path(__file__).resolve().parents[2] / "crawler-worker"
        if previous_cwd != service_dir:
            self.addCleanup(os.chdir, previous_cwd)
            os.chdir(service_dir)
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

    def test_category_page_keeps_original_layout_and_article_link(self):
        """Fails if a visual refactor replaces the original result page or article URL."""
        app = self.load_app()

        with patch("app.routers.news.collect_korean_news") as mocked_collect:
            mocked_collect.return_value = {
                "category": "KR_WORLD",
                "label": "Investing.com 뉴스",
                "description": "설명",
                "count": 1,
                "articles": [
                    {
                        "title_ko": "미 연준, 기준금리 동결 결정",
                        "title_original": "Fed holds rates steady",
                        "provider": "Investing.com RSS",
                        "topics": ["세계동향"],
                        "source": "Investing.com 한국어",
                        "collected_at": "2026-08-10T00:00:00+00:00",
                        "summary": "기사 요약",
                        "published_at": "2026-08-09T23:00:00+00:00",
                        "url": "https://example.com/fed-rates",
                    },
                ],
                "cache": {"hit": True, "age_seconds": 12, "ttl_seconds": 300},
            }

            from fastapi.testclient import TestClient

            with TestClient(app) as client:
                response = client.get("/category?category=KR_WORLD")

        self.assertEqual(response.status_code, 200)
        self.assertIn('<section class="hero compact">', response.text)
        self.assertIn("<h1>Investing.com 뉴스</h1>", response.text)
        self.assertIn('class="article-link" href="https://example.com/fed-rates"', response.text)

    def test_saved_page_keeps_original_layout_search_form_and_article_link(self):
        """Fails if the original archive layout, search action, or article URL is lost."""
        app = self.load_app()

        with patch("app.routers.news.list_recent_news") as mocked_recent_news:
            mocked_recent_news.return_value = [
                {
                    "category": "KR_WORLD",
                    "title_ko": "반도체 수출 제한 확대",
                    "title": "Chip export restrictions expand",
                    "topics": ["세계동향"],
                    "source": "Investing.com 한국어",
                    "collected_at": "2026-08-10T00:00:00+00:00",
                    "summary": "기사 요약",
                    "published_at": "2026-08-09T23:00:00+00:00",
                    "url": "https://example.com/chip-exports",
                },
            ]

            from fastapi.testclient import TestClient

            with TestClient(app) as client:
                response = client.get("/saved")

        self.assertEqual(response.status_code, 200)
        self.assertIn('<section class="hero compact">', response.text)
        self.assertIn("<h1>보관 뉴스</h1>", response.text)
        self.assertIn('class="search-form saved-search-form" action="/saved" method="get"', response.text)
        self.assertIn('class="article-link" href="https://example.com/chip-exports"', response.text)


if __name__ == "__main__":
    unittest.main()
