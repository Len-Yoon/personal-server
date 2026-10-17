"""Bounded, committed Book payloads for the caller-owned local release lab."""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shutil
import stat
import subprocess
import tarfile
import time
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
MAX_ARCHIVE = 8 * 1024 * 1024
MAX_PAYLOAD = 4 * 1024 * 1024
MAX_FILES = 256
ERROR = 'Invalid release artifact'
_SHA = re.compile(r'[0-9a-f]{40}\Z')
_NAME = re.compile(r'(?:[A-Za-z0-9_][A-Za-z0-9_.-]*|\.gitkeep)\Z')


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('utf-8')


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _check_budget(deadline: float | None, interrupted: Callable[[], bool] | None) -> None:
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline) or time.monotonic() >= deadline):
        raise ValueError(ERROR)
    if interrupted is not None and (not callable(interrupted) or interrupted()):
        raise ValueError(ERROR)


def git_environment() -> dict[str, str]:
    """Keep Git fixed to the explicit repository, independent of caller settings."""
    return {
        'PATH': os.defpath,
        'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_CONFIG_SYSTEM': os.devnull,
        'GIT_TERMINAL_PROMPT': '0',
        'LC_ALL': 'C',
    }


def _git_bytes(arguments: list[str], limit: int, *, deadline: float | None = None, interrupted: Callable[[], bool] | None = None) -> bytes:
    """Read a fixed Git command with both a deadline and a hard output cap."""
    _check_budget(deadline, interrupted)
    deadline = min(time.monotonic() + 10, deadline) if deadline is not None else time.monotonic() + 10
    process = subprocess.Popen(['git', '-C', str(ROOT), *arguments], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=git_environment())
    result = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                _check_budget(deadline, interrupted)
                remaining = deadline - time.monotonic()
                if not selector.select(min(.05, max(0, remaining))):
                    continue
                _check_budget(deadline, interrupted)
                block = os.read(process.stdout.fileno(), min(65536, limit + 1 - len(result)))
                if not block:
                    break
                result.extend(block)
                if len(result) > limit:
                    raise ValueError(ERROR)
        while process.poll() is None:
            _check_budget(deadline, interrupted)
            try:
                process.wait(timeout=min(.05, max(.001, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                continue
        _check_budget(deadline, interrupted)
        if process.returncode != 0:
            raise ValueError(ERROR)
        return bytes(result)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def _git_archive(commit: str, *, deadline: float | None = None, interrupted: Callable[[], bool] | None = None) -> bytes:
    return _git_bytes(['archive', '--format=tar', commit, '--', 'book-memo/app', 'book-memo/Dockerfile', 'book-memo/requirements.txt'], MAX_ARCHIVE, deadline=deadline, interrupted=interrupted)


def _owned_destination(destination: Path, *, existing: bool) -> Path:
    if not isinstance(destination, Path) or '..' in destination.parts:
        raise ValueError(ERROR)
    destination = destination.absolute()
    for ancestor in (destination.parent, *destination.parent.parents):
        if ancestor.is_symlink() or not ancestor.is_dir():
            raise ValueError(ERROR)
    marker = destination.parent / '.release-lab-owner'
    info = marker.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= 128:
        raise ValueError(ERROR)
    if existing:
        if destination.is_symlink() or not destination.is_dir():
            raise ValueError(ERROR)
    elif os.path.lexists(destination):
        raise ValueError(ERROR)
    return destination


def _valid_payload_path(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return bool(parts) and str(PurePosixPath(name)) == name and all(_NAME.fullmatch(part) for part in parts) and (name in {'Dockerfile', 'requirements.txt'} or (len(parts) >= 2 and parts[0] == 'app'))


def _unpack(archive_bytes: bytes, *, deadline: float | None = None, interrupted: Callable[[], bool] | None = None) -> dict[str, bytes]:
    """Validate every member before the caller creates any output directory."""
    _check_budget(deadline, interrupted)
    if not isinstance(archive_bytes, bytes) or len(archive_bytes) > MAX_ARCHIVE:
        raise ValueError(ERROR)
    payload = {}
    seen = set()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode='r:') as archive:
        for member in archive:
            _check_budget(deadline, interrupted)
            name = member.name.rstrip('/') if member.isdir() else member.name
            parts = PurePosixPath(name).parts
            if not parts or parts[0] != 'book-memo' or name != str(PurePosixPath(name)) or any(not _NAME.fullmatch(part) for part in parts) or name in seen:
                raise ValueError(ERROR)
            seen.add(name)
            if len(seen) > MAX_FILES * 2:
                raise ValueError(ERROR)
            relative = '/'.join(parts[1:])
            if member.isdir():
                if relative and relative != 'app' and not relative.startswith('app/'):
                    raise ValueError(ERROR)
                continue
            if not member.isfile() or not _valid_payload_path(relative) or member.size < 0:
                raise ValueError(ERROR)
            total += member.size
            if total > MAX_PAYLOAD or len(payload) >= MAX_FILES:
                raise ValueError(ERROR)
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValueError(ERROR)
            content = extracted.read(member.size + 1)
            if len(content) != member.size:
                raise ValueError(ERROR)
            payload[relative] = content
    if not {'Dockerfile', 'requirements.txt', 'app/main.py'} <= payload.keys():
        raise ValueError(ERROR)
    # A regular member may not also be another member's parent directory.
    if any(str(parent) in payload for name in payload for parent in PurePosixPath(name).parents if str(parent) != '.'):
        raise ValueError(ERROR)
    return payload


def build_release(destination: Path, commit: str, release_id: str, readiness_path: str, *, deadline: float | None = None, interrupted: Callable[[], bool] | None = None) -> dict:
    """Package the current committed Book source without consulting dirty files."""
    created = False
    try:
        _check_budget(deadline, interrupted)
        destination = _owned_destination(destination, existing=False)
        if not isinstance(commit, str) or not _SHA.fullmatch(commit) or release_id not in ('baseline', 'candidate', 'unready') or readiness_path not in ('/ready', '/__lab_missing_readiness'):
            raise ValueError(ERROR)
        if _git_bytes(['rev-parse', 'HEAD'], 41, deadline=deadline, interrupted=interrupted).decode('ascii').strip() != commit:
            raise ValueError(ERROR)
        payload = _unpack(_git_archive(commit, deadline=deadline, interrupted=interrupted), deadline=deadline, interrupted=interrupted)
        files = {name: _digest(content) for name, content in sorted(payload.items())}
        descriptor = {'schema_version': 1, 'service': 'book-memo', 'source_commit': commit, 'release_id': release_id, 'readiness_path': readiness_path, 'files': files, 'payload_digest': _digest(_canonical(files))}
        descriptor['release_digest'] = _digest(_canonical(descriptor))
        _check_budget(deadline, interrupted)
        _owned_destination(destination, existing=False)
        destination.mkdir(mode=0o700)
        created = True
        for name, content in payload.items():
            _check_budget(deadline, interrupted)
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as output:
                output.write(content)
        _check_budget(deadline, interrupted)
        with (destination / 'release.json').open('xb') as output:
            output.write(_canonical(descriptor))
        return verify_release(destination, descriptor, deadline=deadline, interrupted=interrupted)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, tarfile.TarError, subprocess.SubprocessError):
        if created:
            shutil.rmtree(destination)
        raise ValueError(ERROR) from None


def verify_release(destination: Path, expected: dict, *, deadline: float | None = None, interrupted: Callable[[], bool] | None = None) -> dict:
    """Verify exact inventory, hashes and immutable descriptor before execution."""
    try:
        _check_budget(deadline, interrupted)
        destination = _owned_destination(destination, existing=True)
        contents = {}
        directories = set()
        total = 0
        for base, dirs, files in os.walk(destination, followlinks=False):
            _check_budget(deadline, interrupted)
            for name in dirs:
                _check_budget(deadline, interrupted)
                path = Path(base) / name
                if not stat.S_ISDIR(path.lstat().st_mode):
                    raise ValueError(ERROR)
                directories.add(path.relative_to(destination).as_posix())
            for name in files:
                _check_budget(deadline, interrupted)
                path = Path(base) / name
                relative = path.relative_to(destination).as_posix()
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError(ERROR)
                total += info.st_size
                if total > MAX_ARCHIVE or len(contents) > MAX_FILES or info.st_size > MAX_PAYLOAD:
                    raise ValueError(ERROR)
                if relative != 'release.json' and not _valid_payload_path(relative):
                    raise ValueError(ERROR)
                contents[relative] = path.read_bytes()
        manifest_bytes = contents.pop('release.json')
        descriptor = json.loads(manifest_bytes)
        if descriptor != expected or manifest_bytes != _canonical(descriptor):
            raise ValueError(ERROR)
        if set(descriptor) != {'schema_version', 'service', 'source_commit', 'release_id', 'readiness_path', 'files', 'payload_digest', 'release_digest'}:
            raise ValueError(ERROR)
        if type(descriptor['schema_version']) is not int or descriptor['schema_version'] != 1 or descriptor['service'] != 'book-memo' or not isinstance(descriptor['source_commit'], str) or not _SHA.fullmatch(descriptor['source_commit']) or descriptor['release_id'] not in ('baseline', 'candidate', 'unready') or descriptor['readiness_path'] not in ('/ready', '/__lab_missing_readiness'):
            raise ValueError(ERROR)
        required_dirs = {str(parent) for name in contents for parent in PurePosixPath(name).parents if str(parent) != '.'}
        hashes = {name: _digest(content) for name, content in sorted(contents.items())}
        if directories != required_dirs or not {'Dockerfile', 'requirements.txt', 'app/main.py'} <= contents.keys() or len(contents) > MAX_FILES or sum(map(len, contents.values())) > MAX_PAYLOAD or hashes != descriptor['files'] or _digest(_canonical(hashes)) != descriptor['payload_digest']:
            raise ValueError(ERROR)
        unsigned = {key: value for key, value in descriptor.items() if key != 'release_digest'}
        if _digest(_canonical(unsigned)) != descriptor['release_digest']:
            raise ValueError(ERROR)
        _check_budget(deadline, interrupted)
        return descriptor
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError(ERROR) from None
