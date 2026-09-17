import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RuntimeServiceDeploymentContractTests(unittest.TestCase):
    def _run_health_check(self, runtime_state):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "data" / "portal-runtime.mode").write_text("compose\n", encoding="utf-8")
            calls = root / "calls"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            (fake_bin / "python3").write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$FAKE_RUNTIME_STATE\"\n",
                encoding="utf-8",
            )
            (fake_bin / "docker").write_text(
                "#!/bin/sh\n"
                f"printf 'docker %s\\n' \"$*\" >> '{calls}'\n"
                "if [ \"$1\" = compose ]; then\n"
                "  printf '%s\\n' system-agent crawler-worker car-care-worker caddy homeops-executor portal-web\n"
                "  if [ \"${FAKE_YOUTUBE_MEMO_COMPOSE_PRESENT:-1}\" = 1 ]; then printf '%s\\n' youtube-memo; fi\n"
                "  if [ \"${FAKE_BOOK_MEMO_COMPOSE_PRESENT:-1}\" = 1 ]; then printf '%s\\n' book-memo; fi\n"
                "elif [ \"$1\" = inspect ]; then\n"
                "  printf '%s\\n' healthy\n"
                "fi\n",
                encoding="utf-8",
            )
            (fake_bin / "sudo").write_text(
                "#!/bin/sh\n"
                f"printf 'sudo %s\\n' \"$*\" >> '{calls}'\n"
                "case \"$*\" in\n"
                "  *'deployment/book-memo'*'.spec.replicas'*) printf '%s\\n' 1 ;;\n"
                "  *'deployment/book-memo'*'.status.readyReplicas'*) printf '%s\\n' 1 ;;\n"
                "  *'deployment/book-memo'*'.status.availableReplicas'*) printf '%s\\n' 1 ;;\n"
                "  *'rollout status deployment/book-memo'*) exit 0 ;;\n"
                "  *'service/book-memo'*'.spec.selector.app\\\\.kubernetes\\\\.io/name'*) printf '%s\\n' book-memo ;;\n"
                "  *'service/book-memo'*'.spec.clusterIP'*) printf '%s\\n' 192.0.2.10 ;;\n"
                "  *'service/book-memo'*'.spec.ports[0].port'*) printf '%s\\n' 8003 ;;\n"
                "  *'service/book-memo'*'.spec.ports[0].targetPort'*) printf '%s\\n' http ;;\n"
                "  *'endpoints/book-memo'*'.ports[*].port'*) printf '%s\\n' 8003 ;;\n"
                "  *'endpoints/book-memo'*'.addresses[*].ip'*) printf '%s\\n' 198.51.100.10 ;;\n"
                "  *'pvc/book-memo-data'*) printf '%s\\n' Bound ;;\n"
                "  *'deployment/youtube-memo'*'.spec.replicas'*) printf '%s\\n' 1 ;;\n"
                "  *'deployment/youtube-memo'*'.status.readyReplicas'*) printf '%s\\n' 1 ;;\n"
                "  *'deployment/youtube-memo'*'.status.availableReplicas'*) printf '%s\\n' 1 ;;\n"
                "  *'rollout status deployment/youtube-memo'*) exit 0 ;;\n"
                "  *'service/youtube-memo'*'.spec.selector.app\\\\.kubernetes\\\\.io/name'*) printf '%s\\n' youtube-memo ;;\n"
                "  *'service/youtube-memo'*'.spec.clusterIP'*) printf '%s\\n' 192.0.2.11 ;;\n"
                "  *'service/youtube-memo'*'.spec.ports[0].port'*) printf '%s\\n' 8002 ;;\n"
                "  *'service/youtube-memo'*'.spec.ports[0].targetPort'*) printf '%s\\n' http ;;\n"
                "  *'endpoints/youtube-memo'*'.ports[*].port'*) printf '%s\\n' 8002 ;;\n"
                "  *'endpoints/youtube-memo'*'.addresses[*].ip'*) printf '%s\\n' 198.51.100.11 ;;\n"
                "  *'pvc/youtube-memo-data'*) printf '%s\\n' Bound ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            (fake_bin / "curl").write_text(
                "#!/bin/sh\n"
                f"printf 'curl %s\\n' \"$*\" >> '{calls}'\n",
                encoding="utf-8",
            )
            for tool in fake_bin.iterdir():
                tool.chmod(0o755)

            result = subprocess.run(
                ["bash", str(ROOT / "scripts" / "verify-n100-deployment-health.sh"), str(root)],
                env={
                    **os.environ,
                    "FAKE_RUNTIME_STATE": runtime_state,
                    "FAKE_YOUTUBE_MEMO_COMPOSE_PRESENT": "0" if "youtube-memo=k3s" in runtime_state else "1",
                    "FAKE_BOOK_MEMO_COMPOSE_PRESENT": "0" if "book-memo=k3s" in runtime_state else "1",
                    "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
                },
                capture_output=True,
                text=True,
                check=False,
            )
            return result, calls.read_text(encoding="utf-8")

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

    def test_book_memo_k3s_health_uses_kubernetes_resources_not_docker_loopback(self):
        """K3s Book Memo must not be checked through the retired Compose port."""
        text = (ROOT / "scripts" / "verify-n100-deployment-health.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn('"service/$service"', text)
        self.assertIn('"endpoints/$service"', text)
        self.assertIn('"pvc/$pvc_name"', text)
        self.assertIn("book-memo) expected_port=8003; pvc_name=book-memo-data", text)
        self.assertIn(".status.phase", text)
        self.assertIn(".subsets[*].addresses[*].ip", text)

        common_urls = text[text.index("for url in") : text.index("for attempt in")]
        self.assertIn('if [[ "$BOOK_MEMO_RUNTIME_MODE" != k3s ]]', common_urls)
        self.assertIn("http://127.0.0.1:8003/health", common_urls)

    def test_book_memo_k3s_health_uses_kubernetes_endpoint_not_docker_port(self):
        result, calls = self._run_health_check(
            "crawler-worker=compose\nyoutube-memo=compose\nbook-memo=k3s\n"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("service/book-memo", calls)
        self.assertIn("endpoints/book-memo", calls)
        self.assertIn("pvc/book-memo-data", calls)
        self.assertIn(".spec.selector.app\\.kubernetes\\.io/name", calls)
        self.assertIn(".targetPort", calls)
        self.assertIn(".ports[*].port", calls)
        self.assertNotIn("curl --fail --silent --show-error --retry-all --retry-connrefused --retry 6 --retry-delay 5 http://127.0.0.1:8003/health", calls)
        self.assertNotIn("docker inspect --format {{.State.Health.Status}} book-memo", calls)

    def test_book_memo_compose_health_keeps_docker_loopback_check(self):
        result, calls = self._run_health_check(
            "crawler-worker=compose\nyoutube-memo=compose\nbook-memo=compose\n"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("curl --fail --silent --show-error --retry-all --retry-connrefused --retry 6 --retry-delay 5 http://127.0.0.1:8003/health", calls)

    def test_youtube_memo_k3s_health_uses_kubernetes_endpoint_not_docker_port(self):
        result, calls = self._run_health_check(
            "crawler-worker=compose\nyoutube-memo=k3s\nbook-memo=compose\n"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("service/youtube-memo", calls)
        self.assertIn("endpoints/youtube-memo", calls)
        self.assertIn("pvc/youtube-memo-data", calls)
        self.assertIn(".spec.selector.app\\.kubernetes\\.io/name", calls)
        self.assertIn(".targetPort", calls)
        self.assertNotIn("http://127.0.0.1:8002/health", calls)
        self.assertNotIn("docker inspect --format {{.State.Health.Status}} youtube-memo", calls)

    def test_caddy_uses_runtime_selected_youtube_memo_upstream(self):
        caddy = (ROOT / "caddy" / "Caddyfile").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.n100.yml").read_text(encoding="utf-8")

        self.assertIn("reverse_proxy {env.YOUTUBE_MEMO_UPSTREAM}", caddy)
        self.assertIn("YOUTUBE_MEMO_UPSTREAM: ${YOUTUBE_MEMO_UPSTREAM:-youtube-memo:8002}", compose)
        caddy_definition = compose[compose.index("  caddy:") : compose.index("volumes:")]
        self.assertNotIn("      youtube-memo:\n        condition: service_healthy", caddy_definition)


if __name__ == "__main__":
    unittest.main()
