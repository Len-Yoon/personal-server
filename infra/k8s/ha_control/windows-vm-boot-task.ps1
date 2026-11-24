# Register only the VM start phase of N100 boot recovery.
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[a-f0-9]{64}$')]
    [string]$VmAIdSha256,
    [Parameter(Mandatory)]
    [ValidatePattern('^[a-f0-9]{64}$')]
    [string]$VmBIdSha256,
    [Parameter(Mandatory)]
    [ValidatePattern('^[a-f0-9]{64}$')]
    [string]$ScriptSha256,
    [Parameter(Mandatory)]
    [string]$ScriptBase64,
    [switch]$Install
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($VmAIdSha256 -ceq $VmBIdSha256 -or $ScriptBase64.Length -gt 16384) {
    throw 'n100_boot_task_input_unverified'
}
$bytes = [Convert]::FromBase64String($ScriptBase64)
$sha = [Security.Cryptography.SHA256]::Create()
try {
    $digest = [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant()
} finally {
    $sha.Dispose()
}
if ($digest -cne $ScriptSha256) { throw 'n100_boot_task_script_unpinned' }
$scriptText = [Text.UTF8Encoding]::new($false, $true).GetString($bytes)
[void][ScriptBlock]::Create($scriptText)

$name = 'N100HA-Start-VM-Guests'
$taskPath = '\'
$engine = Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $engine -PathType Leaf)) { throw 'n100_boot_task_engine_unverified' }

# Store the exact reviewed script within the Scheduler action. Startup needs
# no user checkout, shell profile, local script file, or network.
$template = '$ErrorActionPreference=''Stop''; &([ScriptBlock]::Create([Text.UTF8Encoding]::new($false,$true).GetString([Convert]::FromBase64String(''{0}'')))) -VmAIdSha256 ''{1}'' -VmBIdSha256 ''{2}'' -StartGuests'
$launch = $template -f $ScriptBase64, $VmAIdSha256, $VmBIdSha256
$payload = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($launch))
$arguments = '-NoLogo -NoProfile -NonInteractive -EncodedCommand ' + $payload
if ($arguments.Length -gt 24000) { throw 'n100_boot_task_action_too_large' }

function Get-ExistingTask {
    Get-ScheduledTask -TaskPath $taskPath -ErrorAction Stop | Where-Object { $_.TaskName -ceq $name }
}

function Assert-Task($Task) {
    $actions = @($Task.Actions)
    $triggers = @($Task.Triggers)
    if ($actions.Count -ne 1 -or $actions[0].Execute -ine $engine -or $actions[0].Arguments -cne $arguments -or
        $triggers.Count -ne 1 -or $triggers[0].CimClass.CimClassName -cne 'MSFT_TaskBootTrigger' -or
        -not $triggers[0].Enabled -or $triggers[0].Delay -cne 'PT45S' -or
        $Task.Principal.UserId -inotmatch '^(SYSTEM|NT AUTHORITY\\SYSTEM|S-1-5-18)$' -or
        [string]$Task.Principal.LogonType -cne 'ServiceAccount' -or
        [string]$Task.Principal.RunLevel -cne 'Highest' -or
        -not $Task.Settings.Enabled -or
        [string]$Task.Settings.MultipleInstances -cne 'IgnoreNew' -or
        $Task.Settings.StartWhenAvailable -or $Task.Settings.RestartCount -ne 0 -or
        $Task.Settings.ExecutionTimeLimit -cne 'PT8M') {
        throw 'n100_boot_task_definition_conflict'
    }
}

$existing = @(Get-ExistingTask)
if ($existing.Count -gt 1) { throw 'n100_boot_task_definition_conflict' }
if ($existing.Count -eq 1) {
    Assert-Task $existing[0]
    $result = 'already_registered'
} elseif ($Install) {
    $action = New-ScheduledTaskAction -Execute $engine -Argument $arguments
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $trigger.Delay = 'PT45S'
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::FromMinutes(8))
    $taskArgs = @{
        Action = $action; Trigger = $trigger; Principal = $principal; Settings = $settings
        Description = 'Start only the two pinned N100 Hyper-V guests. Writer admission is separate.'
    }
    $task = New-ScheduledTask @taskArgs
    Register-ScheduledTask -TaskName $name -TaskPath $taskPath -InputObject $task -ErrorAction Stop | Out-Null
    $installed = @(Get-ExistingTask)
    if ($installed.Count -ne 1) { throw 'n100_boot_task_install_unverified' }
    Assert-Task $installed[0]
    $result = 'registered'
} else {
    $result = 'ready_to_register'
}
@{ result = $result; script_sha256 = $ScriptSha256; task = $name; scope = 'vm_start_only' } | ConvertTo-Json -Compress
