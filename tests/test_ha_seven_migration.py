"""Frozen-source boundaries and failure receipts; no host/production access."""
import copy
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'infra/k8s'))
from ha_runtime import migrate,seven_migration as M
from ha_control.legacy_cutover import LegacyCutover, TransitionBlocked
from ha_control import frozen_ingress, frozen_transport, native_frozen_capture, native_guest_restore
from ha_control import native_guest_activation, native_guest_live
from ha_control import native_guest_ssh, native_guest_worker, native_stdio_bridge


def encoded(value):return json.dumps(value,sort_keys=True).encode()
def sha(raw):return hashlib.sha256(raw).hexdigest()


class TransitionTargetBridgeTests(unittest.TestCase):
    def test_exact_route_address_and_authenticated_guest_required(self):
        bridge = native_stdio_bridge
        binding = bridge.Binding(*("abcde"[i]*64 for i in range(5)))
        request = bridge.Request(binding, "B", bridge.GUEST_TRANSITION_TARGET, 1, "f"*64, {"installation_id": "9"*64})
        generation = native_guest_activation.ServingGeneration("B", 2, 2, 2)
        dispatcher = object.__new__(native_guest_ssh.GuestSshDispatcher)
        dispatcher.guests = {"B": SimpleNamespace(host="192.168.20.2")}
        with patch.object(dispatcher, "_dispatch", return_value=bridge.Receipt("PASS", "8"*64)) as dispatch:
            with self.assertRaisesRegex(bridge.BridgeRejected, "target_unbound"):
                dispatcher.verify_transition_target(request, generation, "192.168.20.8", 45)
            with self.assertRaises(bridge.BridgeRejected):
                dispatcher(request, 45)
            dispatch.assert_not_called()
            self.assertIs(dispatcher.verify_transition_target(request, generation, "192.168.20.2", 45), True)
            dispatch.assert_called_once_with(request, 45)

    def test_target_worker_reads_only_installation_and_current_boot(self):
        bridge = native_stdio_bridge
        boot = b"boot-identifier"
        binding = bridge.Binding("a"*64, "b"*64, "c"*64, "d"*64, sha(boot))
        request = bridge.Request(binding, "B", bridge.GUEST_TRANSITION_TARGET, 1, "f"*64, {"installation_id": "9"*64})
        payload = {"node": "B", "installation_id": "9"*64}
        with patch.object(native_guest_worker, "_read_json", return_value=(encoded(payload), payload)) as read, \
                patch.object(native_guest_restore, "BOOT_ID", SimpleNamespace(read_bytes=lambda: boot)), \
                patch.object(native_guest_worker.os, "geteuid", return_value=0), \
                patch.object(native_guest_worker, "mutation_lock") as lock:
            result = native_guest_worker.handle(request)
            self.assertEqual(result.status, "PASS")
            read.assert_called_once_with(native_guest_restore.INSTALLATION)
            payload["node"] = "A"
            self.assertEqual(native_guest_worker.handle(request).status, "UNKNOWN")
            lock.assert_not_called()


class InitialStandbyRetirementBridgeTests(unittest.TestCase):
    def test_guest_worker_binds_initial_retirement_to_b_and_exact_closed_receipt(self):
        wire = native_stdio_bridge
        verb = wire.GUEST_OBSERVE_INITIAL_STANDBY_RETIREMENT
        binding = wire.Binding(*(key * 64 for key in "abcde"))
        args = {"verify_closed_sha256": "f"*64}
        req = wire.Request(binding, "B", verb, 1, "1"*64, args)
        plan = {"node": "B", "attempt_id": binding.attempt_id}
        original = object.__new__(native_guest_worker.GuestWorker)
        original.node, original.request, original.args = req.node, req, req.args
        original.stage = Path("/prior")
        prepared = {"manifest_sha256": "2"*64, "runtime_receipt_sha256": "3"*64}
        original._proof = MagicMock(return_value=(args["verify_closed_sha256"], prepared))
        original._prepared_pins = MagicMock(return_value=("db", "pins", prepared.copy()))
        original.identity = MagicMock()
        original._assert_no_activation = MagicMock()
        original._record = MagicMock()

        def inspect(pins, db, stage, *, runtime_receipt_sha256, assert_no_activation):
            self.assertEqual((pins, db, stage, runtime_receipt_sha256), ("pins", "db", original.stage, "3"*64))
            assert_no_activation()
            return plan

        with patch.object(native_guest_worker, "GuestWorker", return_value=original) as create, \
                patch.object(native_guest_worker, "mutation_lock") as lock, \
                patch.object(native_guest_worker.runtime, "observe_initial_standby_retirement", side_effect=inspect) as observe:
            with self.assertRaisesRegex(wire.BridgeRejected, "bridge_guest_only_request"):
                wire._Chain("cutover").check(verb, "B", args)
            self.assertEqual(native_guest_worker.handle(replace(req, node="A")).status, "UNKNOWN")
            lock.assert_not_called()
            create.assert_not_called()
            self.assertEqual(native_guest_worker.handle(req), wire.Receipt("PASS", wire._digest(wire._bytes(plan))))
            lock.assert_called_once_with(wait=0)
            original._proof.assert_called_once_with("verify_closed", args["verify_closed_sha256"])
            original._prepared_pins.assert_called_once_with(prepared["manifest_sha256"])
            original.identity.assert_called_once()
            original._assert_no_activation.assert_called_once()
            original._record.assert_not_called()
            original._prepared_pins.return_value[2]["runtime_receipt_sha256"] = "4"*64
            self.assertEqual(native_guest_worker.handle(req).status, "UNKNOWN")
            observe.assert_called_once()


class ReplacementStandbyBridgeTests(unittest.TestCase):
    def test_generation_handoff_rejects_lower_wal_before_dispatch(self):
        from ha_control.native_guest_activation import ServingGeneration

        bridge = native_stdio_bridge
        binding = bridge.Binding(*(character*64 for character in "abcde"))
        generation = ServingGeneration("B", 2, 2, 3)
        instance = "f"*64
        content = {"binding": binding.fields(), "generation": {"primary": "B", "epoch": 2, "timeline": 2, "revision": 3},
                   "activation": {"windows_service_instance_sha256": instance}, "replay_floor": 10}
        pin = bridge._digest(bridge._bytes(content))
        request = bridge.Request(binding, "B", bridge.GUEST_PUBLISH_GENERATION, 1, "7"*64, {
            "handoff": content, "generation_handoff_sha256": pin, "effects_plan_sha256": "8"*64,
        })
        dispatcher = object.__new__(native_guest_ssh.GuestSshDispatcher)
        dispatcher.binding = binding
        with patch.object(dispatcher, "_dispatch", return_value=bridge.Receipt("PASS", pin)) as dispatch:
            with self.assertRaisesRegex(bridge.BridgeRejected, "handoff_unbound"):
                dispatcher.publish_generation_handoff(request, generation, instance, 11, 100)
            dispatch.assert_not_called()
            self.assertEqual(dispatcher.publish_generation_handoff(request, generation, instance, 10, 100), pin)
        dispatch.assert_called_once_with(request, 100)

    def test_confirmation_requires_matching_windows_replication_receipt_after_guest(self):
        bridge = native_stdio_bridge
        binding = bridge.Binding(*("abcdef"[i]*64 for i in range(5)))
        stage = "3"*64
        request = bridge.Request(binding, "A", bridge.GUEST_OBSERVE_REPLACEMENT, 1, "1"*64, {
            "bootstrap_manifest_sha256": "2"*64, "rejoin_stage_sha256": stage,
        })
        dispatcher = object.__new__(native_guest_ssh.GuestSshDispatcher)
        receipt = bridge.Receipt("PASS", "5"*64)
        proof = {"status": "PASS", "phase": "rejoining", "authority_opened": False,
                 "standby_rejoined": True, "rejoin_stage_sha256": stage, "revision": 5}
        guard = MagicMock(return_value=stage)
        with patch.object(dispatcher, "observe_replacement_standby", return_value=receipt) as observe:
            for result in (proof | {"rejoin_stage_sha256": "4"*64}, proof | {"standby_rejoined": False}):
                with self.assertRaisesRegex(bridge.BridgeRejected, "guest_ssh_replacement_replication_unconfirmed"):
                    dispatcher.confirm_replacement_rejoin(request, 100, assert_rejoin_current=guard,
                                                          check_synchronized=lambda _: result)
            self.assertEqual(dispatcher.confirm_replacement_rejoin(
                request, 100, assert_rejoin_current=guard, check_synchronized=lambda _: proof,
            ), (receipt, proof["revision"]))
            observe.assert_called_with(request, 100, assert_rejoin_current=guard)

    def test_worker_runs_only_boot_and_stage_bound_standby_package(self):
        bridge, worker = native_stdio_bridge, native_guest_worker
        boot, code = b"replacement boot", b"pinned bootstrap program"
        binding = bridge.Binding("a"*64, "b"*64, "c"*64, sha(boot), "d"*64)
        installed = {"node": "A", "source_commit": "1"*40, "installation_id": "2"*64}
        lock = installed | {"primary_node": "B", "source_node": "B", "plan_sha256": binding.plan_sha256}
        manifest = encoded({"files": {"bootstrap.py": sha(code), "lock.json": sha(encoded(lock))}})
        request = bridge.Request(binding, "A", bridge.GUEST_START_REPLACEMENT, 1, "e"*64, {
            "bootstrap_manifest_sha256": sha(manifest), "rejoin_stage_sha256": "f"*64,
        })
        grant = {"schema_version": 1, "binding": binding.fields(), "node": "A",
                 "installation_id": installed["installation_id"], **request.args}
        reply = encoded({"status": "PASS", "standby_start_requested": True, "writer_authority_issued": False})
        boot_file = MagicMock()
        boot_file.read_bytes.return_value = boot
        with patch.object(worker.os, "geteuid", return_value=0), patch.object(worker.restore, "BOOT_ID", boot_file), \
                patch.object(worker.runtime, "_directory"), patch.object(worker, "_read_json") as read, \
                patch.object(worker.runtime, "_read") as read_file, patch.object(worker.subprocess, "run") as run:
            read.side_effect = [(b"grant", grant | {"rejoin_stage_sha256": "0"*64})]
            with self.assertRaisesRegex(TransitionBlocked, "guest_replacement_start_not_bound"):
                worker.replacement_standby(request)
            run.assert_not_called()
            read.side_effect = [(b"grant", grant), (b"installation", installed)]
            read_file.side_effect = [manifest, encoded(lock), code]
            run.return_value = SimpleNamespace(returncode=0, stdout=reply)
            self.assertEqual(worker.replacement_standby(request).status, "PASS")
            self.assertEqual(run.call_args.args[0][0], "/usr/bin/python3")
            self.assertEqual(run.call_args.args[0][-1], sha(boot))
            self.assertEqual(run.call_args.kwargs["input"], code)
            read.side_effect = [(b"grant", grant), (b"installation", installed)]
            read_file.side_effect = [manifest, encoded(lock), code]
            run.return_value.stdout = encoded({
                "status": "PASS", "standby_container_sha256": "1"*64, "writer_authority_issued": False,
            })
            result = worker.replacement_standby(replace(request, verb=bridge.GUEST_OBSERVE_REPLACEMENT))
            self.assertEqual(result.status, "PASS")
            self.assertIn("observe-replacement-standby", run.call_args.args[0])

    def test_dispatch_requires_current_windows_and_db_stage_around_start(self):
        bridge = native_stdio_bridge
        binding = bridge.Binding(*("abcdef"[i]*64 for i in range(5)))
        request = bridge.Request(binding, "A", bridge.GUEST_START_REPLACEMENT, 1, "1"*64, {
            "bootstrap_manifest_sha256": "2"*64, "rejoin_stage_sha256": "3"*64,
        })
        dispatcher = object.__new__(native_guest_ssh.GuestSshDispatcher)
        dispatcher.binding = binding
        guard = MagicMock(side_effect=["3"*64, "4"*64])
        with patch.object(dispatcher, "_dispatch", return_value=bridge.Receipt("PASS", "5"*64)) as dispatch:
            with self.assertRaisesRegex(bridge.BridgeRejected, "guest_ssh_replacement_guard_required"):
                dispatcher(request, 100)
            dispatch.assert_not_called()
            with self.assertRaisesRegex(bridge.BridgeRejected, "guest_ssh_replacement_start_unconfirmed"):
                dispatcher.start_replacement_standby(request, 100, assert_rejoin_current=guard)
            dispatch.assert_called_once_with(request, 100)
            self.assertEqual(guard.call_count, 2)


class NativeGuestActivationTests(unittest.TestCase):
    def test_b_runtime_preflight_rejects_a_or_unbound_postgres_address(self):
        runtime = native_guest_worker.runtime
        boot = b"boot"
        install = {"installation_id": "a"*64, "node": "B"}
        spec = encoded(install | {"source_commit": "b"*40, "guest_ip": "192.168.1.10",
                                 "pg_a": "192.168.1.10", "pg_b": "192.168.1.11"})
        pins = SimpleNamespace(boot_sha256=sha(boot), installation_id=install["installation_id"],
                               node="B", spec_sha256=sha(spec), release={"source_commit": "b"*40})
        with patch.object(runtime, "_directory"), \
                patch.object(runtime, "BOOT_ID", MagicMock(read_bytes=MagicMock(return_value=boot))), \
                patch.object(runtime, "_read", side_effect=[encoded(install), spec]), \
                patch.object(runtime, "_renderer") as renderer:
            with self.assertRaisesRegex(TransitionBlocked, "runtime_spec_scope_mismatch"):
                runtime._preflight(pins, Path("/fixture"))
            renderer.assert_not_called()

    def test_generation_runtime_prepare_requires_closed_primary_and_current_wal(self):
        runtime = native_guest_worker.runtime
        generation = native_guest_activation.ServingGeneration("B", 2, 3, 4)
        node = generation.primary
        boot = b"fixture boot"
        shared = {"boot_sha256": sha(boot), "installation_id": "1"*64, "plan_sha256": "2"*64,
                  "manifest_sha256": "3"*64, "system_id": "12345", "cluster": "test_ha",
                  "postgres_id": "4"*64, "retained_postgres_id": None, "attempt_id": "5"*64}
        pins = SimpleNamespace(**shared, validate=lambda: None, release={"source_commit": "a"*40},
                               node=node, primary_node=node, generation=2,
                               frozen_transfer_sha256="6"*64, restore_receipt_sha256="7"*64)
        postgres = SimpleNamespace(**shared, validate=lambda: None, source_commit="a"*40)
        settled = ["ha_B", "12345", False, "test_ha", 2, "B", "A", 3, 3, False, False, True, "0/80"]
        installed = encoded({"node": node, "installation_id": pins.installation_id, "source_commit": postgres.source_commit})
        proof = MagicMock(return_value="8"*64)
        with patch.object(runtime.os, "geteuid", return_value=0), \
                patch.object(runtime, "BOOT_ID", MagicMock(read_bytes=MagicMock(return_value=boot))), \
                patch.object(runtime, "_read", return_value=installed), \
                patch.object(runtime.os.path, "lexists", return_value=False), \
                patch.object(runtime, "_pg", return_value=settled) as sql, \
                patch.object(runtime, "prepare_runtime", side_effect=lambda *_a, assert_guard: assert_guard()):
            call = (pins, postgres, Path("/fixture"), generation, 0x80)
            verified = runtime.prepare_generation_runtime(*call, assert_windows_rejoin=proof)
            self.assertTrue(verified["target_replay_verified"])
            self.assertEqual(proof.call_args.args[1], "prepare_generation:1")
            for index, value in ((8, 4), (9, True), (11, False), (12, "0/7F")):
                invalid = settled.copy()
                invalid[index] = value
                sql.return_value = invalid
                with self.assertRaisesRegex(TransitionBlocked, "generation_not_closed"):
                    runtime.prepare_generation_runtime(*call, assert_windows_rejoin=proof)

    def test_generation_handoff_builder_matches_worker_and_frame(self):
        worker, bridge = native_guest_worker, native_stdio_bridge
        binding = bridge.Binding(*(character*64 for character in "abcde"))
        shared = {"boot_sha256": binding.boot("B"), "installation_id": "1"*64, "plan_sha256": "2"*64,
                  "manifest_sha256": "3"*64, "system_id": "1234567890123456789", "cluster": "test_ha",
                  "postgres_id": "4"*64}
        pins = worker.runtime.RuntimePins(
            **shared, node="B", primary_node="B", generation=2, attempt_id=binding.attempt_id,
            release={"source_commit": "a"*40, "release_id": "5"*64, "source_files_sha256": "6"*64},
            protected_inventory_sha256="7"*64, engine_id_sha256="8"*64, renderer_sha256="9"*64,
            spec_sha256="a"*64, inputs_sha256="b"*64, frozen_transfer_sha256="c"*64,
            restore_receipt_sha256="d"*64, broker_instance="e"*64, retained_postgres_id="f"*64,
        )
        pg = worker.restore.RestorePins(
            **shared, inventory_sha256="5"*64, uid=70, gid=70, utility_digest="sha256:"+"6"*64,
            initializer_sha256="7"*64, source_commit="a"*40, config_sha256="8"*64, peer_ipv4="192.168.1.10",
            retained_postgres_id="f"*64, attempt_id=binding.attempt_id,
        )
        activation = native_guest_activation.ActivationPins(*(character*64 for character in "012345"))
        serving = native_guest_activation.ServingGeneration("B", 2, 2, 3)
        request = native_guest_activation.generation_handoff_request(
            binding, 4, serving, "9"*64, 2**64-1, pins=pins, postgres=pg, activation=activation,
        )
        handoff = request.args["handoff"]
        raw = bridge._bytes(handoff)
        envelope = worker._scope(request) | {"args": request.args}
        frame = bridge._bytes(envelope)
        self.assertGreater(len(frame), bridge.FRAME_BYTES)
        self.assertLessEqual(len(frame), bridge.GENERATION_HANDOFF_FRAME_BYTES)
        self.assertEqual(worker.read_request(io.BytesIO(frame)), request)
        with self.assertRaisesRegex(TransitionBlocked, "guest_worker_request_size"):
            worker.read_request(io.BytesIO(bridge._bytes(envelope | {"verb": bridge.GUEST_ACTIVATE_GENERATION})))
        self.assertEqual(worker._checked_generation_handoff(request, raw, handoff),
                         (pins, pg, replace(activation, windows_service_instance_sha256="9"*64), serving, 2**64-1))

        prep = replace(request, verb=bridge.GUEST_PREPARE_GENERATION, args={
            "runtime": handoff["runtime"], "postgres": handoff["postgres"], "generation": handoff["generation"],
            "source_fence_wal": "f"*16, "rejoin_stage_sha256": "a"*64,
        })
        prep_frame = bridge._bytes(worker._scope(prep) | {"args": prep.args})
        self.assertLessEqual(len(prep_frame), bridge.FRAME_BYTES)
        self.assertEqual(worker.read_request(io.BytesIO(prep_frame)), prep)

    def test_generation_guest_request_binds_b_and_requires_wal_at_each_boundary(self):
        worker, bridge = native_guest_worker, native_stdio_bridge
        boot, installation = b"fresh boot", "4"*64
        binding = bridge.Binding("a"*64, "b"*64, "c"*64, "d"*64, sha(boot))
        fields = dict(boot_sha256=sha(boot), installation_id=installation, plan_sha256="5"*64,
                      manifest_sha256="6"*64, system_id="123", cluster="fixture", postgres_id="7"*64)
        release = {"source_commit": "8"*40}
        pins = SimpleNamespace(**fields, node="B", primary_node="B", generation=2, attempt_id=binding.attempt_id,
                               release=release, validate=lambda: None)
        pg = SimpleNamespace(**fields, source_commit=release["source_commit"], validate=lambda: None)
        generation = native_guest_activation.ServingGeneration("B", 2, 2, 3)
        activation_pins = native_guest_activation.ActivationPins(*("012345"[i]*64 for i in range(6)))
        handoff = {"schema_version": 1, "binding": binding.fields(), "generation": generation.__dict__,
                   "runtime": {}, "postgres": {}, "activation": activation_pins.__dict__, "replay_floor": 0x20}
        raw = encoded(handoff)
        request = bridge.Request(binding, "B", bridge.GUEST_ACTIVATE_GENERATION, 1, "9"*64,
                                 {"generation_handoff_sha256": sha(raw), "effects_plan_sha256": pins.plan_sha256})
        installed = {"installation_id": installation, "node": "B", "source_commit": pg.source_commit}
        result = {"status": "STARTED", "automatic_retry_permitted": False}
        def record(path):
            if path == worker.restore.INSTALLATION:
                return b"installed", installed
            if path.name == "runtime-activation.json":
                return encoded(result), result
            return raw, handoff
        windows = MagicMock(return_value="f"*64)
        sampled = datetime.now(timezone.utc)
        expiry = sampled + timedelta(seconds=4)
        boot_file = MagicMock()
        boot_file.read_bytes.return_value = boot
        boundary_receipts = []
        def activate(runtime_pins, input_pins, stage, serving, *, assert_lease):
            self.assertEqual((runtime_pins, input_pins, serving), (pins, activation_pins, generation))
            boundary_receipts.append(assert_lease("e"*64, "start:portal-web"))
            return result
        with patch.object(worker.os, "geteuid", return_value=0), patch.object(worker.runtime, "_directory"), \
                patch.object(worker, "_read_json", side_effect=record), patch.object(worker.runtime, "RuntimePins", return_value=pins), \
                patch.object(worker.restore, "RestorePins", return_value=pg), patch.object(worker.restore, "BOOT_ID", boot_file), \
                patch.object(worker.runtime, "_runtime_identity", return_value={}), patch.object(worker.live, "_lease") as lease, \
                patch.object(worker.activation, "activate_prepared_generation", side_effect=activate):
            with self.assertRaisesRegex(TransitionBlocked, "guest_generation_handoff_unbound"):
                worker.activate_generation(replace(request, args=request.args | {"effects_plan_sha256": "0"*64}), windows)
            windows.assert_not_called()
            lease.return_value = ({}, sampled, expiry, 0x1F)
            with self.assertRaisesRegex(TransitionBlocked, "guest_generation_replay_unconfirmed"):
                worker.activate_generation(request, windows)
            self.assertEqual(boundary_receipts, [])
            lease.return_value = ({}, sampled, expiry, 0x20)
            self.assertEqual(worker.activate_generation(request, windows).status, "PASS")
            self.assertEqual((boundary_receipts[0]["primary"], boundary_receipts[0]["epoch"]), ("B", 2))
            self.assertTrue(boundary_receipts[0]["previous_primary_fenced"])

    def test_start_accepts_docker_oom_default_without_accepting_contract_drift(self):
        activation = native_guest_activation
        runtime = activation.runtime
        role = "ha-broker"
        identifier = "c" * 64
        image = "sha256:" + "d" * 64
        attempt = "e" * 64
        pins = SimpleNamespace(attempt_id=attempt, protected_inventory_sha256=runtime.inventory_sha256({}))
        containers = {role: {"container_id": identifier, "image_id": image}}
        item = {
            "Image": image, "Name": "/broker", "RestartCount": 0, "Mounts": [],
            "Config": {"Labels": {runtime.ATTEMPT_LABEL: attempt, "com.docker.compose.service": role}},
            "HostConfig": {"NetworkMode": "host", "OomKillDisable": False, "ReadonlyRootfs": True},
            "NetworkSettings": {"Networks": {"host": {}}},
            "State": {"Status": "created", "Running": False, "Paused": False, "Restarting": False, "Pid": 0},
        }
        with patch.object(runtime, "_inventory", return_value={identifier: item}):
            baseline = activation._state(pins, containers, set())
            item["State"].update(Status="running", Running=True, Pid=42)
            item["HostConfig"]["OomKillDisable"] = None
            self.assertEqual(activation._state(pins, containers, {role}, baseline), baseline)
            self.assertIsNone(item["HostConfig"]["OomKillDisable"])

            for drift in ({"OomKillDisable": True}, {"OomKillDisable": 0}, {"ReadonlyRootfs": False}):
                with self.subTest(drift=drift), patch.dict(item["HostConfig"], drift):
                    with self.assertRaisesRegex(TransitionBlocked, "guest_activation_container_contract_changed"):
                        activation._state(pins, containers, {role}, baseline)

    def test_transport_window_does_not_extend_database_freshness(self):
        activation = native_guest_activation
        pins = activation.ActivationPins(*("abcdef"[i] * 64 for i in range(6)))
        now = datetime.now(timezone.utc)

        def proof(challenge, boundary):
            sampled = now - timedelta(seconds=age)
            return {
                **activation.asdict(pins), "challenge": challenge, "boundary": boundary,
                "coordinator_intent_durable": True, "initial_postcommit_verified": True,
                "legacy_writers_closed": True, "standby_runtime_closed": True,
                "runtime_mutations_excluded": True, "epoch": 1, "timeline": 1, "revision": 2,
                "primary": "A", "partner": "B", "writes_open": True, "lease_sha256": "f" * 64,
                "observed_at": sampled.isoformat().replace("+00:00", "Z"),
                "expires_at": (sampled + timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
            }

        for age, elapsed, failure in (
            (0.1, 6.2, None),
            (2.1, 6.2, "guest_activation_observation_age"),
            (5.1, 6.2, "guest_activation_expired_during_check"),
            (0.1, 7.1, "guest_activation_boundary_deadline"),
        ):
            with self.subTest(age=age, elapsed=elapsed), \
                    patch.object(activation.runtime, "_runtime_identity", return_value={}), \
                    patch.object(activation.time, "monotonic", side_effect=(0, elapsed)):
                if failure is None:
                    self.assertEqual(activation._check_lease(None, pins, proof, "activation_intent"), "f" * 64)
                else:
                    with self.assertRaisesRegex(TransitionBlocked, failure):
                        activation._check_lease(None, pins, proof, "activation_intent")

    def test_failed_boundary_preserves_only_bounded_diagnostics(self):
        activation = native_guest_activation
        pins = activation.ActivationPins(*("abcdef"[i] * 64 for i in range(6)))
        for reason, expected in (
            ("guest_live_pg_lease_expired", "guest_live_pg_lease_expired"),
            ("private driver text", "guest_activation_sample_unconfirmed"),
        ):
            with self.subTest(reason=reason), patch.object(activation.os, "geteuid", return_value=0), \
                    patch.object(activation.runtime, "_directory"), \
                    patch.object(activation.runtime, "_inspect_prepared",
                                 return_value=pins.primary_runtime_receipt_sha256), \
                    patch.object(activation.runtime, "_read", return_value=b"prepared"), \
                    patch.object(activation.runtime, "_digest", return_value=pins.primary_runtime_receipt_sha256), \
                    patch.object(activation.runtime, "_runtime_identity", return_value={}), \
                    patch.object(activation.runtime, "_write") as write, \
                    patch.object(activation.json, "loads", return_value={
                        "containers": dict.fromkeys(activation.SERVICES | {"ha-broker"}),
                    }), patch.object(activation, "_authorities"), patch.object(activation, "_state"):
                runtime_pins = SimpleNamespace(node="A", primary_node="A", generation=1,
                                               attempt_id="1" * 64, validate=lambda: None)
                result = activation.activate_prepared_a(
                    runtime_pins, pins, Path("/unused/attempt"),
                    assert_lease=MagicMock(side_effect=TransitionBlocked(reason)),
                )
            self.assertEqual(result, {"status": "UNKNOWN", "automatic_retry_permitted": False})
            path, raw = write.call_args.args
            self.assertEqual(path.name, "runtime-activation-failure.json")
            self.assertEqual(json.loads(raw)["reason"], expected)
            self.assertEqual(json.loads(raw)["boundary"], "activation_intent")

    def test_failover_activation_requires_new_generation_and_live_old_writer_fence(self):
        activation = native_guest_activation
        pins = activation.ActivationPins(*(character * 64 for character in "abcdef"))
        generation = activation.ServingGeneration("B", 2, 3, 7)
        now = datetime.now(timezone.utc)
        facts = dict(transition_postcommit_verified=True, previous_primary_fenced=True, epoch=2, timeline=3,
                     revision=7, primary="B", partner="A", coordinator_intent_durable=True, writes_open=True,
                     legacy_writers_closed=True, standby_runtime_closed=True, runtime_mutations_excluded=True)

        def verify(overrides):
            def proof(challenge, boundary):
                return {**activation.asdict(pins), **facts, **overrides, "challenge": challenge, "boundary": boundary,
                        "lease_sha256": "f" * 64, "observed_at": now.isoformat().replace("+00:00", "Z"),
                        "expires_at": (now + timedelta(seconds=5)).isoformat().replace("+00:00", "Z")}
            return activation._check_lease(None, pins, proof, "start:ha-broker", generation)

        with patch.object(activation.runtime, "_runtime_identity", return_value={}):
            self.assertEqual(verify({}), "f" * 64)
            for wrong in ({"previous_primary_fenced": False}, {"primary": "A"}, {"epoch": 1},
                          {"timeline": 2}, {"revision": 6}, {"transition_postcommit_verified": False}):
                with self.subTest(wrong=wrong), self.assertRaisesRegex(
                        TransitionBlocked, "guest_activation_lease_unconfirmed"):
                    verify(wrong)
        with patch.object(activation.os, "geteuid", return_value=0), patch.object(activation, "_activate") as start:
            with self.assertRaisesRegex(TransitionBlocked, "guest_activation_generation_scope"):
                activation.activate_prepared_generation(SimpleNamespace(node="A"), pins, Path("/unused"),
                                                        generation, assert_lease=MagicMock())
            start.assert_not_called()

    def test_failover_preparation_guard_requires_matching_generation_and_closed_old_writer(self):
        runtime = native_guest_activation.runtime
        common = dict(node="B", installation_id="a" * 64, attempt_id="b" * 64, plan_sha256="c" * 64,
                      manifest_sha256="d" * 64, frozen_transfer_sha256="e" * 64, restore_receipt_sha256="f" * 64)
        previous = dict(common, legacy_writers_closed=True, target_writes_closed=True, frozen_transfer_closed=True)
        runtime._guard(SimpleNamespace(**common, generation=1, primary_node="A"), lambda: previous)
        pins = SimpleNamespace(**common, generation=2, primary_node="B")
        current = dict(previous, generation=2, primary_node="B", previous_primary_fenced=True,
                       target_replay_verified=True)
        runtime._guard(pins, lambda: current)
        for stale in (previous, dict(current, generation=1), dict(current, previous_primary_fenced=False),
                      dict(current, target_replay_verified=False)):
            with self.assertRaisesRegex(TransitionBlocked, "guest_runtime_live_guard_rejected"):
                runtime._guard(pins, lambda: stale)

    def test_readiness_blocks_next_application_start(self):
        activation = native_guest_activation
        runtime = activation.runtime
        pins = activation.ActivationPins(*("abcdef"[i] * 64 for i in range(6)))
        runtime_pins = SimpleNamespace(node="A", primary_node="A", generation=1,
                                       attempt_id="1" * 64, validate=lambda: None,
                                       spec_sha256=pins.primary_runtime_receipt_sha256)
        containers = {role: {"container_id": role} for role in activation.SERVICES | {"ha-broker"}}
        spec = {"node": "A", "pg_a": "192.0.2.10", "guest_ip": "192.0.2.10"}
        events = []
        files = {}

        def start(*args, **kwargs):
            events.append(args)

        def ready(_context, _leaf, _address, role, *, expected_node):
            self.assertEqual(expected_node, "A")
            events.append(("ready", role))
            if role == "car-care-worker":
                raise TransitionBlocked("guest_activation_app_unready")

        with patch.object(activation.os, "geteuid", return_value=0), \
                patch.object(runtime, "_directory"), \
                patch.object(runtime, "_read", side_effect=lambda path, *_: files.get(path, b"pinned")), \
                patch.object(runtime, "_digest", return_value=pins.primary_runtime_receipt_sha256), \
                patch.object(runtime, "_runtime_identity", return_value={}), \
                patch.object(runtime, "_inspect_prepared", return_value=pins.primary_runtime_receipt_sha256), \
                patch.object(runtime, "_write", side_effect=lambda path, raw: files.__setitem__(path, raw)) as write, \
                patch.object(runtime, "_docker", side_effect=start), \
                patch.object(activation.json, "loads", side_effect=({"containers": containers}, spec)), \
                patch.object(activation, "_authorities", return_value={}), patch.object(activation, "_state"), \
                patch.object(activation, "_check_lease", return_value="f" * 64), \
                patch.object(activation, "_tls", return_value=(None, None)), \
                patch.object(activation, "_wait_for_app", side_effect=ready):
            result = activation.activate_prepared_a(
                runtime_pins, pins, Path("/unused/attempt"), assert_lease=MagicMock(),
            )
        self.assertEqual(result, {"status": "UNKNOWN", "automatic_retry_permitted": False})
        self.assertEqual(events, [("start", "ha-broker"), ("start", "book-memo"), ("ready", "book-memo"),
                                  ("start", "car-care-worker"), ("ready", "car-care-worker")])
        path, raw = write.call_args.args
        self.assertEqual(path.name, "runtime-activation-failure.json")
        self.assertIn(b'"ready:car-care-worker"', raw)

    def test_startup_readiness_retries_unready_but_rejects_late_success(self):
        activation = native_guest_activation
        with patch.object(activation.time, "monotonic", side_effect=(0, 0, 1, 2, 3)), \
                patch.object(activation.time, "sleep") as pause, \
                patch.object(activation, "_probe",
                             side_effect=(TransitionBlocked("guest_live_route_unready"), None)) as probe:
            activation._wait_for_app(None, None, "192.0.2.10", "portal-web")
        self.assertEqual(probe.call_count, 2)
        self.assertEqual([entry.args for entry in probe.call_args_list],
                         [(None, None, "192.0.2.10", "portal-web", activation.APP_READY_SECONDS)] * 2)
        pause.assert_called_once_with(0.7)

        with patch.object(activation.time, "monotonic", side_effect=(0, 24, 25)), \
                patch.object(activation, "_probe", return_value=None), \
                self.assertRaisesRegex(TransitionBlocked, "guest_activation_app_unready"):
            activation._wait_for_app(None, None, "192.0.2.10", "portal-web")


class NativeGuestLiveTests(unittest.TestCase):
    def test_portal_readiness_finishes_before_other_ingress_requests(self):
        live = native_guest_live
        pin = "a" * 64
        fields = dict(boot_sha256=pin, installation_id=pin, system_id="1", plan_sha256=pin,
                      manifest_sha256=pin, cluster="test", validate=lambda: None)
        pins = SimpleNamespace(**fields, node="A", primary_node="A", generation=1, attempt_id=pin,
                               release={"source_commit": pin})
        postgres = SimpleNamespace(**fields, source_commit=pin)
        portal_complete = False
        probed = []

        def probe(_context, _leaf, _address, service, _deadline):
            nonlocal portal_complete
            probed.append(service)
            if service == "portal-web":
                self.assertEqual(probed, [service])
                portal_complete = True
            else:
                self.assertTrue(portal_complete)
            return pin

        with patch.object(live.os, "geteuid", return_value=0), \
                patch.object(live.runtime, "_directory"), \
                patch.object(live.runtime, "_runtime_identity", return_value={}), \
                patch.object(live, "_receipts", return_value=(None, {}, None)), \
                patch.object(live, "_layout", return_value=("192.0.2.10", {}, {})), \
                patch.object(live, "_running", return_value={}), \
                patch.object(live, "_tls", return_value=(None, pin)), \
                patch.object(live, "_installed_authorities"), \
                patch.object(live, "_lease", return_value=(pin, 0, 1, 0)) as lease, \
                patch.object(live, "_probe", side_effect=probe):
            live.observe_live_a(
                pins, SimpleNamespace(validate=lambda: None), postgres, Path("/unused/attempt"),
                activation_receipt_sha256=pin, authority_sha256=pin, windows_generation=1,
                windows_generation_sha256=pin, live_lease_sha256=pin, live_authority_sha256=pin,
            )
        self.assertCountEqual(probed, live.PATHS)
        self.assertEqual(lease.call_count, 2)

    def test_proxy_response_header_timeout_preserves_socket_error_contract(self):
        live = native_guest_live
        context = MagicMock()
        conn = context.wrap_socket.return_value.__enter__.return_value
        conn.getpeercert.return_value = b"cert"
        with patch.object(live.socket, "create_connection"), \
                patch.object(live.http.client, "HTTPResponse") as response:
            response.return_value.__enter__.return_value.begin.side_effect = TimeoutError
            with self.assertRaisesRegex(TimeoutError, "^guest_live_route_headers_timeout$"):
                live._probe(context, live.runtime._digest(b"cert"), "192.0.2.10", "portal-web",
                            live.time.monotonic() + live.LIVE_SECONDS)

    def test_new_primary_route_accepts_only_its_node_markers(self):
        live = native_guest_live
        context = MagicMock()
        conn = context.wrap_socket.return_value.__enter__.return_value
        conn.getpeercert.return_value = b"cert"
        with patch.object(live.socket, "create_connection"), patch.object(live.http.client, "HTTPResponse") as http:
            response = http.return_value.__enter__.return_value
            response.status = 200
            response.headers.get_content_type.return_value = "application/json"
            response.read.return_value = b'{"node_id":"B","service":"system-agent","status":"ok"}'
            response.headers.get_all.return_value = ["B"]
            args = (context, live.runtime._digest(b"cert"), "192.0.2.11", "system-agent",
                    live.time.monotonic() + live.LIVE_SECONDS)
            self.assertEqual(live._probe(*args, expected_node="B"), live.runtime._digest(response.read.return_value))
            with self.assertRaisesRegex(TransitionBlocked, "guest_live_route_wrong_node"):
                live._probe(*args, expected_node="A")
            response.headers.get_all.return_value = ["A"]
            with self.assertRaisesRegex(TransitionBlocked, "guest_live_route_wrong_node"):
                live._probe(*args, expected_node="B")

    def test_new_primary_requires_synchronous_peer_and_exact_epoch(self):
        live = native_guest_live
        generation = native_guest_activation.ServingGeneration("B", 2, 3, 7)
        pins = SimpleNamespace(node="B", system_id="123", cluster="fixture",
                               release={"source_commit": "a" * 40}, plan_sha256="b" * 64)
        postgres = SimpleNamespace(peer_ipv4="192.0.2.10")
        sampled = datetime.now(timezone.utc)
        row = {
            "identity": ["postgres", "ha", "ha_B", False, "123", "on", "remote_apply",
                         "FIRST1(ha_a)", "on", "on", 3],
            "authority": ["fixture", 2, 7, "B", "A", 3, ["B"], "B", "AS", True, True],
            "lease_clock": [sampled.isoformat(), (sampled + timedelta(seconds=5)).isoformat()],
            "binding": [["fixture", "123", "a" * 40, "b" * 64, live.runtime.config.BROKER_ROLE]],
            "peers": [["192.0.2.10", "0/20"]],
        }
        with patch.object(live.restore, "_pg", return_value=row):
            self.assertEqual(live._lease(pins, postgres, generation)[-1], 32)
            row["authority"][1] = 1
            with self.assertRaisesRegex(TransitionBlocked, "guest_live_pg_binding_unconfirmed"):
                live._lease(pins, postgres, generation)
            row["authority"][1] = 2
            row["peers"] = []
            with self.assertRaisesRegex(TransitionBlocked, "guest_live_pg_peer_unconfirmed"):
                live._lease(pins, postgres, generation)

    def test_live_generation_b_binds_all_routes_receipts_and_windows_epoch(self):
        live = native_guest_live
        pin = "a" * 64
        generation = native_guest_activation.ServingGeneration("B", 2, 2, 3)
        shared = dict(boot_sha256=pin, installation_id=pin, system_id="1", plan_sha256=pin,
                      manifest_sha256=pin, cluster="fixture", validate=lambda: None)
        pins = SimpleNamespace(**shared, node="B", primary_node="B", generation=2, release={"source_commit": pin})
        postgres = SimpleNamespace(**shared, source_commit=pin)
        args = (pins, SimpleNamespace(validate=lambda: None), postgres, Path("/unused"), generation)
        authority = dict(activation_receipt_sha256=pin, authority_sha256=pin, windows_generation=2,
                         windows_generation_sha256=pin, live_lease_sha256=pin, live_authority_sha256=pin)
        with patch.object(live.os, "geteuid", return_value=0), patch.object(live.runtime, "_directory"), \
                patch.object(live.runtime, "_runtime_identity", return_value={}), \
                patch.object(live, "_receipts", return_value=(None, {}, None)) as receipts, \
                patch.object(live, "_layout", return_value=("192.0.2.11", {}, {})), \
                patch.object(live, "_running", return_value={}), patch.object(live, "_tls", return_value=(None, pin)), \
                patch.object(live, "_installed_authorities"), \
                patch.object(live, "_lease", return_value=(pin, 0, 1, 0)) as lease, \
                patch.object(live, "_probe", return_value=pin) as probe:
            self.assertRegex(live.observe_live_generation(*args, **authority), r"^[0-9a-f]{64}$")
            self.assertEqual(receipts.call_args.args[-1], generation)
            self.assertEqual(lease.call_count, 2)
            self.assertCountEqual([call.args[3] for call in probe.call_args_list], live.PATHS)
            self.assertTrue(all(call.kwargs == {"expected_node": "B"} for call in probe.call_args_list))
            with self.assertRaisesRegex(TransitionBlocked, "guest_live_observation_unbound"):
                live.observe_live_generation(*args, **{**authority, "windows_generation": 1})
            self.assertEqual(lease.call_count, 2)

    def test_only_timeout_with_time_left_retries_same_authenticated_route_once(self):
        live = native_guest_live
        args = (object(), "a" * 64, "192.0.2.10", "book-memo", 20)
        timeout = live.RouteTimeout("guest_live_route_headers_timeout")
        with patch.object(live.time, "monotonic", return_value=5), \
                patch.object(live, "_probe", side_effect=(timeout, "b" * 64)) as probe:
            self.assertEqual(live._probe_route(*args), "b" * 64)
            self.assertEqual([entry.args for entry in probe.call_args_list], [args, args])
        with patch.object(live.time, "monotonic", return_value=18), \
                patch.object(live, "_probe", side_effect=timeout) as probe:
            with self.assertRaises(live.RouteTimeout):
                live._probe_route(*args)
            probe.assert_called_once_with(*args)
        with patch.object(live.time, "monotonic", return_value=5), \
                patch.object(live, "_probe", side_effect=timeout) as probe:
            with self.assertRaises(live.RouteTimeout):
                live._probe_route(*args)
            self.assertEqual(probe.call_count, 2)
        with patch.object(live, "_probe", side_effect=TransitionBlocked("guest_live_tls_peer_changed")) as probe:
            with self.assertRaisesRegex(TransitionBlocked, "guest_live_tls_peer_changed"):
                live._probe_route(*args)
            probe.assert_called_once_with(*args)


class NativeCaptureHeartbeatTests(unittest.TestCase):
    def test_capture_heartbeats_do_not_dispatch_or_consume_mutations(self):
        bridge = native_stdio_bridge
        binding = bridge.Binding(*("abcdef"[i] * 64 for i in range(5)))
        left, right = socket.socketpair()
        with tempfile.TemporaryDirectory(dir=Path("/tmp").resolve()) as journal, left, right:
            observed = threading.Event()
            dispatch = MagicMock(return_value=bridge.Receipt("PASS", "7" * 64))
            channel = bridge.Stdio(left.fileno(), left.fileno())
            server = bridge.Server(channel, binding, bridge.MutationJournal(journal), dispatch, seconds=4)
            original = channel.write

            def record(frame, deadline):
                original(frame, deadline)
                if frame["kind"] == "heartbeat_ack" and frame["heartbeat"] == 2:
                    observed.set()

            channel.write = record
            errors = []

            def serve():
                try:
                    server.serve()
                except Exception as error:
                    errors.append(error)

            thread = threading.Thread(target=serve)
            thread.start()
            try:
                client = bridge.Client(bridge.Stdio(right.fileno(), right.fileno()), binding, seconds=4)
                with patch.object(bridge, "HEARTBEAT_INTERVAL", 0.01), client.keepalive():
                    self.assertTrue(observed.wait(2))
                    dispatch.assert_not_called()
                self.assertEqual(client.sequence, 0)
                self.assertEqual(client.call("preflight", "A", target_system_id_sha256="8" * 64).status, "PASS")
                self.assertEqual(dispatch.call_count, 1)
                self.assertEqual(list(Path(journal).iterdir()), [])
            finally:
                right.close()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def test_late_heartbeat_reads_same_ack_without_replay(self):
        bridge = native_stdio_bridge
        binding = bridge.Binding(*("abcdef"[i] * 64 for i in range(5)))
        channel = MagicMock()
        received = threading.Event()
        heartbeat_deadlines = []

        def answer(deadline):
            request = channel.write.call_args.args[0]
            if request["kind"] == "open":
                return request | {"kind": "opened", "server_nonce": "7" * 64}
            heartbeat_deadlines.append(deadline)
            if len(heartbeat_deadlines) == 1:
                raise bridge.BridgeRejected("bridge_deadline_exceeded")
            received.set()
            return request | {"kind": "heartbeat_ack"}

        channel.read.side_effect = answer
        client = bridge.Client(channel, binding)
        with client.keepalive():
            self.assertTrue(received.wait(2))
        self.assertEqual(channel.write.call_count, 2)
        self.assertEqual(client.heartbeat, 1)
        self.assertEqual(client.sequence, 0)
        self.assertEqual(heartbeat_deadlines[1] - heartbeat_deadlines[0], bridge.HEARTBEAT_ACK_GRACE)

    def test_unbound_heartbeat_poisoned_before_next_guest_request(self):
        bridge = native_stdio_bridge
        binding = bridge.Binding(*("abcdef"[i] * 64 for i in range(5)))
        channel = MagicMock()
        queried = threading.Event()

        def answer(deadline):
            request = channel.write.call_args.args[0]
            if request["kind"] == "open":
                return request | {"kind": "opened", "server_nonce": "7" * 64}
            queried.set()
            return request | {"kind": "heartbeat_ack", "sequence": 17}

        channel.read.side_effect = answer
        client = bridge.Client(channel, binding)
        with self.assertRaisesRegex(bridge.BridgeRejected, "bridge_capture_transport_unconfirmed"):
            with client.keepalive():
                self.assertTrue(queried.wait(2))
        with self.assertRaisesRegex(bridge.BridgeRejected, "bridge_session_unavailable"):
            client.call("preflight", "A", target_system_id_sha256="8" * 64)
        self.assertEqual(channel.write.call_count, 2)


class NativeRestoreReadbackTests(unittest.TestCase):
    def setUp(self):
        self.bridge = native_stdio_bridge
        self.binding = self.bridge.Binding(*("abcdef"[i] * 64 for i in range(5)))
        self.args = dict(zip(self.bridge.RESTORE_ARGS, ("7" * 64, "8" * 64, "9" * 64)))
        self.request = self.bridge.Request(self.binding, "A", "restore", 6, "1" * 64, self.args)

    def test_existing_worker_input_remains_initial_generation_only(self):
        worker = native_guest_worker
        runtime = worker.runtime
        pin = "1" * 64
        leaf = dict.fromkeys(("engine_id_sha256", "renderer_sha256", "spec_sha256", "inputs_sha256",
                              "broker_instance"), pin)
        leaf["release"] = {"source_commit": "a" * 40, "release_id": pin, "source_files_sha256": pin}
        restore_fields = worker._names(worker.restore.RestorePins) - worker.RESTORE_DYNAMIC - {"retained_postgres_id"}
        payload = dict(schema_version=1, binding=self.binding.fields(), node="A", installation_id=pin,
                       restore=dict.fromkeys(restore_fields, pin), runtime=leaf)
        db = SimpleNamespace(plan_sha256=pin, system_id="123", cluster="fixture", postgres_id=pin,
                             retained_postgres_id=None, inventory_sha256=pin, source_commit="a" * 40)
        with patch.object(worker.os, "geteuid", return_value=0), patch.object(runtime, "_directory"), \
                patch.object(worker, "_read_json", return_value=(b"input", payload)), \
                patch.object(worker.GuestWorker, "identity"), patch.object(worker.GuestWorker, "restore_pins", return_value=db):
            installed = worker.GuestWorker(self.request)
            pins = installed.runtime_pins()
            self.assertEqual((pins.primary_node, pins.generation), ("A", 1))
            self.assertNotIn("generation", runtime._runtime_identity(pins))
            leaf.update(primary_node="B", generation=2)
            with self.assertRaisesRegex(TransitionBlocked, "guest_worker_installed_scope_mismatch"):
                worker.GuestWorker(self.request)

    def test_unconfirmed_ssh_exit_reads_receipt_once_without_restoring_again(self):
        bridge = self.bridge
        envelope = native_guest_worker._scope(self.request)
        private = envelope | {"verb": bridge.GUEST_RESTORE_READBACK}
        proof = "2" * 64
        answer = bridge._bytes(private | bridge.Receipt("PASS", proof).fields(private["verb"]))
        guest = object.__new__(native_guest_ssh.GuestSshDispatcher)
        guest.guests = {"A": SimpleNamespace(alias="guest")}
        guest.jump = SimpleNamespace(alias="jump")
        guest.known_hosts = {"A": Path("guest-key"), "jump": Path("jump-key")}
        guest.command = "fixture"
        guest.run = MagicMock(side_effect=[bridge.BridgeRejected("guest_ssh_exit_unconfirmed"), answer])
        scope = envelope | {"args": self.args}
        with patch.object(native_guest_ssh, "_identity", return_value={}), \
                patch.object(native_guest_ssh, "_checked_file", return_value=(b"key", None)), \
                patch.object(native_guest_ssh, "_host_record"), \
                patch.object(native_guest_ssh, "_same_files", return_value=True), \
                patch.object(native_guest_ssh, "_ssh_config", return_value=Path("/fixture/config")):
            result = guest._invoke("A", bridge._bytes(scope), time.monotonic() + 100, self.request, None)
        self.assertEqual(result, bridge._bytes(envelope | bridge.Receipt("PASS", proof).fields("restore")))
        self.assertEqual([json.loads(call.args[1]) for call in guest.run.call_args_list],
                         [scope, private | {"args": self.args}])
        with self.assertRaisesRegex(bridge.BridgeRejected, "bridge_guest_only_request"):
            bridge._Chain("cutover").check(private["verb"], "A", self.args)

    def test_guest_requires_matching_immutable_receipt_and_fresh_closed_restore(self):
        bridge = self.bridge
        worker = object.__new__(native_guest_worker.GuestWorker)
        worker.request = replace(self.request, verb=bridge.GUEST_RESTORE_READBACK)
        worker.args = self.args
        worker.stage = Path("/fixture/attempt")
        worker.restore_stage = worker.stage / "restore"
        worker.input_digest = "3" * 64
        worker._assert_no_activation = MagicMock()
        worker._receiver_settled = MagicMock()
        worker._proof = MagicMock(return_value=(None, {"manifest_sha256": self.args["manifest_sha256"]}))
        worker.restore_pins = MagicMock()
        common = native_guest_worker._scope(self.request) | {"worker_input_sha256": worker.input_digest}
        result = {"status": "PASS", "manifest_sha256": self.args["manifest_sha256"],
                  "services": sorted(native_guest_worker.SERVICES), "database_and_apps_closed": True,
                  "writer_authority_issued": False, "migration_receipt_sha256": "4" * 64}
        intent = common | {"args_sha256": bridge._digest(bridge._bytes(self.args))}
        receipt = bridge._bytes(common | {"evidence": result})
        records = [(bridge._bytes(intent), intent), (receipt, json.loads(receipt)), (bridge._bytes(result), result)]
        with patch.object(native_guest_worker, "_read_json", side_effect=records * 2), \
                patch.object(native_guest_restore, "migration_journal", return_value=worker.restore_stage / "journal"), \
                patch.object(native_guest_restore, "observe_closed_outcome", side_effect=[
                    {"status": "COMMITTED_CLOSED", "migration_receipt_sha256": result["migration_receipt_sha256"]},
                    {"status": "UNKNOWN"},
                ]):
            self.assertEqual(worker.read_restore_receipt(), bridge.Receipt("PASS", bridge._digest(receipt)))
            with self.assertRaisesRegex(TransitionBlocked, "guest_worker_restore_unsettled"):
                worker.read_restore_receipt()


class NativeGuestRestoreTests(unittest.TestCase):
    def setUp(self):
        self.pins = native_guest_restore.RestorePins(
            boot_sha256="a" * 64, installation_id="b" * 64, inventory_sha256="c" * 64,
            postgres_id="d" * 64, uid=70, gid=70, system_id="123456789",
            utility_digest="sha256:" + "e" * 64, initializer_sha256="f" * 64, source_commit="a" * 40,
            plan_sha256="b" * 64, config_sha256="c" * 64, manifest_sha256="d" * 64, cluster="test-ha",
            peer_ipv4="192.0.2.6",
        )

    def test_only_explicitly_retained_postgres_may_be_stopped_alongside_active(self):
        def container(**state):
            return {"Config": {"Labels": {"com.docker.compose.service": "postgres"}},
                    "State": state, "HostConfig": {"RestartPolicy": {"Name": "no"}}}

        active = container(Running=True)
        retained = container(Status="exited", Running=False, Pid=0, Paused=False, Restarting=False)
        objects = {"active": active, "retained": retained}
        self.assertIs(native_guest_restore.active_postgres(objects, "active", "retained"), active)
        self.assertNotIn("retained_postgres_id", native_guest_restore._intent(self.pins))
        with self.assertRaises(TransitionBlocked):
            native_guest_restore.active_postgres(objects | {"unexpected": retained}, "active", "retained")
        for postgres_id, retained_id in (("active", None), ("active", "foreign"), ("retained", "retained")):
            with self.assertRaises(TransitionBlocked):
                native_guest_restore.active_postgres(objects, postgres_id, retained_id)
        for field, value in (("Running", True), ("Pid", 7), ("Restarting", True), ("Status", "created")):
            with self.subTest(field=field), patch.dict(retained["State"], {field: value}):
                with self.assertRaises(TransitionBlocked):
                    native_guest_restore.active_postgres(objects, "active", "retained")

    def test_restore_has_one_durable_attempt_and_checks_closed_postgres(self):
        module = native_guest_restore
        identifier = "f" * 64
        for timeout in (False, True):
            with self.subTest(timeout=timeout), tempfile.TemporaryDirectory() as directory:
                journal = Path(directory).resolve()
                stage = journal / "stage"
                owned = {"Id": identifier, "Name": "/n100-ha-closed-restore-" + self.pins.installation_id[:16],
                         "Image": self.pins.utility_digest,
                         "Config": {"User": "70:70", "Labels": {"n100-ha.closed-restore": self.pins.manifest_sha256}},
                         "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "RestartPolicy": {"Name": "no"},
                                        "Privileged": False, "CapDrop": ["ALL"], "CapAdd": None,
                                        "SecurityOpt": ["no-new-privileges:true"],
                                        "LogConfig": {"Type": "local", "Config": {"max-size": "32m", "max-file": "1",
                                                                                 "compress": "false"}}},
                         "Mounts": [
                             {"Type": "bind", "Source": str(source), "Destination": destination, "RW": writable}
                             for source, destination, writable in (
                                 (module.INITIALIZER, "/bootstrap/initialize.py", False),
                                 (module.PG_SOCKET, "/var/run/postgresql", False), (stage / "inputs", "/inputs", False),
                                 (stage / "frozen", "/frozen", False), (stage / "receipts", "/receipts", True),
                             )
                         ],
                         "State": {"Status": "created"}}
                exited = dict(owned, State={"Status": "exited", "ExitCode": 0}, RestartCount=0)

                def docker(action, *args, **kwargs):
                    self.assertTrue((journal / "migration-intent.json").is_file())
                    if action == "create":
                        self.assertIn("--read-only", args)
                        self.assertEqual(args[args.index("--network") + 1], "none")
                        self.assertEqual(args[args.index("--phase") + 1], "migration")
                        self.assertEqual(
                            [args[index + 1] for index, option in enumerate(args) if option == "--log-opt"],
                            ["max-size=32m", "max-file=1", "compress=false"],
                        )
                        self.assertIn("type=bind,src=" + str(stage / "frozen") + ",dst=/frozen,readonly", args)
                        self.assertIn("type=bind,src=" + str(stage / "receipts") + ",dst=/receipts", args)
                        return identifier.encode()
                    self.assertEqual((action, args), ("start", ("-a", identifier)))
                    if timeout:
                        raise subprocess.TimeoutExpired(action, 1)
                    return b"PASS-output"

                with (
                    patch.object(module, "JOURNAL", journal),
                    patch.object(module.os, "geteuid", return_value=0),
                    patch.object(module, "_directory"),
                    patch.object(module, "_identity"),
                    patch.object(module, "_prestage", return_value=(stage / "inputs", stage / "frozen",
                                 stage / "receipts", {}, "1" * 64)),
                    patch.object(module, "_assert_inventory"),
                    patch.object(module, "_assert_pg", return_value="2" * 64) as pg,
                    patch.object(module, "_fsync_directory"),
                    patch.object(module, "_docker", side_effect=docker) as commands,
                    patch.object(module, "_inspect", side_effect=([owned], [exited])),
                    patch.object(module, "_verify_receipt", return_value="9" * 64) as receipt,
                ):
                    if timeout:
                        with self.assertRaisesRegex(TransitionBlocked, "outcome_unconfirmed_reconcile_intent"):
                            module.restore_closed(self.pins, stage)
                        receipt.assert_not_called()
                        self.assertFalse((journal / "migration-pass.json").exists())
                    else:
                        result = module.restore_closed(self.pins, stage)
                        self.assertIs(result["database_and_apps_closed"], True)
                        self.assertIs(result["writer_authority_issued"], False)
                        self.assertEqual(json.loads((journal / "migration-pass.json").read_bytes()), result)
                        pg.assert_called_with(self.pins, restored=True)
                    with self.assertRaisesRegex(TransitionBlocked, "reconciliation_required"):
                        module.restore_closed(self.pins, stage)
                    self.assertEqual(commands.call_count, 2)
                    self.assertNotIn(self.pins.peer_ipv4, (journal / "migration-intent.json").read_text())

    def test_scoped_restore_journal_and_container_do_not_reuse_legacy_identity(self):
        module = native_guest_restore
        attempt = "1" * 64
        pins = replace(self.pins, retained_postgres_id="2" * 64, attempt_id=attempt)
        stage = Path("/var/lib/n100-ha/cutover") / attempt / "restore"
        self.assertEqual(module.migration_journal(self.pins, stage), module.JOURNAL)
        self.assertEqual(module.migration_journal(pins, stage), stage / "journal")
        self.assertEqual(module._intent(pins)["container"], module._intent(self.pins)["container"] + "-" + attempt)
        self.assertNotIn("attempt_id", module._intent(self.pins))
        with self.assertRaisesRegex(TransitionBlocked, "attempt_stage_unbound"):
            module.migration_journal(pins, stage.parent.parent / ("3" * 64) / "restore")

    def test_reconciliation_observes_missing_or_ambiguous_intent_without_retry(self):
        module = native_guest_restore
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory).resolve()
            with (patch.object(module, "JOURNAL", journal), patch.object(module.os, "geteuid", return_value=0),
                  patch.object(module, "_directory"), patch.object(module, "_identity"),
                  patch.object(module, "_prestage"), patch.object(module, "_assert_inventory"),
                  patch.object(module, "_assert_pg"), patch.object(module, "_docker") as docker):
                self.assertEqual(module.observe_closed_outcome(self.pins, journal)["status"], "CLEAN")
                (journal / "migration-intent.json").write_bytes(b"{")
                self.assertEqual(module.observe_closed_outcome(self.pins, journal),
                                 {"status": "UNKNOWN", "automatic_retry_permitted": False})
                docker.assert_not_called()

    def test_reconciliation_requires_exact_precommit_failure_and_fresh_postgres(self):
        module = native_guest_restore
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory).resolve()
            stage = journal / "stage"
            identifier = "9" * 64
            manifest = {"freeze_receipt": {"sha256": "7" * 64}}
            audit = "8" * 64
            name = "migration-intent.json"
            (journal / name).touch()
            common = module._receipt_common(self.pins, manifest, audit)
            failure = dict(common, status="FAILED", error_type="RuntimeError", stage="frozen_data", sqlstate="P0001")
            values = {name: module._intent(self.pins), "migration.json.prepared": dict(common, status="PREPARED"),
                      "migration.json.failed": failure}
            owned = {"Id": identifier, "State": {"ExitCode": 1}, "RestartCount": 0}
            output = encoded({"status": "FAIL", "reason": "frozen_seven_migration_failed"})

            def read(path, *args):
                return encoded(values[path.name])

            with (
                patch.object(module, "JOURNAL", journal),
                patch.object(module.os, "geteuid", return_value=0),
                patch.object(module, "_directory"),
                patch.object(module, "_identity"),
                patch.object(module, "_prestage",
                             return_value=(stage / "inputs", stage / "frozen", stage / "receipts", manifest, audit)),
                patch.object(module, "_read", side_effect=read),
                patch.object(module, "_inspect", return_value=[owned]),
                patch.object(module, "_assert_migration_container"),
                patch.object(module, "_assert_inventory") as inventory,
                patch.object(module, "_assert_pg") as postgres,
                patch.object(module, "_pg", return_value=True) as catalog,
                patch.object(module, "_docker", return_value=output) as docker,
            ):
                self.assertEqual(module.observe_closed_outcome(self.pins, stage), {
                    "status": "CLEAN_FAILED", "automatic_retry_permitted": False,
                    "migration_failure_sha256": sha(encoded(failure)),
                })
                inventory.assert_called_once_with(self.pins, identifier)
                postgres.assert_called_once_with(self.pins)
                catalog.assert_called_once_with(self.pins, module.FRESH_CATALOG)
                docker.assert_called_once_with("logs", identifier)
                catalog.return_value = False
                self.assertEqual(module.observe_closed_outcome(self.pins, stage)["status"], "UNKNOWN")
                catalog.return_value = True
                values["migration.json.failed"] = dict(failure, status="UNKNOWN_COMMIT", stage="commit")
                self.assertEqual(module.observe_closed_outcome(self.pins, stage)["status"], "UNKNOWN")
                values["migration.json.failed"] = dict(failure, manifest_sha256="0" * 64)
                self.assertEqual(module.observe_closed_outcome(self.pins, stage)["status"], "UNKNOWN")
                self.assertTrue(all(call.args == ("logs", identifier) for call in docker.call_args_list))

    def test_postgres_rejects_drift_and_open_authority(self):
        module = native_guest_restore
        closed = [True, self.pins.cluster, 14, 14, sorted(M.SERVICES), 0, 1, True]
        replica = ["postgres", "ha", "ha_A", False, self.pins.system_id, "on", "remote_apply", "FIRST1(ha_b)", 11]
        with patch.object(module, "_pg", side_effect=(replica, closed, "1/F0", [[self.pins.peer_ipv4, "1/F0"]])):
            self.assertRegex(module._assert_pg(self.pins, restored=True), r"^[a-f0-9]{64}$")
        with patch.object(module, "_pg", return_value=[*replica[:6], "off", *replica[7:8], 0]):
            with self.assertRaisesRegex(TransitionBlocked, "identity_or_sync_mismatch"):
                module._assert_pg(self.pins)
        with patch.object(module, "_pg", side_effect=(replica, [False, *closed[1:]])):
            with self.assertRaisesRegex(TransitionBlocked, "postgres_not_closed"):
                module._assert_pg(self.pins, restored=True)
        with patch.object(module, "_pg", side_effect=([*replica[:8], 0], [["192.0.2.90", "1/F0"]])):
            with self.assertRaisesRegex(TransitionBlocked, "replica_identity_mismatch"):
                module._assert_pg(self.pins)
        with (patch.object(module, "_pg", side_effect=(replica, closed, "2/0", [[self.pins.peer_ipv4, "1/FFFFFFFF"]])),
              patch.object(module.time, "monotonic", side_effect=(1, 7))):
            with self.assertRaisesRegex(TransitionBlocked, "replica_replay_unconfirmed"):
                module._assert_pg(self.pins, restored=True)

    def test_receipt_rejects_a_missing_component_or_state(self):
        module = native_guest_restore
        services = {service: {"states": {"settings": "json"}, "databases": {"main": "db"}}
                    for service in M.SERVICES}
        data = {
            service: {"state_keys": 1, "state_sha256": "c" * 64,
                      "databases": [{"service": service, "component": "main", "tables": {"test": {}}}]}
            for service in services
        }
        module._verify_restored_data(data, services)
        data["portal-web"]["databases"] = []
        with self.assertRaisesRegex(TransitionBlocked, "receipt_unconfirmed"):
            module._verify_restored_data(data, services)

    def test_handoff_publishes_only_verified_frozen_catalog_and_preserves_source(self):
        module = native_guest_restore
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, stage = root / "received", root / "stage"
            for path in (source, stage):
                path.mkdir(mode=0o700)
            write = native_frozen_capture.seven_capture._write
            evidence = {key: write(source / f"{key}.json", b"{}")
                        for key in ("runtime", "writer_graph", "writer_closure", "dispositions")}
            audit = write(source / "capture-audit.json", encoded({"evidence": evidence}))
            freeze = write(source / "freeze-receipt.json", encoded({"capture_audit": audit}))
            artifact = write(source / "artifact.blob", b"original")
            write(source / "unlisted.blob", b"not part of the migration")
            manifest = write(source / "manifest.json", encoded({
                "source_commit": self.pins.source_commit, "target_system_id": self.pins.system_id,
                "plan_sha256": self.pins.plan_sha256, "cluster": self.pins.cluster,
                "services": dict.fromkeys(M.SERVICES, {}), "freeze_receipt": freeze,
                "artifacts": {"artifact": artifact},
            }))
            pins = replace(self.pins, manifest_sha256=manifest["sha256"])

            def fixture_copy(path, destination, entry, pins, *, metadata=False):
                raw = path.read_bytes()
                if (len(raw), sha(raw)) != (entry["bytes"], entry["sha256"]):
                    raise M.MigrationRejected("fixture_changed")
                write(destination / entry["file"], raw)
                return raw if metadata else None

            with (patch.object(module.os, "geteuid", return_value=0), patch.object(module, "_directory"),
                  patch.object(module.os, "chown"), patch.object(module, "_copy_frozen_entry", side_effect=fixture_copy),
                  patch.object(module, "_read", return_value=(source / "manifest.json").read_bytes())):
                (source / "artifact.blob").write_bytes(b"modified")
                with self.assertRaisesRegex(TransitionBlocked, "frozen_stage_unconfirmed"):
                    module.stage_frozen(pins, source, stage)
                self.assertEqual(list(stage.iterdir()), [])
                (source / "artifact.blob").write_bytes(b"original")
                before = {item.name: item.stat().st_ino for item in source.iterdir()}
                destination = module.stage_frozen(pins, source, stage)
                self.assertEqual((destination / "artifact.blob").read_bytes(), b"original")
                self.assertFalse((destination / "unlisted.blob").exists())
                self.assertEqual(before, {item.name: item.stat().st_ino for item in source.iterdir()})


class NativeFrozenCaptureTests(unittest.TestCase):
    def test_migration_ingress_checks_challenge_and_exact_http_body(self):
        manifest, challenge = "1" * 64, "2" * 64
        body = b"x" * (frozen_ingress.OVERHEAD + 1)

        def exchange(auth, *, fail_ack=False):
            raw = (f"POST {frozen_ingress.PATH} HTTP/1.1\r\n"
                   f"Host: {frozen_ingress.HOST}\r\nAuthorization: {auth}\r\n"
                   "Content-Type: application/octet-stream\r\n"
                   f"Content-Length: {len(body)}\r\n\r\n").encode() + body
            response = bytearray()
            def sendall(value):
                if fail_ack and value == manifest.encode():
                    raise TimeoutError("fixture")
                response.extend(value)
            request = SimpleNamespace(settimeout=lambda _: None, makefile=lambda *_: io.BytesIO(raw),
                                      sendall=sendall, shutdown=lambda _: None)
            server = SimpleNamespace(authorization="Bearer " + challenge, manifest=manifest,
                                     destination="private", max_bytes=100, result=None, rejected=False,
                                     published=None, deadline=time.monotonic() + 1)
            frozen_ingress._Receiver(request, (), server)
            headers, payload = response.split(b"\r\n\r\n", 1)
            return int(headers.split(b" ", 2)[1]), bytes(payload), server.result

        with patch.object(frozen_ingress, "receive_bundle", return_value=manifest) as receive:
            self.assertEqual(exchange("Bearer " + "3" * 64), (403, b"", None))
            receive.assert_not_called()
            self.assertEqual(exchange("Bearer " + challenge), (200, manifest.encode(), manifest))
            self.assertEqual(receive.call_args.kwargs, {"max_bytes": 100, "wire_bytes": len(body)})
            self.assertEqual(exchange("Bearer " + challenge, fail_ack=True), (200, b"", None))

    def test_frozen_transfer_rejects_tampering_before_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "source"
            source.mkdir(mode=0o700)
            write = native_frozen_capture.seven_capture._write
            evidence = {key: write(source / f"{key}.json", b"{}")
                        for key in ("runtime", "writer_graph", "writer_closure", "dispositions")}
            audit = write(source / "capture-audit.json", encoded({"evidence": evidence}))
            receipt = write(source / "freeze-receipt.json", encoded({"capture_audit": audit}))
            content = b"synthetic frozen bytes"
            artifact = write(source / "synthetic.blob", content)
            manifest = write(source / "manifest.json", encoded({
                "freeze_receipt": receipt, "artifacts": {"synthetic": artifact},
            }))
            (source / "unreferenced.txt").write_text("do not send")
            wire = io.BytesIO()
            frozen_transport.send_bundle(source, manifest["sha256"], wire)
            data = wire.getvalue()
            self.assertNotIn(b"do not send", data)
            (source / "synthetic.blob").write_bytes(b"X" + content[1:])
            bad_source = io.BytesIO()
            with self.assertRaisesRegex(M.MigrationRejected, "frozen_artifact_changed"):
                frozen_transport.send_bundle(source, manifest["sha256"], bad_source)
            self.assertNotIn(b"X" + content[1:], bad_source.getvalue())
            target = root / "target"
            mutated = data.replace(content, b"X" + content[1:])
            with self.assertRaisesRegex(TransitionBlocked, "hash_mismatch"):
                frozen_transport.receive_bundle(target, manifest["sha256"], io.BytesIO(mutated), max_bytes=4000)
            self.assertFalse(target.exists())
            with self.assertRaisesRegex(TransitionBlocked, "size_invalid"):
                frozen_transport.receive_bundle(target, manifest["sha256"], io.BytesIO(data), max_bytes=10)
            self.assertFalse(target.exists())
            with self.assertRaisesRegex(TransitionBlocked, "wire_size_invalid"):
                frozen_transport.receive_bundle(target, manifest["sha256"], io.BytesIO(data),
                                                max_bytes=4000, wire_bytes=len(data) + 1)
            self.assertEqual(frozen_transport.receive_bundle(target, manifest["sha256"], io.BytesIO(data),
                                                            max_bytes=4000, wire_bytes=len(data)), manifest["sha256"])
            self.assertEqual((target / "synthetic.blob").read_bytes(), content)
            self.assertFalse((target / "unreferenced.txt").exists())

    def test_frozen_transfer_publish_is_exclusive_and_reports_uncertain_durability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            staging, target = root / "staging", root / "target"
            staging.mkdir(mode=0o700)
            target.mkdir(mode=0o700)
            inode = target.stat().st_ino
            with self.assertRaisesRegex(TransitionBlocked, "destination_exists"):
                frozen_transport._publish(staging, target)
            self.assertEqual(target.stat().st_ino, inode)
            target.rmdir()
            with patch.object(frozen_transport.os, "fsync", side_effect=OSError("fixture")):
                with self.assertRaisesRegex(TransitionBlocked, "publish_unconfirmed"):
                    frozen_transport._publish(staging, target)
            self.assertTrue(target.is_dir())
            self.assertFalse(staging.exists())

    def test_actual_writer_and_reader_wiring_requires_complete_external_closure(self):
        services = sorted(M.SERVICES)
        k3s = ("portal-web", "book-memo", "youtube-memo", "crawler-worker")
        identity = {"source_commit": "a" * 40, "cluster": "n100", "plan_sha256": "b" * 64,
                    "target_system_id": "123"}
        external = {service: ["windows:recovery", "github:deploy"] for service in services}
        baseline = {
            "containers": {service: {"id": str(index) * 64, "running": service == "car-care-worker"}
                           for index, service in enumerate(services, 1)},
            "deployments": {service: {"uid": service + "-uid"} for service in k3s},
            "schedules": [{"uid": "portal-backup", "name": "portal-backup"}],
        }
        bindings = {service: {"deployment_uid": service + "-uid", "claims": [{"name": service}]}
                    for service in k3s}
        schedule = {"spec": {"jobTemplate": {"spec": {"template": {"spec": {}}}}}}
        witnesses = {"windows:recovery": "c" * 64, "github:deploy": "d" * 64}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(native_frozen_capture, "inspect_live_baseline", return_value=baseline),
            patch.object(native_frozen_capture, "inspect_pvc_bindings", return_value=bindings),
            patch.object(native_frozen_capture, "inspect_compose_capture", return_value={}) as compose,
            patch.object(native_frozen_capture, "LinuxWriters", autospec=True) as linux,
            patch.object(native_frozen_capture, "PvcCapture", autospec=True) as pvc,
            patch.object(native_frozen_capture, "ComposeCapture"),
            patch.object(native_frozen_capture, "capture_closed_sources") as capture,
        ):
            linux.return_value._schedule.return_value = (schedule, False)
            linux.return_value._claims.return_value = {"portal-web"}
            pvc.return_value.bindings = bindings
            observe = MagicMock(return_value={"windows:recovery": "c" * 64})
            converter = native_frozen_capture.seven_capture
            source_pins = converter.inspect_converter_sources(converter.Reader())
            source = native_frozen_capture.NativeFrozenCapture(Path(directory).resolve(), identity, "e" * 64,
                                                               source_pins, external, observe)
            coordinator = {"pid": 26, "start": 39, "process": "a" * 64}
            source.prepare(coordinator=coordinator)
            runtime_path = source.root / "runtime.json"
            self.assertEqual(json.loads(runtime_path.read_bytes())["coordinator"], coordinator)
            self.assertEqual(converter.descriptor(runtime_path)["sha256"], source.runtime_sha256)
            self.assertEqual(compose.call_args.kwargs["stopped_services"], {"homeops-executor"})
            source.block_reactivation()
            with self.assertRaisesRegex(TransitionBlocked, "external_closure_incomplete"):
                source.fence("f" * 64, "1" * 64)
            linux.return_value.fence.assert_not_called()
            observe.return_value = witnesses
            fenced = source.fence("f" * 64, "1" * 64)
            fence = fenced["evidence_sha256"]
            with self.assertRaisesRegex(TransitionBlocked, "fence_unbound"):
                source.capture("9" * 64)
            capture.assert_not_called()
            evidence = source.root / "writer-closure.json"
            original = evidence.read_bytes()
            modified = json.loads(original)
            modified["observations"][0]["evidence_sha256"] = "9" * 64
            evidence.write_bytes(encoded(modified))
            with self.assertRaisesRegex(TransitionBlocked, "evidence_changed"):
                source.capture(fence)
            capture.assert_not_called()
            evidence.write_bytes(original)

            def capture_fixture(**kwargs):
                kwargs["assert_writers_closed"]()
                output = kwargs["output"]
                output.mkdir(mode=0o700)
                write = native_frozen_capture.seven_capture._write
                receipt = write(output / "freeze-receipt.json", encoded({
                    "writer_fence_receipt_sha256": fence,
                    "source_runtime_sha256": source.runtime_sha256,
                    "writer_graph_sha256": source.graph_sha256,
                    "capture_audit": {"sha256": "3" * 64},
                }))
                manifest = write(output / "manifest.json", encoded(identity | {"freeze_receipt": receipt}))
                return {"status": "captured", "manifest_sha256": manifest["sha256"],
                        "capture_audit_sha256": "3" * 64}

            capture.side_effect = capture_fixture
            self.assertEqual(source.capture(fence)["evidence_sha256"], "3" * 64)
            self.assertEqual({call.args[2] for call in observe.call_args_list}, {"1" * 64})
            graph = json.loads((source.root / "writer-graph.json").read_bytes())["services"]
            self.assertIn("kube:cronjob:portal-backup", graph["portal-web"])
            self.assertNotIn("kube:cronjob:portal-backup", graph["book-memo"])
            pvc.return_value.assert_settled.side_effect = TransitionBlocked("pvc_cleanup_pending")
            with self.assertRaisesRegex(TransitionBlocked, "pvc_cleanup_pending"):
                source.restore_writers()
            linux.return_value.restore.assert_not_called()


class FrozenFixture:
    def __init__(self,root):
        self.root=root;self.artifacts={}
        self.config={'source_commit':'a'*40,'cluster':'migration_fixture','plan_sha256':'b'*64}
        self.services={service:{'databases':{},'states':{}} for service in M.SERVICES}
        for service in sorted(M.SERVICES):
            for component in sorted(M.REQUIRED_DATABASES[service]):
                key=service+'.'+component
                path=root/(key+'.sqlite3')
                with sqlite3.connect(path) as db:
                    db.execute('CREATE TABLE records(id INTEGER PRIMARY KEY AUTOINCREMENT,value TEXT NOT NULL)')
                    db.execute('INSERT INTO records(value) VALUES(?)',('retained 한글',))
                path.chmod(0o600);self.add_file(key,path)
                self.services[service]['databases'][component]=key
            for key in sorted(M.REQUIRED_STATES[service]):
                payload={}
                if key=='portfolio':payload={'content':'retained portfolio'}
                if key=='auth_sessions':payload={'sessions':{},'confirmations':{}}
                if key=='archive':payload={'articles':[]}
                if key=='file-namespace':payload={'format':1,'version':0,'entries':{},'trash':{},'confirmations':{},'audit':[]}
                identifier=service+'.'+key
                self.add_json(identifier,payload)
                self.services[service]['states'][key]=identifier
        self.add_json('private-hyundai',{'refresh_token':'fixture-private-refresh-value'})
        now=datetime.now(timezone.utc)
        self.receipt={'schema_version':1,'capture_id':'1'*32,'source_runtime_sha256':'2'*64,'writer_graph_sha256':'3'*64,
            'writer_fence_receipt_sha256':'4'*64,'writers_closed_at':(now-timedelta(seconds=3)).isoformat().replace('+00:00','Z'),
            'capture_started_at':(now-timedelta(seconds=2)).isoformat().replace('+00:00','Z'),
            'capture_finished_at':(now-timedelta(seconds=1)).isoformat().replace('+00:00','Z'),
            'services':sorted(M.SERVICES),'target_system_id':'123456','cluster':self.config['cluster'],
            'plan_sha256':self.config['plan_sha256'],'artifacts':{}}
        evidence={}
        for key,field in (('runtime','source_runtime_sha256'),('writer_graph','writer_graph_sha256'),
                          ('writer_closure','writer_fence_receipt_sha256'),('dispositions',None)):
            path=root/(key+'.json');raw=encoded({'fixture':key});path.write_bytes(raw);path.chmod(0o600)
            evidence[key]={'file':path.name,'sha256':sha(raw),'bytes':len(raw)}
            if field:self.receipt[field]=sha(raw)
        path=root/'capture-audit.json';raw=encoded({'schema_version':1,'evidence':evidence})
        path.write_bytes(raw);path.chmod(0o600)
        self.receipt['capture_audit']={'file':path.name,'sha256':sha(raw),'bytes':len(raw)}
        self.value={'schema_version':1,**self.config,'target_system_id':'123456','freeze_receipt':{},'artifacts':self.artifacts,
            'services':self.services,'portal_objects':{},'private_states':{'car-care-worker':{'hyundai_tokens':'private-hyundai'}}}
        self.path=root/'manifest.json';self.save()

    def add_file(self,key,path):
        raw=path.read_bytes();self.artifacts[key]={'file':path.name,'sha256':sha(raw),'bytes':len(raw)}

    def add_json(self,key,value):
        path=self.root/(key+'.json');path.write_bytes(encoded(value));path.chmod(0o600);self.add_file(key,path)

    def save(self):
        self.receipt['artifacts']={key:{'sha256':value['sha256'],'bytes':value['bytes']} for key,value in self.artifacts.items()}
        raw=encoded(self.receipt);path=self.root/'freeze.json';path.write_bytes(raw);path.chmod(0o600)
        self.value['freeze_receipt']={'file':path.name,'sha256':sha(raw),'bytes':len(raw)}
        self.path.write_bytes(encoded(self.value));self.path.chmod(0o600);self.expected=sha(self.path.read_bytes())

    def prepare(self):return M.FrozenSources(self.path,self.expected,self.config)


class FrozenInputTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.env=patch.dict(os.environ,{'APP_ENV':'test','HA_LOCAL_RUNTIME':'1'});self.env.start()
        self.fixture=FrozenFixture(self.root)
        prepare=self.fixture.prepare
        def registered_prepare():
            source=prepare();self.addCleanup(source.close);return source
        self.fixture.prepare=registered_prepare

    def tearDown(self):self.env.stop();self.temp.cleanup()

    def reject(self,reason=None):
        with self.assertRaises(M.MigrationRejected) as error:self.fixture.prepare()
        if reason:self.assertEqual(str(error.exception),reason)

    def test_all_seven_sources_remain_private_and_unchanged(self):
        source=self.fixture.prepare();source.unchanged()
        self.assertEqual(set(source.states),set(M.SERVICES))
        self.assertNotIn('hyundai_tokens',source.states['car-care-worker'])
        self.assertEqual(source.private_payload['refresh_token'],'fixture-private-refresh-value')

    def test_verified_sqlite_copy_survives_source_ancestor_swap_and_restore(self):
        stage=self.root/'stage';stage.mkdir(mode=0o700)
        fixture=FrozenFixture(stage)
        alternate=self.root/'replacement';shutil.copytree(stage,alternate)
        path=alternate/fixture.artifacts['book-memo.main']['file']
        with sqlite3.connect(path) as db:db.execute("UPDATE records SET value='untrusted alternate'")
        with fixture.prepare() as source:
            staged=source.databases['book-memo']['main']
            self.assertFalse(staged.is_relative_to(stage))
            self.assertEqual(staged.stat().st_mode&0o777,0o600)
            parked=self.root/'original-parked';seen=[]
            def copy_verified(admin,service,component,path):
                if service=='book-memo':
                    stage.rename(parked);alternate.rename(stage)
                    try:
                        with sqlite3.connect(path) as db:
                            seen.extend(db.execute('SELECT value FROM records').fetchall())
                    finally:stage.rename(alternate);parked.rename(stage)
                return {'copied':True}
            with patch.object(M.migrate,'copy_sqlite',side_effect=copy_verified),patch.object(M.migrate,'create_namespace'),patch.object(M.migrate,'seed_state'):
                M.import_data(MagicMock(),source)
            source.unchanged()  # ABA restores every original leaf stamp.
            self.assertEqual(seen,[('retained 한글',)])
        self.assertFalse(staged.exists())

    def test_private_sqlite_stage_rejects_untrusted_writable_ancestor(self):
        original=M.Path.lstat
        def unsafe(path):
            info=original(path)
            if path==M.Path('/tmp').resolve():
                values=list(info);values[0]=0o40777;return os.stat_result(values)
            return info
        with patch.object(M.Path,'lstat',unsafe):
            with self.assertRaisesRegex(M.MigrationRejected,'trusted_sqlite_stage_ancestor_required'):
                M._private_sqlite_stage()

    def test_constructor_failure_deterministically_removes_staged_private_copies(self):
        created=[];allocate=M._private_sqlite_stage
        def record():
            value=allocate();created.append(Path(value.name));return value
        self.fixture.add_json('crawler-worker.archive',{'articles':[],'alias':'fixture-private-refresh-value'})
        self.fixture.save()
        with patch.object(M,'_private_sqlite_stage',side_effect=record):
            self.reject('private_credential_cross_service_copy_rejected')
        self.assertTrue(created);self.assertTrue(all(not path.exists() for path in created))

    def test_mixed_case_credential_column_is_rejected(self):
        path=self.root/self.fixture.artifacts['book-memo.main']['file']
        with sqlite3.connect(path) as db:
            db.execute('ALTER TABLE records ADD COLUMN Refresh_Token TEXT')
        self.fixture.add_file('book-memo.main',path);self.fixture.save()
        self.reject('private_credential_sqlite_column_rejected')

    def test_private_token_in_generic_sqlite_blob_is_rejected(self):
        path=self.root/self.fixture.artifacts['book-memo.main']['file']
        with sqlite3.connect(path) as db:
            db.execute('UPDATE records SET value=?',(sqlite3.Binary(b'prefix-fixture-private-refresh-value-suffix'),))
        self.fixture.add_file('book-memo.main',path);self.fixture.save()
        self.reject('private_credential_sqlite_copy_rejected')

    def test_secret_comparison_handles_all_sqlite_binary_representations(self):
        secret='fixture-private-refresh-value'
        for value in (secret,secret.encode(),bytearray(secret.encode()),memoryview(secret.encode())):
            with self.subTest(kind=type(value).__name__):self.assertTrue(M._sqlite_secret(value,[secret]))
        self.assertFalse(M._sqlite_secret(b'ordinary binary data',[secret]))

    def test_source_only_preflight_does_not_enable_unpackaged_import(self):
        with patch.dict(os.environ,{'APP_ENV':'production','HA_LOCAL_RUNTIME':'0'}):
            self.reject('packaged_migration_release_required')
            with M.FrozenSources(self.fixture.path,self.fixture.expected,self.fixture.config,source_only_preflight=True) as source:
                source.unchanged()

    def test_one_service_omission_is_never_defaulted_to_empty(self):
        del self.fixture.services['system-agent'];self.fixture.save();self.reject('all_seven_sources_required')

    def test_manifest_identity_and_external_hash_are_both_required(self):
        self.fixture.config['cluster']='another_cluster';self.reject('frozen_manifest_identity_mismatch')
        with self.assertRaises(M.MigrationRejected):M.FrozenSources(self.fixture.path,'0'*64,self.fixture.config)

    def test_freeze_must_follow_writer_closure_and_bind_exact_inventory(self):
        self.fixture.receipt['writers_closed_at']=self.fixture.receipt['capture_finished_at']
        self.fixture.save();self.reject('freeze_capture_order_invalid')

    def test_changed_artifact_and_late_mutation_are_rejected(self):
        source=self.fixture.prepare()
        path=self.root/self.fixture.artifacts['crawler-worker.archive']['file'];path.write_bytes(b'{} ')
        with self.assertRaises(M.MigrationRejected):source.unchanged()
        self.reject('frozen_artifact_binding_invalid')

    def test_external_evidence_bytes_are_bound_and_cannot_change_after_validation(self):
        source=self.fixture.prepare();path=self.root/'writer_closure.json'
        path.write_bytes(b'{"changed":true}')
        with self.assertRaises(M.MigrationRejected):source.unchanged()
        self.reject('frozen_artifact_binding_invalid')

    def test_rereading_same_bytes_cannot_reset_original_metadata_stamp(self):
        source=self.fixture.prepare()
        path=self.root/self.fixture.artifacts['crawler-worker.archive']['file'];path.chmod(0o400)
        with self.assertRaises(M.MigrationRejected):source.artifact('crawler-worker.archive')

    def test_linked_or_nonprivate_artifacts_are_rejected(self):
        path=self.root/self.fixture.artifacts['crawler-worker.archive']['file'];path.chmod(0o644)
        self.reject('private_frozen_input_required');path.chmod(0o600)
        link=self.root/'linked';os.link(path,link);self.reject('private_frozen_input_required')

    def test_traversal_and_unconsumed_artifacts_are_rejected(self):
        self.fixture.artifacts['crawler-worker.archive']['file']='../outside.json';self.fixture.save()
        self.reject('artifact_relative_file_required')

    def test_unused_source_is_not_silently_discarded(self):
        self.fixture.add_json('unconsumed-source',{'retained':True});self.fixture.save()
        self.reject('unconsumed_frozen_artifact')

    def test_live_wal_snapshot_and_executable_ddl_are_rejected_before_pg(self):
        path=self.root/self.fixture.artifacts['book-memo.main']['file']
        sidecar=Path(str(path)+'-wal');sidecar.write_bytes(b'uncheckpointed');sidecar.chmod(0o600)
        self.reject('sqlite_snapshot_sidecar_rejected')

    def test_provider_credential_cannot_move_to_another_service(self):
        self.fixture.add_json('crawler-worker.archive',{'articles':[],'alias':'fixture-private-refresh-value'})
        self.fixture.save();self.reject('private_credential_cross_service_copy_rejected')

    def test_app_credential_keys_and_wrong_private_service_are_rejected(self):
        for key in ('client_secret','authorization_state'):
            self.fixture.add_json('crawler-worker.archive',{key:'must-not-enter-app-state'})
            self.fixture.save();self.reject('app_provider_credential_rejected')

    def test_portal_object_set_requires_exact_reference_and_bytes(self):
        content=b'actual frozen object';identifier=sha(content)
        namespace={'format':1,'version':0,'entries':{'file.txt':{'type':'file','size':len(content),'sha256':identifier}},'trash':{},'confirmations':{},'audit':[]}
        self.fixture.add_json('portal-web.file-namespace',namespace);self.fixture.save()
        self.reject('portal_blob_inventory_mismatch')
        path=self.root/'blob';path.write_bytes(content);path.chmod(0o600);self.fixture.add_file('blob',path)
        self.fixture.value['portal_objects']={identifier:'blob'};self.fixture.save()
        self.assertEqual(self.fixture.prepare().objects,{identifier:'blob'})

    def test_json_database_rejection_context_cannot_receive_nonfinite_or_nul_data(self):
        for raw in (b'{"value":NaN}',b'{"value":1e999}',b'{"value":"\\u0000"}',b'{"value":"\\ud800"}'):
            with self.assertRaises(M.MigrationRejected):M.json_payload(raw)

    def test_raw_bearer_session_and_unknown_restart_state_never_enter_typed_state(self):
        self.fixture.add_json('book-memo.write-sessions',{'raw-legacy-bearer':'2026-11-11T00:00:00+00:00'})
        self.fixture.save();self.reject('typed_session_artifact_required')
        with self.assertRaises(M.MigrationRejected):
            M.validate_state('homeops-executor','restart_claims',{'request':{'payload_hash':'a'*64,'created_at':1,'state':'invented','result':None}})

    def test_noncanonical_hidden_portal_path_is_rejected_by_actual_app_validator(self):
        namespace={'format':1,'version':0,'entries':{'.private':{'type':'folder'}},'trash':{},'confirmations':{},'audit':[]}
        self.fixture.add_json('portal-web.file-namespace',namespace);self.fixture.save()
        with self.assertRaises(DatabaseErrorTypes):self.fixture.prepare()


DatabaseErrorTypes=(M.MigrationRejected,M.DatabaseUnavailable)


class SourceSqlTests(unittest.TestCase):
    def test_data_literals_survive_but_functions_cannot_execute_as_pg_admin(self):
        self.assertIn("'lo_export(1)'",migrate._ddl("CREATE TABLE good(id INTEGER PRIMARY KEY,value TEXT DEFAULT 'lo_export(1)')"))
        for source in ["CREATE TABLE x(value TEXT CHECK(lo_export(1,'x')=1))",
                       'CREATE TABLE x(value TEXT DEFAULT (pg_catalog.current_setting(\'data_directory\')))',
                       'CREATE TABLE x(value TEXT CHECK("pg_sleep"(1) IS NULL))',
                       'CREATE TABLE x(value TEXT); DROP TABLE anything',
                       'CREATE TABLE x(value TEXT GENERATED ALWAYS AS (lower(value)) STORED)']:
            with self.subTest(source=source):
                with self.assertRaises(M.DatabaseUnavailable):migrate._ddl(source)

    def test_sqlitex_is_a_user_table_not_the_sqlite_reserved_prefix(self):
        with sqlite3.connect(':memory:') as source:
            source.execute('CREATE TABLE sqlitexevil(value TEXT)')
            self.assertEqual(set(migrate._source_tables(source)),{'sqlitexevil'})

    def test_triggers_fail_before_any_pg_call(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'source.sqlite3'
            with sqlite3.connect(path) as source:
                source.execute('CREATE TABLE records(value TEXT)')
                source.execute('CREATE TRIGGER t AFTER INSERT ON records BEGIN SELECT 1; END')
            admin=MagicMock()
            with self.assertRaises(M.DatabaseUnavailable):migrate.copy_sqlite(admin,'book-memo','main',path)
            admin.execute.assert_not_called()


class TransactionReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.admin=MagicMock();self.admin.autocommit=True;self.admin.info.transaction_status=0
        self.source=SimpleNamespace(close=MagicMock(),manifest_sha256='a'*64,freeze_receipt_sha256='b'*64,capture_audit_sha256='d'*64,objects={},manifest={'target_system_id':'123'})
        self.config={'source_commit':'c'*40,'primary_node':'A'};self.receipt=self.root/'receipt.json'

    def tearDown(self):self.temp.cleanup()

    def run_apply(self,prepare,guard):
        return M.apply(self.admin,self.config,self.root/'unused.json','a'*64,self.receipt,prepare,guard)

    def test_existing_transaction_cannot_turn_outer_commit_into_savepoint(self):
        for autocommit,status in ((False,0),(True,2)):
            self.admin.autocommit=autocommit;self.admin.info.transaction_status=status
            with self.assertRaisesRegex(M.MigrationRejected,'idle_autocommit_migration_connection_required'):
                self.run_apply(MagicMock(),MagicMock())
        self.admin.transaction.assert_not_called();self.assertFalse(self.receipt.exists())

    def test_import_failure_rolls_back_without_guard_or_authority_and_keeps_receipts(self):
        prepare=MagicMock();guard=MagicMock()
        with patch.object(M,'FrozenSources',return_value=self.source),patch.object(M,'fresh_target'),patch.object(M,'import_data',side_effect=RuntimeError('data_invalid')):
            with self.assertRaises(M.MigrationRejected):self.run_apply(prepare,guard)
        prepare.assert_called_once();guard.assert_not_called()
        self.assertIs(self.admin.transaction.return_value.__exit__.call_args.args[0],RuntimeError)
        self.assertFalse(self.receipt.exists())
        value=json.loads(Path(str(self.receipt)+'.failed').read_text())
        self.assertEqual(value['status'],'FAILED');self.assertFalse(value['writer_authority_issued'])
        self.assertTrue(Path(str(self.receipt)+'.prepared').exists())
        self.source.close.assert_called_once()

    def test_commit_acknowledgement_loss_is_unknown_not_retry_or_success(self):
        self.admin.transaction.return_value.__exit__.side_effect=OSError('connection_lost')
        with patch.object(M,'FrozenSources',return_value=self.source),patch.object(M,'fresh_target'),patch.object(M,'import_data',return_value=([],{})),patch.object(M,'finalize_data'):
            with self.assertRaises(M.MigrationRejected):self.run_apply(MagicMock(),MagicMock())
        self.assertFalse(self.receipt.exists())
        value=json.loads(Path(str(self.receipt)+'.failed').read_text());self.assertEqual(value['status'],'UNKNOWN_COMMIT')
        self.assertFalse(value['automatic_enable_performed'])

    def test_existing_receipt_is_preserved_before_any_prepare_or_write(self):
        self.receipt.write_text('preserve');prepare=MagicMock()
        with patch.object(M,'FrozenSources',return_value=self.source),patch.object(M,'fresh_target'):
            with self.assertRaises(M.MigrationRejected):self.run_apply(prepare,MagicMock())
        prepare.assert_not_called();self.assertEqual(self.receipt.read_text(),'preserve')
        self.source.close.assert_called_once()

    def test_wrong_database_and_contamination_never_clean_up_target(self):
        admin=MagicMock();admin.info.dbname='postgres'
        with self.assertRaises(M.MigrationRejected):M.fresh_target(admin,self.source,'A')
        admin.execute.assert_not_called()
        admin.info.dbname='ha'
        admin.execute.return_value.fetchone.side_effect=[(False,'ha_A','123'),(True,),(True,),(0,),(1,)]
        with self.assertRaises(M.MigrationRejected):M.fresh_target(admin,self.source,'A')
        self.assertFalse(any('DROP ' in str(call.args[0]).upper() or 'DELETE ' in str(call.args[0]).upper() for call in admin.execute.call_args_list))


def postgres_integration(host):
    """Explicit disposable-PG probe, never discovered as an implicit CI test.

    The caller owns an isolated network and fresh PG17 server named by host.
    This exercises the real transaction/import/guards, not source capture or a
    synchronous pair; the initializer's replication preflight remains separate.
    """
    import importlib.util
    import secrets
    import psycopg
    if os.getenv('APP_ENV')!='test' or os.getenv('HA_LOCAL_RUNTIME')!='1':
        raise AssertionError('isolated_test_environment_required')
    spec=importlib.util.spec_from_file_location('migration_initializer',ROOT/'infra/k8s/ha-package/bootstrap_assets/initialize.py')
    initializer=importlib.util.module_from_spec(spec);spec.loader.exec_module(initializer)
    with psycopg.connect(host=host,dbname='postgres',user='postgres',autocommit=True) as setup:
        setup.execute('CREATE DATABASE ha')
    with tempfile.TemporaryDirectory() as directory,psycopg.connect(host=host,dbname='ha',user='postgres',autocommit=True) as admin:
        root=Path(directory)/'frozen';root.mkdir(mode=0o700);fixture=FrozenFixture(root)
        for service in ('crawler-worker','homeops-executor'):
            key=service+'.main';path=root/(key+'.sqlite3')
            with sqlite3.connect(path) as source:
                source.execute('CREATE TABLE retained(id INTEGER PRIMARY KEY,value TEXT)')
                source.execute('INSERT INTO retained VALUES(7,?)',('preserved '+service,))
            path.chmod(0o600);fixture.add_file(key,path);fixture.services[service]['databases']['main']=key
        content=b'real PG frozen blob';identifier=sha(content);path=root/'blob'
        path.write_bytes(content);path.chmod(0o600);fixture.add_file('blob',path)
        fixture.add_json('portal-web.file-namespace',{'format':1,'version':0,
            'entries':{'retained.txt':{'type':'file','size':len(content),'sha256':identifier}},
            'trash':{},'confirmations':{},'audit':[]})
        fixture.value['portal_objects']={identifier:'blob'}
        system=admin.execute('SELECT system_identifier::text FROM pg_control_system()').fetchone()[0]
        fixture.value['target_system_id']=system;fixture.receipt['target_system_id']=system;fixture.save()
        config={**fixture.config,'primary_node':'A','broker_instance':'e'*64,
                'passwords':{service.replace('-','_')+'_'+node:secrets.token_urlsafe(32)
                             for service in M.SERVICES for node in ('a','b')}}
        config['passwords']['ha_effect_broker']=secrets.token_urlsafe(32)
        def fail_after_data(connection,value,entries):
            assert connection.execute('SELECT count(*) FROM s_book_memo_main.records').fetchone()==(1,)
            raise RuntimeError('deliberate_mid_import_failure')
        try:
            M.apply(admin,config,fixture.path,fixture.expected,root/'rollback.json',initializer.prepare_closed_control,fail_after_data)
        except M.MigrationRejected:
            pass
        else:raise AssertionError('deliberate_failure_committed')
        assert admin.execute("SELECT to_regnamespace('ha'),to_regnamespace('s_book_memo_main')").fetchone()==(None,None)
        assert admin.execute("SELECT count(*) FROM pg_roles WHERE rolname='book_memo_a'").fetchone()==(0,)
        assert json.loads((root/'rollback.json.failed').read_text())['status']=='FAILED'
        alternate=root.with_name('replacement');shutil.copytree(root,alternate)
        with sqlite3.connect(alternate/fixture.artifacts['book-memo.main']['file']) as replacement:
            replacement.execute("UPDATE records SET value='untrusted alternate'")
        original_copy=M.migrate.copy_sqlite;swaps=[]
        def swap_external_ancestor(connection,service,component,path):
            if service!='book-memo':return original_copy(connection,service,component,path)
            parked=root.with_name('parked');root.rename(parked);alternate.rename(root)
            try:
                swaps.append(True)
                return original_copy(connection,service,component,path)
            finally:root.rename(alternate);parked.rename(root)
        with patch.object(M.migrate,'copy_sqlite',side_effect=swap_external_ancestor):
            receipt=M.apply(admin,config,fixture.path,fixture.expected,root/'success.json',initializer.prepare_closed_control,initializer.install_guarded_catalog)
        assert swaps==[True]
        assert admin.execute('SELECT value FROM s_book_memo_main.records').fetchone()==('retained 한글',)
        assert receipt['status']=='PASS' and receipt['writer_authority_issued'] is False
        assert admin.execute('SELECT NOT writes_open AND NOT history_safe FROM ha.control').fetchone()==(True,)
        assert admin.execute('SELECT count(*) FROM ha.enforced_roles').fetchone()==(14,)
        assert admin.execute('SELECT count(DISTINCT service) FROM ha.enforced_namespaces').fetchone()==(7,)
        for service in ('crawler-worker','homeops-executor'):
            assert admin.execute('SELECT id,value FROM s_'+service.replace('-','_')+'_main.retained').fetchone()==(7,'preserved '+service)
        from ha_runtime.backup import _pg_blob
        assert _pg_blob(admin,identifier,len(content))==content
        assert admin.execute("SELECT payload->>'refresh_token' FROM ha_effects.private_state WHERE service='car-care-worker' AND key='hyundai_tokens'").fetchone()==('fixture-private-refresh-value',)
        with psycopg.connect(host=host,dbname='ha',user='book_memo_a',autocommit=True) as app:
            try:app.execute("INSERT INTO s_book_memo_main.records(value) VALUES('blocked')")
            except psycopg.Error as error:assert error.sqlstate=='42501'
            else:raise AssertionError('closed_app_write_accepted')
            try:app.execute('SELECT payload FROM ha_effects.private_state')
            except psycopg.Error as error:assert error.sqlstate=='42501'
            else:raise AssertionError('app_read_private_state_accepted')
        return {'status':'PASS','real_postgresql':True,'mid_import_rollback':True,'seven_services':True,
                'portal_blob':True,'source_ancestor_ABA_cannot_change_imported_bytes':True,'private_tokens_separated':True,'closed_app_write_denied':True,
                'source_capture_verified':False,'synchronous_pair_verified':False,'writer_authority_issued':False}



class CutoverRehearsal:
    """In-process adapter exercising order, observations, and a failure boundary."""

    def __init__(self, path, failure=None, unsafe=False, unsettled=False):
        self.path = path
        self.failure = failure
        self.unsafe = unsafe
        self.unsettled = unsettled
        self.calls = []
        self.guarded = False
        self.legacy_open = True
        self.vm_open = False

    @contextmanager
    def guard(self, plan, attempt):
        assert not self.guarded
        self.guarded = True
        self.attempt = attempt
        try:
            yield
        finally:
            self.guarded = False

    def receipt(self, stage, plan, **facts):
        assert self.guarded
        journal = json.loads(self.path.read_bytes())
        assert journal['plan_sha256'] == plan and journal['attempt_id'] == self.attempt
        assert stage == 'preflight' or journal['phase'].endswith('_intent') or stage.startswith('verify_')
        self.calls.append(stage)
        if self.failure == stage:
            raise RuntimeError('lost action response')
        return dict(plan_sha256=plan, attempt_id=self.attempt, evidence_sha256=sha(stage.encode()), **facts)

    def preflight(self, plan):
        return self.receipt('preflight', plan, capture_ready=True, restore_ready=True, ingress_ready=True,
                            legacy_live=True, target_writes_closed=True)

    def close_admission(self, plan, baseline):
        return self.receipt('close_admission', plan, baseline_sha256=baseline,
                            new_deployments_blocked=True, admitted_deployments_settled=True)

    def block_reactivation(self, plan, baseline, admission):
        return self.receipt('block_reactivation', plan, baseline_sha256=baseline,
                            admission_sha256=admission, blocked=True)

    def assert_reactivation_closed(self, plan, admission, blocked, boundary):
        return self.receipt(boundary, plan, admission_sha256=admission, reactivation_sha256=blocked,
                            boundary=boundary, new_deployments_blocked=True, admitted_deployments_settled=True,
                            windows_reactivation_blocked=True, recovery_commands_settled=True)

    def assert_activation_safe(self, plan, admission, blocked, fence, restore):
        return self.receipt('before_activation', plan, admission_sha256=admission, reactivation_sha256=blocked,
                            boundary='before_activation', fence_sha256=fence, restore_sha256=restore,
                            new_deployments_blocked=True, admitted_deployments_settled=True,
                            windows_reactivation_blocked=True, recovery_commands_settled=True,
                            legacy_writers_closed=not self.legacy_open, target_writes_closed=not self.vm_open,
                            restore_ready=True)

    def fence_writers(self, plan, blocked, settled):
        self.legacy_open = False
        return self.receipt('fence_writers', plan, reactivation_sha256=blocked, settlement_sha256=settled, writers_closed=True,
                            services=sorted(M.SERVICES))

    def capture(self, plan, fence):
        assert not self.legacy_open and not self.vm_open
        return self.receipt('capture', plan, fence_sha256=fence, manifest_sha256='a' * 64)

    def restore(self, plan, manifest):
        assert not self.legacy_open and not self.vm_open
        return self.receipt('restore', plan, manifest_sha256=manifest, target_writes_closed=True,
                            services=sorted(M.SERVICES))

    def verify_closed(self, plan, fence, restore):
        return self.receipt('verify_closed', plan, fence_sha256=fence, restore_sha256=restore, readiness=True,
                            target_writes_closed=True, services=sorted(M.SERVICES))

    def activate_vm(self, plan, verified, settled):
        assert not self.legacy_open
        self.vm_open = True
        return self.receipt('activate_vm', plan, verified_sha256=verified, settlement_sha256=settled, vm_write_authority=True)

    def switch_caddy(self, plan, authority):
        assert self.vm_open and not self.legacy_open
        return self.receipt('switch_caddy', plan, authority_sha256=authority, vm_route_live=True)

    def verify_live(self, plan, authority, ingress):
        return self.receipt('verify_live', plan, authority_sha256=authority, ingress_sha256=ingress,
                            services=sorted(M.SERVICES), ready=True)

    def assert_rollback_safe(self, plan, baseline):
        return self.receipt('rollback_inspection', plan, baseline_sha256=baseline,
                            target_never_opened=not (self.vm_open or self.unsafe), legacy_route_unchanged=True,
                            target_writes_closed=not self.vm_open, prior_mutations_settled=not self.unsettled,
                            deployment_admission_reconciled=not self.unsettled)

    def restore_legacy(self, plan, baseline, safety):
        assert not self.vm_open
        self.legacy_open = True
        return self.receipt('restore_legacy', plan, baseline_sha256=baseline, safety_sha256=safety,
                            legacy_state_restored=True, deployment_admission_restored=True, ready=True)


class LegacyCutoverTransactionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name).resolve() / 'cutover.json'
        self.plan = '1' * 64

    def test_preflight_records_safe_failure_without_changing_admission(self):
        class Unhealthy(CutoverRehearsal):
            def preflight(self, plan):
                self.calls.append('preflight')
                raise TransitionBlocked('native_host_external_health_unverified')

        backend = Unhealthy(self.path)
        with self.assertRaisesRegex(TransitionBlocked, 'legacy_cutover_preflight_failed'):
            LegacyCutover(self.path, backend).run(self.plan)
        state = json.loads(self.path.read_bytes())
        self.assertEqual(state['preflight_failure'], 'native_host_external_health_unverified')
        self.assertEqual(state['receipts'], {})
        self.assertEqual(backend.calls, ['preflight'])

    def test_single_run_fences_captures_restores_and_switches_in_order(self):
        backend = CutoverRehearsal(self.path)
        operation = LegacyCutover(self.path, backend)
        result = operation.run(self.plan)
        self.assertEqual(result['phase'], 'complete')
        self.assertEqual(backend.calls, ['preflight', 'close_admission', 'block_reactivation', 'before_fence',
                                         'fence_writers', 'capture', 'restore', 'verify_closed', 'before_activation',
                                         'activate_vm', 'switch_caddy', 'verify_live'])
        self.assertTrue(backend.vm_open)
        self.assertFalse(backend.legacy_open)
        with self.assertRaisesRegex(TransitionBlocked, 'legacy_cutover_reconciliation_required'):
            operation.run(self.plan)

    def test_capture_manifest_from_real_converter_reaches_restore_adapter(self):
        from tests.test_ha_seven_capture import CaptureFixture

        capture = CaptureFixture(self.path.parent)

        class CaptureRehearsal(CutoverRehearsal):
            def capture(self, plan, fence):
                assert not self.legacy_open and not self.vm_open
                result = capture.run()
                self.manifest_pin = result['manifest_sha256']
                return self.receipt('capture', plan, fence_sha256=fence, manifest_sha256=self.manifest_pin)

            def restore(self, plan, manifest):
                assert manifest == self.manifest_pin
                source = capture.load({'manifest_sha256': manifest})
                try:
                    assert set(source.states) == set(M.SERVICES)
                    assert len(source.objects) == 2
                finally:
                    source.close()
                return super().restore(plan, manifest)

        backend = CaptureRehearsal(self.path)
        result = LegacyCutover(self.path, backend).run(capture.plan['plan_sha256'])
        self.assertEqual(result['phase'], 'complete')
        self.assertEqual(sha((capture.output / 'manifest.json').read_bytes()), backend.manifest_pin)

    def test_failed_restore_reopens_legacy_only_after_positive_closed_observation(self):
        backend = CutoverRehearsal(self.path, failure='restore')
        with self.assertRaisesRegex(TransitionBlocked, 'legacy_cutover_rolled_back'):
            LegacyCutover(self.path, backend).run(self.plan)
        self.assertEqual(backend.calls[-2:], ['rollback_inspection', 'restore_legacy'])
        self.assertTrue(backend.legacy_open)
        self.assertFalse(backend.vm_open)
        journal = json.loads(self.path.read_bytes())
        self.assertEqual(journal['failure_phase'], 'restore_intent')
        self.assertEqual(journal['phase'], 'rolled_back')

    def test_undrained_deployment_or_stale_recovery_proof_stops_transition(self):
        for fault, forbidden in (
            ('close_admission', 'fence_writers'), ('before_fence', 'fence_writers'),
            ('before_activation', 'activate_vm'),
        ):
            with self.subTest(fault=fault):
                class FaultyEvidence(CutoverRehearsal):
                    def receipt(self, stage, plan, **facts):
                        if stage == fault and fault != 'before_activation':
                            facts['admitted_deployments_settled' if fault == 'close_admission'
                                  else 'recovery_commands_settled'] = False
                        result = super().receipt(stage, plan, **facts)
                        if stage == fault == 'before_activation':
                            result['evidence_sha256'] = sha(b'before_fence')
                        return result

                path = self.path.with_name(fault + '.json')
                backend = FaultyEvidence(path)
                with self.assertRaisesRegex(TransitionBlocked, 'legacy_cutover_rolled_back'):
                    LegacyCutover(path, backend).run(self.plan)
                self.assertNotIn(forbidden, backend.calls)
                self.assertTrue(backend.legacy_open)
                self.assertFalse(backend.vm_open)

    def test_github_deployment_admission_checks_full_run_list_and_restores_owned_disable(self):
        from types import SimpleNamespace
        from ha_control.deployment_admission import DeploymentAdmission

        workflow = dict(id=123, path='.github/workflows/deploy-n100.yml', state='active')
        run = dict(id=51, workflow_id=123, status='completed')
        stale_reads = 0

        def github(args, **kwargs):
            nonlocal stale_reads
            endpoint = args[4]
            if endpoint.endswith('/disable'):
                self.assertEqual(args[5:], ('--method', 'PUT'))
                workflow['state'] = 'disabled_manually'
                stale_reads = 1
            if endpoint.endswith('/enable'):
                self.assertEqual(args[5:], ('--method', 'PUT'))
                workflow['state'] = 'active'
            if '/runs?' in endpoint:
                self.assertEqual(args[5:], ('-H', 'Cache-Control: no-cache'))
                page = int(endpoint.rpartition('=')[2])
                self.assertIn(page, (1, 2))
                rows = [dict(run, id=index) for index in range(1, 101)] if page == 1 else [dict(run, id=101)]
                result = dict(total_count=101, workflow_runs=rows)
            else:
                result = workflow
                if endpoint.endswith('/123'):
                    self.assertEqual(args[5:], ('-H', 'Cache-Control: no-cache'))
                if endpoint.endswith('/123') and stale_reads:
                    stale_reads -= 1
                    result = dict(workflow, state='active')
            return SimpleNamespace(stdout=json.dumps(result))

        admission = DeploymentAdmission('owner/repo', 123, '/usr/bin/gh', runner=github)
        with patch('ha_control.deployment_admission.time.sleep') as sleep:
            admission.close()
        sleep.assert_called_once_with(1)
        workflow['state'] = 'active'
        with self.assertRaisesRegex(TransitionBlocked, 'deployment_admission_reopened'):
            admission.assert_closed()
        workflow['state'] = 'disabled_manually'
        run['status'] = 'waiting'
        with self.assertRaisesRegex(TransitionBlocked, 'deployment_admission_runs_not_settled'):
            admission.assert_closed()
        with self.assertRaisesRegex(TransitionBlocked, 'deployment_admission_restore_ownership_required'):
            admission.restore(owned_disable=False)
        run['status'] = 'completed'
        admission.assert_closed()
        admission.restore(owned_disable=True)
        self.assertEqual(workflow['state'], 'active')

    def test_admission_retries_transient_reads_without_redriving_disable(self):
        from ha_control.deployment_admission import DeploymentAdmission

        workflow = dict(id=123, path='.github/workflows/deploy-n100.yml', state='disabled_manually')
        run = dict(id=1, workflow_id=123, status='completed')
        runner = MagicMock(side_effect=[
            SimpleNamespace(stdout=json.dumps(workflow)),
            subprocess.TimeoutExpired('gh', 20),
            SimpleNamespace(stdout=json.dumps(dict(total_count=1, workflow_runs=[run]))),
            SimpleNamespace(stdout=json.dumps(workflow)),
        ])
        policy = DeploymentAdmission('owner/repo', 123, '/usr/bin/gh', runner=runner)
        with patch('ha_control.deployment_admission.time.sleep') as sleep:
            policy.assert_closed()
        sleep.assert_called_once_with(1)
        self.assertEqual(runner.call_args_list[1], runner.call_args_list[2])

        workflow['state'] = 'active'
        runner.reset_mock(side_effect=True)
        runner.side_effect = [
            SimpleNamespace(stdout=json.dumps(workflow)),
            subprocess.TimeoutExpired('gh', 20),
        ]
        with self.assertRaisesRegex(TransitionBlocked, 'deployment_admission_command_unconfirmed'):
            policy.close()
        self.assertEqual(runner.call_count, 2)
        self.assertTrue(runner.call_args.args[0][4].endswith('/disable'))

    def test_admission_acquisition_waits_for_consistent_closure_without_replaying_disable(self):
        from ha_control.deployment_admission import DeploymentAdmission

        states = iter((
            'active', 'disabled_manually', 'active',
            'disabled_manually', 'active',
            'disabled_manually', 'disabled_manually',
            'active',
        ))
        run = dict(id=1, workflow_id=123, status='completed')

        def github(args, **kwargs):
            endpoint = args[4]
            if endpoint.endswith('/disable'):
                return SimpleNamespace(stdout=b'')
            if '/runs?' in endpoint:
                result = dict(total_count=1, workflow_runs=[run])
            else:
                result = dict(id=123, path='.github/workflows/deploy-n100.yml', state=next(states))
            return SimpleNamespace(stdout=json.dumps(result))

        runner = MagicMock(side_effect=github)
        admission = DeploymentAdmission('owner/repo', 123, '/usr/bin/gh', runner=runner)
        with patch('ha_control.deployment_admission.time.sleep') as sleep:
            admission.close()
            with self.assertRaisesRegex(TransitionBlocked, 'deployment_admission_reopened'):
                admission.assert_closed()
        self.assertEqual(sleep.call_count, 2)
        endpoints = [call.args[0][4] for call in runner.call_args_list]
        self.assertEqual(sum(endpoint.endswith('/disable') for endpoint in endpoints), 1)
        self.assertEqual(sum('/runs?' in endpoint for endpoint in endpoints), 2)

    def test_parallel_admission_scan_rejects_duplicate_across_pages(self):
        from ha_control.deployment_admission import DeploymentAdmission

        workflow = dict(id=123, path='.github/workflows/deploy-n100.yml', state='disabled_manually')
        all_workers = threading.Barrier(3, timeout=3)

        def github(args, **kwargs):
            endpoint = args[4]
            result = workflow
            if '/runs?' in endpoint:
                page = int(endpoint.rpartition('=')[2])
                if page > 1:
                    all_workers.wait()
                stop = min((page * 100) + 1, 302)
                ids = list(range((page - 1) * 100 + 1, stop))
                if page == 4:
                    ids[0] = 100
                result = dict(total_count=301, workflow_runs=[
                    dict(id=id_, workflow_id=123, status='completed') for id_ in ids
                ])
            return SimpleNamespace(stdout=json.dumps(result))

        admission = DeploymentAdmission('owner/repo', 123, '/usr/bin/gh', runner=github)
        with self.assertRaisesRegex(TransitionBlocked, 'deployment_admission_runs_unconfirmed'):
            admission.assert_closed()

    def test_native_windows_channel_rejects_mismatched_response_and_cannot_retry(self):
        from ha_control.native_cutover import WindowsReactivationSession

        child = """
import json, sys
plan, attempt = sys.argv[-3], sys.argv[-1]
print(json.dumps(dict(plan_sha256=plan, attempt_id=attempt, sequence=0, action='ready')), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request['action'] == 'verify':
        request['attempt_id'] = '0' * 64
    print(json.dumps(request), flush=True)
"""
        channel = WindowsReactivationSession((sys.executable, '-u', '-c', child))
        with channel.guard(self.plan, '2' * 64):
            self.assertRegex(channel.request('close', '3' * 64), r'^[a-f0-9]{64}$')
            with self.assertRaisesRegex(TransitionBlocked, 'legacy_native_response_unconfirmed_verify'):
                channel.request('verify', '3' * 64)
            with self.assertRaisesRegex(TransitionBlocked, 'legacy_native_guard_lost'):
                channel.request('restore', '3' * 64)
        self.assertIsNone(channel.process)

    def test_native_windows_channel_identifies_verify_timeout_and_closed_pipe(self):
        from ha_control.native_cutover import WindowsReactivationSession

        child = """
import json, sys
print(json.dumps(dict(plan_sha256=sys.argv[-3], attempt_id=sys.argv[-1],
                      sequence=0, action='ready')), flush=True)
sys.stdin.readline()
"""
        for timed_out in (False, True):
            with self.subTest(timed_out=timed_out):
                channel = WindowsReactivationSession((sys.executable, '-u', '-c', child))
                with channel.guard(self.plan, '2' * 64):
                    expected = 'legacy_native_pipe_closed_verify'
                    if timed_out:
                        expected = 'legacy_native_pipe_timeout_verify'
                        with patch('ha_control.native_cutover.selectors.DefaultSelector') as selector:
                            selector.return_value.__enter__.return_value.select.side_effect = [[True], []]
                            with self.assertRaisesRegex(TransitionBlocked, expected):
                                channel.request('verify', '3' * 64)
                    else:
                        with self.assertRaisesRegex(TransitionBlocked, expected):
                            channel.request('verify', '3' * 64)
                    self.assertFalse(channel.healthy)

    def test_native_windows_close_failure_is_bound_and_never_an_acknowledgement(self):
        from ha_control.native_cutover import WindowsReactivationSession

        child = """
import json, sys
mode = sys.argv[1]
print(json.dumps(dict(plan_sha256=sys.argv[-3], attempt_id=sys.argv[-1],
                      sequence=0, action='ready')), flush=True)
response = json.loads(sys.stdin.readline())
response['failure'] = 'legacy_recovery_cycle_in_flight'
if mode == 'wrong_nonce':
    response['nonce'] = '0' * 64
elif mode == 'unstructured':
    response['failure'] = 'private path'
elif mode == 'null':
    response['failure'] = None
print(json.dumps(response), flush=True)
"""
        outcomes = (
            ('bound', 'close', 'legacy_native_windows_legacy_recovery_cycle_in_flight_close'),
            ('wrong_nonce', 'close', 'legacy_native_response_unconfirmed_close'),
            ('unstructured', 'close', 'legacy_native_response_unconfirmed_close'),
            ('null', 'close', 'legacy_native_response_unconfirmed_close'),
            ('bound', 'verify', 'legacy_native_response_unconfirmed_verify'),
        )
        for mode, action, error in outcomes:
            with self.subTest(mode=mode, action=action):
                channel = WindowsReactivationSession((sys.executable, '-u', '-c', child, mode))
                with channel.guard(self.plan, '2' * 64):
                    with self.assertRaisesRegex(TransitionBlocked, error + '$'):
                        channel.request(action, '3' * 64)
                    self.assertFalse(channel.healthy)

    def test_wsl_bootstrap_observes_native_marker_and_rejects_legacy_collision(self):
        from ha_control.native_cutover import WslBootstrapAdmission

        root = self.path.parent
        (root / 'scripts').mkdir()
        (root / 'data').mkdir()
        content = b'bootstrap'
        (root / 'scripts/windows-bootstrap.sh').write_bytes(content)
        guard = WslBootstrapAdmission(sha(content))
        guard.root = root
        guard.authority = root / 'protected'
        guard.authority.mkdir()
        marker = guard.authority / 'ha-controller-owner.json'
        old = root / 'data/ha-controller-owner.json'
        with guard.guard():
            with self.assertRaisesRegex(TransitionBlocked, 'legacy_bootstrap_owner_missing'):
                guard.assert_visible()
            marker.write_bytes(b'{"published_by":"Windows"}')
            guard.assert_visible()
            with self.assertRaisesRegex(TransitionBlocked, 'legacy_bootstrap_owner_not_released'):
                guard.assert_released()
            old.symlink_to(root / 'missing')
            with self.assertRaisesRegex(TransitionBlocked, 'legacy_bootstrap_owner_changed'):
                guard.assert_visible()
            old.unlink()
            marker.unlink()
            guard.assert_released()

    def test_native_cutover_cannot_fence_wsl_without_direct_recovery_exclusion(self):
        from ha_control.native_cutover import NativeCutover

        class Admission:
            closed = False

            def close(self):
                self.closed = True

            def assert_closed(self):
                if not self.closed:
                    raise AssertionError('deployment admission is open')

            def restore(self, *, owned_disable):
                if not owned_disable:
                    raise AssertionError('unowned disable')
                self.closed = False

        class Windows:
            healthy = True

            @contextmanager
            def guard(self, plan, attempt):
                yield

            def request(self, action, proof):
                raise AssertionError('Windows mutation must remain unreachable')

        class Applications(CutoverRehearsal):
            def assert_direct_recovery_excluded(self, plan, proof):
                raise TransitionBlocked('direct_recovery_exclusion_missing')

            def restore_legacy_runtime(self, plan, baseline, safety, linux_touched):
                if linux_touched:
                    raise AssertionError('unexpected WSL mutation')
                return sha(b'legacy-runtime-unchanged')

        admission = Admission()
        apps = Applications(self.path)
        with self.assertRaisesRegex(TransitionBlocked, 'legacy_cutover_rolled_back'):
            LegacyCutover(self.path, NativeCutover(Windows(), admission, apps)).run(self.plan)
        self.assertNotIn('fence_writers', apps.calls)
        self.assertTrue(apps.legacy_open)
        self.assertFalse(admission.closed)

    def test_unknown_target_or_activation_never_reopens_legacy(self):
        for failure, unsafe, unsettled, phase in (
            ('restore', True, False, 'rollback_reconciliation_required'),
            ('capture', False, True, 'rollback_reconciliation_required'),
            ('activate_vm', False, False, 'forward_recovery_required'),
            ('switch_caddy', False, False, 'forward_recovery_required'),
        ):
            with self.subTest(failure=failure):
                path = self.path.with_name(failure + '.json')
                backend = CutoverRehearsal(path, failure=failure, unsafe=unsafe, unsettled=unsettled)
                with self.assertRaisesRegex(TransitionBlocked, 'legacy_cutover_' + phase):
                    LegacyCutover(path, backend).run(self.plan)
                self.assertFalse(backend.legacy_open)
                self.assertNotIn('restore_legacy', backend.calls)
                self.assertEqual(json.loads(path.read_bytes())['phase'], phase)

if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--postgres-integration':
        print(json.dumps(postgres_integration(sys.argv[2]),sort_keys=True))
    else:unittest.main()
