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

    def restart(self, timeout: int):
        self.restart_calls.append(timeout)

    def reload(self):
        return None

    def logs(self, **kwargs):
        return b"worker ready\nAuthorization: Bearer should-not-be-masked-here\n"

    def stats(self, stream=False):
        return {
            "memory_stats": {"usage": 45, "limit": 100},
            "cpu_stats": {"cpu_usage": {"total_usage": 200, "percpu_usage": [50, 50]}, "system_cpu_usage": 2_000},
            "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1_000},
        }


class FakeContainers:
    def __init__(self):
        self.filters: list[dict[str, str]] = []
        self.container = FakeContainer()

    def list(self, filters: dict[str, str]):
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
        self.assertEqual(
            client.containers.filters,
            [
                {"label": f"com.docker.compose.service={service}"}
                for service in sorted(docker_ops.ALLOWED_SERVICES)
            ],
        )

    def test_restart_all_places_executor_last(self):
        from app.services import docker_ops

        client = FakeDockerClient()

        result = docker_ops.restart_all_services(client=client)

        expected_services = sorted(docker_ops.ALLOWED_SERVICES - {"homeops-executor"}) + ["homeops-executor"]
        self.assertEqual([item["service"] for item in result], expected_services)
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

            def list(self, filters):
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
            with patch("app.main.docker_ops.restart_all_services", return_value=restarts):
                response = TestClient(app).post(
                    "/v1/restarts/all",
                    headers={"X-HomeOps-Executor-Secret": "shared"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), restarts)

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
