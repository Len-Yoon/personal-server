import re
import unittest
from pathlib import Path

import yaml


WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "trivy-security.yml"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class SupplyChainSecurityWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.workflow = yaml.safe_load(cls.workflow_text)
        cls.events = cls.workflow.get("on", cls.workflow.get(True, {}))

    def test_reports_filesystem_and_configuration_scans_on_push_and_pull_request(self):
        self.assertIn("pull_request", self.events)
        self.assertEqual(self.events["push"]["branches"], ["main"])
        steps = self.workflow["jobs"]["security-scan"]["steps"]
        actions = [step for step in steps if "uses" in step]
        trivy_steps = [step for step in actions if step["uses"].startswith("aquasecurity/trivy-action@")]
        self.assertEqual(len(trivy_steps), 2)
        scan_types = {step["with"]["scan-type"] for step in trivy_steps}
        self.assertEqual(scan_types, {"fs", "config"})

    def test_uses_are_full_sha_pinned_with_version_comments(self):
        action_lines = [line.strip() for line in self.workflow_text.splitlines() if "uses:" in line]
        self.assertGreaterEqual(len(action_lines), 2)
        for line in action_lines:
            reference = line.split("#", 1)[0]
            sha = reference.rsplit("@", 1)[1].strip()
            self.assertRegex(sha, SHA_PATTERN)
        trivy_lines = [line for line in action_lines if "aquasecurity/trivy-action@" in line]
        self.assertEqual(len(trivy_lines), 2)
        for line in trivy_lines:
            self.assertRegex(line.split("#", 1)[1].strip(), r"^v\d+\.\d+\.\d+$")

    def test_scan_is_report_only_and_has_read_only_contents_permission(self):
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        steps = self.workflow["jobs"]["security-scan"]["steps"]
        trivy_steps = [step for step in steps if "aquasecurity/trivy-action@" in step.get("uses", "")]
        self.assertEqual({step["with"]["exit-code"] for step in trivy_steps}, {"0"})
        self.assertTrue(all("format" in step["with"] for step in trivy_steps))


if __name__ == "__main__":
    unittest.main()
