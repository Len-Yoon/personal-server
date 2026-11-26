"""Forced SSH entry point for a dedicated Windows VM A boot key.

Install this file in root-owned, non-writable custody and invoke its canonical
path from an authorized_keys entry with `restrict`. The launcher path and
SHA-256 are fixed arguments of that entry. No command text received from the
client is executed. The verified launcher initializes the guest import roots.
"""

import hashlib
import os
from pathlib import Path
import re
import shlex
import stat
import sys


ROOT = Path('/var/lib/n100-ha/cutover')
NAME = 'native-guest-worker.py'
HASH = re.compile(r'[a-f0-9]{64}\Z')
STAGED = re.compile(r'recovery-source-[a-f0-9]{64}\Z')
ENTRY = b'from ha_control.native_guest_worker import main\nraise SystemExit(main())'
LIMIT = 131072


def _need(condition):
    if not condition:
        raise ValueError('guest_boot_ssh_scope_rejected')


def _launcher_path(value):
    _need(type(value) is str)
    path = Path(value)
    _need(str(path) == value and path.name == NAME and
          (path.parent == ROOT or path.parent.parent == ROOT and STAGED.fullmatch(path.parent.name)))
    return path


def _read_launcher(path, pin):
    _need(type(pin) is str and HASH.fullmatch(pin))
    for parent in path.parents:
        info = parent.lstat()
        _need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), 'rb') as stream:
        before = os.fstat(stream.fileno())
        _need(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
              and not before.st_mode & 0o022 and 0 < before.st_size <= LIMIT)
        raw = stream.read(LIMIT + 1)
        after = os.fstat(stream.fileno())
        _need(len(raw) == before.st_size == after.st_size and before.st_ctime_ns == after.st_ctime_ns
              and hashlib.sha256(raw).hexdigest() == pin)
    prefix, entry, tail = raw.partition(ENTRY)
    _need(entry == ENTRY and tail in (b'', b'\n') and bool(prefix.strip()))
    return prefix


def _run(original_command, launcher, launcher_sha256):
    _need(os.geteuid() == 0 and type(original_command) is str and 0 < len(original_command) <= 4096)
    path = _launcher_path(launcher)
    prefix = _read_launcher(path, launcher_sha256)
    exec(compile(prefix, str(path), 'exec'), {'__name__': '__main__', '__file__': str(path)})

    from ha_control import native_guest_ssh as ssh, native_guest_worker as worker, native_stdio_bridge as wire

    expected = shlex.join(('/usr/bin/python3', '-I', '-B', '-S', '-c', ssh.BOOTSTRAP, str(path), launcher_sha256))
    _need(original_command == expected)
    allowed = frozenset((wire.GUEST_OBSERVE_INITIAL_RECOVERY_HOLD, wire.GUEST_OBSERVE_CAR_ISOLATION,
                         wire.GUEST_OBSERVE_INITIAL_POSTBOOT, wire.GUEST_START_INITIAL_POSTBOOT))
    return worker.main(authorize=lambda request: request.node == 'A' and request.verb in allowed)


def main():
    try:
        _need(len(sys.argv) == 3 and sys.flags.isolated and sys.flags.dont_write_bytecode and sys.flags.no_site)
        return _run(os.environ.get('SSH_ORIGINAL_COMMAND'), *sys.argv[1:])
    except (OSError, TypeError, ValueError):
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
