import importlib.util
import json
import subprocess
import unittest
import base64
import copy
import io
import sys
import types
from contextlib import redirect_stdout
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch


PATH = Path(__file__).resolve().parents[1] / "infra/k8s/tools/homeops-auth-configure.py"


def load_module():
    if not PATH.is_file():
        raise AssertionError("HomeOps 인증 구성 실행 도구가 필요함")
    spec = importlib.util.spec_from_file_location("homeops_auth_runtime", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HomeOpsAuthRuntimeTests(unittest.TestCase):
    def test_live_diagnostic_probe_uses_existing_no_healthcheck_contract(self):
        module = load_module()
        scope = frozenset({"system-agent", "caddy", "homeops-executor"})
        class Diagnostics(list):
            managed_services = scope
        values = Diagnostics({"service": name, "container": {"status": "running", "health": "none" if name == "caddy" else "healthy"}} for name in scope)
        class Client:
            def all_diagnostics(self):
                return values
        class Service:
            def _all_services_healthy(self, diagnostics):
                return all(x["container"]["status"] == "running" and x["container"]["health"] in ("none", "healthy") for x in diagnostics)
        fake = types.ModuleType("app.services.homeops")
        fake.ExecutorClient, fake.HomeOpsService = Client, Service
        def execute(args, **kwargs):
            if args[0] == "docker":
                return json.dumps({"executor_auth_match": True, "missing_auth_status": 403, "wrong_auth_status": 403})
            output = io.StringIO()
            with patch.dict(sys.modules, {"app.services.homeops": fake}), patch.object(sys, "stdin", io.StringIO(kwargs["input_text"])), patch.object(module.os, "getenv", return_value="a" * 64), redirect_stdout(output):
                exec(args[-1], {})
            return output.getvalue()
        configuration = module.Configuration(str(PATH.parents[3]))
        with patch.object(module, "run_command", side_effect=execute):
            self.assertEqual(configuration.verify("a" * 64, {"metadata": {"name": "portal"}})["summary_health"], "PASS")
            values[0]["container"]["health"] = "unhealthy"
            with self.assertRaises(AssertionError):
                configuration.verify("a" * 64, {"metadata": {"name": "portal"}})

    def test_command_failure_never_reveals_sensitive_stderr_or_input(self):
        module = load_module()
        secret = "test-only-sensitive-input"
        with patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", secret)):
            with self.assertRaises(module.OperationError) as caught:
                module.run_command(["kubectl", "patch"], input_text=json.dumps({"value": secret}))
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(str(caught.exception), "command_failed:kubectl")

    def test_secret_payload_is_stdin_not_command_argument_or_parent_environment(self):
        module = load_module()
        secret = "test-only-sensitive-input"
        payload = json.dumps({"value": secret})
        with patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "ok", "")) as execute:
            self.assertEqual(module.run_command(["kubectl", "patch", "--patch-file=/dev/stdin"], input_text=payload), "ok")
        args, kwargs = execute.call_args
        self.assertNotIn(secret, repr(args))
        self.assertEqual(kwargs["input"], payload)
        self.assertTrue(kwargs["capture_output"])

    def test_timeout_is_sanitized(self):
        module = load_module()
        with patch.object(module.subprocess, "run", side_effect=subprocess.TimeoutExpired(["kubectl", "private-value"], 1)):
            with self.assertRaises(module.OperationError) as caught:
                module.run_command(["kubectl", "get"], timeout=1)
        self.assertEqual(str(caught.exception), "command_timeout:kubectl")

    def test_portal_guard_rejects_non_single_writer_or_wrong_runtime_user(self):
        module = load_module()
        deployment = {"spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "template": {"spec": {"containers": [{"name": "portal-web", "image": "pinned-image"}]}}}, "status": {"readyReplicas": 1}}
        pod = {"status": {"phase": "Running", "containerStatuses": [{"ready": True, "imageID": "pinned-image"}]}}
        module.assert_portal_ready(deployment, [pod], "10001")
        with self.assertRaises(module.OperationError):
            module.assert_portal_ready(deployment, [pod, pod], "10001")
        with self.assertRaises(module.OperationError):
            module.assert_portal_ready(deployment, [pod], "0")
        deployment["spec"]["strategy"]["type"] = "RollingUpdate"
        with self.assertRaises(module.OperationError):
            module.assert_portal_ready(deployment, [pod], "10001")

    def test_uncertain_portal_patch_recovers_observed_applied_state(self):
        module = load_module()
        self.assertTrue(hasattr(module, "rollback_portal_patch"), "원격 응답 유실 뒤 실제 상태 기반 원복이 필요함")
        original = {"metadata": {"uid": "original", "resourceVersion": "1"}, "spec": {"template": {"spec": {"containers": [{"env": [{"name": "OTHER", "value": "preserved"}]}]}}}}
        expected = {"template": {"spec": {"containers": [{"env": [{"name": "OTHER", "value": "preserved"}, {"name": module.KEY, "valueFrom": {"secretKeyRef": {"name": "source", "key": module.KEY}}}]}]}}}
        current = {"metadata": {"uid": "original", "resourceVersion": "3"}, "spec": expected}
        patch = module.rollback_portal_patch(original, current, expected)
        self.assertEqual(patch[-1], {"op": "replace", "path": "/spec/template/spec/containers/0/env", "value": [{"name": "OTHER", "value": "preserved"}]})
        self.assertEqual(patch[1], {"op": "test", "path": "/metadata/resourceVersion", "value": "3"})
        self.assertEqual(module.rollback_portal_patch(original, original, expected), [])
        current["metadata"]["uid"] = "replaced"
        with self.assertRaises(module.OperationError):
            module.rollback_portal_patch(original, current, expected)

    def test_executor_recovery_refuses_another_operators_authentication(self):
        module = load_module()
        self.assertTrue(hasattr(module.Configuration, "assert_executor_recovery_owner"), "원복 직전 인증값 소유권 확인이 필요함")
        service = module.Configuration(str(PATH.parents[3]))
        before = {"Image": "image", "Config": {"Env": [module.KEY + "="]}, "HostConfig": {}, "Mounts": []}
        observed = copy.deepcopy(before)
        observed["Config"]["Env"] = [module.KEY + "=another-operator-test-auth"]
        with self.assertRaises(module.OperationError):
            service.assert_executor_recovery_owner(before, observed, "this-operation-test-auth")
        observed["Config"]["Env"] = [module.KEY + "=this-operation-test-auth"]
        service.assert_executor_recovery_owner(before, observed, "this-operation-test-auth")

    def test_partial_secret_and_executor_setup_resumes_only_missing_portal(self):
        module = load_module()
        auth = "a" * 64
        deployment = {"metadata": {"uid": "portal", "resourceVersion": "1"}, "spec": {"template": {"spec": {"containers": [{"env": []}]}}}}
        pod = {"metadata": {"name": "portal"}}
        docker = {"Id": "original", "Image": "image", "Config": {"Env": [module.KEY + "=" + auth]}, "HostConfig": {}, "Mounts": []}
        secret = {"metadata": {"name": "source", "uid": "source-uid", "resourceVersion": "1"}, "data": {module.KEY: base64.b64encode(auth.encode()).decode()}}

        class PartialConfiguration(module.Configuration):
            def __init__(self):
                super().__init__(str(PATH.parents[3]))
                self.value = copy.deepcopy(deployment)
                self.loaded = False
                self.compose_count = 0

            def portal(self):
                return copy.deepcopy(self.value), pod

            def docker(self):
                return docker

            def source(self, _):
                return secret

            def portal_auth_matches(self, *_):
                return self.loaded

            def compose(self, *_):
                self.compose_count += 1
                raise AssertionError("인증이 이미 일치하는 실행기는 다시 만들면 안 됨")

            def no_backup_activity(self):
                pass

            def portal_cas(self, patch):
                self.value["spec"]["template"]["spec"]["containers"][0]["env"].append(copy.deepcopy(patch[-1]["value"]))
                self.loaded = True

            def wait_portal(self):
                return self.portal()

            def verify(self, *_):
                if not self.loaded:
                    raise module.OperationError("portal_auth_not_loaded")
                return {"portal_auth_match": True}

        service = PartialConfiguration()
        sequence = type("Sequence", (), {"local_lock": staticmethod(nullcontext)})
        with patch.object(module, "run_command", side_effect=["c" * 40, "main", ""]), patch.object(module.importlib.util, "module_from_spec", return_value=sequence), patch.object(module.importlib.util, "spec_from_file_location") as factory:
            factory.return_value = type("Spec", (), {"name": "sequence-test", "loader": type("Loader", (), {"exec_module": staticmethod(lambda _: None)})})
            result = service.configure("c" * 40)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(service.compose_count, 0)
        self.assertEqual(service.value["spec"]["template"]["spec"]["containers"][0]["env"][0]["valueFrom"]["secretKeyRef"]["name"], "source")


if __name__ == "__main__":
    unittest.main()
