"""Read back the running A containers, their mTLS routes, and current PostgreSQL lease."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import http.client
import os
from pathlib import Path
import socket
import ssl
import stat
import subprocess
import time

from ha_runtime.config import SERVICES
from ha_runtime.seven_capture import parse
from ha_runtime.seven_migration import MigrationRejected

from . import native_guest_restore as restore, native_guest_runtime as runtime
from .legacy_writers import TransitionBlocked, _need


PATHS = {
    "book-memo": "/ready", "car-care-worker": "/health", "crawler-worker": "/ready",
    "homeops-executor": "/ready", "portal-web": "/ready", "system-agent": "/health", "youtube-memo": "/ready",
}
MAX_BODY = 16 * 1024
PORT = 10443
LIVE_SECONDS = 20
SOCKET_SECONDS = 3
OPEN_STATE = """
WITH sampled AS MATERIALIZED (SELECT clock_timestamp() AS now)
SELECT json_build_object(
    'identity', json_build_array(session_user, current_database(), current_setting('cluster_name'),
        pg_is_in_recovery(), system_identifier::text, current_setting('transaction_read_only'),
        current_setting('synchronous_commit'),
        regexp_replace(current_setting('synchronous_standby_names'), '[[:space:]"]', '', 'g'),
        current_setting('fsync'), current_setting('full_page_writes'),
        ('x'||substring(pg_walfile_name(pg_current_wal_insert_lsn()),1,8))::bit(32)::integer),
    'authority', (SELECT json_build_array(cluster, epoch, revision, primary_node, partner,
        timeline, serving, worker, mode, writes_open, history_safe)
        FROM ha.control WHERE singleton=1),
    'lease_clock', (SELECT json_build_array(sampled.now, expires_at)
        FROM ha.control WHERE singleton=1),
    'binding', (SELECT json_agg(json_build_array(cluster,system_id,source_commit,plan_sha256,broker_role::text))
        FROM ha_effects.binding),
    'peers', (SELECT COALESCE(json_agg(json_build_array(host(r.client_addr),r.replay_lsn::text)), '[]'::json)
        FROM pg_stat_replication r JOIN pg_stat_ssl tls USING (pid)
        WHERE r.application_name='ha_b' AND r.usename='ha_repl' AND r.state='streaming'
          AND r.sync_state='sync' AND tls.ssl AND r.replay_lsn IS NOT NULL))
FROM pg_control_system(), sampled
"""


def _matches(value, expected):
    return runtime._json_bytes(value) == runtime._json_bytes(expected)


def _clock(value):
    parsed = datetime.fromisoformat(value)
    _need(parsed.utcoffset() is not None, "guest_live_clock_unconfirmed")
    return parsed


def _lease(pins, postgres):
    observed = restore._pg(postgres, OPEN_STATE)
    identity = ["postgres", "ha", "ha_A", False, pins.system_id, "on", "remote_apply", "FIRST1(ha_b)", "on", "on", 1]
    authority = [pins.cluster, 1, 2, "A", "B", 1, ["A"], "A", "AS", True, True]
    binding = [[pins.cluster, pins.system_id, pins.release["source_commit"], pins.plan_sha256, runtime.config.BROKER_ROLE]]
    _need(type(observed) is dict and set(observed) == {"identity", "authority", "lease_clock", "binding", "peers"}
          and _matches(observed["identity"], identity) and _matches(observed["authority"], authority)
          and _matches(observed["binding"], binding), "guest_live_pg_binding_unconfirmed")
    peer, = observed["peers"]
    _need(type(peer) is list and len(peer) == 2 and peer[0] == postgres.peer_ipv4,
          "guest_live_pg_peer_unconfirmed")
    replay = restore._lsn(peer[1])
    sampled, expires = map(_clock, observed["lease_clock"])
    _need(0 < (expires - sampled).total_seconds() <= 5 and datetime.now(timezone.utc) < expires,
          "guest_live_pg_lease_expired")
    return observed, sampled, expires, replay


def _authority_paths():
    return {str(path.relative_to(runtime.ROOT)): path for path in (
        runtime.ROOT / "broker" / "authority.json",
        *(runtime.ROOT / "apps" / service / "activation.json" for service in sorted(SERVICES)),
    )}


def _installed_authorities(digests):
    paths = _authority_paths()
    _need(type(digests) is dict and digests.keys() == paths.keys()
          and all(runtime._digest(runtime._read(path, 0, {0o444})) == digests[name] for name, path in paths.items()),
          "guest_live_installed_authority_changed")


def _receipts(pins, activation, stage, activation_receipt_sha256):
    identity = runtime._runtime_identity(pins)
    _need(runtime._read(stage / "runtime-intent.json", 0, {0o600}) == runtime._json_bytes(runtime._runtime_intent(pins)),
          "guest_live_preparation_intent_unconfirmed")
    raw = runtime._read(stage / "runtime-prepared.json", 0, {0o600})
    _need(runtime._digest(raw) == activation.primary_runtime_receipt_sha256, "guest_live_preparation_unconfirmed")
    prepared = parse(raw)
    containers, project, broker_sha = (prepared[key] for key in ("containers", "project", "broker_sha256"))
    order = ["ha-broker", *sorted(SERVICES)]
    _need(raw == runtime._json_bytes(runtime._prepared_receipt(pins, project, containers, broker_sha))
          and type(containers) is dict and set(containers) == set(order) and runtime._match(runtime.SHA, broker_sha)
          and all(type(row) is dict and set(row) == {"container_id", "image_id"}
                  and runtime._match(runtime.SHA, row["container_id"])
                  and runtime._match(r"sha256:[0-9a-f]{64}", row["image_id"]) for row in containers.values()),
          "guest_live_preparation_unconfirmed")
    done = stage / "runtime-activation.json"
    done_info = done.lstat()
    raw = runtime._read(done, 0, {0o600})
    _need(runtime.stamp(done_info) == runtime.stamp(done.lstat())
          and runtime._digest(raw) == activation_receipt_sha256, "guest_live_activation_unconfirmed")
    result = parse(raw)
    intent_raw = runtime._read(stage / "runtime-activation-intent.json", 0, {0o600})
    intent = parse(intent_raw)
    files = result["authority_files"]
    common = {"schema_version": 1, **identity, **asdict(activation), "authority_files": files, "containers": containers}
    expected_intent = {**common, "status": "INTENT", "activation_order": order, "lease_sha256": intent["lease_sha256"]}
    expected_done = {**common, "status": "STARTED", "guest_intent_sha256": runtime._digest(intent_raw),
                     "lease_sha256": result["lease_sha256"], "automatic_retry_permitted": False}
    _need(intent_raw == runtime._json_bytes(expected_intent) and raw == runtime._json_bytes(expected_done)
          and all(runtime._match(runtime.SHA, row["lease_sha256"]) for row in (intent, result)),
          "guest_live_activation_unconfirmed")
    _installed_authorities(files)
    return prepared, files, datetime.fromtimestamp(done_info.st_mtime_ns / 1e9, timezone.utc)


def _layout(pins, postgres, stage, prepared):
    _need(runtime._digest(runtime.BOOT_ID.read_bytes()) == pins.boot_sha256, "guest_live_boot_changed")
    identity = parse(runtime._read(runtime.INSTALLATION, 0, {0o400, 0o600}))
    _need(identity["node"] == "A" and identity["installation_id"] == pins.installation_id,
          "guest_live_installation_changed")
    raw = runtime._read(stage / "guest-spec.json", 0, {0o400, 0o600})
    _need(runtime._digest(raw) == pins.spec_sha256, "guest_live_spec_unbound")
    spec = parse(raw)
    _need(spec["node"] == "A" and spec["guest_ip"] == spec["pg_a"] and spec["pg_b"] == postgres.peer_ipv4
          and spec["installation_id"] == pins.installation_id and spec["source_commit"] == pins.release["source_commit"],
          "guest_live_spec_unbound")
    renderer = runtime._renderer(pins)
    compose, haproxy, _ = renderer["render"]({**spec, "broker_sha256": prepared["broker_sha256"]})
    _need(compose["name"] == prepared["project"], "guest_live_project_changed")
    sock = runtime.DOCKER_SOCKET.lstat()
    _need(stat.S_ISSOCK(sock.st_mode) and sock.st_uid == 0 and sock.st_gid == spec["docker_gid"]
          and Path("/var/run/docker.sock").resolve(strict=True) == runtime.DOCKER_SOCKET,
          "guest_live_engine_socket_changed")
    engine = runtime._docker("info", "--format", "{{.ID}}").decode().strip()
    _need(runtime._digest(engine.encode()) == pins.engine_id_sha256, "guest_live_engine_changed")
    _need(runtime._read(runtime.ROOT / "ingress" / "haproxy.cfg", 0, {0o440, 0o444}) == haproxy.encode(),
          "guest_live_haproxy_config_changed")
    return spec["guest_ip"], compose["services"], runtime._images(spec)


def _running(pins, postgres, prepared, layouts, images, activated_at):
    inventory = runtime._inventory()
    rows = prepared["containers"]
    owned = {row["container_id"] for row in rows.values()}
    protected = {key: value for key, value in inventory.items() if key not in owned}
    pg = protected[postgres.postgres_id]
    _need(len(owned) == len(rows) and runtime.inventory_sha256(protected) == pins.protected_inventory_sha256
          and runtime._service(pg) == "postgres" and pg["State"]["Running"] is True
          and pg["Config"]["User"] == f"{postgres.uid}:{postgres.gid}", "guest_live_protected_inventory_changed")
    instances = {}
    for role, row in rows.items():
        item = inventory[row["container_id"]]
        state = item["State"]
        image = images["ha-backup-utility" if role == "ha-broker" else role]
        _need(row["image_id"] == image["id"] and item["Id"] == row["container_id"] and state["Status"] == "running"
              and state["Running"] is True and state["Paused"] is False and state["Restarting"] is False
              and type(state["Pid"]) is int and state["Pid"] > 0 and _clock(state["StartedAt"]) <= activated_at,
              "guest_live_container_state_changed")
        # Reuse the stopped-package contract with only the three verified lifecycle fields normalized.
        layout_item = {**item, "State": {**state, "Status": "created", "Running": False, "Pid": 0}}
        runtime._assert_created(layout_item, prepared["project"], role, image, layouts[role], pins)
        instances[role] = [row["container_id"], state["Pid"], state["StartedAt"]]
    return instances


def _tls(prepared):
    root = runtime.ROOT / "broker"
    broker_raw = runtime._read(root / "config.json", 0, {0o444})
    _need(runtime._digest(broker_raw) == prepared["broker_sha256"], "guest_live_broker_config_changed")
    broker = parse(broker_raw)
    providers = broker["providers"]
    leaf = providers["executor"]["server_sha256"]
    _need(runtime._match(runtime.SHA, leaf) and all(value["server_sha256"] == leaf
          for value in providers["internal"]["endpoints"].values()), "guest_live_ingress_leaf_unbound")
    local_leaf = runtime._read(runtime.ROOT / "ingress" / "server.pem", runtime.INGRESS_UID, {0o400, 0o600})
    _need(runtime._cert_digest(local_leaf) == leaf, "guest_live_ingress_leaf_unbound")
    ca = runtime._read(root / "ingress-server-ca.crt", 0, {0o440, 0o444})
    cert_path, key_path = root / "ingress-client.crt", root / "ingress-client.key"
    cert = runtime._read(cert_path, 0, {0o440, 0o444})
    key = runtime._read(key_path, runtime.BROKER_UID, {0o400, 0o600})
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_verify_locations(cadata=ca.decode("ascii"))
    context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    _need(runtime._read(cert_path, 0, {0o440, 0o444}) == cert
          and runtime._read(key_path, runtime.BROKER_UID, {0o400, 0o600}) == key, "guest_live_mtls_inputs_changed")
    return context, leaf


def _probe(context, leaf, ip, service):
    host, path = service + ".ha.internal", PATHS[service]
    with socket.create_connection((ip, PORT), timeout=SOCKET_SECONDS) as tcp:
        with context.wrap_socket(tcp, server_hostname=host) as conn:
            _need(runtime._digest(conn.getpeercert(binary_form=True)) == leaf, "guest_live_tls_peer_changed")
            conn.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode("ascii"))
            with http.client.HTTPResponse(conn) as response:
                response.begin()
                body = response.read(MAX_BODY + 1)
                _need(response.status == 200 and 0 < len(body) <= MAX_BODY, "guest_live_route_unready")
                content_type = response.headers.get_content_type()
                node_header = response.headers.get_all("X-HA-Node-ID", [])
    if service == "car-care-worker":
        _need(content_type == "text/plain" and body == b"ok", "guest_live_route_unready")
    else:
        data = parse(body)
        _need(content_type == "application/json" and type(data) is dict and data.get("service") == service
              and data.get("status") == ("ready" if path == "/ready" else "ok"), "guest_live_route_unready")
        if service in {"portal-web", "system-agent"}:
            _need(data.get("node_id") == "A", "guest_live_route_wrong_node")
        if service == "system-agent":
            _need(node_header == ["A"], "guest_live_route_wrong_node")
    return runtime._digest(body)


def observe_live_a(pins, activation, postgres, stage, *, activation_receipt_sha256, authority_sha256,
                   windows_generation, windows_generation_sha256, live_lease_sha256, live_authority_sha256):
    """Return a digest only after fresh A runtime, mTLS and database observations."""
    _need(os.geteuid() == 0 and pins.node == "A", "guest_live_a_root_required")
    pins.validate()
    activation.validate()
    postgres.validate()
    bindings = {"activation_receipt_sha256": activation_receipt_sha256, "authority_sha256": authority_sha256,
                "windows_generation_sha256": windows_generation_sha256, "live_lease_sha256": live_lease_sha256,
                "live_authority_sha256": live_authority_sha256}
    _need(type(windows_generation) is int and windows_generation > 0
          and all(runtime._match(runtime.SHA, value) for value in bindings.values())
          and all(getattr(postgres, key) == getattr(pins, key) for key in (
              "boot_sha256", "installation_id", "system_id", "plan_sha256", "manifest_sha256", "cluster"))
          and postgres.source_commit == pins.release["source_commit"], "guest_live_observation_unbound")
    stage = Path(stage)
    started = time.monotonic()
    try:
        runtime._directory(stage, 0)
        prepared, files, activated_at = _receipts(pins, activation, stage, activation_receipt_sha256)
        ip, layouts, images = _layout(pins, postgres, stage, prepared)
        before = _running(pins, postgres, prepared, layouts, images, activated_at)
        context, leaf = _tls(prepared)
        first, sampled, expires, replay = _lease(pins, postgres)
        with ThreadPoolExecutor(max_workers=len(PATHS)) as pool:
            pending = {service: pool.submit(_probe, context, leaf, ip, service) for service in PATHS}
            routes = {service: task.result() for service, task in pending.items()}
        _need(set(routes) == SERVICES, "guest_live_routes_incomplete")
        _installed_authorities(files)
        after = _running(pins, postgres, prepared, layouts, images, activated_at)
        last, sampled_after, expires_after, replay_after = _lease(pins, postgres)
        _need(before == after and sampled_after >= sampled and expires_after >= expires and replay_after >= replay
              and time.monotonic() - started < LIVE_SECONDS, "guest_live_observation_stale")
        return runtime._digest(runtime._json_bytes({
            **runtime._runtime_identity(pins), **bindings, "windows_generation": windows_generation,
            "containers": after, "routes": routes, "ingress_leaf_sha256": leaf, "postgres_before": first,
            "postgres_after": last,
        }))
    except (OSError, ValueError, KeyError, TypeError, MigrationRejected, subprocess.TimeoutExpired,
            http.client.HTTPException):
        raise TransitionBlocked("guest_live_observation_unconfirmed") from None
