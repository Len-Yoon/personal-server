import importlib
import unittest

from tests._test_support import prepare_service_import


class VelogTrendingTests(unittest.TestCase):
    def load_module(self):
        prepare_service_import("crawler-worker")
        import app.crawlers.velog_trending as velog_trending

        return importlib.reload(velog_trending)

    def test_extracts_velog_post_links_and_titles(self):
        module = self.load_module()
        html = """
        <a href="/@dev/react-server">React 서버 컴포넌트 정리</a>
        <a href="https://velog.io/@dev/ai-agent">AI Agent 만들기</a>
        <a href="/about">소개</a>
        """

        parser = module._VelogLinkParser()
        parser.feed(html)
        articles = module._to_articles(parser.posts, limit=10)

        self.assertEqual(len(articles), 2)
        self.assertEqual(articles[0]["source"], "Velog")
        self.assertEqual(articles[0]["source_status"], "velog")
        self.assertEqual(articles[0]["title"], "React 서버 컴포넌트 정리")

    def test_deduplicates_trending_posts(self):
        module = self.load_module()

        articles = module._to_articles(
            [
                ("https://velog.io/@dev/post", "첫 번째 제목"),
                ("https://velog.io/@dev/post", "중복 제목"),
            ],
            limit=10,
        )

        self.assertEqual(len(articles), 1)


if __name__ == "__main__":
    unittest.main()
