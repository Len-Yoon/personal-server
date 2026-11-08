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

function Assert-HaTaskCandidateXmlNamespace($Document,$Watch,[int]$DeadlineMilliseconds) {
    Assert-HaTaskCandidateDeadline $Watch $DeadlineMilliseconds
    $nodes=$Document.GetElementsByTagName('*')
    if($nodes.Count -gt 32768){throw 'candidate_unsupported'}
    # The fixed XPath checks every descendant element, including unrelated task
    # metadata. Run it in .NET instead of one PowerShell call per XML element.
    $foreign=$Document.SelectSingleNode("//*[namespace-uri() != 'http://schemas.microsoft.com/windows/2004/02/mit/task']")
    Assert-HaTaskCandidateDeadline $Watch $DeadlineMilliseconds
    if($null -ne $foreign){throw 'candidate_unsupported'}
}

function Read-HaTaskCandidateXml([string]$Xml,$Watch,[int]$DeadlineMilliseconds,$Budget) {
    # XSD defaults/mapping: https://learn.microsoft.com/en-us/windows/win32/taskschd/task-scheduler-schema
    # This is a conservative projection, not full XSD/native equivalence proof.
    # Settings/Enabled is optional with default true in Microsoft's complete XSD.
    $reader=$null;$textReader=$null
    try {
        $settings=[Xml.XmlReaderSettings]::new()
        $settings.DtdProcessing=[Xml.DtdProcessing]::Prohibit
        $settings.XmlResolver=$null
        $settings.MaxCharactersInDocument=1048576
        $settings.MaxCharactersFromEntities=0
        $textReader=[IO.StringReader]::new($Xml)
        $reader=[Xml.XmlReader]::Create($textReader,$settings)
        $document=[Xml.XmlDocument]::new();$document.XmlResolver=$null;$document.PreserveWhitespace=$true
        $document.Load($reader)
    } catch {throw 'candidate_unsupported'}
    finally {if($null -ne $reader){$reader.Dispose()};if($null -ne $textReader){$textReader.Dispose()}}
    Assert-HaTaskCandidateDeadline $Watch $DeadlineMilliseconds
    $ns='http://schemas.microsoft.com/windows/2004/02/mit/task'
    $root=$document.DocumentElement
    if($null -eq $root -or $root.LocalName -cne 'Task' -or $root.NamespaceURI -cne $ns){throw 'candidate_unsupported'}
    Assert-HaTaskCandidateXmlNamespace $document $Watch $DeadlineMilliseconds
    $manager=[Xml.XmlNamespaceManager]::new($document.NameTable);$manager.AddNamespace('t',$ns)
    $sections=@{}
    foreach($node in $root.ChildNodes){
        if($node -is [Xml.XmlElement]){
            if($node.LocalName -cnotin @('RegistrationInfo','Triggers','Principals','Settings','Data','Actions') -or $sections.ContainsKey($node.LocalName)){throw 'candidate_unsupported'}
            $sections[$node.LocalName]=$true
        } elseif(($node -is [Xml.XmlText] -or $node -is [Xml.XmlCDataSection]) -and -not [string]::IsNullOrWhiteSpace($node.Value)){throw 'candidate_unsupported'}
    }
    if($root.SelectNodes('t:Settings',$manager).Count -gt 1){throw 'candidate_unsupported'}
    $enabledNodes=$root.SelectNodes('t:Settings/t:Enabled',$manager)
    if($enabledNodes.Count -gt 1){throw 'candidate_unsupported'}
    $enabled=$true
    if($enabledNodes.Count -eq 1){
        if($enabledNodes[0].SelectNodes('*').Count){throw 'candidate_unsupported'}
        try {$enabled=[Xml.XmlConvert]::ToBoolean($enabledNodes[0].InnerText.Trim())}catch {throw 'candidate_unsupported'}
    }
    $containers=$root.SelectNodes('t:Actions',$manager)
    if($containers.Count -ne 1){throw 'candidate_unsupported'}
    $actions=@()
    foreach($action in $containers[0].ChildNodes){
        Assert-HaTaskCandidateDeadline $Watch $DeadlineMilliseconds
        if($action -isnot [Xml.XmlElement]){
            if(($action -is [Xml.XmlText] -or $action -is [Xml.XmlCDataSection]) -and -not [string]::IsNullOrWhiteSpace($action.Value)){throw 'candidate_unsupported'}
            continue
        }
        if($actions.Count -ge 1024 -or $Budget.actions -ge 4096){throw 'candidate_unsupported'}
        $Budget.actions++
        switch -CaseSensitive ($action.LocalName){
            'Exec' {$allowed=@('Command','Arguments','WorkingDirectory');$required='Command'}
            'ComHandler' {$allowed=@('ClassId','Data');$required='ClassId'}
            default {throw 'candidate_unsupported'}
        }
        $fields=@{}
        foreach($field in $action.ChildNodes){
            if($field -isnot [Xml.XmlElement]){
                if(($field -is [Xml.XmlText] -or $field -is [Xml.XmlCDataSection]) -and -not [string]::IsNullOrWhiteSpace($field.Value)){throw 'candidate_unsupported'}
                continue
            }
            if($field.LocalName -cnotin $allowed -or $fields.ContainsKey($field.LocalName) -or $field.SelectNodes('*').Count){throw 'candidate_unsupported'}
            $value=$field.InnerText
            Add-HaTaskCandidateCharacters $value $Budget
            $fields[$field.LocalName]=$value
        }
        if(-not $fields.ContainsKey($required) -or [string]::IsNullOrEmpty($fields[$required])){throw 'candidate_unsupported'}
        if($action.LocalName -ceq 'Exec'){
            $arguments='';$workingDirectory=''
            if($fields.ContainsKey('Arguments')){$arguments=$fields['Arguments']}
            if($fields.ContainsKey('WorkingDirectory')){$workingDirectory=$fields['WorkingDirectory']}
            $actions+=,[pscustomobject]@{Execute=$fields['Command'];Arguments=$arguments;WorkingDirectory=$workingDirectory}
        } else {$actions+=,[pscustomobject]@{ClassId=$fields['ClassId']}}
        Assert-HaTaskCandidateDeadline $Watch $DeadlineMilliseconds
    }
    if($actions.Count -eq 0){throw 'candidate_unsupported'}
    return @{Enabled=$enabled;Actions=$actions}
}

function ConvertFrom-HaTaskCandidate($Task,[string]$FolderPath,$Watch=([Diagnostics.Stopwatch]::StartNew()),[int]$DeadlineMilliseconds=2000,$Budget=(@{characters=0;actions=0})) {
    # These are precisely the properties consumed by the original task loop.
    # Xml is retained verbatim; native Export-ScheduledTask byte equivalence is
    # NOT established by this projection and remains a separate acceptance gate.
    Assert-HaTaskCandidateDeadline $Watch $DeadlineMilliseconds
    $name=[string]$Task.Name;$registeredPath=[string]$Task.Path
    # Compare the registered identity before retaining any task payload. A task
    # moved/renamed during enumeration must not inherit its old folder identity.
    if([string]::IsNullOrWhiteSpace($name) -or $name -in @('.','..') -or
        $name -match '[\\/\x00-\x1f]' -or -not $FolderPath.StartsWith('\') -or
        $FolderPath -match '[/\x00-\x1f]' -or $FolderPath.Contains('\\') -or
        @($FolderPath.Split('\') | Where-Object {$_ -in @('.','..')}).Count -gt 0){throw 'candidate_unsupported'}
    if(-not $FolderPath.EndsWith('\')){$FolderPath+='\'}
    if(-not [StringComparer]::OrdinalIgnoreCase.Equals($registeredPath,($FolderPath+$name))){throw 'candidate_unsupported'}
    $xml=[string]$Task.Xml
    Add-HaTaskCandidateCharacters $name $Budget
    Add-HaTaskCandidateCharacters $registeredPath $Budget
    Add-HaTaskCandidateCharacters $xml $Budget 1048576
    Add-HaTaskCandidateCharacters $FolderPath $Budget
    $states=@('Unknown','Disabled','Queued','Ready','Running')
    $state=[int]$Task.State
    if($state -lt 0 -or $state -ge $states.Count){throw 'candidate_unsupported'}
    # Exactly one Xml read replaces Definition/Settings/action-property RPCs.
    $projection=Read-HaTaskCandidateXml $xml $Watch $DeadlineMilliseconds $Budget
    $enabled=$projection.Enabled;$actions=$projection.Actions
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
