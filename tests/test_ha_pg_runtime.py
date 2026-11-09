"""Local guards and uncertainty boundaries; actual PG evidence is separate."""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/k8s"))
from ha_runtime import config, db, migrate, proof


class FixtureRuntimeContextTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('ha_fixture_context_runner',ROOT/'infra/k8s/ha-lab/run.py')
        self.runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.runner)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        self.platform=patch.object(self.runner.sys,'platform','darwin');self.platform.start();self.addCleanup(self.platform.stop)

    def endpoint(self,context):
        if context=='orbstack':return 'unix://'+str(Path.home()/'.orbstack/run/docker.sock')
        return 'unix://'+str(Path.home()/'.colima'/context.removeprefix('colima-')/'docker.sock')

    def response(self,context,host=None):
        return json.dumps([{'Name':context,'Endpoints':{'docker':{'Host':host or self.endpoint(context)}}}])

    def test_only_explicit_local_contexts_and_no_default_context_mutation(self):
        context='colima-codex-ha-01a1add8'
        fixture=self.runner.Fixture(self.directory,runtime_context=context)
        with patch.object(self.runner.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='ok',stderr='')) as run:
            with patch.dict(os.environ,{'DOCKER_HOST':'tcp://remote','DOCKER_CONTEXT':'remote'}):
                fixture=self.runner.Fixture(self.directory,runtime_context=context)
                fixture.docker('version')
            self.assertEqual(run.call_args.args[0],['docker','--context',context,'version'])
            self.assertFalse(any(key.startswith('DOCKER_') for key in run.call_args.kwargs['env']))
        self.assertEqual(self.runner.Fixture(self.directory).context,'orbstack')
        for value in ('default','remote','tcp://daemon','colima','colima-default','colima-codex-ha-01a1add8/../x',
                      'colima-codex-ha-ABCDEF01','--context=orbstack',None):
            with self.assertRaisesRegex(self.runner.FixtureError,'local_context_required'):
                self.runner.Fixture(self.directory,runtime_context=value)

    def test_runtime_identity_available_before_creation_and_copy_cannot_change_pin(self):
        for context in ('orbstack','colima-codex-ha-01a1add8'):
            fixture=self.runner.Fixture(self.directory,runtime_context=context)
            with patch.object(fixture,'docker',side_effect=[self.response(context),'daemon-123']) as docker:
                marker=fixture.validate_runtime()
            self.assertEqual(marker['context'],context);self.assertEqual(marker['daemon_id'],'daemon-123')
            self.assertRegex(marker['local_socket_sha256'],'^[0-9a-f]{64}$')
            marker['daemon_id']='changed'
            self.assertEqual(fixture.runtime_identity['daemon_id'],'daemon-123')
            self.assertEqual(fixture.report['runtime_identity'],fixture.runtime_identity)
            self.assertFalse((self.directory/'fixture').exists())
            self.assertEqual([call.args for call in docker.call_args_list],[('context','inspect',context),('info','--format','{{.ID}}')])

    def test_remote_alias_other_profile_or_wrong_context_rejected_before_creation(self):
        context='colima-codex-ha-01a1add8'
        cases=[self.response(context,'tcp://127.0.0.1:2375'),
               self.response(context,'unix:///private/tmp/alias.sock'),
               self.response(context,'unix://'+str(Path.home()/'.colima/default/docker.sock')),
               self.response('orbstack'),json.dumps([])]
        for value in cases:
            fixture=self.runner.Fixture(self.directory,runtime_context=context)
            with patch.object(fixture,'docker',return_value=value) as docker,patch.object(self.runner.subprocess,'run') as process:
                with self.assertRaisesRegex(self.runner.FixtureError,'local_context_required'):fixture.prepare()
                self.assertEqual(docker.call_count,1);process.assert_not_called()
                self.assertTrue(fixture.cleanup())
            self.assertIsNone(fixture.runtime_identity)
            self.assertFalse((self.directory/'fixture').exists())

    def test_changed_daemon_blocks_prepare_and_cleanup_without_resource_commands(self):
        context='colima-codex-ha-01a1add8';fixture=self.runner.Fixture(self.directory,runtime_context=context)
        with patch.object(fixture,'docker',side_effect=[self.response(context),'original']):fixture.validate_runtime()
        for action in ('prepare','cleanup'):
            with patch.object(fixture,'docker',side_effect=[self.response(context),'replacement']) as docker:
                if action=='prepare':
                    with self.assertRaisesRegex(self.runner.FixtureError,'fixture_runtime_identity_changed'):fixture.prepare()
                else:self.assertFalse(fixture.cleanup())
                self.assertEqual([call.args[0] for call in docker.call_args_list],['context','info'])
        self.assertEqual(fixture.runtime_identity['daemon_id'],'original')
        self.assertFalse((self.directory/'fixture').exists())

    def test_cleanup_keeps_uuid_label_scope_after_same_daemon_revalidation(self):
        context='colima-codex-ha-01a1add8';fixture=self.runner.Fixture(self.directory,runtime_context=context)
        with patch.object(fixture,'docker',side_effect=[self.response(context),'original']):fixture.validate_runtime()
        with patch.object(fixture,'docker',side_effect=[self.response(context),'original','','','']) as docker:
            self.assertTrue(fixture.cleanup())
        for call in docker.call_args_list[2:]:
            self.assertIn('label='+fixture.label,call.args)
        self.assertFalse(any(call.args[0] in {'rm','start','restart','prune'} for call in docker.call_args_list))

    def test_nonmac_and_unavailable_daemon_cannot_create_or_cleanup_resources(self):
        fixture=self.runner.Fixture(self.directory)
        with patch.object(self.runner.sys,'platform','linux'),patch.object(fixture,'docker') as docker:
            with self.assertRaisesRegex(self.runner.FixtureError,'mac_local_context_required'):fixture.prepare()
            self.assertTrue(fixture.cleanup());docker.assert_not_called()
        for daemon in ('','space separated','private\nvalue'):
            fixture=self.runner.Fixture(self.directory)
            with patch.object(fixture,'docker',side_effect=[self.response('orbstack'),daemon]):
                with self.assertRaisesRegex(self.runner.FixtureError,'local_daemon_identity_required'):fixture.validate_runtime()
                self.assertTrue(fixture.cleanup())


class PGGuardTests(unittest.TestCase):
    def test_fixture_renders_literal_pinned_existing_app_base(self):
        spec = importlib.util.spec_from_file_location("ha_fixture_runner", ROOT / "infra/k8s/ha-lab/run.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        for service in ("portal-web", "system-agent"):
            base = (ROOT / service / "Dockerfile").read_text().splitlines()[0].split()[1]
            rendered = runner.render_runner(base)
            self.assertTrue(rendered.startswith("FROM " + base + "\n"))
            self.assertNotIn("@APP_BASE_IMAGE@", rendered)
            self.assertIn("USER 10001:10001", rendered)
            self.assertEqual(runner.approved_base("311" if service == "portal-web" else "312"), base)
        for base in ("python:latest", "python:3.11-alpine3.24", "untrusted@sha256:" + "a" * 64):
            with self.assertRaises(runner.FixtureError):
                runner.render_runner(base)
        with patch.object(Path, "read_text", return_value="FROM @APP_BASE_IMAGE@\nFROM untrusted\n"):
            with self.assertRaises(runner.FixtureError):
                runner.render_runner("python:3.11-alpine3.24@sha256:" + "a" * 64)
        for declarations in ("ARG BASE_IMAGE\nFROM ${BASE_IMAGE}", "FROM python:latest\nFROM python:latest"):
            with patch.object(Path, "read_text", return_value=declarations), self.assertRaises(runner.FixtureError):
                runner.approved_base("311")

    def test_proof_admission_diagnostic_keeps_only_static_contract_reason(self):
        before={'node':'A','primary':'A'};connection=MagicMock()
        for reason,expected in [('not_serving_owner','not_serving_owner'),
                                ('authority_expired_or_invalid','authority_expired_or_invalid'),
                                ('private arbitrary text','invalid_authority')]:
            with patch.object(proof,'observe',return_value=before),patch.object(proof,'_generation',return_value=()), \
                 patch.object(proof,'authority'),patch.object(proof.contract,'require_serving_owner',side_effect=proof.contract.AdmissionDenied(reason)):
                with self.assertRaisesRegex(config.DatabaseUnavailable,'^replication_admission_denied:'+expected+'$'):
                    proof.protect(connection,{'service':'portal-web','node':'A'},'fixture',None,before)
            connection.execute.assert_not_called()

    def test_commit_installs_and_joins_driver_cancel_deadline(self):
        connection = MagicMock()
        with patch.object(proof.threading, "Timer") as timer:
            proof.bounded_commit(connection)
            timer.assert_called_once()
            self.assertEqual(timer.call_args.args[0], 1.5)
            timer.call_args.args[1]()
            connection.cancel_safe.assert_called_once_with(timeout=1)
            timer.return_value.start.assert_called_once()
            timer.return_value.cancel.assert_called_once()
            timer.return_value.join.assert_called_once()
        connection.execute.assert_called_once_with("COMMIT")

    def test_empty_explicit_configuration_never_falls_back(self):
        with patch.dict(os.environ, {"HA_PG_CONFIG": ""}, clear=True):
            self.assertTrue(config.enabled())
            with self.assertRaisesRegex(config.DatabaseUnavailable, "production_ha_not_armed"):
                config.load("book-memo")

    def test_production_mode_cannot_arm_the_local_backend(self):
        with patch.dict(os.environ, {"HA_PG_CONFIG": "/tmp/not-read", "HA_LOCAL_RUNTIME": "1", "APP_ENV": "production"}, clear=True):
            with self.assertRaisesRegex(config.DatabaseUnavailable, "production_ha_not_armed"):
                config.load()

    def test_namespaces_reject_cross_service_and_identifier_injection(self):
        self.assertEqual(config.schema_name("portal-web", "homeops"), "s_portal_web_homeops")
        for service, component in (("unknown", "main"), ("portal-web", "../main"), ("portal-web", 'main;DROP')):
            with self.subTest(service=service, component=component), self.assertRaises(config.DatabaseUnavailable):
                config.schema_name(service, component)

    def test_private_configuration_refuses_symlink_world_readable_and_hardlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "config.json"
            path.write_text("{}")
            path.chmod(0o644)
            with self.assertRaises(config.DatabaseUnavailable):
                config._private(path)
            path.chmod(0o600)
            self.assertEqual(config._private(path), path)
            linked = root / "linked"
            linked.symlink_to(path)
            with self.assertRaises(config.DatabaseUnavailable):
                config._private(linked)
            os.link(path, root / "hardlink")
            with self.assertRaises(config.DatabaseUnavailable):
                config._private(path)

    def test_qmark_keeps_question_quotes_and_escapes_literal_percent(self):
        value = "SELECT '?' AS literal, title FROM videos WHERE id=? AND title NOT LIKE 'YouTube 영상 %'"
        compiled = db.qmark(value, (3,))
        self.assertIn("'?' AS literal", compiled)
        self.assertIn("id=%s", compiled)
        self.assertIn("영상 %%'", compiled)
        self.assertEqual(db.qmark("SELECT '100%'", ()), "SELECT '100%'")
        self.assertEqual(db.qmark("SELECT 'a''?' WHERE id=?", (2,)), "SELECT 'a''?' WHERE id=%s")

    def test_qmark_rejects_unclosed_literals_and_comments(self):
        for value in ("SELECT 'unterminated", "SELECT ? -- ?", "SELECT /* ? */ 1"):
            with self.subTest(value=value), self.assertRaises(config.DatabaseUnavailable):
                db.qmark(value, (2,))

    def test_sqlite_row_integer_key_mapping_and_iteration_contract(self):
        row = db.Row(("id", "title"), (4, "preserved"))
        self.assertEqual(row[0], 4)
        self.assertEqual(row["id"], 4)
        self.assertEqual(list(row), [4, "preserved"])
        self.assertEqual(dict(row), {"id": 4, "title": "preserved"})

    def test_offline_schema_conversion_preserves_literal_defaults(self):
        value = migrate._ddl("CREATE TABLE t(id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT DEFAULT 'INTEGER REAL BLOB DEFAULT CURRENT_TIMESTAMP', at TEXT DEFAULT CURRENT_TIMESTAMP)")
        self.assertIn("BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", value)
        self.assertIn("DEFAULT 'INTEGER REAL BLOB DEFAULT CURRENT_TIMESTAMP'", value)
        self.assertIn("to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC'", value)
        for source in ("CREATE VIRTUAL TABLE x", "CREATE TABLE t(id INTEGER); DROP TABLE other"):
            with self.assertRaises(config.DatabaseUnavailable):
                migrate._ddl(source)

    def test_worker_needs_actual_owner_and_single_executor_lock(self):
        cfg = {"node": "A", "service": "crawler-worker"}
        observation = {"recovery": False, "node": "A", "primary": "A", "valid": True,
                       "worker": "A", "serving": ["A"], "writes_open": True, "history_safe": True}
        raw = MagicMock()
        raw.execute.return_value.fetchone.return_value = (False,)
        with patch.object(db, "load", return_value=cfg), patch.object(proof, "raw_connection", return_value=raw), patch.object(proof, "observe", return_value=observation):
            with self.assertRaisesRegex(config.DatabaseUnavailable, "worker_already_running"):
                with db.worker_scope("crawler-worker"):
                    self.fail("second worker entered")
        raw.close.assert_called_once()

    def test_actual_timeline_and_authenticated_role_identity_are_checked(self):
        raw = MagicMock()
        cfg = {"cluster": "fixture", "system_id": "123", "service": "book-memo", "node": "A"}
        good = dict(cluster="fixture", system_id="123", identity_service="book-memo", identity_node="A", recovery=False, actual_timeline=2, timeline=2)
        raw.execute.return_value.fetchone.return_value = (good,)
        self.assertEqual(proof.observe(raw, cfg), good)
        for changes in ({"actual_timeline": 3}, {"identity_service": "car-care-worker"}, {"identity_node": "B"}, {"system_id": "different"}):
            with self.subTest(changes=changes), self.assertRaises(config.DatabaseUnavailable):
                raw.execute.return_value.fetchone.return_value = (dict(good, **changes),)
                proof.observe(raw, cfg)

    def test_observation_disconnect_is_static_unavailable(self):
        raw = MagicMock()
        raw.execute.side_effect = RuntimeError("private database details")
        with self.assertRaisesRegex(config.DatabaseUnavailable, "^database_observation_unavailable$"):
            proof.observe(raw, {})

    def test_worker_lock_failure_is_static_unavailable(self):
        raw = MagicMock()
        raw.execute.side_effect = RuntimeError("private connection details")
        with patch.object(db, "load", return_value={}), patch.object(proof, "raw_connection", return_value=raw):
            with self.assertRaisesRegex(config.DatabaseUnavailable, "^database_operation_unavailable$"):
                with db.worker_scope("crawler-worker"):
                    self.fail("lock acquisition failed")
            with self.assertRaisesRegex(config.DatabaseUnavailable, "^database_operation_unavailable$"):
                db.worker_owned("crawler-worker")

    def test_nested_worker_cannot_reuse_lost_lock_connection(self):
        raw = MagicMock()
        cfg = {"service": "crawler-worker", "node": "A"}
        db._LOCAL.workers = {"crawler-worker": raw}
        try:
            with patch.object(db, "load", return_value=cfg), patch.object(proof, "observe", side_effect=config.DatabaseUnavailable("database_observation_unavailable")):
                with self.assertRaises(config.DatabaseUnavailable):
                    with db.worker_scope("crawler-worker"):
                        self.fail("lost permit must not admit effects")
                with self.assertRaises(config.DatabaseUnavailable):
                    db.connect("crawler-worker", "state")
        finally:
            db._LOCAL.workers = {}


class SharedStateBoundaryTests(unittest.TestCase):
    def fake_connection(self, payload):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = (payload,)
        return connection

    def json_module(self):
        return {"psycopg.types.json": SimpleNamespace(Jsonb=lambda value: value)}

    def test_unchanged_completed_result_still_requires_fresh_protection(self):
        connection = self.fake_connection({"status": "completed", "result": {"ok": True}})
        with patch.object(db, "connect", return_value=connection), patch.dict(sys.modules, self.json_module()):
            with db.state_transaction("homeops-executor", "claim", {}) as state:
                self.assertTrue(state["result"]["ok"])
        connection.protect_replay.assert_called_once()
        connection.commit.assert_called_once()
        connection.close.assert_called_once()

    def test_nested_keys_share_one_atomic_connection_not_a_second_session_lock(self):
        connection = self.fake_connection({})
        with patch.object(db, "connect", return_value=connection) as connect, patch.dict(sys.modules, self.json_module()):
            with db.state_transaction("portal-web", "files", {}) as files:
                files["version"] = 1
                with db.state_transaction("portal-web", "audit", {}) as audit:
                    audit["event"] = "saved"
                with db.state_transaction("portal-web", "files", {}) as nested:
                    self.assertIs(files, nested)
                connection.commit.assert_not_called()
        connect.assert_called_once()
        connection.commit.assert_called_once()
        self.assertEqual(sum(call.args[0].startswith("INSERT") for call in connection.execute.call_args_list), 2)

    def test_caught_nested_failure_poison_rolls_back_the_whole_outer_transaction(self):
        connection = self.fake_connection({})
        with patch.object(db, "connect", return_value=connection), patch.dict(sys.modules, self.json_module()):
            with self.assertRaisesRegex(config.DatabaseUnavailable, "nested_state_transaction_failed"):
                with db.state_transaction("portal-web", "files", {}) as state:
                    state["new"] = True
                    try:
                        with db.state_transaction("portal-web", "audit", {}):
                            raise RuntimeError("injected failure")
                    except RuntimeError:
                        pass
        connection.commit.assert_not_called()
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()

    def test_commit_proof_failure_is_not_reported_as_success(self):
        connection = self.fake_connection({"state": "completed"})
        connection.commit.side_effect = config.DatabaseUnavailable("replica_unavailable")
        with patch.object(db, "connect", return_value=connection), patch.dict(sys.modules, self.json_module()):
            with self.assertRaises(config.DatabaseUnavailable):
                with db.state_transaction("homeops-executor", "claim", {}) as value:
                    self.assertEqual(value["state"], "completed")
        connection.protect_replay.assert_called_once()
        connection.rollback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
