#!/usr/bin/env python3
"""Read the fixed runtime marker with fd-based, fail-closed validation."""

import os
import stat
import sys


SERVICES = ("crawler-worker", "youtube-memo", "book-memo")
DEFAULT = "".join(f"{service}=compose\n" for service in SERVICES)


def fail(message: str) -> int:
    print(f"runtime-state: {message}", file=sys.stderr)
    return 1


def trusted_directory(path: str) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        return False
    return os.access(path, os.R_OK | os.X_OK)


def trusted_directory_hierarchy(state_path: str) -> bool:
    parent = os.path.dirname(state_path)
    return trusted_directory(os.path.dirname(parent)) and trusted_directory(parent)


def read_state(path: str, require_explicit: bool = False) -> int:
    parent = os.path.dirname(path)
    try:
        state_info = os.lstat(path)
    except FileNotFoundError:
        if require_explicit:
            return fail("explicit state file is required")
        if os.path.lexists(parent):
            if not trusted_directory(parent):
                return fail("state parent is not trusted")
        elif not trusted_directory(os.path.dirname(parent)):
            return fail("state anchor is not trusted")
        print(DEFAULT, end="")
        return 0
    except OSError:
        return fail("state file cannot be inspected")

    if stat.S_ISLNK(state_info.st_mode):
        return fail("state file symlink is not allowed")
    if not stat.S_ISREG(state_info.st_mode):
        return fail("state file is not a regular file")
    if state_info.st_mode & 0o022:
        return fail("state file is group/other writable")
    if state_info.st_uid != 0:
        return fail("state file is not root-owned regular file")
    if not trusted_directory_hierarchy(path):
        return fail("state parent is not trusted")

    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return fail("state file cannot be opened safely")
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_mode) != (
            state_info.st_dev,
            state_info.st_ino,
            state_info.st_uid,
            state_info.st_mode,
        ):
            return fail("state file changed during open")
        raw = os.read(fd, 1024 * 1024 + 1)
    finally:
        os.close(fd)
    if len(raw) > 1024 * 1024 or b"\0" in raw:
        return fail("state file contains invalid bytes")
    try:
        rows = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return fail("state file is not valid UTF-8")
    values = {service: "compose" for service in SERVICES}
    seen_services: set[str] = set()
    for row in rows:
        if "=" not in row:
            return fail("malformed state row")
        service, value = row.split("=", 1)
        if service not in values or value not in {"compose", "k3s"}:
            return fail("unknown or invalid state row")
        if service in seen_services:
            return fail("duplicate state service")
        seen_services.add(service)
        values[service] = value
    if require_explicit and seen_services != set(SERVICES):
        return fail("explicit state required for every service")
    print("".join(f"{service}={values[service]}\n" for service in SERVICES), end="")
    return 0


if __name__ == "__main__":
    if len(sys.argv) not in {2, 3} or not sys.argv[1]:
        raise SystemExit(fail("state path argument required"))
    if len(sys.argv) == 3 and sys.argv[2] != "--require-explicit":
        raise SystemExit(fail("invalid option"))
    raise SystemExit(read_state(sys.argv[1], require_explicit=len(sys.argv) == 3))
