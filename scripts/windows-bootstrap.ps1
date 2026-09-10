param(
    [switch]$InstallTask,
    [switch]$Start,
    [switch]$Supervisor,
    [switch]$Daemon,
    [string]$ProjectRoot = "C:\personal-server"
)

$ErrorActionPreference = "Stop"

$ScriptPath = Join-Path $ProjectRoot "scripts\windows-bootstrap.ps1"
$TaskName = "personal-server-autostart"
$WslDistribution = "Ubuntu-24.04"
$WslServiceUser = "window"
$CloudflareTunnelService = "cloudflared-personal-server.service"
$TunnelTelegramCredentialTarget = "personal-server-tunnel-telegram"
$CaddyContainerName = "personal-server-caddy-1"
$RecoveryIntervalSeconds = 180
$RecoveryFailureThreshold = 2
$RecoveryMaxAttempts = 3
$RecoveryCommandTimeoutSeconds = 20
$EmergencyRebootTaskName = "PersonalServer-EmergencyReboot"
$EmergencyRebootGraceSeconds = 1200
$EmergencyRebootCooldownSeconds = 21600
$RecoveryStateDirectory = Join-Path $ProjectRoot "data"
$RecoveryStatePath = Join-Path $ProjectRoot "data\recovery-state.json"
$RecoveryEventLogPath = Join-Path $RecoveryStateDirectory "recovery-events.jsonl"
$RecoveryEventLogMaxEntries = 200
$RecoveryLockPath = Join-Path $RecoveryStateDirectory "recovery.lock"
$SupervisorLockPath = Join-Path $RecoveryStateDirectory "supervisor.lock"
$DaemonLockPath = Join-Path $RecoveryStateDirectory "daemon.lock"
$DaemonRestartDelaySeconds = 15
$DaemonFailureBackoffSeconds = 60
$DaemonRapidFailureWindowSeconds = 60
$DaemonRapidFailureLimit = 3
$RecoveryComponents = @("keepalive", "k3s", "portal", "nodeport", "tunnel")
$RecoveryFailureCounts = @{}
$RecoveryAttemptCounts = @{}
$RecoveryStateDirty = $false
$EmergencyRebootLastAt = $null
$EmergencyRebootCauseComponent = $null
$EmergencyRebootCooldownStateValid = $true
$TunnelAlertDownNotified = $false
function Write-Info([string]$Message) {
    Write-Host $Message
}

function ConvertTo-WslArgument([string]$Argument) {
    if ($Argument -notmatch '[\s"]') {
        return $Argument
    }
    return '"' + $Argument.Replace('"', '\"') + '"'
}

function Invoke-WslWithTimeout([string[]]$Arguments, [string]$Operation) {
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = "wsl.exe"
    $startInfo.Arguments = (($Arguments | ForEach-Object { ConvertTo-WslArgument ([string]$_) }) -join " ")
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        Write-Info "$Operation could not start."
        return $false
    }
    if (-not $process.WaitForExit($RecoveryCommandTimeoutSeconds * 1000)) {
        try {
            $process.Kill()
        } catch {
            Write-Info "$Operation timed out and could not be terminated."
        }
        Write-Info "$Operation timed out after $RecoveryCommandTimeoutSeconds seconds."
        return $false
    }
    if ($process.ExitCode -ne 0) {
        Write-Info "$Operation failed with exit code $($process.ExitCode)."
        return $false
    }
    return $true
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

function Get-TunnelTelegramConfiguration {
    try {
        if ($null -ne ("PersonalServer.CredentialReader" -as [type])) {
            $credential = [PersonalServer.CredentialReader]::ReadGeneric($TunnelTelegramCredentialTarget)
        } else {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace PersonalServer {
    public sealed class GenericCredential {
        public string UserName { get; private set; }
        public string Password { get; private set; }

        public GenericCredential(string userName, string password) {
            UserName = userName;
            Password = password;
        }
    }

    public static class CredentialReader {
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct Credential {
            public uint Flags;
            public uint Type;
            public string TargetName;
            public string Comment;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
            public uint CredentialBlobSize;
            public IntPtr CredentialBlob;
            public uint Persist;
            public uint AttributeCount;
            public IntPtr Attributes;
            public string TargetAlias;
            public string UserName;
        }

        [DllImport("Advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool CredRead(string target, uint type, uint flags, out IntPtr credential);

        [DllImport("Advapi32.dll", SetLastError = true)]
        private static extern void CredFree(IntPtr buffer);

        public static GenericCredential ReadGeneric(string target) {
            IntPtr pointer;
            if (!CredRead(target, 1, 0, out pointer)) return null;
            try {
                var credential = (Credential)Marshal.PtrToStructure(pointer, typeof(Credential));
                if (credential.CredentialBlob == IntPtr.Zero || credential.CredentialBlobSize == 0 || credential.CredentialBlobSize % 2 != 0) return null;
                return new GenericCredential(
                    credential.UserName,
                    Marshal.PtrToStringUni(credential.CredentialBlob, (int)credential.CredentialBlobSize / 2)
                );
            } finally {
                CredFree(pointer);
            }
        }
    }
}
'@
            $credential = [PersonalServer.CredentialReader]::ReadGeneric($TunnelTelegramCredentialTarget)
        }
    } catch {
        Write-Info "Tunnel Telegram credential is unavailable."
        return $null
    }
    if ($null -eq $credential -or [string]::IsNullOrWhiteSpace([string]$credential.Password)) {
        Write-Info "Tunnel Telegram credential is unavailable."
        return $null
    }

    if (([string]$credential.Password).TrimStart().StartsWith("{")) {
        try {
            $configuration = $credential.Password | ConvertFrom-Json
            if ($null -eq $configuration -or [string]::IsNullOrWhiteSpace([string]$configuration.bot_token) -or [string]::IsNullOrWhiteSpace([string]$configuration.chat_id)) {
                Write-Info "Tunnel Telegram credential has an invalid format."
                return $null
            }
            return $configuration
        } catch {
            Write-Info "Tunnel Telegram credential has an invalid format."
            return $null
        }
    }

    if ([string]::IsNullOrWhiteSpace([string]$credential.UserName)) {
        Write-Info "Tunnel Telegram credential is unavailable."
        return $null
    }

    return [pscustomobject]@{
        bot_token = [string]$credential.Password
        chat_id = [string]$credential.UserName
    }
}

function Send-TunnelTelegramNotification([string]$Transition) {
    $configuration = Get-TunnelTelegramConfiguration
    if ($null -eq $configuration) {
        return $false
    }
    $message = switch ($Transition) {
        "down" { "[개인서버 장애] Cloudflare Tunnel 연결 실패를 확인했습니다. 자동복구를 진행합니다." }
        "recovered" { "[개인서버 복구] Cloudflare Tunnel 연결이 정상 복구되었습니다." }
        default { return $false }
    }
    try {
        $payload = @{
            chat_id = [string]$configuration.chat_id
            text = $message
        } | ConvertTo-Json -Compress
        $payloadBytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
        $response = Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$($configuration.bot_token)/sendMessage" -ContentType "application/json; charset=utf-8" -Body $payloadBytes -TimeoutSec $RecoveryCommandTimeoutSeconds -ErrorAction Stop
        if ($null -eq $response -or $response.ok -ne $true -or $null -eq $response.result -or $null -eq $response.result.message_id) {
            Write-Info "Tunnel Telegram delivery was not accepted."
            return $false
        }
        return $true
    } catch {
        Write-Info "Tunnel Telegram delivery failed."
        return $false
    }
}

function Update-TunnelTelegramNotification([string]$TunnelHealth) {
    if ($TunnelHealth -eq "deferred") {
        return
    }
    if ($TunnelHealth -eq "healthy") {
        if (-not $TunnelAlertDownNotified) {
            return
        }
        if (Send-TunnelTelegramNotification "recovered") {
            $script:TunnelAlertDownNotified = $false
            [void](Save-RecoveryFailureState)
            Write-RecoveryEvent -Component "tunnel" -Event "alert_recovered" -Status "sent" -Action "notify_telegram"
        } else {
            Write-RecoveryEvent -Component "tunnel" -Event "alert_recovered" -Status "failed" -Action "notify_telegram"
        }
        return
    }
    if ($TunnelAlertDownNotified) {
        return
    }
    if (Send-TunnelTelegramNotification "down") {
        $script:TunnelAlertDownNotified = $true
        [void](Save-RecoveryFailureState)
        Write-RecoveryEvent -Component "tunnel" -Event "alert_down" -Status "sent" -Action "notify_telegram"
    } else {
        Write-RecoveryEvent -Component "tunnel" -Event "alert_down" -Status "failed" -Action "notify_telegram"
    }
}

function Test-CloudflareTunnelRunning {
    return (Invoke-WslWithTimeout -Arguments @("-d", $WslDistribution, "bash", "-lc", "pgrep -af '[c]loudflared.*tunnel run' >/dev/null") -Operation "Cloudflare Tunnel probe")
}

function Test-CloudflareTunnelService {
    return (Invoke-WslWithTimeout -Arguments @(
        "-d", $WslDistribution, "-u", $WslServiceUser, "--",
        "bash", "-lc", "systemctl --user is-active --quiet '$CloudflareTunnelService'"
    ) -Operation "Cloudflare Tunnel service probe")
}

function Test-PublicPortalHealth {
    return (Invoke-WslWithTimeout -Arguments @(
        "-d", $WslDistribution, "--",
        "curl", "--fail", "--silent", "--show-error", "--max-time", "15", "https://len.pe.kr/health"
    ) -Operation "Public Portal health probe")
}

function Start-CloudflareTunnel([switch]$ForceRestart) {
    if (Test-CloudflareTunnelService) {
        if (Test-CloudflareTunnelRunning) {
            if (-not $ForceRestart) {
                return $false
            }
        }
        if (-not (Invoke-WslWithTimeout -Arguments @(
            "-d", $WslDistribution, "-u", $WslServiceUser, "--",
            "bash", "-lc", "systemctl --user restart '$CloudflareTunnelService'"
        ) -Operation "Cloudflare Tunnel service restart")) { return $false }
        return ((Test-CloudflareTunnelService) -and (Test-CloudflareTunnelRunning))
    }
    if (-not (Invoke-WslWithTimeout -Arguments @(
        "-d", $WslDistribution, "-u", $WslServiceUser, "--",
        "bash", "-lc", "systemctl --user start '$CloudflareTunnelService'"
    ) -Operation "Cloudflare Tunnel service start")) { return $false }
    return ((Test-CloudflareTunnelService) -and (Test-CloudflareTunnelRunning))
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
            [void](Start-CloudflareTunnel)
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

function Get-RecoveryHealth {
    $health = [ordered]@{
        keepalive = "unhealthy"
        k3s = "unhealthy"
        portal = "deferred"
        nodeport = "unhealthy"
        tunnel = "deferred"
    }

    $keepAliveTask = Get-ScheduledTask -TaskName "PersonalServer-WSL-KeepAlive" -ErrorAction SilentlyContinue
    if ($null -ne $keepAliveTask -and $keepAliveTask.State -eq "Running" -and (Invoke-WslWithTimeout -Arguments @("-d", $WslDistribution, "--", "true") -Operation "WSL KeepAlive probe")) {
        $health.keepalive = "healthy"
    }

    if (Invoke-WslWithTimeout -Arguments @("-d", $WslDistribution, "-u", "root", "--", "systemctl", "is-active", "--quiet", "k3s") -Operation "K3s service probe") {
        if (Invoke-WslWithTimeout -Arguments @("-d", $WslDistribution, "-u", "root", "--", "k3s", "kubectl", "get", "--raw=/readyz") -Operation "K3s API probe") {
            $health.k3s = "healthy"
        }
    }

    if ($health.k3s -eq "healthy") {
        $health.portal = "unhealthy"
        if (Invoke-WslWithTimeout -Arguments @("-d", $WslDistribution, "-u", "root", "--", "k3s", "kubectl", "-n", "personal-server", "wait", "--for=condition=Available", "--timeout=1s", "deployment/portal-web") -Operation "Portal availability probe") {
            $health.portal = "healthy"
        }
    }

    if (Invoke-WslWithTimeout -Arguments @("-d", $WslDistribution, "--", "docker", "exec", $CaddyContainerName, "curl", "--fail", "--silent", "--show-error", "--max-time", "10", "http://host.docker.internal:30080/health") -Operation "Portal NodePort probe") {
        $health.nodeport = "healthy"
    }

    if ($health.nodeport -eq "healthy") {
        $health.tunnel = "unhealthy"
        if ((Test-CloudflareTunnelService) -and (Test-CloudflareTunnelRunning) -and (Test-PublicPortalHealth)) {
            $health.tunnel = "healthy"
        }
    }

    return $health
}

function Write-RecoveryEvent(
    [string]$Component,
    [string]$Event,
    [string]$Status,
    [string]$Action = "none"
) {
    $temporaryPath = $null
    try {
        New-Item -ItemType Directory -Path $RecoveryStateDirectory -Force | Out-Null
        $existingEntries = @()
        if (Test-Path -LiteralPath $RecoveryEventLogPath) {
            $existingEntries = @(Get-Content -LiteralPath $RecoveryEventLogPath -ErrorAction Stop | Where-Object {
                -not [string]::IsNullOrWhiteSpace($_)
            })
        }
        if ($existingEntries.Count -ge $RecoveryEventLogMaxEntries) {
            $existingEntries = @($existingEntries | Select-Object -Last ($RecoveryEventLogMaxEntries - 1))
        }
        $entry = [ordered]@{
            timestamp = (Get-Date).ToUniversalTime().ToString("o")
            component = $Component
            event = $Event
            status = $Status
            action = $Action
        } | ConvertTo-Json -Compress
        $temporaryPath = Join-Path $RecoveryStateDirectory "recovery-events.$PID.tmp"
        $entriesToWrite = @($existingEntries + $entry)
        [System.IO.File]::WriteAllLines(
            $temporaryPath,
            [string[]]$entriesToWrite,
            (New-Object System.Text.UTF8Encoding($false))
        )
        Move-Item -LiteralPath $temporaryPath -Destination $RecoveryEventLogPath -Force
    } catch {
        Write-Info "Recovery event logging failed."
    } finally {
        if ($null -ne $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
    }
}

function Save-RecoveryFailureState {
    try {
        New-Item -ItemType Directory -Path $RecoveryStateDirectory -Force | Out-Null
        $state = [ordered]@{}
        foreach ($component in $RecoveryComponents) {
            $failureCount = if ($RecoveryFailureCounts.ContainsKey($component)) { [int]$RecoveryFailureCounts[$component] } else { 0 }
            $attemptCount = if ($RecoveryAttemptCounts.ContainsKey($component)) { [int]$RecoveryAttemptCounts[$component] } else { 0 }
            $state[$component] = [ordered]@{
                health_failures = $failureCount
                recovery_attempts = $attemptCount
            }
        }
        $state.emergency_reboot = [ordered]@{
            last_emergency_reboot_at = $EmergencyRebootLastAt
            cause_component = $EmergencyRebootCauseComponent
        }
        $state.tunnel_alert = [ordered]@{
            down_notified = $TunnelAlertDownNotified
        }
        $state | ConvertTo-Json -Depth 3 | Set-Content -Path $RecoveryStatePath -Encoding utf8
        $script:RecoveryStateDirty = $false
        return $true
    } catch {
        Write-Info "Recovery state could not be saved; continuing without state details."
        $script:RecoveryStateDirty = $true
        return $false
    }
}

function Set-RecoveryStateInvalid {
    $script:EmergencyRebootCooldownStateValid = $false
    $script:RecoveryStateDirty = $true
}

function Load-RecoveryFailureState {
    if (-not (Test-Path -LiteralPath $RecoveryStatePath)) {
        return
    }
    try {
        $storedState = Get-Content -LiteralPath $RecoveryStatePath -Raw | ConvertFrom-Json
        if ($storedState -isnot [PSCustomObject]) {
            Set-RecoveryStateInvalid
            return
        }
        $legacyRecoveryState = $false
        $storedEmergencyReboot = $storedState.PSObject.Properties["emergency_reboot"]
        if ($null -eq $storedEmergencyReboot) {
            # State files written before emergency reboot support have no cooldown metadata.
            # Migrate only after all component counters have passed the existing validation.
            $legacyRecoveryState = $true
            Write-Info "Legacy recovery state is missing emergency reboot metadata; migrating."
        } elseif ($null -eq $storedEmergencyReboot.Value -or $storedEmergencyReboot.Value -isnot [PSCustomObject]) {
            Set-RecoveryStateInvalid
        } else {
            $storedLastReboot = $storedEmergencyReboot.Value.PSObject.Properties["last_emergency_reboot_at"]
            $storedCauseComponent = $storedEmergencyReboot.Value.PSObject.Properties["cause_component"]
            if ($null -eq $storedLastReboot -or $null -eq $storedCauseComponent) {
                Set-RecoveryStateInvalid
            } elseif ($null -eq $storedLastReboot.Value -and $null -eq $storedCauseComponent.Value) {
                # A normal saved state has no prior emergency reboot yet.
            } elseif ($null -eq $storedLastReboot.Value -or [string]::IsNullOrWhiteSpace([string]$storedLastReboot.Value) -or $null -eq $storedCauseComponent.Value -or [string]$storedCauseComponent.Value -notin @("keepalive", "k3s")) {
                Set-RecoveryStateInvalid
            } else {
                $script:EmergencyRebootLastAt = [string]$storedLastReboot.Value
                $script:EmergencyRebootCauseComponent = [string]$storedCauseComponent.Value
                $parsedLastReboot = [DateTimeOffset]::MinValue
                if (-not [DateTimeOffset]::TryParseExact([string]$storedLastReboot.Value, "o", [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::RoundtripKind, [ref]$parsedLastReboot) -or $parsedLastReboot.Offset -ne [TimeSpan]::Zero) {
                    Set-RecoveryStateInvalid
                }
            }
        }
        $storedTunnelAlert = $storedState.PSObject.Properties["tunnel_alert"]
        if ($null -eq $storedTunnelAlert) {
            $legacyRecoveryState = $true
        } elseif ($null -eq $storedTunnelAlert.Value -or $storedTunnelAlert.Value -isnot [PSCustomObject]) {
            Set-RecoveryStateInvalid
        } else {
            $storedDownNotified = $storedTunnelAlert.Value.PSObject.Properties["down_notified"]
            if ($null -eq $storedDownNotified -or $storedDownNotified.Value -isnot [bool]) {
                Set-RecoveryStateInvalid
            } else {
                $script:TunnelAlertDownNotified = [bool]$storedDownNotified.Value
            }
        }
        foreach ($component in $RecoveryComponents) {
            $storedComponent = $storedState.PSObject.Properties[$component]
            $failureCount = 0
            $attemptCount = 0
            if ($null -eq $storedComponent -or $null -eq $storedComponent.Value -or $storedComponent.Value -isnot [PSCustomObject]) {
                Set-RecoveryStateInvalid
                if ($null -ne $storedComponent -and [int]::TryParse([string]$storedComponent.Value, [ref]$failureCount) -and $failureCount -ge 0) {
                    $RecoveryFailureCounts[$component] = $failureCount
                    $RecoveryAttemptCounts[$component] = 0
                }
                continue
            }
            $storedFailureCount = $storedComponent.Value.PSObject.Properties["health_failures"]
            $storedAttemptCount = $storedComponent.Value.PSObject.Properties["recovery_attempts"]
            if ($null -eq $storedFailureCount -or $null -eq $storedAttemptCount -or -not [int]::TryParse([string]$storedFailureCount.Value, [ref]$failureCount) -or $failureCount -lt 0 -or -not [int]::TryParse([string]$storedAttemptCount.Value, [ref]$attemptCount) -or $attemptCount -lt 0) {
                Set-RecoveryStateInvalid
                continue
            }
            $RecoveryFailureCounts[$component] = $failureCount
            $RecoveryAttemptCounts[$component] = $attemptCount
        }
        if ($legacyRecoveryState -and -not $RecoveryStateDirty) {
            [void](Save-RecoveryFailureState)
        }
    } catch {
        $RecoveryFailureCounts = @{}
        $RecoveryAttemptCounts = @{}
        Set-RecoveryStateInvalid
    }
}

function Register-RecoveryFailure([string]$Component) {
    $failureCount = 0
    if ($RecoveryFailureCounts.ContainsKey($Component)) {
        $failureCount = [int]$RecoveryFailureCounts[$Component]
    }
    $failureCount += 1
    $RecoveryFailureCounts[$Component] = $failureCount
    Write-RecoveryEvent -Component $Component -Event "unhealthy_detected" -Status "unhealthy" -Action "none"
    [void](Save-RecoveryFailureState)
    if ($failureCount -lt $RecoveryFailureThreshold) {
        Write-Info "$Component recovery failure $failureCount/$RecoveryFailureThreshold."
    }
    return $failureCount
}

function Reset-RecoveryFailure([string]$Component) {
    $failureCount = if ($RecoveryFailureCounts.ContainsKey($Component)) { [int]$RecoveryFailureCounts[$Component] } else { 0 }
    $attemptCount = if ($RecoveryAttemptCounts.ContainsKey($Component)) { [int]$RecoveryAttemptCounts[$Component] } else { 0 }
    $RecoveryFailureCounts[$Component] = 0
    $RecoveryAttemptCounts[$Component] = 0
    if ($failureCount -ne 0 -or $attemptCount -ne 0) {
        Write-RecoveryEvent -Component $Component -Event "health_restored" -Status "healthy" -Action "none"
        [void](Save-RecoveryFailureState)
    }
}

function Test-RecoveryAttemptAvailable([string]$Component) {
    $attemptCount = if ($RecoveryAttemptCounts.ContainsKey($Component)) { [int]$RecoveryAttemptCounts[$Component] } else { 0 }
    if ($attemptCount -ge $RecoveryMaxAttempts) {
        Write-Info "$Component recovery limit reached; waiting for external status handling."
        return $false
    }
    return $true
}

function Register-RecoveryAttempt([string]$Component) {
    $attemptCount = if ($RecoveryAttemptCounts.ContainsKey($Component)) { [int]$RecoveryAttemptCounts[$Component] } else { 0 }
    if ($attemptCount -ge $RecoveryMaxAttempts) {
        Write-Info "$Component recovery limit reached; waiting for external status handling."
        return $false
    }
    $RecoveryAttemptCounts[$Component] = $attemptCount + 1
    [void](Save-RecoveryFailureState)
    return $true
}

function Get-RecoveryActionName([string]$Component) {
    switch ($Component) {
        "keepalive" { return "start_keepalive" }
        "k3s" { return "start_k3s" }
        "portal" { return "rollout_restart_portal" }
        "tunnel" { return "restart_tunnel" }
        default { return "none" }
    }
}

function Test-RecoveryActionNeeded([string]$Component) {
    switch ($Component) {
        "keepalive" {
            $keepAliveTask = Get-ScheduledTask -TaskName "PersonalServer-WSL-KeepAlive" -ErrorAction SilentlyContinue
            return ($null -eq $keepAliveTask -or $keepAliveTask.State -ne "Running")
        }
        "k3s" {
            return (-not (Invoke-WslWithTimeout -Arguments @("-d", $WslDistribution, "-u", "root", "--", "systemctl", "is-active", "--quiet", "k3s") -Operation "K3s recovery service check"))
        }
        "portal" {
            return $true
        }
        "nodeport" {
            Write-Info "NodePort is unhealthy; deferring to the next recovery cycle."
            return $false
        }
        "tunnel" {
            return $true
        }
    }
    return $false
}

function Invoke-TargetedRecovery([string]$Component) {
    Write-Info "Targeted recovery requested for $Component."
    switch ($Component) {
        "keepalive" {
            Start-ScheduledTask -TaskName "PersonalServer-WSL-KeepAlive"
            $health = Get-RecoveryHealth
            return ($health.keepalive -eq "healthy")
        }
        "k3s" {
            if (-not (Invoke-WslWithTimeout -Arguments @("-d", $WslDistribution, "-u", "root", "--", "systemctl", "start", "k3s") -Operation "K3s recovery start")) {
                return $false
            }
            $health = Get-RecoveryHealth
            return ($health.k3s -eq "healthy")
        }
        "portal" {
            [void](Invoke-WslWithTimeout -Arguments @("-d", $WslDistribution, "-u", "root", "--", "k3s", "kubectl", "-n", "personal-server", "rollout", "restart", "deployment/portal-web") -Operation "Portal rollout restart")
            return $true
        }
        "nodeport" {
            Write-Info "NodePort is unhealthy; deferring to the next recovery cycle."
            return $false
        }
        "tunnel" {
            return (Start-CloudflareTunnel -ForceRestart)
        }
    }
    return $false
}

function Enter-RecoveryLock {
    New-Item -ItemType Directory -Path $RecoveryStateDirectory -Force | Out-Null
    try {
        return [System.IO.File]::Open(
            $RecoveryLockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
    } catch [System.IO.IOException] {
        return $null
    }
}

function Exit-RecoveryLock($LockStream) {
    if ($null -eq $LockStream) {
        return
    }
    try {
        $LockStream.Dispose()
    } finally {
        Remove-Item -LiteralPath $RecoveryLockPath -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-RecoveryCycle {
    $lockStream = Enter-RecoveryLock
    if ($null -eq $lockStream) {
        Write-Info "Recovery cycle is already running; skipping this interval."
        return
    }

    try {
        if (-not $RecoveryStateDirty) {
            Load-RecoveryFailureState
        }
        $health = Get-RecoveryHealth
        if ($RecoveryStateDirty) {
            Write-Info "Recovery state is unsaved; retaining in-memory counters and blocking automated recovery until restart or operator action."
            return
        }
        Update-TunnelTelegramNotification $health.tunnel
        if ($RecoveryStateDirty) {
            Write-Info "Recovery state is unsaved; skipping automated recovery actions this cycle."
            return
        }
        foreach ($component in $RecoveryComponents) {
            if ($health[$component] -eq "healthy") {
                Reset-RecoveryFailure $component
                continue
            }
            if ($health[$component] -eq "deferred") {
                Write-Info "$component recovery is deferred because a required local dependency is not healthy."
                continue
            }

            $failureCount = Register-RecoveryFailure $component
            if ($RecoveryStateDirty) {
                Write-Info "Recovery state is unsaved; skipping automated recovery actions this cycle."
                continue
            }
            if ($failureCount -lt $RecoveryFailureThreshold) {
                continue
            }

            if ($component -eq "portal" -and $health.k3s -ne "healthy") {
                Write-Info "Portal restart deferred until the K3s API is healthy."
                continue
            }
            if (-not (Test-RecoveryActionNeeded $component)) {
                continue
            }
            if (-not (Test-RecoveryAttemptAvailable $component)) {
                continue
            }
            if (-not (Register-RecoveryAttempt $component)) {
                continue
            }
            if ($RecoveryStateDirty) {
                Write-Info "Recovery state is unsaved; skipping automated recovery actions this cycle."
                continue
            }
            $targetedRecoverySucceeded = $false
            $recoveryAction = Get-RecoveryActionName $component
            Write-RecoveryEvent -Component $component -Event "recovery_dispatch" -Status "requested" -Action $recoveryAction
            try {
                $targetedRecoverySucceeded = Invoke-TargetedRecovery $component
            } catch {
                Write-Info "$component targeted recovery failed: $($_.Exception.Message)"
            }
            if ($targetedRecoverySucceeded) {
                Write-RecoveryEvent -Component $component -Event "recovery_dispatch" -Status "accepted" -Action $recoveryAction
            } else {
                Write-RecoveryEvent -Component $component -Event "recovery_dispatch" -Status "failed" -Action $recoveryAction
            }
            if (-not $targetedRecoverySucceeded) {
                if ($component -notin @("keepalive", "k3s")) {
                    continue
                }
                $finalHealth = Get-RecoveryHealth
                if ($finalHealth[$component] -ne "unhealthy") {
                    continue
                }
                if (Test-EmergencyRebootEligible $component) {
                    if (Request-EmergencyReboot $component) {
                        return
                    }
                }
            }
        }
    } finally {
        Exit-RecoveryLock $lockStream
    }
}

function Install-EmergencyRebootTask {
    $command = 'shutdown.exe /r /f /t 60'
    $temporaryTaskXml = Join-Path $env:TEMP "$EmergencyRebootTaskName.xml"
    $taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <URI>\PersonalServer-EmergencyReboot</URI>
    <Description>Emergency reboot task for exhausted core recovery attempts.</Description>
  </RegistrationInfo>
  <Principals>
    <Principal id="SYSTEM">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>true</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>true</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="SYSTEM">
    <Exec>
      <Command>shutdown.exe</Command>
      <Arguments>/r /f /t 60</Arguments>
    </Exec>
  </Actions>
</Task>
"@
    try {
        Set-Content -LiteralPath $temporaryTaskXml -Value $taskXml -Encoding unicode
        $previousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $result = & schtasks.exe /Create /TN $EmergencyRebootTaskName /XML $temporaryTaskXml /F 2>&1 | Out-String
            $createExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        if ($createExitCode -ne 0) {
            throw "Failed to register emergency reboot task (exit code $createExitCode): $($result.Trim())"
        }
    } finally {
        Remove-Item -LiteralPath $temporaryTaskXml -Force -ErrorAction SilentlyContinue
    }
}

function Test-EmergencyRebootEligible([string]$Component) {
    if ($Component -notin @("keepalive", "k3s")) {
        return $false
    }
    if ($RecoveryStateDirty) {
        Write-Info "Emergency reboot blocked because recovery state is unsaved."
        return $false
    }

    $attemptCount = if ($RecoveryAttemptCounts.ContainsKey($Component)) { [int]$RecoveryAttemptCounts[$Component] } else { 0 }
    if ($attemptCount -ne $RecoveryMaxAttempts) {
        return $false
    }

    $operatingSystem = Get-CimInstance Win32_OperatingSystem
    $bootAgeSeconds = ((Get-Date).ToUniversalTime() - $operatingSystem.LastBootUpTime.ToUniversalTime()).TotalSeconds
    if ($bootAgeSeconds -lt $EmergencyRebootGraceSeconds) {
        Write-Info "Emergency reboot blocked during the post-boot grace period."
        return $false
    }

    if (-not $EmergencyRebootCooldownStateValid) {
        Write-Info "Emergency reboot blocked because the persisted cooldown timestamp is invalid."
        return $false
    }
    if ($null -ne $EmergencyRebootLastAt) {
        $lastRebootAt = [DateTimeOffset]::MinValue
        if (-not [DateTimeOffset]::TryParse($EmergencyRebootLastAt, [ref]$lastRebootAt)) {
            Write-Info "Emergency reboot blocked because the persisted cooldown timestamp is invalid."
            return $false
        }
        $cooldownAgeSeconds = ((Get-Date).ToUniversalTime() - $lastRebootAt.UtcDateTime).TotalSeconds
        if ($cooldownAgeSeconds -lt $EmergencyRebootCooldownSeconds) {
            Write-Info "Emergency reboot blocked during the cooldown period."
            return $false
        }
    }
    return $true
}

function Request-EmergencyReboot([string]$Component) {
    if ($Component -notin @("keepalive", "k3s")) {
        return $false
    }
    try {
        Get-ScheduledTask -TaskName $EmergencyRebootTaskName -ErrorAction Stop | Out-Null
    } catch {
        Write-Info "Emergency reboot blocked because its scheduled task is unavailable."
        return $false
    }
    $previousEmergencyRebootLastAt = $EmergencyRebootLastAt
    $previousEmergencyRebootCauseComponent = $EmergencyRebootCauseComponent
    $script:EmergencyRebootLastAt = (Get-Date).ToUniversalTime().ToString("o")
    $script:EmergencyRebootCauseComponent = $Component
    if (-not (Save-RecoveryFailureState)) {
        $script:EmergencyRebootLastAt = $previousEmergencyRebootLastAt
        $script:EmergencyRebootCauseComponent = $previousEmergencyRebootCauseComponent
        Write-Info "Emergency reboot blocked because its state could not be saved."
        return $false
    }
    try {
        Start-ScheduledTask -TaskName $EmergencyRebootTaskName
    } catch {
        Write-Info "Emergency reboot request was persisted but its scheduled task could not start."
        return $false
    }
    Write-Info "Emergency reboot requested after exhausted $Component recovery attempts."
    return $true
}

function Set-RecoveryTaskSettings {
    $scheduledTask = Get-ScheduledTask -TaskName $TaskName
    $settings = $scheduledTask.Settings
    $settings.ExecutionTimeLimit = "PT0S"
    $settings.RestartCount = 3
    $settings.RestartInterval = "PT1M"
    Set-ScheduledTask -TaskName $TaskName -Settings $settings | Out-Null
}

function Install-ScheduledTask {
    Install-EmergencyRebootTask
    $taskAction = "powershell.exe -WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" -Supervisor"
    $runAsUser = "$env:USERDOMAIN\$env:USERNAME"
    Write-Info "Registering startup task for $runAsUser. Windows will prompt for the account password."
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $createOutput = (& schtasks.exe /Create /TN $TaskName /SC ONSTART /RU $runAsUser /RP * /TR $taskAction /RL LIMITED /F 2>&1 | Out-String)
        $createExitCode = $LASTEXITCODE
        if ($createExitCode -ne 0) {
            $existingTask = (& schtasks.exe /Query /TN $TaskName /FO LIST /V 2>&1 | Out-String)
            if ($existingTask -match [regex]::Escape($ScriptPath)) {
                Write-Info "Scheduled task '$TaskName' already points to $ScriptPath."
            } else {
                throw "Failed to register scheduled task '$TaskName' with schtasks.exe (exit code $createExitCode)."
            }
        } elseif ($createOutput.Trim()) {
            Write-Info $createOutput.Trim()
        }
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    try {
        [void](Set-RecoveryTaskSettings)
    } catch {
        Write-Info "Could not update scheduled task recovery settings after registration for '$TaskName': $($_.Exception.Message). Task identity was retained; manual follow-up required to configure restart-on-failure settings."
    }
    Write-Info "Registered scheduled task '$TaskName' to start with Windows."
}

Load-RecoveryFailureState

function Enter-SupervisorLock {
    New-Item -ItemType Directory -Path $RecoveryStateDirectory -Force | Out-Null
    try {
        return [System.IO.File]::Open(
            $SupervisorLockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
    } catch [System.IO.IOException] {
        return $null
    }
}

function Exit-SupervisorLock($LockStream) {
    if ($null -eq $LockStream) {
        return
    }
    try {
        $LockStream.Dispose()
    } finally {
        Remove-Item -LiteralPath $SupervisorLockPath -Force -ErrorAction SilentlyContinue
    }
}

function Start-Supervisor {
    $lockStream = Enter-SupervisorLock
    if ($null -eq $lockStream) {
        Write-Info "Recovery supervisor is already running; skipping duplicate start."
        return
    }

    try {
        [void](Set-RecoveryTaskSettings)
    } catch {
        Write-Info "Could not update scheduled task recovery settings: $($_.Exception.Message)"
    }

    try {
        try {
            Update-HostMetrics
        } catch {
            Write-Info "Initial host metrics update failed; continuing startup."
        }
        Write-Info "Waiting 120 seconds for WSL and Docker after logon."
        Start-Sleep -Seconds 120
        try {
            Start-PersonalServerStack
        } catch {
            Write-Info "Initial stack bootstrap failed: $($_.Exception.Message)"
        }

        $rapidFailureCount = 0
        while ($true) {
            $startedAt = Get-Date
            try {
                [void](Start-Process -FilePath "powershell.exe" -ArgumentList @(
                    "-WindowStyle", "Hidden", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", $ScriptPath, "-Daemon", "-ProjectRoot", $ProjectRoot
                ) -PassThru -Wait -ErrorAction Stop)
            } catch {
                Write-Info "Recovery daemon could not start; scheduling a supervised retry."
            }

            $daemonLifetimeSeconds = ((Get-Date) - $startedAt).TotalSeconds
            if ($daemonLifetimeSeconds -lt $DaemonRapidFailureWindowSeconds) {
                $rapidFailureCount += 1
            } else {
                $rapidFailureCount = 0
            }
            if ($rapidFailureCount -ge $DaemonRapidFailureLimit) {
                Write-Info "Recovery daemon ended repeatedly; applying restart backoff."
                Start-Sleep -Seconds $DaemonFailureBackoffSeconds
                $rapidFailureCount = 0
                continue
            }
            Write-Info "Recovery daemon exited; restarting under supervisor."
            Start-Sleep -Seconds $DaemonRestartDelaySeconds
        }
    } finally {
        Exit-SupervisorLock $lockStream
    }
}

function Enter-DaemonLock {
    New-Item -ItemType Directory -Path $RecoveryStateDirectory -Force | Out-Null
    try {
        return [System.IO.File]::Open(
            $DaemonLockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
    } catch [System.IO.IOException] {
        return $null
    }
}

function Exit-DaemonLock($LockStream) {
    if ($null -eq $LockStream) {
        return
    }
    try {
        $LockStream.Dispose()
    } finally {
        Remove-Item -LiteralPath $DaemonLockPath -Force -ErrorAction SilentlyContinue
    }
}

function Start-Daemon {
    $lockStream = Enter-DaemonLock
    if ($null -eq $lockStream) {
        Write-Info "Recovery daemon is already running; skipping duplicate start."
        return
    }
    try {
        while ($true) {
            try {
                Update-HostMetrics
                Invoke-RecoveryCycle
            } catch {
                Write-Info "Recovery check failed: $($_.Exception.Message)"
                Write-RecoveryEvent -Component "system" -Event "recovery_cycle" -Status "failed" -Action "none"
            }
            Write-Info "Sleeping 3 minutes before the next recovery check."
            Start-Sleep -Seconds $RecoveryIntervalSeconds
        }
    } finally {
        Exit-DaemonLock $lockStream
    }
}

if ($InstallTask) {
    Install-ScheduledTask
    exit 0
}

if ($Supervisor) {
    Start-Supervisor
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

throw "Use -InstallTask, -Start, -Supervisor, or -Daemon."
