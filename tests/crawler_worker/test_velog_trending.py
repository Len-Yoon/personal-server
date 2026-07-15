import importlib
import unittest
from unittest.mock import patch

from tests._test_support import prepare_service_import


class VelogTrendingTests(unittest.TestCase):
    def load_module(self):
        prepare_service_import("crawler-worker")
        import app.crawlers.velog_trending as velog_trending

        return importlib.reload(velog_trending)

    def test_converts_api_post_to_stack_article(self):
        module = self.load_module()

        article = module._to_article(
            {
                "title": "React 서버 컴포넌트 정리",
                "urlSlug": "react-server-components",
                "shortDescription": "React와 서버 컴포넌트 핵심 정리",
                "releasedAt": "2026-07-15T01:00:00.000Z",
                "likes": 42,
                "comments": 7,
                "user": {"username": "dev-user"},
            }
        )

        self.assertEqual(article["source"], "Velog")
        self.assertEqual(article["source_status"], "velog")
        self.assertEqual(article["url"], "https://velog.io/@dev-user/react-server-components")
        self.assertIn("좋아요 42", article["summary"])

    def test_keeps_all_posts_from_trending_api(self):
        module = self.load_module()

        posts = [
            {"title": "FastAPI 배포 방법", "urlSlug": "fastapi", "user": {"username": "dev"}},
            {"title": "개발자의 이직 회고", "urlSlug": "career", "user": {"username": "dev"}},
        ]

        with patch.object(module, "_fetch_trending_posts", return_value=posts):
            articles = module.search_velog_trending(limit=5)

        self.assertEqual([article["title"] for article in articles], [
            "FastAPI 배포 방법",
            "개발자의 이직 회고",
        ])

    def test_fetches_weekly_trending_api_and_returns_stack_posts(self):
        module = self.load_module()
        posts = [
            {"title": "Next.js 캐싱 전략", "urlSlug": "next-cache", "user": {"username": "dev"}},
            {"title": "일상 회고", "urlSlug": "daily", "user": {"username": "dev"}},
        ]

        with patch.object(module, "_fetch_trending_posts", return_value=posts) as mocked_fetch:
            articles = module.search_velog_trending(limit=5)

        self.assertEqual(len(articles), 2)
        self.assertEqual(
            [article["title"] for article in articles],
            ["Next.js 캐싱 전략", "일상 회고"],
        )
        mocked_fetch.assert_called_once_with(limit=30)


if __name__ == "__main__":
    unittest.main()
