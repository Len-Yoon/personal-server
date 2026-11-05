#!/usr/bin/env python3
"""Opt-in Mac-only PG sync/cancellation fixture, separate from all app runtimes.

No published ports, host data mounts, production credentials, V2 resources or
production data. Requires an existing local postgres:17-bookworm image. This is
DB behavior evidence, not an application/fencing/HA acceptance test.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


class LabError(RuntimeError):
    pass


def run_lab() -> dict:
    # Pin the CLI context on every command; never inherit DOCKER_HOST overrides.
    environment = {k: v for k, v in os.environ.items() if not k.startswith("DOCKER_")}
    environment.pop("CONTAINER_HOST", None)

    def docker(*args, timeout=45, check=True):
        result = subprocess.run(["docker", "--context", "orbstack", *args], env=environment,
                                capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            raise LabError("docker_operation_failed_" + args[0])
        return result

    contexts = json.loads(docker("context", "inspect", "orbstack").stdout)
    expected = "unix://" + str(Path.home() / ".orbstack/run/docker.sock")
    if contexts[0]["Endpoints"]["docker"]["Host"] != expected:
        raise LabError("local_context_required")
    image = docker("image", "inspect", "postgres:17-bookworm", "--format", "{{.Id}}").stdout.strip()
    if not image.startswith("sha256:") or len(image) != 71:
        raise LabError("invalid_local_image")
    owner = str(uuid.uuid4())
    label = "personal-server.ha-lab-owner=" + owner
    network = "n100-ha-lab-" + owner
    containers = []
    network_id = None
    writer = None
    report = {"scope": "isolated_pg_behavior_only", "image": image, "production_access": False}

    def assert_owned(kind, identifier):
        inspected = json.loads(docker(kind, "inspect", identifier).stdout)[0]
        labels = inspected.get("Config", {}).get("Labels", {}) if kind == "container" else inspected.get("Labels", {})
        if labels.get("personal-server.ha-lab-owner") != owner or inspected["Id"] != identifier:
            raise LabError("ownership_mismatch")

    def sql(node, statement):
        assert_owned("container", node)
        return docker("exec", node, "psql", "-XAt", "-U", "postgres", "-v", "ON_ERROR_STOP=1", "-c", statement).stdout.strip()

    def wait_for(predicate, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except LabError:
                pass
            time.sleep(0.2)
        raise LabError("fixture_deadline_" + report.get("phase", "unknown"))

    def start(alias, command, *, initialize=False):
        args = ["create", "--pull=never", "--label", label,
                "--network", network_id, "--network-alias", alias,
                "--memory", "256m", "--cpus", "0.5",
                "--tmpfs", "/var/lib/postgresql/data:rw,uid=999,gid=999,mode=0700,size=192m"]
        if initialize:
            args += ["-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_INITDB_ARGS=--data-checksums"]
        else:
            args += ["--user", "999:999", "--entrypoint", "bash"]
        identifier = docker(*args, image, *command).stdout.strip()
        containers.append(identifier)
        assert_owned("container", identifier)
        docker("start", identifier)
        return identifier

    try:
        network_id = docker("network", "create", "--internal", "--label", label, network).stdout.strip()
        assert_owned("network", network_id)
        tuning = ["-c", "shared_buffers=16MB", "-c", "max_connections=12"]
        report["phase"] = "primary_start"
        a = start("ha-primary", ["postgres", *tuning, "-c", "wal_level=replica",
                                "-c", "max_wal_senders=3", "-c", "wal_sender_timeout=2000",
                                "-c", "synchronous_commit=on"], initialize=True)
        # initdb briefly starts a temporary socket server. Do not mistake it for
        # the final postmaster: wait for entrypoint to exec postgres as PID 1.
        wait_for(lambda: docker("exec", a, "cat", "/proc/1/comm").stdout.strip() == "postgres"
                 and sql(a, "SELECT 1") == "1")
        sql(a, "CREATE TABLE requests (id integer PRIMARY KEY, result text NOT NULL)")
        docker("exec", a, "bash", "-ceu",
               "printf 'host replication postgres all trust\\n' >> /var/lib/postgresql/data/pg_hba.conf")
        sql(a, "SELECT pg_reload_conf()")
        # Empty tmpfs is owned by postgres; no host directory is read or written.
        report["phase"] = "standby_start"
        b = start("ha-standby", ["-ceu",
            "pg_basebackup -d 'host=ha-primary user=postgres application_name=ha_b' "
            "-D /var/lib/postgresql/data -R -X stream; "
            "exec postgres -D /var/lib/postgresql/data -c shared_buffers=16MB -c max_connections=12 "
            "-c wal_receiver_timeout=2000 -c wal_retrieve_retry_interval=1000"])
        wait_for(lambda: sql(b, "SELECT pg_is_in_recovery()") == "t")
        sql(a, "ALTER SYSTEM SET synchronous_standby_names = 'FIRST 1 (ha_b)'")
        sql(a, "SELECT pg_reload_conf()")
        report["phase"] = "sync_pair"
        wait_for(lambda: sql(a, "SELECT count(*) FROM pg_stat_replication WHERE application_name='ha_b' AND sync_state='sync'") == "1")
        sql(a, "INSERT INTO requests VALUES (1,'healthy')")
        wait_for(lambda: sql(b, "SELECT count(*) FROM requests WHERE id=1") == "1")
        report["healthy_sync_row_on_both"] = True

        # Network isolation preserves standby data, unlike stop/start with tmpfs.
        assert_owned("network", network_id)
        docker("network", "disconnect", network_id, b)
        report["phase"] = "sender_disconnect"
        wait_for(lambda: sql(a, "SELECT count(*) FROM pg_stat_replication") == "0")
        writer = subprocess.Popen(
            ["docker", "--context", "orbstack", "exec", a, "psql", "-XAt", "-U", "postgres",
             "-v", "ON_ERROR_STOP=1", "-c",
             "SET application_name='ha_cancel_writer'; BEGIN; INSERT INTO requests VALUES (2,'uncertain'); COMMIT;"],
            env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        report["phase"] = "writer_wait"
        wait_for(lambda: sql(a, "SELECT count(*) FROM pg_stat_activity WHERE application_name='ha_cancel_writer' AND wait_event='SyncRep'") == "1")
        sql(a, "SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name='ha_cancel_writer' AND wait_event='SyncRep'")
        output, warning = writer.communicate(timeout=10)
        report["cancel_warning_received"] = "canceling wait for synchronous replication" in warning
        report["psql_exit_after_cancel"] = writer.returncode
        report["local_commit_survived_cancel"] = sql(a, "SELECT count(*) FROM requests WHERE id=2") == "1"
        report["disconnected_partner_has_no_row"] = sql(b, "SELECT count(*) FROM requests WHERE id=2") == "0"
        if not all(report[key] for key in ("cancel_warning_received", "local_commit_survived_cancel", "disconnected_partner_has_no_row")):
            raise LabError("cancellation_contract_not_reproduced")

        assert_owned("container", b)
        docker("network", "connect", "--alias", "ha-standby", network_id, b)
        report["phase"] = "partner_rejoin"
        wait_for(lambda: sql(a, "SELECT count(*) FROM pg_stat_replication WHERE application_name='ha_b' AND sync_state='sync'") == "1")
        # Fresh WAL-producing barrier, then verify both request and barrier.
        sql(a, "INSERT INTO requests VALUES (3,'rearmed-barrier')")
        wait_for(lambda: sql(b, "SELECT count(*) FROM requests WHERE id IN (2,3)") == "2")
        report["fresh_barrier_and_uncertain_row_replicated"] = True
    finally:
        original_error = sys.exc_info()[1]
        if writer is not None and writer.poll() is None:
            writer.terminate()
            try:
                writer.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                writer.kill()
                writer.communicate(timeout=5)
        # A create response can be lost after Docker created the resource. Find
        # owned resources by this run's random label; never retry the creation.
        cleanup_failed = False
        try:
            discovered = docker("ps", "-aq", "--no-trunc", "--filter", "label=" + label).stdout.split()
        except (LabError, subprocess.TimeoutExpired):
            discovered, cleanup_failed = [], True
        for identifier in reversed(list(dict.fromkeys(containers + discovered))):
            try:
                assert_owned("container", identifier)
                docker("rm", "-f", "-v", identifier)
            except (LabError, subprocess.TimeoutExpired):
                cleanup_failed = True
        try:
            networks = docker("network", "ls", "-q", "--no-trunc", "--filter", "label=" + label).stdout.split()
        except (LabError, subprocess.TimeoutExpired):
            networks, cleanup_failed = [], True
        for identifier in dict.fromkeys(([network_id] if network_id else []) + networks):
            try:
                assert_owned("network", identifier)
                docker("network", "rm", identifier)
            except (LabError, subprocess.TimeoutExpired):
                cleanup_failed = True
        if cleanup_failed:
            original_reason = (str(original_error) if isinstance(original_error, LabError)
                               else type(original_error).__name__ if original_error else "none")
            raise LabError(original_reason + ";owned_fixture_cleanup_incomplete") from None
    report["owned_fixture_removed"] = True
    return report


if __name__ == "__main__":
    try:
        print(json.dumps(run_lab(), sort_keys=True))
    except (LabError, OSError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired) as error:
        # Never echo raw Docker/PG errors: they can contain connection metadata.
        reason = str(error) if isinstance(error, LabError) else type(error).__name__
        print(json.dumps({"scope": "isolated_pg_behavior_only", "status": "FAIL", "reason": reason}))
        raise SystemExit(1)
