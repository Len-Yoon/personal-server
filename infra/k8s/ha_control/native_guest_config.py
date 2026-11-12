"""Render initial guest configuration without starting apps or issuing authority."""
import re
import secrets

from ha_runtime.config import SERVICES
from ha_runtime.enforcement import definition_sha256

from .legacy_writers import _need


HEX = re.compile(r"[a-f0-9]{64}\Z")
PASSWORD = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
NODES = {"A": "ha-a", "B": "ha-b"}
BROKER_ROLE = "ha_effect_broker"
INTERNAL_TARGETS = {"news": "crawler-worker", "youtube": "youtube-memo", "books": "book-memo", "system": "system-agent"}
ROLES = {service.replace("-", "_") + "_" + node.lower()
         for service in SERVICES for node in NODES} | {"ha_repl", BROKER_ROLE}


def _match(pattern, value):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def new_role_passwords(replication_password):
    """Keep the already installed replication role credential."""
    _need(_match(PASSWORD, replication_password), "guest_replication_credential_invalid")
    result = {role: secrets.token_urlsafe(48) for role in sorted(ROLES - {"ha_repl"})}
    result["ha_repl"] = replication_password
    _need(len(set(result.values())) == len(ROLES), "guest_role_credential_collision")
    return result


def initializer_config(*, cluster, release, plan_sha256, broker_instance, passwords):
    _identity(cluster, release, plan_sha256)
    _need(_match(HEX, broker_instance) and type(passwords) is dict and set(passwords) == ROLES
          and all(_match(PASSWORD, secret) for secret in passwords.values())
          and len(set(passwords.values())) == len(ROLES), "guest_initializer_configuration_invalid")
    return {"schema_version": 2, "cluster": cluster, "source_commit": release["source_commit"],
            "plan_sha256": plan_sha256, "primary_node": "A", "broker_instance": broker_instance,
            "passwords": dict(passwords)}


def _identity(cluster, release, plan_sha256):
    _need(_match(r"[a-z][a-z0-9_]{0,62}", cluster) and _match(HEX, plan_sha256)
          and type(release) is dict and set(release) == {"source_commit", "release_id", "source_files_sha256"}
          and _match(r"[a-f0-9]{40}", release["source_commit"])
          and all(_match(HEX, release[key]) for key in ("release_id", "source_files_sha256")),
          "guest_application_release_invalid")


def _guest_identity(cluster, release, plan_sha256, node, installation_id, system_id):
    _identity(cluster, release, plan_sha256)
    _need(node in NODES and _match(HEX, installation_id) and _match(r"[1-9][0-9]{0,19}", system_id),
          "guest_application_identity_invalid")


def _postgres(role, password, root):
    _need(_match(PASSWORD, password), "guest_role_credential_invalid")
    endpoints = {node: {"host": host, "port": 5432, "dbname": "ha", "user": role,
                        "sslmode": "verify-full", "sslrootcert": root + "/pg-ca.crt",
                        "passfile": root + "/postgres.pgpass"} for node, host in NODES.items()}
    passfile = "".join(f"{host}:5432:ha:{role}:{password}\n" for host in NODES.values()).encode()
    return endpoints, passfile


def application_config(*, cluster, release, plan_sha256, node, installation_id, system_id,
                       service, password, broker_server_sha256):
    """Return the exact app config and this node's private passfile bytes."""
    _guest_identity(cluster, release, plan_sha256, node, installation_id, system_id)
    _need(service in SERVICES and _match(HEX, broker_server_sha256), "guest_application_configuration_invalid")
    endpoints, passfile = _postgres(service.replace("-", "_") + "_" + node.lower(), password, "/etc/ha")
    return {
        **release, "scope": "n100-approved", "profile": "enforced-vm-v1",
        "enforcement_sha256": definition_sha256(), "installation_id": installation_id, "generation": 1,
        "cluster": cluster, "system_id": system_id, "node": node, "service": service, "primary_node": "A",
        "primary": endpoints["A"], "partner": endpoints["B"],
        "effects": {"url": "https://ha-broker:8443", "ca": "/etc/ha/broker-ca.crt",
                    "certificate": "/etc/ha/broker-client.crt", "key": "/etc/ha/broker-client.key",
                    "server_sha256": broker_server_sha256, "plan_sha256": plan_sha256},
    }, passfile


def broker_config(*, cluster, release, plan_sha256, node, installation_id, system_id, broker_instance,
                  password, client_sha256, ingress_server_sha256, engine_id, targets):
    """Return a broker config containing only private provider file references."""
    _guest_identity(cluster, release, plan_sha256, node, installation_id, system_id)
    _need(_match(HEX, broker_instance) and type(client_sha256) is dict and set(client_sha256) == SERVICES
          and all(_match(HEX, value) for value in client_sha256.values()) and len(set(client_sha256.values())) == 7,
          "guest_broker_client_configuration_invalid")
    _need(_match(HEX, ingress_server_sha256) and _match(r"[a-zA-Z0-9_.:-]{1,128}", engine_id)
          and type(targets) is dict and targets and set(targets) <= SERVICES - {"car-care-worker"}
          and all(type(target) is dict and set(target) == {"container_id", "image_id"}
                  and _match(HEX, target["container_id"]) and _match(r"sha256:[a-f0-9]{64}", target["image_id"])
                  for target in targets.values()), "guest_broker_targets_invalid")
    root = "/etc/ha-broker"
    endpoints, passfile = _postgres(BROKER_ROLE, password, root)
    ingress_tls = {"ca": root + "/ingress-server-ca.crt", "certificate": root + "/ingress-client.crt",
                   "key": root + "/ingress-client.key"}
    providers = {
        "telegram": {service: root + "/providers/" + service + "-telegram.json"
                     for service in ("portal-web", "crawler-worker", "car-care-worker")},
        "hyundai": root + "/providers/hyundai.json",
        "yes24": root + "/providers/yes24.json",
        "docker": {"socket_path": "/var/run/docker.sock", "engine_id": engine_id,
                   "project": "n100-ha-" + installation_id[:12],
                   "targets": {service: dict(target) for service, target in targets.items()}},
        "executor": {"url": "https://homeops-executor.ha.internal:10443",
                     "secret_file": root + "/providers/executor.json", "tls": ingress_tls,
                     "server_sha256": ingress_server_sha256, "services": sorted(targets)},
        "internal": {"tls": ingress_tls, "endpoints": {
            key: {"url": f"https://{service}.ha.internal:10443", "server_sha256": ingress_server_sha256}
            for key, service in INTERNAL_TARGETS.items()}},
    }
    return {
        "schema_version": 1, "scope": "n100-approved", "profile": "enforced-vm-v1", "release": dict(release),
        "installation_id": installation_id, "node": node, "generation": 1,
        "binding": {"cluster": cluster, "system_id": system_id, "source_commit": release["source_commit"],
                    "plan_sha256": plan_sha256, "broker_role": BROKER_ROLE, "broker_instance": broker_instance},
        "postgres": {"primary": endpoints["A"], "peer": endpoints["B"]},
        "tls": {"ca": root + "/broker-ca.crt", "certificate": root + "/broker.crt", "key": root + "/broker.key"},
        "clients": [{"sha256": client_sha256[service], "service": service, "node": node, "generation": 1}
                    for service in sorted(SERVICES)],
        "providers": providers,
    }, passfile
