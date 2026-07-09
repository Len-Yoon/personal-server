param(
    [string]$OutputPath = ".\data\system\host-metrics.json"
)

$ErrorActionPreference = "Stop"

function Get-CounterValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$CounterPath
    )

    try {
        $sample = Get-Counter $CounterPath -ErrorAction Stop
        return [double]$sample.CounterSamples[0].CookedValue
    } catch {
        return $null
    }
}

function Get-SystemDriveMetrics {
    param(
        [Parameter(Mandatory = $true)]
        [string]$DriveLetter
    )

    try {
        $drive = [System.IO.DriveInfo]::GetDrives() |
            Where-Object { $_.Name.TrimEnd('\').TrimEnd(':') -eq $DriveLetter } |
            Select-Object -First 1

        if (-not $drive -or -not $drive.IsReady) {
            return @{
                name = "${DriveLetter}:"
                total_bytes = $null
                used_bytes = $null
                percent = $null
            }
        }

        $totalBytes = [double]$drive.TotalSize
        $freeBytes = [double]$drive.TotalFreeSpace
        $usedBytes = $totalBytes - $freeBytes

        if ($totalBytes -le 0) {
            return @{
                name = "${DriveLetter}:"
                total_bytes = $null
                used_bytes = $null
                percent = $null
            }
        }

        return @{
            name = "${DriveLetter}:"
            total_bytes = [int64]$totalBytes
            used_bytes = [int64]$usedBytes
            percent = [math]::Round(($usedBytes / $totalBytes) * 100, 1)
        }
    } catch {
        return @{
            name = "${DriveLetter}:"
            total_bytes = $null
            used_bytes = $null
            percent = $null
        }
    }
}

$outputDirectory = Split-Path -Parent $OutputPath
if ($outputDirectory -and -not (Test-Path $outputDirectory)) {
    New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null
}

$systemDriveLetter = $env:SystemDrive.TrimEnd(':')
$cpuPercent = Get-CounterValue '\Processor(_Total)\% Processor Time'
$memoryCommitted = Get-CounterValue '\Memory\Committed Bytes'
$memoryCommitLimit = Get-CounterValue '\Memory\Commit Limit'
$uptimeSecondsValue = Get-CounterValue '\System\System Up Time'
$memoryPercent = if ($memoryCommitted -ne $null -and $memoryCommitLimit -and $memoryCommitLimit -gt 0) {
    [math]::Round(($memoryCommitted / $memoryCommitLimit) * 100, 1)
} else {
    $null
}
$diskMetrics = Get-SystemDriveMetrics -DriveLetter $systemDriveLetter

$payload = [ordered]@{
    captured_at = (Get-Date).ToUniversalTime().ToString("o")
    source = "windows"
    computer_name = $env:COMPUTERNAME
    cpu_name = $env:PROCESSOR_IDENTIFIER
    cpu_percent = if ($cpuPercent -ne $null) { [math]::Round($cpuPercent, 1) } else { $null }
    memory_total_bytes = if ($memoryCommitLimit -ne $null) { [int64]$memoryCommitLimit } else { $null }
    memory_used_bytes = if ($memoryCommitted -ne $null) { [int64]$memoryCommitted } else { $null }
    memory_percent = $memoryPercent
    disk_name = $diskMetrics.name
    disk_total_bytes = $diskMetrics.total_bytes
    disk_used_bytes = $diskMetrics.used_bytes
    disk_percent = $diskMetrics.percent
    uptime_seconds = if ($uptimeSecondsValue -ne $null) { [int64][math]::Round($uptimeSecondsValue, 0) } else { $null }
}

$payload | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 -Path $OutputPath
