"""New profile protocol/real PowerShell AST fixtures, never native acceptance."""
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'infra/k8s'))
from ha_control import enforced_windows as E
from ha_control.controller import TransitionBlocked

DRIVER = ROOT / 'infra/k8s/ha_control/windows-enforced-vm.ps1'


def fixtures():
    cfg = dict(driver_sha256=hashlib.sha256(DRIVER.read_bytes()).hexdigest(),
        adapter_sha256=hashlib.sha256(Path(E.__file__).read_bytes()).hexdigest(),
        policy_path=r'C:\Private\policy.json', policy_sha256='a' * 64,
        service_name='FixtureBroker', service_sid='S-1-5-80-1-2-3-4-5',
        boot_ticks='638000000000000000', session_id='b' * 64,
        python_path=r'C:\Private\python.exe', python_sha256='c' * 64,
        launcher_path=r'C:\Private\launcher.py', launcher_sha256='d' * 64,
        service_image_sha256='e' * 64, vm_id='11111111-1111-1111-1111-111111111111',
        vm_notes_sha256=hashlib.sha256(b'fixture-notes').hexdigest(), vm_config_root=r'C:\Private\VM',
        disks=[dict(path=r'C:\Private\disk.vhdx', disk_id='22222222-2222-2222-2222-222222222222',
                    file_identity_sha256=hashlib.sha256(b'fixture-file-id').hexdigest())])
    plan = dict(schema_version=2, profile=E.PROFILE, source_commit='f' * 40,
                cluster='fixture', node='A', generation=3, windows_enforced=cfg,
                pg_nodes={node: dict(sslrootcert=r'C:\Private\ca.pem', passfile=r'C:\Private\pgpass') for node in ('A','B')})
    raw = json.dumps(plan).encode()
    plan_hash = hashlib.sha256(raw).hexdigest()
    approval = dict(schema_version=2, profile=E.PROFILE, plan_sha256=plan_hash,
        source_commit=plan['source_commit'], policy_sha256=cfg['policy_sha256'], action='ha-transition',
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec='seconds').replace('+00:00', 'Z'))
    policy = {key: deepcopy(cfg[key]) for key in ('session_id','service_sid','boot_ticks','vm_id','vm_notes_sha256','vm_config_root','disks')}
    policy.update(schema_version=1, profile=E.PROFILE, cluster=plan['cluster'], generation=plan['generation'],
                  trusted_admin_sids=['S-1-5-21-1'], hyperv_member_sids=[cfg['service_sid']],
                  host_admins_tcb_acknowledged=True)
    return plan, approval, plan_hash, policy


def installed_fixtures():
    plan,_,_,policy=fixtures()
    plan['installation_id']='6'*64
    plan_hash=hashlib.sha256(json.dumps(plan).encode()).hexdigest()
    authority={'schema_version':3,'profile':E.PROFILE,'action':'ha-transition','authority_kind':'installed-runtime',
               'installation_id':plan['installation_id'],'installed_at':(datetime.now(timezone.utc)-timedelta(days=30)).isoformat(timespec='seconds').replace('+00:00','Z'),
               'plan_sha256':plan_hash,'source_commit':plan['source_commit'],'policy_sha256':plan['windows_enforced']['policy_sha256']}
    return plan,authority,plan_hash,policy


class FakeInput:
    def __init__(self, adapter, replies):
        self.adapter, self.replies = adapter, replies
        self.requests = []
    def write(self, payload):
        request = json.loads(payload)
        self.requests.append(request)
        frame = self.adapter._base() | dict(status='ok', broker_instance=self.adapter.instance,
            nonce=request['nonce'], sequence=request['sequence'], node='A', generation=self.adapter.plan_data['generation'], vm_id=self.adapter.cfg['vm_id'], fenced=True)
        transform = self.replies.pop(0) if self.replies else lambda value: value
        self.adapter._answers.put(json.dumps(transform(frame)))
    def flush(self):
        pass


def adapter(replies=()):
    plan, approval, plan_hash, _ = fixtures()
    value = object.__new__(E.EnforcedWindowsFence)
    value.plan_data, value.cfg = plan, plan['windows_enforced']
    value.plan_sha256, value.approval_sha256 = plan_hash, '9' * 64
    value.expires = datetime.now(timezone.utc) + timedelta(hours=1)
    value.instance, value._healthy, value._sequence = '8' * 64, True, 1
    value._lock, value._answers = threading.Lock(), queue.Queue()
    value.process = type('Process', (), {'poll': lambda _: None})()
    value.process.stdin = FakeInput(value, list(replies))
    return value


class EnforcedWindowsTests(unittest.TestCase):
    def test_trusted_installer_owner_is_only_a_higher_os_volume_root(self):
        body=r'''
function Get-EnforcedOsVolumeRoot{return 'C:\'}
$ti='S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
if(-not (Test-EnforcedOsRootOwner $ti 'c:\' 2)){throw 'os_root_rejected'}
foreach($case in @(@($ti,'C:\',0),@($ti,'C:\',1),@($ti,'C:\Private',3),@($ti,'D:\',3),@($ti,'C:\..\',3),@('S-1-1-0','C:\',3))){
 if(Test-EnforcedOsRootOwner $case[0] $case[1] ([int]$case[2])){throw 'unsafe_owner_exception'}
}
$denied=$false
try{Assert-EnforcedAllowedMask $ti 268435456 @('S-1-5-18','S-1-5-32-544') 1343029590}catch{$denied=$true}
if(-not $denied){throw 'owner_exception_granted_write_ace'}
'owner-only-root-context'
'''
        self.assertEqual(self.ps(body).strip(), 'owner-only-root-context')

    @classmethod
    def setUpClass(cls):
        cls.pwsh = shutil.which('pwsh') or '/private/tmp/n100-ha-powershell/runtime/pwsh'
        if not Path(cls.pwsh).is_file():
            raise AssertionError('PowerShell required; no skipped profile fixtures')

    def ps(self, body, setup='', *, installed=False):
        plan, approval, plan_hash, policy = installed_fixtures() if installed else fixtures()
        prefix = r'''
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
. $env:ENFORCED_DRIVER
$script:realPgFiles=(Get-Command Get-EnforcedPgFiles).ScriptBlock
$script:realDataReaders=(Get-Command Assert-EnforcedDataReaders).ScriptBlock
$script:realJournal=(Get-Command Assert-EnforcedJournal).ScriptBlock
function Assert-EnforcedJournal($Config,$Policy,$Watch){}
$config=Convert-EnforcedJson $env:ENFORCED_CONFIG
$approved=Convert-EnforcedJson $env:ENFORCED_APPROVED
$policy=Convert-EnforcedJson $env:ENFORCED_POLICY
$planHash=$env:ENFORCED_PLAN_HASH;$approvalHash='9'*64;$instance='8'*64
$script:Plan='C:\Private\plan.json';$script:Approval='C:\Private\approval.json'
$script:enforcedSequence=[long]0
$script:trace=[Collections.Generic.List[string]]::new();$script:powerOff=$false
$request=[pscustomobject]@{action='observe';node='A';generation=3;nonce=(('7'*48)+'0000000000000001');broker_instance=$instance;sequence=[long]1}
function Read-EnforcedPrivate($Path,$Watch){
 if($Path -ceq $script:Plan){return @{sha256=$planHash;bytes=@()}}
 if($Path -ceq $script:Approval){return @{sha256=$approvalHash;bytes=@()}}
 if($Path -ceq $config.windows_enforced.policy_path){return @{sha256=$config.windows_enforced.policy_sha256;bytes=[Text.Encoding]::UTF8.GetBytes($env:ENFORCED_POLICY)}}
 throw 'fixture_unknown_path'
}
function Read-EnforcedCodeHash($Path,$Watch){if($Path.EndsWith('enforced_windows.py')){return $config.windows_enforced.adapter_sha256};return $config.windows_enforced.driver_sha256}
function Get-EnforcedPgFiles($Config,$Policy,$Watch){return 'fixture-private-file-identities'}
function Stop-VM($Id,[switch]$TurnOff,[switch]$Force,$Confirm,$ErrorAction){
 if($Id.ToString() -cne $config.windows_enforced.vm_id -or -not $TurnOff -or -not $Force -or $Confirm -ne $false){throw 'unsafe_vm_target'}
 $script:trace.Add('turnoff');$script:powerOff=$true
}
'''
        env = dict(__import__('os').environ, ENFORCED_DRIVER=str(DRIVER),
                   ENFORCED_CONFIG=json.dumps(plan), ENFORCED_APPROVED=json.dumps(approval),
                   ENFORCED_POLICY=json.dumps(policy), ENFORCED_PLAN_HASH=plan_hash)
        encoded = base64.b64encode((prefix + setup + body).encode('utf-16le')).decode()
        result = subprocess.run([self.pwsh, '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                                env=env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        return result.stdout.decode('utf-8-sig')

    def test_pg_file_acl_provider_boundaries_and_identity_change(self):
        result = self.ps(r'''
$script:writes=0;$script:reads=0;$script:bad=''
function Assert-EnforcedPath($Path,$Writers){
 if($config.windows_enforced.service_sid -in $Writers){throw 'service_write_granted'}
 if('S-1-5-18' -notin $Writers -or 'S-1-5-21-1' -notin $Writers){throw 'missing_tcb'}
 $script:writes++;if($script:bad -ceq 'write'){throw 'fixture_write_acl_rejected'}
}
function Get-Item($LiteralPath,[switch]$Force,$ErrorAction){
 return [pscustomobject]@{FullName=$LiteralPath;PSIsContainer=($script:bad -ceq 'directory');Length=16;Directory=[pscustomobject]@{FullName='C:\Private'}}
}
function Get-EnforcedPathDescriptor($Path){return $Path}
function Assert-EnforcedDescriptor($Descriptor,$Readers,$Mask,$IgnoreInheritOnly){
 if($Mask -ne 2415919105 -or -not $IgnoreInheritOnly -or $config.windows_enforced.service_sid -notin $Readers){throw 'reader_boundary_missing'}
 $script:reads++;if($script:bad -ceq 'read'){throw 'fixture_public_read_rejected'}
}
function Get-EnforcedFileIdentity($Path){return ('file-id-'+$script:bad)}
$first=& $script:realPgFiles $config $policy ([Diagnostics.Stopwatch]::StartNew())
if($script:writes -ne 4 -or $script:reads -ne 8){throw 'missing_pg_path_checks'}
foreach($bad in @('read','write','directory')){
 $script:bad=$bad;$rejected=$false
 try{& $script:realPgFiles $config $policy ([Diagnostics.Stopwatch]::StartNew())}catch{$rejected=$true}
 if(-not $rejected){throw 'private_file_boundary_accepted'}
}
$script:bad='changed'
if((& $script:realPgFiles $config $policy ([Diagnostics.Stopwatch]::StartNew())) -ceq $first){throw 'identity_change_hidden'}
$script:bad='';$config.pg_nodes.A.passfile=$null;$rejected=$false
try{& $script:realPgFiles $config $policy ([Diagnostics.Stopwatch]::StartNew())}catch{$rejected=$true}
if(-not $rejected){throw 'missing_private_file_accepted'}
'ok'
''')
        self.assertEqual(result.strip(), 'ok')

    def test_mutable_journal_private_child_keeps_authority_parent_immutable(self):
        self.assertEqual(self.ps(r'''
$script:bad='';$script:checked=[Collections.Generic.List[string]]::new();$script:grantChecks=0
function Assert-EnforcedPath($Path,$Writers){
 if($Path -cne 'C:\Private' -or $config.windows_enforced.service_sid -in $Writers){throw 'authority_parent_writable'}
}
function Get-Item($LiteralPath,[switch]$Force,$ErrorAction){return [pscustomobject]@{FullName=$LiteralPath;PSIsContainer=($script:bad -cne 'notdir');Attributes=0}}
function Get-EnforcedJournalChildren($Root){
 if($Root -cne 'C:\Private\journal'){throw 'wrong_journal_root'}
 $names=@('controller-state.json','controller-state.lock','.ha-journal-abcdefgh')
 if($script:bad -ceq 'extra'){$names+=@('plan.json')}
 return @($names|ForEach-Object {[pscustomobject]@{Name=$_;FullName=($Root+'\'+$_);PSIsContainer=$false;Attributes=0}})
}
function Get-EnforcedPathDescriptor($Path){return $Path}
function Assert-EnforcedDescriptor($Descriptor,$Allowed,$Mask,$IgnoreInheritOnly){
 if($Mask -ne 3490513239 -or -not $IgnoreInheritOnly){throw 'journal_mask_incomplete'}
 $script:checked.Add($Descriptor)
 Assert-EnforcedAllowedMask $config.windows_enforced.service_sid 2 $Allowed $Mask
 Assert-EnforcedAllowedMask $config.windows_enforced.service_sid 1 $Allowed $Mask
 if($script:bad -ceq 'write'){Assert-EnforcedAllowedMask 'S-1-5-32-545' 2 $Allowed $Mask}
 if($script:bad -ceq 'read'){Assert-EnforcedAllowedMask 'S-1-1-0' 1 $Allowed $Mask}
}
function global:Read-EnforcedJournalGrant($Path,$Sid){
 $script:grantChecks++
 if($Sid -cne $config.windows_enforced.service_sid){throw 'wrong_journal_sid'}
 if($script:bad -ceq 'readonly'){Assert-EnforcedJournalGrant 1 $false}
 elseif($script:bad -ceq 'denied'){Assert-EnforcedJournalGrant 197055 $true}
 else{Assert-EnforcedJournalGrant 197055 $false}
}
function Get-EnforcedFileIdentity($Path){if($script:bad -ceq 'hardlink'){throw 'fixture_hardlink_rejected'};return $Path}
# Compile the saved production body in this provider fixture context instead
# of depending on command-cache resolution in the dot-sourced file's scope.
$script:fixtureJournal=[ScriptBlock]::Create($script:realJournal.ToString())
& $script:fixtureJournal $config $policy ([Diagnostics.Stopwatch]::StartNew())
if($script:grantChecks -ne 4){throw 'fixture_grant_provider_not_used'}
if($script:checked.Count -ne 4){throw 'journal_file_or_lock_unchecked'}
$closed=0
foreach($bad in @('notdir','extra','write','read','hardlink','readonly','denied')){
 $script:bad=$bad;try{& $script:fixtureJournal $config $policy ([Diagnostics.Stopwatch]::StartNew())}catch{$closed++}
}
if($closed -ne 7){throw 'journal_boundary_accepted'}
'journal-provider-only'
''').strip(),'journal-provider-only')

    def test_sensitive_vm_and_vhd_reader_boundaries_real_mask_logic(self):
        self.assertEqual(self.ps(r'''
$vmSid='S-1-5-83-1';$writers=@('S-1-5-18','S-1-5-32-544','S-1-5-21-1',$vmSid)
$readers=$writers+@($config.windows_enforced.service_sid)
$script:leakPath='';$script:leakSid='';$script:leakMask=1;$script:readPaths=[Collections.Generic.List[string]]::new()
function Get-Item($LiteralPath,[switch]$Force,$ErrorAction){
 $dir=$LiteralPath -ceq $config.windows_enforced.vm_config_root
 return [pscustomobject]@{FullName=$LiteralPath;PSIsContainer=$dir;Attributes=0;Parent=[pscustomobject]@{FullName='C:\Private'};Directory=[pscustomobject]@{FullName='C:\Private'}}
}
function Get-ChildItem($LiteralPath,[switch]$Force,$ErrorAction){return [pscustomobject]@{FullName='C:\Private\VM\vm.vmcx'}}
function Get-EnforcedPathDescriptor($Path){return $Path}
function Assert-EnforcedDescriptor($Descriptor,$Allowed,$Mask,$IgnoreInheritOnly){
 # Managed ACL provider double; exercise production SID/mask authorization.
 if($Mask -eq 2415919105){$script:readPaths.Add($Descriptor)}
 foreach($sid in @('S-1-5-18','S-1-5-32-544','S-1-5-21-1',$vmSid,$config.windows_enforced.service_sid)){
  Assert-EnforcedAllowedMask $sid 1 $Allowed $Mask
 }
 if($Descriptor -ceq $script:leakPath){Assert-EnforcedAllowedMask $script:leakSid $script:leakMask $Allowed $Mask}
}
& $script:realDataReaders $config.windows_enforced.vm_config_root $readers $writers ([Diagnostics.Stopwatch]::StartNew()) $true
& $script:realDataReaders $config.windows_enforced.disks[0].path $readers $writers ([Diagnostics.Stopwatch]::StartNew())
foreach($path in @('C:\Private','C:\Private\VM','C:\Private\VM\vm.vmcx',$config.windows_enforced.disks[0].path)){
 if($path -notin $script:readPaths){throw 'sensitive_path_not_checked'}
}
$blocked=0
foreach($path in @('C:\Private','C:\Private\VM\vm.vmcx',$config.windows_enforced.disks[0].path)){
 foreach($sid in @('S-1-1-0','S-1-5-32-545','S-1-5-83-0','S-1-5-83-2')){
  foreach($mask in @(1,2147483648,268435456)){
   $script:leakPath=$path;$script:leakSid=$sid;$script:leakMask=$mask
   try{
    & $script:realDataReaders $config.windows_enforced.vm_config_root $readers $writers ([Diagnostics.Stopwatch]::StartNew()) $true
    & $script:realDataReaders $config.windows_enforced.disks[0].path $readers $writers ([Diagnostics.Stopwatch]::StartNew())
   }catch{$blocked++}
  }
 }
}
if($blocked -ne 36){throw 'public_data_reader_accepted'}
$expired=[pscustomobject]@{Elapsed=[pscustomobject]@{TotalMilliseconds=2000}};$closed=$false
try{& $script:realDataReaders $config.windows_enforced.vm_config_root $readers $writers $expired $true}catch{$closed=$true}
if(-not $closed){throw 'late_acl_accepted'}
'private-data-bound'
''').strip(), 'private-data-bound')

    def test_constant_memory_sequences_outlive_4096_and_reject_replay(self):
        value=adapter()
        for _ in range(4100):
            self.assertTrue(value.fenced(dict(old='A',generation=3)))
        self.assertEqual(value._sequence,4101)
        self.assertNotIn('_issued',value.__dict__)
        self.assertEqual(value.process.stdin.requests[-1]['nonce'][48:],f'{4101:016x}')
        value._sequence=9007199254740991
        with self.assertRaises(TransitionBlocked):value.fenced(dict(old='A',generation=3))
        self.assertEqual(self.ps(r'''
for($i=1;$i -le 4100;$i++){
 $request.sequence=[long]$i;$request.nonce=('a'*48)+([long]$i).ToString('x16')
 Assert-EnforcedSequence $request $instance
}
if($script:enforcedSequence -ne 4100){throw 'sequence_lifetime_truncated'}
$closed=0
foreach($bad in @([long]4100,[long]4102,$true,[double]4101,[long]9007199254740992)){
 $request.sequence=$bad;$request.nonce=('a'*48)+'0000000000001005'
 try{Assert-EnforcedSequence $request $instance}catch{$closed++}
}
if($closed -ne 5){throw 'invalid_sequence_accepted'}
$request.sequence=[long]4101;$request.nonce=('a'*48)+'0000000000001004'
try{Assert-EnforcedSequence $request $instance;throw 'wrong_suffix_accepted'}catch{if($_.Exception.Message -ceq 'wrong_suffix_accepted'){throw}}
$script:enforcedSequence=[long]9007199254740990;$request.sequence=[long]9007199254740991;$request.nonce=('a'*48)+$request.sequence.ToString('x16')
Assert-EnforcedSequence $request $instance
'sequence-bound'
''').strip(),'sequence-bound')

    def test_installed_authority_accepts_old_utc_stamp_but_only_exact_fixed_installation(self):
        plan,authority,plan_hash,_=installed_fixtures()
        cfg,expiry=E._validate(plan,authority,plan_hash,plan['windows_enforced']['driver_sha256'])
        self.assertIsNone(expiry);self.assertEqual(cfg,plan['windows_enforced'])
        mutations=[('schema_version',3.0),('schema_version',True),('authority_kind','bounded'),
                   ('profile','legacy'),('action','activate-ha'),('installation_id','0'*64),
                   ('installation_id',None),('installed_at',None),('installed_at','2020-01-01T00:00:00+00:00'),
                   ('installed_at','2020-02-30T00:00:00Z'),
                   ('installed_at',(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat().replace('+00:00','Z')),
                   ('plan_sha256','0'*64),('source_commit','0'*40),('policy_sha256','0'*64)]
        for key,value in mutations:
            bad=deepcopy(authority);bad[key]=value
            with self.subTest(field=key,value=value),self.assertRaises(TransitionBlocked):E._validate(plan,bad,plan_hash,plan['windows_enforced']['driver_sha256'])
        for key in ('expires','expires_at'):
            with self.assertRaises(TransitionBlocked):E._validate(plan,authority|{key:None},plan_hash,plan['windows_enforced']['driver_sha256'])
        for installation in (None,'bad','7'*64):
            with self.assertRaises(TransitionBlocked):E._validate(plan|{'installation_id':installation},authority,plan_hash,plan['windows_enforced']['driver_sha256'])
        bounded_plan,bounded,bounded_hash,_=fixtures()
        for offset in (-1,25):
            bad=bounded|{'expires_at':(datetime.now(timezone.utc)+timedelta(hours=offset)).isoformat().replace('+00:00','Z')}
            with self.assertRaises(TransitionBlocked):E._validate(bounded_plan,bad,bounded_hash,bounded_plan['windows_enforced']['driver_sha256'])

    def test_installed_expiry_sentinel_cannot_rearm_bounded_approval_or_changed_boot_response(self):
        value=adapter();value.expires=None
        with self.assertRaises(TransitionBlocked):value.fenced(dict(old='A',generation=3))
        value=adapter();value.expires=None;value._installed_runtime=True
        self.assertTrue(value.fenced(dict(old='A',generation=3)))
        self.assertEqual(value._base()['schema_version'],2)
        value=adapter([lambda frame:frame|{'boot_ticks':'1'}]);value.expires=None;value._installed_runtime=True
        with self.assertRaises(TransitionBlocked):value.fenced(dict(old='A',generation=3))
        value=adapter();value.expires=None;value._installed_runtime=True
        with self.assertRaises(TransitionBlocked):value.fenced(dict(old='A',generation=4))

    def test_installed_authority_template_is_unissued_and_bounded_templates_remain_separate(self):
        from ha_control.prepare import templates
        names=('source_bootstrap_sha256','native_windows_code_sha256','controller_code_sha256','legacy_linux_sha256',
               'legacy_guard_code_sha256','windows_guard_sha256','enforced_definition_sha256','effects_runtime_sha256',
               'enforced_driver_sha256','enforced_adapter_sha256')
        generated=templates('f'*40,{name:'a'*64 for name in names})
        authority=json.loads(generated['enforced-installed-authority.template.json'])
        plan=json.loads(generated['enforced-plan.template.json'])
        status=json.loads(generated['readiness.json'])
        self.assertEqual(set(authority),{'schema_version','profile','action','authority_kind','installation_id','installed_at','plan_sha256','source_commit','policy_sha256'})
        self.assertEqual(authority['schema_version'],3);self.assertEqual(authority['action'],'ha-transition')
        for key in ('installation_id','installed_at','plan_sha256','policy_sha256'):self.assertIsNone(authority[key])
        self.assertIsNone(plan['installation_id']);self.assertEqual(plan['schema_version'],2)
        self.assertFalse(status['ready']);self.assertFalse(status['approval_issued']);self.assertFalse(status['execution_performed'])
        bounded=json.loads(generated['enforced-approval.template.json'])
        self.assertEqual(bounded['schema_version'],2);self.assertIn('expires_at',bounded)
        self.assertNotIn('authority_kind',bounded)

    def test_installed_native_authority_utc_age_and_schema_bounds_real_powershell(self):
        self.assertEqual(self.ps(r'''
Assert-EnforcedConfig $config
Assert-EnforcedApproval $config $approved $planHash
$original=Convert-EnforcedJson ($approved|ConvertTo-Json -Compress)
$cases=@(
 @('profile','legacy'),@('action','activate-ha'),@('authority_kind','bounded'),@('installation_id',('0'*64)),
 @('installed_at','2099-01-01T00:00:00Z'),@('installed_at','2020-01-01T00:00:00+00:00'),
 @('installed_at','2020-02-30T00:00:00Z'),@('plan_sha256',('0'*64)),@('policy_sha256',('0'*64)),@('source_commit',('0'*40)))
$blocked=0
foreach($case in $cases){
 $bad=Convert-EnforcedJson ($original|ConvertTo-Json -Compress);$bad.($case[0])=$case[1]
 try{Assert-EnforcedApproval $config $bad $planHash}catch{$blocked++}
}
foreach($key in @('expires','expires_at')){
 $bad=Convert-EnforcedJson ($original|ConvertTo-Json -Compress);$bad|Add-Member NoteProperty $key $null
 try{Assert-EnforcedApproval $config $bad $planHash}catch{$blocked++}
}
$config.installation_id='7'*64
try{Assert-EnforcedApproval $config $original $planHash}catch{$blocked++}
if($blocked -ne 13){throw 'installed_authority_mutation_accepted'}
'installed-purpose-bound'
''',installed=True).strip(),'installed-purpose-bound')

    def test_installed_native_authority_removal_replacement_and_post_read_change_stay_blocked(self):
        setup=r'''
function Get-EnforcedVmSnapshot($Config,$Policy,$Watch){return @{fenced=$true;context=@{principal_sha256=('1'*64);boot_ticks=$Config.windows_enforced.boot_ticks}}}
$script:originalPrivate=(Get-Command Read-EnforcedPrivate).ScriptBlock
$script:approvalMode='';$script:approvalReads=0
function Read-EnforcedPrivate($Path,$Watch){
 if($Path -ceq $script:Approval){
  $script:approvalReads++
  if($script:approvalMode -ceq 'remove'){throw 'fixture_authority_removed'}
 }
 $value=& $script:originalPrivate $Path $Watch
 if($Path -ceq $script:Approval -and ($script:approvalMode -ceq 'replace' -or ($script:approvalMode -ceq 'post-change' -and $script:approvalReads -ge 2))){$value.sha256='0'*64}
 return $value
}
'''
        self.assertEqual(self.ps(r'''
$response=Get-EnforcedResponse $config $approved $planHash $approvalHash $instance $request ([Diagnostics.Stopwatch]::StartNew())
if($response.schema_version -ne 2 -or $response.boot_ticks -cne $config.windows_enforced.boot_ticks){throw 'protocol_schema_changed'}
$blocked=0;$sequence=1
foreach($mode in @('remove','replace','post-change')){
 $script:approvalMode=$mode;$script:approvalReads=0;$sequence++
 $request.sequence=[long]$sequence;$request.nonce=New-EnforcedNonce $sequence
 try{Get-EnforcedResponse $config $approved $planHash $approvalHash $instance $request ([Diagnostics.Stopwatch]::StartNew())|Out-Null}catch{$blocked++}
}
if($blocked -ne 3 -or $script:trace.Count -ne 0){throw 'changed_authority_accepted_or_mutated'}
'installed-files-rechecked'
''',setup,installed=True).strip(),'installed-files-rechecked')

    def test_plan_and_approval_separate_profile_exact_bindings(self):
        plan, approval, plan_hash, _ = fixtures()
        E._validate(plan, approval, plan_hash, plan['windows_enforced']['driver_sha256'])
        for mutate in (
            lambda p: p.update(schema_version=True), lambda p: p.update(profile='legacy'),
            lambda p: p.update(generation=True), lambda p: p['windows_enforced'].update(extra=True),
            lambda p: p['windows_enforced'].update(service_sid='S-1-5-21-1'),
            lambda p: p['windows_enforced'].update(policy_path=r'\\remote\policy'),
            lambda p: p['windows_enforced']['disks'].append(deepcopy(p['windows_enforced']['disks'][0])),
        ):
            bad = deepcopy(plan); mutate(bad)
            with self.assertRaises(TransitionBlocked):
                E._validate(bad, approval, plan_hash, plan['windows_enforced']['driver_sha256'])
        for field, value in [('schema_version', 1), ('policy_sha256', '0'*64), ('action', 'fence'),
                             ('expires_at', '2000-01-01T00:00:00Z'), ('plan_sha256', '0'*64)]:
            bad = deepcopy(approval); bad[field] = value
            with self.assertRaises(TransitionBlocked):
                E._validate(plan, bad, plan_hash, plan['windows_enforced']['driver_sha256'])
        with self.assertRaises(TransitionBlocked):
            E._decode(b'{"schema_version":2,"schema_version":1}')
        with self.assertRaises(TransitionBlocked):
            E.EnforcedWindowsFence('/unused-plan', '/unused-approval')

    def test_fresh_protocol_and_legacy_replay_type_binding_rejection(self):
        state = dict(old='A', generation=3)
        self.assertTrue(adapter().fenced(state))
        mutations = [lambda v: {'legacy_guard': dict(complete=True, verified=True)},
                     lambda v: v | {'profile': 'legacy'}, lambda v: v | {'generation': True},
                     lambda v: v | {'nonce': '0'*64}, lambda v: v | {'source_sha256': '0'*64},
                     lambda v: v | {'broker_instance': '0'*64}, lambda v: v | {'fenced': 1},
                     lambda v: v | {'cached': True}, lambda v: v | {'schema_version': 2.0},
                     lambda v: v | {'sequence': True}, lambda v: v | {'sequence': 2.0},
                     lambda v: v | {'sequence': 0}, lambda v: v | {'sequence': 9007199254740992}]
        for mutate in mutations:
            with self.assertRaises(TransitionBlocked):
                adapter([mutate]).fenced(state)
        with self.assertRaises(TransitionBlocked):
            adapter().fenced(dict(old='B', generation=3))
        boolean_generation = adapter([lambda v: v | {'generation': True}])
        boolean_generation.plan_data['generation'] = 1
        with self.assertRaises(TransitionBlocked):
            boolean_generation.fenced(dict(old='A', generation=1))

    def test_lost_fence_reconciles_only_with_new_observe(self):
        value = adapter([lambda v: v | {'profile': 'legacy'}])
        value.fence(dict(old='A', generation=3))
        requests = value.process.stdin.requests
        self.assertEqual([r['action'] for r in requests], ['fence', 'observe'])
        self.assertNotEqual(requests[0]['nonce'], requests[1]['nonce'])

    def test_guard_handshake_exact_new_profile_and_owned_child_cleanup(self):
        for legacy in (False, True):
            value = adapter()
            value.plan, value.approval, value.driver = Path('/fixture/plan'), Path('/fixture/approval'), DRIVER
            value.approval_data = dict(action='ha-transition')
            hello = value._base() | dict(guard='ready', broker_instance='8'*64, sequence=1)
            if legacy:
                hello = dict(guard='ready', legacy_guard_schema=1)
            class Child:
                def __init__(self):
                    self.stdin = io.StringIO()
                    self.stdout = io.StringIO(json.dumps(hello)+'\n')
                    self.code = None
                def poll(self):
                    return self.code
                def wait(self, timeout):
                    self.code = 0
                def kill(self):
                    self.code = -1
            child = Child()
            with patch.object(E, '_powershell_path', return_value=Path('/fixed/powershell')), patch.object(E.subprocess, 'Popen', return_value=child) as spawn:
                if legacy:
                    with self.assertRaises(TransitionBlocked):
                        with value.guard():
                            self.fail('legacy hello accepted')
                else:
                    with value.guard():
                        self.assertTrue(value._healthy)
                self.assertIsNone(value.process)
                self.assertFalse(value._healthy)
                self.assertNotIn('shell', spawn.call_args.kwargs)

    def test_pipe_write_failure_cannot_authorize_or_repeat_mutation(self):
        value = adapter()
        value.process.stdin.write = lambda _: (_ for _ in ()).throw(OSError('private'))
        with self.assertRaises(TransitionBlocked):
            value.fenced(dict(old='A', generation=3))
        self.assertFalse(value._healthy)

    def test_real_powershell_ast_and_native_without_service_are_closed(self):
        self.assertEqual(self.ps(r'''
$t=$null;$e=$null;$ast=[Management.Automation.Language.Parser]::ParseFile($env:ENFORCED_DRIVER,[ref]$t,[ref]$e)
if($e.Count){throw 'parse_failed'}
$commands=@($ast.FindAll({param($a) $a -is [Management.Automation.Language.CommandAst]},$true)|ForEach-Object {$_.GetCommandName()})
foreach($forbidden in @('Start-VM','New-VM','Set-VM','Restart-Computer','Set-Acl','Get-LegacyWriterProof','Start-Service','New-Service')){if($forbidden -in $commands){throw 'forbidden_command'}}
if(@($commands|Where-Object {$_ -ceq 'Stop-VM'}).Count -ne 1){throw 'mutation_surface_changed'}
Initialize-EnforcedNative
if($null -eq ('EnforcedVmNativeV1' -as [type])){throw 'native_helper_did_not_compile'}
$collision=$false;try{Initialize-EnforcedNative}catch{$collision=$true}
if(-not $collision){throw 'native_helper_type_collision_accepted'}
function Get-EnforcedCurrentSid{return 'S-1-5-21-1'}
$blocked=$false;try{Get-EnforcedContext $config $policy ([Diagnostics.Stopwatch]::StartNew())|Out-Null}catch{$blocked=$true}
if(-not $blocked){throw 'interactive_context_accepted'}
'closed'
''').strip(), 'closed')

    def test_observe_fence_replay_and_expired_request_real_driver_core(self):
        setup = r'''
function Get-EnforcedVmSnapshot($Config,$Policy,$Watch){Assert-EnforcedDeadline $Watch;$script:trace.Add('snapshot');return @{fenced=$script:powerOff;context=@{principal_sha256=(Get-EnforcedHash ([Text.Encoding]::UTF8.GetBytes($Config.windows_enforced.service_sid)));boot_ticks=$Config.windows_enforced.boot_ticks}}}
'''
        self.assertEqual(self.ps(r'''
$response=Get-EnforcedResponse $config $approved $planHash $approvalHash $instance $request ([Diagnostics.Stopwatch]::StartNew())
if($response.profile -cne 'enforced-vm-v1' -or $response.scope -cne 'vm-enforcement-only' -or $response.fenced){throw 'wrong_profile_response'}
$blocked=$false;try{Get-EnforcedResponse $config $approved $planHash $approvalHash $instance $request ([Diagnostics.Stopwatch]::StartNew())|Out-Null}catch{$blocked=$true}
if(-not $blocked){throw 'replayed_nonce_accepted'}
$request.sequence=[long]2;$request.nonce=('6'*48)+'0000000000000002';$request.action='fence'
$response=Get-EnforcedResponse $config $approved $planHash $approvalHash $instance $request ([Diagnostics.Stopwatch]::StartNew())
if(-not $response.fenced -or ($script:trace -join ',') -cne 'snapshot,snapshot,turnoff,snapshot'){throw 'fence_order_changed'}
$request.sequence=[long]3;$request.nonce=('5'*48)+'0000000000000003'
$expired=[pscustomobject]@{Elapsed=[pscustomobject]@{TotalMilliseconds=2000}}
$blocked=$false;try{Get-EnforcedResponse $config $approved $planHash $approvalHash $instance $request $expired|Out-Null}catch{$blocked=$true}
if(-not $blocked -or @($script:trace|Where-Object {$_ -ceq 'turnoff'}).Count -ne 1){throw 'late_mutation'}
'bounded-profile'
''', setup).strip(), 'bounded-profile')

    def test_managed_profile_scope_authority_and_witness_are_explicit(self):
        selected=dict(name=E.CONFIG_PROFILE_DEFINITION['name'],definition_sha256=E.CONFIG_PROFILE_DEFINITION_SHA256,
            capability_name='vmWorkerProcess',capability_sid=E.CONFIG_PROFILE_DEFINITION['capability_sid'],
            host_hypervisor_tcb_acknowledged=True,acceptance_scope='isolated-predeployment',
            operational_cutover_accepted=False,io_acceptance_sha256='',external_capability_review_sha256='',isolation_receipt_sha256='')
        self.assertEqual(E.config_profile(selected),selected)
        plan,approved,plan_hash,_=fixtures();plan['windows_enforced']['config_profile']=deepcopy(selected)
        with self.assertRaises(TransitionBlocked):E._validate(plan,approved,plan_hash,plan['windows_enforced']['driver_sha256'])
        selected.update(io_acceptance_sha256='1'*64,isolation_receipt_sha256='2'*64)
        plan['windows_enforced']['config_profile']=deepcopy(selected)
        E._validate(plan,approved,plan_hash,plan['windows_enforced']['driver_sha256'])
        bridge=E.EnforcedWindowsFence.__new__(E.EnforcedWindowsFence);bridge.cfg=plan['windows_enforced'];bridge.plan_sha256=plan_hash;bridge.approval_sha256='3'*64
        witness=bridge._base()
        self.assertEqual(witness['profile'],'enforced-vm-v1')
        self.assertEqual(witness['config_profile_sha256'],E.config_profile_digest(selected))
        self.assertEqual(witness['config_profile_definition_sha256'],E.CONFIG_PROFILE_DEFINITION_SHA256)
        production=deepcopy(selected);production.update(acceptance_scope='production',isolation_receipt_sha256='')
        plan['windows_enforced']['config_profile']=production
        with self.assertRaises(TransitionBlocked):E._validate(plan,approved,plan_hash,plan['windows_enforced']['driver_sha256'])
        production.update(operational_cutover_accepted=True,external_capability_review_sha256='4'*64)
        E._validate(plan,approved,plan_hash,plan['windows_enforced']['driver_sha256'])
        self.assertNotEqual(E.config_profile_digest(selected),E.config_profile_digest(production))
        for key,value in [('name','unknown'),('definition_sha256','0'*64),('capability_sid','S-1-15-3-1'),('host_hypervisor_tcb_acknowledged',1),('acceptance_scope',[]),('io_acceptance_sha256',True),('operational_cutover_accepted','false')]:
            bad=deepcopy(selected);bad[key]=value
            with self.assertRaises(TransitionBlocked):E.config_profile(bad)
        wrong=deepcopy(selected);wrong['operational_cutover_accepted']=True
        with self.assertRaises(TransitionBlocked):E.config_profile(wrong)
        production['isolation_receipt_sha256']=selected['isolation_receipt_sha256']
        with self.assertRaises(TransitionBlocked):E.config_profile(production)

    def test_managed_config_ps_hash_roles_and_pending_authority_boundary(self):
        profile=dict(name=E.CONFIG_PROFILE_DEFINITION['name'],definition_sha256=E.CONFIG_PROFILE_DEFINITION_SHA256,
            capability_name='vmWorkerProcess',capability_sid=E.CONFIG_PROFILE_DEFINITION['capability_sid'],
            host_hypervisor_tcb_acknowledged=True,acceptance_scope='isolated-predeployment',
            operational_cutover_accepted=False,io_acceptance_sha256='',external_capability_review_sha256='',isolation_receipt_sha256='')
        raw=base64.b64encode(json.dumps(profile).encode()).decode()
        result=self.ps("$p=Convert-EnforcedJson ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"+raw+"')))\n"+r'''
$null=Assert-EnforcedConfigProfile $p
$config.windows_enforced|Add-Member config_profile $p
Assert-EnforcedConfig $config
$closed=$false;try{Assert-EnforcedApproval $config $approved $planHash}catch{if($_.Exception.Message -cne 'managed_config_authority_blocked'){throw};$closed=$true};if(-not $closed){throw 'pending_io_opened_authority'}
$readers=@('S-1-5-18','S-1-5-32-544',$p.capability_sid,$config.windows_enforced.service_sid)
Assert-EnforcedManagedAce $p.capability_sid 2032127 $readers $p.capability_sid $config.windows_enforced.service_sid
Assert-EnforcedManagedAce $config.windows_enforced.service_sid 1179785 $readers $p.capability_sid $config.windows_enforced.service_sid
foreach($case in @(@('S-1-15-3-1',2032127),@('S-1-5-83-0',1180063),@('S-1-5-83-1-2-3-4-5',2032127),@($p.capability_sid,268435456),@($config.windows_enforced.service_sid,1180063))){$closed=$false;try{Assert-EnforcedManagedAce $case[0] $case[1] $readers $p.capability_sid $config.windows_enforced.service_sid}catch{$closed=$true};if(-not $closed){throw 'config_tcb_role_expanded'}}
Get-EnforcedConfigProfileHash $p
''')
        self.assertEqual(result.strip(),E.config_profile_digest(profile))

    def test_vm_vhd_and_restart_policy_real_snapshot_body(self):
        setup = r'''
function Get-EnforcedContext($Config,$Policy,$Watch){return @{principal_sha256=('1'*64);boot_ticks=$Config.windows_enforced.boot_ticks}}
function Assert-EnforcedPath($Path,$Writers){}
function Assert-EnforcedDataReaders($Path,$Readers,$Writers,$Watch,$Tree){
 if('S-1-5-83-0' -in $Readers -or 'S-1-5-83-1' -notin $Readers -or $config.windows_enforced.service_sid -notin $Readers){throw 'invalid_data_readers'}
 $script:trace.Add($Path)
}
function Get-EnforcedVmSid($Id){return 'S-1-5-83-1'}
function Get-EnforcedFileIdentity($Path){return 'fixture-file-id'}
$script:vm=[pscustomobject]@{Id=[Guid]$config.windows_enforced.vm_id;Generation=2;AutomaticStartAction='Nothing';ConfigurationLocation=$config.windows_enforced.vm_config_root;Notes='fixture-notes';State='Off'}
$script:vhd=[pscustomobject]@{DiskIdentifier=[Guid]$config.windows_enforced.disks[0].disk_id;ParentPath=''}
function Get-VM($Id,$ErrorAction){if($Id.ToString() -cne $config.windows_enforced.vm_id){throw 'wrong_vm'};return $script:vm}
function Get-VMHardDiskDrive($VM,$ErrorAction){return [pscustomobject]@{Path=$config.windows_enforced.disks[0].path}}
function Get-VHD($Path,$ErrorAction){if($script:race){$script:vm.Notes='changed-during-read'};return $script:vhd}
$script:race=$false
'''
        self.assertEqual(self.ps(r'''
$snapshot=Get-EnforcedVmSnapshot $config $policy ([Diagnostics.Stopwatch]::StartNew())
if(-not $snapshot.fenced){throw 'off_vm_lost'}
if($script:trace.Count -ne 2 -or $config.windows_enforced.vm_config_root -notin $script:trace -or $config.windows_enforced.disks[0].path -notin $script:trace){throw 'missing_data_acl_checks'}
$originalId=$config.windows_enforced.disks[0].disk_id
foreach($native in @('7B0A3FEB-04CF-5C40-85BD-CFD50B85AB9D','F3602945-C7E3-8846-9FC2-5011BDDDFA1D')){
 $config.windows_enforced.disks[0].disk_id=$native.ToLowerInvariant();$script:vhd.DiskIdentifier=$native
 $snapshot=Get-EnforcedVmSnapshot $config $policy ([Diagnostics.Stopwatch]::StartNew())
 if(-not $snapshot.fenced -or (ConvertTo-EnforcedDiskIdentifier $native) -cne (ConvertTo-EnforcedDiskIdentifier ([Guid]$native))){throw 'native_uppercase_identity_failed'}
}
$config.windows_enforced.disks[0].disk_id=$originalId
foreach($bad in @($null,$true,7,[Guid]::Empty,'00000000-0000-0000-0000-000000000000','{7B0A3FEB-04CF-5C40-85BD-CFD50B85AB9D}','7B0A3FEB04CF5C4085BDCFD50B85AB9D','not-a-guid')){
 $script:vhd.DiskIdentifier=$bad;$closed=$false
 try{$null=Get-EnforcedVmSnapshot $config $policy ([Diagnostics.Stopwatch]::StartNew())}catch{$closed=$true}
 if(-not $closed){throw 'malformed_native_identifier_accepted'}
}
$script:vhd.DiskIdentifier=[Guid]$originalId
$blocked=0
$script:vm.AutomaticStartAction='Start'
try{Get-EnforcedVmSnapshot $config $policy ([Diagnostics.Stopwatch]::StartNew())|Out-Null}catch{$blocked++}
$script:vm.AutomaticStartAction='Nothing';$script:vm.Notes='changed'
try{Get-EnforcedVmSnapshot $config $policy ([Diagnostics.Stopwatch]::StartNew())|Out-Null}catch{$blocked++}
$script:vm.Notes='fixture-notes';$script:vhd.ParentPath='checkpoint.avhdx'
try{Get-EnforcedVmSnapshot $config $policy ([Diagnostics.Stopwatch]::StartNew())|Out-Null}catch{$blocked++}
$script:vhd.ParentPath='';$script:vhd.DiskIdentifier=[Guid]::Empty
try{Get-EnforcedVmSnapshot $config $policy ([Diagnostics.Stopwatch]::StartNew())|Out-Null}catch{$blocked++}
$script:vhd.DiskIdentifier=[Guid]$config.windows_enforced.disks[0].disk_id;$script:race=$true
try{Get-EnforcedVmSnapshot $config $policy ([Diagnostics.Stopwatch]::StartNew())|Out-Null}catch{$blocked++}
if($blocked -ne 5){throw 'changed_vm_accepted'}
'vm-bound'
''', setup).strip(), 'vm-bound')

    def test_exact_launcher_and_post_read_authority_change_block(self):
        setup = r'''
function Get-EnforcedVmSnapshot($Config,$Policy,$Watch){return @{fenced=$true;context=@{principal_sha256=('1'*64);boot_ticks=$Config.windows_enforced.boot_ticks}}}
$script:originalPrivate=(Get-Command Read-EnforcedPrivate).ScriptBlock;$script:reads=0
function Read-EnforcedPrivate($Path,$Watch){$script:reads++;$value=& $script:originalPrivate $Path $Watch;if($script:reads -eq 4){$value.sha256='0'*64};return $value}
'''
        self.assertEqual(self.ps(r'''
$argv=@($config.windows_enforced.python_path,'-I','-B',$config.windows_enforced.launcher_path,'serve','--plan',$script:Plan,'--approval',$script:Approval,'--journal','C:\Private\journal\controller-state.json')
Assert-EnforcedParentArgv $argv $config
$blocked=0
$bad=$argv.Clone();$bad[3]='C:\Other\launcher.py'
try{Assert-EnforcedParentArgv $bad $config}catch{$blocked++}
$bad=$argv+@('extra')
try{Assert-EnforcedParentArgv $bad $config}catch{$blocked++}
try{Get-EnforcedResponse $config $approved $planHash $approvalHash $instance $request ([Diagnostics.Stopwatch]::StartNew())|Out-Null}catch{$blocked++}
if($blocked -ne 3){throw 'authority_or_parent_change_accepted'}
'changed-authority-blocked'
''', setup).strip(), 'changed-authority-blocked')

    def test_observe_consumption_deadline_and_ambiguous_fence_no_repeat(self):
        value = adapter()
        with patch.object(E.time, 'monotonic', side_effect=[0, 0, 0, 2.01]):
            with self.assertRaises(TransitionBlocked):
                value.fenced(dict(old='A', generation=3))
        value = adapter([lambda v: v | {'profile': 'legacy'}, lambda v: v | {'profile': 'legacy'}])
        with self.assertRaises(TransitionBlocked):
            value.fence(dict(old='A', generation=3))
        self.assertEqual([r['action'] for r in value.process.stdin.requests], ['fence', 'observe'])


if __name__ == '__main__':
    unittest.main()
