import unittest
import importlib
import os
import threading
import tracemalloc

from fastapi.testclient import TestClient
from tests._test_support import prepare_service_import


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
