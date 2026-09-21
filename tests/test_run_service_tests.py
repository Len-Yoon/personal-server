import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from tests import run_service_tests as runner


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "tests" / "ci_test_matrix.json"
RUNNER = ROOT / "tests" / "run_service_tests.py"


EXPECTED_GROUPS = {
    "portal": ("3.11", "portal-web/requirements.txt", [], "portal-web", "python3 -m unittest tests.test_file_access tests.test_portal_dashboard tests.test_portal_security tests.test_homeops tests.test_homeops_notifier tests.test_portal_http_metrics tests.test_portal_k3s_bridge_contract tests.test_portfolio"),
    "system-agent": ("3.12", "system-agent/requirements.txt", [], "system-agent", "python3 -m unittest tests.system_agent.test_metrics"),
    "crawler-worker": ("3.11", "crawler-worker/requirements.txt", [], "crawler-worker", "python3 -m unittest tests.crawler_worker.test_datetime_format tests.crawler_worker.test_investing_news_rss tests.crawler_worker.test_nasdaq_relevance tests.crawler_worker.test_news_collection_metrics tests.crawler_worker.test_news_collection_status tests.crawler_worker.test_news_scheduler tests.crawler_worker.test_news_service tests.crawler_worker.test_news_routes tests.crawler_worker.test_rss_news tests.crawler_worker.test_telegram_notifier"),
    "homeops-executor": ("3.12", "homeops-executor/requirements.txt", [], "homeops-executor", "python3 -m unittest tests.homeops_executor.test_docker_ops"),
    "youtube-memo": ("3.11", "youtube-memo/requirements.txt", ["httpx==0.28.1"], "youtube-memo", "python3 -m unittest tests.youtube_memo.test_ui_contract tests.youtube_memo.test_video_titles"),
    "book-memo": ("3.11", "book-memo/requirements.txt", ["httpx==0.28.1"], "book-memo", "python3 -m unittest tests.book_memo.test_book_service tests.book_memo.test_ui_contract"),
    "car-care-worker": ("3.12", "car-care-worker/requirements.txt", [], "car-care-worker", "python3 -m unittest discover -s tests/car_care_worker"),
    "k8s-contracts": ("3.11", "", ["PyYAML"], ".", "python3 -m unittest tests.test_k8s_monitoring_tools tests.test_k8s_monitoring_values tests.test_k8s_portal_availability_alert tests.test_k8s_portal_backup_verify tests.test_k8s_portal_cutover tests.test_k8s_portal_nodeport_connectivity_smoke tests.test_k8s_portal_pvc_backup_automation tests.test_k8s_portal_pvc_backup_verify tests.test_k8s_portal_secret_shadow_smoke tests.test_k8s_sre_health_audit tests.test_k8s_sre_pod_recovery_lab tests.test_k8s_sre_telegram_manifests tests.test_k8s_sre_telegram_tools tests.test_k8s_transition_runner_artifacts tests.test_k8s_transition_runner_install_tools tests.test_k8s_transition_runner_policy tests.test_k8s_quarterly_sre_audit_cronjob tests.test_k8s_quarterly_sre_audit_automation tests.test_monthly_recovery_drill tests.test_k8s_slo_daily_evidence tests.test_k8s_crawler_worker_migration tests.test_k3s_app_image_build tests.test_k3s_app_upgrade tests.test_k3s_app_image_transfer tests.test_k8s_book_memo_migration tests.test_k8s_grafana_portal_http_dashboard tests.test_k8s_portal_nonroot tests.test_k8s_portal_pvc_backup_cronjob tests.test_k8s_portal_pvc_backup_cronjob_install tests.test_k8s_youtube_memo_migration tests.test_validate_backup_evidence tests.test_validate_sre_alertmanager_config"),
    "maintenance": ("3.11", "", [], ".", "python3 -m unittest tests.test_compose_config tests.test_supply_chain_security_workflow tests.test_documentation_index tests.test_dependabot_config tests.test_verify_change_scope tests.test_maintenance tests.test_windows_bootstrap tests.test_deploy_n100 tests.test_public_uptime_monitor tests.test_change_harness tests.test_change_harness_evals tests.test_token_measurements tests.test_python_multipart_security tests.test_runtime_service_deployment_contract tests.test_caddy_portal_upstream tests.test_n100_remote_dev tests.test_n100_safe_deployment tests.test_run_service_tests tests.test_runtime_service_state tests.test_setup_local_test_venvs tests.test_sre_telegram_relay"),
}


class ServiceTestRunnerTests(unittest.TestCase):
    def test_matrix_coverage_matches_every_repository_test_file_exactly_once(self):
        assigned = runner.validate_matrix_coverage(runner.load_matrix())
        self.assertEqual(assigned, runner.discover_test_files())

    def test_matrix_coverage_rejects_missing_module(self):
        matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
        matrix[0]["test_command"] = matrix[0]["test_command"].replace(
            " tests.test_portal_security", ""
        )
        with self.assertRaisesRegex(ValueError, "tests/test_portal_security.py"):
            runner.validate_matrix_coverage(matrix)

    def test_matrix_coverage_rejects_duplicate_module(self):
        matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
        matrix[1]["test_command"] += " tests.test_file_access"
        with self.assertRaisesRegex(ValueError, "duplicate"):
            runner.validate_matrix_coverage(matrix)

    def test_matrix_coverage_rejects_a_new_unassigned_test_file(self):
        matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
        expected = runner.discover_test_files() | {"tests/test_new_regression.py"}
        with mock.patch.object(runner, "discover_test_files", return_value=expected), \
             self.assertRaisesRegex(ValueError, "tests/test_new_regression.py"):
            runner.validate_matrix_coverage(matrix)

    def test_command_parser_rejects_unknown_module_and_options(self):
        with self.assertRaises(ValueError):
            runner.files_for_test_command(
                "python3 -m unittest tests.test_does_not_exist"
            )
        with self.assertRaises(ValueError):
            runner.files_for_test_command(
                "python3 -m unittest -k tests.test_portfolio"
            )

    def test_discover_parser_rejects_nested_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests_root = root / "tests" / "car_care_worker"
            (tests_root / "nested").mkdir(parents=True)
            (tests_root / "test_ok.py").touch()
            (tests_root / "nested" / "test_hidden.py").touch()
            with self.assertRaisesRegex(ValueError, "nested"):
                runner.files_for_test_command(
                    "python3 -m unittest discover -s tests/car_care_worker",
                    root=root,
                )

    def test_discover_parser_rejects_escape_and_unapproved_target(self):
        with self.assertRaises(ValueError):
            runner.files_for_test_command(
                "python3 -m unittest discover -s tests/../outside"
            )
        with self.assertRaises(ValueError):
            runner.files_for_test_command(
                "python3 -m unittest discover -s tests"
            )

    def test_command_environment_allows_only_fixed_fixture_values(self):
        entry = json.loads(MATRIX.read_text(encoding="utf-8"))[0]
        with mock.patch.dict(
            os.environ,
            {"PATH": "/fixture/path", "USER": "real-user", "TOKEN": "secret"},
            clear=False,
        ):
            _, environment = runner.command_for(entry)
        self.assertEqual(environment["PATH"], "/fixture/path")
        self.assertEqual(environment["USER"], "audit_fixture")
        self.assertNotIn("TOKEN", environment)

    def test_github_matrix_stops_before_output_when_coverage_fails(self):
        stdout = StringIO()
        stderr = StringIO()
        with mock.patch.object(runner, "validate_matrix_coverage", side_effect=ValueError("coverage")), \
             mock.patch.object(sys, "argv", ["run_service_tests.py", "--github-matrix"]), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(runner.main(), 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("coverage", stderr.getvalue())

    def test_coverage_failure_does_not_start_a_suite(self):
        with mock.patch.object(runner, "validate_matrix_coverage", side_effect=ValueError("coverage")), \
             mock.patch.object(runner, "run_suite") as run_suite, \
             mock.patch.object(sys, "argv", ["run_service_tests.py"]):
            self.assertEqual(runner.main(), 2)
        run_suite.assert_not_called()

    def test_pr_matrix_runs_crawler_cutover_and_runtime_caddy_regressions(self):
        groups = {item['name']: item for item in json.loads(MATRIX.read_text())}
        self.assertIn('tests.test_k8s_crawler_worker_migration', groups['k8s-contracts']['test_command'].split())
        for module in ('tests.test_runtime_service_deployment_contract', 'tests.test_caddy_portal_upstream',
                       'tests.test_run_service_tests', 'tests.test_setup_local_test_venvs'):
            self.assertIn(module, groups['maintenance']['test_command'].split())

    def run_runner(self, *arguments, environment=None):
        return subprocess.run([sys.executable, str(RUNNER), *arguments], cwd=ROOT, capture_output=True, text=True, env=environment)

    def test_matrix_defines_the_nine_ci_python_groups(self):
        """Fails if a local group drifts from the CI test contract."""
        matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
        actual = {item["name"]: (item["python_version"], item["requirements"], item["extra_packages"], item["pythonpath"], item["test_command"]) for item in matrix}
        self.assertEqual(actual, EXPECTED_GROUPS)

    def test_github_matrix_is_exactly_the_json_matrix(self):
        """Fails if CI cannot consume the common matrix without a second definition."""
        expected = json.loads(MATRIX.read_text(encoding="utf-8"))
        result = self.run_runner("--github-matrix")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), {"include": expected})

    def test_dry_run_uses_an_isolated_group_command_without_executing_it(self):
        """Fails if inspection starts tests or inherits another service PYTHONPATH."""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = "must-not-be-inherited"
        result = self.run_runner(
            "--dry-run",
            "--suite",
            "system-agent",
            "--venv-root",
            str(ROOT / "missing-local-venvs"),
            environment=environment,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[DRY RUN] system-agent", result.stdout)
        self.assertIn("PYTHONPATH=" + str(ROOT / "system-agent"), result.stdout)
        self.assertNotIn("must-not-be-inherited", result.stdout)
        self.assertIn("python3.12 -m unittest tests.system_agent.test_metrics", result.stdout)
        self.assertNotIn("[PASS]", result.stdout)

    def test_local_virtual_environment_python_is_preferred_when_available(self):
        entry = {
            "name": "system-agent",
            "python_version": "3.12",
            "requirements": "system-agent/requirements.txt",
            "extra_packages": [],
            "pythonpath": "system-agent",
            "test_command": "python3 -m unittest tests.system_agent.test_metrics",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            python_path = Path(temporary_directory) / "system-agent" / "bin" / "python"
            python_path.parent.mkdir(parents=True)
            python_path.touch()
            python_path.chmod(0o755)

            command, _ = runner.command_for(entry, Path(temporary_directory))

        self.assertEqual(command[0], str(python_path))

    def test_unknown_suite_is_rejected(self):
        """Fails if a typo could silently skip all CI-equivalent suites."""
        result = self.run_runner("--suite", "not-a-ci-suite", "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid choice", result.stderr)

    def test_runner_continues_after_a_group_failure_and_returns_failure(self):
        """Fails if one broken Python group hides later group results."""
        matrix = [
            {"name": "first", "python_version": "3.11", "requirements": "", "extra_packages": [], "pythonpath": ".", "test_command": "python3 -m unittest tests.test_run_service_tests"},
            {"name": "second", "python_version": "3.11", "requirements": "", "extra_packages": [], "pythonpath": ".", "test_command": "python3 -m unittest tests.test_run_service_tests"},
        ]
        with mock.patch.object(runner, "load_matrix", return_value=matrix), \
             mock.patch.object(runner, "validate_matrix_coverage", return_value=set()), \
             mock.patch.object(runner, "run_suite", side_effect=[1, 0]) as run_suite, \
             mock.patch.object(sys, "argv", ["run_service_tests.py"]):
            self.assertEqual(runner.main(), 1)
        self.assertEqual([call.args[0]["name"] for call in run_suite.call_args_list], ["first", "second"])


if __name__ == "__main__":
    unittest.main()
