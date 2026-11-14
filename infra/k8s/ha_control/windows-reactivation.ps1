# Draft native stage. Keep out of the controller bundle until the host guard
# can drain deployment admissions, settle recovery commands, and bind rollback.
function Get-HaReactivationHash([string]$Text) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text))).Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
}

function Read-HaReactivationTask($Pin, $Folder = $null) {
    $expected = @{supervisor='personal-server-autostart'; 'wsl-anchor'='PersonalServer-WSL-KeepAlive';
                  emergency='PersonalServer-EmergencyReboot'}
    if ($Pin.path -ne '\' -or -not $expected.ContainsKey($Pin.role) -or $Pin.name -ine $expected[$Pin.role]) {
        throw 'legacy_reactivation_task_scope'
    }
    $scheduler = $null
    try {
        if ($null -eq $Folder) {
            $scheduler = New-Object -ComObject 'Schedule.Service'
            $scheduler.Connect()
            $Folder = $scheduler.GetFolder($Pin.path)
        }
        $registered = $Folder.GetTask($Pin.name)
        [xml]$xml = $registered.Xml
        $task = [pscustomobject]@{
            State = switch ([int]$registered.State) {
                1 { 'Disabled' }
                3 { 'Ready' }
                4 { 'Running' }
                default { throw 'legacy_reactivation_task_pending' }
            }
            Settings = [pscustomobject]@{ Enabled = [bool]$registered.Enabled }
        }
    } finally {
        if ($null -ne $scheduler) { $null = [Runtime.InteropServices.Marshal]::ReleaseComObject($scheduler) }
    }
    $settings = $xml.DocumentElement.SelectSingleNode("*[local-name()='Settings']")
    $enabled = $settings.SelectSingleNode("*[local-name()='Enabled']")
    if ($null -ne $enabled) { $null = $settings.RemoveChild($enabled) }
    if ((Get-HaReactivationHash $xml.OuterXml) -cne $Pin.spec_sha256) {
        throw 'legacy_reactivation_task_drift'
    }
    # Disabling the anchor prevents new starts while preserving its pinned running instance.
    $runningDisabledAnchor = $Pin.role -ceq 'wsl-anchor' -and
        $task.State -eq 'Running' -and -not $task.Settings.Enabled
    if ($task.State -notin @('Disabled', 'Ready', 'Running') -or
        ((($task.State -eq 'Disabled') -eq [bool]$task.Settings.Enabled) -and -not $runningDisabledAnchor)) {
        throw 'legacy_reactivation_task_pending'
    }
    return $task
}

function Read-HaReactivationRunner($Pin) {
    $ErrorActionPreference = 'Stop'
    if ($Pin.name -notmatch '^actions\.runner\.[A-Za-z0-9._-]{1,180}$') { throw 'legacy_reactivation_runner_scope' }
    $config = Get-ItemProperty -LiteralPath "HKLM:\SYSTEM\CurrentControlSet\Services\$($Pin.name)" -ErrorAction Stop
    if ($config.PSChildName -cne $Pin.name -or $config.ImagePath -isnot [string] -or $config.ObjectName -isnot [string] -or
        (Get-HaReactivationHash "$($config.PSChildName)`n$($config.ImagePath)`n$($config.ObjectName)") -cne $Pin.spec_sha256) {
        throw 'legacy_reactivation_runner_drift'
    }
    # Query the pinned service directly without enumerating the host service table.
    if ($null -eq ('System.ServiceProcess.ServiceController' -as [type])) {
        Add-Type -AssemblyName System.ServiceProcess -ErrorAction Stop
    }
    $service = [System.ServiceProcess.ServiceController]::new()
    try {
        $service.ServiceName = $Pin.name
        $serviceStartType = [int]$service.StartType
        $state = [string]$service.Status
    } finally { $service.Dispose() }
    $startMode = switch ($serviceStartType) {
        2 { 'Auto' }
        4 { 'Disabled' }
        default { throw 'legacy_reactivation_runner_pending' }
    }
    $delayed = $config.PSObject.Properties['DelayedAutoStart']
    $isDelayed = $null -ne $delayed -and [int]$delayed.Value -eq 1
    if ($config.Start -isnot [int] -or $config.Start -ne $serviceStartType -or
        ($startMode -eq 'Auto' -and $isDelayed -ne $Pin.delayed) -or $state -notin @('Running', 'Stopped')) {
        throw 'legacy_reactivation_runner_pending'
    }
    return [pscustomobject]@{ StartMode = $startMode; State = $state }
}

function Read-HaReactivationSnapshot($Plan, [switch]$ClosedObservation, $Progress = $null) {
    if ($null -ne $Progress) { $Progress.step = 'windows_boot' }
    $boot = (Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 5 -ErrorAction Stop).LastBootUpTime
    if ($boot.ToUniversalTime().Ticks.ToString() -cne $Plan.boot_ticks) {
        throw 'legacy_reactivation_host_rebooted'
    }
    if ($null -ne $Progress) { $Progress.step = 'windows_shutdown' }
    try {
        $shutdown = @(Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='User32';
                       Id=1074; StartTime=$boot} -MaxEvents 1 -ErrorAction Stop)
    } catch {
        if ($_.FullyQualifiedErrorId -notlike 'NoMatchingEventsFound*') { throw }
        $shutdown = @()
    }
    if ($shutdown.Count -ne 0) { throw 'legacy_reactivation_shutdown_unconfirmed' }
    $tasks = @($Plan.tasks)
    if ($tasks.Count -ne 3 -or @($tasks.name | Sort-Object -Unique).Count -ne $tasks.Count -or
        @($tasks | Where-Object { $_.role -ceq 'supervisor' }).Count -ne 1 -or
        @($tasks | Where-Object { $_.role -ceq 'wsl-anchor' }).Count -ne 1 -or
        @($tasks | Where-Object { $_.role -ceq 'emergency' }).Count -ne 1 -or
        @($tasks | Where-Object { $_.enabled -isnot [bool] }).Count -ne 0 -or
        $Plan.runner.delayed -isnot [bool]) {
        throw 'legacy_reactivation_tasks_incomplete'
    }
    if ($null -ne $Progress) { $Progress.step = 'windows_tasks' }
    $observed = @{}
    $scheduler = New-Object -ComObject 'Schedule.Service'
    try {
        $scheduler.Connect()
        $folder = $scheduler.GetFolder('\')
        Assert-HaReactivationTaskIdle (@($tasks | Where-Object { $_.role -ceq 'emergency' })[0]) $folder
        foreach ($pin in $tasks) { $observed[$pin.role] = Read-HaReactivationTask $pin $folder }
        if ($ClosedObservation) {
            Assert-HaReactivationTaskIdle (@($tasks | Where-Object { $_.role -ceq 'supervisor' })[0]) $folder
            Assert-HaTaskInstance (@($tasks | Where-Object { $_.role -ceq 'wsl-anchor' })[0]) $folder
        }
    } finally { $null = [Runtime.InteropServices.Marshal]::ReleaseComObject($scheduler) }
    if ($null -ne $Progress) { $Progress.step = 'windows_runner' }
    $runner = Read-HaReactivationRunner $Plan.runner
    if (-not $ClosedObservation) {
        $workers = @(Get-CimInstance Win32_Process -Filter "Name='Runner.Worker.exe'" -OperationTimeoutSec 5 -ErrorAction Stop)
        if ($workers.Count -ne 0) { throw 'legacy_reactivation_runner_busy' }
    }
    return @{tasks=$observed; runner=$runner}
}

function Get-HaTaskInstances($Pin, $Folder = $null) {
    $scheduler = $null
    try {
        if ($null -eq $Folder) {
            $scheduler = New-Object -ComObject 'Schedule.Service'
            $scheduler.Connect()
            $Folder = $scheduler.GetFolder($Pin.path)
        }
        $task = $Folder.GetTask($Pin.name)
        foreach ($instance in $task.GetInstances(0)) {
            [pscustomobject]@{guid=[string]$instance.InstanceGuid; state=[int]$instance.State}
        }
    } finally {
        if ($null -ne $scheduler) { $null = [Runtime.InteropServices.Marshal]::ReleaseComObject($scheduler) }
    }
}

function Assert-HaReactivationTaskIdle($Pin, $Folder = $null) {
    if (@(Get-HaTaskInstances $Pin $Folder).Count -ne 0) { throw 'legacy_reactivation_task_instance_present' }
}

function Assert-HaTaskInstance($Pin, $Folder = $null) {
    $instances = @(Get-HaTaskInstances $Pin $Folder)
    if ($instances.Count -ne 1 -or $instances[0].state -ne 4 -or
        $instances[0].guid -ine $Pin.instance_guid -or
        $Pin.instance_guid -notmatch '^\{[a-fA-F0-9-]{36}\}$') {
        throw 'legacy_reactivation_task_instance_drift'
    }
}

function Assert-HaReactivationClosed($Plan, $Progress = $null) {
    # The final process inventory below checks the runner worker as well.
    $state = Read-HaReactivationSnapshot $Plan -ClosedObservation -Progress $Progress
    if (@($state.tasks.Values | Where-Object { $_.Settings.Enabled }).Count -ne 0 -or
        $state.tasks.supervisor.State -ne 'Disabled' -or $state.tasks.emergency.State -ne 'Disabled' -or
        $state.tasks['wsl-anchor'].State -notin @('Disabled', 'Running') -or
        $state.runner.StartMode -ne 'Disabled' -or $state.runner.State -ne 'Stopped') {
        throw 'legacy_reactivation_not_closed'
    }
    # Stopping a scheduled task is insufficient if it already spawned a detached child.
    if ($null -ne $Progress) { $Progress.step = 'windows_processes' }
    $filter = "CommandLine LIKE '%windows-bootstrap.ps1%' OR Name = 'Runner.Listener.exe' OR Name = 'Runner.Worker.exe'"
    $children = @(Get-CimInstance Win32_Process -Property ProcessId -Filter $filter -OperationTimeoutSec 5 -ErrorAction Stop)
    if ($children.Count -ne 0) { throw 'legacy_reactivation_child_present' }
}

function Open-HaRecoveryProcess($Pin, [string]$Role, [string]$Bootstrap) {
    if ($Pin.pid -isnot [int] -or $Pin.pid -le 4 -or
        $Pin.command_sha256 -cnotmatch '^[a-f0-9]{64}$' -or
        $Pin.created_ticks -cnotmatch '^[1-9][0-9]{1,20}$') { throw 'legacy_reactivation_process_pin' }
    $process = [Diagnostics.Process]::GetProcessById($Pin.pid)
    try {
        # Cache the kernel handle while the process identity is being checked.
        $null = $process.Handle
        $row = Get-CimInstance Win32_Process -Filter "ProcessId=$($Pin.pid)" -OperationTimeoutSec 5 -ErrorAction Stop
        $systemPowerShell = Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'
        $command = [string]$row.CommandLine
        $rolePattern = '(?i)(?:^|\s)-' + $Role + '(?:\s|$)'
        $scriptPattern = '(?i)(?:^|\s)-File\s+(?:"' + [regex]::Escape($Bootstrap) + '"|' +
                         [regex]::Escape($Bootstrap) + ')(?:\s|$)'
        if ($null -eq $row -or $process.HasExited -or $row.ExecutablePath -ine $systemPowerShell -or
            $command -notmatch $rolePattern -or $command -notmatch $scriptPattern -or
            (Get-HaReactivationHash $command) -cne $Pin.command_sha256 -or
            $process.StartTime.ToUniversalTime().Ticks.ToString() -cne $Pin.created_ticks) {
            throw 'legacy_reactivation_process_drift'
        }
        return @{process=$process; parent_id=[int]$row.ParentProcessId}
    } catch {
        $process.Dispose()
        throw
    }
}

function Stop-HaPinnedRecoveryProcess($Process) {
    if ($Process.HasExited) { return }
    if (-not ('N100Ha.ReactivationProcess' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
namespace N100Ha {
    public static class ReactivationProcess {
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool TerminateProcess(SafeProcessHandle process, uint exitCode);
    }
}
'@ -ErrorAction Stop
    }
    $ok = [N100Ha.ReactivationProcess]::TerminateProcess($Process.SafeHandle, 1)
    if ((-not $ok -and -not $Process.HasExited) -or -not $Process.WaitForExit(10000)) {
        throw 'legacy_reactivation_process_exit_unconfirmed'
    }
}

function Update-HaSupervisorInstance($Plan) {
    # The task may have restarted since the plan was staged. Only the
    # execution identity can change. The task and command definitions are pinned.
    $task = @($Plan.tasks | Where-Object { $_.role -ceq 'supervisor' })[0]
    $null = Read-HaReactivationTask $task
    $instances = @(Get-HaTaskInstances $task)
    if ($instances.Count -ne 1 -or $instances[0].state -ne 4 -or
        $instances[0].guid -notmatch '^\{[a-fA-F0-9-]{36}\}$') {
        throw 'legacy_reactivation_task_instance_drift'
    }
    $processes = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -OperationTimeoutSec 5 -ErrorAction Stop)
    $pins = @{}
    foreach ($role in @('supervisor', 'daemon')) {
        $expected = $Plan.$role
        $matches = @($processes | Where-Object {
            (Get-HaReactivationHash ([string]$_.CommandLine)) -ceq $expected.command_sha256
        })
        if ($matches.Count -ne 1) { throw 'legacy_reactivation_process_drift' }
        $process = [Diagnostics.Process]::GetProcessById($matches[0].ProcessId)
        try {
            $pins[$role] = [pscustomobject]@{
                pid = $process.Id
                created_ticks = $process.StartTime.ToUniversalTime().Ticks.ToString()
                command_sha256 = $expected.command_sha256
            }
        } finally { $process.Dispose() }
    }
    $task.instance_guid = $instances[0].guid
    $Plan.supervisor = $pins.supervisor
    $Plan.daemon = $pins.daemon
}

function Close-HaReactivation($Plan, [scriptblock]$AssertAdmissionsDrained, [scriptblock]$AssertRecoverySettled) {
    if ($null -eq $AssertAdmissionsDrained -or $null -eq $AssertRecoverySettled) {
        throw 'legacy_reactivation_admission_guard_required'
    }
    & $AssertAdmissionsDrained
    $state = Read-HaReactivationSnapshot $Plan
    if ($state.runner.StartMode -ne 'Auto' -or $state.runner.State -ne 'Running' -or
        $state.tasks.supervisor.State -ne 'Running' -or $state.tasks['wsl-anchor'].State -ne 'Running') {
        throw 'legacy_reactivation_original_state_changed'
    }
    $bootstrap = Join-Path $env:SystemDrive 'personal-server\scripts\windows-bootstrap.ps1'
    Update-HaSupervisorInstance $Plan
    foreach ($pin in $Plan.tasks) {
        if ($pin.role -ne 'emergency') { Assert-HaTaskInstance $pin }
        if ([bool]$state.tasks[$pin.role].Settings.Enabled -ne $pin.enabled) {
            throw 'legacy_reactivation_original_state_changed'
        }
    }
    $supervisor = Open-HaRecoveryProcess $Plan.supervisor 'Supervisor' $bootstrap
    $daemon = $null
    try {
        $daemon = Open-HaRecoveryProcess $Plan.daemon 'Daemon' $bootstrap
        if ($daemon.parent_id -ne $supervisor.process.Id -or
            $daemon.process.StartTime -lt $supervisor.process.StartTime) {
            throw 'legacy_reactivation_process_parent_changed'
        }
        & $AssertRecoverySettled
        Assert-HaTaskInstance (@($Plan.tasks | Where-Object { $_.role -ceq 'supervisor' })[0])
        if ($supervisor.process.HasExited -or $daemon.process.HasExited) {
            throw 'legacy_reactivation_process_drift'
        }
        Set-Service -Name $Plan.runner.name -StartupType Disabled -ErrorAction Stop
        # Refuse to interrupt a GitHub Actions job even if it was assigned during the first observation.
        $null = Read-HaReactivationSnapshot $Plan
        & $AssertAdmissionsDrained
        Stop-Service -Name $Plan.runner.name -ErrorAction Stop
        foreach ($pin in $Plan.tasks) {
            $null = Read-HaReactivationTask $pin
            if ($pin.enabled) {
                $null = Disable-ScheduledTask -TaskPath $pin.path -TaskName $pin.name -ErrorAction Stop
            }
            if ($pin.role -ceq 'supervisor') {
                Assert-HaTaskInstance $pin
                Stop-ScheduledTask -TaskPath $pin.path -TaskName $pin.name -ErrorAction Stop
            }
        }
        Assert-HaReactivationTaskIdle (@($Plan.tasks | Where-Object { $_.role -ceq 'supervisor' })[0])
        if (-not $supervisor.process.WaitForExit(10000)) { throw 'legacy_reactivation_supervisor_exit_unconfirmed' }
        & $AssertRecoverySettled
        Stop-HaPinnedRecoveryProcess $daemon.process
        Assert-HaReactivationClosed $Plan
    } finally {
        if ($null -ne $daemon) { $daemon.process.Dispose() }
        $supervisor.process.Dispose()
    }
}

function Restore-HaReactivation($Plan, [scriptblock]$AssertRollbackSafe) {
    if ($null -eq $AssertRollbackSafe) { throw 'legacy_reactivation_rollback_guard_required' }
    & $AssertRollbackSafe
    Assert-HaTaskInstance (@($Plan.tasks | Where-Object { $_.role -ceq 'wsl-anchor' })[0])
    $null = Read-HaReactivationSnapshot $Plan
    foreach ($pin in $Plan.tasks) {
        $task = Read-HaReactivationTask $pin
        if ($pin.enabled -and -not $task.Settings.Enabled) {
            $null = Enable-ScheduledTask -TaskPath $pin.path -TaskName $pin.name -ErrorAction Stop
        }
        if ($pin.role -ceq 'supervisor' -and (Read-HaReactivationTask $pin).State -ne 'Running') {
            Start-ScheduledTask -TaskPath $pin.path -TaskName $pin.name -ErrorAction Stop
        }
    }
    $runner = Read-HaReactivationRunner $Plan.runner
    if ($runner.StartMode -ne 'Auto') {
        $startMode = if ($Plan.runner.delayed) { 'delayed-auto' } else { 'auto' }
        $scm = Join-Path ([Environment]::SystemDirectory) 'sc.exe'
        $null = & $scm config $Plan.runner.name 'start=' $startMode
        if ($LASTEXITCODE -ne 0) { throw 'legacy_reactivation_runner_restore_failed' }
    }
    if ($runner.State -ne 'Running') { Start-Service -Name $Plan.runner.name -ErrorAction Stop }
    $state = Read-HaReactivationSnapshot $Plan
    if ($state.runner.StartMode -ne 'Auto' -or $state.runner.State -ne 'Running' -or
        $state.tasks.supervisor.State -ne 'Running') {
        throw 'legacy_reactivation_restore_unconfirmed'
    }
    foreach ($pin in $Plan.tasks) {
        if ([bool]$state.tasks[$pin.role].Settings.Enabled -ne $pin.enabled) {
            throw 'legacy_reactivation_restore_unconfirmed'
        }
    }
}

function Invoke-HaRecoveryCycleGuard([string]$BootstrapSha256, [scriptblock]$Action) {
    if ($BootstrapSha256 -cnotmatch '^[a-f0-9]{64}$' -or $null -eq $Action) {
        throw 'legacy_recovery_guard_scope'
    }
    $root = Join-Path $env:SystemDrive 'personal-server'
    $path = Join-Path $root 'data\recovery.lock'
    $source = Join-Path $root 'scripts\windows-bootstrap.ps1'
    foreach ($candidate in @($root, (Split-Path $path), (Split-Path $source), $source, $path)) {
        $item = Get-Item -LiteralPath $candidate -Force -ErrorAction SilentlyContinue
        if ($null -ne $item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'legacy_recovery_guard_path'
        }
    }
    if ((Get-FileHash -LiteralPath $source -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant() -cne $BootstrapSha256) {
        throw 'legacy_recovery_guard_source_drift'
    }
    try {
        $stream = [IO.File]::Open($path, [IO.FileMode]::OpenOrCreate,
                                  [IO.FileAccess]::Write, [IO.FileShare]::None)
    } catch [IO.IOException] {
        throw 'legacy_recovery_cycle_in_flight'
    }
    try {
        # This is the lock used by Invoke-RecoveryCycle. The caller separately
        # excludes direct bootstrap actions and proves native children settled.
        & $Action $stream
    } finally { $stream.Dispose() }
}

function Assert-HaRecoveryCommandsSettled([string]$Distribution) {
    if ($Distribution -cne 'Ubuntu-24.04') { throw 'legacy_recovery_distribution_scope' }
    $exe = Join-Path ([Environment]::SystemDirectory) 'wsl.exe'
    $running = @(& $exe --list --running --quiet | ForEach-Object { ($_ -replace ([char]0),'').Trim() })
    if ($LASTEXITCODE -ne 0 -or $Distribution -cnotin $running) { throw 'legacy_recovery_wsl_not_running' }
    # Read /proc with a fixed program. A killed wsl.exe client can leave
    # bash, kubectl, docker, or systemctl work running in the distribution.
    $code = @'
import os, pathlib, re, sys
names = re.compile(rb'(windows-bootstrap[.]sh|deploy-n100(?:-safe)?[.]sh|kubectl|systemctl|docker)')
mutation = re.compile(rb'(rollout|restart|start|stop|compose|apply|scale|patch|update|delete|replace|create)')
for item in pathlib.Path('/proc').iterdir():
    if not item.name.isdecimal() or int(item.name) == os.getpid():
        continue
    try:
        args = (item / 'cmdline').read_bytes().split(b'\0')
    except (FileNotFoundError, ProcessLookupError):
        continue
    except OSError:
        sys.exit(2)
    if any(b'windows-bootstrap.sh' in arg or b'deploy-n100' in arg for arg in args) or (
            any(names.search(arg) for arg in args) and any(mutation.fullmatch(arg) for arg in args)):
        sys.exit(3)
'@
    $null = $code | & $exe --distribution $Distribution --exec /usr/bin/timeout 5 /usr/bin/python3 -
    if ($LASTEXITCODE -ne 0) { throw 'legacy_recovery_wsl_commands_unsettled' }
}

function Get-HaCutoverAuthority($Plan) {
    if ($Plan.authority_service_name -cnotmatch '^N100HA_[0-9a-f]{24}_controller$') {
        throw 'cutover_authority_service_identity'
    }
    $programData = [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
    if ($programData -ine 'C:\ProgramData') { throw 'cutover_authority_wsl_alias_unverified' }
    $directory = Join-Path $programData 'N100HA-Cutover'
    $admins = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $controller = [Security.Principal.NTAccount]::new('NT SERVICE', $Plan.authority_service_name).
        Translate([Security.Principal.SecurityIdentifier])
    $danger = [Security.AccessControl.FileSystemRights]'Delete,DeleteSubdirectoriesAndFiles,ChangePermissions,TakeOwnership'
    foreach ($parent in @('C:\', $programData, $directory)) {
        $node = Get-Item -LiteralPath $parent -Force
        if (-not $node.PSIsContainer -or ($node.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'cutover_authority_parent_untrusted'
        }
        $rules = (Get-Acl -LiteralPath $parent).GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])
        if (@($rules | Where-Object { $_.AccessControlType -eq 'Allow' -and
              $_.IdentityReference -notin @($admins, $system) -and
              -not ($_.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -and
              ($_.FileSystemRights -band $danger) }).Count -ne 0) {
            throw 'cutover_authority_parent_replaceable'
        }
    }
    $dirSecurity = [Security.AccessControl.DirectorySecurity]::new()
    $dirSecurity.SetOwner($admins)
    $dirSecurity.SetAccessRuleProtection($true, $false)
    foreach ($writer in @($admins, $system)) {
        $dirSecurity.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $writer, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    }
    $dirSecurity.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $controller, 'ReadAndExecute', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    $users = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-545')
    $dirSecurity.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $users, 'ReadAndExecute', 'None', 'None', 'Allow'))
    $sections = [Security.AccessControl.AccessControlSections]'Owner,Access'
    if ((Get-Acl -LiteralPath $directory).GetSecurityDescriptorSddlForm($sections) -cne
        $dirSecurity.GetSecurityDescriptorSddlForm($sections)) {
        throw 'cutover_authority_directory_acl'
    }
    $legacyData = Join-Path $env:SystemDrive 'personal-server\data'
    if (@(Get-ChildItem -LiteralPath $legacyData -Force -ErrorAction Stop |
          Where-Object { $_.Name -ieq 'ha-controller-owner.json' }).Count -ne 0) {
        throw 'cutover_authority_legacy_marker_present'
    }
    return @{directory=$directory; admins=$admins; system=$system; controller=$controller}
}

function New-HaCutoverMarker($Plan, [string]$PlanSha256, [string]$AttemptId) {
    $authority = Get-HaCutoverAuthority $Plan
    $path = Join-Path $authority.directory 'ha-controller-owner.json'
    if ($Plan.authority_cluster -cnotmatch '^[a-z][a-z0-9_]{0,62}$' -or
        $Plan.authority_generation -isnot [int] -or $Plan.authority_generation -lt 1 -or
        $Plan.authority_source_commit -cnotmatch '^[a-f0-9]{40}$') {
        throw 'cutover_authority_marker_scope'
    }
    $payload = [ordered]@{cluster=$Plan.authority_cluster;generation=$Plan.authority_generation;
        source_commit=$Plan.authority_source_commit;plan_sha256=$PlanSha256;attempt_id=$AttemptId}
    $raw = [Text.Encoding]::UTF8.GetBytes(($payload | ConvertTo-Json -Compress))
    $security = [Security.AccessControl.FileSecurity]::new()
    $security.SetOwner($authority.admins)
    $security.SetAccessRuleProtection($true, $false)
    foreach ($writer in @($authority.admins, $authority.system)) {
        $security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($writer, 'FullControl', 'Allow'))
    }
    $security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($authority.controller, 'Read', 'Allow'))
    $rights = [Security.AccessControl.FileSystemRights]'WriteData,Synchronize'
    $file = [IO.FileStream]::new($path, [IO.FileMode]::CreateNew, $rights, [IO.FileShare]::None,
        4096, [IO.FileOptions]::WriteThrough, $security)
    try {
        $file.Write($raw, 0, $raw.Length)
        $file.Flush($true)
    } finally { $file.Dispose() }
    $sections = [Security.AccessControl.AccessControlSections]'Owner,Access'
    $pin = @{
        path=$path
        sha256=(Get-HaReactivationHash ([Text.Encoding]::UTF8.GetString($raw)))
        acl=$security.GetSecurityDescriptorSddlForm($sections)
        file_id=(Get-HaMarkerFileId $path)
    }
    Assert-HaOwnedCutoverMarker $pin
    return $pin
}

function Get-HaMarkerFileId([string]$Path) {
    $fsutil = Join-Path ([Environment]::SystemDirectory) 'fsutil.exe'
    $links = @(& $fsutil hardlink list $Path)
    if ($LASTEXITCODE -ne 0 -or $links.Count -ne 1) { throw 'cutover_authority_marker_untrusted' }
    $identity = @(& $fsutil file queryfileid $Path)
    if ($LASTEXITCODE -ne 0 -or $identity.Count -ne 1 -or $identity[0] -notmatch '0x[0-9a-fA-F]{16,32}') {
        throw 'cutover_authority_marker_identity'
    }
    return $Matches[0].ToLowerInvariant()
}

function Assert-HaOwnedCutoverMarker($Pin) {
    $item = Get-Item -LiteralPath $Pin.path -Force
    $sections = [Security.AccessControl.AccessControlSections]'Owner,Access'
    if ($item.PSIsContainer -or $item.Length -gt 4096 -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        (Get-HaMarkerFileId $Pin.path) -cne $Pin.file_id -or
        (Get-Acl -LiteralPath $Pin.path).GetSecurityDescriptorSddlForm($sections) -cne $Pin.acl -or
        (Get-FileHash -LiteralPath $Pin.path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $Pin.sha256) {
        throw 'cutover_authority_marker_unconfirmed'
    }
}

function Invoke-HaReactivationSession($Plan, [string]$PlanSha256, [string]$AttemptId) {
    if ($PlanSha256 -cnotmatch '^[a-f0-9]{64}$' -or $AttemptId -cnotmatch '^[a-f0-9]{64}$') {
        throw 'legacy_reactivation_session_scope'
    }
    $ErrorActionPreference = 'Stop'
    Set-StrictMode -Version Latest
    $null = Read-HaReactivationSnapshot $Plan
    $null = Get-HaCutoverAuthority $Plan
    $seq = 0
    $handshake = @{plan_sha256=$PlanSha256; attempt_id=$AttemptId; sequence=0; action='ready'}
    [Console]::WriteLine(($handshake | ConvertTo-Json -Compress))

    $read = {
        $line = [Console]::ReadLine()
        if ($null -eq $line -or $line.Length -gt 4096) { throw 'legacy_reactivation_session_lost' }
        $request = ConvertFrom-Json -InputObject $line -ErrorAction Stop
        if (@($request.PSObject.Properties).Count -ne 6 -or $request.plan_sha256 -cne $PlanSha256 -or
            $request.attempt_id -cne $AttemptId -or $request.sequence -ne ($seq + 1) -or
            $request.nonce -cnotmatch '^[a-f0-9]{64}$' -or $request.proof_sha256 -cnotmatch '^[a-f0-9]{64}$') {
            throw 'legacy_reactivation_session_request'
        }
        $request
    }
    $send = {
        param($Request)
        [Console]::WriteLine((@{plan_sha256=$PlanSha256; attempt_id=$AttemptId;
            sequence=$Request.sequence; nonce=$Request.nonce; action=$Request.action;
            proof_sha256=$Request.proof_sha256} | ConvertTo-Json -Compress))
    }

    $claim = & $read
    if ($claim.action -cne 'claim') { throw 'legacy_reactivation_session_order' }
    $seq = $claim.sequence
    $marker = New-HaCutoverMarker $Plan $PlanSha256 $AttemptId
    & $send $claim
    $first = & $read
    if ($first.action -cne 'close') { throw 'legacy_reactivation_session_order' }
    $seq = $first.sequence
    # The coordinator runs GitHub's admission check before sending each request.
    # Windows additionally verifies no local worker or recovery command is active.
    $restoreRequest = Invoke-HaRecoveryCycleGuard $Plan.bootstrap_sha256 {
        param($Lease)
        $checkAdmission = {
            $null = Read-HaReactivationSnapshot $Plan
        }
        $checkRecovery = {
            if ($Lease.SafeFileHandle.IsClosed) { throw 'legacy_recovery_guard_lost' }
            Assert-HaRecoveryCommandsSettled $Plan.distribution
        }
        Assert-HaOwnedCutoverMarker $marker
        Close-HaReactivation $Plan $checkAdmission $checkRecovery
        & $send $first
        while ($true) {
            $request = & $read
            $seq = $request.sequence
            Assert-HaOwnedCutoverMarker $marker
            if ($request.action -ceq 'restore') {
                Assert-HaReactivationClosed $Plan
                & $checkRecovery
                [IO.File]::Delete($marker.path)
                if ([IO.File]::Exists($marker.path)) { throw 'cutover_authority_release_unconfirmed' }
                return $request
            }
            if ($request.action -ceq 'finish') {
                Assert-HaReactivationClosed $Plan
                & $checkRecovery
                & $send $request
                return
            }
            if ($request.action -cne 'verify') { throw 'legacy_reactivation_session_order' }
            Assert-HaReactivationClosed $Plan
            & $checkRecovery
            & $send $request
        }
    }
    if ($null -ne $restoreRequest) {
        if ($restoreRequest.action -cne 'restore') { throw 'legacy_reactivation_session_output' }
        # The controller sends restore only after it proves VM writes never
        # opened, routing stayed on WSL, and the legacy application is healthy.
        Restore-HaReactivation $Plan {
            Assert-HaReactivationClosed $Plan
            Assert-HaRecoveryCommandsSettled $Plan.distribution
        }
        & $send $restoreRequest
    }
}
