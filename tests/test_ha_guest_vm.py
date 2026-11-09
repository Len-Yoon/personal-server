"""Pure PowerShell contracts and bounded mutation graph; no Windows runtime claim."""
import os
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
$script:port=[pscustomobject]@{Number=1;Path=$names.pipe}
function Get-VM {return $script:vm};function Get-VMSwitch{return $script:sw};function Get-VMProcessor{return $script:cpu};function Get-VMMemory{return $script:mem}
function Get-VMNetworkAdapter{if($null -ne $script:egress){return @($script:nic,$script:egress)};return $script:nic};function Get-VMHardDiskDrive{return $script:hdd};function Get-VMDvdDrive{return $script:dvd};function Get-VHD{return $script:vhd}
function Get-VMFirmware{return $script:fw};function Get-VMComPort{return $script:port};function Get-GuestVmSid{return 'owned-vm-sid'}
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


if __name__=='__main__':unittest.main()
