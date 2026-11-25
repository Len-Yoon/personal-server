# Scoped Save/Resume/reboot of the exact already-closed writer's enrolled pair.
# Reboot requests share a protected, write-once receipt per Windows boot.
[CmdletBinding()]
param(
    [ValidateSet('CheckUnissuedReboot','Observe','ObserveOldBootRecovery','SavePair','ResumePair','Reboot','CancelUnissuedReboot')][string]$Mode='Observe',
    [string]$VmAIdSha256,[string]$VmBIdSha256,[string]$WindowsBootSha256,
    [string]$ResumeBootSha256,[string]$RebootNonce,[string]$PlanPath,[string]$PlanSha256,
    [string]$BindingPath,[string]$BindingSha256,[string]$TerminalPath,[string]$TerminalSha256,
    [scriptblock]$ClosureProbe,[string]$ClosureChallenge,[long]$ClosedRevision,
    [scriptblock]$BootTaskProbe,
    [switch]$Go
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$hashPattern='\A[a-f0-9]{64}\z'
function Get-SavedPairHash([byte[]]$Raw){
    $sha=[Security.Cryptography.SHA256]::Create()
    try{return [BitConverter]::ToString($sha.ComputeHash($Raw)).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
}
function Get-SavedPairBoot {
    $os=Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 5 -ErrorAction Stop
    return Get-SavedPairHash ([Text.Encoding]::UTF8.GetBytes($os.LastBootUpTime.ToUniversalTime().Ticks.ToString()))
}
function Initialize-SavedPairJson {
    if('N100SavedPairJsonV1.Json' -as [type]){return}
    Add-Type -TypeDefinition @'
using System;using System.Text;using System.Collections.Generic;
namespace N100SavedPairJsonV1 {
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

'@ -ErrorAction Stop
}
function Get-SavedPairPredecessorNumbers([byte[]]$Raw,[string]$ExpectedSha,[bool]$Published){
    $bad='saved_pair_terminal'
    if($null -eq $Raw -or $Raw.Length -lt 1 -or $Raw.Length -gt 65536 -or $ExpectedSha -cnotmatch '\A[a-f0-9]{64}\z'){throw $bad}
    $h=[Security.Cryptography.SHA256]::Create()
    try{$pin=[BitConverter]::ToString($h.ComputeHash($Raw)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}
    if($pin -cne $ExpectedSha){throw $bad}
    # Reuse the existing strict parser: duplicate/escaped duplicate keys and non-integer JSON numbers fail closed.
    $text=[Text.UTF8Encoding]::new($false,$true).GetString($Raw)
    $null=[N100SavedPairJsonV1.Json]::Object($text,65536);$v=ConvertFrom-Json $text
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
        $savedReboot=$tag -and $scope.predecessor_kind -ceq 'published-postboot-saved-reboot-v1'
        $same=$tag -and $scope.predecessor_kind -cin @('published-postboot-sameboot-v1','published-postboot-saved-reboot-v1')
        if($tag){
            if($scope.predecessor_kind -cnotin @('published-unpublished-recovery-v1','published-postboot-sameboot-v1','published-postboot-saved-reboot-v1')){throw $bad}
            $scopeKeys+=@('predecessor_kind')
        }
        if($same){$scopeKeys+=@('predecessor_plan_sha256','stopped_attempt_id','stopped_receipt_sha256')}
        if($savedReboot -or ($same -and 'carryover_receipt_sha256' -cin $scope.PSObject.Properties.Name)){$scopeKeys+=@('carryover_receipt_sha256')}
        $transitionKeys=@('previous_service_image_sha256','successor_service_image_sha256','successor_service_image_path_sha256')
        if(@($transitionKeys|Where-Object {$_ -cin $scope.PSObject.Properties.Name}).Count){
            if(-not $same -or 'carryover_receipt_sha256' -cnotin $scope.PSObject.Properties.Name -or
               $scope.previous_service_image_sha256 -ceq $scope.successor_service_image_sha256){throw $bad}
            $scopeKeys+=$transitionKeys
        }
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

function Assert-SavedPairPath([string]$Path,[string]$ReaderSid='',[switch]$Journal){
    if($Path -cnotmatch '\A[A-Za-z]:\\[A-Za-z0-9_.\\-]+\z' -or $Path -match '\\\.\.?(\\|$)'){throw 'saved_pair_path'}
    $leaf=Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if($leaf -isnot [IO.FileInfo]){throw 'saved_pair_path'}
    for($entry=$leaf;$null -ne $entry;$entry=if($entry -is [IO.FileInfo]){$entry.Directory}else{$entry.Parent}){
        if($entry.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'saved_pair_path'}
        $journalEntry=$Journal -and ($entry.FullName -ceq $leaf.FullName -or $entry.FullName -ceq $leaf.Directory.FullName)
        $writers=@('S-1-5-18','S-1-5-32-544');if($journalEntry){$writers+=@($ReaderSid)}
        $sd=[Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $entry.FullName -ErrorAction Stop).Sddl)
        $osRoot=$entry.FullName -ceq 'C:\' -and $sd.Owner.Value -ceq 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
        if(($sd.Owner.Value -cnotin $writers -and -not $osRoot) -or $null -eq $sd.DiscretionaryAcl -or $sd.DiscretionaryAcl.Count -lt 1){throw 'saved_pair_custody'}
        foreach($ace in $sd.DiscretionaryAcl){
            if($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback){throw 'saved_pair_custody'}
            if($ace.AceQualifier -ne [Security.AccessControl.AceQualifier]::AccessAllowed -or ([int]$ace.AceFlags -band 8)){continue}
            $mask=if($entry -is [IO.DirectoryInfo]){269287488L}else{1343029590L}
            if(($ace.AccessMask -band $mask) -ne 0 -and $ace.SecurityIdentifier.Value -cnotin $writers){throw 'saved_pair_custody'}
            if($entry.FullName -ceq $leaf.FullName -and $ReaderSid -and -not $Journal -and $ace.SecurityIdentifier.Value -cnotin ($writers+@($ReaderSid))){throw 'saved_pair_reader'}
        }
    }
}
function Open-SavedPairInput([string]$Path,[string]$Pin,[long]$Maximum,[string]$ReaderSid='',[switch]$Journal){
    Assert-SavedPairPath $Path $ReaderSid -Journal:$Journal
    $stream=[IO.File]::Open($Path,'Open','Read','Read')
    try{
        if($Pin -cnotmatch $hashPattern -or $stream.Length -lt 1 -or $stream.Length -gt $Maximum){throw 'saved_pair_size'}
        $raw=[byte[]]::new([int]$stream.Length);$offset=0
        while($offset -lt $raw.Length){$read=$stream.Read($raw,$offset,$raw.Length-$offset);if($read -le 0){throw 'saved_pair_read'};$offset+=$read}
        if((Get-SavedPairHash $raw) -cne $Pin){throw 'saved_pair_pin'}
        return [pscustomobject]@{Path=$Path;Pin=$Pin;Raw=$raw;Stream=$stream;ReaderSid=$ReaderSid;Journal=[bool]$Journal}
    }catch{$stream.Dispose();throw}
}
function Convert-SavedPairJson([byte[]]$Raw,[int]$Maximum){
    $text=[Text.UTF8Encoding]::new($false,$true).GetString($Raw)
    $null=[N100SavedPairJsonV1.Json]::Object($text,$Maximum)
    return ConvertFrom-Json $text
}
function Assert-SavedPairObject($Actual,$Expected){
    if($Expected -is [pscustomobject]){
        if($Actual -isnot [pscustomobject]){throw 'saved_pair_scope'}
        $keys=@($Expected.PSObject.Properties.Name|Sort-Object -CaseSensitive)
        if((@($Actual.PSObject.Properties.Name|Sort-Object -CaseSensitive) -join ',') -cne ($keys -join ',')){throw 'saved_pair_scope'}
        foreach($key in $keys){Assert-SavedPairObject $Actual.$key $Expected.$key}
    }elseif($Expected -is [string]){if($Actual -isnot [string] -or $Actual -cne $Expected){throw 'saved_pair_scope'}}
    elseif(($Expected -is [int] -or $Expected -is [long]) -and ($Actual -is [int] -or $Actual -is [long])){if($Actual -ne $Expected){throw 'saved_pair_scope'}}
    else{if($Actual.GetType() -ne $Expected.GetType() -or $Actual -cne $Expected){throw 'saved_pair_scope'}}
}
function Assert-SavedPairService($Plan,$Binding){
    $name=$Plan.windows_enforced.service_name
    if($name -cnotmatch '\AN100HA_[a-f0-9]{24}_controller\z'){throw 'saved_pair_service'}
    $service=@(Get-CimInstance Win32_Service -Filter ("Name='"+$name+"'") -OperationTimeoutSec 5 -ErrorAction Stop)
    if($service.Count -ne 1){throw 'saved_pair_service'};$service=$service[0]
    if($service.Name -cne $name -or $service.State -cne 'Stopped' -or $service.StartMode -cne 'Disabled' -or $service.ProcessId -ne 0 -or
       $service.StartName -ine ('NT SERVICE\'+$name) -or $service.ServiceType -cne 'Own Process' -or $service.DesktopInteract -or
       $service.PathName -cnotmatch '\A"[A-Za-z]:\\[A-Za-z0-9_.\\-]+\\windows-service-host\.exe"\z' -or
       (Get-SavedPairHash ([Text.Encoding]::UTF8.GetBytes($service.PathName))) -cne $Binding.service_image_path_sha256 -or
       $Binding.service_image_path_sha256 -cne $Plan.initial_service_start.service_image_path_sha256){throw 'saved_pair_service'}
    $sid=[Security.Principal.NTAccount]::new('NT SERVICE',$name).Translate([Security.Principal.SecurityIdentifier]).Value
    if($sid -cne $Plan.windows_enforced.service_sid){throw 'saved_pair_service'}
    $hostFile=Open-SavedPairInput $service.PathName.Trim('"') $Plan.windows_enforced.service_image_sha256 67108864 $sid
    $hostFile.Stream.Dispose()
}
function Assert-SavedPairSource($Plan,$Binding,$Terminal){
    $root=[IO.Path]::GetDirectoryName($PlanPath)
    if($PlanPath -cnotmatch '\AC:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}\\plan\.json\z' -or
       $PlanSha256 -cne $Binding.controller_plan_sha256 -or $PlanSha256 -cne $Terminal.controller_plan_sha256 -or
       $Plan.windows_initial_observer.windows_boot_sha256 -cne $WindowsBootSha256 -or
       $BindingPath -cne $Plan.initial_service_start.binding_path -or
       $BindingPath -cne (Join-Path $root ('initial-recovery-service-'+$Plan.initial_cutover_recovery.attempt_id+'.json')) -or
       $TerminalPath -cne (Join-Path $root 'journal\controller-state.json') -or $TerminalPath -cne $Binding.state_path -or
       $Binding.attempt_id -cne $Plan.initial_cutover_handoff.attempt_id -or $Binding.attempt_id -cne $Terminal.attempt_id -or
       $Binding.legacy_plan_sha256 -cne $Plan.initial_cutover_handoff.legacy_plan_sha256 -or
       $Binding.service_name -cne $Plan.windows_enforced.service_name -or
       $Terminal.kind -cnotin @('initial-wsl-cutover-postboot','initial-wsl-cutover-postboot-unpublished','initial-wsl-cutover-postboot-resume')){throw 'saved_pair_scope'}
    foreach($key in @('attempt_id','legacy_plan_sha256','restore_receipt_sha256','activation_intent_sha256')){
        if($Terminal.$key -cne $Plan.initial_cutover_handoff.$key){throw 'saved_pair_scope'}
    }
    Assert-SavedPairObject $Terminal.recovery $Plan.initial_cutover_recovery
    Assert-SavedPairObject $Terminal.postboot $Plan.initial_recovery_postboot
    Assert-SavedPairService $Plan $Binding
}
function Assert-SavedPairHeld($Inputs,$Plan,$Binding,$Terminal,[string]$Boot){
    if((Get-SavedPairBoot) -cne $Boot){throw 'saved_pair_boot'}
    foreach($input in $Inputs){
        Assert-SavedPairPath $input.Path $input.ReaderSid -Journal:$input.Journal
        if((Get-FileHash -LiteralPath $input.Path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $input.Pin){throw 'saved_pair_pin'}
    }
    Assert-SavedPairSource $Plan $Binding $Terminal
}
function Get-SavedPairVmId([guid]$Id){return Get-SavedPairHash ([Text.Encoding]::UTF8.GetBytes($Id.ToString('D').ToLowerInvariant()))}
function Find-SavedPairVm($Inventory,[string]$Pin,[string]$WaitingFor=''){
    $found=@($Inventory|Where-Object {(Get-SavedPairVmId $_.Id) -ceq $Pin})
    if($found.Count -ne 1){throw 'saved_pair_vm_identity'}
    $vm=$found[0]
    $states=if($WaitingFor -ceq 'Saved'){@('Running','Saving','Saved')}elseif($WaitingFor -ceq 'Running'){@('Saved','Starting','Resuming','Running')}else{@('Running','Saved')}
    if($vm.Generation -ne 2 -or [string]$vm.AutomaticStartAction -cne 'Nothing' -or [string]$vm.AutomaticStopAction -cne 'TurnOff' -or
       [string]$vm.State -cnotin $states){throw 'saved_pair_vm_policy'}
    return $vm
}
function Get-SavedPairVms {
    $inventory=@(Get-VM -ErrorAction Stop)
    return [pscustomobject]@{A=(Find-SavedPairVm $inventory $VmAIdSha256);B=(Find-SavedPairVm $inventory $VmBIdSha256)}
}
function Wait-SavedPairVm([guid]$Id,[string]$Pin,[string]$State,[Diagnostics.Stopwatch]$Clock){
    do{
        if($Clock.Elapsed.TotalSeconds -ge 240){throw 'saved_pair_vm_timeout'}
        $vm=Find-SavedPairVm @(Get-VM -Id $Id -ErrorAction Stop) $Pin $State
        if([string]$vm.State -ceq $State){return}
        Start-Sleep -Milliseconds 250
    }while($true)
}
function New-SavedPairDecisionFile([string]$Path,[byte[]]$Raw){
    $security=[Security.AccessControl.FileSecurity]::new()
    $security.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    $security.SetAccessRuleProtection($true,$false)
    foreach($sid in @('S-1-5-18','S-1-5-32-544')){
        $identity=[Security.Principal.SecurityIdentifier]::new($sid)
        $security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($identity,'FullControl','Allow'))
    }
    $file=[IO.FileStream]::new($Path,[IO.FileMode]::CreateNew,
        [Security.AccessControl.FileSystemRights]'WriteData,Synchronize',
        [IO.FileShare]::None,4096,[IO.FileOptions]::WriteThrough,$security)
    try{$file.Write($Raw,0,$Raw.Length);$file.Flush($true)}finally{$file.Dispose()}
}
function Assert-SavedPairDecisionAbsent([string]$Root){
    $directory=Get-Item -LiteralPath $Root -Force -ErrorAction Stop
    if($directory -isnot [IO.DirectoryInfo] -or ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'saved_pair_decision_parent'}
    $path=Join-Path $Root ("saved-reboot-$WindowsBootSha256.issued.json")
    try{Get-Item -LiteralPath $path -Force -ErrorAction Stop|Out-Null}catch [System.Management.Automation.ItemNotFoundException]{return}
    throw 'saved_pair_decision_exists'
}
function Read-SavedPairPreflight {
    # A completed cancel or dispatch consumes the boot's decision slot.
    # A later attempt must fail before retiring the current writer or apps.
    if($Go -or $WindowsBootSha256 -cnotmatch $hashPattern -or
       $VmAIdSha256 -or $VmBIdSha256 -or $ResumeBootSha256 -or $RebootNonce -or
       $PlanPath -or $PlanSha256 -or $BindingPath -or $BindingSha256 -or $TerminalPath -or $TerminalSha256 -or
       $ClosureProbe -or $ClosureChallenge -or $ClosedRevision -or $BootTaskProbe){throw 'saved_pair_arguments'}
    $mutex=[Threading.Mutex]::new($false,'Global\N100HA-Start-VM-Guests');$locked=$false
    try{
        try{$locked=$mutex.WaitOne(0)}catch [Threading.AbandonedMutexException]{$locked=$true;throw 'saved_pair_lock_abandoned'}
        if(-not $locked -or (Get-SavedPairBoot) -cne $WindowsBootSha256){throw 'saved_pair_boot'}
        Assert-SavedPairDecisionAbsent 'C:\ProgramData\N100HA-InitialCutover'
        return [ordered]@{mode=$Mode;windows_boot_sha256=$WindowsBootSha256;available=$true}
    }finally{
        if($locked){$mutex.ReleaseMutex()};$mutex.Dispose()
    }
}
function Confirm-SavedPairRebootDecision([string]$Decision,[switch]$ReadOnly){
    # Plan validation and held handles verify the ancestors of this directory.
    # CreateNew rejects any earlier attempt or preexisting file, including links.
    $root=[IO.Path]::GetDirectoryName([IO.Path]::GetDirectoryName($PlanPath))
    $path=Join-Path $root ("saved-reboot-$WindowsBootSha256.issued.json")
    $record=[ordered]@{schema_version=1;decision=$Decision;windows_boot_sha256=$WindowsBootSha256;
        plan_sha256=$PlanSha256;binding_sha256=$BindingSha256;terminal_sha256=$TerminalSha256;
        nonce=$RebootNonce;vm_id_sha256=[ordered]@{A=$VmAIdSha256;B=$VmBIdSha256}}
    $raw=[Text.Encoding]::UTF8.GetBytes(($record|ConvertTo-Json -Depth 3 -Compress))
    if(-not $ReadOnly -and ($Decision -ceq 'dispatch' -or -not [IO.File]::Exists($path))){
        New-SavedPairDecisionFile $path $raw
    }
    # A lost cancellation reply can only be reconciled with its own exact
    # record. An existing dispatch always prevents same-boot recovery.
    $checked=Open-SavedPairInput $path (Get-SavedPairHash $raw) 4096
    $checked.Stream.Dispose()
}
function Invoke-SavedPairOperation {
    if($Mode -ceq 'CheckUnissuedReboot'){return Read-SavedPairPreflight}
    foreach($pin in @($VmAIdSha256,$VmBIdSha256,$WindowsBootSha256,$PlanSha256,$BindingSha256,$TerminalSha256)){
        if($pin -cnotmatch $hashPattern){throw 'saved_pair_arguments'}
    }
    $observe=$Mode -cin @('Observe','ObserveOldBootRecovery')
    $oldResume=$Mode -ceq 'ResumePair' -and $ResumeBootSha256 -ceq $WindowsBootSha256
    $requiresNonce=$oldResume -or $Mode -cin @('Reboot','CancelUnissuedReboot','ObserveOldBootRecovery')
    if($VmAIdSha256 -ceq $VmBIdSha256 -or ($observe -eq [bool]$Go) -or
       ($Mode -ceq 'ResumePair' -and $ResumeBootSha256 -cnotmatch $hashPattern) -or
       ($Mode -cne 'ResumePair' -and $ResumeBootSha256) -or
       ($requiresNonce -and $RebootNonce -cnotmatch $hashPattern) -or
       (-not $requiresNonce -and $RebootNonce)){throw 'saved_pair_arguments'}
    if($Mode -ceq 'SavePair'){
        if($null -eq $ClosureProbe -or $ClosureChallenge -cnotmatch $hashPattern -or $ClosedRevision -lt 1){throw 'saved_pair_closure'}
    }elseif($null -ne $ClosureProbe -or $ClosureChallenge -or $ClosedRevision){throw 'saved_pair_closure'}
    if(($Mode -cin @('SavePair','Reboot')) -ne ($null -ne $BootTaskProbe)){throw 'saved_pair_boot_task'}
    Initialize-SavedPairJson
    $inputs=[Collections.Generic.List[object]]::new();$mutex=[Threading.Mutex]::new($false,'Global\N100HA-Start-VM-Guests');$locked=$false
    try{
        try{$locked=$mutex.WaitOne(0)}catch [Threading.AbandonedMutexException]{$locked=$true;throw 'saved_pair_lock_abandoned'}
        if(-not $locked){throw 'saved_pair_busy'}
        $planFile=Open-SavedPairInput $PlanPath $PlanSha256 1048576;$inputs.Add($planFile)
        $plan=Convert-SavedPairJson $planFile.Raw 1048576;$sid=$plan.windows_enforced.service_sid
        $planFile.ReaderSid=$sid;Assert-SavedPairPath $PlanPath $sid
        $bindingFile=Open-SavedPairInput $BindingPath $BindingSha256 8192 $sid;$inputs.Add($bindingFile)
        $terminalFile=Open-SavedPairInput $TerminalPath $TerminalSha256 8192 $sid -Journal;$inputs.Add($terminalFile)
        $binding=Convert-SavedPairJson $bindingFile.Raw 8192
        $numbers=Get-SavedPairPredecessorNumbers $terminalFile.Raw $TerminalSha256 $true;$terminal=$numbers.terminal
        Assert-SavedPairSource $plan $binding $terminal
        $boot=Get-SavedPairBoot
        if(($Mode -cin @('SavePair','Reboot','CancelUnissuedReboot','ObserveOldBootRecovery') -and $boot -cne $WindowsBootSha256) -or
           ($Mode -ceq 'ResumePair' -and $boot -cne $ResumeBootSha256)){throw 'saved_pair_boot'}
        $pair=Get-SavedPairVms
        $clock=[Diagnostics.Stopwatch]::StartNew()
        if($null -ne $BootTaskProbe){
            $definition=@(& $BootTaskProbe)
            if($definition.Count -ne 1 -or $definition[0] -isnot [string]){throw 'saved_pair_boot_task'}
            $task=ConvertFrom-Json $definition[0]
            if($task.result -cne 'already_registered' -or $task.task -cne 'N100HA-Start-VM-Guests' -or
               $task.scope -cne 'vm_start_only'){throw 'saved_pair_boot_task'}
        }
        if($Mode -ceq 'SavePair'){
            Assert-SavedPairDecisionAbsent ([IO.Path]::GetDirectoryName([IO.Path]::GetDirectoryName($PlanPath)))
            if([string]$pair.A.State -cne 'Running' -or [string]$pair.B.State -cne 'Running'){throw 'saved_pair_save_state'}
            # Run the pinned archived-runtime A/B query under the scheduler
            # mutex and with the authoritative files held, before Save-VM.
            $proof=@(& $ClosureProbe)
            if($proof.Count -ne 1 -or $proof[0] -isnot [string]){throw 'saved_pair_closure'}
            $closed=ConvertFrom-Json $proof[0]
            if($closed.status -cne 'CLOSED_PAIR_VERIFIED' -or $closed.challenge -cne $ClosureChallenge -or
               $closed.terminal_sha256 -cne $TerminalSha256 -or $closed.closed_revision -ne $ClosedRevision -or
               $closed.proof_sha256 -cnotmatch $hashPattern){throw 'saved_pair_closure'}
            foreach($node in @('A','B')){
                Assert-SavedPairHeld $inputs $plan $binding $terminal $boot
                $pair=Get-SavedPairVms;$vm=$pair.$node
                if([string]$vm.State -cne 'Running' -or ($node -ceq 'B' -and [string]$pair.A.State -cne 'Saved')){throw 'saved_pair_save_state'}
                Save-VM -VM $vm -ErrorAction Stop | Out-Null
                Wait-SavedPairVm $vm.Id $(if($node -ceq 'A'){$VmAIdSha256}else{$VmBIdSha256}) 'Saved' $clock
            }
        }elseif($Mode -ceq 'ResumePair'){
            if([string]$pair.A.State -ceq 'Running' -and [string]$pair.B.State -ceq 'Saved'){throw 'saved_pair_resume_order'}
            if($oldResume){Confirm-SavedPairRebootDecision 'cancel'}
            foreach($node in @('B','A')){
                Assert-SavedPairHeld $inputs $plan $binding $terminal $boot
                $pair=Get-SavedPairVms;$vm=$pair.$node
                if([string]$vm.State -ceq 'Saved'){
                    Start-VM -VM $vm -ErrorAction Stop | Out-Null
                    Wait-SavedPairVm $vm.Id $(if($node -ceq 'A'){$VmAIdSha256}else{$VmBIdSha256}) 'Running' $clock
                }
            }
        }
        Assert-SavedPairHeld $inputs $plan $binding $terminal $boot
        $pair=Get-SavedPairVms
        if(($Mode -cin @('SavePair','Reboot','CancelUnissuedReboot') -and ([string]$pair.A.State -cne 'Saved' -or [string]$pair.B.State -cne 'Saved')) -or
           ($Mode -ceq 'ResumePair' -and ([string]$pair.A.State -cne 'Running' -or [string]$pair.B.State -cne 'Running'))){throw 'saved_pair_final_state'}
        if($Mode -ceq 'CancelUnissuedReboot'){
            Confirm-SavedPairRebootDecision 'cancel'
        }elseif($Mode -ceq 'ObserveOldBootRecovery'){
            Confirm-SavedPairRebootDecision 'cancel' -ReadOnly
        }elseif($Mode -ceq 'Reboot'){
            $shutdown=Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::Windows)) 'System32\shutdown.exe'
            $start=[Diagnostics.ProcessStartInfo]::new();$start.FileName=$shutdown;$start.Arguments='/r /t 0 /d p:0:0'
            $start.UseShellExecute=$false;$start.CreateNoWindow=$true;$start.RedirectStandardOutput=$true;$start.RedirectStandardError=$true
            Confirm-SavedPairRebootDecision 'dispatch'
            $process=[Diagnostics.Process]::new();$process.StartInfo=$start
            try{
                if(-not $process.Start()){throw 'saved_pair_reboot_unconfirmed'}
                $stdout=$process.StandardOutput.ReadToEndAsync();$stderr=$process.StandardError.ReadToEndAsync()
                if(-not $process.WaitForExit(5000) -or -not $stdout.Wait(500) -or -not $stderr.Wait(500) -or $process.ExitCode -ne 0){throw 'saved_pair_reboot_unconfirmed'}
            }finally{$process.Dispose()}
        }
        return [ordered]@{mode=$Mode;windows_boot_sha256=$boot;vm_id_sha256=[ordered]@{A=$VmAIdSha256;B=$VmBIdSha256};
            states=[ordered]@{A=[string]$pair.A.State;B=[string]$pair.B.State};plan_sha256=$PlanSha256;binding_sha256=$BindingSha256;terminal_sha256=$TerminalSha256}
    }finally{
        foreach($input in $inputs){$input.Stream.Dispose()}
        if($locked){$mutex.ReleaseMutex()};$mutex.Dispose()
    }
}
if($MyInvocation.InvocationName -ne '.'){
    try{Invoke-SavedPairOperation | ConvertTo-Json -Depth 4 -Compress}catch{
        # No exception details, file contents, credentials or transport output escape.
        [ordered]@{status='BLOCKED';reason='saved_pair_operation_unconfirmed'}|ConvertTo-Json -Compress
        exit 1
    }
}
