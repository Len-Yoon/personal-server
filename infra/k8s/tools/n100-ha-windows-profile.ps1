# Disconnected read components only; no observation/guard/reader function loads.
$ErrorActionPreference='Stop'
$WarningPreference='SilentlyContinue'
$ProgressPreference='SilentlyContinue'
Set-StrictMode -Version Latest

function Get-HaProfileParts([string]$Source,[string]$ExpectedSha256) {
    $approved='f07a7cf479aba1bbab238a8b3a7a7db4816145ecc71d1603348e85925d6d55b9'
    $sha=[Security.Cryptography.SHA256]::Create()
    try {$actual=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Source))).Replace('-','').ToLowerInvariant()}
    finally {$sha.Dispose()}
    if($ExpectedSha256 -cne $approved -or $actual -cne $approved){throw 'source_rejected'}
    $tokens=$null;$errors=$null
    $ast=[Management.Automation.Language.Parser]::ParseInput($Source,[ref]$tokens,[ref]$errors)
    if($errors.Count){throw 'source_rejected'}
    $allowed=@('Get-LegacyHash','Get-LegacyObjectHash','Get-LegacyCodePath','Get-LegacyCodeHash',
        'Test-LegacyProviderCode','Get-LegacyRegistry','Test-LegacyCreator','Expand-LegacyCommand',
        'Get-LegacyReferenceHashes','Assert-LegacyDeadline')
    $definitions=@()
    foreach($name in $allowed){
        $found=@($ast.FindAll({param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -ceq $name},$true))
        if($found.Count -ne 1){throw 'source_rejected'}
        $definitions+= $found[0].Extent.Text
    }
    $observation=@($ast.FindAll({param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -ceq 'Get-LegacyWindowsObservation'},$true))
    if($observation.Count -ne 1){throw 'source_rejected'}
    $queries=[ordered]@{
        services='Get-CimInstance Win32_Service -ErrorAction Stop | Sort-Object Name -CaseSensitive'
        tasks='Get-ScheduledTask -ErrorAction Stop | Sort-Object TaskPath,TaskName -CaseSensitive'
        processes='Get-CimInstance Win32_Process -ErrorAction Stop | Sort-Object ProcessId'
    }
    $loops=[ordered]@{}
    foreach($name in $queries.Keys){
        $wanted='('+$queries[$name]+')'
        $found=@($observation[0].FindAll({param($a) $a -is [Management.Automation.Language.ForEachStatementAst] -and $a.Condition.Extent.Text -ceq $wanted},$true))
        if($found.Count -ne 1){throw 'source_rejected'}
        $loops[$name]=@{variable=$found[0].Variable.Extent.Text;body=$found[0].Body.Extent.Text}
    }
    $statements=@($observation[0].Body.EndBlock.Statements)
    $start=@($statements|Where-Object {$_.Extent.Text -ceq '$startup = [ordered]@{}'})
    $end=@($statements|Where-Object {$_.Extent.Text -ceq '$principal = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value'})
    if($start.Count -ne 1 -or $end.Count -ne 1){throw 'source_rejected'}
    $startup=$Source.Substring($start[0].Extent.StartOffset,$end[0].Extent.StartOffset-$start[0].Extent.StartOffset)
    # Only the pinned, fixed interop type declaration; never source top-level code.
    $type=@($ast.EndBlock.Statements|Where-Object {$_.Extent.Text.StartsWith('Add-Type -TypeDefinition')})
    if($type.Count -ne 1){throw 'source_rejected'}
    return @{definitions=$definitions;queries=$queries;loops=$loops;startup=$startup;argv=$type[0].Extent.Text}
}

function Invoke-HaProfileCall([string]$Phase,[scriptblock]$Call) {
    $row=$script:profileRows[$Phase];$row.calls++
    $watch=[Diagnostics.Stopwatch]::StartNew()
    try {$result=& $Call;if($row.status -ne 'blocked'){$row.status='measured';$row.count++};return $result}
    catch {$row.status='blocked';$row.reason='query_failed';$row.count=$null;throw}
    finally {$watch.Stop();$row.elapsed_ms+=$watch.Elapsed.TotalMilliseconds}
}

function Write-HaProfileProgress([string]$Phase,[string]$Pin) {
    $report=@{schema_version=1;kind='ha-windows-component-diagnostic';authority=$false;ready=$false;
        source_sha256=$Pin;status='progress';phases=@($script:profileRows.Values)}
    [Console]::Out.WriteLine((@{event='progress';active_phase=$Phase;report=$report}|ConvertTo-Json -Depth 7 -Compress))
    [Console]::Out.Flush()
}

function Invoke-HaWindowsProfile([string]$Source,[string]$ExpectedSha256,[bool]$StreamProgress=$false) {
    $parts=Get-HaProfileParts $Source $ExpectedSha256
    foreach($definition in $parts.definitions){
        # Preserve the exact body; the registry wrapper also measures recursion.
        if($definition.StartsWith('function Get-LegacyRegistry(')){
            $definition=$definition.Replace('function Get-LegacyRegistry(','function Get-HaOriginalRegistry(')
        }
        . ([ScriptBlock]::Create($definition))
    }
    if(-not ('LegacyArgv' -as [type])){. ([ScriptBlock]::Create($parts.argv))}
    $script:profileRows=[ordered]@{}
    foreach($name in @('services_query','tasks_query','processes_query','services','tasks','processes','startup','registry','hash','signature','task_xml')){
        $script:profileRows[$name]=@{phase=$name;calls=0;count=0;elapsed_ms=0.0;status='not_run';reason='none'}
    }
    function Get-LegacyRegistry($Key,[int]$Depth=0){Invoke-HaProfileCall registry {Get-HaOriginalRegistry $Key $Depth}}
    function Get-FileHash($LiteralPath,$Algorithm,$ErrorAction){Invoke-HaProfileCall hash {Microsoft.PowerShell.Utility\Get-FileHash -LiteralPath $LiteralPath -Algorithm $Algorithm -ErrorAction Stop}}
    function Get-AuthenticodeSignature($LiteralPath,$ErrorAction){Invoke-HaProfileCall signature {Microsoft.PowerShell.Security\Get-AuthenticodeSignature -LiteralPath $LiteralPath -ErrorAction Stop}}
    function Export-ScheduledTask($TaskName,$TaskPath,$ErrorAction){Invoke-HaProfileCall task_xml {ScheduledTasks\Export-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction Stop}}
    $script:config=@{legacy_root='__diagnostic_unapproved_root__'}
    $env:ProgramFiles=[Environment]::GetFolderPath('ProgramFiles')
    $services=@();$tasks=@();$processes=@();$servicePids=@{};$serviceImages=@{};$serviceProviders=@{}
    $ownParent=$false
    $raw=[ordered]@{};$queryFailed=@{}
    foreach($name in $parts.queries.Keys){
        if($StreamProgress){Write-HaProfileProgress ($name+'_query') $ExpectedSha256}
        try {
            $query=[ScriptBlock]::Create($parts.queries[$name])
            $raw[$name]=@(Invoke-HaProfileCall ($name+'_query') $query)
            $script:profileRows[$name+'_query'].count=$raw[$name].Count
        } catch {$queryFailed[$name]=$true}
        if($StreamProgress){Write-HaProfileProgress ($name+'_query') $ExpectedSha256}
    }
    foreach($name in $parts.loops.Keys){
        if($StreamProgress){Write-HaProfileProgress $name $ExpectedSha256}
        if($queryFailed.ContainsKey($name)){
            if($StreamProgress){Write-HaProfileProgress $name $ExpectedSha256}
            continue
        }
        $Watch=[Diagnostics.Stopwatch]::StartNew()
        $component=[ScriptBlock]::Create('foreach ('+$parts.loops[$name].variable+' in $raw[$name]) '+$parts.loops[$name].body)
        try {
            # Dot-source retains service PID/image relationships for process reads.
            $timer=[Diagnostics.Stopwatch]::StartNew();$row=$script:profileRows[$name];$row.calls++
            . $component
            Assert-LegacyDeadline $Watch
            $row.status='measured';$row.count=$raw[$name].Count
        } catch {$row.status='blocked';$row.reason='query_failed';$row.count=$null}
        finally {$timer.Stop();$Watch.Stop();$row.elapsed_ms+=$timer.Elapsed.TotalMilliseconds}
        if($StreamProgress){Write-HaProfileProgress $name $ExpectedSha256}
    }
    if($StreamProgress){Write-HaProfileProgress startup $ExpectedSha256}
    $Watch=[Diagnostics.Stopwatch]::StartNew();$timer=[Diagnostics.Stopwatch]::StartNew()
    $row=$script:profileRows.startup;$row.calls++
    try {. ([ScriptBlock]::Create($parts.startup));Assert-LegacyDeadline $Watch;$row.status='measured';$row.count=$startup.Count}
    catch {$row.status='blocked';$row.reason='query_failed';$row.count=$null}
    finally {$timer.Stop();$Watch.Stop();$row.elapsed_ms+=$timer.Elapsed.TotalMilliseconds}
    if($StreamProgress){Write-HaProfileProgress startup $ExpectedSha256}
    $status='measured'
    foreach($row in $script:profileRows.Values){if($row.status -eq 'blocked'){$status='partial'}}
    # Nested elapsed metrics are inclusive and must never be summed.
    return @{schema_version=1;kind='ha-windows-component-diagnostic';authority=$false;ready=$false;
        source_sha256=$ExpectedSha256;status=$status;phases=@($script:profileRows.Values)}
}

if($MyInvocation.InvocationName -cne '.'){
    try {
        $buffer=New-Object char[] 262145;$length=[Console]::In.ReadBlock($buffer,0,$buffer.Length)
        if($length -lt 1 -or $length -gt 262144){throw 'input_rejected'}
        $request=(-join $buffer[0..($length-1)])|ConvertFrom-Json
        if(@($request.PSObject.Properties).Count -ne 2 -or $null -eq $request.PSObject.Properties['source'] -or $null -eq $request.PSObject.Properties['source_sha256']){throw 'input_rejected'}
        Invoke-HaWindowsProfile ([string]$request.source) ([string]$request.source_sha256) $true|ConvertTo-Json -Depth 6 -Compress
    } catch {[Console]::Out.WriteLine('{"status":"blocked","reason":"diagnostic_input_rejected"}');exit 1}
}
