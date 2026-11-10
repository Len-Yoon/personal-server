"""New internal-interface ownership and subnet safety; no native networking."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class GuestNetworkTests(unittest.TestCase):
    def run_ps(self, script):
        pwsh = shutil.which('pwsh') or '/private/tmp/n100-ha-powershell/runtime/pwsh'
        env = dict(os.environ, NETWORK_SOURCE=str(ROOT/'infra/k8s/ha_control/windows-guest-network.ps1'))
        result = subprocess.run([pwsh, '-NoProfile', '-NonInteractive', '-Command', script],
                                env=env, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    def test_private_subnet_requires_three_distinct_usable_hosts_and_strict_types(self):
        self.run_ps(r'''
$ErrorActionPreference='Stop'
. $env:NETWORK_SOURCE
$value=[pscustomobject]@{schema_version=1;source_commit=('a'*40);vm_driver_source_commit=('b'*40);vm_driver_sha256=('c'*64);installation_id=('d'*64);prepared_receipt_sha256=('e'*64);host_ipv4='10.8.7.1';prefix_length=29;guest_a_ipv4='10.8.7.2';guest_b_ipv4='10.8.7.3'}
Assert-GuestNetworkSpec $value
foreach($case in @(@('host_ipv4','8.8.8.8'),@('host_ipv4','10.8.7.0'),@('guest_b_ipv4','10.8.7.7'),@('guest_b_ipv4','10.8.7.2'),@('guest_b_ipv4','10.8.8.3'),@('host_ipv4','010.8.7.1'),@('prefix_length',$true),@('schema_version',$true),@('installation_id',@('d'*64)))){
 $copy=$value.PSObject.Copy();$copy.($case[0])=$case[1];$blocked=$false
 try{Assert-GuestNetworkSpec $copy}catch{$blocked=$true}
 if(-not $blocked){throw 'unsafe_network_spec_accepted'}
}
$range=Get-GuestNetworkRange '10.8.7.3' 29
if($range.last-$range.first -ne 7 -or $range.address-$range.first -ne 3){throw 'ipv4_range_wrong'}
''')

    def test_management_port_cannot_select_physical_duplicate_or_different_switch_adapter(self):
        self.run_ps(r'''
$ErrorActionPreference='Stop'
. $env:NETWORK_SOURCE
$id=[Guid]'11111111-1111-1111-1111-111111111111'
$switch=[pscustomobject]@{Id=$id;Name='owned-data'}
$nic=[pscustomobject]@{SwitchId=$id;SwitchName='owned-data';MacAddress='021122334455'}
$adapter=[pscustomobject]@{MacAddress='02-11-22-33-44-55';Virtual=$true;HardwareInterface=$false;InterfaceIndex=8}
if((Get-GuestNetworkAdapter $switch @($nic) @($adapter)).InterfaceIndex -ne 8){throw 'owned_interface_not_selected'}
foreach($case in @(@('Virtual',$false),@('HardwareInterface',$true),@('Virtual','true'),@('InterfaceIndex',0),@('MacAddress','02-11-22-33-44-56'))){
 $copy=$adapter.PSObject.Copy();$copy.($case[0])=$case[1];$blocked=$false
 try{Get-GuestNetworkAdapter $switch @($nic) @($copy)|Out-Null}catch{$blocked=$true}
 if(-not $blocked){throw 'foreign_or_physical_adapter_selected'}
}
$blocked=$false;try{Get-GuestNetworkAdapter $switch @($nic) @($adapter,$adapter)|Out-Null}catch{$blocked=$true}
if(-not $blocked){throw 'duplicate_mac_selected'}
$copy=$nic.PSObject.Copy();$copy.SwitchId=[Guid]::Empty
$blocked=$false;try{Get-GuestNetworkAdapter $switch @($copy) @($adapter)|Out-Null}catch{$blocked=$true}
if(-not $blocked){throw 'other_switch_selected'}
''')

    def test_unarmed_and_parser_scope_has_no_nat_forwarding_or_service_operations(self):
        self.run_ps(r'''
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($env:NETWORK_SOURCE,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'parse_failed'}
. $env:NETWORK_SOURCE
$blocked=$false;try{Invoke-GuestNetwork}catch{$blocked=$true}
if(-not $blocked){throw 'unarmed_network_write_accepted'}
$commands=@($ast.FindAll({param($a)$a -is [Management.Automation.Language.CommandAst]},$true)|ForEach-Object{$_.GetCommandName()})
foreach($name in @('New-NetNat','Remove-NetIPAddress','Set-NetIPInterface','Set-DnsClientServerAddress','New-NetRoute','Restart-Computer','Start-VM','Stop-VM','Start-Service','Stop-Service','Enable-NetAdapter','Disable-NetAdapter')){
 if($name -in $commands){throw 'scope_expanded'}
}
''')


if __name__ == '__main__':
    unittest.main()
