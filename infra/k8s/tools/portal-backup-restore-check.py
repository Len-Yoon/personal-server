#!/usr/bin/env python3
"""Download and validate one authoritative encrypted backup in disposable scratch.

No Kubernetes API, production volume, upload, or evidence-writing operations.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import resource
import shlex
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile

MAX_EXTRACT_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBERS = 100000
MAX_DOWNLOAD_BYTES = MAX_EXTRACT_BYTES + MAX_MEMBERS * 1024
MAX_EVIDENCE_BYTES = 16384
MAX_CONFIG_BYTES = 1024 * 1024
ENCRYPTED_CONFIG_HEADER = b'RCLONE_ENCRYPT_V0:'
REMOTE = 'gdrive:PersonalServer-encrypted-backups'
EXPECTED = dict(schema_version='1', scope='portal', backup_status='success', encrypted='true',
                restore_status='success', source_runtime='k3s-pvc',
                restore_check='sqlite_quick_check', restore_path_check='success')
KEYS = set(EXPECTED) | {'backup_id', 'artifact_digest', 'source_digest', 'backup_completed_at',
                        'restore_verified_at', 'evidence_expires_at'}


class RestoreError(ValueError):
    """Safe failure; private details are never printed by the CLI."""


def require(condition):
    if not condition:
        raise RestoreError('validation failed')


def memory_backed(path, mountinfo=Path('/proc/self/mountinfo')):
    """Require a tmpfs mount, rather than trusting a caller's directory name."""
    try:
        mounts = []
        for line in mountinfo.read_text().splitlines():
            left, right = line.split(' - ', 1)
            encoded = left.split()[4]
            mount = Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), encoded))
            if path.is_relative_to(mount):
                mounts.append((len(mount.parts), right.split()[0]))
        return bool(mounts) and max(mounts)[1] == 'tmpfs'
    except (OSError, ValueError, IndexError):
        return False


def encrypted_config_snapshot(path, *, allow_symlink=False, private=False):
    """Read and validate the same bounded inode snapshot that will be copied."""
    flags = os.O_RDONLY | (0 if allow_symlink else os.O_NOFOLLOW)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RestoreError('validation failed') from error
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_size <= MAX_CONFIG_BYTES)
        if private:
            require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600)
        raw = stream.read(MAX_CONFIG_BYTES + 1)
    require(len(raw) <= MAX_CONFIG_BYTES)
    # rclone permits comments and blank lines before the encryption marker.
    meaningful = [line.strip() for line in raw.splitlines()
                  if line.strip() and not line.strip().startswith((b'#', b';'))]
    require(bool(meaningful) and meaningful[0] == ENCRYPTED_CONFIG_HEADER)
    return raw


def download_with_memory_config(command, runner, config_work_dir, credential_dir, **kwargs):
    """Only the encrypted config may be updated in disposable memory scratch."""
    source = credential_dir / 'rclone-config'
    require(command[0] == 'rclone' and command.count('--config') == 1)
    index = command.index('--config') + 1
    require(command[index] == str(source))
    # Projected Secret files are symlinks. Copy exactly the bytes checked here,
    # without reopening the source across a projected-volume atomic transition.
    raw = encrypted_config_snapshot(source, allow_symlink=True)
    with tempfile.TemporaryDirectory(prefix='rclone-', dir=config_work_dir) as temporary:
        config = Path(temporary) / 'rclone.conf'
        descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(raw)
        args = list(command)
        args[index] = str(config)
        try:
            runner(args, **kwargs)
        finally:
            # Reject plaintext/permissions/link regressions even on a failed
            # provider call; TemporaryDirectory still removes the private copy.
            encrypted_config_snapshot(config, private=True)
            require(encrypted_config_snapshot(source, allow_symlink=True) == raw)


def read_pairs(path: Path, limit=MAX_EVIDENCE_BYTES):
    require(path.is_file() and path.stat().st_size <= limit)
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        require('=' in line)
        key, value = line.split('=', 1)
        require(key and value and key.strip() == key and value.strip() == value and key not in values)
        values[key] = value
    return values


def timestamp(value):
    require(re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', value) is not None)
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise RestoreError('validation failed') from error


def validate_evidence(values, now):
    require(set(values) == KEYS)
    require(all(values[key] == value for key, value in EXPECTED.items()))
    require(re.fullmatch(r'portal-[A-Za-z0-9][A-Za-z0-9._-]{0,127}', values['backup_id']) is not None)
    for key in ('artifact_digest', 'source_digest'):
        require(re.fullmatch(r'sha256:[0-9a-f]{64}', values[key]) is not None)
    completed, restored, expires = [timestamp(values[key]) for key in
                                    ('backup_completed_at', 'restore_verified_at', 'evidence_expires_at')]
    # Producer computes expiry with a later clock read than completion.
    # Freshness is bounded by the backup age, independently of expiry.
    require(now - timedelta(days=1) <= completed <= restored <= now < expires)


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(directory):
    """Match GNU find/sort -z/xargs sha256sum | sha256sum, including escaping."""
    require(directory.is_dir() and not directory.is_symlink())
    files = []
    for parent, dirs, names in os.walk(directory, followlinks=False):
        for name in dirs + names:
            path = Path(parent) / name
            require(not path.is_symlink() and (path.is_dir() or path.is_file()))
        files.extend(Path(parent) / name for name in names)
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda entry: os.fsencode('./' + entry.relative_to(directory).as_posix())):
        name = './' + path.relative_to(directory).as_posix()
        escaped = '\\' in name or '\n' in name
        name = name.replace('\\', '\\\\').replace('\n', '\\n')
        row = ('\\' if escaped else '') + file_hash(path) + '  ' + name + '\n'
        digest.update(os.fsencode(row))
    return digest.hexdigest()


def source_digest(data):
    value = tree_digest(data / 'files') + '\n' + tree_digest(data / 'portal-web-state') + '\n'
    return 'sha256:' + hashlib.sha256(value.encode('ascii')).hexdigest()


def extract_safe(archive, destination):
    require(archive.stat().st_size <= MAX_EXTRACT_BYTES + MAX_MEMBERS * 1024)
    with tarfile.open(archive, 'r:') as stream:
        members = []
        seen = set()
        total = 0
        for member in stream:
            require(len(members) < MAX_MEMBERS)
            parts = PurePosixPath(member.name).parts
            require(parts and not member.name.startswith('/') and '..' not in parts)
            path = PurePosixPath(*parts).as_posix()
            require(path not in seen and (member.isdir() or member.isfile()) and not member.issparse())
            require(path == 'manifest.txt' or path == 'data' or path.startswith('data/files/') or
                    path == 'data/files' or path.startswith('data/portal-web-state/') or path == 'data/portal-web-state')
            require(path != 'manifest.txt' or member.isfile())
            require(path not in ('data', 'data/files', 'data/portal-web-state') or member.isdir())
            require(member.size >= 0)
            seen.add(path)
            total += member.size
            require(total <= MAX_EXTRACT_BYTES)
            members.append((member, path))
        # Validate all entries before creating any extracted files. Never use extractall.
        for member, path in members:
            target = destination / path
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with stream.extractfile(member) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                target.chmod(0o600)


def limit_download_file_size():
    """Apply the disk-file cap in the standalone CLI's rclone child only.

    preexec_fn runs after fork; never change the verifier parent's limits.
    Keep any stricter inherited soft/hard cap. This CLI is single threaded.
    """
    inherited = resource.getrlimit(resource.RLIMIT_FSIZE)
    limits = tuple(MAX_DOWNLOAD_BYTES if value == resource.RLIM_INFINITY
                   else min(value, MAX_DOWNLOAD_BYTES) for value in inherited)
    resource.setrlimit(resource.RLIMIT_FSIZE, limits)


def check_restore(evidence, work_dir, credential_dir, remote=REMOTE, *, config_work_dir=None, now=None, runner=None):
    now = now or datetime.now(timezone.utc)
    values = read_pairs(evidence)
    validate_evidence(values, now)
    require(remote == REMOTE)
    require(work_dir.is_absolute() and work_dir.is_dir() and not work_dir.is_symlink())
    scratch_root = work_dir.resolve()
    require(not evidence.resolve().is_relative_to(scratch_root))
    require(not credential_dir.resolve().is_relative_to(scratch_root))
    require(config_work_dir is not None and config_work_dir.is_absolute()
            and config_work_dir.is_dir() and not config_work_dir.is_symlink())
    config_root = config_work_dir.resolve()
    require(memory_backed(config_root))
    require(not config_root.is_relative_to(scratch_root) and not scratch_root.is_relative_to(config_root))
    require(not credential_dir.resolve().is_relative_to(config_root)
            and not config_root.is_relative_to(credential_dir.resolve()))
    require(not evidence.resolve().is_relative_to(config_root))
    for name in ('rclone-config', 'rclone-config-passphrase', 'age-identity'):
        # Kubernetes Secret mounts use symlinks; follow only these read-only inputs.
        require((credential_dir / name).is_file() and (credential_dir / name).stat().st_size > 0)
    runner = runner or subprocess.run
    with tempfile.TemporaryDirectory(prefix='portal-restore-', dir=work_dir) as temporary:
        scratch = Path(temporary)
        ciphertext, archive = scratch / 'download.age', scratch / 'restore.tar'
        env = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
               'HOME': str(scratch), 'LANG': 'C.UTF-8'}
        def run(command):
            # Passphrase and identity stay in their Secret files. Discard diagnostics
            # because even stderr may include tokens or sensitive internal paths.
            options = dict(check=True, timeout=300, cwd=scratch, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            if command[0] == 'rclone':
                options['preexec_fn'] = limit_download_file_size
                download_with_memory_config(command, runner, config_root, credential_dir, **options)
            else:
                runner(command, **options)
        run(['rclone', '--config', str(credential_dir / 'rclone-config'), '--password-command',
             '/usr/bin/cat ' + shlex.quote(str(credential_dir / 'rclone-config-passphrase')),
             'copyto', '--log-level', 'ERROR', '--retries', '1', '--low-level-retries', '1',
             '--max-transfer', str(MAX_DOWNLOAD_BYTES), '--cutoff-mode', 'hard',
             '--buffer-size', '0', '--multi-thread-streams', '0',
             remote + '/' + values['backup_id'] + '.tar.age', str(ciphertext)])
        require(ciphertext.is_file() and ciphertext.stat().st_size <= MAX_DOWNLOAD_BYTES)
        require('sha256:' + file_hash(ciphertext) == values['artifact_digest'])
        run(['age', '-d', '-i', str(credential_dir / 'age-identity'), '-o', str(archive), str(ciphertext)])
        restored = scratch / 'restored'
        restored.mkdir()
        extract_safe(archive, restored)
        require(read_pairs(restored / 'manifest.txt') == {'source_runtime': 'k3s-pvc', 'source_digest': values['source_digest']})
        require(source_digest(restored / 'data') == values['source_digest'])
        database = restored / 'data/portal-web-state/homeops.sqlite3'
        require(database.is_file())
        # Read only the restored scratch database, including its committed WAL.
        # immutable=1 would silently ignore WAL pages copied by the producer.
        try:
            with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
                require(connection.execute('PRAGMA quick_check').fetchall() == [('ok',)])
        except sqlite3.Error as error:
            raise RestoreError('validation failed') from error


def main(argv=None):
    class SafeParser(argparse.ArgumentParser):
        def error(self, message):
            raise RestoreError('invalid arguments')
    parser = SafeParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--credential-dir', type=Path, required=True)
    parser.add_argument('--config-work-dir', type=Path, required=True)
    parser.add_argument('--remote', default=REMOTE)
    try:
        args = parser.parse_args(argv)
        check_restore(args.evidence, args.work_dir, args.credential_dir, args.remote,
                      config_work_dir=args.config_work_dir)
    except Exception:
        print('portal_backup_restore_check=FAIL')
        return 1
    print('portal_backup_restore_check=PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
