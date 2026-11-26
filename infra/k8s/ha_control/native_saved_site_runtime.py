"""Verify approved Windows intake and stage a successor runtime without starting it."""

import base64
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import time

from .controller import TransitionBlocked
from .native_sameboot_release import WindowsPairClosure, _json
from .native_sameboot_successor import host_path
from .prepare import verify
from .sameboot_transition import SavedRuntimeVerificationBlocked, _closed, _digest, _encoded, _pin, _validate


VERIFY_STEPS = frozenset({'bootstrap', 'initialize', 'lock', 'predecessor', 'host', 'terminal',
                          'reservation', 'incoming', 'manifest', 'archive', 'readback'})


def _need(condition):
    if not condition:
        raise TransitionBlocked('native_saved_site_runtime_unconfirmed')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


class WindowsSavedSiteRuntime:
    """Use already uploaded, digest-pinned intake, and a separately staged host EXE.

    No upload, SCM change, candidate argument publication or service start is
    performed here. A lost apply response requires read-only reconciliation.
    """

    def __init__(self, pair, *, manifest_raw, manifest_sha256, source_commit, controller_bundle,
                 stager_sha256, rearm_attempt, predecessor_args_sha256, candidate_image_sha256,
                 staged_journal_sha256=None):
        _need(type(pair) is WindowsPairClosure and type(manifest_raw) is bytes
              and 0 < len(manifest_raw) <= 1048576 and _sha(manifest_raw) == manifest_sha256
              and all(_pin(pin) for pin in (stager_sha256, rearm_attempt,
                                          predecessor_args_sha256, candidate_image_sha256))
              and (staged_journal_sha256 is None or _pin(staged_journal_sha256))
              and type(source_commit) is str and re.fullmatch('[a-f0-9]{40}', source_commit))
        meta = _json(manifest_raw)
        fields = {'schema_version', 'source_commit', 'installation_id', 'archive_sha256', 'archive_bytes',
                  'service_name', 'service_sid', 'files', 'recovery_attempt_id', 'reservation_receipt_sha256'}
        _need(type(meta) is dict and set(meta) == fields and type(meta['schema_version']) is int
              and meta['schema_version'] == 2 and meta['source_commit'] == source_commit
              and all(_pin(meta[key]) for key in ('installation_id', 'archive_sha256', 'recovery_attempt_id',
                                                  'reservation_receipt_sha256'))
              and rearm_attempt != meta['recovery_attempt_id']
              and type(meta['archive_bytes']) is int and 22 <= meta['archive_bytes'] <= 1073741824
              and type(meta['files']) is dict and 777 <= len(meta['files']) <= 8192
              and meta['service_name'] == 'N100HA_' + meta['recovery_attempt_id'][:24] + '_controller'
              and type(meta['service_sid']) is str and re.fullmatch(r'S-1-5-80(?:-[0-9]{1,10}){5}', meta['service_sid']))
        raw = Path(__file__).with_name('windows-runtime-stage.ps1').read_bytes()
        _need(_sha(raw) == stager_sha256)
        files = meta['files']

        def file_pin(name):
            row = files.get(name)
            _need(type(row) is dict and row.get('filename') == name and _pin(row.get('sha256')))
            return row['sha256']

        bundle = file_pin('controller/source-manifest.json')
        provenance = verify(Path(controller_bundle), bundle)
        _need(provenance['source_commit'] == source_commit and provenance['verified'] is True)
        module = 'controller/files/ha_control/'
        _need(file_pin(module + 'windows-runtime-stage.ps1') == stager_sha256)
        self.pair = pair
        self._staged_journal_sha256 = staged_journal_sha256
        self.manifest_raw = manifest_raw
        self._manifest_sha256 = manifest_sha256
        self._starter_sha256 = file_pin(module + 'windows-ha-start-initial-service.ps1')
        recovery = meta['recovery_attempt_id']
        runtime_id = 'runtime-' + manifest_sha256[:24]
        root = PureWindowsPath('C:/ProgramData') / ('N100HA_' + recovery[:24])
        self._runtime = {
            'path': str(root / runtime_id), 'manifest_sha256': manifest_sha256,
            'bundle_sha256': bundle, 'provenance': provenance,
            'python_sha256': file_pin('python/python.exe'), 'launcher_sha256': file_pin(module + 'launcher.py'),
            'preflight_sha256': file_pin(module + 'windows-service-preflight.ps1'),
            'scm_sha256': file_pin(module + 'stopped_service_installer/StoppedService.cs'),
        }
        self._config = {
            'root': str(root), 'manifest': manifest_sha256, 'manifest_bytes': len(manifest_raw),
            'archive': meta['archive_sha256'], 'archive_bytes': meta['archive_bytes'], 'files': len(files),
            'recovery': recovery, 'reservation': meta['reservation_receipt_sha256'], 'rearm': rearm_attempt,
            'service': meta['service_name'], 'sid': meta['service_sid'], 'revision': pair.config['revision'],
            'source_commit': source_commit, 'installation': meta['installation_id'], 'runtime_id': runtime_id,
            'plan': pair.config['plan'], 'plan_pin': pair.config['plan_sha256'], 'binding': pair.config['binding'],
            'binding_pin': pair.config['binding_sha256'], 'boot': pair.config['boot_sha256'],
            'args': predecessor_args_sha256, 'candidate': str(host_path(recovery, rearm_attempt)),
            'candidate_image': candidate_image_sha256, 'stager_pin': stager_sha256,
            'stager': base64.b64encode(gzip.compress(raw, mtime=0)).decode('ascii'),
        }

    @property
    def starter_sha256(self):
        return self._starter_sha256

    def _scope(self, scope):
        config = self._config
        _need(scope.get('saved_reboot') is True and scope['previous_plan_sha256'] == config['plan_pin']
              and scope['previous_instance_sha256'] == config['binding_pin']
              and scope['previous_windows_boot_sha256'] == config['boot']
              and scope['previous_revision'] == config['revision']
              and scope['runtime_manifest_sha256'] == config['manifest']
              and scope['successor_image_sha256'] == config['candidate_image']
              and scope['attempt_sha256'] == config['rearm'])

    def _call(self, mode, terminal, deadline):
        _need(time.monotonic() < deadline)
        config = self._config | {'mode': mode, 'terminal': terminal}
        blob = base64.b64encode(_encoded(config)).decode('ascii')
        source = NATIVE.replace('__TERMINAL_GUARD__', TERMINAL_GUARD).replace('__CONFIG__', blob)
        value = self.pair.windows.powershell(source, deadline)
        if type(value) is dict and value.get('status') == 'BLOCKED':
            _need(set(value) == {'status', 'step', 'reason'} and type(value['step']) is str
                  and value['step'] in VERIFY_STEPS and type(value['reason']) is str
                  and re.fullmatch(r'(?:runtime_stage|saved_runtime)_[a-z_]{1,64}', value['reason']))
            raise SavedRuntimeVerificationBlocked(f"{value['step']}:{value['reason']}")
        expected = {'status', 'manifest_sha256', 'native_journal_sha256'}
        _need(type(value) is dict and set(value) == expected and value['manifest_sha256'] == self._manifest_sha256)
        if mode in {'preflight', 'preflight_stopped'}:
            _need(value['status'] == 'INTAKE_VERIFIED' and value['native_journal_sha256'] is None)
        elif mode == 'ready':
            _need(value['status'] == 'STAGE_READY' and value['native_journal_sha256'] is None)
        else:
            _need(value['status'] == 'RUNTIME_VERIFIED' and _pin(value['native_journal_sha256']))
        return value

    def _apply(self, terminal, deadline):
        _need(time.monotonic() < deadline)
        config = self._config | {'terminal': terminal}
        blob = base64.b64encode(_encoded(config)).decode('ascii')
        source = APPLY.replace('__TERMINAL_GUARD__', TERMINAL_GUARD).replace('__CONFIG__', blob)
        receipt = self.pair.windows.powershell(source, deadline)
        expected = {
            'scope': 'private_code_stage_only', 'schema_version': 4, 'native_stage': True, 'inventory_version': 1,
            'runtime_id': config['runtime_id'], 'manifest_sha256': config['manifest'], 'archive_sha256': config['archive'],
            'source_commit': config['source_commit'], 'installation_id': config['installation'], 'file_count': config['files'],
            'recovery_attempt_id': config['recovery'], 'reservation_receipt_sha256': config['reservation'],
            'rearm_attempt_id': config['rearm'], 'predecessor_args_sha256': config['args'], 'readonly': True,
            'native_imports_verified': True, 'host_staged': False, 'SCM_registered': False,
            'group_changed': False, 'namespace_changed': False, 'VM_changed': False, 'authority_issued': False,
        }
        _need(type(receipt) is dict and set(receipt) == expected.keys() | {'python_version', 'psycopg_version', 'tzdata_version'}
              and all(type(receipt[key]) is type(value) and receipt[key] == value for key, value in expected.items()))

    def preflight(self, operation, deadline, *, stopped_terminal=None):
        """Read the exact incoming archive with either a running or verified closed predecessor."""
        _need(operation['stage'] == 'preflight')
        scope = operation['scope']
        self._scope(scope)
        if stopped_terminal is None:
            _need(self._staged_journal_sha256 is None)
            return self._call('preflight', None, deadline)
        _need(_pin(stopped_terminal) and _closed(self.pair.closed_pair_before_disable(stopped_terminal, deadline),
                                                scope['previous_revision'] + 1, stopped_terminal))
        if self._staged_journal_sha256 is not None:
            return self._observe_staged(stopped_terminal, deadline)
        return self._call('preflight_stopped', stopped_terminal, deadline)

    def stage(self, operation, receipts, deadline, *, observe_only):
        """Return exact coordinator evidence; an observation never extracts or retries."""
        _need(operation['stage'] == 'stage_runtime' and _pin(operation['nonce']) and type(observe_only) is bool)
        scope = operation['scope']
        self._scope(scope)
        _validate('close_pair', receipts['close_pair'], scope, receipts)
        _validate('retire_provider', receipts['retire_provider'], scope, receipts)
        terminal = receipts['close_pair']['terminal_sha256']
        revision = scope['previous_revision'] + 1
        _need(_closed(self.pair.closed_pair(terminal, deadline), revision, terminal))
        if not observe_only and self._staged_journal_sha256 is None:
            self._call('ready', terminal, deadline)
            self._apply(terminal, deadline)
        value = self._observe_staged(terminal, deadline)
        _need(_closed(self.pair.closed_pair(terminal, deadline), revision, terminal))
        result = {'runtime_stage_sha256': _digest(value), 'manifest_sha256': self._manifest_sha256,
                  'old_runtime_preserved': True, 'authority_issued': False}
        _validate('stage_runtime', result, scope, receipts)
        return result

    def _observe_staged(self, terminal, deadline):
        value = self._call('observe', terminal, deadline)
        _need(self._staged_journal_sha256 is None
              or value['native_journal_sha256'] == self._staged_journal_sha256)
        return value

    def material(self, scope, stage_receipt):
        """Describe the exact runtime in a coordinator-verified stage receipt."""
        self._scope(scope)
        _validate('stage_runtime', stage_receipt, scope, {})
        return deepcopy(self._runtime)


TERMINAL_GUARD = r'''
function AssertTerminal([string]$path){
    $planRoot=[IO.Path]::GetDirectoryName($cfg.plan)
    if($path -cne (Join-Path $planRoot 'journal\controller-state.json')){throw 'saved_runtime_terminal_path'}
    $leaf=Get-Item -LiteralPath $path -Force
    if($leaf -isnot [IO.FileInfo]){throw 'saved_runtime_terminal_path'}
    for($entry=$leaf;$null-ne $entry;$entry=if($entry -is [IO.FileInfo]){$entry.Directory}else{$entry.Parent}){
        if($entry.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'saved_runtime_terminal_path'}
        $writers=@('S-1-5-18','S-1-5-32-544')
        if($entry.FullName -cin @($leaf.FullName,$leaf.Directory.FullName)){$writers+=@($cfg.sid)}
        $sd=[Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $entry.FullName -ErrorAction Stop).Sddl)
        $osRoot=$entry.FullName -ceq 'C:\' -and $sd.Owner.Value -ceq 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
        if(($sd.Owner.Value -cnotin $writers -and -not $osRoot) -or $null-eq $sd.DiscretionaryAcl -or
           $sd.DiscretionaryAcl.Count -lt 1){throw 'saved_runtime_terminal_acl'}
        foreach($ace in $sd.DiscretionaryAcl){
            if($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback){throw 'saved_runtime_terminal_acl'}
            if($ace.AceQualifier -ne [Security.AccessControl.AceQualifier]::AccessAllowed -or ([int]$ace.AceFlags -band 8)){continue}
            $mask=if($entry -is [IO.DirectoryInfo]){269287488L}else{1343029590L}
            if(($ace.AccessMask -band $mask) -ne 0 -and $ace.SecurityIdentifier.Value -cnotin $writers){throw 'saved_runtime_terminal_acl'}
        }
    }
}
function OpenTerminal([string]$path){
    AssertTerminal $path
    $file=[IO.File]::Open($path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    try{
        if($file.Length -lt 1 -or $file.Length -gt 8192){throw 'saved_runtime_terminal_size'}
        $h=[Security.Cryptography.SHA256]::Create()
        try{$pin=[BitConverter]::ToString($h.ComputeHash($file)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}
        if($pin -cne $cfg.terminal){throw 'saved_runtime_terminal_pin'}
        AssertTerminal $path
        $file.Position=0
        return $file
    }catch{$file.Dispose();throw}
}
'''


NATIVE = r'''
$ErrorActionPreference='Stop';Set-StrictMode -Version Latest
$verifyStep='bootstrap';$mutex=$null;$locked=$false
$held=[Collections.Generic.List[IDisposable]]::new()
try{
$enc=[Text.UTF8Encoding]::new($false,$true)
$cfg=ConvertFrom-Json ($enc.GetString([Convert]::FromBase64String('__CONFIG__')))
function Need([bool]$ok,[string]$reason='saved_runtime_scope'){if(-not $ok){throw $reason}}
function HashBytes([byte[]]$bytes){$h=[Security.Cryptography.SHA256]::Create();try{[BitConverter]::ToString($h.ComputeHash($bytes)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}}
$raw=[IO.MemoryStream]::new([Convert]::FromBase64String($cfg.stager))
$gzip=[IO.Compression.GZipStream]::new($raw,[IO.Compression.CompressionMode]::Decompress)
$reader=[IO.StreamReader]::new($gzip,$enc,$false)
try{$source=$reader.ReadToEnd()}finally{$reader.Dispose()}
Need ((HashBytes ($enc.GetBytes($source))) -ceq $cfg.stager_pin)
$tokens=$null;$errors=$null
$module=[Management.Automation.Language.Parser]::ParseInput($source,[ref]$tokens,[ref]$errors)
$entry=$module.EndBlock.Statements[-1]
Need ($errors.Count -eq 0 -and $entry -is [Management.Automation.Language.IfStatementAst] -and
      $entry.Clauses.Count -eq 1 -and $entry.Clauses[0].Item1.Extent.Text -ceq "`$MyInvocation.InvocationName -ne '.'")
# This path imports the hash-pinned verification helpers only. The native
# stager entry point is invoked separately by APPLY after the stopped gate.
. ([scriptblock]::Create($source.Substring(0,$entry.Extent.StartOffset)))
$verifyStep='initialize'
Initialize-RuntimeStage
$script:RuntimeRecoverySpec=[pscustomobject]@{ServiceSid=$cfg.sid}
$script:RuntimeRecoveryRoot=$cfg.root
$mutex=[Threading.Mutex]::new($false,'Global\N100HA-Start-VM-Guests')
function OpenPin([string]$path,[string]$root,[long]$max,[string]$pin,[string]$readerSid=''){
    $null=Assert-RuntimeStagePath $path $root $readerSid
    $identity=[HaRuntimeStageV1.Native]::Identity($path)
    $file=[IO.File]::Open($path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    $held.Add($file)
    Need ($file.Length -gt 0 -and $file.Length -le $max) 'saved_runtime_file_length'
    Need ([HaRuntimeStageV1.Native]::Identity($path) -ceq $identity) 'saved_runtime_file_identity'
    Need ([HaRuntimeStageV1.Rules]::StreamHash($file) -ceq $pin) 'saved_runtime_file_hash'
    $file.Position=0
    return ,$file
}
function ReadText($file){$r=[IO.StreamReader]::new($file,$enc,$false,1024,$true);try{return $r.ReadToEnd()}finally{$r.Dispose();$file.Position=0}}
__TERMINAL_GUARD__
function VerifyTerminalIdentity {
    AssertTerminal $binding.state_path
    Need ([HaRuntimeStageV1.Native]::Identity($binding.state_path) -ceq $terminalIdentity)
}
function AssertIncoming([string]$path){
    Assert-RuntimeStagePath $path $path
    $sd=[Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $path).Sddl)
    Need ($sd.Owner.Value -ceq 'S-1-5-32-544' -and ($sd.ControlFlags -band 0x1004) -eq 0x1004 -and $sd.DiscretionaryAcl.Count -eq 2)
    $owners=@('S-1-5-18','S-1-5-32-544')
    foreach($ace in $sd.DiscretionaryAcl){
        Need ($ace -is [Security.AccessControl.CommonAce] -and -not $ace.IsCallback -and $ace.AceQualifier -ceq 'AccessAllowed' -and
              $ace.AccessMask -eq 2032127 -and [int]$ace.AceFlags -eq 3 -and $ace.SecurityIdentifier.Value -cin $owners)
        $owners=@($owners|Where-Object{$_ -cne $ace.SecurityIdentifier.Value})
    }
    Need ($owners.Count -eq 0)
}
function Guard {
    $boot=(Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 5).LastBootUpTime.ToUniversalTime().Ticks.ToString()
    Need ((HashBytes ($enc.GetBytes($boot))) -ceq $cfg.boot)
    $s=@(Get-CimInstance Win32_Service -Filter ("Name='"+$cfg.service+"'") -OperationTimeoutSec 5)
    Need ($s.Count -eq 1 -and $s[0].Name -ceq $cfg.service -and $s[0].StartName -ieq ('NT SERVICE\'+$cfg.service) -and
          $s[0].ServiceType -ceq 'Own Process' -and $s[0].DesktopInteract -is [bool] -and -not $s[0].DesktopInteract -and
          (HashBytes ($enc.GetBytes($s[0].PathName))) -ceq $binding.service_image_path_sha256)
    $p=$s[0]
    if($cfg.mode -ceq 'preflight'){
        Need ($p.State -ceq 'Running' -and $p.StartMode -ceq 'Manual' -and $p.ProcessId -gt 0 -and $p.ProcessId -eq $binding.service_pid)
    }elseif($cfg.mode -ceq 'preflight_stopped'){
        Need ($p.State -ceq 'Stopped' -and $p.StartMode -cin @('Manual','Disabled') -and $p.ProcessId -eq 0)
    }else{Need ($p.State -ceq 'Stopped' -and $p.StartMode -ceq 'Disabled' -and $p.ProcessId -eq 0)}
    Need (-not (Test-Path -LiteralPath (Join-Path ([IO.Path]::GetDirectoryName($cfg.candidate)) 'service.args')))
    return $p
}
    $verifyStep='lock'
    $locked=$mutex.WaitOne(5000);Need $locked
    $verifyStep='predecessor'
    Need ($cfg.root -ceq (Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)) ('N100HA_'+$cfg.recovery.Substring(0,24))))
    $planRoot=[IO.Path]::GetDirectoryName($cfg.plan)
    Need ([IO.Path]::GetDirectoryName($cfg.binding) -ceq $planRoot)
    $plan=ConvertFrom-Json (ReadText (OpenPin $cfg.plan $planRoot 65536 $cfg.plan_pin $cfg.sid))
    $binding=ConvertFrom-Json (ReadText (OpenPin $cfg.binding $planRoot 8192 $cfg.binding_pin $cfg.sid))
    Need ($plan.initial_service_start.binding_path -ceq $cfg.binding -and $plan.windows_enforced.service_name -ceq $cfg.service -and
          $plan.windows_enforced.service_sid -ceq $cfg.sid -and $plan.initial_cutover_recovery.attempt_id -ceq $cfg.recovery -and
          $binding.controller_plan_sha256 -ceq $cfg.plan_pin -and $binding.service_name -ceq $cfg.service -and
          $binding.state_path -ceq (Join-Path $planRoot 'journal\controller-state.json') -and
          $binding.service_image_path_sha256 -ceq $plan.initial_service_start.service_image_path_sha256)
    $s=Guard
    $verifyStep='host'
    $oldHost=$s.PathName.Trim('"')
    Need ($s.PathName -ceq ('"'+$oldHost+'"') -and $oldHost -cne $cfg.candidate -and
          $plan.windows_enforced.service_image_sha256 -cne $cfg.candidate_image)
    $null=OpenPin $oldHost $cfg.root 67108864 $plan.windows_enforced.service_image_sha256 $cfg.sid
    $null=OpenPin (Join-Path ([IO.Path]::GetDirectoryName($oldHost)) 'service.args') $cfg.root 16384 $cfg.args $cfg.sid
    $null=OpenPin $cfg.candidate $cfg.root 32768 $cfg.candidate_image $cfg.sid
    $verifyStep='terminal'
    if($cfg.mode -cne 'preflight'){
        $terminalIdentity=[HaRuntimeStageV1.Native]::Identity($binding.state_path)
        $held.Add((OpenTerminal $binding.state_path))
        VerifyTerminalIdentity
    }
    $verifyStep='reservation'
    $reservation=OpenPin (Join-Path $cfg.root 'reservation-receipt.json') $cfg.root 32768 $cfg.reservation $cfg.sid
    $reservationRaw=ReadText $reservation
    $incoming=Join-Path $cfg.root ('incoming-runtime-'+$cfg.manifest.Substring(0,24))
    $manifestPath=Join-Path $incoming 'runtime-manifest.json';$archivePath=Join-Path $incoming 'runtime.zip'
    $verifyStep='incoming'
    AssertIncoming $incoming
    $children=@(Get-ChildItem -LiteralPath $incoming -Force)
    Need ($children.Count -eq 2 -and @($children|Where-Object{$_.Name -cin @('runtime-manifest.json','runtime.zip') -and $_ -is [IO.FileInfo]}).Count -eq 2)
    $verifyStep='manifest'
    # The imported stager declares [string]$Manifest. PowerShell variables
    # are case-insensitive, so a differently named stream is required here.
    $manifestStream=OpenPin $manifestPath $incoming $cfg.manifest_bytes $cfg.manifest
    $archiveStream=OpenPin $archivePath $incoming $cfg.archive_bytes $cfg.archive
    Need ($manifestStream -is [IO.FileStream] -and $archiveStream -is [IO.FileStream]) 'saved_runtime_intake_stream'
    Need ($manifestStream.Length -eq [long]$cfg.manifest_bytes) 'saved_runtime_manifest_length'
    Need ($archiveStream.Length -eq [long]$cfg.archive_bytes) 'saved_runtime_archive_length'
    $spec=[HaRuntimeStageV1.Rules]::Manifest((ReadText $manifestStream))
    Need ($spec.ArchiveHash -ceq $cfg.archive -and $spec.ArchiveBytes -eq $cfg.archive_bytes) 'saved_runtime_archive_manifest'
    Need ($spec.Files.Count -eq $cfg.files) 'saved_runtime_inventory_count'
    Need ($spec.ServiceName -ceq $cfg.service -and $spec.ServiceSid -ceq $cfg.sid -and
          [HaRuntimeStageV1.Native]::ServiceIdentity($spec.ServiceName,$spec.ServiceSid)) 'saved_runtime_service_identity'
    Need ($spec.RecoveryAttempt -ceq $cfg.recovery -and $spec.ReservationHash -ceq $cfg.reservation) 'saved_runtime_reservation_binding'
    Need ($spec.SourceCommit -ceq $cfg.source_commit -and $spec.InstallationId -ceq $cfg.installation) 'saved_runtime_source_binding'
    Assert-RecoveryRuntimeReceipt ([HaRuntimeStageV1.Json]::Object($reservationRaw,32768)) $spec
    $verifyStep='archive'
    $zip=[IO.Compression.ZipArchive]::new($archiveStream,[IO.Compression.ZipArchiveMode]::Read,$true)
    try{[HaRuntimeStageV1.Rules]::Zip($zip,$spec)}finally{$zip.Dispose();$archiveStream.Position=0}
    $verifyStep='readback'
    $target=Join-Path $cfg.root $cfg.runtime_id;$journal=Join-Path $cfg.root ($cfg.runtime_id+'-stage-journal.jsonl')
    if($cfg.mode -cne 'observe'){
        Need (-not (Test-Path -LiteralPath $target) -and -not (Test-Path -LiteralPath $journal))
        $null=Guard
        if($cfg.mode -cne 'preflight'){VerifyTerminalIdentity}
        $status=if($cfg.mode -cin @('preflight','preflight_stopped')){'INTAKE_VERIFIED'}else{'STAGE_READY'}
        @{status=$status;manifest_sha256=$cfg.manifest;native_journal_sha256=$null}|ConvertTo-Json -Compress
        return
    }
    $file=Read-RuntimeStageInput $journal $cfg.root 2097152;$held.Add($file)
    $journalPin=[HaRuntimeStageV1.Rules]::StreamHash($file);$file.Position=0
    $lines=(ReadText $file).Split([char]10)
    Need ($lines.Length -eq ($cfg.files+6) -and $lines[-1] -ceq '')
    $events=@($lines[0..($lines.Length-2)]|ForEach-Object{ConvertFrom-Json $_})
    $first=$events[0];$last=$events[-1];$data=$first.data
    Need ($first.event -ceq 'intent' -and $data.manifest_sha256 -ceq $cfg.manifest -and $data.archive_sha256 -ceq $cfg.archive -and
          $data.source_commit -ceq $cfg.source_commit -and $data.installation_id -ceq $cfg.installation -and $data.runtime_id -ceq $cfg.runtime_id -and
          $data.recovery_attempt_id -ceq $cfg.recovery -and $data.reservation_receipt_sha256 -ceq $cfg.reservation -and
          $data.rearm_attempt_id -ceq $cfg.rearm -and $data.predecessor_args_sha256 -ceq $cfg.args -and
          $events[-4].event -ceq 'seal' -and $events[-3].event -ceq 'authenticode' -and $events[-2].event -ceq 'fresh_python_imports' -and
          $last.event -ceq 'sealed_code_stage' -and $last.data.file_count -eq $cfg.files -and
          $last.data.native_code_only -ceq $true -and $last.data.SCM_registered -ceq $false -and $last.data.authority_issued -ceq $false)
    $seen=[Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach($event in $events[1..$cfg.files]){Need ($event.event -ceq 'extract' -and $spec.Files.ContainsKey($event.data.filename) -and $seen.Add($event.data.filename))}
    Need ((Assert-RuntimeStageSealed $target $cfg.root $spec) -eq $cfg.files)
    Assert-RuntimeStageSignatures $target
    $null=Guard
    VerifyTerminalIdentity
    @{status='RUNTIME_VERIFIED';manifest_sha256=$cfg.manifest;native_journal_sha256=$journalPin}|ConvertTo-Json -Compress
}catch{
    $reason=$_.Exception.Message
    if($reason -cnotmatch '\A(?:runtime_stage|saved_runtime)_[a-z_]{1,64}\z'){$reason='saved_runtime_unexpected'}
    @{status='BLOCKED';step=$verifyStep;reason=$reason}|ConvertTo-Json -Compress
}finally{
    foreach($file in $held){$file.Dispose()}
    if($null -ne $mutex){if($locked){$mutex.ReleaseMutex()};$mutex.Dispose()}
}
'''


APPLY = r'''
$ErrorActionPreference='Stop';Set-StrictMode -Version Latest
$enc=[Text.UTF8Encoding]::new($false,$true)
$cfg=ConvertFrom-Json ($enc.GetString([Convert]::FromBase64String('__CONFIG__')))
$m=[IO.MemoryStream]::new([Convert]::FromBase64String($cfg.stager))
$z=[IO.Compression.GZipStream]::new($m,[IO.Compression.CompressionMode]::Decompress)
$reader=[IO.StreamReader]::new($z,$enc,$false)
try{$source=$reader.ReadToEnd()}finally{$reader.Dispose()}
function HashBytes([byte[]]$bytes){$h=[Security.Cryptography.SHA256]::Create();try{[BitConverter]::ToString($h.ComputeHash($bytes)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}}
__TERMINAL_GUARD__
if((HashBytes ($enc.GetBytes($source))) -cne $cfg.stager_pin){exit 1}
$mutex=[Threading.Mutex]::new($false,'Global\N100HA-Start-VM-Guests');$locked=$false;$terminalStream=$null
try{
    $locked=$mutex.WaitOne(5000)
    if(-not $locked){exit 1}
    $boot=(Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 5).LastBootUpTime.ToUniversalTime().Ticks.ToString()
    if((HashBytes ($enc.GetBytes($boot))) -cne $cfg.boot){exit 1}
    $service=@(Get-CimInstance Win32_Service -Filter ("Name='"+$cfg.service+"'") -OperationTimeoutSec 5)
    if($service.Count -ne 1 -or $service[0].State -cne 'Stopped' -or $service[0].StartMode -cne 'Disabled' -or
       $service[0].ProcessId -ne 0){exit 1}
    $terminal=Join-Path ([IO.Path]::GetDirectoryName($cfg.plan)) 'journal\controller-state.json'
    foreach($item in @(@($cfg.plan,$cfg.plan_pin),@($cfg.binding,$cfg.binding_pin),@($cfg.candidate,$cfg.candidate_image))){
        if((Get-FileHash -LiteralPath $item[0] -Algorithm SHA256).Hash.ToLowerInvariant() -cne $item[1]){exit 1}
    }
    $terminalStream=OpenTerminal $terminal
    if(Test-Path -LiteralPath (Join-Path ([IO.Path]::GetDirectoryName($cfg.candidate)) 'service.args')){exit 1}
    $incoming=Join-Path $cfg.root ('incoming-runtime-'+$cfg.manifest.Substring(0,24))
    $result=@(& ([scriptblock]::Create($source)) -ManifestPath (Join-Path $incoming 'runtime-manifest.json') `
       -ManifestSha256 $cfg.manifest -RecoveryAttemptId $cfg.recovery -RecoveryReservationSha256 $cfg.reservation `
       -RearmAttemptId $cfg.rearm -RearmPredecessorArgsSha256 $cfg.args -IncomingRecoveryRuntime -Go)
    AssertTerminal $terminal
    $result
}finally{if($null-ne $terminalStream){$terminalStream.Dispose()};if($locked){$mutex.ReleaseMutex()};$mutex.Dispose()}
'''
