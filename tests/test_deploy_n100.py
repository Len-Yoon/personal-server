import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "deploy-n100.yml").read_text(encoding="utf-8")
CI_WORKFLOW = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
SCRIPT = (ROOT / "scripts" / "deploy-n100.sh").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
HANDOFF = (ROOT / "docs" / "agent-handoff.md").read_text(encoding="utf-8")
GUIDE = (ROOT / "docs" / "n100-github-auto-deploy.md").read_text(encoding="utf-8") if (ROOT / "docs" / "n100-github-auto-deploy.md").exists() else ""
HOMEOPS_REQUIREMENTS = (ROOT / "homeops-executor" / "requirements.txt").read_text(encoding="utf-8")
CRAWLER_REQUIREMENTS = (ROOT / "crawler-worker" / "requirements.txt").read_text(encoding="utf-8")


class DeployN100Tests(unittest.TestCase):
    def test_deploy_workflow_skips_compose_redeploy_for_docs_and_gitops_drafts(self):
        runtime_detector = WORKFLOW.split("- id: runtime", maxsplit=1)[1].split(
            "\n\n  deploy:", maxsplit=1
        )[0]

        self.assertIn("name: Detect deployable runtime changes", WORKFLOW)
        self.assertIn("needs: changes", WORKFLOW)
        self.assertIn("needs.changes.outputs.runtime == 'true'", WORKFLOW)
        self.assertIn("git diff --quiet HEAD^ HEAD --", runtime_detector)
        for runtime_path in (
            "docker-compose.yml",
            "docker-compose.n100.yml",
            "scripts/deploy-n100.sh",
            "caddy/",
            "portal-web/",
            "system-agent/",
            "crawler-worker/",
            "youtube-memo/",
            "book-memo/",
            "car-care-worker/",
            "homeops-executor/",
        ):
            self.assertIn(runtime_path, runtime_detector)

    def test_workflow_waits_for_successful_main_ci_and_uses_n100_runner(self):
        self.assertIn("workflow_run:", WORKFLOW)
        self.assertIn("workflows: [CI]", WORKFLOW)
        self.assertIn("types: [completed]", WORKFLOW)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", WORKFLOW)
        self.assertIn("github.event.workflow_run.head_branch == 'main'", WORKFLOW)
        self.assertIn("runs-on: [self-hosted, Windows, X64]", WORKFLOW)
        self.assertIn("C:\\personal-server", WORKFLOW)
        self.assertIn("wsl.exe -d Ubuntu-24.04 -- bash -lc", WORKFLOW)
        self.assertIn("shell: cmd", WORKFLOW)
        self.assertNotIn("shell: powershell", WORKFLOW)
        self.assertNotIn("shell: pwsh", WORKFLOW)
        self.assertIn("bash ./scripts/deploy-n100.sh", WORKFLOW)
        self.assertNotIn("N100_SSH_KEY", WORKFLOW)

    def test_ci_covers_homeops_news_routes_and_deploy_script(self):
        self.assertIn("tests.test_homeops tests.test_homeops_notifier", CI_WORKFLOW)
        self.assertIn("tests.homeops_executor.test_docker_ops", CI_WORKFLOW)
        self.assertIn("tests.crawler_worker.test_news_routes", CI_WORKFLOW)
        self.assertIn("tests.test_deploy_n100", CI_WORKFLOW)

    def test_homeops_executor_test_client_dependency_is_pinned(self):
        self.assertIn("httpx==0.28.1", HOMEOPS_REQUIREMENTS)

    def test_crawler_worker_test_client_dependency_is_pinned(self):
        self.assertIn("httpx==0.28.1", CRAWLER_REQUIREMENTS)

    def test_deploy_workflow_checks_services_after_deployment(self):
        health_check = WORKFLOW.split("- name: Verify deployed service health", maxsplit=1)[1]
        self.assertIn("Verify deployed service health", WORKFLOW)
        self.assertIn("compose() { docker compose -f docker-compose.yml -f docker-compose.n100.yml $@; }", WORKFLOW)
        self.assertIn("http://127.0.0.1:8000/health", WORKFLOW)
        self.assertIn("http://127.0.0.1:18010/health", WORKFLOW)
        self.assertIn("http://127.0.0.1:8001/health", WORKFLOW)
        self.assertIn("http://127.0.0.1:8002/health", WORKFLOW)
        self.assertIn("http://127.0.0.1:8003/health", WORKFLOW)
        self.assertIn("http://127.0.0.1:8015/health", WORKFLOW)
        self.assertIn("homeops-executor", WORKFLOW)
        self.assertIn("--retry-all", health_check)
        self.assertIn("--retry-connrefused", health_check)
        self.assertNotIn("$service", health_check)
        self.assertNotIn("$url", health_check)
        self.assertIn("for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18", health_check)
        self.assertIn("sleep 5", health_check)
        for service in (
            "portal-web",
            "system-agent",
            "crawler-worker",
            "youtube-memo",
            "book-memo",
            "caddy",
            "homeops-executor",
            "car-care-worker",
        ):
            self.assertIn(f"grep -Fx -- {service}", health_check)

    def test_deploy_health_check_is_portal_runtime_marker_aware(self):
        """K3s/cutover must not be judged as a missing Compose Portal writer."""
        health_check = WORKFLOW.split("- name: Verify deployed service health", maxsplit=1)[1]

        self.assertIn("portal_runtime_marker=data/portal-runtime.mode", health_check)
        self.assertIn("case $portal_runtime_mode in", health_check)
        self.assertIn("compose)", health_check)
        self.assertIn("k3s)", health_check)
        self.assertIn("cutover)", health_check)
        self.assertIn("Invalid portal runtime marker", health_check)
        self.assertIn("http://127.0.0.1:8000/health", health_check)
        self.assertIn("http://127.0.0.1:30080/health", health_check)

        compose_case = health_check[health_check.index("compose)") : health_check.index("k3s)")]
        k3s_case = health_check[health_check.index("k3s)") : health_check.index("cutover)")]
        cutover_case = health_check[health_check.index("cutover)") : health_check.index("*)")]
        self.assertIn("grep -Fx -- portal-web", compose_case)
        self.assertIn("grep -Fx -- portal-web", k3s_case)
        self.assertIn("grep -Fx -- portal-web", cutover_case)
        self.assertNotIn("http://127.0.0.1:8000/health", k3s_case)
        self.assertNotIn("http://127.0.0.1:8000/health", cutover_case)

    def test_deploy_refuses_to_mount_blank_portal_state(self):
        """Deploy must stop before Compose can create a blank Portal state mount."""
        self.assertIn("require_portal_state_ready", SCRIPT)
        self.assertIn("Portal state migration is required before Compose Portal can start", SCRIPT)
        self.assertIn("PRAGMA quick_check", SCRIPT)
        self.assertLess(
            SCRIPT.index("require_portal_state_ready"),
            SCRIPT.index("up -d --build portal-web"),
        )

    def test_deploy_parses_the_runtime_marker_before_selecting_compose_portal(self):
        """An invalid mode must not silently restart the former Compose writer."""
        self.assertIn("PORTAL_RUNTIME_MARKER", SCRIPT)
        self.assertIn("load_portal_runtime_mode", SCRIPT)
        self.assertIn("Invalid portal runtime marker", SCRIPT)
        self.assertIn("case \"$PORTAL_RUNTIME_MODE\" in", SCRIPT)
        self.assertLess(
            SCRIPT.index("load_portal_runtime_mode"),
            SCRIPT.index("up -d --build portal-web"),
        )

    def test_deploy_keeps_compose_portal_stopped_for_cutover_and_k3s(self):
        """Only explicit compose mode may include portal-web in the deploy command."""
        self.assertIn("cutover|k3s)", SCRIPT)
        self.assertIn("--no-deps caddy", SCRIPT)
        self.assertIn("HOMEOPS_DOCKER_MANAGED_SERVICES=\"system-agent,crawler-worker,youtube-memo,book-memo,caddy,homeops-executor\"", SCRIPT)
        deploy_runtime = SCRIPT[SCRIPT.index("deploy_runtime_services() {") :]
        mode_body = deploy_runtime[deploy_runtime.index("cutover|k3s)") :]
        mode_body = mode_body[: mode_body.index(";;")]
        self.assertNotIn("portal-web", mode_body)

    def test_k3s_marker_restarts_only_bridge_dependencies_and_caddy(self):
        """The deploy entrypoint must honor K3s marker state at runtime, not just in text."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            (root / ".env").write_text("unused=true\n", encoding="utf-8")
            (root / "data").mkdir()
            (root / "data" / "portal-runtime.mode").write_text("k3s\n", encoding="utf-8")
            bridge = root / "docker-compose.portal-bridge.yml"
            bridge.write_text("services: {}\n", encoding="utf-8")
            calls = root / "calls"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            (fake_bin / "git").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (fake_bin / "docker").write_text(
                "#!/bin/sh\n"
                f"printf '%s|%s\\n' \"${{DOCKER_BRIDGE_GATEWAY:-unset}}\" \"$*\" >> '{calls}'\n"
                "if [ \"$1\" = network ] && [ \"$2\" = inspect ]; then echo 172.17.0.1; fi\n",
                encoding="utf-8",
            )
            for tool in fake_bin.iterdir():
                tool.chmod(0o755)

            result = subprocess.run(
                ["bash", str(ROOT / "scripts" / "deploy-n100.sh"), str(root)],
                env={**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            recorded = calls.read_text(encoding="utf-8")
            self.assertIn(f"-f {bridge}", recorded)
            self.assertIn("172.17.0.1|compose", recorded)
            self.assertIn("up -d --no-deps caddy", recorded)
            self.assertNotIn("up -d --build portal-web", recorded)

    def test_invalid_runtime_marker_refuses_before_any_compose_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            (root / ".env").write_text("unused=true\n", encoding="utf-8")
            (root / "data").mkdir()
            (root / "data" / "portal-runtime.mode").write_text("unknown\n", encoding="utf-8")
            calls = root / "calls"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            (fake_bin / "git").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (fake_bin / "docker").write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> '{calls}'\n",
                encoding="utf-8",
            )
            for tool in fake_bin.iterdir():
                tool.chmod(0o755)

            result = subprocess.run(
                ["bash", str(ROOT / "scripts" / "deploy-n100.sh"), str(root)],
                env={**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Invalid portal runtime marker", result.stderr)
            self.assertNotIn(" up ", calls.read_text(encoding="utf-8"))

    def test_workflows_limit_token_permissions_and_serialize_deployments(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

        self.assertIn("permissions:\n  contents: read", ci)
        self.assertIn("concurrency:", WORKFLOW)
        self.assertIn("group: deploy-n100-${{ github.ref }}", WORKFLOW)
        self.assertIn("cancel-in-progress: false", WORKFLOW)

    def test_deploy_workflow_validates_local_n100_directory(self):
        self.assertIn("Verify N100 deployment directory", WORKFLOW)
        self.assertIn("if not exist", WORKFLOW)

    def test_deploy_script_resets_and_restarts_compose_stack(self):
        self.assertNotIn(b"\r\n", (ROOT / "scripts" / "deploy-n100.sh").read_bytes())
        self.assertIn('test -d "$PROJECT_ROOT/.git"', SCRIPT)
        self.assertIn('test -f "$PROJECT_ROOT/.env"', SCRIPT)
        self.assertIn('test -d "$PROJECT_ROOT/data"', SCRIPT)
        self.assertIn("docker compose -f docker-compose.yml -f docker-compose.n100.yml config", SCRIPT)
        self.assertIn("git fetch --prune origin", SCRIPT)
        self.assertIn("git reset --hard origin/main", SCRIPT)
        self.assertIn("wait_for_docker", SCRIPT)
        self.assertIn("docker info", SCRIPT)
        self.assertIn("DOCKER_WAIT_ATTEMPTS", SCRIPT)
        self.assertIn(
            "docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build portal-web homeops-executor system-agent crawler-worker youtube-memo book-memo car-care-worker caddy",
            SCRIPT,
        )
        self.assertNotIn(
            "docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build\n",
            SCRIPT,
        )
        self.assertIn("docker compose -f docker-compose.yml -f docker-compose.n100.yml ps", SCRIPT)

    def test_documentation_mentions_auto_deploy_flow(self):
        self.assertIn("docs/n100-github-auto-deploy.md", README)
        self.assertIn("main", README)
        self.assertIn("self-hosted", GUIDE)
        self.assertIn("runs-on: [self-hosted, Windows, X64]", GUIDE)
        self.assertNotIn("N100_SSH_HOST", GUIDE)
        self.assertIn("git fetch --prune origin", GUIDE)
        self.assertIn(
            "docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build portal-web homeops-executor system-agent crawler-worker youtube-memo book-memo caddy",
            GUIDE,
        )
        self.assertIn("CI가 성공", HANDOFF)
        self.assertIn("CI가 성공", GUIDE)
        self.assertIn("main", GUIDE)
        self.assertIn("health", GUIDE)
        self.assertNotIn("main push에만 반응", GUIDE)
        self.assertIn("직접 push", GUIDE)
        self.assertIn("기능 브랜치 PR", GUIDE)
        self.assertIn("런타임 배포 경로", GUIDE)
        self.assertIn("문서와 비활성 GitOps 초안", GUIDE)
        self.assertNotIn("PR은 선택", GUIDE)


if __name__ == "__main__":
    unittest.main()
