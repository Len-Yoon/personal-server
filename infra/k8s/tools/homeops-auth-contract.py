"""Pure, fail-closed contracts for HomeOps shared-auth configuration.

This module never reads credentials, runs commands, or mutates its inputs.
"""
from copy import deepcopy
import hashlib
import json


KEY = "HOMEOPS_EXECUTOR_SHARED_SECRET"
ENV_PATH = "/spec/template/spec/containers/0/env"


def _safe_string(value, label):
    if not isinstance(value, str) or not value.strip() or any(c in value for c in ("\n", "\r", "\x00")):
        raise ValueError(label + " is missing or invalid")
    return value


def _cas(obj):
    metadata = obj.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("object metadata is invalid")
    return [{"op": "test", "path": "/metadata/" + key,
             "value": _safe_string(metadata.get(key), "object identity")}
            for key in ("uid", "resourceVersion")]


def _portal_container(deployment):
    _cas(deployment)
    try:
        containers = deployment["spec"]["template"]["spec"]["containers"]
    except (KeyError, TypeError):
        raise ValueError("deployment containers are missing") from None
    if not isinstance(containers, list) or len(containers) != 1 or not isinstance(containers[0], dict):
        raise ValueError("deployment must have exactly one container")
    return containers[0]


def _auth_env(container, secret_name):
    env = container.get("env", [])
    if not isinstance(env, list):
        raise ValueError("deployment environment is invalid")
    names = set()
    found = False
    for entry in env:
        if not isinstance(entry, dict):
            raise ValueError("deployment environment entry is invalid")
        name = _safe_string(entry.get("name"), "environment name")
        if name in names:
            raise ValueError("duplicate environment key")
        names.add(name)
        if name != KEY:
            continue
        value_from = entry.get("valueFrom")
        ref = value_from.get("secretKeyRef") if isinstance(value_from, dict) else None
        if ("value" in entry or not isinstance(ref, dict)
                or set(value_from) != {"secretKeyRef"}
                or ref.get("name") != secret_name or ref.get("key") != KEY
                or ref.get("optional", False) is not False):
            raise ValueError("existing authentication reference is invalid")
        found = True
    return found


def _secret_data(secret):
    _cas(secret)
    if secret.get("immutable", False) is not False:
        raise ValueError("Secret must be mutable")
    data = secret.get("data")
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise ValueError("Secret data is missing or invalid")
    return data


def validate_secret_source(deployment: dict, secret: dict) -> str:
    """Resolve one unprefixed, required envFrom Secret and validate identity."""
    container = _portal_container(deployment)
    sources = container.get("envFrom", [])
    if not isinstance(sources, list) or any(not isinstance(s, dict) for s in sources):
        raise ValueError("deployment environment sources are invalid")
    refs = [s for s in sources if "secretRef" in s]
    if len(refs) != 1:
        raise ValueError("deployment must reference exactly one envFrom Secret")
    source = refs[0]
    ref = source["secretRef"]
    if not isinstance(ref, dict) or source.get("prefix", "") or ref.get("optional", False) is not False:
        raise ValueError("envFrom Secret reference is invalid")
    name = _safe_string(ref.get("name"), "Secret name")
    _secret_data(secret)
    if secret["metadata"].get("name") != name:
        raise ValueError("Secret identity does not match deployment reference")
    _auth_env(container, name)
    return name


def secret_patch(secret: dict, encoded_value: str) -> list[dict]:
    """Add missing auth data under UID/version CAS; never overwrite a key."""
    data = _secret_data(secret)
    if KEY in data:
        return []
    _safe_string(encoded_value, "encoded authentication value")
    return _cas(secret) + [{"op": "add", "path": "/data/" + KEY, "value": encoded_value}]


def portal_patch(deployment: dict, secret_name: str) -> list[dict]:
    """Append exactly one required Secret reference without replacing env."""
    _safe_string(secret_name, "Secret name")
    container = _portal_container(deployment)
    if _auth_env(container, secret_name):
        return []
    patch = _cas(deployment)
    if "env" in container:
        patch.append({"op": "test", "path": ENV_PATH, "value": deepcopy(container["env"])})
    else:
        patch.append({"op": "add", "path": ENV_PATH, "value": []})
    patch.append({"op": "add", "path": ENV_PATH + "/-", "value": {
        "name": KEY, "valueFrom": {"secretKeyRef": {"name": secret_name, "key": KEY, "optional": False}}}})
    return patch


def fingerprint(container: dict, ignore_auth: bool = False) -> dict[str, str]:
    """Hash preserved Docker configuration after order-only normalization."""
    config = deepcopy(container.get("Config", {}))
    host = deepcopy(container.get("HostConfig", {}))
    mounts = deepcopy(container.get("Mounts", []))
    for key in ("Image", "Hostname", "Labels"):
        config.pop(key, None)
    env = config.get("Env", [])
    if not isinstance(env, list) or any(not isinstance(e, str) for e in env):
        raise ValueError("Docker environment is invalid")
    names = [e.split("=", 1)[0] for e in env]
    if len(names) != len(set(names)):
        raise ValueError("duplicate Docker environment key")
    if "Env" in config:
        config["Env"] = sorted(e for e in env if not ignore_auth or e.split("=", 1)[0] != KEY)
    if isinstance(host.get("Binds"), list):
        host["Binds"].sort()
    bindings = host.get("PortBindings")
    if isinstance(bindings, dict):
        for key, values in bindings.items():
            if isinstance(values, list):
                bindings[key] = sorted(values, key=lambda value: json.dumps(value, sort_keys=True))
    if isinstance(mounts, list):
        mounts.sort(key=lambda value: value["Destination"])
    networks = {}
    for name, network in _networks(container).items():
        aliases = network.get("Aliases")
        if aliases is not None and (not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases)):
            raise ValueError("Docker network aliases are invalid")
        networks[name] = {"Aliases": sorted(aliases) if aliases is not None else None,
                          "IPAMConfig": deepcopy(network.get("IPAMConfig")),
                          "Links": deepcopy(network.get("Links"))}
    return {name: hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
            for name, value in (("Config", config), ("HostConfig", host), ("Mounts", mounts), ("Networks", networks))}


def _networks(container):
    settings = container.get("NetworkSettings", {})
    if not isinstance(settings, dict):
        raise ValueError("Docker network settings are invalid")
    networks = settings.get("Networks", {})
    if not isinstance(networks, dict) or any(not isinstance(n, str) or not isinstance(v, dict) for n, v in networks.items()):
        raise ValueError("Docker networks are invalid")
    return networks


def assert_compose_networks(configuration: dict, container: dict) -> None:
    """Reject network membership, static address, or alias changes before up."""
    try:
        service = configuration["services"]["homeops-executor"]
        configured_networks = service["networks"]
        definitions = configuration["networks"]
    except (KeyError, TypeError):
        raise ValueError("Compose executor networks are missing") from None
    if not isinstance(configured_networks, dict) or not isinstance(definitions, dict):
        raise ValueError("Compose executor networks are invalid")
    current = _networks(container)
    resolved = {}
    for logical_name, options in configured_networks.items():
        definition = definitions.get(logical_name)
        if not isinstance(definition, dict):
            raise ValueError("Compose network definition is missing")
        actual_name = _safe_string(definition.get("name"), "Compose network name")
        if actual_name in resolved or (options is not None and not isinstance(options, dict)):
            raise ValueError("Compose network mapping is ambiguous")
        resolved[actual_name] = options or {}
    if set(resolved) != set(current):
        raise ValueError("Compose network membership differs from running executor")
    for name, options in resolved.items():
        network = current[name]
        ipam = network.get("IPAMConfig") or {}
        if not isinstance(ipam, dict):
            raise ValueError("Docker static network configuration is invalid")
        for compose_key, docker_key in (("ipv4_address", "IPv4Address"), ("ipv6_address", "IPv6Address")):
            if options.get(compose_key) != ipam.get(docker_key):
                raise ValueError("Compose static network address differs from running executor")
        configured_aliases = options.get("aliases", [])
        current_aliases = network.get("Aliases") or []
        if (not isinstance(configured_aliases, list) or not isinstance(current_aliases, list)
                or any(not isinstance(a, str) for a in configured_aliases + current_aliases)):
            raise ValueError("network aliases are invalid")
        automatic_aliases = {"homeops-executor"}
        if service.get("container_name"):
            automatic_aliases.add(_safe_string(service["container_name"], "Compose container name"))
        configured_set = set(configured_aliases)
        current_set = set(current_aliases)
        if not configured_set <= current_set or not current_set <= configured_set | automatic_aliases:
            raise ValueError("Compose aliases differ from running executor")


def compose_invocation(container: dict, repo: str, secret: str, bridge: str) -> tuple[list[str], dict[str, str]]:
    """Reuse actual Compose ownership and file order; recreate executor only."""
    _safe_string(repo, "repository directory")
    _safe_string(secret, "authentication value")
    _safe_string(bridge, "bridge gateway")
    labels = container.get("Config", {}).get("Labels", {})
    if not isinstance(labels, dict):
        raise ValueError("Compose ownership labels are missing")
    project = _safe_string(labels.get("com.docker.compose.project"), "Compose project")
    files = _safe_string(labels.get("com.docker.compose.project.config_files"), "Compose files").split(",")
    argv = ["docker", "compose", "--project-directory", repo, "-p", project]
    for path in files:
        argv.extend(["-f", _safe_string(path, "Compose file")])
    argv.extend(["up", "-d", "--no-build", "--no-deps", "--pull", "never", "homeops-executor"])
    return argv, {KEY: secret, "DOCKER_BRIDGE_GATEWAY": bridge}
