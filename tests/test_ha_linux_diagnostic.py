"""Pinned Linux diagnostic fixtures; no privileged/live provider calls."""
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import signal
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_ha_legacy_linux import fixture
sys.path.insert(0, str(ROOT / 'infra/k8s'))
from ha_control import legacy_guard

SPEC = importlib.util.spec_from_file_location('linux_diagnostic', ROOT / 'infra/k8s/tools/n100_ha_linux_diagnostic.py')
diagnostic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostic)


class LinuxDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.sha = hashlib.sha256(diagnostic.SOURCE.read_bytes()).hexdigest()
        self.module, self.reasons = diagnostic._load(self.sha)
        self.request, self.snapshot = fixture()
        self.request['collector_sha256'] = self.sha

    def profile(self, collector):
        return diagnostic._profile(self.module, self.reasons, self.request, self.sha, collector)

    def test_actual_pinned_source_two_snapshots_budget_and_validation_order_equivalent(self):
        def run(profile):
            events, budgets = [], []
            original = self.module.validate_snapshot
            def validate(request, snapshot):
                events.append('validate')
                return original(request, snapshot)
            def collector(budget):
                events.append('collect')
                budgets.append(budget)
                budget.left()
                return deepcopy(self.snapshot)
            with patch.object(self.module, 'validate_snapshot', validate):
                result = self.profile(collector) if profile else self.module.evaluate(self.request, collector=collector)
            self.assertEqual(len(budgets), 2)
            self.assertIs(budgets[0], budgets[1])
            return result, events
        original, expected = run(False)
        result, actual = run(True)
        self.assertTrue(original['verified'])
        self.assertEqual(actual, expected)
        self.assertEqual(result['status'], 'measured')
        rows = {row['phase']: row for row in result['phases']}
        self.assertEqual(rows['collector']['calls'], 2)
        self.assertEqual(rows['validation']['calls'], 5)
        self.assertEqual(rows['stability']['calls'], 2)
        self.assertFalse(result['authority'])
        self.assertFalse(result['ready'])
        self.assertLess(len(json.dumps(result)), diagnostic.OUTPUT_LIMIT)
        with self.assertRaises(Exception):
            legacy_guard.verify(result)

    def test_provider_io_wrappers_return_identical_objects_and_restore_after_failure(self):
        module = self.module
        values = {phase: object() for phase in diagnostic.PHASES}
        names = [('collect_docker', 'docker'), ('collect_kubernetes', 'kubernetes'),
                 ('collect_systemd', 'systemd'), ('collect_cron', 'cron'),
                 ('collect_processes', 'processes'), ('parse_mountinfo', 'mounts')]
        from contextlib import ExitStack
        with ExitStack() as stack:
            for name, phase in names:
                stack.enter_context(patch.object(module, name, return_value=values[phase]))
            for name, phase in [('file', 'read'), ('hash_file', 'hash_file'),
                                ('command', 'command'), ('docker', 'docker_query')]:
                stack.enter_context(patch.object(module.Budget, name, return_value=values[phase]))
            originals = {name: getattr(module, name) for name, _ in names}
            def collector(budget):
                for name, phase in names:
                    self.assertIs(getattr(module, name)(budget), values[phase])
                for name, phase in [('file', 'read'), ('hash_file', 'hash_file'),
                                    ('command', 'command'), ('docker', 'docker_query')]:
                    self.assertIs(getattr(budget, name)('private argument'), values[phase])
                return deepcopy(self.snapshot)
            result = self.profile(collector)
            self.assertEqual(result['status'], 'measured')
            for name, _ in names:
                self.assertIs(getattr(module, name), originals[name])
            rows = {r['phase']: r for r in result['phases']}
            for phase in ['docker', 'kubernetes', 'systemd', 'cron', 'processes', 'mounts',
                          'read', 'hash_file', 'command', 'docker_query']:
                self.assertEqual(rows[phase]['calls'], 2)

    def test_full_real_collector_body_with_only_synthetic_provider_boundaries(self):
        # Module-local identities plus entirely synthetic I/O: no process-global
        # platform/root impersonation and no real provider/file calls.
        from contextlib import ExitStack
        module = self.module
        order = []
        def provider(name, value):
            def call(*args, **kwargs):
                order.append(name)
                return deepcopy(value)
            return call
        with ExitStack() as stack:
            stack.enter_context(patch.object(module, 'sys', SimpleNamespace(platform='linux')))
            stack.enter_context(patch.object(module, 'os', SimpleNamespace(geteuid=lambda: 0)))
            stack.enter_context(patch.object(module.Budget, 'file', side_effect=lambda _, path:
                self.snapshot['boot_id'].encode() if path.endswith('boot_id') else b'fixture-mounts', autospec=True))
            stack.enter_context(patch.object(module, 'parse_mountinfo', return_value=[]))
            stack.enter_context(patch.object(module, 'mount_identity_catalog', return_value={}))
            stack.enter_context(patch.object(module, 'docker_identity_catalog', side_effect=lambda value: value))
            stack.enter_context(patch.object(module, 'kubernetes_identity_catalog', side_effect=lambda value: value))
            stack.enter_context(patch.object(module, 'collect_docker', side_effect=provider('docker',
                ('synthetic-engine', {}, self.snapshot['resources'], {}))))
            stack.enter_context(patch.object(module, 'collect_kubernetes', side_effect=provider('kubernetes',
                (self.snapshot['kube_system_uid'], {'objects': []}, [], {}))))
            stack.enter_context(patch.object(module, 'collect_systemd', side_effect=provider('systemd',
                ({'files': {}}, [], {}))))
            stack.enter_context(patch.object(module, 'os_activation_units', return_value=(set(), {})))
            stack.enter_context(patch.object(module, 'collect_cron', side_effect=provider('cron', ({}, []))))
            stack.enter_context(patch.object(module, 'collect_processes', side_effect=provider('processes',
                ([], self.snapshot['tcb_processes'], [], set(), set()))))
            stack.enter_context(patch.object(module, 'validate_activation_closure'))
            stack.enter_context(patch.object(module, 'provider_configuration', return_value={}))
            stack.enter_context(patch.object(module, 'protect_app_storage'))
            snapshot = module.collect_snapshot(module.Budget())
            self.request['spec']['providers'] = snapshot['providers']
            order.clear()
            result = diagnostic._profile(module, self.reasons, self.request, self.sha)
            self.assertEqual(result['status'], 'measured')
            self.assertEqual(order, ['docker', 'kubernetes', 'systemd', 'cron', 'processes'] * 2)
            self.assertIsNone(module._MOUNT_ENTRIES)
            self.assertEqual(module._SOURCE_PATHS, {})
            self.assertIsNone(module._LIVE_SPEC)
            rows = {row['phase']: row for row in result['phases']}
            self.assertEqual(rows['collector']['calls'], 2)
            self.assertEqual(rows['read']['calls'], 6)
            with patch.object(module, 'collect_kubernetes', side_effect=module.Blocked('live_provider_query_failed')):
                failed = diagnostic._profile(module, self.reasons, self.request, self.sha)
            self.assertEqual(failed['reason'], 'live_provider_query_failed')
            self.assertIsNone(module._MOUNT_ENTRIES)
            self.assertIsNone(module._LIVE_SPEC)
            rows = {row['phase']: row for row in failed['phases']}
            self.assertEqual(rows['kubernetes']['status'], 'blocked')
            self.assertEqual(rows['systemd']['calls'], 0)

    def test_failure_and_partial_and_changes_preserve_original_denial(self):
        for change, expected, calls in [('partial', 'incomplete_live_graph', 1),
                                        ('change', 'live_graph_changed_during_collection', 2),
                                        ('source', 'verified_collector_source_required', 0)]:
            count = []
            def collector(budget):
                count.append(budget)
                value = deepcopy(self.snapshot)
                if change == 'partial':
                    value['complete'] = False
                elif change == 'change' and len(count) == 2:
                    # uid identity is validated first, so change a valid managed
                    # runtime field not pinned for a legacy resource is invalid.
                    # A snapshot-only extra distinction in ordered inventory
                    # remains valid while its whole-snapshot hash differs.
                    value['resources'].reverse()
                return value
            request = deepcopy(self.request)
            if change == 'source':
                request['collector_sha256'] = 'f' * 64
            with self.subTest(change=change):
                with self.assertRaisesRegex(self.module.Blocked, expected):
                    self.module.evaluate(request, collector=collector)
                count.clear()
                result = diagnostic._profile(self.module, self.reasons, request, self.sha, collector)
                self.assertEqual(result['reason'], expected)
                self.assertEqual(len(count), calls)

    def test_root_gate_runs_before_any_real_provider_io(self):
        # Never impersonate root: force an explicitly unprivileged identity.
        with patch.object(self.module.os, 'geteuid', return_value=1000, create=True), \
                patch.object(self.module.Budget, 'file', side_effect=AssertionError('live read')):
            result = diagnostic._profile(self.module, self.reasons, self.request, self.sha)
        self.assertEqual(result['reason'], 'complete_linux_reader_required')

    def test_timer_timeout_and_signal_handlers_are_restored(self):
        if not hasattr(signal, 'SIGALRM'):
            self.fail('Unix signal fixture required')
        before = (signal.getsignal(signal.SIGALRM), signal.getsignal(signal.SIGTERM))
        def slow(budget):
            time.sleep(2)
            return self.snapshot
        started = time.monotonic()
        result = self.profile(slow)
        self.assertEqual(result['reason'], 'live_graph_deadline_exceeded')
        self.assertLess(time.monotonic() - started, 1.9)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        self.assertEqual(before, (signal.getsignal(signal.SIGALRM), signal.getsignal(signal.SIGTERM)))

    def test_existing_timer_is_never_replaced(self):
        original = signal.getsignal(signal.SIGALRM)
        signal.setitimer(signal.ITIMER_REAL, 5)
        try:
            result = self.profile(lambda budget: self.fail('collector called'))
            self.assertEqual(result['reason'], 'collector_timer_already_owned')
            self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0], 4)
            self.assertIs(signal.getsignal(signal.SIGALRM), original)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)

    def test_arbitrary_exceptions_and_inventory_never_escape(self):
        private = 'private-token /private/key synthetic-engine'
        for error in [RuntimeError(private), self.module.Blocked(private)]:
            def collector(budget):
                raise error
            result = self.profile(collector)
            serialized = json.dumps(result)
            self.assertEqual(result['reason'], 'query_unavailable')
            for forbidden in [private, 'nonce', 'plan_sha256', 'complete', 'verified', 'resources']:
                self.assertNotIn(forbidden, serialized)
            with self.assertRaises(Exception):
                legacy_guard.verify(result)

    def test_drifted_source_rejected_without_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'source.py'
            path.write_bytes(b'raise RuntimeError("must never execute")')
            with patch.object(diagnostic, 'SOURCE', path):
                with self.assertRaisesRegex(diagnostic.DiagnosticError, 'source_rejected'):
                    diagnostic._load(self.sha)
                path.write_bytes(b'x' * (diagnostic.SOURCE_LIMIT + 1))
                with self.assertRaisesRegex(diagnostic.DiagnosticError, 'source_rejected'):
                    diagnostic._load(self.sha)

    def test_cli_requires_explicit_flags_and_bounds_stdin_private_errors(self):
        class Input:
            buffer = io.BytesIO(b'x' * (diagnostic.REQUEST_LIMIT + 1))
        with patch.object(diagnostic.sys, 'stdin', Input()), patch.object(diagnostic.sys, 'platform', 'linux'), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            code = diagnostic.main(['--collect-local', '--source-sha256', self.sha])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())['reason'], 'request_rejected')
        with patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
            diagnostic.main(['--source-sha256', self.sha])


if __name__ == '__main__':
    unittest.main()
