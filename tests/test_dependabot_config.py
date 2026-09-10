import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / ".github" / "dependabot.yml"
EXPECTED_DOCKER_DIRECTORIES = {
    "/book-memo",
    "/car-care-worker",
    "/crawler-worker",
    "/homeops-executor",
    "/portal-web",
    "/sre-telegram-relay",
    "/system-agent",
    "/youtube-memo",
}


class DependabotConfigContractTests(unittest.TestCase):
    def test_updates_only_approved_dependencies_on_monday_without_automation_commands(self):
        """Fails if Dependabot can update an unapproved path or trigger merge/deploy actions."""
        self.assertTrue(CONFIG_PATH.exists(), "Dependabot configuration must exist")
        content = CONFIG_PATH.read_text(encoding="utf-8")

        self.assertRegex(content, r"(?m)^version: 2$")
        entries = re.findall(
            r"(?ms)^  - package-ecosystem: [^\n]+.*?(?=^  - package-ecosystem:|\Z)",
            content,
        )
        self.assertEqual(len(entries), 9)

        package_directories = []
        for entry in entries:
            ecosystem = re.search(r'(?m)^  - package-ecosystem: "([^"]+)"$', entry)
            directory = re.search(r'(?m)^    directory: "([^"]+)"$', entry)
            self.assertIsNotNone(ecosystem)
            self.assertIsNotNone(directory)
            self.assertRegex(entry, r'(?m)^      interval: "weekly"$')
            self.assertRegex(entry, r'(?m)^      day: "monday"$')
            self.assertRegex(entry, r"(?m)^    open-pull-requests-limit: 5$")
            self.assertRegex(
                entry,
                r'(?m)^    labels:\n      - "dependencies"\n      - "security"$',
            )
            package_directories.append((ecosystem.group(1), directory.group(1)))

        self.assertEqual(package_directories.count(("github-actions", "/")), 1)
        self.assertEqual(
            {directory for ecosystem, directory in package_directories if ecosystem == "docker"},
            EXPECTED_DOCKER_DIRECTORIES,
        )
        self.assertEqual(
            {ecosystem for ecosystem, _ in package_directories},
            {"github-actions", "docker"},
        )

        lowered = content.casefold()
        self.assertNotIn("caddy", lowered)
        for forbidden_command in ("auto-merge", "automerge", "gh pr merge", "deploy"):
            self.assertNotIn(forbidden_command, lowered)


if __name__ == "__main__":
    unittest.main()
