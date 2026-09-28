#!/usr/bin/env python3
"""Check or narrowly reconcile the three service PVC backup CronJobs."""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[3]
TARGET = ROOT / "infra/k8s/backup-automation/production-cronjob-state.json"
SCHEDULES = {
    "book-pvc-backup": ("0 4 * * *", "book-memo", "book-memo-data"),
    "youtube-pvc-backup": ("30 8 * * *", "youtube-memo", "youtube-memo-data"),
    "crawler-pvc-backup": ("0 13 * * *", "crawler-worker", "crawler-worker-data"),
}
KUBECTL = ("sudo", "-n", "k3s", "kubectl")
RUNTIME_MARKER = "/var/lib/personal-server/k3s-runtime-services.state"


class StateError(Exception):
    pass


def command(*args, input_text=None):
    completed = subprocess.run(args, input=input_text, text=True, capture_output=True, check=False)
    if completed.returncode:
        raise StateError(f"command failed: {args[0]} {args[1] if len(args) > 1 else ''}")
    return completed.stdout


def kubectl(*args, input_text=None):
    return command(*KUBECTL, *args, input_text=input_text)


def get_json(namespace, resource, name=None):
    args = ["-n", namespace, "get", resource]
    if name:
        args.append(name)
    return json.loads(kubectl(*args, "-o", "json"))


def validate_target(path=TARGET):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise StateError("target file is unavailable or invalid") from exc
    if set(data) != {"schemaVersion", "namespace", "cronJobs"} or data["schemaVersion"] != 1 or data["namespace"] != "personal-server":
        raise StateError("target header mismatch")
    entries = data["cronJobs"]
    if not isinstance(entries, list) or len(entries) != 3:
        raise StateError("exactly three CronJobs required")
    names = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"name", "schedule", "timeZone", "startingDeadlineSeconds", "suspend", "image"}:
            raise StateError("target entry schema mismatch")
        name = entry["name"]
        if name not in SCHEDULES or name in names:
            raise StateError("target name mismatch")
        names.add(name)
        if (entry["schedule"] != SCHEDULES[name][0] or entry["timeZone"] != "Asia/Seoul" or
                type(entry["startingDeadlineSeconds"]) is not int or not 60 <= entry["startingDeadlineSeconds"] <= 300 or
                type(entry["suspend"]) is not bool):
            raise StateError(f"target schedule or state mismatch: {name}")
        expected_image = f"docker.io/library/personal-server-{name}@sha256:"
        if not isinstance(entry["image"], str) or not re.fullmatch(re.escape(expected_image) + r"[0-9a-f]{64}", entry["image"]):
            raise StateError(f"target image mismatch: {name}")
        manifest = ROOT / "infra/k8s/backup-automation" / f"{SCHEDULES[name][1]}-pvc-backup-cronjob.yaml"
        content = manifest.read_text()
        for line in (f'  schedule: "{entry["schedule"]}"', "  timeZone: Asia/Seoul",
                     f'  startingDeadlineSeconds: {entry["startingDeadlineSeconds"]}', "  suspend: true"):
            if content.count(line) != 1:
                raise StateError(f"bootstrap manifest differs: {name}")
    if names != set(SCHEDULES):
        raise StateError("target set mismatch")
    return entries


def git_guard(expected_sha):
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise StateError("expected SHA must have 40 lowercase hex characters")
    head = command("git", "rev-parse", "HEAD").strip()
    origin = command("git", "rev-parse", "origin/main").strip()
    if head != expected_sha or origin != expected_sha or command("git", "status", "--porcelain", "--untracked-files=no").strip():
        raise StateError("N100 checkout must be clean at the approved origin/main commit")


def assert_quiet_window(entry, now):
    minute, hour = (int(part) for part in entry["schedule"].split()[:2])
    local = now.astimezone(ZoneInfo("Asia/Seoul"))
    scheduled = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if scheduled - timedelta(seconds=60) <= local < scheduled + timedelta(seconds=entry["startingDeadlineSeconds"]):
        raise StateError(f"scheduled backup window is active: {entry['name']}")


def assert_template(name, spec, pod, init, backup):
    service = SCHEDULES[name][1]
    short = name.split("-", 1)[0]
    job_spec = spec["jobTemplate"]["spec"]
    volumes = {item.get("name"): item for item in pod.get("volumes", [])}
    mounts = {item.get("name"): item for item in backup.get("volumeMounts", [])}
    init_mounts = {item.get("name"): item for item in init.get("volumeMounts", [])}
    secret = volumes.get("credentials", {}).get("secret", {})
    secret_keys = {item.get("key") for item in secret.get("items", [])}
    pod_security = pod.get("securityContext", {})
    init_security = init.get("securityContext", {})
    backup_security = backup.get("securityContext", {})
    if (job_spec.get("backoffLimit") != 0 or job_spec.get("activeDeadlineSeconds") != 14400 or
            job_spec.get("ttlSecondsAfterFinished") != 86400 or
            spec.get("successfulJobsHistoryLimit") != 1 or spec.get("failedJobsHistoryLimit") != 1 or
            pod.get("serviceAccountName") != name or pod.get("restartPolicy") != "Never" or
            pod.get("terminationGracePeriodSeconds") != 300 or
            len(volumes) != 3 or len(mounts) != 3 or len(init_mounts) != 1 or
            init_mounts.get("work", {}).get("mountPath") != "/work" or
            mounts.get("work", {}).get("mountPath") != "/work" or
            init.get("command") != ["/bin/sh", "-ec", "chown 10001:10001 /work && chmod 0700 /work"] or
            backup.get("command") != [f"/opt/personal-server/{name}-verify.py", "--go"] or
            volumes.get(short + "-data", {}).get("persistentVolumeClaim", {}).get("claimName") != SCHEDULES[name][2] or
            volumes.get(short + "-data", {}).get("persistentVolumeClaim", {}).get("readOnly") is not True or
            secret.get("secretName") != f"{service}-pvc-backup-runtime" or
            secret.get("optional") is not False or secret.get("defaultMode") != 292 or
            secret_keys != {"rclone-config", "rclone-config-passphrase", "age-recipient", "age-identity"} or
            volumes.get("work", {}).get("emptyDir", {}).get("sizeLimit") != "10Gi" or
            mounts.get(short + "-data", {}).get("readOnly") is not True or
            mounts.get(short + "-data", {}).get("mountPath") != f"/data/{service}" or
            mounts.get("credentials", {}).get("readOnly") is not True or
            mounts.get("credentials", {}).get("mountPath") != f"/run/secrets/{short}-backup" or
            pod_security.get("runAsNonRoot") is not True or pod_security.get("runAsUser") != 10001 or
            pod_security.get("runAsGroup") != 10001 or
            pod_security.get("seccompProfile", {}).get("type") != "RuntimeDefault" or
            init_security.get("runAsUser") != 0 or init_security.get("readOnlyRootFilesystem") is not True or
            init_security.get("allowPrivilegeEscalation") is not False or
            backup_security.get("runAsNonRoot") is not True or backup_security.get("runAsUser") != 10001 or
            backup_security.get("readOnlyRootFilesystem") is not True or
            backup_security.get("allowPrivilegeEscalation") is not False):
        raise StateError(f"backup template drift: {name}")


def assert_single_writer():
    output = command("python3", str(ROOT / "scripts/runtime-service-state-reader.py"),
                     RUNTIME_MARKER, "--require-explicit")
    expected = {f"{service}=k3s" for _, service, _ in SCHEDULES.values()}
    if set(output.splitlines()) != expected:
        raise StateError("runtime marker is not exclusively K3s")
    docker_services = command("docker", "ps", "--format", '{{.Label "com.docker.compose.service"}}').splitlines()
    if any(service in {item[1] for item in SCHEDULES.values()} for service in docker_services):
        raise StateError("duplicate Docker writer is running")


def inspect(entries, now, *, enforce_window=False):
    assert_single_writer()
    jobs = get_json("personal-server", "jobs").get("items", [])
    if not isinstance(jobs, list):
        raise StateError("Job listing invalid")
    for job in jobs:
        name = job.get("metadata", {}).get("name", "")
        pod = job.get("spec", {}).get("template", {}).get("spec", {})
        backup_account = pod.get("serviceAccountName") in SCHEDULES
        backup_name = any(name.startswith(prefix + "-") for prefix in SCHEDULES)
        if (backup_account or backup_name) and job.get("status", {}).get("active", 0):
            raise StateError("backup Job is active")
    relay = get_json("monitoring", "deployment", "sre-telegram-relay")
    if relay.get("spec", {}).get("replicas") != 1 or relay.get("status", {}).get("availableReplicas") != 1:
        raise StateError("SRE relay is not ready")
    live = []
    for entry in entries:
        name = entry["name"]
        if enforce_window:
            assert_quiet_window(entry, now)
        cron = get_json("personal-server", "cronjob", name)
        spec = cron.get("spec", {})
        metadata = cron.get("metadata", {})
        if (metadata.get("name") != name or metadata.get("namespace") != "personal-server" or
                not metadata.get("uid") or not metadata.get("resourceVersion") or
                spec.get("schedule") != entry["schedule"] or spec.get("timeZone") != entry["timeZone"] or
                spec.get("concurrencyPolicy") != "Forbid" or cron.get("status", {}).get("active", []) or
                type(spec.get("suspend")) is not bool or type(spec.get("startingDeadlineSeconds")) is not int):
            raise StateError(f"CronJob invariant mismatch: {name}")
        pod = spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
        init = pod.get("initContainers", [])
        containers = pod.get("containers", [])
        if (len(init) != 1 or init[0].get("name") != "prepare-scratch" or
                len(containers) != 1 or containers[0].get("name") != "backup" or
                init[0].get("image") != entry["image"] or containers[0].get("image") != entry["image"] or
                init[0].get("imagePullPolicy") != "Never" or containers[0].get("imagePullPolicy") != "Never"):
            raise StateError(f"image drift: {name}")
        assert_template(name, spec, pod, init[0], containers[0])
        deployment = get_json("personal-server", "deployment", SCHEDULES[name][1])
        pvc = get_json("personal-server", "pvc", SCHEDULES[name][2])
        state = get_json("personal-server", "configmap", name + "-state").get("data", {})
        if (deployment.get("spec", {}).get("replicas") != 1 or deployment.get("status", {}).get("availableReplicas") != 1 or
                pvc.get("status", {}).get("phase") != "Bound" or state.get("status") != "completed" or
                state.get("lock_run_id") != "" or not state.get("evidence")):
            raise StateError(f"backup safety state mismatch: {name}")
        live.append(cron)
    return live


def changes(entries, live):
    return [entry["name"] for entry, cron in zip(entries, live) if
            cron["spec"]["startingDeadlineSeconds"] != entry["startingDeadlineSeconds"] or
            cron["spec"]["suspend"] != entry["suspend"]]


def assert_images_available(entries):
    available = set(command("sudo", "-n", "k3s", "ctr", "-n", "k8s.io", "images", "ls", "-q").splitlines())
    missing = [entry["name"] for entry in entries if entry["image"] not in available]
    if missing:
        raise StateError("approved image is not imported: " + ",".join(missing))


def patch_ops(entry, cron):
    spec = cron["spec"]
    pod = spec["jobTemplate"]["spec"]["template"]["spec"]
    ops = [
        {"op": "test", "path": "/metadata/uid", "value": cron["metadata"]["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": cron["metadata"]["resourceVersion"]},
    ]
    guarded = {
        "/spec/schedule": spec["schedule"], "/spec/timeZone": spec["timeZone"],
        "/spec/concurrencyPolicy": spec["concurrencyPolicy"],
        "/spec/startingDeadlineSeconds": spec["startingDeadlineSeconds"],
        "/spec/suspend": spec["suspend"],
        "/spec/jobTemplate/spec/template/spec/initContainers/0/image": pod["initContainers"][0]["image"],
        "/spec/jobTemplate/spec/template/spec/containers/0/image": pod["containers"][0]["image"],
    }
    ops.extend({"op": "test", "path": path, "value": value} for path, value in guarded.items())
    for field in ("startingDeadlineSeconds", "suspend"):
        if spec[field] != entry[field]:
            ops.append({"op": "replace", "path": "/spec/" + field, "value": entry[field]})
    return ops


def apply(entries, live, now):
    # Validate every object before the first write, then recheck stability per object.
    pending = changes(entries, live)
    for entry, cron in zip(entries, live):
        if entry["name"] not in pending:
            continue
        inspect(entries, datetime.now(ZoneInfo("Asia/Seoul")), enforce_window=True)
        patch = json.dumps(patch_ops(entry, cron), separators=(",", ":"))
        args = ("-n", "personal-server", "patch", "cronjob", entry["name"], "--type=json", "-p", patch)
        kubectl(*args, "--dry-run=server", "-o", "name")
        kubectl(*args, "-o", "name")
        print(f"applied={entry['name']}")
    latest = inspect(entries, now)
    if changes(entries, latest):
        raise StateError("post-apply drift remains")
    return len(pending)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--validate", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-sha")
    args = parser.parse_args()
    try:
        entries = validate_target()
        if args.validate:
            print("target=PASS")
            return 0
        if args.apply:
            if not args.expected_sha:
                raise StateError("--expected-sha required for apply")
            git_guard(args.expected_sha)
        now = datetime.now(ZoneInfo("Asia/Seoul"))
        live = inspect(entries, now, enforce_window=args.apply)
        assert_images_available(entries)
        pending = changes(entries, live)
        if args.check:
            if pending:
                raise StateError("CronJob drift: " + ",".join(pending))
            print("production_state=PASS")
            return 0
        writes = apply(entries, live, now)
        print(f"production_state=PASS writes={writes}")
        return 0
    except (StateError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"production_state=FAIL reason={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
