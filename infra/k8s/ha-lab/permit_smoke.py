"""Actual-role privilege, session-loss and stale-node checks; no host effects."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path


def run(phase):
    import psycopg
    import ha_runtime
    from ha_runtime import db
    from ha_runtime.config import load
    from ha_runtime.proof import raw_connection
    service = "crawler-worker"
    if phase == "stale":
        for operation in (lambda: ha_runtime.connect(service, "state"), lambda: ha_runtime.require_worker(service)):
            try:
                operation()
            except ha_runtime.DatabaseUnavailable:
                continue
            raise AssertionError("stale_node_admitted")
        return {"status": "PASS", "old_node_denied": True}

    raw = raw_connection(load(service), "primary")
    denied = 0
    try:
        for query in ("UPDATE ha.control SET writes_open=false",
                      "SELECT * FROM s_book_memo_main.books",
                      "DELETE FROM s_crawler_worker_state.ha_receipts"):
            try:
                raw.execute(query)
            except psycopg.errors.InsufficientPrivilege:
                denied += 1
            else:
                raise AssertionError("app_privilege_escalation")
    finally:
        raw.close()

    def successor():
        with ha_runtime.worker_scope(service):
            return True

    lost_denied = 0
    try:
        with ha_runtime.worker_scope(service):
            held = db._LOCAL.workers[service]
            pid = held.info.backend_pid
            with psycopg.connect(**json.loads(Path("/fixture/admin.json").read_text()), autocommit=True) as admin:
                if admin.execute("SELECT pg_terminate_backend(%s)", (pid,)).fetchone() != (True,):
                    raise AssertionError("session_fault_not_injected")
            with ThreadPoolExecutor(max_workers=1) as executor:
                if not executor.submit(successor).result(timeout=5):
                    raise AssertionError("successor_not_admitted")
            for operation in (lambda: ha_runtime.require_worker(service), lambda: ha_runtime.connect(service, "state")):
                try:
                    operation()
                except ha_runtime.DatabaseUnavailable:
                    lost_denied += 1
                else:
                    raise AssertionError("lost_session_reused")
    except ha_runtime.DatabaseUnavailable:
        lost_denied += 1
    if denied != 3 or lost_denied != 3:
        raise AssertionError("permit_checks_incomplete")
    return {"status": "PASS", "privilege_denials": denied, "lost_session_denials": lost_denied,
            "successor_acquired": True, "external_effects": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("active", "stale"), required=True)
    phase = parser.parse_args().phase
    try:
        print(json.dumps(run(phase), sort_keys=True))
    except Exception as error:
        print(json.dumps({"status": "FAIL", "phase": phase, "error_class": type(error).__name__}))
        raise SystemExit(1) from None
