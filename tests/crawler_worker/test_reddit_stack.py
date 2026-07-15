import importlib
import unittest
from unittest.mock import patch

from tests._test_support import prepare_service_import


class RedditStackTests(unittest.TestCase):
    def load_module(self):
        prepare_service_import("crawler-worker")
        import app.crawlers.reddit_stack as reddit_stack

        return importlib.reload(reddit_stack)

    def test_fetches_stack_subreddit_hot_feeds(self):
        module = self.load_module()
        raw_article = {
            "category": "KR_STACK",
            "url": "https://www.reddit.com/r/reactjs/comments/abc/post/",
            "title": "React Server Components discussion",
            "title_ko": "React Server Components discussion",
            "summary": "A discussion",
            "source": "Reddit",
            "provider": "Reddit Hot RSS",
        }

        with patch(
            "app.crawlers.reddit_stack.search_rss_news",
            return_value=[raw_article],
        ) as mocked_search:
            articles = module.search_reddit_stack_posts(limit=5)

        self.assertEqual(articles[0]["source_status"], "reddit")
        self.assertEqual(articles[0]["topics"], ["React"])
        self.assertEqual(mocked_search.call_args.kwargs["category"], "KR_STACK")
        self.assertIn("reactjs/hot/.rss", mocked_search.call_args.kwargs["feed_urls"][2])


if __name__ == "__main__":
    unittest.main()
