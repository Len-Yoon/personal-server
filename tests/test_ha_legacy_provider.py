"""Exact guest-provider retirement and one-shot rollback; no host mutations."""

from contextlib import ExitStack
from pathlib import Path
import json
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/k8s"))
from ha_control import native_legacy_provider as module
from ha_control import native_stdio_bridge as wire
from ha_control.legacy_writers import TransitionBlocked


FRAGMENT = b"""# transient observer fixture
[Unit]
Description=original observer
CollectMode=inactive-or-failed
[Service]
User=root
Restart=no
PrivateTmp=yes
NoNewPrivileges=yes
ExecStart=
ExecStart=/usr/bin/python3 -I -B -S -c fixture
"""
HOLD = "8" * 64
BOOT = wire._digest(b"boot")
SOURCE = "/var/lib/n100-ha/cutover/recovery-source-fixture/native-guest-recovery-observer.py"
SOURCE_BYTES = b"# protected original observer fixture\n"


def argv_bytes(role):
    tail = [SOURCE, wire._digest(SOURCE_BYTES), "850" if role == "provider" else "851", HOLD, BOOT]
    if role == "held":
        tail += ["9" * 64, "a" * 64]
    return ("\0".join(["/usr/bin/python3", "-I", "-B", "-S", "-c", "value = '$test %i'", *tail]) + "\0").encode()


ARGV = argv_bytes("provider")


def read_fixture(path, *_args, **_kwargs):
    return SOURCE_BYTES if str(path) == SOURCE else path.read_bytes()


def entry(role):
    return {"unit": "n100ha-" + ("recovery" if role == "provider" else "postboot") + "-observer-fixture.service",
            "fragment": FRAGMENT.hex(), "argv": argv_bytes(role).hex(),
            "fragment_sha256": wire._digest(FRAGMENT), "argv_sha256": wire._digest(argv_bytes(role)),
            "scope": "exact-original-observer-profile"}


class LegacyProviderTest(unittest.TestCase):
    def adapter(self, root):
        binding = wire.Binding("1" * 64, "2" * 64, "3" * 64, wire._digest(b"boot"), "5" * 64)
        return module.ProviderRetirement(root, binding, latest_intent_sha256="6" * 64,
                                        rollback_sha256="7" * 64, hold_sha256=HOLD, provider_port=850, held_port=851)

    def state(self, adapter):
        """Only the systemd/process boundary is fake; journal files are real."""
        stack = ExitStack()
        self.addCleanup(stack.close)
        units = {role: entry(role) for role in ("provider", "held")}
        state = {"provider": True, "held": True, "uncertain": False, "held_pid": 42, "provider_pid": 41}

        def live(_entry, role, *, original=True):
            if not state[role]:
                raise TransitionBlocked(module.ERROR)
            return {"pid": state[role + "_pid"], "fragment_sha256": _entry["fragment_sha256"],
                    "argv_sha256": _entry["argv_sha256"]}

        def absent(_entry):
            if state["provider"]:
                raise TransitionBlocked(module.UNSETTLED)

        def command(argv):
            if state["uncertain"]:
                raise subprocess.TimeoutExpired("observer-command", 8)
            if argv[0] == "/usr/bin/systemctl":
                self.assertEqual(argv, ("/usr/bin/systemctl", "stop", units["provider"]["unit"]))
                state["provider"] = False
            else:
                self.assertEqual(argv[0], "/usr/bin/systemd-run")
                self.assertIn("--unit=" + units["provider"]["unit"], argv)
                self.assertIn("--expand-environment=no", argv)
                self.assertEqual(list(argv[argv.index("--") + 1:]),
                    module._argv(ARGV, role="provider", port=850, hold_sha256=HOLD, boot_sha256=BOOT))
                state["provider"] = True
                state["provider_pid"] = 51
            return b""

        stack.enter_context(patch.object(adapter, "_archive", return_value=units))
        stack.enter_context(patch.object(adapter, "_live", side_effect=live))
        stack.enter_context(patch.object(adapter, "_absent", side_effect=absent))
        stack.enter_context(patch.object(module.runtime, "_read", side_effect=read_fixture))
        dispatch = stack.enter_context(patch.object(module, "_command", side_effect=command))
        return state, dispatch

    def test_retirement_targets_only_provider_and_restoration_needs_new_proofs(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = self.adapter(root)
            state, dispatch = self.state(adapter)
            retire_gate = Mock(return_value="a" * 64)
            retired = adapter.retire(retire_gate)
            self.assertEqual(retired.status, "PASS")
            self.assertFalse(state["provider"])
            self.assertTrue(state["held"])
            self.assertEqual([call.args[1] for call in retire_gate.call_args_list], ["before", "dispatch", "after"])
            self.assertEqual(adapter.retire(retire_gate), retired)
            self.assertEqual(dispatch.call_count, 1)
            denied = Mock(side_effect=TransitionBlocked("successor_writer_started"))
            with self.assertRaisesRegex(TransitionBlocked, "successor_writer_started"):
                adapter.restore(denied)
            self.assertEqual(dispatch.call_count, 1)
            restored = adapter.restore(Mock(return_value="b" * 64))
            self.assertEqual(restored.status, "PASS")
            self.assertTrue(state["provider"])
            self.assertTrue(state["held"])
            self.assertEqual(adapter.restore(Mock(return_value="c" * 64)), restored)
            self.assertEqual(dispatch.call_count, 2)
            # A completed restore is terminal for this retirement identifier.
            with self.assertRaises(TransitionBlocked):
                adapter.retire(Mock(return_value="a" * 64))
            self.assertEqual(dispatch.call_count, 2)

    def test_uncertain_stop_is_not_replayed_and_later_absence_is_reconciled(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = self.adapter(root)
            state, dispatch = self.state(adapter)
            state["uncertain"] = True
            for _ in range(2):
                with self.assertRaisesRegex(TransitionBlocked, module.UNSETTLED):
                    adapter.retire(Mock(return_value="a" * 64))
            self.assertEqual(dispatch.call_count, 1)
            self.assertFalse(adapter._paths("retire")[1].exists())
            state["provider"] = False
            self.assertEqual(adapter.retire(Mock(return_value="a" * 64)).status, "PASS")
            self.assertEqual(dispatch.call_count, 1)

    def test_uncertain_restore_is_not_replayed_and_must_observe_same_provider(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = self.adapter(root)
            state, dispatch = self.state(adapter)
            adapter.retire(Mock(return_value="a" * 64))
            state["uncertain"] = True
            for _ in range(2):
                with self.assertRaises(TransitionBlocked):
                    adapter.restore(Mock(return_value="a" * 64))
            self.assertEqual(dispatch.call_count, 2)
            state["provider"] = True
            state["provider_pid"] = 51
            adapter.restore(Mock(return_value="a" * 64))
            self.assertEqual(dispatch.call_count, 2)
            state["provider_pid"] = 52
            with self.assertRaises(TransitionBlocked):
                adapter.restore(Mock(return_value="a" * 64))

    def test_held_restart_blocks_reconciliation_and_provider_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = self.adapter(root)
            state, dispatch = self.state(adapter)
            state["uncertain"] = True
            with self.assertRaises(TransitionBlocked):
                adapter.retire(Mock(return_value="a" * 64))
            state["provider"] = False
            state["held_pid"] += 1
            with self.assertRaises(TransitionBlocked):
                adapter.retire(Mock(return_value="a" * 64))
            self.assertEqual(dispatch.call_count, 1)

    def test_restore_without_retirement_receipt_never_dispatches(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = self.adapter(root)
            _, dispatch = self.state(adapter)
            with self.assertRaises(FileNotFoundError):
                adapter.restore(Mock(return_value="a" * 64))
            dispatch.assert_not_called()

    def test_no_dispatch_when_closed_proof_is_missing_or_held_is_absent(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = self.adapter(root)
            state, dispatch = self.state(adapter)
            with self.assertRaises(TransitionBlocked):
                adapter.retire(Mock(return_value=None))
            state["held"] = False
            with self.assertRaises(TransitionBlocked):
                adapter.retire(Mock(return_value="a" * 64))
            dispatch.assert_not_called()
            self.assertFalse(adapter._paths("retire")[0].exists())

    def test_archive_is_hash_binding_role_and_boot_scoped(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = self.adapter(root)
            document = {"scope": adapter.scope, "latest_intent": adapter.intent,
                        "units": {role: entry(role) for role in ("provider", "held")}}
            raw = wire._bytes(document)
            adapter.rollback = wire._digest(raw)
            archive = Path(root) / ("provider-rollback-" + adapter.intent + ".json")
            archive.write_bytes(raw)
            with (patch.object(module.os, "geteuid", return_value=0),
                  patch.object(module.runtime, "BOOT_ID") as boot,
                  patch.object(module.runtime, "_directory"),
                  patch.object(module.runtime, "_read", side_effect=read_fixture)):
                boot.read_bytes.return_value = b"boot"
                self.assertEqual(adapter._archive(), document["units"])
                for mutation in (lambda row: row.update(scope={}),
                                 lambda row: row["units"]["provider"].update(unit="other-writer.service"),
                                 lambda row: row["units"]["provider"].update(argv_sha256="f" * 64)):
                    row = json.loads(raw)
                    mutation(row)
                    changed = wire._bytes(row)
                    adapter.rollback = wire._digest(changed)
                    archive.write_bytes(changed)
                    with self.assertRaises(TransitionBlocked):
                        adapter._archive()
                archive.write_bytes(raw)
                adapter.rollback = wire._digest(raw)
                boot.read_bytes.return_value = b"another-boot"
                with self.assertRaises(TransitionBlocked):
                    adapter._archive()

    def test_restore_profile_rejects_dependencies_environment_and_stop_hooks(self):
        self.assertTrue(module._profile(FRAGMENT))
        for line in (b"ExecStop=/bin/true", b"ExecStopPost=/bin/true", b"ExecStartPre=/bin/true",
                     b"ExecStartPost=/bin/true", b"Environment=NEW=VALUE", b"PartOf=protected.service",
                     b"Restart=always", b"PrivateTmp=no", b"RootDirectory=/other"):
            with self.subTest(line=line), self.assertRaises(TransitionBlocked):
                module._profile(FRAGMENT + line + b"\n")
        for raw in (ARGV.replace(b"-S\0", b""), ARGV + b"extra\0", ARGV[:-1]):
            with self.assertRaises(TransitionBlocked):
                module._argv(raw, role="provider", port=850, hold_sha256=HOLD, boot_sha256=BOOT)

    def test_real_archived_tail_shapes_bind_source_hash_port_hold_and_boot(self):
        with patch.object(module.runtime, "_read", side_effect=read_fixture) as read:
            for role, port in (("provider", 850), ("held", 851)):
                raw = argv_bytes(role)
                options = dict(role=role, port=port, hold_sha256=HOLD, boot_sha256=BOOT)
                parsed = module._argv(raw, **options)
                self.assertEqual(parsed[8:11], [str(port), HOLD, BOOT])
                read.assert_called_with(Path(SOURCE), 0, {0o400}, limit=131072)
                for index, replacement in ((0, "/bin/sh"), (6, "/tmp/native-guest-recovery-observer.py"),
                        (6, SOURCE.replace("native-guest-recovery-observer.py", "other.py")),
                        (7, "f" * 64), (8, "852"), (9, "b" * 64), (10, "c" * 64)):
                    values = list(parsed)
                    values[index] = replacement
                    with self.subTest(role=role, index=index), self.assertRaises(TransitionBlocked):
                        module._argv(("\0".join(values) + "\0").encode(), **options)
                for values in (parsed[:6], parsed + ["extra"], parsed[:-1]):
                    with self.assertRaises(TransitionBlocked):
                        module._argv(("\0".join(values) + "\0").encode(), **options)
            with self.assertRaises(TransitionBlocked):
                module._argv(argv_bytes("held"), role="provider", port=850, hold_sha256=HOLD, boot_sha256=BOOT)

    def test_live_checks_exact_proc_socket_and_no_stop_propagation(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = self.adapter(root)
            record = entry("provider")
            properties = {name: "" for name in module.PROPERTIES}
            properties.update(Id=record["unit"], MainPID="41", ActiveState="active", SubState="running",
                              FragmentPath=str(module.TRANSIENT / record["unit"]), Transient="yes", Restart="no",
                              NoNewPrivileges="yes", PrivateTmp="yes", User="root")
            fake_proc = Mock()
            process = fake_proc.__truediv__ = Mock(return_value=Mock())
            process = process.return_value
            process.stat.return_value = SimpleNamespace(st_uid=0)
            cmdline, executable = Mock(), Mock()
            cmdline.read_bytes.return_value = ARGV
            executable.resolve.return_value = Path("/usr/bin/python3").resolve()
            process.__truediv__ = Mock(side_effect=lambda name: {"cmdline": cmdline, "exe": executable}[name])
            real_path = Path
            with (patch.object(module, "_properties", return_value=properties),
                  patch.object(module, "Path", side_effect=lambda value: fake_proc if value == "/proc" else real_path(value)),
                  patch.object(module.runtime, "_read", return_value=FRAGMENT),
                  patch.object(module, "_listeners", return_value={850: ['LISTEN 0 128 *:850 *:* users:(("python3",pid=41,fd=3))']}) as listeners):
                self.assertEqual(adapter._live(record, "provider")["pid"], 41)
                properties["BoundBy"] = "protected.service"
                with self.assertRaises(TransitionBlocked):
                    adapter._live(record, "provider")
                properties["BoundBy"] = ""
                for hook in module.EXEC_HOOKS:
                    properties[hook] = "{ path=/bin/true ; argv[]=/bin/true ; }"
                    with self.subTest(hook=hook), self.assertRaises(TransitionBlocked):
                        adapter._live(record, "provider")
                    properties[hook] = ""
                listeners.return_value = {850: ["LISTEN 0 128 *:850 *:* pid=41,pid=42,"]}
                with self.assertRaises(TransitionBlocked):
                    adapter._live(record, "provider")
                listeners.return_value = {850: ["LISTEN 0 128 *:850 *:* pid=41,"], 852: ["LISTEN 0 128 *:852 *:* pid=41,"]}
                with self.assertRaises(TransitionBlocked):
                    adapter._live(record, "provider")

    def test_property_parser_accepts_only_undefined_exec_arrays_and_requires_other_fields(self):
        properties = {name: "" for name in module.PROPERTIES}
        properties.update(Id=entry("provider")["unit"], MainPID="41", ActiveState="active")
        raw = "\n".join(name + "=" + value for name, value in properties.items()).encode() + b"\n"
        with patch.object(module, "_command", return_value=raw) as command:
            self.assertEqual(module._properties(properties["Id"]), properties)
            self.assertIn("--all", command.call_args.args[0])
            absent_hooks = raw
            for hook in module.EXEC_HOOKS:
                absent_hooks = absent_hooks.replace((hook + "=\n").encode(), b"")
            command.return_value = absent_hooks
            self.assertEqual(module._properties(properties["Id"]), properties)
            for mandatory in set(module.PROPERTIES) - module.EXEC_HOOKS:
                command.return_value = absent_hooks.replace(
                    (mandatory + "=" + properties[mandatory] + "\n").encode(), b"")
                with self.subTest(mandatory=mandatory), self.assertRaises(TransitionBlocked):
                    module._properties(properties["Id"])
            command.return_value = raw + b"ExecStopPost=\n"
            with self.assertRaises(TransitionBlocked):
                module._properties(properties["Id"])

    def test_read_only_cli_requires_pinned_root_config_and_has_no_mutation_mode(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = self.adapter(root)
            config = {"binding": adapter.scope, "latest_intent_sha256": adapter.intent,
                      "rollback_sha256": adapter.rollback, "hold_sha256": HOLD, "provider_port": 850, "held_port": 851}
            raw = wire._bytes(config)
            source = Path(root) / "config.json"
            source.write_bytes(raw)
            observer = Mock(return_value={"rollback_sha256": adapter.rollback})
            from ha_control import native_guest_worker as worker
            with (patch.object(module.os, "geteuid", return_value=0),
                  patch.object(module.runtime, "_read", return_value=raw),
                  patch.object(module.ProviderRetirement, "observe", observer),
                  patch.object(worker, "_guest_mutation_descriptor") as lock,
                  patch("builtins.print") as output):
                self.assertEqual(module.main(["observe", "--config", str(source),
                                              "--config-sha256", wire._digest(raw)]), 0)
                observer.assert_called_once()
                lock.assert_called_once()
                result = json.loads(output.call_args.args[0])
                self.assertEqual(set(result), {"status", "rollback_sha256", "observation_sha256"})
                self.assertEqual(result["status"], "OBSERVED")
                with self.assertRaises(TransitionBlocked):
                    module.main(["observe", "--config", str(source), "--config-sha256", "f" * 64])
                with patch("sys.stderr"), self.assertRaises(SystemExit):
                    module.main(["restore", "--config", str(source), "--config-sha256", wire._digest(raw)])


if __name__ == "__main__":
    unittest.main()
