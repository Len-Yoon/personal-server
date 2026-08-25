import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "change_harness"
HARNESS = REPO_ROOT / "scripts" / "run_change_harness.py"


class ChangeHarnessEvaluationTests(unittest.TestCase):
    def test_representative_fixtures_match_cli_contract(self):
        fixtures = sorted(FIXTURES.glob("*.json"))
        self.assertEqual([fixture.stem for fixture in fixtures], [
            "blocked_path",
            "failed_check",
            "incomplete_check",
            "ready_documentation",
            "ready_service",
            "unclassified_path",
        ])

        for fixture_path in fixtures:
            with self.subTest(fixture=fixture_path.name):
                fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as handle:
                    handle.write("\n".join(fixture["paths"]))
                    handle.flush()
                    command = [
                        sys.executable,
                        str(HARNESS),
                        "--input",
                        handle.name,
                    ]
                    for result in fixture["check_results"]:
                        command.extend(["--check-result", result])
                    completed = subprocess.run(
                        command,
                        cwd=REPO_ROOT,
                        text=True,
                        capture_output=True,
                        check=False,
                    )

                evidence = json.loads(completed.stdout)
                self.assertEqual(completed.returncode, fixture["expected_exit_code"])
                self.assertEqual(evidence["work_status"], fixture["expected_status"])
                self.assertEqual(
                    evidence["human_review_required"],
                    fixture["expected_human_review_required"],
                )
                for field, expected in fixture["expected_summary"].items():
                    self.assertEqual(evidence["summary"][field], expected, field)


if __name__ == "__main__":
    unittest.main()
