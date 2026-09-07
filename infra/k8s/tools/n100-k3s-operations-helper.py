#!/usr/bin/python3
"""Root-owned, narrowly scoped K3s operations for the N100 runner."""

from __future__ import annotations

import subprocess
import sys

import yaml


K3S = "/usr/local/bin/k3s"
ALLOWED = {"diagnose", "verify_news_observability", "apply_news_observability"}
IDENTITIES = (
    ("monitoring.coreos.com/v1", "ServiceMonitor", "monitoring", "crawler-news-observability"),
    ("monitoring.coreos.com/v1", "PrometheusRule", "monitoring", "sre-telegram-k3s-alerts"),
)


def run_kubectl(*args: str, payload: bytes | None = None) -> bool:
    completed = subprocess.run(
        [K3S, "kubectl", *args],
        input=payload,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def run_diagnose() -> bool:
    return run_kubectl("get", "nodes", "--no-headers") and run_kubectl(
        "-n", "personal-server", "get", "deployment/portal-web"
    )


def run_verify() -> bool:
    secret = subprocess.run(
        [
            K3S,
            "kubectl",
            "-n",
            "monitoring",
            "get",
            "secret",
            "crawler-news-metrics",
            "-o",
            "jsonpath={.data.bearer_token}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    token_present = secret.returncode == 0 and bool(secret.stdout.strip())
    del secret
    return (
        token_present
        and run_kubectl("-n", "monitoring", "get", "servicemonitor", "crawler-news-observability")
        and run_kubectl("-n", "monitoring", "get", "prometheusrule", "sre-telegram-k3s-alerts")
    )


def canonical_documents(raw: bytes) -> bytes | None:
    try:
        documents = list(yaml.safe_load_all(raw))
    except yaml.YAMLError:
        return None
    if len(documents) != 2 or any(not isinstance(document, dict) for document in documents):
        return None
    if any(document.get("kind") == "List" for document in documents):
        return None
    actual = tuple(
        (
            document.get("apiVersion"),
            document.get("kind"),
            document.get("metadata", {}).get("namespace")
            if isinstance(document.get("metadata"), dict)
            else None,
            document.get("metadata", {}).get("name")
            if isinstance(document.get("metadata"), dict)
            else None,
        )
        for document in documents
    )
    if actual != IDENTITIES:
        return None
    try:
        return b"---\n".join(
            yaml.safe_dump(document, sort_keys=True, default_flow_style=False).encode("utf-8")
            for document in documents
        )
    except yaml.YAMLError:
        return None


def run_apply_from_bytes(raw: bytes) -> bool:
    canonical = canonical_documents(raw)
    if canonical is None:
        return False
    return run_kubectl("apply", "--dry-run=client", "-f", "-", payload=canonical) and run_kubectl(
        "apply", "-f", "-", payload=canonical
    )


def run_apply() -> bool:
    return run_apply_from_bytes(sys.stdin.buffer.read())


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ALLOWED:
        return 2
    operation = argv[1]
    if operation == "diagnose":
        return 0 if run_diagnose() else 1
    if operation == "verify_news_observability":
        return 0 if run_verify() else 1
    return 0 if run_apply() else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise SystemExit(1)
