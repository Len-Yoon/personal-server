import importlib
import unittest

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


if __name__ == "__main__":
    unittest.main()
