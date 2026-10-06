#!/usr/bin/env python3
"""Configure the existing Portal/executor shared authentication without secret files.

The existing Portal Kubernetes Secret is the canonical source. Supported
recreations use scripts/homeops-auth-run.py; ordinary restarts retain Env.
No scheduler/bootstrap integration or management POST is performed here.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

KEY = "HOMEOPS_EXECUTOR_SHARED_SECRET"
APP = "homeops-executor"
NAMESPACE = "personal-server"


class OperationError(Exception):
    """A fixed error code, never a command payload, output, or secret."""


def run_command(args, *, input_text=None, environment=None, timeout=90):
    try:
        completed = subprocess.run(args, input=input_text, env=environment,
                                   capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise OperationError("command_timeout:" + args[0]) from None
    except OSError:
        raise OperationError("command_unavailable:" + args[0]) from None
    if completed.returncode:
        raise OperationError("command_failed:" + args[0])
    return completed.stdout


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def load_contract():
    spec = importlib.util.spec_from_file_location("homeops_auth_contract", Path(__file__).with_name("homeops-auth-contract.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assert_portal_ready(deployment, pods, user):
    spec = deployment["spec"]
    if (spec.get("replicas") != 1 or spec.get("strategy", {}).get("type") != "Recreate"
            or deployment.get("status", {}).get("readyReplicas") != 1
            or len(spec["template"]["spec"]["containers"]) != 1
            or len(pods) != 1 or user != "10001"
            or pods[0].get("status", {}).get("phase") != "Running"
            or not pods[0]["status"]["containerStatuses"][0].get("ready")):
        raise OperationError("portal_single_writer_not_ready")


def decode_auth(encoded):
    try:
        value = base64.b64decode(encoded, validate=True).decode("ascii")
    except (ValueError, UnicodeError):
        raise OperationError("canonical_auth_invalid") from None
    if len(value) < 32 or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in value):
        raise OperationError("canonical_auth_invalid")
    return value


def rollback_portal_patch(original, current, expected_spec):
    if current["metadata"]["uid"] != original["metadata"]["uid"]:
        raise OperationError("rollback_portal_concurrent_change")
    if current["spec"] == original["spec"]:
        return []
    if current["spec"] != expected_spec:
        raise OperationError("rollback_portal_concurrent_change")
    env_path = "/spec/template/spec/containers/0/env"
    undo = [{"op": "test", "path": "/metadata/uid", "value": current["metadata"]["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": current["metadata"]["resourceVersion"]},
            {"op": "test", "path": env_path, "value": current["spec"]["template"]["spec"]["containers"][0]["env"]}]
    container = original["spec"]["template"]["spec"]["containers"][0]
    undo.append({"op": "replace", "path": env_path, "value": container["env"]} if "env" in container else {"op": "remove", "path": env_path})
    return undo


class Configuration:
    def __init__(self, repo):
        self.repo = Path(repo).resolve()
        self.contract = load_contract()

    def k(self, *args, input_text=None, namespace=NAMESPACE, timeout=90):
        command = ["sudo", "-n", "k3s", "kubectl", "-n", namespace, "--request-timeout=20s", *args]
        return json.loads(run_command(command + ["-o", "json"], input_text=input_text, timeout=timeout))

    def portal(self):
        deployment = self.k("get", "deployment", "portal-web")
        pods = [p for p in self.k("get", "pods", "-l", "app.kubernetes.io/name=portal-web")["items"]
                if not p["metadata"].get("deletionTimestamp")]
        if len(pods) != 1:
            raise OperationError("portal_single_writer_not_ready")
        user = run_command(["sudo", "-n", "k3s", "kubectl", "-n", NAMESPACE,
                            "exec", pods[0]["metadata"]["name"], "--", "id", "-u"]).strip()
        assert_portal_ready(deployment, pods, user)
        return deployment, pods[0]

    def docker(self):
        values = json.loads(run_command(["docker", "inspect", APP]))
        if len(values) != 1:
            raise OperationError("executor_single_instance_required")
        d = values[0]
        if not d["State"]["Running"] or d["State"].get("Health", {}).get("Status") != "healthy" or d["Config"]["User"] != "10001:10001":
            raise OperationError("executor_not_healthy_nonroot")
        ids = run_command(["docker", "ps", "--filter", "label=com.docker.compose.service=" + APP, "--format", "{{.ID}}"])
        if len(ids.splitlines()) != 1:
            raise OperationError("executor_single_instance_required")
        return d

    def source(self, deployment):
        refs = [x["secretRef"]["name"] for x in deployment["spec"]["template"]["spec"]["containers"][0].get("envFrom", []) if "secretRef" in x]
        if len(refs) != 1:
            raise OperationError("portal_secret_source_ambiguous")
        secret = self.k("get", "secret", refs[0])
        self.contract.validate_secret_source(deployment, secret)
        return secret

    def no_backup_activity(self):
        if any(x.get("status", {}).get("active", 0) for x in self.k("get", "jobs")["items"]):
            raise OperationError("backup_jobs_active")
        sequence = self.k("get", "configmap", "pvc-backup-sequence-state")["data"]
        if sequence.get("status") != "completed" or sequence.get("current") != "none" or sequence.get("lock_run_id"):
            raise OperationError("backup_sequence_not_idle")
        for name in ("book-pvc-backup-state", "youtube-pvc-backup-state", "crawler-pvc-backup-state"):
            if self.k("get", "configmap", name)["data"].get("lock_run_id"):
                raise OperationError("backup_lock_active")
        if self.k("get", "configmap", "sre-telegram-backup-status", namespace="monitoring")["data"].get("lock_run_id"):
            raise OperationError("portal_backup_lock_active")

    def bridge(self, docker):
        bindings = docker["HostConfig"]["PortBindings"]["8011/tcp"]
        values = [p["HostIp"] for p in bindings if p["HostIp"] != "127.0.0.1"]
        if len(bindings) != 2 or len(values) != 1 or not values[0]:
            raise OperationError("existing_bridge_binding_required")
        return values[0]

    def compose(self, before, auth):
        # All paths come from the actual existing container labels. Values are
        # captured only in memory; no rendered Compose or credential file exists.
        args, overrides = self.contract.compose_invocation(before, str(self.repo), auth, self.bridge(before))
        for f in before["Config"]["Labels"]["com.docker.compose.project.config_files"].split(","):
            if not Path(f).is_file() or Path(f).is_symlink():
                raise OperationError("existing_compose_file_invalid")
        environment = dict(os.environ, **overrides)
        up_index = args.index("up")
        full_configuration = json.loads(run_command(args[:up_index] + ["config", "--format", "json"], environment=environment))
        self.contract.assert_compose_networks(full_configuration, before)
        configuration = full_configuration["services"][APP]
        env = dict(x.split("=", 1) for x in before["Config"]["Env"])
        if configuration["image"] != before["Config"]["Image"]:
            raise OperationError("compose_image_drift")
        prospective_image = json.loads(run_command(["docker", "image", "inspect", configuration["image"]]))[0]
        if prospective_image["Id"] != before["Image"]:
            raise OperationError("compose_image_drift")
        expected_env = dict(x.split("=", 1) for x in prospective_image["Config"].get("Env", []))
        for key, value in configuration.get("environment", {}).items():
            if value is None:
                expected_env.pop(key, None)
            else:
                expected_env[key] = str(value)
        if {k: v for k, v in env.items() if k != KEY} != {k: v for k, v in expected_env.items() if k != KEY}:
            raise OperationError("compose_environment_drift")
        if env.get("HOMEOPS_DOCKER_MANAGED_SERVICES") != "system-agent,caddy,homeops-executor":
            raise OperationError("managed_scope_changed")
        return args, environment

    def secret_cas(self, name, patch):
        return self.k("patch", "secret", name, "--type=json", "--patch-file=/dev/stdin", input_text=json.dumps(patch))

    def portal_cas(self, patch):
        return self.k("patch", "deployment", "portal-web", "--type=json", "--patch-file=/dev/stdin", input_text=json.dumps(patch))

    def wait_executor(self, before):
        end = time.monotonic() + 120
        while time.monotonic() < end:
            value = json.loads(run_command(["docker", "inspect", APP]))[0]
            if value["State"]["Running"] and value["State"].get("Health", {}).get("Status") == "healthy":
                if value["Image"] != before["Image"] or self.contract.fingerprint(value, ignore_auth=True) != self.contract.fingerprint(before, ignore_auth=True):
                    raise OperationError("executor_configuration_changed")
                return value
            time.sleep(2)
        raise OperationError("executor_health_timeout")

    def wait_portal(self):
        run_command(["sudo", "-n", "k3s", "kubectl", "-n", NAMESPACE, "rollout", "status", "deployment/portal-web", "--timeout=180s"], timeout=210)
        return self.portal()

    def portal_auth_matches(self, auth, pod):
        code = "import os,sys,json; print(json.dumps(os.getenv('HOMEOPS_EXECUTOR_SHARED_SECRET')==json.loads(sys.stdin.read())['auth']))"
        return json.loads(run_command(["sudo", "-n", "k3s", "kubectl", "-n", NAMESPACE, "exec", "-i", pod["metadata"]["name"], "--", "python", "-c", code], input_text=json.dumps({"auth": auth}))) is True

    def assert_executor_recovery_owner(self, before, observed, applied_auth):
        before_auth = dict(x.split("=", 1) for x in before["Config"]["Env"]).get(KEY, "")
        observed_auth = dict(x.split("=", 1) for x in observed["Config"]["Env"]).get(KEY, "")
        if (observed["Image"] != before["Image"]
                or self.contract.fingerprint(observed, ignore_auth=True) != self.contract.fingerprint(before, ignore_auth=True)
                or observed_auth not in (before_auth, applied_auth)):
            raise OperationError("rollback_executor_concurrent_change")

    def verify(self, auth, pod):
        code = r'''import json,os,sys
from app.services.homeops import ExecutorClient,HomeOpsService
expected=json.loads(sys.stdin.read())['auth']
assert os.getenv('HOMEOPS_EXECUTOR_SHARED_SECRET')==expected
client=ExecutorClient();values=client.all_diagnostics()
scope=frozenset({'system-agent','caddy','homeops-executor'})
assert values.managed_services==scope and len(values)==3 and {x['service'] for x in values}==scope
service=object.__new__(HomeOpsService);service.executor=client
assert service._all_services_healthy(values)
print(json.dumps({'portal_auth_match':True,'authenticated_diagnostics':'PASS','membership':'PASS','scope_count':3,'summary_health':'PASS','healthcheck_absent':[x['service'] for x in values if x['container'].get('health')=='none'],'management_post':'NOT_PERFORMED'}))'''
        portal_proof = json.loads(run_command(["sudo", "-n", "k3s", "kubectl", "-n", NAMESPACE, "exec", "-i", pod["metadata"]["name"], "--", "python", "-c", code], input_text=json.dumps({"auth": auth})))
        code = r'''import os,json,sys
from urllib.request import Request,urlopen
from urllib.error import HTTPError
assert os.getenv('HOMEOPS_EXECUTOR_SHARED_SECRET')==json.loads(sys.stdin.read())['auth']
codes=[]
for headers in ({},{'X-HomeOps-Executor-Secret':'invalid-test-auth'}):
 try:
  with urlopen(Request('http://127.0.0.1:8011/v1/diagnostics',headers=headers),timeout=5) as response:codes.append(response.status)
 except HTTPError as exc:codes.append(exc.code)
assert codes==[403,403]
print(json.dumps({'executor_auth_match':True,'missing_auth_status':403,'wrong_auth_status':403}))'''
        executor_proof = json.loads(run_command(["docker", "exec", "-i", APP, "python", "-c", code], input_text=json.dumps({"auth": auth})))
        return dict(portal_proof, **executor_proof)

    def check(self):
        deployment, pod = self.portal()
        docker = self.docker()
        secret = self.source(deployment)
        if not secret.get("data", {}).get(KEY):
            return {"status": "NOT_CONFIGURED", "canonical_auth": False, "runtime_source": deployment["spec"]["template"]["spec"]["containers"][0]["image"]}
        auth = decode_auth(secret["data"][KEY])
        env = dict(x.split("=", 1) for x in docker["Config"]["Env"])
        if not env.get(KEY):
            return {"status": "EXECUTOR_SYNC_REQUIRED", "canonical_auth": True}
        if env[KEY] != auth:
            raise OperationError("executor_auth_conflicts_with_canonical")
        return dict(status="PASS", **self.verify(auth, pod))

    def configure(self, expected_commit):
        if run_command(["git", "rev-parse", "HEAD"]).strip() != expected_commit or run_command(["git", "branch", "--show-current"]).strip() != "main":
            raise OperationError("approved_main_commit_required")
        if any(not x.startswith("?? ") for x in run_command(["git", "status", "--porcelain"]).splitlines()):
            raise OperationError("tracked_worktree_changed")
        deployment, pod = self.portal()
        docker = self.docker()
        secret = self.source(deployment)
        encoded = secret["data"].get(KEY)
        auth = decode_auth(encoded) if encoded else secrets.token_hex(32)
        if KEY in secret["data"] and not encoded:
            raise OperationError("canonical_auth_empty_key")
        old_auth = dict(x.split("=", 1) for x in docker["Config"]["Env"]).get(KEY, "")
        if old_auth and old_auth != auth:
            raise OperationError("executor_auth_conflicts_with_canonical")
        portal_loaded = self.portal_auth_matches(auth, pod) if encoded else False
        if encoded and old_auth and portal_loaded:
            return dict(status="PASS", mutation="NONE", **self.verify(auth, pod))
        executor_needs_update = old_auth != auth
        args, environment = self.compose(docker, auth) if executor_needs_update else (None, None)
        patch = self.contract.portal_patch(deployment, secret["metadata"]["name"])
        baseline_spec = copy.deepcopy(deployment["spec"])
        expected_spec = copy.deepcopy(baseline_spec)
        expected_env = expected_spec["template"]["spec"]["containers"][0].setdefault("env", [])
        if not any(x["name"] == KEY for x in expected_env):
            expected_env.append({"name": KEY, "valueFrom": {"secretKeyRef": {"name": secret["metadata"]["name"], "key": KEY, "optional": False}}})
        executor_attempted = False
        spec = importlib.util.spec_from_file_location("homeops_backup_sequence", Path(__file__).with_name("pvc-backup-sequence.py"))
        sequence = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = sequence
        spec.loader.exec_module(sequence)
        with sequence.local_lock():
            self.no_backup_activity()
            fresh, _ = self.portal()
            if fresh["metadata"]["resourceVersion"] != deployment["metadata"]["resourceVersion"] or self.docker()["Id"] != docker["Id"]:
                raise OperationError("target_changed_after_preflight")
            stage = "canonical_secret"
            try:
                if not encoded:
                    value = base64.b64encode(auth.encode("ascii")).decode("ascii")
                    secret = self.secret_cas(secret["metadata"]["name"], self.contract.secret_patch(secret, value))
                if executor_needs_update:
                    stage = "executor_apply"
                    executor_attempted = True
                    run_command(args, environment=environment, timeout=180)
                    stage = "executor_health"
                    self.wait_executor(docker)
                stage = "backup_idle"
                self.no_backup_activity()
                if patch:
                    stage = "portal_apply"
                    self.portal_cas(patch)
                    stage = "portal_rollout"
                    _, pod = self.wait_portal()
                elif not portal_loaded:
                    stage = "portal_rollout"
                    _, pod = self.wait_portal()
                stage = "target_preservation"
                after, _ = self.portal()
                if after["metadata"]["uid"] != deployment["metadata"]["uid"] or after["spec"] != expected_spec:
                    raise OperationError("portal_configuration_changed")
                current_secret = self.source(after)
                other = {k: v for k, v in current_secret["data"].items() if k != KEY}
                before_other = {k: v for k, v in secret["data"].items() if k != KEY}
                if other != before_other or decode_auth(current_secret["data"][KEY]) != auth:
                    raise OperationError("secret_changed_concurrently")
                stage = "authenticated_verification"
                result = self.verify(auth, pod)
                return dict(status="PASS", mutation="CONFIGURED", rollback_used=False, **result)
            except Exception:
                # Read fresh state before any recovery. Roll back only our exact
                # added env/key; never overwrite an entire Secret or Deployment.
                try:
                    current = self.k("get", "deployment", "portal-web")
                    undo = rollback_portal_patch(deployment, current, expected_spec)
                    if undo:
                        self.portal_cas(undo)
                        self.wait_portal()
                    if executor_attempted:
                        observed = json.loads(run_command(["docker", "inspect", APP]))[0]
                        self.assert_executor_recovery_owner(docker, observed, auth)
                        rollback_env = dict(environment, **{KEY: old_auth})
                        run_command(args, environment=rollback_env, timeout=180)
                        self.wait_executor(docker)
                    if not encoded:
                        current_secret = self.k("get", "secret", secret["metadata"]["name"])
                        if current_secret["metadata"]["uid"] != secret["metadata"]["uid"]:
                            raise OperationError("rollback_secret_concurrent_change")
                        if KEY in current_secret["data"]:
                            if decode_auth(current_secret["data"][KEY]) != auth:
                                raise OperationError("rollback_secret_concurrent_change")
                            self.secret_cas(secret["metadata"]["name"], [
                                {"op": "test", "path": "/metadata/uid", "value": current_secret["metadata"]["uid"]},
                                {"op": "test", "path": "/metadata/resourceVersion", "value": current_secret["metadata"]["resourceVersion"]},
                                {"op": "test", "path": "/data/" + KEY, "value": current_secret["data"][KEY]},
                                {"op": "remove", "path": "/data/" + KEY}])
                except Exception:
                    raise OperationError("configuration_failed_recovery_requires_inspection") from None
                raise OperationError("configuration_failed_rollback_completed:" + stage) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--go", action="store_true")
    parser.add_argument("--expected-commit")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[3]))
    options = parser.parse_args(argv)
    try:
        if options.go and (not options.expected_commit or len(options.expected_commit) != 40 or any(c not in "0123456789abcdef" for c in options.expected_commit)):
            raise OperationError("expected_commit_required")
        os.chdir(options.repo)
        service = Configuration(options.repo)
        result = service.configure(options.expected_commit) if options.go else service.check()
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "PASS" else 2
    except Exception as exc:
        # Never render arbitrary exceptions: SDK/subprocess errors can embed
        # credentials, internal endpoints, or account paths.
        code = str(exc) if isinstance(exc, OperationError) else "validation_failed"
        print(json.dumps({"status": "FAIL", "reason": code}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
