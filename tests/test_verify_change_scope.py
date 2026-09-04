import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]


def run_scope(
    *paths: str,
    test_result: str | None = None,
    executed_checks: tuple[str, ...] | None = None,
) -> tuple[int, dict]:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write("\n".join(paths))
        input_path = Path(handle.name)
    try:
        command = [
            sys.executable,
            "scripts/verify_change_scope.py",
            "--input",
            str(input_path),
        ]
        if test_result is not None:
            command.extend(["--test-result", test_result])
        if executed_checks is not None:
            command.append("--executed-checks")
            command.extend(executed_checks)
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        evidence = json.loads(completed.stdout) if completed.stdout else {}
        return completed.returncode, evidence
    finally:
        input_path.unlink()


def run_git_name_status(
    contents: bytes,
    *,
    executed_checks: tuple[str, ...] | None = None,
) -> tuple[int, dict]:
    with tempfile.NamedTemporaryFile("wb", delete=False) as handle:
        handle.write(contents)
        input_path = Path(handle.name)
    try:
        command = [
            sys.executable,
            "scripts/verify_change_scope.py",
            "--input",
            str(input_path),
            "--input-format",
            "git-name-status-z",
        ]
        if executed_checks is not None:
            command.append("--executed-checks")
            command.extend(executed_checks)
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        evidence = json.loads(completed.stdout) if completed.stdout else {}
        return completed.returncode, evidence
    finally:
        input_path.unlink()


def run_command(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/verify_change_scope.py", *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


class VerifyChangeScopeTests(unittest.TestCase):
    def test_service_changes_require_matching_checks(self):
        expected = {
            "portal-web/app/main.py": "portal",
            "system-agent/app/main.py": "system-agent",
            "crawler-worker/app/main.py": "crawler-worker",
            "homeops-executor/app/main.py": "homeops-executor",
            "youtube-memo/app/main.py": "youtube-memo",
            "book-memo/app/main.py": "book-memo",
            "car-care-worker/app/main.py": "car-care-worker",
        }
        for path, service in expected.items():
            with self.subTest(path=path):
                code, evidence = run_scope(path)
                self.assertEqual(code, 0)
                self.assertEqual(evidence["services"], [service])
                self.assertEqual(evidence["required_checks"], [service])

    def test_documentation_change_needs_no_service_test(self):
        code, evidence = run_scope(
            "README.md",
            "AGENTS.md",
            "CLAUDE.md",
            "docs/codex-work-loop.md",
            "docs/agent-loop-evidence.md",
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            evidence["documentation_files"],
            [
                "README.md",
                "AGENTS.md",
                "CLAUDE.md",
                "docs/codex-work-loop.md",
                "docs/agent-loop-evidence.md",
            ],
        )
        self.assertEqual(evidence["required_checks"], [])

    def test_sdd_report_is_documentation(self):
        path = ".superpowers/sdd/kst-display-unification/verification-report.md"

        code, evidence = run_scope(path)

        self.assertEqual(code, 0)
        self.assertEqual(evidence["documentation_files"], [path])
        self.assertEqual(evidence["unclassified_files"], [])
        self.assertEqual(evidence["required_checks"], [])

    def test_non_report_sdd_file_blocks_review(self):
        path = ".superpowers/sdd/kst-display-unification/verification-notes.md"

        code, evidence = run_scope(path)

        self.assertEqual(code, 2)
        self.assertEqual(evidence["documentation_files"], [])
        self.assertEqual(evidence["unclassified_files"], [path])

    def test_missing_input_is_an_input_error(self):
        completed = run_command()
        self.assertEqual(completed.returncode, 1)

    def test_unreadable_input_is_an_input_error(self):
        completed = run_command("--input", "/path/that/does/not/exist")
        self.assertEqual(completed.returncode, 1)

    def test_automation_changes_require_maintenance_check(self):
        paths = (
            ".github/workflows/ci.yml",
            "tests/test_verify_change_scope.py",
        )
        code, evidence = run_scope(*paths)
        self.assertEqual(code, 0)
        self.assertEqual(evidence["automation_files"], list(paths))
        self.assertEqual(evidence["required_checks"], ["maintenance"])
        self.assertEqual(evidence["unclassified_files"], [])

    def test_gitops_draft_infrastructure_paths_require_maintenance_check(self):
        paths = (
            "infra/k8s/README.md",
            "infra/k8s/clusters/n100/apps/transition-scope.yaml.tmpl",
        )

        code, evidence = run_scope(*paths)

        self.assertEqual(code, 0)
        self.assertEqual(evidence["infrastructure_files"], list(paths))
        self.assertEqual(evidence["required_checks"], ["maintenance"])
        self.assertEqual(evidence["unclassified_files"], [])

    def test_sre_telegram_relay_paths_require_only_maintenance_check(self):
        paths = (
            "sre-telegram-relay/app/main.py",
            "sre-telegram-relay/Dockerfile",
        )

        code, evidence = run_scope(*paths, executed_checks=("maintenance",))

        self.assertEqual(code, 0)
        self.assertEqual(evidence["services"], [])
        self.assertEqual(evidence["infrastructure_files"], list(paths))
        self.assertEqual(evidence["required_checks"], ["maintenance"])
        self.assertEqual(evidence["unclassified_files"], [])

    def test_sre_telegram_relay_scope_rejects_traversal_and_near_miss_paths(self):
        paths = (
            "sre-telegram-relay/../scripts/maintenance.py",
            "sre-telegram-relay-extra/app/main.py",
        )

        code, evidence = run_scope(*paths, executed_checks=("maintenance",))

        self.assertEqual(code, 2)
        self.assertEqual(evidence["infrastructure_files"], [])
        self.assertEqual(evidence["blocked_files"], [])
        self.assertEqual(evidence["unclassified_files"], list(paths))

    def test_blocked_paths_remain_blocked_alongside_gitops_drafts(self):
        paths = (
            "infra/k8s/README.md",
            "scripts/maintenance.py",
        )

        code, evidence = run_scope(*paths, executed_checks=("maintenance",))

        self.assertEqual(code, 2)
        self.assertEqual(evidence["infrastructure_files"], [paths[0]])
        self.assertEqual(evidence["blocked_files"], [paths[1]])
        self.assertEqual(evidence["unclassified_files"], [])

    def test_path_traversal_cannot_enter_gitops_infrastructure_scope(self):
        path = "infra/k8s/../scripts/maintenance.py"

        code, evidence = run_scope(path, executed_checks=("maintenance",))

        self.assertEqual(code, 2)
        self.assertEqual(evidence["infrastructure_files"], [])
        self.assertEqual(evidence["blocked_files"], [])
        self.assertEqual(evidence["unclassified_files"], [path])

    def test_agent_loop_policy_files_are_maintainable_with_maintenance_check(self):
        paths = (
            "scripts/verify_change_scope.py",
            "tests/test_verify_change_scope.py",
        )

        code, evidence = run_scope(*paths, executed_checks=("maintenance",))

        self.assertEqual(code, 0)
        self.assertEqual(evidence["automation_files"], list(paths))
        self.assertEqual(evidence["blocked_files"], [])
        self.assertEqual(evidence["required_checks"], ["maintenance"])

    def test_change_harness_is_maintenance_policy_file(self):
        code, evidence = run_scope(
            "scripts/run_change_harness.py", executed_checks=("maintenance",)
        )

        self.assertEqual(code, 0)
        self.assertEqual(evidence["automation_files"], ["scripts/run_change_harness.py"])
        self.assertEqual(evidence["required_checks"], ["maintenance"])
        self.assertEqual(evidence["blocked_files"], [])

    def test_runtime_service_state_parser_is_maintenance_policy_file(self):
        code, evidence = run_scope(
            "scripts/runtime-service-state.sh", executed_checks=("maintenance",)
        )
        self.assertEqual(code, 2)
        self.assertEqual(evidence["automation_files"], ["scripts/runtime-service-state.sh"])
        self.assertEqual(
            evidence["required_checks"],
            ["maintenance", "crawler-worker", "youtube-memo", "book-memo"],
        )
        self.assertEqual(
            evidence["missing_checks"], ["crawler-worker", "youtube-memo", "book-memo"]
        )
        self.assertEqual(evidence["blocked_files"], [])

    def test_runtime_service_state_parser_accepts_all_required_service_checks(self):
        code, evidence = run_scope(
            "scripts/runtime-service-state.sh",
            executed_checks=("maintenance", "crawler-worker", "youtube-memo", "book-memo"),
        )
        self.assertEqual(code, 0)
        self.assertEqual(evidence["missing_checks"], [])

    def test_approved_caddy_runtime_files_require_full_validation(self):
        paths = ("docker-compose.n100.yml", "caddy/Caddyfile")
        code, evidence = run_scope(*paths, executed_checks=("maintenance",))
        self.assertEqual(code, 2)
        self.assertEqual(evidence["blocked_files"], [])
        self.assertEqual(evidence["automation_files"], list(paths))
        self.assertEqual(evidence["missing_checks"], [
            "portal", "system-agent", "crawler-worker", "homeops-executor",
            "youtube-memo", "book-memo", "car-care-worker",
        ])

    def test_approved_portal_cutover_runtime_files_require_full_validation(self):
        paths = (
            "docker-compose.yml",
            "docker-compose.portal-bridge.yml",
            "scripts/deploy-n100.sh",
            "scripts/windows-bootstrap.sh",
            "scripts/verify-n100-deployment-health.sh",
        )
        code, evidence = run_scope(*paths, executed_checks=("maintenance",))

        self.assertEqual(code, 2)
        self.assertEqual(evidence["blocked_files"], [])
        self.assertEqual(evidence["automation_files"], list(paths))
        self.assertEqual(evidence["missing_checks"], [
            "portal",
            "system-agent",
            "crawler-worker",
            "homeops-executor",
            "youtube-memo",
            "book-memo",
            "car-care-worker",
        ])

    def test_server_startup_paths_are_blocked(self):
        paths = (
            "scripts/maintenance.py",
        )
        code, evidence = run_scope(*paths)
        self.assertEqual(code, 2)
        self.assertEqual(evidence["blocked_files"], list(paths))
        self.assertEqual(evidence["infrastructure_files"], [])
        self.assertEqual(evidence["required_checks"], [])

    def test_blocked_changes_stop_review(self):
        for path in (
            "scripts/maintenance.py",
            "crawler-worker/app/services/news_scheduler.py",
        ):
            with self.subTest(path=path):
                code, evidence = run_scope(path)
                self.assertEqual(code, 2)
                self.assertEqual(evidence["blocked_files"], [path])

    def test_deployment_workflow_requires_maintenance_and_all_service_checks(self):
        path = ".github/workflows/deploy-n100.yml"

        incomplete_checks = ("maintenance",)
        code, evidence = run_scope(path, executed_checks=incomplete_checks)

        self.assertEqual(code, 2)
        self.assertEqual(evidence["blocked_files"], [])
        self.assertEqual(evidence["automation_files"], [path])
        self.assertEqual(
            evidence["required_checks"],
            [
                "maintenance",
                "portal",
                "system-agent",
                "crawler-worker",
                "homeops-executor",
                "youtube-memo",
                "book-memo",
                "car-care-worker",
            ],
        )
        self.assertEqual(
            evidence["missing_checks"],
            [
                "portal",
                "system-agent",
                "crawler-worker",
                "homeops-executor",
                "youtube-memo",
                "book-memo",
                "car-care-worker",
            ],
        )

        code, evidence = run_scope(
            path,
            executed_checks=(
                "maintenance",
                "portal",
                "system-agent",
                "crawler-worker",
                "homeops-executor",
                "youtube-memo",
                "book-memo",
                "car-care-worker",
            ),
        )

        self.assertEqual(code, 0)
        self.assertEqual(evidence["missing_checks"], [])

    def test_unknown_change_blocks_review(self):
        code, evidence = run_scope("unknown-area/config.toml")
        self.assertEqual(code, 2)
        self.assertEqual(evidence["unclassified_files"], ["unknown-area/config.toml"])

    def test_duplicate_paths_preserve_first_input_order(self):
        code, evidence = run_scope(
            "README.md", "portal-web/app/main.py", "README.md", "portal-web/app/main.py"
        )
        self.assertEqual(code, 0)
        self.assertEqual(evidence["changed_files"], ["README.md", "portal-web/app/main.py"])
        self.assertEqual(evidence["documentation_files"], ["README.md"])
        self.assertEqual(evidence["services"], ["portal"])

    def test_test_result_is_included(self):
        code, evidence = run_scope("README.md", test_result="success")
        self.assertEqual(code, 0)
        self.assertEqual(evidence["test_result"], "success")

    def test_missing_required_checks_fail_policy_validation(self):
        code, evidence = run_scope(
            "portal-web/app/main.py",
            ".github/workflows/ci.yml",
            executed_checks=("portal",),
        )
        self.assertEqual(code, 2)
        self.assertEqual(evidence["executed_checks"], ["portal"])
        self.assertEqual(evidence["missing_checks"], ["maintenance"])

    def test_executed_checks_satisfy_policy_validation(self):
        code, evidence = run_scope(
            "portal-web/app/main.py",
            ".github/workflows/ci.yml",
            executed_checks=("portal", "maintenance"),
        )
        self.assertEqual(code, 0)
        self.assertEqual(evidence["missing_checks"], [])

    def test_nul_separated_git_rename_includes_old_and_new_paths_verbatim(self):
        old_path = "docs/old name.md"
        new_path = "tests/new\t이름.py"
        contents = f"R100\0{old_path}\0{new_path}\0".encode()

        code, evidence = run_git_name_status(
            contents,
            executed_checks=("maintenance",),
        )

        self.assertEqual(code, 0)
        self.assertEqual(evidence["changed_files"], [old_path, new_path])
        self.assertEqual(evidence["documentation_files"], [old_path])
        self.assertEqual(evidence["automation_files"], [new_path])

    def test_malformed_nul_separated_git_input_is_an_input_error(self):
        completed = run_command(
            "--input",
            __file__,
            "--input-format",
            "git-name-status-z",
        )
        self.assertEqual(completed.returncode, 1)


if __name__ == "__main__":
    unittest.main()
