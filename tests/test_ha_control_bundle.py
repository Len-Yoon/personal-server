"""Controller handoff contains only exact committed code and inert templates."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ha_control_prepare", ROOT / "infra/k8s/ha_control/prepare.py")
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)
sys.path.insert(0, str(ROOT / "infra/k8s"))
from ha_control import cli
from ha_control.controller import TransitionBlocked


class ControlBundleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        for name in bundle.SOURCES:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(("# synthetic exact committed source: " + name + "\n").encode())
        for name in (".env", "data/private.db", "secrets/token.key"):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"synthetic excluded fixture")
        self.git("init", "-q")
        self.git("add", ".")
        self.git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "synthetic")
        self.commit = self.git("rev-parse", "HEAD").strip()
        self.output = Path(self.temp.name) / "bundle"
        root_patch = patch.object(bundle, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True, stderr=subprocess.DEVNULL)

    def test_exact_commit_allowlist_and_native_binding(self):
        original = (self.root / "scripts/windows-bootstrap.ps1").read_bytes()
        (self.root / "scripts/windows-bootstrap.ps1").write_bytes(b"# dirty uncommitted")
        result = bundle.prepare(self.commit, self.output)
        self.assertEqual((self.output / "files/scripts/windows-bootstrap.ps1").read_bytes(), original)
        manifest = json.loads((self.output / "source-manifest.json").read_text())
        self.assertEqual(set(manifest["source_files"]), set(bundle.SOURCES))
        self.assertEqual(result["source_bootstrap_sha256"], bundle.sha(original))
        self.assertEqual((self.output / "files/requirements.txt").read_bytes(), b"psycopg[binary]==3.3.8\n")
        self.assertTrue(bundle.verify(self.output, result["manifest_sha256"])["verified"])
        paths = "\n".join(bundle.inventory(self.output))
        self.assertNotIn(".env", paths)
        self.assertNotIn("private.db", paths)
        self.assertNotIn("token.key", paths)

    def test_templates_never_issue_approval_or_supply_real_resource_identity(self):
        bundle.prepare(self.commit, self.output)
        ready = json.loads((self.output / "readiness.json").read_text())
        self.assertFalse(ready["ready"])
        self.assertFalse(ready["approval_issued"])
        self.assertIn("native_windows_acceptance", ready["requires"])
        plan = json.loads((self.output / "native-plan.template.json").read_text())
        self.assertIsNone(plan["cluster"])
        self.assertIsNone(plan["resource"]["container_ids"])
        self.assertIsNone(plan["pg_nodes"]["A"]["host"])
        for action in ("fence", "ha-transition"):
            approval = json.loads((self.output / (action + "-approval.template.json")).read_text())
            self.assertEqual(approval["action"], action)
            self.assertIsNone(approval["plan_sha256"])
            self.assertIsNone(approval["expires_at"])

    def test_source_tamper_and_extra_file_rejected(self):
        bundle.prepare(self.commit, self.output)
        target = self.output / "files/ha_control/controller.py"
        target.chmod(0o600)
        target.write_bytes(b"# modified")
        with self.assertRaisesRegex(bundle.BundleError, "bundle_files_modified"):
            bundle.verify(self.output)
        target.write_bytes((self.root / "infra/k8s/ha_control/controller.py").read_bytes())
        (self.output / ".env").write_text("synthetic")
        with self.assertRaisesRegex(bundle.BundleError, "bundle_files_modified"):
            bundle.verify(self.output)

    def test_trusted_pin_detects_rewritten_manifest(self):
        result = bundle.prepare(self.commit, self.output)
        manifest = self.output / "source-manifest.json"
        manifest.chmod(0o600)
        manifest.write_bytes(manifest.read_bytes() + b"\n")
        with self.assertRaisesRegex(bundle.BundleError, "trusted_manifest_pin"):
            bundle.verify(self.output, result["manifest_sha256"])

    def test_existing_output_short_commit_and_source_symlink_refused(self):
        with self.assertRaisesRegex(bundle.BundleError, "full_source_commit"):
            bundle.prepare("HEAD", self.output)
        self.output.mkdir()
        with self.assertRaisesRegex(bundle.BundleError, "new_output"):
            bundle.prepare(self.commit, self.output)
        source = self.root / "infra/k8s/ha_control/controller.py"
        source.unlink()
        source.symlink_to("pg.py")
        self.git("add", ".")
        self.git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "symlink")
        with self.assertRaisesRegex(bundle.BundleError, "regular_committed"):
            bundle.snapshot(self.git("rev-parse", "HEAD").strip())

    def native_plan(self):
        result = bundle.prepare(self.commit, self.output)
        plan = json.loads((self.output / "native-plan.template.json").read_text())
        plan["controller_bundle_sha256"] = result["manifest_sha256"]
        return plan

    def test_cli_requires_matching_pin_commit_code_hashes_and_current_execution_path(self):
        plan = self.native_plan()
        current = str((self.output / "files/ha_control/cli.py").resolve())
        with patch.object(cli, "__file__", current):
            cli.validate_bundle(plan)
            for key, value in (("controller_bundle_sha256", "0" * 64), ("source_commit", "0" * 40),
                               ("controller_code_sha256", "0" * 64), ("native_windows_code_sha256", "0" * 64),
                               ("legacy_linux_sha256", "0" * 64), ("legacy_guard_code_sha256", "0" * 64),
                               ("windows_guard_sha256", "0" * 64)):
                with self.subTest(binding=key), self.assertRaises(TransitionBlocked):
                    cli.validate_bundle(dict(plan, **{key: value}))
        with self.assertRaisesRegex(TransitionBlocked, "approved_controller_bundle_required"):
            cli.validate_bundle(plan)
        other = Path(self.temp.name) / "other/files/ha_control/cli.py"
        other.parent.mkdir(parents=True)
        other.write_text("# unapproved execution location")
        with patch.object(cli, "__file__", str(other.resolve())), self.assertRaises(TransitionBlocked):
            cli.validate_bundle(plan)

    def test_cli_refuses_writable_manifest_or_modified_executed_source(self):
        plan = self.native_plan()
        current = (self.output / "files/ha_control/cli.py").resolve()
        manifest = self.output / "source-manifest.json"
        with patch.object(cli, "__file__", str(current)):
            manifest.chmod(0o644)
            with self.assertRaisesRegex(TransitionBlocked, "readonly_controller_manifest_required"):
                cli.validate_bundle(plan)
            manifest.chmod(0o444)
            current.chmod(0o644)
            current.write_text("# changed current executing source")
            with self.assertRaisesRegex(TransitionBlocked, "approved_controller_bundle_required"):
                cli.validate_bundle(plan)


    def test_effects_transitive_import_closure_and_exact_template_binding(self):
        bundle.prepare(self.commit,self.output)
        manifest=json.loads((self.output/'source-manifest.json').read_text())
        plan=json.loads((self.output/'enforced-plan.template.json').read_text())
        expected=bundle.effects_runtime_sha256(manifest['source_files'])
        self.assertEqual(manifest['effects_runtime_sha256'],expected)
        self.assertEqual(plan['pg_enforcement']['effects'],{'code_sha256':expected,'broker_role':None})
        self.assertTrue(set(bundle.EFFECTS_SOURCES).issubset(bundle.SOURCES))
        actual=Path(self.temp.name)/'real-files'
        for source in bundle.SOURCES:
            target=actual/bundle.TARGETS[source]
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes((ROOT/source).read_bytes())
        code="import sys;sys.path.insert(0,sys.argv[1]);from ha_control.pg import PgBackend;assert 'ha_runtime' not in sys.modules;from ha_runtime.effects import validate_catalog;from ha_runtime.enforcement import install_namespace"
        subprocess.run([sys.executable,'-I','-c',code,str(actual)],check=True,capture_output=True)

    def test_cli_rejects_effects_binding_mismatch_or_missing_runtime_closure(self):
        result=bundle.prepare(self.commit,self.output)
        plan=json.loads((self.output/'enforced-plan.template.json').read_text())
        plan['controller_bundle_sha256']=result['manifest_sha256']
        current=str((self.output/'files/ha_control/cli.py').resolve())
        with patch.object(cli,'__file__',current):
            cli.validate_bundle(plan)
            plan['pg_enforcement']['effects']['code_sha256']='0'*64
            with self.assertRaisesRegex(TransitionBlocked,'approved_enforcement_source_mismatch'):
                cli.validate_bundle(plan)
            plan['pg_enforcement'].pop('effects')
            with self.assertRaises(TransitionBlocked):cli.validate_bundle(plan)


if __name__ == "__main__":
    unittest.main()
