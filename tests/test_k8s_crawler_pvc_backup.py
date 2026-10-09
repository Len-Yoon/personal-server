"""Crawler PVC backup contracts; no production Kubernetes resources are touched."""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "infra/k8s/tools/crawler-pvc-backup-verify.py"
MANIFEST = ROOT / "infra/k8s/backup-automation/crawler-worker-pvc-backup-cronjob.yaml"
STATE_BOOTSTRAP = ROOT / "infra/k8s/backup-automation/crawler-worker-pvc-backup-state-bootstrap.yaml"


def load_runner():
    spec = importlib.util.spec_from_file_location("crawler_pvc_backup_verify", RUNNER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_crawler_state(root: Path) -> None:
    (root / "news_archive.json").write_text(json.dumps({
        "schema_version": "2026-08-20-market-focus-v3",
        "articles": [{"title": "preserved"}],
        "telegram_outbox": [],
    }), encoding="utf-8")
    (root / "news_collection_status.json").write_text(json.dumps({
        "initialized": True, "consecutive_failures": 0,
    }), encoding="utf-8")


class CrawlerBackupDataTests(unittest.TestCase):
    def test_command_timeout_has_safe_distinct_type(self):
        runner = load_runner()
        with patch.object(runner.subprocess, "run", side_effect=subprocess.TimeoutExpired(
            ["rclone", "private-config-path"], 30, stderr=b"private diagnostic"
        )):
            with self.assertRaises(runner.CommandTimeout) as caught:
                runner.run_command("rclone", "lsd", timeout=30)
        self.assertEqual(str(caught.exception), "external command timed out")

    def test_remote_preflight_retries_one_timeout_with_bounded_calls(self):
        runner = load_runner()
        with patch.object(runner, "rclone_args", return_value=("rclone",)), \
                patch.object(runner, "run_command", side_effect=[runner.CommandTimeout("timeout"), ""]) as command, \
                patch.object(runner.time, "sleep") as sleep:
            runner.remote_preflight()
        self.assertEqual(command.call_count, 2)
        self.assertEqual([call.kwargs["timeout"] for call in command.call_args_list], [60, 60])
        sleep.assert_called_once_with(2)

    def test_remote_preflight_does_not_retry_command_error(self):
        runner = load_runner()
        with patch.object(runner, "rclone_args", return_value=("rclone",)), \
                patch.object(runner, "run_command", side_effect=runner.BackupError("private diagnostic")) as command, \
                patch.object(runner.time, "sleep") as sleep:
            with self.assertRaises(runner.RemotePreflightError) as caught:
                runner.remote_preflight()
        self.assertEqual(str(caught.exception), "remote preflight failed")
        command.assert_called_once()
        sleep.assert_not_called()

    def test_snapshot_round_trip_checks_database_and_tree_digest(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            write_crawler_state(source)
            with sqlite3.connect(source / "news_summaries.sqlite3") as db:
                db.execute("create table summaries (title text not null)")
                db.execute("insert into summaries values ('example')")
            archive = root / "snapshot.tar"
            original_digest = runner.create_snapshot(source, archive)
            restored = root / "restored"
            runner.restore_snapshot(archive, restored, original_digest)
            self.assertEqual(json.loads((restored / "news_archive.json").read_text())["articles"][0]["title"], "preserved")
            self.assertEqual(json.loads((restored / "news_collection_status.json").read_text())["consecutive_failures"], 0)
            with sqlite3.connect(f"file:{restored / 'news_summaries.sqlite3'}?mode=ro", uri=True) as db:
                self.assertEqual(db.execute("select title from summaries").fetchone(), ("example",))

    def test_snapshot_accepts_absent_legacy_sqlite(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            write_crawler_state(source)
            digest = runner.create_snapshot(source, root / "snapshot.tar")
            runner.restore_snapshot(root / "snapshot.tar", root / "restored", digest)

    def test_snapshot_rejects_invalid_archive_schema_and_missing_status(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            write_crawler_state(source)
            (source / "news_archive.json").write_text('{"schema_version":"unsupported","articles":[]}', encoding="utf-8")
            with self.assertRaises(runner.BackupError):
                runner.create_snapshot(source, root / "snapshot.tar")
            write_crawler_state(source)
            (source / "news_collection_status.json").unlink()
            with self.assertRaises(runner.BackupError):
                runner.create_snapshot(source, root / "snapshot.tar")

    def test_snapshot_rejects_malformed_json_entries_and_corrupt_optional_sqlite(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            write_crawler_state(source)
            (source / "news_archive.json").write_text("{malformed", encoding="utf-8")
            with self.assertRaises(runner.BackupError):
                runner.create_snapshot(source, root / "snapshot.tar")
            write_crawler_state(source)
            archive = json.loads((source / "news_archive.json").read_text(encoding="utf-8"))
            archive["articles"] = ["invalid"]
            (source / "news_archive.json").write_text(json.dumps(archive), encoding="utf-8")
            with self.assertRaises(runner.BackupError):
                runner.create_snapshot(source, root / "snapshot.tar")
            write_crawler_state(source)
            (source / "news_summaries.sqlite3").write_text("not a database", encoding="utf-8")
            with self.assertRaises(runner.BackupError):
                runner.create_snapshot(source, root / "snapshot.tar")

    def test_snapshot_rejects_symlink_before_archiving(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            write_crawler_state(source)
            (source / "outside").symlink_to(root)
            with self.assertRaises(runner.BackupError):
                runner.create_snapshot(source, root / "snapshot.tar")

    def test_snapshot_rejects_hardlink(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            write_crawler_state(source)
            (source / "linked.json").hardlink_to(source / "news_archive.json")
            with self.assertRaises(runner.BackupError):
                runner.create_snapshot(source, root / "snapshot.tar")

    def test_restore_rejects_path_traversal(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "bad.tar"
            with tarfile.open(archive, "w") as tar:
                payload = b"escaped"
                member = tarfile.TarInfo("../outside.txt")
                member.size = len(payload)
                tar.addfile(member, io.BytesIO(payload))
            with self.assertRaises(runner.BackupError):
                runner.restore_snapshot(archive, root / "restore", "sha256:" + "0" * 64)
            self.assertFalse((root / "outside.txt").exists())


class CrawlerBackupControllerTests(unittest.TestCase):
    def test_check_refuses_stale_lock(self):
        runner = load_runner()
        with patch.object(runner, "k8s_json", return_value={"data": {"lock_run_id": "previous-run"}}):
            with self.assertRaises(runner.BackupError):
                runner.assert_lock_available()

    def test_check_rejects_invalid_archive_before_remote_access(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_crawler_state(root)
            (root / "news_archive.json").write_text("{malformed", encoding="utf-8")
            with patch.object(runner.sys, "argv", ["crawler-pvc-backup-verify.py", "--check"]), patch.object(
                runner, "MOUNT", root
            ), patch.object(runner, "required_secret_files", return_value=(root,) * 4), patch.object(
                runner, "workload_state", return_value=("deployment-1", "pvc-1")
            ), patch.object(runner, "assert_lock_available"), patch.object(
                runner, "run_command"
            ) as command:
                self.assertEqual(runner.main(), 1)
            command.assert_not_called()

    def test_go_entrypoint_does_not_precheck_before_run_go(self):
        runner = load_runner()
        with patch.object(runner.sys, "argv", ["crawler-pvc-backup-verify.py", "--go"]), patch.object(
            runner, "required_secret_files"
        ) as secret_check, patch.object(runner, "workload_state") as workload_check, patch.object(
            runner, "run_go"
        ) as run_go:
            self.assertEqual(runner.main(), 0)
        secret_check.assert_not_called()
        workload_check.assert_not_called()
        run_go.assert_called_once_with()

    def test_go_reports_invalid_archive_without_writer_pause(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_crawler_state(root)
            (root / "news_archive.json").write_text("{malformed", encoding="utf-8")
            with patch.object(runner, "MOUNT", root), patch.object(
                runner, "required_secret_files", return_value=(root,) * 4
            ), patch.object(runner, "workload_state", return_value=("deployment-1", "pvc-1")), patch.object(
                runner, "run_command"
            ) as command, patch.object(runner, "patch_state") as state_patch:
                controller = runner.CrawlerBackupController(root)
                with self.assertRaises(runner.BackupError):
                    controller.run_backup()
                controller.publish_failure()
            command.assert_not_called()
            self.assertEqual(state_patch.call_count, 2)
            self.assertEqual(state_patch.call_args_list[0].args[0]["status"], "running")
            self.assertEqual(state_patch.call_args_list[1].args[0]["status"], "failed")

    def run_full_backup(self, *, fail_remote_restore=False, fail_remote_preflight=False,
                        remote_preflight_timeouts=0, lock_busy=False,
                        fail_success_patch=False, fail_failure_patch=False,
                        fail_scale=False, pods_stay=False, fail_writer_recovery=False,
                        missing_secret=False, fail_cleanup=False,
                        replacement_deployment=False, pause_conflict=False,
                        pause_replacement=False, restore_conflict=False,
                        restore_replacement=False):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pvc"
            source.mkdir()
            write_crawler_state(source)
            with sqlite3.connect(source / "news_summaries.sqlite3") as db:
                db.execute("create table summaries (title text)")
                db.execute("insert into summaries values ('kept')")
            secret = root / "secret"
            secret.mkdir()
            for key in ("rclone-config", "rclone-config-passphrase", "age-recipient", "age-identity"):
                (secret / key).write_text("fake", encoding="utf-8")
            if missing_secret:
                (secret / "age-identity").unlink()
            remote = root / "remote.age"
            calls = []
            reports = []
            state = {"replicas": 1, "lock": "other-run" if lock_busy else "", "uid": "deployment-1"}
            revision = [1]
            remote_attempts = [0]

            def command(*argv, **_kwargs):
                calls.append(argv)
                if argv[:3] == ("kubectl", "-n", "personal-server"):
                    request = argv[3:]
                    if request[:3] == ("get", "deployment", "crawler-worker"):
                        return json.dumps({"metadata": {"uid": state["uid"], "resourceVersion": str(revision[0])}, "spec": {"replicas": state["replicas"]}, "status": {"availableReplicas": state["replicas"]}})
                    if request[:3] == ("get", "pvc", "crawler-worker-data"):
                        return json.dumps({"metadata": {"uid": "pvc-1"}, "spec": {"accessModes": ["ReadWriteOnce"]}, "status": {"phase": "Bound"}})
                    if request[:2] == ("get", "pods"):
                        return json.dumps({"items": []})
                    if request[:2] == ("scale", "deployment/crawler-worker"):
                        target = int(request[2].split("=")[1])
                        if target == 0 and pause_replacement:
                            state["uid"] = "replacement-deployment"
                            revision[0] += 1
                        if target == 1 and restore_replacement:
                            state["uid"] = "replacement-deployment"
                            revision[0] += 1
                        if (target == 0 and pause_conflict) or (target == 1 and restore_conflict):
                            state["replicas"] = 2
                            revision[0] += 1
                        if (f"--current-replicas={state['replicas']}" not in request or
                                f"--resource-version={revision[0]}" not in request):
                            raise runner.BackupError("scale precondition failed")
                        state["replicas"] = target
                        revision[0] += 1
                        if replacement_deployment and state["replicas"] == 0:
                            state["uid"] = "replacement-deployment"
                        if fail_scale and state["replicas"] == 0:
                            raise runner.BackupError("scale response ambiguous")
                        return ""
                    if request[:2] == ("patch", "configmap"):
                        operations = json.loads(request[-1].split("=", 1)[1])
                        if request[2] != "crawler-pvc-backup-state" or operations[0]["value"] != state["lock"]:
                            raise runner.BackupError("lock busy")
                        status = next((item["value"] for item in operations if item["path"] == "/data/status"), "")
                        if (status == "completed" and fail_success_patch) or (status == "failed" and fail_failure_patch):
                            raise runner.BackupError("status patch failed")
                        state["lock"] = next(item["value"] for item in operations if item["path"] == "/data/lock_run_id" and item["op"] == "add")
                        reports.append((request[2], operations))
                        return ""
                if argv[0] == "age":
                    shutil.copyfile(argv[-1], argv[argv.index("-o") + 1])
                    return ""
                if argv[0] == "rclone":
                    if "lsd" in argv:
                        remote_attempts[0] += 1
                        if remote_attempts[0] <= remote_preflight_timeouts:
                            raise runner.CommandTimeout("external command timed out")
                        if fail_remote_preflight:
                            raise runner.BackupError("remote unavailable")
                        return ""
                    if fail_remote_restore and str(argv[-1]).endswith("download.age"):
                        raise runner.BackupError("remote restore failed")
                    if str(argv[-1]).startswith("gdrive:"):
                        shutil.copyfile(argv[-2], remote)
                    else:
                        shutil.copyfile(remote, argv[-1])
                    return ""
                raise AssertionError(argv)

            def no_pods():
                if pods_stay:
                    raise runner.BackupError("pods did not terminate")
                return None

            def ready():
                if fail_writer_recovery:
                    raise runner.BackupError("writer not ready")
                return None

            real_snapshot = runner.create_snapshot

            def snapshot(*args):
                self.assertEqual(state["replicas"], 0)
                result = real_snapshot(*args)
                calls.append(("snapshot-complete",))
                return result

            class ScratchDirectory:
                def __enter__(self):
                    return str(root)

                def __exit__(self, *_args):
                    calls.append(("scratch-cleanup",))
                    if fail_cleanup:
                        raise OSError("scratch cleanup failed")
                    return False

            with patch.object(runner, "run_command", side_effect=command), patch.object(
                runner, "MOUNT", source
            ), patch.object(runner, "SECRET", secret), patch.object(
                runner, "wait_for_pods_absent", side_effect=no_pods
            ), patch.object(runner, "wait_for_ready", side_effect=ready), patch.object(
                runner.tempfile, "TemporaryDirectory", return_value=ScratchDirectory()
            ), patch.object(runner, "run_retention") as retention, patch.object(runner, "create_snapshot", side_effect=snapshot
            ):
                if any((fail_remote_restore, fail_remote_preflight, remote_preflight_timeouts >= 2,
                        lock_busy, fail_success_patch, fail_scale,
                        pods_stay, fail_writer_recovery, missing_secret, fail_cleanup,
                        replacement_deployment, pause_conflict, pause_replacement,
                        restore_conflict, restore_replacement)):
                    with self.assertRaises((runner.BackupError, OSError)):
                        runner.run_go()
                    retention.assert_not_called()
                else:
                    runner.run_go()
                    retention.assert_called_once_with(delete=True)
            return calls, reports, state, remote.read_bytes() if remote.exists() else b""

    @staticmethod
    def scale_call(calls, replicas):
        prefix = ("kubectl", "-n", "personal-server", "scale", "deployment/crawler-worker", f"--replicas={replicas}")
        return next(call for call in calls if call[:len(prefix)] == prefix)

    def test_scale_uses_replica_and_resource_version_preconditions(self):
        calls, _reports, _state, _ciphertext = self.run_full_backup()
        self.assertEqual(self.scale_call(calls, 0)[-2:], ("--current-replicas=1", "--resource-version=1"))
        self.assertEqual(self.scale_call(calls, 1)[-2:], ("--current-replicas=0", "--resource-version=2"))

    def test_concurrent_replica_change_during_pause_keeps_lock(self):
        calls, reports, state, _ciphertext = self.run_full_backup(pause_conflict=True)
        self.assertEqual(state["replicas"], 2)
        self.assertNotEqual(state["lock"], "")
        self.assertFalse(any("--replicas=1" in call for call in calls))
        self.assertIn({"op": "add", "path": "/data/status", "value": "failed"}, reports[-1][1])

    def test_replacement_during_pause_keeps_lock(self):
        calls, _reports, state, _ciphertext = self.run_full_backup(pause_replacement=True)
        self.assertEqual(state["uid"], "replacement-deployment")
        self.assertNotEqual(state["lock"], "")
        self.assertFalse(any("--replicas=1" in call for call in calls))

    def test_concurrent_replica_change_during_restore_keeps_lock(self):
        calls, reports, state, _ciphertext = self.run_full_backup(restore_conflict=True)
        self.assertEqual(state["replicas"], 2)
        self.assertNotEqual(state["lock"], "")
        self.assertIn({"op": "add", "path": "/data/stage", "value": "writer-recovery"}, reports[-1][1])

    def test_replacement_during_restore_keeps_lock(self):
        calls, _reports, state, _ciphertext = self.run_full_backup(restore_replacement=True)
        self.assertEqual(state["uid"], "replacement-deployment")
        self.assertEqual(state["replicas"], 0)
        self.assertNotEqual(state["lock"], "")
        self.assertEqual(len([call for call in calls if "--replicas=1" in call]), 1)

    def test_upload_download_and_isolated_restore_publish_evidence_after_writer_recovery(self):
        calls, reports, state, ciphertext = self.run_full_backup()
        self.assertTrue(ciphertext)
        self.assertEqual(state, {"replicas": 1, "lock": "", "uid": "deployment-1"})
        self.assertIn(self.scale_call(calls, 0), calls)
        self.assertIn(self.scale_call(calls, 1), calls)
        self.assertLess(calls.index(("snapshot-complete",)), calls.index(self.scale_call(calls, 1)))
        self.assertLess(calls.index(self.scale_call(calls, 1)), next(i for i, call in enumerate(calls) if call[0] == "age"))
        self.assertTrue(all(name == "crawler-pvc-backup-state" for name, _ in reports))
        self.assertIn({"op": "add", "path": "/data/evidence", "value": ""}, reports[0][1])
        evidence = reports[-1][1]
        self.assertIn("scope=crawler-worker", next(item["value"] for item in evidence if item["path"] == "/data/evidence"))
        self.assertIn("restore_status=success", next(item["value"] for item in evidence if item["path"] == "/data/evidence"))
        self.assertIn("restore_check=json_schema_and_optional_sqlite_quick_check", next(item["value"] for item in evidence if item["path"] == "/data/evidence"))
        self.assertIn({"op": "add", "path": "/data/status", "value": "completed"}, evidence)
        self.assertLess(
            calls.index(self.scale_call(calls, 1)),
            calls.index(("scratch-cleanup",)),
        )
        self.assertLess(
            calls.index(("scratch-cleanup",)),
            next(index for index, call in enumerate(calls) if call[:6] == (
                "kubectl", "-n", "personal-server", "patch", "configmap", "crawler-pvc-backup-state"
            ) and "scope=crawler-worker" in call[-1]),
        )

    def test_failed_remote_restore_recovers_writer_without_success_evidence(self):
        calls, reports, state, ciphertext = self.run_full_backup(fail_remote_restore=True)
        self.assertTrue(ciphertext)
        self.assertEqual(state, {"replicas": 1, "lock": "", "uid": "deployment-1"})
        self.assertLess(
            calls.index(self.scale_call(calls, 1)),
            next(i for i, call in enumerate(calls) if call[0] == "rclone" and "copyto" in call),
        )
        self.assertTrue(all(name == "crawler-pvc-backup-state" for name, _ in reports))
        self.assertIn({"op": "add", "path": "/data/status", "value": "failed"}, reports[-1][1])
        self.assertIn({"op": "add", "path": "/data/evidence", "value": ""}, reports[-1][1])

    def test_failed_success_and_failure_report_leave_evidence_invalid(self):
        _calls, reports, state, _ciphertext = self.run_full_backup(
            fail_success_patch=True, fail_failure_patch=True
        )
        self.assertEqual(state["replicas"], 1)
        self.assertNotEqual(state["lock"], "")
        self.assertEqual(len(reports), 1)
        self.assertIn({"op": "add", "path": "/data/evidence", "value": ""}, reports[0][1])
        self.assertIn({"op": "add", "path": "/data/status", "value": "running"}, reports[0][1])

    def test_scratch_cleanup_failure_keeps_lock_and_blocks_success_evidence(self):
        calls, reports, state, _ciphertext = self.run_full_backup(fail_cleanup=True)
        self.assertEqual(state["replicas"], 1)
        self.assertNotEqual(state["lock"], "")
        self.assertIn(("scratch-cleanup",), calls)
        self.assertEqual(len(reports), 1)
        self.assertIn({"op": "add", "path": "/data/status", "value": "running"}, reports[0][1])
        self.assertIn({"op": "add", "path": "/data/evidence", "value": ""}, reports[0][1])

    def test_failed_restore_and_scratch_cleanup_keeps_lock(self):
        calls, reports, state, _ciphertext = self.run_full_backup(
            fail_remote_restore=True, fail_cleanup=True
        )
        self.assertEqual(state["replicas"], 1)
        self.assertNotEqual(state["lock"], "")
        self.assertIn(("scratch-cleanup",), calls)
        self.assertEqual(len(reports), 1)
        self.assertIn({"op": "add", "path": "/data/evidence", "value": ""}, reports[0][1])

    def test_secret_missing_prevents_any_writer_or_lock_change(self):
        calls, reports, state, _ciphertext = self.run_full_backup(missing_secret=True)
        self.assertEqual([next(item["value"] for item in operations if item["path"] == "/data/status") for _, operations in reports], ["running", "failed"])
        self.assertFalse(any(call[3:5] == ("scale", "deployment/crawler-worker") for call in calls))
        self.assertEqual(state, {"replicas": 1, "lock": "", "uid": "deployment-1"})

    def test_remote_preflight_failure_reports_failed_without_writer_pause(self):
        calls, reports, state, _ciphertext = self.run_full_backup(fail_remote_preflight=True)
        self.assertEqual([next(item["value"] for item in operations if item["path"] == "/data/status") for _, operations in reports], ["running", "failed"])
        self.assertFalse(any(call[3:5] == ("scale", "deployment/crawler-worker") for call in calls))
        self.assertEqual(state["replicas"], 1)
        self.assertEqual(sum(call[0] == "rclone" and "lsd" in call for call in calls), 1)
        self.assertIn({"op": "add", "path": "/data/stage", "value": "preflight-remote-error"}, reports[-1][1])

    def test_remote_preflight_timeout_exhaustion_reports_safe_stage_without_writer_pause(self):
        calls, reports, state, _ciphertext = self.run_full_backup(remote_preflight_timeouts=2)
        self.assertEqual(sum(call[0] == "rclone" and "lsd" in call for call in calls), 2)
        self.assertFalse(any(call[3:5] == ("scale", "deployment/crawler-worker") for call in calls))
        self.assertEqual(state["replicas"], 1)
        self.assertIn({"op": "add", "path": "/data/stage", "value": "preflight-remote-timeout"}, reports[-1][1])

    def test_remote_preflight_single_timeout_recovers_before_writer_pause(self):
        calls, reports, state, ciphertext = self.run_full_backup(remote_preflight_timeouts=1)
        self.assertTrue(ciphertext)
        self.assertEqual(sum(call[0] == "rclone" and "lsd" in call for call in calls), 2)
        self.assertEqual(state["replicas"], 1)
        self.assertIn({"op": "add", "path": "/data/status", "value": "completed"}, reports[-1][1])

    def test_ambiguous_pause_response_keeps_lock_for_manual_recovery(self):
        calls, reports, state, _ciphertext = self.run_full_backup(fail_scale=True)
        self.assertEqual(state["replicas"], 0)
        self.assertNotEqual(state["lock"], "")
        self.assertFalse(any("--replicas=1" in call for call in calls))
        self.assertIn({"op": "add", "path": "/data/status", "value": "failed"}, reports[-1][1])

    def test_pod_termination_failure_restores_writer(self):
        calls, reports, state, _ciphertext = self.run_full_backup(pods_stay=True)
        self.assertEqual(state, {"replicas": 1, "lock": "", "uid": "deployment-1"})
        self.assertIn(self.scale_call(calls, 1), calls)
        self.assertIn({"op": "add", "path": "/data/status", "value": "failed"}, reports[-1][1])

    def test_writer_recovery_failure_does_not_publish_success(self):
        _calls, reports, state, _ciphertext = self.run_full_backup(fail_writer_recovery=True)
        self.assertNotEqual(state["lock"], "")
        self.assertFalse(any({"op": "add", "path": "/data/status", "value": "completed"} in operations for _, operations in reports))
        self.assertIn({"op": "add", "path": "/data/status", "value": "failed"}, reports[-1][1])
        self.assertIn({"op": "add", "path": "/data/evidence", "value": ""}, reports[-1][1])

    def test_writer_recovery_refuses_replacement_deployment(self):
        runner = load_runner()
        controller = runner.CrawlerBackupController(Path("/tmp/crawler-backup-test"))
        controller.deployment_uid = "original-deployment"
        controller.writer_restore_needed = True
        controller.pause_scale_confirmed = True
        with patch.object(runner, "k8s_json", return_value={"metadata": {"uid": "replacement-deployment", "resourceVersion": "2"}, "spec": {"replicas": 0}}), patch.object(
            runner, "kubectl"
        ) as kubectl:
            with self.assertRaises(runner.BackupError):
                controller._restore_writer()
        kubectl.assert_not_called()
        self.assertTrue(controller.writer_restore_needed)

    def test_replacement_deployment_blocks_scale_and_keeps_lock(self):
        calls, reports, state, _ciphertext = self.run_full_backup(replacement_deployment=True)
        self.assertEqual(state["uid"], "replacement-deployment")
        self.assertNotEqual(state["lock"], "")
        self.assertFalse(any("--replicas=1" in call for call in calls))
        self.assertIn({"op": "add", "path": "/data/status", "value": "failed"}, reports[-1][1])
        self.assertIn({"op": "add", "path": "/data/stage", "value": "writer-recovery"}, reports[-1][1])

    def test_existing_lock_prevents_writer_pause(self):
        calls, reports, state, _ciphertext = self.run_full_backup(lock_busy=True)
        self.assertEqual(state, {"replicas": 1, "lock": "other-run", "uid": "deployment-1"})
        self.assertFalse(any(call[3:5] == ("scale", "deployment/crawler-worker") for call in calls))
        self.assertEqual(reports, [])

    def test_writer_is_restored_after_snapshot_failure(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            source = Path(directory)
            write_crawler_state(source)

            def command(*argv, **_kwargs):
                calls.append(argv)
                if argv[-5:] == ("get", "deployment", "crawler-worker", "-o", "json"):
                    paused = any("--replicas=0" in call for call in calls)
                    return json.dumps({"metadata": {"uid": "deployment-1", "resourceVersion": "2" if paused else "1"}, "spec": {"replicas": 0 if paused else 1}, "status": {"availableReplicas": 1}})
                if argv[-5:] == ("get", "pvc", "crawler-worker-data", "-o", "json"):
                    return json.dumps({"metadata": {"uid": "pvc-1"}, "spec": {"accessModes": ["ReadWriteOnce"]}, "status": {"phase": "Bound"}})
                if argv[-6:] == ("get", "pods", "-l", "app.kubernetes.io/name=crawler-worker", "-o", "json"):
                    return json.dumps({"items": []})
                return ""

            with patch.object(runner, "MOUNT", source), patch.object(runner, "run_command", side_effect=command), patch.object(
                runner, "required_secret_files", return_value=(Path(directory),) * 4
            ), patch.object(
                runner, "create_snapshot", side_effect=runner.BackupError("snapshot failed")
            ), patch.object(runner, "wait_for_ready", return_value=None):
                controller = runner.CrawlerBackupController(Path(directory))
                with self.assertRaises(runner.BackupError):
                    controller.run_backup()
            self.assertIn(self.scale_call(calls, 0), calls)
            self.assertIn(self.scale_call(calls, 1), calls)


class CrawlerBackupManifestTests(unittest.TestCase):
    def test_cronjob_is_suspended_and_has_no_secret_values(self):
        text = MANIFEST.read_text(encoding="utf-8")
        self.assertIn("suspend: true", text)
        self.assertIn("concurrencyPolicy: Forbid", text)
        self.assertIn("backoffLimit: 0", text)
        self.assertIn("claimName: crawler-worker-data", text)
        self.assertIn("readOnly: true", text)
        self.assertIn("secretName: crawler-worker-pvc-backup-runtime", text)
        self.assertIn("resourceNames: [crawler-pvc-backup-state]", text)
        self.assertNotIn("crawler-pvc-backup-status", text)
        self.assertNotIn("stringData:", text)
        self.assertNotIn("secretKeyRef:", text)

    def test_status_configmap_requires_separate_bootstrap(self):
        cronjob = MANIFEST.read_text(encoding="utf-8")
        bootstrap = STATE_BOOTSTRAP.read_text(encoding="utf-8")
        self.assertNotIn("kind: ConfigMap", cronjob)
        self.assertNotIn("  lock_run_id:", cronjob)
        self.assertNotIn("  evidence:", cronjob)
        self.assertEqual(bootstrap.count("kind: ConfigMap"), 1)
        self.assertIn("name: crawler-pvc-backup-state", bootstrap)
        self.assertIn('  lock_run_id: ""', bootstrap)
        self.assertIn('  evidence: ""', bootstrap)


if __name__ == "__main__":
    unittest.main()
