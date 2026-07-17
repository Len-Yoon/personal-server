param(
    [switch]$InstallTask,
    [switch]$Start,
    [switch]$Daemon,
    [string]$ProjectRoot = "C:\personal-server"
)

$ErrorActionPreference = "Stop"

$ScriptPath = Join-Path $ProjectRoot "scripts\windows-bootstrap.ps1"
$TaskName = "personal-server-autostart"
$WslDistribution = "Ubuntu-24.04"
function Write-Info([string]$Message) {
    Write-Host $Message
}

function Invoke-WslCommand {
    if ($ProjectRoot -match "^(?<drive>[A-Za-z]):[\\/](?<rest>.*)$") {
        $wslProjectRoot = "/mnt/$($Matches.drive.ToLower())/$($Matches.rest -replace '\\', '/')"
    } else {
        throw "Project path '$ProjectRoot' must be a Windows drive path such as C:\\personal-server."
    }

    & wsl.exe -d $WslDistribution bash -lc "cd '$wslProjectRoot' && bash scripts/windows-bootstrap.sh '$wslProjectRoot'"
    if ($LASTEXITCODE -ne 0) {
        throw "WSL command failed with exit code $LASTEXITCODE"
    }
}

function Start-CloudflareTunnel {
    & wsl.exe -d $WslDistribution bash -lc "pgrep -af '[c]loudflared.*tunnel run' >/dev/null"
    if ($LASTEXITCODE -eq 0) {
        return
    }

    Start-Process -FilePath 'wsl.exe' -WindowStyle Hidden -ArgumentList @(
        '-d', $WslDistribution,
        '--', 'cloudflared', 'tunnel', 'run'
    ) | Out-Null
}

function Update-HostMetrics {
    $systemDir = Join-Path $ProjectRoot "data\system"
    $metricsPath = Join-Path $systemDir "host-metrics.json"

    New-Item -ItemType Directory -Path $systemDir -Force | Out-Null

    $os = Get-CimInstance Win32_OperatingSystem
    $cpu = Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average
    $disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"

    $totalMemory = [double]$os.TotalVisibleMemorySize * 1KB
    $freeMemory = [double]$os.FreePhysicalMemory * 1KB
    $usedMemory = [Math]::Max([double]0, $totalMemory - $freeMemory)
    $memoryPercent = if ($totalMemory -gt 0) { [Math]::Round(($usedMemory / $totalMemory) * 100, 1) } else { 0 }
    $diskPercent = if ($disk.Size -gt 0) { [Math]::Round((($disk.Size - $disk.FreeSpace) / $disk.Size) * 100, 1) } else { 0 }
    $cpuPercent = if ($null -ne $cpu.Average) { [Math]::Round([double]$cpu.Average, 1) } else { 0 }

    $payload = [ordered]@{
        captured_at = (Get-Date).ToUniversalTime().ToString("o")
        cpu_percent = $cpuPercent
        memory_percent = $memoryPercent
        disk_percent = $diskPercent
        uptime_seconds = [Math]::Round(((Get-Date) - $os.LastBootUpTime).TotalSeconds)
    }

    $payload | ConvertTo-Json -Depth 3 | Set-Content -Path $metricsPath -Encoding utf8
    Write-Info "Updated host metrics at $metricsPath"
}

function Start-PersonalServerStack {
    for ($attempt = 1; $attempt -le 10; $attempt++) {
        try {
            Invoke-WslCommand
            Start-CloudflareTunnel
            Write-Info "Ensured Docker stack is up and Cloudflare Tunnel is running."
            return
        } catch {
            if ($attempt -eq 10) {
                throw
            }
            Write-Info "Startup attempt $attempt failed; retrying in 30 seconds."
            Start-Sleep -Seconds 30
        }
    }
}

function Install-ScheduledTask {
    $taskAction = "powershell.exe -WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" -Daemon"
    $runAsUser = "$env:USERDOMAIN\$env:USERNAME"
    Write-Info "Registering startup task for $runAsUser. Windows will prompt for the account password."
    & schtasks.exe /Create /TN $TaskName /SC ONSTART /RU $runAsUser /RP * /TR $taskAction /RL LIMITED /F
    $createExitCode = $LASTEXITCODE
    if ($createExitCode -ne 0) {
        $existingTask = (& schtasks.exe /Query /TN $TaskName /FO LIST /V 2>&1 | Out-String)
        if ($existingTask -match [regex]::Escape($ScriptPath)) {
            Write-Info "Scheduled task '$TaskName' already points to $ScriptPath."
            return
        }
        throw "Failed to register scheduled task '$TaskName' with schtasks.exe (exit code $createExitCode)."
    }
    if ($createOutput.Trim()) {
        Write-Info $createOutput.Trim()
    }
    Write-Info "Registered scheduled task '$TaskName' to start with Windows."
}

function Start-Daemon {
    Update-HostMetrics
    Write-Info "Waiting 120 seconds for WSL and Docker after logon."
    Start-Sleep -Seconds 120

    while ($true) {
        try {
            Update-HostMetrics
            Start-PersonalServerStack
        } catch {
            Write-Info "Recovery check failed: $($_.Exception.Message)"
        }
        Write-Info "Sleeping 30 minutes before the next recovery check."
        Start-Sleep -Seconds 1800
    }
}

if ($InstallTask) {
    Install-ScheduledTask
    exit 0
}

if ($Daemon) {
    Start-Daemon
    exit 0
}

if ($Start) {
    Update-HostMetrics
    Start-PersonalServerStack
    exit 0
}

throw "Use -InstallTask, -Start, or -Daemon."
