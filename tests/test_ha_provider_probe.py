"""Probe grammar/denial classification, without claiming native WMI acceptance."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'infra/k8s/ha_control/windows-provider-probe.cs'


class ProviderProbeTests(unittest.TestCase):
    def test_reconciliation_is_unarmed_and_cannot_start_or_stop_resources(self):
        pwsh=shutil.which('pwsh') or '/private/tmp/n100-ha-powershell/runtime/pwsh'
        env=dict(os.environ, CLEANUP=str(ROOT/'infra/k8s/ha_control/windows-provider-cleanup.ps1'))
        script=r'''
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($env:CLEANUP,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'parse_failed'}
. $env:CLEANUP
$blocked=$false;try{Invoke-ProviderCleanup}catch{$blocked=$true}
if(-not $blocked){throw 'unarmed_cleanup_accepted'}
$commands=@($ast.FindAll({param($a) $a -is [Management.Automation.Language.CommandAst]},$true)|ForEach-Object{$_.GetCommandName()})
foreach($forbidden in @('Start-VM','Stop-VM','Set-VM','New-VM','Start-Service','Stop-Service','New-Service','Restart-Computer','Remove-Item','Set-Acl','Add-LocalGroupMember','Remove-LocalGroupMember')){
 if($forbidden -in $commands){throw 'reconciliation_scope_expanded'}
}
$names=@(Get-CleanupExpectedNames ('0123456789abcdef'+'fedcba9876543210'+('a'*32)))
if($names.Count -ne 2 -or $names[0] -cne 'HaProbe_0123456789abcdef' -or $names[1] -cne 'HaProbe_fedcba9876543210'){throw 'names_not_exact'}
if(-not (Test-CleanupServicePath 'C:\Private\probe.exe' 'C:\Private\probe.exe')){throw 'same_path_rejected'}
if(Test-CleanupServicePath 'C:\Private\probe.exe -x' 'C:\Private\probe.exe'){throw 'command_input_accepted'}
if(Test-CleanupServicePath 'C:\Program Files\probe.exe' 'C:\Program Files\probe.exe'){throw 'ambiguous_path_accepted'}
if(-not (Test-CleanupDisconnectedNic ([pscustomobject]@{SwitchName='';SwitchId=$null}))){throw 'disconnected_rejected'}
foreach($nic in @([pscustomobject]@{SwitchName='connected';SwitchId=$null},[pscustomobject]@{SwitchName='';SwitchId=@()},[pscustomobject]@{SwitchName='';SwitchId='unknown'})){
 if(Test-CleanupDisconnectedNic $nic){throw 'unknown_or_connected_accepted'}
}
'parsed-unarmed-cleanup-only'
'''
        result=subprocess.run([pwsh,'-NoProfile','-NonInteractive','-Command',script],env=env,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        self.assertEqual(result.stdout.decode().strip(),'parsed-unarmed-cleanup-only')

    def test_installer_real_powershell_parse_has_explicit_intent(self):
        pwsh=shutil.which('pwsh') or '/private/tmp/n100-ha-powershell/runtime/pwsh'
        env=dict(os.environ, PROBE_INSTALLER=str(ROOT/'infra/k8s/ha_control/windows-provider-acceptance.ps1'))
        script=r'''
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($env:PROBE_INSTALLER,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'parse_failed'}
. $env:PROBE_INSTALLER
if(-not (Test-ProbeFixedMemory ([pscustomobject]@{Startup=[long]268435456;DynamicMemoryEnabled=$false}))){throw 'fixed_native_memory_rejected'}
foreach($memory in @($null,[pscustomobject]@{Startup=[long]268435456;DynamicMemoryEnabled=$true},[pscustomobject]@{Startup=[long]536870912;DynamicMemoryEnabled=$false},[pscustomobject]@{Startup='268435456';DynamicMemoryEnabled=$false},[pscustomobject]@{Startup=[long]268435456;DynamicMemoryEnabled='false'},[pscustomobject]@{Startup=[long]268435456},[pscustomobject]@{DynamicMemoryEnabled=$false})){
 if(Test-ProbeFixedMemory $memory){throw 'dynamic_or_unknown_memory_accepted'}
}
$observed=@{current_path_sha256=('a'*64);current_instance_sha256=('b'*64);current_path_exact=$true;current_instance_exact=$true;original_marker_exact=$false;tested_marker_exact=$true}
if(-not (Test-ProbeCurrentSettingsExact $observed $true)){throw 'exact_wmi_readback_rejected'}
if(Test-ProbeCurrentSettingsExact $observed $false){throw 'unreset_wmi_notes_accepted'}
foreach($field in @('current_path_exact','current_instance_exact','tested_marker_exact')){
 $copy=$observed.Clone();$copy[$field]=$false
 if(Test-ProbeCurrentSettingsExact $copy $true){throw 'stale_or_unpersisted_wmi_accepted'}
 $copy=$observed.Clone();$copy[$field]='true'
 if(Test-ProbeCurrentSettingsExact $copy $true){throw 'wmi_boolean_confusion_accepted'}
}
$nativeFrame=[pscustomobject]@{scope='owned_vm_provider_role_probe';schema_version=5;process_id=12;process_start_ticks='639298437494210749';operation='settings';stage='valid_operation';attempted=$true;complete=$false;authorized=$true;authorization_denied=$false;return_code=0;administrator=$false;hyperv_role=$true;settings_serialized_value_present=$true;settings_readback_verified=$false;native_target_paths_bound=$true;method_result_verified=$true;target_descriptor_sha256=('a'*64);settings_payload_sha256=('b'*64);settings_current_path_sha256=('c'*64);settings_current_instance_sha256=('d'*64);settings_current_association_verified=$true;exception_kind='none';exception_scope='none';exception_hresult=0;native_proxy_security_verified=$true;native_exec_returned=$true;native_exec_hresult=0;native_call_status_hresult=0;native_call_status_terminal=$true;native_call_status=0;native_result_hresult=0;native_output_verified=$true;native_call_result_present=$true;native_call_proxy_hresult=0}
Assert-ProbeResultShape $nativeFrame
$denial=$nativeFrame.PSObject.Copy();$denial.complete=$true;$denial.authorized=$false;$denial.authorization_denied=$true;$denial.return_code=2147942405;$denial.native_exec_hresult=2147942405;$denial.native_call_status_terminal=$false;$denial.native_call_status_hresult=4294967295;$denial.native_call_status=4294967295;$denial.native_result_hresult=4294967295;$denial.native_output_verified=$false;$denial.native_call_result_present=$false;$denial.native_call_proxy_hresult=4294967295
Assert-ProbeResultShape $denial
$terminal=$nativeFrame.PSObject.Copy();$terminal.complete=$true;$terminal.authorized=$false;$terminal.authorization_denied=$true;$terminal.return_code=2147749891;$terminal.native_call_status=2147749891;$terminal.native_result_hresult=4294967295;$terminal.native_output_verified=$false
Assert-ProbeResultShape $terminal
# Status 1 is only a permission to inspect this call's typed output.
$falseStatus=$nativeFrame.PSObject.Copy();$falseStatus.native_call_status=1
Assert-ProbeResultShape $falseStatus
$typedDenied=$falseStatus.PSObject.Copy();$typedDenied.authorized=$false;$typedDenied.authorization_denied=$true;$typedDenied.complete=$true;$typedDenied.return_code=32769
Assert-ProbeResultShape $typedDenied
foreach($row in @(@('native_output_verified',$false),@('native_result_hresult',2147942405),@('native_call_status',2),@('return_code',1))){
 $bad=$typedDenied.PSObject.Copy();$bad.($row[0])=$row[1]
 $blocked=$false;try{Assert-ProbeResultShape $bad}catch{$blocked=$true};if(-not $blocked){throw 'informational_status_or_untyped_output_became_denial'}
}
$bad=$terminal.PSObject.Copy();$bad.native_call_status_hresult=2147749891
$blocked=$false;try{Assert-ProbeResultShape $bad}catch{$blocked=$true}
if(-not $blocked){throw 'poll_api_hresult_misclassified_as_exec_status'}
$bad=$nativeFrame.PSObject.Copy();$bad.native_result_hresult=2147942405;$bad.complete=$true
$blocked=$false;try{Assert-ProbeResultShape $bad}catch{$blocked=$true}
if(-not $blocked){throw 'result_retrieval_error_became_authorized'}
foreach($row in @(@('exception_kind','other'),@('exception_scope','outside_invoke'),@('attempted',$false),@('method_result_verified',$false),@('native_exec_returned',$false),@('native_proxy_security_verified',$false),@('exception_kind','raw_sensitive_exception'))){
 $bad=$denial.PSObject.Copy();$bad.($row[0])=$row[1]
 $blocked=$false;try{Assert-ProbeResultShape $bad}catch{$blocked=$true}
 if(-not $blocked){throw 'unverified_native_frame_accepted'}
}
$outside=$nativeFrame.PSObject.Copy();$outside.authorized=$false;$outside.exception_kind='unauthorized_access';$outside.exception_scope='outside_invoke';$outside.exception_hresult=2147942405
Assert-ProbeResultShape $outside
$projected=$denial.PSObject.Copy();$projected.exception_kind='unauthorized_access';$projected.exception_scope='invoke';$projected.exception_hresult=2147942405
$blocked=$false;try{Assert-ProbeResultShape $projected}catch{$blocked=$true}
if(-not $blocked){throw 'clr_projection_still_granted_native_authority'}
foreach($field in @('settings_serialized_value_present','settings_readback_verified','return_code','process_start_ticks','target_descriptor_sha256','settings_payload_sha256','native_target_paths_bound','method_result_verified','settings_current_path_sha256','settings_current_instance_sha256','settings_current_association_verified','exception_kind','exception_scope','exception_hresult','native_proxy_security_verified','native_exec_returned','native_exec_hresult','native_call_status_hresult','native_call_status_terminal','native_call_status','native_result_hresult','native_output_verified','native_call_result_present','native_call_proxy_hresult')){
 $copy=$nativeFrame.PSObject.Copy();$copy.$field=@($copy.$field)
 $blocked=$false;try{Assert-ProbeResultShape $copy}catch{$blocked=$true}
 if(-not $blocked){throw 'diagnostic_type_confusion_accepted'}
}
$copy=$nativeFrame.PSObject.Copy();$copy|Add-Member NoteProperty extra 'untrusted'
$blocked=$false;try{Assert-ProbeResultShape $copy}catch{$blocked=$true}
if(-not $blocked){throw 'unbounded_diagnostic_fields_accepted'}
# Even a valid typed status-1 output zero is not a negative-role pass.
$zero=$falseStatus.PSObject.Copy();$zero.operation='power_off';$zero.complete=$true
$blocked=$false;try{Assert-ProbeOperationAuthority $zero 'false' 'power_off'}catch{$blocked=$true}
if(-not $blocked){throw 'negative_success_bypassed_role_boundary'}
Assert-ProbeOperationAuthority $zero 'true' 'power_off'
$deniedPower=$typedDenied.PSObject.Copy();$deniedPower.operation='power_off'
Assert-ProbeOperationAuthority $deniedPower 'false' 'power_off'
$receiptRoot=Join-Path ([IO.Path]::GetTempPath()) ('provider-pure-receipts-'+[Guid]::NewGuid().ToString('N'))
$null=[IO.Directory]::CreateDirectory($receiptRoot)
try{
 $on1=Get-ProbeReceiptName 'true' 'power_on' 1;$on2=Get-ProbeReceiptName 'true' 'power_on' 3
 if($on1 -ceq $on2){throw 'planned_repeat_receipt_collapsed'}
 $first=Join-Path $receiptRoot $on1;$second=Join-Path $receiptRoot $on2
 Write-ProbeOperationReceipt $first ([ordered]@{return_code=0;stage=1})
 $before=[IO.File]::ReadAllBytes($first)
 Write-ProbeOperationReceipt $second ([ordered]@{return_code=0;stage=3})
 $blocked=$false;try{Write-ProbeOperationReceipt $first ([ordered]@{return_code=32769;stage=99})}catch{$blocked=$true}
 if(-not $blocked -or [Convert]::ToBase64String($before) -cne [Convert]::ToBase64String([IO.File]::ReadAllBytes($first))){throw 'duplicate_receipt_overwrote_original'}
 foreach($row in @(@('true','power_on',2),@('false','power_off',7),@('true','settings',8),@('false','power_on',0),@('true','unknown',1))){
  $blocked=$false;try{Get-ProbeReceiptName $row[0] $row[1] $row[2]|Out-Null}catch{$blocked=$true};if(-not $blocked){throw 'unplanned_role_operation_accepted'}
 }
}finally{[IO.Directory]::Delete($receiptRoot,$true)}
$names=@(Get-ProbeServiceNames ('0123456789abcdef'+'fedcba9876543210'+('a'*32)))
if($names.Count -ne 2 -or $names[0] -cne 'HaProbe_0123456789abcdef' -or $names[1] -cne 'HaProbe_fedcba9876543210'){throw 'service_names_collapsed'}
$blocked=$false;try{Get-ProbeServiceNames ('a'*64)|Out-Null}catch{$blocked=$true}
if(-not $blocked){throw 'duplicate_service_names_accepted'}
foreach($path in @('C:\Private\probe.exe','"C:\Private\probe.exe"')){
 if(-not (Test-ProbeServiceImagePath $path 'C:\Private\probe.exe')){throw 'same_single_exe_rejected'}
}
foreach($path in @('C:\Private\probe.exe anything','"C:\Private\probe.exe" anything','C:\Private\other.exe','C:\Program Files\probe.exe')){
 if(Test-ProbeServiceImagePath $path 'C:\Private\probe.exe'){throw 'different_argv_accepted'}
}
if(Test-ProbeServiceImagePath 'C:\Program Files\probe.exe' 'C:\Program Files\probe.exe'){throw 'ambiguous_unquoted_path_accepted'}
if(-not (Test-ProbeServiceImagePath '"C:\Program Files\probe.exe"' 'C:\Program Files\probe.exe')){throw 'quoted_path_rejected'}
$frame=[pscustomobject]@{scope='owned_vm_provider_role_probe';schema_version=5;operation='settings';stage='schema_query';attempted=$false;complete=$false;authorized=$false;authorization_denied=$false;administrator=$false;hyperv_role=$false}
if(-not (Test-ProbeNoOperation $frame 'settings' $false)){throw 'known_no_operation_not_recognized'}
foreach($field in @('attempted','complete','authorized','authorization_denied','administrator','hyperv_role')){
 $copy=$frame.PSObject.Copy();$copy.$field=$true
 if(Test-ProbeNoOperation $copy 'settings' $false){throw 'attempt_or_unverified_identity_discarded'}
 $copy=$frame.PSObject.Copy();$copy.$field='false'
 if(Test-ProbeNoOperation $copy 'settings' $false){throw 'string_boolean_accepted'}
}
if(Test-ProbeNoOperation $frame 'power_off' $false){throw 'different_operation_accepted'}
foreach($field in @('scope','operation','stage')){
 $copy=$frame.PSObject.Copy();$copy.$field=@($copy.$field)
 if(Test-ProbeNoOperation $copy 'settings' $false){throw 'array_protocol_field_accepted'}
}
$copy=$frame.PSObject.Copy();$copy.schema_version=$true
if(Test-ProbeNoOperation $copy 'settings' $false){throw 'boolean_schema_accepted'}
foreach($id in @($null,'',[Guid]::Empty,[Guid]::Empty.ToString())){
 if(-not (Test-ProbeDisconnectedNic ([pscustomobject]@{SwitchName='';SwitchId=$id}))){throw 'disconnected_adapter_rejected'}
}
foreach($nic in @([pscustomobject]@{SwitchName='existing';SwitchId=$null},[pscustomobject]@{SwitchName='';SwitchId='11111111-1111-1111-1111-111111111111'},[pscustomobject]@{SwitchName='';SwitchId='unknown'})){
 if(Test-ProbeDisconnectedNic $nic){throw 'connected_or_unknown_adapter_accepted'}
}
foreach($nic in @($null,[pscustomobject]@{SwitchId=''},[pscustomobject]@{SwitchName=''},[pscustomobject]@{SwitchName=@();SwitchId=''},[pscustomobject]@{SwitchName='';SwitchId=@()},[pscustomobject]@{SwitchName=[pscustomobject]@{};SwitchId=''},[pscustomobject]@{SwitchName=' ';SwitchId=''},[pscustomobject]@{SwitchName='';SwitchId=' '})){
 if(Test-ProbeDisconnectedNic $nic){throw 'malformed_adapter_accepted'}
}
if((Get-ProbeFailureReason 'fixture_vm_ownership_changed') -cne 'fixture_vm_ownership_changed'){throw 'fixed_reason_lost'}
foreach($message in @('sensitive_internal_account','Fixture_vm_ownership_changed','some secret','C:\private')){
 if((Get-ProbeFailureReason $message) -cne 'unclassified_runtime_error'){throw 'external_exception_reflected'}
}
$blocked=$false;try{Invoke-ProviderAcceptance}catch{$blocked=$true}
if(-not $blocked){throw 'unapproved_execution_accepted'}
$commands=@($ast.FindAll({param($a) $a -is [Management.Automation.Language.CommandAst]},$true)|ForEach-Object{$_.GetCommandName()})
foreach($forbidden in @('Set-Acl','Restart-Computer','Set-NetIPAddress','New-NetNat','New-VMSwitch','Invoke-Expression','Stop-Process')){
 if($forbidden -in $commands){throw 'fixture_scope_expanded'}
}
'parsed-and-unarmed-only'
'''
        result=subprocess.run([pwsh,'-NoProfile','-NonInteractive','-Command',script],env=env,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        self.assertEqual(result.stdout.decode().strip(),'parsed-and-unarmed-only')

    def test_fixed_grammar_and_exact_authorization_denial(self):
        pwsh=shutil.which('pwsh') or '/private/tmp/n100-ha-powershell/runtime/pwsh'
        env=dict(os.environ, PROBE_SOURCE=str(SOURCE))
        script=r'''
$ErrorActionPreference='Stop'
Add-Type -Path $env:PROBE_SOURCE -CompilerOptions '/define:PORTABLE_FIXTURE'
$vm='11111111-1111-1111-1111-111111111111'
$payload=[Text.Encoding]::UTF8.GetBytes(('ha-owned-provider-probe:'+('a'*64)+':tested '+$vm))
$payloadHash=[HaProviderProbeV1.Settings]::Hash($payload)
$service=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('Msvm_VirtualSystemManagementService.Name="service"'))
$target=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('Msvm_VirtualSystemSettingData.InstanceID="Microsoft:config"'))
$descriptor="format=ha-provider-target-v3`ninstallation_id=$('a'*64)`nvm_id=$vm`nsettings_vm_state=Running`nsettings_virtual_system_type=Microsoft:Hyper-V:System:Realized`nsettings_virtual_system_identifier=$vm`nsettings_service_path_b64=$service`nsettings_target_path_b64=$target`nsettings_payload_sha256=$payloadHash`nsettings_payload_bytes=$($payload.Length)`n"
$descriptorBytes=[Text.Encoding]::UTF8.GetBytes($descriptor)
$descriptorHash=[HaProviderProbeV1.Settings]::Hash($descriptorBytes)
$descriptorB64=[Convert]::ToBase64String($descriptorBytes)
$text="format=ha-provider-probe-v5`nservice_name=HaProbe_0123456789abcdef`nservice_sid=S-1-5-80-1-2-3-4-5`ninstallation_id=$('a'*64)`nvm_id=11111111-1111-1111-1111-111111111111`noperation=settings`nexpected_hyperv_role=false`ntarget_descriptor_b64=$descriptorB64`ntarget_descriptor_sha256=$descriptorHash`n"
$cfg=[HaProviderProbeV1.Settings]::new($text)
if($cfg.Marker() -cne ('ha-owned-provider-probe:'+('a'*64))){throw 'not_fixed_owned_marker'}
# A CLR projected E_ACCESSDENIED is not necessarily a COMException.
$projected=$null
try{[Runtime.InteropServices.Marshal]::ThrowExceptionForHR([int]-2147024891,[IntPtr]::new(-1))}catch{$projected=$_.Exception.InnerException}
if($null -eq $projected -or $projected.GetType() -ne [UnauthorizedAccessException]){throw 'clr_access_denied_mapping_missing'}
if([HaProviderProbeV1.Settings]::ExceptionKind($projected) -cne 'unauthorized_access'){throw 'projected_diagnostic_kind_lost'}
Add-Type -TypeDefinition 'public sealed class ProbeFakeDenied : System.Exception {public ProbeFakeDenied(){HResult=unchecked((int)0x80070005);}} public sealed class ProbeDerivedDenied : System.UnauthorizedAccessException {}'
foreach($other in @([ProbeFakeDenied]::new(),[ProbeDerivedDenied]::new(),[InvalidOperationException]::new('never output this detail'))){
 if([HaProviderProbeV1.Settings]::ExceptionKind($other) -cne 'other'){throw 'unrecognized_type_was_not_static_other'}
}
if($null -ne [HaProviderProbeV1.Settings].GetMethod('InvokeExceptionDenial')){throw 'clr_exception_authority_helper_retained'}
# The exact native HRESULT/terminal-status state machine is portable. No COM call.
foreach($code in @([uint32]2147749891,[uint32]2147942405)){
 $e=[HaProviderProbeV1.ExecEvidence]::new();$e.Started=$true;$e.Returned=$true;$e.ExecHresult=$code
 if(-not $e.Denied() -or -not $e.Verified() -or $e.Code() -ne $code){throw 'immediate_exec_denial_lost'}
 $e.Started=$false;if($e.Denied() -or $e.Verified()){throw 'preexec_hresult_granted_authority'}
 $e=[HaProviderProbeV1.ExecEvidence]::new();$e.Started=$true;$e.Returned=$true;$e.ExecHresult=0;$e.CallResultPresent=$true;$e.CallProxyHresult=0;$e.PollHresult=0;$e.Terminal=$true;$e.Status=$code
 if(-not $e.Denied() -or -not $e.Verified() -or $e.Code() -ne $code){throw 'terminal_exec_denial_lost'}
 $e.PollHresult=$code;if($e.Denied() -or $e.Verified()){throw 'poll_api_denial_granted_authority'}
}
$ok=[HaProviderProbeV1.ExecEvidence]::new();$ok.Started=$true;$ok.Returned=$true;$ok.ExecHresult=0;$ok.CallResultPresent=$true;$ok.CallProxyHresult=0;$ok.PollHresult=0;$ok.Terminal=$true;$ok.Status=0;$ok.OutputHresult=0;$ok.OutputVerified=$true;$ok.ReturnValue=0
if(-not $ok.Verified() -or $ok.Denied() -or $ok.Code() -ne 0){throw 'verified_native_output_lost'}
$ok.ReturnValue=32769;if(-not $ok.Denied()){throw 'provider_return_denial_lost'}
foreach($field in @('Started','Returned','CallResultPresent','Terminal','OutputVerified')){
 $copy=[HaProviderProbeV1.ExecEvidence]::new();foreach($f in $ok.GetType().GetFields()){if($f.Name -ne 'Job'){$f.SetValue($copy,$f.GetValue($ok))}};$copy.$field=$false
 if($copy.Denied() -or $copy.Verified()){throw 'incomplete_native_trace_granted_authority'}
}
foreach($row in @(@('PollHresult',[uint32]262148),@('CallProxyHresult',[uint32]2147942405),@('OutputHresult',[uint32]2147942405))){
 $copy=[HaProviderProbeV1.ExecEvidence]::new();foreach($f in $ok.GetType().GetFields()){if($f.Name -ne 'Job'){$f.SetValue($copy,$f.GetValue($ok))}};$copy.($row[0])=$row[1]
 if($copy.Denied() -or $copy.Verified()){throw 'timeout_proxy_or_output_error_granted_authority'}
}
foreach($status in @([uint32]0,[uint32]1)){
 $trace=[HaProviderProbeV1.ExecEvidence]::new();foreach($f in $ok.GetType().GetFields()){if($f.Name -ne 'Job'){$f.SetValue($trace,$f.GetValue($ok))}};$trace.Status=$status
 $trace.OutputVerified=$false;$trace.ReturnValue=32769
 if($trace.Verified() -or $trace.Denied() -or $trace.Code() -ne [uint32]::MaxValue){throw 'terminal_status_without_output_granted_authority'}
 $trace.OutputVerified=$true
 if(-not $trace.Verified() -or -not $trace.Denied() -or $trace.Code() -ne 32769){throw 'typed_output_denial_lost_or_status_used_as_code'}
 $trace.ReturnValue=0
 if(-not $trace.Verified() -or $trace.Denied() -or $trace.Code() -ne 0){throw 'typed_output_zero_lost'}
}
foreach($status in @([uint32]2,[uint32]3,[uint32]262148)){
 $trace=[HaProviderProbeV1.ExecEvidence]::new();foreach($f in $ok.GetType().GetFields()){if($f.Name -ne 'Job'){$f.SetValue($trace,$f.GetValue($ok))}};$trace.Status=$status
 if([HaProviderProbeV1.ExecEvidence]::OutputStatus($status) -or $trace.Verified() -or $trace.Denied()){throw 'other_informational_status_granted_authority'}
}
$typed=[uint32]0
foreach($value in @('32769',[long]32769,[double]32769,$null,$true)){
 if([HaProviderProbeV1.ExecEvidence]::TryReturnValue($value,19,[ref]$typed)){throw 'malformed_variant_accepted'}
}
if([HaProviderProbeV1.ExecEvidence]::TryReturnValue([int]32769,8,[ref]$typed)){throw 'wrong_cim_type_accepted'}
if(-not [HaProviderProbeV1.ExecEvidence]::TryReturnValue([int]32769,19,[ref]$typed) -or $typed -ne 32769){throw 'typed_cim_uint32_rejected'}
if($cfg.Descriptor.VerifyPayload($payload,$cfg.Marker()) -cne [Text.Encoding]::UTF8.GetString($payload)){throw 'payload_not_exact'}
foreach($bad in @([Text.Encoding]::UTF8.GetBytes('changed'),($payload+[byte]0))){
 $blocked=$false;try{$cfg.Descriptor.VerifyPayload($bad,$cfg.Marker())|Out-Null}catch{$blocked=$true}
 if(-not $blocked){throw 'payload_tampering_accepted'}
}
foreach($bad in @($descriptor.Replace('vm_id='+$vm,'vm_id=22222222-2222-2222-2222-222222222222'),$descriptor.Replace('Realized','Planned'),$descriptor.Replace('settings_vm_state=Running','settings_vm_state=Off'),($descriptor+"extra=1`n"),$descriptor.Replace('settings_payload_bytes='+$payload.Length,'settings_payload_bytes=1048577'))){
 $hash=[HaProviderProbeV1.Settings]::Hash([Text.Encoding]::UTF8.GetBytes($bad))
 $blocked=$false;try{[HaProviderProbeV1.TargetDescriptor]::new($bad,$hash,('a'*64),$vm)|Out-Null}catch{$blocked=$true}
 if(-not $blocked){throw 'wrong_descriptor_identity_accepted'}
}
$blocked=$false;try{[HaProviderProbeV1.TargetDescriptor]::new(($descriptor+'x'),$descriptorHash,('a'*64),$vm)|Out-Null}catch{$blocked=$true}
if(-not $blocked){throw 'external_descriptor_pin_ignored'}
$power=$text.Replace('operation=settings','operation=power_on').Replace('target_descriptor_b64='+$descriptorB64,'target_descriptor_b64=').Replace('target_descriptor_sha256='+$descriptorHash,'target_descriptor_sha256=')
$powerCfg=[HaProviderProbeV1.Settings]::new($power)
if($null -ne $powerCfg.Descriptor){throw 'power_took_settings_descriptor'}
foreach($bad in @($power.Replace('target_descriptor_b64=','target_descriptor_b64='+$descriptorB64),$power.Replace('operation=power_on','operation=settings'),$text.Replace('ha-provider-probe-v5','ha-provider-probe-v2'))){
 $blocked=$false;try{[HaProviderProbeV1.Settings]::new($bad)|Out-Null}catch{$blocked=$true}
 if(-not $blocked){throw 'cross_version_or_operation_inputs_accepted'}
}
$id='abcdef12-abcd-abcd-abcd-abcdefabcdef'
$marker='ha-owned-provider-probe:'+('a'*64)
if(-not [HaProviderProbeV1.Settings]::CurrentReadback('Microsoft:Hyper-V:System:Realized',$id,$id,'current-path','current-path',@($marker+':tested'),$marker)){throw 'exact_current_readback_rejected'}
foreach($row in @(@('stale-path','current-path',($marker+':tested')),@('current-path','current-path',$marker))){
 if([HaProviderProbeV1.Settings]::CurrentReadback('Microsoft:Hyper-V:System:Realized',$id,$id,$row[0],$row[1],@($row[2]),$marker)){throw 'stale_path_or_unpersisted_notes_accepted'}
}
if(-not [HaProviderProbeV1.Settings]::CurrentSetting('Microsoft:Hyper-V:System:Realized',$id.ToUpperInvariant(),$id)){throw 'v2_current_setting_rejected'}
$relative='Msvm_ComputerSystem.Name="'+$id+'"'
foreach($server in @('', '.')){
 if(-not [HaProviderProbeV1.Settings]::NativeRelativeTarget($true,'Msvm_ComputerSystem','Msvm_ComputerSystem',$server,'',$relative,$relative,$relative)){throw 'implicit_local_path_rejected'}
}
foreach($server in @('localhost','remote','127.0.0.1','..')){
 if([HaProviderProbeV1.Settings]::NativeRelativeTarget($true,'Msvm_ComputerSystem','Msvm_ComputerSystem',$server,'',$relative,$relative,$relative)){throw 'unverified_server_accepted'}
}
if([HaProviderProbeV1.Settings]::NativeRelativeTarget($true,'Msvm_ComputerSystem','Msvm_ComputerSystem','.','root\virtualization\v2',$relative,$relative,$relative)){throw 'explicit_namespace_accepted'}
if([HaProviderProbeV1.Settings]::NativeRelativeTarget($true,'Msvm_ComputerSystem','Msvm_ComputerSystem','.','',('\\.\root\virtualization\v2:'+$relative),$relative,$relative)){throw 'explicit_full_path_accepted'}
if([HaProviderProbeV1.Settings]::NativeRelativeTarget($false,'Msvm_ComputerSystem','Msvm_ComputerSystem','.','',$relative,$relative,$relative)){throw 'class_path_accepted'}
if([HaProviderProbeV1.Settings]::NativeRelativeTarget($true,'OtherClass','Msvm_ComputerSystem','.','',$relative,$relative,$relative)){throw 'other_class_accepted'}
if([HaProviderProbeV1.Settings]::NativeRelativeTarget($true,'Msvm_ComputerSystem','Msvm_ComputerSystem','.','',$relative,$relative,($relative+'x'))){throw 'different_source_path_accepted'}
foreach($type in @('Microsoft:Hyper-V:System:Planned','Microsoft:Hyper-V:Snapshot:Realized','microsoft:hyper-v:system:realized',$null)){
 if([HaProviderProbeV1.Settings]::CurrentSetting($type,$id,$id)){throw 'non_current_setting_accepted'}
}
foreach($identifier in @($null,(' '+$id),('{'+$id+'}'),'11111111-1111-1111-1111-111111111111')){
 if([HaProviderProbeV1.Settings]::CurrentSetting('Microsoft:Hyper-V:System:Realized',$identifier,$id)){throw 'different_or_malformed_system_accepted'}
}
foreach($code in @([uint32]32769,[uint32]2147749891,[uint32]2147942405)){
 if(-not [HaProviderProbeV1.Settings]::AuthorizationDenial($true,$code)){throw 'denial_not_classified'}
 if([HaProviderProbeV1.Settings]::AuthorizationDenial($false,$code)){throw 'query_failure_counted_as_method_denial'}
}
foreach($code in @([uint32]0,[uint32]1,[uint32]5,[uint32]4096,[uint32]32768,[uint32]32770,[uint32]4294967295)){
 if([HaProviderProbeV1.Settings]::AuthorizationDenial($true,$code)){throw 'generic_failure_counted_as_denial'}
}
foreach($bad in @($text.Replace('operation=settings','operation=shell'),$text.Replace('expected_hyperv_role=false','expected_hyperv_role=unknown'),$text.Replace('HaProbe_0123456789abcdef','ExistingService'),$text.Replace('11111111-1111-1111-1111-111111111111',"x' OR 1=1"),($text+"extra=1`n"),$text.Replace("`n","`r`n"))){
 $blocked=$false;try{[HaProviderProbeV1.Settings]::new($bad)|Out-Null}catch{$blocked=$true}
 if(-not $blocked){throw 'invalid_probe_input_accepted'}
}
'portable-grammar-only'
'''
        result=subprocess.run([pwsh,'-NoProfile','-NonInteractive','-Command',script],env=env,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        self.assertEqual(result.stdout.decode().strip(),'portable-grammar-only')

    def test_direct_call_has_no_target_query_and_denial_is_exec_only(self):
        source=SOURCE.read_text()
        prepare=source[source.index('public DirectWmi('):source.index('public void Execute(')]
        execute=source[source.index('public void Execute('):source.index('public void Dispose()')]
        self.assertIn('services.GetObject(className,0x10',prepare)
        self.assertNotIn('services.GetObject(path',prepare)
        self.assertNotIn('InvokeMethod(',source)
        self.assertNotIn('new ManagementClass(',source)
        self.assertIn('klass.GetMethod(method,0',prepare)
        self.assertIn('inputSignature.SpawnInstance(0',prepare)
        self.assertIn('input.Put(property,0,ref value,0)',prepare)
        self.assertIn('beforeExec();trace.Started=true;',execute)
        self.assertIn('services.ExecMethod(path,method,0x10,IntPtr.Zero,input,IntPtr.Zero,out result)',execute)
        self.assertIn('result.GetCallStatus(left,out status)',execute)
        self.assertIn('result.GetResultObject(0,out output)',execute)
        self.assertIn('Blanket(result)',execute)
        self.assertIn('CoSetProxyBlanket(proxy,10,0,IntPtr.Zero,6,3,IntPtr.Zero,0)',source)
        self.assertIn('ConnectServer(@"root\\virtualization\\v2",null,null,null,0,null,IntPtr.Zero',source)
        self.assertIn('Settings.NativeRelativeTarget(',source)
        # Verify published COM vtable method order, not only textual API names.
        import re
        expected={
            'NativeLocator':['ConnectServer'],
            'NativeCallResult':['GetResultObject','GetResultString','GetResultServices','GetCallStatus'],
            'NativeServices':['OpenNamespace','CancelAsyncCall','QueryObjectSink','GetObject','GetObjectAsync','PutClass','PutClassAsync','DeleteClass','DeleteClassAsync','CreateClassEnum','CreateClassEnumAsync','PutInstance','PutInstanceAsync','DeleteInstance','DeleteInstanceAsync','CreateInstanceEnum','CreateInstanceEnumAsync','ExecQuery','ExecQueryAsync','ExecNotificationQuery','ExecNotificationQueryAsync','ExecMethod','ExecMethodAsync'],
            'NativeObject':['GetQualifierSet','Get','Put','Delete','GetNames','BeginEnumeration','Next','EndEnumeration','GetPropertyQualifierSet','Clone','GetObjectText','SpawnDerivedClass','SpawnInstance','CompareTo','GetPropertyOrigin','InheritsFrom','GetMethod','PutMethod','DeleteMethod','BeginMethodEnumeration','NextMethod','EndMethodEnumeration','GetMethodQualifierSet','GetMethodOrigin'],
        }
        for name,methods in expected.items():
            body=source.split('interface '+name+' {',1)[1].split('\n }',1)[0]
            self.assertEqual(re.findall(r'PreserveSig\] int (\w+)\(',body),methods)
        parent=(ROOT/'infra/k8s/ha_control/windows-provider-acceptance.ps1').read_text()
        creation=parent[parent.index('$vm=New-VM'):parent.index("'fixture_service_creation'")]
        self.assertLess(creation.index("'fixture_memory_bound'"),creation.index("'fixture_cpu_bound'"))
        self.assertLess(creation.index('Set-VMMemory -VM $vm -DynamicMemoryEnabled $false'),creation.index("'fixture_target_validation'"))
        self.assertEqual(parent.count('Set-VMMemory -VM $vm -DynamicMemoryEnabled $false'),1)
        self.assertIn('Test-ProbeFixedMemory (Get-VMMemory -VM $vm)',creation)

        operations=parent[parent.index('$positive=$fixtures[1]'):parent.index('$complete=$true')]
        expected=["'positive_power_on'","'positive_control_power_off'","'positive_control_power_on'","'positive_settings'","'negative_settings'","'negative_power_off'","'positive_power_off'"]
        offsets=[operations.index(item) for item in expected]
        self.assertEqual(offsets,sorted(offsets))
        self.assertGreater(operations.index('New-ProbeTargetDescriptor'),offsets[2])
        self.assertLess(operations.index('New-ProbeTargetDescriptor'),offsets[3])
        calls=["Invoke-ProbeOperation $positive 'power_on' 1","Invoke-ProbeOperation $positive 'power_off' 2","Invoke-ProbeOperation $positive 'power_on' 3","Invoke-ProbeOperation $positive 'settings' 4","Invoke-ProbeOperation $negative 'settings' 5","Invoke-ProbeOperation $negative 'power_off' 6","Invoke-ProbeOperation $positive 'power_off' 7"]
        self.assertEqual([operations.index(x) for x in calls],sorted(operations.index(x) for x in calls))
        cycle=operations[offsets[1]:operations.index('New-ProbeTargetDescriptor')]
        self.assertIn("(Test-ProbeTarget).State -ne 'Off'",cycle)
        self.assertIn("(Test-ProbeTarget).State -ne 'Running'",cycle)
        invoke=parent[parent.index('function Invoke-ProbeOperation'):parent.index('function Test-ProbeTarget')]
        self.assertLess(invoke.index('if(Test-Path -LiteralPath $saved)'),invoke.index('Start-Service'))
        self.assertLess(invoke.index('Write-ProbeOperationReceipt $saved $result'),invoke.rindex('$script:pendingOperation=$false'))
        self.assertIn('Assert-ProbeOperationAuthority $result $Fixture.role $Operation',invoke)
        self.assertEqual(parent.count('GetText([Management.TextFormat]::WmiDtd20)'),1)
        self.assertIn('-not (Test-ProbeCurrentSettingsExact $fresh $true) -or $vm.Notes -cne',parent)
        self.assertIn('Test-ProbeCurrentSettingsExact $reset $false',parent)
        self.assertIn('Test-ProbeCurrentSettingsExact $negativeReadback $false',parent)


if __name__=='__main__':
    unittest.main()
