param([string]$Artifact,[string]$ArtifactSha256,[string]$SourceCommit,[string]$ProbeSourceSha256,[switch]$Go)
# Creates only fresh marked, diskless/disconnected fixtures. Never changes an
# existing VM, namespace ACL, production service, physical NIC, DB or reboot.
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
function Get-ProbeHash([byte[]]$Bytes){$h=[Security.Cryptography.SHA256]::Create();try{return [BitConverter]::ToString($h.ComputeHash($Bytes)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}}
function Get-ProbeServiceSid([string]$Name){
    $h=[Security.Cryptography.SHA1]::Create()
    try{$bytes=$h.ComputeHash([Text.Encoding]::Unicode.GetBytes($Name.ToUpperInvariant()))}finally{$h.Dispose()}
    return 'S-1-5-80-'+((0..4|ForEach-Object{[BitConverter]::ToUInt32($bytes,4*$_).ToString()})-join '-')
}
function Get-ProbeServiceNames([string]$Installation){
    if($Installation -cnotmatch '^[0-9a-f]{64}$'){throw 'fixture_service_names_invalid'}
    $names=@(('HaProbe_'+$Installation.Substring(0,16)),('HaProbe_'+$Installation.Substring(16,16)))
    if($names.Count -ne 2 -or @($names|Select-Object -Unique).Count -ne 2 -or
        @($names|Where-Object{$_ -cnotmatch '^HaProbe_[0-9a-f]{16}$'}).Count){throw 'fixture_service_names_invalid'}
    return $names
}
function Test-ProbeServiceImagePath([string]$Actual,[string]$Expected){
    if([string]::IsNullOrEmpty($Expected) -or $Expected.Contains('"')){return $false}
    return ($Actual -ceq ('"'+$Expected+'"') -or ($Expected -notmatch '\s' -and $Actual -ceq $Expected))
}
function Test-ProbeNoOperation($Result,[string]$Operation,[bool]$ExpectedHyperv){
    if($Result.scope -isnot [string] -or $Result.operation -isnot [string] -or $Result.stage -isnot [string] -or
        ($Result.schema_version -isnot [int] -and $Result.schema_version -isnot [long]) -or
        $Result.scope -cne 'owned_vm_provider_role_probe' -or $Result.schema_version -ne 5 -or $Result.operation -cne $Operation){return $false}
    foreach($field in @('attempted','complete','authorized','authorization_denied','administrator','hyperv_role')){if($Result.$field -isnot [bool]){return $false}}
    return (-not $Result.attempted -and -not $Result.complete -and -not $Result.authorized -and -not $Result.authorization_denied -and
        -not $Result.administrator -and $Result.hyperv_role -eq $ExpectedHyperv -and $Result.stage -cin @('identity','input_validation','schema_query','valid_operation'))
}
function Assert-ProbeResultShape($Result){
    $keys=@('schema_version','scope','process_id','process_start_ticks','operation','stage','attempted','complete','authorized','authorization_denied','return_code','administrator','hyperv_role','settings_serialized_value_present','settings_readback_verified','native_target_paths_bound','method_result_verified','target_descriptor_sha256','settings_payload_sha256','settings_current_path_sha256','settings_current_instance_sha256','settings_current_association_verified','exception_kind','exception_scope','exception_hresult','native_proxy_security_verified','native_exec_returned','native_exec_hresult','native_call_status_hresult','native_call_status_terminal','native_call_status','native_result_hresult','native_output_verified','native_call_result_present','native_call_proxy_hresult')
    if((($Result.PSObject.Properties.Name|Sort-Object -CaseSensitive)-join ',') -cne (($keys|Sort-Object -CaseSensitive)-join ',')){throw 'fixture_result_unverified'}
    foreach($field in @('attempted','complete','authorized','authorization_denied','administrator','hyperv_role','settings_serialized_value_present','settings_readback_verified','native_target_paths_bound','method_result_verified','settings_current_association_verified','native_proxy_security_verified','native_exec_returned','native_call_status_terminal','native_output_verified','native_call_result_present')){
        if($Result.$field -isnot [bool]){throw 'fixture_result_unverified'}
    }
    foreach($field in @('target_descriptor_sha256','settings_payload_sha256')){
        if($Result.$field -isnot [string] -or ($Result.operation -ceq 'settings' -and $Result.$field -cnotmatch '^[0-9a-f]{64}$') -or ($Result.operation -cne 'settings' -and $Result.$field -cne '')){throw 'fixture_result_unverified'}
    }
    foreach($field in @('settings_current_path_sha256','settings_current_instance_sha256')){if($Result.$field -isnot [string] -or ($Result.$field -cne '' -and $Result.$field -cnotmatch '^[0-9a-f]{64}$') -or ($Result.operation -cne 'settings' -and $Result.$field -cne '')){throw 'fixture_result_unverified'}}
    foreach($field in @('schema_version','process_id','return_code','exception_hresult','native_exec_hresult','native_call_status_hresult','native_call_status','native_result_hresult','native_call_proxy_hresult')){
        if($Result.$field -isnot [int] -and $Result.$field -isnot [long]){throw 'fixture_result_unverified'}
    }
    foreach($field in @('native_exec_hresult','native_call_status_hresult','native_call_status','native_result_hresult','native_call_proxy_hresult')){if($Result.$field -lt 0 -or $Result.$field -gt [uint32]::MaxValue){throw 'fixture_result_unverified'}}
    if($Result.schema_version -ne 5 -or $Result.process_id -le 0 -or $Result.process_id -gt [int]::MaxValue -or
        $Result.return_code -lt 0 -or $Result.return_code -gt [uint32]::MaxValue -or
        $Result.exception_hresult -lt 0 -or $Result.exception_hresult -gt [uint32]::MaxValue -or
        $Result.exception_kind -isnot [string] -or $Result.exception_kind -cnotin @('none','management','com','unauthorized_access','other') -or
        $Result.exception_scope -isnot [string] -or $Result.exception_scope -cnotin @('none','invoke','outside_invoke') -or
        $Result.scope -isnot [string] -or $Result.scope -cne 'owned_vm_provider_role_probe' -or
        $Result.operation -isnot [string] -or $Result.operation -cnotin @('settings','power_on','power_off') -or
        $Result.stage -isnot [string] -or $Result.stage -cnotin @('identity','input_validation','schema_query','valid_operation') -or
        $Result.process_start_ticks -isnot [string] -or $Result.process_start_ticks -cnotmatch '^[0-9]{15,20}$'){throw 'fixture_result_unverified'}
    if(($Result.exception_scope -ceq 'none' -and ($Result.exception_kind -cne 'none' -or $Result.exception_hresult -ne 0)) -or
        ($Result.exception_scope -cne 'none' -and $Result.exception_kind -ceq 'none') -or
        ($Result.exception_scope -ceq 'invoke' -and (-not $Result.attempted -or $Result.stage -cne 'valid_operation')) -or
        ($Result.exception_scope -ceq 'outside_invoke' -and ($Result.complete -or $Result.authorization_denied)) -or
        ($Result.authorization_denied -and (-not $Result.attempted -or -not $Result.method_result_verified -or $Result.return_code -notin @(32769,2147749891,2147942405)))){throw 'fixture_result_unverified'}
    if(($Result.exception_scope -cne 'none' -and ($Result.complete -or $Result.authorized -or $Result.authorization_denied)) -or
        ($Result.native_exec_returned -and (-not $Result.attempted -or -not $Result.native_proxy_security_verified)) -or
        ($Result.native_call_status_terminal -and (-not $Result.native_exec_returned -or $Result.native_exec_hresult -ne 0 -or $Result.native_call_status_hresult -ne 0 -or -not $Result.native_call_result_present -or $Result.native_call_proxy_hresult -ne 0))){throw 'fixture_result_unverified'}
    $nativeOutput=($Result.attempted -and $Result.native_exec_returned -and $Result.native_proxy_security_verified -and
        $Result.native_exec_hresult -eq 0 -and $Result.native_call_status_terminal -and $Result.native_call_status_hresult -eq 0 -and
        $Result.native_call_status -in @(0,1) -and $Result.native_result_hresult -eq 0 -and $Result.native_output_verified)
    $nativeDenial=($Result.attempted -and $Result.native_exec_returned -and $Result.native_proxy_security_verified -and
        ($Result.native_exec_hresult -in @(2147749891,2147942405) -or
         ($Result.native_exec_hresult -eq 0 -and $Result.native_call_status_terminal -and $Result.native_call_status_hresult -eq 0 -and $Result.native_call_status -in @(2147749891,2147942405)) -or
         ($nativeOutput -and $Result.return_code -in @(32769,2147749891,2147942405))))
    if(($Result.authorized -and -not $nativeOutput) -or ($Result.authorization_denied -and -not $nativeDenial)){throw 'fixture_result_unverified'}
}
function Test-ProbeFixedMemory($Memory){
    if($null -eq $Memory -or $null -eq $Memory.PSObject.Properties['Startup'] -or $null -eq $Memory.PSObject.Properties['DynamicMemoryEnabled']){return $false}
    return (($Memory.Startup -is [long] -or $Memory.Startup -is [int]) -and $Memory.Startup -eq 268435456 -and
        $Memory.DynamicMemoryEnabled -is [bool] -and -not $Memory.DynamicMemoryEnabled)
}
function Test-ProbeDisconnectedNic($Nic){
    # An unattached adapter may expose null/empty SwitchId rather than Guid.Empty.
    # Both independent switch fields must prove disconnection; unknown IDs fail.
    if($null -eq $Nic -or $null -eq $Nic.PSObject.Properties['SwitchName'] -or $null -eq $Nic.PSObject.Properties['SwitchId']){return $false}
    if(($null -ne $Nic.SwitchName -and $Nic.SwitchName -isnot [string]) -or
        ($null -ne $Nic.SwitchId -and $Nic.SwitchId -isnot [string] -and $Nic.SwitchId -isnot [Guid])){return $false}
    return ([string]::IsNullOrEmpty([string]$Nic.SwitchName) -and
        ([string]::IsNullOrEmpty([string]$Nic.SwitchId) -or [string]$Nic.SwitchId -ieq [Guid]::Empty.ToString()))
}
function Get-ProbeFailureReason([string]$Message){
    $codes=@('fixture_path_exists','fixture_service_operation_failed','fixture_namespace_read_failed',
        'fixture_process_unverified','fixture_process_exit_unverified','fixture_service_not_stopped','fixture_result_exists',
        'fixture_service_timeout','fixture_service_incomplete','fixture_result_unverified','positive_control_failed',
        'provider_role_bypass','fixture_vm_ownership_changed','native_fixture_intent_required','trusted_admin_required',
        'fixture_artifact_pin_failed','fixture_memory_reserve_failed','fixture_memory_bound_unverified','fixture_parent_reparse','fixture_service_collision',
        'fixture_service_sid_mismatch','positive_settings_not_applied','positive_power_not_applied',
        'negative_settings_changed_state','negative_power_changed_state','positive_stop_not_applied',
        'fixture_service_ownership_changed','fixture_service_delete_unverified','fixture_service_names_invalid')
    if($Message -cin $codes){return $Message}
    return 'unclassified_runtime_error'
}
function Set-ProbePhase($Record,[string]$Journal,[string]$Phase){
    $Record['phase']=$Phase
    [IO.File]::WriteAllText($Journal,($Record|ConvertTo-Json -Depth 6),[Text.UTF8Encoding]::new($false))
}
function New-ProbeDirectory([string]$Path,[string[]]$Readers=@(),[string]$Writer=''){
    if(Test-Path -LiteralPath $Path){throw 'fixture_path_exists'}
    $acl=[Security.AccessControl.DirectorySecurity]::new();$acl.SetAccessRuleProtection($true,$false)
    $acl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    foreach($sid in @('S-1-5-18','S-1-5-32-544')){
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
    }
    foreach($sid in $Readers){$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),'ReadAndExecute','ContainerInherit,ObjectInherit','None','Allow'))}
    if($Writer){$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($Writer),'Modify','ContainerInherit,ObjectInherit','None','Allow'))}
    # Framework overload applies protected security at creation, before files.
    return [IO.Directory]::CreateDirectory($Path,$acl)
}
function Invoke-ProbeSc([string[]]$Arguments){
    $null=& (Join-Path $env:SystemRoot 'System32\sc.exe') @Arguments
    if($LASTEXITCODE -ne 0){throw 'fixture_service_operation_failed'}
}
function Get-ProbeNamespaceHash{
    $security=Get-CimInstance -Namespace 'root/virtualization/v2' -ClassName '__SystemSecurity'
    $answer=Invoke-CimMethod -InputObject $security -MethodName GetSD
    if($answer.ReturnValue -ne 0 -or $null -eq $answer.SD){throw 'fixture_namespace_read_failed'}
    # Hash the descriptor actually returned. No claim about unreturned SACL.
    return Get-ProbeHash ([byte[]]$answer.SD)
}
function Add-ProbeProcess($Fixture,[int]$Id,[string]$Ticks){
    if($Id -le 0 -or $Ticks -cnotmatch '^[0-9]{15,20}$'){throw 'fixture_process_unverified'}
    $key=$Id.ToString()+':'+$Ticks
    if(-not $Fixture.processes.ContainsKey($key)){$Fixture.processes.Add($key,@{id=$Id;ticks=$Ticks})}
}
function Wait-ProbeProcesses($Fixture){
    $watch=[Diagnostics.Stopwatch]::StartNew()
    do{
        $active=$false
        foreach($item in $Fixture.processes.Values){
            $process=$null
            try{$process=[Diagnostics.Process]::GetProcessById($item.id)}catch [ArgumentException]{}
            if($null -ne $process){try{if($process.StartTime.ToUniversalTime().Ticks.ToString() -ceq $item.ticks){$active=$true}}finally{$process.Dispose()}}
        }
        if(-not $active){return}
        Start-Sleep -Milliseconds 100
    }while($watch.Elapsed.TotalSeconds -lt 15)
    throw 'fixture_process_exit_unverified'
}
function Get-ProbeReceiptName([string]$Role,[string]$Operation,[int]$Sequence){
    # A finite plan distinguishes intentional repeat controls from retries.
    $plan=@('true:power_on','true:power_off','true:power_on','true:settings','false:settings','false:power_off','true:power_off')
    if($Sequence -lt 1 -or $Sequence -gt $plan.Count -or ($Role+':'+$Operation) -cne $plan[$Sequence-1]){throw 'fixture_result_unverified'}
    return ('{0:D2}-{1}-{2}.json' -f $Sequence,$Role,$Operation)
}
function Write-ProbeOperationReceipt([string]$Path,$Result){
    $file=[IO.FileStream]::new($Path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try{$bytes=[Text.UTF8Encoding]::new($false).GetBytes(($Result|ConvertTo-Json -Depth 6 -Compress));$file.Write($bytes,0,$bytes.Length);$file.Flush($true)}finally{$file.Dispose()}
}
function Assert-ProbeOperationAuthority($Result,[string]$Role,[string]$Operation){
    if($Role -cnotin @('true','false') -or $Operation -cnotin @('settings','power_on','power_off')){throw 'fixture_result_unverified'}
    if($Role -ceq 'true'){
        if($Result.authorized -ne $true -or $Result.authorization_denied -ne $false -or $Result.return_code -ne 0){throw 'positive_control_failed'}
        if($Operation -ceq 'settings' -and (-not $Result.settings_serialized_value_present -or -not $Result.settings_readback_verified -or -not $Result.settings_current_association_verified -or $Result.settings_current_path_sha256 -cne $script:settingsTargetPathSha256 -or $Result.settings_current_instance_sha256 -cne $script:settingsInstanceSha256)){throw 'positive_control_failed'}
    }elseif($Result.authorization_denied -ne $true -or $Result.authorized -ne $false){throw 'provider_role_bypass'}
}
function Invoke-ProbeOperation($Fixture,[string]$Operation,[int]$Sequence){
    $saved=Join-Path $script:root (Get-ProbeReceiptName $Fixture.role $Operation $Sequence)
    if(Test-Path -LiteralPath $saved){throw 'fixture_result_exists'}

    $service=Get-Service -Name $Fixture.name -ErrorAction Stop
    if($service.Status -ne 'Stopped'){throw 'fixture_service_not_stopped'}
    $output=Join-Path $Fixture.root 'result\probe.json'
    if(Test-Path -LiteralPath $output){throw 'fixture_result_exists'}
    $descriptor=if($Operation -ceq 'settings'){$script:targetDescriptorB64}else{''}
    $descriptorSha=if($Operation -ceq 'settings'){$script:targetDescriptorSha256}else{''}
    $payloadSha=if($Operation -ceq 'settings'){$script:settingsPayloadSha256}else{''}
    $text="format=ha-provider-probe-v5`nservice_name=$($Fixture.name)`nservice_sid=$($Fixture.sid)`ninstallation_id=$script:installation`nvm_id=$script:vmId`noperation=$Operation`nexpected_hyperv_role=$($Fixture.role)`ntarget_descriptor_b64=$descriptor`ntarget_descriptor_sha256=$descriptorSha`n"
    [IO.File]::WriteAllText((Join-Path $Fixture.root 'probe.args'),$text,[Text.UTF8Encoding]::new($false))
    # A fast one-shot may already have stopped when StartService returns. Do
    # not repeat it: reconcile the pinned service state and its exact result.
    $script:pendingOperation=$true
    try{Start-Service -Name $Fixture.name}catch{}
    $watch=[Diagnostics.Stopwatch]::StartNew()
    do{
        $state=Get-CimInstance Win32_Service -Filter ("Name='"+$Fixture.name+"'")
        if($state.ProcessId -gt 0){
            $process=$null
            try{$process=[Diagnostics.Process]::GetProcessById($state.ProcessId)}catch [ArgumentException]{}
            if($null -ne $process){try{Add-ProbeProcess $Fixture $state.ProcessId ($process.StartTime.ToUniversalTime().Ticks.ToString())}finally{$process.Dispose()}}
        }
        Start-Sleep -Milliseconds 100;$service.Refresh()
    }while($service.Status -ne 'Stopped' -and $watch.Elapsed.TotalSeconds -lt 20)
    if($service.Status -ne 'Stopped'){throw 'fixture_service_timeout'}
    $state=Get-CimInstance Win32_Service -Filter ("Name='"+$Fixture.name+"'")
    if(-not (Test-Path -LiteralPath $output)){throw 'fixture_service_incomplete'}
    $result=[IO.File]::ReadAllText($output)|ConvertFrom-Json
    Add-ProbeProcess $Fixture $result.process_id $result.process_start_ticks
    Wait-ProbeProcesses $Fixture
    Assert-ProbeResultShape $result
    if($result.target_descriptor_sha256 -cne $descriptorSha -or $result.settings_payload_sha256 -cne $payloadSha){throw 'fixture_result_unverified'}
    # Preserve an exact-source failed query's diagnostic after its process has
    # exited. No query denial counts as a method authorization denial. Only a
    # verified attempted=false frame can clear the pending-operation barrier.
    if($state.ExitCode -ne 0 -or $result.complete -ne $true){
        $script:probeDiagnostics+=@($result)
        if(Test-ProbeNoOperation $result $Operation ($Fixture.role -ceq 'true')){
            $script:pendingOperation=$false
        }
        throw 'fixture_service_incomplete'
    }
    if($result.scope -cne 'owned_vm_provider_role_probe' -or $result.schema_version -ne 5 -or
        $result.operation -cne $Operation -or $result.attempted -ne $true -or $result.complete -ne $true -or
        $result.administrator -ne $false -or $result.hyperv_role -ne ($Fixture.role -ceq 'true') -or
        -not $result.native_target_paths_bound -or -not $result.method_result_verified){throw 'fixture_result_unverified'}
    Assert-ProbeOperationAuthority $result $Fixture.role $Operation
    # Preserve each planned result once; an unknown/failed operation keeps the
    # pending barrier and cannot be retried or treated as safe cleanup.
    Write-ProbeOperationReceipt $saved $result
    [IO.File]::Delete($output)
    $script:pendingOperation=$false
    return $result
}
function Test-ProbeTarget{
    $vm=Get-VM -Id $script:vmId -ErrorAction Stop
    if($vm.Generation -ne 2 -or $vm.Name -cne $script:vmName -or $vm.Notes -cne ('ha-owned-provider-probe:'+$script:installation) -or
        -not (Test-ProbeFixedMemory (Get-VMMemory -VM $vm)) -or
        @(Get-VMHardDiskDrive -VM $vm).Count -ne 0 -or @(Get-VMNetworkAdapter -VM $vm|Where-Object {-not (Test-ProbeDisconnectedNic $_)}).Count -ne 0){throw 'fixture_vm_ownership_changed'}
    return $vm
}
function New-ProbeTargetDescriptor {
    # ROOT/admin makes one typed payload from the actual owned current setting.
    # Both isolated service tokens receive exactly these bytes, not a query recipe.
    $vm=Test-ProbeTarget
    if($vm.State -ne 'Running'){throw 'fixture_vm_ownership_changed'}
    Add-Type -AssemblyName System.Management
    $scope=[Management.ManagementScope]::new('\\.\root\virtualization\v2');$scope.Connect()
    $search=[Management.ManagementObjectSearcher]::new($scope,[Management.ObjectQuery]::new("SELECT * FROM Msvm_ComputerSystem WHERE Name='$script:vmId'"))
    $found=$search.Get();$setting=$null;$service=$null;$related=$null;$services=$null;$serviceSearch=$null
    try{
        if($found.Count -ne 1){throw 'fixture_vm_ownership_changed'}
        foreach($item in $found){
            $related=$item.GetRelated('Msvm_VirtualSystemSettingData','Msvm_SettingsDefineState',$null,$null,$null,$null,$false,$null)
            foreach($candidate in $related){
                if([string]$candidate['VirtualSystemType'] -ceq 'Microsoft:Hyper-V:System:Realized' -and [string]$candidate['VirtualSystemIdentifier'] -ieq $script:vmId){
                    if($null -ne $setting){throw 'fixture_vm_ownership_changed'}
                    $setting=$candidate
                }
            }
        }
        if($null -eq $setting -or @($setting['Notes']).Count -ne 1 -or [string]$setting['Notes'][0] -cne ('ha-owned-provider-probe:'+$script:installation)){throw 'fixture_vm_ownership_changed'}
        $serviceSearch=[Management.ManagementObjectSearcher]::new($scope,[Management.ObjectQuery]::new('SELECT * FROM Msvm_VirtualSystemManagementService'))
        $services=$serviceSearch.Get()
        if($services.Count -ne 1){throw 'fixture_vm_ownership_changed'}
        foreach($candidate in $services){$service=$candidate}
        $setting['Notes']=[string[]]@('ha-owned-provider-probe:'+$script:installation+':tested')
        $script:settingsPayload=[Text.UTF8Encoding]::new($false,$true).GetBytes($setting.GetText([Management.TextFormat]::WmiDtd20))
        if($script:settingsPayload.Length -lt 1 -or $script:settingsPayload.Length -gt 1048576){throw 'fixture_result_unverified'}
        $script:settingsPayloadSha256=Get-ProbeHash $script:settingsPayload
        $svc=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($service.Path.RelativePath))
        $script:settingsTargetPathSha256=Get-ProbeHash ([Text.Encoding]::UTF8.GetBytes($setting.Path.RelativePath))
        $script:settingsInstanceSha256=Get-ProbeHash ([Text.Encoding]::UTF8.GetBytes([string]$setting['InstanceID']))
        $target=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($setting.Path.RelativePath))
        $descriptor="format=ha-provider-target-v3`ninstallation_id=$script:installation`nvm_id=$script:vmId`nsettings_vm_state=Running`nsettings_virtual_system_type=Microsoft:Hyper-V:System:Realized`nsettings_virtual_system_identifier=$($setting['VirtualSystemIdentifier'])`nsettings_service_path_b64=$svc`nsettings_target_path_b64=$target`nsettings_payload_sha256=$script:settingsPayloadSha256`nsettings_payload_bytes=$($script:settingsPayload.Length)`n"
        $descriptorBytes=[Text.Encoding]::UTF8.GetBytes($descriptor)
        $script:targetDescriptorB64=[Convert]::ToBase64String($descriptorBytes)
        $script:targetDescriptorSha256=Get-ProbeHash $descriptorBytes
    }finally{
        if($null -ne $related){foreach($item in $related){$item.Dispose()};$related.Dispose()}
        if($null -ne $services){foreach($item in $services){$item.Dispose()};$services.Dispose()}
        foreach($item in $found){$item.Dispose()};$found.Dispose();$search.Dispose()
        if($null -ne $serviceSearch){$serviceSearch.Dispose()}
    }
}
function Test-ProbeCurrentSettingsExact($Observation,[bool]$Tested){
    foreach($field in @('current_path_exact','current_instance_exact','original_marker_exact','tested_marker_exact')){if($Observation[$field] -isnot [bool]){return $false}}
    foreach($field in @('current_path_sha256','current_instance_sha256')){if($Observation[$field] -isnot [string] -or $Observation[$field] -cnotmatch '^[0-9a-f]{64}$'){return $false}}
    return ($Observation.current_path_exact -and $Observation.current_instance_exact -and
        $Observation.tested_marker_exact -eq $Tested -and $Observation.original_marker_exact -eq (-not $Tested))
}
function Get-ProbeCurrentSettingsObservation {
    # Independent ROOT read of the current association. No path/notes are emitted.
    $scope=[Management.ManagementScope]::new('\\.\root\virtualization\v2');$scope.Connect()
    $search=[Management.ManagementObjectSearcher]::new($scope,[Management.ObjectQuery]::new("SELECT * FROM Msvm_ComputerSystem WHERE Name='$script:vmId'"))
    $found=$search.Get();$related=$null;$result=$null;$count=0
    try{
        if($found.Count -ne 1){throw 'fixture_vm_ownership_changed'}
        foreach($vm in $found){
            if([uint32]$vm['EnabledState'] -ne 2){throw 'fixture_vm_ownership_changed'}
            $related=$vm.GetRelated('Msvm_VirtualSystemSettingData','Msvm_SettingsDefineState',$null,$null,$null,$null,$false,$null)
            foreach($setting in $related){
                if([string]$setting['VirtualSystemType'] -ceq 'Microsoft:Hyper-V:System:Realized' -and [string]$setting['VirtualSystemIdentifier'] -ieq $script:vmId){
                    $count++;$notes=@($setting['Notes'])
                    $pathHash=Get-ProbeHash ([Text.Encoding]::UTF8.GetBytes($setting.Path.RelativePath))
                    $instanceHash=Get-ProbeHash ([Text.Encoding]::UTF8.GetBytes([string]$setting['InstanceID']))
                    $result=[ordered]@{current_path_sha256=$pathHash;current_instance_sha256=$instanceHash;
                        current_path_exact=($pathHash -ceq $script:settingsTargetPathSha256);current_instance_exact=($instanceHash -ceq $script:settingsInstanceSha256);
                        original_marker_exact=($notes.Count -eq 1 -and [string]$notes[0] -ceq ('ha-owned-provider-probe:'+$script:installation));
                        tested_marker_exact=($notes.Count -eq 1 -and [string]$notes[0] -ceq ('ha-owned-provider-probe:'+$script:installation+':tested'))}
                }
            }
        }
        if($count -ne 1){throw 'fixture_vm_ownership_changed'}
        return $result
    }finally{
        if($null -ne $related){foreach($item in $related){$item.Dispose()};$related.Dispose()}
        foreach($item in $found){$item.Dispose()};$found.Dispose();$search.Dispose()
    }
}
function Write-ProbePayload([string]$Root,[string]$Sid){
    $file=[IO.FileStream]::new((Join-Path $Root 'settings.xml'),[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try{$file.Write($script:settingsPayload,0,$script:settingsPayload.Length);$file.Flush($true)}finally{$file.Dispose()}
    $acl=[Security.AccessControl.FileSecurity]::new();$acl.SetAccessRuleProtection($true,$false)
    $acl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    foreach($owner in @('S-1-5-18','S-1-5-32-544')){$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($owner),'FullControl','Allow'))}
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($Sid),'ReadAndExecute','Allow'))
    [IO.File]::SetAccessControl((Join-Path $Root 'settings.xml'),$acl)
    # Parent root has only Admin/System full control and this one SID RX.
    # Child result directory is the sole location writable by that service.
    if((Get-ProbeHash ([IO.File]::ReadAllBytes((Join-Path $Root 'settings.xml')))) -cne $script:settingsPayloadSha256){throw 'fixture_result_unverified'}
}
function Invoke-ProviderAcceptance {
    if(-not $Go -or [Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or -not [Environment]::Is64BitProcess -or
        $ArtifactSha256 -cnotmatch '^[0-9a-f]{64}$' -or $ProbeSourceSha256 -cnotmatch '^[0-9a-f]{64}$' -or $SourceCommit -cnotmatch '^[0-9a-f]{40}$'){throw 'native_fixture_intent_required'}
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    try{if(-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))){throw 'trusted_admin_required'}}finally{$identity.Dispose()}
    $bytes=[Convert]::FromBase64String($Artifact)
    if($bytes.Length -lt 512 -or $bytes.Length -gt 1048576 -or (Get-ProbeHash $bytes) -cne $ArtifactSha256){throw 'fixture_artifact_pin_failed'}
    $os=Get-CimInstance Win32_OperatingSystem
    if([long]$os.FreePhysicalMemory -lt 4194304){throw 'fixture_memory_reserve_failed'}
    $base=[Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
    if((Get-Item -LiteralPath $base -Force).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'fixture_parent_reparse'}
    $script:installation=([Guid]::NewGuid().ToString('N')+[Guid]::NewGuid().ToString('N'))
    $short=$script:installation.Substring(0,16);$script:root=Join-Path $base ('HaProbe_'+$short)
    $script:vmName='HaProbeVM_'+$short;$script:vmId=$null;$script:pendingOperation=$false;$script:probeDiagnostics=@();$fixtures=@();$results=@();$complete=$false;$clean=$true
    $namespaceBefore=Get-ProbeNamespaceHash
    $bootBefore=$os.LastBootUpTime.ToUniversalTime().Ticks.ToString()
    $adminsBefore=@(Get-LocalGroupMember -SID 'S-1-5-32-544'|ForEach-Object{$_.SID.Value}|Sort-Object)
    $beforeVm=@(Get-VM|ForEach-Object{$_.Id.ToString()}|Sort-Object)
    $beforeMembers=@(Get-LocalGroupMember -SID 'S-1-5-32-578'|ForEach-Object{$_.SID.Value}|Sort-Object)
    $names=@(Get-ProbeServiceNames $script:installation)
    $sids=@($names|ForEach-Object{Get-ProbeServiceSid $_})
    if(@($names|Where-Object{Get-Service -Name $_ -ErrorAction SilentlyContinue}).Count){throw 'fixture_service_collision'}
    $null=New-ProbeDirectory $script:root $sids
    $journal=Join-Path $script:root 'ownership.json'
    $record=[ordered]@{schema_version=1;installation_id=$script:installation;source_commit=$SourceCommit;probe_source_sha256=$ProbeSourceSha256;artifact_sha256=$ArtifactSha256;boot_ticks=$bootBefore;namespace_descriptor_sha256=$namespaceBefore;vm_id=$null;service_names=$names;phase='fixture_vm_creation';complete=$false}
    $failureType=$null;$failureReason=$null;$record['settings_readback']=$null;$record['target_descriptor_sha256']=$null;$record['settings_payload_sha256']=$null;$record['positive_negative_dataset_identical']=$false
    [IO.File]::WriteAllText($journal,($record|ConvertTo-Json -Depth 6),[Text.UTF8Encoding]::new($false))
    try{
        $config=Join-Path $script:root 'vm';$null=New-ProbeDirectory $config
        $vm=New-VM -Name $script:vmName -Generation 2 -MemoryStartupBytes 256MB -NoVHD -Path $config
        $script:vmId=$vm.Id.ToString();$record.vm_id=$script:vmId
        Set-ProbePhase $record $journal 'fixture_vm_policy'
        Set-VM -VM $vm -Notes ('ha-owned-provider-probe:'+$script:installation) -AutomaticStartAction Nothing -AutomaticStopAction TurnOff -CheckpointType Disabled
        Set-ProbePhase $record $journal 'fixture_memory_bound'
        Set-VMMemory -VM $vm -DynamicMemoryEnabled $false
        if(-not (Test-ProbeFixedMemory (Get-VMMemory -VM $vm))){throw 'fixture_memory_bound_unverified'}
        Set-ProbePhase $record $journal 'fixture_cpu_bound'
        Set-VMProcessor -VM $vm -Count 1 -Maximum 10
        Set-ProbePhase $record $journal 'fixture_target_validation'
        $null=Test-ProbeTarget
        Set-ProbePhase $record $journal 'fixture_service_creation'
        for($i=0;$i -lt 2;$i++){
            $role=if($i -eq 0){'false'}else{'true'};$dir=Join-Path $script:root ('service'+$i)
            $null=New-ProbeDirectory $dir @($sids[$i]);$null=New-ProbeDirectory (Join-Path $dir 'result') @() $sids[$i]
            $exe=Join-Path $dir 'probe.exe';[IO.File]::WriteAllBytes($exe,$bytes)
            $fixture=[pscustomobject]@{name=$names[$i];sid=$sids[$i];root=$dir;role=$role;processes=@{}};$fixtures+=@($fixture)
            Invoke-ProbeSc @('create',$fixture.name,'binPath=',('"'+$exe+'"'),'start=','demand','obj=',('NT SERVICE\'+$fixture.name))
            Invoke-ProbeSc @('sidtype',$fixture.name,'unrestricted')
            $resolved=([Security.Principal.NTAccount]::new('NT SERVICE\'+$fixture.name)).Translate([Security.Principal.SecurityIdentifier]).Value
            if($resolved -cne $fixture.sid){throw 'fixture_service_sid_mismatch'}
            if($role -ceq 'true'){Add-LocalGroupMember -SID 'S-1-5-32-578' -Member $fixture.sid}
        }
        $positive=$fixtures[1];$negative=$fixtures[0]
        Set-ProbePhase $record $journal 'positive_power_on'
        $results+=@(Invoke-ProbeOperation $positive 'power_on' 1)
        if((Test-ProbeTarget).State -ne 'Running'){throw 'positive_power_not_applied'}
        Set-ProbePhase $record $journal 'positive_control_power_off'
        $results+=@(Invoke-ProbeOperation $positive 'power_off' 2)
        if((Test-ProbeTarget).State -ne 'Off'){throw 'positive_stop_not_applied'}
        Set-ProbePhase $record $journal 'positive_control_power_on'
        $results+=@(Invoke-ProbeOperation $positive 'power_on' 3)
        if((Test-ProbeTarget).State -ne 'Running'){throw 'positive_power_not_applied'}
        # The running current-setting descriptor is frozen after the control cycle.
        New-ProbeTargetDescriptor
        $record['target_descriptor_sha256']=$script:targetDescriptorSha256
        $record['settings_payload_sha256']=$script:settingsPayloadSha256
        $record['settings_payload_bytes']=$script:settingsPayload.Length
        $record['positive_negative_dataset_identical']=$true
        $record['settings_input_state']='Running'
        $record['settings_target_path_sha256']=$script:settingsTargetPathSha256
        $record['settings_instance_sha256']=$script:settingsInstanceSha256
        foreach($fixture in $fixtures){Write-ProbePayload $fixture.root $fixture.sid}
        Set-ProbePhase $record $journal 'positive_settings'
        $results+=@(Invoke-ProbeOperation $positive 'settings' 4)
        $vm=Get-VM -Id $script:vmId
        if($vm.State -ne 'Running'){throw 'positive_power_not_applied'}
        $fresh=Get-ProbeCurrentSettingsObservation
        $record['settings_readback']=[ordered]@{wmi_current=$fresh;notes_length=([string]$vm.Notes).Length;original_marker_exact=($vm.Notes -ceq ('ha-owned-provider-probe:'+$script:installation));tested_marker_exact=($vm.Notes -ceq ('ha-owned-provider-probe:'+$script:installation+':tested'));notes_sha256=(Get-ProbeHash ([Text.Encoding]::UTF8.GetBytes([string]$vm.Notes)))}
        if(-not (Test-ProbeCurrentSettingsExact $fresh $true) -or $vm.Notes -cne ('ha-owned-provider-probe:'+$script:installation+':tested')){throw 'positive_settings_not_applied'}
        Set-VM -VM $vm -Notes ('ha-owned-provider-probe:'+$script:installation)
        $reset=Get-ProbeCurrentSettingsObservation
        $record['settings_reset_readback']=$reset
        if(-not (Test-ProbeCurrentSettingsExact $reset $false)){throw 'fixture_vm_ownership_changed'}
        if((Test-ProbeTarget).State -ne 'Running'){throw 'positive_power_not_applied'}
        Set-ProbePhase $record $journal 'negative_settings'
        $results+=@(Invoke-ProbeOperation $negative 'settings' 5)
        $negativeReadback=Get-ProbeCurrentSettingsObservation
        $record['negative_settings_readback']=$negativeReadback
        if(-not (Test-ProbeCurrentSettingsExact $negativeReadback $false)){throw 'negative_settings_changed_state'}
        if((Test-ProbeTarget).State -ne 'Running'){throw 'negative_settings_changed_state'}
        Set-ProbePhase $record $journal 'negative_power_off'
        $results+=@(Invoke-ProbeOperation $negative 'power_off' 6)
        if((Test-ProbeTarget).State -ne 'Running'){throw 'negative_power_changed_state'}
        Set-ProbePhase $record $journal 'positive_power_off'
        $results+=@(Invoke-ProbeOperation $positive 'power_off' 7)
        if((Test-ProbeTarget).State -ne 'Off'){throw 'positive_stop_not_applied'}
    $complete=$true
    }catch{
        $failureType=$_.Exception.GetType().Name
        $failureReason=Get-ProbeFailureReason $_.Exception.Message
        throw
    }finally{
        foreach($fixture in $fixtures){
            # Revoke only our fresh group membership even if process waiting
            # later fails. This does not revoke a previously issued token.
            if($fixture.role -ceq 'true'){
                try{
                    $members=@(Get-LocalGroupMember -SID 'S-1-5-32-578'|ForEach-Object{$_.SID.Value})
                    if($fixture.sid -in $members){Remove-LocalGroupMember -SID 'S-1-5-32-578' -Member $fixture.sid}
                }catch{$clean=$false}
            }
            try{
                $service=Get-Service -Name $fixture.name -ErrorAction SilentlyContinue
                if($null -ne $service){
                    $actual=Get-CimInstance Win32_Service -Filter ("Name='"+$fixture.name+"'")
                    if($actual.StartName -ine ('NT SERVICE\'+$fixture.name) -or -not (Test-ProbeServiceImagePath $actual.PathName (Join-Path $fixture.root 'probe.exe'))){throw 'fixture_service_ownership_changed'}
                    if($service.Status -ne 'Stopped'){Stop-Service -Name $fixture.name}
                    Wait-ProbeProcesses $fixture
                    Invoke-ProbeSc @('delete',$fixture.name)
                    $watch=[Diagnostics.Stopwatch]::StartNew()
                    do{
                        $remaining=Get-CimInstance Win32_Service -Filter ("Name='"+$fixture.name+"'")
                        if($null -eq $remaining){break}
                        Start-Sleep -Milliseconds 100
                    }while($watch.Elapsed.TotalSeconds -lt 5)
                    if($null -ne $remaining){throw 'fixture_service_delete_unverified'}
                }
            }catch{$clean=$false}
        }
        if($script:pendingOperation){$clean=$false}
        if($null -ne $script:vmId -and $clean){
            try{
                $vm=Get-VM -Id $script:vmId -ErrorAction Stop
                if($vm.Name -cne $script:vmName -or $vm.Notes -notin @(('ha-owned-provider-probe:'+$script:installation),('ha-owned-provider-probe:'+$script:installation+':tested'))){throw 'fixture_vm_ownership_changed'}
                if($vm.State -ne 'Off'){Stop-VM -VM $vm -TurnOff -Confirm:$false}
                Remove-VM -VM $vm -Force
            }catch{$clean=$false}
        }
        $afterVm=@(Get-VM|ForEach-Object{$_.Id.ToString()}|Sort-Object)
        $afterMembers=@(Get-LocalGroupMember -SID 'S-1-5-32-578'|ForEach-Object{$_.SID.Value}|Sort-Object)
        $namespaceAfter=Get-ProbeNamespaceHash
        $adminsAfter=@(Get-LocalGroupMember -SID 'S-1-5-32-544'|ForEach-Object{$_.SID.Value}|Sort-Object)
        $bootAfter=(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().Ticks.ToString()
        if($namespaceBefore -cne $namespaceAfter -or ($adminsBefore -join ',') -cne ($adminsAfter -join ',') -or $bootBefore -cne $bootAfter){$clean=$false}
        if(($beforeVm -join ',') -cne ($afterVm -join ',') -or ($beforeMembers -join ',') -cne ($afterMembers -join ',')){$clean=$false}
        $record.complete=$complete;$record['cleanup_verified']=$clean
        $record['failure_type']=$failureType;$record['failure_reason']=$failureReason
        [IO.File]::WriteAllText($journal,($record|ConvertTo-Json -Depth 6),[Text.UTF8Encoding]::new($false))
        # Leave the private receipts/configuration for reconciliation. Never
        # recursively delete a directory after a failed/ambiguous remote step.
        [ordered]@{scope='fresh_disconnected_vm_provider_role_acceptance';source_commit=$SourceCommit;probe_source_sha256=$ProbeSourceSha256;artifact_sha256=$ArtifactSha256;
            complete=$complete;cleanup_verified=$clean;phase=$record.phase;failure_type=$failureType;failure_reason=$failureReason;namespace_descriptor_unchanged=($namespaceBefore -ceq $namespaceAfter);namespace_acl_write_performed=$false;
            namespace_sacl_acceptance=$false;boot_unchanged=($bootBefore -ceq $bootAfter);production_transition=$false;results=$results;probe_diagnostics=$script:probeDiagnostics;settings_readback=$record.settings_readback;target_descriptor_sha256=$record.target_descriptor_sha256;settings_payload_sha256=$record.settings_payload_sha256;positive_negative_dataset_identical=$record.positive_negative_dataset_identical}|ConvertTo-Json -Depth 6 -Compress
    }
}
if($MyInvocation.InvocationName -ne '.'){Invoke-ProviderAcceptance}
