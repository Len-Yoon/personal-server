"""Safety contracts for the service backup production target and reconciler."""

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/k8s/tools/service-pvc-backup-production-state.py"
spec = importlib.util.spec_from_file_location("backup_production_state", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
WRITER_GUARD = module.assert_single_writer


class ProductionStateTests(unittest.TestCase):
    def setUp(self):
        self.entries = module.validate_target()
        patcher = mock.patch.object(module, "assert_single_writer")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_target_has_exact_live_allowlist_and_bootstrap_stays_suspended(self):
        self.assertEqual({entry["name"] for entry in self.entries}, set(module.SCHEDULES))
        for entry in self.entries:
            self.assertTrue(entry["suspend"])
            self.assertIn("@sha256:", entry["image"])
            manifest = ROOT / "infra/k8s/backup-automation" / f"{module.SCHEDULES[entry['name']][1]}-pvc-backup-cronjob.yaml"
            self.assertIn("  suspend: true", manifest.read_text())

    def test_duplicate_name_and_unapproved_image_fail_closed(self):
        target = json.loads(module.TARGET.read_text())
        target["cronJobs"][1]["name"] = target["cronJobs"][0]["name"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "target.json"
            path.write_text(json.dumps(target))
            with self.assertRaises(module.StateError):
                module.validate_target(path)
            target["cronJobs"][1]["name"] = "youtube-pvc-backup"
            target["cronJobs"][1]["image"] = "evil.example/image@sha256:" + "a" * 64
            path.write_text(json.dumps(target))
            with self.assertRaises(module.StateError):
                module.validate_target(path)

    def test_full_image_reference_and_safety_state_checked_before_writes(self):
        entry = self.entries[0]
        cron = self._cron(entry)
        cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"] = "other.example/backup@" + entry["image"].split("@", 1)[1]
        with mock.patch.object(module, "get_json", side_effect=self._reader({entry["name"]: cron})):
            with self.assertRaisesRegex(module.StateError, "image drift"):
                module.inspect([entry], self._now())

    def test_command_or_pvc_drift_fails_live_check(self):
        entry = self.entries[0]
        cron = self._cron(entry)
        pod = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        pod["containers"][0]["command"] = ["/bin/true"]
        with mock.patch.object(module, "get_json", side_effect=self._reader({entry["name"]: cron})):
            with self.assertRaisesRegex(module.StateError, "template drift"):
                module.inspect([entry], self._now())
        pod["containers"][0]["command"] = ["/opt/personal-server/book-pvc-backup-verify.py", "--go"]
        pod["volumes"][0]["persistentVolumeClaim"]["claimName"] = "other-data"
        with mock.patch.object(module, "get_json", side_effect=self._reader({entry["name"]: cron})):
            with self.assertRaisesRegex(module.StateError, "template drift"):
                module.inspect([entry], self._now())

    def test_active_backup_job_blocks_reconciliation(self):
        entry = self.entries[0]
        def read(namespace, resource, name=None):
            if resource == "jobs":
                return {"items": [{"metadata": {"name": "custom-manual-backup"},
                                   "spec": {"template": {"spec": {"serviceAccountName": "book-pvc-backup"}}},
                                   "status": {"active": 1}}]}
            raise AssertionError("unsafe read after active Job")
        with mock.patch.object(module, "get_json", side_effect=read):
            with self.assertRaisesRegex(module.StateError, "Job is active"):
                module.inspect([entry], self._now())

    def test_unimported_image_blocks_live_check(self):
        with mock.patch.object(module, "command", return_value="unrelated-image\n"):
            with self.assertRaisesRegex(module.StateError, "not imported"):
                module.assert_images_available(self.entries)

    def test_apply_requires_exact_clean_main_commit(self):
        sha = "a" * 40
        with mock.patch.object(module, "command", side_effect=[sha + "\n", sha + "\n", " M AGENTS.md\n"]):
            with self.assertRaisesRegex(module.StateError, "clean"):
                module.git_guard(sha)

    def test_invalid_runtime_marker_or_duplicate_docker_writer_blocks(self):
        with mock.patch.object(module, "command", side_effect=["book-memo=compose\nyoutube-memo=k3s\ncrawler-worker=k3s\n"]):
            with self.assertRaisesRegex(module.StateError, "runtime marker"):
                WRITER_GUARD()
        marker = "book-memo=k3s\nyoutube-memo=k3s\ncrawler-worker=k3s\n"
        with mock.patch.object(module, "command", side_effect=[marker, "book-memo\n"]):
            with self.assertRaisesRegex(module.StateError, "duplicate Docker"):
                WRITER_GUARD()

    def test_noop_apply_has_zero_kubernetes_writes(self):
        entries = self.entries
        live = [self._cron(entry) for entry in entries]
        with mock.patch.object(module, "inspect", return_value=live), mock.patch.object(module, "kubectl") as kubectl:
            self.assertEqual(module.apply(entries, live, self._now()), 0)
            kubectl.assert_not_called()

    def test_apply_only_patches_deadline_and_suspend_with_cas(self):
        entries = self.entries
        live = [self._cron(entry) for entry in entries]
        live[0]["spec"]["suspend"] = False
        after = [self._cron(entry) for entry in entries]
        with mock.patch.object(module, "inspect", side_effect=[live, after]), mock.patch.object(module, "kubectl") as kubectl:
            self.assertEqual(module.apply(entries, live, self._now()), 1)
        self.assertEqual(kubectl.call_count, 2)
        dry, real = (call.args for call in kubectl.call_args_list)
        self.assertIn("--dry-run=server", dry)
        self.assertNotIn("--dry-run=server", real)
        patch = json.loads(dry[dry.index("-p") + 1])
        writes = [op["path"] for op in patch if op["op"] != "test"]
        self.assertEqual(writes, ["/spec/suspend"])
        self.assertIn("/metadata/resourceVersion", [op["path"] for op in patch if op["op"] == "test"])

    def test_apply_stops_after_failed_dry_run(self):
        entries = self.entries
        live = [self._cron(entry) for entry in entries]
        live[0]["spec"]["suspend"] = False
        with mock.patch.object(module, "inspect", return_value=live), mock.patch.object(module, "kubectl", side_effect=module.StateError("conflict")) as kubectl:
            with self.assertRaises(module.StateError):
                module.apply(entries, live, self._now())
            self.assertEqual(kubectl.call_count, 1)

    def test_missed_schedule_window_blocks_apply(self):
        with self.assertRaisesRegex(module.StateError, "window"):
            module.assert_quiet_window(self.entries[0], datetime(2026, 9, 28, 4, 2, tzinfo=ZoneInfo("Asia/Seoul")))

    @staticmethod
    def _now():
        return datetime(2026, 9, 28, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))

    @staticmethod
    def _cron(entry):
        service = module.SCHEDULES[entry["name"]][1]
        short = entry["name"].split("-", 1)[0]
        return {
            "metadata": {"name": entry["name"], "namespace": "personal-server", "uid": "uid", "resourceVersion": "42"},
            "spec": {
                "schedule": entry["schedule"], "timeZone": entry["timeZone"],
                "startingDeadlineSeconds": entry["startingDeadlineSeconds"], "suspend": entry["suspend"],
                "concurrencyPolicy": "Forbid", "successfulJobsHistoryLimit": 1, "failedJobsHistoryLimit": 1,
                "jobTemplate": {"spec": {"backoffLimit": 0, "activeDeadlineSeconds": 14400,
                    "ttlSecondsAfterFinished": 86400,
                    "template": {"spec": {
                    "serviceAccountName": entry["name"], "restartPolicy": "Never", "terminationGracePeriodSeconds": 300,
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                        "seccompProfile": {"type": "RuntimeDefault"}},
                    "initContainers": [{"name": "prepare-scratch", "image": entry["image"], "imagePullPolicy": "Never",
                                        "command": ["/bin/sh", "-ec", "chown 10001:10001 /work && chmod 0700 /work"],
                                        "securityContext": {"runAsUser": 0, "readOnlyRootFilesystem": True,
                                                            "allowPrivilegeEscalation": False},
                                        "volumeMounts": [{"name": "work", "mountPath": "/work"}]}],
                    "containers": [{"name": "backup", "image": entry["image"], "imagePullPolicy": "Never",
                                    "command": [f"/opt/personal-server/{entry['name']}-verify.py", "--go"],
                                    "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                                        "readOnlyRootFilesystem": True,
                                                        "allowPrivilegeEscalation": False},
                                    "volumeMounts": [
                                        {"name": short + "-data", "mountPath": f"/data/{service}", "readOnly": True},
                                        {"name": "credentials", "mountPath": f"/run/secrets/{short}-backup", "readOnly": True},
                                        {"name": "work", "mountPath": "/work"},
                                    ]}],
                    "volumes": [
                        {"name": short + "-data", "persistentVolumeClaim": {"claimName": module.SCHEDULES[entry["name"]][2], "readOnly": True}},
                        {"name": "credentials", "secret": {"secretName": f"{service}-pvc-backup-runtime",
                                                           "optional": False, "defaultMode": 292,
                                                           "items": [{"key": key} for key in (
                                                               "rclone-config", "rclone-config-passphrase",
                                                               "age-recipient", "age-identity")]}},
                        {"name": "work", "emptyDir": {"sizeLimit": "10Gi"}},
                    ],
                }}}},
            },
        }

    @staticmethod
    def _reader(crons):
        def read(namespace, resource, name=None):
            if resource == "jobs":
                return {"items": []}
            if resource == "cronjob":
                return crons[name]
            if resource == "deployment":
                return {"spec": {"replicas": 1}, "status": {"availableReplicas": 1}}
            if resource == "pvc":
                return {"status": {"phase": "Bound"}}
            if resource == "configmap":
                return {"data": {"status": "completed", "lock_run_id": "", "evidence": "backup_status=success"}}
            raise AssertionError(resource)
        return read


if __name__ == "__main__":
    unittest.main()
