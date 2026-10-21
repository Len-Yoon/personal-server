import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("notification_followup", ROOT / "infra/k8s/tools/notification-followup-verification.py")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
UTC = timezone.utc


class Fixture:
    def __init__(self):
        self.at = datetime(2026, 10, 21, 2, tzinfo=UTC)
        self.image = "docker.io/library/personal-server-sre-telegram-relay@sha256:" + "a" * 64
        self.app = "b" * 64
        self.dep = {"metadata": {"uid": "relay"}, "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
                    "template": {"spec": {"containers": [{"image": self.image}]}}}}
        self.relaypod = {"metadata": {"name": "relay-pod", "uid": "relay-pod-uid", "ownerReferences": self.owner("ReplicaSet", "rs")},
                         "status": {"phase": "Running", "containerStatuses": [{"ready": True, "restartCount": 0, "imageID": self.image}]}}
        self.crons = {}; self.jobs = {}; self.pods = {}; self.cms = {}; self.expected = {}; self.logs = {}; self.calls = []
        self.plan = {"schema_version": 1, "created_at": self.at.isoformat(), "targets": M.targets(self.at),
                     "program_sha256": M.S.file_hash(Path(M.__file__).read_bytes()), "support_sha256": M.SUPPORT_HASH,
                     "task_id": "20261021-123456789abc", "relay_uid": "relay", "relay_spec_sha256": M.digest(self.dep["spec"]),
                     "relay_image_id": self.image, "relay_app_sha256": self.app, "crons": {}, "reports": {},
                     "sequence_timer_sha256": M.digest("timer-unit"), "sequence_service_sha256": M.digest("service-unit")}
        for kind in M.KINDS:
            ns, name = M.CRONS[kind]; target = M.stamp(self.plan["targets"][kind])
            podspec = {"containers": [{"name": "work", "image": "fixed-image"}]}
            cron = {"metadata": {"uid": kind + "-cron"}, "spec": {"schedule": "0 3 * * *" if kind == "portal_backup" else "30 3 1 * *",
                    "timeZone": "Asia/Seoul", "suspend": kind == "portal_backup", "jobTemplate": {"spec": {"template": {"spec": podspec}}}}}
            self.crons[kind] = cron
            self.plan["crons"][kind] = {"uid": cron["metadata"]["uid"], "spec_sha256": M.digest(cron["spec"])}
            data = {"run_id": (target + timedelta(seconds=5)).strftime("%Y%m%dT%H%M%SZ") + "-42",
                    "completed_at": (target + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "completed" if kind == "portal_backup" else "passed"}
            if kind == "portal_backup":
                data["stage"] = "completed"
            else:
                data.update(health_audit="passed", backup_check="passed", recovery_lab="passed", slo_evidence="failed",
                            slo_evidence_reason="none", slo_criteria="freshness_1800", slo_days_recorded="30", slo_days_ok="0", slo_days_unobservable="0")
            cm = {"metadata": {"uid": kind + "-report"}, "data": data}; self.cms[kind] = cm
            self.plan["reports"][kind] = cm["metadata"]["uid"]
            expected = {"run_id": data["run_id"], "completed_at": data["completed_at"], "report_sha256": M.digest(data),
                        "message_sha256": "d" * 64, "wording": True, "status": data["status"]}
            self.expected[kind] = expected
            receipt = {"schema_version": 1, "kind": kind, **{k: expected[k] for k in ["run_id", "report_sha256", "message_sha256"]},
                       "sent_at": (target + timedelta(seconds=62)).isoformat()}
            self.logs[kind] = M.PREFIX + json.dumps(receipt)
            annotations = {"batch.kubernetes.io/cronjob-scheduled-timestamp": target.isoformat()}
            labels = {}
            job_name = "monthly-sre-audit-123"
            if kind == "portal_backup":
                day = target.astimezone(M.KST).date().isoformat(); job_name = "portal-pvc-backup-" + day.replace("-", "")
                annotations = {"personal-server/backup-template-sha256": M.digest(cron["spec"]["jobTemplate"]["spec"])}
                labels = {"personal-server/backup-stage": "portal", "personal-server/backup-date": day}
            job = {"metadata": {"name": job_name, "uid": kind + "-job", "ownerReferences": self.owner("CronJob", cron["metadata"]["uid"]),
                                "annotations": annotations, "labels": labels}, "spec": copy.deepcopy(cron["spec"]["jobTemplate"]["spec"]),
                   "status": {"startTime": (target + timedelta(seconds=2)).isoformat(), "completionTime": (target + timedelta(seconds=61)).isoformat(),
                              "succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]}}
            self.jobs[kind] = job
            self.pods[kind] = {"metadata": {"uid": kind + "-pod", "ownerReferences": self.owner("Job", job["metadata"]["uid"])},
                               "status": {"phase": "Succeeded", "containerStatuses": [{"restartCount": 0,
                               "state": {"terminated": {"exitCode": 0, "startedAt": (target + timedelta(seconds=3)).isoformat()}}}]}}
        base = {k: v for k, v in self.plan.items() if k != "task_id"}
        self.plan["task_id"] = self.at.astimezone(M.KST).strftime("%Y%m%d") + "-" + M.digest(base)[:12]

    @staticmethod
    def owner(kind, uid):
        return [{"kind": kind, "uid": uid, "controller": True}]

    def read(self, kind, ns, name=None):
        if kind == "deployment":
            return copy.deepcopy(self.dep)
        if kind == "replicasets":
            return {"items": [{"metadata": {"uid": "rs", "ownerReferences": self.owner("Deployment", "relay")}}]}
        if kind == "cronjob":
            return copy.deepcopy(self.crons[next(k for k, pair in M.CRONS.items() if pair == (ns, name))])
        if kind in {"pods", "jobs"}:
            chosen = [k for k in M.KINDS if M.CRONS[k][0] == ns]
            items = [self.pods[k] if kind == "pods" else self.jobs[k] for k in chosen]
            if ns == "monitoring" and kind == "pods":
                items = [self.relaypod, *items]
            return {"items": copy.deepcopy(items)}
        if name == "sre-telegram-relay-state":
            return {"data": {M.LEDGERS[k]: json.dumps([self.expected[k]["run_id"]]) for k in M.KINDS}}
        if name == "pvc-backup-sequence-state":
            return {"data": {"run_date": "2026-10-22", "results": '{"portal":"passed"}'}}
        return copy.deepcopy(self.cms[next(k for k, value in M.REPORTS.items() if value == name)])

    def execute(self, argv, value=None):
        self.calls.append((argv, value))
        if "logs" in argv:
            target = next(v for v in argv if v.startswith("--since-time="))[13:]
            return self.logs[next(k for k, t in self.plan["targets"].items() if t == target)]
        if M.EXPECTED_CODE in argv:
            return json.dumps(self.expected[json.loads(value)["kind"]])
        if "exec" in argv:
            return self.app
        if argv[0] == "date":
            return argv[2]
        if argv[0] == "loginctl":
            return "yes"
        if "is-system-running" in argv:
            return "running"
        if "is-active" in argv:
            return "active"
        if "is-enabled" in argv:
            return "enabled"
        if "LoadState" in argv:
            return "not-found"
        if "ActiveState" in argv:
            return "inactive"
        if "cat" in argv:
            return "timer-unit" if argv[-1].endswith(".timer") else "service-unit"
        if "LastTriggerUSec" in argv:
            return self.plan["targets"]["portal_backup"]
        if "ExecMainStartTimestamp" in argv:
            return (M.stamp(self.plan["targets"]["portal_backup"]) + timedelta(seconds=1)).isoformat()
        return ""

    def observation(self, kind):
        return M.inspect(kind, self.plan, read=self.read, execute=self.execute)


class NotificationFollowupEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()

    def test_actual_formatter_code_normalizes_report_and_checks_both_wordings(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "sre-telegram-relay")}
        for kind in M.KINDS:
            cm = copy.deepcopy(self.f.cms[kind])
            if kind == "portal_backup":
                cm["data"]["retention_summary"] = "ignored-extra-evidence"
            p = subprocess.run([sys.executable, "-B", "-c", M.EXPECTED_CODE], input=json.dumps({"kind": kind, "configmap": cm}),
                               text=True, capture_output=True, env=env, check=True)
            value = json.loads(p.stdout)
            self.assertTrue(value["wording"])
            self.assertEqual(value["report_sha256"], self.f.expected[kind]["report_sha256"])
            self.assertNotIn("ignored-extra-evidence", p.stdout)

    def test_next_targets_follow_kst_day_month_year_and_exact_boundary(self):
        self.assertEqual(self.f.plan["targets"]["portal_backup"], "2026-10-21T17:30:00+00:00")
        self.assertEqual(self.f.plan["targets"]["quarterly_audit"], "2026-10-31T18:30:00+00:00")
        target = M.targets(datetime(2026, 12, 31, 23, tzinfo=UTC))
        self.assertEqual(target["quarterly_audit"], "2027-01-31T18:30:00+00:00")

    def test_prepare_is_read_only_and_fixed_existing_schedules(self):
        plan = M.prepare(read=self.f.read, execute=self.f.execute, at=self.f.at)
        self.assertEqual(plan["targets"], self.f.plan["targets"])
        self.assertFalse(any(any(x in argv for x in ["patch", "apply", "delete", "create", "enable"]) for argv, _ in self.f.calls))
        self.f.crons["quarterly_audit"]["spec"]["suspend"] = True
        with self.assertRaisesRegex(M.S.Failed, "schedule_drift"):
            M.prepare(read=self.f.read, execute=self.f.execute, at=self.f.at)

    def test_real_receipt_both_natural_jobs_and_ledger_required(self):
        for kind in M.KINDS:
            value = M.inspect(kind, self.f.plan, read=self.f.read, execute=self.f.execute)
            self.assertEqual(value["status"], "passed")
            self.assertEqual(value["human_receipt"], "unverified")
            self.assertEqual(value["source"]["job_uid"], kind + "-job")

    def test_no_receipt_never_passes_even_with_formatter_and_delivered_ledger(self):
        self.f.logs["portal_backup"] = "polling healthy"
        with self.assertRaises(M.Pending):
            M.inspect("portal_backup", self.f.plan, read=self.f.read, execute=self.f.execute)

    def test_tampered_duplicate_or_late_receipts_are_not_success(self):
        kind = "quarterly_audit"; original = self.f.logs[kind]
        for mutate in [lambda r: r.update(message_sha256="e" * 64), lambda r: r.update(report_sha256="e" * 64),
                       lambda r: r.update(sent_at="2026-11-01T03:00:00+00:00"), lambda r: r.update(extra="value")]:
            value = json.loads(original[len(M.PREFIX):]); mutate(value); self.f.logs[kind] = M.PREFIX + json.dumps(value)
            with self.assertRaises(M.S.Failed):
                M.inspect(kind, self.f.plan, read=self.f.read, execute=self.f.execute)
        self.f.logs[kind] = original + "\n" + original
        with self.assertRaisesRegex(M.S.Failed, "duplicate_receipt"):
            M.inspect(kind, self.f.plan, read=self.f.read, execute=self.f.execute)

    def test_manual_audit_job_wrong_owner_or_template_is_rejected(self):
        for mutation in [lambda j: j["metadata"]["annotations"].clear(),
                         lambda j: j["metadata"]["ownerReferences"][0].update(uid="foreign"),
                         lambda j: j["spec"]["template"]["spec"]["containers"][0].update(image="foreign")]:
            f = Fixture(); mutation(f.jobs["quarterly_audit"])
            with self.assertRaises((M.Pending, M.S.Failed)):
                M.inspect("quarterly_audit", f.plan, read=f.read, execute=f.execute)

    def test_backup_timer_or_sequence_result_drift_does_not_pass(self):
        def wrong(argv, value=None):
            if "LastTriggerUSec" in argv:
                return "2026-10-21T18:00:00+00:00"
            return self.f.execute(argv, value)
        with self.assertRaisesRegex(M.S.Failed, "sequence_not_scheduled"):
            M.inspect("portal_backup", self.f.plan, read=self.f.read, execute=wrong)

    def test_relay_restart_wrong_source_image_and_multiple_pods_do_not_pass(self):
        for field, value in [("restartCount", 1), ("ready", False), ("imageID", "foreign")]:
            f = Fixture(); f.relaypod["status"]["containerStatuses"][0][field] = value
            with self.assertRaises(M.S.Failed):
                M.inspect("portal_backup", f.plan, read=f.read, execute=f.execute)
        self.f.app = "f" * 64
        with self.assertRaisesRegex(M.S.Failed, "relay_source_drift"):
            M.inspect("portal_backup", self.f.plan, read=self.f.read, execute=self.f.execute)

    def test_previous_report_and_missing_or_mismatched_runner_are_not_pass(self):
        f = self.f; f.cms["portal_backup"]["data"]["completed_at"] = f.at.isoformat()
        with self.assertRaises(M.Pending):
            M.inspect("portal_backup", f.plan, read=f.read, execute=f.execute)
        f = Fixture(); f.expected["portal_backup"]["run_id"] = "20261021T180000Z-42"
        with self.assertRaisesRegex(M.S.Failed, "report_runner_mismatch"):
            M.inspect("portal_backup", f.plan, read=f.read, execute=f.execute)


class NotificationFollowupLifecycleTests(unittest.TestCase):
    def install(self, home, f=None, execute=None):
        f = f or Fixture()
        with patch.object(M, "os", wraps=os) as runtime_os:
            runtime_os.getuid.return_value = os.getuid() or 10001
            return M.install(f.plan, M.digest(f.plan), home=home, execute=execute or f.execute, read=f.read, clock_now=lambda: f.at)

    def test_install_absolute_two_windows_private_files_and_no_legacy_unit_changes(self):
        f = Fixture()
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as home:
            self.install(home, f)
            paths = M.layout(f.plan["task_id"], home)
            timer = (paths["units"] / (paths["unit"] + ".timer")).read_text()
            self.assertEqual(timer.count("OnCalendar="), 2)
            self.assertIn("Persistent=true", timer)
            self.assertIn("2026-10-21 17:35:00 UTC", timer)
            self.assertEqual((paths["task"] / "plan.json").stat().st_mode & 0o777, 0o600)
            self.assertTrue(any("Linger" in argv for argv, _ in f.calls))
            self.assertFalse(any("linger" in x.lower() and x != "Linger" for argv, _ in f.calls for x in argv))
            with self.assertRaises(M.S.Failed):
                self.install(home, f)

    def test_uncertain_activation_preserves_files_and_never_stops_service(self):
        f = Fixture()
        def uncertain(argv, value=None):
            if "enable" in argv and "--now" in argv:
                raise M.S.Failed("activation_response_lost")
            if "ActiveState" in argv:
                return "active"
            return f.execute(argv, value)
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as home:
            with self.assertRaisesRegex(M.S.Failed, "installation_state_uncertain"):
                self.install(home, f, uncertain)
            paths = M.layout(f.plan["task_id"], home)
            self.assertTrue((paths["task"] / "plan.json").exists())
            self.assertTrue((paths["units"] / (paths["unit"] + ".service")).exists())
            self.assertFalse(any("stop" in argv for argv, _ in f.calls))

    def test_two_natural_windows_resume_without_resending_and_cleanup_owned_resources(self):
        f = Fixture(); delivered = []
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as home:
            self.install(home, f)
            observe = lambda kind, plan: f.observation(kind)
            deliver = lambda plan, text, **kw: delivered.append(text) or True
            first = M.stamp(f.plan["targets"]["portal_backup"]) + timedelta(minutes=5)
            result = M.run_task(f.plan["task_id"], home=home, clock_now=lambda: first, observe=observe, deliver=deliver, execute=f.execute)
            self.assertEqual(list(result["observations"]), ["portal_backup"])
            self.assertEqual(len(delivered), 1)
            M.run_task(f.plan["task_id"], home=home, clock_now=lambda: first, observe=observe, deliver=deliver, execute=f.execute)
            self.assertEqual(len(delivered), 1)
            last = M.stamp(f.plan["targets"]["quarterly_audit"]) + timedelta(minutes=5)
            result = M.run_task(f.plan["task_id"], home=home, clock_now=lambda: last, observe=observe, deliver=deliver, execute=f.execute)
            paths = M.layout(f.plan["task_id"], home)
            self.assertEqual(result["cleanup"], "passed")
            self.assertEqual(len(delivered), 2)
            self.assertIn("임시 예약·파일 정리: 완료", delivered[-1])
            self.assertFalse(paths["task"].exists())
            self.assertFalse(paths["result"].with_suffix(".lock").exists())
            self.assertEqual(list(paths["units"].iterdir()), [])
            self.assertTrue(paths["result"].exists())
            self.assertTrue(any(paths["programs"].iterdir()))

    def test_boot_before_window_is_quiet_and_missed_all_windows_reports_unconfirmed(self):
        f = Fixture(); delivered = []
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as home:
            self.install(home, f)
            observe = lambda *a: self.fail("must not observe before/after the bounded windows")
            deliver = lambda plan, text, **kw: delivered.append(text) or True
            result = M.run_task(f.plan["task_id"], home=home, clock_now=lambda: f.at, observe=observe, deliver=deliver, execute=f.execute)
            self.assertEqual(result["observations"], {}); self.assertEqual(delivered, [])
            late = M.stamp(f.plan["targets"]["quarterly_audit"]) + timedelta(hours=2)
            result = M.run_task(f.plan["task_id"], home=home, clock_now=lambda: late, observe=observe, deliver=deliver, execute=f.execute)
            self.assertTrue(all(v["status"] == "unconfirmed" for v in result["observations"].values()))
            self.assertEqual(result["cleanup"], "passed")
            self.assertEqual(len(delivered), 2)

    def test_unknown_send_attempt_is_persisted_and_never_retried(self):
        f = Fixture(); sends = []
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as home:
            self.install(home, f)
            at = M.stamp(f.plan["targets"]["portal_backup"]) + timedelta(minutes=5)
            def send(plan, text, **kw):
                sends.append(text); raise TimeoutError()
            kwargs = dict(home=home, clock_now=lambda: at, observe=lambda kind, plan: f.observation(kind), deliver=send, execute=f.execute)
            first = M.run_task(f.plan["task_id"], **kwargs)
            self.assertEqual(first["notifications"]["portal_backup"], "unconfirmed")
            M.run_task(f.plan["task_id"], **kwargs)
            self.assertEqual(len(sends), 1)

    def test_cleanup_drift_preserves_user_file_and_partial_cleanup_can_resume(self):
        f = Fixture()
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as home:
            self.install(home, f); paths = M.layout(f.plan["task_id"], home)
            plan = json.loads((paths["task"] / "plan.json").read_text())
            user = paths["task"] / "user.txt"; user.write_text("preserve")
            with self.assertRaisesRegex(M.S.Failed, "cleanup_drift"):
                M.cleanup(paths, plan, execute=f.execute)
            self.assertEqual(user.read_text(), "preserve"); user.unlink()
            def fail_reload(argv, value=None):
                if "daemon-reload" in argv:
                    raise M.S.Failed("reload_response_lost")
                return f.execute(argv, value)
            with self.assertRaises(M.S.Failed):
                M.cleanup(paths, plan, execute=fail_reload)
            self.assertFalse(paths["task"].exists())
            M.cleanup(paths, plan, execute=f.execute)

    def test_result_symlink_never_overwrites_user_file(self):
        f = Fixture()
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as home:
            self.install(home, f); paths = M.layout(f.plan["task_id"], home)
            paths["result"].parent.mkdir(parents=True); user = Path(home) / "user.json"; user.write_text("preserve")
            paths["result"].symlink_to(user)
            with self.assertRaises(M.S.Failed):
                M.run_task(f.plan["task_id"], home=home)
            self.assertEqual(user.read_text(), "preserve")

    def test_saved_result_plan_or_unit_fingerprint_drift_blocks_cleanup(self):
        f = Fixture()
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as home:
            self.install(home, f)
            M.run_task(f.plan["task_id"], home=home, clock_now=lambda: f.at, execute=f.execute)
            paths = M.layout(f.plan["task_id"], home)
            result = json.loads(paths["result"].read_text())
            for mutate in [lambda r: r["plan"].update(task_id="20261021-000000000000"),
                           lambda r: r["plan"]["units"].update(service="0" * 64),
                           lambda r: r.update(cleanup="arbitrary"),
                           lambda r: r["plan"]["targets"].update(quarterly_audit="2026-11-01T01:00:00+00:00"),
                           lambda r: r["observations"].update(portal_backup={"status": "passed"})]:
                broken = copy.deepcopy(result); mutate(broken); M.S.atomic_result(paths["result"], broken)
                with self.assertRaises(M.S.Failed):
                    M.run_task(f.plan["task_id"], home=home, clock_now=lambda: f.at, execute=f.execute)
                self.assertTrue(paths["task"].exists())
                self.assertTrue((paths["units"] / (paths["unit"] + ".timer")).exists())

    def test_pending_reads_are_bounded_and_do_not_become_success(self):
        f = Fixture(); current = [M.stamp(f.plan["targets"]["portal_backup"]) + timedelta(minutes=59, seconds=30)]
        polls = []; delivered = []
        def observe(*a):
            polls.append(True); raise M.Pending()
        def sleep(seconds):
            self.assertLessEqual(seconds, 60); current[0] += timedelta(seconds=seconds)
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as home:
            self.install(home, f)
            result = M.run_task(f.plan["task_id"], home=home, clock_now=lambda: current[0], sleep=sleep, observe=observe,
                                deliver=lambda plan, text, **kw: delivered.append(text) or True, execute=f.execute)
            self.assertEqual(len(polls), 2)
            self.assertEqual(result["observations"]["portal_backup"]["status"], "unconfirmed")
            self.assertIn("확인 필요", delivered[0])


if __name__ == "__main__":
    unittest.main()
