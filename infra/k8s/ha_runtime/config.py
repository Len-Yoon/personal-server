"""Fail-closed HA configuration; production requires a private release-bound approval."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

SERVICES = frozenset({"portal-web", "book-memo", "youtube-memo", "crawler-worker", "car-care-worker", "homeops-executor", "system-agent"})
WEB = frozenset({"portal-web", "book-memo", "youtube-memo", "crawler-worker"})


class DatabaseUnavailable(sqlite3.OperationalError):
    def __init__(self, reason="protected_database_unavailable"):
        # Reasons are static program labels; never libpq errors, SQL or params.
        super().__init__(reason)


def enabled():
    return "HA_PG_CONFIG" in os.environ


def schema_name(service, component="main"):
    if service not in SERVICES or not re.fullmatch(r"[a-z][a-z0-9_]{0,30}", component):
        raise DatabaseUnavailable("invalid_database_namespace")
    return "s_" + service.replace("-", "_") + "_" + component


def _private(path):
    candidate = Path(path)
    info = candidate.lstat()
    if (not candidate.is_absolute() or not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077 or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or any(p.is_symlink() for p in candidate.parents)):
        raise DatabaseUnavailable("private_configuration_required")
    return candidate


def _authority_file(path):
    """App-readable approval authority must be outside the app's write boundary."""
    candidate = Path(path)
    info = candidate.lstat()
    if (not candidate.is_absolute() or os.geteuid() == 0
            or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) not in {0o440, 0o444}):
        raise DatabaseUnavailable("trusted_activation_authority_required")
    for parent in candidate.parents:
        parent_info = parent.lstat()
        if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != 0
                or parent_info.st_mode & 0o022):
            raise DatabaseUnavailable("trusted_activation_authority_required")
    return candidate


def _release():
    path = _authority_file(Path(__file__).with_name("release.json"))
    value = json.loads(path.read_text())
    if (set(value) != {"source_commit", "release_id", "source_files_sha256"}
            or not re.fullmatch(r"[0-9a-f]{40}", value["source_commit"])
            or any(not re.fullmatch(r"[0-9a-f]{64}", value[key]) for key in ("release_id", "source_files_sha256"))):
        raise DatabaseUnavailable("packaged_release_required")
    return value


def _approved(payload):
    if os.getenv("APP_ENV") != "production":
        raise DatabaseUnavailable("production_ha_not_armed")
    release = _release()
    approval = json.loads(_authority_file(os.environ["HA_PG_ACTIVATION"]).read_text())
    if type(approval) is not dict:
        raise DatabaseUnavailable('invalid_activation_authority')
    installed = payload.get('profile') == 'enforced-vm-v1' and approval.get('schema_version') == 3
    identities = {"cluster", "system_id", "node", "service", "primary_node", "primary", "partner"} | set(release)
    if payload.get("profile") == "enforced-vm-v1":
        identities |= {"profile", "enforcement_sha256", "effects"}
        try:
            from ha_egress.client import BrokerUnavailable, validate_binding
        except ImportError:
            raise DatabaseUnavailable("activation_effects_binding_required") from None
        try:
            validate_binding(payload.get("effects"))
            for key in ("ca", "certificate"):
                _authority_file(payload["effects"][key])
            _private(payload["effects"]["key"])
        except (BrokerUnavailable, OSError, ValueError, KeyError, TypeError):
            raise DatabaseUnavailable("activation_effects_binding_required") from None
    if installed:
        if (payload.get('scope') != 'n100-approved' or type(payload.get('generation')) is not int
                or payload['generation'] < 1):
            raise DatabaseUnavailable('installed_authority_invalid')
        identities |= {'installation_id','generation'}
        validate_installed_authority(approval, payload.get('installation_id'))
    authority_fields = ({'schema_version','action','authority_kind','installed_at','nodes'}
                        if installed else {'action','expires','nodes'})
    if (set(payload) != identities | {"scope"}
            or set(approval) != identities | authority_fields
            or approval["action"] != "activate-ha"
            or any(payload[key] != approval[key] for key in identities)
            or any(payload[key] != value for key, value in release.items())):
        raise DatabaseUnavailable("activation_identity_mismatch")
    # Explicit UTC, bounded remaining lifetime: stale or open-ended grants fail.
    if not installed:
        if not isinstance(approval["expires"], str) or not approval["expires"].endswith("Z"):
            raise DatabaseUnavailable("activation_expiry_required")
        expiry = datetime.fromisoformat(approval["expires"].removesuffix("Z") + "+00:00")
        now = datetime.now(timezone.utc)
        if expiry.utcoffset() != timedelta(0) or not now < expiry <= now + timedelta(hours=24):
            raise DatabaseUnavailable("activation_expired_or_unbounded")
    nodes = approval["nodes"]
    if (not isinstance(nodes, dict) or set(nodes) != {"A", "B"} or nodes["A"] == nodes["B"]
            or any(not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host)
                   for host in nodes.values())
            or not isinstance(payload["system_id"], str)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", payload["system_id"])):
        raise DatabaseUnavailable("activation_nodes_required")
    return nodes


def validate_installed_authority(value, installation_id):
    """Validate the permanent same-plan installation envelope, not a live lease.

    Only a trusted owner can issue the containing authority file. Native boot
    checks and the DB lease remain responsible for current execution authority.
    """
    try:
        if (type(value) is not dict or type(value.get('schema_version')) is not int or value['schema_version'] != 3
                or value.get('authority_kind') != 'installed-runtime'
                or type(installation_id) is not str or not re.fullmatch(r'[0-9a-f]{64}', installation_id)
                or value.get('installation_id') != installation_id
                or 'expires' in value or 'expires_at' in value
                or type(value.get('installed_at')) is not str or not value['installed_at'].endswith('Z')):
            raise ValueError()
        stamp = datetime.fromisoformat(value['installed_at'].removesuffix('Z') + '+00:00')
        if stamp.utcoffset() != timedelta(0) or stamp > datetime.now(timezone.utc):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise DatabaseUnavailable('installed_authority_invalid') from None


def load(service=None):
    try:
        environment = os.getenv("APP_ENV")
        if (environment not in {"test", "development", "production"}
                or (environment == "production" and not os.getenv("HA_PG_ACTIVATION"))
                or (environment in {"test", "development"} and os.getenv("HA_LOCAL_RUNTIME") != "1")):
            raise DatabaseUnavailable("production_ha_not_armed")
        payload = json.loads(_private(os.environ["HA_PG_CONFIG"]).read_text())
        if "profile" in payload:
            from .enforcement import PROFILE, definition_sha256
            if payload["profile"] != PROFILE or payload.get("enforcement_sha256") != definition_sha256():
                raise DatabaseUnavailable("enforced_profile_binding_required")
        if payload["scope"] == "local-isolated":
            if (os.getenv("HA_LOCAL_RUNTIME") != "1"
                    or os.getenv("APP_ENV") not in {"test", "development"}):
                raise DatabaseUnavailable("production_ha_not_armed")
            nodes = {"A": "ha-a", "B": "ha-b"}
        elif payload["scope"] == "n100-approved":
            nodes = _approved(payload)
        else:
            raise DatabaseUnavailable("configuration_scope_mismatch")
        if (payload["node"] not in {"A", "B"}
                or payload["service"] not in SERVICES or (service and payload["service"] != service)
                or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", payload["cluster"])):
            raise DatabaseUnavailable("configuration_identity_mismatch")
        primary_node = payload.get("primary_node", "A")
        if primary_node not in {"A", "B"}:
            raise DatabaseUnavailable("invalid_primary_mapping")
        for endpoint, expected in (("primary", nodes[primary_node]),
                                   ("partner", nodes["B" if primary_node == "A" else "A"])):
            value = payload[endpoint]
            if (value["host"] != expected or value.get("port", 5432) != 5432
                    or value["sslmode"] != "verify-full" or "password" in value
                    or set(value) - {"host", "port", "dbname", "user", "sslmode", "sslrootcert", "passfile"}):
                raise DatabaseUnavailable("authenticated_endpoint_required")
            if payload["scope"] == "n100-approved" and (
                    not isinstance(value.get("dbname"), str) or not value["dbname"]
                    or not isinstance(value.get("user"), str) or not value["user"]):
                raise DatabaseUnavailable("authenticated_endpoint_identity_required")
            _private(value["passfile"])
            ca = Path(value["sslrootcert"])
            if (not ca.is_absolute() or ca.is_symlink() or not ca.is_file()
                    or any(parent.is_symlink() for parent in ca.parents)
                    or (payload["scope"] == "n100-approved" and (ca.stat().st_mode & 0o022
                         or ca.stat().st_uid not in {0, os.geteuid()}))):
                raise DatabaseUnavailable("trusted_ca_required")
        return payload
    except DatabaseUnavailable:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise DatabaseUnavailable("invalid_ha_configuration") from None


def node_id():
    return load()["node"]
