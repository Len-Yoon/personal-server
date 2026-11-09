"""Small DB-API bridge for explicitly ported SQL, not a SQLite SQL translator."""
from collections import deque
from contextlib import contextmanager
from functools import wraps
import copy
import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid

from .config import DatabaseUnavailable, WEB, load, schema_name
from . import proof

_LOCAL = threading.local()
ROLE_LOCK = 7142026110601


def database_boundary(method):
    @wraps(method)
    def guarded(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (DatabaseUnavailable, sqlite3.IntegrityError):
            raise
        except Exception:
            raise DatabaseUnavailable("database_operation_unavailable") from None
    return guarded


@database_boundary
def acquire_lock(connection, statement, parameters):
    return connection.execute(statement, parameters)


def qmark(statement, parameters):
    """Convert bound placeholders, preserving quoted literals and percent signs."""
    if not isinstance(statement, str) or "\x00" in statement:
        raise DatabaseUnavailable("invalid_statement")
    output, quote, index = [], None, 0
    while index < len(statement):
        char = statement[index]
        if quote:
            output.append("%%" if char == "%" and parameters else char)
            if char == quote:
                if index + 1 < len(statement) and statement[index + 1] == quote:
                    output.append(quote)
                    index += 1
                else:
                    quote = None
        elif char in "'\"":
            quote = char
            output.append(char)
        elif char == "?":
            output.append("%s")
        else:
            output.append("%%" if char == "%" and parameters else char)
        index += 1
    if quote or "--" in statement or "/*" in statement:
        raise DatabaseUnavailable("unsupported_sql_syntax")
    return "".join(output)


class Row(tuple):
    def __new__(cls, names, values):
        obj = tuple.__new__(cls, values)
        obj.names = names
        return obj

    def keys(self):
        return self.names

    def __getitem__(self, key):
        return tuple.__getitem__(self, self.names.index(key) if isinstance(key, str) else key)


class Cursor:
    def __init__(self, names=(), rows=(), rowcount=-1, lastrowid=None):
        self.rows = deque(Row(tuple(names), row) for row in rows)
        self.rowcount, self.lastrowid = rowcount, lastrowid

    def fetchone(self):
        return self.rows.popleft() if self.rows else None

    def fetchall(self):
        values = list(self.rows)
        self.rows.clear()
        return values

    def __iter__(self):
        while self.rows:
            yield self.rows.popleft()


def _admit(value, config, *, write=False, worker=False):
    if value["recovery"] or value["node"] != value["primary"] or not value["valid"]:
        raise DatabaseUnavailable("primary_role_unavailable")
    if worker or config["service"] not in WEB:
        if config["node"] != value["worker"]:
            raise DatabaseUnavailable("not_worker_owner")
    elif config["node"] not in value["serving"]:
        raise DatabaseUnavailable("not_serving_owner")
    if write and (not value["writes_open"] or not value["history_safe"]):
        raise DatabaseUnavailable("protected_writes_closed")


class Connection:
    def __init__(self, service, component="main"):
        self.config = load(service)
        self.schema = schema_name(service, component)
        self.raw = proof.raw_connection(self.config, "primary")
        self.closed = False
        self.active = False
        self.write = False
        self.replay = False
        self.identity_columns = {}
        self.hasher = hashlib.sha256()
        try:
            if self.config.get("profile") == "enforced-vm-v1":
                ready = self.raw.execute("SELECT ha.enforced_session_ready(%s::name,%s)",
                                         (self.schema, self.config["enforcement_sha256"])).fetchone()
                if ready != (True,):
                    raise DatabaseUnavailable("server_enforcement_required")
            self.raw.execute("SELECT pg_advisory_lock_shared(%s)", (ROLE_LOCK,))
            self.original = proof.observe(self.raw, self.config)
            _admit(self.original, self.config)
            # Match the old SQLite BEGIN IMMEDIATE serialization, including
            # read-version-check + write and whole-JSON load/modify/save.
            self.raw.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))", (self.schema,))
            self._start()
        except Exception:
            self.raw.close()
            raise

    def _start(self):
        if not self.active:
            from psycopg import sql
            self.raw.execute("BEGIN")
            self.raw.execute("SET LOCAL synchronous_commit=on")
            self.raw.execute(sql.SQL("SET LOCAL search_path TO {},pg_catalog").format(sql.Identifier(self.schema)))
            self.active = True
        observed = proof.observe(self.raw, self.config)
        if proof._generation(observed) != proof._generation(self.original):
            raise DatabaseUnavailable("role_generation_changed")
        _admit(observed, self.config)

    @database_boundary
    def execute(self, statement, parameters=()):
        import psycopg
        from psycopg import sql
        if self.closed:
            raise DatabaseUnavailable("database_connection_closed")
        normalized = statement.strip().rstrip(";")
        if normalized.upper() in {"BEGIN", "BEGIN TRANSACTION"}:
            self._start()
            return Cursor()
        verb = normalized.split(None, 1)[0].upper() if normalized else ""
        if verb not in {"SELECT", "WITH", "INSERT", "UPDATE", "DELETE"}:
            raise DatabaseUnavailable("application_schema_changes_forbidden")
        self._start()
        mutation = verb in {"INSERT", "UPDATE", "DELETE"}
        if mutation:
            _admit(proof.observe(self.raw, self.config), self.config, write=True)
            self.write = True
        identity = None
        match = re.match(r'INSERT\s+INTO\s+([a-z_][a-z0-9_]*)\b', normalized, re.I)
        if match and "RETURNING" not in normalized.upper():
            table = match[1].lower()
            if table not in self.identity_columns:
                self.identity_columns[table] = self.raw.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s AND is_identity='YES'",
                    (self.schema, table),
                ).fetchone()
            found = self.identity_columns[table]
            if found:
                identity = found[0]
                normalized += " RETURNING " + sql.Identifier(identity).as_string(self.raw)
        self.raw.execute("SAVEPOINT application_statement")
        try:
            cursor = self.raw.execute(qmark(normalized, parameters), parameters or None)
            count = cursor.rowcount
            names = tuple(column.name for column in cursor.description) if cursor.description else ()
            rows = cursor.fetchall() if cursor.description else []
            self.raw.execute("RELEASE SAVEPOINT application_statement")
        except psycopg.IntegrityError:
            self.raw.execute("ROLLBACK TO SAVEPOINT application_statement")
            self.raw.execute("RELEASE SAVEPOINT application_statement")
            raise sqlite3.IntegrityError("database_constraint_conflict") from None
        except psycopg.Error:
            raise DatabaseUnavailable("database_statement_failed") from None
        self.hasher.update(proof.digest([statement, parameters, names, rows]).encode())
        return Cursor(names, rows, count, rows[0][0] if identity and rows else None)

    def executemany(self, statement, parameters):
        count = 0
        for values in parameters:
            count += max(0, self.execute(statement, values).rowcount)
        return Cursor(rowcount=count)

    def protect_replay(self):
        self.replay = True

    @database_boundary
    def commit(self):
        if self.closed or not self.active:
            return
        import psycopg
        from psycopg import sql
        notices = []
        handler = lambda value: notices.append(value.severity_nonlocalized)
        self.raw.add_notice_handler(handler)
        binding = None
        try:
            if self.write or self.replay:
                _admit(proof.observe(self.raw, self.config), self.config, write=True)
                identity = str(uuid.uuid4())
                result_hash = self.hasher.hexdigest()
                binding = proof.contract.RequestBinding(self.config["service"], identity, "transaction",
                                                       result_hash, result_hash,
                                                       self.config["node"] if self.config["service"] in {"homeops-executor", "system-agent"} else None)
                self.raw.execute(sql.SQL("INSERT INTO {}.ha_receipts(receipt,payload_hash,result_hash) VALUES(%s,%s,%s)").format(sql.Identifier(self.schema)),
                                 (identity, result_hash, result_hash))
            proof.bounded_commit(self.raw)
            self.active = False
            if "WARNING" in notices or "ERROR" in notices:
                raise DatabaseUnavailable("uncertain_application_commit")
            if binding:
                proof.protect(self.raw, self.config, self.schema, binding, self.original)
            self.write = self.replay = False
            self.hasher = hashlib.sha256()
        except psycopg.Error:
            raise DatabaseUnavailable("uncertain_application_commit") from None
        finally:
            self.raw.remove_notice_handler(handler)

    @database_boundary
    def rollback(self):
        if not self.closed and self.active:
            self.raw.execute("ROLLBACK")
        self.active = self.write = self.replay = False

    def close(self):
        if not self.closed:
            self.raw.close()  # Rolls back open work and releases ALL session locks.
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        try:
            self.rollback() if kind else self.commit()
        finally:
            self.close()


def connect(service, component="main"):
    try:
        if (held := getattr(_LOCAL, "workers", {}).get(service)) is not None:
            _worker_probe(held, load(service))
        return Connection(service, component)
    except DatabaseUnavailable:
        raise
    except Exception:
        raise DatabaseUnavailable("database_connection_unavailable") from None


def require_schema(service, component="main", required_tables=()):
    with connect(service, component) as connection:
        tables = {row[0] for row in connection.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()")}
        if not set(required_tables) <= tables or not {"ha_receipts", "ha_barriers"} <= tables:
            raise DatabaseUnavailable("database_migration_required")


@contextmanager
def state_transaction(service, key, default):
    if not isinstance(key, str) or not 0 < len(key) <= 128:
        raise DatabaseUnavailable("invalid_shared_state_key")
    groups = getattr(_LOCAL, "states", None)
    if groups is None:
        groups = _LOCAL.states = {}
    outer = service not in groups
    if outer:
        groups[service] = {"connection": connect(service, "state"), "entries": {}, "failed": False}
    group = groups[service]
    connection, entries = group["connection"], group["entries"]
    try:
        if key not in entries:
            row = connection.execute("SELECT payload FROM shared_state WHERE key=?", (key,)).fetchone()
            if row is not None:
                connection.protect_replay()
            payload = copy.deepcopy(row[0] if row else default)
            entries[key] = (payload, proof.digest(payload), row is not None)
        yield entries[key][0]
        if outer:
            if group["failed"]:
                raise DatabaseUnavailable("nested_state_transaction_failed")
            from psycopg.types.json import Jsonb
            for state_key, (payload, previous, existed) in entries.items():
                if proof.digest(payload) != previous or not existed:
                    connection.execute("INSERT INTO shared_state(key,payload,version) VALUES(?,?,1) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,version=shared_state.version+1",
                                       (state_key, Jsonb(payload)))
            connection.commit()
    except BaseException:
        group["failed"] = True
        if outer:
            connection.rollback()
        raise
    finally:
        if outer:
            connection.close()
            groups.pop(service, None)


def active_state_connection(service):
    """Only the current state transaction may attach atomic PG file chunks."""
    group = getattr(_LOCAL, "states", {}).get(service)
    if not group or group["failed"] or group["connection"].closed:
        raise DatabaseUnavailable("active_state_transaction_required")
    return group["connection"]


def read_state(service, key, default):
    with connect(service, "state") as connection:
        row = connection.execute("SELECT payload FROM shared_state WHERE key=?", (key,)).fetchone()
        return copy.deepcopy(row[0] if row else default)


def write_state(service, key, payload):
    with state_transaction(service, key, payload) as current:
        if not isinstance(current, dict) or not isinstance(payload, dict):
            raise DatabaseUnavailable("shared_state_object_required")
        current.clear()
        current.update(copy.deepcopy(payload))


@contextmanager
def worker_scope(service):
    scopes = getattr(_LOCAL, "workers", None)
    if scopes is None:
        scopes = _LOCAL.workers = {}
    if service in scopes:
        _worker_probe(scopes[service], load(service))
        yield
        _worker_probe(scopes[service], load(service))
        return
    config = load(service)
    connection = proof.raw_connection(config, "primary")
    try:
        acquire_lock(connection, "SELECT pg_advisory_lock_shared(%s)", (ROLE_LOCK,))
        _worker_probe(connection, config)
        owned = acquire_lock(connection, "SELECT pg_try_advisory_lock(hashtextextended(%s,0))", ("worker:" + service,)).fetchone()[0]
        if not owned:
            raise DatabaseUnavailable("worker_already_running")
        scopes[service] = connection
        yield
        _worker_probe(connection, config)
    finally:
        scopes.pop(service, None)
        connection.close()


def _worker_probe(connection, config):
    _admit(proof.observe(connection, config), config, write=True, worker=True)


def require_worker(service):
    with worker_scope(service):
        pass


def worker_owned(service):
    config = load(service)
    connection = proof.raw_connection(config, "primary")
    try:
        acquire_lock(connection, "SELECT pg_advisory_lock_shared(%s)", (ROLE_LOCK,))
        value = proof.observe(connection, config)
        _admit(value, config)
        return config["node"] == value["worker"] and value["writes_open"] and value["history_safe"]
    finally:
        connection.close()
