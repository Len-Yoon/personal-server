"""Prepare the exact VM guest app and broker containers in the stopped state."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import stat
import subprocess

from ha_runtime.config import SERVICES
from ha_runtime.seven_capture import stamp

from . import native_guest_config as config
from .legacy_writers import TransitionBlocked, _need
from .native_guest_restore import BOOT_ID, INSTALLATION, SHA, _directory, _docker, _fsync_directory, _inventory
from .native_guest_restore import _json_bytes, inventory_sha256


ROOT = Path("/etc/n100-ha")
RUN = Path("/run/n100-ha")
DOCKER_SOCKET = Path("/run/docker.sock")
RENDERER = Path(__file__).resolve().parents[1] / "ha-package" / "guest.py"
ATTEMPT_LABEL = "n100-ha.runtime-attempt"
APP_UID = 10001
BROKER_UID = 10002
GATEWAY_UID = 10003
INGRESS_UID = 10004
LIVE_BASE = {"postgres", "ha-network", "ha-ingress"}
APP_PUBLIC = ("pg-ca.crt", "broker-ca.crt", "broker-client.crt")
BROKER_PUBLIC = ("pg-ca.crt", "broker-ca.crt", "broker.crt", "ingress-server-ca.crt", "ingress-client.crt")
BROKER_PRIVATE = ("postgres.pgpass", "broker.key", "ingress-client.key")
PROVIDERS = ("portal-web-telegram.json", "crawler-worker-telegram.json", "car-care-worker-telegram.json",
             "hyundai.json", "yes24.json", "executor.json")


@dataclass(frozen=True)
class RuntimePins:
    node: str
    boot_sha256: str
    installation_id: str
    protected_inventory_sha256: str
    engine_id_sha256: str
    renderer_sha256: str
    spec_sha256: str
    inputs_sha256: str
    plan_sha256: str
    manifest_sha256: str
    frozen_transfer_sha256: str
    restore_receipt_sha256: str
    attempt_id: str
    release: dict
    system_id: str
    cluster: str
    broker_instance: str

    def validate(self):
        digests = (self.boot_sha256, self.installation_id, self.protected_inventory_sha256, self.engine_id_sha256,
                   self.renderer_sha256, self.spec_sha256, self.inputs_sha256, self.plan_sha256, self.manifest_sha256,
                   self.frozen_transfer_sha256, self.restore_receipt_sha256, self.attempt_id, self.broker_instance)
        _need(self.node in ("A", "B") and all(_match(SHA, value) for value in digests),
              "guest_runtime_pins_invalid")
        config._guest_identity(self.cluster, self.release, self.plan_sha256, self.node, self.installation_id, self.system_id)


def _match(pattern, value):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _read(path, uid, modes, *, limit=1024 * 1024, gids=None):
    _directory(path.parent, 0, private=False)
    info = path.lstat()
    _need(stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_nlink == 1
          and stat.S_IMODE(info.st_mode) in modes and 0 < info.st_size <= limit
          and (gids is None or info.st_mode & 0o004 or info.st_gid in gids), "guest_runtime_input_untrusted")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        _need(stamp(os.fstat(stream.fileno())) == stamp(info), "guest_runtime_input_changed")
        raw = stream.read(limit + 1)
        _need(stamp(os.fstat(stream.fileno())) == stamp(info), "guest_runtime_input_changed")
    _need(stamp(path.lstat()) == stamp(info) and len(raw) == info.st_size, "guest_runtime_input_changed")
    return raw


def _renderer(pins):
    raw = _read(RENDERER, 0, {0o400, 0o444, 0o500, 0o544, 0o644, 0o755})
    _need(_digest(raw) == pins.renderer_sha256, "guest_runtime_renderer_mismatch")
    namespace = {"__name__": "_native_guest_layout"}
    exec(compile(raw, str(RENDERER), "exec"), namespace)
    return namespace


def _cert_digest(raw):
    matches = re.search(rb"-----BEGIN CERTIFICATE-----\r?\n[\sA-Za-z0-9+/=]+-----END CERTIFICATE-----", raw)
    _need(matches is not None, "guest_runtime_certificate_invalid")
    return _digest(ssl.PEM_cert_to_DER_cert(matches[0].decode("ascii")))


def _password(raw, role):
    lines = raw.decode("ascii").splitlines()
    _need(len(lines) == 2, "guest_runtime_pgpass_invalid")
    password = lines[0].rpartition(":")[2]
    _need(_match(config.PASSWORD, password)
          and raw == "".join(f"{host}:5432:ha:{role}:{password}\n" for host in ("ha-a", "ha-b")).encode(),
          "guest_runtime_pgpass_invalid")
    return password


def _traversable(path, groups):
    _directory(path, 0, private=False)
    info = path.lstat()
    _need(info.st_mode & 0o001 or info.st_mode & 0o010 and info.st_gid in groups,
          "guest_runtime_mount_inaccessible")


def _inputs(pins, renderer, spec, haproxy, runtime_ownership, *, prepared=False):
    digests = {}

    def read(path, uid=0, *, private=False, gids=None):
        modes = {0o400, 0o600} if private else {0o440, 0o444}
        raw = _read(path, uid, modes, gids=gids)
        digests[str(path.relative_to(ROOT))] = _digest(raw)
        return raw

    broker = ROOT / "broker"
    ingress = ROOT / "ingress"
    broker_groups = {BROKER_UID, spec["docker_gid"]}
    _traversable(broker, broker_groups)
    _traversable(broker / "providers", broker_groups)
    _need(not os.path.lexists(broker / "authority.json")
          and (prepared or not os.path.lexists(broker / "config.json")),
          "guest_runtime_broker_not_closed")
    broker_certificate = _cert_digest(read(broker / "broker.crt", gids=broker_groups))
    for name in BROKER_PUBLIC:
        if name != "broker.crt":
            read(broker / name, gids=broker_groups)
    broker_passfile = read(broker / "postgres.pgpass", BROKER_UID, private=True)
    for name in BROKER_PRIVATE:
        if name != "postgres.pgpass":
            read(broker / name, BROKER_UID, private=True)
    for name in PROVIDERS:
        value = json.loads(read(broker / "providers" / name, BROKER_UID, private=True))
        _need(type(value) is dict and bool(value), "guest_runtime_provider_input_invalid")

    _traversable(ingress, {INGRESS_UID})
    _need(read(ingress / "haproxy.cfg", gids={INGRESS_UID}) == haproxy.encode(), "guest_runtime_ingress_drift")
    ingress_certificate = _cert_digest(read(ingress / "server.pem", INGRESS_UID, private=True))
    read(ingress / "clients-ca.crt", gids={INGRESS_UID})
    gateway = ROOT / "gateway"
    _traversable(gateway, {APP_UID, GATEWAY_UID})
    _need(_digest(read(gateway / "config.json", gids={APP_UID, GATEWAY_UID})) == spec["gateway_sha256"],
          "guest_runtime_gateway_drift")

    clients = {}
    identity = dict(cluster=pins.cluster, release=pins.release, plan_sha256=pins.plan_sha256,
                    node=pins.node, installation_id=pins.installation_id, system_id=pins.system_id)
    for service in sorted(SERVICES):
        root = ROOT / "apps" / service
        groups = {APP_UID, INGRESS_UID}
        _traversable(root, groups)
        _need(not os.path.lexists(root / "activation.json"), "guest_runtime_app_not_closed")
        actual = json.loads(read(root / "config.json", APP_UID, private=True))
        passfile = read(root / "postgres.pgpass", APP_UID, private=True)
        role = service.replace("-", "_") + "_" + pins.node.lower()
        expected, expected_passfile = config.application_config(
            **identity, service=service, password=_password(passfile, role), broker_server_sha256=broker_certificate)
        _need(actual == expected and passfile == expected_passfile, "guest_runtime_application_config_mismatch")
        for name in APP_PUBLIC:
            raw = read(root / name, gids=groups)
            if name == "broker-client.crt":
                clients[service] = _cert_digest(raw)
        read(root / "broker-client.key", APP_UID, private=True)
        if service in renderer["AUTH_INPUTS"]:
            required, optional = map(set, renderer["AUTH_INPUTS"][service])
            auth = json.loads(read(root / "runtime-auth.json", APP_UID, private=True))
            _need(type(auth) is dict and required <= set(auth) <= required | optional
                  and all(type(value) is str and value.strip() and len(value) <= 4096 and "\0" not in value
                          for value in auth.values()), "guest_runtime_application_auth_invalid")
        if service == "homeops-executor":
            _need(read(root / "runtime-ownership.conf", gids=groups) == runtime_ownership.encode(),
                  "guest_runtime_ownership_config_mismatch")
    _need(_digest(_json_bytes(digests)) == pins.inputs_sha256, "guest_runtime_inputs_pin_mismatch")
    return identity, clients, ingress_certificate, broker_passfile


def _environment(rows):
    if rows is None:
        return {}
    _need(type(rows) is list, "guest_runtime_environment_invalid")
    environment = {}
    for row in rows:
        _need(type(row) is str, "guest_runtime_environment_invalid")
        key, separator, value = row.partition("=")
        _need(separator and key and key not in environment, "guest_runtime_environment_invalid")
        environment[key] = value
    return environment


def _images(spec):
    images = {}
    for name, reference in spec["images"].items():
        _need(_match(r"sha256:[0-9a-f]{64}", reference), "guest_runtime_image_pin_required")
        fields = "{{.Id}} {{.Architecture}} {{.Os}} {{json .Config.Env}}"
        parts = _docker("image", "inspect", "--format", fields, reference).decode().split(maxsplit=3)
        _need(len(parts) == 4 and parts[:3] == [reference, "amd64", "linux"], "guest_runtime_image_mismatch")
        images[name] = {"id": reference, "environment": _environment(json.loads(parts[3]))}
    return images


def _service(item):
    return (item["Config"]["Labels"] or {}).get("com.docker.compose.service")


def _protected(pins, images, layouts):
    objects = _inventory()
    _need(inventory_sha256(objects) == pins.protected_inventory_sha256, "guest_runtime_protected_state_changed")
    _need(not any(_service(item) in SERVICES | {"ha-broker"} for item in objects.values()),
          "guest_runtime_existing_application_container")
    for service in LIVE_BASE:
        selected = [item for item in objects.values() if _service(item) == service]
        _need(len(selected) == 1 and selected[0]["State"]["Running"] is True, "guest_runtime_prerequisite_missing")
        if service != "postgres":
            utility = "ha-backup-utility" if service == "ha-network" else "ha-ingress-utility"
            item, layout = selected[0], layouts[service]
            host = item["HostConfig"]
            _need(item["Image"] == images[utility]["id"] and item["Config"]["User"] == layout["user"]
                  and item["Config"]["Cmd"] == layout["command"] and host["NetworkMode"] == layout["network_mode"]
                  and host["ReadonlyRootfs"] is True and host["CapDrop"] == ["ALL"]
                  and host["RestartPolicy"]["Name"] == "no" and _binds_match(item, layout),
                  "guest_runtime_prerequisite_changed")
            if "entrypoint" in layout:
                _need(item["Config"]["Entrypoint"] == layout["entrypoint"], "guest_runtime_prerequisite_changed")
    return objects


def _sockets(renderer):
    parent = RUN / "network"
    _directory(parent.parent, 0, private=False)
    for path in (parent, *(parent / service for service in SERVICES)):
        info = path.lstat()
        _need(stat.S_ISDIR(info.st_mode) and info.st_uid == GATEWAY_UID and info.st_gid == APP_UID
              and stat.S_IMODE(info.st_mode) == 0o750, "guest_runtime_gateway_socket_dir_mismatch")
    for service in renderer["SERVICES"]:
        for channel in ("pg-a", "pg-b", "broker"):
            info = (parent / service / (channel + ".sock")).lstat()
            _need(stat.S_ISSOCK(info.st_mode) and info.st_uid == GATEWAY_UID and info.st_gid == APP_UID
                  and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o660,
                  "guest_runtime_gateway_socket_mismatch")


def _write(path, raw, mode=0o600):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fchmod(stream.fileno(), mode)
        os.fsync(stream.fileno())
    _fsync_directory(path.parent)


def _assert_ingress_dir(directory):
    info = directory.lstat()
    _need(stat.S_ISDIR(info.st_mode) and info.st_uid == APP_UID and info.st_gid == INGRESS_UID
          and stat.S_IMODE(info.st_mode) == 0o750 and not any(directory.iterdir()),
          "guest_runtime_ingress_socket_dir_mismatch")


def _ingress_dirs():
    parent = RUN / "ingress"
    _directory(parent, 0, private=False)
    _need(not os.path.lexists(parent / "portal-web"), "guest_runtime_frozen_ingress_still_present")
    for service in sorted(SERVICES):
        directory = parent / service
        if not os.path.lexists(directory):
            directory.mkdir(mode=0o700)
            os.chown(directory, APP_UID, INGRESS_UID, follow_symlinks=False)
            os.chmod(directory, 0o750)
        _assert_ingress_dir(directory)
        _fsync_directory(directory)
    _fsync_directory(parent)


def _guard(pins, assert_guard):
    expected = {
        "node": pins.node, "installation_id": pins.installation_id, "attempt_id": pins.attempt_id,
        "plan_sha256": pins.plan_sha256, "manifest_sha256": pins.manifest_sha256,
        "frozen_transfer_sha256": pins.frozen_transfer_sha256, "restore_receipt_sha256": pins.restore_receipt_sha256,
        "legacy_writers_closed": True, "target_writes_closed": True, "frozen_transfer_closed": True,
    }
    actual = assert_guard()
    _need(type(actual) is dict and actual.keys() == expected.keys()
          and all(type(actual[key]) is type(value) and actual[key] == value for key, value in expected.items()),
          "guest_runtime_live_guard_rejected")


def _compose_bytes(project, layouts, pins):
    services = {name: {**layout, "labels": {ATTEMPT_LABEL: pins.attempt_id}} for name, layout in layouts.items()}
    return _json_bytes({"name": project, "services": services})


def _compose(stage, project, filename, layouts, pins):
    path = stage / filename
    _write(path, _compose_bytes(project, layouts, pins))
    _docker("compose", "--project-directory", str(stage), "--env-file", "/dev/null", "--project-name", project,
            "--file", str(path), "create", "--no-build", "--pull", "never", "--no-recreate", *sorted(layouts),
            timeout=120)


def _binds_match(item, layout):
    expected = {(mount["source"], mount["target"], not mount["read_only"],
                 mount["bind"].get("propagation", "rprivate")) for mount in layout["volumes"]}
    actual = {(mount["Source"], mount["Destination"], mount["RW"], mount["Propagation"])
              for mount in item["Mounts"] if mount["Type"] == "bind"}
    return actual == expected and all(mount["Type"] == "bind" or
        mount["Type"] == "tmpfs" and mount["Destination"] == "/tmp" for mount in item["Mounts"])


def _assert_created(item, project, service, image, layout, pins):
    host, actual = item["HostConfig"], item["Config"]
    labels = actual["Labels"]
    environment = {**image["environment"], **layout["environment"]}
    # Compose sends aliases as host:address strings and tmpfs as a path/options map.
    extra_hosts = sorted(f"{name}:{address}" for name, address in layout["extra_hosts"].items())
    tmpfs = dict(mount.split(":", 1) for mount in layout["tmpfs"])
    _need(labels["com.docker.compose.project"] == project and _service(item) == service
          and labels[ATTEMPT_LABEL] == pins.attempt_id and item["Image"] == image["id"]
          and item["State"]["Status"] == "created" and item["State"]["Running"] is False and item["State"]["Pid"] == 0
          and item["RestartCount"] == 0 and actual["User"] == layout["user"] and actual["Cmd"] == layout["command"]
          and actual["Entrypoint"] == layout.get("entrypoint")
          and host["ReadonlyRootfs"] is True and host["NetworkMode"] == layout["network_mode"]
          and host["Privileged"] is False and host["PidMode"] == "" and host["PidsLimit"] == layout["pids_limit"]
          and host["IpcMode"] == "private" and host["UTSMode"] == ""
          and host["CapDrop"] == ["ALL"] and not host["CapAdd"] and host["RestartPolicy"]["Name"] == "no"
          and host["SecurityOpt"] in (["no-new-privileges:true"], ["no-new-privileges=true"], ["no-new-privileges"])
          and host["GroupAdd"] == layout["group_add"] and not host["PortBindings"] and not host["Devices"]
          and not host.get("DeviceRequests") and not host.get("DeviceCgroupRules")
          and sorted(host["ExtraHosts"]) == extra_hosts and host["Tmpfs"] == tmpfs
          and _environment(actual["Env"]) == environment
          and set(item["NetworkSettings"]["Networks"] or {}) <= {layout["network_mode"]},
          "guest_runtime_created_container_mismatch")
    _need(_binds_match(item, layout), "guest_runtime_created_mount_mismatch")


def _created(pins, project, layouts, images, previous=None):
    objects = _inventory()
    owned = {}
    for identifier, item in objects.items():
        if (item["Config"]["Labels"] or {}).get(ATTEMPT_LABEL) == pins.attempt_id:
            service = _service(item)
            _need(service in layouts and service not in owned and _match(SHA, identifier),
                  "guest_runtime_created_inventory_mismatch")
            image = images["ha-backup-utility" if service == "ha-broker" else service]
            _assert_created(item, project, service, image, layouts[service], pins)
            owned[service] = {"container_id": identifier, "image_id": image["id"]}
    protected = {key: value for key, value in objects.items() if key not in
                 {item["container_id"] for item in owned.values()}}
    _need(set(owned) == set(layouts) and inventory_sha256(protected) == pins.protected_inventory_sha256
          and (previous is None or all(owned[service] == item for service, item in previous.items())),
          "guest_runtime_created_inventory_mismatch")
    return owned


def _preflight(pins, stage, *, prepared=False):
    _directory(stage, 0)
    _need(_digest(BOOT_ID.read_bytes()) == pins.boot_sha256, "guest_runtime_boot_changed")
    identity = json.loads(_read(INSTALLATION, 0, {0o400, 0o600}))
    _need(identity["installation_id"] == pins.installation_id and identity["node"] == pins.node,
          "guest_runtime_installation_changed")
    spec_raw = _read(stage / "guest-spec.json", 0, {0o400, 0o600})
    _need(_digest(spec_raw) == pins.spec_sha256, "guest_runtime_spec_pin_mismatch")
    spec = json.loads(spec_raw)
    _need(spec["source_commit"] == pins.release["source_commit"] and spec["installation_id"] == pins.installation_id
          and spec["node"] == pins.node and spec["guest_ip"] == spec["pg_a" if pins.node == "A" else "pg_b"],
          "guest_runtime_spec_scope_mismatch")
    _need(_match(r"-?[1-9][0-9]{0,19}", spec["app_environment"]["car-care-worker"]["CAR_CARE_TELEGRAM_CHAT_ID"]),
          "guest_runtime_application_environment_invalid")
    renderer = _renderer(pins)
    compose, haproxy, review = renderer["render"](spec)
    images = _images(spec)
    if not prepared:
        _protected(pins, images, compose["services"])
    _sockets(renderer)
    _directory(RUN / "ingress", 0, private=False)
    sock = DOCKER_SOCKET.lstat()
    _need(stat.S_ISSOCK(sock.st_mode) and sock.st_uid == 0 and sock.st_gid == spec["docker_gid"]
          and Path("/var/run/docker.sock").resolve(strict=True) == DOCKER_SOCKET,
          "guest_runtime_engine_socket_mismatch")
    engine_id = _docker("info", "--format", "{{.ID}}").decode().strip()
    _need(_digest(engine_id.encode()) == pins.engine_id_sha256, "guest_runtime_engine_changed")
    inputs = _inputs(pins, renderer, spec, haproxy, review["runtime_ownership_file"], prepared=prepared)
    return renderer, spec, compose, images, engine_id, inputs


def preflight_runtime(pins, stage):
    """Validate the installed guest inputs without touching runtime state."""
    _need(os.geteuid() == 0, "guest_runtime_root_required")
    pins.validate()
    try:
        _preflight(pins, Path(stage))
    except (OSError, KeyError, TypeError, ValueError, subprocess.TimeoutExpired):
        raise TransitionBlocked("guest_runtime_preflight_rejected") from None
    return {"status": "PREFLIGHT_PASS", "node": pins.node, "installation_id": pins.installation_id,
            "attempt_id": pins.attempt_id, "inputs_sha256": pins.inputs_sha256,
            "protected_inventory_sha256": pins.protected_inventory_sha256,
            "frozen_socket_released": not os.path.lexists(RUN / "ingress" / "portal-web")}


def _runtime_identity(pins):
    return {"node": pins.node, "installation_id": pins.installation_id, "attempt_id": pins.attempt_id,
            "cluster": pins.cluster, "system_id_sha256": _digest(pins.system_id.encode()),
            "source_commit": pins.release["source_commit"], "release_id": pins.release["release_id"],
            "source_files_sha256": pins.release["source_files_sha256"], "plan_sha256": pins.plan_sha256,
            "manifest_sha256": pins.manifest_sha256, "frozen_transfer_sha256": pins.frozen_transfer_sha256,
            "restore_receipt_sha256": pins.restore_receipt_sha256, "boot_sha256": pins.boot_sha256,
            "engine_id_sha256": pins.engine_id_sha256, "protected_inventory_sha256": pins.protected_inventory_sha256,
            "renderer_sha256": pins.renderer_sha256, "spec_sha256": pins.spec_sha256,
            "inputs_sha256": pins.inputs_sha256, "broker_instance": pins.broker_instance}


def _runtime_intent(pins):
    return {"schema_version": 1, "status": "INTENT", **_runtime_identity(pins)}


def _prepared_receipt(pins, project, owned, broker_sha256):
    return {"schema_version": 1, "status": "PREPARED", **_runtime_identity(pins), "project": project,
            "containers": owned, "broker_sha256": broker_sha256, "writer_authority_issued": False,
            "applications_started": False}


def _broker_bundle(pins, renderer, spec, engine_id, inputs, stopped):
    identity, clients, ingress_cert, passfile = inputs
    managed = spec["app_environment"]["homeops-executor"]["HOMEOPS_DOCKER_MANAGED_SERVICES"].split(",")
    broker, expected_passfile = config.broker_config(
        **identity, broker_instance=pins.broker_instance, password=_password(passfile, config.BROKER_ROLE),
        client_sha256=clients, ingress_server_sha256=ingress_cert, engine_id=engine_id,
        targets={service: stopped[service] for service in managed},
    )
    _need(passfile == expected_passfile, "guest_runtime_broker_passfile_mismatch")
    raw = _json_bytes(broker)
    broker_sha256 = _digest(raw)
    bound, _, _ = renderer["render"]({**spec, "broker_sha256": broker_sha256})
    return raw, broker_sha256, {"ha-broker": bound["services"]["ha-broker"]}


def _settlement(pins, assert_settled):
    challenge = secrets.token_hex(32)
    expected = {**_runtime_identity(pins), "challenge": challenge, "runtime_mutations_excluded": True,
                "runtime_mutations_settled": True, "target_writes_closed": True, "target_never_opened": True}
    try:
        actual = assert_settled(challenge)
    except Exception:
        raise TransitionBlocked("guest_runtime_settlement_unconfirmed") from None
    _need(type(actual) is dict and set(actual) == {*expected, "evidence_sha256"}
          and all(type(actual[key]) is type(value) and actual[key] == value for key, value in expected.items())
          and _match(SHA, actual["evidence_sha256"]), "guest_runtime_settlement_unconfirmed")
    return actual["evidence_sha256"]


def _inspect_prepared(pins, stage, intent, prepared):
    _need(_read(intent, 0, {0o600}) == _json_bytes(_runtime_intent(pins)), "guest_runtime_intent_unconfirmed")
    receipt = _read(prepared, 0, {0o600})
    renderer, spec, compose, images, engine_id, inputs = _preflight(pins, stage, prepared=True)
    project = compose["name"]
    apps = {service: compose["services"][service] for service in SERVICES}
    containers = json.loads(receipt)["containers"]
    stopped = {service: containers[service] for service in SERVICES}
    raw, broker_sha256, broker_layout = _broker_bundle(pins, renderer, spec, engine_id, inputs, stopped)
    _need(_read(ROOT / "broker" / "config.json", 0, {0o444}) == raw
          and _read(stage / "apps.json", 0, {0o600}) == _compose_bytes(project, apps, pins)
          and _read(stage / "broker.json", 0, {0o600}) == _compose_bytes(project, broker_layout, pins),
          "guest_runtime_prepared_files_unconfirmed")
    for service in sorted(SERVICES):
        _assert_ingress_dir(RUN / "ingress" / service)
    owned = _created(pins, project, {**apps, **broker_layout}, images, stopped)
    _need(receipt == _json_bytes(_prepared_receipt(pins, project, owned, broker_sha256)),
          "guest_runtime_prepared_receipt_unconfirmed")
    return _digest(receipt)


def observe_runtime_outcome(pins, stage, *, assert_settled):
    """Inspect stopped preparation while the caller excludes mutation through use of the result.

    The callback authenticates live settlement and closed PostgreSQL authority.
    A stopped client or elapsed timeout alone cannot prove daemon-side settlement.
    """
    _need(os.geteuid() == 0, "guest_runtime_root_required")
    pins.validate()
    stage = Path(stage)
    intent, prepared = stage / "runtime-intent.json", stage / "runtime-prepared.json"
    outcome = {"status": "UNKNOWN", "automatic_retry_permitted": False}
    try:
        before = _settlement(pins, assert_settled)
        if os.path.lexists(intent):
            receipt_sha256 = _inspect_prepared(pins, stage, intent, prepared)
            settled = _settlement(pins, assert_settled)
            _need(settled != before, "guest_runtime_settlement_unconfirmed")
            return {**outcome, "status": "PREPARED_CLOSED", "runtime_receipt_sha256": receipt_sha256,
                    "settlement_sha256": settled}
        _preflight(pins, stage)
        settled = _settlement(pins, assert_settled)
        residue = (intent, prepared, stage / "apps.json", stage / "broker.json", ROOT / "broker" / "config.json",
                   *(RUN / "ingress" / service for service in SERVICES))
        _need(settled != before and not any(os.path.lexists(path) for path in residue),
              "guest_runtime_settlement_unconfirmed")
        return {**outcome, "status": "CLEAN", "settlement_sha256": settled}
    except (TransitionBlocked, OSError, KeyError, TypeError, ValueError, subprocess.TimeoutExpired):
        return outcome


def prepare_runtime(pins, stage, *, assert_guard):
    """Create stopped app and broker containers. A later held authority owns any start."""
    _need(os.geteuid() == 0, "guest_runtime_root_required")
    pins.validate()
    stage = Path(stage)
    intent = stage / "runtime-intent.json"
    _need(not os.path.lexists(intent), "guest_runtime_reconciliation_required")
    try:
        renderer, spec, compose, images, engine_id, inputs = _preflight(pins, stage)
        _guard(pins, assert_guard)
        _need(not os.path.lexists(RUN / "ingress" / "portal-web"), "guest_runtime_frozen_ingress_still_present")
        _write(intent, _json_bytes(_runtime_intent(pins)))
    except FileExistsError:
        raise TransitionBlocked("guest_runtime_reconciliation_required") from None
    except (OSError, KeyError, TypeError, ValueError, subprocess.TimeoutExpired):
        raise TransitionBlocked("guest_runtime_preflight_rejected") from None
    try:
        _ingress_dirs()
        _guard(pins, assert_guard)
        apps = {service: compose["services"][service] for service in SERVICES}
        project = compose["name"]
        _compose(stage, project, "apps.json", apps, pins)
        stopped = _created(pins, project, apps, images)
        raw, broker_sha256, broker_layout = _broker_bundle(pins, renderer, spec, engine_id, inputs, stopped)
        _write(ROOT / "broker" / "config.json", raw, 0o444)
        _guard(pins, assert_guard)
        _compose(stage, project, "broker.json", broker_layout, pins)
        owned = _created(pins, project, {**apps, **broker_layout}, images, stopped)
        _guard(pins, assert_guard)
        result = _prepared_receipt(pins, project, owned, broker_sha256)
        _write(stage / "runtime-prepared.json", _json_bytes(result))
        return result
    except (TransitionBlocked, OSError, KeyError, TypeError, ValueError, subprocess.TimeoutExpired):
        raise TransitionBlocked("guest_runtime_prepare_outcome_unconfirmed") from None
