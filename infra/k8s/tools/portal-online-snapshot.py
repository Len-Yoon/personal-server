#!/usr/bin/env python3
"""Read-only Portal capture; consistency is per store, never a global epoch.

FileStore must publish immutable completed files under its root-directory flock.
Portfolio/JSON writers must replace files atomically; logs must only append.
Use read-only source mounts (SQLite may otherwise maintain WAL shared memory).
"""
import argparse
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import sqlite3
import stat
import time


MAX_ENTRIES = 10000


class CaptureError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def open_read(path, *, dir_fd=None):
    return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)


def open_root(path):
    """Reject symlinks in every source-root component, including ancestors."""
    absolute = Path(os.path.abspath(path))
    fd = open_read("/")
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def state_kind(path, directory=False):
    name = path.as_posix()
    databases = {"homeops.sqlite3", "news-bookmarks.sqlite3", "auth/session-state.sqlite3"}
    if directory:
        return "directory" if name in {"auth", "security-events.txt"} else None
    if name in databases:
        return "database"
    if any(name == db + suffix for db in databases for suffix in ("-wal", "-shm", "-journal")):
        return "skip"
    if name in {"auth-rate-limit-state.json", ".auth-rate-limit-state.json.lock"}:
        return "atomic" if name.endswith(".json") else "skip"
    if re.fullmatch(r"\.auth-rate-limit-state\.json\.[a-f0-9]+\.tmp", name):
        return "skip"
    if name == "security-events.txt" or re.fullmatch(r"(?:security-events\.txt/)?security-events-\d{4}-\d{2}-\d{2}\.txt", name):
        return "append"
    return None


def capture(files_root, state_root, output, lock_budget_ms=250, *, min_free_bytes=256 * 1024**2,
            max_output_bytes=2 * 1024**3, after_pin=None):
    """Capture into an empty output; optional hook is only for local race tests."""
    if not 1 <= lock_budget_ms <= 250:
        raise CaptureError("lock budget must be between 1 and 250 ms")
    output = Path(output)
    if not output.is_dir() or output.is_symlink() or any(output.iterdir()):
        raise CaptureError("output must be an existing empty directory")
    roots = [Path(files_root), Path(state_root)]
    if min_free_bytes < 0 or max_output_bytes < 1:
        raise CaptureError("invalid capacity bounds")
    def space_guard(required=0):
        for path in (*roots, output):
            if shutil.disk_usage(path).free < min_free_bytes + required:
                raise CaptureError("insufficient backup capacity")
    resolved_output = output.resolve()
    if any(root.resolve() == resolved_output or root.resolve() in resolved_output.parents or resolved_output in root.resolve().parents for root in roots):
        raise CaptureError("source and output must be disjoint")
    # Reserve descriptors for traversal, SQLite and output, respecting real usage.
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        used = len(os.listdir("/proc/self/fd"))
    except FileNotFoundError:
        used = sum(1 for fd in range(min(int(soft), 65536)) if _fd_open(fd))
    limit = min(10000, max(0, int(soft) - used - 64)) if soft != resource.RLIM_INFINITY else 10000
    records, directories = [], []
    visited = 0
    created = []
    manifest = {"consistency": "per-store", "started_at": utc_now(), "stores": {}}
    try:
        with ExitStack() as stack:
            def pin(fd, root_dev, prefix, relative, kind, deadline=None):
                nonlocal visited
                if len(relative.parts) > 64:
                    raise CaptureError("source namespace limit exceeded")
                if deadline is not None and time.monotonic() >= deadline:
                    raise CaptureError("file namespace lock budget exceeded")
                # Never materialize/sort a directory while holding the writer lock.
                # Check before next(): even skipped temporary entries consume budget.
                with os.scandir(fd) as entries:
                    while True:
                        if deadline is not None and time.monotonic() >= deadline:
                            raise CaptureError("file namespace lock budget exceeded")
                        if visited >= MAX_ENTRIES:
                            raise CaptureError("source namespace limit exceeded")
                        try:
                            entry = next(entries)
                        except StopIteration:
                            break
                        visited += 1
                        name = entry.name
                        rel = relative / name
                        if kind == "state" and state_kind(rel) == "skip":
                            continue
                        if kind == "files" and rel.parent == Path(".portfolio") and name.startswith(".portfolio.md."):
                            continue
                        child = open_read(name, dir_fd=fd)
                        try:
                            info = os.fstat(child)
                            if info.st_dev != root_dev:
                                raise CaptureError("cross-device source is unsupported")
                            is_directory = stat.S_ISDIR(info.st_mode)
                            if not is_directory and not stat.S_ISREG(info.st_mode):
                                raise CaptureError("non-regular source is unsupported")
                            policy = state_kind(rel, is_directory) if kind == "state" else "immutable"
                            if policy is None:
                                raise CaptureError("unknown mutable state entry")
                            if policy == "skip":
                                continue
                            if is_directory:
                                directories.append(Path(prefix) / rel)
                                pin(child, root_dev, prefix, rel, kind, deadline)
                            else:
                                if len(records) >= limit:
                                    raise CaptureError("source descriptor budget exceeded")
                                records.append((child, info, Path(prefix) / rel, policy, roots[1] / rel))
                                stack.callback(os.close, child)
                                child = None
                        finally:
                            if child is not None:
                                os.close(child)
            for root, prefix, kind in zip(roots, ("files", "portal-web-state"), ("files", "state")):
                fd = open_root(root)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISDIR(info.st_mode):
                        raise CaptureError("source root must be a directory")
                    started = utc_now()
                    if kind == "files":
                        deadline = time.monotonic() + lock_budget_ms / 1000
                        while True:
                            try:
                                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                break
                            except BlockingIOError:
                                if time.monotonic() >= deadline:
                                    raise CaptureError("file namespace lock budget exceeded")
                                time.sleep(min(.001, max(0, deadline - time.monotonic())))
                        try:
                            pin(fd, info.st_dev, prefix, Path(), kind, deadline)
                            if time.monotonic() >= deadline:
                                raise CaptureError("file namespace lock budget exceeded")
                        finally:
                            fcntl.flock(fd, fcntl.LOCK_UN)
                    else:
                        pin(fd, info.st_dev, prefix, Path(), kind)
                    manifest["stores"][prefix] = {"pin_started_at": started, "pin_finished_at": utc_now()}
                finally:
                    os.close(fd)
            if after_pin is not None:
                after_pin()
            declared = sum(info.st_size for _, info, _, _, _ in records)
            if declared > max_output_bytes:
                raise CaptureError("snapshot output limit exceeded")
            space_guard(declared * 6)
            for prefix in ("files", "portal-web-state"):
                target = output / prefix
                target.mkdir(mode=0o700)
                created.append(target)
            for relative in directories:
                (output / relative).mkdir(mode=0o700, parents=True, exist_ok=True)
            written = 0
            for fd, before, relative, policy, source in records:
                destination = output / relative
                remaining_budget = max_output_bytes - written
                if policy == "database":
                    _backup_database(source, fd, before, destination, space_guard, remaining_budget)
                    manifest["stores"][relative.as_posix()] = {"backup_finished_at": utc_now()}
                else:
                    if before.st_size > remaining_budget:
                        raise CaptureError("snapshot output limit exceeded")
                    _copy_prefix(fd, before, destination, append=policy == "append", guard=space_guard)
                written += destination.stat().st_size
                if written > max_output_bytes:
                    raise CaptureError("snapshot output limit exceeded")
            manifest["finished_at"] = utc_now()
            target = output / "snapshot-manifest.json"
            with target.open("x", encoding="utf-8") as stream:
                created.append(target)
                json.dump(manifest, stream, sort_keys=True)
                stream.write("\n")
        return manifest
    except BaseException:
        for target in reversed(created):
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        raise


def _fd_open(fd):
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


def _copy_prefix(fd, before, destination, *, append, guard):
    with destination.open("xb") as target:
        remaining = before.st_size
        while remaining:
            guard()
            data = os.read(fd, min(1024 * 1024, remaining))
            if not data:
                raise CaptureError("source shrank during capture")
            target.write(data)
            remaining -= len(data)
    after = os.fstat(fd)
    if not append and (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise CaptureError("immutable source changed during capture")
    if append:
        if after.st_size < before.st_size:
            raise CaptureError("append source shrank during capture")
        if after.st_size == before.st_size and after.st_mtime_ns != before.st_mtime_ns:
            raise CaptureError("append source was rewritten during capture")
        # A size sample may land inside another writer's final record.
        with destination.open("r+b") as target:
            position = before.st_size
            while position:
                start = max(0, position - 65536)
                target.seek(start)
                data = target.read(position - start)
                newline = data.rfind(b"\n")
                if newline >= 0:
                    target.truncate(start + newline + 1)
                    break
                position = start
            else:
                target.truncate(0)


def _backup_database(source, fd, before, destination, guard, max_output_bytes):
    # WAL companions must be resolved next to the live DB, not a pinned /proc FD.
    current = source.stat(follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
        raise CaptureError("database namespace changed")
    deadline = time.monotonic() + 5
    page_size = None
    def progress(_status, _remaining, _total):
        guard()
        # Source main-file size omits committed WAL pages. Check SQLite's full
        # page count each batch; at most 16 * page_size bytes are copied before
        # detecting newly observed growth (SQLite page size is at most 64 KiB).
        if _total * page_size > max_output_bytes:
            raise CaptureError("database output budget exceeded")
        if destination.exists() and destination.stat().st_size > max_output_bytes:
            raise CaptureError("snapshot output limit exceeded")
        if time.monotonic() >= deadline:
            raise CaptureError("database backup deadline exceeded")
    with closing(sqlite3.connect(source.absolute().as_uri() + "?mode=ro", uri=True, timeout=.1)) as original:
        page_size = original.execute("PRAGMA page_size").fetchone()[0]
        with closing(sqlite3.connect(destination)) as result:
            original.backup(result, pages=16, progress=progress, sleep=.01)
            if result.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise CaptureError("database integrity check failed")
    current = source.stat(follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
        raise CaptureError("database namespace changed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["capture"])
    parser.add_argument("--files-root", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--lock-budget-ms", type=int, default=250)
    parser.add_argument("--min-free-bytes", type=int, default=256 * 1024**2)
    parser.add_argument("--max-output-bytes", type=int, default=2 * 1024**3)
    args = parser.parse_args()
    def cancel(signum, _frame):
        raise SystemExit(128 + signum)
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, cancel)
    try:
        capture(args.files_root, args.state_root, args.output, args.lock_budget_ms,
                min_free_bytes=args.min_free_bytes, max_output_bytes=args.max_output_bytes)
    except (OSError, sqlite3.Error, CaptureError):
        # Never print source names, paths, database contents or credentials.
        parser.exit(1, "portal_online_snapshot=FAIL\n")
    print("portal_online_snapshot=PASS")


if __name__ == "__main__":
    main()
