"""New internal-interface ownership and subnet safety; no native networking."""
import os
import base64
import hashlib
import json
from tests import test_ha_guest_vm as V
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


    def test_installer_first_network_uses_exact_original_schema4_manifest(self):
        bundle=json.loads(V.GuestVmTests.derivation_fixture())
        spec=bundle['spec']
        profile=dict(name='windows-managed-config-v1',definition_sha256='61fb12a5c53fa5a76c067f50e6a1ea56a915ebd3d9af86fcb67ff36cd48bd0ef',capability_name='vmWorkerProcess',capability_sid='S-1-15-3-1024-2268835264-3721307629-241982045-173645152-1490879176-104643441-2915960892-1612460704',host_hypervisor_tcb_acknowledged=True,operational_cutover_accepted=False,io_acceptance_sha256='',external_capability_review_sha256='',acceptance_scope='isolated-predeployment',isolation_receipt_sha256='')
        driver=ROOT/'infra/k8s/ha_control/windows-guest-vm.ps1'
        spec.update(schema_version=4,config_profile=profile,peer_config_profile_receipt_sha256='',peer_source_commit='',root_profile='strict-canonical-observed-v1',executor_source_commit='9'*40,executor_script_sha256=hashlib.sha256(driver.read_bytes()).hexdigest(),reservation_receipt_sha256='8'*64,reservation_root_identity='12345678:1234567812345678')
        raw=json.dumps(spec).encode()
        network=dict(schema_version=2,source_commit='9'*40,vm_driver_source_commit='9'*40,vm_driver_sha256=spec['executor_script_sha256'],installation_id=spec['installation_id'],prepared_receipt_sha256='5'*64,host_ipv4='10.8.7.1',prefix_length=29,guest_a_ipv4='10.8.7.2',guest_b_ipv4='10.8.7.3',root_profile=spec['root_profile'],executor_source_commit=spec['executor_source_commit'],executor_script_sha256='7'*64,reservation_receipt_sha256=spec['reservation_receipt_sha256'],reservation_root_identity=spec['reservation_root_identity'],vm_manifest_base64=base64.b64encode(raw).decode(),vm_journal_sha256='6'*64)
        self.run_ps(r'''
$ErrorActionPreference='Stop';. $env:NETWORK_SOURCE
$v=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__NETWORK__'))|ConvertFrom-Json
Assert-GuestNetworkSpec $v
$base=Split-Path $env:NETWORK_SOURCE;$support=Join-Path $base 'guest-owned-profile.ps1'
$driver=[IO.File]::ReadAllBytes((Join-Path $base 'windows-guest-vm.ps1'))
. ([scriptblock]::Create([Text.UTF8Encoding]::new($false,$true).GetString($driver)))
$pins=[pscustomobject]@{manifest_sha256='__MANIFEST_SHA__'}
$original=Read-GuestNetworkOwnedVmSpec $v $pins $support
if($original.source_commit -ceq $original.executor_source_commit -or $original.schema_version -ne 4){throw 'media_origin_relabelled'}
foreach($key in @('reservation_receipt_sha256','reservation_root_identity','vm_driver_sha256')){
 $old=$v.$key;$v.$key=if($key -eq 'reservation_root_identity'){'99999999:9999999999999999'}else{'0'*64};$closed=$false
 try{Read-GuestNetworkOwnedVmSpec $v $pins $support|Out-Null}catch{$closed=$true};if(-not $closed){throw 'component_binding_lost'};$v.$key=$old
}
$v.schema_version=1;$closed=$false;try{Assert-GuestNetworkSpec $v}catch{$closed=$true};if(-not $closed){throw 'new_profile_silently_inferred'}
'''.replace('__NETWORK__',base64.b64encode(json.dumps(network).encode()).decode()).replace('__MANIFEST_SHA__',hashlib.sha256(raw).hexdigest()))


if __name__ == '__main__':
    unittest.main()
