import contextlib
import importlib.util
import io
import subprocess
import tempfile
import unittest
import shlex
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "infra/k8s/tools/n100-k3s-operations-install.py"


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE_PATH.is_file(), "installer policy module is missing")
        spec = importlib.util.spec_from_file_location("installer", MODULE_PATH)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def result(self, code, out="", err=""):
        return subprocess.CompletedProcess([], code, out.encode(), err.encode())

    def test_query_distinguishes_policy_denial_from_errors_without_executing(self):
        command = ["/usr/local/bin/k3s", "kubectl", "version", "--client"]
        cases = [
            (self.result(0, " ".join(command) + "\n"), "ALLOWED"),
            (self.result(1, "", "Sorry, user window is not allowed to execute '/usr/local/bin/k3s kubectl version --client' as root on n100.\n"), "DENIED"),
            (self.result(1, "", "sudo: a password is required\n"), "ERROR"),
            (self.result(1, "", "sudo: unable to initialize policy plugin\n"), "ERROR"),
            (self.result(1), "ERROR"),
            (self.result(0, "unexpected super-secret"), "ERROR"),
            (self.result(0, " ".join(command), "super-secret"), "ERROR"),
            (self.result(2), "ERROR"),
        ]
        for response, expected in cases:
            with self.subTest(expected=expected, response=response), mock.patch.object(self.module.subprocess, "run", return_value=response) as run:
                self.assertEqual(self.module.query(command), expected)
                argv = run.call_args.args[0]
                self.assertEqual(argv, ["/usr/sbin/runuser", "-u", "window", "--", "/usr/bin/env", "-i", "LC_ALL=C", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "/usr/bin/sudo", "-n", "-l", "-u", "root", "--", *command])
                self.assertEqual(run.call_args.kwargs["env"], {"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})

    def listing(self, commands=None):
        commands = commands or ["diagnose", "verify_news_observability", "apply_news_observability"]
        return "Matching Defaults entries for window on n100:\n    env_reset\n\nUser window may run the following commands on n100:\n\n" + "\n".join(
            "Sudoers entry:\n    RunAsUsers: root\n    Options: !authenticate, !setenv\n    Commands:\n        /usr/local/libexec/personal-server/n100-k3s-operations " + c + "\n"
            for c in commands
        )

    def test_exact_policy_rejects_extra_wildcard_missing_and_unknown_grants(self):
        self.assertTrue(self.module.exact_policy(self.listing()))
        for text in [self.listing(["diagnose"]), self.listing(["diagnose", "verify_news_observability", "*"]), self.listing() + "\nSudoers entry:\n    RunAsUsers: ALL\n    Commands:\n        ALL\n", self.listing().replace("!setenv", "setenv"), "super-secret"]:
            self.assertFalse(self.module.exact_policy(text))

    def test_missing_identity_and_allowed_or_error_preflight_never_touch_installation(self):
        with mock.patch.object(self.module.pwd, "getpwnam", side_effect=KeyError), mock.patch.object(self.module, "replace_files") as change:
            with self.assertRaises(self.module.InstallError):
                self.module.install()
            change.assert_not_called()
        for state in ["ALLOWED", "ERROR"]:
            with mock.patch.object(self.module.pwd, "getpwnam"), mock.patch.object(self.module, "query", return_value=state), mock.patch.object(self.module, "replace_files") as change:
                with self.assertRaises(self.module.InstallError):
                    self.module.install()
                change.assert_not_called()

    def test_postcheck_failure_restores_previous_files_and_modes(self):
        for existing in [False, True]:
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                helper = root / "helper"
                sudoers = root / "sudoers"
                if existing:
                    helper.write_bytes(b"old helper")
                    sudoers.write_bytes(b"old policy")
                    helper.chmod(0o751)
                    sudoers.chmod(0o440)
                with mock.patch.object(self.module.os, "chown"), mock.patch.object(self.module, "postcheck", side_effect=self.module.InstallError):
                    with self.assertRaises(self.module.InstallError):
                        self.module.replace_files(b"new helper", b"new policy", helper, sudoers)
                if existing:
                    self.assertEqual(helper.read_bytes(), b"old helper")
                    self.assertEqual(sudoers.read_bytes(), b"old policy")
                    self.assertEqual(helper.stat().st_mode & 0o777, 0o751)
                    self.assertEqual(sudoers.stat().st_mode & 0o777, 0o440)
                else:
                    self.assertFalse(helper.exists())
                    self.assertFalse(sudoers.exists())
                self.assertEqual(sorted(p.name for p in root.iterdir()), ["helper", "sudoers"] if existing else [])

    def test_unknown_failure_never_prints_sensitive_output(self):
        stream = io.StringIO()
        with mock.patch.object(self.module.sys, "argv", ["installer"]), mock.patch.object(self.module.os, "geteuid", return_value=0), mock.patch.object(self.module, "install", side_effect=RuntimeError("super-secret")), contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            self.assertEqual(self.module.main(), 1)
        self.assertNotIn("super-secret", stream.getvalue())

    def test_postcheck_queries_each_operation_as_window_and_rejects_unknown(self):
        with mock.patch.object(self.module, "capture", return_value=self.result(0, self.listing())) as capture:
            with mock.patch.object(self.module, "query", side_effect=["DENIED", "ALLOWED", "ALLOWED", "ALLOWED"]) as query:
                self.module.postcheck()
                self.assertEqual(query.call_args_list, [mock.call(["/usr/local/bin/k3s", "kubectl", "version", "--client"]), *[mock.call(["/usr/local/libexec/personal-server/n100-k3s-operations", operation]) for operation in ["diagnose", "verify_news_observability", "apply_news_observability"]]])
                self.assertEqual(capture.call_args.args[0][-4:], ["-n", "-ll", "-u", "root"])
            for results in [["ALLOWED"], ["ERROR"], ["DENIED", "ERROR"], ["DENIED", "ALLOWED", "DENIED"]]:
                with mock.patch.object(self.module, "query", side_effect=results):
                    with self.assertRaises(self.module.InstallError):
                        self.module.postcheck()
        for response in [self.result(1), self.result(0, "super-secret"), self.result(0, self.listing(), "super-secret")]:
            with mock.patch.object(self.module, "query", side_effect=["DENIED", "ALLOWED", "ALLOWED", "ALLOWED"]), mock.patch.object(self.module, "capture", return_value=response):
                with self.assertRaises(self.module.InstallError):
                    self.module.postcheck()

    def test_failed_rollback_keeps_previous_installation_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper, sudoers = root / "helper", root / "sudoers"
            helper.write_bytes(b"old helper")
            sudoers.write_bytes(b"old policy")
            real_replace = self.module.os.replace
            def replace(source, destination):
                if Path(source).name == "previous" and destination == helper:
                    raise OSError("super-secret")
                return real_replace(source, destination)
            with mock.patch.object(self.module.os, "chown"), mock.patch.object(self.module.os, "replace", side_effect=replace), mock.patch.object(self.module, "postcheck", side_effect=self.module.InstallError):
                with self.assertRaises(self.module.InstallError):
                    self.module.replace_files(b"new helper", b"new policy", helper, sudoers)
            self.assertEqual(sudoers.read_bytes(), b"old policy")
            self.assertIn(b"old helper", [path.read_bytes() for path in root.glob(".n100-install-*/previous")])

    def test_success_installs_root_owned_exact_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper, sudoers = root / "helper", root / "sudoers"
            with mock.patch.object(self.module.os, "chown") as chown, mock.patch.object(self.module, "postcheck"):
                self.module.replace_files(b"new helper", b"new policy", helper, sudoers)
            self.assertEqual(helper.read_bytes(), b"new helper")
            self.assertEqual(sudoers.read_bytes(), b"new policy")
            self.assertEqual(helper.stat().st_mode & 0o777, 0o755)
            self.assertEqual(sudoers.stat().st_mode & 0o777, 0o440)
            self.assertEqual([call.args[1:] for call in chown.call_args_list], [(0, 0), (0, 0)])

    def test_missing_parent_directories_are_created_and_tracked(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "libexec" / "personal-server"
            created = []
            with mock.patch.object(self.module, "trusted_parent"):
                self.module.ensure_directory(target, created)
            self.assertTrue(target.is_dir())
            self.assertEqual(created, [target.parent, target])
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    def test_required_ci_job_executes_installer_regressions(self):
        import yaml
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        jobs = [entry for job in workflow["jobs"].values()
                for entry in job.get("strategy", {}).get("matrix", {}).get("include", [])
                if entry.get("name") == "n100-operations"]
        self.assertEqual(len(jobs), 1)
        modules = shlex.split(jobs[0]["test_command"])
        self.assertIn("tests.test_n100_operations_installer", modules)


if __name__ == "__main__":
    unittest.main()
