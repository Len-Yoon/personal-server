# Start one operator-owned lease observer. The task has no trigger or restart policy.
param([string]$InputPath,[string]$InputSha256,[switch]$Go)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Digest([byte[]]$Bytes) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { [BitConverter]::ToString($sha.ComputeHash($Bytes)).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Assert-LocalPath([string]$Path) {
    if ($Path -cnotmatch '\AC:\\ProgramData\\N100HA-Cutover-Inputs\\[a-f0-9]{40}\\[A-Za-z0-9_.-]+\z' -or
        $Path -match '\\\.\.?(\\|$)' -or $Path.EndsWith('.')) { throw 'observer_task_input_path' }
}

try {
    if (-not $Go -or $PSVersionTable.PSEdition -cne 'Desktop' -or -not [Environment]::Is64BitProcess -or
        $InputSha256 -cnotmatch '\A[a-f0-9]{64}\z') { throw 'observer_task_scope' }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        if (-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            throw 'observer_task_operator'
        }
        $operatorSid = $identity.User.Value
        $operatorName = $identity.Name
    } finally { $identity.Dispose() }
    Assert-LocalPath $InputPath
    $inputFile = Get-Item -LiteralPath $InputPath -Force
    $root = $inputFile.Directory
    $writers = @('S-1-5-18','S-1-5-32-544',$operatorSid)
    for ($entry = $inputFile; $null -ne $entry; $entry = $(if ($entry -is [IO.FileInfo]) {$entry.Directory} else {$entry.Parent})) {
        if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'observer_task_input_path' }
        $acl = Get-Acl -LiteralPath $entry.FullName
        $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        $trustedRoot = $entry.FullName -ceq 'C:\' -and $owner -ceq 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
        if ($owner -cnotin $writers -and -not $trustedRoot) { throw 'observer_task_input_acl' }
        $mask = if ($entry -is [IO.FileInfo] -or $entry.FullName -ceq $root.FullName) {1343029590} else {269287488}
        foreach ($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
            if ($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Value -cnotin $writers -and
                -not ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -and
                ($rule.FileSystemRights -band $mask)) { throw 'observer_task_input_acl' }
        }
    }
    if ($inputFile.Length -lt 1 -or $inputFile.Length -gt 8192) { throw 'observer_task_input_size' }
    $raw = [IO.File]::ReadAllBytes($InputPath)
    if ($raw.Length -gt 8192 -or (Digest $raw) -cne $InputSha256) { throw 'observer_task_input_pin' }
    $config = ConvertFrom-Json -InputObject ([Text.UTF8Encoding]::new($false,$true).GetString($raw))
    $expected = @('server_path','server_sha256','arguments')
    if (@($config.PSObject.Properties).Count -ne $expected.Count -or
        @($config.PSObject.Properties.Name | Where-Object {$_ -cnotin $expected}).Count -ne 0) { throw 'observer_task_input' }
    if ($config.server_path -isnot [string] -or $config.server_sha256 -isnot [string]) { throw 'observer_task_server_scope' }
    Assert-LocalPath $config.server_path
    if ([IO.Path]::GetDirectoryName($config.server_path) -cne $root.FullName -or
        $config.server_sha256 -cnotmatch '\A[a-f0-9]{64}\z') { throw 'observer_task_server_scope' }
    $argsMap = $config.arguments
    $pins = @('PlanSha256','AttemptId','WindowsBootSha256','WslBootSha256','CollectorSha256','ReactivationPlanSha256',
        'MarkerSha256','CallbackSha256','CallbackConfigSha256','KeySha256')
    $paths = @('CallbackPath','CallbackConfigPath','KeyPath','ReadyPath')
    $required = $pins + $paths + @('PipeName','OperatorSid','ServiceSid','Generation')
    if (@($argsMap.PSObject.Properties).Count -ne $required.Count -or
        @($argsMap.PSObject.Properties.Name | Where-Object {$_ -cnotin $required}).Count -ne 0 -or
        $argsMap.OperatorSid -isnot [string] -or $argsMap.ServiceSid -isnot [string] -or $argsMap.PipeName -isnot [string] -or
        ($argsMap.Generation -isnot [int] -and $argsMap.Generation -isnot [long]) -or
        $argsMap.OperatorSid -cne $operatorSid -or $argsMap.Generation -ne 1 -or
        $argsMap.ServiceSid -cnotmatch '\AS-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}\z' -or
        $argsMap.PipeName -cnotmatch '\An100ha-lease-proof-[a-f0-9]{32}\z' -or
        $argsMap.PlanSha256 -cne $argsMap.ReactivationPlanSha256) { throw 'observer_task_arguments' }
    foreach ($name in $pins) {
        if ($argsMap.$name -isnot [string] -or $argsMap.$name -cnotmatch '\A[a-f0-9]{64}\z') { throw 'observer_task_arguments' }
    }
    foreach ($name in $paths) {
        if ($argsMap.$name -isnot [string]) { throw 'observer_task_arguments' }
        Assert-LocalPath $argsMap.$name
        if ([IO.Path]::GetDirectoryName($argsMap.$name) -cne $root.FullName) { throw 'observer_task_arguments' }
    }
    if ([IO.Path]::GetFileName($argsMap.ReadyPath) -cne ('lease-observer-ready-' + $argsMap.AttemptId + '.json') -or
        (Test-Path -LiteralPath $argsMap.ReadyPath)) { throw 'observer_task_ready_exists' }
    $server = Get-Item -LiteralPath $config.server_path -Force
    if ($server -isnot [IO.FileInfo] -or ($server.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        $server.Length -gt 65536 -or (Digest ([IO.File]::ReadAllBytes($server.FullName))) -cne $config.server_sha256) {
        throw 'observer_task_server_pin'
    }
    $taskName = 'N100HA_lease_observer_' + $argsMap.AttemptId
    if (Get-ScheduledTask -TaskPath '\' -TaskName $taskName -ErrorAction SilentlyContinue) { throw 'observer_task_exists' }
    $engine = Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'
    # The delayed task loads exactly the bytes pinned by the protected input.
    $command = '$ErrorActionPreference="Stop";$p="' + $server.FullName + '";' +
        '$f=Get-Item -LiteralPath $p -Force;if($f -isnot [IO.FileInfo] -or ($f.Attributes -band 1024) -or $f.Length -gt 65536){exit 1};' +
        '$b=[IO.File]::ReadAllBytes($p);$h=[Security.Cryptography.SHA256]::Create();try{$s=[BitConverter]::ToString($h.ComputeHash($b)).Replace("-","").ToLowerInvariant()}finally{$h.Dispose()};' +
        'if($s -cne "' + $config.server_sha256 + '"){exit 1}; & ([ScriptBlock]::Create([Text.UTF8Encoding]::new($false,$true).GetString($b)))'
    foreach ($name in $required) { $command += ' -' + $name + " '" + $argsMap.$name + "'" }
    $command += ' -Go'
    $argument = '-NoLogo -NoProfile -NonInteractive -EncodedCommand ' + [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    $scheduler = New-Object -ComObject 'Schedule.Service'
    $scheduler.Connect()
    $folder = $scheduler.GetFolder('\')
    $definition = $scheduler.NewTask(0)
    $definition.Principal.UserId = $operatorName
    $definition.Principal.LogonType = 2 # S4U
    $definition.Principal.RunLevel = 1 # Highest
    $definition.Settings.ExecutionTimeLimit = 'PT0S'
    $definition.Settings.MultipleInstances = 2 # IgnoreNew
    $definition.Settings.DisallowStartIfOnBatteries = $false
    $definition.Settings.StopIfGoingOnBatteries = $false
    $action = $definition.Actions.Create(0) # Exec
    $action.Path = $engine
    $action.Arguments = $argument
    # TASK_CREATE fails if another caller has already created this task name.
    $owned = $folder.RegisterTaskDefinition($taskName,$definition,2,$operatorName,$null,2,$null)
    $registered = $owned.Definition
    $registeredUser = $registered.Principal.UserId
    $registeredSid = if ($registeredUser -match '^S-1-') { $registeredUser } else {
        ([Security.Principal.NTAccount]::new($registeredUser)).Translate([Security.Principal.SecurityIdentifier]).Value
    }
    if ($registered.Actions.Count -ne 1 -or $registered.Triggers.Count -ne 0 -or
        $registeredSid -cne $operatorSid -or $registered.Principal.LogonType -ne 2 -or
        $registered.Principal.RunLevel -ne 1 -or [Xml.XmlConvert]::ToTimeSpan($registered.Settings.ExecutionTimeLimit).Ticks -ne 0 -or
        $registered.Settings.RestartCount -ne 0 -or $registered.Settings.MultipleInstances -ne 2 -or
        $registered.Settings.DisallowStartIfOnBatteries -or $registered.Settings.StopIfGoingOnBatteries) {
        throw 'observer_task_registration_changed'
    }
    $registeredAction = $registered.Actions.Item(1)
    if ($registeredAction.Type -ne 0 -or $registeredAction.Path -cne $engine -or $registeredAction.Arguments -cne $argument) {
        throw 'observer_task_registration_changed'
    }
    $running = $owned.Run($null)
    $instanceGuid = $running.InstanceGuid
    if ([guid]$instanceGuid -eq [guid]::Empty) { throw 'observer_task_instance_unconfirmed' }
    $watch = [Diagnostics.Stopwatch]::StartNew()
    while ($watch.ElapsedMilliseconds -lt 15000) {
        if ([IO.File]::Exists($argsMap.ReadyPath)) {
            $ready = Get-Item -LiteralPath $argsMap.ReadyPath -Force
            if ($ready.Length -lt 1 -or $ready.Length -gt 1024 -or ($ready.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw 'observer_task_ready_unconfirmed'
            }
            $record = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($ready.FullName))
            if ($record.scope -cne 'n100ha-legacy-closure-v1' -or $record.server_sid -cne $operatorSid -or $record.pid -lt 1 -or
                $record.attempt_id -cne $argsMap.AttemptId -or $record.pipe_name -cne $argsMap.PipeName -or
                $record.plan_sha256 -cne $argsMap.PlanSha256 -or $record.callback_config_sha256 -cne $argsMap.CallbackConfigSha256) {
                throw 'observer_task_ready_unconfirmed'
            }
            $process = Get-Process -Id $record.pid
            try {
                $instance = @($owned.GetInstances(0))
                $nativeProcess = Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$record.pid) -OperationTimeoutSec 5
                $nativeOwner = Invoke-CimMethod -InputObject $nativeProcess -MethodName GetOwnerSid
                if ($process.HasExited -or $process.SessionId -ne 0 -or $process.MainModule.FileName -ine $engine -or
                    $process.StartTime.ToFileTimeUtc() -ne $record.creation_filetime -or
                    $instance.Count -ne 1 -or $instance[0].InstanceGuid -ne $instanceGuid -or
                    $instance[0].State -ne 4 -or $nativeOwner.ReturnValue -ne 0 -or
                    $nativeOwner.Sid -cne $operatorSid -or
                    -not $nativeProcess.CommandLine.EndsWith((' ' + $argument),[StringComparison]::Ordinal)) {
                    throw 'observer_task_ready_unconfirmed'
                }
                [ordered]@{observer_ready=$true;observer_pid=$record.pid}|ConvertTo-Json -Compress
                return
            } finally { $process.Dispose() }
        }
        Start-Sleep -Milliseconds 200
    }
    throw 'observer_task_ready_unconfirmed'
} catch { exit 1 }
