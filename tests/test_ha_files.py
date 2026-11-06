"""Actual file API boundaries with a transactional shared-state test double."""
import copy
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from contextlib import contextmanager
import zipfile

from tests._test_support import prepare_service_import


class ProtectedUnavailable(sqlite3.OperationalError):
    pass


class SharedState:
    DatabaseUnavailable = ProtectedUnavailable

    def __init__(self):
        self.data = {}
        self.fail_commit = False

    @contextmanager
    def state_transaction(self, service, key, default):
        assert (service, key) == ("portal-web", "file-namespace")
        working = copy.deepcopy(self.data.get(key, default))
        yield working
        if self.fail_commit:
            raise ProtectedUnavailable("replication proof failed")
        self.data[key] = copy.deepcopy(working)


class HaFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve() / "objects"
        prepare_service_import("portal-web")
        self.store = importlib.import_module("app.services.file_store")
        self.manage = importlib.import_module("app.services.file_management")
        self.ha = importlib.import_module("app.services.ha_files")
        self.runtime = SharedState()
        self.environment = patch.dict(os.environ, {"HA_PG_CONFIG": "fixture", "APP_ENV": "test",
                                                    "HA_LOCAL_RUNTIME": "1", "HA_FILE_OBJECT_ROOT": str(self.root)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.runtime_patch = patch.object(self.ha, "runtime", return_value=self.runtime)
        self.runtime_patch.start()
        self.addCleanup(self.runtime_patch.stop)
        self.guard = patch.object(self.ha, "_guard")
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def upload(self, name="memo.txt", data=b"preserved"):
        return SimpleNamespace(filename=name, content_type="text/plain", file=io.BytesIO(data))

    def test_upload_listing_download_move_archive_and_original_name(self):
        self.store.create_directory("", "folder")
        self.assertEqual(self.store.save_upload("folder", self.upload()), 9)
        listing = self.store.get_directory("folder")
        self.assertEqual(listing["files"][0]["name"], "memo.txt")
        self.assertEqual(listing["files"][0]["content_type"], "text/plain")
        self.assertEqual(self.manage.move("folder/memo.txt", "", "renamed.txt"), "renamed.txt")
        path = self.store.get_download_path("renamed.txt")
        self.assertEqual(path.name, "renamed.txt")
        self.assertEqual(path.read_bytes(), b"preserved")
        self.assertEqual(path.stat().st_mode & 0o222, 0)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            self.store.write_download_archive(archive, ["folder", "renamed.txt"])
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.namelist(), ["renamed.txt"])
            self.assertEqual(archive.read("renamed.txt"), b"preserved")
        self.assertEqual(self.manage.search("rename")["items"][0]["path"], "renamed.txt")

    def test_bulk_failure_rolls_back_namespace_but_retains_published_orphan(self):
        with self.assertRaises(FileExistsError):
            self.store.save_uploads("", [self.upload(), self.upload()])
        self.assertEqual(self.store.get_directory()["files"], [])
        self.assertTrue((self.root / hashlib.sha256(b"preserved").hexdigest()).is_file())

    def test_http_download_preserves_original_filename_and_content(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.routers import files
        self.store.save_upload("", self.upload("original.txt"))
        application = FastAPI()
        application.include_router(files.router)
        with patch.dict(os.environ, {"FILE_MANAGER_AUTH_REQUIRED": "false"}), patch.object(files, "append_security_event"):
            response = TestClient(application).get("/files/download", params={"path": "original.txt"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"preserved")
        self.assertIn("original.txt", response.headers["content-disposition"])
        self.assertEqual(response.headers["content-type"], "text/plain; charset=utf-8")

    def test_download_snapshot_survives_shared_path_and_inode_replacement(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.routers import files
        self.store.save_upload("", self.upload())
        blob = self.root / hashlib.sha256(b"preserved").hexdigest()
        snapshot = self.store.get_download_path("memo.txt")
        blob.chmod(0o600)
        blob.write_bytes(b"mutated!!")
        self.assertEqual(snapshot.read_bytes(), b"preserved")
        blob.chmod(0o400)
        blob.unlink()
        outside = self.root.parent / "external.txt"
        outside.write_bytes(b"external secret")
        blob.symlink_to(outside)
        self.assertEqual(snapshot.read_bytes(), b"preserved")
        application = FastAPI()
        application.include_router(files.router)
        with patch.dict(os.environ, {"FILE_MANAGER_AUTH_REQUIRED": "false"}), patch.object(files, "append_security_event"), patch.object(self.store, "get_download_path", return_value=snapshot):
            response = TestClient(application).get("/files/download", params={"path": "memo.txt"})
        self.assertEqual(response.content, b"preserved")
        snapshot.close()

    def test_replication_proof_failure_cannot_report_success(self):
        self.runtime.fail_commit = True
        with self.assertRaises(ProtectedUnavailable):
            self.store.save_upload("", self.upload())
        self.assertNotIn("file-namespace", self.runtime.data)
        self.assertTrue((self.root / hashlib.sha256(b"preserved").hexdigest()).is_file())

    def test_fsync_failure_does_not_publish_namespace(self):
        self.ha.ensure_storage()
        with patch.object(self.ha.os, "fsync", side_effect=OSError("fsync failed")):
            with self.assertRaises(ProtectedUnavailable):
                self.store.save_upload("", self.upload())
        self.assertEqual(self.store.get_directory()["files"], [])

    def test_trash_restore_and_purge_are_transactional_and_keep_blobs(self):
        self.store.save_upload("", self.upload())
        token = self.manage.trash("memo.txt")
        self.assertEqual(self.store.get_directory()["files"], [])
        self.assertEqual(self.manage.trash_page()["size_bytes"], 9)
        self.assertEqual(self.manage.restore(token, "back.txt"), "back.txt")
        token = self.manage.trash("back.txt")
        preview = self.manage.preview_purge(item_id=token)
        manifest = self.ha.backup_manifest()
        self.assertNotIn(preview["confirmation"], json.dumps(manifest))
        self.assertIn(hashlib.sha256(preview["confirmation"].encode()).hexdigest(), manifest["confirmations"])
        self.assertEqual(preview["count"], 1)
        self.assertEqual(self.manage.confirm_purge(preview["confirmation"])["deleted"], [token])
        with self.assertRaises(ValueError):
            self.manage.confirm_purge(preview["confirmation"])
        self.assertTrue((self.root / hashlib.sha256(b"preserved").hexdigest()).is_file())
        manifest = self.ha.backup_manifest()
        self.assertEqual(manifest["blob_gc"], "disabled-until-backup-pins")
        self.assertTrue(any(item["event"] == "file_trash_purged" for item in manifest["audit"]))

    def test_restore_conflict_and_stale_purge_preserve_both_items(self):
        self.store.save_upload("", self.upload())
        token = self.manage.trash("memo.txt")
        self.store.save_upload("", self.upload(data=b"new"))
        with self.assertRaises(FileExistsError):
            self.manage.restore(token)
        preview = self.manage.preview_purge()
        self.manage.trash("memo.txt")
        with self.assertRaises(ValueError):
            self.manage.confirm_purge(preview["confirmation"])
        self.assertEqual(len(self.manage.list_trash()), 2)

    def test_logical_bounds_private_files_and_database_content_are_rejected(self):
        for name in ["../secret.txt", ".env", "db.sqlite3", "id_rsa", "private.key", "pgdata", "bad\\name.txt"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.create_directory("", name)
        for data in [b"SQLite format 3\\x00payload".replace(b"\\x00", b"\x00"), b"-----BEGIN PRIVATE KEY-----", bytes.fromhex("377f0682") + b"wal"]:
            with self.assertRaises(ValueError):
                self.store.save_upload("", self.upload(data=data))
        self.assertEqual(self.store.get_directory()["files"], [])

    def test_upload_and_download_limits_use_live_and_trash_metadata(self):
        with patch.object(self.store, "MAX_UPLOAD_BYTES", 2):
            with self.assertRaises(ValueError):
                self.store.save_upload("", self.upload())
        self.store.save_upload("", self.upload())
        self.manage.trash("memo.txt")
        with patch.object(self.store, "STORAGE_QUOTA_BYTES", 10):
            with self.assertRaises(ValueError):
                self.store.save_upload("", self.upload("second.txt"))
        self.manage.restore(next(iter(self.runtime.data["file-namespace"]["trash"])))
        with patch.object(self.store, "MAX_DOWNLOAD_TOTAL_BYTES", 2):
            with self.assertRaises(ValueError):
                self.store.validate_download_limits(["memo.txt"])

    def test_symlink_hardlink_and_mutable_blob_are_fail_closed(self):
        self.store.save_upload("", self.upload())
        path = self.root / hashlib.sha256(b"preserved").hexdigest()
        link = self.root.parent / "hardlink"
        os.link(path, link)
        with self.assertRaises(ProtectedUnavailable):
            self.store.get_download_path("memo.txt")
        link.unlink()
        path.chmod(0o600)
        with self.assertRaises(ProtectedUnavailable):
            self.store.get_download_path("memo.txt")
        path.unlink()
        path.symlink_to(self.root.parent / "external.txt")
        with self.assertRaises(ProtectedUnavailable):
            self.store.get_download_path("memo.txt")

    def test_symlink_object_root_and_metadata_delete_fail_closed(self):
        outside = self.root.parent / "outside"
        outside.mkdir(mode=0o700)
        self.root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ProtectedUnavailable):
            self.store.save_upload("", self.upload())
        self.assertEqual(list(outside.iterdir()), [])

    def test_directory_iteration_delete_and_backup_hash_metadata(self):
        self.store.create_directory("", "folder")
        self.store.save_uploads("folder", [self.upload("one.txt"), self.upload("two.txt", b"second")])
        directory = self.store.get_download_item_path("folder")
        self.assertTrue(directory.is_dir())
        self.assertEqual(len(list(self.store.iter_download_files(directory))), 2)
        manifest = self.ha.backup_manifest()
        self.assertEqual(manifest["entries"]["folder/one.txt"]["sha256"], hashlib.sha256(b"preserved").hexdigest())
        with self.assertRaises(ValueError):
            self.store.validate_delete_items(["folder", "folder/one.txt"])
        self.store.delete_item("folder")
        self.assertEqual(self.store.get_directory()["directories"], [])

    def test_archive_uses_snapshot_when_shared_inode_changes_after_verification(self):
        self.store.save_upload("", self.upload())
        blob = self.root / hashlib.sha256(b"preserved").hexdigest()
        original = self.ha._download_snapshot
        def mutate_after_snapshot(*args):
            descriptor = original(*args)
            blob.chmod(0o600)
            blob.write_bytes(b"mutated!!")
            return descriptor
        output = io.BytesIO()
        with patch.object(self.ha, "_download_snapshot", side_effect=mutate_after_snapshot), zipfile.ZipFile(output, "w") as archive:
            self.store.write_download_archive(archive, ["memo.txt"])
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.read("memo.txt"), b"preserved")

    def test_production_guard_remains_disarmed(self):
        self.guard.stop()
        import sys
        config = SimpleNamespace(load=lambda service: None)
        with patch.dict(sys.modules, {"ha_runtime.config": config}), patch.dict(os.environ, {"APP_ENV": "production"}):
            with self.assertRaises(ProtectedUnavailable):
                self.store.create_directory("", "blocked")
