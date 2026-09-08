import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "classify-n100-safe-deployment.py"
ROOT = Path(__file__).parents[1]
SAFE_DEPLOY_SCRIPT = ROOT / "scripts" / "deploy-n100-safe.sh"
SAFE_HEALTH_SCRIPT = ROOT / "scripts" / "verify-n100-safe-deployment-health.sh"
SPEC = importlib.util.spec_from_file_location("classify_n100_safe_deployment", MODULE_PATH)
classifier = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = classifier
SPEC.loader.exec_module(classifier)


class N100SafeDeploymentClassifierTests(unittest.TestCase):
    def test_allowlisted_service_change_deploys_only_that_service(self):
        decision = classifier.classify_changed_paths(["crawler-worker/app/main.py"])
        self.assertEqual(decision.action, "deploy")
        self.assertEqual(decision.services, ("crawler-worker",))

    def test_known_service_code_and_build_paths_deploy(self):
        decision = classifier.classify_changed_paths(
            ["crawler-worker/app/templates/index.html", "crawler-worker/app/static/site.css", "crawler-worker/Dockerfile"]
        )
        self.assertEqual(decision.action, "deploy")

    def test_unknown_service_local_operational_paths_are_blocked(self):
        for path in (
            "crawler-worker/state/checkpoint.json",
            "crawler-worker/.state/checkpoint.json",
            "crawler-worker/storage/items.json",
            "crawler-worker/cache/index.json",
            "crawler-worker/var/run.pid",
            "crawler-worker/config/auth.yaml",
        ):
            with self.subTest(path=path):
                self.assertEqual(classifier.classify_changed_paths([path]).action, "blocked")

    def test_app_operational_and_database_sidecar_paths_are_blocked(self):
        for path in (
            "crawler-worker/app/state/checkpoint.py",
            "crawler-worker/app/.state/checkpoint.py",
            "crawler-worker/app/storage/items.py",
            "crawler-worker/app/cache/index.py",
            "crawler-worker/app/var/runtime.py",
            "crawler-worker/app/config/settings.py",
            "crawler-worker/app/state/checkpoint.json",
            "crawler-worker/app/.state/checkpoint.json",
            "crawler-worker/app/storage/items.json",
            "crawler-worker/app/cache/index.json",
            "crawler-worker/app/var/run.pid",
            "crawler-worker/app/data/runtime.sqlite",
            "crawler-worker/app/data/runtime.sqlite-wal",
            "crawler-worker/app/data/runtime.sqlite-shm",
            "crawler-worker/app/data/runtime.db-wal",
            "crawler-worker/app/data/runtime.db-shm",
            "crawler-worker/app/config/auth.yaml",
        ):
            with self.subTest(path=path):
                self.assertEqual(classifier.classify_changed_paths([path]).action, "blocked")

    def test_empty_log_mountpoint_markers_are_the_only_allowed_app_data_paths(self):
        allowed = [
            "crawler-worker/app/data/logs/.gitkeep",
            "youtube-memo/app/data/logs/.gitkeep",
            "book-memo/app/data/logs/.gitkeep",
        ]
        decision = classifier.classify_changed_paths(allowed)
        self.assertEqual(decision.action, "deploy")
        self.assertEqual(
            decision.services,
            ("book-memo", "crawler-worker", "youtube-memo"),
        )

        for path in (
            "crawler-worker/app/data/logs/config.json",
            "crawler-worker/app/data/logs/.keep",
            "crawler-worker/app/data/runtime.sqlite3",
            "car-care-worker/app/data/logs/.gitkeep",
        ):
            with self.subTest(path=path):
                self.assertEqual(classifier.classify_changed_paths([path]).action, "blocked")

    def test_explicit_source_template_and_static_paths_deploy(self):
        decision = classifier.classify_changed_paths(
            [
                "crawler-worker/app/main.py",
                "crawler-worker/app/templates/index.html",
                "crawler-worker/app/static/site.css",
            ]
        )
        self.assertEqual(decision.action, "deploy")

    def test_multiple_allowlisted_services_are_sorted_and_deployed(self):
        decision = classifier.classify_changed_paths(
            ["youtube-memo/app/main.py", "book-memo/app/main.py"]
        )
        self.assertEqual(decision.action, "deploy")
        self.assertEqual(decision.services, ("book-memo", "youtube-memo"))

    def test_shared_compose_changes_are_blocked(self):
        for path in ("docker-compose.yml", "docker-compose.n100.yml"):
            with self.subTest(path=path):
                self.assertEqual(classifier.classify_changed_paths([path]).action, "blocked")

    def test_allowlisted_and_unknown_runtime_change_is_blocked(self):
        decision = classifier.classify_changed_paths(
            ["crawler-worker/app/main.py", "scripts/deploy-n100.sh"]
        )
        self.assertEqual(decision.action, "blocked")

    def test_portal_or_k3s_change_is_blocked_before_deployment(self):
        for path in ("portal-web/app/main.py", "infra/k8s/deployment.yaml"):
            with self.subTest(path=path):
                decision = classifier.classify_changed_paths([path])
                self.assertEqual(decision.action, "blocked")
                self.assertEqual(decision.services, ())

    def test_documentation_and_tests_only_changes_are_skipped(self):
        decision = classifier.classify_changed_paths(["README.md", "tests/test_example.py"])
        self.assertEqual(decision.action, "skip")
        self.assertEqual(decision.services, ())

    def test_cli_writes_github_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "github-output"
            result = classifier.main(
                [
                    "--base",
                    "base",
                    "--head",
                    "head",
                    "--github-output",
                    str(output),
                ],
                changed_paths=["crawler-worker/app/main.py"],
            )
            self.assertEqual(result, 0)
            values = dict(
                line.split("=", 1) for line in output.read_text().splitlines() if "=" in line
            )
            self.assertEqual(values["deploy_action"], "deploy")
            self.assertEqual(values["deploy_services"], "crawler-worker")

    def test_sensitive_service_local_runtime_paths_are_blocked(self):
        for path in ("crawler-worker/.env", "crawler-worker/data/runtime.sqlite"):
            with self.subTest(path=path):
                self.assertEqual(classifier.classify_changed_paths([path]).action, "blocked")

    def test_traversal_segments_are_blocked(self):
        for path in ("crawler-worker/../portal-web/app.py", "crawler-worker/./app.py"):
            with self.subTest(path=path):
                self.assertEqual(classifier.classify_changed_paths([path]).action, "blocked")

    def test_rename_input_includes_old_and_new_paths(self):
        decision = classifier.classify_changed_paths(
            ["crawler-worker/app/old.py", "portal-web/app/new.py"]
        )
        self.assertEqual(decision.action, "blocked")

    def test_cli_git_discovery_is_nul_safe_and_expands_rename_paths(self):
        completed = mock.Mock(stdout=b"R100\0crawler-worker/.env\0crawler-worker/app/new.py\0")
        with mock.patch.object(classifier.subprocess, "run", return_value=completed) as run:
            paths = classifier._changed_paths("base", "head")
        self.assertEqual(paths, ["crawler-worker/.env", "crawler-worker/app/new.py"])
        self.assertEqual(run.call_args.args[0][1:5], ["diff", "--name-status", "-z", "--find-renames"])

    def test_sensitive_components_inside_allowlisted_service_are_blocked(self):
        paths = (
            "crawler-worker/credentials/config.yaml",
            "crawler-worker/.secrets/config.yaml",
            "crawler-worker/.env.d/prod",
            "crawler-worker/config/credential.yaml",
            "crawler-worker/config/keys/service.yaml",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(classifier.classify_changed_paths([path]).action, "blocked")

    def test_control_characters_are_rejected_without_output_injection(self):
        path = "crawler-worker/app.py\n" + "deploy_action=deploy_services=portal-web"
        decision = classifier.classify_changed_paths([path])
        self.assertEqual(decision.action, "blocked")
        self.assertNotIn("crawler-worker", decision.reason)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "github-output"
            classifier.main(
                ["--base", "base", "--head", "head", "--github-output", str(output)],
                changed_paths=[path],
            )
            lines = output.read_text().splitlines()
            self.assertEqual(lines, ["deploy_action=blocked", "deploy_services=", "deploy_reason=blocked_control_character"])


class N100SafeDeploymentScriptTests(unittest.TestCase):
    NEW_SHA = "a" * 40
    OLD_SHA = "b" * 40

    def _write_executable(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def run_safe_deploy(
        self,
        *,
        expected_sha: str = NEW_SHA,
        services: tuple[str, ...] = ("crawler-worker",),
        services_csv: str | None = None,
        previous_sha: str | None = None,
        health_results: tuple[int, ...] = (0,),
        compose_results: tuple[int, ...] = (0,),
        rejected_sha: str | None = None,
        origin_main_sha: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], str, str | None]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            (root / ".git").mkdir()
            for compose_file in ("docker-compose.yml", "docker-compose.n100.yml", ".env"):
                (root / compose_file).write_text("services: {}\n", encoding="utf-8")
            (root / "data").mkdir()
            source = Path(directory) / "source"
            for service in ("crawler-worker", "youtube-memo", "book-memo", "car-care-worker"):
                service_root = source / service
                (service_root / "app").mkdir(parents=True)
                (service_root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
                (service_root / "requirements.txt").write_text("\n", encoding="utf-8")
                (service_root / "app" / "main.py").write_text("\n", encoding="utf-8")
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            if previous_sha is not None:
                (state_dir / "last-healthy-revision").write_text(
                    f"{previous_sha}\n", encoding="ascii"
                )

            fake_bin = Path(directory) / "bin"
            fake_bin.mkdir()
            calls = Path(directory) / "calls"
            health_counter = Path(directory) / "health-counter"
            compose_counter = Path(directory) / "compose-counter"
            self._write_executable(
                fake_bin / "git",
                "#!/bin/sh\n"
                f"printf 'git %s\\n' \"$*\" >> '{calls}'\n"
                "if [ \"$1\" = merge-base ]; then\n"
                "  if [ -n \"${FAKE_REJECT_SHA:-}\" ] && [ \"$3\" = \"$FAKE_REJECT_SHA\" ]; then exit 1; fi\n"
                "fi\n"
                "if [ \"$1\" = rev-parse ] && [ \"$2\" = origin/main ]; then\n"
                "  printf '%s\\n' \"${FAKE_ORIGIN_MAIN_SHA}\"\n"
                "fi\n"
                "if [ \"$1\" = show ]; then\n"
                "  cat \"${FAKE_HEALTH_SCRIPT}\"\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"$1\" = archive ]; then\n"
                "  exec /usr/bin/tar -C \"${FAKE_RELEASE_SOURCE}\" -cf - crawler-worker youtube-memo book-memo car-care-worker\n"
                "fi\n"
                "exit 0\n",
            )
            self._write_executable(
                fake_bin / "docker",
                "#!/bin/sh\n"
                f"printf 'docker %s\\n' \"$*\" >> '{calls}'\n"
                "if [ \"$1\" = compose ]; then\n"
                f"  count=0; [ -f '{compose_counter}' ] && count=$(cat '{compose_counter}')\n"
                "  count=$((count + 1)); printf '%s' \"$count\" > '"
                f"{compose_counter}'\n"
                "  result=$(printf '%s' \"${FAKE_COMPOSE_RESULTS:-0}\" | cut -d, -f \"$count\")\n"
                "  [ -n \"$result\" ] || result=0\n"
                "  [ \"$result\" = 0 ] || exit \"$result\"\n"
                "fi\n"
                "if [ \"$1\" = inspect ]; then printf '%s\\n' \"${FAKE_INSPECT_STATUS:-healthy}\"; fi\n"
                "exit 0\n",
            )
            self._write_executable(
                fake_bin / "curl",
                "#!/bin/sh\n"
                f"printf 'curl %s\\n' \"$*\" >> '{calls}'\n"
                f"count=0; [ -f '{health_counter}' ] && count=$(cat '{health_counter}')\n"
                "count=$((count + 1)); printf '%s' \"$count\" > '"
                f"{health_counter}'\n"
                "result=$(printf '%s' \"${FAKE_HEALTH_RESULTS:-0}\" | cut -d, -f \"$count\")\n"
                "[ -n \"$result\" ] || result=0\n"
                "exit \"$result\"\n",
            )
            environment = {
                **os.environ,
                "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
                "N100_SAFE_DEPLOY_PROJECT_ROOT": str(root),
                "N100_SAFE_DEPLOY_STATE_DIR": str(state_dir),
                "FAKE_HEALTH_RESULTS": ",".join(map(str, health_results)),
                "FAKE_COMPOSE_RESULTS": ",".join(map(str, compose_results)),
                "FAKE_ORIGIN_MAIN_SHA": origin_main_sha or expected_sha,
                "FAKE_RELEASE_SOURCE": str(source),
                "FAKE_HEALTH_SCRIPT": str(SAFE_HEALTH_SCRIPT),
                "N100_SAFE_DEPLOY_HEALTH_MAX_ATTEMPTS": "1",
                "N100_SAFE_DEPLOY_HEALTH_INTERVAL_SECONDS": "0",
            }
            if rejected_sha is not None:
                environment["FAKE_REJECT_SHA"] = rejected_sha
            result = subprocess.run(
                [
                    "bash",
                    str(SAFE_DEPLOY_SCRIPT),
                    expected_sha,
                    services_csv if services_csv is not None else ",".join(services),
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            recorded_calls = calls.read_text(encoding="utf-8") if calls.exists() else ""
            saved_state = state_dir / "last-healthy-revision"
            saved_content = (
                saved_state.read_text(encoding="ascii") if saved_state.exists() else None
            )
            return result, recorded_calls, saved_content

    def test_deploy_refuses_revision_that_is_not_an_origin_main_ancestor(self):
        result, calls, _ = self.run_safe_deploy(expected_sha="f" * 40, rejected_sha="f" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("docker compose", calls)
        self.assertIn("safe_cd_stage=preflight", result.stderr)

    def test_successful_deployment_records_exact_revision_only_after_health(self):
        result, calls, saved_state = self.run_safe_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(saved_state, f"{self.NEW_SHA}\n")
        self.assertNotIn("git checkout", calls)
        self.assertIn(f"git archive --format=tar {self.NEW_SHA} -- crawler-worker/Dockerfile crawler-worker/requirements.txt crawler-worker/app", calls)
        self.assertIn("/project/docker-compose.yml -f", calls)
        self.assertIn("/project/docker-compose.n100.yml -f", calls)
        self.assertIn("up -d --build --no-deps crawler-worker", calls)
        self.assertIn("safe_cd_stage=deploy", result.stderr)
        self.assertIn("safe_cd_stage=health", result.stderr)

    def test_csv_service_argument_deploys_each_validated_service(self):
        result, calls, _ = self.run_safe_deploy(
            services_csv="crawler-worker,book-memo"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("up -d --build --no-deps crawler-worker book-memo", calls)

    def test_malformed_or_repeated_csv_service_arguments_fail_before_compose(self):
        malformed_values = (
            "",
            "crawler-worker,",
            ",crawler-worker",
            "crawler-worker,crawler-worker",
            "crawler-worker;touch should-not-run",
            "crawler-worker book-memo",
            "crawler-worker\nbook-memo",
            'crawler-worker,"book-memo"',
        )
        for services_csv in malformed_values:
            with self.subTest(services_csv=services_csv):
                result, calls, _ = self.run_safe_deploy(services_csv=services_csv)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("docker compose", calls)

    def test_malformed_csv_cannot_execute_an_extra_shell_command(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "unexpected-command"
            payload = f"crawler-worker;touch {marker}"
            result, calls, _ = self.run_safe_deploy(services_csv=payload)
            marker_created = marker.exists()

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("docker compose", calls)
        self.assertFalse(marker_created)

    def test_first_deploy_health_failure_does_not_create_healthy_state(self):
        result, calls, saved_state = self.run_safe_deploy(health_results=(1,))
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(saved_state)
        self.assertEqual(calls.count("git archive"), 1)
        self.assertNotIn("safe_cd_stage=rollback", result.stderr)

    def test_health_failure_rolls_back_once_to_saved_healthy_revision(self):
        result, calls, saved_state = self.run_safe_deploy(
            previous_sha=self.OLD_SHA, health_results=(1, 0)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.count("git archive"), 2)
        self.assertIn(f"git archive --format=tar {self.OLD_SHA}", calls)
        self.assertEqual(saved_state, f"{self.OLD_SHA}\n")
        self.assertEqual(result.stderr.count("safe_cd_stage=rollback"), 1)

    def test_compose_deploy_failure_rolls_back_once_to_saved_healthy_revision(self):
        result, calls, saved_state = self.run_safe_deploy(
            previous_sha=self.OLD_SHA, compose_results=(0, 1, 0, 0), health_results=(0,)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.count("git archive"), 2)
        self.assertIn(f"git archive --format=tar {self.OLD_SHA}", calls)
        self.assertEqual(result.stderr.count("safe_cd_stage=rollback"), 1)
        self.assertEqual(saved_state, f"{self.OLD_SHA}\n")

    def test_rollback_health_failure_returns_failure_without_repeating_rollback(self):
        result, calls, saved_state = self.run_safe_deploy(
            previous_sha=self.OLD_SHA, health_results=(1, 1)
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls.count("git archive"), 2)
        self.assertEqual(result.stderr.count("safe_cd_stage=rollback"), 1)
        self.assertEqual(saved_state, f"{self.OLD_SHA}\n")

    def test_invalid_saved_revision_never_attempts_rollback(self):
        result, calls, _ = self.run_safe_deploy(
            previous_sha="not-a-revision", health_results=(1,)
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls.count("git archive"), 1)
        self.assertNotIn("safe_cd_stage=rollback", result.stderr)

    def test_saved_revision_that_is_not_an_origin_main_ancestor_never_rolls_back(self):
        result, calls, saved_state = self.run_safe_deploy(
            previous_sha=self.OLD_SHA, health_results=(1,), rejected_sha=self.OLD_SHA
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls.count("git archive"), 1)
        self.assertNotIn("safe_cd_stage=rollback", result.stderr)
        self.assertEqual(saved_state, f"{self.OLD_SHA}\n")

    def test_rejects_invalid_sha_and_non_allowlisted_service_before_compose(self):
        invalid_sha, invalid_sha_calls, _ = self.run_safe_deploy(expected_sha="too-short")
        invalid_service, invalid_service_calls, _ = self.run_safe_deploy(
            services=("portal-web",)
        )
        self.assertNotEqual(invalid_sha.returncode, 0)
        self.assertNotEqual(invalid_service.returncode, 0)
        self.assertNotIn("docker compose", invalid_sha_calls)
        self.assertNotIn("docker compose", invalid_service_calls)

    def test_stale_expected_sha_refuses_before_archive_or_docker(self):
        result, calls, _ = self.run_safe_deploy(origin_main_sha=self.OLD_SHA)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("git rev-parse origin/main", calls)
        self.assertNotIn("git archive", calls)
        self.assertNotIn("docker compose", calls)

    def test_health_checks_requested_allowlisted_containers_at_loopback_only(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_bin = Path(directory) / "bin"
            fake_bin.mkdir()
            calls = Path(directory) / "calls"
            self._write_executable(
                fake_bin / "docker",
                "#!/bin/sh\n"
                f"printf 'docker %s\\n' \"$*\" >> '{calls}'\n"
                "printf 'healthy\\n'\n",
            )
            self._write_executable(
                fake_bin / "curl",
                "#!/bin/sh\n"
                f"printf 'curl %s\\n' \"$*\" >> '{calls}'\n",
            )
            result = subprocess.run(
                ["bash", str(SAFE_HEALTH_SCRIPT), "crawler-worker", "book-memo"],
                env={**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]},
                capture_output=True,
                text=True,
                check=False,
            )
            recorded_calls = calls.read_text(encoding="utf-8") if calls.exists() else ""
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("inspect --format {{.State.Health.Status}} crawler-worker", recorded_calls)
        self.assertIn("inspect --format {{.State.Health.Status}} book-memo", recorded_calls)
        self.assertNotIn("portal-web", recorded_calls)
        self.assertIn("http://127.0.0.1:8001/health", recorded_calls)
        self.assertIn("http://127.0.0.1:8003/health", recorded_calls)
        self.assertNotIn("host.docker.internal", recorded_calls)

    def test_health_waits_for_starting_container_to_become_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_bin = Path(directory) / "bin"
            fake_bin.mkdir()
            calls = Path(directory) / "calls"
            counter = Path(directory) / "inspect-counter"
            self._write_executable(
                fake_bin / "docker",
                "#!/bin/sh\n"
                f"printf 'docker %s\\n' \"$*\" >> '{calls}'\n"
                f"count=0; [ -f '{counter}' ] && count=$(cat '{counter}')\n"
                "count=$((count + 1)); printf '%s' \"$count\" > '"
                f"{counter}'\n"
                "[ \"$count\" -eq 1 ] && printf 'starting\\n' || printf 'healthy\\n'\n",
            )
            self._write_executable(
                fake_bin / "curl",
                "#!/bin/sh\n"
                f"printf 'curl %s\\n' \"$*\" >> '{calls}'\n",
            )
            result = subprocess.run(
                ["bash", str(SAFE_HEALTH_SCRIPT), "crawler-worker"],
                env={
                    **os.environ,
                    "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
                    "N100_SAFE_DEPLOY_HEALTH_MAX_ATTEMPTS": "2",
                    "N100_SAFE_DEPLOY_HEALTH_INTERVAL_SECONDS": "0",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            recorded_calls = calls.read_text(encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(recorded_calls.count("docker inspect"), 2)
        self.assertEqual(recorded_calls.count("curl "), 1)

    def test_health_times_out_when_container_never_becomes_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_bin = Path(directory) / "bin"
            fake_bin.mkdir()
            calls = Path(directory) / "calls"
            self._write_executable(
                fake_bin / "docker",
                "#!/bin/sh\n"
                f"printf 'docker %s\\n' \"$*\" >> '{calls}'\n"
                "printf 'starting\\n'\n",
            )
            self._write_executable(
                fake_bin / "curl",
                "#!/bin/sh\n"
                f"printf 'curl %s\\n' \"$*\" >> '{calls}'\n",
            )
            result = subprocess.run(
                ["bash", str(SAFE_HEALTH_SCRIPT), "crawler-worker"],
                env={
                    **os.environ,
                    "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
                    "N100_SAFE_DEPLOY_HEALTH_MAX_ATTEMPTS": "2",
                    "N100_SAFE_DEPLOY_HEALTH_INTERVAL_SECONDS": "0",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            recorded_calls = calls.read_text(encoding="utf-8")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(recorded_calls.count("docker inspect"), 3)
        self.assertNotIn("docker logs", recorded_calls)
        self.assertNotIn("curl ", recorded_calls)

    def test_health_timeout_reports_fixed_container_diagnostics_without_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_bin = Path(directory) / "bin"
            fake_bin.mkdir()
            self._write_executable(
                fake_bin / "docker",
                "#!/bin/sh\n"
                "if [ \"$1\" = inspect ]; then printf 'unhealthy\\n'; exit 0; fi\n"
                "if [ \"$1\" = logs ]; then\n"
                "  printf 'startup failed token=123456789:abcdefghijklmnopqrstuvwxyz\\n'\n"
                "fi\n",
            )
            self._write_executable(fake_bin / "curl", "#!/bin/sh\nexit 1\n")
            result = subprocess.run(
                ["bash", str(SAFE_HEALTH_SCRIPT), "crawler-worker"],
                env={
                    **os.environ,
                    "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
                    "N100_SAFE_DEPLOY_HEALTH_MAX_ATTEMPTS": "1",
                    "N100_SAFE_DEPLOY_HEALTH_INTERVAL_SECONDS": "0",
                },
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("safe_cd_health_diagnostic service=crawler-worker", result.stderr)
        self.assertIn("unhealthy", result.stderr)
        self.assertNotIn("123456789:abcdefghijklmnopqrstuvwxyz", result.stderr)
        self.assertNotIn("startup failed", result.stderr)

    def test_deploy_script_uses_release_archive_and_never_checks_out_shared_project(self):
        script = SAFE_DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("git checkout", script)
        self.assertIn("git archive --format=tar", script)
        self.assertIn('git show "$revision:scripts/verify-n100-safe-deployment-health.sh"', script)
        self.assertIn('mktemp "$STATE_DIR/verify-health.XXXXXX"', script)
        self.assertIn('chmod 700 "$HEALTH_SCRIPT"', script)
        self.assertNotIn('readonly HEALTH_SCRIPT=', script)
        self.assertIn("mktemp -d", script)
        self.assertIn("/app:ro", script)
        self.assertIn("docker-compose.yml", script)
        self.assertIn("docker-compose.n100.yml", script)

    def test_deploy_script_trap_tracks_temporary_resources_for_rollback_cleanup(self):
        script = SAFE_DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("COMPOSE_OVERRIDES=()", script)
        self.assertIn("HEALTH_SCRIPTS=()", script)
        self.assertIn('COMPOSE_OVERRIDES+=("$COMPOSE_OVERRIDE")', script)
        self.assertIn('HEALTH_SCRIPTS+=("$HEALTH_SCRIPT")', script)
        self.assertIn("trap cleanup_generated_resources EXIT INT TERM HUP", script)

    def test_workflow_uses_full_ci_run_range_not_bare_head_parent(self):
        workflow = (ROOT / ".github/workflows/deploy-n100.yml").read_text(encoding="utf-8")
        self.assertIn("actions/github-script@v7", workflow)
        self.assertIn("listWorkflowRuns", workflow)
        self.assertIn("context.payload.before", workflow)
        self.assertIn("workflow_id: 'ci.yml'", workflow)
        self.assertNotIn("--base HEAD^", workflow)
        self.assertIn("github.sha", workflow)



if __name__ == "__main__":
    unittest.main()
