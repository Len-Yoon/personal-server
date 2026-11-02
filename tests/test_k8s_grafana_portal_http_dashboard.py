import json
import copy
import importlib.util
import os
import shutil
import subprocess
import sys
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


class HelmDashboardPostRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / "infra/k8s/tools/monitoring-dashboard-post-renderer.py"
        spec = importlib.util.spec_from_file_location("helm_dashboard_post_renderer", path)
        cls.post = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.post)
        cls.module = cls.post.renderer

    def fixture(self):
        targets, cms = N100DashboardDataStateTests.fixture(self)
        for cm in cms:
            cm["apiVersion"] = "v1"
            cm["metadata"]["labels"].update({
                "app.kubernetes.io/managed-by": "Helm",
                "app.kubernetes.io/instance": "personal-server-monitoring",
                "release": "personal-server-monitoring",
                "chart": "kube-prometheus-stack-88.6.1",
            })
        return targets, [cm for cm in cms if targets[cm["metadata"]["name"]]["helm_owned"]]

    def encoded(self, cms):
        import yaml
        return yaml.safe_dump_all(cms, allow_unicode=True, sort_keys=False).encode()

    def test_only_allowlisted_scalars_change_and_helm_subset_is_idempotent(self):
        import yaml
        targets, cms = self.fixture()
        for cm in cms:
            cm["metadata"].pop("annotations")
        secret = (b"# keep this sensitive document verbatim\napiVersion: v1\nkind: Secret\n"
                  b"metadata: {name: preserve-secret}\nstringData: {password: private-fixture-value}\n")
        raw = secret + b"---\n" + self.encoded(cms)
        with patch.object(self.module, "TARGETS", targets):
            result = self.post.render(raw)
            self.assertTrue(result.startswith(secret + b"---\n"))
            def without_reviewed_values(stream):
                source = stream.decode()
                spans = []
                for node in yaml.compose_all(source, Loader=yaml.SafeLoader):
                    fields = {k.value: v for k, v in node.value}
                    if fields["kind"].value != "ConfigMap":
                        continue
                    metadata = {k.value: v for k, v in fields["metadata"].value}
                    name = metadata["name"].value
                    if name in targets and targets[name]["helm_owned"]:
                        data = {k.value: v for k, v in fields["data"].value}
                        value = data[targets[name]["key"]]
                        spans.append((value.start_mark.index, value.end_mark.index))
                for start, end in reversed(spans):
                    source = source[:start] + "REVIEWED_VALUE" + source[end:]
                return source.encode()
            self.assertEqual(without_reviewed_values(raw), without_reviewed_values(result))
            output = list(yaml.safe_load_all(result))[1:]
            for before, after in zip(cms, output):
                key = targets[before["metadata"]["name"]]["key"]
                expected = self.module.transform_dashboard(json.loads(before["data"][key]))
                self.assertEqual(json.loads(after["data"][key]), expected)
                after["data"][key] = before["data"][key]
                self.assertEqual(after, before)
            self.assertEqual(self.post.render(result), result)
            # The live-CAS renderer retains its strict 19-target/owner contract.
            with self.assertRaisesRegex(ValueError, "target_configmap_missing"):
                self.module.build_plan(cms)

    def test_block_scalar_replacement_preserves_next_key_and_document_bytes(self):
        targets, cms = self.fixture()
        import yaml
        first = cms[0]
        key = targets[first["metadata"]["name"]]["key"]
        original_value = first["data"][key]
        # Use an actual literal block as Helm does, with a following sibling key.
        single = yaml.safe_dump({"apiVersion": "v1", **first}, allow_unicode=True, sort_keys=False)
        start = single.index("\ndata:\n") + 1
        single = single[:start] + "data:\n  " + key + ": |-\n    " + original_value + "\n  unrelated: keep-exactly\n"
        raw = single + "---\n" + self.encoded(cms[1:]).decode()
        with patch.object(self.module, "TARGETS", targets):
            result = self.post.render(raw.encode())
        docs = list(yaml.safe_load_all(result))
        self.assertEqual(docs[0]["data"]["unrelated"], "keep-exactly")
        self.assertIn(b"\n  unrelated: keep-exactly\n---\n", result)

    def test_unicode_yaml_line_separators_survive_as_dashboard_data(self):
        targets, cms = self.fixture()
        name = cms[0]["metadata"]["name"]
        key = targets[name]["key"]
        original = json.loads(cms[0]["data"][key])
        original["title"] = "한글\u0085NEL\u2028LS\u2029PS"
        cms[0]["data"][key] = json.dumps(original)
        targets[name]["source_sha256"] = self.module.sha(cms[0]["data"][key])
        targets[name]["after_sha256"] = self.module.document_sha(self.module.transform_dashboard(original))
        with patch.object(self.module, "TARGETS", targets):
            output = list(yaml.safe_load_all(self.post.render(self.encoded(cms))))
        self.assertEqual(json.loads(output[0]["data"][key])["title"], original["title"])

    def test_upstream_drift_missing_duplicate_owner_and_extra_uid_fail_closed(self):
        targets, original = self.fixture()
        for mutation in ("missing", "duplicate", "source", "owner", "uid", "release", "chart"):
            with self.subTest(mutation=mutation):
                cms = copy.deepcopy(original)
                if mutation == "missing":
                    cms.pop()
                elif mutation == "duplicate":
                    cms.append(copy.deepcopy(cms[0]))
                elif mutation == "owner":
                    cms[0]["metadata"]["annotations"]["meta.helm.sh/release-name"] = "other"
                elif mutation in ("release", "chart"):
                    cms[0]["metadata"]["labels"][mutation] = "unreviewed"
                elif mutation == "uid":
                    cms.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "extra"},
                                "data": {"extra.json": next(iter(cms[0]["data"].values()))}})
                else:
                    key = targets[cms[0]["metadata"]["name"]]["key"]
                    cms[0]["data"][key] += " "
                with patch.object(self.module, "TARGETS", targets), self.assertRaises(ValueError):
                    self.post.render(self.encoded(cms))

    def test_cli_rejects_unsafe_malformed_and_oversize_input_without_output_or_leaks(self):
        script = ROOT / "infra/k8s/tools/monitoring-dashboard-post-renderer.py"
        for raw in (b"private-fixture-value: [", b"!!python/object:private-fixture-value {}",
                    b"key: private-fixture-value\nkey: duplicated\n", b"x: &secret private-fixture-value\ny: *secret\n",
                    b"\xffprivate-fixture-value", b"", b"x" * (self.post.MAX_INPUT_BYTES + 1)):
            with self.subTest(length=len(raw)):
                result = subprocess.run([sys.executable, str(script)], input=raw, capture_output=True, check=False)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(result.stderr, self.post.ERROR.encode())

    def test_late_policy_failure_does_not_emit_prior_secret_or_partial_manifests(self):
        import io
        from types import SimpleNamespace
        targets, cms = self.fixture()
        key = targets[cms[-1]["metadata"]["name"]]["key"]
        cms[-1]["data"][key] += " private-fixture-value"
        secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "secret"},
                  "stringData": {"password": "private-fixture-value"}}
        incoming = io.BytesIO(self.encoded([secret, *cms]))
        outgoing, errors = io.BytesIO(), io.StringIO()
        with patch.object(self.module, "TARGETS", targets), \
                patch.object(self.post.sys, "argv", ["renderer"]), \
                patch.object(self.post.sys, "stdin", SimpleNamespace(buffer=incoming)), \
                patch.object(self.post.sys, "stdout", SimpleNamespace(buffer=outgoing)), \
                patch.object(self.post.sys, "stderr", errors):
            self.assertEqual(self.post.main(), 2)
        self.assertEqual(outgoing.getvalue(), b"")
        self.assertEqual(errors.getvalue(), self.post.ERROR)

    def test_yaml_duplicate_keys_and_resource_limits_are_enforced(self):
        targets, cms = self.fixture()
        duplicate = (b"apiVersion: v1\nkind: Secret\nmetadata: {name: secret}\n"
                     b"stringData: {password: private-fixture-value, password: other}\n---\n")
        with patch.object(self.module, "TARGETS", targets):
            with self.assertRaisesRegex(ValueError, "duplicate_mapping_key"):
                self.post.render(duplicate + self.encoded(cms))
            for limit in ("MAX_NODES", "MAX_DOCUMENTS", "MAX_DEPTH", "MAX_DASHBOARD_BYTES", "MAX_OUTPUT_BYTES"):
                with self.subTest(limit=limit), patch.object(self.post, limit, 1), self.assertRaises(ValueError):
                    self.post.render(self.encoded(cms))


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
