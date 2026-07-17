import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts" / "windows-bootstrap.ps1").read_text(encoding="utf-8-sig")
WSL_SCRIPT = (ROOT / "scripts" / "windows-bootstrap.sh").read_text(encoding="utf-8-sig")


class WindowsBootstrapTests(unittest.TestCase):
    def test_uses_schtasks_when_scheduled_task_cmdlets_are_unavailable(self):
        self.assertIn("schtasks.exe /Create", SCRIPT)
        self.assertIn("/SC ONSTART", SCRIPT)
        self.assertIn("/RP *", SCRIPT)
        self.assertIn("/F", SCRIPT)
        self.assertIn("schtasks.exe /Query", SCRIPT)

    def test_recovery_starts_the_news_crawler_and_other_services(self):
        self.assertIn("docker-compose.yml", WSL_SCRIPT)
        self.assertIn("up -d", WSL_SCRIPT)
        self.assertIn("crawler-worker", WSL_SCRIPT)
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

    def test_powershell_daemon_keeps_cloudflare_tunnel_in_wsl_process(self):
        self.assertIn("Start-Process -FilePath 'wsl.exe'", SCRIPT)
        self.assertIn("'cloudflared', 'tunnel', 'run'", SCRIPT)
        self.assertNotIn("nohup cloudflared tunnel run", WSL_SCRIPT)


if __name__ == "__main__":
    unittest.main()
