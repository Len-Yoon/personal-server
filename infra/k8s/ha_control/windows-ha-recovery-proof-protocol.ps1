# Wire format only. The hosting broker supplies protected pins and a fresh observation.
function Assert-HaRecoveryPins([System.Collections.IDictionary]$Pins) {
    $hashNames = @('attempt_id','broker_scope_sha256','guest_boot_sha256','hold_sha256',
        'plan_sha256','predecessor_terminal_sha256','resource_set_sha256','windows_boot_sha256')
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
