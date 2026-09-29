#!/usr/bin/env python3
"""Run the four existing PVC backup CronJob templates in a safe fixed order."""

import fcntl
import copy
import os
import hashlib
import json
import re
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
NAMESPACE = "personal-server"
STATE_NAME = "pvc-backup-sequence-state"
ORDER = ("portal", "book", "youtube", "crawler")
RESULTS = {"passed", "runner_failed", "failed_before_runner", "skipped"}


@dataclass(frozen=True)
class Stage:
    name: str
    command: str
    secret: str
    claims: tuple[str, ...]
    image: str
    status_name: str
    status_namespace: str = NAMESPACE


STAGES = {
    "portal": Stage("portal-pvc-backup", "/opt/personal-server/portal-pvc-backup-verify.sh", "portal-pvc-backup-runtime", ("portal-web-files-dynamic", "portal-web-state-dynamic"), "personal-server-portal-pvc-backup:task2", "sre-telegram-backup-status", "monitoring"),
    "book": Stage("book-pvc-backup", "/opt/personal-server/book-pvc-backup-verify.py", "book-memo-pvc-backup-runtime", ("book-memo-data",), "docker.io/library/personal-server-book-pvc-backup@sha256:dc46dfdc67145e649029c3fc3d0a76b2dd3480a94ac4d0198df748854ca0bc54", "book-pvc-backup-state"),
    "youtube": Stage("youtube-pvc-backup", "/opt/personal-server/youtube-pvc-backup-verify.py", "youtube-memo-pvc-backup-runtime", ("youtube-memo-data",), "docker.io/library/personal-server-youtube-pvc-backup@sha256:fb570a295d8a681d9d3990750cd74152bb30a9ce0701ccf34421f9a50c72a4b4", "youtube-pvc-backup-state"),
    "crawler": Stage("crawler-pvc-backup", "/opt/personal-server/crawler-pvc-backup-verify.py", "crawler-worker-pvc-backup-runtime", ("crawler-worker-data",), "docker.io/library/personal-server-crawler-pvc-backup@sha256:6e49af740101fe47601b48dc1a7da59a201f01e108b770e221bcb5a3b8a62846", "crawler-pvc-backup-state"),
}

SECRET_ITEMS = [
    {"key": "rclone-config", "path": "rclone-config"},
    {"key": "rclone-config-passphrase", "path": "rclone-config-passphrase"},
    {"key": "age-recipient", "path": "age-recipient"},
    {"key": "age-identity", "path": "age-identity"},
]
POD_SECURITY = {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                "seccompProfile": {"type": "RuntimeDefault"}}
BACKUP_SECURITY = {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                   "readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
                   "capabilities": {"drop": ["ALL"]}}
INIT_SECURITY = {"runAsNonRoot": False, "runAsUser": 0, "runAsGroup": 0,
                 "readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
                 "capabilities": {"drop": ["ALL"], "add": ["CHOWN", "FOWNER"]}}
PORTAL_ENV = [
    {"name": "PORTAL_BACKUP_EXECUTION_MODE", "value": "in-cluster"},
    {"name": "TMPDIR", "value": "/work"},
    {"name": "PORTAL_RCLONE_CONFIG_FILE", "value": "/run/secrets/portal-backup/rclone-config"},
    {"name": "PORTAL_RCLONE_PASSWORD_COMMAND", "value": "/usr/bin/cat /run/secrets/portal-backup/rclone-config-passphrase"},
]


class APIError(Exception):
    pass


class SafetyError(Exception):
    pass


def utc_time(value):
    if not isinstance(value, str) or not value:
        raise SafetyError("missing timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SafetyError("invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise SafetyError("timestamp without time zone")
    return parsed.astimezone(timezone.utc)


def activation_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise SafetyError("invalid activation date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SafetyError("invalid activation date") from exc


def canonical_hash(template_spec):
    raw = json.dumps(template_spec, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def validate_security_contract(stage, template, pod, main, image):
    config = STAGES[stage]
    if (template.get("ttlSecondsAfterFinished") != 86400
            or template.get("parallelism") not in (None, 1)
            or template.get("completions") not in (None, 1)
            or pod.get("terminationGracePeriodSeconds") != 300
            or pod.get("securityContext") != POD_SECURITY
            or any(pod.get(field) not in (None, False) for field in
                   ("hostNetwork", "hostPID", "hostIPC", "shareProcessNamespace"))
            or pod.get("nodeName") or pod.get("automountServiceAccountToken") is False
            or main.get("securityContext") != BACKUP_SECURITY
            or main.get("env", []) != (PORTAL_ENV if stage == "portal" else [])
            or main.get("envFrom") or main.get("args") or main.get("lifecycle") or main.get("volumeDevices")):
        raise SafetyError(f"{stage}: pod or backup security drift")
    init = pod.get("initContainers", [])
    expected_init_command = (["/bin/sh", "-ec", "chown 10001:10001 /work /tmp && chmod 0700 /work /tmp"]
                             if stage == "portal" else
                             ["/bin/sh", "-ec", "chown 10001:10001 /work && chmod 0700 /work"])
    if (len(init) != 1 or init[0].get("name") != ("prepare-writable-scratch" if stage == "portal" else "prepare-scratch")
            or init[0].get("image") != image or init[0].get("imagePullPolicy") != "Never"
            or init[0].get("command") != expected_init_command or init[0].get("args")
            or init[0].get("env") or init[0].get("envFrom")
            or init[0].get("securityContext") != INIT_SECURITY):
        raise SafetyError(f"{stage}: init container drift")
    source_names = ({"portal-files": ("portal-web-files-dynamic", "/data/files"),
                     "portal-state": ("portal-web-state-dynamic", "/data/portal-web-state")}
                    if stage == "portal" else
                    {f"{stage}-data": (config.claims[0], f"/data/{'crawler-worker' if stage == 'crawler' else stage + '-memo'}")})
    credential_name = "backup-runtime" if stage == "portal" else "credentials"
    credential_path = f"/run/secrets/{stage}-backup"
    scratch_names = {"work": ("/work", "10Gi")}
    if stage == "portal":
        scratch_names["tmp"] = ("/tmp", "1Gi")
    volumes = pod.get("volumes", [])
    by_name = {volume.get("name"): volume for volume in volumes}
    expected_names = set(source_names) | {credential_name} | set(scratch_names)
    if len(volumes) != len(expected_names) or set(by_name) != expected_names:
        raise SafetyError(f"{stage}: volume set drift")
    for name, (claim, _) in source_names.items():
        if by_name[name] != {"name": name, "persistentVolumeClaim": {"claimName": claim, "readOnly": True}}:
            raise SafetyError(f"{stage}: PVC source drift")
    secret = by_name[credential_name].get("secret", {})
    if (set(by_name[credential_name]) != {"name", "secret"}
            or set(secret) != {"secretName", "optional", "defaultMode", "items"}
            or secret["secretName"] != config.secret or secret["optional"] is not False
            or secret["defaultMode"] != 0o444 or secret["items"] != SECRET_ITEMS):
        raise SafetyError(f"{stage}: Secret projection drift")
    for name, (_, size) in scratch_names.items():
        if by_name[name] != {"name": name, "emptyDir": {"sizeLimit": size}}:
            raise SafetyError(f"{stage}: scratch volume drift")
    expected_mounts = {name: {"name": name, "mountPath": path, "readOnly": True}
                       for name, (_, path) in source_names.items()}
    expected_mounts[credential_name] = {"name": credential_name, "mountPath": credential_path, "readOnly": True}
    expected_mounts.update({name: {"name": name, "mountPath": path} for name, (path, _) in scratch_names.items()})
    mounts = main.get("volumeMounts", [])
    if len(mounts) != len(expected_mounts) or {m.get("name"): m for m in mounts} != expected_mounts:
        raise SafetyError(f"{stage}: backup mount drift")
    init_mounts = init[0].get("volumeMounts", [])
    expected_init_mounts = {name: {"name": name, "mountPath": path} for name, (path, _) in scratch_names.items()}
    if len(init_mounts) != len(expected_init_mounts) or {m.get("name"): m for m in init_mounts} != expected_init_mounts:
        raise SafetyError(f"{stage}: init mount drift")


def validate_cronjob(stage, cronjob):
    config = STAGES[stage]
    metadata = cronjob.get("metadata", {})
    spec = cronjob.get("spec", {})
    if (cronjob.get("kind") != "CronJob" or metadata.get("name") != config.name
            or metadata.get("namespace") != NAMESPACE or not metadata.get("uid")
            or spec.get("suspend") is not True or spec.get("timeZone") != "Asia/Seoul"
            or spec.get("concurrencyPolicy") != "Forbid"):
        raise SafetyError(f"{stage}: CronJob identity or schedule safety mismatch")
    template = spec.get("jobTemplate", {}).get("spec", {})
    pod = template.get("template", {}).get("spec", {})
    containers = pod.get("containers", [])
    if (template.get("backoffLimit") != 0 or not 0 < template.get("activeDeadlineSeconds", 0) <= 14400
            or pod.get("serviceAccountName") != config.name or pod.get("restartPolicy") != "Never"
            or len(containers) != 1):
        raise SafetyError(f"{stage}: Job execution template mismatch")
    main = containers[0]
    image = main.get("image")
    image_ok = image == config.image
    if (main.get("name") != "backup" or not image_ok or main.get("imagePullPolicy") != "Never"
            or main.get("command") != [config.command, "--go"]
            or main.get("securityContext", {}).get("allowPrivilegeEscalation") is not False):
        raise SafetyError(f"{stage}: backup container mismatch")
    validate_security_contract(stage, template, pod, main, image)
    return template, canonical_hash(template)


def make_job(stage, date, cronjob, template, digest):
    config = STAGES[stage]
    return {"apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": f"{config.name}-{date.strftime('%Y%m%d')}", "namespace": NAMESPACE,
                         "labels": {"personal-server/backup-stage": stage, "personal-server/backup-date": date.isoformat()},
                         "annotations": {"personal-server/backup-template-sha256": digest},
                         "ownerReferences": [{"apiVersion": "batch/v1", "kind": "CronJob", "name": config.name,
                                              "uid": cronjob["metadata"]["uid"], "controller": True}]},
            "spec": copy.deepcopy(template)}


def expected_fields_present(actual, expected):
    """Compare authored template fields while allowing Kubernetes API defaults."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and expected_fields_present(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) >= len(expected) and all(
            expected_fields_present(actual[i], item) for i, item in enumerate(expected))
    return actual == expected


def matching_job(existing, desired):
    if not existing:
        return False
    metadata = existing.get("metadata", {})
    wanted = desired["metadata"]
    owners = metadata.get("ownerReferences", [])
    return (existing.get("kind") == "Job" and metadata.get("name") == wanted["name"]
            and metadata.get("namespace") == NAMESPACE
            and metadata.get("labels", {}).get("personal-server/backup-stage") == wanted["labels"]["personal-server/backup-stage"]
            and metadata.get("labels", {}).get("personal-server/backup-date") == wanted["labels"]["personal-server/backup-date"]
            and metadata.get("annotations", {}).get("personal-server/backup-template-sha256") == wanted["annotations"]["personal-server/backup-template-sha256"]
            and any(o.get("uid") == wanted["ownerReferences"][0]["uid"] and o.get("name") == wanted["ownerReferences"][0]["name"] for o in owners)
            and len(existing.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])) == 1
            and expected_fields_present(existing.get("spec"), desired["spec"]))


def terminal(job):
    conditions = job.get("status", {}).get("conditions", [])
    for kind in ("Complete", "Failed"):
        if any(c.get("type") == kind and c.get("status") == "True" for c in conditions):
            return kind
    return None


def load_results(state, run_date):
    data = state.get("data", {})
    results = {stage: "skipped" for stage in ORDER}
    if data.get("run_date") != run_date:
        return results
    try:
        prior = json.loads(data.get("results", "{}"))
    except (TypeError, ValueError) as exc:
        raise SafetyError("invalid previous results") from exc
    if not isinstance(prior, dict) or any(k not in ORDER or v not in RESULTS for k, v in prior.items()):
        raise SafetyError("invalid previous results")
    results.update(prior)
    return results


def validate_state_identity(state):
    metadata = state.get("metadata", {})
    if (metadata.get("name") != STATE_NAME or metadata.get("namespace") != NAMESPACE
            or not metadata.get("resourceVersion")):
        raise SafetyError("sequence state ConfigMap is missing or invalid")


def backup_jobs_without_terminal(api):
    active = []
    accounts = {STAGES[stage].name for stage in ORDER}
    for job in api.list_jobs():
        name = job.get("metadata", {}).get("name", "")
        account = job.get("spec", {}).get("template", {}).get("spec", {}).get("serviceAccountName")
        if (any(name.startswith(prefix + "-") for prefix in accounts) or account in accounts) and terminal(job) is None:
            active.append(job)
    return active


def check(api):
    """Read-only preactivation check; active_from is intentionally optional."""
    try:
        validate_state_identity(api.get_state())
        for stage in ORDER:
            validate_cronjob(stage, api.get_cronjob(stage))
        if backup_jobs_without_terminal(api):
            raise SafetyError("active backup Job exists")
        return 0
    except (APIError, SafetyError, KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
        print(f"PVC backup sequence check blocked: {type(exc).__name__}", file=sys.stderr)
        return 1


def update_state(api, run_date, status, current, results, now):
    old = api.get_state()
    validate_state_identity(old)
    metadata = old["metadata"]
    data = dict(old.get("data") or {})
    data.update({"run_date": run_date, "status": status, "current": current,
                 "updated_at": now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                 "results": json.dumps(results, sort_keys=True, separators=(",", ":"))})
    api.patch_state(metadata["resourceVersion"], data)


def classify_failure(api, stage, job):
    try:
        data = api.get_backup_status(stage).get("data", {})
        if data.get("status") in {"failed", "restore_failed"} and utc_time(data.get("completed_at")) > utc_time(job["metadata"].get("creationTimestamp")):
            return "runner_failed"
    except (APIError, SafetyError, AttributeError, TypeError):
        pass
    return "failed_before_runner"


def run(api, now=lambda: datetime.now(KST), sleep=time.sleep, poll_seconds=30):
    local = now().astimezone(KST)
    run_date = local.date().isoformat()
    results = {stage: "skipped" for stage in ORDER}
    try:
        state = api.get_state()  # Must already exist; never create it here.
        validate_state_identity(state)
        if (local.hour, local.minute) < (0, 30):
            raise SafetyError("before 00:30 KST")
        active_from = activation_date(state.get("data", {}).get("active_from"))
        if local.date() < active_from:
            raise SafetyError("before activation date")
        results = load_results(state, run_date)
        cronjobs = {stage: api.get_cronjob(stage) for stage in ORDER}
        templates = {stage: validate_cronjob(stage, cronjobs[stage]) for stage in ORDER}
        active_jobs = backup_jobs_without_terminal(api)
        if active_jobs:
            current = state.get("data", {}).get("current") if state.get("data", {}).get("run_date") == run_date else None
            if len(active_jobs) != 1 or current not in ORDER or results[current] != "skipped":
                raise SafetyError("unexpected active backup Job")
            if any(results[prior] == "skipped" for prior in ORDER[:ORDER.index(current)]):
                raise SafetyError("active backup Job is out of order")
            template, digest = templates[current]
            desired = make_job(current, local.date(), cronjobs[current], template, digest)
            if not matching_job(active_jobs[0], desired):
                raise SafetyError("active backup Job identity or template mismatch")
        for stage in ORDER:
            if results[stage] != "skipped":
                continue
            config = STAGES[stage]
            template, digest = templates[stage]
            desired = make_job(stage, local.date(), cronjobs[stage], template, digest)
            job = api.get_job(desired["metadata"]["name"])
            if job is None and (now().astimezone(KST).date().isoformat() != run_date or now().astimezone(KST).hour >= 6):
                break
            update_state(api, run_date, "running", stage, results, now)
            if job is None:
                try:
                    api.create_job(desired)
                except APIError:
                    pass  # A lost response can still mean the Job was created.
                job = api.get_job(desired["metadata"]["name"])
            if not matching_job(job, desired):
                raise SafetyError(f"{stage}: Job identity or template hash mismatch")
            job_uid = job["metadata"].get("uid")
            created_at = job["metadata"].get("creationTimestamp")
            if not job_uid or not created_at:
                raise SafetyError(f"{stage}: Job identity incomplete")
            deadline = utc_time(created_at) + timedelta(seconds=template["activeDeadlineSeconds"] + 600)
            polls = 1 if poll_seconds <= 0 else (template["activeDeadlineSeconds"] + 600) // poll_seconds + 1
            for _ in range(polls):
                result = terminal(job)
                if result:
                    break
                if now().astimezone(timezone.utc) >= deadline:
                    raise SafetyError(f"{stage}: Job has not reached terminal state")
                sleep(poll_seconds)
                job = api.get_job(desired["metadata"]["name"])
                if (not matching_job(job, desired) or job["metadata"].get("uid") != job_uid
                        or job["metadata"].get("creationTimestamp") != created_at):
                    raise SafetyError(f"{stage}: Job identity changed")
            else:
                raise SafetyError(f"{stage}: Job has not reached terminal state")
            results[stage] = "passed" if result == "Complete" else classify_failure(api, stage, job)
            update_state(api, run_date, "running", "none", results, now)
        update_state(api, run_date, "completed", "none", results, now)
        return 0
    except (APIError, SafetyError, KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
        try:
            current = stage if "stage" in locals() else "none"
            update_state(api, run_date, "blocked", current, results, now)
        except (APIError, SafetyError, KeyError, TypeError, ValueError, AttributeError, IndexError):
            pass
        print(f"PVC backup sequence blocked: {type(exc).__name__}", file=sys.stderr)
        return 1


class KubectlAPI:
    def call(self, args, body=None, missing_ok=False):
        command = ["sudo", "-n", "k3s", "kubectl", "--request-timeout=20s", *args]
        try:
            response = subprocess.run(command, input=json.dumps(body) if body is not None else None,
                                      text=True, capture_output=True, check=False, timeout=25)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise APIError("kubectl request unavailable") from exc
        if response.returncode:
            if missing_ok and "NotFound" in response.stderr:
                return None
            raise APIError("kubectl request failed")
        if not response.stdout.strip():
            return None
        try:
            return json.loads(response.stdout)
        except ValueError as exc:
            raise APIError("kubectl returned invalid JSON") from exc

    def get_cronjob(self, stage):
        return self.call(["-n", NAMESPACE, "get", "cronjob", STAGES[stage].name, "-o", "json"])

    def list_jobs(self):
        return self.call(["-n", NAMESPACE, "get", "jobs", "-o", "json"])["items"]

    def get_job(self, name):
        return self.call(["-n", NAMESPACE, "get", "job", name, "-o", "json"], missing_ok=True)

    def create_job(self, job):
        return self.call(["-n", NAMESPACE, "create", "-f", "-", "-o", "json"], body=job)

    def get_state(self):
        return self.call(["-n", NAMESPACE, "get", "configmap", STATE_NAME, "-o", "json"])

    def patch_state(self, old_version, data):
        patch = [{"op": "test", "path": "/metadata/resourceVersion", "value": old_version},
                 {"op": "replace", "path": "/data", "value": data}]
        return self.call(["-n", NAMESPACE, "patch", "configmap", STATE_NAME, "--type=json", "-p", json.dumps(patch), "-o", "json"])

    def get_backup_status(self, stage):
        config = STAGES[stage]
        return self.call(["-n", config.status_namespace, "get", "configmap", config.status_name, "-o", "json"])


@contextmanager
def local_lock(home=None):
    """Hold a user-owned lock without following state-directory or file symlinks."""
    directory = os.open(home or Path.home(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock_fd = None
    try:
        owner = os.getuid()
        for component in (".local", "state", "personal-server"):
            parent = os.fstat(directory)
            if parent.st_uid != owner or not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o022:
                raise OSError("unsafe lock parent directory")
            try:
                os.mkdir(component, 0o700, dir_fd=directory)
            except FileExistsError:
                pass
            next_directory = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_directory
        folder = os.fstat(directory)
        if folder.st_uid != owner or stat.S_IMODE(folder.st_mode) != 0o700:
            raise OSError("unsafe backup lock directory")
        lock_fd = os.open("pvc-backup-sequence.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                          0o600, dir_fd=directory)
        lock = os.fstat(lock_fd)
        if lock.st_uid != owner or not stat.S_ISREG(lock.st_mode) or stat.S_IMODE(lock.st_mode) != 0o600 or lock.st_nlink != 1:
            raise OSError("unsafe backup lock file")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(directory)


def main():
    if sys.argv[1:] == ["--check"]:
        return check(KubectlAPI())
    if sys.argv[1:] == ["--check-lock"]:
        try:
            with local_lock():
                return 0
        except OSError:
            print("PVC backup sequence lock check blocked", file=sys.stderr)
            return 1
    if sys.argv[1:] != ["--go"]:
        print("usage: pvc-backup-sequence.py --check|--check-lock|--go", file=sys.stderr)
        return 2
    try:
        with local_lock():
            return run(KubectlAPI())
    except (OSError, BlockingIOError):
        print("PVC backup sequence blocked: local lock unavailable", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
