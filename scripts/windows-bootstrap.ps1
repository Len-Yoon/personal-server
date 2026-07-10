param(
    [switch]$InstallTask,
    [switch]$Start,
    [switch]$Daemon,
    [string]$ProjectRoot = "C:\personal-server"
)

$ErrorActionPreference = "Stop"

$ScriptPath = Join-Path $ProjectRoot "scripts\windows-bootstrap.ps1"
$TaskName = "personal-server-autostart"
function Write-Info([string]$Message) {
    Write-Host $Message
}

function Invoke-WslCommand {
    $wslProjectRoot = (& wsl.exe wslpath -a $ProjectRoot | Select-Object -First 1).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($wslProjectRoot)) {
        throw "Failed to convert project path '$ProjectRoot' to a WSL path."
    }

    & wsl.exe bash -lc "cd '$wslProjectRoot' && bash scripts/windows-bootstrap.sh"
    if ($LASTEXITCODE -ne 0) {
        throw "WSL command failed with exit code $LASTEXITCODE"
    }
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
    Invoke-WslCommand
    Write-Info "Ensured Docker stack is up and Cloudflare Tunnel is running."
}

function Install-ScheduledTask {
    try {
        $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" -Daemon"
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $trigger.Delay = "PT1M"
        $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew

        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
        Write-Info "Registered scheduled task '$TaskName' to start at logon with a 1-minute delay."
        return
    } catch {
        Write-Info "Scheduled task registration failed, using Startup folder fallback."
    }

    Install-StartupEntry
}

function Install-StartupEntry {
    $startupDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
    $startupScript = Join-Path $startupDir "personal-server-bootstrap.cmd"
    $content = @"
@echo off
powershell.exe -WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -File "$ScriptPath" -Daemon
"@

    New-Item -ItemType Directory -Path $startupDir -Force | Out-Null
    Set-Content -Path $startupScript -Value $content -Encoding ASCII
    Write-Info "Installed Startup folder entry at $startupScript."
}

function Start-Daemon {
    Update-HostMetrics
    Write-Info "Waiting 60 seconds before the first startup sync."
    Start-Sleep -Seconds 60

    while ($true) {
        Update-HostMetrics
        Start-PersonalServerStack
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
