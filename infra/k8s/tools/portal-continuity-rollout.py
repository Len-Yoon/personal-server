#!/usr/bin/env python3
"""Prepare a guarded Portal JSON patch. This tool never calls Kubernetes."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import re
import sys


SESSION_PATH = "/var/lib/portal/auth/session-state.sqlite3"
CLAIMS = {"portal-web-files-dynamic", "portal-web-state-dynamic"}
LABEL = {"app.kubernetes.io/name": "portal-web"}
AFFINITY = {"podAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{"labelSelector": {"matchLabels": LABEL}, "namespaces": ["personal-server"], "topologyKey": "kubernetes.io/hostname"}]}}
LIFECYCLE = {"preStop": {"exec": {"command": ["python3", "-c", "import time; time.sleep(10)"]}}}


def prepare_patch(deployment, pvcs, pvs, image):
    if not re.fullmatch(r"[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("a digest-pinned image is required")
    metadata, spec = deployment["metadata"], deployment["spec"]
    if deployment.get("kind") != "Deployment" or (metadata.get("namespace"), metadata.get("name")) != ("personal-server", "portal-web"):
        raise ValueError("unexpected deployment")
    if not metadata.get("uid") or not metadata.get("resourceVersion") or spec.get("replicas") != 1:
        raise ValueError("a stable single-replica deployment is required")
    pod = spec["template"]["spec"]
    containers = pod["containers"]
    if len(containers) != 1 or containers[0].get("name") != "portal-web":
        raise ValueError("unexpected container layout")
    container = containers[0]
    if container.get("lifecycle") and container["lifecycle"] != LIFECYCLE:
        raise ValueError("existing lifecycle requires separate review")
    if container.get("imagePullPolicy") != "Never" or pod.get("securityContext", {}).get("runAsUser") != 10001:
        raise ValueError("unexpected image or filesystem ownership policy")
    if spec["template"]["metadata"].get("labels", {}).get("app.kubernetes.io/name") != "portal-web":
        raise ValueError("unexpected pod selector")
    mounted = {v["persistentVolumeClaim"]["claimName"] for v in pod.get("volumes", []) if "persistentVolumeClaim" in v}
    if mounted != CLAIMS:
        raise ValueError("unexpected PVCs")
    claims = {c["metadata"]["name"]: c for c in pvcs["items"] if c["metadata"].get("namespace") == "personal-server"}
    volumes = {v["metadata"]["name"]: v for v in pvs["items"]}
    nodes = None
    for name in CLAIMS:
        claim = claims[name]
        if claim.get("status", {}).get("phase") != "Bound" or claim["spec"].get("accessModes") != ["ReadWriteOnce"] or claim["spec"].get("volumeMode", "Filesystem") != "Filesystem":
            raise ValueError("bound same-node RWO claims are required")
        volume = volumes[claim["spec"]["volumeName"]]
        value = volume["spec"]
        reference = value.get("claimRef", {})
        if volume.get("status", {}).get("phase") != "Bound" or (reference.get("namespace"), reference.get("name"), reference.get("uid")) != ("personal-server", name, claim["metadata"].get("uid")) or not reference.get("uid"):
            raise ValueError("local volume claim ownership must match")
        if not (value.get("hostPath") or value.get("local")) or value.get("csi") or value.get("nfs"):
            raise ValueError("local filesystem volumes are required")
        terms = value.get("nodeAffinity", {}).get("required", {}).get("nodeSelectorTerms", [])
        hosts = set()
        for term in terms:
            expressions = term.get("matchExpressions", [])
            matches = [e for e in expressions if e.get("key") == "kubernetes.io/hostname" and e.get("operator") == "In"]
            if len(matches) != 1 or len(matches[0].get("values", [])) != 1:
                raise ValueError("each local-volume node term must identify one node")
            hosts.update(matches[0]["values"])
        if len(hosts) != 1:
            raise ValueError("a single local node is required")
        nodes = hosts if nodes is None else nodes & hosts
    if not nodes:
        raise ValueError("both PVCs must belong to the same node")
    affinity = pod.get("affinity")
    if affinity and affinity != AFFINITY:
        raise ValueError("existing affinity requires separate review")
    env = container.get("env", [])
    matches = [(i, e) for i, e in enumerate(env) if e.get("name") == "AUTH_SESSION_STATE_PATH"]
    if len(matches) > 1 or matches and matches[0][1].get("value") != SESSION_PATH:
        raise ValueError("unexpected session storage configuration")
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
        {"op": "test", "path": "/spec/replicas", "value": 1},
        {"op": "test", "path": "/spec/template/spec/containers/0/image", "value": container["image"]},
    ]
    def add(path, value):
        patch.append({"op": "add", "path": path, "value": value})
    add("/spec/strategy", {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}})
    add("/spec/minReadySeconds", 5)
    add("/spec/progressDeadlineSeconds", 600)
    add("/spec/template/spec/terminationGracePeriodSeconds", 330)
    add("/spec/template/spec/affinity", AFFINITY)
    base = "/spec/template/spec/containers/0"
    add(base + "/image", image)
    add(base + "/command", ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--timeout-graceful-shutdown", "300"])
    add(base + "/lifecycle", LIFECYCLE)
    add(base + "/readinessProbe", {"httpGet": {"path": "/ready", "port": "http"}, "periodSeconds": 3, "timeoutSeconds": 2, "failureThreshold": 3})
    add(base + "/startupProbe", {"httpGet": {"path": "/ready", "port": "http"}, "periodSeconds": 2, "timeoutSeconds": 2, "failureThreshold": 60})
    if not matches:
        if "env" not in container:
            add(base + "/env", [])
        add(base + "/env/-", {"name": "AUTH_SESSION_STATE_PATH", "value": SESSION_PATH})
    if not spec["template"]["metadata"].get("annotations"):
        add("/spec/template/metadata/annotations", {})
    add("/spec/template/metadata/annotations/portal.personal-server~1continuity-version", "1")
    return patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment-json", type=Path, required=True)
    parser.add_argument("--pvcs-json", type=Path, required=True)
    parser.add_argument("--pvs-json", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--backup-cronjob-json", type=Path)
    parser.add_argument("--backup-image")
    args = parser.parse_args()
    try:
        patch = prepare_patch(*(json.loads(p.read_text()) for p in (args.deployment_json, args.pvcs_json, args.pvs_json)), args.image)
        if bool(args.backup_cronjob_json) != bool(args.backup_image):
            raise ValueError("backup inputs must be supplied together")
        result = {"schema_version": 1, "requires_operating_approval": True, "deployment_patch": patch}
        if args.backup_cronjob_json:
            result["backup_cronjob_patch"] = prepare_backup_patch(json.loads(args.backup_cronjob_json.read_text()), args.backup_image)
            # A service-only drop-in preserves the existing timer and command.
            result["sequence_service_dropin"] = "[Service]\nEnvironment=PORTAL_BACKUP_ONLINE_IMAGE=" + args.backup_image + "\n"
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        parser.exit(2, "portal_continuity_plan=FAIL\n")
    print(json.dumps(result, separators=(",", ":")))


def prepare_backup_patch(cronjob, image):
    if not re.fullmatch(r"[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("a digest-pinned backup image is required")
    metadata, spec = cronjob["metadata"], cronjob["spec"]
    if cronjob.get("kind") != "CronJob" or (metadata.get("namespace"), metadata.get("name")) != ("personal-server", "portal-pvc-backup"):
        raise ValueError("unexpected backup cronjob")
    if not metadata.get("uid") or not metadata.get("resourceVersion") or spec.get("suspend") is not True or spec.get("concurrencyPolicy") != "Forbid" or cronjob.get("status", {}).get("active"):
        raise ValueError("suspended idle backup template is required")
    pod = spec["jobTemplate"]["spec"]["template"]["spec"]
    if len(pod["containers"]) != 1 or len(pod["initContainers"]) != 1:
        raise ValueError("unexpected backup container layout")
    main, init = pod["containers"][0], pod["initContainers"][0]
    if main.get("name") != "backup" or init.get("name") != "prepare-writable-scratch" or main.get("imagePullPolicy") != "Never" or init.get("imagePullPolicy") != "Never":
        raise ValueError("unexpected backup image policy")
    mounts = {m["mountPath"]: m for m in main.get("volumeMounts", [])}
    if any(mounts.get(path, {}).get("readOnly") is not True for path in ("/data/files", "/data/portal-web-state")):
        raise ValueError("both source PVC mounts must stay read-only")
    env = main.get("env", [])
    if [e.get("value") for e in env if e.get("name") == "PORTAL_BACKUP_EXECUTION_MODE"] != ["in-cluster"]:
        raise ValueError("online capture requires the in-cluster runner")
    existing = [e for e in env if e.get("name") == "PORTAL_BACKUP_SNAPSHOT_MODE"]
    if existing and existing != [{"name": "PORTAL_BACKUP_SNAPSHOT_MODE", "value": "online"}]:
        raise ValueError("unexpected snapshot mode")
    candidate = copy.deepcopy(cronjob)
    target = candidate["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    target["containers"][0]["image"] = image
    target["initContainers"][0]["image"] = image
    if not existing:
        target["containers"][0]["env"].append({"name": "PORTAL_BACKUP_SNAPSHOT_MODE", "value": "online"})
    name = "_portal_continuity_sequence_contract"
    module_spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("pvc-backup-sequence.py"))
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[name] = module
    module_spec.loader.exec_module(module)
    try:
        module.validate_cronjob("portal", candidate, online_image=image)
    except module.SafetyError:
        raise ValueError("backup template security drift") from None
    base = "/spec/jobTemplate/spec/template/spec"
    patch = [{"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
             {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
             {"op": "test", "path": "/spec/suspend", "value": True},
             {"op": "test", "path": base + "/containers/0/image", "value": main["image"]},
             {"op": "test", "path": base + "/initContainers/0/image", "value": init["image"]},
             {"op": "replace", "path": base + "/containers/0/image", "value": image},
             {"op": "replace", "path": base + "/initContainers/0/image", "value": image}]
    if not existing:
        patch.append({"op": "add", "path": base + "/containers/0/env/-", "value": {"name": "PORTAL_BACKUP_SNAPSHOT_MODE", "value": "online"}})
    return patch


if __name__ == "__main__":
    main()
