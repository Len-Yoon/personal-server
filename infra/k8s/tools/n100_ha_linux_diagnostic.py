#!/usr/bin/env python3
"""Local full Linux collector timing; never an authority receipt.

Read-only provider calls still require the collector's existing root capability.
This diagnostic grants no privilege and does not establish OS least privilege.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from types import ModuleType

SOURCE = Path(__file__).resolve().parents[1] / 'ha_control/legacy_linux.py'
SOURCE_LIMIT = 131072
REQUEST_LIMIT = 1048576
OUTPUT_LIMIT = 16384
PHASES = ('collector', 'docker', 'kubernetes', 'systemd', 'cron', 'processes',
          'mounts', 'identity', 'read', 'hash_file', 'command', 'docker_query',
          'validation', 'stability', 'signals')


class DiagnosticError(RuntimeError):
    pass


def _load(expected_sha256):
    if type(expected_sha256) is not str or not re.fullmatch('[0-9a-f]{64}', expected_sha256):
        raise DiagnosticError('source_rejected')
    with SOURCE.open('rb') as stream:
        source = stream.read(SOURCE_LIMIT + 1)
    if len(source) > SOURCE_LIMIT or hashlib.sha256(source).hexdigest() != expected_sha256:
        raise DiagnosticError('source_rejected')
    # Compile the exact bytes already pinned, never reopen/execute the path.
    tree = ast.parse(source, filename='<pinned-linux-collector>')
    reasons = frozenset(node.args[0].value for node in ast.walk(tree)
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == 'deny' and len(node.args) == 1
                        and isinstance(node.args[0], ast.Constant)
                        and type(node.args[0].value) is str)
    module = ModuleType('_ha_linux_diagnostic_pinned_collector')
    module.__collector_sha256__ = expected_sha256
    exec(compile(tree, '<pinned-linux-collector>', 'exec'), module.__dict__)
    return module, reasons


def _reason(error, module, reasons):
    # Only reviewed literal deny reasons from the externally pinned source.
    if isinstance(error, module.Blocked) and len(error.args) == 1:
        candidate = error.args[0]
        if type(candidate) is str and candidate in reasons:
            return candidate
    return 'query_unavailable'


class _Metrics:
    def __init__(self, module, reasons):
        self.module, self.reasons = module, reasons
        self.rows = {phase: dict(phase=phase, calls=0, elapsed_ms=0.0,
                                 status='not_run', reason='none') for phase in PHASES}
        self.restore = []

    def wrap(self, function, phase):
        def measured(*args, **kwargs):
            row = self.rows[phase]
            started = time.monotonic()
            row['calls'] += 1
            try:
                result = function(*args, **kwargs)
            except BaseException as error:
                row['status'], row['reason'] = 'blocked', _reason(error, self.module, self.reasons)
                raise
            else:
                if row['status'] != 'blocked':
                    row['status'] = 'measured'
                return result
            finally:
                row['elapsed_ms'] += max(0.0, (time.monotonic() - started) * 1000)
        return measured

    def replace(self, owner, name, phase):
        original = getattr(owner, name)
        self.restore.append((owner, name, original))
        setattr(owner, name, self.wrap(original, phase))

    def install(self):
        module = self.module
        for name, phase in (
                ('collect_snapshot', 'collector'), ('collect_docker', 'docker'),
                ('collect_kubernetes', 'kubernetes'), ('collect_systemd', 'systemd'),
                ('collect_cron', 'cron'), ('collect_processes', 'processes'),
                ('parse_mountinfo', 'mounts'), ('mount_identity_catalog', 'mounts'),
                ('protect_app_storage', 'mounts'), ('provider_configuration', 'identity'),
                ('validate_activation_closure', 'identity'), ('os_activation_units', 'identity'),
                ('validate_request', 'validation'), ('validate_snapshot', 'validation')):
            self.replace(module, name, phase)
        for name, phase in (('file', 'read'), ('hash_file', 'hash_file'),
                            ('command', 'command'), ('docker', 'docker_query')):
            self.replace(module.Budget, name, phase)
        original_digest = module.digest
        measured_digest = self.wrap(original_digest, 'stability')
        def digest(value):
            # evaluate's two whole-snapshot hashes, not per-resource hashes.
            if type(value) is dict and set(value) == {'uid', 'boot_id', 'engine_id',
                    'kube_system_uid', 'providers', 'resources', 'tcb_processes', 'complete'}:
                return measured_digest(value)
            return original_digest(value)
        self.restore.append((module, 'digest', original_digest))
        module.digest = digest
        original_signal = module.signal
        metrics = self
        class Signals:
            def __getattr__(self, name):
                value = getattr(original_signal, name)
                return metrics.wrap(value, 'signals') if name in {
                    'getsignal', 'getitimer', 'signal', 'setitimer'} else value
        self.restore.append((module, 'signal', original_signal))
        module.signal = Signals()

    def close(self):
        for owner, name, original in reversed(self.restore):
            setattr(owner, name, original)


def _profile(module, reasons, request, source_sha256, collector=None):
    """Private fixture seam: CLI always uses the unchanged real collector."""
    metrics = _Metrics(module, reasons)
    started = time.monotonic()
    status, reason = 'measured', 'none'
    try:
        metrics.install()
        selected = module.collect_snapshot if collector is None else metrics.wrap(collector, 'collector')
        # evaluate's default captured the original function: pass the wrapper.
        # Discard its private receipt immediately; no proof conversion path.
        module.evaluate(request, collector=selected)
    except Exception as error:
        status, reason = 'blocked', _reason(error, module, reasons)
    finally:
        metrics.close()
    return dict(schema_version=1, kind='ha-linux-collector-diagnostic', authority=False,
                ready=False, source_sha256=source_sha256, status=status, reason=reason,
                elapsed_ms=max(0.0, (time.monotonic() - started) * 1000),
                phases=list(metrics.rows.values()))


def collect(expected_source_sha256, payload):
    if sys.platform != 'linux':
        raise DiagnosticError('native_linux_required')
    if type(payload) is not bytes or len(payload) > REQUEST_LIMIT:
        raise DiagnosticError('request_rejected')
    module, reasons = _load(expected_source_sha256)
    try:
        request = module.decode(payload)
    except Exception:
        raise DiagnosticError('request_rejected') from None
    return _profile(module, reasons, request, expected_source_sha256)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collect-local', action='store_true', required=True)
    parser.add_argument('--source-sha256', required=True)
    args = parser.parse_args(argv)
    try:
        payload = sys.stdin.buffer.read(REQUEST_LIMIT + 1)
        value = collect(args.source_sha256, payload)
    except Exception as error:
        reason = error.args[0] if type(error) is DiagnosticError and error.args in (
            ('source_rejected',), ('request_rejected',), ('native_linux_required',)) else 'query_unavailable'
        value = dict(schema_version=1, kind='ha-linux-collector-diagnostic', authority=False,
                     ready=False, status='blocked', reason=reason)
    output = json.dumps(value, sort_keys=True, allow_nan=False)
    if len(output.encode()) > OUTPUT_LIMIT:
        value = dict(schema_version=1, kind='ha-linux-collector-diagnostic', authority=False,
                     ready=False, status='blocked', reason='response_rejected')
        output = json.dumps(value, sort_keys=True)
    print(output)
    return 0 if value['status'] == 'measured' else 1


if __name__ == '__main__':
    raise SystemExit(main())
