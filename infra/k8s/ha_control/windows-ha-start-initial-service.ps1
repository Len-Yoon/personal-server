# Starts the existing manual controller after protected activation inputs have been staged.
param(
    [string]$ServiceName,
    [string]$ServiceHostSha256,
    [string]$PlanPath,
    [string]$PlanSha256,
    [string]$ApprovalPath,
    [string]$ApprovalSha256,
    [string]$BindingPath,
    [string]$PreflightPath,
    [string]$PreflightSha256,
    [string]$ScmVerifierPath,
    [string]$ExpectedActivationIntentSha256,
    [string]$RecoveryAttemptId,
    [string]$RecoveryReservationSha256,
    [switch]$StartTransition,
    [switch]$VerifyPreparedTransition,
    [switch]$PublishTransitionArgs,
    [string]$PreviousArgsSha256,
    [switch]$CheckLeaseClosed,
    [switch]$RestoreClosureArgs,
    [switch]$ObserveClosureCheck,
    [string]$RejoinMode,
    [string]$RejoinRequestId,
    [switch]$RestoreRejoinArgs,
    [switch]$InspectRejoinStage,
    [switch]$ObserveTransition,
    [switch]$StopTransition,
    [string]$ExpectedServicePid,
    [string]$ExpectedServiceCreatedTicks,
    [switch]$Go
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$hashPattern = '\A[a-f0-9]{64}\z'

function Assert-RecoveryStartScope($Plan,$Approval,[string]$Attempt,[string]$ReservationSha,
                                   [string]$Name,[string]$HostPath,[string]$Parent){
    $recovery=$Plan.initial_cutover_recovery
    $postboot='initial_recovery_postboot' -cin $Plan.PSObject.Properties.Name
    $rearming='initial_recovery_rearm' -cin $Plan.PSObject.Properties.Name
    $unpublished='initial_unpublished_recovery' -cin $Plan.PSObject.Properties.Name
    if(($postboot -and $rearming) -or ($unpublished -and ($postboot -or $rearming))){throw 'initial_service_postboot_scope'}
    $identity='N100HA_'+$Attempt.Substring(0,24)
    $root=Join-Path $Parent $identity
    $approvalFields=@($Approval.PSObject.Properties.Name|Sort-Object -CaseSensitive)
    foreach($field in @('action','authority_kind','installation_id','installed_at','plan_sha256','source_commit')){
        if($Approval.$field -isnot [string]){throw 'initial_service_recovery_scope'}
    }
    if(($approvalFields -join ',') -cne 'action,authority_kind,installation_id,installed_at,plan_sha256,schema_version,source_commit' -or
        ($Approval.schema_version -isnot [int] -and $Approval.schema_version -isnot [long]) -or
        $Approval.schema_version -ne 1 -or $Approval.authority_kind -cne 'installed-runtime' -or
        $Approval.installation_id -cnotmatch $hashPattern -or $Approval.plan_sha256 -cnotmatch $hashPattern -or
        $Approval.source_commit -cnotmatch '\A[a-f0-9]{40}\z' -or
        $Approval.installed_at -cnotmatch '\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z\z' -or
        [DateTimeOffset]::Parse($Approval.installed_at,[Globalization.CultureInfo]::InvariantCulture) -ge [DateTimeOffset]::UtcNow){
        throw 'initial_service_recovery_scope'
    }
    $expectedHost=if($unpublished){Join-Path $root ('rearm-'+$Plan.initial_unpublished_recovery.attempt_id+'\host\windows-service-host.exe')}elseif($postboot){Join-Path $root ('postboot-'+$Plan.initial_recovery_postboot.attempt_id+'\host\windows-service-host.exe')}elseif($rearming){Join-Path $root ('rearm-'+$Plan.initial_recovery_rearm.attempt_id+'\host\windows-service-host.exe')}else{Join-Path $root 'host\windows-service-host.exe'}
    $expectedAction=if($unpublished){'ha-recover-unpublished-initial-writer'}elseif($postboot){'ha-resume-initial-writer'}elseif($rearming){'ha-rearm-initial-writer'}else{'ha-recover-initial-writer'}
    if($Parent -cne 'C:\ProgramData' -or $Plan.installation_id -cnotmatch $hashPattern -or
        $Attempt.Substring(0,24) -ceq $Plan.installation_id.Substring(0,24) -or
        $Name -cne ($identity+'_controller') -or
        $HostPath -cne $expectedHost -or
        $Approval.action -cne $expectedAction -or
        $Plan.initial_service_start.reservation_receipt_sha256 -cne $ReservationSha -or
        (@($recovery.PSObject.Properties.Name | Sort-Object -CaseSensitive) -join ',') -cne
            'attempt_id,predecessor_service_sha256,predecessor_terminal_sha256' -or
        $recovery.attempt_id -cne $Attempt -or $recovery.attempt_id -ceq $Plan.initial_cutover_handoff.attempt_id -or
        $recovery.predecessor_service_sha256 -cnotmatch $hashPattern -or
        $recovery.predecessor_terminal_sha256 -cnotmatch $hashPattern){
        throw 'initial_service_recovery_scope'
    }
    $receipt=Read-PinnedJson (Join-Path $root 'reservation-receipt.json') $ReservationSha
    if($receipt.format -cne 'fresh-recovery-service-reservation-receipt-v1' -or
        $receipt.installation_id -cne $Plan.installation_id -or $receipt.recovery_attempt_id -cne $Attempt){
        throw 'initial_service_recovery_scope'
    }
    foreach($field in @('ready_to_start','runtime_authority_issued','service_started','args_present','namespace_changed')){
        if($receipt.$field -isnot [bool] -or $receipt.$field){throw 'initial_service_recovery_scope'}
    }
    if($receipt.ownership_receipt_sha256 -cnotmatch $hashPattern -or
        $receipt.service_config_sha256 -cnotmatch $hashPattern){throw 'initial_service_recovery_scope'}
    $ownership=Read-PinnedJson (Join-Path $root 'namespace-service-ownership.json') $receipt.ownership_receipt_sha256
    if($ownership.format -cne 'ha-owned-recovery-service-v1' -or
        $ownership.installation_id -cne $Plan.installation_id -or $ownership.recovery_attempt_id -cne $Attempt -or
        $ownership.service_name -cne $Name -or $ownership.service_sid -cne $Plan.windows_enforced.service_sid -or
        $ownership.service_image_sha256 -cne $Plan.windows_enforced.service_image_sha256 -or
        $ownership.service_config_sha256 -cne $receipt.service_config_sha256 -or
        $ownership.predecessor_service_sha256 -cne $recovery.predecessor_service_sha256 -or
        $ownership.predecessor_terminal_sha256 -cne $recovery.predecessor_terminal_sha256 -or
        $ownership.new_owned_service -isnot [bool] -or -not $ownership.new_owned_service -or
        $ownership.scm_registration_complete -isnot [bool] -or -not $ownership.scm_registration_complete){
        throw 'initial_service_recovery_scope'
    }
}

function Assert-RearmEnvelope($Rearm){
    foreach($key in @('attempt_id','closure_mode','predecessor_args_sha256','predecessor_binding_sha256','predecessor_terminal_sha256')){
        if($Rearm.$key -isnot [string]){throw 'initial_service_rearm_scope'}
    }
    foreach($key in @('attempt_id','predecessor_args_sha256','predecessor_binding_sha256','predecessor_terminal_sha256')){
        if($Rearm.$key -cnotmatch $hashPattern){throw 'initial_service_rearm_scope'}
    }
}

function Assert-RearmSealedPredecessor($Plan,[string]$Root){
    if('initial_unpublished_recovery' -cin $Plan.PSObject.Properties.Name){throw 'initial_service_rearm_scope'}
    $r=$Plan.initial_recovery_rearm
    Assert-RearmEnvelope $r
    if((@($r.PSObject.Properties.Name|Sort-Object -CaseSensitive) -join ',') -cne
       'attempt_id,closure_mode,predecessor_args_sha256,predecessor_binding_sha256,predecessor_terminal_sha256,schema_version' -or
       ($r.schema_version -isnot [int] -and $r.schema_version -isnot [long]) -or $r.schema_version -ne 1 -or
       $r.closure_mode -cne 'windows-offline-v1' -or $r.attempt_id -cnotmatch $hashPattern -or
       $r.attempt_id -ceq $Plan.initial_cutover_recovery.attempt_id -or
       $r.attempt_id -ceq $Plan.initial_cutover_handoff.attempt_id){throw 'initial_service_rearm_scope'}
    $old=Read-PinnedJson (Join-Path $Root 'rearm-predecessor-binding.json') $r.predecessor_binding_sha256
    $terminal=Read-PinnedJson (Join-Path $Root 'rearm-predecessor-state.json') $r.predecessor_terminal_sha256
    $argsFile=Join-Path $Root 'rearm-predecessor-args.txt'
    if((Get-FileHash -LiteralPath $argsFile -Algorithm SHA256).Hash.ToLowerInvariant() -cne $r.predecessor_args_sha256){throw 'initial_service_rearm_scope'}
    $sealPath=Join-Path $Root 'rearm-predecessor-seal.json'
    $seal=Read-PinnedJson $sealPath ((Get-FileHash -LiteralPath $sealPath -Algorithm SHA256).Hash.ToLowerInvariant())
    $planPin=(Get-FileHash -LiteralPath (Join-Path $Root 'plan.json') -Algorithm SHA256).Hash.ToLowerInvariant()
    if($seal.plan_sha256 -cne $planPin -or $seal.schema_version -ne 1 -or
       (ConvertTo-Json $seal.rearm -Compress) -cne (ConvertTo-Json $r -Compress) -or
       $seal.windows_boot_sha256 -cne $Plan.windows_initial_observer.windows_boot_sha256 -or
       $seal.service.state -cne 'Stopped' -or $seal.service.start_mode -cne 'Disabled' -or $seal.service.process_id -ne 0 -or
       $seal.service.service_name -cne $Plan.windows_enforced.service_name -or
       $seal.service.service_sid -cne $Plan.windows_enforced.service_sid -or
       $seal.service.service_image_path_sha256 -cne $old.service_image_path_sha256 -or
       $old.controller_plan_sha256 -cne $terminal.controller_plan_sha256 -or $old.controller_plan_sha256 -ceq $planPin -or
       $terminal.kind -cne 'initial-wsl-cutover-recovery' -or $terminal.revision -ne 4 -or
       $terminal.phase -cnotin @('lease_lost','local_revoke_observed') -or $terminal.sequence -lt 1 -or
       (ConvertTo-Json $terminal.recovery -Compress) -cne (ConvertTo-Json $Plan.initial_cutover_recovery -Compress)){
        throw 'initial_service_rearm_scope'
    }
}

function Seal-HaInitialRearmPredecessor([string]$PlanPath,[string]$PlanSha256){
    $plan=Read-PinnedJson $PlanPath $PlanSha256
    $r=$plan.initial_recovery_rearm
    Assert-RearmEnvelope $r
    $root=[IO.Path]::GetDirectoryName($PlanPath)
    $binding=Read-PinnedJson (Join-Path $root 'rearm-predecessor-binding.json') $r.predecessor_binding_sha256
    $terminal=Read-PinnedJson (Join-Path $root 'rearm-predecessor-state.json') $r.predecessor_terminal_sha256
    $oldArgs=Join-Path $root 'rearm-predecessor-args.txt'
    if((Get-FileHash -LiteralPath $oldArgs -Algorithm SHA256).Hash.ToLowerInvariant() -cne $r.predecessor_args_sha256){throw 'initial_service_rearm_scope'}
    $service=Get-CimInstance Win32_Service -Filter ("Name='"+$plan.windows_enforced.service_name+"'") -OperationTimeoutSec 5
    if($null -eq $service -or $service.State -cne 'Stopped' -or $service.StartMode -cne 'Disabled' -or $service.ProcessId -ne 0 -or
       $service.Name -cne $binding.service_name -or $service.StartName -ine ('NT SERVICE\'+$service.Name) -or
       $service.ServiceType -cne 'Own Process' -or $service.DesktopInteract -or
       $service.PathName -cnotmatch '\A"[^"\r\n]+"\z'){throw 'initial_service_rearm_scope'}
    Assert-Executable $service.PathName.Trim('"') $plan.windows_enforced.service_image_sha256
    $sha=[Security.Cryptography.SHA256]::Create()
    try{$image=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($service.PathName))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
    if($image -cne $binding.service_image_path_sha256){throw 'initial_service_rearm_scope'}
    $boot=(Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 5).LastBootUpTime.ToUniversalTime().Ticks.ToString()
    $sha=[Security.Cryptography.SHA256]::Create()
    try{$bootPin=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($boot))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
    if($bootPin -cne $plan.windows_initial_observer.windows_boot_sha256){throw 'initial_service_rearm_scope'}
    # Hold the configured predecessor's actual files read-only through sealing.
    # Copies alone cannot establish which stopped incarnation is being handed over.
    $held=[Collections.Generic.List[IDisposable]]::new()
    try{
        $actualArgs=Join-Path ([IO.Path]::GetDirectoryName($service.PathName.Trim('"'))) 'service.args'
        $argsRaw=[IO.File]::ReadAllBytes($actualArgs)
        $argsText=[Text.UTF8Encoding]::new($false,$true).GetString($argsRaw)
        $rows=$argsText.Split([char]10)
        $keys=@('format','service_name','service_sid','python_path','python_sha256','launcher_path','launcher_sha256',
                'plan_path','plan_sha256','approval_path','approval_sha256','mode','host_sha256')
        if($rows.Count -ne 14 -or $rows[-1] -cne '' -or $argsText.Contains([char]13) -or $argsText.Contains([char]0)){
            throw 'initial_service_rearm_scope'
        }
        $previousArgs=@{}
        for($i=0;$i -lt $keys.Count;$i++){
            $prefix=$keys[$i]+'='
            if(-not $rows[$i].StartsWith($prefix,[StringComparison]::Ordinal)) {throw 'initial_service_rearm_scope'}
            $previousArgs[$keys[$i]]=$rows[$i].Substring($prefix.Length)
        }
        if($previousArgs.format -cne 'enforced-service-v1' -or $previousArgs.mode -cne 'serve' -or
           $previousArgs.service_name -cne $service.Name -or $previousArgs.service_sid -cne $plan.windows_enforced.service_sid -or
           $previousArgs.plan_sha256 -cne $binding.controller_plan_sha256 -or
           $previousArgs.host_sha256 -cne $plan.windows_enforced.service_image_sha256){throw 'initial_service_rearm_scope'}
        $oldRoot=[IO.Path]::GetDirectoryName($previousArgs.plan_path)
        if($binding.state_path -cne (Join-Path $oldRoot 'journal\controller-state.json')){throw 'initial_service_rearm_scope'}
        $actualBinding=Join-Path $oldRoot ('initial-recovery-service-'+$plan.initial_cutover_recovery.attempt_id+'.json')
        $actual=@(@{path=$actualArgs;pin=$r.predecessor_args_sha256},
                  @{path=$previousArgs.plan_path;pin=$binding.controller_plan_sha256},
                  @{path=$binding.state_path;pin=$r.predecessor_terminal_sha256},
                  @{path=$actualBinding;pin=$r.predecessor_binding_sha256})
        foreach($entry in $actual){
            $item=Get-Item -LiteralPath $entry.path -Force
            if($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {throw 'initial_service_rearm_scope'}
            for($parent=$item.Directory;$null -ne $parent;$parent=$parent.Parent){
                if($parent.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'initial_service_rearm_scope'}
            }
            $handle=[IO.File]::Open($item.FullName,'Open','Read','Read')
            $held.Add($handle)
            $hasher=[Security.Cryptography.SHA256]::Create()
            try{$pin=[BitConverter]::ToString($hasher.ComputeHash($handle)).Replace('-','').ToLowerInvariant()}finally{$hasher.Dispose()}
            if($pin -cne $entry.pin){throw 'initial_service_rearm_scope'}
        }
        $confirmed=Get-CimInstance Win32_Service -Filter ("Name='"+$service.Name+"'") -OperationTimeoutSec 5
        if($confirmed.State -cne 'Stopped' -or $confirmed.StartMode -cne 'Disabled' -or $confirmed.ProcessId -ne 0 -or
           $confirmed.PathName -cne $service.PathName -or $confirmed.StartName -cne $service.StartName -or
           $confirmed.ServiceType -cne 'Own Process' -or $confirmed.DesktopInteract){throw 'initial_service_rearm_scope'}
    $receipt=[ordered]@{schema_version=1;plan_sha256=$PlanSha256;rearm=$r;windows_boot_sha256=$bootPin;
        service=[ordered]@{service_name=$service.Name;service_sid=$plan.windows_enforced.service_sid;state='Stopped';
                          start_mode='Disabled';process_id=0;service_image_path_sha256=$image}}
    $security=[Security.AccessControl.FileSecurity]::new()
    $admins=[Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $security.SetOwner($admins);$security.SetAccessRuleProtection($true,$false)
    foreach($sid in @('S-1-5-18','S-1-5-32-544')){$security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),'FullControl','Allow'))}
    $security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($plan.windows_enforced.service_sid),'Read','Allow'))
    $path=Join-Path $root 'rearm-predecessor-seal.json'
    $raw=[Text.UTF8Encoding]::new($false).GetBytes((ConvertTo-Json $receipt -Depth 8 -Compress))
    $file=[IO.FileStream]::new($path,[IO.FileMode]::CreateNew,[Security.AccessControl.FileSystemRights]'WriteData,Synchronize',
        [IO.FileShare]::None,4096,[IO.FileOptions]::WriteThrough,$security)
    try{$file.Write($raw,0,$raw.Length);$file.Flush($true)}finally{$file.Dispose()}
    Assert-RearmSealedPredecessor $plan $root
    }finally{foreach($handle in $held){$handle.Dispose()}}
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

function Assert-UnpublishedTcbPath([string]$Path){
    $node=Get-Item -LiteralPath $Path -Force
    while($null -ne $node){
        if($node.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'initial_service_unpublished_custody'}
        $acl=[Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $node.FullName).Sddl)
        $owner=$acl.Owner.Value;$rootOwner=$node.FullName -ieq 'C:\' -and $owner -ceq 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
        if(($owner -notin @('S-1-5-18','S-1-5-32-544') -and -not $rootOwner) -or $null -eq $acl.DiscretionaryAcl -or $acl.DiscretionaryAcl.Count -lt 1){throw 'initial_service_unpublished_custody'}
        $mask=if($node -is [IO.DirectoryInfo]){269287488}else{1343029590}
        foreach($ace in $acl.DiscretionaryAcl){
            if($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback){throw 'initial_service_unpublished_custody'}
            if(([int]$ace.AceFlags -band 8) -ne 0){continue}
            if($ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed -and ($ace.AccessMask -band $mask) -ne 0 -and $ace.SecurityIdentifier.Value -notin @('S-1-5-18','S-1-5-32-544')){throw 'initial_service_unpublished_custody'}
        }
        $node=if($node -is [IO.DirectoryInfo]){$node.Parent}else{$node.Directory}
    }
}

function Assert-UnpublishedBindingAbsent([string]$Path){
    try{$null=Get-Item -LiteralPath $Path -Force -ErrorAction Stop;throw 'initial_service_unpublished_binding_present'}
    catch [Management.Automation.ItemNotFoundException]{}
}
function Assert-UnpublishedPreSeal($Plan,$Old,$Pre,$Value){
    foreach($key in @('windows_boot_sha256','predecessor_terminal_sha256','predecessor_args_sha256','predecessor_plan_sha256')){
        if($Pre.$key -isnot [string] -or $Pre.$key -cnotmatch $hashPattern){throw 'initial_service_unpublished_scope'}
    }
    if($Pre.binding_path -isnot [string]){throw 'initial_service_unpublished_scope'}
    foreach($key in @('guest_boot_sha256','hold_sha256')){if($Pre.guest.$key -isnot [string] -or $Pre.guest.$key -cnotmatch $hashPattern){throw 'initial_service_unpublished_scope'}}
    foreach($key in @('service_name','service_sid','state','start_mode','service_image_path_sha256')){if($Pre.service.$key -isnot [string]){throw 'initial_service_unpublished_scope'}}
    $keys=@('binding_absent','binding_path','guest','predecessor_args_sha256','predecessor_plan_sha256','predecessor_terminal_sha256','schema_version','service','windows_boot_sha256')
    if((@($Pre.PSObject.Properties.Name|Sort-Object -CaseSensitive) -join ',') -cne ($keys -join ',') -or
       ($Pre.schema_version -isnot [int] -and $Pre.schema_version -isnot [long]) -or $Pre.schema_version -ne 1 -or
       $Pre.windows_boot_sha256 -cne $Plan.windows_initial_observer.windows_boot_sha256 -or
       $Pre.predecessor_terminal_sha256 -cne $Value.predecessor_terminal_sha256 -or $Pre.predecessor_args_sha256 -cne $Value.predecessor_args_sha256 -or
       $Pre.predecessor_plan_sha256 -cne $Value.predecessor_plan_sha256 -or $Pre.binding_path -cne $Old.initial_service_start.binding_path -or
       $Pre.binding_absent -isnot [bool] -or -not $Pre.binding_absent -or
       (@($Pre.guest.PSObject.Properties.Name|Sort-Object -CaseSensitive) -join ',') -cne 'app_start_intent_absent,guest_boot_sha256,hold_sha256' -or
       $Pre.guest.app_start_intent_absent -isnot [bool] -or -not $Pre.guest.app_start_intent_absent -or
       $Pre.guest.guest_boot_sha256 -cne $Plan.windows_recovery_observers.guest_boot_sha256 -or $Pre.guest.hold_sha256 -cne $Plan.windows_recovery_observers.hold_sha256 -or
       (@($Pre.service.PSObject.Properties.Name|Sort-Object -CaseSensitive) -join ',') -cne 'process_id,service_image_path_sha256,service_name,service_sid,start_mode,state' -or
       $Pre.service.service_name -cne $Plan.windows_enforced.service_name -or $Pre.service.service_sid -cne $Plan.windows_enforced.service_sid -or
       $Pre.service.state -cne 'Stopped' -or $Pre.service.start_mode -cne 'Disabled' -or
       ($Pre.service.process_id -isnot [int] -and $Pre.service.process_id -isnot [long]) -or $Pre.service.process_id -ne 0 -or
       $Pre.service.service_image_path_sha256 -cne $Old.initial_service_start.service_image_path_sha256){throw 'initial_service_unpublished_scope'}
}

function Assert-UnpublishedEnvelope($Value){
    $keys=@('attempt_id','closure_mode','predecessor_args_sha256','predecessor_plan_sha256','predecessor_seal_sha256','predecessor_terminal_sha256','schema_version')
    if((@($Value.PSObject.Properties.Name|Sort-Object -CaseSensitive) -join ',') -cne ($keys -join ',') -or
       ($Value.schema_version -isnot [int] -and $Value.schema_version -isnot [long]) -or $Value.schema_version -ne 1 -or
       $Value.closure_mode -isnot [string] -or $Value.closure_mode -cne 'windows-offline-v1'){throw 'initial_service_unpublished_scope'}
    foreach($key in $keys){if($key -notin @('schema_version','closure_mode') -and ($Value.$key -isnot [string] -or $Value.$key -cnotmatch $hashPattern)){throw 'initial_service_unpublished_scope'}}
}
function Assert-UnpublishedPrior($Plan,$Old,$Terminal,$Value){
    $raw=[Text.UTF8Encoding]::new($false).GetBytes((ConvertTo-Json $Terminal -Depth 12 -Compress))
    $h=[Security.Cryptography.SHA256]::Create();try{$pin=[BitConverter]::ToString($h.ComputeHash($raw)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}
    $numbers=Get-InitialPredecessorNumbers $raw $pin $false
    $field=if($Terminal.kind -ceq 'initial-wsl-cutover-rearm'){'rearm'}else{'unpublished'}
    $oldField=if($field -ceq 'rearm'){'initial_recovery_rearm'}else{'initial_unpublished_recovery'}
    $other=if($field -ceq 'rearm'){'initial_unpublished_recovery'}else{'initial_recovery_rearm'}
    foreach($key in @('service_name','service_sid','service_image_sha256')){
        if($Old.windows_enforced.$key -isnot [string] -or $Old.windows_enforced.$key -cne $Plan.windows_enforced.$key){throw 'initial_service_unpublished_scope'}
    }
    if($Old.installation_id -isnot [string] -or $Old.installation_id -cnotmatch '\A[a-f0-9]{64}\z' -or $Old.installation_id -cne $Plan.installation_id -or
       $Old.source_commit -isnot [string] -or $Old.source_commit -cnotmatch '\A[a-f0-9]{40}\z' -or
       ($field -ceq 'rearm' -and $Old.source_commit -cne '7cdb958bd2db88d60be40336d5d7a172a94f4115') -or
       'initial_recovery_postboot' -cin $Old.PSObject.Properties.Name -or $other -cin $Old.PSObject.Properties.Name -or $oldField -cnotin $Old.PSObject.Properties.Name -or
       $Terminal.controller_plan_sha256 -cne $Value.predecessor_plan_sha256 -or
       (ConvertTo-Json $Terminal.$field -Compress) -cne (ConvertTo-Json $Old.$oldField -Compress) -or
       (ConvertTo-Json $Terminal.recovery -Compress) -cne (ConvertTo-Json $Plan.initial_cutover_recovery -Compress) -or
       (ConvertTo-Json $Old.initial_cutover_recovery -Compress) -cne (ConvertTo-Json $Plan.initial_cutover_recovery -Compress) -or
       (ConvertTo-Json $Old.initial_cutover_handoff -Compress) -cne (ConvertTo-Json $Plan.initial_cutover_handoff -Compress) -or
       $Old.windows_initial_observer.windows_boot_sha256 -cne $Plan.windows_initial_observer.windows_boot_sha256 -or
       $Old.windows_recovery_observers.guest_boot_sha256 -cne $Plan.windows_recovery_observers.guest_boot_sha256 -or
       $Old.windows_recovery_observers.hold_sha256 -cne $Plan.windows_recovery_observers.hold_sha256 -or
       $Value.attempt_id -in @($Old.$oldField.attempt_id,$Plan.initial_cutover_recovery.attempt_id,$Plan.initial_cutover_handoff.attempt_id)){
        throw 'initial_service_unpublished_scope'
    }
    foreach($key in @('attempt_id','legacy_plan_sha256','restore_receipt_sha256','activation_intent_sha256')){
        if($Terminal.$key -cne $Plan.initial_cutover_handoff.$key){throw 'initial_service_unpublished_scope'}
    }
}
function Assert-UnpublishedSealedPredecessor($Plan,[string]$Root){
    if('initial_recovery_rearm' -cin $Plan.PSObject.Properties.Name -or 'initial_recovery_postboot' -cin $Plan.PSObject.Properties.Name){throw 'initial_service_unpublished_scope'}
    foreach($name in @('plan.json','unpublished-predecessor-plan.json','unpublished-predecessor-state.json','unpublished-predecessor-args.txt','unpublished-predecessor-seal.json','unpublished-acceptance-seal.json')){Assert-UnpublishedTcbPath (Join-Path $Root $name)}
    $r=$Plan.initial_unpublished_recovery;Assert-UnpublishedEnvelope $r
    $old=Read-PinnedJson (Join-Path $Root 'unpublished-predecessor-plan.json') $r.predecessor_plan_sha256
    $terminal=Read-PinnedJson (Join-Path $Root 'unpublished-predecessor-state.json') $r.predecessor_terminal_sha256
    Assert-UnpublishedPrior $Plan $old $terminal $r
    if((Get-FileHash -LiteralPath (Join-Path $Root 'unpublished-predecessor-args.txt') -Algorithm SHA256).Hash.ToLowerInvariant() -cne $r.predecessor_args_sha256){throw 'initial_service_unpublished_scope'}
    $pre=Read-PinnedJson (Join-Path $Root 'unpublished-predecessor-seal.json') $r.predecessor_seal_sha256
    Assert-UnpublishedPreSeal $Plan $old $pre $r
    $sealPath=Join-Path $Root 'unpublished-acceptance-seal.json'
    $seal=Read-PinnedJson $sealPath ((Get-FileHash -LiteralPath $sealPath -Algorithm SHA256).Hash.ToLowerInvariant())
    if((@($seal.PSObject.Properties.Name|Sort-Object -CaseSensitive) -join ',') -cne 'plan_sha256,schema_version,service,unpublished,windows_boot_sha256' -or
       ($seal.schema_version -isnot [int] -and $seal.schema_version -isnot [long]) -or $seal.schema_version -ne 1){throw 'initial_service_unpublished_scope'}
    foreach($key in @('plan_sha256','windows_boot_sha256')){if($seal.$key -isnot [string] -or $seal.$key -cnotmatch $hashPattern){throw 'initial_service_unpublished_scope'}}
    $planPin=(Get-FileHash -LiteralPath (Join-Path $Root 'plan.json') -Algorithm SHA256).Hash.ToLowerInvariant()
    if($pre.schema_version -ne 1 -or $pre.windows_boot_sha256 -cne $Plan.windows_initial_observer.windows_boot_sha256 -or
       $pre.predecessor_terminal_sha256 -cne $r.predecessor_terminal_sha256 -or $pre.predecessor_args_sha256 -cne $r.predecessor_args_sha256 -or
       $pre.predecessor_plan_sha256 -cne $r.predecessor_plan_sha256 -or $pre.binding_path -cne $old.initial_service_start.binding_path -or
       $pre.binding_absent -isnot [bool] -or -not $pre.binding_absent -or $pre.guest.app_start_intent_absent -isnot [bool] -or -not $pre.guest.app_start_intent_absent -or
       $pre.guest.guest_boot_sha256 -cne $Plan.windows_recovery_observers.guest_boot_sha256 -or $pre.guest.hold_sha256 -cne $Plan.windows_recovery_observers.hold_sha256 -or
       $pre.service.service_name -cne $Plan.windows_enforced.service_name -or $pre.service.service_sid -cne $Plan.windows_enforced.service_sid -or
       $pre.service.state -cne 'Stopped' -or $pre.service.start_mode -cne 'Disabled' -or $pre.service.process_id -ne 0 -or
       $pre.service.service_image_path_sha256 -cne $old.initial_service_start.service_image_path_sha256 -or
       $seal.schema_version -ne 1 -or $seal.plan_sha256 -cne $planPin -or $seal.windows_boot_sha256 -cne $pre.windows_boot_sha256 -or
       (ConvertTo-Json $seal.unpublished -Compress) -cne (ConvertTo-Json $r -Compress) -or
       (ConvertTo-Json $seal.service -Compress) -cne (ConvertTo-Json $pre.service -Compress)){
        throw 'initial_service_unpublished_scope'
    }
}
function Write-UnpublishedSeal([string]$Path,$Record,[string]$ServiceSid){
    if($Path -cnotmatch '\A[A-Za-z]:\\[A-Za-z0-9_.\\-]+\z' -or $Path -match '\\\.\.?(\\|$)'){throw 'initial_service_unpublished_custody'}
    Assert-UnpublishedTcbPath ([IO.Path]::GetDirectoryName($Path))
    $security=[Security.AccessControl.FileSecurity]::new();$security.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'));$security.SetAccessRuleProtection($true,$false)
    foreach($sid in @('S-1-5-18','S-1-5-32-544')){$security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),'FullControl','Allow'))}
    $security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($ServiceSid),'Read','Allow'))
    $raw=[Text.UTF8Encoding]::new($false).GetBytes((ConvertTo-Json $Record -Depth 12 -Compress))
    $file=[IO.FileStream]::new($Path,[IO.FileMode]::CreateNew,[Security.AccessControl.FileSystemRights]'WriteData,Synchronize',[IO.FileShare]::None,4096,[IO.FileOptions]::WriteThrough,$security)
    try{$file.Write($raw,0,$raw.Length);$file.Flush($true)}finally{$file.Dispose()}
}
function Assert-UnpublishedPriorApproval($Old,$Approval,[string]$PlanPin){
    if((@($Approval.PSObject.Properties.Name|Sort-Object -CaseSensitive) -join ',') -cne 'action,authority_kind,installation_id,installed_at,plan_sha256,schema_version,source_commit' -or
       ($Approval.schema_version -isnot [int] -and $Approval.schema_version -isnot [long]) -or $Approval.schema_version -ne 1){throw 'initial_service_unpublished_approval'}
    foreach($key in @('action','authority_kind','installation_id','installed_at','plan_sha256','source_commit')){if($Approval.$key -isnot [string]){throw 'initial_service_unpublished_approval'}}
    $action=if('initial_unpublished_recovery' -cin $Old.PSObject.Properties.Name){'ha-recover-unpublished-initial-writer'}else{'ha-rearm-initial-writer'}
    if($Approval.action -cne $action -or $Approval.authority_kind -cne 'installed-runtime' -or
       $Approval.installation_id -cnotmatch '\A[a-f0-9]{64}\z' -or $Approval.installation_id -cne $Old.installation_id -or
       $Approval.plan_sha256 -cne $PlanPin -or $Approval.source_commit -cne $Old.source_commit -or $Approval.source_commit -cnotmatch '\A[a-f0-9]{40}\z' -or
       $Approval.installed_at -cnotmatch '\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z\z' -or
       [DateTimeOffset]::Parse($Approval.installed_at,[Globalization.CultureInfo]::InvariantCulture) -ge [DateTimeOffset]::UtcNow){throw 'initial_service_unpublished_approval'}
}

function Seal-HaInitialUnpublishedAcceptance([string]$PlanPath,[string]$PlanSha256){
    Assert-UnpublishedTcbPath $PlanPath
    $plan=Read-PinnedJson $PlanPath $PlanSha256;$r=$plan.initial_unpublished_recovery;Assert-UnpublishedEnvelope $r
    $root=[IO.Path]::GetDirectoryName($PlanPath)
    foreach($name in @('unpublished-predecessor-plan.json','unpublished-predecessor-state.json','unpublished-predecessor-args.txt','unpublished-predecessor-seal.json')){Assert-UnpublishedTcbPath (Join-Path $root $name)}
    $old=Read-PinnedJson (Join-Path $root 'unpublished-predecessor-plan.json') $r.predecessor_plan_sha256
    $terminal=Read-PinnedJson (Join-Path $root 'unpublished-predecessor-state.json') $r.predecessor_terminal_sha256
    Assert-UnpublishedPrior $plan $old $terminal $r
    $pre=Read-PinnedJson (Join-Path $root 'unpublished-predecessor-seal.json') $r.predecessor_seal_sha256
    Assert-UnpublishedPreSeal $plan $old $pre $r
    $service=Get-CimInstance Win32_Service -Filter ("Name='"+$plan.windows_enforced.service_name+"'") -OperationTimeoutSec 5
    if($null -eq $service -or $service.State -cne 'Stopped' -or $service.StartMode -cne 'Disabled' -or $service.ProcessId -ne 0 -or
       $service.StartName -ine ('NT SERVICE\'+$service.Name) -or $service.ServiceType -cne 'Own Process' -or $service.DesktopInteract -or $service.PathName -cnotmatch '\A"[^"\r\n]+"\z') {throw 'initial_service_unpublished_scope'}
    Assert-Executable $service.PathName.Trim('"') $old.windows_enforced.service_image_sha256
    $sha=[Security.Cryptography.SHA256]::Create()
    try{$image=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($service.PathName))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
    if($image -cne $old.initial_service_start.service_image_path_sha256 -or $image -cne $pre.service.service_image_path_sha256){throw 'initial_service_unpublished_scope'}
    $actualArgs=Join-Path ([IO.Path]::GetDirectoryName($service.PathName.Trim('"'))) 'service.args'
    $argsRaw=[IO.File]::ReadAllBytes($actualArgs);$sha=[Security.Cryptography.SHA256]::Create()
    try{$argsPin=[BitConverter]::ToString($sha.ComputeHash($argsRaw)).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
    if($argsPin -cne $r.predecessor_args_sha256){throw 'initial_service_unpublished_scope'}
    $rows=([Text.UTF8Encoding]::new($false,$true).GetString($argsRaw)).Split([char]10)
    if($rows.Count -ne 14 -or $rows[7] -cnotmatch '\Aplan_path=[A-Za-z]:\\[A-Za-z0-9_.\\-]+\z' -or $rows[8] -cne ('plan_sha256='+$r.predecessor_plan_sha256)){throw 'initial_service_unpublished_scope'}
    $settings=@{};$keys=@('format','service_name','service_sid','python_path','python_sha256','launcher_path','launcher_sha256','plan_path','plan_sha256','approval_path','approval_sha256','mode','host_sha256')
    if($rows[-1] -cne '' -or ([Text.UTF8Encoding]::new($false,$true).GetString($argsRaw)).Contains([char]13)){throw 'initial_service_unpublished_scope'}
    for($i=0;$i -lt $keys.Count;$i++){$prefix=$keys[$i]+'=';if(-not $rows[$i].StartsWith($prefix,[StringComparison]::Ordinal)){throw 'initial_service_unpublished_scope'};$settings[$keys[$i]]=$rows[$i].Substring($prefix.Length)}
    if($settings.format -cne 'enforced-service-v1' -or $settings.mode -cne 'serve' -or $settings.service_name -cne $service.Name -or $settings.service_sid -cne $plan.windows_enforced.service_sid -or $settings.host_sha256 -cne $old.windows_enforced.service_image_sha256 -or $settings.approval_sha256 -cnotmatch $hashPattern){throw 'initial_service_unpublished_scope'}
    $oldPlanPath=$settings.plan_path;$oldRoot=[IO.Path]::GetDirectoryName($oldPlanPath)
    Assert-UnpublishedTcbPath $settings.approval_path
    $oldApproval=Read-PinnedJson $settings.approval_path $settings.approval_sha256
    Assert-UnpublishedPriorApproval $old $oldApproval $r.predecessor_plan_sha256
    Assert-UnpublishedTcbPath $settings.python_path;Assert-Executable $settings.python_path $settings.python_sha256
    if([string](Get-AuthenticodeSignature -LiteralPath $settings.python_path).Status -cne 'Valid'){throw 'initial_service_unpublished_runtime_signature'}
    Assert-UnpublishedTcbPath $settings.launcher_path
    if((Get-FileHash -LiteralPath $settings.launcher_path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $settings.launcher_sha256){throw 'initial_service_unpublished_runtime_pin'}
    $bindingPath=Join-Path $oldRoot ('initial-recovery-service-'+$plan.initial_cutover_recovery.attempt_id+'.json')
    if($bindingPath -cne $old.initial_service_start.binding_path){throw 'initial_service_unpublished_scope'}
    Assert-UnpublishedBindingAbsent $bindingPath
    $held=[Collections.Generic.List[IDisposable]]::new()
    try{
        foreach($entry in @(@{path=$actualArgs;pin=$r.predecessor_args_sha256},@{path=$oldPlanPath;pin=$r.predecessor_plan_sha256},
                           @{path=(Join-Path $oldRoot 'journal\controller-state.json');pin=$r.predecessor_terminal_sha256},
                           @{path=$settings.approval_path;pin=$settings.approval_sha256},@{path=$settings.python_path;pin=$settings.python_sha256},@{path=$settings.launcher_path;pin=$settings.launcher_sha256})){
            $item=Get-Item -LiteralPath $entry.path -Force
            if($item -isnot [IO.FileInfo] -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'initial_service_unpublished_scope'}
            for($parent=$item.Directory;$null -ne $parent;$parent=$parent.Parent){if($parent.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'initial_service_unpublished_scope'}}
            $handle=[IO.File]::Open($item.FullName,'Open','Read','Read');$held.Add($handle);$sha=[Security.Cryptography.SHA256]::Create()
            try{$pin=[BitConverter]::ToString($sha.ComputeHash($handle)).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
            if($pin -cne $entry.pin){throw 'initial_service_unpublished_scope'}
        }
        $boot=(Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 5).LastBootUpTime.ToUniversalTime().Ticks.ToString();$sha=[Security.Cryptography.SHA256]::Create()
        try{$bootPin=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($boot))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
        if($bootPin -cne $plan.windows_initial_observer.windows_boot_sha256 -or $bootPin -cne $pre.windows_boot_sha256){throw 'initial_service_unpublished_scope'}
        $confirmed=Get-CimInstance Win32_Service -Filter ("Name='"+$service.Name+"'") -OperationTimeoutSec 5
        if($confirmed.State -cne 'Stopped' -or $confirmed.StartMode -cne 'Disabled' -or $confirmed.ProcessId -ne 0 -or $confirmed.PathName -cne $service.PathName -or $confirmed.StartName -cne $service.StartName){throw 'initial_service_unpublished_scope'}
        Assert-UnpublishedBindingAbsent $bindingPath
        $receipt=[ordered]@{schema_version=1;plan_sha256=$PlanSha256;unpublished=$r;windows_boot_sha256=$bootPin;service=$pre.service}
        Write-UnpublishedSeal (Join-Path $root 'unpublished-acceptance-seal.json') $receipt $plan.windows_enforced.service_sid
        Assert-UnpublishedSealedPredecessor $plan $root
    }finally{foreach($handle in $held){$handle.Dispose()}}
}

function Assert-PostbootEnvelope($Postboot){
    $keys=@('attempt_id','closure_mode','predecessor_args_sha256','predecessor_binding_sha256',
        'predecessor_seal_sha256','predecessor_terminal_sha256','previous_windows_boot_sha256','schema_version')
    $tagged='predecessor_kind' -cin $Postboot.PSObject.Properties.Name
    if($tagged){
        if($Postboot.predecessor_kind -cnotin @('published-unpublished-recovery-v1','published-postboot-sameboot-v1')){throw 'initial_service_postboot_scope'}
        $keys+=@('predecessor_kind')
        if($Postboot.predecessor_kind -ceq 'published-postboot-sameboot-v1'){$keys+=@('predecessor_plan_sha256','stopped_attempt_id','stopped_receipt_sha256')}
        $keys=@($keys|Sort-Object -CaseSensitive)
    }
    if((@($Postboot.PSObject.Properties.Name|Sort-Object -CaseSensitive) -join ',') -cne ($keys -join ',') -or
       ($Postboot.schema_version -isnot [int] -and $Postboot.schema_version -isnot [long]) -or $Postboot.schema_version -ne 1){throw 'initial_service_postboot_scope'}
    foreach($key in $keys){
        if($key -ceq 'schema_version'){continue}
        if($Postboot.$key -isnot [string]){throw 'initial_service_postboot_scope'}
        if($key -notin @('closure_mode','predecessor_kind') -and $Postboot.$key -cnotmatch $hashPattern){throw 'initial_service_postboot_scope'}
    }
    if($Postboot.closure_mode -cne 'windows-offline-v1' -or ($tagged -and $Postboot.predecessor_kind -ceq 'published-postboot-sameboot-v1' -and $Postboot.attempt_id -ceq $Postboot.stopped_attempt_id)){throw 'initial_service_postboot_scope'}
}
function Assert-PostbootSealedPredecessor($Plan,[string]$Root){
    if('initial_recovery_rearm' -cin $Plan.PSObject.Properties.Name -or 'initial_unpublished_recovery' -cin $Plan.PSObject.Properties.Name){throw 'initial_service_postboot_scope'}
    $r=$Plan.initial_recovery_postboot
    Assert-PostbootEnvelope $r
    if($r.attempt_id -ceq $Plan.initial_cutover_recovery.attempt_id -or $r.attempt_id -ceq $Plan.initial_cutover_handoff.attempt_id){throw 'initial_service_postboot_scope'}
    $old=Read-PinnedJson (Join-Path $Root 'postboot-predecessor-binding.json') $r.predecessor_binding_sha256
    $terminal=Read-PinnedJson (Join-Path $Root 'postboot-predecessor-state.json') $r.predecessor_terminal_sha256
    $pre=Read-PinnedJson (Join-Path $Root 'postboot-predecessor-seal.json') $r.predecessor_seal_sha256
    if((Get-FileHash -LiteralPath (Join-Path $Root 'postboot-predecessor-args.txt') -Algorithm SHA256).Hash.ToLowerInvariant() -cne $r.predecessor_args_sha256){throw 'initial_service_postboot_scope'}
    $path=Join-Path $Root 'postboot-acceptance-seal.json'
    $seal=Read-PinnedJson $path ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant())
    $resumePath=Join-Path $Root 'postboot-resume-evidence.json'
    $resumePin=(Get-FileHash -LiteralPath $resumePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $resume=Read-PinnedJson $resumePath $resumePin
    $tagged='predecessor_kind' -cin $r.PSObject.Properties.Name
    $sameboot=$tagged -and $r.predecessor_kind -ceq 'published-postboot-sameboot-v1'
    $priorKind=if($tagged){'initial-wsl-cutover-unpublished'}else{'initial-wsl-cutover-rearm'}
    $numbers=Get-InitialPredecessorNumbers ([IO.File]::ReadAllBytes((Join-Path $Root 'postboot-predecessor-state.json'))) $r.predecessor_terminal_sha256 $true
    $priorRevision=$numbers.closed-1;$closedRevision=$numbers.closed
    $newboot=$Plan.windows_initial_observer.windows_boot_sha256
    $planPin=(Get-FileHash -LiteralPath (Join-Path $Root 'plan.json') -Algorithm SHA256).Hash.ToLowerInvariant()
    $bootWrong=if($sameboot){$newboot -cne $r.previous_windows_boot_sha256}else{$newboot -ceq $r.previous_windows_boot_sha256}
    $kindWrong=if($sameboot){$terminal.kind -cnotin @('initial-wsl-cutover-postboot','initial-wsl-cutover-postboot-unpublished','initial-wsl-cutover-postboot-resume')}else{$terminal.kind -cne $priorKind}
    if($sameboot){Assert-SamebootPostbootInputs $Plan $Root $terminal $old $pre $resume}
    if($bootWrong -or $pre.windows_boot_sha256 -cne $r.previous_windows_boot_sha256 -or
       $kindWrong -or $terminal.revision -ne $priorRevision -or $terminal.phase -cnotin @('lease_lost','local_revoke_observed') -or
       $old.controller_plan_sha256 -cne $terminal.controller_plan_sha256 -or $old.controller_plan_sha256 -ceq $planPin -or
       (ConvertTo-Json $terminal.recovery -Compress) -cne (ConvertTo-Json $Plan.initial_cutover_recovery -Compress) -or
       $seal.plan_sha256 -cne $planPin -or $seal.windows_boot_sha256 -cne $newboot -or $seal.schema_version -ne 1 -or
       (ConvertTo-Json $seal.postboot -Compress) -cne (ConvertTo-Json $r -Compress) -or $seal.resume_evidence_sha256 -cne $resumePin -or
       $seal.service.state -cne 'Stopped' -or $seal.service.start_mode -cne 'Disabled' -or $seal.service.process_id -ne 0 -or
       $seal.service.service_name -cne $Plan.windows_enforced.service_name -or $seal.service.service_sid -cne $Plan.windows_enforced.service_sid -or
       $seal.service.service_image_path_sha256 -cne $old.service_image_path_sha256 -or
       $resume.plan_sha256 -cne $planPin -or $resume.windows_boot_sha256 -cne $newboot -or
       $resume.predecessor_seal_sha256 -cne $r.predecessor_seal_sha256 -or $resume.closed_revision -ne $closedRevision){throw 'initial_service_postboot_scope'}
}

function Assert-SamebootPostbootInputs($Plan,[string]$Root,$Terminal,$Binding,$Pre,$Resume){
    $r=$Plan.initial_recovery_postboot
    $oldPath=Join-Path $Root 'postboot-predecessor-plan.json'
    $stopPath=Join-Path $Root 'postboot-stopped-inventory.json'
    Assert-UnpublishedTcbPath $oldPath;Assert-UnpublishedTcbPath $stopPath
    $old=Read-PinnedJson $oldPath $r.predecessor_plan_sha256
    $stopped=Read-PinnedJson $stopPath $r.stopped_receipt_sha256
    $previous=$old.initial_recovery_postboot;Assert-PostbootEnvelope $previous
    $previousSame=('predecessor_kind' -cin $previous.PSObject.Properties.Name) -and $previous.predecessor_kind -ceq 'published-postboot-sameboot-v1'
    $stopAttempt=if($previousSame){$previous.stopped_attempt_id}else{$previous.attempt_id}
    if($Binding.controller_plan_sha256 -cne $r.predecessor_plan_sha256 -or $Terminal.controller_plan_sha256 -cne $r.predecessor_plan_sha256 -or
       (ConvertTo-Json $Terminal.postboot -Compress) -cne (ConvertTo-Json $previous -Compress) -or $r.attempt_id -ceq $previous.attempt_id -or
       $old.installation_id -cne $Plan.installation_id -or $old.source_commit -cnotmatch '\A[a-f0-9]{40}\z' -or
       (ConvertTo-Json $old.initial_cutover_handoff -Compress) -cne (ConvertTo-Json $Plan.initial_cutover_handoff -Compress) -or
       (ConvertTo-Json $old.initial_cutover_recovery -Compress) -cne (ConvertTo-Json $Plan.initial_cutover_recovery -Compress) -or
       $old.windows_initial_observer.windows_boot_sha256 -cne $Plan.windows_initial_observer.windows_boot_sha256 -or
       $old.windows_recovery_observers.hold_sha256 -cne $Plan.windows_recovery_observers.hold_sha256 -or
       $old.windows_recovery_observers.guest_boot_sha256 -cne $Plan.windows_recovery_observers.guest_boot_sha256 -or
       $r.stopped_attempt_id -cne $stopAttempt -or ($previousSame -and $r.stopped_receipt_sha256 -cne $previous.stopped_receipt_sha256) -or
       $Pre.postboot_stopped_receipt_sha256 -cne $r.stopped_receipt_sha256 -or $Resume.postboot_stopped_receipt_sha256 -cne $r.stopped_receipt_sha256 -or
       $stopped.args.postboot_attempt_id -cne $stopAttempt -or $stopped.hold_sha256 -cne $Plan.windows_recovery_observers.hold_sha256 -or
       $stopped.closed_revision -ge $Pre.closed_revision -or (-not $previousSame -and $stopped.closed_revision -ne ($Terminal.revision-1)) -or
       $Resume.previous_windows_boot_sha256 -cne $Plan.windows_initial_observer.windows_boot_sha256 -or
       $Resume.closed_pair_sha256 -cne $Pre.closed_pair_sha256 -or
       (@($Resume.observed.PSObject.Properties.Name|Sort-Object -CaseSensitive) -join ',') -cne 'A,B' -or $Resume.observed.A -cne 'Running' -or $Resume.observed.B -cne 'Running'){
        throw 'initial_service_postboot_scope'
    }
    foreach($key in @('service_name','service_sid','service_image_sha256')){if($old.windows_enforced.$key -cne $Plan.windows_enforced.$key){throw 'initial_service_postboot_scope'}}
}

function Seal-HaInitialPostbootAcceptance([string]$PlanPath,[string]$PlanSha256){
    $plan=Read-PinnedJson $PlanPath $PlanSha256
    $r=$plan.initial_recovery_postboot
    Assert-PostbootEnvelope $r
    $root=[IO.Path]::GetDirectoryName($PlanPath)
    $binding=Read-PinnedJson (Join-Path $root 'postboot-predecessor-binding.json') $r.predecessor_binding_sha256
    $terminal=Read-PinnedJson (Join-Path $root 'postboot-predecessor-state.json') $r.predecessor_terminal_sha256
    $oldArgs=Join-Path $root 'postboot-predecessor-args.txt'
    if((Get-FileHash -LiteralPath $oldArgs -Algorithm SHA256).Hash.ToLowerInvariant() -cne $r.predecessor_args_sha256){throw 'initial_service_rearm_scope'}
    $service=Get-CimInstance Win32_Service -Filter ("Name='"+$plan.windows_enforced.service_name+"'") -OperationTimeoutSec 5
    if($null -eq $service -or $service.State -cne 'Stopped' -or $service.StartMode -cne 'Disabled' -or $service.ProcessId -ne 0 -or
       $service.Name -cne $binding.service_name -or $service.StartName -ine ('NT SERVICE\'+$service.Name) -or
       $service.ServiceType -cne 'Own Process' -or $service.DesktopInteract -or
       $service.PathName -cnotmatch '\A"[^"\r\n]+"\z'){throw 'initial_service_rearm_scope'}
    Assert-Executable $service.PathName.Trim('"') $plan.windows_enforced.service_image_sha256
    $sha=[Security.Cryptography.SHA256]::Create()
    try{$image=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($service.PathName))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
    if($image -cne $binding.service_image_path_sha256){throw 'initial_service_rearm_scope'}
    $boot=(Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 5).LastBootUpTime.ToUniversalTime().Ticks.ToString()
    $sha=[Security.Cryptography.SHA256]::Create()
    try{$bootPin=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($boot))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
    if($bootPin -cne $plan.windows_initial_observer.windows_boot_sha256){throw 'initial_service_rearm_scope'}
    # Hold the configured predecessor's actual files read-only through sealing.
    # Copies alone cannot establish which stopped incarnation is being handed over.
    $held=[Collections.Generic.List[IDisposable]]::new()
    try{
        $actualArgs=Join-Path ([IO.Path]::GetDirectoryName($service.PathName.Trim('"'))) 'service.args'
        $argsRaw=[IO.File]::ReadAllBytes($actualArgs)
        $argsText=[Text.UTF8Encoding]::new($false,$true).GetString($argsRaw)
        $rows=$argsText.Split([char]10)
        $keys=@('format','service_name','service_sid','python_path','python_sha256','launcher_path','launcher_sha256',
                'plan_path','plan_sha256','approval_path','approval_sha256','mode','host_sha256')
        if($rows.Count -ne 14 -or $rows[-1] -cne '' -or $argsText.Contains([char]13) -or $argsText.Contains([char]0)){
            throw 'initial_service_rearm_scope'
        }
        $previousArgs=@{}
        for($i=0;$i -lt $keys.Count;$i++){
            $prefix=$keys[$i]+'='
            if(-not $rows[$i].StartsWith($prefix,[StringComparison]::Ordinal)) {throw 'initial_service_rearm_scope'}
            $previousArgs[$keys[$i]]=$rows[$i].Substring($prefix.Length)
        }
        if($previousArgs.format -cne 'enforced-service-v1' -or $previousArgs.mode -cne 'serve' -or
           $previousArgs.service_name -cne $service.Name -or $previousArgs.service_sid -cne $plan.windows_enforced.service_sid -or
           $previousArgs.plan_sha256 -cne $binding.controller_plan_sha256 -or
           $previousArgs.host_sha256 -cne $plan.windows_enforced.service_image_sha256){throw 'initial_service_rearm_scope'}
        $oldRoot=[IO.Path]::GetDirectoryName($previousArgs.plan_path)
        if($binding.state_path -cne (Join-Path $oldRoot 'journal\controller-state.json')){throw 'initial_service_rearm_scope'}
        $actualBinding=Join-Path $oldRoot ('initial-recovery-service-'+$plan.initial_cutover_recovery.attempt_id+'.json')
        $actual=@(@{path=$actualArgs;pin=$r.predecessor_args_sha256},
                  @{path=$previousArgs.plan_path;pin=$binding.controller_plan_sha256},
                  @{path=$binding.state_path;pin=$r.predecessor_terminal_sha256},
                  @{path=$actualBinding;pin=$r.predecessor_binding_sha256})
        if(('predecessor_kind' -cin $r.PSObject.Properties.Name) -and $r.predecessor_kind -ceq 'published-postboot-sameboot-v1'){
            if($binding.controller_plan_sha256 -cne $r.predecessor_plan_sha256){throw 'initial_service_postboot_scope'}
            foreach($path in @($actualArgs,$previousArgs.plan_path,$binding.state_path,$actualBinding,(Join-Path $oldRoot 'postboot-stopped-inventory.json'))){Assert-UnpublishedTcbPath $path}
            $actual+=@(@{path=(Join-Path $oldRoot 'postboot-stopped-inventory.json');pin=$r.stopped_receipt_sha256})
        }
        foreach($entry in $actual){
            $item=Get-Item -LiteralPath $entry.path -Force
            if($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {throw 'initial_service_rearm_scope'}
            for($parent=$item.Directory;$null -ne $parent;$parent=$parent.Parent){
                if($parent.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'initial_service_rearm_scope'}
            }
            $handle=[IO.File]::Open($item.FullName,'Open','Read','Read')
            $held.Add($handle)
            $hasher=[Security.Cryptography.SHA256]::Create()
            try{$pin=[BitConverter]::ToString($hasher.ComputeHash($handle)).Replace('-','').ToLowerInvariant()}finally{$hasher.Dispose()}
            if($pin -cne $entry.pin){throw 'initial_service_rearm_scope'}
        }
        $confirmed=Get-CimInstance Win32_Service -Filter ("Name='"+$service.Name+"'") -OperationTimeoutSec 5
        if($confirmed.State -cne 'Stopped' -or $confirmed.StartMode -cne 'Disabled' -or $confirmed.ProcessId -ne 0 -or
           $confirmed.PathName -cne $service.PathName -or $confirmed.StartName -cne $service.StartName -or
           $confirmed.ServiceType -cne 'Own Process' -or $confirmed.DesktopInteract){throw 'initial_service_rearm_scope'}
    $resumePath=Join-Path $root 'postboot-resume-evidence.json'
    $resumePin=(Get-FileHash -LiteralPath $resumePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $receipt=[ordered]@{schema_version=1;plan_sha256=$PlanSha256;postboot=$r;windows_boot_sha256=$bootPin;resume_evidence_sha256=$resumePin;
        service=[ordered]@{service_name=$service.Name;service_sid=$plan.windows_enforced.service_sid;state='Stopped';
                          start_mode='Disabled';process_id=0;service_image_path_sha256=$image}}
    $security=[Security.AccessControl.FileSecurity]::new()
    $admins=[Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $security.SetOwner($admins);$security.SetAccessRuleProtection($true,$false)
    foreach($sid in @('S-1-5-18','S-1-5-32-544')){$security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),'FullControl','Allow'))}
    $security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($plan.windows_enforced.service_sid),'Read','Allow'))
    $path=Join-Path $root 'postboot-acceptance-seal.json'
    $raw=[Text.UTF8Encoding]::new($false).GetBytes((ConvertTo-Json $receipt -Depth 8 -Compress))
    $file=[IO.FileStream]::new($path,[IO.FileMode]::CreateNew,[Security.AccessControl.FileSystemRights]'WriteData,Synchronize',
        [IO.FileShare]::None,4096,[IO.FileOptions]::WriteThrough,$security)
    try{$file.Write($raw,0,$raw.Length);$file.Flush($true)}finally{$file.Dispose()}
    Assert-PostbootSealedPredecessor $plan $root
    }finally{foreach($handle in $held){$handle.Dispose()}}
}

function Assert-RecoveryPredecessorStopped($Plan, [string]$Root) {
    if('initial_unpublished_recovery' -cin $Plan.PSObject.Properties.Name){Assert-UnpublishedSealedPredecessor $Plan $Root}
    if('initial_recovery_postboot' -cin $Plan.PSObject.Properties.Name){Assert-PostbootSealedPredecessor $Plan $Root}
    if('initial_recovery_rearm' -cin $Plan.PSObject.Properties.Name){Assert-RearmSealedPredecessor $Plan $Root}
    $recovery = $Plan.initial_cutover_recovery
    $binding = Read-PinnedJson (Join-Path $Root 'initial-predecessor-service.json') $recovery.predecessor_service_sha256
    $oldName = 'N100HA_' + $Plan.installation_id.Substring(0,24) + '_controller'
    if ($binding.service_name -cne $oldName -or $binding.attempt_id -cne $Plan.initial_cutover_handoff.attempt_id -or
        $binding.service_image_path_sha256 -cnotmatch $hashPattern) {
        throw 'initial_service_predecessor_unconfirmed'
    }
    $services = @(Get-CimInstance Win32_Service -Filter ("Name='" + $oldName + "'") -OperationTimeoutSec 5 -ErrorAction Stop)
    if ($services.Count -ne 1) { throw 'initial_service_predecessor_unconfirmed' }
    $old = $services[0]
    if ($old.Name -cne $oldName -or $old.State -cne 'Stopped' -or $old.ProcessId -ne 0 -or
        $old.StartMode -cne 'Disabled' -or $old.ServiceType -cne 'Own Process' -or
        $old.DesktopInteract -ne $false -or $old.StartName -ine ('NT SERVICE\'+$oldName) -or
        $old.PathName -isnot [string]) { throw 'initial_service_predecessor_unconfirmed' }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $image = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($old.PathName))).
            Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
    if ($image -cne $binding.service_image_path_sha256) { throw 'initial_service_predecessor_unconfirmed' }
}

function Read-PinnedJson([string]$Path, [string]$Sha) {
    if ($Path -cnotmatch '\A[A-Za-z]:\\[A-Za-z0-9_.\\-]+\z' -or $Path -match '\\\.\.?(\\|$)') {
        throw 'initial_service_input_path'
    }
    $file = Get-Item -LiteralPath $Path -Force
    if ($file -isnot [IO.FileInfo] -or $file.Length -lt 1 -or $file.Length -gt 1048576 -or
        ($file.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'initial_service_input_path' }
    for ($parent = $file.Directory; $null -ne $parent; $parent = $parent.Parent) {
        if ($parent.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'initial_service_input_path' }
    }
    $stream = [IO.File]::Open($Path, 'Open', 'Read', 'Read')
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $observed = [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
        if ($observed -cne $Sha -or $stream.Length -gt 1048576) { throw 'initial_service_input_pin' }
        $stream.Position = 0
        $reader = [IO.StreamReader]::new($stream,[Text.UTF8Encoding]::new($false,$true),$false,1024,$true)
        try { return ConvertFrom-Json -InputObject $reader.ReadToEnd() }
        finally { $reader.Dispose() }
    } finally { $hasher.Dispose(); $stream.Dispose() }
}

function Assert-Executable([string]$Path, [string]$Sha) {
    if ($Path -cnotmatch '\A[A-Za-z]:\\[A-Za-z0-9_.\\-]+\z' -or $Path -match '\\\.\.?(\\|$)' -or
        $Sha -cnotmatch $hashPattern) { throw 'initial_service_executable_path' }
    $item = Get-Item -LiteralPath $Path -Force
    if ($item -isnot [IO.FileInfo] -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $Sha) {
        throw 'initial_service_executable_pin'
    }
    for ($parent = $item.Directory; $null -ne $parent; $parent = $parent.Parent) {
        if ($parent.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'initial_service_executable_path' }
    }
}

function Assert-TransitionPreflight([string]$ExpectedArgs) {
    $ArgsSha256 = $ExpectedArgs
    $checked = ConvertFrom-Json -InputObject (Test-EnforcedServicePreflight)
    if ($checked.scope -cne 'pre_StartService_inventory_and_ACL' -or $checked.host_sha256 -cne $ServiceHostSha256 -or
        $checked.args_sha256 -cne $ExpectedArgs -or $checked.execution_performed -isnot [bool] -or $checked.execution_performed) {
        throw 'initial_service_preflight_unconfirmed'
    }
}

function Assert-TransitionStopped($Plan, [string]$HostPath, [string]$ImagePath) {
    $scm = [TrustedStoppedV1.Native]::new()
    try {
        $scm.OpenForRead($ServiceName)
        [TrustedStoppedV1.Contract]::Inputs($Plan.installation_id,$ServiceName,$HostPath)
        [TrustedStoppedV1.Contract]::Verify($scm.Read($ServiceName),$ServiceName,$ImagePath)
    } finally { $scm.Dispose() }
}

function Write-TransitionArgsStage([string]$Path, [byte[]]$Bytes, $Acl) {
    $file = [IO.FileStream]::new($Path,[IO.FileMode]::CreateNew,
        [Security.AccessControl.FileSystemRights]'WriteData,Synchronize',[IO.FileShare]::None,4096,
        [IO.FileOptions]::WriteThrough,$Acl)
    try { $file.Write($Bytes,0,$Bytes.Length); $file.Flush($true) } finally { $file.Dispose() }
}

function Confirm-ClosureArgsState([string]$ServingSha, [string]$ClosureSha) {
    try { Assert-TransitionPreflight $ServingSha }
    catch {
        if ($_.Exception.Message -cne 'preflight_hash_rejected') { throw }
        Assert-TransitionPreflight $ClosureSha
    }
}

function Restore-TransitionServingArgs([string]$ArgsPath, [string]$RestorePath, [string]$ServingSha, [string]$ClosureSha) {
    Confirm-ClosureArgsState $ServingSha $ClosureSha
    $observedArgs = (Get-FileHash -LiteralPath $ArgsPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($observedArgs -ceq $ClosureSha) {
        $restoreEntry = Get-Item -LiteralPath $RestorePath -Force
        if ($restoreEntry -isnot [IO.FileInfo] -or $restoreEntry.Length -gt 65536) {
            throw 'initial_service_transition_restore_pin'
        }
        Assert-PreflightEntry $restoreEntry $authors ($authors + @($sid)) $true $false
        if ((Get-FileHash -LiteralPath $RestorePath -Algorithm SHA256).Hash.ToLowerInvariant() -cne $ServingSha) {
            throw 'initial_service_transition_restore_pin'
        }
        [IO.File]::Replace($RestorePath,$ArgsPath,[NullString]::Value,$false)
    }
    Assert-TransitionPreflight $ServingSha
}

function Start-PreparedTransitionService($Current, [string]$HostPath, $Plan, $Approval) {
    $scope = $Plan.windows_enforced
    $pins = $Plan.transition_service_start
    $mode = $pins.mode
    $oneShot = $CheckLeaseClosed -or $RejoinMode -ne ''
    if ($oneShot -or $RestoreClosureArgs -or $ObserveClosureCheck) {
        if ($pins.lease_closure_check -isnot [bool] -or -not $pins.lease_closure_check) {
            throw 'initial_service_transition_scope'
        }
    }
    if ($RejoinMode -ne '') {
        $mode = $RejoinMode
    } elseif ($CheckLeaseClosed -or $RestoreClosureArgs -or $ObserveClosureCheck) {
        $mode = 'check-lease-closed'
    }

    if ($Plan.profile -cne 'enforced-vm-v1' -or $Plan.generation -lt 2 -or
        $Plan.PSObject.Properties['initial_wsl_cutover'] -or -not $Plan.PSObject.Properties['windows_transition_observer'] -or
        $scope.service_name -cne $ServiceName -or $scope.service_image_sha256 -cne $ServiceHostSha256 -or
        $pins.preflight_sha256 -cne $PreflightSha256 -or $pins.mode -cnotin @('transition-serve','transition-rearm') -or
        $Approval.schema_version -ne 3 -or $Approval.profile -cne 'enforced-vm-v1' -or $Approval.action -cne 'ha-transition' -or
        $Approval.authority_kind -cne 'installed-runtime' -or $Approval.plan_sha256 -cne $PlanSha256 -or
        $Approval.installation_id -cne $Plan.installation_id -or $Approval.source_commit -cne $Plan.source_commit -or
        $Approval.policy_sha256 -cne $scope.policy_sha256) { throw 'initial_service_transition_scope' }
    $sid = ([Security.Principal.NTAccount]::new('NT SERVICE',$ServiceName)).
        Translate([Security.Principal.SecurityIdentifier]).Value
    if ($scope.service_sid -cne $sid) { throw 'initial_service_transition_scope' }
    Assert-Executable $scope.python_path $scope.python_sha256
    Assert-Executable $scope.launcher_path $scope.launcher_sha256
    Assert-Executable $PreflightPath $PreflightSha256
    Assert-Executable $ScmVerifierPath $pins.scm_verifier_sha256
    $hostRoot = [IO.Path]::GetDirectoryName($HostPath)
    $settings = [ordered]@{format='enforced-service-v1';service_name=$ServiceName;service_sid=$sid;
        python_path=$scope.python_path;python_sha256=$scope.python_sha256;launcher_path=$scope.launcher_path;
        launcher_sha256=$scope.launcher_sha256;plan_path=$PlanPath;plan_sha256=$PlanSha256;
        approval_path=$ApprovalPath;approval_sha256=$ApprovalSha256;mode=$mode;host_sha256=$ServiceHostSha256}
    $lines = @($settings.GetEnumerator() | ForEach-Object { $_.Key + '=' + $_.Value })
    $settings['mode'] = $pins.mode
    $servingLines = @($settings.GetEnumerator() | ForEach-Object { $_.Key + '=' + $_.Value })
    $encoder = [Text.UTF8Encoding]::new($false,$true)
    $hash = [Security.Cryptography.SHA256]::Create()
    try {
        $argsBytes = $encoder.GetBytes(($lines -join [char]10) + [char]10)
        $argsSha = [BitConverter]::ToString($hash.ComputeHash($argsBytes)).Replace('-','').ToLowerInvariant()
        $servingBytes = $encoder.GetBytes(($servingLines -join [char]10) + [char]10)
        $servingSha = [BitConverter]::ToString($hash.ComputeHash($servingBytes)).Replace('-','').ToLowerInvariant()
        if ($InspectRejoinStage) {
            $settings['mode'] = 'initialize'
            $initializeLines = @($settings.GetEnumerator() | ForEach-Object { $_.Key + '=' + $_.Value })
            $initializeBytes = $encoder.GetBytes(($initializeLines -join [char]10) + [char]10)
            $initializeSha = [BitConverter]::ToString($hash.ComputeHash($initializeBytes)).Replace('-','').ToLowerInvariant()
        }
        $preflight = [IO.File]::ReadAllBytes($PreflightPath)
        $scmSource = [IO.File]::ReadAllBytes($ScmVerifierPath)
        if ([BitConverter]::ToString($hash.ComputeHash($preflight)).Replace('-','').ToLowerInvariant() -cne $PreflightSha256 -or
            [BitConverter]::ToString($hash.ComputeHash($scmSource)).Replace('-','').ToLowerInvariant() -cne $pins.scm_verifier_sha256 -or
            [BitConverter]::ToString($hash.ComputeHash($encoder.GetBytes($Current.PathName))).Replace('-','').ToLowerInvariant() -cne
                $pins.service_image_path_sha256) { throw 'initial_service_transition_scope' }
    } finally { $hash.Dispose() }
    $preflightBlock = [ScriptBlock]::Create($encoder.GetString($preflight))
    . $preflightBlock -HostRoot $hostRoot -HostSha256 $ServiceHostSha256 -ArgsSha256 $argsSha -ServiceSid $sid -TrustedAdminSids $pins.trusted_admin_sids
    $restoreOneShot = $RestoreClosureArgs -or $RestoreRejoinArgs
    if ($PublishTransitionArgs) { Confirm-ClosureArgsState $servingSha $PreviousArgsSha256 }
    elseif ($InspectRejoinStage) {
        try { Confirm-ClosureArgsState $servingSha $argsSha }
        catch {
            if ($_.Exception.Message -cne 'preflight_hash_rejected') { throw }
            Assert-TransitionPreflight $initializeSha
        }
    } elseif ($restoreOneShot) { Confirm-ClosureArgsState $servingSha $argsSha }
    else { Assert-TransitionPreflight $servingSha }
    if ($PlanPath -cnotmatch '\AC:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}\\plan\.json\z') {
        throw 'initial_service_transition_intent_scope'
    }
    $intentRoot = Get-Item -LiteralPath ([IO.Path]::GetDirectoryName($PlanPath)) -Force
    $authors = @('S-1-5-18','S-1-5-32-544') + @($pins.trusted_admin_sids)
    Assert-PreflightEntry $intentRoot $authors ($authors + @($sid)) $true $false
    for ($parent = $intentRoot.Parent; $null -ne $parent; $parent = $parent.Parent) {
        Assert-PreflightEntry $parent $authors $authors $false $true
    }
    $intentPrefix = if ($CheckLeaseClosed -or $RestoreClosureArgs -or $ObserveClosureCheck) {'transition-closure-start-'} else {'transition-service-start-'}
    if ($RejoinMode -ne '') {
        $intentPrefix = 'transition-'+$RejoinMode+'-'
        if ($RejoinMode -cnotin @('initialize','stage-rejoin')) { $intentPrefix += $RejoinRequestId+'-' }
    }
    $intentPath = Join-Path $intentRoot.FullName ($intentPrefix + $PlanSha256 + '.json')
    $intent = [ordered]@{service_name=$ServiceName;controller_plan_sha256=$PlanSha256;approval_sha256=$ApprovalSha256;
        args_sha256=$argsSha;service_image_path_sha256=$pins.service_image_path_sha256;mode=$mode}
    $intentBytes = $encoder.GetBytes((ConvertTo-Json -InputObject $intent -Compress))
    $intentAcl = [Security.AccessControl.FileSecurity]::new()
    $intentAcl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    $intentAcl.SetAccessRuleProtection($true,$false)
    foreach ($writer in @('S-1-5-18','S-1-5-32-544')) {
        $intentAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($writer),'FullControl','Allow'))
    }
    $sections = [Security.AccessControl.AccessControlSections]'Owner,Access'
    # The host parent has already passed the pinned private ACL check.
    # Use the same lock for all transition commands, across plan generations.
    $lockPath = Join-Path ([IO.Path]::GetDirectoryName($hostRoot)) ($ServiceName + '.transition.lock')
    $commandLock = [IO.FileStream]::new($lockPath,[IO.FileMode]::CreateNew,
        [Security.AccessControl.FileSystemRights]'WriteData,Synchronize,Delete',[IO.FileShare]::None,4096,
        [IO.FileOptions]::DeleteOnClose,$intentAcl)
    try {
        if ($PublishTransitionArgs) {
            if ('TrustedStoppedV1.Native' -as [type]) { throw 'initial_service_scm_verifier_already_loaded' }
            Add-Type -TypeDefinition ($encoder.GetString($scmSource)) -ErrorAction Stop
            Assert-TransitionStopped $Plan $HostPath $Current.PathName
            Confirm-ClosureArgsState $servingSha $PreviousArgsSha256
            $target = Join-Path $hostRoot 'service.args'
            $backup = Join-Path $intentRoot.FullName ('transition-prior-args-' + $PlanSha256)
            $observed = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($observed -cne $servingSha) {
                if ($observed -cne $PreviousArgsSha256 -or (Test-Path -LiteralPath $backup)) {
                    throw 'initial_service_transition_args_reconciliation_required'
                }
                $ready = Join-Path $intentRoot.FullName ('transition-next-args-' + [Guid]::NewGuid().ToString('N'))
                Write-TransitionArgsStage $ready $servingBytes (Get-Acl -LiteralPath $target)
                [IO.File]::Replace($ready,$target,$backup,$false)
            }
            Assert-TransitionPreflight $servingSha
            if ($PreviousArgsSha256 -cne $servingSha) {
                $prior = Get-Item -LiteralPath $backup -Force
                Assert-PreflightEntry $prior $authors ($authors + @($sid)) $true $false
                if ($prior -isnot [IO.FileInfo] -or $prior.Length -gt 65536 -or
                    (Get-FileHash -LiteralPath $backup -Algorithm SHA256).Hash.ToLowerInvariant() -cne $PreviousArgsSha256) {
                    throw 'initial_service_transition_args_reconciliation_required'
                }
            }
            Assert-TransitionStopped $Plan $HostPath $Current.PathName
            [ordered]@{transition_service_prepared=$true;controller_plan_sha256=$PlanSha256;
                service_sid=$sid;serving_args_sha256=$servingSha}|ConvertTo-Json -Compress
            return
        }
        if ($VerifyPreparedTransition) {
            Assert-TransitionPreflight $servingSha
            if ('TrustedStoppedV1.Native' -as [type]) { throw 'initial_service_scm_verifier_already_loaded' }
            Add-Type -TypeDefinition ($encoder.GetString($scmSource)) -ErrorAction Stop
            Assert-TransitionStopped $Plan $HostPath $Current.PathName
            [ordered]@{transition_service_prepared=$true;controller_plan_sha256=$PlanSha256;
                service_sid=$sid;serving_args_sha256=$servingSha}|ConvertTo-Json -Compress
            return
        }
        if ($InspectRejoinStage) {
            if ('TrustedStoppedV1.Native' -as [type]) { throw 'initial_service_scm_verifier_already_loaded' }
            Add-Type -TypeDefinition ($encoder.GetString($scmSource)) -ErrorAction Stop
            Assert-TransitionStopped $Plan $HostPath $Current.PathName
            try { $entry = Get-Item -LiteralPath $intentPath -Force -ErrorAction Stop }
            catch [System.Management.Automation.ItemNotFoundException] { $entry = $null }
            if ($null -ne $entry) {
                Assert-PreflightEntry $entry $authors $authors $true $false
                if ($entry -isnot [IO.FileInfo] -or $entry.Length -ne $intentBytes.Length -or
                    (Get-Acl -LiteralPath $intentPath).GetSecurityDescriptorSddlForm($sections) -cne
                        $intentAcl.GetSecurityDescriptorSddlForm($sections) -or
                    $encoder.GetString([IO.File]::ReadAllBytes($intentPath)) -cne $encoder.GetString($intentBytes)) {
                    throw 'initial_service_transition_intent_scope'
                }
            }
            [ordered]@{stage_intent_present=($null -ne $entry);controller_plan_sha256=$PlanSha256}|ConvertTo-Json -Compress
            return
        }
        if (-not $restoreOneShot) { Assert-TransitionPreflight $servingSha }
        if ($ObserveTransition -or $StopTransition -or $restoreOneShot -or $ObserveClosureCheck) {
            $intentHash = [Security.Cryptography.SHA256]::Create()
            try { $intentSha = [BitConverter]::ToString($intentHash.ComputeHash($intentBytes)).Replace('-','').ToLowerInvariant() }
            finally { $intentHash.Dispose() }
            $null = Read-PinnedJson $intentPath $intentSha
            $entry = Get-Item -LiteralPath $intentPath -Force
            Assert-PreflightEntry $entry $authors $authors $true $false
            if ((Get-Acl -LiteralPath $intentPath).GetSecurityDescriptorSddlForm($sections) -cne
                $intentAcl.GetSecurityDescriptorSddlForm($sections)) { throw 'initial_service_transition_intent_scope' }
            if ($ObserveClosureCheck) {
                if ('TrustedStoppedV1.Native' -as [type]) { throw 'initial_service_scm_verifier_already_loaded' }
                Add-Type -TypeDefinition ($encoder.GetString($scmSource)) -ErrorAction Stop
                Assert-TransitionStopped $Plan $HostPath $Current.PathName
                $receiptPath = $intentPath + '.completed'
                $entry = Get-Item -LiteralPath $receiptPath -Force
                if ($entry -isnot [IO.FileInfo] -or $entry.Length -lt 1 -or $entry.Length -gt 1024) {
                    throw 'initial_service_closure_check_unconfirmed'
                }
                Assert-PreflightEntry $entry $authors $authors $true $false
                if ((Get-Acl -LiteralPath $receiptPath).GetSecurityDescriptorSddlForm($sections) -cne
                    $intentAcl.GetSecurityDescriptorSddlForm($sections)) { throw 'initial_service_closure_check_unconfirmed' }
                $record = ConvertFrom-Json -InputObject $encoder.GetString([IO.File]::ReadAllBytes($receiptPath))
                if ((@($record.PSObject.Properties.Name | Sort-Object -CaseSensitive) -join ',') -cne
                    'closure_check_completed,controller_plan_sha256,host_pid,service_sid,started_at_ms' -or
                    $record.closure_check_completed -isnot [bool] -or -not $record.closure_check_completed -or
                    $record.controller_plan_sha256 -cne $PlanSha256 -or $record.service_sid -cne $sid -or
                    $record.host_pid -isnot [int] -or $record.host_pid -lt 1 -or
                    $record.started_at_ms -isnot [long] -or $record.started_at_ms -lt 1) {
                    throw 'initial_service_closure_check_unconfirmed'
                }
                $record | ConvertTo-Json -Compress
                return
            }
            if ($restoreOneShot) {
                if ('TrustedStoppedV1.Native' -as [type]) { throw 'initial_service_scm_verifier_already_loaded' }
                Add-Type -TypeDefinition ($encoder.GetString($scmSource)) -ErrorAction Stop
                Assert-TransitionStopped $Plan $HostPath $Current.PathName
                Restore-TransitionServingArgs (Join-Path $hostRoot 'service.args') ($intentPath + '.serving-args') $servingSha $argsSha
                [ordered]@{serving_args_restored=$true;controller_plan_sha256=$PlanSha256}|ConvertTo-Json -Compress
                return
            }
        } else {
            if ('TrustedStoppedV1.Native' -as [type]) { throw 'initial_service_scm_verifier_already_loaded' }
            Add-Type -TypeDefinition ($encoder.GetString($scmSource)) -ErrorAction Stop
            Assert-TransitionStopped $Plan $HostPath $Current.PathName
            if (Test-Path -LiteralPath $intentPath) { throw 'initial_service_transition_reconciliation_required' }
            $file = [IO.FileStream]::new($intentPath,[IO.FileMode]::CreateNew,
                [Security.AccessControl.FileSystemRights]'WriteData,Synchronize',[IO.FileShare]::None,4096,
                [IO.FileOptions]::WriteThrough,$intentAcl)
            try { $file.Write($intentBytes,0,$intentBytes.Length); $file.Flush($true) } finally { $file.Dispose() }
            if ((Get-Acl -LiteralPath $intentPath).GetSecurityDescriptorSddlForm($sections) -cne
                $intentAcl.GetSecurityDescriptorSddlForm($sections)) { throw 'initial_service_transition_intent_scope' }
            if ($oneShot) {
                $argsPath = Join-Path $hostRoot 'service.args'
                $argsAcl = Get-Acl -LiteralPath $argsPath
                $stage = $intentPath + '.closure-args'
                $restore = $intentPath + '.serving-args'
                Write-TransitionArgsStage $stage $argsBytes $argsAcl
                Write-TransitionArgsStage $restore $servingBytes $argsAcl
                Assert-TransitionStopped $Plan $HostPath $Current.PathName
                Assert-TransitionPreflight $servingSha
                $receipt = $null
                try {
                    [IO.File]::Replace($stage,$argsPath,[NullString]::Value,$false)
                    Assert-TransitionPreflight $argsSha
                    $startedMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
                    Start-Service -Name $ServiceName -ErrorAction Stop
                    $watch = [Diagnostics.Stopwatch]::StartNew()
                    $timeoutMs = if ($RejoinMode -ceq 'stage-rejoin') { 120000 } else { 15000 }
                    $seenPid = 0
                    while ($watch.ElapsedMilliseconds -lt $timeoutMs) {
                        $sample = Get-CimInstance Win32_Service -Filter ("Name='" + $ServiceName + "'") -OperationTimeoutSec 3
                        if ($sample.Name -cne $ServiceName -or $sample.PathName -cne $Current.PathName -or
                            $sample.StartMode -cne 'Manual' -or $sample.StartName -cne $Current.StartName) {
                            throw 'initial_service_closure_check_unconfirmed'
                        }
                        if ($sample.State -ceq 'Stopped') {
                            if ($seenPid -lt 1 -or $sample.ProcessId -ne 0 -or $sample.ExitCode -ne 0 -or
                                $sample.ServiceSpecificExitCode -ne 0 -or $watch.ElapsedMilliseconds -ge $timeoutMs) {
                                throw 'initial_service_closure_check_unconfirmed'
                            }
                            $receipt = [ordered]@{host_pid=$seenPid;started_at_ms=$startedMs;service_sid=$sid;
                                controller_plan_sha256=$PlanSha256}
                            if ($CheckLeaseClosed) { $receipt['closure_check_completed'] = $true }
                            else { $receipt['rejoin_command_completed'] = $true; $receipt['mode'] = $RejoinMode }
                            break
                        }
                        if ($sample.ProcessId -gt 0) {
                            if ($seenPid -ne 0 -and $sample.ProcessId -ne $seenPid) { throw 'initial_service_closure_check_unconfirmed' }
                            $seenPid = $sample.ProcessId
                        }
                        Start-Sleep -Milliseconds 100
                    }
                    if ($null -eq $receipt) { throw 'initial_service_closure_check_unconfirmed' }
                } finally {
                    Assert-TransitionStopped $Plan $HostPath $Current.PathName
                    Restore-TransitionServingArgs $argsPath $restore $servingSha $argsSha
                    foreach ($leaf in @($stage,$restore)) {
                        if (Test-Path -LiteralPath $leaf) { Remove-Item -LiteralPath $leaf -ErrorAction Stop }
                    }
                }
                $receiptJson = ConvertTo-Json -InputObject $receipt -Compress
                Write-TransitionArgsStage ($intentPath + '.completed') ($encoder.GetBytes($receiptJson)) $intentAcl
                $receiptJson
                return
            }
            Start-Service -Name $ServiceName -ErrorAction Stop
        }
        $running = Get-CimInstance Win32_Service -Filter ("Name='" + $ServiceName + "'") -OperationTimeoutSec 5
        if ($running.State -cne 'Running' -or $running.Name -cne $ServiceName -or $running.ProcessId -lt 1 -or
            $running.PathName -cne $Current.PathName -or
            (($ObserveTransition -or $StopTransition) -and $running.ProcessId -ne $Current.ProcessId)) { throw 'initial_service_transition_start_unconfirmed' }
        $process = Get-Process -Id $running.ProcessId -ErrorAction Stop
        try {
            if ($process.HasExited -or $process.SessionId -ne 0 -or $process.MainModule.FileName -ine $HostPath) {
                throw 'initial_service_transition_start_unconfirmed'
            }
            $created = $process.StartTime.ToUniversalTime().Ticks.ToString([Globalization.CultureInfo]::InvariantCulture)
            $confirmed = Get-CimInstance Win32_Service -Filter ("Name='" + $ServiceName + "'") -OperationTimeoutSec 5
            if ($confirmed.State -cne 'Running' -or $confirmed.Name -cne $ServiceName -or
                $confirmed.ProcessId -ne $running.ProcessId -or $confirmed.PathName -cne $running.PathName -or
                $process.HasExited) { throw 'initial_service_transition_start_unconfirmed' }
            if ($StopTransition) {
                if ([string]$confirmed.ProcessId -cne $ExpectedServicePid -or $created -cne $ExpectedServiceCreatedTicks) {
                    throw 'initial_service_transition_stop_unconfirmed'
                }
                if ($confirmed.StartMode -cne 'Manual') { throw 'initial_service_transition_stop_unconfirmed' }
                Add-Type -TypeDefinition @'
    using System;
    using System.Runtime.InteropServices;
    using Microsoft.Win32.SafeHandles;
    public static class HaTransitionProcessStop {
        [DllImport("kernel32.dll", SetLastError=true)]
        static extern SafeWaitHandle OpenProcess(uint access, bool inherit, uint pid);
        [DllImport("kernel32.dll", SetLastError=true)]
        static extern bool GetProcessTimes(SafeWaitHandle process, out long born, out long exited, out long kernel, out long user);
        [DllImport("kernel32.dll", SetLastError=true)]
        static extern bool TerminateProcess(SafeWaitHandle process, uint code);
        [DllImport("kernel32.dll", SetLastError=true)]
        static extern uint WaitForSingleObject(SafeWaitHandle process, uint milliseconds);
        public static bool Stop(uint pid, long expectedTicks) {
            // SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE.
            using (var process = OpenProcess(0x101001, false, pid)) {
                long born, exited, kernel, user;
                if (process.IsInvalid || !GetProcessTimes(process, out born, out exited, out kernel, out user)
                    || DateTime.FromFileTimeUtc(born).Ticks != expectedTicks) return false;
                if (WaitForSingleObject(process, 0) != 258) return false;
                return TerminateProcess(process, 1) && WaitForSingleObject(process, 8000) == 0;
            }
        }
    }
'@ -ErrorAction Stop
                if (-not [HaTransitionProcessStop]::Stop([uint32]$ExpectedServicePid,[long]$ExpectedServiceCreatedTicks)) {
                    throw 'initial_service_transition_stop_unconfirmed'
                }
                $stopped = Get-CimInstance Win32_Service -Filter ("Name='" + $ServiceName + "'") -OperationTimeoutSec 5
                if ($stopped.Name -cne $ServiceName -or $stopped.State -cne 'Stopped' -or $stopped.ProcessId -ne 0 -or
                    $stopped.StartMode -cne 'Manual' -or $stopped.PathName -cne $Current.PathName) {
                    throw 'initial_service_transition_stop_unconfirmed'
                }
                [ordered]@{transition_service_stopped=$true;controller_plan_sha256=$PlanSha256} | ConvertTo-Json -Compress
                return
            }
            $statePath = Join-Path $intentRoot.FullName 'journal\controller-state.json'
            $command = '"{0}" -I -B "{1}" {2} --plan "{3}" --approval "{4}" --journal "{5}"' -f
                $scope.python_path,$scope.launcher_path,$pins.mode,$PlanPath,$ApprovalPath,$statePath
            $commandHasher = [Security.Cryptography.SHA256]::Create()
            try { $commandSha = [BitConverter]::ToString($commandHasher.ComputeHash($encoder.GetBytes($command))).Replace('-','').ToLowerInvariant() }
            finally { $commandHasher.Dispose() }
            $result = [ordered]@{service_pid=$running.ProcessId;controller_plan_sha256=$PlanSha256;
                service_created_ticks=$created;service_image_path_sha256=$pins.service_image_path_sha256;python_command_sha256=$commandSha}
            $result[$(if ($ObserveTransition) {'transition_service_observed'} else {'transition_service_started'})] = $true
            $result | ConvertTo-Json -Compress
        } finally { $process.Dispose() }
    } finally { $commandLock.Dispose() }
}

try {
    $rejoin = $RejoinMode -ne ''
    $transition = $rejoin -or $PublishTransitionArgs -or $VerifyPreparedTransition -or $StartTransition -or $ObserveTransition -or $StopTransition -or $CheckLeaseClosed -or $RestoreClosureArgs -or $ObserveClosureCheck
    $modes = @(@($PublishTransitionArgs,$VerifyPreparedTransition,$StartTransition,$ObserveTransition,$StopTransition,$CheckLeaseClosed,$RestoreClosureArgs,$ObserveClosureCheck) | Where-Object { $_.IsPresent }).Count
    if ($rejoin) { $modes++ }
    $recovering=[bool]$RecoveryAttemptId -or [bool]$RecoveryReservationSha256
    if (-not $Go -or $modes -gt 1 -or
        ($recovering -and ($transition -or $RecoveryAttemptId -cnotmatch $hashPattern -or
                           $RecoveryReservationSha256 -cnotmatch $hashPattern)) -or
        ($PublishTransitionArgs -and $PreviousArgsSha256 -cnotmatch $hashPattern) -or
        (-not $PublishTransitionArgs -and $PreviousArgsSha256 -cne '') -or
        ($rejoin -and ($RejoinMode -cnotin @('initialize','stage-rejoin','observe-rejoin-stage','check-rejoin') -or
            $RejoinRequestId -cnotmatch '\A[a-f0-9]{32}\z')) -or
        ($InspectRejoinStage -and ($RejoinMode -cne 'stage-rejoin' -or $RestoreRejoinArgs)) -or
        (-not $rejoin -and ($RestoreRejoinArgs -or $RejoinRequestId -cne '')) -or
        ($StopTransition -and ($ExpectedServicePid -cnotmatch '\A[1-9][0-9]{0,9}\z' -or
            $ExpectedServiceCreatedTicks -cnotmatch '\A[1-9][0-9]{1,19}\z')) -or
        (-not $StopTransition -and ($ExpectedServicePid -cne '' -or $ExpectedServiceCreatedTicks -cne '')) -or $PSVersionTable.PSEdition -cne 'Desktop' -or
        $ServiceName -cnotmatch '\AN100HA_[a-f0-9]{24}_controller\z' -or
        $ServiceHostSha256 -cnotmatch $hashPattern -or $PlanSha256 -cnotmatch $hashPattern -or
        $ApprovalSha256 -cnotmatch $hashPattern -or
        ($transition -and ($ExpectedActivationIntentSha256 -cne '' -or $BindingPath -cne '')) -or
        (-not $transition -and $ExpectedActivationIntentSha256 -cnotmatch $hashPattern)) {
        throw 'initial_service_scope'
    }
    $current = Get-CimInstance Win32_Service -Filter ("Name='" + $ServiceName + "'")
    if ($null -eq $current -or $current.Name -cne $ServiceName -or
        (($ObserveTransition -or $StopTransition) -and ($current.State -cne 'Running' -or $current.ProcessId -lt 1)) -or
        (-not ($ObserveTransition -or $StopTransition) -and ($current.State -cne 'Stopped' -or $current.ProcessId -ne 0)) -or
        $current.StartMode -cne 'Manual' -or $current.ServiceType -cne 'Own Process' -or
        $current.DesktopInteract -or
        $current.StartName -cne ('NT SERVICE\' + $ServiceName) -or $current.PathName -cnotmatch '\A"[^"\r\n]+"\z') {
        throw 'initial_service_baseline_changed'
    }
    $hostPath = $current.PathName.Substring(1,$current.PathName.Length - 2)
    Assert-Executable $hostPath $ServiceHostSha256
    $plan = Read-PinnedJson $PlanPath $PlanSha256
    $approval = Read-PinnedJson $ApprovalPath $ApprovalSha256
    if ($transition) {
        Start-PreparedTransitionService $current $hostPath $plan $approval
        return
    }
    $scope = $plan.windows_enforced
    $startScope = $plan.initial_service_start
    $postboot='initial_recovery_postboot' -cin $plan.PSObject.Properties.Name
    $rearming='initial_recovery_rearm' -cin $plan.PSObject.Properties.Name
    $unpublished='initial_unpublished_recovery' -cin $plan.PSObject.Properties.Name
    $postbootUnpublished=$postboot -and ('predecessor_kind' -cin $plan.initial_recovery_postboot.PSObject.Properties.Name) -and $plan.initial_recovery_postboot.predecessor_kind -ceq 'published-unpublished-recovery-v1'
    $postbootResume=$postboot -and ('predecessor_kind' -cin $plan.initial_recovery_postboot.PSObject.Properties.Name) -and $plan.initial_recovery_postboot.predecessor_kind -ceq 'published-postboot-sameboot-v1'
    if($unpublished -and ($postboot -or $rearming)){throw 'initial_service_unpublished_scope'}
    $expectedRevision=if($rearming){6}else{4}
    if($unpublished -or $postboot){
        $r=if($unpublished){$plan.initial_unpublished_recovery}else{$plan.initial_recovery_postboot}
        $leaf=if($unpublished){'unpublished-predecessor-state.json'}else{'postboot-predecessor-state.json'}
        $predecessorPath=Join-Path ([IO.Path]::GetDirectoryName($PlanPath)) $leaf;Assert-UnpublishedTcbPath $predecessorPath
        $numbers=Get-InitialPredecessorNumbers ([IO.File]::ReadAllBytes($predecessorPath)) $r.predecessor_terminal_sha256 $postboot
        $expectedRevision=$numbers.opened
    }
    $action = if($unpublished){'ha-recover-unpublished-initial-writer'}elseif($postboot){'ha-resume-initial-writer'}elseif($rearming){'ha-rearm-initial-writer'}elseif($recovering){'ha-recover-initial-writer'}else{'ha-initial-wsl-cutover'}
    if ($recovering) {
        Assert-RecoveryStartScope $plan $approval $RecoveryAttemptId $RecoveryReservationSha256 $ServiceName $hostPath (
            [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData))
    } elseif ('initial_cutover_recovery' -cin $plan.PSObject.Properties.Name) {
        throw 'initial_service_recovery_scope'
    }
    if ($plan.initial_cutover_handoff.activation_intent_sha256 -cne $ExpectedActivationIntentSha256) {
        throw 'initial_service_intent_unbound'
    }
    $imageHasher = [Security.Cryptography.SHA256]::Create()
    try {
        $configuredImageHash = [BitConverter]::ToString($imageHasher.ComputeHash(
            [Text.Encoding]::UTF8.GetBytes($current.PathName))).Replace('-','').ToLowerInvariant()
    } finally { $imageHasher.Dispose() }
    $sid = [Security.Principal.NTAccount]::new('NT SERVICE',$ServiceName).Translate([Security.Principal.SecurityIdentifier])
    if ($scope.service_name -cne $ServiceName -or $scope.service_sid -cne $sid.Value -or
        $scope.service_image_sha256 -cne $ServiceHostSha256 -or $startScope.preflight_sha256 -cne $PreflightSha256 -or
        $startScope.service_image_path_sha256 -cne $configuredImageHash -or $startScope.binding_path -cne $BindingPath -or
        $plan.initial_wsl_cutover.primary_node -cne 'A' -or $plan.initial_wsl_cutover.partner_node -cne 'B' -or
        $approval.action -cne $action -or $approval.plan_sha256 -cne $PlanSha256 -or
        $approval.installation_id -cne $plan.installation_id -or $approval.source_commit -cne $plan.source_commit) {
        throw 'initial_service_plan_scope'
    }
    Assert-Executable $scope.python_path $scope.python_sha256
    Assert-Executable $scope.launcher_path $scope.launcher_sha256
    Assert-Executable $PreflightPath $PreflightSha256
    Assert-Executable $ScmVerifierPath $startScope.scm_verifier_sha256
    $directory = [IO.Path]::GetDirectoryName($hostPath)
    $argsPath = Join-Path $directory 'service.args'
    $statePath = Join-Path ([IO.Path]::GetDirectoryName($PlanPath)) 'journal\controller-state.json'
    $attempt = $plan.initial_cutover_handoff.attempt_id
    $bindingName=if($recovering){'initial-recovery-service-'+$RecoveryAttemptId+'.json'}
        else{'initial-service-'+$attempt+'.json'}
    if ($attempt -cnotmatch $hashPattern -or
        $BindingPath -cnotmatch ('\AC:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}\\'+[regex]::Escape($bindingName)+'\z') -or
        (Test-Path -LiteralPath $BindingPath)) { throw 'initial_service_binding_scope' }
    if (Test-Path -LiteralPath $argsPath) { throw 'initial_service_args_exist' }
    if ((Test-Path -LiteralPath $statePath) -or -not (Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($statePath)) -PathType Container)) {
        throw 'initial_service_journal_not_fresh'
    }
    $settings = [ordered]@{format='enforced-service-v1';service_name=$ServiceName;service_sid=$sid.Value;
        python_path=$scope.python_path;python_sha256=$scope.python_sha256;launcher_path=$scope.launcher_path;
        launcher_sha256=$scope.launcher_sha256;plan_path=$PlanPath;plan_sha256=$PlanSha256;
        approval_path=$ApprovalPath;approval_sha256=$ApprovalSha256;mode='serve';host_sha256=$ServiceHostSha256}
    $lines = @($settings.GetEnumerator() | ForEach-Object { $_.Key + '=' + $_.Value })
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($lines -join "`n") + "`n")
    $security = [Security.AccessControl.FileSecurity]::new()
    $admins = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $security.SetOwner($admins)
    $security.SetAccessRuleProtection($true,$false)
    foreach ($writer in @($admins,$system)) {
        $security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($writer,'FullControl','Allow'))
    }
    $security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'Read','Allow'))
    if ($recovering) {
        Assert-RecoveryPredecessorStopped $plan ([IO.Path]::GetDirectoryName($PlanPath))
    }
    $file = [IO.FileStream]::new($argsPath,[IO.FileMode]::CreateNew,
        [Security.AccessControl.FileSystemRights]'WriteData,Synchronize',[IO.FileShare]::None,4096,
        [IO.FileOptions]::WriteThrough,$security)
    try { $file.Write($bytes,0,$bytes.Length); $file.Flush($true) } finally { $file.Dispose() }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $argsHash = [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-','').ToLowerInvariant()
        $preflight = [IO.File]::ReadAllBytes($PreflightPath)
        $scmSource = [IO.File]::ReadAllBytes($ScmVerifierPath)
        if ([BitConverter]::ToString($sha.ComputeHash($preflight)).Replace('-','').ToLowerInvariant() -cne $PreflightSha256) {
            throw 'initial_service_preflight_pin'
        }
        if ([BitConverter]::ToString($sha.ComputeHash($scmSource)).Replace('-','').ToLowerInvariant() -cne $startScope.scm_verifier_sha256) {
            throw 'initial_service_scm_verifier_pin'
        }
    } finally { $sha.Dispose() }
    $preflightBlock = [ScriptBlock]::Create([Text.UTF8Encoding]::new($false,$true).GetString($preflight))
    . $preflightBlock -HostRoot $directory -HostSha256 $ServiceHostSha256 `
        -ArgsSha256 $argsHash -ServiceSid $sid.Value -TrustedAdminSids $startScope.trusted_admin_sids
    $checked = ConvertFrom-Json -InputObject (Test-EnforcedServicePreflight)
    if ($checked.scope -cne 'pre_StartService_inventory_and_ACL' -or $checked.host_sha256 -cne $ServiceHostSha256 -or
        $checked.args_sha256 -cne $argsHash -or $checked.execution_performed -isnot [bool] -or $checked.execution_performed) {
        throw 'initial_service_preflight_unconfirmed'
    }
    $writers = @('S-1-5-18','S-1-5-32-544') + @($startScope.trusted_admin_sids)
    $bindingParent = Get-Item -LiteralPath ([IO.Path]::GetDirectoryName($BindingPath)) -Force
    if ($bindingParent -isnot [IO.DirectoryInfo]) { throw 'initial_service_binding_scope' }
    Assert-PreflightEntry $bindingParent $writers ($writers + @($sid.Value)) $true $false
    for ($parent = $bindingParent.Parent; $null -ne $parent; $parent = $parent.Parent) {
        Assert-PreflightEntry $parent $writers $writers $false $true
    }
    if ('TrustedStoppedV1.Native' -as [type]) { throw 'initial_service_scm_verifier_already_loaded' }
    Add-Type -TypeDefinition ([Text.UTF8Encoding]::new($false,$true).GetString($scmSource)) -ErrorAction Stop
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class HaInitialLeaseFileTime {
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool GetFileTime(SafeFileHandle file, IntPtr creation, IntPtr access, out long written);
    public static DateTime Written(SafeFileHandle file) {
        long written;
        if (!GetFileTime(file, IntPtr.Zero, IntPtr.Zero, out written))
            throw new InvalidOperationException("initial_service_lease_file_time");
        return DateTime.FromFileTimeUtc(written);
    }
}
'@ -ErrorAction Stop
    $scm = [TrustedStoppedV1.Native]::new()
    try {
        $scm.OpenForRead($ServiceName)
        $identityScope=if($recovering){$RecoveryAttemptId}else{$plan.installation_id}
        [TrustedStoppedV1.Contract]::Inputs($identityScope,$ServiceName,$hostPath)
        [TrustedStoppedV1.Contract]::Verify($scm.Read($ServiceName),$ServiceName,$current.PathName)
    } finally { $scm.Dispose() }
    if ($recovering) {
        Assert-RecoveryPredecessorStopped $plan ([IO.Path]::GetDirectoryName($PlanPath))
    }
    # Once invoked, a missing response is an uncertain activation. Preserve all records.
    Start-Service -Name $ServiceName -ErrorAction Stop
    $wait = [Diagnostics.Stopwatch]::StartNew()
    while ($wait.ElapsedMilliseconds -lt 25000) {
        $native = Get-CimInstance Win32_Service -Filter ("Name='" + $ServiceName + "'") -OperationTimeoutSec 5
        if ($native.State -ceq 'Stopped') { throw 'initial_service_lease_unconfirmed' }
        if ($native.State -ceq 'Running' -and $native.ProcessId -gt 0 -and [IO.File]::Exists($statePath)) {
            $stream = [IO.File]::Open($statePath,[IO.FileMode]::Open,[IO.FileAccess]::Read,
                ([IO.FileShare]::Read -bor [IO.FileShare]::Delete))
            try {
                $written = [HaInitialLeaseFileTime]::Written($stream.SafeFileHandle)
                if ($stream.Length -lt 1 -or $stream.Length -gt 8192) { throw 'initial_service_lease_unconfirmed' }
                $reader = [IO.StreamReader]::new($stream,[Text.UTF8Encoding]::new($false,$true),$false,1024,$true)
                try { $lease = ConvertFrom-Json -InputObject $reader.ReadToEnd() }
                finally { $reader.Dispose() }
            } finally { $stream.Dispose() }
            if ($lease.phase -ceq 'serving') {
                $age = ([DateTime]::UtcNow - $written).TotalMilliseconds
                $kind=if($unpublished){'initial-wsl-cutover-unpublished'}elseif($postbootResume){'initial-wsl-cutover-postboot-resume'}elseif($postbootUnpublished){'initial-wsl-cutover-postboot-unpublished'}elseif($postboot){'initial-wsl-cutover-postboot'}elseif($rearming){'initial-wsl-cutover-rearm'}elseif($recovering){'initial-wsl-cutover-recovery'}else{'initial-wsl-cutover'}
                if ($lease.kind -cne $kind -or $lease.schema_version -ne 1 -or $age -lt 0 -or $age -gt 2900 -or
                    $lease.attempt_id -cne $attempt -or $lease.generation -ne 1 -or $lease.sequence -lt 1 -or
                    $lease.controller_plan_sha256 -cne $PlanSha256 -or $lease.first_write_proof_sha256 -cnotmatch $hashPattern -or
                    $lease.activation_intent_sha256 -cne $plan.initial_cutover_handoff.activation_intent_sha256 -or
                    $lease.restore_receipt_sha256 -cne $plan.initial_cutover_handoff.restore_receipt_sha256 -or
                    $lease.legacy_plan_sha256 -cne $plan.initial_cutover_handoff.legacy_plan_sha256 -or
                    $lease.proof_elapsed_ms -lt 0 -or $lease.proof_elapsed_ms -ge 4000 -or ($age + $lease.proof_elapsed_ms) -ge 4800 -or
                    $native.PathName -cne $current.PathName) { throw 'initial_service_lease_unconfirmed' }
                if($unpublished){
                    Assert-UnpublishedEnvelope $lease.unpublished
                    if((ConvertTo-Json $lease.unpublished -Compress) -cne (ConvertTo-Json $plan.initial_unpublished_recovery -Compress)){throw 'initial_service_unpublished_scope'}
                }
                if($postboot){
                    Assert-PostbootEnvelope $lease.postboot
                    if((ConvertTo-Json $lease.postboot -Compress) -cne (ConvertTo-Json $plan.initial_recovery_postboot -Compress)){throw 'initial_service_postboot_scope'}
                }
                if($rearming){
                    Assert-RearmEnvelope $lease.rearm
                    if($lease.kind -cne 'initial-wsl-cutover-rearm' -or
                       (ConvertTo-Json $lease.rearm -Compress) -cne (ConvertTo-Json $plan.initial_recovery_rearm -Compress)){
                        throw 'initial_service_rearm_scope'
                    }
                }
                if($recovering){
                    foreach($field in @('schema_version','generation','sequence','revision')){
                        if($lease.$field -isnot [int] -and $lease.$field -isnot [long]){
                            throw 'initial_service_lease_unconfirmed'
                        }
                    }
                    $recoveryFields=@($lease.recovery.PSObject.Properties.Name|Sort-Object -CaseSensitive)
                    if($lease.revision -ne $expectedRevision -or $lease.lease_proof_sha256 -isnot [string] -or
                        $lease.lease_proof_sha256 -cnotmatch $hashPattern -or
                        ($lease.proof_elapsed_ms -isnot [int] -and $lease.proof_elapsed_ms -isnot [long] -and
                         $lease.proof_elapsed_ms -isnot [double] -and $lease.proof_elapsed_ms -isnot [decimal]) -or [double]::IsNaN([double]$lease.proof_elapsed_ms) -or
                        ($recoveryFields -join ',') -cne 'attempt_id,predecessor_service_sha256,predecessor_terminal_sha256' -or
                        $lease.recovery.attempt_id -cne $RecoveryAttemptId -or
                        $lease.recovery.predecessor_service_sha256 -cne $plan.initial_cutover_recovery.predecessor_service_sha256 -or
                        $lease.recovery.predecessor_terminal_sha256 -cne $plan.initial_cutover_recovery.predecessor_terminal_sha256){
                        throw 'initial_service_lease_unconfirmed'
                    }
                }
                $process = Get-Process -Id $native.ProcessId -ErrorAction Stop
                try {
                    if ($process.HasExited -or $process.SessionId -ne 0 -or $process.MainModule.FileName -ine $hostPath) {
                        throw 'initial_service_lease_unconfirmed'
                    }
                    $sha = [Security.Cryptography.SHA256]::Create()
                    try {
                        $imageBytes = [Text.Encoding]::UTF8.GetBytes($native.PathName)
                        $imageHash = [BitConverter]::ToString($sha.ComputeHash($imageBytes)).Replace('-','').ToLowerInvariant()
                        $binding = [ordered]@{attempt_id=$attempt;controller_plan_sha256=$PlanSha256;
                            first_write_proof_sha256=$lease.first_write_proof_sha256;legacy_plan_sha256=$lease.legacy_plan_sha256;
                            service_created_ticks=$process.StartTime.ToUniversalTime().Ticks.ToString();service_image_path_sha256=$imageHash;
                            service_name=$ServiceName;service_pid=$native.ProcessId;state_path=$statePath}
                        $bindingBytes = [Text.UTF8Encoding]::new($false).GetBytes((ConvertTo-Json -InputObject $binding -Compress))
                        $instance = [BitConverter]::ToString($sha.ComputeHash($bindingBytes)).Replace('-','').ToLowerInvariant()
                    } finally { $sha.Dispose() }
                    $confirmed = Get-CimInstance Win32_Service -Filter ("Name='" + $ServiceName + "'") -OperationTimeoutSec 5
                    $confirmedAge = ([DateTime]::UtcNow - $written).TotalMilliseconds
                    if ($confirmed.State -cne 'Running' -or $confirmed.ProcessId -ne $native.ProcessId -or
                        $confirmed.PathName -cne $current.PathName -or $process.HasExited -or
                        $confirmedAge -lt 0 -or $confirmedAge -gt 2900 -or
                        ($confirmedAge + $lease.proof_elapsed_ms) -ge 4800) { throw 'initial_service_lease_unconfirmed' }
                    $output = [IO.FileStream]::new($BindingPath,[IO.FileMode]::CreateNew,
                        [Security.AccessControl.FileSystemRights]'WriteData,Synchronize',[IO.FileShare]::None,4096,
                        [IO.FileOptions]::WriteThrough,$security)
                    try { $output.Write($bindingBytes,0,$bindingBytes.Length); $output.Flush($true) }
                    finally { $output.Dispose() }
                    [ordered]@{first_write_proof_sha256=$lease.first_write_proof_sha256;
                        service_instance_sha256=$instance}|ConvertTo-Json -Compress
                    return
                } finally { $process.Dispose() }
            }
        }
        Start-Sleep -Milliseconds 200
    }
    throw 'initial_service_lease_unconfirmed'
} catch {
    $reason = $_.Exception.Message
    if ($reason -cnotmatch '\Ainitial_service_[a-z0-9_]{1,80}\z') { $reason = 'initial_service_native_error' }
    [ordered]@{blocked=$reason} | ConvertTo-Json -Compress
}
