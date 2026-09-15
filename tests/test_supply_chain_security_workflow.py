import re
import unittest
from pathlib import Path

WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "trivy-security.yml"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class SupplyChainSecurityWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.trivy_steps = re.findall(
            r"(?ms)^      - name: Scan .*?(?=^      - name:|\Z)", cls.workflow_text
        )

    def test_reports_filesystem_and_configuration_scans_on_push_and_pull_request(self):
        self.assertRegex(self.workflow_text, r"(?m)^  pull_request:\s*$")
        self.assertRegex(self.workflow_text, r"(?m)^  push:\n    branches:\n      - main\s*$")
        self.assertIn("security-scan:", self.workflow_text)
        self.assertEqual(len(self.trivy_steps), 2)
        scan_types = {re.search(r"(?m)^          scan-type: (\w+)$", step).group(1) for step in self.trivy_steps}
        self.assertEqual(scan_types, {"fs", "config"})

    def test_uses_are_full_sha_pinned_with_version_comments(self):
        action_lines = [line.strip() for line in self.workflow_text.splitlines() if "uses:" in line]
        self.assertGreaterEqual(len(action_lines), 2)
        for line in action_lines:
            reference, comment = line.split("#", 1)
            sha = reference.rsplit("@", 1)[1].strip()
            self.assertRegex(sha, SHA_PATTERN)
            self.assertTrue(comment.strip().startswith("v"))
        trivy_lines = [line for line in action_lines if "aquasecurity/trivy-action@" in line]
        self.assertEqual(len(trivy_lines), 2)
        for line in trivy_lines:
            self.assertRegex(line.split("#", 1)[1].strip(), r"^v\d+\.\d+\.\d+$")

    def test_high_and_critical_findings_block_with_read_only_contents_permission(self):
        self.assertRegex(self.workflow_text, r"(?ms)^permissions:\n  contents: read\s*$")
        self.assertTrue(all(re.search(r'(?m)^          severity: CRITICAL,HIGH$', step) for step in self.trivy_steps))
        self.assertTrue(all(re.search(r'(?m)^          exit-code: "1"$', step) for step in self.trivy_steps))
        self.assertTrue(all(re.search(r"(?m)^          format: table$", step) for step in self.trivy_steps))


if __name__ == "__main__":
    unittest.main()
