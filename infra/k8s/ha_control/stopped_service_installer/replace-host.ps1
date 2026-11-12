param(
    [Parameter(Mandatory=$true)][string]$Request,
    [Parameter(Mandatory=$true)][string]$RequestSha256,
    [Parameter(Mandatory=$true)][string]$SupportSha256,
    [switch]$Go
)

$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$phase='native_intent'
$intent=$false
$lease=$null
$hostLease=$null
$auditLease=$null

function Get-Hash([byte[]]$Bytes) {
    $sha=[Security.Cryptography.SHA256]::Create()
    try { [BitConverter]::ToString($sha.ComputeHash($Bytes)).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Get-CheckedFile([string]$Path,[string]$Expected,[string]$Reader='') {
    Assert-PreflightEntry (Get-Item -LiteralPath $Path -Force) $writers ($writers+@($Reader)) $true $false
    if(@(Get-Item -LiteralPath $Path -Stream * | Where-Object Stream -cne ':$DATA').Count){throw 'alternate_stream'}
    $held=[TrustedStoppedV1.Held]::new($Path,$false)
    try {
        if((Get-Hash $held.Bytes) -cne $Expected){throw 'file_hash'}
        $held.Verify()
        return ,$held.Bytes
    } finally { $held.Dispose() }
}

function Read-StoppedService {
    $api=[TrustedStoppedV1.Native]::new()
    try {
        $api.OpenForRead($name)
        [TrustedStoppedV1.Contract]::Verify($api.Read($name),$name,('"'+$target+'"'))
    } finally { $api.Dispose() }
    $service=Get-CimInstance Win32_Service -Filter ("Name='"+$name+"'") -OperationTimeoutSec 5
    if($null -eq $service -or $service.State -cne 'Stopped' -or $service.ProcessId -ne 0 -or
       $service.StartMode -cne 'Manual' -or $service.ServiceType -cne 'Own Process' -or $service.DesktopInteract -or
       $service.StartName -ine ('NT SERVICE\'+$name) -or $service.PathName -cne ('"'+$target+'"')){throw 'service_state'}
    if([HaNamespacePrivilegeV1]::ServiceSidType($name) -ne 1){throw 'service_sid_type'}
    $config=[ordered]@{name=$service.Name;start_name=$service.StartName;path_name=$service.PathName;
        start_mode=$service.StartMode;service_type=$service.ServiceType;desktop_interact=[bool]$service.DesktopInteract}
    $cfg=Get-Hash ([Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject $config -Depth 12 -Compress)))
    $priv=[HaNamespacePrivilegeV1]::new()
    try { $sd=[HaNamespacePrivilegeV1]::ServiceSecurity($name) } finally { $priv.Dispose() }
    [TrustedStoppedV1.Contract]::VerifySecurityDescriptor($name,$sd)
    if($cfg -cne $reservation.service_config_sha256 -or
       (Get-Hash $sd) -cne $reservation.service_sd_sha256){throw 'service_pin'}
}

function Write-PrivateRecord([string]$Leaf,$Value) {
    $bytes=[Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject $Value -Depth 6 -Compress)+"`n")
    [TrustedStoppedV1.NewPrivate]::File((Join-Path $audit $Leaf),$bytes,'',$true)
}

try {
    if(-not $Go -or [Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or
       -not [Environment]::Is64BitProcess -or $PSVersionTable.PSEdition -cne 'Desktop'){throw 'native_required'}
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        if(-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'administrator_required'}
    } finally { $identity.Dispose() }

    $phase='input'
    if($Request.Length -gt 16384 -or $RequestSha256 -cnotmatch '^[0-9a-f]{64}$' -or $SupportSha256 -cnotmatch '^[0-9a-f]{64}$'){throw 'input_pin'}
    $raw=[Convert]::FromBase64String($Request)
    if((Get-Hash $raw) -cne $RequestSha256){throw 'input_hash'}
    $parent=Split-Path -Parent $PSScriptRoot
    $supportPaths=@((Join-Path $parent 'guest-owned-profile.ps1'),(Join-Path $parent 'windows-service-preflight.ps1'),
        (Join-Path $PSScriptRoot 'StoppedService.cs'),(Join-Path $PSScriptRoot 'PrivateIo.cs'),
        (Join-Path $PSScriptRoot 'ProductServiceSecurity.cs'),(Join-Path $PSScriptRoot 'StrictJson.cs'))
    $parts=@(foreach($path in $supportPaths){,[IO.File]::ReadAllBytes($path)})
    $encoding=[Text.UTF8Encoding]::new($false,$true)
    $hashes=foreach($part in $parts){Get-Hash $part}
    if((Get-Hash ($encoding.GetBytes(($hashes -join "`n")))) -cne $SupportSha256){throw 'support_pin'}
    . ([ScriptBlock]::Create($encoding.GetString($parts[0])))
    $ast=[Management.Automation.Language.Parser]::ParseInput($encoding.GetString($parts[1]),[ref]$null,[ref]$null)
    foreach($fn in $ast.FindAll({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst]},$false)){
        . ([ScriptBlock]::Create($fn.Extent.Text))
    }
    $using='using System;using System.IO;using System.Text;using System.Collections.Generic;using System.Runtime.InteropServices;using System.Security.AccessControl;using System.Security.Cryptography;using Microsoft.Win32.SafeHandles;'
    $units=foreach($i in 2..5){[regex]::Replace($encoding.GetString($parts[$i]),'using [A-Za-z0-9_.]+;','')}
    Add-Type -TypeDefinition ($using+"`n"+($units -join "`n"))
    $data=[TrustedStoppedV1.Json]::Object($encoding.GetString($raw),12288)
    $keys=@('installation_id','root_profile','executor_source_commit','executor_script_sha256',
        'reservation_receipt_sha256','reservation_root_identity','new_host_sha256','source_commit','transfer_name')
    $hasPredecessor=$data.ContainsKey('previous_request_sha256')
    if($hasPredecessor){$keys+=@('previous_request_sha256','previous_intent_sha256')}
    if($data.Count -ne $keys.Count){throw 'input_shape'}
    foreach($key in $keys){if(-not $data.ContainsKey($key) -or $data[$key] -isnot [string]){throw 'input_shape'}}
    if($data['new_host_sha256'] -cnotmatch '^[0-9a-f]{64}$' -or $data['source_commit'] -cnotmatch '^[0-9a-f]{40}$' -or
       $data['transfer_name'] -cnotmatch '^incoming-[0-9a-f]{32}$'){throw 'input_shape'}
    $profile=[pscustomobject]$data
    Assert-GuestOwnedDeclaration $profile ''
    $sid=Get-GuestOwnedServiceSid $profile.installation_id
    $stem='N100HA_'+$profile.installation_id.Substring(0,24)
    $name=$stem+'_controller'
    $root=Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)) $stem
    $writers=@('S-1-5-18','S-1-5-32-544')
    $lease=Open-GuestOwnedRoot $root $profile ''
    $reservation=Read-GuestOwnedPinned (Join-Path $root 'reservation-receipt.json') $profile.reservation_receipt_sha256
    $ownership=Read-GuestOwnedPinned (Join-Path $root 'namespace-service-ownership.json') $reservation.ownership_receipt_sha256
    $oldHash=$ownership.service_image_sha256
    if($hasPredecessor){
        $phase='predecessor'
        if($profile.previous_request_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
           $profile.previous_intent_sha256 -cnotmatch '^[0-9a-f]{64}$'){throw 'predecessor_pin'}
        $priorRoot=Join-Path $root ('host-upgrade-'+$profile.previous_request_sha256.Substring(0,24))
        Assert-PreflightEntry (Get-Item -LiteralPath $priorRoot -Force) $writers $writers $true $false
        $priorLease=[TrustedStoppedV1.Held]::new($priorRoot,$true)
        try {
            $prior=Read-GuestOwnedPinned (Join-Path $priorRoot 'intent.json') $profile.previous_intent_sha256
            if($prior.schema_version -ne 1 -or $prior.installation_id -cne $profile.installation_id -or
               $prior.request_sha256 -cne $profile.previous_request_sha256 -or
               $prior.reservation_sha256 -cne $profile.reservation_receipt_sha256 -or
               $prior.ownership_sha256 -cne $reservation.ownership_receipt_sha256 -or
               $prior.old_host_sha256 -cne $oldHash -or $prior.new_host_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
               $prior.service_config_sha256 -cne $reservation.service_config_sha256 -or
               $prior.service_sd_sha256 -cne $reservation.service_sd_sha256){throw 'predecessor_binding'}
            $null=Get-CheckedFile (Join-Path $priorRoot 'original.exe') $oldHash
            $oldHash=$prior.new_host_sha256
            $priorLease.Verify()
        } finally { $priorLease.Dispose() }
    }
    $newHash=$profile.new_host_sha256
    if($oldHash -ceq $newHash){throw 'same_image'}

    $phase='preflight'
    $hostRoot=Join-Path $root 'host'
    $target=Join-Path $hostRoot 'windows-service-host.exe'
    Assert-PreflightEntry (Get-Item -LiteralPath $hostRoot -Force) $writers ($writers+@($sid)) $true $false
    $hostLease=[TrustedStoppedV1.Held]::new($hostRoot,$true)
    $items=@(Get-ChildItem -LiteralPath $hostRoot -Force)
    if($items.Count -ne 1 -or $items[0].Name -cne 'windows-service-host.exe'){throw 'host_inventory'}
    $oldAttributes=[IO.File]::GetAttributes($target)
    if(-not ($oldAttributes -band [IO.FileAttributes]::ReadOnly)){throw 'host_attributes'}
    $oldAcl=(Get-Acl -LiteralPath $target).Sddl
    $oldBytes=Get-CheckedFile $target $oldHash $sid
    Read-StoppedService
    $transfer=Join-Path $root $profile.transfer_name
    Assert-PreflightEntry (Get-Item -LiteralPath $transfer -Force) $writers $writers $true $false
    $transferLease=[TrustedStoppedV1.Held]::new($transfer,$true)
    try {
        $candidate=Get-CheckedFile (Join-Path $transfer 'candidate.exe') $newHash
        $transferLease.Verify()
    } finally { $transferLease.Dispose() }

    $phase='intent'
    $audit=Join-Path $root ('host-upgrade-'+$RequestSha256.Substring(0,24))
    [TrustedStoppedV1.NewPrivate]::Directory($audit,'',$false)
    $intent=$true
    $auditLease=[TrustedStoppedV1.Held]::new($audit,$true)
    $replacement=Join-Path $audit 'replacement.exe'
    $backup=Join-Path $audit 'original.exe'
    [TrustedStoppedV1.NewPrivate]::File($backup,$oldBytes,'',$true)
    $null=Get-CheckedFile $backup $oldHash
    [TrustedStoppedV1.NewPrivate]::File($replacement,$candidate,$sid,$false)
    $null=Get-CheckedFile $replacement $newHash $sid
    $record=[ordered]@{schema_version=1;source_commit=$profile.source_commit;installation_id=$profile.installation_id;
        request_sha256=$RequestSha256;reservation_sha256=$profile.reservation_receipt_sha256;
        ownership_sha256=$reservation.ownership_receipt_sha256;old_host_sha256=$oldHash;new_host_sha256=$newHash;
        service_config_sha256=$reservation.service_config_sha256;service_sd_sha256=$reservation.service_sd_sha256;
        old_host_acl_sha256=(Get-Hash ($encoding.GetBytes($oldAcl)));old_host_attributes=[int]$oldAttributes}
    if($hasPredecessor){$record['previous_intent_sha256']=$profile.previous_intent_sha256}
    Write-PrivateRecord 'intent.json' $record
    $phase='replace'
    $lease.AssertCurrent()
    $hostLease.Verify()
    $auditLease.Verify()
    Read-StoppedService
    $null=Get-CheckedFile $target $oldHash $sid
    try {
        $writable=$oldAttributes -band (-bnot [IO.FileAttributes]::ReadOnly)
        if(-not $writable){$writable=[IO.FileAttributes]::Normal}
        [IO.File]::SetAttributes($target,$writable)
        [IO.File]::Replace($replacement,$target,[NullString]::Value,$false)
    } finally {
        if([IO.File]::Exists($target)){[IO.File]::SetAttributes($target,$oldAttributes)}
    }

    $phase='readback'
    $null=Get-CheckedFile $target $newHash $sid
    $null=Get-CheckedFile $backup $oldHash
    $items=@(Get-ChildItem -LiteralPath $hostRoot -Force)
    if($items.Count -ne 1 -or $items[0].Name -cne 'windows-service-host.exe' -or
       (Get-Acl -LiteralPath $target).Sddl -cne $oldAcl -or
       [IO.File]::GetAttributes($target) -ne $oldAttributes){throw 'host_metadata_changed'}
    Read-StoppedService
    $lease.AssertCurrent()
    $hostLease.Verify()
    $auditLease.Verify()
    $record['scope']='stopped_service_host_replacement'
    $record['service_started']=$false
    $record['authority_issued']=$false
    Write-PrivateRecord 'receipt.json' $record
    $record|ConvertTo-Json -Compress
} catch {
    [ordered]@{scope='stopped_service_host_replacement';status='BLOCKED';failed_stage=$phase;
        reconcile_required=$intent;service_started=$false;authority_issued=$false}|ConvertTo-Json -Compress
    exit 1
} finally {
    if($null -ne $auditLease){$auditLease.Dispose()}
    if($null -ne $hostLease){$hostLease.Dispose()}
    if($null -ne $lease){$lease.Dispose()}
}
