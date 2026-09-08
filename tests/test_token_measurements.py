import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "summarize_token_measurements.py"
RECORD_SCRIPT = ROOT / "scripts" / "record_token_measurement.py"
RECORD_FINGERPRINT = "sha256:" + "a" * 64


class TokenMeasurementTests(unittest.TestCase):
    def run_cli(self, contents: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "measurements.jsonl"
            input_path.write_text(contents, encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(SCRIPT), "--input", str(input_path)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_summarizes_valid_utc_measurements(self):
        completed = self.run_cli(
            "\n".join(
                [
                    json.dumps({
                        "task_id": "task-001",
                        "model": "gpt-5",
                        "measurement_group": "harness-v1",
                        "prompt_fingerprint": "sha256:abc123",
                        "recorded_at": "2026-08-26T01:00:00+00:00",
                        "baseline_input_tokens": 100,
                        "baseline_output_tokens": 20,
                        "harness_input_tokens": 50,
                        "harness_output_tokens": 10,
                    }),
                    json.dumps({
                        "task_id": "task-001",
                        "model": "gpt-5",
                        "measurement_group": "harness-v1",
                        "prompt_fingerprint": "sha256:abc123",
                        "recorded_at": "2026-08-26T02:00:00Z",
                        "baseline_input_tokens": 80,
                        "baseline_output_tokens": 20,
                        "harness_input_tokens": 70,
                        "harness_output_tokens": 10,
                    }),
                ]
            )
            + "\n"
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "measurement_count": 2,
                "baseline_total_tokens": 220,
                "harness_total_tokens": 140,
                "saved_tokens": 80,
                "reduction_percent": 36.4,
            },
        )

    def test_rejects_naive_timestamp_and_negative_or_non_integer_tokens(self):
        cases = [
            {"task_id": "task-001", "model": "gpt-5", "measurement_group": "harness-v1", "prompt_fingerprint": "sha256:abc123", "recorded_at": "2026-08-26T01:00:00", "baseline_input_tokens": 1, "baseline_output_tokens": 0, "harness_input_tokens": 1, "harness_output_tokens": 0},
            {"task_id": "task-001", "model": "gpt-5", "measurement_group": "harness-v1", "prompt_fingerprint": "sha256:abc123", "recorded_at": "2026-08-26T01:00:00+00:00", "baseline_input_tokens": -1, "baseline_output_tokens": 0, "harness_input_tokens": 1, "harness_output_tokens": 0},
            {"task_id": "task-001", "model": "gpt-5", "measurement_group": "harness-v1", "prompt_fingerprint": "sha256:abc123", "recorded_at": "2026-08-26T01:00:00+00:00", "baseline_input_tokens": 1.5, "baseline_output_tokens": 0, "harness_input_tokens": 1, "harness_output_tokens": 0},
            {"task_id": "", "model": "gpt-5", "measurement_group": "harness-v1", "prompt_fingerprint": "sha256:abc123", "recorded_at": "2026-08-26T01:00:00+00:00", "baseline_input_tokens": 1, "baseline_output_tokens": 0, "harness_input_tokens": 1, "harness_output_tokens": 0},
            {"task_id": "task-001", "model": "", "measurement_group": "harness-v1", "prompt_fingerprint": "sha256:abc123", "recorded_at": "2026-08-26T01:00:00+00:00", "baseline_input_tokens": 1, "baseline_output_tokens": 0, "harness_input_tokens": 1, "harness_output_tokens": 0},
            {"task_id": "task-001", "model": "gpt-5", "measurement_group": "", "prompt_fingerprint": "sha256:abc123", "recorded_at": "2026-08-26T01:00:00+00:00", "baseline_input_tokens": 1, "baseline_output_tokens": 0, "harness_input_tokens": 1, "harness_output_tokens": 0},
            {"task_id": "task-001", "model": "gpt-5", "measurement_group": "harness-v1", "prompt_fingerprint": "", "recorded_at": "2026-08-26T01:00:00+00:00", "baseline_input_tokens": 1, "baseline_output_tokens": 0, "harness_input_tokens": 1, "harness_output_tokens": 0},
        ]
        for record in cases:
            with self.subTest(record=record):
                completed = self.run_cli(json.dumps(record) + "\n")

                self.assertEqual(completed.returncode, 1)
                self.assertIn("input_error", completed.stderr)

    def test_appends_to_existing_measurement_log(self):
        record = {
            "task_id": "task-001",
            "model": "gpt-5",
            "measurement_group": "harness-v1",
            "prompt_fingerprint": RECORD_FINGERPRINT,
            "recorded_at": "2026-08-26T01:00:00Z",
            "baseline_input_tokens": 100,
            "baseline_output_tokens": 20,
            "harness_input_tokens": 50,
            "harness_output_tokens": 10,
        }
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "measurements.jsonl"
            first = self.run_record_cli(json.dumps(record) + "\n", output_path)
            second = self.run_record_cli(json.dumps(record) + "\n", output_path)

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(len(output_path.read_text(encoding="utf-8").splitlines()), 2)

    def test_documents_local_recording_cli_and_secret_boundary(self):
        evidence = (ROOT / "docs" / "agent-loop-evidence.md").read_text(encoding="utf-8")

        self.assertIn("record_token_measurement.py", evidence)
        self.assertIn("표준 입력", evidence)
        self.assertIn("비밀값", evidence)

    def run_record_cli(self, payload: str, output_path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RECORD_SCRIPT), "--output", str(output_path)],
            cwd=ROOT,
            input=payload,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_records_one_valid_measurement_and_summary_reads_it(self):
        record = {
            "task_id": "task-001",
            "model": "gpt-5",
            "measurement_group": "harness-v1",
            "prompt_fingerprint": RECORD_FINGERPRINT,
            "recorded_at": "2026-08-26T01:00:00Z",
            "baseline_input_tokens": 100,
            "baseline_output_tokens": 20,
            "harness_input_tokens": 50,
            "harness_output_tokens": 10,
            "secret_value": "must-not-be-recorded",
        }
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "measurements.jsonl"
            completed = self.run_record_cli(json.dumps(record) + "\n", output_path)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, "")
            saved = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertNotIn("secret_value", saved)
            self.assertEqual(saved["recorded_at"], record["recorded_at"])
            summary = subprocess.run(
                [sys.executable, str(SCRIPT), "--input", str(output_path)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(summary.returncode, 0, summary.stderr)
            self.assertEqual(json.loads(summary.stdout)["measurement_count"], 1)

    def test_rejects_invalid_input_without_creating_or_changing_output(self):
        record = {
            "task_id": "task-001",
            "model": "gpt-5",
            "measurement_group": "harness-v1",
            "prompt_fingerprint": RECORD_FINGERPRINT,
            "recorded_at": "2026-08-26T01:00:00",
            "baseline_input_tokens": 100,
            "baseline_output_tokens": 20,
            "harness_input_tokens": 50,
            "harness_output_tokens": 10,
        }
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "measurements.jsonl"
            completed = self.run_record_cli(json.dumps(record) + "\n", output_path)

            self.assertEqual(completed.returncode, 1)
            self.assertIn("input_error", completed.stderr)
            self.assertFalse(output_path.exists())

    def test_rejects_prompt_fingerprint_that_is_not_a_sha256_digest(self):
        record = {
            "task_id": "task-001",
            "model": "gpt-5",
            "measurement_group": "harness-v1",
            "prompt_fingerprint": "synthetic-secret-token",
            "recorded_at": "2026-08-26T01:00:00Z",
            "baseline_input_tokens": 100,
            "baseline_output_tokens": 20,
            "harness_input_tokens": 50,
            "harness_output_tokens": 10,
        }
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "measurements.jsonl"
            completed = self.run_record_cli(json.dumps(record) + "\n", output_path)

            self.assertEqual(completed.returncode, 1)
            self.assertIn("prompt_fingerprint", completed.stderr)
            self.assertFalse(output_path.exists())

    def test_rejects_more_than_one_nonempty_json_line(self):
        record = {
            "task_id": "task-001",
            "model": "gpt-5",
            "measurement_group": "harness-v1",
            "prompt_fingerprint": RECORD_FINGERPRINT,
            "recorded_at": "2026-08-26T01:00:00Z",
            "baseline_input_tokens": 100,
            "baseline_output_tokens": 20,
            "harness_input_tokens": 50,
            "harness_output_tokens": 10,
        }
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "measurements.jsonl"
            completed = self.run_record_cli(
                json.dumps(record) + "\n" + json.dumps(record) + "\n", output_path
            )

            self.assertEqual(completed.returncode, 1)
            self.assertIn("one JSON object", completed.stderr)
            self.assertFalse(output_path.exists())

    def test_rejects_records_from_different_measurement_conditions(self):
        base = {
            "task_id": "task-001",
            "model": "gpt-5",
            "measurement_group": "harness-v1",
            "prompt_fingerprint": RECORD_FINGERPRINT,
            "recorded_at": "2026-08-26T01:00:00+00:00",
            "baseline_input_tokens": 10,
            "baseline_output_tokens": 1,
            "harness_input_tokens": 5,
            "harness_output_tokens": 1,
        }
        for field, different_value in (
            ("task_id", "task-002"),
            ("model", "gpt-5-mini"),
            ("measurement_group", "harness-v2"),
            ("prompt_fingerprint", "sha256:def456"),
        ):
            with self.subTest(field=field):
                changed = dict(base)
                changed[field] = different_value
                completed = self.run_cli(
                    "\n".join((json.dumps(base), json.dumps(changed))) + "\n"
                )

                self.assertEqual(completed.returncode, 1)
                self.assertIn("input_error", completed.stderr)


if __name__ == "__main__":
    unittest.main()
