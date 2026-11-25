"""Read-only proof of both closed DBs for the Windows authority starter."""

import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
from contextlib import ExitStack

LIMIT_SECONDS = 6


def pin(raw):
    return hashlib.sha256(raw).hexdigest()


def _read(path, limit):
    path = Path(path)
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or path.resolve(strict=True) != path.absolute()):
        raise ValueError("untrusted_pair_proof_input")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("changed_pair_proof_input")
        data = stream.read(limit + 1)
    if not 0 < len(data) <= limit:
        raise ValueError("pair_proof_input_size")
    return data


def _checked_pin(value):
    if type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("invalid_pair_proof_pin")
    return value


def _run_probe(operation):
    # Includes bundle validation, connection establishment and queries. The
    # standalone process never writes DB state, even when forcibly timed out.
    watchdog = threading.Timer(LIMIT_SECONDS, lambda: os._exit(124))
    watchdog.daemon = True
    watchdog.start()
    started = time.monotonic()
    try:
        result = operation()
        if time.monotonic() - started >= LIMIT_SECONDS:
            raise ValueError("pair_proof_deadline")
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception:
        # PostgreSQL exceptions and plans can contain private connection data.
        print("initial_closed_pair_unverified", file=sys.stderr)
        return 1
    finally:
        watchdog.cancel()


def query_closed_pair(plan, revision, plan_pin, terminal_pin, challenge):
    """Verify the exact installed cluster and synchronous closed revision."""
    from ha_control.initial_pg_lease import InitialPgLeaseRecovery
    from ha_control.pg import _private_file
    from ha_runtime.effects import Binding, _binding, validate_catalog
    import psycopg

    if sys.flags.optimize:
        raise ValueError("pair_proof_requires_assertions")
    endpoints = plan["pg_nodes"]
    fields = {"host", "port", "dbname", "user", "sslmode", "sslrootcert", "passfile"}
    if (type(endpoints) is not dict or set(endpoints) != {"A", "B"}
            or any(type(endpoint) is not dict or set(endpoint) != fields or endpoint["sslmode"] != "verify-full"
                   for endpoint in endpoints.values())
            or (endpoints["A"]["host"], endpoints["A"]["port"]) == (endpoints["B"]["host"], endpoints["B"]["port"])):
        raise ValueError("authenticated_pair_endpoints_required")
    for endpoint in endpoints.values():
        _private_file(endpoint["sslrootcert"])
        _private_file(endpoint["passfile"])
    expected=plan['pg_enforcement']['effects']['deployment_binding']
    assert expected['plan_sha256']==plan['initial_cutover_handoff']['legacy_plan_sha256']
    quarantine=InitialPgLeaseRecovery.quarantine_binding(plan)
    binding=Binding(plan['cluster'],str(plan['system_id']),expected['source_commit'],
                    expected['plan_sha256'],plan['pg_enforcement']['effects']['broker_role'],broker_instance='0'*64)
    def lsn(v):
        assert type(v) is str and re.fullmatch('[0-9A-F]+/[0-9A-F]+',v)
        a,b=v.split('/')
        return (int(a,16)<<32)+int(b,16)
    with ExitStack() as stack:
        nodes={n:stack.enter_context(psycopg.connect(**p,connect_timeout=2,autocommit=True,gssencmode='disable',
              options='-c default_transaction_read_only=on -c statement_timeout=700 -c lock_timeout=500')) for n,p in endpoints.items()}
        facts={}
        for node,conn in nodes.items():
            conn.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
            assert conn.execute('SHOW transaction_read_only').fetchone()==('on',)
            validate_catalog(conn,binding)
            _binding(conn, binding)
            assert InitialPgLeaseRecovery._quarantine_observation(conn,renewal=True)==(quarantine[0],quarantine[2],quarantine[3])
            row=conn.execute("""SELECT c.cluster,(pg_control_system()).system_identifier::text,c.epoch,c.revision,c.timeline,
                c.primary_node,c.partner,c.serving,c.worker,c.mode,c.writes_open,c.history_safe,c.expires_at,
                c.expires_at<clock_timestamp(),pg_is_in_recovery(),current_setting('cluster_name'),
                current_setting('synchronous_commit'),current_setting('fsync'),current_setting('full_page_writes'),
                CASE WHEN pg_is_in_recovery() THEN pg_last_wal_replay_lsn() ELSE pg_current_wal_flush_lsn() END::text,
                regexp_replace(current_setting('synchronous_standby_names'),'[[:space:]"]','','g'),
                (SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()),current_setting('transaction_read_only'),
                CASE WHEN pg_is_in_recovery() THEN (SELECT received_tli FROM pg_stat_wal_receiver)
                ELSE ('x'||substring(pg_walfile_name(pg_current_wal_insert_lsn()),1,8))::bit(32)::integer END,
                host(inet_server_addr()) FROM ha.control c WHERE singleton=1""").fetchone()
            assert row[:12]==(plan['cluster'],str(plan['system_id']),1,revision,1,'A','B',['A'],'A','AS',False,False)
            assert isinstance(row[12],datetime.datetime) and row[12].utcoffset() is not None
            assert row[13:19]==(True,node=='B','ha_'+node,'remote_apply','on','on')
            assert row[20:24]==('FIRST1(ha_b)' if node=='A' else 'FIRST1(ha_a)',True,'on',1)
            facts[node]=row
        primary,replica=facts['A'],facts['B']
        assert primary[12]==replica[12] and 0<lsn(primary[19])<=lsn(replica[19])
        peers=nodes['A'].execute("""SELECT r.application_name,r.usename,host(r.client_addr),r.state,r.sync_state,s.ssl,
               r.replay_lsn::text FROM pg_stat_replication r JOIN pg_stat_ssl s USING(pid)""").fetchall()
        assert len(peers)==1 and peers[0][:6]==('ha_b','ha_repl',replica[24],'streaming','sync',True)
        assert lsn(peers[0][6])>=lsn(primary[19])
        recv=nodes['B'].execute("SELECT status,received_tli FROM pg_stat_wal_receiver").fetchall()
        assert recv==[('streaming',1)]
        proof={'challenge':challenge,'plan_sha256':plan_pin,'terminal_sha256':terminal_pin,
               'revision':revision,'expired_at':primary[12].isoformat(),'flush':primary[19],'replay':replica[19]}
        return {"closed_revision":revision, "proof_sha256":pin(json.dumps(proof,sort_keys=True,separators=(",",":")).encode()),
                "full_closed_pair_verified":True, "candidate_effects_binding_matches_both_databases":True,
                "exact_quarantine_matches":True}


def _verify_saved_reboot(plan_path, plan_pin, challenge):
    from ha_control.cli import validate_bundle
    from ha_control.initial_postboot import FILES, files_for, saved_reboot, scope, validate_seal
    from ha_control.initial_unpublished import derive_predecessor
    from ha_control.initial_windows_gate import _decode

    _checked_pin(plan_pin)
    _checked_pin(challenge)
    path = Path(plan_path)
    raw = _read(path, 65536)
    if pin(raw) != plan_pin:
        raise ValueError("saved_reboot_probe_plan_pin")
    plan = _decode(raw)
    if not saved_reboot(scope(plan)):
        raise ValueError("saved_reboot_probe_scope")
    validate_bundle(plan)
    files = {name: _read(path.parent / name, 65536 if name.endswith("predecessor-plan.json") else 8192)
             for name in files_for(plan)}
    scope_value = validate_seal(plan, plan_pin, files)
    old = _decode(files["postboot-predecessor-plan.json"])
    if any(plan[key] != old[key] for key in ("pg_nodes", "pg_enforcement", "cluster", "system_id")):
        raise ValueError("saved_reboot_probe_cluster_changed")
    terminal_pin = scope_value["predecessor_terminal_sha256"]
    revision, _, _ = derive_predecessor(files[FILES[0]], terminal_pin, published=True)
    fresh = query_closed_pair(plan, revision, plan_pin, terminal_pin, challenge)
    return {"challenge": challenge, "plan_sha256": plan_pin, "closed_revision": fresh["closed_revision"],
            "proof_sha256": fresh["proof_sha256"]}


def saved_reboot_main(argv):
    if len(argv) != 3:
        return 1
    return _run_probe(lambda: _verify_saved_reboot(*argv))


def _verify_archived_runtime(runtime, plan_path, plan_pin, terminal_path, terminal_pin, challenge, expected_revision):
    # The SSH reader has already authenticated the archived runtime and paths.
    sys.path.insert(0, str(Path(runtime) / "controller/files"))
    from ha_control.cli import validate_bundle
    from ha_control.initial_unpublished import derive_predecessor
    from ha_control.initial_windows_gate import _decode

    raw = _read(plan_path, 65536)
    terminal_raw = _read(terminal_path, 8192)
    if pin(raw) != _checked_pin(plan_pin) or pin(terminal_raw) != _checked_pin(terminal_pin):
        raise ValueError("pair_proof_archive_pin")
    _checked_pin(challenge)
    plan, terminal = _decode(raw), _decode(terminal_raw)
    revision, _, _ = derive_predecessor(terminal_raw, terminal_pin, published=True)
    if (terminal["controller_plan_sha256"] != plan_pin or terminal["postboot"] != plan["initial_recovery_postboot"]
            or revision != int(expected_revision) + 1):
        raise ValueError("pair_proof_archive_lineage")
    validate_bundle(plan)
    return query_closed_pair(plan, revision, plan_pin, terminal_pin, challenge)


if __name__ == "__main__":
    raise SystemExit(_run_probe(lambda: _verify_archived_runtime(*sys.argv[1:])))
