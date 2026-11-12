"""Capture the five fenced K3s PVCs locally using disposable read-only pods."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shutil
import subprocess
import tarfile
import tempfile
import time

from .controller import TransitionBlocked
from .legacy_writers import K3S_SERVICES, KUBE, NAME, UID, _match, _need

NAMESPACE = "personal-server"
POD_PREFIX = "ha-frozen-reader-"
CLAIMS = {"portal-web": ("portal-web-files-dynamic", "portal-web-state-dynamic"),
          "book-memo": ("book-memo-data",), "youtube-memo": ("youtube-memo-data",),
          "crawler-worker": ("crawler-worker-data",)}
APP_PATHS = {
    "portal-web": {
        "FILE_STORAGE_PATH": ("/data/files", False),
        "HOMEOPS_DB_PATH": ("/var/lib/portal/homeops.sqlite3", False),
        "NEWS_BOOKMARK_DB_PATH": ("/var/lib/portal/news-bookmarks.sqlite3", True),
        "AUTH_SESSION_STATE_PATH": ("/var/lib/portal/auth/session-state.sqlite3", False),
        "AUTH_RATE_LIMIT_STATE_PATH": ("/var/lib/portal/auth-rate-limit-state.json", False),
        "PORTFOLIO_CONTENT_PATH": ("/data/files/.portfolio/portfolio.md", True),
    },
    "book-memo": {"BOOK_MEMO_DB_PATH": ("/data/book-memo/book_memo.sqlite3", False),
                  "AUTH_RATE_LIMIT_STATE_PATH": ("/data/book-memo/auth-rate-limit-state.json", True)},
    "youtube-memo": {"YOUTUBE_MEMO_DB_PATH": ("/data/youtube-memo/youtube_memo.sqlite3", False),
                     "AUTH_RATE_LIMIT_STATE_PATH": ("/data/youtube-memo/auth-rate-limit-state.json", True)},
    "crawler-worker": {"NEWS_DB_PATH": ("/data/crawler-worker/news_summaries.sqlite3", False),
                       "NEWS_ARCHIVE_PATH": ("/data/crawler-worker/news_archive.json", False),
                       "NEWS_COLLECTION_STATUS_PATH": ("/data/crawler-worker/news_collection_status.json", True)},
}
MAX_ARCHIVE = 2 * 1024 * 1024 * 1024
MAX_MEMBERS = 50000
IMAGE_PIN = re.compile(r"[a-zA-Z0-9._:/-]+@sha256:[a-f0-9]{64}\Z")
READER = "import time; time.sleep(900)"
DIGEST = re.compile(r"[a-f0-9]{64}\Z")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _run_archive_command(argv, *, body=None, output=None, timeout=20):
    environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C", "HOME": os.path.expanduser("~")}
    try:
        if output is None:
            return subprocess.run(argv, input=body, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  check=True, env=environment, timeout=timeout).stdout
        _need(body is None, "pvc_capture_command_unconfirmed")
        deadline = time.monotonic() + timeout
        size = 0
        with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, env=environment) as child:
            try:
                with selectors.DefaultSelector() as reader:
                    fd = child.stdout.fileno()
                    reader.register(fd, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        _need(remaining > 0 and reader.select(remaining), "pvc_capture_command_unconfirmed")
                        chunk = os.read(fd, 65536)
                        if not chunk:
                            break
                        size += len(chunk)
                        _need(size <= MAX_ARCHIVE, "pvc_capture_archive_too_large")
                        output.write(chunk)
                _need(child.wait(timeout=max(0, deadline - time.monotonic())) == 0,
                      "pvc_capture_command_unconfirmed")
            finally:
                if child.poll() is None:
                    child.kill()
    except (OSError, subprocess.SubprocessError):
        raise TransitionBlocked("pvc_capture_command_unconfirmed") from None


def _command(args, *, body=None, output=None, timeout=20):
    return _run_archive_command((*KUBE, *args), body=body, output=output, timeout=timeout)


def _private_root(path):
    return (path.is_absolute() and path.resolve(strict=True) == path and path.is_dir()
            and path.stat().st_uid == os.geteuid() and not path.stat().st_mode & 0o077)


def _extract_selected(archive, destination, roots):
    """Extract only regular files/directories beneath the fixed capture roots."""
    seen = set()
    size = 0
    with tarfile.open(fileobj=archive, mode="r:") as entries:
        for entry in entries:
            path = PurePosixPath(entry.name)
            _need(not path.is_absolute() and all(part not in (".", "..") for part in path.parts)
                  and bool(path.parts) and path.parts[0] in roots
                  and path not in seen and len(seen) < MAX_MEMBERS
                  and (entry.isdir() or (entry.isreg() and not entry.issparse())),
                  "pvc_capture_archive_member_invalid")
            seen.add(path)
            target = destination.joinpath(*path.parts)
            if entry.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            size += entry.size
            _need(size <= MAX_ARCHIVE, "pvc_capture_archive_too_large")
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with entries.extractfile(entry) as source, os.fdopen(os.open(
                    target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as sink:
                shutil.copyfileobj(source, sink)
    _need(all(PurePosixPath(root) in seen for root in roots), "pvc_capture_mount_missing")


def _extract(archive, destination, mount_count):
    roots = {f"v{i}" for i in range(mount_count)}
    _extract_selected(archive, destination, roots)
    _need(all((destination / root).is_dir() for root in roots), "pvc_capture_mount_missing")


class PvcCapture:
    """Pin each service's deployment, local storage and reader image before use."""

    def __init__(self, bindings, attempt, *, run=_command):
        _need(set(bindings) == K3S_SERVICES and re.fullmatch(r"[a-f0-9]{64}", attempt) is not None,
              "pvc_capture_scope_invalid")
        self.bindings = deepcopy(bindings)
        self.attempt = attempt
        self.run = run
        self.cleanup_unconfirmed = False
        for service, row in bindings.items():
            _need(set(row) == {"deployment_uid", "template_sha256", "node", "image", "image_id", "claims"}
                  and _match(UID, row["deployment_uid"]) and _match(NAME, row["node"])
                  and _match(DIGEST, row["template_sha256"])
                  and _match(IMAGE_PIN, row["image"])
                  and _match(re.compile(r"(?:[a-zA-Z0-9._:/-]+@)?sha256:[a-f0-9]{64}\Z"), row["image_id"])
                  and tuple(claim["name"] for claim in row["claims"]) == CLAIMS[service],
                  "pvc_capture_scope_invalid")
            for claim in row["claims"]:
                _need(set(claim) == {"name", "uid", "pv", "pv_uid", "pv_spec_sha256"}
                      and _match(NAME, claim["name"]) and _match(NAME, claim["pv"])
                      and _match(UID, claim["uid"]) and _match(UID, claim["pv_uid"])
                      and _match(DIGEST, claim["pv_spec_sha256"]),
                      "pvc_capture_scope_invalid")

    def _read(self, kind, name, *, namespaced=True):
        scope = ("-n", NAMESPACE) if namespaced else ()
        return json.loads(self.run((*scope, "get", kind, name, "-o", "json")))

    def _assert_sources(self, service):
        row = self.bindings[service]
        deploy = self._read("deployment", service)
        spec = deploy["spec"]
        _need(deploy["metadata"]["uid"] == row["deployment_uid"] and spec.get("replicas") == 0
              and deploy.get("status", {}).get("replicas", 0) == 0
              and _digest(spec["template"]) == row["template_sha256"],
              "pvc_capture_deployment_not_closed")
        claims = {volume["persistentVolumeClaim"]["claimName"]
                  for volume in spec["template"]["spec"].get("volumes", ()) if "persistentVolumeClaim" in volume}
        _need(claims == {x["name"] for x in row["claims"]}, "pvc_capture_claim_scope_changed")
        for bound in row["claims"]:
            pvc = self._read("pvc", bound["name"])
            pv = self._read("pv", bound["pv"], namespaced=False)
            ref = pv["spec"]["claimRef"]
            _need(pv["spec"].get("local") and pv["spec"].get("nodeAffinity", {}).get("required"),
                  "pvc_capture_volume_node_binding_missing")
            spec_digest = _digest(pv["spec"])
            _need(pvc["metadata"]["uid"] == bound["uid"] and pvc["status"]["phase"] == "Bound"
                  and pvc["spec"]["volumeName"] == bound["pv"] and pv["metadata"]["uid"] == bound["pv_uid"]
                  and ref["uid"] == bound["uid"] and ref["namespace"] == NAMESPACE
                  and ref["name"] == bound["name"] and pv["status"]["phase"] == "Bound"
                  and spec_digest == bound["pv_spec_sha256"], "pvc_capture_claim_identity_changed")

    def _pod(self, service):
        row = self.bindings[service]
        mounts = [{"name": f"v{i}", "mountPath": f"/capture/v{i}", "readOnly": True}
                  for i in range(len(row["claims"]))]
        volumes = [{"name": mount["name"], "persistentVolumeClaim": {"claimName": claim["name"], "readOnly": True}}
                   for mount, claim in zip(mounts, row["claims"])]
        return {"apiVersion": "v1", "kind": "Pod",
                "metadata": {"name": POD_PREFIX + self.attempt[:12] + "-" + service, "namespace": NAMESPACE,
                             "labels": {"ha-frozen-capture": self.attempt[:32]}},
                "spec": {"affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
                             "nodeSelectorTerms": [{"matchFields": [{"key": "metadata.name", "operator": "In",
                                                                    "values": [row["node"]]}]}]}}},
                         "restartPolicy": "Never", "automountServiceAccountToken": False,
                         "enableServiceLinks": False, "activeDeadlineSeconds": 900,
                         "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}}, "volumes": volumes,
                         "containers": [{"name": "reader", "image": row["image"], "imagePullPolicy": "Never",
                                         "command": ["python3", "-c", READER], "volumeMounts": mounts,
                                         "resources": {"limits": {"cpu": "200m", "memory": "96Mi"}},
                                         "securityContext": {"allowPrivilegeEscalation": False,
                                                             "readOnlyRootFilesystem": True,
                                                             "capabilities": {"drop": ["ALL"]}}}]}}

    def _verify_pod(self, desired, current, uid, *, node=None):
        spec = current["spec"]
        wanted = desired["spec"]
        meta = current["metadata"]
        _need(meta.get("uid") == uid and meta.get("name") == desired["metadata"]["name"]
              and meta.get("namespace") == NAMESPACE and meta["labels"] == desired["metadata"]["labels"]
              and spec.get("nodeName") in ((None, "") if node is None else (node,))
              and spec.get("schedulerName", "default-scheduler") == "default-scheduler"
              and not meta.get("ownerReferences") and not meta.get("deletionTimestamp")
              and not any(spec.get(key) for key in ("hostPID", "hostIPC", "hostNetwork", "initContainers", "ephemeralContainers"))
              and all(spec[key] == wanted[key] for key in ("affinity", "restartPolicy", "automountServiceAccountToken", "enableServiceLinks",
                                                           "activeDeadlineSeconds", "securityContext", "volumes"))
              and len(spec["containers"]) == 1, "pvc_capture_reader_drift")
        reader, = spec["containers"]
        _need(all(reader[key] == wanted["containers"][0][key] for key in
                  ("name", "image", "imagePullPolicy", "command", "volumeMounts", "securityContext"))
              and not any(reader.get(key) for key in ("env", "envFrom", "args", "lifecycle")),
              "pvc_capture_reader_drift")

    def _verify_running_reader(self, service, desired, name, uid):
        current = self._read("pod", name)
        self._verify_pod(desired, current, uid, node=self.bindings[service]["node"])
        status = current["status"]
        rows = status.get("containerStatuses", [])
        _need(current["spec"].get("nodeName") == self.bindings[service]["node"]
              and status["phase"] == "Running" and len(rows) == 1 and rows[0]["ready"] is True
              and rows[0]["imageID"].removeprefix("docker-pullable://") == self.bindings[service]["image_id"],
              "pvc_capture_reader_image_unconfirmed")

    def _assert_runtime_mounts(self, name, count):
        code = (
            "import pathlib,sys; "
            "rows=[line.split() for line in pathlib.Path('/proc/self/mountinfo').read_text().splitlines()]; "
            "sys.exit(any(sum(row[4]==path and 'ro' in row[5].split(',') for row in rows)!=1 "
            "or any(row[4].startswith(path+'/') for row in rows) for path in sys.argv[1:]))"
        )
        self._reader_command("mount_exec", ("-n", NAMESPACE, "exec", name, "-c", "reader", "--", "python3", "-c", code,
                                            *(f"/capture/v{i}" for i in range(count))))

    def _reader_command(self, stage, args, **options):
        try:
            return self.run(args, **options)
        except TransitionBlocked as error:
            if str(error) != "pvc_capture_command_unconfirmed":
                raise
            raise TransitionBlocked("pvc_capture_reader_" + stage) from None

    def _remove_reader(self, name, uid):
        opts = json.dumps({"apiVersion": "v1", "kind": "DeleteOptions",
                           "preconditions": {"uid": uid}}).encode()
        try:
            self.run(("delete", "--raw", f"/api/v1/namespaces/{NAMESPACE}/pods/{name}", "-f", "-"), body=opts)
        except TransitionBlocked:
            # The API may have accepted the UID-bound request. Observe without sending it again.
            pass
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            remaining = self._reader_command("delete_observation",
                                             ("-n", NAMESPACE, "get", "pod", name, "--ignore-not-found", "-o", "json"),
                                             timeout=max(0.1, min(5, deadline - time.monotonic())))
            if not remaining.strip():
                return
            try:
                found = json.loads(remaining)
                _need(found["metadata"]["uid"] == uid and found["metadata"]["deletionTimestamp"],
                      "pvc_capture_cleanup_reconciliation_required")
            except (ValueError, TypeError, KeyError):
                raise TransitionBlocked("pvc_capture_cleanup_reconciliation_required") from None
            time.sleep(0.25)
        raise TransitionBlocked("pvc_capture_cleanup_reconciliation_required")

    @contextmanager
    def _reader(self, service):
        desired = self._pod(service)
        name = desired["metadata"]["name"]
        raw = json.dumps(desired).encode()
        command = ("-n", NAMESPACE, "create", "-f", "-", "-o", "json")
        preview = json.loads(self._reader_command("dry_run", (*command, "--dry-run=server"), body=raw))
        self._verify_pod(desired, preview, preview["metadata"].get("uid"))
        self.cleanup_unconfirmed = True
        try:
            created = json.loads(self.run(command, body=raw))
            uid = created["metadata"]["uid"]
            _need(_match(UID, uid) and created["metadata"]["name"] == name,
                  "pvc_capture_reader_creation_unconfirmed")
        except (TransitionBlocked, ValueError, KeyError):
            observed = self.run(("-n", NAMESPACE, "get", "pod", name, "--ignore-not-found", "-o", "json"))
            if observed.strip():
                found = json.loads(observed)
                uid = found["metadata"]["uid"]
                _need(_match(UID, uid), "pvc_capture_cleanup_reconciliation_required")
                node = found["spec"].get("nodeName")
                self._verify_pod(desired, found, uid, node=self.bindings[service]["node"] if node else None)
                self._remove_reader(name, uid)
                self.cleanup_unconfirmed = False
            raise TransitionBlocked("pvc_capture_reader_creation_unconfirmed") from None
        reader_failure = None
        try:
            self._verify_pod(desired, created, uid)
            self._reader_command("ready_wait", ("-n", NAMESPACE, "wait", "--for=condition=Ready",
                                                "pod/" + name, "--timeout=30s"), timeout=35)
            self._verify_running_reader(service, desired, name, uid)
            self._assert_runtime_mounts(name, len(self.bindings[service]["claims"]))
            yield name
            self._verify_running_reader(service, desired, name, uid)
        except BaseException as error:
            reader_failure = error
            raise
        finally:
            try:
                self._remove_reader(name, uid)
            except TransitionBlocked:
                code = str(reader_failure)
                if re.fullmatch(r"pvc_capture_reader_[a-z_]{1,40}", code):
                    raise TransitionBlocked(code + "_cleanup_unconfirmed") from None
                raise
            self.cleanup_unconfirmed = False

    def assert_settled(self):
        _need(not self.cleanup_unconfirmed, "pvc_capture_cleanup_reconciliation_required")

    def collect(self, root, assert_writers_closed):
        self.assert_settled()
        root = Path(root)
        _need(_private_root(root), "pvc_capture_private_destination_required")
        final = root / "pvc-snapshot"
        _need(not os.path.lexists(final), "pvc_capture_destination_exists")
        with tempfile.TemporaryDirectory(prefix="pvc-staging-", dir=root) as directory:
            staging = Path(directory)
            for service in sorted(self.bindings):
                assert_writers_closed()
                self._assert_sources(service)
                target = staging / service
                target.mkdir(mode=0o700)
                count = len(self.bindings[service]["claims"])
                with self._reader(service) as reader, tempfile.TemporaryFile(dir=root) as archive:
                    self._reader_command("archive_exec", ("-n", NAMESPACE, "exec", reader, "-c", "reader", "--",
                                                           "tar", "-C", "/capture", "-cf", "-",
                                                           *(f"v{i}" for i in range(count))),
                                         output=archive, timeout=120)
                    _need(archive.tell() <= MAX_ARCHIVE, "pvc_capture_archive_too_large")
                    archive.seek(0)
                    _extract(archive, target, count)
                assert_writers_closed()
                self._assert_sources(service)
            _need(not os.path.lexists(final), "pvc_capture_destination_exists")
            staging.rename(final)
        return {service: final / service for service in self.bindings}


def inspect_pvc_bindings(*, run=_command):
    """Bind an active app's exact image, node and five source volumes."""
    def read(*args):
        return json.loads(run((*args, "-o", "json")))

    rs = read("-n", NAMESPACE, "get", "replicasets")["items"]
    pods = read("-n", NAMESPACE, "get", "pods")["items"]

    def owned(item, kind, ids):
        return any(owner.get("controller") is True and owner["kind"] == kind and owner["uid"] in ids
                   for owner in item["metadata"].get("ownerReferences", ()))

    bindings = {}
    for service in sorted(K3S_SERVICES):
        deploy = read("-n", NAMESPACE, "get", "deployment", service)
        uid = deploy["metadata"]["uid"]
        controllers = {item["metadata"]["uid"] for item in rs if owned(item, "Deployment", {uid})}
        candidates = [pod for pod in pods if owned(pod, "ReplicaSet", controllers)
                      and not pod["metadata"].get("deletionTimestamp") and pod["status"].get("phase") == "Running"]
        _need(len(candidates) == 1, "pvc_capture_active_source_unconfirmed")
        pod, = candidates
        app = [row for row in pod["spec"]["containers"] if row["name"] == service]
        status = [row for row in pod["status"]["containerStatuses"] if row["name"] == service]
        _need(len(app) == len(status) == 1 and status[0]["ready"] is True, "pvc_capture_active_source_unconfirmed")
        script = ("import json,os,sys; rules=json.loads(sys.argv[1]); "
                  "print(json.dumps(all(os.getenv(key)==value or "
                  "(allow_default and not os.getenv(key)) for key,(value,allow_default) in rules.items())))")
        named = pod["metadata"]["name"]
        _need(_match(NAME, named), "pvc_capture_active_source_unconfirmed")
        result = run(("-n", NAMESPACE, "exec", "pod/" + named, "-c", service, "--",
                      "python3", "-c", script, json.dumps(APP_PATHS[service], separators=(",", ":"))))
        _need(json.loads(result) is True, "pvc_capture_application_paths_changed")
        claims = []
        for volume in pod["spec"]["volumes"]:
            if "persistentVolumeClaim" not in volume:
                continue
            name = volume["persistentVolumeClaim"]["claimName"]
            pvc = read("-n", NAMESPACE, "get", "pvc", name)
            pv_name = pvc["spec"]["volumeName"]
            pv = read("get", "pv", pv_name)
            spec = pv["spec"]
            ref = spec["claimRef"]
            _need(pvc["status"]["phase"] == pv["status"]["phase"] == "Bound"
                  and ref["uid"] == pvc["metadata"]["uid"] and ref["namespace"] == NAMESPACE
                  and ref["name"] == name and "local" in spec and spec.get("nodeAffinity", {}).get("required"),
                  "pvc_capture_claim_identity_changed")
            claims.append(dict(name=name, uid=pvc["metadata"]["uid"], pv=pv_name, pv_uid=pv["metadata"]["uid"],
                               pv_spec_sha256=_digest(spec)))
        claims.sort(key=lambda row: CLAIMS[service].index(row["name"]))
        image_id = status[0]["imageID"].removeprefix("docker-pullable://")
        image = app[0]["image"] if _match(IMAGE_PIN, app[0]["image"]) else image_id
        bindings[service] = dict(deployment_uid=uid, node=pod["spec"]["nodeName"], image=image,
                                 template_sha256=_digest(deploy["spec"]["template"]), image_id=image_id, claims=claims)
    # Reuse the same strict schema that will be applied to the frozen snapshot.
    PvcCapture(bindings, "0" * 64, run=run)
    return bindings
