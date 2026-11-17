"""Native-adapter unit evidence only; no Windows/N100 actions are executed."""
import base64
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/k8s"))
from ha_control.controller import Journal, TransitionBlocked
from ha_control.initial_pg_lease import INITIAL_CUTOVER, InitialPgLease
from ha_control.native_guest_activation import ServingGeneration
from ha_control.native_windows_activation import (
    WindowsActivationLease, WindowsTransitionLease, WindowsTransitionServiceStart, bind_transition_observer_plan, publish_transition_observer_config,
    prepare_transition_observer_plan, publish_transition_observer_launch, start_transition_observer,
    transition_authority_records, publish_transition_authority_records, open_transition_controller, settle_transition_controller, read_transition_closure_receipt, observe_transition_db_closed, read_transition_rejoin_receipt,
)
from ha_control.native_stdio_bridge import BridgeRejected
from ha_control.pg import PgBackend, ROLE_LOCK
from ha_control import cli, scm_pg_auth_probe


class TransitionServiceStartTests(unittest.TestCase):
    def test_controller_requires_ready_lease_before_exposing_app_guard(self):
        generation = ServingGeneration("B", 2, 3, 2)
        controller = {"plan_sha256": "1"*64}
        starter = MagicMock()
        prepare = MagicMock()
        handoff = MagicMock(return_value="2"*64)
        records = {"service_instance_sha256": "3"*64, "binding_path": "fixture",
                   "binding": b'{"legacy_plan_sha256":"fixture"}'}
        check = MagicMock(side_effect=[None, BridgeRejected("lease unavailable")])
        settle = MagicMock(return_value={"plan_sha256": controller["plan_sha256"], "generation": 2,
                                        "node_proofs": {"A": "a"*64, "B": "b"*64}})
        with patch("ha_control.native_windows_activation._transition_authority_scope"), \
                patch("ha_control.native_windows_activation.transition_authority_records", return_value=records), \
                patch("ha_control.native_windows_activation.publish_transition_authority_records") as publish, \
                patch("ha_control.native_windows_activation.WindowsTransitionLease") as lease:
            lease.return_value.guest_authority.return_value = nullcontext(check)
            with self.assertRaisesRegex(BridgeRejected, "lease unavailable"), open_transition_controller(
                controller, generation, starter=starter, prepare_service=prepare, stage_sha256="4"*64,
                live_sha256="5"*64, root="fixture", attempt="6"*64, old_vm_id="fixture",
                publish_handoff=handoff, assert_non_vm_exclusions=MagicMock(), observe_closed=settle, deadline=time.monotonic()+30,
            ):
                self.fail("unconfirmed lease allowed activation")
        starter.start.assert_called_once()
        publish.assert_called_once()
        self.assertEqual(check.call_count, 2)
        starter.stop.assert_called_once()
        settle.assert_called_once()
        self.assertEqual(check.call_args.args[1], "controller-ready")

    def test_closure_receipt_reader_is_pinned_to_launched_query(self):
        script = ROOT / "infra/k8s/ha_control/windows-ha-cutover-live-authority.ps1"
        pin = hashlib.sha256(script.read_bytes()).hexdigest()
        root = rf"C:\ProgramData\N100HA-InitialCutover\{'d'*40}"
        windows = MagicMock()
        kwargs = dict(host_pid=123, started_ms=1731712345000, service_sid="S-1-5-80-1-2-3-4-5",
                      deadline=time.monotonic()+10)
        with self.assertRaisesRegex(BridgeRejected, "db_closure_unconfirmed"):
            read_transition_closure_receipt(windows, pin, root, "a"*64, 2, **(kwargs | {"host_pid": True}))
        windows.powershell.assert_not_called()
        read_transition_closure_receipt(windows, pin, root, "a"*64, 2, **kwargs)
        command = windows.powershell.call_args.args[0]
        self.assertIn(" -ReadClosureReceipt ", command)
        self.assertIn(" -ClosureHostPid '123' ", command)
        self.assertIn(f" -BindingPath '{root}\\journal\\lease-closure-{'a'*64}.json'", command)

    def test_rejoin_reader_binds_exact_mode_and_rejects_open_authority(self):
        script = ROOT / "infra/k8s/ha_control/windows-ha-cutover-live-authority.ps1"
        pin = hashlib.sha256(script.read_bytes()).hexdigest()
        windows = MagicMock()
        receipt = {"status": "PASS", "phase": "rejoining", "authority_opened": False,
                   "rejoin_stage_sha256": "b"*64, "standby_rejoined": True}
        windows.powershell.side_effect = [receipt, receipt | {"authority_opened": True}, receipt]
        root = rf"C:\ProgramData\N100HA-InitialCutover\{'d'*40}"
        common = (windows, pin, root, "a"*64)
        kwargs = dict(host_pid=123, started_ms=1731712345000, service_sid="S-1-5-80-1-2-3-4-5",
                      deadline=time.monotonic()+10)
        self.assertEqual(read_transition_rejoin_receipt(*common, "check-rejoin", **kwargs), receipt)
        command = windows.powershell.call_args.args[0]
        self.assertIn(" -ReadRejoinReceipt ", command)
        self.assertIn(" -RejoinMode 'check-rejoin' ", command)
        for mode in ("check-rejoin", "observe-rejoin-stage"):
            with self.assertRaisesRegex(BridgeRejected, "rejoin_receipt_unconfirmed"):
                read_transition_rejoin_receipt(*common, mode, **kwargs)

    def test_lost_closure_response_recovers_scm_result_without_replay(self):
        starter = MagicMock()
        starter.check_lease_closed.side_effect = OSError("lost response")
        started = {"host_pid": 123, "started_at_ms": 1731712345000, "service_sid": "S-1-5-80-1-2-3-4-5"}
        starter.observe_closure_check.return_value = started
        controller = {"plan_sha256": "a"*64}
        generation = ServingGeneration("B", 2, 3, 2)
        deadline = time.monotonic() + 35
        with patch("ha_control.native_windows_activation.read_transition_closure_receipt") as reader:
            outcome = observe_transition_db_closed(
                starter, controller, {}, generation, live_sha256="b"*64, root="fixture", deadline=deadline,
            )
            self.assertIs(outcome, reader.return_value)
            reader.assert_called_once_with(
                starter.windows, "b"*64, "fixture", "a"*64, 2,
                host_pid=123, started_ms=1731712345000, service_sid=started["service_sid"], deadline=deadline,
            )
        starter.check_lease_closed.assert_called_once()
        starter.observe_closure_check.assert_called_once_with(controller, {}, deadline)
        starter.restore_closure_args.assert_not_called()

    def test_settlement_never_reads_db_until_exact_service_is_stopped(self):
        starter = MagicMock()
        controller = {"plan_sha256": "a"*64}
        parameters = {}
        generation = ServingGeneration("B", 2, 3, 2)
        deadline = time.monotonic() + 10
        closed = MagicMock()
        order = MagicMock()
        order.attach_mock(starter, "service")
        order.attach_mock(closed, "db")
        starter.stop.side_effect = BridgeRejected("stop unconfirmed")
        with self.assertRaisesRegex(BridgeRejected, "stop unconfirmed"):
            settle_transition_controller(starter, controller, parameters, None, generation, closed, deadline)
        self.assertEqual([call[0] for call in order.mock_calls], ["service.observe", "service.stop"])
        order.reset_mock()
        starter.stop.side_effect = None
        closed.return_value = {"plan_sha256": "a"*64, "generation": 2, "node_proofs": {"A": "b"*64}}
        with self.assertRaisesRegex(BridgeRejected, "db_closure_unconfirmed"):
            settle_transition_controller(starter, controller, parameters, None, generation, closed, deadline)
        self.assertEqual([call[0] for call in order.mock_calls], ["service.observe", "service.stop", "db"])

    def test_live_authority_records_bind_previous_vm_and_running_controller(self):
        image = "7"*64
        vm_id = "11111111-2222-3333-4444-555555555555"
        python = r"C:\Python311\python.exe"
        observer = {"legacy_plan_sha256": "8"*64}
        plan = {"profile": "enforced-vm-v1", "generation": 2, "node": "A",
                "windows_enforced": {"service_name": "N100HA_" + "1"*24 + "_controller",
                                     "vm_id": vm_id, "python_path": python,
                                     "disks": [{"path": r"C:\VM\Z.vhdx"}, {"path": r"C:\VM\a.vhdx"}]},
                "windows_transition_observer": observer,
                "transition_service_start": {"service_image_path_sha256": image}}
        raw = json.dumps(plan).encode()
        pin = hashlib.sha256(raw).hexdigest()
        controller = {"plan": raw, "plan_sha256": pin}
        started = {"controller_plan_sha256": pin, "service_pid": 123,
                   "service_created_ticks": "638674752000000000", "service_image_path_sha256": image,
                   "python_command_sha256": "e"*64}
        root = rf"C:\ProgramData\N100HA-InitialCutover\{'d'*40}"
        generation = ServingGeneration("B", 2, 3, 2)
        args = dict(root=root, attempt="a"*64, handoff_sha256="b"*64, old_vm_id=vm_id)
        records = transition_authority_records(controller, started, generation, **args)
        body = json.loads(records["binding"])
        self.assertEqual((body["old_vm_id"], body["target"], body["generation"], body["timeline"]), (vm_id, "B", 2, 3))
        self.assertEqual(body["python_path_sha256"], hashlib.sha256(python.encode()).hexdigest())
        self.assertEqual(body["python_command_sha256"], started["python_command_sha256"])
        expected_disks = (r"c:\vm\a.vhdx" + "\n" + r"c:\vm\z.vhdx").encode()
        self.assertEqual(body["old_disk_paths_sha256"], hashlib.sha256(expected_disks).hexdigest())
        self.assertEqual(body["state_path"], root + r"\journal\controller-state.json")
        self.assertEqual(json.loads(records["grant"]), {
            "attempt_id": args["attempt"], "handoff_sha256": args["handoff_sha256"],
            "service_instance_sha256": hashlib.sha256(records["binding"]).hexdigest(),
        })
        with self.assertRaisesRegex(BridgeRejected, "authority_record_invalid"):
            transition_authority_records(controller, started, generation, **(args | {"old_vm_id": "9"*8 + vm_id[8:]}))

        stage = ROOT / "infra/k8s/ha_control/windows-ha-stage-initial-activation.ps1"
        stage_pin = hashlib.sha256(stage.read_bytes()).hexdigest()
        windows = MagicMock()
        record_pins = [hashlib.sha256(records[name]).hexdigest() for name in ("binding", "grant")]
        windows.powershell.side_effect = [{"transition_authority_record_sha256": digest} for digest in record_pins]
        deadline = time.monotonic() + 30
        self.assertEqual(publish_transition_authority_records(windows, stage_pin, records, pin, deadline), record_pins[0])
        sent = [call.args[0] for call in windows.powershell.call_args_list]
        for command, name in zip(sent, ("binding", "grant"), strict=True):
            self.assertIn(records[f"{name}_path"], command)
            self.assertIn("-PublishTransitionAuthority", command)
        windows.powershell.reset_mock()
        windows.powershell.side_effect = [{"blocked": "incorrect binding"}]
        with self.assertRaisesRegex(BridgeRejected, "record_unconfirmed"):
            publish_transition_authority_records(windows, stage_pin, records, pin, deadline)
        windows.powershell.assert_called_once()

    def test_native_start_records_durable_intent_before_scm(self):
        script = (ROOT / "infra/k8s/ha_control/windows-ha-start-initial-service.ps1").read_text()
        scope = script[script.index("function Start-PreparedTransitionService"):script.index("    if (-not $Go")]
        statements = (
            "transition-service-start-",
            "$commandLock =",
            "Assert-TransitionPreflight $servingSha",
            "Assert-TransitionStopped $Plan $HostPath $Current.PathName",
            "Test-Path -LiteralPath $intentPath",
            "[IO.FileMode]::CreateNew",
            "[IO.FileOptions]::WriteThrough",
            "$file.Flush($true)",
            "Get-Acl -LiteralPath $intentPath",
            "[IO.File]::Replace($stage,$argsPath",
            "Assert-TransitionPreflight $argsSha",
            "Start-Service -Name $ServiceName",
            "} finally {",
            "Assert-TransitionStopped $Plan $HostPath $Current.PathName",
            "Restore-TransitionServingArgs $argsPath $restore $servingSha $argsSha",
            "Write-TransitionArgsStage ($intentPath + '.completed')",
            "$receiptJson\n                return",
            "} finally { $commandLock.Dispose() }",
        )
        cursor = 0
        for statement in statements:
            cursor = scope.index(statement, cursor) + len(statement)

    def test_native_closure_restore_does_not_replay_scm_start(self):
        script = (ROOT / "infra/k8s/ha_control/windows-ha-start-initial-service.ps1").read_text()
        scope = script[script.index("            if ($RestoreClosureArgs) {"):
                       script.index("        } else {\n            if ('TrustedStoppedV1.Native'")]
        self.assertLess(scope.index("Assert-TransitionStopped"), scope.index("Restore-TransitionServingArgs"))
        self.assertLess(scope.index("Restore-TransitionServingArgs"), scope.index("serving_args_restored"))
        self.assertNotIn("Start-Service", scope)

    def test_start_is_bound_to_plan_and_uncertain_response_cannot_replay(self):
        script = ROOT / "infra/k8s/ha_control/windows-ha-start-initial-service.ps1"
        service = "N100HA_" + "1"*24 + "_controller"
        image = "6"*64
        raw = json.dumps({"windows_enforced": {"service_name": service}, "windows_transition_observer": {},
                          "transition_service_start": {"service_image_path_sha256": image}}).encode()
        pin = hashlib.sha256(raw).hexdigest()
        plan = {"plan": raw, "plan_sha256": pin}
        parameters = {"ServiceName": service, "PlanSha256": pin, "ApprovalSha256": "2"*64,
                      "ServiceHostSha256": "3"*64, "PreflightSha256": "4"*64,
                      "PlanPath": r"C:\fixture\plan.json", "ApprovalPath": r"C:\fixture\approval.json",
                      "PreflightPath": r"C:\fixture\preflight.ps1", "ScmVerifierPath": r"C:\fixture\scm.cs"}
        windows = MagicMock()
        observed = {"transition_service_observed": True, "service_pid": 123, "controller_plan_sha256": pin,
                    "service_created_ticks": "638674752000000000", "service_image_path_sha256": image,
                   "python_command_sha256": "e"*64}
        windows.powershell.side_effect = [TimeoutError("lost reply"), observed]
        start = WindowsTransitionServiceStart(windows, hashlib.sha256(script.read_bytes()).hexdigest())
        deadline = time.monotonic() + 30
        with self.assertRaisesRegex(BridgeRejected, "start_scope_invalid"):
            start.start(plan, parameters | {"PlanSha256": "5"*64}, deadline)
        windows.powershell.assert_not_called()
        self.assertEqual(start.start(plan, parameters, deadline), observed)
        with self.assertRaisesRegex(BridgeRejected, "start_reconciliation_required"):
            start.start(plan, parameters, deadline)
        requests = [call.args[0] for call in windows.powershell.call_args_list]
        self.assertEqual(len(requests), 2)
        self.assertTrue(requests[0].endswith(" -StartTransition -Go"))
        self.assertTrue(requests[1].endswith(" -ObserveTransition -Go"))

        for replies, calls in (([{"blocked": "initial_service_transition_scope"}], 1),
                               ([TimeoutError("lost reply"), {"blocked": "not running"}], 2),
                               ([TimeoutError("lost reply"), observed | {"service_image_path_sha256": "7"*64}], 2),
                               ([TimeoutError("lost reply"), observed | {"service_created_ticks": 123}], 2)):
            with self.subTest(replies=replies):
                windows.powershell.reset_mock()
                windows.powershell.side_effect = replies
                start = WindowsTransitionServiceStart(windows, hashlib.sha256(script.read_bytes()).hexdigest())
                with self.assertRaisesRegex(BridgeRejected, "service_start_unconfirmed"):
                    start.start(plan, parameters, deadline)
                self.assertEqual(windows.powershell.call_count, calls)


class TransitionObserverPublicationTests(unittest.TestCase):
    def test_prepare_observer_plan_runs_the_exact_setup_order(self):
        callback = b"fixture"
        pin = hashlib.sha256(callback).hexdigest()
        service = "N100HA_" + "1"*24 + "_controller"
        plan = {"profile": "enforced-vm-v1", "generation": 2,
                "windows_enforced": {"service_name": service, "service_sid": "S-1-5-80-1-2-3-4-5"}}
        plan_raw = json.dumps(plan).encode()
        pins = {key: "2"*64 for key in ("windows_boot_sha256", "wsl_boot_sha256", "collector_sha256")}
        metadata = pins | {key: "3"*64 for key in ("launcher_sha256", "server_sha256", "callback_sha256",
                                                   "reactivation_plan_sha256", "marker_sha256")}
        script = ROOT / "infra/k8s/ha_control/windows-ha-stage-initial-activation.ps1"
        options = dict(
            root=rf"C:\ProgramData\N100HA-InitialCutover\{'d'*40}",
            plan_raw=plan_raw, plan_sha256=hashlib.sha256(plan_raw).hexdigest(),
            binding={"callback_config": callback, "callback_config_sha256": pin,
                     "plan": "a"*64, "attempt": "b"*64, "runtime_sha256": "c"*64},
            observer=metadata | {"service_name": service}, manifest_sha256="4"*64,
            stage_sha256=hashlib.sha256(script.read_bytes()).hexdigest(),
            client_sha256="5"*64, deadline=time.monotonic() + 30,
        )
        steps = []
        windows = MagicMock()

        def execute(source, _):
            mode = next(name for name in ("PublishTransitionObserverConfig", "ObserveTransitionObserverConfig",
                                          "PublishTransitionObserverLaunch", "StartTransitionObserver")
                        if f" -{name}" in source)
            steps.append(mode)
            if mode == "PublishTransitionObserverLaunch":
                submitted = base64.b64decode(source.split(" -IntentBase64 '", 1)[1].split("'", 1)[0])
                return {"transition_observer_launch_sha256": hashlib.sha256(submitted).hexdigest()}
            return {
                "PublishTransitionObserverConfig": {"transition_observer_config_sha256": pin},
                "ObserveTransitionObserverConfig": {"observed_transition_observer_config_sha256": pin},
                "StartTransitionObserver": pins | {"callback_config_sha256": pin, "key_sha256": "6"*64,
                                                   "observer_pid": 123, "pipe_name": "n100ha-lease-proof-" + "7"*32},
            }[mode]

        windows.powershell.side_effect = execute
        with self.assertRaisesRegex(BridgeRejected, "observer_plan_invalid"):
            prepare_transition_observer_plan(windows, **(options | {"plan_sha256": "0"*64}))
        windows.powershell.assert_not_called()
        result = prepare_transition_observer_plan(windows, **options)
        self.assertEqual(steps, ["PublishTransitionObserverConfig", "ObserveTransitionObserverConfig",
                                 "PublishTransitionObserverLaunch", "ObserveTransitionObserverConfig",
                                 "StartTransitionObserver"])
        self.assertEqual(json.loads(result["plan"])["windows_transition_observer"]["server_pid"], 123)

        steps.clear()

        def reject_launch(source, deadline):
            result = execute(source, deadline)
            if steps[-1] == "PublishTransitionObserverLaunch":
                return {"blocked": "initial_stage_observer"}
            return result

        windows.powershell.side_effect = reject_launch
        with self.assertRaisesRegex(BridgeRejected, "observer_launch_unconfirmed"):
            prepare_transition_observer_plan(windows, **options)
        self.assertEqual(steps, ["PublishTransitionObserverConfig", "ObserveTransitionObserverConfig",
                                 "PublishTransitionObserverLaunch"])

    def test_plan_binds_observer_receipt_and_rejects_stale_or_foreign_evidence(self):
        callback = b"fixture"
        pin = hashlib.sha256(callback).hexdigest()
        root = rf"C:\ProgramData\N100HA-InitialCutover\{'d'*40}"
        service = "N100HA_" + "1"*24 + "_controller"
        plan = {"profile": "enforced-vm-v1", "generation": 2, "untouched": [1, 2],
                "windows_enforced": {"service_name": service, "service_sid": "S-1-5-80-1-2-3-4-5"}}
        raw = json.dumps(plan).encode()
        expected_sha = hashlib.sha256(raw).hexdigest()
        binding = {"callback_config": callback, "callback_config_sha256": pin, "plan": "a"*64, "attempt": "b"*64}
        boot = {"windows_boot_sha256": "5"*64, "wsl_boot_sha256": "6"*64, "collector_sha256": "7"*64}
        observer = boot | {"service_name": service}
        started = boot | {"observer_pid": 123, "pipe_name": "n100ha-lease-proof-" + "2"*32,
                          "callback_config_sha256": pin, "key_sha256": "3"*64,
                          "callback_config_path": root + rf"\transition-observer-config-{pin}.json",
                          "key_path": root + rf"\transition-observer-key-{pin}.key"}
        options = {"root": root, "binding": binding, "observer": observer, "started": started, "client_sha256": "4"*64}
        result = bind_transition_observer_plan(raw, expected_sha, **options)
        self.assertEqual(hashlib.sha256(result["plan"]).hexdigest(), result["plan_sha256"])
        data = json.loads(result["plan"])
        observed = data.pop("windows_transition_observer")
        self.assertEqual(data, plan)
        self.assertEqual(observed, boot | {
            "server_pid": 123, "pipe_name": started["pipe_name"], "key_sha256": started["key_sha256"],
            "legacy_plan_sha256": binding["plan"], "legacy_attempt_id": binding["attempt"],
            "client_sha256": options["client_sha256"], "callback_config_sha256": pin,
        })
        for change in ({"observer": observer | {"service_name": "foreign"}},
                       {"started": started | {"wsl_boot_sha256": "8"*64}},
                       {"started": started | {"key_path": root + r"\other.key"}},
                       {"binding": binding | {"callback_config": b"tampered"}}):
            with self.subTest(change=change), self.assertRaisesRegex(BridgeRejected, "observer_plan_invalid"):
                bind_transition_observer_plan(raw, expected_sha, **(options | change))
        with self.assertRaisesRegex(BridgeRejected, "observer_plan_invalid"):
            bind_transition_observer_plan(raw, "0"*64, **options)

    def test_native_publication_binds_expected_location_and_rejects_uncertain_receipt(self):
        script = ROOT / "infra/k8s/ha_control/windows-ha-stage-initial-activation.ps1"
        stage_pin = hashlib.sha256(script.read_bytes()).hexdigest()
        raw = b'{"fixture":true}\n'
        pin = hashlib.sha256(raw).hexdigest()
        binding = {"callback_config": raw, "callback_config_sha256": pin, "runtime_sha256": "a"*64}
        root = rf"C:\ProgramData\N100HA-InitialCutover\{'d'*40}"
        path = root + rf"\transition-observer-config-{pin}.json"
        windows = MagicMock()
        windows.powershell.side_effect = [{"transition_observer_config_sha256": pin}, {"blocked": "initial_stage_observer"}]

        self.assertEqual(publish_transition_observer_config(windows, root, stage_pin, binding, "b"*64, 40), path)
        source, deadline = windows.powershell.call_args.args
        self.assertIn("-InputPath '" + path + "'", source)
        self.assertIn(" -Go ", source)
        self.assertTrue(source.endswith(" -PublishTransitionObserverConfig"))
        self.assertEqual(deadline, 40)
        with self.assertRaisesRegex(BridgeRejected, "config_unconfirmed"):
            publish_transition_observer_config(windows, root, stage_pin, binding, "b"*64, 40)
        with self.assertRaisesRegex(BridgeRejected, "config_invalid"):
            publish_transition_observer_config(windows, root, stage_pin, binding | {"callback_config": b"changed"}, "b"*64, 40)
        self.assertEqual(windows.powershell.call_count, 2)

        deadline = time.monotonic() + 40
        windows.powershell.reset_mock()
        observed = {"observed_transition_observer_config_sha256": pin}
        windows.powershell.side_effect = [TimeoutError("missing receipt"), observed, observed]
        self.assertEqual(publish_transition_observer_config(windows, root, stage_pin, binding, "b"*64, deadline), path)
        self.assertEqual(publish_transition_observer_config(
            windows, root, stage_pin, binding, "b"*64, deadline, observe_only=True,
        ), path)
        publication, observed_after_timeout, observed_only = (
            call.args[0] for call in windows.powershell.call_args_list
        )
        self.assertIn("-PublishTransitionObserverConfig", publication)
        self.assertEqual(observed_after_timeout, observed_only)
        self.assertIn("-ObserveTransitionObserverConfig", observed_only)
        self.assertNotIn("-IntentBase64", observed_only)
        self.assertEqual(windows.powershell.call_count, 3)


    def test_launch_publication_binds_exact_legacy_plan_and_attempt(self):
        script = ROOT / "infra/k8s/ha_control/windows-ha-stage-initial-activation.ps1"
        stage_pin = hashlib.sha256(script.read_bytes()).hexdigest()
        raw = b'{"fixture":true}\n'
        pin = hashlib.sha256(raw).hexdigest()
        binding = {"callback_config": raw, "callback_config_sha256": pin, "runtime_sha256": "a"*64,
                   "plan": "e"*64, "attempt": "f"*64}
        root = rf"C:\ProgramData\N100HA-InitialCutover\{'d'*40}"
        deadline = time.monotonic() + 30
        observer = {key: "2"*64 for key in (
            "launcher_sha256", "server_sha256", "callback_sha256", "windows_boot_sha256",
            "wsl_boot_sha256", "collector_sha256", "reactivation_plan_sha256", "marker_sha256",
        )}
        observer["service_name"] = "N100HA_" + "1"*24 + "_controller"

        def execute(source, _):
            if source.endswith(" -ObserveTransitionObserverConfig"):
                return {"observed_transition_observer_config_sha256": pin}
            self.assertIn(f"transition-observer-launch-{pin}.json", source)
            self.assertIn(" -PublishTransitionObserverLaunch ", source)
            self.assertNotIn(" -StartTransitionObserver ", source)
            submitted = base64.b64decode(source.split(" -IntentBase64 '", 1)[1].split("'", 1)[0])
            self.assertEqual(json.loads(submitted), observer | {
                "callback_config_sha256": pin, "legacy_plan_sha256": binding["plan"],
                "legacy_attempt_id": binding["attempt"],
            })
            return {"transition_observer_launch_sha256": hashlib.sha256(submitted).hexdigest()}

        windows = MagicMock()
        windows.powershell.side_effect = execute
        digest = publish_transition_observer_launch(windows, root, stage_pin, binding, "b"*64, observer, deadline)
        self.assertEqual(len(digest), 64)
        self.assertEqual(windows.powershell.call_count, 2)

    def test_transition_observer_rechecks_publication_and_starts_exact_launch_once(self):
        script = ROOT / "infra/k8s/ha_control/windows-ha-stage-initial-activation.ps1"
        stage_pin = hashlib.sha256(script.read_bytes()).hexdigest()
        raw = b'{"fixture":true}\n'
        pin = hashlib.sha256(raw).hexdigest()
        binding = {"callback_config": raw, "callback_config_sha256": pin, "runtime_sha256": "a"*64}
        root = rf"C:\ProgramData\N100HA-InitialCutover\{'d'*40}"
        deadline = time.monotonic() + 30
        response = {"observer_pid": 123, "pipe_name": "n100ha-lease-proof-" + "1"*32, "callback_config_sha256": pin,
                    **{key: "2"*64 for key in ("key_sha256", "windows_boot_sha256", "wsl_boot_sha256", "collector_sha256")}}
        windows = MagicMock()
        windows.powershell.side_effect = [{"observed_transition_observer_config_sha256": pin}, response]
        result = start_transition_observer(windows, root, stage_pin, binding, "b"*64, "c"*64, deadline)
        self.assertEqual(result["observer_pid"], 123)
        self.assertTrue(result["key_path"].endswith(f"transition-observer-key-{pin}.key"))
        observed, launched = [entry.args[0] for entry in windows.powershell.call_args_list]
        self.assertIn("-ObserveTransitionObserverConfig", observed)
        self.assertIn("-StartTransitionObserver", launched)
        self.assertIn(f"transition-observer-launch-{pin}.json", launched)


class InitialServiceRetirementTests(unittest.TestCase):
    def test_uncertain_retirement_requires_exact_read_only_observation(self):
        script = ROOT / "infra/k8s/ha_control/windows-ha-cutover-live-authority.ps1"
        guard = MagicMock()
        windows = MagicMock()
        windows.powershell.side_effect = [
            TimeoutError("uncertain"), {"initial_service_retired": True, "retirement_sha256": "d" * 64},
        ]
        custody = SimpleNamespace(windows=windows, live_sha256=hashlib.sha256(script.read_bytes()).hexdigest(),
                                  binding=SimpleNamespace(plan_sha256="a" * 64, attempt_id="b" * 64))
        lease = WindowsActivationLease(custody, r"C:\Private\binding.json", {
            "first_write_proof_sha256": "e" * 64, "service_instance_sha256": "c" * 64,
        })
        deadline = time.monotonic() + 30
        with self.assertRaises(TimeoutError):
            lease.retire_service(deadline, assert_guard=guard)
        with self.assertRaisesRegex(BridgeRejected, "initial_service_retirement_reconciliation_required"):
            lease.retire_service(deadline, assert_guard=guard)
        self.assertEqual(lease.observe_retired(deadline, assert_guard=guard), "d" * 64)
        retire, observe = (call.args[0] for call in windows.powershell.call_args_list)
        self.assertTrue(retire.endswith(" -Retire"))
        self.assertTrue(observe.endswith(" -ObserveRetired"))
        self.assertIn("-ExpectedServiceInstanceSha256 '" + "c" * 64 + "'", retire)
        self.assertEqual(guard.call_count, 3)
        unsettled = MagicMock(side_effect=TransitionBlocked("previous_scm_call_unsettled"))
        with self.assertRaisesRegex(TransitionBlocked, "previous_scm_call_unsettled"):
            lease.resume_retirement(deadline, assert_guard=guard, assert_settled=unsettled)
        self.assertEqual(windows.powershell.call_count, 2)
        windows.powershell.side_effect = None
        windows.powershell.return_value = {"initial_service_retired": True, "retirement_sha256": "f" * 64}
        self.assertEqual(lease.resume_retirement(deadline, assert_guard=guard, assert_settled=lambda: None), "f" * 64)
        self.assertTrue(windows.powershell.call_args.args[0].endswith(" -ResumeRetire"))

    def test_generation_windows_challenge_requires_live_process_and_other_exclusions(self):
        script = ROOT / "infra/k8s/ha_control/windows-ha-cutover-live-authority.ps1"
        attempt, service = "a"*64, "b"*64
        path = rf"C:\ProgramData\N100HA-InitialCutover\{'c'*40}\transition-service-{attempt}.json"
        reply = MagicMock(return_value={"generation": 2, "timeline": 3, "revision": 4,
                                       "transition_proof_sha256": "d"*64})
        windows = MagicMock()
        windows.live_authority_session.return_value = nullcontext(reply)
        exclusions = MagicMock(return_value={"legacy_sha256": "e"*64, "standby_sha256": "f"*64})
        lease = WindowsTransitionLease(
            windows, path, service_instance_sha256=service, live_sha256=hashlib.sha256(script.read_bytes()).hexdigest(),
            transition_attempt=attempt, legacy_plan_sha256="1"*64, generation_handoff_sha256="2"*64,
            generation=ServingGeneration("B", 2, 3, 4), assert_non_vm_exclusions=exclusions,
        )
        deadline = time.monotonic() + 30
        with lease.guest_authority(deadline) as verify:
            result = verify("3"*64, "start:portal-web", deadline)
            self.assertRegex(result, r"^[a-f0-9]{64}$")
            source = reply.call_args.args[0]
            self.assertIn("-PriorSha256 '" + "3"*64 + "'", source)
            self.assertIn("-TransitionBoundarySha256 '" + hashlib.sha256(b"start:portal-web").hexdigest() + "'", source)
            reply.return_value["timeline"] = 7
            with self.assertRaisesRegex(BridgeRejected, "native_transition_service_proof_unconfirmed"):
                verify("4"*64, "guest_started", deadline)
            exclusions.return_value = {"legacy_sha256": "e"*64}
            with self.assertRaisesRegex(BridgeRejected, "native_transition_exclusions_unconfirmed"):
                verify("5"*64, "guest_started", deadline)
        self.assertEqual(reply.call_count, 2)


class Fence:
    def __init__(self, plan):
        self.plan_data, self.enters, self.exits, self.stopped = plan, 0, 0, True

    @contextmanager
    def guard(self):
        self.enters += 1
        try:
            yield
        finally:
            self.exits += 1

    def fenced(self, state): return self.stopped
    def fence(self, state): self.stopped = True


class PGControlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        for name in ("ca", "pass"):
            (self.root / name).write_text("synthetic fixture")
            (self.root / name).chmod(0o600)
        endpoint = dict(port=5432, dbname="ha", user="fixture_admin", sslmode="verify-full",
                        sslrootcert=str(self.root / "ca"), passfile=str(self.root / "pass"))
        self.plan = dict(cluster="synthetic", node="A", generation=7, timeline=1, system_id="123", _approval_action="ha-transition",
                         pg_nodes={"A": dict(endpoint, host="ha-a"), "B": dict(endpoint, host="ha-b")})
        self.fence = Fence(self.plan)
        self.backend = PgBackend(self.root / "plan.json", self.root / "approval.json", fence=self.fence)
        self.state = dict(cluster="synthetic", old="A", target="B", generation=7, timeline=1,
                          phase="validating", attempts=1)
        self.guard = self.backend.guard()
        self.guard.__enter__()
        self.addCleanup(lambda: self.guard.__exit__(None, None, None))

    def observation(self, node="B", *, recovery=False, opened=False):
        return dict(cluster="synthetic", epoch=7, revision=9, primary="B", partner="A", timeline=2,
                    serving=["B"], worker="B", mode="AS", writes=opened, history=opened, remaining=4,
                    system_id="123", node="ha_" + node, recovery=recovery, actual_timeline=2,
                    flush="0/20", replay="0/20", sync_names="FIRST1(ha_a)", fsync=True,
                    full_page_writes=True, sync_peer="ha_a")

    def candidate(self):
        return dict(self.observation("B", recovery=True), epoch=6, primary="A", partner="B", timeline=1, actual_timeline=1)

    @contextmanager
    def fake_connection(self, node):
        yield self.primary if node == "B" else self.peer

    def prepare_fresh(self, *, lease=False, peer_lsn="0/20", bad_nonce=False, opened=False, update_rows=1):
        self.primary, self.peer = MagicMock(), MagicMock()
        def execute(query, params=None):
            result = MagicMock()
            result.rowcount = update_rows
            if "SELECT pg_current_wal_insert_lsn" in query:
                result.fetchone.return_value = ("0/10",)
            elif "SELECT generation FROM" in query:
                result.fetchone.return_value = None if bad_nonce else (7,)
            else:
                result.fetchone.return_value = None
            return result
        self.primary.execute.side_effect = execute
        self.peer.execute.side_effect = execute
        before = [self.observation(opened=opened), self.observation("A", recovery=True, opened=opened)]
        sequence = before * 3 + [self.observation(opened=lease), dict(self.observation("A", recovery=True, opened=lease), replay=peer_lsn)]
        return patch.object(self.backend, "_connection", side_effect=self.fake_connection), patch.object(self.backend, "_observe", side_effect=deepcopy(sequence))

    def test_guard_reentrancy_reuses_exact_native_fence_process(self):
        with self.backend.guard():
            with self.backend.guard():
                self.assertEqual(self.fence.enters, 1)
                self.assertEqual(self.backend.depth, 3)
        self.assertEqual(self.backend.depth, 1)
        self.assertEqual(self.fence.exits, 0)

    def test_unbound_generation_and_inline_password_cannot_reach_database(self):
        with self.assertRaisesRegex(TransitionBlocked, "controller_plan_state_mismatch"):
            self.backend.bind(dict(self.state, generation=6))
        self.plan["pg_nodes"]["A"]["password"] = "synthetic"
        with self.assertRaisesRegex(TransitionBlocked, "authenticated_pg_endpoints_required"):
            self.backend._plan()

    def test_dead_or_lock_busy_old_primary_does_not_prevent_positive_fencing(self):
        with patch.object(self.backend, "_read", return_value=self.candidate()), patch.object(self.backend, "_local_metadata", side_effect=TransitionBlocked("authenticated_pg_unavailable")):
            self.backend.close(self.state)
            self.assertTrue(self.backend.fenced(self.state))
        with patch.object(self.backend, "_read", return_value=dict(self.candidate(), system_id="wrong")):
            # Identity validation itself belongs to _observe, tested below.
            raw = MagicMock()
            raw.execute.return_value.fetchone.return_value = ({"cluster": "foreign"},)
            with self.assertRaisesRegex(TransitionBlocked, "pg_identity_mismatch"):
                self.backend._observe(raw, "B", self.state)

    def test_reachable_foreign_old_primary_is_not_misclassified_as_network_loss(self):
        with patch.object(self.backend, "_read", return_value=self.candidate()), patch.object(self.backend, "_local_metadata", side_effect=TransitionBlocked("pg_identity_mismatch")):
            with self.assertRaisesRegex(TransitionBlocked, "pg_identity_mismatch"):
                self.backend.close(self.state)

    def test_resumed_promoted_candidate_is_closed_before_other_actions(self):
        with patch.object(self.backend, "_read", return_value=self.observation()), patch.object(self.backend, "_local_metadata") as close:
            self.backend.close(self.state)
        self.assertEqual(close.call_count, 2)
        self.assertEqual(close.call_args_list[0].args, ("B", self.state))
        self.assertEqual(close.call_args_list[0].kwargs, {})
        self.assertEqual(close.call_args_list[1].kwargs, {"promoted": True})

    def test_initial_already_primary_target_is_closed_and_never_adopted(self):
        initial = dict(self.state, phase="fencing")
        with patch.object(self.backend, "_read", return_value=self.observation()), patch.object(self.backend, "_local_metadata") as close:
            with self.assertRaisesRegex(TransitionBlocked, "unexpected_primary_before_promotion_intent"):
                self.backend.close(initial)
        # Close the unexpected writer, but do not publish a new primary/epoch.
        close.assert_called_once_with("B", initial)
        for method in (self.backend.promote, self.backend.promoted):
            with patch.object(self.backend, "_connection") as connect:
                with self.assertRaisesRegex(TransitionBlocked, "unexpected_primary_before_promotion_intent"):
                    method(initial)
                connect.assert_not_called()

    def test_fence_only_approval_cannot_arm_pg_adapter_or_first_database_call(self):
        plan = deepcopy(self.plan)
        plan["_approval_action"] = "fence"
        bridge = Fence(plan)
        backend = PgBackend(self.root / "plan.json", self.root / "approval.json", fence=bridge)
        with patch.object(backend, "_connection") as connect:
            with self.assertRaisesRegex(TransitionBlocked, "ha_transition_approval_required"):
                with backend.guard():
                    backend.close(self.state)
            connect.assert_not_called()
        self.assertEqual(backend.depth, 0)
        self.assertEqual(bridge.enters, 1)
        self.assertEqual(bridge.exits, 1)

    def test_initial_recovery_target_requires_exact_source_timeline_and_old_epoch(self):
        for changes in ({"epoch": 7}, {"timeline": 2}, {"actual_timeline": 2}, {"primary": "B"}):
            with patch.object(self.backend, "_read", return_value=dict(self.candidate(), **changes)), patch.object(self.backend, "_local_metadata") as metadata:
                with self.assertRaisesRegex(TransitionBlocked, "promotion_candidate_mismatch"):
                    self.backend.close(dict(self.state, phase="fencing"))
                metadata.assert_not_called()

    def test_promotion_without_positive_host_fence_is_impossible(self):
        self.fence.stopped = False
        with patch.object(self.backend, "_connection") as connect:
            for operation in (self.backend.promote, self.backend.promoted):
                with self.assertRaisesRegex(TransitionBlocked, "positive_fence_required"):
                    operation(self.state)
            connect.assert_not_called()

    def test_planned_vm_stop_requires_a_drained_and_closed_source_lease(self):
        self.backend.plan.update(profile="enforced-vm-v1", required_replay_lsn="0/20",
                                 source_lease_close_required=True)
        state = dict(self.state, phase="fencing")
        closed = dict(self.candidate(), node="ha_A", recovery=False, remaining=-1)
        self.primary, self.peer = MagicMock(), MagicMock()
        self.fence.stopped = False
        for drift in ({"writes": True}, {"history": True}, {"remaining": 1}, {"actual_timeline": 2},
                      {"epoch": 5}, {"remaining": float("nan")}, {"remaining": None}):
            with self.subTest(drift=drift):
                with patch.object(self.backend, "_connection", side_effect=self.fake_connection), patch.object(
                    self.backend, "_observe", return_value=closed | drift,
                ):
                    with self.assertRaisesRegex(TransitionBlocked, "source_write_authority_not_closed"):
                        self.backend.fence(state)
                self.assertFalse(self.fence.stopped)
        active = []

        @contextmanager
        def source_connection(node):
            self.assertEqual(node, "A")
            active.append(node)
            try:
                yield self.peer
            finally:
                active.pop()

        fence = self.fence.fence
        record = MagicMock()

        def stop_while_locked(current):
            self.assertEqual(active, ["A"])
            self.peer.execute.assert_any_call("SELECT pg_advisory_xact_lock(%s)", (ROLE_LOCK,))
            record.assert_called_once_with(0x20)
            return fence(current)

        with patch.object(self.backend, "_connection", side_effect=source_connection), patch.object(
            self.backend, "_observe", return_value=closed,
        ), patch.object(self.backend, "_read") as observe_peer, patch.object(
            self.fence, "fence", side_effect=stop_while_locked,
        ) as stop:
            observe_peer.return_value = dict(self.candidate(), replay="0/1F")
            with self.assertRaisesRegex(TransitionBlocked, "source_final_wal_not_replayed"):
                self.backend.fence(state, record_source_wal=record)
            stop.assert_not_called()
            record.assert_not_called()
            observe_peer.return_value = self.candidate()
            with self.assertRaisesRegex(TransitionBlocked, "source_fence_wal_record_required"):
                self.backend.fence(state)
            self.backend.fence(state, record_source_wal=record)
        self.assertTrue(self.fence.stopped)
        self.assertEqual(active, [])

    def test_enforced_transition_requires_a_nonzero_approved_replay_floor(self):
        self.backend.plan = dict(self.plan, profile="enforced-vm-v1")
        for checkpoint in (None, "0/0", "0/100000000", "missing"):
            with self.subTest(checkpoint=checkpoint):
                self.backend.plan["required_replay_lsn"] = checkpoint
                with self.assertRaisesRegex(TransitionBlocked, "promotion_replay_floor_required"):
                    self.backend.bind(self.state)
        self.backend.plan["required_replay_lsn"] = "1/0"
        self.backend.bind(self.state)

    def test_fenced_candidate_cannot_promote_before_approved_replay(self):
        self.backend.plan["required_replay_lsn"] = "0/21"
        self.primary = MagicMock()
        self.peer = MagicMock()
        with patch.object(self.backend, "_connection", side_effect=self.fake_connection), \
             patch.object(self.backend, "_observe", return_value=self.candidate()):
            with self.assertRaisesRegex(TransitionBlocked, "promotion_replay_floor_not_reached"):
                self.backend.promote(self.state)
        self.primary.execute.assert_not_called()

    def test_planned_promotion_after_restart_requires_recorded_wal(self):
        self.backend.plan.update(profile="enforced-vm-v1", required_replay_lsn="0/20",
                                 source_lease_close_required=True)
        state = dict(self.state, phase="promoting")
        self.primary, self.peer = MagicMock(), MagicMock()
        with self.assertRaisesRegex(TransitionBlocked, "source_fence_wal_record_required"):
            self.backend.fenced(dict(state, phase="fencing"))
        with patch.object(self.backend, "_connection", side_effect=self.fake_connection), patch.object(
            self.backend, "_observe", return_value=self.candidate(),
        ):
            with self.assertRaisesRegex(TransitionBlocked, "source_fence_wal_record_required"):
                self.backend.promote(state)
            with self.assertRaisesRegex(TransitionBlocked, "promotion_replay_floor_not_reached"):
                self.backend.promote(state | {"source_fence_wal": 0x21})
        self.primary.execute.assert_not_called()

    def test_reconciled_primary_must_fork_from_the_source_at_the_recorded_wal(self):
        self.backend.plan.update(profile="enforced-vm-v1", required_replay_lsn="0/20",
                                 source_lease_close_required=True)
        state = dict(self.state, phase="promoting", source_fence_wal=0x30)
        promoted = dict(self.candidate(), recovery=False, actual_timeline=2)
        connection = MagicMock()
        for history in ("1\t0/2F\tbefore recorded WAL\n", "3\t0/30\twrong parent\n",
                        "# no parent\n", "1\t0/30\tvalid parent\n"):
            connection.execute.return_value.fetchone.side_effect = [
                ("123", 2, "0/80", "ha"), ("00000002.history", history),
            ]
            with patch.object(self.backend, "_connection", return_value=nullcontext(connection)):
                if history.startswith("1\t0/30"):
                    self.backend._promoted_history(promoted, state)
                else:
                    with self.assertRaisesRegex(TransitionBlocked, "promotion_fork_unconfirmed"):
                        self.backend._promoted_history(promoted, state)

    def test_disconnected_standby_uses_replay_timeline_without_substituting_flush(self):
        self.backend.plan["required_replay_lsn"] = "0/21"
        state = dict(self.state, phase="promoting")
        sql = MagicMock()
        sql.execute.return_value.fetchone.return_value = (
            dict(self.candidate(), actual_timeline=None, replay="0/20", flush=None),
        )
        replication = MagicMock()
        replication.execute.return_value.fetchone.return_value = ("123", 1, "0/30", "ha")
        with patch.object(self.backend, "_connection", return_value=nullcontext(replication)) as connect:
            status = self.backend._observe_runtime(sql, "B", state)
            self.assertEqual((status["actual_timeline"], status["replay"], status["flush"]), (1, "0/20", "0/30"))
            connect.assert_called_once_with("B", replication=True)
            replication.execute.assert_called_once_with("IDENTIFY_SYSTEM")
            for invalid in (("foreign", 1, "0/30", "ha"), ("123", 2, "0/30", "ha"),
                            ("123", 1, "0/1F", "ha"), ("123", 1, "0/30", "foreign")):
                replication.execute.return_value.fetchone.return_value = invalid
                with self.assertRaisesRegex(TransitionBlocked, "promotion_replay_identity_unconfirmed"):
                    self.backend._observe_runtime(sql, "B", state)

    def test_lost_promotion_response_is_reconciled_without_second_promote(self):
        self.backend.plan["required_replay_lsn"] = "0/20"
        self.primary, self.peer = MagicMock(), MagicMock()
        def execute(query, *args):
            if "pg_promote" in query:
                raise OSError("private transport detail")
            return MagicMock()
        self.primary.execute.side_effect = execute
        with patch.object(self.backend, "_connection", side_effect=self.fake_connection), patch.object(self.backend, "_observe", return_value=self.candidate()), patch.object(self.backend, "_read", return_value=self.observation()), patch.object(self.backend, "_local_metadata") as metadata:
            self.backend.promote(self.state)
        self.assertEqual(sum("pg_promote" in call.args[0] for call in self.primary.execute.call_args_list), 1)
        metadata.assert_called_once_with("B", self.state, promoted=True)

    def test_rejoin_staging_proves_closed_primary_between_fence_observations(self):
        state = dict(self.state, phase="rejoining")
        closed = dict(self.observation(), remaining=-1)
        with patch.object(self.backend, "_read", return_value=closed) as observe, patch.object(
            self.backend, "fenced", side_effect=[True, False],
        ):
            with self.assertRaisesRegex(TransitionBlocked, "positive_fence_required"):
                self.backend.assert_rejoin_pending(state)
            observe.assert_called_once_with("B", state)
        with patch.object(self.backend, "_read", return_value=closed | {"writes": True}):
            with self.assertRaisesRegex(TransitionBlocked, "rejoin_primary_authority_not_closed"):
                self.backend.assert_rejoin_pending(state)
        with patch.object(self.backend, "_read", return_value=closed):
            self.backend.assert_rejoin_pending(state)

    def test_rejoin_never_clones_or_restarts_fenced_resource(self):
        with patch.object(self.backend, "rejoined", return_value=False):
            with self.assertRaisesRegex(TransitionBlocked, "new_standby_required"):
                self.backend.rejoin(self.state)

    def test_rejoin_replication_check_rejects_lag_and_open_lease(self):
        state = dict(self.state, phase="rejoining")
        primary = dict(self.observation(), remaining=-1)
        peer = dict(primary, node="ha_A", recovery=True)
        for changes, reason in (
            ({"replay": "0/1F"}, "replacement_standby_replay_not_current"),
            ({"remaining": 2}, "rejoin_primary_authority_not_closed"),
            ({"recovery": False}, "strict_replication_required"),
        ):
            with self.subTest(changes=changes), patch.object(self.backend, "_read", side_effect=[primary, peer | changes]):
                with self.assertRaisesRegex(TransitionBlocked, reason):
                    self.backend.assert_rejoin_synchronized(state)
        with patch.object(self.backend, "_read", side_effect=[primary, peer]) as read:
            self.backend.assert_rejoin_synchronized(state)
            self.assertEqual([call.args[0] for call in read.call_args_list], ["B", "A"])

    def test_pair_rejects_async_wrong_timeline_role_revision_and_wrong_node(self):
        good, peer = self.observation(), self.observation("A", recovery=True)
        self.backend._pair(good, peer, self.state)
        for change in ({"sync_names": "ANY1(ha_a)"}, {"fsync": False}, {"actual_timeline": 1}, {"serving": ["A"]}):
            with self.subTest(change=change), self.assertRaises(TransitionBlocked):
                self.backend._pair(dict(good, **change), peer, self.state)
        with self.assertRaises(TransitionBlocked):
            self.backend._pair(good, dict(peer, revision=10), self.state)
        raw = MagicMock()
        raw.execute.return_value.fetchone.return_value = (dict(good, node="ha_A"),)
        with self.assertRaisesRegex(TransitionBlocked, "pg_identity_mismatch"):
            self.backend._observe(raw, "B", self.state)

    def test_fresh_proof_uses_postcommit_watermark_and_second_sync_commit(self):
        connections, observations = self.prepare_fresh()
        with connections, observations, patch.object(self.backend, "_commit", side_effect=lambda c: c.execute("COMMIT")):
            self.backend.validate(self.state)
        queries = [call.args[0] for call in self.primary.execute.call_args_list]
        first_commit, watermark = queries.index("COMMIT"), next(i for i, q in enumerate(queries) if "pg_current_wal_insert_lsn" in q)
        self.assertLess(first_commit, watermark)
        self.assertGreater(max(i for i, q in enumerate(queries) if q == "COMMIT"), watermark)
        self.assertEqual(queries.count("SET LOCAL synchronous_commit=on"), 2)
        locks = [call.args[1] for call in self.primary.execute.call_args_list if "pg_advisory_xact_lock" in call.args[0]]
        self.assertEqual(locks, [(ROLE_LOCK,), (ROLE_LOCK,)])
        self.assertIsNotNone(self.backend.validated)

    def test_repeated_renewals_bound_only_ephemeral_nonces_under_role_lock(self):
        retained = {"earlier-attempt", "earlier-attempt:flush"}
        seen_attempts = []
        for _ in range(3):
            connections, observations = self.prepare_fresh(lease=True, opened=True)
            original_execute = self.primary.execute.side_effect
            def execute(query, params=None):
                if query.startswith("DELETE FROM ha.controller_probes"):
                    self.assertEqual(len(params), 2)
                    self.assertEqual(params[1], params[0] + ":flush")
                    retained.intersection_update(params)
                    seen_attempts.append(params)
                elif query.startswith("INSERT INTO ha.controller_probes"):
                    retained.add(params[0])
                return original_execute(query, params)
            self.primary.execute.side_effect = execute
            with connections, observations, patch.object(self.backend, "_commit", side_effect=lambda c: c.execute("COMMIT")):
                self.backend.renew(self.state)
            self.assertEqual(retained, set(seen_attempts[-1]))
            self.assertEqual(len(retained), 2)
            calls = self.primary.execute.call_args_list
            queries = [call.args[0] for call in calls]
            cleanup = next(i for i, query in enumerate(queries) if query.startswith("DELETE FROM"))
            first_lock = next(i for i, query in enumerate(queries) if "pg_advisory_xact_lock" in query)
            first_insert = next(i for i, query in enumerate(queries) if query.startswith("INSERT INTO"))
            first_commit = queries.index("COMMIT")
            self.assertLess(first_lock, cleanup)
            self.assertLess(cleanup, first_insert)
            self.assertLess(first_insert, first_commit)
            self.assertEqual(sum(query.startswith("DELETE FROM") for query in queries), 1)
            self.assertEqual(sum(query.startswith("INSERT INTO ha.controller_probes") for query in queries), 2)
            self.assertEqual(queries.count("SELECT pg_advisory_xact_lock_shared(%s)"), 2)
            # Both observations still verify the exact current :flush nonce.
            for connection in (self.primary, self.peer):
                reads = [call for call in connection.execute.call_args_list if "SELECT generation FROM ha.controller_probes" in call.args[0]]
                self.assertEqual(reads[-1].args[1], (seen_attempts[-1][1],))
        self.assertEqual(len({attempt[0] for attempt in seen_attempts}), 3)

    def test_stale_replay_missing_nonce_or_deadline_never_arms_authority(self):
        for options in ({"peer_lsn": "0/1"}, {"bad_nonce": True}, {}):
            connections, observations = self.prepare_fresh(**options)
            clock = patch("ha_control.pg.time.monotonic", side_effect=[0, 3]) if not options else patch("ha_control.pg.time.monotonic", return_value=0)
            with connections, observations, clock, patch.object(self.backend, "_commit"):
                with self.assertRaisesRegex(TransitionBlocked, "fresh_replication_proof_required"):
                    self.backend.validate(self.state)
            self.assertIsNone(self.backend.validated)
            self.assertFalse(any("UPDATE ha.control" in c.args[0] for c in self.primary.execute.call_args_list))

    def test_two_host_observations_and_database_share_one_total_deadline(self):
        # Synthetic latency on the actual PG proof path, not native performance
        # evidence. Each component fits two seconds even when their sum does not.
        for host_seconds, accepted in ((0.7, True), (0.9, False)):
            with self.subTest(host_seconds=host_seconds):
                self.backend.validated = None
                connections, observations = self.prepare_fresh()
                clock = [100.0]
                phases = []
                def fenced(state):
                    self.assertEqual(state, self.state)
                    phases.append("host")
                    clock[0] += host_seconds
                    return True
                def commit(connection):
                    self.assertIs(connection, self.primary)
                    phases.append("sync_commit")
                    clock[0] += 0.2
                with connections, observations, patch.object(self.backend, "fenced", side_effect=fenced), patch.object(
                        self.backend, "_commit", side_effect=commit), patch(
                        "ha_control.pg.time.monotonic", side_effect=lambda: clock[0]):
                    if accepted:
                        self.backend._fresh_observation(self.state)
                        self.assertIsNotNone(self.backend.validated)
                    else:
                        with self.assertRaisesRegex(TransitionBlocked, "fresh_replication_proof_required"):
                            self.backend._fresh_observation(self.state)
                        self.assertIsNone(self.backend.validated)
                self.assertEqual(phases, ["host", "sync_commit", "sync_commit", "host"])
                self.assertFalse(any("UPDATE ha.control" in call.args[0]
                                     for call in self.primary.execute.call_args_list))

    def test_open_requires_fresh_validation_and_rechecks_every_replication_fact(self):
        with self.assertRaisesRegex(TransitionBlocked, "fresh_replication_proof_required"):
            self.backend.open(self.state)
        self.backend.validated = ("synthetic", 7, "B", 2, 100)
        connections, observations = self.prepare_fresh(lease=True)
        with connections, observations, patch("ha_control.pg.time.monotonic", return_value=100), patch.object(self.backend, "_commit"):
            self.backend.open(self.state)
        updates = [c for c in self.primary.execute.call_args_list if "UPDATE ha.control" in c.args[0]]
        self.assertEqual(len(updates), 1)
        self.assertIn("interval '5 seconds'", updates[0].args[0])
        self.assertFalse(any("pg_advisory_xact_lock_shared" in call.args[0] for call in self.primary.execute.call_args_list))

    def test_long_worker_shared_permit_coexists_with_expiry_only_renewal(self):
        connections, observations = self.prepare_fresh(lease=True, opened=True)
        original_execute = self.primary.execute.side_effect
        worker_shared_permit_held = True
        def execute(query, params=None):
            if worker_shared_permit_held and query == "SELECT pg_advisory_xact_lock(%s)":
                raise TransitionBlocked("exclusive_role_change_blocked_by_worker")
            return original_execute(query, params)
        self.primary.execute.side_effect = execute
        with connections, observations, patch.object(self.backend, "_commit", side_effect=lambda c: c.execute("COMMIT")):
            self.backend.renew(self.state)
        calls = self.primary.execute.call_args_list
        queries = [call.args[0] for call in calls]
        self.assertEqual(queries.count("SELECT pg_advisory_xact_lock_shared(%s)"), 2)
        self.assertNotIn("SELECT pg_advisory_xact_lock(%s)", queries)
        cleanup = next(i for i, query in enumerate(queries) if query.startswith("DELETE FROM ha.controller_probes"))
        self.assertLess(queries.index("SELECT pg_advisory_xact_lock_shared(%s)"), cleanup)
        update = next(call for call in calls if call.args[0].startswith("UPDATE ha.control"))
        assignment, predicate = update.args[0].split(" WHERE ")
        self.assertEqual(assignment, "UPDATE ha.control SET expires_at=clock_timestamp()+interval '5 seconds'")
        for column in ("cluster", "epoch", "revision", "primary_node", "partner", "timeline", "serving", "worker", "mode"):
            self.assertIn(column + "=%s", predicate)
        self.assertIn("writes_open=true AND history_safe=true AND expires_at>clock_timestamp()", predicate)
        self.assertEqual(update.args[1], ("synthetic", 7, 9, "B", "A", 2, ["B"], "B", "AS"))
        self.assertIsNotNone(self.backend.validated)

    def test_cli_checks_only_the_plan_bound_closed_lease(self):
        journal = self.root / "journal"
        journal.mkdir()
        Journal(journal / "controller-state.json").save(self.state)
        backend = MagicMock()
        backend.plan = dict(self.plan, profile="enforced-vm-v1", windows_transition_observer={},
                            transition_service_start={"target_timeline": 2, "lease_closure_check": True})
        backend.fence_adapter.plan_sha256 = "a"*64
        expected = {"plan_sha256": "a"*64, "generation": 7, "node_proofs": {"A": "b"*64, "B": "c"*64}}
        backend.observe_transition_lease_closed.return_value = expected
        args = SimpleNamespace(plan=str(self.root / "plan.json"), approval=str(self.root / "approval.json"),
                               journal=None, command="check-lease-closed")
        with patch.object(cli, "PgBackend", return_value=backend), patch.object(cli, "validate_bundle"):
            backend.plan["transition_service_start"]["lease_closure_check"] = 1
            with self.assertRaisesRegex(TransitionBlocked, "closure_unconfirmed"):
                cli.run(args)
            backend.observe_transition_lease_closed.assert_not_called()
            backend.plan["transition_service_start"]["lease_closure_check"] = True
            self.assertEqual(cli.run(args), {"status": "PASS", **expected})
            receipt = journal / ("lease-closure-" + "a"*64 + ".json")
            raw = receipt.read_bytes()
            data = json.loads(raw)
            self.assertGreater(data.pop("observed_at_ms"), 0)
            self.assertEqual(data, expected | {"schema_version": 1, "python_pid": os.getpid()})
            with self.assertRaisesRegex(TransitionBlocked, "receipt_unconfirmed"):
                cli.run(args)
            self.assertEqual(receipt.read_bytes(), raw)
        pin, generation, _ = backend.observe_transition_lease_closed.call_args.args
        self.assertEqual((pin, generation.primary, generation.partner, generation.timeline), ("a"*64, "B", "A", 2))
        backend.renew.assert_not_called()
        backend.open.assert_not_called()

    def test_transition_closure_observes_each_node_and_rejects_wrong_role(self):
        generation = ServingGeneration("B", 7, 2, 9)
        self.backend.plan["profile"] = "enforced-vm-v1"
        self.backend.fence_adapter.plan_sha256 = "a"*64
        rows = {
            node: ("ha_" + node, "123", node == "A", "synthetic", 7, "B", "A", 2, 10, True)
            for node in ("A", "B")
        }
        connections = {node: MagicMock() for node in rows}
        for node, row in rows.items():
            connections[node].execute.return_value.fetchone.return_value = row
        with patch.object(self.backend, "_connection", side_effect=lambda node: nullcontext(connections[node])):
            proof = self.backend.observe_transition_lease_closed("a"*64, generation, time.monotonic()+5)
            self.assertEqual(set(proof["node_proofs"]), {"A", "B"})
            self.assertNotEqual(proof["node_proofs"]["A"], proof["node_proofs"]["B"])
            for connection in connections.values():
                connection.execute.assert_any_call("SET TRANSACTION READ ONLY")
            connections["B"].execute.return_value.fetchone.return_value = rows["A"]
            with self.assertRaisesRegex(TransitionBlocked, "db_closure_unconfirmed"):
                self.backend.observe_transition_lease_closed("a"*64, generation, time.monotonic()+5)

    def test_transition_renewal_needs_observed_legacy_closure_before_pg_write(self):
        self.backend.plan["windows_transition_observer"] = {}
        rejected = MagicMock(side_effect=ValueError("observer unavailable"))
        for observer in (None, rejected):
            self.backend.transition_observer = observer
            with patch.object(self.backend, "_connection") as connection, patch.object(
                self.backend, "_best_effort_revoke",
            ) as revoke:
                with self.assertRaisesRegex(TransitionBlocked, "transition_legacy_closure_unverified"):
                    self.backend.renew(self.state)
                connection.assert_not_called()
                revoke.assert_called_once_with(self.state)

        self.backend.transition_observer = MagicMock(return_value="a"*64)
        connections, observations = self.prepare_fresh(lease=True, opened=True)
        with connections, observations, patch.object(self.backend, "_commit") as commit:
            self.backend.renew(self.state)
        self.backend.transition_observer.assert_called_once()
        self.assertEqual(commit.call_count, 2)

    def test_shared_renewal_never_reopens_closed_or_expired_authority(self):
        for opened, expired in ((False, False), (True, True)):
            connections, observations = self.prepare_fresh(lease=True, opened=opened)
            if expired:
                values = [dict(self.observation(opened=True), remaining=0), dict(self.observation("A", recovery=True, opened=True), remaining=0)]
                observations = patch.object(self.backend, "_observe", side_effect=values)
            with connections, observations, patch.object(self.backend, "_commit") as commit:
                with self.assertRaisesRegex(TransitionBlocked, "closed_authority_requires_explicit_open"):
                    self.backend.renew(self.state)
                commit.assert_not_called()
            writes = [call.args[0] for call in self.primary.execute.call_args_list if call.args[0].startswith(("UPDATE", "INSERT", "DELETE"))]
            self.assertTrue(all(query.startswith("UPDATE ha.control SET writes_open=false,history_safe=false") for query in writes))
            self.assertIsNone(self.backend.validated)

    def test_shared_renewal_requires_exact_current_authority_and_one_updated_row(self):
        connections, observations = self.prepare_fresh(lease=True, opened=True, update_rows=0)
        with connections, observations, patch.object(self.backend, "_commit") as commit:
            with self.assertRaisesRegex(TransitionBlocked, "authority_changed_during_renewal"):
                self.backend.renew(self.state)
            self.assertEqual(commit.call_count, 1)
        self.assertIsNone(self.backend.validated)
        connections, _ = self.prepare_fresh(lease=True, opened=True)
        values = [self.observation(opened=True), self.observation("A", recovery=True, opened=True),
                  dict(self.observation(opened=True), revision=10), dict(self.observation("A", recovery=True, opened=True), revision=10)]
        with connections, patch.object(self.backend, "_observe", side_effect=values), patch.object(self.backend, "_commit") as commit:
            with self.assertRaisesRegex(TransitionBlocked, "authority_changed_during_renewal"):
                self.backend.renew(self.state)
            commit.assert_not_called()

    def test_shared_role_lock_cannot_be_requested_for_initial_validation(self):
        with patch.object(self.backend, "_connection") as connection:
            with self.assertRaisesRegex(TransitionBlocked, "shared_lock_only_for_lease_renewal"):
                self.backend._fresh(self.state, shared=True)
            connection.assert_not_called()
        with patch.object(self.backend, "renew") as renew, patch.object(self.backend, "validate") as validate, patch.object(self.backend, "open") as reopen:
            self.backend.assert_ready(self.state)
            renew.assert_called_once_with(self.state)
            validate.assert_not_called()
            reopen.assert_not_called()

    def test_cli_refuses_any_journal_outside_the_verified_plan_directory(self):
        args = SimpleNamespace(plan=str(self.root / "plan.json"), approval=str(self.root / "approval.json"),
                               journal=str(self.root / "elsewhere.json"), command="advance")
        with patch("ha_control.cli.PgBackend") as backend:
            with self.assertRaisesRegex(TransitionBlocked, "plan_bound_journal_required"):
                cli.run(args)
            backend.assert_not_called()

    def test_authenticate_checks_both_isolated_endpoints_without_a_journal(self):
        backend = self.backend
        backend.plan = dict(self.plan, profile="enforced-vm-v1", windows_enforced={
            "config_profile": {"acceptance_scope": "isolated-predeployment", "operational_cutover_accepted": False},
        }, pg_nodes={node: {"dbname": "ha", "user": "postgres"} for node in ("A", "B")})
        connections = {node: MagicMock() for node in ("A", "B")}
        for node, connection in connections.items():
            connection.execute.return_value.fetchone.return_value = (
                "ha", "postgres", "ha_" + node, str(backend.plan["system_id"]))

        @contextmanager
        def connect(node):
            yield connections[node]

        with patch.object(backend, "_connection", side_effect=connect) as connect_mock:
            backend.authenticate_isolated_nodes()
            self.assertEqual([call.args for call in connect_mock.call_args_list], [("A",), ("B",)])
            for connection in connections.values():
                connection.transaction.assert_called_once()
                self.assertEqual(connection.execute.call_args_list[0].args, ("SET TRANSACTION READ ONLY",))
                self.assertEqual(connection.execute.call_count, 2)
            connections["B"].execute.return_value.fetchone.return_value = (
                "ha", "postgres", "ha_A", str(backend.plan["system_id"]))
            with self.assertRaisesRegex(TransitionBlocked, "pg_identity_mismatch"):
                backend.authenticate_isolated_nodes()
            backend.plan["windows_enforced"]["config_profile"]["operational_cutover_accepted"] = True
            with self.assertRaisesRegex(TransitionBlocked, "isolated_pg_authentication_required"):
                backend.authenticate_isolated_nodes()
            self.assertEqual(connect_mock.call_count, 4)

        (self.root / "journal").mkdir()
        args = SimpleNamespace(plan=str(self.root / "plan.json"), approval=str(self.root / "approval.json"),
                               journal=None, command="authenticate")
        service_backend = MagicMock()
        service_backend.plan = backend.plan
        with patch.object(cli, "PgBackend", return_value=service_backend), patch.object(cli, "validate_bundle"), \
                patch.object(cli, "Controller") as controller, patch.object(cli, "Journal") as journal:
            self.assertEqual(cli.run(args), {"status": "PASS", "authenticated_nodes": ["A", "B"], "authority_opened": False})
            service_backend.authenticate_isolated_nodes.assert_called_once()
            controller.assert_not_called()
            journal.assert_not_called()
        self.assertEqual(list((self.root / "journal").iterdir()), [])

    def test_production_transition_cannot_start_writes_without_observer_or_by_alternate_mode(self):
        (self.root / "journal").mkdir()
        backend = MagicMock()
        backend.plan = dict(self.plan, profile="enforced-vm-v1",
                            windows_enforced={"config_profile": {"acceptance_scope": "production"}})
        backend.fence_adapter.plan_sha256 = "a"*64
        args = SimpleNamespace(plan=str(self.root / "plan.json"), approval=str(self.root / "approval.json"),
                               journal=None, command="transition-serve")
        with patch.object(cli, "PgBackend", return_value=backend), patch.object(cli, "validate_bundle"), \
                patch.object(cli, "Controller") as controller, patch.object(cli, "Journal"):
            for command in ("advance", "serve", "transition-serve", "transition-rearm"):
                args.command = command
                with self.subTest(command=command), self.assertRaisesRegex(TransitionBlocked, "observer_required"):
                    cli.run(args)
            controller.assert_not_called()
            backend.plan["windows_transition_observer"] = {}
            args.command = "serve"
            with self.assertRaisesRegex(TransitionBlocked, "observer_required"):
                cli.run(args)
            controller.assert_not_called()
            del backend.plan["windows_transition_observer"]
            backend.plan["windows_enforced"] = {}
            args.command = "transition-serve"
            with self.assertRaisesRegex(TransitionBlocked, "acceptance_scope_required"):
                cli.run(args)
            controller.assert_not_called()
            args.command = "stage-rejoin"
            controller.return_value.advance.return_value = {"phase": "rejoining"}
            self.assertFalse(cli.run(args)["authority_opened"])
            controller.return_value.advance.assert_called_once_with(stop_at_rejoin=True)

    def test_observe_rejoin_stage_never_runs_fencing_or_promotion(self):
        (self.root / "journal").mkdir()
        backend = MagicMock(spec=cli.PgBackend)
        backend.plan = dict(self.plan, profile="enforced-vm-v1")
        backend.fence_adapter = SimpleNamespace(plan_sha256="a" * 64)
        state = {"phase": "rejoining", "attempts": 1}
        args = SimpleNamespace(plan=str(self.root / "plan.json"), approval=str(self.root / "approval.json"),
                               journal=None, command="observe-rejoin-stage")
        with patch.object(cli, "PgBackend", return_value=backend), patch.object(cli, "validate_bundle"), \
                patch.object(cli, "Controller") as controller, patch.object(cli, "Journal") as journal:
            journal.return_value.read.return_value = state
            observed = cli.run(args)
            controller.return_value.advance.assert_not_called()
        backend.assert_rejoin_pending.assert_called_once_with(state)
        backend.assert_rejoin_synchronized.assert_not_called()
        self.assertEqual(observed, cli.rejoin_stage_receipt(backend, state))
        self.assertFalse(observed["authority_opened"])

    def test_rejoin_receipt_is_launch_bound_and_never_overwritten(self):
        journal = self.root / "controller-state.json"
        plan_pin, probe = "a" * 64, "c" * 32
        proof = {"status": "PASS", "phase": "rejoining", "authority_opened": False,
                 "rejoin_stage_sha256": "b" * 64}
        cli.write_rejoin_receipt(journal, plan_pin, "observe-rejoin-stage", proof, probe)
        path = self.root / f"rejoin-result-{plan_pin}-{probe}.json"
        raw = path.read_bytes()
        record = json.loads(raw)
        self.assertEqual(record, proof | {"schema_version": 1, "plan_sha256": plan_pin, "probe": probe,
                                          "mode": "observe-rejoin-stage", "python_pid": os.getpid(),
                                          "observed_at_ms": record["observed_at_ms"]})
        with self.assertRaisesRegex(TransitionBlocked, "receipt_unconfirmed"):
            cli.write_rejoin_receipt(journal, plan_pin, "check-rejoin", proof, probe)
        self.assertEqual(path.read_bytes(), raw)

    def test_transition_rearm_holds_observer_and_reopens_only_a_ready_generation(self):
        journal = self.root / "journal"
        journal.mkdir()
        state_path = journal / "controller-state.json"
        backend = MagicMock()
        backend.plan = dict(self.plan, profile="enforced-vm-v1", windows_transition_observer={},
                            windows_enforced={"config_profile": {"acceptance_scope": "production"}})
        args = SimpleNamespace(plan=str(self.root / "plan.json"), approval=str(self.root / "approval.json"),
                               journal=None, command="transition-rearm")
        observer = MagicMock()

        def arm(state):
            self.assertEqual(state["phase"], "ready")
            self.assertIs(backend.transition_observer, observer)
            if sys.platform != "win32":
                from ha_control.controller import exclusive
                with self.assertRaisesRegex(TransitionBlocked, "controller_lock_unavailable"):
                    with exclusive(state_path.with_suffix(".lock")):
                        pass

        backend.arm.side_effect = arm
        with patch.object(cli, "PgBackend", return_value=backend), patch.object(cli, "validate_bundle"), \
                patch("ha_control.initial_lease_service.native_transition_observer", return_value=nullcontext(observer)), \
                patch.object(cli, "record_renewal"), patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt):
            Journal(state_path).save(self.state)
            with self.assertRaisesRegex(TransitionBlocked, "ready_transition_required"):
                cli.run(args)
            backend.arm.assert_not_called()
            Journal(state_path).save(dict(self.state, phase="ready"))
            with self.assertRaises(KeyboardInterrupt):
                cli.run(args)
        backend.arm.assert_called_once()
        backend.renew.assert_called_once()
        backend.close.assert_called_once()
        self.assertIsNone(backend.transition_observer)

    def test_enforced_cli_persists_only_in_precreated_mutable_journal_root(self):
        backend = MagicMock()
        backend.plan = dict(self.plan, profile="enforced-vm-v1")
        args = SimpleNamespace(plan=str(self.root / "plan.json"), approval=str(self.root / "approval.json"),
                               journal=None, command="initialize")
        with patch("ha_control.cli.PgBackend", return_value=backend), patch("ha_control.cli.validate_bundle"):
            with self.assertRaisesRegex(TransitionBlocked, "plan_bound_journal_required"):
                cli.run(args)
            self.assertFalse((self.root / "journal").exists())
            (self.root / "journal").mkdir(mode=0o700)
            result = cli.run(args)
            self.assertEqual(result["phase"], "fencing")
            self.assertTrue((self.root / "journal/controller-state.json").is_file())
            self.assertFalse((self.root / "controller-state.json").exists())
            args.journal = str(self.root / "controller-state.json")
            with self.assertRaisesRegex(TransitionBlocked, "plan_bound_journal_required"):
                cli.run(args)
            args.journal = None
            (self.root / "journal/controller-state.json").unlink()
            (self.root / "journal").rename(self.root / "other-journal")
            (self.root / "journal").symlink_to(self.root / "other-journal", target_is_directory=True)
            with self.assertRaisesRegex(TransitionBlocked, "plan_bound_journal_required"):
                cli.run(args)

    def test_cli_validates_bundle_after_private_guard_before_controller_or_db_call(self):
        args = SimpleNamespace(plan=str(self.root / "plan.json"), approval=str(self.root / "approval.json"),
                               journal=None, command="initialize")
        backend = MagicMock()
        backend.plan = self.plan
        with patch("ha_control.cli.PgBackend", return_value=backend), patch("ha_control.cli.Controller") as control:
            def reject(plan):
                backend.guard.return_value.__enter__.assert_called_once()
                control.assert_not_called()
                backend.bind.assert_not_called()
                raise TransitionBlocked("approved_controller_bundle_required")
            with patch("ha_control.cli.validate_bundle", side_effect=reject), self.assertRaises(TransitionBlocked):
                cli.run(args)
            control.assert_not_called()
            backend.bind.assert_not_called()

    def test_failed_open_proof_immediately_attempts_closed_authority(self):
        self.backend.validated = ("synthetic", 7, "B", 2, 100)
        with patch("ha_control.pg.time.monotonic", return_value=100), patch.object(self.backend, "_fresh", side_effect=TransitionBlocked("fresh_replication_proof_required")), patch.object(self.backend, "_local_metadata") as close:
            with self.assertRaisesRegex(TransitionBlocked, "fresh_replication_proof_required"):
                self.backend.open(self.state)
        close.assert_called_once_with("B", self.state)

    def test_lost_guard_during_renewal_attempts_revocation_and_clears_cached_proof(self):
        connections, observations = self.prepare_fresh(lease=True, opened=True)
        self.backend.validated = ("synthetic", 7, "B", 2, 100)
        with connections, observations, patch.object(self.backend, "fenced", side_effect=[True, False]), patch.object(self.backend, "_local_metadata_write") as close:
            with self.assertRaisesRegex(TransitionBlocked, "fresh_replication_proof_required"):
                self.backend.renew(self.state)
        close.assert_called_once_with("B", self.state)
        self.assertIsNone(self.backend.validated)

    def test_transition_heartbeat_records_only_postcommit_live_renewal(self):
        connections, observations = self.prepare_fresh(lease=True, opened=True)
        state = dict(self.state, phase="ready")
        self.fence.plan_sha256, self.fence.instance = "a"*64, "b"*64
        journal = Journal(self.root / "controller-state.json")
        journal.save(state)
        with connections, observations, patch("ha_control.pg.time.time_ns", return_value=10_000_000_000):
            proof = self.backend.renew(state)
            self.assertEqual(proof["sampled_at_ms"], 10000)
            self.assertEqual(proof["valid_until_ms"], 14000)
            cli.record_renewal(self.backend, journal, state, 1, proof)
            heartbeat = journal.read()["lease_heartbeat"]
            self.assertEqual((heartbeat["epoch"], heartbeat["timeline"], heartbeat["revision"]), (7, 2, 9))
            self.assertEqual(heartbeat["broker_instance_sha256"], "b"*64)
            self.assertEqual(heartbeat["python_pid"], os.getpid())
            with self.assertRaisesRegex(TransitionBlocked, "transition_lease_heartbeat_unconfirmed"):
                cli.record_renewal(self.backend, journal, state, 2, proof | {"valid_until_ms": 9000})
            self.assertEqual(journal.read()["lease_heartbeat"], heartbeat)

    def test_transition_service_removes_heartbeat_before_closing_lease(self):
        backend = MagicMock()
        backend.fence_adapter.plan_sha256, backend.fence_adapter.instance = "a"*64, "b"*64
        backend.renew.return_value = {"epoch": 7, "timeline": 2, "revision": 9, "sampled_at_ms": 10000,
                                      "valid_until_ms": 14000, "postcommit_nonce_sha256": "c"*64}
        state = dict(self.state, phase="ready")
        journal = Journal(self.root / "controller-state.json")
        journal.save(state)

        def inspect_active_then_stop(_):
            self.assertEqual(journal.read()["lease_heartbeat"]["sequence"], 1)
            raise KeyboardInterrupt

        def close_after_unpublish(_):
            self.assertNotIn("lease_heartbeat", journal.read())

        backend.close.side_effect = close_after_unpublish
        with patch("ha_control.cli.time.time_ns", return_value=10_000_000_000), patch(
            "ha_control.cli.time.sleep", side_effect=inspect_active_then_stop,
        ):
            with self.assertRaises(KeyboardInterrupt):
                cli.serve_ready(backend, state, heartbeat_journal=journal)
        backend.close.assert_called_once_with(state)
        self.assertNotIn("lease_heartbeat", journal.read())

    def test_guard_unavailable_before_renewal_does_not_prevent_safe_revocation(self):
        with patch.object(self.backend, "fenced", side_effect=TransitionBlocked("windows_guard_lost")), patch.object(self.backend, "_local_metadata_write") as close:
            with self.assertRaisesRegex(TransitionBlocked, "windows_guard_lost"):
                self.backend.renew(self.state)
            close.assert_called_once_with("B", self.state)
            close.reset_mock()
            self.backend._local_metadata("B", self.state)
            close.assert_called_once_with("B", self.state)

    def test_failed_revocation_preserves_original_failure_and_never_reports_success(self):
        with patch.object(self.backend, "fenced", side_effect=TransitionBlocked("windows_guard_lost")), patch.object(self.backend, "_local_metadata_write", side_effect=OSError("unavailable")):
            with self.assertRaisesRegex(TransitionBlocked, "windows_guard_lost"):
                self.backend.renew(self.state)
        self.assertIsNone(self.backend.validated)

    def test_promoted_history_publication_requires_post_guard_and_whole_deadline(self):
        for answers, clock in (([True, False], [10]), ([True, True], [10, 12.001])):
            with patch.object(self.backend, "fenced", side_effect=answers), patch.object(self.backend, "_local_metadata_write") as write, patch("ha_control.pg.time.monotonic", side_effect=clock):
                with self.assertRaisesRegex(TransitionBlocked, "fresh_legacy_guard_required"):
                    self.backend._local_metadata("B", self.state, promoted=True)
                self.assertEqual(write.call_count, 2)
                self.assertEqual(write.call_args_list[-1].kwargs, {})

    def test_only_durable_promoting_phase_can_publish_the_new_timeline(self):
        self.primary, self.peer = MagicMock(), MagicMock()
        newly_promoted = dict(self.candidate(), recovery=False, actual_timeline=2)
        with patch.object(self.backend, "_connection", side_effect=self.fake_connection), patch.object(self.backend, "_observe", return_value=newly_promoted):
            self.backend._local_metadata("B", dict(self.state, phase="promoting"), promoted=True)
        self.assertTrue(any("SET primary_node=" in call.args[0] for call in self.primary.execute.call_args_list))
        self.primary.reset_mock()
        with patch.object(self.backend, "_connection", side_effect=self.fake_connection), patch.object(self.backend, "_observe", return_value=newly_promoted):
            with self.assertRaisesRegex(TransitionBlocked, "recorded_primary_history_required"):
                self.backend._local_metadata("B", dict(self.state, phase="ready"), promoted=True)
        updates = [call.args[0] for call in self.primary.execute.call_args_list if call.args[0].startswith("UPDATE")]
        self.assertEqual(len(updates), 1)
        self.assertIn("SET writes_open=false,history_safe=false", updates[0])
        self.assertNotIn("primary_node=", updates[0])
        self.primary.reset_mock()
        with patch.object(self.backend, "_connection", side_effect=self.fake_connection), patch.object(self.backend, "_observe", return_value=self.observation()):
            self.backend._local_metadata("B", dict(self.state, phase="ready"), promoted=True)
        updates = [call.args[0] for call in self.primary.execute.call_args_list if call.args[0].startswith("UPDATE")]
        self.assertEqual(len(updates), 1)
        self.assertIn("SET writes_open=false,history_safe=false", updates[0])
        self.assertNotIn("primary_node=", updates[0])
        self.assertNotIn("timeline=", updates[0])
        self.assertNotIn("epoch=", updates[0])

    def test_recorded_later_phase_closes_unexpected_fork_without_adopting_it(self):
        for phase in ("ready", "rejoining", "validating"):
            for changes in ({"actual_timeline": 3}, {"epoch": 6}, {"primary": "A"}):
                with self.subTest(phase=phase, changed=list(changes)), patch.object(self.backend, "_read", return_value=dict(self.observation(), **changes)), patch.object(self.backend, "_local_metadata") as metadata:
                    with self.assertRaisesRegex(TransitionBlocked, "recorded_primary_history_required"):
                        self.backend.close(dict(self.state, phase=phase))
                    metadata.assert_called_once_with("B", dict(self.state, phase=phase))
        with patch.object(self.backend, "_read", return_value=dict(self.observation(), actual_timeline=3)), patch.object(self.backend, "_local_metadata") as metadata, patch.object(self.backend, "validate") as validate, patch.object(self.backend, "open") as reopen:
            with self.assertRaisesRegex(TransitionBlocked, "recorded_primary_history_required"):
                self.backend.arm(dict(self.state, phase="ready"))
            metadata.assert_called_once_with("B", dict(self.state, phase="ready"))
            validate.assert_not_called()
            reopen.assert_not_called()

    def test_explicit_arm_renews_an_already_open_recorded_generation(self):
        with patch.object(self.backend, "_read", return_value=self.observation(opened=True)), patch.object(self.backend, "renew") as renew, patch.object(self.backend, "close") as close, patch.object(self.backend, "validate") as validate, patch.object(self.backend, "open") as reopen:
            self.backend.arm(dict(self.state, phase="ready"))
            renew.assert_called_once_with(dict(self.state, phase="ready"))
            close.assert_not_called()
            validate.assert_not_called()
            reopen.assert_not_called()

    def test_explicit_arm_reopens_expired_generation_with_exclusive_fresh_proofs(self):
        connections, _ = self.prepare_fresh(lease=True)
        closed = [self.observation(), self.observation("A", recovery=True)]
        opened = [self.observation(opened=True), self.observation("A", recovery=True, opened=True)]
        sequence = closed * 4 + closed * 3 + opened
        events = []
        original_validate, original_open = self.backend.validate, self.backend.open
        def validate(state):
            events.append("validate")
            original_validate(state)
        def reopen(state):
            events.append("open")
            original_open(state)
        with connections, patch.object(self.backend, "_observe", side_effect=deepcopy(sequence)), patch.object(self.backend, "_read", return_value=dict(self.observation(opened=True), remaining=-1)), patch.object(self.backend, "close", side_effect=lambda state: events.append("close")), patch.object(self.backend, "validate", side_effect=validate), patch.object(self.backend, "open", side_effect=reopen), patch.object(self.backend, "_commit"):
            self.backend.arm(dict(self.state, phase="ready"))
        self.assertEqual(events, ["close", "validate", "open"])
        queries = [call.args[0] for call in self.primary.execute.call_args_list]
        self.assertEqual(queries.count("SELECT pg_advisory_xact_lock(%s)"), 4)
        self.assertNotIn("SELECT pg_advisory_xact_lock_shared(%s)", queries)
        self.assertEqual(sum(query.startswith("UPDATE ha.control SET serving=") for query in queries), 1)

    def test_cli_arm_is_explicit_serve_only_and_keeps_same_guard_and_lock(self):
        from ha_control.controller import Journal, exclusive
        path = self.root / "controller-state.json"
        Journal(path).save(dict(self.state, phase="ready"))
        args = SimpleNamespace(plan=str(self.root / "plan.json"), approval=str(self.root / "approval.json"), journal=None, command="serve", arm=True)
        events = []
        def action(name):
            def call(state):
                events.append(name)
                self.assertGreater(self.backend.depth, 1)
                self.assertEqual(self.fence.enters, 1)
                if sys.platform != "win32":
                    with self.assertRaisesRegex(TransitionBlocked, "controller_lock_unavailable"):
                        with exclusive(path.with_suffix(".lock")):
                            pass
            return call
        with patch("ha_control.cli.validate_bundle"), patch("ha_control.cli.PgBackend", return_value=self.backend), patch.object(self.backend, "arm", side_effect=action("arm")) as arm, patch.object(self.backend, "renew", side_effect=action("renew")), patch.object(self.backend, "close", side_effect=action("close")), patch("ha_control.cli.time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                cli.run(args)
            arm.assert_called_once()
        self.assertEqual(events, ["arm", "renew", "close"])
        for command in ("initialize", "advance"):
            with patch("ha_control.cli.PgBackend") as backend:
                with self.assertRaisesRegex(TransitionBlocked, "arm_only_for_serve"):
                    cli.run(SimpleNamespace(**dict(vars(args), command=command)))
                backend.assert_not_called()

    def test_default_serve_does_not_rearm_an_expired_generation(self):
        from ha_control.controller import Journal
        path = self.root / "controller-state.json"
        Journal(path).save(dict(self.state, phase="ready"))
        args = SimpleNamespace(plan=str(self.root / "plan.json"), approval=str(self.root / "approval.json"), journal=None, command="serve")
        with patch("ha_control.cli.validate_bundle"), patch("ha_control.cli.PgBackend", return_value=self.backend), patch.object(self.backend, "renew", side_effect=TransitionBlocked("closed_authority_requires_explicit_open")), patch.object(self.backend, "arm") as arm, patch.object(self.backend, "close"):
            with self.assertRaisesRegex(TransitionBlocked, "closed_authority_requires_explicit_open"):
                cli.run(args)
            arm.assert_not_called()

    def test_serve_keeps_host_guard_and_controller_lock_until_lease_is_closed(self):
        from ha_control.controller import Journal, exclusive
        path = self.root / "controller-state.json"
        Journal(path).save(dict(self.state, phase="ready"))
        args = SimpleNamespace(plan=str(self.root / "plan.json"), approval=str(self.root / "approval.json"), journal=None, command="serve")
        def ready(state):
            self.assertGreater(self.backend.depth, 1)
            self.assertEqual(self.fence.enters, 1)
            if sys.platform != "win32":
                with self.assertRaisesRegex(TransitionBlocked, "controller_lock_unavailable"):
                    with exclusive(path.with_suffix(".lock")):
                        pass
        with patch("ha_control.cli.validate_bundle"), patch("ha_control.cli.PgBackend", return_value=self.backend), patch.object(self.backend, "renew", side_effect=ready), patch.object(self.backend, "close") as close, patch("ha_control.cli.time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                cli.run(args)
        close.assert_called_once()
        self.assertEqual(self.backend.depth, 1)

    def test_cli_does_not_print_unknown_sql_or_transport_exception(self):
        import io
        output = io.StringIO()
        with patch("ha_control.cli.run", side_effect=OSError("private endpoint credentials and SQL")), patch("sys.stdout", output):
            code = cli.main(["advance", "--plan", str(self.root / "plan.json"), "--approval", str(self.root / "approval.json")])
        self.assertEqual(code, 1)
        self.assertNotIn("private endpoint", output.getvalue())
        self.assertIn("controller_outcome_unknown", output.getvalue())

    def enforced_plan(self):
        import hashlib
        from ha_control.prepare import EFFECTS_SOURCES, TARGETS, effects_runtime_sha256
        from ha_control.pg import ENFORCED_SERVICES
        services = sorted(ENFORCED_SERVICES)
        sources = {path:hashlib.sha256((ROOT/'infra/k8s'/TARGETS[path]).read_bytes()).hexdigest()
                   for path in EFFECTS_SOURCES}
        self.plan.update(profile='enforced-vm-v1',schema_version=2,source_commit='c'*40, required_replay_lsn='0/10',
                         pg_enforcement={'definition_sha256':hashlib.sha256((ROOT/'infra/k8s/ha_runtime/enforcement.sql').read_bytes()).hexdigest(),
                            'effects':{'code_sha256':effects_runtime_sha256(sources),'broker_role':'ha_broker'},
                            'role_sets':{node:{service:'app_'+str(index)+'_'+node.lower() for index,service in enumerate(services)} for node in ('A','B')},
                            'namespace_tables':{'s_'+service.replace('-','_')+'_main':{'service':service,'tables':['shared_state']} for service in services}})
        self.fence.plan_sha256='d'*64
        return self.plan

    def test_enforced_plan_requires_exact_effects_closure_broker_and_raw_plan_pin(self):
        plan=self.enforced_plan()
        self.backend._plan()
        for replacement in ({}, {'broker_role':'ha_broker'}, {'code_sha256':'0'*64,'broker_role':'ha_broker'},
                            dict(plan['pg_enforcement']['effects'],broker_role='bad;role')):
            original=plan['pg_enforcement']['effects'];plan['pg_enforcement']['effects']=replacement
            with self.assertRaises(TransitionBlocked):self.backend._plan()
            plan['pg_enforcement']['effects']=original
        del self.fence.plan_sha256
        with self.assertRaisesRegex(TransitionBlocked,'enforced_effects_plan_required'):self.backend._plan()

    def test_effects_observation_rejects_missing_storage_and_every_binding_mismatch(self):
        from ha_runtime import effects
        self.enforced_plan()
        expected=('synthetic','123','c'*40,'d'*64,'ha_broker')
        raw=MagicMock();raw.execute.return_value.fetchall.return_value=[expected]
        with patch.object(effects,'validate_catalog') as validate:
            self.backend._effects_observation(raw)
            self.assertEqual(validate.call_args.args[1].plan_sha256,'d'*64)
            for index in range(5):
                mismatch=list(expected);mismatch[index]='different'
                raw.execute.return_value.fetchall.return_value=[tuple(mismatch)]
                with self.assertRaisesRegex(TransitionBlocked,'external_effect_controller_binding_mismatch'):
                    self.backend._effects_observation(raw)
        with patch.object(effects,'validate_catalog',side_effect=OSError('missing ledger')):
            with self.assertRaisesRegex(TransitionBlocked,'external_effect_controller_catalog_unverified'):
                self.backend._effects_observation(raw)

    def test_effect_catalog_and_binding_failures_record_phase_and_node(self):
        from ha_runtime import effects

        class QueryCanceled(Exception):
            sqlstate = '57014'

        self.enforced_plan()
        raw = MagicMock()
        error = QueryCanceled('private query detail')
        with patch.object(effects, 'validate_catalog', side_effect=error):
            with self.assertRaisesRegex(TransitionBlocked, '^external_effect_controller_catalog_unverified$') as caught:
                self.backend._effects_observation(raw, 'A')
        self.assertEqual(caught.exception.failure_code, 'initial_pg_a_effects_catalog_canceled')

        raw.execute.side_effect = error
        with patch.object(effects, 'validate_catalog'):
            with self.assertRaisesRegex(TransitionBlocked, '^external_effect_controller_catalog_unverified$') as caught:
                self.backend._effects_observation(raw, 'B')
        self.assertEqual(caught.exception.failure_code, 'initial_pg_b_effects_binding_canceled')

    def test_real_observe_requires_effects_catalog_even_for_closed_authority(self):
        self.enforced_plan()
        raw=MagicMock()
        with patch.object(self.backend,'_enforced_observation'), patch.object(self.backend,'_effects_observation',side_effect=TransitionBlocked('missing_effects')) as observe:
            with self.assertRaisesRegex(TransitionBlocked,'missing_effects'):
                self.backend._observe(raw,'B',self.state)
            observe.assert_called_once_with(raw, 'B')
        raw.execute.assert_not_called()

    def effect_connections(self, *, unresolved=None, peer_unresolved=None, replay='0/20', **options):
        connections,observations=self.prepare_fresh(**options)
        for connection,value in ((self.primary,unresolved),(self.peer,peer_unresolved)):
            original=connection.execute.side_effect
            def execute(query,params=None,original=original,value=value):
                if 'SELECT pg_last_wal_replay_lsn()' in query:
                    return SimpleNamespace(fetchone=lambda:(replay,))
                if 'SELECT 1 FROM ha_effects.ledger' in query:
                    # Represent each actual persisted state named by the query.
                    found=value is not None and "'"+value+"'" in query
                    return SimpleNamespace(fetchone=lambda:(1,) if found else None)
                return original(query,params)
            connection.execute.side_effect=execute
        return connections,observations

    def test_initial_open_rejects_any_unresolved_state_on_either_node_before_update(self):
        self.enforced_plan()
        for state in ('pending','inflight','uncertain'):
            for where in ('unresolved','peer_unresolved'):
                connections,observations=self.effect_connections(lease=True,**{where:state})
                with connections,observations,patch.object(self.backend,'_commit'):
                    with self.assertRaisesRegex(TransitionBlocked,'external_effect_controller_unresolved'):
                        self.backend._fresh_observation(self.state,lease=True)
                queries=[call.args[0] for call in self.primary.execute.call_args_list]
                self.assertFalse(any('UPDATE ha.control SET serving' in query for query in queries))
                self.assertTrue(any('SELECT 1 FROM ha_effects.ledger' in call.args[0] for call in self.peer.execute.call_args_list))

    def test_completed_open_passes_only_after_exclusive_lock_and_peer_watermark(self):
        self.enforced_plan()
        connections,observations=self.effect_connections(lease=True,unresolved='completed',peer_unresolved='completed')
        with connections,observations,patch.object(self.backend,'_commit'):
            self.backend._fresh_observation(self.state,lease=True)
        queries=[call.args[0] for call in self.primary.execute.call_args_list]
        gate=next(index for index,query in enumerate(queries) if 'SELECT 1 FROM ha_effects.ledger' in query)
        locks=[index for index,query in enumerate(queries) if 'pg_advisory_xact_lock(%s)' in query]
        opening=next(index for index,query in enumerate(queries) if 'UPDATE ha.control SET serving' in query)
        self.assertLess(locks[-1],gate);self.assertLess(gate,opening)
        connections,observations=self.effect_connections(lease=True,replay='0/0')
        with connections,observations,patch.object(self.backend,'_commit'):
            with self.assertRaisesRegex(TransitionBlocked,'external_effect_controller_peer_unproven'):
                self.backend._fresh_observation(self.state,lease=True)
        self.assertFalse(any('UPDATE ha.control SET serving' in call.args[0] for call in self.primary.execute.call_args_list))

    def test_shared_renew_and_closed_validation_do_not_gate_normal_inflight(self):
        self.enforced_plan()
        for shared,lease in ((True,True),(False,False)):
            connections,observations=self.effect_connections(lease=lease,opened=shared,unresolved='inflight',peer_unresolved='pending')
            with connections,observations,patch.object(self.backend,'_commit'),patch.object(self.backend,'_effects_open_gate') as gate:
                self.backend._fresh_observation(self.state,lease=lease,shared=shared)
                gate.assert_not_called()
            self.assertFalse(any('SELECT 1 FROM ha_effects.ledger' in call.args[0] for call in self.primary.execute.call_args_list))




class InitialLeaseServiceTests(unittest.TestCase):
    def test_windows_heartbeat_replace_retries_transient_open_reader(self):
        from ha_control import controller

        move = MagicMock(side_effect=(0, 1))
        with patch.object(controller.ctypes, 'WinDLL', return_value=SimpleNamespace(MoveFileExW=move), create=True), \
                patch.object(controller.ctypes, 'get_last_error', return_value=5, create=True), \
                patch.object(controller.time, 'monotonic', return_value=1.0), patch.object(controller.time, 'sleep') as delay:
            controller.Journal._replace_windows('temporary', 'target', 1.15)
        self.assertEqual([call.args for call in move.call_args_list], [('temporary', 'target', 9)] * 2)
        delay.assert_called_once_with(0.01)

    def test_windows_replace_preserves_fail_closed_for_other_errors_and_expired_budget(self):
        from ha_control import controller

        for error, deadline in ((5, None), (5, 1.0), (87, 1.15)):
            with self.subTest(error=error, deadline=deadline):
                move = MagicMock(return_value=0)
                with patch.object(controller.ctypes, 'WinDLL', return_value=SimpleNamespace(MoveFileExW=move), create=True), \
                        patch.object(controller.ctypes, 'get_last_error', return_value=error, create=True), \
                        patch.object(controller.time, 'monotonic', return_value=1.0), patch.object(controller.time, 'sleep') as delay:
                    with self.assertRaises(OSError) as failure:
                        controller.Journal._replace_windows('temporary', 'target', deadline)
                if deadline == 1.0:
                    self.assertIsInstance(failure.exception, TimeoutError)
                    move.assert_not_called()
                else:
                    self.assertEqual(failure.exception.errno, error)
                    move.assert_called_once()
                delay.assert_not_called()

    def test_native_directory_open_requests_only_metadata_rights(self):
        from ha_control import initial_windows_gate as gate
        from pathlib import PureWindowsPath

        native = gate._Native.__new__(gate._Native)
        path = PureWindowsPath(r'C:\ProgramData\N100HA-fixture')

        def open_directory(handle, mask, attributes, status, sharing, options):
            self.assertEqual((mask, sharing, options), (0x20080, 7, 0x200000))
            name = attributes._obj.name.contents
            self.assertEqual(name.buffer, '\\??\\' + str(path))
            self.assertEqual(name.maximum, name.length + 2)
            handle._obj.value = 42
            return 0

        native.nt_open = open_directory
        self.assertEqual(native._open_directory(path), 42)
        native.nt_open = MagicMock(return_value=-1)
        with self.assertRaises(TransitionBlocked):
            native._open_directory(path)

    def test_pinned_read_accepts_distinct_stable_handle_ctime_and_rejects_changes(self):
        from ha_control import initial_lease_service as service

        raw = b'{"generation":1}'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder).resolve() / 'intent.json'
            path.write_bytes(raw)
            named = path.lstat()
            fields = ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
            handle = SimpleNamespace(**{field: getattr(named, field) for field in fields})
            handle.st_ctime_ns += 1000
            digest = hashlib.sha256(raw).hexdigest()
            with patch.object(service.os, 'fstat', return_value=handle):
                self.assertEqual(service._read_pinned(path, digest),
                                 ({'generation': 1}, (named.st_dev, named.st_ino)))
                with self.assertRaises(TransitionBlocked):
                    service._read_pinned(path, '0' * 64)
            changed = SimpleNamespace(**(vars(handle) | {'st_ctime_ns': handle.st_ctime_ns + 1}))
            with patch.object(service.os, 'fstat', side_effect=[handle, changed]):
                with self.assertRaises(TransitionBlocked):
                    service._read_pinned(path, digest)

    def test_persists_intent_before_initial_open_and_never_rearms_after_loss(self):
        from ha_control import initial_lease_service as service

        handoff = dict(attempt_id='a' * 64, legacy_plan_sha256='b' * 64,
                       restore_receipt_sha256='c' * 64, activation_intent_sha256='d' * 64)
        backend = SimpleNamespace(plan={'cluster': 'initial_test', 'initial_wsl_cutover': INITIAL_CUTOVER,
                                        '_approval_action': 'ha-initial-wsl-cutover', 'initial_cutover_handoff': handoff},
                                  fence_adapter=SimpleNamespace(_installed_runtime=True, plan_sha256='e' * 64),
                                  _effects_deployment_binding=lambda: ('1' * 40, 'b' * 64))
        observer = MagicMock(spec=service.WindowsInitialObserver)
        lease = MagicMock()
        with tempfile.TemporaryDirectory() as folder:
            journal = Path(folder) / 'controller-state.json'

            def open_lease(_state):
                state = json.loads(journal.read_text())
                self.assertEqual((state['phase'], state['sequence']), ('opening', 0))
                lease.proof_started = time.monotonic()
                return 'f' * 64

            def fail_renewal(_state):
                state = json.loads(journal.read_text())
                self.assertEqual((state['phase'], state['sequence'], state['lease_proof_sha256']), ('serving', 1, 'f' * 64))
                raise service.LegacyResponseTimeout('postcommit')

            lease.open.side_effect = open_lease
            lease.renew.side_effect = fail_renewal
            lease.revoke.return_value = True
            with patch.object(service, 'InitialPgLease', return_value=lease), patch.object(service.time, 'sleep'):
                with self.assertRaisesRegex(TransitionBlocked, '^legacy_pipe_response_timeout$'):
                    service.serve_initial(backend, journal, observer)
                terminal = json.loads(journal.read_text())
                self.assertEqual(terminal['phase'], 'local_revoke_observed')
                self.assertEqual(terminal['failure_code'], 'legacy_pipe_response_timeout_postcommit')
                with self.assertRaisesRegex(TransitionBlocked, 'initial_service_reconciliation_required'):
                    service.serve_initial(backend, journal, observer)
        observer.assert_intent.assert_called_once()
        lease.open.assert_called_once()
        lease.revoke.assert_called_once()


class InitialPgLeaseTests(unittest.TestCase):
    def test_timeout_before_renewal_commit_keeps_existing_revocation_path(self):
        from ha_control.initial_pg_lease import LegacyResponseTimeout

        self.lease.open(self.state)
        self.lease.assert_legacy_closed = MagicMock(side_effect=TransitionBlocked('legacy_pipe_response_timeout'))
        with self.assertRaises(LegacyResponseTimeout) as failure:
            self.lease.renew(self.state)
        self.assertEqual(failure.exception.failure_code, 'legacy_pipe_response_timeout_precommit')
        self.assertEqual(self.revision, 3)
        self.assertFalse(self.opened)

    def test_single_catalog_cancel_retries_renewal_before_the_only_write(self):
        from ha_control.pg import PgUnavailable

        self.lease.open(self.state)
        self.trace.clear()
        original = self.backend._observe
        primary_reads = 0

        def cancel_first_primary_read(connection, node, state):
            nonlocal primary_reads
            if node == 'A':
                primary_reads += 1
                if primary_reads == 1:
                    raise PgUnavailable(node, 'effects_catalog', '57014')
            return original(connection, node, state)

        with patch.object(self.backend, '_observe', side_effect=cancel_first_primary_read):
            self.assertRegex(self.lease.renew(self.state), r'^[a-f0-9]{64}$')
        self.assertEqual(primary_reads, 2)
        self.assertEqual(self.trace.count(('connect', 'A')), 2)
        self.assertEqual(self.trace.count(('connect', 'B')), 2)
        self.assertEqual(self.trace.count(('closure',)), 4)
        self.assertEqual(sum(row[1].startswith('UPDATE ha.control') for row in self.trace if len(row) == 3), 1)
        self.assertTrue(self.opened)

    def test_repeat_catalog_cancel_revokes_without_a_third_renewal(self):
        from ha_control.pg import PgUnavailable

        self.lease.open(self.state)
        original = self.backend._observe

        def cancel_replica_read(connection, node, state):
            if node == 'B':
                raise PgUnavailable(node, 'effects_binding', '57014')
            return original(connection, node, state)

        with patch.object(self.backend, '_observe', side_effect=cancel_replica_read) as reads:
            with self.assertRaises(PgUnavailable):
                self.lease.renew(self.state)
        self.assertEqual([call.args[1] for call in reads.call_args_list].count('B'), 2)
        self.assertFalse(self.opened)
        self.assertEqual(self.revision, 3)

    def test_catalog_cancel_does_not_override_simultaneous_closure_or_peer_failure(self):
        from ha_control.pg import PgUnavailable

        for guard in ('closure', 'peer'):
            with self.subTest(guard=guard):
                self.setUp()
                self.lease.open(self.state)
                original = self.backend._observe

                def observe(connection, node, state):
                    if node == 'A':
                        raise PgUnavailable(node, 'effects_catalog', '57014')
                    value = original(connection, node, state)
                    return dict(value, remaining=0) if guard == 'peer' else value

                def closure_failure(state, deadline):
                    raise TransitionBlocked('fixture_closure_failed')

                if guard == 'closure':
                    self.lease.assert_legacy_closed = closure_failure
                reason = 'fixture_closure_failed' if guard == 'closure' else 'initial_lease_stamp_unverified'
                with patch.object(self.backend, '_observe', side_effect=observe) as reads:
                    with self.assertRaisesRegex(TransitionBlocked, reason):
                        self.lease.renew(self.state)
                self.assertEqual([call.args[1] for call in reads.call_args_list].count('A'), 1)
                self.assertFalse(self.opened)

    def test_initial_commit_cancellation_is_attributed_to_primary(self):
        class CommitCanceled(Exception):
            sqlstate = '57014'

        with patch.object(self.backend, '_commit', side_effect=CommitCanceled('private driver detail')):
            with self.assertRaisesRegex(TransitionBlocked, '^authenticated_pg_unavailable$') as error:
                self.lease._commit_primary(object())
        self.assertEqual(error.exception.failure_code, 'initial_pg_a_commit_canceled')

    def test_initial_pg_read_overlaps_closure_and_failure_blocks_writes(self):
        from threading import Event

        reading = Event()
        original = self.backend._observe

        def observe(connection, node, state):
            reading.set()
            return original(connection, node, state)

        def reject(state, deadline):
            self.assertTrue(reading.wait(timeout=1))
            raise TransitionBlocked('fixture_closure_failed')

        self.backend._observe = observe
        self.lease.assert_legacy_closed = reject
        with self.assertRaisesRegex(TransitionBlocked, 'fixture_closure_failed'):
            self.lease.open(self.state)
        self.assertEqual(self.revision, 1)
        self.assertFalse(self.probes)
        self.assertFalse(any(row[1].startswith(('DELETE', 'INSERT', 'UPDATE'))
                             for row in self.trace if len(row) == 3))

    def test_pair_observations_overlap_on_distinct_connections(self):
        from threading import Barrier

        barrier = Barrier(2, timeout=2)
        primary, standby = MagicMock(), MagicMock()
        expiry = datetime.now(timezone.utc)
        for connection in (primary, standby):
            connection.execute.return_value.fetchone.return_value = (expiry,)

        def observe(connection, node, state):
            self.assertIs(connection, primary if node == 'A' else standby)
            self.assertEqual(state, self.state)
            barrier.wait()
            return node

        self.backend._observe = observe
        with patch.object(self.lease, '_pair') as verify:
            self.assertEqual(self.lease._observe_pair(primary, standby, self.state, opened=False,
                                                       started=time.monotonic()),
                             ('A', 'B', expiry))
        verify.assert_called_once_with('A', 'B', self.state, opened=False)

    def test_observation_timeout_uses_remaining_cutover_budget_and_restores_default(self):
        expiry = datetime.now(timezone.utc)
        primary, standby = MagicMock(), MagicMock()
        for connection in (primary, standby):
            connection.execute.return_value.fetchone.return_value = (expiry,)
        self.backend._observe = MagicMock()

        from ha_control import initial_pg_lease
        for elapsed, expected_ms in ((0.5, 1200), (3.5, 500)):
            with self.subTest(elapsed=elapsed), patch.object(initial_pg_lease.time, 'monotonic', return_value=elapsed), \
                    patch.object(self.lease, '_pair'):
                self.lease._observe_pair(primary, standby, self.state, opened=False, started=0)
            for connection in (primary, standby):
                self.assertEqual([call.args[0] for call in connection.execute.call_args_list],
                                 [f'SET statement_timeout={expected_ms}', "SET statement_timeout='700ms'",
                                  'SELECT expires_at FROM ha.control WHERE singleton=1'])
                connection.reset_mock()
            self.backend._observe.reset_mock()

        with patch.object(initial_pg_lease.time, 'monotonic', return_value=4.0):
            with self.assertRaisesRegex(TransitionBlocked, 'initial_replication_deadline_exceeded'):
                self.lease._observe_pair(primary, standby, self.state, opened=False, started=0)
        primary.execute.assert_not_called()
        standby.execute.assert_not_called()
        self.backend._observe.assert_not_called()

    def setUp(self):
        self.cluster = 'initial_test'
        self.state = dict(cluster=self.cluster, target='A', old='B', timeline=1, generation=1)
        self.binding = dict(source_commit='a' * 40, plan_sha256='b' * 64)
        self.backend = object.__new__(PgBackend)
        self.backend.depth = 1
        self.backend.plan = dict(cluster=self.cluster, system_id='1234', node='A', generation=1, timeline=1,
                                 profile='enforced-vm-v1', _approval_action='ha-initial-wsl-cutover', source_commit='c' * 40,
                                 initial_wsl_cutover=dict(INITIAL_CUTOVER),
                                 pg_enforcement={'effects': {'deployment_binding': self.binding,
                                                             'broker_role': 'ha_effect_broker'}})
        self.backend.fence_adapter = SimpleNamespace(plan_sha256='d' * 64)
        self.backend._connection = self.connection
        self.backend._observe = self.observe
        self.backend._observe_runtime = self.observe
        self.backend._commit = self.commit
        self.backend._effects_open_gate = MagicMock()
        self.trace = []
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.expiry = self.now - timedelta(days=1)
        self.revision = 1
        self.opened = False
        self.pending = None
        self.probes = set()
        self.pending_probes = set()
        self.lost_commit = False
        self.hide_lease_on_b = False
        self.elapsed = 0
        self.over_budget = False
        self.lease = InitialPgLease(self.backend, self.legacy_closed)

    def legacy_closed(self, state, deadline):
        self.assertEqual(state, self.state)
        self.trace.append(('closure',))
        if self.over_budget and self.opened:
            self.elapsed = 4.1

    @contextmanager
    def connection(self, node):
        self.trace.append(('connect', node))
        try:
            yield SimpleNamespace(node=node, execute=lambda sql, parameters=(): self.execute(node, sql, parameters))
        finally:
            if node == 'A':
                self.pending = None
                self.pending_probes.clear()

    def observe(self, connection, node, state):
        self.assertEqual(connection.node, node)
        self.assertEqual(state, self.state)
        return dict(cluster=self.cluster, epoch=1, revision=self.revision, primary='A', partner='B',
                    timeline=1, actual_timeline=1, serving=['A'], worker='A', mode='AS', system_id='1234',
                    node='ha_' + node, recovery=node == 'B', sync_names='FIRST1(ha_b)' if node == 'A' else 'FIRST1(ha_a)',
                    sync_peer='ha_b' if node == 'A' else None, fsync=True, full_page_writes=True, writes=self.opened,
                    history=self.opened, remaining=(self.expiry - self.now).total_seconds(), flush='0/30', replay='0/30')

    def execute(self, node, sql, parameters):
        self.trace.append((node, sql, parameters))
        row, count = None, 0
        if sql == 'SELECT expires_at FROM ha.control WHERE singleton=1':
            row = (self.expiry,)
        elif sql == 'SELECT pg_current_wal_flush_lsn()::text':
            row = ('0/20',)
        elif sql.startswith('SELECT generation FROM ha.controller_probes'):
            nonce, = parameters
            visible = nonce in self.probes and not (node == 'B' and nonce.endswith(':lease') and self.hide_lease_on_b)
            row = (1,) if visible else None
        elif sql.startswith('INSERT INTO ha.controller_probes'):
            self.pending_probes.add(parameters[0])
        elif sql.startswith('DELETE FROM ha.controller_probes'):
            self.probes = {nonce for nonce in self.probes if not nonce.startswith('initial-a:')}
        elif sql.startswith('UPDATE ha.control'):
            expected = (self.cluster, self.revision, self.opened, self.opened, self.expiry, '1234')
            if parameters == expected:
                count = 1
                if sql.startswith('UPDATE ha.control SET revision=3'):
                    self.pending = (3, False, self.now - timedelta(seconds=1))
                else:
                    row = (self.now + timedelta(seconds=5),)
                    self.pending = (2, True, row[0])
        return SimpleNamespace(fetchone=lambda: row, rowcount=count)

    def commit(self, connection):
        connection.execute('COMMIT')
        self.probes.update(self.pending_probes)
        self.pending_probes.clear()
        if self.pending is not None:
            self.revision, self.opened, self.expiry = self.pending
            self.pending = None
            if self.opened and self.lost_commit:
                raise TransitionBlocked('fixture_commit_response_lost')

    def test_open_and_renew_preserve_initial_timeline_with_scoped_cas(self):
        self.backend._observe = MagicMock(wraps=self.observe)
        self.backend._observe_runtime = MagicMock(wraps=self.observe)
        self.assertRegex(self.lease.open(self.state), r'^[a-f0-9]{64}$')
        self.assertEqual(self.backend._observe.call_count, 4)
        self.assertEqual(self.backend._observe_runtime.call_count, 2)
        self.assertTrue(self.opened)
        self.assertEqual(self.revision, 2)
        self.backend._effects_open_gate.assert_called_once()
        queries = [row[1] for row in self.trace if row[0] == 'A' and len(row) == 3]
        self.assertEqual(queries.count('COMMIT'), 2)
        self.assertEqual(queries.count('SET LOCAL synchronous_commit=remote_apply'), 2)
        self.assertEqual(queries.count('SELECT pg_advisory_xact_lock(%s)'), 2)
        self.assertEqual(self.trace.count(('closure',)), 4)
        self.assertFalse(any('pg_promote' in query for query in queries))
        self.trace.clear()
        self.backend._observe.reset_mock()
        self.backend._observe_runtime.reset_mock()
        self.now += timedelta(seconds=1)
        self.assertRegex(self.lease.renew(self.state), r'^[a-f0-9]{64}$')
        self.assertEqual(self.backend._observe.call_count, 2)
        self.assertEqual(self.backend._observe_runtime.call_count, 2)
        queries = [row[1] for row in self.trace if row[0] == 'A' and len(row) == 3]
        self.assertEqual(queries.count('SELECT pg_advisory_xact_lock_shared(%s)'), 1)
        self.assertEqual(queries.count('COMMIT'), 1)
        self.assertEqual(queries.count('SET LOCAL synchronous_commit=remote_apply'), 1)
        _, _, nonce_args = next(row for row in self.trace if len(row) == 3 and row[1].startswith('INSERT INTO ha.controller_probes'))
        self.assertTrue(nonce_args[0].endswith(':lease'))
        self.assertIn(('B', 'SELECT generation FROM ha.controller_probes WHERE nonce=%s', nonce_args), self.trace)
        renew, = [query for query in queries if query.startswith('UPDATE ha.control')]
        self.assertTrue(renew.startswith("UPDATE ha.control SET expires_at=clock_timestamp()+interval '5 seconds' WHERE"))
        self.assertIn('expires_at=%s', renew)
        self.assertIn('expires_at>clock_timestamp()', renew)
        self.backend._effects_open_gate.assert_called_once()
        self.now += timedelta(seconds=6)
        with self.assertRaisesRegex(TransitionBlocked, 'initial_lease_stamp_unverified'):
            self.lease.renew(self.state)
        self.assertFalse(self.opened)
        self.assertEqual(self.revision, 3)
        with self.assertRaisesRegex(TransitionBlocked, 'initial_lease_reconciliation_required'):
            self.lease.open(self.state)

    def test_missing_replica_nonce_revokes_only_the_issued_expiry(self):
        self.hide_lease_on_b = True
        with self.assertRaisesRegex(TransitionBlocked, 'initial_replication_barrier_unconfirmed'):
            self.lease.open(self.state)
        self.assertFalse(self.opened)
        revoke, = [row for row in self.trace if len(row) == 3 and row[1].startswith('UPDATE ha.control SET revision=3')]
        self.assertEqual(revoke[2], (self.cluster, 2, True, True, self.now + timedelta(seconds=5), '1234'))
        self.assertIn('expires_at=%s', revoke[1])

    def test_missing_replica_renewal_nonce_revokes_the_new_stamp(self):
        self.lease.open(self.state)
        self.now += timedelta(seconds=1)
        self.trace.clear()
        self.hide_lease_on_b = True
        with self.assertRaisesRegex(TransitionBlocked, 'initial_replication_barrier_unconfirmed'):
            self.lease.renew(self.state)
        self.assertEqual(self.revision, 3)
        self.assertFalse(self.opened)
        attempted_stamps = [row[2][-2] for row in self.trace
                            if len(row) == 3 and row[1].startswith('UPDATE ha.control SET revision=3')]
        self.assertEqual(attempted_stamps, [self.now + timedelta(seconds=4), self.now + timedelta(seconds=5)])

    def test_renewal_reuses_only_a_fresh_closure_with_an_independent_owner_check(self):
        full = MagicMock(wraps=self.legacy_closed)
        owner = MagicMock(return_value=True)
        lease = InitialPgLease(self.backend, full, assert_exclusion_held=owner)
        with patch('ha_control.initial_pg_lease.time.monotonic', side_effect=lambda: self.elapsed):
            lease.open(self.state)
            self.assertEqual(full.call_count, 3)
            self.assertEqual(owner.call_count, 1)
            full.reset_mock()
            owner.reset_mock()
            self.now += timedelta(seconds=1)
            lease.renew(self.state)
            self.assertEqual(full.call_count, 1)
            self.assertEqual(owner.call_count, 2)

            def slow_sample(connection, node, state):
                if node == 'A':
                    self.elapsed = 2.1
                return self.observe(connection, node, state)

            self.backend._observe_runtime = slow_sample
            full.reset_mock()
            self.now += timedelta(seconds=1)
            lease.renew(self.state)
            self.assertEqual(full.call_count, 2)

    def test_legacy_closure_loss_after_barrier_blocks_authority_cas(self):
        def guard(state, deadline):
            self.legacy_closed(state, deadline)
            if self.backend._effects_open_gate.called:
                raise TransitionBlocked('fixture_legacy_reopened')

        lease = InitialPgLease(self.backend, guard)
        with self.assertRaisesRegex(TransitionBlocked, 'fixture_legacy_reopened'):
            lease.open(self.state)
        self.assertFalse(self.opened)
        self.assertEqual(self.revision, 1)
        self.assertEqual(self.trace.count(('closure',)), 2)
        self.assertFalse(any(len(row) == 3 and row[1].startswith('UPDATE ha.control') for row in self.trace))

    def test_native_open_has_time_for_a_second_independent_closure_proof(self):
        observe = self.lease.assert_legacy_closed

        def delayed_observation(state, deadline):
            observe(state, deadline)
            if self.opened:
                self.elapsed = 3.6
                self.assertGreater(deadline, self.elapsed)

        self.lease.assert_legacy_closed = delayed_observation
        with patch('ha_control.initial_pg_lease.time.monotonic', side_effect=lambda: self.elapsed):
            self.assertRegex(self.lease.open(self.state), r'^[a-f0-9]{64}$')
        self.assertTrue(self.opened)
        self.assertEqual(self.revision, 2)

    def test_closed_preparation_does_not_spend_the_initial_authority_window(self):
        def finish_closed_preparation(*_args):
            self.assertFalse(self.opened)
            self.elapsed = 3.2

        def observe_closure(state, deadline):
            self.legacy_closed(state, deadline)
            if self.opened:
                self.elapsed = 5.4
                self.assertGreater(deadline, self.elapsed)

        self.backend._effects_open_gate.side_effect = finish_closed_preparation
        lease = InitialPgLease(self.backend, observe_closure, assert_exclusion_held=lambda *_: True)
        with patch('ha_control.initial_pg_lease.time.monotonic', side_effect=lambda: self.elapsed):
            lease.open(self.state)
        self.assertEqual(lease.proof_started, 3.2)
        self.assertEqual(self.revision, 2)

    def test_expired_closed_preparation_never_opens_the_lease(self):
        def expire_closed_preparation(*_args):
            self.elapsed = 4.1

        self.backend._effects_open_gate.side_effect = expire_closed_preparation
        with patch('ha_control.initial_pg_lease.time.monotonic', side_effect=lambda: self.elapsed):
            with self.assertRaisesRegex(TransitionBlocked, 'initial_replication_deadline_exceeded'):
                self.lease.open(self.state)
        self.assertIsNone(self.lease.proof_started)
        self.assertEqual(self.revision, 1)

    def test_lost_commit_or_whole_proof_budget_cannot_report_open(self):
        for option, reason in (('lost_commit', 'fixture_commit_response_lost'),
                               ('over_budget', 'initial_replication_deadline_exceeded')):
            with self.subTest(option=option):
                self.setUp()
                setattr(self, option, True)
                with patch('ha_control.initial_pg_lease.time.monotonic', side_effect=lambda: self.elapsed):
                    with self.assertRaisesRegex(TransitionBlocked, reason):
                        self.lease.open(self.state)
                self.assertFalse(self.opened)
                self.assertEqual(self.revision, 3)

    def test_explicit_initial_mode_and_deployment_binding_are_both_required(self):
        self.assertEqual(self.backend._effects_deployment_binding(), ('a' * 40, 'b' * 64))
        self.assertNotEqual(self.binding['plan_sha256'], self.backend.fence_adapter.plan_sha256)
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [(
            self.cluster, '1234', self.binding['source_commit'], self.binding['plan_sha256'], 'ha_effect_broker',
        )]
        with patch('ha_runtime.effects.validate_catalog') as validate:
            self.backend._effects_observation(connection)
            self.assertEqual(validate.call_args.args[1].plan_sha256, self.binding['plan_sha256'])
        self.backend.plan.pop('initial_wsl_cutover')
        with self.assertRaisesRegex(TransitionBlocked, 'initial_cutover_approval_required'):
            self.lease.open(self.state)
        self.backend.plan['initial_wsl_cutover'] = dict(INITIAL_CUTOVER)
        self.backend.plan['pg_enforcement']['effects'] = {}
        self.assertEqual(self.backend._effects_deployment_binding(), ('c' * 40, 'd' * 64))
        with self.assertRaisesRegex(TransitionBlocked, 'initial_cutover_deployment_binding_required'):
            self.lease.open(self.state)
        self.backend.plan['pg_enforcement']['effects']['deployment_binding'] = dict(self.binding, plan_sha256='bad')
        with self.assertRaisesRegex(TransitionBlocked, 'enforced_effects_plan_required'):
            self.lease.open(self.state)
        self.assertEqual(self.trace, [])


class IsolatedLeaseRehearsalTests(unittest.TestCase):
    def test_fixture_observer_rejects_replayed_receipt(self):
        import hmac
        from ha_control.scm_isolated_lease_probe import FixtureObserver

        key = bytes(range(32))
        saved = None

        def request(envelope, _deadline):
            nonlocal saved
            challenge = envelope["challenge"]
            if saved is None:
                result = {"held": True}
                body = {"challenge": challenge, "result": result}
                raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("ascii")
                saved = dict(body, mac=hmac.digest(key, raw, "sha256").hex())
            return saved

        observer = FixtureObserver(request, "a" * 32, key)
        self.assertTrue(observer.exclusion({}, time.monotonic() + 1))
        with self.assertRaises(ValueError):
            observer.exclusion({}, time.monotonic() + 1)

    def test_rehearsal_requires_post_401_renewal_then_revokes(self):
        from ha_control import isolated_lease_rehearsal as rehearsal

        sid = "S-1-5-80-1-2-3-4-5"
        start = datetime.now(timezone.utc)
        expiry = start + timedelta(seconds=3)
        backend = object.__new__(rehearsal.DisposableLeaseBackend)
        backend.plan = {"system_id": "123", "cluster": "ha_lease_rehearsal_" + "a" * 16,
                        "windows_enforced": {"service_sid": sid, "config_profile": {
                            "acceptance_scope": "isolated-predeployment", "operational_cutover_accepted": False}},
                        "pg_nodes": {node: {"dbname": "ha_lease_rehearsal", "port": 25432,
                                            "sslmode": "verify-full"} for node in ("A", "B")}}
        elapsed = 0
        lease = MagicMock()

        def issue(_):
            lease.owned_expiries = {start + timedelta(seconds=elapsed + 5)}

        def sleep(seconds):
            nonlocal elapsed
            elapsed += seconds

        lease.open.side_effect = lease.renew.side_effect = issue
        clock = MagicMock()
        clock.now.side_effect = lambda _: start + timedelta(seconds=elapsed)
        status = MagicMock(side_effect=[200, 401])
        check = MagicMock(return_value=True)
        database = MagicMock()
        database.__enter__.return_value.execute.return_value.fetchone.return_value = (True,)
        with patch.object(rehearsal, "service_identity", return_value=sid), \
                patch.object(backend, "_connection", return_value=database), \
                patch.object(rehearsal, "InitialPgLease", return_value=lease), \
                patch.object(rehearsal, "datetime", clock), \
                patch.object(rehearsal.time, "monotonic", side_effect=lambda: elapsed), \
                patch.object(rehearsal.time, "sleep", side_effect=sleep):
            result = rehearsal.run(backend, state={}, service_sid=sid, retained_system_ids={"456"},
                                   observe=MagicMock(), exclusion=MagicMock(), previous_token_status=status,
                                   token_expires_at=expiry, assert_single_writer=check)
            with self.assertRaisesRegex(TransitionBlocked, "isolated_lease_scope_invalid"):
                rehearsal.run(backend, state={}, service_sid=sid, retained_system_ids={"123"},
                              observe=MagicMock(), exclusion=MagicMock(), previous_token_status=status,
                              token_expires_at=expiry, assert_single_writer=check)
        self.assertEqual(result, {"renewals": 4, "old_token_rejected": True,
                                  "max_proof_ms": 0, "max_lease_gap_ms": 1000})
        self.assertEqual(check.call_count, 3)
        check.assert_called_with({}, None, writes_open=False)
        lease.revoke.assert_called_once_with({})


class ScmPgAuthProbeTests(unittest.TestCase):
    def test_exact_service_scope_and_two_verified_read_only_connections(self):
        probe=scm_pg_auth_probe
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            material=root/"db-auth-material"
            material.mkdir()
            ca=material/"ca.crt"
            ca.write_bytes(b"fixture CA")
            (material/"postgres.pgpass").write_text("fixture only")
            authority=root/"pg-auth-probe-authority"
            journal=authority/"journal"
            journal.mkdir(parents=True)
            plan_path=authority/"plan.json"
            approval_path=authority/"approval.json"
            report=journal/"controller-state.json"
            trace=journal/".ha-journal-authdiag"
            sid="S-1-5-80-1-2-3-4-5"
            plan={"scope":probe.SCOPE,"service_sid":sid,"hosts":{"A":"10.0.0.2","B":"10.0.0.3"},
                  "system_id":"1234","ca_sha256":hashlib.sha256(ca.read_bytes()).hexdigest()}
            raw=json.dumps(plan).encode()
            plan_path.write_bytes(raw)
            pin=hashlib.sha256(raw).hexdigest()
            approval={"scope":probe.SCOPE,"plan_sha256":pin,"expires_unix":int(time.time())+60}
            approval_path.write_text(json.dumps(approval))
            args=SimpleNamespace(plan=str(plan_path),approval=str(approval_path),journal=str(report))
            connection_a=MagicMock()
            connection_b=MagicMock()
            for node,connection in (("A",connection_a),("B",connection_b)):
                connection.execute.return_value.fetchone.side_effect=[
                    ("ha_"+node,node=="B","on","1234","postgres"),(True,),
                ]
            contexts=[]
            for connection in (connection_a,connection_b):
                context=MagicMock()
                context.__enter__.return_value=connection
                contexts.append(context)
            connect=MagicMock(side_effect=contexts)
            with patch.dict(os.environ,{},clear=True), patch.dict(sys.modules,{"psycopg":SimpleNamespace(connect=connect)}):
                with patch.object(probe,"service_identity",return_value="S-1-5-80-6-7-8-9-10"):
                    with self.assertRaises(ValueError):
                        probe.run(args)
                    connect.assert_not_called()
                    self.assertFalse(report.exists())
                    self.assertEqual(trace.read_text().splitlines(), ["service_identity"])
                    trace.unlink()
                with patch.object(probe,"service_identity",return_value=sid):
                    with patch.dict(os.environ,{"PGPASSWORD":"fixture"}):
                        with self.assertRaises(ValueError):
                            probe.run(args)
                        connect.assert_not_called()
                        self.assertEqual(trace.read_text().splitlines(), ["service_identity", "environment"])
                        trace.unlink()
                    probe.run(args)
                    with self.assertRaises(ValueError):
                        probe.run(args)
            for node,call,connection in zip(("A","B"),connect.call_args_list,(connection_a,connection_b)):
                self.assertEqual(call.kwargs["host"],plan["hosts"][node])
                self.assertEqual(call.kwargs["hostaddr"],plan["hosts"][node])
                self.assertEqual(call.kwargs["sslmode"],"verify-full")
                self.assertEqual(call.kwargs["gssencmode"],"disable")
                self.assertEqual(call.kwargs["require_auth"],"scram-sha-256")
                self.assertEqual(call.kwargs["passfile"],str(material/"postgres.pgpass"))
                connection.execute.assert_any_call("SET TRANSACTION READ ONLY")
                connection.rollback.assert_called_once()
            result=json.loads(report.read_text())
            self.assertEqual(trace.read_text().splitlines(), ["service_identity", "environment", "load_driver",
                "credential_files", "connect_A", "observe_A", "connect_B", "observe_B", "report", "complete"])
            self.assertEqual(result,{"scope":probe.SCOPE,"plan_sha256":pin,"nodes":["A","B"],
                                     "service_identity_verified":True,"tls_verified":True,"authority_opened":False})
            approval["expires_unix"]=0
            with self.assertRaises(ValueError):
                probe.verify_plan(plan,approval,raw,time.time())
            with self.assertRaises(ValueError):
                probe.strict_json('{"scope":0,"scope":1}')

class NativeActivationHandoffTests(unittest.TestCase):
    def test_windows_mux_proof_is_rejected_if_pinned_files_change_during_read(self):
        from ha_control import native_guest_ssh as ssh, native_stdio_bridge as bridge, native_windows_ssh as windows

        fingerprint = 'SHA256:' + 'A' * 43
        endpoint = ssh.SshEndpoint('host.example', 22, 'operator', 'jump', Path('/unused'),
                                   fingerprint, fingerprint)
        client = windows.WindowsSsh(endpoint, '/unused')
        argv = (ssh.SSH, '-F', '/private/tmp/config', '-T', 'jump')
        with patch.object(client, '_connection', return_value=nullcontext((argv, {'stamp': 1}))), \
             patch.object(Path, 'is_socket', return_value=True), \
             patch.object(windows.subprocess, 'Popen') as popen, \
             patch.object(windows.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
             patch.object(ssh, '_same_files', side_effect=[True, False]), \
             patch.object(client, '_powershell', return_value={'proof': 'read'}) as read:
            master = popen.return_value.__enter__.return_value
            master.poll.return_value = None
            with client.live_authority_session(time.monotonic() + 10) as powershell:
                with self.assertRaisesRegex(bridge.BridgeRejected, 'native_windows_ssh_local_file_changed'):
                    powershell('sample', time.monotonic() + 6)
            self.assertIn('ProxyCommand=/usr/bin/false', read.call_args.args[0])
            master.terminate.assert_called_once()

    def test_only_long_running_wsl_cutover_uses_tolerant_ssh_keepalive(self):
        from ha_control import native_guest_ssh as ssh, native_windows_ssh as windows

        fingerprint = 'SHA256:' + 'A' * 43
        endpoint = ssh.SshEndpoint('host.example', 22, 'operator', 'jump', Path('/unused'),
                                   fingerprint, fingerprint)
        client = windows.WindowsSsh(endpoint, '/unused')
        with patch.object(windows.tempfile, 'TemporaryDirectory') as workspace, \
             patch.object(ssh, '_identity', return_value={}), \
             patch.object(ssh, '_checked_file', return_value=(b'', None)), \
             patch.object(ssh, '_host_record'), \
             patch.object(ssh, '_ssh_config', return_value=Path('/private/tmp/ssh-config')), \
             patch.object(ssh, '_same_files', return_value=True):
            workspace.return_value.__enter__.return_value = '/private/tmp/mock-ssh'
            with client._connection(12) as (short, stamps):
                self.assertEqual(short, (ssh.SSH, '-F', '/private/tmp/ssh-config', '-T', 'jump'))
                self.assertEqual(stamps, {Path('/unused'): None})
            with client._connection(12, cutover=True) as (long, _):
                self.assertEqual(long, short[:3] + ('-o', 'ServerAliveInterval=5',
                                                   '-o', 'ServerAliveCountMax=12') + short[3:])

    def test_wsl_import_executes_only_verified_source_bytes(self):
        from ha_control import native_wsl_launcher as launcher, native_stdio_bridge as bridge

        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            parent = Path(directory)
            root = parent / 'source'
            package = root / 'ha_control'
            package.mkdir(mode=0o700, parents=True)
            root.chmod(0o700)
            entry = package / 'run_native_wsl_cutover.py'
            entry.write_bytes(b'value = 1\n')
            entry.chmod(0o600)
            manifest = parent / 'manifest.json'
            raw = bridge._bytes({'ha_control/run_native_wsl_cutover.py': bridge._digest(entry.read_bytes())})
            manifest.write_bytes(raw)
            manifest.chmod(0o600)
            sources = launcher.verify(root, manifest, bridge._digest(raw), os.geteuid())
            entry.write_bytes(b'value = 2\n')
            finder = launcher.VerifiedSources(root, sources)
            import importlib.util
            spec = finder.find_spec('ha_control.run_native_wsl_cutover')
            module = importlib.util.module_from_spec(spec)
            finder.exec_module(module)
            self.assertEqual(module.value, 1)
            with self.assertRaisesRegex(ValueError, 'native_wsl_source_file_unbound'):
                launcher.verify(root, manifest, bridge._digest(raw), os.geteuid())
            with self.assertRaises(ModuleNotFoundError):
                finder.find_spec('ha_control.unapproved')

    def test_guest_live_bridge_uses_raw_guest_receipt_and_keeps_windows_binding(self):
        from ha_control import native_guest_bridge as runtime, native_stdio_bridge as bridge

        binding = bridge.Binding(*[character * 64 for character in 'abcde'])
        chain = bridge._Chain('cutover')
        chain.manifest = '0' * 64
        chain.results['assert_activation_closed', 'A'] = '1' * 64

        def call(verb, node, **args):
            chain.check(verb, node, args)
            result = bridge.Receipt('PASS', '2' * 64, '3' * 64) if verb == 'activate' else bridge.Receipt('PASS', '4' * 64)
            chain.record(verb, node, args, result)
            return result

        guest = runtime.GuestBridgeRuntime(SimpleNamespace(binding=binding, call=call), '/unused',
                                         '192.0.2.1', '/unused', 1024)
        guest.verified = '5' * 64
        args = (binding.plan_sha256, binding.attempt_id)
        receipt = guest.activate(*args, chain.manifest, guest.verified, '6' * 64, activation_check_sha256='1' * 64)
        authority = receipt['evidence_sha256']
        self.assertNotEqual(authority, '2' * 64)
        self.assertEqual(guest.service_instance_sha256, '3' * 64)
        refs = {'windows_generation': 1, 'windows_generation_sha256': '7' * 64,
                'live_lease_sha256': '8' * 64, 'live_authority_sha256': '9' * 64}
        self.assertEqual(guest.assert_live(*args, authority, **refs)['authority_sha256'], authority)
        with self.assertRaisesRegex(TransitionBlocked, 'guest_bridge_authority_unbound'):
            guest.assert_live(*args, '2' * 64, **refs)

    def test_ssh_activation_checks_live_windows_at_each_boundary(self):
        from ha_control import native_guest_ssh as ssh, native_stdio_bridge as bridge

        worker = '''
import json, sys
def send(value):
    print(json.dumps(value, sort_keys=True, separators=(',', ':')), flush=True)
request = json.loads(sys.stdin.readline())
for index, boundary in enumerate(request['boundaries']):
    fields = dict(request_nonce=request['nonce'], challenge=format(index, '064x'), boundary=boundary)
    send(dict(kind='activation_boundary', **fields))
    assert json.loads(sys.stdin.readline()) == dict(kind='activation_boundary_current', windows_proof_sha256='c' * 64, **fields)
send({'status': 'PASS'})
'''
        nonce = 'f' * 64
        payload = bridge._bytes({'nonce': nonce, 'boundaries': ssh.ACTIVATION_BOUNDARIES})
        checks = []

        def current(deadline):
            self.assertLess(time.monotonic(), deadline)
            checks.append(deadline)
            return 'c' * 64

        command = (sys.executable, '-c', worker)
        result = ssh._activation_exchange(command, payload, time.monotonic() + 5, nonce, current)
        self.assertEqual(json.loads(result), {'status': 'PASS'})
        self.assertEqual(len(checks), len(ssh.ACTIVATION_BOUNDARIES))

        checks.clear()

        def lost_windows(deadline):
            proof = current(deadline)
            if len(checks) == 3:
                raise bridge.BridgeRejected('windows_service_lost')
            return proof

        with self.assertRaisesRegex(bridge.BridgeRejected, 'windows_service_lost'):
            ssh._activation_exchange(command, payload, time.monotonic() + 5, nonce, lost_windows)
        self.assertEqual(len(checks), 3)
        with self.assertRaisesRegex(bridge.BridgeRejected, 'guest_ssh_live_windows_unconfirmed'):
            ssh._activation_exchange(command, payload, time.monotonic() + 5, nonce, lambda _: False)

    def test_binds_wsl_intent_and_exact_guest_receipt_before_activation(self):
        from ha_control import native_activation_handoff as handoff, native_stdio_bridge as bridge

        binding = bridge.Binding(*[character * 64 for character in 'abcde'])
        evidence = dict(zip(('manifest_sha256', 'restore_receipt_sha256', 'runtime_receipt_sha256',
                             'closed_a_sha256', 'closed_b_sha256', 'closed_postgres_sha256'),
                            (character * 64 for character in '012345')))
        record = dict(binding=binding.fields(), node='A', verb='assert_activation_closed', evidence=evidence)
        arguments = {'manifest_sha256': evidence['manifest_sha256'], 'settlement_sha256': '6' * 64,
                     'activation_check_sha256': bridge._digest(bridge._bytes(record))}
        request = bridge.Request(binding, 'A', 'activate', 1, '7' * 64, arguments)
        journal = {'phase': 'activate_vm_intent', 'plan_sha256': binding.plan_sha256, 'attempt_id': binding.attempt_id,
                   'receipts': {'restore': evidence['restore_receipt_sha256'], 'recheck_reactivation': '6' * 64,
                                'fence_writers': '8' * 64, 'capture': '9' * 64, 'verify_closed': 'a' * 64}}
        intent = handoff.activation_intent(request, journal, evidence['restore_receipt_sha256'])
        result = json.loads(handoff.guest_handoff(request, 'b' * 64, record, intent, 'c' * 64, 'd' * 64))
        self.assertEqual(result['activation_pins']['coordinator_intent_sha256'], bridge._digest(intent))
        self.assertEqual(result['prepared_receipt_sha256'], evidence['runtime_receipt_sha256'])
        with self.assertRaisesRegex(bridge.BridgeRejected, 'activation_intent_unbound'):
            handoff.activation_intent(request, journal | {'phase': 'restore_intent'}, evidence['restore_receipt_sha256'])
        with self.assertRaisesRegex(bridge.BridgeRejected, 'activation_handoff_unbound'):
            handoff.guest_handoff(request, 'b' * 64, record | {'node': 'B'}, intent, 'c' * 64, 'd' * 64)

        observed = []

        @contextmanager
        def guest_authority(_deadline):
            observed.append('ssh_ready')
            yield lambda _: observed.append('boundary') or 'f' * 64
            observed.append('ssh_closed')

        @contextmanager
        def open_first_write(_request, native_intent, _deadline):
            self.assertEqual(native_intent, intent)
            observed.append('open')
            yield SimpleNamespace(first_write_proof_sha256='c' * 64, service_instance_sha256='d' * 64,
                                  guest_authority=guest_authority,
                                  assert_current=lambda _deadline, *, guest_boundary:
                                  observed.append('current') or 'f' * 64)
            observed.append('handover')

        def publish(_request, payload, _deadline):
            self.assertEqual(json.loads(payload), result)
            observed.append('publish')

        def guest(_request, _deadline, *, assert_windows_current):
            assert_windows_current(_deadline)
            observed.append('start')
            return bridge.Receipt('PASS', 'e' * 64)

        custody = SimpleNamespace(read_guest_check=lambda *_: record, read_wsl_intent=lambda *_: journal,
                                  installation_id='b' * 64, open_first_write=open_first_write,
                                  publish_guest_handoff=publish)
        dispatcher = handoff.ActivationDispatcher(guest, custody)
        reply = dispatcher(request, time.monotonic() + 1)
        self.assertEqual(reply, bridge.Receipt('PASS', 'e' * 64, 'd' * 64))
        self.assertEqual(reply.fields('activate')['service_instance_sha256'], 'd' * 64)
        with self.assertRaisesRegex(bridge.BridgeRejected, 'bridge_result_rejected'):
            reply.fields('assert_live')
        self.assertEqual(observed, ['open', 'current', 'ssh_ready', 'publish', 'boundary', 'start',
                                    'ssh_closed', 'current', 'handover'])


        attempts = []

        def fail_before_handoff(_request, native_intent, _deadline):
            self.assertEqual(native_intent, intent)

            def timeout(_deadline, *, guest_boundary=True):
                self.assertFalse(guest_boundary)
                attempts.append(_deadline)
                raise bridge.BridgeRejected('guest_ssh_deadline_exceeded')

            yield SimpleNamespace(first_write_proof_sha256='c' * 64, service_instance_sha256='d' * 64,
                                  assert_current=timeout)

        from ha_control.run_native_mac_cutover import _reason

        custody.open_first_write = contextmanager(fail_before_handoff)
        observed.clear()
        with self.assertRaises(bridge.BridgeRejected) as failure:
            dispatcher(request, time.monotonic() + 1)
        self.assertEqual(_reason(failure.exception), 'activation_live_service_check_ssh_deadline_exceeded')
        self.assertEqual(observed, [])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])

        @contextmanager
        def transient_timeout(_request, _intent, _deadline):
            check = MagicMock(side_effect=[bridge.BridgeRejected('guest_ssh_deadline_exceeded'), 'f' * 64,
                                      'f' * 64])
            @contextmanager
            def session(_deadline):
                yield lambda _: 'f' * 64

            yield SimpleNamespace(first_write_proof_sha256='c' * 64, service_instance_sha256='d' * 64,
                                  assert_current=check, guest_authority=session)
            self.assertEqual(check.call_count, 3)

        custody.open_first_write = transient_timeout
        self.assertEqual(dispatcher(request, time.monotonic() + 1), reply)
        self.assertEqual(observed, ['publish', 'start'])

    def test_activation_timeout_identifies_innermost_transport(self):
        from ha_control import native_activation_handoff as handoff, native_stdio_bridge as bridge

        with self.assertRaisesRegex(bridge.BridgeRejected, 'activation_guest_boundary_check_ssh_deadline_exceeded'):
            with handoff._transport_stage('start_guest'), handoff._transport_stage('guest_boundary_check'):
                raise bridge.BridgeRejected('guest_ssh_deadline_exceeded')

        with self.assertRaisesRegex(bridge.BridgeRejected, '^guest_handoff_response_unbound$'):
            with handoff._transport_stage('publish_handoff'):
                raise bridge.BridgeRejected('guest_handoff_response_unbound')


if __name__ == "__main__":
    unittest.main()
