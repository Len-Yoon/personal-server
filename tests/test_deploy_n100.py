import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "deploy-n100.yml").read_text(encoding="utf-8")
SCRIPT = (ROOT / "scripts" / "deploy-n100.sh").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
HANDOFF = (ROOT / "docs" / "agent-handoff.md").read_text(encoding="utf-8")
GUIDE = (ROOT / "docs" / "n100-github-auto-deploy.md").read_text(encoding="utf-8") if (ROOT / "docs" / "n100-github-auto-deploy.md").exists() else ""


class DeployN100Tests(unittest.TestCase):
    def test_workflow_triggers_on_main_push_and_uses_n100_runner(self):
        self.assertIn("push:", WORKFLOW)
        self.assertIn("branches:", WORKFLOW)
        self.assertIn("- main", WORKFLOW)
        self.assertIn("runs-on: [self-hosted, Windows, X64]", WORKFLOW)
        self.assertIn("C:\\personal-server", WORKFLOW)
        self.assertIn("wsl.exe -d Ubuntu-24.04 -- bash -lc", WORKFLOW)
        self.assertIn("shell: cmd", WORKFLOW)
        self.assertNotIn("shell: powershell", WORKFLOW)
        self.assertNotIn("shell: pwsh", WORKFLOW)
        self.assertIn("bash ./scripts/deploy-n100.sh", WORKFLOW)
        self.assertNotIn("N100_SSH_KEY", WORKFLOW)

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
        self.assertIn('test -d "$PROJECT_ROOT/.git"', SCRIPT)
        self.assertIn('test -f "$PROJECT_ROOT/.env"', SCRIPT)
        self.assertIn('test -d "$PROJECT_ROOT/data"', SCRIPT)
        self.assertIn("docker compose -f docker-compose.yml -f docker-compose.n100.yml config", SCRIPT)
        self.assertIn("git fetch --prune origin", SCRIPT)
        self.assertIn("git reset --hard origin/main", SCRIPT)
        self.assertIn(
            "docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build portal-web system-agent crawler-worker youtube-memo book-memo caddy",
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
            "docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build portal-web system-agent crawler-worker youtube-memo book-memo caddy",
            GUIDE,
        )
        self.assertIn("push", HANDOFF)


if __name__ == "__main__":
    unittest.main()
