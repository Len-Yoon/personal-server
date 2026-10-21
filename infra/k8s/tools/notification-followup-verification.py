#!/usr/bin/env python3
"""Observe two natural notification windows on N100; never rerun production jobs."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SUPPORT_HASH = "c366516118015e59a279059b39bb282f23a1e26a84163ae9541a9d4d6b9ab824"
SUPPORT = Path(__file__).with_name("slo-followup-verification.py")
if hashlib.sha256(SUPPORT.read_bytes()).hexdigest() != SUPPORT_HASH:
    raise RuntimeError("support_fingerprint_mismatch")
_spec = importlib.util.spec_from_file_location("notification_followup_support", SUPPORT)
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)
require, digest, stamp = S.require, S.digest, S.timestamp
KST = ZoneInfo("Asia/Seoul")
KINDS = ("portal_backup", "quarterly_audit")
REPORTS = {"portal_backup": "sre-telegram-backup-status", "quarterly_audit": "sre-telegram-quarterly-audit-status"}
LEDGERS = {"portal_backup": "backup_delivered_run_ids", "quarterly_audit": "quarterly_audit_delivered_run_ids"}
CRONS = {"portal_backup": ("personal-server", "portal-pvc-backup"), "quarterly_audit": ("monitoring", "monthly-sre-audit")}
SEQUENCE = "personal-server-pvc-backup-sequence"
PREFIX = "notification_delivery_receipt "
HEX = re.compile(r"[a-f0-9]{64}")
LOG_LIMIT = 2_000_000
EXPECTED_CODE = '''import hashlib,json,sys
from app.main import _read_backup_report,_read_quarterly_audit_report,_format_backup_report_message,_format_quarterly_audit_message
p=json.load(sys.stdin)
class Client:
 def get_config_map(self,*args):return p['configmap']
kind=p['kind'];report=(_read_backup_report if kind=='portal_backup' else _read_quarterly_audit_report)(Client())
assert report is not None
text=(_format_backup_report_message if kind=='portal_backup' else _format_quarterly_audit_message)(report)
wording=('계획 정지 후 가용 상태 복귀 확인' in text and '완료 시각 기준' in text) if kind=='portal_backup' else ('과거 관측' in text and '현재 상태 점검과 별도' in text)
print(json.dumps(dict(report_sha256=hashlib.sha256(json.dumps(report,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest(),message_sha256=hashlib.sha256(text.encode()).hexdigest(),run_id=report['run_id'],completed_at=report['completed_at'],wording=wording,status=report['status'])))
'''


class Pending(Exception):
    pass


def command(args, value=None):
    if args[:len(S.KUBE) + 1] == [*S.KUBE, "logs"]:
        # Preserve raw whitespace for the byte-limit sentinel; S.command strips it.
        try:
            response = subprocess.run(args, input=value, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            raise S.Failed("receipt_logs_unavailable") from None
        require(response.returncode == 0 and len(response.stdout.encode("utf-8")) < LOG_LIMIT,
                "receipt_logs_incomplete")
        return response.stdout
    return S.command(args, value)


def get(kind, ns, name=None):
    return json.loads(command(["sudo", "-n", "k3s", "kubectl", "--request-timeout=10s", "-n", ns,
                               "get", kind, *([name] if name else []), "-o", "json"]))


def now():
    return datetime.now(timezone.utc)


def targets(at):
    local = at.astimezone(KST)
    backup = local.replace(hour=2, minute=30, second=0, microsecond=0)
    if backup <= local:
        backup += timedelta(days=1)
    audit = local.replace(day=1, hour=3, minute=30, second=0, microsecond=0)
    if audit <= local:
        audit = (audit.replace(day=28) + timedelta(days=4)).replace(day=1)
    return {"portal_backup": backup.astimezone(timezone.utc).isoformat(),
            "quarterly_audit": audit.astimezone(timezone.utc).isoformat()}


def owner(obj, kind, uid):
    refs = [r for r in obj.get("metadata", {}).get("ownerReferences", []) if r.get("controller") is True]
    return len(refs) == 1 and refs[0].get("kind") == kind and refs[0].get("uid") == uid


def contains(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and contains(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(contains(a, b) for a, b in zip(actual, expected))
    return type(actual) is type(expected) and actual == expected


def relay(plan, *, read=get, execute=command):
    dep = read("deployment", "monitoring", "sre-telegram-relay")
    require(dep["metadata"]["uid"] == plan["relay_uid"] and digest(dep["spec"]) == plan["relay_spec_sha256"], "relay_drift")
    sets = read("replicasets", "monitoring")["items"]
    uids = {r["metadata"]["uid"] for r in sets if owner(r, "Deployment", plan["relay_uid"])}
    pods = [p for p in read("pods", "monitoring")["items"]
            if any(owner(p, "ReplicaSet", uid) for uid in uids)
            and p.get("status", {}).get("phase") not in {"Succeeded", "Failed"}]
    require(len(pods) == 1 and not pods[0]["metadata"].get("deletionTimestamp"), "relay_not_single")
    pod = pods[0]
    states = pod.get("status", {}).get("containerStatuses", [])
    require(len(states) == 1 and states[0].get("ready") is True and states[0].get("restartCount") == 0
            and states[0].get("imageID") == plan["relay_image_id"], "relay_runtime_drift")
    app = execute([*S.KUBE, "exec", pod["metadata"]["name"], "--", "python", "-B", "-c",
                   "import pathlib,hashlib;print(hashlib.sha256(pathlib.Path('/app/app/main.py').read_bytes()).hexdigest())"])
    require(app == plan["relay_app_sha256"], "relay_source_drift")
    return pod


def prepare(*, read=get, execute=command, at=None):
    at = at or now()
    dep = read("deployment", "monitoring", "sre-telegram-relay")
    containers = dep["spec"]["template"]["spec"]["containers"]
    require(len(containers) == 1 and dep["spec"].get("replicas") == 1
            and dep["spec"].get("strategy", {}).get("type") == "Recreate", "unsafe_relay")
    image = containers[0]["image"]
    require(re.fullmatch(r"docker.io/library/personal-server-sre-telegram-relay@sha256:[a-f0-9]{64}", image), "unpinned_relay")
    app = execute([*S.KUBE, "exec", "deployment/sre-telegram-relay", "--", "python", "-B", "-c",
                   "import pathlib,hashlib;print(hashlib.sha256(pathlib.Path('/app/app/main.py').read_bytes()).hexdigest())"])
    require(HEX.fullmatch(app) is not None, "invalid_source_hash")
    plan = {"schema_version": 1, "created_at": at.isoformat(), "targets": targets(at),
            "program_sha256": S.file_hash(Path(__file__).read_bytes()), "support_sha256": SUPPORT_HASH,
            "relay_uid": dep["metadata"]["uid"], "relay_spec_sha256": digest(dep["spec"]),
            "relay_image_id": image, "relay_app_sha256": app, "crons": {}, "reports": {}}
    for kind, (ns, name) in CRONS.items():
        cron = read("cronjob", ns, name)
        wanted = "0 3 * * *" if kind == "portal_backup" else "30 3 1 * *"
        require(cron["spec"].get("schedule") == wanted and cron["spec"].get("timeZone") == "Asia/Seoul"
                and cron["spec"].get("suspend", False) == (kind == "portal_backup"), "schedule_drift")
        plan["crons"][kind] = {"uid": cron["metadata"]["uid"], "spec_sha256": digest(cron["spec"])}
        plan["reports"][kind] = read("configmap", "monitoring", REPORTS[kind])["metadata"]["uid"]
    require(execute(["systemctl", "--user", "is-active", SEQUENCE + ".timer"]) == "active", "sequence_not_active")
    require(execute(["systemctl", "--user", "is-enabled", SEQUENCE + ".timer"]) == "enabled", "sequence_not_enabled")
    plan["sequence_timer_sha256"] = digest(execute(["systemctl", "--user", "cat", SEQUENCE + ".timer"]))
    plan["sequence_service_sha256"] = digest(execute(["systemctl", "--user", "cat", SEQUENCE + ".service"]))
    relay(plan, read=read, execute=execute)
    return plan


def matched_receipt(logs, kind, expected, target, deadline):
    matches = []
    keys = {"schema_version", "kind", "run_id", "report_sha256", "message_sha256", "sent_at"}
    for line in logs.splitlines():
        if not line.startswith(PREFIX):
            continue
        try:
            value = json.loads(line[len(PREFIX):])
        except ValueError:
            continue
        if not isinstance(value, dict) or value.get("kind") != kind or value.get("run_id") != expected["run_id"]:
            continue
        require(set(value) == keys and type(value["schema_version"]) is int and value["schema_version"] == 1, "invalid_receipt")
        require(all(value.get(k) == expected[k] for k in ("report_sha256", "message_sha256")), "receipt_hash_mismatch")
        sent = stamp(value["sent_at"])
        require(target <= stamp(expected["completed_at"]) <= sent <= deadline, "receipt_time_mismatch")
        matches.append(value)
    if not matches:
        raise Pending()
    require(len(matches) == 1, "duplicate_receipt")
    return matches[0]


def natural_job(kind, plan, report, *, read=get, execute=command):
    target = stamp(plan["targets"][kind])
    ns, name = CRONS[kind]
    cron = read("cronjob", ns, name)
    require(cron["metadata"]["uid"] == plan["crons"][kind]["uid"]
            and digest(cron["spec"]) == plan["crons"][kind]["spec_sha256"], "cron_drift")
    jobs = read("jobs", ns)["items"]
    matching = []
    for job in jobs:
        if not owner(job, "CronJob", cron["metadata"]["uid"]):
            continue
        meta, status = job["metadata"], job.get("status", {})
        if kind == "quarterly_audit":
            scheduled = meta.get("annotations", {}).get("batch.kubernetes.io/cronjob-scheduled-timestamp")
            if not scheduled or stamp(scheduled) != target:
                continue
        else:
            day = target.astimezone(KST).date().isoformat()
            if (meta.get("name") != "portal-pvc-backup-" + day.replace("-", "")
                    or meta.get("labels", {}).get("personal-server/backup-stage") != "portal"
                    or meta.get("labels", {}).get("personal-server/backup-date") != day):
                continue
            require(meta.get("annotations", {}).get("personal-server/backup-template-sha256") == digest(cron["spec"]["jobTemplate"]["spec"]), "backup_template_drift")
        if not status.get("completionTime"):
            continue
        require(target <= stamp(status["startTime"]) <= stamp(report["completed_at"])
                <= stamp(status["completionTime"]) + timedelta(seconds=90), "report_job_time_mismatch")
        require(status.get("succeeded") == 1 and not status.get("failed", 0)
                and any(c.get("type") == "Complete" and c.get("status") == "True" for c in status.get("conditions", [])), "scheduled_job_failed")
        matching.append(job)
    if not matching:
        raise Pending()
    require(len(matching) == 1, "ambiguous_scheduled_job")
    job = matching[0]
    require(not job["metadata"].get("deletionTimestamp") and contains(job["spec"], cron["spec"]["jobTemplate"]["spec"]), "job_template_drift")
    pods = [p for p in read("pods", ns)["items"] if owner(p, "Job", job["metadata"]["uid"])]
    require(len(pods) == 1 and pods[0].get("status", {}).get("phase") == "Succeeded", "job_pod_unconfirmed")
    states = pods[0]["status"].get("containerStatuses", [])
    require(len(states) == 1 and states[0].get("restartCount") == 0
            and states[0].get("state", {}).get("terminated", {}).get("exitCode") == 0, "job_container_failed")
    started = stamp(states[0]["state"]["terminated"]["startedAt"])
    run_start = datetime.strptime(report["run_id"].split("-")[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    require(timedelta(0) <= run_start - started <= timedelta(seconds=30), "report_runner_mismatch")
    if kind == "portal_backup":
        for suffix in ("timer", "service"):
            require(digest(execute(["systemctl", "--user", "cat", SEQUENCE + "." + suffix])) == plan["sequence_" + suffix + "_sha256"], "sequence_drift")
        triggered = execute(["systemctl", "--user", "show", SEQUENCE + ".timer", "-p", "LastTriggerUSec", "--value"])
        require(bool(triggered), "sequence_trigger_missing")
        # Ask systemd's date parser to normalize its locale-dependent timestamp.
        trigger = stamp(execute(["date", "--date", triggered, "--iso-8601=seconds"]))
        require(abs((trigger - target).total_seconds()) <= 90, "sequence_not_scheduled")
        service_start = execute(["systemctl", "--user", "show", SEQUENCE + ".service", "-p", "ExecMainStartTimestamp", "--value"])
        started_service = stamp(execute(["date", "--date", service_start, "--iso-8601=seconds"]))
        require(trigger <= started_service <= stamp(job["status"]["startTime"])
                and started_service - trigger <= timedelta(seconds=120), "sequence_invocation_mismatch")
        state = read("configmap", "personal-server", "pvc-backup-sequence-state")["data"]
        require(state.get("run_date") == target.astimezone(KST).date().isoformat()
                and json.loads(state["results"]).get("portal") == "passed", "sequence_result_missing")
    return {"job_uid": job["metadata"]["uid"], "pod_uid": pods[0]["metadata"]["uid"]}


def inspect(kind, plan, *, read=get, execute=command):
    target = stamp(plan["targets"][kind]); deadline = target + timedelta(hours=1)
    pod = relay(plan, read=read, execute=execute)
    cm = read("configmap", "monitoring", REPORTS[kind])
    require(cm["metadata"]["uid"] == plan["reports"][kind], "report_recreated")
    completed = stamp(cm["data"]["completed_at"])
    if completed < target:
        raise Pending()
    require(completed <= deadline, "late_report")
    expected = json.loads(execute([*S.KUBE, "exec", "-i", pod["metadata"]["name"], "--", "python", "-B", "-c", EXPECTED_CODE],
                                  json.dumps({"kind": kind, "configmap": cm}, ensure_ascii=False)))
    require(expected["wording"] is True, "new_wording_missing")
    require(expected["status"] in ({"completed", "unchanged"} if kind == "portal_backup" else {"passed", "failed"}), "producer_failed")
    source = natural_job(kind, plan, expected, read=read, execute=execute)
    logs = execute([*S.KUBE, "logs", pod["metadata"]["name"], "--since-time=" + target.isoformat(),
                    "--limit-bytes=" + str(LOG_LIMIT + 1)])
    require(len(logs.encode("utf-8")) < LOG_LIMIT, "receipt_logs_incomplete")
    receipt = matched_receipt(logs, kind, expected, target, deadline)
    ledger = json.loads(read("configmap", "monitoring", "sre-telegram-relay-state")["data"][LEDGERS[kind]])
    require(isinstance(ledger, list) and expected["run_id"] in ledger, "delivery_ledger_missing")
    return {"status": "passed", "receipt": receipt, "source": source, "relay_pod_uid": pod["metadata"]["uid"],
            "relay_image_id": plan["relay_image_id"], "producer_status": expected["status"],
            "completed_at": expected["completed_at"], "human_receipt": "unverified"}


def layout(task_id, home=None):
    require(re.fullmatch(r"\d{8}-[a-f0-9]{12}", task_id or "") is not None, "invalid_task_id")
    home = Path.home() if home is None else Path(home)
    return {"task": home / ".local/state/personal-server" / ("notification-followup-" + task_id),
            "result": home / ".local/state/personal-server/notification-followup-results" / (task_id + ".json"),
            "units": home / ".config/systemd/user", "programs": home / ".local/lib/personal-server/notification-followup",
            "unit": "personal-server-notification-followup-" + task_id}


def validate_plan(plan, paths, *, installed=False):
    keys = {"schema_version", "created_at", "targets", "program_sha256", "support_sha256", "relay_uid", "relay_spec_sha256",
            "relay_image_id", "relay_app_sha256", "crons", "reports", "sequence_timer_sha256", "sequence_service_sha256", "task_id"}
    require(isinstance(plan, dict) and set(plan) == keys | ({"units"} if installed else set()), "invalid_plan_keys")
    require(type(plan["schema_version"]) is int and plan["schema_version"] == 1, "invalid_plan_version")
    require(plan["targets"] == targets(stamp(plan["created_at"])), "invalid_plan_targets")
    require(plan["program_sha256"] == S.file_hash(Path(__file__).read_bytes()) and plan["support_sha256"] == SUPPORT_HASH, "plan_program_drift")
    base = {k: v for k, v in plan.items() if k not in {"task_id", "units"}}
    ident = stamp(plan["created_at"]).astimezone(KST).strftime("%Y%m%d") + "-" + digest(base)[:12]
    require(plan["task_id"] == ident and paths["unit"].endswith(ident), "plan_identity_drift")
    require(set(plan["crons"]) == set(KINDS) and set(plan["reports"]) == set(KINDS), "invalid_plan_resources")
    for key in ("relay_spec_sha256", "relay_app_sha256", "sequence_timer_sha256", "sequence_service_sha256"):
        require(isinstance(plan[key], str) and HEX.fullmatch(plan[key]) is not None, "invalid_plan_digest")
    for value in plan["crons"].values():
        require(set(value) == {"uid", "spec_sha256"} and HEX.fullmatch(value["spec_sha256"]) is not None, "invalid_cron_fingerprint")
    if installed:
        rendered, _ = units(plan, paths, Path(__file__).read_bytes())
        require(plan["units"] == {k: S.file_hash(v) for k, v in rendered.items()}, "unit_fingerprint_drift")


def validate_observation(kind, value, plan):
    require(isinstance(value, dict), "invalid_observation")
    if value.get("status") == "unconfirmed":
        require(value == {"status": "unconfirmed", "reason": "evidence_deadline_expired"}, "invalid_unconfirmed_observation")
        return
    keys = {"status", "receipt", "source", "relay_pod_uid", "relay_image_id", "producer_status", "completed_at", "human_receipt"}
    require(set(value) == keys and value["status"] == "passed" and value["human_receipt"] == "unverified", "invalid_pass_observation")
    require(value["relay_image_id"] == plan["relay_image_id"] and isinstance(value["relay_pod_uid"], str)
            and 0 < len(value["relay_pod_uid"]) <= 100, "invalid_observed_relay")
    require(isinstance(value["source"], dict) and set(value["source"]) == {"job_uid", "pod_uid"}
            and all(isinstance(v, str) and 0 < len(v) <= 100 for v in value["source"].values()), "invalid_observed_source")
    require(value["producer_status"] in ({"completed", "unchanged"} if kind == "portal_backup" else {"passed", "failed"}), "invalid_observed_producer")
    receipt = value["receipt"]
    require(isinstance(receipt, dict) and receipt.get("kind") == kind, "invalid_observed_receipt")
    require(all(isinstance(receipt.get(k), str) and HEX.fullmatch(receipt[k]) is not None for k in ("report_sha256", "message_sha256")), "invalid_observed_hash")
    require(isinstance(receipt.get("run_id"), str) and re.fullmatch(r"\d{8}T\d{6}Z-[1-9]\d*", receipt["run_id"]) is not None, "invalid_observed_run")
    target = stamp(plan["targets"][kind])
    expected = {**{k: receipt[k] for k in ("run_id", "report_sha256", "message_sha256")}, "completed_at": value["completed_at"]}
    matched_receipt(PREFIX + json.dumps(receipt), kind, expected, target, target + timedelta(hours=1))


def validate_result(result, task_id, paths):
    require(isinstance(result, dict) and set(result) == {"schema_version", "task_id", "plan", "observations", "notifications", "cleanup"}, "invalid_result")
    require(result["schema_version"] == 1 and result["task_id"] == task_id, "result_identity_drift")
    validate_plan(result["plan"], paths, installed=True)
    require(set(result["observations"]) <= set(KINDS) and set(result["notifications"]) <= set(result["observations"]), "invalid_result_kinds")
    require(all(v.get("status") in {"passed", "unconfirmed"} for v in result["observations"].values())
            and all(v in {"attempted", "accepted", "unconfirmed"} for v in result["notifications"].values())
            and result["cleanup"] in {"pending", "passed"}, "invalid_result_status")
    for kind, value in result["observations"].items():
        validate_observation(kind, value, result["plan"])
    if (paths["task"] / "plan.json").exists():
        S.owned(paths["task"] / "plan.json")
        require(json.loads((paths["task"] / "plan.json").read_text()) == result["plan"], "stored_plan_drift")


def units(plan, paths, program):
    driver = S.file_hash(program)
    bundle = S.file_hash(program + SUPPORT.read_bytes())
    script = "%h/.local/lib/personal-server/notification-followup/" + bundle + "/notification-followup-verification.py"
    service = ("[Unit]\nDescription=Verify natural notification delivery\nAfter=network-online.target\nStartLimitIntervalSec=300\nStartLimitBurst=3\n"
               "[Service]\nType=oneshot\nExecStart=/usr/bin/python3 -B " + script + " run --task-id " + plan["task_id"]
               + " --expected-program-sha256 " + driver + "\nTimeoutStartSec=4500\nRestart=on-failure\nRestartSec=60\n"
               "[Install]\nWantedBy=default.target\n")
    calendar = "".join("OnCalendar=" + (stamp(t) + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S UTC") + "\n"
                       for t in plan["targets"].values())
    timer = "[Unit]\nDescription=Two bounded notification verification windows\n[Timer]\n" + calendar + "Persistent=true\nAccuracySec=1s\nUnit=" + paths["unit"] + ".service\n[Install]\nWantedBy=timers.target\n"
    return {"service": service.encode(), "timer": timer.encode()}, bundle


def install(plan, expected, *, home=None, execute=command, read=get, clock_now=now):
    require(os.getuid() != 0 and digest(plan) == expected, "explicit_reviewed_plan_required")
    require(set(plan["targets"]) == set(KINDS) and all(stamp(t) > clock_now() + timedelta(seconds=120) for t in plan["targets"].values()), "installation_too_late")
    require(execute(["loginctl", "show-user", str(os.getuid()), "-p", "Linger", "--value"]) == "yes", "existing_linger_required")
    require(execute(["systemctl", "--user", "is-system-running"]) == "running", "user_manager_unavailable")
    relay(plan, read=read, execute=execute)
    for kind, (ns, name) in CRONS.items():
        current = read("cronjob", ns, name)
        require(current["metadata"]["uid"] == plan["crons"][kind]["uid"]
                and digest(current["spec"]) == plan["crons"][kind]["spec_sha256"], "cron_drift")
    for suffix in ("timer", "service"):
        require(digest(execute(["systemctl", "--user", "cat", SEQUENCE + "." + suffix])) == plan["sequence_" + suffix + "_sha256"], "sequence_drift")
    paths = layout(plan["task_id"], home)
    validate_plan(plan, paths)
    for path in (paths["task"], paths["result"]):
        require(not path.exists() and not path.is_symlink(), "task_already_exists")
    for suffix in ("timer", "service"):
        require(execute(["systemctl", "--user", "show", paths["unit"] + "." + suffix, "-p", "LoadState", "--value"]) == "not-found", "unit_already_loaded")
        path = paths["units"] / (paths["unit"] + "." + suffix)
        require(not path.exists() and not path.is_symlink(), "unit_already_exists")
    program = Path(__file__).read_bytes(); rendered, bundle = units(plan, paths, program)
    S.safe_parents(paths["programs"]); paths["programs"].mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = paths["programs"] / bundle
    payloads = {"notification-followup-verification.py": program, "slo-followup-verification.py": SUPPORT.read_bytes()}
    if directory.exists():
        S.owned(directory, directory=True)
        require({p.name for p in directory.iterdir()} == set(payloads), "runtime_drift")
        for name, value in payloads.items():
            S.owned(directory / name); require((directory / name).read_bytes() == value, "runtime_drift")
    else:
        directory.mkdir(mode=0o700)
        for name, value in payloads.items():
            S.exclusive(directory / name, value)
    S.safe_parents(paths["task"]); S.safe_parents(paths["units"])
    paths["task"].mkdir(parents=True, mode=0o700); paths["units"].mkdir(parents=True, exist_ok=True, mode=0o700)
    stored = {**plan, "units": {k: S.file_hash(v) for k, v in rendered.items()}}
    created = []; reload_attempted = False
    try:
        for path, value in [(paths["task"] / "plan.json", json.dumps(stored, sort_keys=True).encode()),
                            *[(paths["units"] / (paths["unit"] + "." + key), value) for key, value in rendered.items()]]:
            S.exclusive(path, value); created.append((path, S.file_hash(value)))
        execute(["systemd-analyze", "--user", "verify", *[str(paths["units"] / (paths["unit"] + "." + s)) for s in ("service", "timer")]])
        reload_attempted = True; execute(["systemctl", "--user", "daemon-reload"])
        execute(["systemctl", "--user", "enable", paths["unit"] + ".service"])
        require(min(stamp(t) for t in plan["targets"].values()) > clock_now() + timedelta(seconds=120), "installation_window_too_short")
        execute(["systemctl", "--user", "enable", "--now", paths["unit"] + ".timer"])
        require(execute(["systemctl", "--user", "is-active", paths["unit"] + ".timer"]) == "active", "timer_inactive")
    except Exception:
        if reload_attempted:
            execute(["systemctl", "--user", "disable", "--now", paths["unit"] + ".timer"])
            require(execute(["systemctl", "--user", "show", paths["unit"] + ".service", "-p", "ActiveState", "--value"]) in {"inactive", "failed"}, "installation_state_uncertain")
            execute(["systemctl", "--user", "disable", paths["unit"] + ".service"])
        for path, expected_hash in created:
            S.owned(path); require(S.file_hash(path.read_bytes()) == expected_hash, "cleanup_drift")
        for path, _ in reversed(created):
            path.unlink()
        paths["task"].rmdir()
        execute(["systemctl", "--user", "daemon-reload"])
        raise
    return {"installed": True, "task_id": plan["task_id"], "targets": plan["targets"], "program_sha256": S.file_hash(program)}


def cleanup(paths, plan, *, execute=command):
    # Reuse the audited, restart-safe deletion boundary; no collector/schema files here.
    expected_plan = S.file_hash(json.dumps(plan, sort_keys=True).encode())
    S.cleanup(paths, {**plan, "files": {}}, expected_plan, execute=execute)


def result_message(kind, result, cleaned):
    label = "Portal 백업" if kind == "portal_backup" else "정기 감사"
    passed = result["observations"][kind]["status"] == "passed"
    return ("[정기 알림 문구 검증 " + ("완료" if passed else "확인 필요") + "]\n대상: " + label
            + "\n새 문구·실제 Telegram API 전송: " + ("통과" if passed else "미확인 — 증거 부족 또는 기한 만료")
            + "\n사람의 읽음·수신 여부는 자동 확인 대상이 아닙니다."
            + ("\n임시 예약·파일 정리: 완료" if cleaned else "\n감사 검증이 끝나면 임시 예약·파일을 정리합니다.")
            + "\n결과 증거는 N100에 보존합니다.")


def run_task(task_id, *, home=None, clock_now=now, sleep=time.sleep, observe=inspect, deliver=S.send, execute=command):
    paths = layout(task_id, home)
    S.safe_parents(paths["result"].parent); paths["result"].parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = paths["result"].with_suffix(".lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        S.owned(lock); fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if paths["result"].exists() or paths["result"].is_symlink():
            S.owned(paths["result"])
            require(not paths["result"].is_symlink(), "unsafe_result")
            result = json.loads(paths["result"].read_text())
        else:
            result = None
        if result is None:
            S.owned(paths["task"] / "plan.json")
            plan = json.loads((paths["task"] / "plan.json").read_text())
            result = {"schema_version": 1, "task_id": task_id, "plan": plan, "observations": {}, "notifications": {}, "cleanup": "pending"}
            S.atomic_result(paths["result"], result)
        validate_result(result, task_id, paths)
        # A crash after the marker leaves acceptance unknown, never permission to resend.
        for kind, state in result["notifications"].items():
            if state == "attempted":
                result["notifications"][kind] = "unconfirmed"
                S.atomic_result(paths["result"], result)
        plan = result["plan"]
        for kind in KINDS:
            target = stamp(plan["targets"][kind]); deadline = target + timedelta(hours=1)
            if kind in result["observations"] or clock_now() < target:
                continue
            while clock_now() <= deadline:
                try:
                    observation = observe(kind, plan)
                    validate_observation(kind, observation, plan)
                    result["observations"][kind] = observation
                    break
                except Pending:
                    pass
                except Exception:
                    # Unavailable reads are not success evidence. Keep the bounded window open.
                    pass
                sleep(min(60, max(1, (deadline - clock_now()).total_seconds())))
            if kind not in result["observations"]:
                result["observations"][kind] = {"status": "unconfirmed", "reason": "evidence_deadline_expired"}
            S.atomic_result(paths["result"], result)
        complete = all(k in result["observations"] for k in KINDS)
        if complete and result["cleanup"] != "passed":
            cleanup(paths, plan, execute=execute)
            result["cleanup"] = "passed"; S.atomic_result(paths["result"], result)
        for kind in KINDS:
            if kind not in result["observations"] or kind in result["notifications"]:
                continue
            result["notifications"][kind] = "attempted"; S.atomic_result(paths["result"], result)
            try:
                accepted = deliver(plan, result_message(kind, result, complete), execute=execute)
            except Exception:
                accepted = False
            result["notifications"][kind] = "accepted" if accepted else "unconfirmed"
            S.atomic_result(paths["result"], result)
        if complete:
            require(os.fstat(fd).st_ino == lock.lstat().st_ino, "lock_drift"); lock.unlink()
        return result
    finally:
        os.close(fd)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=("prepare", "install", "run")); p.add_argument("--plan")
    p.add_argument("--expected-plan-sha256"); p.add_argument("--expected-program-sha256")
    p.add_argument("--task-id"); p.add_argument("--go", action="store_true")
    args = p.parse_args(argv)
    try:
        if args.mode == "prepare":
            plan = prepare(); plan["task_id"] = stamp(plan["created_at"]).astimezone(KST).strftime("%Y%m%d") + "-" + digest(plan)[:12]
            output = {"plan": plan, "plan_sha256": digest(plan)}
        elif args.mode == "install":
            require(args.go and args.plan and args.expected_plan_sha256, "explicit_install_required")
            output = install(json.loads(Path(args.plan).read_text()), args.expected_plan_sha256)
        else:
            require(args.expected_program_sha256 == S.file_hash(Path(__file__).read_bytes()), "program_drift")
            output = run_task(args.task_id)
        print(json.dumps(output, ensure_ascii=False, sort_keys=True)); return 0
    except Exception:
        print("notification_followup=FAIL reason=controlled_operation_failed"); return 1


if __name__ == "__main__":
    raise SystemExit(main())
