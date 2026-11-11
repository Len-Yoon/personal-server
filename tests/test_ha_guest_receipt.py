"""Portable COM parser/binding/ACL-policy tests. No Windows native calls."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]
SOURCE=Path(os.environ.get('HA_GUEST_RECEIPT_SOURCE',ROOT/'infra/k8s/ha_control/windows-guest-receipt.ps1'))
PWSH=shutil.which('pwsh') or str(ROOT/'build/ha-predeploy-evidence/windows-build/powershell-7.5.4/pwsh')


def request():
    return {'schema_version':1,'collector_source_commit':'d'*40,'vm_source_commit':'a'*40,'stage_source_commit':'b'*40,
        'seed_source_commit':'c'*40,'installation_id':'1'*64,'node':'A','vm_id':'11111111-1111-1111-1111-111111111111',
        'prepared_receipt_sha256':'2'*64,'seed_id':'3'*64,'bootstrap_manifest_sha256':'4'*64,
        'expected_kernel_release':'6.8.0-149-generic','expected_pg_image':'sha256:'+'5'*64,'expected_package_count':73,
        'timeout_seconds':120,'egress_mac_address':'','peer_prepared_receipt_sha256':'','switch_owner_manifest_sha256':'8'*64}


def payload():
    cfg=request();raw=b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20'+b'x'*32
    return {'source_commit':cfg['seed_source_commit'],'installation_id':cfg['installation_id'],'node':cfg['node'],
        'seed_id':cfg['seed_id'],'bootstrap_manifest_sha256':cfg['bootstrap_manifest_sha256'],
        'expected_kernel_release':cfg['expected_kernel_release'],'status':'PASS',
        'scope':'new_guest_os_engine_and_pg_image_not_started','cloud_init_status_exit':0,'kernel_boot_verified':True,
        'guest_reboot_requests':1,'new_boot_id_observed':True,'boot_id_sha256':'7'*64,'automatic_os_updates_disabled':True,
        'package_count':73,'pg_image':cfg['expected_pg_image'],'pg_state':'installed-not-started','pg_started':False,
        'writer_authority_issued':False,'provider_credentials_supplied_by_seed':False,'effects_transition_performed':False,
        'production_deployed':False,'server_public_key':'ssh-ed25519 '+base64.b64encode(raw).decode(),
        'server_key_fingerprint':'SHA256:'+base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip('='),
        'serial_delivery_is_not_host_receipt_verification':True}


def host_profile():
    cfg=request()
    result={key:cfg[key] for key in ('collector_source_commit','vm_source_commit','stage_source_commit','seed_source_commit',
        'installation_id','node','vm_id','prepared_receipt_sha256','seed_id')}
    result.update(schema_version=1,profile_name='registered-host-compute-v1',evidence_sha256='e'*64,
        evidence_source_sha256='cea85b2ae3cf57ef1891faf08f8cde5d2daa25fbfd8e9d86e90ae33c7ced1ab8',expected_service='vmcompute',
        expected_pid=7372,expected_start_ticks='639298297721183929',expected_file_identity='966cbda0:000b00000004ce2a',
        expected_image_sha256='a'*64,expected_signer_certificate_sha256='b'*64,expected_service_sddl_sha256='c'*64,
        expected_file_sddl_sha256='d'*64,expected_pipe_sddl_sha256='f'*64,capability_name='vmWorkerProcess',
        capability_sid_sha256='59217592fcdabf0b9b76ab2987b0dce343b526fb9639c8e749d269f4e99797ce',
        trust_model='HostHypervisorTCB',scope='isolated_predeployment_guest_enrollment',isolated_predeployment=True,
        writer_authority_issued=False,production_transition=False)
    return result


def prepared():
    cfg=request();root=r'C:\ProgramData\N100HA_'+cfg['installation_id'][:24];vm='N100HA_'+cfg['installation_id'][:16]+'_A';guest=root+r'\guest-A'
    pins={'installation_id':cfg['installation_id'],'source_commit':cfg['vm_source_commit'],'stage_source_commit':cfg['stage_source_commit'],
        'seed_source_commit':cfg['seed_source_commit'],'node':'A','manifest_sha256':'8'*64,'stage_manifest_sha256':'9'*64,
        'vm_id':cfg['vm_id'],'vm_name':vm,'switch_id':'22222222-2222-2222-2222-222222222222',
        'switch_name':'N100HA_'+cfg['installation_id'][:16]+'_data','config_path':guest,'disk_path':guest+r'\guest-A.vhdx',
        'disk_sha256':'a'*64,'disk_identity':'12345678:0000000000000001','disk_identifier':'33333333-3333-3333-3333-333333333333',
        'seed_path':root+r'\seed-A.iso','seed_sha256':'b'*64,'seed_identity':'12345678:0000000000000002',
        'mac_address':'021122334455','com1_pipe':chr(92)*2+'.'+chr(92)+'pipe'+chr(92)+vm+'_com1','optional_egress_nic':False,
        'egress_mac_address':'','peer_prepared_receipt_sha256':'','switch_owner_manifest_sha256':'8'*64}
    return root,{'schema_version':1,'prepared':True,'pins':pins}


class GuestReceiptTests(unittest.TestCase):
    def run_ps(self,body,*,native_compile=False,**extra):
        cfg=json.dumps(request(),separators=(',',':')).encode()
        environment=dict(os.environ,HA_GUEST_RECEIPT_SOURCE=str(SOURCE),REQUEST=base64.b64encode(cfg).decode(),
                         REQUEST_SHA256=hashlib.sha256(cfg).hexdigest(),PAYLOAD=json.dumps(payload(),separators=(',',':')),PREPARED=json.dumps(prepared()[1],separators=(',',':')),FIXTURE_ROOT=prepared()[0],HOST_PROFILE=json.dumps(host_profile(),separators=(',',':')),**extra)
        script="$ErrorActionPreference='Stop'\n. $env:HA_GUEST_RECEIPT_SOURCE\n"
        script+='Initialize-GuestComTypes'+('' if native_compile else ' -PortableFixture')+'\n'
        script+="$Request=$env:REQUEST;$RequestSha256=$env:REQUEST_SHA256\n"+body
        result=subprocess.run([PWSH,'-NoProfile','-NonInteractive','-Command',script],env=environment,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        return result.stdout.decode().strip()

    def test_fixture_payload_matches_frozen_guest_seed_contract(self):
        source=Path(os.environ.get('HA_GUEST_SEED_SOURCE',ROOT/'infra/k8s/ha-package/guest_seed.py'))
        scope={'__file__':str(source),'__name__':'guest_seed_model_fixture'}
        exec(compile(source.read_bytes(),str(source),'exec'),scope)
        line=scope['serial_payload'](payload())
        self.assertTrue(line.startswith(b'N100_HA_SEED_RECEIPT '))
        self.assertLessEqual(len(line),4096)

    def test_ast_scope_unarmed_gate_and_native_source_only_compilation(self):
        text=self.run_ps(r'''
$tokens=$null;$errors=$null;$ast=[Management.Automation.Language.Parser]::ParseFile($env:HA_GUEST_RECEIPT_SOURCE,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'parse_failed'}
$commands=@($ast.FindAll({param($a) $a -is [Management.Automation.Language.CommandAst]},$true)|ForEach-Object{$_.GetCommandName()})
foreach($forbidden in @('Start-VM','Stop-VM','Set-VM','Set-VMComPort','Set-Acl','New-VMSwitch','Set-NetIPAddress','New-NetNat','Start-Service','Restart-Computer','Invoke-Expression')){if($forbidden -in $commands){throw 'native_scope_expanded'}}
$blocked=$false;try{Invoke-GuestComReceipt}catch{$blocked=$_.Exception.Message -ceq 'guest_com_native_intent_required'}
if(-not $blocked){throw 'unarmed_native_accepted'}
$readyAssignment=$ast.FindAll({param($a)$a -is [Management.Automation.Language.AssignmentStatementAst] -and $a.Left -is [Management.Automation.Language.VariableExpressionAst] -and $a.Left.VariablePath.UserPath -ceq 'ready'},$true)
if($readyAssignment.Count -ne 1){throw 'ready_frame_ambiguous'}
$hash=$readyAssignment[0].Right.Find({param($a)$a -is [Management.Automation.Language.HashtableAst]},$true)
$actual=@($hash.KeyValuePairs|ForEach-Object{$_.Item1.SafeGetValue()}|Sort-Object -CaseSensitive)
$expected=@('format','schema_version','source_commit','request_sha256','prepared_receipt_sha256','installation_id','node','vm_id','com1_pipe','reader_pid','reader_start_ticks','reader_image_sha256','pipe_bound')|Sort-Object -CaseSensitive
if(($actual-join ',') -cne ($expected-join ',')){throw 'ready13_consumer_contract_changed'}
'portable-ast-and-unarmed-only'
''')
        self.assertEqual(text,'portable-ast-and-unarmed-only')
        self.assertIn('bool writer=HcsPure.ControlMutator(mask,allow,file);',SOURCE.read_text())
        self.assertEqual(self.run_ps("'full-source-compiled-no-native-call'",native_compile=True),'full-source-compiled-no-native-call')

    def test_prepared_snapshot_identity_source_media_pipe_and_owner_are_strict(self):
        self.assertEqual(self.run_ps(r'''
$cfg=Read-GuestComRequest;$lease=[pscustomobject]@{Bytes=[Text.Encoding]::UTF8.GetBytes($env:PREPARED)}
$pins=Read-GuestComPrepared $lease $cfg $env:FIXTURE_ROOT
if($pins['source_commit'] -cne $cfg['vm_source_commit'] -or $pins['seed_source_commit'] -cne $cfg['seed_source_commit']){throw 'component_pin_misbound'}
foreach($changed in @($env:PREPARED.Replace(('a'*40),('f'*40)),$env:PREPARED.Replace('_A_com1','_B_com1'),$env:PREPARED.Replace('guest-A.vhdx','foreign.vhdx'),$env:PREPARED.Replace('"optional_egress_nic":false','"optional_egress_nic":"false"'))){
 $blocked=$false;try{Read-GuestComPrepared ([pscustomobject]@{Bytes=[Text.Encoding]::UTF8.GetBytes($changed)}) $cfg $env:FIXTURE_ROOT|Out-Null}catch{$blocked=$true};if(-not $blocked){throw 'prepared_snapshot_forgery_accepted'}
}
'prepared-contract-only'
'''),'prepared-contract-only')

    def test_current_vm_com_media_switch_and_disconnected_nic_are_checked_without_native_calls(self):
        self.assertEqual(self.run_ps(r'''
$cfg=Read-GuestComRequest;$lease=[pscustomobject]@{Bytes=[Text.Encoding]::UTF8.GetBytes($env:PREPARED)}
$script:pins=Read-GuestComPrepared $lease $cfg $env:FIXTURE_ROOT;$script:tamper=''
function Get-VM{[CmdletBinding()]param($Id) return [pscustomobject]@{Id=[Guid]$cfg['vm_id'];Generation=2;Name=$script:pins['vm_name'];Notes=('ha-owned-guest-v1:'+$cfg['installation_id']+':'+$script:pins['manifest_sha256']);Path=$script:pins['config_path'];State='Running'}}
function Get-VMComPort{param($VM) $path=$script:pins['com1_pipe'];if($script:tamper -ceq 'wrong-com'){$path='wrong'};$items=@([pscustomobject]@{Name='COM 1';VMId=[Guid]$cfg['vm_id'];Path=$path},[pscustomobject]@{Name='COM 2';VMId=[Guid]$cfg['vm_id'];Path='' });switch -CaseSensitive ($script:tamper){'extra-com'{$items[1].Path='not-empty'} 'missing-com'{$items=@($items[0])} 'duplicate-com'{$items[1].Name='COM 1'} 'unknown-com'{$items[1].Name='COM 3'} 'foreign-com'{$items[1].VMId=[Guid]'44444444-4444-4444-4444-444444444444'}};return $items}
function Get-VMSwitch{param($Id) $note='ha-owned-guest-v1:'+$cfg['installation_id']+':'+$script:pins['switch_owner_manifest_sha256'];if($script:tamper -ceq 'wrong-switch-owner'){$note='foreign'};return [pscustomobject]@{Id=[Guid]$script:pins['switch_id'];Name=$script:pins['switch_name'];SwitchType='Internal';Notes=$note}}
function Get-VMNetworkAdapter{param($VM) $items=@([pscustomobject]@{Name='ha-data';MacAddress=$script:pins['mac_address'];DynamicMacAddressEnabled=$false;MacAddressSpoofing='Off';SwitchId=$script:pins['switch_id']});if($script:pins['optional_egress_nic']){$items+=([pscustomobject]@{Name='egress-disconnected';MacAddress=$script:pins['egress_mac_address'];DynamicMacAddressEnabled=$false;MacAddressSpoofing='Off';SwitchName='Default Switch';SwitchId=[Guid]'44444444-4444-4444-4444-444444444444'})};return $items}
function Get-VMHardDiskDrive{param($VM) $path=$script:pins['disk_path'];if($script:tamper -ceq 'wrong-media'){$path='foreign'};return [pscustomobject]@{Path=$path}}
function Get-VMDvdDrive{param($VM) return [pscustomobject]@{Path=$script:pins['seed_path']}}
function Get-CimInstance{param($Namespace,$ClassName,$Filter,$OperationTimeoutSec) return [pscustomobject]@{Name=$cfg['vm_id'];EnabledState=2;ProcessID=100}}
$state=Get-GuestComVm $cfg $script:pins;if(-not $state.Running -or $state.Pid -ne 100){throw 'valid_vm_snapshot_rejected'}
foreach($case in @('wrong-com','extra-com','missing-com','duplicate-com','unknown-com','foreign-com','wrong-switch-owner','wrong-media','connected-egress')){
 $script:tamper=$case;if($case -ceq 'connected-egress'){$script:pins['optional_egress_nic']=$true;$script:pins['egress_mac_address']='0266778899AA'}
 $blocked=$false;try{Get-GuestComVm $cfg $script:pins|Out-Null}catch{$blocked=$true};if(-not $blocked){throw 'vm_snapshot_drift_accepted'}
}
'fake-provider-contract-only'
'''),'fake-provider-contract-only')

    def test_ready_record_is_written_before_connect_and_is_not_pipe_trust(self):
        source=SOURCE.read_text()
        invoke=source[source.index('function Invoke-GuestComReceipt{'):]
        self.assertLess(invoke.index('$readyHash=New-GuestComReaderReady'),invoke.index('[HaGuestComV1.Reader]::TryConnect'))
        self.assertIn("$initial.State -cne 'Off'",invoke)
        self.assertIn('[IO.FileMode]::CreateNew',source)
        self.assertIn('$file.Flush($true)',source)
        self.assertIn('pipe_bound=$false',source)
        self.assertIn('WindowsPowerShell\\v1.0\\powershell.exe',source)
        self.assertIn('Assert-GuestComReaderBoundary $reader $process $hostLease $caller $vmSid',invoke)
        self.assertIn('$Reader.Verify($Process.Pid,$Caller,$VmSid)',source)
        self.assertIn("[Security.Principal.NTAccount]::new('NT VIRTUAL MACHINE\\'+$spec['vm_id'])",invoke)
        self.assertIn('$process.AssertAlive()',invoke)
        self.assertIn('$file.AssertUnchanged()',invoke)
        self.assertIn("if($null -ne $hostLease){$answer['schema_version']=2",invoke)
        self.assertIn("$answer['scope']='owned_guest_registered_host_compute_public_key_receipt'",invoke)
        # The old result keyset stays exact; explicit v2 adds its own boundary.
        import re
        base=re.search(r"\$answer=\[ordered\]@\{status='PASS';(.*?)\}\n",source,re.S).group(1)
        expected=['scope','collector_source_commit','reader_ready_sha256','vm_source_commit','stage_source_commit','seed_source_commit','installation_id','node','vm_id','prepared_receipt_sha256','vmwp_pid','vmwp_start_ticks','connections','bytes_read','lines_seen','native_host_boundary_verified','pipe_server_pid_verified','pipe_acl_verified','current_vm_com_path_verified','guest_receipt','windows_fence_verified','writer_authority_issued','production_transition']
        self.assertEqual(re.findall(r'(?:^|;)([a-z_0-9]+)=',base),expected)


    def test_registered_profile_request_and_external_evidence_pin_are_explicit(self):
        self.assertEqual(self.run_ps(r'''
$legacy=Read-GuestComRequest
function Set-RequestValue($Value){$bytes=[Text.Encoding]::UTF8.GetBytes(($Value|ConvertTo-Json -Depth 5 -Compress));$script:Request=[Convert]::ToBase64String($bytes);$script:RequestSha256=Get-GuestComHash $bytes}
$cfg=[HaGuestComV1.Json]::Object(([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Request))),16384)
$cfg['schema_version']=[long]2;$cfg.Add('pipe_profile','registered-host-compute-v1');$cfg.Add('host_profile_sha256',('a'*64));$cfg.Add('host_evidence_sha256',('e'*64))
Set-RequestValue $cfg;$cfg=Read-GuestComRequest
$profile=[HaGuestComV1.HostProfile]::Parse($env:HOST_PROFILE,$cfg)
if($profile['expected_service'] -cne 'vmcompute'){throw 'profile_unbound'}
foreach($row in @(@('pipe_profile','unknown'),@('schema_version',[long]1),@('host_evidence_sha256','invalid'))){
 $copy=[HaGuestComV1.Json]::Object(($cfg|ConvertTo-Json -Compress),16384);$copy[$row[0]]=$row[1];Set-RequestValue $copy
 $blocked=$false;try{Read-GuestComRequest|Out-Null}catch{$blocked=$true};if(-not $blocked){throw 'profile_optin_or_version_boundary_lost'}
}
foreach($row in @(@('profile_name','strict-vmwp-v1'),@('expected_service','other'),@('trust_model','SYSTEM'),@('isolated_predeployment',$false),@('writer_authority_issued',$true),@('production_transition',$true),@('evidence_sha256',('9'*64)),@('evidence_source_sha256',('9'*64)),@('vm_id','22222222-2222-2222-2222-222222222222'),@('expected_pid','7372'),@('expected_file_identity','unknown'),@('capability_name','other'),@('expected_image_sha256',('0'*64)))){
 $copy=[HaGuestComV1.Json]::Object($env:HOST_PROFILE,16384);$copy[$row[0]]=$row[1]
 $blocked=$false;try{[HaGuestComV1.HostProfile]::Parse(($copy|ConvertTo-Json -Compress),$cfg)|Out-Null}catch{$blocked=$true};if(-not $blocked){throw 'unsafe_profile_accepted'}
}
$extra=$env:HOST_PROFILE.TrimEnd('}')+',"unexpected":true}'
$blocked=$false;try{[HaGuestComV1.HostProfile]::Parse($extra,$cfg)|Out-Null}catch{$blocked=$true};if(-not $blocked){throw 'unknown_profile_key_accepted'}
'profile-optin-and-root-evidence-pins-only'
'''),'profile-optin-and-root-evidence-pins-only')

    def test_node_scoped_profile_v3_is_explicit_and_keeps_both_nodes_separate(self):
        self.assertEqual(self.run_ps(r'''
$cfg=Read-GuestComRequest
$cfg['schema_version']=[long]3;$cfg.Add('pipe_profile','registered-host-compute-v1');$cfg.Add('host_profile_sha256',('a'*64));$cfg.Add('host_evidence_sha256',('e'*64))
$root=Join-Path ([IO.Path]::GetTempPath()) 'two-node-profile-contract'
$seen=@()
foreach($node in @('A','B')){
 $cfg['node']=$node;$cfg['peer_prepared_receipt_sha256']=$(if($node -ceq 'A'){''}else{'b'*64})
 $raw=[Text.Encoding]::UTF8.GetBytes(($cfg|ConvertTo-Json -Depth 5 -Compress));$script:Request=[Convert]::ToBase64String($raw);$script:RequestSha256=Get-GuestComHash $raw
 $bound=Read-GuestComRequest;$paths=Get-GuestComHostProfilePaths $root $bound
 if((Split-Path -Leaf $paths.profile) -cne ('com-'+$node+'-host-compute-profile.json') -or (Split-Path -Leaf $paths.evidence) -cne ('com-'+$node+'-host-compute-evidence.json')){throw 'node_scope_lost'}
 $seen+=@($paths.profile,$paths.evidence)
 $bound['schema_version']=[long]2;$legacy=Get-GuestComHostProfilePaths $root $bound
 if((Split-Path -Leaf $legacy.profile) -cne 'com-host-compute-profile.json' -or (Split-Path -Leaf $legacy.evidence) -cne 'com-host-compute-evidence.json'){throw 'legacy_contract_changed'}
}
if(@($seen|Sort-Object -Unique).Count -ne 4){throw 'two_nodes_collide'}
foreach($node in @('../A','a','C','A/profile')){
 $cfg['node']=$node;$closed=$false;try{$null=Get-GuestComHostProfilePaths $root $cfg}catch{$closed=$true};if(-not $closed){throw 'unbound_profile_path'}
}
$cfg['node']='A';foreach($v in @(1,4)){$cfg['schema_version']=[long]$v;$closed=$false;try{$null=Get-GuestComHostProfilePaths $root $cfg}catch{$closed=$true};if(-not $closed){throw 'unsupported_layout_version'}}
'node-profile-v3-and-legacy-v2-separate'
'''),'node-profile-v3-and-legacy-v2-separate')

    def test_host_service_acl_writer_roles_and_boundary_dispatch_fail_closed(self):
        self.assertEqual(self.run_ps(r'''
$acl=[HaGuestComV1.Json]::Object('{"owner_role":"system","aces":[{"common_noncallback":true,"sid_role":"system","qualifier":"allow","mutating_access":true},{"common_noncallback":true,"sid_role":"authenticated_users","qualifier":"allow","mutating_access":false}]}',4096)
if(-not [HaGuestComV1.HostProfile]::SafeControlAcl($acl,$false)){throw 'known_service_dacl_rejected'}
# Exercise the exact helper used by native Describe, not a parallel mask.
foreach($mask in @([uint32]0x40,[uint32]0x100,[uint32]0x20000000,[uint32]0x20000140)){
 $acl['aces'][1]['mutating_access']=[HaGuestComV1.HcsPure]::ControlMutator($mask,$true,$false)
 if(-not $acl['aces'][1]['mutating_access'] -or [HaGuestComV1.HostProfile]::SafeControlAcl($acl,$false)){throw 'service_control_bit_escaped_writer_classification'}
 $acl['aces'][1]['qualifier']='deny';$acl['aces'][1]['mutating_access']=[HaGuestComV1.HcsPure]::ControlMutator($mask,$false,$false)
 if($acl['aces'][1]['mutating_access'] -or -not [HaGuestComV1.HostProfile]::SafeControlAcl($acl,$false)){throw 'deny_ace_misclassified_as_writer'}
 $acl['aces'][1]['qualifier']='allow'
}
$acl['aces'][1]['mutating_access']=[HaGuestComV1.HcsPure]::ControlMutator([uint32]131213,$true,$false)
if($acl['aces'][1]['mutating_access'] -or -not [HaGuestComV1.HostProfile]::SafeControlAcl($acl,$false)){throw 'actual_authenticated_users_read_mask_rejected'}
# File control policy is separate and unchanged: service PAUSE_CONTINUE's 0x40
# means delete-child for files and stays a file mutator.
if(-not [HaGuestComV1.HcsPure]::ControlMutator([uint32]0x40,$true,$true)){throw 'file_mask_changed'}
$acl['aces'][1]['mutating_access']=$true
if([HaGuestComV1.HostProfile]::SafeControlAcl($acl,$false)){throw 'known_non_tcb_writer_accepted'}
$acl['aces'][1]['mutating_access']=$false;$acl['aces'][1]['sid_role']='unclassified'
if([HaGuestComV1.HostProfile]::SafeControlAcl($acl,$false)){throw 'unknown_ace_role_accepted'}
$acl['aces'][1]['sid_role']='trusted_installer_os_file_only';$acl['aces'][1]['mutating_access']=$true
if([HaGuestComV1.HostProfile]::SafeControlAcl($acl,$false)){throw 'trustedinstaller_service_writer_accepted'}
$acl['owner_role']='trusted_installer_os_file_only'
if(-not [HaGuestComV1.HostProfile]::SafeControlAcl($acl,$true)){throw 'trustedinstaller_os_file_role_rejected'}
$acl['aces'][1]['common_noncallback']=$false
if([HaGuestComV1.HostProfile]::SafeControlAcl($acl,$true)){throw 'unknown_callback_ace_accepted'}
$cfg=Read-GuestComRequest;$cfg.Add('host_evidence_sha256',('e'*64));$profile=[HaGuestComV1.HostProfile]::Parse($env:HOST_PROFILE,$cfg)
$facts=[Collections.Generic.Dictionary[string,object]]::new();$facts.Add('server_pid',[uint32]7372);$facts.Add('process_handle_retained',$true);$facts.Add('process_identity_stable',$true);$facts.Add('process_start_ticks',[long]639298297721183929);$facts.Add('process_owner_role','system')
$image='C:\Windows\System32\vmcompute.exe'
if(-not [HaGuestComV1.HostProfile]::ProcessBinding($facts,$profile,$image,$image)){throw 'retained_registered_process_rejected'}
foreach($row in @(@('server_pid',[uint32]7373),@('process_handle_retained',$false),@('process_identity_stable',$false),@('process_start_ticks',[long]639298297721183930),@('process_owner_role','caller'))){
 $before=$facts[$row[0]];$facts[$row[0]]=$row[1]
 if([HaGuestComV1.HostProfile]::ProcessBinding($facts,$profile,$image,$image)){throw 'reused_or_unregistered_process_accepted'};$facts[$row[0]]=$before
}
if([HaGuestComV1.HostProfile]::ProcessBinding($facts,$profile,'C:\Windows\System32evil\vmcompute.exe',$image)){throw 'system32_prefix_was_trusted'}
$script:strictCalls=0;$script:registeredCalls=0;$script:alive=0;$script:hostChecks=0
$process=[pscustomobject]@{Pid=[uint32]10};$process|Add-Member ScriptMethod AssertAlive {$script:alive++}
$reader=[pscustomobject]@{};$reader|Add-Member ScriptMethod Verify {param($ServerId,$Caller,$Vm) if($ServerId -ne 10){throw 'wrong_strict_pid'};$script:strictCalls++}
$reader|Add-Member ScriptMethod VerifyRegistered {param($ServerId,$Hash) if($ServerId -ne 20 -or $Hash -cne ('f'*64)){throw 'wrong_host_pid_or_descriptor'};$script:registeredCalls++}
$hostLease=[pscustomobject]@{Pid=[uint32]20;PipeDescriptorSha256=('f'*64)};$hostLease|Add-Member ScriptMethod AssertCurrent {$script:hostChecks++}
Assert-GuestComReaderBoundary $reader $process $null 'caller' 'vm'
Assert-GuestComReaderBoundary $reader $process $hostLease 'caller' 'vm'
if($script:strictCalls -ne 1 -or $script:registeredCalls -ne 1 -or $script:alive -ne 2 -or $script:hostChecks -ne 2){throw 'profile_fell_back_or_lost_vm_lease'}
$hostLease.Pid=[uint32]99;$blocked=$false;try{Assert-GuestComReaderBoundary $reader $process $hostLease 'caller' 'vm'}catch{$blocked=$true}
if(-not $blocked -or $script:strictCalls -ne 1){throw 'host_failure_fell_back_to_default'}
'host-role-and-two-process-boundaries-only'
'''),'host-role-and-two-process-boundaries-only')

    def test_dual_authz_proof_requires_every_case_and_requested_bit(self):
        self.assertEqual(self.run_ps(r'''
Add-Type -TypeDefinition @'
using System;using System.Collections.Generic;
public static class HcsProofFixture {
 public static Dictionary<string,object> Create(){string[] names={"normal_unpriv_everyone","normal_foreign_vm","normal_own_vm","normal_hvg","app_unpriv_with_cap","app_foreign_vm_with_cap","app_own_vm_with_cap","app_hvg_without_cap"};bool[] yes={false,false,true,true,false,false,true,false};var cases=new Dictionary<string,object>();
 for(int i=0;i<8;i++){var checks=new List<object>();foreach(uint mask in new uint[]{2,4,0x40000,0x80000})checks.Add(new Dictionary<string,object>{{"desired",mask},{"api_succeeded",true},{"allowed",yes[i]},{"access_error",yes[i]?0U:5U},{"granted",yes[i]?mask:0U},{"status",yes[i]?"ALLOW":"DENY"}});cases[names[i]]=new Dictionary<string,object>{{"model_status","OBSERVED"},{"normal_and_capability_separated",true},{"normal_group_contains_capability",false},{"capability_list_contains_target",i>=4&&i!=7},{"app_container_model",i>=4},{"checks",checks}};}
 return new Dictionary<string,object>{{"model_status","OBSERVED"},{"six_ace_descriptor_matches_observed",true},{"reconstructed_pipe_sddl_sha256",new String('f',64)},{"capability_matches_observed",true},{"expected_vm_matches_observed",true},{"cases",cases}};
 }
}
'@
$p=[HcsProofFixture]::Create();if(-not [HaGuestComV1.HostProfile]::ModelAccepted($p,('f'*64))){throw 'complete_dual_model_rejected'}
foreach($case in @('normal_foreign_vm','app_foreign_vm_with_cap','normal_own_vm','normal_hvg','app_own_vm_with_cap','app_hvg_without_cap')){
 $p=[HcsProofFixture]::Create();$p['cases'][$case]['checks'][0]['allowed']= -not $p['cases'][$case]['checks'][0]['allowed']
 if([HaGuestComV1.HostProfile]::ModelAccepted($p,('f'*64))){throw 'inverted_case_or_missing_restriction_accepted'}
}
$p=[HcsProofFixture]::Create();$p['cases']['app_own_vm_with_cap']['normal_group_contains_capability']=$true
if([HaGuestComV1.HostProfile]::ModelAccepted($p,('f'*64))){throw 'capability_in_normal_groups_accepted'}
$p=[HcsProofFixture]::Create();$p['cases']['normal_foreign_vm']['checks'][0]['api_succeeded']=$false
if([HaGuestComV1.HostProfile]::ModelAccepted($p,('f'*64))){throw 'api_failure_became_denial_proof'}
$p=[HcsProofFixture]::Create();$p['cases']['normal_hvg']['checks'][0]['desired']=[uint32]1
if([HaGuestComV1.HostProfile]::ModelAccepted($p,('f'*64))){throw 'wrong_access_bit_accepted'}
$p=[HcsProofFixture]::Create();$p['capability_matches_observed']=$false
if([HaGuestComV1.HostProfile]::ModelAccepted($p,('f'*64))){throw 'unknown_native_capability_accepted'}
$p=[HcsProofFixture]::Create();$p['cases'].Remove('normal_hvg')|Out-Null
if([HaGuestComV1.HostProfile]::ModelAccepted($p,('f'*64))){throw 'missing_model_case_accepted'}
$p=[HcsProofFixture]::Create();if([HaGuestComV1.HostProfile]::ModelAccepted($p,('a'*64))){throw 'descriptor_hash_mismatch_accepted'}
'dual-authz-memory-model-contract-only'
'''),'dual-authz-memory-model-contract-only')

    def test_strict_json_duplicate_nested_escaped_case_and_noninteger_rejection(self):
        self.assertEqual(self.run_ps(r'''
foreach($bad in @('{"node":"A","node":"B"}','{"node":"A","\u006Eode":"B"}','{"pins":{"a":1,"a":2}}','{"a":1.0}','{"a":1e0}','{"a":01}','{"a":true}trailing','{"a":"line\nbreak"}')){
 $blocked=$false;try{[HaGuestComV1.Json]::Object($bad,4096)|Out-Null}catch{$blocked=$true};if(-not $blocked){throw 'ambiguous_json_accepted'}
}
$value=[HaGuestComV1.Json]::Object('{"pins":{"node":"A"},"optional":null,"flag":false,"n":73}',4096)
if($value['n'] -isnot [long] -or $value['pins']['node'] -cne 'A'){throw 'typed_json_rejected'}
'json-boundaries-only'
'''),'json-boundaries-only')

    def test_request_is_exact_and_component_sources_are_not_interchangeable(self):
        self.assertEqual(self.run_ps(r'''
$cfg=Read-GuestComRequest
$receipt=[HaGuestComV1.Rules]::Receipt($env:PAYLOAD,$cfg)
if($receipt['source_commit'] -cne $cfg['seed_source_commit']){throw 'wrong_component_source'}
foreach($bad in @($env:PAYLOAD.Replace(('c'*40),('a'*40)),$env:PAYLOAD.Replace('"guest_reboot_requests":1','"guest_reboot_requests":2'),$env:PAYLOAD.Replace('"pg_started":false','"pg_started":true'),$env:PAYLOAD.Replace('"automatic_os_updates_disabled":true','"automatic_os_updates_disabled":false'),$env:PAYLOAD.Replace('"package_count":73','"package_count":74'),$env:PAYLOAD.Replace('"cloud_init_status_exit":0','"cloud_init_status_exit":false'))){
 $blocked=$false;try{[HaGuestComV1.Rules]::Receipt($bad,$cfg)|Out-Null}catch{$blocked=$true};if(-not $blocked){throw 'unverified_receipt_accepted'}
}
$RequestSha256='0'*64;$blocked=$false;try{Read-GuestComRequest|Out-Null}catch{$blocked=$true};if(-not $blocked){throw 'request_pin_ignored'}
'exact-receipt-pins-only'
'''),'exact-receipt-pins-only')

    def test_server_public_key_fingerprint_and_known_keys_are_strict(self):
        self.assertEqual(self.run_ps(r'''
$cfg=Read-GuestComRequest
foreach($bad in @($env:PAYLOAD.Replace('SHA256:','SHA256:wrong'),$env:PAYLOAD.Replace('"pg_state":"installed-not-started"','"pg_state":"running"'),$env:PAYLOAD.Substring(0,$env:PAYLOAD.Length-1)+',"unknown":1}',$env:PAYLOAD.Replace('ssh-ed25519 ','ssh-rsa '))){
 $blocked=$false;try{[HaGuestComV1.Rules]::Receipt($bad,$cfg)|Out-Null}catch{$blocked=$true};if(-not $blocked){throw 'public_key_or_shape_forgery_accepted'}
}
'public-key-wire-only'
'''),'public-key-wire-only')

    def test_unknown_pipe_writers_create_instance_and_owner_changes_are_denied(self):
        self.assertEqual(self.run_ps(r'''
$caller='S-1-5-21-1-2-3-1001';$vmSid='S-1-5-83-1-10-20-30-40';$otherVm='S-1-5-83-1-10-20-30-41'
foreach($mask in @([uint32]1,[uint32]2147483648,[uint32]1179785)){
 if(-not [HaGuestComV1.Rules]::AllowPipeAce('S-1-1-0',$mask,$caller,$vmSid) -or -not [HaGuestComV1.Rules]::AllowPipeAce('S-1-5-7',$mask,$caller,$vmSid)){throw 'readonly_pipe_ace_rejected'}
}
foreach($mask in @([uint32]2,[uint32]4,[uint32]16,[uint32]256,[uint32]65536,[uint32]262144,[uint32]524288,[uint32]1073741824,[uint32]268435456)){
 if([HaGuestComV1.Rules]::AllowPipeAce('S-1-1-0',$mask,$caller,$vmSid) -or [HaGuestComV1.Rules]::AllowPipeAce($otherVm,$mask,$caller,$vmSid)){throw 'unknown_pipe_write_allowed'}
}
if(-not [HaGuestComV1.Rules]::AllowPipeAce('S-1-5-18',[uint32]2032127,$caller,$vmSid) -or -not [HaGuestComV1.Rules]::AllowPipeAce($vmSid,[uint32]2032127,$caller,$vmSid) -or [HaGuestComV1.Rules]::TrustedSid($vmSid,$caller) -or [HaGuestComV1.Rules]::TrustedPipeSid($otherVm,$caller,$vmSid)){throw 'pipe_tcb_owner_wrong'}
'pipe-mask-policy-only'
'''),'pipe-mask-policy-only')

    def test_pid_start_time_and_exact_vm_virtual_identity_are_required(self):
        self.assertEqual(self.run_ps(r'''
$expected='S-1-5-83-1-10-20-30-40'
[HaGuestComV1.Rules]::ProcessBinding(100,100,100,123,123,$true,'C:\Windows\System32\vmwp.exe','C:\Windows\System32\vmwp.exe',$expected,$expected)
foreach($which in 0..10){$blocked=$false;try{
 $vm=100;$server=100;$retained=100;$before=123;$after=123;$alive=$true;$owner=$expected;$wanted=$expected;$image='C:\Windows\System32\vmwp.exe'
 switch($which){0{$server=101}1{$after=124}2{$alive=$false}3{$owner='S-1-5-21-1-2-3-1001'}4{$retained=99}5{$owner='S-1-5-18'}6{$owner='S-1-5-83-1-10-20-30-41'}7{$wanted='S-1-5-18'}8{$image='C:\foreign\vmwp.exe'}9{$wanted='S-1-5-83-1-0-0-0-0'}10{$wanted='S-1-5-83-1-10-20-30-4294967296'}}
 [HaGuestComV1.Rules]::ProcessBinding($vm,$server,$retained,$before,$after,$alive,$image,'C:\Windows\System32\vmwp.exe',$owner,$wanted)
}catch{$blocked=$true};if(-not $blocked){throw 'pid_reuse_or_wrong_owner_accepted'}}
'process-binding-policy-only'
'''),'process-binding-policy-only')

    def test_framer_discards_boot_logs_handles_split_prefix_and_bounds_input(self):
        self.assertEqual(self.run_ps(r'''
$frame=[HaGuestComV1.Framer]::new();$raw=[Text.Encoding]::UTF8.GetBytes("kernel boot address redacted by design`nN100_HA_")
if($null -ne $frame.Feed($raw,$raw.Length)){throw 'raw_log_became_receipt'}
$tail=[Text.Encoding]::UTF8.GetBytes("SEED_RECEIPT "+$env:PAYLOAD+"`r`n")
$value=$frame.Feed($tail,$tail.Length);if($value.TrimEnd("`r") -cne $env:PAYLOAD){throw 'split_frame_lost'}
$frame=[HaGuestComV1.Framer]::new();$big=[Text.Encoding]::ASCII.GetBytes([HaGuestComV1.Rules]::Prefix+('x'*4097))
$blocked=$false;try{$frame.Feed($big,$big.Length)|Out-Null}catch{$blocked=$true};if(-not $blocked){throw 'oversized_receipt_accepted'}
$frame=[HaGuestComV1.Framer]::new();$many=[Text.Encoding]::ASCII.GetBytes("`n"*8193)
$blocked=$false;try{$frame.Feed($many,$many.Length)|Out-Null}catch{$blocked=$true};if(-not $blocked){throw 'line_count_limit_missing'}
'bounded-prefix-framing-only'
'''),'bounded-prefix-framing-only')



    def test_schema4_root_profile_preserves_component_sources_and_pins_collector(self):
        self.run_ps(r'''
$v=Read-GuestComRequest;$v['schema_version']=[long]4
$v.Add('pipe_profile','registered-host-compute-v1');$v.Add('host_profile_sha256',('a'*64));$v.Add('host_evidence_sha256',('b'*64))
$v.Add('root_profile','strict-canonical-observed-v1');$v.Add('executor_source_commit',$v['collector_source_commit']);$v.Add('executor_script_sha256',('c'*64));$v.Add('reservation_receipt_sha256',('d'*64));$v.Add('reservation_root_identity','12345678:1234567812345678')
function Bind-Request($Value){$bytes=[Text.Encoding]::UTF8.GetBytes(($Value|ConvertTo-Json -Compress));$script:Request=[Convert]::ToBase64String($bytes);$script:RequestSha256=Get-GuestComHash $bytes}
Bind-Request $v;$accepted=Read-GuestComRequest;$paths=Get-GuestComHostProfilePaths ([IO.Path]::GetTempPath()) $accepted
if(-not $paths.profile.EndsWith('com-A-host-compute-profile.json')){throw 'node_evidence_scope_lost'}
$v['executor_source_commit']='9'*40;Bind-Request $v;$closed=$false;try{Read-GuestComRequest|Out-Null}catch{$closed=$true};if(-not $closed){throw 'collector_origin_unbound'}
''')


if __name__=='__main__':unittest.main()
