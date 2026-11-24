"""One fixed, root-owned guest command for the Mac SSH bridge.

The launcher authenticates the staged source and the SSH dispatcher authenticates
the guest. The host coordinator owns WSL fencing. This worker owns guest-local
mutation exclusion, evidence, and observations. Activation is a separate stage.
"""

from contextlib import contextmanager
from dataclasses import asdict, fields, replace
from datetime import timezone
import fcntl
import json
import os
import re
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

from ha_runtime.config import DatabaseUnavailable, SERVICES
from ha_egress.boot import validate_authority as validate_broker_authority

from . import frozen_ingress, native_guest_restore as restore, native_guest_runtime as runtime
from . import native_guest_activation as activation, native_guest_live as live
from .legacy_writers import TransitionBlocked, _need
from .native_stdio_bridge import (
    rejoin_wal_evidence,
    ACTIVATION_GUEST_CHECK_SECONDS, ACTIVATION_GUEST_REPLY_SECONDS, DUPLEX_GUEST_VERBS, PREPARATION_BOUNDARY_SECONDS, Binding, BridgeRejected, FRAME_BYTES, GENERATION_HANDOFF_FRAME_BYTES,
    GUEST_ACTIVATE_GENERATION, GUEST_OBSERVE_GENERATION, GUEST_OBSERVE_PREPARATION, GUEST_PREPARE_GENERATION, GUEST_PUBLISH_GENERATION, GUEST_RESTORE_READBACK, GUEST_PREFLIGHT_REPLACEMENT, GUEST_PUBLISH_REPLACEMENT, GUEST_START_REPLACEMENT,
    GUEST_OBSERVE_INITIAL_STANDBY_RETIREMENT, GUEST_OBSERVE_REPLACEMENT, GUEST_RETIRE_INITIAL_STANDBY_CONTAINERS,
    GUEST_QUIESCE_INITIAL_RECOVERY, GUEST_OBSERVE_INITIAL_RECOVERY_HOLD, GUEST_OBSERVE_CAR_ISOLATION, QUIESCENCE_BOUNDARIES,
    GUEST_TRANSITION_TARGET, GUEST_START_INITIAL_RECOVERY, GUEST_START_INITIAL_REARM, GUEST_START_INITIAL_POSTBOOT, GUEST_QUIESCE_INITIAL_POSTBOOT, GUEST_OBSERVE_INITIAL_POSTBOOT, GUEST_START_INITIAL_UNPUBLISHED, GUEST_OBSERVE_UNPUBLISHED_PREDECESSOR, GUEST_QUIESCE_POSTBOOT_UNPUBLISHED, RECOVERY_START_ORDER,
    GUEST_VERIFY_GENERATION, GUEST_VERIFY_PREPARED_GENERATION, INITIAL_RECOVERY_HOLD, MAX_REQUESTS, Receipt, Request, Stdio, VERSION, _args, _bytes, _digest,
    _hash, _matches, _pairs, _reject_constant,
)


ROOT = Path("/var/lib/n100-ha/cutover")
LOCK = ROOT / "mutation.lock"
VERBS = {
    "preflight", "begin_receive", "finish_receive", "restore", "prepare_runtime", "verify_closed",
    "assert_activation_closed", "activate", "assert_live", "observe_restore", "observe_rollback",
    GUEST_RESTORE_READBACK,
    GUEST_OBSERVE_INITIAL_STANDBY_RETIREMENT,
    GUEST_RETIRE_INITIAL_STANDBY_CONTAINERS,
    GUEST_QUIESCE_INITIAL_RECOVERY,
    GUEST_OBSERVE_INITIAL_RECOVERY_HOLD,
    GUEST_OBSERVE_CAR_ISOLATION,
    GUEST_START_INITIAL_RECOVERY,
    GUEST_START_INITIAL_REARM,
    GUEST_START_INITIAL_POSTBOOT,
    GUEST_QUIESCE_INITIAL_POSTBOOT,
    GUEST_OBSERVE_INITIAL_POSTBOOT,
    GUEST_START_INITIAL_UNPUBLISHED,
    GUEST_OBSERVE_UNPUBLISHED_PREDECESSOR,
    GUEST_QUIESCE_POSTBOOT_UNPUBLISHED,
    GUEST_PREFLIGHT_REPLACEMENT,
    GUEST_PUBLISH_REPLACEMENT,
    GUEST_START_REPLACEMENT,
    GUEST_OBSERVE_REPLACEMENT,
    GUEST_ACTIVATE_GENERATION,
    GUEST_OBSERVE_GENERATION,
    GUEST_PUBLISH_GENERATION,
    GUEST_PREPARE_GENERATION,
    GUEST_OBSERVE_PREPARATION,
    GUEST_TRANSITION_TARGET,
    GUEST_VERIFY_GENERATION,
    GUEST_VERIFY_PREPARED_GENERATION,
}
HANDOFF = "activation-handoff.json"
EMPTY = "0" * 64
RESTORE_DYNAMIC = {"boot_sha256", "installation_id", "plan_sha256", "manifest_sha256", "attempt_id"}
RUNTIME_DYNAMIC = {
    "node", "boot_sha256", "installation_id", "protected_inventory_sha256", "plan_sha256", "manifest_sha256",
    "frozen_transfer_sha256", "restore_receipt_sha256", "attempt_id", "system_id", "cluster",
    "postgres_id", "retained_postgres_id", "primary_node", "generation",
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
    raw = stream.readline(GENERATION_HANDOFF_FRAME_BYTES + 1)
    _need(0 < len(raw) <= GENERATION_HANDOFF_FRAME_BYTES and raw.endswith(b"\n"), "guest_worker_request_size")
    value = json.loads(raw.decode("ascii"), object_pairs_hook=_pairs, parse_constant=_reject_constant)
    _need(type(value) is dict and _bytes(value) == raw and type(value["binding"]) is dict,
          "guest_worker_request_encoding")
    _need(len(raw) <= FRAME_BYTES or value.get("verb") == GUEST_PUBLISH_GENERATION, "guest_worker_request_size")
    binding = Binding(**value["binding"])
    binding.fields()
    request = Request(binding, value["node"], value["verb"], value["sequence"], value["nonce"], value["args"])
    _need(type(request.sequence) is int and 0 < request.sequence <= MAX_REQUESTS and _hash(request.nonce)
          and type(request.verb) is str and request.verb in VERBS, "guest_worker_request_scope")
    _args(request.verb, request.node, request.args)
    _need(_matches(value, {**_scope(request), "args": request.args}), "guest_worker_request_scope")
    if request.verb not in DUPLEX_GUEST_VERBS:
        _need(not stream.read(1), "guest_worker_request_size")
    return request


@contextmanager
def _guest_mutation_descriptor(wait=0):
    """Hold the installed guest mutation lock without inspecting the recovery hold."""
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


@contextmanager
def mutation_lock(wait=0):
    with _guest_mutation_descriptor(wait=wait) as descriptor:
        # Test under the installed mutation lock. A damaged or interrupted
        # recovery record must fence ordinary workers just as a valid one does.
        try:
            (ROOT / INITIAL_RECOVERY_HOLD).lstat()
        except FileNotFoundError:
            pass
        else:
            raise TransitionBlocked("guest_worker_initial_recovery_held")
        yield descriptor


def _initial_recovery_inventory(binding, containers, protected_inventory_sha256, stopped, baseline=None):
    error = "guest_initial_recovery_quiescence_unverified"
    _need(type(containers) is dict and set(containers) == SERVICES | {"ha-broker"}
          and all(type(item) is dict and set(item) == {"container_id", "image_id"}
                  and _hash(item["container_id"])
                  and runtime._match(r"sha256:[a-f0-9]{64}", item["image_id"])
                  for item in containers.values()), error)
    ids = {item["container_id"] for item in containers.values()}
    _need(len(ids) == len(containers), error)
    objects = runtime._inventory()
    owned = {identifier for identifier, item in objects.items()
             if (item["Config"]["Labels"] or {}).get(runtime.ATTEMPT_LABEL) == binding.attempt_id}
    _need(owned == ids, error)
    unowned = {identifier: item for identifier, item in objects.items() if identifier not in ids}
    for item in unowned.values():
        if runtime._service(item) in containers:
            state = item["State"]
            _need(state["Status"] in {"created", "exited"} and state["Running"] is False
                  and state["Restarting"] is False and state["Paused"] is False
                  and item["HostConfig"]["RestartPolicy"]["Name"] == "no", error)
    contract = {}
    for service, pin in containers.items():
        item = objects[pin["container_id"]]
        state = item["State"]
        running = state["Running"] if stopped is None else service not in stopped
        _need(type(running) is bool and runtime._service(item) == service and item["Image"] == pin["image_id"]
              and item["HostConfig"]["RestartPolicy"]["Name"] == "no" and item["HostConfig"]["AutoRemove"] is False
              and item["RestartCount"] == 0
              and state["Running"] is running and state["Paused"] is False and state["Restarting"] is False
              and state["OOMKilled"] is False and state["Dead"] is False
              and state["Status"] == ("running" if running else "exited")
              and type(state["Pid"]) is int and (state["Pid"] > 0 if running else state["Pid"] == 0)
              and (running or (type(state["ExitCode"]) is int and state["ExitCode"] in {0, 143}
                               and state["FinishedAt"] > state["StartedAt"])), error)
        layout = _digest(_bytes({"container": {key: item[key] for key in ("Name", "Image", "Config", "HostConfig")},
                                 "mounts": sorted(item["Mounts"], key=lambda mount: mount["Destination"])}))
        _need(type(state["StartedAt"]) is str and state["StartedAt"], error)
        contract[service] = {"layout_sha256": layout, "started_at": state["StartedAt"], "running": running}
    protected = runtime.inventory_sha256(unowned)
    _need(protected == protected_inventory_sha256, error)
    if baseline is not None:
        original, original_protected = baseline
        _need(protected == original_protected and original.keys() == contract.keys()
              and all(record["layout_sha256"] == original[name]["layout_sha256"]
                      for name, record in contract.items()), error)
        if stopped is not None:
            _need(all(record["started_at"] == original[name]["started_at"] for name, record in contract.items()), error)
        else:
            broker = contract["ha-broker"]
            _need(not broker["running"] or broker["started_at"] != original["ha-broker"]["started_at"], error)
    return contract, protected


def quiesce_initial_recovery(binding, *, recovery_attempt_id, installation_id, runtime_receipt_sha256,
                             activation_receipt_sha256, protected_inventory_sha256,
                             assert_recovery_closed, car_route_release_sha256=None):
    """Durably hold ordinary activation and stop only A's installed workloads.

    The caller supplies a live authenticated authority check for the exact
    recovery attempt, closed database pair and drained external effects.
    A failure after persistence leaves the hold in place for reconciliation.
    """
    error = "guest_initial_recovery_quiescence_unverified"
    _need(os.geteuid() == 0 and type(binding) is Binding and callable(assert_recovery_closed)
          and all(_hash(pin) for pin in (recovery_attempt_id, installation_id, runtime_receipt_sha256,
                                         activation_receipt_sha256, protected_inventory_sha256))
          and (car_route_release_sha256 is None or _hash(car_route_release_sha256))
          and recovery_attempt_id != binding.attempt_id, error)
    source = binding.fields()
    stage = ROOT / binding.attempt_id
    _need(_digest(restore.BOOT_ID.read_bytes()) == binding.boot("A"), error)
    runtime._directory(stage, 0)
    raw, prepared = _read_json(stage / "runtime-prepared.json")
    activated_raw, activated = _read_json(stage / "runtime-activation.json")
    _, installed = _read_json(restore.INSTALLATION)
    original = {"node": "A", "installation_id": installation_id, "attempt_id": binding.attempt_id,
                "plan_sha256": binding.plan_sha256, "boot_sha256": binding.boot("A")}
    retained_identity = ("cluster", "system_id_sha256", "source_commit", "manifest_sha256",
                         "restore_receipt_sha256", "engine_id_sha256", "protected_inventory_sha256")
    _need(_digest(raw) == runtime_receipt_sha256 and installed.get("node") == "A"
          and installed.get("installation_id") == installation_id
          and prepared.get("status") == "PREPARED" and activated.get("status") == "STARTED"
          and _digest(activated_raw) == activation_receipt_sha256
          and all(prepared.get(field) == activated.get(field) == value for field, value in original.items())
          and all(type(prepared.get(field)) is str and prepared[field] and activated.get(field) == prepared[field]
                  for field in retained_identity)
          and prepared.get("containers") == activated.get("containers"), error)
    containers = prepared.get("containers")
    scope = {"schema_version": 1, "binding": source, "node": "A", "installation_id": installation_id,
             "attempt_id": recovery_attempt_id, "runtime_receipt_sha256": runtime_receipt_sha256,
             "activation_receipt_sha256": activation_receipt_sha256,
             "car_route_release_sha256": car_route_release_sha256}


    with mutation_lock():
        baseline = _initial_recovery_inventory(binding, containers, protected_inventory_sha256, set())
        ids = {item["container_id"] for item in containers.values()}
        route_path = runtime.ROOT / "broker/car-route-release.json"
        route_backup = stage / f"recovery-{recovery_attempt_id}-car-route-release.json"
        route = None
        if car_route_release_sha256 is not None:
            route = runtime._read(route_path, 0, {0o444}, limit=4096)
            value = json.loads(route, object_pairs_hook=_pairs, parse_constant=_reject_constant)
            authority_path = route_path.with_name("authority.json")
            config_path = route_path.with_name("config.json")
            raw_config = runtime._read(config_path, 0, {0o444})
            raw_authority = runtime._read(authority_path, 0, {0o444}, limit=4096)
            config = json.loads(raw_config, object_pairs_hook=_pairs, parse_constant=_reject_constant)
            authority = json.loads(raw_authority, object_pairs_hook=_pairs, parse_constant=_reject_constant)
            _need(type(config) is dict and type(authority) is dict
                  and type(authority.get("schema_version")) is int and authority["schema_version"] == 3
                  and authority.get("installation_id") == installation_id
                  and config.get("installation_id") == installation_id
                  and config.get("node") == "A" and type(config.get("generation")) is int and config["generation"] == 1
                  and _digest(raw_config) == prepared["broker_sha256"]
                  and _digest(raw_authority) == activated["authority_files"]["broker/authority.json"], error)
            try:
                validate_broker_authority(authority, config, _digest(raw_config))
            except DatabaseUnavailable:
                raise TransitionBlocked(error) from None
            _need(_digest(route) == car_route_release_sha256 and type(value) is dict
                  and set(value) == {"schema_version", "action", "broker_authority_sha256", "route_verification_sha256"}
                  and type(value["schema_version"]) is int and value["schema_version"] == 1
                  and value["action"] == "enable-car-after-route-verification"
                  and value["broker_authority_sha256"] == _digest(raw_authority)
                  and _hash(value["route_verification_sha256"]) and not os.path.lexists(route_backup), error)
        intent = _bytes(scope | {"containers": containers, "container_contracts": baseline[0],
                                  "protected_inventory_sha256": baseline[1]})
        intent_pin = _digest(intent)
        _need(assert_recovery_closed(intent_pin, QUIESCENCE_BOUNDARIES[0]) is True, error)
        path = ROOT / INITIAL_RECOVERY_HOLD
        runtime._write(path, intent)

        def assert_held(boundary):
            _need(runtime._read(path, 0, {0o600}) == intent
                  and _digest(restore.BOOT_ID.read_bytes()) == binding.boot("A")
                  and assert_recovery_closed(intent_pin, boundary) is True, error)

        assert_held(QUIESCENCE_BOUNDARIES[1])
        broker = containers["ha-broker"]["container_id"]
        runtime._docker("stop", "--signal=SIGTERM", "--timeout=-1", broker, timeout=25)
        _initial_recovery_inventory(binding, containers, protected_inventory_sha256, {"ha-broker"}, baseline)
        assert_held(QUIESCENCE_BOUNDARIES[2])
        if route is not None:
            runtime._write(route_backup, route, 0o400)
            _need(runtime._read(route_backup, 0, {0o400}, limit=4096) == route
                  and runtime._read(route_path, 0, {0o444}, limit=4096) == route, error)
            _need(runtime._read(config_path, 0, {0o444}) == raw_config
                  and runtime._read(authority_path, 0, {0o444}, limit=4096) == raw_authority, error)
            route_path.unlink()
            runtime._fsync_directory(route_path.parent)
            _need(not os.path.lexists(route_path), error)
        runtime._docker("stop", "--signal=SIGTERM", "--timeout=-1", *sorted(ids - {broker}), timeout=25)
        _initial_recovery_inventory(binding, containers, protected_inventory_sha256, set(containers), baseline)
        if route is not None:
            _need(not os.path.lexists(route_path) and runtime._read(route_backup, 0, {0o400}, limit=4096) == route, error)
        assert_held(QUIESCENCE_BOUNDARIES[3])
        return intent_pin


def _read_initial_recovery_hold(request):
    error = "guest_initial_recovery_hold_unverified"
    args = request.args
    _need(os.geteuid() == 0 and request.node == "A" and request.binding.attempt_id != args["recovery_attempt_id"], error)
    _need(_digest(restore.BOOT_ID.read_bytes()) == request.binding.boot("A"), error)
    path = ROOT / INITIAL_RECOVERY_HOLD
    stage = ROOT / request.binding.attempt_id
    raw, hold = _read_json(path)
    prepared_raw, prepared = _read_json(stage / "runtime-prepared.json")
    active_raw, active = _read_json(stage / "runtime-activation.json")
    _, installed = _read_json(restore.INSTALLATION)
    expected = {"schema_version": 1, "binding": request.binding.fields(), "node": "A",
                "attempt_id": args["recovery_attempt_id"], "installation_id": args["installation_id"],
                **{name: args[name] for name in ("runtime_receipt_sha256", "activation_receipt_sha256",
                                                "protected_inventory_sha256", "car_route_release_sha256")},
                "containers": prepared.get("containers"), "container_contracts": hold.get("container_contracts")}
    contracts = hold.get("container_contracts")
    _need(raw == _bytes(expected) and hold == expected and installed.get("node") == "A"
          and installed.get("installation_id") == args["installation_id"]
          and prepared.get("status") == "PREPARED" and active.get("status") == "STARTED"
          and _digest(prepared_raw) == args["runtime_receipt_sha256"]
          and _digest(active_raw) == args["activation_receipt_sha256"]
          and active.get("containers") == hold["containers"] and type(contracts) is dict
          and set(contracts) == SERVICES | {"ha-broker"}
          and all(type(value) is dict and set(value) == {"layout_sha256", "started_at", "running"}
                  and _hash(value["layout_sha256"]) and type(value["started_at"]) is str and value["started_at"]
                  and value["running"] is True for value in contracts.values()), error)
    return raw, hold, prepared, active


def _assert_car_route_blocked(request, raw, prepared, active):
    error = "guest_initial_recovery_car_isolation_unverified"
    args = request.args
    broker = runtime.ROOT / "broker"
    backup = ROOT / request.binding.attempt_id / f"recovery-{args['recovery_attempt_id']}-car-route-release.json"
    _need(_digest(runtime._read(backup, 0, {0o400}, limit=4096)) == args["car_route_release_sha256"]
          and _digest(runtime._read(broker / "config.json", 0, {0o444})) == prepared["broker_sha256"]
          and _digest(runtime._read(broker / "authority.json", 0, {0o444}, limit=4096))
            == active["authority_files"]["broker/authority.json"]
          and runtime._read(ROOT / INITIAL_RECOVERY_HOLD, 0, {0o600}, limit=65536) == raw
          and _digest(restore.BOOT_ID.read_bytes()) == request.binding.boot("A"), error)
    try:
        (broker / "car-route-release.json").lstat()
    except FileNotFoundError:
        return
    raise TransitionBlocked(error)


def start_initial_recovery(request, assert_windows_current):
    """Restart the installed containers while retaining the recovery hold."""
    from . import initial_app_resume
    error = "guest_initial_recovery_start_unverified"
    _need(callable(assert_windows_current), error)
    _args(request.verb, request.node, request.args)
    unpublished=request.verb==GUEST_START_INITIAL_UNPUBLISHED
    postboot = request.verb == GUEST_START_INITIAL_POSTBOOT
    rearming = request.verb == GUEST_START_INITIAL_REARM
    with _guest_mutation_descriptor():
        raw, hold, prepared, active = _read_initial_recovery_hold(request)
        args = request.args
        _need(_digest(raw) == args["hold_sha256"], error)
        stage = ROOT / request.binding.attempt_id
        prefix = (("unpublished-"+args["unpublished"]["attempt_id"]) if unpublished else ("postboot-" + args["postboot"]["attempt_id"]) if postboot else
                  (("rearm-" + args["rearm"]["attempt_id"]) if rearming else ("recovery-" + args["recovery_attempt_id"]))) + "-start-"
        intent_path, progress_path, done_path = (stage / (prefix + name + ".json")
                                                 for name in ("intent", "progress", "done"))
        resume = None
        if initial_app_resume.supports(request):
            partial = postboot and os.path.lexists(initial_app_resume.path(request, partial=True))
            if partial or os.path.lexists(initial_app_resume.path(request)):
                resume = initial_app_resume.load(request, raw, hold, prepared, active, partial=partial)
                prefix = resume[2]
                intent_path, progress_path, done_path = (stage / (prefix + name + ".json")
                                                         for name in ("intent", "progress", "done"))
        _need(not any(os.path.lexists(path) for path in (intent_path, progress_path, done_path)), error)
        containers = hold["containers"]
        _need(set(containers) == set(RECOVERY_START_ORDER), error)
        baseline = (hold["container_contracts"], args["protected_inventory_sha256"])
        if resume is not None:
            baseline = (resume[1]['container_contracts'], resume[1]['protected_inventory_sha256'])
        if postboot and resume is None:
            from .native_guest_postboot import observe
            from .initial_postboot import stopped_attempt, sameboot
            if sameboot(args['postboot']):
                _need(args['postboot_stopped_receipt_sha256']==args['postboot']['stopped_receipt_sha256'],error)
            stopped = observe(request,stopped_attempt(args['postboot']),args['postboot_stopped_receipt_sha256'])
            baseline = (stopped['container_contracts'],args['protected_inventory_sha256'])
        _initial_recovery_inventory(request.binding, containers, args["protected_inventory_sha256"],
                                    set(containers), baseline)
        worker = GuestWorker(request)
        postgres, pins, _ = worker._prepared_pins(prepared["manifest_sha256"])
        _need(pins.installation_id == args["installation_id"] and pins.node == "A"
              and pins.attempt_id == request.binding.attempt_id, error)
        authorities = {runtime.ROOT / name: pin for name, pin in active["authority_files"].items()}
        _need(set(authorities) == set(live._authority_paths().values()), error)
        started = set()

        def unchanged():
            if resume is not None:
                initial_app_resume.preserved(request, resume[0], resume[1], hold, active, raw)
            _need(_read_initial_recovery_hold(request)[:2] == (raw, hold), error)
            _assert_car_route_blocked(request, raw, prepared, active)
            _need(all(_digest(runtime._read(path, 0, {0o444})) == pin
                      for path, pin in authorities.items()), error)
            contract, _ = _initial_recovery_inventory(request.binding, containers, args["protected_inventory_sha256"],
                                                      None, baseline)
            _need(all(contract[role]["started_at"] != baseline[0][role]["started_at"]
                      for role in started), error)
            if resume is not None or postboot:
                _need(all(contract[role]["started_at"] == baseline[0][role]["started_at"]
                          for role in containers if role not in started), error)
            _need(all(contract[role]["running"] is (role in started) for role in containers), error)

        def check(boundary):
            deadline = time.monotonic() + ACTIVATION_GUEST_CHECK_SECONDS
            unchanged()
            challenge_scope = {"request": _scope(request), "hold_sha256": args["hold_sha256"], "boundary": boundary}
            if resume is not None:
                challenge_scope['resume_authorization_sha256'] = _digest(resume[0])
            if unpublished:
                challenge_scope.update(unpublished=args["unpublished"],unpublished_plan_sha256=args["unpublished_plan_sha256"])
            if postboot:
                challenge_scope.update(postboot=args["postboot"], postboot_plan_sha256=args["postboot_plan_sha256"])
            if rearming:
                challenge_scope.update(rearm=args["rearm"], rearm_plan_sha256=args["rearm_plan_sha256"])
            challenge = _digest(_bytes(challenge_scope))
            proof = assert_windows_current(challenge, boundary)
            _need(_hash(proof) and time.monotonic() < deadline, error)
            unchanged()
            # All Docker and file observations precede the final DB-clock sample.
            # The sample promises >1s remaining; dispatch has strictly <0.5s
            # measured before that read, accounting for time spent sampling.
            dispatch_deadline = min(deadline, time.monotonic() + 0.5)
            observe_lease = live.observe_unpublished_a_lease if unpublished else (live.observe_postboot_a_lease if postboot else (live.observe_rearmed_a_lease if rearming else live.observe_recovered_a_lease))
            local = observe_lease(
                pins, postgres, recovery_attempt_id=args["recovery_attempt_id"], challenge_sha256=challenge,
                deadline=dispatch_deadline, minimum_remaining_seconds=1,
                **({"unpublished":args["unpublished"],"unpublished_plan_sha256":args["unpublished_plan_sha256"]} if unpublished else ({"postboot": args["postboot"], "postboot_plan_sha256": args["postboot_plan_sha256"]} if postboot else
                   ({"rearm": args["rearm"], "rearm_plan_sha256": args["rearm_plan_sha256"]} if rearming else {}))))
            _need(_hash(local) and time.monotonic() < dispatch_deadline, error)
            return {"windows_proof_sha256": proof, "guest_proof_sha256": local}, dispatch_deadline

        def fresh(deadline):
            _need(time.monotonic() < deadline, error)

        proof, deadline = check("recovery_start_intent")
        intent_record = {"schema_version": 1, "status": "INTENT", **_scope(request), "args": args,
                         "containers": containers, "authority_files": active["authority_files"],
                         "proof": proof, "automatic_retry_permitted": False}
        if resume is not None:
            intent_record["resume_authorization_sha256"] = _digest(resume[0])
        intent = _bytes(intent_record)
        fresh(deadline)
        runtime._write(intent_path, intent)
        context, leaf = activation._tls(prepared)
        raw_spec = runtime._read(stage / "guest-spec.json", 0, {0o400, 0o600})
        _need(_digest(raw_spec) == pins.spec_sha256, error)
        spec = json.loads(raw_spec)
        address = spec["guest_ip"]
        _need(address == spec["pg_a"] and spec["node"] == "A", error)
        try:
            for role in RECOVERY_START_ORDER:
                _, deadline = check("recovery_before:" + role)
                _need(runtime._read(intent_path, 0, {0o600}) == intent, error)
                fresh(deadline)
                # Record the candidate before dispatch so an uncertain start is also stopped.
                started.add(role)
                runtime._docker("start", containers[role]["container_id"], timeout=30)
                proof, deadline = check("recovery_after:" + role)
                fresh(deadline)
                runtime._write(stage / (prefix + "progress-" + role + ".json"), _bytes({"intent_sha256": _digest(intent),
                    "started": sorted(started), "proof": proof}))
                if role in SERVICES:
                    activation._wait_for_app(context, leaf, address, role, expected_node="A")
                check("recovery_ready:" + role)
            proof, deadline = check("recovery_started")
            _need(runtime._read(intent_path, 0, {0o600}) == intent, error)
            result = _bytes({"schema_version": 1, "status": "STARTED", "intent_sha256": _digest(intent),
                             "hold_sha256": args["hold_sha256"], "started": list(RECOVERY_START_ORDER),
                             "proof": proof, "automatic_retry_permitted": False})
            fresh(deadline)
            runtime._write(done_path, result)
            return Receipt("PASS", _digest(result))
        except Exception:
            # Intent and hold survive; only this attempt's pinned workloads are stopped.
            if started:
                runtime._docker("stop", "--signal=SIGTERM", "--timeout=-1",
                                *[containers[role]["container_id"] for role in reversed(RECOVERY_START_ORDER)
                                  if role in started], timeout=30)
            raise


def observe_unpublished_predecessor(request):
    from .native_guest_postboot import current
    with _guest_mutation_descriptor():
        raw,hold,contracts,protected=current(request)
        _need(all(row['running'] is False and row['started_at']==hold['container_contracts'][role]['started_at']
                  for role,row in contracts.items()),'guest_unpublished_predecessor_unconfirmed')
        error='guest_unpublished_predecessor_unconfirmed'
        from .initial_unpublished import derive_predecessor
        plan_pin=request.args['predecessor_plan_sha256']
        terminal=runtime._read(ROOT/('predecessor-plan-'+plan_pin+'.json'),0,{0o400},limit=65536)
        terminal_pin=_digest(terminal)
        _need(runtime._read(ROOT/('predecessor-terminal-'+terminal_pin+'.json'),0,{0o400},limit=65536)==terminal,error)
        derive_predecessor(terminal,terminal_pin,published=False)
        prior=json.loads(terminal)
        field='rearm' if prior['kind']=='initial-wsl-cutover-rearm' else 'unpublished'
        _need(prior['controller_plan_sha256']==plan_pin
              and prior[field]['attempt_id']==request.args['predecessor_rearm_attempt_id']
              and prior['recovery']['attempt_id']==request.args['recovery_attempt_id']
              and prior['attempt_id']==request.binding.attempt_id
              and prior['legacy_plan_sha256']==request.binding.plan_sha256
              and request.args['unpublished_attempt_id'] not in {
                  prior[field]['attempt_id'],request.args['recovery_attempt_id'],request.binding.attempt_id},error)
        stage=ROOT/request.binding.attempt_id
        _need(not any(re.match(r'^(?:recovery|rearm|unpublished|postboot)-[a-f0-9]{64}-start-',path.name)
                      for path in stage.iterdir()),error)
        return Receipt('PASS',_digest(_bytes(_scope(request)|{'args':request.args,'hold_sha256':_digest(raw),
            'predecessor_terminal_sha256':terminal_pin,'containers':contracts,
            'protected_inventory_sha256':protected,'app_start_intent_absent':True})))


def observe_initial_recovery_hold(request):
    args = request.args
    with _guest_mutation_descriptor():
        raw, hold, prepared, active = _read_initial_recovery_hold(request)
        _initial_recovery_inventory(request.binding, hold["containers"], args["protected_inventory_sha256"],
                                    SERVICES | {"ha-broker"},
                                    (hold["container_contracts"], args["protected_inventory_sha256"]))
        _assert_car_route_blocked(request, raw, prepared, active)
        return Receipt("PASS", _digest(raw))


def observe_car_isolation(request):
    """Verify the same route block whether the exact broker is stopped or newly started."""
    args = request.args
    raw, hold, prepared, active = _read_initial_recovery_hold(request)
    _need(_digest(raw) == args["hold_sha256"], "guest_initial_recovery_car_isolation_unverified")
    _assert_car_route_blocked(request, raw, prepared, active)
    current, _ = _initial_recovery_inventory(request.binding, hold["containers"], args["protected_inventory_sha256"],
        None, (hold["container_contracts"], args["protected_inventory_sha256"]))
    _assert_car_route_blocked(request, raw, prepared, active)
    return Receipt("PASS", _digest(_bytes(_scope(request) | {"hold_sha256": args["hold_sha256"], "containers": current})))


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
                                   attempt_id=(self.request.binding.attempt_id
                                               if self.input["restore"].get("retained_postgres_id") else None),
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
                                   primary_node="A", generation=1,
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
            journal = restore.migration_journal(pins, self.restore_stage)
            runtime._directory(journal, 0)
            _need(not any(os.path.lexists(journal / name) for name in
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

    def read_restore_receipt(self):
        """Return the existing receipt for the exact request if its database remains closed."""
        self._assert_no_activation()
        self._proof("preflight", self.args["target_receipt_sha256"])
        _, transfer = self._proof("finish_receive", self.args["transfer_receipt_sha256"])
        manifest = self.args["manifest_sha256"]
        _need(transfer["manifest_sha256"] == manifest, "guest_worker_restore_transfer_unbound")
        self._receiver_settled(manifest)
        original = _scope(replace(self.request, verb="restore"))
        common = {**original, "worker_input_sha256": self.input_digest}
        raw, _ = _read_json(self.stage / "worker-restore.intent.json")
        _need(raw == _bytes({**common, "args_sha256": _digest(_bytes(self.args))}),
              "guest_worker_restore_intent_unbound")
        receipt, _ = _read_json(self.stage / "worker-restore.json")
        pins = self.restore_pins(manifest)
        journal = restore.migration_journal(pins, self.restore_stage)
        _, result = _read_json(journal / "migration-pass.json")
        _need(receipt == _bytes({**common, "evidence": result}) and result["status"] == "PASS"
              and result["manifest_sha256"] == manifest and result["services"] == sorted(SERVICES)
              and result["database_and_apps_closed"] is True and result["writer_authority_issued"] is False,
              "guest_worker_restore_receipt_unbound")
        observed = restore.observe_closed_outcome(pins, self.restore_stage)
        _need(observed["status"] == "COMMITTED_CLOSED"
              and observed["migration_receipt_sha256"] == result["migration_receipt_sha256"],
              "guest_worker_restore_unsettled")
        return Receipt("PASS", _digest(receipt))

    def _inventory_for_preparation(self, pins, *, prepared=False, restore_receipt=None):
        if self.node == "B":
            return pins.inventory_sha256
        _, proof = self._proof("restore", restore_receipt or self.args.get("restore_receipt_sha256"))
        _, passed = _read_json(restore.migration_journal(pins, self.restore_stage) / "migration-pass.json")
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

    def _initial_standby_retirement_inputs(self):
        """Bind the exact original B attempt under the worker mutation lock."""
        _need(self.node == "B", "guest_initial_standby_retirement_unconfirmed")
        _, closed = self._proof("verify_closed", self.args["verify_closed_sha256"])
        db, pins, prepared = self._prepared_pins(closed["manifest_sha256"])
        _need(closed["runtime_receipt_sha256"] == prepared["runtime_receipt_sha256"],
              "guest_initial_standby_retirement_unconfirmed")

        return db, pins, closed["runtime_receipt_sha256"]

    def _assert_initial_standby_unactivated(self):
        self.identity()
        self._assert_no_activation()

    def observe_initial_standby_retirement(self):
        db, pins, receipt_sha256 = self._initial_standby_retirement_inputs()
        plan = runtime.observe_initial_standby_retirement(
            pins, db, self.stage, runtime_receipt_sha256=receipt_sha256,
            assert_no_activation=self._assert_initial_standby_unactivated,
        )
        return Receipt("PASS", _digest(_bytes(plan)))

    def retire_initial_standby_containers(self):
        db, pins, receipt_sha256 = self._initial_standby_retirement_inputs()
        result = runtime.retire_initial_standby_containers(
            pins, db, self.stage, runtime_receipt_sha256=receipt_sha256,
            plan_sha256=self.args["retirement_plan_sha256"],
            assert_no_activation=self._assert_initial_standby_unactivated,
        )
        return Receipt("PASS", _digest(_bytes(result)))

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
            # Read the database clock after transport so SSH startup cannot
            # consume the freshness window of the write lease.
            windows_proof = assert_windows_current(challenge, boundary)
            _need(_hash(windows_proof), "guest_worker_live_windows_unconfirmed")
            observed, sampled, expires, _ = live._lease(pins, db)
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
        journal = restore.migration_journal(pins, self.restore_stage)
        if self.node == "A" and os.path.lexists(journal / "migration-intent.json"):
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
                    GUEST_RESTORE_READBACK: self.read_restore_receipt,
                    GUEST_OBSERVE_INITIAL_STANDBY_RETIREMENT: self.observe_initial_standby_retirement,
                    GUEST_RETIRE_INITIAL_STANDBY_CONTAINERS: self.retire_initial_standby_containers,
                    "prepare_runtime": self.prepare_runtime, "verify_closed": self.verify_closed,
                    "assert_activation_closed": self.assert_activation_closed,
                    "assert_live": self.assert_live,
                    "observe_restore": self.observe_restore, "observe_rollback": self.observe_rollback}
        return handlers[self.request.verb]()


def _replacement_bundle(request, stage, installation):
    _, installed = _read_json(restore.INSTALLATION)
    bundle = stage / "replacement-bootstrap"
    runtime._directory(bundle, 0)
    manifest = runtime._read(bundle / "manifest.json", 0, {0o400, 0o444})
    _need(_digest(manifest) == request.args["bootstrap_manifest_sha256"], "guest_replacement_manifest_unconfirmed")
    lock = runtime._read(bundle / "lock.json", 0, {0o400, 0o444})
    index = json.loads(manifest, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    _need(_digest(lock) == index["files"]["lock.json"], "guest_replacement_manifest_unconfirmed")
    spec = json.loads(lock, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    source = "B" if request.node == "A" else "A"
    _need(all(installed[key] == spec[key] for key in ("node", "installation_id", "source_commit"))
          and spec["node"] == request.node and spec["installation_id"] == installation
          and spec["plan_sha256"] == request.binding.plan_sha256
          and spec["primary_node"] == spec["source_node"] == source, "guest_replacement_installation_changed")
    bootstrap = bundle / "bootstrap.py"
    pinned_code = runtime._read(bootstrap, 0, {0o400, 0o444, 0o500, 0o555})
    _need(_digest(pinned_code) == index["files"]["bootstrap.py"], "guest_replacement_bootstrap_unconfirmed")
    return bundle, bootstrap, pinned_code


def _run_replacement_bootstrap(bootstrap, code, command, bundle, arguments, timeout):
    runner = (
        "import sys\n"
        "sys.argv.pop(0)\n"
        "__file__ = sys.argv[0]\n"
        "exec(compile(sys.stdin.buffer.read(), __file__, 'exec'))\n"
    )
    result = subprocess.run(("/usr/bin/python3", "-I", "-B", "-S", "-c", runner, str(bootstrap), command,
                             "--bundle", str(bundle), *arguments), input=code, capture_output=True,
                            check=False, timeout=timeout, env={"PATH": "/usr/bin:/bin", "HOME": "/root", "LANG": "C"})
    _need(result.returncode == 0, "guest_replacement_outcome_unconfirmed")
    return json.loads(result.stdout)


def preflight_replacement_standby(request):
    _need(os.geteuid() == 0, "guest_worker_root_required")
    boot = request.binding.boot(request.node)
    _need(_digest(restore.BOOT_ID.read_bytes()) == boot, "guest_worker_boot_changed")
    stage = ROOT / request.binding.attempt_id
    runtime._directory(stage, 0)
    installation = request.args["installation_id"]
    bundle, bootstrap, code = _replacement_bundle(request, stage, installation)
    arguments = ("--manifest-sha256", request.args["bootstrap_manifest_sha256"], "--boot-sha256", boot)
    result = _run_replacement_bootstrap(bootstrap, code, "preflight-replacement-standby", bundle, arguments, 30)
    _, installed = _read_json(restore.INSTALLATION)
    _need(_matches(result, {"status": "PASS", "source_commit": installed["source_commit"], "production_deployed": False})
          and installed["installation_id"] == installation and installed["node"] == request.node
          and _digest(restore.BOOT_ID.read_bytes()) == boot, "guest_replacement_manifest_unconfirmed")
    return Receipt("PASS", _digest(_bytes(_scope(request) | {"args": request.args})))


def publish_replacement_standby(request):
    _need(os.geteuid() == 0, "guest_worker_root_required")
    boot = request.binding.boot(request.node)
    _need(_digest(restore.BOOT_ID.read_bytes()) == boot, "guest_worker_boot_changed")
    stage = ROOT / request.binding.attempt_id
    runtime._directory(stage, 0)
    installation = request.args["installation_id"]
    _replacement_bundle(request, stage, installation)
    grant = _bytes({"schema_version": 1, "binding": request.binding.fields(), "node": request.node, **request.args})
    path = stage / "replacement-standby-input.json"
    if not os.path.lexists(path):
        runtime._write(path, grant, 0o400)
    _need(runtime._read(path, 0, {0o400}) == grant and _digest(restore.BOOT_ID.read_bytes()) == boot,
          "guest_replacement_start_not_bound")
    return Receipt("PASS", _digest(grant))


def replacement_standby(request):
    _need(os.geteuid() == 0, "guest_worker_root_required")
    boot = request.binding.boot(request.node)
    _need(_digest(restore.BOOT_ID.read_bytes()) == boot, "guest_worker_boot_changed")
    stage = ROOT / request.binding.attempt_id
    runtime._directory(stage, 0)
    _, grant = _read_json(stage / "replacement-standby-input.json")
    installation = grant.get("installation_id")
    _need(_hash(installation) and _matches(grant, {
        "schema_version": 1, "binding": request.binding.fields(), "node": request.node,
        "installation_id": installation, **request.args,
    }), "guest_replacement_start_not_bound")
    bundle, bootstrap, pinned_code = _replacement_bundle(request, stage, installation)
    observe = request.verb == GUEST_OBSERVE_REPLACEMENT
    command = "observe-replacement-standby" if observe else "start-replacement-standby"
    reply = _run_replacement_bootstrap(
        bootstrap, pinned_code, command, bundle,
        ("--manifest-sha256", request.args["bootstrap_manifest_sha256"], "--boot-sha256", boot), 75,
    )
    expected = {"status": "PASS", "writer_authority_issued": False}
    if observe:
        container = reply.get("standby_container_sha256")
        _need(_hash(container), "guest_replacement_outcome_unconfirmed")
        expected["standby_container_sha256"] = container
    else:
        expected["standby_start_requested"] = True
    _need(_matches(reply, expected), "guest_replacement_outcome_unconfirmed")
    return Receipt("PASS", _digest(_bytes(_scope(request) | {"args": request.args, "installation_id": installation,
                                                          "result": expected})))


def _checked_generation_handoff(request, raw, handoff):
    _need(_digest(raw) == request.args["generation_handoff_sha256"]
          and set(handoff) == {"schema_version", "binding", "generation", "runtime", "postgres", "activation", "replay_floor"}
          and type(handoff["schema_version"]) is int and handoff["schema_version"] == 1
          and handoff["binding"] == request.binding.fields(), "guest_generation_handoff_unbound")
    pins = runtime.RuntimePins(**handoff["runtime"])
    pg = restore.RestorePins(**handoff["postgres"])
    activation_pins = activation.ActivationPins(**handoff["activation"])
    generation = activation.ServingGeneration(**handoff["generation"])
    floor = handoff["replay_floor"]
    activation.validate_generation_handoff_pins(
        request.binding, request.node, pins, pg, activation_pins, generation, floor, request.args["effects_plan_sha256"],
    )

    return pins, pg, activation_pins, generation, floor


def _generation_preparation_scope(request):
    pins = runtime.RuntimePins(**request.args["runtime"])
    pg = restore.RestorePins(**request.args["postgres"])
    generation = activation.ServingGeneration(**request.args["generation"])
    stage = ROOT / request.binding.attempt_id
    runtime._directory(stage, 0)
    _need(pins.node == request.node and pins.attempt_id == request.binding.attempt_id
          and pins.boot_sha256 == request.binding.boot(request.node), "guest_generation_prepared_unbound")
    return pins, pg, generation, stage


def observe_generation_preparation(request):
    _need(os.geteuid() == 0, "guest_worker_root_required")
    pins, pg, generation, stage = _generation_preparation_scope(request)
    pins.validate()
    pg.validate()
    generation.validate()
    _need(generation.epoch > 1 and pins.node == pins.primary_node == generation.primary
          and pins.generation == generation.epoch and pg.postgres_id == pins.postgres_id
          and pg.installation_id == pins.installation_id and pg.boot_sha256 == pins.boot_sha256
          and pg.source_commit == pins.release["source_commit"], "guest_generation_prepared_unbound")
    pin = runtime._inspect_prepared(pins, stage, stage / "runtime-intent.json", stage / "runtime-prepared.json")
    return Receipt("PASS", pin)


def prepare_generation(request, assert_windows_current):
    _need(os.geteuid() == 0 and callable(assert_windows_current), "guest_worker_live_windows_required")
    pins, pg, generation, stage = _generation_preparation_scope(request)
    wal, = rejoin_wal_evidence(request.args, "guest_generation_prepared_unbound").values()
    result = runtime.prepare_generation_runtime(
        pins, pg, stage, generation, int(wal, 16),
        assert_windows_rejoin=assert_windows_current,
    )
    raw, persisted = _read_json(stage / "runtime-prepared.json")
    _need(_matches(result, persisted) and result["status"] == "PREPARED"
          and result["applications_started"] is False and result["writer_authority_issued"] is False,
          "guest_generation_prepared_unbound")
    return Receipt("PASS", _digest(raw))


def publish_generation_handoff(request):
    _need(os.geteuid() == 0, "guest_worker_root_required")
    stage = ROOT / request.binding.attempt_id
    runtime._directory(stage, 0)
    handoff = request.args["handoff"]
    raw = _bytes(handoff)
    pins, pg, _, _, _ = _checked_generation_handoff(request, raw, handoff)
    _, installed = _read_json(restore.INSTALLATION)
    _need(_digest(restore.BOOT_ID.read_bytes()) == pins.boot_sha256
          and installed["installation_id"] == pins.installation_id and installed["node"] == request.node
          and installed["source_commit"] == pg.source_commit, "guest_generation_handoff_changed")
    path = stage / "generation-handoff.json"
    if os.path.lexists(path):
        _need(runtime._read(path, 0, {0o400}, limit=GENERATION_HANDOFF_FRAME_BYTES) == raw,
              "guest_generation_handoff_changed")
    else:
        _need(not any(os.path.lexists(stage / name) for name in
                      ("runtime-activation-intent.json", "runtime-activation.json")),
              "guest_generation_already_activated")
        runtime._write(path, raw, 0o400)
    _need(runtime._read(path, 0, {0o400}, limit=GENERATION_HANDOFF_FRAME_BYTES) == raw,
          "guest_generation_handoff_changed")
    return Receipt("PASS", _digest(raw))


def activate_generation(request, assert_windows_current):
    _need(os.geteuid() == 0 and callable(assert_windows_current), "guest_worker_live_windows_required")
    stage = ROOT / request.binding.attempt_id
    runtime._directory(stage, 0)
    handoff_path = stage / "generation-handoff.json"
    raw, handoff = _read_json(handoff_path)
    pins, pg, activation_pins, generation, floor = _checked_generation_handoff(request, raw, handoff)

    def assert_lease(challenge, boundary):
        _need(_digest(restore.BOOT_ID.read_bytes()) == pins.boot_sha256, "guest_worker_boot_changed")
        _, installed = _read_json(restore.INSTALLATION)
        _need(installed["installation_id"] == pins.installation_id and installed["node"] == request.node
              and installed["source_commit"] == pg.source_commit and _read_json(handoff_path)[0] == raw,
              "guest_generation_handoff_changed")
        windows = assert_windows_current(challenge, boundary)
        _need(_hash(windows), "guest_worker_live_windows_unconfirmed")
        observed, sampled, expires, replay = live._lease(pins, pg, generation)
        _need(replay >= floor, "guest_generation_replay_unconfirmed")
        facts = {**runtime._runtime_identity(pins), **asdict(activation_pins), "challenge": challenge,
                 "boundary": boundary, **asdict(generation), "partner": generation.partner, "writes_open": True,
                 "coordinator_intent_durable": True, "transition_postcommit_verified": True,
                 "previous_primary_fenced": True, "legacy_writers_closed": True,
                 "standby_runtime_closed": True, "runtime_mutations_excluded": True}
        observed_at, expires_at = (timepoint.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                                   for timepoint in (sampled, expires))
        evidence = {"handoff_sha256": _digest(raw), "windows_proof_sha256": windows, "observation": observed, **facts}
        return {**facts, "observed_at": observed_at, "expires_at": expires_at, "lease_sha256": _digest(_bytes(evidence))}

    result = activation.activate_prepared_generation(pins, activation_pins, stage, generation, assert_lease=assert_lease)
    receipt, persisted = _read_json(stage / "runtime-activation.json")
    _need(result["status"] == "STARTED" and _matches(persisted, result)
          and result["automatic_retry_permitted"] is False and _read_json(handoff_path)[0] == raw,
          "guest_worker_activation_unconfirmed")
    return Receipt("PASS", _digest(receipt))


def observe_generation_activation(request):
    _need(os.geteuid() == 0, "guest_worker_root_required")
    stage = ROOT / request.binding.attempt_id
    runtime._directory(stage, 0)
    raw, handoff = _read_json(stage / "generation-handoff.json")
    pins, pg, activation_pins, generation, floor = _checked_generation_handoff(request, raw, handoff)
    _, installed = _read_json(restore.INSTALLATION)
    _need(_digest(restore.BOOT_ID.read_bytes()) == pins.boot_sha256
          and installed["installation_id"] == pins.installation_id and installed["node"] == request.node
          and installed["source_commit"] == pg.source_commit, "guest_generation_handoff_changed")
    receipt_raw = runtime._read(stage / "runtime-activation.json", 0, {0o600})
    pin = _digest(receipt_raw)
    live._receipts(pins, activation_pins, stage, pin, generation)
    _, _, _, replay = live._lease(pins, pg, generation)
    _need(replay >= floor and _read_json(stage / "generation-handoff.json")[0] == raw
          and runtime._read(stage / "runtime-activation.json", 0, {0o600}) == receipt_raw,
          "guest_generation_replay_unconfirmed")
    return Receipt("PASS", pin)


def verify_prepared_generation(request):
    _need(os.geteuid() == 0, "guest_worker_root_required")
    pins = runtime.RuntimePins(**request.args["runtime"])
    pins.validate()
    stage = ROOT / request.binding.attempt_id
    runtime._directory(stage, 0)
    _need(pins.node == pins.primary_node == request.node and pins.generation > 1
          and pins.attempt_id == request.binding.attempt_id and pins.boot_sha256 == request.binding.boot(request.node),
          "guest_generation_prepared_unbound")
    spec, values = _read_json(stage / "guest-spec.json")
    _need(_digest(spec) == pins.spec_sha256 and type(values["guest_ip"]) is str
          and _digest(values["guest_ip"].encode("ascii")) == request.args["target_sha256"],
          "guest_generation_prepared_unbound")
    _, installation = _read_json(restore.INSTALLATION)
    _need(installation["installation_id"] == pins.installation_id and installation["node"] == request.node
          and installation["source_commit"] == pins.release["source_commit"], "guest_generation_prepared_unbound")
    expected = request.args["runtime_receipt_sha256"]
    if expected is None:
        _need(not any(os.path.lexists(path) for path in (
            stage / "runtime-intent.json", stage / "runtime-prepared.json", stage / "apps.json",
            stage / "broker.json", runtime.ROOT / "broker" / "config.json",
            *(runtime.RUN / "ingress" / service for service in SERVICES),
        )), "guest_generation_prepared_unbound")
        checked = runtime.preflight_runtime(pins, stage)
        _need(checked["status"] == "PREFLIGHT_PASS" and checked["frozen_socket_released"] is True,
              "guest_generation_prepared_unbound")
        pin = _digest(_bytes(_scope(request) | {"preflight": checked}))
    else:
        pin = runtime._inspect_prepared(pins, stage, stage / "runtime-intent.json", stage / "runtime-prepared.json")
        _need(pin == expected, "guest_generation_prepared_unbound")
    _need(_digest(restore.BOOT_ID.read_bytes()) == pins.boot_sha256, "guest_generation_prepared_unbound")
    return Receipt("PASS", pin)


def verify_live_generation(request):
    _need(os.geteuid() == 0, "guest_worker_root_required")
    stage = ROOT / request.binding.attempt_id
    runtime._directory(stage, 0)
    path = stage / "generation-handoff.json"
    raw, handoff = _read_json(path)
    pins, pg, activation_pins, generation, floor = _checked_generation_handoff(request, raw, handoff)
    _, installed = _read_json(restore.INSTALLATION)
    _need(_digest(restore.BOOT_ID.read_bytes()) == pins.boot_sha256
          and installed["installation_id"] == pins.installation_id and installed["node"] == request.node
          and installed["source_commit"] == pg.source_commit, "guest_generation_handoff_changed")
    proof = live.observe_current_generation(
        pins, activation_pins, pg, stage, generation, activation_receipt_sha256=request.args["activation_receipt_sha256"],
        target_sha256=request.args["target_sha256"], replay_floor=floor,
    )
    _need(_hash(proof) and _read_json(path)[0] == raw
          and _digest(restore.BOOT_ID.read_bytes()) == pins.boot_sha256, "guest_generation_handoff_changed")
    return Receipt("PASS", proof)


def verify_transition_target(request):
    _need(os.geteuid() == 0, "guest_worker_root_required")
    boot = request.binding.boot(request.node)
    raw, identity = _read_json(restore.INSTALLATION)
    _need(identity.get("node") == request.node and identity.get("installation_id") == request.args["installation_id"]
          and _digest(restore.BOOT_ID.read_bytes()) == boot, "guest_transition_target_unconfirmed")
    return Receipt("PASS", _digest(_bytes(_scope(request) | {"installation_sha256": _digest(raw)})))


def handle(request, assert_windows_current=None):
    try:
        _need(type(request) is Request and request.verb in VERBS, "guest_worker_request_scope")
        _args(request.verb, request.node, request.args)
        if request.verb == GUEST_TRANSITION_TARGET:
            return verify_transition_target(request)
        if request.verb == GUEST_OBSERVE_UNPUBLISHED_PREDECESSOR:
            return observe_unpublished_predecessor(request)
        if request.verb in {GUEST_QUIESCE_INITIAL_POSTBOOT,GUEST_QUIESCE_POSTBOOT_UNPUBLISHED}:
            from .native_guest_postboot import stop
            return stop(request,assert_windows_current)
        if request.verb == GUEST_OBSERVE_INITIAL_POSTBOOT:
            from .native_guest_postboot import observe
            with _guest_mutation_descriptor():
                observe(request,request.args['postboot_attempt_id'],request.args['postboot_stopped_receipt_sha256'])
                return Receipt('PASS',request.args['postboot_stopped_receipt_sha256'])
        if request.verb == GUEST_OBSERVE_INITIAL_RECOVERY_HOLD:
            return observe_initial_recovery_hold(request)
        if request.verb == GUEST_OBSERVE_CAR_ISOLATION:
            return observe_car_isolation(request)
        if request.verb in {GUEST_START_INITIAL_RECOVERY, GUEST_START_INITIAL_REARM, GUEST_START_INITIAL_POSTBOOT, GUEST_START_INITIAL_UNPUBLISHED}:
            return start_initial_recovery(request, assert_windows_current)
        if request.verb == GUEST_QUIESCE_INITIAL_RECOVERY:
            _need(callable(assert_windows_current), "guest_worker_live_windows_required")

            def assert_closed(intent_pin, boundary):
                challenge = _digest(_bytes({"request": _scope(request), "intent_sha256": intent_pin, "boundary": boundary}))
                return _hash(assert_windows_current(challenge, boundary))

            pin = quiesce_initial_recovery(request.binding, **request.args, assert_recovery_closed=assert_closed)
            return Receipt("PASS", pin)
        with mutation_lock(wait=15 if request.verb == "finish_receive" else 0) as descriptor:
            if request.verb == GUEST_PREFLIGHT_REPLACEMENT:
                return preflight_replacement_standby(request)
            if request.verb == GUEST_PUBLISH_REPLACEMENT:
                return publish_replacement_standby(request)
            if request.verb in {GUEST_START_REPLACEMENT, GUEST_OBSERVE_REPLACEMENT}:
                return replacement_standby(request)
            if request.verb == GUEST_OBSERVE_PREPARATION:
                return observe_generation_preparation(request)
            if request.verb == GUEST_PREPARE_GENERATION:
                return prepare_generation(request, assert_windows_current)
            if request.verb == GUEST_PUBLISH_GENERATION:
                return publish_generation_handoff(request)
            if request.verb == GUEST_VERIFY_PREPARED_GENERATION:
                return verify_prepared_generation(request)
            if request.verb == GUEST_VERIFY_GENERATION:
                return verify_live_generation(request)
            if request.verb == GUEST_OBSERVE_GENERATION:
                return observe_generation_activation(request)
            if request.verb == GUEST_ACTIVATE_GENERATION:
                return activate_generation(request, assert_windows_current)
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
    if request.verb in DUPLEX_GUEST_VERBS:
        channel = Stdio(sys.stdin.fileno(), sys.stdout.fileno())

        def assert_windows_current(challenge, boundary):
            reply_seconds = (PREPARATION_BOUNDARY_SECONDS + 0.5 if request.verb == GUEST_PREPARE_GENERATION
                             else ACTIVATION_GUEST_REPLY_SECONDS)
            deadline = time.monotonic() + reply_seconds
            fields = {"request_nonce": request.nonce, "challenge": challenge, "boundary": boundary}
            channel.write({"kind": "activation_boundary", **fields}, deadline)
            reply = channel.read(deadline)
            proof = reply.get("windows_proof_sha256")
            _need(_hash(proof) and _matches(reply, {"kind": "activation_boundary_current", **fields,
                                                   "windows_proof_sha256": proof}),
                  "guest_worker_live_windows_unconfirmed")
            return proof

        result = handle(request, assert_windows_current)
        channel.write({**_scope(request), **result.fields(request.verb)}, time.monotonic() + 2)
        return 0
    result = handle(request)
    raw = _bytes({**_scope(request), **result.fields(request.verb)})
    _need(len(raw) <= FRAME_BYTES, "guest_worker_response_size")
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
