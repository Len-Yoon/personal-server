"""Book PVC backup contracts; no production Kubernetes resources are touched."""

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

import yaml


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "infra/k8s/tools/book-pvc-backup-verify.py"
MANIFEST = ROOT / "infra/k8s/backup-automation/book-memo-pvc-backup-cronjob.yaml"
STATE_BOOTSTRAP = ROOT / "infra/k8s/backup-automation/book-memo-pvc-backup-state-bootstrap.yaml"


def scale_calls(calls, replicas):
    prefix = ("kubectl", "-n", "personal-server", "scale", "deployment/book-memo", f"--replicas={replicas}")
    return [call for call in calls if call[:len(prefix)] == prefix]


def load_runner():
    spec = importlib.util.spec_from_file_location("book_pvc_backup_verify", RUNNER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BookBackupDataTests(unittest.TestCase):
    def test_snapshot_round_trip_checks_database_and_tree_digest(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            with sqlite3.connect(source / "book_memo.sqlite3") as db:
                db.execute("create table books (title text not null)")
                db.execute("insert into books values ('example')")
            (source / "notes.txt").write_text("preserved", encoding="utf-8")
            archive = root / "snapshot.tar"
            original_digest = runner.create_snapshot(source, archive)
            restored = root / "restored"
            runner.restore_snapshot(archive, restored, original_digest)
            self.assertEqual((restored / "notes.txt").read_text(), "preserved")
            with sqlite3.connect(f"file:{restored / 'book_memo.sqlite3'}?mode=ro", uri=True) as db:
                self.assertEqual(db.execute("select title from books").fetchone(), ("example",))

    def test_snapshot_rejects_symlink_before_archiving(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            with sqlite3.connect(source / "book_memo.sqlite3") as db:
                db.execute("create table books (title text)")
            (source / "outside").symlink_to(root)
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


class BookBackupControllerTests(unittest.TestCase):
    def test_remote_preflight_retries_only_timeout_without_exposing_diagnostics(self):
        runner = load_runner()
        with patch.object(runner.subprocess, "run", side_effect=subprocess.TimeoutExpired(
            ["rclone", "private-config-path"], 30, stderr=b"private diagnostic"
        )):
            with self.assertRaises(runner.CommandTimeout) as caught:
                runner.run_command("rclone", "lsd", timeout=30)
        self.assertEqual(str(caught.exception), "external command timed out")
        with patch.object(runner, "rclone_args", return_value=("rclone",)), patch.object(
            runner, "run_command", side_effect=[runner.CommandTimeout("private diagnostic"), ""]
        ) as command, patch.object(runner.time, "sleep") as sleep:
            runner.remote_preflight()
        self.assertEqual([call.kwargs["timeout"] for call in command.call_args_list], [60, 60])
        sleep.assert_called_once_with(2)
        with patch.object(runner, "rclone_args", return_value=("rclone",)), patch.object(
            runner, "run_command", side_effect=runner.BackupError("private diagnostic")
        ) as command, patch.object(runner.time, "sleep") as sleep:
            with self.assertRaises(runner.RemotePreflightError) as caught:
                runner.remote_preflight()
        self.assertEqual(str(caught.exception), "remote preflight failed")
        command.assert_called_once()
        sleep.assert_not_called()

    def test_check_refuses_stale_lock(self):
        runner = load_runner()
        with patch.object(runner, "k8s_json", return_value={"data": {"lock_run_id": "previous-run"}}):
            with self.assertRaises(runner.BackupError):
                runner.assert_lock_available()

    def run_full_backup(self, *, fail_remote_restore=False, fail_remote_preflight=False,
                        remote_preflight_timeouts=0, lock_busy=False,
                        fail_success_patch=False, fail_failure_patch=False,
                        fail_scale=False, pods_stay=False, fail_writer_recovery=False,
                        missing_secret=False, fail_cleanup=False,
                        replacement_deployment=False, missing_database=False):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pvc"
            source.mkdir()
            with sqlite3.connect(source / "book_memo.sqlite3") as db:
                db.execute("create table books (title text)")
                db.execute("insert into books values ('kept')")
            if missing_database:
                (source / "book_memo.sqlite3").unlink()
            secret = root / "secret"
            secret.mkdir()
            for key in ("rclone-config", "rclone-config-passphrase", "age-recipient", "age-identity"):
                (secret / key).write_text("fake", encoding="utf-8")
            if missing_secret:
                (secret / "age-identity").unlink()
            remote = root / "remote.age"
            calls = []
            reports = []
            state = {"replicas": 1, "lock": "other-run" if lock_busy else "", "uid": "deployment-1", "rv": 8}

            def command(*argv, **_kwargs):
                calls.append(argv)
                if argv[:3] == ("kubectl", "-n", "personal-server"):
                    request = argv[3:]
                    if request[:3] == ("get", "deployment", "book-memo"):
                        return json.dumps({"metadata": {"uid": state["uid"], "resourceVersion": str(state["rv"])}, "spec": {"replicas": state["replicas"]}, "status": {"availableReplicas": state["replicas"]}})
                    if request[:3] == ("get", "pvc", "book-memo-data"):
                        return json.dumps({"metadata": {"uid": "pvc-1"}, "spec": {"accessModes": ["ReadWriteOnce"]}, "status": {"phase": "Bound"}})
                    if request[:2] == ("get", "pods"):
                        return json.dumps({"items": []})
                    if request[:2] == ("scale", "deployment/book-memo"):
                        if f"--resource-version={state['rv']}" not in request or f"--current-replicas={state['replicas']}" not in request:
                            raise runner.BackupError("scale precondition failed")
                        state["replicas"] = int(request[2].split("=")[1])
                        state["rv"] += 1
                        if replacement_deployment and state["replicas"] == 0:
                            state["uid"] = "replacement-deployment"
                        if fail_scale and state["replicas"] == 0:
                            raise runner.BackupError("scale response ambiguous")
                        return ""
                    if request[:2] == ("patch", "configmap"):
                        operations = json.loads(request[-1].split("=", 1)[1])
                        if request[2] != "book-pvc-backup-state" or operations[0]["value"] != state["lock"]:
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
                        if fail_remote_preflight:
                            raise runner.BackupError("private diagnostic")
                        if sum("lsd" in call for call in calls) <= remote_preflight_timeouts:
                            raise runner.CommandTimeout("private diagnostic")
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

            def ready(_expected_uid):
                if fail_writer_recovery:
                    raise runner.BackupError("writer not ready")
                return None

            class ScratchDirectory:
                def __enter__(self):
                    return str(root)

                def __exit__(self, *_args):
                    calls.append(("scratch-cleanup",))
                    if fail_cleanup:
                        raise OSError("scratch cleanup failed")
                    return False

            with patch.object(runner, "run_command", side_effect=command), patch.object(
                runner.time, "sleep"
            ), patch.object(
                runner, "MOUNT", source
            ), patch.object(runner, "SECRET", secret), patch.object(
                runner, "wait_for_pods_absent", side_effect=no_pods
            ), patch.object(runner, "wait_for_ready", side_effect=ready), patch.object(
                runner.tempfile, "TemporaryDirectory", return_value=ScratchDirectory()
            ), patch.object(runner, "run_retention") as retention:
                if any((fail_remote_restore, fail_remote_preflight, remote_preflight_timeouts > 1,
                        lock_busy, fail_success_patch, fail_scale,
                        pods_stay, fail_writer_recovery, missing_secret, fail_cleanup,
                        replacement_deployment, missing_database)):
                    with self.assertRaises((runner.BackupError, OSError)):
                        runner.run_go()
                    retention.assert_not_called()
                else:
                    runner.run_go()
                    retention.assert_called_once_with(delete=True)
            visible_state = {key: value for key, value in state.items() if key != "rv"}
            return calls, reports, visible_state, remote.read_bytes() if remote.exists() else b""

    def test_upload_download_and_isolated_restore_publish_evidence_after_writer_recovery(self):
        calls, reports, state, ciphertext = self.run_full_backup()
        self.assertTrue(ciphertext)
        self.assertEqual(state, {"replicas": 1, "lock": "", "uid": "deployment-1"})
        self.assertEqual(len(scale_calls(calls, 0)), 1)
        self.assertEqual(len(scale_calls(calls, 1)), 1)
        self.assertIn("--current-replicas=1", scale_calls(calls, 0)[0])
        self.assertIn("--current-replicas=0", scale_calls(calls, 1)[0])
        self.assertTrue(all(name == "book-pvc-backup-state" for name, _ in reports))
        self.assertIn({"op": "add", "path": "/data/evidence", "value": ""}, reports[0][1])
        evidence = reports[-1][1]
        self.assertIn("scope=book-memo", next(item["value"] for item in evidence if item["path"] == "/data/evidence"))
        self.assertIn("restore_status=success", next(item["value"] for item in evidence if item["path"] == "/data/evidence"))
        self.assertIn({"op": "add", "path": "/data/status", "value": "completed"}, evidence)
        self.assertLess(
            calls.index(scale_calls(calls, 1)[0]),
            next(index for index, call in enumerate(calls) if call[:2] == ("rclone", "--config") and "copyto" in call),
        )
        self.assertLess(
            calls.index(scale_calls(calls, 1)[0]),
            calls.index(("scratch-cleanup",)),
        )
        self.assertLess(
            calls.index(("scratch-cleanup",)),
            next(index for index, call in enumerate(calls) if call[:6] == (
                "kubectl", "-n", "personal-server", "patch", "configmap", "book-pvc-backup-state"
            ) and "scope=book-memo" in call[-1]),
        )

    def test_remote_preflight_timeout_exhaustion_releases_lock_without_pausing_writer(self):
        calls, reports, state, _ = self.run_full_backup(remote_preflight_timeouts=2)
        self.assertEqual(sum("lsd" in call for call in calls), 2)
        self.assertFalse(scale_calls(calls, 0))
        self.assertEqual(state["replicas"], 1)
        self.assertEqual(state["lock"], "")
        self.assertIn({"op": "add", "path": "/data/stage", "value": "preflight-remote-timeout"}, reports[-1][1])
        self.assertIn({"op": "add", "path": "/data/evidence", "value": ""}, reports[0][1])

    def test_remote_preflight_non_timeout_fails_once_without_pausing_writer(self):
        calls, reports, state, _ = self.run_full_backup(fail_remote_preflight=True)
        self.assertEqual(sum("lsd" in call for call in calls), 1)
        self.assertFalse(scale_calls(calls, 0))
        self.assertEqual(state["replicas"], 1)
        self.assertEqual(state["lock"], "")
        self.assertIn({"op": "add", "path": "/data/stage", "value": "preflight-remote-error"}, reports[-1][1])

    def test_single_remote_timeout_recovers_before_writer_pause(self):
        calls, reports, state, ciphertext = self.run_full_backup(remote_preflight_timeouts=1)
        self.assertTrue(ciphertext)
        self.assertEqual(sum("lsd" in call for call in calls), 2)
        self.assertEqual(state["replicas"], 1)
        self.assertIn({"op": "add", "path": "/data/status", "value": "completed"}, reports[-1][1])

    def test_failed_remote_restore_recovers_writer_without_success_evidence(self):
        calls, reports, state, ciphertext = self.run_full_backup(fail_remote_restore=True)
        self.assertTrue(ciphertext)
        self.assertEqual(state, {"replicas": 1, "lock": "", "uid": "deployment-1"})
        resume = scale_calls(calls, 1)[0]
        self.assertEqual(len(scale_calls(calls, 1)), 1)
        self.assertLess(calls.index(resume), next(index for index, call in enumerate(calls) if
            call[:2] == ("rclone", "--config") and "copyto" in call))
        self.assertTrue(all(name == "book-pvc-backup-state" for name, _ in reports))
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

    def test_secret_missing_reports_failure_without_pausing_writer(self):
        calls, reports, state, _ciphertext = self.run_full_backup(missing_secret=True)
        self.assertEqual(scale_calls(calls, 0), [])
        self.assertEqual(len(reports), 2)
        self.assertIn({"op": "add", "path": "/data/status", "value": "failed"}, reports[-1][1])
        self.assertIn(("scratch-cleanup",), calls)
        self.assertEqual(state, {"replicas": 1, "lock": "", "uid": "deployment-1"})

    def test_missing_database_preflight_never_pauses_writer(self):
        calls, reports, state, _ciphertext = self.run_full_backup(missing_database=True)
        self.assertFalse(any(call[:5] == ("kubectl", "-n", "personal-server", "scale", "deployment/book-memo") for call in calls))
        self.assertEqual(len(reports), 2)
        self.assertIn({"op": "add", "path": "/data/status", "value": "failed"}, reports[-1][1])
        self.assertEqual(state, {"replicas": 1, "lock": "", "uid": "deployment-1"})

    def test_ambiguous_pause_keeps_lock_without_overwriting_replica_state(self):
        calls, reports, state, _ciphertext = self.run_full_backup(fail_scale=True)
        self.assertEqual(state["replicas"], 0)
        self.assertNotEqual(state["lock"], "")
        self.assertEqual(len(scale_calls(calls, 0)), 1)
        self.assertEqual(scale_calls(calls, 1), [])
        self.assertIn({"op": "add", "path": "/data/status", "value": "failed"}, reports[-1][1])

    def test_pod_termination_failure_restores_writer(self):
        calls, reports, state, _ciphertext = self.run_full_backup(pods_stay=True)
        self.assertEqual(state, {"replicas": 1, "lock": "", "uid": "deployment-1"})
        self.assertEqual(len(scale_calls(calls, 1)), 1)
        self.assertIn({"op": "add", "path": "/data/status", "value": "failed"}, reports[-1][1])

    def test_writer_recovery_failure_does_not_publish_success(self):
        calls, reports, state, _ciphertext = self.run_full_backup(fail_writer_recovery=True)
        self.assertNotEqual(state["lock"], "")
        self.assertFalse(any(call[0] == "rclone" and "copyto" in call for call in calls))
        self.assertFalse(any({"op": "add", "path": "/data/status", "value": "completed"} in operations for _, operations in reports))
        self.assertIn({"op": "add", "path": "/data/status", "value": "failed"}, reports[-1][1])
        self.assertIn({"op": "add", "path": "/data/evidence", "value": ""}, reports[-1][1])

    def test_writer_recovery_refuses_replacement_deployment(self):
        runner = load_runner()
        controller = runner.BookBackupController(Path("/tmp/book-backup-test"))
        controller.deployment_uid = "original-deployment"
        controller.writer_restore_needed = True
        with patch.object(runner, "k8s_json", return_value={"metadata": {"uid": "replacement-deployment"}}), patch.object(
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
        self.assertEqual(scale_calls(calls, 1), [])
        self.assertIn({"op": "add", "path": "/data/status", "value": "failed"}, reports[-1][1])
        self.assertIn({"op": "add", "path": "/data/stage", "value": "writer-recovery"}, reports[-1][1])

    def test_existing_lock_prevents_writer_pause(self):
        calls, reports, state, _ciphertext = self.run_full_backup(lock_busy=True)
        self.assertEqual(state, {"replicas": 1, "lock": "other-run", "uid": "deployment-1"})
        self.assertFalse(any(call[3:5] == ("scale", "deployment/book-memo") for call in calls))
        self.assertEqual(reports, [])

    def test_writer_is_restored_after_snapshot_failure(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            state = {"replicas": 1, "rv": 8}

            def command(*argv, **_kwargs):
                calls.append(argv)
                if argv[-5:] == ("get", "deployment", "book-memo", "-o", "json"):
                    return json.dumps({"metadata": {"uid": "deployment-1", "resourceVersion": str(state["rv"])}, "spec": {"replicas": state["replicas"]}, "status": {"availableReplicas": state["replicas"]}})
                if argv[-5:] == ("get", "pvc", "book-memo-data", "-o", "json"):
                    return json.dumps({"metadata": {"uid": "pvc-1"}, "spec": {"accessModes": ["ReadWriteOnce"]}, "status": {"phase": "Bound"}})
                if argv[-6:] == ("get", "pods", "-l", "app.kubernetes.io/name=book-memo", "-o", "json"):
                    return json.dumps({"items": []})
                if argv[3:5] == ("scale", "deployment/book-memo"):
                    state["replicas"] = int(argv[5].split("=")[1])
                    state["rv"] += 1
                return ""

            with patch.object(runner, "run_command", side_effect=command), patch.object(
                runner, "required_secret_files", return_value=(Path(directory),) * 4
            ), patch.object(
                runner, "create_snapshot", side_effect=runner.BackupError("snapshot failed")
            ), patch.object(runner, "wait_for_ready", return_value=None), patch.object(
                runner, "check_sqlite", return_value=None
            ):
                controller = runner.BookBackupController(Path(directory))
                with self.assertRaises(runner.BackupError):
                    controller.run_backup()
            self.assertEqual(len(scale_calls(calls, 0)), 1)
            self.assertEqual(len(scale_calls(calls, 1)), 1)


class BookBackupManifestTests(unittest.TestCase):
    def test_service_backup_cronjobs_limit_missed_schedule_catchup(self):
        for filename in (
            "book-memo-pvc-backup-cronjob.yaml",
            "youtube-memo-pvc-backup-cronjob.yaml",
            "crawler-worker-pvc-backup-cronjob.yaml",
        ):
            with self.subTest(filename=filename):
                path = ROOT / "infra/k8s/backup-automation" / filename
                documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
                cronjob = next(document for document in documents if document["kind"] == "CronJob")
                self.assertEqual(cronjob["spec"].get("startingDeadlineSeconds"), 300)
                self.assertTrue(cronjob["spec"]["suspend"])

    def test_conditional_scale_has_named_update_permission(self):
        manifests = {
            "book-memo-pvc-backup-cronjob.yaml": "book-memo",
            "youtube-memo-pvc-backup-cronjob.yaml": "youtube-memo",
            "crawler-worker-pvc-backup-cronjob.yaml": "crawler-worker",
        }
        for filename, deployment in manifests.items():
            with self.subTest(filename=filename):
                path = ROOT / "infra/k8s/backup-automation" / filename
                documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
                role = next(document for document in documents if document["kind"] == "Role")
                scale = [rule for rule in role["rules"] if rule["resources"] == ["deployments/scale"]]
                self.assertEqual(len(scale), 1)
                self.assertEqual(scale[0]["apiGroups"], ["apps"])
                self.assertEqual(scale[0]["resourceNames"], [deployment])
                self.assertEqual(set(scale[0]["verbs"]), {"get", "patch", "update"})
                self.assertFalse(any("secrets" in rule["resources"] for rule in role["rules"]))

    def test_cronjob_is_suspended_and_has_no_secret_values(self):
        text = MANIFEST.read_text(encoding="utf-8")
        self.assertIn("suspend: true", text)
        self.assertIn("concurrencyPolicy: Forbid", text)
        self.assertIn("backoffLimit: 0", text)
        self.assertIn("claimName: book-memo-data", text)
        self.assertIn("readOnly: true", text)
        self.assertIn("secretName: book-memo-pvc-backup-runtime", text)
        self.assertIn("resourceNames: [book-pvc-backup-state]", text)
        self.assertNotIn("kind: ConfigMap", text)
        self.assertNotIn("book-pvc-backup-status", text)
        self.assertNotIn("stringData:", text)
        self.assertNotIn("secretKeyRef:", text)

    def test_state_configmap_is_bootstrap_only(self):
        text = STATE_BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("kind: ConfigMap", text)
        self.assertIn("name: book-pvc-backup-state", text)
        self.assertIn('lock_run_id: ""', text)
        self.assertIn('evidence: ""', text)
        self.assertNotIn("kind: CronJob", text)


class IndependentBookRestoreTests(unittest.TestCase):
    """Only temporary synthetic fixture data is used by this offline consumer suite."""

    def load_consumer(self):
        path = ROOT / "infra/k8s/tools/book-independent-restore.py"
        spec = importlib.util.spec_from_file_location("book_independent_restore", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def fixture(self, root, *, empty=False):
        import os
        import sys
        consumer = self.load_consumer()
        runner = load_runner()
        source = root / "source"
        source.mkdir(mode=0o700)
        database = source / "book_memo.sqlite3"
        # Use the real current service schema on a dedicated private fixture DB.
        subprocess.run([sys.executable, "-c", "from app.services.book_service import init_db; init_db()"],
                       cwd=ROOT / "book-memo", check=True, capture_output=True,
                       env={"PATH": os.defpath, "BOOK_MEMO_DB_PATH": str(database)})
        if not empty:
            with sqlite3.connect(database) as db:
                db.execute("INSERT INTO books(isbn,title) VALUES ('fixture-isbn','private-content-never-report')")
                db.execute("INSERT INTO book_memos(book_id,title,content) VALUES (1,'private-title','private-memo')")
        archive = root / "backup.tar"
        tree_digest = runner.create_snapshot(source, archive)
        evidence = root / "evidence.txt"
        evidence.write_text("\n".join(("schema_version=1", "scope=book-memo", "backup_status=success",
            "encrypted=true", "restore_status=success", "source_runtime=k3s-pvc",
            "restore_check=sqlite_quick_check", "restore_path_check=success", "backup_id=book-fixture",
            "backup_completed_at=2026-01-01T00:00:00Z", "restore_verified_at=2026-01-01T00:00:01Z",
            "evidence_expires_at=2026-01-02T00:00:00Z", "artifact_digest=sha256:" + "a" * 64,
            "source_digest=" + tree_digest)) + "\n")
        manifest = root / "manifest.json"
        value = {"schema_version": 1, "provenance": "synthetic_fixture", "source_host_label": "n100",
                 "destination_host_label": "recovery-mac", "independent_host_asserted": True,
                 "backup_evidence_sha256": consumer.sha(evidence.read_bytes()),
                 "plaintext_sha256": consumer.sha(archive.read_bytes()), "image_digest": "sha256:" + "b" * 64,
                 "configuration_sha256": consumer.runtime_config_digest(),
                 "source_main_sha256": "sha256:" + "c" * 64, "source_service_sha256": "sha256:" + "d" * 64,
                 "preparation": {}}
        manifest.write_text(json.dumps(value))
        for path in (archive, evidence, manifest):
            path.chmod(0o600)
        return consumer, archive, evidence, manifest, value

    def update_manifest(self, manifest, value):
        manifest.write_text(json.dumps(value))

    def test_check_defaults_to_no_extraction_or_runtime_and_never_claims_DR(self):
        with tempfile.TemporaryDirectory() as directory:
            consumer, archive, evidence, manifest, _ = self.fixture(Path(directory))
            with patch.object(consumer, "validate_database") as validation, patch.object(
                    consumer, "runtime_adapter") as runtime:
                report = consumer.run(archive, evidence, manifest)
            validation.assert_not_called()
            runtime.assert_not_called()
            self.assertEqual(report["status"], "input_check_passed")
            self.assertEqual(report["provenance"], "synthetic_fixture")
            self.assertFalse(report["independent_host_verified"])
            self.assertFalse(report["service_recovery_verified"])
            self.assertIsNone(report["total_rto_seconds"])
            self.assertEqual(report["preparation"]["acquisition"]["status"], "missing")
            self.assertNotIn("private-content", json.dumps(report))

    def test_go_checks_real_book_schema_exact_tree_and_read_without_data_output(self):
        with tempfile.TemporaryDirectory() as directory:
            consumer, archive, evidence, manifest, _ = self.fixture(Path(directory))
            before = archive.read_bytes()
            report = consumer.run(archive, evidence, manifest, execute=True)
            self.assertEqual(report["status"], "offline_restore_validation_passed")
            self.assertEqual(report["table_row_counts"]["books"], 1)
            self.assertEqual(report["table_row_counts"]["book_memos"], 1)
            self.assertTrue(report["restored_data_removed"])
            self.assertEqual(report["application_runtime"], "not_executed")
            self.assertNotIn("private-content", json.dumps(report))
            self.assertEqual(archive.read_bytes(), before)

    def test_rejects_tampered_plaintext_and_authoritative_evidence_bindings(self):
        with tempfile.TemporaryDirectory() as directory:
            consumer, archive, evidence, manifest, value = self.fixture(Path(directory))
            value["plaintext_sha256"] = "sha256:" + "0" * 64
            self.update_manifest(manifest, value)
            with self.assertRaisesRegex(consumer.RestoreError, "plaintext_digest_mismatch"):
                consumer.run(archive, evidence, manifest, execute=True)
            value["backup_evidence_sha256"] = "sha256:" + "1" * 64
            self.update_manifest(manifest, value)
            with self.assertRaisesRegex(consumer.RestoreError, "backup_evidence_digest_mismatch"):
                consumer.run(archive, evidence, manifest, execute=True)

    def test_rejects_shared_or_symlink_input_and_duplicate_manifest_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            consumer, archive, evidence, manifest, _ = self.fixture(root)
            archive.chmod(0o644)
            with self.assertRaisesRegex(consumer.RestoreError, "private_input_required"):
                consumer.run(archive, evidence, manifest)
            archive.chmod(0o600)
            link = root / "link.tar"
            link.symlink_to(archive)
            with self.assertRaisesRegex(consumer.RestoreError, "private_input_required"):
                consumer.run(link, evidence, manifest)
            manifest.write_text('{"schema_version":1,"schema_version":1}')
            with self.assertRaisesRegex(consumer.RestoreError, "duplicate_metadata_key"):
                consumer.run(archive, evidence, manifest)

    def test_rejects_archive_escape_links_duplicates_and_bombs_before_extraction(self):
        for kind in ("escape", "symlink", "duplicate", "expanded"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                consumer, archive, evidence, manifest, value = self.fixture(root)
                with tarfile.open(archive, "w") as tar:
                    item = tarfile.TarInfo("../escape" if kind == "escape" else "payload")
                    if kind == "symlink":
                        item.type, item.linkname = tarfile.SYMTYPE, "/private"
                    tar.addfile(item)
                    if kind == "duplicate":
                        tar.addfile(item)
                value["plaintext_sha256"] = consumer.sha(archive.read_bytes())
                self.update_manifest(manifest, value)
                cap = 0 if kind == "expanded" else consumer.MAX_MEMBERS
                with patch.object(consumer, "MAX_MEMBERS", cap), self.assertRaises(consumer.RestoreError):
                    consumer.run(archive, evidence, manifest, execute=True)
                self.assertFalse((root / "escape").exists())

    def test_evidence_tree_digest_is_not_replaced_by_operator_plaintext_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            consumer, archive, evidence, manifest, value = self.fixture(Path(directory))
            text = evidence.read_text()
            key = "source_digest="
            old = next(line for line in text.splitlines() if line.startswith(key))
            evidence.write_text(text.replace(old, key + "sha256:" + "0" * 64))
            value["backup_evidence_sha256"] = consumer.sha(evidence.read_bytes())
            self.update_manifest(manifest, value)
            with self.assertRaisesRegex(consumer.RestoreError, "restored_tree_digest_mismatch"):
                consumer.run(archive, evidence, manifest, execute=True)

    def test_old_or_wrong_book_schema_fails_even_when_sqlite_and_tree_are_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            consumer, archive, evidence, manifest, value = self.fixture(root)
            with sqlite3.connect(root / "source/book_memo.sqlite3") as db:
                db.execute("DROP INDEX idx_books_home_order")
            runner = load_runner()
            tree = runner.create_snapshot(root / "source", archive)
            text = evidence.read_text()
            old = next(line for line in text.splitlines() if line.startswith("source_digest="))
            evidence.write_text(text.replace(old, "source_digest=" + tree))
            value.update(plaintext_sha256=consumer.sha(archive.read_bytes()),
                         backup_evidence_sha256=consumer.sha(evidence.read_bytes()))
            self.update_manifest(manifest, value)
            with self.assertRaisesRegex(consumer.RestoreError, "book_schema_incompatible"):
                consumer.run(archive, evidence, manifest, execute=True)

    def test_preparation_is_explicit_operator_timing_not_total_rto(self):
        with tempfile.TemporaryDirectory() as directory:
            consumer, archive, evidence, manifest, value = self.fixture(Path(directory))
            value["preparation"] = {"acquisition": {"started_at": "2026-01-01T01:00:00Z",
                                                    "completed_at": "2026-01-01T01:02:00Z"}}
            self.update_manifest(manifest, value)
            report = consumer.run(archive, evidence, manifest)
            self.assertEqual(report["preparation"]["acquisition"]["seconds"], 120)
            self.assertEqual(report["preparation"]["acquisition"]["status"], "operator_supplied_unverified")
            self.assertIsNone(report["total_rto_seconds"])
            value["preparation"]["acquisition"]["completed_at"] = "2026-01-01T00:02:00Z"
            self.update_manifest(manifest, value)
            with self.assertRaisesRegex(consumer.RestoreError, "invalid_preparation"):
                consumer.run(archive, evidence, manifest)

    def test_runtime_requires_go_and_fixed_configuration_before_engine_access(self):
        with tempfile.TemporaryDirectory() as directory:
            consumer, archive, evidence, manifest, value = self.fixture(Path(directory))
            with patch.object(consumer, "runtime_adapter") as runtime:
                with self.assertRaisesRegex(consumer.RestoreError, "runtime_requires_go"):
                    consumer.run(archive, evidence, manifest, runtime=True)
                value["configuration_sha256"] = "sha256:" + "1" * 64
                self.update_manifest(manifest, value)
                with self.assertRaisesRegex(consumer.RestoreError, "runtime_configuration_digest_mismatch"):
                    consumer.run(archive, evidence, manifest, execute=True, runtime=True)
                runtime.assert_not_called()

    def test_runtime_functional_source_mismatch_still_cleans_owned_scratch(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            consumer, archive, evidence, manifest, value = self.fixture(root)
            engine = MagicMock()
            engine.cleanup.return_value = {"ok": True}
            engine.functional_read.return_value = {"row_counts": {}}
            with patch.object(consumer, "runtime_adapter", return_value=engine):
                with self.assertRaisesRegex(consumer.RestoreError, "application_runtime_failed"):
                    consumer.run(archive, evidence, manifest, execute=True, runtime=True)
            engine.cleanup.assert_called_once()

    def test_runtime_contract_rejects_images_with_baked_credentials_before_volume_creation(self):
        import sys
        import types
        consumer = self.load_consumer()
        class FakeBase:
            def __init__(self, *_args):
                pass
            def _inspect(self, *_args):
                return {"Id": "sha256:" + "b" * 64, "Os": "linux", "Config": {
                    "User": "10001:10001", "WorkingDir": "/app", "Entrypoint": None,
                    "Volumes": None, "OnBuild": None, "Healthcheck": None,
                    "Cmd": ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8003"],
                    "Labels": {"org.opencontainers.image.source": "https://github.com/Len-Yoon/personal-server",
                               "org.opencontainers.image.title": "book-memo"},
                    "Env": ["WRITE_AUTH_SECRET=must-never-be-copied"]}}
            def volume(self):
                raise AssertionError("must reject image before creating resources")
        with patch.dict(sys.modules, {"container_runtime": types.SimpleNamespace(LocalDocker=FakeBase)}):
            engine = consumer.runtime_adapter("a" * 32, 999999999)
            with self.assertRaisesRegex(consumer.RestoreError, "image_contract_mismatch"):
                engine.launch("sha256:" + "b" * 64, Path("/not-read"))


    def test_runtime_success_requires_exact_DB_source_and_HTTP_with_cleanup(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            consumer, archive, evidence, manifest, value = self.fixture(root)
            counts = {table: (1 if table in {"books", "book_memos"} else 0) for table in consumer.TABLE_COLUMNS}
            engine = MagicMock()
            engine.cleanup.return_value = {"ok": True, "containers_removed": 1, "volumes_removed": 1}
            engine.functional_read.return_value = {
                "row_counts": counts, "db_sha256": consumer.sha((root / "source/book_memo.sqlite3").read_bytes()),
                "main_sha256": value["source_main_sha256"], "service_sha256": value["source_service_sha256"],
                "ready_http": 200, "health_http": 200, "home_http": 200}
            with patch.object(consumer, "runtime_adapter", return_value=engine):
                report = consumer.run(archive, evidence, manifest, execute=True, runtime=True)
            self.assertEqual(report["status"], "isolated_application_read_verification_passed")
            self.assertTrue(report["application_runtime"]["cleanup"]["ok"])
            self.assertTrue(report["release_inputs"]["image_and_configuration_verified"])
            self.assertFalse(report["service_recovery_verified"])
            self.assertFalse(report["independent_host_verified"])
            self.assertIsNone(report["total_rto_seconds"])
            engine.cleanup.assert_called_once()

    def test_uncertain_cleanup_never_becomes_runtime_success_and_retains_owner_for_manual_review(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            consumer, archive, evidence, manifest, value = self.fixture(root)
            engine = MagicMock()
            engine.launch.side_effect = RuntimeError("must-not-log-private-diagnostic")
            engine.cleanup.return_value = {"ok": False}
            with patch.object(consumer, "runtime_adapter", return_value=engine):
                with self.assertRaisesRegex(consumer.RestoreError, "runtime_cleanup_failed_private_scratch_may_remain") as caught:
                    consumer.run(archive, evidence, manifest, execute=True, runtime=True)
            self.assertRegex(caught.exception.cleanup_owner, "^[a-f0-9]{32}$")
            self.assertNotIn("private-diagnostic", str(caught.exception))

    def test_nonempty_wal_cannot_be_silently_ignored_by_immutable_SQLite(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            consumer, _, _, _, _ = self.fixture(root)
            (root / "source/book_memo.sqlite3-wal").write_bytes(b"not-an-offline-main-DB")
            with self.assertRaisesRegex(consumer.RestoreError, "nonempty_WAL_requires_separate_consistent_snapshot"):
                consumer.validate_database(root / "source", time.monotonic() + 30)


    def test_nonempty_hot_rollback_journal_requires_a_consistent_snapshot(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            consumer, _, _, _, _ = self.fixture(root)
            (root / "source/book_memo.sqlite3-journal").write_bytes(b"hot-journal-needs-rollback")
            with self.assertRaisesRegex(consumer.RestoreError, "nonempty_rollback_journal_requires_separate_consistent_snapshot"):
                consumer.validate_database(root / "source", time.monotonic() + 30)

    def test_cleanup_exception_keeps_fixed_error_and_owner_for_private_residual_review(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            consumer, archive, evidence, manifest, _ = self.fixture(root)
            engine = MagicMock()
            engine.launch.side_effect = RuntimeError("private-launch-diagnostic")
            engine.cleanup.side_effect = OSError("private-cleanup-path")
            with patch.object(consumer, "runtime_adapter", return_value=engine):
                with self.assertRaisesRegex(consumer.RestoreError, "runtime_cleanup_failed_private_scratch_may_remain") as caught:
                    consumer.run(archive, evidence, manifest, execute=True, runtime=True)
            self.assertRegex(caught.exception.cleanup_owner, "^[a-f0-9]{32}$")
            self.assertNotIn("private", str(caught.exception).replace("private_scratch", ""))

    def test_CLI_temp_cleanup_errors_never_print_paths_or_report_success(self):
        import contextlib
        import sys
        consumer = self.load_consumer()
        output = io.StringIO()
        argv = ["book-independent-restore.py", "--archive", "/private-input", "--evidence", "/private-evidence",
                "--manifest", "/private-manifest", "--go"]
        with patch.object(sys, "argv", argv), patch.object(consumer.signal, "signal"), patch.object(
                consumer, "run", side_effect=OSError("secret-private-cleanup-path")), contextlib.redirect_stdout(output):
            self.assertEqual(consumer.main(), 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["reason"], "offline_input_or_validation_failed")
        self.assertNotIn("secret-private", output.getvalue())
        self.assertFalse(report["service_recovery_verified"])


class BookIndependentRestoreStagingTests(unittest.TestCase):
    def load_stage(self):
        path = ROOT / "infra/k8s/tools/book-independent-restore-stage.py"
        spec = importlib.util.spec_from_file_location("book_restore_stage_renderer", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def production_shape_fixture(self, root):
        consumer, _, evidence, _, _ = IndependentBookRestoreTests().fixture(root)
        evidence.write_text(evidence.read_text().replace("backup_id=book-fixture", "backup_id=book-20261026T010203Z-12345"))
        return consumer, evidence, consumer.sha(evidence.read_bytes())

    def rendered(self, root):
        stage = self.load_stage()
        consumer, evidence, evidence_sha = self.production_shape_fixture(root)
        image = "personal-server-book-pvc-backup@sha256:" + "1" * 64
        return stage, stage.render(image, evidence, evidence_sha)

    def test_render_contains_only_new_immutable_configmap_and_bounded_tokenless_Pod(self):
        with tempfile.TemporaryDirectory() as directory:
            stage, resources = self.rendered(Path(directory))
            self.assertEqual([item["kind"] for item in resources["items"]], ["ConfigMap", "Pod"])
            cm, pod = resources["items"]
            self.assertTrue(cm["immutable"])
            expected = __import__("hashlib").sha256(stage.canonical(cm["data"])).hexdigest()
            self.assertEqual(cm["metadata"]["annotations"]["personal-server/content-sha256"], expected)
            self.assertTrue(cm["metadata"]["name"].endswith(expected[:24]))
            spec = pod["spec"]
            self.assertFalse(spec["automountServiceAccountToken"])
            self.assertFalse(spec["enableServiceLinks"])
            self.assertEqual(spec["restartPolicy"], "Never")
            self.assertEqual(spec["activeDeadlineSeconds"], 900)
            self.assertEqual(spec["securityContext"]["runAsUser"], 10001)
            actor = spec["containers"][0]
            self.assertEqual(actor["imagePullPolicy"], "Never")
            self.assertEqual(actor["command"], ["/usr/local/bin/python", "-B", "/stage/stage.py"])
            self.assertEqual(actor["securityContext"]["capabilities"]["drop"], ["ALL"])
            self.assertTrue(actor["securityContext"]["readOnlyRootFilesystem"])
            self.assertFalse(actor["securityContext"]["allowPrivilegeEscalation"])
            self.assertNotIn("ports", actor)
            self.assertNotIn("env", actor)
            self.assertEqual(actor["resources"]["limits"]["ephemeral-storage"], "1Gi")
            volumes = {value["name"]: value for value in spec["volumes"]}
            secret = volumes["credentials"]["secret"]
            self.assertEqual(secret["secretName"], "book-memo-pvc-backup-runtime")
            self.assertEqual({item["key"] for item in secret["items"]},
                             {"rclone-config", "rclone-config-passphrase", "age-identity"})
            self.assertEqual(volumes["config-memory"]["emptyDir"], {"medium": "Memory", "sizeLimit": "16Mi"})
            self.assertEqual(volumes["scratch"]["emptyDir"]["sizeLimit"], "512Mi")
            self.assertFalse(any("persistentVolumeClaim" in value or "hostPath" in value for value in volumes.values()))
            self.assertNotIn("serviceAccountName", spec)
            compile(cm["data"]["stage.py"], "rendered-stage.py", "exec")

    def test_render_rejects_unpinned_wrong_images_fixture_ids_and_extra_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = self.load_stage()
            consumer, evidence, digest = self.production_shape_fixture(root)
            for image in ("personal-server-book-pvc-backup:latest", "other-service@sha256:" + "1" * 64):
                with self.subTest(image=image), self.assertRaisesRegex(ValueError, "pinned_book_backup_image_required"):
                    stage.render(image, evidence, digest)
            evidence.write_text(evidence.read_text() + "private_extra=must-not-copy\n")
            with self.assertRaisesRegex(ValueError, "production_book_evidence_required"):
                stage.render("personal-server-book-pvc-backup@sha256:" + "1" * 64, evidence, consumer.sha(evidence.read_bytes()))

    def executable_fixture(self, root):
        stage, rendered = self.rendered(root)
        cm = rendered["items"][0]
        inputs = root / "stage"
        inputs.mkdir()
        for name, value in cm["data"].items():
            (inputs / name).write_text(value)
        work, memory, secret = (root / name for name in ("work", "memory", "secret"))
        for path in (work, memory, secret):
            path.mkdir(mode=0o700)
        (secret / "rclone-config").write_bytes(b"# encrypted fixture\nRCLONE_ENCRYPT_V0:\nfixture-encrypted\n")
        (secret / "rclone-config-passphrase").write_bytes(b"fixture-not-real-password")
        (secret / "age-identity").write_bytes(b"fixture-not-real-identity")
        namespace = {"__name__": "test_rendered_stage"}
        exec(compile(stage.STAGE_CODE, "rendered-stage.py", "exec"), namespace)
        namespace.update(WORK=work, MEMORY=memory, SECRET=secret, INPUT=inputs, memory_backed=lambda _path: True)
        request = json.loads(cm["data"]["request.json"])
        # The original fake production evidence has a placeholder cipher digest.
        # Bind a deterministic synthetic ciphertext for the no-network runner.
        cipher = b"synthetic-encrypted-artifact"
        request["artifact_digest"] = "sha256:" + __import__("hashlib").sha256(cipher).hexdigest()
        return namespace, request, cipher, work, memory, secret

    def test_staging_downloads_only_exact_artifact_and_emits_hashes_times_with_bounded_hold(self):
        import contextlib
        with tempfile.TemporaryDirectory() as directory:
            ns, request, cipher, work, memory, secret = self.executable_fixture(Path(directory))
            original = (secret / "rclone-config").read_bytes()
            commands = []
            plaintext = b"synthetic-private-tar-content"
            def runner(command, limit, timeout):
                commands.append((command, limit, timeout))
                if command[0] == "/usr/bin/rclone":
                    Path(command[-1]).write_bytes(cipher)
                else:
                    Path(command[command.index("-o") + 1]).write_bytes(plaintext)
            output = io.StringIO()
            with patch.object(ns["time"], "sleep") as sleep, contextlib.redirect_stdout(output):
                result = ns["stage"](request, runner=runner)
            sleep.assert_called_once_with(300)
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["plaintext_sha256"], "sha256:" + __import__("hashlib").sha256(plaintext).hexdigest())
            self.assertEqual(set(result["preparation"]), {"acquisition", "decryption"})
            first, cipher_limit, cipher_timeout = commands[0]
            self.assertEqual(first[-2], "gdrive:PersonalServer-encrypted-backups/book-memo/20261026T010203Z-12345.tar.age")
            self.assertEqual(first[first.index("--password-command") + 1],
                             "/usr/bin/cat /run/secrets/book-backup/rclone-config-passphrase")
            self.assertEqual(first[first.index("--cutoff-mode") + 1], "hard")
            self.assertEqual(cipher_limit, 128 * 1024 * 1024)
            self.assertEqual(cipher_timeout, 240)
            self.assertEqual(commands[1][1:], (256 * 1024 * 1024, 240))
            self.assertFalse(any(work.iterdir()))
            self.assertFalse(any(memory.iterdir()))
            self.assertEqual((secret / "rclone-config").read_bytes(), original)
            self.assertNotIn("synthetic-private", output.getvalue())
            self.assertNotIn("fixture-not-real", output.getvalue())
            self.assertNotIn(str(secret), output.getvalue())

    def test_wrong_cipher_never_decrypts_or_publishes_ready_and_removes_scratch(self):
        import contextlib
        with tempfile.TemporaryDirectory() as directory:
            ns, request, _, work, memory, _ = self.executable_fixture(Path(directory))
            calls = []
            def runner(command, *_args):
                calls.append(command)
                Path(command[-1]).write_bytes(b"wrong-cipher")
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ns["StageError"], "ciphertext_digest_mismatch"):
                ns["stage"](request, runner=runner, hold=False)
            self.assertEqual(len(calls), 1)
            self.assertFalse(any(work.iterdir()))
            self.assertFalse(any(memory.iterdir()))

    def test_stage_refuses_plaintext_config_nonmemory_refresh_and_oversized_plaintext(self):
        import contextlib
        for case in ("plaintext_config", "nonmemory", "oversized"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                ns, request, cipher, work, memory, secret = self.executable_fixture(Path(directory))
                if case == "plaintext_config":
                    (secret / "rclone-config").write_bytes(b"[gdrive]\npassword=must-not-copy")
                elif case == "nonmemory":
                    ns["memory_backed"] = lambda _path: False
                else:
                    ns["PLAIN_LIMIT"] = 2
                def runner(command, *_args):
                    if command[0] == "/usr/bin/rclone":
                        Path(command[-1]).write_bytes(cipher)
                    else:
                        Path(command[command.index("-o") + 1]).write_bytes(b"too-long")
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ns["StageError"]):
                    ns["stage"](request, runner=runner, hold=False)
                self.assertFalse(any(work.iterdir()))
                self.assertFalse(any(memory.iterdir()))

    def test_staging_subprocess_has_child_file_cap_timeout_no_tool_output_and_no_auth_env(self):
        from unittest.mock import MagicMock
        import resource
        import signal
        stage = self.load_stage()
        ns = {"__name__": "test_rendered_stage"}
        exec(compile(stage.STAGE_CODE, "rendered-stage.py", "exec"), ns)
        process = MagicMock()
        process.pid = 123456789
        process.wait.return_value = 0
        observed = {}
        def popen(command, **kwargs):
            observed.update(kwargs)
            with patch.object(resource, "getrlimit", return_value=(resource.RLIM_INFINITY, resource.RLIM_INFINITY)), patch.object(
                    resource, "setrlimit") as limit:
                kwargs["preexec_fn"]()
            limit.assert_called_once_with(resource.RLIMIT_FSIZE, (128, 128))
            return process
        with patch.object(ns["subprocess"], "Popen", side_effect=popen), patch.object(ns["os"], "killpg") as kill:
            ns["capped_command"](["/usr/bin/rclone", "fixed-args"], 128, 240)
        self.assertEqual(observed["stdout"], subprocess.DEVNULL)
        self.assertEqual(observed["stderr"], subprocess.DEVNULL)
        self.assertEqual(observed["stdin"], subprocess.DEVNULL)
        self.assertTrue(observed["start_new_session"])
        self.assertEqual(set(observed["env"]), {"PATH", "HOME", "LC_ALL", "TMPDIR"})
        self.assertEqual(process.wait.call_args_list[0].kwargs, {"timeout": 240})
        kill.assert_called_once_with(process.pid, signal.SIGKILL)


    def test_failed_provider_removes_partial_artifacts_and_refresh_temporaries_without_following_links(self):
        import contextlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ns, request, _, work, memory, _ = self.executable_fixture(root)
            outside = root / "outside-fixture"
            outside.write_text("must-preserve")
            def runner(*_args):
                (work / "backup.tar.age.partial").write_bytes(b"partial-private-cipher")
                (memory / "refresh.tmp").write_bytes(b"RCLONE_ENCRYPT_V0:\nrefresh-encrypted")
                (work / "outside-link").symlink_to(outside)
                raise ns["StageError"]("external_stage_failed")
            output = io.StringIO()
            with contextlib.redirect_stdout(output), self.assertRaisesRegex(ns["StageError"], "external_stage_failed"):
                ns["stage"](request, runner=runner, hold=False)
            self.assertFalse(any(work.iterdir()))
            self.assertFalse(any(memory.iterdir()))
            self.assertEqual(outside.read_text(), "must-preserve")
            self.assertEqual(output.getvalue(), "")

    def test_provider_plaintext_config_regression_fails_even_when_command_also_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            ns, request, _, work, memory, _ = self.executable_fixture(Path(directory))
            def runner(*_args):
                (memory / "rclone.conf").write_bytes(b"[gdrive]\npassword=private-data")
                raise ns["StageError"]("external_stage_failed")
            with self.assertRaisesRegex(ns["StageError"], "encrypted_config_invalid"):
                ns["stage"](request, runner=runner, hold=False)
            self.assertFalse(any(work.iterdir()))
            self.assertFalse(any(memory.iterdir()))

    def test_staging_timeout_kills_child_group_and_keeps_diagnostics_private(self):
        from unittest.mock import MagicMock
        stage = self.load_stage()
        ns = {"__name__": "test_rendered_stage"}
        exec(compile(stage.STAGE_CODE, "rendered-stage.py", "exec"), ns)
        process = MagicMock()
        process.pid = 123456789
        process.wait.side_effect = [subprocess.TimeoutExpired("private-command-diagnostic", 240), 0]
        with patch.object(ns["subprocess"], "Popen", return_value=process), patch.object(ns["os"], "killpg") as kill:
            with self.assertRaisesRegex(ns["StageError"], "external_stage_failed") as caught:
                ns["capped_command"](["/usr/bin/rclone", "fixed-args"], 128, 240)
        self.assertNotIn("private-command", str(caught.exception))
        kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)


class BookScratchOwnershipReadinessTests(unittest.TestCase):
    def test_real_readiness_requires_writable_owned_import_and_original_backup_stays_unchanged(self):
        import os
        import stat
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            consumer, archive, _, _, _ = IndependentBookRestoreTests().fixture(root)
            source = root / "source/book_memo.sqlite3"
            source_before, archive_before = source.read_bytes(), archive.read_bytes()
            destination = root / "owned-volume"
            destination.mkdir(mode=0o700)
            scratch = destination / "book_memo.sqlite3"
            shutil.copyfile(source, scratch)
            scratch.chmod(0o444)
            readiness_path = ROOT / "book-memo/app/services/readiness.py"
            spec = importlib.util.spec_from_file_location("actual_book_readiness", readiness_path)
            readiness = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(readiness)
            schema = lambda db: db.execute("SELECT count(*) FROM books").fetchone()[0] == 1
            if os.getuid() != 0:
                self.assertFalse(readiness.database_ready(scratch, schema))
            consumer.own_scratch_database(scratch, consumer.sha(source_before))
            self.assertEqual(stat.S_IMODE(scratch.stat().st_mode), 0o600)
            self.assertEqual(scratch.stat().st_uid, os.getuid())
            self.assertTrue(os.access(scratch, os.R_OK | os.W_OK))
            self.assertTrue(readiness.database_ready(scratch, schema))
            self.assertEqual(scratch.read_bytes(), source_before)
            self.assertEqual(source.read_bytes(), source_before)
            self.assertEqual(archive.read_bytes(), archive_before)
            self.assertEqual(list(destination.iterdir()), [scratch])
            self.assertTrue(consumer.RUNTIME_CONFIG["scratch_database_writable"])
            self.assertEqual(consumer.RUNTIME_CONFIG["scratch_database_mode"], "0600")
            self.assertTrue(consumer.RUNTIME_CONFIG["readonly_root"])

    def test_digest_failure_never_replaces_copied_DB_or_leaves_partial_owned_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            consumer, _, _, _, _ = IndependentBookRestoreTests().fixture(root)
            destination = root / "owned-volume"
            destination.mkdir(mode=0o700)
            scratch = destination / "book_memo.sqlite3"
            shutil.copyfile(root / "source/book_memo.sqlite3", scratch)
            scratch.chmod(0o444)
            before = scratch.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "scratch_import_digest_mismatch"):
                consumer.own_scratch_database(scratch, "sha256:" + "0" * 64)
            self.assertEqual(scratch.read_bytes(), before)
            self.assertEqual(scratch.stat().st_mode & 0o777, 0o444)
            self.assertEqual(list(destination.iterdir()), [scratch])

    def test_ownership_import_rejects_symlink_and_never_replaces_an_existing_stage_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            consumer, _, _, _, _ = IndependentBookRestoreTests().fixture(root)
            source = root / "source/book_memo.sqlite3"
            destination = root / "owned-volume"
            destination.mkdir(mode=0o700)
            scratch = destination / "book_memo.sqlite3"
            scratch.symlink_to(source)
            with self.assertRaises(OSError):
                consumer.own_scratch_database(scratch, consumer.sha(source.read_bytes()))
            scratch.unlink()
            shutil.copyfile(source, scratch)
            scratch.chmod(0o444)
            preexisting = destination / ".book-restore-owned.stage"
            preexisting.write_bytes(b"must-preserve")
            with self.assertRaises(FileExistsError):
                consumer.own_scratch_database(scratch, consumer.sha(source.read_bytes()))
            self.assertEqual(preexisting.read_bytes(), b"must-preserve")


    def test_actual_runtime_probe_composes_same_import_helper_before_database_readiness(self):
        import inspect
        import sys
        import types
        from unittest.mock import MagicMock
        consumer = IndependentBookRestoreTests().load_consumer()
        class FakeBase:
            def __init__(self, *_args):
                pass
        with patch.dict(sys.modules, {"container_runtime": types.SimpleNamespace(LocalDocker=FakeBase)}):
            engine = consumer.runtime_adapter("a" * 32, 999999999)
        engine._container = lambda _id: {"State": {"Running": True}}
        engine._record = lambda _id: ("owned", {"database_sha256": "sha256:" + "f" * 64})
        engine._verify_runtime = MagicMock()
        engine._run = MagicMock(return_value="{}")
        engine.functional_read("owned-id")
        args = engine._run.call_args.args[0]
        self.assertEqual(args[:4], ["exec", "owned-id", "/usr/local/bin/python", "-c"])
        script = args[4]
        compile(script, "actual-runtime-read-probe", "exec")
        self.assertIn(inspect.getsource(consumer.own_scratch_database), script)
        self.assertLess(script.index("request('/health');break"), script.index("own_scratch_database(db_path,sys.argv[1])"))
        self.assertLess(script.index("own_scratch_database(db_path,sys.argv[1])"), script.index("request('/ready');break"))
        self.assertEqual(args[5], "sha256:" + "f" * 64)
        self.assertEqual(engine._run.call_args.kwargs["cap"], 70)


if __name__ == "__main__":
    unittest.main()
