param(
    [string]$OldVmId,
    [string]$PrimaryVmId,
    [string]$ReplacementVmId,
    [string]$OldDiskPathsSha256,
    [string]$ReplacementDiskPathsSha256,
    [string]$IsolatedSwitchId,
    [string]$ServingSwitchId,
    [string]$ReplacementMac,
    [string]$StageSha256,
    [switch]$ObserveOnly,
    [switch]$Go
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-DiskPaths($Vm, [string]$Expected) {
    [string[]]$paths = @(Get-VMHardDiskDrive -VM $Vm | ForEach-Object {
        $path = [string]$_.Path
        if ($path -cnotmatch '\A[A-Za-z]:\\[\x20-\x7e]+\z') { throw 'replacement_disk_path_unconfirmed' }
        $path.ToLowerInvariant()
    })
    [Array]::Sort($paths,[StringComparer]::Ordinal)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $raw = [Text.Encoding]::UTF8.GetBytes($paths -join [char]10)
        $digest = [BitConverter]::ToString($sha.ComputeHash($raw)).Replace('-','').ToLowerInvariant()
    } finally { $sha.Dispose() }
    if ($paths.Count -lt 1 -or $paths.Count -gt 8 -or
        @($paths | Select-Object -Unique).Count -ne $paths.Count -or $digest -cne $Expected) {
        throw 'replacement_disk_path_unconfirmed'
    }
}

function Test-Disconnected($Nic) {
    return (-not [string]$Nic.SwitchName -and
        (-not [string]$Nic.SwitchId -or [string]$Nic.SwitchId -eq [string][Guid]::Empty))
}

function Read-Nic($Vm) {
    $nics = @(Get-VMNetworkAdapter -VM $Vm)
    $data = @($nics | Where-Object {$_.Name -ceq 'ha-data'})
    $egress = @($nics | Where-Object {$_.Name -ceq 'egress-disconnected'})
    if ($data.Count -ne 1 -or $egress.Count -gt 1 -or $nics.Count -ne ($data.Count + $egress.Count) -or
        @($nics | Where-Object {$_.DynamicMacAddressEnabled -or [string]$_.MacAddressSpoofing -cne 'Off'}).Count -ne 0 -or
        ($egress.Count -eq 1 -and -not (Test-Disconnected $egress[0]))) { throw 'replacement_nic_unconfirmed' }
    return $data[0]
}

function Read-ReplacementTopology {
    $old = Get-VM -Id ([Guid]$OldVmId)
    $primary = Get-VM -Id ([Guid]$PrimaryVmId)
    $replacement = Get-VM -Id ([Guid]$ReplacementVmId)
    if (($ObserveOnly -and $old.State -notin @('Off','Running')) -or
        ($Go -and ($old.State -ne 'Off' -or $old.AutomaticStartAction -ne 'Nothing')) -or
        $primary.State -ne 'Running' -or $replacement.State -ne 'Running' -or
        $replacement.AutomaticStartAction -ne 'Nothing') { throw 'replacement_vm_state_unconfirmed' }
    Assert-DiskPaths $old $OldDiskPathsSha256
    Assert-DiskPaths $replacement $ReplacementDiskPathsSha256
    $oldNic = Read-Nic $old
    $primaryNic = Read-Nic $primary
    $replacementNic = Read-Nic $replacement
    $serving = Get-VMSwitch -Id ([Guid]$ServingSwitchId)
    $isolated = Get-VMSwitch -Id ([Guid]$IsolatedSwitchId)
    if ($serving.SwitchType -ne 'Internal' -or $isolated.SwitchType -ne 'Internal' -or
        $primaryNic.SwitchId -ne $serving.Id -or $primaryNic.SwitchName -cne $serving.Name -or
        $replacementNic.MacAddress -cne $ReplacementMac -or
        (-not (Test-Disconnected $oldNic) -and ($oldNic.SwitchId -ne $serving.Id -or $oldNic.SwitchName -cne $serving.Name)) -or
        (($replacementNic.SwitchId -ne $isolated.Id -or $replacementNic.SwitchName -cne $isolated.Name) -and
         ($replacementNic.SwitchId -ne $serving.Id -or $replacementNic.SwitchName -cne $serving.Name))) { throw 'replacement_switch_unconfirmed' }
    $oldAttached = $oldNic.SwitchId -eq $serving.Id
    $attached = $replacementNic.SwitchId -eq $serving.Id
    if ($oldAttached -and $attached) { throw 'replacement_switch_overlap' }
    $expectedServing = @($primary.Id)
    if ($oldAttached) { $expectedServing += $old.Id }
    if ($attached) { $expectedServing += $replacement.Id }
    $all = @(Get-VMNetworkAdapter -VMName '*')
    $liveMembers = @($all | Where-Object {$_.SwitchId -eq $serving.Id})
    $isolatedMembers = @($all | Where-Object {$_.SwitchId -eq $isolated.Id})
    if ($liveMembers.Count -ne $expectedServing.Count -or
        @($liveMembers | Where-Object {$_.VMId -notin $expectedServing}).Count -ne 0 -or
        @($liveMembers.VMId | Select-Object -Unique).Count -ne $expectedServing.Count -or
        @($all | Where-Object {$_.MacAddress -ieq $ReplacementMac}).Count -ne 1 -or
        ($attached -and $isolatedMembers.Count -ne 0) -or
        (-not $attached -and ($isolatedMembers.Count -ne 1 -or $isolatedMembers[0].VMId -ne $replacement.Id))) {
        throw 'replacement_switch_membership_unconfirmed'
    }
    return [pscustomobject]@{OldNic=$oldNic; ReplacementNic=$replacementNic; Serving=$serving;
        OldAttached=$oldAttached; Attached=$attached}
}

$guid = '\A[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\z'
$ids = @($OldVmId,$PrimaryVmId,$ReplacementVmId,$IsolatedSwitchId,$ServingSwitchId)
$hashes = @($OldDiskPathsSha256,$ReplacementDiskPathsSha256)
if ($Go) { $hashes += $StageSha256 }
if ($ObserveOnly -and $StageSha256) { throw 'replacement_network_scope_invalid' }
if (($Go.IsPresent -eq $ObserveOnly.IsPresent) -or @($ids | Where-Object {$_ -cnotmatch $guid -or [Guid]$_ -eq [Guid]::Empty}).Count -ne 0 -or
    @($ids | Select-Object -Unique).Count -ne $ids.Count -or $ReplacementMac -cnotmatch '\A02[0-9A-F]{10}\z' -or
    @($hashes | Where-Object {$_ -cnotmatch '\A[a-f0-9]{64}\z'}).Count -ne 0) {
    throw 'replacement_network_scope_invalid'
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
try {
    if (-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'replacement_network_admin_required'
    }
} finally { $identity.Dispose() }

$mutex = [Threading.Mutex]::new($false,('Global\N100HA-ReplacementNetwork-' + $ServingSwitchId))
$held = $false
try {
    $held = $mutex.WaitOne(0)
    if (-not $held) { throw 'replacement_network_busy' }
    $state = Read-ReplacementTopology
    if ($ObserveOnly) {
        if (-not $state.OldAttached -or $state.Attached) { throw 'replacement_preflight_topology_unconfirmed' }
        [ordered]@{isolated=$true;vm_id=$ReplacementVmId;switch_id=$IsolatedSwitchId} | ConvertTo-Json -Compress
        return
    }
    if ($state.OldAttached) {
        Disconnect-VMNetworkAdapter -VMNetworkAdapter $state.OldNic
        $state = Read-ReplacementTopology
        if ($state.OldAttached) { throw 'replacement_old_disconnect_unconfirmed' }
    }
    if (-not $state.Attached) {
        Connect-VMNetworkAdapter -VMNetworkAdapter $state.ReplacementNic -VMSwitch $state.Serving
        $state = Read-ReplacementTopology
    }
    if (-not $state.Attached -or $state.OldAttached) { throw 'replacement_network_unconfirmed' }
    [ordered]@{attached=$true;stage_sha256=$StageSha256;vm_id=$ReplacementVmId;switch_id=$ServingSwitchId} |
        ConvertTo-Json -Compress
} finally {
    if ($held) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
