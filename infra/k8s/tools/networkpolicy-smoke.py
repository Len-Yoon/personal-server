#!/usr/bin/env python3
"""Observe NetworkPolicy enforcement only in a disposable namespace."""

import argparse
import ipaddress
import json
import re
import secrets
import signal
import subprocess
import sys
import time


IMAGE = "busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
NAMESPACE_PREFIX = "np-smoke-"
RUN_ID_PATTERN = re.compile(r"[0-9a-f]{12}\Z")
NAMESPACE_PATTERN = re.compile(r"np-smoke-([0-9a-f]{12})\Z")
OWNERSHIP_LABEL = "len.pe.kr/networkpolicy-smoke-id"
DNS_NAME = "kubernetes.default.svc.cluster.local"


class SmokeError(Exception):
    pass


class Kube:
    def __init__(self, context: str):
        if not context or context.startswith("-"):
            raise SmokeError("explicit Kubernetes context is required")
        self.context = context
        self.base = ("sudo", "-n", "k3s", "kubectl")

    def current_context(self) -> str:
        try:
            result = subprocess.run(
                [*self.base, "config", "current-context"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SmokeError("cannot read current Kubernetes context") from error
        if result.returncode:
            raise SmokeError("cannot read current Kubernetes context")
        return result.stdout.strip()

    def run(self, *args: str, input_text: str | None = None, check: bool = True, timeout: int = 30):
        try:
            result = subprocess.run(
                [*self.base, "--context", self.context, *args],
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SmokeError(f"kubectl {args[0]} did not complete") from error
        if check and result.returncode:
            raise SmokeError(f"kubectl {args[0]} failed")
        return result


def namespace_name(run_id: str) -> str:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise SmokeError("invalid smoke run id")
    return f"{NAMESPACE_PREFIX}{run_id}"


def build_namespace(run_id: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": namespace_name(run_id),
            "labels": {"app.kubernetes.io/part-of": "personal-server", OWNERSHIP_LABEL: run_id},
        },
    }


def _pod(namespace: str, name: str, command: list[str]) -> dict:
    container = {
        "name": name,
        "image": IMAGE,
        "imagePullPolicy": "Never",
        "command": command,
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "resources": {"requests": {"cpu": "5m", "memory": "8Mi"}, "limits": {"cpu": "100m", "memory": "64Mi"}},
    }
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace, "labels": {"role": name}},
        "spec": {
            "automountServiceAccountToken": False,
            "restartPolicy": "Never",
            "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [container],
        },
    }


def build_pods(namespace: str) -> dict:
    if not NAMESPACE_PATTERN.fullmatch(namespace):
        raise SmokeError("temporary namespace name required")
    server = _pod(namespace, "server", ["sh", "-c", "mkdir -p /tmp/www; printf ok >/tmp/www/index.html; exec httpd -f -p 8080 -h /tmp/www"])
    server["spec"]["containers"][0]["volumeMounts"] = [{"name": "temporary-content", "mountPath": "/tmp"}]
    server["spec"]["volumes"] = [{"name": "temporary-content", "emptyDir": {}}]
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            server,
            _pod(namespace, "allowed", ["sleep", "3600"]),
            _pod(namespace, "blocked", ["sleep", "3600"]),
        ],
    }


def build_deny_policy(namespace: str) -> dict:
    if not NAMESPACE_PATTERN.fullmatch(namespace):
        raise SmokeError("temporary namespace name required")
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "default-deny", "namespace": namespace},
        "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
    }


def build_allow_policies(namespace: str) -> dict:
    if not NAMESPACE_PATTERN.fullmatch(namespace):
        raise SmokeError("temporary namespace name required")
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "allow-http-ingress", "namespace": namespace},
                "spec": {
                    "podSelector": {"matchLabels": {"role": "server"}},
                    "policyTypes": ["Ingress"],
                    "ingress": [{"from": [{"podSelector": {"matchLabels": {"role": "allowed"}}}], "ports": [{"protocol": "TCP", "port": 8080}]}],
                },
            },
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "allow-http-and-dns-egress", "namespace": namespace},
                "spec": {
                    "podSelector": {"matchLabels": {"role": "allowed"}},
                    "policyTypes": ["Egress"],
                    "egress": [
                        {"to": [{"podSelector": {"matchLabels": {"role": "server"}}}], "ports": [{"protocol": "TCP", "port": 8080}]},
                        {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}], "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]},
                    ],
                },
            },
        ],
    }


def _existing_namespace(kube: Kube, namespace: str) -> dict | None:
    result = kube.run("get", "namespace", namespace, "--ignore-not-found", "-o", "json")
    if not result.stdout.strip():
        return None
    try:
        existing = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise SmokeError("namespace response is invalid") from error
    if not isinstance(existing, dict) or existing.get("metadata", {}).get("name") != namespace:
        raise SmokeError("namespace response does not match target")
    return existing


def cleanup_namespace(kube: Kube, namespace: str) -> None:
    match = NAMESPACE_PATTERN.fullmatch(namespace)
    if not match:
        raise SmokeError("cleanup is limited to smoke namespaces")
    existing = _existing_namespace(kube, namespace)
    if existing is None:
        return
    if existing.get("metadata", {}).get("labels", {}).get(OWNERSHIP_LABEL) != match.group(1):
        raise SmokeError("namespace ownership label does not match; cleanup refused")
    kube.run("delete", "namespace", namespace, "--wait=true", "--timeout=120s", timeout=130)
    if _existing_namespace(kube, namespace) is not None:
        raise SmokeError("temporary namespace still exists after cleanup")


def _pod_ip(kube: Kube, namespace: str) -> str:
    result = kube.run("get", "pod/server", "-n", namespace, "-o", "json")
    try:
        pod_ip = json.loads(result.stdout)["status"]["podIP"]
        ipaddress.ip_address(pod_ip)
    except (KeyError, TypeError, ValueError) as error:
        raise SmokeError("server pod IP is unavailable") from error
    return pod_ip


def _probe_http(kube: Kube, namespace: str, pod: str, ip: str) -> bool:
    host = f"[{ip}]" if ipaddress.ip_address(ip).version == 6 else ip
    result = kube.run("exec", pod, "-n", namespace, "--", "wget", "-q", "-T", "2", "-O", "-", f"http://{host}:8080/", check=False, timeout=12)
    return result.returncode == 0 and result.stdout.strip() == "ok"


def _probe_dns(kube: Kube, namespace: str, pod: str) -> bool:
    result = kube.run("exec", pod, "-n", namespace, "--", "nslookup", DNS_NAME, check=False, timeout=12)
    return result.returncode == 0


def _expect_probe(probe, expected: bool, name: str, observation_seconds: int) -> None:
    deadline = time.monotonic() + observation_seconds
    while True:
        if probe() is expected:
            return
        if time.monotonic() >= deadline:
            raise SmokeError(f"network policy observation failed: {name}")
        time.sleep(2)


def run_smoke(kube: Kube, run_id: str, *, observation_seconds: int = 30) -> None:
    namespace = namespace_name(run_id)
    if kube.current_context() != kube.context:
        raise SmokeError("current Kubernetes context differs from the explicit target")
    if _existing_namespace(kube, namespace) is not None:
        raise SmokeError("temporary namespace name already exists")

    creation_attempted = False
    failure: BaseException | None = None
    try:
        creation_attempted = True
        kube.run("create", "-f", "-", input_text=json.dumps(build_namespace(run_id)))
        kube.run("create", "-f", "-", "-n", namespace, input_text=json.dumps(build_pods(namespace)))
        kube.run("wait", "-n", namespace, "--for=condition=Ready", "pod/server", "pod/allowed", "pod/blocked", "--timeout=120s", timeout=130)
        ip = _pod_ip(kube, namespace)

        for pod in ("allowed", "blocked"):
            _expect_probe(lambda pod=pod: _probe_http(kube, namespace, pod, ip), True, f"baseline HTTP from {pod}", observation_seconds)
            _expect_probe(lambda pod=pod: _probe_dns(kube, namespace, pod), True, f"baseline DNS from {pod}", observation_seconds)
        print("networkpolicy_smoke_baseline=PASS")

        kube.run("apply", "-f", "-", "-n", namespace, input_text=json.dumps(build_deny_policy(namespace)))
        _expect_probe(lambda: _probe_http(kube, namespace, "server", "127.0.0.1"), True, "server local health after deny", observation_seconds)
        for pod in ("allowed", "blocked"):
            kube.run("exec", pod, "-n", namespace, "--", "true")
            _expect_probe(lambda pod=pod: _probe_http(kube, namespace, pod, ip), False, f"denied HTTP from {pod}", observation_seconds)
            _expect_probe(lambda pod=pod: _probe_dns(kube, namespace, pod), False, f"denied DNS from {pod}", observation_seconds)
        print("networkpolicy_smoke_deny=PASS")

        kube.run("apply", "-f", "-", "-n", namespace, input_text=json.dumps(build_allow_policies(namespace)))
        _expect_probe(lambda: _probe_http(kube, namespace, "allowed", ip), True, "selected HTTP allow", observation_seconds)
        _expect_probe(lambda: _probe_http(kube, namespace, "blocked", ip), False, "blocked HTTP stays denied", observation_seconds)
        _expect_probe(lambda: _probe_dns(kube, namespace, "allowed"), True, "selected DNS allow", observation_seconds)
        _expect_probe(lambda: _probe_dns(kube, namespace, "blocked"), False, "blocked DNS stays denied", observation_seconds)
        print("networkpolicy_smoke_selective_allow=PASS")
    except BaseException as error:
        failure = error
    finally:
        if creation_attempted:
            try:
                cleanup_namespace(kube, namespace)
                print("networkpolicy_smoke_cleanup=PASS")
            except SmokeError as cleanup_error:
                if failure is None:
                    failure = cleanup_error
                else:
                    failure = SmokeError(f"{failure}; cleanup failed: {cleanup_error}")
    if failure is not None:
        raise failure


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--render", action="store_true", help="print disposable resources without contacting a cluster")
    mode.add_argument("--go", action="store_true", help="run isolated smoke against the explicit current context")
    mode.add_argument("--cleanup", metavar="NAMESPACE", help="remove a labeled leftover smoke namespace")
    parser.add_argument("--context", help="must match the current Kubernetes context")
    args = parser.parse_args(argv)
    if args.render:
        run_id = "0123456789ab"
        namespace = namespace_name(run_id)
        print(json.dumps([build_namespace(run_id), build_pods(namespace), build_deny_policy(namespace), build_allow_policies(namespace)], indent=2))
        return 0
    if not args.context:
        parser.error("--context is required for cluster access")
    try:
        kube = Kube(args.context)
        if args.cleanup:
            if kube.current_context() != kube.context:
                raise SmokeError("current Kubernetes context differs from the explicit target")
            cleanup_namespace(kube, args.cleanup)
        else:
            run_id = secrets.token_hex(6)
            print(f"networkpolicy_smoke_namespace={namespace_name(run_id)}")
            run_smoke(kube, run_id)
    except (SmokeError, KeyboardInterrupt) as error:
        print(f"networkpolicy_smoke=FAIL reason={error}", file=sys.stderr)
        if args.go:
            print("If cleanup failed, rerun --cleanup with the printed namespace and explicit context.", file=sys.stderr)
        return 1
    print("networkpolicy_smoke=PASS")
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SmokeError("interrupted")))
    raise SystemExit(main())
