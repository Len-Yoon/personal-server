"""Transition ordering, durable retry bounds and legacy exclusion contracts."""
from contextlib import contextmanager
from dataclasses import replace
import json
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/k8s"))
from ha_control.controller import Controller, Journal, TransitionBlocked, exclusive
from ha_control.windows import WindowsFence
from ha_control import car_callback_ingress, car_callback_runtime, caddy_runtime, transition_execution, transition_ingress
from ha_control import wsl_writer_proof
from ha_control.native_guest_activation import ServingGeneration


class TransitionIngressTests(unittest.TestCase):
    def test_observer_and_apps_precede_external_route_switch(self):
        from ha_runtime.seven_capture import encoded

        generation = ServingGeneration("B", 2, 2, 2)
        candidate = encoded({"generation": 2, "node": "A", "source_lease_close_required": True})
        steps = []
        execution = MagicMock()
        execution.publish_observer_runtime.side_effect = lambda **_: steps.append("runtime") or {"runtime": True}
        execution.spec = {"plan_sha256": "a"*64, "attempt_id": "b"*64}
        controller = {"plan": candidate, "plan_sha256": caddy_runtime._sha(candidate)}

        @contextmanager
        def open_controller(plan, serving, *, attempt, **options):
            self.assertEqual(plan, controller)
            self.assertEqual(serving, generation)
            self.assertEqual(attempt, execution.spec["attempt_id"])
            steps.append("controller")
            self.assertEqual(options["publish_handoff"](serving, "5"*64, 50), "6"*64)
            steps.append("lease")
            yield MagicMock(), MagicMock()

        def guest(request, deadline, *, assert_windows_current):
            steps.append("guest")
            self.assertEqual(request.verb, transition_ingress.bridge.GUEST_ACTIVATE_GENERATION)
            self.assertEqual(request.sequence, 5)
            self.assertEqual(request.args, {"generation_handoff_sha256": "6"*64, "effects_plan_sha256": "3"*64})
            self.assertTrue(callable(assert_windows_current))
            raise RuntimeError("guest failed")

        args = dict(
            previous_runtime_sha256="c"*64, legacy_plan="d"*64, legacy_attempt="e"*64,
            expected_mtls_sha256="f"*64, previous_callback=b"callback", previous_callback_sha256="1"*64,
            replacement_vm={"installation_id": "a"*64}, observer_release={}, observer_setup={"plan_raw": candidate, "plan_sha256": caddy_runtime._sha(candidate), "deadline": 50,
                    "stage_sha256": "1"*64, "manifest_sha256": "2"*64},
            assert_legacy_closed=MagicMock(),
            controller_setup={"deadline": 50, "starter": MagicMock(),
                              "live_sha256": "3"*64, "root": "fixture", "old_vm_id": "fixture"},
            replacement_requests=lambda _: (replacement, observe),
            generation_handoff_pins=lambda _: {"pins": SimpleNamespace(plan_sha256="2"*64), "postgres": None,
                "activation": SimpleNamespace(primary_runtime_receipt_sha256="a"*64)},
            stage_service={"approval": lambda *_: encoded({"plan_sha256": controller["plan_sha256"],
                                                                        "action": "ha-transition",
                                                                        "authority_kind": "installed-runtime"}),
                           "close_previous_service": lambda *_: steps.append("close-prior") or "4"*64,
                           "preflight_path": "fixture", "scm_verifier_path": "fixture"},
            guests=SimpleNamespace(verify_prepared_transition_target=MagicMock(return_value=True),
                                   publish_generation_handoff=MagicMock(return_value="6"*64),
                                   activate_generation=guest, verify_live_generation=MagicMock(return_value="9"*64)),
            target_request=SimpleNamespace(binding=SimpleNamespace(**execution.spec)),
        )
        stage = "7"*64
        replacement = transition_ingress.bridge.Request(
            None, "A", transition_ingress.bridge.GUEST_START_REPLACEMENT, 2, "3"*64, {"rejoin_stage_sha256": stage},
        )
        observe = replace(replacement, sequence=3, nonce="4"*64)
        handoff = transition_ingress.bridge.Request(
            args["target_request"].binding, "B", transition_ingress.bridge.GUEST_PUBLISH_GENERATION, 4, "5"*64,
            {"generation_handoff_sha256": "6"*64, "effects_plan_sha256": "3"*64},
        )
        routes = (execution, None, None, SimpleNamespace(primary_ip="previous"),
                  SimpleNamespace(guest="previous", route_pin="9"*64), generation, "target", "8"*64)
        prepared = {"ApprovalSha256": "a"*64, "PreflightPath": "fixture", "ScmVerifierPath": "fixture"}
        def switch_routes(*_args, **kwargs):
            self.assertEqual(kwargs["verify_guest"](generation, "target"), "9"*64)
            steps.append("ingress")
            return None, None, {}

        with patch.object(transition_ingress, "validate_generation_handoff_identity"), \
                patch.object(transition_ingress, "generation_handoff_request", return_value=handoff), \
                patch.object(transition_ingress, "validate_transition_approval") as validate_approval, \
                patch.object(transition_ingress, "fixed_script"), \
                patch.object(transition_ingress, "stage_approved_transition_service",
                          side_effect=lambda *_a, **_kw: steps.append("stage") or prepared) as stage_service, \
                patch.object(transition_ingress, "prepare_staged_transition_service",
                             side_effect=lambda *_a, **_kw: steps.append("verify") or prepared), \
                patch.object(transition_ingress, "_bind_rejoin_controller"), \
                patch.object(transition_ingress, "_windows_rejoin", return_value=(stage, 8, MagicMock(), MagicMock())), \
                patch.object(transition_ingress, "_rejoin_replacement", side_effect=lambda *_: steps.append("rejoin")), \
                patch.object(transition_ingress, "open_transition_controller", side_effect=open_controller), \
                patch.object(transition_ingress, "observer_callback_config", return_value={}), \
                patch.object(transition_ingress, "prepare_transition_observer_plan",
                             side_effect=lambda **_: steps.append("observer") or controller), \
                patch.object(transition_ingress, "switch_ingress", side_effect=switch_routes) as switch:
            args["guests"].verify_prepared_transition_target.return_value = False
            with self.assertRaisesRegex(TransitionBlocked, "target_unconfirmed"):
                transition_ingress.switch_with_observer(*routes, **args)
            self.assertEqual(steps, [])
            args["guests"].verify_prepared_transition_target.return_value = True
            validate_approval.side_effect = TransitionBlocked("approval invalid")
            with self.assertRaisesRegex(TransitionBlocked, "approval invalid"):
                transition_ingress.switch_with_observer(*routes, **args)
            self.assertEqual(steps, ["runtime", "observer"])
            stage_service.assert_not_called()
            steps.clear()
            validate_approval.side_effect = None
            stage_service.side_effect = TransitionBlocked("publication unconfirmed")
            with self.assertRaisesRegex(TransitionBlocked, "publication unconfirmed"):
                transition_ingress.switch_with_observer(*routes, **args)
            self.assertEqual(steps, ["runtime", "observer", "close-prior"])
            switch.assert_not_called()
            steps.clear()
            stage_service.side_effect = lambda *_a, **_kw: steps.append("stage") or prepared
            with self.assertRaisesRegex(RuntimeError, "guest failed"):
                transition_ingress.switch_with_observer(*routes, **args)
            switch.assert_not_called()
            self.assertEqual(steps, ["runtime", "observer", "close-prior", "stage", "verify", "rejoin", "controller", "lease", "guest"])
            steps.clear()
            args["guests"].activate_generation = lambda *_a, **_kw: steps.append("guest") or transition_ingress.bridge.Receipt("PASS", "5"*64)
            outcome = transition_ingress.switch_with_observer(*routes, **args)
            self.assertEqual(outcome.activation_sha256, "5"*64)
            verified = args["guests"].verify_live_generation.call_args.args[0]
            self.assertEqual(verified.verb, transition_ingress.bridge.GUEST_VERIFY_GENERATION)
            self.assertEqual(verified.sequence, 7)
            self.assertEqual(verified.args["activation_receipt_sha256"], outcome.activation_sha256)
            self.assertEqual(steps, ["runtime", "observer", "close-prior", "stage", "verify", "rejoin", "controller", "lease", "guest", "ingress"])

    def test_preparing_generation_refreshes_closed_windows_proof_per_boundary(self):
        from ha_control.native_guest_activation import ActivationPins

        switcher = transition_ingress
        bridge = switcher.bridge
        binding = bridge.Binding(*(item*64 for item in "abcde"))
        generation = ServingGeneration("B", 2, 2, 3)
        stage = "f"*64
        activation = ActivationPins(*(item*64 for item in "123456"))
        pins = {"pins": generation, "postgres": generation, "activation": activation}
        settled = {"rejoin_stage_sha256": stage, "source_fence_wal": "0000000000000008",
                   "standby_rejoined": True, "authority_opened": False}
        sync = MagicMock(return_value=settled)
        legacy = MagicMock()
        guests = MagicMock()
        proof_digests = []

        def prepare(request, serving, reported_stage, floor, deadline, *, assert_rejoin):
            self.assertEqual(request.verb, bridge.GUEST_PREPARE_GENERATION)
            self.assertEqual(request.args["source_fence_wal"], "0000000000000008")
            for boundary in bridge.PREPARATION_BOUNDARIES:
                proof_digests.append(assert_rejoin("1"*64, boundary, deadline))
            return "7"*64

        guests.prepare_generation_runtime.side_effect = prepare
        result = switcher._prepare_target_generation(
            guests, binding, 4, generation, pins, stage, 8, float("inf"),
            check_sync=sync, assert_legacy_closed=legacy,
        )
        self.assertEqual(result["activation"].primary_runtime_receipt_sha256, "7"*64)
        self.assertEqual(len(set(proof_digests)), 4)
        self.assertEqual(sync.call_count, 4)
        self.assertEqual(legacy.call_count, 4)
        sync.return_value = settled | {"authority_opened": True}
        with self.assertRaisesRegex(TransitionBlocked, "replacement_replication_unconfirmed"):
            switcher._prepare_target_generation(
                guests, binding, 4, generation, pins, stage, 8, float("inf"),
                check_sync=sync, assert_legacy_closed=legacy,
            )

        guests.reset_mock()
        guests.prepare_generation_runtime.side_effect = OSError("response lost")
        sync.return_value = settled

        def readback(request, deadline, *, assert_rejoin):
            self.assertEqual(request.verb, bridge.GUEST_OBSERVE_PREPARATION)
            self.assertEqual(request.sequence, 5)
            for boundary in bridge.PREPARATION_OBSERVATION_BOUNDARIES:
                self.assertTrue(bridge._hash(assert_rejoin("2"*64, boundary, deadline)))
            return "9"*64

        guests.observe_generation_preparation.side_effect = readback
        recovered = switcher._prepare_target_generation(
            guests, binding, 4, generation, pins, stage, 8, float("inf"),
            check_sync=sync, assert_legacy_closed=legacy,
        )
        self.assertEqual(recovered["activation"].primary_runtime_receipt_sha256, "9"*64)
        guests.prepare_generation_runtime.assert_called_once()
        guests.observe_generation_preparation.assert_called_once()

    def test_lost_activation_is_followed_only_by_fresh_readback(self):
        bridge = transition_ingress.bridge
        guests = MagicMock()
        guests.activate_generation.side_effect = OSError("response lost")
        recorded = bridge.Receipt("PASS", "c"*64)
        guests.observe_generation_activation.return_value = recorded
        binding = bridge.Binding(*(character*64 for character in "abcde"))
        request = bridge.Request(binding, "B", bridge.GUEST_ACTIVATE_GENERATION, 7, "6"*64,
                                 {"generation_handoff_sha256": "7"*64, "effects_plan_sha256": "8"*64})
        live, guard = MagicMock(), MagicMock()
        deadline = float("inf")
        result = transition_ingress._activate_or_observe_generation(
            guests, request, deadline, assert_guard=guard, guest_boundary=live,
        )
        self.assertIs(result, recorded)
        guests.activate_generation.assert_called_once_with(request, deadline, assert_windows_current=live)
        guests.observe_generation_activation.assert_called_once()
        observed, limit = guests.observe_generation_activation.call_args.args
        self.assertEqual(observed.verb, bridge.GUEST_OBSERVE_GENERATION)
        self.assertEqual(observed.sequence, request.sequence + 1)
        self.assertNotEqual(observed.nonce, request.nonce)
        self.assertEqual(observed.args, request.args)
        self.assertEqual(limit, deadline)
        self.assertIs(guests.observe_generation_activation.call_args.kwargs["assert_guard"], guard)

    def test_roundtrip_uses_verified_b_service_routes_and_closure_for_return(self):
        switcher = transition_ingress
        gen_b, gen_a = ServingGeneration("B", 2, 2, 3), ServingGeneration("A", 3, 3, 4)
        windows = object()
        starter = SimpleNamespace(windows=windows, start_sha256="e"*64)
        next_starter = SimpleNamespace(windows=windows, start_sha256="e"*64, attempted=False)
        prior = {"starter": starter, "root": "prior-root", "live_sha256": "c"*64, "old_vm_id": "prior-old-vm"}
        observer_binding = {"runtime_sha256": "f"*64, "callback_config": b"callback",
                            "callback_config_sha256": "1"*64}
        first = switcher.CompletedTransition(
            "B-route", "B-relay", {}, {"plan": b"B"}, {"PlanSha256": "d"*64}, gen_b, observer_binding,
        )
        second = switcher.CompletedTransition("A-route", "A-relay", {}, {}, {}, gen_a)
        failover = {"execution": SimpleNamespace(spec={"attempt_id": "a"*64}), "controller_setup": prior,
                    "generation": gen_b, "target": "B", "previous_runtime_sha256": "0"*64,
                    "legacy_plan": "2"*64, "legacy_attempt": "3"*64}
        returning = {"execution": SimpleNamespace(spec={"attempt_id": "b"*64}),
                     "controller_setup": prior | {"starter": next_starter},
                     "generation": gen_a, "target": "A", "previous_runtime_sha256": "0"*64,
                     "legacy_plan": "2"*64, "legacy_attempt": "3"*64,
                     "previous_callback": b"callback", "previous_callback_sha256": "1"*64,
                     "stage_service": {"approval": MagicMock(), "preflight_path": "pf", "scm_verifier_path": "scm"}}
        prepare = MagicMock(return_value=returning)

        def return_leg(**options):
            self.assertEqual((options["previous"], options["relay"]), (first.active, first.relay))
            options["stage_service"]["close_previous_service"](second.controller, gen_a, 90)
            return second

        with patch.object(switcher, "switch_with_observer", side_effect=[first, second]) as run, \
                patch.object(switcher, "close_previous_transition_service") as close:
            run.side_effect = lambda **options: first if options["generation"] == gen_b else return_leg(**options)
            self.assertEqual(switcher.switch_roundtrip(None, None, "A-route", "A-relay",
                                                      failover=failover, prepare_return=prepare), (first, second))
        prepare.assert_called_once_with(first)
        close.assert_called_once_with(starter, first.controller, first.parameters, gen_b, second.controller, gen_a,
                                      previous_root="prior-root", previous_attempt="a"*64, previous_old_vm_id="prior-old-vm",
                                      live_sha256="c"*64, deadline=90)

    def test_rejoin_resume_matches_exact_recorded_controller_and_parameters(self):
        controller = {"plan_sha256": "c"*64}
        parameters = {"PlanSha256": "c"*64, "ApprovalSha256": "d"*64}
        with tempfile.TemporaryDirectory() as directory:
            execution = SimpleNamespace(directory=Path(directory).resolve(),
                                        spec={"plan_sha256": "a"*64, "attempt_id": "b"*64})
            bind = transition_ingress._bind_rejoin_controller
            bind(execution, controller, parameters, resume=False)
            bind(execution, controller, parameters, resume=True)
            with self.assertRaisesRegex(TransitionBlocked, "controller_unbound"):
                bind(execution, controller, parameters | {"ApprovalSha256": "f"*64}, resume=True)
            with self.assertRaisesRegex(TransitionBlocked, "controller_unbound"):
                bind(execution, {"plan_sha256": "e"*64}, parameters | {"PlanSha256": "e"*64}, resume=True)

    def test_windows_rejoin_resume_never_dispatches_stage_rejoin(self):
        starter = MagicMock()
        stage = {"rejoin_stage_sha256": "f"*64, "source_fence_wal": "0000000000000008"}
        starter.run_rejoin.return_value = stage
        controller, parameters = {"plan": b"fixture"}, {"PlanSha256": "a"*64}
        for resume, first_mode in ((False, "stage-rejoin"), (True, "observe-rejoin-stage")):
            starter.reset_mock()
            pin, floor, current, synchronized = transition_ingress._windows_rejoin(
                starter, controller, parameters, live_sha256="b"*64, root="fixture", deadline=90, resume=resume,
            )
            self.assertEqual(pin, stage["rejoin_stage_sha256"])
            self.assertEqual(floor, 8)
            self.assertEqual(current(80), pin)
            self.assertEqual(synchronized(70), stage)
            expected = [first_mode, "observe-rejoin-stage", "check-rejoin"]
            if not resume:
                expected.insert(0, "initialize")
            self.assertEqual([call.args[2] for call in starter.run_rejoin.call_args_list], expected)
            self.assertEqual(len({call.args[3] for call in starter.run_rejoin.call_args_list}), len(expected))
            if resume:
                self.assertEqual(starter.mock_calls[0][0], "rejoin_stage_intent_present")
                starter.restore_rejoin_args.assert_called_once()
            else:
                starter.restore_rejoin_args.assert_not_called()

    def test_windows_rejoin_rejects_changed_source_wal_on_either_read(self):
        starter = MagicMock()
        stage = {"rejoin_stage_sha256": "f"*64, "source_fence_wal": "0000000000000008"}
        starter.run_rejoin.return_value = stage
        _, _, current, synchronized = transition_ingress._windows_rejoin(
            starter, {}, {}, live_sha256="b"*64, root="fixture", deadline=90, resume=False,
        )
        starter.run_rejoin.return_value = stage | {"source_fence_wal": "0000000000000007"}
        for observe in (current, synchronized):
            with self.assertRaisesRegex(TransitionBlocked, "source_fence_wal_unconfirmed"):
                observe(80)

    def test_rejoin_resume_before_stage_restores_initialize_without_replaying_it(self):
        starter = MagicMock()
        starter.rejoin_stage_intent_present.return_value = False
        starter.run_rejoin.return_value = {"rejoin_stage_sha256": "f"*64, "source_fence_wal": "0000000000000008"}
        controller, parameters = {"plan": b"fixture"}, {"PlanSha256": "a"*64}
        transition_ingress._windows_rejoin(
            starter, controller, parameters, live_sha256="b"*64, root="fixture", deadline=90, resume=True,
        )
        self.assertEqual([call[0] for call in starter.mock_calls],
                         ["rejoin_stage_intent_present", "restore_rejoin_args", "run_rejoin"])
        self.assertEqual(starter.restore_rejoin_args.call_args.args[2], "initialize")
        self.assertEqual(starter.run_rejoin.call_args.args[2], "stage-rejoin")

    def test_replacement_unknown_start_reconciles_without_replay(self):
        bridge = transition_ingress.bridge
        binding = bridge.Binding(*(character * 64 for character in "abcde"))
        pin = "1" * 64
        args = {"bootstrap_manifest_sha256": "2" * 64, "rejoin_stage_sha256": pin}
        target = bridge.Request(binding, "B", bridge.GUEST_TRANSITION_TARGET, 1, "3" * 64, {})
        start = bridge.Request(binding, "A", bridge.GUEST_START_REPLACEMENT, 3, "4" * 64, args)
        observe = bridge.Request(binding, "A", bridge.GUEST_OBSERVE_REPLACEMENT, 4, "5" * 64, args)
        guests = MagicMock()
        guests.start_replacement_standby.side_effect = OSError("connection interrupted")
        guests.confirm_replacement_rejoin.side_effect = [
            bridge.BridgeRejected("rejoin not yet visible"), bridge.Receipt("PASS", "6" * 64),
        ]
        stage, sync = MagicMock(return_value=pin), MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            execution = SimpleNamespace(directory=Path(directory).resolve(), spec=binding.fields())
            call = (execution, guests, ServingGeneration("B", 2, 2, 2), target, start, observe, "a"*64, stage, sync, 90)
            with self.assertRaisesRegex(bridge.BridgeRejected, "rejoin not yet visible"):
                transition_ingress._rejoin_replacement(*call)
            self.assertTrue((execution.directory / "replacement-start-intent.json").is_file())
            fresh_target = replace(target, sequence=5, nonce="7" * 64)
            fresh_start = replace(start, sequence=7, nonce="8" * 64)
            fresh_observe = replace(observe, sequence=8, nonce="9" * 64)
            transition_ingress._rejoin_replacement(
                execution, guests, call[2], fresh_target, fresh_start, fresh_observe, "a"*64, stage, sync, 90,
            )
        guests.publish_replacement_standby.assert_called_once()
        published = guests.publish_replacement_standby.call_args.args[0]
        self.assertEqual(published.sequence, start.sequence - 1)
        self.assertEqual(published.args["installation_id"], "a"*64)
        guests.start_replacement_standby.assert_called_once_with(start, 90, assert_rejoin_current=stage)
        self.assertEqual(guests.confirm_replacement_rejoin.call_count, 2)
        guests.confirm_replacement_rejoin.assert_called_with(
            fresh_observe, 90, assert_rejoin_current=stage, check_synchronized=sync,
        )

    def test_callback_rotation_preserves_primary_and_legacy_pins_in_both_directions(self):
        from ha_runtime.seven_capture import encoded

        a, b, new_a, new_b = (f"{i:08x}-1111-1111-1111-111111111111" for i in range(1, 5))
        base = {
            "installation_id": "a"*64, "vm_a": a, "vm_b": b,
            "a_disk_paths_sha256": "1"*64, "b_disk_paths_sha256": "2"*64,
            "driver_path": "driver", "driver_sha256": "3"*64, "reactivation_plan_path": "reactivation",
            "runtime_path": "legacy", "runtime_sha256": "4"*64, "source_loader_sha256": "5"*64,
            "source_manifest_path": "manifest", "source_manifest_sha256": "6"*64, "source_root": "source",
            "vm_observer_path": "observer", "vm_observer_sha256": "7"*64, "wsl_uid": 1000,
        }
        release_commit = "a"*40
        source_base = f"/home/operator/.local/state/n100-ha/cutover-source-{release_commit}"
        release = {
            "vm_observer_path": rf"C:\ProgramData\N100HA-InitialCutover\{release_commit}\new-observer.ps1",
            "vm_observer_sha256": "a"*64, "source_loader_sha256": "b"*64, "source_manifest_sha256": "c"*64,
            "source_root": source_base + "/infra/k8s", "source_manifest_path": source_base + ".manifest.json",
        }
        runtime_b = {"runtime_path": "phase-b", "runtime_sha256": "8"*64}
        runtime_a = {"runtime_path": "phase-a", "runtime_sha256": "9"*64}
        next_a = {"node": "A", "vm_id": new_a, "installation_id": "b"*64, "disk_paths_sha256": "c"*64}
        next_b = {"node": "B", "vm_id": new_b, "installation_id": "d"*64, "disk_paths_sha256": "e"*64}

        def rotate(previous, runtime, primary, replacement, code=release):
            return transition_execution.observer_callback_config(
                previous, caddy_runtime._sha(previous), runtime, primary, replacement, code,
            )

        raw = encoded(base)
        after_b = rotate(raw, runtime_b, "B", next_a)
        expected_b = base | release | runtime_b | {
            "vm_a": new_a, "installation_id": "b"*64, "vm_b_installation_id": "a"*64,
            "a_disk_paths_sha256": "c"*64,
        }
        self.assertEqual(json.loads(after_b["callback_config"]), expected_b)
        self.assertEqual(caddy_runtime._sha(after_b["callback_config"]), after_b["callback_config_sha256"])
        after_a = rotate(after_b["callback_config"], runtime_a, "A", next_b)
        self.assertEqual(json.loads(after_a["callback_config"]), expected_b | runtime_a | {
            "vm_b": new_b, "vm_b_installation_id": "d"*64, "b_disk_paths_sha256": "e"*64,
        })
        for rejected in (next_a | {"vm_id": a}, next_a | {"vm_id": b}, next_a | {"node": "B"},
                         next_a | {"installation_id": "a"*64}):
            with self.subTest(rejected=rejected), self.assertRaisesRegex(TransitionBlocked, "callback_unbound"):
                rotate(raw, runtime_b, "B", rejected)
        with self.assertRaisesRegex(TransitionBlocked, "callback_unbound"):
            transition_execution.observer_callback_config(raw, "0"*64, runtime_b, "B", next_a, release)

        for invalid in ({}, release | {"driver_sha256": "d"*64},
                        release | {"vm_observer_path": r"C:\Elsewhere\observer.ps1"},
                        release | {"source_manifest_path": source_base + "/wrong.json"}):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(TransitionBlocked, "callback_unbound"):
                rotate(raw, runtime_b, "B", next_a, invalid)

    def test_observer_runtime_publishes_exact_prior_baseline_and_new_command_scope(self):
        import os
        import time

        from ha_runtime.seven_capture import encoded

        caddy = caddy_runtime
        legacy_plan, prior_attempt, next_attempt = "a"*64, "b"*64, "c"*64
        owner = {"pid": os.getpid(), "start": 100, "process": "d"*64}
        with tempfile.TemporaryDirectory() as temp, patch.object(caddy, "_private_dir", side_effect=Path):
            root = Path(temp).resolve()
            prior = root/prior_attempt
            next_dir = root/next_attempt
            ingress = next_dir/"ingress"
            for directory in (prior, next_dir, ingress):
                directory.mkdir(mode=0o700)
            raw = encoded({"schema_version": 1, "plan_sha256": legacy_plan, "attempt_id": prior_attempt,
                           "writer_baseline": {"writers": ["original"]}, "coordinator": {"pid": 99}})
            caddy._write(prior/"runtime.json", raw, 0o600)
            spec = {"directory": str(ingress), "plan_sha256": "e"*64, "attempt_id": next_attempt,
                    "project": "personal-server", "coordinator": owner}
            execution = transition_execution.TransitionExecution(spec)
            closed = MagicMock()
            with patch.object(caddy.CaddyExecution, "identity", return_value=owner), patch.object(
                caddy, "_lock_held_by", return_value=True,
            ):
                binding = execution.publish_observer_runtime(previous_sha256=caddy._sha(raw), legacy_plan=legacy_plan,
                                                            legacy_attempt=prior_attempt, assert_legacy_closed=closed)
            self.assertEqual(closed.call_count, 2)
            active = next_dir/"runtime.json"
            self.assertEqual(caddy._sha(active.read_bytes()), binding["runtime_sha256"])
            data = json.loads(active.read_bytes())
            self.assertEqual(data["ingress_execution"], spec)
            self.assertNotIn("coordinator", data)
            mount = MagicMock()
            mount.read_bytes.return_value = b"boot"
            with patch.object(wsl_writer_proof, "BOOT_ID", mount), patch.object(
                wsl_writer_proof, "observe_closed", return_value="f"*64,
            ) as observer, patch.object(wsl_writer_proof, "_recovery_settled", return_value="1"*64):
                options = {"wsl_boot_sha256": caddy._sha(b"boot"), "challenge": "2"*64, "deadline": time.monotonic()+2}
                wsl_writer_proof.prove_runtime(**binding, **options)
                self.assertEqual(observer.call_args.args[0], {"writers": ["original"]})
                forged = encoded(data | {"writer_baseline": {"writers": []}})
                active.unlink()
                caddy._write(active, forged, 0o400)
                with self.assertRaisesRegex(TransitionBlocked, "ingress_observer_runtime_unbound"):
                    wsl_writer_proof.prove_runtime(**(binding | {"runtime_sha256": caddy._sha(forged)}), **options)
                observer.assert_called_once()

    def test_writer_observer_permits_only_exact_locked_ingress_commands(self):
        import fcntl
        import os

        from ha_runtime.seven_capture import encoded

        caddy = caddy_runtime
        pid = os.getpid()
        coordinator = (1, 100, [b"python", b"coordinator"], b"/usr/bin/python3")
        owner = {"pid": pid, "start": 100, "process": caddy._sha(b"\0".join([coordinator[3], *coordinator[2]]))}
        with tempfile.TemporaryDirectory() as temp, patch.object(caddy, "_private_dir", side_effect=Path):
            directory = Path(temp).resolve()
            for name in ("main", "relay"):
                (directory/name).mkdir(mode=0o700)
            spec = {"directory": str(directory), "plan_sha256": "a"*64, "attempt_id": "b"*64,
                    "project": "personal-server", "coordinator": owner}
            raw, container, guest = b"candidate", "c"*64, "192.168.100.3"
            outer = {"plan_sha256": spec["plan_sha256"], "attempt_id": spec["attempt_id"],
                     "old_caddy_sha256": "d"*64, "caddy_candidate_sha256": "e"*64, "overlay_sha256": "f"*64,
                     "old_relay_sha256": caddy._sha(container.encode()), "car_route_sha256": "1"*64,
                     "target_sha256": caddy._sha(guest.encode())}
            main_intent = {"original_container_sha256": outer["old_caddy_sha256"],
                           "candidate_sha256": outer["caddy_candidate_sha256"],
                           "overlay_sha256": outer["overlay_sha256"], "isolated_sha256": caddy._sha(raw)}
            relay_intent = {"previous_container_sha256": outer["old_relay_sha256"],
                            "route_pin": outer["car_route_sha256"], "guest": guest}
            for filename, data in (("ingress-switch-intent.json", outer), ("main/caddy-apply-intent.json", main_intent),
                                   ("relay/car-relay-replacement-intent.json", relay_intent)):
                caddy._write(directory/filename, encoded(data), 0o600)
            main = [part.encode() for part in caddy._execution_command(spec["project"])]
            stop = [part.encode() for part in (*caddy.DOCKER, "stop", "--time", "5", container)]
            foreign_stop = [*stop[:-1], b"9"*64]
            processes = {pid: coordinator, 101: (pid, 101, main, main[0]), 102: (pid, 102, stop, stop[0]),
                         103: (pid, 102, foreign_stop, stop[0])}

            def locked(path, candidate):
                if candidate != pid:
                    return False
                try:
                    with path.open("rb") as stream:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                return False

            execution = transition_execution.TransitionExecution(spec)
            first, relay = MagicMock(project=spec["project"], execution=None), MagicMock(execution=None)
            allowed = transition_execution.authorized
            with patch.object(caddy, "_process", side_effect=processes.__getitem__), patch.object(
                caddy, "_lock_held_by", side_effect=locked,
            ), execution.guard(first, relay):
                self.assertFalse(allowed(Path("/proc/102"), stop, spec))
                with execution.stop_relay(directory/"relay", container):
                    self.assertTrue(allowed(Path("/proc/102"), stop, spec))
                    self.assertFalse(allowed(Path("/proc/103"), foreign_stop, spec))
                self.assertFalse(allowed(Path("/proc/102"), stop, spec))
                with self.assertRaises(FileExistsError):
                    with execution.stop_relay(directory/"relay", container):
                        self.fail("unsettled relay stop was replayed")
                with execution.stop_relay(directory/"relay", container, settled=True):
                    self.assertTrue(allowed(Path("/proc/102"), stop, spec))
                with execution.open(directory/"main", spec["project"], raw):
                    self.assertTrue(allowed(Path("/proc/101"), main, spec))
                    self.assertFalse(allowed(Path("/proc/101"), main, spec | {"attempt_id": "2"*64}))
                override = os.fsencode(directory/"main"/"caddy-ha-compose.json")
                read = [main[0], *wsl_writer_proof.CADDY_COMPOSE_GLOBALS, b"personal-server",
                        b"--project-directory", wsl_writer_proof.CADDY_ROOT,
                        b"--env-file", wsl_writer_proof.CADDY_ROOT + b"/.env"]
                for path in (*wsl_writer_proof.CADDY_BASE_FILES, override):
                    read.extend([b"-f", path])
                read.extend([b"config", b"--format", b"json"])
                self.assertIsNone(wsl_writer_proof._recovery_command_reason(read, main[0], spec))
                self.assertEqual(wsl_writer_proof._recovery_command_reason(read, main[0]),
                                 "legacy_recovery_linux_compose_command")
                self.assertEqual(wsl_writer_proof._recovery_command_reason(read[:-3] + [b"up"], main[0], spec),
                                 "legacy_recovery_linux_compose_command")

    def test_relay_first_then_caddy_and_readback_after_unknown_response(self):
        old_ip, new_ip = "192.168.100.2", "192.168.100.3"
        old_main = {"container_sha256": "a"*64}
        new_main = {"container_sha256": "b"*64, "candidate_sha256": "c"*64, "live_sha256": "d"*64}
        car_receipt = {"container_sha256": "e"*64, "candidate_sha256": "f"*64, "public_proof_sha256": []}
        main, car, public, guard = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        guest = MagicMock(return_value="1"*64)
        generation = ServingGeneration("B", 2, 2, 3)
        order = []
        with tempfile.TemporaryDirectory() as temp, patch.object(transition_ingress.caddy, "_private_dir", side_effect=Path):
            root = Path(temp).resolve()
            old_prepared = caddy_runtime.PreparedCaddy({}, root/"previous.json", root/"previous.caddy", "", "", "", "")
            active = caddy_runtime.ActiveCaddy(old_prepared, old_main, b"fixed source", old_ip, "2"*64)
            relay = car_callback_runtime.Relay("2"*64, "sha256:"+"3"*64, "4"*64, "192.168.100.4",
                                               root/"relay.caddy", old_ip, "5"*64, "6"*64, "7"*64)
            (root/"main").mkdir()
            prepared = caddy_runtime.PreparedCaddy({}, root/"main"/"caddy-ha-compose.json",
                                                   root/"main"/"Caddyfile", "", "c"*64, "", "")
            main.prepare.return_value = prepared
            car.replace.side_effect = lambda *args, **kwargs: order.append("relay") or car_receipt

            def uncertain_main(*args):
                self.assertTrue((root/transition_ingress.INTENT).is_file())
                order.append("main")
                (root/"main"/"caddy-apply-intent.json").touch()
                raise TransitionBlocked("caddy_reconciliation_required")

            route = "8"*64
            main.apply.side_effect = uncertain_main
            with self.assertRaisesRegex(TransitionBlocked, "caddy_reconciliation_required"):
                transition_ingress.switch_ingress(main, car, active, relay, generation, new_ip, route, root,
                                                   expected_mtls_sha256={}, assert_guard=guard, verify_guest=guest,
                                                   plan_sha256="a"*64, attempt_id="b"*64,
                                                   verify_public=public)
            self.assertEqual(order, ["relay", "main"])
            public.assert_not_called()
            main.observe_applied.return_value, car.observe.return_value = new_main, car_receipt
            with self.assertRaisesRegex(TransitionBlocked, "docker_not_settled"):
                transition_ingress.resume_ingress(
                    main, car, active, relay, prepared, generation, new_ip, route, root,
                    assert_guard=guard, assert_settled=MagicMock(side_effect=TransitionBlocked("docker_not_settled")),
                    verify_guest=guest, verify_public=public, plan_sha256="a"*64, attempt_id="b"*64,
                )
            main.observe_applied.assert_not_called()
            current, new_relay, receipt = transition_ingress.resume_ingress(
                main, car, active, relay, prepared, generation, new_ip, route, root,
                assert_guard=guard, assert_settled=MagicMock(), verify_guest=guest, verify_public=public,
                plan_sha256="a"*64, attempt_id="b"*64,
            )
            main.apply.assert_called_once()
            car.replace.assert_called_once()
            public.assert_called_once_with(3)
            self.assertEqual((current.primary_ip, new_relay.guest), (new_ip, new_ip))
            self.assertEqual(current.relay_container_sha256, new_relay.container_sha256)
            self.assertTrue((root/transition_ingress.RECEIPT).is_file())
            self.assertEqual(json.loads((root/transition_ingress.RECEIPT).read_bytes()), receipt)
            main.adopt_unapplied.assert_not_called()
            car.resume.assert_not_called()


class CarCallbackOriginTests(unittest.TestCase):
    def test_retarget_requires_exact_existing_route_and_fresh_marker(self):
        render, retarget = car_callback_ingress.render, car_callback_ingress.retarget
        a, b, old_pin, new_pin = "192.168.100.2", "192.168.100.3", "a" * 64, "b" * 64
        active = render(a, route_pin=old_pin)
        candidate = retarget(active, a, old_pin, b, new_pin)
        self.assertEqual(candidate, render(b, route_pin=new_pin))
        self.assertNotIn(old_pin.encode(), candidate)
        for current, host, marker in ((active + b"\n", b, new_pin), (active, a, new_pin), (active, b, old_pin)):
            with self.assertRaises(TransitionBlocked):
                retarget(current, a, old_pin, host, marker)

    def test_independent_relay_reconciles_unknown_run_without_second_mutation(self):
        relay = car_callback_runtime
        caddy = relay.caddy
        a, b = '192.168.100.2', '192.168.100.3'
        old_pin, new_pin = 'a' * 64, 'b' * 64
        old_id, new_id, network = '1' * 64, '2' * 64, '3' * 64
        image, address = 'sha256:' + '4' * 64, '172.20.0.4'
        calls = []
        volume = {'Mountpoint': '/fixture/mtls'}
        fingerprints = dict.fromkeys(caddy.TLS_FILES, 'f' * 64)
        tls_output = ''.join(f'{pin}  {caddy.MOUNT}/{name}\n' for name, pin in fingerprints.items()).encode()
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory).resolve()
            old_path = root / 'old'
            old_path.write_bytes(car_callback_ingress.render(a, route_pin=old_pin))
            previous = relay.Relay(caddy._sha(old_id.encode()), image, network, address, old_path, a, old_pin,
                                  caddy._pin(volume), caddy._pin(fingerprints))
            current = {'id': old_id, 'path': old_path, 'running': True, 'labels': {}}

            def inspect():
                return {
                    'Id': current['id'], 'Name': '/' + relay.NAME, 'Image': image,
                    'State': {'Running': current['running'], 'Status': 'running' if current['running'] else 'exited'},
                    'Config': {'User': '10001:10001', 'Entrypoint': ['/usr/bin/caddy'], 'Labels': current['labels'],
                               'Cmd': ['run', '--config', caddy.CONFIG, '--adapter', 'caddyfile']},
                    'HostConfig': {'ReadonlyRootfs': True, 'Privileged': False, 'LogConfig': {'Type': 'none'},
                                   'PortBindings': {}, 'CapDrop': ['ALL'], 'CapAdd': ['CAP_NET_BIND_SERVICE'],
                                   'SecurityOpt': ['no-new-privileges'], 'RestartPolicy': {'Name': 'unless-stopped'}},
                    'Mounts': [
                        {'Destination': caddy.CONFIG, 'Type': 'bind', 'Source': str(current['path']), 'RW': False},
                        {'Destination': caddy.MOUNT, 'Type': 'volume', 'Name': caddy.MTLS_VOLUME, 'RW': False,
                         'Source': volume['Mountpoint']},
                    ],
                    'NetworkSettings': {'Networks': {'test': {'NetworkID': network, 'IPAddress': address}}},
                }

            def docker(*args, **options):
                calls.append(args)
                if args[0] == 'ps':
                    return (current['id'] or '').encode()
                if args[0] == 'inspect':
                    self.assertEqual(args[1], current['id'])
                    return json.dumps([inspect()]).encode()
                if args[:2] == ('network', 'inspect'):
                    return json.dumps([{'Id': network, 'Driver': 'bridge', 'Scope': 'local',
                                        'Containers': {current['id']: {}} if current['running'] else {}}]).encode()
                if args[:2] == ('volume', 'inspect'):
                    return json.dumps([volume]).encode()
                if args[0] == 'exec':
                    return current['path'].read_bytes() if args[2] == '/bin/cat' else tls_output
                if args[:2] == ('run', '--rm'):
                    return tls_output
                if args[0] == 'stop':
                    self.assertTrue((root / relay.INTENT).exists())
                    self.assertEqual(args[-1], old_id)
                    current['running'] = False
                    raise TransitionBlocked('fixture_ambiguous_stop')
                if args[0] == 'rm':
                    self.assertEqual(args, ('rm', old_id))
                    self.assertFalse(current['running'])
                    current['id'] = None
                if args[:2] == ('run', '--detach'):
                    labels = dict(args[index + 1].split('=', 1) for index, value in enumerate(args) if value == '--label')
                    self.assertEqual(labels, relay.LABELS | {'n100-ha.car-relay-route': new_pin})
                    current.update(id=new_id, path=root / 'Caddyfile', running=True, labels=labels)
                    raise TransitionBlocked('fixture_ambiguous_run')
                return b''

            instance = relay.CallbackRuntime(docker)
            proof = MagicMock(side_effect=['5' * 64, '6' * 64, '7' * 64])
            with self.assertRaisesRegex(TransitionBlocked, 'car_relay_reconciliation_required'):
                instance.replace(previous, b, new_pin, root, assert_guard=MagicMock(), verify_origin=proof)
            self.assertEqual(instance.status(previous, root, assert_guard=MagicMock()), 'old_stopped')
            with self.assertRaisesRegex(TransitionBlocked, 'fixture_daemon_unsettled'):
                instance.resume(previous, root, assert_guard=MagicMock(),
                                assert_settled=MagicMock(side_effect=TransitionBlocked('fixture_daemon_unsettled')))
            self.assertFalse(any(args[0] == 'rm' for args in calls))
            with self.assertRaisesRegex(TransitionBlocked, 'car_relay_reconciliation_required'):
                instance.resume(previous, root, assert_guard=MagicMock(), assert_settled=MagicMock(),
                                verify_origin=proof)
            self.assertEqual(instance.status(previous, root, assert_guard=MagicMock()), 'new_running')
            with patch.object(relay.time, 'sleep') as pause:
                receipt = instance.observe(previous, root, assert_guard=MagicMock(), verify_origin=proof)
            self.assertEqual([call.args for call in pause.call_args_list], [(10,), (10,)])
            self.assertEqual(receipt['container_sha256'], caddy._sha(new_id.encode()))
            self.assertEqual(proof.call_count, 3)
            with self.assertRaisesRegex(TransitionBlocked, 'car_relay_reconciliation_required'):
                instance.replace(previous, b, new_pin, root, assert_guard=MagicMock(), verify_origin=proof)
            self.assertEqual([args[0] for args in calls if args[0] in {'stop', 'rm'}], ['stop', 'rm'])
            self.assertEqual(sum(args[:2] == ('run', '--detach') for args in calls), 1)

    def test_public_origin_requires_current_marker_and_healthy_upstream(self):
        pin = "a" * 64
        marked = car_callback_ingress.render("192.168.100.2", route_pin=pin)
        self.assertIn(f"{car_callback_ingress.ROUTE_HEADER} {pin}".encode(), marked)
        self.assertIn(b"path /health", marked)
        self.assertNotIn(car_callback_ingress.ROUTE_HEADER.encode(),
                         car_callback_ingress.render("192.168.100.2"))
        with self.assertRaises(TransitionBlocked):
            car_callback_ingress.render("192.168.100.2", route_pin="invalid\nrespond 200")

        cases = [(200, [pin], b"ok", True),
                 (200, [], b"ok", False),
                 (200, ["b" * 64], b"ok", False),
                 (200, [pin, pin], b"ok", False),
                 (302, [pin], b"ok", False),
                 (502, [pin], b"ok", False),
                 (200, [pin], b"not healthy", False)]
        for status, markers, body, valid in cases:
            with self.subTest(status=status, markers=markers, body=body):
                with patch.object(car_callback_ingress, "HTTPSConnection") as connection:
                    response = MagicMock(status=status, headers=Message())
                    for value in markers:
                        response.headers.add_header(car_callback_ingress.ROUTE_HEADER, value)
                    response.read.side_effect = lambda size: body[:size]
                    connection.return_value.getresponse.return_value.__enter__.return_value = response
                    if valid:
                        self.assertRegex(car_callback_ingress.verify_public_origin(pin), r"^[a-f0-9]{64}$")
                    else:
                        with self.assertRaisesRegex(TransitionBlocked, "^car_callback_public_origin_unconfirmed$"):
                            car_callback_ingress.verify_public_origin(pin)
                    connection.assert_called_once_with("car.len.pe.kr", timeout=5)
                    self.assertRegex(connection.return_value.request.call_args.args[1], r"^/health\?ha_route_check=[a-f0-9]{32}$")
                    connection.return_value.close.assert_called_once()


class Backend:
    def __init__(self):
        self.actions, self.stopped, self.primary, self.ready, self.peer = [], False, False, False, False
        self.failure = None

    @contextmanager
    def guard(self):
        if self.failure == "legacy":
            raise TransitionBlocked("legacy_active")
        yield

    def close(self, state): self.actions.append("close"); self.ready = False
    def fenced(self, state): return self.stopped
    def fence(self, state, *, record_source_wal=None):
        self.actions.append("fence")
        if self.failure != "fence": self.stopped = True
        if self.failure == "fence_unknown": raise OSError("unknown")
    def promoted(self, state): return self.primary
    def promote(self, state):
        self.actions.append("promote")
        self.primary = True
        if self.failure == "promote_unknown": raise OSError("unknown")
    def rejoin(self, state):
        self.actions.append("rejoin")
        if self.failure == "rejoin": raise TransitionBlocked("peer_unavailable")
        self.peer = True
        if self.failure == "rejoin_unknown": raise OSError("unknown")
    def rejoined(self, state): return self.peer
    def validate(self, state):
        self.actions.append("validate")
        if self.failure == "proof": raise TransitionBlocked("peer_proof_unavailable")
    def open(self, state):
        self.actions.append("open"); self.ready = True
        if self.failure == "open_unknown": raise OSError("unknown")
    def assert_ready(self, state):
        if not self.ready: raise TransitionBlocked("not_ready")
    def assert_rejoin_pending(self, state):
        if not self.primary or not self.stopped or self.ready:
            raise TransitionBlocked("rejoin_pending_unconfirmed")


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "journal.json"
        self.backend = Backend()
        self.control = Controller(self.path, self.backend)
        self.control.initialize(cluster="synthetic", generation=3, old="A", target="B", timeline=1)

    def test_fence_promote_rejoin_proof_open_order(self):
        self.assertEqual(self.control.advance()["phase"], "ready")
        self.assertEqual(self.backend.actions, ["close", "fence", "promote", "rejoin", "validate", "open"])

    def test_stage_rejoin_returns_durable_closed_primary_and_reads_on_repeat(self):
        staged = self.control.advance(stop_at_rejoin=True)
        self.assertEqual(staged, Journal(self.path).read())
        self.assertEqual(staged["phase"], "rejoining")
        self.assertFalse(self.backend.ready)
        self.assertEqual(self.backend.actions, ["close", "fence", "promote"])
        self.assertEqual(self.control.advance(stop_at_rejoin=True), staged)
        self.assertEqual(self.backend.actions, ["close", "fence", "promote"])
        self.assertEqual(self.control.advance()["phase"], "ready")
        with self.assertRaisesRegex(TransitionBlocked, "rejoin_staging_phase_invalid"):
            self.control.advance(stop_at_rejoin=True)

    def test_ready_lease_owner_inherits_both_guards_without_a_gap(self):
        active = []

        @contextmanager
        def guard():
            active.append(True)
            try:
                yield
            finally:
                active.pop()

        def serve(state):
            self.assertEqual(state, Journal(self.path).read())
            self.assertEqual(state["phase"], "ready")
            self.assertEqual(active, [True])
            with self.assertRaisesRegex(TransitionBlocked, "controller_lock_unavailable"):
                with exclusive(self.path.with_suffix(".lock")):
                    self.fail("second controller acquired lock during lease handoff")
            return "served"

        with patch.object(self.backend, "guard", guard):
            self.assertEqual(self.control.advance(on_ready=serve), "served")
        self.assertEqual(active, [])

    def test_unconfirmed_fence_never_promotes(self):
        self.backend.failure = "fence"
        with self.assertRaisesRegex(TransitionBlocked, "positive_fence_required"):
            self.control.advance()
        self.assertNotIn("promote", self.backend.actions)
        self.assertEqual(Journal(self.path).read()["phase"], "fencing")

    def test_unknown_fence_is_reobserved_without_repeating_stop(self):
        self.backend.failure = "fence_unknown"
        with self.assertRaises(TransitionBlocked): self.control.advance()
        self.backend.failure = None
        Controller(self.path, self.backend).advance()
        self.assertEqual(self.backend.actions.count("fence"), 1)

    def test_wal_is_saved_before_stop_and_survives_a_lost_stop_response(self):
        stop = self.backend.fence
        self.backend.failure = "fence_unknown"

        def stop_with_record(state, *, record_source_wal):
            record_source_wal(0x30)
            record_source_wal(0x20)
            self.assertEqual(Journal(self.path).read()["source_fence_wal"], 0x30)
            stop(state)

        with patch.object(self.backend, "fence", side_effect=stop_with_record):
            with self.assertRaises(TransitionBlocked):
                self.control.advance()
        self.backend.failure = None
        state = Controller(self.path, self.backend).advance()
        self.assertEqual(state["source_fence_wal"], 0x30)
        self.assertEqual(self.backend.actions.count("fence"), 1)

    def test_unknown_promotion_is_reobserved_without_second_promotion(self):
        self.backend.failure = "promote_unknown"
        with self.assertRaises(TransitionBlocked): self.control.advance()
        self.backend.failure = None
        Controller(self.path, self.backend).advance()
        self.assertEqual(self.backend.actions.count("promote"), 1)

    def test_rejoin_or_proof_failure_keeps_writes_closed(self):
        for failure in ("rejoin", "proof"):
            with self.subTest(failure=failure):
                self.backend.failure = failure
                with self.assertRaises(TransitionBlocked): self.control.advance()
                self.assertNotIn("open", self.backend.actions)

    def test_unknown_rejoin_is_observed_without_second_resource_creation(self):
        self.backend.failure = "rejoin_unknown"
        with self.assertRaises(TransitionBlocked): self.control.advance()
        self.backend.failure = None
        Controller(self.path, self.backend).advance()
        self.assertEqual(self.backend.actions.count("rejoin"), 1)

    def test_unknown_open_closes_again_and_reproves_without_new_generation(self):
        self.backend.failure = "open_unknown"
        with self.assertRaises(TransitionBlocked): self.control.advance()
        self.assertFalse(self.backend.ready)
        self.assertEqual(self.backend.actions[-2:], ["open", "close"])
        self.backend.failure = None
        state = Controller(self.path, self.backend).advance()
        self.assertEqual(state["generation"], 3)
        self.assertEqual(self.backend.actions[-3:], ["close", "validate", "open"])

    def test_ready_record_failure_revokes_without_starting_lease_service(self):
        save = self.control.journal.save

        def fail_ready(state):
            if state["phase"] == "ready":
                raise TransitionBlocked("controller_journal_not_durable")
            save(state)

        serve = MagicMock()
        with patch.object(self.control.journal, "save", fail_ready):
            with self.assertRaisesRegex(TransitionBlocked, "controller_journal_not_durable"):
                self.control.advance(on_ready=serve)
        serve.assert_not_called()
        self.assertEqual(Journal(self.path).read()["phase"], "validating")
        self.assertFalse(self.backend.ready)
        self.assertEqual(self.backend.actions[-2:], ["open", "close"])

    def test_three_attempt_bound_survives_controller_restart(self):
        self.backend.failure = "fence"
        for _ in range(3):
            with self.assertRaises(TransitionBlocked): Controller(self.path, self.backend).advance()
        actions = list(self.backend.actions)
        with self.assertRaisesRegex(TransitionBlocked, "exhausted"):
            Controller(self.path, self.backend).advance()
        self.assertEqual(self.backend.actions, actions)
        self.assertEqual(Journal(self.path).read()["attempts"], 3)

    def test_journal_corruption_and_failed_persistence_deny_actions(self):
        self.path.write_text("corrupt")
        with self.assertRaises(TransitionBlocked): self.control.advance()
        self.assertEqual(self.backend.actions, [])
        with patch.object(Journal, "save", side_effect=TransitionBlocked("not_durable")):
            with self.assertRaises(TransitionBlocked): self.control.advance()

    def test_legacy_guard_excludes_all_actions_and_duplicate_controller(self):
        self.backend.failure = "legacy"
        with self.assertRaises(TransitionBlocked): self.control.advance()
        self.assertEqual(self.backend.actions, [])
        if sys.platform != "win32":
            with exclusive(self.path.with_suffix(".lock")):
                with self.assertRaises(TransitionBlocked): self.control.advance()

    def test_native_adapter_cannot_be_armed_on_mac_or_linux(self):
        with patch("ha_control.windows.sys.platform", "darwin"):
            with self.assertRaisesRegex(TransitionBlocked, "native_windows_required"):
                WindowsFence("unused", "unused")

    def test_every_legacy_start_mutator_checks_durable_ownership(self):
        source = (ROOT / "scripts/windows-bootstrap.ps1").read_text(encoding="utf-8-sig")
        for function in ("Invoke-WslCommand", "Start-CloudflareTunnel", "Start-PersonalServerStack", "Invoke-TargetedRecovery", "Request-EmergencyReboot", "Install-ScheduledTask"):
            start = source.index("function " + function)
            end = source.index("\nfunction ", start + 1) if "\nfunction " in source[start + 1:] else len(source)
            self.assertIn("Assert-HaLegacyAllowed", source[start:end])
        self.assertIn('"ha-controller-owner.json"', source)

    def test_windows_fence_has_exact_inventory_engine_and_legacy_barriers(self):
        source = (ROOT / "infra/k8s/ha_control/windows-fence.ps1").read_text()
        for text in ("Sort-Object -Unique", "Compare-Object $expected $observed", "unix:///var/run/docker.sock", "owned_engine_mismatch", "ha-controller-owner.json", "bootstrap_sha256", "legacy_process_running", "shutdown_state_unverified", '"supervisor.lock", "daemon.lock", "recovery.lock"', "Assert-PrivateDirectory", "PropagationFlags]::InheritOnly", "ChangePermissions", "TakeOwnership"):
            self.assertIn(text, source)
        self.assertNotIn("Restart-Computer", source)
        self.assertNotIn("wsl --shutdown", source)
