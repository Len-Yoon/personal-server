"""Executable B crash trigger, with only external native effects mocked."""

from contextlib import contextmanager
from dataclasses import replace
import base64
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'infra/k8s'))
from ha_control import native_b_recovery as b, native_stdio_bridge as wire
from ha_control.controller import TransitionBlocked
from ha_control.caddy_runtime import ActiveCaddy, PreparedCaddy, CaddyRuntime
from ha_control.car_callback_runtime import CallbackRuntime, Relay
from ha_control.native_guest_activation import ServingGeneration
from ha_control.native_guest_ssh import GuestSshDispatcher, SshEndpoint
from ha_control.native_replacement_network import WindowsReplacementNetwork
from ha_control.native_windows_activation import WindowsTransitionServiceStart
from ha_control.native_windows_ssh import WindowsSsh
from ha_control.transition_execution import TransitionExecution
from ha_control.transition_ingress import CompletedTransition


OLD = '11111111-1111-1111-1111-111111111111'
TARGET = '22222222-2222-2222-2222-222222222222'
REPLACEMENT = '33333333-3333-3333-3333-333333333333'
BOOT = 'a' * 64
B_RECOVERY_SITE_CONFIG = {'fixture': True}


def existing_site_callback(*_args, **_kwargs):
    assert B_RECOVERY_SITE_CONFIG == {'fixture': True}
    raise AssertionError('site callback must be invoked by the existing native engine only')


def captured_site_callback(state):
    def callback(*_args):
        assert B_RECOVERY_SITE_CONFIG == {'fixture': True}
        return state['proof']
    return callback


class BRecoveryTests(unittest.TestCase):
    @contextmanager
    def prepared(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            root.chmod(0o700)
            journal = root / 'dispatch'
            journal.mkdir(mode=0o700)
            execution = object.__new__(TransitionExecution)
            execution.directory = root / 'native-ingress'
            execution.spec = {'plan_sha256': '1' * 64, 'attempt_id': '2' * 64,
                'directory': str(execution.directory), 'coordinator': {'pid': 123}, 'project': 'fixture'}
            execution.verify_crash_source_record = Mock(return_value='3' * 64)
            binding = wire.Binding('1' * 64, '2' * 64, '4' * 64, '5' * 64, '6' * 64)
            def endpoint(host, alias):
                return SshEndpoint(host, 22, 'fixture' if alias == 'jump' else 'root', alias, root/'fixture-key',
                                   'SHA256:'+'a'*43, 'SHA256:'+{'jump':'b','A':'c','B':'d'}[alias]*43)
            jump = endpoint('192.0.2.1', 'jump')
            guests = GuestSshDispatcher(binding, jump=jump,
                guests={'A': endpoint('192.0.2.10', 'A'), 'B': endpoint('192.0.2.11', 'B')},
                known_hosts={role: root/('known-'+role) for role in ('jump','A','B')},
                launcher_sha256='a'*64, run=existing_site_callback)
            request = wire.Request(binding, 'B', wire.GUEST_TRANSITION_TARGET, 1, '7' * 64,
                                   {'installation_id': '8' * 64})
            windows = WindowsSsh(guests.jump, guests.known_hosts['jump'])
            network_pin = b._sha((ROOT / 'infra/k8s/ha_control/windows-ha-replacement-network.ps1').read_bytes())
            network = WindowsReplacementNetwork(windows, network_pin, primary_vm_id=TARGET,
                old_disk_paths_sha256='9' * 64, isolated_switch_id='44444444-4444-4444-4444-444444444444',
                serving_switch_id='55555555-5555-5555-5555-555555555555', replacement_mac='021122334455')
            starter = WindowsTransitionServiceStart(windows, 'b' * 64)
            template = {'node': 'A', 'generation': 2, 'source_lease_close_required': False,
                'crash_source_sha256': '3' * 64, 'windows_enforced': {'vm_id': OLD},
                'pg_nodes': {'A': {'host': '192.0.2.10'}, 'B': {'host': '192.0.2.11'}}}
            plan = b._bytes(template)
            candidate = PreparedCaddy({}, root/'override.json', root/'Caddyfile', *(['1'*64]*4))
            previous = ActiveCaddy(candidate, {'baseline': 'old'}, b'old config', '192.0.2.10', '2'*64)
            relay = Relay('3'*64, 'sha256:'+'4'*64, '5'*64, '192.0.2.15', root/'relay',
                          '192.0.2.10', '6'*64, '7'*64, '8'*64)
            inputs = dict(execution=execution, main=CaddyRuntime(root, 'fixture'),
                car=CallbackRuntime(existing_site_callback), previous=previous,
                relay=relay, generation=ServingGeneration('B', 2, 2, 2), target='192.0.2.11',
                route_pin='c' * 64, previous_runtime_sha256='d' * 64, legacy_plan='e' * 64,
                legacy_attempt='f' * 64, expected_mtls_sha256='0' * 64,
                previous_callback=b'callback', previous_callback_sha256=b._sha(b'callback'),
                replacement_vm=dict(node='A', vm_id=REPLACEMENT, installation_id='1' * 64, disk_paths_sha256='2' * 64),
                observer_release={}, observer_setup=dict(windows=windows, plan_raw=plan,
                    plan_sha256=b._sha(plan), deadline=1e12, root='fixture'),
                controller_setup=dict(starter=starter, old_vm_id=OLD, deadline=1e12, root='fixture'),
                assert_legacy_closed=existing_site_callback, guests=guests, target_request=request,
                replacement_manifest_sha256='4' * 64, generation_handoff_pins=existing_site_callback,
                stage_service={'approval': existing_site_callback}, isolated_replacement={},
                attach_replacement_network=network, prepare_guest_runtime=True, verify_crash_source=existing_site_callback)
            seen = []
            def native_reader(source, deadline):
                marker = "FromBase64String('"
                native_source = base64.b64decode(source.split(marker)[2].split("'", 1)[0]).decode()
                self.assertIn('-ReadCrashSource', native_source)
                self.assertNotIn('-Go', native_source)
                encoded = source.split(marker, 1)[1].split("'", 1)[0]
                cfg = json.loads(base64.b64decode(encoded))
                seen.append(cfg)
                return dict(challenge=cfg['nonce'], windows_boot_sha256=cfg['boot'], vm_id=OLD,
                    disk_paths_sha256='9' * 64, state='Off', automatic_start='Nothing',
                    target_vm_id=TARGET, target_state='Running', boot_task_disabled=True,
                    boot_task_instances=0, boot_task_xml_sha256='c'*64)
            config = b._bytes(B_RECOVERY_SITE_CONFIG)
            site = b.ReviewedBRecoverySite(Path(__file__).resolve(), b._sha(Path(__file__).read_bytes()),
                config, b._sha(config), 'c'*64)
            with patch.object(WindowsSsh, 'powershell', new=Mock(side_effect=native_reader)):
                raw = b.arm_b_recovery(inputs, windows_boot_sha256=BOOT, reviewed_site=site, journal=journal)
                dispatcher = b.BRecoveryDispatcher(armed_raw=raw, armed_sha256=b._sha(raw),
                    transition_inputs=inputs, journal=journal, reviewed_site=site)
                yield dispatcher, inputs, journal, seen

    def outcome(self, dispatcher, inputs):
        generation = replace(inputs['generation'], revision=6)
        receipt = {'scope': {'primary': 'B', 'target_sha256': dispatcher.armed['target_sha256'],
            'attempt_id': dispatcher.armed['attempt_id'], 'plan_sha256': dispatcher.armed['plan_sha256']}}
        return CompletedTransition(None, None, receipt, {'plan_sha256': '5' * 64}, {}, generation,
                                   activation_sha256='6' * 64)

    def test_dispatch_calls_real_transition_entry_after_durable_fence_and_issue(self):
        with self.prepared() as (dispatcher, inputs, journal, seen):
            expected = self.outcome(dispatcher, inputs)
            def engine(**kwargs):
                self.assertTrue((journal / 'fenced.json').is_file())
                issued = json.loads((journal / 'issued.json').read_bytes())
                self.assertFalse(issued['automatic_retry_permitted'])
                self.assertEqual(issued['armed_sha256'], dispatcher.pin)
                self.assertIs(kwargs['guests'], inputs['guests'])
                self.assertEqual(kwargs['generation'].primary, 'B')
                self.assertEqual(kwargs['observer_setup']['deadline'], 1e12)
                return expected
            with patch.object(b.transition, 'switch_with_observer', side_effect=engine) as switch:
                self.assertIs(dispatcher.dispatch(1e12), expected)
                self.assertEqual(switch.call_count, 1)
                self.assertEqual(len(seen), 3)
                self.assertEqual(len({fact['nonce'] for fact in seen}), 3)
                before = {p.name: p.read_bytes() for p in journal.iterdir() if p.name != 'lock'}
                self.assertEqual(dispatcher.observe(1e12)['status'], 'COMPLETED_RECEIPT_PRESENT')
                self.assertEqual(before, {p.name: p.read_bytes() for p in journal.iterdir() if p.name != 'lock'})
                with self.assertRaisesRegex(TransitionBlocked, b.UNKNOWN): dispatcher.dispatch(1e12)
                self.assertEqual(switch.call_count, 1)

    def test_unknown_dispatch_never_redispatches_even_after_new_adapter(self):
        with self.prepared() as (dispatcher, inputs, journal, _):
            def uncertain(**_):
                inputs['controller_setup']['starter'].attempted = True
                inputs['main'].attempted = True
                raise TimeoutError('fixture')
            with patch.object(b.transition, 'switch_with_observer', side_effect=uncertain) as switch:
                with self.assertRaisesRegex(TransitionBlocked, b.UNKNOWN): dispatcher.dispatch(1e12)
                second = b.BRecoveryDispatcher(armed_raw=dispatcher.armed_raw, armed_sha256=dispatcher.pin,
                                              transition_inputs=inputs, journal=journal, reviewed_site=dispatcher.site)
                self.assertEqual(second.observe(1e12)['status'], 'RECONCILIATION_REQUIRED')
                with self.assertRaisesRegex(TransitionBlocked, b.UNKNOWN): second.dispatch(1e12)
                self.assertEqual(switch.call_count, 1)
                self.assertFalse((journal / 'observed.json').exists())

    def test_running_or_restarting_a_or_wrong_boot_never_dispatches(self):
        for changes in ({'state': 'Running'}, {'automatic_start': 'Start'}, {'windows_boot_sha256': '0' * 64},
                        {'vm_id': REPLACEMENT}, {'target_state': 'Off'}, {'challenge': '0' * 64}):
            with self.subTest(changes=changes), self.prepared() as (dispatcher, inputs, journal, _):
                windows = inputs['attach_replacement_network'].windows
                read = windows.powershell.side_effect
                windows.powershell.side_effect = lambda *args: read(*args) | changes
                with patch.object(b.transition, 'switch_with_observer') as switch:
                    with self.assertRaises(TransitionBlocked): dispatcher.dispatch(1e12)
                    switch.assert_not_called()
                self.assertFalse((journal / 'issued.json').exists())

    def test_stale_crash_source_or_changed_armed_generation_blocks(self):
        with self.prepared() as (dispatcher, inputs, journal, _):
            inputs['execution'].verify_crash_source_record.return_value = '0' * 64
            with patch.object(b.transition, 'switch_with_observer') as switch:
                with self.assertRaises(TransitionBlocked): dispatcher.dispatch(1e12)
                switch.assert_not_called()
            self.assertFalse((journal / 'issued.json').exists())
        with self.prepared() as (dispatcher, inputs, _, _):
            dispatcher.inputs['generation'] = ServingGeneration('B', 3, 3, 3)
            with self.assertRaises(TransitionBlocked): dispatcher.dispatch(1e12)

    def test_a_target_and_false_arm_digest_and_unsafe_journal_rejected(self):
        with self.prepared() as (dispatcher, inputs, journal, _):
            with self.assertRaises(TransitionBlocked):
                b.BRecoveryDispatcher(armed_raw=dispatcher.armed_raw, armed_sha256='0' * 64,
                                     transition_inputs=inputs, journal=journal, reviewed_site=dispatcher.site)
            inputs['generation'] = ServingGeneration('A', 2, 2, 2)
            with self.assertRaises(TransitionBlocked):
                b.arm_b_recovery(inputs, windows_boot_sha256=BOOT, reviewed_site=dispatcher.site, journal=journal)
            journal.chmod(0o755)
            with self.assertRaises(TransitionBlocked): dispatcher.observe(1e12)

    def test_fence_readback_cannot_rebind_to_other_vm_after_issuance(self):
        with self.prepared() as (dispatcher, inputs, _, _):
            inputs['attach_replacement_network'].pins['PrimaryVmId'] = REPLACEMENT
            with self.assertRaises(TransitionBlocked): dispatcher.observe(1e12)

    def test_another_journal_cannot_reuse_same_armed_ticket(self):
        with self.prepared() as (dispatcher, inputs, journal, _):
            other = journal.parent/'another-dispatch'
            other.mkdir(mode=0o700)
            with self.assertRaises(TransitionBlocked):
                b.BRecoveryDispatcher(armed_raw=dispatcher.armed_raw, armed_sha256=dispatcher.pin,
                    transition_inputs=inputs, journal=other, reviewed_site=dispatcher.site)

    def test_site_config_or_boot_task_cannot_be_substituted(self):
        with self.prepared() as (dispatcher, inputs, journal, _):
            bad = replace(dispatcher.site, config_raw=b'{}', config_sha256=b._sha(b'{}'))
            with self.assertRaises(TransitionBlocked):
                b.BRecoveryDispatcher(armed_raw=dispatcher.armed_raw, armed_sha256=dispatcher.pin,
                    transition_inputs=inputs, journal=journal, reviewed_site=bad)
            original = inputs['attach_replacement_network'].windows.powershell.side_effect
            inputs['attach_replacement_network'].windows.powershell.side_effect = lambda *a: original(*a) | {'boot_task_disabled':False}
            with patch.object(b.transition, 'switch_with_observer') as switch:
                with self.assertRaises(TransitionBlocked): dispatcher.dispatch(1e12)
                switch.assert_not_called()

    def test_changed_callback_closure_cannot_reuse_armed_generation(self):
        with self.prepared() as (old, inputs, journal, _):
            state = {'proof':'1'*64}
            inputs['assert_legacy_closed'] = captured_site_callback(state)
            raw = b.arm_b_recovery(inputs, windows_boot_sha256=BOOT, reviewed_site=old.site, journal=journal)
            dispatcher = b.BRecoveryDispatcher(armed_raw=raw, armed_sha256=b._sha(raw),
                transition_inputs=inputs, journal=journal, reviewed_site=old.site)
            state['proof'] = '2'*64
            with patch.object(b.transition, 'switch_with_observer') as switch:
                with self.assertRaises(TransitionBlocked): dispatcher.dispatch(1e12)
                switch.assert_not_called()

    def test_consumed_command_change_after_arm_blocks_before_fence_or_dispatch(self):
        with self.prepared() as (dispatcher, inputs, journal, seen):
            inputs['guests'].command += ' unexpected'
            with patch.object(b.transition, 'switch_with_observer') as switch:
                with self.assertRaises(TransitionBlocked): dispatcher.dispatch(1e12)
                switch.assert_not_called()
            self.assertEqual(seen, [])
            self.assertFalse((journal/'issued.json').exists())

    def test_direct_link_missing_or_added_and_b_scope_drift_are_refused(self):
        changes = [lambda guests: setattr(guests, 'direct_link', object()),
                   lambda guests: delattr(guests, 'direct_link'),
                   lambda guests: guests.guests.pop('B'),
                   lambda guests: guests.known_hosts.pop('B'),
                   lambda guests: guests.guests.update(B=replace(guests.guests['B'], host='192.0.2.12'))]
        for change in changes:
            with self.subTest(change=change), self.prepared() as (dispatcher, inputs, _, seen):
                change(inputs['guests'])
                with patch.object(b.transition, 'switch_with_observer') as switch:
                    with self.assertRaises((TransitionBlocked, wire.BridgeRejected)):
                        dispatcher.dispatch(1e12)
                    switch.assert_not_called()
                self.assertEqual(seen, [])


if __name__ == '__main__': unittest.main()
