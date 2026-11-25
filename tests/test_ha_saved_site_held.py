"""Same-boot held adapter contracts; no Windows/guest service calls."""

from contextlib import contextmanager
from copy import deepcopy
import base64
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'infra/k8s'))
from ha_control import native_saved_site_held as held, native_stdio_bridge as wire
from ha_control import native_guest_worker as worker, native_guest_postboot as post, native_legacy_provider as native
from ha_control import native_saved_site_apps as apps_module
from ha_control.controller import TransitionBlocked


BOOT = '1' * 64
CARRY = '2' * 64
STOP = '3' * 64
ATTEMPT = '4' * 64
NONCE = '5' * 64
ROOT_WIN = 'C:\\ProgramData\\N100HA-InitialCutover\\' + 'a' * 40


def spec_fixture():
    callback = dict(windows_boot_sha256=BOOT, purpose='held_admission', guest_vsock_port=850,
                    guest_boot_sha256='6' * 64, hold_sha256='7' * 64)
    raw = held._encoded(callback)
    args = {key: '8' * 64 for key in ('PlanSha256', 'AttemptId', 'CallbackSha256', 'KeySha256',
                                             'RecoveryProtocolSha256')}
    args.update(Generation=1, WindowsBootSha256=BOOT, CallbackConfigSha256=held._sha(raw),
                PipeName='n100ha-recovery-proof-' + '9' * 32,
                OperatorSid='S-1-5-21-1-2-3-4', ServiceSid='S-1-5-80-1-2-3-4-5')
    args.update({key: ROOT_WIN + '\\' + key + '.ps1' for key in
                 ('CallbackPath', 'CallbackConfigPath', 'KeyPath', 'ReadyPath', 'RecoveryProtocolPath')})
    descriptor = held._encoded({'server_path': ROOT_WIN + '\\server.ps1', 'server_sha256': '9' * 64,
                               'arguments': args})
    return {'descriptor_path': ROOT_WIN + '\\input.json', 'descriptor_raw': descriptor,
            'descriptor_sha256': held._sha(descriptor), 'callback_raw': raw, 'callback_sha256': held._sha(raw)}


def prepared(spec=None, **changes):
    return held.prepare_held(spec or spec_fixture(), **(dict(boot=BOOT, nonce=NONCE, attempt=ATTEMPT,
        stopped=STOP, carryover=CARRY, port=852, mapping_label='N100HA held fixture') | changes))


class SavedSiteHeldTests(unittest.TestCase):
    def test_sameboot_preparation_is_deterministic_and_keeps_native_identity_and_key(self):
        original = spec_fixture()
        before = deepcopy(original)
        value = prepared(original)
        self.assertEqual(value, prepared(original))
        self.assertEqual(original, before)
        old = json.loads(original['descriptor_raw'])
        new = value['descriptor']
        self.assertEqual(old['server_path'], new['server_path'])
        self.assertEqual(old['server_sha256'], new['server_sha256'])
        for key in ('KeyPath', 'KeySha256', 'ServiceSid', 'OperatorSid', 'Generation', 'WindowsBootSha256'):
            self.assertEqual(old['arguments'][key], new['arguments'][key])
        callback = json.loads(base64.b64decode(value['config_base64']))
        self.assertEqual(callback['guest_vsock_port'], 852)
        self.assertEqual(callback['postboot_carryover_receipt_sha256'], CARRY)
        self.assertEqual(callback['postboot_stopped_receipt_sha256'], STOP)
        self.assertNotEqual(new['arguments']['PipeName'], old['arguments']['PipeName'])
        self.assertNotEqual(value['config_sha256'], prepared(nonce='a' * 64)['config_sha256'])

    def test_scope_or_source_drift_rejected_before_any_io(self):
        for changes in (dict(boot='b' * 64), dict(port=850), dict(port=True),
                        dict(carryover='bad'), dict(mapping_label='unsafe\\path')):
            with self.subTest(changes=changes), self.assertRaises(TransitionBlocked):
                prepared(**changes)
        changed = spec_fixture()
        changed['callback_raw'] += b' '
        with self.assertRaises(TransitionBlocked):
            prepared(changed)

    def test_historical_admission_stop_is_distinct_from_current_quiesce_stop(self):
        adapter = object.__new__(held.SavedSiteHeldAdmission)
        adapter.scope = {'previous_windows_boot_sha256': BOOT}
        adapter.spec, adapter.port, adapter.label = spec_fixture(), 852, 'N100HA held fixture'
        current_request = {'fixture': 'current quiesce'}
        adapter.apps = SimpleNamespace(_operation=Mock(), terminal_pin='9' * 64,
            previous=SimpleNamespace(args={'postboot_stopped_receipt_sha256': STOP}),
            read_quiesce_request=lambda: current_request)
        current_stop = '0' * 64
        chain = {'carryover': {'carryover_sha256': CARRY},
                 'close_pair': {'terminal_sha256': '9' * 64},
                 'quiesce_apps': {'stop_receipt_sha256': current_stop,
                                  'request_sha256': held._sha(wire._bytes(current_request))}}
        operation = {'scope': adapter.scope, 'plan_material_sha256': None, 'nonce': NONCE}
        with patch.object(held, '_validate'), patch.object(held, '_fallback_attempt', return_value=ATTEMPT), \
                patch.object(held, '_scope_request', side_effect=lambda value: value):
            spec = adapter._prepare(operation, chain, CARRY, 1e12)
        callback = json.loads(base64.b64decode(spec['config_base64']))
        self.assertEqual(callback['postboot_stopped_receipt_sha256'], STOP)
        self.assertNotEqual(callback['postboot_stopped_receipt_sha256'], current_stop)

    def host(self):
        adapter = object.__new__(held.SavedSiteHeldAdmission)
        adapter.scope = dict(previous_windows_boot_sha256=BOOT, previous_plan_sha256='a' * 64,
                             previous_instance_sha256='b' * 64, previous_revision=18)
        adapter.pair = SimpleNamespace(config={'plan': ROOT_WIN + '\\plan.json'},
            _command=Mock(return_value=('throw "fixture_not_executed"', 'c' * 64)),
            windows=SimpleNamespace(powershell=Mock()))
        adapter.callback = b'function unused {}'
        adapter.starter = {'path': ROOT_WIN + '\\starter.ps1', 'sha256': 'd' * 64}
        adapter.native_windows = held._windows_source()
        spec = prepared()
        challenge = 'e' * 64
        callback = json.loads(base64.b64decode(spec['config_base64']))
        request = json.dumps({'guest_boot_sha256': callback['guest_boot_sha256'],
            'hold_sha256': callback['hold_sha256'], 'purpose': 'held_admission', 'probe': challenge},
            separators=(',', ':')).encode()
        fact = dict(server_pid=123, pipe_name=spec['descriptor']['arguments']['PipeName'],
            key_sha256=spec['descriptor']['arguments']['KeySha256'], creation_filetime='123456',
            ready_sha256='f' * 64, config_sha256=spec['config_sha256'], input_sha256=spec['input_sha256'])
        reply = dict(status='HELD_CALLBACK_VERIFIED', challenge=challenge, observation_sha256=CARRY,
                     challenge_sha256=held._sha(request), observer=fact)
        adapter.pair.windows.powershell.return_value = reply
        args = ('observe', {'nonce': NONCE}, {'close_pair': {'terminal_sha256': STOP}}, CARRY, spec, challenge, 1e12)
        return adapter, args, reply

    def test_host_requires_fresh_exact_native_callback_not_echoed_receipt(self):
        adapter, args, reply = self.host()
        self.assertEqual(adapter._host(*args), reply)
        source = adapter.pair.windows.powershell.call_args.args[0]
        self.assertIn('Get-HaRecoveryObservation $raw $deadline', source)
        for fields in ({'challenge': '0' * 64}, {'challenge_sha256': '0' * 64},
                       {'observation_sha256': '0' * 64}, {'extra': True}):
            adapter.pair.windows.powershell.return_value = reply | fields
            with self.subTest(fields=fields), self.assertRaises(TransitionBlocked):
                adapter._host(*args)
        adapter.pair.windows.powershell.return_value = {'observation_sha256': CARRY}
        with self.assertRaises(TransitionBlocked):
            adapter._host(*args)

    def test_host_sequence_releases_guest_before_callback_and_never_reapplies_on_observe(self):
        adapter, _, reply = self.host()
        adapter.lock, adapter.issued = threading.Lock(), False
        adapter.apps = SimpleNamespace(_operation=Mock())
        adapter.scope = {}
        adapter._prepare = Mock(return_value=prepared())
        calls = []
        def guest(mode, *_):
            calls.append(('guest', mode))
            return {'fixture_guest': 'same'}
        def host(mode, *_):
            calls.append(('host', mode))
            return reply
        adapter._guest, adapter._host = guest, host
        with patch.object(held, '_validate'):
            adapter.observe_held_carryover({}, {}, CARRY, 1e12, observe_only=False)
            self.assertEqual(calls, [('host', 'preflight'), ('guest', 'apply'), ('host', 'apply'),
                                     ('guest', 'observe'), ('host', 'observe')])
            with self.assertRaises(TransitionBlocked):
                adapter.observe_held_carryover({}, {}, CARRY, 1e12, observe_only=False)
            calls.clear()
            adapter.observe_held_carryover({}, {}, CARRY, 1e12, observe_only=True)
            self.assertEqual(calls, [('guest', 'observe'), ('host', 'observe'), ('guest', 'observe')])

    def test_lost_apply_response_cannot_dispatch_again(self):
        adapter, _, _ = self.host()
        adapter.lock, adapter.issued = threading.Lock(), False
        adapter._prepare = Mock(return_value=prepared())
        adapter._host = Mock(return_value={})
        adapter._guest = Mock(side_effect=TransitionBlocked('unknown'))
        with self.assertRaises(TransitionBlocked):
            adapter.observe_held_carryover({}, {}, CARRY, 1e12, observe_only=False)
        with self.assertRaises(TransitionBlocked):
            adapter.observe_held_carryover({}, {}, CARRY, 1e12, observe_only=False)
        self.assertEqual(adapter._guest.call_count, 1)


class HeldGuestJournalTests(unittest.TestCase):
    """Run the actual emitted guest program with fake external native boundaries."""

    def fixture(self):
        binding = wire.Binding('a' * 64, 'b' * 64, 'c' * 64, wire._digest(b'boot'), 'd' * 64)
        root = PurePosixPath('/var/lib/n100-ha/cutover')
        stage = root / binding.attempt_id
        manifest, rollback = 'e' * 64, 'f' * 64
        source = root / ('recovery-source-' + manifest) / 'native-guest-recovery-observer.py'
        source_raw = b'# protected native wrapper fixture\n'
        oldheld = dict(pid=41, fragment_sha256='1' * 64, argv_sha256='2' * 64)
        restored_provider = dict(pid=52, fragment_sha256='3' * 64, argv_sha256='4' * 64)
        units = {'held': dict(unit='n100ha-postboot-observer-original.service'),
                 'provider': dict(unit='n100ha-recovery-observer-original.service', argv_sha256='4' * 64)}
        restore_path = stage / 'restore-done.json'
        restore_raw = wire._bytes({'held': oldheld, 'provider': restored_provider})
        carry = dict(history={'intent_sha256': '9' * 64}, recovery_args={'hold_sha256': '8' * 64},
                     postboot={'stopped_receipt_sha256': STOP})
        carry_raw = wire._bytes(carry)
        carry_pin = wire._digest(carry_raw)
        carry_path = stage / 'carry.json'
        envelope = dict(mode='apply', operation_nonce=NONCE, operation_sha256='7' * 64, carryover_sha256=carry_pin)
        cfg = dict(envelope=envelope, seconds=60, attempt=ATTEMPT, port=852, manifest=manifest,
            source=str(source), source_pin=wire._digest(source_raw), stopped=STOP, bootstrap=held._BOOTSTRAP,
            rollback=rollback, archive=dict(previous_request={'binding': binding.fields()},
                history=carry['history'], hold_sha256='8' * 64, provider_port=850, held_port=851,
                expected_held=oldheld))
        quiesce = wire.Request(binding, 'A', wire.GUEST_QUIESCE_SAMEBOOT_SUCCESSOR, 1, 'b' * 64,
                               {'postboot_attempt_id': 'c' * 64})
        cfg['quiesce'] = apps_module._scope_request(quiesce)
        stopped = dict(args=quiesce.args, containers={}, closed_revision=19, closed_proof_sha256='d' * 64)
        stop_done_raw = wire._bytes(stopped)
        stop_intent_raw = wire._bytes({'schema_version': 1, **worker._scope(quiesce), 'args': quiesce.args,
            'closed_proof_sha256': 'd' * 64, 'containers': {}, 'automatic_retry_permitted': False})
        qi, qd = stage / 'current-stop-intent.json', stage / 'current-stop-done.json'
        cfg['quiesce_receipt'] = {'request_sha256': wire._digest(wire._bytes(cfg['quiesce'])),
                                  'stop_receipt_sha256': wire._digest(stop_done_raw),
                                  'revision': 19, 'closed_proof_sha256': 'd' * 64}
        self.assertNotEqual(cfg['quiesce_receipt']['stop_receipt_sha256'], cfg['stopped'])
        storage = {str(source): source_raw, str(restore_path): restore_raw, str(carry_path): carry_raw}
        storage.update({str(qi): stop_intent_raw, str(qd): stop_done_raw})
        state = dict(started=False, commands=[], writes=[], frames=[], lock=False, lose=False)
        owner = self

        class PathFact:
            def __init__(self, path): self.path = PurePosixPath(str(path))
            def __truediv__(self, name): return PathFact(self.path / name)
            def __str__(self): return str(self.path)
            def __eq__(self, other): return str(self) == str(other)
            def read_text(self):
                owner.assertEqual(str(self), '/proc/63/stat')
                return '63 (observer) ' + ' '.join(['0'] * 19 + ['12345'])

        class Adapter:
            def __init__(self, _stage, _binding, *, latest_intent_sha256, rollback_sha256, hold_sha256,
                         provider_port, held_port):
                self.intent, self.rollback, self.hold = latest_intent_sha256, rollback_sha256, hold_sha256
                self.ports = {'provider': provider_port, 'held': held_port}
            def _archive(self): return units
            def _receipt(self, op):
                owner.assertEqual(op, 'restore')
                return wire._digest(restore_raw)
            def _paths(self, op): return stage / 'restore-intent.json', restore_path
            def _live(self, entry, role, **_):
                if entry['unit'] == units['held']['unit']: return oldheld
                if entry['unit'] == units['provider']['unit']: return restored_provider
                owner.assertTrue(state['started'])
                return dict(pid=63, fragment_sha256='6' * 64, argv_sha256=entry['argv_sha256'])

        class Channel:
            def read(self, _):
                if not state['frames']: return cfg['envelope']
                frame = state['frames'][-1]
                return {k: v for k, v in frame.items() if k != 'kind'} | dict(
                    kind='activation_boundary_current', windows_proof_sha256='a' * 64)
            def write(self, frame, _): state['frames'].append(frame)

        def read(path, uid, modes, **_):
            owner.assertEqual(uid, 0)
            owner.assertEqual(modes, {0o400})
            return storage[str(path)]
        def write(path, raw, mode):
            owner.assertTrue(state['lock'])
            owner.assertNotIn(str(path), storage)
            owner.assertEqual(mode, 0o400)
            storage[str(path)] = raw
            state['writes'].append(str(path))
        @contextmanager
        def lock(**_):
            owner.assertFalse(state['lock']);state['lock'] = True
            try: yield
            finally: state['lock'] = False
        def command(argv):
            owner.assertTrue(state['lock'])
            owner.assertTrue(any(p.endswith('-issued.json') for p in storage))
            owner.assertEqual(argv[0], '/usr/bin/systemd-run')
            owner.assertIn('--expand-environment=no', argv)
            suffix = list(argv[argv.index('--') + 1:])
            owner.assertEqual(suffix[6:10], [str(source), cfg['source_pin'], '--port', '852'])
            owner.assertEqual(suffix[-2:], ['--postboot-carryover-receipt-sha256', carry_pin])
            state['commands'].append(argv);state['started'] = True
            if state['lose']: raise subprocess.TimeoutExpired('fixture', 1)
        def full_quiesce(*_args, **_kwargs):
            # Model the native reader's own non-reentrant descriptor, rather
            # than returning a Boolean that conceals nested lock attempts.
            with lock():
                return cfg['quiesce_receipt']
        fake_pathlib = ModuleType('pathlib');fake_pathlib.Path = PathFact
        patches = [patch.object(worker, 'ROOT', root), patch.object(worker.runtime, '_read', side_effect=read),
            patch.object(worker.runtime, '_write', side_effect=write),
            patch.object(worker.runtime, 'BOOT_ID', SimpleNamespace(read_bytes=lambda: b'boot')),
            patch.object(worker, '_guest_mutation_descriptor', lock), patch.object(post, 'carryover_path', return_value=carry_path),
            patch.object(post, 'observe_carryover', return_value=carry), patch.object(native, 'ProviderRetirement', Adapter),
            patch.object(post, 'observe_quiesce_completion', side_effect=full_quiesce),
            patch.object(post, 'paths', return_value=(qi, qd)), patch.object(post, 'observe', return_value=stopped),
            patch.object(apps_module, '_request', return_value=quiesce),
            patch.object(native, '_properties', side_effect=lambda unit: dict(Id=unit, MainPID='0', ActiveState='inactive')),
            patch.object(native, '_listeners', side_effect=lambda: {852: ['fixture']} if state['started'] else {}),
            patch.object(native, '_command', side_effect=command), patch.object(wire, 'Stdio', return_value=Channel()),
            patch.object(held.sys.modules['os'], 'geteuid', return_value=0),
            patch.object(held.sys.modules['os'].path, 'lexists', side_effect=lambda path: str(path) in storage),
            patch.dict(sys.modules, {'pathlib': fake_pathlib})]
        return cfg, storage, state, patches

    def run_guest(self, cfg, patches):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for item in patches: stack.enter_context(item)
            exec(compile(held._GUEST, '<held-guest>', 'exec'), {'CFG': cfg, 'sys': sys, 'json': json})

    def test_actual_guest_apply_once_and_observe_writes_nothing(self):
        cfg, storage, state, patches = self.fixture()
        self.run_guest(cfg, patches)
        self.assertEqual(state['frames'][-1]['status'], 'OBSERVED')
        self.assertEqual(len(state['commands']), 1)
        self.assertEqual(len(state['writes']), 2)
        before = deepcopy(storage)
        state['frames'].clear();cfg['envelope']['mode'] = 'observe'
        self.run_guest(cfg, patches)
        self.assertEqual(state['frames'][-1]['status'], 'OBSERVED')
        self.assertEqual(storage, before)
        self.assertEqual(len(state['commands']), 1)
        state['frames'].clear();cfg['envelope']['mode'] = 'apply'
        self.run_guest(cfg, patches)
        self.assertEqual(state['frames'][-1]['status'], 'UNKNOWN')
        self.assertEqual(len(state['commands']), 1)

    def test_lost_start_response_reconciles_without_receipt_repair_or_redispatch(self):
        cfg, storage, state, patches = self.fixture()
        state['lose'] = True
        self.run_guest(cfg, patches)
        self.assertEqual(state['frames'][-1]['status'], 'UNKNOWN')
        self.assertEqual(len(state['writes']), 1)
        before = deepcopy(storage)
        state['frames'].clear();cfg['envelope']['mode'] = 'observe'
        self.run_guest(cfg, patches)
        self.assertEqual(state['frames'][-1]['status'], 'OBSERVED')
        self.assertEqual(storage, before)
        self.assertEqual(len(state['commands']), 1)


if __name__ == '__main__':
    unittest.main()
