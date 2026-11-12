"""Native-adapter unit evidence only; no Windows/N100 actions are executed."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/k8s"))
from ha_control.controller import TransitionBlocked
from ha_control.pg import PgBackend, ROLE_LOCK
from ha_control import cli, scm_pg_auth_probe


class Fence:
    def __init__(self, plan):
        self.plan_data, self.enters, self.exits, self.stopped = plan, 0, 0, True

    @contextmanager
    def guard(self):
        self.enters += 1
        try:
            yield
        finally:
            self.exits += 1

    def fenced(self, state): return self.stopped
    def fence(self, state): self.stopped = True


class PGControlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        for name in ("ca", "pass"):
            (self.root / name).write_text("synthetic fixture")
            (self.root / name).chmod(0o600)
        endpoint = dict(port=5432, dbname="ha", user="fixture_admin", sslmode="verify-full",
                        sslrootcert=str(self.root / "ca"), passfile=str(self.root / "pass"))
        self.plan = dict(cluster="synthetic", node="A", generation=7, timeline=1, system_id="123", _approval_action="ha-transition",
                         pg_nodes={"A": dict(endpoint, host="ha-a"), "B": dict(endpoint, host="ha-b")})
        self.fence = Fence(self.plan)
        self.backend = PgBackend(self.root / "plan.json", self.root / "approval.json", fence=self.fence)
        self.state = dict(cluster="synthetic", old="A", target="B", generation=7, timeline=1,
                          phase="validating", attempts=1)
        self.guard = self.backend.guard()
        self.guard.__enter__()
        self.addCleanup(lambda: self.guard.__exit__(None, None, None))

    def observation(self, node="B", *, recovery=False, opened=False):
        return dict(cluster="synthetic", epoch=7, revision=9, primary="B", partner="A", timeline=2,
                    serving=["B"], worker="B", mode="AS", writes=opened, history=opened, remaining=4,
                    system_id="123", node="ha_" + node, recovery=recovery, actual_timeline=2,
                    flush="0/20", replay="0/20", sync_names="FIRST1(ha_a)", fsync=True,
                    full_page_writes=True, sync_peer="ha_a")

    def candidate(self):
        return dict(self.observation("B", recovery=True), epoch=6, primary="A", partner="B", timeline=1, actual_timeline=1)

    @contextmanager
    def fake_connection(self, node):
        yield self.primary if node == "B" else self.peer

    def prepare_fresh(self, *, lease=False, peer_lsn="0/20", bad_nonce=False, opened=False, update_rows=1):
        self.primary, self.peer = MagicMock(), MagicMock()
        def execute(query, params=None):
            result = MagicMock()
            result.rowcount = update_rows
            if "SELECT pg_current_wal_insert_lsn" in query:
                result.fetchone.return_value = ("0/10",)
            elif "SELECT generation FROM" in query:
                result.fetchone.return_value = None if bad_nonce else (7,)
            else:
                result.fetchone.return_value = None
            return result
        self.primary.execute.side_effect = execute
        self.peer.execute.side_effect = execute
        before = [self.observation(opened=opened), self.observation("A", recovery=True, opened=opened)]
        sequence = before * 3 + [self.observation(opened=lease), dict(self.observation("A", recovery=True, opened=lease), replay=peer_lsn)]
        return patch.object(self.backend, "_connection", side_effect=self.fake_connection), patch.object(self.backend, "_observe", side_effect=deepcopy(sequence))

    def test_guard_reentrancy_reuses_exact_native_fence_process(self):
        with self.backend.guard():
            with self.backend.guard():
                self.assertEqual(self.fence.enters, 1)
                self.assertEqual(self.backend.depth, 3)
        self.assertEqual(self.backend.depth, 1)
        self.assertEqual(self.fence.exits, 0)

    def test_unbound_generation_and_inline_password_cannot_reach_database(self):
        with self.assertRaisesRegex(TransitionBlocked, "controller_plan_state_mismatch"):
            self.backend.bind(dict(self.state, generation=6))
        self.plan["pg_nodes"]["A"]["password"] = "synthetic"
        with self.assertRaisesRegex(TransitionBlocked, "authenticated_pg_endpoints_required"):
            self.backend._plan()

    def test_dead_or_lock_busy_old_primary_does_not_prevent_positive_fencing(self):
        with patch.object(self.backend, "_read", return_value=self.candidate()), patch.object(self.backend, "_local_metadata", side_effect=TransitionBlocked("authenticated_pg_unavailable")):
            self.backend.close(self.state)
            self.assertTrue(self.backend.fenced(self.state))
        with patch.object(self.backend, "_read", return_value=dict(self.candidate(), system_id="wrong")):
            # Identity validation itself belongs to _observe, tested below.
            raw = MagicMock()
            raw.execute.return_value.fetchone.return_value = ({"cluster": "foreign"},)
            with self.assertRaisesRegex(TransitionBlocked, "pg_identity_mismatch"):
                self.backend._observe(raw, "B", self.state)

    def test_reachable_foreign_old_primary_is_not_misclassified_as_network_loss(self):
        with patch.object(self.backend, "_read", return_value=self.candidate()), patch.object(self.backend, "_local_metadata", side_effect=TransitionBlocked("pg_identity_mismatch")):
            with self.assertRaisesRegex(TransitionBlocked, "pg_identity_mismatch"):
                self.backend.close(self.state)

    def test_resumed_promoted_candidate_is_closed_before_other_actions(self):
        with patch.object(self.backend, "_read", return_value=self.observation()), patch.object(self.backend, "_local_metadata") as close:
            self.backend.close(self.state)
        self.assertEqual(close.call_count, 2)
        self.assertEqual(close.call_args_list[0].args, ("B", self.state))
        self.assertEqual(close.call_args_list[0].kwargs, {})
        self.assertEqual(close.call_args_list[1].kwargs, {"promoted": True})

    def test_initial_already_primary_target_is_closed_and_never_adopted(self):
        initial = dict(self.state, phase="fencing")
        with patch.object(self.backend, "_read", return_value=self.observation()), patch.object(self.backend, "_local_metadata") as close:
            with self.assertRaisesRegex(TransitionBlocked, "unexpected_primary_before_promotion_intent"):
                self.backend.close(initial)
        # Close the unexpected writer, but do not publish a new primary/epoch.
        close.assert_called_once_with("B", initial)
        for method in (self.backend.promote, self.backend.promoted):
            with patch.object(self.backend, "_connection") as connect:
                with self.assertRaisesRegex(TransitionBlocked, "unexpected_primary_before_promotion_intent"):
                    method(initial)
                connect.assert_not_called()

    def test_fence_only_approval_cannot_arm_pg_adapter_or_first_database_call(self):
        plan = deepcopy(self.plan)
        plan["_approval_action"] = "fence"
        bridge = Fence(plan)
        backend = PgBackend(self.root / "plan.json", self.root / "approval.json", fence=bridge)
        with patch.object(backend, "_connection") as connect:
            with self.assertRaisesRegex(TransitionBlocked, "ha_transition_approval_required"):
                with backend.guard():
                    backend.close(self.state)
            connect.assert_not_called()
        self.assertEqual(backend.depth, 0)
        self.assertEqual(bridge.enters, 1)
        self.assertEqual(bridge.exits, 1)

    def test_initial_recovery_target_requires_exact_source_timeline_and_old_epoch(self):
        for changes in ({"epoch": 7}, {"timeline": 2}, {"actual_timeline": 2}, {"primary": "B"}):
            with patch.object(self.backend, "_read", return_value=dict(self.candidate(), **changes)), patch.object(self.backend, "_local_metadata") as metadata:
                with self.assertRaisesRegex(TransitionBlocked, "promotion_candidate_mismatch"):
                    self.backend.close(dict(self.state, phase="fencing"))
                metadata.assert_not_called()

    def test_promotion_without_positive_host_fence_is_impossible(self):
        self.fence.stopped = False
        with patch.object(self.backend, "_connection") as connect:
            for operation in (self.backend.promote, self.backend.promoted):
                with self.assertRaisesRegex(TransitionBlocked, "positive_fence_required"):
                    operation(self.state)
            connect.assert_not_called()

    def test_lost_promotion_response_is_reconciled_without_second_promote(self):
        self.primary, self.peer = MagicMock(), MagicMock()
        def execute(query, *args):
            if "pg_promote" in query:
                raise OSError("private transport detail")
            return MagicMock()
        self.primary.execute.side_effect = execute
        with patch.object(self.backend, "_connection", side_effect=self.fake_connection), patch.object(self.backend, "_observe", return_value=self.candidate()), patch.object(self.backend, "_read", return_value=self.observation()), patch.object(self.backend, "_local_metadata") as metadata:
            self.backend.promote(self.state)
        self.assertEqual(sum("pg_promote" in call.args[0] for call in self.primary.execute.call_args_list), 1)
        metadata.assert_called_once_with("B", self.state, promoted=True)

    def test_rejoin_never_clones_or_restarts_fenced_resource(self):
        with patch.object(self.backend, "rejoined", return_value=False):
            with self.assertRaisesRegex(TransitionBlocked, "new_standby_required"):
                self.backend.rejoin(self.state)

    def test_pair_rejects_async_wrong_timeline_role_revision_and_wrong_node(self):
        good, peer = self.observation(), self.observation("A", recovery=True)
        self.backend._pair(good, peer, self.state)
        for change in ({"sync_names": "ANY1(ha_a)"}, {"fsync": False}, {"actual_timeline": 1}, {"serving": ["A"]}):
            with self.subTest(change=change), self.assertRaises(TransitionBlocked):
                self.backend._pair(dict(good, **change), peer, self.state)
        with self.assertRaises(TransitionBlocked):
            self.backend._pair(good, dict(peer, revision=10), self.state)
        raw = MagicMock()
        raw.execute.return_value.fetchone.return_value = (dict(good, node="ha_A"),)
        with self.assertRaisesRegex(TransitionBlocked, "pg_identity_mismatch"):
            self.backend._observe(raw, "B", self.state)

    def test_fresh_proof_uses_postcommit_watermark_and_second_sync_commit(self):
        connections, observations = self.prepare_fresh()
        with connections, observations, patch.object(self.backend, "_commit", side_effect=lambda c: c.execute("COMMIT")):
            self.backend.validate(self.state)
        queries = [call.args[0] for call in self.primary.execute.call_args_list]
        first_commit, watermark = queries.index("COMMIT"), next(i for i, q in enumerate(queries) if "pg_current_wal_insert_lsn" in q)
        self.assertLess(first_commit, watermark)
        self.assertGreater(max(i for i, q in enumerate(queries) if q == "COMMIT"), watermark)
        self.assertEqual(queries.count("SET LOCAL synchronous_commit=on"), 2)
        locks = [call.args[1] for call in self.primary.execute.call_args_list if "pg_advisory_xact_lock" in call.args[0]]
        self.assertEqual(locks, [(ROLE_LOCK,), (ROLE_LOCK,)])
        self.assertIsNotNone(self.backend.validated)

    def test_repeated_renewals_bound_only_ephemeral_nonces_under_role_lock(self):
        retained = {"earlier-attempt", "earlier-attempt:flush"}
        seen_attempts = []
        for _ in range(3):
            connections, observations = self.prepare_fresh(lease=True, opened=True)
            original_execute = self.primary.execute.side_effect
            def execute(query, params=None):
                if query.startswith("DELETE FROM ha.controller_probes"):
                    self.assertEqual(len(params), 2)
                    self.assertEqual(params[1], params[0] + ":flush")
                    retained.intersection_update(params)
                    seen_attempts.append(params)
                elif query.startswith("INSERT INTO ha.controller_probes"):
                    retained.add(params[0])
                return original_execute(query, params)
            self.primary.execute.side_effect = execute
            with connections, observations, patch.object(self.backend, "_commit", side_effect=lambda c: c.execute("COMMIT")):
                self.backend.renew(self.state)
            self.assertEqual(retained, set(seen_attempts[-1]))
            self.assertEqual(len(retained), 2)
            calls = self.primary.execute.call_args_list
            queries = [call.args[0] for call in calls]
            cleanup = next(i for i, query in enumerate(queries) if query.startswith("DELETE FROM"))
            first_lock = next(i for i, query in enumerate(queries) if "pg_advisory_xact_lock" in query)
            first_insert = next(i for i, query in enumerate(queries) if query.startswith("INSERT INTO"))
            first_commit = queries.index("COMMIT")
            self.assertLess(first_lock, cleanup)
            self.assertLess(cleanup, first_insert)
            self.assertLess(first_insert, first_commit)
            self.assertEqual(sum(query.startswith("DELETE FROM") for query in queries), 1)
            self.assertEqual(sum(query.startswith("INSERT INTO ha.controller_probes") for query in queries), 2)
            self.assertEqual(queries.count("SELECT pg_advisory_xact_lock_shared(%s)"), 2)
            # Both observations still verify the exact current :flush nonce.
            for connection in (self.primary, self.peer):
                reads = [call for call in connection.execute.call_args_list if "SELECT generation FROM ha.controller_probes" in call.args[0]]
                self.assertEqual(reads[-1].args[1], (seen_attempts[-1][1],))
        self.assertEqual(len({attempt[0] for attempt in seen_attempts}), 3)

    def test_stale_replay_missing_nonce_or_deadline_never_arms_authority(self):
        for options in ({"peer_lsn": "0/1"}, {"bad_nonce": True}, {}):
            connections, observations = self.prepare_fresh(**options)
            clock = patch("ha_control.pg.time.monotonic", side_effect=[0, 3]) if not options else patch("ha_control.pg.time.monotonic", return_value=0)
            with connections, observations, clock, patch.object(self.backend, "_commit"):
                with self.assertRaisesRegex(TransitionBlocked, "fresh_replication_proof_required"):
                    self.backend.validate(self.state)
            self.assertIsNone(self.backend.validated)
            self.assertFalse(any("UPDATE ha.control" in c.args[0] for c in self.primary.execute.call_args_list))

    def test_two_host_observations_and_database_share_one_total_deadline(self):
        # Synthetic latency on the actual PG proof path, not native performance
        # evidence. Each component fits two seconds even when their sum does not.
        for host_seconds, accepted in ((0.7, True), (0.9, False)):
            with self.subTest(host_seconds=host_seconds):
                self.backend.validated = None
                connections, observations = self.prepare_fresh()
                clock = [100.0]
                phases = []
                def fenced(state):
                    self.assertEqual(state, self.state)
                    phases.append("host")
                    clock[0] += host_seconds
                    return True
                def commit(connection):
                    self.assertIs(connection, self.primary)
                    phases.append("sync_commit")
                    clock[0] += 0.2
                with connections, observations, patch.object(self.backend, "fenced", side_effect=fenced), patch.object(
                        self.backend, "_commit", side_effect=commit), patch(
                        "ha_control.pg.time.monotonic", side_effect=lambda: clock[0]):
                    if accepted:
                        self.backend._fresh_observation(self.state)
                        self.assertIsNotNone(self.backend.validated)
                    else:
                        with self.assertRaisesRegex(TransitionBlocked, "fresh_replication_proof_required"):
                            self.backend._fresh_observation(self.state)
                        self.assertIsNone(self.backend.validated)
                self.assertEqual(phases, ["host", "sync_commit", "sync_commit", "host"])
                self.assertFalse(any("UPDATE ha.control" in call.args[0]
                                     for call in self.primary.execute.call_args_list))

    def test_open_requires_fresh_validation_and_rechecks_every_replication_fact(self):
        with self.assertRaisesRegex(TransitionBlocked, "fresh_replication_proof_required"):
            self.backend.open(self.state)
        self.backend.validated = ("synthetic", 7, "B", 2, 100)
        connections, observations = self.prepare_fresh(lease=True)
        with connections, observations, patch("ha_control.pg.time.monotonic", return_value=100), patch.object(self.backend, "_commit"):
            self.backend.open(self.state)
        updates = [c for c in self.primary.execute.call_args_list if "UPDATE ha.control" in c.args[0]]
        self.assertEqual(len(updates), 1)
        self.assertIn("interval '5 seconds'", updates[0].args[0])
        self.assertFalse(any("pg_advisory_xact_lock_shared" in call.args[0] for call in self.primary.execute.call_args_list))

    def test_long_worker_shared_permit_coexists_with_expiry_only_renewal(self):
        connections, observations = self.prepare_fresh(lease=True, opened=True)
        original_execute = self.primary.execute.side_effect
        worker_shared_permit_held = True
        def execute(query, params=None):
            if worker_shared_permit_held and query == "SELECT pg_advisory_xact_lock(%s)":
                raise TransitionBlocked("exclusive_role_change_blocked_by_worker")
            return original_execute(query, params)
        self.primary.execute.side_effect = execute
        with connections, observations, patch.object(self.backend, "_commit", side_effect=lambda c: c.execute("COMMIT")):
            self.backend.renew(self.state)
        calls = self.primary.execute.call_args_list
        queries = [call.args[0] for call in calls]
        self.assertEqual(queries.count("SELECT pg_advisory_xact_lock_shared(%s)"), 2)
        self.assertNotIn("SELECT pg_advisory_xact_lock(%s)", queries)
        cleanup = next(i for i, query in enumerate(queries) if query.startswith("DELETE FROM ha.controller_probes"))
        self.assertLess(queries.index("SELECT pg_advisory_xact_lock_shared(%s)"), cleanup)
        update = next(call for call in calls if call.args[0].startswith("UPDATE ha.control"))
        assignment, predicate = update.args[0].split(" WHERE ")
        self.assertEqual(assignment, "UPDATE ha.control SET expires_at=clock_timestamp()+interval '5 seconds'")
        for column in ("cluster", "epoch", "revision", "primary_node", "partner", "timeline", "serving", "worker", "mode"):
            self.assertIn(column + "=%s", predicate)
        self.assertIn("writes_open=true AND history_safe=true AND expires_at>clock_timestamp()", predicate)
        self.assertEqual(update.args[1], ("synthetic", 7, 9, "B", "A", 2, ["B"], "B", "AS"))
        self.assertIsNotNone(self.backend.validated)

    def test_shared_renewal_never_reopens_closed_or_expired_authority(self):
        for opened, expired in ((False, False), (True, True)):
            connections, observations = self.prepare_fresh(lease=True, opened=opened)
            if expired:
                values = [dict(self.observation(opened=True), remaining=0), dict(self.observation("A", recovery=True, opened=True), remaining=0)]
                observations = patch.object(self.backend, "_observe", side_effect=values)
            with connections, observations, patch.object(self.backend, "_commit") as commit:
                with self.assertRaisesRegex(TransitionBlocked, "closed_authority_requires_explicit_open"):
                    self.backend.renew(self.state)
                commit.assert_not_called()
            writes = [call.args[0] for call in self.primary.execute.call_args_list if call.args[0].startswith(("UPDATE", "INSERT", "DELETE"))]
            self.assertTrue(all(query.startswith("UPDATE ha.control SET writes_open=false,history_safe=false") for query in writes))
            self.assertIsNone(self.backend.validated)

    def test_shared_renewal_requires_exact_current_authority_and_one_updated_row(self):
        connections, observations = self.prepare_fresh(lease=True, opened=True, update_rows=0)
        with connections, observations, patch.object(self.backend, "_commit") as commit:
            with self.assertRaisesRegex(TransitionBlocked, "authority_changed_during_renewal"):
                self.backend.renew(self.state)
            self.assertEqual(commit.call_count, 1)
        self.assertIsNone(self.backend.validated)
        connections, _ = self.prepare_fresh(lease=True, opened=True)
        values = [self.observation(opened=True), self.observation("A", recovery=True, opened=True),
                  dict(self.observation(opened=True), revision=10), dict(self.observation("A", recovery=True, opened=True), revision=10)]
        with connections, patch.object(self.backend, "_observe", side_effect=values), patch.object(self.backend, "_commit") as commit:
            with self.assertRaisesRegex(TransitionBlocked, "authority_changed_during_renewal"):
                self.backend.renew(self.state)
            commit.assert_not_called()

    def test_shared_role_lock_cannot_be_requested_for_initial_validation(self):
        with patch.object(self.backend, "_connection") as connection:
            with self.assertRaisesRegex(TransitionBlocked, "shared_lock_only_for_lease_renewal"):
                self.backend._fresh(self.state, shared=True)
            connection.assert_not_called()
        with patch.object(self.backend, "renew") as renew, patch.object(self.backend, "validate") as validate, patch.object(self.backend, "open") as reopen:
            self.backend.assert_ready(self.state)
            renew.assert_called_once_with(self.state)
            validate.assert_not_called()
            reopen.assert_not_called()

    def test_cli_refuses_any_journal_outside_the_verified_plan_directory(self):
        args = SimpleNamespace(plan=str(self.root / "plan.json"), approval=str(self.root / "approval.json"),
                               journal=str(self.root / "elsewhere.json"), command="advance")
        with patch("ha_control.cli.PgBackend") as backend:
            with self.assertRaisesRegex(TransitionBlocked, "plan_bound_journal_required"):
                cli.run(args)
            backend.assert_not_called()

    def test_authenticate_checks_both_isolated_endpoints_without_a_journal(self):
        backend = self.backend
        backend.plan = dict(self.plan, profile="enforced-vm-v1", windows_enforced={
            "config_profile": {"acceptance_scope": "isolated-predeployment", "operational_cutover_accepted": False},
        }, pg_nodes={node: {"dbname": "ha", "user": "postgres"} for node in ("A", "B")})
        connections = {node: MagicMock() for node in ("A", "B")}
        for node, connection in connections.items():
            connection.execute.return_value.fetchone.return_value = (
                "ha", "postgres", "ha_" + node, str(backend.plan["system_id"]))

        @contextmanager
        def connect(node):
            yield connections[node]

        with patch.object(backend, "_connection", side_effect=connect) as connect_mock:
            backend.authenticate_isolated_nodes()
            self.assertEqual([call.args for call in connect_mock.call_args_list], [("A",), ("B",)])
            for connection in connections.values():
                connection.transaction.assert_called_once()
                self.assertEqual(connection.execute.call_args_list[0].args, ("SET TRANSACTION READ ONLY",))
                self.assertEqual(connection.execute.call_count, 2)
            connections["B"].execute.return_value.fetchone.return_value = (
                "ha", "postgres", "ha_A", str(backend.plan["system_id"]))
            with self.assertRaisesRegex(TransitionBlocked, "pg_identity_mismatch"):
                backend.authenticate_isolated_nodes()
            backend.plan["windows_enforced"]["config_profile"]["operational_cutover_accepted"] = True
            with self.assertRaisesRegex(TransitionBlocked, "isolated_pg_authentication_required"):
                backend.authenticate_isolated_nodes()
            self.assertEqual(connect_mock.call_count, 4)

        (self.root / "journal").mkdir()
        args = SimpleNamespace(plan=str(self.root / "plan.json"), approval=str(self.root / "approval.json"),
                               journal=None, command="authenticate")
        service_backend = MagicMock()
        service_backend.plan = backend.plan
        with patch.object(cli, "PgBackend", return_value=service_backend), patch.object(cli, "validate_bundle"), \
                patch.object(cli, "Controller") as controller, patch.object(cli, "Journal") as journal:
            self.assertEqual(cli.run(args), {"status": "PASS", "authenticated_nodes": ["A", "B"], "authority_opened": False})
            service_backend.authenticate_isolated_nodes.assert_called_once()
            controller.assert_not_called()
            journal.assert_not_called()
        self.assertEqual(list((self.root / "journal").iterdir()), [])

    def test_enforced_cli_persists_only_in_precreated_mutable_journal_root(self):
        backend = MagicMock()
        backend.plan = dict(self.plan, profile="enforced-vm-v1")
        args = SimpleNamespace(plan=str(self.root / "plan.json"), approval=str(self.root / "approval.json"),
                               journal=None, command="initialize")
        with patch("ha_control.cli.PgBackend", return_value=backend), patch("ha_control.cli.validate_bundle"):
            with self.assertRaisesRegex(TransitionBlocked, "plan_bound_journal_required"):
                cli.run(args)
            self.assertFalse((self.root / "journal").exists())
            (self.root / "journal").mkdir(mode=0o700)
            result = cli.run(args)
            self.assertEqual(result["phase"], "fencing")
            self.assertTrue((self.root / "journal/controller-state.json").is_file())
            self.assertFalse((self.root / "controller-state.json").exists())
            args.journal = str(self.root / "controller-state.json")
            with self.assertRaisesRegex(TransitionBlocked, "plan_bound_journal_required"):
                cli.run(args)
            args.journal = None
            (self.root / "journal/controller-state.json").unlink()
            (self.root / "journal").rename(self.root / "other-journal")
            (self.root / "journal").symlink_to(self.root / "other-journal", target_is_directory=True)
            with self.assertRaisesRegex(TransitionBlocked, "plan_bound_journal_required"):
                cli.run(args)

    def test_cli_validates_bundle_after_private_guard_before_controller_or_db_call(self):
        args = SimpleNamespace(plan=str(self.root / "plan.json"), approval=str(self.root / "approval.json"),
                               journal=None, command="initialize")
        backend = MagicMock()
        backend.plan = self.plan
        with patch("ha_control.cli.PgBackend", return_value=backend), patch("ha_control.cli.Controller") as control:
            def reject(plan):
                backend.guard.return_value.__enter__.assert_called_once()
                control.assert_not_called()
                backend.bind.assert_not_called()
                raise TransitionBlocked("approved_controller_bundle_required")
            with patch("ha_control.cli.validate_bundle", side_effect=reject), self.assertRaises(TransitionBlocked):
                cli.run(args)
            control.assert_not_called()
            backend.bind.assert_not_called()

    def test_failed_open_proof_immediately_attempts_closed_authority(self):
        self.backend.validated = ("synthetic", 7, "B", 2, 100)
        with patch("ha_control.pg.time.monotonic", return_value=100), patch.object(self.backend, "_fresh", side_effect=TransitionBlocked("fresh_replication_proof_required")), patch.object(self.backend, "_local_metadata") as close:
            with self.assertRaisesRegex(TransitionBlocked, "fresh_replication_proof_required"):
                self.backend.open(self.state)
        close.assert_called_once_with("B", self.state)

    def test_lost_guard_during_renewal_attempts_revocation_and_clears_cached_proof(self):
        connections, observations = self.prepare_fresh(lease=True, opened=True)
        self.backend.validated = ("synthetic", 7, "B", 2, 100)
        with connections, observations, patch.object(self.backend, "fenced", side_effect=[True, False]), patch.object(self.backend, "_local_metadata_write") as close:
            with self.assertRaisesRegex(TransitionBlocked, "fresh_replication_proof_required"):
                self.backend.renew(self.state)
        close.assert_called_once_with("B", self.state)
        self.assertIsNone(self.backend.validated)

    def test_guard_unavailable_before_renewal_does_not_prevent_safe_revocation(self):
        with patch.object(self.backend, "fenced", side_effect=TransitionBlocked("windows_guard_lost")), patch.object(self.backend, "_local_metadata_write") as close:
            with self.assertRaisesRegex(TransitionBlocked, "windows_guard_lost"):
                self.backend.renew(self.state)
            close.assert_called_once_with("B", self.state)
            close.reset_mock()
            self.backend._local_metadata("B", self.state)
            close.assert_called_once_with("B", self.state)

    def test_failed_revocation_preserves_original_failure_and_never_reports_success(self):
        with patch.object(self.backend, "fenced", side_effect=TransitionBlocked("windows_guard_lost")), patch.object(self.backend, "_local_metadata_write", side_effect=OSError("unavailable")):
            with self.assertRaisesRegex(TransitionBlocked, "windows_guard_lost"):
                self.backend.renew(self.state)
        self.assertIsNone(self.backend.validated)

    def test_promoted_history_publication_requires_post_guard_and_whole_deadline(self):
        for answers, clock in (([True, False], [10]), ([True, True], [10, 12.001])):
            with patch.object(self.backend, "fenced", side_effect=answers), patch.object(self.backend, "_local_metadata_write") as write, patch("ha_control.pg.time.monotonic", side_effect=clock):
                with self.assertRaisesRegex(TransitionBlocked, "fresh_legacy_guard_required"):
                    self.backend._local_metadata("B", self.state, promoted=True)
                self.assertEqual(write.call_count, 2)
                self.assertEqual(write.call_args_list[-1].kwargs, {})

    def test_only_durable_promoting_phase_can_publish_the_new_timeline(self):
        self.primary, self.peer = MagicMock(), MagicMock()
        newly_promoted = dict(self.candidate(), recovery=False, actual_timeline=2)
        with patch.object(self.backend, "_connection", side_effect=self.fake_connection), patch.object(self.backend, "_observe", return_value=newly_promoted):
            self.backend._local_metadata("B", dict(self.state, phase="promoting"), promoted=True)
        self.assertTrue(any("SET primary_node=" in call.args[0] for call in self.primary.execute.call_args_list))
        self.primary.reset_mock()
        with patch.object(self.backend, "_connection", side_effect=self.fake_connection), patch.object(self.backend, "_observe", return_value=newly_promoted):
            with self.assertRaisesRegex(TransitionBlocked, "recorded_primary_history_required"):
                self.backend._local_metadata("B", dict(self.state, phase="ready"), promoted=True)
        updates = [call.args[0] for call in self.primary.execute.call_args_list if call.args[0].startswith("UPDATE")]
        self.assertEqual(len(updates), 1)
        self.assertIn("SET writes_open=false,history_safe=false", updates[0])
        self.assertNotIn("primary_node=", updates[0])
        self.primary.reset_mock()
        with patch.object(self.backend, "_connection", side_effect=self.fake_connection), patch.object(self.backend, "_observe", return_value=self.observation()):
            self.backend._local_metadata("B", dict(self.state, phase="ready"), promoted=True)
        updates = [call.args[0] for call in self.primary.execute.call_args_list if call.args[0].startswith("UPDATE")]
        self.assertEqual(len(updates), 1)
        self.assertIn("SET writes_open=false,history_safe=false", updates[0])
        self.assertNotIn("primary_node=", updates[0])
        self.assertNotIn("timeline=", updates[0])
        self.assertNotIn("epoch=", updates[0])

    def test_recorded_later_phase_closes_unexpected_fork_without_adopting_it(self):
        for phase in ("ready", "rejoining", "validating"):
            for changes in ({"actual_timeline": 3}, {"epoch": 6}, {"primary": "A"}):
                with self.subTest(phase=phase, changed=list(changes)), patch.object(self.backend, "_read", return_value=dict(self.observation(), **changes)), patch.object(self.backend, "_local_metadata") as metadata:
                    with self.assertRaisesRegex(TransitionBlocked, "recorded_primary_history_required"):
                        self.backend.close(dict(self.state, phase=phase))
                    metadata.assert_called_once_with("B", dict(self.state, phase=phase))
        with patch.object(self.backend, "_read", return_value=dict(self.observation(), actual_timeline=3)), patch.object(self.backend, "_local_metadata") as metadata, patch.object(self.backend, "validate") as validate, patch.object(self.backend, "open") as reopen:
            with self.assertRaisesRegex(TransitionBlocked, "recorded_primary_history_required"):
                self.backend.arm(dict(self.state, phase="ready"))
            metadata.assert_called_once_with("B", dict(self.state, phase="ready"))
            validate.assert_not_called()
            reopen.assert_not_called()

    def test_explicit_arm_renews_an_already_open_recorded_generation(self):
        with patch.object(self.backend, "_read", return_value=self.observation(opened=True)), patch.object(self.backend, "renew") as renew, patch.object(self.backend, "close") as close, patch.object(self.backend, "validate") as validate, patch.object(self.backend, "open") as reopen:
            self.backend.arm(dict(self.state, phase="ready"))
            renew.assert_called_once_with(dict(self.state, phase="ready"))
            close.assert_not_called()
            validate.assert_not_called()
            reopen.assert_not_called()

    def test_explicit_arm_reopens_expired_generation_with_exclusive_fresh_proofs(self):
        connections, _ = self.prepare_fresh(lease=True)
        closed = [self.observation(), self.observation("A", recovery=True)]
        opened = [self.observation(opened=True), self.observation("A", recovery=True, opened=True)]
        sequence = closed * 4 + closed * 3 + opened
        events = []
        original_validate, original_open = self.backend.validate, self.backend.open
        def validate(state):
            events.append("validate")
            original_validate(state)
        def reopen(state):
            events.append("open")
            original_open(state)
        with connections, patch.object(self.backend, "_observe", side_effect=deepcopy(sequence)), patch.object(self.backend, "_read", return_value=dict(self.observation(opened=True), remaining=-1)), patch.object(self.backend, "close", side_effect=lambda state: events.append("close")), patch.object(self.backend, "validate", side_effect=validate), patch.object(self.backend, "open", side_effect=reopen), patch.object(self.backend, "_commit"):
            self.backend.arm(dict(self.state, phase="ready"))
        self.assertEqual(events, ["close", "validate", "open"])
        queries = [call.args[0] for call in self.primary.execute.call_args_list]
        self.assertEqual(queries.count("SELECT pg_advisory_xact_lock(%s)"), 4)
        self.assertNotIn("SELECT pg_advisory_xact_lock_shared(%s)", queries)
        self.assertEqual(sum(query.startswith("UPDATE ha.control SET serving=") for query in queries), 1)

    def test_cli_arm_is_explicit_serve_only_and_keeps_same_guard_and_lock(self):
        from ha_control.controller import Journal, exclusive
        path = self.root / "controller-state.json"
        Journal(path).save(dict(self.state, phase="ready"))
        args = SimpleNamespace(plan=str(self.root / "plan.json"), approval=str(self.root / "approval.json"), journal=None, command="serve", arm=True)
        events = []
        def action(name):
            def call(state):
                events.append(name)
                self.assertGreater(self.backend.depth, 1)
                self.assertEqual(self.fence.enters, 1)
                if sys.platform != "win32":
                    with self.assertRaisesRegex(TransitionBlocked, "controller_lock_unavailable"):
                        with exclusive(path.with_suffix(".lock")):
                            pass
            return call
        with patch("ha_control.cli.validate_bundle"), patch("ha_control.cli.PgBackend", return_value=self.backend), patch.object(self.backend, "arm", side_effect=action("arm")) as arm, patch.object(self.backend, "renew", side_effect=action("renew")), patch.object(self.backend, "close", side_effect=action("close")), patch("ha_control.cli.time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                cli.run(args)
            arm.assert_called_once()
        self.assertEqual(events, ["arm", "renew", "close"])
        for command in ("initialize", "advance"):
            with patch("ha_control.cli.PgBackend") as backend:
                with self.assertRaisesRegex(TransitionBlocked, "arm_only_for_serve"):
                    cli.run(SimpleNamespace(**dict(vars(args), command=command)))
                backend.assert_not_called()

    def test_default_serve_does_not_rearm_an_expired_generation(self):
        from ha_control.controller import Journal
        path = self.root / "controller-state.json"
        Journal(path).save(dict(self.state, phase="ready"))
        args = SimpleNamespace(plan=str(self.root / "plan.json"), approval=str(self.root / "approval.json"), journal=None, command="serve")
        with patch("ha_control.cli.validate_bundle"), patch("ha_control.cli.PgBackend", return_value=self.backend), patch.object(self.backend, "renew", side_effect=TransitionBlocked("closed_authority_requires_explicit_open")), patch.object(self.backend, "arm") as arm, patch.object(self.backend, "close"):
            with self.assertRaisesRegex(TransitionBlocked, "closed_authority_requires_explicit_open"):
                cli.run(args)
            arm.assert_not_called()

    def test_serve_keeps_host_guard_and_controller_lock_until_lease_is_closed(self):
        from ha_control.controller import Journal, exclusive
        path = self.root / "controller-state.json"
        Journal(path).save(dict(self.state, phase="ready"))
        args = SimpleNamespace(plan=str(self.root / "plan.json"), approval=str(self.root / "approval.json"), journal=None, command="serve")
        def ready(state):
            self.assertGreater(self.backend.depth, 1)
            self.assertEqual(self.fence.enters, 1)
            if sys.platform != "win32":
                with self.assertRaisesRegex(TransitionBlocked, "controller_lock_unavailable"):
                    with exclusive(path.with_suffix(".lock")):
                        pass
        with patch("ha_control.cli.validate_bundle"), patch("ha_control.cli.PgBackend", return_value=self.backend), patch.object(self.backend, "renew", side_effect=ready), patch.object(self.backend, "close") as close, patch("ha_control.cli.time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                cli.run(args)
        close.assert_called_once()
        self.assertEqual(self.backend.depth, 1)

    def test_cli_does_not_print_unknown_sql_or_transport_exception(self):
        import io
        output = io.StringIO()
        with patch("ha_control.cli.run", side_effect=OSError("private endpoint credentials and SQL")), patch("sys.stdout", output):
            code = cli.main(["advance", "--plan", str(self.root / "plan.json"), "--approval", str(self.root / "approval.json")])
        self.assertEqual(code, 1)
        self.assertNotIn("private endpoint", output.getvalue())
        self.assertIn("controller_outcome_unknown", output.getvalue())

    def enforced_plan(self):
        import hashlib
        from ha_control.prepare import EFFECTS_SOURCES, TARGETS, effects_runtime_sha256
        from ha_control.pg import ENFORCED_SERVICES
        services = sorted(ENFORCED_SERVICES)
        sources = {path:hashlib.sha256((ROOT/'infra/k8s'/TARGETS[path]).read_bytes()).hexdigest()
                   for path in EFFECTS_SOURCES}
        self.plan.update(profile='enforced-vm-v1',schema_version=2,source_commit='c'*40,
                         pg_enforcement={'definition_sha256':hashlib.sha256((ROOT/'infra/k8s/ha_runtime/enforcement.sql').read_bytes()).hexdigest(),
                            'effects':{'code_sha256':effects_runtime_sha256(sources),'broker_role':'ha_broker'},
                            'role_sets':{node:{service:'app_'+str(index)+'_'+node.lower() for index,service in enumerate(services)} for node in ('A','B')},
                            'namespace_tables':{'s_'+service.replace('-','_')+'_main':{'service':service,'tables':['shared_state']} for service in services}})
        self.fence.plan_sha256='d'*64
        return self.plan

    def test_enforced_plan_requires_exact_effects_closure_broker_and_raw_plan_pin(self):
        plan=self.enforced_plan()
        self.backend._plan()
        for replacement in ({}, {'broker_role':'ha_broker'}, {'code_sha256':'0'*64,'broker_role':'ha_broker'},
                            dict(plan['pg_enforcement']['effects'],broker_role='bad;role')):
            original=plan['pg_enforcement']['effects'];plan['pg_enforcement']['effects']=replacement
            with self.assertRaises(TransitionBlocked):self.backend._plan()
            plan['pg_enforcement']['effects']=original
        del self.fence.plan_sha256
        with self.assertRaisesRegex(TransitionBlocked,'enforced_effects_plan_required'):self.backend._plan()

    def test_effects_observation_rejects_missing_storage_and_every_binding_mismatch(self):
        from ha_runtime import effects
        self.enforced_plan()
        expected=('synthetic','123','c'*40,'d'*64,'ha_broker')
        raw=MagicMock();raw.execute.return_value.fetchall.return_value=[expected]
        with patch.object(effects,'validate_catalog') as validate:
            self.backend._effects_observation(raw)
            self.assertEqual(validate.call_args.args[1].plan_sha256,'d'*64)
            for index in range(5):
                mismatch=list(expected);mismatch[index]='different'
                raw.execute.return_value.fetchall.return_value=[tuple(mismatch)]
                with self.assertRaisesRegex(TransitionBlocked,'external_effect_controller_binding_mismatch'):
                    self.backend._effects_observation(raw)
        with patch.object(effects,'validate_catalog',side_effect=OSError('missing ledger')):
            with self.assertRaisesRegex(TransitionBlocked,'external_effect_controller_catalog_unverified'):
                self.backend._effects_observation(raw)

    def test_real_observe_requires_effects_catalog_even_for_closed_authority(self):
        self.enforced_plan()
        raw=MagicMock()
        with patch.object(self.backend,'_enforced_observation'), patch.object(self.backend,'_effects_observation',side_effect=TransitionBlocked('missing_effects')) as observe:
            with self.assertRaisesRegex(TransitionBlocked,'missing_effects'):
                self.backend._observe(raw,'B',self.state)
            observe.assert_called_once_with(raw)
        raw.execute.assert_not_called()

    def effect_connections(self, *, unresolved=None, peer_unresolved=None, replay='0/20', **options):
        connections,observations=self.prepare_fresh(**options)
        for connection,value in ((self.primary,unresolved),(self.peer,peer_unresolved)):
            original=connection.execute.side_effect
            def execute(query,params=None,original=original,value=value):
                if 'SELECT pg_last_wal_replay_lsn()' in query:
                    return SimpleNamespace(fetchone=lambda:(replay,))
                if 'SELECT 1 FROM ha_effects.ledger' in query:
                    # Represent each actual persisted state named by the query.
                    found=value is not None and "'"+value+"'" in query
                    return SimpleNamespace(fetchone=lambda:(1,) if found else None)
                return original(query,params)
            connection.execute.side_effect=execute
        return connections,observations

    def test_initial_open_rejects_any_unresolved_state_on_either_node_before_update(self):
        self.enforced_plan()
        for state in ('pending','inflight','uncertain'):
            for where in ('unresolved','peer_unresolved'):
                connections,observations=self.effect_connections(lease=True,**{where:state})
                with connections,observations,patch.object(self.backend,'_commit'):
                    with self.assertRaisesRegex(TransitionBlocked,'external_effect_controller_unresolved'):
                        self.backend._fresh_observation(self.state,lease=True)
                queries=[call.args[0] for call in self.primary.execute.call_args_list]
                self.assertFalse(any('UPDATE ha.control SET serving' in query for query in queries))
                self.assertTrue(any('SELECT 1 FROM ha_effects.ledger' in call.args[0] for call in self.peer.execute.call_args_list))

    def test_completed_open_passes_only_after_exclusive_lock_and_peer_watermark(self):
        self.enforced_plan()
        connections,observations=self.effect_connections(lease=True,unresolved='completed',peer_unresolved='completed')
        with connections,observations,patch.object(self.backend,'_commit'):
            self.backend._fresh_observation(self.state,lease=True)
        queries=[call.args[0] for call in self.primary.execute.call_args_list]
        gate=next(index for index,query in enumerate(queries) if 'SELECT 1 FROM ha_effects.ledger' in query)
        locks=[index for index,query in enumerate(queries) if 'pg_advisory_xact_lock(%s)' in query]
        opening=next(index for index,query in enumerate(queries) if 'UPDATE ha.control SET serving' in query)
        self.assertLess(locks[-1],gate);self.assertLess(gate,opening)
        connections,observations=self.effect_connections(lease=True,replay='0/0')
        with connections,observations,patch.object(self.backend,'_commit'):
            with self.assertRaisesRegex(TransitionBlocked,'external_effect_controller_peer_unproven'):
                self.backend._fresh_observation(self.state,lease=True)
        self.assertFalse(any('UPDATE ha.control SET serving' in call.args[0] for call in self.primary.execute.call_args_list))

    def test_shared_renew_and_closed_validation_do_not_gate_normal_inflight(self):
        self.enforced_plan()
        for shared,lease in ((True,True),(False,False)):
            connections,observations=self.effect_connections(lease=lease,opened=shared,unresolved='inflight',peer_unresolved='pending')
            with connections,observations,patch.object(self.backend,'_commit'),patch.object(self.backend,'_effects_open_gate') as gate:
                self.backend._fresh_observation(self.state,lease=lease,shared=shared)
                gate.assert_not_called()
            self.assertFalse(any('SELECT 1 FROM ha_effects.ledger' in call.args[0] for call in self.primary.execute.call_args_list))




class ScmPgAuthProbeTests(unittest.TestCase):
    def test_exact_service_scope_and_two_verified_read_only_connections(self):
        probe=scm_pg_auth_probe
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            material=root/"db-auth-material"
            material.mkdir()
            ca=material/"ca.crt"
            ca.write_bytes(b"fixture CA")
            (material/"postgres.pgpass").write_text("fixture only")
            authority=root/"pg-auth-probe-authority"
            journal=authority/"journal"
            journal.mkdir(parents=True)
            plan_path=authority/"plan.json"
            approval_path=authority/"approval.json"
            report=journal/"controller-state.json"
            trace=journal/".ha-journal-authdiag"
            sid="S-1-5-80-1-2-3-4-5"
            plan={"scope":probe.SCOPE,"service_sid":sid,"hosts":{"A":"10.0.0.2","B":"10.0.0.3"},
                  "system_id":"1234","ca_sha256":hashlib.sha256(ca.read_bytes()).hexdigest()}
            raw=json.dumps(plan).encode()
            plan_path.write_bytes(raw)
            pin=hashlib.sha256(raw).hexdigest()
            approval={"scope":probe.SCOPE,"plan_sha256":pin,"expires_unix":int(time.time())+60}
            approval_path.write_text(json.dumps(approval))
            args=SimpleNamespace(plan=str(plan_path),approval=str(approval_path),journal=str(report))
            connection_a=MagicMock()
            connection_b=MagicMock()
            for node,connection in (("A",connection_a),("B",connection_b)):
                connection.execute.return_value.fetchone.side_effect=[
                    ("ha_"+node,node=="B","on","1234","postgres"),(True,),
                ]
            contexts=[]
            for connection in (connection_a,connection_b):
                context=MagicMock()
                context.__enter__.return_value=connection
                contexts.append(context)
            connect=MagicMock(side_effect=contexts)
            with patch.dict(os.environ,{},clear=True), patch.dict(sys.modules,{"psycopg":SimpleNamespace(connect=connect)}):
                with patch.object(probe,"service_identity",return_value="S-1-5-80-6-7-8-9-10"):
                    with self.assertRaises(ValueError):
                        probe.run(args)
                    connect.assert_not_called()
                    self.assertFalse(report.exists())
                    self.assertEqual(trace.read_text().splitlines(), ["service_identity"])
                    trace.unlink()
                with patch.object(probe,"service_identity",return_value=sid):
                    with patch.dict(os.environ,{"PGPASSWORD":"fixture"}):
                        with self.assertRaises(ValueError):
                            probe.run(args)
                        connect.assert_not_called()
                        self.assertEqual(trace.read_text().splitlines(), ["service_identity", "environment"])
                        trace.unlink()
                    probe.run(args)
                    with self.assertRaises(ValueError):
                        probe.run(args)
            for node,call,connection in zip(("A","B"),connect.call_args_list,(connection_a,connection_b)):
                self.assertEqual(call.kwargs["host"],plan["hosts"][node])
                self.assertEqual(call.kwargs["hostaddr"],plan["hosts"][node])
                self.assertEqual(call.kwargs["sslmode"],"verify-full")
                self.assertEqual(call.kwargs["gssencmode"],"disable")
                self.assertEqual(call.kwargs["require_auth"],"scram-sha-256")
                self.assertEqual(call.kwargs["passfile"],str(material/"postgres.pgpass"))
                connection.execute.assert_any_call("SET TRANSACTION READ ONLY")
                connection.rollback.assert_called_once()
            result=json.loads(report.read_text())
            self.assertEqual(trace.read_text().splitlines(), ["service_identity", "environment", "load_driver",
                "credential_files", "connect_A", "observe_A", "connect_B", "observe_B", "report", "complete"])
            self.assertEqual(result,{"scope":probe.SCOPE,"plan_sha256":pin,"nodes":["A","B"],
                                     "service_identity_verified":True,"tls_verified":True,"authority_opened":False})
            approval["expires_unix"]=0
            with self.assertRaises(ValueError):
                probe.verify_plan(plan,approval,raw,time.time())
            with self.assertRaises(ValueError):
                probe.strict_json('{"scope":0,"scope":1}')

if __name__ == "__main__":
    unittest.main()
