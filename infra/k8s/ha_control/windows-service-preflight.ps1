param([string]$HostRoot,[string]$HostSha256,[string]$ArgsSha256,[string]$ServiceSid,[string[]]$TrustedAdminSids)
# Run from a separately hash-pinned trusted installer BEFORE StartService.
# Does not load the target EXE/CLR config, register or start a service, or set ACLs.
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
function Get-PreflightHash([byte[]]$Bytes){$h=[Security.Cryptography.SHA256]::Create();try{return [BitConverter]::ToString($h.ComputeHash($Bytes)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}}
function Get-PreflightPlatform{return [Environment]::OSVersion.Platform}
function Assert-PreflightMask([string]$Sid,[long]$Mask,[string[]]$Writers,[string[]]$Readers,[bool]$Private,[bool]$Ancestor){
    $mask=$Mask -band 4294967295
    $writeMask=if($Ancestor){269287488}else{1343029590}
    if(($mask -band $writeMask) -ne 0 -and $Sid -notin $Writers){throw 'preflight_writer_rejected'}
    if($Private -and ($mask -band 2415919105) -ne 0 -and $Sid -notin $Readers){throw 'preflight_reader_rejected'}
}
function Get-PreflightOsVolumeRoot{return [IO.Path]::GetPathRoot([Environment]::GetFolderPath([Environment+SpecialFolder]::Windows))}
function Test-PreflightOsRootOwner([string]$Owner,[string]$Path,[bool]$Ancestor){
    return ($Owner -ceq 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464' -and
        $Ancestor -and $Path -cmatch '^[A-Za-z]:\\$' -and $Path -ieq (Get-PreflightOsVolumeRoot))
}
function Assert-PreflightDescriptor($Descriptor,[string[]]$Writers,[string[]]$Readers,[bool]$Private,[bool]$Ancestor,[bool]$OsRootOwner=$false){
    if($null -eq $Descriptor.Owner -or ($Descriptor.Owner.Value -notin $Writers -and
        -not ($OsRootOwner -and $Descriptor.Owner.Value -ceq 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')) -or $null -eq $Descriptor.DiscretionaryAcl -or $Descriptor.DiscretionaryAcl.Count -eq 0){throw 'preflight_acl_rejected'}
    foreach($ace in $Descriptor.DiscretionaryAcl){
        if($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback){throw 'preflight_acl_rejected'}
        if($ace.AceFlags -band [Security.AccessControl.AceFlags]::InheritOnly){continue}
        if($ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed){
            Assert-PreflightMask $ace.SecurityIdentifier.Value ([long]$ace.AccessMask) $Writers $Readers $Private $Ancestor
        }
    }
}
function Assert-PreflightEntry($Entry,$Writers,$Readers,[bool]$Private,[bool]$Ancestor){
    if($Entry.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'preflight_reparse_rejected'}
    $acl=Get-Acl -LiteralPath $Entry.FullName -ErrorAction Stop
    $descriptor=[Security.AccessControl.RawSecurityDescriptor]::new($acl.Sddl)
    Assert-PreflightDescriptor $descriptor $Writers $Readers $Private $Ancestor (Test-PreflightOsRootOwner $descriptor.Owner.Value $Entry.FullName $Ancestor)
}
function Test-EnforcedServicePreflight {
    if((Get-PreflightPlatform) -ne [PlatformID]::Win32NT){throw 'native_preflight_required'}
    if($HostRoot -cnotmatch '^[A-Za-z]:\\' -or $HostRoot.Substring(2).Contains(':') -or $HostRoot -match '(^|\\)\.\.?($|\\)' -or $ServiceSid -cnotmatch '^S-1-5-80-(\d+-){4}\d+$' -or $HostSha256 -cnotmatch '^[0-9a-f]{64}$' -or $ArgsSha256 -cnotmatch '^[0-9a-f]{64}$'){throw 'preflight_input_rejected'}
    $actualAdmins=@(Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction Stop|ForEach-Object {$_.SID.Value}|Sort-Object)
    if(($actualAdmins -join ',') -cne (($TrustedAdminSids|Sort-Object) -join ',')){throw 'preflight_admin_membership_changed'}
    $writers=@('S-1-5-18','S-1-5-32-544')+$actualAdmins
    $readers=$writers+@($ServiceSid)
    $root=Get-Item -LiteralPath $HostRoot -Force -ErrorAction Stop
    if(-not $root.PSIsContainer){throw 'preflight_private_root_required'}
    Assert-PreflightEntry $root $writers $readers $true $false
    $parent=$root.Parent;$depth=0
    while($null -ne $parent){Assert-PreflightEntry $parent $writers $readers ($depth -eq 0) ($depth -gt 0);$parent=$parent.Parent;$depth++}
    # The dedicated host directory contains only these two leaves. In
    # particular EXE.config/AppDomainManager DLLs are rejected before CLR load.
    $items=@(Get-ChildItem -LiteralPath $root.FullName -Force -ErrorAction Stop)
    $names=@($items|ForEach-Object {$_.Name}|Sort-Object -CaseSensitive)
    if(($names -join ',') -cne 'service.args,windows-service-host.exe'){throw 'preflight_inventory_rejected'}
    $pins=@{'windows-service-host.exe'=$HostSha256;'service.args'=$ArgsSha256}
    foreach($item in $items){
        if($item.PSIsContainer -or $item.Length -gt 67108864){throw 'preflight_inventory_rejected'}
        Assert-PreflightEntry $item $writers $readers $true $false
        if((Get-PreflightHash ([IO.File]::ReadAllBytes($item.FullName))) -cne $pins[$item.Name]){throw 'preflight_hash_rejected'}
        Assert-PreflightEntry (Get-Item -LiteralPath $item.FullName -Force) $writers $readers $true $false
    }
    [ordered]@{scope='pre_StartService_inventory_and_ACL';ready=$false;execution_performed=$false;native_service_accepted=$false;host_sha256=$HostSha256;args_sha256=$ArgsSha256}|ConvertTo-Json -Compress
}
if($MyInvocation.InvocationName -ne '.'){Test-EnforcedServicePreflight}
