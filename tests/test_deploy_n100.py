import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "deploy-n100.yml").read_text(encoding="utf-8")
CI_WORKFLOW = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
SCRIPT = (ROOT / "scripts" / "deploy-n100.sh").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
HANDOFF = (ROOT / "docs" / "agent-handoff.md").read_text(encoding="utf-8")
GUIDE = (ROOT / "docs" / "n100-github-auto-deploy.md").read_text(encoding="utf-8") if (ROOT / "docs" / "n100-github-auto-deploy.md").exists() else ""
HOMEOPS_REQUIREMENTS = (ROOT / "homeops-executor" / "requirements.txt").read_text(encoding="utf-8")
CRAWLER_REQUIREMENTS = (ROOT / "crawler-worker" / "requirements.txt").read_text(encoding="utf-8")


class DeployN100Tests(unittest.TestCase):
    def test_workflow_waits_for_successful_main_ci_and_uses_n100_runner(self):
        self.assertIn("workflow_run:", WORKFLOW)
        self.assertIn("workflows: [CI]", WORKFLOW)
        self.assertIn("types: [completed]", WORKFLOW)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", WORKFLOW)
        self.assertIn("github.event.workflow_run.head_branch == 'main'", WORKFLOW)
        self.assertIn("runs-on: [self-hosted, Windows, X64]", WORKFLOW)
        self.assertIn("C:\\personal-server", WORKFLOW)
        self.assertIn("wsl.exe -d Ubuntu-24.04 -- bash -lc", WORKFLOW)
        self.assertIn("shell: cmd", WORKFLOW)
        self.assertNotIn("shell: powershell", WORKFLOW)
        self.assertNotIn("shell: pwsh", WORKFLOW)
        self.assertIn("bash ./scripts/deploy-n100.sh", WORKFLOW)
        self.assertNotIn("N100_SSH_KEY", WORKFLOW)

    def test_ci_covers_homeops_news_routes_and_deploy_script(self):
        self.assertIn("tests.test_homeops tests.test_homeops_notifier", CI_WORKFLOW)
        self.assertIn("tests.homeops_executor.test_docker_ops", CI_WORKFLOW)
        self.assertIn("tests.crawler_worker.test_news_routes", CI_WORKFLOW)
        self.assertIn("tests.test_deploy_n100", CI_WORKFLOW)

    def test_homeops_executor_test_client_dependency_is_pinned(self):
        self.assertIn("httpx==0.28.1", HOMEOPS_REQUIREMENTS)

    def test_crawler_worker_test_client_dependency_is_pinned(self):
        self.assertIn("httpx==0.28.1", CRAWLER_REQUIREMENTS)

    def test_deploy_workflow_checks_services_after_deployment(self):
        health_check = WORKFLOW.split("- name: Verify deployed service health", maxsplit=1)[1]
        self.assertIn("Verify deployed service health", WORKFLOW)
        self.assertIn("docker compose -f docker-compose.yml -f docker-compose.n100.yml ps", WORKFLOW)
        self.assertIn("http://127.0.0.1:8000/health", WORKFLOW)
        self.assertIn("http://127.0.0.1:18010/health", WORKFLOW)
        self.assertIn("http://127.0.0.1:8001/health", WORKFLOW)
        self.assertIn("http://127.0.0.1:8002/health", WORKFLOW)
        self.assertIn("http://127.0.0.1:8003/health", WORKFLOW)
        self.assertIn("homeops-executor", WORKFLOW)
        self.assertIn("--retry-all", health_check)
        self.assertIn("--retry-connrefused", health_check)
        self.assertNotIn("$service", health_check)
        self.assertNotIn("$url", health_check)
        self.assertIn("for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18", health_check)
        self.assertIn("sleep 5", health_check)
        for service in (
            "portal-web",
            "system-agent",
            "crawler-worker",
            "youtube-memo",
            "book-memo",
            "caddy",
            "homeops-executor",
        ):
            self.assertIn(f"grep -Fx -- {service}", health_check)

    def test_workflows_limit_token_permissions_and_serialize_deployments(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

        self.assertIn("permissions:\n  contents: read", ci)
        self.assertIn("concurrency:", WORKFLOW)
        self.assertIn("group: deploy-n100-${{ github.ref }}", WORKFLOW)
        self.assertIn("cancel-in-progress: false", WORKFLOW)

    def test_deploy_workflow_validates_local_n100_directory(self):
        self.assertIn("Verify N100 deployment directory", WORKFLOW)
        self.assertIn("if not exist", WORKFLOW)

    def test_deploy_script_resets_and_restarts_compose_stack(self):
        self.assertNotIn(b"\r\n", (ROOT / "scripts" / "deploy-n100.sh").read_bytes())
        self.assertIn('test -d "$PROJECT_ROOT/.git"', SCRIPT)
        self.assertIn('test -f "$PROJECT_ROOT/.env"', SCRIPT)
        self.assertIn('test -d "$PROJECT_ROOT/data"', SCRIPT)
        self.assertIn("docker compose -f docker-compose.yml -f docker-compose.n100.yml config", SCRIPT)
        self.assertIn("git fetch --prune origin", SCRIPT)
        self.assertIn("git reset --hard origin/main", SCRIPT)
        self.assertIn("wait_for_docker", SCRIPT)
        self.assertIn("docker info", SCRIPT)
        self.assertIn("DOCKER_WAIT_ATTEMPTS", SCRIPT)
        self.assertIn(
            "docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build portal-web homeops-executor system-agent crawler-worker youtube-memo book-memo caddy",
            SCRIPT,
        )
        self.assertNotIn(
            "docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build\n",
            SCRIPT,
        )
        self.assertIn("docker compose -f docker-compose.yml -f docker-compose.n100.yml ps", SCRIPT)

    def test_documentation_mentions_auto_deploy_flow(self):
        self.assertIn("docs/n100-github-auto-deploy.md", README)
        self.assertIn("main", README)
        self.assertIn("self-hosted", GUIDE)
        self.assertIn("runs-on: [self-hosted, Windows, X64]", GUIDE)
        self.assertNotIn("N100_SSH_HOST", GUIDE)
        self.assertIn("git fetch --prune origin", GUIDE)
        self.assertIn(
            "docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build portal-web homeops-executor system-agent crawler-worker youtube-memo book-memo caddy",
            GUIDE,
        )
        self.assertIn("CI가 성공", HANDOFF)
        self.assertIn("CI가 성공", GUIDE)
        self.assertIn("main", GUIDE)
        self.assertIn("health", GUIDE)
        self.assertNotIn("main push에만 반응", GUIDE)
        self.assertIn("직접 push", GUIDE)
        self.assertIn("PR은 선택", GUIDE)


if __name__ == "__main__":
    unittest.main()
