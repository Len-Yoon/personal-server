"""Install generation-bound authority and start exact stopped containers."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import http.client
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import time

from ha_egress.boot import validate_authority as validate_broker_authority
from ha_runtime.config import SERVICES, validate_installed_authority
from ha_runtime.seven_migration import MigrationRejected

from . import native_guest_runtime as runtime
from .native_guest_config import NODES
from .native_guest_live import _probe, _tls
from .legacy_writers import TransitionBlocked, _need
from .native_stdio_bridge import ACTIVATION_GUEST_CHECK_SECONDS, BridgeRejected


LEASE_SAMPLE_FAILURES = frozenset({
    "guest_worker_boot_changed", "guest_worker_installation_changed", "guest_worker_inputs_changed",
    "guest_worker_activation_handoff_changed", "guest_worker_live_windows_unconfirmed",
    "guest_live_pg_binding_unconfirmed", "guest_live_pg_peer_unconfirmed",
    "guest_live_pg_lease_expired", "guest_live_clock_unconfirmed",
    "guest_restore_docker_rejected", "bridge_deadline_exceeded",
})
APP_READY_SECONDS = 25


@dataclass(frozen=True)
class ActivationPins:
    coordinator_intent_sha256: str
    primary_prepared_closed_sha256: str
    standby_prepared_closed_sha256: str
    primary_runtime_receipt_sha256: str
    windows_service_instance_sha256: str
    first_write_proof_sha256: str

    def validate(self):
        _need(all(runtime._match(runtime.SHA, digest) for digest in asdict(self).values())
              and self.primary_prepared_closed_sha256 != self.standby_prepared_closed_sha256,
              "guest_activation_pins_invalid")


@dataclass(frozen=True)
class ServingGeneration:
    primary: str
    epoch: int
    timeline: int
    revision: int

    def validate(self):
        _need(type(self.primary) is str and self.primary in NODES
              and all(type(number) is int for number in (self.epoch, self.timeline, self.revision))
              and self.epoch >= 1 and self.revision >= self.epoch
              and (self == INITIAL_GENERATION or (self.epoch > 1 and self.timeline > 1)),
              "guest_activation_generation_invalid")

    @property
    def partner(self):
        return "B" if self.primary == "A" else "A"


INITIAL_GENERATION = ServingGeneration("A", 1, 1, 2)


def _check_lease(pins, activation, assert_lease, boundary, generation=INITIAL_GENERATION):
    """The caller authenticates the response and holds mutation exclusion."""
    challenge = secrets.token_hex(32)
    expected = {
        **runtime._runtime_identity(pins), **asdict(activation), "challenge": challenge, "boundary": boundary,
        "coordinator_intent_durable": True, "legacy_writers_closed": True,
        "standby_runtime_closed": True, "runtime_mutations_excluded": True,
        **asdict(generation), "partner": generation.partner, "writes_open": True,
    }
    if generation == INITIAL_GENERATION:
        expected["initial_postcommit_verified"] = True
    else:
        expected.update(transition_postcommit_verified=True, previous_primary_fenced=True)
    started = time.monotonic()
    try:
        actual = assert_lease(challenge, boundary)
    except Exception as error:
        reason = str(error) if isinstance(error, (TransitionBlocked, BridgeRejected)) else None
        raise TransitionBlocked(reason if reason in LEASE_SAMPLE_FAILURES
                                else "guest_activation_sample_unconfirmed") from None
    try:
        _need(type(actual) is dict and set(actual) == set(expected) | {"lease_sha256", "observed_at", "expires_at"}
              and all(type(actual[key]) is type(value) and actual[key] == value for key, value in expected.items())
              and runtime._match(runtime.SHA, actual["lease_sha256"]), "guest_activation_lease_unconfirmed")
        timestamps = [actual[key] for key in ("observed_at", "expires_at")]
        _need(all(type(value) is str and value.endswith("Z") for value in timestamps),
              "guest_activation_lease_unconfirmed")
        observed, expires = (datetime.fromisoformat(value[:-1] + "+00:00") for value in timestamps)
        now = datetime.now(timezone.utc)
        _need(0 < (expires - observed).total_seconds() <= 5, "guest_activation_expiry_span")
        _need(now < expires, "guest_activation_expired_during_check")
        _need(-1 <= (now - observed).total_seconds() < 2, "guest_activation_observation_age")
        _need(time.monotonic() - started < ACTIVATION_GUEST_CHECK_SECONDS, "guest_activation_boundary_deadline")
        return actual["lease_sha256"]
    except TransitionBlocked:
        raise
    except Exception:
        raise TransitionBlocked("guest_activation_lease_unconfirmed") from None


def _authorities(pins, prepared, generation):
    installed_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    installed = {"schema_version": 3, "authority_kind": "installed-runtime", "installed_at": installed_at,
                 "installation_id": pins.installation_id}
    authorities = {}
    app_fields = {"cluster", "system_id", "node", "service", "primary_node", "primary", "partner",
                  "profile", "enforcement_sha256", "effects", "installation_id", "generation", *pins.release}
    for service in sorted(SERVICES):
        root = runtime.ROOT / "apps" / service
        config = json.loads(runtime._read(root / "config.json", runtime.APP_UID, {0o400, 0o600}))
        _need(set(config) == app_fields | {"scope"} and config["scope"] == "n100-approved"
              and config["profile"] == "enforced-vm-v1" and config["primary_node"] == generation.primary
              and config["primary"]["host"] == NODES[generation.primary]
              and config["partner"]["host"] == NODES[generation.partner]
              and config["node"] == generation.primary and config["service"] == service and config["cluster"] == pins.cluster
              and config["system_id"] == pins.system_id and config["installation_id"] == pins.installation_id
              and type(config["generation"]) is int and config["generation"] == generation.epoch
              and config["effects"]["plan_sha256"] == pins.plan_sha256
              and all(config[key] == value for key, value in pins.release.items()), "guest_activation_app_unbound")
        authority = {**{key: config[key] for key in app_fields}, **installed,
                     "action": "activate-ha", "nodes": {"A": "ha-a", "B": "ha-b"}}
        validate_installed_authority(authority, pins.installation_id)
        authorities[root / "activation.json"] = runtime._json_bytes(authority)
    broker_root = runtime.ROOT / "broker"
    broker_raw = runtime._read(broker_root / "config.json", 0, {0o444})
    broker = json.loads(broker_raw)
    broker_hash = runtime._digest(broker_raw)
    _need(broker_hash == prepared["broker_sha256"] and broker["node"] == generation.primary
          and broker["scope"] == "n100-approved" and broker["profile"] == "enforced-vm-v1"
          and broker["installation_id"] == pins.installation_id and broker["release"] == pins.release
          and type(broker["generation"]) is int and broker["generation"] == generation.epoch
          and broker["postgres"]["primary"]["host"] == NODES[generation.primary]
          and broker["postgres"]["peer"]["host"] == NODES[generation.partner]
          and broker["binding"]["cluster"] == pins.cluster
          and broker["binding"]["system_id"] == pins.system_id and broker["binding"]["plan_sha256"] == pins.plan_sha256
          and broker["binding"]["broker_instance"] == pins.broker_instance, "guest_activation_broker_unbound")
    authority = {**installed, "action": "start-effect-broker", "source_commit": pins.release["source_commit"],
                 "plan_sha256": pins.plan_sha256, "config_sha256": broker_hash}
    validate_broker_authority(authority, broker, broker_hash)
    authorities[broker_root / "authority.json"] = runtime._json_bytes(authority)
    return authorities


def _state(pins, containers, started, baseline=None):
    observed = runtime._inventory()
    ids = {entry["container_id"] for entry in containers.values()}
    protected = {identifier: item for identifier, item in observed.items() if identifier not in ids}
    _need(len(ids) == len(containers) and runtime.inventory_sha256(protected) == pins.protected_inventory_sha256,
          "guest_activation_protected_inventory_changed")
    contract = {}
    for role, pinned in containers.items():
        item = observed[pinned["container_id"]]
        state = item["State"]
        running = role in started
        _need(item["Image"] == pinned["image_id"] and runtime._service(item) == role
              and item["Config"]["Labels"][runtime.ATTEMPT_LABEL] == pins.attempt_id
              and state["Status"] == ("running" if running else "created") and state["Running"] is running
              and state["Paused"] is False and state["Restarting"] is False and item["RestartCount"] == 0
              and type(state["Pid"]) is int and (state["Pid"] > 0 if running else state["Pid"] == 0)
              and set(item["NetworkSettings"]["Networks"] or {}) <= {item["HostConfig"]["NetworkMode"]},
              "guest_activation_container_state_unconfirmed")
        host = item["HostConfig"].copy()
        # Docker can clear the default false pointer when it starts a container.
        if "OomKillDisable" in host:
            oom_disabled = host["OomKillDisable"]
            _need(oom_disabled is None or type(oom_disabled) is bool, "guest_activation_container_contract_changed")
            if oom_disabled is None:
                host["OomKillDisable"] = False
        contract[role] = {**{key: item[key] for key in ("Image", "Name", "Config")}, "HostConfig": host,
                          "Mounts": sorted(item["Mounts"], key=lambda mount: mount["Destination"])}
    _need(baseline is None or contract == baseline, "guest_activation_container_contract_changed")
    return contract


def _authority_files_match(authorities):
    _need(all(runtime._read(path, 0, {0o444}) == raw for path, raw in authorities.items()),
          "guest_activation_authority_file_changed")


def _wait_for_app(context, leaf, address, service, *, expected_node="A"):
    deadline = time.monotonic() + APP_READY_SECONDS
    while time.monotonic() < deadline:
        try:
            _probe(context, leaf, address, service, deadline, expected_node=expected_node)
        except TransitionBlocked as error:
            if str(error) != "guest_live_route_unready":
                raise
        except ssl.SSLError:
            raise TransitionBlocked("guest_activation_ingress_tls_unconfirmed") from None
        except MigrationRejected:
            raise TransitionBlocked("guest_activation_readiness_response_invalid") from None
        except (OSError, http.client.HTTPException):
            pass
        else:
            _need(time.monotonic() < deadline, "guest_activation_app_unready")
            return
        time.sleep(min(0.7, max(0, deadline - time.monotonic())))
    raise TransitionBlocked("guest_activation_app_unready")


def activate_prepared_a(pins, activation, stage, *, assert_lease):
    """Run the original A-only WSL cutover with its existing receipt format."""
    _need(os.geteuid() == 0 and pins.node == "A", "guest_activation_a_root_required")
    return _activate(pins, activation, stage, INITIAL_GENERATION, assert_lease=assert_lease)


def activate_prepared_generation(pins, activation, stage, generation, *, assert_lease):
    """Start a new primary only with live exclusion, transition and replay evidence."""
    _need(type(generation) is ServingGeneration, "guest_activation_generation_invalid")
    generation.validate()
    _need(os.geteuid() == 0 and pins.node == generation.primary and generation.epoch > 1,
          "guest_activation_generation_scope")
    return _activate(pins, activation, stage, generation, assert_lease=assert_lease)


def _activate(pins, activation, stage, generation, *, assert_lease):
    pins.validate()
    activation.validate()
    generation_fields = {} if generation == INITIAL_GENERATION else {"serving_generation": asdict(generation)}
    stage = Path(stage)
    intent = stage / "runtime-activation-intent.json"
    done = stage / "runtime-activation.json"
    outcome = {"status": "UNKNOWN", "automatic_retry_permitted": False}
    failure = None

    def check(boundary):
        nonlocal failure
        try:
            return _check_lease(pins, activation, assert_lease, boundary, generation)
        except TransitionBlocked as error:
            failure = {"boundary": boundary, "reason": str(error)}
            raise

    try:
        runtime._directory(stage, 0)
        _need(not os.path.lexists(intent) and not os.path.lexists(done), "guest_activation_reconciliation_required")
        prepare_intent, prepare_receipt = stage / "runtime-intent.json", stage / "runtime-prepared.json"
        _need(runtime._inspect_prepared(pins, stage, prepare_intent, prepare_receipt)
              == activation.primary_runtime_receipt_sha256, "guest_activation_preparation_unbound")
        raw = runtime._read(prepare_receipt, 0, {0o600})
        _need(runtime._digest(raw) == activation.primary_runtime_receipt_sha256,
              "guest_activation_preparation_unbound")
        prepared = json.loads(raw)
        containers = prepared["containers"]
        _need(set(containers) == SERVICES | {"ha-broker"}, "guest_activation_preparation_unbound")
        authorities = _authorities(pins, prepared, generation)
        _need(runtime._inspect_prepared(pins, stage, prepare_intent, prepare_receipt)
              == activation.primary_runtime_receipt_sha256, "guest_activation_preparation_unbound")
        baseline = _state(pins, containers, set())
        lease = check("activation_intent")
        order = ["ha-broker", *sorted(SERVICES)]
        artifact_pins = {str(path.relative_to(runtime.ROOT)): runtime._digest(value)
                         for path, value in authorities.items()}
        intent_bytes = runtime._json_bytes({
            "schema_version": 1, "status": "INTENT", **runtime._runtime_identity(pins), **asdict(activation),
            **generation_fields, "activation_order": order, "containers": containers, "authority_files": artifact_pins,
            "lease_sha256": lease,
        })
        runtime._write(intent, intent_bytes)

        def assert_intent():
            _need(runtime._read(intent, 0, {0o600}) == intent_bytes, "guest_activation_intent_changed")

        for path, value in authorities.items():
            check("publish:" + str(path.relative_to(runtime.ROOT)))
            assert_intent()
            runtime._write(path, value, 0o444)
        _authority_files_match(authorities)
        raw_spec = runtime._read(stage / "guest-spec.json", 0, {0o400, 0o600})
        _need(runtime._digest(raw_spec) == pins.spec_sha256, "guest_activation_preparation_unbound")
        spec = json.loads(raw_spec)
        _need(spec["node"] == generation.primary and spec["guest_ip"] == spec["pg_" + generation.primary.lower()],
              "guest_activation_preparation_unbound")
        address = str(IPv4Address(spec["guest_ip"]))
        context, leaf = _tls(prepared)
        started = set()
        for role in order:
            _state(pins, containers, started, baseline)
            _authority_files_match(authorities)
            check("start:" + role)
            assert_intent()
            runtime._docker("start", containers[role]["container_id"], timeout=30)
            started.add(role)
            if role in SERVICES:
                try:
                    _wait_for_app(context, leaf, address, role, expected_node=generation.primary)
                except TransitionBlocked as error:
                    failure = {"boundary": "ready:" + role, "reason": str(error)}
                    raise
        _state(pins, containers, started, baseline)
        _authority_files_match(authorities)
        lease = check("guest_started")
        assert_intent()
        receipt = {
            "schema_version": 1, "status": "STARTED", **runtime._runtime_identity(pins), **asdict(activation),
            **generation_fields, "guest_intent_sha256": runtime._digest(intent_bytes), "lease_sha256": lease,
            "authority_files": artifact_pins, "containers": containers, "automatic_retry_permitted": False,
        }
        runtime._write(done, runtime._json_bytes(receipt))
        return receipt
    except (TransitionBlocked, MigrationRejected, OSError, KeyError, TypeError, ValueError, subprocess.TimeoutExpired):
        if failure is not None:
            try:
                runtime._write(stage / "runtime-activation-failure.json", runtime._json_bytes({
                    "schema_version": 1, "attempt_id": pins.attempt_id,
                    "prepared_receipt_sha256": activation.primary_runtime_receipt_sha256, **failure,
                }))
            except OSError:
                pass
        return outcome
