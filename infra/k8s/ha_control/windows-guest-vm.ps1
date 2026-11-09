param([ValidateSet('prepare','start')][string]$Mode,[string]$Manifest,[string]$ManifestSha256,[string]$JournalSha256,[string]$PreparedReceiptSha256,[string]$ReaderReadySha256,[string]$ReaderSourceCommit,[switch]$Go)
# Fresh guests only. B can reuse only A's receipt-pinned owned Internal switch.
# Every mutation is journaled first. Partial failures are preserved for manual reconciliation.
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
function Get-GuestVmHash([byte[]]$Bytes){$h=[Security.Cryptography.SHA256]::Create();try{return [BitConverter]::ToString($h.ComputeHash($Bytes)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}}
function Get-GuestVmStreamHash($Stream){$h=[Security.Cryptography.SHA256]::Create();try{return [BitConverter]::ToString($h.ComputeHash($Stream)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}}
function Assert-GuestVmKeys($Value,[string[]]$Keys){if((($Value.PSObject.Properties.Name|Sort-Object -CaseSensitive)-join ',') -cne (($Keys|Sort-Object -CaseSensitive)-join ',')){throw 'guest_vm_shape_rejected'}}
function ConvertTo-GuestVmDiskIdentifier($Value){
    # Get-VHD returns GUID objects or uppercase strings depending on the native provider.
    # Accept only a nonzero D-format GUID, then bind its value in canonical lowercase.
    if($Value -isnot [string] -and $Value -isnot [Guid]){throw 'guest_vm_disk_identifier_rejected'}
    $text=$Value.ToString();$guid=[Guid]::Empty
    if($text.Length -ne 36 -or $text -cnotmatch '^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$' -or -not [Guid]::TryParseExact($text,'D',[ref]$guid) -or $guid -eq [Guid]::Empty){throw 'guest_vm_disk_identifier_rejected'}
    return $guid.ToString('D')
}
function Assert-GuestVmSpec($Value){
    $fields=@('schema_version','source_commit','stage_source_commit','seed_source_commit','installation_id','stage_manifest_sha256','seed_iso_sha256','seed_iso_bytes','node','mac_address','optional_egress_nic','egress_mac_address','peer_prepared_receipt_sha256')
    if(($Value.schema_version -isnot [int] -and $Value.schema_version -isnot [long]) -or $Value.schema_version -notin @(1,2)){throw 'guest_vm_identity_rejected'}
    if($Value.schema_version -eq 2){$fields+=@('creation_method','node_image')}
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
    if($Value.schema_version -eq 2){
        if($Value.creation_method -isnot [string] -or $Value.creation_method -cne 'verified-node-image-copy-v1'){throw 'guest_vm_creation_method_rejected'}
        $image=$Value.node_image
        Assert-GuestVmKeys $image @('filename','sha256','bytes','disk_identifier','source_qcow_sha256','conversion_receipt_sha256','tool_version','tool_sha256')
        foreach($key in @('sha256','source_qcow_sha256','conversion_receipt_sha256','tool_sha256')){if($image.$key -isnot [string] -or $image.$key -cnotmatch '^[0-9a-f]{64}$'){throw 'guest_vm_node_image_hash_rejected'}}
        if($image.filename -isnot [string] -or $image.filename -cne ('node-'+$Value.node+'.vhdx') -or ($image.bytes -isnot [int] -and $image.bytes -isnot [long]) -or $image.bytes -lt 1048576 -or $image.bytes -gt 8589934592 -or
            $image.disk_identifier -isnot [string] -or
            $image.tool_version -isnot [string] -or $image.tool_version -cnotmatch '^qemu-img version [0-9]+\.[0-9]+\.[0-9]+$' -or
            $image.source_qcow_sha256 -cne '04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94'){throw 'guest_vm_node_image_rejected'}
        $null=ConvertTo-GuestVmDiskIdentifier $image.disk_identifier
    }
    return $Value
}
function Read-GuestVmSpec{
    if($Manifest -isnot [string] -or $Manifest.Length -gt 32768 -or $ManifestSha256 -cnotmatch '^[0-9a-f]{64}$'){throw 'guest_vm_manifest_pin_required'}
    $bytes=[Convert]::FromBase64String($Manifest)
    $text=([Text.UTF8Encoding]::new($false,$true)).GetString($bytes)
    # Every accepted key/value alphabet is escape-free. Reject before JSON decoding
    # so Unicode-escaped duplicate keys cannot bypass the literal key inventory.
    if($text.Contains('\')){throw 'guest_vm_duplicate_or_escaped_keys'}
    $keys=@([regex]::Matches($text,'"([A-Za-z_][A-Za-z0-9_]*)"\s*:')|ForEach-Object{$_.Groups[1].Value})
    if($keys.Count -notin @(13,23) -or @($keys|Select-Object -Unique).Count -ne $keys.Count){throw 'guest_vm_duplicate_or_escaped_keys'}
    if((Get-GuestVmHash $bytes) -cne $ManifestSha256){throw 'guest_vm_manifest_changed'}
    return Assert-GuestVmSpec ($text|ConvertFrom-Json)
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
function Assert-GuestVmPrivate([string]$Path,[string]$VmSid='',[bool]$VmWriter=$false){
    Assert-GuestVmPath $Path
    $sd=[Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $Path).Sddl)
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent();try{$allowed=@('S-1-5-18','S-1-5-32-544',$identity.User.Value)}finally{$identity.Dispose()}
    if($sd.Owner.Value -notin $allowed -or $null -eq $sd.DiscretionaryAcl -or $sd.DiscretionaryAcl.Count -eq 0){throw 'guest_vm_private_owner_required'}
    foreach($ace in $sd.DiscretionaryAcl){
        if($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback){throw 'guest_vm_private_acl_required'}
        if($ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed -and $ace.SecurityIdentifier.Value -notin $allowed -and $ace.AccessMask -ne 0){
            if(-not $VmSid -or $ace.SecurityIdentifier.Value -cne $VmSid -or (-not $VmWriter -and (([long]$ace.AccessMask -band 4294967295) -band (4294967295 -bxor 1179785)) -ne 0)){throw 'guest_vm_private_acl_required'}
        }
    }
}
function Initialize-GuestVmNative{
    if('HaGuestVmFileV1' -as [type]){throw 'guest_vm_native_type_collision'}
    Add-Type -TypeDefinition @'
using System;using System.Runtime.InteropServices;using Microsoft.Win32.SafeHandles;
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
    try{if($stream.Length -gt 65536){throw 'guest_vm_journal_limit'};$reader=[IO.StreamReader]::new($stream,[Text.UTF8Encoding]::new($false,$true),$false,1024,$true);try{$text=$reader.ReadToEnd()}finally{$reader.Dispose()}}finally{$stream.Dispose()}
    $records=@();$sequence=0
    foreach($line in $text.Split("`n")){if(-not $line){continue};$row=$line|ConvertFrom-Json;Assert-GuestVmKeys $row @('sequence','event','data');$sequence++;if($row.sequence -isnot [long] -and $row.sequence -isnot [int]){throw 'guest_vm_journal_invalid'};if($row.sequence -ne $sequence -or $row.event -isnot [string] -or $sequence -gt 64){throw 'guest_vm_journal_invalid'};$records+=@($row)}
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
function Assert-GuestVmPrepared($Spec,$Names,$Pins,[string]$GuestRoot,[string]$DiskPath,[string]$IsoPath,[string]$ExpectedManifestSha256=$ManifestSha256,[bool]$PeerReadOnly=$false){
    Assert-GuestVmKeys $Pins @('installation_id','source_commit','stage_source_commit','seed_source_commit','node','manifest_sha256','stage_manifest_sha256','vm_id','vm_name','switch_id','switch_name','config_path','disk_path','disk_sha256','disk_identity','disk_identifier','seed_path','seed_sha256','seed_identity','mac_address','com1_pipe','optional_egress_nic','egress_mac_address','peer_prepared_receipt_sha256','switch_owner_manifest_sha256')
    if($Pins.node -cne $Spec.node -or $Pins.installation_id -cne $Spec.installation_id -or $Pins.source_commit -cne $Spec.source_commit -or $Pins.stage_source_commit -cne $Spec.stage_source_commit -or $Pins.seed_source_commit -cne $Spec.seed_source_commit -or $Pins.manifest_sha256 -cne $ExpectedManifestSha256 -or
        $Pins.stage_manifest_sha256 -cne $Spec.stage_manifest_sha256 -or $Pins.vm_name -cne $Names.vm -or $Pins.switch_name -cne $Names.switch -or
        $Pins.config_path -isnot [string] -or -not ($Pins.config_path -ieq $GuestRoot -or $Pins.config_path -ieq (Join-Path $GuestRoot $Names.vm)) -or $Pins.disk_path -ine $DiskPath -or $Pins.seed_path -ine $IsoPath -or $Pins.seed_sha256 -cne $Spec.seed_iso_sha256 -or
        $Pins.mac_address -cne $Spec.mac_address -or $Pins.egress_mac_address -cne $Spec.egress_mac_address -or $Pins.peer_prepared_receipt_sha256 -cne $Spec.peer_prepared_receipt_sha256 -or $Pins.com1_pipe -cne $Names.pipe -or $Pins.optional_egress_nic -ne $Spec.optional_egress_nic -or
        $Pins.vm_id -isnot [string] -or $Pins.vm_id -cnotmatch '^[0-9a-f-]{36}$' -or $Pins.switch_id -isnot [string] -or $Pins.switch_id -cnotmatch '^[0-9a-f-]{36}$'){throw 'guest_vm_pin_rejected'}
    foreach($key in @('installation_id','source_commit','stage_source_commit','seed_source_commit','node','manifest_sha256','stage_manifest_sha256','vm_id','vm_name','switch_id','switch_name','config_path','disk_path','disk_sha256','disk_identity','disk_identifier','seed_path','seed_sha256','seed_identity','mac_address','com1_pipe','egress_mac_address','peer_prepared_receipt_sha256','switch_owner_manifest_sha256')){if($Pins.$key -isnot [string]){throw 'guest_vm_pin_type_rejected'}}
    if($Pins.optional_egress_nic -isnot [bool] -or $Pins.disk_sha256 -cnotmatch '^[0-9a-f]{64}$' -or $Pins.switch_owner_manifest_sha256 -cnotmatch '^[0-9a-f]{64}$' -or ($Spec.node -ceq 'A' -and $Pins.switch_owner_manifest_sha256 -cne $ExpectedManifestSha256)){throw 'guest_vm_pin_type_rejected'}
    if($null -ne $Spec.PSObject.Properties['schema_version'] -and $Spec.schema_version -eq 2 -and ((ConvertTo-GuestVmDiskIdentifier $Pins.disk_identifier) -cne (ConvertTo-GuestVmDiskIdentifier $Spec.node_image.disk_identifier) -or $Pins.disk_sha256 -cne $Spec.node_image.sha256)){throw 'guest_vm_node_image_pin_changed'}
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
    if((ConvertTo-GuestVmDiskIdentifier $disk.DiskIdentifier) -cne (ConvertTo-GuestVmDiskIdentifier $Pins.disk_identifier) -or (Get-GuestVmFileIdentity $DiskPath $vmSid $true $PeerReadOnly) -cne $Pins.disk_identity -or (Get-GuestVmFileIdentity $IsoPath $vmSid $false) -cne $Pins.seed_identity){throw 'guest_vm_media_identity_changed'}
    # A running peer's disk contents are mutable. Inspect its immutable identity,
    # never obtain an exclusive handle or compare its obsolete pre-boot hash.
    if(-not $PeerReadOnly){$d=Open-GuestVmPinned $DiskPath $Pins.disk_sha256 -1 $vmSid $true;$d.Dispose()}
    $iso=Open-GuestVmPinned $IsoPath $Spec.seed_iso_sha256 $Spec.seed_iso_bytes $vmSid $false;$iso.Dispose()
    $firmware=Get-VMFirmware -VM $vm;$ports=@(Get-VMComPort -VM $vm)
    if(@($firmware.BootOrder).Count -lt 1 -or $firmware.BootOrder[0].Device.Id -ne $drives[0].Id -or $firmware.SecureBoot -ne 'On' -or $firmware.SecureBootTemplate -cne 'MicrosoftUEFICertificateAuthority' -or @($ports|Where-Object{$_.Number -eq 1 -and $_.Path -ceq $Names.pipe}).Count -ne 1 -or @($ports|Where-Object{$_.Number -ne 1 -and $_.Path}).Count -ne 0){throw 'guest_vm_firmware_changed'}
    Assert-GuestVmPrivate $GuestRoot
    return $vm
}
function Get-GuestVmPeer([string]$Root,$Spec){
    if($Spec.node -cne 'B'){throw 'guest_vm_peer_only_for_b'}
    $receipt=Read-GuestVmPreparedReceipt (Join-Path $Root 'vm-A-prepared-receipt.json') $Spec.peer_prepared_receipt_sha256
    $pins=$receipt.pins
    if($pins.node -cne 'A' -or $pins.installation_id -cne $Spec.installation_id -or $pins.source_commit -cne $Spec.source_commit -or $pins.stage_source_commit -cne $Spec.stage_source_commit -or $pins.seed_source_commit -cne $Spec.seed_source_commit -or $pins.stage_manifest_sha256 -cne $Spec.stage_manifest_sha256 -or $pins.manifest_sha256 -isnot [string] -or $pins.manifest_sha256 -cnotmatch '^[0-9a-f]{64}$'){throw 'guest_vm_peer_component_changed'}
    $peerRoot=Join-Path $Root 'guest-A';$peerDisk=Join-Path $peerRoot 'guest-A.vhdx';$peerIso=Join-Path $Root 'seed-A.iso'
    $isoBytes=(Get-Item -LiteralPath $peerIso -Force).Length
    # This is an installed-state projection, not a claim about A's creation method.
    # The caller pins A's original receipt/manifest SHA, all 25 pins and actual VM/media identity.
    $peerSpec=[pscustomobject]@{schema_version=1;source_commit=$Spec.source_commit;stage_source_commit=$Spec.stage_source_commit;seed_source_commit=$Spec.seed_source_commit;installation_id=$Spec.installation_id;stage_manifest_sha256=$Spec.stage_manifest_sha256;seed_iso_sha256=$pins.seed_sha256;seed_iso_bytes=$isoBytes;node='A';mac_address=$pins.mac_address;optional_egress_nic=$pins.optional_egress_nic;egress_mac_address=$pins.egress_mac_address;peer_prepared_receipt_sha256=''}
    $null=Assert-GuestVmSpec $peerSpec
    $peerMacs=@($peerSpec.mac_address);if($peerSpec.optional_egress_nic){$peerMacs+=@($peerSpec.egress_mac_address)}
    if($Spec.mac_address -in $peerMacs -or ($Spec.optional_egress_nic -and $Spec.egress_mac_address -in $peerMacs)){throw 'guest_vm_peer_mac_collision'}
    $peerVm=Assert-GuestVmPrepared $peerSpec (Get-GuestVmNames $peerSpec) $pins $peerRoot $peerDisk $peerIso $pins.manifest_sha256 $true
    $switch=Get-VMSwitch -Id ([Guid]$pins.switch_id) -ErrorAction Stop
    return [pscustomobject]@{pins=$pins;vm=$peerVm;switch=$switch}
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
    Initialize-GuestVmNative
    $parent=[Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
    $root=Join-Path $parent ('N100HA_'+$spec.installation_id.Substring(0,24));Assert-GuestVmPrivate $root
    $stagePath=Join-Path $root 'stage-manifest.json';$stageStream=Open-GuestVmPinned $stagePath $spec.stage_manifest_sha256 -1
    $isoStream=$null;$baseStream=$null;$nodeStream=$null
    try{
        if($stageStream.Length -gt 32768){throw 'guest_vm_stage_limit'};$reader=[IO.StreamReader]::new($stageStream,[Text.UTF8Encoding]::new($false,$true),$false,1024,$true);try{$stage=$reader.ReadToEnd()|ConvertFrom-Json}finally{$reader.Dispose()}
        Assert-GuestVmKeys $stage @('schema_version','source_commit','installation_id','compressed_sha256','compressed_bytes','vhdx_sha256','vhdx_bytes')
        if(($stage.schema_version -isnot [long] -and $stage.schema_version -isnot [int]) -or $stage.schema_version -ne 1 -or $stage.source_commit -isnot [string] -or $stage.installation_id -isnot [string] -or $stage.vhdx_sha256 -isnot [string] -or $stage.source_commit -cne $spec.stage_source_commit -or $stage.installation_id -cne $spec.installation_id -or $stage.vhdx_sha256 -cnotmatch '^[0-9a-f]{64}$' -or ($stage.vhdx_bytes -isnot [long] -and $stage.vhdx_bytes -isnot [int]) -or $stage.vhdx_bytes -lt 1048576 -or $stage.vhdx_bytes -gt 8589934592){throw 'guest_vm_stage_changed'}
        $guestRoot=Join-Path $root ('guest-'+$spec.node);$diskPath=Join-Path $guestRoot ('guest-'+$spec.node+'.vhdx');$isoPath=Join-Path $root ('seed-'+$spec.node+'.iso');$journal=Join-Path $root ('vm-'+$spec.node+'-journal.jsonl');$preparedPath=Join-Path $root ('vm-'+$spec.node+'-prepared-receipt.json')
        if($Mode -ceq 'start'){
            $records=Read-GuestVmJournal $journal $JournalSha256;$intent=$records[0].data
            if($intent.manifest_sha256 -cne $ManifestSha256 -or $intent.installation_id -cne $spec.installation_id){throw 'guest_vm_intent_changed'}
            $pins=$records[-1].data
            $prepared=Read-GuestVmPreparedReceipt $preparedPath $PreparedReceiptSha256
            if(($prepared.pins|ConvertTo-Json -Depth 8 -Compress) -cne ($pins|ConvertTo-Json -Depth 8 -Compress)){throw 'guest_vm_prepared_changed'}
            if($spec.node -ceq 'B'){
                $peer=Get-GuestVmPeer $root $spec
                if($pins.switch_id -cne $peer.pins.switch_id -or $pins.switch_owner_manifest_sha256 -cne $peer.pins.manifest_sha256){throw 'guest_vm_peer_switch_changed'}
            }
            $vm=Assert-GuestVmPrepared $spec $names $pins $guestRoot $diskPath $isoPath
            $os=Get-CimInstance Win32_OperatingSystem
            if([long]$os.FreePhysicalMemory -lt 5242880){throw 'guest_vm_memory_reserve_failed'}
            $null=Assert-GuestVmReaderReady $root $spec $pins
            # CreateNew is durable before power-on; ambiguous outcomes cannot be retried.
            $request=Join-Path $root ('vm-'+$spec.node+'-start-request.json')
            Write-GuestVmRecord $request ([ordered]@{manifest_sha256=$ManifestSha256;journal_sha256=$JournalSha256;prepared_receipt_sha256=$PreparedReceiptSha256;reader_ready_sha256=$ReaderReadySha256;reader_source_commit=$ReaderSourceCommit;vm_id=$pins.vm_id;installation_id=$spec.installation_id}) $true
            $script:guestVmJournal=$journal;$script:guestVmSequence=$records.Count
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
        $isoStream=Open-GuestVmPinned $isoPath $spec.seed_iso_sha256 $spec.seed_iso_bytes
        Assert-GuestVmFresh $spec $names
        $peer=$null
        if($spec.node -ceq 'B'){$peer=Get-GuestVmPeer $root $spec}
        if((Test-Path -LiteralPath $guestRoot) -or (Test-Path -LiteralPath $journal) -or (Test-Path -LiteralPath $preparedPath)){throw 'guest_vm_existing_partial_manual_reconcile'}
        $base=Join-Path $root 'base.vhdx';$baseStream=Open-GuestVmPinned $base $stage.vhdx_sha256 $stage.vhdx_bytes
        Assert-GuestVmDisk (Get-VHD -Path $base)
        $copySource=$baseStream;$copyHash=$stage.vhdx_sha256;$copyBytes=$stage.vhdx_bytes;$copySourcePath=$base
        if($spec.schema_version -eq 2){
            $nodeStream=Open-GuestVmNodeImage $root $spec;$copySource=$nodeStream;$copyHash=$spec.node_image.sha256;$copyBytes=$spec.node_image.bytes;$copySourcePath=Join-Path $root $spec.node_image.filename
            if((ConvertTo-GuestVmDiskIdentifier $spec.node_image.disk_identifier) -ceq (ConvertTo-GuestVmDiskIdentifier (Get-VHD -Path $base).DiskIdentifier) -or ($spec.node -ceq 'B' -and (ConvertTo-GuestVmDiskIdentifier $spec.node_image.disk_identifier) -ceq (ConvertTo-GuestVmDiskIdentifier $peer.pins.disk_identifier))){throw 'guest_vm_node_image_identifier_collision'}
        }
        $drive=[IO.DriveInfo]::new([IO.Path]::GetPathRoot($root));if($drive.AvailableFreeSpace -lt (25769803776+10737418240)){throw 'guest_vm_disk_reserve_failed'}
        $script:guestVmJournal=$journal;$script:guestVmSequence=1
        Write-GuestVmRecord $journal ([ordered]@{sequence=1;event='intent';data=@{installation_id=$spec.installation_id;source_commit=$spec.source_commit;stage_source_commit=$spec.stage_source_commit;seed_source_commit=$spec.seed_source_commit;node=$spec.node;manifest_sha256=$ManifestSha256;stage_manifest_sha256=$spec.stage_manifest_sha256;seed_sha256=$spec.seed_iso_sha256;vm_name=$names.vm;switch_name=$names.switch;config_path=$guestRoot;disk_path=$diskPath;com1_pipe=$names.pipe;egress_mac_address=$spec.egress_mac_address;peer_prepared_receipt_sha256=$spec.peer_prepared_receipt_sha256}}) $true
        Add-GuestVmEvent 'create_private_directory' @{};New-GuestVmPrivateDirectory $guestRoot
        $copyEvidence=@{}
        if($spec.schema_version -eq 2){$copyEvidence=@{creation_method=$spec.creation_method;node_image=$spec.node_image}}
        Add-GuestVmEvent 'copy_independent_disk' $copyEvidence
        $copy=[IO.File]::Open($diskPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        try{$copySource.CopyTo($copy,1048576);$copy.Flush($true)}finally{$copy.Dispose()}
        $copied=Open-GuestVmPinned $diskPath $copyHash $copyBytes;$copied.Dispose()
        if((Get-GuestVmFileIdentity $diskPath) -ceq (Get-GuestVmFileIdentity $copySourcePath) -or (Get-GuestVmFileIdentity $diskPath) -ceq (Get-GuestVmFileIdentity $base)){throw 'guest_vm_copy_file_identity_collision'}
        $disk=Get-VHD -Path $diskPath;Assert-GuestVmDisk $disk
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
        Grant-GuestVmDataAccess $diskPath $vmSid $true
        Grant-GuestVmDataAccess $isoPath $vmSid $false
        $pins=[ordered]@{installation_id=$spec.installation_id;source_commit=$spec.source_commit;stage_source_commit=$spec.stage_source_commit;seed_source_commit=$spec.seed_source_commit;node=$spec.node;manifest_sha256=$ManifestSha256;stage_manifest_sha256=$spec.stage_manifest_sha256;vm_id=$vm.Id.ToString();vm_name=$names.vm;switch_id=$switch.Id.ToString();switch_name=$names.switch;config_path=$vm.Path;disk_path=$diskPath;disk_sha256=$diskHash;disk_identity=(Get-GuestVmFileIdentity $diskPath $vmSid $true);disk_identifier=(ConvertTo-GuestVmDiskIdentifier $disk.DiskIdentifier);seed_path=$isoPath;seed_sha256=$spec.seed_iso_sha256;seed_identity=(Get-GuestVmFileIdentity $isoPath $vmSid $false);mac_address=$spec.mac_address;com1_pipe=$names.pipe;optional_egress_nic=$spec.optional_egress_nic;egress_mac_address=$spec.egress_mac_address;peer_prepared_receipt_sha256=$spec.peer_prepared_receipt_sha256;switch_owner_manifest_sha256=$switchOwner}
        if($spec.node -ceq 'B'){$peer=Get-GuestVmPeer $root $spec;if($peer.pins.switch_id -cne $pins.switch_id -or $peer.pins.manifest_sha256 -cne $switchOwner){throw 'guest_vm_peer_switch_changed'}}
        $null=Assert-GuestVmPrepared $spec $names ([pscustomobject]$pins) $guestRoot $diskPath $isoPath
        Add-GuestVmEvent 'prepared' $pins
        Write-GuestVmRecord $preparedPath ([ordered]@{schema_version=1;prepared=$true;pins=$pins}) $true
        Assert-GuestVmPrivate $preparedPath
        [ordered]@{scope='fresh_owned_guest';node=$spec.node;vm_id=$vm.Id.ToString();switch_id=$switch.Id.ToString();manifest_sha256=$ManifestSha256;journal_sha256=(Get-GuestVmHash ([IO.File]::ReadAllBytes($journal)));prepared_receipt_sha256=(Get-GuestVmHash ([IO.File]::ReadAllBytes($preparedPath)));prepared=$true;started=$false;production_transition=$false}|ConvertTo-Json -Compress
    }finally{if($null -ne $nodeStream){$nodeStream.Dispose()};if($null -ne $baseStream){$baseStream.Dispose()};if($null -ne $isoStream){$isoStream.Dispose()};$stageStream.Dispose()}
}
if($MyInvocation.InvocationName -ne '.'){
    try{Invoke-GuestVm}catch{throw 'guest_vm_rejected_preserve_partial_manual_reconcile'}
}
