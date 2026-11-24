param(
    [string]$BindingPath,
    [string]$LegacyPlanSha256,
    [string]$AttemptId,
    [string]$PriorSha256,
    [string]$ExpectedServiceInstanceSha256,
    [string]$RecoveryAttemptId,
    [string]$RecoveryPredecessorTerminalSha256,
    [string]$RecoveryPredecessorServiceSha256,
    [string]$TransitionHandoffSha256,
    [string]$TransitionBoundarySha256,
    [string]$RearmAttemptId,
    [string]$RearmPredecessorTerminalSha256,
    [string]$RearmPredecessorBindingSha256,
    [string]$RearmPredecessorArgsSha256,
    [string]$PostbootAttemptId,
    [string]$PostbootPredecessorTerminalSha256,
    [string]$PostbootPredecessorBindingSha256,
    [string]$PostbootPredecessorArgsSha256,
    [string]$PostbootPredecessorSealSha256,
    [string]$PostbootPreviousWindowsBootSha256,
    [string]$UnpublishedAttemptId,
    [string]$UnpublishedPredecessorTerminalSha256,
    [string]$UnpublishedPredecessorArgsSha256,
    [string]$UnpublishedPredecessorPlanSha256,
    [string]$UnpublishedPredecessorSealSha256,
    [string]$PostbootPredecessorKind,
    [string]$PostbootPredecessorPlanSha256,
    [string]$PostbootStoppedAttemptId,
    [string]$PostbootStoppedReceiptSha256,
    [switch]$RecoveryContinuity,
    [switch]$Retire,
    [switch]$ObserveRetired,
    [switch]$ReadPriorServiceArgs,
    [switch]$RequireTerminalInitial,
    [switch]$ResumeRetire,
    [switch]$ReadClosureReceipt,
    [switch]$ReadRejoinReceipt,
    [string]$RejoinMode,
    [string]$ClosurePlanSha256,
    [string]$ClosureGeneration,
    [string]$ClosureHostPid,
    [string]$ClosureStartedMs,
    [string]$ClosureServiceSid
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$pattern = '\A[a-f0-9]{64}\z'

function Get-Digest([string]$Value) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value))).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Get-InitialPredecessorNumbers([byte[]]$Raw,[string]$ExpectedSha,[bool]$Published){
    $bad='initial_service_predecessor_numbers'
    if($null -eq $Raw -or $Raw.Length -lt 1 -or $Raw.Length -gt 65536 -or $ExpectedSha -cnotmatch '\A[a-f0-9]{64}\z'){throw $bad}
    $h=[Security.Cryptography.SHA256]::Create()
    try{$pin=[BitConverter]::ToString($h.ComputeHash($Raw)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}
    if($pin -cne $ExpectedSha){throw $bad}
    # Reuse the existing strict parser: duplicate/escaped duplicate keys and non-integer JSON numbers fail closed.
    if(-not ('N100InitialPredecessorJsonV1.Json' -as [type])){Add-Type -TypeDefinition @'
using System;using System.Text;using System.Collections.Generic;
namespace N100InitialPredecessorJsonV1 {
 public static class Json {
  sealed class Parser {
   readonly string text; int at; int nodes;
   public Parser(string s){text=s;}
   void White(){while(at<text.Length&&(text[at]==' '||text[at]=='\r'||text[at]=='\n'||text[at]=='\t'))at++;}
   void Need(bool ok){if(!ok)throw new InvalidOperationException("runtime_stage_json_rejected");}
   string String(){
    Need(at<text.Length&&text[at++]=='"');var b=new StringBuilder();bool done=false;
    while(at<text.Length){char c=text[at++];if(c=='"'){done=true;break;}
     if(c=='\\'){Need(at<text.Length);c=text[at++];if(c=='u'){Need(at+4<=text.Length);int value=0;
       for(int i=0;i<4;i++){char h=text[at++];int n=h>='0'&&h<='9'?h-'0':h>='a'&&h<='f'?h-'a'+10:h>='A'&&h<='F'?h-'A'+10:-1;Need(n>=0);value=value*16+n;}c=(char)value;
      }else Need(c=='"'||c=='\\'||c=='/');}
     Need(c>=32&&c<=126);b.Append(c);Need(b.Length<=4096);
    }Need(done);return b.ToString();
   }
   object Value(int depth){
    Need(depth<=8&&++nodes<=65536);White();Need(at<text.Length);char c=text[at];
    if(c=='"')return String();
    if(c=='{'){at++;White();var result=new Dictionary<string,object>(StringComparer.Ordinal);
     if(at<text.Length&&text[at]=='}'){at++;return result;}
     while(true){White();string key=String();Need(key.Length>0&&key.Length<=200&&!result.ContainsKey(key));White();Need(at<text.Length&&text[at++]==':');result.Add(key,Value(depth+1));White();Need(at<text.Length);char end=text[at++];if(end=='}')return result;Need(end==',');}
    }
    if(c=='['){at++;White();var values=new List<object>();if(at<text.Length&&text[at]==']'){at++;return values;}
     while(true){Need(values.Count<8192);values.Add(Value(depth+1));White();Need(at<text.Length);char end=text[at++];if(end==']')return values;Need(end==',');}}
    foreach(string word in new string[]{"true","false","null"})if(at+word.Length<=text.Length&&System.String.CompareOrdinal(text,at,word,0,word.Length)==0){at+=word.Length;return word=="null"?null:(object)(word=="true");}
    int start=at;if(c=='-')at++;Need(at<text.Length&&text[at]>='0'&&text[at]<='9');
    if(text[at]=='0')at++;else while(at<text.Length&&text[at]>='0'&&text[at]<='9')at++;
    long integer;Need(Int64.TryParse(text.Substring(start,at-start),System.Globalization.NumberStyles.AllowLeadingSign,System.Globalization.CultureInfo.InvariantCulture,out integer));return integer;
   }
   public object Parse(){object value=Value(0);White();Need(at==text.Length);return value;}
  }
  public static Dictionary<string,object> Object(string text,int maximum){
   if(text==null||text.Length>maximum||Encoding.UTF8.GetByteCount(text)>maximum)throw new InvalidOperationException("runtime_stage_json_size");
   object value=new Parser(text).Parse();var result=value as Dictionary<string,object>;if(result==null)throw new InvalidOperationException("runtime_stage_json_object_required");return result;
  }
 }
}
'@ -ErrorAction Stop}
    $text=[Text.UTF8Encoding]::new($false,$true).GetString($Raw)
    $null=[N100InitialPredecessorJsonV1.Json]::Object($text,65536);$v=ConvertFrom-Json $text
    $names=@($v.PSObject.Properties.Name)
    if($v.kind -isnot [string] -or $v.kind -cnotin @('initial-wsl-cutover-rearm','initial-wsl-cutover-unpublished','initial-wsl-cutover-postboot','initial-wsl-cutover-postboot-unpublished','initial-wsl-cutover-postboot-resume')){throw $bad}
    $field=if($v.kind -ceq 'initial-wsl-cutover-rearm'){'rearm'}elseif($v.kind -ceq 'initial-wsl-cutover-unpublished'){'unpublished'}else{'postboot'}
    $expected=@('schema_version','kind','generation','attempt_id','legacy_plan_sha256','restore_receipt_sha256','activation_intent_sha256','controller_plan_sha256','recovery','revision','phase','sequence',$field)
    $hasFailure='failure_code' -cin $names;if($hasFailure){$expected+=@('failure_code')}
    if((@($names|Sort-Object -CaseSensitive) -join ',') -cne (@($expected|Sort-Object -CaseSensitive) -join ',')){throw $bad}
    foreach($key in @('schema_version','generation','revision','sequence')){if($v.$key -isnot [int] -and $v.$key -isnot [long]){throw $bad}}
    if($v.schema_version -ne 1 -or $v.generation -ne 1 -or $v.sequence -lt 0 -or $v.phase -isnot [string] -or $v.phase -cnotin @('lease_lost','local_revoke_observed')){throw $bad}
    foreach($key in @('attempt_id','legacy_plan_sha256','restore_receipt_sha256','activation_intent_sha256','controller_plan_sha256')){if($v.$key -isnot [string] -or $v.$key -cnotmatch '\A[a-f0-9]{64}\z'){throw $bad}}
    if((@($v.recovery.PSObject.Properties.Name|Sort-Object -CaseSensitive) -join ',') -cne 'attempt_id,predecessor_service_sha256,predecessor_terminal_sha256'){throw $bad}
    foreach($key in @('attempt_id','predecessor_service_sha256','predecessor_terminal_sha256')){if($v.recovery.$key -isnot [string] -or $v.recovery.$key -cnotmatch '\A[a-f0-9]{64}\z'){throw $bad}}
    $scope=$v.$field
    $scopeKeys=@('schema_version','attempt_id','closure_mode','predecessor_args_sha256','predecessor_terminal_sha256')
    if($field -ceq 'rearm'){$scopeKeys+=@('predecessor_binding_sha256')}elseif($field -ceq 'unpublished'){$scopeKeys+=@('predecessor_plan_sha256','predecessor_seal_sha256')}else{
        if(-not $Published){throw $bad}
        $scopeKeys+=@('predecessor_binding_sha256','predecessor_seal_sha256','previous_windows_boot_sha256')
        $tag='predecessor_kind' -cin $scope.PSObject.Properties.Name
        $same=$tag -and $scope.predecessor_kind -ceq 'published-postboot-sameboot-v1'
        if($tag){
            if($scope.predecessor_kind -cnotin @('published-unpublished-recovery-v1','published-postboot-sameboot-v1')){throw $bad}
            $scopeKeys+=@('predecessor_kind')
        }
        if($same){$scopeKeys+=@('predecessor_plan_sha256','stopped_attempt_id','stopped_receipt_sha256')}
        $expectedKind=if($same){'initial-wsl-cutover-postboot-resume'}elseif($tag){'initial-wsl-cutover-postboot-unpublished'}else{'initial-wsl-cutover-postboot'}
        if($v.kind -cne $expectedKind){throw $bad}
    }
    if((@($scope.PSObject.Properties.Name|Sort-Object -CaseSensitive) -join ',') -cne (@($scopeKeys|Sort-Object -CaseSensitive) -join ',') -or ($scope.schema_version -isnot [int] -and $scope.schema_version -isnot [long]) -or $scope.schema_version -ne 1 -or $scope.closure_mode -isnot [string] -or $scope.closure_mode -cne 'windows-offline-v1'){throw $bad}
    foreach($key in $scopeKeys){if($key -notin @('schema_version','closure_mode','predecessor_kind') -and ($scope.$key -isnot [string] -or $scope.$key -cnotmatch '\A[a-f0-9]{64}\z')){throw $bad}}
    if($scope.attempt_id -cin @($v.attempt_id,$v.recovery.attempt_id) -or ($field -ceq 'postboot' -and $same -and $scope.attempt_id -ceq $scope.stopped_attempt_id)){throw $bad}
    $r=[long]$v.revision
    if(($field -ceq 'rearm' -and $r -ne 6) -or ($field -cne 'rearm' -and ($r -lt 8 -or $r -gt 2147483644 -or ($r % 2) -ne 0))){throw $bad}
    if($hasFailure -and ($v.failure_code -isnot [string] -or $v.failure_code -cnotmatch '\A[a-z][a-z0-9_]{0,95}\z')){throw $bad}
    if($Published){if($v.sequence -lt 1){throw $bad}}else{
        if(-not $hasFailure){throw $bad}
        $journal=$v.sequence -eq 1 -and $v.phase -ceq 'local_revoke_observed' -and $v.failure_code -ceq 'controller_journal_not_durable'
        $timeout=$field -ceq 'unpublished' -and $v.sequence -eq 0 -and $v.phase -ceq 'lease_lost' -and $v.failure_code -ceq 'legacy_pipe_response_timeout_postcommit'
        if(-not ($journal -or $timeout)){throw $bad}
    }
    return [pscustomobject]@{closed=($r+1);opened=($r+2);revoked=($r+3);terminal=$v}
}

function Read-Scope([string]$Path, [string[]]$Owners, [switch]$Immutable) {
    if ($Path -cnotmatch '\A[A-Za-z]:\\[A-Za-z0-9_.\\-]+\z' -or $Path -match '\\\.\.?(\\|$)') { throw 'live_authority_path' }
    $entry = Get-Item -LiteralPath $Path -Force
    if ($entry -isnot [IO.FileInfo] -or $entry.Length -lt 1 -or $entry.Length -gt 8192 -or
        ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'live_authority_path' }
    $node = $entry.Directory
    while ($null -ne $node) {
        if ($node.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'live_authority_path' }
        $node = $node.Parent
    }
    $entries = @($entry,$entry.Directory)
    if ($Immutable) {
        for ($node = $entry.Directory.Parent; $null -ne $node; $node = $node.Parent) { $entries += $node }
    }
    foreach ($item in $entries) {
        $sd = Get-Acl -LiteralPath $item.FullName
        $ancestor = $Immutable -and $item.FullName -cne $entry.FullName -and $item.FullName -cne $entry.Directory.FullName
        $allowed = if ($ancestor) { @($Owners | Where-Object { $_ -cne $ClosureServiceSid }) } else { $Owners }
        $owner = $sd.GetOwner([Security.Principal.SecurityIdentifier]).Value
        $trustedRoot = $Immutable -and $item.FullName -ceq 'C:\' -and
            $owner -ceq 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
        if ($owner -cnotin $allowed -and -not $trustedRoot) { throw 'live_authority_acl' }
        # Match the installer: ancestor entries must prevent replacing the child,
        # while the file and journal also deny general writes by other principals.
        $mask = if ($ancestor) { 269287488L } elseif ($Immutable) { 1343029590L } else {
            [long][Security.AccessControl.FileSystemRights]'Write,Delete,DeleteSubdirectoriesAndFiles,ChangePermissions,TakeOwnership'
        }
        foreach ($rule in $sd.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
            if ($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Value -cnotin $allowed -and
                -not ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -and
                (([long]$rule.FileSystemRights -band 4294967295L) -band $mask)) { throw 'live_authority_acl' }
        }
    }
    # Snapshot the path timestamp before opening. A journal replaced between the
    # snapshot and open can only be judged older than the new record itself.
    $written = $entry.LastWriteTimeUtc
    $share = if ($Immutable) { [IO.FileShare]::Read } else { [IO.FileShare]::Read -bor [IO.FileShare]::Delete }
    $stream = [IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,$share)
    try {
        if ($stream.Length -lt 1 -or $stream.Length -gt 8192) { throw 'live_authority_path' }
        if ($Immutable) {
            $links = @(& (Join-Path ([Environment]::SystemDirectory) 'fsutil.exe') hardlink list $Path)
            if ($LASTEXITCODE -ne 0 -or $links.Count -ne 1) { throw 'live_authority_path' }
        }
        $reader = [IO.BinaryReader]::new($stream,[Text.UTF8Encoding]::new($false,$true),$true)
        try {
            $raw = $reader.ReadBytes([int]$stream.Length)
            if ($stream.ReadByte() -ne -1) { throw 'live_authority_path' }
            return [pscustomobject]@{raw=[Text.UTF8Encoding]::new($false,$true).GetString($raw);write_time=$written}
        } finally { $reader.Dispose() }
    } finally { $stream.Dispose() }
}

function Find-BoundService($Plan) {
    $service = [wmi]('Win32_Service.Name="' + $Plan.service_name + '"')
    $service.Get()
    if ($null -eq $service -or $service.Name -cne $Plan.service_name -or
        (Get-Digest $service.PathName) -cne $Plan.service_image_path_sha256) { throw 'live_authority_service' }
    return $service
}

function Assert-Retired($Service) {
    if ($Service.StartMode -cne 'Disabled' -or $Service.State -cne 'Stopped' -or $Service.ProcessId -ne 0) {
        throw 'initial_service_retirement_unconfirmed'
    }
}

function Assert-TransitionOldVm($Plan) {
    $vm = @(Get-VM -Id ([Guid]::Parse($Plan.old_vm_id)) -ErrorAction Stop)
    if ($vm.Count -ne 1 -or [string]$vm[0].State -cne 'Off' -or [string]$vm[0].AutomaticStartAction -cne 'Nothing') {
        throw 'transition_old_vm_not_fenced'
    }
    [string[]]$paths = @(Get-VMHardDiskDrive -VM $vm[0] -ErrorAction Stop | ForEach-Object {
        $path = [string]$_.Path
        if ($path -cnotmatch '\A[A-Za-z]:\\[\x20-\x7e]+\z') { throw 'transition_old_vm_disk_drift' }
        $path.ToLowerInvariant()
    })
    [Array]::Sort($paths,[StringComparer]::Ordinal)
    if ($paths.Count -lt 1 -or $paths.Count -gt 8 -or
        @($paths | Select-Object -Unique).Count -ne $paths.Count -or
        (Get-Digest ($paths -join [char]10)) -cne $Plan.old_disk_paths_sha256) {
        throw 'transition_old_vm_disk_drift'
    }
}

function Read-TransitionAuthority([string[]]$Admins) {
    if ($BindingPath -cnotmatch '\AC:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}\\transition-service-[a-f0-9]{64}\.json\z' -or
        -not $BindingPath.EndsWith(('-' + $AttemptId + '.json'),[StringComparison]::Ordinal) -or
        (@($AttemptId,$LegacyPlanSha256,$PriorSha256,$ExpectedServiceInstanceSha256,$TransitionHandoffSha256,$TransitionBoundarySha256) |
            Where-Object { $_ -cnotmatch $pattern })) { throw 'transition_authority_scope' }
    $inputFile = Read-Scope $BindingPath $Admins
    if ((Get-Digest $inputFile.raw) -cne $ExpectedServiceInstanceSha256) { throw 'transition_authority_instance' }
    $grantPath = Join-Path ([IO.Path]::GetDirectoryName($BindingPath)) ("transition-grant-" + $AttemptId + ".json")
    $grantFile = Read-Scope $grantPath $Admins
    $grant = ConvertFrom-Json -InputObject $grantFile.raw
    if ((@($grant.PSObject.Properties.Name | Sort-Object -CaseSensitive) -join ',') -cne 'attempt_id,handoff_sha256,service_instance_sha256' -or
        $grant.attempt_id -cne $AttemptId -or $grant.handoff_sha256 -cne $TransitionHandoffSha256 -or
        $grant.service_instance_sha256 -cne $ExpectedServiceInstanceSha256) { throw 'transition_authority_scope' }
    $plan = ConvertFrom-Json -InputObject $inputFile.raw
    $fields = @($plan.PSObject.Properties.Name | Sort-Object -CaseSensitive)
    if (($fields -join ',') -cne 'attempt_id,controller_plan_sha256,generation,legacy_plan_sha256,old_disk_paths_sha256,old_vm_id,python_command_sha256,python_path_sha256,service_created_ticks,service_image_path_sha256,service_name,service_pid,state_path,target,timeline' -or
        $plan.attempt_id -cne $AttemptId -or $plan.legacy_plan_sha256 -cne $LegacyPlanSha256 -or
        (@($plan.controller_plan_sha256,$plan.old_disk_paths_sha256,$plan.python_command_sha256,$plan.python_path_sha256,$plan.service_image_path_sha256) |
            Where-Object { $_ -cnotmatch $pattern }) -or
        ($plan.generation -isnot [int] -and $plan.generation -isnot [long]) -or $plan.generation -le 1 -or
        ($plan.timeline -isnot [int] -and $plan.timeline -isnot [long]) -or $plan.timeline -le 1 -or
        $plan.target -cnotin @('A','B') -or $plan.old_vm_id -cnotmatch '\A[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\z' -or
        $plan.service_name -cnotmatch '\AN100HA_[a-f0-9]{24}_controller\z' -or
        $plan.service_created_ticks -cnotmatch '\A[1-9][0-9]{1,19}\z' -or
        ($plan.service_pid -isnot [int] -and $plan.service_pid -isnot [long]) -or $plan.service_pid -lt 1 -or
        -not $plan.state_path.EndsWith('\journal\controller-state.json',[StringComparison]::Ordinal)) { throw 'transition_authority_scope' }
    $service = Find-BoundService $plan
    if ($service.State -cne 'Running' -or $service.ProcessId -ne $plan.service_pid) { throw 'transition_authority_instance' }
    $process = Get-Process -Id $plan.service_pid -ErrorAction Stop
    $childProcess = $null
    try {
        if ($process.HasExited -or $process.SessionId -ne 0 -or
            ('"' + $process.MainModule.FileName + '"') -ine $service.PathName -or
            $process.StartTime.ToUniversalTime().Ticks.ToString() -cne $plan.service_created_ticks) { throw 'transition_authority_instance' }
        $identity = [Security.Principal.NTAccount]::new('NT SERVICE',$plan.service_name).Translate([Security.Principal.SecurityIdentifier]).Value
        Assert-TransitionOldVm $plan
        $saved = Read-Scope $plan.state_path ($Admins + $identity)
        $record = ConvertFrom-Json -InputObject $saved.raw
        if ($record.target -cne $plan.target -or $record.old -cnotin @('A','B') -or
            $record.old -ceq $record.target -or $record.generation -ne $plan.generation) { throw 'transition_authority_lease' }
        if ($TransitionBoundarySha256 -ceq (Get-Digest 'controller-ready') -and
            -not $record.PSObject.Properties['lease_heartbeat'] -and
            $record.phase -cin @('rejoining','validating','ready') -and -not $process.HasExited) {
            [ordered]@{transition_authority_pending=$true}|ConvertTo-Json -Compress
            return
        }
        $stamp = $record.lease_heartbeat
        $numbers = @($stamp.sampled_at_ms,$stamp.valid_until_ms,$stamp.python_pid,$stamp.sequence,$stamp.revision)
        if ($record.phase -cne 'ready' -or $record.target -cne $plan.target -or $record.old -cnotin @('A','B') -or
            $record.old -ceq $record.target -or $record.generation -ne $plan.generation -or
            $stamp.plan_sha256 -cne $plan.controller_plan_sha256 -or $stamp.epoch -ne $plan.generation -or
            $stamp.timeline -ne $plan.timeline -or
            ($numbers | Where-Object { ($_ -isnot [int] -and $_ -isnot [long]) -or $_ -le 0 }) -or
            $stamp.revision -lt $plan.generation -or $stamp.broker_instance_sha256 -cnotmatch $pattern -or
            $stamp.postcommit_nonce_sha256 -cnotmatch $pattern -or $stamp.valid_until_ms -le $stamp.sampled_at_ms -or
            ($stamp.valid_until_ms - $stamp.sampled_at_ms) -gt 5000) { throw 'transition_authority_lease' }
        $child = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $stamp.python_pid) -ErrorAction Stop
        if ($null -eq $child -or $child.ParentProcessId -ne $plan.service_pid -or $child.SessionId -ne 0 -or
            (Get-Digest $child.CommandLine) -cne $plan.python_command_sha256 -or
            (Get-Digest $child.ExecutablePath) -cne $plan.python_path_sha256 -or
            $child.CreationDate.ToUniversalTime() -lt $process.StartTime.ToUniversalTime() -or
            ([DateTimeOffset]$child.CreationDate).ToUnixTimeMilliseconds() -gt $stamp.sampled_at_ms) { throw 'transition_authority_instance' }
        $childProcess = Get-Process -Id $stamp.python_pid -ErrorAction Stop
        if ($childProcess.SessionId -ne 0 -or (Get-Digest $childProcess.MainModule.FileName) -cne $plan.python_path_sha256 -or
            [Math]::Abs(($childProcess.StartTime.ToUniversalTime() - $child.CreationDate.ToUniversalTime()).TotalMilliseconds) -ge 1) {
            throw 'transition_authority_instance'
        }
        Assert-TransitionOldVm $plan
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        if ($process.HasExited -or $childProcess.HasExited -or $now -lt $stamp.sampled_at_ms -or ($now - $stamp.sampled_at_ms) -ge 2000 -or
            $now -ge $stamp.valid_until_ms) { throw 'transition_authority_lease' }
        [ordered]@{generation=$stamp.epoch;timeline=$stamp.timeline;revision=$stamp.revision;
            transition_proof_sha256=(Get-Digest ($inputFile.raw + ':' + $grantFile.raw + ':' + $saved.raw + ':' + $PriorSha256 + ':' + $TransitionBoundarySha256))}|ConvertTo-Json -Compress
    } finally {
        if ($null -ne $childProcess) { $childProcess.Dispose() }
        $process.Dispose()
    }
}

function Read-ClosureReceipt {
    if ($BindingPath -cnotmatch '\AC:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}\\journal\\lease-closure-[a-f0-9]{64}\.json\z' -or
        $ClosurePlanSha256 -cnotmatch $pattern -or
        -not $BindingPath.EndsWith(('-' + $ClosurePlanSha256 + '.json'),[StringComparison]::Ordinal) -or
        $ClosureServiceSid -cnotmatch '\AS-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}\z' -or
        @(@($ClosureGeneration,$ClosureHostPid,$ClosureStartedMs) | Where-Object { $_ -cnotmatch '\A[1-9][0-9]{0,17}\z' }).Count) {
        throw 'transition_closure_receipt_invalid'
    }
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $owners = @('S-1-5-18','S-1-5-32-544',$sid,$ClosureServiceSid)
    $launchPath = Join-Path ([IO.Path]::GetDirectoryName($BindingPath)) ("lease-closure-launch-" + $ClosurePlanSha256 + ".json")
    $launchFile = Read-Scope $launchPath $owners -Immutable
    $launch = ConvertFrom-Json -InputObject $launchFile.raw
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    if ((@($launch.PSObject.Properties.Name | Sort-Object -CaseSensitive) -join ',') -cne
        'host_pid,plan_sha256,python_pid,schema_version,started_at_ms' -or
        $launch.plan_sha256 -isnot [string] -or $launch.plan_sha256 -cne $ClosurePlanSha256 -or
        $launch.schema_version -isnot [int] -or $launch.schema_version -ne 1 -or
        $launch.host_pid -isnot [int] -or $launch.host_pid -ne [long]$ClosureHostPid -or
        $launch.python_pid -isnot [int] -or $launch.python_pid -le 0 -or
        $launch.started_at_ms -isnot [long] -or $launch.started_at_ms -lt [long]$ClosureStartedMs -or
        $launch.started_at_ms -gt $now -or $now - $launch.started_at_ms -gt 25000) {
        throw 'transition_closure_receipt_invalid'
    }
    $file = Read-Scope $BindingPath $owners -Immutable
    $record = ConvertFrom-Json -InputObject $file.raw
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    if ((@($record.PSObject.Properties.Name | Sort-Object -CaseSensitive) -join ',') -cne
        'generation,node_proofs,observed_at_ms,plan_sha256,python_pid,schema_version' -or
        (@($record.node_proofs.PSObject.Properties.Name | Sort-Object -CaseSensitive) -join ',') -cne 'A,B' -or
        $record.plan_sha256 -isnot [string] -or $record.plan_sha256 -cne $ClosurePlanSha256 -or
        ($record.schema_version -isnot [int] -and $record.schema_version -isnot [long]) -or $record.schema_version -ne 1 -or
        ($record.generation -isnot [int] -and $record.generation -isnot [long]) -or
        ($record.python_pid -isnot [int] -and $record.python_pid -isnot [long]) -or
        $record.node_proofs.A -isnot [string] -or $record.node_proofs.B -isnot [string] -or
        $record.generation -ne [long]$ClosureGeneration -or $record.python_pid -ne $launch.python_pid -or
        ($record.observed_at_ms -isnot [long] -and $record.observed_at_ms -isnot [int]) -or
        $record.observed_at_ms -lt $launch.started_at_ms -or $record.observed_at_ms -gt $now -or
        $now - $record.observed_at_ms -gt 25000 -or $record.node_proofs.A -cnotmatch $pattern -or
        $record.node_proofs.B -cnotmatch $pattern -or $record.node_proofs.A -ceq $record.node_proofs.B) {
        throw 'transition_closure_receipt_invalid'
    }
    [ordered]@{plan_sha256=$record.plan_sha256;generation=$record.generation;node_proofs=$record.node_proofs} |
        ConvertTo-Json -Compress
}

function Read-RejoinReceipt {
    if ($BindingPath -cnotmatch '\AC:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}\\journal\z' -or
        $ClosurePlanSha256 -cnotmatch $pattern -or
        $ClosureServiceSid -cnotmatch '\AS-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}\z' -or
        $ClosureHostPid -cnotmatch '\A[1-9][0-9]{0,9}\z' -or
        $ClosureStartedMs -cnotmatch '\A[1-9][0-9]{0,17}\z' -or
        $RejoinMode -cnotin @('initialize','stage-rejoin','observe-rejoin-stage','check-rejoin')) {
        throw 'transition_rejoin_receipt_invalid'
    }
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $owners = @('S-1-5-18','S-1-5-32-544',$sid,$ClosureServiceSid)
    $launchPattern = '\Arejoin-launch-' + $ClosurePlanSha256 + '-([a-f0-9]{32})\.json\z'
    $leaves = @(Get-ChildItem -LiteralPath $BindingPath -Force)
    if ($leaves.Count -gt 64) { throw 'transition_rejoin_receipt_invalid' }
    $launch = $null
    foreach ($leaf in $leaves) {
        $match = [regex]::Match($leaf.Name,$launchPattern)
        if (-not $match.Success) { continue }
        $probe = $match.Groups[1].Value
        $entry = ConvertFrom-Json -InputObject (Read-Scope $leaf.FullName $owners -Immutable).raw
        if ((@($entry.PSObject.Properties.Name | Sort-Object -CaseSensitive) -join ',') -cne
            'host_pid,mode,plan_sha256,probe,python_pid,schema_version,started_at_ms' -or
            $entry.plan_sha256 -isnot [string] -or $entry.plan_sha256 -cne $ClosurePlanSha256 -or
            $entry.probe -isnot [string] -or $entry.probe -cne $probe -or
            $entry.mode -isnot [string] -or $entry.mode -cnotin @('initialize','stage-rejoin','observe-rejoin-stage','check-rejoin') -or
            $entry.schema_version -isnot [int] -or $entry.schema_version -ne 1 -or
            $entry.host_pid -isnot [int] -or $entry.host_pid -lt 1 -or
            $entry.python_pid -isnot [int] -or $entry.python_pid -lt 1 -or
            $entry.started_at_ms -isnot [long] -or $entry.started_at_ms -lt 1) {
            throw 'transition_rejoin_receipt_invalid'
        }
        if ($entry.host_pid -ne [long]$ClosureHostPid -or $entry.started_at_ms -lt [long]$ClosureStartedMs) { continue }
        if ($null -ne $launch -or $entry.mode -cne $RejoinMode) { throw 'transition_rejoin_receipt_invalid' }
        $launch = $entry
    }
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $launchMaxAge = if ($RejoinMode -ceq 'stage-rejoin') { 150000 } else { 25000 }
    if ($null -eq $launch -or $launch.started_at_ms -gt $now -or $now - $launch.started_at_ms -gt $launchMaxAge) {
        throw 'transition_rejoin_receipt_invalid'
    }
    $path = Join-Path $BindingPath ("rejoin-result-" + $ClosurePlanSha256 + "-" + $launch.probe + ".json")
    $result = ConvertFrom-Json -InputObject (Read-Scope $path $owners -Immutable).raw
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $keys = 'authority_opened,mode,observed_at_ms,phase,plan_sha256,probe,python_pid,rejoin_stage_sha256,schema_version,status'
    if ($RejoinMode -ceq 'check-rejoin') {
        $keys = $keys.Replace(',schema_version,status',',revision,schema_version,standby_rejoined,status')
    }
    $walKeys = @(@('replica_fence_wal','source_fence_wal') | Where-Object { $null -ne $result.PSObject.Properties[$_] })
    if ($walKeys.Count -gt 1) { throw 'transition_rejoin_receipt_invalid' }
    if ($walKeys.Count -eq 1) {
        $walKey = $walKeys[0]
        $wal = $result.PSObject.Properties[$walKey].Value
        if ($RejoinMode -ceq 'initialize' -or $wal -isnot [string] -or
            $wal -cnotmatch '\A[a-f0-9]{16}\z' -or $wal -ceq '0000000000000000') {
            throw 'transition_rejoin_receipt_invalid'
        }
        $keys = ((($keys -split ',') + $walKey) | Sort-Object -CaseSensitive) -join ','
    }
    $expectedPhase = if ($RejoinMode -ceq 'initialize') { 'fencing' } else { 'rejoining' }
    if ((@($result.PSObject.Properties.Name | Sort-Object -CaseSensitive) -join ',') -cne $keys -or
        $result.schema_version -isnot [int] -or $result.schema_version -ne 1 -or
        $result.plan_sha256 -isnot [string] -or $result.plan_sha256 -cne $ClosurePlanSha256 -or
        $result.mode -isnot [string] -or $result.mode -cne $RejoinMode -or
        $result.probe -isnot [string] -or $result.probe -cne $launch.probe -or $result.python_pid -isnot [int] -or
        $result.python_pid -ne $launch.python_pid -or $result.observed_at_ms -isnot [long] -or
        $result.observed_at_ms -lt $launch.started_at_ms -or $result.observed_at_ms -gt $now -or
        $now - $launch.started_at_ms -gt $launchMaxAge -or $now - $result.observed_at_ms -gt 25000 -or
        $result.status -isnot [string] -or $result.status -cne 'PASS' -or
        $result.phase -isnot [string] -or $result.phase -cne $expectedPhase -or
        $result.authority_opened -isnot [bool] -or $result.authority_opened -or
        $result.rejoin_stage_sha256 -isnot [string] -or $result.rejoin_stage_sha256 -cnotmatch $pattern) { throw 'transition_rejoin_receipt_invalid' }
    $reply = [ordered]@{status='PASS';phase=$expectedPhase;authority_opened=$false;rejoin_stage_sha256=$result.rejoin_stage_sha256}
    if ($walKeys.Count -eq 1) { $reply[$walKey] = $wal }
    if ($RejoinMode -ceq 'check-rejoin') {
        if ($result.standby_rejoined -isnot [bool] -or -not $result.standby_rejoined -or
            ($result.revision -isnot [int] -and $result.revision -isnot [long]) -or $result.revision -lt 1) {
            throw 'transition_rejoin_receipt_invalid'
        }
        $reply['standby_rejoined'] = $true
        $reply['revision'] = $result.revision
    }
    $reply | ConvertTo-Json -Compress
}

try {
    $recovering = $PSBoundParameters.ContainsKey('RecoveryAttemptId') -or
        $PSBoundParameters.ContainsKey('RecoveryPredecessorTerminalSha256') -or
        $PSBoundParameters.ContainsKey('RecoveryPredecessorServiceSha256')
    $rearming=$PSBoundParameters.ContainsKey('RearmAttemptId') -or
        $PSBoundParameters.ContainsKey('RearmPredecessorTerminalSha256') -or
        $PSBoundParameters.ContainsKey('RearmPredecessorBindingSha256') -or
        $PSBoundParameters.ContainsKey('RearmPredecessorArgsSha256')
    if($rearming -and (-not $recovering -or $RearmAttemptId -cnotmatch $pattern -or
       $RearmPredecessorTerminalSha256 -cnotmatch $pattern -or $RearmPredecessorBindingSha256 -cnotmatch $pattern -or
       $RearmPredecessorArgsSha256 -cnotmatch $pattern -or $RearmAttemptId -ceq $RecoveryAttemptId -or
       $RearmAttemptId -ceq $AttemptId)){throw 'live_authority_rearm_scope'}
    $postboot=$PSBoundParameters.ContainsKey('PostbootAttemptId') -or $PSBoundParameters.ContainsKey('PostbootPredecessorTerminalSha256') -or $PSBoundParameters.ContainsKey('PostbootPredecessorBindingSha256') -or $PSBoundParameters.ContainsKey('PostbootPredecessorArgsSha256') -or $PSBoundParameters.ContainsKey('PostbootPredecessorSealSha256') -or $PSBoundParameters.ContainsKey('PostbootPreviousWindowsBootSha256')
    if($postboot -and (-not $recovering -or $rearming -or $PostbootAttemptId -cnotmatch $pattern -or $PostbootPredecessorTerminalSha256 -cnotmatch $pattern -or $PostbootPredecessorBindingSha256 -cnotmatch $pattern -or $PostbootPredecessorArgsSha256 -cnotmatch $pattern -or $PostbootPredecessorSealSha256 -cnotmatch $pattern -or $PostbootPreviousWindowsBootSha256 -cnotmatch $pattern -or $PostbootAttemptId -ceq $RecoveryAttemptId -or $PostbootAttemptId -ceq $AttemptId)){throw 'live_authority_postboot_scope'}
    $postbootTagged=$PSBoundParameters.ContainsKey('PostbootPredecessorKind')
    $postbootUnpublished=$postbootTagged -and $PostbootPredecessorKind -ceq 'published-unpublished-recovery-v1'
    $postbootResume=$postbootTagged -and $PostbootPredecessorKind -ceq 'published-postboot-sameboot-v1'
    if($postbootTagged -and (-not $postboot -or -not ($postbootUnpublished -or $postbootResume))){throw 'live_authority_postboot_scope'}
    $resumePins=@($PostbootPredecessorPlanSha256,$PostbootStoppedAttemptId,$PostbootStoppedReceiptSha256)
    if(($postbootResume -and @($resumePins|Where-Object {$_ -cnotmatch $pattern}).Count) -or
       (-not $postbootResume -and @($resumePins|Where-Object {$_}).Count)){throw 'live_authority_postboot_scope'}
    $unpublished=$PSBoundParameters.ContainsKey('UnpublishedAttemptId') -or $PSBoundParameters.ContainsKey('UnpublishedPredecessorTerminalSha256') -or $PSBoundParameters.ContainsKey('UnpublishedPredecessorArgsSha256') -or $PSBoundParameters.ContainsKey('UnpublishedPredecessorPlanSha256') -or $PSBoundParameters.ContainsKey('UnpublishedPredecessorSealSha256')
    if($unpublished -and (-not $recovering -or $rearming -or $postboot -or $UnpublishedAttemptId -cnotmatch $pattern -or $UnpublishedPredecessorTerminalSha256 -cnotmatch $pattern -or $UnpublishedPredecessorArgsSha256 -cnotmatch $pattern -or $UnpublishedPredecessorPlanSha256 -cnotmatch $pattern -or $UnpublishedPredecessorSealSha256 -cnotmatch $pattern -or $UnpublishedAttemptId -ceq $RecoveryAttemptId -or $UnpublishedAttemptId -ceq $AttemptId)){throw 'live_authority_unpublished_scope'}
    if ($RecoveryContinuity -and -not $recovering) { throw 'live_authority_recovery_scope' }
    if ($recovering -and ($ReadRejoinReceipt -or $ReadClosureReceipt -or $ReadPriorServiceArgs -or $Retire -or
            $ResumeRetire -or $ObserveRetired -or $TransitionHandoffSha256 -or $TransitionBoundarySha256 -or
            $ExpectedServiceInstanceSha256 -cnotmatch $pattern -or $RecoveryAttemptId -cnotmatch $pattern -or
            $RecoveryPredecessorTerminalSha256 -cnotmatch $pattern -or
            $RecoveryPredecessorServiceSha256 -cnotmatch $pattern -or $RecoveryAttemptId -ceq $AttemptId)) {
        throw 'live_authority_recovery_scope'
    }
    if ($RequireTerminalInitial -and -not $ReadPriorServiceArgs) { throw 'initial_service_retirement_scope' }
    if ($ReadRejoinReceipt) {
        if ($ReadPriorServiceArgs -or $ReadClosureReceipt -or $Retire -or $ResumeRetire -or $ObserveRetired -or $TransitionHandoffSha256 -or $TransitionBoundarySha256) {
            throw 'transition_rejoin_receipt_invalid'
        }
        Read-RejoinReceipt
        return
    }
    if ($ReadClosureReceipt) {
        if ($ReadPriorServiceArgs -or $Retire -or $ResumeRetire -or $ObserveRetired -or $TransitionHandoffSha256 -or $TransitionBoundarySha256) {
            throw 'transition_closure_receipt_invalid'
        }
        Read-ClosureReceipt
        return
    }
    $retireModes = @(@($Retire,$ObserveRetired,$ResumeRetire) | Where-Object { $_.IsPresent }).Count
    if (($ReadPriorServiceArgs -and ($retireModes -ne 0 -or $TransitionHandoffSha256 -or
            $TransitionBoundarySha256 -or $ExpectedServiceInstanceSha256 -cnotmatch $pattern)) -or
        $retireModes -gt 1 -or ($retireModes -eq 1 -and $ExpectedServiceInstanceSha256 -cnotmatch $pattern)) {
        throw 'initial_service_retirement_scope'
    }
    if ($TransitionHandoffSha256 -or $TransitionBoundarySha256) {
        if ($retireModes -ne 0) { throw 'transition_authority_scope' }
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        Read-TransitionAuthority @('S-1-5-18','S-1-5-32-544',$sid)
        return
    }
    $fileName = if ($recovering) { 'initial-recovery-service-' + $RecoveryAttemptId + '.json' }
        else { 'initial-service-' + $AttemptId + '.json' }
    if ($LegacyPlanSha256 -cnotmatch $pattern -or $AttemptId -cnotmatch $pattern -or $PriorSha256 -cnotmatch $pattern -or
        $BindingPath -cnotmatch ('\AC:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}\\' + [regex]::Escape($fileName) + '\z')) {
        throw 'live_authority_scope'
    }
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $admins = @('S-1-5-18','S-1-5-32-544',$sid)
    $inputFile = Read-Scope $BindingPath $admins
    if ($ExpectedServiceInstanceSha256 -and ($ExpectedServiceInstanceSha256 -cnotmatch $pattern -or
        (Get-Digest $inputFile.raw) -cne $ExpectedServiceInstanceSha256)) { throw 'live_authority_service_instance' }
    $plan = ConvertFrom-Json -InputObject $inputFile.raw
    $fields = @($plan.PSObject.Properties.Name | Sort-Object -CaseSensitive)
    if (($fields -join ',') -cne 'attempt_id,controller_plan_sha256,first_write_proof_sha256,legacy_plan_sha256,service_created_ticks,service_image_path_sha256,service_name,service_pid,state_path' -or
        $plan.attempt_id -cne $AttemptId -or $plan.legacy_plan_sha256 -cne $LegacyPlanSha256 -or
        $plan.controller_plan_sha256 -cnotmatch $pattern -or $plan.first_write_proof_sha256 -cnotmatch $pattern -or
        $plan.service_image_path_sha256 -cnotmatch $pattern -or $plan.service_name -cnotmatch '\AN100HA_[a-f0-9]{24}_controller\z' -or
        $plan.service_created_ticks -cnotmatch '\A[1-9][0-9]{1,19}\z' -or
        ($plan.service_pid -isnot [int] -and $plan.service_pid -isnot [long]) -or $plan.service_pid -lt 1 -or
        -not $plan.state_path.EndsWith('\journal\controller-state.json',[StringComparison]::Ordinal)) { throw 'live_authority_scope' }
    $expectedRevision=if($rearming){6}else{4}
    if($unpublished -or $postboot){
        $root=[IO.Path]::GetDirectoryName([IO.Path]::GetDirectoryName($plan.state_path))
        $leaf=if($unpublished){'unpublished-predecessor-state.json'}else{'postboot-predecessor-state.json'}
        $pin=if($unpublished){$UnpublishedPredecessorTerminalSha256}else{$PostbootPredecessorTerminalSha256}
        $prior=Read-Scope (Join-Path $root $leaf) $admins -Immutable
        $numbers=Get-InitialPredecessorNumbers ([Text.UTF8Encoding]::new($false,$true).GetBytes($prior.raw)) $pin $postboot
        if($postboot){
            $kindWrong=if($postbootResume){$numbers.terminal.kind -cnotin @('initial-wsl-cutover-postboot','initial-wsl-cutover-postboot-unpublished','initial-wsl-cutover-postboot-resume')}elseif($postbootUnpublished){$numbers.terminal.kind -cne 'initial-wsl-cutover-unpublished'}else{$numbers.terminal.kind -cne 'initial-wsl-cutover-rearm'}
            if($kindWrong){throw 'live_authority_postboot_scope'}
        }
        $expectedRevision=$numbers.opened
    }

    $identity = [Security.Principal.NTAccount]::new('NT SERVICE',$plan.service_name).Translate([Security.Principal.SecurityIdentifier]).Value
    if ($recovering -and $plan.service_name -cne ('N100HA_' + $RecoveryAttemptId.Substring(0,24) + '_controller')) {
        throw 'live_authority_recovery_scope'
    }
    # Bind directly to the validated service key instead of enumerating Win32_Service.
    $service = Find-BoundService $plan
    if ($ReadPriorServiceArgs) {
        if ($service.State -cne 'Stopped' -or $service.ProcessId -ne 0 -or
            $service.StartMode -cnotin @('Manual','Disabled') -or
            ($RequireTerminalInitial -and $service.StartMode -cne 'Disabled') -or $service.ServiceType -cne 'Own Process' -or
            $service.DesktopInteract -or $service.StartName -ine ('NT SERVICE\' + $plan.service_name) -or
            $service.PathName -cnotmatch '\A"[A-Za-z]:\\[A-Za-z0-9_.\\-]+\\windows-service-host\.exe"\z') {
            throw 'live_authority_prior_args'
        }
        $directory = [IO.Path]::GetDirectoryName($service.PathName.Trim('"'))
        $argsPath = Join-Path $directory 'service.args'
        $ClosureServiceSid = $identity
        $savedArgs = Read-Scope $argsPath ($admins + @($identity)) -Immutable
        $lines = $savedArgs.raw.Split([char]10)
        if ($lines.Count -ne 14 -or $lines[-1] -cne '' -or $lines[0] -cne 'format=enforced-service-v1' -or
            @($lines | Where-Object { $_ -cmatch '\A(?:service_name|service_sid|plan_sha256|mode)=' }).Count -ne 4 -or
            ('service_name=' + $plan.service_name) -cnotin $lines -or ('service_sid=' + $identity) -cnotin $lines -or
            ('plan_sha256=' + $plan.controller_plan_sha256) -cnotin $lines -or 'mode=serve' -cnotin $lines) {
            throw 'live_authority_prior_args'
        }
        $terminal = $null
        if ($RequireTerminalInitial) {
            $terminal = Read-Scope $plan.state_path ($admins + @($identity)) -Immutable
            $record = ConvertFrom-Json -InputObject $terminal.raw
            if ($record.kind -cne 'initial-wsl-cutover' -or $record.schema_version -ne 1 -or
                $record.phase -cnotin @('lease_lost','local_revoke_observed') -or $record.generation -ne 1 -or
                $record.attempt_id -cne $AttemptId -or $record.legacy_plan_sha256 -cne $LegacyPlanSha256 -or
                $record.controller_plan_sha256 -cne $plan.controller_plan_sha256) {
                throw 'live_authority_prior_args'
            }
        }
        $confirmed = Find-BoundService $plan
        if ($confirmed.State -cne 'Stopped' -or $confirmed.ProcessId -ne 0 -or
            $confirmed.StartMode -cne $service.StartMode) { throw 'live_authority_prior_args' }
        $reply = [ordered]@{prior_args_sha256=(Get-Digest $savedArgs.raw);
            service_instance_sha256=$ExpectedServiceInstanceSha256}
        if ($RequireTerminalInitial) {
            $settled = Read-Scope $plan.state_path ($admins + @($identity)) -Immutable
            if ($settled.raw -cne $terminal.raw) { throw 'live_authority_prior_args' }
            $reply['terminal_state_sha256'] = Get-Digest $settled.raw
        }
        $reply | ConvertTo-Json -Compress
        return
    }
    if ($ObserveRetired) {
        Assert-Retired $service
        [ordered]@{initial_service_retired=$true;retirement_sha256=(Get-Digest ($inputFile.raw + ':retired:' + $PriorSha256))}|ConvertTo-Json -Compress
        return
    }
    if ($service.State -cne 'Running' -or $service.ProcessId -ne $plan.service_pid) { throw 'live_authority_service' }
    $process = Get-Process -Id $plan.service_pid -ErrorAction Stop
    try {
        if ($process.HasExited -or $process.SessionId -ne 0 -or
            ('"' + $process.MainModule.FileName + '"') -ine $service.PathName -or
            $process.StartTime.ToUniversalTime().Ticks.ToString() -cne $plan.service_created_ticks) {
            throw 'live_authority_service'
        }
        if ($Retire -or $ResumeRetire) {
            if ($Retire -and ($service.StartMode -cne 'Manual' -or $process.HasExited -or
                $service.ChangeStartMode('Disabled').ReturnValue -ne 0)) { throw 'initial_service_disable_unconfirmed' }
            $service = Find-BoundService $plan
            if ($service.StartMode -cne 'Disabled') { throw 'initial_service_disable_unconfirmed' }
            if ($service.State -ceq 'Running' -and $service.ProcessId -eq $plan.service_pid -and -not $process.HasExited) {
                if ($service.StopService().ReturnValue -ne 0) { throw 'initial_service_stop_unconfirmed' }
            }
            if (-not $process.WaitForExit(15000)) { throw 'initial_service_stop_unconfirmed' }
            Assert-Retired (Find-BoundService $plan)
            [ordered]@{initial_service_retired=$true;retirement_sha256=(Get-Digest ($inputFile.raw + ':retired:' + $PriorSha256))}|ConvertTo-Json -Compress
            return
        }
        $saved = Read-Scope $plan.state_path ($admins + $identity)
        $record = ConvertFrom-Json -InputObject $saved.raw
        $age = ([DateTime]::UtcNow - $saved.write_time).TotalMilliseconds
        $kind = if ($unpublished) {'initial-wsl-cutover-unpublished'} elseif ($postbootResume) {'initial-wsl-cutover-postboot-resume'} elseif ($postbootUnpublished) {'initial-wsl-cutover-postboot-unpublished'} elseif ($postboot) {'initial-wsl-cutover-postboot'} elseif ($rearming) {'initial-wsl-cutover-rearm'} elseif ($recovering) { 'initial-wsl-cutover-recovery' } else { 'initial-wsl-cutover' }
        if ($record.phase -cne 'serving' -or $record.kind -cne $kind -or $record.schema_version -ne 1 -or
            $record.generation -ne 1 -or $record.sequence -lt 1 -or $record.attempt_id -cne $AttemptId -or
            $record.legacy_plan_sha256 -cne $LegacyPlanSha256 -or $record.controller_plan_sha256 -cne $plan.controller_plan_sha256 -or
            $record.first_write_proof_sha256 -cne $plan.first_write_proof_sha256 -or $record.lease_proof_sha256 -cnotmatch $pattern -or
            $record.proof_elapsed_ms -lt 0 -or $record.proof_elapsed_ms -ge 4000 -or ($age + $record.proof_elapsed_ms) -ge 4800 -or $age -lt 0 -or $age -gt 2900 -or
            $process.HasExited) { throw 'live_authority_lease' }
        if ($recovering) {
            $names = @($record.recovery.PSObject.Properties.Name | Sort-Object -CaseSensitive)
            foreach ($field in @('schema_version','generation','sequence','revision')) {
                if ($record.$field -isnot [int] -and $record.$field -isnot [long]) {
                    throw 'live_authority_recovery_scope'
                }
            }
            if (($names -join ',') -cne 'attempt_id,predecessor_service_sha256,predecessor_terminal_sha256' -or
                $record.revision -ne $expectedRevision -or ($record.proof_elapsed_ms -isnot [int] -and
                    $record.proof_elapsed_ms -isnot [long] -and $record.proof_elapsed_ms -isnot [double] -and $record.proof_elapsed_ms -isnot [decimal]) -or
                [double]::IsNaN([double]$record.proof_elapsed_ms) -or
                $record.recovery.attempt_id -cne $RecoveryAttemptId -or
                $record.recovery.predecessor_service_sha256 -cne $RecoveryPredecessorServiceSha256 -or
                $record.recovery.predecessor_terminal_sha256 -cne $RecoveryPredecessorTerminalSha256 -or
                $service.StartMode -cne 'Manual' -or $service.StartName -ine ('NT SERVICE\' + $plan.service_name)) {
                throw 'live_authority_recovery_scope'
            }
            $service = Find-BoundService $plan
            $confirmedAge = ([DateTime]::UtcNow - $saved.write_time).TotalMilliseconds
            if ($service.State -cne 'Running' -or $service.ProcessId -ne $plan.service_pid -or
                $service.StartMode -cne 'Manual' -or $service.StartName -ine ('NT SERVICE\' + $plan.service_name) -or
                $service.ServiceType -cne 'Own Process' -or $service.DesktopInteract -or $process.HasExited -or
                $confirmedAge -lt 0 -or $confirmedAge -gt 2900 -or
                $confirmedAge + $record.proof_elapsed_ms -ge 4800) {
                throw 'live_authority_recovery_scope'
            }
        }
        if($rearming){
            foreach($key in @('attempt_id','closure_mode','predecessor_args_sha256','predecessor_binding_sha256','predecessor_terminal_sha256')){
                if($record.rearm.$key -isnot [string]){throw 'live_authority_rearm_scope'}
            }
            $rearmNames=@($record.rearm.PSObject.Properties.Name|Sort-Object -CaseSensitive)
            if(($rearmNames -join ',') -cne 'attempt_id,closure_mode,predecessor_args_sha256,predecessor_binding_sha256,predecessor_terminal_sha256,schema_version' -or
               ($record.rearm.schema_version -isnot [int] -and $record.rearm.schema_version -isnot [long]) -or
               $record.rearm.schema_version -ne 1 -or $record.rearm.closure_mode -cne 'windows-offline-v1' -or
               $record.rearm.attempt_id -cne $RearmAttemptId -or
               $record.rearm.predecessor_terminal_sha256 -cne $RearmPredecessorTerminalSha256 -or
               $record.rearm.predecessor_binding_sha256 -cne $RearmPredecessorBindingSha256 -or
               $record.rearm.predecessor_args_sha256 -cne $RearmPredecessorArgsSha256){throw 'live_authority_rearm_scope'}
        }
        if($postboot){
            $expected=[ordered]@{schema_version=1;attempt_id=$PostbootAttemptId;predecessor_terminal_sha256=$PostbootPredecessorTerminalSha256;
                predecessor_binding_sha256=$PostbootPredecessorBindingSha256;predecessor_args_sha256=$PostbootPredecessorArgsSha256;
                predecessor_seal_sha256=$PostbootPredecessorSealSha256;previous_windows_boot_sha256=$PostbootPreviousWindowsBootSha256;closure_mode='windows-offline-v1'}
            if($postbootTagged){$expected.predecessor_kind=$PostbootPredecessorKind}
            if($postbootResume){$expected.predecessor_plan_sha256=$PostbootPredecessorPlanSha256;$expected.stopped_attempt_id=$PostbootStoppedAttemptId;$expected.stopped_receipt_sha256=$PostbootStoppedReceiptSha256}
            $names=@($record.postboot.PSObject.Properties.Name|Sort-Object -CaseSensitive)
            if(($names -join ',') -cne (@($expected.Keys|Sort-Object -CaseSensitive) -join ',') -or
               ($record.postboot.schema_version -isnot [int] -and $record.postboot.schema_version -isnot [long]) -or $record.postboot.schema_version -ne 1){throw 'live_authority_postboot_scope'}
            foreach($key in $expected.Keys){
                if($key -ceq 'schema_version'){continue}
                if($record.postboot.$key -isnot [string] -or $record.postboot.$key -cne $expected[$key]){throw 'live_authority_postboot_scope'}
            }
        }
        if($unpublished){
            $expected=[ordered]@{schema_version=1;attempt_id=$UnpublishedAttemptId;predecessor_terminal_sha256=$UnpublishedPredecessorTerminalSha256;
                predecessor_args_sha256=$UnpublishedPredecessorArgsSha256;predecessor_plan_sha256=$UnpublishedPredecessorPlanSha256;
                predecessor_seal_sha256=$UnpublishedPredecessorSealSha256;closure_mode='windows-offline-v1'}
            $names=@($record.unpublished.PSObject.Properties.Name|Sort-Object -CaseSensitive)
            if(($names -join ',') -cne 'attempt_id,closure_mode,predecessor_args_sha256,predecessor_plan_sha256,predecessor_seal_sha256,predecessor_terminal_sha256,schema_version' -or
               ($record.unpublished.schema_version -isnot [int] -and $record.unpublished.schema_version -isnot [long]) -or $record.unpublished.schema_version -ne 1){throw 'live_authority_unpublished_scope'}
            foreach($key in $expected.Keys){
                if($key -ceq 'schema_version'){continue}
                if($record.unpublished.$key -isnot [string] -or $record.unpublished.$key -cne $expected[$key]){throw 'live_authority_unpublished_scope'}
            }
        }
        if ($RecoveryContinuity) {
            # The protected binding fixes the original published proof and SCM
            # incarnation. Fresh validated heartbeats may advance around a DB read.
            [ordered]@{windows_generation=1;
                service_incarnation_sha256=(Get-Digest ($inputFile.raw + ':continuity:' + $PriorSha256));
                heartbeat_sequence=$record.sequence;live_lease_sha256=$record.lease_proof_sha256}|ConvertTo-Json -Compress
            return
        }
        [ordered]@{windows_generation=1;windows_generation_sha256=(Get-Digest ($inputFile.raw + ':' + $saved.raw + ':' + $PriorSha256));
            live_lease_sha256=$record.lease_proof_sha256}|ConvertTo-Json -Compress
    } finally { $process.Dispose() }
} catch { exit 1 }
