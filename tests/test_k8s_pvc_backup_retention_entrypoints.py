"""Service retention entry points never touch live PVCs or pause writers."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


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
    def test_automatic_retention_runs_after_plaintext_cleanup_and_publication(self):
        for short_name, _, _ in SERVICES:
            with self.subTest(service=short_name):
                runner = load_runner(short_name)
                events = []
                controller = Mock()
                controller.run_backup.side_effect = lambda: events.append("backup") or "new-proof"
                controller.publish_success.side_effect = lambda proof: events.append(("publish", proof))

                @contextlib.contextmanager
                def scratch(**kwargs):
                    yield "/scratch"
                    events.append("cleanup")

                with patch.object(runner.tempfile, "TemporaryDirectory", scratch), patch.object(
                    runner, ("YouTube" if short_name == "youtube" else short_name.title()) + "BackupController", return_value=controller
                ), patch.object(runner, "run_retention", side_effect=lambda **kw: events.append(("retention", kw))):
                    runner.run_go()
                self.assertEqual(events, ["backup", "cleanup", ("publish", "new-proof"),
                                          ("retention", {"delete": True})])
                controller.publish_failure.assert_not_called()

    def test_automatic_retention_failure_preserves_backup_success(self):
        for short_name, _, _ in SERVICES:
            with self.subTest(service=short_name):
                runner = load_runner(short_name)
                controller = Mock()
                controller.run_backup.return_value = "new-proof"
                stderr, stdout = io.StringIO(), io.StringIO()
                with patch.object(runner.sys, "argv", [runner.__file__, "--go"]), patch.object(
                    runner.tempfile, "TemporaryDirectory", return_value=contextlib.nullcontext("/scratch")
                ), patch.object(runner, ("YouTube" if short_name == "youtube" else short_name.title()) + "BackupController", return_value=controller), patch.object(
                    runner, "run_retention", side_effect=runner.BackupError("sensitive retention failure")
                ), contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
                    self.assertEqual(runner.main(), 1)
                controller.publish_success.assert_called_once_with("new-proof")
                controller.publish_failure.assert_not_called()
                self.assertEqual(stderr.getvalue(), "pvc_backup_retention=FAIL\n")
                self.assertNotIn("=PASS", stdout.getvalue())

    def test_failed_backup_cleanup_or_publication_never_runs_retention(self):
        for short_name, _, _ in SERVICES:
            for failure in ("backup", "cleanup", "publish"):
                with self.subTest(service=short_name, failure=failure):
                    runner = load_runner(short_name)
                    controller = Mock()
                    controller.run_backup.return_value = "new-proof"
                    if failure == "backup":
                        controller.run_backup.side_effect = runner.BackupError("backup failed")
                    if failure == "publish":
                        controller.publish_success.side_effect = runner.BackupError("publish failed")

                    @contextlib.contextmanager
                    def scratch(**kwargs):
                        yield "/scratch"
                        if failure == "cleanup":
                            raise runner.BackupError("cleanup failed")

                    with patch.object(runner.tempfile, "TemporaryDirectory", scratch), patch.object(
                        runner, ("YouTube" if short_name == "youtube" else short_name.title()) + "BackupController", return_value=controller
                    ), patch.object(runner, "run_retention") as retention:
                        with self.assertRaises(runner.BackupError):
                            runner.run_go()
                    retention.assert_not_called()

    def test_failed_retention_releases_only_owned_lock_and_preserves_evidence(self):
        for short_name, _, _ in SERVICES:
            with self.subTest(service=short_name):
                runner = load_runner(short_name)
                state = {"status": "completed", "lock_run_id": "", "run_id": "verified-run",
                         "evidence": f"backup_id={short_name}-verified-run", "stage": "completed"}
                original = dict(state)

                def apply_patch(*args):
                    operations = json.loads(args[-1].split("=", 1)[1])
                    for operation in operations:
                        # Relay consumes completed backup stage throughout retention.
                        self.assertNotEqual(operation["path"], "/data/stage")
                        key = operation["path"].split("/")[-1]
                        if operation["op"] == "test":
                            self.assertEqual(state[key], operation["value"])
                        elif operation["op"] in {"add", "replace"}:
                            state[key] = operation["value"]

                with patch.object(runner, "k8s_json", return_value={"data": state}), patch.object(
                    runner, "kubectl", side_effect=apply_patch
                ), patch.object(runner, "rclone_args", return_value=("rclone",)), patch.object(
                    runner.pvc_backup_retention, "run_retention",
                    side_effect=runner.pvc_backup_retention.RetentionError("failure")
                ):
                    with self.assertRaises(runner.pvc_backup_retention.RetentionError):
                        runner.run_retention(delete=True)
                self.assertEqual(state["stage"], "completed")
                self.assertEqual(state["retention_status"], "failed")
                self.assertTrue(state["retention_completed_at"].endswith("Z"))
                for key in ("status", "lock_run_id", "run_id", "evidence"):
                    self.assertEqual(state[key], original[key])

    def test_retention_failure_is_not_hidden_by_release_failure(self):
        for short_name, _, _ in SERVICES:
            with self.subTest(service=short_name):
                runner = load_runner(short_name)
                state = {"data": {"status": "completed", "lock_run_id": "", "run_id": "verified-run",
                                  "evidence": f"backup_id={short_name}-verified-run"}}
                original = runner.pvc_backup_retention.RetentionError("retention failure")
                release_error = runner.BackupError("lock ownership lost")
                with patch.object(runner, "k8s_json", return_value=state), patch.object(
                    runner, "kubectl", side_effect=["", release_error]
                ), patch.object(runner, "rclone_args", return_value=("rclone",)), patch.object(
                    runner.pvc_backup_retention, "run_retention", side_effect=original
                ):
                    with self.assertRaises(runner.pvc_backup_retention.RetentionError) as raised:
                        runner.run_retention(delete=True)
                self.assertIs(raised.exception, original)
                self.assertIs(raised.exception.__cause__, release_error)

    def test_automatic_retention_lock_conflict_keeps_published_proof(self):
        for short_name, _, _ in SERVICES:
            with self.subTest(service=short_name):
                runner = load_runner(short_name)
                controller = Mock()
                proof = f"backup_id={short_name}-verified-run"
                controller.run_backup.return_value = proof
                state = {"data": {"status": "completed", "lock_run_id": "", "run_id": "verified-run",
                                  "evidence": proof}}
                with patch.object(runner.tempfile, "TemporaryDirectory", return_value=contextlib.nullcontext("/scratch")), patch.object(
                    runner, ("YouTube" if short_name == "youtube" else short_name.title()) + "BackupController", return_value=controller
                ), patch.object(runner, "k8s_json", return_value=state), patch.object(
                    runner, "kubectl", side_effect=runner.BackupError("lock conflict")
                ), patch.object(runner, "rclone_args") as credentials, patch.object(
                    runner.pvc_backup_retention, "run_retention"
                ) as retention:
                    with self.assertRaises(runner.RetentionStageError):
                        runner.run_go()
                controller.publish_success.assert_called_once_with(proof)
                controller.publish_failure.assert_not_called()
                credentials.assert_not_called()
                retention.assert_not_called()
                self.assertEqual(state["data"]["evidence"], proof)

    def test_new_backup_state_clears_previous_retention_result(self):
        for short_name, _, _ in SERVICES:
            with self.subTest(service=short_name):
                runner = load_runner(short_name)
                controller_class = getattr(runner, ("YouTube" if short_name == "youtube" else short_name.title()) + "BackupController")
                controller = controller_class(Path("/scratch"))
                with patch.object(runner, "patch_state") as state_patch:
                    controller._update_state("running", "preflight")
                values = state_patch.call_args.args[0]
                self.assertEqual(values["retention_status"], "")
                self.assertEqual(values["retention_completed_at"], "")

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
                ), patch.object(runner, "kubectl"), patch.object(runner, "rclone_args", return_value=("rclone",)), patch.object(
                    runner.pvc_backup_retention, "run_retention",
                    side_effect=runner.pvc_backup_retention.RetentionError("sensitive remote detail")
                ) as retention, contextlib.redirect_stderr(stderr):
                    self.assertEqual(runner.main(), 1)
                retention.assert_called_once()
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
