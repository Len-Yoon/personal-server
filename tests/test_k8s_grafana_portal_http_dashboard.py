import json
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "infra/k8s/monitoring/portal-http-dashboard.yaml"


class PortalHttpGrafanaDashboardTests(unittest.TestCase):
    def test_dashboard_exposes_portal_http_operational_panels(self):
        self.assertTrue(DASHBOARD.is_file())

        manifest = yaml.safe_load(DASHBOARD.read_text(encoding="utf-8"))
        self.assertEqual(manifest["apiVersion"], "v1")
        self.assertEqual(manifest["kind"], "ConfigMap")
        self.assertEqual(manifest["metadata"]["namespace"], "monitoring")
        self.assertEqual(manifest["metadata"]["labels"]["grafana_dashboard"], "1")

        dashboard = json.loads(manifest["data"]["portal-http-observability.json"])
        panels = {panel["title"]: panel for panel in dashboard["panels"]}
        self.assertEqual(
            set(panels),
            {"최근 5분 요청 수", "상태 코드별 요청 수", "5xx 오류 비율", "p95 응답 시간"},
        )
        self.assertEqual(panels["최근 5분 요청 수"]["targets"][0]["expr"], "sum(increase(portal_http_requests_total[5m]))")
        self.assertEqual(panels["상태 코드별 요청 수"]["targets"][0]["expr"], "sum by (status_code) (increase(portal_http_requests_total[5m]))")
        self.assertEqual(panels["5xx 오류 비율"]["targets"][0]["expr"], "(sum(rate(portal_http_requests_total{status_code=~\"5..\"}[5m])) / clamp_min(sum(rate(portal_http_requests_total[5m])), 1)) or vector(0)")
        self.assertEqual(panels["p95 응답 시간"]["targets"][0]["expr"], "histogram_quantile(0.95, sum by (le) (rate(portal_http_request_duration_seconds_bucket[5m])))")


if __name__ == "__main__":
    unittest.main()
