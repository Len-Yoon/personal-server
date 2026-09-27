"""Service retention entry points never touch live PVCs or pause writers."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "infra/k8s/tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

SERVICES = (
    ("book", "book-memo", "book-pvc-backup-state"),
    ("youtube", "youtube-memo", "youtube-pvc-backup-state"),
    ("crawler", "crawler-worker", "crawler-pvc-backup-state"),
)


def load_runner(short_name: str):
    path = TOOLS / f"{short_name}-pvc-backup-verify.py"
    spec = importlib.util.spec_from_file_location(f"{short_name}_retention_entrypoint", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RetentionEntrypointTests(unittest.TestCase):
    def test_preview_and_go_use_only_own_completed_unlocked_evidence(self):
        for short_name, service, state_name in SERVICES:
            with self.subTest(service=service):
                runner = load_runner(short_name)
                state = {"data": {"status": "completed", "lock_run_id": "", "run_id": "verified-run", "evidence": f"backup_id={short_name}-verified-run"}}
                for mode, deletion in (("--prune-preview", False), ("--prune-go", True)):
                    with self.subTest(mode=mode), patch.object(
                        runner.sys, "argv", [str(runner.__file__), mode]
                    ), patch.object(runner, "k8s_json", return_value=state) as k8s, patch.object(
                        runner, "rclone_args", return_value=("rclone", "--config", "/secret/config")
                    ), patch.object(runner, "kubectl") as kubectl, patch.object(
                        runner, "workload_state") as workload, patch.object(
                        runner, "run_go"
                    ) as backup, patch.object(
                        runner.pvc_backup_retention, "run_retention", return_value=[]
                    ) as retention, contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(runner.main(), 0)
                    k8s.assert_called_once_with("get", "configmap", state_name)
                    workload.assert_not_called()
                    backup.assert_not_called()
                    self.assertEqual(kubectl.call_count, 2 if deletion else 0)
                    if deletion:
                        acquire = json.loads(kubectl.call_args_list[0].args[-1].split("=", 1)[1])
                        release = json.loads(kubectl.call_args_list[1].args[-1].split("=", 1)[1])
                        self.assertIn({"op": "test", "path": "/data/status", "value": "completed"}, acquire)
                        self.assertIn({"op": "test", "path": "/data/evidence", "value": state["data"]["evidence"]}, acquire)
                        self.assertEqual(acquire[0], {"op": "test", "path": "/data/lock_run_id", "value": ""})
                        self.assertEqual(release[0]["path"], "/data/lock_run_id")
                    self.assertEqual(retention.call_args.args[:3], (
                        service, state["data"]["evidence"], ("rclone", "--config", "/secret/config")
                    ))
                    self.assertIs(retention.call_args.kwargs["delete"], deletion)

    def test_locked_or_failed_backup_refuses_retention(self):
        for short_name, _, _ in SERVICES:
            with self.subTest(service=short_name):
                runner = load_runner(short_name)
                for state in (
                    {"data": {"status": "running", "lock_run_id": "active", "evidence": "old"}},
                    {"data": {"status": "failed", "lock_run_id": "", "evidence": "old"}},
                ):
                    with patch.object(runner.sys, "argv", [str(runner.__file__), "--prune-go"]), patch.object(
                        runner, "k8s_json", return_value=state
                    ), patch.object(runner, "rclone_args") as credentials, patch.object(
                        runner.pvc_backup_retention, "run_retention"
                    ) as retention, contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(runner.main(), 1)
                    credentials.assert_not_called()
                    retention.assert_not_called()


    def test_remote_validation_failure_returns_fixed_failure_status(self):
        for short_name, _, _ in SERVICES:
            with self.subTest(service=short_name):
                runner = load_runner(short_name)
                state = {"data": {"status": "completed", "lock_run_id": "", "run_id": "verified-run", "evidence": f"backup_id={short_name}-verified-run"}}
                stderr = io.StringIO()
                with patch.object(runner.sys, "argv", [str(runner.__file__), "--prune-go"]), patch.object(
                    runner, "k8s_json", return_value=state
                ), patch.object(runner, "rclone_args", return_value=("rclone",)), patch.object(
                    runner.pvc_backup_retention, "run_retention",
                    side_effect=runner.pvc_backup_retention.RetentionError("sensitive remote detail")
                ), contextlib.redirect_stderr(stderr):
                    self.assertEqual(runner.main(), 1)
                self.assertNotIn("sensitive remote detail", stderr.getvalue())



    def test_retention_lock_conflict_prevents_remote_deletion(self):
        for short_name, _, _ in SERVICES:
            with self.subTest(service=short_name):
                runner = load_runner(short_name)
                state = {"data": {"status": "completed", "lock_run_id": "", "run_id": "verified-run", "evidence": f"backup_id={short_name}-verified-run"}}
                with patch.object(runner.sys, "argv", [str(runner.__file__), "--prune-go"]), patch.object(
                    runner, "k8s_json", return_value=state
                ), patch.object(runner, "kubectl", side_effect=runner.BackupError("lock conflict")), patch.object(
                    runner, "rclone_args"
                ) as credentials, patch.object(runner.pvc_backup_retention, "run_retention") as retention, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(runner.main(), 1)
                credentials.assert_not_called()
                retention.assert_not_called()



    def test_mismatched_state_run_id_refuses_retention(self):
        for short_name, _, _ in SERVICES:
            with self.subTest(service=short_name):
                runner = load_runner(short_name)
                state = {"data": {"status": "completed", "lock_run_id": "",
                                  "run_id": "new-run", "evidence": f"backup_id={short_name}-old-run"}}
                with patch.object(runner.sys, "argv", [str(runner.__file__), "--prune-go"]), patch.object(
                    runner, "k8s_json", return_value=state
                ), patch.object(runner, "kubectl") as kubectl, patch.object(
                    runner.pvc_backup_retention, "run_retention"
                ) as retention, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(runner.main(), 1)
                kubectl.assert_not_called()
                retention.assert_not_called()



if __name__ == "__main__":
    unittest.main()
