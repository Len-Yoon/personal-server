"""Production HA approval is private, bounded and pinned to packaged source."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import stat

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ha_activation_config", ROOT / "infra/k8s/ha_runtime/config.py")
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


class ActivationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # macOS /tmp is a symlink: the runtime intentionally requires a canonical path.
        self.directory = Path(self.temp.name).resolve()
        self.ca = self.directory / "ca.crt"
        self.ca.write_text("synthetic CA, no connection is made")
        self.ca.chmod(0o444)
        self.passfile = self.directory / "synthetic.pgpass"
        self.passfile.write_text("synthetic credential fixture")
        self.passfile.chmod(0o600)
        self.release = {"source_commit": "a" * 40, "release_id": "b" * 64, "source_files_sha256": "c" * 64}
        def endpoint(host):
            return {"host": host, "port": 5432, "dbname": "synthetic", "user": "book_memo_a",
                    "sslmode": "verify-full", "sslrootcert": str(self.ca), "passfile": str(self.passfile)}
        self.payload = {"scope": "n100-approved", "cluster": "synthetic_cluster", "system_id": "1234567890",
                        "node": "A", "service": "book-memo", "primary_node": "A",
                        "primary": endpoint("approved-a.invalid"), "partner": endpoint("approved-b.invalid"), **self.release}
        self.approval = {k: deepcopy(v) for k, v in self.payload.items() if k != "scope"}
        self.approval.update({"action": "activate-ha", "expires": self.expiry(1),
                              "nodes": {"A": "approved-a.invalid", "B": "approved-b.invalid"}})
        self.config_path = self.directory / "config.json"
        self.approval_path = self.directory / "activation.json"
        self.write()
        self.environment = {"APP_ENV": "production", "HA_PG_CONFIG": str(self.config_path),
                            "HA_PG_ACTIVATION": str(self.approval_path)}

    def expiry(self, hours):
        return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")

    def write(self):
        for path, value in ((self.config_path, self.payload), (self.approval_path, self.approval)):
            path.write_text(json.dumps(value))
            path.chmod(0o600)

    def load(self, mock_release=True):
        # Approval authority ownership is independently exercised with mocked
        # root-owned stat metadata; never chown files on the developer's host.
        with patch.dict(os.environ, self.environment, clear=True), patch.object(
                config, "_authority_file", side_effect=lambda path: Path(path)):
            if mock_release:
                with patch.object(config, "_release", return_value=self.release):
                    return config.load("book-memo")
            return config.load("book-memo")

    def test_exact_approved_release_and_failover_mapping(self):
        self.assertEqual(self.load(), self.payload)
        for value in (self.payload, self.approval):
            value["primary_node"] = "B"
            value["primary"], value["partner"] = value["partner"], value["primary"]
        self.write()
        self.assertEqual(self.load()["primary"]["host"], "approved-b.invalid")

    def test_environment_alone_and_source_tree_cannot_arm(self):
        self.approval_path.unlink()
        with self.assertRaises(config.DatabaseUnavailable):
            self.load()
        self.write()
        with self.assertRaises(config.DatabaseUnavailable):
            self.load(mock_release=False)
        self.environment.pop("HA_PG_ACTIVATION")
        with self.assertRaisesRegex(config.DatabaseUnavailable, "production_ha_not_armed"):
            self.load()

    def test_other_source_commit_release_or_manifest_rejected(self):
        for key, changed in (("source_commit", "d" * 40), ("release_id", "d" * 64), ("source_files_sha256", "d" * 64)):
            with self.subTest(key=key):
                original = self.payload[key]
                self.payload[key] = self.approval[key] = changed
                self.write()
                with self.assertRaisesRegex(config.DatabaseUnavailable, "activation_identity"):
                    self.load()
                self.payload[key] = self.approval[key] = original

    def test_expired_non_utc_and_unbounded_approval_rejected(self):
        for expiry in (self.expiry(-1), self.expiry(25), "2026-01-01T00:00:00", "invalidZ"):
            self.approval["expires"] = expiry
            self.write()
            with self.assertRaises(config.DatabaseUnavailable):
                self.load()

    def test_endpoint_or_db_identity_mismatch_rejected(self):
        for key, changed in (("host", "unapproved.invalid"), ("dbname", "other"), ("user", "other"),
                             ("sslrootcert", str(self.directory / "other.crt")), ("passfile", str(self.directory / "other.pgpass"))):
            with self.subTest(key=key):
                original = self.payload["primary"][key]
                self.payload["primary"][key] = changed
                self.write()
                with self.assertRaisesRegex(config.DatabaseUnavailable, "activation_identity"):
                    self.load()
                self.payload["primary"][key] = original
        self.payload["primary"]["host"] = self.approval["primary"]["host"] = "unapproved.invalid"
        self.write()
        with self.assertRaisesRegex(config.DatabaseUnavailable, "authenticated_endpoint"):
            self.load()

    def test_private_config_activation_and_passfile_required(self):
        for path in (self.config_path, self.passfile):
            with self.subTest(path=path.name):
                path.chmod(0o644)
                with self.assertRaisesRegex(config.DatabaseUnavailable, "private_configuration"):
                    self.load()
                path.chmod(0o600)

    def test_app_owned_activation_is_rejected_without_authority_mock(self):
        with patch.dict(os.environ, self.environment, clear=True), patch.object(
                config, "_release", return_value=self.release):
            with self.assertRaisesRegex(config.DatabaseUnavailable, "trusted_activation_authority"):
                config.load("book-memo")

    def authority_fixture(self, mode=0o444, uid=0, nlink=1, parent_mode=0o755, parent_uid=0):
        candidate = MagicMock()
        candidate.is_absolute.return_value = True
        candidate.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=uid, st_nlink=nlink)
        parent = MagicMock()
        parent.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFDIR | parent_mode, st_uid=parent_uid)
        candidate.parents = [parent]
        return candidate

    def test_root_owned_readonly_approval_with_trusted_parents_accepted(self):
        for mode in (0o440, 0o444):
            candidate = self.authority_fixture(mode=mode)
            with patch.object(config, "Path", return_value=candidate), patch.object(config.os, "geteuid", return_value=10001):
                self.assertIs(config._authority_file("/run/secrets/activation.json"), candidate)

    def test_authority_write_owner_hardlink_and_parent_boundaries(self):
        for kwargs in ({"uid": 10001}, {"mode": 0o644}, {"mode": 0o464}, {"nlink": 2},
                       {"parent_uid": 10001}, {"parent_mode": 0o775}, {"parent_mode": 0o777}):
            with self.subTest(metadata=kwargs):
                candidate = self.authority_fixture(**kwargs)
                with patch.object(config, "Path", return_value=candidate), patch.object(config.os, "geteuid", return_value=10001):
                    with self.assertRaisesRegex(config.DatabaseUnavailable, "trusted_activation_authority"):
                        config._authority_file("/run/secrets/activation.json")
        candidate = self.authority_fixture()
        candidate.lstat.return_value.st_mode = stat.S_IFLNK | 0o444
        with patch.object(config, "Path", return_value=candidate), patch.object(config.os, "geteuid", return_value=10001):
            with self.assertRaises(config.DatabaseUnavailable):
                config._authority_file("/run/secrets/activation.json")
        candidate = self.authority_fixture()
        candidate.parents[0].lstat.return_value.st_mode = stat.S_IFLNK | 0o755
        with patch.object(config, "Path", return_value=candidate), patch.object(config.os, "geteuid", return_value=10001):
            with self.assertRaises(config.DatabaseUnavailable):
                config._authority_file("/run/secrets/activation.json")
        candidate = self.authority_fixture()
        with patch.object(config, "Path", return_value=candidate), patch.object(config.os, "geteuid", return_value=0):
            with self.assertRaises(config.DatabaseUnavailable):
                config._authority_file("/run/secrets/activation.json")

    def test_unknown_action_plaintext_password_or_non_tls_rejected(self):
        self.approval["action"] = "deploy"
        self.write()
        with self.assertRaises(config.DatabaseUnavailable):
            self.load()
        self.approval["action"] = "activate-ha"
        for key, value in (("sslmode", "require"), ("password", "synthetic-never-output")):
            self.payload["primary"][key] = self.approval["primary"][key] = value
            self.write()
            with self.assertRaises(config.DatabaseUnavailable) as caught:
                self.load()
            self.assertNotIn("synthetic-never-output", str(caught.exception))
            if key == "sslmode":
                self.payload["primary"][key] = self.approval["primary"][key] = "verify-full"

    def test_local_isolated_contract_is_preserved(self):
        self.payload = {k: v for k, v in self.payload.items() if k not in self.release}
        self.payload["scope"] = "local-isolated"
        self.payload["primary"]["host"] = "ha-a"
        self.payload["partner"]["host"] = "ha-b"
        self.environment = {"APP_ENV": "test", "HA_LOCAL_RUNTIME": "1", "HA_PG_CONFIG": str(self.config_path)}
        self.write()
        self.assertEqual(self.load()["scope"], "local-isolated")


if __name__ == "__main__":
    unittest.main()
