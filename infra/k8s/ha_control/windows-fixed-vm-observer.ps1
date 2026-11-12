param(
    [string]$InstallationId,
    [string]$VmA,
    [string]$VmB,
    [string]$PipeName,
    [switch]$NativeObserver
)

function Get-FixedVmObserverIdentity([string]$Installation) {
    if ($Installation -cnotmatch '\A[a-f0-9]{64}\z') { throw 'fixed_observer_installation' }
    $name = 'N100HA_' + $Installation.Substring(0,24) + '_controller'
    return ([Security.Principal.NTAccount]::new('NT SERVICE\' + $name)).Translate([Security.Principal.SecurityIdentifier])
}

function Get-FixedVmObserverHash([string]$Value) {
    $hash = [Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value))).Replace('-','').ToLowerInvariant()
    } finally {
        $hash.Dispose()
    }
}

function Get-FixedVmObserverDisks($Vm) {
    $drives = @(Get-VMHardDiskDrive -VM $Vm -ErrorAction Stop)
    if ($drives.Count -lt 1 -or $drives.Count -gt 4) { throw 'fixed_observer_disk_inventory' }
    $paths = @($drives | ForEach-Object {
        $path = [string]$_.Path
        if ($path -cnotmatch '\A[A-Za-z]:\\[^\r\n]+\z') { throw 'fixed_observer_disk_path' }
        $path.ToLowerInvariant()
    } | Sort-Object -CaseSensitive)
    if (@($paths | Sort-Object -Unique).Count -ne $paths.Count) { throw 'fixed_observer_duplicate_disk' }
    return ,$paths
}

function Get-FixedVmObserverSnapshot([string]$Installation,[guid]$A,[guid]$B) {
    if ($Installation -cnotmatch '\A[a-f0-9]{64}\z' -or $A -eq [guid]::Empty -or $B -eq [guid]::Empty -or $A -eq $B) {
        throw 'fixed_observer_targets'
    }
    $prefix = 'N100HA_' + $Installation.Substring(0,16) + '_'
    $notes = 'ha-owned-guest-v1:' + $Installation + ':'
    $targets = [ordered]@{ A = $A; B = $B }
    $facts = foreach ($node in $targets.Keys) {
        $id = $targets[$node]
        $row = @(Get-VM -Id $id -ErrorAction Stop)
        if ($row.Count -ne 1 -or [guid]$row[0].Id -ne $id -or $row[0].Name -cne ($prefix + $node) -or
            -not ([string]$row[0].Notes).StartsWith($notes,[StringComparison]::Ordinal) -or
            $row[0].Generation -ne 2 -or $row[0].AutomaticStartAction -ne 'Nothing') {
            throw 'fixed_observer_vm_binding'
        }
        $vm = $row[0]
        $paths = Get-FixedVmObserverDisks $vm
        $current = @(Get-VM -Id $id -ErrorAction Stop)
        if ($current.Count -ne 1 -or $current[0].Id -ne $vm.Id -or $current[0].Name -cne $vm.Name -or
            $current[0].Notes -cne $vm.Notes -or $current[0].Generation -ne $vm.Generation -or
            $current[0].AutomaticStartAction -ne $vm.AutomaticStartAction -or $current[0].State -ne $vm.State) {
            throw 'fixed_observer_vm_changed'
        }
        $currentPaths = Get-FixedVmObserverDisks $current[0]
        if (($paths -join "`n") -cne ($currentPaths -join "`n")) { throw 'fixed_observer_disks_changed' }
        [ordered]@{
            node = $node
            id = $id.ToString('D')
            state = $vm.State.ToString()
            disk_paths_sha256 = Get-FixedVmObserverHash (ConvertTo-Json -InputObject $paths -Compress)
        }
    }
    return ,@($facts)
}

function New-FixedVmObserverPipe([string]$Name,[Security.Principal.SecurityIdentifier]$Client) {
    $security = [IO.Pipes.PipeSecurity]::new()
    $security.SetAccessRuleProtection($true,$false)
    foreach ($sid in @('S-1-5-18','S-1-5-32-544')) {
        $security.AddAccessRule([IO.Pipes.PipeAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($sid),[IO.Pipes.PipeAccessRights]::FullControl,'Allow'))
    }
    $security.AddAccessRule([IO.Pipes.PipeAccessRule]::new($Client,[IO.Pipes.PipeAccessRights]::ReadWrite,'Allow'))
    return [IO.Pipes.NamedPipeServerStream]::new($Name,[IO.Pipes.PipeDirection]::InOut,1,
        [IO.Pipes.PipeTransmissionMode]::Byte,[IO.Pipes.PipeOptions]::Asynchronous,4096,4096,$security)
}

function Invoke-FixedVmObserver {
    $ErrorActionPreference = 'Stop'
    if (-not $NativeObserver -or $PSVersionTable.PSEdition -cne 'Desktop' -or
        -not [Environment]::Is64BitProcess -or $PipeName -cnotmatch '\An100ha-vm-observe-[a-f0-9]{32}\z') {
        throw 'fixed_observer_native_required'
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        $principal = [Security.Principal.WindowsPrincipal]::new($identity)
        if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'fixed_observer_admin_required' }
    } finally { $identity.Dispose() }
    Import-Module Hyper-V -ErrorAction Stop
    $client = Get-FixedVmObserverIdentity $InstallationId
    $a = [guid]::ParseExact($VmA,'D')
    $b = [guid]::ParseExact($VmB,'D')
    $keyText = [Environment]::GetEnvironmentVariable('N100_HA_OBSERVER_KEY','Process')
    [Environment]::SetEnvironmentVariable('N100_HA_OBSERVER_KEY',$null,'Process')
    if ($keyText -cnotmatch '\A[a-f0-9]{64}\z') { throw 'fixed_observer_key' }
    $key = [Text.Encoding]::ASCII.GetBytes($keyText)
    $keyText = $null
    $mac = [Security.Cryptography.HMACSHA256]::new($key)
    [array]::Clear($key,0,$key.Length)
    $pipe = $null
    try {
        $pipe = New-FixedVmObserverPipe $PipeName $client
        [Console]::WriteLine('ready')
        $connection = $pipe.WaitForConnectionAsync()
        if (-not $connection.Wait(20000)) { throw 'fixed_observer_connect_timeout' }
        for ($round = 0; $round -lt 2; $round++) {
            $deadline = [Diagnostics.Stopwatch]::StartNew()
            $nonce = [byte[]]::new(32)
            $offset = 0
            while ($offset -lt $nonce.Length) {
                $remaining = 5000 - [int]$deadline.ElapsedMilliseconds
                if ($remaining -le 0) { throw 'fixed_observer_request_timeout' }
                $read = $pipe.ReadAsync($nonce,$offset,$nonce.Length-$offset)
                if (-not $read.Wait($remaining) -or $read.Result -le 0) { throw 'fixed_observer_request_timeout' }
                $offset += $read.Result
            }
            $pipe.RunAsClient([IO.Pipes.PipeStreamImpersonationWorker]{
                $caller = [Security.Principal.WindowsIdentity]::GetCurrent($true)
                if ($null -eq $caller) { throw 'fixed_observer_client_required' }
                try {
                    if ($caller.User.Value -cne $client.Value) { throw 'fixed_observer_client_rejected' }
                } finally { $caller.Dispose() }
            })
            $challenge = [Text.Encoding]::ASCII.GetString($nonce)
            if ($challenge -cnotmatch '\A[a-f0-9]{32}\z') { throw 'fixed_observer_nonce' }
            $response = [ordered]@{
                nonce = $challenge
                vms = Get-FixedVmObserverSnapshot $InstallationId $a $b
            }
            $body = [Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject $response -Depth 5 -Compress))
            $envelope = [ordered]@{
                body = [Convert]::ToBase64String($body)
                mac = [BitConverter]::ToString($mac.ComputeHash($body)).Replace('-','').ToLowerInvariant()
            }
            $bytes = [Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject $envelope -Compress) + "`n")
            if ($bytes.Length -gt 4096) { throw 'fixed_observer_response_size' }
            $write = $pipe.WriteAsync($bytes,0,$bytes.Length)
            if (-not $write.Wait(1000)) { throw 'fixed_observer_response_timeout' }
        }
    } finally {
        if ($null -ne $pipe) { $pipe.Dispose() }
        $mac.Dispose()
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    try { Invoke-FixedVmObserver } catch { exit 1 }
}
