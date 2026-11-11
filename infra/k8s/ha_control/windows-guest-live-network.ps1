param([string]$Manifest,[string]$ManifestSha256,[switch]$Go)

$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest

function Get-LiveNetworkHash([byte[]]$Bytes){
    $sha=[Security.Cryptography.SHA256]::Create()
    try{return [BitConverter]::ToString($sha.ComputeHash($Bytes)).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
}

function Read-LiveNetworkCode([string]$Path,[string]$Hash){
    $item=Get-Item -LiteralPath $Path -Force
    if($item -isnot [IO.FileInfo] -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.Length -gt 262144){throw 'live_network_code_rejected'}
    $bytes=[IO.File]::ReadAllBytes($Path)
    if((Get-LiveNetworkHash $bytes) -cne $Hash){throw 'live_network_code_changed'}
    return [ScriptBlock]::Create(([Text.UTF8Encoding]::new($false,$true)).GetString($bytes))
}

function Assert-LiveNetworkSpec($Spec){
    $keys=@('schema_version','source_commit','executor_script_sha256','installation_id','vm_driver_source_commit','vm_driver_sha256',
        'vm_b_manifest_base64','vm_b_manifest_sha256','vm_b_prepared_sha256','vm_b_profile_sha256',
        'host_ipv4','prefix_length','guest_a_ipv4','guest_b_ipv4')
    if($null -ne $Spec.PSObject.Properties['allow_owned_dhcp_disable']){
        $keys+=@('allow_owned_dhcp_disable')
        if($Spec.allow_owned_dhcp_disable -isnot [bool]){throw 'live_network_dhcp_intent_type_rejected'}
    }
    if((($Spec.PSObject.Properties.Name|Sort-Object -CaseSensitive)-join ',') -cne (($keys|Sort-Object -CaseSensitive)-join ',') -or
        ($Spec.schema_version -isnot [int] -and $Spec.schema_version -isnot [long]) -or $Spec.schema_version -ne 1){throw 'live_network_shape_rejected'}
    if($Spec.source_commit -isnot [string] -or $Spec.source_commit -cnotmatch '^[0-9a-f]{40}$' -or
        $Spec.vm_driver_source_commit -isnot [string] -or $Spec.vm_driver_source_commit -cnotmatch '^[0-9a-f]{40}$' -or
        $Spec.vm_driver_sha256 -isnot [string] -or $Spec.vm_driver_sha256 -cnotmatch '^[0-9a-f]{64}$'){throw 'live_network_pinned_driver_required'}
    foreach($key in @('executor_script_sha256','installation_id','vm_b_manifest_sha256','vm_b_prepared_sha256','vm_b_profile_sha256')){
        if($Spec.$key -isnot [string] -or $Spec.$key -cnotmatch '^[0-9a-f]{64}$'){throw 'live_network_pin_rejected'}
    }
    if($Spec.vm_b_manifest_base64 -isnot [string] -or $Spec.vm_b_manifest_base64.Length -gt 32768 -or
        $Spec.vm_b_manifest_base64 -cnotmatch '^[A-Za-z0-9+/]+={0,2}$'){throw 'live_network_original_manifest_required'}
    # Reuse the original subnet, canonical IPv4 and private-host validation.
    Assert-GuestNetworkSpec ([pscustomobject]@{
        schema_version=1;source_commit=$Spec.source_commit;vm_driver_source_commit=$Spec.vm_driver_source_commit
        vm_driver_sha256=$Spec.vm_driver_sha256;installation_id=$Spec.installation_id;prepared_receipt_sha256=$Spec.vm_b_prepared_sha256
        host_ipv4=$Spec.host_ipv4;prefix_length=$Spec.prefix_length;guest_a_ipv4=$Spec.guest_a_ipv4;guest_b_ipv4=$Spec.guest_b_ipv4
    })
}

function Assert-LiveNetworkSwitchMembers($Switch,$ExpectedMacs,$Adapters){
    $switchId=[Guid]$Switch.Id
    $seen=@{}
    foreach($row in $Adapters){
        if($null -eq $row -or $null -eq $row.PSObject.Properties['SwitchId'] -or $null -eq $row.PSObject.Properties['SwitchName']){
            throw 'live_network_switch_membership_unverified'
        }
        $idText=[string]$row.SwitchId
        $name=[string]$row.SwitchName
        $rowSwitchId=[Guid]::Empty
        if($idText -and -not [Guid]::TryParseExact($idText,'D',[ref]$rowSwitchId)){throw 'live_network_switch_membership_unverified'}
        if(($rowSwitchId -eq [Guid]::Empty) -ne [string]::IsNullOrEmpty($name)){throw 'live_network_switch_membership_unverified'}
        if($rowSwitchId -ne $switchId){
            if($name -ieq $Switch.Name){throw 'live_network_switch_membership_unverified'}
            continue
        }
        if($name -cne $Switch.Name -or $null -eq $row.PSObject.Properties['VMId'] -or
            $null -eq $row.PSObject.Properties['Name'] -or $null -eq $row.PSObject.Properties['MacAddress']){
            throw 'live_network_switch_membership_unverified'
        }
        $vmId=[Guid]::Empty
        if(-not [Guid]::TryParseExact([string]$row.VMId,'D',[ref]$vmId)){throw 'live_network_switch_membership_unverified'}
        $key=$vmId.ToString('D')
        if(-not $ExpectedMacs.ContainsKey($key) -or $seen.ContainsKey($key) -or
            $row.Name -cne 'ha-data' -or $row.MacAddress -cne $ExpectedMacs[$key]){throw 'live_network_foreign_or_duplicate_switch_member'}
        $seen[$key]=$true
    }
    if($seen.Count -ne 2 -or $ExpectedMacs.Count -ne 2){throw 'live_network_switch_membership_unverified'}
}

function Get-LiveNetworkPair($Spec,$VmSpec,[string]$Root){
    $peer=Get-GuestVmPeer $Root $VmSpec
    $prepared=Read-GuestVmPreparedReceipt (Join-Path $Root 'vm-B-prepared-receipt.json') $Spec.vm_b_prepared_sha256
    $pins=$prepared.pins
    if($pins.switch_id -cne $peer.pins.switch_id -or $pins.switch_owner_manifest_sha256 -cne $peer.pins.manifest_sha256 -or
        $pins.vm_id -ceq $peer.pins.vm_id){throw 'live_network_pair_binding_rejected'}
    $guest=Join-Path $Root 'guest-B'
    $vm=Assert-GuestVmPrepared $VmSpec (Get-GuestVmNames $VmSpec) $pins $guest (Join-Path $guest 'guest-B.vhdx') (Join-Path $Root 'seed-B.iso') $Spec.vm_b_manifest_sha256 $true
    $profile=Read-GuestVmConfigProfileReceipt $Root 'B' $Spec.vm_b_profile_sha256 $pins $VmSpec.config_profile $Spec.vm_b_prepared_sha256
    if($profile.finalizer_source_commit -cne $VmSpec.vm_operation_source_commit){throw 'live_network_b_profile_origin_changed'}
    if($peer.vm.State -ne 'Running' -or $vm.State -ne 'Running'){throw 'live_network_two_running_guests_required'}
    $expectedMacs=@{}
    $expectedMacs[([Guid]$peer.vm.Id).ToString('D')]=$peer.pins.mac_address
    $expectedMacs[([Guid]$vm.Id).ToString('D')]=$pins.mac_address
    # VMName wildcard enumerates VM ports. The separate management-OS lookup
    # in Get-LiveNetworkAdapter verifies the single owned host port.
    Assert-LiveNetworkSwitchMembers $peer.switch $expectedMacs @(Get-VMNetworkAdapter -VMName '*' -ErrorAction Stop)
    return [pscustomobject]@{switch=$peer.switch;guest_a=$peer.pins.vm_id;guest_b=$pins.vm_id}
}

function Get-LiveNetworkAdapter($Switch,$Spec,[bool]$ExpectDhcpDisabled=$false){
    $adapter=Get-GuestNetworkAdapter $Switch @(Get-VMNetworkAdapter -ManagementOS -SwitchName $Switch.Name) @(Get-NetAdapter -IncludeHidden)
    if([Guid]$adapter.InterfaceGuid -eq [Guid]::Empty -or $adapter.Status -ne 'Up'){throw 'live_network_adapter_unavailable'}
    $interface=@(Get-NetIPInterface -InterfaceIndex $adapter.InterfaceIndex -AddressFamily IPv4)
    if($interface.Count -ne 1 -or $interface[0].Dhcp -notin @('Enabled','Disabled') -or $interface[0].Forwarding -ne 'Disabled' -or
        $interface[0].WeakHostSend -ne 'Disabled' -or $interface[0].WeakHostReceive -ne 'Disabled'){throw 'live_network_interface_policy_rejected'}
    $dhcpEnabled=$interface[0].Dhcp -eq 'Enabled'
    if($dhcpEnabled -and ($ExpectDhcpDisabled -or $null -eq $Spec.PSObject.Properties['allow_owned_dhcp_disable'] -or
        $Spec.allow_owned_dhcp_disable -isnot [bool] -or -not $Spec.allow_owned_dhcp_disable)){throw 'live_network_dhcp_disable_not_authorized'}
    return [pscustomobject]@{InterfaceGuid=$adapter.InterfaceGuid;InterfaceIndex=$adapter.InterfaceIndex;DhcpEnabled=$dhcpEnabled}
}

function Get-LiveNetworkRoutes{
    foreach($store in @('ActiveStore','PersistentStore')){
        try{
            $routes=@(Get-NetRoute -AddressFamily IPv4 -PolicyStore $store -ErrorAction Stop)
        }catch{
            # NetTCPIP reports an empty filtered persistent store as ObjectNotFound.
            if($store -ceq 'PersistentStore' -and $_.CategoryInfo.Category -eq 'ObjectNotFound' -and
                $_.FullyQualifiedErrorId -ceq 'CmdletizationQuery_NotFound_AddressFamily,Get-NetRoute'){continue}
            throw
        }
        foreach($route in $routes){
            [pscustomobject]@{store=$store;prefix=$route.DestinationPrefix;index=$route.InterfaceIndex;next_hop=$route.NextHop;metric=$route.RouteMetric}
        }
    }
}

function Assert-LiveNetworkUnused($Spec,[int]$Index,$Routes,$Addresses,[bool]$DhcpEnabled){
    foreach($address in $Addresses){
        if($address.IPAddress -ceq $Spec.host_ipv4 -or
            ($address.InterfaceIndex -eq $Index -and $address.IPAddress -notlike '169.254.*')){throw 'live_network_existing_address'}
        if($DhcpEnabled -and $address.InterfaceIndex -eq $Index -and $address.PrefixOrigin -eq 'Dhcp'){
            throw 'live_network_dhcp_lease_present'
        }
    }
    $candidate=Get-GuestNetworkRange $Spec.host_ipv4 $Spec.prefix_length
    foreach($route in $Routes){
        if($route.prefix -ceq '0.0.0.0/0'){
            if($route.index -eq $Index){throw 'live_network_target_default_route'}
            continue
        }
        $parts=$route.prefix.Split('/')
        $range=Get-GuestNetworkRange $parts[0] ([int]$parts[1])
        if($candidate.first -le $range.last -and $range.first -le $candidate.last){throw 'live_network_route_overlap'}
        if($DhcpEnabled -and $route.index -eq $Index){
            $apipa=Get-GuestNetworkRange '169.254.0.0' 16
            $inherent=$route.prefix -cin @('224.0.0.0/4','255.255.255.255/32')
            if($route.next_hop -cne '0.0.0.0' -or (-not $inherent -and ($range.first -lt $apipa.first -or $range.last -gt $apipa.last))){
                throw 'live_network_dhcp_route_present'
            }
        }
    }
}

function Assert-LiveNetworkSelectedRoute([string]$Guest,$Spec,[int]$Index){
    $selection=@(Find-NetRoute -RemoteIPAddress $Guest)
    $address=@($selection|Where-Object{$null -ne $_.PSObject.Properties['IPAddress']})
    $route=@($selection|Where-Object{$null -ne $_.PSObject.Properties['DestinationPrefix']})
    if($address.Count -ne 1 -or $route.Count -ne 1 -or $address[0].IPAddress -cne $Spec.host_ipv4 -or
        $address[0].InterfaceIndex -ne $Index -or $route[0].InterfaceIndex -ne $Index -or $route[0].NextHop -cne '0.0.0.0'){
        throw 'live_network_guest_route_unverified'
    }
}

function Invoke-LiveNetworkChange($Spec,$VmSpec,[string]$Root,$Lease,[string]$RequestHash){
    $pair=Get-LiveNetworkPair $Spec $VmSpec $Root
    $adapter=Get-LiveNetworkAdapter $pair.switch $Spec
    $index=[int]$adapter.InterfaceIndex
    $routes=@(Get-LiveNetworkRoutes)
    $addresses=@(foreach($store in @('ActiveStore','PersistentStore')){Get-NetIPAddress -AddressFamily IPv4 -PolicyStore $store})
    Assert-LiveNetworkUnused $Spec $index $routes $addresses $adapter.DhcpEnabled
    $request=Join-Path $Root 'live-network-request.json'
    $receipt=Join-Path $Root 'live-network-receipt.json'
    if(Test-Path -LiteralPath $receipt){throw 'live_network_prior_receipt_requires_review'}
    $record=[ordered]@{schema_version=1;scope='owned_live_internal_host_ipv4';source_commit=$Spec.source_commit
        request_sha256=$RequestHash;switch_id=$pair.switch.Id.ToString();interface_guid=$adapter.InterfaceGuid.ToString()
        interface_index=$index;guest_a=$pair.guest_a;guest_b=$pair.guest_b
        dhcp_enabled_before=$adapter.DhcpEnabled;automatic_dhcp_disable_expected=$adapter.DhcpEnabled}
    $Lease.AssertCurrent()
    Assert-GuestOwnedRootAcl $Root $VmSpec
    Write-GuestVmRecord $request $record $true
    $current=Get-LiveNetworkPair $Spec $VmSpec $Root
    $check=Get-LiveNetworkAdapter $current.switch $Spec
    if($current.switch.Id -ne $pair.switch.Id -or $current.guest_a -cne $pair.guest_a -or $current.guest_b -cne $pair.guest_b -or
        $check.InterfaceGuid -ne $adapter.InterfaceGuid -or $check.InterfaceIndex -ne $index -or
        $check.DhcpEnabled -ne $adapter.DhcpEnabled){throw 'live_network_adapter_changed'}
    $routes=@(Get-LiveNetworkRoutes)
    $addresses=@(foreach($store in @('ActiveStore','PersistentStore')){Get-NetIPAddress -AddressFamily IPv4 -PolicyStore $store})
    Assert-LiveNetworkUnused $Spec $index $routes $addresses $check.DhcpEnabled
    $Lease.AssertCurrent()
    # Windows automatically disables DHCP when adding an address to a DHCP interface.
    # The explicit manifest flag authorizes this side effect on the verified vNIC.
    $null=New-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 -IPAddress $Spec.host_ipv4 -PrefixLength $Spec.prefix_length -PolicyStore ActiveStore
    $watch=[Diagnostics.Stopwatch]::StartNew()
    do{
        $added=@(Get-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 -PolicyStore ActiveStore |
            Where-Object{$_.IPAddress -ceq $Spec.host_ipv4 -and $_.PrefixLength -eq $Spec.prefix_length})
        if($added.Count -eq 1 -and $added[0].AddressState -eq 'Preferred'){break}
        if(@($added|Where-Object{$_.AddressState -in @('Duplicate','Invalid')}).Count){throw 'live_network_address_state_rejected'}
        Start-Sleep -Milliseconds 100
    }while($watch.Elapsed.TotalSeconds -lt 5)
    if($added.Count -ne 1 -or $added[0].AddressState -ne 'Preferred'){throw 'live_network_address_not_preferred'}
    $after=Get-LiveNetworkPair $Spec $VmSpec $Root
    $afterAdapter=Get-LiveNetworkAdapter $after.switch $Spec $true
    if($after.switch.Id -ne $pair.switch.Id -or $after.guest_a -cne $pair.guest_a -or $after.guest_b -cne $pair.guest_b -or
        $afterAdapter.InterfaceGuid -ne $adapter.InterfaceGuid -or $afterAdapter.InterfaceIndex -ne $index){throw 'live_network_post_identity_changed'}
    $afterRoutes=@(Get-LiveNetworkRoutes)
    $unchanged={ $_.store -eq 'PersistentStore' -or $_.index -ne $index -or $_.prefix -eq '0.0.0.0/0' }
    $beforeFacts=@($routes|Where-Object $unchanged|ForEach-Object{$_|ConvertTo-Json -Compress}|Sort-Object -CaseSensitive)
    $afterFacts=@($afterRoutes|Where-Object $unchanged|ForEach-Object{$_|ConvertTo-Json -Compress}|Sort-Object -CaseSensitive)
    if(($beforeFacts -join "`n") -cne ($afterFacts -join "`n")){throw 'live_network_unrelated_routes_changed'}
    Assert-LiveNetworkSelectedRoute $Spec.guest_a_ipv4 $Spec $index
    Assert-LiveNetworkSelectedRoute $Spec.guest_b_ipv4 $Spec $index
    $Lease.AssertCurrent()
    Assert-GuestOwnedRootAcl $Root $VmSpec
    $record['configured']=$true
    $record['policy_store']='ActiveStore'
    $record['dhcp_enabled_after']=$afterAdapter.DhcpEnabled
    Write-GuestVmRecord $receipt $record $true
    [ordered]@{scope=$record.scope;source_commit=$Spec.source_commit;request_sha256=$RequestHash;configured=$true;policy_store='ActiveStore'
        dhcp_automatically_disabled=$record.automatic_dhcp_disable_expected}|ConvertTo-Json -Compress
}

function Assert-LiveNetworkVmBinding($Spec,$VmSpec){
    if($VmSpec.schema_version -ne 4 -or $VmSpec.node -cne 'B' -or $VmSpec.installation_id -cne $Spec.installation_id -or
        $VmSpec.executor_source_commit -cne 'edf29d2fa1acf117815f6dbd3e074d650a02e94b' -or
        $null -eq $VmSpec.PSObject.Properties['vm_operation_source_commit'] -or
        $VmSpec.vm_operation_source_commit -cne $Spec.vm_driver_source_commit -or $VmSpec.executor_script_sha256 -cne $Spec.vm_driver_sha256 -or
        $VmSpec.config_profile.acceptance_scope -cne 'isolated-predeployment' -or $VmSpec.config_profile.operational_cutover_accepted){throw 'live_network_original_b_profile_required'}
}

function Invoke-GuestLiveNetwork{
    if(-not $Go -or [Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or -not [Environment]::Is64BitProcess){throw 'live_network_native_intent_required'}
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    try{
        if(-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))){throw 'live_network_admin_required'}
    }finally{$identity.Dispose()}
    if($Manifest.Length -gt 65536 -or $ManifestSha256 -cnotmatch '^[0-9a-f]{64}$'){throw 'live_network_request_required'}
    $bytes=[Convert]::FromBase64String($Manifest)
    if((Get-LiveNetworkHash $bytes) -cne $ManifestSha256){throw 'live_network_request_changed'}
    $requestHash=$ManifestSha256
    $json=([Text.UTF8Encoding]::new($false,$true)).GetString($bytes)
    $keys=@([regex]::Matches($json,'"([A-Za-z_][A-Za-z0-9_]*)"\s*:')|ForEach-Object{$_.Groups[1].Value})
    if($json.Contains('\') -or $keys.Count -notin @(14,15) -or @($keys|Select-Object -Unique).Count -ne $keys.Count){throw 'live_network_request_keys_rejected'}
    $spec=$json|ConvertFrom-Json
    if($spec.executor_script_sha256 -cne (Get-LiveNetworkHash ([IO.File]::ReadAllBytes($PSCommandPath)))){throw 'live_network_executor_changed'}
    $support=Join-Path $PSScriptRoot 'guest-owned-profile.ps1'
    $network=Join-Path $PSScriptRoot 'windows-guest-network.ps1'
    . (Read-LiveNetworkCode $network '46f3873221c2270878e66726e61418ef3467973cb1c7adb7e850b660d5525b73')
    Assert-LiveNetworkSpec $spec
    $root=Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)) ('N100HA_'+$spec.installation_id.Substring(0,24))
    . (Read-LiveNetworkCode (Join-Path $PSScriptRoot 'windows-guest-vm.ps1') $spec.vm_driver_sha256)
    . (Read-LiveNetworkCode $support '437d02933c2800cea4097a79ee14db3052e8c522c15f132a52c7c094d6f84045')
    $Manifest=$spec.vm_b_manifest_base64
    $ManifestSha256=$spec.vm_b_manifest_sha256
    $vmSpec=Read-GuestVmSpec $support
    Assert-LiveNetworkVmBinding $spec $vmSpec
    Initialize-GuestVmNative
    $lease=Open-GuestOwnedRoot $root $vmSpec $spec.vm_driver_sha256
    $script:guestOwnedSpec=$vmSpec
    $script:guestOwnedRoot=$root
    try{Invoke-LiveNetworkChange $spec $vmSpec $root $lease $requestHash}finally{$lease.Dispose()}
}

if($MyInvocation.InvocationName -ne '.'){Invoke-GuestLiveNetwork}
