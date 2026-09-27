#!/usr/bin/env python3
"""YouTube Memo PVC encrypted backup with an isolated restore verification.

This runner is deliberately limited to one fixed workload and one PVC. It is
not an installation tool and must run only in the suspended CronJob's Pod.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath


NAMESPACE = "personal-server"
DEPLOYMENT = "youtube-memo"
PVC = "youtube-memo-data"
POD_LABEL = "app.kubernetes.io/name=youtube-memo"
MOUNT = Path("/data/youtube-memo")
SECRET = Path("/run/secrets/youtube-backup")
REMOTE_PARENT = "gdrive:PersonalServer-encrypted-backups"
REMOTE = REMOTE_PARENT + "/youtube-memo"
STATE_CONFIGMAP = "youtube-pvc-backup-state"
DB_NAME = "youtube_memo.sqlite3"
COMMAND_TIMEOUT = 120
READY_TIMEOUT = 300


class BackupError(RuntimeError):
    """A fixed-label backup failure; never includes command diagnostics."""


def run_command(*argv: str, timeout: int = COMMAND_TIMEOUT) -> str:
    try:
        result = subprocess.run(argv, check=True, text=True, capture_output=True, timeout=timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        raise BackupError("external command failed") from error
    return result.stdout


def kubectl(*argv: str, timeout: int = COMMAND_TIMEOUT) -> str:
    return run_command("kubectl", "-n", NAMESPACE, *argv, timeout=timeout)


def k8s_json(*argv: str) -> dict:
    try:
        value = json.loads(kubectl(*argv, "-o", "json"))
    except (ValueError, TypeError) as error:
        raise BackupError("Kubernetes response invalid") from error
    if not isinstance(value, dict):
        raise BackupError("Kubernetes response invalid")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def regular_tree(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        raise BackupError("PVC tree invalid")
    entries = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    for entry in entries:
        mode = entry.lstat().st_mode
        if not stat.S_ISREG(mode) and not stat.S_ISDIR(mode):
            raise BackupError("PVC contains unsupported entry")
    return entries


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for entry in regular_tree(root):
        relative = entry.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative + b"\0")
        if entry.is_dir():
            digest.update(b"dir\0")
            continue
        digest.update(b"file\0")
        with entry.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    return "sha256:" + digest.hexdigest()


def check_sqlite(root: Path) -> None:
    database = root / DB_NAME
    if not database.is_file() or database.is_symlink():
        raise BackupError("YouTube database missing")
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=10) as connection:
            rows = connection.execute("PRAGMA quick_check").fetchall()
    except sqlite3.Error as error:
        raise BackupError("YouTube database check failed") from error
    if rows != [("ok",)]:
        raise BackupError("YouTube database check failed")


def create_snapshot(source: Path, archive: Path) -> str:
    entries = regular_tree(source)
    check_sqlite(source)
    digest = tree_digest(source)
    with tarfile.open(archive, "w") as output:
        for entry in entries:
            output.add(entry, arcname=entry.relative_to(source).as_posix(), recursive=False)
    return digest


def restore_snapshot(archive: Path, destination: Path, expected_digest: str) -> None:
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    with tarfile.open(archive, "r") as source:
        for item in source:
            relative = PurePosixPath(item.name)
            if (relative.is_absolute() or not relative.parts or
                    any(part in {".", ".."} for part in relative.parts) or
                    not (item.isfile() or item.isdir())):
                raise BackupError("snapshot entry invalid")
            target = destination.joinpath(*relative.parts)
            if item.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                content = source.extractfile(item)
                if content is None:
                    raise BackupError("snapshot file invalid")
                with target.open("xb") as output:
                    for block in iter(lambda: content.read(1024 * 1024), b""):
                        output.write(block)
    if tree_digest(destination) != expected_digest:
        raise BackupError("restored tree digest mismatch")
    check_sqlite(destination)


def required_secret_files() -> tuple[Path, Path, Path, Path]:
    paths = tuple(SECRET / name for name in (
        "rclone-config", "rclone-config-passphrase", "age-recipient", "age-identity"
    ))
    if any(not path.is_file() or not os.access(path, os.R_OK) for path in paths):
        raise BackupError("backup credential files unavailable")
    return paths


def rclone_args() -> tuple[str, ...]:
    config, password, _, _ = required_secret_files()
    return ("rclone", "--config", str(config), "--password-command", f"/usr/bin/cat {password}")


def workload_state() -> tuple[str, str]:
    deployment = k8s_json("get", "deployment", DEPLOYMENT)
    pvc = k8s_json("get", "pvc", PVC)
    deployment_uid = deployment.get("metadata", {}).get("uid")
    pvc_uid = pvc.get("metadata", {}).get("uid")
    if (not deployment_uid or not pvc_uid or deployment.get("spec", {}).get("replicas") != 1 or
            deployment.get("status", {}).get("availableReplicas") != 1 or
            pvc.get("status", {}).get("phase") != "Bound" or
            pvc.get("spec", {}).get("accessModes") != ["ReadWriteOnce"]):
        raise BackupError("YouTube workload preflight failed")
    return deployment_uid, pvc_uid


def assert_lock_available() -> None:
    state = k8s_json("get", "configmap", STATE_CONFIGMAP)
    if state.get("data", {}).get("lock_run_id") != "":
        raise BackupError("previous YouTube backup requires review")


def assert_same_pvc(expected_uid: str) -> None:
    pvc = k8s_json("get", "pvc", PVC)
    if pvc.get("metadata", {}).get("uid") != expected_uid or pvc.get("status", {}).get("phase") != "Bound":
        raise BackupError("YouTube PVC changed during backup")


def deployment_scale_version(expected_uid: str, expected_replicas: int) -> str:
    deployment = k8s_json("get", "deployment", DEPLOYMENT)
    metadata = deployment.get("metadata", {})
    version = metadata.get("resourceVersion")
    if (metadata.get("uid") != expected_uid or not isinstance(version, str) or not version or
            deployment.get("spec", {}).get("replicas") != expected_replicas):
        raise BackupError("YouTube Deployment changed before scale")
    return version


def wait_for_pods_absent() -> None:
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        pods = k8s_json("get", "pods", "-l", POD_LABEL)
        if pods.get("items") == []:
            return
        time.sleep(2)
    raise BackupError("YouTube writer did not stop")


def wait_for_ready() -> None:
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        deployment = k8s_json("get", "deployment", DEPLOYMENT)
        if deployment.get("spec", {}).get("replicas") == 1 and deployment.get("status", {}).get("availableReplicas") == 1:
            return
        time.sleep(2)
    raise BackupError("YouTube writer did not recover")


def patch_state(values: dict[str, str], *, lock_expected: str) -> None:
    operations = [{"op": "test", "path": "/data/lock_run_id", "value": lock_expected}]
    operations.extend({"op": "add", "path": f"/data/{key}", "value": value} for key, value in values.items())
    kubectl("patch", "configmap", STATE_CONFIGMAP, "--type=json", "--patch=" + json.dumps(operations, separators=(",", ":")))


class YouTubeBackupController:
    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}"
        self.writer_restore_needed = False
        self.lock_held = False
        self.failure_stage = "preflight"
        self.writer_recovered = True
        self.deployment_uid = ""
        self.pause_scale_confirmed = False

    def _update_state(self, status: str, stage: str, *, evidence: str = "", release: bool = False) -> None:
        # The same atomic patch owns the lock, status, and evidence. A failed
        # patch cannot publish success or allow a later run to overlap.
        patch_state({
            "lock_run_id": "" if release else self.run_id,
            "evidence": evidence, "run_id": self.run_id, "status": status,
            "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "stage": stage,
        }, lock_expected=self.run_id if self.lock_held else "")
        self.lock_held = not release

    def _restore_writer(self) -> None:
        if self.writer_restore_needed:
            if not self.pause_scale_confirmed:
                raise BackupError("YouTube writer pause outcome uncertain")
            version = deployment_scale_version(self.deployment_uid, 0)
            kubectl("scale", f"deployment/{DEPLOYMENT}", "--replicas=1",
                    "--current-replicas=0", f"--resource-version={version}")
            wait_for_ready()
            if k8s_json("get", "deployment", DEPLOYMENT).get("metadata", {}).get("uid") != self.deployment_uid:
                raise BackupError("YouTube Deployment changed during writer recovery")
            self.writer_restore_needed = False

    def run_backup(self) -> str:
        self._update_state("running", "preflight")
        stage = "preflight"
        failed = False
        writer_recovered = True
        try:
            required_secret_files()
            deployment_uid, pvc_uid = workload_state()
            self.deployment_uid = deployment_uid
            check_sqlite(MOUNT)
            run_command(*rclone_args(), "lsd", "--max-depth", "1", REMOTE_PARENT, timeout=30)
            self.writer_restore_needed = True
            if workload_state() != (deployment_uid, pvc_uid):
                raise BackupError("YouTube workload changed before backup")
            stage = "writer-pause"
            version = deployment_scale_version(deployment_uid, 1)
            kubectl("scale", f"deployment/{DEPLOYMENT}", "--replicas=0",
                    "--current-replicas=1", f"--resource-version={version}")
            self.pause_scale_confirmed = True
            wait_for_pods_absent()
            assert_same_pvc(pvc_uid)
            stage = "snapshot"
            archive = self.workdir / "youtube.tar"
            source_digest = create_snapshot(MOUNT, archive)
            stage = "writer-recovery"
            self._restore_writer()
            stage = "encryption"
            _, _, recipient, identity = required_secret_files()
            ciphertext = self.workdir / "youtube.tar.age"
            run_command("age", "-R", str(recipient), "-o", str(ciphertext), str(archive), timeout=600)
            artifact_digest = sha256(ciphertext)
            remote_object = f"{REMOTE}/{self.run_id}.tar.age"
            stage = "remote-upload"
            run_command(*rclone_args(), "copyto", "--immutable", str(ciphertext), remote_object, timeout=3600)
            stage = "remote-restore"
            download = self.workdir / "download.age"
            run_command(*rclone_args(), "copyto", remote_object, str(download), timeout=3600)
            if sha256(download) != artifact_digest:
                raise BackupError("downloaded ciphertext digest mismatch")
            restored_tar = self.workdir / "restored.tar"
            run_command("age", "-d", "-i", str(identity), "-o", str(restored_tar), str(download), timeout=600)
            stage = "restore-validation"
            restore_snapshot(restored_tar, self.workdir / "restored", source_digest)
            now = datetime.now(timezone.utc)
            evidence = "\n".join((
                "schema_version=1", "scope=youtube-memo", "backup_status=success", "encrypted=true",
                f"backup_completed_at={now:%Y-%m-%dT%H:%M:%SZ}", "restore_status=success",
                f"restore_verified_at={now:%Y-%m-%dT%H:%M:%SZ}",
                f"evidence_expires_at={(now + timedelta(days=1)):%Y-%m-%dT%H:%M:%SZ}",
                f"backup_id=youtube-{self.run_id}", f"artifact_digest={artifact_digest}",
                f"source_digest={source_digest}", "source_runtime=k3s-pvc",
                "restore_check=sqlite_quick_check", "restore_path_check=success", "",
            ))
            return evidence
        except Exception:
            failed = True
            raise
        finally:
            try:
                self._restore_writer()
            except Exception:
                failed = True
                writer_recovered = False
                stage = "writer-recovery"
            if failed:
                self.failure_stage = stage
                self.writer_recovered = writer_recovered
            if failed and sys.exc_info()[0] is None:
                raise BackupError("YouTube backup cleanup failed")

    def publish_failure(self) -> None:
        if self.lock_held:
            self._update_state("failed", self.failure_stage, release=self.writer_recovered)

    def publish_success(self, evidence: str) -> None:
        # The caller invokes this only after TemporaryDirectory has removed
        # every plaintext snapshot and isolated restore file.
        try:
            self._update_state("completed", "completed", evidence=evidence, release=True)
        except Exception:
            if self.lock_held:
                try:
                    self._update_state("failed", "evidence", release=True)
                except Exception:
                    pass  # Keep the lock and blank evidence for manual review.
            raise


def run_go() -> None:
    operation_error = None
    with tempfile.TemporaryDirectory(prefix="youtube-pvc-backup-", dir="/work") as directory:
        controller = YouTubeBackupController(Path(directory))
        try:
            evidence = controller.run_backup()
        except Exception as error:
            operation_error = error
    # A cleanup error exits the context before any state patch can release the lock.
    if operation_error is not None:
        controller.publish_failure()
        raise operation_error
    controller.publish_success(evidence)


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"--check", "--go"}:
        print("usage: youtube-pvc-backup-verify.py --check|--go", file=sys.stderr)
        return 2
    mode = sys.argv[1]
    os.umask(0o077)
    def stop(_signum: int, _frame: object) -> None:
        raise BackupError("backup interrupted")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        if mode == "--check":
            required_secret_files()
            workload_state()
            assert_lock_available()
            check_sqlite(MOUNT)
            run_command(*rclone_args(), "lsd", "--max-depth", "1", REMOTE_PARENT, timeout=30)
            print("youtube_pvc_backup_check=PASS")
            return 0
        run_go()
        print("youtube_pvc_backup=PASS")
        return 0
    except (BackupError, OSError, ValueError, tarfile.TarError):
        print("youtube_pvc_backup=FAIL", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
