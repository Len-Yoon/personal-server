#!/usr/bin/env python3
"""Real PG promotion/rejoin and new-system restore; Mac fixture only.

Docker stop/inspect is the local test actuator. It is NOT a Windows fencing
acceptance test. The native Windows bridge has a separate approval boundary.
"""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "infra/k8s"))
from ha_control.controller import Controller, TransitionBlocked

spec = importlib.util.spec_from_file_location("actual_apps_fixture", Path(__file__).with_name("run.py"))
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


class TransitionFixture(lab.Fixture):
    def psql_at(self, identifier, statement, database="ha"):
        self.owned("container", identifier)
        return self.docker("exec", "-i", identifier, "psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", database,
                           input=statement, timeout=30)

    def psql(self, statement, database="ha"):
        return self.psql_at(getattr(self, "primary", self.a), statement, database)

    def update_configs(self, primary):
        self.configurations(primary)
        for runner in self.runners.values():
            self.docker("cp", str(self.directory / "fixture") + "/.", runner + ":/fixture")
            self.docker("exec", "--user", "0:0", runner, "chown", "-R", "10001:10001", "/fixture")

    def script(self, runner, script, arguments=(), service="portal-web", node="B", objects="/fixture/objects"):
        output = self.docker("exec", "-w", "/source/" + service, "-e", "PYTHONPATH=/source/infra/k8s:/source/" + service,
                             "-e", "APP_ENV=test", "-e", "HA_LOCAL_RUNTIME=1", "-e", "HA_PG_CONFIG=/fixture/config/" + service + "-" + node + ".json",
                             "-e", "HA_FILE_OBJECT_ROOT=" + objects, runner, "/venvs/" + service + "/bin/python",
                             "/source/infra/k8s/ha-lab/" + script, *arguments, timeout=120)
        value = json.loads(output.splitlines()[-1])
        if value.get("status") != "PASS":
            raise lab.FixtureError("transition_application_check_failed")
        return value

    def check_apps(self, stage, objects="/fixture/objects"):
        results = {}
        for service in lab.SERVICES:
            group = "311" if service in lab.GROUPS["311"] else "312"
            self.phase = stage + "_" + service
            results[service] = self.script(self.runners[group], "post_transition.py", ("--stage", stage, "--service", service), service=service, objects=objects)
        return results

    def restore_all(self):
        self.phase = "whole_backup"
        source_runner = self.runners["311"]
        self.report["whole_backup"] = self.script(source_runner, "restore_smoke.py", ("--phase", "backup"))
        # Keep the original cluster intact; a second private network isolates DNS
        # and a freshly initialized PG system from both old primary identities.
        restore_network = self.docker("network", "create", "--internal", "--label", self.label, "ha-restore-" + self.owner)
        self.owned("network", restore_network)
        self.original_primary, self.original_network = self.primary, self.network
        self.network = restore_network
        self.phase = "new_system_start"
        restored_a = self.start_pg("A")
        self.wait(lambda: self.docker("exec", restored_a, "cat", "/proc/1/comm") == "postgres" and self.psql_at(restored_a, "SELECT 1", "postgres") == "1")
        self.primary = restored_a
        self.psql("CREATE DATABASE ha", "postgres")
        for role, password in self.passwords.items():
            if role == "postgres":
                continue
            extra = " REPLICATION" if role == "ha_repl" else ""
            self.psql("CREATE ROLE " + role + " LOGIN" + extra + " PASSWORD '" + password + "'")
        clone_image = self.docker("container", "inspect", source_runner, "--format", "{{.Image}}")
        clone = self.docker("create", "--label", self.label, "--network", self.network, "--memory", "768m", "--cpus", "2", clone_image)
        self.docker("cp", source_runner + ":/fixture", str(self.directory / "saved-fixture"))
        self.docker("cp", str(self.directory / "saved-fixture") + "/.", clone + ":/fixture")
        self.docker("start", clone)
        self.docker("exec", "--user", "0:0", clone, "chown", "-R", "10001:10001", "/fixture")
        # The clone's admin must point to the newly initialized A, not old B.
        self.system_id = self.psql("SELECT system_identifier::text FROM pg_control_system()")
        self.runners = {"311": clone, "312": self.runners["312"]}
        self.configurations("A")
        self.docker("cp", str(self.directory / "fixture/config") + "/.", clone + ":/fixture/config")
        self.docker("cp", str(self.directory / "fixture/admin.json"), clone + ":/fixture/admin.json")
        self.docker("exec", "--user", "0:0", clone, "chown", "-R", "10001:10001", "/fixture")
        self.phase = "whole_restore"
        self.docker("exec", clone, "mkdir", "-m", "700", "/fixture/restored-objects")
        self.report["whole_restore"] = self.script(clone, "restore_smoke.py", ("--phase", "restore", "--objects", "/fixture/restored-objects"))
        if self.psql("SELECT writes_open OR history_safe OR expires_at>clock_timestamp() FROM ha.control") != "f":
            raise lab.FixtureError("restored_old_authority_not_sealed")
        self.report["restored_old_authority_denied"] = self.script(clone, "permit_smoke.py", ("--phase", "stale"), service="crawler-worker", node="A")
        self.psql("BEGIN; SET LOCAL synchronous_commit=local; UPDATE ha.control SET primary_node='A',partner='B',timeline=1,epoch=epoch+1,revision=revision+1,serving=ARRAY['A','B'],worker='B',mode='AA',expires_at=clock_timestamp()+interval '2 hours'; COMMIT")
        self.docker("exec", restored_a, "bash", "-ceu", "printf '%s\\n' 'hostssl replication ha_repl all scram-sha-256' >> /var/lib/postgresql/data/pg_hba.conf")
        self.psql("SELECT pg_reload_conf()")
        self.a = restored_a
        self.b = self.start_pg("B")
        self.wait(lambda: self.psql("SELECT count(*) FROM pg_stat_replication WHERE application_name='ha_b' AND state='streaming'") == "1")
        self.psql("ALTER SYSTEM SET synchronous_standby_names='FIRST 1 (ha_b)'")
        self.psql("SELECT pg_reload_conf()")
        self.wait(lambda: self.psql("SELECT count(*) FROM pg_stat_replication WHERE application_name='ha_b' AND sync_state='sync'") == "1")
        # Only the disposable fixture owner may rearm its newly recreated pair.
        self.psql("BEGIN; SELECT pg_advisory_xact_lock(7142026110601); UPDATE ha.control SET writes_open=true,history_safe=true; COMMIT")
        # Create a separate 3.12 clone in the NEW network as well.
        old312 = self.runners["312"]
        image312 = self.docker("container", "inspect", old312, "--format", "{{.Image}}")
        clone312 = self.docker("create", "--label", self.label, "--network", self.network, "--memory", "768m", "--cpus", "2", image312)
        self.docker("cp", str(self.directory / "fixture") + "/.", clone312 + ":/fixture")
        self.docker("start", clone312)
        self.docker("exec", "--user", "0:0", clone312, "chown", "-R", "10001:10001", "/fixture")
        self.runners["312"] = clone312
        self.report["restored_apps"] = self.check_apps("restored", objects="/fixture/restored-objects")
        self.report["restored_retry"] = self.script(clone, "retry_smoke.py", ("--phase", "reprotected"), service="book-memo")


class LocalBackend:
    def __init__(self, fixture):
        self.f = fixture
        self.old = fixture.a
        self.checked_at = None

    @contextmanager
    def guard(self):
        # The native Windows guard is NOT simulated as accepted here.
        yield

    def close(self, state):
        self.f.psql("BEGIN; SET LOCAL synchronous_commit=local; SELECT pg_advisory_xact_lock(7142026110601); UPDATE ha.control SET writes_open=false,history_safe=false; COMMIT")

    def fenced(self, state):
        self.f.owned("container", self.old)
        value = json.loads(self.f.docker("container", "inspect", self.old))[0]
        return value["State"]["Running"] is False and value["HostConfig"]["RestartPolicy"]["Name"] in {"no", ""}

    def fence(self, state):
        self.f.docker("stop", "--time", "5", self.old)
        self.f.docker("network", "disconnect", self.f.network, self.old)

    def promoted(self, state):
        return self.f.psql_at(self.f.b, "SELECT NOT pg_is_in_recovery()") == "t"

    def promote(self, state):
        if not self.fenced(state):
            raise TransitionBlocked("positive_fence_required")
        if self.f.psql_at(self.f.b, "SELECT pg_promote(true,5)") != "t":
            raise TransitionBlocked("promotion_unconfirmed")
        self.f.primary = self.f.b
        self.f.psql("ALTER SYSTEM SET synchronous_standby_names='FIRST 1 (ha_a)'")
        self.f.psql("SELECT pg_reload_conf()")
        self.f.psql("BEGIN; SET LOCAL synchronous_commit=local; UPDATE ha.control SET primary_node='B',partner='A',timeline=2,epoch=" + str(state["generation"]) + ",revision=revision+1,writes_open=false,history_safe=false; COMMIT")

    def rejoin(self, state):
        self.f.a = self.f.start_pg("A", source="B")
        self.f.wait(lambda: self.f.psql("SELECT count(*) FROM pg_stat_replication WHERE application_name='ha_a' AND sync_state='sync'") == "1")

    def rejoined(self, state):
        return self.f.a != self.old and self.f.psql("SELECT count(*) FROM pg_stat_replication WHERE application_name='ha_a' AND sync_state='sync'") == "1"

    def validate(self, state):
        # A committed WAL-producing control check is covered by a following
        # synchronous commit, then compared directly with the exact receiver.
        self.f.psql("SET synchronous_commit=on; CREATE TABLE IF NOT EXISTS ha.transition_check(id integer PRIMARY KEY); INSERT INTO ha.transition_check VALUES(1) ON CONFLICT DO NOTHING")
        watermark = self.f.psql("SELECT pg_current_wal_insert_lsn()::text")
        self.f.psql("UPDATE ha.transition_check SET id=id WHERE id=1")
        peer = self.f.psql_at(self.f.a, "SELECT pg_is_in_recovery() AND received_tli=2 AND pg_last_wal_receive_lsn()>='" + watermark + "'::pg_lsn AND pg_last_wal_replay_lsn()>='" + watermark + "'::pg_lsn FROM pg_stat_wal_receiver")
        if peer != "t" or self.f.psql("SELECT system_identifier::text FROM pg_control_system()") != self.f.system_id:
            raise TransitionBlocked("fresh_peer_proof_unavailable")
        self.checked_at = time.monotonic()

    def open(self, state):
        if self.checked_at is None or time.monotonic() - self.checked_at > 2 or not self.fenced(state):
            raise TransitionBlocked("fresh_peer_proof_unavailable")
        self.f.psql("BEGIN; SELECT pg_advisory_xact_lock(7142026110601); UPDATE ha.control SET serving=ARRAY['A','B'],worker='B',mode='AA',writes_open=true,history_safe=true; COMMIT")
        self.f.update_configs("B")

    def assert_ready(self, state):
        if not self.fenced(state) or self.f.psql("SELECT primary_node='B' AND timeline=2 AND writes_open AND history_safe FROM ha.control") != "t":
            raise TransitionBlocked("new_authority_unconfirmed")


def main():
    with tempfile.TemporaryDirectory(prefix="n100-ha-transition-") as temporary:
        fixture = TransitionFixture(Path(temporary).resolve())
        error = None
        try:
            fixture.prepare()
            fixture.databases()
            fixture.build()
            fixture.applications()
            fixture.primary = fixture.a
            fixture.phase = "fence_promote_rejoin"
            control = Controller(fixture.directory / "controller.json", LocalBackend(fixture))
            control.initialize(cluster=fixture.cluster, generation=3, old="A", target="B", timeline=1)
            state = control.advance()
            fixture.report["db_transition"] = {"phase": state["phase"], "timeline": 2,
                                                "old_primary_fenced": True, "new_standby_rejoined": True,
                                                "native_windows_test": False}
            fixture.report["promoted_apps"] = fixture.check_apps("promoted")
            fixture.phase = "native_pg_adapter"
            fixture.report["native_pg_adapter"] = fixture.script(fixture.runners["311"], "native_pg_smoke.py")
            # Restore the local fixture's longer synthetic authority for the
            # independent full-backup/restore stage, after the native 5s test.
            fixture.psql("BEGIN; SELECT pg_advisory_xact_lock(7142026110601); UPDATE ha.control SET writes_open=true,history_safe=true,expires_at=clock_timestamp()+interval '2 hours'; COMMIT")
            fixture.restore_all()
        except Exception as value:
            error = type(value).__name__ + ":" + (str(value) if isinstance(value, (lab.FixtureError, TransitionBlocked)) else fixture.phase)
        finally:
            cleaned = fixture.cleanup()
        fixture.report.update(status="FAIL" if error or not cleaned else "PASS", error=error, owned_fixture_removed=cleaned)
        print(json.dumps(fixture.report, sort_keys=True))
        return int(error is not None or not cleaned)


if __name__ == "__main__":
    raise SystemExit(main())
