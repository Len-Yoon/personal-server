"""Run one closed seven-service migration on the pinned A guest."""

from dataclasses import asdict, dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time

from ha_runtime.config import SERVICES
from ha_runtime.seven_capture import stamp
from ha_runtime.seven_migration import MigrationRejected, read_verified

from .frozen_transport import NAME, _catalog, _publish
from .legacy_writers import TransitionBlocked, _need


BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
INSTALLATION = Path("/etc/n100-ha-bootstrap-intent.json")
INITIALIZER = Path("/etc/n100-ha/pg-bootstrap/initialize.py")
PG_SOCKET = Path("/run/n100-ha/pg")
JOURNAL = Path("/var/lib/n100-ha/restore")
DOCKER = ("/usr/bin/docker", "--host", "unix:///var/run/docker.sock")
SHA = re.compile(r"[0-9a-f]{64}\Z")
LSN = re.compile(r"[0-9A-F]{1,8}/[0-9A-F]{1,8}\Z")
TIMEOUT = 1800
SQLITE_STAGE_BYTES = 128 * 1024 * 1024
INITIAL_STATE = """
SELECT json_build_array(session_user, current_database(), current_setting('cluster_name'),
    pg_is_in_recovery(), system_identifier::text, current_setting('transaction_read_only'),
    current_setting('synchronous_commit'),
    regexp_replace(current_setting('synchronous_standby_names'), '[[:space:]"]', '', 'g'),
    (SELECT count(*) FROM pg_namespace WHERE nspname IN ('ha','ha_effects') OR left(nspname,2)='s_'))
FROM pg_control_system()
"""
PEER_STATE = """
SELECT COALESCE(json_agg(json_build_array(host(r.client_addr), r.replay_lsn::text)), '[]'::json)
FROM pg_stat_replication r JOIN pg_stat_ssl tls USING (pid)
WHERE r.application_name='ha_b' AND r.usename='ha_repl' AND r.state='streaming'
  AND r.sync_state='sync' AND tls.ssl AND r.replay_lsn IS NOT NULL
"""
FRESH_CATALOG = """
SELECT to_json(
    EXISTS (SELECT 1 FROM pg_roles WHERE rolname=session_user AND rolsuper)
    AND EXISTS (SELECT 1 FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba
                WHERE d.datname=current_database() AND r.rolsuper)
    AND NOT EXISTS (SELECT 1 FROM pg_namespace
                    WHERE nspname NOT IN ('public','pg_catalog','pg_toast','information_schema'))
    AND NOT EXISTS (
      SELECT 1 FROM (
        SELECT relnamespace AS oid FROM pg_class
        UNION ALL SELECT pronamespace FROM pg_proc
        UNION ALL SELECT typnamespace FROM pg_type
      ) catalog JOIN pg_namespace n USING (oid)
      WHERE n.nspname NOT IN ('pg_catalog','pg_toast','information_schema')
    )
    AND NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname IN ({roles}))
    AND NOT EXISTS (SELECT 1 FROM pg_default_acl)
    AND NOT EXISTS (SELECT 1 FROM pg_largeobject_metadata)
    AND NOT EXISTS (SELECT 1 FROM pg_foreign_server)
    AND NOT EXISTS (SELECT 1 FROM pg_user_mapping)
    AND NOT EXISTS (SELECT 1 FROM pg_foreign_data_wrapper)
    AND NOT EXISTS (SELECT 1 FROM pg_event_trigger)
    AND NOT EXISTS (SELECT 1 FROM pg_publication)
    AND NOT EXISTS (SELECT 1 FROM pg_subscription)
    AND NOT EXISTS (SELECT 1 FROM pg_replication_origin)
    AND NOT EXISTS (SELECT 1 FROM pg_parameter_acl)
)
""".format(roles=",".join(
    ["'ha_effect_broker'"] + [f"'{service.replace('-', '_')}_{node}'" for service in sorted(SERVICES) for node in ("a", "b")]
))
CLOSED_STATE = """
SELECT json_build_array(
    (SELECT count(*)=1 FROM ha.control WHERE singleton=1 AND primary_node='A' AND partner='B'
        AND NOT writes_open AND NOT history_safe AND expires_at < clock_timestamp()),
    (SELECT cluster FROM ha.control WHERE singleton=1),
    (SELECT count(*) FROM ha.identities),
    (SELECT count(*) FROM ha.enforced_roles),
    (SELECT json_agg(DISTINCT service ORDER BY service) FROM ha.enforced_namespaces),
    (SELECT count(*) FROM ha_effects.ledger),
    (SELECT count(*) FROM ha_effects.private_state),
    to_regprocedure('ha.local_roles(text[],text,boolean)') IS NULL)
"""


@dataclass(frozen=True)
class RestorePins:
    boot_sha256: str
    installation_id: str
    inventory_sha256: str
    postgres_id: str
    uid: int
    gid: int
    system_id: str
    utility_digest: str
    initializer_sha256: str
    source_commit: str
    plan_sha256: str
    config_sha256: str
    manifest_sha256: str
    cluster: str
    peer_ipv4: str

    def validate(self):
        digests = (self.boot_sha256, self.installation_id, self.inventory_sha256, self.postgres_id,
                   self.initializer_sha256, self.plan_sha256, self.config_sha256, self.manifest_sha256)
        _need(all(type(value) is str and SHA.fullmatch(value) for value in digests)
              and type(self.uid) is int and self.uid in {70, 999}
              and type(self.gid) is int and 1 <= self.gid < 2**32 - 1
              and type(self.system_id) is str and re.fullmatch(r"[1-9][0-9]{0,19}", self.system_id)
              and type(self.utility_digest) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", self.utility_digest)
              and type(self.source_commit) is str and re.fullmatch(r"[0-9a-f]{40}", self.source_commit)
              and type(self.cluster) is str and re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", self.cluster),
              "guest_restore_pins_invalid")
        try:
            _need(type(self.peer_ipv4) is str and str(ipaddress.IPv4Address(self.peer_ipv4)) == self.peer_ipv4,
                  "guest_restore_peer_pin_invalid")
        except ValueError:
            raise TransitionBlocked("guest_restore_peer_pin_invalid") from None


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _docker(*arguments, timeout=10):
    result = subprocess.run((*DOCKER, *arguments), timeout=timeout, capture_output=True, check=False,
                            env={"PATH": "/usr/bin:/bin", "HOME": "/root", "LC_ALL": "C"})
    _need(result.returncode == 0, "guest_restore_docker_rejected")
    return result.stdout


def _directory(path, uid, *, private=True):
    _need(path.is_absolute() and ".." not in path.parts and "," not in str(path),
          "guest_restore_private_path_required")
    info = path.lstat()
    _need(stat.S_ISDIR(info.st_mode) and info.st_uid == uid
          and not info.st_mode & (0o077 if private else 0o022), "guest_restore_private_path_required")
    for parent in path.parents:
        info = parent.lstat()
        _need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
              "guest_restore_root_ancestor_required")


def _read(path, uid, *, digest=None, private=True, limit=32 * 1024 * 1024):
    _directory(path.parent, uid, private=private)
    info = path.lstat()
    _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == uid
          and not info.st_mode & (0o077 if private else 0o022) and 0 < info.st_size <= limit,
          "guest_restore_private_file_required")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        _need(stamp(os.fstat(stream.fileno())) == stamp(info), "guest_restore_input_changed")
        raw = stream.read(limit + 1)
        _need(stamp(os.fstat(stream.fileno())) == stamp(info), "guest_restore_input_changed")
    _need(len(raw) == info.st_size and stamp(path.lstat()) == stamp(info)
          and (digest is None or _digest(raw) == digest), "guest_restore_input_changed")
    return raw


def _mount(source, target, *, readonly=True):
    return ("--mount", f"type=bind,src={source},dst={target}" + (",readonly" if readonly else ""))


def _inspect(*identifiers):
    return json.loads(_docker("inspect", "--type", "container", *identifiers))


def _inventory(excluded=None):
    identifiers = _docker("ps", "-aq", "--no-trunc").decode().split()
    _need(identifiers and len(identifiers) == len(set(identifiers)) and all(SHA.fullmatch(item) for item in identifiers),
          "guest_restore_inventory_rejected")
    objects = {item["Id"]: item for item in _inspect(*identifiers)}
    _need(set(objects) == set(identifiers) and (excluded is None or excluded in objects),
          "guest_restore_inventory_rejected")
    if excluded is not None:
        del objects[excluded]
    return objects


def inventory_sha256(objects):
    """Return the private inventory pin. Container environment values never leave this function."""
    protected = {
        identifier: {
            **{key: item[key] for key in ("Id", "Name", "Image", "Config", "HostConfig", "Mounts", "RestartCount",
                                         "NetworkSettings")},
            "State": {key: item["State"][key] for key in ("Status", "Running", "Paused", "Restarting",
                                                        "StartedAt", "FinishedAt", "Pid", "ExitCode")},
        }
        for identifier, item in objects.items()
    }
    return _digest(_json_bytes(protected))


def _assert_inventory(pins, excluded=None):
    objects = _inventory(excluded)
    _need(inventory_sha256(objects) == pins.inventory_sha256 and pins.postgres_id in objects,
          "guest_restore_inventory_changed")
    services = [(item["Config"]["Labels"] or {}).get("com.docker.compose.service") for item in objects.values()]
    _need(not set(services) & (SERVICES | {"ha-broker"}), "guest_restore_apps_not_closed")
    _need(all(services.count(service) == 1 for service in ("postgres", "ha-network", "ha-ingress")),
          "guest_restore_inventory_rejected")
    _need(all(item["State"]["Running"] is True for item, service in zip(objects.values(), services)
              if service in ("postgres", "ha-network", "ha-ingress")), "guest_restore_inventory_rejected")
    pg = objects[pins.postgres_id]
    volumes = {mount["Destination"]: mount for mount in pg["Mounts"]}
    _need(pg["Config"]["Labels"]["com.docker.compose.service"] == "postgres"
          and pg["State"]["Running"] is True and pg["Config"]["User"] == f"{pins.uid}:{pins.gid}"
          and volumes["/var/run/postgresql"]["Type"] == "bind"
          and volumes["/var/run/postgresql"]["Source"] == str(PG_SOCKET)
          and volumes["/bootstrap"]["Source"] == str(INITIALIZER.parent)
          and volumes["/bootstrap"]["RW"] is False, "guest_restore_postgres_container_mismatch")
    _directory(PG_SOCKET, pins.uid)
    socket = (PG_SOCKET / ".s.PGSQL.5432").lstat()
    _need(stat.S_ISSOCK(socket.st_mode) and socket.st_uid == pins.uid and socket.st_gid == pins.gid,
          "guest_restore_postgres_socket_mismatch")


def _identity(pins):
    _need(_digest(BOOT_ID.read_bytes()) == pins.boot_sha256, "guest_restore_wrong_boot")
    # The file is private even though its root-owned /etc parent is traversable.
    _directory(INSTALLATION.parent, 0, private=False)
    info = INSTALLATION.lstat()
    _need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and not info.st_mode & 0o077,
          "guest_restore_installation_file_rejected")
    identity = json.loads(_read(INSTALLATION, 0, private=False, limit=65536))
    _need(identity["node"] == "A" and identity["installation_id"] == pins.installation_id,
          "guest_restore_wrong_installation")
    _read(INITIALIZER, 0, digest=pins.initializer_sha256, private=False, limit=1024 * 1024)
    raw = _docker("image", "inspect", "--format", "{{.Id}} {{.Architecture}} {{.Os}}", pins.utility_digest)
    _need(raw.decode().strip() == pins.utility_digest + " amd64 linux", "guest_restore_utility_mismatch")


def _pg(pins, sql):
    raw = _docker("exec", "--user", f"{pins.uid}:{pins.gid}", "--env",
                  "PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=3000",
                  "--env", "PGCONNECT_TIMEOUT=3", pins.postgres_id, "psql", "-XqwAt", "-v", "ON_ERROR_STOP=1",
                  "-h", "/var/run/postgresql", "-U", "postgres", "-d", "ha", "-c", sql)
    return json.loads(raw)


def _lsn(value):
    _need(type(value) is str and LSN.fullmatch(value), "guest_restore_replica_lsn_invalid")
    high, low = value.split("/")
    return (int(high, 16) << 32) + int(low, 16)


def _peer_replay(pins):
    peers = _pg(pins, PEER_STATE)
    _need(type(peers) is list and len(peers) == 1 and type(peers[0]) is list and len(peers[0]) == 2
          and peers[0][0] == pins.peer_ipv4, "guest_restore_replica_identity_mismatch")
    return _lsn(peers[0][1])


def _assert_pg(pins, *, restored=False):
    actual = _pg(pins, INITIAL_STATE)
    identity = ["postgres", "ha", "ha_A", False, pins.system_id, "on", "remote_apply", "FIRST1(ha_b)"]
    _need(type(actual) is list and len(actual) == 9 and actual[:8] == identity
          and type(actual[8]) is int and (actual[8] > 2 if restored else actual[8] == 0),
          "guest_restore_postgres_identity_or_sync_mismatch")
    if not restored:
        _peer_replay(pins)
        return None
    closed = [True, pins.cluster, 14, 14, sorted(SERVICES), 0, 1, True]
    _need(_pg(pins, CLOSED_STATE) == closed, "guest_restore_postgres_not_closed")
    flushed = _pg(pins, "SELECT to_json(pg_current_wal_flush_lsn()::text)")
    barrier = _lsn(flushed)
    deadline = time.monotonic() + 5
    while _peer_replay(pins) < barrier:
        _need(time.monotonic() < deadline, "guest_restore_replica_replay_unconfirmed")
        time.sleep(0.2)
    return _digest(_json_bytes({"system_id": pins.system_id, "flush_lsn": flushed}))


def _validate_manifest(pins, manifest):
    expected = {"source_commit": pins.source_commit, "target_system_id": pins.system_id,
                "plan_sha256": pins.plan_sha256, "cluster": pins.cluster}
    _need(all(manifest[key] == value for key, value in expected.items())
          and set(manifest["services"]) == set(SERVICES), "guest_restore_manifest_mismatch")


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_frozen_entry(source, temporary, entry, pins, *, metadata=False):
    _need(source.lstat().st_uid == 0, "guest_restore_root_frozen_source_required")
    descriptor = os.open(temporary / entry["file"], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        if metadata:
            raw = read_verified(source, entry["sha256"], entry["bytes"])
            output.write(raw)
        else:
            raw = None
            read_verified(source, entry["sha256"], entry["bytes"], load=False, sink=output)
        output.flush()
        os.fchown(output.fileno(), pins.uid, pins.gid)
        os.fsync(output.fileno())
    return raw


def stage_frozen(pins, source, stage):
    """Copy the exact verified ingress catalog to a new PG-owned leaf of the private attempt."""
    _need(os.geteuid() == 0, "guest_restore_root_required")
    pins.validate()
    source, stage = Path(source), Path(stage)
    destination = stage / "frozen"
    try:
        _directory(source, 0)
        _directory(stage, 0)
        _need(not os.path.lexists(destination), "guest_restore_frozen_stage_exists")
        manifest_file = source / "manifest.json"
        raw = _read(manifest_file, 0, digest=pins.manifest_sha256)
        _validate_manifest(pins, json.loads(raw))
        with tempfile.TemporaryDirectory(prefix=".frozen-restore-", dir=stage) as directory:
            temporary = Path(directory)
            _copy_frozen_entry(manifest_file, temporary, {"file": "manifest.json",
                               "sha256": pins.manifest_sha256, "bytes": len(raw)}, pins, metadata=True)

            def metadata(entry):
                return _copy_frozen_entry(source / entry["file"], temporary, entry, pins, metadata=True)

            for entry in _catalog(raw, metadata):
                _copy_frozen_entry(source / entry["file"], temporary, entry, pins)
            os.chown(temporary, pins.uid, pins.gid)
            _fsync_directory(temporary)
            _publish(temporary, destination)
    except (OSError, ValueError, KeyError, TypeError, MigrationRejected):
        raise TransitionBlocked("guest_restore_frozen_stage_unconfirmed") from None
    return destination


def _prestage(pins, stage, *, new_receipts=True):
    _directory(stage, 0)
    inputs, frozen, receipts = (stage / name for name in ("inputs", "frozen", "receipts"))
    for directory in (inputs, frozen, receipts):
        _directory(directory, pins.uid)
        _need(directory.lstat().st_gid == pins.gid, "guest_restore_stage_owner_mismatch")
    if new_receipts:
        _need(not any(receipts.iterdir()), "guest_restore_receipt_exists")
    _read(inputs / "config.json", pins.uid, digest=pins.config_sha256, limit=65536)
    manifest = json.loads(_read(frozen / "manifest.json", pins.uid, digest=pins.manifest_sha256))
    _validate_manifest(pins, manifest)
    sqlite_ids = {identifier for service in manifest["services"].values()
                  for identifier in service["databases"].values()}
    sqlite_sizes = [manifest["artifacts"][identifier]["bytes"] for identifier in sqlite_ids]
    _need(all(type(size) is int and size > 0 for size in sqlite_sizes)
          and sum(sqlite_sizes) + 4096 * len(sqlite_sizes) <= SQLITE_STAGE_BYTES,
          "guest_restore_sqlite_stage_capacity")
    freeze = _frozen_metadata(frozen, pins.uid, manifest["freeze_receipt"])
    audit_entry = freeze["capture_audit"]
    _frozen_metadata(frozen, pins.uid, audit_entry)
    return inputs, frozen, receipts, manifest, audit_entry["sha256"]


def _frozen_metadata(directory, uid, entry):
    _need(type(entry) is dict and set(entry) == {"file", "sha256", "bytes"}
          and type(entry["file"]) is str and NAME.fullmatch(entry["file"])
          and type(entry["sha256"]) is str and SHA.fullmatch(entry["sha256"])
          and type(entry["bytes"]) is int and 0 < entry["bytes"] <= 32 * 1024 * 1024,
          "guest_restore_metadata_descriptor_rejected")
    raw = _read(directory / entry["file"], uid, digest=entry["sha256"], limit=entry["bytes"])
    _need(len(raw) == entry["bytes"], "guest_restore_metadata_descriptor_rejected")
    return json.loads(raw)


def _write_marker(name, value):
    directory = os.open(JOURNAL, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                             dir_fd=directory)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)


def _verify_restored_data(data, services):
    _need(type(data) is dict and set(data) == set(services), "guest_restore_receipt_unconfirmed")
    for service, item in data.items():
        _need(type(item) is dict and set(item) == {"databases", "state_keys", "state_sha256"}
              and type(item["state_keys"]) is int and item["state_keys"] == len(services[service]["states"])
              and type(item["state_sha256"]) is str and SHA.fullmatch(item["state_sha256"])
              and type(item["databases"]) is list, "guest_restore_receipt_unconfirmed")
        components = set()
        for database in item["databases"]:
            _need(type(database) is dict and set(database) == {"service", "component", "tables"}
                  and database["service"] == service and type(database["component"]) is str
                  and database["component"] not in components and type(database["tables"]) is dict
                  and database["tables"], "guest_restore_receipt_unconfirmed")
            components.add(database["component"])
        _need(components == set(services[service]["databases"]), "guest_restore_receipt_unconfirmed")


def _receipt_common(pins, manifest, audit_sha256):
    return {"schema_version": 1, "source_commit": pins.source_commit, "manifest_sha256": pins.manifest_sha256,
              "freeze_receipt_sha256": manifest["freeze_receipt"]["sha256"],
              "services": sorted(SERVICES), "target_system_id_sha256": _digest(pins.system_id.encode()),
              "primary_node": "A", "source_freeze_evidence_bound": True,
              "source_writer_fencing_verified_by_this_tool": False,
              "legacy_to_typed_artifact_normalization_verified_by_this_tool": False,
              "writer_authority_issued": False, "automatic_enable_performed": False,
              "external_effect_reconciliation_required": True,
              "capture_audit_sha256": audit_sha256}


def _matches(receipt, expected):
    return all(key in receipt and type(receipt[key]) is type(value) and receipt[key] == value
               for key, value in expected.items())


def _verify_receipt(pins, manifest, audit_sha256, receipts, output):
    path = receipts / "migration.json"
    raw = _read(path, pins.uid)
    receipt = json.loads(raw)
    prepared = json.loads(_read(path.with_suffix(".json.prepared"), pins.uid))
    _need(not os.path.lexists(path.with_suffix(".json.failed")) and receipt == json.loads(output),
          "guest_restore_receipt_unconfirmed")
    common = _receipt_common(pins, manifest, audit_sha256)
    expected = dict(common, status="PASS", provider_private_state_imported=True,
                    static_provider_configuration_imported=False, portal_objects=len(manifest["portal_objects"]))
    _need(prepared == dict(common, status="PREPARED")
          and set(receipt) == set(expected) | {"data"}
          and _matches(receipt, expected),
          "guest_restore_receipt_unconfirmed")
    _verify_restored_data(receipt["data"], manifest["services"])
    return _digest(raw)


def _verify_failed_receipt(pins, manifest, audit_sha256, receipts, output):
    path = receipts / "migration.json"
    _need(not os.path.lexists(path)
          and json.loads(output) == {"status": "FAIL", "reason": "frozen_seven_migration_failed"},
          "guest_restore_receipt_unconfirmed")
    common = _receipt_common(pins, manifest, audit_sha256)
    prepared = json.loads(_read(path.with_suffix(".json.prepared"), pins.uid))
    expected = dict(common, status="PREPARED")
    _need(type(prepared) is dict and prepared.keys() == expected.keys() and _matches(prepared, expected),
          "guest_restore_receipt_unconfirmed")
    raw = _read(path.with_suffix(".json.failed"), pins.uid)
    receipt = json.loads(raw)
    expected = dict(common, status="FAILED")
    _need(type(receipt) is dict
          and set(receipt) - {"sqlstate"} == expected.keys() | {"error_type", "stage"}
          and _matches(receipt, expected)
          and receipt["stage"] in ("transaction_begin", "closed_control", "frozen_data",
                                   "guard_effects", "logical_verification")
          and type(receipt["error_type"]) is str and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", receipt["error_type"])
          and ("sqlstate" not in receipt or type(receipt["sqlstate"]) is str
               and re.fullmatch(r"[0-9A-Z]{5}", receipt["sqlstate"])), "guest_restore_receipt_unconfirmed")
    return _digest(raw)


def _restore_mounts(inputs, frozen, receipts):
    return ((str(INITIALIZER), "/bootstrap/initialize.py", False), (str(PG_SOCKET), "/var/run/postgresql", False),
            (str(inputs), "/inputs", False), (str(frozen), "/frozen", False), (str(receipts), "/receipts", True))


def _assert_migration_container(item, identifier, name, pins, mounts, *, state="created"):
    host = item["HostConfig"]
    _need(item["Id"] == identifier and item["Name"] == "/" + name
          and item["Image"] == pins.utility_digest and item["State"]["Status"] == state
          and item["Config"]["User"] == f"{pins.uid}:{pins.gid}"
          and item["Config"]["Labels"]["n100-ha.closed-restore"] == pins.manifest_sha256
          and host["NetworkMode"] == "none" and host["ReadonlyRootfs"] is True
          and host["RestartPolicy"]["Name"] == "no" and host["Privileged"] is False
          and host["CapDrop"] == ["ALL"] and not host["CapAdd"]
          and host["SecurityOpt"] in (["no-new-privileges:true"], ["no-new-privileges=true"])
          and host["LogConfig"] == {"Type": "local", "Config": {"max-size": "32m", "max-file": "1",
                                                               "compress": "false"}},
          "guest_restore_container_unconfirmed")
    binds = {(item["Source"], item["Destination"], item["RW"]) for item in item["Mounts"] if item["Type"] == "bind"}
    _need(binds == set(mounts)
          and all(item["Type"] == "bind" or item["Type"] == "tmpfs" and item["Destination"] == "/tmp"
                  for item in item["Mounts"]), "guest_restore_container_unconfirmed")


def _intent(pins):
    name = "n100-ha-closed-restore-" + pins.installation_id[:16]
    identity = asdict(pins)
    identity["peer_ipv4_sha256"] = _digest(identity.pop("peer_ipv4").encode())
    return {"status": "INTENT", "container": name, **identity}


def observe_closed_outcome(pins, stage):
    """Inspect a pending attempt. Observation never reruns the importer or opens authority."""
    _need(os.geteuid() == 0, "guest_restore_root_required")
    pins.validate()
    stage = Path(stage)
    intent_path = JOURNAL / "migration-intent.json"
    try:
        _directory(JOURNAL, 0)
        _identity(pins)
        if not os.path.lexists(intent_path):
            _need(not os.path.lexists(JOURNAL / "migration-pass.json"), "guest_restore_intent_unconfirmed")
            _prestage(pins, stage)
            _assert_inventory(pins)
            _assert_pg(pins)
            _need(not os.path.lexists(intent_path), "guest_restore_intent_unconfirmed")
            return {"status": "CLEAN", "automatic_retry_permitted": False}

        expected_intent = _intent(pins)
        _need(json.loads(_read(intent_path, 0)) == expected_intent, "guest_restore_intent_unconfirmed")
        inputs, frozen, receipts, manifest, audit_sha256 = _prestage(pins, stage, new_receipts=False)
        name = expected_intent["container"]
        owned, = _inspect(name)
        identifier = owned["Id"]
        exit_code = owned["State"]["ExitCode"]
        _need(SHA.fullmatch(identifier) and type(exit_code) is int and exit_code in (0, 1)
              and owned["RestartCount"] == 0,
              "guest_restore_container_unconfirmed")
        _assert_migration_container(owned, identifier, name, pins, _restore_mounts(inputs, frozen, receipts),
                                    state="exited")
        output = _docker("logs", identifier)
        if exit_code == 1:
            _need(not os.path.lexists(JOURNAL / "migration-pass.json"), "guest_restore_receipt_unconfirmed")
            receipt_sha256 = _verify_failed_receipt(pins, manifest, audit_sha256, receipts, output)
            _assert_inventory(pins, identifier)
            _assert_pg(pins)
            _need(_pg(pins, FRESH_CATALOG) is True, "guest_restore_postgres_not_fresh")
            return {"status": "CLEAN_FAILED", "automatic_retry_permitted": False,
                    "migration_failure_sha256": receipt_sha256}
        receipt_sha256 = _verify_receipt(pins, manifest, audit_sha256, receipts, output)
        _assert_inventory(pins, identifier)
        wal_barrier_sha256 = _assert_pg(pins, restored=True)
        return {"status": "COMMITTED_CLOSED", "automatic_retry_permitted": False,
                "migration_receipt_sha256": receipt_sha256, "wal_barrier_sha256": wal_barrier_sha256}
    except (TransitionBlocked, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return {"status": "UNKNOWN", "automatic_retry_permitted": False}


def restore_closed(pins, stage):
    """Restore exactly once. Keep the durable intent and owned container for reconciliation."""
    _need(os.geteuid() == 0, "guest_restore_root_required")
    pins.validate()
    stage = Path(stage)
    _directory(JOURNAL, 0)
    intent_name = "migration-intent.json"
    _need(not os.path.lexists(JOURNAL / intent_name), "guest_restore_reconciliation_required")
    _need(not os.path.lexists(JOURNAL / "migration-pass.json"), "guest_restore_reconciliation_required")
    try:
        _identity(pins)
        inputs, frozen, receipts, manifest, audit_sha256 = _prestage(pins, stage)
        _assert_inventory(pins)
        _assert_pg(pins)
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        raise TransitionBlocked("guest_restore_preflight_rejected") from None
    intent = _intent(pins)
    name = intent["container"]
    try:
        _write_marker(intent_name, intent)
    except FileExistsError:
        raise TransitionBlocked("guest_restore_reconciliation_required") from None
    except OSError:
        raise TransitionBlocked("guest_restore_intent_unconfirmed") from None
    try:
        mounts = _restore_mounts(inputs, frozen, receipts)
        mount_arguments = [arg for source, target, writable in mounts
                           for arg in _mount(source, target, readonly=not writable)]
        identifier = _docker(
            "create", "--name", name, "--label", "n100-ha.closed-restore=" + pins.manifest_sha256,
            "--pull", "never", "--network", "none", "--restart", "no", "--read-only",
            "--log-driver", "local", "--log-opt", "max-size=32m", "--log-opt", "max-file=1",
            "--log-opt", "compress=false",
            "--user", f"{pins.uid}:{pins.gid}", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--pids-limit", "64",
            "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size={SQLITE_STAGE_BYTES},mode=1777",
            *mount_arguments,
            "--entrypoint", "/bin/busybox", pins.utility_digest, "timeout", "-s", "KILL", str(TIMEOUT),
            "/usr/local/bin/python", "-B", "/bootstrap/initialize.py",
            "--config", "/inputs/config.json", "--phase", "migration",
            "--migration-manifest", "/frozen/manifest.json", "--migration-sha256", pins.manifest_sha256,
            "--migration-receipt", "/receipts/migration.json",
        ).decode().strip()
        _need(SHA.fullmatch(identifier), "guest_restore_container_unconfirmed")
        owned, = _inspect(identifier)
        _assert_migration_container(owned, identifier, name, pins, mounts)
        _assert_inventory(pins, identifier)
        _assert_pg(pins)
        output = _docker("start", "-a", identifier, timeout=TIMEOUT + 15)
        owned, = _inspect(identifier)
        _need(owned["State"]["Status"] == "exited" and owned["State"]["ExitCode"] == 0
              and owned["RestartCount"] == 0, "guest_restore_container_unconfirmed")
        receipt_sha256 = _verify_receipt(pins, manifest, audit_sha256, receipts, output)
        _assert_inventory(pins, identifier)
        wal_barrier_sha256 = _assert_pg(pins, restored=True)
        _fsync_directory(receipts)
        result = {"status": "PASS", "container_id": identifier, "manifest_sha256": pins.manifest_sha256,
                  "migration_receipt_sha256": receipt_sha256, "target_system_id_sha256": _digest(pins.system_id.encode()),
                  "wal_barrier_sha256": wal_barrier_sha256,
                  "services": sorted(SERVICES), "database_and_apps_closed": True, "writer_authority_issued": False}
        _write_marker("migration-pass.json", result)
        return result
    except (TransitionBlocked, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        raise TransitionBlocked("guest_restore_outcome_unconfirmed_reconcile_intent") from None
