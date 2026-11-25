# Start only the two enrolled Hyper-V guests. Database admission is separate.
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[a-f0-9]{64}$')]
    [string]$VmAIdSha256,
    [Parameter(Mandatory)]
    [ValidatePattern('^[a-f0-9]{64}$')]
    [string]$VmBIdSha256,
    [switch]$StartGuests,
    [switch]$SavedGuestsOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-VmIdSha256([guid]$Id) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::ASCII.GetBytes($Id.ToString('D').ToLowerInvariant())
        return [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Assert-VmScope($VM) {
    if ($VM.Generation -ne 2 -or
        [string]$VM.AutomaticStartAction -cne 'Nothing' -or
        [string]$VM.AutomaticStopAction -cne 'TurnOff' -or
        [string]$VM.State -cnotin @('Off', 'Saved', 'Running')) {
        throw 'n100_boot_vm_state_unverified'
    }
    if ($SavedGuestsOnly -and [string]$VM.State -ceq 'Off') {
        throw 'n100_boot_saved_guest_missing'
    }
}

function Find-Guest($Inventory, [string]$IdSha256) {
    $matches = @($Inventory | Where-Object { (Get-VmIdSha256 $_.Id) -ceq $IdSha256 })
    if ($matches.Count -ne 1) { throw 'n100_boot_vm_identity_unverified' }
    Assert-VmScope $matches[0]
    return $matches[0]
}

function Start-Guest($Guest, [Diagnostics.Stopwatch]$Clock) {
    $vm = Get-VM -Id $Guest.Id -ErrorAction Stop
    if ($vm.Id -ne $Guest.Id) { throw 'n100_boot_vm_identity_changed' }
    Assert-VmScope $vm
    if ([string]$vm.State -ceq 'Running') { return }
    if ($Clock.Elapsed.TotalSeconds -ge 240) { throw 'n100_boot_vm_start_timeout' }
    Start-VM -VM $vm -ErrorAction Stop | Out-Null
    do {
        if ($Clock.Elapsed.TotalSeconds -ge 240) { throw 'n100_boot_vm_start_timeout' }
        $vm = Get-VM -Id $Guest.Id -ErrorAction Stop
        if ($vm.Id -ne $Guest.Id) { throw 'n100_boot_vm_identity_changed' }
        if ([string]$vm.State -ceq 'Running') {
            Assert-VmScope $vm
            return
        }
        Start-Sleep -Seconds 2
    } while ($true)
}

if ($VmAIdSha256 -ceq $VmBIdSha256) { throw 'n100_boot_duplicate_vm_identity' }

$gate = [Threading.Mutex]::new($false, 'Global\N100HA-Start-VM-Guests')
$held = $false
try {
    try { $held = $gate.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $held = $true }
    if (-not $held) { throw 'n100_boot_vm_start_already_running' }

    $inventory = @(Get-VM -ErrorAction Stop)
    $a = Find-Guest $inventory $VmAIdSha256
    $b = Find-Guest $inventory $VmBIdSha256
    if ($StartGuests) {
        if ($SavedGuestsOnly -and [string]$a.State -ceq 'Running' -and [string]$b.State -cne 'Running') {
            throw 'n100_boot_saved_guest_order_unverified'
        }
        $clock = [Diagnostics.Stopwatch]::StartNew()
        Start-Guest $b $clock
        Start-Guest $a $clock
        $inventory = @(Get-VM -ErrorAction Stop)
        $a = Find-Guest $inventory $VmAIdSha256
        $b = Find-Guest $inventory $VmBIdSha256
        if ([string]$a.State -cne 'Running' -or [string]$b.State -cne 'Running') {
            throw 'n100_boot_vm_start_unverified'
        }
    }
    @{
        a = [string]$a.State
        b = [string]$b.State
        mode = if ($StartGuests) { 'guests_started' } else { 'observe' }
        database_authority = 'separate_admission_required'
    } | ConvertTo-Json -Compress
} finally {
    if ($held) { $gate.ReleaseMutex() }
    $gate.Dispose()
}
