"""Exercise isolated rollback drill at the external kubectl boundary."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/k8s/tools/deployment-rollback-lab.sh"
MANIFEST = ROOT / "infra/k8s/observability-lab/rollback-lab.yaml"


class RollbackLabTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.calls = self.directory / "calls"
        fake = self.directory / "sudo"
        fake.write_text('''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$LAB_CALLS"
case "$*" in
  *'config current-context'*) printf '%s' "${LAB_CONTEXT:-test}";;
  *'get namespace deployment-rollback-lab'*) printf '{"metadata":{"labels":{"app.kubernetes.io/managed-by":"%s"}}}' "${LAB_OWNER:-deployment-rollback-lab}";;
  *'get deployment rollback-sample'*)
    if [[ "$*" == *'-o json'* ]]; then
      printf '{"metadata":{"generation":2,"labels":{"app.kubernetes.io/managed-by":"%s"}},"status":{"observedGeneration":2,"updatedReplicas":1,"readyReplicas":%s}}' "${LAB_DEPLOY_OWNER:-deployment-rollback-lab}" "${LAB_READY_AFTER_FAULT:-0}"
    fi;;
  *'patch deployment rollback-sample'*)
    if [ "${LAB_INTERRUPT:-no}" = yes ]; then kill -TERM "$PPID"; fi
    [ "${LAB_PATCH_FAIL:-no}" = no ];;
  *'apply -f '*rollback-lab.yaml*)
    n=$(awk '/apply -f/ {n++} END {print n}' "$LAB_CALLS")
    if [ "$n" -gt 1 ] && [ "${LAB_RESTORE_FAIL:-no}" = yes ]; then exit 1; fi;;
  *'rollout status '*)
    n=$(awk '/apply -f/ {n++} END {print n}' "$LAB_CALLS")
    if [ "$n" -eq 1 ] && awk '/patch deployment/ {found=1} END {exit !found}' "$LAB_CALLS"; then exit 1; fi
    [ "${LAB_BASELINE_FAIL:-no}" = no ];;
  *'exec deployment/rollback-sample'*)
    if [[ "$*" == *'wget'* ]]; then
      n=$(awk '/apply -f/ {n++} END {print n}' "$LAB_CALLS")
      if [ "$n" -gt 1 ] && [ "${LAB_RESTORE_RESPONSE_BAD:-no}" = yes ]; then printf wrong; else printf '%s' "${LAB_RESPONSE:-rollback lab sample ready}"; fi
    fi;;
  *) exit 0;;
esac
''')
        fake.chmod(0o755)

    def run_tool(self, *args, **extra):
        env = dict(os.environ, PATH=f"{self.directory}:{os.environ['PATH']}", LAB_CALLS=str(self.calls), **extra)
        return subprocess.run(["bash", str(SCRIPT), *args], cwd=ROOT, env=env, text=True, capture_output=True, timeout=10)

    def recorded(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_check_and_context_mismatch_never_write(self):
        for extra in ({}, {"LAB_CONTEXT": "wrong"}, {"LAB_OWNER": "other"}, {"LAB_DEPLOY_OWNER": "other"}):
            with self.subTest(extra=extra):
                result = self.run_tool("--check", "--context", "test", **extra)
                self.assertEqual(result.returncode == 0, not extra)
                self.assertFalse(any(" apply " in c or " patch " in c for c in self.recorded()))

    def test_success_detects_failure_and_restores_fixed_baseline(self):
        result = self.run_tool("--go", "--context", "test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("deployment_rollback_lab=PASS", result.stdout)
        calls = self.recorded()
        patch = next(i for i, c in enumerate(calls) if "patch deployment" in c)
        self.assertTrue(any("rollout status" in c for c in calls[:patch]))
        self.assertTrue(any("apply -f" in c for c in calls[patch + 1:]))
        self.assertTrue(any("exec deployment/rollback-sample" in c for c in calls[patch + 1:]))
        self.assertFalse(any("delete " in c or "-n monitoring" in c or "-n personal-server" in c for c in calls))
        self.assertTrue(all("--request-timeout=" in c for c in calls if "--context" in c))

    def test_go_rejects_foreign_ownership_before_any_write(self):
        for extra in ({"LAB_OWNER": "other"}, {"LAB_DEPLOY_OWNER": "other"}, {"LAB_CONTEXT": "wrong"}):
            with self.subTest(extra=extra):
                self.calls.unlink(missing_ok=True)
                result = self.run_tool("--go", "--context", "test", **extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(" apply " in c or " patch " in c for c in self.recorded()))

    def test_baseline_failure_never_injects_fault(self):
        result = self.run_tool("--go", "--context", "test", LAB_BASELINE_FAIL="yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("patch deployment" in c for c in self.recorded()))

    def test_wrong_http_baseline_response_never_injects_fault(self):
        result = self.run_tool("--go", "--context", "test", LAB_RESPONSE="wrong")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("patch deployment" in c for c in self.recorded()))
        self.assertNotIn("wrong", result.stdout + result.stderr)

    def test_wrong_http_restored_response_reports_failure(self):
        result = self.run_tool("--go", "--context", "test", LAB_RESTORE_RESPONSE_BAD="yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback_restore=FAIL", result.stdout)
        self.assertNotIn("deployment_rollback_lab=PASS", result.stdout)

    def test_not_observed_failure_is_not_success(self):
        result = self.run_tool("--go", "--context", "test", LAB_READY_AFTER_FAULT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback_restore=PASS", result.stdout)

    def test_partial_patch_and_interrupt_restore_before_failure_exit(self):
        for extra in ({"LAB_PATCH_FAIL": "yes"}, {"LAB_INTERRUPT": "yes"}):
            with self.subTest(extra=extra):
                self.calls.unlink(missing_ok=True)
                result = self.run_tool("--go", "--context", "test", **extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("rollback_restore=PASS", result.stdout)
                self.assertNotIn("deployment_rollback_lab=PASS", result.stdout)

    def test_failed_restore_reports_failure(self):
        result = self.run_tool("--go", "--context", "test", LAB_RESTORE_FAIL="yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback_restore=FAIL", result.stdout)
        self.assertNotIn("deployment_rollback_lab=PASS", result.stdout)

    def test_manifest_has_no_storage_or_external_service_and_pinned_image(self):
        self.assertTrue(MANIFEST.is_file())
        docs = list(yaml.safe_load_all(MANIFEST.read_text()))
        self.assertEqual({d["kind"] for d in docs}, {"Namespace", "Deployment"})
        deployment = next(d for d in docs if d["kind"] == "Deployment")
        self.assertEqual(deployment["metadata"]["namespace"], "deployment-rollback-lab")
        pod = deployment["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertNotIn("hostPath", json.dumps(pod))
        self.assertIn("@sha256:", pod["containers"][0]["image"])
