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
$frame=[pscustomobject]@{scope='owned_vm_provider_role_probe';schema_version=1;operation='settings';stage='target_query';attempted=$false;complete=$false;authorized=$false;authorization_denied=$false;administrator=$false;hyperv_role=$false}
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
$text="format=ha-provider-probe-v1`nservice_name=HaProbe_0123456789abcdef`nservice_sid=S-1-5-80-1-2-3-4-5`ninstallation_id=$('a'*64)`nvm_id=11111111-1111-1111-1111-111111111111`noperation=settings`nexpected_hyperv_role=false`n"
$cfg=[HaProviderProbeV1.Settings]::new($text)
if($cfg.Marker() -cne ('ha-owned-provider-probe:'+('a'*64))){throw 'not_fixed_owned_marker'}
$id='abcdef12-abcd-abcd-abcd-abcdefabcdef'
if(-not [HaProviderProbeV1.Settings]::CurrentSetting('Microsoft:Hyper-V:System:Realized',$id.ToUpperInvariant(),$id)){throw 'v2_current_setting_rejected'}
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


if __name__=='__main__':
    unittest.main()
