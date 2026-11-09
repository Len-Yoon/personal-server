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
                         REQUEST_SHA256=hashlib.sha256(cfg).hexdigest(),PAYLOAD=json.dumps(payload(),separators=(',',':')),PREPARED=json.dumps(prepared()[1],separators=(',',':')),FIXTURE_ROOT=prepared()[0],**extra)
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
'portable-ast-and-unarmed-only'
''')
        self.assertEqual(text,'portable-ast-and-unarmed-only')
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
function Get-VMComPort{param($VM) $path=$script:pins['com1_pipe'];if($script:tamper -ceq 'wrong-com'){$path='wrong'};$items=@([pscustomobject]@{Number=1;Path=$path});if($script:tamper -ceq 'extra-com'){$items+=([pscustomobject]@{Number=2;Path='not-empty'})};return $items}
function Get-VMSwitch{param($Id) $note='ha-owned-guest-v1:'+$cfg['installation_id']+':'+$script:pins['switch_owner_manifest_sha256'];if($script:tamper -ceq 'wrong-switch-owner'){$note='foreign'};return [pscustomobject]@{Id=[Guid]$script:pins['switch_id'];Name=$script:pins['switch_name'];SwitchType='Internal';Notes=$note}}
function Get-VMNetworkAdapter{param($VM) $items=@([pscustomobject]@{Name='ha-data';MacAddress=$script:pins['mac_address'];DynamicMacAddressEnabled=$false;MacAddressSpoofing='Off';SwitchId=$script:pins['switch_id']});if($script:pins['optional_egress_nic']){$items+=([pscustomobject]@{Name='egress-disconnected';MacAddress=$script:pins['egress_mac_address'];DynamicMacAddressEnabled=$false;MacAddressSpoofing='Off';SwitchName='Default Switch';SwitchId=[Guid]'44444444-4444-4444-4444-444444444444'})};return $items}
function Get-VMHardDiskDrive{param($VM) $path=$script:pins['disk_path'];if($script:tamper -ceq 'wrong-media'){$path='foreign'};return [pscustomobject]@{Path=$path}}
function Get-VMDvdDrive{param($VM) return [pscustomobject]@{Path=$script:pins['seed_path']}}
function Get-CimInstance{param($Namespace,$ClassName,$Filter,$OperationTimeoutSec) return [pscustomobject]@{Name=$cfg['vm_id'];EnabledState=2;ProcessID=100}}
$state=Get-GuestComVm $cfg $script:pins;if(-not $state.Running -or $state.Pid -ne 100){throw 'valid_vm_snapshot_rejected'}
foreach($case in @('wrong-com','extra-com','wrong-switch-owner','wrong-media','connected-egress')){
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
        self.assertIn('$reader.Verify($process.Pid,$caller)',invoke)
        self.assertIn('$process.AssertAlive()',invoke)
        self.assertIn('$file.AssertUnchanged()',invoke)

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
$caller='S-1-5-21-1-2-3-1001'
foreach($mask in @([uint32]1,[uint32]2147483648,[uint32]1179785)){
 if(-not [HaGuestComV1.Rules]::AllowPipeAce('S-1-1-0',$mask,$caller) -or -not [HaGuestComV1.Rules]::AllowPipeAce('S-1-5-7',$mask,$caller)){throw 'readonly_pipe_ace_rejected'}
}
foreach($mask in @([uint32]2,[uint32]4,[uint32]16,[uint32]256,[uint32]65536,[uint32]262144,[uint32]524288,[uint32]1073741824,[uint32]268435456)){
 if([HaGuestComV1.Rules]::AllowPipeAce('S-1-1-0',$mask,$caller)){throw 'unknown_pipe_write_allowed'}
}
if(-not [HaGuestComV1.Rules]::AllowPipeAce('S-1-5-18',[uint32]2032127,$caller) -or [HaGuestComV1.Rules]::TrustedSid('S-1-1-0',$caller)){throw 'pipe_tcb_owner_wrong'}
'pipe-mask-policy-only'
'''),'pipe-mask-policy-only')

    def test_pid_start_time_and_system_owner_are_required(self):
        self.assertEqual(self.run_ps(r'''
[HaGuestComV1.Rules]::ProcessBinding(100,100,100,123,123,$true,'C:\Windows\System32\vmwp.exe','C:\Windows\System32\vmwp.exe','S-1-5-18')
foreach($which in 0..4){$blocked=$false;try{
 $vm=100;$server=100;$retained=100;$before=123;$after=123;$alive=$true;$owner='S-1-5-18'
 switch($which){0{$server=101}1{$after=124}2{$alive=$false}3{$owner='S-1-5-21-1-2-3-1001'}4{$retained=99}}
 [HaGuestComV1.Rules]::ProcessBinding($vm,$server,$retained,$before,$after,$alive,'C:\Windows\System32\vmwp.exe','C:\Windows\System32\vmwp.exe',$owner)
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


if __name__=='__main__':unittest.main()
