param([ValidateSet('prepare','expand')][string]$Mode,[string]$Manifest,[string]$ManifestSha256,[switch]$Go)
# Private fresh-guest staging only. No VM, switch, service, DB or reboot action.
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$script:guestOwnedSpec=$null;$script:guestOwnedRoot=''
function Import-GuestOwnedSupport{
 $path=Join-Path $PSScriptRoot 'guest-owned-profile.ps1';$bytes=[IO.File]::ReadAllBytes($path)
 if((Get-GuestStageHash $bytes) -cne '437d02933c2800cea4097a79ee14db3052e8c522c15f132a52c7c094d6f84045'){throw 'guest_owned_support_changed'}
 return [scriptblock]::Create([Text.UTF8Encoding]::new($false,$true).GetString($bytes))
}
function Get-GuestStageHash([byte[]]$Bytes){$h=[Security.Cryptography.SHA256]::Create();try{return [BitConverter]::ToString($h.ComputeHash($Bytes)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}}
function Get-GuestStageStreamHash($Stream){$h=[Security.Cryptography.SHA256]::Create();try{return [BitConverter]::ToString($h.ComputeHash($Stream)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}}
function Assert-GuestStageKeys($Value,[string[]]$Keys){
    if((($Value.PSObject.Properties.Name|Sort-Object -CaseSensitive)-join ',') -cne (($Keys|Sort-Object -CaseSensitive)-join ',')){throw 'guest_stage_shape_rejected'}
}
function Read-GuestStageManifest{
    if($ManifestSha256 -cnotmatch '^[0-9a-f]{64}$' -or $Manifest.Length -gt 32768){throw 'guest_stage_pin_required'}
    $raw=[Convert]::FromBase64String($Manifest)
    if((Get-GuestStageHash $raw) -cne $ManifestSha256){throw 'guest_stage_pin_changed'}
    $value=([Text.UTF8Encoding]::new($false,$true)).GetString($raw)|ConvertFrom-Json
    $keys=@('schema_version','source_commit','installation_id','compressed_sha256','compressed_bytes','vhdx_sha256','vhdx_bytes')
    if($value.schema_version -eq 2){
        $text=[Text.UTF8Encoding]::new($false,$true).GetString($raw);$names=@([regex]::Matches($text,'"([A-Za-z_][A-Za-z0-9_]*)"\s*:')|ForEach-Object{$_.Groups[1].Value})
        if($text.Contains('\') -or @($names|Sort-Object -Unique).Count -ne $names.Count){throw 'guest_stage_duplicate_or_escaped_keys'}
        . (Import-GuestOwnedSupport);$keys+=@(Get-GuestOwnedFields);Assert-GuestOwnedDeclaration $value ''}
    Assert-GuestStageKeys $value $keys
    if(($value.schema_version -isnot [int] -and $value.schema_version -isnot [long]) -or $value.schema_version -notin @(1,2) -or $value.source_commit -isnot [string] -or $value.source_commit -cnotmatch '^[0-9a-f]{40}$' -or
        $value.installation_id -isnot [string] -or $value.installation_id -cnotmatch '^[0-9a-f]{64}$'){throw 'guest_stage_identity_rejected'}
    foreach($key in @('compressed_sha256','vhdx_sha256')){if($value.$key -isnot [string] -or $value.$key -cnotmatch '^[0-9a-f]{64}$'){throw 'guest_stage_hash_rejected'}}
    foreach($key in @('compressed_bytes','vhdx_bytes')){if(($value.$key -isnot [int] -and $value.$key -isnot [long]) -or $value.$key -lt 1048576){throw 'guest_stage_size_rejected'}}
    if($value.compressed_bytes -gt 1073741824 -or $value.vhdx_bytes -gt 8589934592 -or $value.compressed_bytes -ge $value.vhdx_bytes){throw 'guest_stage_size_rejected'}
    return $value
}
function New-GuestStageDirectory([string]$Path){
    if(Test-Path -LiteralPath $Path){throw 'guest_stage_path_exists'}
    $acl=[Security.AccessControl.DirectorySecurity]::new();$acl.SetAccessRuleProtection($true,$false)
    $acl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    foreach($sid in @('S-1-5-18','S-1-5-32-544')){
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
    }
    return [IO.Directory]::CreateDirectory($Path,$acl)
}
function Assert-GuestStagePrivate([string]$Path){
    if($null -ne $script:guestOwnedSpec -and $Path -ieq $script:guestOwnedRoot){Assert-GuestOwnedRootAcl $Path $script:guestOwnedSpec;return}
    $entry=Get-Item -LiteralPath $Path -Force
    if($entry.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'guest_stage_reparse_rejected'}
    $sd=[Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $Path).Sddl)
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    try{$allowed=@('S-1-5-18','S-1-5-32-544',$identity.User.Value)}finally{$identity.Dispose()}
    if($sd.Owner.Value -notin $allowed -or $null -eq $sd.DiscretionaryAcl -or $sd.DiscretionaryAcl.Count -eq 0){throw 'guest_stage_owner_rejected'}
    foreach($ace in $sd.DiscretionaryAcl){
        if($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback){throw 'guest_stage_acl_rejected'}
        if($ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed -and $ace.SecurityIdentifier.Value -notin $allowed -and $ace.AccessMask -ne 0){throw 'guest_stage_access_rejected'}
    }
}
function Expand-GuestStageStream($CompressedStream,$ExpandedStream,[long]$ExpectedBytes,[string]$ExpectedSha256){
    if($ExpectedBytes -lt 1 -or $ExpectedBytes -gt 8589934592 -or $ExpectedSha256 -cnotmatch '^[0-9a-f]{64}$'){throw 'guest_stage_expansion_pin_required'}
    $gzip=[IO.Compression.GZipStream]::new($CompressedStream,[IO.Compression.CompressionMode]::Decompress,$true)
    try{
        $buffer=New-Object byte[] 1048576;$count=[long]0
        while(($read=$gzip.Read($buffer,0,$buffer.Length)) -gt 0){
            if($read -gt ($ExpectedBytes-$count)){throw 'guest_stage_expansion_limit'}
            $ExpandedStream.Write($buffer,0,$read);$count+=$read
        }
    }finally{$gzip.Dispose()}
    $ExpandedStream.Flush()
    if($count -ne $ExpectedBytes){throw 'guest_stage_expansion_size_changed'}
    $ExpandedStream.Position=0
    if((Get-GuestStageStreamHash $ExpandedStream) -cne $ExpectedSha256){throw 'guest_stage_expanded_hash_changed'}
}
function Invoke-GuestStage{
    if(-not $Go -or [Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or -not [Environment]::Is64BitProcess){throw 'guest_stage_native_intent_required'}
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    try{if(-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))){throw 'guest_stage_admin_required'}}finally{$identity.Dispose()}
    $spec=Read-GuestStageManifest
    $parent=[Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
    if((Get-Item -LiteralPath $parent -Force).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'guest_stage_parent_reparse'}
    $root=Join-Path $parent ('N100HA_'+$spec.installation_id.Substring(0,24))
    $ownedLease=$null
    if($spec.schema_version -eq 2){. (Import-GuestOwnedSupport);$script:guestOwnedSpec=$spec;$script:guestOwnedRoot=$root;$ownedLease=Open-GuestOwnedRoot $root $spec (Get-GuestStageHash ([IO.File]::ReadAllBytes($PSCommandPath)))}
    try{
    if($Mode -ceq 'prepare'){
        $drive=[IO.DriveInfo]::new([IO.Path]::GetPathRoot($parent))
        if($drive.AvailableFreeSpace -lt ($spec.vhdx_bytes*3+$spec.compressed_bytes+10737418240)){throw 'guest_stage_disk_reserve_failed'}
        if($null -eq $ownedLease){$null=New-GuestStageDirectory $root}else{$ownedLease.AssertCurrent();Assert-GuestOwnedRootAcl $root $spec}
        $bytes=[Convert]::FromBase64String($Manifest)
        $stream=[IO.File]::Open((Join-Path $root 'stage-manifest.json'),[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        try{$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
        [ordered]@{scope='fresh_guest_private_staging';source_commit=$spec.source_commit;installation_id=$spec.installation_id;manifest_sha256=$ManifestSha256;prepared=$true;expanded=$false;vm_created=$false;production_transition=$false}|ConvertTo-Json -Compress
        return
    }
    if($Mode -cne 'expand'){throw 'guest_stage_mode_rejected'}
    Assert-GuestStagePrivate $root
    $manifestPath=Join-Path $root 'stage-manifest.json';Assert-GuestStagePrivate $manifestPath
    if((Get-GuestStageHash ([IO.File]::ReadAllBytes($manifestPath))) -cne $ManifestSha256){throw 'guest_stage_manifest_changed'}
    $source=Join-Path $root 'base.vhdx.gz';Assert-GuestStagePrivate $source
    $target=Join-Path $root 'base.vhdx';$request=Join-Path $root 'expansion-request.json'
    if((Test-Path -LiteralPath $target) -or (Test-Path -LiteralPath $request)){throw 'guest_stage_expansion_already_requested'}
    $compressedStream=[IO.File]::Open($source,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    try{
        if($compressedStream.Length -ne $spec.compressed_bytes -or (Get-GuestStageStreamHash $compressedStream) -cne $spec.compressed_sha256){throw 'guest_stage_archive_changed'}
        $compressedStream.Position=0
        $marker=[Text.Encoding]::UTF8.GetBytes(($spec|ConvertTo-Json -Depth 3 -Compress))
        $file=[IO.File]::Open($request,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        try{$file.Write($marker,0,$marker.Length);$file.Flush($true)}finally{$file.Dispose()}
        $output=[IO.File]::Open($target,[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
        try{
            Expand-GuestStageStream $compressedStream $output $spec.vhdx_bytes $spec.vhdx_sha256
            $output.Flush($true)
        }finally{$output.Dispose()}
    }finally{$compressedStream.Dispose()}
    Assert-GuestStagePrivate $target
    $vhd=Get-VHD -Path $target
    if($vhd.VhdFormat -ne 'VHDX' -or $vhd.VhdType -ne 'Dynamic' -or $vhd.ParentPath -or $vhd.Size -ne 25769803776 -or $vhd.Attached){throw 'guest_stage_independent_disk_required'}
    [ordered]@{scope='fresh_guest_private_staging';source_commit=$spec.source_commit;installation_id=$spec.installation_id;manifest_sha256=$ManifestSha256;
        prepared=$true;expanded=$true;vhdx_sha256=$spec.vhdx_sha256;virtual_bytes=$vhd.Size;vm_created=$false;production_transition=$false}|ConvertTo-Json -Compress
    }finally{if($null -ne $ownedLease){$ownedLease.Dispose()}}
}
if($MyInvocation.InvocationName -ne '.'){Invoke-GuestStage}
