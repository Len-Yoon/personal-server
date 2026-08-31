import os
import subprocess
import tempfile
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

    def test_powershell_daemon_keeps_cloudflare_tunnel_in_wsl_process(self):
        self.assertIn("Start-Process -FilePath 'wsl.exe'", SCRIPT)
        self.assertIn("'cloudflared', 'tunnel', 'run'", SCRIPT)
        self.assertNotIn("nohup cloudflared tunnel run", WSL_SCRIPT)

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
