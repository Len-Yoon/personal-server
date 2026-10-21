import copy
import fcntl
from datetime import date, datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "infra/k8s/tools/slo-followup-verification.py"
SPEC = importlib.util.spec_from_file_location("slo_followup_verification", SOURCE)
TOOL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOL)
DAY = "2026-10-21"
SCHEDULED = "2026-10-20T17:15:00Z"


def record(day, *, version=False):
    item = {"date": day, "collected_at": datetime.fromisoformat(day).replace(tzinfo=TOOL.KST).astimezone(timezone.utc).isoformat(),
            "overall": "ok", "public_health": "ok", "portal_ready": "failed", "crawler_freshness": "ok",
            "portal_http": {"requests": 10, "server_errors": 0, "server_error_ratio": 0, "p95_seconds": 0.05}, "missing": []}
    if version:
        item["criteria_version"] = TOOL.CRITERIA
        item["collected_at"] = "2026-10-20T17:15:03Z"
    return item


class SloFollowupVerificationTests(unittest.TestCase):
    def setUp(self):
        # Production diagnostics are separately injected below; ordinary recovery
        # tests must never contact a real cluster or Prometheus endpoint.
        diagnostic = mock.patch.object(TOOL, "read_portal_context", return_value=TOOL.unknown_portal_context())
        self.diagnostic = diagnostic.start()
        self.addCleanup(diagnostic.stop)
        target = date.fromisoformat(DAY)
        self.rows = [record((target - timedelta(days=i)).isoformat()) for i in range(1, 31)]
        self.podspec = {"serviceAccountName": "slo-daily-evidence", "restartPolicy": "Never",
                        "containers": [{"name": "collector", "image": TOOL.IMAGE}]}
        self.cron = {"metadata": {"uid": "cron-uid"}, "spec": {"schedule": "15 2 * * *", "timeZone": "Asia/Seoul",
                     "suspend": False, "concurrencyPolicy": "Forbid", "jobTemplate": {"spec": {"template": {"spec": self.podspec}}}}}
        self.cm = {"metadata": {"uid": "cm-uid"}, "data": {"records.json": json.dumps(self.rows)}}
        self.relay = {"metadata": {"uid": "relay-uid"}, "spec": {"replicas": 1}, "status": {"readyReplicas": 1}}
        self.validator = TOOL.load_validator(ROOT / "infra/k8s/tools/slo-daily-evidence.py")
        self.jobs = []
        self.pods = []
        self.plan = TOOL.make_plan(DAY, get=self.get, validator=self.validator)
        self.new = record(DAY, version=True)
        self.cm["data"]["records.json"] = json.dumps([self.new, *self.rows[:29]])
        self.jobs = [{"metadata": {"uid": "job-uid", "name": "scheduled-job",
                      "annotations": {"batch.kubernetes.io/cronjob-scheduled-timestamp": SCHEDULED},
                      "ownerReferences": [{"kind": "CronJob", "uid": "cron-uid", "controller": True}]},
                      "spec": {"template": {"spec": copy.deepcopy(self.podspec)}},
                      "status": {"succeeded": 1, "startTime": "2026-10-20T17:15:02Z", "completionTime": "2026-10-20T17:15:05Z",
                                 "conditions": [{"type": "Complete", "status": "True"}]}}]
        self.pods = [{"metadata": {"uid": "pod-uid", "ownerReferences": [{"kind": "Job", "uid": "job-uid", "controller": True}]},
                     "status": {"phase": "Succeeded", "containerStatuses": [{"name": "collector", "restartCount": 0,
                         "imageID": TOOL.IMAGE, "state": {"terminated": {"exitCode": 0}}}]}}]

    def get(self, kind, name=None):
        if kind == "cronjob":
            return copy.deepcopy(self.cron)
        if kind == "configmap":
            return copy.deepcopy(self.cm)
        if kind == "deployment":
            return copy.deepcopy(self.relay)
        return {"items": copy.deepcopy(self.jobs if kind == "jobs" else self.pods)}

    def inspect(self):
        return TOOL.inspect(self.plan, get=self.get, validator=self.validator)

    def test_real_record_fields_preserved_with_exact_retention(self):
        result = self.inspect()
        self.assertEqual(result["verification"], "passed")
        self.assertEqual(result["preserved_rows"], 29)
        self.assertEqual(result["expired_dates"], ["2026-09-21"])
        self.assertEqual(result["new_record"]["portal_ready"], "failed")
        self.assertNotIn("2026-09-21", result["retained_row_hashes"])

    def test_each_existing_field_change_is_rejected(self):
        for key, value in [("crawler_freshness", "failed"), ("collected_at", "2026-10-19T20:00:00Z"),
                           ("criteria_version", TOOL.CRITERIA), ("unexpected", "changed")]:
            with self.subTest(key=key):
                rows = [self.new, *copy.deepcopy(self.rows[:29])]
                rows[1][key] = value
                self.cm["data"]["records.json"] = json.dumps(rows)
                with self.assertRaisesRegex(TOOL.Failed, "existing_records_changed"):
                    self.inspect()

    def test_wrong_expired_day_or_missing_retained_day_is_rejected(self):
        self.cm["data"]["records.json"] = json.dumps([self.new, *self.rows[1:]])
        with self.assertRaisesRegex(TOOL.Failed, "existing_records_changed"):
            self.inspect()

    def test_duplicate_dates_are_rejected(self):
        self.cm["data"]["records.json"] = json.dumps([self.new, *self.rows[:28], self.rows[0]])
        with self.assertRaisesRegex(TOOL.Failed, "duplicate_dates"):
            self.inspect()

    def test_new_criteria_required(self):
        del self.new["criteria_version"]
        self.cm["data"]["records.json"] = json.dumps([self.new, *self.rows[:29]])
        with self.assertRaisesRegex(TOOL.Failed, "new_criteria_missing"):
            self.inspect()

    def test_naive_or_outside_job_collection_time_rejected(self):
        for stamp in ["2026-10-20T17:15:03", "2026-10-20T17:15:01Z", "2026-10-20T17:15:06Z"]:
            with self.subTest(stamp=stamp):
                self.new["collected_at"] = stamp
                self.cm["data"]["records.json"] = json.dumps([self.new, *self.rows[:29]])
                with self.assertRaises(TOOL.Failed):
                    self.inspect()

    def test_manual_job_without_schedule_annotation_cannot_satisfy_check(self):
        del self.jobs[0]["metadata"]["annotations"]
        with self.assertRaises(TOOL.Pending):
            self.inspect()

    def test_foreign_owner_and_wrong_scheduled_day_are_ignored(self):
        for field in ["owner", "date"]:
            with self.subTest(field=field):
                saved = copy.deepcopy(self.jobs)
                if field == "owner":
                    self.jobs[0]["metadata"]["ownerReferences"][0]["uid"] = "foreign"
                else:
                    self.jobs[0]["metadata"]["annotations"]["batch.kubernetes.io/cronjob-scheduled-timestamp"] = "2026-10-19T17:15:00Z"
                with self.assertRaises(TOOL.Pending):
                    self.inspect()
                self.jobs = saved

    def test_duplicate_scheduled_jobs_are_rejected(self):
        self.jobs.append(copy.deepcopy(self.jobs[0]))
        with self.assertRaisesRegex(TOOL.Failed, "ambiguous_scheduled_job"):
            self.inspect()

    def test_failed_and_incomplete_jobs_are_not_success(self):
        self.jobs[0]["status"]["failed"] = 1
        with self.assertRaisesRegex(TOOL.Failed, "job_failed"):
            self.inspect()
        del self.jobs[0]["status"]["failed"]
        self.jobs[0]["status"]["conditions"] = []
        with self.assertRaises(TOOL.Pending):
            self.inspect()

    def test_job_image_or_command_drift_is_rejected(self):
        self.jobs[0]["spec"]["template"]["spec"]["containers"][0]["command"] = ["different"]
        with self.assertRaisesRegex(TOOL.Failed, "job_template_drift"):
            self.inspect()

    def test_actual_image_exit_and_restarts_are_verified(self):
        for key, value in [("imageID", "wrong@sha256:" + "0" * 64), ("restartCount", 1),
                           ("state", {"terminated": {"exitCode": 1}})]:
            with self.subTest(key=key):
                saved = copy.deepcopy(self.pods)
                self.pods[0]["status"]["containerStatuses"][0][key] = value
                with self.assertRaisesRegex(TOOL.Failed, "runtime_image_or_exit_mismatch"):
                    self.inspect()
                self.pods = saved

    def test_ambiguous_pods_are_rejected(self):
        self.pods.append(copy.deepcopy(self.pods[0]))
        with self.assertRaisesRegex(TOOL.Failed, "ambiguous_job_pods"):
            self.inspect()

    def test_cron_and_configmap_identity_and_spec_are_pinned(self):
        for target in ["cron_uid", "spec", "cm_uid"]:
            with self.subTest(target=target):
                cron, cm = copy.deepcopy(self.cron), copy.deepcopy(self.cm)
                if target == "cron_uid":
                    self.cron["metadata"]["uid"] = "new"
                elif target == "spec":
                    self.cron["spec"]["suspend"] = True
                else:
                    self.cm["metadata"]["uid"] = "new"
                with self.assertRaises(TOOL.Failed):
                    self.inspect()
                self.cron, self.cm = cron, cm

    def test_baseline_must_be_exact_previous_thirty_days(self):
        self.cm["data"]["records.json"] = json.dumps(self.rows[1:] + [record("2026-09-20")])
        with self.assertRaisesRegex(TOOL.Failed, "baseline_window_mismatch"):
            TOOL.make_plan(DAY, get=self.get, validator=self.validator)

    def test_source_missing_does_not_become_zero_days(self):
        self.cm["data"]["records.json"] = "not-json"
        with self.assertRaisesRegex(TOOL.Failed, "invalid_records"):
            self.inspect()

    def test_calendar_is_one_absolute_utc_date(self):
        units = TOOL.render_units(DAY)
        self.assertIn(b"OnCalendar=2026-10-20 17:20:00 UTC", units["timer"])
        self.assertIn(b"Persistent=true", units["timer"])
        self.assertIn(b"Restart=on-failure", units["service"])
        self.assertIn(b"StartLimitBurst=3", units["service"])
        self.assertIn(b"ExecCondition=/usr/bin/python3 -B %h/.local/lib/", units["service"])
        self.assertIn(TOOL.file_hash(SOURCE.read_bytes()).encode(), units["service"])
        self.assertNotIn(b"OnUnitActiveSec", units["timer"])

    def test_sender_uses_existing_relay_and_does_not_export_credentials(self):
        calls = []
        def execute(args, value=None):
            calls.append((args, value))
            return "slo_followup_delivery=PASS"
        self.assertTrue(TOOL.send(self.plan, "fixed result", get=self.get, execute=execute))
        self.assertEqual(len(calls), 1)
        args, value = calls[0]
        self.assertEqual(args[:len(TOOL.KUBE)], TOOL.KUBE)
        self.assertIn("deployment/sre-telegram-relay", args)
        self.assertEqual(json.loads(value), {"text": "fixed result"})
        self.assertNotIn("getUpdates", TOOL.SEND_CODE)
        self.assertNotIn("get", args[len(TOOL.KUBE):])

    def test_changed_relay_blocks_notification(self):
        self.relay["metadata"]["uid"] = "changed"
        execute = mock.Mock()
        with self.assertRaisesRegex(TOOL.Failed, "relay_drift"):
            TOOL.send(self.plan, "result", get=self.get, execute=execute)
        execute.assert_not_called()

    def test_command_errors_do_not_expose_raw_error_or_secret(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", "TOKEN https://private.invalid"))
        with self.assertRaisesRegex(TOOL.Failed, "^command_unavailable$"):
            TOOL.command(["fixed"], run=run)
        with self.assertRaisesRegex(TOOL.Failed, "^command_unavailable$"):
            TOOL.command(["fixed"], run=mock.Mock(side_effect=subprocess.TimeoutExpired("secret", 1)))

    def install_fixture(self, home, *, hook=None, clock_now=None):
        self.cm["data"]["records.json"] = json.dumps(self.rows)
        calls = []
        def execute(args, value=None):
            calls.append(args)
            if hook:
                answer = hook(args)
                if answer is not None:
                    return answer
            if args[0] == "loginctl":
                return "yes"
            if "is-system-running" in args:
                return "running"
            if "LoadState" in args:
                return "not-found"
            if "is-active" in args:
                return "active"
            if "is-enabled" in args:
                return "enabled"
            if "NextElapseUSecRealtime" in args:
                return "Tue 2026-10-20 17:20:00 UTC"
            if "ActiveState" in args:
                return "inactive"
            return ""
        with mock.patch.object(TOOL.os, "getuid", return_value=os.getuid() or 1000):
            TOOL.install(DAY, self.plan["records_sha256"], home=home, get=self.get, execute=execute,
                         now=datetime(2026, 10, 20, tzinfo=timezone.utc), clock_now=clock_now)
        self.cm["data"]["records.json"] = json.dumps([self.new, *self.rows[:29]])
        return calls

    def test_install_freezes_files_and_cleanup_removes_only_owned_exact_files(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            self.install_fixture(home)
            paths, plan, ph = TOOL.load_task(DAY, home)
            unrelated = paths["units"] / "keep.service"
            unrelated.write_text("user file")
            calls = []
            TOOL.cleanup(paths, plan, ph, execute=lambda args: calls.append(args) or "")
            self.assertFalse(paths["task"].exists())
            self.assertEqual(unrelated.read_text(), "user file")
            self.assertIn("disable", calls[0])
            self.assertIn("clean", calls[1])

    def test_unit_or_state_drift_blocks_file_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            self.install_fixture(home)
            paths, plan, ph = TOOL.load_task(DAY, home)
            timer = paths["units"] / (paths["unit"] + ".timer")
            timer.write_text("changed")
            with self.assertRaisesRegex(TOOL.Failed, "cleanup_drift"):
                TOOL.cleanup(paths, plan, ph, execute=lambda args: "")
            self.assertTrue((paths["task"] / "plan.json").exists())

    def test_symlink_state_or_unexpected_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            self.install_fixture(home)
            paths, _, _ = TOOL.load_task(DAY, home)
            (paths["task"] / "user-file").write_text("keep")
            with self.assertRaisesRegex(TOOL.Failed, "task_file_drift"):
                TOOL.load_task(DAY, home)

    def test_run_success_cleans_before_send_and_keeps_result(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            self.install_fixture(home)
            paths = TOOL.layout(DAY, home)
            calls = []
            def execute(args, value=None):
                calls.append(args)
                if "exec" in args:
                    self.assertFalse(paths["task"].exists())
                    return "slo_followup_delivery=PASS"
                return ""
            result = TOOL.run_task(DAY, home=home, now=lambda: TOOL.times(DAY)["start"], get=self.get, execute=execute)
            self.assertEqual((result["verification"], result["cleanup"], result["delivery"]), ("passed", "passed", "accepted"))
            self.assertEqual(json.loads(paths["result"].read_text()), result)
            self.assertEqual(sum("exec" in args for args in calls), 1)

    def test_deadline_still_cleans_and_sends_unconfirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            self.install_fixture(home)
            result = TOOL.run_task(DAY, home=home, now=lambda: TOOL.times(DAY)["deadline"], get=self.get,
                                   execute=lambda args, value=None: "slo_followup_delivery=PASS" if "exec" in args else "")
            self.assertEqual(result["verification"], "unconfirmed")
            self.assertEqual(result["reason"], "deadline_reached")
            self.assertEqual(result["cleanup"], "passed")

    def test_notification_ambiguous_does_not_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            self.install_fixture(home)
            calls = []
            def execute(args, value=None):
                calls.append(args)
                return "slo_followup_delivery=UNCONFIRMED" if "exec" in args else ""
            result = TOOL.run_task(DAY, home=home, now=lambda: TOOL.times(DAY)["start"], get=self.get, execute=execute)
            self.assertEqual(result["delivery"], "unconfirmed")
            self.assertEqual(sum("exec" in args for args in calls), 1)

    def test_invalid_existing_result_blocks_run(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            self.install_fixture(home)
            paths = TOOL.layout(DAY, home)
            paths["result"].parent.mkdir(parents=True)
            paths["result"].write_text('{"delivery":"attempting"}')
            execute = mock.Mock()
            with self.assertRaisesRegex(TOOL.Failed, "invalid_result"):
                TOOL.run_task(DAY, home=home, now=lambda: TOOL.times(DAY)["start"], get=self.get, execute=execute)
            execute.assert_not_called()

    def test_cleanup_failure_is_reported_and_sent_without_false_success(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            self.install_fixture(home)
            with mock.patch.object(TOOL, "cleanup", side_effect=TOOL.Failed("cleanup_drift")):
                result = TOOL.run_task(DAY, home=home, now=lambda: TOOL.times(DAY)["start"], get=self.get,
                                       sleep=lambda _: None, execute=lambda args, value=None: "slo_followup_delivery=PASS")
            self.assertEqual(result["cleanup"], "failed")
            self.assertEqual(result["cleanup_attempts"], 3)
            self.assertIn("임시 예약·파일 정리: 확인 필요", TOOL.message(result))

    def run_fixture(self, home, **kwargs):
        return TOOL.run_task(DAY, home=home, now=lambda: TOOL.times(DAY)["start"], get=self.get,
                             execute=lambda args, value=None: "slo_followup_delivery=PASS" if "exec" in args else "",
                             **kwargs)

    def test_interrupted_verification_resumes_from_durable_phase(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            self.install_fixture(home)
            jobs, self.jobs = self.jobs, []
            with self.assertRaises(SystemExit):
                self.run_fixture(home, sleep=mock.Mock(side_effect=SystemExit))
            paths = TOOL.layout(DAY, home)
            self.assertEqual(json.loads(paths["result"].read_text())["phase"], "verification")
            self.jobs = jobs
            result = self.run_fixture(home)
            self.assertEqual(result["verification"], "passed")
            self.assertFalse(paths["result"].with_suffix(".lock").exists())

    def test_partial_cleanup_resumes_after_baseline_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            self.install_fixture(home)
            original = Path.unlink
            def interrupt(path, *args, **kwargs):
                original(path, *args, **kwargs)
                if path.name == "plan.json":
                    raise SystemExit
            with mock.patch.object(Path, "unlink", interrupt), self.assertRaises(SystemExit):
                self.run_fixture(home)
            paths = TOOL.layout(DAY, home)
            self.assertEqual(json.loads(paths["result"].read_text())["phase"], "cleanup")
            self.assertFalse((paths["task"] / "plan.json").exists())
            result = self.run_fixture(home)
            self.assertEqual((result["cleanup"], result["delivery"]), ("passed", "accepted"))
            self.assertFalse(paths["task"].exists())

    def test_interrupted_send_never_sends_twice_and_complete_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            self.install_fixture(home)
            with mock.patch.object(TOOL, "send", side_effect=SystemExit) as sender, self.assertRaises(SystemExit):
                self.run_fixture(home)
            sender.assert_called_once()
            with mock.patch.object(TOOL, "send") as sender:
                result = self.run_fixture(home)
                self.assertEqual(result["delivery"], "unconfirmed")
                self.assertEqual(self.run_fixture(home), result)
            sender.assert_not_called()

    def test_completed_run_has_no_side_effects_on_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            self.install_fixture(home)
            result = self.run_fixture(home)
            with mock.patch.object(TOOL, "send") as sender, mock.patch.object(TOOL, "cleanup") as cleanup:
                self.assertEqual(self.run_fixture(home), result)
            sender.assert_not_called()
            cleanup.assert_not_called()

    def test_lock_collision_does_not_change_result_or_send(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            self.install_fixture(home)
            paths = TOOL.layout(DAY, home)
            paths["result"].parent.mkdir(parents=True)
            lock = paths["result"].with_suffix(".lock")
            with lock.open("w") as stream:
                os.chmod(lock, 0o600)
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with mock.patch.object(TOOL, "send") as sender, self.assertRaisesRegex(TOOL.Failed, "execution_locked"):
                    self.run_fixture(home)
                sender.assert_not_called()
            self.assertFalse(paths["result"].exists())

    def test_atomic_result_recovers_owned_stale_temporary_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "result.json"
            stale = path.with_name(".result.json.tmp-" + "a" * 32)
            stale.write_text("partial")
            TOOL.atomic_result(path, {"phase": "verification"})
            self.assertFalse(stale.exists())
            self.assertEqual(json.loads(path.read_text()), {"phase": "verification"})
            unsafe = path.with_name(".result.json.tmp-user-file")
            unsafe.write_text("keep")
            with self.assertRaisesRegex(TOOL.Failed, "unsafe_result_temp"):
                TOOL.atomic_result(path, {"phase": "changed"})
            self.assertEqual(unsafe.read_text(), "keep")

    def test_private_runtime_is_owned_immutable_reusable_and_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            self.install_fixture(home)
            paths, plan, ph = TOOL.load_task(DAY, home)
            path = paths["programs"] / (plan["program_sha256"] + ".py")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.read_bytes(), SOURCE.read_bytes())
            self.assertEqual(TOOL.install_program(paths["programs"], SOURCE.read_bytes()), (path, False))
            TOOL.cleanup(paths, plan, ph, execute=lambda args: "")
            self.assertTrue(path.exists())
            path.write_text("drift")
            with self.assertRaisesRegex(TOOL.Failed, "program_source_drift"):
                TOOL.install_program(paths["programs"], SOURCE.read_bytes())

    def test_source_fingerprint_and_permission_drift_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "program.py"
            private.write_bytes(SOURCE.read_bytes())
            expected = TOOL.file_hash(private.read_bytes())
            with mock.patch.object(TOOL, "__file__", str(private)):
                TOOL.verify_program(expected)
                with self.assertRaisesRegex(TOOL.Failed, "program_source_drift"):
                    TOOL.verify_program("0" * 64)
                private.chmod(0o777)
                with self.assertRaisesRegex(TOOL.Failed, "unsafe_file"):
                    TOOL.verify_program(expected)

    def test_early_execution_has_no_state_or_send(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            with self.assertRaisesRegex(TOOL.Failed, "execution_too_early"):
                TOOL.run_task(DAY, home=home, now=lambda: TOOL.times(DAY)["scheduled"])
            self.assertEqual(list(home.iterdir()), [])

    def test_cli_due_is_side_effect_free_before_start(self):
        with mock.patch.object(TOOL, "verify_program") as verify, mock.patch.object(TOOL, "datetime", wraps=datetime) as instant:
            instant.now.return_value = datetime(2026, 10, 20, 17, 15, tzinfo=timezone.utc)
            with mock.patch.object(TOOL, "run_task") as run:
                self.assertEqual(TOOL.main(["due", "--target-date", DAY,
                                          "--expected-program-sha256", "a" * 64]), 1)
            run.assert_not_called()
            verify.assert_called_once_with("a" * 64)

    def test_stale_baseline_install_creates_no_files(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            self.cm["data"]["records.json"] = json.dumps(self.rows)
            def execute(args, value=None):
                return "yes" if args[0] == "loginctl" else "running" if "is-system-running" in args else "not-found"
            with self.assertRaisesRegex(TOOL.Failed, "reviewed_baseline_changed"):
                TOOL.install(DAY, "0" * 64, home=home, get=self.get, execute=execute,
                             now=datetime(2026, 10, 20, tzinfo=timezone.utc))
            self.assertEqual(list(home.iterdir()), [])

    def test_no_linger_install_does_not_enable_it(self):
        with tempfile.TemporaryDirectory() as directory:
            execute = mock.Mock(return_value="no")
            with self.assertRaisesRegex(TOOL.Failed, "existing_linger_required"):
                TOOL.install(DAY, self.plan["records_sha256"], home=Path(directory).resolve(), execute=execute,
                             now=datetime(2026, 10, 20, tzinfo=timezone.utc))
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(execute.call_args.args[0][1], "show-user")

    def test_install_near_deadline_rolls_back_only_new_owned_files(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            paths = TOOL.layout(DAY, home)
            paths["units"].mkdir(parents=True)
            keep = paths["units"] / "user.service"
            keep.write_text("keep")
            with self.assertRaisesRegex(TOOL.Failed, "installation_window_too_short"):
                self.install_fixture(home, clock_now=lambda: TOOL.times(DAY)["start"] - timedelta(seconds=119))
            self.assertFalse(paths["task"].exists())
            self.assertEqual(list(paths["programs"].iterdir()), [])
            self.assertEqual(keep.read_text(), "keep")

    def test_ambiguous_activation_preserves_running_service_files(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            calls = []
            def hook(args):
                calls.append(args)
                if "enable" in args and "--now" in args:
                    raise TOOL.Failed("command_unavailable")
                if "ActiveState" in args:
                    return "activating"
            with self.assertRaisesRegex(TOOL.Failed, "installation_state_uncertain"):
                self.install_fixture(home, hook=hook)
            paths = TOOL.layout(DAY, home)
            self.assertTrue((paths["task"] / "plan.json").exists())
            self.assertEqual(len(list(paths["programs"].glob("*.py"))), 1)
            self.assertTrue((paths["units"] / (paths["unit"] + ".service")).exists())
            self.assertFalse(any("stop" in args for args in calls))

    def test_failed_install_preserves_reused_runtime_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            paths = TOOL.layout(DAY, home)
            runtime, _ = TOOL.install_program(paths["programs"], SOURCE.read_bytes())
            def hook(args):
                if "is-active" in args:
                    return "inactive"
            with self.assertRaisesRegex(TOOL.Failed, "timer_not_active"):
                self.install_fixture(home, hook=hook)
            self.assertEqual(runtime.read_bytes(), SOURCE.read_bytes())
            self.assertFalse(paths["task"].exists())

    def test_cli_install_requires_explicit_go_and_reviewed_hash(self):
        with mock.patch.object(TOOL, "install") as install, mock.patch("sys.stderr"):
            self.assertEqual(TOOL.main(["install", "--target-date", DAY]), 1)
        install.assert_not_called()

    def test_diagnostic_failure_does_not_block_verified_cleanup_or_send(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            self.install_fixture(home)
            self.diagnostic.side_effect = RuntimeError("private error")
            result = self.run_fixture(home)
            self.assertEqual((result["verification"], result["cleanup"], result["delivery"]),
                             ("passed", "passed", "accepted"))
            self.assertEqual(result["portal_context"], TOOL.unknown_portal_context())
            self.assertNotIn("private error", TOOL.message(result))

    def test_late_success_preserves_cleanup_time_instead_of_running_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            self.install_fixture(home)
            result = TOOL.run_task(DAY, home=home, now=lambda: TOOL.times(DAY)["deadline"] - timedelta(seconds=30),
                                   get=self.get, execute=lambda args, value=None: "slo_followup_delivery=PASS")
            self.diagnostic.assert_not_called()
            self.assertEqual((result["verification"], result["cleanup"], result["delivery"]), ("passed", "passed", "accepted"))


class PortalAvailabilityContextTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2026, 10, 20, 17, 15, 3, tzinfo=timezone.utc)
        self.row = record(DAY, version=True)
        self.deployment = {"metadata": {"uid": "deployment-uid", "creationTimestamp": "2026-09-01T00:00:00Z", "generation": 3},
                           "spec": {"replicas": 1}, "status": {"observedGeneration": 3, "readyReplicas": 1, "availableReplicas": 1}}
        spec = {"serviceAccountName": "portal-pvc-backup", "containers": [{"name": "backup", "image": "fixed@sha256:abc"}]}
        self.cron = {"metadata": {"uid": "backup-cron-uid", "creationTimestamp": "2026-09-01T00:00:00Z"},
                     "spec": {"jobTemplate": {"spec": {"template": {"spec": spec}}}}}
        self.av = self.matrix()
        self.de = self.matrix()
        self.av["data"]["result"][0]["values"][30][1] = "0"
        self.av["data"]["result"][0]["values"][31][1] = "0"
        self.de["data"]["result"][0]["values"][30][1] = "0"
        first = self.av["data"]["result"][0]["values"][30][0]
        last = self.av["data"]["result"][0]["values"][31][0]
        self.jobs = [{"metadata": {"uid": "backup-job-uid", "ownerReferences": [{"kind": "CronJob", "uid": "backup-cron-uid", "controller": True}]},
                      "spec": {"template": {"spec": copy.deepcopy(spec)}},
                      "status": {"startTime": datetime.fromtimestamp(first - 30, timezone.utc).isoformat(),
                                 "completionTime": datetime.fromtimestamp(last + 120, timezone.utc).isoformat(), "succeeded": 1,
                                 "conditions": [{"type": "Complete", "status": "True"}]}}]

    def matrix(self):
        return {"status": "success", "data": {"resultType": "matrix", "result": [{
            "metric": {"namespace": "personal-server", "deployment": "portal-web"},
            "values": [[self.at.timestamp() - 86400 + i * 30, "1"] for i in range(1, 2881)]}]}}

    def classify(self):
        return TOOL.classify_portal_context(self.row, self.deployment, self.cron, self.jobs,
                                            self.av, self.de, self.at + timedelta(minutes=5))

    def test_successful_backup_including_readiness_lag_is_explained(self):
        result = self.classify()
        self.assertEqual((result["current"], result["history"], result["zero_samples"]), ("available", "backup_overlap", 2))
        self.assertEqual(self.row["portal_ready"], "failed")
        lines = "\n".join(TOOL.portal_context_lines(result))
        self.assertIn("백업 계획 정지·복귀 구간과 일치", lines)
        self.assertIn("2026-10-21 02:20 KST 기준", lines)
        self.assertIn("원시 SLO 판정은 유지", lines)

    def test_recurrence_after_recovery_is_not_hidden_by_same_backup_job(self):
        self.av["data"]["result"][0]["values"][33][1] = "0"
        self.assertEqual(self.classify()["history"], "unexplained")

    def test_unavailability_before_pause_anchor_is_not_planned(self):
        self.av["data"]["result"][0]["values"][29][1] = "0"
        self.assertEqual(self.classify()["history"], "unexplained")

    def test_missing_failed_foreign_or_changed_backup_is_unknown(self):
        original = copy.deepcopy(self.jobs)
        for mutation in (lambda: self.jobs.clear(),
                         lambda: self.jobs[0]["status"].update(failed=1),
                         lambda: self.jobs[0]["status"].update(conditions=[]),
                         lambda: self.jobs[0]["metadata"]["ownerReferences"][0].update(uid="different"),
                         lambda: self.jobs[0]["metadata"]["ownerReferences"].append({"controller": True}),
                         lambda: self.jobs[0]["spec"]["template"]["spec"].update(serviceAccountName="other"),
                         lambda: self.jobs[0]["status"].update(completionTime="2026-10-20T18:00:00Z")):
            self.jobs = copy.deepcopy(original)
            mutation()
            with self.subTest(job=self.jobs):
                self.assertEqual(self.classify()["history"], "unknown")

    def test_current_unavailability_is_separate_from_historical_backup(self):
        self.deployment["status"].update(availableReplicas=0, readyReplicas=0)
        result = self.classify()
        self.assertEqual((result["history"], result["current"]), ("backup_overlap", "unavailable"))
        self.assertIn("장애 여부 확인 필요", "\n".join(TOOL.portal_context_lines(result)))

    def test_duplicate_overlapping_backup_jobs_do_not_prove_planned_pause(self):
        self.jobs.append(copy.deepcopy(self.jobs[0]))
        self.jobs[1]["metadata"]["uid"] = "second-job"
        self.assertEqual(self.classify()["history"], "unknown")

    def test_scaled_multiple_writers_during_pause_are_not_explained_as_backup(self):
        self.de["data"]["result"][0]["values"][31][1] = "2"
        self.assertEqual(self.classify()["history"], "unknown")
        self.deployment["spec"]["replicas"] = 2
        with self.assertRaisesRegex(TOOL.Failed, "invalid_deployment_status"):
            self.classify()

    def test_warning_or_info_partial_responses_are_not_positive_evidence(self):
        for key in ("warnings", "infos"):
            with self.subTest(key=key):
                self.av[key] = ["partial data"]
                with self.assertRaisesRegex(TOOL.Failed, "incomplete_diagnostic_matrix"):
                    self.classify()
                del self.av[key]

    def test_no_zero_samples_with_failed_raw_record_is_conflicting(self):
        self.av = self.matrix()
        with self.assertRaisesRegex(TOOL.Failed, "raw_diagnostic_conflict"):
            self.classify()
        self.row["portal_ready"] = "ok"
        self.assertEqual(self.classify()["history"], "none")

    def test_raw_ok_cannot_be_reconciled_with_zero(self):
        self.row["portal_ready"] = "ok"
        with self.assertRaisesRegex(TOOL.Failed, "raw_diagnostic_conflict"):
            self.classify()

    def test_matrix_rejects_gaps_repeated_future_nan_and_multiple_series(self):
        original = copy.deepcopy(self.av)
        for mutation in (lambda: self.av["data"]["result"][0]["values"].__delitem__(slice(2, 8)),
                         lambda: self.av["data"]["result"][0]["values"].__delitem__(slice(0, 4)),
                         lambda: self.av["data"]["result"][0]["values"][3].__setitem__(0, self.at.timestamp()+1),
                         lambda: self.av["data"]["result"][0]["values"][0].__setitem__(1, "NaN"),
                         lambda: self.av["data"]["result"].append(copy.deepcopy(self.av["data"]["result"][0]))):
            self.av = copy.deepcopy(original)
            mutation()
            with self.assertRaises(TOOL.Failed):
                self.classify()

    def test_desired_and_available_must_have_same_scrape_timestamps(self):
        self.de["data"]["result"][0]["values"][10][0] += 1
        with self.assertRaisesRegex(TOOL.Failed, "diagnostic_timestamp_mismatch"):
            self.classify()

    def test_recreation_stale_generation_and_old_record_are_unknown(self):
        self.deployment["status"]["observedGeneration"] = 2
        with self.assertRaisesRegex(TOOL.Failed, "stale_deployment_status"):
            self.classify()
        self.deployment["status"]["observedGeneration"] = 3
        self.deployment["metadata"]["creationTimestamp"] = self.at.isoformat()
        with self.assertRaisesRegex(TOOL.Failed, "deployment_identity_changed"):
            self.classify()
        self.deployment["metadata"]["creationTimestamp"] = "2026-09-01T00:00:00Z"
        self.row["collected_at"] = (self.at - timedelta(days=2)).isoformat()
        with self.assertRaisesRegex(TOOL.Failed, "stale_diagnostic_record"):
            self.classify()

    def test_read_failure_is_fixed_unknown_without_raw_labels_or_errors(self):
        get = mock.Mock(side_effect=RuntimeError("private-address secret-value"))
        result = TOOL.read_portal_context(self.row, get=get, query=mock.Mock(), now=self.at)
        self.assertEqual(result, TOOL.unknown_portal_context())
        self.assertNotIn("secret-value", json.dumps(result))

    def test_read_uses_fixed_resources_and_record_timestamp(self):
        def get(kind, name=None):
            return {"deployment": self.deployment, "cronjob": self.cron, "jobs": {"items": self.jobs}}[kind]
        calls = []
        def query(name, at):
            calls.append((name, at))
            return self.av if name == "available" else self.de
        result = TOOL.read_portal_context(self.row, get=get, query=query, now=self.at + timedelta(minutes=5))
        self.assertEqual(result["history"], "backup_overlap")
        self.assertEqual(calls, [("available", self.at), ("desired", self.at)])

    def test_diagnostic_get_cannot_access_secrets_or_mutate(self):
        with mock.patch.object(TOOL, "command") as call:
            with self.assertRaisesRegex(TOOL.Failed, "invalid_diagnostic_resource"):
                TOOL.portal_get("secret", "runtime")
            call.assert_not_called()
            call.return_value = "{}"
            TOOL.portal_get("deployment", "portal-web")
            args = call.call_args.args[0]
            self.assertIn("personal-server", args)
            self.assertEqual(args[args.index("get")+1], "deployment")

    def test_diagnostic_command_has_short_deadline_and_response_limit(self):
        with mock.patch.object(TOOL.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "{}", "")) as call:
            self.assertEqual(TOOL.diagnostic_command([*TOOL.KUBE, "get", "--raw", "fixed"]), "{}")
            self.assertEqual(call.call_args.kwargs["timeout"], 8)
            self.assertIn("--request-timeout=5s", call.call_args.args[0])
        with mock.patch.object(TOOL, "command", return_value="x" * 2_000_001):
            with self.assertRaisesRegex(TOOL.Failed, "diagnostic_response_too_large"):
                TOOL.diagnostic_command(["fixed"])


if __name__ == "__main__":
    unittest.main()
