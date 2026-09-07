import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _service_block(compose: str, service_name: str) -> str:
    match = re.search(
        rf"^  {re.escape(service_name)}:\n(?P<body>.*?)(?=^  \S|\Z)",
        compose,
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"Missing service: {service_name}")
    return match.group("body")


class ComposeConfigTests(unittest.TestCase):
    def test_compose_defines_isolated_car_care_worker(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        worker = _service_block(compose, "car-care-worker")
        volumes = re.search(r"^    volumes:\n(?P<items>(?:      - .+\n?)*)", worker, re.MULTILINE)
        environment = re.search(
            r"^    environment:\n(?P<items>(?:      - .+\n?)*)", worker, re.MULTILINE
        )

        self.assertIn("build: ./car-care-worker", worker)
        self.assertIn("restart: unless-stopped", worker)
        self.assertIn("stop_grace_period: 15s", worker)
        self.assertNotIn("env_file:", worker)
        self.assertIsNotNone(environment)
        self.assertEqual(
            environment.group("items").splitlines(),
            [
                "      - CAR_CARE_TELEGRAM_BOT_TOKEN=${CAR_CARE_TELEGRAM_BOT_TOKEN:-}",
                "      - CAR_CARE_TELEGRAM_CHAT_ID=${CAR_CARE_TELEGRAM_CHAT_ID:-}",
                "      - CAR_CARE_DB_PATH=/data/car-care/car-care.sqlite3",
                "      - HYUNDAI_CLIENT_ID=${HYUNDAI_CLIENT_ID:-}",
                "      - HYUNDAI_CLIENT_SECRET=${HYUNDAI_CLIENT_SECRET:-}",
                "      - HYUNDAI_VEHICLE_ID=${HYUNDAI_VEHICLE_ID:-}",
                "      - HYUNDAI_REDIRECT_URI=${HYUNDAI_REDIRECT_URI:-}",
                "      - HYUNDAI_TOKEN_STORE_PATH=/data/oauth/hyundai-token.json",
            ],
        )
        self.assertIn("      - \"127.0.0.1:8015:8015\"", worker)
        self.assertIsNotNone(volumes)
        self.assertEqual(
            volumes.group("items").splitlines(),
            ["      - ./data/car-care:/data/car-care", "      - car-care-oauth:/data/oauth"],
        )

    def test_n100_car_care_worker_remains_read_only_with_loopback_callback_port(self):
        compose = (ROOT / "docker-compose.n100.yml").read_text(encoding="utf-8")
        worker = _service_block(compose, "car-care-worker")

        self.assertIn("read_only: true", worker)
        self.assertIn("      - \"127.0.0.1:8015:8015\"", worker)
        self.assertIn("http://127.0.0.1:8015/health", worker)

    def test_agent_loop_documents_require_branch_cleanup_after_merge(self):
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        work_loop = (ROOT / "docs" / "codex-work-loop.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        for document in (agents, claude, work_loop, readme):
            self.assertIn("브랜치", document)
            self.assertIn("병합", document)

    def test_agent_loop_documents_state_artifact_retention_and_archive_policy(self):
        evidence = (ROOT / "docs" / "agent-loop-evidence.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("90일", evidence)
        self.assertIn("별도 증적 저장소", evidence)
        self.assertIn("90일", readme)
        self.assertIn("장기 보관", readme)

    def test_claude_instructions_reference_the_codex_work_loop(self):
        instructions = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

        self.assertIn("## Codex 작업 완료 루프", instructions)
        self.assertIn("docs/codex-work-loop.md", instructions)
        self.assertIn("## Skill routing", instructions)

    def test_agent_review_workflow_enforces_pull_request_policy(self):
        workflow = (ROOT / ".github" / "workflows" / "agent-review.yml").read_text(encoding="utf-8")

        self.assertIn("pull_request:", workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("git diff --name-status -z --find-renames", workflow)
        self.assertNotIn("git diff --name-only", workflow)
        self.assertIn("--input-format git-name-status-z", workflow)
        self.assertIn("--executed-checks", workflow)
        self.assertIn("portal system-agent crawler-worker homeops-executor youtube-memo book-memo car-care-worker maintenance n100-operations", workflow)
        self.assertIn("agent-review-scope", workflow)
        self.assertIn("policy_status", workflow)

    def test_ci_collects_and_enforces_agent_loop_evidence(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

        self.assertIn("  scope:\n", workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("git diff --name-status -z --find-renames", workflow)
        self.assertIn("git diff-tree --no-commit-id --name-status -z --find-renames", workflow)
        self.assertNotIn("git diff --name-only", workflow)
        self.assertIn("--input-format git-name-status-z", workflow)
        self.assertIn("agent-loop-evidence", workflow)
        self.assertIn("  summary:\n", workflow)
        self.assertIn("needs: [scope, test]", workflow)
        self.assertIn("  test:\n    needs: scope\n    if: always()", workflow)
        self.assertIn("  summary:\n    needs: [scope, test]\n    if: always()", workflow)
        self.assertIn("--test-result \"${{ needs.test.result }}\"", workflow)
        self.assertIn("--executed-checks", workflow)
        self.assertIn("portal system-agent crawler-worker homeops-executor youtube-memo book-memo car-care-worker maintenance k8s-contracts n100-operations", workflow)
        self.assertIn("Missing checks", workflow)
        self.assertIn("scripts/run_change_harness.py", workflow)
        self.assertIn("agent-loop-harness.json", workflow)
        self.assertIn("agent-loop-harness-context.md", workflow)
        self.assertIn("--agent-context", workflow)
        self.assertIn('if [[ "$harness_status" -ne "$context_status" ]]; then', workflow)
        self.assertIn('test "${{ steps.evidence.outputs.harness_status }}" -eq 0', workflow)
        self.assertIn('test "${{ steps.evidence.outputs.context_status }}" -eq 0', workflow)
        self.assertIn('test "${{ needs.test.result }}" = "success"', workflow)

        expected_matrix_entries = {
            "portal": "python3 -m unittest tests.test_file_access tests.test_portal_dashboard tests.test_portal_security tests.test_homeops tests.test_homeops_notifier",
            "system-agent": "python3 -m unittest tests.system_agent.test_metrics",
            "crawler-worker": "python3 -m unittest tests.crawler_worker.test_datetime_format tests.crawler_worker.test_investing_news_rss tests.crawler_worker.test_news_service tests.crawler_worker.test_news_routes tests.crawler_worker.test_rss_news",
            "homeops-executor": "python3 -m unittest tests.homeops_executor.test_docker_ops",
            "youtube-memo": "python3 -m unittest tests.youtube_memo.test_video_titles",
            "book-memo": "python3 -m unittest tests.book_memo.test_book_service",
            "car-care-worker": "python3 -m unittest discover -s tests/car_care_worker",
            "k8s-contracts": "python3 -m unittest tests.test_k8s_monitoring_tools tests.test_k8s_monitoring_values tests.test_k8s_portal_availability_alert tests.test_k8s_portal_backup_verify tests.test_k8s_portal_cutover tests.test_k8s_portal_nodeport_connectivity_smoke tests.test_k8s_portal_pvc_backup_automation tests.test_k8s_portal_pvc_backup_verify tests.test_k8s_portal_secret_shadow_smoke tests.test_k8s_sre_health_audit tests.test_k8s_sre_pod_recovery_lab tests.test_k8s_sre_telegram_manifests tests.test_k8s_sre_telegram_tools tests.test_k8s_transition_runner_artifacts tests.test_k8s_transition_runner_install_tools tests.test_k8s_transition_runner_policy",
            "maintenance": "python3 -m unittest tests.test_compose_config tests.test_documentation_index tests.test_verify_change_scope tests.test_maintenance tests.test_windows_bootstrap tests.test_deploy_n100 tests.test_public_uptime_monitor tests.test_change_harness tests.test_change_harness_evals tests.test_token_measurements",
            "n100-operations": "python3 -m unittest tests.test_n100_operations tests.test_n100_operations_installer tests.test_verify_change_scope",
        }
        for service_name, test_command in expected_matrix_entries.items():
            self.assertIn(f"- name: {service_name}", workflow)
            self.assertIn(f"test_command: {test_command}", workflow)
        self.assertEqual(workflow.count("tests.test_documentation_index"), 1)

    def test_ci_has_dedicated_k8s_contract_check(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("- name: k8s-contracts", workflow)
        self.assertIn("Install K3s contract dependencies", workflow)
        self.assertIn("Install N100 operations dependencies", workflow)
        self.assertIn("test_command: python3 -m unittest tests.test_k8s_monitoring_tools", workflow)
        self.assertIn("tests.test_k8s_sre_telegram_tools", workflow)
        maintenance_start = workflow.index("- name: maintenance")
        maintenance_end = workflow.index("\n    steps:")
        self.assertNotIn("tests.test_k8s_", workflow[maintenance_start:maintenance_end])

    def test_ci_matrix_matches_service_docker_python_versions(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        test_job = workflow.split("  test:\n", 1)[1].split("\n  summary:", 1)[0]
        dockerfiles = {
            "portal": ROOT / "portal-web" / "Dockerfile",
            "system-agent": ROOT / "system-agent" / "Dockerfile",
            "crawler-worker": ROOT / "crawler-worker" / "Dockerfile",
            "homeops-executor": ROOT / "homeops-executor" / "Dockerfile",
            "youtube-memo": ROOT / "youtube-memo" / "Dockerfile",
            "book-memo": ROOT / "book-memo" / "Dockerfile",
            "car-care-worker": ROOT / "car-care-worker" / "Dockerfile",
        }
        expected_versions = {
            name: re.search(
                r"^FROM python:(?P<version>\d+\.\d+)-slim$",
                path.read_text(encoding="utf-8"),
                re.MULTILINE,
            ).group("version")
            for name, path in dockerfiles.items()
        }
        expected_versions.update({"k8s-contracts": "3.11", "maintenance": "3.11", "n100-operations": "3.11"})

        for name, expected_version in expected_versions.items():
            entry = re.search(
                rf"(?ms)^          - name: {re.escape(name)}\n(?P<body>.*?)(?=^          - name:|^\n    steps:)",
                test_job,
            )
            self.assertIsNotNone(entry, f"Missing CI matrix entry: {name}")
            self.assertIn(
                f'python_version: "{expected_version}"',
                entry.group("body"),
                f"CI Python version mismatch for {name}",
            )

        setup_python = re.search(
            r"(?ms)^      - name: Set up Python\n(?P<body>.*?)(?=^      - name:|^\n  summary:)",
            test_job,
        )
        self.assertIsNotNone(setup_python)
        self.assertIn("python-version: ${{ matrix.python_version }}", setup_python.group("body"))

    def test_runtime_services_define_healthchecks(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        for port in (8000, 8010, 8001, 8002, 8003):
            self.assertIn("healthcheck:", compose)
            self.assertIn(f"127.0.0.1:{port}", compose)

    def test_homeops_executor_is_internal_and_owns_docker_socket(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("homeops-executor:", compose)
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock", compose)
        self.assertNotIn("ports:\n      - \"8011:8011\"", compose)

    def test_caddy_waits_for_runtime_services_to_be_healthy(self):
        compose = (ROOT / "docker-compose.n100.yml").read_text(encoding="utf-8")
        self.assertIn("condition: service_healthy", compose)
        self.assertIn("portal-web:", compose)
        self.assertIn("crawler-worker:", compose)


if __name__ == "__main__":
    unittest.main()
