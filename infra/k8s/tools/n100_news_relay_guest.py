#!/usr/bin/env python3
"""Manage a fixed, broker-only IPv4 route to the Windows news TLS relays."""

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

DESTINATIONS = {
    "kr.investing.com": 46191,
    "news.google.com": 46192,
    "api.telegram.org": 46193,
}
TABLE = 18461
NAME = "n100_news_relay"
BROKER_UID = 10002
BEGIN = b"# BEGIN N100 NEWS RELAY\n"
END = b"# END N100 NEWS RELAY\n"
HOSTS = Path("/etc/hosts")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")


def policy(config):
    if set(config) != {"guest", "relay", "interface", "providers", "boot_sha256"}:
        raise ValueError("invalid_relay_fields")
    guest = ipaddress.IPv4Address(config["guest"])
    relay = ipaddress.IPv4Address(config["relay"])
    if (guest == relay or guest.is_global or relay.is_global or guest.is_loopback
            or relay.is_loopback or guest.is_link_local or relay.is_link_local
            or guest.is_multicast or relay.is_multicast or guest.is_unspecified
            or relay.is_unspecified or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", config["interface"])
            or not re.fullmatch(r"[0-9a-f]{64}", config["boot_sha256"])):
        raise ValueError("invalid_relay_link")
    if not isinstance(config["providers"], dict) or set(config["providers"]) != set(DESTINATIONS):
        raise ValueError("invalid_relay_providers")
    addresses = {name: ipaddress.IPv4Address(value) for name, value in config["providers"].items()}
    if len(set(addresses.values())) != len(DESTINATIONS) or any(
            not addr.is_global or addr.is_multicast or addr.is_reserved for addr in addresses.values()):
        raise ValueError("nonpublic_or_shared_relay_address")
    entries = "".join(f"{addresses[name]}\t{name}\n" for name in DESTINATIONS).encode()
    routes = [(str(addresses[name]), DESTINATIONS[name]) for name in DESTINATIONS]
    nat_rules = "\n".join(
        f"    meta skuid {BROKER_UID} ip daddr {address} tcp dport 443 dnat to {relay}:{port}"
        for address, port in routes
    )
    ports = ", ".join(str(port) for port in DESTINATIONS.values())
    upstream = ", ".join(address for address, _ in routes)
    nft = f"""table ip {NAME} {{
  chain redirect_tls {{
    type nat hook output priority dstnat; policy accept;
{nat_rules}
  }}
  chain protect_relay {{
    type filter hook output priority -10; policy accept;
    ip daddr {relay} tcp dport {{ {ports} }} meta skuid != {BROKER_UID} reject with tcp reset
    ip daddr {{ {upstream} }} reject
  }}
}}
"""
    return BEGIN + entries + END, routes, nft


def run(command, *, input_text=None, check=True):
    executable = shutil.which(command[0], path="/usr/sbin:/usr/bin:/sbin:/bin")
    if not executable:
        raise RuntimeError("relay_tool_absent")
    result = subprocess.run([executable, *command[1:]], input=input_text, text=True, check=False,
                            capture_output=True, timeout=8)
    if check and result.returncode:
        raise RuntimeError("relay_command_failed")
    return result


def read_hosts():
    st = HOSTS.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != 0 or st.st_mode & 0o022:
        raise RuntimeError("untrusted_hosts_file")
    return HOSTS.read_bytes(), stat.S_IMODE(st.st_mode)


def write_hosts(previous, data, mode):
    if read_hosts()[0] != previous:
        raise RuntimeError("hosts_file_changed")
    with tempfile.NamedTemporaryFile(dir=HOSTS.parent, prefix=".n100-news-relay-", delete=False) as handle:
        path = Path(handle.name)
        try:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            if read_hosts()[0] != previous:
                raise RuntimeError("hosts_file_changed")
            os.replace(path, HOSTS)
        finally:
            path.unlink(missing_ok=True)


def route_state(config, block):
    if hashlib.sha256(BOOT_ID.read_bytes()).hexdigest() != config["boot_sha256"]:
        raise RuntimeError("relay_boot_changed")
    if json.loads(run(("ip", "-j", "-4", "route", "show", "default")).stdout):
        raise RuntimeError("unexpected_default_route")
    live = json.loads(run(("ip", "-j", "-4", "route", "get", config["relay"])).stdout)
    if len(live) != 1 or live[0].get("dev") != config["interface"] or live[0].get("prefsrc") != config["guest"] or live[0].get("gateway"):
        raise RuntimeError("unexpected_relay_link")
    hosts, mode = read_hosts()
    has_rules = any(row.get("priority") == TABLE or str(row.get("table")) == str(TABLE)
                    for row in json.loads(run(("ip", "-j", "-4", "rule", "list")).stdout))
    has_table = run(("nft", "list", "table", "ip", NAME), check=False).returncode == 0
    routes = run(("ip", "-j", "-4", "route", "show", "table", str(TABLE)), check=False)
    has_routes = routes.returncode == 0 and bool(json.loads(routes.stdout))
    return hosts, mode, (block in hosts, has_rules, has_table, has_routes)


def apply(config):
    block, addresses, nft = policy(config)
    hosts, mode, state = route_state(config, block)
    if any(state) or BEGIN in hosts or END in hosts:
        raise RuntimeError("relay_already_present")
    existing = {token for line in hosts.splitlines() for token in line.split(b"#", 1)[0].split()[1:]}
    if existing.intersection(name.encode() for name in DESTINATIONS):
        raise RuntimeError("provider_hosts_already_present")
    if hosts and not hosts.endswith(b"\n"):
        raise RuntimeError("hosts_missing_final_newline")
    run(("nft", "--check", "-f", "-"), input_text=nft)
    uidrange = f"{BROKER_UID}-{BROKER_UID}"
    priority = str(TABLE)
    cleanup = []
    try:
        run(("nft", "-f", "-"), input_text=nft)
        cleanup.append(("nft", "delete", "table", "ip", NAME))
        for address, _ in addresses:
            tail = (address + "/32", "via", config["relay"], "dev", config["interface"], "table", priority)
            run(("ip", "-4", "route", "add", *tail))
            cleanup.append(("ip", "-4", "route", "del", *tail))
        rule = ("pref", priority, "uidrange", uidrange, "lookup", priority)
        run(("ip", "-4", "rule", "add", *rule))
        cleanup.append(("ip", "-4", "rule", "del", *rule))
        write_hosts(hosts, hosts + block, mode)
        if route_state(config, block)[2] != (True, True, True, True):
            raise RuntimeError("relay_readback_failed")
    except Exception:
        try:
            for command in reversed(cleanup):
                run(command, check=False)
        finally:
            if read_hosts()[0] == hosts + block:
                write_hosts(hosts + block, hosts, mode)
        raise


def remove(config):
    block, addresses, _ = policy(config)
    hosts, mode, state = route_state(config, block)
    if not any(state):
        return
    if state != (True, True, True, True) or hosts.count(block) != 1:
        raise RuntimeError("relay_mixed_state_manual_reconciliation")
    priority = str(TABLE)
    run(("ip", "-4", "rule", "del", "pref", priority, "uidrange", f"{BROKER_UID}-{BROKER_UID}", "lookup", priority))
    for address, _ in addresses:
        run(("ip", "-4", "route", "del", address + "/32", "via", config["relay"], "dev", config["interface"], "table", priority))
    run(("nft", "delete", "table", "ip", NAME))
    write_hosts(hosts, hosts.replace(block, b"", 1), mode)
    if any(route_state(config, block)[2]):
        raise RuntimeError("relay_remove_readback_failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "apply", "remove"))
    args = parser.parse_args()
    try:
        config = json.load(sys.stdin)
        block, _, script = policy(config)
        if args.mode == "check":
            run(("nft", "--check", "-f", "-"), input_text=script)
        else:
            if os.geteuid() != 0:
                raise RuntimeError("root_required")
            (apply if args.mode == "apply" else remove)(config)
        state = route_state(config, block)[2]
        print(json.dumps(dict(zip(("host_entries", "uid_rule", "nft_table", "routes"), state))))
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.TimeoutExpired):
        parser.exit(1, "n100_news_relay=failed; reconcile before retry\n")


if __name__ == "__main__":
    main()
