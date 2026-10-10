#!/usr/bin/env python3
"""One-lock, image-only rollout. --check is the default; no backup Jobs are run.

An execution plan is private, reviewed input, not an approval substitute. The
controller deliberately leaves imported images alone and never changes timers.
Unresolved recovery keeps the owner alive and the existing sequence lock held.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

APPS = ("portal", "book", "youtube", "crawler")
NS = "personal-server"
PIN_FILES = (
    "infra/k8s/backup-automation/portal-pvc-backup-cronjob.yaml",
    "infra/k8s/backup-automation/production-cronjob-state.json",
    "infra/k8s/tools/pvc-backup-sequence.py",
)
IMAGE_PATHS = ("/spec/jobTemplate/spec/template/spec/initContainers/0/image",
               "/spec/jobTemplate/spec/template/spec/containers/0/image")
KUBE = ["sudo", "-n", "k3s", "kubectl", "--request-timeout=15s"]
CTR = ["sudo", "-n", "k3s", "ctr", "--namespace", "k8s.io"]
SHA = re.compile(r"[0-9a-f]{64}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
NAME = re.compile(r"[a-z0-9][a-z0-9.-]*\Z")
INDEX_TYPES = {"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"}
MANIFEST_TYPES = {"application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"}
CONFIG_TYPES = {"application/vnd.oci.image.config.v1+json", "application/vnd.docker.container.image.v1+json"}
LAYER_TYPES = {"application/vnd.oci.image.layer.v1.tar", "application/vnd.oci.image.layer.v1.tar+gzip",
               "application/vnd.oci.image.layer.v1.tar+zstd", "application/vnd.docker.image.rootfs.diff.tar.gzip"}
RESOURCE_KINDS = {"deployment", "pod", "persistentvolumeclaim", "service",
                  "endpointslice", "networkpolicy", "configmap", "secret"}


class Blocked(Exception):
    """Messages are fixed reason codes; external command output is never exposed."""


def require(condition, code):
    if not condition:
        raise Blocked(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def hash_object(value):
    return digest(canonical(value))


def regular_hash(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "unsafe_file")
        hashed = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            hashed.update(chunk)
        return hashed.hexdigest()
    finally:
        os.close(fd)


def validate_plan(p):
    require(p.get("schema_version") == 1, "plan_schema")
    commits = [p.get(k) for k in ("c1", "c2", "c3")]
    require(all(isinstance(c, str) and COMMIT.fullmatch(c) and c != "0" * 40 for c in commits)
            and len(set(commits)) == 3 and p.get("origin_main") == p["c3"], "plan_commits")
    require(p.get("pinned_release_ancestor_ack") is True
            and p.get("image_rollout_scope_ack") is True, "plan_scope")
    for field in ("ci", "reviews"):
        records = p.get(field, [])
        require(isinstance(records, list) and len(records) == 2, "plan_proofs")
        require({r.get("commit") for r in records} == {p["c2"], p["c3"]}, "plan_proof_commits")
        for r in records:
            require(r.get("conclusion") == "success"
                    and re.fullmatch(r"https://github\.com/[^\s]+", r.get("url", "")), "plan_proof")
            if field == "ci":
                require(r.get("workflow") == "CI" and r.get("branch") == "main"
                        and r.get("event") == "push", "plan_main_ci")
    require(set(p.get("pin_files", {})) == set(PIN_FILES), "plan_pin_files")
    for hashes in p["pin_files"].values():
        require(set(hashes) == {"c1", "c2", "c3"}
                and all(isinstance(h, str) and SHA.fullmatch(h) for h in hashes.values())
                and hashes["c1"] == hashes["c3"], "plan_pin_hashes")
    for key in ("allowed_forward_paths", "allowed_inverse_paths"):
        paths = p.get(key)
        require(isinstance(paths, list) and paths and len(paths) == len(set(paths)), "plan_paths")
        require(all(isinstance(x, str) and not x.startswith("/") and ".." not in Path(x).parts
                    for x in paths) and set(PIN_FILES) <= set(paths), "plan_paths")
    require(all(x in PIN_FILES or x.startswith("docs/") for x in p["allowed_inverse_paths"]),
            "inverse_path_scope")
    images = p.get("images", [])
    require(isinstance(images, list) and [i.get("app") for i in images] == list(APPS), "plan_images")
    for i in images:
        require(i.get("cronjob") == i["app"] + "-pvc-backup"
                and isinstance(i.get("uid"), str) and i["uid"], "plan_cron_identity")
        require(all(SHA.fullmatch(i.get(k, "")) for k in
                    ("old_spec_sha256", "target_spec_sha256", "archive_sha256")), "plan_hash")
        require(Path(i.get("archive_path", "")).is_absolute(), "plan_archive")
        for side in ("old", "new"):
            image = i.get(side, {})
            prefix = "docker.io/library/personal-server-" + i["app"] + "-pvc-backup@"
            ref = image.get("reference", "")
            require(ref.startswith(prefix) and DIGEST.fullmatch(ref[len(prefix):])
                    and DIGEST.fullmatch(image.get("config_digest", "")), "plan_reference")
            graph = image.get("graph", {})
            require(isinstance(graph, dict) and 3 <= len(graph) <= 256
                    and ref[len(prefix):] in graph and image["config_digest"] in graph, "plan_graph")
            for key, desc in graph.items():
                require(DIGEST.fullmatch(key) and type(desc.get("size")) is int
                        and 0 < desc["size"] <= 1024 ** 3 and isinstance(desc.get("mediaType"), str),
                        "plan_descriptor")
        require(i["old"]["reference"] != i["new"]["reference"], "plan_same_image")
    for field in ("untracked", "protected_files"):
        require(isinstance(p.get(field), dict), "plan_file_map")
        for path, sha in p[field].items():
            require(isinstance(path, str) and SHA.fullmatch(sha), "plan_file_hash")
            require((Path(path).is_absolute() if field == "protected_files" else
                     not Path(path).is_absolute() and ".." not in Path(path).parts), "plan_file_path")
    resources = p.get("protected_resources")
    require(isinstance(resources, list) and resources, "plan_protected")
    identities = []
    for r in resources:
        require(r.get("kind") in RESOURCE_KINDS and NAME.fullmatch(r.get("namespace", ""))
                and NAME.fullmatch(r.get("name", "")) and SHA.fullmatch(r.get("sha256", "")),
                "plan_resource")
        identities.append((r["kind"], r["namespace"], r["name"]))
    require(len(identities) == len(set(identities)), "plan_resource_duplicate")
    required_cms = [("configmap", NS, "pvc-backup-sequence-state"),
                    ("configmap", "monitoring", "sre-telegram-backup-status")]
    required_cms += [("configmap", NS, a + "-pvc-backup-state") for a in APPS[1:]]
    require(all(x in identities for x in required_cms), "plan_backup_state_coverage")
    require("/var/lib/personal-server/k3s-runtime-services.state" in p["protected_files"],
            "plan_runtime_marker_coverage")
    require(RESOURCE_KINDS <= {r["kind"] for r in resources} | {"networkpolicy"}, "plan_resource_coverage")
    require(p.get("networkpolicy_inventory_sha256") and SHA.fullmatch(p["networkpolicy_inventory_sha256"]),
            "plan_policy_inventory")
    writers = p.get("writers", {})
    require(set(writers) == set(APPS) and all(NAME.fullmatch(v) for v in writers.values()), "plan_writers")
    require(all(("deployment", NS, v) in identities for v in writers.values()), "plan_writer_coverage")
    require(Path(p.get("journal_dir", "")).is_absolute()
            and re.fullmatch(r"[A-Za-z0-9_-]+\.jsonl", p.get("journal_name", "")), "plan_journal")
    budget = p.get("budget", {})
    require(budget.get("measured") is True and all(type(budget.get(k)) is int and 0 < budget[k] <= 3600
            for k in ("forward_seconds", "rollback_seconds", "margin_seconds")), "plan_budget")


def transform_spec(spec, reference):
    value = copy.deepcopy(spec)
    pod = value["jobTemplate"]["spec"]["template"]["spec"]
    for field in ("initContainers", "containers"):
        pod[field][0]["image"] = reference
    return value


def validate_cron(cron, image, side):
    metadata, spec = cron.get("metadata", {}), cron.get("spec", {})
    require(cron.get("kind") == "CronJob" and metadata.get("uid") == image["uid"]
            and metadata.get("name") == image["cronjob"] and metadata.get("namespace") == NS
            and isinstance(metadata.get("resourceVersion"), str) and metadata["resourceVersion"], "cron_identity")
    expected = image["old_spec_sha256" if side == "old" else "target_spec_sha256"]
    require(hash_object(spec) == expected, "cron_spec_drift")
    require(spec.get("suspend") is True, "cron_not_suspended")
    pod = spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
    require(pod.get("restartPolicy") == "Never", "cron_restart_policy")
    for field, name in (("initContainers", "prepare-writable-scratch" if image["app"] == "portal" else "prepare-scratch"),
                        ("containers", "backup")):
        values = pod.get(field, [])
        require(len(values) == 1 and values[0].get("name") == name
                and values[0].get("imagePullPolicy") == "Never"
                and values[0].get("image") == image[side]["reference"], "cron_container_contract")
    other = "new" if side == "old" else "old"
    other_hash = image["target_spec_sha256" if other == "new" else "old_spec_sha256"]
    require(hash_object(transform_spec(spec, image[other]["reference"])) == other_hash, "cron_exact_two_fields")


def image_patch(cron, image, old, new):
    validate_cron(cron, image, old)
    tests = [("/metadata/uid", image["uid"]),
             ("/metadata/resourceVersion", cron["metadata"]["resourceVersion"]),
             ("/spec/suspend", True)]
    for image_path in IMAGE_PATHS:
        tests += [(image_path.rsplit("/", 1)[0] + "/name",
                   "backup" if "/containers/" in image_path else
                   ("prepare-writable-scratch" if image["app"] == "portal" else "prepare-scratch")),
                  (image_path, image[old]["reference"])]
    return ([{"op": "test", "path": k, "value": v} for k, v in tests]
            + [{"op": "replace", "path": k, "value": image[new]["reference"]} for k in IMAGE_PATHS])


def validate_backup_status_lock(app, data):
    # Portal's relay ledger has no per-run lock key. Absence alone is not a
    # release proof: the owned sequence flock and terminal Job gate also apply.
    value = data.get("lock_run_id")
    require(value in (None, "") if app == "portal" else value == "", "backup_status_lock")


def resource_projection(kind, obj):
    """Secret payloads are never requested. ConfigMap values are hashed in memory."""
    meta = obj.get("metadata", {})
    require(meta.get("uid"), "protected_uid")
    value = {"uid": meta["uid"]}
    if kind == "secret":
        value.update(resourceVersion=meta.get("resourceVersion"), type=obj.get("type"))
    elif kind == "configmap":
        value["data_sha256"] = hash_object({"data": obj.get("data", {}), "binaryData": obj.get("binaryData", {})})
    elif kind == "endpointslice":
        value.update({k: obj.get(k) for k in ("addressType", "ports", "endpoints")})
    else:
        value["spec"] = obj.get("spec")
        if kind == "persistentvolumeclaim":
            require(obj.get("status", {}).get("phase") == "Bound", "pvc_not_bound")
        if kind == "pod":
            status = obj.get("status", {})
            require(status.get("phase") == "Running" and not meta.get("deletionTimestamp")
                    and any(c.get("type") == "Ready" and c.get("status") == "True"
                            for c in status.get("conditions", [])), "writer_not_ready")
            containers = status.get("containerStatuses", [])
            require(containers and all(c.get("ready") is True and c.get("restartCount") == 0
                    and not c.get("lastState", {}).get("terminated") for c in containers), "writer_restart")
    return value


class Journal:
    def __init__(self, directory, name):
        require(Path(directory).resolve() == Path(directory), "journal_parent_symlink")
        self.directory = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(self.directory)
        require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, "journal_directory")
        try:
            self.fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                              0o600, dir_fd=self.directory)
            os.fsync(self.directory)
        except BaseException:
            os.close(self.directory)
            raise

    def write(self, event, **fields):
        # No raw resource objects, credentials, endpoints or command output.
        raw = canonical({"at": datetime.now(timezone.utc).isoformat(), "event": event, **fields}) + b"\n"
        offset = 0
        while offset < len(raw):
            offset += os.write(self.fd, raw[offset:])
        os.fsync(self.fd)

    def close(self):
        os.close(self.fd)
        os.close(self.directory)


class NativeBackend:
    def __init__(self, root, plan):
        self.root = Path(root).resolve()
        self.plan = plan
        self.deadline = None
        self.command_termination_unknown = False
        spec = importlib.util.spec_from_file_location("rollout_sequence", self.root / PIN_FILES[2])
        self.sequence = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.sequence
        spec.loader.exec_module(self.sequence)
        self.local_lock = self.sequence.local_lock

    def phase_deadline(self, recovery=False):
        key = "rollback_seconds" if recovery else "forward_seconds"
        self.deadline = time.monotonic() + self.plan["budget"][key]

    def remaining(self, limit):
        deadline = getattr(self, "deadline", None)
        if deadline is None:
            return limit
        remaining = deadline - time.monotonic()
        require(remaining > 0, "phase_time_budget")
        return min(limit, remaining)

    def stop_child(self, child):
        """Terminate our entire child group before classifying an uncertain write."""
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            self.command_termination_unknown = True
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            self.command_termination_unknown = True
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.command_termination_unknown = True
        # sudo's exit does not prove a privileged descendant has exited. Only
        # ESRCH for the complete process group proves there are no late writers.
        try:
            os.killpg(child.pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError:
            self.command_termination_unknown = True
        else:
            self.command_termination_unknown = True

    def run(self, args, *, stdin=None, timeout=30):
        timeout = self.remaining(timeout)
        child = None
        try:
            child = subprocess.Popen(args, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     cwd=self.root, start_new_session=True)
            output, _ = child.communicate(input=stdin, timeout=timeout)
            require(child.returncode == 0, "command_failed")
            return output
        except (OSError, subprocess.TimeoutExpired):
            raise Blocked("command_unavailable") from None
        finally:
            if child is not None:
                if child.poll() is None:
                    self.stop_child(child)
                if child.stdout:
                    child.stdout.close()
                if child.stdin:
                    child.stdin.close()

    def git(self, *args):
        return self.run(["git", *args])

    def head(self):
        return self.git("rev-parse", "HEAD").decode().strip()

    def get(self, kind, namespace, name=None):
        args = KUBE + ["-n", namespace, "get", kind] + ([name] if name else [])
        if kind == "secret":
            # Go-template emits metadata/type only; no data/binaryData crosses the boundary.
            args += ["-o=custom-columns=UID:.metadata.uid,RV:.metadata.resourceVersion,TYPE:.type", "--no-headers"]
            fields = self.run(args).decode().strip().split()
            require(len(fields) == 3 and fields[0] != "<none>" and fields[1] != "<none>", "secret_metadata")
            return {"metadata": {"uid": fields[0], "resourceVersion": fields[1]}, "type": fields[2]}
        else:
            args += ["-o", "json"]
        try:
            return json.loads(self.run(args))
        except (ValueError, UnicodeError):
            raise Blocked("invalid_api_response") from None

    def cron(self, image):
        return self.get("cronjob", NS, image["cronjob"])

    def patch(self, image, patch):
        self.run(KUBE + ["-n", NS, "patch", "cronjob", image["cronjob"], "--type=json",
                         "--patch-file=/dev/stdin", "-o", "json"], stdin=canonical(patch))

    def source_guard(self, expected):
        require(not getattr(self, "command_termination_unknown", False), "child_termination_unknown")
        p = self.plan
        gitdir = Path(self.git("rev-parse", "--absolute-git-dir").decode().strip())
        require(not any((gitdir / name).exists() for name in
                        ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "index.lock")),
                "git_operation_active")
        # Inspect hooks/config before any status, fetch or merge command.
        raw = self.run(["git", "config", "--null", "--list"])
        entries = raw.decode().lower().split("\0")
        require(not any(e.startswith("core.hookspath\n") or
                        (e.startswith("core.fsmonitor\n") and e.split("\n", 1)[1] not in ("false", "0"))
                        for e in entries), "git_hooks_config")
        hooks = gitdir / "hooks"
        require(not hooks.is_symlink() and (not hooks.exists() or not any(
            x.is_symlink() or (x.is_file() and os.access(x, os.X_OK) and not x.name.endswith(".sample"))
            for x in hooks.iterdir())), "git_hooks_present")
        require(not any(e.startswith(("merge.autostash\ntrue", "rebase.autostash\ntrue",
                                      "maintenance.autorun\ntrue")) for e in entries), "git_auto_operation")
        require(self.head() == expected and self.git("branch", "--show-current").strip() == b"main", "source_head")
        require(not self.git("status", "--porcelain=v1", "--untracked-files=no"), "source_dirty")
        flags = self.git("ls-files", "-v", "-z").decode().split("\0")
        require(all(not x or (x[0].isupper() and x[0] != "S") for x in flags), "git_hidden_index_flags")
        self.git("diff", "--cached", "--quiet", expected)
        names = self.git("ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")
        actual = {n: regular_hash(self.root / n) for n in names if n}
        require(actual == p["untracked"], "source_untracked_drift")
        phase = {p["c1"]: "c1", p["c2"]: "c2", p["c3"]: "c3"}[expected]
        for path, hashes in p["pin_files"].items():
            require(digest(self.git("show", expected + ":" + path)) == hashes[phase]
                    and regular_hash(self.root / path) == hashes[phase], "source_pin_drift")

    def commits_guard(self):
        p = self.plan
        self.git("fetch", "--no-auto-maintenance", "--no-tags", "origin", "main")
        require(self.git("rev-parse", "origin/main").decode().strip() == p["c3"], "origin_main_drift")
        for old, new, paths in ((p["c1"], p["c2"], p["allowed_forward_paths"]),
                                (p["c2"], p["c3"], p["allowed_inverse_paths"])):
            self.git("merge-base", "--is-ancestor", old, new)
            actual = self.git("diff", "--name-only", "-z", old, new).decode().split("\0")
            require(set(x for x in actual if x) == set(paths), "source_changed_paths")
        for path, hashes in p["pin_files"].items():
            blobs = {k: self.git("show", p[k] + ":" + path) for k in ("c1", "c2", "c3")}
            require(all(digest(blobs[k]) == hashes[k] for k in blobs) and blobs["c1"] == blobs["c3"],
                    "source_blob_hash")
            changed = blobs["c1"]
            for image in p["images"]:
                old = image["old"]["reference"].split("@", 1)[1].encode()
                new = image["new"]["reference"].split("@", 1)[1].encode()
                count = (2 if image["app"] == "portal" else 0) if path == PIN_FILES[0] else (
                    (0 if image["app"] == "portal" else 1) if path == PIN_FILES[1] else 1)
                require(changed.count(old) == count, "source_pin_count")
                changed = changed.replace(old, new)
            require(changed == blobs["c2"], "source_non_pin_change")

    def ff(self, target):
        self.git("merge", "--ff-only", "--no-edit", target)

    def guards(self):
        require(not getattr(self, "command_termination_unknown", False), "child_termination_unknown")
        p = self.plan
        self.aliases_guard()
        for path, sha in p["protected_files"].items():
            require(regular_hash(path) == sha, "protected_file_drift")
        for record in p["protected_resources"]:
            obj = self.get(record["kind"], record["namespace"], record["name"])
            require(hash_object(resource_projection(record["kind"], obj)) == record["sha256"], "protected_resource_drift")
        policies = self.get("networkpolicy", NS)["items"]
        inventory = sorted(({"name": x["metadata"]["name"], **resource_projection("networkpolicy", x)}
                            for x in policies), key=lambda x: x["name"])
        require(hash_object(inventory) == p["networkpolicy_inventory_sha256"], "networkpolicy_inventory_drift")
        state = self.get("configmap", NS, "pvc-backup-sequence-state").get("data", {})
        require(state.get("status") == "completed" and state.get("current") == "none", "sequence_busy")
        for app in APPS:
            stage = self.sequence.STAGES[app]
            data = self.get("configmap", stage.status_namespace, stage.status_name).get("data", {})
            validate_backup_status_lock(app, data)
        for job in self.get("jobs", NS)["items"]:
            meta = job.get("metadata", {})
            account = job.get("spec", {}).get("template", {}).get("spec", {}).get("serviceAccountName", "")
            if (any(meta.get("name", "").startswith(a + "-pvc-backup-") for a in APPS)
                    or account in {a + "-pvc-backup" for a in APPS}):
                require(self.sequence.terminal(job) is not None, "backup_job_active")
        pods = self.get("pods", NS)["items"]
        for name in p["writers"].values():
            deployment = self.get("deployment", NS, name)
            require(deployment.get("spec", {}).get("replicas") == 1
                    and deployment.get("status", {}).get("readyReplicas") == 1, "writer_replica_count")
            labels = deployment["spec"]["selector"]["matchLabels"]
            selected = [pod for pod in pods if all(pod.get("metadata", {}).get("labels", {}).get(k) == v
                        for k, v in labels.items()) and pod.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
            require(len(selected) == 1, "single_writer_count")
            resource_projection("pod", selected[0])
            require(any(r["kind"] == "pod" and r["name"] == selected[0]["metadata"]["name"]
                        and r["namespace"] == NS for r in p["protected_resources"]), "writer_pod_unprotected")
        marker = "/var/lib/personal-server/k3s-runtime-services.state"
        runtime = self.run([sys.executable, str(self.root / "scripts/runtime-service-state-reader.py"),
                            marker, "--require-explicit"]).decode().splitlines()
        require(set(runtime) == {name + "=k3s" for name in ("crawler-worker", "youtube-memo", "book-memo")},
                "runtime_marker_owner")
        compose = self.run(["docker", "ps", "--format", '{{.Label "com.docker.compose.service"}}']).decode().splitlines()
        require(not set(compose) & (set(p["writers"].values()) | {"portal", "portal-web", "crawler-worker",
                    "youtube-memo", "book-memo"}), "compose_writer_active")

    def aliases_guard(self):
        aliases = self.run(CTR + ["images", "list"]).decode().splitlines()
        for image in self.plan["images"]:
            for side in ("old", "new"):
                record = image[side]
                target = record["reference"].split("@", 1)[1]
                rows = [line.split() for line in aliases if line.split()[:1] == [record["reference"]]]
                require(len(rows) == 1 and len(rows[0]) >= 3 and rows[0][2] == target
                        and rows[0][1] == record["graph"][target]["mediaType"]
                        and rows[0][1] in INDEX_TYPES | MANIFEST_TYPES, "alias_target")
        return aliases

    def images_guard(self):
        aliases = self.aliases_guard()
        for image in self.plan["images"]:
            require(regular_hash(image["archive_path"]) == image["archive_sha256"], "archive_hash")
            for side in ("old", "new"):
                record = image[side]
                rows = [line.split() for line in aliases if line.split()[:1] == [record["reference"]]]
                target = record["reference"].split("@", 1)[1]
                require(len(rows) == 1 and len(rows[0]) >= 3 and rows[0][2] == target, "alias_target")
                documents = {}
                for blob, desc in record["graph"].items():
                    # Bounded streaming hashing avoids retaining backup layers in memory.
                    raw = self.read_blob(blob, desc["size"], desc["mediaType"].endswith("json"))
                    if raw is not None:
                        try:
                            documents[blob] = json.loads(raw)
                        except (ValueError, UnicodeError):
                            raise Blocked("image_json") from None
                reachable = set()
                configs = []
                def visit(blob, depth=0):
                    require(depth <= 3 and blob in record["graph"], "image_graph_depth")
                    if blob in reachable:
                        return
                    reachable.add(blob)
                    doc = documents.get(blob)
                    require(isinstance(doc, dict), "image_manifest_json")
                    media = record["graph"][blob]["mediaType"]
                    require(media in INDEX_TYPES | MANIFEST_TYPES and doc.get("mediaType", media) == media, "image_media")
                    require(doc.get("schemaVersion") == 2 and not doc.get("subject") and not doc.get("artifactType"),
                            "image_manifest_schema")
                    if media in INDEX_TYPES:
                        require(isinstance(doc["manifests"], list) and 1 <= len(doc["manifests"]) <= 32, "image_index")
                        for child in doc["manifests"]:
                            descriptor(child)
                            platform = child.get("platform")
                            require(platform is None or (platform.get("os") == "linux"
                                    and platform.get("architecture") == "amd64"), "image_index_platform")
                            visit(child["digest"], depth + 1)
                    else:
                        config = doc.get("config", {})
                        descriptor(config)
                        require(config.get("mediaType") in CONFIG_TYPES, "image_config_media")
                        reachable.add(config["digest"])
                        cfg = documents.get(config["digest"], {})
                        require(cfg.get("os") == "linux" and cfg.get("architecture") == "amd64", "image_platform")
                        configs.append(config["digest"])
                        layers = doc.get("layers")
                        require(isinstance(layers, list) and 0 < len(layers) <= 128, "image_layers")
                        require(cfg.get("rootfs", {}).get("type") == "layers"
                                and len(cfg.get("rootfs", {}).get("diff_ids", [])) == len(layers)
                                and all(isinstance(x, str) and DIGEST.fullmatch(x) for x in cfg["rootfs"]["diff_ids"]), "image_rootfs")
                        for layer in layers:
                            descriptor(layer)
                            require(layer.get("mediaType") in LAYER_TYPES, "image_layer_media")
                            reachable.add(layer["digest"])
                def descriptor(desc):
                    require(isinstance(desc, dict) and desc.get("digest") in record["graph"], "image_graph_missing")
                    expected = record["graph"][desc["digest"]]
                    require(desc.get("size") == expected["size"] and desc.get("mediaType") == expected["mediaType"],
                            "image_descriptor_drift")
                visit(target)
                require(reachable == set(record["graph"]) and configs == [record["config_digest"]], "image_exact_graph")
        refreshed = self.aliases_guard()
        # Only the eight required aliases are protected against list reordering.
        refs = {i[s]["reference"] for i in self.plan["images"] for s in ("old", "new")}
        require(sorted(x for x in aliases if x.split()[:1] and x.split()[0] in refs)
                == sorted(x for x in refreshed if x.split()[:1] and x.split()[0] in refs), "alias_changed")

    def read_blob(self, blob, size, retain):
        require(not retain or size <= 4 * 1024 ** 2, "image_json_size")
        child = None
        started = time.monotonic()
        max_seconds = self.remaining(60)
        total, hashed = 0, hashlib.sha256()
        raw = bytearray() if retain else None
        try:
            child = subprocess.Popen(CTR + ["content", "get", blob], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, cwd=self.root, start_new_session=True)
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while True:
                    require(time.monotonic() - started < max_seconds, "image_content_timeout")
                    if not selector.select(0.2):
                        continue
                    chunk = os.read(child.stdout.fileno(), 1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    require(total <= size, "image_content_size")
                    hashed.update(chunk)
                    if retain:
                        raw.extend(chunk)
            require(child.wait(timeout=2) == 0 and total == size, "image_content_size")
            require("sha256:" + hashed.hexdigest() == blob, "image_content_digest")
            return bytes(raw) if retain else None
        except (OSError, subprocess.TimeoutExpired):
            raise Blocked("image_content_unavailable") from None
        finally:
            if child is not None:
                if child.stdout:
                    child.stdout.close()
                if child.poll() is None:
                    self.stop_child(child)

    def final_check(self):
        # A fresh process imports the exact active source pins after FF.
        self.run([sys.executable, str(self.root / PIN_FILES[2]), "--check"])
        self.run([sys.executable, str(self.root / "infra/k8s/tools/service-pvc-backup-production-state.py"), "--check"])


class Controller:
    def __init__(self, backend, plan, journal_factory=Journal, sleep=time.sleep, emit=None):
        self.backend, self.plan = backend, plan
        self.journal_factory, self.sleep = journal_factory, sleep
        self.emit = emit or self.safe_emit
        self.token = None
        self.journal = None
        self.attempted = []
        self.mutation_attempted = False

    @staticmethod
    def safe_emit(status):
        try:
            print(json.dumps({"status": status}, sort_keys=True), flush=True)
        except (BrokenPipeError, OSError):
            pass  # Losing the observer never terminates a transaction owner.

    def owned(self, token):
        require(token is not None and token is self.token, "owned_lock_required")

    def budget(self, recovery=False):
        now = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Seoul"))
        today_run = now.replace(hour=2, minute=30, second=0, microsecond=0)
        require(not today_run <= now < now.replace(hour=6, minute=0, second=0, microsecond=0),
                "scheduled_sequence_window")
        next_run = today_run
        if next_run <= now:
            next_run += timedelta(days=1)
        b = self.plan["budget"]
        seconds = b["rollback_seconds"] + b["margin_seconds"]
        if not recovery:
            seconds += b["forward_seconds"]
        require((next_run - now).total_seconds() > seconds, "timer_budget")

    def ff(self, target, token):
        self.owned(token)
        p, b = self.plan, self.backend
        require(target in (p["c2"], p["c3"]), "source_target")
        old = p["c1"] if target == p["c2"] else p["c2"]
        b.source_guard(old)
        b.guards()
        self.journal.write("source_intent", source=old, target=target)
        self.mutation_attempted = True
        try:
            b.ff(target)
        except Exception:
            # A failed response does not imply the merge failed. Never reissue.
            actual = b.head()
            require(actual in (old, target), "source_ambiguous")
            b.source_guard(actual)
            if actual != target:
                raise Blocked("source_not_advanced") from None
        b.source_guard(target)
        self.journal.write("source_verified", target=target)

    def change(self, image, old, new):
        b = self.backend
        self.owned(self.token)
        b.source_guard(b.head())
        b.guards()
        cron = b.cron(image)
        patch = image_patch(cron, image, old, new)
        self.journal.write("cron_intent", app=image["app"], old=old, new=new,
                           uid=image["uid"], rv=cron["metadata"]["resourceVersion"],
                           old_spec_sha256=hash_object(cron["spec"]), patch_sha256=hash_object(patch))
        if old == "old":
            self.attempted.append(image)
        self.mutation_attempted = True
        try:
            b.patch(image, patch)
        except Exception:
            actual = b.cron(image)
            side = self.classify(actual, image)
            require(side == new, "cron_not_changed")
        actual = b.cron(image)
        validate_cron(actual, image, new)
        b.guards()
        self.journal.write("cron_verified", app=image["app"], side=new)

    @staticmethod
    def classify(cron, image):
        for side in ("old", "new"):
            try:
                validate_cron(cron, image, side)
                return side
            except Blocked:
                pass
        raise Blocked("cron_unknown_state")

    def verify_consistent(self, source, side):
        b = self.backend
        b.source_guard(source)
        b.guards()
        for image in self.plan["images"]:
            validate_cron(b.cron(image), image, side)
        b.final_check()
        b.source_guard(source)
        b.guards()

    def recover(self):
        b, p = self.backend, self.plan
        self.owned(self.token)
        self.budget(recovery=True)
        b.phase_deadline(recovery=True)
        head = b.head()
        require(head in (p["c1"], p["c2"], p["c3"]), "recovery_source_unknown")
        b.source_guard(head)
        b.guards()
        b.images_guard()
        # Classify all four before inverse mutation, including untouched records.
        states = {i["app"]: self.classify(b.cron(i), i) for i in p["images"]}
        owned = {i["app"] for i in self.attempted}
        require(all(side == "old" or app in owned for app, side in states.items()), "unowned_cron_change")
        for image in reversed(self.attempted):
            if states[image["app"]] == "new":
                self.change(image, "new", "old")
        if head == p["c2"]:
            self.ff(p["c3"], self.token)
        source = p["c3"] if head in (p["c2"], p["c3"]) else p["c1"]
        self.verify_consistent(source, "old")
        self.journal.write("rolled_back", source=source)

    def held_recovery(self):
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)
        # There are deliberately no writes, patches, merges or repeated recovery
        # attempts here. An operator must inspect and resolve the recorded drift.
        while True:
            self.emit("HELD_RECOVERY")
            self.sleep(30)

    def execute(self, apply=False):
        validate_plan(self.plan)
        b, p = self.backend, self.plan
        with b.local_lock():  # The sole acquisition, including forward/recovery.
            self.token = object()
            try:
                self.budget()
                b.phase_deadline()
                b.source_guard(p["c1"])
                b.commits_guard()
                b.source_guard(p["c1"])
                b.guards()
                b.images_guard()
                self.budget()
                for image in p["images"]:
                    validate_cron(b.cron(image), image, "old")
                b.final_check()
                b.source_guard(p["c1"])
                b.guards()
                if not apply:
                    self.emit("CHECK_PASSED_NO_MUTATION")
                    return "CHECK_PASSED_NO_MUTATION"
                self.journal = self.journal_factory(p["journal_dir"], p["journal_name"])
                self.journal.write("prepared", plan_sha256=hash_object(p), c1=p["c1"], c2=p["c2"], c3=p["c3"])
                try:
                    for image in p["images"]:
                        self.budget()
                        self.change(image, "old", "new")
                    self.ff(p["c2"], self.token)
                    self.verify_consistent(p["c2"], "new")
                    self.journal.write("forward_complete", source=p["c2"], origin_main=p["c3"])
                    self.emit("FORWARD_COMPLETE_PINNED_RELEASE_ANCESTOR")
                    return "FORWARD_COMPLETE_PINNED_RELEASE_ANCESTOR"
                except BaseException:
                    if not self.mutation_attempted:
                        raise
                    try:
                        self.recover()
                    except BaseException:
                        try:
                            self.journal.write("held_recovery")
                        except BaseException:
                            pass
                        self.held_recovery()
                        raise AssertionError("held recovery loop returned")
                    self.emit("ROLLED_BACK")
                    return "ROLLED_BACK"
            finally:
                self.token = None
                if self.journal is not None:
                    self.journal.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--root", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        fd = os.open(args.plan, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as handle:
            info = os.fstat(handle.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1, "unsafe_plan_file")
            plan = json.load(handle)
        validate_plan(plan)
        backend = NativeBackend(args.root, plan)
        if args.apply:
            require(Path(__file__).resolve().parent == Path(plan["journal_dir"]).resolve(), "private_controller_required")
            require(regular_hash(__file__) == digest(backend.git("show", plan["c2"] +
                    ":infra/k8s/tools/backup-image-rollout.py")), "controller_release_bytes")
        controller = Controller(backend, plan)
        # Interruption cannot release the lock mid-transaction. The first signal
        # initiates classified recovery; signals in recovery/held state are ignored.
        def interrupted(signum, frame):
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            raise Blocked("interrupted")
        signal.signal(signal.SIGINT, interrupted)
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGHUP, interrupted)
        status = controller.execute(args.apply)
        return 0 if status != "ROLLED_BACK" else 1
    except (Blocked, OSError, ValueError, KeyError, TypeError):
        Controller.safe_emit("BLOCKED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
