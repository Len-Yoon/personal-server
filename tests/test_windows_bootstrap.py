import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "windows-bootstrap.ps1"
SCRIPT = SCRIPT_PATH.read_text(encoding="utf-8-sig")
WSL_SCRIPT = (ROOT / "scripts" / "windows-bootstrap.sh").read_text(encoding="utf-8-sig")


class WindowsBootstrapTests(unittest.TestCase):
    def test_scheduled_task_runs_a_supervisor_that_restarts_the_daemon(self):
        self.assertIn("function Start-Supervisor", SCRIPT)
        installer = SCRIPT[
            SCRIPT.index("function Install-ScheduledTask")
            : SCRIPT.index("\nLoad-RecoveryFailureState")
        ]
        supervisor = SCRIPT[
            SCRIPT.index("function Start-Supervisor")
            : SCRIPT.index("if ($InstallTask)")
        ]

        self.assertIn("[switch]$Supervisor", SCRIPT)
        self.assertIn("-Supervisor", installer)
        self.assertIn("Start-Process", supervisor)
        self.assertIn("-Daemon", supervisor)
        self.assertIn("-Wait", supervisor)
        self.assertIn("Start-Sleep -Seconds $DaemonRestartDelaySeconds", supervisor)
        self.assertIn("Enter-SupervisorLock", supervisor)
        self.assertIn("Exit-SupervisorLock", supervisor)
        self.assertIn("$daemonLifetimeSeconds -lt $DaemonRapidFailureWindowSeconds", supervisor)
        self.assertIn("Start-Sleep -Seconds $DaemonFailureBackoffSeconds", supervisor)
        self.assertIn("if ($Supervisor)", SCRIPT)
        self.assertIn("Start-Supervisor", SCRIPT)

    def test_supervisor_applies_boot_delay_once_while_daemon_restarts_without_it(self):
        self.assertIn("function Start-Supervisor", SCRIPT)
        supervisor = SCRIPT[
            SCRIPT.index("function Start-Supervisor")
            : SCRIPT.index("function Start-Daemon")
        ]
        daemon = SCRIPT[
            SCRIPT.index("function Start-Daemon")
            : SCRIPT.index("if ($InstallTask)")
        ]

        self.assertIn("Waiting 120 seconds for WSL and Docker after logon.", supervisor)
        self.assertIn("Start-Sleep -Seconds 120", supervisor)
        self.assertNotIn("Waiting 120 seconds for WSL and Docker after logon.", daemon)

    def test_supervisor_uses_a_distinct_lifetime_lock_to_prevent_duplicate_daemons(self):
        self.assertIn("function Enter-SupervisorLock", SCRIPT)
        lock = SCRIPT[
            SCRIPT.index("function Enter-SupervisorLock")
            : SCRIPT.index("function Start-Supervisor")
        ]

        self.assertIn('$SupervisorLockPath = Join-Path $RecoveryStateDirectory "supervisor.lock"', SCRIPT)
        self.assertIn("[System.IO.FileShare]::None", lock)
        self.assertIn("[System.IO.FileMode]::OpenOrCreate", lock)
        self.assertIn("Remove-Item -LiteralPath $SupervisorLockPath", lock)

    def test_daemon_uses_its_own_lifetime_lock_to_block_legacy_or_manual_duplicates(self):
        self.assertIn("function Enter-DaemonLock", SCRIPT)
        lock = SCRIPT[
            SCRIPT.index("function Enter-DaemonLock")
            : SCRIPT.index("function Start-Daemon")
        ]
        daemon = SCRIPT[
            SCRIPT.index("function Start-Daemon")
            : SCRIPT.index("if ($InstallTask)")
        ]

        self.assertIn('$DaemonLockPath = Join-Path $RecoveryStateDirectory "daemon.lock"', SCRIPT)
        self.assertIn("[System.IO.FileShare]::None", lock)
        self.assertIn("Enter-DaemonLock", daemon)
        self.assertIn("Exit-DaemonLock", daemon)
        self.assertIn("already running; skipping duplicate start", daemon)

    def test_bootstrap_script_uses_utf8_bom_for_windows_powershell_korean_literals(self):
        self.assertTrue(SCRIPT_PATH.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_tunnel_transition_alert_reads_only_windows_credential_manager(self):
        credential = SCRIPT[
            SCRIPT.index("function Get-TunnelTelegramConfiguration")
            : SCRIPT.index("function Test-CloudflareTunnelRunning")
        ]
        notifier = SCRIPT[
            SCRIPT.index("function Send-TunnelTelegramNotification")
            : SCRIPT.index("function Test-CloudflareTunnelRunning")
        ]

        self.assertIn('$TunnelTelegramCredentialTarget = "personal-server-tunnel-telegram"', SCRIPT)
        self.assertIn("CredRead", credential)
        self.assertIn("CredentialBlob", credential)
        self.assertIn("UserName", credential)
        self.assertIn("bot_token = [string]$credential.Password", credential)
        self.assertIn("chat_id = [string]$credential.UserName", credential)
        self.assertIn("ConvertFrom-Json", credential)
        self.assertIn('([string]$credential.Password).TrimStart().StartsWith("{")', credential)
        self.assertIn("https://api.telegram.org/bot", notifier)
        self.assertNotIn("Write-Info $credentialPayload", credential)
        self.assertNotIn("Write-Info $config", notifier)

    def test_tunnel_transition_alert_keeps_legacy_json_credential_compatibility(self):
        credential = SCRIPT[
            SCRIPT.index("function Get-TunnelTelegramConfiguration")
            : SCRIPT.index("function Send-TunnelTelegramNotification")
        ]

        self.assertIn('([string]$credential.Password).TrimStart().StartsWith("{")', credential)
        self.assertIn("$credential.Password | ConvertFrom-Json", credential)
        self.assertIn("$configuration.bot_token", credential)
        self.assertIn("$configuration.chat_id", credential)

    def test_tunnel_transition_alert_rejects_empty_or_malformed_credentials_without_logging_them(self):
        credential = SCRIPT[
            SCRIPT.index("function Get-TunnelTelegramConfiguration")
            : SCRIPT.index("function Send-TunnelTelegramNotification")
        ]

        self.assertIn("[string]::IsNullOrWhiteSpace([string]$credential.Password)", credential)
        self.assertIn("[string]::IsNullOrWhiteSpace([string]$credential.UserName)", credential)
        self.assertIn('Write-Info "Tunnel Telegram credential has an invalid format."', credential)
        self.assertNotIn("Write-Info $credential", credential)

    def test_tunnel_transition_alert_sends_korean_message_as_utf8_json(self):
        notifier = SCRIPT[
            SCRIPT.index("function Send-TunnelTelegramNotification")
            : SCRIPT.index("function Update-TunnelTelegramNotification")
        ]

        self.assertIn("$payload = @{", notifier)
        self.assertIn("ConvertTo-Json -Compress", notifier)
        self.assertIn("[System.Text.Encoding]::UTF8.GetBytes($payload)", notifier)
        self.assertIn('-ContentType "application/json; charset=utf-8"', notifier)
        self.assertIn("-Body $payloadBytes", notifier)

    def test_tunnel_transition_alerts_are_persisted_and_sent_once_per_transition(self):
        state = SCRIPT[
            SCRIPT.index("function Save-RecoveryFailureState")
            : SCRIPT.index("function Set-RecoveryStateInvalid")
        ]
        loader = SCRIPT[
            SCRIPT.index("function Load-RecoveryFailureState")
            : SCRIPT.index("function Register-RecoveryFailure")
        ]
        cycle = SCRIPT[
            SCRIPT.index("function Invoke-RecoveryCycle")
            : SCRIPT.index("function Install-EmergencyRebootTask")
        ]

        self.assertIn("$TunnelAlertDownNotified", state)
        self.assertIn("tunnel_alert", state)
        self.assertIn('Properties["tunnel_alert"]', loader)
        self.assertIn("Update-TunnelTelegramNotification", cycle)
        self.assertLess(cycle.index("Update-TunnelTelegramNotification"), cycle.index("foreach ($component in $RecoveryComponents)"))

    def test_recovery_events_are_bounded_structured_and_secret_free(self):
        """Operator diagnostics need a compact history without credential or command leakage."""
        self.assertIn('$RecoveryEventLogPath = Join-Path $RecoveryStateDirectory "recovery-events.jsonl"', SCRIPT)
        self.assertIn("$RecoveryEventLogMaxEntries = 200", SCRIPT)
        event_log = SCRIPT[
            SCRIPT.index("function Write-RecoveryEvent")
            : SCRIPT.index("function Save-RecoveryFailureState")
        ]

        self.assertIn("ToUniversalTime().ToString(\"o\")", event_log)
        self.assertIn("component = $Component", event_log)
        self.assertIn("event = $Event", event_log)
        self.assertIn("status = $Status", event_log)
        self.assertIn("action = $Action", event_log)
        self.assertIn("ConvertTo-Json -Compress", event_log)
        self.assertIn("Select-Object -Last ($RecoveryEventLogMaxEntries - 1)", event_log)
        self.assertIn('Write-Info "Recovery event logging failed."', event_log)
        self.assertNotIn("bot_token", event_log)
        self.assertNotIn("chat_id", event_log)
        self.assertNotIn("CommandLine", event_log)

    def test_recovery_events_do_not_treat_action_dispatch_as_health_restoration(self):
        """A restart request is not evidence that the affected component is healthy."""
        cycle = SCRIPT[
            SCRIPT.index("function Invoke-RecoveryCycle")
            : SCRIPT.index("function Install-EmergencyRebootTask")
        ]
        reset = SCRIPT[
            SCRIPT.index("function Reset-RecoveryFailure")
            : SCRIPT.index("function Test-RecoveryActionNeeded")
        ]

        self.assertIn('Event "recovery_dispatch" -Status "accepted"', cycle)
        self.assertIn('Event "recovery_dispatch" -Status "failed"', cycle)
        self.assertNotIn('Event "recovery_completed" -Status "succeeded"', cycle)
        self.assertIn('Event "health_restored" -Status "healthy"', reset)

    def test_emergency_reboot_task_uses_triggerless_system_xml_registration(self):
        reboot = SCRIPT[
            SCRIPT.index("function Install-EmergencyRebootTask")
            : SCRIPT.index("function Test-EmergencyRebootEligible")
        ]
        installer = SCRIPT[
            SCRIPT.index("function Install-ScheduledTask")
            : SCRIPT.index("\nLoad-RecoveryFailureState")
        ]

        self.assertIn("Join-Path $env:TEMP", reboot)
        self.assertIn("schtasks.exe /Create /TN $EmergencyRebootTaskName /XML $temporaryTaskXml /F", reboot)
        self.assertIn("<UserId>S-1-5-18</UserId>", reboot)
        self.assertIn("<RunLevel>HighestAvailable</RunLevel>", reboot)
        self.assertIn("<Command>shutdown.exe</Command>", reboot)
        self.assertIn("<Arguments>/r /f /t 60</Arguments>", reboot)
        self.assertNotIn("<Triggers>", reboot)
        self.assertNotIn("/SC ONCE", reboot)
        self.assertNotIn("/ST ", reboot)
        self.assertIn("finally {", reboot)
        self.assertIn("Remove-Item -LiteralPath $temporaryTaskXml", reboot)
        self.assertLess(installer.index("Install-EmergencyRebootTask"), installer.index("schtasks.exe /Create /TN $TaskName"))

    def test_emergency_reboot_xml_has_exported_schema_element_order_and_complete_settings(self):
        reboot = SCRIPT[
            SCRIPT.index("function Install-EmergencyRebootTask")
            : SCRIPT.index("function Test-EmergencyRebootEligible")
        ]

        required_elements = (
            "<RegistrationInfo>",
            "<URI>\\PersonalServer-EmergencyReboot</URI>",
            "<Description>Emergency reboot task for exhausted core recovery attempts.</Description>",
            "<Principals>",
            '<Principal id="SYSTEM">',
            "<UserId>S-1-5-18</UserId>",
            "<RunLevel>HighestAvailable</RunLevel>",
            "<Settings>",
            "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
            "<DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>",
            "<StopIfGoingOnBatteries>true</StopIfGoingOnBatteries>",
            "<AllowHardTerminate>true</AllowHardTerminate>",
            "<StartWhenAvailable>false</StartWhenAvailable>",
            "<RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>",
            "<IdleSettings>",
            "<StopOnIdleEnd>true</StopOnIdleEnd>",
            "<RestartOnIdle>false</RestartOnIdle>",
            "<AllowStartOnDemand>true</AllowStartOnDemand>",
            "<Enabled>true</Enabled>",
            "<Hidden>true</Hidden>",
            "<RunOnlyIfIdle>false</RunOnlyIfIdle>",
            "<WakeToRun>false</WakeToRun>",
            "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>",
            "<Priority>7</Priority>",
            '<Actions Context="SYSTEM">',
            "<Command>shutdown.exe</Command>",
            "<Arguments>/r /f /t 60</Arguments>",
        )
        for element in required_elements:
            self.assertIn(element, reboot)
        self.assertNotIn("<Triggers>", reboot)
        self.assertEqual(
            [reboot.index(element) for element in required_elements],
            sorted(reboot.index(element) for element in required_elements),
        )

    def test_emergency_reboot_schtasks_capture_preserves_native_error_output(self):
        reboot = SCRIPT[
            SCRIPT.index("function Install-EmergencyRebootTask")
            : SCRIPT.index("function Test-EmergencyRebootEligible")
        ]

        self.assertIn('$previousErrorActionPreference = $ErrorActionPreference', reboot)
        self.assertIn('$ErrorActionPreference = "Continue"', reboot)
        self.assertIn('$createExitCode = $LASTEXITCODE', reboot)
        self.assertIn('$ErrorActionPreference = $previousErrorActionPreference', reboot)
        self.assertIn('exit code $createExitCode', reboot)
        self.assertIn('$result.Trim()', reboot)

    def test_emergency_request_save_failure_restores_declared_prior_memory_state(self):
        request = SCRIPT[
            SCRIPT.index("function Request-EmergencyReboot")
            : SCRIPT.index("function Set-RecoveryTaskSettings")
        ]
        save_failure = request[
            request.index("if (-not (Save-RecoveryFailureState))")
            : request.index("try {", request.index("if (-not (Save-RecoveryFailureState))"))
        ]

        self.assertLess(
            request.index("$previousEmergencyRebootLastAt = $EmergencyRebootLastAt"),
            request.index("$script:EmergencyRebootLastAt = (Get-Date)"),
        )
        self.assertLess(
            request.index("$previousEmergencyRebootCauseComponent = $EmergencyRebootCauseComponent"),
            request.index("$script:EmergencyRebootCauseComponent = $Component"),
        )
        self.assertIn("$script:EmergencyRebootLastAt = $previousEmergencyRebootLastAt", save_failure)
        self.assertIn("$script:EmergencyRebootCauseComponent = $previousEmergencyRebootCauseComponent", save_failure)

    def test_failed_emergency_task_start_keeps_persisted_audit_and_cooldown_evidence(self):
        request = SCRIPT[
            SCRIPT.index("function Request-EmergencyReboot")
            : SCRIPT.index("function Set-RecoveryTaskSettings")
        ]

        start_index = request.index("Start-ScheduledTask -TaskName $EmergencyRebootTaskName")
        failed_start = request[request.index("catch {", start_index) :]
        self.assertIn("Emergency reboot request was persisted but its scheduled task could not start.", failed_start)
        self.assertNotIn("$script:EmergencyRebootLastAt = $previousEmergencyRebootLastAt", failed_start)
        self.assertNotIn("$script:EmergencyRebootCauseComponent = $previousEmergencyRebootCauseComponent", failed_start)

    def test_failed_target_action_escalates_only_after_final_core_health_check(self):
        cycle = SCRIPT[
            SCRIPT.index("function Invoke-RecoveryCycle")
            : SCRIPT.index("function Install-EmergencyRebootTask")
        ]

        failed_action = cycle[cycle.index("if (-not $targetedRecoverySucceeded)") :]
        self.assertIn("$finalHealth = Get-RecoveryHealth", failed_action)
        self.assertIn('if ($finalHealth[$component] -ne "unhealthy")', failed_action)
        self.assertLess(failed_action.index("$finalHealth = Get-RecoveryHealth"), failed_action.index("Test-EmergencyRebootEligible $component"))
        self.assertLess(failed_action.index('if ($finalHealth[$component] -ne "unhealthy")'), failed_action.index("Test-EmergencyRebootEligible $component"))

    def test_invalid_persisted_state_marks_recovery_dirty_before_any_recovery_save(self):
        invalid_state = SCRIPT[
            SCRIPT.index("function Set-RecoveryStateInvalid")
            : SCRIPT.index("function Register-RecoveryFailure")
        ]
        cycle = SCRIPT[
            SCRIPT.index("function Invoke-RecoveryCycle")
            : SCRIPT.index("function Install-EmergencyRebootTask")
        ]

        self.assertIn('$script:EmergencyRebootCooldownStateValid = $false', invalid_state)
        self.assertIn('$script:RecoveryStateDirty = $true', invalid_state)
        self.assertIn("Set-RecoveryStateInvalid", invalid_state)
        dirty_check = cycle.index("if ($RecoveryStateDirty) {", cycle.index("$health = Get-RecoveryHealth"))
        self.assertLess(dirty_check, cycle.index("Register-RecoveryFailure $component"))

    def test_legacy_recovery_state_without_emergency_reboot_schema_is_migrated(self):
        loader = SCRIPT[
            SCRIPT.index("function Load-RecoveryFailureState")
            : SCRIPT.index("function Register-RecoveryFailure")
        ]

        self.assertIn('Properties["emergency_reboot"]', loader)
        self.assertIn('$null -eq $storedEmergencyReboot', loader)
        self.assertIn("Save-RecoveryFailureState", loader)
        self.assertIn("Legacy recovery state is missing emergency reboot metadata; migrating.", loader)
        self.assertIn('$null -eq $storedEmergencyReboot.Value', loader)
        self.assertIn('$null -eq $storedLastReboot', loader)
        self.assertIn('$null -eq $storedCauseComponent', loader)
        self.assertIn("[string]::IsNullOrWhiteSpace", loader)
        self.assertIn("Set-RecoveryStateInvalid", loader)

    def test_existing_recovery_state_requires_complete_component_counter_schema_for_reboot(self):
        loader = SCRIPT[
            SCRIPT.index("function Load-RecoveryFailureState")
            : SCRIPT.index("function Register-RecoveryFailure")
        ]

        self.assertIn("foreach ($component in $RecoveryComponents)", loader)
        self.assertIn('$storedComponent.Value -isnot [PSCustomObject]', loader)
        self.assertIn('$null -eq $storedFailureCount', loader)
        self.assertIn('$null -eq $storedAttemptCount', loader)
        self.assertIn('$failureCount -lt 0', loader)
        self.assertIn('$attemptCount -lt 0', loader)

    def test_corrupt_or_non_object_emergency_reboot_state_fails_closed_but_missing_file_remains_valid(self):
        loader = SCRIPT[
            SCRIPT.index("function Load-RecoveryFailureState")
            : SCRIPT.index("function Register-RecoveryFailure")
        ]

        self.assertIn("if (-not (Test-Path -LiteralPath $RecoveryStatePath)) {\n        return", loader)
        self.assertIn("$storedEmergencyReboot.Value -isnot [PSCustomObject]", loader)
        self.assertIn("Set-RecoveryStateInvalid", loader)
        catch_body = loader[loader.rindex("catch {") :]
        self.assertIn("Set-RecoveryStateInvalid", catch_body)

    def test_emergency_reboot_task_is_installed_and_failed_start_keeps_cooldown_state(self):
        installer = SCRIPT[
            SCRIPT.index("function Install-ScheduledTask")
            : SCRIPT.index("\nLoad-RecoveryFailureState")
        ]
        request = SCRIPT[
            SCRIPT.index("function Request-EmergencyReboot")
            : SCRIPT.index("function Set-RecoveryTaskSettings")
        ]

        self.assertIn("Install-EmergencyRebootTask", installer)
        self.assertIn("Get-ScheduledTask -TaskName $EmergencyRebootTaskName -ErrorAction Stop", request)
        self.assertIn("Start-ScheduledTask -TaskName $EmergencyRebootTaskName", request)
        start_index = request.index("Start-ScheduledTask -TaskName $EmergencyRebootTaskName")
        failed_start = request[request.index("catch {", start_index) :]
        self.assertIn("Emergency reboot request was persisted but its scheduled task could not start.", failed_start)
        self.assertNotIn("$previousEmergencyRebootLastAt", failed_start)

    def test_core_targeted_recovery_rechecks_health_before_emergency_escalation(self):
        targeted = SCRIPT[
            SCRIPT.index("function Invoke-TargetedRecovery")
            : SCRIPT.index("function Enter-RecoveryLock")
        ]
        cycle = SCRIPT[
            SCRIPT.index("function Invoke-RecoveryCycle")
            : SCRIPT.index("function Install-EmergencyRebootTask")
        ]

        self.assertGreaterEqual(targeted.count("Get-RecoveryHealth"), 2)
        self.assertIn('$health.keepalive -eq "healthy"', targeted)
        self.assertIn('$health.k3s -eq "healthy"', targeted)
        self.assertIn("$attemptCount -ne $RecoveryMaxAttempts", SCRIPT)
        self.assertLess(cycle.index("Invoke-TargetedRecovery $component"), cycle.index("Test-EmergencyRebootEligible $component"))
        self.assertIn("if (Request-EmergencyReboot $component)", cycle)

    def test_invalid_persisted_emergency_reboot_timestamp_blocks_reboot_without_erasing_it(self):
        loader = SCRIPT[
            SCRIPT.index("function Load-RecoveryFailureState")
            : SCRIPT.index("function Register-RecoveryFailure")
        ]
        eligible = SCRIPT[
            SCRIPT.index("function Test-EmergencyRebootEligible")
            : SCRIPT.index("function Request-EmergencyReboot")
        ]

        self.assertIn('$script:EmergencyRebootLastAt = [string]$storedLastReboot.Value', loader)
        self.assertIn("Set-RecoveryStateInvalid", loader)
        self.assertIn("if (-not $EmergencyRebootCooldownStateValid)", eligible)
        self.assertIn("persisted cooldown timestamp is invalid", eligible)

    def test_emergency_reboot_is_limited_to_exhausted_keepalive_or_k3s_recovery(self):
        reboot = SCRIPT[
            SCRIPT.index("function Install-EmergencyRebootTask")
            : SCRIPT.index("function Set-RecoveryTaskSettings")
        ]

        self.assertIn('$EmergencyRebootTaskName = "PersonalServer-EmergencyReboot"', SCRIPT)
        self.assertIn('$EmergencyRebootGraceSeconds = 1200', SCRIPT)
        self.assertIn('$EmergencyRebootCooldownSeconds = 21600', SCRIPT)
        self.assertIn('"keepalive", "k3s"', reboot)
        self.assertNotIn('"tunnel", "portal", "nodeport"', reboot)
        self.assertIn('shutdown.exe /r /f /t 60', reboot)
        self.assertIn("<UserId>S-1-5-18</UserId>", reboot)
        self.assertIn("<RunLevel>HighestAvailable</RunLevel>", reboot)

    def test_uses_schtasks_when_scheduled_task_cmdlets_are_unavailable(self):
        self.assertIn("schtasks.exe /Create", SCRIPT)
        self.assertIn("/SC ONSTART", SCRIPT)
        self.assertIn("/RP *", SCRIPT)
        self.assertIn("/F", SCRIPT)
        self.assertIn("schtasks.exe /Query", SCRIPT)

    def test_schtasks_warning_stderr_does_not_terminate_install_task(self):
        installer = SCRIPT[
            SCRIPT.index("function Install-ScheduledTask") : SCRIPT.index("\nLoad-RecoveryFailureState")
        ]

        self.assertIn('$ErrorActionPreference = "Continue"', installer)
        self.assertIn("try {", installer)
        self.assertIn("finally {", installer)
        self.assertIn("$ErrorActionPreference = $previousErrorActionPreference", installer)

    def test_recovery_starts_the_car_care_worker_and_other_services(self):
        self.assertIn("docker-compose.yml", WSL_SCRIPT)
        self.assertIn("up -d", WSL_SCRIPT)
        self.assertIn("crawler-worker", WSL_SCRIPT)
        self.assertIn("car-care-worker", WSL_SCRIPT)
        self.assertNotIn("up -d --build portal-web system-agent", WSL_SCRIPT)

    def test_runs_daily_maintenance_once_after_stack_start(self):
        self.assertIn("run_daily_maintenance", WSL_SCRIPT)
        self.assertIn("scripts/maintenance.py all", WSL_SCRIPT)
        self.assertIn("personal-server-maintenance.last", WSL_SCRIPT)

    def test_loads_maintenance_settings_from_project_env(self):
        for key in (
            "DATA_ROOT",
            "BACKUP_PATH",
            "SECURITY_LOG_PATH",
            "NEWS_ARCHIVE_PATH",
            "BACKUP_RETENTION_DAYS",
            "SECURITY_LOG_RETENTION_DAYS",
            "NEWS_RETENTION_DAYS",
        ):
            self.assertIn(f"load_project_env_value {key}", WSL_SCRIPT)

    def test_normalizes_container_data_paths_for_wsl_maintenance(self):
        self.assertIn("normalize_project_path DATA_ROOT", WSL_SCRIPT)
        self.assertIn("normalize_project_path SECURITY_LOG_PATH", WSL_SCRIPT)

    def test_powershell_daemon_isolates_maintenance_failure(self):
        self.assertIn("bash scripts/windows-bootstrap.sh", SCRIPT)
        self.assertIn("Recovery check failed", SCRIPT)

    def test_tunnel_recovery_controls_the_registered_wsl_user_service(self):
        recovery = SCRIPT[
            SCRIPT.index("function Test-CloudflareTunnelService") : SCRIPT.index("function Update-HostMetrics")
        ]
        self.assertIn('$WslServiceUser = "window"', SCRIPT)
        self.assertIn('"bash", "-lc", "systemctl --user is-active --quiet \'$CloudflareTunnelService\'"', recovery)
        self.assertIn('"bash", "-lc", "systemctl --user start \'$CloudflareTunnelService\'"', recovery)
        self.assertNotIn("Start-Process -FilePath 'wsl.exe'", recovery)
        self.assertNotIn("'cloudflared', 'tunnel', 'run'", recovery)
        self.assertNotIn("nohup cloudflared tunnel run", WSL_SCRIPT)

    def test_tunnel_recovery_restarts_active_service_when_connection_process_is_missing(self):
        recovery = SCRIPT[
            SCRIPT.index("function Start-CloudflareTunnel") : SCRIPT.index("function Update-HostMetrics")
        ]
        self.assertIn("if (Test-CloudflareTunnelService) {", recovery)
        self.assertIn("if (Test-CloudflareTunnelRunning) {", recovery)
        self.assertIn('"bash", "-lc", "systemctl --user restart \'$CloudflareTunnelService\'"', recovery)
        self.assertIn("return ((Test-CloudflareTunnelService) -and (Test-CloudflareTunnelRunning))", recovery)

    def test_daemon_uses_three_minute_health_interval_and_two_failures_before_recovery(self):
        self.assertIn("$RecoveryIntervalSeconds = 180", SCRIPT)
        self.assertIn("$RecoveryFailureThreshold = 2", SCRIPT)
        self.assertIn("if ($failureCount -lt $RecoveryFailureThreshold)", SCRIPT)

    def test_supervisor_preserves_task_identity_while_enabling_restart_after_crash(self):
        settings = SCRIPT[
            SCRIPT.index("function Set-RecoveryTaskSettings") : SCRIPT.index("function Install-ScheduledTask")
        ]
        installer = SCRIPT[
            SCRIPT.index("function Install-ScheduledTask") : SCRIPT.index("\nLoad-RecoveryFailureState\n\nfunction Enter-SupervisorLock")
        ]
        self.assertIn("Get-ScheduledTask -TaskName $TaskName", settings)
        self.assertIn("$settings = $scheduledTask.Settings", settings)
        self.assertIn('$settings.ExecutionTimeLimit = "PT0S"', settings)
        self.assertIn("$settings.RestartCount = 3", settings)
        self.assertIn('$settings.RestartInterval = "PT1M"', settings)
        self.assertIn("Set-ScheduledTask -TaskName $TaskName -Settings $settings", settings)
        self.assertNotIn("New-ScheduledTaskSettingsSet", settings)
        self.assertIn("[void](Set-RecoveryTaskSettings)", installer)
        supervisor = SCRIPT[SCRIPT.index("function Start-Supervisor") : SCRIPT.index("function Start-Daemon")]
        self.assertIn("[void](Set-RecoveryTaskSettings)", supervisor)

    def test_install_task_keeps_registered_task_when_recovery_settings_update_fails(self):
        installer = SCRIPT[
            SCRIPT.index("function Install-ScheduledTask") : SCRIPT.index("\nLoad-RecoveryFailureState")
        ]
        settings_update = installer[installer.index("try {", installer.index("[void](Set-RecoveryTaskSettings)") - 20) :]
        self.assertIn("try {", settings_update)
        self.assertIn("catch {", settings_update)
        self.assertIn("Could not update scheduled task recovery settings after registration", settings_update)
        self.assertIn("$TaskName", settings_update)
        self.assertIn("manual", settings_update.lower())
        self.assertLess(
            settings_update.index("Could not update scheduled task recovery settings after registration"),
            settings_update.index("Registered scheduled task '$TaskName'"),
        )

    def test_daemon_runs_targeted_recovery_cycle_without_periodic_stack_recreation(self):
        """A daemon-loop stack bootstrap would recreate normal services every interval."""
        daemon = SCRIPT[
            SCRIPT.index("function Start-Daemon") : SCRIPT.index("if ($InstallTask)")
        ]
        daemon_loop = daemon[daemon.index("while ($true)") :]
        supervisor = SCRIPT[
            SCRIPT.index("function Start-Supervisor") : SCRIPT.index("function Start-Daemon")
        ]
        self.assertEqual(daemon.count("Start-PersonalServerStack"), 0)
        self.assertEqual(supervisor.count("Start-PersonalServerStack"), 1)
        self.assertIn("Invoke-RecoveryCycle", daemon_loop)
        self.assertIn("Start-Sleep -Seconds $RecoveryIntervalSeconds", daemon_loop)
        self.assertNotIn("Start-PersonalServerStack", daemon_loop)

    def test_daemon_continues_targeted_recovery_after_supervisor_bootstrap_failure(self):
        """An initial bootstrap exception must not prevent later targeted recovery cycles."""
        supervisor = SCRIPT[
            SCRIPT.index("function Start-Supervisor") : SCRIPT.index("function Start-Daemon")
        ]
        daemon = SCRIPT[
            SCRIPT.index("function Start-Daemon") : SCRIPT.index("if ($InstallTask)")
        ]
        startup = supervisor[: supervisor.index("while ($true)")]
        daemon_loop = daemon[daemon.index("while ($true)") :]
        self.assertIn("try {", startup)
        self.assertIn("Start-PersonalServerStack", startup)
        self.assertIn("Initial stack bootstrap failed", startup)
        self.assertIn("Update-HostMetrics", daemon_loop)
        self.assertIn("Invoke-RecoveryCycle", daemon_loop)
        self.assertNotIn("Start-PersonalServerStack", daemon_loop)

    def test_targeted_recovery_does_not_recreate_compose_portal_writer(self):
        recovery = SCRIPT[SCRIPT.index("function Invoke-TargetedRecovery") : SCRIPT.index("function Enter-RecoveryLock")]
        self.assertNotIn("Start-PersonalServerStack", recovery)
        self.assertNotIn("docker compose", recovery)

    def test_recovery_health_has_exact_five_component_contract(self):
        health = SCRIPT[SCRIPT.index("function Get-RecoveryHealth") : SCRIPT.index("function Register-RecoveryFailure")]
        for component in ("keepalive", "k3s", "portal", "nodeport", "tunnel"):
            self.assertIn(f"{component} =", health)
        self.assertNotIn("wsl =", health)

    def test_recovery_failures_persist_only_counters_under_project_root(self):
        self.assertIn('Join-Path $ProjectRoot "data\\recovery-state.json"', SCRIPT)
        self.assertIn("ConvertTo-Json", SCRIPT)
        self.assertIn("ConvertFrom-Json", SCRIPT)
        self.assertIn("Set-Content", SCRIPT)
        self.assertIn("catch {", SCRIPT)
        self.assertIn("$RecoveryFailureCounts = @{}", SCRIPT)

    def test_targeted_recovery_has_single_run_lock_and_component_limits(self):
        self.assertIn("New-Item -ItemType Directory -Path $RecoveryStateDirectory -Force", SCRIPT)
        self.assertIn("$RecoveryLockPath", SCRIPT)
        self.assertIn("$RecoveryMaxAttempts = 3", SCRIPT)
        self.assertIn("Start-CloudflareTunnel", SCRIPT)

    def test_k3s_recovery_only_starts_inactive_service(self):
        action_needed = SCRIPT[SCRIPT.index("function Test-RecoveryActionNeeded") : SCRIPT.index("function Invoke-TargetedRecovery")]
        action = SCRIPT[SCRIPT.index("function Invoke-TargetedRecovery") : SCRIPT.index("function Enter-RecoveryLock")]
        self.assertIn('"systemctl", "is-active", "--quiet", "k3s"', action_needed)
        self.assertIn('"systemctl", "start", "k3s"', action)
        self.assertNotIn("systemctl restart k3s", action_needed + action)

    def test_recovery_health_checks_all_five_components_without_stack_bootstrap(self):
        health = SCRIPT[SCRIPT.index("function Get-RecoveryHealth") : SCRIPT.index("function Save-RecoveryFailureState")]
        self.assertIn('Get-ScheduledTask -TaskName "PersonalServer-WSL-KeepAlive"', health)
        self.assertIn('"systemctl", "is-active", "--quiet", "k3s"', health)
        self.assertIn("--raw=/readyz", health)
        self.assertIn("deployment/portal-web", health)
        self.assertIn("--for=condition=Available", health)
        self.assertIn('$CaddyContainerName = "personal-server-caddy-1"', SCRIPT)
        self.assertIn('"docker", "exec", $CaddyContainerName, "curl"', health)
        self.assertIn("host.docker.internal:30080/health", health)
        self.assertNotIn("127.0.0.1:30080/health", health)
        self.assertIn("Test-CloudflareTunnelRunning", health)
        self.assertIn("[c]loudflared.*tunnel run", SCRIPT)
        self.assertNotIn("Start-PersonalServerStack", health)

    def test_recovery_cycle_defers_nodeport_only_failure_without_portal_restart(self):
        recovery = SCRIPT[SCRIPT.index("function Invoke-TargetedRecovery") : SCRIPT.index("function Enter-RecoveryLock")]
        nodeport = recovery[recovery.index('"nodeport"') : recovery.index('"tunnel"')]
        self.assertIn("NodePort is unhealthy; deferring to the next recovery cycle.", nodeport)
        self.assertNotIn("rollout restart deployment/portal-web", nodeport)

    def test_recovery_lock_reuses_stale_file_but_keeps_exclusive_ownership(self):
        lock = SCRIPT[SCRIPT.index("function Enter-RecoveryLock") : SCRIPT.index("function Exit-RecoveryLock")]
        self.assertIn("[System.IO.FileMode]::OpenOrCreate", lock)
        self.assertIn("[System.IO.FileShare]::None", lock)
        self.assertNotIn("[System.IO.FileMode]::CreateNew", lock)

    def test_keepalive_health_requires_task_and_wsl_command(self):
        health = SCRIPT[SCRIPT.index("function Get-RecoveryHealth") : SCRIPT.index("function Save-RecoveryFailureState")]
        self.assertIn("Get-ScheduledTask -TaskName \"PersonalServer-WSL-KeepAlive\"", health)
        self.assertIn("Invoke-WslWithTimeout", health)
        self.assertNotIn("& wsl.exe", health)

    def test_wsl_health_and_recovery_commands_have_process_timeout(self):
        runner = SCRIPT
        recovery = SCRIPT[SCRIPT.index("function Get-RecoveryHealth") : SCRIPT.index("function Install-ScheduledTask")]
        self.assertIn("$RecoveryCommandTimeoutSeconds = 20", SCRIPT)
        self.assertIn("WaitForExit($RecoveryCommandTimeoutSeconds * 1000)", runner)
        self.assertIn("$process.Kill()", runner)
        self.assertIn("return $false", runner)
        self.assertNotIn("& wsl.exe", recovery)

    def test_portal_health_is_deferred_without_changing_its_counters_when_k3s_is_unhealthy(self):
        health = SCRIPT[SCRIPT.index("function Get-RecoveryHealth") : SCRIPT.index("function Save-RecoveryFailureState")]
        cycle = SCRIPT[SCRIPT.index("function Invoke-RecoveryCycle") : SCRIPT.index("function Install-ScheduledTask")]
        self.assertIn('portal = "deferred"', health)
        self.assertIn('if ($health.k3s -eq "healthy")', health)
        self.assertIn('if ($health[$component] -eq "deferred")', cycle)
        self.assertLess(cycle.index('if ($health[$component] -eq "deferred")'), cycle.index("Register-RecoveryFailure $component"))
        self.assertNotIn("Reset-RecoveryFailure $component", cycle[cycle.index('if ($health[$component] -eq "deferred")') : cycle.index("Register-RecoveryFailure $component")])

    def test_recovery_attempt_budget_is_separate_from_health_failures_and_stops_after_three_attempts(self):
        state = SCRIPT[SCRIPT.index("function Save-RecoveryFailureState") : SCRIPT.index("function Invoke-TargetedRecovery")]
        targeted = SCRIPT[SCRIPT.index("function Invoke-TargetedRecovery") : SCRIPT.index("function Enter-RecoveryLock")]
        self.assertIn("$RecoveryAttemptCounts = @{}", SCRIPT)
        self.assertIn("health_failures", state)
        self.assertIn("recovery_attempts", state)
        self.assertIn("function Register-RecoveryAttempt", state)
        self.assertIn("if ($attemptCount -ge $RecoveryMaxAttempts)", state)
        self.assertIn("$RecoveryAttemptCounts[$Component] = 0", state)
        self.assertNotIn("Register-RecoveryAttempt", targeted)

    def test_alternating_daemons_reload_persisted_attempt_budget_before_recovery_actions(self):
        """Each lock owner must use the latest persisted budget, so actions stay capped at three."""
        loader = SCRIPT[
            SCRIPT.index("function Load-RecoveryFailureState") : SCRIPT.index("function Register-RecoveryFailure")
        ]
        attempt = SCRIPT[
            SCRIPT.index("function Register-RecoveryAttempt") : SCRIPT.index("function Test-RecoveryActionNeeded")
        ]
        cycle = SCRIPT[SCRIPT.index("function Invoke-RecoveryCycle") : SCRIPT.index("function Install-ScheduledTask")]
        self.assertIn("$RecoveryMaxAttempts = 3", SCRIPT)
        self.assertIn("catch {", loader)
        self.assertIn("$RecoveryAttemptCounts = @{}", loader)
        self.assertIn("Load-RecoveryFailureState", cycle)
        self.assertLess(cycle.index("if ($null -eq $lockStream)"), cycle.index("Load-RecoveryFailureState"))
        self.assertLess(cycle.index("Load-RecoveryFailureState"), cycle.index("Get-RecoveryHealth"))
        self.assertLess(cycle.index("Register-RecoveryAttempt $component"), cycle.index("Invoke-TargetedRecovery $component"))
        self.assertLess(attempt.index("$RecoveryAttemptCounts[$Component] = $attemptCount + 1"), attempt.index("Save-RecoveryFailureState"))

    def test_unsaved_recovery_state_blocks_actions_and_retains_in_memory_attempt_budget(self):
        """A failed state save must not allow a stale reload to bypass the three-action limit."""
        save = SCRIPT[
            SCRIPT.index("function Save-RecoveryFailureState") : SCRIPT.index("function Load-RecoveryFailureState")
        ]
        cycle = SCRIPT[SCRIPT.index("function Invoke-RecoveryCycle") : SCRIPT.index("function Install-ScheduledTask")]
        setup = cycle[: cycle.index("$health = Get-RecoveryHealth")]
        pre_actions = cycle[cycle.index("$health = Get-RecoveryHealth") : cycle.index("foreach ($component in $RecoveryComponents)")]
        self.assertIn("$RecoveryStateDirty = $false", SCRIPT)
        self.assertIn("return $true", save)
        self.assertIn("return $false", save)
        self.assertIn("$script:RecoveryStateDirty = $true", save)
        self.assertIn("$script:RecoveryStateDirty = $false", save)
        self.assertNotIn("Save-RecoveryFailureState", setup)
        self.assertIn("if ($RecoveryStateDirty)", pre_actions)
        self.assertIn("return", pre_actions)
        self.assertLess(
            cycle.index("if ($RecoveryStateDirty) {", cycle.index("$health = Get-RecoveryHealth")),
            cycle.index("Invoke-TargetedRecovery $component"),
        )

    def test_dirty_daemon_never_overwrites_newer_persisted_attempt_state(self):
        """A dirty daemon must fail closed rather than save stale counters over another daemon's state."""
        cycle = SCRIPT[SCRIPT.index("function Invoke-RecoveryCycle") : SCRIPT.index("function Install-ScheduledTask")]
        health_index = cycle.index("$health = Get-RecoveryHealth")
        dirty_branch = cycle[
            cycle.index("if ($RecoveryStateDirty) {", health_index) : cycle.index("foreach ($component in $RecoveryComponents)")
        ]
        self.assertIn("retaining in-memory counters", dirty_branch)
        self.assertIn("return", dirty_branch)
        self.assertNotIn("Load-RecoveryFailureState", dirty_branch)
        self.assertNotIn("Save-RecoveryFailureState", dirty_branch)
        self.assertIn("skipping automated recovery actions this cycle", cycle)

    def test_bootstrap_path_keeps_its_original_unbounded_wsl_command(self):
        startup = SCRIPT[SCRIPT.index("function Invoke-WslCommand") : SCRIPT.index("function Test-CloudflareTunnelRunning")]
        self.assertIn("& wsl.exe -d $WslDistribution bash -lc", startup)
        self.assertNotIn("Invoke-WslWithTimeout", startup)

    def test_recovery_attempt_is_reserved_after_action_needed_check_and_before_targeted_action(self):
        needed = SCRIPT
        targeted = SCRIPT[SCRIPT.index("function Invoke-TargetedRecovery") : SCRIPT.index("function Enter-RecoveryLock")]
        cycle = SCRIPT[SCRIPT.index("function Invoke-RecoveryCycle") : SCRIPT.index("function Install-ScheduledTask")]
        self.assertNotIn("Register-RecoveryAttempt", targeted)
        self.assertIn("function Test-RecoveryActionNeeded", needed)
        self.assertIn('Get-ScheduledTask -TaskName "PersonalServer-WSL-KeepAlive"', needed)
        self.assertIn('"systemctl", "is-active", "--quiet", "k3s"', needed)
        self.assertNotIn('Get-ScheduledTask -TaskName "PersonalServer-WSL-KeepAlive"', targeted)
        self.assertIn("if (-not (Test-RecoveryActionNeeded $component))", cycle)
        self.assertLess(cycle.index("if (-not (Test-RecoveryActionNeeded $component))"), cycle.index("Register-RecoveryAttempt $component"))
        self.assertLess(cycle.index("Register-RecoveryAttempt $component"), cycle.index("Invoke-TargetedRecovery $component"))

    def test_tunnel_probe_uses_timeout_runner_without_writing_process_command_line(self):
        tunnel = SCRIPT[SCRIPT.index("function Test-CloudflareTunnelRunning") : SCRIPT.index("function Test-CloudflareTunnelService")]
        health = SCRIPT[SCRIPT.index("function Get-RecoveryHealth") : SCRIPT.index("function Save-RecoveryFailureState")]
        expected_probe = "pgrep -af '[c]loudflared.*tunnel run' >/dev/null"
        self.assertIn("Invoke-WslWithTimeout", tunnel)
        self.assertIn(expected_probe, tunnel)
        self.assertIn("Test-CloudflareTunnelRunning", health)

    def test_public_portal_health_probe_uses_a_bounded_wsl_curl_request(self):
        """A missing or unbounded public probe cannot detect an active disconnected Tunnel."""
        public_health = SCRIPT[
            SCRIPT.index("function Test-CloudflareTunnelRunning")
            : SCRIPT.index("function Get-RecoveryHealth")
        ]

        self.assertIn("function Test-PublicPortalHealth", public_health)
        self.assertIn("Invoke-WslWithTimeout", public_health)
        self.assertIn(
            '"curl", "--fail", "--silent", "--show-error", "--max-time", "15", "https://len.pe.kr/health"',
            public_health,
        )
        self.assertIn(' -Operation "Public Portal health probe"', public_health)

    def test_tunnel_health_requires_nodeport_and_public_health_after_local_tunnel_checks(self):
        """A healthy service/process alone must not hide a disconnected public Tunnel."""
        health = SCRIPT[
            SCRIPT.index("function Get-RecoveryHealth") : SCRIPT.index("function Save-RecoveryFailureState")
        ]
        nodeport_gate = 'if ($health.nodeport -eq "healthy") {'
        tunnel_condition = (
            'if ((Test-CloudflareTunnelService) -and (Test-CloudflareTunnelRunning) '
            '-and (Test-PublicPortalHealth)) {'
        )

        self.assertIn(nodeport_gate, health)
        self.assertIn(tunnel_condition, health)
        self.assertIn('$health.tunnel = "healthy"', health)
        self.assertLess(health.index('$health.nodeport = "healthy"'), health.index(nodeport_gate))
        self.assertLess(health.index(nodeport_gate), health.index(tunnel_condition))

    def test_nodeport_failure_defers_tunnel_without_alert_or_recovery_action(self):
        """A local NodePort fault must not consume the Tunnel alert or restart budget."""
        health = SCRIPT[
            SCRIPT.index("function Get-RecoveryHealth") : SCRIPT.index("function Save-RecoveryFailureState")
        ]
        notifier = SCRIPT[
            SCRIPT.index("function Update-TunnelTelegramNotification")
            : SCRIPT.index("function Test-CloudflareTunnelRunning")
        ]
        cycle = SCRIPT[
            SCRIPT.index("function Invoke-RecoveryCycle") : SCRIPT.index("function Install-EmergencyRebootTask")
        ]
        nodeport_gate = 'if ($health.nodeport -eq "healthy") {'

        self.assertIn('tunnel = "deferred"', health)
        self.assertIn(nodeport_gate, health)
        self.assertLess(health.index(nodeport_gate), health.index('$health.tunnel = "unhealthy"'))
        self.assertIn('if ($TunnelHealth -eq "deferred") {', notifier)
        self.assertLess(notifier.index('if ($TunnelHealth -eq "deferred") {'), notifier.index('if ($TunnelHealth -eq "healthy") {'))
        self.assertLess(cycle.index('if ($health[$component] -eq "deferred")'), cycle.index("Register-RecoveryFailure $component"))
        self.assertLess(cycle.index('if ($health[$component] -eq "deferred")'), cycle.index("Invoke-TargetedRecovery $component"))

    def test_public_only_tunnel_failure_uses_existing_threshold_then_forces_restart(self):
        """A disconnected public path must take the same bounded Tunnel restart path as a local failure."""
        action_needed = SCRIPT[
            SCRIPT.index("function Test-RecoveryActionNeeded") : SCRIPT.index("function Invoke-TargetedRecovery")
        ]
        targeted = SCRIPT[
            SCRIPT.index("function Invoke-TargetedRecovery") : SCRIPT.index("function Enter-RecoveryLock")
        ]
        start_tunnel = SCRIPT[
            SCRIPT.index("function Start-CloudflareTunnel") : SCRIPT.index("function Update-HostMetrics")
        ]
        cycle = SCRIPT[
            SCRIPT.index("function Invoke-RecoveryCycle") : SCRIPT.index("function Install-EmergencyRebootTask")
        ]

        self.assertIn('"tunnel" {\n            return $true', action_needed)
        self.assertIn('return (Start-CloudflareTunnel -ForceRestart)', targeted)
        self.assertIn('function Start-CloudflareTunnel([switch]$ForceRestart)', start_tunnel)
        self.assertIn('if (-not $ForceRestart) {', start_tunnel)
        self.assertIn("Cloudflare Tunnel service restart", start_tunnel)
        self.assertLess(cycle.index("Register-RecoveryFailure $component"), cycle.index("Test-RecoveryActionNeeded $component"))
        self.assertLess(cycle.index("Test-RecoveryActionNeeded $component"), cycle.index("Register-RecoveryAttempt $component"))
        self.assertIn("$RecoveryFailureThreshold = 2", SCRIPT)
        self.assertIn("$RecoveryMaxAttempts = 3", SCRIPT)

    def test_supervisor_continues_startup_when_initial_host_metrics_update_fails(self):
        """Telemetry failure must not suppress boot delay, bootstrap, or Daemon supervision."""
        supervisor = SCRIPT[
            SCRIPT.index("function Start-Supervisor") : SCRIPT.index("function Enter-DaemonLock")
        ]
        metrics_isolation = (
            'try {\n'
            '            Update-HostMetrics\n'
            '        } catch {\n'
            '            Write-Info "Initial host metrics update failed; continuing startup."\n'
            '        }'
        )

        self.assertIn(metrics_isolation, supervisor)
        self.assertLess(supervisor.index(metrics_isolation), supervisor.index("Waiting 120 seconds for WSL and Docker after logon."))
        self.assertLess(supervisor.index("Waiting 120 seconds for WSL and Docker after logon."), supervisor.index("Start-PersonalServerStack"))
        self.assertLess(supervisor.index("Start-PersonalServerStack"), supervisor.index("Start-Process"))

    def test_bootstrap_suppresses_tunnel_recovery_boolean_result(self):
        startup = SCRIPT[SCRIPT.index("function Start-PersonalServerStack") : SCRIPT.index("function Get-RecoveryHealth")]
        self.assertIn("[void](Start-CloudflareTunnel)", startup)

    def test_recovery_cycle_skips_the_fourth_action_before_invoking_targeted_recovery(self):
        budget = SCRIPT
        cycle = SCRIPT[SCRIPT.index("function Invoke-RecoveryCycle") : SCRIPT.index("function Install-ScheduledTask")]
        self.assertIn("function Test-RecoveryAttemptAvailable", budget)
        self.assertIn("if ($attemptCount -ge $RecoveryMaxAttempts)", budget)
        self.assertIn("if (-not (Test-RecoveryAttemptAvailable $component))", cycle)
        self.assertLess(
            cycle.index("if (-not (Test-RecoveryAttemptAvailable $component))"),
            cycle.index("Invoke-TargetedRecovery $component"),
        )

    def test_reserved_attempt_survives_targeted_action_exception_before_the_next_cycle(self):
        cycle = SCRIPT[SCRIPT.index("function Invoke-RecoveryCycle") : SCRIPT.index("function Install-ScheduledTask")]
        self.assertIn("Register-RecoveryAttempt $component", cycle)
        self.assertLess(cycle.index("Register-RecoveryAttempt $component"), cycle.index("Invoke-TargetedRecovery $component"))
        self.assertIn("finally {", cycle)
        self.assertIn("Exit-RecoveryLock $lockStream", cycle)

    def test_k3s_and_cutover_validate_and_preserve_the_opt_in_bridge_override(self):
        """Recovery must not recreate dependencies without their K3s bridge ports."""
        self.assertIn("validate_docker_bridge_gateway", WSL_SCRIPT)
        self.assertIn("DOCKER_BRIDGE_GATEWAY", WSL_SCRIPT)
        self.assertIn("docker network inspect bridge", WSL_SCRIPT)
        self.assertIn("docker-compose.portal-bridge.yml", WSL_SCRIPT)
        self.assertIn("-f \"$PORTAL_BRIDGE_COMPOSE_FILE\"", WSL_SCRIPT)
        self.assertIn("--no-deps --force-recreate $compose_services", WSL_SCRIPT)

    def test_compose_mode_does_not_require_or_export_the_bridge_override(self):
        """Ordinary Compose recovery stays independent of the cutover-only gateway."""
        runtime = WSL_SCRIPT[WSL_SCRIPT.index("start_runtime_services() {") :]
        compose_body = runtime[runtime.index("compose)") : runtime.index("cutover)")]
        self.assertNotIn("PORTAL_BRIDGE_COMPOSE_FILE", compose_body)
        self.assertNotIn("DOCKER_BRIDGE_GATEWAY", compose_body)

    def test_k3s_bootstrap_recreates_dependencies_with_validated_bridge_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            (data / "portal-runtime.mode").write_text("k3s\n", encoding="utf-8")
            bridge = root / "docker-compose.portal-bridge.yml"
            bridge.write_text("services: {}\n", encoding="utf-8")
            calls = root / "calls"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            (fake_bin / "docker").write_text(
                "#!/bin/sh\n"
                f"printf '%s|%s\\n' \"${{DOCKER_BRIDGE_GATEWAY:-unset}}\" \"$*\" >> '{calls}'\n"
                "if [ \"$1\" = network ] && [ \"$2\" = inspect ]; then echo 172.17.0.1; fi\n",
                encoding="utf-8",
            )
            (fake_bin / "curl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            for tool in fake_bin.iterdir():
                tool.chmod(0o755)

            result = subprocess.run(
                ["bash", str(ROOT / "scripts" / "windows-bootstrap.sh"), str(root)],
                env={**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"], "HOME": str(root)},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            recorded = calls.read_text(encoding="utf-8")
            self.assertIn(f"-f {bridge}", recorded)
            self.assertIn("172.17.0.1|compose", recorded)
            self.assertIn("up -d --no-deps caddy", recorded)
            self.assertNotIn("portal-web", recorded)


if __name__ == "__main__":
    unittest.main()
