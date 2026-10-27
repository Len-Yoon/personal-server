#!/usr/bin/env python3
"""Offline consumer of an operator-supplied, already-decrypted Book PVC backup.

Default --check verifies input bindings and tar bounds without extracting data.
--go extracts only into a fresh private temporary directory, validates the exact
backup tree and current Book database read contract, and removes its own copy.
The offline modes run no network clients, subprocesses, Kubernetes or app
writers. Opt-in --go --runtime uses the local Docker Unix socket and a pinned
Book image for read-only functional checks inside owned isolated scratch;
no credentials or production mounts are supplied. Input provenance and preparation timestamps are operator assertions, not proof
of a distinct physical host. This tool never reports service recovery or RTO.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import signal
import sys
import sqlite3
import stat
import tarfile
import tempfile
import time

MAX_ARCHIVE = 512 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_MEMBERS = 10000
MAX_METADATA = 65536
DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")
RUNTIME_CONFIG = {"database": "/app/data/book_memo.sqlite3", "user": "10001:10001",
                  "network": "none", "readonly_root": True, "published_ports": False,
                  "log_driver": "none", "memory_bytes": 268435456, "pids": 64,
                  "cpus": 1, "read_only_functional_verification": True,
                  "scratch_database_writable": True, "scratch_database_mode": "0600",
                  "production_credentials": False, "cap_drop": ["ALL"],
                  "no_new_privileges": True, "tmpfs": "/tmp:rw,noexec,nosuid,size=16m",
                  "command": ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8003"],
                  "environment": {"BOOK_MEMO_DB_PATH": "/app/data/book_memo.sqlite3", "DEMO_MODE": "0",
                                  "PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": "127.0.0.1,localhost"}}
PHASES = ("acquisition", "decryption", "image_acquisition", "destination_preparation")
TABLE_COLUMNS = {
    "books": {"id", "isbn", "title", "reading_status", "progress_percent", "version"},
    "book_chapters": {"id", "book_id", "title", "position", "is_done", "version"},
    "book_memos": {"id", "book_id", "chapter_id", "title", "content", "version"},
    "memo_tags": {"memo_id", "tag", "tag_key", "position"},
    "memo_revisions": {"id", "memo_id", "book_id", "snapshot", "saved_at"},
    "deleted_memos": {"memo_id", "book_id", "snapshot", "tags", "version", "deleted_at"},
    "write_requests": {"request_id", "operation", "payload_hash", "result_id"},
}
REQUIRED_INDEXES = {"idx_memo_revisions_parent", "idx_deleted_memos_parent",
                    "idx_memo_revisions_lookup", "idx_memo_tags_key", "idx_books_home_order",
                    "idx_book_chapters_book", "idx_book_memos_book"}


class RestoreError(RuntimeError):
    """Fixed public error code; never include paths, DB content or diagnostics."""


def fail(code, *, cleanup_owner=None):
    error = RestoreError(code)
    error.cleanup_owner = cleanup_owner
    raise error


def utc():
    return datetime.now(timezone.utc).isoformat()


def private_input(path, maximum):
    """Open once, reject links/shared files; callers retain the verified descriptor."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        stream = os.fdopen(descriptor, "rb")
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or not 0 < info.st_size <= maximum):
            stream.close()
            fail("private_input_required")
        return stream
    except OSError:
        fail("private_input_required")


def sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail("duplicate_metadata_key")
        result[key] = value
    return result


def timestamp(value):
    if not isinstance(value, str):
        fail("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        fail("invalid_timestamp")
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        fail("UTC_timestamp_required")
    return parsed


def manifest_input(path):
    with private_input(path, MAX_METADATA) as stream:
        value = json.loads(stream.read(MAX_METADATA + 1), object_pairs_hook=unique_object)
    required = {"schema_version", "provenance", "source_host_label", "destination_host_label",
                "independent_host_asserted", "backup_evidence_sha256", "plaintext_sha256",
                "image_digest", "configuration_sha256", "source_main_sha256", "source_service_sha256", "preparation"}
    if (not isinstance(value, dict) or set(value) != required
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["provenance"] not in ("production_backup", "synthetic_fixture")
            or type(value["independent_host_asserted"]) is not bool):
        fail("invalid_manifest")
    for key in ("source_host_label", "destination_host_label"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", value[key]):
            fail("invalid_host_label")
    if (value["source_host_label"] == value["destination_host_label"]
            and value["independent_host_asserted"]):
        fail("contradictory_host_assertion")
    for key in ("backup_evidence_sha256", "plaintext_sha256", "image_digest", "configuration_sha256", "source_main_sha256", "source_service_sha256"):
        if not isinstance(value[key], str) or not DIGEST.fullmatch(value[key]):
            fail("invalid_digest")
    preparation = value["preparation"]
    if not isinstance(preparation, dict) or set(preparation) - set(PHASES):
        fail("invalid_preparation")
    for phase in preparation.values():
        if (not isinstance(phase, dict) or set(phase) != {"started_at", "completed_at"}
                or timestamp(phase["completed_at"]) < timestamp(phase["started_at"])
                or timestamp(phase["completed_at"]) > datetime.now(timezone.utc)):
            fail("invalid_preparation")
    return value


def evidence_input(path, expected):
    with private_input(path, MAX_METADATA) as stream:
        raw = stream.read(MAX_METADATA + 1)
    if sha(raw) != expected:
        fail("backup_evidence_digest_mismatch")
    evidence = {}
    for line in raw.decode("utf-8").splitlines():
        if not line:
            continue
        key, separator, value = line.partition("=")
        if not separator or key in evidence:
            fail("invalid_backup_evidence")
        evidence[key] = value
    fixed = {"schema_version": "1", "scope": "book-memo", "backup_status": "success",
             "encrypted": "true", "restore_status": "success", "source_runtime": "k3s-pvc",
             "restore_check": "sqlite_quick_check", "restore_path_check": "success"}
    if any(evidence.get(key) != value for key, value in fixed.items()):
        fail("invalid_backup_evidence")
    for key in ("source_digest", "artifact_digest"):
        if not DIGEST.fullmatch(evidence.get(key, "")):
            fail("invalid_backup_evidence")
    if not re.fullmatch(r"book-[a-zA-Z0-9-]{1,100}", evidence.get("backup_id", "")):
        fail("invalid_backup_evidence")
    completed = timestamp(evidence.get("backup_completed_at"))
    verified = timestamp(evidence.get("restore_verified_at"))
    expires = timestamp(evidence.get("evidence_expires_at"))
    if not completed <= verified < expires or verified > datetime.now(timezone.utc):
        fail("invalid_backup_evidence")
    return evidence


def budget(deadline):
    if time.monotonic() > deadline:
        fail("offline_validation_timeout")


def archive_members(archive, deadline):
    seen, total, members = set(), 0, []
    for item in archive:
        budget(deadline)
        name = PurePosixPath(item.name)
        if (name.is_absolute() or not name.parts or str(name) != item.name.rstrip("/")
                or ".." in name.parts or "\\" in item.name or len(item.name) > 1024
                or any(ord(char) < 32 for char in item.name)
                or not (item.isfile() or item.isdir()) or item.issparse() or name in seen
                or item.size < 0):
            fail("invalid_archive_entry")
        seen.add(name)
        total += item.size
        if total > MAX_EXPANDED or len(seen) > MAX_MEMBERS:
            fail("archive_limit_exceeded")
        members.append(item)
    if not members:
        fail("empty_archive")
    return members


def tree_digest(root, deadline):
    """Identical path/type/content digest contract to book-pvc-backup-verify.py."""
    result = hashlib.sha256()
    for entry in sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix()):
        budget(deadline)
        result.update(entry.relative_to(root).as_posix().encode("utf-8") + b"\0")
        result.update(b"dir\0" if entry.is_dir() else b"file\0")
        if entry.is_file():
            with entry.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    budget(deadline)
                    result.update(block)
    return "sha256:" + result.hexdigest()


def validate_database(root, deadline):
    database = root / "book_memo.sqlite3"
    if not database.is_file() or database.is_symlink():
        fail("database_missing")
    # An immutable reader ignores WAL; never silently omit acknowledged writes.
    for suffix, code in (("-wal", "nonempty_WAL_requires_separate_consistent_snapshot"),
                         ("-journal", "nonempty_rollback_journal_requires_separate_consistent_snapshot")):
        sidecar = root / ("book_memo.sqlite3" + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            fail(code)
    # Immutable input avoids journal writes. A snapshot requiring WAL recovery is
    # rejected by integrity/read checks rather than silently modifying a backup.
    with sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        if hasattr(db, "enable_load_extension"):
            db.enable_load_extension(False)
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA query_only=ON")
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        if db.execute("PRAGMA quick_check(1)").fetchall() != [("ok",)]:
            fail("sqlite_integrity_failed")
        present = dict(db.execute("SELECT name,type FROM sqlite_master"))
        if (any(present.get(table) != "table" for table in TABLE_COLUMNS)
                or any(present.get(index) != "index" for index in REQUIRED_INDEXES)):
            fail("book_schema_incompatible")
        for table, columns in TABLE_COLUMNS.items():
            actual = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            if not columns <= actual:
                fail("book_schema_incompatible")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            fail("sqlite_foreign_key_failed")
        # Fixed functional read of existing Book relationships; no content is
        # emitted and no source app is imported (which could initialize a DB).
        db.execute("SELECT b.id, b.title, m.id, c.id FROM books b "
                   "LEFT JOIN book_memos m ON m.book_id=b.id "
                   "LEFT JOIN book_chapters c ON c.id=m.chapter_id LIMIT 1").fetchone()
        return {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in TABLE_COLUMNS}


def runtime_config_digest():
    return sha(json.dumps(RUNTIME_CONFIG, sort_keys=True, separators=(",", ":")).encode())


def own_scratch_database(database, expected_digest):
    """Replace only the copied DB in the already owned scratch volume.

    The pinned app requires writable storage even for readiness. This import
    creates a byte-identical 0600 file owned by its unprivileged current user;
    the original source snapshot is never mounted in this volume.
    """
    database = Path(database)
    parent = database.parent
    parent_info = parent.lstat()
    if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.getuid():
        raise RuntimeError("scratch_directory_not_owned")
    temporary = parent / ".book-restore-owned.stage"
    created = False
    def file_digest(path):
        result = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                result.update(block)
        return "sha256:" + result.hexdigest()
    try:
        descriptor = os.open(database, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or not 0 < info.st_size <= 256 * 1024 * 1024
                    or stat.S_IMODE(info.st_mode) != 0o444):
                raise RuntimeError("scratch_import_invalid")
            output_descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created = True
            with os.fdopen(output_descriptor, "wb") as output:
                digest = hashlib.sha256()
                copied = 0
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    copied += len(block)
                    if copied > 256 * 1024 * 1024:
                        raise RuntimeError("scratch_import_invalid")
                    digest.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if "sha256:" + digest.hexdigest() != expected_digest or copied != info.st_size:
                raise RuntimeError("scratch_import_digest_mismatch")
        if file_digest(temporary) != expected_digest:
            raise RuntimeError("scratch_import_digest_mismatch")
        os.replace(temporary, database)
        created = False
        restored = database.lstat()
        if (not stat.S_ISREG(restored.st_mode) or restored.st_uid != os.getuid()
                or stat.S_IMODE(restored.st_mode) != 0o600 or restored.st_nlink != 1
                or file_digest(database) != expected_digest):
            raise RuntimeError("scratch_import_ownership_or_digest_mismatch")
    finally:
        if created:
            temporary.unlink(missing_ok=True)


def runtime_adapter(owner, deadline):
    """Reuse bounded local endpoint and identity-checked cleanup, not fixture probes."""
    tools = Path(__file__).resolve().parents[3] / "book-memo" / "tools"
    sys.path.insert(0, str(tools))
    try:
        from container_runtime import LocalDocker
    finally:
        sys.path.remove(str(tools))

    class ActualBookDocker(LocalDocker):
        def _labels(self):
            return {"org.personal-server.lab.owner": self.owner,
                    "org.personal-server.lab.kind": "book-independent-backup"}

        def _verify_runtime(self, data, record):
            # Reuse every existing resource/mount/security check, with the one
            # deliberately different fixed DB path and explicit no-log policy.
            translated = dict(data)
            translated["Config"] = dict(data["Config"])
            translated["Config"]["Env"] = [
                "BOOK_MEMO_DB_PATH=/app/data/fixture.sqlite3" if value ==
                "BOOK_MEMO_DB_PATH=/app/data/book_memo.sqlite3" else value
                for value in data["Config"]["Env"]]
            super()._verify_runtime(translated, record)
            if ("BOOK_MEMO_DB_PATH=/app/data/book_memo.sqlite3" not in data["Config"]["Env"]
                    or data["HostConfig"].get("LogConfig", {}).get("Type") != "none"):
                fail("unsafe_runtime_configuration")

        def launch(self, image_id, database):
            data = self._inspect("image", image_id)
            config = data.get("Config", {})
            labels = config.get("Labels") or {}
            base_env = {"PATH", "LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256"}
            if (data.get("Id") != image_id or data.get("Os") != "linux"
                    or config.get("User") != "10001:10001" or config.get("WorkingDir") != "/app"
                    or config.get("Entrypoint") not in (None, []) or config.get("Volumes")
                    or config.get("OnBuild") or config.get("Healthcheck")
                    or config.get("Cmd") != ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8003"]
                    or labels.get("org.opencontainers.image.source") != "https://github.com/Len-Yoon/personal-server"
                    or labels.get("org.opencontainers.image.title") != "book-memo"
                    or any(value.partition("=")[0] not in base_env for value in config.get("Env", []))):
                fail("image_contract_mismatch")
            volume = self.volume()
            self._counter += 1
            name = f"ps-book-restore-{self.owner}-{self._counter}"
            if self._present("container", name):
                fail("runtime_name_collision")
            image = {"id": image_id}
            self._containers[name] = {"id": None, "image": image, "volume": volume,
                                      "role": "actual-backup", "clean": False, "database_sha256": sha(database.read_bytes())}
            args = ["container", "create", "--pull", "never", "--name", name]
            for key, value in sorted((self._labels() | {"org.personal-server.lab.role": "actual-backup"}).items()):
                args += ["--label", f"{key}={value}"]
            args += ["--network", "none", "--read-only", "--log-driver", "none",
                     "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m", "--cap-drop", "ALL",
                     "--security-opt", "no-new-privileges", "--pids-limit", "64",
                     "--memory", "256m", "--cpus", "1", "--user", "10001:10001",
                     "--mount", f"type=volume,source={volume},target=/app/data"]
            for value in ("BOOK_MEMO_DB_PATH=/app/data/book_memo.sqlite3", "DEMO_MODE=0",
                          "PYTHONDONTWRITEBYTECODE=1", "NO_PROXY=127.0.0.1,localhost"):
                args += ["--env", value]
            args.append(image_id)
            identity = self._run(args).strip()
            if not re.fullmatch(r"[a-f0-9]{64}", identity):
                fail("runtime_identity_invalid")
            self._containers[name]["id"] = identity
            created = self._container(identity)
            self._verify_runtime(created, self._containers[name])
            # The transport copy is 0444. After app startup closes its schema
            # read connection, the app UID imports an identical owned 0600 copy
            # inside this new volume so the real readiness W_OK check can pass.
            with tempfile.TemporaryDirectory(prefix="book-runtime-copy-") as directory:
                upload = Path(directory) / "book_memo.sqlite3"
                shutil.copyfile(database, upload)
                os.chmod(upload, 0o444)
                self._run(["container", "cp", str(upload), f"{identity}:/app/data/book_memo.sqlite3"])
            self._run(["container", "start", identity])
            data = self._container(identity)
            self._verify_runtime(data, self._containers[name])
            if not data.get("State", {}).get("Running"):
                fail("application_not_running")
            return identity

        def functional_read(self, identity):
            data = self._container(identity)
            _, record = self._record(identity)
            self._verify_runtime(data, record)
            if not data.get("State", {}).get("Running"):
                fail("application_not_running")
            # Private HTTP bodies and row contents never leave the container.
            # Bound both response bytes and readiness wait before reading home.
            script = 'import hashlib,json,os,sqlite3,stat,sys,time,urllib.request\nfrom pathlib import Path\n' + inspect.getsource(own_scratch_database) + '''
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def request(path):
    with opener.open('http://127.0.0.1:8003'+path,timeout=2) as response:
        if response.status!=200: raise RuntimeError('http')
        if len(response.read(8*1024*1024+1))>8*1024*1024: raise RuntimeError('size')
# /health is served only after the app's lifespan/schema initialization has
# completed. The isolated runtime has no other clients or published ports.
end=time.monotonic()+30
while True:
    try:
        request('/health');break
    except Exception:
        if time.monotonic()>end: raise RuntimeError('startup')
        time.sleep(.2)
db_path=Path('/app/data/book_memo.sqlite3')
if os.getuid()!=10001 or os.getgid()!=10001: raise RuntimeError('user')
own_scratch_database(db_path,sys.argv[1])
end=time.monotonic()+30
while True:
    try:
        request('/ready');break
    except Exception:
        if time.monotonic()>end: raise RuntimeError('ready')
        time.sleep(.2)
request('/health');request('/')
from app.services import book_service as service
db_path=Path('/app/data/book_memo.sqlite3')
if service.DB_PATH!=db_path: raise RuntimeError('path')
with sqlite3.connect(db_path.as_uri()+'?mode=ro&immutable=1',uri=True) as db:
    db.row_factory=sqlite3.Row
    if not service._schema_ready(db): raise RuntimeError('schema')
    tables=('books','book_chapters','book_memos','memo_tags','memo_revisions','deleted_memos','write_requests')
    counts={t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in tables}
result={'row_counts':counts,'db_sha256':'sha256:'+hashlib.sha256(db_path.read_bytes()).hexdigest(),
        'main_sha256':'sha256:'+hashlib.sha256(Path('/app/app/main.py').read_bytes()).hexdigest(),
        'service_sha256':'sha256:'+hashlib.sha256(Path('/app/app/services/book_service.py').read_bytes()).hexdigest(),
        'ready_http':200,'health_http':200,'home_http':200}
print(json.dumps(result,sort_keys=True))'''
            return json.loads(self._run(["exec", identity, "/usr/local/bin/python", "-c", script, record["database_sha256"]], cap=70, limit=8192))

    return ActualBookDocker(owner, deadline, lambda: False)


def verify_runtime(destination, manifest, counts, deadline):
    if manifest["configuration_sha256"] != runtime_config_digest():
        fail("runtime_configuration_digest_mismatch")
    engine = None
    owner = secrets.token_hex(16)
    started = time.monotonic()
    error = None
    result = None
    try:
        engine = runtime_adapter(owner, deadline)
        engine.info()
        database = destination / "book_memo.sqlite3"
        expected_db = sha(database.read_bytes())
        identity = engine.launch(manifest["image_digest"], database)
        read = engine.functional_read(identity)
        if (read.get("row_counts") != counts or read.get("db_sha256") != expected_db
                or read.get("main_sha256") != manifest["source_main_sha256"]
                or read.get("service_sha256") != manifest["source_service_sha256"]
                or any(read.get(key) != 200 for key in ("ready_http", "health_http", "home_http"))):
            fail("runtime_functional_or_source_mismatch")
        result = {"status": "passed", "http": {"ready": 200, "health": 200, "home": 200},
                  "database_hash_and_counts_match": True, "image_source_hashes_match": True,
                  "image_digest_kind": "docker_local_image_config_sha256",
                  "network": "none", "published_ports": False, "readonly_root": True,
                  "credentials_supplied": False, "verification_seconds": time.monotonic() - started}
    except (RuntimeError, OSError, ValueError, KeyError, TypeError, ImportError, KeyboardInterrupt):
        error = "application_runtime_failed"
    finally:
        if engine is not None:
            try:
                cleanup = engine.cleanup()
                if not isinstance(cleanup, dict) or cleanup.get("ok") is not True:
                    error = "runtime_cleanup_failed_private_scratch_may_remain"
                elif result is not None:
                    result["cleanup"] = cleanup
            except (RuntimeError, OSError, ValueError, TypeError, KeyboardInterrupt):
                error = "runtime_cleanup_failed_private_scratch_may_remain"
    if error:
        fail(error, cleanup_owner=owner if error.startswith("runtime_cleanup_failed") else None)
    return result



def run(archive_path, evidence_path, manifest_path, *, execute=False, runtime=False, timeout=120):
    if type(timeout) is not int or not 1 <= timeout <= 600:
        fail("invalid_timeout")
    if runtime and not execute:
        fail("runtime_requires_go")
    started = time.monotonic()
    deadline = started + timeout
    manifest = manifest_input(manifest_path)
    evidence = evidence_input(evidence_path, manifest["backup_evidence_sha256"])
    report = {"schema_version": 1, "started_at": utc(), "status": "input_check_passed",
              "scope": "offline_backup_validation_only", "provenance": manifest["provenance"],
              "provenance_verification": "operator_assertion_only",
              "independent_host_verified": False, "physical_host_loss_tested": False,
              "host_assertion": {key: manifest[key] for key in (
                  "source_host_label", "destination_host_label", "independent_host_asserted")},
              "backup_evidence_sha256": manifest["backup_evidence_sha256"],
              "plaintext_sha256": manifest["plaintext_sha256"],
              "tree_sha256": evidence["source_digest"], "tree_verification": "not_executed",
              "ciphertext_sha256_expected": evidence["artifact_digest"],
              "ciphertext_download_and_decryption": "not_executed_by_this_tool",
              "backup_evidence_expired": timestamp(evidence["evidence_expires_at"]) < datetime.now(timezone.utc),
              "release_inputs": {"image_digest": manifest["image_digest"],
                                 "configuration_sha256": manifest["configuration_sha256"],
                                 "image_and_configuration_verified": False},
              "preparation": {phase: ({"status": "operator_supplied_unverified", **manifest["preparation"][phase],
                                      "seconds": (timestamp(manifest["preparation"][phase]["completed_at"]) -
                                                  timestamp(manifest["preparation"][phase]["started_at"])).total_seconds()}
                                      if phase in manifest["preparation"] else {"status": "missing"})
                              for phase in PHASES},
              "application_runtime": "not_executed", "service_recovery_verified": False,
              "total_rto_seconds": None, "rpo_seconds": None,
              "total_rto_missing_stages": [phase for phase in PHASES if phase not in manifest["preparation"]] +
                                          ["application_start_and_functional_verification"],
              "total_rto_unverified_stages": list(manifest["preparation"]) + ["loss_boundary_and_independent_host"],
              "restored_data_removed": False}
    with private_input(archive_path, MAX_ARCHIVE) as source, tempfile.TemporaryDirectory(prefix="book-offline-restore-") as directory:
        work = Path(directory)
        os.chmod(work, 0o700)
        # Hash while copying to a private descriptor to avoid validation/extract
        # TOCTOU against an operator replacing or changing the supplied file.
        with tempfile.TemporaryFile(dir=work) as snapshot:
            digest, size = hashlib.sha256(), 0
            for block in iter(lambda: source.read(1024 * 1024), b""):
                budget(deadline)
                size += len(block)
                if size > MAX_ARCHIVE:
                    fail("archive_limit_exceeded")
                digest.update(block)
                snapshot.write(block)
            if "sha256:" + digest.hexdigest() != manifest["plaintext_sha256"]:
                fail("plaintext_digest_mismatch")
            snapshot.seek(0)
            with tarfile.open(fileobj=snapshot, mode="r:") as archive:
                members = archive_members(archive, deadline)
                report["archive_member_count"] = len(members)
                if execute:
                    destination = work / "restored"
                    destination.mkdir(mode=0o700)
                    for item in members:
                        budget(deadline)
                        target = destination / item.name
                        if item.isdir():
                            target.mkdir(mode=0o700, parents=True, exist_ok=True)
                        else:
                            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                            content = archive.extractfile(item)
                            if content is None:
                                fail("invalid_archive_entry")
                            with target.open("xb") as output:
                                os.chmod(target, 0o600)
                                for block in iter(lambda: content.read(1024 * 1024), b""):
                                    budget(deadline)
                                    output.write(block)
                    if tree_digest(destination, deadline) != evidence["source_digest"]:
                        fail("restored_tree_digest_mismatch")
                    report["tree_verification"] = "passed"
                    report["table_row_counts"] = validate_database(destination, deadline)
                    if tree_digest(destination, deadline) != evidence["source_digest"]:
                        fail("restored_data_changed_during_validation")
                    report["status"] = "offline_restore_validation_passed"
                    report["database_read_contract"] = "current_book_tables_columns_indexes_and_relationship_read"
                    report.setdefault("offline_validation_seconds", time.monotonic() - started)
                    if runtime:
                        report["application_runtime"] = verify_runtime(destination, manifest, report["table_row_counts"], deadline)
                        report["release_inputs"]["image_and_configuration_verified"] = True
                        report["total_rto_missing_stages"].remove("application_start_and_functional_verification")
                        # Scope deliberately stays local functional verification;
                        # source provenance and physical host independence remain unverified.
                        report["status"] = "isolated_application_read_verification_passed"
    report["restored_data_removed"] = True
    report.setdefault("offline_validation_seconds", time.monotonic() - started)
    report["consumer_seconds"] = time.monotonic() - started
    report["completed_at"] = utc()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--manifest", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="default: input check; no data extraction")
    mode.add_argument("--go", action="store_true", help="validate a temporary offline restored copy")
    parser.add_argument("--runtime", action="store_true", help="with --go, verify an already supplied pinned local Book image")
    parser.add_argument("--print-runtime-config", action="store_true")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    def interrupted(_number, _frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    if args.print_runtime_config:
        print(json.dumps({"configuration": RUNTIME_CONFIG, "configuration_sha256": runtime_config_digest()}, sort_keys=True))
        return 0
    if any(value is None for value in (args.archive, args.evidence, args.manifest)):
        parser.error("--archive, --evidence and --manifest are required")
    try:
        report = run(args.archive, args.evidence, args.manifest, execute=args.go, runtime=args.runtime, timeout=args.timeout)
    except (RuntimeError, OSError, ValueError, TypeError, KeyError, ImportError, sqlite3.Error, tarfile.TarError, KeyboardInterrupt) as error:
        report = {"schema_version": 1, "status": "failed", "reason": str(error) if isinstance(error, RestoreError) else "offline_input_or_validation_failed",
                  "scope": "offline_backup_validation_only", "service_recovery_verified": False,
                  "total_rto_seconds": None}
        if isinstance(error, RestoreError) and getattr(error, "cleanup_owner", None):
            report["cleanup_owner"] = error.cleanup_owner
        print(json.dumps(report, sort_keys=True))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
