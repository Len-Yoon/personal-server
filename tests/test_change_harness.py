import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.run_change_harness import REPO_ROOT, run_harness


class ChangeHarnessTests(unittest.TestCase):
    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run the public CLI so argument parsing stays part of the contract."""
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "run_change_harness.py"), *arguments],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_cli_input_error(self, completed: subprocess.CompletedProcess[str]):
        self.assertEqual(completed.returncode, 1)
        evidence = json.loads(completed.stdout)
        self.assertEqual(evidence["work_status"], "input_error")
        self.assertIsNone(evidence["policy_evidence"])

    def test_successful_required_check_is_ready_for_review(self):
        code, evidence = run_harness(
            ["portal-web/app/main.py"], check_results=("portal=success",)
        )

        self.assertEqual(code, 0)
        self.assertEqual(evidence["work_status"], "ready_for_review")
        self.assertEqual(evidence["summary"]["required_checks"], ["portal"])

    def test_risk_and_verification_states_follow_the_policy_priority(self):
        """Changing a state branch must not mark risky work ready for review."""
        cases = [
            (["scripts/deploy-n100.sh"], (), "blocked", 2),
            (["unknown-area/config.toml"], (), "blocked", 2),
            (["portal-web/app/main.py"], (), "verification_incomplete", 2),
            (["portal-web/app/main.py"], ("portal=failure",), "verification_failed", 2),
        ]

        for paths, results, expected_status, expected_code in cases:
            with self.subTest(paths=paths, results=results):
                code, evidence = run_harness(paths, check_results=results)

                self.assertEqual(code, expected_code)
                self.assertEqual(evidence["work_status"], expected_status)
                self.assertTrue(evidence["human_review_required"])

    def test_blocked_status_retains_failed_check_reason(self):
        """A blocked-path branch must retain simultaneous failed validation evidence."""
        code, evidence = run_harness(
            ["scripts/deploy-n100.sh", "portal-web/app/main.py"],
            check_results=("portal=failure",),
        )

        self.assertEqual(code, 2)
        self.assertEqual(evidence["work_status"], "blocked")
        self.assertEqual(
            evidence["human_review_reasons"], ["blocked_files", "failed_checks"],
        )

    def test_unknown_check_name_is_an_input_error(self):
        """Removing check-name validation must not permit arbitrary verification claims."""
        code, evidence = run_harness(
            ["portal-web/app/main.py"], check_results=("unapproved=success",)
        )

        self.assertEqual(code, 1)
        self.assertEqual(evidence["work_status"], "input_error")
        self.assertIsNone(evidence["policy_evidence"])
        self.assertTrue(evidence["human_review_required"])

    def test_unknown_check_result_is_an_input_error(self):
        """Accepting a non-terminal result would allow an unverifiable check claim."""
        code, evidence = run_harness(
            ["portal-web/app/main.py"], check_results=("portal=skipped",)
        )

        self.assertEqual(code, 1)
        self.assertEqual(evidence["work_status"], "input_error")
        self.assertIsNone(evidence["policy_evidence"])

    def test_cli_missing_input_returns_input_error_evidence(self):
        """Removing CLI parser normalization must not expose argparse usage output."""
        self.assert_cli_input_error(self.run_cli())

    def test_cli_invalid_input_format_returns_input_error_evidence(self):
        """Invalid format values must produce the harness error contract, not parser output."""
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "paths.txt"
            input_path.write_text("portal-web/app/main.py\n", encoding="utf-8")
            completed = self.run_cli(
                "--input", str(input_path), "--input-format", "invalid-format"
            )

        self.assert_cli_input_error(completed)

    def test_cli_unreadable_input_returns_input_error_evidence(self):
        """A missing input file must not bypass the JSON input-error contract."""
        with tempfile.TemporaryDirectory() as directory:
            completed = self.run_cli("--input", str(Path(directory) / "missing.txt"))

        self.assert_cli_input_error(completed)

    def test_cli_agent_context_summarizes_incomplete_portal_verification(self):
        """Removing the prompt-safe summary must not expose raw policy evidence."""
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "paths.txt"
            input_path.write_text("portal-web/app/main.py\n", encoding="utf-8")
            completed = self.run_cli("--input", str(input_path), "--agent-context")

        self.assertEqual(completed.returncode, 2)
        self.assertIn("verification_incomplete", completed.stdout)
        self.assertIn("portal", completed.stdout)
        self.assertIn("Next action:", completed.stdout)
        self.assertNotIn("policy_evidence", completed.stdout)

    def test_cli_preserves_rename_space_and_korean_paths_from_nul_input(self):
        """A NUL Git diff must retain both rename paths without path splitting."""
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "changed-files.nul"
            input_path.write_bytes(
                "R100\0docs/old name.md\0docs/한글 경로.md\0"
                "M\0portal-web/templates/with space.html\0".encode("utf-8")
            )
            completed = self.run_cli(
                "--input",
                str(input_path),
                "--input-format",
                "git-name-status-z",
                "--check-result",
                "portal=success",
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        evidence = json.loads(completed.stdout)
        self.assertEqual(evidence["work_status"], "ready_for_review")
        self.assertEqual(
            evidence["policy_evidence"]["changed_files"],
            [
                "docs/old name.md",
                "docs/한글 경로.md",
                "portal-web/templates/with space.html",
            ],
        )

    def test_incomplete_status_retains_missing_and_failed_reasons(self):
        """Dropping either simultaneous reason would hide required follow-up work."""
        code, evidence = run_harness(
            ["portal-web/app/main.py", "system-agent/app/main.py"],
            check_results=("portal=failure",),
        )

        self.assertEqual(code, 2)
        self.assertEqual(evidence["work_status"], "verification_incomplete")
        self.assertEqual(
            evidence["human_review_reasons"], ["missing_checks", "failed_checks"],
        )

    def test_blocked_status_retains_missing_and_failed_reasons(self):
        """A blocked branch must retain every simultaneous verification reason."""
        code, evidence = run_harness(
            [
                "scripts/deploy-n100.sh",
                "portal-web/app/main.py",
                "system-agent/app/main.py",
            ],
            check_results=("portal=failure",),
        )

        self.assertEqual(code, 2)
        self.assertEqual(evidence["work_status"], "blocked")
        self.assertEqual(
            evidence["human_review_reasons"],
            ["blocked_files", "missing_checks", "failed_checks"],
        )


if __name__ == "__main__":
    unittest.main()
