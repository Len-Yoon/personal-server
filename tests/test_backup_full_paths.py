"""Exercise backup evidence error paths without operational backup resources."""
import importlib.util
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_validate_backup_evidence import VALIDATOR, valid_evidence


class BackupFullPathsTests(unittest.TestCase):
    def test_non_utf8_evidence_fails_closed_without_traceback_or_file_change(self):
        validator = self.load_validator()
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "binary.evidence"
            original = b"scope=portal\n\xff\xfe"
            evidence.write_bytes(original)
            stdout, stderr = StringIO(), StringIO()
            with patch("sys.argv", [str(VALIDATOR), "--evidence", str(evidence)]), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(validator.main(), 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "backup_evidence=FAIL\n")
            self.assertEqual(evidence.read_bytes(), original)

    def load_validator(self):
        # Load during test execution, so the import side of the CLI guard is
        # measured as well as the subprocess entry point in the existing tests.
        spec = importlib.util.spec_from_file_location("backup_full_paths_validator", VALIDATOR)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_unreadable_or_incomplete_evidence_is_rejected_without_writing(self):
        validator = self.load_validator()
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.evidence"
            with self.assertRaisesRegex(validator.EvidenceError, "cannot be read"):
                validator.parse_evidence(missing)
            self.assertFalse(missing.exists())
            evidence = Path(directory) / "invalid.evidence"
            for content in ("scope=portal\n", "schema_version\n", "scope=\n",
                            "=portal\n", " scope=portal\n", "scope=portal \n"):
                with self.subTest(content=content):
                    evidence.write_text(content, encoding="utf-8")
                    with self.assertRaises(validator.EvidenceError):
                        validator.parse_evidence(evidence)
                    self.assertEqual(evidence.read_text(encoding="utf-8"), content)

    def test_invalid_optional_proofs_are_rejected_individually(self):
        validator = self.load_validator()
        now = datetime(2026, 8, 30, tzinfo=timezone.utc)
        for changes, message in (
            ({"source_digest": "sha256:" + "A" * 64}, "source_digest"),
            ({"restore_check": "unchecked"}, "restore_check"),
            ({"restore_path_check": "failed"}, "restore_path_check"),
        ):
            with self.subTest(changes=changes):
                values = dict(line.split("=", 1) for line in valid_evidence(**changes).splitlines())
                with self.assertRaisesRegex(validator.EvidenceError, message):
                    validator.validate_evidence(values, now, 86400)

    def test_negative_max_age_fails_before_reading_evidence(self):
        validator = self.load_validator()
        stderr, stdout = StringIO(), StringIO()
        with patch("sys.argv", [str(VALIDATOR), "--evidence", "absent", "--max-age-seconds", "-1"]), \
                patch.object(validator, "parse_evidence") as read, \
                redirect_stderr(stderr), redirect_stdout(stdout):
            self.assertEqual(validator.main(), 1)
        read.assert_not_called()
        self.assertEqual(stderr.getvalue(), "backup_evidence=FAIL\n")
        self.assertEqual(stdout.getvalue(), "")

    def test_default_clock_and_zero_age_boundary_accept_current_evidence(self):
        validator = self.load_validator()
        now = datetime.now(timezone.utc).replace(microsecond=0)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                self.assertEqual(tz, timezone.utc)
                return now

        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "current.evidence"
            timestamp = now.strftime(validator.TIMESTAMP_FORMAT)
            content = valid_evidence(
                backup_completed_at=timestamp, restore_verified_at=timestamp,
                evidence_expires_at=(now + timedelta(seconds=1)).strftime(validator.TIMESTAMP_FORMAT),
            )
            evidence.write_text(content, encoding="utf-8")
            stdout, stderr = StringIO(), StringIO()
            with patch("sys.argv", [str(VALIDATOR), "--evidence", str(evidence), "--max-age-seconds", "0"]), \
                    patch.object(validator, "datetime", Clock), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(validator.main(), 0)
            self.assertEqual(stdout.getvalue(), "backup_evidence=PASS\n")
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(evidence.read_text(encoding="utf-8"), content)
