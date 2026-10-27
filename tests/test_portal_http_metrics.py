import asyncio
import unittest
import importlib
import os
import threading
import tracemalloc
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from tests._test_support import prepare_service_import


class PortalSearchMetricsTests(unittest.TestCase):
    def setUp(self):
        prepare_service_import("portal-web")
        from app.services import global_search
        from app.services.global_search import SearchMetrics

        self.search = global_search
        self.metrics = SearchMetrics()
        self.addCleanup(patch.stopall)
        patch.object(global_search, "SEARCH_METRICS", self.metrics).start()
        patch.dict(os.environ, {"DEMO_MODE": ""}).start()

    def execute(self, handler, **kwargs):
        client_type = httpx.AsyncClient
        with patch("httpx.AsyncClient", side_effect=lambda **options: client_type(
            transport=httpx.MockTransport(handler), **options
        )):
            return asyncio.run(self.search.search_all("private-query", **kwargs))

    def assert_source(self, source, outcome, count=1):
        self.assertIn(
            f'portal_search_source_requests_total{{outcome="{outcome}",source="{source}"}} {count}',
            self.metrics.render(),
        )

    def test_successful_empty_scoped_and_fallback_searches_count_once(self):
        async def handle(request):
            return httpx.Response(200, json={"results": []})

        self.execute(handle, source="books")
        self.execute(handle, source="untrusted-source")
        self.assert_source("books", "ok", 2)
        self.assert_source("news", "ok")
        self.assert_source("youtube", "ok")
        self.assertIn('portal_search_requests_total{outcome="complete"} 2', self.metrics.render())
        self.assertNotIn("untrusted-source", self.metrics.render())

    def test_error_categories_are_bounded_and_do_not_leak_inputs(self):
        failures = [
            (lambda request: httpx.Response(503, text="secret-body"), "http_error"),
            (lambda request: httpx.Response(200, text="secret-invalid-json"), "invalid_response"),
            (lambda request: httpx.Response(200, json={"results": [None]}), "invalid_response"),
            (httpx.ReadTimeout("secret-timeout"), "timeout"),
            (httpx.ConnectError("secret-url"), "transport_error"),
            (RuntimeError("secret-internal"), "internal_error"),
        ]
        for failure, expected in failures:
            with self.subTest(expected=expected, failure=type(failure).__name__):
                from app.services.global_search import SearchMetrics
                self.metrics = SearchMetrics()
                with patch.object(self.search, "SEARCH_METRICS", self.metrics):
                    async def handle(request):
                        if isinstance(failure, Exception):
                            raise failure
                        return failure(request)

                    result = self.execute(handle, source="news")
                self.assertEqual(result, {"news": {"items": [], "status": "unavailable"}})
                self.assert_source("news", expected)
                self.assertIn('portal_search_requests_total{outcome="failed"} 1', self.metrics.render())
                self.assertNotIn("secret", self.metrics.render())
                self.assertNotIn("private-query", self.metrics.render())

    def test_deadline_freezes_timeout_even_if_cancelled_task_returns_success(self):
        drained = []

        async def handle(request):
            if request.url.host != "book-memo":
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    drained.append(request.url.host)
            return httpx.Response(200, json={"results": []})

        with patch.object(self.search, "SEARCH_BUDGET_SECONDS", 0.02):
            result = self.execute(handle)
        self.assertEqual(set(drained), {"crawler-worker", "youtube-memo"})
        self.assertEqual(result["books"]["status"], "ok")
        self.assertEqual(result["news"]["status"], "unavailable")
        self.assert_source("news", "timeout")
        self.assert_source("youtube", "timeout")
        self.assert_source("books", "ok")
        self.assertIn('portal_search_requests_total{outcome="partial"} 1', self.metrics.render())

    def test_request_cancellation_propagates_and_drains_without_double_counting(self):
        active = set()

        async def handle(request):
            active.add(request.url.host)
            try:
                await asyncio.sleep(10)
            finally:
                active.remove(request.url.host)

        async def cancel_search():
            task = asyncio.create_task(self.search.search_all("private-query"))
            while len(active) < 3:
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        client_type = httpx.AsyncClient
        with patch("httpx.AsyncClient", side_effect=lambda **options: client_type(
            transport=httpx.MockTransport(handle), **options
        )):
            asyncio.run(cancel_search())
        self.assertFalse(active)
        for source in ("news", "youtube", "books"):
            self.assert_source(source, "cancelled")
        self.assertIn('portal_search_requests_total{outcome="cancelled"} 1', self.metrics.render())
        self.assertIn('portal_search_requests_total{outcome="failed"} 0', self.metrics.render())

    def test_blank_and_demo_searches_do_not_count(self):
        before = self.metrics.render()
        asyncio.run(self.search.search_all("  "))
        with patch.dict(os.environ, {"DEMO_MODE": "1"}):
            asyncio.run(self.search.search_all("sample"))
        self.assertEqual(before, self.metrics.render())

    def test_initial_scrape_exposes_all_25_bounded_zero_series_without_searches(self):
        samples = [line for line in self.metrics.render().splitlines() if not line.startswith("#")]
        self.assertEqual(len(samples), 25)
        self.assertTrue(all(line.endswith(" 0") for line in samples))
        self.assertEqual(sum(line.startswith("portal_search_requests_total{") for line in samples), 4)
        self.assertEqual(sum(line.startswith("portal_search_source_requests_total{") for line in samples), 21)

    def test_cancelled_search_preserves_already_finished_source_observations(self):
        completed = asyncio.Event()

        async def fetch(client, name, *args):
            if name == "books":
                completed.set()
                return {"items": [], "status": "ok"}, "ok"
            await asyncio.sleep(10)

        async def cancel_search():
            task = asyncio.create_task(self.search.search_all("private-query"))
            await completed.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with patch.object(self.search, "_fetch_results", side_effect=fetch):
            asyncio.run(cancel_search())
        self.assert_source("books", "ok")
        self.assert_source("news", "cancelled")
        self.assert_source("youtube", "cancelled")
        self.assertIn('portal_search_requests_total{outcome="cancelled"} 1', self.metrics.render())
        self.assertIn('portal_search_requests_total{outcome="partial"} 0', self.metrics.render())

    def test_client_setup_failure_counts_internal_errors_and_returns_degraded_results(self):
        with patch("httpx.AsyncClient", side_effect=RuntimeError("private-client-error")):
            result = asyncio.run(self.search.search_all("private-query"))
        self.assertTrue(all(value == {"items": [], "status": "unavailable"} for value in result.values()))
        for source in ("news", "youtube", "books"):
            self.assert_source(source, "internal_error")
        self.assertIn('portal_search_requests_total{outcome="failed"} 1', self.metrics.render())
        self.assertNotIn("private", self.metrics.render())

    def test_partial_http_200_is_visible_in_authenticated_metrics_endpoint(self):
        import app.main as main

        app = importlib.reload(main).app
        async def handle(request):
            if request.url.host == "crawler-worker":
                return httpx.Response(503)
            return httpx.Response(200, json={"results": []})

        client_type = httpx.AsyncClient
        with patch.object(main, "SEARCH_METRICS", self.metrics), patch.dict(
            os.environ, {"PORTAL_METRICS_BEARER_TOKEN": "test-token"}
        ), patch("httpx.AsyncClient", side_effect=lambda **options: client_type(
            transport=httpx.MockTransport(handle), **options
        )), TestClient(app) as client:
            self.assertEqual(client.get("/?q=private-query").status_code, 200)
            self.assertEqual(client.get("/internal/metrics").status_code, 404)
            rendered = client.get("/internal/metrics", headers={"Authorization": "Bearer test-token"}).text
        self.assertIn('portal_search_requests_total{outcome="partial"} 1', rendered)
        self.assertIn('portal_http_requests_total{method="GET",route="/search",status_code="200"} 1', rendered)
        self.assertNotIn("private-query", rendered)

    def test_counter_updates_are_atomic_and_reject_dynamic_labels(self):
        def record_many():
            for _ in range(200):
                self.metrics.record({"news": "ok", "books": "http_error"})

        threads = [threading.Thread(target=record_many) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assert_source("news", "ok", 800)
        self.assert_source("books", "http_error", 800)
        self.assertIn('portal_search_requests_total{outcome="partial"} 800', self.metrics.render())
        before = self.metrics.render()
        for outcomes in ({"secret-source": "ok"}, {"news": "secret-error"}, {}):
            with self.assertRaises(ValueError):
                self.metrics.record(outcomes)
        self.assertEqual(before, self.metrics.render())


class PortalHttpMetricsTests(unittest.TestCase):
    def setUp(self):
        self._token = os.environ.get("PORTAL_METRICS_BEARER_TOKEN")

    def tearDown(self):
        if self._token is None:
            os.environ.pop("PORTAL_METRICS_BEARER_TOKEN", None)
        else:
            os.environ["PORTAL_METRICS_BEARER_TOKEN"] = self._token

    def test_records_request_count_and_latency_histogram(self):
        prepare_service_import("portal-web")
        from app.services.http_metrics import HttpMetrics

        metrics = HttpMetrics()
        metrics.record(method="GET", route="/health", status_code=200, duration_seconds=0.04)

        rendered = metrics.render()

        self.assertIn(
            'portal_http_requests_total{method="GET",route="/health",status_code="200"} 1',
            rendered,
        )
        self.assertIn(
            'portal_http_request_duration_seconds_bucket{le="0.05",method="GET",route="/health"} 1',
            rendered,
        )
        self.assertIn(
            'portal_http_request_duration_seconds_count{method="GET",route="/health"} 1',
            rendered,
        )

    def test_duration_memory_stays_bounded_for_a_fixed_label(self):
        prepare_service_import("portal-web")
        from app.services.http_metrics import HttpMetrics

        metrics = HttpMetrics()
        tracemalloc.start()
        try:
            for _ in range(1_000):
                metrics.record(
                    method="GET",
                    route="/memory",
                    status_code=200,
                    duration_seconds=0.04,
                )
            warmed_current, _ = tracemalloc.get_traced_memory()

            for _ in range(100_000):
                metrics.record(
                    method="GET",
                    route="/memory",
                    status_code=200,
                    duration_seconds=0.04,
                )
            current, _ = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertLess(current - warmed_current, 250_000)

    def test_duration_histogram_counts_exact_bucket_boundaries(self):
        prepare_service_import("portal-web")
        from app.services.http_metrics import HttpMetrics

        metrics = HttpMetrics()
        boundaries = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
        for boundary in boundaries:
            route = f"/boundary/{boundary}"
            metrics.record(
                method="GET",
                route=route,
                status_code=200,
                duration_seconds=boundary - 1e-9,
            )
            metrics.record(
                method="GET",
                route=route,
                status_code=200,
                duration_seconds=boundary + 1e-9,
            )
            metrics.record(
                method="GET",
                route=route,
                status_code=200,
                duration_seconds=boundary,
            )

        rendered = metrics.render()
        for boundary in boundaries:
            route = f"/boundary/{boundary}"
            label = f'le="{boundary:g}",method="GET",route="{route}"'
            self.assertIn(
                f"portal_http_request_duration_seconds_bucket{{{label}}} 2",
                rendered,
            )

    def test_duration_negative_values_are_clamped_and_labels_remain_distinct(self):
        prepare_service_import("portal-web")
        from app.services.http_metrics import HttpMetrics

        metrics = HttpMetrics()
        metrics.record(method="GET", route="/one", status_code=200, duration_seconds=-2.0)
        metrics.record(method="POST", route="/one", status_code=201, duration_seconds=0.2)
        metrics.record(method="GET", route="/two", status_code=500, duration_seconds=20.0)

        rendered = metrics.render()
        self.assertIn(
            'portal_http_request_duration_seconds_sum{method="GET",route="/one"} 0',
            rendered,
        )
        self.assertIn(
            'portal_http_request_duration_seconds_sum{method="POST",route="/one"} 0.2',
            rendered,
        )
        self.assertIn(
            'portal_http_request_duration_seconds_bucket{le="+Inf",method="GET",route="/two"} 1',
            rendered,
        )
        self.assertIn(
            'portal_http_requests_total{method="POST",route="/one",status_code="201"} 1',
            rendered,
        )

    def test_concurrent_record_and_render_keep_histogram_totals_consistent(self):
        prepare_service_import("portal-web")
        from app.services.http_metrics import HttpMetrics

        metrics = HttpMetrics()
        errors = []
        barrier = threading.Barrier(5)

        def record_many():
            try:
                barrier.wait()
                for _ in range(2_000):
                    metrics.record(
                        method="GET",
                        route="/concurrent",
                        status_code=200,
                        duration_seconds=0.04,
                    )
            except BaseException as exc:  # pragma: no cover - failure relay
                errors.append(exc)

        workers = [threading.Thread(target=record_many) for _ in range(4)]
        for worker in workers:
            worker.start()
        barrier.wait()
        snapshots = []
        while any(worker.is_alive() for worker in workers):
            rendered = metrics.render()
            if 'portal_http_requests_total{method="GET"' in rendered:
                snapshots.append(rendered)
        for worker in workers:
            worker.join()

        self.assertEqual(errors, [])
        snapshots.append(metrics.render())

        def value(rendered, metric, labels):
            prefix = f"{metric}{{{labels}}} "
            line = next(line for line in rendered.splitlines() if line.startswith(prefix))
            return float(line[len(prefix) :])

        bucket_labels = [
            f'le="{boundary}",method="GET",route="/concurrent"'
            for boundary in ("0.05", "0.1", "0.25", "0.5", "1", "2.5", "5", "10")
        ]
        previous_buckets = [0.0] * len(bucket_labels)
        for rendered in snapshots:
            request_count = value(
                rendered,
                "portal_http_requests_total",
                'method="GET",route="/concurrent",status_code="200"',
            )
            inf_count = value(
                rendered,
                "portal_http_request_duration_seconds_bucket",
                'le="+Inf",method="GET",route="/concurrent"',
            )
            duration_count = value(
                rendered,
                "portal_http_request_duration_seconds_count",
                'method="GET",route="/concurrent"',
            )
            duration_sum = value(
                rendered,
                "portal_http_request_duration_seconds_sum",
                'method="GET",route="/concurrent"',
            )
            self.assertEqual(request_count, inf_count)
            self.assertEqual(inf_count, duration_count)
            self.assertAlmostEqual(duration_sum, duration_count * 0.04, places=6)
            buckets = [value(rendered, "portal_http_request_duration_seconds_bucket", labels) for labels in bucket_labels]
            self.assertTrue(all(bucket <= duration_count for bucket in buckets))
            self.assertTrue(all(left <= right for left, right in zip(buckets, buckets[1:])))
            self.assertTrue(all(previous <= current for previous, current in zip(previous_buckets, buckets)))
            previous_buckets = buckets

        self.assertEqual(inf_count, 8000)

    def test_internal_metrics_hides_response_without_valid_bearer_token(self):
        prepare_service_import("portal-web")
        os.environ["PORTAL_METRICS_BEARER_TOKEN"] = "test-token"
        import app.main as main

        app = importlib.reload(main).app
        with TestClient(app) as client:
            denied = client.get("/internal/metrics")
            allowed = client.get("/internal/metrics", headers={"Authorization": "Bearer test-token"})

        self.assertEqual(denied.status_code, 404)
        self.assertEqual(allowed.status_code, 200)
        self.assertIn("portal_http_requests_total", allowed.text)

    def test_records_normalized_route_after_a_completed_request(self):
        prepare_service_import("portal-web")
        os.environ["PORTAL_METRICS_BEARER_TOKEN"] = "test-token"
        import app.main as main

        app = importlib.reload(main).app
        with TestClient(app) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            rendered = client.get(
                "/internal/metrics",
                headers={"Authorization": "Bearer test-token"},
            ).text

        self.assertIn(
            'portal_http_requests_total{method="GET",route="/health",status_code="200"} 1',
            rendered,
        )

    def test_search_has_distinct_bounded_metric_label_without_query_text(self):
        prepare_service_import("portal-web")
        os.environ["PORTAL_METRICS_BEARER_TOKEN"] = "test-token"
        import app.main as main

        app = importlib.reload(main).app
        with patch("app.routers.dashboard.search_all", new_callable=AsyncMock, return_value={}), TestClient(app) as client:
            self.assertEqual(client.get("/?q=private-term").status_code, 200)
            self.assertEqual(client.get("/").status_code, 200)
            rendered = client.get(
                "/internal/metrics",
                headers={"Authorization": "Bearer test-token"},
            ).text

        self.assertIn('portal_http_requests_total{method="GET",route="/search",status_code="200"} 1', rendered)
        self.assertIn('portal_http_request_duration_seconds_count{method="GET",route="/search"} 1', rendered)
        self.assertIn('portal_http_requests_total{method="GET",route="/",status_code="200"} 1', rendered)
        self.assertNotIn("private-term", rendered)

    def test_records_server_error_when_route_raises(self):
        prepare_service_import("portal-web")
        os.environ["PORTAL_METRICS_BEARER_TOKEN"] = "test-token"
        import app.main as main

        app = importlib.reload(main).app

        @app.get("/metrics-test-failure")
        def metrics_test_failure():
            raise RuntimeError("expected test failure")

        with TestClient(app, raise_server_exceptions=False) as client:
            self.assertEqual(client.get("/metrics-test-failure").status_code, 500)
            rendered = client.get(
                "/internal/metrics",
                headers={"Authorization": "Bearer test-token"},
            ).text

        self.assertIn(
            'portal_http_requests_total{method="GET",route="/metrics-test-failure",status_code="500"} 1',
            rendered,
        )
