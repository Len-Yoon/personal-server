"""Native-adapter unit evidence only; no Windows/N100 actions are executed."""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/k8s"))
from ha_control.controller import TransitionBlocked
from ha_control.pg import PgBackend, ROLE_LOCK
from ha_control import cli


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

    def prepare_fresh(self, *, lease=False, peer_lsn="0/20", bad_nonce=False):
        self.primary, self.peer = MagicMock(), MagicMock()
        def execute(query, params=None):
            result = MagicMock()
            if "SELECT pg_current_wal_insert_lsn" in query:
                result.fetchone.return_value = ("0/10",)
            elif "SELECT generation FROM" in query:
                result.fetchone.return_value = None if bad_nonce else (7,)
            else:
                result.fetchone.return_value = None
            return result
        self.primary.execute.side_effect = execute
        self.peer.execute.side_effect = execute
        before = [self.observation(), self.observation("A", recovery=True)]
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
            connections, observations = self.prepare_fresh(lease=True)
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
                self.backend._fresh(self.state, lease=True)
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

    def test_cli_refuses_any_journal_outside_the_verified_plan_directory(self):
        args = SimpleNamespace(plan=str(self.root / "plan.json"), approval=str(self.root / "approval.json"),
                               journal=str(self.root / "elsewhere.json"), command="advance")
        with patch("ha_control.cli.PgBackend") as backend:
            with self.assertRaisesRegex(TransitionBlocked, "plan_bound_journal_required"):
                cli.run(args)
            backend.assert_not_called()

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
        close.assert_called_once_with("B", self.state, promoted=True)

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
        with patch("ha_control.cli.validate_bundle"), patch("ha_control.cli.PgBackend", return_value=self.backend), patch.object(self.backend, "assert_ready", side_effect=ready), patch.object(self.backend, "close") as close, patch("ha_control.cli.time.sleep", side_effect=KeyboardInterrupt):
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


if __name__ == "__main__":
    unittest.main()
