import json
import copy
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
            {"최근 5분 요청 수", "상태 코드별 요청 수", "5xx 오류 비율", "p95 응답 시간", "검색 p95 응답 시간", "데이터가 비어 있을 때"},
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
        self.assertEqual(len(dashboard["panels"]), 6)
        self.assertEqual(rules["metadata"]["labels"]["release"], "personal-server-monitoring")
        self.assertEqual(
            {r["alert"] for g in rules["spec"]["groups"] for r in g["rules"]},
            {"PortalSearchDegraded", "PortalSearchInstrumentationMissing"},
        )
        for panel in dashboard["panels"]:
            self.assertTrue(panel["description"])
            if panel["type"] == "text":
                self.assertIn("검색 표본", panel["options"]["content"])
                self.assertNotIn("targets", panel)
                continue
            self.assertEqual(panel["datasource"], "Prometheus")
            for target in panel.get("targets", []):
                self.assertNotIn("vector(0)", target["expr"])
                self.assertNotIn("clamp_min", target["expr"])
                self.assertNotIn("query=", target["expr"])
                self.assertNotIn("url=", target["expr"])
        ratio_panel = next(p for p in dashboard["panels"] if p["id"] == 1)
        # Do not show a stale earlier successful ratio during the current idle interval.
        self.assertIs(ratio_panel["targets"][0]["instant"], True)
        self.assertEqual(ratio_panel["options"]["reduceOptions"]["calcs"], ["last"])
        self.assertEqual(ratio_panel["fieldConfig"]["defaults"]["noValue"], "검색 표본 없음 · 계측 상태 확인")
        self.assertIs(ratio_panel["targets"][0]["range"], False)

    def test_search_alerts_and_actual_dashboard_queries_with_promtool(self):
        promtool = os.environ.get("PROMTOOL") or shutil.which("promtool")
        self.assertIsNotNone(promtool, "promtool is required; search observation checks must not skip")
        _, dashboard, manifest = self.load_search_artifacts()
        groups = manifest["spec"]["groups"]
        dashboard_rules = [
            {"record": f"portal_search_dashboard_test_{panel['id']}",
             "expr": panel["targets"][0]["expr"]}
            for panel in dashboard["panels"] if panel.get("targets")
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


class N100DashboardDataStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / "infra/k8s/tools/monitoring-dashboard-data-states.py"
        spec = importlib.util.spec_from_file_location("n100_dashboard_data_states", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def fixture(self):
        m = self.module
        targets = copy.deepcopy(m.TARGETS)
        cms = []
        special = set(m.CONTROL) | {m.KUBELET, m.MULTI, m.NETWORK, m.CORE_DNS, m.API_SERVER,
                                   "portal-http-observability", "portal-search-observability"}
        def panel(i, expr="up", title="CPU Usage"):
            return {"id": i, "type": "timeseries", "title": title,
                    "gridPos": {"x": 0, "y": i * 3, "w": 12, "h": 3},
                    "targets": [{"expr": expr}]}
        for name, target in targets.items():
            uid = target["dashboard_uid"]
            ps = [panel(1)]
            variables = []
            if uid not in special:
                variables = [{"name": n, "current": {"text": "stale", "value": "stale"}, "options": ["stale"]}
                             for n in ("namespace", "pod", "workload", "type")]
            elif uid == m.KUBELET:
                ps = [panel(6, "rate(kubelet_node_config_error[5m])"),
                      panel(13, "rate(storage_operation_errors_total[5m])")]
            elif uid == m.MULTI:
                ps = [panel(8), panel(10)]
                for p in ps:
                    p["type"] = "table"
                    p["targets"] = [{"expr": f"sum(sample_{i}) by (cluster)"} for i in range(5)]
            elif uid == m.NETWORK:
                ps = [panel(12, "rate(node_netstat_Tcp_RetransSegs[5m])"),
                      panel(13, "rate(node_netstat_TcpExt_TCPSynRetrans[5m])")]
            elif uid.startswith("portal-"):
                ps = [panel(1, "rate(search_sample[5m])", "검색 비율")]
                ps[0]["type"] = "stat"
            doc = {"uid": uid, "title": name, "panels": ps, "templating": {"list": variables}}
            raw = json.dumps(doc)
            target["source_sha256"] = m.sha(raw)
            target["after_sha256"] = m.document_sha(m.transform_dashboard(doc))
            metadata = {"name": name, "namespace": "monitoring", "uid": name + "-fixture",
                        "resourceVersion": "1", "labels": {"grafana_dashboard": "1"}}
            if target["helm_owned"]:
                metadata["annotations"] = {"meta.helm.sh/release-name": "personal-server-monitoring"}
            cms.append({"kind": "ConfigMap", "metadata": metadata,
                        "data": {target["key"]: raw, "unrelated": "keep-exactly"}})
        return targets, cms

    def test_plan_preserves_identity_metadata_other_keys_and_is_idempotent(self):
        m = self.module
        targets, cms = self.fixture()
        original = copy.deepcopy(cms)
        with patch.object(m, "TARGETS", targets):
            plan = m.build_plan(cms)
            self.assertEqual(len(plan), 19)
            self.assertEqual(cms, original)
            for entry in plan:
                cm = next(c for c in cms if c["metadata"]["name"] == entry["name"])
                self.assertEqual(json.loads(entry["before"])["uid"], json.loads(entry["after"])["uid"])
                cm["data"][entry["key"]] = entry["after"]
            self.assertEqual(m.build_plan(cms), [])
        self.assertEqual([c["metadata"] for c in cms], [c["metadata"] for c in original])
        self.assertTrue(all(c["data"]["unrelated"] == "keep-exactly" for c in cms))

    def test_already_rendered_but_externally_edited_content_is_not_a_noop(self):
        m = self.module
        targets, cms = self.fixture()
        with patch.object(m, "TARGETS", targets):
            entry = m.build_plan(cms)[0]
            for mutation in ("query", "additional_panel"):
                with self.subTest(mutation=mutation):
                    changed = copy.deepcopy(cms)
                    cm = next(c for c in changed if c["metadata"]["name"] == entry["name"])
                    doc = json.loads(entry["after"])
                    if mutation == "query":
                        doc["panels"][1]["targets"][0]["expr"] = "unreviewed_metric"
                    else:
                        extra = copy.deepcopy(doc["panels"][1]); extra["id"] = 8999
                        doc["panels"].append(extra)
                    cm["data"][entry["key"]] = json.dumps(doc)
                    with self.assertRaisesRegex(ValueError, "unreviewed_dashboard_source"):
                        m.build_plan(changed)

    def test_rejects_missing_duplicate_owner_uid_and_unreviewed_source(self):
        m = self.module
        targets, original = self.fixture()
        with patch.object(m, "TARGETS", targets):
            for mutation in ("missing", "duplicate", "owner", "uid", "source"):
                with self.subTest(mutation=mutation):
                    cms = copy.deepcopy(original)
                    if mutation == "missing":
                        cms.pop()
                    elif mutation == "duplicate":
                        cms.append(copy.deepcopy(cms[0]))
                    elif mutation == "owner":
                        cms[0]["metadata"]["annotations"] = {}
                    elif mutation == "uid":
                        key = targets[cms[0]["metadata"]["name"]]["key"]
                        doc = json.loads(cms[0]["data"][key]); doc["uid"] = "other"
                        cms[0]["data"][key] = json.dumps(doc)
                    else:
                        key = targets[cms[0]["metadata"]["name"]]["key"]
                        cms[0]["data"][key] += " "
                    with self.assertRaises(ValueError):
                        m.build_plan(cms)

    def test_defaults_reset_dependent_choices_without_restricting_queries(self):
        m = self.module
        targets, cms = self.fixture()
        with patch.object(m, "TARGETS", targets):
            plan = m.build_plan(cms)
            count = 0
            for e in plan:
                before, after = json.loads(e["before"]), json.loads(e["after"])
                vs = {v["name"]: v for v in after["templating"]["list"]}
                if "namespace" not in vs:
                    continue
                count += 1
                self.assertEqual(vs["namespace"]["current"]["value"], "personal-server")
                self.assertEqual(vs["pod"]["current"]["value"], "")
                self.assertEqual(vs["workload"]["current"]["value"], "")
                self.assertEqual(vs["type"]["current"]["value"], "deployment")
                self.assertEqual(before["panels"][0]["targets"], after["panels"][1]["targets"])
            self.assertEqual(count, 8)

    def test_kubelet_and_tcp_do_not_turn_absent_metrics_into_zero(self):
        m = self.module
        targets, cms = self.fixture()
        with patch.object(m, "TARGETS", targets):
            docs = {e["dashboard_uid"]: json.loads(e["after"]) for e in m.build_plan(cms)}
        self.assertEqual(m.by_id(docs[m.KUBELET], 6)["type"], "text")
        self.assertNotIn("targets", m.by_id(docs[m.KUBELET], 6))
        expr = m.by_id(docs[m.KUBELET], 13)["targets"][0]["expr"]
        self.assertIn('status=~"fail-.+"', expr)
        self.assertNotIn("storage_operation_errors_total", expr)
        self.assertNotIn("vector(0)", expr)
        for i in (12, 13):
            self.assertEqual(m.by_id(docs[m.NETWORK], i)["type"], "text")
            self.assertNotIn("targets", m.by_id(docs[m.NETWORK], i))

    def test_multi_table_has_unique_refs_display_key_and_real_empty_cluster_link(self):
        m = self.module
        targets, cms = self.fixture()
        with patch.object(m, "TARGETS", targets):
            doc = next(json.loads(e["after"]) for e in m.build_plan(cms) if e["dashboard_uid"] == m.MULTI)
        for i in (8, 10):
            panel = m.by_id(doc, i)
            self.assertEqual([t["refId"] for t in panel["targets"]], list("ABCDE"))
            self.assertTrue(all(t["instant"] and not t["range"] and t["format"] == "table" for t in panel["targets"]))
            self.assertTrue(all('"display_cluster"' in t["expr"] for t in panel["targets"]))
            self.assertEqual(panel["transformations"][0]["options"]["byField"], "display_cluster")
            links = [p["value"] for o in panel["fieldConfig"]["overrides"] for p in o["properties"] if p["id"] == "links"]
            self.assertTrue(links[0][0]["url"].endswith("var-cluster="))
            self.assertNotIn("__data.fields", links[0][0]["url"])

    def test_real_promql_display_label_and_storage_failure_semantics(self):
        m = self.module
        promtool = os.environ.get("PROMTOOL") or shutil.which("promtool")
        self.assertIsNotNone(promtool, "promtool is required")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            storage = m.STORAGE_ERROR_EXPR.replace('$cluster', '').replace('$instance', '.*').replace('$__rate_interval', '5m')
            rules = {"groups": [{"name": "dashboard-only", "rules": [
                {"record": "display_cluster_test", "expr": m.display_cluster("source_value")},
                {"record": "storage_failure_test", "expr": storage},
            ]}]}
            (base / "dashboard.rules.yaml").write_text(yaml.safe_dump(rules))
            (base / "test.yaml").write_text((ROOT / "tests/fixtures/grafana-data-states.test.yaml").read_text())
            r = subprocess.run([promtool, "test", "rules", str(base / "test.yaml")], cwd=base, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
