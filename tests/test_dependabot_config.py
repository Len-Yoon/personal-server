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
TOP_LEVEL_UPDATES_HEADER = re.compile(r"(?m)^version: 2\nupdates:\n")


def find_update_entries(content: str) -> list[str]:
    """Return Dependabot entries only when they follow the required top-level header."""
    header = TOP_LEVEL_UPDATES_HEADER.search(content)
    if header is None:
        return []
    return re.findall(
        r"(?ms)^  - package-ecosystem: [^\n]+.*?(?=^  - package-ecosystem:|\Z)",
        content[header.end() :],
    )


class DependabotConfigContractTests(unittest.TestCase):
    def test_updates_only_approved_dependencies_on_monday_without_automation_commands(self):
        """Fails if Dependabot can update an unapproved path or trigger merge/deploy actions."""
        self.assertTrue(CONFIG_PATH.exists(), "Dependabot configuration must exist")
        content = CONFIG_PATH.read_text(encoding="utf-8")

        self.assertRegex(content, r"(?m)^version: 2$")
        self.assertRegex(content, TOP_LEVEL_UPDATES_HEADER)
        entries = find_update_entries(content)
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

    def test_ignores_entries_when_updates_is_not_directly_after_version(self):
        """Fails if a non-Dependabot top-level structure is accepted as a valid update list."""
        broken_content = (
            'version: 2\nmetadata: ignored\nupdates:\n'
            '  - package-ecosystem: "github-actions"\n'
        )

        self.assertEqual(find_update_entries(broken_content), [])


if __name__ == "__main__":
    unittest.main()
