#!/usr/bin/env python3
"""Supply existing canonical HomeOps auth to a bounded Compose child.

No credentials are written, generated, rotated or printed. Kubernetes reads do
not depend on Portal readiness, so this also supports bootstrap recovery.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

KEY = "HOMEOPS_EXECUTOR_SHARED_SECRET"
SERVICES = {"homeops-executor", "system-agent", "car-care-worker", "crawler-worker", "youtube-memo", "book-memo", "caddy"}


class SupplyError(Exception):
    """Fixed safe error code only; never external output or payload."""


def validate_command(command):
    if command[:2] != ["docker", "compose"] or any(not isinstance(x, str) or not x or any(c in x for c in "\n\r\x00") for x in command):
        raise SupplyError("compose_command_invalid")
    index = 2
    while index < len(command) and command[index] in {"-f", "--file", "-p", "--project-name", "--project-directory", "--profile"}:
        if index + 1 >= len(command) or command[index + 1].startswith("-"):
            raise SupplyError("compose_command_invalid")
        index += 2
    tail = command[index:]
    if tail == ["config", "--quiet"]:
        return
    if not tail or tail[0] != "up":
        raise SupplyError("compose_command_invalid")
    index = 1
    flags = set()
    while index < len(tail) and tail[index].startswith("-"):
        flag = tail[index]
        if flag in flags:
            raise SupplyError("compose_command_invalid")
        flags.add(flag)
        if flag == "--pull":
            if index + 1 >= len(tail) or tail[index + 1] != "never":
                raise SupplyError("compose_command_invalid")
            index += 2
        elif flag in {"-d", "--build", "--no-build", "--no-deps", "--force-recreate"}:
            index += 1
        else:
            raise SupplyError("compose_command_invalid")
    services = tail[index:]
    if ("-d" not in flags or {"--build", "--no-build"} <= flags
            or not services or "homeops-executor" not in services
            or len(services) != len(set(services)) or not set(services) <= SERVICES):
        raise SupplyError("compose_command_invalid")


def run(command, environment, timeout=None):
    try:
        result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise SupplyError("command_unavailable_or_timeout") from None
    if result.returncode:
        raise SupplyError("command_failed")
    return result.stdout


def canonical_auth(environment):
    query_env = dict(environment)
    query_env.pop(KEY, None)
    prefix = ["sudo", "-n", "k3s", "kubectl", "-n", "personal-server", "--request-timeout=20s", "get"]
    try:
        deployment = json.loads(run(prefix + ["deployment", "portal-web", "-o", "json"], query_env, 30))
        containers = deployment["spec"]["template"]["spec"]["containers"]
        if len(containers) != 1:
            raise ValueError()
        refs = [x["secretRef"]["name"] for x in containers[0].get("envFrom", []) if "secretRef" in x]
        if len(refs) != 1 or not isinstance(refs[0], str) or not refs[0] or refs[0].startswith("-"):
            raise ValueError()
        secret = json.loads(run(prefix + ["secret", refs[0], "-o", "json"], query_env, 30))
        path = Path(__file__).resolve().parents[1] / "infra/k8s/tools/homeops-auth-contract.py"
        spec = importlib.util.spec_from_file_location("homeops_auth_contract", path)
        contract = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(contract)
        name = contract.validate_secret_source(deployment, secret)
        if not any(entry.get("name") == KEY for entry in containers[0].get("env", [])):
            raise ValueError()
        if any(obj.get("metadata", {}).get("namespace", "personal-server") != "personal-server" for obj in (deployment, secret)):
            raise ValueError()
        if name != refs[0]:
            raise ValueError()
        value = base64.b64decode(secret["data"][KEY], validate=True).decode("ascii")
        if len(value) < 32 or any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise ValueError()
    except (OSError, ImportError, SyntaxError):
        raise SupplyError("auth_contract_unavailable") from None
    except (ValueError, KeyError, TypeError, IndexError, UnicodeError, AttributeError):
        raise SupplyError("canonical_auth_invalid") from None
    if environment.get(KEY) and environment[KEY] != value:
        raise SupplyError("inherited_auth_mismatch")
    return value


def main(arguments=None):
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    try:
        if len(arguments) < 4 or arguments[0] != "--runtime-mode" or arguments[1] not in {"cutover", "k3s"} or arguments[2] != "--":
            raise SupplyError("invocation_invalid")
        command = arguments[3:]
        validate_command(command)
        environment = dict(os.environ)
        auth = canonical_auth(environment)
        if any(auth in arg for arg in command):
            raise SupplyError("compose_command_invalid")
        environment[KEY] = auth
        run(command, environment)
    except SupplyError as error:
        print("homeops_auth_supply=FAIL reason=" + str(error), file=sys.stderr)
        return 1
    print("homeops_auth_supply=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
