"""Real PG calls through the native backend; only the Windows actuator is a double."""
from contextlib import contextmanager
import json
from pathlib import Path
import time


def run():
    from ha_control.pg import PgBackend
    from ha_control.controller import TransitionBlocked
    import psycopg
    admin = json.loads(Path("/fixture/admin.json").read_text())
    ca = Path("/fixture/controller-ca.crt")
    ca.write_bytes(Path(admin["sslrootcert"]).read_bytes())
    ca.chmod(0o600)
    endpoints = {node: dict(admin, host="ha-" + node.lower(), sslrootcert=str(ca)) for node in ("A", "B")}
    with psycopg.connect(**admin, autocommit=True) as connection:
        # Explicit fixture handoff: the prior generic app test served A/B.
        # Native manual failover's recorded authority serves the promoted B
        # alone. Prepare that initial contract without weakening the adapter.
        connection.execute("BEGIN")
        connection.execute("SELECT pg_advisory_xact_lock(7142026110601)")
        connection.execute("UPDATE ha.control SET serving=ARRAY['B']::text[],worker='B',revision=revision+1")
        connection.execute("COMMIT")
        cluster, epoch = connection.execute("SELECT cluster,epoch FROM ha.control").fetchone()
        system = connection.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0]
    plan = dict(cluster=cluster, node="A", generation=epoch, timeline=1, system_id=system,
                pg_nodes=endpoints, _approval_action="ha-transition")
    class LocalFenceDouble:
        plan_data = plan
        guard_entries = 0
        guard_exits = 0
        @contextmanager
        def guard(self):
            self.guard_entries += 1
            try:
                yield
            finally:
                self.guard_exits += 1
        def fenced(self, state): return True
        def fence(self, state): raise AssertionError("actual_host_fence_must_be_external")
    backend = PgBackend("/fixture/controller-plan.json", "/fixture/controller-approval.json", fence=LocalFenceDouble())
    state = dict(cluster=cluster, generation=epoch, old="A", target="B", timeline=1, phase="validating", attempts=1)
    with backend.guard():
        backend.close(state)
        deadline = time.monotonic() + 5
        while not backend.rejoined(state):
            if time.monotonic() >= deadline:
                raise TransitionBlocked("new_standby_required")
            time.sleep(0.02)
        backend.validate(state)
        backend.open(state)
        # A healthy provider may hold the worker's shared role permit for
        # seconds. Same-generation TTL renewal must coexist with that permit;
        # exclusive role changes must still be excluded.
        held_from = time.monotonic()
        with psycopg.connect(**admin, autocommit=True) as worker:
            worker.execute("SELECT pg_advisory_lock_shared(7142026110601)")
            backend.renew(state)
            with psycopg.connect(**admin, autocommit=True) as contender:
                contender.execute("SET lock_timeout='200ms'")
                try:
                    contender.execute("SELECT pg_advisory_xact_lock(7142026110601)")
                except psycopg.errors.LockNotAvailable:
                    exclusive_denied = True
                else:
                    raise TransitionBlocked("role_change_not_excluded")
            time.sleep(max(0, 2 - (time.monotonic() - held_from)))
        backend.assert_ready(state)
        with psycopg.connect(**admin, autocommit=True) as connection:
            value = connection.execute("SELECT writes_open AND history_safe AND expires_at>clock_timestamp() AND expires_at<clock_timestamp()+interval '6 seconds' FROM ha.control").fetchone()[0]
        if not value:
            raise TransitionBlocked("authority_lease_not_open")
        counts = []
        for node in ("A", "B"):
            with psycopg.connect(**endpoints[node], autocommit=True) as connection:
                counts.append(connection.execute("SELECT count(*) FROM ha.controller_probes").fetchone()[0])
        if counts != [2, 2]:
            raise TransitionBlocked("controller_probe_growth")
        backend.close(state)
        try:
            backend.renew(state)
        except TransitionBlocked:
            closed_denied = True
        else:
            raise TransitionBlocked("closed_authority_renewed")
        state["phase"] = "ready"
        backend.arm(state)
        time.sleep(5.1)
        try:
            backend.renew(state)
        except TransitionBlocked:
            expired_denied = True
        else:
            raise TransitionBlocked("expired_authority_renewed")
        backend.arm(state)
        backend.assert_ready(state)
        backend.close(state)
    if backend.fence_adapter.guard_entries != 1 or backend.fence_adapter.guard_exits != 1:
        raise TransitionBlocked("guard_scope_changed")
    return {"status": "PASS", "real_pg_backend": True, "windows_actuator": "test_double",
            "fresh_proof": True, "short_lease": True, "closed_after_test": True,
            "shared_worker_permit_seconds": 2, "renew_while_worker_active": True,
            "exclusive_role_change_denied": exclusive_denied, "closed_renew_denied": closed_denied,
            "expired_renew_denied": expired_denied, "explicit_arm_reopened": True,
            "single_guard_scope": True,
            "probe_rows_primary_and_partner": counts}


if __name__ == "__main__":
    try:
        print(json.dumps(run(), sort_keys=True))
    except Exception as error:
        from ha_control.controller import TransitionBlocked
        import traceback
        result = {"status": "FAIL", "error_class": type(error).__name__,
                  "frames": [{"file": Path(f.filename).name, "function": f.name, "line": f.lineno} for f in traceback.extract_tb(error.__traceback__)]}
        if isinstance(error, TransitionBlocked): result["runtime_reason"] = str(error)
        print(json.dumps(result))
        raise SystemExit(1) from None
