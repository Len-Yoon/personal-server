# Starts the existing manual controller after protected activation inputs have been staged.
param(
    [string]$ServiceName,
    [string]$ServiceHostSha256,
    [string]$PlanPath,
    [string]$PlanSha256,
    [string]$ApprovalPath,
    [string]$ApprovalSha256,
    [string]$BindingPath,
    [string]$PreflightPath,
    [string]$PreflightSha256,
    [string]$ScmVerifierPath,
    [string]$ExpectedActivationIntentSha256,
    [string]$RecoveryAttemptId,
    [string]$RecoveryReservationSha256,
    [switch]$StartTransition,
    [switch]$VerifyPreparedTransition,
    [switch]$PublishTransitionArgs,
    [string]$PreviousArgsSha256,
    [switch]$CheckLeaseClosed,
    [switch]$RestoreClosureArgs,
    [switch]$ObserveClosureCheck,
    [string]$RejoinMode,
    [string]$RejoinRequestId,
    [switch]$RestoreRejoinArgs,
    [switch]$InspectRejoinStage,
    [switch]$ObserveTransition,
    [switch]$StopTransition,
    [string]$ExpectedServicePid,
    [string]$ExpectedServiceCreatedTicks,
    [switch]$Go
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$hashPattern = '\A[a-f0-9]{64}\z'

function Assert-RecoveryStartScope($Plan,$Approval,[string]$Attempt,[string]$ReservationSha,
                                   [string]$Name,[string]$HostPath,[string]$Parent){
    $recovery=$Plan.initial_cutover_recovery
    $identity='N100HA_'+$Attempt.Substring(0,24)
    $root=Join-Path $Parent $identity
    $approvalFields=@($Approval.PSObject.Properties.Name|Sort-Object -CaseSensitive)
    foreach($field in @('action','authority_kind','installation_id','installed_at','plan_sha256','source_commit')){
        if($Approval.$field -isnot [string]){throw 'initial_service_recovery_scope'}
    }
    if(($approvalFields -join ',') -cne 'action,authority_kind,installation_id,installed_at,plan_sha256,schema_version,source_commit' -or
        ($Approval.schema_version -isnot [int] -and $Approval.schema_version -isnot [long]) -or
        $Approval.schema_version -ne 1 -or $Approval.authority_kind -cne 'installed-runtime' -or
        $Approval.installation_id -cnotmatch $hashPattern -or $Approval.plan_sha256 -cnotmatch $hashPattern -or
        $Approval.source_commit -cnotmatch '\A[a-f0-9]{40}\z' -or
        $Approval.installed_at -cnotmatch '\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z\z' -or
        [DateTimeOffset]::Parse($Approval.installed_at,[Globalization.CultureInfo]::InvariantCulture) -ge [DateTimeOffset]::UtcNow){
        throw 'initial_service_recovery_scope'
    }
    if($Parent -cne 'C:\ProgramData' -or $Plan.installation_id -cnotmatch $hashPattern -or
        $Attempt.Substring(0,24) -ceq $Plan.installation_id.Substring(0,24) -or
        $Name -cne ($identity+'_controller') -or
        $HostPath -cne (Join-Path $root 'host\windows-service-host.exe') -or
        $Approval.action -cne 'ha-recover-initial-writer' -or
        $Plan.initial_service_start.reservation_receipt_sha256 -cne $ReservationSha -or
        (@($recovery.PSObject.Properties.Name | Sort-Object -CaseSensitive) -join ',') -cne
            'attempt_id,predecessor_service_sha256,predecessor_terminal_sha256' -or
        $recovery.attempt_id -cne $Attempt -or $recovery.attempt_id -ceq $Plan.initial_cutover_handoff.attempt_id -or
        $recovery.predecessor_service_sha256 -cnotmatch $hashPattern -or
        $recovery.predecessor_terminal_sha256 -cnotmatch $hashPattern){
        throw 'initial_service_recovery_scope'
    }
    $receipt=Read-PinnedJson (Join-Path $root 'reservation-receipt.json') $ReservationSha
    if($receipt.format -cne 'fresh-recovery-service-reservation-receipt-v1' -or
        $receipt.installation_id -cne $Plan.installation_id -or $receipt.recovery_attempt_id -cne $Attempt){
        throw 'initial_service_recovery_scope'
    }
    foreach($field in @('ready_to_start','runtime_authority_issued','service_started','args_present','namespace_changed')){
        if($receipt.$field -isnot [bool] -or $receipt.$field){throw 'initial_service_recovery_scope'}
    }
    if($receipt.ownership_receipt_sha256 -cnotmatch $hashPattern -or
        $receipt.service_config_sha256 -cnotmatch $hashPattern){throw 'initial_service_recovery_scope'}
    $ownership=Read-PinnedJson (Join-Path $root 'namespace-service-ownership.json') $receipt.ownership_receipt_sha256
    if($ownership.format -cne 'ha-owned-recovery-service-v1' -or
        $ownership.installation_id -cne $Plan.installation_id -or $ownership.recovery_attempt_id -cne $Attempt -or
        $ownership.service_name -cne $Name -or $ownership.service_sid -cne $Plan.windows_enforced.service_sid -or
        $ownership.service_image_sha256 -cne $Plan.windows_enforced.service_image_sha256 -or
        $ownership.service_config_sha256 -cne $receipt.service_config_sha256 -or
        $ownership.predecessor_service_sha256 -cne $recovery.predecessor_service_sha256 -or
        $ownership.predecessor_terminal_sha256 -cne $recovery.predecessor_terminal_sha256 -or
        $ownership.new_owned_service -isnot [bool] -or -not $ownership.new_owned_service -or
        $ownership.scm_registration_complete -isnot [bool] -or -not $ownership.scm_registration_complete){
        throw 'initial_service_recovery_scope'
    }
}

function Assert-RecoveryPredecessorStopped($Plan, [string]$Root) {
    $recovery = $Plan.initial_cutover_recovery
    $binding = Read-PinnedJson (Join-Path $Root 'initial-predecessor-service.json') $recovery.predecessor_service_sha256
    $oldName = 'N100HA_' + $Plan.installation_id.Substring(0,24) + '_controller'
    if ($binding.service_name -cne $oldName -or $binding.attempt_id -cne $Plan.initial_cutover_handoff.attempt_id -or
        $binding.service_image_path_sha256 -cnotmatch $hashPattern) {
        throw 'initial_service_predecessor_unconfirmed'
    }
    $services = @(Get-CimInstance Win32_Service -Filter ("Name='" + $oldName + "'") -OperationTimeoutSec 5 -ErrorAction Stop)
    if ($services.Count -ne 1) { throw 'initial_service_predecessor_unconfirmed' }
    $old = $services[0]
    if ($old.Name -cne $oldName -or $old.State -cne 'Stopped' -or $old.ProcessId -ne 0 -or
        $old.StartMode -cne 'Disabled' -or $old.ServiceType -cne 'Own Process' -or
        $old.DesktopInteract -ne $false -or $old.StartName -ine ('NT SERVICE\'+$oldName) -or
        $old.PathName -isnot [string]) { throw 'initial_service_predecessor_unconfirmed' }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $image = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($old.PathName))).
            Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
    if ($image -cne $binding.service_image_path_sha256) { throw 'initial_service_predecessor_unconfirmed' }
}

function Read-PinnedJson([string]$Path, [string]$Sha) {
    if ($Path -cnotmatch '\A[A-Za-z]:\\[A-Za-z0-9_.\\-]+\z' -or $Path -match '\\\.\.?(\\|$)') {
        throw 'initial_service_input_path'
    }
    $file = Get-Item -LiteralPath $Path -Force
    if ($file -isnot [IO.FileInfo] -or $file.Length -lt 1 -or $file.Length -gt 1048576 -or
        ($file.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'initial_service_input_path' }
    for ($parent = $file.Directory; $null -ne $parent; $parent = $parent.Parent) {
        if ($parent.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'initial_service_input_path' }
    }
    $stream = [IO.File]::Open($Path, 'Open', 'Read', 'Read')
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $observed = [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
        if ($observed -cne $Sha -or $stream.Length -gt 1048576) { throw 'initial_service_input_pin' }
        $stream.Position = 0
        $reader = [IO.StreamReader]::new($stream,[Text.UTF8Encoding]::new($false,$true),$false,1024,$true)
        try { return ConvertFrom-Json -InputObject $reader.ReadToEnd() }
        finally { $reader.Dispose() }
    } finally { $hasher.Dispose(); $stream.Dispose() }
}

function Assert-Executable([string]$Path, [string]$Sha) {
    if ($Path -cnotmatch '\A[A-Za-z]:\\[A-Za-z0-9_.\\-]+\z' -or $Path -match '\\\.\.?(\\|$)' -or
        $Sha -cnotmatch $hashPattern) { throw 'initial_service_executable_path' }
    $item = Get-Item -LiteralPath $Path -Force
    if ($item -isnot [IO.FileInfo] -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $Sha) {
        throw 'initial_service_executable_pin'
    }
    for ($parent = $item.Directory; $null -ne $parent; $parent = $parent.Parent) {
        if ($parent.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'initial_service_executable_path' }
    }
}

function Assert-TransitionPreflight([string]$ExpectedArgs) {
    $ArgsSha256 = $ExpectedArgs
    $checked = ConvertFrom-Json -InputObject (Test-EnforcedServicePreflight)
    if ($checked.scope -cne 'pre_StartService_inventory_and_ACL' -or $checked.host_sha256 -cne $ServiceHostSha256 -or
        $checked.args_sha256 -cne $ExpectedArgs -or $checked.execution_performed -isnot [bool] -or $checked.execution_performed) {
        throw 'initial_service_preflight_unconfirmed'
    }
}

function Assert-TransitionStopped($Plan, [string]$HostPath, [string]$ImagePath) {
    $scm = [TrustedStoppedV1.Native]::new()
    try {
        $scm.OpenForRead($ServiceName)
        [TrustedStoppedV1.Contract]::Inputs($Plan.installation_id,$ServiceName,$HostPath)
        [TrustedStoppedV1.Contract]::Verify($scm.Read($ServiceName),$ServiceName,$ImagePath)
    } finally { $scm.Dispose() }
}

function Write-TransitionArgsStage([string]$Path, [byte[]]$Bytes, $Acl) {
    $file = [IO.FileStream]::new($Path,[IO.FileMode]::CreateNew,
        [Security.AccessControl.FileSystemRights]'WriteData,Synchronize',[IO.FileShare]::None,4096,
        [IO.FileOptions]::WriteThrough,$Acl)
    try { $file.Write($Bytes,0,$Bytes.Length); $file.Flush($true) } finally { $file.Dispose() }
}

function Confirm-ClosureArgsState([string]$ServingSha, [string]$ClosureSha) {
    try { Assert-TransitionPreflight $ServingSha }
    catch {
        if ($_.Exception.Message -cne 'preflight_hash_rejected') { throw }
        Assert-TransitionPreflight $ClosureSha
    }
}

function Restore-TransitionServingArgs([string]$ArgsPath, [string]$RestorePath, [string]$ServingSha, [string]$ClosureSha) {
    Confirm-ClosureArgsState $ServingSha $ClosureSha
    $observedArgs = (Get-FileHash -LiteralPath $ArgsPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($observedArgs -ceq $ClosureSha) {
        $restoreEntry = Get-Item -LiteralPath $RestorePath -Force
        if ($restoreEntry -isnot [IO.FileInfo] -or $restoreEntry.Length -gt 65536) {
            throw 'initial_service_transition_restore_pin'
        }
        Assert-PreflightEntry $restoreEntry $authors ($authors + @($sid)) $true $false
        if ((Get-FileHash -LiteralPath $RestorePath -Algorithm SHA256).Hash.ToLowerInvariant() -cne $ServingSha) {
            throw 'initial_service_transition_restore_pin'
        }
        [IO.File]::Replace($RestorePath,$ArgsPath,[NullString]::Value,$false)
    }
    Assert-TransitionPreflight $ServingSha
}

function Start-PreparedTransitionService($Current, [string]$HostPath, $Plan, $Approval) {
    $scope = $Plan.windows_enforced
    $pins = $Plan.transition_service_start
    $mode = $pins.mode
    $oneShot = $CheckLeaseClosed -or $RejoinMode -ne ''
    if ($oneShot -or $RestoreClosureArgs -or $ObserveClosureCheck) {
        if ($pins.lease_closure_check -isnot [bool] -or -not $pins.lease_closure_check) {
            throw 'initial_service_transition_scope'
        }
    }
    if ($RejoinMode -ne '') {
        $mode = $RejoinMode
    } elseif ($CheckLeaseClosed -or $RestoreClosureArgs -or $ObserveClosureCheck) {
        $mode = 'check-lease-closed'
    }

    if ($Plan.profile -cne 'enforced-vm-v1' -or $Plan.generation -lt 2 -or
        $Plan.PSObject.Properties['initial_wsl_cutover'] -or -not $Plan.PSObject.Properties['windows_transition_observer'] -or
        $scope.service_name -cne $ServiceName -or $scope.service_image_sha256 -cne $ServiceHostSha256 -or
        $pins.preflight_sha256 -cne $PreflightSha256 -or $pins.mode -cnotin @('transition-serve','transition-rearm') -or
        $Approval.schema_version -ne 3 -or $Approval.profile -cne 'enforced-vm-v1' -or $Approval.action -cne 'ha-transition' -or
        $Approval.authority_kind -cne 'installed-runtime' -or $Approval.plan_sha256 -cne $PlanSha256 -or
        $Approval.installation_id -cne $Plan.installation_id -or $Approval.source_commit -cne $Plan.source_commit -or
        $Approval.policy_sha256 -cne $scope.policy_sha256) { throw 'initial_service_transition_scope' }
    $sid = ([Security.Principal.NTAccount]::new('NT SERVICE',$ServiceName)).
        Translate([Security.Principal.SecurityIdentifier]).Value
    if ($scope.service_sid -cne $sid) { throw 'initial_service_transition_scope' }
    Assert-Executable $scope.python_path $scope.python_sha256
    Assert-Executable $scope.launcher_path $scope.launcher_sha256
    Assert-Executable $PreflightPath $PreflightSha256
    Assert-Executable $ScmVerifierPath $pins.scm_verifier_sha256
    $hostRoot = [IO.Path]::GetDirectoryName($HostPath)
    $settings = [ordered]@{format='enforced-service-v1';service_name=$ServiceName;service_sid=$sid;
        python_path=$scope.python_path;python_sha256=$scope.python_sha256;launcher_path=$scope.launcher_path;
        launcher_sha256=$scope.launcher_sha256;plan_path=$PlanPath;plan_sha256=$PlanSha256;
        approval_path=$ApprovalPath;approval_sha256=$ApprovalSha256;mode=$mode;host_sha256=$ServiceHostSha256}
    $lines = @($settings.GetEnumerator() | ForEach-Object { $_.Key + '=' + $_.Value })
    $settings['mode'] = $pins.mode
    $servingLines = @($settings.GetEnumerator() | ForEach-Object { $_.Key + '=' + $_.Value })
    $encoder = [Text.UTF8Encoding]::new($false,$true)
    $hash = [Security.Cryptography.SHA256]::Create()
    try {
        $argsBytes = $encoder.GetBytes(($lines -join [char]10) + [char]10)
        $argsSha = [BitConverter]::ToString($hash.ComputeHash($argsBytes)).Replace('-','').ToLowerInvariant()
        $servingBytes = $encoder.GetBytes(($servingLines -join [char]10) + [char]10)
        $servingSha = [BitConverter]::ToString($hash.ComputeHash($servingBytes)).Replace('-','').ToLowerInvariant()
        if ($InspectRejoinStage) {
            $settings['mode'] = 'initialize'
            $initializeLines = @($settings.GetEnumerator() | ForEach-Object { $_.Key + '=' + $_.Value })
            $initializeBytes = $encoder.GetBytes(($initializeLines -join [char]10) + [char]10)
            $initializeSha = [BitConverter]::ToString($hash.ComputeHash($initializeBytes)).Replace('-','').ToLowerInvariant()
        }
        $preflight = [IO.File]::ReadAllBytes($PreflightPath)
        $scmSource = [IO.File]::ReadAllBytes($ScmVerifierPath)
        if ([BitConverter]::ToString($hash.ComputeHash($preflight)).Replace('-','').ToLowerInvariant() -cne $PreflightSha256 -or
            [BitConverter]::ToString($hash.ComputeHash($scmSource)).Replace('-','').ToLowerInvariant() -cne $pins.scm_verifier_sha256 -or
            [BitConverter]::ToString($hash.ComputeHash($encoder.GetBytes($Current.PathName))).Replace('-','').ToLowerInvariant() -cne
                $pins.service_image_path_sha256) { throw 'initial_service_transition_scope' }
    } finally { $hash.Dispose() }
    $preflightBlock = [ScriptBlock]::Create($encoder.GetString($preflight))
    . $preflightBlock -HostRoot $hostRoot -HostSha256 $ServiceHostSha256 -ArgsSha256 $argsSha -ServiceSid $sid -TrustedAdminSids $pins.trusted_admin_sids
    $restoreOneShot = $RestoreClosureArgs -or $RestoreRejoinArgs
    if ($PublishTransitionArgs) { Confirm-ClosureArgsState $servingSha $PreviousArgsSha256 }
    elseif ($InspectRejoinStage) {
        try { Confirm-ClosureArgsState $servingSha $argsSha }
        catch {
            if ($_.Exception.Message -cne 'preflight_hash_rejected') { throw }
            Assert-TransitionPreflight $initializeSha
        }
    } elseif ($restoreOneShot) { Confirm-ClosureArgsState $servingSha $argsSha }
    else { Assert-TransitionPreflight $servingSha }
    if ($PlanPath -cnotmatch '\AC:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}\\plan\.json\z') {
        throw 'initial_service_transition_intent_scope'
    }
    $intentRoot = Get-Item -LiteralPath ([IO.Path]::GetDirectoryName($PlanPath)) -Force
    $authors = @('S-1-5-18','S-1-5-32-544') + @($pins.trusted_admin_sids)
    Assert-PreflightEntry $intentRoot $authors ($authors + @($sid)) $true $false
    for ($parent = $intentRoot.Parent; $null -ne $parent; $parent = $parent.Parent) {
        Assert-PreflightEntry $parent $authors $authors $false $true
    }
    $intentPrefix = if ($CheckLeaseClosed -or $RestoreClosureArgs -or $ObserveClosureCheck) {'transition-closure-start-'} else {'transition-service-start-'}
    if ($RejoinMode -ne '') {
        $intentPrefix = 'transition-'+$RejoinMode+'-'
        if ($RejoinMode -cnotin @('initialize','stage-rejoin')) { $intentPrefix += $RejoinRequestId+'-' }
    }
    $intentPath = Join-Path $intentRoot.FullName ($intentPrefix + $PlanSha256 + '.json')
    $intent = [ordered]@{service_name=$ServiceName;controller_plan_sha256=$PlanSha256;approval_sha256=$ApprovalSha256;
        args_sha256=$argsSha;service_image_path_sha256=$pins.service_image_path_sha256;mode=$mode}
    $intentBytes = $encoder.GetBytes((ConvertTo-Json -InputObject $intent -Compress))
    $intentAcl = [Security.AccessControl.FileSecurity]::new()
    $intentAcl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    $intentAcl.SetAccessRuleProtection($true,$false)
    foreach ($writer in @('S-1-5-18','S-1-5-32-544')) {
        $intentAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($writer),'FullControl','Allow'))
    }
    $sections = [Security.AccessControl.AccessControlSections]'Owner,Access'
    # The host parent has already passed the pinned private ACL check.
    # Use the same lock for all transition commands, across plan generations.
    $lockPath = Join-Path ([IO.Path]::GetDirectoryName($hostRoot)) ($ServiceName + '.transition.lock')
    $commandLock = [IO.FileStream]::new($lockPath,[IO.FileMode]::CreateNew,
        [Security.AccessControl.FileSystemRights]'WriteData,Synchronize,Delete',[IO.FileShare]::None,4096,
        [IO.FileOptions]::DeleteOnClose,$intentAcl)
    try {
        if ($PublishTransitionArgs) {
            if ('TrustedStoppedV1.Native' -as [type]) { throw 'initial_service_scm_verifier_already_loaded' }
            Add-Type -TypeDefinition ($encoder.GetString($scmSource)) -ErrorAction Stop
            Assert-TransitionStopped $Plan $HostPath $Current.PathName
            Confirm-ClosureArgsState $servingSha $PreviousArgsSha256
            $target = Join-Path $hostRoot 'service.args'
            $backup = Join-Path $intentRoot.FullName ('transition-prior-args-' + $PlanSha256)
            $observed = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($observed -cne $servingSha) {
                if ($observed -cne $PreviousArgsSha256 -or (Test-Path -LiteralPath $backup)) {
                    throw 'initial_service_transition_args_reconciliation_required'
                }
                $ready = Join-Path $intentRoot.FullName ('transition-next-args-' + [Guid]::NewGuid().ToString('N'))
                Write-TransitionArgsStage $ready $servingBytes (Get-Acl -LiteralPath $target)
                [IO.File]::Replace($ready,$target,$backup,$false)
            }
            Assert-TransitionPreflight $servingSha
            if ($PreviousArgsSha256 -cne $servingSha) {
                $prior = Get-Item -LiteralPath $backup -Force
                Assert-PreflightEntry $prior $authors ($authors + @($sid)) $true $false
                if ($prior -isnot [IO.FileInfo] -or $prior.Length -gt 65536 -or
                    (Get-FileHash -LiteralPath $backup -Algorithm SHA256).Hash.ToLowerInvariant() -cne $PreviousArgsSha256) {
                    throw 'initial_service_transition_args_reconciliation_required'
                }
            }
            Assert-TransitionStopped $Plan $HostPath $Current.PathName
            [ordered]@{transition_service_prepared=$true;controller_plan_sha256=$PlanSha256;
                service_sid=$sid;serving_args_sha256=$servingSha}|ConvertTo-Json -Compress
            return
        }
        if ($VerifyPreparedTransition) {
            Assert-TransitionPreflight $servingSha
            if ('TrustedStoppedV1.Native' -as [type]) { throw 'initial_service_scm_verifier_already_loaded' }
            Add-Type -TypeDefinition ($encoder.GetString($scmSource)) -ErrorAction Stop
            Assert-TransitionStopped $Plan $HostPath $Current.PathName
            [ordered]@{transition_service_prepared=$true;controller_plan_sha256=$PlanSha256;
                service_sid=$sid;serving_args_sha256=$servingSha}|ConvertTo-Json -Compress
            return
        }
        if ($InspectRejoinStage) {
            if ('TrustedStoppedV1.Native' -as [type]) { throw 'initial_service_scm_verifier_already_loaded' }
            Add-Type -TypeDefinition ($encoder.GetString($scmSource)) -ErrorAction Stop
            Assert-TransitionStopped $Plan $HostPath $Current.PathName
            try { $entry = Get-Item -LiteralPath $intentPath -Force -ErrorAction Stop }
            catch [System.Management.Automation.ItemNotFoundException] { $entry = $null }
            if ($null -ne $entry) {
                Assert-PreflightEntry $entry $authors $authors $true $false
                if ($entry -isnot [IO.FileInfo] -or $entry.Length -ne $intentBytes.Length -or
                    (Get-Acl -LiteralPath $intentPath).GetSecurityDescriptorSddlForm($sections) -cne
                        $intentAcl.GetSecurityDescriptorSddlForm($sections) -or
                    $encoder.GetString([IO.File]::ReadAllBytes($intentPath)) -cne $encoder.GetString($intentBytes)) {
                    throw 'initial_service_transition_intent_scope'
                }
            }
            [ordered]@{stage_intent_present=($null -ne $entry);controller_plan_sha256=$PlanSha256}|ConvertTo-Json -Compress
            return
        }
        if (-not $restoreOneShot) { Assert-TransitionPreflight $servingSha }
        if ($ObserveTransition -or $StopTransition -or $restoreOneShot -or $ObserveClosureCheck) {
            $intentHash = [Security.Cryptography.SHA256]::Create()
            try { $intentSha = [BitConverter]::ToString($intentHash.ComputeHash($intentBytes)).Replace('-','').ToLowerInvariant() }
            finally { $intentHash.Dispose() }
            $null = Read-PinnedJson $intentPath $intentSha
            $entry = Get-Item -LiteralPath $intentPath -Force
            Assert-PreflightEntry $entry $authors $authors $true $false
            if ((Get-Acl -LiteralPath $intentPath).GetSecurityDescriptorSddlForm($sections) -cne
                $intentAcl.GetSecurityDescriptorSddlForm($sections)) { throw 'initial_service_transition_intent_scope' }
            if ($ObserveClosureCheck) {
                if ('TrustedStoppedV1.Native' -as [type]) { throw 'initial_service_scm_verifier_already_loaded' }
                Add-Type -TypeDefinition ($encoder.GetString($scmSource)) -ErrorAction Stop
                Assert-TransitionStopped $Plan $HostPath $Current.PathName
                $receiptPath = $intentPath + '.completed'
                $entry = Get-Item -LiteralPath $receiptPath -Force
                if ($entry -isnot [IO.FileInfo] -or $entry.Length -lt 1 -or $entry.Length -gt 1024) {
                    throw 'initial_service_closure_check_unconfirmed'
                }
                Assert-PreflightEntry $entry $authors $authors $true $false
                if ((Get-Acl -LiteralPath $receiptPath).GetSecurityDescriptorSddlForm($sections) -cne
                    $intentAcl.GetSecurityDescriptorSddlForm($sections)) { throw 'initial_service_closure_check_unconfirmed' }
                $record = ConvertFrom-Json -InputObject $encoder.GetString([IO.File]::ReadAllBytes($receiptPath))
                if ((@($record.PSObject.Properties.Name | Sort-Object -CaseSensitive) -join ',') -cne
                    'closure_check_completed,controller_plan_sha256,host_pid,service_sid,started_at_ms' -or
                    $record.closure_check_completed -isnot [bool] -or -not $record.closure_check_completed -or
                    $record.controller_plan_sha256 -cne $PlanSha256 -or $record.service_sid -cne $sid -or
                    $record.host_pid -isnot [int] -or $record.host_pid -lt 1 -or
                    $record.started_at_ms -isnot [long] -or $record.started_at_ms -lt 1) {
                    throw 'initial_service_closure_check_unconfirmed'
                }
                $record | ConvertTo-Json -Compress
                return
            }
            if ($restoreOneShot) {
                if ('TrustedStoppedV1.Native' -as [type]) { throw 'initial_service_scm_verifier_already_loaded' }
                Add-Type -TypeDefinition ($encoder.GetString($scmSource)) -ErrorAction Stop
                Assert-TransitionStopped $Plan $HostPath $Current.PathName
                Restore-TransitionServingArgs (Join-Path $hostRoot 'service.args') ($intentPath + '.serving-args') $servingSha $argsSha
                [ordered]@{serving_args_restored=$true;controller_plan_sha256=$PlanSha256}|ConvertTo-Json -Compress
                return
            }
        } else {
            if ('TrustedStoppedV1.Native' -as [type]) { throw 'initial_service_scm_verifier_already_loaded' }
            Add-Type -TypeDefinition ($encoder.GetString($scmSource)) -ErrorAction Stop
            Assert-TransitionStopped $Plan $HostPath $Current.PathName
            if (Test-Path -LiteralPath $intentPath) { throw 'initial_service_transition_reconciliation_required' }
            $file = [IO.FileStream]::new($intentPath,[IO.FileMode]::CreateNew,
                [Security.AccessControl.FileSystemRights]'WriteData,Synchronize',[IO.FileShare]::None,4096,
                [IO.FileOptions]::WriteThrough,$intentAcl)
            try { $file.Write($intentBytes,0,$intentBytes.Length); $file.Flush($true) } finally { $file.Dispose() }
            if ((Get-Acl -LiteralPath $intentPath).GetSecurityDescriptorSddlForm($sections) -cne
                $intentAcl.GetSecurityDescriptorSddlForm($sections)) { throw 'initial_service_transition_intent_scope' }
            if ($oneShot) {
                $argsPath = Join-Path $hostRoot 'service.args'
                $argsAcl = Get-Acl -LiteralPath $argsPath
                $stage = $intentPath + '.closure-args'
                $restore = $intentPath + '.serving-args'
                Write-TransitionArgsStage $stage $argsBytes $argsAcl
                Write-TransitionArgsStage $restore $servingBytes $argsAcl
                Assert-TransitionStopped $Plan $HostPath $Current.PathName
                Assert-TransitionPreflight $servingSha
                $receipt = $null
                try {
                    [IO.File]::Replace($stage,$argsPath,[NullString]::Value,$false)
                    Assert-TransitionPreflight $argsSha
                    $startedMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
                    Start-Service -Name $ServiceName -ErrorAction Stop
                    $watch = [Diagnostics.Stopwatch]::StartNew()
                    $timeoutMs = if ($RejoinMode -ceq 'stage-rejoin') { 120000 } else { 15000 }
                    $seenPid = 0
                    while ($watch.ElapsedMilliseconds -lt $timeoutMs) {
                        $sample = Get-CimInstance Win32_Service -Filter ("Name='" + $ServiceName + "'") -OperationTimeoutSec 3
                        if ($sample.Name -cne $ServiceName -or $sample.PathName -cne $Current.PathName -or
                            $sample.StartMode -cne 'Manual' -or $sample.StartName -cne $Current.StartName) {
                            throw 'initial_service_closure_check_unconfirmed'
                        }
                        if ($sample.State -ceq 'Stopped') {
                            if ($seenPid -lt 1 -or $sample.ProcessId -ne 0 -or $sample.ExitCode -ne 0 -or
                                $sample.ServiceSpecificExitCode -ne 0 -or $watch.ElapsedMilliseconds -ge $timeoutMs) {
                                throw 'initial_service_closure_check_unconfirmed'
                            }
                            $receipt = [ordered]@{host_pid=$seenPid;started_at_ms=$startedMs;service_sid=$sid;
                                controller_plan_sha256=$PlanSha256}
                            if ($CheckLeaseClosed) { $receipt['closure_check_completed'] = $true }
                            else { $receipt['rejoin_command_completed'] = $true; $receipt['mode'] = $RejoinMode }
                            break
                        }
                        if ($sample.ProcessId -gt 0) {
                            if ($seenPid -ne 0 -and $sample.ProcessId -ne $seenPid) { throw 'initial_service_closure_check_unconfirmed' }
                            $seenPid = $sample.ProcessId
                        }
                        Start-Sleep -Milliseconds 100
                    }
                    if ($null -eq $receipt) { throw 'initial_service_closure_check_unconfirmed' }
                } finally {
                    Assert-TransitionStopped $Plan $HostPath $Current.PathName
                    Restore-TransitionServingArgs $argsPath $restore $servingSha $argsSha
                    foreach ($leaf in @($stage,$restore)) {
                        if (Test-Path -LiteralPath $leaf) { Remove-Item -LiteralPath $leaf -ErrorAction Stop }
                    }
                }
                $receiptJson = ConvertTo-Json -InputObject $receipt -Compress
                Write-TransitionArgsStage ($intentPath + '.completed') ($encoder.GetBytes($receiptJson)) $intentAcl
                $receiptJson
                return
            }
            Start-Service -Name $ServiceName -ErrorAction Stop
        }
        $running = Get-CimInstance Win32_Service -Filter ("Name='" + $ServiceName + "'") -OperationTimeoutSec 5
        if ($running.State -cne 'Running' -or $running.Name -cne $ServiceName -or $running.ProcessId -lt 1 -or
            $running.PathName -cne $Current.PathName -or
            (($ObserveTransition -or $StopTransition) -and $running.ProcessId -ne $Current.ProcessId)) { throw 'initial_service_transition_start_unconfirmed' }
        $process = Get-Process -Id $running.ProcessId -ErrorAction Stop
        try {
            if ($process.HasExited -or $process.SessionId -ne 0 -or $process.MainModule.FileName -ine $HostPath) {
                throw 'initial_service_transition_start_unconfirmed'
            }
            $created = $process.StartTime.ToUniversalTime().Ticks.ToString([Globalization.CultureInfo]::InvariantCulture)
            $confirmed = Get-CimInstance Win32_Service -Filter ("Name='" + $ServiceName + "'") -OperationTimeoutSec 5
            if ($confirmed.State -cne 'Running' -or $confirmed.Name -cne $ServiceName -or
                $confirmed.ProcessId -ne $running.ProcessId -or $confirmed.PathName -cne $running.PathName -or
                $process.HasExited) { throw 'initial_service_transition_start_unconfirmed' }
            if ($StopTransition) {
                if ([string]$confirmed.ProcessId -cne $ExpectedServicePid -or $created -cne $ExpectedServiceCreatedTicks) {
                    throw 'initial_service_transition_stop_unconfirmed'
                }
                if ($confirmed.StartMode -cne 'Manual') { throw 'initial_service_transition_stop_unconfirmed' }
                Add-Type -TypeDefinition @'
    using System;
    using System.Runtime.InteropServices;
    using Microsoft.Win32.SafeHandles;
    public static class HaTransitionProcessStop {
        [DllImport("kernel32.dll", SetLastError=true)]
        static extern SafeWaitHandle OpenProcess(uint access, bool inherit, uint pid);
        [DllImport("kernel32.dll", SetLastError=true)]
        static extern bool GetProcessTimes(SafeWaitHandle process, out long born, out long exited, out long kernel, out long user);
        [DllImport("kernel32.dll", SetLastError=true)]
        static extern bool TerminateProcess(SafeWaitHandle process, uint code);
        [DllImport("kernel32.dll", SetLastError=true)]
        static extern uint WaitForSingleObject(SafeWaitHandle process, uint milliseconds);
        public static bool Stop(uint pid, long expectedTicks) {
            // SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE.
            using (var process = OpenProcess(0x101001, false, pid)) {
                long born, exited, kernel, user;
                if (process.IsInvalid || !GetProcessTimes(process, out born, out exited, out kernel, out user)
                    || DateTime.FromFileTimeUtc(born).Ticks != expectedTicks) return false;
                if (WaitForSingleObject(process, 0) != 258) return false;
                return TerminateProcess(process, 1) && WaitForSingleObject(process, 8000) == 0;
            }
        }
    }
'@ -ErrorAction Stop
                if (-not [HaTransitionProcessStop]::Stop([uint32]$ExpectedServicePid,[long]$ExpectedServiceCreatedTicks)) {
                    throw 'initial_service_transition_stop_unconfirmed'
                }
                $stopped = Get-CimInstance Win32_Service -Filter ("Name='" + $ServiceName + "'") -OperationTimeoutSec 5
                if ($stopped.Name -cne $ServiceName -or $stopped.State -cne 'Stopped' -or $stopped.ProcessId -ne 0 -or
                    $stopped.StartMode -cne 'Manual' -or $stopped.PathName -cne $Current.PathName) {
                    throw 'initial_service_transition_stop_unconfirmed'
                }
                [ordered]@{transition_service_stopped=$true;controller_plan_sha256=$PlanSha256} | ConvertTo-Json -Compress
                return
            }
            $statePath = Join-Path $intentRoot.FullName 'journal\controller-state.json'
            $command = '"{0}" -I -B "{1}" {2} --plan "{3}" --approval "{4}" --journal "{5}"' -f
                $scope.python_path,$scope.launcher_path,$pins.mode,$PlanPath,$ApprovalPath,$statePath
            $commandHasher = [Security.Cryptography.SHA256]::Create()
            try { $commandSha = [BitConverter]::ToString($commandHasher.ComputeHash($encoder.GetBytes($command))).Replace('-','').ToLowerInvariant() }
            finally { $commandHasher.Dispose() }
            $result = [ordered]@{service_pid=$running.ProcessId;controller_plan_sha256=$PlanSha256;
                service_created_ticks=$created;service_image_path_sha256=$pins.service_image_path_sha256;python_command_sha256=$commandSha}
            $result[$(if ($ObserveTransition) {'transition_service_observed'} else {'transition_service_started'})] = $true
            $result | ConvertTo-Json -Compress
        } finally { $process.Dispose() }
    } finally { $commandLock.Dispose() }
}

try {
    $rejoin = $RejoinMode -ne ''
    $transition = $rejoin -or $PublishTransitionArgs -or $VerifyPreparedTransition -or $StartTransition -or $ObserveTransition -or $StopTransition -or $CheckLeaseClosed -or $RestoreClosureArgs -or $ObserveClosureCheck
    $modes = @(@($PublishTransitionArgs,$VerifyPreparedTransition,$StartTransition,$ObserveTransition,$StopTransition,$CheckLeaseClosed,$RestoreClosureArgs,$ObserveClosureCheck) | Where-Object { $_.IsPresent }).Count
    if ($rejoin) { $modes++ }
    $recovering=[bool]$RecoveryAttemptId -or [bool]$RecoveryReservationSha256
    if (-not $Go -or $modes -gt 1 -or
        ($recovering -and ($transition -or $RecoveryAttemptId -cnotmatch $hashPattern -or
                           $RecoveryReservationSha256 -cnotmatch $hashPattern)) -or
        ($PublishTransitionArgs -and $PreviousArgsSha256 -cnotmatch $hashPattern) -or
        (-not $PublishTransitionArgs -and $PreviousArgsSha256 -cne '') -or
        ($rejoin -and ($RejoinMode -cnotin @('initialize','stage-rejoin','observe-rejoin-stage','check-rejoin') -or
            $RejoinRequestId -cnotmatch '\A[a-f0-9]{32}\z')) -or
        ($InspectRejoinStage -and ($RejoinMode -cne 'stage-rejoin' -or $RestoreRejoinArgs)) -or
        (-not $rejoin -and ($RestoreRejoinArgs -or $RejoinRequestId -cne '')) -or
        ($StopTransition -and ($ExpectedServicePid -cnotmatch '\A[1-9][0-9]{0,9}\z' -or
            $ExpectedServiceCreatedTicks -cnotmatch '\A[1-9][0-9]{1,19}\z')) -or
        (-not $StopTransition -and ($ExpectedServicePid -cne '' -or $ExpectedServiceCreatedTicks -cne '')) -or $PSVersionTable.PSEdition -cne 'Desktop' -or
        $ServiceName -cnotmatch '\AN100HA_[a-f0-9]{24}_controller\z' -or
        $ServiceHostSha256 -cnotmatch $hashPattern -or $PlanSha256 -cnotmatch $hashPattern -or
        $ApprovalSha256 -cnotmatch $hashPattern -or
        ($transition -and ($ExpectedActivationIntentSha256 -cne '' -or $BindingPath -cne '')) -or
        (-not $transition -and $ExpectedActivationIntentSha256 -cnotmatch $hashPattern)) {
        throw 'initial_service_scope'
    }
    $current = Get-CimInstance Win32_Service -Filter ("Name='" + $ServiceName + "'")
    if ($null -eq $current -or $current.Name -cne $ServiceName -or
        (($ObserveTransition -or $StopTransition) -and ($current.State -cne 'Running' -or $current.ProcessId -lt 1)) -or
        (-not ($ObserveTransition -or $StopTransition) -and ($current.State -cne 'Stopped' -or $current.ProcessId -ne 0)) -or
        $current.StartMode -cne 'Manual' -or $current.ServiceType -cne 'Own Process' -or
        $current.DesktopInteract -or
        $current.StartName -cne ('NT SERVICE\' + $ServiceName) -or $current.PathName -cnotmatch '\A"[^"\r\n]+"\z') {
        throw 'initial_service_baseline_changed'
    }
    $hostPath = $current.PathName.Substring(1,$current.PathName.Length - 2)
    Assert-Executable $hostPath $ServiceHostSha256
    $plan = Read-PinnedJson $PlanPath $PlanSha256
    $approval = Read-PinnedJson $ApprovalPath $ApprovalSha256
    if ($transition) {
        Start-PreparedTransitionService $current $hostPath $plan $approval
        return
    }
    $scope = $plan.windows_enforced
    $startScope = $plan.initial_service_start
    $action = if($recovering){'ha-recover-initial-writer'}else{'ha-initial-wsl-cutover'}
    if ($recovering) {
        Assert-RecoveryStartScope $plan $approval $RecoveryAttemptId $RecoveryReservationSha256 $ServiceName $hostPath (
            [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData))
    } elseif ('initial_cutover_recovery' -cin $plan.PSObject.Properties.Name) {
        throw 'initial_service_recovery_scope'
    }
    if ($plan.initial_cutover_handoff.activation_intent_sha256 -cne $ExpectedActivationIntentSha256) {
        throw 'initial_service_intent_unbound'
    }
    $imageHasher = [Security.Cryptography.SHA256]::Create()
    try {
        $configuredImageHash = [BitConverter]::ToString($imageHasher.ComputeHash(
            [Text.Encoding]::UTF8.GetBytes($current.PathName))).Replace('-','').ToLowerInvariant()
    } finally { $imageHasher.Dispose() }
    $sid = [Security.Principal.NTAccount]::new('NT SERVICE',$ServiceName).Translate([Security.Principal.SecurityIdentifier])
    if ($scope.service_name -cne $ServiceName -or $scope.service_sid -cne $sid.Value -or
        $scope.service_image_sha256 -cne $ServiceHostSha256 -or $startScope.preflight_sha256 -cne $PreflightSha256 -or
        $startScope.service_image_path_sha256 -cne $configuredImageHash -or $startScope.binding_path -cne $BindingPath -or
        $plan.initial_wsl_cutover.primary_node -cne 'A' -or $plan.initial_wsl_cutover.partner_node -cne 'B' -or
        $approval.action -cne $action -or $approval.plan_sha256 -cne $PlanSha256 -or
        $approval.installation_id -cne $plan.installation_id -or $approval.source_commit -cne $plan.source_commit) {
        throw 'initial_service_plan_scope'
    }
    Assert-Executable $scope.python_path $scope.python_sha256
    Assert-Executable $scope.launcher_path $scope.launcher_sha256
    Assert-Executable $PreflightPath $PreflightSha256
    Assert-Executable $ScmVerifierPath $startScope.scm_verifier_sha256
    $directory = [IO.Path]::GetDirectoryName($hostPath)
    $argsPath = Join-Path $directory 'service.args'
    $statePath = Join-Path ([IO.Path]::GetDirectoryName($PlanPath)) 'journal\controller-state.json'
    $attempt = $plan.initial_cutover_handoff.attempt_id
    $bindingName=if($recovering){'initial-recovery-service-'+$RecoveryAttemptId+'.json'}
        else{'initial-service-'+$attempt+'.json'}
    if ($attempt -cnotmatch $hashPattern -or
        $BindingPath -cnotmatch ('\AC:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}\\'+[regex]::Escape($bindingName)+'\z') -or
        (Test-Path -LiteralPath $BindingPath)) { throw 'initial_service_binding_scope' }
    if (Test-Path -LiteralPath $argsPath) { throw 'initial_service_args_exist' }
    if ((Test-Path -LiteralPath $statePath) -or -not (Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($statePath)) -PathType Container)) {
        throw 'initial_service_journal_not_fresh'
    }
    $settings = [ordered]@{format='enforced-service-v1';service_name=$ServiceName;service_sid=$sid.Value;
        python_path=$scope.python_path;python_sha256=$scope.python_sha256;launcher_path=$scope.launcher_path;
        launcher_sha256=$scope.launcher_sha256;plan_path=$PlanPath;plan_sha256=$PlanSha256;
        approval_path=$ApprovalPath;approval_sha256=$ApprovalSha256;mode='serve';host_sha256=$ServiceHostSha256}
    $lines = @($settings.GetEnumerator() | ForEach-Object { $_.Key + '=' + $_.Value })
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($lines -join "`n") + "`n")
    $security = [Security.AccessControl.FileSecurity]::new()
    $admins = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $security.SetOwner($admins)
    $security.SetAccessRuleProtection($true,$false)
    foreach ($writer in @($admins,$system)) {
        $security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($writer,'FullControl','Allow'))
    }
    $security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'Read','Allow'))
    if ($recovering) {
        Assert-RecoveryPredecessorStopped $plan ([IO.Path]::GetDirectoryName($PlanPath))
    }
    $file = [IO.FileStream]::new($argsPath,[IO.FileMode]::CreateNew,
        [Security.AccessControl.FileSystemRights]'WriteData,Synchronize',[IO.FileShare]::None,4096,
        [IO.FileOptions]::WriteThrough,$security)
    try { $file.Write($bytes,0,$bytes.Length); $file.Flush($true) } finally { $file.Dispose() }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $argsHash = [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-','').ToLowerInvariant()
        $preflight = [IO.File]::ReadAllBytes($PreflightPath)
        $scmSource = [IO.File]::ReadAllBytes($ScmVerifierPath)
        if ([BitConverter]::ToString($sha.ComputeHash($preflight)).Replace('-','').ToLowerInvariant() -cne $PreflightSha256) {
            throw 'initial_service_preflight_pin'
        }
        if ([BitConverter]::ToString($sha.ComputeHash($scmSource)).Replace('-','').ToLowerInvariant() -cne $startScope.scm_verifier_sha256) {
            throw 'initial_service_scm_verifier_pin'
        }
    } finally { $sha.Dispose() }
    $preflightBlock = [ScriptBlock]::Create([Text.UTF8Encoding]::new($false,$true).GetString($preflight))
    . $preflightBlock -HostRoot $directory -HostSha256 $ServiceHostSha256 `
        -ArgsSha256 $argsHash -ServiceSid $sid.Value -TrustedAdminSids $startScope.trusted_admin_sids
    $checked = ConvertFrom-Json -InputObject (Test-EnforcedServicePreflight)
    if ($checked.scope -cne 'pre_StartService_inventory_and_ACL' -or $checked.host_sha256 -cne $ServiceHostSha256 -or
        $checked.args_sha256 -cne $argsHash -or $checked.execution_performed -isnot [bool] -or $checked.execution_performed) {
        throw 'initial_service_preflight_unconfirmed'
    }
    $writers = @('S-1-5-18','S-1-5-32-544') + @($startScope.trusted_admin_sids)
    $bindingParent = Get-Item -LiteralPath ([IO.Path]::GetDirectoryName($BindingPath)) -Force
    if ($bindingParent -isnot [IO.DirectoryInfo]) { throw 'initial_service_binding_scope' }
    Assert-PreflightEntry $bindingParent $writers ($writers + @($sid.Value)) $true $false
    for ($parent = $bindingParent.Parent; $null -ne $parent; $parent = $parent.Parent) {
        Assert-PreflightEntry $parent $writers $writers $false $true
    }
    if ('TrustedStoppedV1.Native' -as [type]) { throw 'initial_service_scm_verifier_already_loaded' }
    Add-Type -TypeDefinition ([Text.UTF8Encoding]::new($false,$true).GetString($scmSource)) -ErrorAction Stop
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class HaInitialLeaseFileTime {
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool GetFileTime(SafeFileHandle file, IntPtr creation, IntPtr access, out long written);
    public static DateTime Written(SafeFileHandle file) {
        long written;
        if (!GetFileTime(file, IntPtr.Zero, IntPtr.Zero, out written))
            throw new InvalidOperationException("initial_service_lease_file_time");
        return DateTime.FromFileTimeUtc(written);
    }
}
'@ -ErrorAction Stop
    $scm = [TrustedStoppedV1.Native]::new()
    try {
        $scm.OpenForRead($ServiceName)
        $identityScope=if($recovering){$RecoveryAttemptId}else{$plan.installation_id}
        [TrustedStoppedV1.Contract]::Inputs($identityScope,$ServiceName,$hostPath)
        [TrustedStoppedV1.Contract]::Verify($scm.Read($ServiceName),$ServiceName,$current.PathName)
    } finally { $scm.Dispose() }
    if ($recovering) {
        Assert-RecoveryPredecessorStopped $plan ([IO.Path]::GetDirectoryName($PlanPath))
    }
    # Once invoked, a missing response is an uncertain activation. Preserve all records.
    Start-Service -Name $ServiceName -ErrorAction Stop
    $wait = [Diagnostics.Stopwatch]::StartNew()
    while ($wait.ElapsedMilliseconds -lt 25000) {
        $native = Get-CimInstance Win32_Service -Filter ("Name='" + $ServiceName + "'") -OperationTimeoutSec 5
        if ($native.State -ceq 'Stopped') { throw 'initial_service_lease_unconfirmed' }
        if ($native.State -ceq 'Running' -and $native.ProcessId -gt 0 -and [IO.File]::Exists($statePath)) {
            $stream = [IO.File]::Open($statePath,[IO.FileMode]::Open,[IO.FileAccess]::Read,
                ([IO.FileShare]::Read -bor [IO.FileShare]::Delete))
            try {
                $written = [HaInitialLeaseFileTime]::Written($stream.SafeFileHandle)
                if ($stream.Length -lt 1 -or $stream.Length -gt 8192) { throw 'initial_service_lease_unconfirmed' }
                $reader = [IO.StreamReader]::new($stream,[Text.UTF8Encoding]::new($false,$true),$false,1024,$true)
                try { $lease = ConvertFrom-Json -InputObject $reader.ReadToEnd() }
                finally { $reader.Dispose() }
            } finally { $stream.Dispose() }
            if ($lease.phase -ceq 'serving') {
                $age = ([DateTime]::UtcNow - $written).TotalMilliseconds
                $kind=if($recovering){'initial-wsl-cutover-recovery'}else{'initial-wsl-cutover'}
                if ($lease.kind -cne $kind -or $lease.schema_version -ne 1 -or $age -lt 0 -or $age -gt 2900 -or
                    $lease.attempt_id -cne $attempt -or $lease.generation -ne 1 -or $lease.sequence -lt 1 -or
                    $lease.controller_plan_sha256 -cne $PlanSha256 -or $lease.first_write_proof_sha256 -cnotmatch $hashPattern -or
                    $lease.activation_intent_sha256 -cne $plan.initial_cutover_handoff.activation_intent_sha256 -or
                    $lease.restore_receipt_sha256 -cne $plan.initial_cutover_handoff.restore_receipt_sha256 -or
                    $lease.legacy_plan_sha256 -cne $plan.initial_cutover_handoff.legacy_plan_sha256 -or
                    $lease.proof_elapsed_ms -lt 0 -or $lease.proof_elapsed_ms -ge 4000 -or ($age + $lease.proof_elapsed_ms) -ge 4800 -or
                    $native.PathName -cne $current.PathName) { throw 'initial_service_lease_unconfirmed' }
                if($recovering){
                    foreach($field in @('schema_version','generation','sequence','revision')){
                        if($lease.$field -isnot [int] -and $lease.$field -isnot [long]){
                            throw 'initial_service_lease_unconfirmed'
                        }
                    }
                    $recoveryFields=@($lease.recovery.PSObject.Properties.Name|Sort-Object -CaseSensitive)
                    if($lease.revision -ne 4 -or $lease.lease_proof_sha256 -isnot [string] -or
                        $lease.lease_proof_sha256 -cnotmatch $hashPattern -or
                        ($lease.proof_elapsed_ms -isnot [int] -and $lease.proof_elapsed_ms -isnot [long] -and
                         $lease.proof_elapsed_ms -isnot [double]) -or [double]::IsNaN([double]$lease.proof_elapsed_ms) -or
                        ($recoveryFields -join ',') -cne 'attempt_id,predecessor_service_sha256,predecessor_terminal_sha256' -or
                        $lease.recovery.attempt_id -cne $RecoveryAttemptId -or
                        $lease.recovery.predecessor_service_sha256 -cne $plan.initial_cutover_recovery.predecessor_service_sha256 -or
                        $lease.recovery.predecessor_terminal_sha256 -cne $plan.initial_cutover_recovery.predecessor_terminal_sha256){
                        throw 'initial_service_lease_unconfirmed'
                    }
                }
                $process = Get-Process -Id $native.ProcessId -ErrorAction Stop
                try {
                    if ($process.HasExited -or $process.SessionId -ne 0 -or $process.MainModule.FileName -ine $hostPath) {
                        throw 'initial_service_lease_unconfirmed'
                    }
                    $sha = [Security.Cryptography.SHA256]::Create()
                    try {
                        $imageBytes = [Text.Encoding]::UTF8.GetBytes($native.PathName)
                        $imageHash = [BitConverter]::ToString($sha.ComputeHash($imageBytes)).Replace('-','').ToLowerInvariant()
                        $binding = [ordered]@{attempt_id=$attempt;controller_plan_sha256=$PlanSha256;
                            first_write_proof_sha256=$lease.first_write_proof_sha256;legacy_plan_sha256=$lease.legacy_plan_sha256;
                            service_created_ticks=$process.StartTime.ToUniversalTime().Ticks.ToString();service_image_path_sha256=$imageHash;
                            service_name=$ServiceName;service_pid=$native.ProcessId;state_path=$statePath}
                        $bindingBytes = [Text.UTF8Encoding]::new($false).GetBytes((ConvertTo-Json -InputObject $binding -Compress))
                        $instance = [BitConverter]::ToString($sha.ComputeHash($bindingBytes)).Replace('-','').ToLowerInvariant()
                    } finally { $sha.Dispose() }
                    $confirmed = Get-CimInstance Win32_Service -Filter ("Name='" + $ServiceName + "'") -OperationTimeoutSec 5
                    $confirmedAge = ([DateTime]::UtcNow - $written).TotalMilliseconds
                    if ($confirmed.State -cne 'Running' -or $confirmed.ProcessId -ne $native.ProcessId -or
                        $confirmed.PathName -cne $current.PathName -or $process.HasExited -or
                        $confirmedAge -lt 0 -or $confirmedAge -gt 2900 -or
                        ($confirmedAge + $lease.proof_elapsed_ms) -ge 4800) { throw 'initial_service_lease_unconfirmed' }
                    $output = [IO.FileStream]::new($BindingPath,[IO.FileMode]::CreateNew,
                        [Security.AccessControl.FileSystemRights]'WriteData,Synchronize',[IO.FileShare]::None,4096,
                        [IO.FileOptions]::WriteThrough,$security)
                    try { $output.Write($bindingBytes,0,$bindingBytes.Length); $output.Flush($true) }
                    finally { $output.Dispose() }
                    [ordered]@{first_write_proof_sha256=$lease.first_write_proof_sha256;
                        service_instance_sha256=$instance}|ConvertTo-Json -Compress
                    return
                } finally { $process.Dispose() }
            }
        }
        Start-Sleep -Milliseconds 200
    }
    throw 'initial_service_lease_unconfirmed'
} catch {
    $reason = $_.Exception.Message
    if ($reason -cnotmatch '\Ainitial_service_[a-z0-9_]{1,80}\z') { $reason = 'initial_service_native_error' }
    [ordered]@{blocked=$reason} | ConvertTo-Json -Compress
}
