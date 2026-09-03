import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/k8s/tools/portal-cutover.sh"
DOC = ROOT / "docs/k3s-flux-transition-draft.md"
BOOTSTRAP = ROOT / "scripts/windows-bootstrap.sh"


class PortalCutoverContractTest(unittest.TestCase):
    def test_cutover_rejects_k3s_pvc_backup_evidence_for_compose_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "backup.ok"
            evidence.write_text(
                "\n".join(
                    (
                        "schema_version=1",
                        "scope=portal",
                        "backup_status=success",
                        "encrypted=true",
                        "backup_completed_at=2026-09-03T00:00:00Z",
                        "restore_status=success",
                        "restore_verified_at=2026-09-03T00:00:00Z",
                        "evidence_expires_at=2026-09-04T00:00:00Z",
                        "backup_id=portal-test",
                        "source_runtime=k3s-pvc",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            source_dir = root / "files"
            source_dir.mkdir()
            state_dir = root / "state"
            state_dir.mkdir()
            (state_dir / "homeops.sqlite3").write_bytes(b"sqlite-placeholder")
            env_file = root / ".env"
            env_file.write_text(
                "\n".join(
                    f"{key}=test-value"
                    for key in (
                        "DELETE_PASSWORD",
                        "FILE_MANAGER_PASSWORD",
                        "ADMIN_STATUS_PASSWORD",
                        "FILE_MANAGER_ACCESS_PASSWORD",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            fake_sudo = root / "sudo"
            fake_sudo.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = k3s ] && [ \"$2\" = secrets-encrypt ] && [ \"$3\" = status ]; then\n"
                "  printf '%s\\n' 'Encryption Status: Enabled'\n"
                "  exit 0\n"
                "fi\n"
                "exit 99\n",
                encoding="utf-8",
            )
            fake_sudo.chmod(0o755)
            result = subprocess.run(
                ["/bin/bash", str(SCRIPT), "--go"],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "PORTAL_BACKUP_EVIDENCE": str(evidence),
                    "PORTAL_SOURCE_DIR": str(source_dir),
                    "PORTAL_STATE_SOURCE_DIR": str(state_dir),
                    "PORTAL_ENV_FILE": str(env_file),
                    "RUN_ID": "reject-k3s-evidence",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("encrypted backup evidence is missing or invalid", result.stderr)

    def test_script_exists_and_is_bash(self):
        self.assertTrue(SCRIPT.is_file())
        self.assertTrue(SCRIPT.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash"))

    def test_default_timeout_allows_compose_bridge_services_to_start(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-60}"', text)

    def test_cutover_is_fail_closed_and_operator_gated(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for required in (
            "--go",
            "--switch-caddy",
            "--rollback-caddy",
            "k3s secrets-encrypt status",
            "Encryption Status: Enabled",
            "BACKUP_EVIDENCE",
            "PORTAL_BACKUP_MAX_AGE_SECONDS",
            "validate-backup-evidence.py",
            "docker compose -f",
            "stop portal-web",
            "portal-web-files",
            "storageClassName: local-path",
            "sha256sum",
            "chmod 600",
            "DELETE_PASSWORD",
            "FILE_MANAGER_PASSWORD",
            "ADMIN_STATUS_PASSWORD",
            "FILE_MANAGER_ACCESS_PASSWORD",
            "immutable: true",
            "kind: Deployment",
            "kind: Service",
            "type: NodePort",
            "nodePort: 30080",
            "rollout status deployment/portal-web",
            "automountServiceAccountToken: false",
            "imagePullPolicy: Never",
            "portal_cutover=PASS",
            "portal_cutover=FAIL",
        ):
            self.assertIn(required, text)
        self.assertIn("PORTAL_UPSTREAM", text)
        self.assertIn("host.docker.internal:30080", text)
        self.assertNotRegex(text, r"(^|\n)\s*(source|\.)\s+[^\n]*\.env")
        self.assertNotIn("kubectl create secret generic portal-web-runtime --from-literal", text)
        self.assertNotRegex(text, r"trap[^\n]+EXIT")

    def test_secret_allowlist_requires_only_portal_core_keys(self):
        text = SCRIPT.read_text(encoding="utf-8")
        allowlist = text[text.index("secret_allowlist() {") : text.index("tree_digest() {")]
        self.assertIn('required_keys="DELETE_PASSWORD FILE_MANAGER_PASSWORD ADMIN_STATUS_PASSWORD FILE_MANAGER_ACCESS_PASSWORD"', allowlist)
        self.assertIn('optional_keys="PORTFOLIO_ADMIN_PASSWORD HOMEOPS_EXECUTOR_SHARED_SECRET HOMEOPS_SCHEDULER_SECRET HOMEOPS_TELEGRAM_BOT_TOKEN HOMEOPS_TELEGRAM_CHAT_ID"', allowlist)
        self.assertLess(allowlist.index('if (key in found)'), allowlist.index('if (value == "")'))

    def test_default_mode_requires_go_without_invoking_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = Path(directory) / "calls"
            fake_bin = Path(directory) / "bin"
            fake_bin.mkdir()
            for name in ("sudo", "docker", "sha256sum", "kubectl", "k3s"):
                tool = fake_bin / name
                tool.write_text(f"#!/bin/sh\nprintf '%s\\n' '{name} $*' >> '{calls}'\nexit 99\n")
                tool.chmod(0o755)
            result = subprocess.run(
                ["/bin/bash", str(SCRIPT)],
                env={**os.environ, "PATH": str(fake_bin), "RUN_ID": "no-go"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--go", result.stderr)
            self.assertFalse(calls.exists())

    def test_combined_prepare_and_public_switch_is_rejected_without_invoking_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = Path(directory) / "calls"
            fake_bin = Path(directory) / "bin"
            fake_bin.mkdir()
            for name in ("sudo", "docker", "sha256sum", "kubectl", "k3s"):
                tool = fake_bin / name
                tool.write_text(f"#!/bin/sh\nprintf '%s\\n' '{name} $*' >> '{calls}'\nexit 99\n")
                tool.chmod(0o755)
            result = subprocess.run(
                ["/bin/bash", str(SCRIPT), "--go", "--switch-caddy"],
                env={
                    **os.environ,
                    "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
                    "RUN_ID": "combined-modes",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--go", result.stderr)
            self.assertIn("--switch-caddy", result.stderr)
            self.assertIn("portal_cutover=FAIL", result.stderr)
            self.assertFalse(calls.exists())

    def test_document_requires_separate_prepare_and_public_switch_commands(self):
        text = DOC.read_text(encoding="utf-8")
        self.assertIn("portal-cutover.sh --go", text)
        self.assertIn("portal-cutover.sh --switch-caddy", text)
        self.assertNotIn("portal-cutover.sh --go --switch-caddy", text)

    def test_encryption_gate_happens_before_resource_or_secret_access(self):
        secret_value = "must-not-appear"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = root / "calls"
            fake_sudo = root / "sudo"
            fake_sudo.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> '{calls}'\n"
                "if [ \"$1\" = k3s ] && [ \"$2\" = secrets-encrypt ] && [ \"$3\" = status ]; then\n"
                "  printf '%s\\n' 'Encryption Status: Disabled'\n"
                "  exit 0\n"
                "fi\n"
                "exit 99\n"
            )
            fake_sudo.chmod(0o755)
            env_file = root / ".env"
            env_file.write_text("\n".join(f"{key}={secret_value}" for key in (
                "DELETE_PASSWORD",
                "FILE_MANAGER_PASSWORD",
                "ADMIN_STATUS_PASSWORD",
                "FILE_MANAGER_ACCESS_PASSWORD",
            )))
            result = subprocess.run(
                ["bash", str(SCRIPT), "--go"],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "PORTAL_ENV_FILE": str(env_file),
                    "PORTAL_BACKUP_EVIDENCE": str(root / "backup.ok"),
                    "RUN_ID": "disabled-encryption",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(secret_value, result.stdout + result.stderr)
            self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), ["k3s secrets-encrypt status"])

    def test_rollback_requires_explicit_mode_and_does_not_start_k3s(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertRegex(text, r"rollback.*PORTAL_UPSTREAM|PORTAL_UPSTREAM.*rollback")
        self.assertRegex(text, r"rollback.*docker compose|docker compose.*rollback")
        self.assertRegex(text, r"rollback.*kubectl.*scale|kubectl.*scale.*rollback")

    def test_compose_start_is_gated_on_absent_k3s_selector_pods(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("wait_for_k3s_writer_absent", text)
        self.assertIn("--for=delete", text)
        self.assertRegex(text, r"wait_for_k3s_writer_absent[\s\S]+get pod -l app\.kubernetes\.io/name=portal-web")
        self.assertRegex(text, r"start_compose_writer\(\) \{\s+assert_no_k3s_writer")

    def test_stopping_an_absent_k3s_writer_does_not_fail_rollback(self):
        """A pre-Deployment failure has no K3s writer to scale down."""
        text = SCRIPT.read_text(encoding="utf-8")
        stop = text[text.index("stop_k3s_writer() {") : text.index("wait_for_k3s_writer_absent() {")]

        self.assertIn('get deployment portal-web', stop)
        self.assertIn('scale deployment/portal-web --replicas=0', stop)
        self.assertLess(stop.index('get deployment portal-web'), stop.index('scale deployment/portal-web --replicas=0'))

    def test_stop_k3s_writer_accepts_an_absent_deployment_without_scaling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = root / "calls"
            fake_sudo = root / "sudo"
            fake_sudo.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> '{calls}'\n"
                "case \"$*\" in\n"
                "  *'get deployment portal-web'*) exit 0 ;;\n"
                "  *'get pod -l app.kubernetes.io/name=portal-web'*) exit 0 ;;\n"
                "  *'kubectl wait '*) exit 0 ;;\n"
                "esac\n"
                "exit 99\n"
            )
            fake_sudo.chmod(0o755)
            library = root / "portal-cutover-lib.sh"
            library.write_text(
                SCRIPT.read_text(encoding="utf-8").rsplit('\nmain "$@"', 1)[0],
                encoding="utf-8",
            )

            result = subprocess.run(
                ["/bin/bash", "-c", f'. "{library}"; stop_k3s_writer'],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "RUN_ID": "absent-deployment",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            recorded = calls.read_text(encoding="utf-8")
            self.assertIn("get deployment portal-web", recorded)
            self.assertIn("get pod -l app.kubernetes.io/name=portal-web", recorded)
            self.assertNotIn("scale deployment/portal-web", recorded)

    def test_stop_k3s_writer_rejects_a_deployment_lookup_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_sudo = root / "sudo"
            fake_sudo.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'get deployment portal-web'*) exit 1 ;;\n"
                "esac\n"
                "exit 99\n"
            )
            fake_sudo.chmod(0o755)
            library = root / "portal-cutover-lib.sh"
            library.write_text(
                SCRIPT.read_text(encoding="utf-8").rsplit('\nmain "$@"', 1)[0],
                encoding="utf-8",
            )

            result = subprocess.run(
                ["/bin/bash", "-c", f'. "{library}"; stop_k3s_writer'],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "RUN_ID": "deployment-lookup-error",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)

    def test_absent_k3s_writer_check_rejects_a_deployment_lookup_error(self):
        """A K3s lookup error cannot be treated as evidence that no writer exists."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_sudo = root / "sudo"
            fake_sudo.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'get deployment portal-web'*) exit 1 ;;\n"
                "esac\n"
                "exit 99\n"
            )
            fake_sudo.chmod(0o755)
            library = root / "portal-cutover-lib.sh"
            library.write_text(
                SCRIPT.read_text(encoding="utf-8").rsplit('\nmain \"$@\"', 1)[0],
                encoding="utf-8",
            )

            result = subprocess.run(
                ["/bin/bash", "-c", f'. "{library}"; assert_no_k3s_writer'],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "RUN_ID": "no-writer-lookup-error",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)

    def test_absent_k3s_writer_check_rejects_an_orphan_running_portal_pod(self):
        """A deleted Deployment does not prove that its Portal writer is gone."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_sudo = root / "sudo"
            fake_sudo.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'get deployment portal-web --ignore-not-found -o name'*) exit 0 ;;\n"
                "  *'get pod -l app.kubernetes.io/name=portal-web --field-selector=status.phase=Running -o name'*) printf 'pod/portal-web-orphan\\n'; exit 0 ;;\n"
                "esac\n"
                "exit 99\n"
            )
            fake_sudo.chmod(0o755)
            library = root / "portal-cutover-lib.sh"
            library.write_text(
                SCRIPT.read_text(encoding="utf-8").rsplit('\nmain \"$@\"', 1)[0],
                encoding="utf-8",
            )

            result = subprocess.run(
                ["/bin/bash", "-c", f'. "{library}"; assert_no_k3s_writer'],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "RUN_ID": "no-writer-orphan-pod",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)

    def test_rollback_does_not_start_compose_while_k3s_pod_remains(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = root / "calls"
            fake_sudo = root / "sudo"
            fake_sudo.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"sudo $*\" >> '{calls}'\n"
                "if [ \"$1\" = k3s ] && [ \"$2\" = kubectl ] && [ \"$3\" = wait ]; then exit 0; fi\n"
                "if [ \"$1\" = k3s ] && [ \"$2\" = kubectl ] && [ \"$5\" = get ] && [ \"$6\" = deployment ]; then printf '%s\\n' deployment/portal-web; exit 0; fi\n"
                "if [ \"$1\" = k3s ] && [ \"$2\" = kubectl ] && [ \"$5\" = get ] && [ \"$6\" = pod ]; then printf '%s\\n' pod/portal-web-1; exit 0; fi\n"
                "if [ \"$1\" = k3s ] && [ \"$2\" = kubectl ] && [ \"$5\" = scale ]; then exit 0; fi\n"
                "exit 0\n"
            )
            fake_sudo.chmod(0o755)
            fake_docker = root / "docker"
            fake_docker.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \\\"docker $*\\\" >> '{calls}'\n"
                "case \"$*\" in\n"
                "  *'inspect portal-web --format'*) printf 'false\\n'; exit 0 ;;\n"
                "esac\n"
                "exit 0\n"
            )
            fake_docker.chmod(0o755)
            env_file = root / ".env"
            env_file.write_text("PORTAL_UPSTREAM=host.docker.internal:30080\n")
            result = subprocess.run(
                ["/bin/bash", str(SCRIPT), "--rollback-caddy"],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "PORTAL_ENV_FILE": str(env_file),
                    "RUN_ID": "pod-still-present",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            recorded_calls = calls.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any("scale deployment/portal-web" in call for call in recorded_calls))
            self.assertTrue(any("wait --for=delete pod -l app.kubernetes.io/name=portal-web" in call for call in recorded_calls))
            self.assertFalse(any("docker compose" in call and "start portal-web" in call for call in recorded_calls))

    def test_cutover_excludes_executor_and_preflights_bridge_before_stopping_compose(self):
        """A running executor must lose Portal control before the writer handoff starts."""
        text = SCRIPT.read_text(encoding="utf-8")
        prepare = text[text.index("prepare_cutover() {") : text.index("usage() {")]

        self.assertIn("exclude_portal_from_executor", text)
        self.assertIn("preflight_bridge", text)
        self.assertLess(
            prepare.index("exclude_portal_from_executor"),
            prepare.index("stop portal-web"),
        )
        self.assertLess(
            prepare.index("preflight_bridge"),
            prepare.index("stop portal-web"),
        )

    def test_bridge_preflight_retries_health_checks_until_services_are_ready(self):
        text = SCRIPT.read_text(encoding="utf-8")
        preflight = text[text.index("preflight_bridge() {") : text.index("secret_allowlist() {")]
        self.assertIn('deadline=$((SECONDS + TIMEOUT_SECONDS))', preflight)
        self.assertIn('while [ "$SECONDS" -lt "$deadline" ]', preflight)
        self.assertIn('remaining=$((deadline - SECONDS))', preflight)
        self.assertIn('run_timeout "$request_timeout" curl', preflight)
        self.assertIn("sleep 1", preflight)
        self.assertIn("bridge endpoint did not become ready", preflight)

    def test_public_validation_uses_portfolio_root_not_blocked_health_route(self):
        """The portfolio host intentionally blocks /health but serves its public root."""
        text = SCRIPT.read_text(encoding="utf-8")
        public_validation = text[text.index("validate_public_hosts() {") : text.index("validate_caddy_config() {")]

        self.assertIn("len.pe.kr|/health", public_validation)
        self.assertIn("portfolio.len.pe.kr|/", public_validation)
        self.assertIn("file.len.pe.kr|/health", public_validation)
        self.assertIn("admin.len.pe.kr|/health", public_validation)
        self.assertIn('--write-out "%{http_code}"', public_validation)
        self.assertIn('[ "$status_code" != "200" ]', public_validation)

    def test_public_validation_rejects_a_portfolio_redirect(self):
        """Portfolio root must be served directly, not redirected during the handoff."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_curl = root / "curl"
            fake_curl.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *portfolio.len.pe.kr*) printf '%s' '302' ;;\n"
                "  *) printf '%s' '{\"status\":\"ok\"}' ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)
            fake_sleep = root / "sleep"
            fake_sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_sleep.chmod(0o755)
            library = root / "portal-cutover-lib.sh"
            library.write_text(
                SCRIPT.read_text(encoding="utf-8").rsplit('\nmain \"$@\"', 1)[0],
                encoding="utf-8",
            )

            result = subprocess.run(
                ["/bin/bash", "-c", f'. "{library}"; CADDY_HEALTH_TIMEOUT_SECONDS=1; validate_public_hosts'],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "CADDY_HEALTH_TIMEOUT_SECONDS": "1",
                    "TIMEOUT_SECONDS": "1",
                    "RUN_ID": "portfolio-redirect",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)

    def test_compose_state_migration_requires_an_explicit_command(self):
        """A deployment must not silently switch Portal to an empty state mount."""
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("--migrate-compose-state", text)
        self.assertIn("migrate_compose_state", text)
        bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("Portal state migration is required before Compose Portal can start", bootstrap)
        self.assertLess(bootstrap.index("require_portal_state_ready"), bootstrap.index("portal-web homeops-executor"))

    def test_state_migration_is_atomic_and_verified_before_new_compose_state_can_start(self):
        """Legacy state cannot be replaced by an empty new mount during a cutover retry."""
        text = SCRIPT.read_text(encoding="utf-8")
        prepare = text[text.index("prepare_cutover() {") : text.index("usage() {")]

        self.assertIn("PORTAL_LEGACY_STATE_SOURCE_DIR", text)
        self.assertIn("migrate_legacy_state_atomically", text)
        self.assertIn("Portal state migration digest or HomeOps SQLite quick_check failed", text)
        self.assertLess(
            prepare.index("migrate_legacy_state_atomically"),
            prepare.index("tar -C \"$STATE_SOURCE_DIR\""),
        )

    def test_state_migration_replaces_only_an_empty_target_directory(self):
        """An empty old mount must not block the explicit legacy-state migration."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "logs"
            target = root / "portal-web-state"
            legacy.mkdir()
            target.mkdir()
            database = legacy / "homeops.sqlite3"
            subprocess.run(
                [
                    "python3",
                    "-c",
                    "import sqlite3,sys; connection=sqlite3.connect(sys.argv[1]); "
                    "connection.execute('CREATE TABLE state (id INTEGER)'); connection.commit(); connection.close()",
                    str(database),
                ],
                check=True,
            )
            library = root / "portal-cutover-lib.sh"
            library.write_text(
                SCRIPT.read_text(encoding="utf-8").rsplit('\nmain "$@"', 1)[0],
                encoding="utf-8",
            )

            result = subprocess.run(
                ["/bin/bash", "-c", f'. "{library}"; migrate_legacy_state_atomically'],
                env={
                    **os.environ,
                    "PORTAL_LEGACY_STATE_SOURCE_DIR": str(legacy),
                    "PORTAL_STATE_SOURCE_DIR": str(target),
                    "RUN_ID": "empty-target",
                    "TIMEOUT_SECONDS": "10",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((legacy / "homeops.sqlite3").is_file())
            self.assertTrue((target / "homeops.sqlite3").is_file())
            self.assertEqual(database.read_bytes(), (target / "homeops.sqlite3").read_bytes())

    def test_state_migration_refuses_a_nonempty_target_without_touching_legacy_state(self):
        """Unexpected target entries must block migration rather than be overwritten."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "logs"
            target = root / "portal-web-state"
            legacy.mkdir()
            target.mkdir()
            database = legacy / "homeops.sqlite3"
            subprocess.run(
                [
                    "python3",
                    "-c",
                    "import sqlite3,sys; connection=sqlite3.connect(sys.argv[1]); "
                    "connection.execute('CREATE TABLE state (id INTEGER)'); connection.commit(); connection.close()",
                    str(database),
                ],
                check=True,
            )
            before = database.read_bytes()
            unexpected = target / "unexpected.txt"
            unexpected.write_text("preserve", encoding="utf-8")
            library = root / "portal-cutover-lib.sh"
            library.write_text(
                SCRIPT.read_text(encoding="utf-8").rsplit('\nmain "$@"', 1)[0],
                encoding="utf-8",
            )

            result = subprocess.run(
                ["/bin/bash", "-c", f'. "{library}"; migrate_legacy_state_atomically'],
                env={
                    **os.environ,
                    "PORTAL_LEGACY_STATE_SOURCE_DIR": str(legacy),
                    "PORTAL_STATE_SOURCE_DIR": str(target),
                    "RUN_ID": "nonempty-target",
                    "TIMEOUT_SECONDS": "10",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(database.read_bytes(), before)
            self.assertEqual(unexpected.read_text(encoding="utf-8"), "preserve")

    def test_state_migration_keeps_empty_target_when_legacy_database_is_missing(self):
        """A failed migration must not change even an empty existing target mount."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "logs"
            target = root / "portal-web-state"
            legacy.mkdir()
            target.mkdir()
            library = root / "portal-cutover-lib.sh"
            library.write_text(
                SCRIPT.read_text(encoding="utf-8").rsplit('\nmain "$@"', 1)[0],
                encoding="utf-8",
            )

            result = subprocess.run(
                ["/bin/bash", "-c", f'. "{library}"; migrate_legacy_state_atomically'],
                env={
                    **os.environ,
                    "PORTAL_LEGACY_STATE_SOURCE_DIR": str(legacy),
                    "PORTAL_STATE_SOURCE_DIR": str(target),
                    "RUN_ID": "missing-legacy",
                    "TIMEOUT_SECONDS": "10",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(target.is_dir())

    def test_state_migration_uses_non_directory_replacing_publish(self):
        """A recreated target directory must not absorb the staged state tree."""
        text = SCRIPT.read_text(encoding="utf-8")
        migration = text[text.index("migrate_legacy_state_atomically() {") : text.index("migrate_compose_state() {")]
        self.assertIn('mv -T -- "$temporary" "$STATE_SOURCE_DIR"', migration)

    def test_rollback_restores_verified_state_pvc_before_compose_portal_start(self):
        """Rollback must not revive Compose against stale local Portal state."""
        text = SCRIPT.read_text(encoding="utf-8")
        rollback = text[text.index("rollback_caddy() {") : text.index("switch_caddy() {")]

        self.assertIn("restore_state_from_pvc", text)
        self.assertIn("Portal state PVC restore digest or HomeOps SQLite quick_check failed", text)
        self.assertLess(
            rollback.index("restore_state_from_pvc"),
            rollback.index("start_compose_writer"),
        )

    def test_duplicate_recovery_refuses_live_compose_when_k3s_writer_still_exists(self):
        """A live but unverified Compose writer must not hide a K3s split-brain."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "portal-cutover-lib.sh"
            restored = root / "state-was-replaced"
            library.write_text(
                SCRIPT.read_text(encoding="utf-8").rsplit('\nmain "$@"', 1)[0],
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    "/bin/bash",
                    "-c",
                    f'''. "{library}"
assert_compose_writer_running() {{ return 0; }}
assert_compose_writer_healthy() {{ return 1; }}
assert_no_k3s_writer() {{ return 1; }}
restore_files_from_pvc() {{ touch "{restored}"; return 0; }}
restore_state_from_pvc() {{ touch "{restored}"; return 0; }}
restore_writers_after_switch_failure''',
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(restored.exists())

    def test_duplicate_recovery_is_a_noop_only_after_compose_and_k3s_are_verified(self):
        """The second recovery is safe only after the first one fully completed."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "portal-cutover-lib.sh"
            restored = root / "state-was-replaced"
            library.write_text(
                SCRIPT.read_text(encoding="utf-8").rsplit('\nmain "$@"', 1)[0],
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    "/bin/bash",
                    "-c",
                    f'''. "{library}"
assert_compose_writer_running() {{ return 0; }}
assert_compose_writer_healthy() {{ return 0; }}
assert_no_k3s_writer() {{ return 0; }}
restore_files_from_pvc() {{ touch "{restored}"; return 0; }}
restore_state_from_pvc() {{ touch "{restored}"; return 0; }}
restore_writers_after_switch_failure''',
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(restored.exists())

    def test_switch_failure_rollback_requires_a_stopped_compose_writer_before_state_restore(self):
        """Rollback keeps the writer boundary closed while replacing Portal state."""
        text = SCRIPT.read_text(encoding="utf-8")
        recovery = text[
            text.index("restore_writers_after_switch_failure() {") : text.index("on_signal() {")
        ]

        self.assertIn("set_runtime_marker cutover", recovery)
        self.assertIn("assert_compose_writer_stopped", recovery)
        self.assertLess(recovery.index("set_runtime_marker cutover"), recovery.index("restore_files_from_pvc"))
        self.assertLess(recovery.index("assert_compose_writer_stopped"), recovery.index("restore_state_from_pvc"))

    def test_explicit_rollback_refuses_to_replace_state_below_a_live_compose_writer(self):
        """Manual rollback must enforce the same writer boundary as automatic rollback."""
        text = SCRIPT.read_text(encoding="utf-8")
        rollback = text[text.index("rollback_caddy() {") : text.index("switch_caddy() {")]

        self.assertIn("assert_compose_writer_stopped", rollback)
        self.assertLess(rollback.index("assert_compose_writer_stopped"), rollback.index("restore_state_from_pvc"))

    def test_rollback_rechecks_the_existing_state_pvc_in_a_fresh_process(self):
        """A later --rollback-caddy invocation has no in-memory preparation flags."""
        text = SCRIPT.read_text(encoding="utf-8")
        restore = text[text.index("restore_pvc_to_local() {") : text.index("assert_namespace() {")]

        self.assertIn('get pvc "$pvc_name"', restore)
        self.assertIn('restore_pvc_to_local "$STATE_PVC_NAME"', restore)
        self.assertNotIn('[ "$STATE_PVC_TARGET" -eq 1 ] || return 1', restore)

    def test_pvc_restore_retries_only_transient_exec_stream_failures(self):
        """A closed kubectl exec stream must not discard an otherwise intact PVC."""
        text = SCRIPT.read_text(encoding="utf-8")
        restore = text[text.index("restore_pvc_to_local() {") : text.index("assert_namespace() {")]

        self.assertIn('RESTORE_STREAM_ATTEMPTS="${PORTAL_RESTORE_STREAM_ATTEMPTS:-3}"', text)
        self.assertIn('for attempt in $(seq 1 "$RESTORE_STREAM_ATTEMPTS")', restore)
        self.assertIn('kubectl -n "$NAMESPACE" exec "$pod_name" -- tar', restore)
        self.assertIn('rm -rf -- "$temporary"', restore)
        self.assertIn('sleep 1', restore)
        self.assertLess(restore.index('for attempt in $(seq 1 "$RESTORE_STREAM_ATTEMPTS")'), restore.index('local_digest='))

    def test_rollback_retries_transient_wsl_directory_removal(self):
        """A transient DrvFs removal error must not turn a completed restore into a rollback failure."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "rollback-backup"
            target.mkdir()
            (target / "stale.txt").write_text("stale", encoding="utf-8")
            count = root / "rm-count"
            fake_rm = root / "rm"
            fake_rm.write_text(
                "#!/bin/sh\n"
                f"count=$(cat '{count}' 2>/dev/null || printf '0')\n"
                "count=$((count + 1)); printf '%s\\n' \"$count\" > '" + str(count) + "'\n"
                "if [ \"$count\" = 1 ]; then exit 1; fi\n"
                "exec \"$REAL_RM\" \"$@\"\n",
                encoding="utf-8",
            )
            fake_rm.chmod(0o755)
            fake_sleep = root / "sleep"
            fake_sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_sleep.chmod(0o755)
            library = root / "portal-cutover-lib.sh"
            library.write_text(
                SCRIPT.read_text(encoding="utf-8").rsplit('\nmain "$@"', 1)[0],
                encoding="utf-8",
            )

            result = subprocess.run(
                ["/bin/bash", "-c", f'. "{library}"; remove_tree_with_retries "{target}"'],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "REAL_RM": shutil.which("rm") or "rm",
                    "RUN_ID": "wsl-remove-retry",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(count.read_text(encoding="utf-8").strip(), "2")
            self.assertFalse(target.exists())

    def test_public_host_validation_retries_caddy_startup_before_failing(self):
        """Caddy TLS can briefly refuse connections immediately after recreate."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            count = root / "curl-count"
            fake_timeout = root / "timeout"
            fake_timeout.write_text("#!/bin/sh\nshift\nexec \"$@\"\n", encoding="utf-8")
            fake_timeout.chmod(0o755)
            fake_curl = root / "curl"
            fake_curl.write_text(
                "#!/bin/sh\n"
                f"count=$(cat '{count}' 2>/dev/null || printf '0')\n"
                "count=$((count + 1)); printf '%s\\n' \"$count\" > '" + str(count) + "'\n"
                "if [ \"$count\" = 1 ]; then exit 7; fi\n"
                "case \"$*\" in\n"
                "  *'--write-out'*) printf '%s' '200' ;;\n"
                "  *) printf '%s\\n' '{\"service\":\"portal-web\",\"status\":\"ok\"}' ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)
            fake_sleep = root / "sleep"
            fake_sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_sleep.chmod(0o755)
            library = root / "portal-cutover-lib.sh"
            library.write_text(
                SCRIPT.read_text(encoding="utf-8").rsplit('\nmain "$@"', 1)[0],
                encoding="utf-8",
            )

            result = subprocess.run(
                ["/bin/bash", "-c", f'. "{library}"; validate_public_hosts'],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "RUN_ID": "caddy-startup-retry",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertGreaterEqual(int(count.read_text(encoding="utf-8").strip()), 5)

    def test_nodeport_validation_retries_caddy_startup_before_failing(self):
        """The Caddy container can reject exec probes while it is still starting."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            count = root / "docker-count"
            fake_timeout = root / "timeout"
            fake_timeout.write_text("#!/bin/sh\nshift\nexec \"$@\"\n", encoding="utf-8")
            fake_timeout.chmod(0o755)
            fake_docker = root / "docker"
            fake_docker.write_text(
                "#!/bin/sh\n"
                f"count=$(cat '{count}' 2>/dev/null || printf '0')\n"
                "count=$((count + 1)); printf '%s\\n' \"$count\" > '" + str(count) + "'\n"
                "if [ \"$count\" = 1 ]; then exit 1; fi\n"
                "printf '%s\\n' '{\"service\":\"portal-web\",\"status\":\"ok\"}'\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)
            fake_sleep = root / "sleep"
            fake_sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_sleep.chmod(0o755)
            library = root / "portal-cutover-lib.sh"
            library.write_text(
                SCRIPT.read_text(encoding="utf-8").rsplit('\nmain \"$@\"', 1)[0],
                encoding="utf-8",
            )

            result = subprocess.run(
                ["/bin/bash", "-c", f'. "{library}"; validate_nodeport'],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "RUN_ID": "nodeport-startup-retry",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(count.read_text(encoding="utf-8").strip(), "2")

    def test_caddy_health_retries_have_a_fixed_deadline(self):
        """A stalled Caddy check must not multiply a per-request timeout by retries."""
        text = SCRIPT.read_text(encoding="utf-8")
        nodeport = text[text.index("validate_nodeport() {") : text.index("assert_nodeport_private_exposure() {")]
        public_hosts = text[text.index("validate_public_hosts() {") : text.index("validate_caddy_config() {")]

        self.assertIn("CADDY_HEALTH_TIMEOUT_SECONDS=30", text)
        self.assertNotIn("CADDY_HEALTH_ATTEMPTS", text)
        for function in (nodeport, public_hosts):
            self.assertIn('deadline=$((SECONDS + CADDY_HEALTH_TIMEOUT_SECONDS))', function)
            self.assertIn('while [ "$SECONDS" -lt "$deadline" ]', function)
            self.assertIn('request_timeout=$((deadline - SECONDS))', function)
            self.assertIn('run_timeout "$request_timeout"', function)

    def test_public_switch_reports_safe_failure_stages_in_order(self):
        """An operator must see the first failing Caddy handoff boundary without Secret data."""
        text = SCRIPT.read_text(encoding="utf-8")
        switch = text[text.index("switch_caddy() {") : text.index("switch_prepared_caddy() {")]

        expected = (
            "portal_cutover_stage=caddy_upstream",
            "portal_cutover_stage=caddy_recreate",
            "portal_cutover_stage=nodeport_health",
            "portal_cutover_stage=public_health",
            "portal_cutover_stage=runtime_marker",
        )
        for stage in expected:
            self.assertIn(stage, switch)
        self.assertLess(switch.index(expected[0]), switch.index("set_portal_upstream"))
        self.assertLess(switch.index(expected[1]), switch.index("recreate_caddy"))
        self.assertLess(switch.index(expected[2]), switch.index("validate_nodeport"))
        self.assertLess(switch.index(expected[3]), switch.index("validate_public_hosts"))
        self.assertLess(switch.index(expected[4]), switch.index("set_runtime_marker k3s"))
        self.assertNotRegex(switch, r"portal_cutover_stage=.*(PASSWORD=|TOKEN=|SECRET=|PORTAL_UPSTREAM=)")

    def test_pvc_restore_retries_a_stream_failure_without_replacing_local_data_early(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "fixture"
            fixture.mkdir()
            (fixture / "copied.txt").write_text("from-pvc", encoding="utf-8")
            destination = root / "destination"
            destination.mkdir()
            (destination / "before.txt").write_text("local-before", encoding="utf-8")
            calls = root / "calls"
            count = root / "tar-count"
            fake_timeout = root / "timeout"
            fake_timeout.write_text("#!/bin/sh\nshift\nexec \"$@\"\n", encoding="utf-8")
            fake_timeout.chmod(0o755)
            fake_sudo = root / "sudo"
            fake_sudo.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> '{calls}'\n"
                "case \"$*\" in\n"
                "  *'get pvc'*) exit 0 ;;\n"
                "  *' apply -f -'*) cat >/dev/null; exit 0 ;;\n"
                "  *'wait --for=condition=Ready'*) exit 0 ;;\n"
                "  *'exec restore-pod -- id -u'*) printf '0\\n'; exit 0 ;;\n"
                "  *'exec restore-pod -- id -g'*) printf '0\\n'; exit 0 ;;\n"
                "  *'exec restore-pod -- tar -C /data/files -cf - .'*)\n"
                f"    count=$(cat '{count}' 2>/dev/null || printf '0')\n"
                "    count=$((count + 1)); printf '%s\\n' \"$count\" > '" + str(count) + "'\n"
                "    if [ \"$count\" = 1 ]; then exit 1; fi\n"
                "    test -f \"$DESTINATION/before.txt\" || exit 97\n"
                "    exec \"$REAL_TAR\" -C \"$FIXTURE\" -cf - . ;;\n"
                "  *'exec restore-pod -- sh -c'*)\n"
                "    (cd \"$FIXTURE\" && find . -type f -print0 | sort -z | xargs -0 sha256sum); exit 0 ;;\n"
                "  *'delete pod'*) exit 0 ;;\n"
                "esac\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_sudo.chmod(0o755)
            library = root / "portal-cutover-lib.sh"
            library.write_text(
                SCRIPT.read_text(encoding="utf-8").rsplit('\nmain "$@"', 1)[0],
                encoding="utf-8",
            )
            result = subprocess.run(
                ["/bin/bash", "-c", f'. "{library}"; restore_pvc_to_local demo-pvc /data/files "{destination}" restore-pod ""'],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "DESTINATION": str(destination),
                    "FIXTURE": str(fixture),
                    "REAL_TAR": shutil.which("tar") or "tar",
                    "PORTAL_RESTORE_STREAM_ATTEMPTS": "2",
                    "RUN_ID": "restore-retry",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(count.read_text(encoding="utf-8").strip(), "2")
            self.assertEqual((destination / "copied.txt").read_text(encoding="utf-8"), "from-pvc")
            self.assertFalse((destination / "before.txt").exists())

    def test_rollback_restores_both_pvcs_before_compose_and_only_then_deletes_them(self):
        """Rollback preserves the only current copies until both local restores verify."""
        text = SCRIPT.read_text(encoding="utf-8")
        rollback = text[text.index("rollback_caddy() {") : text.index("switch_caddy() {")]
        cleanup = text[text.index("remove_partial_resources() {") : text.index("abort_cutover() {")]

        self.assertIn("restore_files_from_pvc", rollback)
        self.assertLess(rollback.index("restore_files_from_pvc"), rollback.index("start_compose_writer"))
        self.assertLess(rollback.index("restore_state_from_pvc"), rollback.index("start_compose_writer"))
        self.assertIn("files_restored", cleanup)
        self.assertLess(cleanup.index("restore_files_from_pvc"), cleanup.index('delete pvc "$PVC_NAME"'))
        self.assertLess(cleanup.index("restore_state_from_pvc"), cleanup.index('delete pvc "$STATE_PVC_NAME"'))

    def test_rollback_validates_compose_caddy_before_starting_compose_writer(self):
        """Caddy must point back to Compose and validate before its writer can start."""
        text = SCRIPT.read_text(encoding="utf-8")
        rollback = text[text.index("rollback_caddy() {") : text.index("switch_caddy() {")]

        self.assertLess(rollback.index("stop_k3s_writer"), rollback.index('set_portal_upstream "portal-web:8000"'))
        self.assertLess(rollback.index('set_portal_upstream "portal-web:8000"'), rollback.index("recreate_caddy"))
        self.assertIn("validate_caddy_config", rollback)
        self.assertLess(rollback.index("recreate_caddy"), rollback.index("validate_caddy_config"))
        self.assertLess(rollback.index("validate_caddy_config"), rollback.index("restore_compose_executor"))
        self.assertLess(rollback.index("validate_caddy_config"), rollback.index("start_compose_writer"))
        self.assertLess(rollback.index("start_compose_writer"), rollback.index("validate_public_hosts"))

    def test_pvc_restore_checks_runtime_uid_gid_and_file_permissions_in_pod(self):
        """PVC restore must fail closed when the actual Portal runtime cannot read/write its data."""
        text = SCRIPT.read_text(encoding="utf-8")
        restore = text[text.index("restore_pvc_to_local() {") : text.index("assert_namespace() {")]
        switch = text[text.index("switch_prepared_caddy() {") : text.index("prepare_cutover() {")]

        self.assertIn("assert_pvc_runtime_permissions", text)
        self.assertIn("id -u", text)
        self.assertIn("id -g", text)
        self.assertIn("test -r", text)
        self.assertIn("test -w", text)
        self.assertIn("xargs -0 -r sh -c", text)
        self.assertIn("BEGIN IMMEDIATE", text)
        self.assertIn("ROLLBACK", text)
        self.assertIn("sqlite_master", text)
        self.assertIn("remaining_table", text)
        self.assertIn("remaining_row", text)
        self.assertGreaterEqual(text.count("sqlite3.connect"), 2)
        self.assertNotIn('find "$mount_path" -type f -exec test', text)
        self.assertLess(restore.index("wait --for=condition=Ready"), restore.index("assert_pvc_runtime_permissions"))
        self.assertLess(restore.index("assert_pvc_runtime_permissions"), restore.index("tar -C"))
        self.assertIn("assert_portal_runtime_permissions", switch)
        self.assertLess(switch.index("assert_portal_runtime_permissions"), switch.index("switch_caddy"))

    def test_compose_restart_requires_verified_local_portal_state(self):
        """A rollback cannot start Compose with a missing or corrupt local state database."""
        text = SCRIPT.read_text(encoding="utf-8")
        compose_start = text[text.index("start_compose_writer() {") : text.index("remove_partial_resources() {")]

        self.assertIn("assert_local_compose_data_ready", compose_start)
        self.assertIn("assert_sqlite_quick_check", text)

    def test_failed_state_migration_keeps_the_cutover_marker_and_never_restarts_compose(self):
        """A failed migration is an operator-visible stop, not an automatic writer revival."""
        text = SCRIPT.read_text(encoding="utf-8")
        prepare = text[text.index("prepare_cutover() {") : text.index("usage() {")]
        cleanup = text[text.index("remove_partial_resources() {") : text.index("abort_cutover() {")]

        self.assertIn("MIGRATION_FAILED=1", prepare)
        self.assertIn('[ "$MIGRATION_FAILED" -eq 0 ]', cleanup)

    def test_switch_refuses_a_nodeport_reachable_on_non_bridge_host_addresses(self):
        """Caddy may use the Docker bridge, but a host-wide NodePort must block the switch."""
        text = SCRIPT.read_text(encoding="utf-8")
        switch = text[text.index("switch_prepared_caddy() {") : text.index("prepare_cutover() {")]

        self.assertIn("assert_nodeport_private_exposure", text)
        self.assertLess(switch.index("assert_nodeport_private_exposure"), switch.index("switch_caddy"))

    def test_preparation_also_refuses_a_public_nodeport_before_caddy_switch(self):
        """A hidden Caddy route does not make an already-created NodePort private."""
        text = SCRIPT.read_text(encoding="utf-8")
        prepare = text[text.index("prepare_cutover() {") : text.index("usage() {")]

        self.assertLess(
            prepare.index("nodePort: 30080"),
            prepare.index("assert_nodeport_private_exposure"),
        )
        self.assertLess(
            prepare.index("assert_nodeport_private_exposure"),
            prepare.index('printf \'%s\\n\' "portal_cutover=PASS"'),
        )
        self.assertIn("ip -4 -o addr show scope global", text)

    def test_wait_for_first_consumer_pvcs_are_not_waited_on_before_copy_pod(self):
        """local-path provisions only after the copy Pod consumes both claims."""
        text = SCRIPT.read_text(encoding="utf-8")
        prepare = text[text.index("prepare_cutover() {") : text.index("usage() {")]

        files_claim = prepare.index("name: $PVC_NAME")
        state_claim = prepare.index("name: $STATE_PVC_NAME")
        copy_pod = prepare.index("name: $COPY_POD")
        copy_ready = prepare.index('wait --for=condition=Ready "pod/$COPY_POD"')

        self.assertLess(files_claim, state_claim)
        self.assertLess(state_claim, copy_pod)
        self.assertLess(copy_pod, copy_ready)
        self.assertNotIn("wait --for=jsonpath='{.status.phase}'=Bound", prepare)

    def test_pre_writer_pvc_failure_restarts_compose_without_restoring_empty_claims(self):
        """A Pending first-consumer claim has no newer writer state to restore."""
        text = SCRIPT.read_text(encoding="utf-8")
        cleanup = text[text.index("remove_partial_resources() {") : text.index("abort_cutover() {")]

        self.assertIn("K3S_WRITER_STARTED=0", text)
        self.assertIn('[ "$K3S_WRITER_STARTED" -eq 0 ] || restore_files_from_pvc', cleanup)
        self.assertIn('[ "$K3S_WRITER_STARTED" -eq 0 ] || restore_state_from_pvc', cleanup)

    def test_kubernetes_temporary_pod_names_normalize_the_run_id_to_lowercase(self):
        """The default UTC run id contains T/Z and is invalid in Kubernetes names."""
        text = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('RUN_ID="${RUN_ID:-$(date -u +%Y%m%dt%H%M%Sz)-$$}"', text)
        self.assertIn('K8S_RUN_ID="$RUN_ID"', text)
        self.assertIn("''|*[!a-z0-9-]*) return 1", text)
        self.assertIn('COPY_POD="portal-web-files-copy-${K8S_RUN_ID}"', text)
        self.assertIn('FILES_RESTORE_POD="portal-web-files-restore-${K8S_RUN_ID}"', text)
        self.assertIn('STATE_RESTORE_POD="portal-web-state-restore-${K8S_RUN_ID}"', text)
        self.assertIn('valid_k8s_run_id "$K8S_RUN_ID"', text)

    def test_nodeport_private_check_rejects_a_reachable_non_bridge_address(self):
        """The switch gate must fail when the live NodePort answers on a host address."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_docker = root / "docker"
            fake_docker.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = network ] && [ \"$2\" = inspect ]; then echo 172.17.0.1; exit 0; fi\n"
                "exit 99\n"
            )
            fake_docker.chmod(0o755)
            fake_ip = root / "ip"
            fake_ip.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  '-4 addr show') echo '2: docker0    inet 172.17.0.1/16 scope global docker0' ;;\n"
                "  '-4 -o addr show scope global') printf '%s\\n' '2: docker0    inet 172.17.0.1/16 scope global docker0' '3: eth0    inet 10.20.30.40/24 scope global eth0' ;;\n"
                "esac\n"
            )
            fake_ip.chmod(0o755)
            fake_curl = root / "curl"
            fake_curl.write_text("#!/bin/sh\nexit 0\n")
            fake_curl.chmod(0o755)
            result = subprocess.run(
                ["/bin/bash", str(SCRIPT), "--check-nodeport-private"],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "DOCKER_BRIDGE_GATEWAY": "172.17.0.1",
                    "RUN_ID": "private-nodeport",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("portal_nodeport_private=FAIL", result.stderr)

    def test_transition_document_records_operator_cutover_gates(self):
        text = DOC.read_text(encoding="utf-8")
        for required in (
            "operator-only",
            "--go",
            "--switch-caddy",
            "--rollback-caddy",
            "backup",
            "sha256",
            "0600",
        ):
            self.assertIn(required, text)

    def test_rolledback_cleanup_refuses_unhealthy_compose_before_any_k3s_delete(self):
        """Residual cleanup must not delete K3s data unless Compose is healthy."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = root / "calls"
            fake_docker = root / "docker"
            fake_docker.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"docker $*\" >> '{calls}'\n"
                "if [ \"$1\" = inspect ]; then printf 'true unhealthy\\n'; exit 0; fi\n"
                "exit 99\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)
            fake_sudo = root / "sudo"
            fake_sudo.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"sudo $*\" >> '{calls}'\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_sudo.chmod(0o755)
            marker = root / "portal-runtime.mode"
            marker.write_text("compose\n", encoding="utf-8")
            result = subprocess.run(
                ["/bin/bash", str(SCRIPT), "--cleanup-rolledback"],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "PORTAL_RUNTIME_MARKER": str(marker),
                    "PORTAL_NAMESPACE": "personal-server",
                    "RUN_ID": "cleanup-unhealthy",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Compose Portal", result.stderr)
            recorded = calls.read_text(encoding="utf-8") if calls.exists() else ""
            self.assertNotIn("delete", recorded)

    def test_rolledback_cleanup_checks_all_writer_preconditions_before_pvc_deletion(self):
        text = SCRIPT.read_text(encoding="utf-8")
        cleanup = text[text.index("cleanup_rolledback_resources() {") : text.index("assert_namespace() {")]

        self.assertIn("RUNTIME_MARKER", text)
        self.assertIn("State.Health.Status", text)
        self.assertIn("personal-server", cleanup)
        self.assertIn("assert_k3s_writer_stopped_for_cleanup", cleanup)
        self.assertLess(cleanup.index("assert_compose_writer_healthy"), cleanup.index("delete pvc"))
        self.assertLess(cleanup.index("assert_k3s_writer_stopped_for_cleanup"), cleanup.index("delete pvc"))
        self.assertLess(cleanup.index('delete secret portal-web-runtime'), cleanup.index('delete pvc'))
        self.assertLess(cleanup.index('delete pvc'), cleanup.index("assert_cleanup_resource_absent pvc"))

    def test_rolledback_cleanup_removes_and_verifies_named_portal_resources(self):
        text = SCRIPT.read_text(encoding="utf-8")
        cleanup = text[text.index("cleanup_rolledback_resources() {") : text.index("assert_namespace() {")]

        for required in (
            "delete deployment portal-web",
            "delete service portal-web",
            "delete secret portal-web-runtime",
            "delete endpointslice -l app.kubernetes.io/part-of=portal-compose-bridge",
            "delete service -l app.kubernetes.io/part-of=portal-compose-bridge",
            'delete pvc "$PVC_NAME"',
            'delete pvc "$STATE_PVC_NAME"',
            "assert_cleanup_resource_absent deployment portal-web",
            "assert_cleanup_resource_absent service portal-web",
            "assert_cleanup_resource_absent secret portal-web-runtime",
            "assert_cleanup_selector_absent endpointslice app.kubernetes.io/part-of=portal-compose-bridge",
            "assert_cleanup_selector_absent service app.kubernetes.io/part-of=portal-compose-bridge",
            'assert_cleanup_resource_absent pvc "$PVC_NAME"',
            'assert_cleanup_resource_absent pvc "$STATE_PVC_NAME"',
            "portal_cleanup=PASS",
            "portal_cleanup=FAIL",
        ):
            self.assertIn(required, cleanup)

    def test_rolledback_cleanup_refuses_ready_k3s_writer_without_deleting_pvcs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = root / "calls"
            fake_docker = root / "docker"
            fake_docker.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"docker $*\" >> '{calls}'\n"
                "printf 'true healthy\\n'\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)
            fake_sudo = root / "sudo"
            fake_sudo.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"sudo $*\" >> '{calls}'\n"
                "case \"$*\" in\n"
                "  *'jsonpath={.spec.replicas}'*) printf '1\\n'; exit 0 ;;\n"
                "  *'jsonpath={.status.readyReplicas}'*) printf '1\\n'; exit 0 ;;\n"
                "esac\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_sudo.chmod(0o755)
            marker = root / "portal-runtime.mode"
            marker.write_text("compose\n", encoding="utf-8")
            result = subprocess.run(
                ["/bin/bash", str(SCRIPT), "--cleanup-rolledback"],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "PORTAL_RUNTIME_MARKER": str(marker),
                    "PORTAL_NAMESPACE": "personal-server",
                    "RUN_ID": "cleanup-ready-writer",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ready or desired writer replicas", result.stderr)
            recorded = calls.read_text(encoding="utf-8")
            self.assertNotIn(" delete pvc ", recorded)

    def test_rolledback_cleanup_success_removes_only_named_portal_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = root / "calls"
            fake_docker = root / "docker"
            fake_docker.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"docker $*\" >> '{calls}'\n"
                "printf 'true healthy\\n'\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)
            fake_sudo = root / "sudo"
            fake_sudo.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"sudo $*\" >> '{calls}'\n"
                "case \"$*\" in *'get pod -o json'*) printf '{}\\n'; exit 0 ;; esac\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_sudo.chmod(0o755)
            marker = root / "portal-runtime.mode"
            marker.write_text("compose\n", encoding="utf-8")
            result = subprocess.run(
                ["/bin/bash", str(SCRIPT), "--cleanup-rolledback"],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "PORTAL_RUNTIME_MARKER": str(marker),
                    "PORTAL_NAMESPACE": "personal-server",
                    "RUN_ID": "cleanup-success",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("portal_cleanup=PASS", result.stdout)
            recorded = calls.read_text(encoding="utf-8")
            self.assertIn("delete deployment portal-web", recorded)
            self.assertIn("delete service portal-web", recorded)
            self.assertIn("delete secret portal-web-runtime", recorded)
            self.assertIn("delete endpointslice -l app.kubernetes.io/part-of=portal-compose-bridge", recorded)
            self.assertIn("delete service -l app.kubernetes.io/part-of=portal-compose-bridge", recorded)
            self.assertIn("delete pod portal-web-files-copy-cleanup-success", recorded)
            self.assertIn("delete pod portal-web-files-restore-cleanup-success", recorded)
            self.assertIn("delete pod portal-web-state-restore-cleanup-success", recorded)
            self.assertIn("delete pvc portal-web-files-dynamic", recorded)
            self.assertIn("delete pvc portal-web-state-dynamic", recorded)
            self.assertNotIn("docker compose", recorded)

            lines = recorded.splitlines()
            first_pvc_delete = next(i for i, line in enumerate(lines) if "delete pvc" in line)
            for fragment in (
                "get deployment portal-web",
                "get service portal-web",
                "get secret portal-web-runtime",
                "get endpointslice -l app.kubernetes.io/part-of=portal-compose-bridge",
                "get service -l app.kubernetes.io/part-of=portal-compose-bridge",
                "get pod portal-web-files-copy-cleanup-success",
                "get pod portal-web-files-restore-cleanup-success",
                "get pod portal-web-state-restore-cleanup-success",
            ):
                self.assertLess(
                    next(i for i, line in enumerate(lines) if fragment in line),
                    first_pvc_delete,
                )

    def test_rolledback_cleanup_refuses_pvc_name_overrides_before_any_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = root / "calls"
            fake_docker = root / "docker"
            fake_docker.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"docker $*\" >> '{calls}'\n"
                "printf 'true healthy\\n'\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)
            fake_sudo = root / "sudo"
            fake_sudo.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"sudo $*\" >> '{calls}'\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_sudo.chmod(0o755)
            marker = root / "portal-runtime.mode"
            marker.write_text("compose\n", encoding="utf-8")
            result = subprocess.run(
                ["/bin/bash", str(SCRIPT), "--cleanup-rolledback"],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "PORTAL_RUNTIME_MARKER": str(marker),
                    "PORTAL_NAMESPACE": "personal-server",
                    "PORTAL_PVC_NAME": "unrelated-pvc",
                    "RUN_ID": "cleanup-override",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("fixed Portal PVC names", result.stderr)
            recorded = calls.read_text(encoding="utf-8") if calls.exists() else ""
            self.assertNotIn("delete", recorded)

    def test_rolledback_cleanup_refuses_pvc_referencing_pod_before_pvc_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = root / "calls"
            fake_docker = root / "docker"
            fake_docker.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"docker $*\" >> '{calls}'\n"
                "printf 'true healthy\\n'\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)
            fake_sudo = root / "sudo"
            fake_sudo.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"sudo $*\" >> '{calls}'\n"
                "case \"$*\" in\n"
                "  *'get pod -l app.kubernetes.io/name=portal-web --field-selector=status.phase=Running -o name'*) exit 0 ;;\n"
                "  *'get pod -o json'*) printf '%s\\n' '{\"items\":[{\"metadata\":{\"name\":\"orphan-copy\"},\"spec\":{\"volumes\":[{\"persistentVolumeClaim\":{\"claimName\":\"portal-web-files-dynamic\"}}]}}]}' ; exit 0 ;;\n"
                "esac\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_sudo.chmod(0o755)
            marker = root / "portal-runtime.mode"
            marker.write_text("compose\n", encoding="utf-8")
            result = subprocess.run(
                ["/bin/bash", str(SCRIPT), "--cleanup-rolledback"],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "PORTAL_RUNTIME_MARKER": str(marker),
                    "PORTAL_NAMESPACE": "personal-server",
                    "RUN_ID": "cleanup-pod-ref",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("still reference Portal PVCs", result.stderr)
            recorded = calls.read_text(encoding="utf-8")
            self.assertNotIn("delete pvc", recorded)


if __name__ == "__main__":
    unittest.main()
