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
        cluster, epoch = connection.execute("SELECT cluster,epoch FROM ha.control").fetchone()
        system = connection.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0]
    plan = dict(cluster=cluster, node="A", generation=epoch, timeline=1, system_id=system,
                pg_nodes=endpoints, _approval_action="ha-transition")
    class LocalFenceDouble:
        plan_data = plan
        @contextmanager
        def guard(self): yield
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
        backend.assert_ready(state)
        with psycopg.connect(**admin, autocommit=True) as connection:
            value = connection.execute("SELECT writes_open AND history_safe AND expires_at>clock_timestamp() AND expires_at<clock_timestamp()+interval '6 seconds' FROM ha.control").fetchone()[0]
        if not value:
            raise TransitionBlocked("authority_lease_not_open")
        backend.close(state)
    return {"status": "PASS", "real_pg_backend": True, "windows_actuator": "test_double",
            "fresh_proof": True, "short_lease": True, "closed_after_test": True}


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
