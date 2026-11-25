"""Prepare two independent, inactive service-host paths for saved recovery."""

from pathlib import Path, PureWindowsPath
import time

from .native_sameboot_release import WINDOWS_GUARD, WindowsPairClosure
from .native_sameboot_successor import host_path
from .native_saved_site_material import _b64, _join_encoded, _sha
from .native_stdio_bridge import _hash, _need as require
from .sameboot_transition import _fallback_attempt


def _need(ok):
    require(ok, 'saved_site_host_unconfirmed')


class SavedSiteHostPaths:
    """Stage only a checked EXE. The active SCM command and process stay intact."""

    def __init__(self, pair, scope, *, recovery_attempt, artifact, private_io_sha256):
        _need(type(pair) is WindowsPairClosure and scope.get('saved_reboot') is True
              and pair.config['plan_sha256'] == scope['previous_plan_sha256']
              and pair.config['binding_sha256'] == scope['previous_instance_sha256']
              and pair.config['boot_sha256'] == scope['previous_windows_boot_sha256']
              and pair.config['revision'] == scope['previous_revision']
              and _hash(scope['attempt_sha256']) and _hash(scope['history_sha256'])
              and _hash(recovery_attempt) and type(artifact) is bytes and 0 < len(artifact) <= 32768
              and artifact[:2] == b'MZ' and _sha(artifact) == scope['successor_image_sha256'])
        io = (Path(__file__).parent / 'stopped_service_installer' / 'PrivateIo.cs').read_bytes()
        _need(_hash(private_io_sha256) and _sha(io) == private_io_sha256)
        self.pair = pair
        self.scope = scope.copy()
        self.artifact = artifact
        self.io = io
        self.io_pin = private_io_sha256
        self.paths = [str(host_path(recovery_attempt, attempt)) for attempt in
                      (scope['attempt_sha256'], _fallback_attempt(scope))]
        _need(len(set(self.paths)) == 2)
        self.root = str(PureWindowsPath(self.paths[0]).parents[2])

    def verify(self, deadline, *, mode='observe', stopped_terminal=None):
        _need(mode in {'preflight', 'apply', 'observe'} and time.monotonic() < deadline)
        _need(stopped_terminal is None or (mode == 'observe' and _hash(stopped_terminal)))
        probe, challenge = self.pair._command('snapshot' if stopped_terminal is not None else 'check_release')
        config = {'mode': mode, 'probe': _b64(probe.encode()), 'probe_pin': _sha(probe.encode()),
                  'challenge': challenge, 'stopped_terminal': stopped_terminal, 'plan_path': self.pair.config['plan'],
                  'plan_sha256': self.scope['previous_plan_sha256'], 'root': self.root, 'paths': self.paths,
                  'image': _b64(self.artifact), 'image_pin': self.scope['successor_image_sha256'],
                  'io': _b64(self.io), 'io_pin': self.io_pin}
        helpers = WINDOWS_GUARD[WINDOWS_GUARD.index('function Pin'):]
        source = NATIVE.replace('__CONFIG__', _join_encoded(config)).replace('__GUARDS__', helpers)
        _need(len(source.encode()) <= 131072)
        result = self.pair.windows.powershell(source, deadline)
        _need(result == {'status': 'HOST_PATHS_VERIFIED', 'mode': mode, 'candidate_sha256': config['image_pin'],
                         'count': 2, 'active_service_changed': False})
        return result


NATIVE = r"""
$ErrorActionPreference='Stop';Set-StrictMode -Version Latest
$enc=[Text.UTF8Encoding]::new($false,$true)
$allowed=@('S-1-5-18','S-1-5-32-544')
$mem=[IO.MemoryStream]::new([Convert]::FromBase64String('__CONFIG__'))
$zip=[IO.Compression.GZipStream]::new($mem,[IO.Compression.CompressionMode]::Decompress)
$reader=[IO.StreamReader]::new($zip,$enc)
try{$cfg=ConvertFrom-Json ($reader.ReadToEnd())}finally{$reader.Dispose()}
$mutex=[Threading.Mutex]::new($false,'Global\N100HA-Start-VM-Guests');$locked=$false
$rootHandle=$null
__GUARDS__
function Need([bool]$ok){if(-not $ok){throw 'saved_host_scope'}}
function AssertLive {
 $raw=[Convert]::FromBase64String($cfg.probe)
 Need ((Pin $raw) -ceq $cfg.probe_pin)
 $rows=@(& ([scriptblock]::Create($enc.GetString($raw))))
 Need ($rows.Count -eq 1)
 $reply=ConvertFrom-Json $rows[0]
 Need ($reply.challenge -ceq $cfg.challenge)
 if($null -eq $cfg.stopped_terminal){Need ($reply.status -ceq 'COOPERATIVE_RELEASE_READY')}
 else{Need ($reply.status -ceq 'STOPPED' -and $reply.terminal_sha256 -ceq $cfg.stopped_terminal)}
}
try{
 Need ($PSVersionTable.PSEdition -ceq 'Desktop' -and [Environment]::Is64BitProcess -and $cfg.mode -cin @('preflight','apply','observe'))
 try{$locked=$mutex.WaitOne(0)}catch [Threading.AbandonedMutexException]{$locked=$true;throw 'saved_host_lock'}
 Need $locked
 AssertLive
 $planRaw=[IO.File]::ReadAllBytes($cfg.plan_path)
 Need ((Pin $planRaw) -ceq $cfg.plan_sha256)
 $plan=ConvertFrom-Json ($enc.GetString($planRaw));$sid=$plan.windows_enforced.service_sid
 Need ($sid -cmatch '\AS-1-5-80(?:-[0-9]{1,10}){5}\z' -and $cfg.paths.Count -eq 2 -and
       $cfg.root -ceq ('C:\ProgramData\N100HA_'+$plan.initial_cutover_recovery.attempt_id.Substring(0,24)) -and
       $plan.windows_enforced.service_image_sha256 -cne $cfg.image_pin)
 $null=Assert-Path $cfg.root $true $sid
 $bytes=[Convert]::FromBase64String($cfg.image)
 Need ($bytes.Length -gt 0 -and $bytes.Length -le 32768 -and (Pin $bytes) -ceq $cfg.image_pin)
 $io=[Convert]::FromBase64String($cfg.io);Need ((Pin $io) -ceq $cfg.io_pin)
 $support=$enc.GetString($io).Replace('namespace TrustedStoppedV1 {',@'
namespace TrustedStoppedV1 {
 public sealed class Failure : Exception { public Failure(int code) : base("saved_host_io") {} }
 internal static class Contract { internal static void Need(bool ok) { if(!ok) throw new Failure(0); } }
'@)
 Add-Type -TypeDefinition $support -ErrorAction Stop
 $rootHandle=[TrustedStoppedV1.Held]::new($cfg.root,$true)
 $homes=@()
 foreach($path in $cfg.paths){
  $hostDir=[IO.Path]::GetDirectoryName($path);$attempt=[IO.Path]::GetDirectoryName($hostDir)
  Need ($path -ceq (Join-Path $hostDir 'windows-service-host.exe') -and
        $hostDir -ceq (Join-Path $attempt 'host') -and
        [IO.Path]::GetFileName($attempt) -cmatch '\Apostboot-[a-f0-9]{64}\z' -and
        [IO.Path]::GetDirectoryName($attempt) -ceq $cfg.root)
  if($cfg.mode -ceq 'observe'){
   Current $path 32768 $cfg.image_pin $sid
   Need (-not (Test-Path -LiteralPath (Join-Path $hostDir 'service.args')))
  }else{
   Need (-not (Test-Path -LiteralPath $attempt))
   $homes+=@{attempt=$attempt;home=$hostDir;image=$path}
  }
 }
 if($cfg.mode -ceq 'apply'){
  foreach($entry in $homes){
   $rootHandle.Verify()
   [TrustedStoppedV1.NewPrivate]::Directory($entry.attempt,$sid,$false)
   [TrustedStoppedV1.NewPrivate]::Directory($entry.home,$sid,$false)
   [TrustedStoppedV1.NewPrivate]::File($entry.image,$bytes,$sid,$true)
   Current $entry.image 32768 $cfg.image_pin $sid
   Need (-not (Test-Path -LiteralPath (Join-Path $entry.home 'service.args')))
  }
 }
 $rootHandle.Verify()
 AssertLive
 @{status='HOST_PATHS_VERIFIED';mode=$cfg.mode;candidate_sha256=$cfg.image_pin;count=2;active_service_changed=$false}|ConvertTo-Json -Compress
}finally{if($null-ne $rootHandle){$rootHandle.Dispose()};if($locked){$mutex.ReleaseMutex()};$mutex.Dispose()}
"""
