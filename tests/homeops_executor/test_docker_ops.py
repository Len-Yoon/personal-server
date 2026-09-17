import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._test_support import prepare_service_import


class FakeContainer:
    def __init__(self, name: str = "crawler-worker"):
        self.name = name
        self.status = "running"
        self.attrs = {
            "State": {
                "Status": "running",
                "Health": {"Status": "healthy"},
                "ExitCode": 0,
                "StartedAt": "2026-08-19T00:00:00Z",
            }
        }
        self.restart_calls: list[int] = []
        self.logs_calls = 0
        self.stats_calls = 0

    def restart(self, timeout: int):
        self.restart_calls.append(timeout)

    def reload(self):
        return None

    def logs(self, **kwargs):
        self.logs_calls += 1
        return b"worker ready\nAuthorization: Bearer should-not-be-masked-here\n"

    def stats(self, stream=False):
        self.stats_calls += 1
        return {
            "memory_stats": {"usage": 45, "limit": 100},
            "cpu_stats": {"cpu_usage": {"total_usage": 200, "percpu_usage": [50, 50]}, "system_cpu_usage": 2_000},
            "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1_000},
        }


class FakeContainers:
    def __init__(self):
        self.filters: list[dict[str, str]] = []
        self.container = FakeContainer()

    def list(self, filters: dict[str, str], all: bool = False):
        self.filters.append(filters)
        return [self.container]


class FakeDockerClient:
    def __init__(self):
        self.containers = FakeContainers()


class DockerOpsTests(unittest.TestCase):
    def test_docker_client_uses_restricted_socket_proxy_endpoint(self):
        prepare_service_import("homeops-executor")
        from app.services import docker_ops

        created_clients = []

        class DockerClient:
            def __init__(self, *, base_url: str):
                self.base_url = base_url
                created_clients.append(self)

        def from_env():
            raise AssertionError("raw Docker environment discovery must not be used")

        DockerModule = type(
            "DockerModule",
            (),
            {
                "DockerClient": DockerClient,
                "from_env": staticmethod(from_env),
            },
        )

        with patch.dict(sys.modules, {"docker": DockerModule}):
            with patch.dict(
                os.environ,
                {"HOMEOPS_DOCKER_HOST": "tcp://docker-socket-proxy:2375"},
                clear=False,
            ):
                result = docker_ops._docker_client()

        self.assertEqual(len(created_clients), 1)
        self.assertIs(result, created_clients[0])
        self.assertEqual(result.base_url, "tcp://docker-socket-proxy:2375")

    def test_all_diagnostics_returns_allowlist_in_name_order(self):
        from app.services import docker_ops

        client = FakeDockerClient()

        result = docker_ops.collect_all_diagnostics(client=client)

        self.assertEqual(
            [item["service"] for item in result],
            sorted(docker_ops.ALLOWED_SERVICES),
        )

    def test_all_diagnostics_uses_lightweight_container_state_only(self):
        from app.services import docker_ops

        client = FakeDockerClient()

        result = docker_ops.collect_all_diagnostics(client=client)

        self.assertTrue(all(item["logs"] == [] for item in result))
        self.assertEqual(client.containers.container.logs_calls, 0)
        self.assertEqual(client.containers.container.stats_calls, 0)
        self.assertEqual(
            client.containers.filters,
            [
                {"label": f"com.docker.compose.service={service}"}
                for service in sorted(docker_ops.ALLOWED_SERVICES)
            ],
        )

    def test_all_diagnostics_keeps_other_results_when_one_service_cannot_be_collected(self):
        from app.services import docker_ops

        class PartiallyFailingContainers:
            def list(self, filters, all=False):
                service = filters["label"].removeprefix("com.docker.compose.service=")
                if service == "caddy":
                    raise RuntimeError("docker daemon temporary failure")
                return [FakeContainer(name=service)]

        class PartiallyFailingClient:
            def __init__(self):
                self.containers = PartiallyFailingContainers()

        result = docker_ops.collect_all_diagnostics(client=PartiallyFailingClient())
        by_service = {item["service"]: item for item in result}

        self.assertEqual(by_service["crawler-worker"]["container"]["status"], "running")
        self.assertEqual(by_service["caddy"]["container"]["status"], "unknown")
        self.assertEqual(by_service["caddy"]["error"], "diagnostic_unavailable")

    def test_restart_all_places_executor_last(self):
        from app.services import docker_ops

        client = FakeDockerClient()

        result = docker_ops.restart_all_services(client=client)

        expected_services = sorted(docker_ops.ALLOWED_SERVICES - {"portal-web", "caddy", "homeops-executor"}) + ["portal-web", "caddy", "homeops-executor"]
        self.assertEqual([item["service"] for item in result], expected_services)
        self.assertEqual(
            client.containers.filters,
            [
                {"label": f"com.docker.compose.service={service}"}
                for service in expected_services
            ],
        )

    def test_restart_all_continues_after_failure_and_restarts_executor_last(self):
        from app.services import docker_ops

        class FailingContainer(FakeContainer):
            def restart(self, timeout: int):
                if self.name == "caddy":
                    raise RuntimeError("docker daemon unavailable")
                super().restart(timeout)

        class FailingContainers:
            def __init__(self):
                self.filters: list[dict[str, str]] = []

            def list(self, filters: dict[str, str], all: bool = False):
                self.filters.append(filters)
                service = filters["label"].removeprefix("com.docker.compose.service=")
                return [FailingContainer(name=service)]

        class FailingDockerClient:
            def __init__(self):
                self.containers = FailingContainers()

        client = FailingDockerClient()

        result = docker_ops.restart_all_services(client=client)

        expected_services = [
            "book-memo",
            "crawler-worker",
            "system-agent",
            "youtube-memo",
            "portal-web",
            "caddy",
            "homeops-executor",
        ]
        self.assertEqual([item["service"] for item in result], expected_services)
        self.assertEqual(
            result[5],
            {
                "service": "caddy",
                "status": "failed",
                "error": "docker daemon unavailable",
            },
        )
        self.assertEqual(result[-1]["service"], "homeops-executor")
        self.assertEqual(
            client.containers.filters,
            [
                {"label": f"com.docker.compose.service={service}"}
                for service in expected_services
            ],
        )

    def test_diagnostics_rejects_service_outside_allowlist(self):
        from app.services import docker_ops

        with self.assertRaises(ValueError):
            docker_ops.collect_diagnostics("mysql-test", client=FakeDockerClient())

    def test_diagnostics_allows_every_personal_server_service(self):
        from app.services import docker_ops

        self.assertIn("portal-web", docker_ops.ALLOWED_SERVICES)
        self.assertIn("caddy", docker_ops.ALLOWED_SERVICES)

    def test_restart_uses_allowed_service_and_docker_restart(self):
        from app.services import docker_ops

        client = FakeDockerClient()

        result = docker_ops.restart_service("crawler-worker", client=client)

        self.assertEqual(
            client.containers.filters,
            [{"label": "com.docker.compose.service=crawler-worker"}],
        )
        self.assertEqual(client.containers.container.restart_calls, [10])
        self.assertEqual(result["service"], "crawler-worker")
        self.assertEqual(result["status"], "running")

    def test_diagnostics_finds_a_compose_service_when_container_name_is_generated(self):
        from app.services import docker_ops

        class ComposeServiceContainers:
            def __init__(self):
                self.filters = []
                self.container = FakeContainer(name="personal-server-caddy-1")

            def get(self, _name):
                raise AssertionError("generated Compose names must not be used as a fixed lookup")

            def list(self, filters, all: bool = False):
                self.filters.append(filters)
                return [self.container]

        class ComposeServiceClient:
            def __init__(self):
                self.containers = ComposeServiceContainers()

        client = ComposeServiceClient()

        result = docker_ops.collect_diagnostics("caddy", client=client)

        self.assertEqual(result["service"], "caddy")
        self.assertEqual(
            client.containers.filters,
            [{"label": "com.docker.compose.service=caddy"}],
        )

    def test_diagnostics_limits_log_tail_and_size(self):
        from app.services import docker_ops

        result = docker_ops.collect_diagnostics("crawler-worker", client=FakeDockerClient())

        self.assertEqual(result["service"], "crawler-worker")
        self.assertEqual(result["logs"], ["worker ready", "Authorization: Bearer should-not-be-masked-here"])
        self.assertEqual(result["container"]["health"], "healthy")
        self.assertEqual(result["container"]["cpu_percent"], 20.0)
        self.assertEqual(result["container"]["memory_percent"], 45.0)

    def test_executor_rejects_missing_shared_secret(self):
        from fastapi.testclient import TestClient
        from app.main import app

        with patch.dict("os.environ", {"HOMEOPS_EXECUTOR_SHARED_SECRET": "shared"}, clear=False):
            response = TestClient(app).get("/v1/diagnostics/crawler-worker")

        self.assertEqual(response.status_code, 403)

    def test_executor_returns_all_diagnostics_with_shared_secret(self):
        from fastapi.testclient import TestClient
        from app.main import app

        diagnostics = [{"service": "caddy", "container": {}, "logs": []}]
        with patch.dict("os.environ", {"HOMEOPS_EXECUTOR_SHARED_SECRET": "shared"}, clear=False):
            with patch("app.main.docker_ops.collect_all_diagnostics", return_value=diagnostics):
                response = TestClient(app).get(
                    "/v1/diagnostics",
                    headers={"X-HomeOps-Executor-Secret": "shared"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), diagnostics)

    def test_executor_rejects_admin_password_when_shared_secret_is_missing(self):
        from fastapi.testclient import TestClient
        from app.main import app

        diagnostics = [{"service": "caddy", "container": {}, "logs": []}]
        with patch.dict(
            "os.environ",
            {"HOMEOPS_EXECUTOR_SHARED_SECRET": "", "ADMIN_STATUS_PASSWORD": "admin-secret"},
            clear=False,
        ):
            with patch(
                "app.main.docker_ops.collect_all_diagnostics",
                return_value=diagnostics,
            ) as collect_all_diagnostics:
                response = TestClient(app).get(
                    "/v1/diagnostics",
                    headers={"X-HomeOps-Executor-Secret": "admin-secret"},
                )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"detail": "executor_access_denied"})
        collect_all_diagnostics.assert_not_called()

    def test_executor_rejects_restart_all_without_shared_secret(self):
        from fastapi.testclient import TestClient
        from app.main import app

        with patch.dict("os.environ", {"HOMEOPS_EXECUTOR_SHARED_SECRET": "shared"}, clear=False):
            response = TestClient(app).post("/v1/restarts/all")

        self.assertEqual(response.status_code, 403)

    def test_executor_restarts_all_with_shared_secret(self):
        from fastapi.testclient import TestClient
        from app.main import app

        restarts = [{"service": "homeops-executor", "status": "running", "container": {}}]
        with patch.dict("os.environ", {"HOMEOPS_EXECUTOR_SHARED_SECRET": "shared"}, clear=False):
            with patch("app.main.docker_ops.restart_all_services", return_value=restarts) as restart_all_services:
                response = TestClient(app).post(
                    "/v1/restarts/all",
                    headers={"X-HomeOps-Executor-Secret": "shared"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "accepted"})
        restart_all_services.assert_called_once_with()

    def test_executor_rejects_action_other_than_restart(self):
        from fastapi.testclient import TestClient
        from app.main import app

        payload = {
            "incident_id": "incident-1",
            "approval_token": "approved-token",
            "action": "shell_command",
            "service": "crawler-worker",
        }
        with patch.dict("os.environ", {"HOMEOPS_EXECUTOR_SHARED_SECRET": "shared"}, clear=False):
            response = TestClient(app).post(
                "/v1/restarts",
                json=payload,
                headers={"X-HomeOps-Executor-Secret": "shared"},
            )

        self.assertEqual(response.status_code, 403)


class RuntimeMarkerDockerOpsTests(unittest.TestCase):
    def setUp(self):
        prepare_service_import("homeops-executor")
        from app.services import docker_ops
        self.ops = docker_ops
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.root.chmod(0o755)
        self.marker = self.root / 'k3s-runtime-services.state'
        self.write_marker('compose')
        self.env = patch.dict(os.environ, {
            'HOMEOPS_RUNTIME_STATE_PATH': str(self.marker),
            'HOMEOPS_DOCKER_MANAGED_SERVICES': 'system-agent,crawler-worker,youtube-memo,book-memo,caddy,homeops-executor',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        real_fstat = os.fstat
        def root_owned_fstat(fd):
            fields = list(real_fstat(fd))
            fields[4] = 0
            return os.stat_result(fields)
        self.ownership = patch('os.fstat', side_effect=root_owned_fstat)
        self.ownership.start()
        self.addCleanup(self.ownership.stop)

    def write_marker(self, crawler):
        replacement = self.root / 'replacement'
        replacement.write_text(f'crawler-worker={crawler}\nyoutube-memo=k3s\nbook-memo=k3s\n')
        replacement.chmod(0o644)
        replacement.replace(self.marker)

    def test_marker_replacement_blocks_stale_environment_allowlist_without_restart(self):
        client = FakeDockerClient()
        self.ops.restart_service('crawler-worker', client=client)
        self.write_marker('k3s')
        with self.assertRaisesRegex(ValueError, 'service_not_allowed'):
            self.ops.restart_service('crawler-worker', client=client)
        self.assertEqual(client.containers.container.restart_calls, [10])
        self.assertNotIn('youtube-memo', self.ops.allowed_services())
        self.assertNotIn('book-memo', self.ops.allowed_services())
        self.assertIn('caddy', self.ops.allowed_services())

    def test_restart_all_excludes_k3s_writers_but_keeps_caddy_and_executor(self):
        self.write_marker('k3s')
        result = self.ops.restart_all_services(client=FakeDockerClient())
        self.assertEqual([item['service'] for item in result], ['system-agent', 'caddy', 'homeops-executor'])

    def test_marker_change_during_container_lookup_blocks_restart(self):
        client = FakeDockerClient()
        original_list = client.containers.list
        def changed_list(**kwargs):
            result = original_list(**kwargs)
            self.write_marker('k3s')
            return result
        client.containers.list = changed_list
        with self.assertRaisesRegex(ValueError, 'service_not_allowed'):
            self.ops.restart_service('crawler-worker', client=client)
        self.assertEqual(client.containers.container.restart_calls, [])

    def test_missing_malformed_or_writable_marker_fails_closed_for_writers(self):
        for bad in ('', 'crawler-worker=compose\ncrawler-worker=k3s\n',
                    'crawler-worker=unexpected\n', 'unrecognized=compose\n', '\x00', 'x' * 5000):
            with self.subTest(bad=bad[:40]):
                self.marker.write_text(bad)
                with self.assertRaisesRegex(ValueError, 'service_not_allowed'):
                    self.ops.restart_service('crawler-worker', client=FakeDockerClient())
        self.write_marker('compose')
        self.marker.chmod(0o666)
        self.assertNotIn('crawler-worker', self.ops.allowed_services())
        self.marker.unlink()
        self.assertNotIn('crawler-worker', self.ops.allowed_services())

    def test_symlink_file_or_parent_is_rejected(self):
        target = self.root / 'real-state'
        self.marker.rename(target)
        self.marker.symlink_to(target)
        self.assertNotIn('crawler-worker', self.ops.allowed_services())
        alias = self.root / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with patch.dict(os.environ, {'HOMEOPS_RUNTIME_STATE_PATH': str(alias / 'real-state')}):
            self.assertNotIn('crawler-worker', self.ops.allowed_services())

    def test_untrusted_file_owner_parent_permissions_and_hardlinks_are_rejected(self):
        self.ownership.stop()
        real_fstat = os.fstat
        def untrusted_fstat(fd):
            fields = list(real_fstat(fd))
            fields[4] = 10001
            return os.stat_result(fields)
        with patch('os.fstat', side_effect=untrusted_fstat):
            self.assertNotIn('crawler-worker', self.ops.allowed_services())
        self.ownership.start()
        self.root.chmod(0o777)
        self.assertNotIn('crawler-worker', self.ops.allowed_services())
        self.root.chmod(0o755)
        os.link(self.marker, self.root / 'alias-state')
        self.assertNotIn('crawler-worker', self.ops.allowed_services())

    def test_environment_allowlist_still_blocks_a_compose_owned_service(self):
        with patch.dict(os.environ, {'HOMEOPS_DOCKER_MANAGED_SERVICES': 'system-agent,caddy,homeops-executor'}):
            with self.assertRaisesRegex(ValueError, 'service_not_allowed'):
                self.ops.restart_service('crawler-worker', client=FakeDockerClient())


if __name__ == "__main__":
    unittest.main()
