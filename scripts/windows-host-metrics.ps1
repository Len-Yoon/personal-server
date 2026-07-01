param(
    [string]$OutputPath = ".\data\system\host-metrics.json"
)

$ErrorActionPreference = "Stop"

$outputDirectory = Split-Path -Parent $OutputPath
if ($outputDirectory -and -not (Test-Path $outputDirectory)) {
    New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null
}

$os = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$systemDrive = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$($env:SystemDrive)'"

$totalMemory = [double]$os.TotalVisibleMemorySize * 1KB
$freeMemory = [double]$os.FreePhysicalMemory * 1KB
$usedMemory = $totalMemory - $freeMemory
$memoryPercent = if ($totalMemory -gt 0) { [math]::Round(($usedMemory / $totalMemory) * 100, 1) } else { 0 }

$diskPercent = if ($systemDrive.Size -gt 0) {
    [math]::Round((($systemDrive.Size - $systemDrive.FreeSpace) / $systemDrive.Size) * 100, 1)
} else {
    0
}

$lastBoot = $os.LastBootUpTime
$uptimeSeconds = [math]::Round(((Get-Date) - $lastBoot).TotalSeconds)

$payload = [ordered]@{
    captured_at = (Get-Date).ToUniversalTime().ToString("o")
    source = "windows"
    computer_name = $env:COMPUTERNAME
    cpu_name = $cpu.Name
    cpu_percent = [math]::Round((Get-Counter "\Processor(_Total)\% Processor Time").CounterSamples.CookedValue, 1)
    memory_total_bytes = [int64]$totalMemory
    memory_used_bytes = [int64]$usedMemory
    memory_percent = $memoryPercent
    disk_name = $systemDrive.DeviceID
    disk_total_bytes = [int64]$systemDrive.Size
    disk_used_bytes = [int64]($systemDrive.Size - $systemDrive.FreeSpace)
    disk_percent = $diskPercent
    uptime_seconds = [int64]$uptimeSeconds
}

$payload | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 -Path $OutputPath
