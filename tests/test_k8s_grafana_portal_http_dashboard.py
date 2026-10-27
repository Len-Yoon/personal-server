import json
import os
import shutil
import subprocess
import tempfile
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
            {"최근 5분 요청 수", "상태 코드별 요청 수", "5xx 오류 비율", "p95 응답 시간", "검색 p95 응답 시간"},
        )
        self.assertEqual(panels["최근 5분 요청 수"]["targets"][0]["expr"], "sum(increase(portal_http_requests_total[5m]))")
        self.assertEqual(panels["상태 코드별 요청 수"]["targets"][0]["expr"], "sum by (status_code) (increase(portal_http_requests_total[5m]))")
        self.assertEqual(panels["5xx 오류 비율"]["targets"][0]["expr"], "(sum(rate(portal_http_requests_total{status_code=~\"5..\"}[5m])) / clamp_min(sum(rate(portal_http_requests_total[5m])), 1)) or vector(0)")
        self.assertEqual(panels["p95 응답 시간"]["targets"][0]["expr"], "histogram_quantile(0.95, sum by (le) (rate(portal_http_request_duration_seconds_bucket[5m])))")
        self.assertEqual(panels["검색 p95 응답 시간"]["targets"][0]["expr"], "histogram_quantile(0.95, sum by (le) (rate(portal_http_request_duration_seconds_bucket{method=\"GET\",route=\"/search\"}[5m])))")


class PortalSearchObservationTests(unittest.TestCase):
    def load_search_artifacts(self):
        dashboard_manifest = yaml.safe_load(
            (ROOT / "infra/k8s/monitoring/portal-search-dashboard.yaml").read_text()
        )
        dashboard = json.loads(dashboard_manifest["data"]["portal-search-observability.json"])
        rules = yaml.safe_load(
            (ROOT / "infra/k8s/sre-telegram/portal-search-rule.yaml").read_text()
        )
        return dashboard_manifest, dashboard, rules

    def test_search_dashboard_is_separate_and_uses_only_fixed_metric_labels(self):
        manifest, dashboard, rules = self.load_search_artifacts()
        self.assertEqual(manifest["metadata"]["namespace"], "monitoring")
        self.assertEqual(manifest["metadata"]["labels"]["grafana_dashboard"], "1")
        self.assertNotEqual(dashboard["uid"], "portal-http-observability")
        self.assertEqual(len(dashboard["panels"]), 5)
        self.assertEqual(rules["metadata"]["labels"]["release"], "personal-server-monitoring")
        self.assertEqual(
            {r["alert"] for g in rules["spec"]["groups"] for r in g["rules"]},
            {"PortalSearchDegraded", "PortalSearchInstrumentationMissing"},
        )
        for panel in dashboard["panels"]:
            self.assertEqual(panel["datasource"], "Prometheus")
            self.assertTrue(panel["description"])
            for target in panel["targets"]:
                self.assertNotIn("vector(0)", target["expr"])
                self.assertNotIn("clamp_min", target["expr"])
                self.assertNotIn("query=", target["expr"])
                self.assertNotIn("url=", target["expr"])
        ratio_panel = dashboard["panels"][0]
        # Do not show a stale earlier successful ratio during the current idle interval.
        self.assertIs(ratio_panel["targets"][0]["instant"], True)
        self.assertEqual(ratio_panel["options"]["reduceOptions"]["calcs"], ["last"])
        self.assertEqual(ratio_panel["fieldConfig"]["defaults"]["noValue"], "데이터 없음")

    def test_search_alerts_and_actual_dashboard_queries_with_promtool(self):
        promtool = os.environ.get("PROMTOOL") or shutil.which("promtool")
        self.assertIsNotNone(promtool, "promtool is required; search observation checks must not skip")
        _, dashboard, manifest = self.load_search_artifacts()
        groups = manifest["spec"]["groups"]
        dashboard_rules = [
            {"record": f"portal_search_dashboard_test_{panel['id']}",
             "expr": panel["targets"][0]["expr"]}
            for panel in dashboard["panels"]
        ]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "portal-search.rules.yaml").write_text(yaml.safe_dump(
                {"groups": groups + [{"name": "dashboard-query-tests", "rules": dashboard_rules}]},
                allow_unicode=True,
            ))
            fixture = base / "portal-search-alerts.test.yaml"
            fixture.write_text((ROOT / "tests/fixtures/portal-search-alerts.test.yaml").read_text())
            checked = subprocess.run([promtool, "check", "rules", str(base / "portal-search.rules.yaml")],
                                     cwd=base, capture_output=True, text=True, check=False)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            result = subprocess.run([promtool, "test", "rules", str(fixture)],
                                    cwd=base, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
