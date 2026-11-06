"""Explicit opt-in for isolated development; never silently fall back to SQLite."""
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


def load(service=None):
    try:
        if (os.getenv("HA_LOCAL_RUNTIME") != "1"
                or os.getenv("APP_ENV") not in {"test", "development"}):
            raise DatabaseUnavailable("production_ha_not_armed")
        payload = json.loads(_private(os.environ["HA_PG_CONFIG"]).read_text())
        if (payload["scope"] != "local-isolated" or payload["node"] not in {"A", "B"}
                or payload["service"] not in SERVICES or (service and payload["service"] != service)
                or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", payload["cluster"])):
            raise DatabaseUnavailable("configuration_identity_mismatch")
        for endpoint, expected in (("primary", "ha-a"), ("partner", "ha-b")):
            value = payload[endpoint]
            if (value["host"] != expected or value.get("port", 5432) != 5432
                    or value["sslmode"] != "verify-full" or "password" in value
                    or set(value) - {"host", "port", "dbname", "user", "sslmode", "sslrootcert", "passfile"}):
                raise DatabaseUnavailable("authenticated_local_endpoint_required")
            _private(value["passfile"])
            ca = Path(value["sslrootcert"])
            if not ca.is_absolute() or ca.is_symlink() or not ca.is_file():
                raise DatabaseUnavailable("trusted_ca_required")
        return payload
    except DatabaseUnavailable:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise DatabaseUnavailable("invalid_ha_configuration") from None


def node_id():
    return load()["node"]
