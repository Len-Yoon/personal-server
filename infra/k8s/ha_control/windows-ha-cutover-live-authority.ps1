param(
    [string]$BindingPath,
    [string]$LegacyPlanSha256,
    [string]$AttemptId,
    [string]$PriorSha256,
    [string]$ExpectedServiceInstanceSha256
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$pattern = '\A[a-f0-9]{64}\z'

function Get-Digest([string]$Value) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value))).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Read-Scope([string]$Path, [string[]]$Owners) {
    if ($Path -cnotmatch '\A[A-Za-z]:\\[A-Za-z0-9_.\\-]+\z' -or $Path -match '\\\.\.?(\\|$)') { throw 'live_authority_path' }
    $entry = Get-Item -LiteralPath $Path -Force
    if ($entry -isnot [IO.FileInfo] -or $entry.Length -lt 1 -or $entry.Length -gt 8192 -or
        ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'live_authority_path' }
    $node = $entry.Directory
    while ($null -ne $node) {
        if ($node.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'live_authority_path' }
        $node = $node.Parent
    }
    foreach ($item in @($entry,$entry.Directory)) {
        $sd = Get-Acl -LiteralPath $item.FullName
        if ($sd.GetOwner([Security.Principal.SecurityIdentifier]).Value -cnotin $Owners) { throw 'live_authority_acl' }
        $mask = [Security.AccessControl.FileSystemRights]'Write,Delete,DeleteSubdirectoriesAndFiles,ChangePermissions,TakeOwnership'
        foreach ($rule in $sd.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
            if ($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Value -cnotin $Owners -and
                -not ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -and
                ($rule.FileSystemRights -band $mask)) { throw 'live_authority_acl' }
        }
    }
    # Snapshot the path timestamp before opening. A journal replaced between the
    # snapshot and open can only be judged older than the new record itself.
    $written = $entry.LastWriteTimeUtc
    $stream = [IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,([IO.FileShare]::Read -bor [IO.FileShare]::Delete))
    try {
        if ($stream.Length -lt 1 -or $stream.Length -gt 8192) { throw 'live_authority_path' }
        $reader = [IO.BinaryReader]::new($stream,[Text.UTF8Encoding]::new($false,$true),$true)
        try {
            $raw = $reader.ReadBytes([int]$stream.Length)
            if ($stream.ReadByte() -ne -1) { throw 'live_authority_path' }
            return [pscustomobject]@{raw=[Text.UTF8Encoding]::new($false,$true).GetString($raw);write_time=$written}
        } finally { $reader.Dispose() }
    } finally { $stream.Dispose() }
}

try {
    if ($LegacyPlanSha256 -cnotmatch $pattern -or $AttemptId -cnotmatch $pattern -or $PriorSha256 -cnotmatch $pattern -or
        $BindingPath -cnotmatch '\AC:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}\\initial-service-[a-f0-9]{64}\.json\z' -or
        -not $BindingPath.EndsWith(('-' + $AttemptId + '.json'),[StringComparison]::Ordinal)) { throw 'live_authority_scope' }
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
    $service = Get-CimInstance Win32_Service -Filter ("Name='" + $plan.service_name + "'") -ErrorAction Stop
    if ($null -eq $service -or $service.Name -cne $plan.service_name -or $service.State -cne 'Running' -or
        $service.ProcessId -ne $plan.service_pid -or (Get-Digest $service.PathName) -cne $plan.service_image_path_sha256) { throw 'live_authority_service' }
    $process = Get-Process -Id $plan.service_pid -ErrorAction Stop
    try {
        if ($process.HasExited -or $process.SessionId -ne 0 -or
            ('"' + $process.MainModule.FileName + '"') -ine $service.PathName -or
            $process.StartTime.ToUniversalTime().Ticks.ToString() -cne $plan.service_created_ticks) {
            throw 'live_authority_service'
        }
        $saved = Read-Scope $plan.state_path ($admins + $identity)
        $record = ConvertFrom-Json -InputObject $saved.raw
        $age = ([DateTime]::UtcNow - $saved.write_time).TotalMilliseconds
        if ($record.phase -cne 'serving' -or $record.kind -cne 'initial-wsl-cutover' -or $record.schema_version -ne 1 -or
            $record.generation -ne 1 -or $record.sequence -lt 1 -or $record.attempt_id -cne $AttemptId -or
            $record.legacy_plan_sha256 -cne $LegacyPlanSha256 -or $record.controller_plan_sha256 -cne $plan.controller_plan_sha256 -or
            $record.first_write_proof_sha256 -cne $plan.first_write_proof_sha256 -or $record.lease_proof_sha256 -cnotmatch $pattern -or
            $record.proof_elapsed_ms -lt 0 -or $record.proof_elapsed_ms -ge 3000 -or ($age + $record.proof_elapsed_ms) -ge 4800 -or $age -lt 0 -or $age -gt 2900 -or
            $process.HasExited) { throw 'live_authority_lease' }
        [ordered]@{windows_generation=1;windows_generation_sha256=(Get-Digest ($inputFile.raw + ':' + $saved.raw + ':' + $PriorSha256));
            live_lease_sha256=$record.lease_proof_sha256}|ConvertTo-Json -Compress
    } finally { $process.Dispose() }
} catch { exit 1 }
