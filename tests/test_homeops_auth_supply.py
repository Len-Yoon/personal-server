"""Exercise the authentication supplier at its real process boundary."""
import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "scripts/homeops-auth-run.py"
KEY = "HOMEOPS_EXECUTOR_SHARED_SECRET"
AUTH = "synthetic-authentication-fixture-1234567890"
COMMAND = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.n100.yml",
           "up", "-d", "--no-build", "--no-deps", "--pull", "never", "--force-recreate", "homeops-executor"]


def resources():
    deployment = {"metadata": {"uid": "d", "resourceVersion": "1"}, "status": {"readyReplicas": 0},
                  "spec": {"template": {"spec": {"containers": [{"envFrom": [{"secretRef": {"name": "portal-secrets"}}],
                    "env": [{"name": KEY, "valueFrom": {"secretKeyRef": {"name": "portal-secrets", "key": KEY}}}]}]}}}}
    secret = {"metadata": {"uid": "s", "resourceVersion": "2", "name": "portal-secrets"},
              "data": {KEY: base64.b64encode(AUTH.encode()).decode()}}
    return deployment, secret


class AuthSupplyTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(PATH.is_file(), "authentication supplier is missing")
        spec = importlib.util.spec_from_file_location("auth_supply", PATH)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def invoke(self, deployment=None, secret=None, inherited="", fail=False, command=None):
        d, s = resources()
        calls = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[0] == "sudo":
                self.assertNotIn(KEY, kwargs["env"])
                result = deployment if "deployment" in argv else secret
                result = (d if "deployment" in argv else s) if result is None else result
                return subprocess.CompletedProcess(argv, 0, json.dumps(result), "")
            return subprocess.CompletedProcess(argv, 9 if fail else 0, AUTH, AUTH)
        out = io.StringIO(); err = io.StringIO()
        with patch.object(self.module.subprocess, "run", side_effect=run), patch.dict(os.environ, {KEY: inherited}), patch("sys.stdout", out), patch("sys.stderr", err):
            code = self.module.main(["--runtime-mode", "k3s", "--", *(command or COMMAND)])
        self.assertNotIn(AUTH, out.getvalue() + err.getvalue())
        self.assertTrue(all(AUTH not in " ".join(argv) for argv, _ in calls))
        return code, calls

    def test_not_ready_portal_supplies_existing_auth_only_to_compose_child(self):
        code, calls = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(calls[-1][0], COMMAND)
        self.assertEqual(calls[-1][1]["env"][KEY], AUTH)
        self.assertTrue(calls[-1][1]["capture_output"])

    def test_inconsistent_inherited_auth_blocks_compose(self):
        code, calls = self.invoke(inherited="other-valid-existing-authentication-1234")
        self.assertEqual(code, 1)
        self.assertTrue(all(a[0] == "sudo" for a, _ in calls))

    def test_missing_invalid_and_ambiguous_sources_block_compose(self):
        cases = []
        d, s = resources(); del s["data"][KEY]; cases.append((d, s))
        for value in ("!bad-base64!", base64.b64encode(b"short").decode(), base64.b64encode(b"x" * 32 + b"\n").decode()):
            d, s = resources(); s["data"][KEY] = value; cases.append((d, s))
        d, s = resources(); d["spec"]["template"]["spec"]["containers"][0]["env"] = []; cases.append((d, s))
        d, s = resources(); c = d["spec"]["template"]["spec"]["containers"][0]; c["env"].append(copy.deepcopy(c["env"][0])); cases.append((d, s))
        d, s = resources(); s["immutable"] = True; cases.append((d, s))
        for change in ("wrong_name", "optional", "literal", "multiple_sources", "namespace"):
            d, s = resources(); c = d["spec"]["template"]["spec"]["containers"][0]
            if change == "wrong_name": c["env"][0]["valueFrom"]["secretKeyRef"]["name"] = "other-secret"
            elif change == "optional": c["env"][0]["valueFrom"]["secretKeyRef"]["optional"] = True
            elif change == "literal": c["env"][0] = {"name": KEY, "value": AUTH}
            elif change == "multiple_sources": c["envFrom"].append({"secretRef": {"name": "other-secret"}})
            else: s["metadata"]["namespace"] = "other-namespace"
            cases.append((d, s))
        for d, s in cases:
            with self.subTest(source=s):
                code, calls = self.invoke(d, s)
                self.assertEqual(code, 1)
                self.assertTrue(all(a[0] == "sudo" for a, _ in calls))

    def test_command_failure_redacts_both_output_streams(self):
        code, _ = self.invoke(fail=True)
        self.assertEqual(code, 1)

    def test_source_command_failure_timeout_and_malformed_json_are_closed(self):
        failures = [subprocess.CompletedProcess([], 1, AUTH, AUTH),
                    subprocess.CompletedProcess([], 0, AUTH, ""),
                    subprocess.TimeoutExpired(["sudo"], 30, output=AUTH, stderr=AUTH), OSError(AUTH)]
        for failure in failures:
            out = io.StringIO(); err = io.StringIO()
            kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
            with self.subTest(failure=type(failure).__name__), patch.object(self.module.subprocess, "run", **kwargs) as boundary, patch("sys.stdout", out), patch("sys.stderr", err):
                self.assertEqual(self.module.main(["--runtime-mode", "k3s", "--", *COMMAND]), 1)
                self.assertTrue(all(call.args[0][0] == "sudo" for call in boundary.call_args_list))
                self.assertNotIn(AUTH, out.getvalue() + err.getvalue())

    def test_original_four_compose_files_and_project_order_are_retained(self):
        command = ["docker", "compose", "--project-directory", "/repo", "-p", "existing-project"]
        for name in ("base.yml", "n100.yml", "bridge.yml", "existing-image.yml"):
            command += ["-f", name]
        command += COMMAND[6:]
        code, calls = self.invoke(command=command, inherited=AUTH)
        self.assertEqual(code, 0)
        self.assertEqual(calls[-1][0], command)

    def test_broken_contract_installation_fails_without_traceback_or_payload(self):
        deployment, secret = resources()
        results = [subprocess.CompletedProcess([], 0, json.dumps(deployment), ""),
                   subprocess.CompletedProcess([], 0, json.dumps(secret), "")]
        for failure in (FileNotFoundError(AUTH), ImportError(AUTH), SyntaxError(AUTH)):
            out = io.StringIO(); err = io.StringIO()
            with self.subTest(error=type(failure).__name__), patch.object(self.module.subprocess, "run", side_effect=results), patch.object(self.module.importlib.util, "spec_from_file_location", side_effect=failure), patch("sys.stdout", out), patch("sys.stderr", err):
                self.assertEqual(self.module.main(["--runtime-mode", "k3s", "--", *COMMAND]), 1)
                self.assertNotIn(AUTH, out.getvalue() + err.getvalue())
                self.assertNotIn("Traceback", err.getvalue())

    def test_only_quiet_config_and_known_up_commands_are_allowed(self):
        good = ["docker", "compose", "-f", "a.yml", "config", "--quiet"]
        self.assertEqual(self.invoke(command=good)[0], 0)
        for command in (["docker", "compose", "config", "--format", "json"],
                        ["docker", "compose", "exec", "homeops-executor", "env"],
                        ["docker", "compose", "up", "-d", "unknown-service"],
                        ["docker", "compose", "up", "-d", "--build-arg", AUTH, "homeops-executor"],
                        ["docker", "compose", "--env-file", "a", "up", "-d", "homeops-executor"]):
            with self.subTest(command=command):
                code, calls = self.invoke(command=command)
                self.assertEqual(code, 1)
                self.assertEqual(calls, [])

    def test_real_subprocess_receives_auth_without_output_or_argv_leak(self):
        # Executable doubles stand in for external K3s/Docker only. The helper,
        # environment inheritance, CLI parsing and output handling remain real.
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            deployment, secret = resources()
            fake = "#!" + sys.executable + "\nimport json,os,sys,hashlib\n"
            sudo = directory / "sudo"
            sudo.write_text(fake + "assert 'HOMEOPS_EXECUTOR_SHARED_SECRET' not in os.environ\nprint(json.dumps(" + repr(deployment) + " if 'deployment' in sys.argv else " + repr(secret) + "))\n")
            docker = directory / "docker"
            docker.write_text(fake + "value=os.environ.get('HOMEOPS_EXECUTOR_SHARED_SECRET','')\nassert hashlib.sha256(value.encode()).hexdigest()==" + repr(hashlib.sha256(AUTH.encode()).hexdigest()) + "\nassert value not in ' '.join(sys.argv)\nprint(value)\nprint(value,file=sys.stderr)\n")
            sudo.chmod(0o700); docker.chmod(0o700)
            environment = dict(os.environ, PATH=folder + os.pathsep + os.environ["PATH"])
            environment.pop(KEY, None)
            result = subprocess.run([sys.executable, str(PATH), "--runtime-mode", "cutover", "--", *COMMAND], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(AUTH, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
