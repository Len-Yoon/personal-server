import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RuntimeServiceDeploymentContractTests(unittest.TestCase):
    def test_startup_scripts_source_runtime_state_and_define_k3s_filter(self):
        for name in ("deploy-n100.sh", "windows-bootstrap.sh"):
            text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertIn("runtime-service-state.sh", text)
            self.assertIn("load_service_runtime_state", text)
            self.assertIn("k3s", text)
            self.assertIn("crawler-worker", text)
            self.assertIn("youtube-memo", text)
            self.assertIn("book-memo", text)

    def test_health_checks_k3s_readiness_and_rejects_compose_writer(self):
        text = (ROOT / "scripts" / "verify-n100-deployment-health.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("runtime-service-state.sh", text)
        self.assertIn("kubectl", text)
        self.assertIn("rollout status", text)
        self.assertIn("Compose writer is running during K3s mode", text)
        self.assertIn("crawler-worker", text)
        self.assertIn("youtube-memo", text)
        self.assertIn("book-memo", text)

    def test_k3s_targets_are_removed_from_homeops_control_lists(self):
        for name in ("deploy-n100.sh", "windows-bootstrap.sh"):
            text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertIn("HOMEOPS_DOCKER_MANAGED_SERVICES", text)
            self.assertIn("EXPECTED_CONTAINERS", text)
            self.assertIn("runtime_service_mode", text)
        bootstrap = (ROOT / "scripts" / "windows-bootstrap.sh").read_text(encoding="utf-8")
        runtime = bootstrap[bootstrap.index("start_runtime_services()") :]
        k3s = runtime[runtime.index("k3s)") :]
        self.assertIn("set_cutover_homeops_lists", k3s)

    def test_mixed_portal_compose_mode_rebuilds_homeops_lists(self):
        text = (ROOT / "scripts" / "windows-bootstrap.sh").read_text(encoding="utf-8")
        compose = text[text.index("compose)") : text.index("cutover)")]
        self.assertIn("set_compose_homeops_lists", compose)

    def test_k3s_health_checks_desired_and_ready_replicas_with_sudo_k3s(self):
        text = (ROOT / "scripts" / "verify-n100-deployment-health.sh").read_text(encoding="utf-8")
        self.assertIn("sudo k3s kubectl", text)
        self.assertIn(".spec.replicas", text)
        self.assertIn(".status.readyReplicas", text)
        self.assertIn(".status.availableReplicas", text)


if __name__ == "__main__":
    unittest.main()
