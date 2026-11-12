"""One-shot SCM credential check. It cannot invoke the HA transition controller."""
import argparse
import csv
import ctypes
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from ctypes import wintypes


SCOPE = "isolated-scm-postgres-auth"
SID = re.compile(r"S-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}")
HEX = re.compile(r"[0-9a-f]{64}")
PRIVATE_NETWORKS = tuple(map(ipaddress.ip_network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))


def require(condition):
    if not condition:
        raise ValueError("isolated_auth_probe_rejected")


def plain_path(path):
    require(path.is_absolute())
    for entry in (path, *path.parents):
        info = entry.lstat()
        require(not entry.is_symlink() and not (getattr(info, "st_file_attributes", 0) & 1024))
    return path


def plain_file(path, maximum):
    info = plain_path(path).lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 < info.st_size <= maximum)
    return path


def strict_json(raw):
    def pairs(items):
        value = dict(items)
        require(len(value) == len(items))
        return value

    return json.loads(raw, object_pairs_hook=pairs)


def service_identity():
    require(sys.platform == "win32")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    session = wintypes.DWORD()
    require(kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)) and session.value == 0)
    directory = ctypes.create_unicode_buffer(32768)
    length = kernel.GetSystemDirectoryW(directory, len(directory))
    require(0 < length < len(directory))
    result = subprocess.run([str(Path(directory.value) / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
                            capture_output=True, check=True, timeout=3)
    row, = csv.reader(result.stdout.decode("ascii", errors="replace").splitlines())
    _, sid = row
    require(SID.fullmatch(sid))
    return sid


def verify_plan(plan, approval, raw, now):
    require(type(plan) is dict and set(plan) == {"scope", "service_sid", "hosts", "system_id", "ca_sha256"})
    require(plan["scope"] == SCOPE and SID.fullmatch(plan["service_sid"]) and HEX.fullmatch(plan["ca_sha256"]))
    require(type(plan["system_id"]) is str and re.fullmatch(r"[1-9][0-9]{0,19}", plan["system_id"]))
    hosts = plan["hosts"]
    require(type(hosts) is dict and set(hosts) == {"A", "B"} and hosts["A"] != hosts["B"])
    for host in hosts.values():
        require(type(host) is str)
        address = ipaddress.IPv4Address(host)
        require(any(address in network for network in PRIVATE_NETWORKS))
    pin = hashlib.sha256(raw).hexdigest()
    require(type(approval) is dict and set(approval) == {"scope", "plan_sha256", "expires_unix"})
    require(approval["scope"] == SCOPE and approval["plan_sha256"] == pin and
            type(approval["expires_unix"]) is int and now < approval["expires_unix"] <= now + 900)
    return pin


def authenticate(plan, material, connect):
    ca = plain_file(material / "ca.crt", 16384)
    passfile = plain_file(material / "postgres.pgpass", 1024)
    require(hashlib.sha256(ca.read_bytes()).hexdigest() == plan["ca_sha256"])
    for node in ("A", "B"):
        host = plan["hosts"][node]
        with connect(host=host, hostaddr=host, port=5432, dbname="ha", user="postgres",
                     sslmode="verify-full", gssencmode="disable", sslrootcert=str(ca), passfile=str(passfile),
                     require_auth="scram-sha-256", connect_timeout=2,
                     application_name="ha_scm_isolated_auth") as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute("SET LOCAL statement_timeout='700ms'")
            identity = connection.execute(
                "SELECT current_setting('cluster_name'), pg_is_in_recovery(), "
                "current_setting('transaction_read_only'), system_identifier::text, session_user "
                "FROM pg_control_system()"
            ).fetchone()
            tls = connection.execute("SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()").fetchone()
            require(identity == ("ha_" + node, node == "B", "on", plan["system_id"], "postgres") and tls == (True,))
            connection.rollback()


def run(args):
    plan_path = plain_file(Path(args.plan), 8192)
    approval_path = plain_file(Path(args.approval), 2048)
    report = Path(args.journal)
    authority = plan_path.parent
    require(authority.name == "pg-auth-probe-authority" and plan_path.name == "plan.json" and
            approval_path == authority / "approval.json" and
            report == authority / "journal/controller-state.json")
    require(plain_path(report.parent).is_dir() and not report.exists() and not report.is_symlink())
    raw = plan_path.read_bytes()
    plan = strict_json(raw)
    approval = strict_json(approval_path.read_bytes())
    pin = verify_plan(plan, approval, raw, time.time())
    require(service_identity() == plan["service_sid"])
    require(not any(key.upper().startswith("PG") for key in os.environ))
    import psycopg
    authenticate(plan, authority.parent / "db-auth-material", psycopg.connect)
    require(time.time() < approval["expires_unix"])
    result = {"scope": SCOPE, "plan_sha256": pin, "nodes": ["A", "B"],
              "service_identity_verified": True, "tls_verified": True, "authority_opened": False}
    with report.open("x", encoding="ascii") as stream:
        json.dump(result, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("authenticate",))
    parser.add_argument("--plan", required=True)
    parser.add_argument("--approval", required=True)
    parser.add_argument("--journal", required=True)
    try:
        run(parser.parse_args())
    except Exception:
        # A libpq exception can contain the endpoint and filesystem paths.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
