#!/usr/bin/env python3
"""Bounded, explicitly reviewed Caddy-only ingress transition.

No credentials or complete runtime configuration are written to the journal.
The existing three Compose files remain the persistent source. An external
bridge is created once; the shared default bridge is never recreated.
Unknown writes are read back, never repeated. An unfinished journal blocks a
new forward run; only explicit, identity-checked rollback can recover it.
"""
import argparse
import copy
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

COMPOSE_FILES = ("docker-compose.yml", "docker-compose.n100.yml", "docker-compose.portal-bridge.yml")
OWNER_LABEL = "pe.kr.len.caddy-ingress-owner"
NETWORK_KEY = "caddy_ingress"
ENV_IP = "CADDY_INGRESS_FIXED_IP"
ENV_PORTAL = "CADDY_PORTAL_CLUSTER_UPSTREAM"
KUBE = ["sudo", "-n", "k3s", "kubectl", "--request-timeout=15s"]
SHA = re.compile(r"[0-9a-f]{64}\Z")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
NAME = re.compile(r"[a-z][a-z0-9_-]{0,62}\Z")


class Blocked(Exception):
    """Fixed reason codes only: command output may contain credentials."""


class UnknownWrite(Blocked):
    pass


class CommandFailed(Blocked):
    """A command returned a known nonzero completion, rather than timing out."""


def require(condition, code):
    if not condition:
        raise Blocked(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def hash_object(value):
    return digest(canonical(value))


def select_subnet(collisions):
    """Choose a /29 from the reviewed private range, excluding all known routes."""
    require(isinstance(collisions, list) and collisions, "collision_inventory_empty")
    blocked = []
    for value in collisions:
        try:
            network = ipaddress.ip_network(value, strict=False)
        except (ValueError, TypeError):
            raise Blocked("collision_inventory_invalid") from None
        if network.version == 4 and network.prefixlen != 0:
            blocked.append(network)
    for candidate in ipaddress.ip_network("10.240.0.0/16").subnets(new_prefix=29):
        if not any(candidate.overlaps(value) for value in blocked):
            return {"subnet": str(candidate), "gateway": str(candidate.network_address + 1),
                    "fixed_ip": str(candidate.network_address + 2),
                    "ip_range": str(list(candidate.subnets(new_prefix=30))[-1])}
    raise Blocked("no_collision_free_subnet")


def validate_address_spec(spec):
    try:
        subnet = ipaddress.ip_network(spec["subnet"], strict=True)
        dynamic = ipaddress.ip_network(spec["ip_range"], strict=True)
        fixed, gateway = (ipaddress.ip_address(spec[k]) for k in ("fixed_ip", "gateway"))
        require(subnet.version == 4 and subnet.prefixlen == 29
                and subnet.subnet_of(ipaddress.ip_network("10.240.0.0/16"))
                and dynamic.prefixlen == 30 and dynamic.subnet_of(subnet)
                and dynamic.network_address == subnet.network_address + 4
                and fixed == subnet.network_address + 2 and gateway == subnet.network_address + 1
                and fixed not in dynamic, "network_address_bounds")
    except (ValueError, KeyError, TypeError):
        raise Blocked("network_address_invalid") from None


def managed_blocks(network_name):
    require(isinstance(network_name, str) and NAME.fullmatch(network_name), "network_name")
    service = ("    # BEGIN owned Caddy ingress transition\n"
               "    networks:\n"
               "      default:\n"
               "        gw_priority: 0\n"
               f"      {NETWORK_KEY}:\n"
               f"        ipv4_address: ${{{ENV_IP}:?required}}\n"
               "        gw_priority: 1\n"
               "    # END owned Caddy ingress transition\n").encode()
    environment = ("      # BEGIN owned Caddy Portal upstream\n"
                   f"      PORTAL_UPSTREAM: ${{{ENV_PORTAL}:?required}}\n"
                   "      # END owned Caddy Portal upstream\n").encode()
    network = ("\n# BEGIN owned Caddy external network\n"
               "networks:\n"
               f"  {NETWORK_KEY}:\n"
               "    external: true\n"
               f"    name: {network_name}\n"
               "# END owned Caddy external network\n").encode()
    return service, environment, network


def edit_bridge(raw, network_name):
    """Insert only owned lines. Unusual/ambiguous existing YAML fails closed."""
    require(b"\r" not in raw and raw.endswith(b"\n") and b"\t" not in raw, "bridge_line_format")
    require(raw.count(b"services:\n") == 1 and b"  caddy:\n" not in raw,
            "bridge_service_shape")
    require(not any(word in raw for word in (b"BEGIN owned Caddy", b"networks:",
                ENV_IP.encode(), ENV_PORTAL.encode())), "bridge_existing_override")
    # This ignored runtime override contains service port bindings only. Reject
    # every other key before making an atomic replacement of this file.
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith(b"#") or line == b"services:":
            continue
        require(re.fullmatch(rb"  [a-z][a-z0-9_-]*:", line) is not None
                or re.fullmatch(rb"    ports:(?:\s*!override|\s*!reset\s*\[\]|\s*\[\])?", line) is not None
                or re.fullmatch(rb"      - [\"']?[0-9.:/a-z-]+[\"']?", line) is not None,
                "bridge_noncredential_shape")
    lines = raw.splitlines(keepends=True)
    index = lines.index(b"services:\n")
    end = next((n for n in range(index + 1, len(lines))
                if lines[n].strip() and not lines[n].startswith(b" ")
                and not lines[n].lstrip().startswith(b"#")), len(lines))
    require(end == len(lines), "bridge_services_must_end_file")
    service, environment, network = managed_blocks(network_name)
    caddy = b"  caddy:\n" + service + b"    environment:\n" + environment
    lines.insert(end, caddy)
    return b"".join(lines) + network


def restore_bridge(raw, network_name, expected_original_sha256):
    service, environment, network = managed_blocks(network_name)
    for block in (b"  caddy:\n" + service + b"    environment:\n" + environment, network):
        require(raw.count(block) == 1, "bridge_owned_block_missing")
        raw = raw.replace(block, b"", 1)
    require(digest(raw) == expected_original_sha256, "bridge_original_hash")
    return raw


def inverse_bridge(current, original_size, original_sha256, network_name):
    require(len(current) >= original_size and digest(current[:original_size]) == original_sha256,
            "bridge_original_prefix")
    original = current[:original_size]
    suffix = edit_bridge(original, network_name)[original_size:]
    tail = current[original_size:]
    require(suffix.startswith(tail) and len(tail) <= len(suffix), "bridge_foreign_suffix")
    return original


def env_suffix(env, spec, upstream):
    require(not re.search(rb"(?m)^\s*(?:export\s+)?(?:" + ENV_IP.encode() + b"|"
                          + ENV_PORTAL.encode() + rb")\s*=", env), "env_parameter_exists")
    prefix = b"" if not env or env.endswith(b"\n") else b"\n"
    return prefix + (f"{ENV_IP}={spec['fixed_ip']}\n{ENV_PORTAL}={upstream}\n").encode()


def inverse_env(current, original_size, original_sha256, suffix):
    """Also recognize an interrupted append; never truncate a foreign suffix."""
    require(len(current) >= original_size and digest(current[:original_size]) == original_sha256,
            "env_original_prefix")
    tail = current[original_size:]
    require(suffix.startswith(tail) and len(tail) <= len(suffix), "env_foreign_suffix")
    return current[:original_size]


def config_contract(baseline, target, network_name, spec, upstream):
    """Only Caddy network membership and Portal upstream may differ."""
    old, new = copy.deepcopy(baseline), copy.deepcopy(target)
    require(old.get("services", {}).keys() == new.get("services", {}).keys(), "compose_services_changed")
    caddy = new["services"]["caddy"]
    networks = caddy.get("networks", {})
    require(set(networks) == {"default", NETWORK_KEY} and networks["default"] in ({}, {"gw_priority": 0})
            and networks[NETWORK_KEY] == {"ipv4_address": spec["fixed_ip"], "gw_priority": 1}, "compose_caddy_networks")
    require(caddy.get("environment", {}).get("PORTAL_UPSTREAM") == upstream, "compose_portal_upstream")
    old_caddy = old["services"]["caddy"]
    require(set(old_caddy.get("networks", {})) == {"default"}, "compose_baseline_networks")
    caddy["networks"] = old_caddy["networks"]
    caddy["environment"]["PORTAL_UPSTREAM"] = old_caddy["environment"]["PORTAL_UPSTREAM"]
    require(new.get("networks", {}).pop(NETWORK_KEY, None) ==
            {"external": True, "name": network_name}, "compose_external_network")
    require(old == new, "compose_unapproved_change")


def image_override_contract(approved, effective, image_id):
    require(IMAGE.fullmatch(image_id), "immutable_image_id")
    expected = copy.deepcopy(approved)
    expected["services"]["caddy"]["image"] = image_id
    require(effective == expected, "compose_image_override_changed_model")


def git_config_guard(raw):
    try:
        entries = raw.decode().split("\0")
    except UnicodeError:
        raise Blocked("git_config_encoding") from None
    helpers = []
    for entry in entries:
        if not entry:
            continue
        key, separator, value = entry.partition("\n")
        key = key.lower()
        require(separator, "git_config_shape")
        if key == "credential.helper":
            helpers.append(value)
            continue
        forbidden = (key.startswith(("include.", "includeif.", "filter.", "gpg.", "http.", "credential."))
            or key in ("core.hookspath", "core.sshcommand", "core.worktree", "core.gitproxy",
                       "core.alternaterefscommand", "diff.external", "interactive.difffilter", "extensions.partialclone")
            or key == "core.askpass" and value != ""
            or key.startswith("url.") and key.endswith((".insteadof", ".pushinsteadof"))
            or key.startswith("remote.") and key.endswith((".promisor", ".partialclonefilter"))
            or key in ("commit.gpgsign", "merge.verifysignatures") and value.lower() not in ("false", "0")
            or key.startswith("diff.") and key.endswith((".command", ".textconv"))
            or key.startswith("merge.") and key.endswith(".driver")
            or key == "core.attributesfile" and value != "/dev/null"
            or key == "core.fsmonitor" and value.lower() not in ("false", "0"))
        require(not forbidden, "git_external_config")
    require(helpers and helpers[-1] == "", "git_credential_reset_required")


def isolated_git_env(environ):
    return {**{key: value for key, value in environ.items()
               if not key.upper().startswith("GIT_") and key.upper() != "SSH_ASKPASS"},
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0"}


def network_spec(network):
    """Pin immutable Docker network semantics without volatile endpoint state."""
    keys = ("Name", "Driver", "Scope", "Internal", "Attachable", "Ingress", "EnableIPv4", "EnableIPv6",
            "ConfigFrom", "ConfigOnly", "IPAM", "Options", "Labels")
    return {key: network.get(key) for key in keys}


def caddy_runtime_contract(container, prepared=None):
    """Normalize only Docker/Compose identity and the approved Portal value."""
    config = copy.deepcopy(container["Config"])
    if prepared is not None:
        require(container["Image"] == prepared["image_id"], "caddy_actual_image_changed")
        reference = config.get("Image")
        require(isinstance(reference, str) and (reference == prepared["image_id"]
                or digest(reference.encode()) == prepared["baseline_image_reference_sha256"]), "caddy_image_reference_changed")
        config["Image"] = "<approved-immutable-image>"
    require(isinstance(config.get("Env"), list), "caddy_environment_shape")
    config["Env"] = sorted(item for item in config["Env"] if not item.startswith("PORTAL_UPSTREAM="))
    if config.get("Hostname") == container["Id"][:12]:
        config["Hostname"] = "<docker-generated>"
    labels = config.get("Labels") or {}
    labels.pop("com.docker.compose.config-hash", None)
    labels.pop("com.docker.compose.replace", None)
    labels.pop("com.docker.compose.version", None)
    if prepared is not None:
        files = labels.get("com.docker.compose.project.config_files")
        if files is not None:
            require(digest(files.encode()) in (prepared["baseline_compose_config_files_sha256"],
                    prepared["pinned_compose_config_files_sha256"]), "caddy_config_files_changed")
            labels["com.docker.compose.project.config_files"] = "<approved-config-files>"
        else:
            require(prepared["baseline_compose_config_files_sha256"] is None, "caddy_config_files_missing")
    host = copy.deepcopy(container["HostConfig"])
    # Endpoint membership is independently checked by network ID and exact
    # address. Compose chooses NetworkMode from the first attached network.
    host.pop("NetworkMode", None)
    return {"config_sha256": hash_object(config), "hostconfig_sha256": hash_object(host),
            "mounts_sha256": hash_object(container["Mounts"])}


def generated_identity(container, prepared, allowed_replaced=()):
    labels = container["Config"].get("Labels") or {}
    baseline = digest(container["Id"].encode()) == prepared["caddy_id_sha256"]
    version = labels.get("com.docker.compose.version")
    expected_version = prepared["baseline_compose_version_sha256"] if baseline else prepared["compose_version_sha256"]
    require((digest(version.encode()) if version is not None else None) == expected_version,
            "caddy_generated_compose_version")
    replaced = labels.get("com.docker.compose.replace")
    if replaced is not None:
        replaced_hash = digest(replaced.encode())
        allowed = set(allowed_replaced) | {prepared["caddy_id_sha256"]}
        if baseline:
            allowed.add(prepared["baseline_replace_sha256"])
        require(replaced_hash in allowed, "caddy_generated_replace_identity")
    if "com.docker.compose.image" in labels:
        require(labels["com.docker.compose.image"] == prepared["image_id"], "caddy_generated_image_identity")


def assert_owned_network(network, prepared, *, empty=False, id_sha256=None):
    spec = prepared["address"]
    require(network.get("Name") == prepared["network_name"]
            and network.get("Driver") == "bridge" and network.get("Scope") == "local"
            and network.get("Internal") is False and network.get("Ingress") is False
            and network.get("EnableIPv6") is False and network.get("Attachable") is False
            and network.get("EnableIPv4") is True and network.get("ConfigOnly") is False
            and network.get("ConfigFrom") == {"Network": ""}
            and network.get("Labels") == {OWNER_LABEL: prepared["owner_id"]}
            and network.get("Options", {}) == {}
            and network.get("IPAM", {}).get("Driver") == "default"
            and network.get("IPAM", {}).get("Options") in ({}, None)
            and network.get("IPAM", {}).get("Config") == [{"Subnet": spec["subnet"],
                "IPRange": spec["ip_range"], "Gateway": spec["gateway"]}], "owned_network_spec")
    require(isinstance(network.get("Id"), str) and SHA.fullmatch(network["Id"]), "network_identity")
    if id_sha256 is not None:
        require(digest(network["Id"].encode()) == id_sha256, "owned_network_identity")
    if empty:
        require(not network.get("Containers"), "owned_network_not_empty")


def prepare_transition(fresh, bridge, env, plan):
    """Fresh input is in memory only. Returned data includes nonsecret edits."""
    require(fresh["source_commit"] == plan["source_commit"] and fresh["tracked_clean"] is True,
            "source_guard")
    require(fresh["policy_count"] == 0, "policies_present")
    require(fresh["default_network"]["Driver"] == "bridge", "default_network_driver")
    caddy = fresh["caddy"]
    require(IMAGE.fullmatch(caddy["Image"]) and caddy["Image"] == fresh["local_image_id"], "same_local_image")
    require(caddy["State"]["Running"] is True and len(caddy["NetworkSettings"]["Networks"]) == 1,
            "caddy_baseline_state")
    actual_env = dict(item.split("=", 1) for item in caddy["Config"]["Env"])
    require(all(actual_env.get(key) == value for key, value in
                fresh["config"]["services"]["caddy"].get("environment", {}).items()), "caddy_existing_env_drift")
    default_endpoint = next(iter(caddy["NetworkSettings"]["Networks"].values()))
    require(default_endpoint["NetworkID"] == fresh["default_network"]["Id"], "default_network_identity")
    require(not default_endpoint.get("IPAMConfig"), "baseline_static_address")
    require(digest(bridge) == plan["bridge_sha256"] and digest(env) == plan["env_sha256"], "runtime_baseline_hash")
    spec = select_subnet(fresh["collisions"])
    validate_address_spec(spec)
    upstream = fresh["portal_upstream"]
    require(re.fullmatch(r"[0-9.]+:[0-9]+", upstream) is not None, "portal_upstream_shape")
    host, port = upstream.split(":")
    require(ipaddress.ip_address(host).version == 4 and 1 <= int(port) <= 65535, "portal_upstream_value")
    require(NAME.fullmatch(plan["network_name"]) and SHA.fullmatch(plan["owner_id"]), "plan_owner")
    require(all(network["Name"] != plan["network_name"] for network in fresh["networks"]),
            "owned_network_name_exists")
    target_bridge = edit_bridge(bridge, plan["network_name"])
    suffix = env_suffix(env, spec, upstream)
    reference = caddy["Config"].get("Image")
    require(isinstance(reference, str) and reference, "baseline_image_reference")
    config_files = (caddy["Config"].get("Labels") or {}).get("com.docker.compose.project.config_files")
    runtime_identity = {"image_id": caddy["Image"], "baseline_image_reference_sha256": digest(reference.encode()),
                        "baseline_compose_config_files_sha256": digest(config_files.encode()) if config_files is not None else None,
                        "pinned_compose_config_files_sha256": digest((config_files + "," +
                            fresh.get("compose_stdin_config_name", "-")).encode()) if config_files is not None else None}
    projection = {"source_commit": fresh["source_commit"], "network_name": plan["network_name"],
                  "owner_id": plan["owner_id"], "address": spec, "portal_upstream": upstream,
                  "bridge_sha256": digest(bridge), "bridge_size": len(bridge),
                  "target_bridge_sha256": digest(target_bridge), "env_sha256": digest(env),
                  "env_size": len(env), "target_env_sha256": digest(env + suffix),
                  "config_sha256": hash_object(fresh["config"]), **runtime_identity,
                  "compose_project": fresh["config"]["name"],
                  "compose_version_sha256": digest(fresh["compose_version"].encode()),
                  "baseline_compose_version_sha256": digest(caddy["Config"]["Labels"]["com.docker.compose.version"].encode())
                      if "com.docker.compose.version" in caddy["Config"]["Labels"] else None,
                  "baseline_replace_sha256": digest(caddy["Config"]["Labels"]["com.docker.compose.replace"].encode())
                      if "com.docker.compose.replace" in caddy["Config"]["Labels"] else None,
                  "caddy_id_sha256": digest(caddy["Id"].encode()),
                  "default_network_id_sha256": digest(fresh["default_network"]["Id"].encode()),
                  "default_network_sha256": hash_object(network_spec(fresh["default_network"])),
                  "service_routes": fresh["service_routes"],
                  "other_containers_sha256": hash_object(fresh["other_containers"]),
                  "default_endpoints_sha256": hash_object({key: value for key, value in
                      fresh["default_network"].get("Containers", {}).items() if key != caddy["Id"]}),
                  **{"caddy_" + key: value for key, value in caddy_runtime_contract(caddy, runtime_identity).items()}}
    return {**projection, "prepared_sha256": hash_object(projection), "target_bridge": target_bridge,
            "env_suffix": suffix, "baseline_config": fresh["config"], "baseline_caddy": caddy}


def prepared_projection(prepared):
    """Private reviewed plan may contain addresses, never credentials/configs."""
    return {key: value for key, value in prepared.items()
            if key not in ("prepared_sha256", "target_bridge", "env_suffix", "baseline_config", "baseline_caddy")}


def validate_plan(plan, *, mutation=False):
    require(plan.get("schema_version") == 1 and re.fullmatch(r"[0-9a-f]{40}", plan.get("source_commit", "")),
            "plan_schema")
    for key in ("bridge_sha256", "env_sha256", "owner_id"):
        require(isinstance(plan.get(key), str) and SHA.fullmatch(plan[key]), "plan_hash")
    require(isinstance(plan.get("network_name"), str) and NAME.fullmatch(plan["network_name"]), "plan_network")
    for key in ("forward_seconds", "rollback_seconds"):
        value = plan.get("budget", {}).get(key)
        require(type(value) is int and 10 <= value <= 600, "plan_budget")
    if mutation:
        require(plan.get("reviewed") is True and SHA.fullmatch(plan.get("prepared_sha256", "")), "plan_not_reviewed")
        require(plan.get("protected_guard_ack") is True, "protected_guard_required")
        require(isinstance(plan.get("prepared_projection"), dict)
                and hash_object(plan["prepared_projection"]) == plan["prepared_sha256"], "plan_projection")


def runtime_info_guard(path, info, platform_guard=None):
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid(), "unsafe_file")
    if info.st_mode & 0o022:
        # Only a trusted operational wrapper can prove a specific Drvfs mount
        # and fresh Windows ACL. A plan flag never overrides POSIX protection.
        require(callable(platform_guard) and platform_guard(path, info) is True, "unsafe_file")


def safe_read(path, *, private=False, platform_guard=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        runtime_info_guard(path, info, platform_guard if not private else None)
        if private:
            require(stat.S_IMODE(info.st_mode) == 0o600, "private_file_mode")
        with os.fdopen(os.dup(fd), "rb") as source:
            raw = source.read(2 * 1024 * 1024 + 1)
        require(len(raw) <= 2 * 1024 * 1024, "file_size_bound")
        return raw
    finally:
        os.close(fd)


class Journal:
    """Single private JSON document, fsynced before/after every mutation."""
    def __init__(self, directory, root):
        path = Path(directory).absolute()
        require(path != root and root not in path.parents, "journal_inside_repository")
        self.directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(self.directory)
        require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, "unsafe_journal_directory")
        self.fd = os.open("transition.json", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                          0o600, dir_fd=self.directory)
        info = os.fstat(self.fd)
        require(info.st_uid == os.getuid() and stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600, "unsafe_journal")
        raw = os.read(self.fd, 1024 * 1024)
        self.state = json.loads(raw) if raw else {}
        if self.state:
            require(self.state.get("schema_version") == 1, "journal_schema")

    def save(self, **updates):
        target = {**self.state, **updates}
        raw = canonical(target)
        temporary = ".transition-next.json"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.directory)
        installed = False
        try:
            view = memoryview(raw)
            while view:
                count = os.write(fd, view)
                require(count > 0, "journal_short_write")
                view = view[count:]
            os.fsync(fd)
            current = os.stat("transition.json", dir_fd=self.directory, follow_symlinks=False)
            opened = os.fstat(self.fd)
            require(current.st_ino == opened.st_ino and current.st_dev == opened.st_dev, "journal_write_race")
            os.replace(temporary, "transition.json", src_dir_fd=self.directory, dst_dir_fd=self.directory)
            installed = True
            os.fsync(self.directory)
            os.close(self.fd)
            self.fd = os.open("transition.json", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.directory)
            self.state = target
        finally:
            os.close(fd)
            if not installed:
                # Only this invocation's O_EXCL-owned temporary is removed.
                os.unlink(temporary, dir_fd=self.directory)

    def close(self):
        os.close(self.fd)
        os.close(self.directory)


def public_record(prepared):
    # No IPs, image IDs, names, raw configs or credential values enter evidence.
    return {key: value for key, value in prepared.items()
            if key.endswith("sha256") or key in ("bridge_size", "env_size")}


def transition(plan, backend, journal, *, rollback=False):
    """Caller owns one existing sequence flock for the entire transaction."""
    validate_plan(plan, mutation=True)
    require(backend.lock_owned, "sequence_lock_required")
    require(not backend.command_termination_unknown, "child_termination_unknown")
    require(not journal.state.get("child_termination_unknown", False), "persisted_child_termination_unknown")
    if rollback:
        return rollback_transition(plan, backend, journal)
    require(not journal.state, "unfinished_or_completed_journal")
    backend.phase_deadline(False)
    backend.guard()
    fresh = backend.snapshot()
    bridge, env = backend.runtime_bytes()
    prepared = prepare_transition(fresh, bridge, env, plan)
    require(prepared["prepared_sha256"] == plan["prepared_sha256"], "reviewed_preparation_changed")
    require(prepared_projection(prepared) == plan["prepared_projection"], "reviewed_projection_changed")
    backend._prepared = prepared
    journal.save(schema_version=1, phase="prepared", plan_sha256=hash_object(plan),
                 record=public_record(prepared))
    try:
        backend.guard()
        journal.save(phase="network_create_intent", network_create_attempted=True)
        try:
            backend.create_network(prepared)
        except (Blocked, OSError):
            require(not backend.command_termination_unknown, "child_termination_unknown")
        network = backend.owned_network()
        require(network is not None, "network_create_not_observed")
        assert_owned_network(network, prepared, empty=True)
        journal.save(phase="network_created", network_id_sha256=digest(network["Id"].encode()),
                     network_spec_sha256=hash_object(network_spec(network)))
        backend.guard()
        journal.save(phase="runtime_write_intent")
        backend.write_runtime(prepared, bridge, env)
        target = backend.compose_config()
        config_contract(fresh["config"], target, prepared["network_name"], prepared["address"], prepared["portal_upstream"])
        journal.save(phase="runtime_written")
        backend.guard()
        backend.assert_image(prepared["image_id"])
        journal.save(phase="recreate_intent", forward_recreate_attempted=True)
        try:
            backend.recreate_caddy(prepared)
            journal.save(recreate_response_complete=True)
        except CommandFailed:
            journal.save(recreate_response_complete=True, recreate_failed=True)
            require(not backend.command_termination_unknown, "child_termination_unknown")
        except (Blocked, OSError):
            journal.save(recreate_response_unknown=True)
            require(not backend.command_termination_unknown, "child_termination_unknown")
        # A lost Compose response is acceptable only after exact fresh readback.
        identity = backend.verify(prepared, rollback=False)
        journal.save(phase="complete", recreate_response_observed=True, **(identity or {}))
        return "COMPLETE"
    except (Blocked, OSError, ValueError, KeyError, TypeError):
        journal.save(phase="held", interrupted_phase=journal.state["phase"],
                     child_termination_unknown=backend.command_termination_unknown)
        return "HELD"


def rollback_transition(plan, backend, journal):
    require(journal.state.get("plan_sha256") == hash_object(plan)
            and journal.state.get("phase") not in (None, "rolled_back"), "rollback_journal_identity")
    backend.phase_deadline(True)
    backend.guard()
    prepared = backend.recover_prepared(journal.state["record"])
    require(prepared["prepared_sha256"] == plan["prepared_sha256"], "rollback_preparation_changed")
    try:
        identity = backend.assert_recoverable(prepared, journal.state)
        if identity:
            journal.save(**identity)
        backend.restore_runtime(prepared)
        require(hash_object(backend.compose_config()) == prepared["config_sha256"], "rollback_config_changed")
        # Rollback recreation has its own durable intent, and is never retried.
        prior = journal.state.get("rollback_recreate_attempted", False)
        if not backend.is_original_caddy(prepared):
            require(not prior, "rollback_recreate_already_attempted")
            backend.guard()
            backend.assert_image(prepared["image_id"])
            journal.save(phase="rollback_recreate_intent", rollback_recreate_attempted=True)
            try:
                backend.recreate_caddy(prepared)
                journal.save(rollback_response_complete=True)
            except CommandFailed:
                journal.save(rollback_response_complete=True, rollback_failed=True)
                require(not backend.command_termination_unknown, "child_termination_unknown")
            except (Blocked, OSError):
                journal.save(rollback_response_unknown=True)
                require(not backend.command_termination_unknown, "child_termination_unknown")
        backend.verify(prepared, rollback=True)
        network = backend.owned_network()
        if network is not None:
            assert_owned_network(network, prepared, empty=True,
                                 id_sha256=journal.state.get("network_id_sha256"))
            require(hash_object(network_spec(network)) == journal.state.get("network_spec_sha256"), "rollback_network_spec")
            require(not journal.state.get("network_remove_attempted", False), "network_remove_already_attempted")
            backend.guard()
            journal.save(phase="network_remove_intent", network_remove_attempted=True)
            try:
                backend.remove_network(network["Id"])
            except (Blocked, OSError):
                require(not backend.command_termination_unknown, "child_termination_unknown")
            require(backend.owned_network() is None, "network_remove_not_observed")
        journal.save(phase="rolled_back")
        return "ROLLED_BACK"
    except (Blocked, OSError, ValueError, KeyError, TypeError):
        journal.save(phase="held", interrupted_phase=journal.state["phase"],
                     child_termination_unknown=backend.command_termination_unknown)
        return "HELD"


class NativeBackend:
    """Native adapter. A wrapper may add a fresh protected-resource guard."""
    def __init__(self, root, plan, protected_guard=None, runtime_file_guard=None):
        self.root = Path(root).resolve()
        self.plan, self.protected_guard = plan, protected_guard
        self.runtime_file_guard = runtime_file_guard
        self.project_name = plan.get("prepared_projection", {}).get("compose_project")
        self.lock_owned = False
        self.command_termination_unknown = False
        self.deadline = None
        self._prepared = None
        self.allowed_replaced = set()
        spec = importlib.util.spec_from_file_location("caddy_transition_sequence", self.root / "infra/k8s/tools/pvc-backup-sequence.py")
        sequence = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = sequence
        source = self.root / "infra/k8s/tools/pvc-backup-sequence.py"
        exec(compile(source.read_bytes(), str(source), "exec"), sequence.__dict__)
        self.local_lock = sequence.local_lock

    def phase_deadline(self, rollback):
        self.deadline = time.monotonic() + self.plan["budget"]["rollback_seconds" if rollback else "forward_seconds"]

    def group_exists(self, child):
        try:
            os.killpg(child.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            self.command_termination_unknown = True
            return True

    def stop_group(self, child):
        """Parent exit does not prove descendants have stopped writing."""
        if not self.group_exists(child):
            return
        for action in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(child.pid, action)
            except ProcessLookupError:
                return
            except PermissionError:
                self.command_termination_unknown = True
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            until = time.monotonic() + 2
            while self.group_exists(child) and time.monotonic() < until:
                time.sleep(0.02)
            if not self.group_exists(child):
                return
        self.command_termination_unknown = True

    def run(self, args, *, timeout=30, stdin=None):
        require(not self.command_termination_unknown, "child_termination_unknown")
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
        require(timeout > 0, "phase_time_budget")
        child = None
        child_options = {}
        if args and args[0] == "git":
            child_options["env"] = isolated_git_env(os.environ)
        try:
            child = subprocess.Popen(args, cwd=self.root, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, **child_options)
            output, _ = child.communicate(input=stdin, timeout=timeout)
            if child.returncode < 0:
                raise UnknownWrite("command_terminated")
            if child.returncode != 0:
                raise CommandFailed("command_failed")
            return output
        except (OSError, subprocess.TimeoutExpired):
            raise UnknownWrite("command_response_unknown") from None
        finally:
            if child is not None:
                self.stop_group(child)
                child.stdout.close()
                if child.stdin:
                    child.stdin.close()

    def json(self, args):
        try:
            return json.loads(self.run(args))
        except (ValueError, UnicodeError):
            raise Blocked("invalid_command_json") from None

    def compose(self, *args):
        command = ["docker", "compose", "--env-file", str(self.root / ".env")]
        for path in COMPOSE_FILES:
            command.extend(["-f", str(self.root / path)])
        return command + list(args)

    def compose_config(self):
        return self.json(self.compose("config", "--format", "json"))

    def git(self, *args):
        return self.run(["git", "--no-optional-locks", "--no-replace-objects", "-c",
                         "core.attributesFile=/dev/null", "-c", "credential.helper=", "-c", "core.askPass=", *args])

    def guard(self):
        require(not self.command_termination_unknown, "child_termination_unknown")
        if self.lock_owned:
            require(callable(self.protected_guard), "fresh_protected_guard_required")
            self.protected_guard()
        git_config_guard(self.git("config", "--no-includes", "--null", "--list"))
        gitdir = Path(self.git("rev-parse", "--absolute-git-dir").decode().strip())
        hooks = gitdir / "hooks"
        require(not hooks.is_symlink() and (not hooks.exists() or not any(
            path.is_symlink() or (path.is_file() and os.access(path, os.X_OK) and not path.name.endswith(".sample"))
            for path in hooks.iterdir())), "git_hooks_present")
        require(self.git("rev-parse", "HEAD").decode().strip() == self.plan["source_commit"], "source_commit_changed")
        require(not self.git("status", "--porcelain", "--untracked-files=no"), "source_dirty")
        require(ENV_IP not in os.environ and ENV_PORTAL not in os.environ, "compose_environment_override")

    def runtime_bytes(self):
        return (self.read_runtime(self.root / COMPOSE_FILES[-1]), self.read_runtime(self.root / ".env"))

    def read_runtime(self, path):
        return safe_read(path, platform_guard=self.runtime_file_guard)

    def caddy(self, *, optional=False):
        require(isinstance(self.project_name, str) and NAME.fullmatch(self.project_name), "compose_project_identity")
        ids = self.run(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + self.project_name,
                        "--filter", "label=com.docker.compose.service=caddy"]).decode().split()
        require(len(ids) <= 1 if optional else len(ids) == 1, "caddy_container_count")
        return self.json(["docker", "inspect", ids[0]])[0] if ids else None

    def image_reference(self, config, caddy=None):
        return config["services"]["caddy"].get("image") or (
            caddy["Config"]["Image"] if caddy is not None else config["name"] + "-caddy")

    def owned_network(self):
        ids = self.run(["docker", "network", "ls", "--filter", "name=^" + self.plan["network_name"] + "$", "-q"]).decode().split()
        require(len(ids) <= 1, "network_name_ambiguous")
        return self.json(["docker", "network", "inspect", ids[0]])[0] if ids else None

    def snapshot(self, *, allow_absent=False, config_override=None):
        self.guard()
        config = config_override if config_override is not None else self.compose_config()
        self.project_name = config["name"]
        caddy = self.caddy(optional=allow_absent)
        ids = self.run(["docker", "network", "ls", "-q"]).decode().split()
        require(ids, "docker_networks_empty")
        networks = self.json(["docker", "network", "inspect", *ids])
        default = next(item for item in networks if item["Name"] == config["networks"]["default"]["name"])
        containers = self.run(["docker", "ps", "-aq"]).decode().split()
        others = self.json(["docker", "inspect", *containers]) if containers else []
        # Container lifecycle identity, network endpoints, image and restart
        # count are enough to detect other-container disruption without logs.
        others = [{"Id": item["Id"], "Image": item["Image"], "RestartCount": item["RestartCount"],
                   "NetworkSettings": item["NetworkSettings"],
                   "State": {key: item["State"][key] for key in ("Status", "Running", "Paused", "Restarting",
                             "OOMKilled", "Dead", "ExitCode", "StartedAt", "FinishedAt")}}
                  for item in others if caddy is None or item["Id"] != caddy["Id"]]
        collisions = []
        for network in networks:
            for value in network.get("IPAM", {}).get("Config") or []:
                if value.get("Subnet"):
                    collisions.append(value["Subnet"])
        for family in ("-4", "-6"):
            for route in self.json(["ip", "-j", family, "route", "show", "table", "all"]):
                if route.get("dst") not in (None, "default"):
                    collisions.append(route["dst"])
        for interface in self.json(["ip", "-j", "address", "show"]):
            for address in interface.get("addr_info", []):
                collisions.append(f"{address['local']}/{address['prefixlen']}")
        nodes = self.json(KUBE + ["get", "nodes", "-o", "json"])
        for node in nodes["items"]:
            collisions.extend(node.get("spec", {}).get("podCIDRs") or [node["spec"]["podCIDR"]])
        service_cidrs = self.json(KUBE + ["get", "servicecidrs", "-o", "json"])
        require(service_cidrs["items"], "service_cidr_inventory_empty")
        for item in service_cidrs["items"]:
            collisions.extend(item["spec"]["cidrs"])
        services = self.json(KUBE + ["-n", "personal-server", "get", "services", "-o", "json"])
        for service in services["items"]:
            for address in service["spec"].get("clusterIPs", []):
                if address != "None":
                    collisions.append(address + ("/128" if ":" in address else "/32"))
        portal = next(item for item in services["items"] if item["metadata"]["name"] == "portal-web")
        ports = portal["spec"]["ports"]
        require(len(ports) == 1 and ports[0].get("protocol", "TCP") == "TCP", "portal_service_ports")
        image_name = self.plan.get("prepared_projection", {}).get("image_id") or self.image_reference(config, caddy)
        image_id = self.json(["docker", "image", "inspect", image_name])[0]["Id"]
        policies = self.json(KUBE + ["-n", "personal-server", "get", "networkpolicies", "-o", "json"])
        routes = []
        for name in ("portal-web", "book-memo", "youtube-memo", "crawler-worker"):
            service = next(item for item in services["items"] if item["metadata"]["name"] == name)
            require(len(service["spec"]["ports"]) == 1, "service_route_ports")
            routes.append({"name": name, "address": service["spec"]["clusterIP"],
                           "port": service["spec"]["ports"][0]["port"], "uid": service["metadata"]["uid"]})
        return {"source_commit": self.plan["source_commit"], "tracked_clean": True,
                "compose_version": self.run(["docker", "compose", "version", "--short"]).decode().strip(),
                "compose_stdin_config_name": self.plan.get("compose_stdin_config_name", "-"),
                "policy_count": len(policies["items"]), "config": config, "caddy": caddy,
                "local_image_id": image_id, "networks": networks, "default_network": default,
                "other_containers": sorted(others, key=lambda item: item["Id"]), "collisions": collisions,
                "service_routes": routes,
                "portal_upstream": f"{portal['spec']['clusterIP']}:{ports[0]['port']}"}

    def create_network(self, prepared):
        self._prepared = prepared
        spec = prepared["address"]
        self.run(["docker", "network", "create", "--driver", "bridge", "--subnet", spec["subnet"],
                  "--ip-range", spec["ip_range"], "--gateway", spec["gateway"],
                  "--label", OWNER_LABEL + "=" + prepared["owner_id"], prepared["network_name"]])

    def atomic_bridge(self, expected, target):
        """Prefix-only edit preserves inode, Windows ACL, owner and mode.

        This historical method name is retained for wrapper compatibility;
        owned prefix/suffix hashes, rather than replacement, secure recovery.
        """
        path = self.root / COMPOSE_FILES[-1]
        require(self.read_runtime(path) == expected, "bridge_write_race")
        require(target.startswith(expected) or expected.startswith(target), "bridge_prefix_only")
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | (os.O_APPEND if len(target) > len(expected) else 0))
        try:
            runtime_info_guard(path, os.fstat(fd), self.runtime_file_guard)
            require(self.read_runtime(path) == expected, "bridge_write_race")
            if len(target) > len(expected):
                os.lseek(fd, 0, os.SEEK_END)
                view = memoryview(target[len(expected):])
                while view:
                    count = os.write(fd, view)
                    require(count > 0, "bridge_short_write")
                    view = view[count:]
            else:
                os.ftruncate(fd, len(target))
            os.fsync(fd)
        finally:
            os.close(fd)
        require(self.read_runtime(path) == target, "bridge_write_readback")

    def append_env(self, expected, suffix):
        path = self.root / ".env"
        require(self.read_runtime(path) == expected, "env_write_race")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        try:
            runtime_info_guard(path, os.fstat(fd), self.runtime_file_guard)
            view = memoryview(suffix)
            while view:
                count = os.write(fd, view)
                require(count > 0, "env_short_write")
                view = view[count:]
            os.fsync(fd)
        finally:
            os.close(fd)

    def write_runtime(self, prepared, bridge, env):
        self.append_env(env, prepared["env_suffix"])
        self.atomic_bridge(bridge, prepared["target_bridge"])

    def assert_image(self, image_id):
        require(IMAGE.fullmatch(image_id), "immutable_image_id")
        images = self.json(["docker", "image", "inspect", image_id])
        require(len(images) == 1 and images[0]["Id"] == image_id, "local_image_changed")

    def recreate_caddy(self, prepared):
        override = canonical({"services": {"caddy": {"image": prepared["image_id"]}}})
        try:
            approved = self.compose_config()
            if hash_object(approved) != prepared["config_sha256"]:
                config_contract(prepared["baseline_config"], approved, prepared["network_name"],
                                prepared["address"], prepared["portal_upstream"])
            effective = json.loads(self.run(self.compose() + ["-f", "-", "config", "--format", "json"], stdin=override))
            image_override_contract(approved, effective, prepared["image_id"])
            self.assert_image(prepared["image_id"])
        except (Blocked, OSError, ValueError, KeyError, TypeError):
            # No recreation request has been sent: rollback can distinguish
            # this known preflight failure from an ambiguous Docker write.
            raise CommandFailed("recreate_preflight_failed") from None
        self.run(self.compose() + ["-f", "-", "up", "-d", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", "caddy"],
                 stdin=override, timeout=120)

    def verify(self, prepared, *, rollback):
        self.guard()
        fresh = self.snapshot()
        caddy = fresh["caddy"]
        require(caddy["State"]["Running"] is True and caddy["Image"] == prepared["image_id"], "caddy_image_or_running")
        require(hash_object(fresh["other_containers"]) == prepared["other_containers_sha256"], "other_container_changed")
        require(hash_object(network_spec(fresh["default_network"])) == prepared["default_network_sha256"], "default_network_changed")
        require(digest(fresh["default_network"]["Id"].encode()) == prepared["default_network_id_sha256"], "default_network_replaced")
        require(fresh["service_routes"] == prepared["service_routes"], "service_routes_changed")
        endpoints = {key: value for key, value in fresh["default_network"].get("Containers", {}).items() if key != caddy["Id"]}
        require(hash_object(endpoints) == prepared["default_endpoints_sha256"], "default_other_endpoints_changed")
        if rollback:
            require(set(caddy["NetworkSettings"]["Networks"]) == {fresh["default_network"]["Name"]}
                    and hash_object(fresh["config"]) == prepared["config_sha256"], "caddy_rollback_shape")
            endpoint = caddy["NetworkSettings"]["Networks"][fresh["default_network"]["Name"]]
            require(endpoint.get("NetworkID") == fresh["default_network"]["Id"]
                    and not endpoint.get("IPAMConfig")
                    and caddy["HostConfig"]["NetworkMode"] in (fresh["default_network"]["Name"],
                    fresh["default_network"]["Id"]), "caddy_rollback_endpoint")
        else:
            require(len(caddy["NetworkSettings"]["Networks"]) == 2, "caddy_new_network_count")
            owned = self.owned_network()
            assert_owned_network(owned, prepared)
            require(set(owned.get("Containers", {})) == {caddy["Id"]}, "dedicated_other_endpoint")
            endpoint = caddy["NetworkSettings"]["Networks"].get(prepared["network_name"], {})
            require(endpoint.get("IPAddress") == prepared["address"]["fixed_ip"]
                    and endpoint.get("NetworkID") == owned["Id"]
                    and endpoint.get("GwPriority") == 1
                    and endpoint.get("IPAMConfig", {}).get("IPv4Address") == prepared["address"]["fixed_ip"], "caddy_fixed_endpoint")
            require(caddy["HostConfig"]["NetworkMode"] in (fresh["default_network"]["Name"],
                    fresh["default_network"]["Id"], owned["Name"], owned["Id"]), "caddy_network_mode")
            for route in prepared["service_routes"]:
                observed = self.run(["docker", "exec", caddy["Id"], "ip", "route", "get", route["address"]])
                source = re.search(rb"\bsrc\s+(\S+)", observed)
                gateway = re.search(rb"\bvia\s+(\S+)", observed)
                require(source is not None and source.group(1).decode() == prepared["address"]["fixed_ip"]
                        and gateway is not None and gateway.group(1).decode() == prepared["address"]["gateway"], "caddy_route_source")
                self.run(["docker", "exec", caddy["Id"], "wget", "-q", "-O", "/dev/null", "-T", "5",
                          f"http://{route['address']}:{route['port']}/health"], timeout=10)
            config_contract(prepared["baseline_config"], fresh["config"], prepared["network_name"],
                            prepared["address"], prepared["portal_upstream"])
        # Actual Caddy config (including every credential) is compared only in
        # memory, with the single approved Portal variable normalized.
        contract = caddy_runtime_contract(caddy, prepared)
        require(all(prepared["caddy_" + key] == value for key, value in contract.items()), "caddy_runtime_unapproved_change")
        portal_value = prepared["baseline_config"]["services"]["caddy"]["environment"]["PORTAL_UPSTREAM"] if rollback else prepared["portal_upstream"]
        require([item for item in caddy["Config"]["Env"] if item.startswith("PORTAL_UPSTREAM=")]
                == ["PORTAL_UPSTREAM=" + portal_value], "caddy_actual_portal_upstream")
        generated_identity(caddy, prepared, getattr(self, "allowed_replaced", ()))
        return {"forward_caddy_id_sha256": digest(caddy["Id"].encode())} if not rollback else {}

    def recover_prepared(self, record):
        projection = self.plan["prepared_projection"]
        bridge, env = self.runtime_bytes()
        original_bridge = inverse_bridge(bridge, projection["bridge_size"],
                                         projection["bridge_sha256"], projection["network_name"])
        require(len(env) >= projection["env_size"], "recovery_env_size")
        original_env = env[:projection["env_size"]]
        require(digest(original_env) == projection["env_sha256"], "recovery_env_prefix")
        suffix = env_suffix(original_env, projection["address"], projection["portal_upstream"])
        inverse_env(env, projection["env_size"], projection["env_sha256"], suffix)
        # Only the noncredential original bridge crosses stdin. The existing
        # .env stays in place; its appended keys have no references in the
        # original three-file model and cannot affect its resulting hash.
        command = ["docker", "compose", "--env-file", str(self.root / ".env")]
        for path in COMPOSE_FILES[:-1]:
            command.extend(["-f", str(self.root / path)])
        command.extend(["-f", "-", "config", "--format", "json"])
        baseline = json.loads(self.run(command, stdin=original_bridge))
        require(hash_object(baseline) == projection["config_sha256"], "recovery_baseline_config")
        prepared = {**projection, "prepared_sha256": hash_object(projection),
                    "target_bridge": edit_bridge(original_bridge, projection["network_name"]),
                    "env_suffix": suffix, "baseline_config": baseline}
        require(public_record(prepared) == record, "recovery_record_changed")
        self._prepared = prepared
        return prepared

    def assert_recoverable(self, prepared, state):
        require(not self.command_termination_unknown, "child_termination_unknown")
        current = self.caddy(optional=True)
        self.allowed_replaced = {state[key] for key in ("forward_caddy_id_sha256", "rollback_replaced_caddy_id_sha256")
                                 if state.get(key)}
        network = self.owned_network()
        recovered_identity = {}
        if state.get("network_create_attempted") and network is None and not state.get("network_id_sha256"):
            raise Blocked("unresolved_network_create_intent")
        if network is not None:
            if state.get("network_id_sha256") is None:
                require(state.get("network_create_attempted") is True
                        and not state.get("forward_recreate_attempted"), "unknown_network_identity")
                assert_owned_network(network, prepared, empty=True)
                recovered_identity = {"network_id_sha256": digest(network["Id"].encode()),
                                      "network_spec_sha256": hash_object(network_spec(network))}
            else:
                assert_owned_network(network, prepared, id_sha256=state["network_id_sha256"])
            require(set(network.get("Containers", {})) <= ({current["Id"]} if current else set()), "foreign_dedicated_endpoint")
        forward_unresolved = (state.get("forward_recreate_attempted") is True
            and state.get("recreate_response_complete") is not True
            and state.get("recreate_response_observed") is not True)
        rollback_unresolved = (state.get("rollback_recreate_attempted") is True
            and state.get("rollback_response_complete") is not True
            and state.get("rollback_response_observed") is not True)
        if forward_unresolved:
            require(current is not None and current["State"]["Running"]
                    and digest(current["Id"].encode()) != prepared["caddy_id_sha256"],
                    "unresolved_daemon_recreate")
            # Recovery readback must be as strong as forward completion,
            # including exact endpoints, source routes and actual health.
            recovered_identity.update(self.verify(prepared, rollback=False))
            recovered_identity["recreate_response_observed"] = True
        if rollback_unresolved:
            require(current is not None and current["State"]["Running"], "unresolved_daemon_rollback")
            self.verify(prepared, rollback=True)
            recovered_identity["rollback_response_observed"] = True
        bridge, env = self.runtime_bytes()
        inverse_bridge(bridge, prepared["bridge_size"], prepared["bridge_sha256"], prepared["network_name"])
        inverse_env(env, prepared["env_size"], prepared["env_sha256"], prepared["env_suffix"])
        fresh = self.snapshot(allow_absent=True, config_override=prepared["baseline_config"])
        require(hash_object(fresh["other_containers"]) == prepared["other_containers_sha256"]
                and hash_object(network_spec(fresh["default_network"])) == prepared["default_network_sha256"]
                and digest(fresh["default_network"]["Id"].encode()) == prepared["default_network_id_sha256"]
                and fresh["service_routes"] == prepared["service_routes"], "recovery_runtime_changed")
        if current is None:
            require(state.get("forward_recreate_attempted") is True, "unowned_caddy_absence")
            require(not forward_unresolved and not rollback_unresolved,
                    "unresolved_daemon_recreate")
            return recovered_identity
        if not current["State"]["Running"]:
            require(state.get("forward_recreate_attempted") is True, "unowned_caddy_stop")
            require(not forward_unresolved and not rollback_unresolved,
                    "unresolved_daemon_recreate")
        if digest(current["Id"].encode()) == prepared["caddy_id_sha256"]:
            require(not forward_unresolved,
                    "unresolved_original_caddy_recreate")
        require(current["Image"] == prepared["image_id"] and all(prepared["caddy_" + key] == value
                for key, value in caddy_runtime_contract(current, prepared).items()), "recovery_caddy_contract")
        generated_identity(current, prepared, self.allowed_replaced)
        old_portal = prepared["baseline_config"]["services"]["caddy"]["environment"]["PORTAL_UPSTREAM"]
        actual_portal = [item for item in current["Config"]["Env"] if item.startswith("PORTAL_UPSTREAM=")]
        require(len(actual_portal) == 1 and actual_portal[0] in ("PORTAL_UPSTREAM=" + old_portal,
                "PORTAL_UPSTREAM=" + prepared["portal_upstream"]), "recovery_foreign_portal")
        endpoints = current["NetworkSettings"]["Networks"]
        default = fresh["default_network"]
        approved_names = {default["Name"], prepared["network_name"]}
        require(set(endpoints) <= approved_names, "recovery_foreign_network")
        if current["State"]["Running"]:
            require(default["Name"] in endpoints, "recovery_missing_default_network")
        else:
            require(state.get("forward_recreate_attempted") is True, "recovery_partial_endpoint_without_intent")
        if default["Name"] in endpoints:
            require(endpoints[default["Name"]]["NetworkID"] == default["Id"], "recovery_default_endpoint")
        if prepared["network_name"] in endpoints:
            endpoint = endpoints[prepared["network_name"]]
            require(network is not None and endpoint.get("NetworkID") == network["Id"]
                    and endpoint.get("IPAddress") in ((prepared["address"]["fixed_ip"],)
                        if current["State"]["Running"] else ("", prepared["address"]["fixed_ip"]))
                    and endpoint.get("IPAMConfig", {}).get("IPv4Address") == prepared["address"]["fixed_ip"], "recovery_dedicated_endpoint")
        require(current["HostConfig"]["NetworkMode"] in (*approved_names, default["Id"],
                *([network["Id"]] if network else [])), "recovery_network_mode")
        current_hash = digest(current["Id"].encode())
        self.allowed_replaced.add(current_hash)
        return {**recovered_identity, "rollback_replaced_caddy_id_sha256": current_hash}

    def restore_runtime(self, prepared):
        bridge, env = self.runtime_bytes()
        original_bridge = inverse_bridge(bridge, prepared["bridge_size"], prepared["bridge_sha256"], prepared["network_name"])
        if bridge != original_bridge:
            self.atomic_bridge(bridge, original_bridge)
        require(digest(self.runtime_bytes()[0]) == prepared["bridge_sha256"], "rollback_bridge_hash")
        original = inverse_env(env, prepared["env_size"], prepared["env_sha256"], prepared["env_suffix"])
        if env != original:
            fd = os.open(self.root / ".env", os.O_WRONLY | os.O_NOFOLLOW)
            try:
                runtime_info_guard(self.root / ".env", os.fstat(fd), self.runtime_file_guard)
                require(self.read_runtime(self.root / ".env") == env, "env_restore_race")
                os.ftruncate(fd, len(original))
                os.fsync(fd)
            finally:
                os.close(fd)
        require(digest(self.runtime_bytes()[1]) == prepared["env_sha256"], "rollback_env_hash")

    def is_original_caddy(self, prepared):
        caddy = self.caddy(optional=True)
        if caddy is None:
            return False
        old_portal = prepared["baseline_config"]["services"]["caddy"]["environment"]["PORTAL_UPSTREAM"]
        return (caddy["Image"] == prepared["image_id"]
                and len(caddy["NetworkSettings"]["Networks"]) == 1 and caddy["State"]["Running"]
                and [item for item in caddy["Config"]["Env"] if item.startswith("PORTAL_UPSTREAM=")]
                    == ["PORTAL_UPSTREAM=" + old_portal]
                and all(prepared["caddy_" + key] == value for key, value in caddy_runtime_contract(caddy, prepared).items()))

    def remove_network(self, network_id):
        self.run(["docker", "network", "rm", network_id])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--plan", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--go", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = json.loads(safe_read(args.plan, private=True))
        validate_plan(plan, mutation=args.go or args.rollback)
        backend = NativeBackend(args.root, plan)
        if not (args.go or args.rollback):
            fresh = backend.snapshot()
            bridge, env = backend.runtime_bytes()
            print(json.dumps({"status": "CHECK", **public_record(prepare_transition(fresh, bridge, env, plan))}, sort_keys=True))
            return 0
        # Native mutation deliberately requires the reviewed operational wrapper
        # to attach fresh protected-resource/backup-idle checks. A flag alone
        # cannot manufacture that proof.
        raise Blocked("reviewed_operational_wrapper_required")
    except Blocked as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "BLOCKED", "reason": "preflight_failed"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
