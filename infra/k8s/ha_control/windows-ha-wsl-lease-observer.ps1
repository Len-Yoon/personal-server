# Read-only Windows/WSL proof in the already authenticated Windows broker.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Initialize-HaLeaseObservation($InputConfig, [string]$PlanText, [string]$MarkerSha, [string]$WindowsBootSha, [string]$CollectorSha) {
    $fields = @($InputConfig.PSObject.Properties.Name | Sort-Object -CaseSensitive)
    if ('reactivation_plan_sha256' -cin $fields) {
        if ($InputConfig.reactivation_plan_sha256 -cnotmatch '\A[a-f0-9]{64}\z') { throw 'lease_proof_callback_scope' }
        $fields = @($fields | Where-Object { $_ -cne 'reactivation_plan_sha256' })
    }
    if (($fields -join ',') -cne 'a_disk_paths_sha256,b_disk_paths_sha256,driver_path,driver_sha256,installation_id,reactivation_plan_path,runtime_path,runtime_sha256,source_loader_sha256,source_manifest_path,source_manifest_sha256,source_root,vm_a,vm_b,vm_observer_path,vm_observer_sha256,wsl_uid' -or
        $InputConfig.runtime_sha256 -cnotmatch '\A[a-f0-9]{64}\z' -or
        $InputConfig.installation_id -cnotmatch '\A[a-f0-9]{64}\z' -or
        $InputConfig.a_disk_paths_sha256 -cnotmatch '\A[a-f0-9]{64}\z' -or $InputConfig.b_disk_paths_sha256 -cnotmatch '\A[a-f0-9]{64}\z' -or
        $InputConfig.source_loader_sha256 -cnotmatch '\A[a-f0-9]{64}\z' -or $InputConfig.source_manifest_sha256 -cnotmatch '\A[a-f0-9]{64}\z' -or
        $InputConfig.source_root -cnotmatch '\A/home/[a-zA-Z0-9_.-]+/\.local/state/n100-ha/cutover-source-[a-f0-9]{40}/infra/k8s\z' -or
        $InputConfig.runtime_path -cnotmatch '\A/home/[a-zA-Z0-9_.-]+/\.local/state/n100-ha/[a-zA-Z0-9_.-]+/runtime\.json\z' -or
        $InputConfig.source_manifest_path -cne ($InputConfig.source_root -creplace '/infra/k8s\z','.manifest.json') -or
        ($InputConfig.wsl_uid -isnot [int] -and $InputConfig.wsl_uid -isnot [long]) -or
        $InputConfig.wsl_uid -lt 1 -or $InputConfig.wsl_uid -ge 4294967295L -or
        $InputConfig.source_root -match '/\.\.?/' -or $InputConfig.runtime_path -match '/\.\.?/') {
        throw 'lease_proof_callback_scope'
    }
    $boot = (Get-CimInstance Win32_OperatingSystem -ErrorAction Stop).LastBootUpTime.ToUniversalTime().Ticks
    if ((Get-HaReactivationHash ([string]$boot)) -cne $WindowsBootSha) { throw 'lease_proof_host_boot_changed' }
    $plan = (ConvertFrom-Json -InputObject $PlanText).windows_reactivation
    $authority = Get-HaCutoverAuthority $plan
    $path = Join-Path $authority.directory 'ha-controller-owner.json'
    $security = [Security.AccessControl.FileSecurity]::new()
    $security.SetOwner($authority.admins)
    $security.SetAccessRuleProtection($true, $false)
    foreach ($writer in @($authority.admins,$authority.system)) {
        $security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($writer, 'FullControl', 'Allow'))
    }
    $security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($authority.controller, 'Read', 'Allow'))
    $sections = [Security.AccessControl.AccessControlSections]'Owner,Access'
    $marker = @{path=$path;sha256=$MarkerSha;acl=$security.GetSecurityDescriptorSddlForm($sections);
                file_id=(Get-HaMarkerFileId $path)}
    Assert-HaOwnedCutoverMarker $marker
    if ($plan.distribution -cne 'Ubuntu-24.04') { throw 'lease_proof_callback_scope' }
    Import-Module Hyper-V -ErrorAction Stop
    $script:ClosureVmA = [guid]::ParseExact($InputConfig.vm_a,'D')
    $script:ClosureVmB = [guid]::ParseExact($InputConfig.vm_b,'D')
    if ($script:ClosureVmA -eq [guid]::Empty -or $script:ClosureVmB -eq [guid]::Empty -or
        $script:ClosureVmA -eq $script:ClosureVmB) { throw 'lease_proof_vm_scope' }
    # Initialize the Hyper-V provider before readiness. Each proof still reads
    # both VMs and their disks again inside the request deadline.
    $null = Get-FixedVmObserverSnapshot $InputConfig.installation_id $script:ClosureVmA $script:ClosureVmB
    $script:ClosureInput = $InputConfig
    $script:ClosurePlan = $plan
    $script:ClosureMarker = $marker
    $script:ClosureBoot = $WindowsBootSha
    $script:ClosureWsl = Join-Path ([Environment]::SystemDirectory) 'wsl.exe'
    $running = @(& $script:ClosureWsl --list --running --quiet | ForEach-Object { ($_ -replace ([char]0),'').Trim() })
    if ($LASTEXITCODE -ne 0 -or $plan.distribution -cnotin $running) { throw 'lease_proof_wsl_not_running' }
    if ($CollectorSha -cnotmatch '\A[a-f0-9]{64}\z') { throw 'lease_proof_callback_scope' }
    $source = [string]$InputConfig.source_root
    $bootstrap = "import hashlib,os,pathlib,sys; p,loader,manifest,pin,uid,collector=sys.argv[1:]; p=pathlib.Path(p); assert os.geteuid()==int(uid); b=(p/'ha_control/native_wsl_launcher.py').read_bytes(); assert hashlib.sha256(b).hexdigest()==loader; e={'__name__':'verified_source_loader'}; exec(compile(b,'native_wsl_launcher.py','exec'),e); s=e['verify'](p,pathlib.Path(manifest),pin,int(uid)); assert hashlib.sha256(s['ha_control/wsl_writer_proof.py']).hexdigest()==collector; sys.meta_path.insert(0,e['VerifiedSources'](p,s)); from ha_control.wsl_writer_proof import main; raise SystemExit(main(persistent=True))"
    $command = '--distribution Ubuntu-24.04 --exec /usr/bin/python3 -I -B -S -c "' + $bootstrap + '" ' +
        (@($source,$script:ClosureInput.source_loader_sha256,$script:ClosureInput.source_manifest_path,
            $script:ClosureInput.source_manifest_sha256,$script:ClosureInput.wsl_uid,$CollectorSha) -join ' ')
    $start = [Diagnostics.ProcessStartInfo]::new($script:ClosureWsl, $command)
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($start)
    try {
        $process.StandardInput.AutoFlush = $true
        $ready = $process.StandardOutput.ReadLineAsync()
        # Any stderr byte, or EOF, invalidates this otherwise silent collector.
        $script:ClosureStderr = $process.StandardError.BaseStream.ReadAsync([byte[]]::new(1), 0, 1)
        if (-not $ready.Wait(4000) -or $ready.Result -cne 'ready' -or $process.HasExited -or
            $script:ClosureStderr.IsCompleted) { throw 'lease_proof_wsl_unconfirmed' }
        $script:ClosureProcess = $process
    } catch {
        try { if (-not $process.HasExited) { $process.Kill() } }
        finally { $process.Dispose() }
        throw
    }
}

function Get-HaClosureRemaining($Context) {
    $remaining = [int][Math]::Floor(1000 * ($Context.DeadlineTimestamp - [Diagnostics.Stopwatch]::GetTimestamp()) /
                                   [Diagnostics.Stopwatch]::Frequency)
    if ($remaining -lt 1 -or $remaining -gt 1800) { throw 'lease_proof_callback_deadline' }
    return $remaining
}

function Get-HaLeaseProof($Context) {
    $Context.Progress.step = 'request'
    $request = $Context.Challenge
    $nativePin = if ('reactivation_plan_sha256' -cin $script:ClosureInput.PSObject.Properties.Name) {
        $script:ClosureInput.reactivation_plan_sha256
    } else { $request.plan_sha256 }
    $null = Get-HaClosureRemaining $Context
    if ($request.windows_boot_sha256 -cne $script:ClosureBoot -or
        $Context.MarkerSha256 -cne $script:ClosureMarker.sha256 -or $Context.ReactivationPlanSha256 -cne $nativePin) {
        throw 'lease_proof_callback_scope'
    }
    $challenge = Get-HaReactivationHash ($request.nonce + ':' + $request.plan_sha256 + ':' + $request.attempt_id)
    $payload = [ordered]@{
        runtime_path=$script:ClosureInput.runtime_path; runtime_sha256=$script:ClosureInput.runtime_sha256;
        plan=$request.plan_sha256; attempt=$request.attempt_id; wsl_boot_sha256=$request.wsl_boot_sha256;
        challenge=$challenge; budget_ms=(Get-HaClosureRemaining $Context)
    }
    $process = $script:ClosureProcess
    try {
        $Context.Progress.step = 'wsl_send'
        if ($null -eq $process -or $process.HasExited -or $script:ClosureStderr.IsCompleted) {
            throw 'lease_proof_wsl_unconfirmed'
        }
        $output = $process.StandardOutput.ReadLineAsync()
        $message = ConvertTo-Json -InputObject $payload -Compress
        $write = $process.StandardInput.WriteLineAsync($message)
        if (-not $write.Wait((Get-HaClosureRemaining $Context))) { throw 'lease_proof_callback_deadline' }
        # The collector only reads. Start it before the Windows checks so the
        # same challenge can observe both sides within one deadline.
        $Context.Progress.step = 'marker_before'
        Assert-HaOwnedCutoverMarker $script:ClosureMarker
        $marker = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($script:ClosureMarker.path))
        if ($marker.plan_sha256 -cne $request.plan_sha256 -or $marker.attempt_id -cne $request.attempt_id -or
            $marker.generation -ne $request.generation) { throw 'lease_proof_callback_scope' }
        $Context.Progress.step = 'wsl_running'
        $running = @(& $script:ClosureWsl --list --running --quiet | ForEach-Object { ($_ -replace ([char]0),'').Trim() })
        if ($LASTEXITCODE -ne 0 -or $script:ClosurePlan.distribution -cnotin $running) { throw 'lease_proof_wsl_not_running' }
        $Context.Progress.step = 'windows_closed'
        Assert-HaReactivationClosed $script:ClosurePlan
        $Context.Progress.step = 'hyperv'
        $vms = Get-FixedVmObserverSnapshot $script:ClosureInput.installation_id $script:ClosureVmA $script:ClosureVmB
        if ($vms.Count -ne 2 -or $vms[0].node -cne 'A' -or $vms[1].node -cne 'B' -or
            $vms[0].state -cne 'Running' -or $vms[1].state -cne 'Running' -or
            $vms[0].disk_paths_sha256 -cne $script:ClosureInput.a_disk_paths_sha256 -or
            $vms[1].disk_paths_sha256 -cne $script:ClosureInput.b_disk_paths_sha256) { throw 'lease_proof_vm_scope' }
        $Context.Progress.step = 'wsl_response'
        if (-not $output.Wait((Get-HaClosureRemaining $Context)) -or $process.HasExited -or
            $script:ClosureStderr.IsCompleted -or $output.Result -cnotmatch ('\A' + $challenge + ':[a-f0-9]{64}\z')) {
            throw 'lease_proof_wsl_unconfirmed'
        }
        $digest = $output.Result.Substring(65)
        $Context.Progress.step = 'marker_after'
        Assert-HaOwnedCutoverMarker $script:ClosureMarker
        $null = Get-HaClosureRemaining $Context
        $Context.Progress.step = 'complete'
        return [pscustomobject]@{
            nonce=$request.nonce; generation=$request.generation; marker_sha256=$Context.MarkerSha256;
            reactivation_plan_sha256=$Context.ReactivationPlanSha256; collector_sha256=$request.collector_sha256;
            windows_boot_sha256=$script:ClosureBoot; wsl_boot_sha256=$request.wsl_boot_sha256;
            observation_sha256=(Get-HaReactivationHash ($challenge + ':' + $digest + ':' + $Context.MarkerSha256 + ':' +
                (ConvertTo-Json -InputObject $vms -Compress)));
            recovery_commands_settled=$true; windows_reactivation_closed=$true; wsl_writers_closed=$true
        }
    } catch {
        $script:ClosureProcess = $null
        if ($null -ne $process) {
            try { if (-not $process.HasExited) { $process.Kill() } }
            finally { $process.Dispose() }
        }
        throw
    }
}
