param(
    [switch]$InstallTask,
    [switch]$Start,
    [switch]$Watchdog,
    [switch]$Daemon,
    [string]$ProjectRoot = "C:\personal-server"
)

$ErrorActionPreference = "Stop"

$ScriptPath = Join-Path $ProjectRoot "scripts\windows-bootstrap.ps1"
$TaskName = "personal-server-autostart"
function Write-Info([string]$Message) {
    Write-Host $Message
}

function Invoke-WslCommand([string]$Command) {
    & wsl.exe bash /mnt/c/personal-server/scripts/windows-bootstrap.sh
    if ($LASTEXITCODE -ne 0) {
        throw "WSL command failed with exit code $LASTEXITCODE"
    }
}

function Start-PersonalServerStack {
    Invoke-WslCommand ""
    Write-Info "Ensured Docker stack is up and Cloudflare Tunnel is running."
}

function Install-ScheduledTask {
    $startupAction = "powershell.exe -WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" -Daemon"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" -Daemon"
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $trigger.Delay = "PT1M"
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

    Write-Info "Registered scheduled task '$TaskName' to start at logon with a 1-minute delay."
}

function Start-Daemon {
    Write-Info "Waiting 60 seconds before the first startup sync."
    Start-Sleep -Seconds 60

    while ($true) {
        Start-PersonalServerStack
        Write-Info "Sleeping 10 minutes before the next recovery check."
        Start-Sleep -Seconds 600
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
    Start-PersonalServerStack
    exit 0
}

if ($Watchdog) {
    Start-PersonalServerStack
    exit 0
}

throw "Use -InstallTask, -Start, or -Watchdog."
