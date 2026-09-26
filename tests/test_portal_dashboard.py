import asyncio
import importlib
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from tests._test_support import prepare_service_import


def _search_response(payload):
    return httpx.Response(200, content=payload, request=httpx.Request("GET", "http://search.test"))


class PortalDashboardTests(unittest.TestCase):
    def test_admin_router_owns_administrator_and_homeops_paths(self):
        prepare_service_import("portal-web")
        from app.routers import admin

        paths = {route.path for route in admin.router.routes}

        self.assertTrue(
            {
                "/admin/security",
                "/admin/status",
                "/admin/homeops/diagnose",
                "/admin/homeops/restart-all",
                "/admin/homeops/{incident_id}/approve",
                "/admin/homeops/{incident_id}/execute",
                "/internal/homeops/scan",
                "/admin/events",
            }.issubset(paths)
        )

    def reload_system_status(self, demo_mode: str = ""):
        prepare_service_import("portal-web")
        os.environ["DEMO_MODE"] = demo_mode
        import app.services.system_status as system_status

        return importlib.reload(system_status)

    def load_app(self):
        prepare_service_import("portal-web")
        import app.main as main

        return importlib.reload(main).app

    def test_demo_mode_returns_sample_status(self):
        system_status = self.reload_system_status("true")

        status = system_status.get_dashboard_status()

        self.assertTrue(status["demo_mode"])
        self.assertEqual(status["overall_status"], "ok")
        self.assertEqual(status["host"]["source"], "demo")

    def test_agent_failure_returns_unavailable_status(self):
        system_status = self.reload_system_status("")

        with patch("app.services.system_status.urlopen", side_effect=OSError("down")):
            status = system_status.get_dashboard_status(agent_url="http://system-agent:8010", timeout=0.01)

        self.assertFalse(status["demo_mode"])
        self.assertEqual(status["overall_status"], "unavailable")
        self.assertIn("system_agent_unavailable", status["warnings"])

    def test_search_result_relative_urls_are_prefixed(self):
        prepare_service_import("portal-web")
        from app.services.global_search import _normalize_result_url

        result = _normalize_result_url(
            "youtube",
            {"title": "memo", "url": "/videos/1"},
            public_base_urls={
                "news": "https://news.len.pe.kr",
                "youtube": "https://memo.len.pe.kr",
                "books": "https://books.len.pe.kr",
            },
            local_base_urls={
                "news": "http://127.0.0.1:8001",
                "youtube": "http://127.0.0.1:8002",
                "books": "http://127.0.0.1:8003",
            },
            prefer_local=True,
        )

        self.assertEqual(result["url"], "http://127.0.0.1:8002/videos/1")

    def test_search_result_relative_urls_use_public_domain_outside_local(self):
        prepare_service_import("portal-web")
        from app.services.global_search import _normalize_result_url

        result = _normalize_result_url(
            "books",
            {"title": "memo", "url": "/books/1"},
            public_base_urls={
                "news": "https://news.len.pe.kr",
                "youtube": "https://memo.len.pe.kr",
                "books": "https://books.len.pe.kr",
            },
            local_base_urls={
                "news": "http://127.0.0.1:8001",
                "youtube": "http://127.0.0.1:8002",
                "books": "http://127.0.0.1:8003",
            },
            prefer_local=False,
        )

        self.assertEqual(result["url"], "https://books.len.pe.kr/books/1")

    def test_search_result_rejects_unsafe_external_urls(self):
        prepare_service_import("portal-web")
        from app.services.global_search import _normalize_result_url

        for url in ("javascript:alert(1)", "data:text/html,unsafe", "//attacker.example"):
            with self.subTest(url=url):
                result = _normalize_result_url("news", {"title": "unsafe", "url": url})

                self.assertEqual(result["url"], "#")

    def test_search_result_keeps_absolute_https_url(self):
        prepare_service_import("portal-web")
        from app.services.global_search import _normalize_result_url

        result = _normalize_result_url(
            "news", {"title": "article", "url": "https://news.example/articles/1?source=search"}
        )

        self.assertEqual(result["url"], "https://news.example/articles/1?source=search")

    def test_demo_mode_returns_service_health_samples(self):
        system_status = self.reload_system_status("true")

        services = system_status.get_service_health()

        self.assertTrue(services)
        self.assertTrue(all(service["status"] == "ok" for service in services))
        self.assertTrue(all(service["demo_mode"] for service in services))

    def test_service_health_failure_is_unavailable(self):
        system_status = self.reload_system_status("")

        with patch("app.services.system_status.urlopen", side_effect=OSError("down")):
            services = system_status.get_service_health(timeout=0.01)

        self.assertIn(
            {"name": "뉴스 허브", "status": "unavailable", "url": "http://crawler-worker:8001/health"},
            services,
        )

    def test_recovery_events_incomplete_bridge_body_returns_empty_list(self):
        """Fails if a partial system-agent response breaks the administrator status page."""
        from http.client import IncompleteRead

        system_status = self.reload_system_status("")

        class IncompleteResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                raise IncompleteRead(b"partial", 20)

        with patch("app.services.system_status.urlopen", return_value=IncompleteResponse()):
            events = system_status.get_recovery_events(timeout=0.01)

        self.assertEqual(events, [])

    def test_demo_search_results_include_metadata(self):
        prepare_service_import("portal-web")
        os.environ["DEMO_MODE"] = "true"
        from app.services import global_search

        results = asyncio.run(global_search.search_all("테스트"))

        self.assertEqual(results["youtube"]["status"], "ok")
        self.assertIn("meta", results["youtube"]["items"][0])
        self.assertIn("snippet", results["youtube"]["items"][0])

    def run_search_transport(self, handler, query="test", budget=1.5):
        prepare_service_import("portal-web")
        from app.services import global_search

        client_type = httpx.AsyncClient

        def make_client(**kwargs):
            return client_type(transport=httpx.MockTransport(handler), **kwargs)

        with patch.dict(os.environ, {"DEMO_MODE": ""}), patch(
            "httpx.AsyncClient", side_effect=make_client
        ), patch.object(global_search, "SEARCH_BUDGET_SECONDS", budget):
            return asyncio.run(global_search.search_all(query))

    def test_search_starts_all_services_before_waiting_for_results(self):
        started = set()
        all_started = asyncio.Event()

        async def handle(request):
            started.add(request.url.host)
            if len(started) == 3:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=0.5)
            self.assertEqual(request.url.params["q"], "한글 & query")
            self.assertEqual(request.url.params["limit"], "5")
            return httpx.Response(200, json={"results": [{"title": request.url.host, "url": "#"}]})

        results = self.run_search_transport(handle, query="  한글 & query  ")
        self.assertEqual(len(started), 3)
        self.assertEqual(list(results), ["news", "youtube", "books"])
        self.assertTrue(all(result["status"] == "ok" for result in results.values()))

    def test_search_deadline_preserves_fast_results_and_cleans_up_pending_requests(self):
        cancelled = set()
        active = set()

        async def handle(request):
            host = request.url.host
            active.add(host)
            try:
                if host != "book-memo":
                    try:
                        await asyncio.sleep(10)
                    except asyncio.CancelledError:
                        cancelled.add(host)
                        raise
                return httpx.Response(200, json={"results": [{"title": "fast book", "url": "/books/1"}]})
            finally:
                active.remove(host)

        start = time.monotonic()
        results = self.run_search_transport(handle, budget=0.05)
        elapsed = time.monotonic() - start
        self.assertEqual(results["books"]["status"], "ok")
        self.assertEqual(results["books"]["items"][0]["title"], "fast book")
        self.assertEqual(results["news"], {"items": [], "status": "unavailable"})
        self.assertEqual(results["youtube"], {"items": [], "status": "unavailable"})
        self.assertEqual(cancelled, {"crawler-worker", "youtube-memo"})
        self.assertFalse(active)
        self.assertLess(elapsed, 1.0)

    def test_dashboard_deadline_does_not_wait_for_dns_worker_shutdown(self):
        prepare_service_import("portal-web")
        from app.services import global_search

        release = threading.Event()
        started = threading.Event()
        finished = threading.Event()

        def stalled_dns(*args, **kwargs):
            started.set()
            release.wait(2)
            finished.set()
            raise socket.gaierror("simulated DNS failure")

        environment = {"DEMO_MODE": "", "NO_PROXY": "*", **{
            f"{name.upper()}_SEARCH_URL": f"http://search-delay.test/{name}"
            for name in ("news", "youtube", "books")
        }}
        with patch.dict(os.environ, environment), patch.object(
            global_search, "SEARCH_BUDGET_SECONDS", 0.15
        ), patch("socket.getaddrinfo", side_effect=stalled_dns):
            with TestClient(self.load_app()) as client:
                try:
                    response = client.get("/?q=dns")
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("현재 응답 없음", response.text)
                    self.assertTrue(started.is_set())
                    self.assertFalse(finished.is_set(), "response waited for DNS worker shutdown")
                finally:
                    release.set()

    def test_search_deadline_interrupts_real_http_drip_body(self):
        prepare_service_import("portal-web")
        from app.services import global_search

        stop = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = b'{"results": []}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    if self.path.startswith("/news"):
                        for byte in body:
                            if stop.wait(0.03):
                                return
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                    else:
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        environment = {"DEMO_MODE": "", **{
            f"{name.upper()}_SEARCH_URL": f"{base_url}/{name}"
            for name in ("news", "youtube", "books")
        }}
        try:
            with patch.dict(os.environ, environment), patch.object(
                global_search, "SEARCH_BUDGET_SECONDS", 0.15
            ):
                results = asyncio.run(global_search.search_all("drip"))
            self.assertEqual(results["news"]["status"], "unavailable")
            self.assertEqual(results["youtube"]["status"], "ok")
            self.assertEqual(results["books"]["status"], "ok")
        finally:
            stop.set()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_blank_and_demo_search_do_not_open_http_client(self):
        prepare_service_import("portal-web")
        from app.services import global_search

        with patch("httpx.AsyncClient") as client:
            with patch.dict(os.environ, {"DEMO_MODE": ""}):
                results = asyncio.run(global_search.search_all("   "))
                self.assertTrue(all(item == {"items": [], "status": "ok"} for item in results.values()))
            with patch.dict(os.environ, {"DEMO_MODE": "true"}):
                results = asyncio.run(global_search.search_all("sample"))
                self.assertTrue(all(item["items"] for item in results.values()))
            client.assert_not_called()

    def test_search_invalid_json_is_isolated(self):
        async def handle(request):
            if request.url.host == "crawler-worker":
                return httpx.Response(200, content=b"not json")
            return httpx.Response(200, json={"results": []})

        results = self.run_search_transport(handle)
        self.assertEqual(results["news"]["status"], "unavailable")
        self.assertEqual(results["books"]["status"], "ok")

    def test_search_http_error_does_not_hide_other_services(self):
        async def handle(request):
            status = 503 if request.url.host == "crawler-worker" else 200
            return httpx.Response(status, json={"results": []})

        results = self.run_search_transport(handle)
        self.assertEqual(results["news"]["status"], "unavailable")
        self.assertEqual(results["youtube"], {"items": [], "status": "ok"})
        self.assertEqual(results["books"], {"items": [], "status": "ok"})

    def test_search_all_keeps_partial_results_when_one_service_is_unavailable(self):
        prepare_service_import("portal-web")
        os.environ.pop("DEMO_MODE", None)
        from app.services import global_search

        def open_endpoint(url):
            if "crawler-worker" in url:
                raise OSError("internal failure detail")
            return _search_response(b'{"results": [{"title": "available", "url": "#"}]}')

        with patch("httpx.AsyncClient.get", side_effect=open_endpoint):
            results = asyncio.run(global_search.search_all("test"))

        self.assertEqual(results["news"], {"items": [], "status": "unavailable"})
        self.assertEqual(results["youtube"]["status"], "ok")
        self.assertEqual(results["youtube"]["items"][0]["title"], "available")

    def test_search_all_marks_valid_empty_payload_as_ok(self):
        prepare_service_import("portal-web")
        os.environ.pop("DEMO_MODE", None)
        from app.services import global_search

        with patch("httpx.AsyncClient.get", return_value=_search_response(b'{"results": []}')):
            results = asyncio.run(global_search.search_all("empty"))

        self.assertEqual(results["news"], {"items": [], "status": "ok"})

    def search_with_news_payload(self, payload):
        prepare_service_import("portal-web")
        os.environ.pop("DEMO_MODE", None)
        from app.services import global_search

        with patch("httpx.AsyncClient.get", return_value=_search_response(payload)):
            return asyncio.run(global_search.search_all("malformed"))

    def test_search_all_marks_error_payload_without_results_as_unavailable(self):
        results = self.search_with_news_payload(b'{"error": "upstream failure"}')

        self.assertEqual(results["news"], {"items": [], "status": "unavailable"})

    def test_search_all_marks_non_list_results_as_unavailable(self):
        results = self.search_with_news_payload(b'{"results": "invalid"}')

        self.assertEqual(results["news"], {"items": [], "status": "unavailable"})

    def test_search_all_marks_non_dict_result_item_as_unavailable(self):
        results = self.search_with_news_payload(b'{"results": [null]}')

        self.assertEqual(results["news"], {"items": [], "status": "unavailable"})

    def test_dashboard_search_get_uses_host_specific_service_result_urls(self):
        environment = {
            "DEMO_MODE": "",
            "NEWS_SEARCH_URL": "http://crawler-worker:8001/api/search",
            "YOUTUBE_SEARCH_URL": "http://youtube-memo:8002/api/search",
            "BOOKS_SEARCH_URL": "http://book-memo:8003/api/search",
        }

        def open_endpoint(url):
            if "crawler-worker" in url:
                result = {"title": "뉴스 고유 결과", "url": "/articles/1"}
            elif "youtube-memo" in url:
                result = {"title": "유튜브 고유 결과", "url": "/videos/1"}
            else:
                result = {"title": "책 고유 결과", "url": "/books/1"}
            return _search_response(json.dumps({"results": [result]}).encode("utf-8"))

        expected_urls = {
            "https://len.pe.kr": (
                'href="https://news.len.pe.kr/articles/1"',
                'href="https://memo.len.pe.kr/videos/1"',
                'href="https://books.len.pe.kr/books/1"',
            ),
            "http://localhost": (
                'href="http://127.0.0.1:8001/articles/1"',
                'href="http://127.0.0.1:8002/videos/1"',
                'href="http://127.0.0.1:8003/books/1"',
            ),
        }

        with patch.dict(os.environ, environment, clear=False):
            for base_url, urls in expected_urls.items():
                with self.subTest(base_url=base_url):
                    app = self.load_app()
                    with patch("httpx.AsyncClient.get", side_effect=open_endpoint):
                        with TestClient(app, base_url=base_url, raise_server_exceptions=False) as client:
                            response = client.get("/?q=audit")

                    self.assertEqual(response.status_code, 200)
                    for expected_url in urls:
                        self.assertIn(expected_url, response.text)

    def test_dashboard_search_get_keeps_partial_empty_and_absolute_results(self):
        environment = {
            "DEMO_MODE": "",
            "NEWS_SEARCH_URL": "http://crawler-worker:8001/api/search",
            "YOUTUBE_SEARCH_URL": "http://youtube-memo:8002/api/search",
            "BOOKS_SEARCH_URL": "http://book-memo:8003/api/search",
        }

        def open_endpoint(url):
            if "crawler-worker" in url:
                raise OSError("news unavailable")
            if "youtube-memo" in url:
                return _search_response(b'{"results": []}')
            return _search_response(
                '{"results": [{"title": "책 절대 결과", "url": "https://books.example/books/1"}]}'.encode(
                    "utf-8"
                )
            )

        with patch.dict(os.environ, environment, clear=False):
            app = self.load_app()
            with patch("httpx.AsyncClient.get", side_effect=open_endpoint):
                with TestClient(app, base_url="https://len.pe.kr", raise_server_exceptions=False) as client:
                    response = client.get("/?q=audit")

        self.assertEqual(response.status_code, 200)
        self.assertIn("현재 응답 없음", response.text)
        self.assertIn("검색 결과가 없습니다.", response.text)
        self.assertIn('href="https://books.example/books/1"', response.text)

    def test_dashboard_shows_unavailable_status_without_endpoint_details(self):
        app = self.load_app()
        search_results = {
            "news": {"items": [], "status": "unavailable"},
            "youtube": {"items": [], "status": "ok"},
            "books": {"items": [], "status": "ok"},
        }

        with patch("app.routers.dashboard.search_all", return_value=search_results):
            with TestClient(app) as client:
                response = client.get("/?q=test")

        self.assertEqual(response.status_code, 200)
        self.assertIn("현재 응답 없음", response.text)
        self.assertIn("검색 결과가 없습니다.", response.text)
        self.assertNotIn("crawler-worker", response.text)

    def test_portal_home_url_uses_local_address_on_localhost(self):
        prepare_service_import("book-memo")
        os.environ.pop("PORTAL_HOME_URL", None)
        from app.services.host_urls import portal_home_url

        self.assertEqual(portal_home_url("127.0.0.1"), "http://127.0.0.1:8000/")

    def test_portal_home_url_uses_public_address_outside_local(self):
        prepare_service_import("youtube-memo")
        os.environ.pop("PORTAL_HOME_URL", None)
        from app.services.host_urls import portal_home_url

        self.assertEqual(portal_home_url("memo.len.pe.kr"), "https://len.pe.kr/")

    def test_portal_home_url_uses_env_public_address(self):
        prepare_service_import("youtube-memo")
        os.environ["PORTAL_HOME_URL"] = "https://example.com/"
        from app.services.host_urls import portal_home_url

        try:
            self.assertEqual(portal_home_url("memo.len.pe.kr"), "https://example.com/")
        finally:
            os.environ.pop("PORTAL_HOME_URL", None)

    def test_dashboard_service_urls_follow_host_mode(self):
        prepare_service_import("portal-web")
        from app.services.host_urls import service_url

        self.assertEqual(service_url("NEWS_SERVICE_URL", "127.0.0.1", ""), "http://127.0.0.1:8001")
        self.assertEqual(service_url("YOUTUBE_MEMO_URL", "127.0.0.1", ""), "http://127.0.0.1:8002")
        self.assertEqual(service_url("BOOK_MEMO_URL", "127.0.0.1", ""), "http://127.0.0.1:8003")
        self.assertEqual(service_url("NEWS_SERVICE_URL", "portal.len.pe.kr", ""), "https://news.len.pe.kr")
        self.assertEqual(service_url("YOUTUBE_MEMO_URL", "portal.len.pe.kr", ""), "https://memo.len.pe.kr")
        self.assertEqual(service_url("BOOK_MEMO_URL", "portal.len.pe.kr", ""), "https://books.len.pe.kr")

    def test_dashboard_keeps_original_service_hub_with_existing_entry_points(self):
        """Fails if a visual refactor replaces the original hub or disconnects routes."""
        app = self.load_app()

        with TestClient(app) as client:
            response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("<h1>Len의 개인서버</h1>", response.text)
        self.assertIn('class="service-grid three"', response.text)
        for url in ("/news", "/memo", "/books", "/files", "/admin/status"):
            self.assertIn(f'href="{url}"', response.text)
        self.assertIn('class="service-card disabled" href="#"', response.text)
        self.assertIn('class="global-search" method="get" action="/"', response.text)
        self.assertIn('name="q"', response.text)
        self.assertIn('data-track-event="global_search_submitted"', response.text)
        self.assertIn('data-track-event="service_opened"', response.text)

    def test_admin_status_keeps_original_title_and_login_form_action(self):
        """Fails if the original admin page or its authentication route is disconnected."""
        app = self.load_app()

        with TestClient(app) as client:
            response = client.get("/admin/status")

        self.assertEqual(response.status_code, 200)
        self.assertIn("<h1>관리자 상태</h1>", response.text)
        self.assertIn('class="admin-login" action="/admin/status" method="post"', response.text)
        self.assertIn('name="password"', response.text)

    def test_admin_status_context_combines_server_and_security_data(self):
        prepare_service_import("portal-web")
        from app.services.admin_status import build_admin_status_context

        context = build_admin_status_context(
            system_status={"overall_status": "warning", "warnings": ["backup_missing"]},
            service_health=[{"name": "뉴스 허브", "status": "ok"}],
            security={"headers": ["X-Frame-Options"], "recent_events": []},
        )

        self.assertEqual(context["system_status"]["overall_status"], "warning")
        self.assertEqual(context["service_health"][0]["name"], "뉴스 허브")
        self.assertEqual(context["security_status"]["headers"], ["X-Frame-Options"])
        self.assertTrue(context["has_warnings"])

    def test_admin_status_checked_at_is_formatted_for_display(self):
        prepare_service_import("portal-web")
        from app.services.admin_status import format_status_checked_at

        self.assertEqual(
            format_status_checked_at("2026-07-09T01:02:03+00:00"),
            "2026-07-09 10:02",
        )
        self.assertEqual(
            format_status_checked_at("2026-07-09 10:02:03 KST"),
            "2026-07-09 10:02",
        )
        self.assertEqual(format_status_checked_at(""), "unknown")
        self.assertEqual(format_status_checked_at("not-a-timestamp"), "unknown")

    def test_admin_status_checked_at_treats_naive_iso_as_utc(self):
        """Fails if legacy timezone-naive timestamps are treated as already-KST."""
        prepare_service_import("portal-web")
        from app.services.admin_status import format_status_checked_at

        self.assertEqual(
            format_status_checked_at("2026-07-09T01:02:03"),
            "2026-07-09 10:02",
        )

    def test_admin_status_checked_at_formats_rfc822_utc_as_kst(self):
        """Fails if RFC 822 status timestamps bypass the display-time parser."""
        prepare_service_import("portal-web")
        from app.services.admin_status import format_status_checked_at

        self.assertEqual(
            format_status_checked_at("Fri, 10 Jul 2026 15:58:15 +0000"),
            "2026-07-11 00:58",
        )

    def test_admin_status_login_disables_cache(self):
        app = self.load_app()

        with TestClient(app) as client:
            response = client.get("/admin/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store, no-cache, must-revalidate, max-age=0")
        self.assertEqual(response.headers["pragma"], "no-cache")
        self.assertEqual(response.headers["expires"], "0")

    def test_admin_status_failure_disables_cache(self):
        os.environ["FILE_MANAGER_PASSWORD"] = "secret"
        try:
            app = self.load_app()

            with TestClient(app) as client:
                response = client.post(
                    "/admin/status",
                    data={"password": "wrong"},
                    headers={"Origin": "http://testserver"},
                )

            self.assertIn(response.status_code, {401, 403, 429})
            self.assertEqual(response.headers["cache-control"], "no-store, no-cache, must-revalidate, max-age=0")
            self.assertEqual(response.headers["pragma"], "no-cache")
            self.assertEqual(response.headers["expires"], "0")
        finally:
            os.environ.pop("FILE_MANAGER_PASSWORD", None)

    def test_admin_status_uses_dedicated_password_when_configured(self):
        environment_keys = (
            "ADMIN_STATUS_PASSWORD",
            "AUTH_RATE_LIMIT_STATE_PATH",
            "DELETE_PASSWORD",
            "DEMO_MODE",
            "FILE_MANAGER_PASSWORD",
            "SECURITY_LOG_PATH",
        )
        original_environment = {key: os.environ.get(key) for key in environment_keys}
        try:
            with tempfile.TemporaryDirectory() as tempdir:
                os.environ["ADMIN_STATUS_PASSWORD"] = "dedicated-admin-password"
                os.environ["FILE_MANAGER_PASSWORD"] = "file-manager-password"
                os.environ["DELETE_PASSWORD"] = "delete-password"
                os.environ["AUTH_RATE_LIMIT_STATE_PATH"] = str(Path(tempdir) / "auth-rate-limit.json")
                os.environ["SECURITY_LOG_PATH"] = str(Path(tempdir) / "security-events.txt")
                os.environ["DEMO_MODE"] = "true"
                app = self.load_app()

                with TestClient(app) as client:
                    delete_password = client.post(
                        "/admin/status",
                        data={"password": "delete-password"},
                        headers={"Origin": "http://testserver"},
                    )
                    file_manager_password = client.post(
                        "/admin/status",
                        data={"password": "file-manager-password"},
                        headers={"Origin": "http://testserver"},
                    )
                    dedicated_password = client.post(
                        "/admin/status",
                        data={"password": "dedicated-admin-password"},
                        headers={"Origin": "http://testserver"},
                    )

            self.assertEqual(delete_password.status_code, 401)
            self.assertEqual(file_manager_password.status_code, 401)
            self.assertEqual(dedicated_password.status_code, 200)
        finally:
            for key, value in original_environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_admin_status_page_renders_actual_host_collection_time(self):
        """Fails if the page substitutes the dashboard request time for the host sample time."""
        os.environ["FILE_MANAGER_PASSWORD"] = "secret"
        try:
            app = self.load_app()
            system_status = {
                "captured_at": "2026-07-09T01:02:03+00:00",
                "overall_status": "ok",
                "host": {
                    "captured_at": "2026-07-09T00:40:00+00:00",
                    "cpu_percent": 12,
                    "memory_percent": 34,
                    "disk_percent": 56,
                    "source": "agent",
                },
                "disk": {"percent": 56, "level": "ok"},
                "files": {"file_count": 2, "total_bytes": 100},
                "backup": {"latest_name": "", "status": "ok", "status_reason": "backup_recent"},
                "containers": [],
                "status_checks": [],
                "warnings": [],
            }
            security = {
                "headers": [],
                "file_policy": {
                    "max_upload_mb": 50,
                    "blocked_extensions": [],
                    "allowed_extensions": [],
                },
                "log_files": [],
                "recent_events": [],
                "log_path": "/tmp/security.log",
            }

            with patch(
                "app.routers.admin.get_dashboard_status",
                return_value=system_status,
            ), patch("app.routers.admin.get_service_health", return_value=[]), patch(
                "app.routers.admin.security_status",
                return_value=security,
            ):
                with TestClient(app) as client:
                    response = client.post(
                        "/admin/status",
                        data={"password": "secret"},
                        headers={"Origin": "http://testserver"},
                    )

            self.assertEqual(response.status_code, 200)
            self.assertIn("호스트 수집 시각:", response.text)
            self.assertIn(
                '<time datetime="2026-07-09T00:40:00+00:00">2026-07-09 09:40</time>',
                response.text,
            )
            self.assertNotIn("2026-07-09 10:02", response.text)
        finally:
            os.environ.pop("FILE_MANAGER_PASSWORD", None)

    def test_admin_status_page_shows_unknown_without_host_collection_time(self):
        """Fails if missing host timestamps are presented as a misleading request timestamp."""
        os.environ["FILE_MANAGER_PASSWORD"] = "secret"
        try:
            app = self.load_app()
            system_status = {
                "captured_at": "2026-07-09T01:02:03+00:00",
                "overall_status": "unavailable",
                "host": {
                    "captured_at": None,
                    "cpu_percent": None,
                    "memory_percent": None,
                    "disk_percent": None,
                    "source": "unavailable",
                },
                "disk": {"percent": None, "level": "unknown"},
                "files": {"file_count": 0, "total_bytes": 0},
                "backup": {"latest_name": "", "status": "ok", "status_reason": "backup_recent"},
                "containers": [],
                "status_checks": [],
                "warnings": ["system_agent_unavailable"],
            }
            security = {
                "headers": [],
                "file_policy": {
                    "max_upload_mb": 50,
                    "blocked_extensions": [],
                    "allowed_extensions": [],
                },
                "log_files": [],
                "recent_events": [],
                "log_path": "/tmp/security.log",
            }

            with patch(
                "app.routers.admin.get_dashboard_status",
                return_value=system_status,
            ), patch("app.routers.admin.get_service_health", return_value=[]), patch(
                "app.routers.admin.security_status",
                return_value=security,
            ):
                with TestClient(app) as client:
                    response = client.post(
                        "/admin/status",
                        data={"password": "secret"},
                        headers={"Origin": "http://testserver"},
                    )
                    system_status["host"]["captured_at"] = "not-a-timestamp"
                    invalid_response = client.post(
                        "/admin/status",
                        data={"password": "secret"},
                        headers={"Origin": "http://testserver"},
                    )

            for status_response in (response, invalid_response):
                self.assertEqual(status_response.status_code, 200)
                self.assertIn("호스트 수집 시각:", status_response.text)
                self.assertIn("<time>unknown</time>", status_response.text)
                self.assertNotIn("datetime=", status_response.text)
                self.assertNotIn("2026-07-09 10:02:03 KST", status_response.text)
        finally:
            os.environ.pop("FILE_MANAGER_PASSWORD", None)

    def test_admin_status_renders_only_global_homeops_actions(self):
        """Fails if removed service-level operations reappear on the administrator page."""
        os.environ["ADMIN_STATUS_PASSWORD"] = "secret"
        try:
            app = self.load_app()

            with TestClient(app) as client:
                response = client.post(
                    "/admin/status",
                    data={"password": "secret"},
                    headers={"Origin": "http://testserver"},
                )

            self.assertEqual(response.status_code, 200)
            self.assertIn('class="homeops-action-grid"', response.text)
            self.assertIn('class="homeops-diagnose-button"', response.text)
            self.assertIn('action="/admin/homeops/restart-all"', response.text)
            self.assertIn("전체 상태 진단", response.text)
            self.assertIn("전체 재시작", response.text)
            self.assertNotIn('name="service"', response.text)
            self.assertNotIn("최근 조치 이력", response.text)
            self.assertNotIn("재시작 실행", response.text)
            self.assertNotIn(">승인<", response.text)
        finally:
            os.environ.pop("ADMIN_STATUS_PASSWORD", None)

    def test_authenticated_admin_status_displays_only_sanitized_recent_recovery_events(self):
        """Fails if recovery history bypasses the bridge, leaks raw fields, or exceeds ten rows."""
        os.environ["ADMIN_STATUS_PASSWORD"] = "secret"
        recovery_events = [
            {
                "timestamp": f"2026-09-11T0{index}:30:00+00:00",
                "component": f"component-{index}",
                "event": "accepted" if index == 0 else "health_restored",
                "status": "accepted" if index == 0 else "ok",
                "action": "restart_tunnel" if index == 0 else "none",
                "error": "raw-error-must-not-appear",
                "path": "/host/recovery-events.jsonl",
            }
            for index in range(12)
        ]
        try:
            app = self.load_app()
            with patch(
                "app.routers.admin.get_recovery_events",
                return_value=recovery_events,
                create=True,
            ) as get_recovery_events:
                with TestClient(app) as client:
                    response = client.post(
                        "/admin/status",
                        data={"password": "secret"},
                        headers={"Origin": "http://testserver"},
                    )

            self.assertEqual(response.status_code, 200)
            get_recovery_events.assert_called_once_with()
            self.assertIn("최근 자동복구 이력", response.text)
            self.assertIn("조치 실행 수락", response.text)
            self.assertIn("실제 복구 완료", response.text)
            self.assertIn("2026-09-11 09:30", response.text)
            for index in range(10):
                self.assertIn(f"component-{index}", response.text)
            self.assertNotIn("component-10", response.text)
            self.assertNotIn("component-11", response.text)
            self.assertNotIn("raw-error-must-not-appear", response.text)
            self.assertNotIn("/host/recovery-events.jsonl", response.text)
        finally:
            os.environ.pop("ADMIN_STATUS_PASSWORD", None)

    def test_admin_login_issues_homeops_session_that_allows_diagnosis_without_reentering_password(self):
        """A successful administrator login is the sole point that creates the HomeOps session."""
        os.environ["ADMIN_STATUS_PASSWORD"] = "secret"
        try:
            app = self.load_app()

            with TestClient(app) as client:
                login_page = client.get("/admin/status")
                authenticated_page = client.post(
                    "/admin/status",
                    data={"password": "secret"},
                    headers={"Origin": "http://testserver"},
                )
                diagnosis = client.post(
                    "/admin/homeops/diagnose",
                    headers={"Origin": "http://testserver"},
                    follow_redirects=False,
                )
                redirected_page = client.get(diagnosis.headers["location"])

            self.assertNotIn("homeops_admin_session=", login_page.headers.get("set-cookie", ""))
            self.assertIn("homeops_admin_session=", authenticated_page.headers.get("set-cookie", ""))
            self.assertEqual(diagnosis.status_code, 303)
            self.assertIn("HomeOps 운영 보조", redirected_page.text)
            self.assertNotIn('class="admin-login"', redirected_page.text)
        finally:
            os.environ.pop("ADMIN_STATUS_PASSWORD", None)


if __name__ == "__main__":
    unittest.main()
