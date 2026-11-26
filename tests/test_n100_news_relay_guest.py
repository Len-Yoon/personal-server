import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "infra/k8s/tools/n100_news_relay_guest.py"
SPEC = importlib.util.spec_from_file_location("n100_news_relay_guest", SCRIPT)
relay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(relay)


class NewsRelayPolicyTest(unittest.TestCase):
    def setUp(self):
        self.config = {
            "guest": "192.0.2.10", "relay": "192.0.2.1", "interface": "ha-data",
            "boot_sha256": "0" * 64,
            "providers": {"kr.investing.com": "1.1.1.1", "news.google.com": "8.8.8.8",
                          "api.telegram.org": "9.9.9.9"},
        }

    def test_only_known_provider_endpoints_from_broker_uid(self):
        hosts, routes, rules = relay.policy(self.config)
        self.assertEqual(routes, [("1.1.1.1", 46191), ("8.8.8.8", 46192), ("9.9.9.9", 46193)])
        self.assertEqual(rules.count("meta skuid 10002 ip daddr"), 3)
        self.assertIn("meta skuid != 10002 reject", rules)
        self.assertEqual(hosts.count(b".com") + hosts.count(b".org"), 3)

    def test_unexpected_provider_and_local_or_shared_provider_address_rejected(self):
        self.config["providers"]["api.telegram.org"] = "127.0.0.1"
        with self.assertRaises(ValueError):
            relay.policy(self.config)
        self.config["providers"]["api.telegram.org"] = "8.8.8.8"
        with self.assertRaises(ValueError):
            relay.policy(self.config)
        self.config["providers"]["other.example"] = "8.8.4.4"
        with self.assertRaises(ValueError):
            relay.policy(self.config)

    def test_invalid_link_and_shell_metacharacters_rejected(self):
        self.config["interface"] = "ha-data; ip"
        with self.assertRaises(ValueError):
            relay.policy(self.config)
        self.config["interface"] = "ha-data"
        self.config["relay"] = self.config["guest"]
        with self.assertRaises(ValueError):
            relay.policy(self.config)

    def guard_json(self):
        rows = [{"metainfo": {"json_schema_version": 1}},
                {"table": {"family": "ip", "name": "n100_news_relay_guard", "handle": 9}}]
        for name, hook in (("deny_direct_relay", "output"), ("deny_forwarded_relay", "forward")):
            rows.append({"chain": {"family": "ip", "table": "n100_news_relay_guard",
                                   "name": name, "handle": 1, "type": "filter",
                                   "hook": hook, "prio": -110, "policy": "accept"}})
            for index, port in enumerate((46191, 46192, 46193)):
                rows.append({"rule": {
                    "family": "ip", "table": "n100_news_relay_guard", "chain": name,
                    "handle": index + 2,
                    "expr": [
                        {"match": {"op": "==", "left": {"payload": {"protocol": "ip", "field": "daddr"}},
                                   "right": "192.0.2.1"}},
                        {"match": {"op": "==", "left": {"payload": {"protocol": "tcp", "field": "dport"}},
                                   "right": port}},
                        {"reject": {"type": "tcp reset"}},
                    ],
                }})
        return {"nftables": rows}

    def test_guard_precedes_dnat_and_denies_only_exact_configured_relay_ports(self):
        script = relay.guard_policy(self.config)
        self.assertIn("hook output priority -110", script)
        self.assertIn("hook forward priority -110", script)
        self.assertEqual(script.count("reject with tcp reset"), 6)
        for port in (46191, 46192, 46193):
            self.assertEqual(script.count(f"ip daddr 192.0.2.1 tcp dport {port} reject"), 2)
        for unexpected in ("meta skuid", "dnat", "1.1.1.1", "flush", "input"):
            self.assertNotIn(unexpected, script)

    def test_guard_install_is_atomic_and_idempotent_without_boot_or_network_reads(self):
        present = SimpleNamespace(returncode=0, stdout=json.dumps(self.guard_json()))
        absent = SimpleNamespace(returncode=1, stdout="")
        inventory = SimpleNamespace(returncode=0, stdout='{"nftables": []}')
        with patch.object(relay, "run", side_effect=[absent, inventory, present, present, present]) as run, \
                patch.object(relay, "route_state", side_effect=AssertionError("network read")), \
                patch.object(relay, "read_hosts", side_effect=AssertionError("hosts read")):
            relay.apply_guard(self.config)
            self.assertEqual(run.call_count, 5)
            check_script = run.call_args_list[2].kwargs["input_text"]
            self.assertTrue(check_script.startswith("create table ip n100_news_relay_guard\n"))
            self.assertEqual(check_script, run.call_args_list[3].kwargs["input_text"])
        with patch.object(relay, "run", return_value=present) as run:
            relay.apply_guard(self.config)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][:3], ("nft", "-j", "list"))

    def test_guard_refuses_any_changed_or_additional_owned_table_content(self):
        good = self.guard_json()
        mutations = []
        for field, value in (("prio", -90), ("hook", "forward"), ("policy", "drop")):
            changed = copy.deepcopy(good)
            changed["nftables"][2]["chain"][field] = value
            mutations.append(changed)
        changed = copy.deepcopy(good)
        changed["nftables"][3]["rule"]["expr"][0]["match"]["right"] = "192.0.2.2"
        mutations.append(changed)
        changed = copy.deepcopy(good)
        changed["nftables"].append(copy.deepcopy(changed["nftables"][3]))
        mutations.extend((changed, {"nftables": good["nftables"][:-1]}))
        for changed in mutations:
            with self.subTest(changed=changed), patch.object(relay, "run", return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps(changed))) as run:
                with self.assertRaisesRegex(RuntimeError, "guard_unexpected"):
                    relay.apply_guard(self.config)
                self.assertEqual(run.call_count, 1)

    def test_guard_accepts_equivalent_nft_enumeration_order_only(self):
        value = self.guard_json()
        value["nftables"] = list(reversed(value["nftables"]))
        with patch.object(relay, "run", return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(value))):
            self.assertTrue(relay.guard_present(self.config))

    def test_failed_guard_query_requires_successful_inventory_proof_of_absence(self):
        failed = SimpleNamespace(returncode=1, stdout="")
        present = SimpleNamespace(returncode=0, stdout=json.dumps({"nftables": [
            {"table": {"family": "ip", "name": "n100_news_relay_guard"}}]}))
        for inventory in (RuntimeError("inventory_failed"), present):
            with self.subTest(inventory=inventory), patch.object(relay, "run", side_effect=[failed, inventory]) as run:
                with self.assertRaises(RuntimeError):
                    relay.apply_guard(self.config)
                self.assertEqual(run.call_count, 2)

    def test_stale_hosts_cannot_reconcile_when_nft_or_route_inventory_fails(self):
        block, _, _ = relay.policy(self.config)
        self.config["boot_sha256"] = hashlib.sha256(b"boot").hexdigest()
        route_inventory = ("ip", "-N", "-j", "-4", "route", "show", "table", "all")
        nft_inventory = ("nft", "-j", "list", "tables")
        for failure in (route_inventory, nft_inventory):
            calls = []

            def command(command, **kwargs):
                calls.append((command, kwargs))
                if command == failure:
                    raise RuntimeError("injected_inventory_failure")
                data = []
                if command == ("ip", "-j", "-4", "route", "get", self.config["relay"]):
                    data = [{"dev": "ha-data", "prefsrc": "192.0.2.10"}]
                return SimpleNamespace(returncode=0, stdout=json.dumps(data))

            for action in (relay.apply, relay.remove):
                with self.subTest(failure=failure, action=action.__name__), patch.object(relay, "apply_guard"), \
                        patch.object(relay, "BOOT_ID", SimpleNamespace(read_bytes=lambda: b"boot")), \
                        patch.object(relay, "read_hosts", return_value=(block, 0o644)), \
                        patch.object(relay, "write_hosts") as write, patch.object(relay, "run", side_effect=command):
                    with self.assertRaisesRegex(RuntimeError, "injected_inventory_failure"):
                        action(self.config)
                    write.assert_not_called()
                    self.assertTrue(all(kwargs.get("check", True) for _, kwargs in calls))

    def test_route_state_uses_exact_numeric_table_from_successful_inventories(self):
        block, _, _ = relay.policy(self.config)
        self.config["boot_sha256"] = hashlib.sha256(b"boot").hexdigest()
        for table, nft_name, expected in (("18461", relay.NAME, (True, False, True, True)),
                                          ("254", "unrelated", (True, False, False, False))):
            def command(command, **kwargs):
                data = []
                if command == ("ip", "-j", "-4", "route", "get", self.config["relay"]):
                    data = [{"dev": "ha-data", "prefsrc": "192.0.2.10"}]
                elif command == ("ip", "-N", "-j", "-4", "route", "show", "table", "all"):
                    data = [{"table": table, "dst": "1.1.1.1"}]
                elif command == ("nft", "-j", "list", "tables"):
                    data = {"nftables": [{"table": {"family": "ip", "name": nft_name}}]}
                return SimpleNamespace(returncode=0, stdout=json.dumps(data))

            with self.subTest(table=table), patch.object(relay, "run", side_effect=command), \
                    patch.object(relay, "BOOT_ID", SimpleNamespace(read_bytes=lambda: b"boot")), \
                    patch.object(relay, "read_hosts", return_value=(block, 0o644)):
                self.assertEqual(relay.route_state(self.config, block)[2], expected)

    def test_guard_cli_requires_root_and_skips_boot_network_and_hosts_state(self):
        for uid in (0, 1000):
            with self.subTest(uid=uid), patch.object(relay.sys, "argv", ["relay", "guard"]), \
                    patch.object(relay.sys, "stdin", io.StringIO(json.dumps(self.config))), \
                    patch.object(relay.sys, "stdout", io.StringIO()) as output, \
                    patch.object(relay.sys, "stderr", io.StringIO()), \
                    patch.object(relay.os, "geteuid", return_value=uid), \
                    patch.object(relay, "apply_guard") as guard, \
                    patch.object(relay, "route_state", side_effect=AssertionError("network read")):
                if uid:
                    with self.assertRaises(SystemExit):
                        relay.main()
                    guard.assert_not_called()
                else:
                    relay.main()
                    guard.assert_called_once_with(self.config)
                    self.assertEqual(json.loads(output.getvalue()), {"relay_deny_guard": True})

    def test_apply_reconciles_only_exact_stale_hosts_after_guard(self):
        block, _, _ = relay.policy(self.config)
        base = b"127.0.0.1 localhost\n"
        events = []
        with patch.object(relay, "apply_guard", side_effect=lambda config: events.append("guard")), \
                patch.object(relay, "route_state", side_effect=[
                    (base + block, 0o644, (True, False, False, False)),
                    (base + block, 0o644, (True, True, True, True))]), \
                patch.object(relay, "run", side_effect=lambda *a, **k: events.append("run")), \
                patch.object(relay, "write_hosts") as write:
            relay.apply(self.config)
        self.assertEqual(events[0], "guard")
        self.assertEqual(write.call_args_list[0].args, (base + block, base, 0o644))
        self.assertEqual(write.call_args_list[1].args, (base, base + block, 0o644))

    def test_stale_remove_preserves_guard_and_never_changes_network(self):
        block, _, _ = relay.policy(self.config)
        base = b"127.0.0.1 localhost\n"
        with patch.object(relay, "apply_guard") as guard, patch.object(relay, "route_state", side_effect=[
                (base + block, 0o644, (True, False, False, False)),
                (base, 0o644, (False, False, False, False))]), \
                patch.object(relay, "write_hosts") as write, patch.object(relay, "run") as run:
            relay.remove(self.config)
        guard.assert_called_once_with(self.config)
        write.assert_called_once_with(base + block, base, 0o644)
        run.assert_not_called()

    def test_apply_and_remove_refuse_changed_duplicate_and_partial_stale_markers(self):
        block, _, _ = relay.policy(self.config)
        for hosts in (block + block, block + relay.BEGIN, b"unexpected" + block,
                      block.replace(b"1.1.1.1", b"1.0.0.1")):
            state = (block in hosts, False, False, False)
            for action in (relay.apply, relay.remove):
                with self.subTest(hosts=hosts, action=action.__name__), \
                        patch.object(relay, "apply_guard"), \
                        patch.object(relay, "route_state", return_value=(hosts, 0o644, state)), \
                        patch.object(relay, "write_hosts") as write, patch.object(relay, "run") as run:
                    with self.assertRaises(RuntimeError):
                        action(self.config)
                    write.assert_not_called()
                    run.assert_not_called()

    def test_remove_volatile_allow_never_deletes_deny_guard(self):
        block, _, _ = relay.policy(self.config)
        with patch.object(relay, "apply_guard"), patch.object(relay, "route_state", side_effect=[
                (block, 0o644, (True, True, True, True)),
                (b"", 0o644, (False, False, False, False))]), \
                patch.object(relay, "write_hosts"), patch.object(relay, "run") as run:
            relay.remove(self.config)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(("nft", "delete", "table", "ip", "n100_news_relay"), commands)
        self.assertTrue(all("n100_news_relay_guard" not in command for command in commands))

    def test_apply_failure_retains_deny_guard(self):
        def fail_route(command, **kwargs):
            if command[:4] == ("ip", "-4", "route", "add"):
                raise RuntimeError("injected")

        with patch.object(relay, "apply_guard"), \
                patch.object(relay, "route_state", return_value=(b"", 0o644, (False,) * 4)), \
                patch.object(relay, "read_hosts", return_value=(b"", 0o644)), \
                patch.object(relay, "run", side_effect=fail_route) as run:
            with self.assertRaisesRegex(RuntimeError, "injected"):
                relay.apply(self.config)
        self.assertTrue(all("n100_news_relay_guard" not in call.args[0] for call in run.call_args_list))


if __name__ == "__main__":
    unittest.main()
