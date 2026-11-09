param([string]$Manifest,[string]$ManifestSha256,[switch]$Go)
# One new owned Internal-switch host interface only. No NAT, forwarding, DNS,
# default route, physical adapter, existing VM, service, or host reboot change.
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
function Get-GuestNetworkHash([byte[]]$Bytes){$h=[Security.Cryptography.SHA256]::Create();try{return [BitConverter]::ToString($h.ComputeHash($Bytes)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}}
function Get-GuestNetworkRange([string]$Address,[int]$Prefix){
    if($Address -cnotmatch '^[0-9.]{7,15}$' -or $Prefix -lt 1 -or $Prefix -gt 32){throw 'guest_network_address_rejected'}
    $ip=[Net.IPAddress]::Parse($Address);$b=$ip.GetAddressBytes()
    if($b.Length -ne 4 -or $ip.ToString() -cne $Address){throw 'guest_network_address_rejected'}
    $n=[uint64]$b[0]*16777216+[uint64]$b[1]*65536+[uint64]$b[2]*256+[uint64]$b[3]
    $size=[uint64][Math]::Pow(2,32-$Prefix);$first=[uint64]([Math]::Floor($n/$size)*$size)
    return [pscustomobject]@{address=$n;first=$first;last=($first+$size-1)}
}
function Assert-GuestNetworkSpec($Spec){
    $keys=@('schema_version','source_commit','vm_driver_source_commit','vm_driver_sha256','installation_id','prepared_receipt_sha256','host_ipv4','prefix_length','guest_a_ipv4','guest_b_ipv4')
    if((($Spec.PSObject.Properties.Name|Sort-Object -CaseSensitive)-join ',') -cne (($keys|Sort-Object -CaseSensitive)-join ',')){throw 'guest_network_shape_rejected'}
    if(($Spec.schema_version -isnot [int] -and $Spec.schema_version -isnot [long]) -or $Spec.schema_version -ne 1 -or
        ($Spec.prefix_length -isnot [int] -and $Spec.prefix_length -isnot [long]) -or $Spec.prefix_length -lt 24 -or $Spec.prefix_length -gt 30){throw 'guest_network_shape_rejected'}
    foreach($key in @('source_commit','vm_driver_source_commit')){if($Spec.$key -isnot [string] -or $Spec.$key -cnotmatch '^[0-9a-f]{40}$'){throw 'guest_network_source_rejected'}}
    foreach($key in @('vm_driver_sha256','installation_id','prepared_receipt_sha256')){if($Spec.$key -isnot [string] -or $Spec.$key -cnotmatch '^[0-9a-f]{64}$'){throw 'guest_network_pin_rejected'}}
    $ranges=@(foreach($key in @('host_ipv4','guest_a_ipv4','guest_b_ipv4')){
        if($Spec.$key -isnot [string]){throw 'guest_network_address_rejected'}
        $v=Get-GuestNetworkRange $Spec.$key $Spec.prefix_length
        if(-not (($v.address -ge 167772160 -and $v.address -le 184549375) -or ($v.address -ge 2886729728 -and $v.address -le 2887778303) -or ($v.address -ge 3232235520 -and $v.address -le 3232301055)) -or $v.address -eq $v.first -or $v.address -eq $v.last){throw 'guest_network_private_host_required'}
        $v
    })
    if(@($ranges|ForEach-Object{$_.address}|Select-Object -Unique).Count -ne 3 -or @($ranges|ForEach-Object{$_.first}|Select-Object -Unique).Count -ne 1){throw 'guest_network_subnet_rejected'}
}
function Get-GuestNetworkAdapter($Switch,$ManagementNics,$Adapters){
    $owned=@($ManagementNics|Where-Object{$_.SwitchId.ToString() -ceq $Switch.Id.ToString() -and $_.SwitchName -ceq $Switch.Name})
    if($owned.Count -ne 1 -or $owned[0].MacAddress -cnotmatch '^[0-9A-Fa-f]{12}$'){throw 'guest_network_management_binding_rejected'}
    $mac=$owned[0].MacAddress.ToUpperInvariant()
    $matched=@($Adapters|Where-Object{([string]$_.MacAddress).Replace('-','').ToUpperInvariant() -ceq $mac})
    if($matched.Count -ne 1 -or $matched[0].Virtual -isnot [bool] -or -not $matched[0].Virtual -or $matched[0].HardwareInterface -isnot [bool] -or $matched[0].HardwareInterface -or $matched[0].InterfaceIndex -le 0){throw 'guest_network_virtual_adapter_required'}
    return $matched[0]
}
function Invoke-GuestNetwork{
    if(-not $Go -or [Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or -not [Environment]::Is64BitProcess){throw 'guest_network_native_intent_required'}
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent();try{if(-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))){throw 'guest_network_admin_required'}}finally{$identity.Dispose()}
    if($ManifestSha256 -cnotmatch '^[0-9a-f]{64}$' -or $Manifest.Length -gt 32768){throw 'guest_network_manifest_required'}
    $bytes=[Convert]::FromBase64String($Manifest)
    if((Get-GuestNetworkHash $bytes) -cne $ManifestSha256){throw 'guest_network_manifest_changed'}
    $text=([Text.UTF8Encoding]::new($false,$true)).GetString($bytes)
    $keys=@([regex]::Matches($text,'"([A-Za-z_][A-Za-z0-9_]*)"\s*:')|ForEach-Object{$_.Groups[1].Value})
    if($keys.Count -ne 10 -or @($keys|Select-Object -Unique).Count -ne 10){throw 'guest_network_duplicate_or_escaped_keys'}
    $networkSpec=$text|ConvertFrom-Json
    Assert-GuestNetworkSpec $networkSpec
    $networkManifestHash=$ManifestSha256
    $networkRoot=Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)) ('N100HA_'+$networkSpec.installation_id.Substring(0,24))
    $driverPath=Join-Path $networkRoot 'windows-guest-vm.ps1'
    # Execute only the externally pinned buffer, never reopen mutable code.
    if((Get-Item -LiteralPath $driverPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'guest_network_driver_reparse'}
    $driverBytes=[IO.File]::ReadAllBytes($driverPath)
    if($driverBytes.Length -gt 262144 -or (Get-GuestNetworkHash $driverBytes) -cne $networkSpec.vm_driver_sha256){throw 'guest_network_driver_changed'}
    . ([ScriptBlock]::Create(([Text.UTF8Encoding]::new($false,$true)).GetString($driverBytes)))
    Initialize-GuestVmNative
    Assert-GuestVmPrivate $networkRoot
    Assert-GuestVmPrivate $driverPath
    $prepared=Read-GuestVmPreparedReceipt (Join-Path $networkRoot 'vm-A-prepared-receipt.json') $networkSpec.prepared_receipt_sha256
    $pins=$prepared.pins
    if($pins.installation_id -cne $networkSpec.installation_id -or $pins.source_commit -cne $networkSpec.vm_driver_source_commit -or $pins.node -cne 'A'){throw 'guest_network_prepared_binding_changed'}
    $vmSpec=[pscustomobject]@{schema_version=1;source_commit=$pins.source_commit;stage_source_commit=$pins.stage_source_commit;seed_source_commit=$pins.seed_source_commit;installation_id=$pins.installation_id;stage_manifest_sha256=$pins.stage_manifest_sha256;seed_iso_sha256=$pins.seed_sha256;seed_iso_bytes=(Get-Item -LiteralPath $pins.seed_path).Length;node='A';mac_address=$pins.mac_address;optional_egress_nic=$pins.optional_egress_nic;egress_mac_address=$pins.egress_mac_address;peer_prepared_receipt_sha256=''}
    Assert-GuestVmSpec $vmSpec|Out-Null
    $ManifestSha256=$pins.manifest_sha256
    $names=Get-GuestVmNames $vmSpec
    $null=Assert-GuestVmPrepared $vmSpec $names $pins (Join-Path $networkRoot 'guest-A') $pins.disk_path $pins.seed_path
    $switch=Get-VMSwitch -Id ([Guid]$pins.switch_id)
    $adapter=Get-GuestNetworkAdapter $switch @(Get-VMNetworkAdapter -ManagementOS -SwitchName $names.switch) @(Get-NetAdapter -IncludeHidden)
    $index=[int]$adapter.InterfaceIndex
    $interface=Get-NetIPInterface -InterfaceIndex $index -AddressFamily IPv4
    if(@($interface).Count -ne 1 -or [string]$interface.Forwarding -cne 'Disabled'){throw 'guest_network_forwarding_must_be_disabled'}
    $addresses=@(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop|Where-Object{$_.InterfaceIndex -eq $index})
    if(@($addresses|Where-Object{$_.IPAddress -notlike '169.254.*'}).Count){throw 'guest_network_interface_already_addressed'}
    $candidate=Get-GuestNetworkRange $networkSpec.host_ipv4 $networkSpec.prefix_length
    foreach($route in @(Get-NetRoute -AddressFamily IPv4|Where-Object{$_.DestinationPrefix -ne '0.0.0.0/0'})){
        $parts=$route.DestinationPrefix.Split('/');$range=Get-GuestNetworkRange $parts[0] ([int]$parts[1])
        if($candidate.first -le $range.last -and $range.first -le $candidate.last){throw 'guest_network_existing_route_overlap'}
    }
    $request=Join-Path $networkRoot 'network-request.json'
    Write-GuestVmRecord $request ([ordered]@{schema_version=1;scope='new_owned_internal_interface_only';source_commit=$networkSpec.source_commit;installation_id=$networkSpec.installation_id;manifest_sha256=$networkManifestHash;prepared_receipt_sha256=$networkSpec.prepared_receipt_sha256;switch_id=$pins.switch_id;interface_index=$index;phase='before_new_private_address'}) $true
    $null=New-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 -IPAddress $networkSpec.host_ipv4 -PrefixLength $networkSpec.prefix_length
    $watch=[Diagnostics.Stopwatch]::StartNew()
    do{
        $actual=@(Get-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4|Where-Object{$_.IPAddress -ceq $networkSpec.host_ipv4 -and $_.PrefixLength -eq $networkSpec.prefix_length -and [string]$_.AddressState -ceq 'Preferred'})
        if($actual.Count -eq 1){break};Start-Sleep -Milliseconds 100
    }while($watch.Elapsed.TotalSeconds -lt 5)
    $after=Get-GuestNetworkAdapter (Get-VMSwitch -Id ([Guid]$pins.switch_id)) @(Get-VMNetworkAdapter -ManagementOS -SwitchName $names.switch) @(Get-NetAdapter -IncludeHidden)
    if($actual.Count -ne 1 -or $after.InterfaceIndex -ne $index -or $after.InterfaceGuid -ne $adapter.InterfaceGuid -or [string](Get-NetIPInterface -InterfaceIndex $index -AddressFamily IPv4).Forwarding -cne 'Disabled'){throw 'guest_network_postcheck_failed'}
    Write-GuestVmRecord (Join-Path $networkRoot 'network-receipt.json') ([ordered]@{schema_version=1;source_commit=$networkSpec.source_commit;installation_id=$networkSpec.installation_id;manifest_sha256=$networkManifestHash;switch_id=$pins.switch_id;interface_index=$index;configured=$true;nat_created=$false;default_route_created=$false;forwarding_enabled=$false;production_transition=$false}) $true
    [ordered]@{scope='new_owned_internal_interface_only';source_commit=$networkSpec.source_commit;installation_id=$networkSpec.installation_id;manifest_sha256=$networkManifestHash;configured=$true;nat_created=$false;default_route_created=$false;forwarding_enabled=$false;production_transition=$false}|ConvertTo-Json -Compress
}
if($MyInvocation.InvocationName -ne '.'){Invoke-GuestNetwork}
