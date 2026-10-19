import os
import importlib.util
import json
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "infra/k8s/tools"
SCRIPT = ROOT / "infra/k8s/tools/monitoring-preflight.sh"
DASHBOARD_MANIFEST = ROOT / "infra/k8s/monitoring/portal-http-dashboard.yaml"


class MonitoringDiskAuditTests(unittest.TestCase):
    def load_audit(self):
        spec = importlib.util.spec_from_file_location("monitoring_disk_audit", TOOLS / "monitoring-disk-audit.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def fixture(self, *, missing_pvc=False, low=False, pressure=False, drift=False,
                missing_label=False, missing_node=False, malformed=False, host=False,
                host_stale=False, host_duplicate=False, host_bad_capacity=False, target_down=False):
        module = self.load_audit()
        calls = []
        def fetch(arguments):
            calls.append(arguments)
            if arguments == ["get", "nodes", "-o", "json"]:
                return {"items": [{"metadata": {"name": "fixture-node"}}]}
            if arguments[:1] == ["-n"]:
                return {"items": [{"metadata": {"name": "fixture-claim"}, "status": {"phase": "Bound"}}]}
            self.assertEqual(arguments[:2], ["get", "--raw"])
            parsed = urlsplit(arguments[2])
            if parsed.path.endswith("/targets"):
                targets = [{"scrapePool": "serviceMonitor/monitoring/windows-host-disk/0",
                            "health": "down" if target_down else "up",
                            "labels": {"service": "compose-system-agent", "namespace": "personal-server"}}] if host else []
                return {"status": "success", "data": {"activeTargets": targets}}
            if parsed.path.endswith("/rules"):
                rules = [{"type": "alerting", "name": name, "query": expression,
                          "duration": seconds, "health": "ok",
                          "labels": {"sre_telegram": "true", "severity": "warning"}}
                         for name, expression, seconds in (("NodeDiskPressure", module.NODE_EXPRESSION, 300),
                                                           ("PVCSpaceLow", module.PVC_EXPRESSION, 600))]
                if host:
                    rules += [{"type": "alerting", "name": name, "query": expression,
                               "duration": duration, "health": "ok",
                               "labels": {"sre_telegram": "true", "severity": "warning"}}
                              for name, expression, duration in (
                                  ("HostDiskSpaceLow", module.HOST_LOW_EXPRESSION, 600),
                                  ("HostDiskMetricsStale", module.HOST_STALE_EXPRESSION, 300))]
                if drift:
                    rules[1]["query"] = "vector(0)"
                if missing_label:
                    rules[1]["labels"].pop("sre_telegram")
                return {"status": "success", "data": {"groups": [{"rules": rules}]}}
            expression = parse_qs(parsed.query)["query"][0]
            result = []
            if expression.startswith("kubelet_volume_stats_"):
                for namespace in module.NAMESPACES:
                    if missing_pvc and namespace == "personal-server":
                        continue
                    value = 100 if "capacity_bytes" in expression else (14 if low else 80)
                    result.append({"metric": {"namespace": namespace, "persistentvolumeclaim": "fixture-claim"},
                                   "value": [0, "NaN" if malformed else str(value)]})
            if host and expression.startswith(module.HOST_PREFIX):
                field = expression[len(module.HOST_PREFIX):].split('{')[0]
                values = {"collection_ok": 1, "sample_timestamp_seconds": datetime.now(timezone.utc).timestamp() - (2101 if host_stale else 0),
                          "capacity_bytes": 0 if host_bad_capacity else 100, "available_bytes": 80}
                sample = {"metric": {"host": "windows", "volume": "C:"}, "value": [0, str(values[field])]}
                result = [sample] * (2 if host_duplicate else 1)
            if expression.startswith("kube_node_status_condition") and not missing_node:
                result = [{"metric": {"node": "fixture-node"}, "value": [0, "1" if pressure else "0"]}]
            return {"status": "success", "data": {"resultType": "vector", "result": result}}
        with patch.object(module, "read_json", side_effect=fetch):
            report = module.audit()
        return report, calls

    def test_audit_distinguishes_observed_pvc_node_coverage_from_host_gap_and_delivery(self):
        report, calls = self.fixture()
        self.assertEqual(report["pvc"], {"bound": 2, "covered": 2, "low_space": 0, "metrics": "PASS", "rule": "PASS"})
        self.assertEqual(report["node"]["metrics"], "PASS")
        self.assertEqual(report["node"]["rule"], "PASS")
        self.assertEqual(report["host_filesystem"]["coverage"], "GAP")
        self.assertEqual(report["notification_delivery"], "NOT_TESTED")
        self.assertEqual(report["monitoring_disk_audit"], "GAP")
        self.assertTrue(all(arguments[:1] == ["get"] or arguments[2:3] == ["get"] for arguments in calls))

    def test_complete_host_coverage_requires_fresh_unique_valid_series_and_dedicated_scrape(self):
        report, _ = self.fixture(host=True)
        self.assertEqual(report["host_filesystem"]["coverage"], "PASS")
        self.assertEqual(report["monitoring_disk_audit"], "PASS")
        self.assertEqual(report["notification_delivery"], "NOT_TESTED")
        for options in ({"host_stale": True}, {"host_duplicate": True},
                        {"host_bad_capacity": True}, {"target_down": True}):
            with self.subTest(options=options):
                report, _ = self.fixture(host=True, **options)
                self.assertEqual(report["host_filesystem"]["coverage"], "GAP")
                self.assertEqual(report["monitoring_disk_audit"], "GAP")
        report, _ = self.fixture(host=True, low=True)
        self.assertEqual(report["monitoring_disk_audit"], "WARNING")

    def test_host_rules_match_manifest_and_bridge_filter_keeps_only_agent(self):
        import yaml
        module = self.load_audit()
        monitor, manifest = list(yaml.safe_load_all((ROOT / "infra/k8s/sre-telegram/host-disk-observability.yaml").read_text()))
        rules = {r["alert"]: r for r in manifest["spec"]["groups"][0]["rules"]}
        for name, expression in (("HostDiskSpaceLow", module.HOST_LOW_EXPRESSION), ("HostDiskMetricsStale", module.HOST_STALE_EXPRESSION)):
            self.assertEqual(''.join(rules[name]["expr"].split()), ''.join(expression.split()))
            self.assertEqual(rules[name]["labels"]["sre_telegram"], "true")
        self.assertEqual(monitor["spec"]["endpoints"][0]["relabelings"], [
            {"sourceLabels": ["__meta_kubernetes_service_name"], "regex": "compose-system-agent", "action": "keep"}])

    def test_missing_pvc_or_node_metrics_remain_gaps(self):
        report, _ = self.fixture(missing_pvc=True)
        self.assertEqual(report["pvc"]["covered"], 1)
        self.assertEqual(report["pvc"]["metrics"], "GAP")
        report, _ = self.fixture(missing_node=True)
        self.assertEqual(report["node"]["metrics"], "GAP")

    def test_loaded_rule_drift_or_missing_telegram_label_is_a_gap(self):
        for options in ({"drift": True}, {"missing_label": True}):
            report, _ = self.fixture(**options)
            self.assertEqual(report["pvc"]["rule"], "GAP")

    def test_audit_reports_observed_low_space_and_pressure_without_firing_alerts(self):
        report, _ = self.fixture(low=True, pressure=True)
        self.assertEqual(report["pvc"]["low_space"], 2)
        self.assertEqual(report["node"]["disk_pressure"], 1)

    def test_invalid_numeric_metric_evidence_is_rejected(self):
        with self.assertRaises(ValueError):
            self.fixture(malformed=True)

    def test_rule_expressions_match_manifest_and_existing_telegram_route(self):
        import yaml
        module = self.load_audit()
        manifest = yaml.safe_load((ROOT / "infra/k8s/sre-telegram/prometheus-rule.yaml").read_text())
        rules = {rule["alert"]: rule for group in manifest["spec"]["groups"] for rule in group["rules"]}
        for name, expression in (("NodeDiskPressure", module.NODE_EXPRESSION), ("PVCSpaceLow", module.PVC_EXPRESSION)):
            self.assertEqual("".join(rules[name]["expr"].split()), "".join(expression.split()))
            self.assertEqual(rules[name]["labels"]["sre_telegram"], "true")

    def test_cli_failure_and_gaps_are_nonzero_without_private_output(self):
        module = self.load_audit()
        from contextlib import redirect_stdout
        import io
        for error in (ValueError("private-address"), AttributeError("private-evidence"),
                      IndexError("private-sample"), subprocess.CalledProcessError(1, ["private-path"])):
            output = io.StringIO()
            with patch.object(module, "audit", side_effect=error), patch.object(module.sys, "argv", ["audit"]), redirect_stdout(output):
                self.assertEqual(module.main(), 2)
            self.assertNotIn("private", output.getvalue())
            self.assertEqual(json.loads(output.getvalue())["monitoring_disk_audit"], "FAIL")
        output = io.StringIO()
        with patch.object(module, "audit", return_value={"monitoring_disk_audit": "GAP"}), patch.object(module.sys, "argv", ["audit"]), redirect_stdout(output):
            self.assertEqual(module.main(), 1)


class MonitoringPreflightBehaviorTest(unittest.TestCase):
    def run_preflight(
        self,
        *,
        kubectl_nodes="n100 Ready",
        storageclass=True,
        helm3=True,
        helm_available=True,
        disk_available=9 * 1024 * 1024,
    ):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            calls = directory_path / "calls"
            sudo = directory_path / "sudo"
            helm = directory_path / "helm"
            df = directory_path / "df"
            sudo.write_text(
                "#!/bin/sh\n"
                "printf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  'k3s kubectl get nodes --no-headers')\n"
                f"    printf '%s\\n' '{kubectl_nodes}'\n"
                "    exit 0 ;;\n"
                "  'k3s kubectl get storageclass local-path')\n"
                + ("    printf 'local-path\\n'; exit 0 ;;\n" if storageclass else "    exit 1 ;;\n")
                + "  *) exit 1 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            if helm_available:
                helm.write_text(
                    "#!/bin/sh\n"
                    "printf 'helm %s\\n' \"$*\" >> \"$CALLS\"\n"
                    "case \"$1\" in\n"
                    "  version) "
                    + ("printf 'v3.16.4+g\\n'; exit 0 ;;\n" if helm3 else "printf 'v2.17.0\\n'; exit 0 ;;\n")
                    + "  show) printf 'version: 88.6.1\\n'; exit 0 ;;\n"
                    "  *) exit 1 ;;\n"
                    "esac\n",
                    encoding="utf-8",
                )
            df.write_text(
                "#!/bin/sh\n"
                "printf 'df %s\\n' \"$*\" >> \"$CALLS\"\n"
                "printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'\n"
                f"printf '/dev/sda 100000 1000 {disk_available} 2%% /var/lib/rancher/k3s/storage\\n'\n",
                encoding="utf-8",
            )
            tools = [sudo, df]
            if helm_available:
                tools.append(helm)
            for tool in tools:
                tool.chmod(0o755)
            path = f"{directory}:{os.environ['PATH']}" if helm_available else f"{directory}:/usr/bin:/bin"
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                env={**os.environ, "PATH": path, "CALLS": str(calls)},
                capture_output=True,
                text=True,
                check=False,
            )
            recorded = calls.read_text(encoding="utf-8") if calls.exists() else ""
            return result, recorded

    def test_fails_closed_when_k3s_node_is_not_ready(self):
        result, _ = self.run_preflight(kubectl_nodes="n100 NotReady")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=FAIL"))

    def test_passes_when_a_node_is_cordoned_but_ready(self):
        result, _ = self.run_preflight(kubectl_nodes="n100 Ready,SchedulingDisabled")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=PASS"))

    def test_fails_closed_when_any_node_is_not_ready(self):
        result, _ = self.run_preflight(kubectl_nodes="n100 Ready\nn101 NotReady")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=FAIL"))

    def test_fails_closed_when_local_path_storageclass_is_missing(self):
        result, _ = self.run_preflight(storageclass=False)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=FAIL"))

    def test_fails_closed_when_helm_is_not_version_three(self):
        result, _ = self.run_preflight(helm3=False)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=FAIL"))

    def test_reports_helm_missing_without_hiding_the_failure_reason(self):
        result, _ = self.run_preflight(helm_available=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("check=helm status=FAIL reason=missing", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=FAIL"))

    def test_fails_closed_when_storage_has_less_than_eight_gib_available(self):
        result, _ = self.run_preflight(disk_available=(8 * 1024 * 1024) - 1)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=FAIL"))

    def test_fails_closed_when_storage_has_no_space_available(self):
        result, _ = self.run_preflight(disk_available=0)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=FAIL"))

    def test_passes_and_reports_chart_version_when_all_checks_succeed(self):
        result, calls = self.run_preflight()
        self.assertEqual(result.returncode, 0)
        self.assertIn("chart_version=88.6.1", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_preflight=PASS"))
        self.assertIn("k3s kubectl get nodes --no-headers", calls)
        self.assertIn("k3s kubectl get storageclass local-path", calls)
        self.assertIn("helm show chart prometheus-community/kube-prometheus-stack --version 88.6.1", calls)
        self.assertIn("df -Pk /var/lib/rancher/k3s/storage", calls)

    def test_only_read_only_commands_are_invoked(self):
        result, calls = self.run_preflight()
        self.assertEqual(result.returncode, 0)
        for forbidden in (" repo ", " install", " upgrade", " uninstall", " create", " apply", " delete", "secret"):
            self.assertNotIn(forbidden, calls)
        self.assertNotIn("http://", calls)


class MonitoringToolsTest(unittest.TestCase):
    DASHBOARD_MANIFEST = str(ROOT / "infra/k8s/monitoring/portal-http-dashboard.yaml")

    def run_tool(self, name, *args, stubs=None, env_overrides=None):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            calls = directory_path / "calls"
            for command, body in (stubs or {}).items():
                path = directory_path / command
                path.write_text(body, encoding="utf-8")
                path.chmod(0o755)
            env = {**os.environ, "PATH": f"{directory}:{os.environ['PATH']}", "CALLS": str(calls)}
            env.update(env_overrides or {})
            result = subprocess.run(
                ["bash", str(TOOLS / name), *args],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            recorded = calls.read_text(encoding="utf-8") if calls.exists() else ""
            return result, recorded

    def test_install_requires_explicit_apply_or_render(self):
        result, _ = self.run_tool("monitoring-install.sh")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--apply", result.stderr)

    def test_uninstall_requires_explicit_uninstall(self):
        result, _ = self.run_tool("monitoring-uninstall.sh")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--uninstall", result.stderr)

    def test_dashboard_apply_requires_explicit_mode(self):
        result, calls = self.run_tool("monitoring-dashboard-apply.sh")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--check", result.stderr)
        self.assertEqual(calls, "")

    def test_dashboard_apply_applies_only_dashboard_manifest(self):
        result, calls = self.run_tool(
            "monitoring-dashboard-apply.sh",
            "--apply",
            stubs={
                "sudo": "#!/bin/sh\n"
                "printf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'get deployment personal-server-monitoring-grafana'*) exit 0;;\n"
                f"  *'apply --dry-run=server -f {self.DASHBOARD_MANIFEST}'*) exit 0;;\n"
                f"  *'apply -f {self.DASHBOARD_MANIFEST}'*) exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
            },
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(f"apply -f {self.DASHBOARD_MANIFEST}", calls)
        self.assertNotIn("secret", calls.lower())
        self.assertNotIn("helm", calls.lower())
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_dashboard=PASS"))

    def test_dashboard_apply_runs_server_dry_run_before_apply(self):
        result, calls = self.run_tool(
            "monitoring-dashboard-apply.sh",
            "--apply",
            stubs={
                "sudo": "#!/bin/sh\n"
                "printf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'get deployment personal-server-monitoring-grafana'*) exit 0;;\n"
                f"  *'apply --dry-run=server -f {self.DASHBOARD_MANIFEST}'*) exit 0;;\n"
                f"  *'apply -f {self.DASHBOARD_MANIFEST}'*) exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
            },
        )
        self.assertEqual(result.returncode, 0)
        dry_run = calls.index("apply --dry-run=server")
        apply = calls.index("apply -f")
        self.assertLess(dry_run, apply)

    def test_dashboard_apply_fails_when_grafana_deployment_is_missing(self):
        result, calls = self.run_tool(
            "monitoring-dashboard-apply.sh", "--apply",
            stubs={"sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\nexit 1\n"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_dashboard=FAIL"))
        self.assertNotIn("dry-run", calls)
        self.assertNotIn(" apply ", calls)

    def test_dashboard_apply_fails_when_manifest_contract_is_invalid(self):
        original = DASHBOARD_MANIFEST.read_text(encoding="utf-8")
        DASHBOARD_MANIFEST.write_text(original.replace('grafana_dashboard: "1"', 'grafana_dashboard: "0"'), encoding="utf-8")
        try:
            result, calls = self.run_tool(
                "monitoring-dashboard-apply.sh", "--apply",
                stubs={"sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n"},
            )
        finally:
            DASHBOARD_MANIFEST.write_text(original, encoding="utf-8")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_dashboard=FAIL"))
        self.assertEqual(calls, "")

    def test_dashboard_apply_rejects_a_multi_document_manifest(self):
        original = DASHBOARD_MANIFEST.read_text(encoding="utf-8")
        DASHBOARD_MANIFEST.write_text(
            original
            + "\n--- # second document\napiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: forbidden\n  namespace: personal-server\n",
            encoding="utf-8",
        )
        try:
            result, calls = self.run_tool(
                "monitoring-dashboard-apply.sh",
                "--check",
                stubs={
                    "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                    "case \"$*\" in\n"
                    "  *'get deployment personal-server-monitoring-grafana'*) exit 0;;\n"
                    "  *) exit 1;;\n"
                    "esac\n",
                },
            )
        finally:
            DASHBOARD_MANIFEST.write_text(original, encoding="utf-8")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_dashboard=FAIL"))
        self.assertEqual(calls, "")

    def test_dashboard_check_accepts_a_crlf_manifest(self):
        original = DASHBOARD_MANIFEST.read_text(encoding="utf-8")
        DASHBOARD_MANIFEST.write_bytes(original.replace("\n", "\r\n").encode("utf-8"))
        try:
            result, _ = self.run_tool(
                "monitoring-dashboard-apply.sh",
                "--check",
                stubs={
                    "sudo": "#!/bin/sh\ncase \"$*\" in\n"
                    "  *'get deployment personal-server-monitoring-grafana'*) exit 0;;\n"
                    "  *) exit 1;;\n"
                    "esac\n",
                },
            )
        finally:
            DASHBOARD_MANIFEST.write_text(original, encoding="utf-8")
        self.assertEqual(result.returncode, 0)

    def test_dashboard_apply_fails_when_server_dry_run_fails(self):
        result, calls = self.run_tool(
            "monitoring-dashboard-apply.sh", "--apply",
            stubs={
                "sudo": "#!/bin/sh\n"
                "printf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'get deployment personal-server-monitoring-grafana'*) exit 0;;\n"
                "  *'--dry-run=server'*) exit 1;;\n"
                "  *) exit 0;;\n"
                "esac\n",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_dashboard=FAIL"))
        self.assertNotIn(" apply -f ", calls)

    def test_dashboard_apply_fails_when_apply_fails(self):
        result, calls = self.run_tool(
            "monitoring-dashboard-apply.sh", "--apply",
            stubs={
                "sudo": "#!/bin/sh\n"
                "printf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'get deployment personal-server-monitoring-grafana'*) exit 0;;\n"
                "  *'--dry-run=server'*) exit 0;;\n"
                "  *' apply -f '* ) exit 1;;\n"
                "  *) exit 0;;\n"
                "esac\n",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_dashboard=FAIL"))

    def test_dashboard_rollback_reports_delete_failure(self):
        result, calls = self.run_tool(
            "monitoring-dashboard-apply.sh", "--rollback",
            stubs={"sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\nexit 1\n"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("delete configmap portal-http-observability", calls)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_dashboard=FAIL"))

    def test_dashboard_apply_default_check_is_read_only(self):
        result, calls = self.run_tool(
            "monitoring-dashboard-apply.sh",
            "--check",
            stubs={
                "sudo": "#!/bin/sh\n"
                "printf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'get deployment personal-server-monitoring-grafana'*) exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
            },
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("get deployment personal-server-monitoring-grafana", calls)
        self.assertNotIn(" apply ", calls)
        self.assertNotIn(" delete ", calls)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_dashboard=PASS"))

    def test_dashboard_rollback_deletes_only_dashboard_configmap(self):
        result, calls = self.run_tool(
            "monitoring-dashboard-apply.sh",
            "--rollback",
            stubs={
                "sudo": "#!/bin/sh\n"
                "printf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'delete configmap portal-http-observability --ignore-not-found'*) exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
            },
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("delete configmap portal-http-observability --ignore-not-found", calls)
        self.assertNotIn("secret", calls.lower())
        self.assertNotIn("pvc", calls.lower())
        self.assertNotIn("deployment", calls.lower())
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_dashboard=PASS"))

    def test_apply_uses_pinned_helm_release_arguments(self):
        result, calls = self.run_tool(
            "monitoring-install.sh",
            "--apply",
            stubs={
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
                "preflight": "#!/bin/sh\nprintf 'preflight\\n' >> \"$CALLS\"\nprintf 'monitoring_preflight=PASS\\n'\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'get namespace monitoring'*) printf 'Error from server (NotFound): namespaces \"monitoring\" not found\\n' >&2; exit 1;;\n"
                "  *'create namespace monitoring'*) exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
            },
            env_overrides={"MONITORING_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(
            "helm upgrade --install personal-server-monitoring prometheus-community/kube-prometheus-stack "
            "--namespace monitoring --version 88.6.1 "
            "--values infra/k8s/monitoring/values.n100.yaml --wait --timeout 10m",
            calls,
        )
        self.assertEqual(result.stdout.rstrip().splitlines()[-1], "monitoring_install=PASS")

    def test_apply_refuses_to_upgrade_when_preflight_fails(self):
        result, calls = self.run_tool(
            "monitoring-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nprintf 'monitoring_preflight=FAIL\\n'\nexit 1\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
            env_overrides={"MONITORING_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("upgrade --install", calls)

    def test_apply_refuses_preexisting_monitoring_namespace(self):
        result, calls = self.run_tool(
            "monitoring-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nprintf 'monitoring_preflight=PASS\\n'\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
            env_overrides={"MONITORING_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("get namespace monitoring", calls)
        self.assertNotIn("upgrade --install", calls)

    def test_failed_apply_rolls_back_release_and_new_namespace(self):
        result, calls = self.run_tool(
            "monitoring-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nprintf 'monitoring_preflight=PASS\\n'\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'get namespace monitoring'*) printf 'Error from server (NotFound): namespaces \"monitoring\" not found\\n' >&2; exit 1;;\n"
                "  *'create namespace monitoring'*) exit 0;;\n"
                "  *'delete namespace monitoring'*) exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\ncase \"$1\" in upgrade) exit 9;; uninstall) exit 0;; esac\nexit 0\n",
            },
            env_overrides={"MONITORING_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("helm upgrade --install", calls)
        self.assertIn("helm uninstall personal-server-monitoring --namespace monitoring --wait --timeout 5m", calls)
        self.assertIn("delete namespace monitoring", calls)

    def test_apply_fails_closed_when_namespace_lookup_is_not_not_found(self):
        result, calls = self.run_tool(
            "monitoring-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nprintf 'monitoring_preflight=PASS\\n'\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\nprintf 'forbidden\\n' >&2\nexit 1\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
            env_overrides={"MONITORING_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("upgrade --install", calls)
        self.assertNotIn("delete namespace", calls)

    def test_apply_fails_closed_on_namespace_create_race_without_deletion(self):
        result, calls = self.run_tool(
            "monitoring-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nprintf 'monitoring_preflight=PASS\\n'\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'get namespace monitoring'*) printf 'Error from server (NotFound): namespaces \"monitoring\" not found\\n' >&2; exit 1;;\n"
                "  *'create namespace monitoring'*) printf 'AlreadyExists\\n' >&2; exit 1;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
            env_overrides={"MONITORING_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("upgrade --install", calls)
        self.assertNotIn("delete namespace", calls)

    def test_render_only_calls_helm_template(self):
        result, calls = self.run_tool(
            "monitoring-install.sh",
            "--render",
            stubs={
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(
            "helm template personal-server-monitoring prometheus-community/kube-prometheus-stack "
            "--namespace monitoring --version 88.6.1 --values infra/k8s/monitoring/values.n100.yaml",
            calls,
        )
        self.assertNotIn("upgrade", calls)

    def test_verify_fails_when_grafana_service_is_not_cluster_ip(self):
        result, _ = self.run_tool(
            "monitoring-verify.sh",
            stubs={
                "sudo": "#!/bin/sh\ncase \"$*\" in\n"
                "  *'get pvc'*) printf 'prometheus Bound\\ngrafana Bound\\n'; exit 0;;\n"
                "  *'get pods'*) exit 0;;\n"
                "  *'wait --for=condition=Ready pod --all'*) exit 0;;\n"
                "  *'get service personal-server-monitoring-grafana'*) printf 'NodePort\\n'; exit 0;;\n"
                "  *'get configmap'*) printf 'configmap/grafana-dashboard\\n'; exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stderr.rstrip().endswith("monitoring_verify=FAIL"))

    def test_verify_accepts_bound_pvcs_with_real_kubectl_columns(self):
        result, _ = self.run_tool(
            "monitoring-verify.sh",
            stubs={
                "sudo": "#!/bin/sh\ncase \"$*\" in\n"
                "  *'get pvc'*) printf '%s\\n' 'grafana Bound pvc-grafana 1Gi RWO local-path <unset> 11h' 'prometheus Bound pvc-prometheus 5Gi RWO local-path <unset> 11h'; exit 0;;\n"
                "  *'get pods'*) exit 0;;\n"
                "  *'wait --for=condition=Ready pod --all'*) exit 0;;\n"
                "  *'get service personal-server-monitoring-grafana'*) printf 'ClusterIP\\n'; exit 0;;\n"
                "  *'get configmap'*) printf 'configmap/grafana-dashboard\\n'; exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
            },
        )
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("monitoring_verify=PASS"))

    def test_uninstall_preserves_data_without_delete_data(self):
        result, calls = self.run_tool(
            "monitoring-uninstall.sh",
            "--uninstall",
            stubs={
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("helm uninstall personal-server-monitoring --namespace monitoring --wait --timeout 5m", calls)
        self.assertNotIn("delete pvc", calls)
        self.assertNotIn("delete namespace", calls)

    def test_delete_data_requires_explicit_flag_and_deletes_pvc_and_namespace(self):
        result, calls = self.run_tool(
            "monitoring-uninstall.sh",
            "--uninstall",
            "--delete-data",
            stubs={
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("delete pvc --all", calls)
        self.assertIn("delete namespace monitoring", calls)

    def test_verify_port_forward_binds_localhost_and_does_not_read_secrets(self):
        result, calls = self.run_tool(
            "monitoring-verify.sh",
            "--port-forward-check",
            stubs={
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'get pvc'*) printf 'prometheus Bound\\ngrafana Bound\\n'; exit 0;; *'get pods'*) exit 0;;\n"
                "  *'wait --for=condition=Ready pod --all'*) exit 0;;\n"
                "  *'get service personal-server-monitoring-grafana'*) printf 'ClusterIP\\n'; exit 0;;\n"
                "  *'get configmap'*) printf 'configmap/grafana-dashboard\\n'; exit 0;;\n"
                "  *'port-forward'*) sleep 30;;\n  *) exit 1;;\n"
                "esac\n",
                "curl": "#!/bin/sh\nprintf 'curl %s\\n' \"$*\" >> \"$CALLS\"\nprintf '{\"status\":\"success\",\"data\":{\"active\":[{\"health\":\"up\"}]}}\\n'\nexit 0\n",
            },
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("port-forward --address 127.0.0.1 service/personal-server-monitoring-grafana 3000:80", calls)
        self.assertIn("port-forward --address 127.0.0.1 service/personal-server-monitoring-prometheus 9090:9090", calls)
        self.assertIn("http://127.0.0.1:3000/login", calls)
        self.assertNotIn("get secret", calls)
        self.assertNotIn("jsonpath='{.data", calls)

    def test_verify_fails_when_port_forward_login_check_fails(self):
        result, _ = self.run_tool(
            "monitoring-verify.sh",
            "--port-forward-check",
            stubs={
                "sudo": "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'get pvc'*) printf 'prometheus Bound\\ngrafana Bound\\n'; exit 0;; *'get pods'*) exit 0;;\n"
                "  *'wait --for=condition=Ready pod --all'*) exit 0;;\n"
                "  *'get service personal-server-monitoring-grafana'*) printf 'ClusterIP\\n'; exit 0;;\n"
                "  *'get configmap'*) printf 'configmap/grafana-dashboard\\n'; exit 0;;\n"
                "  *'port-forward'*) sleep 30;;\n  *) exit 1;;\n"
                "esac\n",
                "curl": "#!/bin/sh\nexit 22\n",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stderr.rstrip().endswith("monitoring_verify=FAIL"))

    def test_verify_fails_when_port_forward_exits_before_login_check(self):
        result, _ = self.run_tool(
            "monitoring-verify.sh",
            "--port-forward-check",
            stubs={
                "sudo": "#!/bin/sh\ncase \"$*\" in\n"
                "  *'get pvc'*) printf 'prometheus Bound\\ngrafana Bound\\n'; exit 0;; *'get pods'*) exit 0;;\n"
                "  *'wait --for=condition=Ready pod --all'*) exit 0;;\n"
                "  *'get service personal-server-monitoring-grafana'*) printf 'ClusterIP\\n'; exit 0;;\n"
                "  *'get configmap'*) printf 'configmap/grafana-dashboard\\n'; exit 0;;\n"
                "  *'port-forward'*) exit 1;; *) exit 1;;\n"
                "esac\n",
                "curl": "#!/bin/sh\nexit 0\n",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stderr.rstrip().endswith("monitoring_verify=FAIL"))


if __name__ == "__main__":
    unittest.main()


class HostDiskNotificationPlanTests(unittest.TestCase):
    def load_plan(self):
        spec = importlib.util.spec_from_file_location('host_disk_notification_plan', TOOLS / 'host-disk-notification-plan.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_plan_is_bounded_fixed_scope_and_does_not_contact_production(self):
        module = self.load_plan()
        now = datetime(2026, 10, 19, tzinfo=timezone.utc)
        plan = module.plan(now, test_id='012345abcdef')
        self.assertEqual(plan['metadata']['namespace'], 'monitoring')
        self.assertEqual(plan['metadata']['name'], 'host-disk-notification-test-012345abcdef')
        rule = plan['spec']['groups'][0]['rules'][0]
        self.assertEqual(rule['alert'], 'HostDiskNotificationTest')
        self.assertEqual(rule['for'], '0s')
        self.assertIn(str(int(now.timestamp()) + 240), rule['expr'])
        self.assertEqual(rule['labels']['sre_telegram'], 'true')
        # Preparation is pure and cannot invoke kubectl, Telegram, or subprocesses.
        source = (TOOLS / 'host-disk-notification-plan.py').read_text()
        self.assertNotIn('import subprocess', source)
        self.assertNotIn('import urllib', source)
        self.assertNotIn('--go', source)

    def test_invalid_windows_and_untrusted_ids_are_rejected(self):
        module = self.load_plan()
        now = datetime(2026, 10, 19, tzinfo=timezone.utc)
        for duration in (0, 119, 301, True, float('inf')):
            with self.assertRaises(ValueError):
                module.plan(now, duration=duration)
        for test_id in ('private-address', '../anything', '012345ABCDEF', '0' * 13):
            with self.assertRaises(ValueError):
                module.plan(now, test_id=test_id)
        with self.assertRaises(ValueError):
            module.plan(now.replace(tzinfo=None))
