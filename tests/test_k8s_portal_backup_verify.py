import hashlib
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/k8s/tools/portal-backup-verify.sh"


class PortalBackupVerifyContractTest(unittest.TestCase):
    def _run_fake_backup(self, *, source_digest=True, changed=False, runtime_marker=None, dangling_marker=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = root / "files"; state = root / "state"; bin_dir = root / "bin"
            files.mkdir(); state.mkdir(); bin_dir.mkdir()
            (files / "note.txt").write_text("same", encoding="utf-8")
            (state / "homeops.sqlite3").write_text("db", encoding="utf-8")
            (root / "recipient").write_text("recipient", encoding="utf-8")
            (root / "identity").write_text("identity", encoding="utf-8")
            (bin_dir / "docker").write_text(f"#!/bin/sh\ntouch '{root / 'FAKE_DOCKER_CALLED'}'\nexit 0\n", encoding="utf-8")
            (bin_dir / "sqlite3").write_text("#!/bin/sh\ncase \"$2\" in *PRAGMA*) echo ok;; *.backup*) cp \"$1\" \"$(printf '%s' \"$2\" | sed -n \"s/.*backup '\\(.*\\)'/\\1/p\")\";; esac\n", encoding="utf-8")
            (bin_dir / "age").write_text("#!/bin/sh\nif [ \"$1\" = \"-d\" ]; then cp \"$6\" \"$5\"; else cp \"$5\" \"$4\"; fi\n", encoding="utf-8")
            (bin_dir / "rclone").write_text(f"#!/bin/sh\ntouch '{root / 'FAKE_RCLONE_CALLED'}'; exit 42\n", encoding="utf-8")
            for tool in bin_dir.iterdir(): tool.chmod(0o755)
            def digest(path):
                rows = []
                for item in sorted(path.rglob('*')):
                    if item.is_file(): rows.append(hashlib.sha256(item.read_bytes()).hexdigest() + "  ./" + str(item.relative_to(path)))
                return hashlib.sha256(('\n'.join(rows) + '\n').encode()).hexdigest()
            source = "sha256:" + hashlib.sha256((digest(files) + "\n" + digest(state) + "\n").encode()).hexdigest()
            if changed: (files / "note.txt").write_text("changed", encoding="utf-8")
            now = datetime.now(timezone.utc).replace(microsecond=0)
            evidence = root / "evidence"
            values = ["schema_version=1", "scope=portal", "backup_status=success", "encrypted=true", f"backup_completed_at={(now-timedelta(seconds=60)).strftime('%Y-%m-%dT%H:%M:%SZ')}", "restore_status=success", f"restore_verified_at={(now-timedelta(seconds=30)).strftime('%Y-%m-%dT%H:%M:%SZ')}", f"evidence_expires_at={(now+timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')}", "backup_id=portal-test"]
            if source_digest: values.append(f"source_digest={source}")
            evidence.write_text("\n".join(values) + "\n", encoding="utf-8")
            env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "PORTAL_FILES_SOURCE": str(files), "PORTAL_STATE_SOURCE": str(state), "PORTAL_AGE_RECIPIENT": str(root / "recipient"), "PORTAL_AGE_IDENTITY": str(root / "identity"), "PORTAL_BACKUP_EVIDENCE": str(evidence), "PORTAL_BACKUP_REMOTE": "fake:remote"}
            if runtime_marker is not None:
                marker_path = root / "portal-runtime.mode"
                if dangling_marker:
                    marker_path.symlink_to(root / "missing-runtime.mode")
                else:
                    marker_path.write_text(runtime_marker, encoding="utf-8")
                env["PORTAL_RUNTIME_MARKER"] = str(marker_path)
            result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
            return result, (root / "FAKE_RCLONE_CALLED").exists(), (root / "FAKE_DOCKER_CALLED").exists()

    def test_matching_snapshot_evidence_skips_upload(self):
        result, marker, docker_called = self._run_fake_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SKIPPED_UNCHANGED", result.stdout)
        self.assertFalse(marker)

    def test_compose_marker_allows_matching_snapshot_skip(self):
        result, marker, _ = self._run_fake_backup(runtime_marker="compose")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SKIPPED_UNCHANGED", result.stdout)
        self.assertFalse(marker)

    def test_missing_digest_or_changed_source_does_not_skip(self):
        for kwargs in ({"source_digest": False}, {"changed": True}):
            with self.subTest(kwargs=kwargs):
                result, marker, _ = self._run_fake_backup(**kwargs)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("SKIPPED_UNCHANGED", result.stdout)
                self.assertTrue(marker)

    def test_k3s_and_cutover_markers_fail_before_local_backup_tools(self):
        for runtime_marker in ("k3s", "cutover"):
            with self.subTest(runtime_marker=runtime_marker):
                result, rclone_called, docker_called = self._run_fake_backup(runtime_marker=runtime_marker)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("portal_backup_verify=FAIL", result.stderr)
                self.assertIn("local source backup blocked", result.stderr)
                self.assertFalse(rclone_called)
                self.assertFalse(docker_called)

    def test_unknown_runtime_marker_fails_closed_before_local_backup_tools(self):
        for runtime_marker in ("", "unexpected"):
            with self.subTest(runtime_marker=runtime_marker):
                result, rclone_called, docker_called = self._run_fake_backup(runtime_marker=runtime_marker)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("portal_backup_verify=FAIL", result.stderr)
                self.assertIn("unrecognized runtime marker", result.stderr)
                self.assertFalse(rclone_called)
                self.assertFalse(docker_called)

    def test_dangling_runtime_marker_fails_before_local_backup_tools(self):
        result, rclone_called, docker_called = self._run_fake_backup(
            runtime_marker="k3s", dangling_marker=True
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("portal_backup_verify=FAIL", result.stderr)
        self.assertIn("runtime marker is not a readable file", result.stderr)
        self.assertFalse(rclone_called)
        self.assertFalse(docker_called)
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
            'EVIDENCE=${PORTAL_BACKUP_EVIDENCE:-$REPO_ROOT/.portal-backup-verified}',
            'source_digest="sha256:',
            "SKIPPED_UNCHANGED",
            "backup_upload=SKIPPED_UNCHANGED",
            "validate-backup-evidence.py",
        ):
            self.assertIn(required, text)
        self.assertIn("source_digest", text)
        self.assertLess(text.index('PORTAL_PAUSED=0'), text.index('source_digest="sha256:'))
        self.assertLess(text.index('source_digest="sha256:'), text.index('backup_upload=SKIPPED_UNCHANGED'))
        self.assertNotIn("set -x", text)


if __name__ == "__main__":
    unittest.main()
