# Disconnected typed task diagnostic; external bounded child is mandatory.
$ErrorActionPreference='Stop'
$WarningPreference='SilentlyContinue'
$ProgressPreference='SilentlyContinue'
Set-StrictMode -Version Latest

function Invoke-HaWindowsTypedCandidate([string]$Source,[string]$ExpectedSha256,[string]$Scope='tasks') {
    $approved='e25ca7413992a28978587c8d5f4a10390fd82f4895726685d09c5061d73544cc'
    $preparation=[Diagnostics.Stopwatch]::StartNew()
    $request=[Diagnostics.Stopwatch]::StartNew()
    $report=$null;$reason='source_rejected';$status='blocked'
    try {
        if($Scope -cnotin @('tasks','basic')){$reason='input_rejected';throw 'input_rejected'}
        if([Text.Encoding]::UTF8.GetByteCount($Source) -gt 131072){throw 'source_rejected'}
        $sha=[Security.Cryptography.SHA256]::Create()
        try {$actual=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Source))).Replace('-','').ToLowerInvariant()}finally {$sha.Dispose()}
        if($ExpectedSha256 -cne $approved -or $actual -cne $approved){throw 'source_rejected'}
        # Never trust/reuse a type supplied by an earlier invocation or caller.
        if($null -ne ('N100HaTypedCandidateV1.Collector' -as [type])){$reason='type_collision';throw 'type_collision'}
        $reason='compile_failed'
        Microsoft.PowerShell.Utility\Add-Type -TypeDefinition $Source -ErrorAction Stop
        $preparation.Stop()
        $reason='query_failed'
        # Each selected collection uses one cooperative 2s budget. The basic
        # batch shares it across services/tasks/processes/boot; scopes are not
        # separate phase allowances and never form a complete authority proof.
        # request_elapsed_ms additionally includes pin/compile; neither implies
        # an accepted complete Windows/Linux/DB/IPC authority observation.
        if($Scope -ceq 'basic'){$report=[N100HaTypedCandidateV1.Collector]::CollectBasic()}
        else {$report=[N100HaTypedCandidateV1.Collector]::Collect()}
        $status=$report.Status;$reason=$report.Reason
    } catch {
        # Never emit compiler/native exceptions, provider records or source text.
    } finally {$preparation.Stop();$request.Stop()}
    $count=$null;$actions=$null;$elapsed=$null
    if($null -ne $report){$count=$report.TaskCount;$actions=$report.ActionCount;$elapsed=$report.ElapsedMilliseconds}
    $services=$null;$processes=$null;$boots=$null
    if($Scope -ceq 'basic' -and $null -ne $report){$services=$report.ServiceCount;$processes=$report.ProcessCount;$boots=$report.BootCount}
    $scopeName='local-task-enumeration-only';$kind='ha-windows-typed-task-candidate'
    if($Scope -ceq 'basic'){$scopeName='local-basic-enumeration-only';$kind='ha-windows-typed-basic-candidate'}
    return [ordered]@{schema_version=1;kind=$kind;authority=$false;ready=$false;
        equivalence='unknown';scope=$scopeName;source_sha256=$approved;
        status=$status;reason=$reason;task_count=$count;action_count=$actions;
        service_count=$services;process_count=$processes;boot_count=$boots;
        preparation_elapsed_ms=$preparation.Elapsed.TotalMilliseconds;collection_elapsed_ms=$elapsed;
        request_elapsed_ms=$request.Elapsed.TotalMilliseconds;deadline_ms=2000;
        deadline_mode='cooperative-external-watchdog-required'}
}

if($MyInvocation.InvocationName -cne '.'){
    [Console]::Out.WriteLine('{"schema_version":1,"kind":"ha-windows-typed-task-candidate","authority":false,"ready":false,"status":"blocked","reason":"external_watchdog_required"}')
    exit 1
}
