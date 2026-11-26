"""Pinned native restart of the three observer roles after a saved Windows reboot.

Site inputs are explicit: exact old descriptor/callback bytes and pins, descriptor
paths, reviewed starter path/pin, recovery mapping labels, and optional new held
attempt/carryover pins. No key bytes, inferred paths, registry fallback, old task
restart, service start or database writes are accepted here. Native readiness is
an endpoint receipt, not a replacement for the subsequent authenticated proof.
"""
from copy import deepcopy
import base64
import gzip
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re

from . import native_stdio_bridge as bridge

ERROR = 'saved_observers_unconfirmed'
ROLES = ('legacy', 'held_admission', 'provider_isolation')
ENDPOINT = {'server_pid', 'pipe_name', 'key_sha256'}


def _need(value):
    bridge._need(value, ERROR)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(raw, maximum=8192):
    _need(type(raw) is bytes and 0 < len(raw) <= maximum)
    try:
        value = json.loads(raw, object_pairs_hook=bridge._pairs, parse_constant=bridge._reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise bridge.BridgeRejected(ERROR) from None
    _need(type(value) is dict)
    return value


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def _path(value):
    _need(type(value) is str and re.fullmatch(
        r'C:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}\\[A-Za-z0-9_.-]+', value)
        and not value.endswith('.'))
    return PureWindowsPath(value)


def prepare_role(role, spec, boot):
    """Derive fresh boot-bound files from exact previously installed source bytes."""
    _need(role in ROLES and bridge._hash(boot) and type(spec) is dict)
    required = {'descriptor_path', 'descriptor_raw', 'descriptor_sha256', 'callback_raw',
                'callback_sha256', 'nonce'}
    allowed = required | {'mapping_label', 'callback_overrides'}
    _need(required <= spec.keys() <= allowed and type(spec['nonce']) is str
          and re.fullmatch('[a-f0-9]{32}', spec['nonce']))
    for prefix in ('descriptor', 'callback'):
        _need(type(spec[prefix + '_raw']) is bytes and bridge._hash(spec[prefix + '_sha256']) and
              _sha(spec[prefix + '_raw']) == spec[prefix + '_sha256'])
    descriptor = _json(spec['descriptor_raw'])
    callback = _json(spec['callback_raw'])
    _need(set(descriptor) in ({'server_path', 'server_sha256', 'arguments'},
                              {'server_path', 'server_sha256', 'arguments', 'instance_sha256'}))
    arguments = deepcopy(descriptor['arguments'])
    _need(type(arguments) is dict and type(arguments.get('Generation')) is int and arguments['Generation'] == 1
          and arguments.get('CallbackConfigSha256') == spec['callback_sha256']
          and bridge._hash(arguments.get('WindowsBootSha256'))
          and arguments['WindowsBootSha256'] != boot)
    root = _path(spec['descriptor_path']).parent
    _need(_path(descriptor['server_path']).parent == root and bridge._hash(descriptor['server_sha256']))
    for field in ('CallbackPath', 'CallbackConfigPath', 'KeyPath', 'ReadyPath'):
        _need(_path(arguments[field]).parent == root)
    _need(bridge._hash(arguments['KeySha256']) and bridge._hash(arguments['CallbackSha256'])
          and bridge._hash(arguments['PlanSha256']) and bridge._hash(arguments['AttemptId']))
    recovery = role != 'legacy'
    _need(('RecoveryProtocolSha256' in arguments) == recovery)
    overrides = spec.get('callback_overrides', {})
    _need(type(overrides) is dict and (not overrides or role == 'held_admission')
          and overrides.keys() <= {'postboot_attempt_id', 'postboot_carryover_receipt_sha256'}
          and all(bridge._hash(pin) for pin in overrides.values()))
    if recovery:
        _need(callback.get('purpose') == role and callback.get('windows_boot_sha256') == arguments['WindowsBootSha256']
              and type(callback.get('guest_vsock_port')) is int and 0 < callback['guest_vsock_port'] < 1024
              and bridge._hash(arguments['RecoveryProtocolSha256'])
              and _path(arguments['RecoveryProtocolPath']).parent == root)
        label = spec.get('mapping_label')
        _need(type(label) is str and re.fullmatch(r'[A-Za-z0-9_. :+-]{1,160}', label))
        mapping = {'id': f"{callback['guest_vsock_port']:08x}-facb-11e6-bd58-64006a7986d3", 'label': label}
        callback['windows_boot_sha256'] = boot
    else:
        _need('mapping_label' not in spec and callback.get('closure_mode') == 'windows-offline-v1')
        mapping = None
    callback.update(overrides)
    # Semantic identity is preserved except the explicit boot/held pins. A fresh
    # harmless JSON suffix separates task identities, as the audited native flow did.
    suffix = bytes(9 if bit == '1' else 32 for bit in f"{int(spec['nonce'], 16):0128b}")
    callback_raw = _encoded(callback) + b'\n' + suffix + b'\n'
    config_pin = _sha(callback_raw)
    kind = 'recovery' if recovery else 'transition'
    pipe = 'n100ha-' + ('recovery' if recovery else 'lease') + '-proof-' + spec['nonce']
    config_path = str(root / ('observer-saved-config-' + config_pin + '.json'))
    input_path = str(root / ('observer-saved-task-' + config_pin + '.json'))
    ready = str(root / ('lease-observer-' + kind + '-ready-' + config_pin + '.json'))
    arguments.update(WindowsBootSha256=boot, CallbackConfigPath=config_path,
                     CallbackConfigSha256=config_pin, PipeName=pipe, ReadyPath=ready)
    descriptor = descriptor | {'instance_sha256': config_pin, 'arguments': arguments}
    input_raw = _encoded(descriptor) + b'\n'
    _need(len(callback_raw) <= 8192 and len(input_raw) <= 8192)
    return {'role': role, 'source_input_path': spec['descriptor_path'],
            'source_input_sha256': spec['descriptor_sha256'],
            'source_callback_path': _json(spec['descriptor_raw'])['arguments']['CallbackConfigPath'],
            'source_callback_sha256': spec['callback_sha256'],
            'input_path': input_path, 'input_sha256': _sha(input_raw),
            'input_base64': base64.b64encode(input_raw).decode(),
            'config_path': config_path, 'config_sha256': config_pin,
            'config_base64': base64.b64encode(callback_raw).decode(),
            'task_name': 'N100HA_lease_observer_' + kind + '_' + config_pin,
            'mapping': mapping, 'descriptor': descriptor}


class WindowsSavedObservers:
    """Query or explicitly publish/start fresh observers; never replay an uncertain start."""
    def __init__(self, windows, *, writer_scope, roles, starter_path, starter_sha256):
        keys = {'plan_path', 'plan_sha256', 'binding_path', 'binding_sha256', 'terminal_path',
                'terminal_sha256', 'previous_windows_boot_sha256', 'windows_boot_sha256', 'vm_id_sha256'}
        _need(type(writer_scope) is dict and set(writer_scope) == keys and type(roles) is dict and set(roles) == set(ROLES))
        _need(all(bridge._hash(writer_scope[key]) for key in keys if key.endswith('_sha256') and key != 'vm_id_sha256')
              and writer_scope['previous_windows_boot_sha256'] != writer_scope['windows_boot_sha256']
              and type(writer_scope['vm_id_sha256']) is dict and set(writer_scope['vm_id_sha256']) == {'A', 'B'}
              and all(bridge._hash(pin) for pin in writer_scope['vm_id_sha256'].values())
              and len(set(writer_scope['vm_id_sha256'].values())) == 2)
        plan_path = _path(writer_scope['plan_path'])
        _need(plan_path.name == 'plan.json' and _path(writer_scope['binding_path']).parent == plan_path.parent)
        _need(writer_scope['terminal_path'] == str(plan_path.parent / 'journal' / 'controller-state.json'))
        _path(starter_path)
        _need(bridge._hash(starter_sha256))
        self.windows = windows
        self.writer = deepcopy(writer_scope)
        self.roles = {role: prepare_role(role, roles[role], writer_scope['windows_boot_sha256']) for role in ROLES}
        _need(len({value['descriptor']['arguments']['PipeName'] for value in self.roles.values()}) == 3
              and len({value['descriptor']['arguments']['KeySha256'] for value in self.roles.values()}) == 3
              and len({value['config_sha256'] for value in self.roles.values()}) == 3)
        mappings = {}
        for value in self.roles.values():
            mapping = value['mapping']
            if mapping is not None:
                _need(mapping['id'] not in mappings or mappings[mapping['id']] == mapping['label'])
                mappings[mapping['id']] = mapping['label']
        self.starter = {'path': starter_path, 'sha256': starter_sha256}

    def _call(self, mode, deadline):
        _need(mode in {'observe', 'apply'})
        bridge._remaining(deadline)
        guard = Path(__file__).with_name('windows-saved-reboot.ps1').read_bytes()
        scope = {'writer': self.writer, 'roles': self.roles, 'starter': self.starter,
                 'guard_base64': base64.b64encode(guard).decode(), 'guard_sha256': _sha(guard)}
        config = scope | {'mode': mode, 'intent_sha256': _sha(_encoded(scope))}
        blob = base64.b64encode(gzip.compress(_encoded(config), mtime=0)).decode()
        source = NATIVE_SOURCE.replace('CONFIG_GZIP_BASE64', blob)
        _need(len(source.encode()) <= 131072)
        result = self.windows.powershell(source, deadline)
        _need(type(result) is dict and set(result) == {'status', 'windows_boot_sha256', 'observers'}
              and result['status'] == 'SAVED_OBSERVERS_VERIFIED'
              and result['windows_boot_sha256'] == self.writer['windows_boot_sha256']
              and type(result['observers']) is dict and set(result['observers']) == set(ROLES))
        endpoints = {}
        for role, receipt in result['observers'].items():
            expected = self.roles[role]
            args = expected['descriptor']['arguments']
            fields = ENDPOINT | {'creation_filetime', 'ready_sha256', 'config_sha256', 'input_sha256'}
            _need(type(receipt) is dict and set(receipt) == fields
                  and type(receipt['server_pid']) is int and 1 < receipt['server_pid'] <= 2147483647
                  and receipt['pipe_name'] == args['PipeName'] and receipt['key_sha256'] == args['KeySha256']
                  and receipt['config_sha256'] == expected['config_sha256']
                  and receipt['input_sha256'] == expected['input_sha256']
                  and bridge._hash(receipt['ready_sha256'])
                  and type(receipt['creation_filetime']) is str and re.fullmatch('[1-9][0-9]{0,19}', receipt['creation_filetime']))
            endpoints[role] = {key: receipt[key] for key in ENDPOINT}
        _need(all(len({row[key] for row in endpoints.values()}) == 3 for key in ENDPOINT))
        return endpoints

    def observe(self, deadline):
        """Read-only: all three exact native processes must already be running."""
        return self._call('observe', deadline)

    def apply(self, deadline):
        """Explicit mutation: CreateNew files/missing volatile maps and start once.

        A timeout/missing response is uncertain. The caller must use observe next;
        no retry, process stop, task replacement or registry repair is performed.
        """
        return self._call('apply', deadline)


NATIVE_SOURCE = r'''
$ErrorActionPreference='Stop';Set-StrictMode -Version Latest
$enc=[Text.UTF8Encoding]::new($false,$true)
$m=[IO.MemoryStream]::new([Convert]::FromBase64String('CONFIG_GZIP_BASE64'))
$z=[IO.Compression.GZipStream]::new($m,[IO.Compression.CompressionMode]::Decompress)
$r=[IO.StreamReader]::new($z,$enc);try{$cfg=ConvertFrom-Json ($r.ReadToEnd())}finally{$r.Dispose()}
$identity=[Security.Principal.WindowsIdentity]::GetCurrent()
try{$operator=$identity.User.Value;if(-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'saved_observer_operator'}}finally{$identity.Dispose()}
$held=[Collections.Generic.List[IDisposable]]::new();$mutex=[Threading.Mutex]::new($false,'Global\N100HA-Start-VM-Guests');$locked=$false
function Need([bool]$Value){if(-not $Value){throw 'saved_observer_scope'}}
function Pin([byte[]]$Raw){$h=[Security.Cryptography.SHA256]::Create();try{[BitConverter]::ToString($h.ComputeHash($Raw)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}}
function Private([string]$Path,[bool]$Directory=$false,[string]$Reader=''){
 Need ($Path -cmatch '\AC:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}(?:\\[A-Za-z0-9_.-]+)*\z' -and $Path -cnotmatch '\\\.\.?(\\|$)')
 $leaf=Get-Item -LiteralPath $Path -Force
 Need (($Directory -and $leaf -is [IO.DirectoryInfo]) -or (-not $Directory -and $leaf -is [IO.FileInfo]))
 for($item=$leaf;$null-ne $item;$item=if($item -is [IO.FileInfo]){$item.Directory}else{$item.Parent}){
  Need (-not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint))
  $sd=[Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $item.FullName).Sddl)
  $writers=@('S-1-5-18','S-1-5-32-544',$operator)
  $root=$item.FullName -ceq 'C:\' -and $sd.Owner.Value -ceq 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
  Need (($sd.Owner.Value -cin $writers -or $root) -and $null-ne $sd.DiscretionaryAcl -and $sd.DiscretionaryAcl.Count -gt 0)
  foreach($ace in $sd.DiscretionaryAcl){
   Need ($ace -is [Security.AccessControl.CommonAce] -and -not $ace.IsCallback)
   if($ace.AceQualifier -ne 'AccessAllowed' -or ([int]$ace.AceFlags -band 8)){continue}
   $mask=if($item -is [IO.DirectoryInfo]){269287488L}else{1343029590L}
   Need (($ace.AccessMask -band $mask) -eq 0 -or $ace.SecurityIdentifier.Value -cin $writers)
   if($item.FullName -ceq $leaf.FullName -and -not $Directory){Need ($ace.SecurityIdentifier.Value -cin ($writers+@($Reader)))}
  }
 }
}
function File([string]$Path,[string]$Expected,[long]$Maximum,[string]$Reader=''){
 Private $Path $false $Reader
 $f=[IO.File]::Open($Path,'Open','Read','Read');$held.Add($f)
 Need ($f.Length -gt 0 -and $f.Length -le $Maximum)
 $raw=[byte[]]::new([int]$f.Length);$at=0
 while($at-lt $raw.Length){$n=$f.Read($raw,$at,$raw.Length-$at);Need ($n-gt 0);$at+=$n}
 Need ((Pin $raw)-ceq $Expected);return ,$raw
}
function Guard {
 $g=[Convert]::FromBase64String($cfg.guard_base64);Need ((Pin $g)-ceq $cfg.guard_sha256)
 $w=$cfg.writer
 $rows=@(& ([scriptblock]::Create($enc.GetString($g))) -Mode Observe -VmAIdSha256 $w.vm_id_sha256.A -VmBIdSha256 $w.vm_id_sha256.B -WindowsBootSha256 $w.previous_windows_boot_sha256 -PlanPath $w.plan_path -PlanSha256 $w.plan_sha256 -BindingPath $w.binding_path -BindingSha256 $w.binding_sha256 -TerminalPath $w.terminal_path -TerminalSha256 $w.terminal_sha256)
 Need ($rows.Count-eq 1);$v=ConvertFrom-Json $rows[0]
 Need ($v.mode-ceq 'Observe' -and $v.windows_boot_sha256-ceq $w.windows_boot_sha256 -and $v.states.A-ceq 'Running' -and $v.states.B-ceq 'Running' -and $v.plan_sha256-ceq $w.plan_sha256 -and $v.binding_sha256-ceq $w.binding_sha256 -and $v.terminal_sha256-ceq $w.terminal_sha256)
}
function NewPrivate([string]$Path,[byte[]]$Bytes){
 Private ([IO.Path]::GetDirectoryName($Path)) $true
 $security=[Security.AccessControl.FileSecurity]::new();$security.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'));$security.SetAccessRuleProtection($true,$false)
 foreach($sid in @('S-1-5-18','S-1-5-32-544')){$security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),'FullControl','Allow'))}
 $f=[IO.FileStream]::new($Path,[IO.FileMode]::CreateNew,[Security.AccessControl.FileSystemRights]'WriteData,Synchronize',[IO.FileShare]::None,4096,[IO.FileOptions]::WriteThrough,$security)
 try{$f.Write($Bytes,0,$Bytes.Length);$f.Flush($true)}finally{$f.Dispose()}
 [IO.File]::SetAttributes($Path,[IO.FileAttributes]::ReadOnly)
}
function Mapping($Spec,[switch]$ReadOnly,[switch]$CheckAbsent){
 if($null-eq $Spec){return}
 $root='SOFTWARE\Microsoft\Windows NT\CurrentVersion\Virtualization\GuestCommunicationServices'
 $write=$cfg.mode-ceq 'apply' -and -not $ReadOnly -and -not $CheckAbsent
 $proof=Join-Path $decisionRoot ('saved-map-'+$cfg.writer.windows_boot_sha256+'-'+$Spec.id+'.json')
 $proofBytes=$enc.GetBytes($cfg.intent_sha256+'|'+$Spec.id+'|'+$Spec.label)
 $parent=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($root,$write);Need ($null-ne $parent)
 try{
  $key=$parent.OpenSubKey($Spec.id,$false)
  if($CheckAbsent){
   if($null-ne $key){$key.Dispose();throw 'saved_observer_prior_mapping'}
   Need (-not (Test-Path -LiteralPath $proof))
   return
  }
  if($null-eq $key){
   Need $write;Guard
   $handle=[N100SavedObserverRegistry]::CreateNew($parent.Handle,$Spec.id)
   $created=[Microsoft.Win32.RegistryKey]::FromHandle($handle)
   try{$created.SetValue('ElementName',$Spec.label,[Microsoft.Win32.RegistryValueKind]::String);$created.Flush()}finally{$created.Dispose()}
   $key=$parent.OpenSubKey($Spec.id,$false)
   try{Need ($null-ne $key -and $key.GetValue('ElementName')-ceq $Spec.label)
       NewPrivate $proof $proofBytes
   }catch{if($null-ne $key){$key.Dispose()};throw}
  }
  try{
   Need ($null-ne $key -and $key.SubKeyCount-eq 0 -and $key.ValueCount-eq 1 -and @($key.GetValueNames())[0]-ceq 'ElementName' -and $key.GetValue('ElementName')-ceq $Spec.label)
   $null=File $proof (Pin $proofBytes) 4096
  }finally{if($null-ne $key){$key.Dispose()}}
 }finally{$parent.Dispose()}
}
function ExpectedAction($s){
 $d=$s.descriptor;$a=$d.arguments
 $pins=if($s.role-ceq 'legacy'){@('PlanSha256','AttemptId','WindowsBootSha256','WslBootSha256','CollectorSha256','ReactivationPlanSha256','MarkerSha256','CallbackSha256','CallbackConfigSha256','KeySha256')}else{@('PlanSha256','AttemptId','WindowsBootSha256','CallbackSha256','CallbackConfigSha256','KeySha256','RecoveryProtocolSha256')}
 $paths=@('CallbackPath','CallbackConfigPath','KeyPath','ReadyPath');if($s.role-cne 'legacy'){$paths+='RecoveryProtocolPath'}
 $required=$pins+$paths+@('PipeName','OperatorSid','ServiceSid','Generation')
 Need ((@($a.PSObject.Properties.Name|Sort-Object)-join ',')-ceq (@($required|Sort-Object)-join ',') -and $a.OperatorSid-ceq $operator -and $a.Generation-eq 1 -and $a.WindowsBootSha256-ceq $cfg.writer.windows_boot_sha256)
 $command='$ErrorActionPreference="Stop";$p="'+$d.server_path+'";'+'$f=Get-Item -LiteralPath $p -Force;if($f -isnot [IO.FileInfo] -or ($f.Attributes -band 1024) -or $f.Length -gt 65536){exit 1};'+'$b=[IO.File]::ReadAllBytes($p);$h=[Security.Cryptography.SHA256]::Create();try{$s=[BitConverter]::ToString($h.ComputeHash($b)).Replace("-","").ToLowerInvariant()}finally{$h.Dispose()};'+'if($s -cne "'+$d.server_sha256+'"){exit 1}; & ([ScriptBlock]::Create([Text.UTF8Encoding]::new($false,$true).GetString($b)))'
 foreach($name in $required){$command+=' -'+$name+" '"+$a.$name+"'"};$command+=' -Go'
 return '-NoLogo -NoProfile -NonInteractive -EncodedCommand '+[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
}
function Observe($s,[string]$Action){
 $a=$s.descriptor.arguments;$task=Get-ScheduledTask -TaskPath '\' -TaskName $s.task_name -ErrorAction Stop
 Need ($task.State-ceq 'Running' -and @($task.Actions).Count-eq 1 -and @($task.Triggers|Where-Object {$null-ne $_}).Count-eq 0 -and $task.Settings.Enabled -and [string]$task.Settings.MultipleInstances-ceq 'IgnoreNew' -and $task.Settings.RestartCount-eq 0 -and [Xml.XmlConvert]::ToTimeSpan($task.Settings.ExecutionTimeLimit).Ticks-eq 0 -and [string]$task.Principal.LogonType-ceq 'S4U' -and [string]$task.Principal.RunLevel-ceq 'Highest')
 $sid=if($task.Principal.UserId-cmatch '^S-1-'){$task.Principal.UserId}else{([Security.Principal.NTAccount]::new($task.Principal.UserId)).Translate([Security.Principal.SecurityIdentifier]).Value}
 $engine=Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'
 Need ($sid-ceq $operator -and $task.Actions[0].Execute-ceq $engine -and $task.Actions[0].Arguments-ceq $Action -and [string]::IsNullOrEmpty($task.Actions[0].WorkingDirectory))
 $scheduler=New-Object -ComObject 'Schedule.Service';$scheduler.Connect();$owned=$scheduler.GetFolder('\').GetTask($s.task_name)
 $instances=@($owned.GetInstances(0));Need ($instances.Count-eq 1 -and $instances[0].State-eq 4)
 $pin=(Get-FileHash -LiteralPath $a.ReadyPath -Algorithm SHA256).Hash.ToLowerInvariant()
 $ready=ConvertFrom-Json ($enc.GetString((File $a.ReadyPath $pin 1024)))
 $scope=if($s.role-ceq 'legacy'){'n100ha-legacy-closure-v1'}else{'n100ha-initial-recovery-v1'}
 Need ($ready.scope-ceq $scope -and $ready.callback_config_sha256-ceq $s.config_sha256 -and $ready.pipe_name-ceq $a.PipeName -and $ready.server_sid-ceq $operator -and $ready.plan_sha256-ceq $a.PlanSha256 -and $ready.attempt_id-ceq $a.AttemptId -and $ready.pid-gt 1)
 $p=Get-Process -Id $ready.pid -ErrorAction Stop
 try{
  $native=Get-CimInstance Win32_Process -Filter ('ProcessId='+[int]$ready.pid) -OperationTimeoutSec 5
  $owner=Invoke-CimMethod -InputObject $native -MethodName GetOwnerSid -ErrorAction Stop
  Need (-not $p.HasExited -and $p.SessionId-eq 0 -and $p.MainModule.FileName-ieq $engine -and $p.StartTime.ToFileTimeUtc().ToString()-ceq $ready.creation_filetime.ToString() -and $owner.ReturnValue-eq 0 -and $owner.Sid-ceq $operator -and $native.CommandLine.EndsWith((' '+$Action),[StringComparison]::Ordinal))
 }finally{$p.Dispose()}
 return [ordered]@{server_pid=[int]$ready.pid;creation_filetime=$ready.creation_filetime.ToString();ready_sha256=$pin;pipe_name=$a.PipeName;key_sha256=$a.KeySha256;config_sha256=$s.config_sha256;input_sha256=$s.input_sha256}
}
try{
 Need ($PSVersionTable.PSEdition-ceq 'Desktop' -and [Environment]::Is64BitProcess -and $cfg.mode-cin @('observe','apply'))
 try{$locked=$mutex.WaitOne(0)}catch [Threading.AbandonedMutexException]{$locked=$true;throw 'saved_observer_lock_abandoned'};Need $locked
 Guard
 Add-Type -TypeDefinition @'
using System;using System.Runtime.InteropServices;using Microsoft.Win32.SafeHandles;
public static class N100SavedObserverRegistry {
 [DllImport("advapi32.dll",CharSet=CharSet.Unicode)]static extern int RegCreateKeyEx(SafeRegistryHandle p,string n,int r,string c,int o,int a,IntPtr s,out SafeRegistryHandle k,out int d);
 public static SafeRegistryHandle CreateNew(SafeRegistryHandle p,string n){SafeRegistryHandle h;int d;int rc=RegCreateKeyEx(p,n,0,null,1,0x2001f,IntPtr.Zero,out h,out d);if(rc!=0||d!=1){if(h!=null)h.Dispose();throw new InvalidOperationException("saved_observer_mapping_occupied");}return h;}
}
'@ -ErrorAction Stop
 $starter=File $cfg.starter.path $cfg.starter.sha256 65536
 $plan=ConvertFrom-Json ($enc.GetString((File $cfg.writer.plan_path $cfg.writer.plan_sha256 1048576 $cfg.roles.legacy.descriptor.arguments.ServiceSid)))
 Need ($cfg.intent_sha256 -cmatch '\A[a-f0-9]{64}\z')
 $decisionRoot=[IO.Path]::GetDirectoryName($cfg.writer.plan_path)
 $decision=Join-Path $decisionRoot ('saved-observer-'+$cfg.writer.windows_boot_sha256+'.issued.json')
 $intent=$enc.GetBytes($cfg.intent_sha256)
 $actions=@{}
 foreach($role in @('legacy','held_admission','provider_isolation')){
  Guard;$s=$cfg.roles.$role;$a=$s.descriptor.arguments
  Need ($a.ServiceSid-ceq $plan.windows_enforced.service_sid -and $a.PlanSha256-ceq $plan.initial_cutover_handoff.legacy_plan_sha256 -and $a.AttemptId-ceq $plan.initial_cutover_handoff.attempt_id)
  $original=ConvertFrom-Json ($enc.GetString((File $s.source_input_path $s.source_input_sha256 8192 $a.ServiceSid)))
  $oldEndpoint=if($role-ceq 'legacy'){$plan.windows_initial_observer}else{$plan.windows_recovery_observers.$role}
  Need ($original.arguments.PipeName-ceq $oldEndpoint.pipe_name -and $original.arguments.KeySha256-ceq $oldEndpoint.key_sha256 -and $original.arguments.WindowsBootSha256-ceq $cfg.writer.previous_windows_boot_sha256 -and $original.arguments.CallbackConfigSha256-ceq $s.source_callback_sha256 -and $original.server_path-ceq $s.descriptor.server_path -and $original.server_sha256-ceq $s.descriptor.server_sha256)
  foreach($field in @('PlanSha256','AttemptId','CallbackPath','CallbackSha256','KeyPath','KeySha256','OperatorSid','ServiceSid','Generation')){Need ($original.arguments.$field-ceq $a.$field)}
  if($role-ceq 'legacy'){
   foreach($field in @('WslBootSha256','CollectorSha256','ReactivationPlanSha256','MarkerSha256')){Need ($original.arguments.$field-ceq $a.$field)}
   Need ($a.WslBootSha256-ceq $oldEndpoint.wsl_boot_sha256 -and $a.CollectorSha256-ceq $oldEndpoint.collector_sha256 -and $a.MarkerSha256-ceq $oldEndpoint.marker_sha256)
  }else{foreach($field in @('RecoveryProtocolPath','RecoveryProtocolSha256')){Need ($original.arguments.$field-ceq $a.$field)}}
  $null=File $s.source_callback_path $s.source_callback_sha256 8192 $a.ServiceSid
  $null=File $s.descriptor.server_path $s.descriptor.server_sha256 65536 $a.ServiceSid
  $null=File $a.CallbackPath $a.CallbackSha256 65536 $a.ServiceSid
  $key=File $a.KeyPath $a.KeySha256 64 $a.ServiceSid;Need ($key.Length-eq 32);[Array]::Clear($key,0,$key.Length)
  if($role-cne 'legacy'){$null=File $a.RecoveryProtocolPath $a.RecoveryProtocolSha256 65536 $a.ServiceSid}
  $actions[$role]=ExpectedAction $s
 }
 if($cfg.mode-ceq 'apply'){
  foreach($role in @('legacy','held_admission','provider_isolation')){Mapping $cfg.roles.$role.mapping -CheckAbsent}
  Guard
  NewPrivate $decision $intent
 }
 $null=File $decision (Pin $intent) 4096
 $facts=[ordered]@{}
 foreach($role in @('legacy','held_admission','provider_isolation')){
  Guard;$s=$cfg.roles.$role;$a=$s.descriptor.arguments;$action=$actions[$role];Mapping $s.mapping
  $task=Get-ScheduledTask -TaskPath '\' -TaskName $s.task_name -ErrorAction SilentlyContinue
  if($cfg.mode-ceq 'apply' -and $null-eq $task){
   Need (-not (Test-Path -LiteralPath $a.ReadyPath) -and -not (Test-Path -LiteralPath ($a.ReadyPath+'.outcome.json')))
   $configExists=Test-Path -LiteralPath $s.config_path;$inputExists=Test-Path -LiteralPath $s.input_path
   Need ($configExists-eq $inputExists)
   if(-not $configExists){Guard;NewPrivate $s.config_path ([Convert]::FromBase64String($s.config_base64));NewPrivate $s.input_path ([Convert]::FromBase64String($s.input_base64))}
   $null=File $s.config_path $s.config_sha256 8192;$null=File $s.input_path $s.input_sha256 8192
   Guard
   $rows=@(& ([scriptblock]::Create($enc.GetString($starter))) -InputPath $s.input_path -InputSha256 $s.input_sha256 -Go)
   Need ($rows.Count-eq 1);$started=ConvertFrom-Json $rows[0];Need ($started.observer_ready-ceq $true)
  }
  $null=File $s.config_path $s.config_sha256 8192;$null=File $s.input_path $s.input_sha256 8192
  $facts[$role]=Observe $s $action
  if($cfg.mode-ceq 'apply' -and $null-eq $task){Need ($facts[$role].server_pid-eq $started.observer_pid)}
  Guard
 }
 Guard
 foreach($role in @('legacy','held_admission','provider_isolation')){Mapping $cfg.roles.$role.mapping -ReadOnly}
 @{status='SAVED_OBSERVERS_VERIFIED';windows_boot_sha256=$cfg.writer.windows_boot_sha256;observers=$facts}|ConvertTo-Json -Depth 5 -Compress
}catch{exit 1}finally{foreach($h in $held){$h.Dispose()};if($locked){$mutex.ReleaseMutex()};$mutex.Dispose()}
'''
