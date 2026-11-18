import base64
import hashlib
import hmac
import json
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


class RecoveryProofWireTests(unittest.TestCase):
    def test_native_response_matches_python_and_rejects_unbound_observation(self):
        runtime = shutil.which("pwsh") or "/private/tmp/n100-ha-powershell/runtime/pwsh"
        self.assertTrue(Path(runtime).is_file(), "PowerShell required for recovery wire verification")
        protocol = (Path(__file__).resolve().parents[1] / "infra/k8s/ha_control"
                    / "windows-ha-recovery-proof-protocol.ps1")
        source = r"""
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. $env:HA_RECOVERY_PROTOCOL
$pins = ConvertFrom-Json -AsHashtable $env:HA_RECOVERY_PINS
$fact = [pscustomobject]@{
    challenge_sha256=$env:HA_RECOVERY_DIGEST; closed=$true; observation_sha256=('a'*64)
}
$mac = [Security.Cryptography.HMACSHA256]::new([byte[]]::new(32))
try { [Console]::Write((New-HaRecoveryProofFrame $env:HA_RECOVERY_CHALLENGE $pins $fact $mac)) }
finally { $mac.Dispose() }
"""
        for purpose in ("held_admission", "provider_isolation"):
            with self.subTest(purpose=purpose):
                pins = windows_lease_pipe.RecoveryProofPins(
                    *(letter*64 for letter in "abcdef12"), "S-1-5-80-1-2-3-4-5", purpose)
                challenge = pins.challenge()
                raw = json.dumps(challenge, separators=(",", ":"), sort_keys=True)
                env = os.environ | {"HA_RECOVERY_PROTOCOL": str(protocol), "HA_RECOVERY_CHALLENGE": raw,
                                    "HA_RECOVERY_PINS": json.dumps(vars(pins)),
                                    "HA_RECOVERY_DIGEST": hashlib.sha256(raw.encode()).hexdigest()}
                args = (runtime, "-NoProfile", "-NonInteractive", "-Command", source)
                result = subprocess.run(args, env=env, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(windows_lease_pipe.verify_response(
                    result.stdout, challenge, 2, key=bytearray(32), monotonic=lambda: 0), "a"*64)
                stale = subprocess.run(args, env=env | {"HA_RECOVERY_DIGEST": "0"*64},
                                       capture_output=True, timeout=15)
                self.assertNotEqual(stale.returncode, 0)
                self.assertEqual(stale.stdout, b"")

    def test_recovery_requires_distinct_challenge_and_mac_domain(self):
        pipe = windows_lease_pipe
        sid = "S-1-5-80-1-2-3-4-5"
        pins = pipe.RecoveryProofPins(*(letter*64 for letter in "abcdef12"), sid, "held_admission")
        challenge = pins.challenge()
        key = bytearray(b"x"*32)

        def response(sample, *, prefix=b""):
            body = json.dumps(sample | {"closed": True, "observation_sha256": "1"*64}).encode()
            return json.dumps({"body": base64.b64encode(body).decode(),
                               "mac": hmac.digest(key, prefix + body, "sha256").hex()}).encode() + b"\n"

        recovery_domain = pipe.RECOVERY_SCOPE.encode() + b"\0"
        self.assertEqual(pipe.verify_response(response(challenge, prefix=recovery_domain), challenge, 2,
                                              key=key, monotonic=lambda: 0), "1"*64)
        with self.assertRaises(ValueError):
            pipe.verify_response(response(challenge), challenge, 2, key=key, monotonic=lambda: 0)
        for modified in (challenge | {"purpose": "provider_isolation"},
                         challenge | {"hold_sha256": "0"*64},
                         pins.challenge()):
            # A good MAC for another purpose, pin or fresh nonce still fails.
            with self.assertRaises(ValueError):
                pipe.verify_response(response(modified, prefix=recovery_domain), challenge, 2,
                                     key=key, monotonic=lambda: 0)
        with self.assertRaises(ValueError):
            pipe.verify_response(response(challenge, prefix=recovery_domain), challenge, 2,
                                 key=key, monotonic=lambda: 2)
        legacy = pipe.ProofPins("a"*64,"b"*64,1,"c"*64,sid,"d"*64,"e"*64).challenge()
        self.assertEqual(pipe.verify_response(response(legacy), legacy, 2, key=key, monotonic=lambda: 0), "1"*64)
        with self.assertRaises(ValueError):
            pipe.verify_response(response(legacy), challenge, 2, key=key, monotonic=lambda: 0)

    def test_recovery_pins_cannot_select_legacy_pipe_and_vice_versa(self):
        pipe = windows_lease_pipe
        sid = "S-1-5-80-1-2-3-4-5"
        legacy = pipe.ProofPins("a"*64,"b"*64,1,"c"*64,sid,"d"*64,"e"*64)
        recovery = pipe.RecoveryProofPins(*(letter*64 for letter in "abcdef12"), sid, "provider_isolation")
        args = {"server_pid": 5, "key_path": None, "key_sha256": "1"*64, "powershell": None,
                "client": None, "client_sha256": "2"*64, "compiler_root": None}
        with patch.object(pipe.sys, "platform", "win32"):
            for pins, name in ((legacy, "n100ha-recovery-proof-"), (recovery, "n100ha-lease-proof-")):
                with self.assertRaises(ValueError):
                    pipe.PersistentLeaseProofClient(pins=pins, pipe_name=name+"a"*32, **args)


class WindowsLeaseResponseBudgetTests(unittest.TestCase):
    def test_offline_closure_checks_wsl_before_and_after_task_and_process_readback(self):
        runtime = shutil.which("pwsh") or "/private/tmp/n100-ha-powershell/runtime/pwsh"
        self.assertTrue(Path(runtime).is_file(), "PowerShell required for offline closure verification")
        script = Path(__file__).resolve().parents[1] / "infra/k8s/ha_control/windows-reactivation.ps1"
        source = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($env:HA_OFFLINE_GUARD,[ref]$tokens,[ref]$errors)
if ($errors.Count -ne 0) { throw 'offline_guard_parse' }
$definition = $ast.Find({
    param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -ceq 'Assert-HaWslOfflineReactivationClosed'
}, $false)
. ([scriptblock]::Create($definition.Extent.Text))
$calls = [Collections.Generic.List[string]]::new()
function Assert-HaReactivationHost($Plan) { $calls.Add('boot') }
function Assert-HaWslWorkloadsAbsent {
    $calls.Add('wsl')
    if ($script:wslReturned -and $calls.Count -eq 5) { throw 'wsl_returned' }
}
function Assert-HaReactivationInfrastructureClosed($Plan, [switch]$WslOffline) {
    if (-not $WslOffline) { throw 'requires_idle_anchor' }
    $calls.Add('tasks')
}
function Assert-HaReactivationProcessesClosed { $calls.Add('processes') }
$script:wslReturned = $false
Assert-HaWslOfflineReactivationClosed @{}
if (($calls -join ',') -cne 'boot,wsl,tasks,processes,wsl') { throw 'offline_guard_order' }
$calls.Clear()
$script:wslReturned = $true
try { Assert-HaWslOfflineReactivationClosed @{}; throw 'wsl_return_missed' } catch {
    if ($_.Exception.Message -cne 'wsl_returned') { throw }
}
exit 0
"""
        result = subprocess.run((runtime, "-NoProfile", "-NonInteractive", "-Command", source),
                                env=os.environ | {"HA_OFFLINE_GUARD": str(script)},
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

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
$config.arguments | Add-Member NoteProperty RecoveryProtocolSha256 ('d'*64)
$recovery = Get-ObserverInstanceNames $config
if ($recovery.task -cne ('N100HA_lease_observer_recovery_' + ('c'*64)) -or
    $recovery.ready -cne ('lease-observer-recovery-ready-' + ('c'*64) + '.json') -or
    $recovery.task -ceq $c.task -or $recovery.ready -ceq $c.ready) { throw 'recovery_instance_collision' }
$config.PSObject.Properties.Remove('instance_sha256')
try { $null = Get-ObserverInstanceNames $config; throw 'unpinned_recovery_accepted' } catch {
    if ($_.Exception.Message -cne 'observer_task_instance_pin') { throw }
}
exit 0
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
        fields_start = initial_stage.index("$callbackFields =")
        fields_end = initial_stage.index("Assert-Fields $callback $callbackFields", fields_start)
        self.assertNotIn("vm_b_installation_id", initial_stage[fields_start:fields_end])

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
