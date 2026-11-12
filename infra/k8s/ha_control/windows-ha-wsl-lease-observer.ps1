# Read-only Windows/WSL proof in the already authenticated Windows broker.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Initialize-HaLeaseObservation($InputConfig, [string]$PlanText, [string]$MarkerSha, [string]$WindowsBootSha) {
    $fields = @($InputConfig.PSObject.Properties.Name | Sort-Object -CaseSensitive)
    if (($fields -join ',') -cne 'a_disk_paths_sha256,b_disk_paths_sha256,driver_path,driver_sha256,installation_id,reactivation_plan_path,runtime_path,runtime_sha256,source_root,vm_a,vm_b,vm_observer_path,vm_observer_sha256' -or
        $InputConfig.runtime_sha256 -cnotmatch '\A[a-f0-9]{64}\z' -or
        $InputConfig.installation_id -cnotmatch '\A[a-f0-9]{64}\z' -or
        $InputConfig.a_disk_paths_sha256 -cnotmatch '\A[a-f0-9]{64}\z' -or $InputConfig.b_disk_paths_sha256 -cnotmatch '\A[a-f0-9]{64}\z' -or
        $InputConfig.source_root -cnotmatch '\A/home/[a-zA-Z0-9_.-]+/\.local/state/n100-ha/cutover-source-[a-f0-9]{40}/infra/k8s\z' -or
        $InputConfig.runtime_path -cnotmatch '\A/home/[a-zA-Z0-9_.-]+/\.local/state/n100-ha/[a-zA-Z0-9_.-]+/runtime\.json\z' -or
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
    $script:ClosureInput = $InputConfig
    $script:ClosurePlan = $plan
    $script:ClosureMarker = $marker
    $script:ClosureBoot = $WindowsBootSha
    $script:ClosureWsl = Join-Path ([Environment]::SystemDirectory) 'wsl.exe'
}

function Get-HaClosureRemaining($Context) {
    $remaining = [int][Math]::Floor(1000 * ($Context.DeadlineTimestamp - [Diagnostics.Stopwatch]::GetTimestamp()) /
                                   [Diagnostics.Stopwatch]::Frequency)
    if ($remaining -lt 1 -or $remaining -gt 1800) { throw 'lease_proof_callback_deadline' }
    return $remaining
}

function Get-HaLeaseProof($Context) {
    $request = $Context.Challenge
    $null = Get-HaClosureRemaining $Context
    Assert-HaOwnedCutoverMarker $script:ClosureMarker
    $marker = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($script:ClosureMarker.path))
    if ($marker.plan_sha256 -cne $request.plan_sha256 -or $marker.attempt_id -cne $request.attempt_id -or
        $marker.generation -ne $request.generation -or $request.windows_boot_sha256 -cne $script:ClosureBoot -or
        $Context.MarkerSha256 -cne $script:ClosureMarker.sha256 -or $Context.ReactivationPlanSha256 -cne $request.plan_sha256) {
        throw 'lease_proof_callback_scope'
    }
    $running = @(& $script:ClosureWsl --list --running --quiet | ForEach-Object { ($_ -replace ([char]0),'').Trim() })
    if ($LASTEXITCODE -ne 0 -or $script:ClosurePlan.distribution -cnotin $running) { throw 'lease_proof_wsl_not_running' }
    $challenge = Get-HaReactivationHash ($request.nonce + ':' + $request.plan_sha256 + ':' + $request.attempt_id)
    $source = [string]$script:ClosureInput.source_root
    $payload = [ordered]@{
        runtime_path=$script:ClosureInput.runtime_path; runtime_sha256=$script:ClosureInput.runtime_sha256;
        plan=$request.plan_sha256; attempt=$request.attempt_id; wsl_boot_sha256=$request.wsl_boot_sha256;
        challenge=$challenge; budget_ms=(Get-HaClosureRemaining $Context)
    }
    $bootstrap = "import hashlib,pathlib,sys,types; p=pathlib.Path(sys.argv[1]); sys.path.insert(0,str(p)); b=(p/'ha_control/wsl_writer_proof.py').read_bytes(); assert hashlib.sha256(b).hexdigest()==sys.argv[2]; m=types.ModuleType('ha_control.wsl_writer_proof'); exec(compile(b,'wsl_writer_proof.py','exec'),m.__dict__); raise SystemExit(m.main())"
    $command = '--distribution Ubuntu-24.04 --exec /usr/bin/python3 -I -B -S -c "' + $bootstrap + '" ' + $source + ' ' + $request.collector_sha256
    $start = [Diagnostics.ProcessStartInfo]::new($script:ClosureWsl, $command)
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($start)
    try {
        $output = $process.StandardOutput.ReadLineAsync()
        $failure = $process.StandardError.ReadToEndAsync()
        $message = ConvertTo-Json -InputObject $payload -Compress
        $write = $process.StandardInput.WriteLineAsync($message)
        if (-not $write.Wait((Get-HaClosureRemaining $Context))) { throw 'lease_proof_callback_deadline' }
        $process.StandardInput.Close()
        Assert-HaReactivationClosed $script:ClosurePlan
        $vms = Get-FixedVmObserverSnapshot $script:ClosureInput.installation_id $script:ClosureVmA $script:ClosureVmB
        if ($vms.Count -ne 2 -or $vms[0].node -cne 'A' -or $vms[1].node -cne 'B' -or
            $vms[0].state -cne 'Running' -or $vms[1].state -cne 'Running' -or
            $vms[0].disk_paths_sha256 -cne $script:ClosureInput.a_disk_paths_sha256 -or
            $vms[1].disk_paths_sha256 -cne $script:ClosureInput.b_disk_paths_sha256) { throw 'lease_proof_vm_scope' }
        if (-not $process.WaitForExit((Get-HaClosureRemaining $Context)) -or $process.ExitCode -ne 0 -or
            -not $output.Wait((Get-HaClosureRemaining $Context)) -or $output.Result -cnotmatch '\A[a-f0-9]{64}\z' -or
            -not $failure.IsCompleted -or $failure.Result.Length -ne 0) { throw 'lease_proof_wsl_unconfirmed' }
        Assert-HaOwnedCutoverMarker $script:ClosureMarker
        $null = Get-HaClosureRemaining $Context
        return [pscustomobject]@{
            nonce=$request.nonce; generation=$request.generation; marker_sha256=$Context.MarkerSha256;
            reactivation_plan_sha256=$Context.ReactivationPlanSha256; collector_sha256=$request.collector_sha256;
            windows_boot_sha256=$script:ClosureBoot; wsl_boot_sha256=$request.wsl_boot_sha256;
            observation_sha256=(Get-HaReactivationHash ($challenge + ':' + $output.Result + ':' + $Context.MarkerSha256 + ':' +
                (ConvertTo-Json -InputObject $vms -Compress)));
            recovery_commands_settled=$true; windows_reactivation_closed=$true; wsl_writers_closed=$true
        }
    } finally {
        if (-not $process.HasExited) { $process.Kill() }
        $process.Dispose()
    }
}
