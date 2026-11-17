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
    [switch]$StartTransition,
    [switch]$ObserveTransition,
    [switch]$Go
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$hashPattern = '\A[a-f0-9]{64}\z'

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

function Start-PreparedTransitionService($Current, [string]$HostPath, $Plan, $Approval) {
    $scope = $Plan.windows_enforced
    $pins = $Plan.transition_service_start
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
        approval_path=$ApprovalPath;approval_sha256=$ApprovalSha256;mode=$pins.mode;host_sha256=$ServiceHostSha256}
    $lines = @($settings.GetEnumerator() | ForEach-Object { $_.Key + '=' + $_.Value })
    $encoder = [Text.UTF8Encoding]::new($false,$true)
    $hash = [Security.Cryptography.SHA256]::Create()
    try {
        $argsBytes = $encoder.GetBytes(($lines -join [char]10) + [char]10)
        $argsSha = [BitConverter]::ToString($hash.ComputeHash($argsBytes)).Replace('-','').ToLowerInvariant()
        $preflight = [IO.File]::ReadAllBytes($PreflightPath)
        $scmSource = [IO.File]::ReadAllBytes($ScmVerifierPath)
        if ([BitConverter]::ToString($hash.ComputeHash($preflight)).Replace('-','').ToLowerInvariant() -cne $PreflightSha256 -or
            [BitConverter]::ToString($hash.ComputeHash($scmSource)).Replace('-','').ToLowerInvariant() -cne $pins.scm_verifier_sha256 -or
            [BitConverter]::ToString($hash.ComputeHash($encoder.GetBytes($Current.PathName))).Replace('-','').ToLowerInvariant() -cne
                $pins.service_image_path_sha256) { throw 'initial_service_transition_scope' }
    } finally { $hash.Dispose() }
    $preflightBlock = [ScriptBlock]::Create($encoder.GetString($preflight))
    . $preflightBlock -HostRoot $hostRoot -HostSha256 $ServiceHostSha256 -ArgsSha256 $argsSha -ServiceSid $sid -TrustedAdminSids $pins.trusted_admin_sids
    $checked = ConvertFrom-Json -InputObject (Test-EnforcedServicePreflight)
    if ($checked.scope -cne 'pre_StartService_inventory_and_ACL' -or $checked.host_sha256 -cne $ServiceHostSha256 -or
        $checked.args_sha256 -cne $argsSha -or $checked.execution_performed -isnot [bool] -or $checked.execution_performed) {
        throw 'initial_service_preflight_unconfirmed'
    }
    if ($PlanPath -cnotmatch '\AC:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}\\plan\.json\z') {
        throw 'initial_service_transition_intent_scope'
    }
    $intentRoot = Get-Item -LiteralPath ([IO.Path]::GetDirectoryName($PlanPath)) -Force
    $authors = @('S-1-5-18','S-1-5-32-544') + @($pins.trusted_admin_sids)
    Assert-PreflightEntry $intentRoot $authors ($authors + @($sid)) $true $false
    for ($parent = $intentRoot.Parent; $null -ne $parent; $parent = $parent.Parent) {
        Assert-PreflightEntry $parent $authors $authors $false $true
    }
    $intentPath = Join-Path $intentRoot.FullName ('transition-service-start-' + $PlanSha256 + '.json')
    $intent = [ordered]@{service_name=$ServiceName;controller_plan_sha256=$PlanSha256;approval_sha256=$ApprovalSha256;
        args_sha256=$argsSha;service_image_path_sha256=$pins.service_image_path_sha256;mode=$pins.mode}
    $intentBytes = $encoder.GetBytes((ConvertTo-Json -InputObject $intent -Compress))
    $intentAcl = [Security.AccessControl.FileSecurity]::new()
    $intentAcl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    $intentAcl.SetAccessRuleProtection($true,$false)
    foreach ($writer in @('S-1-5-18','S-1-5-32-544')) {
        $intentAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($writer),'FullControl','Allow'))
    }
    $sections = [Security.AccessControl.AccessControlSections]'Owner,Access'
    if ($ObserveTransition) {
        $intentHash = [Security.Cryptography.SHA256]::Create()
        try { $intentSha = [BitConverter]::ToString($intentHash.ComputeHash($intentBytes)).Replace('-','').ToLowerInvariant() }
        finally { $intentHash.Dispose() }
        $null = Read-PinnedJson $intentPath $intentSha
        $entry = Get-Item -LiteralPath $intentPath -Force
        Assert-PreflightEntry $entry $authors $authors $true $false
        if ((Get-Acl -LiteralPath $intentPath).GetSecurityDescriptorSddlForm($sections) -cne
            $intentAcl.GetSecurityDescriptorSddlForm($sections)) { throw 'initial_service_transition_intent_scope' }
    } else {
        if ('TrustedStoppedV1.Native' -as [type]) { throw 'initial_service_scm_verifier_already_loaded' }
        Add-Type -TypeDefinition ($encoder.GetString($scmSource)) -ErrorAction Stop
        $scm = [TrustedStoppedV1.Native]::new()
        try {
            $scm.OpenForRead($ServiceName)
            [TrustedStoppedV1.Contract]::Inputs($Plan.installation_id,$ServiceName,$HostPath)
            [TrustedStoppedV1.Contract]::Verify($scm.Read($ServiceName),$ServiceName,$Current.PathName)
        } finally { $scm.Dispose() }
        if (Test-Path -LiteralPath $intentPath) { throw 'initial_service_transition_reconciliation_required' }
        $file = [IO.FileStream]::new($intentPath,[IO.FileMode]::CreateNew,
            [Security.AccessControl.FileSystemRights]'WriteData,Synchronize',[IO.FileShare]::None,4096,
            [IO.FileOptions]::WriteThrough,$intentAcl)
        try { $file.Write($intentBytes,0,$intentBytes.Length); $file.Flush($true) } finally { $file.Dispose() }
        if ((Get-Acl -LiteralPath $intentPath).GetSecurityDescriptorSddlForm($sections) -cne
            $intentAcl.GetSecurityDescriptorSddlForm($sections)) { throw 'initial_service_transition_intent_scope' }
        Start-Service -Name $ServiceName -ErrorAction Stop
    }
    $running = Get-CimInstance Win32_Service -Filter ("Name='" + $ServiceName + "'") -OperationTimeoutSec 5
    if ($running.State -cne 'Running' -or $running.Name -cne $ServiceName -or $running.ProcessId -lt 1 -or
        $running.PathName -cne $Current.PathName -or
        ($ObserveTransition -and $running.ProcessId -ne $Current.ProcessId)) { throw 'initial_service_transition_start_unconfirmed' }
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
        $result = [ordered]@{service_pid=$running.ProcessId;controller_plan_sha256=$PlanSha256;
            service_created_ticks=$created;service_image_path_sha256=$pins.service_image_path_sha256}
        $result[$(if ($ObserveTransition) {'transition_service_observed'} else {'transition_service_started'})] = $true
        $result | ConvertTo-Json -Compress
    } finally { $process.Dispose() }
}

try {
    $transition = $StartTransition -or $ObserveTransition
    if (-not $Go -or ($StartTransition -and $ObserveTransition) -or $PSVersionTable.PSEdition -cne 'Desktop' -or
        $ServiceName -cnotmatch '\AN100HA_[a-f0-9]{24}_controller\z' -or
        $ServiceHostSha256 -cnotmatch $hashPattern -or $PlanSha256 -cnotmatch $hashPattern -or
        $ApprovalSha256 -cnotmatch $hashPattern -or
        ($transition -and ($ExpectedActivationIntentSha256 -cne '' -or $BindingPath -cne '')) -or
        (-not $transition -and $ExpectedActivationIntentSha256 -cnotmatch $hashPattern)) {
        throw 'initial_service_scope'
    }
    $current = Get-CimInstance Win32_Service -Filter ("Name='" + $ServiceName + "'")
    if ($null -eq $current -or $current.Name -cne $ServiceName -or
        ($ObserveTransition -and ($current.State -cne 'Running' -or $current.ProcessId -lt 1)) -or
        (-not $ObserveTransition -and ($current.State -cne 'Stopped' -or $current.ProcessId -ne 0)) -or
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
        $approval.action -cne 'ha-initial-wsl-cutover' -or $approval.plan_sha256 -cne $PlanSha256 -or
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
    if ($attempt -cnotmatch $hashPattern -or
        $BindingPath -cnotmatch '\AC:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}\\initial-service-[a-f0-9]{64}\.json\z' -or
        -not $BindingPath.EndsWith(('-' + $attempt + '.json'),[StringComparison]::Ordinal) -or
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
        [TrustedStoppedV1.Contract]::Inputs($plan.installation_id,$ServiceName,$hostPath)
        [TrustedStoppedV1.Contract]::Verify($scm.Read($ServiceName),$ServiceName,$current.PathName)
    } finally { $scm.Dispose() }
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
                if ($lease.kind -cne 'initial-wsl-cutover' -or $lease.schema_version -ne 1 -or $age -lt 0 -or $age -gt 2900 -or
                    $lease.attempt_id -cne $attempt -or $lease.generation -ne 1 -or $lease.sequence -lt 1 -or
                    $lease.controller_plan_sha256 -cne $PlanSha256 -or $lease.first_write_proof_sha256 -cnotmatch $hashPattern -or
                    $lease.activation_intent_sha256 -cne $plan.initial_cutover_handoff.activation_intent_sha256 -or
                    $lease.restore_receipt_sha256 -cne $plan.initial_cutover_handoff.restore_receipt_sha256 -or
                    $lease.legacy_plan_sha256 -cne $plan.initial_cutover_handoff.legacy_plan_sha256 -or
                    $lease.proof_elapsed_ms -lt 0 -or $lease.proof_elapsed_ms -ge 4000 -or ($age + $lease.proof_elapsed_ms) -ge 4800 -or
                    $native.PathName -cne $current.PathName) { throw 'initial_service_lease_unconfirmed' }
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
