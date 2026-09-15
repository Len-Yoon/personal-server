import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / ".github" / "dependabot.yml"
EXPECTED_DOCKER_DIRECTORIES = {
    "/book-memo",
    "/caddy",
    "/car-care-worker",
    "/crawler-worker",
    "/homeops-executor",
    "/portal-web",
    "/sre-telegram-relay",
    "/system-agent",
    "/youtube-memo",
}
EXPECTED_PIP_DIRECTORIES = {
    "/book-memo",
    "/car-care-worker",
    "/crawler-worker",
    "/homeops-executor",
    "/portal-web",
    "/system-agent",
    "/youtube-memo",
}
TOP_LEVEL_UPDATES_HEADER = re.compile(r"(?m)^version: 2\nupdates:\n")
REGULAR_MAJOR_UPDATE_IGNORE_BLOCK = (
    '    ignore:\n'
    '      - dependency-name: "*"\n'
    '        update-types: ["version-update:semver-major"]\n'
)
ENTRY_CONTRACT = re.compile(
    r'(?ms)^  - package-ecosystem: "[^"]+"\n'
    r'    directory: "[^"]+"\n'
    r'    schedule:\n'
    r'      interval: "weekly"\n'
    r'      day: "monday"\n'
    r'    open-pull-requests-limit: 5\n'
    r'    ignore:\n'
    r'      - dependency-name: "\*"\n'
    r'        update-types: \["version-update:semver-major"\]\n'
    r'    labels:\n'
    r'      - "dependencies"\n'
    r'      - "security"\n?\Z'
)


def find_update_entries(content: str) -> list[str]:
    """Return entries only for a single top-level Dependabot updates block."""
    header = TOP_LEVEL_UPDATES_HEADER.search(content)
    if header is None or header.start() != 0:
        return []
    updates_lines = []
    for line in content[header.end() :].splitlines(keepends=True):
        if not line.strip() or line.startswith("  "):
            updates_lines.append(line)
            continue
        return []
    return re.findall(
        r"(?ms)^  - package-ecosystem: [^\n]+.*?(?=^  - package-ecosystem:|\Z)",
        "".join(updates_lines),
    )


class DependabotConfigContractTests(unittest.TestCase):
    def test_updates_only_approved_dependencies_on_monday_without_automation_commands(self):
        """Fails if Dependabot can update an unapproved path or trigger merge/deploy actions."""
        self.assertTrue(CONFIG_PATH.exists(), "Dependabot configuration must exist")
        content = CONFIG_PATH.read_text(encoding="utf-8")

        self.assertRegex(content, r"(?m)^version: 2$")
        self.assertRegex(content, TOP_LEVEL_UPDATES_HEADER)
        entries = find_update_entries(content)
        self.assertEqual(len(entries), 17)

        package_directories = []
        for entry in entries:
            self.assertRegex(entry, ENTRY_CONTRACT)
            ecosystem = re.search(r'(?m)^  - package-ecosystem: "([^"]+)"$', entry)
            directory = re.search(r'(?m)^    directory: "([^"]+)"$', entry)
            self.assertIsNotNone(ecosystem)
            self.assertIsNotNone(directory)
            package_directories.append((ecosystem.group(1), directory.group(1)))

        self.assertEqual(package_directories.count(("github-actions", "/")), 1)
        self.assertEqual(
            {directory for ecosystem, directory in package_directories if ecosystem == "docker"},
            EXPECTED_DOCKER_DIRECTORIES,
        )
        self.assertEqual(
            {ecosystem for ecosystem, _ in package_directories},
            {"github-actions", "docker", "pip"},
        )
        self.assertEqual(
            {directory for ecosystem, directory in package_directories if ecosystem == "pip"},
            EXPECTED_PIP_DIRECTORIES,
        )

        lowered = content.casefold()
        for forbidden_command in ("auto-merge", "automerge", "gh pr merge", "deploy"):
            self.assertNotIn(forbidden_command, lowered)

    def test_ignores_entries_when_updates_is_not_directly_after_version(self):
        """Fails if a non-Dependabot top-level structure is accepted as a valid update list."""
        broken_content = (
            'version: 2\nmetadata: ignored\nupdates:\n'
            '  - package-ecosystem: "github-actions"\n'
        )

        self.assertEqual(find_update_entries(broken_content), [])

    def test_ignores_indented_entries_under_metadata_inside_updates(self):
        """Fails if entries nested under metadata are accepted as direct Dependabot updates."""
        broken_content = (
            'version: 2\nupdates:\nmetadata:\n'
            '  - package-ecosystem: "github-actions"\n'
        )

        self.assertEqual(find_update_entries(broken_content), [])

    def test_rejects_a_top_level_key_after_updates(self):
        """Fails if additional top-level content bypasses the approved update contract."""
        content = (
            'version: 2\nupdates:\n'
            '  - package-ecosystem: "github-actions"\n'
            'metadata:\n'
            '  - package-ecosystem: "docker"\n'
        )

        self.assertEqual(find_update_entries(content), [])

    def test_stops_at_a_top_level_key_with_an_anchor_value(self):
        """Fails if entries below a valued top-level key leak into the updates block."""
        content = (
            "version: 2\nupdates:\nmetadata: &ignored\n"
            + ('  - package-ecosystem: "docker"\n' * 9)
        )

        self.assertEqual(find_update_entries(content), [])

    def test_rejects_a_second_updates_block_with_an_unapproved_daily_target(self):
        """Fails if another top-level block bypasses the approved update targets."""
        first_updates = ('  - package-ecosystem: "docker"\n' * 9)
        content = (
            "version: 2\nupdates:\n"
            + first_updates
            + "updates:\n"
            + '  - package-ecosystem: "docker"\n'
            + '    directory: "/"\n'
            + "    schedule:\n"
            + '      interval: "daily"\n'
        )

        self.assertEqual(find_update_entries(content), [])

    def test_rejects_metadata_in_place_of_schedule(self):
        entry = '  - package-ecosystem: "docker"\n    directory: "/x"\n    metadata:\n      interval: "weekly"\n      day: "monday"\n    open-pull-requests-limit: 5\n' + REGULAR_MAJOR_UPDATE_IGNORE_BLOCK + '    labels:\n      - "dependencies"\n      - "security"\n'
        self.assertIsNone(ENTRY_CONTRACT.fullmatch(entry))

    def test_rejects_unexpected_label(self):
        entry = '  - package-ecosystem: "docker"\n    directory: "/x"\n    schedule:\n      interval: "weekly"\n      day: "monday"\n    open-pull-requests-limit: 5\n' + REGULAR_MAJOR_UPDATE_IGNORE_BLOCK + '    labels:\n      - "dependencies"\n      - "security"\n      - "unexpected"\n'
        self.assertIsNone(ENTRY_CONTRACT.fullmatch(entry))

    def test_rejects_missing_or_malformed_major_update_ignore_rule(self):
        """Fails if regular version-update major updates stop being ignored."""
        entry_prefix = '  - package-ecosystem: "docker"\n    directory: "/x"\n    schedule:\n      interval: "weekly"\n      day: "monday"\n    open-pull-requests-limit: 5\n'
        entry_suffix = '    labels:\n      - "dependencies"\n      - "security"\n'
        invalid_ignore_blocks = (
            "",
            '    ignore:\n      - dependency-name: "requests"\n        update-types: ["version-update:semver-major"]\n',
            '    ignore:\n      - dependency-name: "*"\n        update-types: ["version-update:semver-minor"]\n',
            '    ignore:\n      dependency-name: "*"\n      update-types: ["version-update:semver-major"]\n',
        )

        for ignore_block in invalid_ignore_blocks:
            with self.subTest(ignore_block=ignore_block):
                self.assertIsNone(ENTRY_CONTRACT.fullmatch(entry_prefix + ignore_block + entry_suffix))


if __name__ == "__main__":
    unittest.main()
