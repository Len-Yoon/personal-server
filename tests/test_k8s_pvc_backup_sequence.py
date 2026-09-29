import importlib.util
import copy
import json
import os
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import yaml


SCRIPT = Path(__file__).resolve().parents[1] / "infra/k8s/tools/pvc-backup-sequence.py"
spec = importlib.util.spec_from_file_location("pvc_backup_sequence", SCRIPT)
sequence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sequence)

NOW = datetime(2026, 9, 29, 0, 30, tzinfo=sequence.KST)


def cronjob(stage):
    config = sequence.STAGES[stage]
    filenames = {"portal": "portal-pvc-backup-cronjob.yaml", "book": "book-memo-pvc-backup-cronjob.yaml",
                 "youtube": "youtube-memo-pvc-backup-cronjob.yaml", "crawler": "crawler-worker-pvc-backup-cronjob.yaml"}
    path = SCRIPT.parents[1] / "backup-automation" / filenames[stage]
    data = next(item for item in yaml.safe_load_all(path.read_text()) if item.get("kind") == "CronJob")
    data["metadata"]["uid"] = stage + "-uid"
    data["spec"]["suspend"] = True
    pod = data["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    for container in pod["containers"] + pod["initContainers"]:
        container["image"] = config.image
    return data


class FakeAPI:
    def __init__(self, outcomes=None, create_ambiguous=False, status_completed_at="2026-09-28T15:31:00Z"):
        self.jobs = {}
        self.cronjobs = {stage: cronjob(stage) for stage in sequence.ORDER}
        self.state = {"metadata": {"name": sequence.STATE_NAME, "namespace": "personal-server", "resourceVersion": "1"}, "data": {"active_from": "2026-09-29"}}
        self.outcomes = outcomes or {}
        self.create_ambiguous = create_ambiguous
        self.status_completed_at = status_completed_at
        self.created = []
        self.state_writes = []

    def get_cronjob(self, stage): return self.cronjobs[stage]
    def list_jobs(self): return list(self.jobs.values())
    def get_job(self, name): return self.jobs.get(name)
    def create_job(self, job):
        self.created.append(job["metadata"]["name"])
        stage = job["metadata"]["labels"]["personal-server/backup-stage"]
        job["metadata"]["creationTimestamp"] = "2026-09-28T15:30:00Z"
        job["metadata"]["uid"] = job["metadata"]["name"] + "-uid"
        job["status"] = {"conditions": [{"type": self.outcomes.get(stage, "Complete"), "status": "True"}]}
        self.jobs[job["metadata"]["name"]] = job
        if self.create_ambiguous:
            raise sequence.APIError("response lost")
        return job
    def get_state(self): return self.state
    def patch_state(self, old_version, data):
        assert old_version == self.state["metadata"]["resourceVersion"]
        self.state["metadata"]["resourceVersion"] = str(int(old_version) + 1)
        self.state["data"] = data
        self.state_writes.append(json.loads(json.dumps(data)))
    def get_backup_status(self, stage):
        return {"data": {"status": "failed", "completed_at": self.status_completed_at}}


class SequenceTests(unittest.TestCase):
    def run_sequence(self, api, at=NOW):
        return sequence.run(api, now=lambda: at, sleep=lambda _: None, poll_seconds=0)

    def test_fixed_order_and_state_update(self):
        api = FakeAPI()
        api.state["data"]["active_from"] = "2026-09-29"
        self.assertEqual(self.run_sequence(api), 0)
        self.assertEqual(api.created, [f"{sequence.STAGES[s].name}-20260929" for s in sequence.ORDER])
        self.assertEqual(api.state["data"]["status"], "completed")
        self.assertEqual(api.state["data"]["current"], "none")
        self.assertEqual(api.state["data"]["active_from"], "2026-09-29")
        self.assertEqual(json.loads(api.state["data"]["results"]), {s: "passed" for s in sequence.ORDER})

    def test_terminal_failure_continues_and_classifies_runner(self):
        api = FakeAPI({"book": "Failed"})
        self.assertEqual(self.run_sequence(api), 0)
        self.assertEqual(json.loads(api.state["data"]["results"])["book"], "runner_failed")
        self.assertEqual(len(api.created), 4)

    def test_failed_job_without_new_runner_status_is_classified_before_runner(self):
        api = FakeAPI({"book": "Failed"}, status_completed_at="2026-09-28T15:29:00Z")
        self.assertEqual(self.run_sequence(api), 0)
        self.assertEqual(json.loads(api.state["data"]["results"])["book"], "failed_before_runner")

    def test_existing_job_with_modified_spec_is_not_attached(self):
        api = FakeAPI()
        job = sequence.make_job("portal", NOW.date(), api.cronjobs["portal"], *sequence.validate_cronjob("portal", api.cronjobs["portal"]))
        job["metadata"]["creationTimestamp"] = "2026-09-28T15:30:00Z"
        job["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
        job["spec"] = copy.deepcopy(job["spec"])
        job["spec"]["template"]["spec"]["containers"][0]["command"] = ["/bin/false"]
        api.jobs[job["metadata"]["name"]] = job
        self.assertEqual(self.run_sequence(api), 1)
        self.assertEqual(api.created, [])

    def test_create_response_lost_requeries_without_second_create(self):
        api = FakeAPI(create_ambiguous=True)
        self.assertEqual(self.run_sequence(api), 0)
        self.assertEqual(len(api.created), 4)

    def test_nonterminal_job_blocks_later_stages(self):
        api = FakeAPI({"portal": "Running"})
        self.assertEqual(self.run_sequence(api), 1)
        self.assertEqual(api.created, ["portal-pvc-backup-20260929"])
        self.assertEqual(api.state["data"]["status"], "blocked")

    def test_job_uid_change_during_poll_blocks_later_stages(self):
        api = FakeAPI({"portal": "Running"})
        original_get = api.get_job
        reads = 0
        def replaced_job(name):
            nonlocal reads
            job = original_get(name)
            if job:
                reads += 1
                if reads == 2:
                    job["metadata"]["uid"] = "replacement-uid"
                    job["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
            return job
        api.get_job = replaced_job
        self.assertEqual(sequence.run(api, now=lambda: NOW, sleep=lambda _: None, poll_seconds=1), 1)
        self.assertEqual(api.created, ["portal-pvc-backup-20260929"])

    def test_kubectl_requests_have_bounded_timeouts(self):
        def timed_out(command, **kwargs):
            self.assertIn("--request-timeout=20s", command)
            self.assertEqual(kwargs["timeout"], 25)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        with mock.patch.object(sequence.subprocess, "run", side_effect=timed_out):
            with self.assertRaises(sequence.APIError):
                sequence.KubectlAPI().get_state()

    def test_existing_active_cronjob_job_blocks_before_any_create(self):
        api = FakeAPI()
        api.jobs["book-pvc-backup-legacy"] = {"metadata": {"name": "book-pvc-backup-legacy"}, "status": {"active": 1}}
        self.assertEqual(self.run_sequence(api), 1)
        self.assertEqual(api.created, [])

    def test_manual_active_backup_service_account_blocks_before_any_create(self):
        api = FakeAPI()
        api.jobs["manual-backup"] = {"metadata": {"name": "manual-backup"}, "spec": {"template": {"spec": {
            "serviceAccountName": "book-pvc-backup"}}}, "status": {"active": 1}}
        self.assertEqual(self.run_sequence(api), 1)
        self.assertEqual(api.created, [])

    def test_restart_reattaches_verified_current_active_job(self):
        api = FakeAPI()
        template, digest = sequence.validate_cronjob("portal", api.cronjobs["portal"])
        api.create_job(sequence.make_job("portal", NOW.date(), api.cronjobs["portal"], template, digest))
        job = next(iter(api.jobs.values()))
        job["status"] = {"active": 1}
        api.created.clear()
        api.state["data"].update({"run_date": "2026-09-29", "status": "running", "current": "portal",
                                  "results": json.dumps({s: "skipped" for s in sequence.ORDER})})
        original_get = api.get_job
        def complete_on_attach(name):
            found = original_get(name)
            if found:
                found["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
            return found
        api.get_job = complete_on_attach
        self.assertEqual(self.run_sequence(api), 0)
        self.assertEqual(api.created, [f"{sequence.STAGES[s].name}-20260929" for s in sequence.ORDER[1:]])

    def test_before_0030_cannot_start(self):
        api = FakeAPI()
        self.assertEqual(self.run_sequence(api, NOW.replace(minute=29)), 1)
        self.assertEqual(api.created, [])

    def test_before_activation_date_cannot_start(self):
        api = FakeAPI()
        api.state["data"]["active_from"] = "2026-09-30"
        self.assertEqual(self.run_sequence(api), 1)
        self.assertEqual(api.created, [])

    def test_non_date_activation_marker_cannot_start(self):
        api = FakeAPI()
        api.state["data"]["active_from"] = "2026-09-28T15:00:00Z"
        self.assertEqual(self.run_sequence(api), 1)
        self.assertEqual(api.created, [])

    def test_missing_preexisting_state_cannot_start(self):
        api = FakeAPI()
        api.state["metadata"]["name"] = "other"
        self.assertEqual(self.run_sequence(api), 1)
        self.assertEqual(api.created, [])

    def test_unavailable_job_api_blocks_before_create(self):
        api = FakeAPI()
        def unavailable():
            raise sequence.APIError("API unavailable")
        api.list_jobs = unavailable
        self.assertEqual(self.run_sequence(api), 1)
        self.assertEqual(api.created, [])

    def test_check_accepts_existing_state_without_active_from_and_is_read_only(self):
        api = FakeAPI()
        api.state["data"].pop("active_from")
        self.assertEqual(sequence.check(api), 0)
        self.assertEqual(api.created, [])
        self.assertEqual(api.state_writes, [])

    def test_check_rejects_manual_active_backup_job(self):
        api = FakeAPI()
        api.jobs["manual-backup"] = {"metadata": {"name": "manual-backup"}, "spec": {"template": {"spec": {
            "serviceAccountName": "portal-pvc-backup"}}}, "status": {"active": 1}}
        self.assertEqual(sequence.check(api), 1)
        self.assertEqual(api.created, [])
        self.assertEqual(api.state_writes, [])

    def test_check_rejects_drifted_template_and_missing_state(self):
        api = FakeAPI()
        api.cronjobs["youtube"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"] = "unexpected"
        self.assertEqual(sequence.check(api), 1)
        self.assertEqual(api.state_writes, [])
        api = FakeAPI()
        api.state["metadata"]["name"] = "other"
        self.assertEqual(sequence.check(api), 1)
        self.assertEqual(api.state_writes, [])

    def test_user_state_lock_is_private_regular_file(self):
        with tempfile.TemporaryDirectory() as root:
            with sequence.local_lock(Path(root)):
                state_dir = Path(root) / ".local/state/personal-server"
                lock = state_dir / "pvc-backup-sequence.lock"
                self.assertEqual(stat.S_IMODE(state_dir.stat().st_mode), 0o700)
                self.assertTrue(lock.is_file())
                self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
                self.assertEqual(lock.stat().st_uid, os.getuid())

    def test_lock_rejects_symlink_and_directory(self):
        with tempfile.TemporaryDirectory() as root:
            with sequence.local_lock(Path(root)):
                pass
            lock = Path(root) / ".local/state/personal-server/pvc-backup-sequence.lock"
            lock.unlink()
            lock.symlink_to(Path(root) / "elsewhere")
            with self.assertRaises(OSError):
                with sequence.local_lock(Path(root)):
                    pass
            lock.unlink()
            lock.mkdir()
            with self.assertRaises(OSError):
                with sequence.local_lock(Path(root)):
                    pass

    def test_lock_rejects_insecure_directory_or_foreign_owner(self):
        with tempfile.TemporaryDirectory() as root:
            with sequence.local_lock(Path(root)):
                pass
            state_dir = Path(root) / ".local/state/personal-server"
            state_dir.chmod(0o755)
            with self.assertRaises(OSError):
                with sequence.local_lock(Path(root)):
                    pass
            state_dir.chmod(0o700)
            with mock.patch.object(sequence.os, "getuid", return_value=os.getuid() + 1):
                with self.assertRaises(OSError):
                    with sequence.local_lock(Path(root)):
                        pass

    def test_lock_rejects_insecure_or_foreign_file(self):
        with tempfile.TemporaryDirectory() as root:
            with sequence.local_lock(Path(root)):
                pass
            lock = Path(root) / ".local/state/personal-server/pvc-backup-sequence.lock"
            lock.chmod(0o644)
            with self.assertRaises(OSError):
                with sequence.local_lock(Path(root)):
                    pass
            lock.chmod(0o600)
            real_fstat = os.fstat
            def foreign_file(fd):
                result = real_fstat(fd)
                if stat.S_ISREG(result.st_mode):
                    fields = list(result)
                    fields[4] += 1
                    return os.stat_result(fields)
                return result
            with mock.patch.object(sequence.os, "fstat", side_effect=foreign_file):
                with self.assertRaises(OSError):
                    with sequence.local_lock(Path(root)):
                        pass

    def test_check_lock_cli_acquires_private_lock_without_kubernetes(self):
        with tempfile.TemporaryDirectory() as root:
            with (mock.patch.object(sequence.sys, "argv", ["pvc-backup-sequence.py", "--check-lock"]),
                  mock.patch.object(sequence.Path, "home", return_value=Path(root)),
                  mock.patch.object(sequence, "KubectlAPI", side_effect=AssertionError("Kubernetes request"))):
                self.assertEqual(sequence.main(), 0)
            self.assertTrue((Path(root) / ".local/state/personal-server/pvc-backup-sequence.lock").is_file())

    def test_check_lock_cli_rejects_insecure_directory(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / ".local/state/personal-server"
            folder.mkdir(parents=True)
            folder.chmod(0o755)
            with (mock.patch.object(sequence.sys, "argv", ["pvc-backup-sequence.py", "--check-lock"]),
                  mock.patch.object(sequence.Path, "home", return_value=Path(root)),
                  mock.patch.object(sequence, "KubectlAPI", side_effect=AssertionError("Kubernetes request"))):
                self.assertEqual(sequence.main(), 1)

    def test_cutoff_skips_new_stages(self):
        api = FakeAPI()
        self.assertEqual(self.run_sequence(api, NOW.replace(hour=6)), 0)
        self.assertEqual(api.created, [])
        self.assertEqual(json.loads(api.state["data"]["results"]), {s: "skipped" for s in sequence.ORDER})

    def test_cutoff_reconciles_existing_terminal_job_without_new_stages(self):
        api = FakeAPI()
        template, digest = sequence.validate_cronjob("portal", api.cronjobs["portal"])
        api.create_job(sequence.make_job("portal", NOW.date(), api.cronjobs["portal"], template, digest))
        api.created.clear()
        self.assertEqual(self.run_sequence(api, NOW.replace(hour=6)), 0)
        self.assertEqual(api.created, [])
        self.assertEqual(json.loads(api.state["data"]["results"])["portal"], "passed")
        self.assertEqual(json.loads(api.state["data"]["results"])["book"], "skipped")

    def test_unknown_template_blocks_before_create(self):
        api = FakeAPI()
        api.cronjobs["portal"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"] = ["/bin/sh", "-c", "unsafe"]
        self.assertEqual(self.run_sequence(api), 1)
        self.assertEqual(api.created, [])
        self.assertEqual(api.state["data"]["status"], "blocked")

    def test_security_and_secret_template_drift_blocks_before_any_create(self):
        cases = ("envFrom", "hostNetwork", "fsGroup", "privileged", "mountPath", "secretKey", "defaultMode", "initArgs", "portalDigest")
        for case in cases:
            with self.subTest(case=case):
                api = FakeAPI()
                pod = api.cronjobs["portal"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
                main = pod["containers"][0]
                secret = next(v["secret"] for v in pod["volumes"] if "secret" in v)
                if case == "envFrom": main["envFrom"] = [{"secretRef": {"name": "unexpected"}}]
                elif case == "hostNetwork": pod["hostNetwork"] = True
                elif case == "fsGroup": pod["securityContext"]["fsGroup"] = 0
                elif case == "privileged": main["securityContext"]["privileged"] = True
                elif case == "mountPath": main["volumeMounts"][0]["mountPath"] = "/other"
                elif case == "secretKey": secret["items"][0]["key"] = "unexpected"
                elif case == "defaultMode": secret["defaultMode"] = 0o777
                elif case == "initArgs": pod["initContainers"][0]["args"] = ["unsafe"]
                elif case == "portalDigest": main["image"] = "docker.io/library/personal-server-portal-pvc-backup@sha256:" + "a" * 64
                self.assertEqual(self.run_sequence(api), 1)
                self.assertEqual(api.created, [])

    def test_same_day_rerun_preserves_terminal_results(self):
        api = FakeAPI()
        self.assertEqual(self.run_sequence(api), 0)
        self.assertEqual(self.run_sequence(api), 0)
        self.assertEqual(len(api.created), 4)


if __name__ == "__main__":
    unittest.main()
