#!/usr/bin/env python3
"""Bounded local Windows enumeration diagnostics. No remote or authority path.

The fixed PS driver extracts three exact read pipelines from pinned helper bytes.
It never executes the complete guard, and counts are not a writer inventory.
"""
import argparse
import ctypes
import hashlib
import json
import math
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'ha_control/windows-legacy-guard.ps1'
DRIVER = Path(__file__).with_name('n100-ha-windows-diagnostic.ps1')
LIMIT = 16384
SOURCE_LIMIT = 131072
TIMEOUT = 8.0
PHASES = ('services', 'tasks', 'processes')


class DiagnosticError(RuntimeError):
    pass


def system_directory():
    # Do not select an executable from caller-controlled SystemRoot or PATH.
    buffer = ctypes.create_unicode_buffer(32768)
    size = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < size < len(buffer):
        raise DiagnosticError('query_unavailable')
    return Path(buffer.value)


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise DiagnosticError('response_rejected')
        result[key] = value
    return result


def capture(argv, payload, timeout=TIMEOUT, environment=None):
    """Cap output and time, including input delivery. Kill only our own child."""
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=environment)
    answers = queue.Queue()
    def read():
        try:
            answers.put(process.stdout.read(LIMIT + 1))
        except (OSError, ValueError):
            answers.put(None)
    def write():
        try:
            process.stdin.write(payload)
            process.stdin.close()
        except (OSError, ValueError):
            pass
    reader = threading.Thread(target=read, daemon=True)
    writer = threading.Thread(target=write, daemon=True)
    reader.start(); writer.start()
    try:
        output = answers.get(timeout=max(0.001, deadline - time.monotonic()))
        if output is None or len(output) > LIMIT:
            raise DiagnosticError('response_rejected')
        code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        if code != 0:
            raise DiagnosticError('query_unavailable')
        return output
    except (queue.Empty, subprocess.TimeoutExpired):
        raise DiagnosticError('diagnostic_timeout') from None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        reader.join(timeout=0.2); writer.join(timeout=0.2)
        if reader.is_alive() or writer.is_alive():
            raise DiagnosticError('cleanup_incomplete')
        process.stdin.close(); process.stdout.close()


def validate(raw, source_sha256):
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        keys = {'schema_version', 'kind', 'authority', 'ready', 'scope', 'source_sha256', 'status', 'phases'}
        if (type(value) is not dict or set(value) != keys or type(value['schema_version']) is not int
                or value['schema_version'] != 1 or value['kind'] != 'ha-enumeration-diagnostic'
                or value['authority'] is not False or value['ready'] is not False
                or value['scope'] != 'windows-enumeration-only' or value['source_sha256'] != source_sha256
                or value['status'] not in {'measured', 'partial'} or type(value['phases']) is not list
                or len(value['phases']) != 3):
            raise ValueError()
        for name, row in zip(PHASES, value['phases']):
            if type(row) is not dict or set(row) != {'phase', 'status', 'reason', 'count', 'elapsed_ms'}:
                raise ValueError()
            if (row['phase'] != name or type(row['elapsed_ms']) not in (int, float)
                    or not math.isfinite(row['elapsed_ms']) or row['elapsed_ms'] < 0):
                raise ValueError()
            if row['status'] == 'measured':
                if row['reason'] != 'none' or type(row['count']) is not int or not 0 <= row['count'] <= 1000000:
                    raise ValueError()
            elif row['status'] != 'blocked' or row['reason'] != 'query_failed' or row['count'] is not None:
                raise ValueError()
        partial = any(row['status'] == 'blocked' for row in value['phases'])
        if value['status'] != ('partial' if partial else 'measured'):
            raise ValueError()
        return value
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise DiagnosticError('response_rejected') from None


def collect(expected_source_sha256, expected_driver_sha256):
    started = time.monotonic()
    if sys.platform != 'win32':
        raise DiagnosticError('native_windows_required')
    for pin in (expected_source_sha256, expected_driver_sha256):
        if type(pin) is not str or not re.fullmatch('[0-9a-f]{64}', pin):
            raise DiagnosticError('source_rejected')
    with SOURCE.open('rb') as stream:
        source = stream.read(SOURCE_LIMIT + 1)
    with DRIVER.open('rb') as stream:
        driver = stream.read(SOURCE_LIMIT + 1)
    if (len(source) > SOURCE_LIMIT or len(driver) > SOURCE_LIMIT
            or hashlib.sha256(source).hexdigest() != expected_source_sha256
            or hashlib.sha256(driver).hexdigest() != expected_driver_sha256):
        raise DiagnosticError('source_rejected')
    # Execute the verified bytes in memory, not a path that could be replaced
    # after hashing. Fixed driver/source only; no profiles, plan, approval or Go.
    import base64
    encoded = base64.b64encode(driver.decode('utf-8').encode('utf-16le')).decode('ascii')
    directory = system_directory()
    executable = directory / 'WindowsPowerShell/v1.0/powershell.exe'
    system = directory.parent
    environment = {'SystemRoot': str(system), 'WINDIR': str(system),
                   'PSModulePath': str(executable.parent / 'Modules'),
                   'PATH': str(directory) + ';' + str(executable.parent)}
    payload = json.dumps({'source': source.decode('utf-8'), 'source_sha256': expected_source_sha256}).encode('utf-8')
    remaining = TIMEOUT - (time.monotonic() - started)
    if remaining <= 0:
        raise DiagnosticError('diagnostic_timeout')
    raw = capture([str(executable), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                  payload, timeout=remaining, environment=environment)
    value = validate(raw.decode('utf-8-sig'), expected_source_sha256)
    elapsed = (time.monotonic() - started) * 1000
    if elapsed > TIMEOUT * 1000 or sum(row['elapsed_ms'] for row in value['phases']) > elapsed:
        raise DiagnosticError('diagnostic_timeout')
    value['driver_sha256'] = expected_driver_sha256
    value['elapsed_ms'] = elapsed
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collect-local', action='store_true', required=True)
    parser.add_argument('--source-sha256', required=True)
    parser.add_argument('--driver-sha256', required=True)
    args = parser.parse_args(argv)
    try:
        value = collect(args.source_sha256, args.driver_sha256)
    except (DiagnosticError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        reason = str(error) if isinstance(error, DiagnosticError) else 'query_unavailable'
        value = {'schema_version': 1, 'kind': 'ha-enumeration-diagnostic', 'authority': False,
                 'ready': False, 'scope': 'windows-enumeration-only', 'status': 'blocked', 'reason': reason}
    print(json.dumps(value, sort_keys=True, allow_nan=False))
    return 0 if value['status'] == 'measured' else 1


if __name__ == '__main__':
    raise SystemExit(main())
