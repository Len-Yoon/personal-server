import unittest
from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "infra" / "k8s" / "backup-automation" / "portal-pvc-backup-cronjob.yaml"
DOCKERFILE = ROOT / "infra" / "k8s" / "backup-automation" / "Dockerfile"


def documents():
    with MANIFEST.open(encoding="utf-8") as stream:
        return [doc for doc in yaml.safe_load_all(stream) if doc]


def find(kind, name, namespace=None):
    for doc in documents():
        metadata = doc.get("metadata", {})
        if doc.get("kind") == kind and metadata.get("name") == name:
            if namespace is None or metadata.get("namespace") == namespace:
                return doc
    raise AssertionError(f"missing {kind}/{name}")


def cronjob():
    return find("CronJob", "portal-pvc-backup", "personal-server")


def pod_spec():
    return cronjob()["spec"]["jobTemplate"]["spec"]["template"]["spec"]


class PortalPvcBackupCronJobTests(unittest.TestCase):
    def test_cronjob_is_suspended_singleton_and_never_retries(self):
        job = cronjob()
        self.assertTrue(job["spec"]["suspend"])
        self.assertEqual(job["spec"]["startingDeadlineSeconds"], 300)
        self.assertEqual(job["spec"]["concurrencyPolicy"], "Forbid")
        self.assertEqual(job["spec"]["jobTemplate"]["spec"]["backoffLimit"], 0)
        self.assertEqual(pod_spec()["restartPolicy"], "Never")

    def test_cronjob_runs_only_verifier_go_command(self):
        container = pod_spec()["containers"][0]
        self.assertEqual(container["command"], ["/opt/personal-server/portal-pvc-backup-verify.sh", "--go"])

    def test_backup_secret_is_file_only_with_fixed_keys(self):
        spec = pod_spec()
        self.assertNotIn("envFrom", spec["containers"][0])
        for item in spec["containers"][0].get("env", []):
            self.assertNotIn("valueFrom", item)
        secret_volumes = [v for v in spec["volumes"] if "secret" in v]
        self.assertEqual(len(secret_volumes), 1)
        secret = secret_volumes[0]["secret"]
        self.assertEqual(secret["secretName"], "portal-pvc-backup-runtime")
        self.assertTrue(secret["optional"] is False)
        self.assertEqual(
            {item["key"] for item in secret["items"]},
            {"rclone-config", "rclone-config-passphrase", "age-recipient", "age-identity"},
        )
        self.assertTrue(any(m["name"] == secret_volumes[0]["name"] and m["readOnly"] for m in spec["containers"][0]["volumeMounts"]))

    def test_pod_mounts_both_portal_pvcs_read_only(self):
        mounts = {m["name"]: m for m in pod_spec()["containers"][0]["volumeMounts"]}
        self.assertEqual(mounts["portal-files"]["mountPath"], "/data/files")
        self.assertEqual(mounts["portal-state"]["mountPath"], "/data/portal-web-state")
        self.assertTrue(mounts["portal-files"]["readOnly"])
        self.assertTrue(mounts["portal-state"]["readOnly"])
        volumes = {v["name"]: v for v in pod_spec()["volumes"]}
        self.assertEqual(volumes["portal-files"]["persistentVolumeClaim"]["claimName"], "portal-web-files-dynamic")
        self.assertEqual(volumes["portal-state"]["persistentVolumeClaim"]["claimName"], "portal-web-state-dynamic")

    def test_service_account_and_rbac_have_only_fixed_minimum_permissions(self):
        self.assertEqual(find("ServiceAccount", "portal-pvc-backup", "personal-server")["metadata"]["name"], "portal-pvc-backup")
        roles = [doc for doc in documents() if doc.get("kind") == "Role"]
        rules = [rule for role in roles for rule in role["rules"]]
        self.assertFalse(any("secrets" in rule.get("resources", []) for rule in rules))
        self.assertFalse(any("pods/exec" in rule.get("resources", []) for rule in rules))
        for rule in rules:
            self.assertNotIn("create", rule.get("verbs", []))
            self.assertNotIn("delete", rule.get("verbs", []))
        scale = [r for r in rules if r.get("resources") == ["deployments/scale"]]
        self.assertEqual(scale[0]["resourceNames"], ["portal-web"])
        self.assertEqual(set(scale[0]["verbs"]), {"get", "patch"})
        pvc = [r for r in rules if r.get("resources") == ["persistentvolumeclaims"]]
        self.assertEqual(pvc[0]["resourceNames"], ["portal-web-files-dynamic", "portal-web-state-dynamic"])

    def test_monitoring_status_rolebinding_targets_the_fixed_runner_service_account(self):
        binding = find("RoleBinding", "portal-pvc-backup-status", "monitoring")
        self.assertEqual(binding["roleRef"], {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": "portal-pvc-backup-status",
        })
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount",
            "name": "portal-pvc-backup",
            "namespace": "personal-server",
        }])

    def test_rbac_allows_only_rollout_watch_and_fixed_runner_resources(self):
        """The rollout wait needs watch, while every resource name remains fixed."""
        actual = {
            (role["metadata"]["namespace"], tuple(rule["resources"]), tuple(rule.get("resourceNames", []))): set(rule["verbs"])
            for role in (doc for doc in documents() if doc.get("kind") == "Role")
            for rule in role["rules"]
        }
        self.assertEqual(
            actual,
            {
                ("personal-server", ("persistentvolumeclaims",), ("portal-web-files-dynamic", "portal-web-state-dynamic")): {"get"},
                ("personal-server", ("deployments",), ("portal-web",)): {"get", "watch"},
                ("personal-server", ("pods",), ()): {"list"},
                ("personal-server", ("deployments/scale",), ("portal-web",)): {"get", "patch"},
                ("personal-server", ("configmaps",), ("portal-pvc-backup-evidence",)): {"get", "patch"},
                ("monitoring", ("configmaps",), ("sre-telegram-backup-status",)): {"get", "patch"},
            },
        )

    def test_manifest_does_not_overwrite_existing_telegram_or_evidence_configmaps(self):
        """The installer creates missing non-secret keys without applying over live status."""
        configmaps = [doc for doc in documents() if doc.get("kind") == "ConfigMap"]
        self.assertEqual(configmaps, [])

    def test_pvc_backed_pod_avoids_fsgroup_and_uses_only_prepared_emptydirs_for_writes(self):
        """fsGroup would mutate mounted PVC metadata even when the runner mounts it read-only."""
        spec = pod_spec()
        self.assertNotIn("fsGroup", spec["securityContext"])
        self.assertNotIn("fsGroupChangePolicy", spec["securityContext"])
        volumes = {volume["name"]: volume for volume in spec["volumes"]}
        self.assertEqual(volumes["backup-runtime"]["secret"]["defaultMode"], 0o444)
        self.assertEqual(volumes["work"]["emptyDir"]["sizeLimit"], "10Gi")
        self.assertEqual(volumes["tmp"]["emptyDir"]["sizeLimit"], "1Gi")
        init = spec["initContainers"]
        self.assertEqual(len(init), 1)
        self.assertEqual(init[0]["name"], "prepare-writable-scratch")
        self.assertEqual({mount["name"] for mount in init[0]["volumeMounts"]}, {"work", "tmp", "rclone-config-work"})
        self.assertIn("chown 10001:10001 /work /tmp", init[0]["command"][-1])
        environment = {item["name"]: item["value"] for item in spec["containers"][0]["env"]}
        self.assertEqual(environment["TMPDIR"], "/work")
        self.assertEqual(environment["PORTAL_RCLONE_CONFIG_WORK_DIR"], "/run/rclone-config")
        self.assertNotIn("medium", volumes["work"]["emptyDir"])
        self.assertNotIn("medium", volumes["tmp"]["emptyDir"])
        self.assertEqual(volumes["rclone-config-work"]["emptyDir"], {"medium": "Memory", "sizeLimit": "16Mi"})
        self.assertEqual(init[0]["command"][-1], "chown 10001:10001 /work /tmp /run/rclone-config && chmod 0700 /work /tmp /run/rclone-config")
        mounts = {mount["name"]: mount for mount in spec["containers"][0]["volumeMounts"]}
        self.assertEqual(mounts["rclone-config-work"], {"name": "rclone-config-work", "mountPath": "/run/rclone-config"})
        resources = spec["containers"][0]["resources"]
        self.assertEqual(resources["requests"]["ephemeral-storage"], "1Gi")
        self.assertEqual(resources["limits"]["ephemeral-storage"], "11Gi")

    def test_cronjob_uses_kst_retention_and_a_grace_period_for_portal_restore(self):
        """An interrupted backup needs time to restore the writer and must not retain Jobs indefinitely."""
        job_spec = cronjob()["spec"]
        pod = pod_spec()
        self.assertEqual(job_spec["timeZone"], "Asia/Seoul")
        self.assertEqual(job_spec["jobTemplate"]["spec"]["ttlSecondsAfterFinished"], 86400)
        self.assertGreaterEqual(pod["terminationGracePeriodSeconds"], 300)

    def test_container_is_non_root_read_only_and_capability_restricted(self):
        spec = pod_spec()
        security = spec["containers"][0]["securityContext"]
        self.assertTrue(security["runAsNonRoot"])
        self.assertTrue(security["readOnlyRootFilesystem"])
        self.assertFalse(security["allowPrivilegeEscalation"])
        self.assertEqual(security["capabilities"]["drop"], ["ALL"])

    def test_runner_image_uses_digest_pinned_base_and_bundles_verifier(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertRegex(
            text,
            re.compile(
                r"^FROM python:3\.11-alpine3\.24@sha256:d9368b3a5ac59afea7b5d4f2e2aea0941dbf9fdee9c369c5bec00b98244bc929$",
                re.MULTILINE,
            ),
        )
        self.assertIn("apk upgrade --no-cache", text)
        self.assertRegex(text, r"apk add[^\n]*\bkubectl\b")
        self.assertRegex(text, r"apk add[^\n]*\bflock\b")
        self.assertIn("setuptools==84.0.0 wheel==0.48.0", text)
        self.assertIn("ln -s /bin/cat /usr/bin/cat", text)
        self.assertIn("addgroup -S -g 10001 portal-backup", text)
        self.assertRegex(text, r"adduser[^\n]*-u 10001[^\n]*-G portal-backup")
        self.assertIn("portal-pvc-backup-verify.sh", text)
        self.assertIn("validate-backup-evidence.py", text)


class PortalOnlineSnapshotTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        import tempfile
        spec = importlib.util.spec_from_file_location("portal_online_snapshot", ROOT / "infra/k8s/tools/portal-online-snapshot.py")
        self.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.files, self.state, self.output = (base / name for name in ("files", "state", "output"))
        for path in (self.files, self.state, self.output):
            path.mkdir()

    def capture(self, **kwargs):
        return self.tool.capture(self.files, self.state, self.output, **kwargs)

    def test_pinned_inode_survives_rename_delete_and_live_writes(self):
        import fcntl
        import os
        source = self.files / "acknowledged.txt"
        source.write_bytes(b"acknowledged data")
        before = source.stat()
        def mutate():
            fd = os.open(self.files, os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                source.rename(self.files / "renamed.txt")
                (self.files / "renamed.txt").unlink()
                source.write_bytes(b"new live upload")
            finally:
                os.close(fd)
        result = self.capture(after_pin=mutate)
        self.assertEqual((self.output / "files/acknowledged.txt").read_bytes(), b"acknowledged data")
        self.assertEqual(source.read_bytes(), b"new live upload")
        self.assertEqual(result["consistency"], "per-store")
        self.assertEqual(before.st_nlink, 1)

    def test_capture_does_not_change_source_ctime(self):
        source = self.files / "item"
        source.write_bytes(b"data")
        before = source.stat()
        self.capture()
        after = source.stat()
        self.assertEqual((before.st_ctime_ns, before.st_nlink), (after.st_ctime_ns, after.st_nlink))

    def test_portfolio_atomic_replace_and_append_log_prefix(self):
        portfolio = self.files / ".portfolio"
        portfolio.mkdir()
        (portfolio / "portfolio.md").write_text("acknowledged")
        (portfolio / ".portfolio.md.pending").write_text("unfinished")
        logs = self.state / "security-events.txt"
        logs.mkdir()
        log = logs / "security-events-2026-10-28.txt"
        log.write_bytes(b'{"ack":true}\n{"partial":')
        def mutate():
            replacement = portfolio / ".portfolio.md.new"
            replacement.write_text("new live content")
            replacement.replace(portfolio / "portfolio.md")
            with log.open("ab") as stream:
                stream.write(b'true}\n')
        self.capture(after_pin=mutate)
        self.assertEqual((self.output / "files/.portfolio/portfolio.md").read_text(), "acknowledged")
        self.assertFalse((self.output / "files/.portfolio/.portfolio.md.pending").exists())
        self.assertEqual((self.output / "portal-web-state/security-events.txt" / log.name).read_bytes(), b'{"ack":true}\n')

    def test_all_known_sqlite_databases_use_online_backup_with_wal_commits(self):
        import sqlite3
        from contextlib import ExitStack, closing
        with ExitStack() as stack:
            databases = []
            for name in ("homeops.sqlite3", "news-bookmarks.sqlite3", "auth/session-state.sqlite3"):
                path = self.state / name
                path.parent.mkdir(exist_ok=True)
                db = stack.enter_context(closing(sqlite3.connect(path)))
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE ack (value TEXT)")
                db.execute("INSERT INTO ack VALUES ('before capture')")
                db.commit()
                databases.append((name, db))
            def mutate():
                for _, db in databases:
                    db.execute("INSERT INTO ack VALUES ('live write')")
                    db.commit()
            self.capture(after_pin=mutate)
            for name, _ in databases:
                with closing(sqlite3.connect(self.output / "portal-web-state" / name)) as restored:
                    self.assertEqual(restored.execute("PRAGMA quick_check").fetchone(), ("ok",))
                    self.assertEqual(restored.execute("SELECT value FROM ack").fetchall(), [("before capture",), ("live write",)])
                self.assertFalse(Path(str(self.output / "portal-web-state" / name) + "-wal").exists())

    def test_prior_deployment_sqlite_recovery_point_is_preserved(self):
        import hashlib
        import sqlite3
        from contextlib import closing
        name = hashlib.sha256(b"/var/lib/portal/homeops.sqlite3").hexdigest() + ".sqlite3"
        archive = self.state / ".deploy-backups" / "07c8f81" / name
        archive.parent.mkdir(parents=True)
        with closing(sqlite3.connect(archive)) as db:
            db.execute("CREATE TABLE accepted(value TEXT)")
            db.execute("INSERT INTO accepted VALUES ('prior recovery point')")
            db.commit()
        self.capture()
        restored = self.output / "portal-web-state" / archive.relative_to(self.state)
        with closing(sqlite3.connect(restored)) as db:
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone(), ("ok",))
            self.assertEqual(db.execute("SELECT value FROM accepted").fetchone(), ("prior recovery point",))
        self.assertTrue(archive.exists())

    def test_deployment_archive_accepts_only_known_database_paths(self):
        from pathlib import Path
        for name, directory in [(".deploy-backups/not-a-commit", True),
                                (".deploy-backups/07c8f81/" + "0" * 64 + ".sqlite3", False),
                                (".deploy-backups/07c8f81/arbitrary.json", False)]:
            with self.subTest(name=name):
                self.assertIsNone(self.tool.state_kind(Path(name), directory))

    def test_unknown_mutable_state_and_symlink_are_rejected_without_output(self):
        (self.state / "unknown").write_bytes(b"mutable")
        with self.assertRaises(self.tool.CaptureError):
            self.capture()
        self.assertEqual(list(self.output.iterdir()), [])
        (self.state / "unknown").unlink()
        (self.files / "link").symlink_to(self.state)
        with self.assertRaises(OSError):
            self.capture()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_in_place_mutation_fails_and_cleans_only_owned_output(self):
        source = self.files / "item"
        source.write_bytes(b"before")
        with self.assertRaises(self.tool.CaptureError):
            self.capture(after_pin=lambda: source.write_bytes(b"changed size"))
        self.assertEqual(source.read_bytes(), b"changed size")
        self.assertEqual(list(self.output.iterdir()), [])
        (self.output / "user-owned").write_text("keep")
        with self.assertRaises(self.tool.CaptureError):
            self.capture()
        self.assertEqual((self.output / "user-owned").read_text(), "keep")

    def test_lock_budget_times_out_and_releases_descriptors(self):
        import fcntl
        import os
        import time
        fd = os.open(self.files, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            started = time.monotonic()
            with self.assertRaises(self.tool.CaptureError):
                self.capture(lock_budget_ms=20)
            self.assertLess(time.monotonic() - started, .5)
        finally:
            os.close(fd)
        self.assertEqual(list(self.output.iterdir()), [])
        self.capture()

    def test_cancellation_and_fifo_do_not_leave_output_or_leaked_pins(self):
        import os
        from unittest.mock import patch
        source = self.files / "item"
        source.write_bytes(b"acknowledged")
        def cancel(*_args, **_kwargs):
            raise SystemExit(143)
        with patch.object(self.tool, "_copy_prefix", side_effect=cancel):
            with self.assertRaises(SystemExit):
                self.capture()
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(source.read_bytes(), b"acknowledged")
        os.mkfifo(self.files / "pipe")
        with self.assertRaises(self.tool.CaptureError):
            self.capture()
        (self.files / "pipe").unlink()
        self.capture()

    def test_chunk_capacity_failure_preserves_source_and_cleans_output(self):
        from unittest.mock import patch
        from collections import namedtuple
        usage = namedtuple("Usage", "total used free")
        source = self.files / "item"
        source.write_bytes(b"acknowledged")
        high, low = usage(10**9, 0, 10**9), usage(10**9, 10**9, 0)
        with patch.object(self.tool.shutil, "disk_usage", side_effect=[high, high, high, low]):
            with self.assertRaises(self.tool.CaptureError):
                self.capture()
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(source.read_bytes(), b"acknowledged")

    def test_large_flat_directory_stream_stops_before_budget_exhaustion(self):
        import os
        from unittest.mock import patch
        for index in range(128):
            (self.files / f"item-{index}").write_bytes(b"preserved")
        actual_scandir = os.scandir
        for mode in ("deadline", "entries"):
            with self.subTest(mode=mode):
                clock = [0.0]
                calls = [0]
                closed = [False]
                class StreamingEntries:
                    def __init__(self, fd):
                        self.entries = actual_scandir(fd)
                    def __enter__(self):
                        return self
                    def __exit__(self, *_args):
                        closed[0] = True
                        self.entries.close()
                    def __next__(self):
                        result = next(self.entries)
                        calls[0] += 1
                        if mode == "deadline":
                            clock[0] += .1
                        return result
                def scandir(path):
                    return StreamingEntries(path) if isinstance(path, int) else actual_scandir(path)
                with patch.object(self.tool.os, "scandir", side_effect=scandir), patch.object(self.tool.time, "monotonic", side_effect=lambda: clock[0]), patch.object(self.tool, "MAX_ENTRIES", 3 if mode == "entries" else 10000):
                    with self.assertRaises(self.tool.CaptureError):
                        self.capture()
                self.assertEqual(calls[0], 3)
                self.assertTrue(closed[0])
                self.assertEqual(list(self.output.iterdir()), [])
                self.assertEqual(len(list(self.files.iterdir())), 128)
                # Failure must release the root lock and every pinned source FD.
                fd = os.open(self.files, os.O_RDONLY)
                try:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(fd)
        self.capture()

    def test_wal_database_receives_only_remaining_aggregate_output_budget(self):
        import sqlite3
        from contextlib import closing
        from unittest.mock import patch
        (self.files / "earlier-file").write_bytes(b"x" * 60000)
        with closing(sqlite3.connect(self.state / "homeops.sqlite3")) as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute("CREATE TABLE payload (value BLOB)")
            database.execute("INSERT INTO payload VALUES (?)", (b"x" * 50000,))
            database.commit()
            self.assertLess((self.state / "homeops.sqlite3").stat().st_size, 40000)
            original = self.tool._backup_database
            with patch.object(self.tool, "_backup_database", wraps=original) as backup:
                with self.assertRaisesRegex(self.tool.CaptureError, "database output budget exceeded"):
                    self.capture(max_output_bytes=100000)
                self.assertEqual(backup.call_args.args[-1], 40000)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual((self.files / "earlier-file").stat().st_size, 60000)

    def test_capacity_and_descriptor_limits_fail_closed(self):
        from unittest.mock import patch
        from collections import namedtuple
        (self.files / "item").write_bytes(b"1234")
        usage = namedtuple("Usage", "total used free")(1000, 990, 10)
        with patch.object(self.tool.shutil, "disk_usage", return_value=usage):
            with self.assertRaises(self.tool.CaptureError):
                self.capture(min_free_bytes=1)
        with self.assertRaises(self.tool.CaptureError):
            self.capture(max_output_bytes=1)
        with patch.object(self.tool.resource, "getrlimit", return_value=(64, 64)):
            with self.assertRaises(self.tool.CaptureError):
                self.capture()
        self.assertEqual(list(self.output.iterdir()), [])


class PortalReadonlyRuntimeLabSafetyTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        import argparse
        spec = importlib.util.spec_from_file_location("portal_readonly_runtime", ROOT / "infra/k8s/tools/portal-online-backup-runtime-lab.py")
        self.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)
        self.args = argparse.Namespace(image_config="sha256:" + "a" * 64, source_sha="b" * 40, run_id="123", attempt="1", output=Path("/tmp/unused-runtime-guard-evidence.json"))
        self.env = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}

    def test_known_sqlite_wal_reset_fix_ranges_and_embedded_gates(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import patch
        for version in ("3.51.3", "3.51.4", "3.52.0", "3.50.7", "3.50.8", "3.44.6", "3.44.7"):
            with self.subTest(version=version):
                self.assertTrue(self.tool.sqlite_wal_reset_fixed(version))
                with patch.dict(sys.modules, {"sqlite3": SimpleNamespace(sqlite_version=version)}):
                    exec(self.tool.SQLITE_GATE, {})
        for version in ("3.45.1", "3.44.5", "3.50.6", "3.51.0", "3.51.2", "3.49.9", "3.51.3-vendor", "invalid", "3.51"):
            with self.subTest(version=version):
                self.assertFalse(self.tool.sqlite_wal_reset_fixed(version))
                with patch.dict(sys.modules, {"sqlite3": SimpleNamespace(sqlite_version=version)}):
                    with self.assertRaisesRegex(AssertionError, "unpatched_sqlite_runtime"):
                        exec(self.tool.SQLITE_GATE, {})
        self.assertTrue(self.tool.WRITER.startswith(self.tool.SQLITE_GATE))
        self.assertTrue(self.tool.READER.startswith(self.tool.SQLITE_GATE))

    def test_unpatched_image_stops_before_writer_and_records_public_version(self):
        import json
        import tempfile
        from argparse import Namespace
        from subprocess import CompletedProcess
        from unittest.mock import Mock, patch
        with tempfile.TemporaryDirectory() as directory:
            args = Namespace(**{**vars(self.args), "output": Path(directory) / "result.json"})
            runtime = Mock()
            runtime.create.return_value = "version-probe"
            runtime.docker.return_value = CompletedProcess([], 0, json.dumps({"sqlite_version": "3.45.1"}), "")
            with patch.dict(self.tool.os.environ, self.env, clear=True), patch.object(self.tool, "Runtime", return_value=runtime):
                with self.assertRaises(self.tool.SafetyError):
                    self.tool.run(args)
            self.assertEqual(runtime.create.call_count, 1)
            self.assertEqual(runtime.create.call_args.args[1], "probe")
            runtime.cleanup.assert_called_once()
            result = json.loads(args.output.read_text())
            self.assertEqual(result["sqlite_version"], "3.45.1")
            self.assertFalse(result["sqlite_wal_reset_fixed"])
            self.assertEqual(result["status"], "FAIL")

    def test_hosted_ci_only_and_docker_overrides_rejected_before_execution(self):
        from unittest.mock import Mock, patch
        for env in ({}, {**self.env, "RUNNER_ENVIRONMENT": "self-hosted"}, *({**self.env, key: ""} for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"))):
            with self.subTest(keys=sorted(env)):
                execute = Mock()
                with patch.dict(self.tool.os.environ, env, clear=True):
                    with self.assertRaises(self.tool.SafetyError):
                        self.tool.run(self.args, execute=execute)
                execute.assert_not_called()
        self.tool.validate_request(self.args, self.env)

    def test_exact_digest_source_and_run_identity_are_required(self):
        from argparse import Namespace
        for field, value in (("image_config", "image:latest"), ("source_sha", "main"), ("run_id", "1;prune"), ("attempt", "2")):
            args = Namespace(**{**vars(self.args), field: value})
            with self.assertRaises(self.tool.SafetyError):
                self.tool.validate_request(args, self.env)

    def test_remote_default_endpoint_is_rejected_before_image_or_create(self):
        from unittest.mock import Mock
        from subprocess import CompletedProcess
        execute = Mock(side_effect=[CompletedProcess([], 0, "default\n", ""), CompletedProcess([], 0, "tcp://remote.invalid:2375\n", "")])
        runtime = self.tool.Runtime(self.args, execute=execute)
        with self.assertRaises(self.tool.SafetyError):
            runtime.preflight()
        self.assertEqual(execute.call_count, 2)
        self.assertTrue(all(call.args[0][:3] == ["docker", "--context", "default"] for call in execute.call_args_list))

    def test_unowned_container_is_never_deleted(self):
        import json
        from unittest.mock import Mock
        from subprocess import CompletedProcess
        execute = Mock(side_effect=[CompletedProcess([], 0, "some-id\n", ""), CompletedProcess([], 0, json.dumps([{"Name": "/owned-name", "Config": {"Labels": {self.tool.LABEL: "foreign-owner"}}}]), "")])
        runtime = self.tool.Runtime(self.args, execute=execute)
        runtime.containers = ["owned-name"]
        with self.assertRaises(self.tool.SafetyError):
            runtime.cleanup()
        self.assertEqual(execute.call_count, 2)
        self.assertFalse(any("rm" in call.args[0] for call in execute.call_args_list))

    def test_reader_creation_requires_hardening_and_readonly_owned_fixture(self):
        import json
        from unittest.mock import Mock
        from subprocess import CompletedProcess
        execute = Mock()
        runtime = self.tool.Runtime(self.args, execute=execute)
        source = Path("/tmp/synthetic-source")
        def perform(command, **_kwargs):
            arguments = command[3:]
            if arguments[0] == "create":
                self.assertIn("--read-only", arguments)
                self.assertIn("--cap-drop", arguments)
                self.assertIn("no-new-privileges", arguments)
                self.assertIn("type=bind,source=/tmp/synthetic-source,target=/source,readonly", arguments)
                self.assertIn("10001:10001", arguments)
                return CompletedProcess(command, 0, "a" * 64, "")
            name = arguments[-1]
            value = {"Name": "/"+name, "Image": self.args.image_config, "Config": {"User": "10001:10001", "Labels": {self.tool.LABEL: runtime.owner}}, "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "Memory": 256*1024**2, "NanoCpus": 10**9, "PidsLimit": 64, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"], "PortBindings": {}}, "Mounts": [{"Destination": "/source", "Type": "bind", "Source": str(source), "RW": False}]}
            return CompletedProcess(command, 0, json.dumps([value]), "")
        execute.side_effect = perform
        name = runtime.create(source, "reader", "print('fixture')")
        self.assertIn(name, runtime.containers)
        self.assertEqual(runtime.mounts[name], (source, True))


if __name__ == "__main__":
    unittest.main()
