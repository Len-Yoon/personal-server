import os
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "run-n100-operations.sh"
HELPER = ROOT / "infra/k8s/tools/n100-k3s-operations-helper.py"
INSTALLER = ROOT / "infra/k8s/tools/install-n100-k3s-operations-helper.sh"
HELPER_SPEC = importlib.util.spec_from_file_location("n100_helper", HELPER)
assert HELPER_SPEC is not None and HELPER_SPEC.loader is not None
HELPER_MODULE = importlib.util.module_from_spec(HELPER_SPEC)
HELPER_SPEC.loader.exec_module(HELPER_MODULE)
ALLOWED = {
    "diagnose",
    "deploy_safe_crawler",
    "verify_news_observability",
    "apply_news_observability",
}


class N100OperationsTests(unittest.TestCase):
    def run_operation(self, operation, *extra, env=None):
        merged = os.environ.copy()
        merged.update(
            {
                "N100_OPERATIONS_PROJECT_ROOT": str(ROOT),
                "N100_OPERATIONS_ACTIONS_ROOT": str(ROOT),
                "N100_OPERATIONS_DEPLOY_SHA": "a" * 40,
            }
        )
        if env:
            merged.update(env)
        return subprocess.run(
            ["bash", str(SCRIPT), operation, *extra],
            cwd=ROOT,
            env=merged,
            text=True,
            capture_output=True,
        )

    def test_unknown_operation_is_rejected(self):
        result = self.run_operation("unknown")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("n100_operation=invalid status=FAIL", result.stdout + result.stderr)
        self.assertNotIn("kubectl", result.stdout + result.stderr)

    def test_extra_arguments_are_rejected(self):
        result = self.run_operation("diagnose", "extra")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("n100_operation=invalid status=FAIL", result.stdout + result.stderr)

    def test_allowlist_is_exactly_the_four_operations(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for operation in ALLOWED:
            self.assertIn(operation, text)
        self.assertNotIn("sudo -n k3s", text)
        self.assertIn("/mnt/c/personal-server", text)
        self.assertIn("/usr/local/libexec/personal-server/n100-k3s-operations", text)
        self.assertIn("set +x", text)
        self.assertIn('sudo -n "$HELPER" diagnose', text)
        self.assertIn('sudo -n "$HELPER" verify_news_observability', text)
        self.assertIn('sudo -n "$HELPER" apply_news_observability', text)

    def test_deploy_requires_a_valid_sha_and_fixed_service(self):
        result = self.run_operation(
            "deploy_safe_crawler",
            env={"N100_OPERATIONS_DEPLOY_SHA": "not-a-sha"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("n100_operation=deploy_safe_crawler status=FAIL", result.stdout + result.stderr)
        self.assertNotIn("super-secret", result.stdout + result.stderr)

    def test_apply_uses_only_two_fixed_manifests_without_secret_values(self):
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory) / "bin"
            bin_dir.mkdir()
            calls = Path(directory) / "calls"
            fake_sudo = bin_dir / "sudo"
            fake_sudo.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$N100_TEST_CALLS\"\n"
                "cat >> \"$N100_TEST_CALLS\"\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_sudo.chmod(0o755)
            env = {
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "N100_TEST_CALLS": str(calls),
            }
            result = self.run_operation("apply_news_observability", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = result.stdout + result.stderr
            self.assertIn("n100_operation=apply_news_observability status=PASS", output)
            self.assertNotIn("super-secret", output)
            entries = calls.read_text(encoding="utf-8").splitlines()
            apply_entries = [entry for entry in entries if "n100-k3s-operations apply" in entry]
            self.assertEqual(len(apply_entries), 1)
            self.assertIn("apiVersion: monitoring.coreos.com/v1", calls.read_text(encoding="utf-8"))
            self.assertNotIn("super-secret", calls.read_text(encoding="utf-8"))

    def test_helper_has_exact_root_operation_allowlist(self):
        self.assertEqual(
            HELPER_MODULE.ALLOWED,
            {"diagnose", "verify_news_observability", "apply_news_observability"},
        )
        self.assertEqual(
            subprocess.run(
                ["python3", str(HELPER), "apply_news_observability", "extra"],
                text=True,
                capture_output=True,
            ).returncode,
            2,
        )
        text = HELPER.read_text(encoding="utf-8")
        self.assertNotIn("source_file", text)
        self.assertNotIn("manifest_path", text)
        self.assertEqual(text.splitlines()[0], "#!/usr/bin/python3")

    def test_helper_rejects_symlink_multidoc_list_and_identity_mismatch(self):
        valid = """apiVersion: monitoring.coreos.com/v1\nkind: ServiceMonitor\nmetadata:\n  name: crawler-news-observability\n  namespace: monitoring\n---\napiVersion: monitoring.coreos.com/v1\nkind: PrometheusRule\nmetadata:\n  name: sre-telegram-k3s-alerts\n  namespace: monitoring\n"""
        cases = {
            "multidoc": valid + "---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: bad\n",
            "list": "apiVersion: v1\nkind: List\nitems: []\n",
            "mismatch": valid.replace("crawler-news-observability", "not-allowed", 1),
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                result = subprocess.run(
                    ["python3", str(HELPER), "apply_news_observability"],
                    input=payload,
                    text=True,
                    capture_output=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("super-secret", result.stdout + result.stderr)

    def test_runner_rejects_symlink_manifest_source(self):
        with tempfile.TemporaryDirectory() as directory:
            actions = Path(directory) / "actions"
            target = actions / "infra/k8s/sre-telegram"
            target.mkdir(parents=True)
            (target / "prometheus-rule.yaml").write_text("kind: List\nitems: []\n", encoding="utf-8")
            (target / "crawler-news-observability.yaml").symlink_to(target / "prometheus-rule.yaml")
            result = self.run_operation(
                "apply_news_observability",
                env={"N100_OPERATIONS_ACTIONS_ROOT": str(actions)},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("super-secret", result.stdout + result.stderr)

    def test_verify_suppresses_secret_sentinel_from_fixed_command_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory) / "bin"
            bin_dir.mkdir()
            for command in ("curl", "docker", "sudo"):
                path = bin_dir / command
                path.write_text(
                    "#!/usr/bin/env bash\n"
                    "printf '%s\\n' super-secret\n"
                    "exit 0\n",
                    encoding="utf-8",
                )
                path.chmod(0o755)
            result = self.run_operation(
                "verify_news_observability",
                env={"PATH": f"{bin_dir}:{os.environ['PATH']}"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("super-secret", result.stdout + result.stderr)

    def test_helper_applies_the_same_canonical_stdin_bytes_twice(self):
        payload = (
            "apiVersion: monitoring.coreos.com/v1\nkind: ServiceMonitor\nmetadata:\n"
            "  name: crawler-news-observability\n  namespace: monitoring\n---\n"
            "apiVersion: monitoring.coreos.com/v1\nkind: PrometheusRule\nmetadata:\n"
            "  name: sre-telegram-k3s-alerts\n  namespace: monitoring\n"
        ).encode()
        calls = []
        with mock.patch.object(
            HELPER_MODULE.subprocess,
            "run",
            side_effect=lambda command, **kwargs: calls.append((command, kwargs["input"]))
            or subprocess.CompletedProcess(command, 0),
        ):
            self.assertTrue(HELPER_MODULE.canonical_documents(payload))
            self.assertTrue(HELPER_MODULE.run_apply_from_bytes(payload))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], ["/usr/local/bin/k3s", "kubectl", "apply", "--dry-run=client", "-f", "-"])
        self.assertEqual(calls[0][1], calls[1][1])

    def test_helper_verify_requires_nonempty_secret_stdout_without_logging_value(self):
        base = ["/usr/local/bin/k3s", "kubectl"]

        def fake_run(command, **kwargs):
            if "secret" in command:
                return subprocess.CompletedProcess(command, 0, stdout=b"super-secret")
            return subprocess.CompletedProcess(command, 0, stdout=b"")

        with mock.patch.object(HELPER_MODULE.subprocess, "run", side_effect=fake_run):
            self.assertTrue(HELPER_MODULE.run_verify())

        def empty_secret(command, **kwargs):
            if "secret" in command:
                return subprocess.CompletedProcess(command, 0, stdout=b"")
            return subprocess.CompletedProcess(command, 0, stdout=b"")

        with mock.patch.object(HELPER_MODULE.subprocess, "run", side_effect=empty_secret):
            self.assertFalse(HELPER_MODULE.run_verify())
        self.assertEqual(base, ["/usr/local/bin/k3s", "kubectl"])

    def test_helper_oserror_exits_without_traceback(self):
        result = subprocess.run(
            ["python3", str(HELPER), "verify_news_observability"],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)

    def test_installer_contains_only_three_exact_sudoers_operations(self):
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("EUID", text)
        self.assertIn('exec /usr/bin/python3 -I "$INSTALLER" "$@"', text)
        self.assertNotIn("sudo -n k3s", text)

    def test_script_is_not_an_arbitrary_command_runner(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn('eval "', text)
        self.assertNotIn("sudo -n k3s", text)


if __name__ == "__main__":
    unittest.main()
