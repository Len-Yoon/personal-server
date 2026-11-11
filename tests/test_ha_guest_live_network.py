"""Provider fixtures for the live Windows management-address boundary."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "infra/k8s/ha_control/windows-guest-live-network.ps1"


class GuestLiveNetworkTests(unittest.TestCase):
    def run_ps(self, script):
        pwsh = shutil.which("pwsh") or "/private/tmp/n100-ha-powershell/runtime/pwsh"
        env = dict(os.environ, LIVE_NETWORK_SOURCE=str(SOURCE))
        result = subprocess.run(
            [pwsh, "-NoProfile", "-NonInteractive", "-Command", script],
            env=env, capture_output=True, timeout=25,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_one_active_address_only_and_postcondition_failures(self):
        self.run_ps(r'''
$ErrorActionPreference='Stop'
. $env:LIVE_NETWORK_SOURCE
. (Join-Path (Split-Path $env:LIVE_NETWORK_SOURCE) 'windows-guest-network.ps1')
$spec=[pscustomobject]@{source_commit=('a'*40);host_ipv4='10.8.7.1';prefix_length=29;guest_a_ipv4='10.8.7.2';guest_b_ipv4='10.8.7.3';allow_owned_dhcp_disable=$true}
$lease=[pscustomobject]@{}
$lease|Add-Member ScriptMethod AssertCurrent {}
$script:scenario='good';$script:added=0;$script:pairReads=0
$script:records=[Collections.Generic.List[string]]::new()
function Get-LiveNetworkPair($Spec,$VmSpec,$Root){
    $script:pairReads++
    $id='11111111-1111-1111-1111-111111111111'
    if($script:scenario -eq 'switch_drift' -and $script:pairReads -eq 2){$id='22222222-2222-2222-2222-222222222222'}
    [pscustomobject]@{switch=[pscustomobject]@{Id=[Guid]$id};guest_a='A';guest_b='B'}
}
function Get-LiveNetworkAdapter($Switch,$Spec,$ExpectDhcpDisabled){
    $enabled=($script:scenario -like 'dhcp_*' -and -not $script:added) -or $script:scenario -eq 'dhcp_remains'
    if($ExpectDhcpDisabled -and $enabled){throw 'postcondition_dhcp_still_enabled'}
    [pscustomobject]@{InterfaceIndex=8;InterfaceGuid=[Guid]'33333333-3333-3333-3333-333333333333';DhcpEnabled=$enabled}
}
function Assert-GuestOwnedRootAcl($Root,$VmSpec){}
function Get-LiveNetworkRoutes{
    [pscustomobject]@{store='ActiveStore';prefix='0.0.0.0/0';index=3;next_hop='192.168.1.1';metric=1}
    if($script:scenario -eq 'overlap'){[pscustomobject]@{store='PersistentStore';prefix='10.8.7.2/32';index=3;next_hop='0.0.0.0';metric=1}}
    if($script:scenario -like 'dhcp_*' -and -not $script:added){
        [pscustomobject]@{store='ActiveStore';prefix='169.254.0.0/16';index=8;next_hop='0.0.0.0';metric=1}
        [pscustomobject]@{store='ActiveStore';prefix='224.0.0.0/4';index=8;next_hop='0.0.0.0';metric=1}
    }
    if($script:scenario -eq 'dhcp_route'){[pscustomobject]@{store='PersistentStore';prefix='172.20.0.0/16';index=8;next_hop='0.0.0.0';metric=1}}
    if($script:scenario -eq 'changed_default' -and $script:added){[pscustomobject]@{store='ActiveStore';prefix='0.0.0.0/0';index=8;next_hop='10.8.7.2';metric=2}}
}
function Write-GuestVmRecord($Path,$Record,$New){
    if(-not $New){throw 'record_must_be_new'}
    $script:records.Add([IO.Path]::GetFileName($Path))
}
function Get-NetIPAddress($InterfaceIndex,$AddressFamily,$PolicyStore){
    if($script:scenario -eq 'dhcp_lease' -and $PolicyStore -eq 'ActiveStore'){
        [pscustomobject]@{InterfaceIndex=8;IPAddress='169.254.2.3';PrefixOrigin='Dhcp'}
    }
    if($script:added -and $PolicyStore -eq 'ActiveStore'){
        [pscustomobject]@{InterfaceIndex=8;IPAddress='10.8.7.1';PrefixLength=29;AddressState=$(if($script:scenario -eq 'duplicate'){'Duplicate'}else{'Preferred'})}
    }
}
function New-NetIPAddress($InterfaceIndex,$AddressFamily,$IPAddress,$PrefixLength,$PolicyStore){
    if($script:records.Count -ne 1 -or $InterfaceIndex -ne 8 -or $AddressFamily -ne 'IPv4' -or $IPAddress -ne '10.8.7.1' -or $PrefixLength -ne 29 -or $PolicyStore -ne 'ActiveStore'){throw 'unexpected_mutation'}
    $script:added++
}
function Find-NetRoute($RemoteIPAddress){
    [pscustomobject]@{IPAddress='10.8.7.1';InterfaceIndex=8}
    [pscustomobject]@{DestinationPrefix='10.8.7.0/29';InterfaceIndex=$(if($script:scenario -eq 'foreign_route'){3}else{8});NextHop='0.0.0.0'}
}
$root=[IO.Path]::GetTempPath()
$out=Invoke-LiveNetworkChange $spec $null $root $lease ('f'*64)|ConvertFrom-Json
if(-not $out.configured -or $out.policy_store -ne 'ActiveStore' -or $out.dhcp_automatically_disabled -or $script:added -ne 1 -or $script:records.Count -ne 2){throw 'expected_one_change'}
$script:scenario='dhcp_change';$script:added=0;$script:pairReads=0;$script:records.Clear()
$out=Invoke-LiveNetworkChange $spec $null $root $lease ('f'*64)|ConvertFrom-Json
if(-not $out.dhcp_automatically_disabled -or $script:added -ne 1 -or $script:records.Count -ne 2){throw 'expected_one_change_with_automatic_dhcp_disable'}
foreach($case in @('overlap','switch_drift','duplicate','changed_default','foreign_route','dhcp_lease','dhcp_route','dhcp_remains')){
    $script:scenario=$case;$script:added=0;$script:pairReads=0;$script:records.Clear()
    $blocked=$false
    try{Invoke-LiveNetworkChange $spec $null $root $lease ('f'*64)|Out-Null}catch{$blocked=$true}
    $expected=if($case -in @('overlap','switch_drift','dhcp_lease','dhcp_route')){0}else{1}
    if(-not $blocked -or $script:added -ne $expected -or $script:records.Count -gt 1){throw ('unclosed_'+$case)}
}
''')

    def test_ast_and_unarmed_execution(self):
        self.run_ps(r'''
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($env:LIVE_NETWORK_SOURCE,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'parse_failed'}
$commands=@($ast.FindAll({param($a)$a -is [Management.Automation.Language.CommandAst]},$true)|ForEach-Object{$_.GetCommandName()})
if(@($commands|Where-Object{$_ -eq 'New-NetIPAddress'}).Count -ne 1){throw 'single_address_call_required'}
foreach($name in @('New-NetNat','Remove-NetIPAddress','Set-NetIPInterface','Set-DnsClientServerAddress','New-NetRoute','Restart-Computer','Start-VM','Stop-VM','Start-Service','Stop-Service','Enable-NetAdapter','Disable-NetAdapter')){
    if($name -in $commands){throw 'scope_expanded'}
}
. $env:LIVE_NETWORK_SOURCE
$blocked=$false
try{Invoke-GuestLiveNetwork}catch{if($_.Exception.Message -eq 'live_network_native_intent_required'){$blocked=$true}}
if(-not $blocked){throw 'unarmed_network_write_accepted'}
''')

    def test_pair_pins_and_adapter_policy(self):
        self.run_ps(r'''
$ErrorActionPreference='Stop'
. $env:LIVE_NETWORK_SOURCE
. (Join-Path (Split-Path $env:LIVE_NETWORK_SOURCE) 'windows-guest-network.ps1')
$spec=[pscustomobject]@{vm_b_prepared_sha256=('b'*64);vm_b_profile_sha256=('c'*64);vm_b_manifest_sha256=('d'*64)}
$vmSpec=[pscustomobject]@{config_profile='isolated';vm_operation_source_commit=('a'*40)}
$script:case='good'
$script:switch=[pscustomobject]@{Id=[Guid]'11111111-1111-1111-1111-111111111111';Name='fixture'}
$a=[Guid]'aaaaaaaa-1111-1111-1111-111111111111'
$b=[Guid]'bbbbbbbb-2222-2222-2222-222222222222'
function Get-GuestVmPeer($Root,$VmSpec){
    [pscustomobject]@{pins=[pscustomobject]@{switch_id=$script:switch.Id.ToString();manifest_sha256=('a'*64);vm_id=$a.ToString();mac_address='021122334451'};switch=$script:switch
        vm=[pscustomobject]@{Id=$a;State=$(if($script:case -eq 'a_off'){'Off'}else{'Running'})}}
}
function Read-GuestVmPreparedReceipt($Path,$Hash){
    if($Hash -cne ('b'*64)){throw 'wrong_prepared_pin'}
    [pscustomobject]@{pins=[pscustomobject]@{switch_id=$script:switch.Id.ToString()
        switch_owner_manifest_sha256=$(if($script:case -eq 'owner_changed'){'0'*64}else{'a'*64});vm_id=$b.ToString();mac_address='021122334452'}}
}
function Get-GuestVmNames($VmSpec){'names'}
function Assert-GuestVmPrepared($VmSpec,$Names,$Pins,$Guest,$Disk,$Iso,$Hash,$ReadOnly){
    if($Hash -cne ('d'*64) -or $ReadOnly -ne $true){throw 'wrong_live_vm_assertion'}
    [pscustomobject]@{Id=$b;State=$(if($script:case -eq 'b_off'){'Off'}else{'Running'})}
}
function Read-GuestVmConfigProfileReceipt($Root,$Node,$Hash,$Pins,$Profile,$Prepared){
    if($Node -cne 'B' -or $Hash -cne ('c'*64) -or $Profile -cne 'isolated' -or $Prepared -cne ('b'*64)){throw 'wrong_profile_pin'}
    [pscustomobject]@{finalizer_source_commit=$(if($script:case -eq 'profile_origin'){'9'*40}else{'a'*40})}
}
function Get-VMNetworkAdapter([switch]$ManagementOS,$SwitchName,$VMName,$ErrorAction){
    if($ManagementOS){return [pscustomobject]@{SwitchId=$script:switch.Id;SwitchName='fixture';MacAddress='021122334455'}}
    if($VMName -cne '*' -or $ErrorAction -cne 'Stop'){throw 'full_vm_inventory_required'}
    $nicA=[pscustomobject]@{SwitchId=$script:switch.Id;SwitchName='fixture';VMId=$a;Name='ha-data';MacAddress='021122334451'}
    $nicB=[pscustomobject]@{SwitchId=$script:switch.Id;SwitchName='fixture';VMId=$b;Name='ha-data';MacAddress='021122334452'}
    $foreign=[pscustomobject]@{SwitchId=[Guid]'cccccccc-3333-3333-3333-333333333333';SwitchName='other';VMId=[Guid]'dddddddd-4444-4444-4444-444444444444';Name='other';MacAddress='021122334453'}
    if($script:case -eq 'foreign_nic'){$foreign.SwitchId=$script:switch.Id;$foreign.SwitchName='fixture'}
    if($script:case -eq 'ambiguous_nic'){$foreign.SwitchName='fixture'}
    if($script:case -eq 'missing_vm_id'){$nicB.PSObject.Properties.Remove('VMId')}
    if($script:case -eq 'duplicate_nic'){return @($nicA,$nicB,$nicA,$foreign)}
    if($script:case -eq 'missing_nic'){return @($nicA,$foreign)}
    return @($nicA,$nicB,$foreign)
}
function Get-NetAdapter([switch]$IncludeHidden){
    [pscustomobject]@{MacAddress='02-11-22-33-44-55';Virtual=$true;HardwareInterface=($script:case -eq 'physical')
        InterfaceIndex=8;InterfaceGuid=[Guid]'33333333-3333-3333-3333-333333333333';Status='Up'}
}
function Get-NetIPInterface($InterfaceIndex,$AddressFamily){
    [pscustomobject]@{Dhcp=$(if($script:case -eq 'dhcp'){'Enabled'}else{'Disabled'})
        Forwarding=$(if($script:case -eq 'forwarding'){'Enabled'}else{'Disabled'});WeakHostSend='Disabled';WeakHostReceive='Disabled'}
}
$root=[IO.Path]::GetTempPath()
$pair=Get-LiveNetworkPair $spec $vmSpec $root
if($pair.guest_a -cne $a.ToString() -or $pair.guest_b -cne $b.ToString() -or (Get-LiveNetworkAdapter $pair.switch $spec).InterfaceIndex -ne 8){throw 'pair_or_adapter_not_bound'}
foreach($case in @('a_off','b_off','owner_changed','profile_origin','foreign_nic','ambiguous_nic','missing_vm_id','duplicate_nic','missing_nic','physical','dhcp','forwarding')){
    $script:case=$case;$blocked=$false
    try{$pair=Get-LiveNetworkPair $spec $vmSpec $root;Get-LiveNetworkAdapter $pair.switch $spec|Out-Null}catch{$blocked=$true}
    if(-not $blocked){throw ('unclosed_'+$case)}
}
$script:case='dhcp'
foreach($flag in @($false,'true')){
    $declared=[pscustomobject]@{allow_owned_dhcp_disable=$flag}
    $blocked=$false
    try{Get-LiveNetworkAdapter $pair.switch $declared|Out-Null}catch{$blocked=$true}
    if(-not $blocked){throw 'invalid_dhcp_intent_accepted'}
}
$declared=[pscustomobject]@{allow_owned_dhcp_disable=$true}
if(-not (Get-LiveNetworkAdapter $pair.switch $declared).DhcpEnabled){throw 'explicit_dhcp_intent_rejected'}
$blocked=$false
try{Get-LiveNetworkAdapter $pair.switch $declared $true|Out-Null}catch{$blocked=$true}
if(-not $blocked){throw 'post_change_dhcp_not_required'}
''')

    def test_vm_executor_source_is_bound_separately_from_reservation(self):
        self.run_ps(r'''
. $env:LIVE_NETWORK_SOURCE
$networkSpec=[pscustomobject]@{installation_id=('a'*64);vm_driver_source_commit=('b'*40);vm_driver_sha256=('c'*64)}
$vm=[pscustomobject]@{schema_version=4;node='B';installation_id=('a'*64)
    executor_source_commit='edf29d2fa1acf117815f6dbd3e074d650a02e94b'
    vm_operation_source_commit=('b'*40);executor_script_sha256=('c'*64)
    config_profile=[pscustomobject]@{acceptance_scope='isolated-predeployment';operational_cutover_accepted=$false}}
Assert-LiveNetworkVmBinding $networkSpec $vm
foreach($key in @('executor_source_commit','vm_operation_source_commit','executor_script_sha256')){
    $candidate=$vm.PSObject.Copy();$candidate.$key='d'*$vm.$key.Length
    $blocked=$false
    try{Assert-LiveNetworkVmBinding $networkSpec $candidate}catch{if($_.Exception.Message -eq 'live_network_original_b_profile_required'){$blocked=$true}}
    if(-not $blocked){throw ('unclosed_'+$key)}
}
''')


if __name__ == "__main__":
    unittest.main()
