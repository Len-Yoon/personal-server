"""Actual offline validator and Linux two-snapshot integration; no live providers."""
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'infra/k8s'))
from ha_control import legacy_guard as guard
from tests.test_ha_legacy_linux import fixture as linux_fixture


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'infra/k8s/tools' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DIAG = load('composite_diagnostic', 'n100_ha_composite_diagnostic.py')
LINUX = load('linux_diagnostic_integration', 'n100_ha_linux_diagnostic.py')


def fixture():
    observation = dict(schema_version=1, complete=True, boot_ticks='123456',
                       principal_sha256='a'*64, services=[], tasks=[], processes=[], startup_sha256='b'*64)
    spec = dict(schema_version=1, boot_ticks='123456', principal_sha256='a'*64,
                inventory_sha256=guard.digest(observation), frozen_services=[], frozen_tasks=[],
                provider_services=[], provider_tasks=[], provider_processes=[])
    linux_request, snapshot = linux_fixture()
    source_sha = hashlib.sha256(LINUX.SOURCE.read_bytes()).hexdigest()
    config = dict(schema_version=1, windows=spec,
                  linux=dict(distribution='Fixture', reader_user='root', spec=linux_request['spec']))
    plan = dict(source_commit='a'*40, cluster='PRIVATE_CLUSTER', generation=3,
                legacy_guard=config, legacy_linux_sha256=source_sha,
                legacy_guard_code_sha256=DIAG.APPROVED_SOURCE)
    raw = json.dumps(plan).encode(); plan_sha = hashlib.sha256(raw).hexdigest()
    approval = dict(action='ha-transition', source_commit=plan['source_commit'], plan_sha256=plan_sha,
                    expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat().replace('+00:00','Z'))
    linux_request.update(nonce='e'*64, cluster=plan['cluster'], plan_sha256=plan_sha, collector_sha256=source_sha)
    module, reasons = LINUX._load(source_sha)
    receipt = module.evaluate(linux_request, collector=lambda budget: deepcopy(snapshot))
    request = dict(schema_version=1, nonce='e'*64, plan_sha256=plan_sha, cluster=plan['cluster'], generation=3,
                   collector_sha256=source_sha, guard=config, windows=observation, linux_receipt=receipt,
                   plan_raw=base64.b64encode(raw).decode(), approval_raw=base64.b64encode(json.dumps(approval).encode()).decode())
    return request, linux_request, snapshot, module, reasons


class CompositeDiagnosticTests(unittest.TestCase):
    def test_source_validator_acceptance_does_not_export_or_grant_authority(self):
        request, _, _, _, _ = fixture()
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('provider called')), patch.object(socket, 'socket', side_effect=AssertionError('network called')):
            result = DIAG.collect(DIAG.APPROVED_SOURCE, json.dumps(request).encode())
        self.assertEqual(result['status'], 'measured')
        self.assertEqual(result['scope'], 'offline-validation-only')
        self.assertIs(result['authority'], False); self.assertIs(result['ready'], False)
        text = json.dumps(result)
        for forbidden in ('PRIVATE_CLUSTER', request['nonce'], request['plan_raw'], 'linux_receipt', 'verified', 'complete'):
            self.assertNotIn(forbidden, text)
        with self.assertRaises(guard.GuardBlocked):
            guard.verify(result)

    def test_actual_linux_profile_and_composite_are_not_mutually_substitutable(self):
        request, linux_request, snapshot, module, reasons = fixture()
        seen = []
        def reader(budget):
            seen.append(id(budget)); budget.left()
            return deepcopy(snapshot)
        linux_result = LINUX._profile(module, reasons, linux_request, linux_request['collector_sha256'], collector=reader)
        self.assertEqual(len(seen), 2); self.assertEqual(seen[0], seen[1])
        self.assertEqual(linux_result['status'], 'measured')
        self.assertEqual(DIAG.collect(DIAG.APPROVED_SOURCE, json.dumps(request).encode())['status'], 'measured')
        request['linux_receipt'] = linux_result
        denied = DIAG.collect(DIAG.APPROVED_SOURCE, json.dumps(request).encode())
        self.assertEqual((denied['status'], denied['reason']), ('blocked', 'verification_rejected'))
        with self.assertRaises(guard.GuardBlocked):
            guard.verify(linux_result)

    def test_original_rejections_and_unknown_exceptions_remain_non_authorizing(self):
        request, *_ = fixture()
        variants = []
        for field, value in [('nonce', 'f'*64), ('generation', True), ('collector_sha256', '0'*64)]:
            item = deepcopy(request); item[field] = value; variants.append(item)
        item = deepcopy(request); item['windows']['complete'] = False; variants.append(item)
        item = deepcopy(request); item['linux_receipt']['verified'] = False; variants.append(item)
        item = deepcopy(request); item['guard']['windows']['provider_tasks'].append('f'*64); variants.append(item)
        for item in variants:
            result = DIAG.collect(DIAG.APPROVED_SOURCE, json.dumps(item).encode())
            self.assertEqual(result['status'], 'blocked')
            self.assertIs(result['authority'], False)
            self.assertNotIn('PRIVATE_CLUSTER', json.dumps(result))

    def test_bad_source_and_changed_source_cannot_execute(self):
        with self.assertRaises(DIAG.DiagnosticError):
            DIAG.collect('0'*64, b'{}')
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'source.py'; source.write_text('raise AssertionError("PRIVATE_EXECUTED")')
            with patch.object(DIAG, 'SOURCE', source):
                for pin in (DIAG.APPROVED_SOURCE, hashlib.sha256(source.read_bytes()).hexdigest()):
                    with self.assertRaisesRegex(DIAG.DiagnosticError, '^source_rejected$'):
                        DIAG.collect(pin, b'{}')

    def test_bounded_json_and_duplicates_are_rejected(self):
        for raw in (b'x'*(DIAG.LIMIT+1), b'{"x":1,"x":2}', b'{"x":NaN}', b'[not-json]', b'['*2000):
            with self.subTest(raw_size=len(raw)), self.assertRaises(DIAG.DiagnosticError):
                DIAG.collect(DIAG.APPROVED_SOURCE, raw)

    def test_timing_overrun_fails_and_does_not_relax_original_guard(self):
        request, *_ = fixture()
        with patch.object(DIAG.time, 'monotonic', side_effect=[0, .001, .001, 2.003]):
            value = DIAG.collect(DIAG.APPROVED_SOURCE, json.dumps(request).encode())
        self.assertEqual(value['reason'], 'validation_deadline')
        self.assertEqual(value['status'], 'blocked')
        self.assertIs(value['ready'], False)

    def test_isolated_cli_rejects_unknown_input_without_printing_it(self):
        result = subprocess.run([sys.executable, '-I', '-B', '-S', str(ROOT/'infra/k8s/tools/n100_ha_composite_diagnostic.py'),
                                 '--validate-offline', '--source-sha256', DIAG.APPROVED_SOURCE],
                                input=b'{"PRIVATE_TOKEN":"DO_NOT_PRINT"}', capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(b'PRIVATE_', result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'blocked')


if __name__ == '__main__':
    unittest.main()
