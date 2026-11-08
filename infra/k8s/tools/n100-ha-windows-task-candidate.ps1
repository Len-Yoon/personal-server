# Disconnected candidate. Run ONLY inside an externally bounded diagnostic child.
# COM RPC cannot be cancelled by a Stopwatch; the coordinator must enforce the
# wall-clock limit and reap only its own diagnostic process. Never a guard proof.
# Microsoft API references: https://learn.microsoft.com/en-us/windows/win32/taskschd/taskfolder-gettasks
# https://learn.microsoft.com/en-us/windows/win32/taskschd/taskfolder-getfolders
$ErrorActionPreference='Stop'
$WarningPreference='SilentlyContinue'
$ProgressPreference='SilentlyContinue'
Set-StrictMode -Version Latest

function Assert-HaTaskCandidateDeadline($Watch,[int]$DeadlineMilliseconds) {
    if($Watch.Elapsed.TotalMilliseconds -ge $DeadlineMilliseconds){throw 'candidate_deadline'}
}

function Add-HaTaskCandidateCharacters([string]$Text,$Budget,[int]$IndividualLimit=65536) {
    # Aggregate UTF-16 character cap bounds retained metadata to about 4 MiB,
    # independent of the task count. A provider property may allocate its own
    # return value before we can inspect it; the external watchdog remains vital.
    if($Text.Length -gt $IndividualLimit -or $Budget.characters + $Text.Length -gt 2097152){throw 'candidate_unsupported'}
    $Budget.characters+=$Text.Length
}

function ConvertFrom-HaTaskCandidate($Task,[string]$FolderPath,$Watch=([Diagnostics.Stopwatch]::StartNew()),[int]$DeadlineMilliseconds=2000,$Budget=(@{characters=0;actions=0})) {
    # These are precisely the properties consumed by the original task loop.
    # Xml is retained verbatim; native Export-ScheduledTask byte equivalence is
    # NOT established by this projection and remains a separate acceptance gate.
    Assert-HaTaskCandidateDeadline $Watch $DeadlineMilliseconds
    $name=[string]$Task.Name;$xml=[string]$Task.Xml
    Add-HaTaskCandidateCharacters $name $Budget
    Add-HaTaskCandidateCharacters $xml $Budget 1048576
    Add-HaTaskCandidateCharacters $FolderPath $Budget
    $states=@('Unknown','Disabled','Queued','Ready','Running')
    $state=[int]$Task.State
    if($state -lt 0 -or $state -ge $states.Count){throw 'candidate_unsupported'}
    $definition=$Task.Definition
    $enabled=$definition.Settings.Enabled
    if($enabled -isnot [bool]){throw 'candidate_unsupported'}
    $actions=@()
    foreach($action in $definition.Actions){
        Assert-HaTaskCandidateDeadline $Watch $DeadlineMilliseconds
        if($actions.Count -ge 1024 -or $Budget.actions -ge 4096){throw 'candidate_unsupported'}
        $Budget.actions++
        switch([int]$action.Type){
            0 {
                $execute=[string]$action.Path;$arguments=[string]$action.Arguments;$workingDirectory=[string]$action.WorkingDirectory
                foreach($text in @($execute,$arguments,$workingDirectory)){Add-HaTaskCandidateCharacters $text $Budget}
                $actions+=,[pscustomobject]@{Execute=$execute;Arguments=$arguments;WorkingDirectory=$workingDirectory}
            }
            5 {$classId=[string]$action.ClassId;Add-HaTaskCandidateCharacters $classId $Budget;$actions+=,[pscustomobject]@{ClassId=$classId}}
            default {throw 'candidate_unsupported'}
        }
        Assert-HaTaskCandidateDeadline $Watch $DeadlineMilliseconds
    }
    if($actions.Count -gt 1024){throw 'candidate_unsupported'}
    if(-not $FolderPath.EndsWith('\')){$FolderPath+='\'}
    if([string]::IsNullOrEmpty($name) -or [string]::IsNullOrEmpty($xml) -or $actions.Count -eq 0 -or $xml.Length -gt 1048576){throw 'candidate_unsupported'}
    return [pscustomobject]@{TaskName=$name;TaskPath=$FolderPath;State=$states[$state];Settings=[pscustomobject]@{Enabled=$enabled};Actions=$actions;CandidateXml=$xml}
}

function Get-HaTaskCandidateRecords($Root,$Watch,[int]$DeadlineMilliseconds=2000) {
    $budget=@{characters=0;actions=0}
    $pending=[Collections.Generic.Queue[object]]::new();$pending.Enqueue($Root)
    $records=[Collections.Generic.List[object]]::new();$taskKeys=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase);$seen=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    while($pending.Count){
        Assert-HaTaskCandidateDeadline $Watch $DeadlineMilliseconds
        if($seen.Count -ge 4096){throw 'candidate_unsupported'}
        $folder=$pending.Dequeue();$path=[string]$folder.Path
        Add-HaTaskCandidateCharacters $path $budget
        if(-not $path.StartsWith('\') -or -not $seen.Add($path)){throw 'candidate_unsupported'}
        # TASK_ENUM_HIDDEN=1; no enabled/state/name/path filtering.
        $items=$folder.GetTasks(1)
        Assert-HaTaskCandidateDeadline $Watch $DeadlineMilliseconds
        foreach($item in $items){
            Assert-HaTaskCandidateDeadline $Watch $DeadlineMilliseconds
            $record=ConvertFrom-HaTaskCandidate $item $path $Watch $DeadlineMilliseconds $budget
            if($records.Count -ge 4096 -or -not $taskKeys.Add($record.TaskPath+$record.TaskName)){throw 'candidate_unsupported'}
            $records.Add($record)
            Assert-HaTaskCandidateDeadline $Watch $DeadlineMilliseconds
        }
        # GetFolders requires reserved flags=0; errors are never skipped.
        $children=$folder.GetFolders(0)
        Assert-HaTaskCandidateDeadline $Watch $DeadlineMilliseconds
        foreach($child in $children){
            Assert-HaTaskCandidateDeadline $Watch $DeadlineMilliseconds
            if($pending.Count -ge 4096){throw 'candidate_unsupported'}
            $pending.Enqueue($child)
        }
    }
    $sorted=@($records.ToArray() | Sort-Object TaskPath,TaskName -CaseSensitive)
    Assert-HaTaskCandidateDeadline $Watch $DeadlineMilliseconds
    return $sorted
}

function Invoke-HaTaskCandidate {
    $watch=[Diagnostics.Stopwatch]::StartNew();$count=$null;$actions=$null
    $status='blocked';$reason='query_failed';$service=$null;$root=$null;$complete=$false
    try {
        $service=New-Object -ComObject 'Schedule.Service'
        # No remote host, user, domain or password. Local read-only connection.
        $service.Connect()
        Assert-HaTaskCandidateDeadline $watch 2000
        $root=$service.GetFolder('\')
        $records=@(Get-HaTaskCandidateRecords $root $watch 2000)
        Assert-HaTaskCandidateDeadline $watch 2000
        $count=$records.Count;$actions=0
        foreach($record in $records){
            Assert-HaTaskCandidateDeadline $watch 2000
            $actions+=@($record.Actions).Count
        }
        Assert-HaTaskCandidateDeadline $watch 2000
        $complete=$true
    } catch {
        # Provider paths, task XML, commands, principal and exception text stay local.
        if($_.Exception.Message -ceq 'candidate_deadline'){$reason='deadline_exceeded'}
        elseif($_.Exception.Message -ceq 'candidate_unsupported'){$reason='unsupported_metadata'}
    } finally {
        $records=$null
        # elapsed_ms includes COM cleanup, not JSON serialization/transport.
        try {
            foreach($com in @($root,$service)){
                if($null -ne $com -and [Runtime.InteropServices.Marshal]::IsComObject($com)){
                    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($com)
                }
            }
        } catch {$complete=$false;$reason='query_failed'}
        $watch.Stop()
    }
    if($complete){
        try {
            Assert-HaTaskCandidateDeadline $watch 2000
            $status='measured';$reason='none'
        } catch {$reason='deadline_exceeded'}
    }
    if($status -ne 'measured'){$count=$null;$actions=$null}
    return [ordered]@{schema_version=1;kind='ha-windows-task-candidate';authority=$false;ready=$false;
        scope='local-task-enumeration-only';status=$status;reason=$reason;equivalence='unknown';
        deadline_ms=2000;deadline_mode='cooperative-external-watchdog-required';
        task_count=$count;action_count=$actions;elapsed_ms=$watch.Elapsed.TotalMilliseconds}
}

if($MyInvocation.InvocationName -cne '.'){
    # No native execution without an external coordinator: dot-source the exact
    # candidate bytes inside its bounded child, then invoke the function there.
    [Console]::Out.WriteLine('{"schema_version":1,"kind":"ha-windows-task-candidate","authority":false,"ready":false,"status":"blocked","reason":"external_watchdog_required","equivalence":"unknown"}')
    exit 1
}
