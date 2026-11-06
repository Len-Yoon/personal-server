"""Fresh, closed Linux writer graph. No repair, credentials or inventory output.

The native Windows bridge verifies these exact source bytes before executing
them with python3 -I -B -S and sets __collector_sha256__ in the execution globals.
Unsupported providers/protected capability profiles fail closed. The OS/runtime
TCB is explicitly pinned, never inferred to be harmless from a process name.
"""
import hashlib
import ctypes
import base64
import http.client
import json
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

_LIVE_SPEC = None
_MOUNT_ENTRIES = None
_SOURCE_PATHS = {}

LIMIT = 1048576
SECONDS = 1.5
HEX = re.compile(r"[0-9a-f]{64}\Z")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
SERVICES = frozenset({"portal-web", "book-memo", "youtube-memo", "crawler-worker", "car-care-worker", "homeops-executor", "system-agent"})
MANAGED_SERVICES = SERVICES | {"postgres", "postgres-standby"}
PROVIDERS = frozenset({"docker", "kubernetes", "systemd", "cron", "processes", "mounts"})
REQUEST = {"schema_version", "nonce", "plan_sha256", "cluster", "generation", "collector_sha256", "spec"}
PIN = {"provider", "id", "role", "service", "spec_sha256", "runtime_sha256"}
TCB = {"pid", "start_ticks", "uid", "exe_sha256", "cmdline_sha256", "cgroup_sha256", "unit_id", "unit_spec_sha256"}
CAP = {"running", "restart", "desired", "active", "suspended", "isolated", "observer_safe", "delegated_review", "app_credentials", "management", "privileged", "host_namespace", "writable", "recreator_disabled"}
OBS = {"provider", "id", "service", "spec_sha256", "runtime_sha256", "capabilities", "image", "cluster", "generation", "state_mounts"}
TCB_EXES = frozenset({"/init", "/usr/lib/systemd/systemd", "/usr/lib/systemd/systemd-journald", "/usr/lib/systemd/systemd-udevd", "/usr/lib/systemd/systemd-logind", "/usr/lib/systemd/systemd-networkd", "/usr/lib/systemd/systemd-resolved", "/usr/lib/systemd/systemd-timesyncd", "/usr/bin/dockerd", "/usr/bin/containerd", "/usr/bin/containerd-shim-runc-v2", "/usr/local/bin/k3s", "/usr/bin/dbus-daemon"})
OS_PACKAGES = {"apt-daily.service": ("apt",), "apt-daily.timer": ("apt",), "apt-daily-upgrade.service": ("apt",), "apt-daily-upgrade.timer": ("apt",), "dpkg-db-backup.service": ("dpkg",), "dpkg-db-backup.timer": ("dpkg",), "rsyslog.service": ("rsyslog",), "syslog.socket": ("rsyslog", "systemd"), "ssh.service": ("openssh-server",), "sshd.service": ("openssh-server",), "ssh.socket": ("openssh-server",), "dbus.service": ("dbus", "dbus-daemon"), "dbus.socket": ("dbus", "dbus-daemon"), "cron.service": ("cron",), "user@.service": ("systemd",), "user-runtime-dir@.service": ("systemd",)}
SYSTEMD_OS_PACKAGES = ("systemd", "systemd-resolved", "systemd-timesyncd", "udev")
UNIT_PROPERTIES = frozenset({"Id", "MainPID", "Leader", "ControlGroup", "FragmentPath", "DropInPaths", "UnitFileState", "ActiveState", "SubState", "Restart", "ExecStart", "ExecStartPre", "ExecStartPost", "ExecStop", "ExecReload", "Environment", "EnvironmentFiles", "User", "Group", "SupplementaryGroups", "Triggers", "TriggeredBy", "Wants", "Requires", "PartOf", "BindsTo", "Conflicts", "Before", "After", "OnFailure", "OnSuccess", "TimersMonotonic", "TimersCalendar", "OnClockChange", "OnTimezoneChange", "Unit", "Paths", "Listen", "Accept", "BusName", "Delegate", "DynamicUser", "RootDirectory", "RootImage", "PrivateUsers", "PrivateNetwork", "NoNewPrivileges", "CapabilityBoundingSet", "AmbientCapabilities", "ReadWritePaths", "ReadOnlyPaths", "BindPaths", "BindReadOnlyPaths", "ExecMainPID"})
# A newly discoverable kind may be an external reconciler. It is unsupported,
# rather than silently omitted. API discovery and ALL supported objects are
# pinned, including storage/volume/RBAC/credential relationships.
K8S_RESOURCES = frozenset({"pods", "nodes", "namespaces", "services", "endpoints", "persistentvolumes", "persistentvolumeclaims", "serviceaccounts", "secrets", "configmaps", "events", "limitranges", "resourcequotas", "replicationcontrollers", "bindings", "componentstatuses", "deployments.apps", "daemonsets.apps", "replicasets.apps", "statefulsets.apps", "jobs.batch", "cronjobs.batch", "horizontalpodautoscalers.autoscaling", "clusterroles.rbac.authorization.k8s.io", "clusterrolebindings.rbac.authorization.k8s.io", "roles.rbac.authorization.k8s.io", "rolebindings.rbac.authorization.k8s.io", "storageclasses.storage.k8s.io", "volumeattachments.storage.k8s.io", "csidrivers.storage.k8s.io", "csinodes.storage.k8s.io", "csistoragecapacities.storage.k8s.io", "networkpolicies.networking.k8s.io", "ingresses.networking.k8s.io", "ingressclasses.networking.k8s.io", "poddisruptionbudgets.policy", "endpointslices.discovery.k8s.io", "events.events.k8s.io", "leases.coordination.k8s.io", "priorityclasses.scheduling.k8s.io", "runtimeclasses.node.k8s.io", "certificatesigningrequests.certificates.k8s.io", "validatingwebhookconfigurations.admissionregistration.k8s.io", "mutatingwebhookconfigurations.admissionregistration.k8s.io", "customresourcedefinitions.apiextensions.k8s.io", "apiservices.apiregistration.k8s.io"})
ACTORS = frozenset({"Pod", "Deployment", "DaemonSet", "ReplicaSet", "StatefulSet", "ReplicationController", "Job", "CronJob", "HorizontalPodAutoscaler"})
METRICS_RESOURCES = frozenset({"pods.metrics.k8s.io", "nodes.metrics.k8s.io"})


class Blocked(RuntimeError):
    pass


def deny(reason):
    raise Blocked(reason) from None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def exact(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        deny("invalid_graph_shape")


def integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        deny("invalid_graph_integer")


def text(value, maximum=256):
    if type(value) is not str or not value or len(value.encode()) > maximum or any(ord(c) < 32 for c in value):
        deny("invalid_graph_text")


def sha(value):
    if type(value) is not str or not HEX.fullmatch(value):
        deny("invalid_graph_digest")


def validate_tcb(items):
    if type(items) is not list:
        deny("invalid_graph_inventory")
    pids = set()
    for item in items:
        exact(item, TCB)
        for key in ("pid", "start_ticks"):
            integer(item[key], 1)
        integer(item["uid"])
        text(item["unit_id"])
        for key in ("exe_sha256", "cmdline_sha256", "cgroup_sha256", "unit_spec_sha256"):
            sha(item[key])
        if item["pid"] in pids:
            deny("duplicate_tcb_identity")
        pids.add(item["pid"])


def decode(payload):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                deny("duplicate_json_key")
            result[key] = value
        return result
    try:
        return json.loads(payload, object_pairs_hook=pairs, parse_constant=lambda _: deny("invalid_json_number"))
    except (ValueError, UnicodeError, TypeError):
        deny("invalid_graph_json")


def validate_request(request):
    exact(request, REQUEST)
    if type(request["schema_version"]) is not int or request["schema_version"] != 1:
        deny("unsupported_graph_version")
    for key in ("nonce", "plan_sha256", "collector_sha256"):
        sha(request[key])
    if type(request["cluster"]) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request["cluster"]):
        deny("invalid_graph_cluster")
    integer(request["generation"], 1)
    spec = request["spec"]
    exact(spec, {"schema_version", "uid", "boot_id", "engine_id", "kube_system_uid", "providers", "resources", "tcb_processes"})
    if type(spec["schema_version"]) is not int or spec["schema_version"] != 1:
        deny("unsupported_graph_version")
    integer(spec["uid"])
    if any(type(spec[k]) is not str or not UUID.fullmatch(spec[k]) for k in ("boot_id", "kube_system_uid")):
        deny("invalid_graph_identity")
    text(spec["engine_id"], 128)
    exact(spec["providers"], PROVIDERS)
    for value in spec["providers"].values():
        sha(value)
    if type(spec["resources"]) is not list or type(spec["tcb_processes"]) is not list:
        deny("invalid_graph_inventory")
    ids = set()
    for item in spec["resources"]:
        if type(item) is not dict:
            deny("invalid_graph_shape")
        additions = {"image", "cluster", "generation"} if item.get("role") == "managed_ha" else {"image", "profile", "state_mounts"} if item.get("role") == "protected" else set()
        exact(item, PIN | additions)
        if type(item["provider"]) is not str or item["provider"] not in PROVIDERS or type(item["role"]) is not str or item["role"] not in {"legacy", "protected", "managed_ha"}:
            deny("invalid_graph_role")
        text(item["id"])
        key = item["provider"], item["id"]
        if key in ids:
            deny("duplicate_graph_identity")
        ids.add(key)
        if type(item["service"]) is not str or item["service"] not in MANAGED_SERVICES | {""}:
            deny("invalid_graph_service")
        for key in ("spec_sha256", "runtime_sha256"):
            sha(item[key])
        if item["role"] == "managed_ha":
            if item["cluster"] != request["cluster"] or type(item["generation"]) is not int or item["generation"] != request["generation"]:
                deny("managed_identity_mismatch")
            if type(item["image"]) is not str or not re.fullmatch(r"sha256:[0-9a-f]{64}", item["image"]) or item["service"] not in MANAGED_SERVICES:
                deny("managed_image_required")
        elif item["role"] == "protected":
            if type(item["image"]) is not str or not re.fullmatch(r"sha256:[0-9a-f]{64}", item["image"]) or item["profile"] not in {"service", "host-observer", "delegated-reader"}:
                deny("protected_image_profile_required")
            validate_mount_pins(item["state_mounts"])
    validate_tcb(spec["tcb_processes"])
    return spec


def validate_mount_pins(values):
    if type(values) is not list:
        deny("invalid_state_mount_pins")
    for value in values:
        sha(value)
    if values != sorted(set(values)):
        deny("invalid_state_mount_pins")


def validate_snapshot(request, snapshot):
    spec = validate_request(request)
    exact(snapshot, {"uid", "boot_id", "engine_id", "kube_system_uid", "providers", "resources", "tcb_processes", "complete"})
    if snapshot["complete"] is not True:
        deny("incomplete_live_graph")
    validate_tcb(snapshot["tcb_processes"])
    for key in ("uid", "boot_id", "engine_id", "kube_system_uid", "providers", "tcb_processes"):
        if type(snapshot[key]) is not type(spec[key]) or snapshot[key] != spec[key]:
            deny("live_provider_identity_changed")
    if type(snapshot["resources"]) is not list:
        deny("invalid_live_inventory")
    actual = {}
    for item in snapshot["resources"]:
        exact(item, OBS)
        if type(item["provider"]) is not str or item["provider"] not in PROVIDERS:
            deny("invalid_live_inventory")
        text(item["id"])
        if type(item["service"]) is not str or item["service"] not in MANAGED_SERVICES | {""}:
            deny("invalid_graph_service")
        for field in ("spec_sha256", "runtime_sha256"):
            sha(item[field])
        if type(item["image"]) is not str or type(item["cluster"]) is not str:
            deny("invalid_live_inventory")
        integer(item["generation"])
        validate_mount_pins(item["state_mounts"])
        key = (item["provider"], item["id"])
        if key in actual:
            deny("duplicate_live_identity")
        actual[key] = item
    expected = {(item["provider"], item["id"]): item for item in spec["resources"]}
    if set(actual) != set(expected):
        deny("live_resource_set_changed")
    covered = set()
    for key, pin in expected.items():
        value = actual[key]
        compare = ("service", "spec_sha256") if pin["role"] == "managed_ha" else ("service", "spec_sha256", "runtime_sha256")
        if any(value[k] != pin[k] for k in compare):
            deny("live_resource_spec_changed")
        capabilities = value["capabilities"]
        exact(capabilities, CAP)
        for field in CAP - {"restart", "desired", "active"}:
            if type(capabilities[field]) is not bool:
                deny("invalid_live_capability")
        integer(capabilities["desired"])
        integer(capabilities["active"])
        if type(capabilities["restart"]) is not str:
            deny("invalid_live_capability")
        if pin["service"] in SERVICES:
            covered.add(pin["service"])
        if pin["role"] == "legacy":
            if (capabilities["running"] or capabilities["restart"] != "no" or capabilities["desired"] or capabilities["active"]
                    or not capabilities["suspended"] or not capabilities["recreator_disabled"]):
                deny("legacy_writer_or_recreator_active")
        elif pin["role"] == "protected":
            if (not capabilities["isolated"] or capabilities["management"] or capabilities["app_credentials"] or capabilities["privileged"]
                    or (capabilities["host_namespace"] and not (pin["profile"] == "host-observer" and capabilities["observer_safe"])) or capabilities["writable"]):
                deny("protected_capability_unverified")
            if value["image"] != pin["image"] or value["state_mounts"] != pin["state_mounts"]:
                deny("protected_state_or_image_changed")
            if pin["profile"] == "host-observer" and (not capabilities["observer_safe"] or value["state_mounts"]):
                deny("readonly_observer_required")
            if capabilities["delegated_review"] and pin["profile"] != "delegated-reader":
                deny("delegated_reader_profile_required")
        else:
            if value["provider"] not in {"docker", "kubernetes"} or any(type(value[k]) is not type(pin[k]) or value[k] != pin[k] for k in ("image", "cluster", "generation")):
                deny("managed_runtime_identity_changed")
            if capabilities["management"] or capabilities["privileged"] or capabilities["host_namespace"]:
                deny("managed_control_capability_unverified")
            if capabilities["app_credentials"]:
                # Opaque Secret/envFrom/credential env cannot prove a bounded
                # PG/OAuth identity. A future explicit credential profile must
                # validate references/roles before relaxing this boundary.
                deny("managed_credential_profile_unverified")
    if covered != SERVICES:
        deny("seven_service_inventory_required")


class Budget:
    def __init__(self):
        self.deadline, self.bytes = time.monotonic() + SECONDS, 0

    def left(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            deny("live_graph_deadline_exceeded")
        return remaining

    def count(self, payload):
        self.left()
        self.bytes += len(payload)
        if self.bytes > LIMIT:
            deny("live_graph_payload_exceeded")
        return payload

    def file(self, path):
        with open(path, "rb") as source:
            return self.count(source.read(LIMIT + 1))

    def hash_file(self, path):
        # Executable bytes are streamed into a digest; they are never inventory
        # payloads or cached between fresh snapshots.
        hashed = hashlib.sha256()
        with open(path, "rb") as source:
            while True:
                self.left()
                chunk = source.read(65536)
                if not chunk:
                    break
                hashed.update(chunk)
        return hashed.hexdigest()

    def command(self, argv):
        self.left()
        if argv[0] not in {"/usr/bin/systemctl", "/usr/local/bin/k3s", "/usr/bin/dpkg-query"}:
            deny("unsupported_provider_command")
        executable = Path(argv[0]).resolve(strict=True)
        for path in (executable, *executable.parents):
            info = path.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                deny("untrusted_provider_executable")
        owner = os.getpid()
        def parent_death():
            # Fixed native read-only query programs inherit a Linux parent-
            # death kill. This is no privilege grant; it kills this child when
            # the outer WSL observer is externally killed before its finally.
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != owner:
                os._exit(1)
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C", "HOME": "/nonexistent"}, start_new_session=True,
                                   preexec_fn=parent_death if sys.platform == "linux" else None)
        output = bytearray()
        try:
            os.set_blocking(process.stdout.fileno(), False)
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    for key, _ in selector.select(min(self.left(), 0.02)):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            if process.wait(timeout=self.left()) != 0:
                                deny("live_provider_query_failed")
                            return bytes(output)
                        output.extend(self.count(chunk))
        finally:
            # The leader can exit while its child retains the pipe. This is
            # our newly created session/group even when poll() is non-None.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=0.05)
            process.stdout.close()

    def docker(self, endpoint):
        info = Path("/var/run/docker.sock").lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o002:
            deny("untrusted_docker_socket")
        connection = http.client.HTTPConnection("localhost", timeout=min(self.left(), 0.25))
        unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        unix.settimeout(min(self.left(), 0.25))
        try:
            unix.connect("/var/run/docker.sock")
            connection.sock = unix
            connection.request("GET", "/v1.41/" + endpoint, headers={"Host": "localhost"})
            response = connection.getresponse()
            if response.status != 200:
                deny("live_docker_query_failed")
            chunks = []
            while True:
                unix.settimeout(min(self.left(), 0.1))
                chunk = response.read1(65536)
                if not chunk:
                    return decode(b"".join(chunks))
                chunks.append(self.count(chunk))
        finally:
            connection.close()
            unix.close()


def capability(**changes):
    result = dict(running=False, restart="no", desired=0, active=0, suspended=True, isolated=False, observer_safe=False, delegated_review=False, app_credentials=False,
                  management=False, privileged=False, host_namespace=False, writable=True, recreator_disabled=False)
    result.update(changes)
    return result


def record(provider, identifier, service, specification, runtime, cap, image="", labels=None, state_mounts=None):
    labels = labels or {}
    generation = labels.get("personal-server.ha-generation", "")
    generation = int(generation) if type(generation) is str and re.fullmatch(r"[1-9][0-9]*", generation) else 0
    return dict(provider=provider, id=identifier, service=service, spec_sha256=digest(specification), runtime_sha256=digest(runtime), capabilities=cap,
                image=image, cluster=labels.get("personal-server.ha-cluster", ""), generation=generation, state_mounts=sorted(set(state_mounts or [])))


def canonical_source(path):
    if type(path) is not str or not Path(path).is_absolute():
        deny("unresolved_volume_source")
    actual = Path(path).resolve(strict=True)
    if _MOUNT_ENTRIES is None:
        return "file:" + str(actual)  # Pure helper fixtures; live sets context.
    matches = [entry for entry in _MOUNT_ENTRIES if str(actual) == entry["point"] or str(actual).startswith(entry["point"].rstrip("/") + "/")]
    if not matches:
        deny("unresolved_filesystem_alias")
    entry = max(matches, key=lambda value: len(value["point"]))
    if entry["type"] not in {"ext2", "ext3", "ext4", "xfs", "btrfs", "tmpfs"}:
        deny("unsupported_filesystem_alias_provider")
    info = actual.stat()
    if entry["device"] != str(os.major(info.st_dev)) + ":" + str(os.minor(info.st_dev)):
        deny("filesystem_identity_changed")
    suffix = str(actual)[len(entry["point"].rstrip("/")):].lstrip("/")
    root = entry["root"].rstrip("/") + ("/" + suffix if suffix else "")
    coordinate = "file:fs:" + entry["device"] + ":" + (root or "/")
    _SOURCE_PATHS.setdefault(coordinate, set()).add(str(actual))
    return coordinate


def source_paths(source):
    return _SOURCE_PATHS.get(source, {source[5:]}) if source.startswith("file:") else set()


def parse_mountinfo(payload):
    result = []
    def unescape(value):
        return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)
    for line in payload.splitlines():
        fields = line.split()
        separator = fields.index("-")
        if separator < 6:
            deny("incomplete_mount_identity")
        result.append({"device": fields[2], "root": unescape(fields[3]), "point": unescape(fields[4]), "type": fields[separator + 1]})
    return result


def managed_keys():
    return {(r["provider"], r["id"]) for r in (_LIVE_SPEC or {}).get("resources", []) if r["role"] == "managed_ha"}


def overlaps(left, right):
    return left == right or left.startswith(right.rstrip("/") + "/") or right.startswith(left.rstrip("/") + "/")


def credential_environment(values):
    safe = {"PATH", "LANG", "LC_ALL", "TZ", "APP_ENV", "LOG_LEVEL", "PORT", "HOST", "PYTHON_VERSION", "PYTHON_SHA256", "PYTHON_PIP_VERSION", "PYTHON_SETUPTOOLS_VERSION", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "CADDY_VERSION", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "PG_MAJOR", "PG_VERSION", "PG_SHA256", "PGDATA", "GOSU_VERSION"}
    for value in values:
        if type(value) is not str or "=" not in value:
            return True
        key = value.partition("=")[0].upper()
        if key not in safe:
            return True
        if re.search(r"TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIAL|DATABASE|DOCKER_HOST|KUBECONFIG|SSH_AUTH_SOCK|PGPASS|PGSERVICE|PRIVATE_KEY|CLIENT_KEY|BEARER|AUTHORIZATION|(?:^|_)API_KEY(?:$|_)", key):
            return True
    return False


def control_environment(values):
    return any(re.search(r"DOCKER_|KUBECONFIG|KUBERNETES_MASTER|CONTAINERD_|SSH_AUTH_SOCK", value.partition("=")[0].upper()) for value in values)


def control_mount(source, target):
    value = source.casefold() + "/" + str(target).casefold()
    return source.startswith("control-secret:") or any(name in value for name in ("docker.sock", "containerd.sock", "crio.sock", "podman.sock", "ssh-auth.sock", "kubeconfig", "/.kube", "/serviceaccount", "/etc/rancher", "/id_rsa", "/id_ed25519"))


def readonly_service_account(pod, namespace, objects, *, allow_review=False):
    account = pod.get("serviceAccountName", "default")
    accounts = [o for o in objects if o["kind"] == "ServiceAccount" and o["metadata"].get("namespace") == namespace and o["metadata"]["name"] == account]
    if len(accounts) != 1:
        return False
    roles = {(o["kind"], o["metadata"].get("namespace", ""), o["metadata"]["name"]): o for o in objects if o["kind"] in {"Role", "ClusterRole"}}
    groups = {"system:authenticated", "system:serviceaccounts", "system:serviceaccounts:" + namespace}
    for binding in objects:
        if binding["kind"] not in {"RoleBinding", "ClusterRoleBinding"}:
            continue
        subjects = binding.get("subjects", [])
        applies = any(s.get("kind") == "ServiceAccount" and s.get("name") == account and s.get("namespace", binding["metadata"].get("namespace")) == namespace
                      or s.get("kind") == "User" and s.get("name") == "system:serviceaccount:" + namespace + ":" + account
                      or s.get("kind") == "Group" and s.get("name") in groups for s in subjects)
        if not applies:
            continue
        reference = binding["roleRef"]
        role = roles.get((reference["kind"], binding["metadata"].get("namespace", "") if reference["kind"] == "Role" else "", reference["name"]))
        if role is None or role.get("aggregationRule"):
            return False
        for rule in role.get("rules", []):
            if set(rule) - {"verbs", "resources", "apiGroups", "resourceNames", "nonResourceURLs"}:
                return False
            if not set(rule.get("verbs", [])) <= {"get", "list", "watch"}:
                api_groups = rule.get("apiGroups", [])
                reviews = {"authentication.k8s.io": {"tokenreviews"}, "authorization.k8s.io": {"subjectaccessreviews", "selfsubjectaccessreviews", "selfsubjectrulesreviews"}}
                if (not allow_review or set(rule.get("verbs", [])) != {"create"} or len(api_groups) != 1
                        or api_groups[0] not in reviews or not rule.get("resources") or not set(rule["resources"]) <= reviews[api_groups[0]]):
                    return False
            for resource in rule.get("resources", []):
                if "*" in resource or resource == "secrets" or any(part in resource for part in ("/exec", "/attach", "/proxy", "/token")):
                    return False
            if any("*" in value for value in rule.get("apiGroups", []) + rule.get("nonResourceURLs", [])):
                return False
    return True


def pod_template(item):
    if item["kind"] == "Pod":
        return item.get("spec", {})
    spec = item.get("spec", {})
    if item["kind"] == "CronJob":
        return spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
    return spec.get("template", {}).get("spec", {})


def pod_mount_sources(pod, namespace, identity, objects):
    volumes = {v["name"]: v for v in pod.get("volumes", [])}
    pvcs = {(o["metadata"].get("namespace"), o["metadata"]["name"]): o for o in objects if o["kind"] == "PersistentVolumeClaim"}
    pvs = {o["metadata"]["name"]: o for o in objects if o["kind"] == "PersistentVolume"}
    result, credentials = [], False
    for container in pod.get("containers", []) + pod.get("initContainers", []) + pod.get("ephemeralContainers", []):
        for mount in container.get("volumeMounts", []):
            volume = volumes.get(mount["name"])
            if volume is None or mount.get("subPathExpr"):
                deny("unresolved_pod_volume_relation")
            writable = mount.get("readOnly") is not True
            if "hostPath" in volume:
                source = canonical_source(volume["hostPath"]["path"])
            elif "persistentVolumeClaim" in volume:
                claim = volume["persistentVolumeClaim"]
                pvc = pvcs.get((namespace, claim["claimName"]))
                pv = pvs.get(pvc.get("spec", {}).get("volumeName")) if pvc else None
                if not pvc or not pv or pv.get("spec", {}).get("claimRef", {}).get("uid") != pvc["metadata"]["uid"]:
                    deny("unresolved_pvc_pv_relation")
                backend = pv["spec"].get("local", pv["spec"].get("hostPath"))
                if not backend:
                    deny("unsupported_volume_backend")
                source = canonical_source(backend["path"])
                writable = writable and claim.get("readOnly") is not True
            elif "emptyDir" in volume:
                source = "pod:" + identity + ":" + volume["name"]
            elif "configMap" in volume or "downwardAPI" in volume:
                source, writable = "projected:" + identity + ":" + volume["name"], False
            elif "projected" in volume:
                source, writable = "projected:" + identity + ":" + volume["name"], False
                projections = volume["projected"].get("sources", [])
                if any(set(s) - {"secret", "configMap", "downwardAPI", "serviceAccountToken"} for s in projections):
                    deny("unsupported_projected_volume_provider")
                tokens = [s["serviceAccountToken"] for s in projections if "serviceAccountToken" in s]
                if any("secret" in s for s in projections) or any(t.get("audience") for t in tokens):
                    credentials = True
                if len(tokens) == 1 and not credentials:
                    source = "sa-token:" + namespace + ":" + pod.get("serviceAccountName", "default") + ":" + identity + ":" + volume["name"]
            elif "secret" in volume:
                source, writable, credentials = "secret:" + identity + ":" + volume["name"], False, True
                secrets = [o for o in objects if o["kind"] == "Secret" and o["metadata"].get("namespace") == namespace and o["metadata"]["name"] == volume["secret"]["secretName"]]
                if len(secrets) != 1:
                    deny("unresolved_secret_volume_identity")
                secret = secrets[0]
                if secret.get("type") == "kubernetes.io/service-account-token" or set(secret.get("data", {})) & {"kubeconfig", "k3s.yaml", "admin.conf"}:
                    source = "control-secret:" + secret["metadata"]["uid"]
            else:
                deny("unsupported_pod_volume_provider")
            if mount.get("subPath"):
                if source.startswith("file:"):
                    source = canonical_source(next(iter(source_paths(source))) + "/" + mount["subPath"])
                else:
                    deny("unsupported_volume_subpath")
            result.append((source, writable, mount.get("mountPath", "")))
    return result, credentials


def pod_capabilities(item, objects):
    pod = pod_template(item)
    namespace = item["metadata"].get("namespace", "default")
    mounts, credentials = pod_mount_sources(pod, namespace, item["metadata"]["uid"], objects)
    containers = pod.get("containers", []) + pod.get("initContainers", []) + pod.get("ephemeralContainers", [])
    has_token = any("projected" in v and any("serviceAccountToken" in s for s in v["projected"].get("sources", [])) for v in pod.get("volumes", []))
    # A bounded read-only Kubernetes API token is allowed for a service, while
    # host observers receive no token at all. Effective group/SA grants count.
    api_token = pod.get("automountServiceAccountToken") is not False or has_token
    readonly = readonly_service_account(pod, namespace, objects) if api_token else True
    delegated = not readonly and readonly_service_account(pod, namespace, objects, allow_review=True)
    api_writer = not readonly and not delegated
    management = api_writer
    privileged, observer = False, bool(containers) and not has_token and pod.get("automountServiceAccountToken") is False
    for container in containers:
        security = container.get("securityContext", {})
        capabilities = security.get("capabilities", {})
        privileged |= security.get("privileged") is True or bool(set(capabilities.get("add", [])) - {"NET_BIND_SERVICE"}) or bool(container.get("volumeDevices"))
        env = container.get("env", [])
        values = [v.get("name", "") + "=" + str(v.get("value", "")) for v in env]
        credentials |= bool(container.get("envFrom")) or credential_environment(values) or any("valueFrom" in v and "fieldRef" not in v["valueFrom"] for v in env)
        management |= control_environment(values)
        uid = security.get("runAsUser", pod.get("securityContext", {}).get("runAsUser"))
        observer &= type(uid) is int and uid > 0 and "ALL" in capabilities.get("drop", []) and not capabilities.get("add") and security.get("allowPrivilegeEscalation") is False
    host_mounts = [m for m in mounts if any(path == "/" or any(overlaps(path, p) for p in ("/proc", "/sys", "/dev", "/run")) for path in source_paths(m[0]))]
    management |= any(control_mount(source, target) and not (source.startswith("sa-token:") and "/serviceaccount" in target and not api_writer) for source, _, target in mounts)
    observer &= not management and not credentials and all(not write for _, write, _ in mounts)
    return dict(isolated=bool(containers) and (not host_mounts or observer), observer_safe=bool(observer), delegated_review=bool(delegated), app_credentials=bool(credentials), management=bool(management), privileged=bool(privileged),
                host_namespace=any(pod.get(k) is True for k in ("hostPID", "hostIPC", "hostNetwork")), writable=False), mounts


def service_of(labels):
    found = {labels[k] for k in ("personal-server.ha-service", "com.docker.compose.service", "app.kubernetes.io/name", "app") if labels.get(k) in MANAGED_SERVICES}
    if len(found) > 1:
        deny("ambiguous_service_identity")
    return next(iter(found), "")


def collect_docker(budget):
    info = budget.docker("info")
    # Query-induced utilization/time counters are not executable configuration.
    info = {k: v for k, v in info.items() if k not in {"SystemTime", "NFd", "NGoroutines", "NEventsListener", "ContainersRunning", "ContainersPaused", "ContainersStopped"}}
    if info.get("Swarm", {}).get("LocalNodeState") != "inactive":
        deny("unsupported_swarm_provider")
    plugins = budget.docker("plugins")
    if plugins:
        deny("unsupported_docker_plugin")
    listing = budget.docker("containers/json?all=1")
    containers, resources, process_ids = [], [], {}
    for item in sorted(listing, key=lambda i: i["Id"]):
        identifier = item["Id"]
        if type(identifier) is not str or not HEX.fullmatch(identifier):
            deny("invalid_docker_identity")
        value = budget.docker("containers/" + identifier + "/json")
        if value.get("Id") != identifier:
            deny("docker_identity_race")
        containers.append(value)
        config, host, state = value["Config"], value["HostConfig"], value["State"]
        state = dict(state)
        if "Health" in state:
            state["Health"] = {"Status": state["Health"].get("Status")}
        labels = config.get("Labels") or {}
        namespaced = any(host.get(k) in {"host", "container"} or str(host.get(k, "")).startswith("container:") for k in ("NetworkMode", "PidMode", "IpcMode", "UTSMode", "CgroupnsMode"))
        mounts = value.get("Mounts", [])
        environment_safe = not credential_environment(config.get("Env") or [])
        mounted = [(canonical_source(m["Source"]), m) for m in mounts]
        management = control_environment(config.get("Env") or []) or any(control_mount(source, m.get("Destination", "")) for source, m in mounted)
        private_user = str(config.get("User", "")).partition(":")[0]
        observer = private_user.isdigit() and int(private_user) > 0 and not host.get("CapAdd") and "ALL" in (host.get("CapDrop") or []) and all(m.get("RW") is False for m in mounts) and not management
        host_root_mount = any(path == "/" or any(overlaps(path, p) for p in ("/proc", "/sys", "/dev", "/run")) for source, _ in mounted for path in source_paths(source))
        extra_caps = set(host.get("CapAdd") or []) - {"NET_BIND_SERVICE"}
        isolated = environment_safe and not management and not extra_caps and not host.get("Devices") and not host.get("DeviceRequests") and (not host_root_mount or observer)
        cap = capability()
        cap.update(running=state.get("Running") is not False or state.get("Restarting") is not False,
                   restart=host["RestartPolicy"]["Name"], active=int(state.get("Running") is True),
                   suspended=state.get("Running") is False, isolated=isolated, observer_safe=observer, management=management, app_credentials=not environment_safe,
                   privileged=host.get("Privileged") is not False or bool(host.get("Devices")) or bool(host.get("DeviceRequests")), host_namespace=namespaced,
                   writable=False, recreator_disabled=host["RestartPolicy"]["Name"] == "no")
        state_mounts = [hashlib.sha256(source.encode()).hexdigest() for source, m in mounted if m.get("RW") is not False]
        resources.append(record("docker", identifier, service_of(labels), {"Config": config, "HostConfig": host, "Mounts": mounts}, state, cap, value["Image"], labels, state_mounts))
        process_ids[identifier] = ("docker", identifier)
    catalog = {"info": info, "plugins": plugins, "containers": containers, "networks": budget.docker("networks"), "volumes": budget.docker("volumes")}
    return info["ID"], catalog, resources, process_ids


def docker_identity_catalog(catalog):
    managed = {identifier for provider, identifier in managed_keys() if provider == "docker"}
    result = dict(catalog)
    result["containers"] = []
    for container in catalog["containers"]:
        value = dict(container)
        state = dict(value["State"])
        if "Health" in state:
            state["Health"] = {"Status": state["Health"].get("Status")}
        value["State"] = state
        if value["Id"] in managed:
            value["State"] = {"owned_lifecycle": True}
            for key in ("RestartCount", "ExecIDs", "ResolvConfPath", "HostnamePath", "HostsPath"):
                value.pop(key, None)
            value["NetworkSettings"] = {"Networks": {name: {"NetworkID": network.get("NetworkID")} for name, network in value.get("NetworkSettings", {}).get("Networks", {}).items()}}
        result["containers"].append(value)
    result["networks"] = []
    for network in catalog["networks"]:
        value = dict(network)
        value["Containers"] = {identifier: v for identifier, v in value.get("Containers", {}).items() if identifier not in managed}
        result["networks"].append(value)
    return result


def mount_identity_catalog(payload, docker):
    owned = []
    for container in docker["containers"]:
        if ("docker", container["Id"]) not in managed_keys():
            continue
        merged = container.get("GraphDriver", {}).get("Data", {}).get("MergedDir")
        sandbox = container.get("NetworkSettings", {}).get("SandboxKey")
        owned.extend(p for p in (merged, sandbox) if p)
    lines = []
    for line in payload.splitlines():
        entry = parse_mountinfo(line)[0]
        if not any(entry["point"] == path or entry["point"].startswith(path.rstrip("/") + "/") for path in owned):
            lines.append(line)
    return "\n".join(lines)


def kubernetes_identity_catalog(catalog):
    managed = {identifier for provider, identifier in managed_keys() if provider == "kubernetes"}
    result = dict(catalog)
    result["objects"] = []
    for item in catalog["objects"]:
        value = dict(item)
        if value["metadata"]["uid"] in managed:
            value["status"] = {"owned_lifecycle": True}
        result["objects"].append(value)
    return result


def parse_kubeconfig(payload):
    """Only canonical K3s inline TLS configuration, never generic YAML.

    A finite generated-format grammar rejects unknown keys, plugins, aliases,
    tags, multiline scalars, proxies and insecure transport before kubectl runs.
    No YAML loader or kubeconfig-selected executable is invoked.
    """
    try:
        value = payload.decode("utf-8")
    except UnicodeError:
        deny("unsupported_kubeconfig_profile")
    encoded = r"([A-Za-z0-9+/]+={0,2})"
    pattern = (r"apiVersion: v1\nclusters:\n- cluster:\n    certificate-authority-data: " + encoded
               + r"\n    server: https://127\.0\.0\.1:6443\n  name: default\ncontexts:\n- context:\n    cluster: default\n    user: default\n  name: default\ncurrent-context: default\nkind: Config\npreferences: \{\}\nusers:\n- name: default\n  user:\n    client-certificate-data: " + encoded
               + r"\n    client-key-data: " + encoded + r"\n?")
    match = re.fullmatch(pattern, value)
    if match is None:
        deny("unsupported_kubeconfig_profile")
    try:
        ca, certificate, key = [base64.b64decode(v, validate=True) for v in match.groups()]
    except (ValueError, TypeError):
        deny("unsupported_kubeconfig_profile")
    if (any(len(v) > 32768 for v in (ca, certificate, key)) or not ca.startswith(b"-----BEGIN CERTIFICATE-----")
            or not certificate.startswith(b"-----BEGIN CERTIFICATE-----")
            or not any(key.startswith(b"-----BEGIN " + kind + b"PRIVATE KEY-----") for kind in (b"", b"EC ", b"RSA "))):
        deny("unsupported_kubeconfig_profile")
    return ca, certificate, key


def verify_kubeconfig(budget):
    path = Path("/etc/rancher/k3s/k3s.yaml")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1:
        deny("untrusted_kubeconfig_source")
    for parent in path.parents:
        item = parent.lstat()
        if not stat.S_ISDIR(item.st_mode) or item.st_uid != 0 or item.st_mode & 0o022:
            deny("untrusted_kubeconfig_source")
    values = parse_kubeconfig(budget.file(path))
    # Existing K3s static TLS files must actually match the validated inline
    # material. The child reads /dev/null as kubeconfig, so replacement of the
    # YAML after this check cannot introduce an exec/auth-provider plugin.
    names = ("server-ca.crt", "client-admin.crt", "client-admin.key")
    for name, expected in zip(names, values):
        existing = Path("/var/lib/rancher/k3s/server/tls") / name
        info = existing.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1:
            deny("untrusted_k3s_static_tls_source")
        for parent in existing.parents:
            item = parent.lstat()
            if not stat.S_ISDIR(item.st_mode) or item.st_uid != 0 or item.st_mode & 0o022:
                deny("untrusted_k3s_static_tls_source")
        if budget.file(existing) != expected:
            deny("k3s_static_tls_identity_mismatch")


def collect_kubernetes(budget):
    verify_kubeconfig(budget)
    prefix = ["/usr/local/bin/k3s", "kubectl", "--kubeconfig=/dev/null", "--server=https://127.0.0.1:6443", "--certificate-authority=/var/lib/rancher/k3s/server/tls/server-ca.crt", "--client-certificate=/var/lib/rancher/k3s/server/tls/client-admin.crt", "--client-key=/var/lib/rancher/k3s/server/tls/client-admin.key", "--request-timeout=1s"]
    discovery = budget.command(prefix + ["api-resources", "--verbs=list", "-o", "name"]).decode().splitlines()
    if len(set(discovery)) != len(discovery) or not discovery or any(not re.fullmatch(r"[a-z][a-z0-9.-]{0,252}", name) for name in discovery):
        deny("unsupported_kubernetes_provider")
    unknown = set(discovery) - K8S_RESOURCES - METRICS_RESOURCES
    custom_kinds = set()
    if unknown:
        definitions = decode(budget.command(prefix + ["get", "customresourcedefinitions.apiextensions.k8s.io", "-o", "json"]))
        declared = {item["spec"]["names"]["plural"] + "." + item["spec"]["group"] for item in definitions["items"]}
        custom_kinds = {item["spec"]["names"]["kind"] for item in definitions["items"]}
        if not unknown <= declared:
            deny("unsupported_kubernetes_provider")
    objects = decode(budget.command(prefix + ["get", ",".join(sorted(set(discovery) - METRICS_RESOURCES)), "--all-namespaces", "--chunk-size=0", "-o", "json"]))
    if type(objects) is not dict or type(objects.get("items")) is not list:
        deny("incomplete_kubernetes_inventory")
    # Heartbeats/managed-field timestamps are not actor identity, configuration,
    # replica count or restart history; keep all semantic fields and UIDs.
    def status_semantic(value):
        ignored = {"lastHeartbeatTime", "lastTransitionTime", "lastProbeTime"}
        if type(value) is dict:
            return {k: status_semantic(v) for k, v in value.items() if k not in ignored}
        if type(value) is list:
            return [status_semantic(v) for v in value]
        return value
    for item in objects["items"]:
        item["metadata"] = {k: v for k, v in item["metadata"].items() if k not in {"resourceVersion", "managedFields"}}
        if "status" in item:
            item["status"] = status_semantic(item["status"])
        if item["kind"] == "Lease":
            item["spec"] = {k: v for k, v in item.get("spec", {}).items() if k not in {"renewTime", "acquireTime"}}
    images_by_reference = {}
    for pod in (o for o in objects["items"] if o["kind"] == "Pod"):
        states = {c["name"]: c.get("imageID", "").split("@")[-1] for c in pod.get("status", {}).get("containerStatuses", []) + pod.get("status", {}).get("initContainerStatuses", [])}
        for container in pod.get("spec", {}).get("containers", []) + pod.get("spec", {}).get("initContainers", []):
            actual = states.get(container["name"], "")
            if re.fullmatch(r"sha256:[0-9a-f]{64}", actual):
                images_by_reference.setdefault(container["image"], set()).add(actual)
    resources, namespaces, process_ids = [], [], {}
    for item in objects["items"]:
        kind, metadata = item["kind"], item["metadata"]
        if kind in custom_kinds:
            deny("custom_reconciler_instance_unsupported")
        if kind == "Namespace" and metadata["name"] == "kube-system":
            namespaces.append(metadata["uid"])
        if kind == "APIService" and item.get("spec", {}).get("service"):
            if item.get("spec", {}).get("group") != "metrics.k8s.io":
                deny("unsupported_kubernetes_aggregator")
            reference = item["spec"]["service"]
            endpoints = [o for o in objects["items"] if o["kind"] == "Endpoints" and o["metadata"].get("namespace") == reference["namespace"] and o["metadata"]["name"] == reference["name"]]
            addresses = [a for endpoint in endpoints for subset in endpoint.get("subsets", []) for a in subset.get("addresses", [])]
            backing = [a.get("targetRef", {}) for a in addresses]
            pods = {o["metadata"]["uid"]: o for o in objects["items"] if o["kind"] == "Pod"}
            services = [o for o in objects["items"] if o["kind"] == "Service" and o["metadata"].get("namespace") == reference["namespace"] and o["metadata"]["name"] == reference["name"]]
            if len(services) != 1 or not backing or any(target.get("kind") != "Pod" or target.get("uid") not in pods for target in backing):
                deny("unresolved_aggregator_provider")
            if any(pods[a["targetRef"]["uid"]]["metadata"].get("namespace") != reference["namespace"] or pods[a["targetRef"]["uid"]].get("status", {}).get("podIP") != a.get("ip") for a in addresses):
                deny("aggregator_endpoint_identity_mismatch")
            for target in backing:
                containment, _ = pod_capabilities(pods[target["uid"]], objects["items"])
                if containment["management"] or containment["privileged"] or containment["host_namespace"] or not containment["isolated"]:
                    deny("aggregator_writer_capability_unverified")
        if kind not in ACTORS:
            continue
        spec, status = item.get("spec", {}), item.get("status", {})
        labels = dict(spec.get("template", {}).get("metadata", {}).get("labels") or {}) | (metadata.get("labels") or {})
        image, running, desired, active, suspended = "", False, 0, 0, True
        if kind == "HorizontalPodAutoscaler":
            reference = spec.get("scaleTargetRef", {})
            targets = [o for o in objects["items"] if o["kind"] == reference.get("kind") and o["metadata"].get("namespace") == metadata.get("namespace") and o["metadata"]["name"] == reference.get("name")]
            inactive = any(c.get("type") == "ScalingActive" and c.get("status") == "False" and c.get("reason") == "ScalingDisabled" for c in status.get("conditions", []))
            if len(targets) != 1 or reference.get("kind") not in {"Deployment", "StatefulSet", "ReplicaSet", "ReplicationController"} or targets[0].get("spec", {}).get("replicas") != 0 or status.get("currentReplicas") != 0 or status.get("desiredReplicas") != 0 or not inactive:
                deny("active_autoscaler_unsupported")
            desired, active, suspended = 0, 0, True
        elif kind == "Pod":
            running = status.get("phase") not in {"Succeeded", "Failed"}
            running = running or any("running" in c.get("state", {}) for c in status.get("containerStatuses", []) + status.get("initContainerStatuses", []) + status.get("ephemeralContainerStatuses", []))
            desired, active, suspended = int(running), int(running), not running
            containers = status.get("containerStatuses", [])
            if len(containers) == 1:
                image = containers[0].get("imageID", "").split("@")[-1]
            for container in containers + status.get("initContainerStatuses", []):
                identifier = container.get("containerID", "").split("://")[-1]
                if HEX.fullmatch(identifier):
                    process_ids[identifier] = ("kubernetes", metadata["uid"])
        elif kind == "CronJob":
            suspended = spec.get("suspend") is True
            active = len(status.get("active", []))
            desired = int(not suspended)
        elif kind == "Job":
            terminal = any(c.get("type") in {"Complete", "Failed"} and c.get("status") == "True" for c in status.get("conditions", []))
            active = status.get("active", 0)
            desired, suspended = int(not terminal and spec.get("suspend") is not True), terminal or spec.get("suspend") is True
        elif kind == "DaemonSet":
            desired = status.get("desiredNumberScheduled", 0)
            active = status.get("numberReady", 0)
            suspended = desired == 0
        else:
            desired, active = spec.get("replicas", 1), status.get("replicas", 0)
            suspended = desired == 0
        cap = capability()
        containment, mounts = pod_capabilities(item, objects["items"])
        template = pod_template(item)
        single_image = len(template.get("containers", [])) == 1 and not template.get("initContainers") and not template.get("ephemeralContainers")
        if not single_image:
            # The current image contract is one actual digest. Never hide an
            # init/ephemeral/additional image behind the main container digest
            # when managed status is normalized for its approved lifecycle.
            image = ""
        elif not image:
            images = [images_by_reference.get(c.get("image"), set()) for c in template.get("containers", []) + template.get("initContainers", [])]
            if images and all(len(s) == 1 for s in images):
                resolved = sorted(next(iter(s)) for s in images)
                image = resolved[0] if len(resolved) == 1 else "sha256:" + digest(resolved)
        cap.update(running=running or active > 0, desired=desired, active=active, suspended=suspended,
                   recreator_disabled=suspended and active == 0, **containment)
        state_mounts = [hashlib.sha256(source.encode()).hexdigest() for source, write, _ in mounts if write]
        resources.append(record("kubernetes", metadata["uid"], service_of(labels), item.get("spec", {}), {"metadata": metadata, "status": status}, cap, image, labels, state_mounts))
    if len(namespaces) != 1:
        deny("kubernetes_system_identity_required")
    return namespaces[0], {"discovery": sorted(discovery), "objects": sorted(objects["items"], key=lambda i: i["metadata"]["uid"])}, resources, process_ids


def mounted_control_socket(source, budget):
    if not source.startswith("file:"):
        return False
    pending, inspected = [Path(path) for path in source_paths(source)], 0
    while pending:
        budget.left()
        path = pending.pop()
        inspected += 1
        if inspected > 4096:
            deny("unsupported_mount_inspection_size")
        info = path.lstat()
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            deny("unsupported_hardlinked_mount_source")
        if stat.S_ISSOCK(info.st_mode):
            return True
        if stat.S_ISLNK(info.st_mode):
            deny("unsupported_nested_mount_symlink")
        if stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())
    return False


def protect_app_storage(resources, docker, kubernetes, budget=None):
    """Resolve cross-provider canonical storage ancestry before role validation."""
    sources, applications = {}, []
    for item in docker["containers"]:
        mounts = [(canonical_source(m["Source"]), m.get("RW") is not False) for m in item.get("Mounts", [])]
        sources[("docker", item["Id"])] = mounts
        if service_of(item["Config"].get("Labels") or {}):
            applications.extend(source for source, write in mounts if write)
    for item in kubernetes["objects"]:
        if item["kind"] not in ACTORS:
            continue
        mounts, _ = pod_mount_sources(pod_template(item), item["metadata"].get("namespace", "default"), item["metadata"]["uid"], kubernetes["objects"])
        sources[("kubernetes", item["metadata"]["uid"])] = [(source, write) for source, write, _ in mounts]
        labels = dict(item.get("spec", {}).get("template", {}).get("metadata", {}).get("labels") or {}) | (item["metadata"].get("labels") or {})
        if service_of(labels):
            applications.extend(source for source, write, _ in mounts if write)
    # A writable app directory includes submounts. Bind aliases are compared in
    # kernel filesystem-root coordinates, not caller-visible path strings.
    for source in list(applications):
        for path in source_paths(source):
            for entry in _MOUNT_ENTRIES or []:
                if entry["point"].startswith(path.rstrip("/") + "/"):
                    applications.append(canonical_source(entry["point"]))
    for item in resources:
        mounts = sources.get((item["provider"], item["id"]), [])
        item["capabilities"]["writable"] = any(write and overlaps(source, app) for source, write in mounts for app in applications)
        if budget is not None and any(mounted_control_socket(source, budget) for source, _ in mounts):
            item["capabilities"]["management"] = True
            item["capabilities"]["isolated"] = False


def collect_systemd(budget, user_uid=None):
    manager = ["/usr/bin/systemctl"] + (["--user", "--machine=" + str(user_uid) + "@.host"] if user_uid is not None else [])
    base = manager + ["--no-pager", "--no-legend", "--plain"]
    files = budget.command(base + ["list-unit-files", "--all"]).decode()
    units = budget.command(base + ["list-units", "--all"]).decode()
    names = {line.split()[0] for line in (files + "\n" + units).splitlines() if line.strip()}
    if any(not re.fullmatch(r"[A-Za-z0-9_.@:\\-]{1,255}", n) or n.startswith("-") for n in names):
        deny("unsupported_systemd_unit_identity")
    shown = budget.command(manager + ["--no-pager", "show", "--property=" + ",".join(sorted(UNIT_PROPERTIES)), *sorted(names)]).decode()
    registry, resources, configurations = {}, [], {}
    for block in shown.strip().split("\n\n"):
        properties = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        name = properties.get("Id")
        if name not in names or name in registry:
            deny("incomplete_systemd_inventory")
        content = {}
        paths = [properties.get("FragmentPath", "")] + properties.get("DropInPaths", "").split()
        for raw in paths:
            if raw:
                path = Path(raw)
                if not path.is_absolute():
                    deny("invalid_systemd_fragment")
                content[raw] = hashlib.sha256(budget.file(path)).hexdigest()
        configurations[name] = content
        # Volatile CPU/memory counters in systemctl's complete response must not
        # become a false claim of changed executable/recreator configuration.
        if properties.get("EnvironmentFiles"):
            deny("unsupported_unit_environment_provider")
        properties = {k: v for k, v in properties.items() if k in UNIT_PROPERTIES}
        properties["_source_files_sha256"] = digest(content)
        registry[name] = properties
        if name.endswith((".service", ".timer", ".socket", ".path")):
            live = properties.get("ActiveState") not in {"inactive", "failed"}
            cap = capability()
            cap.update(running=live, desired=int(live), active=int(live), suspended=not live,
                       restart=properties.get("Restart", "no"), recreator_disabled=properties.get("UnitFileState") in {"disabled", "masked", "masked-runtime"} and not properties.get("Triggers") and not properties.get("TriggeredBy"))
            resources.append(record("systemd", name, "", {"properties": properties, "files": content}, {k: properties.get(k) for k in ("ActiveState", "SubState", "MainPID", "UnitFileState")}, cap))
    if set(registry) != names:
        deny("incomplete_systemd_inventory")
    if user_uid is not None:
        prefix = "user:" + str(user_uid) + ":"
        registry = {prefix + name: dict(props, _manager_uid=user_uid) for name, props in registry.items()}
        configurations = {prefix + name: files for name, files in configurations.items()}
        for item in resources:
            item["id"] = prefix + item["id"]
    return {"unit_files": files, "units": units, "properties": registry, "files": configurations}, resources, registry


def packaged_source(budget, path, packages):
    """Installed package provenance plus SHA pin, not a unit-name exemption."""
    source = Path(path).resolve(strict=True)
    info = source.stat()
    if info.st_uid != 0 or info.st_mode & 0o022 or not source.is_file():
        return None
    md5, sha256 = hashlib.md5(usedforsecurity=False), hashlib.sha256()
    with source.open("rb") as handle:
        while True:
            budget.left()
            chunk = handle.read(65536)
            if not chunk:
                break
            md5.update(chunk)
            sha256.update(chunk)
    content_md5 = md5.hexdigest()
    budget.left()
    for package in packages:
        manifest = Path("/var/lib/dpkg/info") / (package + ".md5sums")
        if not manifest.is_file():
            continue
        raw = budget.file(manifest)
        for line in raw.decode().splitlines():
            checksum, relative = line.split(None, 1)
            if Path(relative).name == source.name and Path("/" + relative).resolve(strict=True) == source and checksum == content_md5:
                return {"package": package, "manifest_sha256": hashlib.sha256(raw).hexdigest(), "source_sha256": sha256.hexdigest()}
        conffiles = budget.command(["/usr/bin/dpkg-query", "--show", "--showformat=${Conffiles}", package])
        for line in conffiles.decode().splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1] == content_md5 and Path(fields[0]).resolve(strict=True) == source:
                return {"package": package, "manifest_sha256": digest([hashlib.sha256(raw).hexdigest(), hashlib.sha256(conffiles).hexdigest()]), "source_sha256": sha256.hexdigest()}
    return None


def os_activation_units(budget, registry, configurations):
    approved, evidence = set(), {}
    for name, properties in registry.items():
        local_name = properties.get("Id", name)
        template = re.sub(r"@[0-9]+\.", "@.", local_name)
        packages = OS_PACKAGES.get(template, SYSTEMD_OS_PACKAGES if local_name.startswith("systemd-") else ())
        if not packages or properties.get("DropInPaths") or properties.get("EnvironmentFiles"):
            continue
        fragment = properties.get("FragmentPath")
        if not fragment:
            continue
        proof = packaged_source(budget, fragment, packages)
        if proof is None:
            continue
        commands = []
        valid = True
        for field in ("ExecStart", "ExecStartPre", "ExecStartPost", "ExecStop", "ExecReload"):
            for path in re.findall(r"(?:^|[ {;])path=([^ ;}]+)", properties.get(field, "")):
                verified = packaged_source(budget, path, packages + ("systemd", "coreutils"))
                if verified is None:
                    valid = False
                    break
                commands.append(verified)
        if valid:
            approved.add(name)
            evidence[name] = {"fragment": proof, "commands": commands, "unit_sha256": digest(properties), "files_sha256": digest(configurations.get(name, {}))}
    # A timer/socket/OS service cannot be exempt while it activates an app unit.
    changed = True
    while changed:
        changed = False
        for name in list(approved):
            values = registry[name]
            manager = "user:" + str(values["_manager_uid"]) + ":" if "_manager_uid" in values else ""
            related = {manager + item for item in " ".join(values.get(k, "") for k in ("Triggers", "OnFailure", "OnSuccess", "Unit")).split()}
            if any(unit.endswith((".service", ".timer", ".socket", ".path")) and unit not in approved for unit in related):
                approved.remove(name)
                evidence.pop(name, None)
                changed = True
    return approved, evidence


def collect_cron(budget):
    catalog = {}
    for value in ("/etc/crontab", "/etc/anacrontab", "/etc/cron.d", "/etc/cron.hourly", "/etc/cron.daily", "/etc/cron.weekly", "/etc/cron.monthly", "/var/spool/cron"):
        path = Path(value)
        if not path.exists():
            continue
        files = sorted(path.rglob("*")) if path.is_dir() else [path]
        for child in files:
            if child.is_symlink():
                deny("unsupported_cron_symlink")
            if child.is_dir():
                continue
            info = child.stat()
            if not stat.S_ISREG(info.st_mode):
                deny("unsupported_cron_provider")
            content = budget.file(child)
            catalog[str(child)] = {"uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode), "sha256": hashlib.sha256(content).hexdigest(), "active": any(line.strip() and not line.lstrip().startswith(b"#") for line in content.splitlines())}
    resources = []
    for name, value in catalog.items():
        os_cron = {"/etc/crontab": ("cron",), "/etc/anacrontab": ("anacron",), "/etc/cron.daily/apt-compat": ("apt",), "/etc/cron.daily/dpkg": ("dpkg",)}
        proof = packaged_source(budget, name, os_cron[name]) if name in os_cron else None
        if proof is not None:
            value["os_activation"] = proof
            continue
        # A schedule or executable hook is a recreator, not a protected app.
        cap = capability()
        cap.update(recreator_disabled=not value["active"], suspended=not value["active"], desired=int(value["active"]))
        resources.append(record("cron", hashlib.sha256(name.encode()).hexdigest(), "", value, value, cap))
    return catalog, resources


def provider_configuration(budget):
    """Pin the fixed supported provider config roots, including activators."""
    result = {}
    for value in ("/etc/docker", "/etc/containerd", "/etc/rancher/k3s", "/var/lib/rancher/k3s/server/manifests", "/var/lib/rancher/k3s/agent/etc/containerd", "/etc/systemd", "/etc/dbus-1", "/usr/share/dbus-1/system-services"):
        root = Path(value)
        if not root.exists():
            result[value] = None
            continue
        entries = {}
        for path in sorted(root.rglob("*")):
            budget.left()
            if path.is_symlink():
                # Record the exact link and immutable target bytes; a changed
                # target/link cannot hide a provider definition change.
                target = path.resolve(strict=True)
                info = target.stat()
                if info.st_uid != 0 or info.st_mode & 0o022 or not target.is_file():
                    deny("unsupported_provider_configuration_link")
                entries[str(path)] = {"link": os.readlink(path), "sha256": budget.hash_file(target)}
            elif path.is_file():
                info = path.stat()
                if info.st_uid != 0 or info.st_mode & 0o022:
                    deny("untrusted_provider_configuration")
                entries[str(path)] = {"uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode), "sha256": budget.hash_file(path)}
            elif not path.is_dir():
                deny("unsupported_provider_configuration_entry")
        result[value] = entries
    return result


def runtime_provider_argv(executable, command):
    parts = command.rstrip(b"\x00").split(b"\x00")
    if not parts:
        return False
    if executable == "/usr/local/bin/k3s":
        # The same executable also contains kubectl/crictl/ctr clients, which
        # must not gain an OS/runtime exemption merely by using this binary.
        return len(parts) > 1 and parts[1] in {b"server", b"agent"} and not any(p.split(b"=", 1)[0] in {b"--config", b"--data-dir", b"--private-registry"} for p in parts[2:])
    if executable in {"/usr/bin/dockerd", "/usr/bin/containerd"}:
        if any(p.split(b"=", 1)[0] in {b"--config-file", b"--config", b"-c"} for p in parts[1:]):
            return False
        if any(b"tcp://" in p for p in parts[1:]):
            return False
    if executable.endswith("/systemd"):
        return b"--user" not in parts
    if executable.endswith("/dbus-daemon"):
        return b"--system" in parts
    return True


def packaged_os_process(executable, command, properties):
    paths = [p for field in ("ExecStart", "ExecStartPre", "ExecStartPost", "ExecStop", "ExecReload") for p in re.findall(r"(?:^|[ {;])path=([^ ;}]+)", properties.get(field, ""))]
    if executable in paths:
        return True
    parts = command.rstrip(b"\x00").split(b"\x00")
    interpreters = {"/usr/bin/dash", "/bin/dash", "/usr/bin/sh", "/bin/sh", "/usr/bin/bash", "/bin/bash", "/usr/bin/python3", "/usr/bin/perl"}
    return executable in interpreters and any(p.encode() in parts[1:] for p in paths)


def reader_session_ancestors(budget):
    """Exclude only the actual verified WSL /init reader ancestry, not clients.

    PID/start are transient here. The stable receipt binds root UID, exact /init
    executable bytes/cgroup and the attested collector source; unrelated /init
    instances still need the ordinary exact process registry.
    """
    excluded, chain, seen = set(), [], set()
    initial = parent = os.getppid()
    for _ in range(8):
        if parent <= 0 or parent in seen:
            deny("unverified_reader_ancestry")
        seen.add(parent)
        directory = Path("/proc") / str(parent)
        properties = dict(line.split(":", 1) for line in budget.file(directory / "status").decode().splitlines() if ":" in line)
        executable = directory / "exe"
        name = os.readlink(executable)
        if name != "/init":
            if parent != 1 and name not in {"/usr/lib/systemd/systemd", "/lib/systemd/systemd"}:
                deny("unsupported_reader_parent_boundary")
            break
        info = executable.stat()
        if int(properties["Uid"].split()[0]) != 0 or info.st_uid != 0 or info.st_mode & 0o022:
            deny("unverified_reader_ancestry")
        command = budget.file(directory / "cmdline")
        if command.split(b"\x00")[0] not in {b"/init", b"init"}:
            deny("unverified_reader_ancestry")
        chain.append({"uid": 0, "exe_sha256": budget.hash_file(executable), "cgroup_sha256": hashlib.sha256(budget.file(directory / "cgroup")).hexdigest()})
        excluded.add(parent)
        parent = int(properties["PPid"].strip())
    else:
        deny("unverified_reader_ancestry")
    if os.getppid() != initial:
        deny("reader_parent_changed")
    return excluded, {"collector_sha256": globals().get("__collector_sha256__"), "chain": chain, "interpreter": reader_interpreter(budget)}


def reader_interpreter(budget):
    executable = Path("/proc/self/exe")
    info = executable.stat()
    command = budget.file("/proc/self/cmdline")
    parts = command.rstrip(b"\x00").split(b"\x00")
    if (info.st_uid != 0 or info.st_mode & 0o022 or len(parts) != 6
            or parts[1:5] != [b"-I", b"-B", b"-S", b"-c"]):
        deny("unverified_reader_interpreter")
    status = dict(line.split(":", 1) for line in budget.file("/proc/self/status").decode().splitlines() if ":" in line)
    return {"uid": os.geteuid(), "exe_sha256": budget.hash_file(executable), "cmdline_sha256": hashlib.sha256(command).hexdigest(),
            "security": {k: status.get(k) for k in ("Uid", "Gid", "Groups", "CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb", "NoNewPrivs", "Seccomp")}}


def scoped_runtime_helpers(budget):
    result = {}
    for value in ("/usr/bin/containerd-shim-runc-v2",):
        path = Path(value)
        if path.is_file():
            proof = packaged_source(budget, path, ("containerd", "containerd.io"))
            if proof is not None:
                result[value] = proof
    return result


def owned_runtime_helper(name, command, uid, exe_hash, proofs, container_ids):
    if uid != 0 or name not in proofs or proofs[name]["source_sha256"] != exe_hash:
        return False
    parts = command.rstrip(b"\x00").split(b"\x00")
    fields = {}
    if len(parts[1:]) % 2:
        return False
    for i in range(1, len(parts), 2):
        if parts[i] not in {b"-namespace", b"-id", b"-address"} or parts[i] in fields:
            return False
        fields[parts[i]] = parts[i + 1]
    if set(fields) != {b"-namespace", b"-id", b"-address"}:
        return False
    owner = container_ids.get(fields[b"-id"].decode("ascii"))
    expected_ns = b"moby" if owner and owner[0] == "docker" else b"k8s.io"
    return owner in managed_keys() and fields[b"-namespace"] == expected_ns and fields[b"-address"] in {b"/run/containerd/containerd.sock", b"/run/k3s/containerd/containerd.sock"}


def collect_processes(budget, registry, container_ids, os_units=frozenset()):
    catalog, tcb, resources, used_units, joined = {}, [], [], set(), set()
    reader_pids, reader = reader_session_ancestors(budget)
    helpers = scoped_runtime_helpers(budget)
    # Every process is either a pinned OS/runtime TCB process, an independently
    # joined container process, or an explicit unsupported client. No argv/name
    # substring makes an arbitrary process exempt.
    for directory in sorted(Path("/proc").iterdir(), key=lambda p: p.name):
        if not directory.name.isdigit() or int(directory.name) == os.getpid() or int(directory.name) in reader_pids:
            continue
        status = budget.file(directory / "status").decode()
        fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
        procstat = budget.file(directory / "stat").decode()
        ticks = int(procstat[procstat.rfind(")") + 2:].split()[19])
        uid = int(fields["Uid"].split()[0])
        command = budget.file(directory / "cmdline")
        cgroup = budget.file(directory / "cgroup")
        matched = {container_ids[identifier] for identifier in container_ids if re.search(rb"(?:^|[/:\-])" + identifier.encode() + rb"(?:\.scope|/|$)", cgroup)}
        if len(matched) > 1:
            deny("ambiguous_process_container_relation")
        executable = directory / "exe"
        executable_name = os.readlink(executable)
        info = executable.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            deny("untrusted_process_executable")
        executable_hash = budget.hash_file(executable)
        again = budget.file(directory / "stat").decode()
        if int(again[again.rfind(")") + 2:].split()[19]) != ticks:
            deny("process_identity_changed_during_read")
        item = {"pid": int(directory.name), "start_ticks": ticks, "uid": uid, "exe_sha256": executable_hash,
                "cmdline_sha256": hashlib.sha256(command).hexdigest(), "cgroup_sha256": hashlib.sha256(cgroup).hexdigest()}
        security = {k: fields.get(k) for k in ("Uid", "Gid", "Groups", "CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb", "NoNewPrivs", "Seccomp", "NSpid")}
        helper = owned_runtime_helper(executable_name, command, uid, executable_hash, helpers, container_ids)
        if not helper and (not matched or not matched <= managed_keys()):
            catalog[directory.name] = {**item, "security": security, "owner": sorted(matched)}
        if helper:
            continue
        if matched:
            joined.update(matched)
            continue
        units = [(name, props) for name, props in registry.items() if props.get("MainPID") == directory.name or props.get("Leader") == directory.name]
        # PID1's systemd root is bound to the init.scope/unit configuration;
        # runtime helper children require an exact registered ControlGroup.
        if not units:
            paths = {line.split(":", 2)[2] for line in cgroup.decode().splitlines() if line.count(":") >= 2}
            units = [(name, props) for name, props in registry.items() if props.get("ControlGroup") and any(p == props["ControlGroup"] or p.startswith(props["ControlGroup"].rstrip("/") + "/") for p in paths) and (props.get("MainPID", "0") != "0" or props.get("Leader", "0") != "0")]
            if units:
                longest = max(len(props["ControlGroup"]) for _, props in units)
                units = [(name, props) for name, props in units if len(props["ControlGroup"]) == longest]
        canonical_executable = executable_name.replace("/lib/systemd/", "/usr/lib/systemd/") if executable_name.startswith("/lib/systemd/") else executable_name
        native = canonical_executable in TCB_EXES and runtime_provider_argv(canonical_executable, command)
        packaged = len(units) == 1 and units[0][0] in os_units and packaged_os_process(executable_name, command, units[0][1])
        if (native or packaged) and len(units) == 1:
            unit, props = units[0]
            item.update(unit_id=unit, unit_spec_sha256=digest(props))
            tcb.append(item)
            used_units.add(unit)
        else:
            cap = capability()
            cap.update(running=True, active=1, desired=1, suspended=False, privileged=int(fields.get("CapEff", "0").strip(), 16) != 0, host_namespace=True)
            resources.append(record("processes", directory.name + ":" + str(ticks), "", item, catalog[directory.name], cap))
    return {"processes": catalog, "reader_session": reader, "scoped_helper_binaries": helpers}, sorted(tcb, key=lambda p: p["pid"]), resources, used_units, joined


def collect_snapshot(budget):
    global _MOUNT_ENTRIES, _SOURCE_PATHS
    if sys.platform != "linux" or os.geteuid() != 0:
        deny("complete_linux_reader_required")
    mounts = budget.file("/proc/self/mountinfo").decode()
    _MOUNT_ENTRIES, _SOURCE_PATHS = parse_mountinfo(mounts), {}
    try:
        return _collect_snapshot(budget, mounts)
    finally:
        _MOUNT_ENTRIES, _SOURCE_PATHS = None, {}


def _collect_snapshot(budget, mounts):
    if sys.platform != "linux" or os.geteuid() != 0:
        # No sudo fallback; an ordinary reader cannot claim root cron, hidden
        # /proc entries, system service config or the k3s admin graph complete.
        deny("complete_linux_reader_required")
    budget.left()
    engine, docker, resources, docker_ids = collect_docker(budget)
    kube_uid, kubernetes, kube_resources, kube_ids = collect_kubernetes(budget)
    systemd, units, registry = collect_systemd(budget)
    # Inspect every existing user manager, including its timers/sockets/path
    # activators. Existing authenticated buses only; no manager is started.
    managers = {int(match.group(1)) for name, p in registry.items() if (match := re.fullmatch(r"user@([0-9]+)\.service", name)) and p.get("ActiveState") == "active"}
    for uid in sorted(managers):
        user_catalog, user_units, user_registry = collect_systemd(budget, uid)
        systemd.setdefault("user_managers", {})[str(uid)] = user_catalog
        systemd["files"].update(user_catalog["files"])
        registry.update(user_registry)
        units.extend(user_units)
    os_units, activation = os_activation_units(budget, registry, systemd["files"])
    systemd["os_activation"] = activation
    for name, proof in activation.items():
        registry[name]["_os_activation_sha256"] = digest(proof)
    cron, cron_resources = collect_cron(budget)
    if set(docker_ids) & set(kube_ids):
        deny("ambiguous_runtime_process_identity")
    processes, tcb, process_resources, tcb_units, joined = collect_processes(budget, registry, {**docker_ids, **kube_ids}, os_units)
    processes = {"instances": processes, "provider_configuration": provider_configuration(budget)}
    pod_ids = {o["metadata"]["uid"] for o in kubernetes["objects"] if o["kind"] == "Pod"}
    actual_containers = resources + [r for r in kube_resources if r["id"] in pod_ids]
    if any(r["capabilities"]["running"] and (r["provider"], r["id"]) not in joined for r in actual_containers):
        deny("incomplete_container_process_visibility")
    for item in actual_containers:
        if (item["provider"], item["id"]) in joined:
            item["capabilities"].update(running=True, active=max(1, item["capabilities"]["active"]))
    # TCB units remain fully catalog/config pinned; they are not falsely called
    # nonwriter applications. All other recreator/client units remain actors.
    resources += kube_resources + [u for u in units if u["id"] not in tcb_units | os_units] + cron_resources + process_resources
    protect_app_storage(resources, docker, kubernetes, budget)
    if budget.file("/proc/self/mountinfo").decode() != mounts:
        deny("mount_graph_changed_during_collection")
    snapshot = dict(uid=os.geteuid(), boot_id=budget.file("/proc/sys/kernel/random/boot_id").decode().strip(), engine_id=engine,
                    kube_system_uid=kube_uid, providers={k: digest(v) for k, v in dict(docker=docker_identity_catalog(docker), kubernetes=kubernetes_identity_catalog(kubernetes), systemd=systemd, cron=cron, processes=processes, mounts=mount_identity_catalog(mounts, docker)).items()},
                    resources=sorted(resources, key=lambda r: (r["provider"], r["id"])), tcb_processes=tcb, complete=True)
    budget.count(json.dumps(snapshot, sort_keys=True).encode())
    return snapshot


def evaluate(request, *, collector=collect_snapshot):
    global _LIVE_SPEC
    spec = validate_request(request)
    actual_source = globals().get("__collector_sha256__")
    if type(actual_source) is not str or not HEX.fullmatch(actual_source) or actual_source != request["collector_sha256"]:
        deny("verified_collector_source_required")
    budget = Budget()
    # An incremental reader bounds bodies; SIGALRM additionally interrupts a
    # stalled/drip-fed Unix HTTP header or provider launch on the live Linux
    # main thread. The outer native bridge independently enforces its 2s limit.
    timed = hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()
    previous = signal.getsignal(signal.SIGALRM) if timed else None
    termination = signal.getsignal(signal.SIGTERM) if timed else None
    if timed:
        if signal.getitimer(signal.ITIMER_REAL)[0]:
            deny("collector_timer_already_owned")
        signal.signal(signal.SIGALRM, lambda *_: deny("live_graph_deadline_exceeded"))
        signal.signal(signal.SIGTERM, lambda *_: deny("collector_cancelled"))
        signal.setitimer(signal.ITIMER_REAL, SECONDS)
    try:
        _LIVE_SPEC = spec
        first = collector(budget)
        validate_snapshot(request, first)
        second = collector(budget)
        validate_snapshot(request, second)
        if digest(first) != digest(second):
            deny("live_graph_changed_during_collection")
        budget.left()
        return {k: request[k] for k in REQUEST - {"spec"}} | {"complete": True, "verified": True}
    finally:
        _LIVE_SPEC = None
        if timed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
            signal.signal(signal.SIGTERM, termination)


def main():
    try:
        payload = sys.stdin.buffer.read(LIMIT + 1)
        if len(payload) > LIMIT:
            deny("live_graph_payload_exceeded")
        result = evaluate(decode(payload))
        code = 0
    except Exception as error:
        reason = str(error) if isinstance(error, Blocked) else "linux_live_graph_unavailable"
        result, code = {"status": "blocked", "reason": reason}, 1
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
