#!/usr/bin/env python3
"""Response-only drift drill for the existing, owned localhost Ansible lab.

The default check never deploys. --go requires a converged baseline, modifies
only index.html, then uses the existing site playbook to restore and verify it.
Raw Ansible output is consumed in memory and never printed or saved.
"""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess

LAB = Path(__file__).resolve().parents[1]
PROJECT = "personal-server-ansible-lab"
ORIGINAL = b"ansible lab sample ready\n"
DRIFT = b"ansible lab sample drift\n"


class UnsafeLab(Exception):
    """A fixed boundary or expected verification failed."""


class UnterminatedProcess(Exception):
    """An Ansible process group was not confirmed stopped; do not recover."""


def _regular_fd(root_fd, name, writable=False):
    fd = os.open(name, (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW,
                 dir_fd=root_fd)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_size > 65536
            or info.st_mode & 0o022):
        os.close(fd)
        raise UnsafeLab()
    return fd


def _read(root_fd, name):
    fd = _regular_fd(root_fd, name)
    try:
        return os.read(fd, 65537)
    finally:
        os.close(fd)


def _owned_root(home):
    # Check each directory component, so an ancestor link cannot redirect a
    # response write. The fixed path has no user-supplied CLI override.
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for piece in (None, ".local", "share", PROJECT):
            if piece:
                child = os.open(piece, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise UnsafeLab()
        return fd
    except BaseException:
        os.close(fd)
        raise


def _validate_files(root_fd, allowed_response):
    if set(os.listdir(root_fd)) != {".owner", "compose.yaml", "index.html"}:
        raise UnsafeLab()
    if _read(root_fd, ".owner") != (PROJECT + "\n").encode():
        raise UnsafeLab()
    _read(root_fd, "compose.yaml")
    if _read(root_fd, "index.html") not in allowed_response:
        raise UnsafeLab()


def _replace_response(root_fd, allowed, replacement):
    _validate_files(root_fd, allowed)
    fd = _regular_fd(root_fd, "index.html", writable=True)
    try:
        previous = os.read(fd, 65537)
        if previous not in allowed:
            raise UnsafeLab()
        try:
            _write_fd(fd, replacement)
        except (Exception, KeyboardInterrupt):
            # The open, validated descriptor still identifies our original
            # file even if its pathname changes during a partial write.
            _write_fd(fd, previous)
            raise
    finally:
        os.close(fd)


def _write_fd(fd, content):
    os.lseek(fd, 0, os.SEEK_SET)
    remaining = content
    while remaining:
        count = os.write(fd, remaining)
        if count <= 0:
            raise UnsafeLab()
        remaining = remaining[count:]
    os.ftruncate(fd, len(content))
    os.fsync(fd)


def _runner(args):
    env = os.environ.copy()
    env.pop("ANSIBLE_LOG_PATH", None)
    env.update(ANSIBLE_CONFIG=str(LAB / "ansible.cfg"),
               ANSIBLE_STDOUT_CALLBACK="default", ANSIBLE_NOCOLOR="1",
               LC_ALL="C")
    return _run_process(args, LAB, env)


def _run_process(args, cwd, env, *, timeout=180):
    process = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    except (Exception, KeyboardInterrupt):
        # Ansible can spawn modules. Terminate the entire session before any
        # recovery touches the response; killing only the CLI allows late writes.
        try:
            for signum in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, signum)
                except ProcessLookupError:
                    pass
            process.communicate(timeout=5)
        except (Exception, KeyboardInterrupt):
            raise UnterminatedProcess() from None
        raise


def _play(runner, *options):
    result = runner(["ansible-playbook", "-i", str(LAB / "inventory/localhost.ini"),
                     str(LAB / "playbooks/site.yml"), *options])
    if result.returncode:
        raise UnsafeLab()
    recaps = re.findall(r"^localhost\s+:\s+ok=\d+\s+changed=(\d+)\s+unreachable=(\d+)"
                        r"\s+failed=(\d+)\s+skipped=\d+\s+rescued=(\d+)\s+ignored=(\d+)\s*$",
                        result.stdout, re.MULTILINE)
    if len(recaps) != 1 or any(int(value) for value in recaps[0][1:]):
        raise UnsafeLab()
    return int(recaps[0][0]), result.stdout


def run_drill(home=None, runner=None, *, go=False):
    """Return only fixed status fields; dependencies are injectable for tests."""
    home = Path.home() if home is None else Path(home)
    runner = _runner if runner is None else runner
    status = {"ownership": "FAIL", "baseline": "NOT_RUN", "execution": "FAIL"}
    root_fd = None
    injected = False
    def play(*options):
        current_fd = _owned_root(home)
        try:
            original_info, current_info = os.fstat(root_fd), os.fstat(current_fd)
            if (original_info.st_dev, original_info.st_ino) != (current_info.st_dev, current_info.st_ino):
                raise UnsafeLab()
        finally:
            os.close(current_fd)
        return _play(runner, *options)
    try:
        root_fd = _owned_root(home)
        _validate_files(root_fd, {ORIGINAL})
        play("--check", "--tags", "preflight")
        _validate_files(root_fd, {ORIGINAL})
        status["ownership"] = "PASS"
        changed, _ = play("--check", "--diff")
        if changed != 0:
            raise UnsafeLab()
        status["baseline"] = "PASS"
        if not go:
            status["execution"] = "NOT_RUN"
            return status
        # Mark before the write, ensuring partial write errors reach cleanup.
        injected = True
        _replace_response(root_fd, {ORIGINAL}, DRIFT)
        changed, output = play("--check", "--diff")
        if changed != 1 or not re.search(
                r"TASK \[Render sample response\].*?-ansible lab sample drift"
                r".*?\+ansible lab sample ready.*?changed: \[localhost\]",
                output, re.DOTALL):
            raise UnsafeLab()
        status["drift_detection"] = "PASS"
        _validate_files(root_fd, {DRIFT})
        play()
        _validate_files(root_fd, {ORIGINAL})
        status["restoration"] = "PASS"
        changed, _ = play()
        if changed != 0:
            raise UnsafeLab()
        _validate_files(root_fd, {ORIGINAL})
        status["idempotence"] = "PASS"
        status["execution"] = "PASS"
        injected = False
    except UnterminatedProcess:
        status["process_stop"] = "UNCONFIRMED"
        if injected:
            status["recovery"] = "FAIL"
    except (Exception, KeyboardInterrupt):
        if injected and root_fd is not None:
            status["recovery"] = "FAIL"
            try:
                _validate_files(root_fd, {ORIGINAL, DRIFT})
                play("--check", "--tags", "preflight")
                _replace_response(root_fd, {ORIGINAL, DRIFT}, ORIGINAL)
                play()
                _validate_files(root_fd, {ORIGINAL})
                status["recovery"] = "PASS"
            except (Exception, KeyboardInterrupt):
                pass
    finally:
        if root_fd is not None:
            os.close(root_fd)
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="read-only ownership and baseline check (default)")
    modes.add_argument("--go", action="store_true", help="explicit response drift, restore, and idempotence drill")
    args = parser.parse_args()
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    result = run_drill(go=args.go)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["execution"] in {"PASS", "NOT_RUN"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
