#!/usr/bin/python3
"""One-time root installation with policy queries and reversible replacement."""

import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile

HELPER = Path('/usr/local/libexec/personal-server/n100-k3s-operations')
HELPER_MODULE = Path('/usr/local/libexec/personal-server/n100-k3s-operations.py')
SUDOERS = Path('/etc/sudoers.d/personal-server-n100-k3s-operations')
OPERATIONS = ('diagnose', 'verify_news_observability', 'apply_news_observability')
ENV = {'LC_ALL': 'C', 'PATH': '/usr/sbin:/usr/bin:/sbin:/bin'}
GENERIC = ['/usr/local/bin/k3s', 'kubectl', 'version', '--client']
AS_WINDOW = ['/usr/sbin/runuser', '-u', 'window', '--', '/usr/bin/env', '-i',
             'LC_ALL=C', 'PATH=/usr/sbin:/usr/bin:/sbin:/bin', '/usr/bin/sudo']


class InstallError(Exception):
    pass


def capture(argv):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=ENV, timeout=15, check=False)


def query(command):
    """Never executes command. Exit 0 + exact stdout = ALLOWED; exit 1 +
    exact C-locale denial stderr = DENIED; every other result = ERROR.
    Captured policy output stays in memory and is never logged.
    """
    try:
        result = capture([*AS_WINDOW, '-n', '-l', '-u', 'root', '--', *command])
        expected = ' '.join(command)
        out = result.stdout.decode('utf-8', errors='strict').strip()
        err = result.stderr.decode('utf-8', errors='strict').strip()
        if result.returncode == 0 and out == expected and not err:
            return 'ALLOWED'
        denial = (r"Sorry, user window is not allowed to execute '" + re.escape(expected)
                  + r"' as root on [A-Za-z0-9_.-]+\.")
        if result.returncode == 1 and not out and re.fullmatch(denial, err):
            return 'DENIED'
        return 'ERROR'
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return 'ERROR'


def exact_policy(text):
    """Unknown formatting or additional grants fail closed for local review."""
    blocks = text.split('Sudoers entry:')
    if len(blocks) != 4:
        return False
    expected = {f'{HELPER} {operation}' for operation in OPERATIONS}
    found = set()
    for block in blocks[1:]:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) != 4 or lines[0] != 'RunAsUsers: root' or lines[2] != 'Commands:':
            return False
        if not lines[1].startswith('Options: '):
            return False
        if set(lines[1][9:].split(', ')) != {'!authenticate', '!setenv'}:
            return False
        if lines[3] not in expected or lines[3] in found:
            return False
        found.add(lines[3])
    return found == expected


def postcheck():
    if query(GENERIC) != 'DENIED':
        raise InstallError()
    for operation in OPERATIONS:
        if query([str(HELPER), operation]) != 'ALLOWED':
            raise InstallError()
    result = capture([*AS_WINDOW, '-n', '-ll', '-u', 'root'])
    if result.returncode or result.stderr or not exact_policy(result.stdout.decode('utf-8')):
        raise InstallError()


def trusted_parent(path):
    for parent in (path, *path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise InstallError()


def ensure_directory(path, created):
    if not path.exists() and not path.is_symlink():
        ensure_directory(path.parent, created)
        path.mkdir(mode=0o755)
        created.append(path)
        path.chmod(0o755)
    trusted_parent(path)


def replace_files(wrapper_data, module_data, policy_data, helper=HELPER,
                  module=HELPER_MODULE, sudoers=SUDOERS):
    """Stage on destination filesystems; atomic replacement and rollback."""
    staged = []
    replaced = []
    rollback_failed = False
    try:
        for path, data, mode in [(module, module_data, 0o755),
                                 (helper, wrapper_data, 0o755),
                                 (sudoers, policy_data, 0o440)]:
            directory = Path(tempfile.mkdtemp(prefix='.n100-install-', dir=path.parent))
            backup = directory / 'previous'
            staged.append((path, directory, backup))
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise InstallError()
                shutil.copy2(path, backup, follow_symlinks=False)
                os.chown(backup, info.st_uid, info.st_gid)
            candidate = directory / 'candidate'
            candidate.write_bytes(data)
            os.chown(candidate, 0, 0)
            candidate.chmod(mode)
        for path, directory, backup in staged:
            os.replace(directory / 'candidate', path)
            replaced.append((path, backup))
        postcheck()
    except BaseException:
        for path, backup in reversed(replaced):
            try:
                if backup.exists():
                    os.replace(backup, path)
                else:
                    path.unlink()
            except OSError:
                rollback_failed = True
        if rollback_failed:
            # Preserve private backups for an administrator if disk/FS recovery fails.
            raise InstallError() from None
        raise
    finally:
        if not rollback_failed:
            for _, directory, _ in staged:
                shutil.rmtree(directory)


def install():
    try:
        pwd.getpwnam('window')
    except KeyError:
        raise InstallError() from None
    # No command execution or filesystem mutation precedes this policy gate.
    if query(GENERIC) != 'DENIED':
        raise InstallError()
    dependency = capture(['/usr/bin/python3', '-I', '-c', 'import yaml'])
    if dependency.returncode or dependency.stdout or dependency.stderr:
        raise InstallError()
    source = Path(__file__).with_name('n100-k3s-operations-helper.py')
    if source.is_symlink() or not source.is_file():
        raise InstallError()
    helper_data = source.read_bytes()
    if not helper_data.startswith(b'#!/usr/bin/python3\n'):
        raise InstallError()
    wrapper = Path(__file__).with_name('n100-k3s-operations-wrapper.sh')
    if wrapper.is_symlink() or not wrapper.is_file():
        raise InstallError()
    wrapper_data = wrapper.read_bytes()
    if wrapper_data != (b'#!/bin/sh\nexec /usr/bin/python3 -I '
                        b'/usr/local/libexec/personal-server/n100-k3s-operations.py "$@"\n'):
        raise InstallError()
    policy_data = ''.join(f'window ALL=(root) NOPASSWD: NOSETENV: {HELPER} {op}\n'
                          for op in OPERATIONS).encode()
    with tempfile.TemporaryDirectory(prefix='n100-policy-') as directory:
        policy = Path(directory) / 'policy'
        policy.write_bytes(policy_data)
        policy.chmod(0o600)
        result = capture(['/usr/sbin/visudo', '-cf', str(policy)])
        if result.returncode or result.stderr:
            raise InstallError()
    created = []
    try:
        ensure_directory(HELPER.parent, created)
        trusted_parent(SUDOERS.parent)
        replace_files(wrapper_data, helper_data, policy_data)
    except BaseException:
        for directory in reversed(created):
            try:
                directory.rmdir()
            except OSError:
                pass  # A retained recovery backup must not be removed.
        raise


def interrupted(_signum, _frame):
    raise InstallError()


def main():
    if os.geteuid() != 0 or len(sys.argv) != 1:
        return 1
    os.umask(0o077)
    try:
        install()
    except Exception:
        print('n100_helper_install=FAIL', file=sys.stderr)
        return 1
    print('n100_helper_install=PASS')
    return 0


if __name__ == '__main__':
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, interrupted)
    sys.exit(main())
