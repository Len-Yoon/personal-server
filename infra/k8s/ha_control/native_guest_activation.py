"""A-only installed-authority publication and exact stopped-container start."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import time

from ha_egress.boot import validate_authority as validate_broker_authority
from ha_runtime.config import SERVICES, validate_installed_authority

from . import native_guest_runtime as runtime
from .legacy_writers import TransitionBlocked, _need


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


def _check_lease(pins, activation, assert_lease, boundary):
    """The caller authenticates the response and holds mutation exclusion."""
    challenge = secrets.token_hex(32)
    expected = {
        **runtime._runtime_identity(pins), **asdict(activation), "challenge": challenge, "boundary": boundary,
        "coordinator_intent_durable": True, "initial_postcommit_verified": True, "legacy_writers_closed": True,
        "standby_runtime_closed": True, "runtime_mutations_excluded": True,
        "epoch": 1, "timeline": 1, "revision": 2, "primary": "A", "partner": "B", "writes_open": True,
    }
    started = time.monotonic()
    try:
        actual = assert_lease(challenge, boundary)
        _need(type(actual) is dict and set(actual) == set(expected) | {"lease_sha256", "observed_at", "expires_at"}
              and all(type(actual[key]) is type(value) and actual[key] == value for key, value in expected.items())
              and runtime._match(runtime.SHA, actual["lease_sha256"]), "guest_activation_lease_unconfirmed")
        timestamps = [actual[key] for key in ("observed_at", "expires_at")]
        _need(all(type(value) is str and value.endswith("Z") for value in timestamps),
              "guest_activation_lease_unconfirmed")
        observed, expires = (datetime.fromisoformat(value[:-1] + "+00:00") for value in timestamps)
        now = datetime.now(timezone.utc)
        _need(0 < (expires - observed).total_seconds() <= 5
              and -1 <= (now - observed).total_seconds() < 2
              and now < expires and time.monotonic() - started < 2, "guest_activation_lease_unconfirmed")
        return actual["lease_sha256"]
    except Exception:
        raise TransitionBlocked("guest_activation_lease_unconfirmed") from None


def _authorities(pins, prepared):
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
              and config["profile"] == "enforced-vm-v1" and config["primary_node"] == "A"
              and config["primary"]["host"] == "ha-a" and config["partner"]["host"] == "ha-b"
              and config["node"] == "A" and config["service"] == service and config["cluster"] == pins.cluster
              and config["system_id"] == pins.system_id and config["installation_id"] == pins.installation_id
              and type(config["generation"]) is int and config["generation"] == 1
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
    _need(broker_hash == prepared["broker_sha256"] and broker["node"] == "A"
          and broker["scope"] == "n100-approved" and broker["profile"] == "enforced-vm-v1"
          and broker["installation_id"] == pins.installation_id and broker["release"] == pins.release
          and broker["generation"] == 1 and broker["binding"]["cluster"] == pins.cluster
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
        contract[role] = {**{key: item[key] for key in ("Image", "Name", "Config", "HostConfig")},
                          "Mounts": sorted(item["Mounts"], key=lambda mount: mount["Destination"])}
    _need(baseline is None or contract == baseline, "guest_activation_container_contract_changed")
    return contract


def _authority_files_match(authorities):
    _need(all(runtime._read(path, 0, {0o444}) == raw for path, raw in authorities.items()),
          "guest_activation_authority_file_changed")


def activate_prepared_a(pins, activation, stage, *, assert_lease):
    """Publish trusted A-only activation files and start eight exact A container IDs.

    The caller authenticates the coordinator intent, both prepared-closed outcomes
    and each Windows service response. It holds exclusive native mutation control
    through this operation. Failure leaves the durable intent for reconciliation.
    """
    _need(os.geteuid() == 0 and pins.node == "A", "guest_activation_a_root_required")
    pins.validate()
    activation.validate()
    stage = Path(stage)
    intent = stage / "runtime-activation-intent.json"
    done = stage / "runtime-activation.json"
    outcome = {"status": "UNKNOWN", "automatic_retry_permitted": False}
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
        authorities = _authorities(pins, prepared)
        _need(runtime._inspect_prepared(pins, stage, prepare_intent, prepare_receipt)
              == activation.primary_runtime_receipt_sha256, "guest_activation_preparation_unbound")
        baseline = _state(pins, containers, set())
        lease = _check_lease(pins, activation, assert_lease, "activation_intent")
        order = ["ha-broker", *sorted(SERVICES)]
        artifact_pins = {str(path.relative_to(runtime.ROOT)): runtime._digest(value)
                         for path, value in authorities.items()}
        intent_bytes = runtime._json_bytes({
            "schema_version": 1, "status": "INTENT", **runtime._runtime_identity(pins), **asdict(activation),
            "activation_order": order, "containers": containers, "authority_files": artifact_pins,
            "lease_sha256": lease,
        })
        runtime._write(intent, intent_bytes)
        for path, value in authorities.items():
            _check_lease(pins, activation, assert_lease, "publish:" + str(path.relative_to(runtime.ROOT)))
            runtime._write(path, value, 0o444)
        _authority_files_match(authorities)
        started = set()
        for role in order:
            _state(pins, containers, started, baseline)
            _authority_files_match(authorities)
            _check_lease(pins, activation, assert_lease, "start:" + role)
            runtime._docker("start", containers[role]["container_id"], timeout=30)
            started.add(role)
        _state(pins, containers, started, baseline)
        _authority_files_match(authorities)
        lease = _check_lease(pins, activation, assert_lease, "guest_started")
        _need(runtime._read(intent, 0, {0o600}) == intent_bytes, "guest_activation_intent_changed")
        receipt = {
            "schema_version": 1, "status": "STARTED", **runtime._runtime_identity(pins), **asdict(activation),
            "guest_intent_sha256": runtime._digest(intent_bytes), "lease_sha256": lease,
            "authority_files": artifact_pins, "containers": containers, "automatic_retry_permitted": False,
        }
        runtime._write(done, runtime._json_bytes(receipt))
        return receipt
    except (TransitionBlocked, OSError, KeyError, TypeError, ValueError, subprocess.TimeoutExpired):
        return outcome
