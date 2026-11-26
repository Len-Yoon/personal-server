"""Append one restricted Windows boot public key on the exact VM A guest."""

import ipaddress
import os
from pathlib import Path
import re
import stat

from . import native_boot_ssh_scope as scope, native_stdio_bridge as wire
from .native_guest_worker import _read_json, restore
from .native_windows_boot_identity import _public_key


AUTHORIZED = Path('/root/.ssh/authorized_keys')
ERROR = 'guest_boot_ssh_enrollment_unconfirmed'


def _entry(public_key, host, launcher, launcher_sha256, scope_path):
    wire._need(type(host) is str and type(public_key) is str and public_key.isascii(), ERROR)
    try:
        peer = ipaddress.IPv4Address(host)
    except ValueError:
        raise wire.BridgeRejected(ERROR) from None
    wire._need(str(peer) == host and peer.is_private
               and not (peer.is_loopback or peer.is_link_local or peer.is_unspecified or peer.is_reserved)
               and wire._hash(launcher_sha256), ERROR)
    exact = scope._launcher_path(launcher)
    key, _ = _public_key(public_key.encode('ascii'))
    wire._need(key == public_key, ERROR)
    forced = f'/usr/bin/python3 -I -B -S {scope_path} {exact} {launcher_sha256}'
    return f'from="{peer}/32",restrict,command="{forced}" {key} n100-ha-windows-boot\n'.encode('ascii')


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_ctime_ns, info.st_mtime_ns)


def enroll_vm_a(*, public_key, host, launcher, launcher_sha256, current_keys_sha256,
                scope_path, scope_sha256, guest_boot_sha256, installation_sha256):
    """Preserve all existing key lines and append only to the approved snapshot."""
    import fcntl

    wire._need(os.geteuid() == 0 and all(map(wire._hash,
               (current_keys_sha256, scope_sha256, guest_boot_sha256, installation_sha256)))
               and wire._digest(Path('/proc/sys/kernel/random/boot_id').read_bytes()) == guest_boot_sha256, ERROR)
    raw_installation, identity = _read_json(restore.INSTALLATION)
    wire._need(identity.get('node') == 'A' and wire._digest(raw_installation) == installation_sha256, ERROR)
    script = Path(scope_path)
    wire._need(str(script) == scope_path and script.parent == scope.ROOT
               and re.fullmatch(r'native-boot-ssh-scope-[a-f0-9]{64}\.py', script.name)
               and script.name == f'native-boot-ssh-scope-{scope_sha256}.py', ERROR)
    line = _entry(public_key, host, launcher, launcher_sha256, scope_path)
    scope._read_launcher(scope._launcher_path(launcher), launcher_sha256)
    for parent in (AUTHORIZED.parent, *AUTHORIZED.parent.parents):
        info = parent.lstat()
        wire._need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022, ERROR)
    with os.fdopen(os.open(script, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as supplied:
        info = os.fstat(supplied.fileno())
        wire._need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                   and not info.st_mode & 0o022 and 0 < info.st_size <= 131072, ERROR)
        raw = supplied.read(131073)
        wire._need(len(raw) == info.st_size and wire._digest(raw) == scope_sha256
                   and _identity(os.fstat(supplied.fileno())) == _identity(script.lstat()) == _identity(info), ERROR)
    with os.fdopen(os.open(AUTHORIZED, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK), 'r+b', buffering=0) as keys:
        fcntl.flock(keys, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = os.fstat(keys.fileno())
        wire._need(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
                   and not before.st_mode & 0o077 and 0 < before.st_size <= 131072, ERROR)
        raw = keys.read(131073)
        wire._need(len(raw) == before.st_size and raw.endswith(b'\n') and b'\x00' not in raw, ERROR)
        same_key = [row for row in raw.splitlines() if public_key.split()[1].encode() in row.split()]
        if same_key:
            wire._need(same_key == [line.rstrip(b'\n')] and raw.endswith(line)
                       and wire._digest(raw[:-len(line)]) == current_keys_sha256, ERROR)
        else:
            wire._need(wire._digest(raw) == current_keys_sha256
                       and _identity(os.fstat(keys.fileno())) == _identity(AUTHORIZED.lstat()) == _identity(before)
                       and len(raw) + len(line) <= 131072, ERROR)
            wire._need(keys.write(line) == len(line), ERROR)
            raw += line
        os.fsync(keys.fileno())
        keys.seek(0)
        wire._need(keys.read(len(raw) + 1) == raw
                   and _identity(os.fstat(keys.fileno())) == _identity(AUTHORIZED.lstat()), ERROR)
        return {'authorized_keys_sha256': wire._digest(raw), 'scope_sha256': scope_sha256}
