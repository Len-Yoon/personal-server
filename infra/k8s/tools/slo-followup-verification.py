#!/usr/bin/env python3
"""Bounded, one-time N100 SLO verification; never writes Kubernetes objects.

Only install --go creates the user's dedicated units, temporary baseline/schema,
and fingerprinted private runtime artifact. A run removes its temporary files,
preserves the result and runtime artifact, and sends one fixed report through
the existing relay process credentials. Secrets never leave that container.
"""
import argparse
import copy
import fcntl
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import time as clock
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True
KST = ZoneInfo("Asia/Seoul")
KUBE = ["sudo", "-n", "k3s", "kubectl", "--request-timeout=10s", "-n", "monitoring"]
CRON = "slo-daily-evidence"
RELAY = "sre-telegram-relay"
IMAGE = "docker.io/library/personal-server-slo-evidence@sha256:0dc15788788a8500de9f6906543b8a6f1b61f598c26598af19f35b667a57c4a3"
CRITERIA = "freshness-1800-v1"
SEND_CODE = '''import json,sys
from app.main import TelegramClient,_read_configured_secret
try:
 p=json.load(sys.stdin)
 assert set(p)=={'text'} and isinstance(p['text'],str) and len(p['text'])<=2000
 token=_read_configured_secret('TELEGRAM_BOT_TOKEN_FILE')
 chat=_read_configured_secret('ALLOWED_CHAT_ID_FILE')
 ok=TelegramClient(token,timeout_seconds=20).send_message(chat,p['text'])
 print('slo_followup_delivery=PASS' if ok else 'slo_followup_delivery=UNCONFIRMED')
except Exception:
 print('slo_followup_delivery=UNCONFIRMED')
'''


class Failed(Exception):
    """Fixed reason codes only: never propagate command/HTTP error text."""


class Pending(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise Failed(reason)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def file_hash(data):
    return hashlib.sha256(data).hexdigest()


def timestamp(value):
    require(isinstance(value, str), "invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Failed("invalid_timestamp") from None
    require(parsed.tzinfo is not None, "invalid_timestamp")
    return parsed.astimezone(timezone.utc)


def times(day):
    target = date.fromisoformat(day)
    scheduled = datetime.combine(target, time(2, 15), KST).astimezone(timezone.utc)
    return {"scheduled": scheduled, "start": scheduled + timedelta(minutes=5),
            "deadline": scheduled + timedelta(minutes=65)}


def command(args, value=None, *, run=subprocess.run):
    try:
        result = run(args, input=value, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        raise Failed("command_unavailable") from None
    require(result.returncode == 0 and len(result.stdout) <= 8_000_000, "command_unavailable")
    return result.stdout.strip()


def kube(kind, name=None):
    try:
        return json.loads(command([*KUBE, "get", kind, *([name] if name else []), "-o", "json"]))
    except (ValueError, TypeError):
        raise Failed("invalid_api_response") from None


PORTAL_METRICS = {name: 'kube_deployment_' + metric +
                 '{namespace="personal-server",deployment="portal-web"}'
                 for name, metric in (("available", "status_replicas_available"), ("desired", "spec_replicas"))}
PORTAL_PROXY = "/api/v1/namespaces/monitoring/services/http:personal-server-monitoring-prometheus:9090/proxy/api/v1/query?"


def diagnostic_command(args):
    # Five reads at most: 40s total, with no retries. Preserve cleanup/send time.
    def bounded_run(argv, **kwargs):
        return subprocess.run(argv, **{**kwargs, "timeout": 8})
    raw = command(["--request-timeout=5s" if a == "--request-timeout=10s" else a for a in args], run=bounded_run)
    require(len(raw) <= 2_000_000, "diagnostic_response_too_large")
    return raw


def portal_get(kind, name=None):
    require((kind, name) in {("deployment", "portal-web"), ("cronjob", "portal-pvc-backup"),
                            ("jobs", None)}, "invalid_diagnostic_resource")
    return json.loads(diagnostic_command([*KUBE[:-1], "personal-server", "get", kind,
                                          *([name] if name else []), "-o", "json"]))


def portal_matrix(name, at):
    require(name in PORTAL_METRICS, "invalid_diagnostic_query")
    query = urlencode({"query": PORTAL_METRICS[name] + "[24h]", "time": at.timestamp()})
    raw = diagnostic_command([*KUBE, "get", "--raw", PORTAL_PROXY + query])
    return json.loads(raw)


def unknown_portal_context():
    return {"current": "unknown", "history": "unknown", "checked_at": None, "zero_samples": None}


def diagnostic_samples(payload, end):
    require(payload.get("status") == "success" and payload.get("data", {}).get("resultType") == "matrix",
            "invalid_diagnostic_matrix")
    require(not payload.get("warnings") and not payload.get("infos"), "incomplete_diagnostic_matrix")
    series = payload["data"]["result"]
    require(isinstance(series, list) and len(series) == 1, "ambiguous_diagnostic_series")
    require(series[0].get("metric", {}).get("namespace") == "personal-server" and
            series[0]["metric"].get("deployment") == "portal-web", "invalid_diagnostic_series")
    values = series[0]["values"]
    require(isinstance(values, list) and 2 <= len(values) <= 10000, "invalid_diagnostic_samples")
    result = []
    for pair in values:
        require(isinstance(pair, list) and len(pair) == 2 and type(pair[0]) in (int, float),
                "invalid_diagnostic_samples")
        require(isinstance(pair[1], str), "invalid_diagnostic_samples")
        stamp, value = float(pair[0]), float(pair[1])
        require(math.isfinite(stamp) and math.isfinite(value) and value >= 0 and value.is_integer() and
                end - 86400 <= stamp <= end and (not result or 0 < stamp - result[-1][0] <= 90),
                "diagnostic_sample_gap")
        result.append((stamp, int(value)))
    require(result[0][0] - (end - 86400) <= 90 and end - result[-1][0] <= 90, "diagnostic_sample_gap")
    return result


def classify_portal_context(record, deployment, cron, jobs, available_payload, desired_payload, checked_at):
    """Explain observed unavailability; never rewrite raw records or exempt SLO failures."""
    result = unknown_portal_context()
    result["checked_at"] = checked_at.astimezone(timezone.utc).isoformat()
    observed = timestamp(record["collected_at"])
    require(timedelta(0) <= checked_at - observed <= timedelta(hours=36), "stale_diagnostic_record")
    require(record["portal_ready"] in {"ok", "failed", "unobservable"}, "invalid_diagnostic_record")
    meta, spec, status = deployment["metadata"], deployment["spec"], deployment["status"]
    require(not meta.get("deletionTimestamp") and type(meta["generation"]) is int and
            type(status.get("observedGeneration")) is int and
            status["observedGeneration"] >= meta["generation"], "stale_deployment_status")
    wanted = spec.get("replicas")
    ready, available = status.get("readyReplicas", 0), status.get("availableReplicas", 0)
    require(all(type(v) is int and v in (0, 1) for v in (wanted, ready, available)), "invalid_deployment_status")
    result["current"] = "available" if wanted > 0 and ready >= wanted and available >= wanted else "unavailable"
    # A recreated deployment cannot explain an older name-only metric series.
    require(timestamp(meta["creationTimestamp"]) <= observed - timedelta(hours=24), "deployment_identity_changed")
    av = diagnostic_samples(available_payload, observed.timestamp())
    de = diagnostic_samples(desired_payload, observed.timestamp())
    require([t for t, _ in av] == [t for t, _ in de], "diagnostic_timestamp_mismatch")
    require(record["portal_ready"] != "unobservable", "raw_portal_unobservable")
    zero_count = sum(v == 0 for _, v in av)
    require((record["portal_ready"] == "failed") == (zero_count > 0), "raw_diagnostic_conflict")
    result["zero_samples"] = zero_count
    if not zero_count:
        result["history"] = "none"
        return result
    require(not cron["metadata"].get("deletionTimestamp") and
            timestamp(cron["metadata"]["creationTimestamp"]) <= observed - timedelta(hours=24),
            "cron_identity_changed")
    require(isinstance(jobs, list) and len(jobs) <= 200, "invalid_diagnostic_jobs")
    windows = []
    expected_spec = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    for job in jobs:
        refs = job.get("metadata", {}).get("ownerReferences", [])
        controllers = [r for r in refs if r.get("controller") is True]
        if len(controllers) != 1:
            continue
        parent = controllers[0]
        if parent.get("kind") != "CronJob" or parent.get("uid") != cron["metadata"]["uid"]:
            continue
        js = job.get("status", {})
        conditions = js.get("conditions", [])
        if (job.get("metadata", {}).get("deletionTimestamp") or type(js.get("succeeded")) is not int or js["succeeded"] != 1 or
                js.get("failed", 0) or js.get("active", 0) or
                not any(c.get("type") == "Complete" and c.get("status") == "True" for c in conditions) or
                any(c.get("type") in {"Failed", "FailureTarget"} and c.get("status") == "True" for c in conditions)):
            continue
        if job.get("spec", {}).get("template", {}).get("spec") != expected_spec:
            continue
        started, ended = timestamp(js["startTime"]), timestamp(js["completionTime"])
        if observed - timedelta(hours=24) <= started <= ended <= observed and ended - started <= timedelta(hours=5):
            windows.append((started.timestamp(), ended.timestamp()))
    # Recognize one continuous unavailable run, including restart readiness lag.
    # A recovered sample closes it: later failures need their own pause anchor.
    runs = []
    for i, (stamp, value) in enumerate(av):
        if value == 0:
            if not runs or i != runs[-1][-1] + 1:
                runs.append([])
            runs[-1].append(i)
    matched = []
    for run in runs:
        first, last = run[0], run[-1]
        matched.append(de[first][1] == 0 and all(de[i][1] in (0, 1) for i in run) and
                       sum(start <= av[first][0] <= av[last][0] <= end for start, end in windows) == 1)
    if all(matched):
        result["history"] = "backup_overlap"
    elif any(de[run[0]][1] > 0 for run, covered in zip(runs, matched) if not covered):
        result["history"] = "unexplained"
    return result


def read_portal_context(record, *, get=portal_get, query=portal_matrix, now=None):
    """Bounded read-only diagnostics; unavailable evidence cannot break delivery."""
    try:
        at = timestamp(record["collected_at"])
        return classify_portal_context(record, get("deployment", "portal-web"),
                                       get("cronjob", "portal-pvc-backup"), get("jobs")["items"],
                                       query("available", at), query("desired", at),
                                       now or datetime.now(timezone.utc))
    except Exception:
        return unknown_portal_context()


def portal_context_lines(value):
    if not isinstance(value, dict):
        value = unknown_portal_context()
    history = {"backup_overlap": "백업 계획 정지·복귀 구간과 일치", "unexplained": "계획 백업으로 설명되지 않는 무가용 관측·장애 확인 필요",
               "none": "무가용 표본 없음", "unknown": "원인 확인 필요 (진단 근거 부족)"}
    current = {"available": "가용 복제본 정상", "unavailable": "가용 복제본 부족·장애 여부 확인 필요", "unknown": "확인 필요"}
    try:
        checked = timestamp(value.get("checked_at")).astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
    except Exception:
        checked = "시각 확인 불가"
    return ["Portal 과거 24시간: " + history.get(value.get("history"), history["unknown"]),
            "Portal 조회 상태: " + current.get(value.get("current"), current["unknown"]) + " (" + checked + " 기준)",
            "백업 구간 일치는 관측 설명이며 원시 SLO 판정은 유지합니다."]


def layout(day, home=None):
    require(re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or "") is not None, "invalid_date")
    date.fromisoformat(day)
    home = Path.home() if home is None else Path(home)
    ident = day.replace("-", "")
    unit = "personal-server-slo-followup-" + ident
    return {"task": home / ".local/state/personal-server" / ("slo-followup-" + ident),
            "units": home / ".config/systemd/user", "unit": unit,
            "programs": home / ".local/lib/personal-server/slo-followup-verification",
            "result": home / ".local/state/personal-server/slo-followup-results" / (ident + ".json")}


def owned(path, *, directory=False):
    info = path.lstat()
    require(info.st_uid == os.getuid() and (stat.S_ISDIR(info.st_mode) if directory
            else stat.S_ISREG(info.st_mode)), "unsafe_file")
    require(info.st_mode & 0o022 == 0, "unsafe_file")


def safe_parents(path):
    for parent in [path, *path.parents]:
        if parent.exists() or parent.is_symlink():
            require(not parent.is_symlink() and parent.is_dir(), "unsafe_parent")


def exclusive(path, data):
    safe_parents(path.parent)
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def atomic_result(path, value):
    safe_parents(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    sync_directory(path.parent.parent)
    if path.exists() or path.is_symlink():
        owned(path)
    # A killed writer must not strand a fixed .next name and block recovery.
    prefix = "." + path.name + ".tmp-"
    for stale in path.parent.glob(prefix + "*"):
        require(re.fullmatch(re.escape(prefix) + r"[a-f0-9]{32}", stale.name), "unsafe_result_temp")
        owned(stale)
        stale.unlink()
    temporary = path.parent / (prefix + secrets.token_hex(16))
    try:
        exclusive(temporary, (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode())
        temporary.replace(path)
        sync_directory(path.parent)
    finally:
        if temporary.exists():
            owned(temporary)
            temporary.unlink()


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def verify_program(expected):
    require(re.fullmatch(r"[a-f0-9]{64}", expected or "") is not None, "program_fingerprint_required")
    path = Path(__file__).resolve()
    safe_parents(path.parent)
    owned(path)
    require(file_hash(path.read_bytes()) == expected, "program_source_drift")


def install_program(directory, data):
    """Publish a fingerprinted Linux-home release artifact without overwriting one."""
    safe_parents(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    owned(directory, directory=True)
    expected = file_hash(data)
    prefix = "." + expected + ".tmp-"
    for stale in directory.glob(prefix + "*"):
        require(re.fullmatch(re.escape(prefix) + r"[a-f0-9]{32}", stale.name), "unsafe_program_temp")
        owned(stale)
        stale.unlink()
    target = directory / (expected + ".py")
    if target.exists() or target.is_symlink():
        owned(target)
        require(file_hash(target.read_bytes()) == expected, "program_source_drift")
        return target, False
    temporary = directory / (prefix + secrets.token_hex(16))
    created = False
    try:
        exclusive(temporary, data)
        try:
            os.link(temporary, target, follow_symlinks=False)
            created = True
        except FileExistsError:
            owned(target)
            require(file_hash(target.read_bytes()) == expected, "program_source_drift")
        sync_directory(directory)
        sync_directory(directory.parent)
    except Exception:
        if created:
            owned(target)
            require(file_hash(target.read_bytes()) == expected, "program_source_drift")
            target.unlink()
        raise
    finally:
        if temporary.exists():
            owned(temporary)
            temporary.unlink()
            sync_directory(directory)
    return target, created


def read_records(cm, validator):
    try:
        rows = json.loads(cm["data"]["records.json"])
        require(isinstance(rows, list) and len(rows) == 30, "invalid_records")
        for row in rows:
            validator(row)
        require(len({row["date"] for row in rows}) == 30, "duplicate_dates")
        return rows
    except (KeyError, TypeError, ValueError):
        raise Failed("invalid_records") from None


def load_validator(path):
    spec = importlib.util.spec_from_file_location("_slo_followup_record_schema", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate_record


def make_plan(day, *, get=kube, validator=None):
    spans = times(day)
    validator = validator or load_validator(Path(__file__).with_name("slo-daily-evidence.py"))
    cron, cm, relay = get("cronjob", CRON), get("configmap", CRON), get("deployment", RELAY)
    cs = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
    require(cron["spec"].get("schedule") == "15 2 * * *" and
            cron["spec"].get("timeZone") == "Asia/Seoul" and cron["spec"].get("suspend") is False and
            cron["spec"].get("concurrencyPolicy") == "Forbid" and len(cs) == 1 and
            cs[0]["name"] == "collector" and cs[0]["image"] == IMAGE, "cron_drift")
    rows = read_records(cm, validator)
    target = date.fromisoformat(day)
    dates = {(target - timedelta(days=i)).isoformat() for i in range(1, 31)}
    require({row["date"] for row in rows} == dates, "baseline_window_mismatch")
    require(relay["spec"].get("replicas") == 1 and relay.get("status", {}).get("readyReplicas") == 1,
            "relay_unavailable")
    return {"schema_version": 1, "target_date": day,
            **{key + "_at": value.isoformat() for key, value in spans.items()},
            "cron_uid": cron["metadata"]["uid"], "cm_uid": cm["metadata"]["uid"],
            "cron_spec_sha256": digest(cron["spec"]), "records": rows, "records_sha256": digest(rows),
            "relay_uid": relay["metadata"]["uid"], "relay_spec_sha256": digest(relay["spec"])}


def owner(obj, kind, uid):
    refs = obj.get("metadata", {}).get("ownerReferences", [])
    return any(x.get("kind") == kind and x.get("uid") == uid and x.get("controller") is True for x in refs)


def inspect(plan, *, get=kube, validator):
    spans = times(plan["target_date"])
    cron = get("cronjob", CRON)
    require(cron["metadata"]["uid"] == plan["cron_uid"] and
            digest(cron["spec"]) == plan["cron_spec_sha256"], "cron_drift")
    candidates = []
    for job in get("jobs")["items"]:
        if not owner(job, "CronJob", plan["cron_uid"]):
            continue
        annotation = job["metadata"].get("annotations", {}).get("batch.kubernetes.io/cronjob-scheduled-timestamp")
        if annotation and timestamp(annotation) == spans["scheduled"]:
            candidates.append(job)
    if not candidates:
        raise Pending("scheduled_job_not_observed")
    require(len(candidates) == 1, "ambiguous_scheduled_job")
    job = candidates[0]
    conditions = job.get("status", {}).get("conditions", [])
    require(not any(x.get("type") in ("Failed", "FailureTarget") and x.get("status") == "True"
                    for x in conditions) and not job.get("status", {}).get("failed", 0), "job_failed")
    if not any(x.get("type") == "Complete" and x.get("status") == "True" for x in conditions):
        raise Pending("scheduled_job_incomplete")
    require(job["status"].get("succeeded") == 1 and not job["status"].get("active", 0), "invalid_job_status")
    pod_spec = job["spec"]["template"]["spec"]
    expected = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    require(pod_spec == expected, "job_template_drift")
    pods = [p for p in get("pods")["items"] if owner(p, "Job", job["metadata"]["uid"])]
    require(len(pods) == 1, "ambiguous_job_pods")
    pod = pods[0]
    statuses = pod.get("status", {}).get("containerStatuses", [])
    require(pod.get("status", {}).get("phase") == "Succeeded" and len(statuses) == 1 and
            statuses[0].get("name") == "collector" and statuses[0].get("restartCount") == 0 and
            statuses[0].get("state", {}).get("terminated", {}).get("exitCode") == 0 and
            statuses[0].get("imageID", "").endswith("@" + IMAGE.split("@", 1)[1]), "runtime_image_or_exit_mismatch")
    cm = get("configmap", CRON)
    require(cm["metadata"]["uid"] == plan["cm_uid"], "configmap_identity_drift")
    rows = read_records(cm, validator)
    new = [row for row in rows if row["date"] == plan["target_date"]]
    require(len(new) == 1 and new[0].get("criteria_version") == CRITERIA, "new_criteria_missing")
    collected = timestamp(new[0]["collected_at"])
    started = timestamp(job["status"]["startTime"])
    completed = timestamp(job["status"]["completionTime"])
    require(spans["scheduled"] <= started <= collected <= completed <= spans["deadline"], "collection_time_mismatch")
    expected_rows = sorted([*plan["records"], new[0]], key=lambda r: r["date"], reverse=True)[:30]
    require(rows == expected_rows, "existing_records_changed")
    return {"verification": "passed", "scheduled_at": spans["scheduled"].isoformat(),
            "job_uid": job["metadata"]["uid"], "pod_uid": pod["metadata"]["uid"],
            "image_id": statuses[0]["imageID"], "new_record": new[0],
            "baseline_row_hashes": {r["date"]: digest(r) for r in plan["records"]},
            "retained_row_hashes": {r["date"]: digest(r) for r in rows if r is not new[0]},
            "preserved_rows": 29, "expired_dates": [min(r["date"] for r in plan["records"])]}


def render_units(day):
    folder = Path(__file__).resolve().parents[1] / "slo-evidence"
    values = {"@DATE@": day, "@ID@": day.replace("-", ""),
              "@CALENDAR@": times(day)["start"].strftime("%Y-%m-%d %H:%M:%S UTC"),
              "@PROGRAM_SHA256@": file_hash(Path(__file__).read_bytes())}
    result = {}
    for suffix in ("service", "timer"):
        text = (folder / ("slo-followup-verification." + suffix + ".tmpl")).read_text()
        for key, value in values.items():
            text = text.replace(key, value)
        result[suffix] = text.encode()
    return result


def install(day, expected_records, *, home=None, get=kube, execute=command, now=None, clock_now=None):
    require(os.getuid() != 0, "root_install_forbidden")
    # Preserve datetime injection for offline callers; production rechecks real time.
    clock_now = clock_now or ((lambda: now) if now is not None else (lambda: datetime.now(timezone.utc)))
    now = now or clock_now()
    require(now < times(day)["start"], "installation_too_late")
    require(execute(["loginctl", "show-user", str(os.getuid()), "-p", "Linger", "--value"]) == "yes",
            "existing_linger_required")
    require(execute(["systemctl", "--user", "is-system-running"]) == "running", "user_manager_unavailable")
    paths = layout(day, home)
    require(not paths["task"].exists() and not paths["task"].is_symlink(), "task_already_exists")
    require(not paths["result"].exists() and not paths["result"].is_symlink(), "result_already_exists")
    for suffix in ("timer", "service"):
        path = paths["units"] / (paths["unit"] + "." + suffix)
        require(not path.exists() and not path.is_symlink(), "unit_already_exists")
        require(execute(["systemctl", "--user", "show", paths["unit"] + "." + suffix,
                         "-p", "LoadState", "--value"]) == "not-found", "unit_already_loaded")
    plan = make_plan(day, get=get)
    require(plan["records_sha256"] == expected_records, "reviewed_baseline_changed")
    units = render_units(day)
    payloads = {"record_schema.py": Path(__file__).with_name("slo-daily-evidence.py").read_bytes()}
    plan["files"] = {name: file_hash(data) for name, data in payloads.items()}
    plan["units"] = {key: file_hash(data) for key, data in units.items()}
    program_data = Path(__file__).read_bytes()
    plan["program_sha256"] = file_hash(program_data)
    safe_parents(paths["task"].parent)
    safe_parents(paths["units"])
    paths["task"].mkdir(parents=True, mode=0o700)
    paths["units"].mkdir(parents=True, exist_ok=True, mode=0o700)
    created = []
    manager_reload_attempted = False
    try:
        program, program_created = install_program(paths["programs"], program_data)
        if program_created:
            created.append((program, plan["program_sha256"]))
        for name, data in {**payloads, "plan.json": (json.dumps(plan, ensure_ascii=False, sort_keys=True) + "\n").encode()}.items():
            path = paths["task"] / name
            exclusive(path, data)
            created.append((path, file_hash(data)))
        for suffix, data in units.items():
            path = paths["units"] / (paths["unit"] + "." + suffix)
            exclusive(path, data)
            created.append((path, file_hash(data)))
        sync_directory(paths["task"])
        sync_directory(paths["task"].parent)
        sync_directory(paths["units"])
        execute(["systemd-analyze", "--user", "verify", *[str(paths["units"] / (paths["unit"] + "." + s)) for s in ("timer", "service")]])
        manager_reload_attempted = True
        execute(["systemctl", "--user", "daemon-reload"])
        execute(["systemctl", "--user", "enable", paths["unit"] + ".service"])
        require((times(day)["start"] - clock_now()).total_seconds() >= 120,
                "installation_window_too_short")
        execute(["systemctl", "--user", "enable", "--now", paths["unit"] + ".timer"])
        require(execute(["systemctl", "--user", "is-active", paths["unit"] + ".timer"]) == "active", "timer_not_active")
        require(execute(["systemctl", "--user", "is-enabled", paths["unit"] + ".timer"]) == "enabled", "timer_not_enabled")
        scheduled = execute(["systemctl", "--user", "show", paths["unit"] + ".timer", "-p", "NextElapseUSecRealtime", "--value"])
        require(bool(scheduled), "timer_next_elapse_missing")
    except Exception:
        # A timed-out activation may already be running. Never stop that service
        # or remove its files unless inactivity and timer disarming are confirmed.
        try:
            if manager_reload_attempted:
                execute(["systemctl", "--user", "disable", "--now", paths["unit"] + ".timer"])
                state = execute(["systemctl", "--user", "show", paths["unit"] + ".service",
                                 "-p", "ActiveState", "--value"])
                require(state in ("inactive", "failed"), "installation_state_uncertain")
                execute(["systemctl", "--user", "disable", paths["unit"] + ".service"])
            else:
                require(execute(["systemctl", "--user", "show", paths["unit"] + ".service",
                                 "-p", "LoadState", "--value"]) == "not-found", "installation_state_uncertain")
        except Exception:
            raise Failed("installation_state_uncertain") from None
        for path, expected in created:
            owned(path)
            require(file_hash(path.read_bytes()) == expected, "cleanup_drift")
        for path, expected in reversed(created):
            path.unlink()
        paths["task"].rmdir()
        execute(["systemctl", "--user", "daemon-reload"])
        raise
    return {"installed": True, "target_date": day, "start_at": plan["start_at"],
            "deadline_at": plan["deadline_at"], "records_sha256": plan["records_sha256"],
            "program_sha256": plan["program_sha256"], "timer_active": True}


def load_task(day, home=None):
    paths = layout(day, home)
    safe_parents(paths["task"])
    owned(paths["task"], directory=True)
    require({p.name for p in paths["task"].iterdir()} == {"record_schema.py", "plan.json"}, "task_file_drift")
    path = paths["task"] / "plan.json"
    owned(path)
    raw = path.read_bytes()
    plan = json.loads(raw)
    require(plan.get("schema_version") == 1 and plan.get("target_date") == day, "invalid_plan")
    require(plan.get("program_sha256") == file_hash(Path(__file__).read_bytes()), "program_source_drift")
    require(plan.get("records_sha256") == digest(plan["records"]), "invalid_plan")
    require(set(plan.get("files", {})) == {"record_schema.py"} and
            set(plan.get("units", {})) == {"timer", "service"}, "invalid_plan")
    for name, expected in plan["files"].items():
        path = paths["task"] / name
        owned(path)
        require(file_hash(path.read_bytes()) == expected, "task_source_drift")
    return paths, plan, file_hash(raw)


def cleanup(paths, plan, plan_hash, *, execute=command):
    safe_parents(paths["units"])
    safe_parents(paths["task"])
    files = {paths["task"] / name: value for name, value in {**plan["files"], "plan.json": plan_hash}.items()}
    files.update({paths["units"] / (paths["unit"] + "." + suffix): value for suffix, value in plan["units"].items()})
    if paths["task"].exists():
        owned(paths["task"], directory=True)
        require({p.name for p in paths["task"].iterdir()} <= {*plan["files"], "plan.json"}, "cleanup_drift")
    for path, expected in files.items():
        if path.exists() or path.is_symlink():
            owned(path)
            require(file_hash(path.read_bytes()) == expected, "cleanup_drift")
    timer_path = paths["units"] / (paths["unit"] + ".timer")
    if timer_path.exists():
        execute(["systemctl", "--user", "disable", "--now", paths["unit"] + ".timer"])
        execute(["systemctl", "--user", "clean", "--what=state", paths["unit"] + ".timer"])
    # Keep the boot-enabled service until the last short removal window.
    service_path = paths["units"] / (paths["unit"] + ".service")
    task_files = [p for p in files if p.parent == paths["task"]]
    for path in task_files:
        if path.exists():
            path.unlink()
    if paths["task"].exists():
        paths["task"].rmdir()
        sync_directory(paths["task"].parent)
    if service_path.exists():
        execute(["systemctl", "--user", "disable", paths["unit"] + ".service"])
    for path in files:
        if path.exists():
            path.unlink()
    sync_directory(paths["units"])
    execute(["systemctl", "--user", "daemon-reload"])
    require(all(not p.exists() for p in files) and not paths["task"].exists(), "cleanup_incomplete")


def message(result):
    good = result["verification"] == "passed"
    lines = ["[SLO 정기 수집 검증 " + ("완료]" if good else "확인 필요]"),
             "대상: " + result["target_date"] + " 02:15 KST",
             "신규 기준 저장·실제 실행: " + ("통과" if good else "확인 필요 (" + result["reason"] + ")")]
    if good:
        row = result["new_record"]
        lines += ["기존 29개 기록: 전체 필드 보존", "보관 만료: " + result["expired_dates"][0] + " 1건",
                  "측정 결과: 공개 health=" + row["public_health"] + ", Portal=" + row["portal_ready"] +
                  ", Crawler=" + row["crawler_freshness"],
                  *portal_context_lines(result.get("portal_context")),
                  "수집 성공은 과거 30일 SLO 통과를 뜻하지 않습니다."]
    lines += ["임시 예약·파일 정리: " + ("완료" if result["cleanup"] == "passed" else "확인 필요"),
              "결과 증거는 N100에 보존합니다."]
    return "\n".join(lines)


def send(plan, text, *, get=kube, execute=command):
    relay = get("deployment", RELAY)
    require(relay["metadata"]["uid"] == plan["relay_uid"] and
            digest(relay["spec"]) == plan["relay_spec_sha256"] and
            relay.get("status", {}).get("readyReplicas") == 1, "relay_drift_or_unavailable")
    answer = execute([*KUBE, "exec", "-i", "deployment/" + RELAY, "--", "python", "-c", SEND_CODE],
                     json.dumps({"text": text}, ensure_ascii=False))
    return answer == "slo_followup_delivery=PASS"


def run_task(day, *, home=None, now=None, sleep=clock.sleep, get=kube, execute=command):
    paths = layout(day, home)
    require((now or (lambda: datetime.now(timezone.utc)))() >= times(day)["start"], "execution_too_early")
    safe_parents(paths["result"].parent)
    paths["result"].parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    sync_directory(paths["result"].parent.parent)
    lock = paths["result"].with_suffix(".lock")
    if lock.exists() or lock.is_symlink():
        owned(lock)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        owned(lock)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failed("execution_locked") from None
        result = resume_task(day, paths=paths, home=home, now=now, sleep=sleep, get=get, execute=execute)
        if result["phase"] == "complete":
            owned(lock)
            require(os.fstat(fd).st_ino == lock.lstat().st_ino, "lock_identity_drift")
            lock.unlink()
            sync_directory(lock.parent)
        return result
    finally:
        os.close(fd)


def validate_resume(result, day):
    require(result.get("schema_version") == 2 and result.get("target_date") == day and
            result.get("phase") in ("verification", "cleanup", "delivery", "complete") and
            result.get("delivery") in ("not_attempted", "attempting", "accepted", "unconfirmed"), "invalid_result")
    require(result.get("program_sha256") == file_hash(Path(__file__).read_bytes()), "program_source_drift")
    manifest = result.get("cleanup_manifest", {})
    require(set(manifest) == {"files", "units", "plan_hash"} and
            set(manifest["files"]) == {"record_schema.py"} and
            set(manifest["units"]) == {"timer", "service"}, "invalid_result")
    require(all(re.fullmatch(r"[a-f0-9]{64}", value or "") for value in
                [*manifest["files"].values(), *manifest["units"].values(), manifest["plan_hash"]]), "invalid_result")
    require(set(result.get("relay", {})) == {"relay_uid", "relay_spec_sha256"} and
            isinstance(result["relay"]["relay_uid"], str) and
            re.fullmatch(r"[a-f0-9]{64}", result["relay"]["relay_spec_sha256"] or ""), "invalid_result")
    require(type(result.get("cleanup_attempts")) is int and 0 <= result["cleanup_attempts"] <= 3,
            "invalid_result")


def resume_task(day, *, paths, home=None, now=None, sleep=clock.sleep, get=kube, execute=command):
    now = now or (lambda: datetime.now(timezone.utc))
    plan = None
    if paths["result"].exists() or paths["result"].is_symlink():
        owned(paths["result"])
        result = json.loads(paths["result"].read_bytes())
        validate_resume(result, day)
        if result["phase"] == "complete":
            return result
    else:
        _, plan, plan_hash = load_task(day, home)
        result = {"schema_version": 2, "target_date": day, "phase": "verification",
                  "program_sha256": plan["program_sha256"],
                  "verification": "unconfirmed", "reason": "deadline_reached",
                  "cleanup": "unconfirmed", "cleanup_attempts": 0, "delivery": "not_attempted",
                  "cleanup_manifest": {"files": plan["files"], "units": plan["units"], "plan_hash": plan_hash},
                  "relay": {key: plan[key] for key in ("relay_uid", "relay_spec_sha256")}}
        atomic_result(paths["result"], result)
    if result["phase"] == "verification":
        _, plan, plan_hash = load_task(day, home)
        require(result["cleanup_manifest"] == {"files": plan["files"], "units": plan["units"],
                                                "plan_hash": plan_hash} and
                result["relay"] == {key: plan[key] for key in ("relay_uid", "relay_spec_sha256")}, "task_source_drift")
        validator = load_validator(paths["task"] / "record_schema.py")
        unavailable = 0
        try:
            while now() < times(day)["deadline"]:
                try:
                    result.update(inspect(plan, get=get, validator=validator))
                    result["reason"] = "none"
                    break
                except Pending:
                    unavailable = 0
                except Failed as error:
                    if str(error) != "command_unavailable":
                        raise
                    unavailable += 1
                    if unavailable >= 3:
                        raise
                sleep(min(30, max(0, (times(day)["deadline"] - now()).total_seconds())))
        except Failed as error:
            result.update(verification="failed", reason=str(error))
        except Exception:
            result.update(verification="unconfirmed", reason="verification_unavailable")
        if result["verification"] == "passed":
            try:
                result["portal_context"] = (read_portal_context(result["new_record"])
                                            if now() < times(day)["deadline"] - timedelta(seconds=45)
                                            else unknown_portal_context())
            except Exception:
                result["portal_context"] = unknown_portal_context()
        result["phase"] = "cleanup"
        atomic_result(paths["result"], result)
    if result["phase"] == "cleanup":
        while result["cleanup_attempts"] < 3:
            result["cleanup_attempts"] += 1
            atomic_result(paths["result"], result)
            try:
                cleanup(paths, result["cleanup_manifest"], result["cleanup_manifest"]["plan_hash"], execute=execute)
                result["cleanup"] = "passed"
                break
            except Exception:
                result["cleanup"] = "failed"
                atomic_result(paths["result"], result)
                if result["cleanup_attempts"] < 3:
                    sleep(30)
        if result["cleanup"] != "passed":
            result["cleanup"] = "failed"
        result["phase"] = "delivery"
        atomic_result(paths["result"], result)
    # The durable marker deliberately sacrifices guaranteed delivery for at-most-once sending.
    if result["delivery"] != "not_attempted":
        if result["delivery"] == "attempting":
            result["delivery"] = "unconfirmed"
        result.update(phase="complete", finished_at=now().isoformat())
        atomic_result(paths["result"], result)
        return result
    result["delivery"] = "attempting"
    atomic_result(paths["result"], result)
    try:
        result["delivery"] = "accepted" if send(result["relay"], message(result), get=get, execute=execute) else "unconfirmed"
    except Exception:
        result["delivery"] = "unconfirmed"
    result.update(phase="complete", finished_at=now().isoformat())
    atomic_result(paths["result"], result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "install", "run", "due"))
    parser.add_argument("--target-date", required=True)
    parser.add_argument("--expected-records-sha256")
    parser.add_argument("--expected-program-sha256")
    parser.add_argument("--go", action="store_true")
    args = parser.parse_args(argv)
    try:
        window_date = date.fromisoformat(args.target_date)
        require(window_date.isoformat() == args.target_date, "invalid_date")
        if args.mode == "prepare":
            result = make_plan(args.target_date)
        elif args.mode == "install":
            require(args.go and re.fullmatch(r"[a-f0-9]{64}", args.expected_records_sha256 or ""), "explicit_install_required")
            result = install(args.target_date, args.expected_records_sha256)
        elif args.mode == "due":
            verify_program(args.expected_program_sha256)
            return 0 if datetime.now(timezone.utc) >= times(args.target_date)["start"] else 1
        else:
            verify_program(args.expected_program_sha256)
            result = run_task(args.target_date)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        # A terminal verification failure or unknown delivery is evidence, not a reason to resend.
        return 0
    except Exception:
        print("slo_followup=FAIL reason=controlled_operation_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
