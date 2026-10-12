import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'infra/k8s/tools/windows-source-acl.py'


def load():
    spec = importlib.util.spec_from_file_location('windows_source_acl', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SourceAclPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load()

    def acl(self):
        return {'protected': False, 'aces': [
            {'type': 'deny', 'sid': 'other', 'mask': 2, 'flags': 0},
            {'type': 'allow', 'sid': 'trusted', 'mask': 0x1f01ff, 'flags': 3},
            {'type': 'allow', 'sid': 'other', 'mask': 0x1201ff, 'flags': 0x1b}]}

    def test_reduce_only_unsafe_bits_including_inherit_only_and_flatten(self):
        before = self.acl()
        after = self.m.transform_dacl(before, {'trusted'})
        self.assertTrue(after['protected'])
        self.assertEqual(after['aces'][2]['mask'], 0x1201ff & ~0xd0156)
        self.assertEqual(after['aces'][2]['flags'], 0xb)
        self.assertEqual(after['aces'][:2], before['aces'][:2])
        self.assertFalse(before['protected'])

    def test_inherited_deny_after_explicit_allow_blocks_without_reordering(self):
        acl = self.acl()
        acl['aces'] = [acl['aces'][1], dict(acl['aces'][0], flags=16)]
        with self.assertRaises(self.m.Blocked):
            self.m.transform_dacl(acl, {'trusted'})

    def test_opaque_generic_null_and_callback_rejected(self):
        for field, value in [('type', 'object_allow'), ('mask', 0x80000000), ('callback', True)]:
            acl = self.acl()
            acl['aces'][0][field] = value
            with self.subTest(field=field), self.assertRaises(self.m.Blocked):
                self.m.transform_dacl(acl, {'trusted'})
        with self.assertRaises(self.m.Blocked):
            self.m.transform_dacl(None, {'trusted'})

    def targets(self):
        return [{'path': 'dir%03d' % i, 'kind': 'directory'} for i in range(58)] + [
            {'path': 'file%03d' % i, 'kind': 'file'} for i in range(457)]

    def test_outside_change_and_owner_or_metadata_change_rejected(self):
        before = {'entries': {'dir000': {'acl': self.acl(), 'owner': 'a', 'size': 1},
                              'outside': {'acl': self.acl(), 'owner': 'b', 'size': 2}}}
        after = copy.deepcopy(before)
        after['entries']['dir000']['acl'] = self.m.transform_dacl(self.acl(), {'trusted'})
        with patch.object(self.m, 'TARGETS_SHA256', self.m.object_hash(self.targets())):
            self.m.validate_projection(before, after, 'dir000', {'trusted'}, self.targets())
        for path, field, value in [('outside', 'size', 3), ('dir000', 'owner', 'z'), ('dir000', 'size', 4)]:
            changed = copy.deepcopy(after)
            changed['entries'][path][field] = value
            with self.subTest(path=path, field=field), self.assertRaises(self.m.Blocked):
                with patch.object(self.m, 'TARGETS_SHA256', self.m.object_hash(self.targets())):
                    self.m.validate_projection(before, changed, 'dir000', {'trusted'}, self.targets())

    def test_own_partial_requires_exact_whole_entries_and_outside(self):
        before = {'entries': {'x': {'v': 1}, 'outside': {'v': 2}}}
        after = {'entries': {'x': {'v': 3}, 'outside': {'v': 2}}}
        self.assertTrue(self.m.own_partial(before, before, after))
        self.assertTrue(self.m.own_partial(after, before, after))
        mixed = copy.deepcopy(after)
        mixed['entries']['outside']['v'] = 9
        self.assertFalse(self.m.own_partial(mixed, before, after))

    def test_wrong_exact_list_duplicate_case_and_protected_targets_rejected(self):
        rows = self.targets()
        with patch.object(self.m, 'TARGETS_SHA256', self.m.object_hash(rows)):
            self.assertEqual(len(self.m.validate_targets(rows)), 515)
            for index, path, reason in [(1, 'DIR000', 'target_alias_or_order'),
                                       (0, '.git/x', 'protected_target'),
                                       (0, '.env', 'protected_target'),
                                       (0, '../x', 'invalid_target_path')]:
                changed = copy.deepcopy(rows)
                changed[index]['path'] = path
                with self.subTest(path=path), self.assertRaises(self.m.Blocked) as raised:
                    self.m.validate_targets(changed)
                self.assertEqual(raised.exception.reason, reason)
            changed = copy.deepcopy(rows)
            changed[0]['kind'] = 'file'
            with self.assertRaises(self.m.Blocked) as raised:
                self.m.validate_targets(changed)
            self.assertEqual(raised.exception.reason, 'target_kind_counts')
            changed = copy.deepcopy(rows)
            changed[-1]['path'] = 'zzz'
            with self.assertRaises(self.m.Blocked) as raised:
                self.m.validate_targets(changed)
            self.assertEqual(raised.exception.reason, 'target_manifest_mismatch')

    def test_dependency_rejects_bytes_before_exec(self):
        with self.assertRaises(self.m.Blocked):
            self.m.load_legacy(b'raise RuntimeError("must not execute")')

    def test_native_inventory_does_not_reopen_exclusive_directory_by_path(self):
        script = self.m.POWERSHELL
        self.assertNotIn('Directory.EnumerateFileSystemEntries(', script)
        self.assertIn('DirectoryEntries(handles[p])', script)
        self.assertNotIn('ReOpenFile(', script)
        self.assertIn('CreateFile(@"\\\\?\\"+p,0x20080u|(directory?1u:0u)', script)
        self.assertIn('Identity(original)==Identity(h)', script)
        self.assertIn('GetFileInformationByHandleEx(h,first?11:10', script)

    def test_ancestor_reads_keep_target_generic_masks_strict(self):
        script = self.m.POWERSHELL
        self.assertIn('static SafeFileHandle OpenParent(string p)', script)
        self.assertEqual(script.count('foreach(var p in chain){var h=OpenParent(p);'), 2)
        self.assertIn('uint mask=ParentAccessMask(c.AccessMask)', script)
        target_aces = script.split('static List<Dictionary<string,object>> Aces(', 1)[1].split('static Dictionary<string,object> Row(', 1)[0]
        self.assertIn('c.AccessMask>=0', target_aces)
        self.assertIn('0xf0000000', target_aces)
        for case in ('fixture_parent_generic_read', 'fixture_parent_generic_all_reject',
                     'fixture_parent_inherit_only_generic', 'fixture_parent_callback_reject'):
            self.assertIn(case, script)

    def test_native_add_type_uses_verified_binary_command_without_manifest(self):
        script = self.m.POWERSHELL.split("using System;", 1)[0]
        self.assertNotIn('.psd1', script)
        self.assertNotIn('Microsoft.PowerShell.Utility\\Add-Type', script)
        self.assertIn('$global:bindingVerifiedModules', script)
        self.assertIn('Microsoft.PowerShell.Core\\Import-Module -Name $sourceUtilityPath -PassThru', script)
        self.assertIn("$sourceUtilityModule.ModuleType -eq [Management.Automation.ModuleType]::Binary", script)
        self.assertIn('$sourceAddType.ImplementingType.Assembly.FullName -ceq $sourceUtilityIdentity', script)
        self.assertIn('$sourceAddType.Module.Path', script)
        self.assertEqual(script.count('& $sourceAddType -'), 3)
        self.assertLess(script.index('$sourceAddType.ImplementingType.Assembly.FullName'),
                        script.index('& $sourceAddType -'))

    def test_exact_dependency_loads_without_reaching_native_or_transaction(self):
        raw = SCRIPT.with_name('windows-runtime-acl.py').read_bytes()
        legacy = self.m.load_legacy(raw)
        with (patch.object(legacy.NativeBackend, 'open', side_effect=AssertionError('legacy open')),
              patch.object(legacy.NativeBackend, 'native_self_test', side_effect=AssertionError('legacy fixture'))):
            self.assertEqual(legacy.__name__, '_reviewed_source_acl_dependency')

    def test_module_binding_receipt_requires_actual_binary_command_proof(self):
        proof = {'dll_direct_import': True, 'manifest_import_performed': False,
                 'command_binding_verified': True, 'command_module_name_sha256': '1' * 64,
                 'command_assembly_identity_sha256': self.m.hashlib.sha256(
                     ('windows-source-module-identity-v1\0' +
                      'Microsoft.PowerShell.Commands.Utility, Version=3.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35').encode()).hexdigest(),
                 'command_assembly_location_sha256': '2' * 64}
        self.m.validate_module_binding(proof)
        for key, value in [('manifest_import_performed', True), ('dll_direct_import', False),
                           ('command_binding_verified', False),
                           ('command_assembly_identity_sha256', '0' * 64),
                           ('command_module_name_sha256', 'raw-name')]:
            changed = dict(proof, **{key: value})
            with self.subTest(key=key), self.assertRaises(self.m.Blocked):
                self.m.validate_module_binding(changed)
        with self.assertRaises(self.m.Blocked):
            self.m.validate_module_binding(None)

    def test_fixture_receipt_requires_actual_adapter_fields_and_fresh_time(self):
        backend = object.__new__(self.m.SourceAclNativeBackend)
        backend.opened = False
        receipt = {'adapter_sha256': __import__('hashlib').sha256(self.m.POWERSHELL.encode()).hexdigest(),
                   'utc': '2000-01-01T00:00:00+00:00', 'self_pin_coexists': True, 'objects': 12}
        for key in ('exclusive_suppression', 'protected_flatten', 'future_inheritance',
                    'nested_rollback', 'partialmix', 'outside_preserved', 'cleanup', 'dpapi',
                    'descriptor_binding'):
            receipt[key] = True
        receipt['module_binding'] = {'dll_direct_import': True, 'manifest_import_performed': False,
            'command_binding_verified': True, 'command_module_name_sha256': '1' * 64,
            'command_assembly_identity_sha256': self.m.hashlib.sha256(('windows-source-module-identity-v1\0' +
                'Microsoft.PowerShell.Commands.Utility, Version=3.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35').encode()).hexdigest(),
            'command_assembly_location_sha256': '2' * 64}
        with self.assertRaises(self.m.Blocked) as raised:
            backend.attach_fixture_receipt(receipt, self.m.object_hash(receipt))
        self.assertEqual(raised.exception.reason, 'fixture_not_fresh')
        receipt['utc'] = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
        backend.attach_fixture_receipt(receipt, self.m.object_hash(receipt))
        receipt['descriptor_binding'] = False
        with self.assertRaises(self.m.Blocked):
            backend.attach_fixture_receipt(receipt, self.m.object_hash(receipt))


class MemoryVault:
    def __init__(self, module):
        self.m, self.records, self.events = module, {}, []

    def exists(self, name):
        return name in self.records

    def write(self, name, value, exclusive=False):
        if exclusive and self.exists(name):
            raise AssertionError('exclusive overwrite')
        self.records[name] = copy.deepcopy(value)
        self.events.append((name, copy.deepcopy(value)))

    def read(self, name):
        return copy.deepcopy(self.records[name])

    def read_bytes(self, name):
        return self.m.encoded(self.records[name])


class FakeBackend:
    def __init__(self, module, snapshot):
        self.m, self.actual = module, copy.deepcopy(snapshot)
        self.opened, self.calls, self.fail = False, [], None

    def open_readonly(self, scope):
        self.scope, self.opened = scope, True

    def check_guard(self):
        pass

    def snapshot(self):
        return copy.deepcopy(self.actual)

    def open_target_setters(self):
        self.calls.append('preflight')

    def project_steps(self, snapshot, targets):
        current, steps = copy.deepcopy(snapshot), []
        for row in targets:
            target = row['path']
            after = copy.deepcopy(current)
            after['entries'][target]['acl'] = self.m.transform_dacl(current['entries'][target]['acl'], {'trusted'})
            steps.append({'target': target, 'before_sha256': self.m.object_hash(current),
                          'after_sha256': self.m.object_hash(after), 'entry': after['entries'][target]})
            current = after
        return steps

    def set_dacl(self, target, expected, entry):
        self.calls.append(target)
        if self.fail == 'before':
            raise self.m.Blocked('native_acl_failure')
        self.actual['entries'][target] = copy.deepcopy(entry)
        if self.fail == 'unknown':
            raise self.m.UnknownWrite('native_response_unknown')
        return self.snapshot()


class SourceAclTransactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load()

    targets = SourceAclPolicyTests.targets
    acl = SourceAclPolicyTests.acl
    def setUp(self):
        self.rows = self.targets()
        self.pin = patch.object(self.m, 'TARGETS_SHA256', self.m.object_hash(self.rows))
        self.pin.start()
        self.addCleanup(self.pin.stop)
        entry = {'acl': self.acl(), 'owner': 'owner', 'size': 1, 'sd': 'dummy'}
        self.initial = {'entries': {r['path']: copy.deepcopy(entry) for r in self.rows},
                        'trusted': ['trusted'], 'ancestor_sha256': 'a'*64}
        self.initial['entries']['outside'] = copy.deepcopy(entry)
        self.scope = {'schema': self.m.SCHEMA, 'contract': self.m.CONTRACT,
                      'root': 'C:\\personal-server', 'targets': self.rows,
                      'approval_manifest_sha256': self.m.APPROVAL_MANIFEST_SHA256,
                      'source_sha256': 'b'*64, 'adapter_sha256': __import__('hashlib').sha256(self.m.POWERSHELL.encode()).hexdigest(),
                      'ci_sha256': 'c'*64, 'approval_sha256': 'd'*64, 'operation_id': 'e'*32,
                      'history_sha256': 'f'*64, 'runtime_hashes': {p: '1'*64 for p in self.m.RUNTIME}}
        self.backend = FakeBackend(self.m, self.initial)
        self.vault = MemoryVault(self.m)

    def prepare(self):
        transaction = self.m.SourceAclTransaction(self.backend, self.vault)
        result = transaction.prepare(self.scope)
        return transaction, result['plan_cipher_sha256']

    def test_plan_then_intent_before_single_setter_and_resume_rejected(self):
        txn, pin = self.prepare()
        def set_once(target, expected, entry):
            state = self.vault.read('state.dpapi')
            self.assertEqual(state['pending']['target'], target)
            self.assertFalse(state['pending']['response_complete'])
            self.backend.actual['entries'][target] = copy.deepcopy(entry)
            return self.backend.snapshot()
        self.backend.set_dacl = set_once
        self.assertEqual(txn.apply(pin)['completed'], 515)
        with self.assertRaises(self.m.Blocked):
            txn.apply(pin)

    def test_unknown_retains_intent_and_blocks_forward_and_rollback(self):
        txn, pin = self.prepare()
        self.backend.fail = 'unknown'
        with self.assertRaises(self.m.UnknownWrite):
            txn.apply(pin)
        state = self.vault.read('state.dpapi')
        self.assertTrue(state['unknown'])
        self.assertEqual(state['pending']['index'], 0)
        self.assertEqual(state['phase'], 'held')
        calls = len(self.backend.calls)
        for action in (txn.apply, txn.rollback):
            with self.assertRaises(self.m.Blocked):
                action(pin)
        self.assertEqual(calls, len(self.backend.calls))

    def test_preapply_drift_no_setter_and_held(self):
        txn, pin = self.prepare()
        self.backend.actual['entries']['outside']['size'] = 7
        with self.assertRaises(self.m.Blocked):
            txn.apply(pin)
        self.assertEqual(self.vault.read('state.dpapi')['phase'], 'held')
        self.assertEqual(self.backend.calls, ['preflight'])

    def test_foreign_partial_blocks_rollback_without_outside_repair(self):
        txn, pin = self.prepare()
        self.backend.fail = 'before'
        with self.assertRaises(self.m.Blocked):
            txn.apply(pin)
        self.backend.actual['entries']['outside']['size'] = 9
        calls = len(self.backend.calls)
        with self.assertRaises(self.m.Blocked):
            txn.rollback(pin)
        self.assertEqual(calls, len(self.backend.calls))

    def test_inverse_checkpoint_restart_preserves_owned_frontier(self):
        txn, pin = self.prepare()
        plan = self.vault.read('plan.dpapi')
        self.backend.actual = self.m.materialize(plan, 2)
        state = self.vault.read('state.dpapi')
        state.update(phase='held', completed=2, pending=None)
        self.vault.write('state.dpapi', state)
        calls = []
        def inverse(target, expected, entry):
            calls.append(target)
            if len(calls) == 2:
                raise self.m.Blocked('native_acl_failure')
            self.backend.actual['entries'][target] = copy.deepcopy(entry)
            return self.backend.snapshot()
        self.backend.set_dacl = inverse
        with self.assertRaises(self.m.Blocked):
            txn.rollback(pin)
        checkpoint = self.vault.read('state.dpapi')
        self.assertEqual(checkpoint['rollback_completed'], 1)
        self.assertTrue(checkpoint['pending']['response_complete'])
        fresh_backend = FakeBackend(self.m, self.backend.actual)
        fresh = self.m.SourceAclTransaction(fresh_backend, self.vault)
        self.assertEqual(fresh.rollback(pin)['phase'], 'rolled_back')
        self.assertEqual(fresh_backend.calls, ['preflight', 'dir001'])
        self.assertEqual(fresh_backend.actual, self.initial)

    def test_pending_and_failure_corruption_blocks_before_new_native_open(self):
        txn, pin = self.prepare()
        self.backend.opened = False
        self.backend.open_readonly = lambda scope: self.fail('native open before validation')
        original = self.vault.read('state.dpapi')
        bad = [dict(original, pending={'operation': 'apply', 'index': 515,
               'target': 'outside', 'response_complete': True}),
               dict(original, failures={'raw private value': 1}),
               dict(original, failures={'native_acl_failure': -1}),
               dict(original, failures={'native_acl_failure': 3})]
        for state in bad:
            self.vault.write('state.dpapi', state)
            with self.subTest(state=state['failures']), self.assertRaises(self.m.Blocked):
                txn.load(pin)


if __name__ == '__main__':
    unittest.main()
