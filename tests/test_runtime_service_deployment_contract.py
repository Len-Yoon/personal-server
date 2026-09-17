import os
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RuntimeServiceDeploymentContractTests(unittest.TestCase):
    def _run_health_check(self, runtime_state, crawler_present=None, crawler_ready="1", crawler_endpoint_port="8001"):
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
                "  printf '%s\\n' system-agent car-care-worker caddy homeops-executor portal-web\n"
                "  if [ \"$FAKE_CRAWLER_COMPOSE_PRESENT\" = 1 ]; then printf '%s\\n' crawler-worker; fi\n"
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
                "  *'deployment/crawler-worker'*'.spec.replicas'*) printf '%s\\n' 1 ;;\n"
                "  *'deployment/crawler-worker'*'.status.readyReplicas'*) printf '%s\\n' \"$FAKE_CRAWLER_READY\" ;;\n"
                "  *'deployment/crawler-worker'*'.status.availableReplicas'*) printf '%s\\n' 1 ;;\n"
                "  *'rollout status deployment/crawler-worker'*) exit 0 ;;\n"
                "  *'service/crawler-worker'*'.spec.selector.app\\.kubernetes\\.io/name'*) printf '%s\\n' crawler-worker ;;\n"
                "  *'service/crawler-worker'*'.spec.clusterIP'*) printf '%s\\n' 192.0.2.12 ;;\n"
                "  *'service/crawler-worker'*'.spec.ports[0].port'*) printf '%s\\n' 8001 ;;\n"
                "  *'service/crawler-worker'*'.spec.ports[0].targetPort'*) printf '%s\\n' http ;;\n"
                "  *'endpoints/crawler-worker'*'.ports[*].port'*) printf '%s\\n' \"$FAKE_CRAWLER_ENDPOINT_PORT\" ;;\n"
                "  *'endpoints/crawler-worker'*'.addresses[*].ip'*) printf '%s\\n' 198.51.100.12 ;;\n"
                "  *'pvc/crawler-worker-data'*) printf '%s\\n' Bound ;;\n"
                "  *'deployment/book-memo'*'.spec.replicas'*) printf '%s\\n' 1 ;;\n"
                "  *'deployment/book-memo'*'.status.readyReplicas'*) printf '%s\\n' 1 ;;\n"
                "  *'deployment/book-memo'*'.status.availableReplicas'*) printf '%s\\n' 1 ;;\n"
                "  *'rollout status deployment/book-memo'*) exit 0 ;;\n"
                "  *'service/book-memo'*'.spec.selector.app\\.kubernetes\\.io/name'*) printf '%s\\n' book-memo ;;\n"
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
                "  *'service/youtube-memo'*'.spec.selector.app\\.kubernetes\\.io/name'*) printf '%s\\n' youtube-memo ;;\n"
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
                    "FAKE_CRAWLER_COMPOSE_PRESENT": crawler_present if crawler_present is not None else ("0" if "crawler-worker=k3s" in runtime_state else "1"),
                    "FAKE_CRAWLER_READY": crawler_ready,
                    "FAKE_CRAWLER_ENDPOINT_PORT": crawler_endpoint_port,
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

    def test_crawler_k3s_health_uses_kubernetes_endpoint_not_docker_port(self):
        result, calls = self._run_health_check(
            "crawler-worker=k3s\nyoutube-memo=k3s\nbook-memo=k3s\n"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("service/crawler-worker", calls)
        self.assertIn("endpoints/crawler-worker", calls)
        self.assertIn("pvc/crawler-worker-data", calls)
        self.assertNotIn("127.0.0.1:8001/health", calls)

    def test_crawler_compose_health_keeps_docker_port(self):
        result, calls = self._run_health_check(
            "crawler-worker=compose\nyoutube-memo=k3s\nbook-memo=k3s\n"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("http://127.0.0.1:8001/health", calls)

    def test_crawler_k3s_health_rejects_running_compose_writer_or_unready_pod(self):
        for present, ready in (("1", "1"), ("0", "0")):
            with self.subTest(compose_present=present, ready=ready):
                result, calls = self._run_health_check(
                    "crawler-worker=k3s\nyoutube-memo=compose\nbook-memo=compose\n",
                    crawler_present=present,
                    crawler_ready=ready,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("127.0.0.1:8001/health", calls)

    def test_crawler_k3s_health_rejects_wrong_endpoint_port(self):
        result, calls = self._run_health_check(
            "crawler-worker=k3s\nyoutube-memo=compose\nbook-memo=compose\n",
            crawler_endpoint_port="8002",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("127.0.0.1:8001/health", calls)

    def test_caddy_news_config_accepts_runtime_upstream_without_compose_dependency(self):
        caddy = (ROOT / "caddy" / "Caddyfile").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.n100.yml").read_text(encoding="utf-8")
        caddy_definition = compose[compose.index("  caddy:") : compose.index("\nvolumes:")]

        self.assertIn("reverse_proxy {env.CRAWLER_WORKER_UPSTREAM}", caddy)
        self.assertIn("CRAWLER_WORKER_UPSTREAM: ${CRAWLER_WORKER_UPSTREAM:-crawler-worker:8001}", caddy_definition)
        self.assertNotIn("      crawler-worker:\n        condition: service_healthy", caddy_definition)


class CrawlerRuntimeCaddyTests(unittest.TestCase):
    def _run_runtime(self, script, portal_mode, crawler_mode="k3s", broken_service="crawler-worker", broken_field=None, broken_value=""):
        """Run real entrypoints with only host/Git/Kubernetes boundaries replaced."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            (root / ".env").write_text("", encoding="utf-8")
            state = root / "data" / "portal-web-state"
            state.mkdir(parents=True)
            with sqlite3.connect(state / "homeops.sqlite3") as db:
                db.execute("CREATE TABLE fixture (id INTEGER)")
            (root / "data" / "portal-runtime.mode").write_text(portal_mode + "\n", encoding="utf-8")
            (root / "docker-compose.portal-bridge.yml").write_text("services: {}\n", encoding="utf-8")
            calls = root / "calls"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fixtures = {
                "crawler-worker": {"ip": "192.0.2.12", "port": "8001"},
                "youtube-memo": {"ip": "192.0.2.11", "port": "8002"},
                "book-memo": {"ip": "192.0.2.10", "port": "8003"},
            }
            (fake_bin / "git").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (fake_bin / "python3").write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  *runtime-service-state-reader.py) printf '%s\\n' \"$FAKE_RUNTIME_STATE\" ;;\n"
                "  scripts/maintenance.py) exit 0 ;;\n"
                f"  *) exec '{sys.executable}' \"$@\" ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            (fake_bin / "docker").write_text(
                "#!/bin/sh\n"
                "printf 'docker crawler=%s youtube=%s book=%s args=%s\\n' \"${CRAWLER_WORKER_UPSTREAM:-unset}\" \"${YOUTUBE_MEMO_UPSTREAM:-unset}\" \"${BOOK_MEMO_UPSTREAM:-unset}\" \"$*\" >> \"$FAKE_CALLS\"\n"
                "if [ \"$1\" = network ] && [ \"$2\" = inspect ]; then printf '%s\\n' 172.17.0.1; fi\n",
                encoding="utf-8",
            )
            (fake_bin / "sudo").write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys\n"
                "args = sys.argv[1:]\n"
                "with open(os.environ['FAKE_CALLS'], 'a') as log: log.write('sudo ' + ' '.join(args) + '\\n')\n"
                f"fixtures = {fixtures!r}\n"
                "resource = next((a for a in args if a.startswith(('deployment/', 'service/', 'endpoints/', 'pvc/'))), '')\n"
                "name = resource.partition('/')[2].removesuffix('-data')\n"
                "if name not in fixtures: sys.exit(9)\n"
                "data = fixtures[name]\n"
                "field = next((a.removeprefix('jsonpath=') for a in args if a.startswith('jsonpath=')), 'rollout')\n"
                "values = {\n"
                "  '{.spec.replicas}': '1', '{.status.readyReplicas}': '1', '{.status.availableReplicas}': '1',\n"
                "  '{.spec.selector.app\\\\.kubernetes\\\\.io/name}': name, '{.spec.clusterIP}': data['ip'],\n"
                "  '{.spec.ports[0].port}': data['port'], '{.spec.ports[0].targetPort}': 'http',\n"
                "  '{.subsets[*].addresses[*].ip}': '198.51.100.12', '{.subsets[*].ports[*].port}': data['port'],\n"
                "  '{.status.phase}': 'Bound', 'rollout': 'deployment successfully rolled out'}\n"
                "if field not in values: sys.exit(8)\n"
                "broken = json.loads(os.environ['FAKE_BROKEN'])\n"
                "if name == broken['service'] and field == broken['field']:\n"
                "  if field == 'rollout': sys.exit(1)\n"
                "  print(broken['value'])\n"
                "else: print(values[field])\n",
                encoding="utf-8",
            )
            (fake_bin / "curl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            for tool in fake_bin.iterdir():
                tool.chmod(0o755)
            result = subprocess.run(
                ["bash", str(ROOT / "scripts" / script), str(root)],
                env={
                    **os.environ,
                    "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
                    "FAKE_RUNTIME_STATE": f"crawler-worker={crawler_mode}\nyoutube-memo=k3s\nbook-memo=k3s",
                    "FAKE_CALLS": str(calls),
                    "FAKE_BROKEN": json.dumps({"service": broken_service, "field": broken_field, "value": broken_value}),
                    "CRAWLER_WORKER_UPSTREAM": "untrusted:9999",
                    "HOMEOPS_SCHEDULER_SECRET": "",
                },
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            return result, calls.read_text(encoding="utf-8")

    def test_all_runtime_entrypoints_use_verified_crawler_upstream_and_exclude_docker_writer(self):
        for script in ("deploy-n100.sh", "windows-bootstrap.sh"):
            for mode in ("compose", "cutover", "k3s"):
                with self.subTest(script=script, portal_mode=mode):
                    result, calls = self._run_runtime(script, mode)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    docker_up = [line for line in calls.splitlines() if line.startswith("docker ") and " up " in line]
                    caddy = [line for line in docker_up if line.endswith(" caddy")]
                    self.assertEqual(len(caddy), 1, calls)
                    self.assertIn("crawler=192.0.2.12:8001 youtube=192.0.2.11:8002 book=192.0.2.10:8003", caddy[0])
                    self.assertNotIn("crawler-worker", "\n".join(docker_up))
                    self.assertIn("service/crawler-worker", calls)
                    self.assertIn("pvc/crawler-worker-data", calls)

    def test_compose_crawler_replaces_inherited_upstream_with_docker_hostname(self):
        for script in ("deploy-n100.sh", "windows-bootstrap.sh"):
            with self.subTest(script=script):
                result, calls = self._run_runtime(script, "k3s", crawler_mode="compose")
                self.assertEqual(result.returncode, 0, result.stderr)
                caddy = [line for line in calls.splitlines() if line.startswith("docker ") and line.endswith(" caddy")]
                self.assertEqual(len(caddy), 1, calls)
                self.assertIn("crawler=crawler-worker:8001", caddy[0])
                self.assertNotIn("service/crawler-worker", calls)

    def test_invalid_crawler_resource_preserves_running_caddy_in_every_portal_mode(self):
        invalid = (
            ("{.spec.replicas}", "0"),
            ("{.status.readyReplicas}", "0"),
            ("{.status.availableReplicas}", "0"),
            ("rollout", ""),
            ("{.spec.selector.app\\.kubernetes\\.io/name}", "other-service"),
            ("{.spec.clusterIP}", ""),
            ("{.spec.clusterIP}", "None"),
            ("{.spec.ports[0].port}", "8002"),
            ("{.spec.ports[0].targetPort}", "wrong"),
            ("{.subsets[*].addresses[*].ip}", ""),
            ("{.subsets[*].ports[*].port}", "8002"),
            ("{.status.phase}", "Pending"),
        )
        for script in ("deploy-n100.sh", "windows-bootstrap.sh"):
            for mode in ("compose", "cutover", "k3s"):
                for field, value in invalid:
                    with self.subTest(script=script, mode=mode, field=field, value=value):
                        result, calls = self._run_runtime(script, mode, broken_field=field, broken_value=value)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertFalse(any(line.startswith("docker ") and line.endswith(" caddy") for line in calls.splitlines()), calls)

    def test_other_unready_k3s_services_also_preserve_caddy_after_crawler_migration(self):
        for script in ("deploy-n100.sh", "windows-bootstrap.sh"):
            for service in ("youtube-memo", "book-memo"):
                with self.subTest(script=script, service=service):
                    result, calls = self._run_runtime(script, "k3s", broken_service=service, broken_field="{.status.phase}", broken_value="Pending")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(any(line.startswith("docker ") and line.endswith(" caddy") for line in calls.splitlines()), calls)


if __name__ == "__main__":
    unittest.main()
