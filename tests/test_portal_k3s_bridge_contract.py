import importlib
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._test_support import prepare_service_import


ROOT = Path(__file__).resolve().parents[1]


class PortalK3sBridgeContractTests(unittest.TestCase):
    def test_service_health_uses_configured_bridge_endpoints(self):
        """Fails if a K3s Portal continues to call Docker-only DNS names."""
        environment = {
            "NEWS_HEALTH_URL": "http://compose-crawler:8001/health",
            "YOUTUBE_HEALTH_URL": "http://compose-youtube:8002/health",
            "BOOKS_HEALTH_URL": "http://compose-book:8003/health",
            "SYSTEM_AGENT_HEALTH_URL": "http://compose-system-agent:8010/health",
        }
        original = {key: os.environ.get(key) for key in environment}
        try:
            os.environ.update(environment)
            prepare_service_import("portal-web")
            import app.services.system_status as system_status

            system_status = importlib.reload(system_status)
            with patch("app.services.system_status.urlopen", side_effect=OSError("down")):
                services = system_status.get_service_health(timeout=0.01)

            self.assertEqual(
                [service["url"] for service in services],
                list(environment.values()),
            )
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_compose_contract_separates_portal_state_and_exposes_only_bridge_ports(self):
        """Fails if Portal state remains shared logs or a bridge depends on Docker DNS."""
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        n100 = (ROOT / "docker-compose.n100.yml").read_text(encoding="utf-8")
        bridge = (ROOT / "docker-compose.portal-bridge.yml").read_text(encoding="utf-8")

        self.assertIn("./data/portal-web-state:/var/lib/portal", compose)
        for variable, path in (
            ("HOMEOPS_DB_PATH", "/var/lib/portal/homeops.sqlite3"),
            ("SECURITY_LOG_PATH", "/var/lib/portal/security-events.txt"),
            ("AUTH_RATE_LIMIT_STATE_PATH", "/var/lib/portal/auth-rate-limit-state.json"),
        ):
            self.assertIn(f"{variable}={path}", compose)

        self.assertNotIn("DOCKER_BRIDGE_GATEWAY:?", n100)
        # Regular Compose only binds local diagnostic ports.  The Docker bridge
        # listener is opt-in so an unset gateway cannot break normal startup.
        for port in (
            "127.0.0.1:18010:8010",
            "127.0.0.1:18011:8011",
            "127.0.0.1:8001:8001",
            "127.0.0.1:8002:8002",
            "127.0.0.1:8003:8003",
        ):
            self.assertIn(port, n100)
        for port in ("18010:8010", "18011:8011", "18001:8001", "18002:8002", "18003:8003"):
            self.assertIn(f"${{DOCKER_BRIDGE_GATEWAY}}:{port}", bridge)

    def test_cutover_contract_copies_state_to_its_own_pvc_and_uses_bridge_services(self):
        """Fails if state is written to an image layer or dependencies use host-network shortcuts."""
        script = (ROOT / "infra/k8s/tools/portal-cutover.sh").read_text(encoding="utf-8")

        for required in (
            "PORTAL_STATE_SOURCE_DIR",
            "portal-web-state",
            "STATE_PVC",
            "HOMEOPS_DB_PATH",
            "/var/lib/portal/homeops.sqlite3",
            "SECURITY_LOG_PATH",
            "/var/lib/portal/security-events.txt",
            "AUTH_RATE_LIMIT_STATE_PATH",
            "/var/lib/portal/auth-rate-limit-state.json",
            "compose-system-agent",
            "compose-homeops-executor",
            "compose-crawler",
            "compose-youtube",
            "compose-book",
        ):
            self.assertIn(required, script)
        self.assertNotIn("hostNetwork: true", script)

    def test_homeops_executor_can_exclude_k3s_portal_from_docker_operations(self):
        """Fails if HomeOps keeps diagnosing or restarting the former Docker Portal writer."""
        original = os.environ.get("HOMEOPS_DOCKER_MANAGED_SERVICES")
        try:
            os.environ["HOMEOPS_DOCKER_MANAGED_SERVICES"] = "system-agent,crawler-worker,book-memo"
            prepare_service_import("homeops-executor")
            from app.services import docker_ops

            docker_ops = importlib.reload(docker_ops)
            self.assertEqual(
                docker_ops.allowed_services(),
                frozenset({"system-agent", "crawler-worker", "book-memo"}),
            )
            with self.assertRaisesRegex(ValueError, "service_not_allowed"):
                docker_ops._require_allowed_service("portal-web")
        finally:
            if original is None:
                os.environ.pop("HOMEOPS_DOCKER_MANAGED_SERVICES", None)
            else:
                os.environ["HOMEOPS_DOCKER_MANAGED_SERVICES"] = original

    def test_bootstrap_uses_runtime_marker_to_preserve_the_single_writer_boundary(self):
        """Fails if a recovery loop can recreate Compose Portal during K3s cutover."""
        bootstrap = (ROOT / "scripts/windows-bootstrap.sh").read_text(encoding="utf-8")

        for mode in ("compose", "cutover", "k3s"):
            self.assertIn(f"{mode})", bootstrap)
        self.assertIn("PORTAL_RUNTIME_MARKER", bootstrap)
        self.assertIn("--no-deps caddy", bootstrap)
        self.assertIn("127.0.0.1:30080/internal/homeops/scan", bootstrap)
        self.assertIn("HOMEOPS_DOCKER_MANAGED_SERVICES", bootstrap)


if __name__ == "__main__":
    unittest.main()
