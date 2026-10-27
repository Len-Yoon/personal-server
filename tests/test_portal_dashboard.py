import asyncio
import importlib
import importlib.util
import io
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


SERVICE_CONTRACT_SERVICES = ("portal-web", "crawler-worker", "book-memo", "youtube-memo")


def _load_service_contract_module(service: str, module: str):
    path = Path(__file__).resolve().parents[1] / service / "app" / "services" / f"{module}.py"
    spec = importlib.util.spec_from_file_location(f"contract_{service.replace('-', '_')}_{module}", path)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def _search_response(payload):
    return httpx.Response(200, content=payload, request=httpx.Request("GET", "http://search.test"))


class PortalDashboardTests(unittest.TestCase):
    def test_forwarded_host_port_keeps_public_portal_return_url_across_services(self):
        for service in SERVICE_CONTRACT_SERVICES:
            with self.subTest(service=service):
                urls = _load_service_contract_module(service, "host_urls")
                host = urls.request_host_from_headers(
                    {"x-forwarded-host": "memo.len.pe.kr:443, proxy.internal", "host": "localhost:8000"}
                )
                self.assertEqual(host, "memo.len.pe.kr")
                self.assertEqual(urls.portal_home_url(host), "https://len.pe.kr/")

    def test_local_host_with_port_keeps_local_portal_across_services(self):
        for service in SERVICE_CONTRACT_SERVICES:
            with self.subTest(service=service):
                urls = _load_service_contract_module(service, "host_urls")
                host = urls.request_host_from_headers({"host": "localhost:8001"})
                self.assertEqual(urls.portal_home_url(host), "http://127.0.0.1:8000/")

    def test_utc_display_keeps_same_korean_minute_across_services(self):
        value = "2026-07-09T01:02:03+00:00"
        for service in SERVICE_CONTRACT_SERVICES:
            with self.subTest(service=service):
                if service == "portal-web":
                    formatted = _load_service_contract_module(service, "admin_status").format_status_checked_at(value)
                elif service == "crawler-worker":
                    formatted = _load_service_contract_module(service, "datetime_format").format_news_datetime(value)
                else:
                    formatted = _load_service_contract_module(service, "datetime_format").format_display_datetime(value)
                self.assertEqual(formatted, "2026-07-09 10:02")

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

    def test_service_health_malformed_response_does_not_hide_other_services(self):
        system_status = self.reload_system_status("")
        invalid_bodies = (
            b"[]", b"null", b"1", b"true", b'"ok"', b"\xff",
            b'{"status":true}', b'{"status":[]}', b'{"status":{}}',
            b'{"status":null}', b'{"status":""}',
            b'{"status":"' + b'x' * 65 + b'"}',
            b'[' * 1500 + b'0' + b']' * 1500,
            b'{"unused":' + b'1' * 5000 + b'}',
        )
        for body in invalid_bodies:
            with self.subTest(body=body[:40]):
                responses = [io.BytesIO(body), io.BytesIO(b'{"status":"ok"}'),
                             io.BytesIO(b'{}'), io.BytesIO(b'{"status":"degraded"}')]
                with patch.object(system_status, "urlopen", side_effect=responses):
                    services = system_status.get_service_health()
                self.assertEqual([item["status"] for item in services],
                                 ["unavailable", "ok", "unknown", "degraded"])

    def test_service_health_partial_http_body_is_unavailable(self):
        from http.client import IncompleteRead

        system_status = self.reload_system_status("")
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.read.side_effect = IncompleteRead(b"partial", 20)
        with patch.object(system_status, "urlopen", return_value=response):
            services = system_status.get_service_health()
        self.assertEqual([item["status"] for item in services], ["unavailable"] * 4)

    def test_service_health_rejects_incomplete_http_framing_with_real_parser(self):
        from http.client import HTTPResponse

        system_status = self.reload_system_status("")
        body = b'{"status":"ok"}'
        chunk = format(len(body), "x").encode() + b"\r\n" + body + b"\r\n"

        class MemorySocket:
            def __init__(self, raw):
                self.raw = raw

            def makefile(self, *args):
                return io.BytesIO(self.raw)

        cases = [
            (b"Content-Length: " + str(len(body)).encode() + b"\r\n", body, "ok"),
            (b"Content-Length: 100\r\n", body, "unavailable"),
            (b"Transfer-Encoding: chunked\r\n", chunk + b"0\r\n\r\n", "ok"),
            (b"Transfer-Encoding: chunked\r\n", chunk, "unavailable"),
        ]
        for headers, payload, expected in cases:
            with self.subTest(headers=headers, payload=payload):
                response = HTTPResponse(MemorySocket(b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n" + payload))
                response.begin()
                with patch.object(system_status, "urlopen", return_value=response):
                    result = system_status._check_service({"name": "sample", "url": "http://health.test"}, 1)
                self.assertEqual(result["status"], expected)

    def test_service_health_body_read_is_bounded_and_overflow_rejected(self):
        system_status = self.reload_system_status("")
        limit = 16 * 1024

        class TrackingResponse(io.BytesIO):
            def read(self, size=-1):
                self.requested_size = size
                return super().read(size)

        for size, expected in [(limit, "ok"), (limit + 1, "unavailable")]:
            with self.subTest(size=size):
                body = b'{"status":"ok"}'
                response = TrackingResponse(body + b" " * (size - len(body)))
                with patch.object(system_status, "urlopen", return_value=response):
                    result = system_status._check_service({"name": "sample", "url": "http://health.test"}, 1)
                self.assertEqual(result["status"], expected)
                self.assertEqual(response.requested_size, limit + 1)

    def test_admin_health_results_remain_visible_after_one_malformed_service(self):
        system_status = self.reload_system_status("")
        responses = [io.BytesIO(b"[]"), io.BytesIO(b'{"status":"ok"}'),
                     io.BytesIO(b'{}'), io.BytesIO(b'{"status":"raw-status-must-not-appear"}')]
        with patch.object(system_status, "urlopen", side_effect=responses):
            services = system_status.get_service_health()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "ADMIN_STATUS_PASSWORD": "test-health-password", "DEMO_MODE": "true",
            "AUTH_RATE_LIMIT_STATE_PATH": str(Path(directory) / "auth.json"),
            "SECURITY_LOG_PATH": str(Path(directory) / "security.log"),
        }):
            app = self.load_app()
            from app.routers import admin

            with patch.object(admin, "get_service_health", return_value=services), \
                 patch.object(admin, "get_dashboard_status", return_value=system_status._demo_status()), \
                 patch.object(admin, "get_service_backups", return_value={}), \
                 patch.object(admin, "get_recovery_events", return_value=[]), \
                 patch.object(admin, "get_homeops_service") as homeops:
                homeops.return_value.latest_summary.return_value = {}
                homeops.return_value.operation_history.return_value = []
                with TestClient(app) as client:
                    response = client.post("/admin/status", data={"password": "test-health-password"},
                                           headers={"Origin": "http://testserver"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("뉴스 허브 · 연결·응답 확인 필요", response.text)
            self.assertIn("유튜브 메모 · 정상", response.text)
            self.assertIn("책 메모 · 상태 확인 필요", response.text)
            self.assertIn("시스템 상태 · 상태 확인 필요", response.text)
            self.assertNotIn("raw-status-must-not-appear", response.text)
            self.assertNotIn("http://crawler-worker:8001/health", response.text)

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

    def run_search_transport(self, handler, query="test", budget=1.5, source="all"):
        prepare_service_import("portal-web")
        from app.services import global_search

        client_type = httpx.AsyncClient

        def make_client(**kwargs):
            return client_type(transport=httpx.MockTransport(handler), **kwargs)

        with patch.dict(os.environ, {"DEMO_MODE": ""}), patch(
            "httpx.AsyncClient", side_effect=make_client
        ), patch.object(global_search, "SEARCH_BUDGET_SECONDS", budget):
            return asyncio.run(global_search.search_all(query, source=source))

    def test_search_source_filter_only_contacts_selected_service(self):
        contacted = []

        async def handle(request):
            contacted.append(request.url.host)
            return httpx.Response(200, json={"results": [{"title": "책 결과", "url": "/books/1"}]})

        results = self.run_search_transport(handle, source="books")

        self.assertEqual(contacted, ["book-memo"])
        self.assertEqual(list(results), ["books"])
        self.assertEqual(results["books"]["items"][0]["title"], "책 결과")

    def test_unknown_search_source_falls_back_to_all_services(self):
        contacted = set()

        async def handle(request):
            contacted.add(request.url.host)
            return httpx.Response(200, json={"results": []})

        results = self.run_search_transport(handle, source="unexpected")

        self.assertEqual(len(contacted), 3)
        self.assertEqual(list(results), ["news", "youtube", "books"])

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
        from app.services.global_search import SearchMetrics

        metrics = SearchMetrics()
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
            ), patch.object(global_search, "SEARCH_METRICS", metrics):
                results = asyncio.run(global_search.search_all("drip"))
            self.assertEqual(results["news"]["status"], "unavailable")
            self.assertEqual(results["youtube"]["status"], "ok")
            self.assertEqual(results["books"]["status"], "ok")
            self.assertIn('portal_search_requests_total{outcome="partial"} 1', metrics.render())
            self.assertIn(
                'portal_search_source_requests_total{outcome="timeout",source="news"} 1',
                metrics.render(),
            )
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
                selected = asyncio.run(global_search.search_all("sample", source="youtube"))
                self.assertEqual(list(selected), ["youtube"])
                self.assertTrue(selected["youtube"]["items"])
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

    def test_dashboard_keeps_selected_search_source(self):
        app = self.load_app()
        search_results = {"books": {"items": [], "status": "ok"}}

        with patch("app.routers.dashboard.search_all", return_value=search_results) as search:
            with TestClient(app) as client:
                response = client.get("/?q=test&source=books")

        self.assertEqual(response.status_code, 200)
        self.assertIn('<option value="books" selected>', response.text)
        self.assertIn("검색 결과가 없습니다.", response.text)
        self.assertNotIn('<h3>뉴스</h3>', response.text)
        self.assertEqual(search.call_args.kwargs["source"], "books")

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


class PortalUsabilityTests(unittest.TestCase):
    def test_backup_display_requires_current_ordered_portal_evidence(self):
        from datetime import datetime, timezone
        module = _load_service_contract_module("portal-web", "admin_status")
        now = datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc)
        evidence = dict(scope="portal", verified=True, status_reason="backup_verified",
                        backup_completed_at="2026-10-12T17:30:00Z",
                        restore_verified_at="2026-10-12T17:38:00Z",
                        evidence_expires_at="2026-10-13T17:38:00Z")
        display = module.backup_status_for_display(evidence, now)
        self.assertEqual(display["backup_at"], "2026-10-13 02:30")
        self.assertTrue(display["label"].startswith("정상"))
        for changes in ({"verified": "true"}, {"scope": "all"}, {"verified": False},
                        {"status_reason": "backup_recent"}, {"restore_verified_at": "2026-10-12T17:00:00Z"},
                        {"backup_completed_at": "2026-10-14T00:00:00Z"},
                        {"evidence_expires_at": "2026-10-12T18:00:00Z"},
                        {"backup_completed_at": "bad"}, {"backup_completed_at": "2026-10-12T17:30:00"},
                        {"evidence_expires_at": "9999-12-31T23:59:59Z"}):
            with self.subTest(changes=changes):
                self.assertFalse(module.backup_status_for_display({**evidence, **changes}, now)["label"].startswith("정상"))
        for missing in (None, {}, {"exists": True, "latest_name": "recent"}, "invalid"):
            self.assertEqual(module.backup_status_for_display(missing, now)["backup_at"], "미제공")

    def test_cleanup_reason_and_unknown_counts_render_for_each_service(self):
        from datetime import datetime, timezone
        module = _load_service_contract_module("portal-web", "admin_status")
        now = datetime(2026, 10, 14, tzinfo=timezone.utc)
        for scope in ("portal", "book-memo", "youtube-memo", "crawler-worker", "car-care-db-only"):
            with self.subTest(scope=scope):
                value = dict(scope=scope, retention_status="failure", retention_reason="deletion_unconfirmed",
                             retention_kept_count=None, retention_deleted_count=1, retention_reclaimed_bytes=1024,
                             retention_completed_at="2026-10-13T00:00:00Z")
                row = module.backup_status_for_display(value, now, scope)
                self.assertEqual(row["retention_reason"], "삭제 확인 불가")
                self.assertEqual(row["retention_kept_count"], "확인 필요")
                self.assertEqual(row["retention_deleted_count"], "1")
                self.assertEqual(row["retention_reclaimed_bytes"], "1024")
                self.assertEqual(row["retention_at"], "2026-10-13 09:00")
                self.assertEqual(row["backup_at"], "미제공")
                value["retention_reason"] = "private-path token"
                self.assertEqual(module.backup_status_for_display(value, now, scope)["retention_reason"], "확인 필요")

    def test_car_backup_display_keeps_skip_attempt_separate_from_proof(self):
        from datetime import datetime, timezone
        module = _load_service_contract_module("portal-web", "admin_status")
        now = datetime(2026, 10, 14, 0, 0, tzinfo=timezone.utc)
        evidence = dict(scope="car-care-db-only", verified=True, status_reason="backup_verified",
                        backup_completed_at="2026-10-12T17:39:00Z",
                        restore_verified_at="2026-10-12T17:38:00Z",
                        evidence_expires_at="2026-10-20T17:38:00Z",
                        last_attempt_at="2026-10-13T17:30:00Z", last_attempt_status="skipped_unchanged",
                        retention_status="success", retention_deleted_count=2, secret="must-not-display")
        display = module.backup_status_for_display(evidence, now, "car-care-db-only")
        self.assertEqual(display["backup_at"], "2026-10-13 02:39")
        self.assertEqual(display["restore_at"], "2026-10-13 02:38")
        self.assertEqual(display["attempt_at"], "2026-10-14 02:30")
        self.assertEqual(display["attempt_status"], "변경 없음")
        self.assertEqual(display["retention_deleted_count"], "2")
        self.assertNotIn("secret", display)
        self.assertTrue(display["label"].startswith("정상"))
        retention_failure = module.backup_status_for_display(
            {**evidence, "last_attempt_status": "retention_failed", "retention_status": "failure"}, now, "car-care-db-only")
        self.assertTrue(retention_failure["label"].startswith("정상"))
        self.assertEqual(retention_failure["attempt_status"], "백업 완료 · 정리 실패")
        self.assertEqual(retention_failure["retention_status"], "실패 · 확인 필요")
        expired = module.backup_status_for_display(evidence, datetime(2026, 10, 21, tzinfo=timezone.utc), "car-care-db-only")
        self.assertEqual(expired["label"], "확인 필요 · 복원 증적 만료")
        for changes in ({"scope": "all"}, {"backup_completed_at": "bad"},
                        {"backup_completed_at": "2026-10-12T17:30:00Z"},
                        {"restore_verified_at": "2026-10-15T00:00:00Z"},
                        {"evidence_expires_at": "2027-10-20T17:38:00Z"},
                        {"verified": "true"}, {"status_reason": "backup_unverified"}):
            with self.subTest(changes=changes):
                invalid = module.backup_status_for_display({**evidence, **changes}, now, "car-care-db-only")
                self.assertTrue(invalid["label"].startswith("확인 필요"))
        for changes in ({"last_attempt_at": "bad"}, {"last_attempt_at": "2026-10-15T00:00:00Z"},
                        {"last_attempt_at": "2026-10-13T17:30:00"}, {"last_attempt_status": "<script>"},
                        {"last_attempt_status": []}, {"retention_status": "<script>"},
                        {"retention_deleted_count": True}, {"retention_deleted_count": -1},
                        {"retention_deleted_count": 10001}):
            with self.subTest(changes=changes):
                invalid = module.backup_status_for_display({**evidence, **changes}, now, "car-care-db-only")
                if "last_attempt" in next(iter(changes)):
                    self.assertEqual(invalid["attempt_status"], "확인 필요")
                elif "retention_status" in changes:
                    self.assertEqual(invalid["retention_status"], "확인 필요")
                else:
                    self.assertEqual(invalid["retention_deleted_count"], "확인 필요")
        unverified = module.backup_status_for_display({**evidence, "backup_completed_at": None}, now, "car-care-db-only")
        self.assertEqual(unverified["backup_at"], "미제공")
        self.assertEqual(unverified["attempt_status"], "변경 없음")

    def test_admin_backup_rows_preserve_existing_services_and_add_car_database(self):
        module = _load_service_contract_module("portal-web", "admin_status")
        context = module.build_admin_status_context({}, [], {}, {"car-care": {
            "scope": "car-care-db-only", "last_attempt_at": "2026-01-01T00:00:00Z",
            "last_attempt_status": "skipped_unchanged", "retention_status": "no_op",
            "retention_deleted_count": 0}})
        self.assertEqual([row["name"] for row in context["service_backups"]], ["Book", "YouTube", "Crawler", "차량 DB"])
        car = context["service_backups"][-1]
        self.assertEqual(car["attempt_status"], "변경 없음")
        self.assertEqual(car["retention_status"], "대상 없음")
        self.assertEqual(car["retention_deleted_count"], "0")
        self.assertTrue(context["backup_status"]["label"].startswith("확인 필요"))

    def test_authenticated_admin_renders_car_attempt_and_retention_details(self):
        with patch.dict(os.environ, {"ADMIN_STATUS_PASSWORD": "test-password"}):
            app = PortalDashboardTests().load_app()
            backups = {"car-care": {
                "scope": "car-care-db-only", "last_attempt_at": "2026-01-01T00:00:00Z",
                "last_attempt_status": "skipped_unchanged", "retention_status": "success",
                "retention_deleted_count": 2, "secret": "must-not-display"}}
            with TestClient(app) as client, patch("app.routers.admin.get_service_backups", return_value=backups):
                response = client.post("/admin/status", data={"password": "test-password"},
                                       headers={"Origin": "http://testserver"})
            self.assertEqual(response.status_code, 200)
            for name in ("Portal", "Book", "YouTube", "Crawler", "차량 DB"):
                self.assertIn(f"{name} 백업 상태", response.text)
            self.assertIn("최근 실행 2026-01-01 09:00 · 변경 없음", response.text)
            self.assertIn("백업 보존 정리 정상 · 정리 시각 미제공 · 보존 개수 확인 필요 · 확정 삭제 개수 2 · 확정 회수 공간 확인 필요 bytes", response.text)
            self.assertNotIn("must-not-display", response.text)

    def test_retry_only_calls_requested_source_and_escapes_results(self):
        from unittest.mock import AsyncMock
        app = PortalDashboardTests().load_app()
        result = {"books": {"status": "ok", "items": [{"title": "<script>alert(1)</script>",
                   "url": "https://books.example/item", "description": "test"}]}}
        with TestClient(app) as client, patch("app.routers.dashboard.search_all", new_callable=AsyncMock, return_value=result) as search:
            response = client.get("/search/retry", params={"source": "books", "q": " test "})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(search.await_args.kwargs["source"], "books")
            self.assertIn("&lt;script&gt;", response.text)
            self.assertNotIn("<script>alert", response.text)
            self.assertIn('data-search-source="books"', response.text)
            self.assertNotIn('data-search-source="news"', response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            search.reset_mock()
            for params in ({"source": "all", "q": "test"}, {"source": "books", "q": " "},
                           {"source": "unknown", "q": "test"}, {"source": "books", "q": "x" * 501}):
                self.assertIn(client.get("/search/retry", params=params).status_code, {400, 422})
            search.assert_not_awaited()

    def test_failed_retry_keeps_retry_control(self):
        from unittest.mock import AsyncMock
        app = PortalDashboardTests().load_app()
        with TestClient(app) as client, patch("app.routers.dashboard.search_all", new_callable=AsyncMock,
                return_value={"news": {"status": "unavailable", "items": []}}):
            response = client.get("/search/retry?source=news&q=test")
        self.assertIn("data-search-retry", response.text)
        self.assertIn("현재 응답 없음", response.text)

    def test_upload_json_ack_only_after_storage_and_legacy_redirect_preserved(self):
        app = PortalDashboardTests().load_app()
        with TestClient(app) as client, patch("app.routers.files._require_file_access"), patch("app.routers.files.file_store.save_uploads", return_value=1) as save:
            response = client.post("/files/uploads", headers={"Accept": "application/json", "Origin": "http://testserver"}, files={"uploads": ("a.txt", b"test")})
            self.assertEqual(response.json(), {"uploaded": True, "count": 1})
            save.assert_called_once()
            legacy = client.post("/files/uploads", headers={"Origin": "http://testserver"}, files={"uploads": ("a.txt", b"test")}, follow_redirects=False)
            self.assertEqual(legacy.status_code, 303)
            save.side_effect = FileExistsError("이미 같은 이름의 파일이 있습니다.")
            blocked = client.post("/files/uploads", headers={"Accept": "application/json", "Origin": "http://testserver"}, files={"uploads": ("a.txt", b"test")})
            self.assertEqual(blocked.status_code, 409)
            self.assertNotIn("uploaded", blocked.json())

    def test_browser_helpers_use_same_origin_relative_urls_behind_https_proxy(self):
        app = PortalDashboardTests().load_app()
        with TestClient(app) as client, patch("app.routers.files._has_file_access", return_value=True):
            for path in ("/", "/files"):
                response = client.get(path, headers={"Host": "len.pe.kr", "X-Forwarded-Proto": "https"})
                self.assertEqual(response.status_code, 200)
                self.assertIn('<script src="/static/js/portal-ux.js"></script>', response.text)
                self.assertNotIn('src="http://len.pe.kr/static/js/portal-ux.js"', response.text)

    def test_backup_details_hidden_without_admin_authentication(self):
        with patch.dict(os.environ, {"ADMIN_STATUS_PASSWORD": "test-password"}):
            app = PortalDashboardTests().load_app()
            with TestClient(app) as client, patch("app.routers.admin.get_dashboard_status") as metrics:
                response = client.get("/admin/status")
            metrics.assert_not_called()
            self.assertNotIn('id="backup-status-title"', response.text)

    def test_browser_upload_and_retry_behavior(self):
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js unavailable for browser helper regression")
        script = r'''
const assert = require('node:assert/strict');
const ux = require('./portal-web/app/static/js/portal-ux.js');
let reloads = 0;
global.location = {href: 'https://portal.example/files', origin: 'https://portal.example', reload: () => reloads++};
let lastXHR;
global.XMLHttpRequest = class {
 constructor() { this.upload = {}; lastXHR = this; }
 open(method, path) { assert.equal(method, 'POST'); assert.equal(path, '/files/uploads'); }
 setRequestHeader(key, value) { assert.equal(key, 'Accept'); assert.equal(value, 'application/json'); }
 send() {}
};
(async () => {
const progress = {value: 0, removeAttribute() {delete this.value;}};
const status = {textContent: ''}; const feedback = {hidden: true};
const button = {disabled: false}; const input = {disabled: false, value: 'chosen'};
const upload = ux.createUploader({progress, status, feedback, button, input});
const file = new Blob(['test']);
const pending = upload([file], 'folder');
const xhr = lastXHR;
assert.equal(button.disabled, true);
assert.equal(await upload([file], ''), false);
assert.equal(lastXHR, xhr);
xhr.upload.onprogress({lengthComputable: true, loaded: 50, total: 100});
assert.equal(progress.value, 50);
xhr.upload.onprogress({lengthComputable: true, loaded: 100, total: 100});
assert.match(status.textContent, /서버 저장 확인/); assert.equal(reloads, 0);
xhr.status = 401; xhr.responseText = '{}'; xhr.onload();
assert.equal(await pending, false); assert.match(status.textContent, /인증/);
assert.equal(button.disabled, false); assert.equal(input.value, '');
const success = upload([file], '');
lastXHR.status = 200; lastXHR.responseText = '{"uploaded":true}'; lastXHR.responseURL = location.href;
lastXHR.onload(); assert.equal(await success, false); assert.equal(reloads, 0);
const saved = upload([file], '');
lastXHR.status = 200; lastXHR.responseText = '{"uploaded":true}'; lastXHR.responseURL = 'https://portal.example/files/uploads';
lastXHR.onload(); assert.equal(await saved, true); assert.equal(reloads, 1);
const timeout = upload([file], ''); lastXHR.ontimeout(); assert.equal(await timeout, false);
assert.match(status.textContent, /저장 여부/); assert.equal(button.disabled, false);
assert.match(ux.uploadError(400, '파일은 10MB 이하만 업로드할 수 있습니다.'), /10MB/);
assert.match(ux.uploadError(409), /같은 이름/);
assert.doesNotMatch(ux.uploadError(400, '/secret/path token'), /secret/);
assert.doesNotMatch(ux.uploadError(500, '/secret/path token'), /secret/);
let listener, calls = 0, replaced = 0;
const retryButton = {disabled: false, closest: () => group};
const retryStatus = {textContent: ''};
const group = {dataset: {searchSource:'books', searchQuery:'original'}, querySelector: () => retryStatus, replaceWith: () => replaced++};
const otherGroup = {items:['keep']};
const replacement = {matches: () => true, dataset:{searchSource:'books'}};
const doc = {addEventListener: (_, fn) => listener = fn, createElement: () => ({content:{firstElementChild:replacement,childElementCount:1}})};
ux.bindSearchRetries(doc);
let complete;
global.fetch = url => { calls++; assert.match(url, /source=books&q=original/); return new Promise(resolve => complete=resolve); };
const event = {target:{closest:() => retryButton}};
const retry = listener(event); await listener(event); assert.equal(calls, 1);
complete({ok:true, redirected:false, headers:{get:()=> 'text/html'}, text:async()=>'<section></section>'});
await retry; assert.equal(replaced, 1); assert.deepEqual(otherGroup.items, ['keep']);
retryButton.disabled = false; global.fetch = async () => {throw Error('down');};
await listener(event); assert.equal(retryButton.disabled, false); assert.match(retryStatus.textContent, /다시 시도/);
assert.equal(replaced, 1);
})().catch(error => {console.error(error); process.exitCode=1;});
'''
        result = subprocess.run([node, "-e", script], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
