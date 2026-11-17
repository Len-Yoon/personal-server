import os
from pathlib import Path
from queue import Queue
import shutil
import subprocess
import tempfile
from threading import Lock
import unittest
from unittest.mock import Mock, patch

from infra.k8s.ha_control import enforced_windows, initial_lease_service, windows_lease_pipe


class WindowsLeaseResponseBudgetTests(unittest.TestCase):
    def test_transition_observer_instances_have_distinct_pinned_names(self):
        runtime = shutil.which("pwsh") or "/private/tmp/n100-ha-powershell/runtime/pwsh"
        self.assertTrue(Path(runtime).is_file(), "PowerShell required for observer instance verification")
        launcher = Path(__file__).resolve().parents[1] / "infra/k8s/ha_control/windows-ha-start-lease-observer.ps1"
        source = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:HA_OBSERVER_LAUNCHER,[ref]$tokens,[ref]$errors)
if ($errors.Count -ne 0) { throw 'launcher_parse' }
$definition = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq 'Get-ObserverInstanceNames'
}, $false)
. ([scriptblock]::Create($definition.Extent.Text))
$attempt = 'a'*64
$config = [pscustomobject]@{arguments=[pscustomobject]@{AttemptId=$attempt;CallbackConfigSha256=('b'*64)}}
$initial = Get-ObserverInstanceNames $config
if ($initial.task -cne ('N100HA_lease_observer_' + $attempt) -or
    $initial.ready -cne ('lease-observer-ready-' + $attempt + '.json')) { throw 'legacy_names_changed' }
$config | Add-Member NoteProperty instance_sha256 ('b'*64)
$b = Get-ObserverInstanceNames $config
$config.instance_sha256 = 'c'*64
try { $null = Get-ObserverInstanceNames $config; throw 'mismatch_accepted' } catch {
    if ($_.Exception.Message -cne 'observer_task_instance_pin') { throw }
}
$config.arguments.CallbackConfigSha256 = 'c'*64
$c = Get-ObserverInstanceNames $config
if ($b.task -ceq $c.task -or $b.ready -ceq $c.ready -or $b.task -ceq $initial.task -or
    $b.ready -ceq $initial.ready -or $config.arguments.AttemptId -cne $attempt) { throw 'instance_collision' }
'''
        result = subprocess.run(
            (runtime, "-NoProfile", "-NonInteractive", "-Command", source),
            env=os.environ | {"HA_OBSERVER_LAUNCHER": str(launcher)},
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_replacement_vm_installation_pin_reaches_warmup_and_lease_sampling(self):
        scripts = Path(__file__).resolve().parents[1] / "infra/k8s/ha_control"
        server = (scripts / "windows-ha-lease-proof-server.ps1").read_text()
        callback = (scripts / "windows-ha-wsl-lease-observer.ps1").read_text()
        for source, config, pipeline, execute in (
            (server, "$inputConfig", "$vmInit", "$warm = $vmInit.Invoke()"),
            (callback, "$script:ClosureInput", "$vmObserver", "$vmPending = $vmObserver.BeginInvoke()"),
        ):
            with self.subTest(config=config):
                steps = (
                    ".AddCommand('Get-FixedVmObserverSnapshot')",
                    f"'vm_b_installation_id' -cin {config}.PSObject.Properties.Name",
                    f"{pipeline}.AddParameter('InstallationB', {config}.vm_b_installation_id)",
                    execute,
                )
                positions = [source.index(step) for step in steps]
                self.assertEqual(positions, sorted(positions))
        self.assertIn("$InputConfig.vm_b_installation_id -isnot [string]", callback)
        initial_stage = (scripts / "windows-ha-stage-initial-activation.ps1").read_text()
        self.assertNotIn("vm_b_installation_id", initial_stage)

    def test_process_inventory_follows_task_closure_and_precedes_marker_readback(self):
        scripts = Path(__file__).resolve().parents[1] / "infra/k8s/ha_control"
        callback = (scripts / "windows-ha-wsl-lease-observer.ps1").read_text()
        proof = callback.split("function Get-HaLeaseProof", 1)[1]
        steps = (
            "Assert-HaOwnedCutoverMarker $script:ClosureMarker",
            "Assert-HaReactivationInfrastructureClosed $script:ClosurePlan",
            "$processPending = $processObserver.BeginInvoke()",
            "Assert-HaReactivationHost $script:ClosurePlan",
            "$processPending.AsyncWaitHandle.WaitOne((Get-HaClosureRemaining $Context))",
            "$processObserver.EndInvoke($processPending)",
            "$processObserver.HadErrors -or $processRows.Count -ne 0",
            "$Context.Progress.step = 'marker_after'",
            "recovery_commands_settled=$true",
        )
        positions = [proof.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(proof.count("Assert-HaOwnedCutoverMarker $script:ClosureMarker"), 2)
        self.assertIn("[Math]::Min(1800, (Get-HaClosureRemaining $Context) - 125)", proof)

    def test_response_has_bounded_transport_time_and_checks_before_writing(self):
        runtime = shutil.which("pwsh") or "/private/tmp/n100-ha-powershell/runtime/pwsh"
        self.assertTrue(Path(runtime).is_file(), "PowerShell required for response boundary verification")
        server = Path(__file__).resolve().parents[1] / "infra/k8s/ha_control/windows-ha-lease-proof-server.ps1"
        source = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:HA_RESPONSE_SERVER, [ref]$tokens, [ref]$errors)
if ($errors.Count -ne 0) { throw 'server_parse' }
$definitions = $ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -cin @('Remaining', 'Send-ProofResponse')
}, $false)
if ($definitions.Count -ne 2) { throw 'server_functions' }
foreach ($definition in $definitions) { . ([scriptblock]::Create($definition.Extent.Text)) }
$stream = [pscustomobject]@{ writes = 0 }
$stream | Add-Member -MemberType ScriptMethod -Name WriteAsync -Value {
    param($bytes, $offset, $length)
    $this.writes++
    return [System.Threading.Tasks.Task]::CompletedTask
}
$clock = [pscustomobject]@{ Elapsed = [timespan]::FromMilliseconds(2499) }
if ((Remaining) -ne 1) { throw 'observation_budget_changed' }
$clock.Elapsed = [timespan]::FromMilliseconds(2597)
$observationExpired = $false
try { $null = Remaining } catch {
    if ($_.Exception.Message -cne 'lease_proof_deadline') { throw }
    $observationExpired = $true
}
if (-not $observationExpired) { throw 'observation_deadline_not_enforced' }
Send-ProofResponse $stream ([byte[]]@(123,125,10))
if ($stream.writes -ne 1) { throw 'within_response_budget_not_sent' }
$clock.Elapsed = [timespan]::FromMilliseconds(2650)
$responseExpired = $false
try { Send-ProofResponse $stream ([byte[]]@(123,125,10)) } catch {
    if ($_.Exception.Message -cne 'lease_proof_deadline') { throw }
    $responseExpired = $true
}
if (-not $responseExpired -or $stream.writes -ne 1) { throw 'late_response_sent' }
"""
        result = subprocess.run(
            (runtime, "-NoProfile", "-NonInteractive", "-Command", source),
            env=os.environ | {"HA_RESPONSE_SERVER": str(server)},
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class WindowsLeasePipePathTests(unittest.TestCase):
    def test_only_exact_system_powershell_allows_hardlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            powershell = root / "powershell.exe"
            alias = root / "other.exe"
            powershell.write_bytes(b"fixture")
            os.link(powershell, alias)

            with patch.object(windows_lease_pipe.sys, "platform", "win32"), patch.object(
                enforced_windows, "_powershell_path", return_value=powershell
            ):
                self.assertEqual(windows_lease_pipe._protected_path(powershell, system_powershell=True).st_nlink, 2)
                with self.assertRaises(ValueError):
                    windows_lease_pipe._protected_path(alias, system_powershell=True)
                with self.assertRaises(ValueError):
                    windows_lease_pipe._protected_path(powershell)

    def test_failed_observation_records_only_the_rejection_stage(self):
        for frame, code in ((b"", "legacy_pipe_response_missing"),
                            (b"failure connect\r\n", "legacy_pipe_client_connect"),
                            (b"failure read\r\n", "legacy_pipe_client_read"),
                            (b"failure unknown\r\n", "legacy_pipe_response_invalid"),
                            (b"untrusted payload\n", "legacy_pipe_response_invalid")):
            with self.subTest(code=code):
                client = object.__new__(windows_lease_pipe.PersistentLeaseProofClient)
                client._lock = Lock()
                client._process = Mock()
                client._process.poll.return_value = None
                client._incoming = Queue()
                client._outgoing = Queue()
                client._key = bytearray(32)
                client.monotonic = lambda: 0
                client.pins = Mock()
                client.pins.challenge.return_value = {}
                client.close = Mock()
                client._incoming.put(frame)
                observer = initial_lease_service.WindowsInitialObserver(client, None, None, None)
                with self.assertRaisesRegex(initial_lease_service.TransitionBlocked, f"^{code}$"):
                    observer.assert_legacy_closed({}, 2)
                self.assertIn(code, initial_lease_service.LEASE_FAILURE_CODES)
                client.close.assert_called_once_with()

    def test_request_budget_is_bounded_by_the_caller_and_transport(self):
        for deadline, budget in ((1.75, 1750), (4, 2700)):
            with self.subTest(deadline=deadline):
                client = object.__new__(windows_lease_pipe.PersistentLeaseProofClient)
                client._lock = Lock()
                client._process = Mock()
                client._process.poll.return_value = None
                client._incoming = Queue()
                client._incoming.put(b"proof\n")
                client._outgoing = Queue()
                client._key = bytearray(32)
                client.monotonic = lambda: 0
                client.pins = Mock()
                client.pins.challenge.return_value = {}
                with patch.object(windows_lease_pipe, "verify_response", return_value="verified") as verify:
                    self.assertEqual(client.request(deadline), "verified")
                self.assertEqual(client._outgoing.get_nowait(), f"{budget} {{}}\n".encode("ascii"))
                self.assertAlmostEqual(verify.call_args.args[2], budget / 1000)
