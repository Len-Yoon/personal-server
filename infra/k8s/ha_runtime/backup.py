"""Authenticated whole-database/blob backups with explicit purpose approvals.

No GC, credential export, role/password bootstrap, in-place overwrite or
automatic rearming is provided. Restore requires an empty database
on a newly initialized PostgreSQL system and preprovisioned application roles.
"""
import hashlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import zipfile

from .config import DatabaseUnavailable, SERVICES, _private, _authority_file, _release
from .proof import digest

MAGIC = b"LOCAL-HA-BACKUP-1\x00"
MAX_BYTES = 128 * 1024 * 1024
HEX = re.compile(r"[0-9a-f]{64}\Z")


def _fail(reason):
    raise DatabaseUnavailable(reason) from None


def _guard(admin, *, action=None, object_root=None, artifact_path=None, key_path=None, expected_cluster=None):
    if os.getenv("APP_ENV") == "production":
        return _production_approval(admin, action=action, object_root=object_root,
                                    artifact_path=artifact_path, key_path=key_path,
                                    expected_cluster=expected_cluster)
    if os.environ.get("HA_LOCAL_RUNTIME") != "1" or os.environ.get("APP_ENV") not in {"test", "development"}:
        _fail("isolated_backup_only")
    if (set(admin) != {"host", "port", "dbname", "user", "sslmode", "sslrootcert", "passfile"}
            or admin["host"] not in {"ha-a", "ha-b"} or admin["port"] != 5432
            or admin["dbname"] not in {"ha", "ha_restore"} or admin["user"] != "postgres"
            or admin["sslmode"] != "verify-full"):
        _fail("authenticated_local_backup_only")
    _private(admin["passfile"])
    ca = Path(admin["sslrootcert"])
    if not ca.is_absolute() or ca.is_symlink() or not ca.is_file():
        _fail("trusted_ca_required")
    return None


def _production_approval(admin, *, action, object_root, artifact_path, key_path, expected_cluster):
    """Read, never mint, an immutable root-owned release/purpose-bound grant."""
    try:
        if action not in {"backup-whole", "restore-whole"} or not os.getenv("HA_BACKUP_APPROVAL"):
            _fail("backup_purpose_approval_required")
        release = _release()
        approval = json.loads(_authority_file(os.environ["HA_BACKUP_APPROVAL"]).read_text())
        common = {"action", "expires", "cluster", "system_id", "node", "admin", "object_root", "artifact_path", "key_path"} | set(release)
        fields = common | ({"target_system_id", "artifact_sha256"} if action == "restore-whole" else set())
        if set(approval) != fields or approval["action"] != action:
            _fail("backup_purpose_approval_mismatch")
        if any(approval[key] != value for key, value in release.items()):
            _fail("backup_release_mismatch")
        if (not isinstance(approval["expires"], str) or not approval["expires"].endswith("Z")):
            _fail("backup_approval_expiry_required")
        expiry = datetime.fromisoformat(approval["expires"].removesuffix("Z") + "+00:00")
        now = datetime.now(timezone.utc)
        if expiry.utcoffset() != timedelta(0) or not now < expiry <= now + timedelta(hours=24):
            _fail("backup_approval_expired_or_unbounded")
        if (not isinstance(approval["cluster"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", approval["cluster"])
                or expected_cluster != approval["cluster"] or approval["node"] not in {"A", "B"}
                or not isinstance(approval["system_id"], str) or not re.fullmatch(r"[1-9][0-9]{0,19}", approval["system_id"])):
            _fail("backup_approval_identity_mismatch")
        if (admin != approval["admin"] or not isinstance(admin, dict)
                or set(admin) != {"host", "port", "dbname", "user", "sslmode", "sslrootcert", "passfile"}
                or admin["sslmode"] != "verify-full" or type(admin["port"]) is not int or not 1 <= admin["port"] <= 65535
                or any(not isinstance(admin[k], str) or not admin[k] for k in ("host", "dbname", "user"))
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", admin["host"])):
            _fail("backup_approval_endpoint_mismatch")
        _private(admin["passfile"])
        _authority_file(admin["sslrootcert"])
        for field, actual in (("object_root", object_root), ("artifact_path", artifact_path), ("key_path", key_path)):
            if not isinstance(approval[field], str) or not Path(approval[field]).is_absolute() or str(actual) != approval[field]:
                _fail("backup_approval_path_mismatch")
        if action == "restore-whole" and (
                not isinstance(approval["target_system_id"], str) or not re.fullmatch(r"[1-9][0-9]{0,19}", approval["target_system_id"])
                or approval["target_system_id"] == approval["system_id"]
                or not isinstance(approval["artifact_sha256"], str) or not HEX.fullmatch(approval["artifact_sha256"])):
            _fail("restore_approval_target_required")
        return approval
    except DatabaseUnavailable:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        _fail("backup_approval_invalid")


def _source_identity(connection, approval, cluster, system_id):
    if approval is None:
        if not isinstance(cluster, str) or not re.fullmatch(r"local_[a-zA-Z0-9_]+", cluster):
            _fail("isolated_backup_identity_required")
        return
    if (cluster != approval["cluster"] or system_id != approval["system_id"]
            or connection.execute("SELECT current_setting('cluster_name')").fetchone()[0] != "ha_" + approval["node"]
            or connection.execute("SELECT pg_is_in_recovery()").fetchone()[0]):
        _fail("backup_observed_identity_mismatch")


def _restore_identity(approval, manifest, expected_cluster, artifact_sha256):
    if manifest.get("cluster") != expected_cluster:
        _fail("restore_identity_mismatch")
    if approval is None:
        if manifest.get("scope") != "local-isolated" or not re.fullmatch(r"local_[a-zA-Z0-9_]+", expected_cluster):
            _fail("restore_identity_mismatch")
    elif (manifest.get("scope") != "n100-approved" or manifest.get("system_id") != approval["system_id"]
          or manifest.get("cluster") != approval["cluster"] or artifact_sha256 != approval["artifact_sha256"]):
        _fail("restore_approval_source_mismatch")


def _target_identity(connection, approval, system_id):
    if approval is not None and (system_id != approval["target_system_id"]
            or connection.execute("SELECT current_setting('cluster_name')").fetchone()[0] != "ha_" + approval["node"]):
        _fail("restore_observed_target_mismatch")


def _directory(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        _fail("private_backup_directory_required")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        _fail("private_backup_directory_required")
    return path


def _write_new(path, payload, mode=0o600):
    path = Path(path)
    _directory(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as target:
            target.write(payload)
            target.flush()
            os.fsync(descriptor)
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _key(path):
    value = _private(path).read_bytes()
    if len(value) != 32:
        _fail("backup_key_invalid")
    return value


def _seal(plaintext, key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(plaintext) > MAX_BYTES:
        _fail("backup_size_limit")
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, plaintext, MAGIC)


def _open(ciphertext, key):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not ciphertext.startswith(MAGIC) or not len(MAGIC) + 28 <= len(ciphertext) <= MAX_BYTES + len(MAGIC) + 28:
        _fail("backup_authentication_failed")
    try:
        offset = len(MAGIC)
        return AESGCM(key).decrypt(ciphertext[offset:offset + 12], ciphertext[offset + 12:], MAGIC)
    except (InvalidTag, ValueError):
        _fail("backup_authentication_failed")


def _schemas(connection):
    if connection.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'").fetchone()[0]:
        _fail("isolated_backup_schema_mismatch")
    names = [r[0] for r in connection.execute("SELECT nspname FROM pg_namespace WHERE left(nspname,3)<>'pg_' AND nspname NOT IN ('public','information_schema') ORDER BY nspname").fetchall()]
    prefixes = tuple("s_" + s.replace("-", "_") + "_" for s in SERVICES)
    if "ha" not in names or any(n not in {"ha", "ha_effects"} and not n.startswith(prefixes) for n in names):
        _fail("isolated_backup_schema_mismatch")
    if "ha_effects" in names:
        # Preserve unresolved effects and the old binding through restoration.
        # Storage acceptance is separate from live epoch/system activation.
        from .effects import validate_storage_catalog
        validate_storage_catalog(connection)
    if any(not any(n.startswith("s_" + s.replace("-", "_") + "_") for n in names) for s in SERVICES):
        _fail("backup_requires_all_seven_services")
    return names


def _broker_role_requirements(connection, schemas):
    if 'ha_effects' not in schemas:
        return []
    rows = connection.execute('SELECT broker_role::text FROM ha_effects.binding WHERE singleton=1').fetchall()
    if (len(rows) != 1 or type(rows[0][0]) is not str
            or not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', rows[0][0])):
        _fail('backup_effects_role_inventory_invalid')
    return [{'name': rows[0][0], 'login': True, 'inherit': False, 'superuser': False,
             'create_role': False, 'create_db': False, 'replication': False, 'bypass_rls': False}]


def _check_broker_role_requirements(connection, manifest):
    """Require preprovisioned restricted roles; never bootstrap roles/passwords."""
    declared = manifest.get('required_broker_roles', [])
    includes_effects = 'ha_effects' in manifest.get('logical', {}).get('schemas', [])
    if type(declared) is not list or len(declared) != int(includes_effects):
        _fail('restore_effects_role_inventory_invalid')
    fields = {'name', 'login', 'inherit', 'superuser', 'create_role', 'create_db', 'replication', 'bypass_rls'}
    for role in declared:
        if (type(role) is not dict or set(role) != fields or type(role['name']) is not str
                or not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', role['name'])
                or role['login'] is not True or any(role[key] is not False for key in fields - {'name','login'})):
            _fail('restore_effects_role_inventory_invalid')
        actual = connection.execute('''SELECT rolcanlogin,rolinherit,rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls,
            NOT EXISTS(SELECT 1 FROM pg_auth_members m WHERE m.member=r.oid OR m.roleid=r.oid)
            FROM pg_roles r WHERE rolname=%s''', (role['name'],)).fetchone()
        if actual != (True,False,False,False,False,False,False,True):
            _fail('restore_broker_role_preprovision_required')


def logical_manifest(connection):
    """Compare every table row, sequence high watermark and schema contract."""
    from psycopg import sql
    schemas = _schemas(connection)
    tables, sequences, functions, privileges, triggers, policies = {}, {}, {}, {}, {}, {}
    for schema in schemas:
        for (table,) in connection.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s ORDER BY tablename", (schema,)):
            name = schema + "." + table
            rows = connection.execute(sql.SQL("SELECT to_jsonb(t) FROM {}.{} t").format(sql.Identifier(schema), sql.Identifier(table))).fetchall()
            columns = connection.execute("SELECT a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity,a.attgenerated,pg_get_expr(d.adbin,d.adrelid) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum", (schema, table)).fetchall()
            constraints = connection.execute("SELECT conname,contype,convalidated,pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace WHERE n.nspname=%s AND t.relname=%s ORDER BY conname", (schema, table)).fetchall()
            indexes = connection.execute("SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=%s AND tablename=%s ORDER BY indexname", (schema, table)).fetchall()
            tables[name] = {"rows": len(rows), "sha256": digest(sorted(digest(r[0]) for r in rows)), "columns": columns, "constraints": constraints, "indexes": indexes}
            privileges[name] = connection.execute("SELECT grantor,grantee,privilege_type,is_grantable FROM information_schema.table_privileges WHERE table_schema=%s AND table_name=%s ORDER BY grantor,grantee,privilege_type,is_grantable", (schema, table)).fetchall()
            triggers[name] = connection.execute("SELECT t.tgname,t.tgenabled,pg_get_triggerdef(t.oid) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s AND NOT t.tgisinternal ORDER BY t.tgname", (schema, table)).fetchall()
            tables[name]['row_security'] = connection.execute('SELECT c.relrowsecurity,c.relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s',(schema,table)).fetchone()
            policies[name] = connection.execute('SELECT policyname,permissive,roles,cmd,qual,with_check FROM pg_policies WHERE schemaname=%s AND tablename=%s ORDER BY policyname',(schema,table)).fetchall()
        for (sequence,) in connection.execute("SELECT sequencename FROM pg_sequences WHERE schemaname=%s ORDER BY sequencename", (schema,)):
            state = connection.execute(sql.SQL("SELECT last_value,is_called FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(sequence))).fetchone()
            parameters = connection.execute("SELECT data_type,start_value,min_value,max_value,increment_by,cycle,cache_size FROM pg_sequences WHERE schemaname=%s AND sequencename=%s", (schema, sequence)).fetchone()
            sequences[schema + "." + sequence] = [state, parameters]
        for name, definition in connection.execute("SELECT p.proname,pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=%s ORDER BY p.proname,p.oid", (schema,)):
            functions.setdefault(schema + "." + name, []).append(definition)
    # Normalize tuples, dates and driver values exactly as persisted in JSON.
    return json.loads(json.dumps({"schemas": schemas, "tables": tables, "sequences": sequences, "functions": functions, "privileges": privileges, "triggers": triggers, "policies": policies}, sort_keys=True, default=str))


def _blob_backend(connection):
    if connection.execute("SELECT to_regclass('ha.enforced_namespaces') IS NOT NULL").fetchone() != (True,):
        return 'filesystem'
    row = connection.execute("SELECT definition_sha256 FROM ha.enforced_namespaces WHERE schema_name='s_portal_web_state'").fetchone()
    if row is None:
        return 'filesystem'
    from .enforcement import definition_sha256
    if row != (definition_sha256(),):
        _fail('backup_enforcement_definition_mismatch')
    return 'postgresql'


def _pg_blob(connection, identifier, size):
    from .blobs import CHUNK_BYTES, MAX_BLOB_BYTES
    if not HEX.fullmatch(identifier) or type(size) is not int or not 0 <= size <= MAX_BLOB_BYTES:
        _fail('backup_blob_invalid')
    count = (size + CHUNK_BYTES - 1) // CHUNK_BYTES
    row = connection.execute('SELECT size,chunk_count FROM s_portal_web_state.ha_blob_objects WHERE sha256=%s', (identifier,)).fetchone()
    if row != (size,count):
        _fail('backup_blob_unavailable')
    rows = connection.execute('SELECT ordinal,payload FROM s_portal_web_state.ha_blob_chunks WHERE sha256=%s ORDER BY ordinal', (identifier,)).fetchall()
    if len(rows) != count or any(ordinal != index or len(value) != min(CHUNK_BYTES,size-index*CHUNK_BYTES) for index,(ordinal,value) in enumerate(rows)):
        _fail('backup_blob_invalid')
    value = b''.join(bytes(payload) for _,payload in rows)
    if len(value) != size or hashlib.sha256(value).hexdigest() != identifier:
        _fail('backup_blob_hash_mismatch')
    return value


def object_references(connection):
    """Read live AND trash file references from the exported DB snapshot."""
    row = connection.execute("SELECT payload FROM s_portal_web_state.shared_state WHERE key='file-namespace'").fetchone()
    if row is None or not isinstance(row[0], dict):
        _fail("backup_file_namespace_missing")
    state = row[0]
    if not isinstance(state.get("entries"), dict) or not isinstance(state.get("trash"), dict):
        _fail("backup_file_namespace_invalid")
    groups = [state["entries"]]
    for item in state["trash"].values():
        if not isinstance(item, dict) or not isinstance(item.get("entries"), dict):
            _fail("backup_file_namespace_invalid")
        groups.append(item["entries"])
    result = {}
    for group in groups:
        for item in group.values():
            if not isinstance(item, dict) or item.get("type") not in {"folder", "file"}:
                _fail("backup_file_namespace_invalid")
            if item["type"] == "file":
                sha, size = item.get("sha256"), item.get("size")
                if not isinstance(sha, str) or not HEX.fullmatch(sha) or type(size) is not int or size < 0 or size > MAX_BYTES:
                    _fail("backup_file_reference_invalid")
                if sha in result and result[sha] != size:
                    _fail("backup_file_reference_invalid")
                result[sha] = size
    return dict(sorted(result.items()))


def _blob(root, sha, size):
    if not HEX.fullmatch(sha):
        _fail("backup_blob_invalid")
    try:
        descriptor = os.open(root / sha, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                    or info.st_mode & 0o222 or info.st_size != size or size > MAX_BYTES):
                _fail("backup_blob_invalid")
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                value = source.read(MAX_BYTES + 1)
        finally:
            os.close(descriptor)
    except OSError:
        _fail("backup_blob_unavailable")
    if len(value) != size or hashlib.sha256(value).hexdigest() != sha:
        _fail("backup_blob_hash_mismatch")
    return value


def _pg_tool(tool, admin, path, snapshot=None):
    from psycopg.conninfo import make_conninfo
    command = [tool, "--no-password", "--dbname", make_conninfo(**admin)]
    if tool == "pg_dump":
        command += ["--format=custom", "--snapshot=" + snapshot, "--file", str(path)]
    else:
        command += ["--exit-on-error", "--single-transaction", "--no-owner", str(path)]
    try:
        # Never inherit ambient libpq settings, credentials, tracing or notices.
        result = subprocess.run(command, capture_output=True, timeout=90, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C"})
        if result.returncode or result.stderr:
            _fail("backup_database_tool_failed")
    except (OSError, subprocess.TimeoutExpired):
        _fail("backup_database_tool_failed")


def _connect(admin):
    import psycopg
    connection = psycopg.connect(**admin, autocommit=True, connect_timeout=3, application_name="isolated_ha_backup")
    connection.execute("SET statement_timeout='15000ms'")
    connection.execute("SET lock_timeout='3000ms'")
    connection.execute("SET TIME ZONE 'UTC'")
    if connection.execute("SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()").fetchone() != (True,):
        connection.close()
        _fail("tls_connection_required")
    if connection.execute("SELECT current_setting('cluster_name')").fetchone()[0] not in {"ha_A", "ha_B"}:
        connection.close()
        _fail("isolated_backup_identity_required")
    return connection


def create_backup(admin, object_root, artifact_path, key_path, *, expected_cluster=None):
    """Pin referenced immutable blobs and encrypt a snapshot-matched pg_dump."""
    from psycopg import sql
    approval = _guard(admin, action="backup-whole", object_root=object_root, artifact_path=artifact_path,
                      key_path=key_path, expected_cluster=expected_cluster)
    root, key = _directory(object_root), _key(key_path)
    artifact = Path(artifact_path)
    _directory(artifact.parent)
    try:
        with _connect(admin) as connection, tempfile.TemporaryDirectory(dir=artifact.parent) as temporary:
            # pg_dump's MVCC snapshot covers table data; sequence counters are
            # not transactional. The isolated fixture must stop all app runners
            # first, including sessions that could call nextval independently.
            if connection.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()").fetchone()[0]:
                _fail("quiesced_backup_database_required")
            connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
            connection.execute("SELECT pg_advisory_xact_lock(7142026110601)")
            schemas = _schemas(connection)
            # Ordinary app INSERT/UPDATE/DELETE and authority changes cannot
            # race sequence reads or the snapshot. The fixture has no free-
            # standing sequence writers; its app runners are quiesced first.
            names = connection.execute("SELECT schemaname,tablename FROM pg_tables WHERE schemaname=ANY(%s) ORDER BY schemaname,tablename", (schemas,)).fetchall()
            connection.execute(sql.SQL("LOCK TABLE {} IN SHARE MODE").format(sql.SQL(",").join(sql.Identifier(s, t) for s, t in names)))
            control = connection.execute("SELECT cluster,epoch,revision FROM ha.control WHERE singleton=1").fetchone()
            control_row = connection.execute("SELECT to_jsonb(c) FROM ha.control c WHERE singleton=1").fetchone()[0]
            system_id = connection.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0]
            if not control:
                _fail("isolated_backup_identity_required")
            _source_identity(connection, approval, control[0], system_id)
            if expected_cluster is not None and control[0] != expected_cluster:
                _fail("backup_observed_identity_mismatch")
            snapshot = connection.execute("SELECT pg_export_snapshot()").fetchone()[0]
            logical, objects = logical_manifest(connection), object_references(connection)
            backend = _blob_backend(connection)
            dump = Path(temporary) / "database.dump"
            _pg_tool("pg_dump", admin, dump, snapshot)
            if dump.stat().st_size > MAX_BYTES:
                _fail("backup_size_limit")
            dump_bytes = dump.read_bytes()
            manifest = {"format": 1, "scope": "n100-approved" if approval else "local-isolated", "cluster": control[0], "epoch": control[1], "revision": control[2], "system_id": system_id,
                        "control": control_row, "logical": logical, "objects": objects, "dump_sha256": hashlib.sha256(dump_bytes).hexdigest(), "blob_gc": "disabled-pins-retained", "blob_backend": backend}
            manifest['required_broker_roles'] = _broker_role_requirements(connection, schemas)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True).encode())
                archive.writestr("database.dump", dump_bytes)
                for sha, size in objects.items():
                    archive.writestr("objects/" + sha, _pg_blob(connection,sha,size) if backend=='postgresql' else _blob(root, sha, size))
                    if buffer.tell() > MAX_BYTES:
                        _fail("backup_size_limit")
            encrypted = _seal(buffer.getvalue(), key)
            pins = root / ".backup-pins"
            pins.mkdir(mode=0o700, exist_ok=True)
            _directory(pins)
            identifier = hashlib.sha256(encrypted).hexdigest()
            _write_new(pins / (identifier + ".json"), json.dumps({"artifact_sha256": identifier, "objects": objects, "gc": "disabled"}, sort_keys=True).encode(), 0o400)
            _write_new(artifact, encrypted)
            connection.execute("COMMIT")
            return {"pass": True, "artifact_sha256": identifier, "logical_sha256": digest(logical), "tables": len(logical["tables"]), "sequences": len(logical["sequences"]), "objects": len(objects), "all_services": 7, "pins_retained": True}
    except DatabaseUnavailable:
        raise
    except Exception:
        _fail("backup_creation_failed")


def _unpack(payload):
    """No filesystem extraction; authenticate and validate every member first."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
            names = [m.filename for m in members]
            if len(set(names)) != len(names) or sum(m.file_size for m in members) > MAX_BYTES or "manifest.json" not in names or "database.dump" not in names:
                _fail("backup_archive_invalid")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("format") != 1 or manifest.get("scope") not in {"local-isolated", "n100-approved"} or not isinstance(manifest.get("objects"), dict) or manifest.get('blob_backend','filesystem') not in {'filesystem','postgresql'}:
                _fail("backup_archive_invalid")
            objects = manifest["objects"]
            if set(names) != {"manifest.json", "database.dump", *("objects/" + s for s in objects)}:
                _fail("backup_archive_invalid")
            dump = archive.read("database.dump")
            if hashlib.sha256(dump).hexdigest() != manifest["dump_sha256"]:
                _fail("backup_dump_hash_mismatch")
            blobs = {}
            for sha, size in objects.items():
                if not HEX.fullmatch(sha) or type(size) is not int or size < 0:
                    _fail("backup_archive_invalid")
                value = archive.read("objects/" + sha)
                if len(value) != size or hashlib.sha256(value).hexdigest() != sha:
                    _fail("backup_blob_hash_mismatch")
                blobs[sha] = value
            return manifest, dump, blobs
    except DatabaseUnavailable:
        raise
    except Exception:
        _fail("backup_archive_invalid")


def restore_backup(admin, object_root, artifact_path, key_path, *, expected_cluster):
    """Restore only into an empty, isolated, newly initialized PG system.

    The prior cluster UUID/role mappings/receipts stay intact. New system ID and
    a strictly newer, expired/closed authority require fresh operator proof.
    No old config or replay receipt can reopen a worker by itself.
    """
    approval = _guard(admin, action="restore-whole", object_root=object_root, artifact_path=artifact_path,
                      key_path=key_path, expected_cluster=expected_cluster)
    root = _directory(object_root)
    artifact = _private(artifact_path)
    if artifact.stat().st_size > MAX_BYTES + len(MAGIC) + 28:
        _fail("backup_size_limit")
    encrypted = artifact.read_bytes()
    manifest, dump, blobs = _unpack(_open(encrypted, _key(key_path)))
    _restore_identity(approval, manifest, expected_cluster, hashlib.sha256(encrypted).hexdigest())
    if any(root.iterdir()):
        _fail("empty_restore_object_root_required")
    try:
        with _connect(admin) as connection:
            count = connection.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE left(n.nspname,3)<>'pg_' AND n.nspname<>'information_schema'").fetchone()[0]
            if count or connection.execute("SELECT pg_is_in_recovery()").fetchone()[0]:
                _fail("empty_restore_database_required")
            system_id = connection.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0]
            if system_id == manifest.get("system_id"):
                _fail("fresh_restore_system_required")
            _target_identity(connection, approval, system_id)
            # Prevent any other fixture session from observing a live restored
            # lease during pg_restore's transaction, before authority is sealed.
            others = connection.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()").fetchone()[0]
            if others:
                _fail("quiesced_restore_database_required")
            _check_broker_role_requirements(connection, manifest)
            with tempfile.TemporaryDirectory(dir=artifact.parent) as temporary:
                path = Path(temporary) / "database.dump"
                path.write_bytes(dump)
                path.chmod(0o600)
                _restore_database(admin, path)
            restored = logical_manifest(connection)
            if _broker_role_requirements(connection, restored['schemas']) != manifest.get('required_broker_roles', []):
                _fail('restore_effects_role_inventory_invalid')
            expected = manifest["logical"]
            if 'triggers' not in expected:
                # Older filesystem-only archives predate trigger inventories.
                # Never waive enforcement verification for a PG-blob archive.
                if manifest.get('blob_backend','filesystem') != 'filesystem':
                    _fail('restore_enforcement_inventory_required')
                restored.pop('triggers',None)
            if 'policies' not in expected:
                if manifest.get('blob_backend','filesystem') != 'filesystem':
                    _fail('restore_enforcement_inventory_required')
                restored.pop('policies',None)
                for name,value in restored['tables'].items():
                    if 'row_security' not in expected['tables'][name]:
                        value.pop('row_security',None)
            current_control = connection.execute("SELECT to_jsonb(c),expires_at<clock_timestamp() FROM ha.control c WHERE singleton=1").fetchone()
            row, expired = current_control
            old = manifest["control"]
            changed = {"epoch", "revision", "writes_open", "history_safe", "expires_at"}
            if (not expired or row["epoch"] != manifest["epoch"] + 1 or row["revision"] != manifest["revision"] + 1
                    or row["writes_open"] or row["history_safe"]
                    or {k: v for k, v in row.items() if k not in changed} != {k: v for k, v in old.items() if k not in changed}):
                _fail("restore_authority_seal_failed")
            # All ha.control columns/constraints/indexes remain exact. Only its
            # row hash changes, according to the explicitly verified seal above.
            restored["tables"]["ha.control"]["sha256"] = expected["tables"]["ha.control"]["sha256"]
            if restored != expected or object_references(connection) != manifest["objects"]:
                _fail("restore_logical_mismatch")
            for sha, value in blobs.items():
                if manifest.get('blob_backend','filesystem') == 'postgresql':
                    if _pg_blob(connection,sha,len(value)) != value:
                        _fail('restore_blob_mismatch')
                else:
                    _write_new(root / sha, value, 0o400)
                    _blob(root, sha, len(value))
            return {"pass": True, "all_services": 7, "tables": len(restored["tables"]), "sequences": len(restored["sequences"]), "objects": len(blobs), "logical_sha256": digest(restored), "prior_identity_preserved": True, "fresh_system_id": True, "authority_closed": True, "old_generation_invalidated": True}
    except DatabaseUnavailable:
        raise
    except Exception:
        _fail("backup_restore_failed")


def _restore_database(admin, path):
    """Restore and close old authority atomically, before any reader sees it."""
    from psycopg.conninfo import make_conninfo
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C"}
    try:
        dump = subprocess.run(["pg_restore", "--no-owner", "--file=-", str(path)], capture_output=True, timeout=90, env=environment)
        if dump.returncode or dump.stderr:
            _fail("backup_database_tool_failed")
        seal = b"\nSELECT pg_advisory_xact_lock(7142026110601);\nUPDATE ha.control SET epoch=epoch+1,revision=revision+1,writes_open=false,history_safe=false,expires_at=clock_timestamp()-interval '1 second' WHERE singleton=1;\n"
        result = subprocess.run(["psql", "-X", "--no-password", "--single-transaction", "--set=ON_ERROR_STOP=1", "--dbname", make_conninfo(**admin)], input=dump.stdout + seal, capture_output=True, timeout=90, env=environment)
        if result.returncode:
            _fail("backup_database_tool_failed")
    except (OSError, subprocess.TimeoutExpired):
        _fail("backup_database_tool_failed")
