# Component enumeration only. Never load the guard or produce an authority proof.
$ErrorActionPreference='Stop'
$WarningPreference='SilentlyContinue'
$ProgressPreference='SilentlyContinue'
Set-StrictMode -Version Latest

function Get-HaDiagnosticQueries([string]$Source, [string]$ExpectedSha256) {
    $sha=[Security.Cryptography.SHA256]::Create()
    try {$actual=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Source))).Replace('-','').ToLowerInvariant()}
    finally {$sha.Dispose()}
    if ($ExpectedSha256 -cnotmatch '^[0-9a-f]{64}$' -or $actual -cne $ExpectedSha256) {throw 'source_rejected'}
    $tokens=$null;$errors=$null
    $ast=[Management.Automation.Language.Parser]::ParseInput($Source,[ref]$tokens,[ref]$errors)
    if ($errors.Count) {throw 'source_rejected'}
    $functions=@($ast.FindAll({param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -ceq 'Get-LegacyWindowsObservation'},$true))
    if ($functions.Count -ne 1) {throw 'source_rejected'}
    $expected=[ordered]@{
        services='Get-CimInstance Win32_Service -ErrorAction Stop | Sort-Object Name -CaseSensitive'
        tasks='Get-ScheduledTask -ErrorAction Stop | Sort-Object TaskPath,TaskName -CaseSensitive'
        processes='Get-CimInstance Win32_Process -ErrorAction Stop | Sort-Object ProcessId'
    }
    $queries=[ordered]@{}
    foreach ($name in $expected.Keys) {
        $wanted=$expected[$name]
        $matches=@($functions[0].FindAll({param($a) $a -is [Management.Automation.Language.PipelineAst] -and $a.Extent.Text -ceq $wanted},$true))
        if ($matches.Count -ne 1) {throw 'source_rejected'}
        # Only these literal read pipelines execute; not the containing function,
        # its caller identity checks, Add-Type, or any other source statement.
        $queries[$name]=[ScriptBlock]::Create($matches[0].Extent.Text)
    }
    return $queries
}

function Invoke-HaEnumerationDiagnostic([string]$Source, [string]$ExpectedSha256) {
    $queries=Get-HaDiagnosticQueries $Source $ExpectedSha256
    $rows=@();$status='measured'
    foreach ($name in $queries.Keys) {
        $watch=[Diagnostics.Stopwatch]::StartNew()
        $count=$null;$reason='none';$phaseStatus='measured'
        try {$items=@(& $queries[$name]);$count=$items.Count}
        catch {$phaseStatus='blocked';$reason='query_failed';$status='partial'}
        finally {$watch.Stop()}
        $rows+=@{phase=$name;status=$phaseStatus;reason=$reason;count=$count;elapsed_ms=$watch.Elapsed.TotalMilliseconds}
        $items=$null
    }
    return @{schema_version=1;kind='ha-enumeration-diagnostic';authority=$false;ready=$false;
        scope='windows-enumeration-only';source_sha256=$ExpectedSha256;status=$status;phases=$rows}
}

if ($MyInvocation.InvocationName -cne '.') {
    try {
        $buffer=New-Object char[] 262145
        $length=[Console]::In.ReadBlock($buffer,0,$buffer.Length)
        if ($length -gt 262144) {throw 'input_rejected'}
        $request=(-join $buffer[0..($length-1)]) | ConvertFrom-Json
        if (@($request.PSObject.Properties).Count -ne 2 -or
            $null -eq $request.PSObject.Properties['source'] -or $null -eq $request.PSObject.Properties['source_sha256']) {throw 'input_rejected'}
        $result=Invoke-HaEnumerationDiagnostic ([string]$request.source) ([string]$request.source_sha256)
        $result | ConvertTo-Json -Depth 5 -Compress
    } catch {
        # Never print native messages, paths, provider records, or source bytes.
        [Console]::Out.WriteLine('{"status":"blocked","reason":"diagnostic_input_rejected"}')
        exit 1
    }
}
