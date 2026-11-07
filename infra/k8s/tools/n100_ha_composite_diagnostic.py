#!/usr/bin/env python3
"""Offline timing of the exact composite validator; never a live guard proof."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from types import ModuleType

SOURCE = Path(__file__).resolve().parents[1] / 'ha_control/legacy_guard.py'
APPROVED_SOURCE = 'efcb5adfa7c3fbf59c7c405b5347553e9e29a7a3c5c35bf494ba1c035bf88f00'
LIMIT = 1048576


class DiagnosticError(RuntimeError):
    pass


def collect(expected_source_sha256, payload):
    prepared = time.monotonic()
    if expected_source_sha256 != APPROVED_SOURCE:
        raise DiagnosticError('source_rejected')
    if type(payload) is not bytes or len(payload) > LIMIT:
        raise DiagnosticError('request_rejected')
    with SOURCE.open('rb') as stream:
        source = stream.read(LIMIT + 1)
    if len(source) > LIMIT or hashlib.sha256(source).hexdigest() != expected_source_sha256:
        raise DiagnosticError('source_rejected')
    module = ModuleType('_ha_offline_diagnostic_validator')
    module.__collector_sha256__ = expected_source_sha256
    # Known reviewed validator bytes only. Never execute its __main__ or any
    # caller-supplied source, provider, callback, shell, plan action or receipt.
    exec(compile(source, '<pinned-composite-validator>', 'exec'), module.__dict__)
    try:
        request = json.loads(payload, object_pairs_hook=module.pairs,
                             parse_constant=lambda _: module.deny())
    except (ValueError, TypeError, module.GuardBlocked, RecursionError):
        raise DiagnosticError('request_rejected') from None
    preparation_ms = max(0.0, (time.monotonic() - prepared) * 1000)
    started = time.monotonic()
    status, reason = 'measured', 'none'
    try:
        module.verify(request)  # Deliberately discard the authority-shaped result.
    except module.GuardBlocked:
        status, reason = 'blocked', 'verification_rejected'
    except (ValueError, TypeError, KeyError, OSError, RecursionError):
        status, reason = 'blocked', 'query_unavailable'
    elapsed_ms = max(0.0, (time.monotonic() - started) * 1000)
    if elapsed_ms > 2000:
        status, reason = 'blocked', 'validation_deadline'
    return dict(schema_version=1, kind='ha-composite-diagnostic', scope='offline-validation-only',
                authority=False, ready=False, source_sha256=expected_source_sha256,
                status=status, reason=reason, preparation_ms=preparation_ms,
                phases=[dict(phase='composite', calls=1, elapsed_ms=elapsed_ms,
                             status=status, reason=reason)])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--validate-offline', action='store_true', required=True)
    parser.add_argument('--source-sha256', required=True)
    args = parser.parse_args(argv)
    try:
        value = collect(args.source_sha256, sys.stdin.buffer.read(LIMIT + 1))
    except Exception as error:
        reason = error.args[0] if type(error) is DiagnosticError and error.args in (
            ('source_rejected',), ('request_rejected',)) else 'query_unavailable'
        value = dict(schema_version=1, kind='ha-composite-diagnostic', scope='offline-validation-only',
                     authority=False, ready=False, status='blocked', reason=reason)
    print(json.dumps(value, sort_keys=True, allow_nan=False))
    return 0 if value['status'] == 'measured' else 1


if __name__ == '__main__':
    raise SystemExit(main())
