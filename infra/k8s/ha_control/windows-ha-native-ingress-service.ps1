param(
    [Parameter(Mandatory=$true)][ValidateSet('Install','StartCaddy','StartTunnel','Observe','Rollback','StartCaddyMaintenance','StartTunnelMaintenance','ObserveMaintenance')][string]$Mode,
    [Parameter(Mandatory=$true)][string]$RequestPath,
    [Parameter(Mandatory=$true)][string]$RequestSha256,
    [Parameter(Mandatory=$true)][string]$ExpectedProfileSha256,
    [Parameter(Mandatory=$true)][string]$StageScriptSha256
)

# Manual-start native services. Credential provisioning and legacy connector stop are separate operations.
$ErrorActionPreference='Stop'
$WarningPreference='SilentlyContinue'
Set-StrictMode -Version Latest
$phase='native'
$created=@()
$held=[Collections.Generic.List[object]]::new()
$utf8=[Text.UTF8Encoding]::new($false,$true)

function Require([bool]$Value){if(-not $Value){throw 'native_frontend_rejected'}}
function Digest([byte[]]$Bytes){
    $hash=[Security.Cryptography.SHA256]::Create()
    try{return [BitConverter]::ToString($hash.ComputeHash($Bytes)).Replace('-','').ToLowerInvariant()}
    finally{$hash.Dispose()}
}

function Assert-TunnelCredential($Credential,[string]$TunnelId){
    $keys=@('AccountTag','TunnelSecret','TunnelID')
    Require ($Credential -is [Collections.Generic.Dictionary[string,object]])
    if($Credential.ContainsKey('Endpoint')){
        $keys+=@('Endpoint')
        Require ($Credential['Endpoint'] -is [string])
    }
    Assert-KeysExact $Credential $keys
    Require ($Credential['TunnelID'] -is [string] -and $Credential['TunnelID'] -ceq $TunnelId -and
        $Credential['AccountTag'] -is [string] -and $Credential['AccountTag'] -cmatch '\A[0-9a-f]{32}\z' -and
        $Credential['TunnelSecret'] -is [string] -and [Convert]::FromBase64String($Credential['TunnelSecret']).Length -eq 32)
}

function Assert-ExpectedProfile($Preliminary,[string]$Expected){
    Require ($Expected -cmatch '\A[a-f0-9]{64}\z' -and $Preliminary.profile_sha256 -is [string] -and
        $Preliminary.profile_sha256 -ceq $Expected -and $Preliminary.support_sha256 -is [string] -and
        $Preliminary.support_sha256 -cmatch '\A[a-f0-9]{64}\z')
}

function Invoke-Sc([string[]]$Arguments){
    $result=@(& (Join-Path ([Environment]::SystemDirectory) 'sc.exe') @Arguments 2>$null)
    Require ($LASTEXITCODE -eq 0)
    return ($result -join "`n")
}

function New-NativeFrontendService([string]$Name,[string]$Image){
    $result=Invoke-CimMethod -ClassName Win32_Service -MethodName Create -Arguments @{
        Name=$Name;DisplayName=$Name;PathName=$Image;ServiceType=16;ErrorControl=1;
        StartMode='Manual';StartName='LocalSystem'
    } -ErrorAction Stop
    Require ($result.ReturnValue -eq 0)
}

function Assert-PrivatePath([string]$Path){
    [TrustedStoppedV1.Contract]::LocalPath($Path)
    $entry=Get-Item -LiteralPath $Path -Force
    $depth=0
    while($null -ne $entry){
        $ancestor=$entry.FullName -ieq 'C:\ProgramData' -or $entry.FullName -ieq 'C:\'
        Assert-PreflightEntry $entry @('S-1-5-18','S-1-5-32-544') @('S-1-5-18','S-1-5-32-544') (-not $ancestor) $ancestor
        $entry=if($entry -is [IO.DirectoryInfo]){$entry.Parent}else{$entry.Directory}
        $depth++
        Require ($depth -le 12)
    }
}

function Read-Held([string]$Path,[string]$Pin,[long]$Limit){
    Require ($Pin -cmatch '\A[a-f0-9]{64}\z')
    Assert-PrivatePath $Path
    $item=Get-Item -LiteralPath $Path -Force
    Require ($item -is [IO.FileInfo] -and $item.Length -gt 0 -and $item.Length -le $Limit -and
        [bool]($item.Attributes -band [IO.FileAttributes]::ReadOnly))
    $handle=[TrustedStoppedV1.Held]::new($Path,$false)
    $held.Add($handle)
    Require ((Digest $handle.Bytes) -ceq $Pin)
    return ,$handle.Bytes
}

function Assert-KeysExact($Value,[string[]]$Names){
    Require ($Value -is [Collections.Generic.Dictionary[string,object]] -and $Value.Count -eq $Names.Count)
    foreach($name in $Names){Require ($Value.ContainsKey($name))}
}

function Assert-ServiceRecoveryDisabled($Snapshot){
    Require (-not $Snapshot.Delayed -and -not $Snapshot.NonCrash -and
        $Snapshot.Actions -eq 0 -and $Snapshot.Triggers -eq 0 -and $Snapshot.Start -eq 3)
}

function Service-Facts([string]$Name,[string]$Image,[bool]$AllowMissing=$false){
    $service=Get-CimInstance Win32_Service -Filter ("Name='"+$Name+"'") -OperationTimeoutSec 5
    if($null -eq $service){Require $AllowMissing;return $null}
    Require ($service.Name -ceq $Name -and $service.PathName -ceq $Image -and $service.StartName -ceq 'LocalSystem' -and
        $service.StartMode -ceq 'Manual' -and $service.ServiceType -ceq 'Own Process' -and -not $service.DesktopInteract)
    $native=[TrustedStoppedV1.Native]::new()
    try{
        $native.OpenForRead($Name)
        $snapshot=$native.Read($Name)
    } finally{$native.Dispose()}
    Assert-ServiceRecoveryDisabled $snapshot
    Require ($snapshot.Type -eq 16 -and $snapshot.ErrorControl -eq 1 -and $snapshot.Image -ceq $Image -and
        $snapshot.Account -ceq 'LocalSystem' -and $snapshot.Pid -eq $service.ProcessId -and
        (($snapshot.State -eq 1 -and $service.State -ceq 'Stopped') -or
         ($snapshot.State -eq 4 -and $service.State -ceq 'Running')))
    $actual=[Security.AccessControl.RawSecurityDescriptor]::new($snapshot.SecurityDescriptor,0)
    Require ($null -ne $actual.Owner -and $actual.Owner.Value -ceq 'S-1-5-32-544' -and
        $null -ne $actual.Group -and $actual.Group.Value -ceq 'S-1-5-18' -and
        $actual.DiscretionaryAcl.Count -eq 2 -and ($actual.ControlFlags -band 0x1004) -eq 0x1004)
    $seen=@()
    foreach($ace in $actual.DiscretionaryAcl){
        Require ($ace -is [Security.AccessControl.CommonAce] -and -not $ace.IsCallback -and
            $ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed -and
            $ace.SecurityIdentifier.Value -in @('S-1-5-18','S-1-5-32-544') -and
            $ace.SecurityIdentifier.Value -notin $seen -and $ace.AccessMask -eq 0xF01FF -and [int]$ace.AceFlags -eq 0)
        $seen+=@($ace.SecurityIdentifier.Value)
    }
    if($service.State -ceq 'Running'){
        $process=Get-CimInstance Win32_Process -Filter ('ProcessId='+$service.ProcessId) -OperationTimeoutSec 5
        Require ($null -ne $process -and $process.CommandLine -ceq $Image -and
            $process.ExecutablePath -ceq $Image.Split('"')[1])
    } else{Require ($service.State -ceq 'Stopped' -and $service.ProcessId -eq 0)}
    return $service
}

function Assert-Listeners($Service,[int[]]$Ports){
    Require ($null -ne $Service -and $Service.State -ceq 'Running' -and $Service.ProcessId -gt 0)
    $listeners=@(Get-NetTCPConnection -State Listen | Where-Object {$_.LocalPort -in $Ports -or $_.OwningProcess -eq $Service.ProcessId})
    Require ($listeners.Count -eq $Ports.Count)
    foreach($port in $Ports){
        $listener=@($listeners | Where-Object {$_.LocalPort -eq $port})
        Require ($listener.Count -eq 1 -and $listener[0].LocalAddress -ceq '127.0.0.1' -and
            $listener[0].OwningProcess -eq $Service.ProcessId)
    }
}

function Assert-Origins([string]$Marker){
    foreach($hostname in @('len.pe.kr','portfolio.len.pe.kr','file.len.pe.kr','admin.len.pe.kr',
            'books.len.pe.kr','memo.len.pe.kr','news.len.pe.kr','car.len.pe.kr')){
        $car=$hostname -ceq 'car.len.pe.kr'
        $port=if($car){18015}else{18080}
        $path=if($car){'/health'}else{'/ready'}
        $request=[Net.HttpWebRequest]::Create("http://127.0.0.1:$port$path")
        $request.Host=$hostname
        $request.Proxy=$null
        $request.AllowAutoRedirect=$false
        $request.Timeout=4000
        $request.ReadWriteTimeout=4000
        $request.Headers['Cache-Control']='no-cache'
        $response=$null
        try {
            $response=$request.GetResponse()
            Require ([int]$response.StatusCode -eq 200)
            if($car){
                Require ($response.Headers['X-N100-Car-Route'] -ceq $Marker)
                $stream=$response.GetResponseStream()
                Require ($stream.ReadByte() -eq 111 -and $stream.ReadByte() -eq 107 -and $stream.ReadByte() -eq -1)
            }
        } finally{if($null -ne $response){$response.Dispose()}}
        if(-not $car){
            $request=[Net.HttpWebRequest]::Create("http://127.0.0.1:$port/health")
            $request.Host=$hostname;$request.Proxy=$null;$request.AllowAutoRedirect=$false;$request.Timeout=4000
            $request.ReadWriteTimeout=4000
            $response=$null
            try{$response=$request.GetResponse();Require ([int]$response.StatusCode -eq 200 -and
                $response.Headers['X-N100-Ingress-Route'] -ceq $Marker)}
            finally{if($null -ne $response){$response.Dispose()}}
        }
    }
}

# Maintenance verifies the same protected proxy route while reporting unavailable apps honestly.
# This never exercises callback/OAuth routes and never authorizes database writes.
function Assert-MaintenanceResponse($Response,[string]$Marker,[bool]$Car,[bool]$Health){
    $status=[int]$Response.StatusCode
    Require ($status -in @(200,503))
    if($Health){
        $header=if($Car){'X-N100-Car-Route'}else{'X-N100-Ingress-Route'}
        Require ($Response.Headers[$header] -ceq $Marker)
    }
    if($Car -and $status -eq 200){
        $stream=$Response.GetResponseStream()
        Require ($stream.ReadByte() -eq 111 -and $stream.ReadByte() -eq 107 -and $stream.ReadByte() -eq -1)
    }
    return $status
}

function Assert-MaintenanceOrigins([string]$Marker){
    $statuses=[ordered]@{}
    foreach($hostname in @('len.pe.kr','portfolio.len.pe.kr','file.len.pe.kr','admin.len.pe.kr',
            'books.len.pe.kr','memo.len.pe.kr','news.len.pe.kr','car.len.pe.kr')){
        $car=$hostname -ceq 'car.len.pe.kr'
        $port=if($car){18015}else{18080}
        $paths=if($car){@('/health')}else{@('/ready','/health')}
        foreach($path in $paths){
            $request=[Net.HttpWebRequest]::Create("http://127.0.0.1:$port$path")
            $request.Host=$hostname;$request.Proxy=$null;$request.AllowAutoRedirect=$false
            $request.Timeout=4000;$request.ReadWriteTimeout=4000
            $request.Headers['Cache-Control']='no-cache'
            $response=$null
            try{
                try{$response=$request.GetResponse()}
                catch [Net.WebException]{
                    $response=$_.Exception.Response
                    Require ($null -ne $response -and [int]$response.StatusCode -eq 503)
                }
                $statuses[$hostname+$path]=Assert-MaintenanceResponse $response $Marker $car ($path -ceq '/health')
            }finally{if($null -ne $response){$response.Dispose()}}
        }
    }
    return $statuses
}

function Read-LegacyAuthority([string]$Path,[string]$Pin,[long]$Limit){
    Require ($Pin -cmatch '\A[a-f0-9]{64}\z')
    [TrustedStoppedV1.Contract]::LocalPath($Path)
    $entry=Get-Item -LiteralPath $Path -Force
    Require ($entry -is [IO.FileInfo] -and $entry.Length -gt 0 -and $entry.Length -le $Limit)
    $ancestor=$entry
    while($null -ne $ancestor){
        Assert-PreflightEntry $ancestor @('S-1-5-18','S-1-5-32-544') @() $false ($ancestor -is [IO.DirectoryInfo])
        $ancestor=if($ancestor -is [IO.DirectoryInfo]){$ancestor.Parent}else{$ancestor.Directory}
    }
    $file=[TrustedStoppedV1.Held]::new($Path,$false)
    $held.Add($file)
    Require ((Digest $file.Bytes) -ceq $Pin)
    return ,$file.Bytes
}

function Get-LegacyDeploymentBinding($Plan,$Exclusion){
    Require ($Plan.ContainsKey('windows_reactivation'))
    if($Plan.ContainsKey('deployment_plan_sha256') -or $Plan.ContainsKey('deployment_attempt_id')){
        Assert-KeysExact $Plan @('deployment_plan_sha256','deployment_attempt_id','windows_driver_sha256','windows_reactivation')
        foreach($key in @('deployment_plan_sha256','deployment_attempt_id','windows_driver_sha256')){
            Require ($Plan[$key] -is [string] -and $Plan[$key] -cmatch '\A[a-f0-9]{64}\z')
        }
        Require ($Plan['windows_driver_sha256'] -ceq $Exclusion['driver_sha256'])
        return @{plan_sha256=$Plan['deployment_plan_sha256'];attempt_id=$Plan['deployment_attempt_id']}
    }
    # A direct original plan remains bound to its own exact file digest.
    return @{plan_sha256=$Exclusion['plan_sha256'];attempt_id=$null}
}

function Assert-LegacyMarkerBinding($Marker,$Approved,$Binding){
    Assert-KeysExact $Marker @('cluster','generation','source_commit','plan_sha256','attempt_id')
    Require ($Marker['cluster'] -is [string] -and $Marker['cluster'] -ceq $Approved.authority_cluster -and
        $Marker['source_commit'] -is [string] -and $Marker['source_commit'] -ceq $Approved.authority_source_commit -and
        $Marker['plan_sha256'] -is [string] -and $Marker['plan_sha256'] -ceq $Binding.plan_sha256 -and
        $Marker['generation'] -is [long] -and $Marker['generation'] -eq $Approved.authority_generation -and
        $Marker['generation'] -ge 1 -and $Marker['attempt_id'] -is [string] -and
        $Marker['attempt_id'] -cmatch '\A[a-f0-9]{64}\z' -and
        ($null -eq $Binding.attempt_id -or $Marker['attempt_id'] -ceq $Binding.attempt_id))
}

function Assert-LegacyConnectorExclusion($Exclusion,[string]$Distribution,[switch]$RequireOnline){
    Assert-KeysExact $Exclusion @('plan_path','plan_sha256','driver_path','driver_sha256','marker_sha256',
        'linux_boot_sha256','owner_uid','original_fragment_sha256','original_unit_backup')
    foreach($key in @('plan_sha256','driver_sha256','marker_sha256','linux_boot_sha256','original_fragment_sha256')){
        Require ($Exclusion[$key] -is [string] -and $Exclusion[$key] -cmatch '\A[a-f0-9]{64}\z')
    }
    Require ($Exclusion['owner_uid'] -is [long] -and $Exclusion['owner_uid'] -ge 1000 -and
        $Exclusion['owner_uid'] -lt 65534 -and $Exclusion['original_unit_backup'] -is [string] -and
        $Exclusion['original_unit_backup'] -cmatch '\A/etc/n100-ha-cutover/cloudflared-original-[a-f0-9]{64}[.]service\z' -and
        $Distribution -cmatch '\A[A-Za-z0-9_.-]{1,80}\z')
    Require ($Exclusion['original_unit_backup'] -ceq ('/etc/n100-ha-cutover/cloudflared-original-'+$Exclusion['original_fragment_sha256']+'.service'))
    $planRaw=Read-LegacyAuthority $Exclusion['plan_path'] $Exclusion['plan_sha256'] 1MB
    $driverRaw=Read-LegacyAuthority $Exclusion['driver_path'] $Exclusion['driver_sha256'] 1MB
    $plan=[TrustedStoppedV1.Json]::Object($utf8.GetString($planRaw),1MB)
    $deploymentBinding=Get-LegacyDeploymentBinding $plan $Exclusion
    $approved=(ConvertFrom-Json $utf8.GetString($planRaw)).windows_reactivation
    Require ($approved.distribution -is [string] -and $approved.distribution -ceq $Distribution)
    $errors=$null;$tokens=$null
    $ast=[Management.Automation.Language.Parser]::ParseInput($utf8.GetString($driverRaw),[ref]$tokens,[ref]$errors)
    Require ($errors.Count -eq 0)
    $functions=@($ast.EndBlock.Statements | Where-Object {$_ -is [Management.Automation.Language.FunctionDefinitionAst]})
    foreach($name in @('Assert-HaReactivationClosed','Assert-HaWslOfflineReactivationClosed','Read-HaCutoverMarker')){
        Require (@($functions | Where-Object {$_.Name -ceq $name}).Count -eq 1)
    }
    # Only the exact approved driver's function definitions enter this scope.
    foreach($function in $functions){. ([scriptblock]::Create($function.Extent.Text))}
    $markerPath='C:\ProgramData\N100HA-Cutover\ha-controller-owner.json'
    $markerRaw=Read-LegacyAuthority $markerPath $Exclusion['marker_sha256'] 4096
    $marker=[TrustedStoppedV1.Json]::Object($utf8.GetString($markerRaw),4096)
    Assert-LegacyMarkerBinding $marker $approved $deploymentBinding
    Require ((Read-HaCutoverMarker $markerPath).Sha256 -ceq $Exclusion['marker_sha256'])
    $wsl=Join-Path ([Environment]::SystemDirectory) 'wsl.exe'
    $running=@(& $wsl --list --running --quiet | ForEach-Object {($_ -replace '\x00','').Trim()} | Where-Object {$_})
    Require ($LASTEXITCODE -eq 0)
    if($running.Count -eq 0){
        Require (-not $RequireOnline)
        Assert-HaWslOfflineReactivationClosed $approved
    } else {
        Require ($running.Count -eq 1 -and $running[0] -ceq $Distribution)
        Assert-HaReactivationClosed $approved
        $code=@'
from pathlib import Path
import hashlib, json, os, pwd, stat, subprocess, sys
expected=json.loads(sys.argv[1])
unit='cloudflared-personal-server.service'
assert hashlib.sha256(Path('/proc/sys/kernel/random/boot_id').read_bytes()).hexdigest()==expected['boot']
uid=expected['uid']
owner=pwd.getpwuid(uid)
backup=Path(expected['backup'])
for parent in (backup, *backup.parents):
    info=parent.lstat()
    assert not stat.S_ISLNK(info.st_mode) and info.st_uid==0 and not info.st_mode & 0o022
info=backup.lstat()
assert stat.S_ISREG(info.st_mode) and info.st_nlink==1 and stat.S_IMODE(info.st_mode) in (0o400,0o444)
assert hashlib.sha256(backup.read_bytes()).hexdigest()==expected['fragment']
def as_owner():
    os.setgroups([])
    os.setgid(owner.pw_gid)
    os.setuid(uid)
properties=('Id','LoadState','ActiveState','SubState','UnitFileState','MainPID','TriggeredBy','Triggers')
result=subprocess.run(('/usr/bin/systemctl','--user','show',unit,*('--property='+key for key in properties)),
    capture_output=True,text=True,timeout=6,preexec_fn=as_owner,
    env={'PATH':'/usr/bin:/bin','XDG_RUNTIME_DIR':f'/run/user/{uid}','DBUS_SESSION_BUS_ADDRESS':f'unix:path=/run/user/{uid}/bus'})
assert result.returncode==0
facts=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
assert facts==dict(Id=unit,LoadState='masked',ActiveState='inactive',SubState='dead',UnitFileState='masked',MainPID='0',TriggeredBy='',Triggers='')
for entry in Path('/proc').iterdir():
    if not entry.name.isdecimal():
        continue
    try:
        name=(entry/'comm').read_text().strip()
    except (FileNotFoundError,ProcessLookupError):
        continue
    assert name!='cloudflared'
print('ORIGINAL_CONNECTOR_EXCLUDED')
'@
        $arguments=ConvertTo-Json -InputObject @{boot=$Exclusion['linux_boot_sha256'];uid=$Exclusion['owner_uid'];
            backup=$Exclusion['original_unit_backup'];fragment=$Exclusion['original_fragment_sha256']} -Compress
        $payload=[Convert]::ToBase64String($utf8.GetBytes($code))
        $argumentBytes=[Convert]::ToBase64String($utf8.GetBytes($arguments))
        $loader="import base64,sys; sys.argv=['guard',base64.b64decode('$argumentBytes').decode()];exec(base64.b64decode('$payload'))"
        $result=@(& $wsl --distribution $Distribution --user root --exec /usr/bin/python3 -I -B -c $loader 2>$null)
        Require ($LASTEXITCODE -eq 0 -and $result.Count -eq 1 -and $result[0] -ceq 'ORIGINAL_CONNECTOR_EXCLUDED')
        Assert-HaReactivationClosed $approved
    }
    Require ((Read-HaCutoverMarker $markerPath).Sha256 -ceq $Exclusion['marker_sha256'])
    foreach($file in $held){$file.Verify()}
}

function Assert-TunnelReady {
    $request=[Net.HttpWebRequest]::Create('http://127.0.0.1:19191/ready')
    $request.Proxy=$null;$request.AllowAutoRedirect=$false;$request.Timeout=4000
    $request.ReadWriteTimeout=4000
    $response=$null
    try{$response=$request.GetResponse();Require ([int]$response.StatusCode -eq 200)}
    finally{if($null -ne $response){$response.Dispose()}}
}

try{
    Require ($Mode -cin @('Install','StartCaddy','StartTunnel','Observe','Rollback','StartCaddyMaintenance','StartTunnelMaintenance','ObserveMaintenance') -and
        [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT -and [Environment]::Is64BitProcess -and
        $PSVersionTable.PSEdition -ceq 'Desktop' -and $PSVersionTable.PSVersion.Major -eq 5 -and
        $RequestSha256 -cmatch '\A[a-f0-9]{64}\z' -and $ExpectedProfileSha256 -cmatch '\A[a-f0-9]{64}\z' -and $StageScriptSha256 -cmatch '\A[a-f0-9]{64}\z')
    $who=[Security.Principal.WindowsIdentity]::GetCurrent()
    try{Require (([Security.Principal.WindowsPrincipal]::new($who)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))}
    finally{$who.Dispose()}

    $phase='request'
    # Do not execute request-supplied paths or strings. Decode only after native helper bootstrap.
    $stageScript=Join-Path $PSScriptRoot 'windows-ha-native-ingress.ps1'
    $stageRaw=[IO.File]::ReadAllBytes($stageScript)
    Require ((Digest $stageRaw) -ceq $StageScriptSha256)
    $raw=[IO.File]::ReadAllBytes($RequestPath)
    Require ($raw.Length -gt 0 -and $raw.Length -le 8192 -and (Digest $raw) -ceq $RequestSha256)
    $preliminary=ConvertFrom-Json $utf8.GetString($raw)
    Assert-ExpectedProfile $preliminary $ExpectedProfileSha256
    $stageRoot=Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)) ('N100HA_NativeIngress_'+$preliminary.profile_sha256)
    $supportNames=@('stopped_service_installer\StrictJson.cs','stopped_service_installer\StoppedService.cs',
        'stopped_service_installer\PrivateIo.cs','windows-service-preflight.ps1')
    $supportRaw=@(foreach($name in $supportNames){,[IO.File]::ReadAllBytes((Join-Path $PSScriptRoot $name))})
    $supportHashes=@(foreach($part in $supportRaw){Digest $part})
    Require ((Digest ($utf8.GetBytes($supportHashes -join "`n"))) -ceq $preliminary.support_sha256)
    if($Mode -ceq 'Rollback'){
        # Stopping an owned service remains possible when its route or credential files are damaged.
        $using='using System;using System.IO;using System.Text;using System.Collections.Generic;using System.Security.Cryptography;using System.Runtime.InteropServices;using System.Security.AccessControl;using Microsoft.Win32.SafeHandles;'
        $units=foreach($index in 0..2){[regex]::Replace($utf8.GetString($supportRaw[$index]),'using [A-Za-z0-9_.]+;','')}
        Add-Type -Language CSharp -TypeDefinition ($using+"`n"+($units -join "`n"))
    } else {
        # A file invocation preserves PSScriptRoot on Windows PowerShell 5.1.
        # Dot-sourcing also retains the stager's script-scoped route contracts.
        $serviceMode=$Mode
        $stageFile=[IO.File]::Open($stageScript,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
        try{
            Require ($stageFile.Length -eq $stageRaw.Length)
            $pinnedStage=[byte[]]::new($stageRaw.Length);$at=0
            while($at -lt $pinnedStage.Length){$count=$stageFile.Read($pinnedStage,$at,$pinnedStage.Length-$at);Require ($count -gt 0);$at+=$count}
            Require ((Digest $pinnedStage) -ceq $StageScriptSha256)
            $validated=. $stageScript -Mode Validate -InputDirectory $stageRoot -ProfileSha256 $preliminary.profile_sha256 -SupportSha256 $preliminary.support_sha256
        }finally{$stageFile.Dispose();$Mode=$serviceMode}
        Require ((ConvertFrom-Json $validated).status -ceq 'validated')
    }
    $held=[Collections.Generic.List[object]]::new()
    $stageAst=[Management.Automation.Language.Parser]::ParseInput($utf8.GetString($stageRaw),[ref]$null,[ref]$null)
    foreach($fn in $stageAst.FindAll({param($node)$node -is [Management.Automation.Language.FunctionDefinitionAst]},$false)){
        . ([ScriptBlock]::Create($fn.Extent.Text))
    }
    $preflight=$utf8.GetString($supportRaw[3])
    $ast=[Management.Automation.Language.Parser]::ParseInput($preflight,[ref]$null,[ref]$null)
    foreach($fn in $ast.FindAll({param($node)$node -is [Management.Automation.Language.FunctionDefinitionAst]},$false)){
        . ([ScriptBlock]::Create($fn.Extent.Text))
    }
    $request=Read-Held $RequestPath $RequestSha256 8192
    $spec=[TrustedStoppedV1.Json]::Object($utf8.GetString($request),8192)
    Assert-KeysExact $spec @('schema_version','profile_sha256','stage_receipt_sha256','support_sha256','tls_files','tunnel_config_sha256',
        'tunnel_credentials_sha256','legacy_distribution','legacy_exclusion')
    Require ($spec['schema_version'] -is [long] -and $spec['schema_version'] -eq 1 -and
        $spec['profile_sha256'] -ceq $preliminary.profile_sha256 -and $spec['support_sha256'] -ceq $preliminary.support_sha256 -and
        $spec['legacy_distribution'] -is [string])
    foreach($key in @('stage_receipt_sha256','tunnel_config_sha256','tunnel_credentials_sha256')){
        Require ($spec[$key] -is [string] -and $spec[$key] -cmatch '\A[a-f0-9]{64}\z')
    }
    $tunnelRoot='C:\ProgramData\N100HA\ingress\tunnel'
    $configPath=Join-Path $tunnelRoot 'config.json'
    if($Mode -cne 'Rollback'){
        $profileRaw=Read-Held (Join-Path $stageRoot 'profile.json') $spec['profile_sha256'] 8192
        $profile=[TrustedStoppedV1.Json]::Object($utf8.GetString($profileRaw),8192)
        $stageReceiptRaw=Read-Held (Join-Path $stageRoot 'stage-receipt.json') $spec['stage_receipt_sha256'] 4096
        $stageReceipt=[TrustedStoppedV1.Json]::Object($utf8.GetString($stageReceiptRaw),4096)
        Assert-KeysExact $stageReceipt @('schema_version','status','profile_sha256','support_sha256','source_commit',
            'route_sha256','protocol','artifact_count','service_started','service_registered','credentials_staged','stage_name')
        Require ($stageReceipt['status'] -ceq 'staged' -and $stageReceipt['profile_sha256'] -ceq $spec['profile_sha256'] -and
            $stageReceipt['support_sha256'] -ceq $spec['support_sha256'] -and $stageReceipt['source_commit'] -ceq $profile['source_commit'] -and
            $stageReceipt['route_sha256'] -ceq $profile['route_sha256'] -and $stageReceipt['protocol'] -ceq 'http2' -and
            $stageReceipt['artifact_count'] -eq 4 -and $stageReceipt['service_started'] -is [bool] -and -not $stageReceipt['service_started'] -and
            $stageReceipt['service_registered'] -is [bool] -and -not $stageReceipt['service_registered'] -and
            $stageReceipt['credentials_staged'] -is [bool] -and -not $stageReceipt['credentials_staged'] -and
            $stageReceipt['stage_name'] -ceq ('N100HA_NativeIngress_'+$spec['profile_sha256']))
        foreach($name in @('caddy.exe','cloudflared.exe','Caddyfile','cloudflared-ingress.json')){
            $null=Read-Held (Join-Path $stageRoot $name) $profile['files'][$name]['sha256'] 64MB
        }

        $phase='protected_inputs'
        Assert-KeysExact $spec['tls_files'] @('server-ca.crt','client.crt','client.key')
        $tlsRoot='C:\ProgramData\N100HA\ingress\tls'
        foreach($name in @('server-ca.crt','client.crt','client.key')){$null=Read-Held (Join-Path $tlsRoot $name) $spec['tls_files'][$name] 65536}
        $credentialPath=Join-Path $tunnelRoot 'credentials.json'
        $configPath=Join-Path $tunnelRoot 'config.json'
        $configRaw=Read-Held $configPath $spec['tunnel_config_sha256'] 32768
        $config=[TrustedStoppedV1.Json]::Object($utf8.GetString($configRaw),32768)
        Assert-KeysExact $config @('tunnel','credentials-file','protocol','loglevel','metrics','ingress')
        Require ($config['tunnel'] -is [string] -and $config['tunnel'] -cmatch '\A[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\z' -and
            $config['credentials-file'] -ceq $credentialPath -and $config['protocol'] -ceq 'http2' -and
            $config['loglevel'] -ceq 'fatal' -and $config['metrics'] -ceq '127.0.0.1:19191')
        $ingressJson=ConvertTo-Json -InputObject @{ingress=$config['ingress']} -Depth 8 -Compress
        Assert-Routes ([IO.File]::ReadAllBytes((Join-Path $stageRoot 'Caddyfile'))) ($utf8.GetBytes($ingressJson)) $profile['route_sha256']
        $credentials=Read-Held $credentialPath $spec['tunnel_credentials_sha256'] 4096
        $credential=[TrustedStoppedV1.Json]::Object($utf8.GetString($credentials),4096)
        Assert-TunnelCredential $credential $config['tunnel']
    }

    $caddy='N100HA_Caddy_'+$spec['profile_sha256'].Substring(0,24)
    $tunnel='Cloudflared'
    $images=[ordered]@{}
    $images[$caddy]='"'+(Join-Path $stageRoot 'caddy.exe')+'" run --config "'+(Join-Path $stageRoot 'Caddyfile')+'" --adapter caddyfile'
    $images[$tunnel]='"'+(Join-Path $stageRoot 'cloudflared.exe')+'" --no-autoupdate --config "'+$configPath+'" tunnel run'
    $authority=Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)) ('N100HA_NativeIngressService_'+$RequestSha256)
    $intent=$utf8.GetBytes((ConvertTo-Json -InputObject ([ordered]@{schema_version=1;request_sha256=$RequestSha256;
        stage_script_sha256=$StageScriptSha256;images_sha256=(Digest ($utf8.GetBytes(($images.Values -join "`n"))))}) -Compress)+"`n")
    $phase='service'
    if($Mode -ceq 'Install'){
        foreach($name in $images.Keys){Require ($null -eq (Get-CimInstance Win32_Service -Filter ("Name='"+$name+"'") -OperationTimeoutSec 5))}
        foreach($port in @(18080,18015,19191)){Require (@(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue).Count -eq 0)}
        foreach($entry in $held){$entry.Verify()}
        Require (-not (Test-Path -LiteralPath $authority))
        [TrustedStoppedV1.NewPrivate]::Directory($authority,'',$false)
        Assert-PrivatePath $authority
        [TrustedStoppedV1.NewPrivate]::File((Join-Path $authority 'intent.json'),$intent,'',$true)
        foreach($name in $images.Keys){
            New-NativeFrontendService $name $images[$name]
            $created+=@($name)
            $null=Invoke-Sc -Arguments @('sdset',$name,'O:BAG:SYD:P(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;SY)(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)')
            $service=Service-Facts $name $images[$name]
            Require ($service.State -ceq 'Stopped')
        }
    } else {
        $maintenance=$Mode -cin @('StartCaddyMaintenance','StartTunnelMaintenance','ObserveMaintenance')
        $originStatuses=$null
        $null=Read-Held (Join-Path $authority 'intent.json') (Digest $intent) 4096
        $caddyState=Service-Facts $caddy $images[$caddy] ($Mode -ceq 'Rollback')
        $tunnelState=Service-Facts $tunnel $images[$tunnel] ($Mode -ceq 'Rollback')
        if($Mode -ceq 'Rollback'){
            foreach($name in @($tunnel,$caddy)){
                $service=Service-Facts $name $images[$name] $true
                if($null -ne $service -and $service.State -ceq 'Running'){
                    Stop-Service -Name $name -ErrorAction Stop
                    (Get-Service -Name $name).WaitForStatus([ServiceProcess.ServiceControllerStatus]::Stopped,[TimeSpan]::FromSeconds(15))
                }
                $null=Service-Facts $name $images[$name] $true
            }
        } elseif($Mode -cin @('StartCaddy','StartCaddyMaintenance')){
            Require ($tunnelState.State -ceq 'Stopped')
            if($caddyState.State -ceq 'Stopped'){
                foreach($port in @(18080,18015)){Require (@(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue).Count -eq 0)}
                foreach($entry in $held){$entry.Verify()}
                Start-Service -Name $caddy
                (Get-Service -Name $caddy).WaitForStatus([ServiceProcess.ServiceControllerStatus]::Running,[TimeSpan]::FromSeconds(15))
            }
            Assert-Listeners (Service-Facts $caddy $images[$caddy]) @(18080,18015)
            if($maintenance){$originStatuses=Assert-MaintenanceOrigins $profile['route_sha256']}else{Assert-Origins $profile['route_sha256']}
        } elseif($Mode -cin @('StartTunnel','StartTunnelMaintenance')){
            Assert-Listeners $caddyState @(18080,18015)
            if($maintenance){$originStatuses=Assert-MaintenanceOrigins $profile['route_sha256']}else{Assert-Origins $profile['route_sha256']}
            Assert-LegacyConnectorExclusion $spec['legacy_exclusion'] $spec['legacy_distribution'] -RequireOnline:($tunnelState.State -ceq 'Stopped')
            if($tunnelState.State -ceq 'Stopped'){
                Require (@(Get-NetTCPConnection -State Listen -LocalPort 19191 -ErrorAction SilentlyContinue).Count -eq 0)
                foreach($entry in $held){$entry.Verify()}
                Start-Service -Name $tunnel
                (Get-Service -Name $tunnel).WaitForStatus([ServiceProcess.ServiceControllerStatus]::Running,[TimeSpan]::FromSeconds(15))
            }
            Assert-Listeners (Service-Facts $tunnel $images[$tunnel]) @(19191)
            Assert-TunnelReady
            Assert-LegacyConnectorExclusion $spec['legacy_exclusion'] $spec['legacy_distribution']
        } else {
            if($caddyState.State -ceq 'Running'){
                Assert-Listeners $caddyState @(18080,18015)
                if($maintenance){$originStatuses=Assert-MaintenanceOrigins $profile['route_sha256']}else{Assert-Origins $profile['route_sha256']}
            }
            if($maintenance){Require ($caddyState.State -ceq 'Running' -and $tunnelState.State -ceq 'Running')}
            if($tunnelState.State -ceq 'Running'){
                Require ($caddyState.State -ceq 'Running')
                Assert-LegacyConnectorExclusion $spec['legacy_exclusion'] $spec['legacy_distribution']
                Assert-Listeners $tunnelState @(19191)
                Assert-TunnelReady
            }
        }
    }
    foreach($entry in $held){$entry.Verify()}
    $finalCaddy=Service-Facts $caddy $images[$caddy] ($Mode -ceq 'Rollback')
    $finalTunnel=Service-Facts $tunnel $images[$tunnel] ($Mode -ceq 'Rollback')
    $caddyResult=if($null -eq $finalCaddy){'Absent'}else{$finalCaddy.State}
    $tunnelResult=if($null -eq $finalTunnel){'Absent'}else{$finalTunnel.State}
    $result=[ordered]@{schema_version=1;status='PASS';mode=$Mode;request_sha256=$RequestSha256;
        profile_sha256=$spec['profile_sha256'];caddy_state=$caddyResult;tunnel_state=$tunnelResult;
        production_external_verified=$false;credentials_copied=$false}
    if($Mode -cin @('StartCaddyMaintenance','StartTunnelMaintenance','ObserveMaintenance')){
        $result.status='MAINTENANCE';$result.application_ready=$false;$result.origin_status_codes=$originStatuses
    }
    $result|ConvertTo-Json -Depth 4 -Compress
} catch {
    [ordered]@{schema_version=1;status='BLOCKED';mode=$Mode;phase=$phase;created_services=$created.Count;
        remote_action_unconfirmed=$phase -ceq 'service';credentials_copied=$false}|ConvertTo-Json -Compress
    exit 1
} finally{foreach($entry in $held){$entry.Dispose()}}
