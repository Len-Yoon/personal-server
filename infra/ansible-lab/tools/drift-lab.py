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
APPROVED_RESPONSES = {b"ansible lab sample ready\n", b"ansible lab sample ready\r\n"}


class UnsafeLab(Exception):
    """A fixed boundary or expected verification failed."""


class UnterminatedProcess(Exception):
    """An Ansible process group was not confirmed stopped; do not recover."""


class UnavailableLocale(Exception):
    """No installed English UTF-8 locale is available for Ansible."""


def _expected_response():
    # The sample is intentionally a static template. Preserve checkout bytes,
    # while refusing arbitrary template content as a new ownership baseline.
    fd = os.open(LAB / "templates/index.html.j2", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 26:
            raise UnsafeLab()
        response = os.read(fd, 27)
        if response not in APPROVED_RESPONSES:
            raise UnsafeLab()
        return response
    finally:
        os.close(fd)


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
    # Ignore inherited translation/locale overrides. The locale inventory
    # probe is ASCII; only the selected installed UTF-8 locale runs Ansible.
    for name in tuple(env):
        if name.startswith("LC_") or name in {"LANG", "LANGUAGE"}:
            env.pop(name)
    probe_env = {**env, "LC_ALL": "C", "LANG": "C"}
    try:
        available = _run_process(["locale", "-a"], LAB, probe_env, timeout=5)
    except UnterminatedProcess:
        raise
    except Exception:
        raise UnavailableLocale() from None
    if available.returncode:
        raise UnavailableLocale()
    installed = set(available.stdout.splitlines())
    selected = next((name for name in ("C.UTF-8", "C.utf8", "en_US.UTF-8", "en_US.utf8")
                     if name in installed), None)
    if selected is None:
        raise UnavailableLocale()
    env.update(ANSIBLE_CONFIG=str(LAB / "ansible.cfg"),
               ANSIBLE_STDOUT_CALLBACK="default", ANSIBLE_NOCOLOR="1",
               LC_ALL=selected, LANG=selected)
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
        if _expected_response() != original:
            raise UnsafeLab()
        current_fd = _owned_root(home)
        try:
            original_info, current_info = os.fstat(root_fd), os.fstat(current_fd)
            if (original_info.st_dev, original_info.st_ino) != (current_info.st_dev, current_info.st_ino):
                raise UnsafeLab()
        finally:
            os.close(current_fd)
        return _play(runner, *options)
    try:
        original = _expected_response()
        drift = original.replace(b"ready", b"drift")
        root_fd = _owned_root(home)
        _validate_files(root_fd, {original})
        play("--check", "--tags", "preflight")
        _validate_files(root_fd, {original})
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
        _replace_response(root_fd, {original}, drift)
        changed, output = play("--check", "--diff")
        if changed != 1 or not re.search(
                r"TASK \[Render sample response\].*?-ansible lab sample drift"
                r".*?\+ansible lab sample ready.*?changed: \[localhost\]",
                output, re.DOTALL):
            raise UnsafeLab()
        status["drift_detection"] = "PASS"
        _validate_files(root_fd, {drift})
        play()
        _validate_files(root_fd, {original})
        status["restoration"] = "PASS"
        changed, _ = play()
        if changed != 0:
            raise UnsafeLab()
        _validate_files(root_fd, {original})
        status["idempotence"] = "PASS"
        status["execution"] = "PASS"
        injected = False
    except UnterminatedProcess:
        status["process_stop"] = "UNCONFIRMED"
        if injected:
            status["recovery"] = "FAIL"
    except UnavailableLocale:
        status["locale"] = "UNAVAILABLE"
        if injected:
            status["recovery"] = "FAIL"
    except (Exception, KeyboardInterrupt):
        if injected and root_fd is not None:
            status["recovery"] = "FAIL"
            try:
                _validate_files(root_fd, {original, drift})
                play("--check", "--tags", "preflight")
                _replace_response(root_fd, {original, drift}, original)
                play()
                _validate_files(root_fd, {original})
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
