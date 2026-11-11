"""Stage input and bounded decompression, never Windows ACL/VM acceptance."""
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]
SOURCE=Path(os.environ.get('HA_GUEST_STAGE_SOURCE',str(ROOT/'infra/k8s/ha_control/windows-guest-stage.ps1')))


class GuestStageTests(unittest.TestCase):
    def ps(self,body,**extra):
        pwsh=shutil.which('pwsh') or '/private/tmp/n100-ha-powershell/runtime/pwsh'
        result=subprocess.run([pwsh,'-NoProfile','-NonInteractive','-Command',"$ErrorActionPreference='Stop'\n. $env:STAGE_SOURCE\n"+body],env=dict(os.environ,STAGE_SOURCE=str(SOURCE),**extra),capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        return result.stdout.decode().strip()

    def test_exact_manifest_and_native_intent(self):
        value={'schema_version':1,'source_commit':'a'*40,'installation_id':'b'*64,'compressed_sha256':'c'*64,'compressed_bytes':2097152,'vhdx_sha256':'d'*64,'vhdx_bytes':4194304}
        raw=json.dumps(value).encode()
        self.assertEqual(self.ps(r'''
$script:Manifest=$env:MANIFEST;$script:ManifestSha256=$env:MANIFEST_SHA
$v=Read-GuestStageManifest
if($v.vhdx_bytes -ne 4194304){throw 'manifest_value_changed'}
$blocked=$false;try{Invoke-GuestStage}catch{$blocked=$true}
if(-not $blocked){throw 'unarmed_stage_accepted'}
foreach($field in @('command','unknown')){
 $v|Add-Member -NotePropertyName $field -NotePropertyValue 'x'
 $bytes=[Text.Encoding]::UTF8.GetBytes(($v|ConvertTo-Json -Compress));$script:Manifest=[Convert]::ToBase64String($bytes);$script:ManifestSha256=Get-GuestStageHash $bytes
 $blocked=$false;try{Read-GuestStageManifest|Out-Null}catch{$blocked=$true}
 if(-not $blocked){throw 'extra_fields_accepted'}
 $v.PSObject.Properties.Remove($field)
}
'manifest-only'
''',MANIFEST=base64.b64encode(raw).decode(),MANIFEST_SHA=hashlib.sha256(raw).hexdigest()),'manifest-only')

    def test_real_gzip_expansion_checks_bound_length_and_hash(self):
        raw=b'bounded actual decompression\n'*4096;compressed=gzip.compress(raw,mtime=0)
        self.assertEqual(self.ps(r'''
$bytes=[Convert]::FromBase64String($env:GZIP)
$source=[IO.MemoryStream]::new($bytes);$target=[IO.MemoryStream]::new()
Expand-GuestStageStream $source $target ([long]$env:EXPECTED_SIZE) $env:EXPECTED_SHA
if($target.Length -ne [long]$env:EXPECTED_SIZE){throw 'wrong_output'}
foreach($kind in @('oversize','undersize','hash')){
 $source.Position=0;$target=[IO.MemoryStream]::new();$length=[long]$env:EXPECTED_SIZE;$hash=$env:EXPECTED_SHA
 if($kind -ceq 'oversize'){$length=16}elseif($kind -ceq 'undersize'){$length++}else{$hash='0'*64}
 $blocked=$false;try{Expand-GuestStageStream $source $target $length $hash}catch{$blocked=$true}
 if(-not $blocked){throw 'invalid_expansion_accepted'}
 if($kind -ceq 'oversize' -and $target.Length -gt 16){throw 'over_limit_write'}
}
'bounded-gzip-only'
''',GZIP=base64.b64encode(compressed).decode(),EXPECTED_SIZE=str(len(raw)),EXPECTED_SHA=hashlib.sha256(raw).hexdigest()),'bounded-gzip-only')



    def test_installer_first_manifest_preserves_media_origin_and_pins_executor(self):
        value=dict(schema_version=2,source_commit='a'*40,installation_id='b'*64,compressed_sha256='c'*64,compressed_bytes=2097152,vhdx_sha256='d'*64,vhdx_bytes=4194304,
                   root_profile='strict-canonical-observed-v1',executor_source_commit='e'*40,executor_script_sha256=hashlib.sha256(SOURCE.read_bytes()).hexdigest(),reservation_receipt_sha256='f'*64,reservation_root_identity='12345678:1234567812345678')
        raw=json.dumps(value).encode()
        self.ps(r'''
$Manifest=$env:MANIFEST;$ManifestSha256=$env:MANIFEST_SHA;$v=Read-GuestStageManifest
. (Import-GuestOwnedSupport);Assert-GuestOwnedDeclaration $v (Get-GuestStageHash ([IO.File]::ReadAllBytes($env:STAGE_SOURCE)))
if($v.source_commit -ceq $v.executor_source_commit){throw 'origin_relabelled'}
$closed=$false;try{Assert-GuestOwnedDeclaration $v ('0'*64)}catch{$closed=$true};if(-not $closed){throw 'executor_unbound'}
Initialize-GuestOwnedNative
$tokens=$null;$errors=$null;$null=[Management.Automation.Language.Parser]::ParseFile((Join-Path (Split-Path $env:STAGE_SOURCE) 'guest-owned-profile.ps1'),[ref]$tokens,[ref]$errors);if($errors.Count){throw 'helper_parse_failed'}
''',MANIFEST=base64.b64encode(raw).decode(),MANIFEST_SHA=hashlib.sha256(raw).hexdigest())

    def test_owned_root_acl_only_accepts_deterministic_folder_metadata(self):
        self.ps(r'''
. (Import-GuestOwnedSupport);$id='b'*64;$sid=Get-GuestOwnedServiceSid $id
$a=@([pscustomobject]@{sid='S-1-5-18';mask=2032127;flags=3;simple_allow=$true},[pscustomobject]@{sid='S-1-5-32-544';mask=2032127;flags=3;simple_allow=$true},[pscustomobject]@{sid=$sid;mask=131232;flags=0;simple_allow=$true})
Assert-GuestOwnedRootFacts 'S-1-5-18' $true $a $id
foreach($mask in @(131104,131233,131234,1179785,2032127)){$a[2].mask=$mask;$closed=$false;try{Assert-GuestOwnedRootFacts 'S-1-5-18' $true $a $id}catch{$closed=$true};if(-not $closed){throw 'root_mask_widened'}};$a[2].mask=131232
foreach($flags in @(1,3,8,16)){$a[2].flags=$flags;$closed=$false;try{Assert-GuestOwnedRootFacts 'S-1-5-18' $true $a $id}catch{$closed=$true};if(-not $closed){throw 'root_inheritance_widened'}};$a[2].flags=0
foreach($mutation in @('sid','owner','unprotected','duplicate','callback')){
 $owner='S-1-5-18';$protected=$true;$selected=$a
 switch($mutation){'sid'{$a[2].sid='S-1-5-80-1-2-3-4-5'}'owner'{$owner='S-1-5-21-1-2-3-4'}'unprotected'{$protected=$false}'duplicate'{$selected=@($a[0],$a[1],$a[1])}'callback'{$a[2].simple_allow=$false}}
 $closed=$false;try{Assert-GuestOwnedRootFacts $owner $protected $selected $id}catch{$closed=$true};if(-not $closed){throw 'root_scope_widened'};$a[2].sid=$sid;$a[2].simple_allow=$true
}
''')

    def test_installer_receipt_phase_and_service_ownership_are_closed(self):
        self.ps(r'''
. (Import-GuestOwnedSupport)
$spec=[pscustomobject]@{root_profile='strict-canonical-observed-v1';installation_id=('b'*64);executor_source_commit=('e'*40)}
$r=[pscustomobject]@{format='fresh-service-identity-reservation-receipt-v1';descriptor_profile=$spec.root_profile;root_service_ace_mask=131232;request_sha256=('1'*64);installation_id=$spec.installation_id;selected_source_commit=$spec.executor_source_commit;build_origin_commit=('a'*40);ownership_receipt_sha256=('2'*64);service_sd_sha256=('3'*64);service_config_sha256=('4'*64);journal_identity_sha256=('5'*64);journal_sd_sha256=('6'*64);ready_to_start=$false;args_present=$false;runtime_authority_issued=$false;namespace_changed=$false;service_started=$false}
$o=[pscustomobject]@{schema_version=1;format='ha-owned-controller-service-v1';source_commit=$spec.executor_source_commit;installation_id=$spec.installation_id;service_name=('N100HA_'+$spec.installation_id.Substring(0,24)+'_controller');service_sid=(Get-GuestOwnedServiceSid $spec.installation_id);service_config_sha256=$r.service_config_sha256;service_image_sha256=('7'*64);new_owned_service=$true;scm_registration_complete=$true}
Assert-GuestOwnedReservation $r $o $spec
foreach($key in @('ready_to_start','args_present','runtime_authority_issued','namespace_changed','service_started')){
 $r.$key=$true;$closed=$false;try{Assert-GuestOwnedReservation $r $o $spec}catch{$closed=$true};if(-not $closed){throw 'non_reservation_phase_accepted'};$r.$key=$false
}
foreach($key in @('source_commit','service_sid','service_config_sha256','new_owned_service','scm_registration_complete')){
 $old=$o.$key;$o.$key=if($old -is [bool]){$false}else{'invalid'};$closed=$false;try{Assert-GuestOwnedReservation $r $o $spec}catch{$closed=$true};if(-not $closed){throw 'ownership_relabelled'};$o.$key=$old
}
''')


if __name__=='__main__':unittest.main()
