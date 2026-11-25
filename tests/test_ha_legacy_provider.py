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


class SamebootReleaseTest(unittest.TestCase):
    def setUp(self):
        from ha_control import native_sameboot_release as release
        self.release = release

    def pair(self):
        release = self.release
        files = {name: {"filename": name, "bytes": 10, "sha256": "7" * 64} for name in (
            "python/python.exe", "controller/files/ha_control/cli.py",
            "controller/files/ha_control/initial_pg_lease.py")}
        raw = wire._bytes({"files": files})
        config = {"plan": r"C:\ProgramData\fixture\plan.json", "plan_sha256": "1" * 64,
                  "binding": r"C:\ProgramData\fixture\binding.json", "binding_sha256": "2" * 64,
                  "runtime": r"C:\ProgramData\fixture\runtime", "python_sha256": "7" * 64,
                  "boot_sha256": "8" * 64, "revision": 14}
        return release.WindowsPairClosure(Mock(), config, runtime_manifest=raw,
                                          runtime_manifest_sha256=wire._digest(raw))

    def coordinator(self):
        from ha_control.sameboot_transition import SCOPE_FIELDS
        pair = self.pair()
        scope = {key: "a" * 64 for key in SCOPE_FIELDS}
        scope.update(previous_revision=14, previous_plan_sha256="1" * 64,
                     previous_instance_sha256="2" * 64)
        guard = Mock(return_value=True)
        guest = Mock()
        retired = {"retirement_sha256": "b" * 64, "held_observation_sha256": "c" * 64,
                   "rollback_sha256": "d" * 64}
        guest.retire.return_value = retired
        guest.observe_retired.return_value = retired
        driver = self.release.NativeSamebootRelease(scope, guest, pair, guard)
        request = {"scope": scope, "stage": "retire_provider", "nonce": "e" * 64,
                   "previous_sha256": "f" * 64, "plan_material_sha256": None}
        receipts = {"preflight": {"rollback_sha256": "d" * 64}}
        closed = {"closed_nodes": ["A", "B"], "synchronous": True, "same_expiry": True,
                  "replay_verified": True, "effects_isolated": True, "revision": 15,
                  "terminal_sha256": "3" * 64, "closed_proof_sha256": "4" * 64}
        pair.snapshot = Mock(side_effect=[{"status": "RUNNING"}, {"status": "STOPPED", "terminal_sha256": "3" * 64},
            {"status": "STOPPED", "disabled": True, "terminal_sha256": "3" * 64}])
        pair.disable_stopped = Mock()
        pair.closed_pair = Mock(return_value=closed)
        return driver, request, receipts

    def test_release_disables_only_after_native_stop_and_reobserves_closed_pair(self):
        import time
        driver, request, receipts = self.coordinator()
        result = driver.apply(self.release._encoded(request), self.release._encoded(receipts), time.monotonic() + 30)
        self.assertEqual(result["evidence"]["terminal_sha256"], "3" * 64)
        self.assertTrue(result["evidence"]["old_service_stopped_disabled"])
        driver.guest.retire.assert_called_once()
        driver.guest.observe_retired.assert_called_once()
        driver.pair.disable_stopped.assert_called_once()
        driver.pair.closed_pair.assert_called_once()
        self.assertEqual(driver.pair.snapshot.call_count, 3)

    def test_reconciliation_never_reissues_retirement_or_scm_mutation(self):
        import time
        driver, request, receipts = self.coordinator()
        driver.pair.snapshot.side_effect = None
        driver.pair.snapshot.return_value = {"status": "STOPPED", "disabled": True, "terminal_sha256": "3" * 64}
        driver.observe(self.release._encoded(request), self.release._encoded(receipts), time.monotonic() + 30)
        driver.guest.retire.assert_not_called()
        driver.pair.disable_stopped.assert_not_called()
        request["stage"] = "close_pair"
        receipts["retire_provider"] = {"terminal_sha256": "9" * 64}
        with self.assertRaises(TransitionBlocked):
            driver.observe(self.release._encoded(request), self.release._encoded(receipts), time.monotonic() + 30)
        self.assertEqual(driver.pair.closed_pair.call_count, 1)

    def test_changed_package_or_guard_denial_fails_before_guest_mutation(self):
        import time
        driver, request, receipts = self.coordinator()
        driver.pair.snapshot.side_effect = TransitionBlocked("runtime_changed")
        with self.assertRaisesRegex(TransitionBlocked, "runtime_changed"):
            driver.apply(self.release._encoded(request), self.release._encoded(receipts), time.monotonic() + 30)
        driver.guest.retire.assert_not_called()
        driver.pair.disable_stopped.assert_not_called()
        driver.custody_guard.return_value = False
        driver.pair.snapshot.reset_mock()
        with self.assertRaises(TransitionBlocked):
            driver.apply(self.release._encoded(request), self.release._encoded(receipts), time.monotonic() + 30)
        driver.pair.snapshot.assert_not_called()

    def test_pinned_natural_stop_needs_exact_native_terminal_and_pair_before_retirement(self):
        import time
        driver, request, receipts = self.coordinator()
        driver.expected_stopped_terminal_sha256 = "3" * 64
        driver.pair.closed_pair_before_disable = Mock()
        driver.pair.snapshot.side_effect = [
            {"status": "STOPPED", "terminal_sha256": "3" * 64},
            {"status": "STOPPED", "terminal_sha256": "3" * 64},
            {"status": "STOPPED", "disabled": True, "terminal_sha256": "3" * 64}]
        driver.retire_provider(request, receipts, time.monotonic() + 30)
        driver.pair.closed_pair_before_disable.assert_called_once()
        driver.guest.retire.assert_called_once()
        other, request, receipts = self.coordinator()
        other.expected_stopped_terminal_sha256 = "9" * 64
        other.pair.snapshot.side_effect = None
        other.pair.snapshot.return_value = {"status": "STOPPED", "terminal_sha256": "3" * 64}
        with self.assertRaises(TransitionBlocked):
            other.retire_provider(request, receipts, time.monotonic() + 30)
        other.guest.retire.assert_not_called()
        other.pair.disable_stopped.assert_not_called()

    def test_later_prearm_guard_does_not_demand_absent_replaced_provider_port(self):
        import time
        driver, request, receipts = self.coordinator()
        request["stage"] = "carryover"
        receipts.update(retire_provider={"terminal_sha256": "3" * 64}, close_pair={"terminal_sha256": "3" * 64})
        result = driver.observe_closed(request, receipts, time.monotonic() + 30)
        self.assertEqual(result["revision"], 15)
        driver.guest.retire.assert_not_called()
        driver.guest.observe_retired.assert_not_called()
        driver.pair.snapshot.assert_not_called()

    def test_guest_reconciliation_checks_preserved_held_and_writes_nothing(self):
        fixture = LegacyProviderTest()
        self.addCleanup(fixture.doCleanups)
        with tempfile.TemporaryDirectory() as root:
            provider = fixture.adapter(root)
            state, dispatch = fixture.state(provider)
            provider.retire(Mock(return_value="a" * 64))
            # An uncertain response can lose the done receipt; protected intent
            # and direct absence/preserved-held facts are sufficient to observe.
            provider._paths("retire")[1].unlink()
            with patch.object(module.runtime, "_write") as write:
                result = self.release.observe_guest_retired(provider, Mock(return_value="a" * 64))
                self.assertEqual(result["rollback_sha256"], provider.rollback)
                write.assert_not_called()
            self.assertEqual(dispatch.call_count, 1)
            state["held_pid"] = 99
            with self.assertRaises(TransitionBlocked):
                self.release.observe_guest_retired(provider, Mock(return_value="a" * 64))
            self.assertEqual(dispatch.call_count, 1)

    def test_windows_stopped_journal_retains_failure_after_local_revocation(self):
        source = self.release.WINDOWS_BODY
        start = source.index("if($record.phase -cnotin")
        end = source.index("if($cfg.mode -ceq 'snapshot')", start)
        validator = source[start:end]
        script = r'''
$ErrorActionPreference='Stop'
$record=[pscustomobject]@{activation_intent_sha256='a';attempt_id='b';controller_plan_sha256='c';
    generation=1;kind='initial-wsl-cutover-postboot-resume';legacy_plan_sha256='d';
    phase='local_revoke_observed';postboot=@{};recovery=@{};restore_receipt_sha256='e';
    revision=16;schema_version=1;sequence=2;failure_code='legacy_pipe_response_timeout_postcommit'}
function Test-Terminal { VALIDATOR }
Test-Terminal
$record.phase='lease_lost'
Test-Terminal
foreach($malformed in @('0invalid',"valid_code`n",'',12)){
    $record.failure_code=$malformed
    $accepted=$false
    try {Test-Terminal;$accepted=$true} catch {if($_.Exception.Message -cne 'release_terminal'){throw}}
    if($accepted){throw 'accepted_malformed_failure_code'}
}
$record.phase='serving'
$record.failure_code='valid_code'
$accepted=$false
try {Test-Terminal;$accepted=$true} catch {if($_.Exception.Message -cne 'release_terminal'){throw}}
if($accepted){throw 'accepted_serving'}
exit 0
'''.replace("VALIDATOR", validator)
        result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_windows_script_never_stops_running_service_and_closes_exact_pair(self):
        pair = self.pair()
        source = self.release.WINDOWS_BODY
        self.assertNotIn("StopService", source)
        self.assertNotIn("Stop-Service", source)
        self.assertLess(source.index("Stopped $service"), source.index("Invoke-CimMethod -InputObject $service -MethodName ChangeStartMode"))
        self.assertIn("-Arguments @{StartMode='Disabled'} -OperationTimeoutSec 5 -ErrorAction Stop", source)
        self.assertNotIn("$service.ChangeStartMode(", source)
        self.assertIn("SameScope $record.postboot $plan.initial_recovery_postboot", source)
        sql = self.release.PAIRED_SQL
        compile(sql, "exact_pair", "exec")
        for expected in ("REPEATABLE READ READ ONLY", "validate_catalog(conn,binding)",
                         "InitialPgLeaseRecovery._quarantine_observation", "primary[12]==replica[12]",
                         "lsn(peers[0][6])>=lsn(primary[19])"):
            self.assertIn(expected, sql)
        import base64
        import gzip
        import re
        import time
        def powershell(script, _deadline):
            blob = re.search(r"FromBase64String\('([^']+)'\)", script)[1]
            cfg = json.loads(gzip.decompress(base64.b64decode(blob)))
            return {"challenge": cfg["challenge"], "status": "CLOSED_PAIR_VERIFIED", "terminal_sha256": "3" * 64,
                    "proof_sha256": "4" * 64, "closed_revision": 15}
        pair.windows.powershell.side_effect = powershell
        result = pair.closed_pair("3" * 64, time.monotonic() + 30)
        self.assertEqual(result["closed_nodes"], ["A", "B"])
        pair.windows.powershell.return_value = {}
        pair.windows.powershell.side_effect = None
        with self.assertRaises(TransitionBlocked):
            pair.closed_pair("3" * 64, time.monotonic() + 30)

    def test_postarm_closure_requires_both_successor_pins_and_preserves_original_scope(self):
        import base64
        import gzip
        import re
        import time
        pair = self.pair()
        captured = []
        def powershell(script, _deadline):
            blob = re.search(r"FromBase64String\('([^']+)'\)", script)[1]
            cfg = json.loads(gzip.decompress(base64.b64decode(blob)))
            captured.append(cfg)
            return {"challenge": cfg["challenge"], "status": "CLOSED_PAIR_VERIFIED", "terminal_sha256": "3" * 64,
                    "proof_sha256": "4" * 64, "closed_revision": 15}
        pair.windows.powershell.side_effect = powershell
        pair.closed_pair_after_arm("3" * 64, "5" * 64, "6" * 64, time.monotonic() + 30)
        self.assertEqual(captured[0]["mode"], "closed_after_arm")
        self.assertEqual(captured[0]["successor"], {"path_sha256": "5" * 64, "image_sha256": "6" * 64})
        self.assertEqual(captured[0]["plan_sha256"], pair.config["plan_sha256"])
        self.assertTrue(captured[0]["verify_runtime"])
        with self.assertRaises(TransitionBlocked):
            pair.closed_pair_after_arm("3" * 64, "5" * 64, "invalid", time.monotonic() + 30)
        self.assertEqual(len(captured), 1)


if __name__ == "__main__":
    unittest.main()
