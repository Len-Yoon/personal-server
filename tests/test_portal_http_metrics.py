import unittest
import importlib
import os

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
