import unittest
from unittest.mock import patch


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

    def test_executor_accepts_admin_password_as_internal_secret_fallback(self):
        from fastapi.testclient import TestClient
        from app.main import app

        diagnostics = [{"service": "caddy", "container": {}, "logs": []}]
        with patch.dict(
            "os.environ",
            {"HOMEOPS_EXECUTOR_SHARED_SECRET": "", "ADMIN_STATUS_PASSWORD": "admin-secret"},
            clear=False,
        ):
            with patch("app.main.docker_ops.collect_all_diagnostics", return_value=diagnostics):
                response = TestClient(app).get(
                    "/v1/diagnostics",
                    headers={"X-HomeOps-Executor-Secret": "admin-secret"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), diagnostics)

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


if __name__ == "__main__":
    unittest.main()
