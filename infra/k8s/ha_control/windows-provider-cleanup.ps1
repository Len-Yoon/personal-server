param([string]$InstallationId,[string]$ExpectedJournalSourceCommit,[string]$ArtifactSha256,[switch]$Go)
# Recovery only for this probe's pre-operation, never-started fixture failure.
# Never starts/stops a service or VM, deletes files, or changes namespace ACLs.
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
function Get-CleanupHash([byte[]]$Bytes){$h=[Security.Cryptography.SHA256]::Create();try{return [BitConverter]::ToString($h.ComputeHash($Bytes)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}}
function Assert-CleanupPrivate([string]$Path){
    $item=Get-Item -LiteralPath $Path -Force
    if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'cleanup_reparse_rejected'}
    $acl=Get-Acl -LiteralPath $Path
    $sd=[Security.AccessControl.RawSecurityDescriptor]::new($acl.Sddl)
    if($sd.Owner.Value -notin @('S-1-5-18','S-1-5-32-544') -or $null -eq $sd.DiscretionaryAcl -or $sd.DiscretionaryAcl.Count -eq 0){throw 'cleanup_private_owner_required'}
    foreach($ace in $sd.DiscretionaryAcl){
        if($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback){throw 'cleanup_acl_rejected'}
        if($ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed -and
            (([long]$ace.AccessMask -band 4294967295) -band 1343029590) -ne 0 -and
            $ace.SecurityIdentifier.Value -notin @('S-1-5-18','S-1-5-32-544')){throw 'cleanup_untrusted_writer'}
    }
}
function Test-CleanupServicePath([string]$Actual,[string]$Expected){
    return (-not [string]::IsNullOrEmpty($Expected) -and -not $Expected.Contains('"') -and
        ($Actual -ceq ('"'+$Expected+'"') -or ($Expected -notmatch '\s' -and $Actual -ceq $Expected)))
}
function Test-CleanupDisconnectedNic($Nic){
    if($null -eq $Nic -or $null -eq $Nic.PSObject.Properties['SwitchName'] -or $null -eq $Nic.PSObject.Properties['SwitchId']){return $false}
    if(($null -ne $Nic.SwitchName -and $Nic.SwitchName -isnot [string]) -or
        ($null -ne $Nic.SwitchId -and $Nic.SwitchId -isnot [string] -and $Nic.SwitchId -isnot [Guid])){return $false}
    return ([string]::IsNullOrEmpty([string]$Nic.SwitchName) -and
        ([string]::IsNullOrEmpty([string]$Nic.SwitchId) -or [string]$Nic.SwitchId -ieq [Guid]::Empty.ToString()))
}
function Get-CleanupExpectedNames([string]$Id){
    if($Id -cnotmatch '^[0-9a-f]{64}$'){throw 'cleanup_identity_rejected'}
    return @(('HaProbe_'+$Id.Substring(0,16)),('HaProbe_'+$Id.Substring(16,16)))
}
function Invoke-ProviderCleanup{
    if(-not $Go -or $InstallationId -cnotmatch '^[0-9a-f]{64}$' -or $ExpectedJournalSourceCommit -cnotmatch '^[0-9a-f]{40}$' -or
        $ArtifactSha256 -cnotmatch '^[0-9a-f]{64}$' -or [Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or -not [Environment]::Is64BitProcess){throw 'cleanup_intent_required'}
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    try{if(-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))){throw 'cleanup_trusted_admin_required'}}finally{$identity.Dispose()}
    $base=[Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
    if((Get-Item -LiteralPath $base -Force).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'cleanup_parent_reparse'}
    $root=Join-Path $base ('HaProbe_'+$InstallationId.Substring(0,16));Assert-CleanupPrivate $root
    $journal=Join-Path $root 'ownership.json';Assert-CleanupPrivate $journal
    if((Get-Item -LiteralPath $journal).Length -gt 65536){throw 'cleanup_journal_limit'}
    $record=[IO.File]::ReadAllText($journal)|ConvertFrom-Json
    if($record.installation_id -cne $InstallationId -or $record.source_commit -cne $ExpectedJournalSourceCommit -or
        $record.artifact_sha256 -cne $ArtifactSha256 -or $record.phase -cne 'fixture_service_creation' -or
        $record.complete -ne $false -or $record.failure_type -cne 'IndexOutOfRangeException'){throw 'cleanup_pre_operation_proof_required'}
    $names=@(Get-CleanupExpectedNames $InstallationId);$actual=@($record.service_names)
    $legacy=($actual.Count -eq 1 -and $actual[0] -ceq ($names -join ' '))
    if(-not $legacy -and ($actual.Count -ne 2 -or ($actual -join ',') -cne ($names -join ','))){throw 'cleanup_names_not_owned'}
    if(([Guid]::Parse($record.vm_id)).ToString() -cne $record.vm_id){throw 'cleanup_vm_identity_rejected'}
    $receipt=Join-Path $root 'cleanup-reconciliation.json'
    if(Test-Path -LiteralPath $receipt){throw 'cleanup_receipt_exists_reconcile_only'}
    $vm=Get-VM -Id $record.vm_id
    if($vm.Name -cne ('HaProbeVM_'+$InstallationId.Substring(0,16)) -or $vm.Notes -cne ('ha-owned-provider-probe:'+$InstallationId) -or
        $vm.State -ne 'Off' -or $vm.MemoryAssigned -ne 0 -or @(Get-VMHardDiskDrive -VM $vm).Count -ne 0 -or
        @(Get-VMNetworkAdapter -VM $vm|Where-Object{-not (Test-CleanupDisconnectedNic $_)}).Count -ne 0){throw 'cleanup_off_owned_diskless_vm_required'}
    $configuration=[IO.Path]::GetFullPath([string]$vm.ConfigurationLocation)
    if($configuration -ine ($root+'\vm') -and -not $configuration.StartsWith(($root+'\vm\'),[StringComparison]::OrdinalIgnoreCase)){throw 'cleanup_vm_storage_not_owned'}
    $targets=@()
    for($i=0;$i -lt $actual.Count;$i++){
        $name=$actual[$i];$service=Get-CimInstance Win32_Service -Filter ("Name='"+$name+"'")
        if($null -eq $service){continue}
        $dir=Join-Path $root ('service'+$i);$exe=Join-Path $dir 'probe.exe';Assert-CleanupPrivate $dir;Assert-CleanupPrivate $exe
        if($service.State -cne 'Stopped' -or $service.ProcessId -ne 0 -or $service.ExitCode -ne 1077 -or
            $service.StartName -ine ('NT SERVICE\'+$name) -or -not (Test-CleanupServicePath $service.PathName $exe) -or
            (Get-CleanupHash ([IO.File]::ReadAllBytes($exe))) -cne $ArtifactSha256){throw 'cleanup_never_started_owned_service_required'}
        $sid=([Security.Principal.NTAccount]::new('NT SERVICE\'+$name)).Translate([Security.Principal.SecurityIdentifier]).Value
        if($sid -in @(Get-LocalGroupMember -SID 'S-1-5-32-578'|ForEach-Object{$_.SID.Value})){throw 'cleanup_unexpected_privilege'}
        $targets+=@($name)
    }
    $before=@(Get-VM|Where-Object{$_.Id.ToString() -cne $record.vm_id}|ForEach-Object{$_.Id.ToString()}|Sort-Object)
    # Preflight of every target completes before deleting either resource.
    foreach($name in $targets){
        $null=& (Join-Path $env:SystemRoot 'System32\sc.exe') delete $name
        if($LASTEXITCODE -ne 0){throw 'cleanup_service_delete_failed'}
        $watch=[Diagnostics.Stopwatch]::StartNew()
        do{$remaining=Get-CimInstance Win32_Service -Filter ("Name='"+$name+"'");if($null -eq $remaining){break};Start-Sleep -Milliseconds 100}while($watch.Elapsed.TotalSeconds -lt 5)
        if($null -ne $remaining){throw 'cleanup_service_delete_unverified'}
    }
    # Refresh immutable ownership and Off state immediately before removal.
    $vm=Get-VM -Id $record.vm_id
    if($vm.Notes -cne ('ha-owned-provider-probe:'+$InstallationId) -or $vm.State -ne 'Off' -or $vm.MemoryAssigned -ne 0){throw 'cleanup_vm_changed'}
    Remove-VM -VM $vm -Force
    $after=@(Get-VM|ForEach-Object{$_.Id.ToString()}|Sort-Object)
    if(($before -join ',') -cne ($after -join ',')){throw 'cleanup_vm_inventory_changed'}
    $result=[ordered]@{scope='never_started_owned_provider_fixture_cleanup';installation_id=$InstallationId;target_source_commit=$ExpectedJournalSourceCommit;
        artifact_sha256=$ArtifactSha256;deleted_services=$targets.Count;deleted_off_diskless_vm=1;complete=$true;original_failure_preserved=$true;production_transition=$false}
    $bytes=[Text.Encoding]::UTF8.GetBytes(($result|ConvertTo-Json -Depth 4 -Compress))
    $stream=[IO.File]::Open($receipt,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try{$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
    $result|ConvertTo-Json -Depth 4 -Compress
}
if($MyInvocation.InvocationName -ne '.'){Invoke-ProviderCleanup}
