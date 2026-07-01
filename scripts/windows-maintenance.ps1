param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$PythonCommand = "python",
    [switch]$IncludeFiles
)

$ErrorActionPreference = "Stop"

$env:DATA_ROOT = Join-Path $ProjectRoot "data"
$env:BACKUP_PATH = Join-Path $env:DATA_ROOT "backups"
$env:SECURITY_LOG_PATH = Join-Path $env:DATA_ROOT "logs\security-events.txt"

if ($IncludeFiles) {
    $env:BACKUP_INCLUDE_FILES = "true"
}

Set-Location $ProjectRoot
& $PythonCommand ".\scripts\maintenance.py" "all"
