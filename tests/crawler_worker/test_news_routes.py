import importlib
import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._test_support import prepare_service_import


PORTAL_SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://img.youtube.com https://image.aladin.co.kr "
        "https://books.google.com https://covers.openlibrary.org; "
        "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'; frame-src 'self' https://www.youtube.com"
    ),
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}


class CrawlerWorkerNewsRouteTests(unittest.TestCase):
    def assert_portal_security_headers(self, response):
        for name, value in PORTAL_SECURITY_HEADERS.items():
            self.assertEqual(response.headers[name], value)

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

    def test_health_response_has_browser_security_headers(self):
        """Fails if the news service stops applying its common browser protections."""
        app = self.load_app()
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assert_portal_security_headers(response)

    def test_csp_keeps_explicit_shared_media_allowlist(self):
        """Fails if the shared CSP drops the explicit external media sources."""
        app = self.load_app()
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            response = client.get("/health")

        policy = response.headers["content-security-policy"]
        self.assertIn(
            "img-src 'self' data: https://img.youtube.com https://image.aladin.co.kr "
            "https://books.google.com https://covers.openlibrary.org",
            policy,
        )
        self.assertIn("frame-src 'self' https://www.youtube.com", policy)

    def test_cross_origin_unsafe_request_is_rejected_before_route_handling(self):
        """Fails if a future news write route can be reached from another origin."""
        app = self.load_app()
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            response = client.post("/health", headers={"Origin": "https://attacker.example"})

        self.assertEqual(response.status_code, 403)

    def test_untrusted_forwarded_headers_do_not_change_expected_origin(self):
        """Fails if a direct request can spoof forwarded headers to pass the Origin guard."""
        app = self.load_app()
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            response = client.post(
                "/health",
                headers={
                    "Origin": "https://attacker.example",
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "attacker.example",
                },
            )

        self.assertEqual(response.status_code, 403)

    def test_caddy_public_host_accepts_https_same_origin(self):
        """Fails if Caddy's internal HTTP hop rejects a browser's same-origin HTTPS form."""
        app = self.load_app()
        from fastapi.testclient import TestClient

        with TestClient(app, base_url="http://news.len.pe.kr") as client:
            response = client.post("/health", headers={"Origin": "https://news.len.pe.kr"})

        self.assertEqual(response.status_code, 405)

    def test_security_headers_are_applied_to_forbidden_not_found_and_static_responses(self):
        """Fails if middleware protections are skipped for non-success or static responses."""
        app = self.load_app()
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            responses = (
                client.post("/health", headers={"Origin": "https://attacker.example"}),
                client.get("/does-not-exist"),
                client.get("/static/css/style.css"),
            )

        self.assertEqual([response.status_code for response in responses], [403, 404, 200])
        for response in responses:
            self.assert_portal_security_headers(response)

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

    def test_investing_page_shows_alert_archive_status_and_classification_reasons(self):
        """Fails if Nasdaq classification is collected but no longer visible to the user."""
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
                        "title_ko": "원자재 시장 동향",
                        "provider": "Investing.com RSS",
                        "source": "Investing.com 한국어",
                        "nasdaq_relevance": {
                            "level": "archive",
                            "reasons": [],
                        },
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
        self.assertIn("분류 사유: 연준·금리", response.text)
        rendered_markup = response.text.split('<script id="news-auto-refresh">', 1)[0]
        self.assertEqual(rendered_markup.count("분류 사유:"), 1)

    def test_investing_page_shows_collection_and_alert_summary(self):
        app = self.load_app()

        with patch("app.routers.news.collect_korean_news") as mocked_collect:
            mocked_collect.return_value = {
                "category": "KR_WORLD",
                "label": "Investing.com 뉴스",
                "description": "설명",
                "count": 24,
                "articles": [],
                "relevance_summary": {"total": 24, "alert": 2, "archive": 21, "unclassified": 1},
                "cache": {"hit": True, "age_seconds": 12, "ttl_seconds": 300},
            }

            from fastapi.testclient import TestClient

            with TestClient(app) as client:
                response = client.get("/category?category=KR_WORLD")

        self.assertEqual(response.status_code, 200)
        self.assertIn('data-relevance-summary="true"', response.text)
        self.assertIn("수집 기사", response.text)
        self.assertIn("텔레그램 알림", response.text)
        self.assertIn("보관 전용", response.text)
        self.assertIn(">24<", response.text)
        self.assertIn(">2<", response.text)
        self.assertIn(">21<", response.text)

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

    def test_saved_page_shows_classification_only_for_investing_world_news(self):
        """Fails if classification leaks to unrelated categories or disappears from the archive."""
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
                    "nasdaq_relevance": {
                        "level": "alert",
                        "reasons": ["반도체 영향"],
                    },
                },
                {
                    "category": "KR_IT",
                    "title_ko": "개발자 도구 업데이트",
                    "title": "Developer tools update",
                    "topics": ["IT 동향"],
                    "source": "Google News",
                    "collected_at": "2026-08-10T00:00:00+00:00",
                    "summary": "기사 요약",
                    "published_at": "2026-08-09T23:00:00+00:00",
                    "url": "https://example.com/dev-tools",
                    "nasdaq_relevance": {
                        "level": "archive",
                        "reasons": ["표시되면 안 됨"],
                    },
                },
            ]

            from fastapi.testclient import TestClient

            with TestClient(app) as client:
                response = client.get("/saved")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text.count("data-news-alert-status="), 1)
        self.assertIn('data-news-alert-status="alert"', response.text)
        self.assertIn("분류 사유: 반도체 영향", response.text)
        self.assertNotIn("표시되면 안 됨", response.text)

    def test_news_pages_ignore_malformed_classification_reasons(self):
        """Fails if one malformed archived reason can break both Investing views."""
        app = self.load_app()
        malformed_article = {
            "category": "KR_WORLD",
            "title_ko": "손상된 분류 기사",
            "title": "Malformed classification",
            "topics": ["세계동향"],
            "provider": "Investing.com RSS",
            "source": "Investing.com 한국어",
            "collected_at": "2026-08-10T00:00:00+00:00",
            "summary": "기사 요약",
            "published_at": "2026-08-09T23:00:00+00:00",
            "url": "https://example.com/malformed",
            "nasdaq_relevance": {"level": "alert", "reasons": 1},
        }

        with patch("app.routers.news.collect_korean_news") as mocked_collect, patch(
            "app.routers.news.list_recent_news",
            return_value=[malformed_article],
        ):
            mocked_collect.return_value = {
                "category": "KR_WORLD",
                "label": "Investing.com 뉴스",
                "description": "설명",
                "count": 1,
                "articles": [malformed_article],
                "cache": {"hit": True, "age_seconds": 12, "ttl_seconds": 300},
            }

            from fastapi.testclient import TestClient

            with TestClient(app, raise_server_exceptions=False) as client:
                category_response = client.get("/category?category=KR_WORLD")
                saved_response = client.get("/saved")

        for response in (category_response, saved_response):
            self.assertEqual(response.status_code, 200)
            self.assertIn("텔레그램 알림 대상", response.text)
            rendered_markup = response.text.split('<script id="news-auto-refresh">', 1)[0]
            self.assertNotIn("분류 사유:", rendered_markup)

    def test_news_pages_replace_non_http_article_links_with_safe_fallback(self):
        """Fails if an archived feed URL can execute script from a rendered article link."""
        app = self.load_app()
        unsafe_article = {
            "category": "KR_WORLD",
            "title_ko": "안전하지 않은 링크 기사",
            "title": "Unsafe link article",
            "topics": ["세계동향"],
            "provider": "Investing.com RSS",
            "source": "Investing.com 한국어",
            "collected_at": "2026-08-10T00:00:00+00:00",
            "summary": "기사 요약",
            "published_at": "2026-08-09T23:00:00+00:00",
            "url": "javascript:alert(1)",
        }

        with patch("app.routers.news.collect_korean_news") as mocked_collect, patch(
            "app.routers.news.list_recent_news",
            return_value=[unsafe_article],
        ):
            mocked_collect.return_value = {
                "category": "KR_WORLD",
                "label": "Investing.com 뉴스",
                "description": "설명",
                "count": 1,
                "articles": [unsafe_article],
                "cache": {"hit": True, "age_seconds": 12, "ttl_seconds": 300},
            }

            from fastapi.testclient import TestClient

            with TestClient(app) as client:
                category_response = client.get("/category?category=KR_WORLD")
                saved_response = client.get("/saved")

        for response in (category_response, saved_response):
            self.assertEqual(response.status_code, 200)
            self.assertIn('class="article-link" href="#"', response.text)
            rendered_markup = response.text.split('<script id="news-auto-refresh">', 1)[0]
            self.assertNotIn('href="javascript:', rendered_markup)


if __name__ == "__main__":
    unittest.main()
