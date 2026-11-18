param(
    [Parameter(Mandatory=$true)][ValidateSet('Validate','Stage')][string]$Mode,
    [Parameter(Mandatory=$true)][string]$InputDirectory,
    [Parameter(Mandatory=$true)][string]$ProfileSha256,
    [Parameter(Mandatory=$true)][string]$SupportSha256
)

# Validates and stages inert route material. The staging receipt is not an activation approval.
$ErrorActionPreference='Stop'
$WarningPreference='SilentlyContinue'
Set-StrictMode -Version Latest
$script:phase='input'
$script:native=[Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT
$script:utf8=[Text.UTF8Encoding]::new($false,$true)
$script:held=[Collections.Generic.List[object]]::new()
$script:privateSids=@('S-1-5-18','S-1-5-32-544')
$script:artifactNames=@('caddy.exe','cloudflared.exe','Caddyfile','cloudflared-ingress.json')
$script:mainRoutes=[ordered]@{
    'admin.len.pe.kr'='portal-web';'books.len.pe.kr'='book-memo';'file.len.pe.kr'='portal-web';'len.pe.kr'='portal-web';
    'memo.len.pe.kr'='youtube-memo';'news.len.pe.kr'='crawler-worker';'portal.len.pe.kr'='';'portfolio.len.pe.kr'='portal-web'
}
$script:hosts=@($script:mainRoutes.Keys)+@('car.len.pe.kr')

function Need([bool]$Valid){if(-not $Valid){throw 'native_ingress_stage_rejected'}}

function Hash([byte[]]$Bytes){
    $algorithm=[Security.Cryptography.SHA256]::Create()
    try{return [BitConverter]::ToString($algorithm.ComputeHash($Bytes)).Replace('-','').ToLowerInvariant()}
    finally{$algorithm.Dispose()}
}

function Is-Pin($Value){return $Value -is [string] -and $Value -cmatch '\A[0-9a-f]{64}\z'}

function Assert-Keys($Object,[string[]]$Keys){
    Need ($Object -is [Collections.Generic.Dictionary[string,object]] -and $Object.Count -eq $Keys.Count)
    foreach($key in $Keys){Need ($Object.ContainsKey($key))}
}

function Assert-CanonicalEntry($Entry){
    while($null -ne $Entry){
        Need (-not ($Entry.Attributes -band [IO.FileAttributes]::ReparsePoint))
        $Entry=if($Entry -is [IO.DirectoryInfo]){$Entry.Parent}else{$Entry.Directory}
    }
}

function Read-Input([string]$Name,[long]$Limit){
    $path=Join-Path $InputDirectory $Name
    $item=Get-Item -LiteralPath $path -Force
    Need ($item -is [IO.FileInfo] -and $item.Length -gt 0 -and $item.Length -le $Limit)
    Assert-CanonicalEntry $item
    if($script:native){
        $file=[TrustedStoppedV1.Held]::new($path,$false)
        $script:held.Add($file)
        Need ($file.Bytes.Length -eq $item.Length)
        return ,$file.Bytes
    }
    $file=[IO.File]::Open($path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    try {
        Need ($file.Length -eq $item.Length)
        $bytes=[byte[]]::new([int]$file.Length)
        $read=0
        while($read -lt $bytes.Length){
            $count=$file.Read($bytes,$read,$bytes.Length-$read)
            Need ($count -gt 0)
            $read+=$count
        }
        Need ($file.ReadByte() -eq -1)
        return ,$bytes
    } finally{$file.Dispose()}
}

function Assert-PeAmd64([byte[]]$Bytes){
    Need ($Bytes.Length -ge 256 -and [BitConverter]::ToUInt16($Bytes,0) -eq 0x5a4d)
    $pe=[BitConverter]::ToInt32($Bytes,60)
    Need ($pe -ge 64 -and $pe -le ($Bytes.Length-26))
    $optional=[BitConverter]::ToUInt16($Bytes,($pe+20))
    $characteristics=[BitConverter]::ToUInt16($Bytes,($pe+22))
    Need ([BitConverter]::ToUInt32($Bytes,$pe) -eq 0x4550 -and
        [BitConverter]::ToUInt16($Bytes,($pe+4)) -eq 0x8664 -and
        [BitConverter]::ToUInt16($Bytes,($pe+6)) -gt 0 -and
        $optional -ge 112 -and $pe+24+$optional -le $Bytes.Length -and
        ($characteristics -band 0x2002) -eq 2 -and [BitConverter]::ToUInt16($Bytes,($pe+24)) -eq 0x20b)
}

function Get-CaddyProxy([string]$Target,[string]$Service,[bool]$Callback){
    $indent=if($Callback){'        '}else{'    '}
    $forwardedHost=if($Callback){'car-care-worker.ha.internal'}else{'{hostport}'}
    $lines=@("${indent}reverse_proxy https://$($Target):10443 {", "${indent}    header_up Host $forwardedHost",
        "${indent}    transport http {", "${indent}        tls_server_name $Service.ha.internal",
        "${indent}        tls_trust_pool file C:/ProgramData/N100HA/ingress/tls/server-ca.crt",
        "${indent}        tls_client_auth C:/ProgramData/N100HA/ingress/tls/client.crt C:/ProgramData/N100HA/ingress/tls/client.key")
    if($Callback){$lines+=@("${indent}        dial_timeout 2s")}
    return (($lines+@("${indent}    }","${indent}}")) -join "`n")
}

function Assert-CaddyRoutes([string]$Config,[string]$Route){
    # Replay the renderer's bounded grammar. Neither a reviewed hash nor a route marker
    # alone can rule out directives that read mutable modules, paths or environment state.
    $upstreams=[regex]::Matches($Config,'(?m)^ +reverse_proxy https://([0-9.]+):10443 \{\n')
    Need ($upstreams.Count -eq 8)
    $target=$upstreams[0].Groups[1].Value
    [Net.IPAddress]$address=$null
    Need ([Net.IPAddress]::TryParse($target,[ref]$address) -and $address.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork -and
        $address.ToString() -ceq $target)
    $octets=$address.GetAddressBytes()
    Need ($octets[0] -eq 10 -or ($octets[0] -eq 172 -and $octets[1] -in (16..31)) -or
        ($octets[0] -eq 192 -and $octets[1] -eq 168) -or $target -ceq '192.0.2.1')

    $expected=@{
        '{'="{`n    admin off`n    auto_https off`n    persist_config off`n    log {`n        output discard`n    }`n}"
        '(common_tls) {'="(common_tls) {`n    encode zstd gzip`n}"
        '(portal_file_upload_limit) {'=(@('(portal_file_upload_limit) {','    @file_uploads path /files/uploads',
            '    request_body @file_uploads {','        max_size 30MB','    }','}') -join "`n")
    }
    foreach($port in 18080,18015){$expected["http://:$port {"]="http://:$port {`n    bind 127.0.0.1`n    respond 404`n}"}
    $marker=@('    bind 127.0.0.1','    @native_health {','        method GET','        path /health','    }',
        '    header @native_health {',"        X-N100-Ingress-Route $Route",'        Cache-Control no-store','        match status 200','    }')
    foreach($hostname in $script:mainRoutes.Keys){
        $service=$script:mainRoutes[$hostname]
        $heading="http://$($hostname):18080 {"
        $lines=@($heading)+$marker+@('    import common_tls')
        if($service -ceq 'portal-web'){$lines+=@('    import portal_file_upload_limit')}
        if($service){$lines+=@(Get-CaddyProxy $target $service $false)}
        else{$lines+=@('    redir https://len.pe.kr{uri} permanent')}
        $expected[$heading]=(($lines+@('}')) -join "`n")
    }

    $carHeading='http://car.len.pe.kr:18015 {'
    $carAllowlist='        expression `(method(''GET'') && path(''/health'', ''/oauth/hyundai/callback'', ''/oauth/hyundai/data-redirect'')) || (method(''POST'') && path(''/oauth/hyundai/data-redirect''))`'
    $expected[$carHeading]=(@($carHeading,'    bind 127.0.0.1','    @health {','        method GET','        path /health','    }',
        '    header @health {',"        X-N100-Car-Route $Route",'        Cache-Control no-store','        match status 200','    }',
        '    @allowed {',$carAllowlist,'    }','    handle @allowed {',(Get-CaddyProxy $target 'car-care-worker' $true),
        '    }','    respond 404','}') -join "`n")

    $leading=@('{','(common_tls) {','(portal_file_upload_limit) {')
    $mainHeadings=@(foreach($hostname in $script:mainRoutes.Keys){"http://$($hostname):18080 {"})
    $trailing=@('http://:18080 {',$carHeading,'http://:18015 {')
    $trailingStart=$leading.Count+$mainHeadings.Count
    $sections=[regex]::new('(?m)\G(?<block>(?<heading>[^\s}][^\n]*)\n[\s\S]*?^\})\n(?:\n|\z)')
    $position=0
    $index=0
    while($position -lt $Config.Length){
        $section=$sections.Match($Config,$position)
        Need ($section.Success)
        $heading=$section.Groups['heading'].Value
        Need ($expected.ContainsKey($heading) -and $expected[$heading] -ceq $section.Groups['block'].Value)
        if($index -lt $leading.Count){Need ($heading -ceq $leading[$index])}
        elseif($index -lt $trailingStart){Need ($heading -cin $mainHeadings)}
        else{
            $trailingIndex=$index-$trailingStart
            Need ($trailingIndex -lt $trailing.Count -and $heading -ceq $trailing[$trailingIndex])
        }
        $expected.Remove($heading)
        $position+=$section.Length
        $index++
    }
    Need ($expected.Count -eq 0)
}

function Assert-Routes([byte[]]$Caddy,[byte[]]$Tunnel,[string]$Route){
    Assert-CaddyRoutes ($script:utf8.GetString($Caddy)) $Route

    $routes=[TrustedStoppedV1.Json]::Object($script:utf8.GetString($Tunnel),16384)
    Assert-Keys $routes @('ingress')
    $rules=$routes['ingress']
    Need ($rules -is [Collections.Generic.List[object]] -and $rules.Count -eq ($script:hosts.Count + 1))
    for($index=0;$index -lt $script:hosts.Count;$index++){
        $rule=$rules[$index]
        $hostname=$script:hosts[$index]
        $port=if($hostname -ceq 'car.len.pe.kr'){18015}else{18080}
        Assert-Keys $rule @('hostname','originRequest','service')
        Assert-Keys $rule['originRequest'] @('httpHostHeader')
        Need ($rule['hostname'] -is [string] -and $rule['hostname'] -ceq $hostname -and
            $rule['originRequest']['httpHostHeader'] -is [string] -and $rule['originRequest']['httpHostHeader'] -ceq $hostname -and
            $rule['service'] -is [string] -and $rule['service'] -ceq "http://127.0.0.1:$port")
    }
    Assert-Keys $rules[$script:hosts.Count] @('service')
    Need ($rules[$script:hosts.Count]['service'] -is [string] -and $rules[$script:hosts.Count]['service'] -ceq 'http_status:404')
}

function Assert-PrivateAcl([string]$Path,[bool]$Directory){
    $item=Get-Item -LiteralPath $Path -Force
    Need ($item.PSIsContainer -eq $Directory -and -not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint))
    if(-not $Directory){Need ([bool]($item.Attributes -band [IO.FileAttributes]::ReadOnly))}
    $acl=[Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $Path).Sddl)
    Need ($acl.Owner.Value -ceq 'S-1-5-32-544' -and $acl.Group.Value -ceq 'S-1-5-18' -and
        ($acl.ControlFlags -band 0x1004) -eq 0x1004 -and
        $acl.DiscretionaryAcl.Count -eq 2)
    $flags=if($Directory){3}else{0}
    $seen=@()
    foreach($ace in $acl.DiscretionaryAcl){
        Need ($ace -is [Security.AccessControl.CommonAce] -and -not $ace.IsCallback -and
            $ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed -and
            $ace.AccessMask -eq 2032127 -and [int]$ace.AceFlags -eq $flags -and
            $ace.SecurityIdentifier.Value -in $script:privateSids -and $ace.SecurityIdentifier.Value -notin $seen)
        $seen+=@($ace.SecurityIdentifier.Value)
    }
}

function Write-StagedFile([string]$Directory,[string]$Name,[byte[]]$Bytes){
    $path=Join-Path $Directory $Name
    [TrustedStoppedV1.NewPrivate]::File($path,$Bytes,'',$true)
    Assert-PrivateAcl $path $false
    $file=[TrustedStoppedV1.Held]::new($path,$false)
    try{
        $file.Verify()
        Need ((Hash $file.Bytes) -ceq (Hash $Bytes))
    } finally{$file.Dispose()}
}

$published=$false
$quarantineCreated=$false
try{
    Need ($Mode -cin @('Validate','Stage') -and (Is-Pin $ProfileSha256) -and (Is-Pin $SupportSha256) -and
        [IO.Path]::IsPathRooted($InputDirectory) -and
        $InputDirectory -ceq [IO.Path]::GetFullPath($InputDirectory))
    if($Mode -ceq 'Stage'){
        $script:phase='native_intent'
        Need ($script:native -and [Environment]::Is64BitProcess -and $PSVersionTable.PSEdition -ceq 'Desktop' -and
            $PSVersionTable.PSVersion.Major -eq 5)
        $who=[Security.Principal.WindowsIdentity]::GetCurrent()
        try {Need (([Security.Principal.WindowsPrincipal]::new($who)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))}
        finally{$who.Dispose()}
    }

    $script:phase='support'
    $support=Join-Path $PSScriptRoot 'stopped_service_installer'
    $paths=@((Join-Path $support 'StrictJson.cs'),(Join-Path $support 'StoppedService.cs'),
        (Join-Path $support 'PrivateIo.cs'),(Join-Path $PSScriptRoot 'windows-service-preflight.ps1'))
    $sources=@(foreach($path in $paths){,[IO.File]::ReadAllBytes($path)})
    $supportHashes=@(foreach($source in $sources){Hash $source})
    Need ((Hash ($script:utf8.GetBytes($supportHashes -join "`n"))) -ceq $SupportSha256)
    $using='using System;using System.IO;using System.Text;using System.Collections.Generic;using System.Security.Cryptography;using System.Runtime.InteropServices;using System.Security.AccessControl;using Microsoft.Win32.SafeHandles;'
    $units=foreach($index in 0..2){[regex]::Replace($script:utf8.GetString($sources[$index]),'using [A-Za-z0-9_.]+;','')}
    Add-Type -Language CSharp -TypeDefinition ($using+"`n"+($units -join "`n"))
    if($script:native){
        [TrustedStoppedV1.Contract]::LocalPath($InputDirectory)
        $inputRoot=Get-Item -LiteralPath $InputDirectory -Force
        Need ($inputRoot -is [IO.DirectoryInfo])
        Assert-CanonicalEntry $inputRoot
        $script:held.Add([TrustedStoppedV1.Held]::new($InputDirectory,$true))
    }

    $script:phase='profile'
    $profileRaw=Read-Input 'profile.json' 8192
    Need ((Hash $profileRaw) -ceq $ProfileSha256)
    $profile=[TrustedStoppedV1.Json]::Object($script:utf8.GetString($profileRaw),8192)
    Assert-Keys $profile @('schema_version','source_commit','route_sha256','protocol','files')
    Need ($profile['schema_version'] -is [long] -and $profile['schema_version'] -eq 1 -and
        $profile['source_commit'] -is [string] -and $profile['source_commit'] -cmatch '\A[0-9a-f]{40}\z' -and
        (Is-Pin $profile['route_sha256']) -and $profile['protocol'] -is [string] -and $profile['protocol'] -ceq 'http2')
    Assert-Keys $profile['files'] $script:artifactNames

    $script:phase='artifacts'
    $artifacts=[ordered]@{}
    foreach($name in $script:artifactNames){
        $pin=$profile['files'][$name]
        Assert-Keys $pin @('sha256','bytes')
        $limit=if($name.EndsWith('.exe')){64MB}else{16384}
        Need ((Is-Pin $pin['sha256']) -and $pin['bytes'] -is [long] -and $pin['bytes'] -gt 0 -and $pin['bytes'] -le $limit)
        $bytes=Read-Input $name $limit
        Need ($bytes.Length -eq $pin['bytes'] -and (Hash $bytes) -ceq $pin['sha256'])
        if($name.EndsWith('.exe')){Assert-PeAmd64 $bytes}
        $artifacts[$name]=$bytes
    }
    Assert-Routes $artifacts['Caddyfile'] $artifacts['cloudflared-ingress.json'] $profile['route_sha256']
    $receipt=[ordered]@{schema_version=1;status='validated';profile_sha256=$ProfileSha256;support_sha256=$SupportSha256;
        source_commit=$profile['source_commit'];route_sha256=$profile['route_sha256'];protocol='http2';
        artifact_count=$script:artifactNames.Count;service_started=$false;service_registered=$false;credentials_staged=$false}

    if($Mode -ceq 'Stage'){
        $script:phase='destination'
        $scriptAst=[Management.Automation.Language.Parser]::ParseInput($script:utf8.GetString($sources[3]),[ref]$null,[ref]$null)
        foreach($fn in $scriptAst.FindAll({param($node)$node -is [Management.Automation.Language.FunctionDefinitionAst]},$false)){
            . ([ScriptBlock]::Create($fn.Extent.Text))
        }
        $parent=[Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
        Need ($parent -ieq 'C:\ProgramData')
        $parentEntry=Get-Item -LiteralPath $parent -Force
        Need ($parentEntry -is [IO.DirectoryInfo])
        Assert-PreflightEntry $parentEntry $script:privateSids $script:privateSids $false $true
        Assert-PreflightEntry $parentEntry.Parent $script:privateSids $script:privateSids $false $true
        $parentHandle=[TrustedStoppedV1.Held]::new($parent,$true)
        $script:held.Add($parentHandle)
        $stageName='N100HA_NativeIngress_'+$ProfileSha256
        $destination=Join-Path $parent $stageName
        $pending=Join-Path $parent ($stageName+'.pending-'+[Guid]::NewGuid().ToString('N'))
        Need (-not (Test-Path -LiteralPath $destination))

        $script:phase='stage'
        [TrustedStoppedV1.NewPrivate]::Directory($pending,'',$false)
        $quarantineCreated=$true
        Assert-PrivateAcl $pending $true
        $pendingHandle=[TrustedStoppedV1.Held]::new($pending,$true)
        try {
            $pendingIdentity=$pendingHandle.Identity
            foreach($name in $script:artifactNames){Write-StagedFile $pending $name $artifacts[$name]}
            Write-StagedFile $pending 'profile.json' $profileRaw
            foreach($entry in $script:held){$entry.Verify()}
            $receipt.status='staged'
            $receipt['stage_name']=$stageName
            $receiptRaw=$script:utf8.GetBytes((ConvertTo-Json -InputObject $receipt -Compress)+"`n")
            Write-StagedFile $pending 'stage-receipt.json' $receiptRaw
            $actual=@(Get-ChildItem -LiteralPath $pending -Force | ForEach-Object {$_.Name} | Sort-Object -CaseSensitive)
            $expected=@(($script:artifactNames + @('profile.json','stage-receipt.json')) | Sort-Object -CaseSensitive)
            Need (($actual -join ',') -ceq ($expected -join ','))
            $pendingHandle.Verify()
        } finally{$pendingHandle.Dispose()}

        $script:phase='publish'
        $parentHandle.Verify()
        [IO.Directory]::Move($pending,$destination)
        $published=$true
        $quarantineCreated=$false
        $destinationHandle=[TrustedStoppedV1.Held]::new($destination,$true)
        try {
            Need ($destinationHandle.Identity -ceq $pendingIdentity)
            Assert-PrivateAcl $destination $true
        } finally{$destinationHandle.Dispose()}
    }
    ConvertTo-Json -InputObject $receipt -Compress
} catch {
    [ordered]@{schema_version=1;status='rejected';phase=$script:phase;published=$published;
        incomplete_private_staging=$quarantineCreated;service_started=$false;credentials_staged=$false} | ConvertTo-Json -Compress
    exit 1
} finally {
    foreach($entry in $script:held){$entry.Dispose()}
}
