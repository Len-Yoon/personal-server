import os
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "run-n100-operations.sh"
HELPER = ROOT / "infra/k8s/tools/n100-k3s-operations-helper.py"
INSTALLER = ROOT / "infra/k8s/tools/install-n100-k3s-operations-helper.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "n100-operations.yml"
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

    def test_apply_sends_two_yaml_documents_with_the_fixed_identities(self):
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory) / "bin"
            bin_dir.mkdir()
            calls = Path(directory) / "calls"
            fake_sudo = bin_dir / "sudo"
            fake_sudo.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$N100_TEST_CALLS\"\n"
                "cat > \"$N100_TEST_STDIN\"\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_sudo.chmod(0o755)
            env = {
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "N100_TEST_CALLS": str(calls),
                "N100_TEST_STDIN": str(Path(directory) / "apply.yaml"),
            }
            result = self.run_operation("apply_news_observability", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = result.stdout + result.stderr
            self.assertIn("n100_operation=apply_news_observability status=PASS", output)
            self.assertNotIn("super-secret", output)
            entries = calls.read_text(encoding="utf-8").splitlines()
            apply_entries = [entry for entry in entries if "n100-k3s-operations apply" in entry]
            self.assertEqual(len(apply_entries), 1)
            applied = Path(env["N100_TEST_STDIN"]).read_text(encoding="utf-8")
            documents = list(yaml.safe_load_all(applied))
            identities = {
                (document["kind"], document["metadata"]["namespace"], document["metadata"]["name"])
                for document in documents
            }
            self.assertEqual(
                identities,
                {
                    ("ServiceMonitor", "monitoring", "crawler-news-observability"),
                    ("PrometheusRule", "monitoring", "sre-telegram-k3s-alerts"),
                },
            )
            self.assertNotIn("super-secret", applied)

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


class N100OperationsWorkflowTests(unittest.TestCase):
    def read_workflow(self):
        self.assertTrue(WORKFLOW.is_file(), "N100 operations workflow is required")
        return WORKFLOW.read_text(encoding="utf-8")

    def workflow_document(self):
        document = yaml.safe_load(self.read_workflow())
        self.assertIsInstance(document, dict)
        return document

    def test_workflow_is_manual_and_has_only_the_fixed_operation_choice(self):
        document = self.workflow_document()
        triggers = document.get("on", document.get(True))
        self.assertEqual(set(triggers), {"workflow_dispatch"})
        operation = triggers["workflow_dispatch"]["inputs"]["operation"]
        self.assertEqual(operation["type"], "choice")
        self.assertTrue(operation["required"])
        self.assertEqual(operation["options"], [
            "diagnose",
            "deploy_safe_crawler",
            "verify_news_observability",
            "apply_news_observability",
        ])
        text = self.read_workflow()
        self.assertNotIn("pull_request", text)
        self.assertNotIn("schedule:", text)
        self.assertNotIn("type: string", text)
        for operation in sorted(ALLOWED):
            self.assertIn(f"- {operation}", text)
        self.assertNotIn("inputs.command", text)
        self.assertNotIn("inputs.ref", text)
        self.assertNotIn("inputs.path", text)
        self.assertNotIn("inputs.sha", text)

    def test_workflow_requires_current_main_sha_with_successful_push_ci(self):
        text = self.read_workflow()
        self.assertIn("github.ref == 'refs/heads/main'", text)
        self.assertIn('ref: "heads/main"', text)
        self.assertIn("context.sha !== mainSha", text)
        self.assertIn('workflow_id: "ci.yml"', text)
        self.assertIn('event: "push"', text)
        self.assertIn("head_sha: mainSha", text)
        self.assertIn('run.conclusion === "success"', text)
        self.assertIn("CI success is required", text)

    def test_workflow_uses_minimal_permissions_and_shared_mutation_concurrency(self):
        document = self.workflow_document()
        self.assertEqual(document["permissions"], {"actions": "read", "contents": "read"})
        operation_job = document["jobs"]["run-operation"]
        self.assertEqual(operation_job["concurrency"], {
            "group": "deploy-n100-${{ github.ref }}",
            "cancel-in-progress": False,
        })
        text = self.read_workflow()
        self.assertIn("persist-credentials: false", text)

    def test_concurrency_protected_recheck_precedes_wsl_and_rejects_stale_main(self):
        document = self.workflow_document()
        operation_job = document["jobs"]["run-operation"]
        self.assertEqual(operation_job["needs"], "gate-main-ci")
        steps = operation_job["steps"]
        self.assertIn("Recheck current main CI", [step.get("name") for step in steps])
        recheck_index = next(
            index for index, step in enumerate(steps)
            if step.get("name") == "Recheck current main CI"
        )
        invocation_index = next(
            index for index, step in enumerate(steps)
            if step.get("name") == "Run approved N100 operation"
        )
        self.assertEqual(recheck_index + 1, invocation_index)
        recheck = steps[recheck_index]
        self.assertEqual(recheck["uses"], "actions/github-script@v7")
        script = recheck["with"]["script"]
        self.assertIn('ref: "heads/main"', script)
        self.assertIn("context.sha !== mainSha", script)
        self.assertIn("head_sha: mainSha", script)
        self.assertIn('run.conclusion === "success"', script)
        self.assertIn("Main advanced or dispatch SHA is stale", script)

    def test_workflow_runs_checked_out_revision_through_fixed_wsl_entrypoint(self):
        document = self.workflow_document()
        operation_job = document["jobs"]["run-operation"]
        self.assertEqual(operation_job["runs-on"], ["self-hosted", "Windows", "X64"])
        run_step = next(step for step in operation_job["steps"] if step.get("name") == "Run approved N100 operation")
        self.assertEqual(run_step["shell"], "pwsh")
        run_script = run_step["run"]
        self.assertIn("[version]'7.3'", run_script)
        self.assertIn("$PSNativeCommandArgumentPassing = 'Standard'", run_script)
        self.assertIn("$bashArguments = @(", run_script)
        self.assertIn("& \"$env:SystemRoot\\System32\\wsl.exe\" @bashArguments", run_script)
        for argument in ("'bash'", "'--noprofile'", "'--norc'", "'-c'"):
            self.assertIn(argument, run_script)
        self.assertIn('exec "$N100_OPERATIONS_ACTIONS_ROOT/scripts/run-n100-operations.sh" "$N100_OPERATION"', run_script)
        self.assertNotIn("Invoke-Expression", run_script)
        self.assertNotIn("bash -lc", run_script)
        self.assertNotIn("$fixedEntryPoint", run_script)
        self.assertNotIn("inputs.operation", run_script)
        text = self.read_workflow()
        self.assertIn("ref: ${{ needs.gate-main-ci.outputs.main_sha }}", text)
        self.assertIn("N100_OPERATIONS_ACTIONS_ROOT: ${{ github.workspace }}", text)
        self.assertIn("N100_OPERATIONS_DEPLOY_SHA: ${{ needs.gate-main-ci.outputs.main_sha }}", text)
        for forbidden in ("printenv", "docker inspect", "set -x", "-o yaml", "-o json"):
            self.assertNotIn(forbidden, text)


class N100OperationsDocumentationTests(unittest.TestCase):
    DOCUMENTS = (
        ROOT / "docs" / "n100-github-auto-deploy.md",
        ROOT / "docs" / "operations-reference.md",
    )

    @classmethod
    def documentation(cls):
        return "\n".join(path.read_text(encoding="utf-8") for path in cls.DOCUMENTS)

    def test_documents_explain_manual_actions_workflow_and_operation_scope(self):
        documentation = self.documentation()
        for phrase in (
            "Actions → N100 Operations → Run workflow",
            "diagnose",
            "deploy_safe_crawler",
            "verify_news_observability",
            "apply_news_observability",
            "읽기 전용",
            "변경",
            "성공한 main CI",
            "최신 main",
            "SSH",
            "필요하지 않음",
        ):
            self.assertIn(phrase, documentation)

    def test_documents_describe_manual_host_onboarding_without_secret_values(self):
        documentation = self.documentation()
        self.assertIn("수동 호스트 단계", documentation)
        self.assertIn("일회성", documentation)
        self.assertIn("root helper", documentation)
        self.assertIn("generic broad k3s", documentation)
        self.assertIn("세 가지 정확한 root operation", documentation)
        self.assertIn("비밀값", documentation)
        self.assertIn("기록하지 않음", documentation)
        self.assertNotRegex(documentation, r"(?i)(ghp_|github_pat_|AKIA[0-9A-Z]{16})")

    def test_documents_cover_fail_closed_safety_smoke_checklist_and_boundaries(self):
        documentation = self.documentation()
        for phrase in (
            "직전 정상 revision 1회 rollback",
            "Secret 값은 출력하지 않음",
            "실제 N100 smoke checklist",
            "diagnose → verify → 필요한 변경",
            "fail-closed",
            "서버 bootstrap",
            "scheduler",
            "Caddy",
            "Tunnel",
            "Secret",
            "PVC",
            "운영 데이터",
            "실패 사례",
        ):
            self.assertIn(phrase, documentation)

    def test_documents_provide_practical_failure_and_log_pointers(self):
        documentation = self.documentation()
        for phrase in (
            "Actions 로그",
            "Runner가 Offline",
            "helper 권한 부재",
            "main이 변경",
            "N100 smoke",
            "확인 필요",
        ):
            self.assertIn(phrase, documentation)

    def test_documents_separate_deploy_rollback_from_apply_partial_failure(self):
        for path in self.DOCUMENTS:
            with self.subTest(document=path.name):
                documentation = path.read_text(encoding="utf-8")
                self.assertIn(
                    "`deploy_safe_crawler`의 health 검증과 직전 정상 revision 1회 rollback은 기존 안전 배포 도구에만 있음",
                    documentation,
                )
                self.assertIn(
                    "`apply_news_observability`는 제한형 helper가 client dry-run 후 두 리소스를 순서대로 apply함",
                    documentation,
                )
                self.assertIn("helper는 health/undo/자동 rollback을 수행하지 않음", documentation)
                self.assertIn("partial apply 가능", documentation)
                self.assertIn(
                    "`verify_news_observability`와 Kubernetes 리소스 확인 후 운영자 판단",
                    documentation,
                )
                self.assertIn("자동 복구를 약속하지 않음", documentation)
                self.assertNotIn("변경 실패 시 롤백함", documentation)


if __name__ == "__main__":
    unittest.main()
