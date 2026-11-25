# Wire format only. The hosting broker supplies protected pins and a fresh observation.
function Assert-HaRecoveryPins([System.Collections.IDictionary]$Pins) {
    $hashNames = @('attempt_id','broker_scope_sha256','guest_boot_sha256','hold_sha256',
        'plan_sha256','predecessor_service_sha256','predecessor_terminal_sha256','resource_set_sha256','windows_boot_sha256')
    $pinNames = $hashNames + @('service_sid','purpose')
    if ($Pins.Count -ne $pinNames.Count -or
        @($pinNames | Where-Object { $_ -cnotin $Pins.Keys }).Count -ne 0) {
        throw 'recovery_proof_pins'
    }
    foreach ($name in $hashNames) {
        if ($Pins[$name] -isnot [string] -or $Pins[$name] -cnotmatch '\A[a-f0-9]{64}\z') {
            throw 'recovery_proof_pin'
        }
    }
    if ($Pins.service_sid -isnot [string] -or
        $Pins.service_sid -cnotmatch '\AS-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}\z' -or
        $Pins.purpose -isnot [string] -or $Pins.purpose -cnotin @('held_admission','provider_isolation')) {
        throw 'recovery_proof_context'
    }
}

function Read-HaRecoveryChallenge([string]$Raw, [System.Collections.IDictionary]$Pins) {
    Assert-HaRecoveryPins $Pins
    if ($Raw.Length -gt 2048) { throw 'recovery_proof_frame' }
    $fields = ConvertFrom-Json -InputObject $Raw
    if ($null -eq $fields -or 'nonce' -cnotin $fields.PSObject.Properties.Name -or
        $fields.nonce -isnot [string] -or $fields.nonce -cnotmatch '\A[a-f0-9]{32}\z') {
        throw 'recovery_proof_nonce'
    }
    $expected = [ordered]@{
        attempt_id=$Pins.attempt_id
        broker_scope_sha256=$Pins.broker_scope_sha256
        generation=1
        guest_boot_sha256=$Pins.guest_boot_sha256
        hold_sha256=$Pins.hold_sha256
        nonce=$fields.nonce
        plan_sha256=$Pins.plan_sha256
        predecessor_service_sha256=$Pins.predecessor_service_sha256
        predecessor_terminal_sha256=$Pins.predecessor_terminal_sha256
        purpose=$Pins.purpose
        resource_set_sha256=$Pins.resource_set_sha256
        scope='n100ha-initial-recovery-v1'
        service_sid=$Pins.service_sid
        windows_boot_sha256=$Pins.windows_boot_sha256
    }
    if ((ConvertTo-Json -InputObject $expected -Compress) -cne $Raw) { throw 'recovery_proof_binding' }
    return $expected
}

function New-HaRecoveryProofFrame(
    [string]$Raw, [System.Collections.IDictionary]$Pins, $Observation,
    [Security.Cryptography.HMACSHA256]$Mac
) {
    $challenge = Read-HaRecoveryChallenge $Raw $Pins
    $names = @($Observation.PSObject.Properties.Name | Sort-Object -CaseSensitive)
    if (($names -join ',') -cne 'challenge_sha256,closed,observation_sha256' -or
        $Observation.closed -isnot [bool] -or -not $Observation.closed -or
        $Observation.observation_sha256 -isnot [string] -or
        $Observation.observation_sha256 -cnotmatch '\A[a-f0-9]{64}\z') {
        throw 'recovery_proof_observation'
    }
    $encoding = [Text.UTF8Encoding]::new($false, $true)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $challengeDigest = [BitConverter]::ToString($sha.ComputeHash($encoding.GetBytes($Raw))).
            Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
    if ($Observation.challenge_sha256 -isnot [string] -or $Observation.challenge_sha256 -cne $challengeDigest) {
        throw 'recovery_proof_observation_binding'
    }
    $challenge.Add('closed', $true)
    $challenge.Add('observation_sha256', $Observation.observation_sha256)
    $bodyText = ConvertTo-Json -InputObject $challenge -Compress
    $body = $encoding.GetBytes($bodyText)
    $macInput = $encoding.GetBytes($challenge.scope + [char]0 + $bodyText)
    $tag = [BitConverter]::ToString($Mac.ComputeHash($macInput)).Replace('-', '').ToLowerInvariant()
    $envelope = [ordered]@{body=[Convert]::ToBase64String($body); mac=$tag}
    return (ConvertTo-Json -InputObject $envelope -Compress) + "`n"
}

function New-HaRecoveryPredecessorScope($Binding, $Terminal, [string]$InstallationId, $Pins) {
    if ($InstallationId -cnotmatch '\A[a-f0-9]{64}\z') { throw 'recovery_proof_predecessor' }
    $name = 'N100HA_' + $InstallationId.Substring(0,24) + '_controller'
    if ($Binding.service_name -cne $name -or
        $Binding.service_image_path_sha256 -isnot [string] -or
        $Binding.service_image_path_sha256 -cnotmatch '\A[a-f0-9]{64}\z' -or
        $Binding.attempt_id -isnot [string] -or $Binding.attempt_id -cnotmatch '\A[a-f0-9]{64}\z' -or
        $Binding.attempt_id -ceq $Pins.attempt_id -or $Binding.attempt_id -cne $Terminal.attempt_id -or
        $Binding.legacy_plan_sha256 -cne $Pins.plan_sha256 -or
        $Terminal.legacy_plan_sha256 -cne $Pins.plan_sha256 -or
        $Binding.controller_plan_sha256 -isnot [string] -or
        $Binding.controller_plan_sha256 -cnotmatch '\A[a-f0-9]{64}\z' -or
        $Binding.controller_plan_sha256 -cne $Terminal.controller_plan_sha256 -or
        $Terminal.kind -cne 'initial-wsl-cutover' -or $Terminal.phase -cnotin @('lease_lost','local_revoke_observed') -or
        ($Terminal.generation -isnot [int] -and $Terminal.generation -isnot [long]) -or
        $Terminal.generation -ne 1) { throw 'recovery_proof_predecessor' }
    [pscustomobject]@{name=$name;image_path_sha256=$Binding.service_image_path_sha256}
}

function Read-HaRecoveryPredecessorScm([string]$Name) {
    $scm = [TrustedStoppedV1.Native]::new()
    try {
        $scm.OpenForRead($Name)
        return $scm.Read($Name)
    } finally { $scm.Dispose() }
}

function Assert-HaRecoveryPredecessorClosed($Predecessor, [long]$Deadline) {
    if ([Diagnostics.Stopwatch]::GetTimestamp() -ge $Deadline) { throw 'recovery_proof_predecessor_deadline' }
    $name = $Predecessor.name
    if ($name -cnotmatch '\AN100HA_[a-f0-9]{24}_controller\z') { throw 'recovery_proof_predecessor' }
    $old = Read-HaRecoveryPredecessorScm $name
    # SERVICE_DISABLED=4, SERVICE_STOPPED=1, SERVICE_WIN32_OWN_PROCESS=16.
    if ($old.Name -cne $name -or $old.Start -ne 4 -or $old.State -ne 1 -or
        $old.Pid -ne 0 -or $old.Type -ne 16 -or
        $old.Account -ine ('NT SERVICE\' + $name) -or $old.Image -isnot [string]) {
        throw 'recovery_proof_predecessor'
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $image = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($old.Image))).
            Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
    if ($image -cne $Predecessor.image_path_sha256) { throw 'recovery_proof_predecessor' }
    if ([Diagnostics.Stopwatch]::GetTimestamp() -ge $Deadline) { throw 'recovery_proof_predecessor_deadline' }
}

function Get-HaBoundRecoveryObservation([string]$Raw, [long]$Deadline, $Predecessor) {
    Assert-HaRecoveryPredecessorClosed $Predecessor $Deadline
    $fact = Get-HaRecoveryObservation $Raw $Deadline
    Assert-HaRecoveryPredecessorClosed $Predecessor $Deadline
    return $fact
}
