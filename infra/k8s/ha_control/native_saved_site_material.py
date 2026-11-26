"""Publish the approved successor inputs for one saved-VM recovery.

The native service gate creates the acceptance seal separately. Neither this
publisher nor its readback configures SCM or publishes writer authority.
"""

import base64
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import stat
import sys
import tempfile
import time

from .controller import exclusive
from .initial_postboot import FILES, files_for, saved_reboot, scope as postboot_scope
from .native_sameboot_release import WINDOWS_GUARD, WindowsPairClosure
from .native_sameboot_successor import assemble_material
from .native_stdio_bridge import _hash, _need as require, _pairs
from .sameboot_transition import (
    _closed, _encoded, _expects_reboot, _fallback_attempt, _private_file, _sameboot_recovery, _validate,
    bind_successor_plan,
)

ERROR = 'saved_site_material_unconfirmed'
KEYS = {'initial-cutover-proof.key', 'initial-recovery-held_admission.key',
        'initial-recovery-provider_isolation.key'}
COPY_FILES = KEYS | {'initial-cutover-activation.json', 'initial-predecessor-service.json',
                     'initial-predecessor-state.json'}


def _need(value):
    require(value, ERROR)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _b64(raw):
    return base64.b64encode(raw).decode('ascii')


def _join_encoded(value):
    return _b64(gzip.compress(_encoded(value), mtime=0))


class SavedSitePlanAssembly:
    """Persist one exact native plan before asking Windows to publish it."""

    def __init__(self, *, pair, runtime, scope, journal, previous, vm_ids, copy_pins, private_io_sha256,
                 saved_reboot_script_sha256, read_terminal, read_evidence):
        _need(type(pair) is WindowsPairClosure and type(scope) is dict and scope.get('saved_reboot') is True
              and callable(read_terminal) and callable(read_evidence)
              and type(previous) is dict and set(previous) == {'plan', 'binding', 'args', 'stopped'}
              and all(type(value) is bytes and 0 < len(value) <= 65536 for value in previous.values())
              and _sha(previous['plan']) == scope['previous_plan_sha256'] == pair.config['plan_sha256']
              and _sha(previous['binding']) == scope['previous_instance_sha256'] == pair.config['binding_sha256'])
        self.pair, self.runtime, self.scope = pair, runtime, scope.copy()
        self.previous, self.vm_ids = previous.copy(), vm_ids.copy()
        self.copy_pins, self.private_io_sha256 = copy_pins.copy(), private_io_sha256
        self.saved_reboot_script_sha256 = saved_reboot_script_sha256
        self.read_terminal, self.read_evidence = read_terminal, read_evidence
        self.journal = Path(journal)
        self.path = self.journal / 'planned-successor.json'

    def _private(self):
        if sys.platform == 'win32':
            from .windows_protected_io import WindowsProtectedIO

            WindowsProtectedIO(admin_only=True).assert_directory(self.journal)
            return
        info = self.journal.lstat()
        _need(self.journal.is_absolute() and self.journal.resolve(strict=True) == self.journal
              and stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
              and stat.S_IMODE(info.st_mode) == 0o700)
        for parent in self.journal.parents:
            info = parent.lstat()
            sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            _need(stat.S_ISDIR(info.st_mode) and info.st_uid in {0, os.geteuid()}
                  and (not info.st_mode & 0o022 or sticky_root))

    def _prepare(self, operation, chain, deadline):
        reboot = _expects_reboot(self.scope, chain)
        attempt = self.scope['attempt_sha256'] if reboot else _fallback_attempt(self.scope)
        terminal = self.read_terminal(operation, chain, deadline)
        _need(type(terminal) is bytes and _sha(terminal) == chain['retire_provider']['terminal_sha256'])
        runtime = self.runtime.material(self.scope, chain['stage_runtime'])
        data = self.read_evidence(operation, chain, deadline)
        _need(type(data) is dict and set(data) == {'evidence', 'pre_save_seal', 'runtime_manifest', 'fallback_observers'})
        evidence = data['evidence']
        branch = chain['resume_guests'] if reboot else _sameboot_recovery(chain)
        carryover = chain.get('fallback_carryover', chain['carryover'])
        _need(evidence['closed_pair_sha256'] == branch['closed_pair']['closed_proof_sha256']
              and evidence['carryover_sha256'] == carryover['carryover_sha256'])
        if 'verify_restored_pair' in chain:
            _validate('connect_rollback_held', chain.get('connect_rollback_held'), self.scope, chain)
            _need({node: data['vm_id_sha256'] for node, data in evidence['guests'].items()}
                  == branch['vm_id_sha256'])
        if reboot:
            _need(evidence['new_windows_boot_sha256'] == branch['windows_boot_sha256']
                  and type(data['runtime_manifest']) is bytes
                  and _sha(data['runtime_manifest']) == self.scope['runtime_manifest_sha256']
                  and type(data['pre_save_seal']) is bytes and data['fallback_observers'] is None)
            observers = chain['rebind_observers']['observers']
        else:
            _need(data['runtime_manifest'] is None and data['pre_save_seal'] is None
                  and set(evidence.get('observed', {})) == {'A', 'B'}
                  and type(data['fallback_observers']) is dict
                  and set(data['fallback_observers']) == {'legacy', 'held_admission', 'provider_isolation'})
            observers = data['fallback_observers']
            if 'verify_restored_pair' in chain:
                _need(observers['held_admission'] == chain['connect_rollback_held']['held_endpoint'])
        prior = self.previous
        material = assemble_material(prior['plan'], terminal, prior['binding'], prior['args'], prior['stopped'],
                                     previous_sha256=self.scope['previous_plan_sha256'], attempt=attempt,
                                     successor_image_sha256=self.scope['successor_image_sha256'], runtime=runtime,
                                     observers=observers, evidence=evidence, pre_save_seal=data['pre_save_seal'],
                                     reboot_runtime_manifest=data['runtime_manifest'])
        plan = material['candidate']
        approval = _encoded({'schema_version': 1, 'action': 'ha-resume-initial-writer',
                              'authority_kind': 'installed-runtime', 'plan_sha256': material['plan_pin'],
                              'installation_id': plan['installation_id'], 'source_commit': plan['source_commit'],
                              'installed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')})
        settings = {'approval': _b64(approval), 'candidate': plan, 'plan_pin': material['plan_pin'],
                    'files': {name: _b64(raw) for name, raw in material['files'].items()},
                    'runtime': runtime, 'attempt': attempt, 'prior_receipt_sha256': operation['previous_sha256']}
        encoded = _encoded(settings)
        _need(len(encoded) <= 524288)
        if sys.platform == 'win32':
            from .windows_protected_io import WindowsProtectedIO

            WindowsProtectedIO(admin_only=True).write_new(self.path, encoded)
            return
        with tempfile.NamedTemporaryFile(dir=self.journal, prefix='.successor-', delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
                stream.close()
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
        fd = os.open(self.journal, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _load(self, operation, chain, deadline, *, prepare=False):
        _need(operation['scope'] == self.scope and time.monotonic() < deadline)
        self._private()
        try:
            raw = _private_file(self.path, 524288)
        except FileNotFoundError:
            _need(prepare and operation['stage'] == 'publish_material' and _hash(operation['previous_sha256']))
            lock = exclusive
            if sys.platform == 'win32':
                from .windows_protected_io import WindowsProtectedIO

                lock = WindowsProtectedIO(admin_only=True).exclusive
            with lock(self.journal / 'plan.lock'):
                try:
                    raw = _private_file(self.path, 524288)
                except FileNotFoundError:
                    self._prepare(operation, chain, deadline)
                    raw = _private_file(self.path, 524288)
        stored = json.loads(raw, object_pairs_hook=_pairs)
        _need(_encoded(stored) == raw)
        if operation['stage'] == 'publish_material':
            _need(stored['prior_receipt_sha256'] == operation['previous_sha256'])
        else:
            receipt = chain['publish_material']
            _need(stored['plan_pin'] == receipt['plan_sha256']
                  and _sha(base64.b64decode(stored['approval'], validate=True)) == receipt['approval_sha256'])
        material = {'candidate': stored['candidate'], 'plan_pin': stored['plan_pin'],
                    'files': {name: base64.b64decode(data, validate=True) for name, data in stored['files'].items()},
                    'previous_raw': self.previous['plan']}
        bind_successor_plan(**material, allow_saved_reboot=_expects_reboot(self.scope, chain))
        return stored, material

    def _publisher(self, operation, chain, deadline, *, prepare):
        stored, material = self._load(operation, chain, deadline, prepare=prepare)
        manifest = None
        if _expects_reboot(self.scope, chain):
            recovery = material['candidate']['initial_cutover_recovery']['attempt_id']
            pin = self.scope['runtime_manifest_sha256']
            manifest = str(PureWindowsPath(r'C:\ProgramData') / ('N100HA_' + recovery[:24]) /
                           ('incoming-runtime-' + pin[:24]) / 'runtime-manifest.json')
        return WindowsSavedSiteMaterial(self.pair, material=material, approval=base64.b64decode(stored['approval']),
                                       vm_ids=self.vm_ids, copy_pins=self.copy_pins,
                                       private_io_sha256=self.private_io_sha256,
                                       saved_reboot_script_sha256=self.saved_reboot_script_sha256,
                                       material_manifest_path=manifest)

    def prepare_material(self, operation, chain, deadline):
        publisher = self._publisher(operation, chain, deadline, prepare=True)
        publisher.verify(deadline, mode='preflight')
        return True

    def saved_material(self, operation, chain, deadline):
        return self._publisher(operation, chain, deadline, prepare=False)

    def read_arm_material(self, operation, chain, deadline):
        return self._load(operation, chain, deadline)[1]

    def successor_options(self, operation, chain, material, deadline):
        stored, expected = self._load(operation, chain, deadline)
        _need(expected == material)
        plan = material['candidate']
        root = PureWindowsPath(plan['initial_service_start']['binding_path']).parent
        source = PureWindowsPath(stored['runtime']['path']) / 'controller/files/ha_control'
        return {'approval_path': str(root / 'approval.json'), 'approval_sha256': chain['publish_material']['approval_sha256'],
                'preflight_path': str(source / 'windows-service-preflight.ps1'),
                'scm_path': str(source / 'stopped_service_installer/StoppedService.cs'),
                'starter_path': str(source / 'windows-ha-start-initial-service.ps1'),
                'starter_sha256': self.runtime.starter_sha256,
                'reservation_sha256': plan['initial_service_start']['reservation_receipt_sha256']}


class WindowsSavedSiteMaterial:
    def __init__(self, pair, *, material, approval, copy_pins, vm_ids, private_io_sha256,
                 saved_reboot_script_sha256, material_manifest_path=None):
        _need(type(pair) is WindowsPairClosure and type(approval) is bytes and 0 < len(approval) <= 8192
              and type(copy_pins) is dict and set(copy_pins) == COPY_FILES and all(_hash(x) for x in copy_pins.values())
              and type(vm_ids) is dict and set(vm_ids) == {'A', 'B'} and all(_hash(x) for x in vm_ids.values())
              and vm_ids['A'] != vm_ids['B'])
        candidate, files = material['candidate'], material['files']
        prev = json.loads(material['previous_raw'], object_pairs_hook=_pairs)
        postboot = postboot_scope(candidate)
        is_saved = saved_reboot(postboot)
        checked = bind_successor_plan(**material, allow_saved_reboot=is_saved)
        binding = pair.config
        previous_root = PureWindowsPath(binding['plan']).parent
        root = previous_root.parent / postboot['attempt_id'][:40]
        current_boot = candidate['windows_initial_observer']['windows_boot_sha256']
        _need(binding['plan_sha256'] == checked['previous_plan_sha256']
              and binding['binding_sha256'] == postboot['predecessor_binding_sha256']
              and binding['binding'] == prev['initial_service_start']['binding_path']
              and binding['boot_sha256'] == postboot['previous_windows_boot_sha256']
              and str(previous_root / 'plan.json') == binding['plan']
              and previous_root.parent == PureWindowsPath(r'C:\ProgramData\N100HA-InitialCutover')
              and previous_root != root
              and _hash(current_boot) and (current_boot != binding['boot_sha256']) == is_saved)
        approval_doc = json.loads(approval, object_pairs_hook=_pairs)
        _need(set(approval_doc) == {'schema_version', 'action', 'authority_kind', 'installed_at',
                                    'installation_id', 'plan_sha256', 'source_commit'}
              and type(approval_doc['schema_version']) is int and approval_doc['schema_version'] == 1
              and approval_doc['action'] == 'ha-resume-initial-writer'
              and approval_doc['authority_kind'] == 'installed-runtime'
              and approval_doc['source_commit'] == candidate['source_commit']
              and approval_doc['installation_id'] == candidate['installation_id']
              and approval_doc['plan_sha256'] == checked['plan_sha256'])
        recovery = candidate['initial_cutover_recovery']['attempt_id']
        _need(_hash(recovery))
        expected_names = set(files_for(candidate))
        manifest_leaf = 'postboot-runtime-manifest.json'
        manifest_pin = None
        if is_saved:
            manifest_pin = candidate['saved_reboot_runtime_manifest_sha256']
            expected_names.add(manifest_leaf)
            expected_source = PureWindowsPath(r'C:\ProgramData') / ('N100HA_' + recovery[:24]) / (
                'incoming-runtime-' + manifest_pin[:24]) / 'runtime-manifest.json'
            _need(material_manifest_path == str(expected_source) and _sha(files[manifest_leaf]) == manifest_pin)
        else:
            _need(material_manifest_path is None)
        _need(set(files) == expected_names)
        for role in ('held_admission', 'provider_isolation'):
            pin = copy_pins[f'initial-recovery-{role}.key']
            _need(pin == prev['windows_recovery_observers'][role]['key_sha256']
                  == candidate['windows_recovery_observers'][role]['key_sha256'])
        _need(copy_pins['initial-cutover-proof.key'] == prev['windows_initial_observer']['key_sha256']
              == candidate['windows_initial_observer']['key_sha256'])

        module = Path(__file__).parent
        io = (module / 'stopped_service_installer' / 'PrivateIo.cs').read_bytes()
        guard = (module / 'windows-saved-reboot.ps1').read_bytes()
        _need(_hash(private_io_sha256) and _sha(io) == private_io_sha256
              and _hash(saved_reboot_script_sha256) and _sha(guard) == saved_reboot_script_sha256)
        entries = {leaf: [_sha(raw), _b64(raw)] for leaf, raw in
                   (files | {'plan.json': _encoded(candidate), 'approval.json': approval}).items()
                   if leaf not in {FILES[5], manifest_leaf}}
        _need(all(len(raw) <= 65536 for leaf, raw in files.items() if leaf != manifest_leaf))
        self.pair, self.is_saved = pair, is_saved
        self.config = {'root': str(root), 'old_root': str(previous_root), 'boot': current_boot,
                       'old_boot': binding['boot_sha256'], 'plan_path': binding['plan'],
                       'old_plan': binding['plan_sha256'], 'old_binding_path': binding['binding'],
                       'old_binding': binding['binding_sha256'], 'terminal': checked['terminal_sha256'],
                       'vm_ids': vm_ids.copy(), 'sid': prev['windows_enforced']['service_sid'],
                       'old_image': prev['windows_enforced']['service_image_sha256'],
                       'old_image_path': prev['initial_service_start']['service_image_path_sha256'],
                       'old_args': postboot['predecessor_args_sha256'], 'copies': copy_pins.copy(),
                       'io': _b64(io), 'io_pin': private_io_sha256, 'guard': _b64(guard),
                       'guard_pin': saved_reboot_script_sha256, 'manifest_path': material_manifest_path,
                       'manifest_pin': manifest_pin, 'entries': entries}
        self.plan_pin = checked['plan_sha256']
        self.approval_pin = _sha(approval)

    def _closure(self, deadline):
        terminal = self.config['terminal']
        closed = (self.pair.closed_pair_after_reboot(terminal, self.config['boot'], deadline)
                  if self.is_saved else self.pair.closed_pair(terminal, deadline))
        _need(_closed(closed, self.pair.config['revision'] + 1, terminal))

    def verify(self, deadline, *, mode='observe'):
        """Reconcile only a missing or complete target; a partial target blocks."""
        _need(mode in {'preflight', 'apply', 'observe', 'reconcile'} and time.monotonic() < deadline)
        self._closure(deadline)
        config = _join_encoded(self.config | {'mode': mode})
        helpers = WINDOWS_GUARD[WINDOWS_GUARD.index('function Pin'):]
        source = NATIVE.replace('__CONFIG__', config).replace('__GUARD_HELPERS__', helpers)
        _need(len(source.encode()) <= 131072)
        result = self.pair.windows.powershell(source, deadline)
        _need(result == {'status': 'MATERIAL_VERIFIED', 'mode': mode, 'plan_sha256': self.plan_pin,
                         'approval_sha256': self.approval_pin, 'authority_issued': False})
        self._closure(deadline)
        return result


NATIVE = r"""
$ErrorActionPreference='Stop';Set-StrictMode -Version Latest
$enc=[Text.UTF8Encoding]::new($false,$true)
$mem=[IO.MemoryStream]::new([Convert]::FromBase64String('__CONFIG__'))
$zip=[IO.Compression.GZipStream]::new($mem,[IO.Compression.CompressionMode]::Decompress)
$reader=[IO.StreamReader]::new($zip,$enc)
try{$cfg=ConvertFrom-Json ($reader.ReadToEnd())}finally{$reader.Dispose()}
$allowed=@('S-1-5-18','S-1-5-32-544')
$held=[Collections.Generic.List[IDisposable]]::new()
$mutex=[Threading.Mutex]::new($false,'Global\N100HA-Start-VM-Guests');$locked=$false
__GUARD_HELPERS__
function Need([bool]$ok){if(-not $ok){throw 'saved_material_unconfirmed'}}
function NativeGuard {
 $raw=[Convert]::FromBase64String($cfg.guard);Need ((Pin $raw) -ceq $cfg.guard_pin)
 $terminal=Join-Path $cfg.old_root 'journal\controller-state.json'
 $observed=@(& ([scriptblock]::Create($enc.GetString($raw))) -Mode Observe -VmAIdSha256 $cfg.vm_ids.A -VmBIdSha256 $cfg.vm_ids.B -WindowsBootSha256 $cfg.old_boot -PlanPath $cfg.plan_path -PlanSha256 $cfg.old_plan -BindingPath $cfg.old_binding_path -BindingSha256 $cfg.old_binding -TerminalPath $terminal -TerminalSha256 $cfg.terminal)
 Need ($observed.Count -eq 1)
 $receipt=ConvertFrom-Json $observed[0]
 Need ($receipt.mode -ceq 'Observe' -and $receipt.windows_boot_sha256 -ceq $cfg.boot -and
       $receipt.states.A -ceq 'Running' -and $receipt.states.B -ceq 'Running' -and
       $receipt.plan_sha256 -ceq $cfg.old_plan -and $receipt.binding_sha256 -ceq $cfg.old_binding -and
       $receipt.terminal_sha256 -ceq $cfg.terminal)
}
function HoldExact([string]$path,[string]$pin,[long]$limit,[string]$sid=''){
 $null=Assert-Path $path $false $sid
 $h=[TrustedStoppedV1.Held]::new($path,$false);$held.Add($h)
 Need ($h.Bytes.Length -gt 0 -and $h.Bytes.Length -le $limit -and (Pin $h.Bytes) -ceq $pin)
 return ,$h.Bytes
}
function VerifyEmptyJournal([string]$path){
 $journal=Get-Item -LiteralPath $path -Force -ErrorAction Stop
 Need ($journal -is [IO.DirectoryInfo] -and -not ($journal.Attributes -band [IO.FileAttributes]::ReparsePoint))
 $sections=[Security.AccessControl.AccessControlSections]'Owner,Group,Access'
 $expected=[Security.AccessControl.RawSecurityDescriptor]::new([TrustedStoppedV1.NewPrivate]::Acl($cfg.sid,$true))
 $actual=[Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $path -ErrorAction Stop).Sddl)
 Need ($actual.GetSddlForm($sections) -ceq $expected.GetSddlForm($sections) -and
       @(Get-ChildItem -LiteralPath $path -Force -ErrorAction Stop).Count -eq 0)
}
try{
 Need ($PSVersionTable.PSEdition -ceq 'Desktop' -and [Environment]::Is64BitProcess -and $cfg.mode -cin @('preflight','apply','observe','reconcile'))
 try{$locked=$mutex.WaitOne(0)}catch [Threading.AbandonedMutexException]{$locked=$true;throw 'saved_material_lock'}
 Need $locked
 NativeGuard
 $io=[Convert]::FromBase64String($cfg.io);Need ((Pin $io) -ceq $cfg.io_pin)
 $support=$enc.GetString($io).Replace('namespace TrustedStoppedV1 {',@'
namespace TrustedStoppedV1 {
 public sealed class Failure : Exception { public Failure(int code) : base("saved_material_io") {} }
 internal static class Contract { internal static void Need(bool ok) { if(!ok) throw new Failure(0); } }
'@)
 Add-Type -TypeDefinition $support -ErrorAction Stop
 $null=Assert-Path ([IO.Path]::GetDirectoryName($cfg.root)) $true $cfg.sid
 $null=Assert-Path $cfg.old_root $true $cfg.sid
 $held.Add([TrustedStoppedV1.Held]::new([IO.Path]::GetDirectoryName($cfg.root),$true))
 $held.Add([TrustedStoppedV1.Held]::new($cfg.old_root,$true))
 $old=ConvertFrom-Json ($enc.GetString((HoldExact $cfg.plan_path $cfg.old_plan 65536 $cfg.sid)))
 $null=HoldExact $cfg.old_binding_path $cfg.old_binding 8192 $cfg.sid
 # NativeGuard checked the service-owned journal ACL. Keep its exact
 # terminal read-locked while writing the independent successor inputs.
 $terminal=[TrustedStoppedV1.Held]::new((Join-Path $cfg.old_root 'journal\controller-state.json'),$false)
 $held.Add($terminal)
 Need ($terminal.Bytes.Length -gt 0 -and $terminal.Bytes.Length -le 8192 -and (Pin $terminal.Bytes) -ceq $cfg.terminal)
 Need ($old.windows_enforced.service_name -cmatch '\AN100HA_[a-f0-9]{24}_controller\z' -and $old.windows_enforced.service_sid -ceq $cfg.sid)
 $service=Get-CimInstance Win32_Service -Filter ("Name='"+$old.windows_enforced.service_name+"'") -OperationTimeoutSec 5
 Need ($service.State -ceq 'Stopped' -and $service.StartMode -ceq 'Disabled' -and $service.ProcessId -eq 0 -and
       (Pin ($enc.GetBytes($service.PathName))) -ceq $cfg.old_image_path -and
       $service.PathName -cmatch '\A"C:\\[A-Za-z0-9_.\\-]+\\windows-service-host\.exe"\z')
 $image=$service.PathName.Trim('"')
 $null=HoldExact $image $cfg.old_image 67108864 $cfg.sid
 $null=HoldExact (Join-Path ([IO.Path]::GetDirectoryName($image)) 'service.args') $cfg.old_args 16384 $cfg.sid
 $items=@{}
 foreach($field in $cfg.copies.PSObject.Properties){
  $items[$field.Name]=@{pin=$field.Value;bytes=(HoldExact (Join-Path $cfg.old_root $field.Name) $field.Value 65536 $cfg.sid)}
 }
 foreach($field in $cfg.entries.PSObject.Properties){
  $bytes=[Convert]::FromBase64String($field.Value[1])
  Need ($field.Name -cmatch '\A[a-zA-Z0-9_.-]+\z' -and $bytes.Length -le 65536 -and (Pin $bytes) -ceq $field.Value[0])
  $items[$field.Name]=@{pin=$field.Value[0];bytes=$bytes}
 }
 if($cfg.manifest_path){
  $items['postboot-runtime-manifest.json']=@{pin=$cfg.manifest_pin;bytes=(HoldExact $cfg.manifest_path $cfg.manifest_pin 1048576)}
 }
 $journal=Join-Path $cfg.root 'journal'
 # Only absence under the native mutex permits a deferred first publication.
 # An existing directory is execution evidence, so validate it without writes.
 $exists=Test-Path -LiteralPath $cfg.root
 $create=$false
 if($cfg.mode -ceq 'apply'){Need (-not $exists);$create=$true}
 if($cfg.mode -ceq 'reconcile'){$create=-not $exists}
 if($cfg.mode -ceq 'preflight'){Need (-not $exists)}
 if($create){
  foreach($h in $held){$h.Verify()}
  NativeGuard
  [TrustedStoppedV1.NewPrivate]::Directory($cfg.root,$cfg.sid,$false)
 }
 if($cfg.mode -cne 'preflight'){
  $null=Assert-Path $cfg.root $true $cfg.sid
  $held.Add([TrustedStoppedV1.Held]::new($cfg.root,$true))
  if($create){
   [TrustedStoppedV1.NewPrivate]::Directory($journal,$cfg.sid,$true)
   foreach($entry in $items.GetEnumerator()){
    $p=Join-Path $cfg.root $entry.Key
    [TrustedStoppedV1.NewPrivate]::File($p,$entry.Value.bytes,$cfg.sid,$true)
   }
  }
  $children=@(Get-ChildItem -LiteralPath $cfg.root -Force -ErrorAction Stop)
  $names=@($items.Keys)+@('journal')
  Need ($children.Count -eq $names.Count -and @($children|Where-Object{$_.Name -cnotin $names}).Count -eq 0)
  foreach($entry in $items.GetEnumerator()){$null=HoldExact (Join-Path $cfg.root $entry.Key) $entry.Value.pin 1048576 $cfg.sid}
  VerifyEmptyJournal $journal
 }
 foreach($h in $held){$h.Verify()}
 NativeGuard
 @{status='MATERIAL_VERIFIED';mode=$cfg.mode;plan_sha256=$cfg.entries.'plan.json'[0];
   approval_sha256=$cfg.entries.'approval.json'[0];authority_issued=$false}|ConvertTo-Json -Compress
}finally{foreach($h in $held){$h.Dispose()};if($locked){$mutex.ReleaseMutex()};$mutex.Dispose()}
"""
