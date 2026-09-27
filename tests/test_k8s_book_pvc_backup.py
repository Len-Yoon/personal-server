"""Book PVC backup contracts; no production Kubernetes resources are touched."""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import sqlite3
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
    def test_check_refuses_stale_lock(self):
        runner = load_runner()
        with patch.object(runner, "k8s_json", return_value={"data": {"lock_run_id": "previous-run"}}):
            with self.assertRaises(runner.BackupError):
                runner.assert_lock_available()

    def run_full_backup(self, *, fail_remote_restore=False, lock_busy=False,
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
                runner, "MOUNT", source
            ), patch.object(runner, "SECRET", secret), patch.object(
                runner, "wait_for_pods_absent", side_effect=no_pods
            ), patch.object(runner, "wait_for_ready", side_effect=ready), patch.object(
                runner.tempfile, "TemporaryDirectory", return_value=ScratchDirectory()
            ):
                if any((fail_remote_restore, lock_busy, fail_success_patch, fail_scale,
                        pods_stay, fail_writer_recovery, missing_secret, fail_cleanup,
                        replacement_deployment, missing_database)):
                    with self.assertRaises((runner.BackupError, OSError)):
                        runner.run_go()
                else:
                    runner.run_go()
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


if __name__ == "__main__":
    unittest.main()
