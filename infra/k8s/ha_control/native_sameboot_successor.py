"""Build and invoke the pinned Windows successor for an already closed A/B pair."""

import base64
import hashlib
from pathlib import PureWindowsPath
import re
import time

from .controller import TransitionBlocked
from .initial_postboot import FILES, SAMEBOOT_KIND, SAVED_REBOOT_KIND, saved_reboot, scope, stopped_attempt
from .initial_unpublished import derive_predecessor
from .initial_windows_gate import _args, _decode, _path
from .sameboot_transition import _encoded, bind_successor_plan

ERROR = 'sameboot_successor_unconfirmed'
HASH = re.compile('[a-f0-9]{64}')
PROVENANCE = ('native_windows_code_sha256', 'controller_code_sha256', 'legacy_linux_sha256',
              'legacy_guard_code_sha256', 'windows_guard_sha256')
ENDPOINT_FIELDS = {'pipe_name', 'server_pid', 'key_sha256'}
START_FIELDS = {'ServiceName', 'ServiceHostSha256', 'PlanPath', 'PlanSha256', 'ApprovalPath',
                'ApprovalSha256', 'BindingPath', 'PreflightPath', 'PreflightSha256',
                'ScmVerifierPath', 'ExpectedActivationIntentSha256', 'RecoveryAttemptId',
                'RecoveryReservationSha256'}
STARTER_FILE_MAX_BYTES = 160 * 1024


def _need(condition):
    if not condition:
        raise TransitionBlocked(ERROR)


def _pin(value):
    return type(value) is str and HASH.fullmatch(value) is not None


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def host_path(recovery, attempt):
    _need(_pin(recovery) and _pin(attempt))
    return PureWindowsPath('C:/ProgramData') / ('N100HA_' + recovery[:24]) / (
        'postboot-' + attempt) / 'host' / 'windows-service-host.exe'


def draft_postboot(previous_raw, terminal_raw, binding_raw, args_raw, stop_raw, *,
                   previous_sha256, attempt, successor_image_sha256, saved_reboot=False):
    """Construct fixed identity fields for the guest's one-shot carryover request."""
    _need(all(type(raw) is bytes and 0 < len(raw) <= 262144
              for raw in (previous_raw, terminal_raw, binding_raw, args_raw, stop_raw)))
    _need(_sha(previous_raw) == previous_sha256 and _pin(attempt) and _pin(successor_image_sha256)
          and type(saved_reboot) is bool)
    old = _decode(previous_raw)
    previous = scope(old)
    terminal = _decode(terminal_raw)
    old_binding = _decode(binding_raw)
    old_args = _args(args_raw)
    derive_predecessor(terminal_raw, _sha(terminal_raw), published=True)
    _need(terminal['controller_plan_sha256'] == old_binding['controller_plan_sha256'] ==
          old_args['plan_sha256'] == previous_sha256 and terminal['postboot'] == previous)
    _need(terminal['recovery'] == old['initial_cutover_recovery']
          and attempt not in {previous['attempt_id'], stopped_attempt(previous)})
    stop_pin = _sha(stop_raw)
    _need(previous.get('stopped_receipt_sha256', stop_pin) == stop_pin)
    identity = old['windows_enforced']
    _need(identity['service_image_sha256'] == old_args['host_sha256'] != successor_image_sha256)
    boot = old['windows_initial_observer']['windows_boot_sha256']
    image_path = host_path(terminal['recovery']['attempt_id'], attempt)
    return {'schema_version': 1, 'attempt_id': attempt, 'predecessor_terminal_sha256': _sha(terminal_raw),
            'predecessor_binding_sha256': _sha(binding_raw), 'predecessor_args_sha256': _sha(args_raw),
            'predecessor_seal_sha256': '0' * 64, 'previous_windows_boot_sha256': boot,
            'closure_mode': 'windows-offline-v1',
            'predecessor_kind': SAVED_REBOOT_KIND if saved_reboot else SAMEBOOT_KIND,
            'predecessor_plan_sha256': previous_sha256, 'stopped_attempt_id': stopped_attempt(previous),
            'stopped_receipt_sha256': stop_pin, 'carryover_receipt_sha256': '0' * 64,
            'previous_service_image_sha256': identity['service_image_sha256'],
            'successor_service_image_sha256': successor_image_sha256,
            'successor_service_image_path_sha256': _sha(('"' + str(image_path) + '"').encode())}


def _endpoint(old, current):
    _need(type(current) is dict and current.keys() == ENDPOINT_FIELDS)
    _need(type(current['server_pid']) is int and current['server_pid'] > 0 and _pin(current['key_sha256'])
          and type(current['pipe_name']) is str and re.fullmatch(
              r'n100ha-(?:lease|recovery)-proof-[0-9a-f]{32}', current['pipe_name']))
    return old | current


def assemble_material(previous_raw, terminal_raw, binding_raw, args_raw, stop_raw, *,
                      previous_sha256, attempt, successor_image_sha256, runtime, observers, evidence,
                      pre_save_seal=None, reboot_runtime_manifest=None):
    """Prepare native input bytes from verified runtime, closure and observer receipts.

    The acceptance file in this result is the expected native result. The Windows
    sealer creates the actual file after checking the real predecessor and SCM.
    """
    reboot = pre_save_seal is not None
    _need((not reboot and reboot_runtime_manifest is None) or
          (reboot and type(pre_save_seal) is bytes and 0 < len(pre_save_seal) <= 8192
           and type(reboot_runtime_manifest) is bytes and 0 < len(reboot_runtime_manifest) <= 1048576))
    postboot = draft_postboot(previous_raw, terminal_raw, binding_raw, args_raw, stop_raw,
                             previous_sha256=previous_sha256, attempt=attempt,
                             successor_image_sha256=successor_image_sha256, saved_reboot=reboot)
    old = _decode(previous_raw)
    identity = old['windows_enforced']
    recovery = old['initial_cutover_recovery']['attempt_id']
    root = PureWindowsPath('C:/ProgramData/N100HA-InitialCutover') / attempt[:40]
    reserve = PureWindowsPath('C:/ProgramData') / ('N100HA_' + recovery[:24])
    _need(type(runtime) is dict and set(runtime) == {'path', 'manifest_sha256', 'bundle_sha256',
          'provenance', 'python_sha256', 'launcher_sha256', 'preflight_sha256', 'scm_sha256'})
    runtime_directory = _path(runtime['path'])
    _need(all(_pin(runtime[name]) for name in runtime if name.endswith('_sha256')) and
          runtime_directory == reserve / ('runtime-' + runtime['manifest_sha256'][:24]))
    provenance = runtime['provenance']
    _need(type(provenance) is dict and type(provenance.get('source_commit')) is str and
          re.fullmatch('[a-f0-9]{40}', provenance['source_commit']) and
          all(_pin(provenance.get(name)) for name in (*PROVENANCE, 'enforced_adapter_sha256',
              'enforced_driver_sha256', 'enforced_definition_sha256', 'effects_runtime_sha256')))
    _need(old['pg_enforcement']['definition_sha256'] == provenance['enforced_definition_sha256']
          and old['pg_enforcement']['effects']['code_sha256'] == provenance['effects_runtime_sha256'])
    _need(set(observers) == {'legacy', 'held_admission', 'provider_isolation'})
    plan = old | {**{key: provenance[key] for key in PROVENANCE},
                  'source_commit': provenance['source_commit'], 'controller_bundle_sha256': runtime['bundle_sha256']}
    if reboot:
        plan['saved_reboot_runtime_manifest_sha256'] = runtime['manifest_sha256']
    else:
        plan.pop('saved_reboot_runtime_manifest_sha256', None)
    python = runtime_directory / 'python' / 'python.exe'
    launcher = runtime_directory / 'controller' / 'files' / 'ha_control' / 'launcher.py'
    plan['windows_enforced'] = identity | {
        'service_image_sha256': successor_image_sha256, 'adapter_sha256': provenance['enforced_adapter_sha256'],
        'driver_sha256': provenance['enforced_driver_sha256'], 'python_path': str(python),
        'launcher_path': str(launcher), 'python_sha256': runtime['python_sha256'],
        'launcher_sha256': runtime['launcher_sha256']}
    plan['initial_service_start'] = old['initial_service_start'] | {
        'binding_path': str(root / ('initial-recovery-service-' + recovery + '.json')),
        'service_image_path_sha256': postboot['successor_service_image_path_sha256'],
        'preflight_sha256': runtime['preflight_sha256'], 'scm_verifier_sha256': runtime['scm_sha256']}
    old_recovery = old['windows_recovery_observers']
    plan['windows_initial_observer'] = _endpoint(old['windows_initial_observer'], observers['legacy'])
    plan['windows_recovery_observers'] = old_recovery | {
        name: _endpoint(old_recovery[name], observers[name]) for name in ('held_admission', 'provider_isolation')}
    _need(all(len({record[key] for record in observers.values()}) == 3 for key in ENDPOINT_FIELDS))
    _need(observers['held_admission']['server_pid'] != old_recovery['held_admission']['server_pid'])
    state_fields = ('saved', 'resumed') if reboot else ('observed',)
    boot_fields = {'new_windows_boot_sha256'} if reboot else set()
    _need(type(evidence) is dict and set(evidence) == {'closed_pair_sha256', 'closed_revision', 'carryover_sha256',
          'guests', 'guest_observation_sha256', 'clock_ca_validated', 'fresh_vsock'} | set(state_fields) | boot_fields)
    closed, _, _ = derive_predecessor(terminal_raw, postboot['predecessor_terminal_sha256'], published=True)
    _need(type(evidence['closed_revision']) is int and evidence['closed_revision'] == closed)
    _need(_pin(evidence['closed_pair_sha256']) and _pin(evidence['carryover_sha256']))
    postboot['carryover_receipt_sha256'] = evidence['carryover_sha256']
    previous_boot = postboot['previous_windows_boot_sha256']
    boot = evidence.get('new_windows_boot_sha256', previous_boot)
    _need(_pin(boot) and ((boot != previous_boot) == reboot))
    plan['windows_initial_observer']['windows_boot_sha256'] = boot
    service = {'service_name': identity['service_name'], 'service_sid': identity['service_sid'],
               'state': 'Stopped', 'start_mode': 'Disabled', 'process_id': 0,
               'service_image_path_sha256': old['initial_service_start']['service_image_path_sha256']}
    seal = {'schema_version': 1, 'windows_boot_sha256': previous_boot, 'service': service, 'guests': evidence['guests'],
            'hold_sha256': old_recovery['hold_sha256'], 'closed_revision': closed,
            'closed_pair_sha256': evidence['closed_pair_sha256'],
            'postboot_stopped_receipt_sha256': postboot['stopped_receipt_sha256']}
    seal.update({key: postboot[key] for key in (
        'predecessor_terminal_sha256', 'predecessor_binding_sha256', 'predecessor_args_sha256')})
    seal_raw = pre_save_seal if reboot else _encoded(seal)
    if reboot:
        archived = _decode(seal_raw)
        _need(type(archived) is dict and _pin(archived.get('closed_pair_sha256'))
              and archived == seal | {'closed_pair_sha256': archived['closed_pair_sha256'],
                                      'saved': evidence['saved']})
    postboot['predecessor_seal_sha256'] = _sha(seal_raw)
    plan['initial_recovery_postboot'] = postboot
    plan_pin = _sha(_encoded(plan))
    resumed = {key: evidence[key] for key in ('guests', *state_fields, 'guest_observation_sha256', 'clock_ca_validated',
                                             'fresh_vsock', 'closed_revision', 'closed_pair_sha256')}
    resumed.update(schema_version=1, plan_sha256=plan_pin, windows_boot_sha256=boot, previous_windows_boot_sha256=previous_boot,
                   predecessor_seal_sha256=_sha(seal_raw), hold_sha256=seal['hold_sha256'],
                   postboot_stopped_receipt_sha256=postboot['stopped_receipt_sha256'])
    resumed_raw = _encoded(resumed)
    accepted = {'schema_version': 1, 'plan_sha256': plan_pin, 'postboot': postboot,
                'windows_boot_sha256': boot, 'resume_evidence_sha256': _sha(resumed_raw), 'service': service}
    files = dict(zip(FILES, (terminal_raw, binding_raw, args_raw, seal_raw, resumed_raw, _encoded(accepted), stop_raw),
                     strict=True))
    files['postboot-predecessor-plan.json'] = previous_raw
    if reboot:
        files['postboot-runtime-manifest.json'] = reboot_runtime_manifest
    bind_successor_plan(plan, previous_raw, plan_pin, files, allow_saved_reboot=reboot)
    return {'candidate': plan, 'previous_raw': previous_raw, 'plan_pin': plan_pin, 'files': files}


_PINNED_READ = r"""
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$hashPattern='\A[a-f0-9]{64}\z'
$utf=[Text.UTF8Encoding]::new($false,$true)
$cfg=ConvertFrom-Json ($utf.GetString([Convert]::FromBase64String('__CONFIG__')))
function Digest([byte[]]$raw){
 $sha=[Security.Cryptography.SHA256]::Create()
 try {return [BitConverter]::ToString($sha.ComputeHash($raw)).Replace('-','').ToLowerInvariant()}
 finally {$sha.Dispose()}
}
function Read-Safe([string]$Path,[int]$Maximum){
 $item=Get-Item -LiteralPath $Path -Force
 if($item -isnot [IO.FileInfo] -or $item.Length -lt 1 -or $item.Length -gt $Maximum){throw 'sameboot_successor_input'}
 for($node=$item;$null-ne $node;$node=if($node -is [IO.FileInfo]){$node.Directory}else{$node.Parent}){
    if($node.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'sameboot_successor_path'}
 }
 $fd=[IO.File]::Open($item.FullName,'Open','Read',[IO.FileShare]::Read)
 try {
   $reader=[IO.BinaryReader]::new($fd)
   try {$body=$reader.ReadBytes($Maximum+1)} finally {$reader.Dispose()}
   if($body.Length -lt 1 -or $body.Length -gt $Maximum){throw 'sameboot_successor_size'}
   return ,$body
 } finally {$fd.Dispose()}
}
function Read-Pin([string]$Path,[string]$Pin,[int]$Maximum){
 $raw=Read-Safe $Path $Maximum
 if((Digest $raw) -cne $Pin){throw 'sameboot_successor_pin'}
 return ,$raw
}
$plan=ConvertFrom-Json ($utf.GetString((Read-Pin $cfg.parameters.PlanPath $cfg.parameters.PlanSha256 65536)))
$start=$plan.initial_service_start
if($plan.initial_recovery_postboot.attempt_id -cne $cfg.attempt -or
   $plan.windows_enforced.service_name -cne $cfg.parameters.ServiceName -or
   $plan.windows_enforced.service_image_sha256 -cne $cfg.parameters.ServiceHostSha256 -or
   $start.binding_path -cne $cfg.parameters.BindingPath -or
   $start.service_image_path_sha256 -cne $cfg.image_path_pin){throw 'sameboot_successor_plan'}
$boot=(Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 5).LastBootUpTime.ToUniversalTime().Ticks.ToString()
if((Digest ($utf.GetBytes($boot))) -cne $cfg.boot_pin){throw 'sameboot_successor_boot'}
"""


class WindowsSamebootHost:
    """Use the installed native armer/starter, with read-only reconciliation."""

    def __init__(self, windows, *, material, approval_path, approval_sha256, preflight_path,
                 scm_path, starter_path, starter_sha256, reservation_sha256, reboot_pair=None):
        from .native_sameboot_release import WindowsPairClosure

        allow_saved_reboot = isinstance(reboot_pair, WindowsPairClosure)
        checked = bind_successor_plan(**material, allow_saved_reboot=allow_saved_reboot)
        plan = material['candidate']
        self.acceptance = _decode(material['files'][FILES[5]])
        postboot = scope(plan)
        _need(saved_reboot(postboot) == allow_saved_reboot)
        _need(allow_saved_reboot or reboot_pair is None)
        self.reboot_pair = reboot_pair
        if allow_saved_reboot:
            self.closed_revision, _, _ = derive_predecessor(
                material['files'][FILES[0]], checked['terminal_sha256'], published=True)
            _need(reboot_pair.config['plan_sha256'] == postboot['predecessor_plan_sha256']
                  and reboot_pair.config['binding_sha256'] == postboot['predecessor_binding_sha256']
                  and reboot_pair.config['boot_sha256'] == postboot['previous_windows_boot_sha256']
                  and reboot_pair.config['revision'] == self.closed_revision - 1)
        start = plan['initial_service_start']
        custody = plan['initial_cutover_handoff']
        recovery = plan['initial_cutover_recovery']['attempt_id']
        _need(all(_pin(pin) for pin in (approval_sha256, starter_sha256, reservation_sha256,
                                       start['preflight_sha256'], custody['activation_intent_sha256'])))
        root = PureWindowsPath('C:/ProgramData/N100HA-InitialCutover') / postboot['attempt_id'][:40]
        executable = host_path(recovery, postboot['attempt_id'])
        binding_path = _path(start['binding_path'])
        _need(binding_path == root / ('initial-recovery-service-' + recovery + '.json'))
        _need(_sha(('"' + str(executable) + '"').encode()) == postboot['successor_service_image_path_sha256'])
        python = _path(plan['windows_enforced']['python_path'])
        _need(python.parts[-2:] == ('python', 'python.exe'))
        source = python.parent.parent / 'controller' / 'files' / 'ha_control'
        _need(_path(starter_path) == source / 'windows-ha-start-initial-service.ps1'
              and _path(preflight_path) == source / 'windows-service-preflight.ps1'
              and _path(scm_path) == source / 'stopped_service_installer' / 'StoppedService.cs'
              and _path(approval_path) == root / 'approval.json')
        parameters = {'ServiceName': plan['windows_enforced']['service_name'],
                      'ServiceHostSha256': checked['successor_image_sha256'],
                      'PlanPath': str(root / 'plan.json'), 'PlanSha256': checked['plan_sha256'],
                      'ApprovalPath': str(approval_path), 'ApprovalSha256': approval_sha256,
                      'BindingPath': str(binding_path), 'PreflightPath': str(preflight_path),
                      'PreflightSha256': start['preflight_sha256'], 'ScmVerifierPath': str(scm_path),
                      'ExpectedActivationIntentSha256': custody['activation_intent_sha256'],
                      'RecoveryAttemptId': recovery, 'RecoveryReservationSha256': reservation_sha256}
        _need(set(parameters) == START_FIELDS and re.fullmatch(
            r'N100HA_[a-f0-9]{24}_controller', parameters['ServiceName']))
        self.windows = windows
        self.checked = checked
        self.config = {'parameters': parameters, 'attempt': postboot['attempt_id'],
                       'image_path_pin': postboot['successor_service_image_path_sha256'],
                       'boot_pin': plan['windows_initial_observer']['windows_boot_sha256'],
                       'closed_proof': checked['closed_proof_sha256'], 'starter_path': str(starter_path),
                       'starter_pin': starter_sha256, 'starter_max_bytes': STARTER_FILE_MAX_BYTES,
                       'expected_state_path': str(root / 'journal' / 'controller-state.json')}

    def _run(self, body, deadline):
        _need(time.monotonic() < deadline)
        cfg = base64.b64encode(_encoded(self.config)).decode('ascii')
        result = self.windows.powershell(_PINNED_READ.replace('__CONFIG__', cfg) + body, deadline)
        _need(time.monotonic() < deadline and type(result) is dict)
        return result

    def _verify_reboot_pair(self, deadline):
        if self.reboot_pair is None:
            return
        terminal = self.checked['terminal_sha256']
        boot = self.config['boot_pin']
        closed = self.reboot_pair.closed_pair_after_reboot(terminal, boot, deadline)
        _need(closed['revision'] == self.closed_revision and closed['terminal_sha256'] == terminal
              and closed['closed_nodes'] == ['A', 'B'] and _pin(closed['closed_proof_sha256'])
              and all(closed[field] is True for field in ('synchronous', 'same_expiry',
                                                         'replay_verified', 'effects_isolated')))

    def seal(self, deadline, *, allow_existing=False):
        """Create acceptance through the native stopped-instance verifier."""
        _need(type(allow_existing) is bool)
        self._verify_reboot_pair(deadline)
        body = r"""
$source=$utf.GetString((Read-Pin $cfg.starter_path $cfg.starter_pin $cfg.starter_max_bytes))
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseInput($source,[ref]$tokens,[ref]$errors)
if($errors.Count -ne 0){throw 'sameboot_successor_source'}
foreach($definition in $ast.EndBlock.Statements){
 if($definition -is [Management.Automation.Language.FunctionDefinitionAst]){. ([scriptblock]::Create($definition.Extent.Text))}
}
Seal-HaInitialPostbootAcceptance $cfg.parameters.PlanPath $cfg.parameters.PlanSha256 __ALLOW_EXISTING__
$seal=Join-Path ([IO.Path]::GetDirectoryName($cfg.parameters.PlanPath)) 'postboot-acceptance-seal.json'
$raw=Read-Safe $seal 65536
@{acceptance_sha256=(Digest $raw)}|ConvertTo-Json -Compress
"""
        result = self._run(body.replace('__ALLOW_EXISTING__', '-AllowExistingSeal' if allow_existing else ''),
                           deadline)
        _need(set(result) == {'acceptance_sha256'} and _pin(result['acceptance_sha256']))
        return result['acceptance_sha256']

    def observe_seal(self, deadline):
        """Reconcile acceptance without asking the native sealer to write."""
        self._verify_reboot_pair(deadline)
        result = self._run(r"""
$path=Join-Path ([IO.Path]::GetDirectoryName($cfg.parameters.PlanPath)) 'postboot-acceptance-seal.json'
$raw=Read-Safe $path 2500
@{data=[Convert]::ToBase64String($raw)}|ConvertTo-Json -Compress
""", deadline)
        _need(set(result) == {'data'} and type(result['data']) is str)
        raw = base64.b64decode(result['data'], validate=True)
        _need(_decode(raw) == self.acceptance)
        return _sha(raw)

    def _invoke(self, deadline, *, arm):
        body = r"""
$raw=Read-Pin $cfg.starter_path $cfg.starter_pin $cfg.starter_max_bytes
$p=@{}
foreach($entry in $cfg.parameters.PSObject.Properties){$p[$entry.Name]=$entry.Value}
$p.Go=$true
__ARM__
& ([ScriptBlock]::Create($utf.GetString($raw))) @p
"""
        options = '$p.ArmPostbootHost=$true; $p.ClosedPairProofSha256=$cfg.closed_proof' if arm else ''
        return self._run(body.replace('__ARM__', options), deadline)

    def arm(self, deadline):
        expected = {'host_handoff_armed': True, 'plan_sha256': self.checked['plan_sha256'],
                    'service_image_path_sha256': self.config['image_path_pin'], 'service_started': False}
        _need(self._invoke(deadline, arm=True) == expected)
        return self.arm_evidence()

    def arm_evidence(self):
        return {'successor_image_sha256': self.checked['successor_image_sha256'],
                'plan_sha256': self.checked['plan_sha256'], 'carryover_sha256': self.checked['carryover_sha256'],
                'service_stopped': True}

    def observe_armed(self, deadline):
        result = self._run(r"""
$service=Get-CimInstance Win32_Service -Filter ("Name='"+$cfg.parameters.ServiceName+"'") -OperationTimeoutSec 5
if($null -eq $service -or $service.State -cne 'Stopped' -or $service.StartMode -cne 'Manual' -or
   $service.ProcessId -ne 0 -or $service.StartName -ine ('NT SERVICE\'+$cfg.parameters.ServiceName) -or
   $service.ServiceType -cne 'Own Process' -or $service.DesktopInteract -or
   (Digest ($utf.GetBytes($service.PathName))) -cne $cfg.image_path_pin -or
   (Test-Path -LiteralPath $cfg.parameters.BindingPath) -or (Test-Path -LiteralPath $cfg.expected_state_path)){
 throw 'sameboot_successor_arm_unconfirmed'
}
$null=Read-Pin ($service.PathName.Trim('"')) $cfg.parameters.ServiceHostSha256 67108864
@{armed=$true}|ConvertTo-Json -Compress
""", deadline)
        _need(result == {'armed': True})
        return self.arm_evidence()

    def start(self, deadline):
        return self._publication(self._invoke(deadline, arm=False))

    def observe_publication(self, deadline):
        """Verify the exact protected binding and SCM process without starting it."""
        return self._publication(self._run(r"""
$parent=[IO.Path]::GetDirectoryName($cfg.parameters.BindingPath)
foreach($path in @($parent,$cfg.parameters.BindingPath)){
 $acl=[Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $path).Sddl)
 if($acl.Owner.Value -cnotin @('S-1-5-18','S-1-5-32-544') -or $null -eq $acl.DiscretionaryAcl){throw 'sameboot_successor_binding_acl'}
 $writeMask=if($path -ceq $parent){269287488L}else{1343029590L}
 foreach($ace in $acl.DiscretionaryAcl){
   if($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback){throw 'sameboot_successor_binding_acl'}
   if($ace.AceQualifier -ceq 'AccessAllowed' -and ([long]$ace.AccessMask -band $writeMask) -ne 0 -and
       $ace.SecurityIdentifier.Value -cnotin @('S-1-5-18','S-1-5-32-544')){throw 'sameboot_successor_binding_acl'}
 }
}
$raw=Read-Safe $cfg.parameters.BindingPath 8192
$b=ConvertFrom-Json ($utf.GetString($raw))
$fields=@($b.PSObject.Properties.Name|Sort-Object -CaseSensitive) -join ','
if($fields -cne 'attempt_id,controller_plan_sha256,first_write_proof_sha256,legacy_plan_sha256,service_created_ticks,service_image_path_sha256,service_name,service_pid,state_path' -or
   $b.controller_plan_sha256 -cne $cfg.parameters.PlanSha256 -or $b.service_name -cne $cfg.parameters.ServiceName -or
   $b.attempt_id -cne $plan.initial_cutover_handoff.attempt_id -or
   $b.legacy_plan_sha256 -cne $plan.initial_cutover_handoff.legacy_plan_sha256 -or
   $b.state_path -cne $cfg.expected_state_path -or $b.service_image_path_sha256 -cne $cfg.image_path_pin -or
   $b.first_write_proof_sha256 -cnotmatch $hashPattern){throw 'sameboot_successor_publication'}
$svc=Get-CimInstance Win32_Service -Filter ("Name='"+$b.service_name+"'") -OperationTimeoutSec 5
if($null -eq $svc -or $svc.State -cne 'Running' -or $svc.ProcessId -ne $b.service_pid -or $svc.ProcessId -lt 1 -or
   $svc.StartMode -cne 'Manual' -or $svc.StartName -ine ('NT SERVICE\'+$b.service_name) -or
   $svc.ServiceType -cne 'Own Process' -or $svc.DesktopInteract -or
   (Digest ($utf.GetBytes($svc.PathName))) -cne $cfg.image_path_pin){throw 'sameboot_successor_publication'}
$process=Get-Process -Id $b.service_pid
try {
 if($process.HasExited -or $process.SessionId -ne 0 -or
    $process.MainModule.FileName -ine $svc.PathName.Trim('"') -or
    $process.StartTime.ToUniversalTime().Ticks.ToString() -cne $b.service_created_ticks){throw 'sameboot_successor_publication'}
} finally {$process.Dispose()}
$null=Read-Pin ($svc.PathName.Trim('"')) $cfg.parameters.ServiceHostSha256 67108864
$instance=Digest $raw
$after=Get-CimInstance Win32_Service -Filter ("Name='"+$b.service_name+"'") -OperationTimeoutSec 5
if($null -eq $after -or $after.State -cne $svc.State -or $after.ProcessId -ne $svc.ProcessId -or
   $after.PathName -cne $svc.PathName -or $after.StartMode -cne $svc.StartMode -or
   $after.StartName -cne $svc.StartName -or $after.ServiceType -cne $svc.ServiceType -or
   $after.DesktopInteract -ne $svc.DesktopInteract -or
   (Digest (Read-Safe $cfg.parameters.BindingPath 8192)) -cne $instance){throw 'sameboot_successor_publication'}
@{first_write_proof_sha256=$b.first_write_proof_sha256;service_instance_sha256=$instance}|ConvertTo-Json -Compress
""", deadline))

    @staticmethod
    def _publication(result):
        _need(type(result) is dict and set(result) == {'first_write_proof_sha256', 'service_instance_sha256'}
              and all(_pin(pin) for pin in result.values()))
        return result
