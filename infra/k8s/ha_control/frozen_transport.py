"""Stream only manifest-bound frozen files over an authenticated transport."""
import ctypes
import errno
import hashlib
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile

from ha_runtime import seven_capture
from ha_runtime.seven_migration import HEX, json_payload, read_verified

from .legacy_writers import TransitionBlocked, _need


MAGIC = b"N100HA-FROZEN-1\n"
END = b"N100HA-END-1\n"
MAX_METADATA = 32 * 1024 * 1024
MAX_ARTIFACT = 1024 * 1024 * 1024
NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,149}\Z")


def _entry(value, names, maximum):
    _need(type(value) is dict and set(value) == {"file", "sha256", "bytes"},
          "frozen_transport_descriptor_invalid")
    name, digest, size = value["file"], value["sha256"], value["bytes"]
    _need(type(name) is str and NAME.fullmatch(name) and name not in names
          and type(digest) is str and HEX.fullmatch(digest) and type(size) is int and 0 <= size <= maximum,
          "frozen_transport_descriptor_invalid")
    names.add(name)
    return value


def _catalog(raw, metadata):
    manifest = json_payload(raw)
    artifacts = manifest["artifacts"]
    _need(type(artifacts) is dict and 1 <= len(artifacts) <= 20000, "frozen_transport_catalog_invalid")
    names = {"manifest.json"}
    freeze = json_payload(metadata(_entry(manifest["freeze_receipt"], names, MAX_METADATA)))
    audit = json_payload(metadata(_entry(freeze["capture_audit"], names, MAX_METADATA)))
    evidence = audit["evidence"]
    _need(type(evidence) is dict and set(evidence) == {"runtime", "writer_graph", "writer_closure", "dispositions"},
          "frozen_transport_catalog_invalid")
    return ([_entry(evidence[key], names, MAX_METADATA) for key in sorted(evidence)]
            + [_entry(artifacts[key], names, MAX_ARTIFACT) for key in sorted(artifacts)])


def _manifest_digest(expected):
    _need(type(expected) is str and HEX.fullmatch(expected), "frozen_transport_manifest_unbound")
    return expected


def _read(stream, size):
    value = stream.read(size)
    _need(type(value) is bytes and len(value) == size, "frozen_transport_truncated")
    return value


def _publish(staging, target):
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "linux" and hasattr(libc, "renameat2"):
        rename, flag = libc.renameat2, 1  # RENAME_NOREPLACE
    elif sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        rename, flag = libc.renameatx_np, 4  # RENAME_EXCL
    else:
        raise TransitionBlocked("frozen_transport_exclusive_publish_unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if rename(fd, os.fsencode(staging.name), fd, os.fsencode(target.name), flag):
            if ctypes.get_errno() in (errno.EEXIST, errno.ENOTEMPTY):
                raise TransitionBlocked("frozen_transport_destination_exists")
            raise TransitionBlocked("frozen_transport_exclusive_publish_failed")
        try:
            os.fsync(fd)
        except OSError:
            raise TransitionBlocked("frozen_transport_publish_unconfirmed") from None
    finally:
        os.close(fd)


def send_bundle(source, manifest_sha256, stream):
    """Send from a private directory into a blocking, buffered binary stream."""
    root = seven_capture.trusted(Path(source), directory=True)
    expected = _manifest_digest(manifest_sha256)
    manifest = root / "manifest.json"
    raw = read_verified(manifest, expected, manifest.stat().st_size)
    stream.write(MAGIC)
    stream.write(struct.pack(">Q", len(raw)))
    stream.write(raw)

    def send_metadata(entry):
        value = read_verified(root / entry["file"], entry["sha256"], entry["bytes"])
        stream.write(value)
        return value

    entries = _catalog(raw, send_metadata)
    for entry in entries:
        with tempfile.TemporaryFile(dir=root) as checked:
            read_verified(root / entry["file"], entry["sha256"], entry["bytes"], load=False, sink=checked)
            checked.seek(0)
            shutil.copyfileobj(checked, stream, 65536)
    stream.write(END)
    stream.flush()
    return expected


def receive_bundle(destination, manifest_sha256, stream, *, max_bytes):
    """Verify every file before publishing a new private restore directory."""
    target = Path(destination)
    _need(target.is_absolute() and ".." not in target.parts and not os.path.lexists(target)
          and type(max_bytes) is int and max_bytes > 0, "frozen_transport_destination_invalid")
    seven_capture.trusted(target.parent, directory=True)
    expected = _manifest_digest(manifest_sha256)
    _need(_read(stream, len(MAGIC)) == MAGIC, "frozen_transport_version_invalid")
    (size,) = struct.unpack(">Q", _read(stream, 8))
    _need(0 < size <= min(MAX_METADATA, max_bytes), "frozen_transport_size_invalid")
    raw = _read(stream, size)
    _need(hashlib.sha256(raw).hexdigest() == expected, "frozen_transport_manifest_unbound")
    consumed = size
    with tempfile.TemporaryDirectory(prefix=".frozen-transfer-", dir=target.parent) as directory:
        staging = Path(directory)
        seven_capture._write(staging / "manifest.json", raw)

        def receive_metadata(entry):
            nonlocal consumed
            consumed += entry["bytes"]
            _need(consumed <= max_bytes, "frozen_transport_size_invalid")
            data = _read(stream, entry["bytes"])
            _need(hashlib.sha256(data).hexdigest() == entry["sha256"], "frozen_transport_hash_mismatch")
            seven_capture._write(staging / entry["file"], data)
            return data

        entries = _catalog(raw, receive_metadata)
        remaining = sum(entry["bytes"] for entry in entries)
        _need(consumed + remaining <= max_bytes and remaining <= shutil.disk_usage(staging).free,
              "frozen_transport_size_invalid")
        for entry in entries:
            path = staging / entry["file"]
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            digest = hashlib.sha256()
            with os.fdopen(fd, "wb") as output:
                left = entry["bytes"]
                while left:
                    chunk = _read(stream, min(left, 65536))
                    output.write(chunk)
                    digest.update(chunk)
                    left -= len(chunk)
                output.flush()
                os.fsync(output.fileno())
            _need(digest.hexdigest() == entry["sha256"], "frozen_transport_hash_mismatch")
        _need(_read(stream, len(END)) == END and not os.path.lexists(target), "frozen_transport_unconfirmed")
        fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        _publish(staging, target)
    return expected
