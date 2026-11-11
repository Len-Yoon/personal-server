"""Exact source packaging, immutable bases and offline archive integrity checks."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ha_package", ROOT / "infra/k8s/ha-package/build.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class PackageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        for service in package.SERVICES:
            for name, data in {
                "Dockerfile": (ROOT / service / "Dockerfile").read_bytes(),
                "requirements.txt": b"# synthetic, no external dependencies\n",
                "app/main.py": b"value = 'committed'\n",
                "app/static/icon.svg": b"<svg/>\n",
                "app/static/token.key": b"SYNTHETIC_PRIVATE_KEY_EXCLUDED\n",
                "app/.env": b"SYNTHETIC_PASSWORD_EXCLUDED\n",
                "data/example.db": b"SYNTHETIC_DATA_EXCLUDED\n",
            }.items():
                target = self.root / service / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        for name in (*package.CAR_PROVIDER_FILES, *package.PORTAL_MIGRATION_FILES, *package.CONTROL_RUNTIME, *package.EGRESS_RUNTIME, *package.EGRESS_BROKER, "infra/k8s/ha_runtime/__init__.py", "infra/k8s/ha_runtime/schema.sql",
                     "infra/k8s/ha_runtime/backup.py", "infra/k8s/ha_runtime/backup_cli.py",
                     "infra/k8s/ha_runtime/seven_migration.py",
                     "infra/k8s/tools/n100_ha_contract.py", "infra/k8s/ha-package/Dockerfile.template",
                     "infra/k8s/ha-package/Utility.Dockerfile.template"):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes() if (ROOT / name).exists() else b"# synthetic backup CLI fixture\n")
        self.git("init", "-q")
        self.git("add", ".")
        self.git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
        self.commit = self.git("rev-parse", "HEAD").strip()
        self.bundle = Path(self.temp.name) / "bundle"
        self.root_patch = patch.object(package, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True, stderr=subprocess.DEVNULL)

    def test_all_seven_preserve_native_base_command_user_and_env(self):
        manifest = package.prepare(self.commit, self.bundle)
        self.assertEqual(set(manifest["services"]), set(package.SERVICES))
        for service in package.SERVICES:
            original = (self.root / service / "Dockerfile").read_text()
            rendered = (self.bundle / "contexts" / service / "Dockerfile").read_text()
            self.assertTrue(rendered.startswith(original.split("USER ")[0].rstrip()))
            self.assertTrue(rendered.endswith(original[original.index("USER "):].rstrip() + "\n"))
            self.assertNotIn("ARG ", rendered)
            self.assertIn("psycopg[binary]==3.3.8", rendered)
            self.assertEqual(rendered.count(package.APK_SECURITY_UPDATE), 1)
            self.assertLess(rendered.index(package.APK_SECURITY_UPDATE), rendered.index("USER 10001:10001"))
            self.assertEqual((self.bundle / "contexts" / service / "requirements.txt").read_bytes(),
                             (self.root / service / "requirements.txt").read_bytes())
            self.assertTrue((self.bundle / "contexts" / service / "_ha/tools/n100_ha_contract.py").is_file())
            self.assertEqual(package.verify_source(self.bundle), manifest)
        plan = json.loads((self.bundle / "deployment-plan.json").read_text())
        self.assertFalse(plan["production_deployed"])
        self.assertIn("approved_controller", plan["activation"])

    def test_packaged_canonical_runtime_imports_without_controller_sources(self):
        # Commit the real runtime, then import from each isolated generated context.
        for source in (ROOT / "infra/k8s/ha_runtime").iterdir():
            if source.suffix in {".py", ".sql"}:
                (self.root / "infra/k8s/ha_runtime" / source.name).write_bytes(source.read_bytes())
        self.git("add", ".")
        self.git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "real runtime")
        commit = self.git("rev-parse", "HEAD").strip()
        manifest = package.prepare(commit, self.bundle)
        self.assertEqual(package.verify_source(self.bundle), manifest)
        import sys
        for service in (*package.SERVICES, package.UTILITY):
            context = self.bundle / "contexts" / service / "_ha"
            self.assertEqual({p.name for p in (context / "ha_egress").iterdir()},
                             {Path(name).name for name in package.EGRESS_RUNTIME | package.EGRESS_BROKER} if service == package.UTILITY
                             else {Path(name).name for name in package.EGRESS_RUNTIME})
            self.assertEqual({p.name for p in (context / "ha_control").iterdir()},
                             {"__init__.py", "privilege_closure.py"})
            subprocess.run([sys.executable, "-I", "-c",
                "import sys;sys.path.insert(0,sys.argv[1]);"
                "import ha_runtime.enforcement;import ha_control.privilege_closure;import ha_egress.client;import ha_egress.http_read;import ha_egress.app_entrypoint",
                str(context)], check=True, capture_output=True)
            if service == package.UTILITY:
                subprocess.run([sys.executable,'-I','-c',
                    'import sys;sys.path.insert(0,sys.argv[1]);'
                    'from ha_runtime.seven_migration import _portal_validator;'
                    '_portal_validator()._validate({"format":1,"version":0,"entries":{},"trash":{},"confirmations":{},"audit":[]})',
                    str(context)],check=True,capture_output=True)
                subprocess.run([sys.executable, '-I', '-c',
                    'import sys;sys.path.insert(0,sys.argv[1]);'
                    'from ha_egress.hyundai_provider import canonical_client_type;'
                    'assert canonical_client_type().__name__ == "HyundaiClient"', str(context)],
                    check=True, capture_output=True)

    def test_commit_objects_ignore_dirty_worktree_and_credentials(self):
        (self.root / "portal-web/app/main.py").write_text("value = 'dirty'\n")
        manifest = package.prepare(self.commit, self.bundle)
        context = self.bundle / "contexts/portal-web"
        self.assertEqual((context / "app/main.py").read_text(), "value = 'committed'\n")
        paths = "\n".join(manifest["bundle_files"])
        self.assertNotIn("token.key", paths)
        self.assertNotIn(".env", paths)
        self.assertNotIn("example.db", paths)
        self.assertNotIn(".git", paths)
        self.assertTrue((context / "app/static/icon.svg").exists())

    def test_separate_utility_preserves_seven_app_contracts_and_contains_no_config(self):
        manifest = package.prepare(self.commit, self.bundle)
        self.assertEqual(len(manifest["services"]), 7)
        self.assertEqual(set(manifest["utilities"]), {package.UTILITY})
        context = self.bundle / "contexts" / package.UTILITY
        dockerfile = (context / "Dockerfile").read_text()
        self.assertIn("postgresql17-client", dockerfile)
        self.assertIn("cryptography==50.0.0", dockerfile)
        self.assertIn("setuptools==84.0.0 wheel==0.48.0", dockerfile)
        self.assertIn("psycopg[binary]==3.3.8", dockerfile)
        self.assertEqual(dockerfile.count(package.APK_SECURITY_UPDATE), 1)
        self.assertLess(dockerfile.index(package.APK_SECURITY_UPDATE), dockerfile.index("USER 10001:10001"))
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn('["python", "-m", "ha_runtime.backup_cli", "--help"]', dockerfile)
        self.assertEqual(set(path.name for path in context.iterdir()), {"Dockerfile", "_ha"})
        release = json.loads((context / "_ha/ha_runtime/release.json").read_text())
        self.assertEqual(release["source_commit"], self.commit)
        self.assertEqual(release["release_id"], manifest["release_id"])
        self.assertEqual(release, json.loads((self.bundle / "contexts/portal-web/_ha/ha_runtime/release.json").read_text()))
        for service in package.SERVICES:
            original = (self.bundle / "contexts" / service / "Dockerfile").read_text()
            self.assertNotIn("cryptography==50.0.0", original)
            self.assertNotIn("postgresql17-client", original)
        self.assertEqual(package.verify_source(self.bundle), manifest)

    def test_utility_template_drift_and_utility_context_tampering_rejected(self):
        files = package.snapshot(self.commit)
        files["infra/k8s/ha-package/Utility.Dockerfile.template"] += b"RUN echo unapproved\n"
        with self.assertRaisesRegex(package.PackageError, "invalid_utility_template"):
            package.utility_recipe(files)
        package.prepare(self.commit, self.bundle)
        target = self.bundle / "contexts" / package.UTILITY / "_ha/ha_runtime/backup_cli.py"
        target.chmod(0o600)
        target.write_bytes(b"# tampered utility code")
        with self.assertRaisesRegex(package.PackageError, "bundle_files_modified"):
            package.verify_source(self.bundle)

    def test_source_context_tampering_rejected_even_with_rehashed_manifest(self):
        manifest = package.prepare(self.commit, self.bundle)
        target = self.bundle / "contexts/portal-web/app/main.py"
        target.chmod(0o600)
        target.write_bytes(b"value = 'tampered'\n")
        with self.assertRaisesRegex(package.PackageError, "bundle_files_modified"):
            package.verify_source(self.bundle)
        manifest["bundle_files"]["portal-web/app/main.py"] = package.sha(target.read_bytes())
        manifest["release_id"] = package.sha(package.encode({k: v for k, v in manifest.items() if k not in {"release_id", "bundle_files"}}))
        source_manifest = self.bundle / "source-manifest.json"
        source_manifest.chmod(0o600)
        source_manifest.write_bytes(package.encode(manifest))
        with self.assertRaisesRegex(package.PackageError, "rendered_source_binding"):
            package.verify_source(self.bundle)

    def test_unknown_mutable_base_root_user_template_override_rejected(self):
        original = (self.root / "portal-web/Dockerfile").read_text()
        template = (self.root / "infra/k8s/ha-package/Dockerfile.template").read_text()
        for bad in (original.replace(original.splitlines()[0], "FROM python:3.11"),
                    original.replace("USER 10001:10001", "USER root"),
                    "ARG BASE\n" + original, original + "\nFROM alpine\n"):
            with self.assertRaises(package.PackageError):
                package.render(bad, template)
        for bad in (template + "RUN curl example.invalid\n", template.replace("3.3.8", "3.3.7")):
            with self.assertRaises(package.PackageError):
                package.render(original, bad)

    def test_full_commit_and_fresh_output_required(self):
        with self.assertRaises(package.PackageError):
            package.prepare("HEAD", self.bundle)
        package.prepare(self.commit, self.bundle)
        with self.assertRaises(package.PackageError):
            package.prepare(self.commit, self.bundle)

    def test_security_template_missing_lowered_or_extended_instructions_rejected(self):
        files = package.snapshot(self.commit)
        app_template = files["infra/k8s/ha-package/Dockerfile.template"].decode()
        utility_template = files["infra/k8s/ha-package/Utility.Dockerfile.template"].decode()
        original = files["portal-web/Dockerfile"].decode()
        for template, utility in [(app_template, False), (utility_template, True)]:
            mutations = [template.replace(package.APK_SECURITY_UPDATE, ""),
                         template.replace("--no-cache --upgrade", "--no-cache --allow-untrusted --upgrade"),
                         template.replace("'zlib>=1.3.2-r1'", "'zlib>=1.3.2-r1' && echo unapproved")]
            for name, minimum in package.APK_MINIMUMS.items():
                mutations += [template.replace("'" + name + ">=" + minimum + "'", ""),
                              template.replace("'" + name + ">=" + minimum + "'",
                                               "'" + name + ">=0-r0'")]
            for mutated in mutations:
                with self.subTest(utility=utility, template=mutated):
                    with self.assertRaisesRegex(package.PackageError, "invalid_.*template"):
                        if utility:
                            package.utility_recipe(files | {"infra/k8s/ha-package/Utility.Dockerfile.template": mutated.encode()})
                        else:
                            package.render(original, mutated)

    def test_apk_security_smoke_uses_installed_database_and_native_comparison(self):
        expected = {"musl": "1.2.6-r3", "musl-utils": "1.2.6-r3", "zlib": "1.3.2-r1",
                    "krb5-libs": "1.22.3-r0"}
        self.assertEqual(package.APK_MINIMUMS, expected)
        def check(versions, comparisons):
            database = "\n\n".join("P:" + name + "\nV:" + version for name, version in versions.items())
            with patch.object(Path, "read_text", return_value=database) as read, patch.object(
                    subprocess, "check_output", side_effect=comparisons) as command:
                exec(package.apk_security_smoke(), {})
                read.assert_called_once_with()
                self.assertEqual([call.args[0] for call in command.call_args_list], [
                    ["/sbin/apk", "version", "-t", versions[name], minimum] for name, minimum in expected.items()])
        check(expected, ["=\n"] * len(expected))
        check({"musl": "1.2.6-r10", "musl-utils": "1.2.6-r10", "zlib": "1.3.3-r0",
               "krb5-libs": "1.22.3-r1"}, [">\n"] * len(expected))
        for versions, comparisons in [(expected, ["<\n"]), (expected, ["unknown\n"]),
                                      ({"musl": "1.2.6-r3"}, ["=\n"])]:
            with self.subTest(versions=versions, comparisons=comparisons):
                database = "\n\n".join("P:" + name + "\nV:" + version for name, version in versions.items())
                with patch.object(Path, "read_text", return_value=database), patch.object(
                        subprocess, "check_output", side_effect=comparisons):
                    with self.assertRaises(AssertionError):
                        exec(package.apk_security_smoke(), {})

    def test_security_instruction_tampering_rejected_even_when_manifest_rehashed(self):
        manifest = package.prepare(self.commit, self.bundle)
        target = self.bundle / "contexts/portal-web/Dockerfile"
        target.chmod(0o600)
        target.write_text(target.read_text().replace("musl>=1.2.6-r3", "musl>=1.2.6-r2"))
        manifest["bundle_files"]["portal-web/Dockerfile"] = package.sha(target.read_bytes())
        source_manifest = self.bundle / "source-manifest.json"
        source_manifest.chmod(0o600)
        source_manifest.write_bytes(package.encode(manifest))
        with self.assertRaisesRegex(package.PackageError, "rendered_source_binding"):
            package.verify_source(self.bundle)

    def test_source_symlink_rejected(self):
        (self.root / "portal-web/app/link.py").symlink_to("main.py")
        self.git("add", ".")
        self.git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "symlink")
        with self.assertRaisesRegex(package.PackageError, "non_regular_source"):
            package.snapshot(self.git("rev-parse", "HEAD").strip())

    def test_archive_config_and_layer_digest_binding(self):
        archive_path = Path(self.temp.name) / "image.tar"
        layer = b"synthetic layer bytes"
        config = package.encode({"rootfs": {"diff_ids": ["sha256:" + package.sha(layer)]}})
        identifier = "sha256:" + package.sha(config)
        def write_archive(payload):
            with tarfile.open(archive_path, "w") as archive:
                for name, data in {"manifest.json": package.encode([{"Config": "config.json", "Layers": ["layer.tar"]}]),
                                   "config.json": config, "layer.tar": payload}.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
        write_archive(layer)
        binding = package.archive_binding(archive_path, identifier)
        self.assertEqual(binding["image_config_digest"], identifier)
        with self.assertRaisesRegex(package.PackageError, "image_config_binding"):
            package.archive_binding(archive_path, "sha256:" + "0" * 64)
        write_archive(b"tampered layer bytes")
        with self.assertRaisesRegex(package.PackageError, "image_layer_binding"):
            package.archive_binding(archive_path, identifier)

    def test_docker_socket_rejects_remote_and_strips_environment(self):
        bad_context = json.dumps([{"Endpoints": {"docker": {"Host": "tcp://remote.invalid:2375"}}}])
        with patch.object(package.platform, "system", return_value="Darwin"), patch.object(
                package.LocalDocker, "call", return_value=bad_context):
            with self.assertRaisesRegex(package.PackageError, "isolated_local_socket"):
                package.LocalDocker()
        with patch.object(package.platform, "system", return_value="Linux"):
            with self.assertRaisesRegex(package.PackageError, "mac_orbstack_only"):
                package.LocalDocker()

    def test_archive_builder_rejects_manifest_id_image_store_before_build(self):
        endpoint='unix://'+str(Path.home()/'.orbstack/run/docker.sock')
        context=json.dumps([{'Name':'orbstack','Endpoints':{'docker':{'Host':endpoint}}}])
        for driver in ('overlayfs','overlay2'):
            with patch.object(package.platform,'system',return_value='Darwin'), \
                 patch.object(package.LocalDocker,'call',side_effect=[context,'daemon-fixture',driver]) as call:
                if driver=='overlayfs':
                    with self.assertRaisesRegex(package.PackageError,'classic_image_store_required'):
                        package.LocalDocker()
                else:
                    self.assertEqual(package.LocalDocker().daemon_id,'daemon-fixture')
                self.assertEqual(call.call_count,3)


if __name__ == "__main__":
    unittest.main()
