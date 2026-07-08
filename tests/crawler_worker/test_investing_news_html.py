import importlib
import unittest

from tests._test_support import prepare_service_import


class InvestingNewsHtmlCrawlerTests(unittest.TestCase):
    def reload_crawler(self):
        prepare_service_import("crawler-worker")
        import app.crawlers.investing_news_html as investing_news_html

        return importlib.reload(investing_news_html)

    def test_parse_news_list_extracts_articles(self):
        crawler = self.reload_crawler()
        html = """
        <html>
          <body>
            <h1>상품과 선물 뉴스</h1>
            <p>원자재에 대한 최신 소식과 앞으로의 전망을 살펴보세요.</p>
            <ul>
              <li>
                <a href="/news/commodities-news/gold-steady-12345">이란 리스크와 연준 의사록에 주목하며 금값 안정세 유지</a>
                <p>Investing.com- 수요일 아시아 거래에서 금값은 새로운 군사적 충돌 소식을 소화하며 보합권에 머물렀다.</p>
                <span>By Investing.com•2시간 전</span>
              </li>
              <li>
                <a href="/news/commodities-news/oil-surges-67890">미국, 호르무즈 해협 공격 이란 타격 후 유가 급등</a>
                <p>Investing.com - 미국 군이 호르무즈 해협 선박 공격을 이유로 이란에 대한 추가 타격을 감행했다.</p>
                <span>By Investing.com•6시간 전</span>
              </li>
            </ul>
          </body>
        </html>
        """

        articles = crawler.parse_investing_news_html(
            html,
            category="GOLD",
            page_url="https://kr.investing.com/news/commodities-news",
        )

        self.assertEqual(len(articles), 2)
        self.assertEqual(articles[0]["category"], "GOLD")
        self.assertEqual(articles[0]["source"], "Investing.com")
        self.assertEqual(articles[0]["published_at"], "2시간 전")
        self.assertEqual(articles[0]["provider"], "Investing.com KR")
        self.assertEqual(
            articles[0]["url"],
            "https://kr.investing.com/news/commodities-news/gold-steady-12345",
        )

    def test_cloudflare_challenge_is_detected(self):
        crawler = self.reload_crawler()
        html = """
        <html>
          <head><title>Just a moment...</title></head>
          <body><script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script></body>
        </html>
        """

        self.assertTrue(crawler._is_cloudflare_challenge(html))


if __name__ == "__main__":
    unittest.main()
