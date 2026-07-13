import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "deploy-n100.yml").read_text(encoding="utf-8")
SCRIPT = (ROOT / "scripts" / "deploy-n100.sh").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
HANDOFF = (ROOT / "docs" / "agent-handoff.md").read_text(encoding="utf-8")
GUIDE = (ROOT / "docs" / "n100-github-auto-deploy.md").read_text(encoding="utf-8") if (ROOT / "docs" / "n100-github-auto-deploy.md").exists() else ""


class DeployN100Tests(unittest.TestCase):
    def test_workflow_triggers_on_main_push_and_uses_ssh_secrets(self):
        self.assertIn("push:", WORKFLOW)
        self.assertIn("branches:", WORKFLOW)
        self.assertIn("- main", WORKFLOW)
        self.assertIn("N100_SSH_HOST", WORKFLOW)
        self.assertIn("N100_SSH_USER", WORKFLOW)
        self.assertIn("N100_SSH_KEY", WORKFLOW)
        self.assertIn("N100_SSH_KNOWN_HOSTS", WORKFLOW)
        self.assertIn("N100_APP_DIR", WORKFLOW)
        self.assertIn("bash scripts/deploy-n100.sh", WORKFLOW)

    def test_deploy_script_resets_and_restarts_compose_stack(self):
        self.assertIn("git fetch --prune origin", SCRIPT)
        self.assertIn("git reset --hard origin/main", SCRIPT)
        self.assertIn("docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build", SCRIPT)
        self.assertIn("docker compose -f docker-compose.yml -f docker-compose.n100.yml ps", SCRIPT)

    def test_documentation_mentions_auto_deploy_flow(self):
        self.assertIn("docs/n100-github-auto-deploy.md", README)
        self.assertIn("main", README)
        self.assertIn("N100_SSH_HOST", GUIDE)
        self.assertIn("git fetch --prune origin", GUIDE)
        self.assertIn("docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build", GUIDE)
        self.assertIn("push", HANDOFF)


if __name__ == "__main__":
    unittest.main()
