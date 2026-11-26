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
    [switch]$Install,
    [switch]$SavedGuestsOnly,
    [ValidateSet('Normal','SavedOnly')]
    [string]$ReplaceModeFrom,
    [string]$PreviousScriptSha256
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$replaceMode = $PSBoundParameters.ContainsKey('ReplaceModeFrom')
if (($replaceMode -and ($PreviousScriptSha256 -cnotmatch '\A[a-f0-9]{64}\z' -or
      (($ReplaceModeFrom -ceq 'SavedOnly') -eq [bool]$SavedGuestsOnly))) -or
    (-not $replaceMode -and $PSBoundParameters.ContainsKey('PreviousScriptSha256'))) {
    throw 'n100_boot_task_mode_scope'
}
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
function New-BootTaskArguments([string]$SourceBase64,[bool]$SavedOnly) {
    $launch = $template -f $SourceBase64, $VmAIdSha256, $VmBIdSha256
    if ($SavedOnly) { $launch += ' -SavedGuestsOnly' }
    $payload = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($launch))
    $value = '-NoLogo -NoProfile -NonInteractive -EncodedCommand ' + $payload
    if ($value.Length -gt 24000) { throw 'n100_boot_task_action_too_large' }
    return $value
}
$arguments = New-BootTaskArguments $ScriptBase64 ([bool]$SavedGuestsOnly)

function Get-ExistingTask {
    Get-ScheduledTask -TaskPath $taskPath -ErrorAction Stop | Where-Object { $_.TaskName -ceq $name }
}

function Assert-Task($Task,[string]$ExpectedArguments=$arguments) {
    $actions = @($Task.Actions)
    $triggers = @($Task.Triggers)
    if ($actions.Count -ne 1 -or $actions[0].Execute -ine $engine -or $actions[0].Arguments -cne $ExpectedArguments -or
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

function Get-BootTaskPreviousArguments($Task) {
    $actions=@($Task.Actions)
    if($actions.Count -ne 1 -or $actions[0].Arguments -isnot [string]){throw 'n100_boot_task_mode_conflict'}
    $prefix='-NoLogo -NoProfile -NonInteractive -EncodedCommand '
    $actual=$actions[0].Arguments
    if(-not $actual.StartsWith($prefix,[StringComparison]::Ordinal) -or $actual.Length -gt 24000){throw 'n100_boot_task_mode_conflict'}
    $encoded=$actual.Substring($prefix.Length)
    if($encoded -cnotmatch '\A[A-Za-z0-9+/]+={0,2}\z'){throw 'n100_boot_task_mode_conflict'}
    $raw=[Convert]::FromBase64String($encoded)
    $launch=[Text.UnicodeEncoding]::new($false,$false,$true).GetString($raw)
    if([Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($launch)) -cne $encoded){throw 'n100_boot_task_mode_conflict'}
    $saved=$ReplaceModeFrom -ceq 'SavedOnly'
    $expected=$template -f '__PINNED_SCRIPT__',$VmAIdSha256,$VmBIdSha256
    if($saved){$expected+=' -SavedGuestsOnly'}
    $pattern='\A'+[regex]::Escape($expected).Replace('__PINNED_SCRIPT__','(?<source>[A-Za-z0-9+/]+={0,2})')+'\z'
    $match=[regex]::Match($launch,$pattern,[Text.RegularExpressions.RegexOptions]::CultureInvariant)
    if(-not $match.Success){throw 'n100_boot_task_mode_conflict'}
    $source=$match.Groups['source'].Value
    if($source.Length -gt 16384){throw 'n100_boot_task_mode_conflict'}
    $sourceBytes=[Convert]::FromBase64String($source)
    if([Convert]::ToBase64String($sourceBytes) -cne $source){throw 'n100_boot_task_mode_conflict'}
    $sha=[Security.Cryptography.SHA256]::Create()
    try{$pin=[BitConverter]::ToString($sha.ComputeHash($sourceBytes)).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
    if($pin -cne $PreviousScriptSha256){throw 'n100_boot_task_previous_script_unpinned'}
    $text=[Text.UTF8Encoding]::new($false,$true).GetString($sourceBytes)
    [void][ScriptBlock]::Create($text)
    $value=New-BootTaskArguments $source $saved
    Assert-Task $Task $value
    return $value
}

function Get-BootTaskXml {
    $text=Export-ScheduledTask -TaskName $name -TaskPath $taskPath -ErrorAction Stop
    $settings=[Xml.XmlReaderSettings]::new();$settings.DtdProcessing=[Xml.DtdProcessing]::Prohibit;$settings.XmlResolver=$null
    $reader=[Xml.XmlReader]::Create([IO.StringReader]::new([string]$text),$settings)
    try{
        $document=[Xml.XmlDocument]::new();$document.XmlResolver=$null;$document.PreserveWhitespace=$false;$document.Load($reader)
        if($document.DocumentElement.LocalName -cne 'Task' -or
           $document.DocumentElement.NamespaceURI -cne 'http://schemas.microsoft.com/windows/2004/02/mit/task'){throw 'n100_boot_task_xml_conflict'}
        return ,$document
    }finally{$reader.Dispose()}
}

function Get-BootTaskPreservedXml($Document) {
    $copy=$Document.CloneNode($true)
    $actions=@($copy.DocumentElement.ChildNodes|Where-Object {$_.LocalName -ceq 'Actions' -and $_.NamespaceURI -ceq $copy.DocumentElement.NamespaceURI})
    if($actions.Count -ne 1){throw 'n100_boot_task_xml_conflict'}
    $exec=@($actions[0].ChildNodes|Where-Object {$_.LocalName -ceq 'Exec' -and $_.NamespaceURI -ceq $copy.DocumentElement.NamespaceURI})
    if($exec.Count -ne 1 -or $actions[0].ChildNodes.Count -ne 1){throw 'n100_boot_task_xml_conflict'}
    $argumentNode=@($exec[0].ChildNodes|Where-Object {$_.LocalName -ceq 'Arguments' -and $_.NamespaceURI -ceq $copy.DocumentElement.NamespaceURI})
    if($argumentNode.Count -ne 1){throw 'n100_boot_task_xml_conflict'}
    # Mask only the one field this operation may change; retain action ID,
    # working directory, executable, principal, triggers and all settings.
    $argumentNode[0].InnerText='__N100HA_PINNED_ACTION_ARGUMENTS__'
    return $copy.OuterXml
}

function Update-BootTaskMode($Task) {
    $gate=[Threading.Mutex]::new($false,'Global\N100HA-Start-VM-Guests');$held=$false
    try{
        try{$held=$gate.WaitOne(0)}catch [Threading.AbandonedMutexException]{$held=$true;throw 'n100_boot_task_mode_uncertain'}
        if(-not $held){throw 'n100_boot_task_mode_busy'}
        $previousArguments=Get-BootTaskPreviousArguments $Task
        if([string]$Task.State -cne 'Ready'){throw 'n100_boot_task_mode_not_idle'}
        $before=Get-BootTaskXml
        $preserved=Get-BootTaskPreservedXml $before
        if(-not $Install){return 'ready_to_update_mode'}
        # Compare the complete definition again immediately before the limited
        # action-only update. The shared mutex also excludes VM save/resume/reboot.
        $current=@(Get-ExistingTask)
        if($current.Count -ne 1){throw 'n100_boot_task_mode_conflict'}
        Assert-Task $current[0] $previousArguments
        if([string]$current[0].State -cne 'Ready' -or (Get-BootTaskXml).OuterXml -cne $before.OuterXml){throw 'n100_boot_task_mode_conflict'}
        $action=$current[0].Actions[0]
        $action.Arguments=$arguments
        Set-ScheduledTask -TaskName $name -TaskPath $taskPath -Action $action -ErrorAction Stop | Out-Null
        $updated=@(Get-ExistingTask)
        if($updated.Count -ne 1){throw 'n100_boot_task_mode_update_unverified'}
        Assert-Task $updated[0]
        if((Get-BootTaskPreservedXml (Get-BootTaskXml)) -cne $preserved){throw 'n100_boot_task_mode_preservation_unverified'}
        return 'mode_updated'
    }finally{if($held){$gate.ReleaseMutex()};$gate.Dispose()}
}

$existing = @(Get-ExistingTask)
if ($existing.Count -gt 1) { throw 'n100_boot_task_definition_conflict' }
if ($replaceMode) {
    if($existing.Count -ne 1){throw 'n100_boot_task_mode_missing'}
    $result=Update-BootTaskMode $existing[0]
} elseif ($existing.Count -eq 1) {
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
