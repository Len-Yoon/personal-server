"""Synthetic transitions only; no production, Docker, credentials or network."""
import copy
import importlib.util
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

PATH = Path(__file__).resolve().parents[1] / "infra/k8s/tools/caddy-ingress-transition.py"
SPEC = importlib.util.spec_from_file_location("caddy_ingress_transition", PATH)
m = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = m
SPEC.loader.exec_module(m)

BRIDGE = b"# existing runtime override\nservices:\n  book-memo:\n    ports:\n      - '127.0.0.1:18003:8003'\n"
ENV = b"UNRELATED=original\nSYNTHETIC_TOKEN=do-not-copy\n"


def fixture():
    caddy_id, default_id = "a" * 64, "b" * 64
    caddy = {"Id": caddy_id, "Image": "sha256:" + "c" * 64, "State": {"Running": True},
             "Config": {"Image": "synthetic-caddy:existing", "Hostname": caddy_id[:12],
                        "Env": ["PORTAL_UPSTREAM=old-route:8000", "SYNTHETIC_TOKEN=do-not-copy"],
                        "Labels": {"com.docker.compose.config-hash": "old"}},
             "HostConfig": {"NetworkMode": "synthetic_default", "CapDrop": ["ALL"], "PortBindings": {"80/tcp": []}},
             "Mounts": [{"Name": "existing-data", "Destination": "/data"}],
             "NetworkSettings": {"Networks": {"synthetic_default": {"NetworkID": default_id, "IPAMConfig": None}}}}
    default = {"Name": "synthetic_default", "Id": default_id, "Driver": "bridge", "Scope": "local",
               "Internal": False, "Attachable": False, "Ingress": False, "EnableIPv4": True, "EnableIPv6": False,
               "ConfigFrom": {"Network": ""}, "ConfigOnly": False,
               "IPAM": {"Driver": "default", "Options": {}, "Config": [{"Subnet": "172.29.0.0/16"}]},
               "Options": {}, "Labels": {"com.docker.compose.network": "default"},
               "Containers": {caddy_id: {"Name": "synthetic-caddy"}, "d" * 64: {"Name": "other"}}}
    config = {"name": "synthetic", "services": {
        "caddy": {"image": "synthetic-caddy:existing", "networks": {"default": {}},
                  "environment": {"PORTAL_UPSTREAM": "old-route:8000", "SYNTHETIC_TOKEN": "do-not-copy"},
                  "ports": [{"target": 80, "published": "80"}], "volumes": [{"source": "existing-data", "target": "/data"}]},
        "book-memo": {"image": "synthetic-book:existing"}},
        "networks": {"default": {"name": "synthetic_default"}}}
    fresh = {"source_commit": "1" * 40, "tracked_clean": True, "policy_count": 0, "compose_version": "5.3.0",
             "caddy": caddy, "local_image_id": caddy["Image"], "default_network": default,
             "networks": [default], "config": config, "other_containers": [{"Id": "d" * 64}],
             "collisions": ["172.29.0.0/16", "10.43.0.0/16", "10.42.0.0/16", "192.0.2.2/32"],
             "portal_upstream": "10.43.1.10:8000", "service_routes": [
                 {"name": "portal-web", "address": "10.43.1.10", "port": 8000, "uid": "synthetic-uid"}]}
    plan = {"schema_version": 1, "source_commit": fresh["source_commit"],
            "bridge_sha256": m.digest(BRIDGE), "env_sha256": m.digest(ENV),
            "network_name": "synthetic_caddy_ingress", "owner_id": "e" * 64,
            "budget": {"forward_seconds": 60, "rollback_seconds": 60},
            "reviewed": True, "protected_guard_ack": True}
    prepared = m.prepare_transition(fresh, BRIDGE, ENV, plan)
    plan.update(prepared_sha256=prepared["prepared_sha256"], prepared_projection=m.prepared_projection(prepared))
    return plan, fresh, prepared


def network(prepared):
    address = prepared["address"]
    return {"Id": "f" * 64, "Name": prepared["network_name"], "Driver": "bridge", "Scope": "local",
            "Internal": False, "Attachable": False, "Ingress": False, "EnableIPv4": True, "EnableIPv6": False,
               "ConfigFrom": {"Network": ""}, "ConfigOnly": False,
            "Labels": {m.OWNER_LABEL: prepared["owner_id"]}, "Options": {}, "Containers": {},
            "IPAM": {"Driver": "default", "Options": {}, "Config": [{"Subnet": address["subnet"],
                "IPRange": address["ip_range"], "Gateway": address["gateway"]}]}}


class FakeJournal:
    def __init__(self):
        self.state = {}
        self.events = []

    def save(self, **updates):
        self.state.update(copy.deepcopy(updates))
        self.events.append(copy.deepcopy(self.state))


class FakeBackend:
    def __init__(self, plan, fresh, prepared):
        self.plan, self.fresh, self._prepared = plan, fresh, prepared
        self.lock_owned = True
        self.command_termination_unknown = False
        self.bridge, self.env = BRIDGE, ENV
        self.network = None
        self.calls = []
        self.forward = False
        self.fail = set()
        self.lost = set()

    def guard(self):
        self.calls.append("guard")
        if "guard" in self.fail:
            raise m.Blocked("synthetic_guard_failure")

    def phase_deadline(self, rollback):
        self.calls.append("deadline_rollback" if rollback else "deadline_forward")

    def snapshot(self):
        return self.fresh

    def runtime_bytes(self):
        return self.bridge, self.env

    def create_network(self, prepared):
        self.calls.append("create")
        if "create" not in self.fail:
            self.network = network(prepared)
        if "create" in self.fail | self.lost:
            raise m.UnknownWrite("synthetic_lost_response")

    def owned_network(self):
        self.calls.append("inspect_network")
        return self.network

    def write_runtime(self, prepared, bridge, env):
        self.calls.append("write")
        if "partial_env" in self.fail:
            self.env += prepared["env_suffix"][:10]
            raise OSError("synthetic_partial_append")
        self.bridge, self.env = prepared["target_bridge"], env + prepared["env_suffix"]

    def compose_config(self):
        config = copy.deepcopy(self.fresh["config"])
        if self.bridge != BRIDGE:
            config["services"]["caddy"]["networks"] = {"default": {"gw_priority": 0}, m.NETWORK_KEY: {
                "ipv4_address": self._prepared["address"]["fixed_ip"], "gw_priority": 1}}
            config["services"]["caddy"]["environment"]["PORTAL_UPSTREAM"] = self._prepared["portal_upstream"]
            config["networks"][m.NETWORK_KEY] = {"external": True, "name": self.plan["network_name"]}
        if "config" in self.fail:
            config["services"]["book-memo"]["image"] = "unexpected"
        return config

    def assert_image(self, image):
        self.calls.append("assert_image")
        if "image" in self.fail:
            raise m.Blocked("synthetic_image_changed")

    def recreate_caddy(self, prepared):
        self.calls.append("recreate")
        self.forward = self.bridge != BRIDGE
        if self.network:
            self.network["Containers"] = {"a" * 64: {}} if self.forward else {}
        if "recreate" in self.lost:
            raise m.UnknownWrite("synthetic_lost_response")
        if "recreate_unknown" in self.fail:
            self.command_termination_unknown = True
            raise m.UnknownWrite("synthetic_live_descendant")

    def verify(self, prepared, *, rollback):
        self.calls.append("verify_rollback" if rollback else "verify_forward")
        if "verify" in self.fail or self.forward == rollback:
            raise m.Blocked("synthetic_verify_failure")

    def recover_prepared(self, record):
        if m.public_record(self._prepared) != record:
            raise m.Blocked("synthetic_record_changed")
        return self._prepared

    def assert_recoverable(self, prepared, state):
        self.calls.append("recoverable")
        if self.command_termination_unknown:
            raise m.Blocked("child_termination_unknown")
        if self.network:
            m.assert_owned_network(self.network, prepared, id_sha256=state.get("network_id_sha256"))
        m.inverse_env(self.env, prepared["env_size"], prepared["env_sha256"], prepared["env_suffix"])

    def restore_runtime(self, prepared):
        self.calls.append("restore")
        if self.bridge != BRIDGE:
            self.bridge = m.restore_bridge(self.bridge, prepared["network_name"], prepared["bridge_sha256"])
        self.env = m.inverse_env(self.env, prepared["env_size"], prepared["env_sha256"], prepared["env_suffix"])

    def is_original_caddy(self, prepared):
        return not self.forward

    def remove_network(self, network_id):
        self.calls.append("remove")
        if "remove" not in self.fail:
            self.network = None
        if "remove" in self.fail | self.lost:
            raise m.UnknownWrite("synthetic_lost_response")


class PreparationTests(unittest.TestCase):
    def test_collision_search_checks_all_ranges_and_fixed_outside_dynamic_pool(self):
        spec = m.select_subnet(["10.240.0.0/29", "10.240.0.9/32", "2001:db8::/32", "0.0.0.0/0"])
        self.assertEqual(spec["subnet"], "10.240.0.16/29")
        self.assertEqual(spec["fixed_ip"], "10.240.0.18")
        self.assertEqual(spec["ip_range"], "10.240.0.20/30")
        m.validate_address_spec(spec)

    def test_collision_fail_closed_missing_invalid_or_exhausted(self):
        for collisions in ([], ["malformed"], ["10.240.0.0/16"]):
            with self.subTest(collisions=collisions), self.assertRaises(m.Blocked):
                m.select_subnet(collisions)

    def test_runtime_bridge_original_prefix_and_exact_inverse(self):
        target = m.edit_bridge(BRIDGE, "synthetic_caddy_ingress")
        self.assertTrue(target.startswith(BRIDGE))
        self.assertEqual(m.restore_bridge(target, "synthetic_caddy_ingress", m.digest(BRIDGE)), BRIDGE)
        with self.assertRaises(m.Blocked):
            m.restore_bridge(target + b"# foreign\n", "synthetic_caddy_ingress", m.digest(BRIDGE))

    def test_bridge_ambiguous_shape_credentials_and_existing_network_refused(self):
        for raw in (BRIDGE + b"  caddy:\n", BRIDGE + b"networks:\n", BRIDGE.replace(b"ports:", b"password:"), BRIDGE.replace(b"\n", b"\r\n")):
            with self.subTest(raw=raw), self.assertRaises(m.Blocked):
                m.edit_bridge(raw, "synthetic_caddy_ingress")

    def test_env_rollback_accepts_only_owned_partial_or_full_suffix(self):
        _, _, prepared = fixture()
        suffix = prepared["env_suffix"]
        for size in (0, 1, 10, len(suffix)):
            self.assertEqual(m.inverse_env(ENV + suffix[:size], len(ENV), m.digest(ENV), suffix), ENV)
        for current in (ENV + b"foreign", b"different" + suffix, ENV + suffix + b"foreign"):
            with self.assertRaises(m.Blocked):
                m.inverse_env(current, len(ENV), m.digest(ENV), suffix)

    def test_bridge_partial_append_rollback_accepts_only_owned_suffix(self):
        target = m.edit_bridge(BRIDGE, "synthetic_caddy_ingress")
        suffix = target[len(BRIDGE):]
        for count in (0, 1, 20, len(suffix)):
            self.assertEqual(m.inverse_bridge(BRIDGE + suffix[:count], len(BRIDGE), m.digest(BRIDGE),
                                             "synthetic_caddy_ingress"), BRIDGE)
        with self.assertRaises(m.Blocked):
            m.inverse_bridge(BRIDGE + b"foreign", len(BRIDGE), m.digest(BRIDGE), "synthetic_caddy_ingress")

    def test_env_suffix_never_contains_existing_credential_bytes(self):
        _, _, prepared = fixture()
        self.assertNotIn(b"do-not-copy", prepared["env_suffix"])
        self.assertEqual(len(prepared["env_suffix"].splitlines()), 2)
        with self.assertRaises(m.Blocked):
            m.env_suffix(ENV + b"export CADDY_INGRESS_FIXED_IP=foreign\n", prepared["address"], prepared["portal_upstream"])

    def test_prepare_pins_actual_image_source_network_and_runtime_hash(self):
        plan, fresh, _ = fixture()
        changes = [("policy_count", 1), ("tracked_clean", False), ("source_commit", "2" * 40),
                   ("local_image_id", "sha256:" + "d" * 64)]
        for key, value in changes:
            changed = copy.deepcopy(fresh)
            changed[key] = value
            with self.subTest(key=key), self.assertRaises(m.Blocked):
                m.prepare_transition(changed, BRIDGE, ENV, plan)
        with self.assertRaises(m.Blocked):
            m.prepare_transition(fresh, BRIDGE, ENV + b"changed", plan)

    def test_network_owner_full_spec_identity_and_empty_gate(self):
        _, _, prepared = fixture()
        good = network(prepared)
        m.assert_owned_network(good, prepared, empty=True, id_sha256=m.digest(good["Id"].encode()))
        for key, value in (("Id", "0" * 64), ("Options", {"foreign": "true"}), ("Labels", {}), ("Containers", {"foreign": {}})):
            bad = copy.deepcopy(good)
            bad[key] = value
            with self.subTest(key=key), self.assertRaises(m.Blocked):
                m.assert_owned_network(bad, prepared, empty=True, id_sha256=m.digest(good["Id"].encode()))

    def test_config_contract_blocks_other_services_ports_security_env_and_volumes(self):
        plan, fresh, prepared = fixture()
        backend = FakeBackend(plan, fresh, prepared)
        backend.write_runtime(prepared, BRIDGE, ENV)
        target = backend.compose_config()
        m.config_contract(fresh["config"], target, prepared["network_name"], prepared["address"], prepared["portal_upstream"])
        for field, value in (("ports", []), ("volumes", []), ("cap_add", ["SYS_ADMIN"]),
                             ("environment", {"PORTAL_UPSTREAM": prepared["portal_upstream"], "SYNTHETIC_TOKEN": "changed"})):
            changed = copy.deepcopy(target)
            changed["services"]["caddy"][field] = value
            with self.subTest(field=field), self.assertRaises(m.Blocked):
                m.config_contract(fresh["config"], changed, prepared["network_name"], prepared["address"], prepared["portal_upstream"])

    def test_runtime_contract_normalizes_only_expected_generated_fields(self):
        _, fresh, _ = fixture()
        old = fresh["caddy"]
        new = copy.deepcopy(old)
        new["Id"] = "f" * 64
        new["Config"]["Hostname"] = new["Id"][:12]
        new["Config"]["Labels"]["com.docker.compose.config-hash"] = "new"
        new["HostConfig"]["NetworkMode"] = "synthetic_caddy_ingress"
        new["Config"]["Env"][0] = "PORTAL_UPSTREAM=approved-route:8000"
        self.assertEqual(m.caddy_runtime_contract(old), m.caddy_runtime_contract(new))
        new["Config"]["Env"][1] = "SYNTHETIC_TOKEN=changed"
        self.assertNotEqual(m.caddy_runtime_contract(old), m.caddy_runtime_contract(new))

    def test_generated_metadata_accepts_only_pinned_version_image_and_predecessor(self):
        _, fresh, prepared = fixture()
        new = copy.deepcopy(fresh["caddy"])
        old_id = new["Id"]
        new["Id"] = "9" * 64
        new["Config"]["Labels"].update({"com.docker.compose.version": "5.3.0",
            "com.docker.compose.replace": old_id, "com.docker.compose.image": new["Image"]})
        m.generated_identity(new, prepared)
        for key, value in (("com.docker.compose.version", "foreign"), ("com.docker.compose.replace", "0" * 64),
                           ("com.docker.compose.image", "sha256:" + "0" * 64)):
            changed = copy.deepcopy(new)
            changed["Config"]["Labels"][key] = value
            with self.subTest(key=key), self.assertRaises(m.Blocked):
                m.generated_identity(changed, prepared)

    def test_immutable_image_contract_allows_only_actual_id_and_approved_reference(self):
        _, fresh, prepared = fixture()
        baseline = fresh["caddy"]
        pinned = copy.deepcopy(baseline)
        pinned["Config"]["Image"] = prepared["image_id"]
        self.assertEqual(m.caddy_runtime_contract(baseline, prepared), m.caddy_runtime_contract(pinned, prepared))
        pinned["Config"]["Image"] = "foreign-reference:latest"
        with self.assertRaisesRegex(m.Blocked, "caddy_image_reference_changed"):
            m.caddy_runtime_contract(pinned, prepared)
        pinned["Image"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(m.Blocked, "caddy_actual_image_changed"):
            m.caddy_runtime_contract(pinned, prepared)

    def test_config_files_label_only_allows_exact_baseline_or_pinned_stdin_list(self):
        plan, fresh, _ = fixture()
        files = "/synthetic/base.yml,/synthetic/n100.yml,/synthetic/bridge.yml"
        key = "com.docker.compose.project.config_files"
        fresh["caddy"]["Config"]["Labels"][key] = files
        prepared = m.prepare_transition(fresh, BRIDGE, ENV, plan)
        baseline = m.caddy_runtime_contract(fresh["caddy"], prepared)
        pinned = copy.deepcopy(fresh["caddy"])
        pinned["Config"]["Image"] = prepared["image_id"]
        pinned["Config"]["Labels"][key] = files + ",-"
        self.assertEqual(m.caddy_runtime_contract(pinned, prepared), baseline)
        for value in (files + ",foreign.yml", "/other/base.yml,-"):
            pinned["Config"]["Labels"][key] = value
            with self.subTest(value=value), self.assertRaises(m.Blocked):
                m.caddy_runtime_contract(pinned, prepared)
        fresh["compose_stdin_config_name"] = "/synthetic/-"
        prepared = m.prepare_transition(fresh, BRIDGE, ENV, plan)
        pinned["Config"]["Labels"][key] = files + ",/synthetic/-"
        self.assertEqual(m.caddy_runtime_contract(pinned, prepared), m.caddy_runtime_contract(fresh["caddy"], prepared))

    def test_ephemeral_image_model_cannot_change_build_env_ports_or_other_service(self):
        _, fresh, prepared = fixture()
        expected = copy.deepcopy(fresh["config"])
        expected["services"]["caddy"]["image"] = prepared["image_id"]
        m.image_override_contract(fresh["config"], expected, prepared["image_id"])
        for service, field, value in (("caddy", "ports", []), ("caddy", "environment", {}),
                                      ("caddy", "build", {"context": "foreign"}),
                                      ("book-memo", "image", "foreign:latest")):
            changed = copy.deepcopy(expected)
            changed["services"][service][field] = value
            with self.subTest(service=service, field=field), self.assertRaises(m.Blocked):
                m.image_override_contract(fresh["config"], changed, prepared["image_id"])

    def test_git_config_guard_blocks_external_execution_and_includes(self):
        m.git_config_guard(b"core.attributesfile\n/dev/null\0core.fsmonitor\nfalse\0user.name\nsynthetic\0")
        for key, value in (("include.path", "/foreign"), ("includeif.gitdir:x.path", "/foreign"),
                ("filter.attack.clean", "external-command"), ("core.attributesfile", "/foreign"),
                ("diff.custom.command", "external-command"), ("diff.custom.textconv", "external-command"),
                ("core.sshcommand", "external-command"), ("core.fsmonitor", "true"), ("core.hookspath", "/foreign")):
            with self.subTest(key=key), self.assertRaises(m.Blocked):
                m.git_config_guard((key + "\n" + value + "\0").encode())


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.plan, self.fresh, self.prepared = fixture()
        self.backend = FakeBackend(self.plan, self.fresh, self.prepared)
        self.journal = FakeJournal()

    def test_complete_only_caddy_once_and_private_journal(self):
        self.assertEqual(m.transition(self.plan, self.backend, self.journal), "COMPLETE")
        self.assertEqual(self.backend.calls.count("create"), 1)
        self.assertEqual(self.backend.calls.count("recreate"), 1)
        raw = json.dumps(self.journal.events)
        for forbidden in ("do-not-copy", self.prepared["address"]["fixed_ip"], self.prepared["portal_upstream"], self.prepared["network_name"]):
            self.assertNotIn(forbidden, raw)
        self.assertEqual(self.journal.events[1]["phase"], "network_create_intent")

    def test_missing_lock_or_review_or_changed_preparation_never_writes(self):
        for key, value in (("reviewed", False), ("prepared_sha256", "0" * 64), ("protected_guard_ack", False)):
            plan = copy.deepcopy(self.plan)
            plan[key] = value
            with self.subTest(key=key), self.assertRaises(m.Blocked):
                m.transition(plan, self.backend, self.journal)
        self.backend.lock_owned = False
        with self.assertRaises(m.Blocked):
            m.transition(self.plan, self.backend, self.journal)
        self.assertNotIn("create", self.backend.calls)

    def test_lost_create_and_compose_response_reads_back_without_repeat(self):
        self.backend.lost = {"create", "recreate"}
        self.assertEqual(m.transition(self.plan, self.backend, self.journal), "COMPLETE")
        self.assertEqual(self.backend.calls.count("create"), 1)
        self.assertEqual(self.backend.calls.count("recreate"), 1)
        self.assertIn("inspect_network", self.backend.calls)

    def test_create_absent_or_partial_env_holds_and_new_go_blocked(self):
        for failure in ("create", "partial_env"):
            backend = FakeBackend(self.plan, self.fresh, self.prepared)
            backend.fail = {failure}
            journal = FakeJournal()
            self.assertEqual(m.transition(self.plan, backend, journal), "HELD")
            with self.assertRaises(m.Blocked):
                m.transition(self.plan, backend, journal)

    def test_unapproved_config_stops_before_caddy_recreation(self):
        self.backend.fail = {"config"}
        self.assertEqual(m.transition(self.plan, self.backend, self.journal), "HELD")
        self.assertNotIn("recreate", self.backend.calls)

    def test_complete_explicit_rollback_restores_exact_files_then_network_removed(self):
        m.transition(self.plan, self.backend, self.journal)
        self.assertEqual(m.transition(self.plan, self.backend, self.journal, rollback=True), "ROLLED_BACK")
        self.assertEqual(self.backend.bridge, BRIDGE)
        self.assertEqual(self.backend.env, ENV)
        self.assertIsNone(self.backend.network)
        self.assertEqual(self.backend.calls.count("recreate"), 2)
        self.assertLess(self.backend.calls.index("verify_rollback"), self.backend.calls.index("remove"))

    def test_partial_append_rollback_has_no_unnecessary_caddy_recreate(self):
        self.backend.fail = {"partial_env"}
        self.assertEqual(m.transition(self.plan, self.backend, self.journal), "HELD")
        self.assertEqual(m.transition(self.plan, self.backend, self.journal, rollback=True), "ROLLED_BACK")
        self.assertEqual(self.backend.env, ENV)
        self.assertNotIn("recreate", self.backend.calls)

    def test_unknown_descendant_never_rolls_back_or_retries(self):
        self.backend.fail = {"recreate_unknown"}
        self.assertEqual(m.transition(self.plan, self.backend, self.journal), "HELD")
        with self.assertRaises(m.Blocked):
            m.transition(self.plan, self.backend, self.journal, rollback=True)
        self.assertNotIn("restore", self.backend.calls)
        self.assertEqual(self.backend.calls.count("recreate"), 1)
        self.backend.command_termination_unknown = False
        with self.assertRaises(m.Blocked):
            m.transition(self.plan, self.backend, self.journal, rollback=True)
        self.assertNotIn("restore", self.backend.calls)

    def test_foreign_network_or_suffix_blocks_rollback_without_writes(self):
        m.transition(self.plan, self.backend, self.journal)
        self.backend.network["Id"] = "0" * 64
        before = list(self.backend.calls)
        self.assertEqual(m.transition(self.plan, self.backend, self.journal, rollback=True), "HELD")
        self.assertNotIn("restore", self.backend.calls[len(before):])
        self.assertNotIn("remove", self.backend.calls)

    def test_lost_rollback_and_remove_response_never_repeated(self):
        m.transition(self.plan, self.backend, self.journal)
        self.backend.lost = {"recreate", "remove"}
        self.assertEqual(m.transition(self.plan, self.backend, self.journal, rollback=True), "ROLLED_BACK")
        self.assertEqual(self.backend.calls.count("recreate"), 2)
        self.assertEqual(self.backend.calls.count("remove"), 1)


class NativeSafetyTests(unittest.TestCase):
    def test_git_child_has_isolated_config_without_parent_environment_mutation(self):
        backend = object.__new__(m.NativeBackend)
        backend.root, backend.deadline, backend.command_termination_unknown = Path("/synthetic"), None, False
        backend.group_exists = Mock(return_value=False)
        child = Mock(returncode=0, stdin=None)
        child.communicate.return_value = (b"ok", None)
        hostile = {"GIT_CONFIG_GLOBAL": "/synthetic/hostile", "GIT_DIR": "/synthetic/foreign-repo",
                   "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "filter.attack.clean",
                   "GIT_CONFIG_VALUE_0": "external-command", "GIT_TRACE": "/synthetic/trace",
                   "GIT_WORK_TREE": "/synthetic/work-tree", "GIT_INDEX_FILE": "/synthetic/index",
                   "GIT_OBJECT_DIRECTORY": "/synthetic/objects", "GIT_TRACE2_EVENT": "/synthetic/events"}
        with patch.dict(os.environ, hostile), patch.object(m.subprocess, "Popen", return_value=child) as create:
            backend.git("status", "--porcelain")
            self.assertTrue(all(os.environ[key] == value for key, value in hostile.items()))
            env = create.call_args.kwargs["env"]
            self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null")
            self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
            self.assertEqual(env["GIT_ATTR_NOSYSTEM"], "1")
            self.assertEqual({key for key in env if key.startswith("GIT_")},
                             {"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_ATTR_NOSYSTEM"})
            self.assertIn("core.attributesFile=/dev/null", create.call_args.args[0])

    def test_protected_source_config_guard_precedes_any_git_status(self):
        backend = object.__new__(m.NativeBackend)
        backend.command_termination_unknown, backend.lock_owned = False, True
        events = []
        backend.protected_guard = Mock(side_effect=lambda: events.append("protected"))

        def unsafe_config(*args):
            events.append(args)
            return b"filter.attack.clean\nexternal-command\0"

        backend.git = Mock(side_effect=unsafe_config)
        with self.assertRaises(m.Blocked):
            backend.guard()
        self.assertEqual(events[0], "protected")
        backend.git.assert_called_once_with("config", "--no-includes", "--null", "--list")

    def test_forward_and_rollback_pin_actual_image_even_after_mutable_tag_drift(self):
        plan, fresh, _ = fixture()
        fresh["config"]["services"]["caddy"].pop("image")
        fresh["config"]["services"]["caddy"]["build"] = {"context": "/synthetic/caddy"}
        prepared = m.prepare_transition(fresh, BRIDGE, ENV, plan)
        fake = FakeBackend(plan, fresh, prepared)
        fake.write_runtime(prepared, BRIDGE, ENV)
        forward, rollback = fake.compose_config(), fresh["config"]
        backend = object.__new__(m.NativeBackend)
        backend.root = Path("/synthetic")
        tag = {"image": prepared["image_id"]}
        current = {"model": forward}
        writes = []
        backend.compose_config = Mock(side_effect=lambda: current["model"])

        def inspect(args):
            self.assertEqual(args, ["docker", "image", "inspect", prepared["image_id"]])
            tag["image"] = "sha256:" + "0" * 64
            return [{"Id": prepared["image_id"]}]

        backend.json = Mock(side_effect=inspect)

        def run(args, *, stdin, **kwargs):
            self.assertEqual(json.loads(stdin), {"services": {"caddy": {"image": prepared["image_id"]}}})
            self.assertNotIn(b"do-not-copy", stdin)
            if "config" in args:
                model = copy.deepcopy(current["model"])
                model["services"]["caddy"]["image"] = prepared["image_id"]
                return m.canonical(model)
            self.assertEqual(tag["image"], "sha256:" + "0" * 64)
            self.assertEqual(args[-8:], ["up", "-d", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", "caddy"])
            writes.append(json.loads(stdin)["services"]["caddy"]["image"])
            return b""

        backend.run = Mock(side_effect=run)
        backend.recreate_caddy(prepared)
        current["model"] = rollback
        backend.recreate_caddy(prepared)
        self.assertEqual(writes, [prepared["image_id"], prepared["image_id"]])

    def test_runtime_guard_is_explicit_and_bridge_edits_preserve_inode_and_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bridge = root / m.COMPOSE_FILES[-1]
            bridge.write_bytes(BRIDGE)
            bridge.chmod(0o777)
            with self.assertRaises(m.Blocked):
                m.safe_read(bridge)
            denied = Mock(return_value=False)
            with self.assertRaises(m.Blocked):
                m.safe_read(bridge, platform_guard=denied)
            denied.assert_called_once()
            guard = Mock(return_value=True)  # trusted wrapper's synthetic Drvfs/ACL proof
            self.assertEqual(m.safe_read(bridge, platform_guard=guard), BRIDGE)
            backend = object.__new__(m.NativeBackend)
            backend.root, backend.runtime_file_guard = root, guard
            info = bridge.stat()
            target = m.edit_bridge(BRIDGE, "synthetic_caddy_ingress")
            backend.atomic_bridge(BRIDGE, target)
            self.assertEqual(bridge.stat().st_ino, info.st_ino)
            self.assertEqual(bridge.stat().st_mode, info.st_mode)
            backend.atomic_bridge(target, BRIDGE)
            self.assertEqual(bridge.read_bytes(), BRIDGE)
            self.assertEqual(bridge.stat().st_ino, info.st_ino)
            self.assertEqual(bridge.stat().st_mode, info.st_mode)
            with self.assertRaises(m.Blocked):
                m.safe_read(bridge, private=True, platform_guard=guard)

    def test_actual_endpoint_route_source_and_health_are_verified_before_completion(self):
        plan, fresh, prepared = fixture()
        owned = network(prepared)
        old_id, new_id = fresh["caddy"]["Id"], "9" * 64
        observed = copy.deepcopy(fresh)
        caddy = observed["caddy"]
        caddy["Id"] = new_id
        caddy["Config"]["Hostname"] = new_id[:12]
        caddy["Config"]["Labels"].update({"com.docker.compose.version": "5.3.0", "com.docker.compose.replace": old_id})
        caddy["Config"]["Env"][0] = "PORTAL_UPSTREAM=" + prepared["portal_upstream"]
        observed["default_network"]["Containers"].pop(old_id)
        observed["default_network"]["Containers"][new_id] = {}
        caddy["NetworkSettings"]["Networks"][prepared["network_name"]] = {
            "NetworkID": owned["Id"], "IPAddress": prepared["address"]["fixed_ip"], "GwPriority": 1,
            "IPAMConfig": {"IPv4Address": prepared["address"]["fixed_ip"]}}
        owned["Containers"] = {new_id: {}}
        fake = FakeBackend(plan, fresh, prepared)
        fake.write_runtime(prepared, BRIDGE, ENV)
        observed["config"] = fake.compose_config()
        backend = object.__new__(m.NativeBackend)
        backend.guard = Mock()
        backend.snapshot = Mock(return_value=observed)
        backend.owned_network = Mock(return_value=owned)
        route = f"10.43.1.10 via {prepared['address']['gateway']} dev eth1 src {prepared['address']['fixed_ip']}\n".encode()
        backend.run = Mock(side_effect=[route, b""])
        identity = backend.verify(prepared, rollback=False)
        self.assertEqual(identity["forward_caddy_id_sha256"], m.digest(new_id.encode()))
        self.assertEqual(backend.run.call_count, 2)
        self.assertIn("wget", backend.run.call_args.args[0])
        backend.run = Mock(return_value=b"10.43.1.10 via 172.29.0.1 dev eth0 src 172.29.0.2\n")
        with self.assertRaises(m.Blocked):
            backend.verify(prepared, rollback=False)
        self.assertEqual(backend.run.call_count, 1)

    def test_exited_parent_still_requires_descendant_termination(self):
        backend = object.__new__(m.NativeBackend)
        backend.root, backend.deadline = Path("/synthetic"), None
        backend.command_termination_unknown = False
        backend.group_exists = Mock(side_effect=[True, False, False])
        child = Mock(pid=123, returncode=0, stdin=None)
        child.poll.return_value = 0
        child.communicate.return_value = (b"ok", None)
        with patch.object(m.subprocess, "Popen", return_value=child), patch.object(m.os, "killpg") as kill:
            self.assertEqual(backend.run(["synthetic", "read"]), b"ok")
        kill.assert_called_once_with(123, signal.SIGTERM)
        self.assertFalse(backend.command_termination_unknown)

    def test_surviving_descendants_mark_execution_unknown(self):
        backend = object.__new__(m.NativeBackend)
        backend.command_termination_unknown = False
        backend.group_exists = Mock(return_value=True)
        child = Mock(pid=123)
        with patch.object(m.os, "killpg") as kill, patch.object(m.time, "monotonic", side_effect=[0, 3, 4, 7]):
            backend.stop_group(child)
        self.assertEqual(kill.call_args_list[0].args, (123, signal.SIGTERM))
        self.assertEqual(kill.call_args_list[1].args, (123, signal.SIGKILL))
        self.assertTrue(backend.command_termination_unknown)
        with patch.object(m.subprocess, "Popen") as create, self.assertRaises(m.Blocked):
            backend.run(["synthetic", "readback"])
        create.assert_not_called()

    def test_interrupted_journal_write_preserves_prior_durable_intent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            path.chmod(0o700)
            journal = m.Journal(path, Path("/synthetic-repo"))
            journal.save(schema_version=1, phase="network_created", network_id_sha256="a" * 64)
            baseline = (path / "transition.json").read_bytes()
            actual_write, calls = m.os.write, []

            def interrupted(fd, raw):
                calls.append(True)
                if len(calls) == 1:
                    return actual_write(fd, raw[:5])
                raise OSError("synthetic_interrupted_write")

            with patch.object(m.os, "write", side_effect=interrupted), self.assertRaises(OSError):
                journal.save(phase="runtime_write_intent")
            self.assertEqual((path / "transition.json").read_bytes(), baseline)
            self.assertEqual(journal.state["phase"], "network_created")
            journal.close()
            reopened = m.Journal(path, Path("/synthetic-repo"))
            self.assertEqual(reopened.state["phase"], "network_created")
            reopened.close()

    def test_new_process_recovers_from_private_projection_without_credential_backup(self):
        plan, fresh, prepared = fixture()
        backend = object.__new__(m.NativeBackend)
        backend.root, backend.plan, backend._prepared = Path("/synthetic"), plan, None
        backend.runtime_bytes = Mock(return_value=(prepared["target_bridge"], ENV + prepared["env_suffix"][:20]))
        backend.run = Mock(return_value=m.canonical(fresh["config"]))
        recovered = backend.recover_prepared(m.public_record(prepared))
        self.assertEqual(recovered["prepared_sha256"], prepared["prepared_sha256"])
        self.assertEqual(recovered["baseline_config"], fresh["config"])
        self.assertEqual(backend.run.call_args.kwargs["stdin"], BRIDGE)
        self.assertNotIn(b"do-not-copy", backend.run.call_args.kwargs["stdin"])

    def native_recovery(self):
        plan, fresh, prepared = fixture()
        backend = object.__new__(m.NativeBackend)
        backend.plan, backend.command_termination_unknown = plan, False
        backend.runtime_bytes = Mock(return_value=(prepared["target_bridge"], ENV + prepared["env_suffix"]))
        backend.owned_network = Mock(return_value=network(prepared))
        backend.caddy = Mock(return_value=fresh["caddy"])
        backend.snapshot = Mock(return_value=fresh)
        state = {"network_id_sha256": m.digest(("f" * 64).encode()), "forward_recreate_attempted": True, "recreate_response_complete": True}
        return backend, fresh, prepared, state

    def test_owned_recreate_failure_allows_absent_and_stopped_caddy_recovery(self):
        backend, fresh, prepared, state = self.native_recovery()
        backend.caddy.return_value = None
        fresh["caddy"] = None
        backend.assert_recoverable(prepared, state)
        state["forward_recreate_attempted"] = False
        with self.assertRaises(m.Blocked):
            backend.assert_recoverable(prepared, state)

        backend, fresh, prepared, state = self.native_recovery()
        fresh["caddy"]["State"]["Running"] = False
        fresh["caddy"]["NetworkSettings"]["Networks"] = {}
        backend.assert_recoverable(prepared, state)
        state["forward_recreate_attempted"] = False
        with self.assertRaises(m.Blocked):
            backend.assert_recoverable(prepared, state)

    def test_unknown_daemon_write_keeps_absent_or_stopped_caddy_held(self):
        for shape in ("absent", "stopped", "original"):
            backend, fresh, prepared, state = self.native_recovery()
            state["recreate_response_unknown"] = True
            state.pop("recreate_response_complete")
            if shape == "absent":
                backend.caddy.return_value = None
                fresh["caddy"] = None
            elif shape == "stopped":
                fresh["caddy"]["State"]["Running"] = False
            with self.subTest(shape=shape), self.assertRaises(m.Blocked):
                backend.assert_recoverable(prepared, state)

    def test_crash_after_recreate_intent_without_response_record_stays_held(self):
        for phase in ("forward", "rollback"):
            for shape in ("absent", "stopped", "original"):
                backend, fresh, prepared, state = self.native_recovery()
                if phase == "forward":
                    state.pop("recreate_response_complete")
                else:
                    state["rollback_recreate_attempted"] = True
                if shape == "absent":
                    backend.caddy.return_value = None
                    fresh["caddy"] = None
                elif shape == "stopped":
                    fresh["caddy"]["State"]["Running"] = False
                with self.subTest(phase=phase, shape=shape):
                    if phase == "rollback" and shape == "original":
                        backend.verify = Mock(side_effect=m.Blocked("exact_rollback_readback_failed"))
                    with self.assertRaises(m.Blocked):
                        backend.assert_recoverable(prepared, state)

    def test_unresolved_new_running_caddy_requires_full_forward_verification(self):
        backend, fresh, prepared, state = self.native_recovery()
        state.pop("recreate_response_complete")
        fresh["caddy"]["Id"] = "9" * 64
        fresh["caddy"]["Config"]["Hostname"] = "9" * 12
        fresh["caddy"]["Config"]["Labels"]["com.docker.compose.version"] = "5.3.0"
        backend.verify = Mock(side_effect=m.Blocked("exact_forward_readback_failed"))
        with self.assertRaises(m.Blocked):
            backend.assert_recoverable(prepared, state)
        backend.verify.assert_called_once_with(prepared, rollback=False)
        backend.verify = Mock(return_value={"forward_caddy_id_sha256": m.digest(("9" * 64).encode())})
        identity = backend.assert_recoverable(prepared, state)
        self.assertTrue(identity["recreate_response_observed"])
        self.assertEqual(identity["forward_caddy_id_sha256"], m.digest(("9" * 64).encode()))

    def test_unknown_network_create_requires_observed_owned_identity_before_release(self):
        backend, fresh, prepared, state = self.native_recovery()
        state = {"network_create_attempted": True}
        backend.owned_network.return_value = None
        with self.assertRaises(m.Blocked):
            backend.assert_recoverable(prepared, state)
        backend.owned_network.return_value = network(prepared)
        identity = backend.assert_recoverable(prepared, state)
        self.assertEqual(identity["network_id_sha256"], m.digest(("f" * 64).encode()))
        self.assertEqual(identity["network_spec_sha256"], m.hash_object(m.network_spec(network(prepared))))

    def test_foreign_portal_or_third_network_is_held_before_restoration(self):
        backend, fresh, prepared, state = self.native_recovery()
        fresh["caddy"]["Config"]["Env"][0] = "PORTAL_UPSTREAM=foreign:8000"
        with self.assertRaises(m.Blocked):
            backend.assert_recoverable(prepared, state)
        backend, fresh, prepared, state = self.native_recovery()
        fresh["caddy"]["NetworkSettings"]["Networks"]["foreign"] = {"NetworkID": "0" * 64}
        with self.assertRaises(m.Blocked):
            backend.assert_recoverable(prepared, state)

    def test_only_caddy_recreate_argv_no_pull_or_build_or_down(self):
        plan, fresh, prepared = fixture()
        backend = object.__new__(m.NativeBackend)
        backend.root = Path("/synthetic")
        backend.compose_config = Mock(return_value=fresh["config"])
        effective = copy.deepcopy(fresh["config"])
        effective["services"]["caddy"]["image"] = prepared["image_id"]
        backend.run = Mock(side_effect=[m.canonical(effective), b""])
        backend.assert_image = Mock()
        backend.recreate_caddy(prepared)
        args = backend.run.call_args.args[0]
        self.assertEqual(args[-8:], ["up", "-d", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", "caddy"])
        self.assertEqual(args.count("-f"), 4)
        self.assertEqual(args[args.index("up") - 2:args.index("up")], ["-f", "-"])
        self.assertNotIn("down", args)
        self.assertEqual(json.loads(backend.run.call_args.kwargs["stdin"]),
                         {"services": {"caddy": {"image": prepared["image_id"]}}})
        backend.assert_image.assert_called_once_with(prepared["image_id"])

    def test_safe_read_refuses_symlink_wrong_owner_and_permissive_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "existing.env"
            path.write_bytes(ENV)
            path.chmod(0o600)
            self.assertEqual(m.safe_read(path, private=True), ENV)
            link = Path(folder) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                m.safe_read(link)
            path.chmod(0o666)
            with self.assertRaises(m.Blocked):
                m.safe_read(path)

    def test_durable_private_journal_roundtrip_and_repository_exclusion(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            path.chmod(0o700)
            journal = m.Journal(path, Path("/synthetic-repo"))
            journal.save(schema_version=1, phase="held", record={"env_sha256": "a" * 64, "env_size": 12})
            journal.close()
            reopened = m.Journal(path, Path("/synthetic-repo"))
            self.assertEqual(reopened.state["phase"], "held")
            reopened.close()
            self.assertEqual((path / "transition.json").stat().st_mode & 0o777, 0o600)
            with self.assertRaises(m.Blocked):
                m.Journal(path, path)

    def test_cli_default_is_check_and_mutation_requires_operational_wrapper(self):
        plan, fresh, prepared = fixture()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "plan.json"
            path.write_text(json.dumps(plan))
            path.chmod(0o600)
            backend = Mock()
            backend.snapshot.return_value = fresh
            backend.runtime_bytes.return_value = (BRIDGE, ENV)
            with patch.object(m, "NativeBackend", return_value=backend), patch("builtins.print") as output:
                self.assertEqual(m.main(["--root", folder, "--plan", str(path)]), 0)
                record = json.loads(output.call_args.args[0])
                self.assertEqual(record["prepared_sha256"], prepared["prepared_sha256"])
                self.assertEqual(record["status"], "CHECK")
                self.assertEqual(m.main(["--root", folder, "--plan", str(path), "--go"]), 2)
            backend.create_network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
