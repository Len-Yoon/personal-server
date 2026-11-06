param([Parameter(Mandatory=$true)][string]$Plan,
      [Parameter(Mandatory=$true)][string]$Approval, [switch]$Go)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$locks = @()

function Read-PrivateBytes([string]$Path) {
    if (-not [IO.Path]::IsPathRooted($Path)) { throw "private_configuration_required" }
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "private_configuration_required" }
    $parent = $item.Directory
    while ($null -ne $parent) {
        if ($parent.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "private_configuration_required" }
        $parent = $parent.Parent
    }
    $allowed = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, "S-1-5-18", "S-1-5-32-544")
    $acl = Get-Acl -LiteralPath $Path
    $ownerSid = (New-Object Security.Principal.NTAccount($acl.Owner)).Translate([Security.Principal.SecurityIdentifier]).Value
    if ($ownerSid -notin $allowed) { throw "private_owner_required" }
    foreach ($rule in $acl.Access) {
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($rule.AccessControlType -eq "Allow" -and $sid -notin $allowed) { throw "private_acl_required" }
    }
    $parent = $item.Directory
    while ($null -ne $parent) {
        foreach ($rule in (Get-Acl -LiteralPath $parent.FullName).Access) {
            $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
            $write = [Security.AccessControl.FileSystemRights]::Write -bor [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor [Security.AccessControl.FileSystemRights]::Delete
            if ($rule.AccessControlType -eq "Allow" -and $sid -notin $allowed -and ($rule.FileSystemRights -band $write)) { throw "private_parent_acl_required" }
        }
        $parent = $parent.Parent
    }
    $links = @(& (Join-Path $env:SystemRoot "System32\fsutil.exe") hardlink list $Path 2>$null)
    if ($LASTEXITCODE -ne 0 -or $links.Count -ne 1) { throw "single_configuration_link_required" }
    $handle = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        $memory = New-Object IO.MemoryStream
        $handle.CopyTo($memory)
        $bytes = $memory.ToArray()
        if ($bytes.Length -gt 1048576) { throw "configuration_size_limit" }
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $hash = [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace("-", "").ToLowerInvariant() } finally { $sha.Dispose() }
        return @{bytes=$bytes;verified_sha256=$hash}
    } finally { $handle.Dispose() }
}

function Read-PrivateJson([string]$Path) {
    $verified = Read-PrivateBytes $Path
    $parsed = [Text.Encoding]::UTF8.GetString($verified.bytes).TrimStart([char]0xFEFF) | ConvertFrom-Json
    $parsed | Add-Member -NotePropertyName verified_sha256 -NotePropertyValue $verified.verified_sha256 -Force
    return $parsed
}

function Assert-Quiescent {
    $owner = Read-PrivateJson (Join-Path (Join-Path $script:config.legacy_root "data") "ha-controller-owner.json")
    $bootstrap = Join-Path $script:config.legacy_root "scripts\windows-bootstrap.ps1"
    if ($owner.cluster -ne $script:config.cluster -or $owner.generation -ne $script:config.generation -or
        $owner.source_commit -ne $script:config.source_commit -or
        (Get-FileHash -LiteralPath $bootstrap -Algorithm SHA256).Hash.ToLowerInvariant() -ne $script:config.bootstrap_sha256) { throw "legacy_ownership_unverified" }
    $boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
    if ($boot.ToUniversalTime().Ticks.ToString() -ne [string]$script:config.boot_ticks) { throw "host_boot_changed" }
    # Conservatively deny any shutdown request since this boot, even if an
    # administrator might have cancelled it. Never cancel/reboot the host here.
    try {
        $events = @(Get-WinEvent -FilterHashtable @{LogName="System";ProviderName="User32";Id=1074;StartTime=$boot} -ErrorAction Stop)
    } catch {
        if ($_.FullyQualifiedErrorId -notlike "NoMatchingEventsFound*") { throw "shutdown_state_unverified" }
        $events = @()
    }
    if ($events.Count -gt 0) { throw "shutdown_state_unverified" }
    foreach ($task in Get-ScheduledTask) {
        $legacy = $task.TaskName -in @("personal-server-autostart", "PersonalServer-WSL-KeepAlive", "PersonalServer-EmergencyReboot")
        foreach ($action in $task.Actions) {
            if ([string]$action.Arguments -match 'windows-bootstrap|EmergencyReboot') { $legacy = $true }
        }
        if ($legacy -and $task.State -ne "Disabled") { throw "legacy_task_not_quiescent" }
    }
    foreach ($process in Get-CimInstance Win32_Process) {
        if ([string]$process.CommandLine -match 'windows-bootstrap\.ps1') { throw "legacy_process_running" }
    }
}

function Invoke-OwnedDocker([string[]]$Arguments) {
    $distribution = [string]$script:config.resource.distribution
    if ($distribution -notmatch '^[a-zA-Z0-9_.-]{1,80}$') { throw "invalid_owned_distribution" }
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = Join-Path $env:SystemRoot "System32\wsl.exe"
    # All variable arguments are restricted identifiers; no shell is invoked.
    $start.Arguments = "--distribution $distribution --exec env -u DOCKER_HOST -u DOCKER_CONTEXT docker --host unix:///var/run/docker.sock " + ($Arguments -join " ")
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $child = New-Object Diagnostics.Process
    $child.StartInfo = $start
    [void]$child.Start()
    $output = $child.StandardOutput.ReadToEndAsync()
    $errors = $child.StandardError.ReadToEndAsync()
    if (-not $child.WaitForExit(20000)) { $child.Kill(); throw "fence_outcome_unknown" }
    if ($child.ExitCode -ne 0) { throw "owned_resource_unavailable" }
    return $output.Result
}

function Test-Fenced {
    if ($script:config.resource.kind -eq "wsl-containers") {
        $ids = @($script:config.resource.container_ids | Sort-Object -Unique)
        if ($ids.Count -lt 8 -or $ids.Count -ne @($script:config.resource.container_ids).Count) { throw "incomplete_writer_inventory" }
        $engine = (Invoke-OwnedDocker @("info", "--format", "{{.ID}}")).Trim()
        if ($engine -ne $script:config.resource.engine_id) { throw "owned_engine_mismatch" }
        $expected = @($ids)
        foreach ($id in $ids) { if ([string]$id -notmatch '^[0-9a-f]{64}$') { throw "invalid_owned_container" } }
        $replacement = $null
        if ($null -ne $script:config.resource.PSObject.Properties["new_standby"] -and $null -ne $script:config.resource.new_standby) {
            $replacement = $script:config.resource.new_standby
            if ($replacement.id -notmatch '^[0-9a-f]{64}$' -or $replacement.id -in $ids) { throw "invalid_rejoin_identity" }
            $expected += [string]$replacement.id
        }
        $objects = @{}
        foreach ($entry in (Invoke-OwnedDocker (@("container", "inspect") + $expected) | ConvertFrom-Json)) { $objects[$entry.Id] = $entry }
        if ($objects.Count -ne $expected.Count) { throw "incomplete_writer_inventory" }
        if ($null -ne $replacement) {
            $object = $objects[[string]$replacement.id]
            if ($object.Id -ne $replacement.id -or $object.Image -ne $replacement.image_id -or
                $object.Config.Labels.'personal-server.ha-cluster' -ne $script:config.cluster -or
                $object.Config.Labels.'personal-server.ha-node' -ne $script:config.node -or
                $object.Config.Labels.'personal-server.ha-generation' -ne [string]$script:config.generation -or
                $object.Config.Labels.'personal-server.ha-service' -ne "postgres-standby" -or
                $object.HostConfig.RestartPolicy.Name -notin @("no", "")) { throw "invalid_rejoin_identity" }
        }
        $observed = @((Invoke-OwnedDocker @("container", "ls", "--all", "--no-trunc", "--quiet", "--filter", "label=personal-server.ha-cluster=$($script:config.cluster)", "--filter", "label=personal-server.ha-node=$($script:config.node)")).Trim() -split '\s+' | Sort-Object -Unique)
        if (@(Compare-Object $expected $observed).Count -ne 0) { throw "incomplete_writer_inventory" }
        $allStopped = $true
        foreach ($id in $script:config.resource.container_ids) {
            if ([string]$id -notmatch '^[0-9a-f]{64}$') { throw "invalid_owned_container" }
            $object = $objects[[string]$id]
            if ($object.Id -ne $id -or $object.Image -ne $script:config.resource.image_ids.PSObject.Properties[$id].Value -or $object.Config.Labels.'personal-server.ha-cluster' -ne $script:config.cluster -or
                $object.Config.Labels.'personal-server.ha-node' -ne $script:config.node -or
                $object.HostConfig.RestartPolicy.Name -notin @("no", "")) { throw "owned_resource_mismatch" }
            if ($object.State.Running) { $allStopped = $false }
        }
        return $allStopped
    }
    if ($script:config.resource.kind -eq "hyperv-vm") {
        $id = [Guid]::Parse([string]$script:config.resource.vm_id)
        $vm = Get-VM -Id $id
        $identity = $vm.Notes | ConvertFrom-Json
        if ($identity.ha_cluster -ne $script:config.cluster -or $identity.ha_node -ne $script:config.node) { throw "owned_vm_mismatch" }
        return ($vm.State -eq "Off" -and $vm.AutomaticStartAction -eq "Nothing")
    }
    throw "unsupported_fence_kind"
}

try {
    if (-not $Go -or [Environment]::OSVersion.Platform -ne "Win32NT") { throw "native_approval_required" }
    $script:config = Read-PrivateJson $Plan
    $approved = Read-PrivateJson $Approval
    $journal = Join-Path ([IO.Path]::GetDirectoryName($Plan)) "controller-state.json"
    if (Test-Path -LiteralPath $journal -ErrorAction Stop) { [void](Read-PrivateBytes $journal) }
    foreach ($node in @("A", "B")) {
        [void](Read-PrivateBytes $script:config.pg_nodes.PSObject.Properties[$node].Value.passfile)
        [void](Read-PrivateBytes $script:config.pg_nodes.PSObject.Properties[$node].Value.sslrootcert)
    }
    $hash = $script:config.verified_sha256
    if ($script:config.schema_version -ne 1 -or $script:config.cluster -notmatch '^[a-zA-Z0-9_-]{1,100}$' -or $script:config.node -notin @("A", "B") -or
        [long]$script:config.generation -lt 1 -or $script:config.source_commit -notmatch '^[0-9a-f]{40}$' -or
        $approved.plan_sha256 -ne $hash -or $approved.source_commit -ne $script:config.source_commit -or
        $approved.action -notin @("fence", "ha-transition") -or [DateTimeOffset]::Parse($approved.expires_at) -le [DateTimeOffset]::UtcNow -or
        [DateTimeOffset]::Parse($approved.expires_at) -gt [DateTimeOffset]::UtcNow.AddHours(24)) { throw "approval_mismatch" }
    Assert-Quiescent
    foreach ($name in @("supervisor.lock", "daemon.lock", "recovery.lock")) {
        $path = Join-Path (Join-Path $script:config.legacy_root "data") $name
        $locks += [IO.File]::Open($path, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::Write, [IO.FileShare]::None)
    }
    @{guard="ready";plan_sha256=$hash;approval_sha256=$approved.verified_sha256;approval_action=$approved.action} | ConvertTo-Json -Compress
    while ($null -ne ($line = [Console]::ReadLine())) {
        if ($line.Length -gt 4096) { throw "invalid_fence_request" }
        Assert-Quiescent
        $request = $line | ConvertFrom-Json
        if ($request.action -notin @("observe", "fence") -or $request.node -ne $script:config.node -or
            $request.generation -ne $script:config.generation -or $request.nonce -notmatch '^[0-9a-f]{64}$' -or
            [DateTimeOffset]::Parse($approved.expires_at) -le [DateTimeOffset]::UtcNow) { throw "invalid_fence_request" }
        $fenced = Test-Fenced
        if ($request.action -eq "fence" -and -not $fenced) {
            if ($script:config.resource.kind -eq "wsl-containers") {
                foreach ($id in $script:config.resource.container_ids) { [void](Invoke-OwnedDocker @("container", "stop", "--time", "10", [string]$id)) }
            } else { Stop-VM -Id ([Guid]::Parse($script:config.resource.vm_id)) -TurnOff -Force -Confirm:$false }
            $fenced = Test-Fenced
        }
        @{status="ok";fenced=$fenced;node=$request.node;generation=$request.generation;nonce=$request.nonce} | ConvertTo-Json -Compress
    }
} catch {
    # Never emit command output, paths, configuration or native exception text.
    Write-Output '{"status":"blocked"}'
    exit 1
} finally {
    foreach ($stream in $locks) { $stream.Dispose() }
}
