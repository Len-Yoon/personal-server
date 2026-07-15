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

    def test_home_page_includes_korean_news_entry(self):
        app = self.load_app()
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("한국어 뉴스", response.text)
        self.assertIn("/news", response.text)

    def test_korean_news_home_renders_topic_cards(self):
        app = self.load_app()
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            response = client.get("/news")

        self.assertEqual(response.status_code, 200)
        self.assertIn("IT 동향", response.text)
        self.assertIn("AI 뉴스", response.text)
        self.assertIn("인기 스택", response.text)

    def test_korean_category_route_uses_korean_collector(self):
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
                response = client.get("/news/category?category=KR_IT")

        self.assertEqual(response.status_code, 200)
        mocked_collect.assert_called_once_with(category="KR_IT", limit=24, force_refresh=False)
        self.assertIn("IT 동향", response.text)


if __name__ == "__main__":
    unittest.main()
