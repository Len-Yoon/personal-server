"""Pure PowerShell contracts and bounded mutation graph; no Windows runtime claim."""
import os
import json
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(os.environ.get('HA_GUEST_VM_TEST_ROOT',str(Path(__file__).resolve().parents[1])))
SOURCE=Path(os.environ.get('HA_GUEST_VM_TEST_SOURCE',str(ROOT/'infra/k8s/ha_control/windows-guest-vm.ps1')))
PWSH=shutil.which('pwsh') or next((str(p) for p in (
    Path('/private/tmp/n100-ha-powershell/runtime/pwsh'),
    ROOT/'build/ha-predeploy-evidence/windows-build/powershell-7.5.4/pwsh') if p.is_file()),'pwsh')


class GuestVmTests(unittest.TestCase):
    def ps(self, body, **extra):
        env=dict(os.environ,GUEST_VM_SOURCE=str(SOURCE),**extra)
        value=subprocess.run([PWSH,'-NoProfile','-NonInteractive','-Command',
            "$ErrorActionPreference='Stop';. $env:GUEST_VM_SOURCE\n"+body],env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(value.returncode,0,value.stderr)
        return value.stdout.strip()

    @staticmethod
    def derivation_fixture():
        upstream='04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94'
        image=dict(filename='node-A.vhdx',sha256='1'*64,bytes=1048576,
                   disk_identifier='12345678-1234-1234-1234-123456789abc',source_qcow_sha256='4'*64,
                   conversion_receipt_sha256='2'*64,tool_version='qemu-img version 11.1.3',tool_sha256='3'*64,
                   derivation_profile='cloud-init-output-preseed-v1',upstream_qcow_sha256=upstream,
                   derivation_receipt_sha256='6'*64)
        spec=dict(schema_version=2,source_commit='a'*40,stage_source_commit='e'*40,
                  seed_source_commit='f'*40,installation_id='b'*64,stage_manifest_sha256='c'*64,
                  seed_iso_sha256='d'*64,seed_iso_bytes=32768,node='A',mac_address='02123456789A',
                  optional_egress_nic=False,egress_mac_address='',peer_prepared_receipt_sha256='',
                  creation_method='verified-node-image-copy-v1',node_image=image)
        receipt=dict(schema_version=1,format='ha-guest-node-image-derivation-v1',profile=image['derivation_profile'],
                     source_commit=spec['source_commit'],node='A',upstream_qcow_sha256=upstream,
                     derived_qcow_sha256=image['source_qcow_sha256'],node_vhdx_sha256=image['sha256'],
                     node_vhdx_bytes=image['bytes'],node_disk_identifier=image['disk_identifier'],
                     patch_path='/etc/cloud/cloud.cfg.d/99zz-n100-ha-initial-output.cfg',
                     patch_sha256='659e515ee99507c6990b06c3a0560a794070514de74a9506e6ee70da20bc77c8',
                     patch_bytes=61,patch_uid=0,patch_gid=0,patch_mode='0644',filesystem_check_exit=0,
                     root_partition_offset=1074790400,root_partition_bytes=2683289088,
                     upstream_unchanged=True,initial_and_userdata_output_match=True,
                     qemu_img_version=image['tool_version'],qemu_img_sha256=image['tool_sha256'],
                     utility_image_config_digest='sha256:ded2c932b40c38c99f0b23b9edc29e160e23689db2d92cdbe251b693ca76be1b',
                     docker_context='colima-codex-ha-fixture',docker_daemon_identity_sha256='7'*64,
                     debugfs_version='debugfs 1.47.0 (5-Feb-2023)',e2fsck_version='e2fsck 1.47.0 (5-Feb-2023)')
        return json.dumps(dict(spec=spec,receipt=receipt))

    def test_derived_image_requires_explicit_upstream_chain_and_exact_receipt(self):
        self.ps(r'''
$fixture=$env:GUEST_DERIVATION_FIXTURE|ConvertFrom-Json;$good=$fixture.spec;$proof=$fixture.receipt
$null=Assert-GuestVmSpec $good;Assert-GuestVmImageDerivationReceipt $proof $good
$raw=[Text.Encoding]::UTF8.GetBytes(($good|ConvertTo-Json -Depth 5 -Compress));$Manifest=[Convert]::ToBase64String($raw);$ManifestSha256=Get-GuestVmHash $raw;$null=Read-GuestVmSpec
foreach($field in @('derivation_profile','upstream_qcow_sha256','derivation_receipt_sha256')){
 $bad=($good|ConvertTo-Json -Depth 5 -Compress)|ConvertFrom-Json;$bad.node_image.PSObject.Properties.Remove($field)
 $closed=$false;try{$null=Assert-GuestVmSpec $bad}catch{$closed=$true};if(-not $closed){throw 'partial_derivation_accepted'}
}
foreach($case in @(@('derivation_profile','arbitrary'),@('upstream_qcow_sha256',('9'*64)),@('derivation_receipt_sha256',('0'*64)),@('source_qcow_sha256',$good.node_image.upstream_qcow_sha256))){
 $bad=($good|ConvertTo-Json -Depth 5 -Compress)|ConvertFrom-Json;$bad.node_image.($case[0])=$case[1]
 $closed=$false;try{$null=Assert-GuestVmSpec $bad}catch{$closed=$true};if(-not $closed){throw 'untrusted_derived_image_accepted'}
}
foreach($case in @(@('source_commit',('9'*40)),@('node','B'),@('upstream_qcow_sha256',('9'*64)),@('derived_qcow_sha256',('9'*64)),@('node_vhdx_sha256',('9'*64)),@('node_vhdx_bytes',1048577),@('node_disk_identifier','99999999-9999-9999-9999-999999999999'),@('patch_path','/etc/other'),@('patch_sha256',('9'*64)),@('patch_bytes',60),@('patch_uid',1),@('patch_mode','0666'),@('filesystem_check_exit',1),@('root_partition_offset',0),@('root_partition_bytes',2683289089),@('upstream_unchanged',$false),@('initial_and_userdata_output_match',$false),@('qemu_img_sha256',('9'*64)),@('utility_image_config_digest',('sha256:'+('9'*64))),@('docker_context','default'),@('docker_daemon_identity_sha256',('0'*64)),@('debugfs_version','unchecked'),@('e2fsck_version','unchecked'),@('schema_version',$true),@('patch_gid',0.0),@('upstream_unchanged','true'))){
 $bad=($proof|ConvertTo-Json -Compress)|ConvertFrom-Json;$bad.($case[0])=$case[1]
 $closed=$false;try{Assert-GuestVmImageDerivationReceipt $bad $good}catch{$closed=$true};if(-not $closed){throw 'untrusted_derivation_receipt_accepted'}
}
$proof|Add-Member production_deployed $true;$closed=$false;try{Assert-GuestVmImageDerivationReceipt $proof $good}catch{$closed=$true};if(-not $closed){throw 'extra_boot_or_production_claim_accepted'}
''',GUEST_DERIVATION_FIXTURE=self.derivation_fixture())

    def test_derived_opener_reads_pinned_json_and_rejects_before_image_open(self):
        with tempfile.TemporaryDirectory() as folder:
            self.ps(r'''
Initialize-GuestVmNative
$fixture=$env:GUEST_DERIVATION_FIXTURE|ConvertFrom-Json;$spec=$fixture.spec;$proof=$fixture.receipt;$root=$env:FIXTURE
$path=Join-Path $root 'node-A.vhdx';[IO.File]::WriteAllBytes($path,[byte[]]::new(1048576));$spec.node_image.sha256=Get-GuestVmHash ([IO.File]::ReadAllBytes($path));$proof.node_vhdx_sha256=$spec.node_image.sha256
$conversion=Join-Path $root 'node-A-conversion-receipt.json';[IO.File]::WriteAllText($conversion,'{}');$spec.node_image.conversion_receipt_sha256=Get-GuestVmHash ([IO.File]::ReadAllBytes($conversion))
$receiptPath=Join-Path $root 'node-A-image-derivation.json';$text=$proof|ConvertTo-Json -Compress;[IO.File]::WriteAllText($receiptPath,$text);$spec.node_image.derivation_receipt_sha256=Get-GuestVmHash ([IO.File]::ReadAllBytes($receiptPath))
$script:imageOpened=$false;function Get-GuestVmFileIdentity([string]$Path){if($Path -eq $script:path){$script:imageOpened=$true};return $Path}
function Get-VHD([string]$Path){return [pscustomobject]@{VhdFormat='VHDX';VhdType='Dynamic';ParentPath='';Size=25769803776;Attached=$false;DiskIdentifier=$spec.node_image.disk_identifier}}
$stream=Open-GuestVmNodeImage $root $spec;try{if($stream.Length -ne 1048576){throw 'wrong_image_stream'}}finally{$stream.Dispose()}
foreach($case in @('missing','hash','content','duplicate')){
 $script:imageOpened=$false;$goodHash=$spec.node_image.derivation_receipt_sha256
 if($case -eq 'missing'){[IO.File]::Delete($receiptPath)}
 elseif($case -eq 'hash'){$spec.node_image.derivation_receipt_sha256='0'*64}
 else{$bad=$(if($case -eq 'content'){$text.Replace('"patch_mode":"0644"','"patch_mode":"0666"')}else{$text.Replace('"patch_mode":','"patch_mode":"0644","patch_mode":')});[IO.File]::WriteAllText($receiptPath,$bad);$spec.node_image.derivation_receipt_sha256=Get-GuestVmHash ([IO.File]::ReadAllBytes($receiptPath))}
 $closed=$false;try{$s=Open-GuestVmNodeImage $root $spec;$s.Dispose()}catch{$closed=$true}
 if(-not $closed -or $script:imageOpened){throw 'bad_receipt_reached_image_open'}
 [IO.File]::WriteAllText($receiptPath,$text);$spec.node_image.derivation_receipt_sha256=$goodHash
}
$exclusive=[IO.File]::Open($receiptPath,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None);$exclusive.Dispose()
''',FIXTURE=folder,GUEST_DERIVATION_FIXTURE=self.derivation_fixture())

    def test_ancestor_walk_accepts_raw_clr_parents_without_provider_properties(self):
        with tempfile.TemporaryDirectory() as name:
            path=Path(name)/'child'/'leaf.bin'
            path.parent.mkdir()
            path.write_bytes(b'fixture')
            result=self.ps(r'''
Set-StrictMode -Version Latest
$item=Get-Item -LiteralPath $env:GUEST_PARENT_FIXTURE
$directory=Get-GuestVmParent $item
if($directory -isnot [IO.DirectoryInfo]){throw 'file_parent_not_directory'}
$raw=Get-GuestVmParent $directory
if($null -ne $raw.PSObject.Properties['PSIsContainer']){throw 'fixture_did_not_exercise_raw_parent'}
$count=0
while($null -ne $raw){$count++;if($count -gt 64){throw 'ancestor_cycle'};$raw=Get-GuestVmParent $raw}
if($count -lt 2){throw 'ancestor_walk_truncated'}
$blocked=$false
try{Get-GuestVmParent ([pscustomobject]@{PSIsContainer=$true;Parent='foreign'})|Out-Null}catch{$blocked=$true}
if(-not $blocked){throw 'nonfilesystem_parent_accepted'}
'raw-clr-ancestor-walk'
''',GUEST_PARENT_FIXTURE=str(path))
            self.assertEqual(result,'raw-clr-ancestor-walk')

    def test_native_intent_and_module_import_have_no_side_effects(self):
        result=self.ps(r'''
function Get-VM {throw 'native_rpc_must_not_run'}
$Go=$true;$blocked=$false
try{Invoke-GuestVm}catch{if($_.Exception.Message -cne 'guest_vm_native_intent_required'){throw};$blocked=$true}
if(-not $blocked){throw 'native_boundary_open'}
'pure-native-boundary'
''')
        self.assertEqual(result,'pure-native-boundary')

    def test_actual_sdk_com_names_without_number_require_two_exact_bound_ports(self):
        self.ps(r'''
$id='11111111-1111-1111-1111-111111111111';$pipe='\\.\pipe\fixture'
$one=[pscustomobject]@{Name='COM 1';Id='sdk-one';VMId=[Guid]$id;Path=$pipe}
$two=[pscustomobject]@{Name='COM 2';Id='sdk-two';VMId=$id.ToUpperInvariant();Path=''}
Assert-GuestVmComPorts @($one,$two) $id $pipe
if($one.PSObject.Properties['Number']){throw 'fixture_invented_number'}
foreach($case in @(@('Name','COM 1'),@('Name','COM2'),@('Id','sdk-one'),@('VMId','22222222-2222-2222-2222-222222222222'),@('Path','other-pipe'))){
 $old=$two.($case[0]);$two.($case[0])=$case[1];$closed=$false;try{Assert-GuestVmComPorts @($one,$two) $id $pipe}catch{$closed=$true};$two.($case[0])=$old;if(-not $closed){throw 'wrong_or_duplicate_com_accepted'}
}
foreach($ports in @(@($one),@($one,$two,$two))){$closed=$false;try{Assert-GuestVmComPorts $ports $id $pipe}catch{$closed=$true};if(-not $closed){throw 'com_count_changed_accepted'}}
''')

    def test_flat_manifest_types_pins_names_and_duplicate_keys(self):
        self.ps(r'''
$good=[pscustomobject][ordered]@{schema_version=1;source_commit=('a'*40);stage_source_commit=('e'*40);seed_source_commit=('f'*40);installation_id=('b'*64);stage_manifest_sha256=('c'*64);seed_iso_sha256=('d'*64);seed_iso_bytes=32768;node='A';mac_address='02123456789A';optional_egress_nic=$false;egress_mac_address='';peer_prepared_receipt_sha256=''}
$null=Assert-GuestVmSpec $good
$n=Get-GuestVmNames $good
if($n.vm -cne ('N100HA_'+('b'*16)+'_A') -or $n.switch -cne ('N100HA_'+('b'*16)+'_data') -or $n.pipe -cne ('\\.\pipe\N100HA_'+('b'*16)+'_A_com1')){throw 'names_not_derived'}
foreach($case in @(@('schema_version','1'),@('seed_iso_bytes',32768.0),@('optional_egress_nic','false'),@('mac_address','00123456789A'),@('mac_address','03123456789A'),@('mac_address','02123456789a'),@('node','B'),@('source_commit',('A'*40)))){
 $bad=($good|ConvertTo-Json -Compress)|ConvertFrom-Json;$bad.($case[0])=$case[1];$closed=$false
 try{$null=Assert-GuestVmSpec $bad}catch{$closed=$true};if(-not $closed){throw 'unsafe_spec'}
}
$raw=[Text.Encoding]::UTF8.GetBytes(($good|ConvertTo-Json -Compress));$Manifest=[Convert]::ToBase64String($raw);$ManifestSha256=Get-GuestVmHash $raw;$null=Read-GuestVmSpec
$good.optional_egress_nic=$true;$good.egress_mac_address='02123456789B';$null=Assert-GuestVmSpec $good
$good.node='B';$good.peer_prepared_receipt_sha256='7'*64;$null=Assert-GuestVmSpec $good
$good.egress_mac_address=$good.mac_address;$closed=$false;try{$null=Assert-GuestVmSpec $good}catch{$closed=$true};if(-not $closed){throw 'duplicate_egress_mac'}
$duplicate=('{"schema_version":1,'+($good|ConvertTo-Json -Compress).Substring(1));$raw=[Text.Encoding]::UTF8.GetBytes($duplicate);$Manifest=[Convert]::ToBase64String($raw);$ManifestSha256=Get-GuestVmHash $raw
$closed=$false;try{$null=Read-GuestVmSpec}catch{$closed=$true};if(-not $closed){throw 'duplicate_accepted'}
$escaped=('{"\u0073chema_version":1,'+($good|ConvertTo-Json -Compress).Substring(1));$raw=[Text.Encoding]::UTF8.GetBytes($escaped);$Manifest=[Convert]::ToBase64String($raw);$ManifestSha256=Get-GuestVmHash $raw
$closed=$false;try{$null=Read-GuestVmSpec}catch{if($_.Exception.Message -cne 'guest_vm_duplicate_or_escaped_keys'){throw};$closed=$true};if(-not $closed){throw 'legacy_unicode_duplicate_accepted'}
''')

    def test_disconnected_nic_and_independent_disk_are_not_guessed(self):
        self.ps(r'''
foreach($id in @($null,'',[Guid]::Empty,[Guid]::Empty.ToString())){if(-not (Test-GuestVmDisconnectedNic ([pscustomobject]@{SwitchName='';SwitchId=$id}))){throw 'empty_rejected'}}
foreach($bad in @([pscustomobject]@{SwitchName='Default Switch';SwitchId=[Guid]::Empty},[pscustomobject]@{SwitchName='';SwitchId=[Guid]::NewGuid()},[pscustomobject]@{SwitchName=''},[pscustomobject]@{SwitchName='';SwitchId=7})){
 if(Test-GuestVmDisconnectedNic $bad){throw 'connection_accepted'}
}
$disk=[pscustomobject]@{VhdFormat='VHDX';VhdType='Dynamic';ParentPath='';Size=25769803776;Attached=$false;DiskIdentifier=[Guid]::NewGuid()}
Assert-GuestVmDisk $disk
foreach($case in @(@('Size',21474836480),@('Attached',$true),@('ParentPath','parent.vhdx'),@('VhdType','Differencing'))){
 $bad=($disk|ConvertTo-Json -Compress)|ConvertFrom-Json;$bad.($case[0])=$case[1];$closed=$false;try{Assert-GuestVmDisk $bad}catch{$closed=$true};if(-not $closed){throw 'unsafe_disk'}
}
''')

    def test_schema2_exact_node_image_types_and_nested_duplicate_rejection(self):
        self.ps(r'''
$good=[pscustomobject][ordered]@{schema_version=2;source_commit=('a'*40);stage_source_commit=('e'*40);seed_source_commit=('f'*40);installation_id=('b'*64);stage_manifest_sha256=('c'*64);seed_iso_sha256=('d'*64);seed_iso_bytes=32768;node='A';mac_address='02123456789A';optional_egress_nic=$false;egress_mac_address='';peer_prepared_receipt_sha256='';creation_method='verified-node-image-copy-v1';node_image=[pscustomobject][ordered]@{filename='node-A.vhdx';sha256=('1'*64);bytes=1048576;disk_identifier='12345678-1234-1234-1234-123456789abc';source_qcow_sha256='04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94';conversion_receipt_sha256=('2'*64);tool_version='qemu-img version 11.1.3';tool_sha256=('3'*64)}}
$null=Assert-GuestVmSpec $good
$raw=[Text.Encoding]::UTF8.GetBytes(($good|ConvertTo-Json -Depth 5 -Compress));$Manifest=[Convert]::ToBase64String($raw);$ManifestSha256=Get-GuestVmHash $raw;$null=Read-GuestVmSpec
foreach($case in @(@('filename','node-B.vhdx'),@('filename','../node-A.vhdx'),@('bytes',1048576.0),@('bytes',8589934593),@('disk_identifier','00000000-0000-0000-0000-000000000000'),@('disk_identifier','{12345678-1234-1234-1234-123456789abc}'),@('sha256',('A'*64)),@('source_qcow_sha256',('4'*64)),@('tool_version','qemu-img version latest'),@('tool_sha256',$true),@('conversion_receipt_sha256',''))){
 $bad=($good|ConvertTo-Json -Depth 5 -Compress)|ConvertFrom-Json;$bad.node_image.($case[0])=$case[1];$closed=$false;try{$null=Assert-GuestVmSpec $bad}catch{$closed=$true};if(-not $closed){throw 'node_image_invalid_accepted'}
}
foreach($method in @('copy','verified-node-image-copy-v2',$true)){$bad=($good|ConvertTo-Json -Depth 5 -Compress)|ConvertFrom-Json;$bad.creation_method=$method;$closed=$false;try{$null=Assert-GuestVmSpec $bad}catch{$closed=$true};if(-not $closed){throw 'method_accepted'}}
$duplicate=($good|ConvertTo-Json -Depth 5 -Compress).Replace('"filename":','"sha256":"'+('1'*64)+'","filename":');$raw=[Text.Encoding]::UTF8.GetBytes($duplicate);$Manifest=[Convert]::ToBase64String($raw);$ManifestSha256=Get-GuestVmHash $raw;$closed=$false;try{$null=Read-GuestVmSpec}catch{$closed=$true};if(-not $closed){throw 'nested_duplicate_accepted'}
foreach($escaped in @(
 ($good|ConvertTo-Json -Depth 5 -Compress).Replace('"filename":','"\u0073ha256":"'+('9'*64)+'","filename":'),
 ($good|ConvertTo-Json -Depth 5 -Compress).Replace('"node":"A"','"node":"\u0041"')
)){
 $raw=[Text.Encoding]::UTF8.GetBytes($escaped);$Manifest=[Convert]::ToBase64String($raw);$ManifestSha256=Get-GuestVmHash $raw;$closed=$false
 try{$null=Read-GuestVmSpec}catch{if($_.Exception.Message -cne 'guest_vm_duplicate_or_escaped_keys'){throw};$closed=$true};if(-not $closed){throw 'unicode_escape_accepted'}
}
$good.node_image.disk_identifier=$good.node_image.disk_identifier.ToUpperInvariant();$null=Assert-GuestVmSpec $good
$good.node='B';$good.node_image.filename='node-B.vhdx';$good.peer_prepared_receipt_sha256='7'*64;$null=Assert-GuestVmSpec $good
''')

    def test_node_image_opener_checks_actual_hash_size_acl_format_and_guid(self):
        with tempfile.TemporaryDirectory() as folder:
            self.ps(r'''
$root=$env:FIXTURE;$path=Join-Path $root 'node-A.vhdx';$receipt=Join-Path $root 'node-A-conversion-receipt.json'
[IO.File]::WriteAllBytes($path,([byte[]]@(1,2,3,4)));[IO.File]::WriteAllText($receipt,'{}')
$spec=[pscustomobject]@{node='A';node_image=[pscustomobject]@{filename='node-A.vhdx';sha256=(Get-GuestVmHash ([IO.File]::ReadAllBytes($path)));bytes=4;disk_identifier='7b0a3feb-04cf-5c40-85bd-cfd50b85ab9d';conversion_receipt_sha256=(Get-GuestVmHash ([IO.File]::ReadAllBytes($receipt)))}}
$script:bad='';function Get-GuestVmFileIdentity([string]$Path){if($script:bad -ceq 'acl'){throw 'private_acl_rejected'};return $Path}
function Get-VHD([string]$Path){return [pscustomobject]@{VhdFormat=$(if($script:bad -ceq 'format'){'VHD'}else{'VHDX'});VhdType='Dynamic';ParentPath=$(if($script:bad -ceq 'parent'){'foreign'}else{''});Size=25769803776;Attached=($script:bad -ceq 'attached');DiskIdentifier=$(if($script:bad -ceq 'guid'){[Guid]::Empty}else{$spec.node_image.disk_identifier.ToUpperInvariant()})}}
$stream=Open-GuestVmNodeImage $root $spec;try{if($stream.Position -ne 0 -or $stream.ReadByte() -ne 1){throw 'input_stream_not_pinned'}}finally{$stream.Dispose()}
foreach($case in @('acl','format','parent','attached','guid','hash','size','receipt')){
 $script:bad=$case;$oldHash=$spec.node_image.sha256;$oldBytes=$spec.node_image.bytes;$oldReceipt=$spec.node_image.conversion_receipt_sha256
 if($case -ceq 'hash'){$spec.node_image.sha256='0'*64};if($case -ceq 'size'){$spec.node_image.bytes=5};if($case -ceq 'receipt'){$spec.node_image.conversion_receipt_sha256='0'*64}
 $closed=$false;try{$s=Open-GuestVmNodeImage $root $spec;$s.Dispose()}catch{$closed=$true};if(-not $closed){throw 'untrusted_node_input_accepted'}
 $spec.node_image.sha256=$oldHash;$spec.node_image.bytes=$oldBytes;$spec.node_image.conversion_receipt_sha256=$oldReceipt
}
# Failed verification must release its own handles; this fixture can be reopened exclusively.
$s=[IO.File]::Open($path,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None);$s.Dispose()
''',FIXTURE=folder)

    def test_native_uppercase_guid_values_are_canonical_without_type_coercion(self):
        self.ps(r'''
# These are the two actual native Get-VHD string values, not Guid.ToString mocks.
foreach($native in @('7B0A3FEB-04CF-5C40-85BD-CFD50B85AB9D','F3602945-C7E3-8846-9FC2-5011BDDDFA1D')){
 $disk=[pscustomobject]@{VhdFormat='VHDX';VhdType='Dynamic';ParentPath='';Size=25769803776;Attached=$false;DiskIdentifier=$native}
 Assert-GuestVmDisk $disk
 $canonical=ConvertTo-GuestVmDiskIdentifier $disk.DiskIdentifier
 if($canonical -cne $native.ToLowerInvariant() -or $canonical -cne (ConvertTo-GuestVmDiskIdentifier ([Guid]$native))){throw 'native_guid_not_canonical'}
 # Case variation must still identify the same disk for clone/peer collisions.
 if((ConvertTo-GuestVmDiskIdentifier $native) -cne (ConvertTo-GuestVmDiskIdentifier $canonical)){throw 'case_variation_evades_collision'}
}
foreach($bad in @($null,$true,7,[Guid]::Empty,'00000000-0000-0000-0000-000000000000','{7B0A3FEB-04CF-5C40-85BD-CFD50B85AB9D}','7B0A3FEB04CF5C4085BDCFD50B85AB9D',' 7B0A3FEB-04CF-5C40-85BD-CFD50B85AB9D')){
 $closed=$false;try{$null=ConvertTo-GuestVmDiskIdentifier $bad}catch{$closed=$true};if(-not $closed){throw 'noncanonical_guid_shape_accepted'}
}
''')

    def test_reset_is_exclusively_legacy_schema1_and_strong_identity_is_preserved(self):
        self.ps(r'''
$t=$null;$e=$null;$ast=[Management.Automation.Language.Parser]::ParseFile($env:GUEST_VM_SOURCE,[ref]$t,[ref]$e);if($e.Count){throw 'parse_failed'}
$resets=@($ast.FindAll({param($a)$a -is [Management.Automation.Language.CommandAst] -and $a.GetCommandName() -ceq 'Set-VHD'},$true));if($resets.Count -ne 1){throw 'reset_path_changed'}
$branch=$resets[0].Parent;$found=$false;while($branch){if($branch -is [Management.Automation.Language.IfStatementAst] -and $branch.Extent.Text.StartsWith('if($spec.schema_version -eq 1)')){$found=$true};$branch=$branch.Parent};if(-not $found){throw 'schema2_can_reset'}
$text=[IO.File]::ReadAllText($env:GUEST_VM_SOURCE)
if(-not $text.Contains('-NoVHD -Path $guestRoot -SwitchName') -or $text.Contains('-NoVHD -Path (Join-Path $guestRoot $names.vm)')){throw 'native_vmname_wrapper_duplicated'}
if(-not $text.Contains('CreateFile(path,0x80040000,ShareMode(managedConfig,directory),') -or $text.IndexOf('Need(managedProfile||!HasUnverifiedCapability())') -gt $text.IndexOf('uint error=SetSecurityInfo')){throw 'media_lock_or_capability_guard_missing'}
if($text.Contains('ForceClose') -or $text.Contains('Stop-Service')){throw 'foreign_writer_force_closed'}
foreach($required in @('guest_vm_copy_file_identity_collision','guest_vm_node_image_identifier_collision','guest_vm_peer_disk_identifier_collision','guest_vm_node_image_pin_changed','guest_vm_duplicate_disk_identifier','Get-GuestVmParent','ReaderReadySha256')){if(-not $text.Contains($required)){throw 'strong_identity_removed'}}
''')

    def test_write_ahead_journal_durable_create_new_and_partial_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            self.ps(r'''
$p=Join-Path $env:FIXTURE 'journal';Write-GuestVmRecord $p ([ordered]@{sequence=1;event='intent';data=@{}}) $true
$before=[IO.File]::ReadAllBytes($p);$closed=$false
try{Write-GuestVmRecord $p @{bad=1} $true}catch{$closed=$true};if(-not $closed -or (Get-GuestVmHash $before) -cne (Get-GuestVmHash ([IO.File]::ReadAllBytes($p)))){throw 'intent_overwritten'}
function Open-GuestVmPinned([string]$Path,[string]$Hash,[long]$Size){return [IO.File]::OpenRead($Path)}
$closed=$false;try{$null=Read-GuestVmJournal $p ('a'*64)}catch{$closed=$true};if(-not $closed){throw 'partial_accepted'}
Write-GuestVmRecord $p ([ordered]@{sequence=2;event='prepared';data=@{}}) $false
$r=Read-GuestVmJournal $p ('a'*64);if($r.Count -ne 2){throw 'journal_not_read'}
Write-GuestVmRecord $p ([ordered]@{sequence=3;event='start_requested';data=@{}}) $false
$closed=$false;try{$null=Read-GuestVmJournal $p ('a'*64)}catch{$closed=$true};if(-not $closed){throw 'ambiguous_restart_accepted'}
''',FIXTURE=folder)

    def test_prepared_guard_reads_actual_identity_resources_media_and_firmware(self):
        self.ps(r'''
function Join-Path([string]$Path,[string]$ChildPath){return $Path.TrimEnd('\')+'\'+$ChildPath}
$ManifestSha256='1'*64
$spec=[pscustomobject]@{installation_id=('b'*64);source_commit=('a'*40);stage_source_commit=('e'*40);seed_source_commit=('f'*40);node='A';stage_manifest_sha256=('c'*64);seed_iso_sha256=('d'*64);seed_iso_bytes=32768;mac_address='02123456789A';optional_egress_nic=$false;egress_mac_address='';peer_prepared_receipt_sha256=''}
$names=Get-GuestVmNames $spec;$root='C:\Private\guest-A';$disk=$root+'\guest-A.vhdx';$iso='C:\Private\seed-A.iso';$vmId='11111111-1111-1111-1111-111111111111';$switchId='22222222-2222-2222-2222-222222222222'
$pins=[pscustomobject][ordered]@{installation_id=$spec.installation_id;source_commit=$spec.source_commit;stage_source_commit=$spec.stage_source_commit;seed_source_commit=$spec.seed_source_commit;node='A';manifest_sha256=$ManifestSha256;stage_manifest_sha256=$spec.stage_manifest_sha256;vm_id=$vmId;vm_name=$names.vm;switch_id=$switchId;switch_name=$names.switch;config_path=$root;disk_path=$disk;disk_sha256=('2'*64);disk_identity='disk-id';disk_identifier='33333333-3333-3333-3333-333333333333';seed_path=$iso;seed_sha256=$spec.seed_iso_sha256;seed_identity='seed-id';mac_address=$spec.mac_address;com1_pipe=$names.pipe;optional_egress_nic=$false;egress_mac_address='';peer_prepared_receipt_sha256='';switch_owner_manifest_sha256=$ManifestSha256}
$script:vm=[pscustomobject]@{Id=[Guid]$vmId;Name=$names.vm;Generation=2;State='Off';Path=$root;Notes=('ha-owned-guest-v1:'+$spec.installation_id+':'+$ManifestSha256);AutomaticStartAction='Nothing';AutomaticStopAction='TurnOff';CheckpointType='Disabled';AutomaticCheckpointsEnabled=$false}
$script:sw=[pscustomobject]@{Id=[Guid]$switchId;Name=$names.switch;SwitchType='Internal';Notes=$script:vm.Notes}
$script:cpu=[pscustomobject]@{Count=1;Maximum=25};$script:mem=[pscustomobject]@{DynamicMemoryEnabled=$true;Startup=1073741824;Minimum=805306368;Maximum=1610612736}
$script:nic=[pscustomobject]@{Name='ha-data';DynamicMacAddressEnabled=$false;MacAddressSpoofing='Off';MacAddress=$spec.mac_address;SwitchId=[Guid]$switchId};$script:egress=$null
$script:hdd=[pscustomobject]@{Path=$disk;Id='drive-id'};$script:dvd=[pscustomobject]@{Path=$iso}
$script:vhd=[pscustomobject]@{VhdFormat='VHDX';VhdType='Dynamic';ParentPath='';Size=25769803776;Attached=$false;DiskIdentifier=[Guid]$pins.disk_identifier}
$script:fw=[pscustomobject]@{SecureBoot='On';SecureBootTemplate='MicrosoftUEFICertificateAuthority';BootOrder=@([pscustomobject]@{Device=$script:hdd})}
$script:port=[pscustomobject]@{Name='COM 1';Id='fixture-com-one';VMId=[Guid]$vmId;Path=$names.pipe};$script:port2=[pscustomobject]@{Name='COM 2';Id='fixture-com-two';VMId=[Guid]$vmId;Path=''}
function Get-VM {return $script:vm};function Get-VMSwitch{return $script:sw};function Get-VMProcessor{return $script:cpu};function Get-VMMemory{return $script:mem}
function Get-VMNetworkAdapter{if($null -ne $script:egress){return @($script:nic,$script:egress)};return $script:nic};function Get-VMHardDiskDrive{return $script:hdd};function Get-VMDvdDrive{return $script:dvd};function Get-VHD{return $script:vhd}
function Get-VMFirmware{return $script:fw};function Get-VMComPort{return @($script:port,$script:port2)};function Get-GuestVmSid{return 'owned-vm-sid'}
function Get-GuestVmFileIdentity([string]$Path){if($Path.EndsWith('.vhdx')){return 'disk-id'};return 'seed-id'}
function Open-GuestVmPinned {return [IO.MemoryStream]::new()};function Assert-GuestVmPrivate {}
$null=Assert-GuestVmPrepared $spec $names $pins $root $disk $iso
$pins.disk_identifier='7b0a3feb-04cf-5c40-85bd-cfd50b85ab9d';$script:vhd.DiskIdentifier='7B0A3FEB-04CF-5C40-85BD-CFD50B85AB9D'
$spec|Add-Member schema_version 2;$spec|Add-Member node_image ([pscustomobject]@{disk_identifier=$pins.disk_identifier.ToUpperInvariant();sha256=$pins.disk_sha256})
$null=Assert-GuestVmPrepared $spec $names $pins $root $disk $iso
foreach($field in @('sha256','disk_identifier')){$prior=$spec.node_image.$field;$spec.node_image.$field='9'*64;$closed=$false;try{$null=Assert-GuestVmPrepared $spec $names $pins $root $disk $iso}catch{$closed=$true};$spec.node_image.$field=$prior;if(-not $closed){throw 'node_image_manifest_detached_from_receipt'}}
$spec.PSObject.Properties.Remove('schema_version');$spec.PSObject.Properties.Remove('node_image')
$pins.disk_identifier='33333333-3333-3333-3333-333333333333';$script:vhd.DiskIdentifier=[Guid]$pins.disk_identifier
foreach($case in @(@('vm','Notes','other'),@('vm','State','Running'),@('sw','SwitchType','External'),@('cpu','Maximum',100),@('mem','Maximum',2147483648),@('nic','MacAddress','02123456789B'),@('hdd','Path','C:\Other.vhdx'),@('vhd','ParentPath','other.vhdx'),@('fw','SecureBoot','Off'),@('port','Path','other-pipe'))){
 $object=Get-Variable -Name $case[0] -Scope Script -ValueOnly;$prior=$object.($case[1]);$object.($case[1])=$case[2];$closed=$false
 try{$null=Assert-GuestVmPrepared $spec $names $pins $root $disk $iso}catch{$closed=$true};$object.($case[1])=$prior;if(-not $closed){throw 'changed_vm_accepted'}
}
$spec.optional_egress_nic=$true;$spec.egress_mac_address='02123456789B';$pins.optional_egress_nic=$true;$pins.egress_mac_address=$spec.egress_mac_address
$script:egress=[pscustomobject]@{Name='egress-disconnected';DynamicMacAddressEnabled=$false;MacAddressSpoofing='Off';MacAddress=$spec.egress_mac_address;SwitchName='';SwitchId=[Guid]::Empty}
$null=Assert-GuestVmPrepared $spec $names $pins $root $disk $iso
foreach($case in @(@('MacAddress','02123456789C'),@('DynamicMacAddressEnabled',$true),@('MacAddressSpoofing','On'),@('SwitchName','Default Switch'))){
 $old=$script:egress.($case[0]);$script:egress.($case[0])=$case[1];$closed=$false;try{$null=Assert-GuestVmPrepared $spec $names $pins $root $disk $iso}catch{$closed=$true};$script:egress.($case[0])=$old;if(-not $closed){throw 'egress_pin_ignored'}
}
$spec.optional_egress_nic=$false;$spec.egress_mac_address='';$pins.optional_egress_nic=$false;$pins.egress_mac_address='';$script:egress=$null
$script:vm.State='Running';$script:vhd.Attached=$true
function Get-GuestVmFileIdentity([string]$Path,[string]$Sid,[bool]$Writer,[bool]$SharedMetadata){if($Path.EndsWith('.vhdx')){if(-not $SharedMetadata){throw 'running_peer_exclusive_handle'};return 'disk-id'};return 'seed-id'}
function Open-GuestVmPinned([string]$Path){if($Path.EndsWith('.vhdx')){throw 'running_peer_disk_contents_read'};return [IO.MemoryStream]::new()}
$null=Assert-GuestVmPrepared $spec $names $pins $root $disk $iso $ManifestSha256 $true
$b=($spec|ConvertTo-Json -Compress)|ConvertFrom-Json;$b.node='B';$b.mac_address='02123456789C';$b.peer_prepared_receipt_sha256='3'*64
function Read-GuestVmPreparedReceipt{return [pscustomobject]@{schema_version=1;prepared=$true;pins=$pins}}
function Get-Item{return [pscustomobject]@{Length=32768}}
$ManifestSha256='4'*64
$peer=Get-GuestVmPeer 'C:\Private' $b;if($peer.switch.Id.ToString() -cne $switchId){throw 'wrong_switch_adopted'}
$b|Add-Member schema_version 2;$b|Add-Member creation_method 'verified-node-image-copy-v1';$b|Add-Member node_image ([pscustomobject]@{filename='node-B.vhdx';disk_identifier='44444444-4444-4444-4444-444444444444'})
$peer=Get-GuestVmPeer 'C:\Private' $b;if($peer.pins.manifest_sha256 -cne $pins.manifest_sha256 -or $peer.pins.disk_identifier -cne $pins.disk_identifier){throw 'schema2_peer_receipt_identity_lost'}
$b.stage_source_commit='9'*40;$closed=$false;try{$null=Get-GuestVmPeer 'C:\Private' $b}catch{$closed=$true};if(-not $closed){throw 'peer_component_mismatch'}
''')

    def test_reader_ready_requires_live_exact_process_and_is_not_pipe_trust(self):
        self.ps(r'''
function Join-Path([string]$Path,[string]$ChildPath){return $Path.TrimEnd('\')+'\'+$ChildPath}
$ReaderReadySha256='1'*64;$PreparedReceiptSha256='2'*64;$ReaderSourceCommit='3'*40
$spec=[pscustomobject]@{installation_id=('4'*64);node='B'};$pins=[pscustomobject]@{vm_id='11111111-1111-1111-1111-111111111111';com1_pipe='fixture-pipe'}
$stamp=[DateTime]::new(638000000000000000,[DateTimeKind]::Utc)
$script:ready=[pscustomobject]@{format='ha-guest-com-reader-ready-v1';schema_version=1;source_commit=$ReaderSourceCommit;request_sha256=('5'*64);prepared_receipt_sha256=$PreparedReceiptSha256;installation_id=$spec.installation_id;node='B';vm_id=$pins.vm_id;com1_pipe=$pins.com1_pipe;reader_pid=321;reader_start_ticks=$stamp.Ticks;reader_image_sha256=('e'*64);pipe_bound=$false}
$script:process=[pscustomobject]@{Id=321;HasExited=$false;StartTime=$stamp;Path='C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'}
$script:process|Add-Member ScriptMethod Refresh {}; $script:process|Add-Member ScriptMethod Dispose {}
function Open-GuestVmPinned{$bytes=[Text.Encoding]::UTF8.GetBytes(($script:ready|ConvertTo-Json -Compress));return [IO.MemoryStream]::new($bytes,$false)}
function Get-Process{return $script:process};function Get-GuestVmReaderImagePath{return 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'};function Get-FileHash{return [pscustomobject]@{Hash=('E'*64)}}
$script:owner='S-1-5-18';function Get-GuestVmReaderOwner{return $script:owner};function Get-GuestVmCurrentSid{return 'S-1-5-18'}
$null=Assert-GuestVmReaderReady 'C:\Private' $spec $pins
foreach($case in @(@('schema_version',$true),@('pipe_bound',$true),@('reader_start_ticks',($stamp.Ticks+1)),@('reader_pid',322),@('prepared_receipt_sha256',('9'*64)))){
 $old=$script:ready.($case[0]);$script:ready.($case[0])=$case[1];$closed=$false;try{$null=Assert-GuestVmReaderReady 'C:\Private' $spec $pins}catch{$closed=$true};$script:ready.($case[0])=$old;if(-not $closed){throw 'unsafe_ready'}
}
foreach($case in @(@('HasExited',$true),@('Path','C:\Other\powershell.exe'))){$old=$script:process.($case[0]);$script:process.($case[0])=$case[1];$closed=$false;try{$null=Assert-GuestVmReaderReady 'C:\Private' $spec $pins}catch{$closed=$true};$script:process.($case[0])=$old;if(-not $closed){throw 'wrong_process_accepted'}}
$script:owner='S-1-5-21-1-2-3-4';$closed=$false;try{$null=Assert-GuestVmReaderReady 'C:\Private' $spec $pins}catch{$closed=$true};if(-not $closed){throw 'wrong_owner_accepted'}
''')

    def test_immutable_receipt_schema_does_not_coerce_boolean_or_string(self):
        self.ps(r'''
$script:frame=[pscustomobject]@{schema_version=1;prepared=$true;pins=[pscustomobject]@{}}
function Open-GuestVmPinned{$bytes=[Text.Encoding]::UTF8.GetBytes(($script:frame|ConvertTo-Json -Compress));return [IO.MemoryStream]::new($bytes,$false)}
$null=Read-GuestVmPreparedReceipt 'fixture' ('1'*64)
foreach($value in @($true,'1',1.0,2)){$script:frame.schema_version=$value;$closed=$false;try{$null=Read-GuestVmPreparedReceipt 'fixture' ('1'*64)}catch{$closed=$true};if(-not $closed){throw 'schema_coerced'}}
''')

    def test_native_metadata_helper_compiles_without_invoking_windows_apis(self):
        self.ps("Initialize-GuestVmNative; if(-not ('HaGuestVmFileV1' -as [type])){throw 'compile_failed'}")

    def test_seal_role_policy_and_json_duplicate_parser_are_portable(self):
        self.ps(r'''
Initialize-GuestVmNative
$sid='S-1-5-83-1-2-3-4-5'
foreach($role in @('config','disk','seed')){
 $rows=[HaGuestVmSealV1]::Policy($sid,$role)
 if($rows.Count -ne 3 -or $rows[0] -cne 'S-1-5-32-544|2032127' -or $rows[1] -cne 'S-1-5-18|2032127' -or $rows[2] -cne ($sid+'|'+$(if($role -ceq 'seed'){'1179785'}elseif($role -ceq 'disk'){'1180063'}else{'2032127'}))){throw 'role_policy_not_exact'}
}
if([HaGuestVmSealV1]::Policy('','private').Count -ne 2){throw 'private_parent_granted_vm_access'}
foreach($bad in @('S-1-5-83-0','S-1-15-3-1','S-1-5-18')){$closed=$false;try{$null=[HaGuestVmSealV1]::Policy($bad,'config')}catch{$closed=$true};if(-not $closed){throw 'global_or_capability_sid_accepted'}}
$null=[HaGuestVmJsonV1]::Object('{"objects":[{"sha256":"a"}]}',1024)
foreach($bad in @('{"objects":[{"sha256":"a","sha256":"b"}]}','{"objects":[{"sha256":"a","SHA256":"b"}]}','{"objects":[{"sha256":"a","\u0073ha256":"b"}]}')){$closed=$false;try{$null=[HaGuestVmJsonV1]::Object($bad,1024)}catch{$closed=$true};if(-not $closed){throw 'request_duplicate_accepted'}}
''')

    def test_private_parent_capability_is_verified_removal_input_never_final_authority(self):
        self.ps(r'''
Initialize-GuestVmNative
$cap='S-1-15-3-1024-2268835264-3721307629-241982045-173645152-1490879176-104643441-2915960892-1612460704'
foreach($role in @('private','disk','seed')){
 foreach($mask in @(2032127,1180063,1179785)){
  if(-not [HaGuestVmSealV1]::CapabilityRuleAllowed($role,$cap,$cap,$mask,16,$true,$false,$false)){throw 'known_removal_input_rejected'}
 }
 $rows=[HaGuestVmSealV1]::ManagedPolicy('S-1-5-83-1-2-3-4-5',$role,$cap,$true)
 if(@($rows|Where-Object{$_ -like 'S-1-15-*'}).Count){throw 'removed_capability_retained'}
}
foreach($args in @(
 @('private','S-1-15-3-foreign',$cap,2032127,0,$true,$false),
 @('private',$cap,$cap,268435456,0,$true,$false),
 @('private',$cap,$cap,2032127,0,$false,$false),
 @('private',$cap,$cap,2032127,0,$true,$true),
 @('unknown',$cap,$cap,2032127,0,$true,$false),
 @('config',$cap,$cap,2032127,8,$true,$false),
 @('config',$cap,$cap,1180063,0,$true,$false)
)){
 if([HaGuestVmSealV1]::CapabilityRuleAllowed($args[0],$args[1],$args[2],$args[3],$args[4],$args[5],$args[6],$true)){throw 'unverified_capability_accepted'}
}
# Exercise the product inventory entry for the actual rejected private role.
function Assert-GuestVmConfigProfile {return $true}
function Get-GuestVmSealTargets {return ,@([pscustomobject]@{path='owned';relative_path='guest-A';role='private';directory=$true})}
function Get-GuestVmCurrentSid {return 'S-1-5-32-544'}
$script:bound=$false;$script:sealed=$false
function Open-GuestVmSealHandle {
 $h=[pscustomobject]@{}
 $h|Add-Member ScriptMethod Identity {return '00000001:0000000000000001'}
 $h|Add-Member ScriptMethod Bytes {return 0};$h|Add-Member ScriptMethod Hash {return ''}
 $h|Add-Member ScriptMethod SddlHash {return ('a'*64)}
 $h|Add-Member ScriptMethod Owner {return 'S-1-5-32-544'}
 $h|Add-Member ScriptMethod BindProfile {param($name,$sid,$role)if($name -cne 'windows-managed-config-v1' -or $sid -cne $cap -or $role -cne 'private'){throw 'wrong_profile_binding'};$script:bound=$true}
 $h|Add-Member ScriptMethod HasUnverifiedCapability {return $true}
 $h|Add-Member ScriptMethod Seal {$script:sealed=$true}
 $h|Add-Member ScriptMethod Dispose {}
 return $h
}
$profile=[pscustomobject]@{name='windows-managed-config-v1';capability_sid=$cap}
$sessions=Open-GuestVmSealInventory 'root' ([pscustomobject]@{}) ([pscustomobject]@{}) $null $profile
try{if(-not $script:bound -or $script:sealed){throw 'private_removal_not_verified_before_mutation'}}finally{foreach($s in $sessions){$s.handle.Dispose()}}
# Absence of the explicit profile still rejects the same capability.
$closed=$false;try{$null=Open-GuestVmSealInventory 'root' ([pscustomobject]@{}) ([pscustomobject]@{}) $null}catch{if($_.Exception.Message -cne 'guest_vm_capability_proof_required'){throw};$closed=$true}
if(-not $closed -or $script:sealed){throw 'strict_private_policy_relaxed'}
''')

    def test_observed_child_only_templates_preserve_effective_config_rights_without_parent_grant(self):
        self.ps(r'''
Initialize-GuestVmNative
$cap='S-1-15-3-1024-2268835264-3721307629-241982045-173645152-1490879176-104643441-2915960892-1612460704'
foreach($role in @('private','config')){
 foreach($flags in @(11,27)){
  if(-not [HaGuestVmSealV1]::CapabilityRuleAllowed($role,$cap,$cap,268435456,$flags,$true,$false,$true)){throw 'observed_child_template_rejected'}
  if([HaGuestVmSealV1]::CapabilityRuleAllowed($role,$cap,$cap,268435456,$flags,$true,$false,$false)){throw 'file_child_template_accepted'}
 }
}
foreach($flags in @(0,3,8,15,16,19,24,31,64)){
 if([HaGuestVmSealV1]::CapabilityRuleAllowed('config',$cap,$cap,268435456,$flags,$true,$false,$true)){throw 'unknown_template_shape_accepted'}
}
foreach($role in @('disk','seed')){if([HaGuestVmSealV1]::CapabilityRuleAllowed($role,$cap,$cap,268435456,11,$true,$false,$true)){throw 'data_template_accepted'}}
function Get-FixtureEffectiveAccess([int[]]$Masks,[int[]]$Flags,[bool]$Child){
 $rights=0
 for($i=0;$i -lt $Masks.Count;$i++){
  if($Child){if(($Flags[$i] -band 3) -ne 3){continue}}
  elseif($Flags[$i] -band 8){continue}
  $rights=$rights -bor $(if($Masks[$i] -eq 268435456){2032127}else{$Masks[$i]})
 }
 return $rights
}
foreach($flags in @(11,27)){
 $plan=[HaGuestVmSealV1]::ConfigurationCapabilityPlan([int[]]@(2032127,268435456),[int[]]@(16,$flags),$true)
 if($plan -ne 3){throw 'effective_parent_and_children_not_preserved'}
 $parentBefore=Get-FixtureEffectiveAccess @(2032127,268435456) @(16,$flags) $false
 $childrenBefore=Get-FixtureEffectiveAccess @(2032127,268435456) @(16,$flags) $true
 $parentAfter=Get-FixtureEffectiveAccess @(2032127) @($plan) $false
 $childrenAfter=Get-FixtureEffectiveAccess @(2032127) @($plan) $true
 if($parentBefore -ne $parentAfter -or $childrenBefore -ne $childrenAfter){throw 'normalization_changes_effective_rights'}
 $closed=$false;try{$null=[HaGuestVmSealV1]::ConfigurationCapabilityPlan([int[]]@(268435456),[int[]]@($flags),$true)}catch{$closed=$true}
 if(-not $closed){throw 'child_only_template_acquired_parent_grant'}
}
if([HaGuestVmSealV1]::ConfigurationCapabilityPlan([int[]]@(),[int[]]@(),$true) -ne -1){throw 'missing_capability_granted'}
if([HaGuestVmSealV1]::ConfigurationCapabilityPlan([int[]]@(2032127),[int[]]@(16),$false) -ne 0){throw 'file_inheritance_added'}
foreach($bad in @(@(2032127,8),@(268435456,3))){$closed=$false;try{$null=[HaGuestVmSealV1]::ConfigurationCapabilityPlan([int[]]@($bad[0]),[int[]]@($bad[1]),$true)}catch{$closed=$true};if(-not $closed){throw 'invalid_config_plan_accepted'}}
foreach($role in @('private','disk','seed')){if(@([HaGuestVmSealV1]::ManagedPolicy('S-1-5-83-1-2-3-4-5',$role,$cap,$true)|Where-Object{$_ -like 'S-1-15-*'}).Count){throw 'private_data_capability_retained'}}
''')

    def test_retained_inventory_rejects_identity_content_acl_and_foreign_writer_before_seal(self):
        setup=r'''
Initialize-GuestVmNative
$script:trace=[Collections.Generic.List[string]]::new();$script:handles=[Collections.Generic.List[object]]::new();$script:bad=''
$spec=[pscustomobject]@{seed_iso_bytes=4};$pins=[pscustomobject]@{disk_identity='00000001:0000000000000002';disk_sha256=('d'*64);seed_identity='00000001:0000000000000003';seed_sha256=('e'*64)}
$script:targets=@([pscustomobject]@{path='parent';relative_path='guest-A';role='private';directory=$true},[pscustomobject]@{path='disk';relative_path='guest-A/guest-A.vhdx';role='disk';directory=$false},[pscustomobject]@{path='seed';relative_path='seed-A.iso';role='seed';directory=$false})
function Get-GuestVmSealTargets{return ,$script:targets};function Get-GuestVmCurrentSid{return 'S-1-5-32-544'}
function Add-GuestVmEvent($Event,$Data){$script:trace.Add($Event+':'+$Data.relative_path)}
function Open-GuestVmSealHandle([string]$Path,[bool]$Directory){
 if($script:bad -ceq 'writer' -and $Path -ceq 'seed'){throw 'guest_vm_seal_open_32'}
 $index=@('parent','disk','seed').IndexOf($Path)+1
 $h=[pscustomobject]@{id=('00000001:'+([long]$index).ToString('x16'));fileBytes=$(if($Directory){0}else{4});fileHash=$(if($Directory){''}elseif($Path -ceq 'disk'){'d'*64}else{'e'*64});sddl=('a'*64);closed=$false;isCanonical=$false;path=$Path}
 $h|Add-Member ScriptMethod Identity {return $this.id};$h|Add-Member ScriptMethod Bytes {return $this.fileBytes};$h|Add-Member ScriptMethod Hash {return $this.fileHash};$h|Add-Member ScriptMethod SddlHash {return $this.sddl};$h|Add-Member ScriptMethod Owner {return 'S-1-5-32-544'}
 $h|Add-Member ScriptMethod HasUnverifiedCapability {return ($script:bad -ceq 'capability' -and $this.path -ceq 'disk')}
 $h|Add-Member ScriptMethod Canonical {param($sid,$role)return $this.isCanonical}
 $h|Add-Member ScriptMethod Seal {param($sid,$role)$this.isCanonical=$true;$this.sddl='f'*64;if($script:bad -ceq 'during-seal'){$this.id='00000001:0000000000000009'}}
 $h|Add-Member ScriptMethod Dispose {$this.closed=$true};$script:handles.Add($h);return $h
}
$s=Open-GuestVmSealInventory 'root' $spec $pins $null;$expected=@($s|ForEach-Object{$_.before});foreach($session in $s){$session.handle.Dispose()};$script:handles.Clear()
'''
        self.ps(setup+r'''
foreach($field in @('identity','sha256','bytes','sddl_sha256','role','relative_path')){
 $bad=($expected|ConvertTo-Json -Depth 5 -Compress)|ConvertFrom-Json
 $bad[1].$field=$(if($field -ceq 'bytes'){5}else{'wrong'})
 $closed=$false;try{$s=Open-GuestVmSealInventory 'root' $spec $pins $bad;foreach($v in $s){$v.handle.Dispose()}}catch{$closed=$true}
 if(-not $closed -or $script:trace.Count -ne 0 -or @($script:handles|Where-Object{-not $_.closed}).Count){throw 'unverified_object_adopted'};$script:handles.Clear()
}
foreach($case in @('writer','capability')){
 $script:bad=$case;$closed=$false;try{$s=Open-GuestVmSealInventory 'root' $spec $pins $expected}catch{$closed=$true}
 if(-not $closed -or $script:trace.Count -ne 0 -or @($script:handles|Where-Object{-not $_.closed}).Count){throw 'unsafe_inventory_modified'};$script:handles.Clear()
}
$script:bad='';$s=Open-GuestVmSealInventory 'root' $spec $pins $expected
function Get-GuestVmFreshSealIdentity([string]$Path){if($script:bad -ceq 'replacement'){return '00000001:0000000000000099'};return @{'disk'=$pins.disk_identity;'seed'=$pins.seed_identity}[$Path]}
Assert-GuestVmSealInventoryComplete 'root' $spec $pins $s
$script:bad='replacement';$closed=$false;try{Assert-GuestVmSealInventoryComplete 'root' $spec $pins $s}catch{if($_.Exception.Message -cne 'guest_vm_seal_path_replaced'){throw};$closed=$true};if(-not $closed){throw 'unlinked_retained_handle_adopted'};$script:bad=''
try{$sealed=Invoke-GuestVmSealInventory $s 'S-1-5-83-1-2-3-4-5';if($sealed.Count -ne 3 -or $script:trace[0] -cne 'seal_exact_owned_object:seed-A.iso' -or $script:trace[2] -cne 'seal_exact_owned_object:guest-A'){throw 'parent_sealed_before_child'}}finally{foreach($v in $s){$v.handle.Dispose()}}
$script:bad='during-seal';$s=Open-GuestVmSealInventory 'root' $spec $pins $expected;$closed=$false
try{$null=Invoke-GuestVmSealInventory $s 'S-1-5-83-1-2-3-4-5'}catch{$closed=$true}finally{foreach($v in $s){$v.handle.Dispose()}};if(-not $closed){throw 'post_seal_identity_changed_accepted'}
''')

    def test_finalize_rejects_replay_and_source_before_native_or_acl(self):
        self.ps(r'''
function Join-Path([string]$Path,[string]$ChildPath){return $Path+'\'+$ChildPath}
$script:existing=$true;$script:trace=0
function Test-Path{return $script:existing};function Read-GuestVmJsonPinned{return $script:request};function Get-GuestVmScriptHash{return ('f'*64)}
function Assert-GuestVmPrepared{$script:trace++;throw 'must_not_reach_native'}
$FinalizationRequestSha256='1'*64;$JournalSha256='2'*64;$ManifestSha256='3'*64;$FinalizerSourceCommit='a'*40
$spec=[pscustomobject]@{source_commit=('b'*40);installation_id=('c'*64);node='A'}
$script:request=[pscustomobject]@{schema_version=1;finalizer_source_commit=$FinalizerSourceCommit;finalizer_script_sha256=('f'*64);original_source_commit=$spec.source_commit;original_manifest_sha256=$ManifestSha256;original_journal_sha256=$JournalSha256;installation_id=$spec.installation_id;node='A';root_identity='id';root_sddl_sha256=('d'*64);pins=[pscustomobject]@{};objects=@()}
$closed=$false;try{Invoke-GuestVmFinalize 'root' $spec $null 'guest' 'disk' 'iso' 'journal' 'prepared'}catch{if($_.Exception.Message -cne 'guest_vm_finalize_existing_partial'){throw};$closed=$true};if(-not $closed -or $script:trace){throw 'finalize_replayed'}
$script:existing=$false
foreach($key in @('finalizer_source_commit','finalizer_script_sha256','original_source_commit','original_manifest_sha256','original_journal_sha256','installation_id','node')){
 $old=$script:request.$key;$script:request.$key='wrong';$closed=$false
 try{Invoke-GuestVmFinalize 'root' $spec $null 'guest' 'disk' 'iso' 'journal' 'prepared'}catch{if($_.Exception.Message -cne 'guest_vm_finalize_source_rejected'){throw};$closed=$true};$script:request.$key=$old
 if(-not $closed -or $script:trace){throw 'source_pin_checked_after_native'}
}
''')

    def test_seal_checks_length_before_any_content_hash(self):
        self.ps(r'''
$target=[pscustomobject]@{role='disk';directory=$false}
$handle=[pscustomobject]@{}
$handle|Add-Member ScriptMethod Bytes {return [long]8589934593}
$handle|Add-Member ScriptMethod Hash {throw 'oversize_file_was_hashed'}
$closed=$false;try{$null=Get-GuestVmHandleRow $handle $target}catch{if($_.Exception.Message -cne 'guest_vm_seal_size_limit'){throw};$closed=$true}
if(-not $closed){throw 'oversize_handle_adopted'}
''')

    def test_finalizer_source_hash_binds_the_loaded_file(self):
        self.ps(r'''
if((Get-GuestVmScriptHash) -cne (Get-GuestVmHash ([IO.File]::ReadAllBytes($env:GUEST_VM_SOURCE)))){throw 'finalizer_source_hash_not_own_file'}
''')

    def finalize_roundtrip(self, profile=None):
        with tempfile.TemporaryDirectory() as folder:
            self.ps(r'''
Initialize-GuestVmNative
$root=$env:FIXTURE;$ManifestSha256='1'*64;$FinalizerSourceCommit='a'*40
$spec=[pscustomobject]@{node='A';source_commit=('b'*40);stage_source_commit=('c'*40);seed_source_commit=('d'*40);installation_id=('e'*64);stage_manifest_sha256=('2'*64);seed_iso_sha256=('3'*64)}
$pins=[pscustomobject]@{vm_id='11111111-1111-1111-1111-111111111111';switch_id='22222222-2222-2222-2222-222222222222';source_commit=$spec.source_commit;installation_id=$spec.installation_id;node='A';manifest_sha256=$ManifestSha256;config_path='guest/config'}
$journal=Join-Path $root 'vm-A-journal.jsonl';$prepared=Join-Path $root 'vm-A-prepared-receipt.json'
$events=@('intent','create_private_directory','copy_independent_disk','create_internal_switch','switch_created','create_off_vm','vm_created','configure_new_vm','grant_owned_vm_data_access')
for($i=0;$i -lt $events.Count;$i++){
 $data=@{vm_id=$pins.vm_id;switch_id=$pins.switch_id}
 if($i -eq 0){$data=@{installation_id=$spec.installation_id;source_commit=$spec.source_commit;stage_source_commit=$spec.stage_source_commit;seed_source_commit=$spec.seed_source_commit;node='A';manifest_sha256=$ManifestSha256;stage_manifest_sha256=$spec.stage_manifest_sha256;seed_sha256=$spec.seed_iso_sha256}}
 Write-GuestVmRecord $journal ([ordered]@{sequence=($i+1);event=$events[$i];data=$data}) ($i -eq 0)
}
$original=[IO.File]::ReadAllBytes($journal);$JournalSha256=Get-GuestVmHash $original
function Get-GuestVmFileIdentity{return 'fixture-file-id'}
function Get-GuestVmScriptHash{return ('f'*64)}
function Assert-GuestVmPrepared{return [pscustomobject]@{Id=[Guid]$pins.vm_id}}
function Get-GuestVmSid{return 'S-1-5-83-1-2-3-4-5'}
function Get-GuestVmRootStamp{return [pscustomobject]@{identity='00000001:0000000000000001';sddl_sha256=('4'*64)}}
function Assert-GuestVmSealRootUnchanged{}
function Assert-GuestVmSealInventoryComplete{}
$script:sealRow=[pscustomobject]@{relative_path='guest-A/config';role='config';identity='00000001:0000000000000002';bytes=4;sha256=('5'*64);sddl_sha256=('6'*64)}
$script:disposed=$false
function Open-GuestVmSealInventory{$h=[pscustomobject]@{};$h|Add-Member ScriptMethod Dispose {$script:disposed=$true};return ,@([pscustomobject]@{handle=$h})}
function Invoke-GuestVmSealInventory{Add-GuestVmEvent 'seal_exact_owned_object' @{relative_path=$script:sealRow.relative_path;identity=$script:sealRow.identity;before_sddl_sha256=('7'*64)};return ,@($script:sealRow)}
function Assert-GuestVmSealedObjects{param($Root,$Spec,$Pins,$Objects,$Stamp)if($Objects.Count -ne 1 -or $Objects[0].identity -cne $script:sealRow.identity){throw 'sealed_inventory_lost'}}
$request=[ordered]@{schema_version=1;finalizer_source_commit=$FinalizerSourceCommit;finalizer_script_sha256=('f'*64);original_source_commit=$spec.source_commit;original_manifest_sha256=$ManifestSha256;original_journal_sha256=$JournalSha256;installation_id=$spec.installation_id;node='A';root_identity='00000001:0000000000000001';root_sddl_sha256=('4'*64);pins=$pins;objects=@($script:sealRow)}
if($env:VM_FINALIZE_CONFIG_PROFILE){$request.config_profile=$env:VM_FINALIZE_CONFIG_PROFILE|ConvertFrom-Json;$request.config_profile_sha256=Get-GuestVmConfigProfileHash $request.config_profile}
$requestPath=Join-Path $root 'vm-A-finalize-request.json';Write-GuestVmRecord $requestPath $request $true;$FinalizationRequestSha256=Get-GuestVmHash ([IO.File]::ReadAllBytes($requestPath))
$result=Invoke-GuestVmFinalize $root $spec $null 'guest' 'disk' 'iso' $journal $prepared|ConvertFrom-Json
if(-not $result.prepared -or $result.started -or -not $script:disposed -or $result.original_source_commit -cne $spec.source_commit -or $result.finalizer_source_commit -cne $FinalizerSourceCommit){throw 'finalize_lineage_or_handles_lost'}
if((Get-GuestVmHash ([IO.File]::ReadAllBytes($journal))) -cne $JournalSha256){throw 'original_journal_modified'}
$FinalizationReceiptSha256=$result.finalization_receipt_sha256;$PreparedReceiptSha256=$result.prepared_receipt_sha256
$receipt=Read-GuestVmFinalization $root $spec $journal
if($receipt.pins.source_commit -cne $spec.source_commit){throw 'old_vm_relabelled_as_finalizer'}
$closed=$false;try{Invoke-GuestVmFinalize $root $spec $null 'guest' 'disk' 'iso' $journal $prepared|Out-Null}catch{if($_.Exception.Message -cne 'guest_vm_finalize_existing_partial'){throw};$closed=$true};if(-not $closed){throw 'finalize_replay_accepted'}
$repair=Join-Path $root 'vm-A-finalize-journal.jsonl';Write-GuestVmRecord $repair @{sequence=4;event='forged';data=@{}} $false
$closed=$false;try{$null=Read-GuestVmFinalization $root $spec $journal}catch{$closed=$true};if(-not $closed){throw 'changed_finalize_journal_accepted'}
if((Get-GuestVmHash ([IO.File]::ReadAllBytes($journal))) -cne $JournalSha256){throw 'replay_changed_original_journal'}
''',FIXTURE=folder,VM_FINALIZE_CONFIG_PROFILE=json.dumps(profile) if profile else '')

    def test_finalize_keeps_original_journal_and_requires_all_fresh_receipts_at_start(self):
        self.finalize_roundtrip()

    def test_managed_finalize_keeps_legacy_notes_source_and_separately_pins_profile(self):
        profile=dict(name='windows-managed-config-v1',definition_sha256='61fb12a5c53fa5a76c067f50e6a1ea56a915ebd3d9af86fcb67ff36cd48bd0ef',
            capability_name='vmWorkerProcess',capability_sid='S-1-15-3-1024-2268835264-3721307629-241982045-173645152-1490879176-104643441-2915960892-1612460704',
            host_hypervisor_tcb_acknowledged=True,acceptance_scope='isolated-predeployment',operational_cutover_accepted=False,
            io_acceptance_sha256='',external_capability_review_sha256='',isolation_receipt_sha256='')
        self.finalize_roundtrip(profile)

    def test_schema3_profile_is_explicit_and_digest_scope_bound(self):
        profile=dict(name='windows-managed-config-v1',definition_sha256='61fb12a5c53fa5a76c067f50e6a1ea56a915ebd3d9af86fcb67ff36cd48bd0ef',
            capability_name='vmWorkerProcess',capability_sid='S-1-15-3-1024-2268835264-3721307629-241982045-173645152-1490879176-104643441-2915960892-1612460704',
            host_hypervisor_tcb_acknowledged=True,acceptance_scope='isolated-predeployment',operational_cutover_accepted=False,
            io_acceptance_sha256='',external_capability_review_sha256='',isolation_receipt_sha256='')
        spec=dict(schema_version=3,source_commit='a'*40,stage_source_commit='e'*40,seed_source_commit='f'*40,installation_id='b'*64,
            stage_manifest_sha256='c'*64,seed_iso_sha256='d'*64,seed_iso_bytes=32768,node='A',mac_address='02123456789A',
            optional_egress_nic=False,egress_mac_address='',peer_prepared_receipt_sha256='',creation_method='verified-node-image-copy-v1',
            node_image=dict(filename='node-A.vhdx',sha256='1'*64,bytes=1048576,disk_identifier='7b0a3feb-04cf-5c40-85bd-cfd50b85ab9d',
                source_qcow_sha256='04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94',conversion_receipt_sha256='2'*64,tool_version='qemu-img version 11.1.3',tool_sha256='3'*64),
            config_profile=profile,peer_config_profile_receipt_sha256='',peer_source_commit='')
        result=self.ps(r'''
+$good=$env:VM_MANAGED_SPEC|ConvertFrom-Json;$null=Assert-GuestVmSpec $good
+$raw=[Text.Encoding]::UTF8.GetBytes($env:VM_MANAGED_SPEC);$Manifest=[Convert]::ToBase64String($raw);$ManifestSha256=Get-GuestVmHash $raw;$null=Read-GuestVmSpec
+foreach($field in @('name','definition_sha256','capability_sid','acceptance_scope')){
+ $bad=($good|ConvertTo-Json -Depth 6 -Compress)|ConvertFrom-Json;$bad.config_profile.$field='unknown';$closed=$false;try{$null=Assert-GuestVmSpec $bad}catch{$closed=$true};if(-not $closed){throw 'unknown_config_profile_accepted'}
+}
+$bad=($good|ConvertTo-Json -Depth 6 -Compress)|ConvertFrom-Json;$bad.schema_version=2;$closed=$false;try{$null=Assert-GuestVmSpec $bad}catch{$closed=$true};if(-not $closed){throw 'implicit_legacy_profile_adoption'}
+$good.node='B';$good.node_image.filename='node-B.vhdx';$good.peer_prepared_receipt_sha256='6'*64;$good.peer_source_commit='8'*40;$closed=$false;try{$null=Assert-GuestVmSpec $good}catch{$closed=$true};if(-not $closed){throw 'peer_profile_without_receipt'}
+$good.peer_config_profile_receipt_sha256='7'*64;$null=Assert-GuestVmSpec $good
+Get-GuestVmConfigProfileHash $good.config_profile
+'''.replace('\n+','\n'),VM_MANAGED_SPEC=json.dumps(spec))
        expected=hashlib.sha256(json.dumps(profile,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        self.assertEqual(result,expected)

    def test_managed_role_never_preserves_media_cap_or_adds_missing_config_cap(self):
        self.ps(r'''
Initialize-GuestVmNative
+foreach($managed in @($false,$true)){foreach($directory in @($false,$true)){
 if([HaGuestVmSealV1]::ShareMode($managed,$directory) -ne $(if($directory){3}elseif($managed){7}else{1})){throw 'native_share_mode_mixed'}
}}
foreach($role in @('private','config','disk','seed')){foreach($directory in @($false,$true)){
 $target=[pscustomobject]@{role=$role;directory=$directory}
 if((Test-GuestVmManagedConfigTarget $target ([pscustomobject]@{})) -ne ($role -ceq 'config' -and -not $directory) -or (Test-GuestVmManagedConfigTarget $target $null)){throw 'media_inherited_config_sharing'}
}}
$vm='S-1-5-83-1-2-3-4-5';$cap='S-1-15-3-1024-2268835264-3721307629-241982045-173645152-1490879176-104643441-2915960892-1612460704'
+$cfg=[HaGuestVmSealV1]::ManagedPolicy($vm,'config',$cap,$true)
+if($cfg.Count -ne 3 -or $cfg[2] -cne ($cap+'|2032127') -or @($cfg|Where-Object{$_.StartsWith($vm+'|')}).Count){throw 'config_granted_worker_authority'}
+if([HaGuestVmSealV1]::ManagedPolicy($vm,'config',$cap,$false).Count -ne 2){throw 'missing_original_cap_added'}
+foreach($role in @('disk','seed')){
+ $rows=[HaGuestVmSealV1]::ManagedPolicy($vm,$role,$cap,$true)
+ if(@($rows|Where-Object{$_.StartsWith('S-1-15-')}).Count -or $rows[2] -cne ($vm+'|'+$(if($role -ceq 'disk'){'1180063'}else{'1179785'}))){throw 'cap_or_excessive_media_access'}
+}
+$closed=$false;try{$null=[HaGuestVmSealV1]::ManagedPolicy($vm,'config','S-1-15-3-1',$true)}catch{$closed=$true};if(-not $closed){throw 'arbitrary_capability_accepted'}
+'''.replace('\n+','\n'))

    def test_ast_has_only_owned_vm_mutations_and_start_is_separate(self):
        self.ps(r'''
$t=$null;$e=$null;$ast=[Management.Automation.Language.Parser]::ParseFile($env:GUEST_VM_SOURCE,[ref]$t,[ref]$e);if($e.Count){throw 'parse_failed'}
$names=@($ast.FindAll({param($a)$a -is [Management.Automation.Language.CommandAst]},$true)|ForEach-Object{$_.GetCommandName()})
foreach($blocked in @('Remove-VM','Stop-VM','Restart-VM','Set-VMSwitch','Remove-VMSwitch','Connect-VMNetworkAdapter','New-NetNat','Set-NetIPAddress','Restart-Computer','Start-Service','Stop-Service','Set-Service','Set-CimInstance','Invoke-CimMethod','Add-LocalGroupMember')){if($blocked -in $names){throw 'protected_operation'}}
$starts=@($ast.FindAll({param($a)$a -is [Management.Automation.Language.CommandAst] -and $a.GetCommandName() -ceq 'Start-VM'},$true));if($starts.Count -ne 1 -or $starts[0].Extent.Text -cne 'Start-VM -VM $vm -ErrorAction Stop'){throw 'unbounded_start'}
$startBranch=$starts[0].Parent
while($startBranch -and $startBranch -isnot [Management.Automation.Language.IfStatementAst]){$startBranch=$startBranch.Parent}
if(-not $startBranch.Extent.Text.StartsWith("if(`$Mode -ceq 'start')")){throw 'start_in_prepare'}
$switches=@($ast.FindAll({param($a)$a -is [Management.Automation.Language.CommandAst] -and $a.GetCommandName() -ceq 'New-VMSwitch'},$true));if($switches.Count -ne 1){throw 'multiple_switch_creators'}
$branch=$switches[0].Parent;while($branch -and $branch -isnot [Management.Automation.Language.IfStatementAst]){$branch=$branch.Parent}
if(-not $branch.Extent.Text.StartsWith("if(`$spec.node -ceq 'A')")){throw 'b_can_create_switch'}
$peer=$ast.Find({param($a)$a -is [Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -ceq 'Get-GuestVmPeer'},$true)
if(@($peer.FindAll({param($a)$a -is [Management.Automation.Language.CommandAst] -and $a.GetCommandName() -match '^(Set|New|Start|Stop|Grant|Remove|Add)-'},$true)).Count){throw 'peer_mutation'}
$text=[IO.File]::ReadAllText($env:GUEST_VM_SOURCE)
if($text.IndexOf("Write-GuestVmRecord `$request") -gt $text.IndexOf('Start-VM -VM') -or $text.IndexOf("Write-GuestVmRecord `$journal") -gt $text.IndexOf("New-GuestVmPrivateDirectory `$guestRoot")){throw 'intent_after_mutation'}
if(-not $text.Contains('$stream.Flush($true)') -or -not $text.Contains('[IO.FileMode]::CreateNew') -or -not $text.Contains('vm-A-prepared-receipt.json') -or -not $text.Contains('PreparedReceiptSha256')){throw 'durable_pin_missing'}
'graph-only-no-HyperV-proof'
''')



    def test_schema4_executor_lineage_does_not_relabel_image_derivation(self):
        profile=dict(name='windows-managed-config-v1',definition_sha256='61fb12a5c53fa5a76c067f50e6a1ea56a915ebd3d9af86fcb67ff36cd48bd0ef',capability_name='vmWorkerProcess',capability_sid='S-1-15-3-1024-2268835264-3721307629-241982045-173645152-1490879176-104643441-2915960892-1612460704',host_hypervisor_tcb_acknowledged=True,operational_cutover_accepted=False,io_acceptance_sha256='',external_capability_review_sha256='',acceptance_scope='isolated-predeployment',isolation_receipt_sha256='')
        fixture=json.loads(self.derivation_fixture());spec=fixture['spec'];spec.update(schema_version=4,config_profile=profile,peer_config_profile_receipt_sha256='',peer_source_commit='',root_profile='strict-canonical-observed-v1',executor_source_commit='9'*40,executor_script_sha256=hashlib.sha256(SOURCE.read_bytes()).hexdigest(),reservation_receipt_sha256='8'*64,reservation_root_identity='12345678:1234567812345678')
        self.ps(r'''
$v=$env:FIXTURE|ConvertFrom-Json;$spec=Assert-GuestVmSpec $v.spec;Assert-GuestVmImageDerivationReceipt $v.receipt $spec
. (Import-GuestOwnedSupport);Assert-GuestOwnedDeclaration $spec (Get-GuestVmScriptHash)
$spec.source_commit=$spec.executor_source_commit;$closed=$false;try{Assert-GuestVmImageDerivationReceipt $v.receipt $spec}catch{$closed=$true};if(-not $closed){throw 'image_origin_relabelled'}
$spec=$v.spec;$spec.schema_version=3;$closed=$false;try{Assert-GuestVmSpec $spec|Out-Null}catch{$closed=$true};if(-not $closed){throw 'legacy_profile_inferred'}
''',FIXTURE=json.dumps(fixture))

    def test_service_acl_role_masks_have_no_data_or_write_grants(self):
        self.ps(r'''
Initialize-GuestVmNative
foreach($row in @(@('private',$true,131232),@('private',$false,0),@('config',$true,131233),@('config',$false,131200),@('disk',$false,131200),@('seed',$false,131200))){
 $mask=[HaGuestVmSealV1]::ServiceMetadataMask($row[0],$row[1]);if($mask -ne $row[2]){throw 'metadata_role_changed'}
 if(($mask -band 1343029590) -ne 0 -or ($row[0] -in @('disk','seed') -and ($mask -band 1) -ne 0)){throw 'service_gained_data_or_write'}
}
''')


if __name__=='__main__':unittest.main()
