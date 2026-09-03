import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "infra/k8s/tools/validate-backup-evidence.py"
NOW = "2026-08-30T00:00:00Z"


def valid_evidence(**overrides: str) -> str:
    values = {
        "schema_version": "1",
        "scope": "portal",
        "backup_status": "success",
        "encrypted": "true",
        "backup_completed_at": "2026-08-29T23:30:00Z",
        "restore_status": "success",
        "restore_verified_at": "2026-08-29T23:45:00Z",
        "evidence_expires_at": "2026-08-30T01:00:00Z",
        "backup_id": "portal-20260829.1",
        "source_runtime": "compose-local",
    }
    values.update(overrides)
    return "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"


class ValidateBackupEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(VALIDATOR.is_file(), "backup evidence validator must exist")

    def run_validator(self, content: str, *extra: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "portal.evidence"
            evidence.write_text(content, encoding="utf-8")
            return subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR),
                    "--evidence",
                    str(evidence),
                    "--now",
                    NOW,
                    "--max-age-seconds",
                    "86400",
                    *extra,
                ],
                capture_output=True,
                text=True,
                check=False,
            )

    def test_valid_required_and_optional_evidence_passes(self):
        result = self.run_validator(
            valid_evidence(
                artifact_digest="sha256:" + "a" * 64,
                restore_check="sqlite_quick_check",
                restore_path_check="success",
            )
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("backup_evidence=PASS", result.stdout)

    def test_valid_source_digest_is_accepted(self):
        result = self.run_validator(valid_evidence(source_digest="sha256:" + "b" * 64))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_evidence_without_source_runtime(self):
        content = valid_evidence().replace("source_runtime=compose-local\n", "")
        self.assertNotEqual(self.run_validator(content).returncode, 0)

    def test_accepts_only_known_source_runtime_values(self):
        self.assertEqual(
            self.run_validator(valid_evidence(source_runtime="compose-local")).returncode,
            0,
        )
        self.assertEqual(
            self.run_validator(valid_evidence(source_runtime="k3s-pvc")).returncode,
            0,
        )
        self.assertNotEqual(
            self.run_validator(valid_evidence(source_runtime="unknown")).returncode,
            0,
        )

    def test_rejects_unencrypted_or_unsuccessful_restore(self):
        for changes in (
            {"encrypted": "false"},
            {"restore_status": "failed"},
            {"backup_status": "failed"},
        ):
            with self.subTest(changes=changes):
                self.assertNotEqual(self.run_validator(valid_evidence(**changes)).returncode, 0)

    def test_rejects_stale_and_future_times(self):
        stale = valid_evidence(
            backup_completed_at="2026-08-28T00:00:00Z",
            restore_verified_at="2026-08-28T00:00:00Z",
        )
        future_backup = valid_evidence(backup_completed_at="2026-08-30T00:00:01Z")
        future_restore = valid_evidence(restore_verified_at="2026-08-30T00:00:01Z")
        expired = valid_evidence(evidence_expires_at="2026-08-30T00:00:00Z")
        for content in (stale, future_backup, future_restore, expired):
            with self.subTest(content=content):
                self.assertNotEqual(self.run_validator(content).returncode, 0)

    def test_rejects_restore_evidence_that_predates_its_backup(self):
        """A restore result cannot prove the archive created later was restored."""
        result = self.run_validator(
            valid_evidence(
                backup_completed_at="2026-08-29T23:45:00Z",
                restore_verified_at="2026-08-29T23:30:00Z",
            )
        )
        self.assertNotEqual(result.returncode, 0)

    def test_rejects_duplicate_unknown_blank_and_comment_lines(self):
        cases = (
            valid_evidence() + "scope=portal\n",
            valid_evidence() + "extra=value\n",
            valid_evidence() + "\n",
            valid_evidence() + "# no comments\n",
        )
        for content in cases:
            with self.subTest(content=content):
                self.assertNotEqual(self.run_validator(content).returncode, 0)

    def test_rejects_invalid_timestamp_scope_and_backup_identifier(self):
        cases = (
            valid_evidence(backup_completed_at="2026-08-29T23:30:00+09:00"),
            valid_evidence(scope="all-services"),
            valid_evidence(backup_id="portal backup id"),
            valid_evidence(artifact_digest="sha256:" + "A" * 64),
        )
        for content in cases:
            with self.subTest(content=content):
                self.assertNotEqual(self.run_validator(content).returncode, 0)


if __name__ == "__main__":
    unittest.main()
