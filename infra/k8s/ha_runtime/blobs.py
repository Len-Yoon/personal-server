"""Immutable PG chunks on the active Portal state transaction; no file store.

Namespace and blob rows use the same Connection/commit/replication proof. This
module never commits, opens a second connection, grants privileges or runs GC.
"""
import hashlib
import re

from .config import DatabaseUnavailable
from . import db

SERVICE = 'portal-web'
CHUNK_BYTES = 65536
MAX_BLOB_BYTES = 32 * 1024 * 1024
MAX_NAMESPACE_BYTES = 1024 * 1024 * 1024
MAX_OBJECTS = 10000
HEX = re.compile('[0-9a-f]{64}\\Z')


def _fail():
    raise DatabaseUnavailable('pg_blob_unavailable') from None


def setup_namespace(admin, schema):
    if schema != 's_portal_web_state':
        _fail()
    admin.execute('CREATE TABLE "s_portal_web_state".ha_blob_objects ('
                  "sha256 text PRIMARY KEY CHECK(sha256 ~ '^[0-9a-f]{64}$'), "
                  'size bigint NOT NULL CHECK(size BETWEEN 0 AND 33554432), '
                  'chunk_count integer NOT NULL CHECK(chunk_count BETWEEN 0 AND 512))')
    admin.execute('CREATE TABLE "s_portal_web_state".ha_blob_chunks ('
                  'sha256 text NOT NULL REFERENCES "s_portal_web_state".ha_blob_objects(sha256), '
                  'ordinal integer NOT NULL CHECK(ordinal BETWEEN 0 AND 511), '
                  'payload bytea NOT NULL CHECK(octet_length(payload) BETWEEN 1 AND 65536), '
                  'PRIMARY KEY(sha256,ordinal))')


def require_ready(service=SERVICE):
    if service != SERVICE:
        _fail()
    connection = db.active_state_connection(service)
    row = connection.execute('SELECT ha.enforced_vm_blob_ready()').fetchone()
    if row is None or len(row) != 1 or row[0] is not True:
        _fail()
    return connection


def _get(connection, identifier, size):
    if not isinstance(identifier, str) or not HEX.fullmatch(identifier) or type(size) is not int or not 0 <= size <= MAX_BLOB_BYTES:
        _fail()
    row = connection.execute('SELECT size,chunk_count FROM ha_blob_objects WHERE sha256=?', (identifier,)).fetchone()
    expected_count = (size + CHUNK_BYTES - 1) // CHUNK_BYTES
    if (row is None or len(row) != 2 or type(row[0]) is not int or type(row[1]) is not int
            or tuple(row) != (size, expected_count)):
        _fail()
    rows = connection.execute('SELECT ordinal,payload FROM ha_blob_chunks WHERE sha256=? ORDER BY ordinal', (identifier,)).fetchall()
    if len(rows) != expected_count:
        _fail()
    payload = bytearray()
    for ordinal, row in enumerate(rows):
        if len(row) != 2 or type(row[0]) is not int or row[0] != ordinal or not isinstance(row[1], (bytes, bytearray, memoryview)):
            _fail()
        chunk = bytes(row[1])
        expected_size = min(CHUNK_BYTES, size - len(payload))
        if len(chunk) != expected_size:
            _fail()
        payload.extend(chunk)
    if len(payload) != size or hashlib.sha256(payload).hexdigest() != identifier:
        _fail()
    return bytes(payload)


def get(service, identifier, size):
    connection = require_ready(service)
    payload = _get(connection, identifier, size)
    connection.protect_replay()
    return payload


def put(service, stream, *, max_bytes=MAX_BLOB_BYTES, max_namespace_bytes=MAX_NAMESPACE_BYTES, validate_prefix=None):
    connection = require_ready(service)
    if (type(max_bytes) is not int or not 0 <= max_bytes <= MAX_BLOB_BYTES
            or type(max_namespace_bytes) is not int or not 0 <= max_namespace_bytes <= MAX_NAMESPACE_BYTES):
        _fail()
    buffered = bytearray(); hashed = hashlib.sha256(); size = 0; prefix = bytearray()
    while True:
        chunk = stream.read(min(CHUNK_BYTES, max_bytes - size + 1))
        if not isinstance(chunk, bytes):
            _fail()
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise ValueError('업로드 파일 용량 제한을 초과합니다.')
        prefix.extend(chunk[:max(0, 8192 - len(prefix))])
        hashed.update(chunk); buffered.extend(chunk)
    if validate_prefix is not None:
        validate_prefix(bytes(prefix))
    # Reads may return short chunks without EOF. Normalize boundaries so get
    # accepts any legitimate stream delivery pattern, including empty files.
    payload = bytes(buffered)
    del buffered
    chunks = [payload[offset:offset + CHUNK_BYTES] for offset in range(0, size, CHUNK_BYTES)]
    identifier = hashed.hexdigest()
    existing = connection.execute('SELECT size,chunk_count FROM ha_blob_objects WHERE sha256=?', (identifier,)).fetchone()
    if existing is not None:
        if _get(connection, identifier, size) != payload:
            _fail()
        connection.protect_replay()
        return {'sha256': identifier, 'size': size}
    row = connection.execute('SELECT count(*),CAST(COALESCE(sum(size),0) AS bigint) FROM ha_blob_objects').fetchone()
    if row is None or len(row) != 2 or any(type(value) is not int or value < 0 for value in row):
        _fail()
    if row[0] >= MAX_OBJECTS or row[1] + size > max_namespace_bytes:
        raise ValueError('공유 파일 저장소의 보존 용량 제한을 초과합니다.')
    inserted = connection.execute('INSERT INTO ha_blob_objects(sha256,size,chunk_count) VALUES(?,?,?) '
                                  'ON CONFLICT(sha256) DO NOTHING', (identifier, size, len(chunks))).rowcount
    if inserted == 1:
        for ordinal, chunk in enumerate(chunks):
            connection.execute('INSERT INTO ha_blob_chunks(sha256,ordinal,payload) VALUES(?,?,?)', (identifier, ordinal, chunk))
    elif inserted != 0 or _get(connection, identifier, size) != payload:
        _fail()
    return {'sha256': identifier, 'size': size}
