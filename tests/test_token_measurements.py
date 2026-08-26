import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "summarize_token_measurements.py"


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

    def test_rejects_records_from_different_measurement_conditions(self):
        base = {
            "task_id": "task-001",
            "model": "gpt-5",
            "measurement_group": "harness-v1",
            "prompt_fingerprint": "sha256:abc123",
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
