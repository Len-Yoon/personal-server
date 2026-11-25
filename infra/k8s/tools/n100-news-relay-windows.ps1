param(
    [Parameter(Mandatory)][ValidateSet('Check','Apply','Remove')][string]$Mode,
    [Parameter(Mandatory)][string]$GuestA,
    [Parameter(Mandatory)][string]$GuestB,
    [string]$ExpectedTargets
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$providers = [ordered]@{
    'kr.investing.com' = 46191
    'news.google.com' = 46192
    'api.telegram.org' = 46193
}
$allow = 'N100HA-NewsRelay-Allow-A'
$deny = 'N100HA-NewsRelay-Deny-Other'
$netsh = Join-Path $env:SystemRoot 'System32\netsh.exe'

function Convert-IPv4Number([string]$Address) {
    $bytes = [Net.IPAddress]::Parse($Address).GetAddressBytes()
    if ($bytes.Count -ne 4) { throw 'relay_requires_ipv4' }
    return (([uint64]$bytes[0] -shl 24) -bor ([uint64]$bytes[1] -shl 16) -bor
        ([uint64]$bytes[2] -shl 8) -bor [uint64]$bytes[3])
}

function Format-IPv4Number([uint64]$Number) {
    return ('{0}.{1}.{2}.{3}' -f (($Number -shr 24) -band 255), (($Number -shr 16) -band 255),
        (($Number -shr 8) -band 255), ($Number -band 255))
}

function Assert-PublicTarget([string]$Address) {
    $value = Convert-IPv4Number $Address
    foreach ($network in @('0.0.0.0/8','10.0.0.0/8','100.64.0.0/10','127.0.0.0/8',
            '169.254.0.0/16','172.16.0.0/12','192.0.0.0/24','192.0.2.0/24',
            '192.168.0.0/16','198.18.0.0/15','198.51.100.0/24','203.0.113.0/24','224.0.0.0/3')) {
        $parts = $network.Split('/')
        $shift = 32 - [int]$parts[1]
        if (($value -shr $shift) -eq ((Convert-IPv4Number $parts[0]) -shr $shift)) {
            throw 'relay_target_not_public'
        }
    }
}

function Assert-PinnedProviderTls([string]$Provider, [string]$Address) {
    $client = [Net.Sockets.TcpClient]::new([Net.Sockets.AddressFamily]::InterNetwork)
    try {
        $connect = $client.ConnectAsync([Net.IPAddress]::Parse($Address), 443)
        if (-not $connect.Wait(3500)) { throw 'relay_provider_connect_timeout' }
        $tls = [Net.Security.SslStream]::new($client.GetStream(), $false)
        try {
            $authentication = $tls.AuthenticateAsClientAsync($Provider)
            if (-not $authentication.Wait(3500)) { throw 'relay_provider_tls_timeout' }
            if (-not $tls.IsAuthenticated -or -not $tls.IsEncrypted) { throw 'relay_provider_tls_unverified' }
        } finally {
            $tls.Dispose()
        }
    } finally {
        $client.Dispose()
    }
}

function Invoke-PortProxy([string]$Action, [int]$Port, [string]$Provider = '') {
    $fields = @("listenport=$Port", "listenaddress=$($interface.IPAddress)", 'protocol=tcp')
    if ($Action -eq 'add') { $fields += @("connectaddress=$Provider", 'connectport=443') }
    $null = & $netsh interface portproxy $Action v4tov4 @fields
    if ($LASTEXITCODE -ne 0) { throw 'relay_proxy_command_failed' }
}

function Get-RelayEntries {
    $output = @(& $netsh interface portproxy show v4tov4)
    if ($LASTEXITCODE -ne 0) { throw 'relay_proxy_query_failed' }
    foreach ($line in $output) {
        if ($line -match '^\s*(\S+)\s+(\d+)\s+(\S+)\s+(\d+)\s*$') {
            [pscustomobject]@{ Local = $Matches[1]; Port = [int]$Matches[2]; Target = $Matches[3]; TargetPort = [int]$Matches[4] }
        }
    }
}

try {
    $a = Convert-IPv4Number $GuestA
    $b = Convert-IPv4Number $GuestB
    if ($a -le 1 -or $a -ge 4294967294 -or $a -eq $b) { throw 'relay_guest_collision' }
    $links = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
        if ($_.InterfaceAlias -notlike 'vEthernet (*)' -or $_.AddressState -ne 'Preferred' -or
            $_.PrefixLength -lt 24 -or $_.PrefixLength -gt 30) { return $false }
        $h = Convert-IPv4Number $_.IPAddress
        $shift = 32 - [int]$_.PrefixLength
        ($h -ne $a -and $h -ne $b -and ($a -shr $shift) -eq ($h -shr $shift) -and
            ($b -shr $shift) -eq ($h -shr $shift))
    })
    if ($links.Count -ne 1) { throw 'relay_link_not_unique' }
    $interface = $links[0]
    $switch = @(Get-VMSwitch | Where-Object {
        $_.SwitchType -eq 'Internal' -and ('vEthernet (' + $_.Name + ')') -ceq $interface.InterfaceAlias
    })
    if ($switch.Count -ne 1 -or $switch[0].Name -eq 'Default Switch' -or
        @(Get-VMNetworkAdapter -VMName '*' | Where-Object { $_.SwitchId -eq $switch[0].Id }).Count -ne 2 -or
        (Get-Service iphlpsvc).Status -ne 'Running') { throw 'relay_switch_or_service_unverified' }

    $targets = @{}
    if ($Mode -ne 'Check') {
        $parsed = ConvertFrom-Json -InputObject $ExpectedTargets
        foreach ($property in $parsed.PSObject.Properties) { $targets[$property.Name] = $property.Value }
        if ($targets.Count -ne $providers.Count) { throw 'relay_target_pin_required' }
        foreach ($provider in $providers.Keys) {
            if (-not $targets.ContainsKey($provider)) { throw 'relay_target_pin_required' }
            Assert-PublicTarget $targets[$provider]
        }
    }

    $ports = @($providers.Values)
    $rules = @(Get-NetFirewallRule -Name @($allow, $deny) -ErrorAction SilentlyContinue)
    $entries = @(Get-RelayEntries | Where-Object { $_.Port -in $ports })
    $owned = @($entries | Where-Object {
        $entry = $_
        $entry.Local -eq $interface.IPAddress -and $entry.TargetPort -eq 443 -and
        @($providers.Keys | Where-Object { $providers[$_] -eq $entry.Port -and $targets[$_] -eq $entry.Target }).Count -eq 1
    })

    if ($Mode -eq 'Remove') {
        if ($owned.Count -ne $entries.Count -or $owned.Count -ne $ports.Count -or $rules.Count -ne 2) {
            throw 'relay_remove_state_unverified'
        }
        foreach ($entry in $owned) { Invoke-PortProxy 'delete' $entry.Port }
        Remove-NetFirewallRule -Name @($allow, $deny)
        if (@(Get-RelayEntries | Where-Object { $_.Port -in $ports }).Count -ne 0) { throw 'relay_remove_failed' }
        @{ removed = $true } | ConvertTo-Json -Compress
        exit 0
    }

    if ($entries.Count -ne 0 -or $rules.Count -ne 0 -or
        @(Get-NetTCPConnection -LocalPort $ports -ErrorAction SilentlyContinue).Count -ne 0) {
        throw 'relay_ports_in_use'
    }
    $profile = @(Get-NetConnectionProfile -InterfaceIndex $interface.InterfaceIndex)
    if ($profile.Count -ne 1) { throw 'relay_profile_not_unique' }
    $category = [string]$profile[0].NetworkCategory
    $profileName = if ($category -eq 'DomainAuthenticated') { 'Domain' } else { $category }
    $settings = Get-NetFirewallProfile -Name $profileName -PolicyStore ActiveStore
    if ($settings.Enabled -ne 'True' -or $settings.DefaultInboundAction -eq 'Allow' -or
        $settings.AllowLocalFirewallRules -eq 'False') { throw 'relay_firewall_unavailable' }

    $addresses = @{}
    foreach ($provider in $providers.Keys) {
        if ($Mode -eq 'Apply') {
            # The reviewed Check result may remain reachable after DNS rotation.
            # Authenticate the original name at the exact IP before any local changes.
            $address = $targets[$provider]
            Assert-PinnedProviderTls $provider $address
        } else {
            $first = @(Resolve-DnsName -Name $provider -Type A -DnsOnly | Where-Object Type -eq A |
                Select-Object -ExpandProperty IPAddress | Sort-Object -Unique | Select-Object -First 1)
            if ($first.Count -ne 1) { throw 'relay_provider_dns_unavailable' }
            $address = $first[0]
            Assert-PublicTarget $address
        }
        $addresses[$provider] = $address
    }
    if ($Mode -eq 'Apply') {
        $written = [Collections.Generic.List[int]]::new()
        $addedRules = [Collections.Generic.List[string]]::new()
        try {
            $scope = @{
                Direction = 'Inbound'; InterfaceAlias = $interface.InterfaceAlias
                LocalAddress = $interface.IPAddress; LocalPort = $ports
                Protocol = 'TCP'; Profile = $profileName
            }
            $otherRemotes = @(
                "0.0.0.1-$(Format-IPv4Number ($a - 1))",
                "$(Format-IPv4Number ($a + 1))-255.255.255.254"
            )
            $null = New-NetFirewallRule @scope -Name $deny -DisplayName $deny -RemoteAddress $otherRemotes -Action Block
            $addedRules.Add($deny)
            $null = New-NetFirewallRule @scope -Name $allow -DisplayName $allow -RemoteAddress $GuestA -Action Allow
            $addedRules.Add($allow)
            foreach ($provider in $providers.Keys) {
                Invoke-PortProxy 'add' $providers[$provider] $targets[$provider]
                $written.Add($providers[$provider])
            }
            $until = (Get-Date).AddSeconds(4)
            do {
                $listeners = @(Get-NetTCPConnection -LocalAddress $interface.IPAddress -LocalPort $ports -State Listen -ErrorAction SilentlyContinue)
                $ready = @($listeners | Select-Object -ExpandProperty LocalPort -Unique).Count -eq $ports.Count
                if (-not $ready) { Start-Sleep -Milliseconds 300 }
            } until ($ready -or (Get-Date) -ge $until)
            if (-not $ready) { throw 'relay_listener_readback_failed' }
        } catch {
            foreach ($port in $written) { Invoke-PortProxy 'delete' $port }
            foreach ($name in $addedRules) { Remove-NetFirewallRule -Name $name -ErrorAction SilentlyContinue }
            throw
        }
    }

    @{ relay = $interface.IPAddress; providers = $addresses; applied = ($Mode -eq 'Apply') } |
        ConvertTo-Json -Depth 3 -Compress
} catch {
    throw 'news_relay_windows_failed; observe before retry'
}
