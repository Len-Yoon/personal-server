"""Integrated synthetic operation; no Windows, Docker, cluster or SSH calls."""
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import base64
import importlib.util
import io
import json
import marshal
import os
from pathlib import Path
import tempfile
import struct
import types
import unittest
from unittest.mock import Mock, patch

from tests import test_k8s_caddy_ingress_transition as transition_tests
from tests import test_k8s_fixed_ingress_rollout as rollout_tests

SCRIPT = Path(__file__).resolve().parents[1] / 'infra/k8s/tools/fixed-ingress-operation.py'
SPEC = importlib.util.spec_from_file_location('fixed_ingress_operation', SCRIPT)
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class FakeGuard:
    def __init__(self):
        self.fail = False
        self.events = []

    def root_guard(self):
        self.events.append('root_acl')
        m.require(not self.fail, 'synthetic_acl_unsafe')

    def source_guard(self, path):
        self.events.append('source_acl')
        return True

    def execution_inputs_guard(self, paths):
        self.events.append('bulk_git_acl')
        m.require(not self.fail, 'synthetic_acl_unsafe')
        return 'a'*64

    def __call__(self, path, supplied=None):
        self.events.append('runtime_acl')
        m.require(not self.fail, 'synthetic_acl_unsafe')
        return True

    def owner_binding(self):
        return 'b'*64


class Harness:
    def __init__(self):
        self.events = []
        self.locked = False
        self.lock_entries = 0
        self.fail = set()
        self.caddy_backends = []
        self.health_count = 0
        self.health_fail_at = None
        self.controller_mode = 'success'
        self.policy_read_failure = False
        self.guard = FakeGuard()
        self.loader = Mock(return_value=(transition_tests.m, rollout_tests.rollout,
                                         types.SimpleNamespace(Collector=self.collector)))
        self.source = Mock(return_value={})

    def collector(self, cb, prepared, journal):
        self.events.append('collector')
        self.events.append(('policy_budget', cb.deadline))
        return types.SimpleNamespace(cleanup=lambda backend: True)

    def caddy(self, root, plan, protected_guard=None, runtime_file_guard=None):
        _, fresh, prepared = transition_tests.fixture()
        harness = self
        class Backend(transition_tests.FakeBackend):
            def __init__(self):
                super().__init__(plan, deepcopy(fresh), deepcopy(prepared))
                self.fail = harness.fail.copy()
                self.protected_guard = protected_guard

            @contextmanager
            def local_lock(self):
                assert not harness.locked
                harness.locked = True
                harness.lock_entries += 1
                harness.events.append('lock')
                try:
                    yield
                finally:
                    harness.events.append('unlock')
                    harness.locked = False

            def guard(self):
                assert self.lock_owned
                harness.events.append('caddy_guard')
                self.protected_guard()
                super().guard()

            def restore_runtime(self, prepared):
                harness.events.append('caddy_restore')
                super().restore_runtime(prepared)
        cb = Backend()
        self.caddy_backends.append(cb)
        return cb

    def policy(self, root, commit, journal=None):
        harness = self
        class Backend:
            def __init__(self):
                self.target_commit, self.journal = commit, journal
                self.collector = None
                self.termination_unknown = False
                self.objects = []

            def source_guard(self):
                harness.events.append('policy_source')

            def guards(self):
                harness.events.append('protection')
                return 'baseline'

            def external_health(self, app):
                assert harness.locked
                harness.health_count += 1
                harness.events.append(('health', app))
                return 503 if harness.health_fail_at == harness.health_count else 200

            def policies(self):
                if harness.policy_read_failure:
                    raise RuntimeError('synthetic private output must not leak')
                return self.objects

            def cleanup(self):
                harness.events.append('collector_cleanup')
                return True

            def begin_recovery(self):
                harness.events.append('policy_recovery_budget')
        self.pb = Backend()
        return self.pb

    def controller(self, backend, sleep=None):
        harness = self
        class Controller:
            def __init__(self):
                self.report = {}
                self.intents = []

            def run(self):
                assert harness.locked
                harness.events.append('policies')
                if harness.controller_mode == 'wrapped_journal_failure':
                    try:
                        backend.journal({'event': 'synthetic', 'address': 'unsafe'})
                    except m.JournalFailure:
                        self.report.update(status='FAIL', rollback_complete=True,
                            owned_cleanup_complete=True, restored_flow_proven=True)
                        raise rollout_tests.rollout.Blocked('policy_rollout_failed') from None
                if harness.controller_mode == 'incomplete':
                    self.report.update(status='FAIL', rollback_complete=False, owned_cleanup_complete=False)
                    raise RuntimeError('private exception details')
                if harness.controller_mode == 'failed':
                    harness.events.append('policies_removed')
                    self.report.update(status='FAIL', rollback_complete=True, owned_cleanup_complete=True,
                                       restored_flow_proven=True)
                    raise RuntimeError('private exception details')
                self.report.update(status='PASS', owned_cleanup_complete=True)
                return self.report

            def rollback(self):
                harness.events.append('policies_removed')

            def verify(self, app):
                harness.events.append('restored_probe')
        return Controller()


class OperationTests(unittest.TestCase):
    def setUp(self):
        # A production Linux home has protected ancestors; /tmp's world-write
        # permission must not become a test-only exception in that contract.
        self.temp = tempfile.TemporaryDirectory(dir=SCRIPT.parents[3])
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / 'repo'
        self.root.mkdir(mode=0o700)
        git = self.root / '.git'
        git.mkdir(mode=0o700)
        (git / 'hooks').mkdir(mode=0o700)
        for name in ('config', 'HEAD', 'index'):
            (git / name).write_bytes(b'fixture')
        self.parent = self.home / 'runs'
        self.h = Harness()
        self.operations = []

    def tearDown(self):
        # Injected synthetic HELD callbacks return for assertions. Production
        # has no such callback and remains inside the quiet live worker.
        for operation in self.operations:
            if operation.stack:
                operation.stack.close()
        self.temp.cleanup()

    def operation(self):
        op = m.Operation(self.root, '1'*40, self.parent, home=self.home,
            source_guard=self.h.source, tools_loader=self.h.loader, runtime_guard=self.h.guard,
            caddy_factory=self.h.caddy, policy_factory=self.h.policy,
            controller_factory=self.h.controller, sleep=Mock(), hold=lambda result: self.h.events.append('held'))
        self.operations.append(op)
        return op

    def prepared(self):
        return self.operation().prepare()

    def execute(self, prepared):
        return self.operation().execute(prepared['run_id'], prepared['prepared_sha256'])

    def test_prepare_is_read_only_and_persists_only_private_noncredential_projection(self):
        result = self.prepared()
        self.assertEqual(result['status'], 'PREPARED')
        self.assertEqual(self.h.lock_entries, 1)
        self.assertFalse(self.h.locked)
        cb = self.h.caddy_backends[-1]
        self.assertNotIn('create', cb.calls)
        self.assertNotIn('recreate', cb.calls)
        directory = self.parent / result['run_id']
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for path in directory.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(b'do-not-copy', path.read_bytes())
            self.assertNotIn(b'SYNTHETIC_TOKEN', path.read_bytes())
        plan = json.loads((directory / 'plan.json').read_bytes())
        self.assertFalse(plan['reviewed'])
        self.assertFalse(plan['protected_guard_ack'])
        self.assertNotIn('baseline_config', plan['prepared_projection'])
        self.assertNotIn('baseline_caddy', plan['prepared_projection'])

    def test_execute_holds_one_lock_across_caddy_health_and_policies(self):
        prepared = self.prepared()
        self.h.events.clear()
        prior_entries = self.h.lock_entries
        result = self.execute(prepared)
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(self.h.lock_entries-prior_entries, 1)
        self.assertFalse(self.h.locked)
        self.assertLess(self.h.events.index('collector'), self.h.events.index('policies'))
        first_collector = self.h.events.index('collector')
        for app in rollout_tests.rollout.ORDER:
            self.assertEqual(self.h.events[:first_collector].count(('health', app)), 3)
        self.assertEqual(self.h.events[-1], 'unlock')
        cb = self.h.caddy_backends[-1]
        self.assertEqual(cb.calls.count('recreate'), 1)
        self.assertFalse(cb.lock_owned)
        self.assertGreater(cb.deadline, 0)

    def test_acl_failure_precedes_source_import_and_metadata_creation(self):
        self.h.guard.fail = True
        with self.assertRaisesRegex(m.Blocked, 'synthetic_acl_unsafe'):
            self.prepared()
        self.h.loader.assert_not_called()
        self.h.source.assert_not_called()
        self.assertFalse(self.parent.exists())

    def test_source_pin_failure_precedes_tool_import(self):
        self.h.source.side_effect = m.Blocked('source_tool_pin_drift')
        with self.assertRaisesRegex(m.Blocked, 'source_tool_pin_drift'):
            self.prepared()
        self.h.loader.assert_not_called()
        self.assertFalse(self.parent.exists())

    def test_wrong_review_hash_cannot_start_any_mutation(self):
        prepared = self.prepared()
        before = self.h.lock_entries
        with self.assertRaisesRegex(m.Blocked, 'reviewed_preparation_mismatch'):
            self.operation().execute(prepared['run_id'], 'f'*64)
        self.assertEqual(self.h.lock_entries, before)
        self.assertNotIn('create', self.h.caddy_backends[-1].calls)

    def test_trusted_owner_change_since_prepare_still_requires_new_review(self):
        prepared = self.prepared()
        self.h.guard.owner_binding = lambda: 'c'*64
        with self.assertRaisesRegex(m.Blocked, 'runtime_owner_changed_since_preparation'):
            self.execute(prepared)
        self.assertFalse(self.h.locked)

    def test_postgate_fresh_owner_hash_must_stay_equal_to_baseline(self):
        op = self.operation()
        op.initialize()
        self.h.guard.owner_binding = lambda: 'c'*64
        with self.assertRaisesRegex(m.Blocked, 'runtime_owner_drift'):
            op.protected_guard()

    def test_fresh_projection_drift_blocks_and_marks_preflight_terminal(self):
        prepared = self.prepared()
        original = self.h.caddy
        def changed(*args, **kwargs):
            cb = original(*args, **kwargs)
            cb.fresh['compose_version'] = 'new-version'
            return cb
        self.h.caddy = changed
        with self.assertRaisesRegex(m.Blocked, 'fresh_preparation_mismatch'):
            self.execute(prepared)
        self.assertFalse(self.h.locked)
        self.assertEqual(m.status(self.parent, prepared['run_id'], self.home)['status'], 'BLOCKED')
        self.assertNotIn('create', self.h.caddy_backends[-1].calls)

    def test_unfinished_prior_run_blocks_new_prepare(self):
        prepared = self.prepared()
        journal = m.RunJournal(self.parent, prepared['run_id'], self.home)
        journal.state_save(status='RUNNING')
        journal.close()
        with self.assertRaisesRegex(m.Blocked, 'unfinished_prior_run'):
            self.prepared()
        self.assertFalse(self.h.locked)

    def test_completed_run_cannot_execute_twice(self):
        prepared = self.prepared()
        self.execute(prepared)
        with self.assertRaisesRegex(m.Blocked, 'prepared_run_already_consumed'):
            self.execute(prepared)

    def test_unknown_caddy_child_retains_live_lock_and_durable_blocker(self):
        prepared = self.prepared()
        self.h.fail.add('recreate_unknown')
        result = self.execute(prepared)
        self.assertEqual(result['status'], 'HELD')
        self.assertTrue(self.h.locked)
        self.assertTrue(self.h.caddy_backends[-1].lock_owned)
        self.assertEqual(self.h.caddy_backends[-1].calls.count('recreate'), 1)
        self.assertNotIn('restore', self.h.caddy_backends[-1].calls)
        self.assertEqual(m.status(self.parent, prepared['run_id'], self.home)['status'], 'HELD')

    def test_health_failure_after_caddy_transition_rolls_back_before_policies(self):
        prepared = self.prepared()
        self.h.health_fail_at = 1
        result = self.execute(prepared)
        self.assertEqual(result['status'], 'ROLLED_BACK')
        self.assertNotIn('policies', self.h.events)
        self.assertIn('caddy_restore', self.h.events)
        self.assertFalse(self.h.locked)
        self.assertEqual(self.h.caddy_backends[-1].calls.count('recreate'), 2)

    def test_native_observed_unknown_response_does_not_prevent_safe_rollback(self):
        prepared = self.prepared()
        original = self.h.caddy
        def lost_response(*args, **kwargs):
            cb = original(*args, **kwargs)
            cb.lost.add('recreate')
            return cb
        self.h.caddy = lost_response
        self.h.health_fail_at = 1
        self.assertEqual(self.execute(prepared)['status'], 'ROLLED_BACK')
        self.assertFalse(self.h.locked)

    def test_controller_failure_requires_policy_recovery_before_caddy_restore(self):
        prepared = self.prepared()
        self.h.controller_mode = 'failed'
        result = self.execute(prepared)
        self.assertEqual(result['status'], 'ROLLED_BACK')
        self.assertLess(self.h.events.index('policies_removed'), self.h.events.index('caddy_restore'))
        self.assertFalse(self.h.locked)

    def test_incomplete_policy_cleanup_holds_without_caddy_restore(self):
        prepared = self.prepared()
        self.h.controller_mode = 'incomplete'
        result = self.execute(prepared)
        self.assertEqual(result['status'], 'HELD')
        self.assertTrue(self.h.locked)
        self.assertNotIn('caddy_restore', self.h.events)

    def test_real_controller_unresolved_create_holds_without_caddy_restore_or_unlock(self):
        prepared = self.prepared()
        policy_backend = rollout_tests.FakeBackend()
        policy_backend.target_commit = '1'*40
        def pending(app, actual):
            del policy_backend.objects[actual['metadata']['name']]
            raise TimeoutError('pending API request')
        policy_backend.create_override = pending
        def policy(root, commit, journal=None):
            policy_backend.journal = journal
            return policy_backend
        controllers = []
        def controller(backend, sleep=None):
            result = rollout_tests.rollout.Controller(backend, generator=rollout_tests.candidate,
                now=lambda: rollout_tests.NOW, sleep=sleep, run_id='e'*32)
            controllers.append(result)
            return result
        operation = self.operation()
        operation.policy_factory, operation.controller_factory = policy, controller
        result = operation.execute(prepared['run_id'], prepared['prepared_sha256'])
        self.assertEqual(result['status'], 'HELD')
        self.assertTrue(self.h.locked)
        self.assertFalse(controllers[0].report['rollback_complete'])
        self.assertEqual(sum(event[0] == 'create' for event in policy_backend.events), 1)
        self.assertNotIn('caddy_restore', self.h.events)
        self.assertEqual(m.status(self.parent, prepared['run_id'], self.home)['status'], 'HELD')

    def test_recovery_precheck_exception_retains_lock(self):
        prepared = self.prepared()
        self.h.health_fail_at = 1
        self.h.policy_read_failure = True
        result = self.execute(prepared)
        self.assertEqual(result['status'], 'HELD')
        self.assertTrue(self.h.locked)
        self.assertNotIn('caddy_restore', self.h.events)

    def test_post_mutation_journal_failure_retains_lock_without_retry(self):
        prepared = self.prepared()
        original = m.RunJournal.emit
        def failed(journal, event, **fields):
            if event == 'transition_mutation_intent':
                raise m.JournalFailure('durable_journal_failed')
            return original(journal, event, **fields)
        with patch.object(m.RunJournal, 'emit', failed):
            result = self.execute(prepared)
        self.assertEqual(result['status'], 'HELD')
        self.assertTrue(self.h.locked)
        self.assertNotIn('recreate', self.h.caddy_backends[-1].calls)

    def test_controller_wrapped_journal_failure_latches_held_without_caddy_rollback(self):
        prepared = self.prepared()
        self.h.controller_mode = 'wrapped_journal_failure'
        result = self.execute(prepared)
        self.assertEqual(result['status'], 'HELD')
        self.assertTrue(self.h.locked)
        self.assertNotIn('caddy_restore', self.h.events)
        self.assertEqual(m.status(self.parent, prepared['run_id'], self.home)['status'], 'RUNNING')

    def test_caddy_durable_save_failure_latches_held_after_network_creation(self):
        prepared = self.prepared()
        original = transition_tests.m.Journal.save
        def failed(journal, **updates):
            if updates.get('phase') == 'network_created':
                raise OSError('private filesystem diagnostic')
            return original(journal, **updates)
        with patch.object(transition_tests.m.Journal, 'save', failed):
            result = self.execute(prepared)
        self.assertEqual(result['status'], 'HELD')
        self.assertTrue(self.h.locked)
        self.assertIn('create', self.h.caddy_backends[-1].calls)
        self.assertNotIn('recreate', self.h.caddy_backends[-1].calls)
        self.assertNotIn('caddy_restore', self.h.events)

    def test_status_is_metadata_only_and_does_not_append_or_contact_backends(self):
        prepared = self.prepared()
        events = self.parent / prepared['run_id'] / 'events.jsonl'
        before = events.read_bytes()
        with patch.object(m, 'run_readonly', side_effect=AssertionError('no operations')):
            self.assertEqual(m.status(self.parent, prepared['run_id'], self.home)['status'], 'PREPARED')
        self.assertEqual(before, events.read_bytes())

    def test_private_path_symlink_hardlink_and_modes_are_rejected(self):
        prepared = self.prepared()
        directory = self.parent / prepared['run_id']
        plan = directory / 'plan.json'
        os.chmod(plan, 0o644)
        with self.assertRaisesRegex(m.Blocked, 'private_file_mode'):
            m.startup_gate(self.parent, self.home)
        os.chmod(plan, 0o600)
        os.link(plan, self.home / 'linked')
        with self.assertRaisesRegex(m.Blocked, 'unsafe_regular_file'):
            m.startup_gate(self.parent, self.home)
        os.unlink(self.home / 'linked')
        link = self.home / 'symlink-runs'
        link.symlink_to(self.parent, target_is_directory=True)
        with self.assertRaisesRegex(m.Blocked, 'linux_home_private_path_required'):
            m.private_tree(link, self.home)

    def test_private_home_ancestor_untrusted_write_is_rejected(self):
        self.parent.mkdir(mode=0o700)
        real = m.os.lstat
        ancestor = self.home.parent
        def unsafe(path, *args, **kwargs):
            result = real(path, *args, **kwargs)
            if Path(path) == ancestor:
                values = list(result)
                values[0] |= 0o002
                return os.stat_result(values)
            return result
        with patch.object(m.os, 'lstat', unsafe):
            with self.assertRaisesRegex(m.Blocked, 'unsafe_private_ancestor'):
                m.private_tree(self.parent, self.home)

    def test_native_and_collector_journal_call_styles_share_fsynced_redacted_ledger(self):
        prepared = self.prepared()
        journal = m.RunJournal(self.parent, prepared['run_id'], self.home)
        try:
            with patch.object(m.os, 'fsync', wraps=os.fsync) as sync:
                journal.emit({'event': 'native_create_intent', 'name': 'book-memo-ingress-reviewed', 'spec_sha256': 'a'*64})
                journal.emit('disposable_created', kind='pod', name='unknown', uid='uid', spec_sha256='b'*64)
                self.assertEqual(sync.call_count, 2)
            for bad in ({'event': 'bad', 'password': 'value'}, {'event': 'bad', 'value': '192.0.2.1'}):
                with self.assertRaises(m.JournalFailure):
                    journal.emit(bad)
        finally:
            journal.close()


class WindowsGuardTests(unittest.TestCase):
    def guard(self):
        with patch.object(Path, 'resolve', lambda path: path.absolute()):
            return m.WindowsRuntimeGuard('/mnt/c/personal-server', runner=Mock())

    def inventory(self, *, file=True):
        sid = 'S-1-5-21-1-2-3-1001'
        trusted = (sid, 'S-1-5-18', 'S-1-5-32-544')
        rules = [{'sid': s, 'rights': 2032127, 'allow': True, 'inherit_only': False, 'inherited': False} for s in trusted]
        paths = ['C:\\personal-server\\.env', 'C:\\personal-server', 'C:\\'] if file else ['C:\\personal-server', 'C:\\']
        return {'current_sid': sid, 'trusted_installer_sid': 'S-1-5-80-123', 'objects': [
            {'path': path, 'owner': sid, 'protected': True, 'canonical': True, 'reparse': False,
             'directory': not (file and index == 0), 'rules': deepcopy(rules)} for index, path in enumerate(paths)]}

    def test_runtime_acl_requires_exact_current_system_admin_and_all_ancestors(self):
        guard = self.guard()
        guard.check_acl(self.inventory(), Path('/mnt/c/personal-server/.env'))
        for mutate in (lambda v: v['objects'][0]['rules'].append({'sid': 'S-1-1-0', 'rights': 2032127, 'allow': True}),
                       lambda v: v['objects'][0].update(protected=False),
                       lambda v: v['objects'][1].update(reparse=True),
                       lambda v: v['objects'][2]['rules'].append({'sid': 'S-1-1-0', 'rights': 64, 'allow': True}),
                       lambda v: v['objects'].pop()):
            with self.subTest(mutate=mutate):
                invalid = self.inventory()
                mutate(invalid)
                with self.assertRaises(m.Blocked):
                    guard.check_acl(invalid, Path('/mnt/c/personal-server/.env'))

    def test_source_file_with_explicit_untrusted_write_is_rejected(self):
        guard, value = self.guard(), self.inventory()
        value['objects'][0]['rules'].append({'sid': 'S-1-1-0', 'rights': 2, 'allow': True})
        with self.assertRaisesRegex(m.Blocked, 'windows_untrusted_ancestor_write'):
            guard.check_acl(value, Path('/mnt/c/personal-server/.env'), source=True, directory=False)

    def test_trusted_system_or_admin_owner_is_accepted_without_owner_remediation(self):
        for owner in ('S-1-5-18', 'S-1-5-32-544'):
            with self.subTest(owner=owner):
                observed = self.inventory()
                for row in observed['objects']:
                    row['owner'] = owner
                self.guard().check_acl(observed, Path('/mnt/c/personal-server/.env'))

    def test_powershell_file_parent_uses_directory_and_bulk_scans_all_git_inputs(self):
        guard = self.guard()
        guard.runner.return_value = b'{}'
        guard.acl(Path('/mnt/c/personal-server/.env'))
        script = base64.b64decode(guard.runner.call_args.args[0][-1]).decode('utf-16le')
        # .Parent returns plain DirectoryInfo without provider PSIsContainer.
        self.assertNotIn('PSIsContainer', script)
        self.assertIn('$isDirectory=($item -is [IO.DirectoryInfo])', script)
        self.assertIn('directory=$isDirectory;rules=$rules', script)
        self.assertIn('$item=if($isDirectory){$item.Parent}else{$item.Directory}', script)
        guard.root_guard = Mock()
        guard.runner.return_value = json.dumps({'safe': False, 'count': 8, 'acl_sha256': 'a'*64}).encode()
        with self.assertRaisesRegex(m.Blocked, 'execution_input_acl_unsafe'):
            guard.execution_inputs_guard([Path('/mnt/c/personal-server/infra/k8s/tools/a.py')])
        script = base64.b64decode(guard.runner.call_args.args[0][-1]).decode('utf-16le')
        self.assertIn('Get-ChildItem -LiteralPath $v.git -Force -Recurse', script)
        self.assertNotIn('Set-Acl', script)

    def test_directory_ancestors_require_strict_boolean_type(self):
        guard = self.guard()
        guard.check_acl(self.inventory(file=False), Path('/mnt/c/personal-server'))
        for index in (0, 1):
            for value in (None, False, 1, 'True'):
                with self.subTest(index=index, value=value):
                    observed = self.inventory(file=False)
                    observed['objects'][index]['directory'] = value
                    with self.assertRaisesRegex(m.Blocked, 'windows_object_type'):
                        guard.check_acl(observed, Path('/mnt/c/personal-server'))

    def test_file_target_cannot_be_reported_as_directory(self):
        observed = self.inventory()
        observed['objects'][0]['directory'] = True
        with self.assertRaisesRegex(m.Blocked, 'windows_object_type'):
            self.guard().check_acl(observed, Path('/mnt/c/personal-server/.env'))

    def test_windows_snapshot_collision_inventory_preserves_kwargs_and_filters_defaults(self):
        class Parent:
            def snapshot(self, **kwargs):
                self.kwargs = kwargs
                return {'collisions': ['10.42.0.0/16']}
            def run(self, args, **options):
                return json.dumps({'routes': ['0.0.0.0/0', '10.240.0.0/29'],
                                   'addresses': ['192.0.2.7/24', 'fe80::1%12/64']}).encode()
        backend = m.native_caddy_type(types.SimpleNamespace(NativeBackend=Parent))()
        value = backend.snapshot(allow_absent=True, config_override={'x': 1})
        self.assertEqual(backend.kwargs, {'allow_absent': True, 'config_override': {'x': 1}})
        self.assertEqual(value['collisions'], ['10.42.0.0/16', '10.240.0.0/29', '192.0.2.0/24', 'fe80::/64'])

    def test_cli_error_does_not_print_private_exception_text(self):
        with patch.object(m, 'status', side_effect=RuntimeError('synthetic_private_token_value')):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(m.main(['--status', '--run-id', 'a'*32]), 2)
        self.assertNotIn('synthetic_private_token_value', output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['reason'], 'operation_failed')


class SourceTrustTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        git = self.root / '.git'
        git.mkdir(mode=0o700)
        self.pins = {}
        for relative in ['infra/k8s/tools/'+name for name in m.TOOLS] + ['scripts/runtime-service-state-reader.py']:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('source_'+relative).encode())
            self.pins[relative] = path.read_bytes()

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, args, **kwargs):
        self.assertEqual(args[:5], ['git', '--no-replace-objects', '--no-optional-locks',
                                   '-c', 'core.attributesFile=/dev/null'])
        command = args[5:]
        if command == ['rev-parse', 'HEAD']:
            return b'1'*40
        if command == ['branch', '--show-current']:
            return b'main'
        if command == ['rev-parse', '--absolute-git-dir']:
            return str(self.root / '.git').encode()
        if command[0] == 'show':
            return self.pins[command[1].split(':', 1)[1]]
        return b''

    def test_bootstrap_verifies_exact_clean_main_and_source_bytes_without_optional_writes(self):
        with patch.dict(os.environ, {}, clear=True):
            actual = m.bootstrap_source_guard(self.root, '1'*40, self.runner)
        self.assertEqual(actual, {path: m.sha(raw) for path, raw in self.pins.items()})

    def test_git_environment_gitdir_replacement_and_external_alternates_are_rejected(self):
        with patch.dict(os.environ, {'GIT_DIR': '/synthetic/other'}, clear=True):
            with self.assertRaisesRegex(m.Blocked, 'git_environment_override'):
                m.bootstrap_source_guard(self.root, '1'*40, self.runner)
        def different_gitdir(args, **kwargs):
            if args[5:] == ['rev-parse', '--absolute-git-dir']:
                return b'/synthetic/other'
            return self.runner(args, **kwargs)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(m.Blocked, 'git_directory_binding_required'):
                m.bootstrap_source_guard(self.root, '1'*40, different_gitdir)
            def replaced(args, **kwargs):
                if args[5] == 'for-each-ref':
                    return b'refs/replace/synthetic'
                return self.runner(args, **kwargs)
            with self.assertRaisesRegex(m.Blocked, 'git_replacement_or_alternate_forbidden'):
                m.bootstrap_source_guard(self.root, '1'*40, replaced)
            alternate = self.root / '.git/objects/info/alternates'
            alternate.parent.mkdir(parents=True)
            alternate.write_text('/synthetic/other')
            with self.assertRaisesRegex(m.Blocked, 'git_replacement_or_alternate_forbidden'):
                m.bootstrap_source_guard(self.root, '1'*40, self.runner)

    def test_local_external_execution_configuration_rejected_without_following_includes(self):
        entries = ('include.path\n/synthetic/private', 'includeIf.gitdir:/x/.path\n/synthetic/private',
                   'filter.synthetic.process\nprivate-command', 'filter.synthetic.clean\nprivate-command',
                   'core.attributesFile\n/synthetic/private', 'core.sshCommand\nprivate-command',
                   'diff.synthetic.command\nprivate-command', 'core.fsmonitor\nprivate-command',
                   'core.hooksPath\n/synthetic/private')
        for entry in entries:
            calls = []
            def configured(args, **kwargs):
                calls.append(args[5:])
                if args[5:] == ['config', '--no-includes', '--null', '--list']:
                    return (entry+'\0core.attributesfile\n/dev/null\0').encode()
                return self.runner(args, **kwargs)
            with self.subTest(key=entry.split('\n')[0]), patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(m.Blocked, 'unsafe_git_configuration'):
                    m.bootstrap_source_guard(self.root, '1'*40, configured)
            self.assertEqual(calls, [['config', '--no-includes', '--null', '--list']])

    def test_readonly_git_child_environment_excludes_external_configuration(self):
        child = Mock(returncode=0, pid=987654321)
        child.communicate.return_value = (b'readonly', None)
        parent = {'SYNTHETIC_PARENT': 'preserved', 'GIT_DIR': '/synthetic/other',
                  'GIT_CONFIG_COUNT': '1', 'GIT_TRACE': '/synthetic/private'}
        with patch.object(m.subprocess, 'Popen', return_value=child) as popen, \
             patch.object(m.os, 'killpg'), patch.dict(os.environ, parent, clear=True):
            self.assertEqual(m.run_readonly(['git', 'status']), b'readonly')
            self.assertEqual(dict(os.environ), parent)
        self.assertEqual(popen.call_args.kwargs['env'], {'SYNTHETIC_PARENT': 'preserved',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_ATTR_NOSYSTEM': '1'})

    def test_timestamp_size_valid_poisoned_pyc_cannot_replace_transitive_source(self):
        repository = SCRIPT.parents[3]
        pins = {}
        for filename in m.TOOLS:
            relative = 'infra/k8s/tools/' + filename
            path = self.root / relative
            raw = (repository / relative).read_bytes()
            path.write_bytes(raw)
            pins[relative] = m.sha(raw)
            info = path.stat()
            code = compile("raise RuntimeError('poisoned_timestamp_cache')\n", str(path), 'exec')
            cache = Path(importlib.util.cache_from_source(str(path)))
            cache.parent.mkdir(exist_ok=True)
            cache.write_bytes(importlib.util.MAGIC_NUMBER + struct.pack('<III', 0, int(info.st_mtime), info.st_size) + marshal.dumps(code))
        caddy, rollout, evidence = m.load_tools(self.root, pins)
        backend = caddy.NativeBackend(self.root, {'source_commit': '1'*40})
        self.assertTrue(callable(backend.local_lock))
        generator = rollout.load_generator()
        from tests import test_project_hardening_tools as contracts
        _, snapshot, inventory = contracts.FixedPreNatIngressTests().fixed_fixture()
        self.assertEqual(generator(snapshot, inventory)['caddy_source_kind'], 'fixed_pre_nat_ip')
        self.assertTrue(callable(evidence.Collector))


if __name__ == '__main__':
    unittest.main()
