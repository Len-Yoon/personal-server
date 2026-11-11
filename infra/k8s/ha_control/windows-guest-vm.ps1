param([ValidateSet('prepare','finalize','start')][string]$Mode,[string]$Manifest,[string]$ManifestSha256,[string]$JournalSha256,[string]$PreparedReceiptSha256,[string]$ReaderReadySha256,[string]$ReaderSourceCommit,[string]$FinalizationRequestSha256,[string]$FinalizationReceiptSha256,[string]$FinalizerSourceCommit,[switch]$Go)
# Fresh owned guests only. Explicit finalize preserves prior creation lineage.
# B can reuse only A's receipt-pinned owned Internal switch.
# Every mutation is journaled first. Partial failures are preserved for manual reconciliation.
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$script:guestOwnedSpec=$null;$script:guestOwnedRoot=''
function Import-GuestOwnedSupport([string]$SupportPath=''){
 if(-not $SupportPath){$SupportPath=Join-Path $PSScriptRoot 'guest-owned-profile.ps1'}
 $bytes=[IO.File]::ReadAllBytes($SupportPath)
 if((Get-GuestVmHash $bytes) -cne '437d02933c2800cea4097a79ee14db3052e8c522c15f132a52c7c094d6f84045'){throw 'guest_owned_support_changed'}
 return [scriptblock]::Create([Text.UTF8Encoding]::new($false,$true).GetString($bytes))
}
function Get-GuestVmHash([byte[]]$Bytes){$h=[Security.Cryptography.SHA256]::Create();try{return [BitConverter]::ToString($h.ComputeHash($Bytes)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}}
function Get-GuestVmStreamHash($Stream){$h=[Security.Cryptography.SHA256]::Create();try{return [BitConverter]::ToString($h.ComputeHash($Stream)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}}
function Assert-GuestVmKeys($Value,[string[]]$Keys){if((($Value.PSObject.Properties.Name|Sort-Object -CaseSensitive)-join ',') -cne (($Keys|Sort-Object -CaseSensitive)-join ',')){throw 'guest_vm_shape_rejected'}}
function Assert-GuestVmConfigProfile($Value){
    Assert-GuestVmKeys $Value @('name','definition_sha256','capability_name','capability_sid','host_hypervisor_tcb_acknowledged','operational_cutover_accepted','io_acceptance_sha256','external_capability_review_sha256','acceptance_scope','isolation_receipt_sha256')
    foreach($key in @('name','definition_sha256','capability_name','capability_sid','io_acceptance_sha256','external_capability_review_sha256','acceptance_scope','isolation_receipt_sha256')){if($Value.$key -isnot [string]){throw 'config_profile_rejected'}}
    if($Value.name -cne 'windows-managed-config-v1' -or $Value.definition_sha256 -cne '61fb12a5c53fa5a76c067f50e6a1ea56a915ebd3d9af86fcb67ff36cd48bd0ef' -or
        $Value.capability_name -cne 'vmWorkerProcess' -or $Value.capability_sid -cne 'S-1-15-3-1024-2268835264-3721307629-241982045-173645152-1490879176-104643441-2915960892-1612460704' -or
        $Value.host_hypervisor_tcb_acknowledged -isnot [bool] -or -not $Value.host_hypervisor_tcb_acknowledged -or $Value.operational_cutover_accepted -isnot [bool]){throw 'config_profile_rejected'}
    if($Value.acceptance_scope -cnotin @('isolated-predeployment','production')){throw 'config_acceptance_scope_rejected'}
    foreach($key in @('io_acceptance_sha256','external_capability_review_sha256','isolation_receipt_sha256')){if($Value.$key -isnot [string] -or ($Value.$key -cne '' -and $Value.$key -cnotmatch '^[0-9a-f]{64}$')){throw 'config_acceptance_rejected'}}
    if(($Value.acceptance_scope -ceq 'isolated-predeployment' -and $Value.operational_cutover_accepted) -or
        ($Value.acceptance_scope -ceq 'production' -and ($Value.isolation_receipt_sha256 -cne '' -or ($Value.operational_cutover_accepted -and ($Value.io_acceptance_sha256 -ceq '' -or $Value.external_capability_review_sha256 -ceq ''))))){throw 'config_acceptance_rejected'}
    return $Value
}
function Get-GuestVmConfigProfileHash($Value){
    $null=Assert-GuestVmConfigProfile $Value
    $ordered=[ordered]@{};foreach($key in @('acceptance_scope','capability_name','capability_sid','definition_sha256','external_capability_review_sha256','host_hypervisor_tcb_acknowledged','io_acceptance_sha256','isolation_receipt_sha256','name','operational_cutover_accepted')){$ordered[$key]=$Value.$key}
    return Get-GuestVmHash ([Text.Encoding]::UTF8.GetBytes(($ordered|ConvertTo-Json -Compress)))
}

function ConvertTo-GuestVmDiskIdentifier($Value){
    # Get-VHD returns GUID objects or uppercase strings depending on the native provider.
    # Accept only a nonzero D-format GUID, then bind its value in canonical lowercase.
    if($Value -isnot [string] -and $Value -isnot [Guid]){throw 'guest_vm_disk_identifier_rejected'}
    $text=$Value.ToString();$guid=[Guid]::Empty
    if($text.Length -ne 36 -or $text -cnotmatch '^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$' -or -not [Guid]::TryParseExact($text,'D',[ref]$guid) -or $guid -eq [Guid]::Empty){throw 'guest_vm_disk_identifier_rejected'}
    return $guid.ToString('D')
}
function Test-GuestVmDerivedImageDeclaration($Image){
    foreach($key in @('derivation_profile','upstream_qcow_sha256','derivation_receipt_sha256')){
        if($null -ne $Image.PSObject.Properties[$key]){return $true}
    }
    return $false
}
function Assert-GuestVmNodeImageProfile($Image){
    $keys=@('filename','sha256','bytes','disk_identifier','source_qcow_sha256','conversion_receipt_sha256','tool_version','tool_sha256')
    $derived=Test-GuestVmDerivedImageDeclaration $Image
    if($derived){$keys+=@('derivation_profile','upstream_qcow_sha256','derivation_receipt_sha256')}
    Assert-GuestVmKeys $Image $keys
    if($derived){
        if($Image.derivation_profile -isnot [string] -or $Image.derivation_profile -cne 'cloud-init-output-preseed-v1' -or
            $Image.upstream_qcow_sha256 -isnot [string] -or $Image.upstream_qcow_sha256 -cne '04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94' -or
            $Image.derivation_receipt_sha256 -isnot [string] -or $Image.derivation_receipt_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
            $Image.derivation_receipt_sha256 -ceq ('0'*64) -or $Image.source_qcow_sha256 -ceq $Image.upstream_qcow_sha256){throw 'guest_vm_image_derivation_rejected'}
    }elseif($Image.source_qcow_sha256 -cne '04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94'){throw 'guest_vm_node_image_rejected'}
}
function Assert-GuestVmImageDerivationReceipt($Value,$Spec){
    # This is a source-bound offline derivation receipt, never guest boot or writer authority.
    $image=$Spec.node_image;Assert-GuestVmNodeImageProfile $image
    if(-not (Test-GuestVmDerivedImageDeclaration $image)){throw 'guest_vm_image_derivation_rejected'}
    Assert-GuestVmKeys $Value @('schema_version','format','profile','source_commit','node','upstream_qcow_sha256','derived_qcow_sha256','node_vhdx_sha256','node_vhdx_bytes','node_disk_identifier','patch_path','patch_sha256','patch_bytes','patch_uid','patch_gid','patch_mode','filesystem_check_exit','root_partition_offset','root_partition_bytes','upstream_unchanged','initial_and_userdata_output_match','qemu_img_version','qemu_img_sha256','utility_image_config_digest','docker_context','docker_daemon_identity_sha256','debugfs_version','e2fsck_version')
    foreach($key in @('schema_version','node_vhdx_bytes','patch_bytes','patch_uid','patch_gid','filesystem_check_exit','root_partition_offset','root_partition_bytes')){
        if($Value.$key -isnot [int] -and $Value.$key -isnot [long]){throw 'guest_vm_image_derivation_type_rejected'}
    }
    foreach($key in @('format','profile','source_commit','node','upstream_qcow_sha256','derived_qcow_sha256','node_vhdx_sha256','node_disk_identifier','patch_path','patch_sha256','patch_mode','qemu_img_version','qemu_img_sha256','utility_image_config_digest','docker_context','docker_daemon_identity_sha256','debugfs_version','e2fsck_version')){
        if($Value.$key -isnot [string]){throw 'guest_vm_image_derivation_type_rejected'}
    }
    foreach($key in @('upstream_unchanged','initial_and_userdata_output_match')){
        if($Value.$key -isnot [bool] -or -not $Value.$key){throw 'guest_vm_image_derivation_rejected'}
    }
    if($Value.schema_version -ne 1 -or $Value.format -cne 'ha-guest-node-image-derivation-v1' -or $Value.profile -cne $image.derivation_profile -or
        $Value.source_commit -cne $Spec.source_commit -or $Value.source_commit -cnotmatch '^[0-9a-f]{40}$' -or $Value.node -cne $Spec.node -or
        $Value.upstream_qcow_sha256 -cne $image.upstream_qcow_sha256 -or $Value.derived_qcow_sha256 -cne $image.source_qcow_sha256 -or
        $Value.node_vhdx_sha256 -cne $image.sha256 -or $Value.node_vhdx_bytes -ne $image.bytes -or
        $Value.node_disk_identifier -cne (ConvertTo-GuestVmDiskIdentifier $Value.node_disk_identifier) -or
        $Value.node_disk_identifier -cne (ConvertTo-GuestVmDiskIdentifier $image.disk_identifier) -or
        $Value.patch_path -cne '/etc/cloud/cloud.cfg.d/99zz-n100-ha-initial-output.cfg' -or $Value.patch_bytes -ne 61 -or
        $Value.patch_sha256 -cne '659e515ee99507c6990b06c3a0560a794070514de74a9506e6ee70da20bc77c8' -or
        $Value.patch_uid -ne 0 -or $Value.patch_gid -ne 0 -or $Value.patch_mode -cne '0644' -or $Value.filesystem_check_exit -ne 0 -or
        $Value.root_partition_offset -ne 1074790400 -or $Value.root_partition_bytes -ne 2683289088 -or
        $Value.qemu_img_version -cne $image.tool_version -or $Value.qemu_img_sha256 -cne $image.tool_sha256 -or
        $Value.utility_image_config_digest -cne 'sha256:ded2c932b40c38c99f0b23b9edc29e160e23689db2d92cdbe251b693ca76be1b' -or
        $Value.docker_context -cnotmatch '^colima-codex-ha-[A-Za-z0-9_-]{1,64}$' -or
        $Value.docker_daemon_identity_sha256 -cnotmatch '^[0-9a-f]{64}$' -or $Value.docker_daemon_identity_sha256 -ceq ('0'*64) -or
        $Value.debugfs_version -cnotmatch '^debugfs [0-9]+\.[0-9]+\.[0-9]+(?: \([A-Za-z0-9 -]{1,60}\))?$' -or
        $Value.e2fsck_version -cnotmatch '^e2fsck [0-9]+\.[0-9]+\.[0-9]+(?: \([A-Za-z0-9 -]{1,60}\))?$'){throw 'guest_vm_image_derivation_rejected'}
}
function Assert-GuestVmSpec($Value,[string]$OwnedSupportPath=''){
    $fields=@('schema_version','source_commit','stage_source_commit','seed_source_commit','installation_id','stage_manifest_sha256','seed_iso_sha256','seed_iso_bytes','node','mac_address','optional_egress_nic','egress_mac_address','peer_prepared_receipt_sha256')
    if(($Value.schema_version -isnot [int] -and $Value.schema_version -isnot [long]) -or $Value.schema_version -notin @(1,2,3,4)){throw 'guest_vm_identity_rejected'}
    if($Value.schema_version -ge 2){$fields+=@('creation_method','node_image')}
    if($Value.schema_version -ge 3){$fields+=@('config_profile','peer_config_profile_receipt_sha256','peer_source_commit')}
    if($Value.schema_version -eq 4){. (Import-GuestOwnedSupport $OwnedSupportPath);$fields+=@(Get-GuestOwnedFields);Assert-GuestOwnedDeclaration $Value ''}
    $operationFields=@('vm_operation_source_commit','running_firmware_sha256','running_peer_firmware_sha256')
    if($Value.schema_version -eq 4 -and @($operationFields|Where-Object{$null -ne $Value.PSObject.Properties[$_]}).Count){
        $fields+=$operationFields
        Assert-GuestVmKeys $Value $fields
        if($Value.node -cne 'B' -or $Value.vm_operation_source_commit -isnot [string] -or
            $Value.vm_operation_source_commit -cnotmatch '^[0-9a-f]{40}$' -or $Value.vm_operation_source_commit -ceq $Value.executor_source_commit){throw 'guest_vm_operation_source_rejected'}
        foreach($key in @('running_firmware_sha256','running_peer_firmware_sha256')){
            if($Value.$key -isnot [string] -or $Value.$key -cnotmatch '^[0-9a-f]{64}$' -or $Value.$key -ceq ('0'*64)){throw 'guest_vm_firmware_pin_rejected'}
        }
    }
    Assert-GuestVmKeys $Value $fields
    if(
        $Value.source_commit -isnot [string] -or $Value.source_commit -cnotmatch '^[0-9a-f]{40}$' -or
        $Value.stage_source_commit -isnot [string] -or $Value.stage_source_commit -cnotmatch '^[0-9a-f]{40}$' -or $Value.seed_source_commit -isnot [string] -or $Value.seed_source_commit -cnotmatch '^[0-9a-f]{40}$' -or
        $Value.installation_id -isnot [string] -or $Value.installation_id -cnotmatch '^[0-9a-f]{64}$'){throw 'guest_vm_identity_rejected'}
    foreach($key in @('stage_manifest_sha256','seed_iso_sha256')){if($Value.$key -isnot [string] -or $Value.$key -cnotmatch '^[0-9a-f]{64}$'){throw 'guest_vm_hash_rejected'}}
    if(($Value.seed_iso_bytes -isnot [int] -and $Value.seed_iso_bytes -isnot [long]) -or $Value.seed_iso_bytes -lt 32768 -or $Value.seed_iso_bytes -gt 2147483648 -or $Value.optional_egress_nic -isnot [bool]){throw 'guest_vm_size_rejected'}
    if($Value.node -isnot [string] -or $Value.node -cnotin @('A','B')){throw 'guest_vm_node_rejected'}
    if($Value.mac_address -isnot [string] -or $Value.mac_address -cnotmatch '^02[0-9A-F]{10}$'){throw 'guest_vm_mac_rejected'}
    if($Value.egress_mac_address -isnot [string] -or ($Value.optional_egress_nic -and ($Value.egress_mac_address -cnotmatch '^02[0-9A-F]{10}$' -or $Value.egress_mac_address -ceq $Value.mac_address)) -or (-not $Value.optional_egress_nic -and $Value.egress_mac_address -cne '')){throw 'guest_vm_egress_mac_rejected'}
    if($Value.peer_prepared_receipt_sha256 -isnot [string] -or ($Value.node -ceq 'A' -and $Value.peer_prepared_receipt_sha256 -cne '') -or ($Value.node -ceq 'B' -and $Value.peer_prepared_receipt_sha256 -cnotmatch '^[0-9a-f]{64}$')){throw 'guest_vm_peer_pin_required'}
    if($Value.schema_version -ge 2){
        if($Value.creation_method -isnot [string] -or $Value.creation_method -cne 'verified-node-image-copy-v1'){throw 'guest_vm_creation_method_rejected'}
        $image=$Value.node_image
        Assert-GuestVmNodeImageProfile $image
        foreach($key in @('sha256','source_qcow_sha256','conversion_receipt_sha256','tool_sha256')){if($image.$key -isnot [string] -or $image.$key -cnotmatch '^[0-9a-f]{64}$'){throw 'guest_vm_node_image_hash_rejected'}}
        if($image.filename -isnot [string] -or $image.filename -cne ('node-'+$Value.node+'.vhdx') -or ($image.bytes -isnot [int] -and $image.bytes -isnot [long]) -or $image.bytes -lt 1048576 -or $image.bytes -gt 8589934592 -or
            $image.disk_identifier -isnot [string] -or
            $image.tool_version -isnot [string] -or $image.tool_version -cnotmatch '^qemu-img version [0-9]+\.[0-9]+\.[0-9]+$'){throw 'guest_vm_node_image_rejected'}
        $null=ConvertTo-GuestVmDiskIdentifier $image.disk_identifier
    }
    if($Value.schema_version -ge 3){
        $null=Assert-GuestVmConfigProfile $Value.config_profile
        if($Value.peer_source_commit -isnot [string] -or ($Value.node -ceq 'A' -and $Value.peer_source_commit -cne '') -or ($Value.node -ceq 'B' -and $Value.peer_source_commit -cnotmatch '^[0-9a-f]{40}$')){throw 'guest_vm_peer_source_required'}
        if($Value.peer_config_profile_receipt_sha256 -isnot [string] -or ($Value.node -ceq 'A' -and $Value.peer_config_profile_receipt_sha256 -cne '') -or ($Value.node -ceq 'B' -and $Value.peer_config_profile_receipt_sha256 -cnotmatch '^[0-9a-f]{64}$')){throw 'guest_vm_peer_config_profile_required'}
    }
    return $Value
}
function Read-GuestVmSpec([string]$OwnedSupportPath=''){
    if($Manifest -isnot [string] -or $Manifest.Length -gt 32768 -or $ManifestSha256 -cnotmatch '^[0-9a-f]{64}$'){throw 'guest_vm_manifest_pin_required'}
    $bytes=[Convert]::FromBase64String($Manifest)
    $text=([Text.UTF8Encoding]::new($false,$true)).GetString($bytes)
    # Every accepted key/value alphabet is escape-free. Reject before JSON decoding
    # so Unicode-escaped duplicate keys cannot bypass the literal key inventory.
    if($text.Contains('\')){throw 'guest_vm_duplicate_or_escaped_keys'}
    $keys=@([regex]::Matches($text,'"([A-Za-z_][A-Za-z0-9_]*)"\s*:')|ForEach-Object{$_.Groups[1].Value})
    if($keys.Count -notin @(13,23,26,36,39,41,44,47) -or @($keys|Select-Object -Unique).Count -ne $keys.Count){throw 'guest_vm_duplicate_or_escaped_keys'}
    if((Get-GuestVmHash $bytes) -cne $ManifestSha256){throw 'guest_vm_manifest_changed'}
    return Assert-GuestVmSpec ($text|ConvertFrom-Json) $OwnedSupportPath
}
function Get-GuestVmNames($Spec){
    $prefix='N100HA_'+$Spec.installation_id.Substring(0,16)
    return [pscustomobject]@{vm=($prefix+'_'+$Spec.node);switch=($prefix+'_data');pipe=('\\.\pipe\'+$prefix+'_'+$Spec.node+'_com1')}
}
function Get-GuestVmParent($Entry){
    # Parent returns a raw CLR object; provider-added PSIsContainer is absent.
    if($Entry -is [IO.DirectoryInfo]){return $Entry.Parent}
    if($Entry -is [IO.FileInfo]){return $Entry.Directory}
    throw 'guest_vm_path_type_rejected'
}
function Assert-GuestVmPath([string]$Path){
    if($Path -cnotmatch '^[A-Za-z]:\\' -or $Path.Substring(2).Contains(':') -or $Path -match '(^|\\)\.\.?($|\\)' -or $Path -match '[ .](\\|$)' -or $Path.Contains('/')){throw 'guest_vm_path_rejected'}
    $entry=Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    while($null -ne $entry){
        if($entry.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'guest_vm_reparse_rejected'}
        $entry=Get-GuestVmParent $entry
    }
}
function Get-GuestVmServiceMetadataMask([string]$Path){
 if($null -eq $script:guestOwnedSpec){return 0}
 foreach($node in @('A','B')){
 $guest=Join-Path $script:guestOwnedRoot ('guest-'+$node)
 $config=Join-Path $guest ('N100HA_'+$script:guestOwnedSpec.installation_id.Substring(0,16)+'_'+$node)
 if($Path -ieq $guest){return 131232}
 if($Path -ieq (Join-Path $guest ('guest-'+$node+'.vhdx')) -or $Path -ieq (Join-Path $script:guestOwnedRoot ('seed-'+$node+'.iso'))){return 131200}
 if($Path -ieq $config -or $Path.StartsWith($config+'\',[StringComparison]::OrdinalIgnoreCase)){
  if((Get-Item -LiteralPath $Path -Force) -is [IO.DirectoryInfo]){return 131233};return 131200
 }
 }

 return 0
}
function Assert-GuestVmPrivate([string]$Path,[string]$VmSid='',[bool]$VmWriter=$false){
    if($null -ne $script:guestOwnedSpec -and $Path -ieq $script:guestOwnedRoot){Assert-GuestVmPath $Path;Assert-GuestOwnedRootAcl $Path $script:guestOwnedSpec;return}
    Assert-GuestVmPath $Path
    $sd=[Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $Path).Sddl)
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent();try{$allowed=@('S-1-5-18','S-1-5-32-544',$identity.User.Value)}finally{$identity.Dispose()}
    if($sd.Owner.Value -notin $allowed -or $null -eq $sd.DiscretionaryAcl -or $sd.DiscretionaryAcl.Count -eq 0){throw 'guest_vm_private_owner_required'}
    foreach($ace in $sd.DiscretionaryAcl){
        if($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback){throw 'guest_vm_private_acl_required'}
        if($null -ne $script:guestOwnedSpec -and $ace.SecurityIdentifier.Value -ceq (Get-GuestOwnedServiceSid $script:guestOwnedSpec.installation_id)){
            $mask=Get-GuestVmServiceMetadataMask $Path
            if($mask -eq 0 -or $ace.AceQualifier -ne 'AccessAllowed' -or $ace.AccessMask -ne $mask -or [int]$ace.AceFlags -ne 0){throw 'guest_vm_service_metadata_acl_rejected'};continue
        }
        if($ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed -and $ace.SecurityIdentifier.Value -notin $allowed -and $ace.AccessMask -ne 0){
            if(-not $VmSid -or $ace.SecurityIdentifier.Value -cne $VmSid -or (-not $VmWriter -and (([long]$ace.AccessMask -band 4294967295) -band (4294967295 -bxor 1179785)) -ne 0)){throw 'guest_vm_private_acl_required'}
        }
    }
}
function Initialize-GuestVmNative{
    if('HaGuestVmFileV1' -as [type]){throw 'guest_vm_native_type_collision'}
    Add-Type -TypeDefinition @'
using System;using System.Runtime.InteropServices;using Microsoft.Win32.SafeHandles;using System.Collections.Generic;using System.Text;
public static class HaGuestVmFileV1 {
 [StructLayout(LayoutKind.Sequential)] struct Info {public uint Attr;public System.Runtime.InteropServices.ComTypes.FILETIME Created,Accessed,Written;public uint Volume,High,Low,Links,IndexHigh,IndexLow;}
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern SafeFileHandle CreateFile(string p,uint a,uint s,IntPtr q,uint d,uint f,IntPtr t);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetFileInformationByHandle(SafeFileHandle h,out Info i);
 [DllImport("kernel32.dll",SetLastError=true)] static extern IntPtr OpenProcess(uint access,bool inherit,int pid);
 [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
 [DllImport("advapi32.dll",SetLastError=true)] static extern bool OpenProcessToken(IntPtr process,uint access,out IntPtr token);
 [DllImport("advapi32.dll",SetLastError=true)] static extern bool GetTokenInformation(IntPtr token,int kind,IntPtr data,int length,out int needed);
 public static string ProcessOwner(int pid){IntPtr process=OpenProcess(0x1000,false,pid);if(process==IntPtr.Zero)throw new InvalidOperationException();try{IntPtr token;if(!OpenProcessToken(process,8,out token))throw new InvalidOperationException();try{int size;GetTokenInformation(token,1,IntPtr.Zero,0,out size);if(size<IntPtr.Size||size>4096)throw new InvalidOperationException();IntPtr data=Marshal.AllocHGlobal(size);try{if(!GetTokenInformation(token,1,data,size,out size))throw new InvalidOperationException();return new System.Security.Principal.SecurityIdentifier(Marshal.ReadIntPtr(data)).Value;}finally{Marshal.FreeHGlobal(data);}}finally{CloseHandle(token);}}finally{CloseHandle(process);}}
 public static string Identity(string path){return ReadIdentity(path,1);}
 public static string SharedIdentity(string path){return ReadIdentity(path,7);}
 static string ReadIdentity(string path,uint share){using(var h=CreateFile(path,0,share,IntPtr.Zero,3,0x00200000,IntPtr.Zero)){Info i;if(h.IsInvalid||!GetFileInformationByHandle(h,out i)||(i.Attr&0x400)!=0||i.Links!=1)throw new InvalidOperationException();return i.Volume.ToString("x8")+":"+i.IndexHigh.ToString("x8")+i.IndexLow.ToString("x8");}}
}
public sealed class HaGuestVmSealV1 : IDisposable {
 [StructLayout(LayoutKind.Sequential)] struct Info {public uint Attr;public System.Runtime.InteropServices.ComTypes.FILETIME Created,Accessed,Written;public uint Volume,High,Low,Links,IndexHigh,IndexLow;}
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern SafeFileHandle CreateFile(string p,uint a,uint s,IntPtr q,uint d,uint f,IntPtr t);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetFileInformationByHandle(SafeFileHandle h,out Info i);
 [DllImport("advapi32.dll")] static extern uint GetSecurityInfo(SafeFileHandle h,int type,uint flags,out IntPtr owner,out IntPtr group,out IntPtr dacl,out IntPtr sacl,out IntPtr sd);
 [DllImport("advapi32.dll")] static extern uint SetSecurityInfo(SafeFileHandle h,int type,uint flags,IntPtr owner,IntPtr group,IntPtr dacl,IntPtr sacl);
 [DllImport("advapi32.dll")] static extern uint GetSecurityDescriptorLength(IntPtr sd);
 [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr p);
 [DllImport("kernelbase.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool DeriveCapabilitySidsFromName(string name,out IntPtr groups,out uint groupCount,out IntPtr caps,out uint capCount);
 static void FreeCapabilityArray(IntPtr array,uint count){if(array==IntPtr.Zero)return;if(count<=16)for(uint i=0;i<count;i++){IntPtr p=Marshal.ReadIntPtr(array,(int)i*IntPtr.Size);if(p!=IntPtr.Zero)LocalFree(p);}LocalFree(array);}
 public static string VmWorkerCapability(){
  IntPtr groups=IntPtr.Zero,caps=IntPtr.Zero;uint ng=0,nc=0;
  try{if(!DeriveCapabilitySidsFromName("vmWorkerProcess",out groups,out ng,out caps,out nc)||ng>16||nc!=1)throw new InvalidOperationException("config_capability_derivation_failed");return new System.Security.Principal.SecurityIdentifier(Marshal.ReadIntPtr(caps)).Value;}finally{FreeCapabilityArray(groups,ng);FreeCapabilityArray(caps,nc);}
 }
 SafeFileHandle handle;System.IO.FileStream stream;bool directory;bool disposed;bool managedProfile;bool preserveCapability;string capabilitySid;System.Security.AccessControl.InheritanceFlags capabilityInheritance;
 static void Need(bool ok){if(!ok)throw new InvalidOperationException("guest_vm_seal_rejected");}
 static string Hex(byte[] b){return BitConverter.ToString(b).Replace("-","").ToLowerInvariant();}
 static string HashBytes(byte[] b){using(var h=System.Security.Cryptography.SHA256.Create())return Hex(h.ComputeHash(b));}
 public HaGuestVmSealV1(string path,bool isDirectory):this(path,isDirectory,false){}
 public HaGuestVmSealV1(string path,bool isDirectory,bool managedConfig){
  directory=isDirectory;
  // Media: retained READ_DATA|WRITE_DAC + shareRead denies data writers/replacement.
  // Directories: shareRead|shareWrite permits trusted child receipt creation/rename;
  // no shareDelete keeps the leased directory from replacement. ACLs still gate writers.
  // Managed config files only: share7 coexists with HostHypervisorTCB writers;
  // this is not a proof of foreign-writer absence. Never force-close handles.
  handle=CreateFile(path,0x80040000,ShareMode(managedConfig,directory),IntPtr.Zero,3,0x02200000,IntPtr.Zero);
  if(handle.IsInvalid){int error=Marshal.GetLastWin32Error();handle.Dispose();throw new InvalidOperationException("guest_vm_seal_open_"+error);}
  try{Info i=ReadInfo();Need(((i.Attr&0x10)!=0)==directory);if(!directory)stream=new System.IO.FileStream(handle,System.IO.FileAccess.Read,1048576,false);}catch{Dispose();throw;}
 }
 public static uint ShareMode(bool managedConfig,bool isDirectory){return isDirectory?3u:(managedConfig?7u:1u);}
 Info ReadInfo(){Need(!disposed);Info i;Need(GetFileInformationByHandle(handle,out i)&&(i.Attr&0x400)==0&&i.Links==1);return i;}
 public string Identity(){Info i=ReadInfo();return i.Volume.ToString("x8")+":"+i.IndexHigh.ToString("x8")+i.IndexLow.ToString("x8");}
 public long Bytes(){Info i=ReadInfo();return directory?0:(long)(((ulong)i.High<<32)|i.Low);}
 public string Hash(){if(directory)return "";stream.Position=0;try{using(var h=System.Security.Cryptography.SHA256.Create())return Hex(h.ComputeHash(stream));}finally{stream.Position=0;}}
 System.Security.AccessControl.RawSecurityDescriptor Descriptor(){
  IntPtr o,g,d,s,sd;uint error=GetSecurityInfo(handle,1,7,out o,out g,out d,out s,out sd);if(error!=0)throw new InvalidOperationException("guest_vm_seal_security_"+error);
  try{uint size=GetSecurityDescriptorLength(sd);Need(size>0&&size<=65536);byte[] raw=new byte[size];Marshal.Copy(sd,raw,0,(int)size);return new System.Security.AccessControl.RawSecurityDescriptor(raw,0);}finally{LocalFree(sd);}
 }
 public string SddlHash(){return HashBytes(System.Text.Encoding.UTF8.GetBytes(Descriptor().GetSddlForm(System.Security.AccessControl.AccessControlSections.All)));}
 public string Owner(){return Descriptor().Owner.Value;}
 public bool HasUnverifiedCapability(){
  var sd=Descriptor();if(sd.DiscretionaryAcl==null)return false;
  foreach(System.Security.AccessControl.GenericAce item in sd.DiscretionaryAcl){var ace=item as System.Security.AccessControl.KnownAce;if(ace!=null&&ace.SecurityIdentifier.Value.StartsWith("S-1-15-",StringComparison.Ordinal))return true;}
  return false;
 }
 public static bool CapabilityRuleAllowed(string role,string sid,string expectedSid,int mask,int flags,bool allow,bool callback,bool isDirectory){
  if(role!="private"&&role!="config"&&role!="disk"&&role!="seed")return false;
  if(sid!=expectedSid||!allow||callback||flags<0||(flags&~31)!=0||(!isDirectory&&(flags&15)!=0))return false;
  // Exact observed Windows child-only template. Never accept effective
  // GENERIC_ALL, a template on a file, or a template on data-media roles.
  if(mask==268435456)return isDirectory&&(role=="private"||role=="config")&&(flags==11||flags==27);
  if(role=="config")return mask==2032127&&(flags&12)==0;
  // Known Windows capability on an owned parent/media is removal input only.
  // It is never retained by ManagedPolicy for these roles.
  return mask==2032127||mask==1180063||mask==1179785;
 }
 public static int ConfigurationCapabilityPlan(int[] masks,int[] flags,bool isDirectory){
  Need(masks!=null&&flags!=null&&masks.Length==flags.Length&&masks.Length<=128);
  bool effective=false,template=false;int inheritance=0;
  for(int i=0;i<masks.Length;i++){
   Need(CapabilityRuleAllowed("config","verified","verified",masks[i],flags[i],true,false,isDirectory));
   if(masks[i]==268435456)template=true;else effective=true;
   inheritance|=flags[i]&3;
  }
  // An inherited child-only ACE alone must not acquire parent authority.
  Need(!template||effective);return effective?inheritance:-1;
 }
 string observedService="";
 public void BindObservedService(string sid){Need(sid!=null&&System.Text.RegularExpressions.Regex.IsMatch(sid,"\\AS-1-5-80-(?:[0-9]+-){4}[0-9]+\\z"));observedService=sid;}
 public static int ServiceMetadataMask(string role,bool directory){
  Need(role=="private"||role=="config"||role=="disk"||role=="seed");
  if(role=="private")return directory?131232:0;
  if(role=="config")return directory?131233:131200;
  return directory?0:131200; // Media metadata only: no READ_DATA or write grant.
 }
 public void BindProfile(string name,string expectedSid,string role){
  Need(name=="windows-managed-config-v1"&&expectedSid==VmWorkerCapability()&&(role=="private"||role=="config"||role=="disk"||role=="seed"));
  managedProfile=false;capabilitySid=expectedSid;preserveCapability=false;capabilityInheritance=System.Security.AccessControl.InheritanceFlags.None;
  var configMasks=new System.Collections.Generic.List<int>();var configFlags=new System.Collections.Generic.List<int>();
  var sd=Descriptor();Need(sd.DiscretionaryAcl!=null);
  foreach(System.Security.AccessControl.GenericAce item in sd.DiscretionaryAcl){var ace=item as System.Security.AccessControl.KnownAce;if(ace==null)throw new InvalidOperationException("config_ace_rejected");
   if(ace.SecurityIdentifier.Value.StartsWith("S-1-15-",StringComparison.Ordinal)){
    var common=ace as System.Security.AccessControl.CommonAce;
    Need(common!=null&&CapabilityRuleAllowed(role,ace.SecurityIdentifier.Value,expectedSid,ace.AccessMask,(int)common.AceFlags,common.AceQualifier==System.Security.AccessControl.AceQualifier.AccessAllowed,common.IsCallback,directory));
    if(role=="config"){
     configMasks.Add(ace.AccessMask);configFlags.Add((int)common.AceFlags);
    }
   }
  }
  if(role=="config"){int plan=ConfigurationCapabilityPlan(configMasks.ToArray(),configFlags.ToArray(),directory);preserveCapability=plan>=0;capabilityInheritance=(System.Security.AccessControl.InheritanceFlags)(plan<0?0:plan);}
  managedProfile=true;
 }
 public static string[] Policy(string vmSid,string role){
  Need(role=="private"||role=="config"||role=="disk"||role=="seed");
  if(role=="private")return new string[]{"S-1-5-32-544|2032127","S-1-5-18|2032127"};
  Need(vmSid!=null&&System.Text.RegularExpressions.Regex.IsMatch(vmSid,"\\AS-1-5-83-(?:[0-9]+-){4}[0-9]+\\z"));
  return new string[]{"S-1-5-32-544|2032127","S-1-5-18|2032127",vmSid+"|"+(role=="seed"?"1179785":role=="disk"?"1180063":"2032127")};
 }
 public static string[] ManagedPolicy(string vmSid,string role,string cap,bool existed){
  Need(cap=="S-1-15-3-1024-2268835264-3721307629-241982045-173645152-1490879176-104643441-2915960892-1612460704");
  if(role=="config")return existed?new string[]{"S-1-5-32-544|2032127","S-1-5-18|2032127",cap+"|2032127"}:Policy("","private");
  return Policy(vmSid,role);
 }
 System.Security.AccessControl.DiscretionaryAcl Expected(string vmSid,string role,bool dir){
  var d=new System.Security.AccessControl.DiscretionaryAcl(dir,false,3);
  var inherit=dir?(System.Security.AccessControl.InheritanceFlags.ContainerInherit|System.Security.AccessControl.InheritanceFlags.ObjectInherit):System.Security.AccessControl.InheritanceFlags.None;
  string[] rows=Policy(vmSid,role);
  if(managedProfile)rows=ManagedPolicy(vmSid,role,capabilitySid,preserveCapability);
  foreach(string row in rows){string[] part=row.Split('|');d.AddAccess(System.Security.AccessControl.AccessControlType.Allow,new System.Security.Principal.SecurityIdentifier(part[0]),Int32.Parse(part[1],System.Globalization.CultureInfo.InvariantCulture),(managedProfile&&role=="config"&&part[0]==capabilitySid)?capabilityInheritance:inherit,System.Security.AccessControl.PropagationFlags.None);}
  if(observedService!=""){int mask=ServiceMetadataMask(role,dir);if(mask!=0)d.AddAccess(System.Security.AccessControl.AccessControlType.Allow,new System.Security.Principal.SecurityIdentifier(observedService),mask,System.Security.AccessControl.InheritanceFlags.None,System.Security.AccessControl.PropagationFlags.None);}
  return d;
 }
 public bool Canonical(string vmSid,string role){
  var sd=Descriptor();if((sd.ControlFlags&System.Security.AccessControl.ControlFlags.DiscretionaryAclProtected)==0||sd.DiscretionaryAcl==null)return false;
  var expected=Expected(vmSid,role,directory);if(sd.DiscretionaryAcl.Count!=expected.Count)return false;
  for(int i=0;i<expected.Count;i++){
   var want=(System.Security.AccessControl.CommonAce)expected[i];bool found=false;
   foreach(System.Security.AccessControl.GenericAce a in sd.DiscretionaryAcl){var ace=a as System.Security.AccessControl.CommonAce;if(ace!=null&&!ace.IsCallback&&ace.AceQualifier==want.AceQualifier&&ace.AceFlags==want.AceFlags&&ace.AccessMask==want.AccessMask&&ace.SecurityIdentifier==want.SecurityIdentifier)found=true;}
   if(!found)return false;
  }return true;
 }
 public void Seal(string vmSid,string role){
  // Strict mode never removes a capability. The explicit managed profile
  // binds the native OS feature before config-only preservation/media removal.
  Need(managedProfile||!HasUnverifiedCapability());
  var d=Expected(vmSid,role,directory);byte[] raw=new byte[d.BinaryLength];d.GetBinaryForm(raw,0);IntPtr acl=Marshal.AllocHGlobal(raw.Length);
  try{Marshal.Copy(raw,0,acl,raw.Length);uint error=SetSecurityInfo(handle,1,0x80000004,IntPtr.Zero,IntPtr.Zero,acl,IntPtr.Zero);if(error!=0)throw new InvalidOperationException("guest_vm_seal_set_"+error);}finally{Marshal.FreeHGlobal(acl);}
  Need(Canonical(vmSid,role));
 }
 public void Dispose(){if(disposed)return;disposed=true;if(stream!=null)stream.Dispose();if(handle!=null)handle.Dispose();}
}
 public static class HaGuestVmJsonV1 {
  sealed class Parser {
   readonly string text; int at; int nodes;
   public Parser(string s){text=s;}
   void White(){while(at<text.Length&&(text[at]==' '||text[at]=='\r'||text[at]=='\n'||text[at]=='\t'))at++;}
   void Need(bool ok){if(!ok)throw new InvalidOperationException("guest_vm_json_rejected");}
   string String(){
    Need(at<text.Length&&text[at++]=='"');var b=new StringBuilder();bool done=false;
    while(at<text.Length){char c=text[at++];if(c=='"'){done=true;break;}
     if(c=='\\'){Need(at<text.Length);c=text[at++];if(c=='u'){Need(at+4<=text.Length);int value=0;
       for(int i=0;i<4;i++){char h=text[at++];int n=h>='0'&&h<='9'?h-'0':h>='a'&&h<='f'?h-'a'+10:h>='A'&&h<='F'?h-'A'+10:-1;Need(n>=0);value=value*16+n;}c=(char)value;
      }else Need(c=='"'||c=='\\'||c=='/');}
     Need(c>=32&&c<=126);b.Append(c);Need(b.Length<=4096);
    }Need(done);return b.ToString();
   }
   object Value(int depth){
    Need(depth<=8&&++nodes<=65536);White();Need(at<text.Length);char c=text[at];
    if(c=='"')return String();
    if(c=='{'){at++;White();var result=new Dictionary<string,object>(StringComparer.OrdinalIgnoreCase);
     if(at<text.Length&&text[at]=='}'){at++;return result;}
     while(true){White();string key=String();Need(key.Length>0&&key.Length<=200&&!result.ContainsKey(key));White();Need(at<text.Length&&text[at++]==':');result.Add(key,Value(depth+1));White();Need(at<text.Length);char end=text[at++];if(end=='}')return result;Need(end==',');}
    }
    if(c=='['){at++;White();var values=new List<object>();if(at<text.Length&&text[at]==']'){at++;return values;}
     while(true){Need(values.Count<8192);values.Add(Value(depth+1));White();Need(at<text.Length);char end=text[at++];if(end==']')return values;Need(end==',');}}
    foreach(string word in new string[]{"true","false","null"})if(at+word.Length<=text.Length&&System.String.CompareOrdinal(text,at,word,0,word.Length)==0){at+=word.Length;return word=="null"?null:(object)(word=="true");}
    int start=at;if(c=='-')at++;Need(at<text.Length&&text[at]>='0'&&text[at]<='9');
    if(text[at]=='0')at++;else while(at<text.Length&&text[at]>='0'&&text[at]<='9')at++;
    long integer;Need(Int64.TryParse(text.Substring(start,at-start),System.Globalization.NumberStyles.AllowLeadingSign,System.Globalization.CultureInfo.InvariantCulture,out integer));return integer;
   }
   public object Parse(){object value=Value(0);White();Need(at==text.Length);return value;}
  }
  public static Dictionary<string,object> Object(string text,int maximum){
   if(text==null||text.Length>maximum||Encoding.UTF8.GetByteCount(text)>maximum)throw new InvalidOperationException("guest_vm_json_size");
   object value=new Parser(text).Parse();var result=value as Dictionary<string,object>;if(result==null)throw new InvalidOperationException("guest_vm_json_object_required");return result;
  }
 }

'@ -ErrorAction Stop
}
function Get-GuestVmSid([string]$Id){return ([Security.Principal.NTAccount]::new('NT VIRTUAL MACHINE\'+$Id)).Translate([Security.Principal.SecurityIdentifier]).Value}
function Get-GuestVmReaderOwner([int]$Id){return [HaGuestVmFileV1]::ProcessOwner($Id)}
function Get-GuestVmCurrentSid{$identity=[Security.Principal.WindowsIdentity]::GetCurrent();try{return $identity.User.Value}finally{$identity.Dispose()}}
function Get-GuestVmReaderImagePath{return Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'}
function Grant-GuestVmDataAccess([string]$Path,[string]$VmSid,[bool]$Writer){
    Assert-GuestVmPrivate $Path $VmSid $Writer
    $acl=Get-Acl -LiteralPath $Path;$rights=if($Writer){'FullControl'}else{'Read'}
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($VmSid),$rights,'Allow'))
    Set-Acl -LiteralPath $Path -AclObject $acl
    Assert-GuestVmPrivate $Path $VmSid $Writer
}
function Get-GuestVmFileIdentity([string]$Path,[string]$VmSid='',[bool]$VmWriter=$false,[bool]$SharedMetadata=$false){Assert-GuestVmPrivate $Path $VmSid $VmWriter;if($SharedMetadata){return [HaGuestVmFileV1]::SharedIdentity($Path)};return [HaGuestVmFileV1]::Identity($Path)}
function Open-GuestVmPinned([string]$Path,[string]$Hash,[long]$Size,[string]$VmSid='',[bool]$VmWriter=$false){
    if($Hash -cnotmatch '^[0-9a-f]{64}$'){throw 'guest_vm_file_hash_required'}
    $identity=Get-GuestVmFileIdentity $Path $VmSid $VmWriter
    $stream=[IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    try{if(($Size -ge 0 -and $stream.Length -ne $Size) -or (Get-GuestVmStreamHash $stream) -cne $Hash -or (Get-GuestVmFileIdentity $Path $VmSid $VmWriter) -cne $identity){throw 'guest_vm_file_pin_changed'};$stream.Position=0;return $stream}catch{$stream.Dispose();throw}
}
function Open-GuestVmNodeImage([string]$Root,$Spec){
    # The manifest pins both the transported image and its conversion evidence.
    # Unlike schema1's generic base clone, this image already has a node-specific VPD83 ID.
    $image=$Spec.node_image;$path=Join-Path $Root $image.filename
    $receipt=Open-GuestVmPinned (Join-Path $Root ('node-'+$Spec.node+'-conversion-receipt.json')) $image.conversion_receipt_sha256 -1
    try{if($receipt.Length -lt 2 -or $receipt.Length -gt 32768){throw 'guest_vm_conversion_receipt_limit'}}finally{$receipt.Dispose()}
    if(Test-GuestVmDerivedImageDeclaration $image){
        $derivation=Read-GuestVmJsonPinned (Join-Path $Root ('node-'+$Spec.node+'-image-derivation.json')) $image.derivation_receipt_sha256 32768
        Assert-GuestVmImageDerivationReceipt $derivation $Spec
    }
    $stream=Open-GuestVmPinned $path $image.sha256 $image.bytes
    try{
        $disk=Get-VHD -Path $path;Assert-GuestVmDisk $disk
        if((ConvertTo-GuestVmDiskIdentifier $disk.DiskIdentifier) -cne (ConvertTo-GuestVmDiskIdentifier $image.disk_identifier)){throw 'guest_vm_node_image_identifier_changed'}
        return $stream
    }catch{$stream.Dispose();throw}
}
function New-GuestVmPrivateDirectory([string]$Path){
    if(Test-Path -LiteralPath $Path){throw 'guest_vm_directory_exists'}
    $acl=[Security.AccessControl.DirectorySecurity]::new();$acl.SetAccessRuleProtection($true,$false);$acl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    foreach($sid in @('S-1-5-18','S-1-5-32-544')){$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),'FullControl','ContainerInherit,ObjectInherit','None','Allow'))}
    $null=[IO.Directory]::CreateDirectory($Path,$acl);Assert-GuestVmPrivate $Path
}
function Write-GuestVmRecord([string]$Path,$Record,[bool]$New){
    $bytes=[Text.UTF8Encoding]::new($false).GetBytes(($Record|ConvertTo-Json -Depth 8 -Compress)+"`n")
    $mode=if($New){[IO.FileMode]::CreateNew}else{[IO.FileMode]::Append}
    $stream=[IO.File]::Open($Path,$mode,[IO.FileAccess]::Write,[IO.FileShare]::Read)
    try{$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
}
function Add-GuestVmEvent([string]$Event,$Data){
    $script:guestVmSequence++
    Write-GuestVmRecord $script:guestVmJournal ([ordered]@{sequence=$script:guestVmSequence;event=$Event;data=$Data}) $false
}
function Assert-GuestVmDisk($Disk,[bool]$AllowAttached=$false){if($Disk.VhdFormat -ne 'VHDX' -or $Disk.VhdType -ne 'Dynamic' -or $Disk.ParentPath -or [long]$Disk.Size -ne 25769803776 -or ($Disk.Attached -and -not $AllowAttached) -or [string]::IsNullOrEmpty([string]$Disk.DiskIdentifier)){throw 'guest_vm_independent_disk_required'};$null=ConvertTo-GuestVmDiskIdentifier $Disk.DiskIdentifier}
function Test-GuestVmDisconnectedNic($Nic){
    if($null -eq $Nic -or $null -eq $Nic.PSObject.Properties['SwitchName'] -or $null -eq $Nic.PSObject.Properties['SwitchId']){return $false}
    if(($null -ne $Nic.SwitchName -and $Nic.SwitchName -isnot [string]) -or ($null -ne $Nic.SwitchId -and $Nic.SwitchId -isnot [string] -and $Nic.SwitchId -isnot [Guid])){return $false}
    return ([string]::IsNullOrEmpty([string]$Nic.SwitchName) -and ([string]::IsNullOrEmpty([string]$Nic.SwitchId) -or [string]$Nic.SwitchId -ieq [Guid]::Empty.ToString()))
}
function Assert-GuestVmFresh($Spec,$Names){
    $macs=@($Spec.mac_address);if($Spec.optional_egress_nic){$macs+=@($Spec.egress_mac_address)}
    if(@(Get-VM|Where-Object{$_.Name -ieq $Names.vm}).Count -ne 0 -or ($Spec.node -ceq 'A' -and @(Get-VMSwitch|Where-Object{$_.Name -ieq $Names.switch}).Count -ne 0) -or
        @(Get-VMNetworkAdapter -All|Where-Object{$_.MacAddress -in $macs}).Count -ne 0){throw 'guest_vm_resource_collision'}
}
function Read-GuestVmJournal([string]$Path,[string]$Hash){
    if($Hash -cnotmatch '^[0-9a-f]{64}$'){throw 'guest_vm_journal_pin_required'}
    $stream=Open-GuestVmPinned $Path $Hash -1
    try{if($stream.Length -gt 1048576){throw 'guest_vm_journal_limit'};$reader=[IO.StreamReader]::new($stream,[Text.UTF8Encoding]::new($false,$true),$false,1024,$true);try{$text=$reader.ReadToEnd()}finally{$reader.Dispose()}}finally{$stream.Dispose()}
    $records=@();$sequence=0
    foreach($line in $text.Split("`n")){if(-not $line){continue};$row=$line|ConvertFrom-Json;Assert-GuestVmKeys $row @('sequence','event','data');$sequence++;if($row.sequence -isnot [long] -and $row.sequence -isnot [int]){throw 'guest_vm_journal_invalid'};if($row.sequence -ne $sequence -or $row.event -isnot [string] -or $sequence -gt 256){throw 'guest_vm_journal_invalid'};$records+=@($row)}
    if($records.Count -lt 2 -or $records[0].event -cne 'intent' -or $records[-1].event -cne 'prepared'){throw 'guest_vm_incomplete_manual_reconcile'}
    return ,$records
}
function Read-GuestVmPreparedReceipt([string]$Path,[string]$Hash){
    $stream=Open-GuestVmPinned $Path $Hash -1
    try{if($stream.Length -gt 32768){throw 'guest_vm_prepared_limit'};$reader=[IO.StreamReader]::new($stream,[Text.UTF8Encoding]::new($false,$true),$false,1024,$true);try{$value=$reader.ReadToEnd()|ConvertFrom-Json}finally{$reader.Dispose()}}finally{$stream.Dispose()}
    Assert-GuestVmKeys $value @('schema_version','prepared','pins')
    if(($value.schema_version -isnot [int] -and $value.schema_version -isnot [long]) -or $value.schema_version -ne 1 -or $value.prepared -isnot [bool] -or -not $value.prepared){throw 'guest_vm_prepared_changed'}
    return $value
}
function Assert-GuestVmComPorts($Ports,[string]$VmId,[string]$Pipe){
    if(@($Ports).Count -ne 2){throw 'guest_vm_com_inventory_changed'}
    $ids=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $names=[Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach($port in $Ports){
        if($port.Name -isnot [string] -or $port.Name -cnotin @('COM 1','COM 2') -or -not $names.Add($port.Name) -or
            ($port.Id -isnot [string] -and $port.Id -isnot [Guid]) -or [string]::IsNullOrEmpty([string]$port.Id) -or ([string]$port.Id).Length -gt 512 -or ([string]$port.Id).IndexOf([char]0) -ge 0 -or -not $ids.Add([string]$port.Id) -or
            (ConvertTo-GuestVmDiskIdentifier $port.VMId) -cne (ConvertTo-GuestVmDiskIdentifier $VmId) -or ($null -ne $port.Path -and $port.Path -isnot [string])){throw 'guest_vm_com_identity_changed'}
        if(($port.Name -ceq 'COM 1' -and $port.Path -cne $Pipe) -or ($port.Name -ceq 'COM 2' -and -not [string]::IsNullOrEmpty($port.Path))){throw 'guest_vm_com_path_changed'}
    }
}

function Test-GuestVmFirmwareDrive($Entry,$Drive){
    return ($null -ne $Entry -and $null -ne $Entry.PSObject.Properties['Device'] -and $null -ne $Entry.Device -and
        $null -ne $Entry.Device.PSObject.Properties['Id'] -and $null -ne $Entry.Device.Id -and $Entry.Device.Id -eq $Drive.Id)
}

function Assert-GuestVmFirmware($Firmware,$Drive,[bool]$PeerReadOnly,[string]$State,[string]$RunningFirmwareSha256){
    if($Firmware.SecureBoot -ne 'On' -or $Firmware.SecureBootTemplate -cne 'MicrosoftUEFICertificateAuthority'){throw 'guest_vm_firmware_changed'}
    $order=@($Firmware.BootOrder)
    if($order.Count -ge 1 -and (Test-GuestVmFirmwareDrive $order[0] $Drive) -and
        ($null -eq $order[0].PSObject.Properties['BootType'] -or $order[0].BootType -ceq 'Drive')){return}
    if(-not $PeerReadOnly -or $State -cne 'Running' -or $RunningFirmwareSha256 -cnotmatch '^[0-9a-f]{64}$' -or
        $order.Count -lt 2 -or $null -eq $order[0] -or $null -eq $order[1]){throw 'guest_vm_firmware_changed'}
    $first=$order[0];$next=$order[1]
    if($null -eq $first.PSObject.Properties['BootType'] -or $first.BootType -cne 'File' -or
        $null -eq $first.PSObject.Properties['Device'] -or $null -ne $first.Device -or
        $null -eq $first.PSObject.Properties['FirmwarePath'] -or $first.FirmwarePath -isnot [string] -or
        $null -eq $next.PSObject.Properties['BootType'] -or $next.BootType -cne 'Drive' -or
        -not (Test-GuestVmFirmwareDrive $next $Drive)){throw 'guest_vm_firmware_changed'}
    $path=$first.FirmwarePath.ToLowerInvariant()
    if($path -cnotmatch '^hd\(15,gpt,[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12},0x2800,0x35000\)/\\efi\\ubuntu\\shimx64\.efi$' -or
        (Get-GuestVmHash ([Text.Encoding]::UTF8.GetBytes($path))) -cne $RunningFirmwareSha256){throw 'guest_vm_firmware_changed'}
}

function Assert-GuestVmPrepared($Spec,$Names,$Pins,[string]$GuestRoot,[string]$DiskPath,[string]$IsoPath,[string]$ExpectedManifestSha256=$ManifestSha256,[bool]$PeerReadOnly=$false,[bool]$PinnedPreSeal=$false,[string]$RunningFirmwareSha256=''){
    Assert-GuestVmKeys $Pins @('installation_id','source_commit','stage_source_commit','seed_source_commit','node','manifest_sha256','stage_manifest_sha256','vm_id','vm_name','switch_id','switch_name','config_path','disk_path','disk_sha256','disk_identity','disk_identifier','seed_path','seed_sha256','seed_identity','mac_address','com1_pipe','optional_egress_nic','egress_mac_address','peer_prepared_receipt_sha256','switch_owner_manifest_sha256')
    if($Pins.node -cne $Spec.node -or $Pins.installation_id -cne $Spec.installation_id -or $Pins.source_commit -cne $Spec.source_commit -or $Pins.stage_source_commit -cne $Spec.stage_source_commit -or $Pins.seed_source_commit -cne $Spec.seed_source_commit -or $Pins.manifest_sha256 -cne $ExpectedManifestSha256 -or
        $Pins.stage_manifest_sha256 -cne $Spec.stage_manifest_sha256 -or $Pins.vm_name -cne $Names.vm -or $Pins.switch_name -cne $Names.switch -or
        $Pins.config_path -isnot [string] -or -not ($Pins.config_path -ieq $GuestRoot -or $Pins.config_path -ieq (Join-Path $GuestRoot $Names.vm)) -or $Pins.disk_path -ine $DiskPath -or $Pins.seed_path -ine $IsoPath -or $Pins.seed_sha256 -cne $Spec.seed_iso_sha256 -or
        $Pins.mac_address -cne $Spec.mac_address -or $Pins.egress_mac_address -cne $Spec.egress_mac_address -or $Pins.peer_prepared_receipt_sha256 -cne $Spec.peer_prepared_receipt_sha256 -or $Pins.com1_pipe -cne $Names.pipe -or $Pins.optional_egress_nic -ne $Spec.optional_egress_nic -or
        $Pins.vm_id -isnot [string] -or $Pins.vm_id -cnotmatch '^[0-9a-f-]{36}$' -or $Pins.switch_id -isnot [string] -or $Pins.switch_id -cnotmatch '^[0-9a-f-]{36}$'){throw 'guest_vm_pin_rejected'}
    foreach($key in @('installation_id','source_commit','stage_source_commit','seed_source_commit','node','manifest_sha256','stage_manifest_sha256','vm_id','vm_name','switch_id','switch_name','config_path','disk_path','disk_sha256','disk_identity','disk_identifier','seed_path','seed_sha256','seed_identity','mac_address','com1_pipe','egress_mac_address','peer_prepared_receipt_sha256','switch_owner_manifest_sha256')){if($Pins.$key -isnot [string]){throw 'guest_vm_pin_type_rejected'}}
    if($Pins.optional_egress_nic -isnot [bool] -or $Pins.disk_sha256 -cnotmatch '^[0-9a-f]{64}$' -or $Pins.switch_owner_manifest_sha256 -cnotmatch '^[0-9a-f]{64}$' -or ($Spec.node -ceq 'A' -and $Pins.switch_owner_manifest_sha256 -cne $ExpectedManifestSha256)){throw 'guest_vm_pin_type_rejected'}
    if($null -ne $Spec.PSObject.Properties['schema_version'] -and $Spec.schema_version -ge 2 -and ((ConvertTo-GuestVmDiskIdentifier $Pins.disk_identifier) -cne (ConvertTo-GuestVmDiskIdentifier $Spec.node_image.disk_identifier) -or $Pins.disk_sha256 -cne $Spec.node_image.sha256)){throw 'guest_vm_node_image_pin_changed'}
    $vm=Get-VM -Id ([Guid]$Pins.vm_id) -ErrorAction Stop
    if($vm.Id.ToString() -cne $Pins.vm_id -or $vm.Name -cne $Names.vm -or $vm.Generation -ne 2 -or ($vm.State -ne 'Off' -and -not ($PeerReadOnly -and $vm.State -eq 'Running')) -or $vm.Path -ine $Pins.config_path -or
        $vm.Notes -cne ('ha-owned-guest-v1:'+$Spec.installation_id+':'+$ExpectedManifestSha256) -or $vm.AutomaticStartAction -ne 'Nothing' -or $vm.AutomaticStopAction -ne 'TurnOff' -or $vm.CheckpointType -ne 'Disabled' -or $vm.AutomaticCheckpointsEnabled){throw 'guest_vm_identity_changed'}
    $switch=Get-VMSwitch -Id ([Guid]$Pins.switch_id) -ErrorAction Stop
    if($switch.Id.ToString() -cne $Pins.switch_id -or $switch.Name -cne $Names.switch -or $switch.SwitchType -ne 'Internal' -or $switch.Notes -cne ('ha-owned-guest-v1:'+$Spec.installation_id+':'+$Pins.switch_owner_manifest_sha256) -or @(Get-VMSwitch|Where-Object{$_.Name -ieq $Names.switch}).Count -ne 1){throw 'guest_vm_switch_changed'}
    $cpu=Get-VMProcessor -VM $vm;$mem=Get-VMMemory -VM $vm
    if($cpu.Count -ne 1 -or $cpu.Maximum -ne 25 -or -not $mem.DynamicMemoryEnabled -or $mem.Startup -ne 1073741824 -or $mem.Minimum -ne 805306368 -or $mem.Maximum -ne 1610612736){throw 'guest_vm_resource_bounds_changed'}
    $nic=@(Get-VMNetworkAdapter -VM $vm);$ha=@($nic|Where-Object{$_.Name -ceq 'ha-data'})
    if(@(Get-VMNetworkAdapter -All|Where-Object{$_.MacAddress -ieq $Spec.mac_address}).Count -ne 1 -or $ha.Count -ne 1 -or $ha[0].DynamicMacAddressEnabled -or $ha[0].MacAddressSpoofing -ne 'Off' -or $ha[0].MacAddress -cne $Spec.mac_address -or $ha[0].SwitchId.ToString() -cne $Pins.switch_id -or $nic.Count -ne (1+[int]$Spec.optional_egress_nic)){throw 'guest_vm_network_changed'}
    if($Spec.optional_egress_nic){$egress=@($nic|Where-Object{$_.Name -ceq 'egress-disconnected'});if($egress.Count -ne 1 -or -not (Test-GuestVmDisconnectedNic $egress[0]) -or $egress[0].DynamicMacAddressEnabled -or $egress[0].MacAddressSpoofing -ne 'Off' -or $egress[0].MacAddress -cne $Spec.egress_mac_address -or @(Get-VMNetworkAdapter -All|Where-Object{$_.MacAddress -ieq $Spec.egress_mac_address}).Count -ne 1){throw 'guest_vm_egress_must_be_disconnected'}}
    $drives=@(Get-VMHardDiskDrive -VM $vm);$dvd=@(Get-VMDvdDrive -VM $vm)
    if($drives.Count -ne 1 -or $drives[0].Path -ine $DiskPath -or $dvd.Count -ne 1 -or $dvd[0].Path -ine $IsoPath){throw 'guest_vm_media_changed'}
    $vmSid=Get-GuestVmSid $Pins.vm_id
    $disk=Get-VHD -Path $DiskPath;Assert-GuestVmDisk $disk $PeerReadOnly
    if((ConvertTo-GuestVmDiskIdentifier $disk.DiskIdentifier) -cne (ConvertTo-GuestVmDiskIdentifier $Pins.disk_identifier)){throw 'guest_vm_media_identity_changed'}
    # Only the externally pinned finalize/just-created prepare path uses preseal.
    # Its complete inventory is checked under retained handles before any DACL write.
    if(-not $PinnedPreSeal -and ((Get-GuestVmFileIdentity $DiskPath $vmSid $true $PeerReadOnly) -cne $Pins.disk_identity -or (Get-GuestVmFileIdentity $IsoPath $vmSid $false) -cne $Pins.seed_identity)){throw 'guest_vm_media_identity_changed'}
    # A running peer's disk contents are mutable. Inspect its immutable identity,
    # never obtain an exclusive handle or compare its obsolete pre-boot hash.
    if(-not $PinnedPreSeal){
        if(-not $PeerReadOnly){$d=Open-GuestVmPinned $DiskPath $Pins.disk_sha256 -1 $vmSid $true;$d.Dispose()}
        $iso=Open-GuestVmPinned $IsoPath $Spec.seed_iso_sha256 $Spec.seed_iso_bytes $vmSid $false;$iso.Dispose()
    }
    $firmware=Get-VMFirmware -VM $vm;$ports=@(Get-VMComPort -VM $vm)
    if(-not $RunningFirmwareSha256 -and $null -ne $Spec.PSObject.Properties['running_firmware_sha256']){$RunningFirmwareSha256=$Spec.running_firmware_sha256}
    Assert-GuestVmFirmware $firmware $drives[0] $PeerReadOnly ([string]$vm.State) $RunningFirmwareSha256
    Assert-GuestVmComPorts $ports $Pins.vm_id $Names.pipe
    if(-not $PinnedPreSeal){Assert-GuestVmPrivate $GuestRoot}
    return $vm
}
function Get-GuestVmPeer([string]$Root,$Spec){
    if($Spec.node -cne 'B'){throw 'guest_vm_peer_only_for_b'}
    $receipt=Read-GuestVmPreparedReceipt (Join-Path $Root 'vm-A-prepared-receipt.json') $Spec.peer_prepared_receipt_sha256
    $pins=$receipt.pins
    $peerSource=if($Spec.PSObject.Properties['schema_version'] -and $Spec.schema_version -ge 3){$Spec.peer_source_commit}else{$Spec.source_commit}
    if($pins.node -cne 'A' -or $pins.installation_id -cne $Spec.installation_id -or $pins.source_commit -cne $peerSource -or $pins.stage_source_commit -cne $Spec.stage_source_commit -or $pins.seed_source_commit -cne $Spec.seed_source_commit -or $pins.stage_manifest_sha256 -cne $Spec.stage_manifest_sha256 -or $pins.manifest_sha256 -isnot [string] -or $pins.manifest_sha256 -cnotmatch '^[0-9a-f]{64}$'){throw 'guest_vm_peer_component_changed'}
    $peerRoot=Join-Path $Root 'guest-A';$peerDisk=Join-Path $peerRoot 'guest-A.vhdx';$peerIso=Join-Path $Root 'seed-A.iso'
    $isoBytes=(Get-Item -LiteralPath $peerIso -Force).Length
    # This is an installed-state projection, not a claim about A's creation method.
    # The caller pins A's original receipt/manifest SHA, all 25 pins and actual VM/media identity.
    $peerSpec=[pscustomobject]@{schema_version=1;source_commit=$peerSource;stage_source_commit=$Spec.stage_source_commit;seed_source_commit=$Spec.seed_source_commit;installation_id=$Spec.installation_id;stage_manifest_sha256=$Spec.stage_manifest_sha256;seed_iso_sha256=$pins.seed_sha256;seed_iso_bytes=$isoBytes;node='A';mac_address=$pins.mac_address;optional_egress_nic=$pins.optional_egress_nic;egress_mac_address=$pins.egress_mac_address;peer_prepared_receipt_sha256=''}
    $null=Assert-GuestVmSpec $peerSpec
    $peerMacs=@($peerSpec.mac_address);if($peerSpec.optional_egress_nic){$peerMacs+=@($peerSpec.egress_mac_address)}
    if($Spec.mac_address -in $peerMacs -or ($Spec.optional_egress_nic -and $Spec.egress_mac_address -in $peerMacs)){throw 'guest_vm_peer_mac_collision'}
    if($Spec.PSObject.Properties['schema_version'] -and $Spec.schema_version -ge 3){$null=Read-GuestVmConfigProfileReceipt $Root 'A' $Spec.peer_config_profile_receipt_sha256 $pins $Spec.config_profile $Spec.peer_prepared_receipt_sha256}
    $firmwarePin=if($null -ne $Spec.PSObject.Properties['running_peer_firmware_sha256']){$Spec.running_peer_firmware_sha256}else{''}
    $peerVm=Assert-GuestVmPrepared $peerSpec (Get-GuestVmNames $peerSpec) $pins $peerRoot $peerDisk $peerIso $pins.manifest_sha256 $true $false $firmwarePin
    $switch=Get-VMSwitch -Id ([Guid]$pins.switch_id) -ErrorAction Stop
    return [pscustomobject]@{pins=$pins;vm=$peerVm;switch=$switch}
}
function Read-GuestVmJsonPinned([string]$Path,[string]$Hash,[long]$Limit=1048576){
    $stream=Open-GuestVmPinned $Path $Hash -1
    try{if($stream.Length -gt $Limit){throw 'guest_vm_json_limit'};$reader=[IO.StreamReader]::new($stream,[Text.UTF8Encoding]::new($false,$true),$false,1024,$true);try{$text=$reader.ReadToEnd()}finally{$reader.Dispose()}}finally{$stream.Dispose()}
    $null=[HaGuestVmJsonV1]::Object($text,[int]$Limit)
    return $text|ConvertFrom-Json
}
function Open-GuestVmSealHandle([string]$Path,[bool]$Directory,[bool]$ManagedConfig=$false){return [HaGuestVmSealV1]::new($Path,$Directory,$ManagedConfig)}
function Test-GuestVmManagedConfigTarget($Target,$Profile){return ($null -ne $Profile -and $Target.role -ceq 'config' -and -not $Target.directory)}
function Get-GuestVmRootStamp([string]$Root){
    Assert-GuestVmPrivate $Root
    $handle=Open-GuestVmSealHandle $Root $true
    try{
        if($null -ne $script:guestOwnedSpec){$handle.BindObservedService((Get-GuestOwnedServiceSid $script:guestOwnedSpec.installation_id))}
        if(-not $handle.Canonical('','private')){throw 'guest_vm_root_not_admin_system_only'}
        return [pscustomobject]@{identity=$handle.Identity();sddl_sha256=$handle.SddlHash()}
    }finally{$handle.Dispose()}
}
function Get-GuestVmSealTargets([string]$Root,$Spec,$Pins){
    $guest=Join-Path $Root ('guest-'+$Spec.node);$disk=Join-Path $guest ('guest-'+$Spec.node+'.vhdx');$seed=Join-Path $Root ('seed-'+$Spec.node+'.iso')
    $config=$Pins.config_path
    if($config -ine $guest -and $config -ine (Join-Path $guest (Get-GuestVmNames $Spec).vm)){throw 'guest_vm_seal_config_scope'}
    $queue=[Collections.Generic.Queue[string]]::new();$queue.Enqueue($guest)
    $targets=[Collections.Generic.List[object]]::new();$seen=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    while($queue.Count){
        $path=$queue.Dequeue();Assert-GuestVmPath $path;$entry=Get-Item -LiteralPath $path -Force
        if(-not $seen.Add($entry.FullName) -or $seen.Count -gt 127){throw 'guest_vm_seal_inventory_limit'}
        $directory=$entry -is [IO.DirectoryInfo]
        $role=if($entry.FullName -ieq $guest){'private'}elseif($entry.FullName -ieq $disk -and -not $directory){'disk'}elseif($entry.FullName -ieq $config -or $entry.FullName.StartsWith($config+'\',[StringComparison]::OrdinalIgnoreCase)){'config'}else{throw 'guest_vm_seal_unowned_child'}
        $relative=$entry.FullName.Substring($Root.Length+1).Replace('\','/')
        if($relative.Length -gt 180){throw 'guest_vm_seal_path_limit'}
        $targets.Add([pscustomobject]@{path=$entry.FullName;relative_path=$relative;role=$role;directory=$directory})
        if($directory){
            $children=@(Get-ChildItem -LiteralPath $entry.FullName -Force|Select-Object -First 128)
            if($seen.Count+$queue.Count+$children.Count -gt 127){throw 'guest_vm_seal_inventory_limit'}
            foreach($child in $children){$queue.Enqueue($child.FullName)}
        }
    }
    Assert-GuestVmPath $seed;$item=Get-Item -LiteralPath $seed -Force
    if($item -isnot [IO.FileInfo]){throw 'guest_vm_seal_seed_type'}
    $targets.Add([pscustomobject]@{path=$seed;relative_path=('seed-'+$Spec.node+'.iso');role='seed';directory=$false})
    if(@($targets|Where-Object{$_.role -ceq 'disk'}).Count -ne 1){throw 'guest_vm_seal_disk_missing'}
    return ,@($targets|Sort-Object relative_path -CaseSensitive)
}
function Assert-GuestVmSealRow($Row,$Target){
    Assert-GuestVmKeys $Row @('relative_path','role','identity','bytes','sha256','sddl_sha256')
    if($Row.relative_path -isnot [string] -or $Row.relative_path -cne $Target.relative_path -or $Row.role -isnot [string] -or $Row.role -cne $Target.role -or
        $Row.identity -isnot [string] -or $Row.identity -cnotmatch '^[0-9a-f]{8}:[0-9a-f]{16}$' -or
        ($Row.bytes -isnot [int] -and $Row.bytes -isnot [long]) -or $Row.bytes -lt 0 -or $Row.bytes -gt 8589934592 -or
        $Row.sha256 -isnot [string] -or ($Target.directory -and ($Row.bytes -ne 0 -or $Row.sha256 -cne '')) -or (-not $Target.directory -and $Row.sha256 -cnotmatch '^[0-9a-f]{64}$') -or
        $Row.sddl_sha256 -isnot [string] -or $Row.sddl_sha256 -cnotmatch '^[0-9a-f]{64}$'){throw 'guest_vm_seal_row_rejected'}
    if($Target.role -ceq 'config' -and $Row.bytes -gt 67108864){throw 'guest_vm_seal_config_size'}
}
function Get-GuestVmHandleRow($Handle,$Target){
    $length=$Handle.Bytes();$limit=if($Target.role -ceq 'config'){67108864}elseif($Target.role -ceq 'seed'){2147483648}else{8589934592}
    if($length -lt 0 -or $length -gt $limit -or ($Target.directory -and $length -ne 0)){throw 'guest_vm_seal_size_limit'}
    return [pscustomobject][ordered]@{relative_path=$Target.relative_path;role=$Target.role;identity=$Handle.Identity();bytes=$length;sha256=$Handle.Hash();sddl_sha256=$Handle.SddlHash()}
}
function Assert-GuestVmSameSealRow($Actual,$Expected){
    foreach($key in @('relative_path','role','identity','bytes','sha256','sddl_sha256')){if($Actual.$key -cne $Expected.$key){throw 'guest_vm_seal_object_changed'}}
}
function Open-GuestVmSealInventory([string]$Root,$Spec,$Pins,$Expected,$ConfigProfile=$null){
    if($null -ne $ConfigProfile){$null=Assert-GuestVmConfigProfile $ConfigProfile}
    $targets=Get-GuestVmSealTargets $Root $Spec $Pins
    if($null -ne $Expected){$Expected=@($Expected|Sort-Object relative_path -CaseSensitive)}
    if($null -ne $Expected -and (@($Expected).Count -ne $targets.Count)){throw 'guest_vm_seal_inventory_changed'}
    $sessions=[Collections.Generic.List[object]]::new()
    try{
        for($i=0;$i -lt $targets.Count;$i++){
            $target=$targets[$i]
            if($null -ne $Expected){Assert-GuestVmSealRow $Expected[$i] $target}
            # Only the explicit config profile selects shared metadata access.
            # Media remains retained/exclusive-read and ends with no capability ACE.
            $handle=Open-GuestVmSealHandle $target.path $target.directory (Test-GuestVmManagedConfigTarget $target $ConfigProfile)
            $sessions.Add([pscustomobject]@{handle=$handle;target=$target;before=$null})
            $row=Get-GuestVmHandleRow $handle $target;Assert-GuestVmSealRow $row $target
            if($Spec.PSObject.Properties['schema_version'] -and $Spec.schema_version -eq 4){$handle.BindObservedService((Get-GuestOwnedServiceSid $Spec.installation_id))}
            if($null -ne $ConfigProfile){$handle.BindProfile($ConfigProfile.name,$ConfigProfile.capability_sid,$target.role)}
            elseif($handle.HasUnverifiedCapability()){throw 'guest_vm_capability_proof_required'}
            $owner=$handle.Owner();if($owner -cnotin @('S-1-5-18','S-1-5-32-544',(Get-GuestVmCurrentSid))){throw 'guest_vm_seal_owner_rejected'}
            if($null -ne $Expected){Assert-GuestVmSameSealRow $row $Expected[$i]}
            if($target.role -ceq 'disk' -and ($row.identity -cne $Pins.disk_identity -or $row.sha256 -cne $Pins.disk_sha256)){throw 'guest_vm_seal_disk_pin_changed'}
            if($target.role -ceq 'seed' -and ($row.identity -cne $Pins.seed_identity -or $row.sha256 -cne $Pins.seed_sha256 -or $row.bytes -ne $Spec.seed_iso_bytes)){throw 'guest_vm_seal_seed_pin_changed'}
            $sessions[$sessions.Count-1].before=$row
        }
        return ,$sessions
    }catch{foreach($session in $sessions){$session.handle.Dispose()};throw}
}
function Invoke-GuestVmSealInventory($Sessions,[string]$VmSid){
    $after=[Collections.Generic.List[object]]::new()
    # Children become protected before parents, preventing inheritance propagation
    # from changing a not-yet-sealed child's pinned descriptor. Handles stay retained.
    for($i=$Sessions.Count-1;$i -ge 0;$i--){
        $session=$Sessions[$i]
        $before=$session.before;$handle=$session.handle;$target=$session.target
        Assert-GuestVmSameSealRow (Get-GuestVmHandleRow $handle $target) $before
        Add-GuestVmEvent 'seal_exact_owned_object' @{relative_path=$target.relative_path;identity=$before.identity;before_sddl_sha256=$before.sddl_sha256}
        $handle.Seal($VmSid,$target.role)
        $row=Get-GuestVmHandleRow $handle $target
        foreach($key in @('identity','bytes','sha256')){if($row.$key -cne $before.$key){throw 'guest_vm_seal_content_changed'}}
        if(-not $handle.Canonical($VmSid,$target.role)){throw 'guest_vm_seal_not_canonical'}
        $after.Add($row)
    }
    return ,@($after.ToArray()|Sort-Object relative_path -CaseSensitive)
}
function Assert-GuestVmSealInventoryComplete([string]$Root,$Spec,$Pins,$Sessions){
    $current=Get-GuestVmSealTargets $Root $Spec $Pins
    if($current.Count -ne $Sessions.Count){throw 'guest_vm_seal_inventory_changed'}
    for($i=0;$i -lt $current.Count;$i++){
        if($current[$i].relative_path -cne $Sessions[$i].target.relative_path -or $current[$i].role -cne $Sessions[$i].target.role -or $current[$i].directory -ne $Sessions[$i].target.directory){throw 'guest_vm_seal_inventory_changed'}
        # A shared config handle can survive a VMMS rename/replace. Verify the
        # current pathname too, not only the retained (possibly unlinked) handle.
        if(-not $current[$i].directory -and (Get-GuestVmFreshSealIdentity $current[$i].path) -cne $Sessions[$i].before.identity){throw 'guest_vm_seal_path_replaced'}
    }
}
function Get-GuestVmFreshSealIdentity([string]$Path){return [HaGuestVmFileV1]::SharedIdentity($Path)}
function Assert-GuestVmSealRootUnchanged([string]$Root,$Stamp){
    $after=Get-GuestVmRootStamp $Root
    if($after.identity -cne $Stamp.identity -or $after.sddl_sha256 -cne $Stamp.sddl_sha256){throw 'guest_vm_root_changed'}
}
function Read-GuestVmPartialJournal([string]$Path,[string]$Hash,$Spec,$Pins){
    $stream=Open-GuestVmPinned $Path $Hash -1
    try{if($stream.Length -gt 65536){throw 'guest_vm_partial_journal_limit'};$reader=[IO.StreamReader]::new($stream,[Text.UTF8Encoding]::new($false,$true),$false,1024,$true);try{$text=$reader.ReadToEnd()}finally{$reader.Dispose()}}finally{$stream.Dispose()}
    if(-not $text.EndsWith("`n")){throw 'guest_vm_partial_journal_truncated'}
    $events=@('intent','create_private_directory','copy_independent_disk','create_internal_switch','switch_created','create_off_vm','vm_created','configure_new_vm','grant_owned_vm_data_access')
    $lines=@($text.Split("`n")|Where-Object{$_})
    if($Spec.node -cne 'A' -or $lines.Count -ne $events.Count){throw 'guest_vm_partial_phase_rejected'}
    $rows=@()
    for($i=0;$i -lt $lines.Count;$i++){
        $null=[HaGuestVmJsonV1]::Object($lines[$i],65536);$row=$lines[$i]|ConvertFrom-Json;Assert-GuestVmKeys $row @('sequence','event','data')
        if(($row.sequence -isnot [int] -and $row.sequence -isnot [long]) -or $row.sequence -ne $i+1 -or $row.event -cne $events[$i]){throw 'guest_vm_partial_phase_rejected'};$rows+=@($row)
    }
    $intent=$rows[0].data
    foreach($key in @('installation_id','source_commit','stage_source_commit','seed_source_commit','node')){if($intent.$key -cne $Spec.$key){throw 'guest_vm_partial_source_changed'}}
    if($intent.manifest_sha256 -cne $ManifestSha256 -or $intent.stage_manifest_sha256 -cne $Spec.stage_manifest_sha256 -or $intent.seed_sha256 -cne $Spec.seed_iso_sha256 -or
        $rows[4].data.switch_id -cne $Pins.switch_id -or $rows[6].data.vm_id -cne $Pins.vm_id -or $rows[6].data.switch_id -cne $Pins.switch_id -or $rows[8].data.vm_id -cne $Pins.vm_id){throw 'guest_vm_partial_identity_changed'}
}
function Get-GuestVmScriptHash{return Get-GuestVmHash ([IO.File]::ReadAllBytes($PSCommandPath))}
function Invoke-GuestVmFinalize([string]$Root,$Spec,$Names,[string]$GuestRoot,[string]$DiskPath,[string]$IsoPath,[string]$Journal,[string]$PreparedPath){
    if($FinalizationRequestSha256 -cnotmatch '^[0-9a-f]{64}$' -or $JournalSha256 -cnotmatch '^[0-9a-f]{64}$' -or $FinalizerSourceCommit -cnotmatch '^[0-9a-f]{40}$'){throw 'guest_vm_finalize_pins_required'}
    $receiptPath=Join-Path $Root ('vm-'+$Spec.node+'-finalization-receipt.json');$repairJournal=Join-Path $Root ('vm-'+$Spec.node+'-finalize-journal.jsonl')
    if((Test-Path -LiteralPath $receiptPath) -or (Test-Path -LiteralPath $repairJournal) -or (Test-Path -LiteralPath $PreparedPath)){throw 'guest_vm_finalize_existing_partial'}
    $request=Read-GuestVmJsonPinned (Join-Path $Root ('vm-'+$Spec.node+'-finalize-request.json')) $FinalizationRequestSha256
    $requestKeys= @('schema_version','finalizer_source_commit','finalizer_script_sha256','original_source_commit','original_manifest_sha256','original_journal_sha256','installation_id','node','root_identity','root_sddl_sha256','pins','objects')
    $configProfile=$null
    if($request.PSObject.Properties['config_profile']){$requestKeys+=@('config_profile','config_profile_sha256');$configProfile=Assert-GuestVmConfigProfile $request.config_profile;if((Get-GuestVmConfigProfileHash $configProfile) -cne $request.config_profile_sha256){throw 'guest_vm_finalize_config_profile_changed'}}
    Assert-GuestVmKeys $request $requestKeys
    if(($request.schema_version -isnot [int] -and $request.schema_version -isnot [long]) -or $request.schema_version -ne 1 -or
        $request.finalizer_source_commit -cne $FinalizerSourceCommit -or $request.finalizer_script_sha256 -cne (Get-GuestVmScriptHash) -or
        $request.original_source_commit -cne $Spec.source_commit -or $request.original_manifest_sha256 -cne $ManifestSha256 -or $request.original_journal_sha256 -cne $JournalSha256 -or
        $request.installation_id -cne $Spec.installation_id -or $request.node -cne $Spec.node -or $request.objects -isnot [array] -or $request.objects.Count -gt 128){throw 'guest_vm_finalize_source_rejected'}
    $pins=$request.pins
    $null=Assert-GuestVmPrepared $Spec $Names $pins $GuestRoot $DiskPath $IsoPath $ManifestSha256 $false $true
    Read-GuestVmPartialJournal $Journal $JournalSha256 $Spec $pins
    $stamp=Get-GuestVmRootStamp $Root
    if($stamp.identity -cne $request.root_identity -or $stamp.sddl_sha256 -cne $request.root_sddl_sha256){throw 'guest_vm_finalize_root_changed'}
    $sessions=Open-GuestVmSealInventory $Root $Spec $pins $request.objects $configProfile
    try{
        # All handles, original source, identities, content and descriptors are pinned before mutation.
        $null=Assert-GuestVmPrepared $Spec $Names $pins $GuestRoot $DiskPath $IsoPath $ManifestSha256 $false $true
        Assert-GuestVmSealInventoryComplete $Root $Spec $pins $sessions
        $script:guestVmJournal=$repairJournal;$script:guestVmSequence=1
        Write-GuestVmRecord $repairJournal ([ordered]@{sequence=1;event='finalize_intent';data=@{finalization_request_sha256=$FinalizationRequestSha256;original_journal_sha256=$JournalSha256;original_source_commit=$Spec.source_commit;finalizer_source_commit=$FinalizerSourceCommit;vm_id=$pins.vm_id}}) $true
        $sealed=Invoke-GuestVmSealInventory $sessions (Get-GuestVmSid $pins.vm_id)
        Assert-GuestVmSealInventoryComplete $Root $Spec $pins $sessions
    }finally{foreach($session in $sessions){$session.handle.Dispose()}}
    Assert-GuestVmSealRootUnchanged $Root $stamp
    # The old journal is immutable historical evidence, never extended or replaced.
    $old=Open-GuestVmPinned $Journal $JournalSha256 -1;$old.Dispose()
    $null=Assert-GuestVmPrepared $Spec $Names $pins $GuestRoot $DiskPath $IsoPath
    $profileReceiptHash=''
    if($null -ne $configProfile){$profileReceiptHash=Write-GuestVmConfigProfileReceipt $Root $Spec $pins $configProfile $FinalizerSourceCommit}
    Add-GuestVmEvent 'finalized' $pins
    Write-GuestVmRecord $PreparedPath ([ordered]@{schema_version=1;prepared=$true;pins=$pins}) $true
    $receipt=[ordered]@{schema_version=1;format='ha-guest-vm-acl-finalization-v1';original_source_commit=$Spec.source_commit;original_manifest_sha256=$ManifestSha256;original_journal_sha256=$JournalSha256;finalizer_source_commit=$FinalizerSourceCommit;finalizer_script_sha256=(Get-GuestVmScriptHash);finalization_request_sha256=$FinalizationRequestSha256;finalize_journal_sha256=(Get-GuestVmHash ([IO.File]::ReadAllBytes($repairJournal)));prepared_receipt_sha256=(Get-GuestVmHash ([IO.File]::ReadAllBytes($PreparedPath)));root_identity=$stamp.identity;root_sddl_sha256=$stamp.sddl_sha256;pins=$pins;sealed_objects=$sealed}
    if($null -ne $configProfile){$receipt.config_profile=$configProfile;$receipt.config_profile_sha256=Get-GuestVmConfigProfileHash $configProfile;$receipt.config_profile_receipt_sha256=$profileReceiptHash}
    Write-GuestVmRecord $receiptPath $receipt $true
    [ordered]@{scope='exact_owned_off_vm_finalize';finalizer_source_commit=$FinalizerSourceCommit;original_source_commit=$Spec.source_commit;finalization_receipt_sha256=(Get-GuestVmHash ([IO.File]::ReadAllBytes($receiptPath)));prepared_receipt_sha256=$receipt.prepared_receipt_sha256;vm_id=$pins.vm_id;prepared=$true;started=$false;production_transition=$false}|ConvertTo-Json -Compress
}

function Assert-GuestVmSealedObjects([string]$Root,$Spec,$Pins,$Objects,$Stamp,$ConfigProfile=$null){
    Assert-GuestVmSealRootUnchanged $Root $Stamp
    $sessions=Open-GuestVmSealInventory $Root $Spec $Pins $Objects $ConfigProfile
    try{foreach($session in $sessions){if(-not $session.handle.Canonical((Get-GuestVmSid $Pins.vm_id),$session.target.role)){throw 'guest_vm_canonical_acl_changed'}}}finally{foreach($session in $sessions){$session.handle.Dispose()}}
}
function Read-GuestVmFinalization([string]$Root,$Spec,[string]$Journal){
    if($FinalizationReceiptSha256 -cnotmatch '^[0-9a-f]{64}$' -or $FinalizerSourceCommit -cnotmatch '^[0-9a-f]{40}$'){throw 'guest_vm_finalization_pin_required'}
    $receipt=Read-GuestVmJsonPinned (Join-Path $Root ('vm-'+$Spec.node+'-finalization-receipt.json')) $FinalizationReceiptSha256
    $receiptKeys= @('schema_version','format','original_source_commit','original_manifest_sha256','original_journal_sha256','finalizer_source_commit','finalizer_script_sha256','finalization_request_sha256','finalize_journal_sha256','prepared_receipt_sha256','root_identity','root_sddl_sha256','pins','sealed_objects')
    $configProfile=$null
    if($receipt.PSObject.Properties['config_profile']){$receiptKeys+=@('config_profile','config_profile_sha256','config_profile_receipt_sha256');$configProfile=Assert-GuestVmConfigProfile $receipt.config_profile;if((Get-GuestVmConfigProfileHash $configProfile) -cne $receipt.config_profile_sha256){throw 'guest_vm_finalize_config_profile_changed'}}
    Assert-GuestVmKeys $receipt $receiptKeys
    if(($receipt.schema_version -isnot [int] -and $receipt.schema_version -isnot [long]) -or $receipt.schema_version -ne 1 -or $receipt.format -cne 'ha-guest-vm-acl-finalization-v1' -or
        $receipt.original_source_commit -cne $Spec.source_commit -or $receipt.original_manifest_sha256 -cne $ManifestSha256 -or $receipt.original_journal_sha256 -cne $JournalSha256 -or $receipt.finalizer_source_commit -cne $FinalizerSourceCommit -or
        $receipt.prepared_receipt_sha256 -cne $PreparedReceiptSha256 -or $receipt.sealed_objects -isnot [array] -or $receipt.sealed_objects.Count -gt 128){throw 'guest_vm_finalization_changed'}
    foreach($key in @('finalizer_script_sha256','finalization_request_sha256','finalize_journal_sha256','root_sddl_sha256')){if($receipt.$key -isnot [string] -or $receipt.$key -cnotmatch '^[0-9a-f]{64}$'){throw 'guest_vm_finalization_hash_required'}}
    Read-GuestVmPartialJournal $Journal $JournalSha256 $Spec $receipt.pins
    $request=Open-GuestVmPinned (Join-Path $Root ('vm-'+$Spec.node+'-finalize-request.json')) $receipt.finalization_request_sha256 -1;$request.Dispose()
    $path=Join-Path $Root ('vm-'+$Spec.node+'-finalize-journal.jsonl');$stream=Open-GuestVmPinned $path $receipt.finalize_journal_sha256 -1
    try{if($stream.Length -gt 1048576){throw 'guest_vm_finalization_journal_limit'};$reader=[IO.StreamReader]::new($stream,[Text.UTF8Encoding]::new($false,$true),$false,1024,$true);try{$text=$reader.ReadToEnd()}finally{$reader.Dispose()}}finally{$stream.Dispose()}
    if(-not $text.EndsWith("`n")){throw 'guest_vm_finalization_journal_truncated'}
    $lines=@($text.Split("`n")|Where-Object{$_});if($lines.Count -ne $receipt.sealed_objects.Count+2+[int]($null -ne $configProfile)){throw 'guest_vm_finalization_journal_changed'}
    for($i=0;$i -lt $lines.Count;$i++){
        $null=[HaGuestVmJsonV1]::Object($lines[$i],1048576);$row=$lines[$i]|ConvertFrom-Json;Assert-GuestVmKeys $row @('sequence','event','data')
        $event=if($i -eq 0){'finalize_intent'}elseif($i -eq $lines.Count-1){'finalized'}elseif($null -ne $configProfile -and $i -eq $lines.Count-2){'write_config_profile_receipt'}else{'seal_exact_owned_object'}
        if(($row.sequence -isnot [int] -and $row.sequence -isnot [long]) -or $row.sequence -ne $i+1 -or $row.event -cne $event){throw 'guest_vm_finalization_journal_changed'}
        if($i -eq 0 -and ($row.data.finalization_request_sha256 -cne $receipt.finalization_request_sha256 -or $row.data.original_journal_sha256 -cne $JournalSha256 -or $row.data.original_source_commit -cne $Spec.source_commit -or $row.data.finalizer_source_commit -cne $FinalizerSourceCommit -or $row.data.vm_id -cne $receipt.pins.vm_id)){throw 'guest_vm_finalization_intent_changed'}
        if($i -eq $lines.Count-1 -and ($row.data|ConvertTo-Json -Depth 8 -Compress) -cne ($receipt.pins|ConvertTo-Json -Depth 8 -Compress)){throw 'guest_vm_finalization_pins_changed'}
        if($null -ne $configProfile -and $i -eq $lines.Count-2){Assert-GuestVmKeys $row.data @('receipt_sha256','config_profile_sha256','prepared_receipt_sha256');if($row.data.receipt_sha256 -cne $receipt.config_profile_receipt_sha256 -or $row.data.config_profile_sha256 -cne $receipt.config_profile_sha256 -or $row.data.prepared_receipt_sha256 -cne $PreparedReceiptSha256){throw 'guest_vm_finalization_profile_journal_changed'}}
        if($i -gt 0 -and $i -le $receipt.sealed_objects.Count -and ($row.data.relative_path -cne $receipt.sealed_objects[$receipt.sealed_objects.Count-$i].relative_path -or $row.data.identity -cne $receipt.sealed_objects[$receipt.sealed_objects.Count-$i].identity)){throw 'guest_vm_finalization_inventory_changed'}
    }
    if($null -ne $configProfile){$null=Read-GuestVmConfigProfileReceipt $Root $Spec.node $receipt.config_profile_receipt_sha256 $receipt.pins $configProfile $PreparedReceiptSha256}
    Assert-GuestVmSealedObjects $Root $Spec $receipt.pins $receipt.sealed_objects ([pscustomobject]@{identity=$receipt.root_identity;sddl_sha256=$receipt.root_sddl_sha256}) $configProfile
    return $receipt
}

function Get-GuestVmPreparedHash($Pins){return Get-GuestVmHash ([Text.UTF8Encoding]::new($false).GetBytes((([ordered]@{schema_version=1;prepared=$true;pins=$Pins})|ConvertTo-Json -Depth 8 -Compress)+"`n"))}
function Write-GuestVmConfigProfileReceipt([string]$Root,$Spec,$Pins,$ConfigProfile,[string]$SourceCommit){
    $hash=Get-GuestVmConfigProfileHash $ConfigProfile
    $value=[ordered]@{schema_version=1;config_profile=$ConfigProfile;config_profile_sha256=$hash;installation_id=$Spec.installation_id;node=$Spec.node;original_source_commit=$Spec.source_commit;manifest_sha256=$ManifestSha256;prepared_receipt_sha256=(Get-GuestVmPreparedHash $Pins);vm_id=$Pins.vm_id;config_path=$Pins.config_path;finalizer_source_commit=$SourceCommit;metadata_trust_scope='HostHypervisorTCB/config-metadata-only';acceptance_scope=$ConfigProfile.acceptance_scope;operational_cutover_accepted=$ConfigProfile.operational_cutover_accepted;operational_blocked_reason=$(if($ConfigProfile.acceptance_scope -ceq 'isolated-predeployment'){'production_cutover_not_authorized_for_isolated_profile'}elseif($ConfigProfile.operational_cutover_accepted){''}else{'production_io_actor_review_and_approval_required'})}
    $bytes=[Text.UTF8Encoding]::new($false).GetBytes(($value|ConvertTo-Json -Depth 8 -Compress)+"`n");$receiptHash=Get-GuestVmHash $bytes
    Add-GuestVmEvent 'write_config_profile_receipt' @{receipt_sha256=$receiptHash;config_profile_sha256=$hash;prepared_receipt_sha256=$value.prepared_receipt_sha256}
    Write-GuestVmRecord (Join-Path $Root ('vm-'+$Spec.node+'-config-profile-receipt.json')) $value $true
    return $receiptHash
}
function Read-GuestVmConfigProfileReceipt([string]$Root,[string]$Node,[string]$Hash,$Pins,$ConfigProfile,[string]$PreparedHash){
    $value=Read-GuestVmJsonPinned (Join-Path $Root ('vm-'+$Node+'-config-profile-receipt.json')) $Hash
    Assert-GuestVmKeys $value @('schema_version','config_profile','config_profile_sha256','installation_id','node','original_source_commit','manifest_sha256','prepared_receipt_sha256','vm_id','config_path','finalizer_source_commit','metadata_trust_scope','acceptance_scope','operational_cutover_accepted','operational_blocked_reason')
    if(($value.schema_version -isnot [int] -and $value.schema_version -isnot [long]) -or $value.schema_version -ne 1 -or
        (Get-GuestVmConfigProfileHash $value.config_profile) -cne (Get-GuestVmConfigProfileHash $ConfigProfile) -or $value.config_profile_sha256 -cne (Get-GuestVmConfigProfileHash $ConfigProfile) -or
        $value.installation_id -cne $Pins.installation_id -or $value.node -cne $Node -or $value.original_source_commit -cne $Pins.source_commit -or $value.manifest_sha256 -cne $Pins.manifest_sha256 -or
        $value.prepared_receipt_sha256 -cne $PreparedHash -or $value.vm_id -cne $Pins.vm_id -or $value.config_path -ine $Pins.config_path -or
        $value.metadata_trust_scope -cne 'HostHypervisorTCB/config-metadata-only' -or $value.acceptance_scope -cne $ConfigProfile.acceptance_scope -or $value.finalizer_source_commit -isnot [string] -or $value.finalizer_source_commit -cnotmatch '^[0-9a-f]{40}$' -or $value.operational_cutover_accepted -isnot [bool] -or $value.operational_cutover_accepted -ne $ConfigProfile.operational_cutover_accepted -or
        $value.operational_blocked_reason -cne $(if($ConfigProfile.acceptance_scope -ceq 'isolated-predeployment'){'production_cutover_not_authorized_for_isolated_profile'}elseif($ConfigProfile.operational_cutover_accepted){''}else{'production_io_actor_review_and_approval_required'})){throw 'guest_vm_config_profile_receipt_changed'}
    return $value
}

function Assert-GuestVmReaderReady([string]$Root,$Spec,$Pins){
    if($ReaderReadySha256 -cnotmatch '^[0-9a-f]{64}$' -or $ReaderSourceCommit -cnotmatch '^[0-9a-f]{40}$'){throw 'guest_vm_reader_pin_required'}
    $stream=Open-GuestVmPinned (Join-Path $Root ('com-'+$Spec.node+'-reader-ready.json')) $ReaderReadySha256 -1
    try{if($stream.Length -gt 32768){throw 'guest_vm_reader_ready_limit'};$reader=[IO.StreamReader]::new($stream,[Text.UTF8Encoding]::new($false,$true),$false,1024,$true);try{$ready=$reader.ReadToEnd()|ConvertFrom-Json}finally{$reader.Dispose()}}finally{$stream.Dispose()}
    Assert-GuestVmKeys $ready @('format','schema_version','source_commit','request_sha256','prepared_receipt_sha256','installation_id','node','vm_id','com1_pipe','reader_pid','reader_start_ticks','reader_image_sha256','pipe_bound')
    foreach($key in @('format','source_commit','request_sha256','prepared_receipt_sha256','installation_id','node','vm_id','com1_pipe','reader_image_sha256')){if($ready.$key -isnot [string]){throw 'guest_vm_reader_ready_type_rejected'}}
    if(($ready.schema_version -isnot [int] -and $ready.schema_version -isnot [long]) -or $ready.schema_version -ne 1 -or $ready.format -cne 'ha-guest-com-reader-ready-v1' -or $ready.source_commit -cne $ReaderSourceCommit -or
        $ready.request_sha256 -isnot [string] -or $ready.request_sha256 -cnotmatch '^[0-9a-f]{64}$' -or $ready.reader_image_sha256 -isnot [string] -or $ready.reader_image_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        $ready.prepared_receipt_sha256 -cne $PreparedReceiptSha256 -or $ready.installation_id -cne $Spec.installation_id -or $ready.node -cne $Spec.node -or $ready.vm_id -cne $Pins.vm_id -or $ready.com1_pipe -cne $Pins.com1_pipe -or
        $ready.pipe_bound -isnot [bool] -or $ready.pipe_bound -or ($ready.reader_pid -isnot [int] -and $ready.reader_pid -isnot [long]) -or $ready.reader_pid -lt 1 -or $ready.reader_pid -gt 2147483647 -or
        ($ready.reader_start_ticks -isnot [int] -and $ready.reader_start_ticks -isnot [long]) -or $ready.reader_start_ticks -lt 1){throw 'guest_vm_reader_ready_rejected'}
    $process=Get-Process -Id ([int]$ready.reader_pid) -ErrorAction Stop
    try{
        $image=Get-GuestVmReaderImagePath
        if($process.Id -ne $ready.reader_pid -or $process.HasExited -or $process.StartTime.ToUniversalTime().Ticks -ne $ready.reader_start_ticks -or $process.Path -ine $image -or (Get-FileHash -LiteralPath $image -Algorithm SHA256).Hash.ToLowerInvariant() -cne $ready.reader_image_sha256 -or (Get-GuestVmReaderOwner $ready.reader_pid) -notin @('S-1-5-18',(Get-GuestVmCurrentSid))){throw 'guest_vm_reader_process_changed'}
        $process.Refresh()
        if($process.HasExited -or $process.StartTime.ToUniversalTime().Ticks -ne $ready.reader_start_ticks){throw 'guest_vm_reader_process_changed'}
    }finally{$process.Dispose()}
    return $ready
}
function Invoke-GuestVm{
    if(-not $Go -or [Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or -not [Environment]::Is64BitProcess){throw 'guest_vm_native_intent_required'}
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent();try{if(-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))){throw 'guest_vm_admin_required'}}finally{$identity.Dispose()}
    $spec=Read-GuestVmSpec;$names=Get-GuestVmNames $spec
    $selectedProfile=if($spec.schema_version -ge 3){$spec.config_profile}else{$null}
    Initialize-GuestVmNative
    $parent=[Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
    $root=Join-Path $parent ('N100HA_'+$spec.installation_id.Substring(0,24));$ownedLease=$null
    if($spec.schema_version -eq 4){. (Import-GuestOwnedSupport);$script:guestOwnedSpec=$spec;$script:guestOwnedRoot=$root;$ownedLease=Open-GuestOwnedRoot $root $spec (Get-GuestVmScriptHash)}
    try{
    Assert-GuestVmPrivate $root
    $stagePath=Join-Path $root 'stage-manifest.json';$stageStream=Open-GuestVmPinned $stagePath $spec.stage_manifest_sha256 -1
    $isoStream=$null;$baseStream=$null;$nodeStream=$null
    try{
        if($stageStream.Length -gt 32768){throw 'guest_vm_stage_limit'};$reader=[IO.StreamReader]::new($stageStream,[Text.UTF8Encoding]::new($false,$true),$false,1024,$true);try{$stage=$reader.ReadToEnd()|ConvertFrom-Json}finally{$reader.Dispose()}
        $stageKeys=@('schema_version','source_commit','installation_id','compressed_sha256','compressed_bytes','vhdx_sha256','vhdx_bytes')
        if($spec.schema_version -eq 4){$stageKeys+=@(Get-GuestOwnedFields);Assert-GuestOwnedDeclaration $stage '';foreach($key in @('root_profile','executor_source_commit','reservation_receipt_sha256','reservation_root_identity')){if($stage.$key -cne $spec.$key){throw 'guest_vm_stage_provenance_changed'}}}
        Assert-GuestVmKeys $stage $stageKeys
        if(($stage.schema_version -isnot [long] -and $stage.schema_version -isnot [int]) -or $stage.schema_version -ne $(if($spec.schema_version -eq 4){2}else{1}) -or $stage.source_commit -isnot [string] -or $stage.installation_id -isnot [string] -or $stage.vhdx_sha256 -isnot [string] -or $stage.source_commit -cne $spec.stage_source_commit -or $stage.installation_id -cne $spec.installation_id -or $stage.vhdx_sha256 -cnotmatch '^[0-9a-f]{64}$' -or ($stage.vhdx_bytes -isnot [long] -and $stage.vhdx_bytes -isnot [int]) -or $stage.vhdx_bytes -lt 1048576 -or $stage.vhdx_bytes -gt 8589934592){throw 'guest_vm_stage_changed'}
        $guestRoot=Join-Path $root ('guest-'+$spec.node);$diskPath=Join-Path $guestRoot ('guest-'+$spec.node+'.vhdx');$isoPath=Join-Path $root ('seed-'+$spec.node+'.iso');$journal=Join-Path $root ('vm-'+$spec.node+'-journal.jsonl');$preparedPath=Join-Path $root ('vm-'+$spec.node+'-prepared-receipt.json')
        if($Mode -ceq 'finalize'){
            Invoke-GuestVmFinalize $root $spec $names $guestRoot $diskPath $isoPath $journal $preparedPath
            return
        }
        if($Mode -ceq 'start'){
            $finalization=$null
            if($FinalizationReceiptSha256){
                $finalization=Read-GuestVmFinalization $root $spec $journal
                $records=@([pscustomobject]@{data=[pscustomobject]@{manifest_sha256=$ManifestSha256;installation_id=$spec.installation_id}},[pscustomobject]@{data=$finalization.pins})
            }else{$records=Read-GuestVmJournal $journal $JournalSha256}
            $intent=$records[0].data
            if($intent.manifest_sha256 -cne $ManifestSha256 -or $intent.installation_id -cne $spec.installation_id){throw 'guest_vm_intent_changed'}
            $pins=$records[-1].data
            $prepared=Read-GuestVmPreparedReceipt $preparedPath $PreparedReceiptSha256
            if(($prepared.pins|ConvertTo-Json -Depth 8 -Compress) -cne ($pins|ConvertTo-Json -Depth 8 -Compress)){throw 'guest_vm_prepared_changed'}
            if($spec.node -ceq 'B'){
                $peer=Get-GuestVmPeer $root $spec
                if($pins.switch_id -cne $peer.pins.switch_id -or $pins.switch_owner_manifest_sha256 -cne $peer.pins.manifest_sha256){throw 'guest_vm_peer_switch_changed'}
            }
            $vm=Assert-GuestVmPrepared $spec $names $pins $guestRoot $diskPath $isoPath
            if($null -eq $finalization -and $spec.schema_version -ge 2){
                $seal=@($records|Where-Object{$_.PSObject.Properties['event'] -and $_.event -ceq 'canonical_acl_sealed'})
                if($seal.Count -ne 1){throw 'guest_vm_canonical_seal_required'}
                Assert-GuestVmSealedObjects $root $spec $pins $seal[0].data.objects $seal[0].data.root_stamp $selectedProfile
                if($null -ne $selectedProfile){
                    $profileWrites=@($records|Where-Object{$_.event -ceq 'write_config_profile_receipt'})
                    if($profileWrites.Count -ne 1 -or $profileWrites[0].data.config_profile_sha256 -cne (Get-GuestVmConfigProfileHash $selectedProfile) -or $profileWrites[0].data.prepared_receipt_sha256 -cne $PreparedReceiptSha256){throw 'guest_vm_config_profile_receipt_required'}
                    $null=Read-GuestVmConfigProfileReceipt $root $spec.node $profileWrites[0].data.receipt_sha256 $pins $selectedProfile $PreparedReceiptSha256
                }
            }
            $os=Get-CimInstance Win32_OperatingSystem
            if([long]$os.FreePhysicalMemory -lt 5242880){throw 'guest_vm_memory_reserve_failed'}
            $null=Assert-GuestVmReaderReady $root $spec $pins
            # CreateNew is durable before power-on; ambiguous outcomes cannot be retried.
            $request=Join-Path $root ('vm-'+$spec.node+'-start-request.json')
            Write-GuestVmRecord $request ([ordered]@{manifest_sha256=$ManifestSha256;journal_sha256=$JournalSha256;prepared_receipt_sha256=$PreparedReceiptSha256;reader_ready_sha256=$ReaderReadySha256;reader_source_commit=$ReaderSourceCommit;finalization_receipt_sha256=$FinalizationReceiptSha256;finalizer_source_commit=$FinalizerSourceCommit;vm_id=$pins.vm_id;installation_id=$spec.installation_id}) $true
            $script:guestVmJournal=if($null -ne $finalization){Join-Path $root ('vm-'+$spec.node+'-finalize-start-journal.jsonl')}else{$journal};$script:guestVmSequence=$records.Count
            if($null -ne $finalization){$script:guestVmSequence=0;Write-GuestVmRecord $script:guestVmJournal ([ordered]@{sequence=1;event='finalized_start_intent';data=@{finalization_receipt_sha256=$FinalizationReceiptSha256;vm_id=$pins.vm_id}}) $true;$script:guestVmSequence=1}
            Add-GuestVmEvent 'start_requested' @{vm_id=$pins.vm_id}
            $null=Assert-GuestVmReaderReady $root $spec $pins
            $null=Start-VM -VM $vm -ErrorAction Stop
            $actual=Get-VM -Id ([Guid]$pins.vm_id) -ErrorAction Stop
            if($actual.State -ne 'Running' -or $actual.Name -cne $names.vm -or $actual.Notes -cne $vm.Notes){throw 'guest_vm_start_uncertain_manual_reconcile'}
            Add-GuestVmEvent 'started' @{vm_id=$pins.vm_id}
            [ordered]@{scope='fresh_owned_guest';node=$spec.node;vm_id=$pins.vm_id;manifest_sha256=$ManifestSha256;journal_sha256=(Get-GuestVmHash ([IO.File]::ReadAllBytes($journal)));started=$true;guest_verified=$false;production_transition=$false}|ConvertTo-Json -Compress
            return
        }
        if($Mode -cne 'prepare'){throw 'guest_vm_mode_rejected'}
        $isoStream=Open-GuestVmPinned $isoPath $spec.seed_iso_sha256 $spec.seed_iso_bytes;$createdSeedIdentity=Get-GuestVmFileIdentity $isoPath
        Assert-GuestVmFresh $spec $names
        $peer=$null
        if($spec.node -ceq 'B'){$peer=Get-GuestVmPeer $root $spec}
        if((Test-Path -LiteralPath $guestRoot) -or (Test-Path -LiteralPath $journal) -or (Test-Path -LiteralPath $preparedPath)){throw 'guest_vm_existing_partial_manual_reconcile'}
        $base=Join-Path $root 'base.vhdx';$baseStream=Open-GuestVmPinned $base $stage.vhdx_sha256 $stage.vhdx_bytes
        Assert-GuestVmDisk (Get-VHD -Path $base)
        $copySource=$baseStream;$copyHash=$stage.vhdx_sha256;$copyBytes=$stage.vhdx_bytes;$copySourcePath=$base
        if($spec.schema_version -ge 2){
            $nodeStream=Open-GuestVmNodeImage $root $spec;$copySource=$nodeStream;$copyHash=$spec.node_image.sha256;$copyBytes=$spec.node_image.bytes;$copySourcePath=Join-Path $root $spec.node_image.filename
            if((ConvertTo-GuestVmDiskIdentifier $spec.node_image.disk_identifier) -ceq (ConvertTo-GuestVmDiskIdentifier (Get-VHD -Path $base).DiskIdentifier) -or ($spec.node -ceq 'B' -and (ConvertTo-GuestVmDiskIdentifier $spec.node_image.disk_identifier) -ceq (ConvertTo-GuestVmDiskIdentifier $peer.pins.disk_identifier))){throw 'guest_vm_node_image_identifier_collision'}
        }
        $drive=[IO.DriveInfo]::new([IO.Path]::GetPathRoot($root));if($drive.AvailableFreeSpace -lt (25769803776+10737418240)){throw 'guest_vm_disk_reserve_failed'}
        $script:guestVmJournal=$journal;$script:guestVmSequence=1
        Write-GuestVmRecord $journal ([ordered]@{sequence=1;event='intent';data=@{installation_id=$spec.installation_id;source_commit=$spec.source_commit;stage_source_commit=$spec.stage_source_commit;seed_source_commit=$spec.seed_source_commit;node=$spec.node;manifest_sha256=$ManifestSha256;stage_manifest_sha256=$spec.stage_manifest_sha256;seed_sha256=$spec.seed_iso_sha256;vm_name=$names.vm;switch_name=$names.switch;config_path=$guestRoot;disk_path=$diskPath;com1_pipe=$names.pipe;egress_mac_address=$spec.egress_mac_address;peer_prepared_receipt_sha256=$spec.peer_prepared_receipt_sha256}}) $true
        Add-GuestVmEvent 'create_private_directory' @{};New-GuestVmPrivateDirectory $guestRoot
        $copyEvidence=@{}
        if($spec.schema_version -ge 2){$copyEvidence=@{creation_method=$spec.creation_method;node_image=$spec.node_image}}
        Add-GuestVmEvent 'copy_independent_disk' $copyEvidence
        $copy=[IO.File]::Open($diskPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        try{$copySource.CopyTo($copy,1048576);$copy.Flush($true)}finally{$copy.Dispose()}
        $copied=Open-GuestVmPinned $diskPath $copyHash $copyBytes;$copied.Dispose()
        if((Get-GuestVmFileIdentity $diskPath) -ceq (Get-GuestVmFileIdentity $copySourcePath) -or (Get-GuestVmFileIdentity $diskPath) -ceq (Get-GuestVmFileIdentity $base)){throw 'guest_vm_copy_file_identity_collision'}
        $disk=Get-VHD -Path $diskPath;Assert-GuestVmDisk $disk;$createdDiskIdentity=Get-GuestVmFileIdentity $diskPath
        # The fresh byte copy duplicates its source identifier. Only this new copy may be reset.
        if($spec.schema_version -eq 1){
            if((ConvertTo-GuestVmDiskIdentifier $disk.DiskIdentifier) -ceq (ConvertTo-GuestVmDiskIdentifier (Get-VHD -Path $base).DiskIdentifier)){Add-GuestVmEvent 'reset_new_copy_disk_identifier' @{};Set-VHD -Path $diskPath -ResetDiskIdentifier -ErrorAction Stop}
        }elseif((ConvertTo-GuestVmDiskIdentifier $disk.DiskIdentifier) -cne (ConvertTo-GuestVmDiskIdentifier $spec.node_image.disk_identifier)){throw 'guest_vm_node_image_identifier_changed'}
        $disk=Get-VHD -Path $diskPath;Assert-GuestVmDisk $disk
        if((ConvertTo-GuestVmDiskIdentifier $disk.DiskIdentifier) -ceq (ConvertTo-GuestVmDiskIdentifier (Get-VHD -Path $base).DiskIdentifier)){throw 'guest_vm_duplicate_disk_identifier'}
        if($spec.node -ceq 'B' -and (ConvertTo-GuestVmDiskIdentifier $disk.DiskIdentifier) -ceq (ConvertTo-GuestVmDiskIdentifier $peer.pins.disk_identifier)){throw 'guest_vm_peer_disk_identifier_collision'}
        $diskHashStream=[IO.File]::OpenRead($diskPath);try{$diskHash=Get-GuestVmStreamHash $diskHashStream}finally{$diskHashStream.Dispose()}
        $switchOwner=$ManifestSha256
        if($spec.node -ceq 'A'){
            Add-GuestVmEvent 'create_internal_switch' @{}
            $switch=New-VMSwitch -Name $names.switch -SwitchType Internal -Notes ('ha-owned-guest-v1:'+$spec.installation_id+':'+$ManifestSha256) -ErrorAction Stop
            Add-GuestVmEvent 'switch_created' @{switch_id=$switch.Id.ToString()}
        }else{
            $peer=Get-GuestVmPeer $root $spec;$switch=$peer.switch;$switchOwner=$peer.pins.manifest_sha256
            Add-GuestVmEvent 'reuse_pinned_a_switch' @{switch_id=$switch.Id.ToString();peer_prepared_receipt_sha256=$spec.peer_prepared_receipt_sha256}
        }
        Add-GuestVmEvent 'create_off_vm' @{switch_id=$switch.Id.ToString()}
        # Hyper-V appends VMName itself. The actual config subtree is already
        # GuestRoot\VMName; appending it here would create a second wrapper.
        $vm=New-VM -Name $names.vm -Generation 2 -MemoryStartupBytes 1GB -NoVHD -Path $guestRoot -SwitchName $names.switch -ErrorAction Stop
        if($vm.Name -cne $names.vm -or $vm.State -ne 'Off'){throw 'guest_vm_new_identity_uncertain'}
        Add-GuestVmEvent 'vm_created' @{vm_id=$vm.Id.ToString();switch_id=$switch.Id.ToString()}
        Add-GuestVmEvent 'configure_new_vm' @{vm_id=$vm.Id.ToString()}
        Set-VM -VM $vm -Notes ('ha-owned-guest-v1:'+$spec.installation_id+':'+$ManifestSha256) -AutomaticStartAction Nothing -AutomaticStopAction TurnOff -CheckpointType Disabled -AutomaticCheckpointsEnabled $false
        Set-VMProcessor -VM $vm -Count 1 -Maximum 25
        Set-VMMemory -VM $vm -DynamicMemoryEnabled $true -StartupBytes 1GB -MinimumBytes 768MB -MaximumBytes 1536MB
        $nic=@(Get-VMNetworkAdapter -VM $vm);if($nic.Count -ne 1 -or $nic[0].SwitchId -ne $switch.Id){throw 'guest_vm_new_nic_uncertain'}
        Rename-VMNetworkAdapter -VMNetworkAdapter $nic[0] -NewName 'ha-data'
        Set-VMNetworkAdapter -VMNetworkAdapter $nic[0] -StaticMacAddress $spec.mac_address -MacAddressSpoofing Off
        if($spec.optional_egress_nic){
            Add-VMNetworkAdapter -VM $vm -Name 'egress-disconnected'
            $newEgress=@(Get-VMNetworkAdapter -VM $vm|Where-Object{$_.Name -ceq 'egress-disconnected'})
            if($newEgress.Count -ne 1 -or -not (Test-GuestVmDisconnectedNic $newEgress[0])){throw 'guest_vm_new_egress_changed'}
            Set-VMNetworkAdapter -VMNetworkAdapter $newEgress[0] -StaticMacAddress $spec.egress_mac_address -MacAddressSpoofing Off
        }
        Add-VMHardDiskDrive -VM $vm -Path $diskPath
        $newDvd=@(Get-VMDvdDrive -VM $vm)
        if($newDvd.Count -eq 0){Add-VMDvdDrive -VM $vm -Path $isoPath}
        elseif($newDvd.Count -eq 1 -and -not $newDvd[0].Path){Set-VMDvdDrive -VMDvdDrive $newDvd[0] -Path $isoPath}
        else{throw 'guest_vm_new_dvd_changed'}
        # Gen2 COM configuration requires Secure Boot disabled only while the new VM stays Off.
        Set-VMFirmware -VM $vm -EnableSecureBoot Off
        Set-VMComPort -VM $vm -Number 1 -Path $names.pipe
        $attached=@(Get-VMHardDiskDrive -VM $vm);if($attached.Count -ne 1 -or $attached[0].Path -ine $diskPath){throw 'guest_vm_new_disk_changed'}
        Set-VMFirmware -VM $vm -EnableSecureBoot On -SecureBootTemplate MicrosoftUEFICertificateAuthority -FirstBootDevice $attached[0]
        $vmSid=Get-GuestVmSid $vm.Id.ToString()
        Add-GuestVmEvent 'grant_owned_vm_data_access' @{vm_id=$vm.Id.ToString()}
        if([HaGuestVmFileV1]::Identity($diskPath) -cne $createdDiskIdentity -or [HaGuestVmFileV1]::Identity($isoPath) -cne $createdSeedIdentity){throw 'guest_vm_created_media_replaced'}
        $pins=[ordered]@{installation_id=$spec.installation_id;source_commit=$spec.source_commit;stage_source_commit=$spec.stage_source_commit;seed_source_commit=$spec.seed_source_commit;node=$spec.node;manifest_sha256=$ManifestSha256;stage_manifest_sha256=$spec.stage_manifest_sha256;vm_id=$vm.Id.ToString();vm_name=$names.vm;switch_id=$switch.Id.ToString();switch_name=$names.switch;config_path=$vm.Path;disk_path=$diskPath;disk_sha256=$diskHash;disk_identity=$createdDiskIdentity;disk_identifier=(ConvertTo-GuestVmDiskIdentifier $disk.DiskIdentifier);seed_path=$isoPath;seed_sha256=$spec.seed_iso_sha256;seed_identity=$createdSeedIdentity;mac_address=$spec.mac_address;com1_pipe=$names.pipe;optional_egress_nic=$spec.optional_egress_nic;egress_mac_address=$spec.egress_mac_address;peer_prepared_receipt_sha256=$spec.peer_prepared_receipt_sha256;switch_owner_manifest_sha256=$switchOwner}
        $null=Assert-GuestVmPrepared $spec $names ([pscustomobject]$pins) $guestRoot $diskPath $isoPath $ManifestSha256 $false $true
        $stamp=Get-GuestVmRootStamp $root;$sessions=Open-GuestVmSealInventory $root $spec ([pscustomobject]$pins) $null $selectedProfile
        try{
            Assert-GuestVmSealInventoryComplete $root $spec ([pscustomobject]$pins) $sessions
            $sealed=Invoke-GuestVmSealInventory $sessions $vmSid
            Assert-GuestVmSealInventoryComplete $root $spec ([pscustomobject]$pins) $sessions
        }finally{foreach($session in $sessions){$session.handle.Dispose()}}
        Assert-GuestVmSealRootUnchanged $root $stamp
        Add-GuestVmEvent 'canonical_acl_sealed' @{objects=$sealed;root_stamp=$stamp}
        if($spec.node -ceq 'B'){$peer=Get-GuestVmPeer $root $spec;if($peer.pins.switch_id -cne $pins.switch_id -or $peer.pins.manifest_sha256 -cne $switchOwner){throw 'guest_vm_peer_switch_changed'}}
        $null=Assert-GuestVmPrepared $spec $names ([pscustomobject]$pins) $guestRoot $diskPath $isoPath
        if($null -ne $selectedProfile){
            $profileOrigin=if($null -ne $spec.PSObject.Properties['vm_operation_source_commit']){$spec.vm_operation_source_commit}elseif($spec.schema_version -eq 4){$spec.executor_source_commit}else{$spec.source_commit}
            $null=Write-GuestVmConfigProfileReceipt $root $spec ([pscustomobject]$pins) $selectedProfile $profileOrigin
        }
        Add-GuestVmEvent 'prepared' $pins
        Write-GuestVmRecord $preparedPath ([ordered]@{schema_version=1;prepared=$true;pins=$pins}) $true
        Assert-GuestVmPrivate $preparedPath
        [ordered]@{scope='fresh_owned_guest';node=$spec.node;vm_id=$vm.Id.ToString();switch_id=$switch.Id.ToString();manifest_sha256=$ManifestSha256;journal_sha256=(Get-GuestVmHash ([IO.File]::ReadAllBytes($journal)));prepared_receipt_sha256=(Get-GuestVmHash ([IO.File]::ReadAllBytes($preparedPath)));prepared=$true;started=$false;production_transition=$false}|ConvertTo-Json -Compress
    }finally{if($null -ne $nodeStream){$nodeStream.Dispose()};if($null -ne $baseStream){$baseStream.Dispose()};if($null -ne $isoStream){$isoStream.Dispose()};$stageStream.Dispose()}
    }finally{if($null -ne $ownedLease){$ownedLease.Dispose()}}
}
if($MyInvocation.InvocationName -ne '.'){
    try{Invoke-GuestVm}catch{throw 'guest_vm_rejected_preserve_partial_manual_reconcile'}
}
