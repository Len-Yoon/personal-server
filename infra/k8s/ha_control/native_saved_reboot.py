"""Run the saved-VM boundary against the archived stopped Windows writer."""

import base64
import hashlib
from pathlib import Path, PureWindowsPath
import time

from .controller import TransitionBlocked
from .native_sameboot_release import WindowsPairClosure
from .native_windows_ssh import fixed_script
from .sameboot_transition import _closed, _digest, _pin, _save_rejected, _validate


SNAPSHOT_FIELDS = {
    'windows_boot_sha256', 'vm_id_sha256', 'states', 'plan_sha256',
    'binding_sha256', 'terminal_sha256',
}


def _need(condition):
    if not condition:
        raise TransitionBlocked('native_saved_reboot_unconfirmed')


class WindowsSavedReboot:
    def __init__(self, pair, *, script_sha256, boot_task_sha256, boot_script_sha256, vm_ids):
        _need(type(pair) is WindowsPairClosure and _pin(script_sha256)
              and _pin(boot_task_sha256) and _pin(boot_script_sha256)
              and type(vm_ids) is dict and set(vm_ids) == {'A', 'B'}
              and all(_pin(pin) for pin in vm_ids.values()) and vm_ids['A'] != vm_ids['B'])
        self.pair = pair
        self.script_sha256 = script_sha256
        self.boot_task_sha256 = boot_task_sha256
        self.boot_script_sha256 = boot_script_sha256
        self.vm_ids = vm_ids.copy()

    def _boot_task_command(self):
        raw = Path(__file__).with_name('windows-vm-boot.ps1').read_bytes()
        _need(hashlib.sha256(raw).hexdigest() == self.boot_script_sha256)
        return fixed_script('windows-vm-boot-task.ps1', self.boot_task_sha256, {
            'VmAIdSha256': self.vm_ids['A'], 'VmBIdSha256': self.vm_ids['B'],
            'ScriptSha256': self.boot_script_sha256,
            'ScriptBase64': base64.b64encode(raw).decode('ascii'), 'SavedGuestsOnly': True,
        })

    def verify_boot_task(self, deadline):
        _need(time.monotonic() < deadline)
        result = self.pair.windows.powershell(self._boot_task_command(), deadline)
        _need(result == {'result': 'already_registered', 'script_sha256': self.boot_script_sha256,
                         'task': 'N100HA-Start-VM-Guests', 'scope': 'vm_start_only'})
        boot = self.pair.config['boot_sha256']
        mode = 'CheckUnissuedReboot'
        script = fixed_script('windows-saved-reboot.ps1', self.script_sha256, {
            'Mode': mode, 'WindowsBootSha256': boot,
        })
        reservation = self.pair.windows.powershell(script, deadline)
        _need(reservation == {'mode': mode, 'windows_boot_sha256': boot, 'available': True})

    def _call(self, mode, terminal, deadline, *, resumed_boot=None, reboot_nonce=None):
        _need(time.monotonic() < deadline and _pin(terminal))
        config = self.pair.config
        old_boot = config['boot_sha256']
        parameters = {
            'Mode': mode, 'WindowsBootSha256': old_boot,
            'VmAIdSha256': self.vm_ids['A'], 'VmBIdSha256': self.vm_ids['B'],
            'PlanPath': config['plan'], 'PlanSha256': config['plan_sha256'],
            'BindingPath': config['binding'], 'BindingSha256': config['binding_sha256'],
            'TerminalPath': str(PureWindowsPath(config['plan']).parent / 'journal/controller-state.json'),
            'TerminalSha256': terminal,
        }
        if mode not in {'Observe', 'ObserveOldBootRecovery'}:
            parameters['Go'] = True
        if mode == 'ResumePair':
            _need(_pin(resumed_boot))
            parameters['ResumeBootSha256'] = resumed_boot
        if mode in {'Reboot', 'CancelUnissuedReboot', 'ObserveOldBootRecovery'} or (
                mode == 'ResumePair' and resumed_boot == old_boot):
            _need(_pin(reboot_nonce))
            parameters['RebootNonce'] = reboot_nonce
        script = fixed_script('windows-saved-reboot.ps1', self.script_sha256, parameters)
        if mode in {'SavePair', 'Reboot'}:
            script += ' -BootTaskProbe {' + self._boot_task_command() + '}'
        if mode == 'SavePair':
            probe, challenge = self.pair._command('closed', terminal)
            script += (" -ClosureProbe {" + probe + "} -ClosureChallenge '" + challenge + "'"
                       " -ClosedRevision " + str(config['revision'] + 1))
        result = self.pair.windows.powershell(script, deadline)
        _need(type(result) is dict and set(result) == SNAPSHOT_FIELDS | {'mode'}
              and result['mode'] == mode and _pin(result['windows_boot_sha256'])
              and result['vm_id_sha256'] == self.vm_ids
              and result['plan_sha256'] == config['plan_sha256']
              and result['binding_sha256'] == config['binding_sha256']
              and result['terminal_sha256'] == terminal
              and type(result['states']) is dict and set(result['states']) == {'A', 'B'})
        if mode in {'SavePair', 'Reboot', 'CancelUnissuedReboot'}:
            _need(result['windows_boot_sha256'] == old_boot
                  and result['states'] == {'A': 'Saved', 'B': 'Saved'})
        if mode == 'ResumePair':
            _need(result['windows_boot_sha256'] == resumed_boot
                  and result['states'] == {'A': 'Running', 'B': 'Running'})
        return {key: result[key] for key in SNAPSHOT_FIELDS}

    def cancel_unissued(self, operation, receipts, deadline):
        scope, stage, terminal, _ = self._bound(operation, receipts)
        _need(stage == 'reboot_windows')
        _validate('save_guests', receipts.get('save_guests'), scope, receipts)
        return self._call('CancelUnissuedReboot', terminal, deadline, reboot_nonce=operation.get('nonce'))

    def _bound(self, operation, receipts):
        scope = operation['scope']
        stage = operation['stage']
        old_boot = self.pair.config['boot_sha256']
        close = receipts['close_pair']
        terminal = close['terminal_sha256']
        revision = scope['previous_revision'] + 1
        _need(scope.get('saved_reboot') is True and scope['previous_windows_boot_sha256'] == old_boot
              and scope['saved_vm_pair_sha256'] == _digest(self.vm_ids)
              and scope['previous_plan_sha256'] == self.pair.config['plan_sha256']
              and scope['previous_instance_sha256'] == self.pair.config['binding_sha256']
              and _closed(close, revision, terminal))
        _validate('quiesce_apps', receipts.get('quiesce_apps'), scope, receipts)
        return scope, stage, terminal, revision

    def run(self, operation, receipts, deadline, *, observe_only):
        scope, stage, terminal, revision = self._bound(operation, receipts)
        old_boot = self.pair.config['boot_sha256']
        if stage == 'save_guests':
            if not observe_only:
                return self._call('SavePair', terminal, deadline)
            return self._call('Observe', terminal, deadline)

        if stage == 'reboot_windows':
            _validate('save_guests', receipts.get('save_guests'), scope, receipts)
            if not observe_only:
                self._call('Reboot', terminal, deadline, reboot_nonce=operation.get('nonce'))
                # An acknowledgement only confirms dispatch. Record success
                # when the Windows boot has actually changed.
            observed = self._call('Observe', terminal, deadline)
            _need(observed['windows_boot_sha256'] != old_boot)
            return observed

        _need(stage in {'resume_guests', 'restore_saved_guests'})
        if stage == 'restore_saved_guests':
            _need(not {'reboot_windows', 'resume_guests', 'arm_host', 'publish'}.intersection(receipts))
            if 'save_rejected' in receipts:
                _save_rejected(receipts['save_rejected'], scope, receipts)
                nonce = operation.get('nonce')
            else:
                _validate('save_guests', receipts.get('save_guests'), scope, receipts)
                cancelled = receipts.get('reboot_cancelled', {})
                _need(cancelled.get('saved') == receipts['save_guests'] and _pin(cancelled.get('nonce')))
                nonce = cancelled['nonce']
            boot = old_boot
            if observe_only:
                state = self._call('ObserveOldBootRecovery', terminal, deadline, reboot_nonce=nonce)
            else:
                state = self._call('ResumePair', terminal, deadline, resumed_boot=boot, reboot_nonce=nonce)
        else:
            _validate('save_guests', receipts.get('save_guests'), scope, receipts)
            _validate('reboot_windows', receipts.get('reboot_windows'), scope, receipts)
            boot = receipts['reboot_windows']['windows_boot_sha256']
            state = self._call('Observe' if observe_only else 'ResumePair', terminal, deadline,
                               resumed_boot=None if observe_only else boot)
        _need(state['windows_boot_sha256'] == boot and state['states'] == {'A': 'Running', 'B': 'Running'})
        fresh = (self.pair.closed_pair(terminal, deadline) if stage == 'restore_saved_guests' else
                 self.pair.closed_pair_after_reboot(terminal, boot, deadline))
        _need(_closed(fresh, revision, terminal))
        return state | {'closed_pair': fresh}
