# Loaded only from the source-pinned controller bundle. No service/task writes.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class LegacyArgv {
 [DllImport("shell32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
 static extern IntPtr CommandLineToArgvW(string command, out int count);
 [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr memory);
 public static string[] Parse(string command) {
  int count; var memory=CommandLineToArgvW(command,out count);
  if(memory==IntPtr.Zero || count<1 || count>128) throw new InvalidOperationException();
  try {var result=new string[count]; for(int i=0;i<count;i++) result[i]=Marshal.PtrToStringUni(Marshal.ReadIntPtr(memory,i*IntPtr.Size)); return result;}
  finally {LocalFree(memory);}
 }
}
'@

function Assert-LegacyArgv($Actual, $Expected) {
    if ($Actual.Count -ne $Expected.Count) {throw 'legacy_observer_identity'}
    for($i=0;$i -lt $Expected.Count;$i++) {
        if (-not [string]::Equals([string]$Actual[$i],[string]$Expected[$i],[StringComparison]::Ordinal)) {throw 'legacy_observer_identity'}
    }
}

function Get-LegacyHash([byte[]]$Bytes) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($Bytes)).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Get-LegacyObjectHash($Value) {
    return Get-LegacyHash ([Text.Encoding]::UTF8.GetBytes(($Value | ConvertTo-Json -Compress -Depth 40)))
}

function Assert-LegacyDeadline($Watch) {
    if ($Watch.Elapsed.TotalSeconds -gt 2) { throw 'legacy_guard_expired' }
}

function Get-LegacyCodePath([string]$Command) {
    $text=[Environment]::ExpandEnvironmentVariables($Command).Trim()
    if ($text.StartsWith('"')) {$path=($text -split '"')[1]} else {$path=($text -split '\s+')[0]}
    if ($path.StartsWith('\SystemRoot\',[StringComparison]::OrdinalIgnoreCase)) {$path=Join-Path $env:SystemRoot $path.Substring(12)}
    if ($path.StartsWith('\??\',[StringComparison]::Ordinal)) {$path=$path.Substring(4)}
    if (-not [IO.Path]::IsPathRooted($path) -and $path -match '^[A-Za-z0-9_.-]+\.(exe|dll)$') {
        $path=Join-Path ([Environment]::SystemDirectory) $path
    }
    if (-not [IO.Path]::IsPathRooted($path) -or $path.StartsWith('\\')) {throw 'legacy_code_path_unverified'}
    $item=Get-Item -LiteralPath $path -Force -ErrorAction Stop
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {throw 'legacy_code_unverified'}
    return $item.FullName
}

function Get-LegacyCodeHash([string]$Command) {
    return (Get-FileHash -LiteralPath (Get-LegacyCodePath $Command) -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
}

function Test-LegacyProviderCode([string]$Command) {
    $path=Get-LegacyCodePath $Command
    $root=[IO.Path]::GetFullPath($env:SystemRoot).TrimEnd('\')+'\'
    $wslRoot=[IO.Path]::GetFullPath((Join-Path $env:ProgramFiles 'WSL')).TrimEnd('\')+'\'
    # A signed executable is not by itself an OS-provider capability proof.
    # Only these provider classes are supported; all other clients stay blocked.
    $providers=@('ntoskrnl.exe','smss.exe','csrss.exe','wininit.exe','services.exe','lsass.exe','winlogon.exe',
        'svchost.exe','fontdrvhost.exe','dwm.exe','SecurityHealthService.exe','sshd.exe','taskschd.dll',
        'wslservice.exe','wslhost.exe','wslrelay.exe','vmcompute.exe','vmwp.exe','vmms.exe')
    if ([IO.Path]::GetFileName($path) -notin $providers -and [IO.Path]::GetExtension($path) -notin @('.dll','.sys')) {return $false}
    $providerRoot=$path.StartsWith($root,[StringComparison]::OrdinalIgnoreCase) -or
        ($path.StartsWith($wslRoot,[StringComparison]::OrdinalIgnoreCase) -and [IO.Path]::GetFileName($path) -in @('wslservice.exe','wslhost.exe','wslrelay.exe'))
    if (-not $providerRoot -or
        $path -match '(?i)(^|\\)(wsl|docker|kubectl|powershell|pwsh|cmd|python[0-9.]*|bash|wscript|cscript|mshta|rundll32|regsvr32|wmic|schtasks|sc|ssh|winrs)\.exe$') {return $false}
    $signature=Get-AuthenticodeSignature -LiteralPath $path -ErrorAction Stop
    return [bool]($signature.Status -eq 'Valid' -and $null -ne $signature.SignerCertificate -and
        $signature.SignerCertificate.Subject -match '(?:^|, )O=Microsoft Corporation(?:,|$)')
}

function Get-LegacyRegistry($Key, [int]$Depth = 0) {
    if ($Depth -gt 16) { throw 'legacy_registry_depth' }
    $values = [ordered]@{}
    $provider=$true; $codeCount=0
    foreach ($name in ($Key.GetValueNames() | Sort-Object -CaseSensitive)) {
        $value=$Key.GetValue($name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $code=$null
        if (($name -in @('ImagePath','ServiceDll') -or ($name -eq '' -and $Key.Name -match '\\(InprocServer32|LocalServer32)$')) -and $value -is [string] -and $value.Length) {
            $code=Get-LegacyCodeHash $value; $codeCount++; $provider=$provider -and (Test-LegacyProviderCode $value)
        }
        $values[$name] = @([string]$Key.GetValueKind($name),$value,$code)
    }
    $children = [ordered]@{}
    foreach ($name in ($Key.GetSubKeyNames() | Sort-Object -CaseSensitive)) {
        $child = $Key.OpenSubKey($name, $false)
        if ($null -eq $child) { throw 'legacy_registry_incomplete' }
        try {
            $children[$name] = Get-LegacyRegistry $child ($Depth + 1)
            $provider=$provider -and $children[$name].provider_verified; $codeCount+=$children[$name].code_count
        } finally { $child.Dispose() }
    }
    return [ordered]@{values=$values; children=$children;provider_verified=[bool]$provider;code_count=$codeCount}
}

function Test-LegacyCreator([string]$Text) {
    return ($Text -match 'windows-bootstrap|personal-server-postboot|Runner\.(Listener|Worker)|RunnerService|actions[._-]runner' -or
        $Text.IndexOf([string]$script:config.legacy_root, [StringComparison]::OrdinalIgnoreCase) -ge 0)
}

function Expand-LegacyCommand([string]$Text) {
    if ($Text -match '(?i)(?:^|\s)-EncodedCommand\s+([A-Za-z0-9+/=]+)') {
        $bytes=[Convert]::FromBase64String($matches[1])
        if ($bytes.Length -gt 1048576){throw 'legacy_command_limit'}
        return $Text+' '+[Text.Encoding]::Unicode.GetString($bytes)
    }
    return $Text
}

function Get-LegacyReferenceHashes([string]$Text, [string]$WorkingDirectory) {
    $text=Expand-LegacyCommand $Text
    $tokens=$null;$errors=$null
    $tokens=[Management.Automation.PSParser]::Tokenize($text,[ref]$errors)
    if ($errors.Count){throw 'legacy_command_unparsed'}
    $hashes=@()
    foreach($token in $tokens){
        $path=[string]$token.Content
        if ($path -notmatch '(?i)\.(ps1|py|cmd|bat|vbs|js)$'){continue}
        if (-not [IO.Path]::IsPathRooted($path)){
            if ([string]::IsNullOrEmpty($WorkingDirectory)){throw 'legacy_relative_code_unverified'}
            $path=Join-Path $WorkingDirectory $path
        }
        $hashes+=Get-LegacyCodeHash ('"'+$path+'"')
    }
    return @($hashes|Sort-Object -CaseSensitive)
}

function Get-LegacyWindowsObservation($Watch) {
    $services = @(); $servicePids = @{}; $serviceImages=@{}; $serviceProviders=@{}
    foreach ($service in (Get-CimInstance Win32_Service -ErrorAction Stop | Sort-Object Name -CaseSensitive)) {
        Assert-LegacyDeadline $Watch
        $id = Get-LegacyObjectHash @('service', $service.Name.ToLowerInvariant())
        $key = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey(('SYSTEM\CurrentControlSet\Services\' + $service.Name), $false)
        if ($null -eq $key) { throw 'legacy_service_incomplete' }
        try { $registry = Get-LegacyRegistry $key } finally { $key.Dispose() }
        $spec = Get-LegacyObjectHash ([ordered]@{name=$service.Name; path=$service.PathName; principal=$service.StartName;
            start=$service.StartMode; type=$service.ServiceType; registry=$registry})
        $services += [ordered]@{id=$id; spec_sha256=$spec; creator=[bool](Test-LegacyCreator ($service.Name + ' ' + $service.PathName));
            provider=[bool]($registry.provider_verified -and (Test-LegacyProviderCode $service.PathName));
            disabled=[bool]($service.StartMode -eq 'Disabled'); stopped=[bool]($service.State -eq 'Stopped')}
        if ($service.ProcessId -gt 0) {
            $pidKey=[string]$service.ProcessId
            $image=Get-LegacyCodeHash $service.PathName
            $verified=[bool]($registry.provider_verified -and (Test-LegacyProviderCode $service.PathName))
            if($servicePids.ContainsKey($pidKey)) {
                $servicePids[$pidKey]+=@($spec); $serviceImages[$pidKey]+=@($image)
                $serviceProviders[$pidKey]=$serviceProviders[$pidKey] -and $verified
            } else {
                $servicePids[$pidKey]=@($spec); $serviceImages[$pidKey]=@($image); $serviceProviders[$pidKey]=$verified
            }
        }
    }
    $tasks = @()
    foreach ($task in (Get-ScheduledTask -ErrorAction Stop | Sort-Object TaskPath,TaskName -CaseSensitive)) {
        Assert-LegacyDeadline $Watch
        $xml = Export-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction Stop
        $text = $task.TaskName
        $provider=$true; $code=@();foreach($action in $task.Actions){
            if ($null -ne $action.PSObject.Properties['Execute'] -and $action.Execute) {
                $text+=' '+$action.Execute+' '+$action.Arguments
                $provider=$provider -and (Test-LegacyProviderCode ('"'+[string]$action.Execute+'"'))
                $code+=Get-LegacyCodeHash ('"'+[string]$action.Execute+'"')
                $code+=Get-LegacyReferenceHashes ([string]$action.Arguments) ([string]$action.WorkingDirectory)
            }
            elseif ($null -ne $action.PSObject.Properties['ClassId'] -and $action.ClassId) {
                $key=[Microsoft.Win32.Registry]::ClassesRoot.OpenSubKey(('CLSID\'+$action.ClassId),$false)
                if ($null -eq $key){throw 'legacy_task_handler_unverified'}
                try {
                    $handler=Get-LegacyRegistry $key; $code+=Get-LegacyObjectHash $handler
                    $provider=$provider -and $handler.provider_verified -and ($handler.code_count -gt 0)
                } finally {$key.Dispose()}
            } else {throw 'legacy_task_action_unsupported'}
        }
        $text=Expand-LegacyCommand $text
        $scriptTask=($task.TaskPath -notlike '\Microsoft\*' -and $text -match '(?i)powershell|pwsh|cmd\.exe|wsl\.exe|python|bash|wscript|cscript')
        $tasks += [ordered]@{id=(Get-LegacyObjectHash @('task', ($task.TaskPath + $task.TaskName).ToLowerInvariant()));
            spec_sha256=(Get-LegacyObjectHash @($xml,$code)); creator=[bool]((Test-LegacyCreator $text) -or $scriptTask);
            provider=[bool]$provider;
            disabled=[bool]($task.State -eq 'Disabled' -and -not $task.Settings.Enabled)}
    }
    $processes = @()
    $self = Get-CimInstance Win32_Process -Filter "ProcessId=$PID" -ErrorAction Stop
    $parent = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $self.ParentProcessId) -ErrorAction Stop
    $ownParent=$false
    if ($null -eq $parent -or [string]$parent.ExecutablePath -cne [string]$script:config.guard_python.path -or
        $null -eq $parent.CreationDate -or $parent.CreationDate -gt $self.CreationDate) {throw 'legacy_observer_identity'}
    $parentArgv=@([LegacyArgv]::Parse([string]$parent.CommandLine))
    $launcher=Join-Path $script:haControlRoot 'launcher.py'
    if ($parentArgv.Count -lt 9 -or $parentArgv[4] -notin @('initialize','advance','serve')) {throw 'legacy_observer_identity'}
    $expected=@([string]$script:config.guard_python.path,'-I','-B',$launcher,$parentArgv[4],'--plan',$Plan,'--approval',$Approval)
    $index=9
    if ($parentArgv.Count -ge 11 -and $parentArgv[9] -ceq '--journal') {
        $expected+=@('--journal',(Join-Path (Split-Path $Plan -Parent) 'controller-state.json'));$index=11
    }
    if ($parentArgv.Count -gt $index -and $parentArgv[$index] -ceq '--arm' -and $parentArgv[4] -ceq 'serve') {$expected+='--arm'}
    Assert-LegacyArgv $parentArgv $expected
    if ((Get-FileHash -LiteralPath $parent.ExecutablePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $script:config.guard_python.sha256) {throw 'legacy_observer_identity'}
    Assert-LegacyArgv @([LegacyArgv]::Parse([string]$self.CommandLine)) @(
        (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'),'-NoProfile','-NonInteractive','-File',
        (Join-Path $script:haControlRoot 'windows-fence.ps1'),'-Plan',$Plan,'-Approval',$Approval,'-Go')
    $ownParent=$true
    foreach ($process in (Get-CimInstance Win32_Process -ErrorAction Stop | Sort-Object ProcessId)) {
        Assert-LegacyDeadline $Watch
        $text = $process.Name + ' ' + $process.CommandLine
        $creator = [bool](Test-LegacyCreator $text)
        # The verified caller/guard are observation processes, never exceptions
        # for a known runner/bootstrap creator. Other identities stay in graph.
        if ($process.ProcessId -eq $PID -or ($ownParent -and $process.ProcessId -eq $self.ParentProcessId)) { continue }
        $kernelObject=($process.ProcessId -in @(0,4) -or ($process.ParentProcessId -eq 4 -and $process.Name -in @('Registry','Memory Compression','Secure System','vmmem','vmmemWSL')))
        $kernel = ($kernelObject -or $servicePids.ContainsKey([string]$process.ProcessId))
        if (-not $kernel -and ([string]::IsNullOrEmpty($process.CommandLine) -or [string]::IsNullOrEmpty($process.ExecutablePath))) {
            throw 'legacy_process_incomplete'
        }
        $image = $null
        if (-not [string]::IsNullOrEmpty($process.ExecutablePath)) {
            $image = (Get-FileHash -LiteralPath $process.ExecutablePath -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
        } elseif ($serviceImages.ContainsKey([string]$process.ProcessId)) {$image=Get-LegacyObjectHash $serviceImages[[string]$process.ProcessId]}
        elseif ($kernelObject) {$image=Get-LegacyCodeHash (Join-Path ([Environment]::SystemDirectory) 'ntoskrnl.exe')}
        else { throw 'legacy_process_image_incomplete' }
        $started = if ($null -ne $process.CreationDate) {$process.CreationDate.ToUniversalTime().Ticks.ToString()} else {'kernel'}
        $processes += [ordered]@{id=(Get-LegacyObjectHash @([long]$process.ProcessId,$started)); creator=$creator;
            provider=[bool]($(if($kernelObject){Test-LegacyProviderCode (Join-Path ([Environment]::SystemDirectory) 'ntoskrnl.exe')}
                elseif($serviceProviders.ContainsKey([string]$process.ProcessId)){$serviceProviders[[string]$process.ProcessId]}
                else{Test-LegacyProviderCode ([string]$process.ExecutablePath)}));
            spec_sha256=(Get-LegacyObjectHash @($process.Name,$process.ExecutablePath,$image,$process.CommandLine,
                $started,$process.SessionId,$servicePids[[string]$process.ProcessId]))}
    }
    $startup = [ordered]@{}
    foreach ($name in @('Software\Microsoft\Windows\CurrentVersion\Run','Software\Microsoft\Windows\CurrentVersion\RunOnce',
                       'Software\Microsoft\Windows\CurrentVersion\Lxss')) {
        foreach ($hive in @([Microsoft.Win32.Registry]::CurrentUser,[Microsoft.Win32.Registry]::LocalMachine)) {
            $key = $hive.OpenSubKey($name,$false)
            $identity = Get-LegacyObjectHash @($hive.Name,$name)
            if ($null -eq $key) {$startup[$identity]='absent'}
            else {try {
                $launchCode=@()
                if ($name -notlike '*\Lxss') {
                    foreach($entry in $key.GetValueNames()) {
                        $command=$key.GetValue($entry,$null,[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
                        if ($command -isnot [string]) {throw 'legacy_startup_unsupported'}
                        $argv=@([LegacyArgv]::Parse([Environment]::ExpandEnvironmentVariables($command)))
                        if ($argv.Count -ne 1 -or -not (Test-LegacyProviderCode ('"'+$argv[0]+'"'))) {throw 'legacy_startup_unfrozen'}
                        $launchCode+=Get-LegacyCodeHash ('"'+$argv[0]+'"')
                    }
                }
                $startup[$identity]=@((Get-LegacyRegistry $key),$launchCode)
            } finally {$key.Dispose()}}
        }
    }
    foreach ($kind in @('Startup','CommonStartup')) {
        $directory = [Environment]::GetFolderPath($kind); $rows = @()
        if ([string]::IsNullOrEmpty($directory)) {throw 'legacy_startup_incomplete'}
        if (Test-Path -LiteralPath $directory) {
            foreach ($file in (Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop | Sort-Object Name -CaseSensitive)) {
                if ($file.PSIsContainer -or ($file.Attributes -band [IO.FileAttributes]::ReparsePoint)) {throw 'legacy_startup_unsupported'}
                if ($file.Extension -ine '.exe' -or -not (Test-LegacyProviderCode $file.FullName)) {throw 'legacy_startup_unfrozen'}
                $rows += @(Get-LegacyObjectHash @($file.Name,(Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash))
            }
        }
        $startup[$kind]=$rows
    }
    $principal = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    Assert-LegacyDeadline $Watch
    return [ordered]@{schema_version=1;complete=$true;boot_ticks=((Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().Ticks.ToString());
        principal_sha256=(Get-LegacyHash ([Text.Encoding]::UTF8.GetBytes($principal))); services=$services;tasks=$tasks;processes=$processes;
        startup_sha256=(Get-LegacyObjectHash $startup)}
}

function Invoke-LegacyReader([string]$FileName, [string]$Arguments, $Payload, $Watch, [bool]$RawOutput=$false) {
    Assert-LegacyDeadline $Watch
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName=$FileName; $start.Arguments=$Arguments; $start.UseShellExecute=$false
    $start.CreateNoWindow=$true; $start.RedirectStandardInput=$true; $start.RedirectStandardOutput=$true; $start.RedirectStandardError=$true
    $child = [Diagnostics.Process]::Start($start)
    $out = $child.StandardOutput.ReadToEndAsync(); $errors = $child.StandardError.ReadToEndAsync()
    try {
        if ($null -ne $Payload) {
            $json=$Payload | ConvertTo-Json -Depth 40 -Compress
            if ([Text.Encoding]::UTF8.GetByteCount($json) -gt 1048576) {throw 'legacy_payload_limit'}
            $write=$child.StandardInput.WriteAsync($json)
            $remaining = [Math]::Max(1,[int](2000-$Watch.Elapsed.TotalMilliseconds))
            if (-not $write.Wait($remaining)) {throw 'legacy_reader_expired'}
        }
        $child.StandardInput.Close()
        $remaining = [Math]::Max(1,[int](2000-$Watch.Elapsed.TotalMilliseconds))
        if (-not $child.WaitForExit($remaining)) {throw 'legacy_reader_expired'}
        if ($child.ExitCode -ne 0 -or $out.Result.Length -gt 1048576) {throw 'legacy_reader_failed'}
        Assert-LegacyDeadline $Watch
        if ($RawOutput) {return $out.Result}
        return ($out.Result | ConvertFrom-Json -ErrorAction Stop)
    } finally {
        if (-not $child.HasExited) {$child.Kill();[void]$child.WaitForExit(2000)}
        $child.Dispose()
    }
}

function Assert-LegacyBundle {
    $bundle = Split-Path (Split-Path $script:haControlRoot -Parent) -Parent
    $manifest = Read-PrivateJson (Join-Path $bundle 'source-manifest.json')
    if ($manifest.verified_sha256 -ne $script:config.controller_bundle_sha256 -or $manifest.source_commit -ne $script:config.source_commit) {
        throw 'legacy_bundle_unverified'
    }
    foreach ($entry in $manifest.bundle_files.PSObject.Properties) {
        if ($entry.Name -notmatch '^[a-zA-Z0-9_.-]+(/[a-zA-Z0-9_.-]+)*$' -or @($entry.Name -split '/' | Where-Object {$_ -in @('.','..')}).Count) {throw 'legacy_bundle_path'}
        $file = Read-PrivateBytes (Join-Path $bundle ($entry.Name -replace '/', '\'))
        if ($file.verified_sha256 -ne $entry.Value) {throw 'legacy_bundle_changed'}
    }
    $script:legacyLinux = Read-PrivateBytes (Join-Path $script:haControlRoot 'legacy_linux.py')
    $script:legacyValidator = Read-PrivateBytes (Join-Path $script:haControlRoot 'legacy_guard.py')
    if ($script:legacyLinux.verified_sha256 -ne $script:config.legacy_linux_sha256 -or
        $script:legacyValidator.verified_sha256 -ne $script:config.legacy_guard_code_sha256) {throw 'legacy_source_changed'}
    $python = Get-Item -LiteralPath $script:config.guard_python.path -Force -ErrorAction Stop
    if ($python.PSIsContainer -or ($python.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        (Get-FileHash -LiteralPath $python.FullName -Algorithm SHA256).Hash.ToLowerInvariant() -ne $script:config.guard_python.sha256) {throw 'legacy_python_unverified'}
    $allowed=@([Security.Principal.WindowsIdentity]::GetCurrent().User.Value,'S-1-5-18','S-1-5-32-544')
    $write=[Security.AccessControl.FileSystemRights]::Write -bor [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor [Security.AccessControl.FileSystemRights]::TakeOwnership
    $entry=$python
    while($null -ne $entry) {
        if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {throw 'legacy_python_unverified'}
        $acl=Get-Acl -LiteralPath $entry.FullName
        $owner=(New-Object Security.Principal.NTAccount($acl.Owner)).Translate([Security.Principal.SecurityIdentifier]).Value
        if ($owner -notin $allowed) {throw 'legacy_python_unverified'}
        foreach($rule in $acl.Access) {
            if ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) {continue}
            $sid=$rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
            $mask=if($entry.FullName -eq $python.FullName){$write}else{
                [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor [Security.AccessControl.FileSystemRights]::Delete -bor
                [Security.AccessControl.FileSystemRights]::ChangePermissions -bor [Security.AccessControl.FileSystemRights]::TakeOwnership}
            if ($rule.AccessControlType -eq 'Allow' -and $sid -notin $allowed -and ($rule.FileSystemRights -band $mask)) {throw 'legacy_python_unverified'}
        }
        $entry=if($entry -is [IO.DirectoryInfo]){$entry.Parent}else{$entry.Directory}
    }
}

function Get-LegacyWriterProof([string]$Nonce) {
    $watch=[Diagnostics.Stopwatch]::StartNew()
    $guard=$script:config.legacy_guard
    if ($guard.schema_version -ne 1 -or $guard.linux.distribution -notmatch '^[A-Za-z0-9_.-]{1,80}$' -or
        $guard.linux.reader_user -notin @('default','root')) {throw 'legacy_guard_required'}
    $windows=Get-LegacyWindowsObservation $watch
    # Code bytes were pinned against the approved manifest before being loaded.
    # The bootstrap verifies them again before execution, never a caller's JSON
    # assertion alone. No profile, site packages, environment or shell is used.
    $bootstrap = "import base64,hashlib,io,json,sys; p=json.load(sys.stdin); raw=base64.b64decode(p['code'],validate=True); h=hashlib.sha256(raw).hexdigest(); assert h==p['sha256']; sys.stdin=io.TextIOWrapper(io.BytesIO(json.dumps(p['request']).encode())); exec(compile(raw,'verified_legacy_reader','exec'),{'__name__':'__main__','__collector_sha256__':h})"
    $boot64=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($bootstrap))
    $code="import base64;exec(base64.b64decode('$boot64'))"
    $request=[ordered]@{schema_version=1;nonce=$Nonce;plan_sha256=$script:config.verified_sha256;cluster=$script:config.cluster;
        generation=$script:config.generation;collector_sha256=$script:legacyLinux.verified_sha256;spec=$guard.linux.spec}
    $distribution=[string]$guard.linux.distribution
    $listed=Invoke-LegacyReader (Join-Path $env:SystemRoot 'System32\wsl.exe') '--list --running --quiet' $null $watch $true
    $running=@(($listed -replace '\x00','') -split '[\r\n]+' | ForEach-Object {$_.Trim()})
    if ($distribution -notin $running) {throw 'existing_running_distribution_required'}
    $user=if($guard.linux.reader_user -eq 'root'){' --user root'}else{''}
    $args="--distribution $distribution$user --exec /usr/bin/env -i PATH=/usr/bin:/bin /usr/bin/python3 -I -B -S -c `"$code`""
    $linux=Invoke-LegacyReader (Join-Path $env:SystemRoot 'System32\wsl.exe') $args @{
        code=[Convert]::ToBase64String($script:legacyLinux.bytes);sha256=$script:legacyLinux.verified_sha256;request=$request} $watch
    $composite=[ordered]@{schema_version=1;nonce=$Nonce;plan_sha256=$script:config.verified_sha256;cluster=$script:config.cluster;
        generation=$script:config.generation;collector_sha256=$script:legacyLinux.verified_sha256;guard=$guard;windows=$windows;
        linux_receipt=$linux;plan_raw=$script:config.verified_raw_base64;approval_raw=$script:approved.verified_raw_base64}
    $proof=Invoke-LegacyReader $script:config.guard_python.path "-I -B -S -c `"$code`"" @{
        code=[Convert]::ToBase64String($script:legacyValidator.bytes);sha256=$script:legacyValidator.verified_sha256;request=$composite} $watch
    Assert-LegacyDeadline $watch
    if ($proof.complete -ne $true -or $proof.verified -ne $true -or $proof.nonce -ne $Nonce -or
        $proof.plan_sha256 -ne $script:config.verified_sha256 -or $proof.generation -ne $script:config.generation -or
        $proof.collector_sha256 -ne $script:legacyLinux.verified_sha256) {throw 'legacy_guard_unverified'}
    return $proof
}
