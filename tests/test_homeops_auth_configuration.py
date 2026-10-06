"""Pure contracts for the narrowly scoped HomeOps shared-auth setup."""
import copy
import importlib.util
from pathlib import Path
import unittest


PATH = Path(__file__).resolve().parents[1] / "infra/k8s/tools/homeops-auth-contract.py"
CONTRACT = None
if PATH.exists():
    SPEC = importlib.util.spec_from_file_location("homeops_auth_contract", PATH)
    CONTRACT = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(CONTRACT)
KEY = "HOMEOPS_EXECUTOR_SHARED_SECRET"


def deployment():
    return {"metadata": {"uid": "deployment-uid", "resourceVersion": "17"},
            "spec": {"template": {"spec": {"containers": [{"name": "portal", "env": [
                {"name": "OTHER", "value": "unchanged"}],
                "envFrom": [{"secretRef": {"name": "portal-secrets"}}]}]}}}}


def secret():
    return {"metadata": {"uid": "secret-uid", "resourceVersion": "23", "name": "portal-secrets"},
            "data": {"OTHER": "dW5jaGFuZ2Vk"}}


def container():
    return {"Config": {"Image": "executor:old", "Hostname": "old-id", "Labels": {
        "com.docker.compose.project": "personal-server",
        "com.docker.compose.project.config_files": "/repo/base.yml,/repo/n100.yml,/repo/bridge.yml,/repo/private-image.yml"},
        "Env": ["OTHER=kept", KEY + "=example-only"], "User": "1000"},
        "HostConfig": {"Binds": ["b:/b:ro", "a:/a:ro"], "PortBindings": {
            "9000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9001"},
                         {"HostIp": "127.0.0.1", "HostPort": "9000"}]}},
        "Mounts": [{"Destination": "/b", "Source": "b"}, {"Destination": "/a", "Source": "a"}]}


class HomeOpsAuthConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(CONTRACT, "shared-auth contract implementation is missing")

    def test_validate_source_resolves_the_single_referenced_secret(self):
        self.assertEqual(CONTRACT.validate_secret_source(deployment(), secret()), "portal-secrets")

    def test_validate_source_rejects_ambiguous_or_unpatchable_objects(self):
        cases = []
        d = deployment(); d["spec"]["template"]["spec"]["containers"].append({}); cases.append((d, secret()))
        d = deployment(); d["spec"]["template"]["spec"]["containers"][0]["envFrom"].append({"secretRef": {"name": "second"}}); cases.append((d, secret()))
        s = secret(); s["metadata"]["name"] = "different"; cases.append((deployment(), s))
        s = secret(); s["immutable"] = True; cases.append((deployment(), s))
        for field in ("uid", "resourceVersion"):
            s = secret(); del s["metadata"][field]; cases.append((deployment(), s))
        s = secret(); del s["data"]; cases.append((deployment(), s))
        for d, s in cases:
            with self.subTest(d=d, s=s), self.assertRaises(ValueError):
                CONTRACT.validate_secret_source(d, s)

    def test_secret_patch_adds_only_missing_key_with_uid_and_version_cas(self):
        s = secret(); original = copy.deepcopy(s)
        self.assertEqual(CONTRACT.secret_patch(s, "ZXhhbXBsZS1vbmx5"), [
            {"op": "test", "path": "/metadata/uid", "value": "secret-uid"},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "23"},
            {"op": "add", "path": "/data/" + KEY, "value": "ZXhhbXBsZS1vbmx5"}])
        self.assertEqual(s, original)
        s["data"][KEY] = "ZXhpc3Rpbmc="
        self.assertEqual(CONTRACT.secret_patch(s, "bmV3"), [])

    def test_secret_patch_rejects_missing_data_and_missing_cas(self):
        for field in ("data", "uid", "resourceVersion"):
            s = secret()
            del (s if field == "data" else s["metadata"])[field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                CONTRACT.secret_patch(s, "ZXhhbXBsZQ==")

    def test_portal_patch_preserves_env_and_uses_secret_ref(self):
        d = deployment(); original = copy.deepcopy(d)
        self.assertEqual(CONTRACT.portal_patch(d, "portal-secrets"), [
            {"op": "test", "path": "/metadata/uid", "value": "deployment-uid"},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "17"},
            {"op": "test", "path": "/spec/template/spec/containers/0/env", "value": [{"name": "OTHER", "value": "unchanged"}]},
            {"op": "add", "path": "/spec/template/spec/containers/0/env/-", "value": {
                "name": KEY, "valueFrom": {"secretKeyRef": {"name": "portal-secrets", "key": KEY, "optional": False}}}}])
        self.assertEqual(d, original)

    def test_portal_patch_handles_absent_env_and_correct_existing_ref(self):
        d = deployment(); c = d["spec"]["template"]["spec"]["containers"][0]; del c["env"]
        patch = CONTRACT.portal_patch(d, "portal-secrets")
        self.assertEqual(patch[2], {"op": "add", "path": "/spec/template/spec/containers/0/env", "value": []})
        c["env"] = [{"name": KEY, "valueFrom": {"secretKeyRef": {"name": "portal-secrets", "key": KEY}}}]
        self.assertEqual(CONTRACT.portal_patch(d, "portal-secrets"), [])
        self.assertEqual(CONTRACT.validate_secret_source(d, secret()), "portal-secrets")

    def test_wrong_and_duplicate_auth_env_are_rejected_without_leaking_value(self):
        wrong = [{"name": KEY, "value": "sensitive-fixture"}, {"name": KEY, "valueFrom": {
            "secretKeyRef": {"name": "wrong", "key": KEY}}}, {"name": KEY, "valueFrom": {
            "secretKeyRef": {"name": "portal-secrets", "key": KEY, "optional": True}}}]
        for entry in wrong + [[wrong[0], wrong[0]]]:
            d = deployment(); d["spec"]["template"]["spec"]["containers"][0]["env"] = entry if isinstance(entry, list) else [entry]
            for call in (lambda: CONTRACT.portal_patch(d, "portal-secrets"), lambda: CONTRACT.validate_secret_source(d, secret())):
                with self.assertRaises(ValueError) as caught: call()
                self.assertNotIn("sensitive-fixture", str(caught.exception))

    def test_fingerprints_normalize_only_documented_irrelevant_order_and_fields(self):
        a = container(); b = copy.deepcopy(a)
        b["Config"]["Env"].reverse(); b["HostConfig"]["Binds"].reverse()
        b["HostConfig"]["PortBindings"]["9000/tcp"].reverse(); b["Mounts"].reverse()
        b["Config"]["Hostname"] = "new-id"; b["Config"]["Image"] = "changed-tag"; b["Config"]["Labels"] = {}
        self.assertEqual(CONTRACT.fingerprint(a), CONTRACT.fingerprint(b))
        b["Config"]["Env"] = ["OTHER=changed", KEY + "=different"]
        self.assertNotEqual(CONTRACT.fingerprint(a, True), CONTRACT.fingerprint(b, True))
        b["Config"]["Env"] = ["OTHER=kept", KEY + "=different"]
        self.assertEqual(CONTRACT.fingerprint(a, True), CONTRACT.fingerprint(b, True))
        self.assertNotEqual(CONTRACT.fingerprint(a), CONTRACT.fingerprint(b))

    def test_duplicate_environment_keys_cannot_be_hidden_by_fingerprint(self):
        c = container(); c["Config"]["Env"] += [KEY + "=sensitive-fixture"]
        for ignore in (False, True):
            with self.assertRaises(ValueError) as caught: CONTRACT.fingerprint(c, ignore)
            self.assertNotIn("sensitive-fixture", str(caught.exception))

    def test_compose_uses_original_four_files_executor_only_and_secret_in_env(self):
        argv, env = CONTRACT.compose_invocation(container(), "/repo", "sensitive-fixture", "172.20.0.1")
        self.assertEqual(argv, ["docker", "compose", "--project-directory", "/repo", "-p", "personal-server",
            "-f", "/repo/base.yml", "-f", "/repo/n100.yml", "-f", "/repo/bridge.yml", "-f", "/repo/private-image.yml",
            "up", "-d", "--no-build", "--no-deps", "--pull", "never", "homeops-executor"])
        self.assertEqual(env, {KEY: "sensitive-fixture", "DOCKER_BRIDGE_GATEWAY": "172.20.0.1"})
        self.assertNotIn("sensitive-fixture", " ".join(argv))

    def test_compose_rejects_empty_and_control_character_inputs_safely(self):
        for value in ("", "bad\nvalue", "bad\x00value"):
            for field in ("repo", "secret", "bridge", "project", "config_files"):
                c = container(); args = {"repo": "/repo", "secret": "sensitive-fixture", "bridge": "172.20.0.1"}
                if field in args: args[field] = value
                else: c["Config"]["Labels"]["com.docker.compose.project." + field if field == "config_files" else "com.docker.compose.project"] = value
                with self.subTest(value=value, field=field), self.assertRaises(ValueError) as caught:
                    CONTRACT.compose_invocation(c, **args)
                self.assertNotIn("sensitive-fixture", str(caught.exception))

    def test_network_fingerprint_preserves_membership_aliases_and_static_ipam(self):
        a = container()
        a["NetworkSettings"] = {"Networks": {"personal-server_default": {
            "Aliases": ["homeops-executor", "friendly", "friendly"],
            "IPAMConfig": {"IPv4Address": "172.21.0.5"}, "Links": None,
            "EndpointID": "old-endpoint", "NetworkID": "old-network", "IPAddress": "172.21.0.5", "MacAddress": "old"}}}
        b = copy.deepcopy(a)
        network = b["NetworkSettings"]["Networks"]["personal-server_default"]
        network["Aliases"].reverse(); network["EndpointID"] = "new-endpoint"
        network["NetworkID"] = "new-network"; network["IPAddress"] = "172.21.0.9"; network["MacAddress"] = "new"
        self.assertIn("Networks", CONTRACT.fingerprint(a))
        self.assertEqual(CONTRACT.fingerprint(a), CONTRACT.fingerprint(b))
        network["Aliases"].append("unexpected")
        self.assertNotEqual(CONTRACT.fingerprint(a), CONTRACT.fingerprint(b))
        network["Aliases"].remove("unexpected"); network["Aliases"].remove("friendly")
        self.assertNotEqual(CONTRACT.fingerprint(a), CONTRACT.fingerprint(b))
        network["Aliases"].append("friendly"); network["IPAMConfig"]["IPv4Address"] = "172.21.0.6"
        self.assertNotEqual(CONTRACT.fingerprint(a), CONTRACT.fingerprint(b))
        network["IPAMConfig"]["IPv4Address"] = "172.21.0.5"
        b["NetworkSettings"]["Networks"]["unexpected_network"] = {}
        self.assertNotEqual(CONTRACT.fingerprint(a), CONTRACT.fingerprint(b))

    def test_compose_network_guard_resolves_logical_names_and_checks_static_addresses(self):
        self.assertTrue(callable(getattr(CONTRACT, "assert_compose_networks", None)), "network guard is missing")
        config = {"services": {"homeops-executor": {"container_name": "executor-container", "networks": {
            "default": {"aliases": ["friendly"], "ipv4_address": "172.21.0.5"}, "docker-api": {}}}},
            "networks": {"default": {"name": "personal-server_default"}, "docker-api": {"name": "api"}}}
        c = container(); c["NetworkSettings"] = {"Networks": {
            "personal-server_default": {"Aliases": ["homeops-executor", "executor-container", "friendly"], "IPAMConfig": {"IPv4Address": "172.21.0.5"}},
            "api": {"Aliases": ["homeops-executor", "executor-container"], "IPAMConfig": None}}}
        CONTRACT.assert_compose_networks(config, c)
        cases = []
        changed = copy.deepcopy(c); del changed["NetworkSettings"]["Networks"]["api"]; cases.append(changed)
        changed = copy.deepcopy(c); changed["NetworkSettings"]["Networks"]["api"]["Aliases"].append("removed-custom-alias"); cases.append(changed)
        changed = copy.deepcopy(c); changed["NetworkSettings"]["Networks"]["personal-server_default"]["Aliases"].remove("friendly"); cases.append(changed)
        changed = copy.deepcopy(c); changed["NetworkSettings"]["Networks"]["personal-server_default"]["IPAMConfig"]["IPv4Address"] = "172.21.0.6"; cases.append(changed)
        for changed in cases:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                CONTRACT.assert_compose_networks(config, changed)


if __name__ == "__main__":
    unittest.main()
