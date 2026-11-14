param(
    [string]$PipeName,
    [string]$OperatorSid,
    [string]$ServiceSid,
    [string]$PlanSha256,
    [string]$AttemptId,
    [long]$Generation,
    [string]$WindowsBootSha256,
    [string]$WslBootSha256,
    [string]$CollectorSha256,
    [string]$ReactivationPlanSha256,
    [string]$MarkerSha256,
    [string]$CallbackPath,
    [string]$CallbackSha256,
    [string]$CallbackConfigPath,
    [string]$CallbackConfigSha256,
    [string]$KeyPath,
    [string]$KeySha256,
    [string]$ReadyPath,
    [int]$RecentNonces = 1024,
    [switch]$Go
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$encoding = [Text.UTF8Encoding]::new($false, $true)
$pipe = $null
$runspace = $null
$vmRunspace = $null
$clock = $null
$mac = $null
$outcome = @{ recorded = $false }
$scope = 'n100ha-legacy-closure-v1'
$servicePattern = '\AS-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}\z'
$sidPattern = '\AS-1-(?:[0-9]{1,10}-){1,14}[0-9]{1,10}\z'
$hashPattern = '\A[a-f0-9]{64}\z'
$writers = @('S-1-5-18', 'S-1-5-32-544', $OperatorSid)

function Remaining {
    $left = 1800 - [int][Math]::Ceiling($clock.Elapsed.TotalMilliseconds)
    if ($left -le 0) { throw 'lease_proof_deadline' }
    return $left
}

function Get-Digest([byte[]]$Bytes) {
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($hash.ComputeHash($Bytes)).Replace('-', '').ToLowerInvariant() }
    finally { $hash.Dispose() }
}

function Assert-LocalPath([string]$InputPath) {
    if ($InputPath -cnotmatch '\A[A-Za-z]:\\' -or $InputPath.Substring(2).Contains(':') -or
        $InputPath.Contains('/') -or $InputPath -match '(^|\\)\.\.?($|\\)' -or $InputPath -match '[ .](\\|$)') {
        throw 'lease_proof_protected_path'
    }
}

function Assert-ProtectedPath($entry, [switch]$PrivateLeaf) {
    $walk = $entry
    while ($null -ne $walk) {
        if ($walk.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'lease_proof_callback_path' }
        $sd = [Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $walk.FullName).Sddl)
        $trustedOsRoot = $walk -is [IO.DirectoryInfo] -and $null -eq $walk.Parent -and
            $walk.FullName -ieq [IO.Path]::GetPathRoot([Environment]::SystemDirectory) -and
            $sd.Owner.Value -ceq 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
        if (($sd.Owner.Value -cnotin $writers -and -not $trustedOsRoot) -or $null -eq $sd.DiscretionaryAcl) {
            throw 'lease_proof_callback_acl'
        }
        foreach ($ace in $sd.DiscretionaryAcl) {
            if ($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback) { throw 'lease_proof_callback_acl' }
            if ($ace.AceQualifier -eq 'AccessAllowed' -and -not ([int]$ace.AceFlags -band 8) -and
                $ace.SecurityIdentifier.Value -cnotin $writers) {
                $mask = if ($walk -is [IO.FileInfo] -or ($PrivateLeaf -and $walk.FullName -ieq $entry.FullName)) { 1343029590 } else { 269287488 }
                if (([long]$ace.AccessMask -band $mask) -ne 0) { throw 'lease_proof_callback_acl' }
            }
        }
        $walk = if ($walk -is [IO.FileInfo]) { $walk.Directory } else { $walk.Parent }
    }
}

function Read-ProtectedText([string]$InputPath, [string]$ExpectedSha) {
    Assert-LocalPath $InputPath
    $entry = Get-Item -LiteralPath $InputPath -Force
    if ($entry -isnot [IO.FileInfo] -or $entry.Length -lt 1 -or $entry.Length -gt 65536) {
        throw 'lease_proof_callback_size'
    }
    Assert-ProtectedPath $entry
    $file = [IO.File]::Open($InputPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        if ($file.Length -lt 1 -or $file.Length -gt 65536) { throw 'lease_proof_callback_size' }
        $reader = [IO.BinaryReader]::new($file, $encoding, $true)
        try {
            $bytes = $reader.ReadBytes([int]$file.Length)
            if ((Get-Digest $bytes) -cne $ExpectedSha -or $file.ReadByte() -ne -1) {
                throw 'lease_proof_callback_pin'
            }
            return $encoding.GetString($bytes)
        } finally { $reader.Dispose() }
    } finally { $file.Dispose() }
}

function Open-ProofMac {
    Assert-LocalPath $KeyPath
    $entry = Get-Item -LiteralPath $KeyPath -Force
    if ($entry -isnot [IO.FileInfo] -or $entry.Length -ne 32) { throw 'lease_proof_key' }
    Assert-ProtectedPath $entry
    $sd = [Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $entry.FullName).Sddl)
    if (($sd.ControlFlags -band 0x1004) -ne 0x1004) { throw 'lease_proof_key_acl' }
    $seen = @{}
    foreach ($ace in $sd.DiscretionaryAcl) {
        $sid = $ace.SecurityIdentifier.Value
        if ($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback -or
            $ace.AceQualifier -ne 'AccessAllowed' -or [int]$ace.AceFlags -ne 0 -or $seen.ContainsKey($sid)) {
            throw 'lease_proof_key_acl'
        }
        if ($sid -ceq $ServiceSid) {
            if (($ace.AccessMask -band 0x120089) -ne 0x120089 -or ($ace.AccessMask -band (-bnot 0x1200a9)) -ne 0) {
                throw 'lease_proof_key_acl'
            }
        } elseif ($sid -cnotin $writers -or $ace.AccessMask -ne 2032127) { throw 'lease_proof_key_acl' }
        $seen[$sid] = $true
    }
    if (-not $seen.ContainsKey($ServiceSid) -or -not $seen.ContainsKey('S-1-5-18') -or
        -not $seen.ContainsKey('S-1-5-32-544')) { throw 'lease_proof_key_acl' }
    $file = [IO.File]::Open($KeyPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        $reader = [IO.BinaryReader]::new($file, $encoding, $true)
        try {
            $key = $reader.ReadBytes(32)
            try {
                if ($key.Length -ne 32 -or $file.ReadByte() -ne -1 -or (Get-Digest $key) -cne $KeySha256) {
                    throw 'lease_proof_key_pin'
                }
                return [Security.Cryptography.HMACSHA256]::new($key)
            } finally { [Array]::Clear($key, 0, $key.Length) }
        } finally { $reader.Dispose() }
    } finally { $file.Dispose() }
}

function Write-ProtectedRecord([string]$Path, $Record) {
    Assert-LocalPath $Path
    $parent = Get-Item -LiteralPath ([IO.Path]::GetDirectoryName($Path)) -Force
    if ($parent -isnot [IO.DirectoryInfo]) { throw 'lease_proof_ready_parent' }
    Assert-ProtectedPath $parent -PrivateLeaf
    $security = [Security.AccessControl.FileSecurity]::new()
    $security.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    $security.SetAccessRuleProtection($true, $false)
    foreach ($sid in @('S-1-5-18','S-1-5-32-544')) {
        $security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($sid), 'FullControl', 'Allow'))
    }
    $temporary = Join-Path $parent.FullName ('.lease-observer-ready-' + [guid]::NewGuid().ToString('N') + '.tmp')
    $file = [IO.FileStream]::new($temporary, [IO.FileMode]::CreateNew,
        [Security.AccessControl.FileSystemRights]'WriteData,Synchronize', [IO.FileShare]::None, 4096,
        [IO.FileOptions]::WriteThrough, $security)
    try {
        try {
            $data = $encoding.GetBytes((ConvertTo-Json -Compress -InputObject $Record) + "`n")
            $file.Write($data, 0, $data.Length)
            $file.Flush($true)
        } finally { $file.Dispose() }
        [IO.File]::Move($temporary,$Path)
    } finally { [IO.File]::Delete($temporary) }
}

function Write-ProofOutcome([string]$Stage, $Progress = $null) {
    if ($outcome.recorded) { return }
    $outcome.recorded = $true
    try {
        $record = [ordered]@{
            schema_version=1; attempt_id=$AttemptId; phase=$Stage
            elapsed_ms=[long]$clock.ElapsedMilliseconds
        }
        if ($null -ne $Progress) {
            $step = $Progress.step
            if ($step -cin @('dispatch','request','wsl_send','marker_before','wsl_running',
                            'windows_closed','windows_boot','windows_shutdown','windows_tasks','windows_runner',
                            'windows_processes','hyperv_start','hyperv','wsl_response','marker_after','complete',
                            'wsl_rejected_deadline','wsl_rejected_inventory','wsl_rejected_identity',
                            'wsl_rejected_recovery','wsl_rejected_closure','wsl_rejected_internal',
                            'wsl_rejected_deadline_deployments','wsl_rejected_deadline_cronjobs',
                            'wsl_rejected_deadline_jobs','wsl_rejected_deadline_replicasets',
                            'wsl_rejected_deadline_pods','wsl_rejected_deadline_docker','wsl_rejected_deadline_autoscalers') -or
                $step -cmatch '\Awsl_rejected_deadline_q(?!(?:00|01|02|04|08|10|20|40)\z)[0-7][0-9a-f]\z') {
                $record['callback_step'] = $step
            }
        }
        Write-ProtectedRecord ($ReadyPath + '.outcome.json') $record
    } catch {
        # The record is diagnostic only. Never turn a rejected proof into a response.
    }
}

function New-ProofPipe {
    $security = [IO.Pipes.PipeSecurity]::new()
    $security.SetOwner([Security.Principal.SecurityIdentifier]::new($OperatorSid))
    $security.SetAccessRuleProtection($true, $false)
    foreach ($sid in $writers) {
        $security.AddAccessRule([IO.Pipes.PipeAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($sid), [IO.Pipes.PipeAccessRights]::FullControl, 'Allow'))
    }
    $rights = [IO.Pipes.PipeAccessRights]::ReadWrite -bor [IO.Pipes.PipeAccessRights]::Synchronize
    $security.AddAccessRule([IO.Pipes.PipeAccessRule]::new(
        [Security.Principal.SecurityIdentifier]::new($ServiceSid), $rights, 'Allow'))
    return [IO.Pipes.NamedPipeServerStream]::new($PipeName, [IO.Pipes.PipeDirection]::InOut, 1,
        [IO.Pipes.PipeTransmissionMode]::Byte, [IO.Pipes.PipeOptions]::Asynchronous, 4096, 4096, $security)
}

function Read-Request {
    $buffer = [byte[]]::new(2048)
    $offset = 0
    while ($offset -lt $buffer.Length) {
        $read = $pipe.ReadAsync($buffer, $offset, $buffer.Length - $offset)
        if (-not $read.Wait((Remaining)) -or $read.Result -le 0) { throw 'lease_proof_request' }
        $end = [Array]::IndexOf($buffer, [byte]10, $offset, $read.Result)
        $offset += $read.Result
        if ($end -ge 0) {
            if ($end -ne $offset - 1) { throw 'lease_proof_frame' }
            return $encoding.GetString($buffer, 0, $end)
        }
    }
    throw 'lease_proof_frame'
}

function New-Challenge([string]$Raw) {
    $fields = ConvertFrom-Json -InputObject $Raw
    if ($fields.nonce -isnot [string] -or $fields.nonce -cnotmatch '\A[a-f0-9]{32}\z') {
        throw 'lease_proof_nonce'
    }
    # Canonical ASCII request exactly matches Python sorted-key encoding.
    $expected = [ordered]@{
        attempt_id=$AttemptId; collector_sha256=$CollectorSha256; generation=$Generation
        nonce=$fields.nonce; plan_sha256=$PlanSha256; scope=$scope; service_sid=$ServiceSid
        windows_boot_sha256=$WindowsBootSha256; wsl_boot_sha256=$WslBootSha256
    }
    if ((ConvertTo-Json -InputObject $expected -Compress) -cne $Raw) { throw 'lease_proof_binding' }
    return $expected
}

function Invoke-ObservedClosure($Challenge, $Progress) {
    $context = [pscustomobject]@{
        Challenge=[pscustomobject]$Challenge
        ReactivationPlanSha256=$ReactivationPlanSha256
        MarkerSha256=$MarkerSha256
        Progress=$Progress
        VmRunspace=$vmRunspace
        DeadlineTimestamp=[Diagnostics.Stopwatch]::GetTimestamp() +
            [long]((Remaining) * [Diagnostics.Stopwatch]::Frequency / 1000)
    }
    $worker = [PowerShell]::Create()
    $worker.Runspace = $runspace
    try {
        $null = $worker.AddCommand('Get-HaLeaseProof').AddArgument($context)
        $pending = $worker.BeginInvoke()
        if (-not $pending.AsyncWaitHandle.WaitOne((Remaining))) {
            # End the isolated broker rather than let an unbounded callback occupy it.
            Write-ProofOutcome 'observation_timeout' $Progress
            [Environment]::Exit(1)
        }
        $rows = $worker.EndInvoke($pending)
        if ($worker.HadErrors -or $rows.Count -ne 1) { throw 'lease_proof_observation' }
        $fact = $rows[0]
        $names = @($fact.PSObject.Properties.Name | Sort-Object -CaseSensitive)
        $expectedNames = @('collector_sha256','generation','marker_sha256','nonce','observation_sha256',
            'reactivation_plan_sha256','recovery_commands_settled','windows_boot_sha256','windows_reactivation_closed',
            'wsl_boot_sha256','wsl_writers_closed')
        if (($names -join ',') -cne ($expectedNames -join ',') -or
            $fact.nonce -cne $Challenge.nonce -or ($fact.generation -isnot [int] -and $fact.generation -isnot [long]) -or
            $fact.generation -ne $Generation -or
            $fact.marker_sha256 -cne $MarkerSha256 -or $fact.reactivation_plan_sha256 -cne $ReactivationPlanSha256 -or
            $fact.collector_sha256 -cne $CollectorSha256 -or $fact.windows_boot_sha256 -cne $WindowsBootSha256 -or
            $fact.wsl_boot_sha256 -cne $WslBootSha256 -or
            $fact.observation_sha256 -isnot [string] -or $fact.observation_sha256 -cnotmatch $hashPattern) {
            throw 'lease_proof_observation'
        }
        foreach ($name in @('recovery_commands_settled','windows_reactivation_closed','wsl_writers_closed')) {
            if ($fact.$name -isnot [bool] -or -not $fact.$name) { throw 'lease_proof_observation' }
        }
        return $fact.observation_sha256
    } finally { $worker.Dispose() }
}

try {
    if (-not $Go -or $PSVersionTable.PSEdition -cne 'Desktop' -or -not [Environment]::Is64BitProcess -or
        [Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or
        $PipeName -cnotmatch '\An100ha-lease-proof-[a-f0-9]{32}\z' -or
        $OperatorSid -cnotmatch $sidPattern -or $OperatorSid -cin @('S-1-5-18','S-1-5-32-544') -or
        $ServiceSid -cnotmatch $servicePattern -or $OperatorSid -ceq $ServiceSid -or
        $Generation -lt 1 -or $RecentNonces -lt 1 -or $RecentNonces -gt 4096) { throw 'lease_proof_context' }
    foreach ($pin in @($PlanSha256,$AttemptId,$WindowsBootSha256,$WslBootSha256,$CollectorSha256,
                      $ReactivationPlanSha256,$MarkerSha256,$CallbackSha256,$CallbackConfigSha256,$KeySha256)) {
        if ($pin -cnotmatch $hashPattern) { throw 'lease_proof_pin' }
    }
    $operator = [Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        if ($operator.User.Value -cne $OperatorSid) { throw 'lease_proof_operator' }
    } finally { $operator.Dispose() }

    $mac = Open-ProofMac
    $callback = Read-ProtectedText $CallbackPath $CallbackSha256
    $inputConfig = ConvertFrom-Json -InputObject (Read-ProtectedText $CallbackConfigPath $CallbackConfigSha256)
    $planText = Read-ProtectedText $inputConfig.reactivation_plan_path $ReactivationPlanSha256
    $nativePlan = ConvertFrom-Json -InputObject $planText
    if ($PlanSha256 -cne $ReactivationPlanSha256 -and
        ((@($nativePlan.PSObject.Properties.Name | Sort-Object -CaseSensitive) -join ',') -cne
           'deployment_attempt_id,deployment_plan_sha256,windows_driver_sha256,windows_reactivation' -or
         $inputConfig.reactivation_plan_sha256 -cne $ReactivationPlanSha256 -or
         $nativePlan.deployment_plan_sha256 -cne $PlanSha256 -or $nativePlan.deployment_attempt_id -cne $AttemptId)) {
        throw 'lease_proof_reactivation_plan_binding'
    }
    $driverText = Read-ProtectedText $inputConfig.driver_path $inputConfig.driver_sha256
    $vmObserverText = Read-ProtectedText $inputConfig.vm_observer_path $inputConfig.vm_observer_sha256
    if ($inputConfig.driver_sha256 -cne $nativePlan.windows_driver_sha256) {
        throw 'lease_proof_callback_pin'
    }
    $runspace = [RunspaceFactory]::CreateRunspace()
    $runspace.ThreadOptions = 'ReuseThread'
    $runspace.Open()
    $vmRunspace = [RunspaceFactory]::CreateRunspace()
    $vmRunspace.ThreadOptions = 'ReuseThread'
    $vmRunspace.Open()
    $vmInit = [PowerShell]::Create()
    $vmInit.Runspace = $vmRunspace
    try {
        $null = $vmInit.AddScript('Import-Module Hyper-V -Global -ErrorAction Stop', $false).AddStatement()
        $null = $vmInit.AddScript($vmObserverText, $false).AddStatement()
        $null = $vmInit.AddCommand('Get-FixedVmObserverSnapshot').
            AddArgument($inputConfig.installation_id).AddArgument([guid]$inputConfig.vm_a).AddArgument([guid]$inputConfig.vm_b)
        $warm = $vmInit.Invoke()
        if ($vmInit.HadErrors -or $warm.Count -ne 1) { throw 'lease_proof_vm_init' }
    } finally { $vmInit.Dispose() }
    $init = [PowerShell]::Create()
    $init.Runspace = $runspace
    try {
        $null = $init.AddScript($driverText, $false).AddStatement()
        $null = $init.AddScript($callback, $false).AddStatement()
        $null = $init.AddCommand('Initialize-HaLeaseObservation').AddArgument($inputConfig).AddArgument($planText).
            AddArgument($MarkerSha256).AddArgument($WindowsBootSha256).AddArgument($CollectorSha256)
        $output = $init.Invoke()
        if ($init.HadErrors -or $output.Count -ne 0) { throw 'lease_proof_callback' }
    } finally { $init.Dispose() }

    $pipe = New-ProofPipe
    $process = [Diagnostics.Process]::GetCurrentProcess()
    try {
        Write-ProtectedRecord $ReadyPath ([ordered]@{
            scope=$scope; pid=$process.Id; creation_filetime=$process.StartTime.ToFileTimeUtc(); server_sid=$OperatorSid
            attempt_id=$AttemptId; pipe_name=$PipeName; plan_sha256=$PlanSha256; callback_config_sha256=$CallbackConfigSha256
        })
    } finally { $process.Dispose() }
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $order = [Collections.Generic.Queue[string]]::new()
    while ($true) {
        $pipe.WaitForConnectionAsync().GetAwaiter().GetResult()
        $phase = 'request'
        $progress = $null
        try {
            $clock = [Diagnostics.Stopwatch]::StartNew()
            $challenge = New-Challenge (Read-Request)
            $phase = 'client_identity'
            $pipe.RunAsClient([IO.Pipes.PipeStreamImpersonationWorker]{
                $client = [Security.Principal.WindowsIdentity]::GetCurrent($true)
                try {
                    if ($null -eq $client -or $client.User.Value -cne $ServiceSid) { throw 'lease_proof_client' }
                } finally { if ($null -ne $client) { $client.Dispose() } }
            })
            $phase = 'nonce'
            if (-not $seen.Add($challenge.nonce)) { throw 'lease_proof_replay' }
            $order.Enqueue($challenge.nonce)
            if ($order.Count -gt $RecentNonces) { $null = $seen.Remove($order.Dequeue()) }
            $phase = 'observation'
            $progress = [hashtable]::Synchronized(@{step='dispatch'})
            $digest = Invoke-ObservedClosure $challenge $progress
            $phase = 'response'
            $challenge.Add('closed', $true)
            $challenge.Add('observation_sha256', $digest)
            $body = $encoding.GetBytes((ConvertTo-Json -InputObject $challenge -Compress))
            $tag = [BitConverter]::ToString($mac.ComputeHash($body)).Replace('-', '').ToLowerInvariant()
            $envelope = [ordered]@{body=[Convert]::ToBase64String($body); mac=$tag}
            $bytes = $encoding.GetBytes((ConvertTo-Json -InputObject $envelope -Compress) + "`n")
            $write = $pipe.WriteAsync($bytes, 0, $bytes.Length)
            if (-not $write.Wait((Remaining))) { throw 'lease_proof_write' }
            # Keep the peer handle associated until the client finishes its
            # post-read PID check. Draining alone only proves byte consumption.
            $phase = 'ack'
            $ack = [byte[]]::new(1)
            $ackRead = $pipe.ReadAsync($ack, 0, $ack.Length)
            if (-not $ackRead.Wait((Remaining))) {
                Write-ProofOutcome 'ack_timeout'
                [Environment]::Exit(1)
            }
            if ($ackRead.Result -ne 1 -or $ack[0] -ne 6) { throw 'lease_proof_ack' }
        } catch {
            # Failure is a closed connection, never an authenticated positive receipt.
            if ($pipe.IsConnected) { $pipe.Disconnect() }
            Write-ProofOutcome $phase $progress
        } finally {
            if ($pipe.IsConnected) { $pipe.Disconnect() }
        }
    }
} catch { exit 1 } finally {
    if ($null -ne $pipe) { $pipe.Dispose() }
    if ($null -ne $mac) { $mac.Dispose() }
    if ($null -ne $runspace) { $runspace.Dispose() }
    if ($null -ne $vmRunspace) { $vmRunspace.Dispose() }
}
