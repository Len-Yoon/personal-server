"""Isolated NetworkPolicy enforcement probe; no live cluster is contacted."""

import importlib.util
import io
import json
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "infra/k8s/tools/networkpolicy-smoke.py"
spec = importlib.util.spec_from_file_location("networkpolicy_smoke", SCRIPT)
assert spec is not None and spec.loader is not None
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class FakeKube:
    def __init__(self, *, enforces=True, existing_namespace=None, creation_times_out=False, actual_context="isolated-test"):
        self.context = "isolated-test"
        self.actual_context = actual_context
        self.enforces = enforces
        self.creation_times_out = creation_times_out
        self.namespace = existing_namespace
        self.phase = "allow"
        self.events = []

    def current_context(self):
        return self.actual_context

    def run(self, *args, input_text=None, check=True, timeout=30):
        self.events.append((args, input_text))
        result = self._respond(args, input_text)
        if check and result.returncode:
            raise smoke.SmokeError("fake command failed")
        return result

    def _respond(self, args, input_text):
        ok = lambda output="": subprocess.CompletedProcess(args, 0, output, "")
        failed = lambda: subprocess.CompletedProcess(args, 1, "", "probe failed")
        if args[:2] == ("get", "namespace"):
            return ok(json.dumps(self.namespace)) if self.namespace else ok()
        if args[:2] == ("create", "-f"):
            resource = json.loads(input_text)
            if resource["kind"] == "Namespace":
                if self.namespace:
                    return failed()
                self.namespace = resource
                if self.creation_times_out:
                    raise smoke.SmokeError("creation result was incomplete")
            return ok()
        if args[:2] == ("apply", "-f"):
            resources = json.loads(input_text)
            if resources["kind"] == "NetworkPolicy":
                self.phase = "deny"
            elif resources["kind"] == "List":
                self.phase = "selective"
            return ok()
        if args[:2] == ("delete", "namespace"):
            self.namespace = None
            return ok()
        if "wait" in args or ("get", "pod/server") == args[-3:-1]:
            return ok()
        if "pod/server" in args and "-o" in args:
            return ok(json.dumps({"status": {"podIP": "10.99.0.10"}}))
        if "exec" in args:
            pod = args[args.index("exec") + 1]
            command = args[args.index("--") + 1:]
            if command == ("true",):
                return ok()
            if pod == "server" and "127.0.0.1" in " ".join(command):
                return ok("ok")
            allowed = self.phase == "allow" or (self.phase == "selective" and pod == "allowed")
            if self.phase == "deny" and not self.enforces:
                allowed = True
            return ok("ok" if "wget" in command else "resolved") if allowed else failed()
        return ok()


class NetworkPolicySmokeTests(unittest.TestCase):
    def test_kubectl_uses_fixed_noninteractive_command_and_explicit_context(self):
        completed = subprocess.CompletedProcess([], 0, "ok", "")
        with patch.object(subprocess, "run", return_value=completed) as run:
            kube = smoke.Kube("isolated-test")
            kube.run("get", "namespace", "np-smoke-0123456789ab")
        self.assertEqual(
            run.call_args.args[0],
            ["sudo", "-n", "k3s", "kubectl", "--context", "isolated-test", "get", "namespace", "np-smoke-0123456789ab"],
        )
        self.assertFalse(run.call_args.kwargs["check"])

    def test_render_is_read_only_and_cluster_modes_require_explicit_context(self):
        output = io.StringIO()
        with patch.object(smoke, "Kube", side_effect=AssertionError("cluster access attempted")):
            with redirect_stdout(output):
                self.assertEqual(smoke.main(["--render"]), 0)
            with self.assertRaises(SystemExit):
                smoke.main(["--go"])
        self.assertEqual(len(json.loads(output.getvalue())), 4)

    def test_manifests_target_only_temporary_namespace_and_unprivileged_pods(self):
        namespace = "np-smoke-0123456789ab"
        pods = smoke.build_pods(namespace)
        deny = smoke.build_deny_policy(namespace)
        allows = smoke.build_allow_policies(namespace)

        self.assertEqual(deny["spec"]["policyTypes"], ["Ingress", "Egress"])
        self.assertEqual(deny["spec"]["podSelector"], {})
        self.assertNotIn("ingress", deny["spec"])
        self.assertNotIn("egress", deny["spec"])
        self.assertEqual({item["kind"] for item in pods["items"]}, {"Pod"})
        self.assertEqual({item["metadata"]["namespace"] for item in pods["items"]}, {namespace})
        for pod in pods["items"]:
            self.assertFalse(pod["spec"]["automountServiceAccountToken"])
            self.assertTrue(pod["spec"]["securityContext"]["runAsNonRoot"])
            self.assertRegex(pod["spec"]["containers"][0]["image"], r"^busybox@sha256:[0-9a-f]{64}$")
            self.assertEqual(pod["spec"]["containers"][0]["imagePullPolicy"], "Never")
            self.assertTrue(pod["spec"]["containers"][0]["securityContext"]["readOnlyRootFilesystem"])
            self.assertNotIn("hostNetwork", pod["spec"])
            if pod["metadata"]["name"] == "server":
                self.assertEqual(pod["spec"]["volumes"], [{"name": "temporary-content", "emptyDir": {}}])
            else:
                self.assertNotIn("volumes", pod["spec"])
        self.assertEqual({item["metadata"]["namespace"] for item in allows["items"]}, {namespace})
        self.assertEqual({item["kind"] for item in allows["items"]}, {"NetworkPolicy"})
        self.assertNotIn("namespaceSelector", json.dumps(allows["items"][0]))
        self.assertIn("kube-system", json.dumps(allows))
        self.assertNotIn("default", json.dumps(pods))

    def test_allow_deny_selective_allow_and_dns_are_observed_then_cleaned(self):
        kube = FakeKube()
        smoke.run_smoke(kube, "0123456789ab", observation_seconds=0)

        self.assertIsNone(kube.namespace)
        self.assertEqual(kube.phase, "selective")
        commands = [args for args, _ in kube.events]
        self.assertTrue(any(args[:2] == ("create", "-f") for args in commands))
        self.assertTrue(any("nslookup" in args for args in commands))
        self.assertTrue(any(args[:2] == ("delete", "namespace") for args in commands))
        self.assertTrue(all("default" not in args and "monitoring" not in args for args in commands))

    def test_unenforced_deny_fails_and_cleans_own_namespace(self):
        kube = FakeKube(enforces=False)
        with self.assertRaises(smoke.SmokeError):
            smoke.run_smoke(kube, "0123456789ab", observation_seconds=0)
        self.assertIsNone(kube.namespace)

    def test_incomplete_create_response_still_cleans_matching_namespace(self):
        kube = FakeKube(creation_times_out=True)
        with self.assertRaises(smoke.SmokeError):
            smoke.run_smoke(kube, "0123456789ab", observation_seconds=0)
        self.assertIsNone(kube.namespace)

    def test_context_mismatch_stops_before_resource_creation(self):
        kube = FakeKube(actual_context="another-cluster")
        with self.assertRaises(smoke.SmokeError):
            smoke.run_smoke(kube, "0123456789ab", observation_seconds=0)
        self.assertIsNone(kube.namespace)
        self.assertFalse(any(args[:2] == ("create", "-f") for args, _ in kube.events))

    def test_existing_namespace_is_never_deleted(self):
        original = {"kind": "Namespace", "metadata": {"name": "np-smoke-0123456789ab", "labels": {}}}
        kube = FakeKube(existing_namespace=original)
        with self.assertRaises(smoke.SmokeError):
            smoke.run_smoke(kube, "0123456789ab", observation_seconds=0)
        self.assertIs(kube.namespace, original)
        self.assertFalse(any(args[:2] == ("delete", "namespace") for args, _ in kube.events))

    def test_manual_cleanup_refuses_unlabeled_namespace(self):
        original = {"kind": "Namespace", "metadata": {"name": "np-smoke-0123456789ab", "labels": {}}}
        kube = FakeKube(existing_namespace=original)
        with self.assertRaises(smoke.SmokeError):
            smoke.cleanup_namespace(kube, "np-smoke-0123456789ab")
        self.assertIs(kube.namespace, original)
        self.assertFalse(any(args[:2] == ("delete", "namespace") for args, _ in kube.events))

    def test_operational_candidate_remains_unresolved_and_has_no_default_deny(self):
        candidate = (SCRIPT.parents[1] / "networkpolicy/portal-allowlist.yaml.tmpl").read_text()
        self.assertIn("__PORTAL_NAMESPACE__", candidate)
        self.assertIn("operator.prometheus.io/name: personal-server-monitoring-prometheus", candidate)
        self.assertIn("k8s-app: kube-dns", candidate)
        self.assertNotIn("name: default-deny", candidate)
        self.assertNotIn("podSelector: {}", candidate)

    def test_http_probe_brackets_ipv6_pod_ip(self):
        kube = FakeKube()
        smoke._probe_http(kube, "np-smoke-0123456789ab", "allowed", "2001:db8::1")
        self.assertIn("http://[2001:db8::1]:8080/", kube.events[-1][0])


if __name__ == "__main__":
    unittest.main()
