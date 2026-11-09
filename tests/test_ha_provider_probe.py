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
$nativeFrame=[pscustomobject]@{scope='owned_vm_provider_role_probe';schema_version=2;process_id=12;process_start_ticks='639298437494210749';operation='settings';stage='valid_operation';attempted=$true;complete=$false;authorized=$true;authorization_denied=$false;return_code=0;administrator=$false;hyperv_role=$true;settings_serialized_value_present=$true;settings_readback_verified=$false;native_target_paths_bound=$true;method_result_verified=$true;target_descriptor_sha256=('a'*64);settings_payload_sha256=('b'*64)}
Assert-ProbeResultShape $nativeFrame
foreach($field in @('settings_serialized_value_present','settings_readback_verified','return_code','process_start_ticks','target_descriptor_sha256','settings_payload_sha256','native_target_paths_bound','method_result_verified')){
 $copy=$nativeFrame.PSObject.Copy();$copy.$field=@($copy.$field)
 $blocked=$false;try{Assert-ProbeResultShape $copy}catch{$blocked=$true}
 if(-not $blocked){throw 'diagnostic_type_confusion_accepted'}
}
$copy=$nativeFrame.PSObject.Copy();$copy|Add-Member NoteProperty extra 'untrusted'
$blocked=$false;try{Assert-ProbeResultShape $copy}catch{$blocked=$true}
if(-not $blocked){throw 'unbounded_diagnostic_fields_accepted'}
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
$frame=[pscustomobject]@{scope='owned_vm_provider_role_probe';schema_version=2;operation='settings';stage='schema_query';attempted=$false;complete=$false;authorized=$false;authorization_denied=$false;administrator=$false;hyperv_role=$false}
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
$descriptor="format=ha-provider-target-v2`ninstallation_id=$('a'*64)`nvm_id=$vm`nsettings_virtual_system_type=Microsoft:Hyper-V:System:Realized`nsettings_virtual_system_identifier=$vm`nsettings_service_path_b64=$service`nsettings_target_path_b64=$target`nsettings_payload_sha256=$payloadHash`nsettings_payload_bytes=$($payload.Length)`n"
$descriptorBytes=[Text.Encoding]::UTF8.GetBytes($descriptor)
$descriptorHash=[HaProviderProbeV1.Settings]::Hash($descriptorBytes)
$descriptorB64=[Convert]::ToBase64String($descriptorBytes)
$text="format=ha-provider-probe-v2`nservice_name=HaProbe_0123456789abcdef`nservice_sid=S-1-5-80-1-2-3-4-5`ninstallation_id=$('a'*64)`nvm_id=11111111-1111-1111-1111-111111111111`noperation=settings`nexpected_hyperv_role=false`ntarget_descriptor_b64=$descriptorB64`ntarget_descriptor_sha256=$descriptorHash`n"
$cfg=[HaProviderProbeV1.Settings]::new($text)
if($cfg.Marker() -cne ('ha-owned-provider-probe:'+('a'*64))){throw 'not_fixed_owned_marker'}
if($cfg.Descriptor.VerifyPayload($payload,$cfg.Marker()) -cne [Text.Encoding]::UTF8.GetString($payload)){throw 'payload_not_exact'}
foreach($bad in @([Text.Encoding]::UTF8.GetBytes('changed'),($payload+[byte]0))){
 $blocked=$false;try{$cfg.Descriptor.VerifyPayload($bad,$cfg.Marker())|Out-Null}catch{$blocked=$true}
 if(-not $blocked){throw 'payload_tampering_accepted'}
}
foreach($bad in @($descriptor.Replace('vm_id='+$vm,'vm_id=22222222-2222-2222-2222-222222222222'),$descriptor.Replace('Realized','Planned'),($descriptor+"extra=1`n"),$descriptor.Replace('settings_payload_bytes='+$payload.Length,'settings_payload_bytes=1048577'))){
 $hash=[HaProviderProbeV1.Settings]::Hash([Text.Encoding]::UTF8.GetBytes($bad))
 $blocked=$false;try{[HaProviderProbeV1.TargetDescriptor]::new($bad,$hash,('a'*64),$vm)|Out-Null}catch{$blocked=$true}
 if(-not $blocked){throw 'wrong_descriptor_identity_accepted'}
}
$blocked=$false;try{[HaProviderProbeV1.TargetDescriptor]::new(($descriptor+'x'),$descriptorHash,('a'*64),$vm)|Out-Null}catch{$blocked=$true}
if(-not $blocked){throw 'external_descriptor_pin_ignored'}
$id='abcdef12-abcd-abcd-abcd-abcdefabcdef'
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
        flow=source[source.index('stage="input_validation"'):source.index('if(stopped){complete=false;authorized=false;denied=false;}')]
        self.assertNotIn('ManagementObjectSearcher',flow)
        self.assertNotIn('GetRelated(',flow)
        self.assertIn('schema.GetMethodParameters(method)',flow)
        self.assertNotIn('target.GetMethodParameters',flow)
        self.assertIn('stage="valid_operation";attempted=true;',flow)
        self.assertIn('try{answer=target.InvokeMethod(method,input,invoke);}',flow)
        self.assertIn('}catch{complete=false;}',flow)
        self.assertIn('if(operation=="settings"&&authorized&&cfg.Values["expected_hyperv_role"]=="true")',flow)
        self.assertIn('Settings.NativeRelativeTarget(path.IsInstance,path.ClassName,className,path.Server,path.NamespacePath,path.Path,path.RelativePath,text)',source)
        parent=(ROOT/'infra/k8s/ha_control/windows-provider-acceptance.ps1').read_text()
        operations=parent[parent.index('$positive=$fixtures[1]'):parent.index('$complete=$true')]
        expected=["'positive_power_on'","'positive_settings'","'negative_settings'","'negative_power_off'","'positive_power_off'"]
        offsets=[operations.index(item) for item in expected]
        self.assertEqual(offsets,sorted(offsets))
        self.assertEqual(parent.count('GetText([Management.TextFormat]::WmiDtd20)'),1)
        self.assertIn('Write-ProbePayload $dir $sids[$i]',parent)
        self.assertIn('FileMode]::CreateNew',parent)
        self.assertIn('SetAccessRuleProtection($true,$false)',parent)


if __name__=='__main__':
    unittest.main()
