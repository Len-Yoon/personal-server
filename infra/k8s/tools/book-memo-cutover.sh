#!/usr/bin/env bash
set -euo pipefail

# Keep private paths, subprocess output and data in memory; emit only stage names.
exec python3 - "$0" "$@" <<'PY'
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid


NAMESPACE = "personal-server"
APP = "book-memo"
CLAIM = "book-memo-data"
HELPER = "book-memo-cutover-data"
MOUNT = "/data/book-memo"
UNCONFIGURED_IMAGE = "personal-server-book-memo:unconfigured-do-not-run"
stage = "arguments"
compose_changed = False
k3s_changed = False
helper_attempted = False
helper_uid = None
helper_owner = uuid.uuid4().hex
OWNER_LABEL = "app.kubernetes.io/cutover-owner"
mode = None


def require(condition):
    if not condition:
        raise RuntimeError()


def run(arguments, *, payload=None, stream=None):
    result = subprocess.run(arguments, input=payload, stdin=stream,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    require(result.returncode == 0)
    return result.stdout.decode()


def kube(*arguments, **kwargs):
    return run(["sudo", "-n", "k3s", "kubectl", "-n", NAMESPACE, *arguments], **kwargs)


def compose():
    return json.loads(run(["docker", "inspect", "--format", "{{json .State}}", APP]))


def compose_healthy():
    state = compose()
    require(state.get("Running") is True and state.get("Status") == "running"
            and state.get("Health", {}).get("Status") == "healthy")


def compose_stopped():
    state = compose()
    require(state.get("Running") is False and state.get("Status") == "exited")


def wait_healthy():
    for _ in range(30):
        try:
            compose_healthy()
            return
        except RuntimeError:
            time.sleep(2)
    raise RuntimeError()


def deployment():
    return json.loads(kube("get", "deployment", APP, "-o", "json"))


def writers_absent(*, allow_helper=False):
    require(deployment()["spec"].get("replicas") == 0)
    # Query all namespace pods, including terminating/unlabelled PVC consumers.
    for pod in json.loads(kube("get", "pods", "-o", "json"))["items"]:
        if allow_helper and pod["metadata"]["name"] == HELPER:
            continue
        require(not any(volume.get("persistentVolumeClaim", {}).get("claimName") == CLAIM
                        for volume in pod["spec"].get("volumes", [])))


def stop_k3s():
    kube("scale", "deployment", APP, "--replicas=0", "--timeout=120s")
    for _ in range(30):
        try:
            writers_absent(allow_helper=True)
            return
        except RuntimeError:
            time.sleep(2)
    raise RuntimeError()


def start_k3s():
    compose_stopped()
    require(deployment()["spec"]["template"]["spec"] == pod)
    kube("scale", "deployment", APP, "--replicas=1", "--timeout=120s")
    kube("rollout", "status", "deployment/" + APP, "--timeout=120s")


# Identical source/PVC validation: no links/special files, SQLite quick_check,
# framed relative names and content hashes. Output is a digest, never content.
DATA_TOOL = r'''
import hashlib, os, pathlib, shutil, sqlite3, stat, sys, tarfile
root = pathlib.Path(sys.argv[1])
database = pathlib.PurePosixPath(sys.argv[2])
action = sys.argv[3]
assert root.is_dir() and not root.is_symlink()
assert not database.is_absolute() and '..' not in database.parts
if action == 'extract':
    assert not any(root.iterdir())
    with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as archive:
        for member in archive:
            relative = pathlib.PurePosixPath(member.name)
            assert not relative.is_absolute() and '..' not in relative.parts
            assert member.isfile() or member.isdir()
            target = root.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as output, archive.extractfile(member) as source:
                    shutil.copyfileobj(source, output)
    sys.exit(0)
entries = sorted(root.rglob('*'))
for entry in entries:
    info = entry.lstat()
    assert stat.S_ISDIR(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
if action == 'layout':
    assert root.joinpath(*database.parts).is_file()
    sys.exit(0)
assert action == 'verify'
db = root.joinpath(*database.parts)
assert db.is_file()
with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as connection:
    assert connection.execute('PRAGMA quick_check').fetchall() == [('ok',)]
digest = hashlib.sha256()
for entry in sorted(root.rglob('*')):
    relative = entry.relative_to(root).as_posix().encode()
    digest.update(len(relative).to_bytes(8, 'big'))
    digest.update(relative)
    if entry.is_dir():
        digest.update(b'D')
    else:
        digest.update(b'F')
        file_digest = hashlib.sha256()
        with entry.open('rb') as handle:
            for block in iter(lambda: handle.read(1024*1024), b''):
                file_digest.update(block)
        digest.update(file_digest.digest())
print(digest.hexdigest())
'''


def data_check(location, action="verify"):
    if location == "source":
        return run([sys.executable, "-c", DATA_TOOL, str(source), database, action]).strip()
    return kube("exec", HELPER, "--", "python3", "-c", DATA_TOOL, MOUNT, database, action).strip()


def create_helper():
    global helper_attempted, helper_uid
    require(get_helper() is None)
    helper_uid = None
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": HELPER, "namespace": NAMESPACE,
                                                            "labels": {OWNER_LABEL: helper_owner}},
           "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                        "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [{"name": "data", "image": image, "imagePullPolicy": "Never",
                                    "command": ["python3", "-c", "import time; time.sleep(1800)"],
                                    "securityContext": {"readOnlyRootFilesystem": True,
                                                        "allowPrivilegeEscalation": False,
                                                        "capabilities": {"drop": ["ALL"]}},
                                    "volumeMounts": [{"name": CLAIM, "mountPath": MOUNT}]}],
                    "volumes": [{"name": CLAIM, "persistentVolumeClaim": {"claimName": CLAIM}}]}}
    # An uncertain create response requires an ownership query, never a blind delete.
    helper_attempted = True
    created = json.loads(kube("create", "-f", "-", "-o", "json", payload=json.dumps(pod).encode()))
    require(created["metadata"].get("labels", {}).get(OWNER_LABEL) == helper_owner)
    helper_uid = created["metadata"]["uid"]
    require(bool(helper_uid))
    kube("wait", "--for=condition=Ready", "pod/" + HELPER, "--timeout=120s")


def get_helper():
    result = kube("get", "pod", HELPER, "--ignore-not-found", "-o", "json").strip()
    return json.loads(result) if result else None


def delete_helper():
    global helper_attempted, helper_uid
    if helper_attempted:
        current_helper = get_helper()
        if current_helper is not None:
            metadata = current_helper["metadata"]
            require(metadata.get("labels", {}).get(OWNER_LABEL) == helper_owner)
            require(bool(metadata.get("uid")))
            if helper_uid is None:
                helper_uid = metadata["uid"]
            require(metadata["uid"] == helper_uid)
            # UID precondition makes replacement between GET and DELETE safe.
            options = {"apiVersion": "v1", "kind": "DeleteOptions",
                       "preconditions": {"uid": helper_uid}}
            kube("delete", "--raw=/api/v1/namespaces/" + NAMESPACE + "/pods/" + HELPER,
                 "-f", "-", payload=json.dumps(options).encode())
            kube("wait", "--for=delete", "pod/" + HELPER, "--timeout=120s")
            require(get_helper() is None)
        helper_attempted = False
        helper_uid = None


def recovery_data_matches():
    global stage
    create_helper()
    try:
        matches = data_check("target") == data_check("source")
    except Exception:
        stage = "recovery_data_unverified"
        delete_helper()
        return False
    delete_helper()
    if not matches:
        stage = "recovery_data_divergence"
    return matches


def recover():
    global stage
    try:
        if mode == "--go" and compose_changed:
            # Never start Compose if K3s termination or helper cleanup is uncertain.
            if k3s_changed:
                stop_k3s()
            delete_helper()
            writers_absent()
            if k3s_changed and not recovery_data_matches():
                # Keep both app writers stopped and preserve the newer PVC data.
                return
            writers_absent()
            run(["docker", "start", APP])
            wait_healthy()
        elif mode == "--rollback" and k3s_changed:
            delete_helper()
            # Failure after Compose start must stop it before returning to K3s.
            if compose_changed:
                run(["docker", "stop", "--time", "60", APP])
                compose_stopped()
                writers_absent()
                if not recovery_data_matches():
                    return
                writers_absent()
            start_k3s()
        else:
            delete_helper()
    except Exception:
        stage = "recovery_required"


def interrupted(_signal, _frame):
    raise RuntimeError()


try:
    arguments = sys.argv[2:]
    modes = {"--check", "--prepare", "--go", "--rollback"}
    options = {}
    while arguments:
        argument = arguments.pop(0)
        if argument in modes:
            require(mode is None)
            mode = argument
        elif argument in {"--source", "--database", "--image"}:
            require(argument not in options and bool(arguments))
            options[argument] = arguments.pop(0)
        else:
            raise RuntimeError()
    require(mode in modes and set(options) == {"--source", "--database", "--image"})
    image = options["--image"]
    require(re.fullmatch(r"docker\.io/library/personal-server-book-memo@sha256:[0-9a-f]{64}", image))
    source = Path(options["--source"])
    require(source.is_absolute() and source.is_dir() and source.resolve() == source
            and source != Path("/"))
    database = options["--database"]
    require(bool(database) and not Path(database).is_absolute() and ".." not in Path(database).parts)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)

    stage = "lock"
    # Lock the existing source directory without writing a lock/status file.
    lock = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    stage = "source"
    mounts = json.loads(run(["docker", "inspect", "--format", "{{json .Mounts}}", APP]))
    require(any(mount.get("Type") == "bind" and mount.get("Source") == str(source)
                and mount.get("Destination") == MOUNT and mount.get("RW") is True for mount in mounts))
    data_check("source", "layout")
    stage = "image"
    rows = run(["sudo", "-n", "k3s", "ctr", "images", "list"]).splitlines()
    require(any(len(fields) >= 5 and fields[0] == image and fields[2] == image.split("@", 1)[1]
                and any("linux/amd64" in value.split(",") for value in fields[3:])
                for fields in map(str.split, rows)))
    stage = "pvc"
    pvc = json.loads(kube("get", "pvc", CLAIM, "-o", "json"))
    require(pvc["status"]["phase"] == "Bound" and pvc["spec"]["accessModes"] == ["ReadWriteOnce"])
    stage = "deployment"
    current = deployment()
    pod = current["spec"]["template"]["spec"]
    require(current["spec"]["strategy"]["type"] == "Recreate")
    require(not pod.get("initContainers") and not pod.get("ephemeralContainers"))
    require(not pod.get("hostNetwork") and not pod.get("hostPID") and not pod.get("hostIPC"))
    require(len(pod["containers"]) == 1)
    container = pod["containers"][0]
    require(container["name"] == APP and container["image"] == image and container["imagePullPolicy"] == "Never")
    volumes = {volume["name"]: volume for volume in pod.get("volumes", [])}
    require(len(pod.get("volumes", [])) == 2 and set(volumes) == {CLAIM, "tmp"})
    require(volumes[CLAIM] == {"name": CLAIM, "persistentVolumeClaim": {"claimName": CLAIM}})
    require(volumes["tmp"].get("emptyDir", {}).get("medium") == "Memory"
            and set(volumes["tmp"]) == {"name", "emptyDir"})
    require(container.get("volumeMounts") == [{"name": CLAIM, "mountPath": MOUNT},
                                             {"name": "tmp", "mountPath": "/tmp"}])

    stage = "helper_collision"
    require(get_helper() is None)

    if mode in {"--check", "--prepare", "--go"}:
        stage = "compose_health"
        compose_healthy()
        stage = "writer_absence"
        writers_absent()
    if mode == "--prepare":
        stage = "dry_run"
        manifest = Path(sys.argv[1]).resolve().parents[1] / "apps" / "book-memo.yaml"
        template = manifest.read_text(encoding="utf-8")
        require(template.count(UNCONFIGURED_IMAGE) == 1)
        kube("apply", "--dry-run=server", "-f", "-",
             payload=template.replace(UNCONFIGURED_IMAGE, image).encode())
    elif mode == "--go":
        stage = "compose_stop"
        compose_changed = True
        run(["docker", "stop", "--time", "60", APP])
        compose_stopped()
        writers_absent()
        stage = "source_sqlite"
        before = data_check("source")
        stage = "helper"
        create_helper()
        stage = "copy"
        compose_stopped()
        writers_absent(allow_helper=True)
        # An unnamed, permission-restricted temporary archive is removed on close.
        with tempfile.TemporaryFile() as archive:
            with tarfile.open(fileobj=archive, mode="w") as output:
                for entry in sorted(source.rglob("*")):
                    require(not entry.is_symlink())
                    output.add(entry, arcname=entry.relative_to(source).as_posix(), recursive=False)
            archive.seek(0)
            kube("exec", "-i", HELPER, "--", "python3", "-c", DATA_TOOL,
                 MOUNT, database, "extract", stream=archive)
        stage = "data_verification"
        require(data_check("target") == before == data_check("source"))
        stage = "helper_cleanup"
        delete_helper()
        writers_absent()
        stage = "writer_handoff"
        k3s_changed = True
        start_k3s()
    elif mode == "--rollback":
        stage = "rollback_precondition"
        compose_stopped()
        require(current["spec"]["replicas"] == 1)
        stage = "k3s_stop"
        k3s_changed = True
        stop_k3s()
        stage = "rollback_data"
        create_helper()
        # Refuse stale-data rollback; reverse copying needs separate approval.
        require(data_check("target") == data_check("source"))
        delete_helper()
        writers_absent()
        stage = "compose_restore"
        compose_changed = True
        run(["docker", "start", APP])
        wait_healthy()
    print("book_memo_cutover=PASS")
except Exception:
    recover()
    print("book_memo_cutover=FAIL stage=" + stage, file=sys.stderr)
    sys.exit(1)
PY
