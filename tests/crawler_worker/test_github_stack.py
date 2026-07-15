import importlib
import unittest
from datetime import date

from tests._test_support import prepare_service_import


class GithubStackTests(unittest.TestCase):
    def load_module(self):
        prepare_service_import("crawler-worker")
        import app.crawlers.github_stack as github_stack

        return importlib.reload(github_stack)

    def test_converts_repository_to_stack_article(self):
        module = self.load_module()

        article = module._to_article(
            {
                "full_name": "vercel/next.js",
                "html_url": "https://github.com/vercel/next.js",
                "description": "The React Framework for the Web",
                "stargazers_count": 130000,
                "language": "TypeScript",
                "pushed_at": "2026-07-15T00:00:00Z",
            }
        )

        self.assertEqual(article["category"], "KR_STACK")
        self.assertEqual(article["source"], "GitHub")
        self.assertIn("stars 130,000", article["summary"])

    def test_builds_recent_activity_query_for_stack_repositories(self):
        module = self.load_module()

        query = module._build_query(date(2026, 7, 8))

        self.assertIn("pushed:>=2026-07-08", query)
        self.assertIn("in:name,description,readme", query)

    def test_uses_topics_when_repository_description_is_generic(self):
        module = self.load_module()

        self.assertTrue(
            module._is_stack_repository(
                {
                    "full_name": "example/project",
                    "description": "A useful developer project",
                    "language": "Python",
                    "topics": ["fastapi"],
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
