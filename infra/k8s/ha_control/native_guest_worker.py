"""One fixed, root-owned guest command for the Mac SSH bridge.

The launcher authenticates the staged source and the SSH dispatcher authenticates
the guest. The host coordinator owns WSL fencing. This worker owns guest-local
mutation exclusion, evidence, and observations. Activation is a separate stage.
"""

from contextlib import contextmanager
from dataclasses import asdict, fields
from datetime import timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

from ha_runtime.config import SERVICES

from . import frozen_ingress, native_guest_restore as restore, native_guest_runtime as runtime
from . import native_guest_activation as activation, native_guest_live as live
from .legacy_writers import TransitionBlocked, _need
from .native_stdio_bridge import (
    ACTIVATION_BOUNDARY_SECONDS, Binding, BridgeRejected, FRAME_BYTES, MAX_REQUESTS, Receipt, Request, Stdio, VERSION, _args, _bytes, _digest,
    _hash, _matches, _pairs, _reject_constant,
)


ROOT = Path("/var/lib/n100-ha/cutover")
LOCK = ROOT / "mutation.lock"
VERBS = {
    "preflight", "begin_receive", "finish_receive", "restore", "prepare_runtime", "verify_closed",
    "assert_activation_closed", "activate", "assert_live", "observe_restore", "observe_rollback",
}
HANDOFF = "activation-handoff.json"
EMPTY = "0" * 64
RESTORE_DYNAMIC = {"boot_sha256", "installation_id", "plan_sha256", "manifest_sha256"}
RUNTIME_DYNAMIC = {
    "node", "boot_sha256", "installation_id", "protected_inventory_sha256", "plan_sha256", "manifest_sha256",
    "frozen_transfer_sha256", "restore_receipt_sha256", "attempt_id", "system_id", "cluster",
    "postgres_id", "retained_postgres_id",
}
STANDBY = """
SELECT json_build_array(session_user, current_database(), current_setting('cluster_name'),
    pg_is_in_recovery(), system_identifier::text, current_setting('transaction_read_only'),
    (SELECT count(*) FROM pg_namespace WHERE nspname IN ('ha','ha_effects') OR left(nspname,2)='s_'),
    (SELECT count(*) FROM pg_stat_wal_receiver WHERE status='streaming' AND sender_host='ha-a'
        AND sender_port=5432 AND latest_end_lsn IS NOT NULL),
    pg_last_wal_replay_lsn()::text)
FROM pg_control_system()
"""
NEVER_OPENED = """
SELECT json_build_array(cluster, epoch, revision, primary_node, partner, timeline, serving, worker, mode,
    writes_open, history_safe, expires_at < clock_timestamp())
FROM ha.control WHERE singleton=1
"""


def _names(dataclass):
    return {field.name for field in fields(dataclass)}


def _read_json(path):
    raw = runtime._read(path, 0, {0o400, 0o600}, limit=65536)
    value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    _need(type(value) is dict, "guest_worker_local_record_invalid")
    return raw, value


def _scope(request):
    return {"version": VERSION, "binding": request.binding.fields(), "node": request.node,
            "boot_sha256": request.binding.boot(request.node), "verb": request.verb,
            "sequence": request.sequence, "nonce": request.nonce}


def read_request(stream):
    raw = stream.readline(FRAME_BYTES + 1)
    _need(0 < len(raw) <= FRAME_BYTES and raw.endswith(b"\n"), "guest_worker_request_size")
    value = json.loads(raw.decode("ascii"), object_pairs_hook=_pairs, parse_constant=_reject_constant)
    _need(type(value) is dict and _bytes(value) == raw and type(value["binding"]) is dict,
          "guest_worker_request_encoding")
    binding = Binding(**value["binding"])
    binding.fields()
    request = Request(binding, value["node"], value["verb"], value["sequence"], value["nonce"], value["args"])
    _need(type(request.sequence) is int and 0 < request.sequence <= MAX_REQUESTS and _hash(request.nonce)
          and type(request.verb) is str and request.verb in VERBS, "guest_worker_request_scope")
    _args(request.verb, request.node, request.args)
    _need(_matches(value, {**_scope(request), "args": request.args}), "guest_worker_request_scope")
    if request.verb != "activate":
        _need(not stream.read(1), "guest_worker_request_size")
    return request


@contextmanager
def mutation_lock(wait=0):
    """All guest mutators must use this installed lock, including activation."""
    runtime._directory(ROOT, 0)
    before = LOCK.lstat()
    _need(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
          and stat.S_IMODE(before.st_mode) == 0o600, "guest_worker_lock_untrusted")
    descriptor = os.open(LOCK, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        current = os.fstat(descriptor)
        _need((current.st_dev, current.st_ino) == (before.st_dev, before.st_ino), "guest_worker_lock_changed")
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                _need(time.monotonic() < deadline, "guest_worker_mutation_unsettled")
                time.sleep(0.05)
        yield descriptor
    finally:
        # A detached receiver inherits the same file description.
        os.close(descriptor)


class GuestWorker:
    def __init__(self, request):
        _need(os.geteuid() == 0, "guest_worker_root_required")
        self.request = request
        self.args = request.args
        self.node = request.node
        self.boot = request.binding.boot(request.node)
        self.stage = ROOT / request.binding.attempt_id
        self.restore_stage = self.stage / "restore"
        self.received = self.stage / "received"
        runtime._directory(self.stage, 0)
        raw, self.input = _read_json(self.stage / "worker-input.json")
        self.input_digest = _digest(raw)
        _need(set(self.input) == {"schema_version", "binding", "node", "installation_id", "restore", "runtime"}
              and type(self.input["schema_version"]) is int and self.input["schema_version"] == 1
              and self.input["binding"] == request.binding.fields() and self.input["node"] == self.node
              and _hash(self.input["installation_id"])
              and type(self.input["restore"]) is dict
              and set(self.input["restore"]) | {"retained_postgres_id"} == _names(restore.RestorePins) - RESTORE_DYNAMIC
              and type(self.input["runtime"]) is dict and set(self.input["runtime"]) == _names(runtime.RuntimePins) - RUNTIME_DYNAMIC,
              "guest_worker_installed_scope_mismatch")
        self.installation = self.input["installation_id"]
        self.identity()

    def identity(self):
        _need(_digest(restore.BOOT_ID.read_bytes()) == self.boot, "guest_worker_boot_changed")
        _, value = _read_json(restore.INSTALLATION)
        _need(value["installation_id"] == self.installation and value["node"] == self.node,
              "guest_worker_installation_changed")
        raw, _ = _read_json(self.stage / "worker-input.json")
        _need(_digest(raw) == self.input_digest, "guest_worker_inputs_changed")

    def restore_pins(self, manifest=EMPTY):
        pins = restore.RestorePins(**self.input["restore"], boot_sha256=self.boot,
                                   installation_id=self.installation, plan_sha256=self.request.binding.plan_sha256,
                                   manifest_sha256=manifest)
        pins.validate()
        return pins

    def runtime_pins(self, manifest=EMPTY, transfer=EMPTY, restored=EMPTY, inventory=None):
        db = self.restore_pins(manifest)
        pins = runtime.RuntimePins(**self.input["runtime"], node=self.node, boot_sha256=self.boot,
                                   installation_id=self.installation, plan_sha256=db.plan_sha256,
                                   manifest_sha256=manifest, frozen_transfer_sha256=transfer,
                                   restore_receipt_sha256=restored, attempt_id=self.request.binding.attempt_id,
                                   system_id=db.system_id, cluster=db.cluster,
                                   postgres_id=db.postgres_id, retained_postgres_id=db.retained_postgres_id,
                                   protected_inventory_sha256=inventory or db.inventory_sha256)
        pins.validate()
        _need(pins.release["source_commit"] == db.source_commit, "guest_worker_release_mismatch")
        return pins

    def _record(self, name, value):
        raw = _bytes(value)
        runtime._write(self.stage / name, raw)
        return _digest(raw)

    def _proof(self, verb, expected=None):
        raw, value = _read_json(self.stage / f"worker-{verb}.json")
        _need(value["node"] == self.node and value["binding"] == self.request.binding.fields()
              and value["worker_input_sha256"] == self.input_digest and value["verb"] == verb
              and (expected is None or _digest(raw) == expected), "guest_worker_prior_proof_unbound")
        return _digest(raw), value["evidence"]

    def _emit(self, evidence, *, status="PASS", save=True):
        value = {**_scope(self.request), "worker_input_sha256": self.input_digest, "evidence": evidence}
        digest = self._record(f"worker-{self.request.verb}.json", value) if save else _digest(_bytes(value))
        return Receipt(status, digest)

    def _intent(self, **evidence):
        value = {**_scope(self.request), "worker_input_sha256": self.input_digest,
                 "args_sha256": _digest(_bytes(self.args)), **evidence}
        return self._record(f"worker-{self.request.verb}.intent.json", value)

    def _assert_no_activation(self, *, allow_handoff=False):
        authorities = [runtime.ROOT / "apps" / service / "activation.json" for service in SERVICES]
        authorities.extend([runtime.ROOT / "broker" / "authority.json", self.stage / "runtime-activation-intent.json",
                            self.stage / "runtime-activation.json", self.stage / "worker-activate.intent.json",
                            self.stage / "worker-activate.json"])
        if not allow_handoff:
            authorities.append(self.stage / HANDOFF)
        _need(not any(os.path.lexists(path) for path in authorities), "guest_worker_activation_unsettled")

    def _pg(self, pins, *, restored):
        self.identity()
        self._assert_no_activation()
        if self.node == "A":
            barrier = restore._assert_pg(pins, restored=restored)
            if not restored:
                _need(restore._pg(pins, restore.FRESH_CATALOG) is True, "guest_worker_postgres_not_fresh")
            return barrier
        observation = restore._pg(pins, STANDBY)
        expected = ["postgres", "ha", "ha_B", True, pins.system_id, "on"]
        _need(type(observation) is list and len(observation) == 9 and observation[:6] == expected
              and type(observation[6]) is int and (observation[6] > 2 if restored else observation[6] == 0)
              and observation[7] == 1 and restore._lsn(observation[8]) > 0, "guest_worker_standby_mismatch")
        if restored:
            closed = [True, pins.cluster, 14, 14, sorted(SERVICES), 0, 1, True]
            _need(restore._pg(pins, restore.CLOSED_STATE) == closed, "guest_worker_standby_not_closed")
        else:
            _need(restore._pg(pins, restore.FRESH_CATALOG) is True, "guest_worker_postgres_not_fresh")
        return _digest(_bytes({"replay_lsn": observation[8], "system_id": pins.system_id}))

    def _fresh(self):
        pins = self.restore_pins()
        restore._assert_inventory(pins)
        if self.node == "A":
            restore._identity(pins)
        self._pg(pins, restored=False)
        return runtime.preflight_runtime(self.runtime_pins(), self.stage)

    def preflight(self):
        pins = self.restore_pins()
        _need(self.args["target_system_id_sha256"] == _digest(pins.system_id.encode()),
              "guest_worker_target_system_unbound")
        if self.node == "A":
            runtime._directory(restore.JOURNAL, 0)
            _need(not any(os.path.lexists(restore.JOURNAL / name) for name in
                          ("migration-intent.json", "migration-pass.json")), "guest_worker_prior_restore_unsettled")
            runtime._directory(self.restore_stage, 0)
            inputs, receipts = self.restore_stage / "inputs", self.restore_stage / "receipts"
            for directory in (inputs, receipts):
                restore._directory(directory, pins.uid)
                _need(directory.lstat().st_gid == pins.gid, "guest_worker_restore_input_unbound")
            _need(not any(receipts.iterdir()) and not os.path.lexists(self.restore_stage / "frozen"),
                  "guest_worker_prior_restore_unsettled")
            restore._read(inputs / "config.json", pins.uid, digest=pins.config_sha256, limit=65536)
        observed = self._fresh()
        _need(observed["status"] == "PREFLIGHT_PASS" and observed["frozen_socket_released"] is True,
              "guest_worker_prefight_unsettled")
        return self._emit(observed)

    @contextmanager
    def _receiver_guard(self, pins):
        restore._assert_inventory(pins)
        self._pg(pins, restored=False)
        yield
        restore._assert_inventory(pins)
        self._pg(pins, restored=False)

    def _receive_child(self, descriptor, manifest, nonce, size, intent):
        try:
            os.setsid()
            null = os.open("/dev/null", os.O_RDWR)
            for number in (0, 1, 2):
                os.dup2(null, number)
            for number in os.listdir("/proc/self/fd"):
                if int(number) > 2 and int(number) != descriptor:
                    try:
                        os.close(int(number))
                    except OSError:
                        pass
            pins = self.restore_pins(manifest)
            result = frozen_ingress.receive_once(self.received, manifest, nonce, max_bytes=size,
                                                  target_guard=self._receiver_guard(pins))
            _need(result == manifest and not os.path.lexists(frozen_ingress.SOCKET.parent),
                  "guest_worker_receive_unconfirmed")
            self._record("worker-receiver-outcome.json", {"status": "PASS", "manifest_sha256": manifest,
                                                          "receiver_intent_sha256": intent})
        except Exception:
            try:
                self._record("worker-receiver-outcome.json", {"status": "FAILED", "manifest_sha256": manifest,
                                                              "receiver_intent_sha256": intent})
            except Exception:
                pass
        finally:
            os._exit(0)

    def begin_receive(self, descriptor):
        self._proof("preflight")
        pins = self.restore_pins(self.args["manifest_sha256"])
        restore._assert_inventory(pins)
        self._pg(pins, restored=False)
        _need(not os.path.lexists(self.received) and not os.path.lexists(frozen_ingress.SOCKET.parent),
              "guest_worker_prior_receive_unsettled")
        intent = self._intent()
        nonce = self.args["transfer_nonce"]
        pid = os.fork()
        if pid == 0:
            self._receive_child(descriptor, pins.manifest_sha256, nonce, self.args["max_bytes"], intent)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            _need(os.waitpid(pid, os.WNOHANG)[0] == 0, "guest_worker_receive_unconfirmed")
            try:
                info = frozen_ingress.SOCKET.lstat()
                parent = frozen_ingress.SOCKET.parent.lstat()
                if (stat.S_ISSOCK(info.st_mode) and info.st_uid == 0 and info.st_gid == 10004
                        and stat.S_IMODE(info.st_mode) == 0o660 and stat.S_ISDIR(parent.st_mode)
                        and parent.st_uid == 0 and parent.st_gid == 10004 and stat.S_IMODE(parent.st_mode) == 0o750):
                    return self._emit({"manifest_sha256": pins.manifest_sha256, "fence_sha256": self.args["fence_sha256"],
                                       "receiver_intent_sha256": intent, "nonce_sha256": _digest(nonce.encode()),
                                       "socket_identity_sha256": _digest(_bytes([info.st_dev, info.st_ino]))})
            except FileNotFoundError:
                pass
            time.sleep(0.05)
        raise TransitionBlocked("guest_worker_receive_unconfirmed")

    def finish_receive(self):
        receiver, evidence = self._proof("begin_receive", self.args["receiver_sha256"])
        manifest = self.args["manifest_sha256"]
        _, outcome = _read_json(self.stage / "worker-receiver-outcome.json")
        transfer = {"stage": "transfer", **self.request.binding.fields(), "manifest": manifest,
                    "receiver": receiver, "nonce_sha256": evidence["nonce_sha256"]}
        _need(outcome == {"status": "PASS", "manifest_sha256": manifest,
                          "receiver_intent_sha256": evidence["receiver_intent_sha256"]}
              and evidence["manifest_sha256"] == manifest
              and _digest(_bytes(transfer).rstrip(b"\n")) == self.args["transfer_proof_sha256"]
              and not os.path.lexists(frozen_ingress.SOCKET.parent), "guest_worker_receive_unconfirmed")
        raw = restore._read(self.received / "manifest.json", 0, digest=manifest)
        restore._validate_manifest(self.restore_pins(manifest), json.loads(raw))
        return self._emit({"manifest_sha256": manifest, "receiver_sha256": receiver,
                           "transfer_proof_sha256": self.args["transfer_proof_sha256"]})

    def restore_data(self):
        self._proof("preflight", self.args["target_receipt_sha256"])
        _, transfer = self._proof("finish_receive", self.args["transfer_receipt_sha256"])
        pins = self.restore_pins(self.args["manifest_sha256"])
        _need(transfer["manifest_sha256"] == pins.manifest_sha256, "guest_worker_restore_transfer_unbound")
        self._intent()
        restore.stage_frozen(pins, self.received, self.restore_stage)
        result = restore.restore_closed(pins, self.restore_stage)
        _need(result["status"] == "PASS" and result["manifest_sha256"] == pins.manifest_sha256
              and result["database_and_apps_closed"] is True and result["writer_authority_issued"] is False
              and result["services"] == sorted(SERVICES), "guest_worker_restore_unconfirmed")
        return self._emit(result)

    def _inventory_for_preparation(self, pins, *, prepared=False, restore_receipt=None):
        if self.node == "B":
            return pins.inventory_sha256
        _, proof = self._proof("restore", restore_receipt or self.args.get("restore_receipt_sha256"))
        _, passed = _read_json(restore.JOURNAL / "migration-pass.json")
        _need(passed == proof and passed["manifest_sha256"] == pins.manifest_sha256,
              "guest_worker_restore_unconfirmed")
        identifier = passed["container_id"]
        _need(_hash(identifier), "guest_worker_restore_unconfirmed")
        objects = restore._inventory()
        migration = objects.pop(identifier)
        if prepared:
            objects = {key: item for key, item in objects.items()
                       if (item["Config"]["Labels"] or {}).get(runtime.ATTEMPT_LABEL) != self.request.binding.attempt_id}
        _need(restore.inventory_sha256(objects) == pins.inventory_sha256 and migration["State"]["ExitCode"] == 0
              and migration["RestartCount"] == 0, "guest_worker_restore_container_unsettled")
        inputs, frozen, receipts, _, _ = restore._prestage(pins, self.restore_stage, new_receipts=False)
        restore._assert_migration_container(migration, identifier, restore._intent(pins)["container"], pins,
                                            restore._restore_mounts(inputs, frozen, receipts), state="exited")
        objects[identifier] = migration
        return restore.inventory_sha256(objects)

    def prepare_runtime(self):
        self._proof("preflight", self.args["target_receipt_sha256"])
        manifest, transfer, restored = (self.args[key] for key in
                                        ("manifest_sha256", "transfer_receipt_sha256", "restore_receipt_sha256"))
        db = self.restore_pins(manifest)
        if self.node == "A":
            self._proof("finish_receive", transfer)
        inventory = self._inventory_for_preparation(db)
        pins = self.runtime_pins(manifest, transfer, restored, inventory)
        self._intent()

        def guard():
            # WSL ownership and cross-guest receipt references are checked by
            # the held coordinator and authenticated Mac bridge. This worker
            # rechecks the local facts instead of treating those hashes as SQL proof.
            self._pg(db, restored=True)
            _need(not os.path.lexists(frozen_ingress.SOCKET), "guest_worker_receive_unconfirmed")
            return {"node": self.node, "installation_id": self.installation, "attempt_id": pins.attempt_id,
                    "plan_sha256": pins.plan_sha256, "manifest_sha256": manifest, "frozen_transfer_sha256": transfer,
                    "restore_receipt_sha256": restored, "legacy_writers_closed": True, "target_writes_closed": True,
                    "frozen_transfer_closed": True}

        result = runtime.prepare_runtime(pins, self.stage, assert_guard=guard)
        raw, persisted = _read_json(self.stage / "runtime-prepared.json")
        _need(result == persisted and result["status"] == "PREPARED" and result["applications_started"] is False
              and result["writer_authority_issued"] is False and set(result["containers"]) == SERVICES | {"ha-broker"},
              "guest_worker_preparation_unconfirmed")
        return self._emit({"manifest_sha256": manifest, "transfer_receipt_sha256": transfer,
                           "restore_receipt_sha256": restored, "runtime_receipt_sha256": _digest(raw)})

    def _prepared_pins(self, manifest, restored=None, *, expected=None):
        _, proof = self._proof("prepare_runtime", expected)
        db = self.restore_pins(proof["manifest_sha256"])
        _need(manifest == db.manifest_sha256 and (restored is None or restored == proof["restore_receipt_sha256"]),
              "guest_worker_preparation_unbound")
        pins = self.runtime_pins(db.manifest_sha256, proof["transfer_receipt_sha256"], proof["restore_receipt_sha256"],
                                 self._inventory_for_preparation(db, prepared=True,
                                                                 restore_receipt=proof["restore_receipt_sha256"]))
        return db, pins, proof

    def _inspect_stopped(self, pins, proof):
        receipt = runtime._inspect_prepared(pins, self.stage, self.stage / "runtime-intent.json",
                                            self.stage / "runtime-prepared.json")
        _need(receipt == proof["runtime_receipt_sha256"], "guest_worker_preparation_unconfirmed")
        return receipt

    def _prepared(self, expected=None):
        db, pins, proof = self._prepared_pins(self.args.get("manifest_sha256"),
                                              self.args.get("restore_receipt_sha256"), expected=expected)
        receipt = self._inspect_stopped(pins, proof)
        return receipt, self._pg(db, restored=True)

    def verify_closed(self):
        receipt, postgres = self._prepared(self.args["prepared_receipt_sha256"])
        return self._emit({"manifest_sha256": self.args["manifest_sha256"], "runtime_receipt_sha256": receipt,
                           "closed_postgres_sha256": postgres})

    def assert_activation_closed(self):
        _need(self.node == "A", "guest_worker_activation_a_required")
        self._assert_no_activation()
        manifest = self.args["manifest_sha256"]
        restored = self.args["restore_receipt_sha256"]
        closed_a, closed_b = self.args["closed_a_sha256"], self.args["closed_b_sha256"]
        _, earlier = self._proof("verify_closed", closed_a)
        db, pins, proof = self._prepared_pins(manifest, restored)
        receipt = self._inspect_stopped(pins, proof)
        postgres = self._pg(db, restored=True)
        expected = [db.cluster, 1, 1, "A", "B", 1, ["A"], "A", "AS", False, False, True]
        _need(_bytes(restore._pg(db, NEVER_OPENED)) == _bytes(expected)
              and earlier["manifest_sha256"] == manifest and earlier["runtime_receipt_sha256"] == receipt
              and closed_a != closed_b, "guest_worker_activation_preflight_unconfirmed")
        return self._emit({"manifest_sha256": manifest, "restore_receipt_sha256": restored,
                           "runtime_receipt_sha256": receipt, "closed_a_sha256": closed_a, "closed_b_sha256": closed_b,
                           "closed_postgres_sha256": postgres})

    def _handoff(self, manifest, check, settlement):
        _, checked = self._proof("assert_activation_closed", check)
        raw = runtime._read(self.stage / HANDOFF, 0, {0o600}, limit=65536)
        handoff = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_reject_constant)
        _need(type(handoff) is dict and type(handoff.get("activation_pins")) is dict
              and handoff["activation_pins"].keys() == _names(activation.ActivationPins),
              "guest_worker_activation_handoff_unbound")
        pins = activation.ActivationPins(**handoff["activation_pins"])
        pins.validate()
        expected = {"schema_version": 1, "binding": self.request.binding.fields(), "node": "A",
                    "installation_id": self.installation, "manifest_sha256": manifest,
                    "settlement_sha256": settlement, "activation_check_sha256": check,
                    "prepared_receipt_sha256": checked["runtime_receipt_sha256"], "activation_pins": asdict(pins)}
        _need(self.node == "A" and raw == _bytes(expected) and checked["manifest_sha256"] == manifest
              and pins.primary_prepared_closed_sha256 == checked["closed_a_sha256"]
              and pins.standby_prepared_closed_sha256 == checked["closed_b_sha256"]
              and pins.primary_runtime_receipt_sha256 == checked["runtime_receipt_sha256"],
              "guest_worker_activation_handoff_unbound")
        return raw, checked, pins

    def activate(self, assert_windows_current):
        _need(callable(assert_windows_current), "guest_worker_live_windows_required")
        self._assert_no_activation(allow_handoff=True)
        manifest, check, settlement = (self.args[key] for key in
                                       ("manifest_sha256", "activation_check_sha256", "settlement_sha256"))
        handoff, checked, activation_pins = self._handoff(manifest, check, settlement)
        handoff_sha256 = _digest(handoff)
        db, pins, proof = self._prepared_pins(manifest, checked["restore_receipt_sha256"])
        _need(self._inspect_stopped(pins, proof) == activation_pins.primary_runtime_receipt_sha256,
              "guest_worker_activation_preparation_unbound")
        intent_sha256 = self._intent(handoff_sha256=handoff_sha256)

        def assert_lease(challenge, boundary):
            self.identity()
            _need(runtime._read(self.stage / HANDOFF, 0, {0o600}, limit=65536) == handoff,
                  "guest_worker_activation_handoff_changed")
            observed, sampled, expires, _ = live._lease(pins, db)
            windows_proof = assert_windows_current(challenge, boundary)
            _need(_hash(windows_proof), "guest_worker_live_windows_unconfirmed")
            facts = {**runtime._runtime_identity(pins), **asdict(activation_pins), "challenge": challenge,
                     "boundary": boundary, "coordinator_intent_durable": True, "initial_postcommit_verified": True,
                     "legacy_writers_closed": True, "standby_runtime_closed": True, "runtime_mutations_excluded": True,
                     "epoch": 1, "timeline": 1, "revision": 2, "primary": "A", "partner": "B", "writes_open": True}
            observed_at, expires_at = (point.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                                       for point in (sampled, expires))
            lease_sha256 = _digest(_bytes({"handoff_sha256": handoff_sha256, "observation": observed,
                                          "windows_proof_sha256": windows_proof, **facts}))
            return {**facts, "observed_at": observed_at, "expires_at": expires_at, "lease_sha256": lease_sha256}

        result = activation.activate_prepared_a(pins, activation_pins, self.stage, assert_lease=assert_lease)
        _need(type(result) is dict and result.get("status") == "STARTED", "guest_worker_activation_unconfirmed")
        raw, persisted = _read_json(self.stage / "runtime-activation.json")
        _need(persisted == result and result["automatic_retry_permitted"] is False
              and runtime._read(self.stage / HANDOFF, 0, {0o600}, limit=65536) == handoff,
              "guest_worker_activation_unconfirmed")
        return self._emit({"manifest_sha256": manifest, "activation_check_sha256": check,
                           "settlement_sha256": settlement, "handoff_sha256": handoff_sha256,
                           "worker_intent_sha256": intent_sha256,
                           "native_activation_receipt_sha256": _digest(raw)})

    def assert_live(self):
        authority = self.args["authority_sha256"]
        _, activated = self._proof("activate", authority)
        manifest = activated["manifest_sha256"]
        handoff, checked, activation_pins = self._handoff(
            manifest, activated["activation_check_sha256"], activated["settlement_sha256"])
        intent, _ = _read_json(self.stage / "worker-activate.intent.json")
        _need(_digest(handoff) == activated["handoff_sha256"]
              and _digest(intent) == activated["worker_intent_sha256"], "guest_worker_activation_handoff_changed")
        db, pins, _ = self._prepared_pins(manifest, checked["restore_receipt_sha256"])
        observed = live.observe_live_a(
            pins, activation_pins, db, self.stage, activation_receipt_sha256=activated["native_activation_receipt_sha256"],
            authority_sha256=authority, windows_generation=self.args["windows_generation"],
            windows_generation_sha256=self.args["windows_generation_sha256"],
            live_lease_sha256=self.args["live_lease_sha256"], live_authority_sha256=self.args["live_authority_sha256"],
        )
        _need(_hash(observed), "guest_worker_live_observation_unconfirmed")
        return self._emit({"guest_live_sha256": observed, "authority_sha256": authority}, save=False)

    def _receiver_settled(self, manifest):
        intent = self.stage / "worker-begin_receive.intent.json"
        _need(not os.path.lexists(frozen_ingress.SOCKET), "guest_worker_receive_unconfirmed")
        if os.path.lexists(intent):
            raw, _ = _read_json(intent)
            _, observed = _read_json(self.stage / "worker-receiver-outcome.json")
            _need(observed["receiver_intent_sha256"] == _digest(raw) and observed["manifest_sha256"] == manifest
                  and observed["status"] in ("PASS", "FAILED")
                  and (observed["status"] == "PASS" or not os.path.lexists(self.received)),
                  "guest_worker_receive_unconfirmed")
        if not os.path.lexists(self.stage / "worker-prepare_runtime.intent.json"):
            _need(not os.path.lexists(frozen_ingress.SOCKET.parent), "guest_worker_receive_unconfirmed")

    def observe_rollback(self):
        manifest = self.args["manifest_sha256"]
        self._assert_no_activation()
        self._receiver_settled(manifest)
        if os.path.lexists(self.stage / "worker-prepare_runtime.intent.json"):
            receipt, postgres = self._prepared()
            return self._emit({"runtime_receipt_sha256": receipt, "closed_postgres_sha256": postgres},
                              status="COMMITTED_CLOSED", save=False)
        _need(not any(os.path.lexists(self.stage / name) for name in
                      ("runtime-intent.json", "runtime-prepared.json", "apps.json", "broker.json")),
              "guest_worker_preparation_unsettled")
        pins = self.restore_pins(manifest or EMPTY)
        if self.node == "A" and os.path.lexists(restore.JOURNAL / "migration-intent.json"):
            result = restore.observe_closed_outcome(pins, self.restore_stage)
            status = result["status"]
            _need(status in ("CLEAN_FAILED", "COMMITTED_CLOSED"), "guest_worker_restore_unsettled")
            return self._emit(result, status=status, save=False)
        restore._assert_inventory(pins)
        # Standby independently observes the replayed catalog. The caller
        # accepts COMMITTED_CLOSED only when both nodes report it.
        if self.node == "B" and manifest is not None:
            observation = restore._pg(pins, STANDBY)
            if type(observation) is list and len(observation) == 9 and type(observation[6]) is int and observation[6] > 2:
                replay = self._pg(pins, restored=True)
                runtime.preflight_runtime(self.runtime_pins(), self.stage)
                return self._emit({"closed_postgres_sha256": replay}, status="COMMITTED_CLOSED", save=False)
        self._fresh()
        return self._emit({"fresh_inventory_sha256": pins.inventory_sha256}, status="CLEAN", save=False)

    def observe_restore(self):
        raw, _ = _read_json(self.stage / "worker-restore.intent.json")
        _need(_digest(raw) == self.args["restore_intent_sha256"], "guest_worker_restore_intent_unbound")
        return self.observe_rollback()

    def dispatch(self, descriptor, assert_windows_current=None):
        if self.request.verb == "begin_receive":
            return self.begin_receive(descriptor)
        if self.request.verb == "activate":
            return self.activate(assert_windows_current)
        handlers = {"preflight": self.preflight, "finish_receive": self.finish_receive, "restore": self.restore_data,
                    "prepare_runtime": self.prepare_runtime, "verify_closed": self.verify_closed,
                    "assert_activation_closed": self.assert_activation_closed,
                    "assert_live": self.assert_live,
                    "observe_restore": self.observe_restore, "observe_rollback": self.observe_rollback}
        return handlers[self.request.verb]()


def handle(request, assert_windows_current=None):
    try:
        _need(type(request) is Request and request.verb in VERBS, "guest_worker_request_scope")
        _args(request.verb, request.node, request.args)
        with mutation_lock(wait=15 if request.verb == "finish_receive" else 0) as descriptor:
            return GuestWorker(request).dispatch(descriptor, assert_windows_current)
    except (TransitionBlocked, BridgeRejected, OSError, KeyError, TypeError, ValueError, subprocess.TimeoutExpired):
        return Receipt("UNKNOWN")


def main():
    try:
        signal.alarm(10)
        request = read_request(sys.stdin.buffer)
    except (TransitionBlocked, BridgeRejected, OSError, KeyError, TypeError, ValueError, RecursionError):
        return 1
    finally:
        signal.alarm(0)
    if request.verb == "activate":
        channel = Stdio(sys.stdin.fileno(), sys.stdout.fileno())

        def assert_windows_current(challenge, boundary):
            deadline = time.monotonic() + ACTIVATION_BOUNDARY_SECONDS
            fields = {"request_nonce": request.nonce, "challenge": challenge, "boundary": boundary}
            channel.write({"kind": "activation_boundary", **fields}, deadline)
            reply = channel.read(deadline)
            proof = reply.get("windows_proof_sha256")
            _need(_hash(proof) and _matches(reply, {"kind": "activation_boundary_current", **fields,
                                                   "windows_proof_sha256": proof}),
                  "guest_worker_live_windows_unconfirmed")
            return proof

        result = handle(request, assert_windows_current)
        channel.write({**_scope(request), **result.fields(request.verb)}, time.monotonic() + ACTIVATION_BOUNDARY_SECONDS)
        return 0
    result = handle(request)
    raw = _bytes({**_scope(request), **result.fields(request.verb)})
    _need(len(raw) <= FRAME_BYTES, "guest_worker_response_size")
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
