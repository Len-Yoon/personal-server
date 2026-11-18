param(
    [string]$BindingPath,
    [string]$LegacyPlanSha256,
    [string]$AttemptId,
    [string]$PriorSha256,
    [string]$ExpectedServiceInstanceSha256,
    [string]$RecoveryAttemptId,
    [string]$RecoveryPredecessorTerminalSha256,
    [string]$RecoveryPredecessorServiceSha256,
    [string]$TransitionHandoffSha256,
    [string]$TransitionBoundarySha256,
    [switch]$Retire,
    [switch]$ObserveRetired,
    [switch]$ReadPriorServiceArgs,
    [switch]$RequireTerminalInitial,
    [switch]$ResumeRetire,
    [switch]$ReadClosureReceipt,
    [switch]$ReadRejoinReceipt,
    [string]$RejoinMode,
    [string]$ClosurePlanSha256,
    [string]$ClosureGeneration,
    [string]$ClosureHostPid,
    [string]$ClosureStartedMs,
    [string]$ClosureServiceSid
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$pattern = '\A[a-f0-9]{64}\z'

function Get-Digest([string]$Value) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value))).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Read-Scope([string]$Path, [string[]]$Owners, [switch]$Immutable) {
    if ($Path -cnotmatch '\A[A-Za-z]:\\[A-Za-z0-9_.\\-]+\z' -or $Path -match '\\\.\.?(\\|$)') { throw 'live_authority_path' }
    $entry = Get-Item -LiteralPath $Path -Force
    if ($entry -isnot [IO.FileInfo] -or $entry.Length -lt 1 -or $entry.Length -gt 8192 -or
        ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'live_authority_path' }
    $node = $entry.Directory
    while ($null -ne $node) {
        if ($node.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'live_authority_path' }
        $node = $node.Parent
    }
    $entries = @($entry,$entry.Directory)
    if ($Immutable) {
        for ($node = $entry.Directory.Parent; $null -ne $node; $node = $node.Parent) { $entries += $node }
    }
    foreach ($item in $entries) {
        $sd = Get-Acl -LiteralPath $item.FullName
        $ancestor = $Immutable -and $item.FullName -cne $entry.FullName -and $item.FullName -cne $entry.Directory.FullName
        $allowed = if ($ancestor) { @($Owners | Where-Object { $_ -cne $ClosureServiceSid }) } else { $Owners }
        $owner = $sd.GetOwner([Security.Principal.SecurityIdentifier]).Value
        $trustedRoot = $Immutable -and $item.FullName -ceq 'C:\' -and
            $owner -ceq 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
        if ($owner -cnotin $allowed -and -not $trustedRoot) { throw 'live_authority_acl' }
        # Match the installer: ancestor entries must prevent replacing the child,
        # while the file and journal also deny general writes by other principals.
        $mask = if ($ancestor) { 269287488L } elseif ($Immutable) { 1343029590L } else {
            [long][Security.AccessControl.FileSystemRights]'Write,Delete,DeleteSubdirectoriesAndFiles,ChangePermissions,TakeOwnership'
        }
        foreach ($rule in $sd.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
            if ($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Value -cnotin $allowed -and
                -not ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -and
                (([long]$rule.FileSystemRights -band 4294967295L) -band $mask)) { throw 'live_authority_acl' }
        }
    }
    # Snapshot the path timestamp before opening. A journal replaced between the
    # snapshot and open can only be judged older than the new record itself.
    $written = $entry.LastWriteTimeUtc
    $share = if ($Immutable) { [IO.FileShare]::Read } else { [IO.FileShare]::Read -bor [IO.FileShare]::Delete }
    $stream = [IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,$share)
    try {
        if ($stream.Length -lt 1 -or $stream.Length -gt 8192) { throw 'live_authority_path' }
        if ($Immutable) {
            $links = @(& (Join-Path ([Environment]::SystemDirectory) 'fsutil.exe') hardlink list $Path)
            if ($LASTEXITCODE -ne 0 -or $links.Count -ne 1) { throw 'live_authority_path' }
        }
        $reader = [IO.BinaryReader]::new($stream,[Text.UTF8Encoding]::new($false,$true),$true)
        try {
            $raw = $reader.ReadBytes([int]$stream.Length)
            if ($stream.ReadByte() -ne -1) { throw 'live_authority_path' }
            return [pscustomobject]@{raw=[Text.UTF8Encoding]::new($false,$true).GetString($raw);write_time=$written}
        } finally { $reader.Dispose() }
    } finally { $stream.Dispose() }
}

function Find-BoundService($Plan) {
    $service = [wmi]('Win32_Service.Name="' + $Plan.service_name + '"')
    $service.Get()
    if ($null -eq $service -or $service.Name -cne $Plan.service_name -or
        (Get-Digest $service.PathName) -cne $Plan.service_image_path_sha256) { throw 'live_authority_service' }
    return $service
}

function Assert-Retired($Service) {
    if ($Service.StartMode -cne 'Disabled' -or $Service.State -cne 'Stopped' -or $Service.ProcessId -ne 0) {
        throw 'initial_service_retirement_unconfirmed'
    }
}

function Assert-TransitionOldVm($Plan) {
    $vm = @(Get-VM -Id ([Guid]::Parse($Plan.old_vm_id)) -ErrorAction Stop)
    if ($vm.Count -ne 1 -or [string]$vm[0].State -cne 'Off' -or [string]$vm[0].AutomaticStartAction -cne 'Nothing') {
        throw 'transition_old_vm_not_fenced'
    }
    [string[]]$paths = @(Get-VMHardDiskDrive -VM $vm[0] -ErrorAction Stop | ForEach-Object {
        $path = [string]$_.Path
        if ($path -cnotmatch '\A[A-Za-z]:\\[\x20-\x7e]+\z') { throw 'transition_old_vm_disk_drift' }
        $path.ToLowerInvariant()
    })
    [Array]::Sort($paths,[StringComparer]::Ordinal)
    if ($paths.Count -lt 1 -or $paths.Count -gt 8 -or
        @($paths | Select-Object -Unique).Count -ne $paths.Count -or
        (Get-Digest ($paths -join [char]10)) -cne $Plan.old_disk_paths_sha256) {
        throw 'transition_old_vm_disk_drift'
    }
}

function Read-TransitionAuthority([string[]]$Admins) {
    if ($BindingPath -cnotmatch '\AC:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}\\transition-service-[a-f0-9]{64}\.json\z' -or
        -not $BindingPath.EndsWith(('-' + $AttemptId + '.json'),[StringComparison]::Ordinal) -or
        (@($AttemptId,$LegacyPlanSha256,$PriorSha256,$ExpectedServiceInstanceSha256,$TransitionHandoffSha256,$TransitionBoundarySha256) |
            Where-Object { $_ -cnotmatch $pattern })) { throw 'transition_authority_scope' }
    $inputFile = Read-Scope $BindingPath $Admins
    if ((Get-Digest $inputFile.raw) -cne $ExpectedServiceInstanceSha256) { throw 'transition_authority_instance' }
    $grantPath = Join-Path ([IO.Path]::GetDirectoryName($BindingPath)) ("transition-grant-" + $AttemptId + ".json")
    $grantFile = Read-Scope $grantPath $Admins
    $grant = ConvertFrom-Json -InputObject $grantFile.raw
    if ((@($grant.PSObject.Properties.Name | Sort-Object -CaseSensitive) -join ',') -cne 'attempt_id,handoff_sha256,service_instance_sha256' -or
        $grant.attempt_id -cne $AttemptId -or $grant.handoff_sha256 -cne $TransitionHandoffSha256 -or
        $grant.service_instance_sha256 -cne $ExpectedServiceInstanceSha256) { throw 'transition_authority_scope' }
    $plan = ConvertFrom-Json -InputObject $inputFile.raw
    $fields = @($plan.PSObject.Properties.Name | Sort-Object -CaseSensitive)
    if (($fields -join ',') -cne 'attempt_id,controller_plan_sha256,generation,legacy_plan_sha256,old_disk_paths_sha256,old_vm_id,python_command_sha256,python_path_sha256,service_created_ticks,service_image_path_sha256,service_name,service_pid,state_path,target,timeline' -or
        $plan.attempt_id -cne $AttemptId -or $plan.legacy_plan_sha256 -cne $LegacyPlanSha256 -or
        (@($plan.controller_plan_sha256,$plan.old_disk_paths_sha256,$plan.python_command_sha256,$plan.python_path_sha256,$plan.service_image_path_sha256) |
            Where-Object { $_ -cnotmatch $pattern }) -or
        ($plan.generation -isnot [int] -and $plan.generation -isnot [long]) -or $plan.generation -le 1 -or
        ($plan.timeline -isnot [int] -and $plan.timeline -isnot [long]) -or $plan.timeline -le 1 -or
        $plan.target -cnotin @('A','B') -or $plan.old_vm_id -cnotmatch '\A[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\z' -or
        $plan.service_name -cnotmatch '\AN100HA_[a-f0-9]{24}_controller\z' -or
        $plan.service_created_ticks -cnotmatch '\A[1-9][0-9]{1,19}\z' -or
        ($plan.service_pid -isnot [int] -and $plan.service_pid -isnot [long]) -or $plan.service_pid -lt 1 -or
        -not $plan.state_path.EndsWith('\journal\controller-state.json',[StringComparison]::Ordinal)) { throw 'transition_authority_scope' }
    $service = Find-BoundService $plan
    if ($service.State -cne 'Running' -or $service.ProcessId -ne $plan.service_pid) { throw 'transition_authority_instance' }
    $process = Get-Process -Id $plan.service_pid -ErrorAction Stop
    $childProcess = $null
    try {
        if ($process.HasExited -or $process.SessionId -ne 0 -or
            ('"' + $process.MainModule.FileName + '"') -ine $service.PathName -or
            $process.StartTime.ToUniversalTime().Ticks.ToString() -cne $plan.service_created_ticks) { throw 'transition_authority_instance' }
        $identity = [Security.Principal.NTAccount]::new('NT SERVICE',$plan.service_name).Translate([Security.Principal.SecurityIdentifier]).Value
        Assert-TransitionOldVm $plan
        $saved = Read-Scope $plan.state_path ($Admins + $identity)
        $record = ConvertFrom-Json -InputObject $saved.raw
        if ($record.target -cne $plan.target -or $record.old -cnotin @('A','B') -or
            $record.old -ceq $record.target -or $record.generation -ne $plan.generation) { throw 'transition_authority_lease' }
        if ($TransitionBoundarySha256 -ceq (Get-Digest 'controller-ready') -and
            -not $record.PSObject.Properties['lease_heartbeat'] -and
            $record.phase -cin @('rejoining','validating','ready') -and -not $process.HasExited) {
            [ordered]@{transition_authority_pending=$true}|ConvertTo-Json -Compress
            return
        }
        $stamp = $record.lease_heartbeat
        $numbers = @($stamp.sampled_at_ms,$stamp.valid_until_ms,$stamp.python_pid,$stamp.sequence,$stamp.revision)
        if ($record.phase -cne 'ready' -or $record.target -cne $plan.target -or $record.old -cnotin @('A','B') -or
            $record.old -ceq $record.target -or $record.generation -ne $plan.generation -or
            $stamp.plan_sha256 -cne $plan.controller_plan_sha256 -or $stamp.epoch -ne $plan.generation -or
            $stamp.timeline -ne $plan.timeline -or
            ($numbers | Where-Object { ($_ -isnot [int] -and $_ -isnot [long]) -or $_ -le 0 }) -or
            $stamp.revision -lt $plan.generation -or $stamp.broker_instance_sha256 -cnotmatch $pattern -or
            $stamp.postcommit_nonce_sha256 -cnotmatch $pattern -or $stamp.valid_until_ms -le $stamp.sampled_at_ms -or
            ($stamp.valid_until_ms - $stamp.sampled_at_ms) -gt 5000) { throw 'transition_authority_lease' }
        $child = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $stamp.python_pid) -ErrorAction Stop
        if ($null -eq $child -or $child.ParentProcessId -ne $plan.service_pid -or $child.SessionId -ne 0 -or
            (Get-Digest $child.CommandLine) -cne $plan.python_command_sha256 -or
            (Get-Digest $child.ExecutablePath) -cne $plan.python_path_sha256 -or
            $child.CreationDate.ToUniversalTime() -lt $process.StartTime.ToUniversalTime() -or
            ([DateTimeOffset]$child.CreationDate).ToUnixTimeMilliseconds() -gt $stamp.sampled_at_ms) { throw 'transition_authority_instance' }
        $childProcess = Get-Process -Id $stamp.python_pid -ErrorAction Stop
        if ($childProcess.SessionId -ne 0 -or (Get-Digest $childProcess.MainModule.FileName) -cne $plan.python_path_sha256 -or
            [Math]::Abs(($childProcess.StartTime.ToUniversalTime() - $child.CreationDate.ToUniversalTime()).TotalMilliseconds) -ge 1) {
            throw 'transition_authority_instance'
        }
        Assert-TransitionOldVm $plan
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        if ($process.HasExited -or $childProcess.HasExited -or $now -lt $stamp.sampled_at_ms -or ($now - $stamp.sampled_at_ms) -ge 2000 -or
            $now -ge $stamp.valid_until_ms) { throw 'transition_authority_lease' }
        [ordered]@{generation=$stamp.epoch;timeline=$stamp.timeline;revision=$stamp.revision;
            transition_proof_sha256=(Get-Digest ($inputFile.raw + ':' + $grantFile.raw + ':' + $saved.raw + ':' + $PriorSha256 + ':' + $TransitionBoundarySha256))}|ConvertTo-Json -Compress
    } finally {
        if ($null -ne $childProcess) { $childProcess.Dispose() }
        $process.Dispose()
    }
}

function Read-ClosureReceipt {
    if ($BindingPath -cnotmatch '\AC:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}\\journal\\lease-closure-[a-f0-9]{64}\.json\z' -or
        $ClosurePlanSha256 -cnotmatch $pattern -or
        -not $BindingPath.EndsWith(('-' + $ClosurePlanSha256 + '.json'),[StringComparison]::Ordinal) -or
        $ClosureServiceSid -cnotmatch '\AS-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}\z' -or
        @(@($ClosureGeneration,$ClosureHostPid,$ClosureStartedMs) | Where-Object { $_ -cnotmatch '\A[1-9][0-9]{0,17}\z' }).Count) {
        throw 'transition_closure_receipt_invalid'
    }
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $owners = @('S-1-5-18','S-1-5-32-544',$sid,$ClosureServiceSid)
    $launchPath = Join-Path ([IO.Path]::GetDirectoryName($BindingPath)) ("lease-closure-launch-" + $ClosurePlanSha256 + ".json")
    $launchFile = Read-Scope $launchPath $owners -Immutable
    $launch = ConvertFrom-Json -InputObject $launchFile.raw
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    if ((@($launch.PSObject.Properties.Name | Sort-Object -CaseSensitive) -join ',') -cne
        'host_pid,plan_sha256,python_pid,schema_version,started_at_ms' -or
        $launch.plan_sha256 -isnot [string] -or $launch.plan_sha256 -cne $ClosurePlanSha256 -or
        $launch.schema_version -isnot [int] -or $launch.schema_version -ne 1 -or
        $launch.host_pid -isnot [int] -or $launch.host_pid -ne [long]$ClosureHostPid -or
        $launch.python_pid -isnot [int] -or $launch.python_pid -le 0 -or
        $launch.started_at_ms -isnot [long] -or $launch.started_at_ms -lt [long]$ClosureStartedMs -or
        $launch.started_at_ms -gt $now -or $now - $launch.started_at_ms -gt 25000) {
        throw 'transition_closure_receipt_invalid'
    }
    $file = Read-Scope $BindingPath $owners -Immutable
    $record = ConvertFrom-Json -InputObject $file.raw
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    if ((@($record.PSObject.Properties.Name | Sort-Object -CaseSensitive) -join ',') -cne
        'generation,node_proofs,observed_at_ms,plan_sha256,python_pid,schema_version' -or
        (@($record.node_proofs.PSObject.Properties.Name | Sort-Object -CaseSensitive) -join ',') -cne 'A,B' -or
        $record.plan_sha256 -isnot [string] -or $record.plan_sha256 -cne $ClosurePlanSha256 -or
        ($record.schema_version -isnot [int] -and $record.schema_version -isnot [long]) -or $record.schema_version -ne 1 -or
        ($record.generation -isnot [int] -and $record.generation -isnot [long]) -or
        ($record.python_pid -isnot [int] -and $record.python_pid -isnot [long]) -or
        $record.node_proofs.A -isnot [string] -or $record.node_proofs.B -isnot [string] -or
        $record.generation -ne [long]$ClosureGeneration -or $record.python_pid -ne $launch.python_pid -or
        ($record.observed_at_ms -isnot [long] -and $record.observed_at_ms -isnot [int]) -or
        $record.observed_at_ms -lt $launch.started_at_ms -or $record.observed_at_ms -gt $now -or
        $now - $record.observed_at_ms -gt 25000 -or $record.node_proofs.A -cnotmatch $pattern -or
        $record.node_proofs.B -cnotmatch $pattern -or $record.node_proofs.A -ceq $record.node_proofs.B) {
        throw 'transition_closure_receipt_invalid'
    }
    [ordered]@{plan_sha256=$record.plan_sha256;generation=$record.generation;node_proofs=$record.node_proofs} |
        ConvertTo-Json -Compress
}

function Read-RejoinReceipt {
    if ($BindingPath -cnotmatch '\AC:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}\\journal\z' -or
        $ClosurePlanSha256 -cnotmatch $pattern -or
        $ClosureServiceSid -cnotmatch '\AS-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}\z' -or
        $ClosureHostPid -cnotmatch '\A[1-9][0-9]{0,9}\z' -or
        $ClosureStartedMs -cnotmatch '\A[1-9][0-9]{0,17}\z' -or
        $RejoinMode -cnotin @('initialize','stage-rejoin','observe-rejoin-stage','check-rejoin')) {
        throw 'transition_rejoin_receipt_invalid'
    }
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $owners = @('S-1-5-18','S-1-5-32-544',$sid,$ClosureServiceSid)
    $launchPattern = '\Arejoin-launch-' + $ClosurePlanSha256 + '-([a-f0-9]{32})\.json\z'
    $leaves = @(Get-ChildItem -LiteralPath $BindingPath -Force)
    if ($leaves.Count -gt 64) { throw 'transition_rejoin_receipt_invalid' }
    $launch = $null
    foreach ($leaf in $leaves) {
        $match = [regex]::Match($leaf.Name,$launchPattern)
        if (-not $match.Success) { continue }
        $probe = $match.Groups[1].Value
        $entry = ConvertFrom-Json -InputObject (Read-Scope $leaf.FullName $owners -Immutable).raw
        if ((@($entry.PSObject.Properties.Name | Sort-Object -CaseSensitive) -join ',') -cne
            'host_pid,mode,plan_sha256,probe,python_pid,schema_version,started_at_ms' -or
            $entry.plan_sha256 -isnot [string] -or $entry.plan_sha256 -cne $ClosurePlanSha256 -or
            $entry.probe -isnot [string] -or $entry.probe -cne $probe -or
            $entry.mode -isnot [string] -or $entry.mode -cnotin @('initialize','stage-rejoin','observe-rejoin-stage','check-rejoin') -or
            $entry.schema_version -isnot [int] -or $entry.schema_version -ne 1 -or
            $entry.host_pid -isnot [int] -or $entry.host_pid -lt 1 -or
            $entry.python_pid -isnot [int] -or $entry.python_pid -lt 1 -or
            $entry.started_at_ms -isnot [long] -or $entry.started_at_ms -lt 1) {
            throw 'transition_rejoin_receipt_invalid'
        }
        if ($entry.host_pid -ne [long]$ClosureHostPid -or $entry.started_at_ms -lt [long]$ClosureStartedMs) { continue }
        if ($null -ne $launch -or $entry.mode -cne $RejoinMode) { throw 'transition_rejoin_receipt_invalid' }
        $launch = $entry
    }
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $launchMaxAge = if ($RejoinMode -ceq 'stage-rejoin') { 150000 } else { 25000 }
    if ($null -eq $launch -or $launch.started_at_ms -gt $now -or $now - $launch.started_at_ms -gt $launchMaxAge) {
        throw 'transition_rejoin_receipt_invalid'
    }
    $path = Join-Path $BindingPath ("rejoin-result-" + $ClosurePlanSha256 + "-" + $launch.probe + ".json")
    $result = ConvertFrom-Json -InputObject (Read-Scope $path $owners -Immutable).raw
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $keys = 'authority_opened,mode,observed_at_ms,phase,plan_sha256,probe,python_pid,rejoin_stage_sha256,schema_version,status'
    if ($RejoinMode -ceq 'check-rejoin') {
        $keys = $keys.Replace(',schema_version,status',',revision,schema_version,standby_rejoined,status')
    }
    $walKeys = @(@('replica_fence_wal','source_fence_wal') | Where-Object { $null -ne $result.PSObject.Properties[$_] })
    if ($walKeys.Count -gt 1) { throw 'transition_rejoin_receipt_invalid' }
    if ($walKeys.Count -eq 1) {
        $walKey = $walKeys[0]
        $wal = $result.PSObject.Properties[$walKey].Value
        if ($RejoinMode -ceq 'initialize' -or $wal -isnot [string] -or
            $wal -cnotmatch '\A[a-f0-9]{16}\z' -or $wal -ceq '0000000000000000') {
            throw 'transition_rejoin_receipt_invalid'
        }
        $keys = ((($keys -split ',') + $walKey) | Sort-Object -CaseSensitive) -join ','
    }
    $expectedPhase = if ($RejoinMode -ceq 'initialize') { 'fencing' } else { 'rejoining' }
    if ((@($result.PSObject.Properties.Name | Sort-Object -CaseSensitive) -join ',') -cne $keys -or
        $result.schema_version -isnot [int] -or $result.schema_version -ne 1 -or
        $result.plan_sha256 -isnot [string] -or $result.plan_sha256 -cne $ClosurePlanSha256 -or
        $result.mode -isnot [string] -or $result.mode -cne $RejoinMode -or
        $result.probe -isnot [string] -or $result.probe -cne $launch.probe -or $result.python_pid -isnot [int] -or
        $result.python_pid -ne $launch.python_pid -or $result.observed_at_ms -isnot [long] -or
        $result.observed_at_ms -lt $launch.started_at_ms -or $result.observed_at_ms -gt $now -or
        $now - $launch.started_at_ms -gt $launchMaxAge -or $now - $result.observed_at_ms -gt 25000 -or
        $result.status -isnot [string] -or $result.status -cne 'PASS' -or
        $result.phase -isnot [string] -or $result.phase -cne $expectedPhase -or
        $result.authority_opened -isnot [bool] -or $result.authority_opened -or
        $result.rejoin_stage_sha256 -isnot [string] -or $result.rejoin_stage_sha256 -cnotmatch $pattern) { throw 'transition_rejoin_receipt_invalid' }
    $reply = [ordered]@{status='PASS';phase=$expectedPhase;authority_opened=$false;rejoin_stage_sha256=$result.rejoin_stage_sha256}
    if ($walKeys.Count -eq 1) { $reply[$walKey] = $wal }
    if ($RejoinMode -ceq 'check-rejoin') {
        if ($result.standby_rejoined -isnot [bool] -or -not $result.standby_rejoined -or
            ($result.revision -isnot [int] -and $result.revision -isnot [long]) -or $result.revision -lt 1) {
            throw 'transition_rejoin_receipt_invalid'
        }
        $reply['standby_rejoined'] = $true
        $reply['revision'] = $result.revision
    }
    $reply | ConvertTo-Json -Compress
}

try {
    $recovering = $PSBoundParameters.ContainsKey('RecoveryAttemptId') -or
        $PSBoundParameters.ContainsKey('RecoveryPredecessorTerminalSha256') -or
        $PSBoundParameters.ContainsKey('RecoveryPredecessorServiceSha256')
    if ($recovering -and ($ReadRejoinReceipt -or $ReadClosureReceipt -or $ReadPriorServiceArgs -or $Retire -or
            $ResumeRetire -or $ObserveRetired -or $TransitionHandoffSha256 -or $TransitionBoundarySha256 -or
            $ExpectedServiceInstanceSha256 -cnotmatch $pattern -or $RecoveryAttemptId -cnotmatch $pattern -or
            $RecoveryPredecessorTerminalSha256 -cnotmatch $pattern -or
            $RecoveryPredecessorServiceSha256 -cnotmatch $pattern -or $RecoveryAttemptId -ceq $AttemptId)) {
        throw 'live_authority_recovery_scope'
    }
    if ($RequireTerminalInitial -and -not $ReadPriorServiceArgs) { throw 'initial_service_retirement_scope' }
    if ($ReadRejoinReceipt) {
        if ($ReadPriorServiceArgs -or $ReadClosureReceipt -or $Retire -or $ResumeRetire -or $ObserveRetired -or $TransitionHandoffSha256 -or $TransitionBoundarySha256) {
            throw 'transition_rejoin_receipt_invalid'
        }
        Read-RejoinReceipt
        return
    }
    if ($ReadClosureReceipt) {
        if ($ReadPriorServiceArgs -or $Retire -or $ResumeRetire -or $ObserveRetired -or $TransitionHandoffSha256 -or $TransitionBoundarySha256) {
            throw 'transition_closure_receipt_invalid'
        }
        Read-ClosureReceipt
        return
    }
    $retireModes = @(@($Retire,$ObserveRetired,$ResumeRetire) | Where-Object { $_.IsPresent }).Count
    if (($ReadPriorServiceArgs -and ($retireModes -ne 0 -or $TransitionHandoffSha256 -or
            $TransitionBoundarySha256 -or $ExpectedServiceInstanceSha256 -cnotmatch $pattern)) -or
        $retireModes -gt 1 -or ($retireModes -eq 1 -and $ExpectedServiceInstanceSha256 -cnotmatch $pattern)) {
        throw 'initial_service_retirement_scope'
    }
    if ($TransitionHandoffSha256 -or $TransitionBoundarySha256) {
        if ($retireModes -ne 0) { throw 'transition_authority_scope' }
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        Read-TransitionAuthority @('S-1-5-18','S-1-5-32-544',$sid)
        return
    }
    $fileName = if ($recovering) { 'initial-recovery-service-' + $RecoveryAttemptId + '.json' }
        else { 'initial-service-' + $AttemptId + '.json' }
    if ($LegacyPlanSha256 -cnotmatch $pattern -or $AttemptId -cnotmatch $pattern -or $PriorSha256 -cnotmatch $pattern -or
        $BindingPath -cnotmatch ('\AC:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}\\' + [regex]::Escape($fileName) + '\z')) {
        throw 'live_authority_scope'
    }
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $admins = @('S-1-5-18','S-1-5-32-544',$sid)
    $inputFile = Read-Scope $BindingPath $admins
    if ($ExpectedServiceInstanceSha256 -and ($ExpectedServiceInstanceSha256 -cnotmatch $pattern -or
        (Get-Digest $inputFile.raw) -cne $ExpectedServiceInstanceSha256)) { throw 'live_authority_service_instance' }
    $plan = ConvertFrom-Json -InputObject $inputFile.raw
    $fields = @($plan.PSObject.Properties.Name | Sort-Object -CaseSensitive)
    if (($fields -join ',') -cne 'attempt_id,controller_plan_sha256,first_write_proof_sha256,legacy_plan_sha256,service_created_ticks,service_image_path_sha256,service_name,service_pid,state_path' -or
        $plan.attempt_id -cne $AttemptId -or $plan.legacy_plan_sha256 -cne $LegacyPlanSha256 -or
        $plan.controller_plan_sha256 -cnotmatch $pattern -or $plan.first_write_proof_sha256 -cnotmatch $pattern -or
        $plan.service_image_path_sha256 -cnotmatch $pattern -or $plan.service_name -cnotmatch '\AN100HA_[a-f0-9]{24}_controller\z' -or
        $plan.service_created_ticks -cnotmatch '\A[1-9][0-9]{1,19}\z' -or
        ($plan.service_pid -isnot [int] -and $plan.service_pid -isnot [long]) -or $plan.service_pid -lt 1 -or
        -not $plan.state_path.EndsWith('\journal\controller-state.json',[StringComparison]::Ordinal)) { throw 'live_authority_scope' }
    $identity = [Security.Principal.NTAccount]::new('NT SERVICE',$plan.service_name).Translate([Security.Principal.SecurityIdentifier]).Value
    if ($recovering -and $plan.service_name -cne ('N100HA_' + $RecoveryAttemptId.Substring(0,24) + '_controller')) {
        throw 'live_authority_recovery_scope'
    }
    # Bind directly to the validated service key instead of enumerating Win32_Service.
    $service = Find-BoundService $plan
    if ($ReadPriorServiceArgs) {
        if ($service.State -cne 'Stopped' -or $service.ProcessId -ne 0 -or
            $service.StartMode -cnotin @('Manual','Disabled') -or
            ($RequireTerminalInitial -and $service.StartMode -cne 'Disabled') -or $service.ServiceType -cne 'Own Process' -or
            $service.DesktopInteract -or $service.StartName -ine ('NT SERVICE\' + $plan.service_name) -or
            $service.PathName -cnotmatch '\A"[A-Za-z]:\\[A-Za-z0-9_.\\-]+\\windows-service-host\.exe"\z') {
            throw 'live_authority_prior_args'
        }
        $directory = [IO.Path]::GetDirectoryName($service.PathName.Trim('"'))
        $argsPath = Join-Path $directory 'service.args'
        $ClosureServiceSid = $identity
        $savedArgs = Read-Scope $argsPath ($admins + @($identity)) -Immutable
        $lines = $savedArgs.raw.Split([char]10)
        if ($lines.Count -ne 14 -or $lines[-1] -cne '' -or $lines[0] -cne 'format=enforced-service-v1' -or
            @($lines | Where-Object { $_ -cmatch '\A(?:service_name|service_sid|plan_sha256|mode)=' }).Count -ne 4 -or
            ('service_name=' + $plan.service_name) -cnotin $lines -or ('service_sid=' + $identity) -cnotin $lines -or
            ('plan_sha256=' + $plan.controller_plan_sha256) -cnotin $lines -or 'mode=serve' -cnotin $lines) {
            throw 'live_authority_prior_args'
        }
        $terminal = $null
        if ($RequireTerminalInitial) {
            $terminal = Read-Scope $plan.state_path ($admins + @($identity)) -Immutable
            $record = ConvertFrom-Json -InputObject $terminal.raw
            if ($record.kind -cne 'initial-wsl-cutover' -or $record.schema_version -ne 1 -or
                $record.phase -cnotin @('lease_lost','local_revoke_observed') -or $record.generation -ne 1 -or
                $record.attempt_id -cne $AttemptId -or $record.legacy_plan_sha256 -cne $LegacyPlanSha256 -or
                $record.controller_plan_sha256 -cne $plan.controller_plan_sha256) {
                throw 'live_authority_prior_args'
            }
        }
        $confirmed = Find-BoundService $plan
        if ($confirmed.State -cne 'Stopped' -or $confirmed.ProcessId -ne 0 -or
            $confirmed.StartMode -cne $service.StartMode) { throw 'live_authority_prior_args' }
        $reply = [ordered]@{prior_args_sha256=(Get-Digest $savedArgs.raw);
            service_instance_sha256=$ExpectedServiceInstanceSha256}
        if ($RequireTerminalInitial) {
            $settled = Read-Scope $plan.state_path ($admins + @($identity)) -Immutable
            if ($settled.raw -cne $terminal.raw) { throw 'live_authority_prior_args' }
            $reply['terminal_state_sha256'] = Get-Digest $settled.raw
        }
        $reply | ConvertTo-Json -Compress
        return
    }
    if ($ObserveRetired) {
        Assert-Retired $service
        [ordered]@{initial_service_retired=$true;retirement_sha256=(Get-Digest ($inputFile.raw + ':retired:' + $PriorSha256))}|ConvertTo-Json -Compress
        return
    }
    if ($service.State -cne 'Running' -or $service.ProcessId -ne $plan.service_pid) { throw 'live_authority_service' }
    $process = Get-Process -Id $plan.service_pid -ErrorAction Stop
    try {
        if ($process.HasExited -or $process.SessionId -ne 0 -or
            ('"' + $process.MainModule.FileName + '"') -ine $service.PathName -or
            $process.StartTime.ToUniversalTime().Ticks.ToString() -cne $plan.service_created_ticks) {
            throw 'live_authority_service'
        }
        if ($Retire -or $ResumeRetire) {
            if ($Retire -and ($service.StartMode -cne 'Manual' -or $process.HasExited -or
                $service.ChangeStartMode('Disabled').ReturnValue -ne 0)) { throw 'initial_service_disable_unconfirmed' }
            $service = Find-BoundService $plan
            if ($service.StartMode -cne 'Disabled') { throw 'initial_service_disable_unconfirmed' }
            if ($service.State -ceq 'Running' -and $service.ProcessId -eq $plan.service_pid -and -not $process.HasExited) {
                if ($service.StopService().ReturnValue -ne 0) { throw 'initial_service_stop_unconfirmed' }
            }
            if (-not $process.WaitForExit(15000)) { throw 'initial_service_stop_unconfirmed' }
            Assert-Retired (Find-BoundService $plan)
            [ordered]@{initial_service_retired=$true;retirement_sha256=(Get-Digest ($inputFile.raw + ':retired:' + $PriorSha256))}|ConvertTo-Json -Compress
            return
        }
        $saved = Read-Scope $plan.state_path ($admins + $identity)
        $record = ConvertFrom-Json -InputObject $saved.raw
        $age = ([DateTime]::UtcNow - $saved.write_time).TotalMilliseconds
        $kind = if ($recovering) { 'initial-wsl-cutover-recovery' } else { 'initial-wsl-cutover' }
        if ($record.phase -cne 'serving' -or $record.kind -cne $kind -or $record.schema_version -ne 1 -or
            $record.generation -ne 1 -or $record.sequence -lt 1 -or $record.attempt_id -cne $AttemptId -or
            $record.legacy_plan_sha256 -cne $LegacyPlanSha256 -or $record.controller_plan_sha256 -cne $plan.controller_plan_sha256 -or
            $record.first_write_proof_sha256 -cne $plan.first_write_proof_sha256 -or $record.lease_proof_sha256 -cnotmatch $pattern -or
            $record.proof_elapsed_ms -lt 0 -or $record.proof_elapsed_ms -ge 4000 -or ($age + $record.proof_elapsed_ms) -ge 4800 -or $age -lt 0 -or $age -gt 2900 -or
            $process.HasExited) { throw 'live_authority_lease' }
        if ($recovering) {
            $names = @($record.recovery.PSObject.Properties.Name | Sort-Object -CaseSensitive)
            foreach ($field in @('schema_version','generation','sequence','revision')) {
                if ($record.$field -isnot [int] -and $record.$field -isnot [long]) {
                    throw 'live_authority_recovery_scope'
                }
            }
            if (($names -join ',') -cne 'attempt_id,predecessor_service_sha256,predecessor_terminal_sha256' -or
                $record.revision -ne 4 -or ($record.proof_elapsed_ms -isnot [int] -and
                    $record.proof_elapsed_ms -isnot [long] -and $record.proof_elapsed_ms -isnot [double] -and $record.proof_elapsed_ms -isnot [decimal]) -or
                [double]::IsNaN([double]$record.proof_elapsed_ms) -or
                $record.recovery.attempt_id -cne $RecoveryAttemptId -or
                $record.recovery.predecessor_service_sha256 -cne $RecoveryPredecessorServiceSha256 -or
                $record.recovery.predecessor_terminal_sha256 -cne $RecoveryPredecessorTerminalSha256 -or
                $service.StartMode -cne 'Manual' -or $service.StartName -ine ('NT SERVICE\' + $plan.service_name)) {
                throw 'live_authority_recovery_scope'
            }
            $service = Find-BoundService $plan
            $confirmedAge = ([DateTime]::UtcNow - $saved.write_time).TotalMilliseconds
            if ($service.State -cne 'Running' -or $service.ProcessId -ne $plan.service_pid -or
                $service.StartMode -cne 'Manual' -or $service.StartName -ine ('NT SERVICE\' + $plan.service_name) -or
                $service.ServiceType -cne 'Own Process' -or $service.DesktopInteract -or $process.HasExited -or
                $confirmedAge -lt 0 -or $confirmedAge -gt 2900 -or
                $confirmedAge + $record.proof_elapsed_ms -ge 4800) {
                throw 'live_authority_recovery_scope'
            }
        }
        [ordered]@{windows_generation=1;windows_generation_sha256=(Get-Digest ($inputFile.raw + ':' + $saved.raw + ':' + $PriorSha256));
            live_lease_sha256=$record.lease_proof_sha256}|ConvertTo-Json -Compress
    } finally { $process.Dispose() }
} catch { exit 1 }
