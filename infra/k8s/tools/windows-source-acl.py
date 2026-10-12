#!/usr/bin/env python3
"""Exact approved source DACL reduction; trusted-memory dependency only.

No repository helper is imported. Credential contents are never persisted or
printed. SACL preservation is a DACL-only Win32 API contract, not a SACL audit.
The CLI is deliberately check-only; operational callers supply reviewed bytes,
the existing sequence lease, external guards, and a private DPAPI Vault.
"""
import copy
import hashlib
import json
import re
import types
from datetime import datetime, timezone

SCHEMA = 1
CONTRACT = 'source_acl_exact515_v1'
LEGACY_SHA256 = 'f897402773f555dd67f98201aaa142b341dd9180ddc2bb6a8ecf2018826bebe2'
APPROVAL_MANIFEST_SHA256 = 'af546be67373c1fdb8d55ab108fd8ed7b65f26a6a39b32a69757bb60bedc0e92'
TARGETS_SHA256 = 'ab9c74f2690b10cc768f2fb652d32f8f151086ef1f598c0720a5a7569206f8f0'
NATIVE_TARGETS_SHA256 = '6c1f499753023595683f5a4b4443fde39f1cd1464562b1bb2da1087ed8f57010'
UNSAFE_MASK = 0xD0156
RUNTIME = ('.env', 'docker-compose.yml', 'docker-compose.n100.yml',
           'docker-compose.portal-bridge.yml')
HASH = re.compile(r'[0-9a-f]{64}\Z')


class Blocked(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class UnknownWrite(Blocked):
    pass


def require(condition, reason):
    if not condition:
        raise Blocked(reason)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def object_hash(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def load_legacy(raw_bytes):
    """Only CI-reviewed bytes supplied by the trusted stdin launcher may execute."""
    require(type(raw_bytes) is bytes and hashlib.sha256(raw_bytes).hexdigest() == LEGACY_SHA256,
            'legacy_source_pin_mismatch')
    module = types.ModuleType('_reviewed_source_acl_dependency')
    module.__dict__['__name__'] = '_reviewed_source_acl_dependency'
    exec(compile(raw_bytes, '<reviewed-legacy-acl>', 'exec'), module.__dict__)
    return module


def validate_targets(rows):
    require(type(rows) is list and len(rows) == 515, 'exact_targets_required')
    paths = []
    for row in rows:
        require(type(row) is dict and set(row) == {'path', 'kind'}, 'invalid_target')
        path = row['path']
        require(type(path) is str and path and '\\' not in path and ':' not in path
                and '\x00' not in path and all(p not in ('', '.', '..') for p in path.split('/'))
                and all(not p.endswith((' ', '.')) for p in path.split('/')),
                'invalid_target_path')
        require(path.casefold() not in {p.casefold() for p in RUNTIME}
                and path.split('/')[0].casefold() != '.git', 'protected_target')
        require(row['kind'] in ('directory', 'file'), 'invalid_target_kind')
        paths.append(path)
    require(paths == sorted(paths) and len({p.casefold() for p in paths}) == 515,
            'target_alias_or_order')
    require(sum(r['kind'] == 'directory' for r in rows) == 58, 'target_kind_counts')
    require(object_hash(rows) == TARGETS_SHA256, 'target_manifest_mismatch')
    return paths


def validate_scope(scope):
    keys = {'schema', 'contract', 'root', 'targets', 'approval_manifest_sha256',
            'source_sha256', 'adapter_sha256', 'ci_sha256', 'approval_sha256',
            'operation_id', 'history_sha256', 'runtime_hashes'}
    require(type(scope) is dict and set(scope) == keys, 'invalid_scope')
    require(scope['schema'] == SCHEMA and scope['contract'] == CONTRACT
            and scope['root'] == 'C:\\personal-server', 'scope_contract_mismatch')
    require(scope['approval_manifest_sha256'] == APPROVAL_MANIFEST_SHA256,
            'approval_manifest_mismatch')
    validate_targets(scope['targets'])
    for key in ('source_sha256', 'adapter_sha256', 'ci_sha256', 'approval_sha256', 'history_sha256'):
        require(type(scope[key]) is str and HASH.fullmatch(scope[key]), 'scope_pin_missing')
    require(scope['adapter_sha256'] == hashlib.sha256(POWERSHELL.encode()).hexdigest(),
            'adapter_pin_mismatch')
    require(type(scope['operation_id']) is str and re.fullmatch('[0-9a-f]{32}', scope['operation_id']),
            'operation_pin_missing')
    require(type(scope['runtime_hashes']) is dict and set(scope['runtime_hashes']) == set(RUNTIME)
            and all(type(v) is str and HASH.fullmatch(v) for v in scope['runtime_hashes'].values()),
            'runtime_hash_pins_required')


def transform_dacl(acl, trusted):
    require(type(acl) is dict and set(acl) == {'protected', 'aces'}
            and type(acl['protected']) is bool and type(acl['aces']) is list and acl['aces'],
            'unsupported_dacl')
    result = {'protected': True, 'aces': []}
    allow_seen = False
    for ace in acl['aces']:
        require(type(ace) is dict and set(ace) == {'type', 'sid', 'mask', 'flags'}
                and ace['type'] in ('allow', 'deny') and type(ace['sid']) is str
                and type(ace['mask']) is int and 0 <= ace['mask'] < 0x10000000
                and type(ace['flags']) is int and not ace['flags'] & ~0x1f,
                'unsupported_ace')
        require(not (allow_seen and ace['type'] == 'deny'), 'flatten_noncanonical')
        allow_seen |= ace['type'] == 'allow'
        row = dict(ace, flags=ace['flags'] & ~16)
        if ace['type'] == 'allow' and ace['sid'] not in trusted:
            row['mask'] &= ~UNSAFE_MASK
        result['aces'].append(row)
    return result


def validate_projection(before, after, target, trusted, targets):
    require(target in validate_targets(targets), 'outside_target')
    require(type(before) is dict and type(after) is dict and set(before) == set(after),
            'projection_shape_changed')
    require({k: v for k, v in before.items() if k != 'entries'} ==
            {k: v for k, v in after.items() if k != 'entries'}, 'projection_binding_changed')
    require(set(before['entries']) == set(after['entries']) and target in before['entries'],
            'projection_inventory_changed')
    for path, original in before['entries'].items():
        actual = after['entries'][path]
        if path != target:
            require(actual == original, 'outside_projection_changed')
            continue
        require({k: v for k, v in actual.items() if k not in ('acl', 'sd')} ==
                {k: v for k, v in original.items() if k not in ('acl', 'sd')},
                'target_metadata_changed')
        require(actual['acl'] == transform_dacl(original['acl'], trusted),
                'target_policy_mismatch')


def own_partial(actual, before, after):
    if set(actual) != set(before) or set(before) != set(after):
        return False
    if any(actual[k] != before[k] or before[k] != after[k] for k in before if k != 'entries'):
        return False
    if set(actual['entries']) != set(before['entries']) or set(before['entries']) != set(after['entries']):
        return False
    return all(value == before['entries'][path] or value == after['entries'][path]
               for path, value in actual['entries'].items())


class SourceAclNativeBackend:
    """Compose the reviewed transport; no legacy filesystem entry point is used."""
    scope_schema = SCHEMA

    def __init__(self, legacy, lock_fd, *, guard, timeout=180):
        require(callable(guard), 'operational_guard_required')
        self.legacy = legacy
        self.transport = legacy.NativeBackend(lock_fd, guard=None, timeout=timeout)
        self.transport.script = POWERSHELL
        self.guard, self.scope, self.receipt = guard, None, None
        self.opened = False

    def request(self, payload, mutation=False):
        require(payload.get('op') in ('compile', 'fixture', 'open', 'snapshot', 'project', 'preflight', 'set'),
                'native_operation_forbidden')
        try:
            return self.transport.request(payload, mutation=mutation)
        except self.legacy.UnknownWrite as exc:
            raise UnknownWrite(exc.reason) from None
        except self.legacy.Blocked as exc:
            raise Blocked(exc.reason) from None

    def check_guard(self):
        proof = self.guard(self.scope)
        require(type(proof) is dict and all(proof.get(k) is True for k in
                ('source_pin', 'ci_verified', 'sequence_lock', 'writers_idle',
                 'root_git_binary_runtime_trust', 'old_records_preserved')),
                'trusted_operational_guard_required')

    def native_self_test(self):
        require(not self.opened, 'fixture_before_open_required')
        nonce = __import__('os').urandom(32).hex()
        result = self.request({'op': 'fixture', 'nonce': nonce}, mutation=True)
        fields = ('exclusive_suppression', 'protected_flatten', 'future_inheritance',
                  'nested_rollback', 'partialmix', 'outside_preserved', 'cleanup', 'dpapi',
                  'descriptor_binding')
        require(result.get('nonce') == nonce and all(result.get(k) is True for k in fields),
                'owned_fixture_failed')
        stamp = datetime.fromisoformat(result.get('utc', '').replace('Z', '+00:00'))
        require(stamp.tzinfo is not None and abs((datetime.now(timezone.utc)-stamp).total_seconds()) < 300,
                'fixture_not_fresh')
        result['adapter_sha256'] = hashlib.sha256(POWERSHELL.encode()).hexdigest()
        self.receipt = result
        return {'reason': 'owned_fixture_passed', 'adapter_sha256': hashlib.sha256(POWERSHELL.encode()).hexdigest(),
                'receipt_sha256': object_hash(result)}

    def attach_fixture_receipt(self, receipt, expected_receipt_sha256):
        """Accept only an independently pinned actual receipt, never a PASS flag."""
        require(not self.opened and object_hash(receipt) == expected_receipt_sha256,
                'fixture_receipt_pin_mismatch')
        fields = ('exclusive_suppression', 'protected_flatten', 'future_inheritance',
                  'nested_rollback', 'partialmix', 'outside_preserved', 'cleanup',
                  'dpapi', 'descriptor_binding')
        require(type(receipt) is dict and receipt.get('adapter_sha256') == hashlib.sha256(POWERSHELL.encode()).hexdigest()
                and all(receipt.get(k) is True for k in fields)
                and type(receipt.get('self_pin_coexists')) is bool
                and type(receipt.get('objects')) is int and 0 < receipt['objects'] <= 100,
                'invalid_fixture_receipt')
        stamp = datetime.fromisoformat(receipt.get('utc', '').replace('Z', '+00:00'))
        require(stamp.tzinfo is not None and abs((datetime.now(timezone.utc)-stamp).total_seconds()) < 300,
                'fixture_not_fresh')
        self.receipt = copy.deepcopy(receipt)

    def open_readonly(self, scope):
        validate_scope(scope)
        require(not self.opened, 'native_already_open')
        self.scope = copy.deepcopy(scope)
        self.check_guard()
        require(self.receipt is not None, 'owned_fixture_required')
        self.attach_fixture_receipt(self.receipt, object_hash(self.receipt))
        opened = self.request({'op': 'open', 'root': scope['root'],
                               'paths': [r['path'] for r in scope['targets']],
                               'kinds': [r['kind'] for r in scope['targets']]})
        require(HASH.fullmatch(opened.get('ancestor_sha256', '')), 'ancestor_pin_missing')
        self.ancestor_sha256 = opened['ancestor_sha256']
        self.opened = True

    def snapshot(self):
        require(self.opened, 'native_not_open')
        result = self.request({'op': 'snapshot'})
        require(result['ancestor_sha256'] == self.ancestor_sha256, 'ancestor_changed')
        require(set(result['trusted']) == {result['principal'], 'S-1-5-18', 'S-1-5-32-544'},
                'native_trusted_identity_changed')
        return result

    def project_steps(self, snapshot, targets):
        require(targets == self.scope['targets'], 'projection_targets_changed')
        current, steps = copy.deepcopy(snapshot), []
        for row in targets:
            target = row['path']
            projected = self.request({'op': 'project', 'snapshot': {'entries': {
                target: current['entries'][target]}}, 'target': target})
            after = copy.deepcopy(current)
            after['entries'][target] = projected['entries'][target]
            validate_projection(current, after, target, set(snapshot['trusted']), targets)
            steps.append({'target': target, 'before_sha256': object_hash(current),
                          'after_sha256': object_hash(after), 'entry': after['entries'][target]})
            current = after
        return steps

    def open_target_setters(self):
        self.check_guard()
        require(self.request({'op': 'preflight'}).get('preflight') is True, 'setter_preflight_failed')

    def set_dacl(self, target, expected, entry):
        require(target in validate_targets(self.scope['targets']), 'outside_setter')
        self.check_guard()
        return self.request({'op': 'set', 'target': target, 'expected': expected,
                             'entry': entry}, mutation=True)

    def encrypt(self, data):
        return self.transport.encrypt(data)

    def decrypt(self, data):
        return self.transport.decrypt(data)

    def close(self):
        # The legacy lifecycle retains the caller's lease until actual Windows
        # termination. Do not kill a WSL process and infer Windows termination.
        return self.transport.close()


def materialize(plan, count):
    require(type(count) is int and 0 <= count <= len(plan['steps']), 'invalid_frontier')
    result = copy.deepcopy(plan['initial'])
    for step in plan['steps'][:count]:
        result['entries'][step['target']] = copy.deepcopy(step['entry'])
    return result


def validate_plan(plan):
    require(type(plan) is dict and set(plan) == {'schema', 'contract', 'scope', 'initial', 'steps', 'final_sha256'}
            and plan['schema'] == SCHEMA and plan['contract'] == CONTRACT, 'plan_schema_mismatch')
    validate_scope(plan['scope'])
    require([s['target'] for s in plan['steps']] == validate_targets(plan['scope']['targets']),
            'plan_targets_changed')
    current = copy.deepcopy(plan['initial'])
    trusted = set(current['trusted'])
    for step in plan['steps']:
        require(set(step) == {'target', 'before_sha256', 'after_sha256', 'entry'}
                and step['before_sha256'] == object_hash(current), 'plan_chain_mismatch')
        after = copy.deepcopy(current)
        after['entries'][step['target']] = copy.deepcopy(step['entry'])
        validate_projection(current, after, step['target'], trusted, plan['scope']['targets'])
        require(object_hash(after) == step['after_sha256'], 'plan_chain_mismatch')
        current = after
    require(object_hash(current) == plan['final_sha256'], 'plan_final_mismatch')


class SourceAclTransaction:
    def __init__(self, backend, vault):
        self.backend, self.vault = backend, vault

    def prepare(self, scope):
        validate_scope(scope)
        require(not self.vault.exists('plan.dpapi') and not self.vault.exists('state.dpapi'),
                'operation_already_prepared')
        if not self.backend.opened:
            self.backend.open_readonly(scope)
        self.backend.check_guard()
        initial = self.backend.snapshot()
        self.backend.open_target_setters()
        require(self.backend.snapshot() == initial, 'prepare_drift')
        steps = self.backend.project_steps(initial, scope['targets'])
        plan = {'schema': SCHEMA, 'contract': CONTRACT, 'scope': copy.deepcopy(scope),
                'initial': initial, 'steps': steps, 'final_sha256': steps[-1]['after_sha256']}
        validate_plan(plan)
        self.backend.check_guard()
        require(self.backend.snapshot() == initial, 'prepare_drift')
        self.vault.write('plan.dpapi', plan, exclusive=True)
        pin = hashlib.sha256(self.vault.read_bytes('plan.dpapi')).hexdigest()
        state = {'schema': SCHEMA, 'plan_cipher_sha256': pin, 'phase': 'prepared',
                 'completed': 0, 'rollback_completed': 0, 'pending': None, 'unknown': False, 'failures': {}}
        self.vault.write('state.dpapi', state, exclusive=True)
        return {'phase': 'prepared', 'plan_cipher_sha256': pin, 'targets': 515,
                'initial_sha256': object_hash(initial), 'final_sha256': plan['final_sha256']}

    def load(self, reviewed_sha):
        require(type(reviewed_sha) is str and HASH.fullmatch(reviewed_sha)
                and hashlib.sha256(self.vault.read_bytes('plan.dpapi')).hexdigest() == reviewed_sha,
                'reviewed_plan_pin_mismatch')
        plan, state = self.vault.read('plan.dpapi'), self.vault.read('state.dpapi')
        validate_plan(plan)
        require(type(state) is dict and set(state) == {'schema', 'plan_cipher_sha256', 'phase',
                'completed', 'rollback_completed', 'pending', 'unknown', 'failures'} and state['schema'] == SCHEMA
                and state['plan_cipher_sha256'] == reviewed_sha and type(state['completed']) is int
                and 0 <= state['completed'] <= 515 and type(state['unknown']) is bool
                and state['phase'] in ('prepared', 'applying', 'applied', 'held', 'rolling_back', 'rolled_back')
                and type(state['rollback_completed']) is int and 0 <= state['rollback_completed'] <= 515
                and type(state['failures']) is dict, 'invalid_state')
        validate_pending(state['pending'], plan)
        reasons = {'native_acl_failure', 'native_adapter_unavailable', 'native_response_unknown',
                   'native_process_held', 'native_process_termination_unknown', 'native_process_failed',
                   'trusted_operational_guard_required', 'preapply_drift', 'frontier_drift',
                   'postwrite_drift', 'final_drift', 'rollback_postwrite_drift', 'rollback_incomplete',
                   'setter_preflight_failed', 'controller_failure'}
        require(all(k in reasons and type(v) is int and v >= 0 for k, v in state['failures'].items()),
                'invalid_failure_history')
        require(sum(state['failures'].values()) < 3, 'failure_limit_reached')
        return plan, state

    def open_plan(self, plan):
        if not self.backend.opened:
            self.backend.open_readonly(plan['scope'])
        require(self.backend.scope == plan['scope'], 'backend_scope_changed')
        self.backend.check_guard()

    def hold(self, state, error):
        state['phase'] = 'held'
        state['unknown'] |= isinstance(error, UnknownWrite)
        # Reasons are fixed tool/native enums; arbitrary exception messages never
        # enter the durable record or stdout.
        reason = error.reason if isinstance(error, Blocked) else 'controller_failure'
        if reason not in {'native_acl_failure', 'native_adapter_unavailable', 'native_response_unknown',
                          'native_process_held', 'native_process_termination_unknown', 'native_process_failed',
                          'trusted_operational_guard_required', 'preapply_drift', 'frontier_drift',
                          'postwrite_drift', 'final_drift', 'rollback_postwrite_drift', 'rollback_incomplete',
                          'setter_preflight_failed'}:
            reason = 'controller_failure'
        state['failures'][reason] = state['failures'].get(reason, 0) + 1
        self.vault.write('state.dpapi', state)

    def apply(self, reviewed_sha):
        plan, state = self.load(reviewed_sha)
        require(state['phase'] == 'prepared' and state['completed'] == 0
                and state['pending'] is None and not state['unknown'], 'forward_resume_forbidden')
        try:
            self.open_plan(plan)
            current = self.backend.snapshot()
            require(current == plan['initial'], 'preapply_drift')
            self.backend.open_target_setters()
            for index, step in enumerate(plan['steps']):
                require(object_hash(current) == step['before_sha256'], 'frontier_drift')
                state.update(phase='applying', pending={'operation': 'apply', 'index': index,
                             'target': step['target'], 'response_complete': False})
                self.vault.write('state.dpapi', state)
                try:
                    actual = self.backend.set_dacl(step['target'], current, step['entry'])
                except Blocked as error:
                    if not isinstance(error, UnknownWrite):
                        state['pending']['response_complete'] = True
                    raise
                state['pending']['response_complete'] = True
                self.vault.write('state.dpapi', state)
                after = copy.deepcopy(current)
                after['entries'][step['target']] = copy.deepcopy(step['entry'])
                require(actual == after, 'postwrite_drift')
                current = actual
                state.update(completed=index+1, pending=None)
                self.vault.write('state.dpapi', state)
            self.backend.check_guard()
            require(self.backend.snapshot() == current, 'final_drift')
            state['phase'] = 'applied'
            self.vault.write('state.dpapi', state)
            return {'phase': 'applied', 'completed': 515, 'unknown': False,
                    'final_sha256': object_hash(current)}
        except BaseException as error:
            self.hold(state, error)
            raise

    def status(self, reviewed_sha):
        plan, state = self.load(reviewed_sha)
        require(not state['unknown'], 'unknown_response_held')
        self.open_plan(plan)
        actual = self.backend.snapshot()
        return {'phase': state['phase'], 'completed': state['completed'],
                'pending': state['pending'] is not None, 'unknown': state['unknown'],
                'current_sha256': object_hash(actual), 'initial_match': actual == plan['initial'],
                'final_match': object_hash(actual) == plan['final_sha256']}

    def rollback(self, reviewed_sha):
        plan, state = self.load(reviewed_sha)
        require(not state['unknown'], 'unknown_response_held')
        require(state['phase'] in ('held', 'applied', 'rolling_back'), 'rollback_not_available')
        pending = state['pending']
        require(pending is None or pending.get('response_complete') is True, 'unresolved_intent_held')
        self.open_plan(plan)
        # Rollback is an explicitly requested operation, never an apply catch.
        # Only exact original/approved rows are owned. Restore parent first;
        # UNPROTECTED children are restored against their original parent state.
        actual = self.backend.snapshot()
        frontier = state['completed']
        provenance = pending
        while provenance:
            if provenance['operation'] == 'apply':
                frontier = max(frontier, provenance['index']+1)
            provenance = provenance.get('provenance')
        original = plan['initial']
        require(own_partial(actual, original, materialize(plan, frontier)), 'rollback_ownership_mismatch')
        try:
            self.backend.open_target_setters()
            for index, step in enumerate(plan['steps']):
                target = step['target']
                if actual['entries'][target] == original['entries'][target]:
                    continue
                # Preserve existing pending provenance through durable inverse
                # intent. No intermediate pending=None checkpoint exists.
                prior = state['pending']
                provenance = copy.deepcopy(prior.get('provenance') if prior and
                                           prior['operation'] == 'rollback' else prior)
                state.update(phase='rolling_back', pending={'operation': 'rollback', 'index': index,
                    'target': target, 'response_complete': False, 'provenance': provenance})
                self.vault.write('state.dpapi', state)
                try:
                    restored = self.backend.set_dacl(target, actual, original['entries'][target])
                except Blocked as error:
                    if not isinstance(error, UnknownWrite):
                        state['pending']['response_complete'] = True
                    raise
                state['pending']['response_complete'] = True
                self.vault.write('state.dpapi', state)
                expected = copy.deepcopy(actual)
                expected['entries'][target] = copy.deepcopy(original['entries'][target])
                require(restored == expected, 'rollback_postwrite_drift')
                actual = restored
                state['rollback_completed'] += 1
                self.vault.write('state.dpapi', state)
            require(actual == original, 'rollback_incomplete')
            self.backend.check_guard()
            state.update(phase='rolled_back', completed=0, pending=None)
            self.vault.write('state.dpapi', state)
            return {'phase': 'rolled_back', 'completed': 0}
        except BaseException as error:
            self.hold(state, error)
            raise


def validate_pending(pending, plan, depth=0):
    if pending is None:
        return
    require(type(pending) is dict and depth <= 515 and pending.get('operation') in ('apply', 'rollback'),
            'invalid_pending')
    keys = {'operation', 'index', 'target', 'response_complete'}
    if pending['operation'] == 'rollback':
        keys.add('provenance')
    require(set(pending) == keys and type(pending['index']) is int and 0 <= pending['index'] < 515
            and pending['target'] == plan['steps'][pending['index']]['target']
            and type(pending['response_complete']) is bool, 'invalid_pending')
    if pending['operation'] == 'rollback':
        validate_pending(pending['provenance'], plan, depth+1)


# Filled by the separately reviewed, fixed Win32 adapter below. No script is
# obtained from the target repository or caller-controlled file paths.
POWERSHELL = r'''
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$PSModuleAutoLoadingPreference='None'
Microsoft.PowerShell.Core\Import-Module -Name ($PSHOME+'\Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1') -Force
Microsoft.PowerShell.Utility\Add-Type -AssemblyName System.Web.Extensions
Microsoft.PowerShell.Utility\Add-Type -AssemblyName System.Security
Microsoft.PowerShell.Utility\Add-Type -ReferencedAssemblies @('System.dll','System.Core.dll','System.Security.dll','System.Web.Extensions.dll') -TypeDefinition @'
using System;
using System.IO;
using System.Linq;
using System.Collections;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Security.Cryptography;
using System.Web.Script.Serialization;
using Microsoft.Win32.SafeHandles;
public static class SourceAcl {
 [StructLayout(LayoutKind.Sequential)] struct Info {
  public uint Attr,CL,CH,AL,AH,ML,MH,Volume,SizeH,SizeL,Links,IndexH,IndexL;
 }
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
 static extern SafeFileHandle CreateFile(string p,uint access,uint share,IntPtr sa,uint creation,uint flags,IntPtr template);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetFileInformationByHandle(SafeFileHandle h,out Info i);
 [DllImport("kernel32.dll",SetLastError=true)] static extern SafeFileHandle ReOpenFile(SafeFileHandle h,uint access,uint share,uint flags);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetFileInformationByHandleEx(SafeFileHandle h,int kind,IntPtr data,uint length);
 [DllImport("advapi32.dll")] static extern uint GetSecurityInfo(SafeFileHandle h,int type,uint flags,out IntPtr o,out IntPtr g,out IntPtr d,out IntPtr s,out IntPtr sd);
 [DllImport("advapi32.dll")] static extern uint SetSecurityInfo(SafeFileHandle h,int type,uint flags,IntPtr o,IntPtr g,IntPtr d,IntPtr s);
 [DllImport("advapi32.dll")] static extern uint GetSecurityDescriptorLength(IntPtr p);
 [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr p);
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool CreateDirectory(string p,IntPtr security);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool SetFileInformationByHandle(SafeFileHandle h,int kind,ref int data,uint length);
 static JavaScriptSerializer json=new JavaScriptSerializer {MaxJsonLength=64*1024*1024};
 static Dictionary<string,SafeFileHandle> handles=new Dictionary<string,SafeFileHandle>(StringComparer.Ordinal);
 static List<SafeFileHandle> ancestors=new List<SafeFileHandle>();
 static List<string> ancestorPins=new List<string>();
 static HashSet<string> targets,trees;
 static string root,principal,exclusive;
 public static string Stage="idle";
 public static string PathHash=null;
 static void Check(bool ok,string stage,string path=null) {
  if(!ok){Stage=stage;PathHash=path==null?null:Hash(System.Text.Encoding.UTF8.GetBytes(path));throw new Exception("native_acl_failure");}
 }
 static string Hash(byte[] b){using(var sha=SHA256.Create())return String.Concat(sha.ComputeHash(b).Select(x=>x.ToString("x2")));}
 static object Sort(object o) {
  var dict=o as IDictionary<string,object>;if(dict!=null){var result=new SortedDictionary<string,object>(StringComparer.Ordinal);foreach(var p in dict)result.Add(p.Key,Sort(p.Value));return result;}
  var list=o as IEnumerable;if(list!=null&&!(o is string)){var result=new List<object>();foreach(var v in list)result.Add(Sort(v));return result;}return o;
 }
 static string Canon(object o){return json.Serialize(Sort(o));}
 static bool Equal(object a,object b){return Canon(a)==Canon(b);}
 static string Full(string p){return p.Length==0?root:Path.Combine(root,p.Replace('/','\\'));}
 static bool Trusted(string sid){return sid==principal||sid=="S-1-5-18"||sid=="S-1-5-32-544";}
 static bool ParentOwner(string sid){return Trusted(sid)||sid=="S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464";}
 static Info Information(SafeFileHandle h){Info i;Check(GetFileInformationByHandle(h,out i),"file_information");Check((i.Attr&1024)==0,"reparse");return i;}
 static string Identity(SafeFileHandle h){var i=Information(h);return i.Volume.ToString("x8")+":"+i.IndexH.ToString("x8")+i.IndexL.ToString("x8");}
 static SafeFileHandle Open(string p,bool write=false) {
  // Establish kind on a metadata-only handle. ReOpenFile remains bound to that
  // same object; LIST is requested only for directories, never as file READ_DATA.
  var original=CreateFile(@"\\?\"+p,0x20080u,3u,IntPtr.Zero,3,0x02200000,IntPtr.Zero);
  if(original.IsInvalid){original.Dispose();Check(false,"read_open");}
  SafeFileHandle h=null;
  try{var old=Information(original);bool directory=(old.Attr&16)!=0;
   h=ReOpenFile(original,0x20080u|(directory?1u:0u)|(write?0x40000u:0u),write?0u:3u,0x02200000);
   Check(!h.IsInvalid,write?"exclusive_open":"read_reopen");var current=Information(h);
   Check(Identity(original)==Identity(h)&&((current.Attr&16)!=0)==directory&&current.Links==old.Links,"reopen_identity_or_kind");
   var result=h;h=null;return result;
  }finally{if(h!=null)h.Dispose();original.Dispose();}
 }
 static byte[] Bytes(RawSecurityDescriptor sd){var b=new byte[sd.BinaryLength];sd.GetBinaryForm(b,0);return b;}
 static RawSecurityDescriptor From(string b){Check(!String.IsNullOrEmpty(b),"invalid_descriptor");return new RawSecurityDescriptor(Convert.FromBase64String(b),0);}
 static RawSecurityDescriptor Descriptor(SafeFileHandle h){
  IntPtr o,g,d,s,p;Check(GetSecurityInfo(h,1,7,out o,out g,out d,out s,out p)==0,"get_security");
  try{var b=new byte[GetSecurityDescriptorLength(p)];Marshal.Copy(p,b,0,b.Length);return new RawSecurityDescriptor(b,0);}finally{LocalFree(p);}
 }
 static List<Dictionary<string,object>> Aces(RawAcl acl) {
  Check(acl!=null&&acl.Count>0,"null_dacl");var rows=new List<Dictionary<string,object>>();
  foreach(GenericAce a in acl){var c=a as CommonAce;Check(c!=null&&!c.IsCallback&&c.OpaqueLength==0,"unsupported_ace");
   Check(c.AceQualifier==AceQualifier.AccessAllowed||c.AceQualifier==AceQualifier.AccessDenied,"unsupported_ace");
   Check(c.AccessMask>=0&&(c.AccessMask&unchecked((int)0xf0000000))==0&&((int)c.AceFlags&~31)==0,"unsupported_mask");
   rows.Add(new Dictionary<string,object>{{"type",c.AceQualifier==AceQualifier.AccessAllowed?"allow":"deny"},{"sid",c.SecurityIdentifier.Value},{"mask",c.AccessMask},{"flags",(int)c.AceFlags}});
  }return rows;
 }
 static Dictionary<string,object> Row(SafeFileHandle h){
  var i=Information(h);var sd=Descriptor(h);
  return new Dictionary<string,object>{{"identity",Identity(h)},{"links",i.Links},{"kind",(i.Attr&16)!=0?"directory":"file"},
   {"attributes",i.Attr},{"created",((ulong)i.CH<<32)|i.CL},{"modified",((ulong)i.MH<<32)|i.ML},{"size",((ulong)i.SizeH<<32)|i.SizeL},
   {"owner",sd.Owner.Value},{"group",sd.Group.Value},{"sd",Convert.ToBase64String(Bytes(sd))},
   {"acl",new Dictionary<string,object>{{"protected",(sd.ControlFlags&ControlFlags.DiscretionaryAclProtected)!=0},{"aces",Aces(sd.DiscretionaryAcl)}}}};
 }
 static void SafeParentDescriptor(RawSecurityDescriptor sd){
  Check(ParentOwner(sd.Owner.Value),"ancestor_owner");Aces(sd.DiscretionaryAcl);
  foreach(CommonAce a in sd.DiscretionaryAcl)Check(a.AceQualifier!=AceQualifier.AccessAllowed||(a.AceFlags&AceFlags.InheritOnly)!=0||ParentOwner(a.SecurityIdentifier.Value)||(a.AccessMask&0xd0040)==0,"ancestor_write");
 }
 static void SafeParent(SafeFileHandle h){SafeParentDescriptor(Descriptor(h));}
 static string ParentPin(SafeFileHandle h){SafeParent(h);return Identity(h)+":"+Hash(Bytes(Descriptor(h)));}
 static void VerifyParents(){for(int i=0;i<ancestors.Count;i++)Check(ParentPin(ancestors[i])==ancestorPins[i],"ancestor_drift");}
 public static string AncestorHash {get{return Hash(System.Text.Encoding.UTF8.GetBytes(String.Join("|",ancestorPins)));}}
 static void Pin(string p){
  if(handles.ContainsKey(p))return;Check(handles.Count<10000,"inventory_limit");
  if(p.Length>0){var index=p.LastIndexOf('/');Pin(index<0?"":p.Substring(0,index));}
  var h=Open(Full(p));try{Check((Information(h).Attr&16)!=0||Information(h).Links==1,"hardlink");
   Check(!handles.Keys.Any(x=>String.Equals(x,p,StringComparison.OrdinalIgnoreCase)),"case_alias");
   Check(!handles.Values.Any(x=>Identity(x)==Identity(h)),"identity_alias");handles.Add(p,h);}catch{h.Dispose();throw;}
 }
 sealed class DirectoryEntry {public string Name;public ulong Id;public uint Attributes;}
 static int directoryPages;
 static List<DirectoryEntry> DirectoryBuffer(IntPtr buffer,int length){
  var rows=new List<DirectoryEntry>();int offset=0;
  while(true){
   Check(offset>=0&&offset<=length-104,"directory_record_bounds");
   uint next=unchecked((uint)Marshal.ReadInt32(buffer,offset));
   uint bytes=unchecked((uint)Marshal.ReadInt32(buffer,offset+60));
   Check(bytes>0&&(bytes&1)==0&&bytes<=510&&bytes<=(uint)(length-offset-104),"directory_name_bounds");
   Check(next==0||(next%8==0&&next>=104+bytes&&next<=(uint)(length-offset-104)),"directory_next_bounds");
   string name=Marshal.PtrToStringUni(IntPtr.Add(buffer,offset+104),(int)bytes/2);
   Check(name.IndexOfAny(new[]{'\0','/','\\',':'})<0&&!name.Any(c=>c<32),"directory_name");
   new System.Text.UTF8Encoding(false,true).GetBytes(name);
   if(name!="."&&name!=".."){
    Check(!name.EndsWith(".")&&!name.EndsWith(" "),"directory_name_alias");
    uint attributes=unchecked((uint)Marshal.ReadInt32(buffer,offset+56));
    Check((attributes&1024)==0,"reparse");
    ulong id=unchecked((ulong)Marshal.ReadInt64(buffer,offset+96));Check(id!=0,"directory_file_id");
    rows.Add(new DirectoryEntry{Name=name,Id=id,Attributes=attributes});
   }
   if(next==0)break;offset+=(int)next;
  }return rows;
 }
 static List<DirectoryEntry> DirectoryEntries(SafeFileHandle h){
  Check((Information(h).Attr&16)!=0,"directory_required");
  const int length=4096;var rows=new List<DirectoryEntry>();var names=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
  IntPtr buffer=Marshal.AllocHGlobal(length);
  try{bool first=true;for(int page=0;page<10000;page++){
    Marshal.Copy(new byte[length],0,buffer,length);
    bool ok=GetFileInformationByHandleEx(h,first?11:10,buffer,length);first=false;
    if(!ok){Check(Marshal.GetLastWin32Error()==18,"directory_enumeration");return rows.OrderBy(x=>x.Name,StringComparer.Ordinal).ToList();}
    directoryPages++;
    foreach(var row in DirectoryBuffer(buffer,length)){Check(rows.Count<10000&&names.Add(row.Name),"directory_alias_or_limit");rows.Add(row);}
   }Check(false,"directory_page_limit");return null;
  }finally{Marshal.FreeHGlobal(buffer);}
 }
 static void Walk(string p,bool initial,HashSet<string> found){
  Check(handles.ContainsKey(p)&&(Information(handles[p]).Attr&16)!=0,"directory_required",p);
  var parent=Information(handles[p]);
  foreach(var entry in DirectoryEntries(handles[p])){
   string rel=p.Length==0?entry.Name:p+"/"+entry.Name;string path=Full(rel);
   if(initial)Pin(rel);else Check(handles.ContainsKey(rel),"inventory_new",rel);
   Check(found.Add(rel),"inventory_alias",rel);
   if(rel!=exclusive)using(var fresh=Open(path))Check(Identity(fresh)==Identity(handles[rel]),"identity_drift",rel);
   var child=Information(handles[rel]);
   Check(child.Volume==parent.Volume&&(((ulong)child.IndexH<<32)|child.IndexL)==entry.Id&&((child.Attr^entry.Attributes)&16)==0,"enumerated_identity_or_kind",rel);
   if((child.Attr&16)!=0)Walk(rel,initial,found);
  }
 }
 static void Inventory(bool initial){
  var found=new HashSet<string>(StringComparer.Ordinal);
  foreach(var p in trees.OrderBy(x=>x,StringComparer.Ordinal))Walk(p,initial,found);
  if(!initial){foreach(var p in handles.Keys){if(p!=exclusive)using(var fresh=Open(Full(p)))Check(Identity(fresh)==Identity(handles[p]),"identity_drift",p);}
   var descendants=new HashSet<string>(handles.Keys.Where(p=>trees.Any(t=>p.StartsWith(t+"/",StringComparison.Ordinal))),StringComparer.Ordinal);
   Check(descendants.SetEquals(found),"inventory_deleted");}
 }
 public static void Start(string r,string[] paths,string[] kinds,bool fixture=false){
  Check(handles.Count==0&&ancestors.Count==0,"already_open");root=r.TrimEnd('\\');principal=WindowsIdentity.GetCurrent().User.Value;
  Check(principal!="S-1-5-18"&&principal!="S-1-5-32-544","unsupported_principal");
  Check(fixture||root==@"C:\personal-server","root_mismatch");Check(paths.Length==kinds.Length,"target_shape");
  if(!fixture){
   Check(paths.Length==515&&kinds.Count(x=>x=="directory")==58&&kinds.Count(x=>x=="file")==457,"native_manifest_count");
   Check(paths.SequenceEqual(paths.OrderBy(x=>x,StringComparer.Ordinal)),"native_manifest_order");
   string manifest="source-acl-targets-v1\0"+String.Concat(paths.Select((p,i)=>p+"\0"+kinds[i]+"\n"));
   Check(Hash(System.Text.Encoding.UTF8.GetBytes(manifest))=="6c1f499753023595683f5a4b4443fde39f1cd1464562b1bb2da1087ed8f57010","native_manifest_pin");
  }
  targets=new HashSet<string>(paths,StringComparer.Ordinal);Check(targets.Count==paths.Length,"target_alias");
  var dirs=paths.Where((p,i)=>kinds[i]=="directory").ToArray();
  trees=new HashSet<string>(dirs.Where(p=>!dirs.Any(q=>q!=p&&p.StartsWith(q+"/",StringComparison.Ordinal))),StringComparer.Ordinal);
  // Pin parent chain BEFORE any traversal. Every directory is opened with
  // OPEN_REPARSE_POINT before its descendants are enumerated.
  var chain=new List<string>();var parent=Path.GetDirectoryName(root);
  while(!String.IsNullOrEmpty(parent)){chain.Add(parent);var next=Path.GetDirectoryName(parent);if(next==parent)break;parent=next;}
  chain.Reverse();foreach(var p in chain){var h=Open(p);try{ancestorPins.Add(ParentPin(h));ancestors.Add(h);}catch{h.Dispose();throw;}}
  Pin("");SafeParent(handles[""]);
  if(!fixture)foreach(var p in new[]{".env","docker-compose.yml","docker-compose.n100.yml","docker-compose.portal-bridge.yml"})Pin(p);
  foreach(var p in paths)Pin(p);Inventory(true);Inventory(false);
  for(int i=0;i<paths.Length;i++)Check((string)Row(handles[paths[i]])["kind"]==kinds[i],"target_kind",paths[i]);
 }
 public static Dictionary<string,object> Snapshot(){
  VerifyParents();Inventory(false);SafeParent(handles[""]);
  var rows=new SortedDictionary<string,object>(StringComparer.Ordinal);foreach(var p in handles.Keys.OrderBy(x=>x,StringComparer.Ordinal))rows.Add(p,Row(handles[p]));
  return new Dictionary<string,object>{{"entries",rows},{"principal",principal},{"trusted",new[]{principal,"S-1-5-18","S-1-5-32-544"}},{"ancestor_sha256",AncestorHash}};
 }
 public static string Reduce(string original){
  var sd=From(original);Aces(sd.DiscretionaryAcl);var acl=new RawAcl(sd.DiscretionaryAcl.Revision,sd.DiscretionaryAcl.Count);bool allow=false;
  foreach(CommonAce a in sd.DiscretionaryAcl){Check(!(allow&&a.AceQualifier==AceQualifier.AccessDenied),"flatten_noncanonical");allow|=a.AceQualifier==AceQualifier.AccessAllowed;
   int mask=a.AccessMask;if(a.AceQualifier==AceQualifier.AccessAllowed&&!Trusted(a.SecurityIdentifier.Value))mask&=~0xd0156;
   acl.InsertAce(acl.Count,new CommonAce(a.AceFlags&~AceFlags.Inherited,a.AceQualifier,mask,a.SecurityIdentifier,false,null));}
  var flags=(sd.ControlFlags|ControlFlags.DiscretionaryAclProtected)&~ControlFlags.DiscretionaryAclAutoInheritRequired;
  return Convert.ToBase64String(Bytes(new RawSecurityDescriptor(flags,sd.Owner,sd.Group,sd.SystemAcl,acl)));
 }
 static SafeFileHandle ReplaceExclusive(string p,Dictionary<string,object> expected){
  Check(targets.Contains(p),"outside_setter",p);Check(Equal(Row(handles[p]),expected),"target_drift",p);
  handles[p].Dispose();handles.Remove(p);SafeFileHandle h=null;
  try{h=Open(Full(p),true);Check(Equal(Row(h),expected),"exclusive_identity_or_metadata_drift",p);handles.Add(p,h);exclusive=p;return h;}
  catch{if(h!=null)h.Dispose();throw;} // No shared-mode retry after any failure.
 }
 static void RestoreRead(string p,Dictionary<string,object> expected){
  handles[p].Dispose();handles.Remove(p);exclusive=null;var h=Open(Full(p));
  try{Check(Equal(Row(h),expected),"read_pin_replacement_drift",p);handles.Add(p,h);}catch{h.Dispose();throw;}
 }
 public static void Preflight(){
  var baseline=Snapshot();foreach(var p in targets.OrderBy(x=>x,StringComparer.Ordinal)){
   var expected=Row(handles[p]);ReplaceExclusive(p,expected);RestoreRead(p,expected);
  }Check(Equal(Snapshot(),baseline),"preflight_drift");
 }
 static void Write(SafeFileHandle h,string descriptor){
  var sd=From(descriptor);Aces(sd.DiscretionaryAcl);byte[] bytes=new byte[sd.DiscretionaryAcl.BinaryLength];sd.DiscretionaryAcl.GetBinaryForm(bytes,0);
  IntPtr d=Marshal.AllocHGlobal(bytes.Length);try{Marshal.Copy(bytes,0,d,bytes.Length);
   uint flags=4u|((sd.ControlFlags&ControlFlags.DiscretionaryAclProtected)!=0?0x80000000u:0x20000000u);
   Check(SetSecurityInfo(h,1,flags,IntPtr.Zero,IntPtr.Zero,d,IntPtr.Zero)==0,"set_security");
  }finally{Marshal.FreeHGlobal(d);}
 }
 static void BindDescriptor(Dictionary<string,object> entry,Dictionary<string,object> original){
  var sd=From((string)entry["sd"]);var acl=new Dictionary<string,object>{{"protected",(sd.ControlFlags&ControlFlags.DiscretionaryAclProtected)!=0},{"aces",Aces(sd.DiscretionaryAcl)}};
  Check(sd.Owner.Value==(string)entry["owner"]&&sd.Group.Value==(string)entry["group"]&&Equal(acl,entry["acl"]),"descriptor_binding");
  var unchanged=new Dictionary<string,object>(original);unchanged["sd"]=entry["sd"];unchanged["acl"]=entry["acl"];
  Check(Equal(unchanged,entry),"descriptor_metadata_binding");
 }
 public static Dictionary<string,object> Set(string p,Dictionary<string,object> expected,Dictionary<string,object> desired){
  Check(targets.Contains(p),"outside_setter",p);Check(Equal(Snapshot(),expected),"prewrite_drift",p);
  var entries=(IDictionary<string,object>)expected["entries"];var old=(Dictionary<string,object>)entries[p];
  BindDescriptor(desired,old); // Validate actual setter bytes BEFORE Write/open gap.
  var h=ReplaceExclusive(p,old);Check(Equal(Snapshot(),expected),"exclusive_prewrite_drift",p);
  Write(h,(string)desired["sd"]);var current=Row(h);RestoreRead(p,current);return Snapshot();
 }
 public static Dictionary<string,object> Project(Dictionary<string,object> snapshot,string p){
  Check(targets.Contains(p),"outside_project",p);
  var clone=json.Deserialize<Dictionary<string,object>>(json.Serialize(snapshot));var entries=(Dictionary<string,object>)clone["entries"];
  var row=(Dictionary<string,object>)entries[p];var sd=From(Reduce((string)row["sd"]));
  row["sd"]=Convert.ToBase64String(Bytes(sd));row["acl"]=new Dictionary<string,object>{{"protected",true},{"aces",Aces(sd.DiscretionaryAcl)}};return clone;
 }
 static Dictionary<string,object> Entry(Dictionary<string,object> snapshot,string p){return (Dictionary<string,object>)((IDictionary<string,object>)snapshot["entries"])[p];}
 static void Outside(Dictionary<string,object> before,Dictionary<string,object> after,string target){
  var b=(IDictionary<string,object>)before["entries"];var a=(IDictionary<string,object>)after["entries"];
  Check(b.Keys.OrderBy(x=>x).SequenceEqual(a.Keys.OrderBy(x=>x)),"fixture_inventory");
  foreach(var p in b.Keys)if(p!=target)Check(Equal(b[p],a[p]),"fixture_outside");
 }
 static void FixtureRaw(string path,string sd,string id){using(var h=Open(path,true)){Check(Identity(h)==id,"fixture_identity");Write(h,sd);}}
 static string FixtureDescriptor(bool directory,bool protect,bool danger,bool inheritOnly=false){
  var acl=new RawAcl(2,4);foreach(var sid in new[]{principal,"S-1-5-18","S-1-5-32-544"})
   acl.InsertAce(acl.Count,new CommonAce(directory?AceFlags.ObjectInherit|AceFlags.ContainerInherit:AceFlags.None,AceQualifier.AccessAllowed,0x1f01ff,new SecurityIdentifier(sid),false,null));
  if(danger)acl.InsertAce(acl.Count,new CommonAce(directory?(AceFlags.ObjectInherit|AceFlags.ContainerInherit|(inheritOnly?AceFlags.InheritOnly:AceFlags.None)):AceFlags.None,AceQualifier.AccessAllowed,0x1201ff,new SecurityIdentifier("S-1-5-32-545"),false,null));
  return Convert.ToBase64String(Bytes(new RawSecurityDescriptor(ControlFlags.DiscretionaryAclPresent|(protect?ControlFlags.DiscretionaryAclProtected:ControlFlags.None),new SecurityIdentifier(principal),new SecurityIdentifier(principal),null,acl)));
 }
 static string CreateOwned(string path,bool directory,Dictionary<string,string> ids,Dictionary<string,SafeFileHandle> pins){
  if(directory)Check(CreateDirectory(path,IntPtr.Zero),"fixture_create_directory");
  else using(var f=new FileStream(path,FileMode.CreateNew,FileAccess.Write,FileShare.None)){f.WriteByte(42);f.Flush(true);}
  var h=Open(path);var id=Identity(h);ids.Add(path,id);pins.Add(path,h);return id;
 }
 static void PolicyChecks(){
  var allow=new CommonAce(AceFlags.ObjectInherit|AceFlags.ContainerInherit|AceFlags.InheritOnly|AceFlags.Inherited,AceQualifier.AccessAllowed,0x1201ff,new SecurityIdentifier("S-1-5-32-545"),false,null);
  var deny=new CommonAce(AceFlags.None,AceQualifier.AccessDenied,2,new SecurityIdentifier("S-1-5-32-545"),false,null);
  var acl=new RawAcl(2,2);acl.InsertAce(0,deny);acl.InsertAce(1,allow);
  var sd=new RawSecurityDescriptor(ControlFlags.DiscretionaryAclPresent,new SecurityIdentifier(principal),new SecurityIdentifier(principal),null,acl);
  var reduced=From(Reduce(Convert.ToBase64String(Bytes(sd))));
  Check(Equal(Aces(reduced.DiscretionaryAcl)[0],Aces(acl)[0]),"fixture_deny_preservation");
  var result=(CommonAce)reduced.DiscretionaryAcl[1];Check(result.AccessMask==(allow.AccessMask&~0xd0156)&&(result.AceFlags&AceFlags.Inherited)==0&&(result.AceFlags&AceFlags.InheritOnly)!=0,"fixture_inherit_only_mask");
  var bad=new RawAcl(2,2);bad.InsertAce(0,allow);bad.InsertAce(1,deny);var badsd=new RawSecurityDescriptor(ControlFlags.DiscretionaryAclPresent,sd.Owner,sd.Group,null,bad);
  bool rejected=false;try{Reduce(Convert.ToBase64String(Bytes(badsd)));}catch{rejected=true;}Check(rejected,"fixture_deny_reorder_reject");
  bad=new RawAcl(2,1);bad.InsertAce(0,new CommonAce(AceFlags.None,AceQualifier.AccessAllowed,1,new SecurityIdentifier(principal),true,null));
  badsd=new RawSecurityDescriptor(ControlFlags.DiscretionaryAclPresent,sd.Owner,sd.Group,null,bad);rejected=false;
  try{Reduce(Convert.ToBase64String(Bytes(badsd)));}catch{rejected=true;}Check(rejected,"fixture_callback_reject");
  var ti=new SecurityIdentifier("S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464");
  var parentAcl=new RawAcl(2,1);parentAcl.InsertAce(0,new CommonAce(AceFlags.None,AceQualifier.AccessAllowed,0x1f01ff,ti,false,null));
  var parentSd=new RawSecurityDescriptor(ControlFlags.DiscretionaryAclPresent,ti,sd.Group,null,parentAcl);SafeParentDescriptor(parentSd);
  Check(!Trusted(ti.Value),"fixture_parent_trust_only");
  var parentReduced=From(Reduce(Convert.ToBase64String(Bytes(parentSd))));
  Check((((CommonAce)parentReduced.DiscretionaryAcl[0]).AccessMask&0xd0156)==0,"fixture_parent_not_transform_trust");
  parentAcl=new RawAcl(2,1);parentAcl.InsertAce(0,new CommonAce(AceFlags.None,AceQualifier.AccessAllowed,0x1f01ff,new SecurityIdentifier("S-1-5-32-545"),false,null));
  parentSd=new RawSecurityDescriptor(ControlFlags.DiscretionaryAclPresent,sd.Owner,sd.Group,null,parentAcl);rejected=false;
  try{SafeParentDescriptor(parentSd);}catch{rejected=true;}Check(rejected,"fixture_untrusted_parent_write_reject");
  // Exercise the real directory record decoder without touching filesystem ACLs.
  IntPtr buffer=Marshal.AllocHGlobal(256);try{
   Marshal.Copy(new byte[256],0,buffer,256);Marshal.WriteInt32(buffer,60,4);Marshal.WriteInt64(buffer,96,9);
   Marshal.Copy(new[]{'o','k'},0,IntPtr.Add(buffer,104),2);
   Check(DirectoryBuffer(buffer,256).Single().Name=="ok","fixture_directory_record");
   foreach(int invalid in new[]{1,104,248}){
    Marshal.WriteInt32(buffer,0,invalid);rejected=false;try{DirectoryBuffer(buffer,256);}catch{rejected=true;}
    Check(rejected,"fixture_directory_next_reject");
   }Marshal.WriteInt32(buffer,0,0);
   foreach(int invalid in new[]{0,3,512}){
    Marshal.WriteInt32(buffer,60,invalid);rejected=false;try{DirectoryBuffer(buffer,256);}catch{rejected=true;}
    Check(rejected,"fixture_directory_length_reject");
   }Marshal.WriteInt32(buffer,60,4);Marshal.WriteInt16(buffer,104,(short)':');rejected=false;
   try{DirectoryBuffer(buffer,256);}catch{rejected=true;}Check(rejected,"fixture_directory_name_reject");
  }finally{Marshal.FreeHGlobal(buffer);}
 }
 static void DeleteOwned(Dictionary<string,string> ids,Dictionary<string,SafeFileHandle> pins){
  foreach(var p in ids.Keys.OrderByDescending(x=>x.Length).ThenBy(x=>x,StringComparer.Ordinal)){
   Check(pins.ContainsKey(p)&&Identity(pins[p])==ids[p],"fixture_cleanup_identity");pins[p].Dispose();pins.Remove(p);
   using(var h=CreateFile(@"\\?\"+p,0x110080,0,IntPtr.Zero,3,0x02200000,IntPtr.Zero)){
    Check(!h.IsInvalid&&Identity(h)==ids[p],"fixture_cleanup_identity");int delete=1;
    Check(SetFileInformationByHandle(h,4,ref delete,4),"fixture_cleanup");
   }
  }
 }
 public static Dictionary<string,object> Fixture(string nonce){
  Check(handles.Count==0&&ancestors.Count==0,"fixture_already_open");principal=WindowsIdentity.GetCurrent().User.Value;
  PolicyChecks();
  string parent=Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);Check(Path.IsPathRooted(parent)&&!parent.StartsWith(@"C:\personal-server",StringComparison.OrdinalIgnoreCase),"fixture_parent");
  var parentPins=new List<SafeFileHandle>();var ids=new Dictionary<string,string>(StringComparer.Ordinal);var ownedPins=new Dictionary<string,SafeFileHandle>(StringComparer.Ordinal);
  string fixture=Path.Combine(parent,"codex-source-acl-"+Guid.NewGuid().ToString("N"));bool cleaned=false;
  try{
   var chain=new List<string>();string a=Path.GetFullPath(parent);while(!String.IsNullOrEmpty(a)){chain.Add(a);var next=Path.GetDirectoryName(a);if(next==a)break;a=next;}
   chain.Reverse();foreach(var p in chain){var h=Open(p);try{SafeParent(h);parentPins.Add(h);}catch{h.Dispose();throw;}}
   CreateOwned(fixture,true,ids,ownedPins);ownedPins[fixture].Dispose();ownedPins.Remove(fixture);
   FixtureRaw(fixture,FixtureDescriptor(true,true,false),ids[fixture]);ownedPins.Add(fixture,Open(fixture));
   string target=Path.Combine(fixture,"target");CreateOwned(target,true,ids,ownedPins);ownedPins[target].Dispose();ownedPins.Remove(target);
   FixtureRaw(target,FixtureDescriptor(true,false,true),ids[target]);ownedPins.Add(target,Open(target));
   foreach(var rel in new[]{"target\\nested","target\\protected","target\\io","target\\np"}){
    string p=Path.Combine(fixture,rel);CreateOwned(p,true,ids,ownedPins);
    if(rel.EndsWith("protected")||rel.EndsWith("io")||rel.EndsWith("np")){
     ownedPins[p].Dispose();ownedPins.Remove(p);string descriptor=FixtureDescriptor(true,rel.EndsWith("protected"),true,rel.EndsWith("io"));
     if(rel.EndsWith("np")){var sd=From(descriptor);var last=(CommonAce)sd.DiscretionaryAcl[sd.DiscretionaryAcl.Count-1];last.AceFlags|=AceFlags.NoPropagateInherit;descriptor=Convert.ToBase64String(Bytes(sd));}
     FixtureRaw(p,descriptor,ids[p]);ownedPins.Add(p,Open(p));
    }
   }
   foreach(var rel in new[]{"target\\outside.txt","target\\nested\\leaf.txt","target\\protected\\leaf.txt","target\\io\\leaf.txt","target\\np\\leaf.txt"})CreateOwned(Path.Combine(fixture,rel),false,ids,ownedPins);
   // More than one fixed 4KiB directory page, with variable Unicode names.
   for(int i=0;i<20;i++)CreateOwned(Path.Combine(target,"page-"+i.ToString("D2")+"-"+new string('x',72)+"-한.txt"),false,ids,ownedPins);
   // Metadata-only access may coexist despite share0. Measure that boundary;
   // only a successful open or documented sharing violation is accepted.
   bool selfPinCoexists;
   using(var h=CreateFile(@"\\?\"+target,0x60080,0,IntPtr.Zero,3,0x02200000,IntPtr.Zero)){
    selfPinCoexists=!h.IsInvalid;Check(selfPinCoexists||Marshal.GetLastWin32Error()==32,"fixture_self_pin_open");
    if(selfPinCoexists)Check(Identity(h)==ids[target],"fixture_self_pin_identity");
   }
   foreach(var h in ownedPins.Values)h.Dispose();ownedPins.Clear();
   Start(fixture,new[]{"target","target/nested","target/nested/leaf.txt"},new[]{"directory","directory","file"},true);
   directoryPages=0;var pageNames=DirectoryEntries(handles["target"]).Select(x=>x.Name).ToArray();
   Check(directoryPages>1&&pageNames.SequenceEqual(DirectoryEntries(handles["target"]).Select(x=>x.Name)),"fixture_directory_restart_pages");
   var initial=Snapshot();Preflight();var current=initial;
   foreach(var p in new[]{"target","target/nested","target/nested/leaf.txt"}){
    var desired=Project(current,p);var next=Set(p,current,Entry(desired,p));Outside(current,next,p);
    Check(Equal(Entry(next,p),Entry(desired,p)),"fixture_project_actual");current=next;
   }
   // Actual partial forward frontier: root+nested changed, leaf restored.
   var partial=Set("target/nested/leaf.txt",current,Entry(initial,"target/nested/leaf.txt"));Outside(current,partial,"target/nested/leaf.txt");current=partial;
   foreach(var p in new[]{"target","target/nested","target/nested/leaf.txt"}){
    var next=Set(p,current,Entry(initial,p));Outside(current,next,p);current=next;
   }Check(Equal(current,initial),"fixture_nested_rollback");
   // Apply parent reduction again, close all production-style handles, then a
   // newly created child must inherit no unsafe nontrusted allow bits.
   var reduced=Project(current,"target");
   var forged=new Dictionary<string,object>(Entry(reduced,"target"));forged["sd"]=Entry(current,"target")["sd"];
   bool rejected=false;try{Set("target",current,forged);}catch{rejected=true;}Check(rejected&&Equal(Snapshot(),current),"fixture_descriptor_binding");
   current=Set("target",current,Entry(reduced,"target"));
   foreach(var p in ids.Keys)ownedPins.Add(p,Open(p));Close();
   string future=Path.Combine(target,"future.txt");CreateOwned(future,false,ids,ownedPins);
   foreach(CommonAce ace in Descriptor(ownedPins[future]).DiscretionaryAcl)Check(ace.AceQualifier!=AceQualifier.AccessAllowed||Trusted(ace.SecurityIdentifier.Value)||(ace.AccessMask&0xd0156)==0,"fixture_future_inheritance");
   byte[] plain=System.Text.Encoding.UTF8.GetBytes(nonce);byte[] cipher=ProtectedData.Protect(plain,null,DataProtectionScope.CurrentUser);
   Check(plain.SequenceEqual(ProtectedData.Unprotect(cipher,null,DataProtectionScope.CurrentUser)),"fixture_dpapi");
   DeleteOwned(ids,ownedPins);cleaned=true;
   return new Dictionary<string,object>{{"nonce",nonce},{"utc",DateTime.UtcNow.ToString("o")},{"objects",ids.Count},
    {"exclusive_suppression",true},{"protected_flatten",true},{"future_inheritance",true},{"nested_rollback",true},
    {"partialmix",true},{"outside_preserved",true},{"cleanup",true},{"dpapi",true},{"descriptor_binding",true},{"self_pin_coexists",selfPinCoexists}};
  }finally{
   if(!cleaned){
    // Acquire cleanup pins while the session still holds every existing parent.
    // If an identity is unknown, stop; never recursively delete foreign entries.
    try{foreach(var p in ids.Keys.OrderBy(x=>x.Length))if(!ownedPins.ContainsKey(p)){var h=Open(p);Check(Identity(h)==ids[p],"fixture_cleanup_identity");ownedPins.Add(p,h);}Close();DeleteOwned(ids,ownedPins);}
    finally{Close();foreach(var h in ownedPins.Values)h.Dispose();}
   }
   foreach(var h in parentPins)h.Dispose();
  }
 }
 public static void Close(){foreach(var h in handles.Values)h.Dispose();handles.Clear();foreach(var h in ancestors)h.Dispose();ancestors.Clear();ancestorPins.Clear();exclusive=null;}
}
'@
$json=[System.Web.Script.Serialization.JavaScriptSerializer]::new()
$json.MaxJsonLength=67108864
try {
 while($null -ne ($line=[Console]::In.ReadLine())) {
  try {
   $v=$json.DeserializeObject($line)
   switch([string]$v.op) {
    'compile' { $result=@{compiled=$true} }
    'fixture' { $result=[SourceAcl]::Fixture([string]$v.nonce) }
    'open' { [SourceAcl]::Start([string]$v.root,[string[]]$v.paths,[string[]]$v.kinds,$false); $result=@{ancestor_sha256=[SourceAcl]::AncestorHash} }
    'snapshot' { $result=[SourceAcl]::Snapshot() }
    'preflight' { [SourceAcl]::Preflight(); $result=@{preflight=$true} }
    'project' { $result=[SourceAcl]::Project($v.snapshot,[string]$v.target) }
    'set' { $result=[SourceAcl]::Set([string]$v.target,$v.expected,$v.entry) }
    default { throw 'native_acl_failure' }
   }
   [Console]::Out.WriteLine($json.Serialize($result));[Console]::Out.Flush()
  } catch { [Console]::Out.WriteLine('{"error":"native_acl_failure"}');[Console]::Out.Flush() }
 }
} finally { [SourceAcl]::Close() }
'''


if __name__ == '__main__':
    print('{"reason":"trusted_memory_launcher_required","status":"BLOCKED"}')
