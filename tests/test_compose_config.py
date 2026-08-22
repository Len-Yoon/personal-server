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
                "      - HYUNDAI_ACCESS_TOKEN=${HYUNDAI_ACCESS_TOKEN:-}",
                "      - HYUNDAI_VEHICLE_ID=${HYUNDAI_VEHICLE_ID:-}",
                "      - HYUNDAI_API_URL=${HYUNDAI_API_URL:-}",
            ],
        )
        self.assertNotIn("\n    ports:", worker)
        self.assertIsNotNone(volumes)
        self.assertEqual(volumes.group("items").splitlines(), ["      - ./data/car-care:/data/car-care"])

    def test_n100_car_care_worker_remains_read_only_without_ports(self):
        compose = (ROOT / "docker-compose.n100.yml").read_text(encoding="utf-8")
        worker = _service_block(compose, "car-care-worker")

        self.assertIn("read_only: true", worker)
        self.assertNotIn("\n    ports:", worker)

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
        self.assertIn("portal system-agent crawler-worker homeops-executor youtube-memo book-memo maintenance", workflow)
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
        self.assertIn("portal system-agent crawler-worker homeops-executor youtube-memo book-memo maintenance", workflow)
        self.assertIn("Missing checks", workflow)

        expected_matrix_entries = {
            "portal": "python3 -m unittest tests.test_file_access tests.test_portal_dashboard tests.test_portal_security tests.test_homeops tests.test_homeops_notifier",
            "system-agent": "python3 -m unittest tests.system_agent.test_metrics",
            "crawler-worker": "python3 -m unittest tests.crawler_worker.test_datetime_format tests.crawler_worker.test_investing_news_rss tests.crawler_worker.test_news_service tests.crawler_worker.test_news_routes tests.crawler_worker.test_rss_news",
            "homeops-executor": "python3 -m unittest tests.homeops_executor.test_docker_ops",
            "youtube-memo": "python3 -m unittest tests.youtube_memo.test_video_titles",
            "book-memo": "python3 -m unittest tests.book_memo.test_book_service",
            "maintenance": "python3 -m unittest tests.test_compose_config tests.test_maintenance tests.test_windows_bootstrap tests.test_deploy_n100",
        }
        for service_name, test_command in expected_matrix_entries.items():
            self.assertIn(f"- name: {service_name}", workflow)
            self.assertIn(f"test_command: {test_command}", workflow)

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
