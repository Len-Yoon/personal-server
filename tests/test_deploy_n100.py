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
    def test_deploy_workflow_classifies_the_exact_push_range_before_runner_allocation(self):
        changes_job = WORKFLOW.split("\n  deploy:", maxsplit=1)[0]

        self.assertIn("name: Classify safe N100 deployment", WORKFLOW)
        self.assertIn("runs-on: ubuntu-latest", changes_job)
        self.assertIn("context.payload.before", changes_job)
        self.assertIn("github.sha", changes_job)
        self.assertIn("classify-n100-safe-deployment.py", changes_job)
        self.assertIn("actions/github-script@v7", changes_job)
        self.assertIn("listWorkflowRuns", changes_job)
        self.assertIn("workflow_id: 'ci.yml'", changes_job)
        self.assertIn("core.setOutput('ci_sha'", changes_job)
        self.assertIn("--base \"${{ steps.wait-ci.outputs.base_sha }}\"", changes_job)
        self.assertIn("--head \"${{ github.sha }}\"", changes_job)
        self.assertNotIn("--base HEAD^", changes_job)
        self.assertIn("deploy_action", changes_job)
        self.assertIn("deploy_services", changes_job)
        self.assertIn("deploy_reason", changes_job)
        self.assertIn("action=blocked", changes_job)
        self.assertIn("exit 1", changes_job)
        self.assertNotIn("git diff --quiet HEAD^ HEAD --", changes_job)

    def test_workflow_waits_for_successful_main_ci_for_the_exact_push_before_using_n100_runner(self):
        self.assertIn("push:", WORKFLOW)
        self.assertIn("branches: [main]", WORKFLOW)
        self.assertIn("candidate.head_sha === context.sha", WORKFLOW)
        self.assertIn("run.conclusion !== 'success'", WORKFLOW)
        self.assertIn("await new Promise", WORKFLOW)
        self.assertIn("actions: read", WORKFLOW)
        self.assertIn("runs-on: [self-hosted, Windows, X64]", WORKFLOW)
        self.assertIn("C:\\personal-server", WORKFLOW)
        self.assertIn("wsl.exe -d Ubuntu-24.04 -- bash -lc", WORKFLOW)
        self.assertIn("shell: cmd", WORKFLOW)
        self.assertNotIn("shell: powershell", WORKFLOW)
        self.assertNotIn("shell: pwsh", WORKFLOW)
        self.assertIn("bash ./scripts/deploy-n100-safe.sh", WORKFLOW)
        self.assertNotIn("bash ./scripts/deploy-n100.sh", WORKFLOW)
        self.assertNotIn("N100_SSH_KEY", WORKFLOW)

    def test_workflow_passes_verified_push_sha_and_selected_services_to_safe_script(self):
        deploy_job = WORKFLOW.split("\n  deploy:", maxsplit=1)[1]

        self.assertIn("needs.changes.outputs.action == 'deploy'", deploy_job)
        self.assertIn("github.sha", deploy_job)
        self.assertIn("N100_SAFE_DEPLOY_SERVICES", deploy_job)
        self.assertIn("N100_SAFE_DEPLOY_SHA", deploy_job)
        self.assertIn('set "WSLENV=N100_SAFE_DEPLOY_SHA:N100_SAFE_DEPLOY_SERVICES"', deploy_job)
        self.assertIn('\\"$N100_SAFE_DEPLOY_SERVICES\\"', deploy_job)
        self.assertIn('\\"$N100_SAFE_DEPLOY_SHA\\"', deploy_job)
        self.assertIn("bash ./scripts/deploy-n100-safe.sh", deploy_job)
        self.assertNotIn("services=${services//,/ }", deploy_job)
        self.assertNotIn("services='${{ needs.changes.outputs.services }}'", deploy_job)

    def test_workflow_limits_both_jobs_to_the_main_push_event(self):
        changes_job, deploy_job = WORKFLOW.split("\n  deploy:", maxsplit=1)
        for job in (changes_job, deploy_job):
            with self.subTest(job=job):
                self.assertNotIn("github.event.workflow_run", job)
        self.assertIn("branches: [main]", WORKFLOW)

    def test_workflow_never_allocates_n100_runner_for_blocked_paths(self):
        changes_job, deploy_job = WORKFLOW.split("\n  deploy:", maxsplit=1)

        self.assertIn("action=blocked", changes_job)
        self.assertIn("exit 1", changes_job)
        self.assertIn("runs-on: ubuntu-latest", changes_job)
        self.assertIn("needs.changes.outputs.action == 'deploy'", deploy_job)
        self.assertNotIn("workflow_dispatch", WORKFLOW)

    def test_ci_covers_homeops_news_routes_and_deploy_script(self):
        self.assertIn("tests.test_homeops tests.test_homeops_notifier", CI_WORKFLOW)
        self.assertIn("tests.homeops_executor.test_docker_ops", CI_WORKFLOW)
        self.assertIn("tests.crawler_worker.test_news_routes", CI_WORKFLOW)
        self.assertIn("tests.test_deploy_n100", CI_WORKFLOW)

    def test_homeops_executor_test_client_dependency_is_pinned(self):
        self.assertIn("httpx==0.28.1", HOMEOPS_REQUIREMENTS)

    def test_crawler_worker_test_client_dependency_is_pinned(self):
        self.assertIn("httpx==0.28.1", CRAWLER_REQUIREMENTS)

    def test_deploy_workflow_delegates_health_and_rollback_to_safe_entrypoint(self):
        deploy_job = WORKFLOW.split("\n  deploy:", maxsplit=1)[1]
        self.assertIn("Run safe deployment", deploy_job)
        self.assertIn("bash ./scripts/deploy-n100-safe.sh", deploy_job)
        self.assertNotIn("verify-n100-deployment-health.sh", deploy_job)
        self.assertNotIn("deploy-n100.sh", deploy_job)

    def test_deploy_health_check_is_portal_runtime_marker_aware(self):
        """K3s/cutover must not be judged as a missing Compose Portal writer."""
        health_check = (ROOT / "scripts" / "verify-n100-deployment-health.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("portal_runtime_marker=data/portal-runtime.mode", health_check)
        self.assertIn('case "$portal_runtime_mode" in', health_check)
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
        self.assertIn("for service in", health_check)
        self.assertIn("for url in", health_check)
        self.assertIn("seq 1 18", health_check)
        self.assertIn("sleep 5", health_check)
        self.assertNotIn(".env", health_check)

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
        self.assertIn("github.sha", GUIDE)
        self.assertIn("revision 고정", GUIDE)
        self.assertIn("직전 정상 revision", GUIDE)
        self.assertIn("자동 배포 제외", GUIDE)
        self.assertIn("crawler-worker", GUIDE)
        self.assertIn("youtube-memo", GUIDE)
        self.assertIn("book-memo", GUIDE)
        self.assertIn("car-care-worker", GUIDE)
        self.assertIn("Portal", GUIDE)
        self.assertIn("Caddy", GUIDE)
        self.assertIn("K3s", GUIDE)
        self.assertIn("Telegram 알림도 1차 CD 범위에서 제외", GUIDE)
        self.assertIn("GitHub Actions 단계와 로그가 운영 신호", GUIDE)
        self.assertIn("마스킹", GUIDE)
        self.assertNotIn("N100_SSH_HOST", GUIDE)
        self.assertIn("CI가 성공", HANDOFF)
        self.assertIn("CI가 성공", GUIDE)
        self.assertIn("main", GUIDE)
        self.assertIn("health", GUIDE)
        self.assertNotIn("main push에만 반응", GUIDE)
        self.assertIn("직접 push", GUIDE)
        self.assertIn("기능 브랜치 PR", GUIDE)
        self.assertNotIn("PR은 선택", GUIDE)


if __name__ == "__main__":
    unittest.main()
