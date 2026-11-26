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
GUARD_NAME = "n100_news_relay_guard"
GUARD_CHAINS = {"deny_direct_relay": "output", "deny_forwarded_relay": "forward"}
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
    type nat hook output priority -100; policy accept;
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


def guard_policy(config):
    """Deny direct relay connections before the later, broker-only DNAT hook.

    This independent table has no allow state and survives apply/remove. A boot
    installer must execute ``guard`` successfully before enabling networking;
    merely installing this helper does not provide that ordering.
    """
    policy(config)
    rules = "\n".join(
        f"    ip daddr {config['relay']} tcp dport {port} reject with tcp reset"
        for port in DESTINATIONS.values()
    )
    chains = "".join(f"""  chain {name} {{
    type filter hook {hook} priority -110; policy accept;
{rules}
  }}
""" for name, hook in GUARD_CHAINS.items())
    return f"table ip {GUARD_NAME} {{\n{chains}}}\n"


def guard_expected(config):
    table = {"family": "ip", "name": GUARD_NAME}
    rows = [{"table": table}]
    for name, hook in GUARD_CHAINS.items():
        rows.append({"chain": {"family": "ip", "table": GUARD_NAME, "name": name,
                              "type": "filter", "hook": hook, "prio": -110, "policy": "accept"}})
        for port in DESTINATIONS.values():
            rows.append({"rule": {
                "family": "ip", "table": GUARD_NAME, "chain": name,
                "expr": [
                    {"match": {"op": "==", "left": {"payload": {"protocol": "ip", "field": "daddr"}},
                               "right": config["relay"]}},
                    {"match": {"op": "==", "left": {"payload": {"protocol": "tcp", "field": "dport"}},
                               "right": port}},
                    {"reject": {"type": "tcp reset"}},
                ],
            }})
    return rows


def guard_present(config):
    result = run(("nft", "-j", "list", "table", "ip", GUARD_NAME), check=False)
    if result.returncode:
        if nft_table_present(GUARD_NAME):
            raise RuntimeError("relay_guard_query_failed")
        return False
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or set(value) != {"nftables"} or not isinstance(value["nftables"], list):
        raise RuntimeError("relay_guard_unexpected_state")
    rows = []
    for row in value["nftables"]:
        if not isinstance(row, dict) or len(row) != 1:
            raise RuntimeError("relay_guard_unexpected_state")
        kind, data = next(iter(row.items()))
        if kind == "metainfo":
            continue
        if not isinstance(data, dict):
            raise RuntimeError("relay_guard_unexpected_state")
        rows.append({kind: {key: item for key, item in data.items() if key != "handle"}})
    # Every owned rule is an unconditional exact-endpoint reject, so row order
    # cannot change its effect. nft versions may enumerate chains differently.
    def canonical(row):
        return json.dumps(row, sort_keys=True)

    if sorted(rows, key=canonical) != sorted(guard_expected(config), key=canonical):
        raise RuntimeError("relay_guard_unexpected_state")
    return True


def apply_guard(config):
    script = guard_policy(config)
    if guard_present(config):
        return
    # create, rather than add/flush, refuses an unknown table or a concurrent
    # creation without altering it. Both statements commit as one nft batch.
    script = f"create table ip {GUARD_NAME}\n" + script
    run(("nft", "--check", "-f", "-"), input_text=script)
    run(("nft", "-f", "-"), input_text=script)
    if not guard_present(config):
        raise RuntimeError("relay_guard_readback_failed")


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


def nft_table_present(name):
    value = json.loads(run(("nft", "-j", "list", "tables")).stdout)
    if not isinstance(value, dict) or set(value) != {"nftables"} or not isinstance(value["nftables"], list):
        raise RuntimeError("relay_nft_inventory_invalid")
    found = False
    for row in value["nftables"]:
        if not isinstance(row, dict) or len(row) != 1:
            raise RuntimeError("relay_nft_inventory_invalid")
        if "metainfo" in row:
            continue
        table = row.get("table")
        if (not isinstance(table, dict) or not isinstance(table.get("family"), str)
                or not isinstance(table.get("name"), str)):
            raise RuntimeError("relay_nft_inventory_invalid")
        found = found or (table["family"] == "ip" and table["name"] == name)
    return found


def route_state(config, block):
    if hashlib.sha256(BOOT_ID.read_bytes()).hexdigest() != config["boot_sha256"]:
        raise RuntimeError("relay_boot_changed")
    if json.loads(run(("ip", "-j", "-4", "route", "show", "default")).stdout):
        raise RuntimeError("unexpected_default_route")
    live = json.loads(run(("ip", "-j", "-4", "route", "get", config["relay"])).stdout)
    if len(live) != 1 or live[0].get("dev") != config["interface"] or live[0].get("prefsrc") != config["guest"] or live[0].get("gateway"):
        raise RuntimeError("unexpected_relay_link")
    hosts, mode = read_hosts()
    rules = json.loads(run(("ip", "-N", "-j", "-4", "rule", "list")).stdout)
    # Listing an absent FIB table exits nonzero, which cannot safely be treated
    # as absence after a crash. A successful complete inventory proves absence.
    routes = json.loads(run(("ip", "-N", "-j", "-4", "route", "show", "table", "all")).stdout)
    if any(not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
           for rows in (rules, routes)):
        raise RuntimeError("relay_route_inventory_invalid")
    if any("table" in row and not re.fullmatch(r"[0-9]+", str(row["table"])) for row in routes):
        raise RuntimeError("relay_route_inventory_invalid")
    has_rules = any(row.get("priority") == TABLE or str(row.get("table")) == str(TABLE) for row in rules)
    has_table = nft_table_present(NAME)
    has_routes = any(str(row.get("table")) == str(TABLE) for row in routes)
    return hosts, mode, (block in hosts, has_rules, has_table, has_routes)


def apply(config):
    block, addresses, nft = policy(config)
    apply_guard(config)
    hosts, mode, state = route_state(config, block)
    previous_hosts = hosts
    stale = exact_stale_hosts(hosts, block, state)
    if stale:
        hosts = hosts.replace(block, b"", 1)
        state = (False, False, False, False)
    if any(state) or BEGIN in hosts or END in hosts:
        raise RuntimeError("relay_already_present")
    existing = {token for line in hosts.splitlines() for token in line.split(b"#", 1)[0].split()[1:]}
    if existing.intersection(name.encode() for name in DESTINATIONS):
        raise RuntimeError("provider_hosts_already_present")
    if hosts and not hosts.endswith(b"\n"):
        raise RuntimeError("hosts_missing_final_newline")
    run(("nft", "--check", "-f", "-"), input_text=nft)
    if stale:
        write_hosts(previous_hosts, hosts, mode)
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
    apply_guard(config)
    hosts, mode, state = route_state(config, block)
    if exact_stale_hosts(hosts, block, state):
        write_hosts(hosts, hosts.replace(block, b"", 1), mode)
        if any(route_state(config, block)[2]):
            raise RuntimeError("relay_remove_readback_failed")
        return
    if not any(state):
        if BEGIN in hosts or END in hosts:
            raise RuntimeError("relay_mixed_state_manual_reconciliation")
        return
    if state != (True, True, True, True) or not exact_hosts_marker(hosts, block):
        raise RuntimeError("relay_mixed_state_manual_reconciliation")
    priority = str(TABLE)
    run(("ip", "-4", "rule", "del", "pref", priority, "uidrange", f"{BROKER_UID}-{BROKER_UID}", "lookup", priority))
    for address, _ in addresses:
        run(("ip", "-4", "route", "del", address + "/32", "via", config["relay"], "dev", config["interface"], "table", priority))
    run(("nft", "delete", "table", "ip", NAME))
    write_hosts(hosts, hosts.replace(block, b"", 1), mode)
    if any(route_state(config, block)[2]):
        raise RuntimeError("relay_remove_readback_failed")


def exact_hosts_marker(hosts, block):
    start = hosts.find(block)
    return (hosts.count(block) == 1 and hosts.count(BEGIN) == 1 and hosts.count(END) == 1
            and (start == 0 or hosts[start - 1:start] == b"\n"))


def exact_stale_hosts(hosts, block, state):
    # A reboot clears nft/routes/rules but leaves this exact hosts block. Do not
    # reconcile partial, changed or duplicated markers or any surviving allow.
    return state == (True, False, False, False) and exact_hosts_marker(hosts, block)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "guard", "apply", "remove"))
    args = parser.parse_args()
    try:
        config = json.load(sys.stdin)
        block, _, script = policy(config)
        if args.mode == "check":
            run(("nft", "--check", "-f", "-"), input_text=script)
        else:
            if os.geteuid() != 0:
                raise RuntimeError("root_required")
            if args.mode == "guard":
                apply_guard(config)
                print(json.dumps({"relay_deny_guard": True}))
                return
            (apply if args.mode == "apply" else remove)(config)
        state = route_state(config, block)[2]
        print(json.dumps(dict(zip(("host_entries", "uid_rule", "nft_table", "routes"), state))))
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.TimeoutExpired):
        parser.exit(1, "n100_news_relay=failed; reconcile before retry\n")


if __name__ == "__main__":
    main()
