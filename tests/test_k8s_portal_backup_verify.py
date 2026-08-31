import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/k8s/tools/portal-backup-verify.sh"


class PortalBackupVerifyContractTest(unittest.TestCase):
    def test_backup_tool_requires_encryption_remote_restore_and_atomic_evidence(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for required in (
            "age -R",
            "rclone copyto",
            "age -d -i",
            "sqlite3",
            "quick_check",
            "sha256sum",
            "backup_completed_at=",
            "restore_verified_at=",
            "evidence_expires_at=",
            "chmod 600",
            "mv --",
            "docker pause portal-web",
            "docker unpause portal-web",
            "rclone copyto --immutable",
            "rm -f -- \"$EVIDENCE\"",
            "unsupported filesystem entry",
            'EVIDENCE="$REPO_ROOT/.portal-backup-verified"',
        ):
            self.assertIn(required, text)
        self.assertNotIn("set -x", text)


if __name__ == "__main__":
    unittest.main()
