"""Artifact security/restore atomicity guards; real PG evidence is separate."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/k8s"))
from ha_runtime import backup
from ha_runtime.config import DatabaseUnavailable
from ha_runtime import backup_cli


class RestoreArtifactTests(unittest.TestCase):
    def test_effects_backup_requires_storage_acceptance_and_preserves_namespace(self):
        connection = MagicMock()
        names = ['ha', 'ha_effects'] + ['s_' + service.replace('-', '_') + '_state' for service in backup.SERVICES]
        connection.execute.return_value.fetchone.return_value = (0,)
        connection.execute.return_value.fetchall.return_value = [(name,) for name in names]
        with patch('ha_runtime.effects.validate_storage_catalog') as validate:
            self.assertEqual(backup._schemas(connection), names)
            validate.assert_called_once_with(connection)
        names.append('pgxevil')
        connection.execute.return_value.fetchall.return_value = [(name,) for name in names]
        with self.assertRaisesRegex(DatabaseUnavailable, 'isolated_backup_schema_mismatch'):
            backup._schemas(connection)

    def test_broker_role_restore_requires_preprovision_and_never_creates_roles(self):
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [('fixture_broker',)]
        required = backup._broker_role_requirements(connection, ['ha', 'ha_effects'])
        manifest = {'logical': {'schemas': ['ha', 'ha_effects']}, 'required_broker_roles': required}
        valid = (True, False, False, False, False, False, False, True)
        connection.execute.return_value.fetchone.return_value = valid
        backup._check_broker_role_requirements(connection, manifest)
        for invalid in (None, (True, True, False, False, False, False, False, True),
                        (True, False, True, False, False, False, False, True), valid[:-1] + (False,)):
            connection.execute.return_value.fetchone.return_value = invalid
            with self.assertRaisesRegex(DatabaseUnavailable, 'restore_broker_role_preprovision_required'):
                backup._check_broker_role_requirements(connection, manifest)
        self.assertTrue(all(call.args[0].lstrip().startswith('SELECT') for call in connection.execute.call_args_list))
        with self.assertRaisesRegex(DatabaseUnavailable, 'restore_effects_role_inventory_invalid'):
            backup._check_broker_role_requirements(connection, {'logical': {'schemas': ['ha_effects']}})
        backup._check_broker_role_requirements(connection, {'logical': {'schemas': ['ha']}})

    def test_aes_gcm_requires_correct_key_nonce_header_and_complete_tag(self):
        key = os.urandom(32)
        encrypted = backup._seal(b"synthetic protected fixture", key)
        self.assertNotIn(b"synthetic protected fixture", encrypted)
        self.assertEqual(backup._open(encrypted, key), b"synthetic protected fixture")
        variants = [encrypted[:-1], encrypted[:-1] + bytes([encrypted[-1] ^ 1]), b"x" + encrypted[1:], encrypted[:len(backup.MAGIC)] + b"x" + encrypted[len(backup.MAGIC) + 1:]]
        for value in variants:
            with self.subTest(value_length=len(value)), self.assertRaisesRegex(DatabaseUnavailable, "^backup_authentication_failed$"):
                backup._open(value, key)
        with self.assertRaisesRegex(DatabaseUnavailable, "^backup_authentication_failed$"):
            backup._open(encrypted, os.urandom(32))
        self.assertNotEqual(encrypted, backup._seal(b"synthetic protected fixture", key))

    def archive(self, *, blob=b"synthetic immutable attachment", names=None, declared=None):
        sha = hashlib.sha256(b"synthetic immutable attachment").hexdigest()
        manifest = {"format": 1, "scope": "local-isolated", "objects": {sha: len(b"synthetic immutable attachment")}, "dump_sha256": hashlib.sha256(b"dump").hexdigest()}
        if declared:
            manifest.update(declared)
        entries = names or [("manifest.json", json.dumps(manifest).encode()), ("database.dump", b"dump"), ("objects/" + sha, blob)]
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for name, value in entries:
                archive.writestr(name, value)
        return output.getvalue(), sha

    def test_all_members_are_validated_before_any_restore(self):
        valid, sha = self.archive()
        manifest, dump, objects = backup._unpack(valid)
        self.assertEqual(dump, b"dump")
        self.assertEqual(hashlib.sha256(objects[sha]).hexdigest(), sha)
        corrupt, _ = self.archive(blob=b"corrupt")
        with self.assertRaisesRegex(DatabaseUnavailable, "backup_blob_hash_mismatch"):
            backup._unpack(corrupt)
        with zipfile.ZipFile(io.BytesIO(valid)) as archive:
            entries = [(n, archive.read(n)) for n in archive.namelist()]
        for bad in (entries[:-1], entries + [("../escape", b"x")], entries + [("database.dump", b"duplicate")]):
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                data, _ = self.archive(names=bad)
            with self.assertRaises(DatabaseUnavailable):
                backup._unpack(data)

    def test_missing_corrupt_mutable_symlink_and_hardlinked_blobs_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            value = b"blob"
            sha = hashlib.sha256(value).hexdigest()
            path = root / sha
            with self.assertRaisesRegex(DatabaseUnavailable, "backup_blob_unavailable"):
                backup._blob(root, sha, len(value))
            backup._write_new(path, value, 0o400)
            self.assertEqual(backup._blob(root, sha, len(value)), value)
            path.chmod(0o600)
            with self.assertRaises(DatabaseUnavailable):
                backup._blob(root, sha, len(value))
            path.write_bytes(b"evil")
            path.chmod(0o400)
            with self.assertRaisesRegex(DatabaseUnavailable, "backup_blob_hash_mismatch"):
                backup._blob(root, sha, len(value))
            os.link(path, root / "hardlink")
            with self.assertRaises(DatabaseUnavailable):
                backup._blob(root, sha, len(value))
            path.unlink()
            path.symlink_to(root / "hardlink")
            with self.assertRaises(DatabaseUnavailable):
                backup._blob(root, sha, len(value))

    def test_key_artifact_and_parent_are_private_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "key"
            backup._write_new(path, os.urandom(32))
            self.assertEqual(len(backup._key(path)), 32)
            with self.assertRaises(FileExistsError):
                backup._write_new(path, b"replacement")
            path.chmod(0o644)
            with self.assertRaises(DatabaseUnavailable):
                backup._key(path)
            root.chmod(0o755)
            with self.assertRaises(DatabaseUnavailable):
                backup._write_new(root / "other", b"x")

    def test_blob_pins_cannot_drop_trash_references_or_conflicting_sizes(self):
        from unittest.mock import MagicMock
        raw = MagicMock()
        sha, other = "a" * 64, "b" * 64
        payload = {"entries": {"live": {"type": "file", "sha256": sha, "size": 2}}, "trash": {"token": {"entries": {"deleted": {"type": "file", "sha256": other, "size": 3}}}}}
        raw.execute.return_value.fetchone.return_value = (payload,)
        self.assertEqual(backup.object_references(raw), {sha: 2, other: 3})
        payload["trash"]["token"]["entries"]["deleted"]["sha256"] = sha
        with self.assertRaisesRegex(DatabaseUnavailable, "backup_file_reference_invalid"):
            backup.object_references(raw)

    def test_restore_seals_authority_in_same_single_transaction_as_data(self):
        from types import SimpleNamespace
        admin = {"host": "ha-a", "sslmode": "verify-full"}
        calls = []
        def run(command, **options):
            calls.append((command, options))
            return SimpleNamespace(returncode=0, stderr=b"", stdout=b"-- trusted pg dump SQL\n")
        with patch.object(backup.subprocess, "run", side_effect=run):
            backup._restore_database(admin, Path("/private/synthetic.dump"))
        command, options = calls[-1]
        self.assertIn("--single-transaction", command)
        self.assertIn("--set=ON_ERROR_STOP=1", command)
        self.assertIn(b"writes_open=false,history_safe=false", options["input"])
        self.assertIn(b"epoch=epoch+1,revision=revision+1", options["input"])
        self.assertTrue(options["input"].startswith(b"-- trusted pg dump SQL"))
        self.assertNotIn("PGPASSWORD", options["env"])
        self.assertNotIn("shell", options)

    def test_production_or_external_endpoints_refused_before_connection(self):
        with patch.dict(os.environ, {"APP_ENV": "production", "HA_LOCAL_RUNTIME": "1"}, clear=True), self.assertRaisesRegex(DatabaseUnavailable, "backup_purpose_approval_required"):
            backup._guard({})
        with patch.dict(os.environ, {"APP_ENV": "test", "HA_LOCAL_RUNTIME": "1"}, clear=True), self.assertRaisesRegex(DatabaseUnavailable, "authenticated_local_backup_only"):
            backup._guard({"host": "external.invalid"})


class ProductionBackupApprovalTests(unittest.TestCase):
    def setUp(self):
        from datetime import datetime, timedelta, timezone
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.approval_path = self.root / "approval.json"
        self.release = {"source_commit": "a" * 40, "release_id": "b" * 64, "source_files_sha256": "c" * 64}
        for name in ("passfile", "ca.crt"):
            (self.root / name).write_text("synthetic fixture only")
            (self.root / name).chmod(0o600)
        self.admin = dict(host="pg-fixture.invalid", port=5432, dbname="ha", user="fixture_admin", sslmode="verify-full",
                          sslrootcert=str(self.root / "ca.crt"), passfile=str(self.root / "passfile"))
        self.arguments = dict(action="backup-whole", object_root=str(self.root / "objects"), artifact_path=str(self.root / "artifact"), key_path=str(self.root / "key"), expected_cluster="approved-cluster")
        self.approval = dict(self.release, action="backup-whole", expires=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"), cluster="approved-cluster", system_id="123", node="A", admin=self.admin,
                             **{k: self.arguments[k] for k in ("object_root", "artifact_path", "key_path")})
        self.env = patch.dict(os.environ, {"APP_ENV": "production", "HA_BACKUP_APPROVAL": str(self.approval_path)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def approved_guard(self, approval=None, **changes):
        self.approval_path.write_text(json.dumps(self.approval if approval is None else approval))
        with patch.object(backup, "_release", return_value=self.release), patch.object(backup, "_authority_file", side_effect=lambda p: Path(p)):
            return backup._guard(self.admin, **dict(self.arguments, **changes))

    def test_purpose_release_endpoint_paths_and_expected_cluster_are_bound(self):
        self.assertEqual(self.approved_guard()["cluster"], "approved-cluster")
        cases = [dict(self.approval, action="restore-whole"), dict(self.approval, source_commit="d" * 40), dict(self.approval, source_files_sha256="d" * 64), dict(self.approval, cluster="different"), dict(self.approval, key_path="/different/key"), dict(self.approval, admin=dict(self.admin, sslmode="require"))]
        for approval in cases:
            with self.subTest(keys=[k for k in approval if approval[k] != self.approval.get(k)]), self.assertRaises(DatabaseUnavailable):
                self.approved_guard(approval)
        with self.assertRaisesRegex(DatabaseUnavailable, "backup_approval_path_mismatch"):
            self.approved_guard(object_root="/different/objects")

    def test_expired_open_ended_or_non_utc_approval_is_rejected(self):
        from datetime import datetime, timedelta, timezone
        for expiry in ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"), (datetime.now(timezone.utc) + timedelta(hours=25)).isoformat().replace("+00:00", "Z"), "2026-11-06T00:00:00+09:00", "invalidZ"):
            with self.subTest(expiry_kind=expiry[-1:]), self.assertRaises(DatabaseUnavailable):
                self.approved_guard(dict(self.approval, expires=expiry))

    def test_untrusted_authority_and_wrong_operation_cannot_reach_database(self):
        from unittest.mock import MagicMock
        self.approval_path.write_text(json.dumps(self.approval))
        with patch.object(backup, "_release", return_value=self.release), patch.object(backup, "_authority_file", side_effect=DatabaseUnavailable("trusted_activation_authority_required")), patch.object(backup, "_connect") as connect:
            with self.assertRaisesRegex(DatabaseUnavailable, "trusted_activation_authority_required"):
                backup.create_backup(self.admin, self.arguments["object_root"], self.arguments["artifact_path"], self.arguments["key_path"], expected_cluster="approved-cluster")
            connect.assert_not_called()
        with patch.object(backup, "_release", return_value=self.release), patch.object(backup, "_authority_file", side_effect=lambda p: Path(p)), patch.object(backup, "_connect") as connect:
            with self.assertRaisesRegex(DatabaseUnavailable, "backup_purpose_approval_mismatch"):
                backup.restore_backup(self.admin, self.arguments["object_root"], self.arguments["artifact_path"], self.arguments["key_path"], expected_cluster="approved-cluster")
            connect.assert_not_called()

    def test_source_and_new_target_pg_system_and_node_must_match_live_observation(self):
        from unittest.mock import MagicMock
        raw = MagicMock()
        raw.execute.return_value.fetchone.side_effect = [("ha_A",), (False,)]
        backup._source_identity(raw, self.approval, "approved-cluster", "123")
        with self.assertRaisesRegex(DatabaseUnavailable, "backup_observed_identity_mismatch"):
            backup._source_identity(raw, self.approval, "approved-cluster", "999")
        approval = dict(self.approval, target_system_id="456")
        raw.execute.return_value.fetchone.side_effect = [("ha_A",)]
        backup._target_identity(raw, approval, "456")
        with self.assertRaisesRegex(DatabaseUnavailable, "restore_observed_target_mismatch"):
            backup._target_identity(raw, approval, "999")

    def test_restore_requires_explicit_artifact_source_and_fresh_target_binding(self):
        approval = dict(self.approval, action="restore-whole", target_system_id="456", artifact_sha256="e" * 64)
        accepted = self.approved_guard(approval, action="restore-whole")
        manifest = dict(scope="n100-approved", cluster="approved-cluster", system_id="123")
        backup._restore_identity(accepted, manifest, "approved-cluster", "e" * 64)
        for changes in ({"scope": "local-isolated"}, {"system_id": "999"}, {"cluster": "different"}):
            with self.assertRaisesRegex(DatabaseUnavailable, "restore_(identity_mismatch|approval_source_mismatch)"):
                backup._restore_identity(accepted, dict(manifest, **changes), "approved-cluster", "e" * 64)
        with self.assertRaisesRegex(DatabaseUnavailable, "restore_approval_source_mismatch"):
            backup._restore_identity(accepted, manifest, "approved-cluster", "f" * 64)
        with self.assertRaisesRegex(DatabaseUnavailable, "restore_approval_target_required"):
            self.approved_guard(dict(approval, target_system_id="123"), action="restore-whole")

    def test_cli_help_is_static_and_runtime_errors_do_not_print_private_inputs(self):
        from unittest.mock import MagicMock
        output = io.StringIO()
        with patch("sys.stdout", output), patch.object(backup_cli, "_private") as private:
            with self.assertRaises(SystemExit) as stop:
                backup_cli.main(["--help"])
            self.assertEqual(stop.exception.code, 0)
            private.assert_not_called()
        output = io.StringIO()
        with patch("sys.stdout", output), patch.object(backup_cli, "_private", side_effect=OSError("private SQL credential values")):
            status = backup_cli.main(["restore", "--admin-json", "/private/admin", "--objects", "/private/objects", "--artifact", "/private/artifact", "--key", "/private/key", "--expected-cluster", "approved-cluster"])
        self.assertEqual(status, 1)
        self.assertNotIn("private", output.getvalue())
        self.assertIn("whole_backup_operation_failed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
