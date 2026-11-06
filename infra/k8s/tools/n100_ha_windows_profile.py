#!/usr/bin/env python3
"""Bounded Windows read-component diagnostics, never full observer authority."""
import argparse
import base64
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import queue
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'ha_control/windows-legacy-guard.ps1'
DRIVER = Path(__file__).with_name('n100-ha-windows-profile.ps1')
APPROVED_SOURCE = 'f07a7cf479aba1bbab238a8b3a7a7db4816145ecc71d1603348e85925d6d55b9'
PHASES = ('services_query', 'tasks_query', 'processes_query', 'services', 'tasks',
          'processes', 'startup', 'registry', 'hash', 'signature', 'task_xml')
spec = importlib.util.spec_from_file_location('_ha_profile_capture', Path(__file__).with_name('n100_ha_windows_diagnostic.py'))
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
DiagnosticError = native.DiagnosticError


def validate(raw, source_sha256, *, progress=False):
    try:
        value = json.loads(raw, object_pairs_hook=native.pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (type(value) is not dict or set(value) != {'schema_version', 'kind', 'authority',
                'ready', 'source_sha256', 'status', 'phases'} or type(value['schema_version']) is not int
                or value['schema_version'] != 1 or value['kind'] != 'ha-windows-component-diagnostic'
                or value['authority'] is not False or value['ready'] is not False
                or value['source_sha256'] != source_sha256 or value['status'] not in ({'progress'} if progress else {'measured', 'partial'})
                or type(value['phases']) is not list or len(value['phases']) != len(PHASES)):
            raise ValueError()
        for phase, row in zip(PHASES, value['phases']):
            if type(row) is not dict or set(row) != {'phase', 'calls', 'count', 'elapsed_ms', 'status', 'reason'}:
                raise ValueError()
            if (row['phase'] != phase or type(row['calls']) is not int or not 0 <= row['calls'] <= 1000000
                    or type(row['elapsed_ms']) not in (int, float) or not math.isfinite(row['elapsed_ms'])
                    or row['elapsed_ms'] < 0):
                raise ValueError()
            if row['status'] == 'blocked':
                if row['reason'] != 'query_failed' or row['count'] is not None or row['calls'] == 0:
                    raise ValueError()
            elif row['status'] in {'measured', 'not_run'}:
                if row['reason'] != 'none' or type(row['count']) is not int or not 0 <= row['count'] <= 1000000:
                    raise ValueError()
                if row['status'] == 'not_run' and (row['calls'] or row['count'] or row['elapsed_ms']):
                    raise ValueError()
                if row['status'] == 'measured' and row['calls'] == 0:
                    raise ValueError()
            else:
                raise ValueError()
        if not progress and value['status'] != ('partial' if any(r['status'] == 'blocked' for r in value['phases']) else 'measured'):
            raise ValueError()
        if progress:
            return value
        rows = {row['phase']: row for row in value['phases']}
        for phase in ('services', 'tasks', 'processes'):
            query, component = rows[phase + '_query'], rows[phase]
            if query['calls'] != 1 or query['status'] == 'not_run':
                raise ValueError()
            if query['status'] == 'blocked':
                if component['status'] != 'not_run':
                    raise ValueError()
            elif component['calls'] != 1 or component['status'] == 'not_run':
                raise ValueError()
        if rows['startup']['calls'] != 1 or rows['startup']['status'] == 'not_run':
            raise ValueError()
        return value
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise DiagnosticError('response_rejected') from None


def validate_progress(raw, source_sha256):
    """Validate one progress JSON line; return its fixed, nonauthority envelope.

    Remote harnesses may retain this only as blocked diagnostic metadata when
    their deadline expires. A final report still requires separate validate().
    """
    try:
        value = json.loads(raw, object_pairs_hook=native.pairs)
        if (type(value) is not dict or set(value) != {'event', 'active_phase', 'report'}
                or value['event'] != 'progress' or value['active_phase'] not in PHASES[:7]):
            raise ValueError()
        value['report'] = validate(json.dumps(value['report']), source_sha256, progress=True)
        return value
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise DiagnosticError('response_rejected') from None


class StreamError(DiagnosticError):
    def __init__(self, reason, last_progress=None):
        super().__init__(reason)
        self.last_progress = last_progress


def capture(argv, payload, source_sha256, timeout, environment=None):
    """Own one child; retain only validated bounded diagnostic progress."""
    if type(payload) is not bytes or len(payload) > 262144:
        raise StreamError('response_rejected')
    deadline = time.monotonic() + timeout
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env=environment)
    messages = queue.Queue(maxsize=17)
    def read():
        try:
            for _ in range(17):
                line = child.stdout.readline(native.LIMIT + 1)
                messages.put(line)
                if not line or len(line) > native.LIMIT:
                    break
        except (OSError, ValueError):
            messages.put(None)
        finally:
            child.stdout.close()
    def write():
        try:
            child.stdin.write(payload)
        except (OSError, ValueError):
            pass
        finally:
            child.stdin.close()
    reader = threading.Thread(target=read, daemon=True, name='ha-profile-reader')
    writer = threading.Thread(target=write, daemon=True, name='ha-profile-writer')
    reader.start(); writer.start()
    last = None
    final = None
    total = lines = 0
    failure = None
    try:
        while True:
            line = messages.get(timeout=max(0.001, deadline - time.monotonic()))
            if line is None:
                raise StreamError('response_rejected')
            if not line:
                break
            total += len(line); lines += 1
            if not line.endswith(b'\n') or len(line) > native.LIMIT or total > 65536 or lines > 16 or final is not None:
                raise StreamError('response_rejected')
            try:
                decoded = line.decode('utf-8-sig')
                candidate = json.loads(decoded, object_pairs_hook=native.pairs)
                if type(candidate) is dict and candidate.get('event') == 'progress':
                    last = validate_progress(decoded, source_sha256)
                else:
                    final = validate(decoded, source_sha256)
            except (ValueError, UnicodeError, DiagnosticError, RecursionError):
                raise StreamError('response_rejected') from None
        code = child.wait(timeout=max(0.001, deadline - time.monotonic()))
        if code != 0 or final is None:
            raise StreamError('query_unavailable', last)
        if time.monotonic() > deadline:
            raise StreamError('diagnostic_timeout', last)
    except (queue.Empty, subprocess.TimeoutExpired):
        failure = StreamError('diagnostic_timeout', last)
    except StreamError as error:
        failure = error
        if error.args == ('response_rejected',):
            last = None
    finally:
        if child.poll() is None:
            child.kill()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            failure = StreamError('cleanup_incomplete', last)
        reader.join(timeout=0.2); writer.join(timeout=0.2)
        if reader.is_alive() or writer.is_alive():
            failure = StreamError('cleanup_incomplete', last)
        # A descendant can inherit stdout even after our child exits. Closing
        # a buffered pipe while its worker owns the I/O lock can block forever.
        # Each worker closes its own stream; incomplete cleanup stays blocked.
    if failure is not None:
        raise failure
    return final


def collect(expected_source_sha256, expected_driver_sha256):
    started = time.monotonic()
    if sys.platform != 'win32':
        raise DiagnosticError('native_windows_required')
    if expected_source_sha256 != APPROVED_SOURCE or type(expected_driver_sha256) is not str or not re.fullmatch('[0-9a-f]{64}', expected_driver_sha256):
        raise DiagnosticError('source_rejected')
    with SOURCE.open('rb') as stream:
        source = stream.read(native.SOURCE_LIMIT + 1)
    with DRIVER.open('rb') as stream:
        driver = stream.read(native.SOURCE_LIMIT + 1)
    if (len(source) > native.SOURCE_LIMIT or len(driver) > native.SOURCE_LIMIT
            or hashlib.sha256(source).hexdigest() != APPROVED_SOURCE
            or hashlib.sha256(driver).hexdigest() != expected_driver_sha256):
        raise DiagnosticError('source_rejected')
    directory = native.system_directory()
    executable = directory / 'WindowsPowerShell/v1.0/powershell.exe'
    environment = {'SystemRoot': str(directory.parent), 'WINDIR': str(directory.parent),
                   'PSModulePath': str(executable.parent / 'Modules'),
                   'PATH': str(directory) + ';' + str(executable.parent)}
    encoded = base64.b64encode(driver.decode('utf-8').encode('utf-16le')).decode('ascii')
    payload = json.dumps({'source': source.decode('utf-8'), 'source_sha256': expected_source_sha256}).encode()
    remaining = native.TIMEOUT - (time.monotonic() - started)
    if remaining <= 0:
        raise DiagnosticError('diagnostic_timeout')
    result = capture([str(executable), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                     payload, expected_source_sha256, timeout=remaining, environment=environment)
    elapsed = (time.monotonic() - started) * 1000
    # Recursive inclusive helper totals can exceed wall time; never sum or
    # compare those totals to the external eight-second wall deadline.
    if elapsed > native.TIMEOUT * 1000:
        raise DiagnosticError('diagnostic_timeout')
    result.update(driver_sha256=expected_driver_sha256, elapsed_ms=elapsed)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collect-local', required=True, action='store_true')
    parser.add_argument('--source-sha256', required=True)
    parser.add_argument('--driver-sha256', required=True)
    args = parser.parse_args(argv)
    try:
        value = collect(args.source_sha256, args.driver_sha256)
    except Exception as error:
        reason = error.args[0] if isinstance(error, DiagnosticError) and error.args in tuple((r,) for r in
            ('source_rejected', 'native_windows_required', 'diagnostic_timeout', 'response_rejected', 'cleanup_incomplete', 'query_unavailable')) else 'query_unavailable'
        value = dict(schema_version=1, kind='ha-windows-component-diagnostic', authority=False,
                     ready=False, status='blocked', reason=reason)
        if isinstance(error, StreamError) and error.last_progress is not None:
            value['last_progress'] = error.last_progress
    print(json.dumps(value, sort_keys=True, allow_nan=False))
    return 0 if value['status'] == 'measured' else 1


if __name__ == '__main__':
    raise SystemExit(main())
