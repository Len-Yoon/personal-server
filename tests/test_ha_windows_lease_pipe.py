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
    def test_predecessor_is_checked_on_both_sides_of_recovery_observation(self):
        runtime = shutil.which("pwsh") or "/private/tmp/n100-ha-powershell/runtime/pwsh"
        protocol = Path(__file__).resolve().parents[1] / "infra/k8s/ha_control/windows-ha-recovery-proof-protocol.ps1"
        source = """
$ErrorActionPreference='Stop'
. $env:RECOVERY_PROOF_PROTOCOL
$name='N100HA_'+('a'*24)+'_controller'
$image='fixture service executable'
$sha=[Security.Cryptography.SHA256]::Create()
try {$digest=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($image))).Replace('-','').ToLowerInvariant()}
finally {$sha.Dispose()}
$binding=@{service_name=$name;service_image_path_sha256=$digest;attempt_id='b'*64;legacy_plan_sha256='c'*64;
    controller_plan_sha256='d'*64}
$terminal=$binding.Clone()
$terminal.kind='initial-wsl-cutover';$terminal.phase='lease_lost';$terminal.generation=1
$scope=New-HaRecoveryPredecessorScope $binding $terminal ('a'*64) @{attempt_id='e'*64;plan_sha256='c'*64}
$clock=[Diagnostics.Stopwatch]::GetTimestamp()+[Diagnostics.Stopwatch]::Frequency*5
function Read-HaRecoveryPredecessorScm {
    $script:checks++
    $mode=if($script:checks -eq $script:drift){3}else{4}
    [pscustomobject]@{Name=$name;Start=$mode;State=1;Pid=0;Type=16;
        Account='NT SERVICE'+[char]92+$name;Image=$image}
}
function Get-HaRecoveryObservation { $script:observed++; return 'fixture_observation' }
foreach($invalid in @(0,1,2)){
    $script:checks=0;$script:observed=0;$script:drift=$invalid
    try {
        $result=Get-HaBoundRecoveryObservation 'fixture' $clock $scope
        if($invalid -ne 0 -or $result -cne 'fixture_observation'){throw 'fixture_accepted_invalid'}
    } catch {
        if($invalid -eq 0 -or $_.Exception.Message -cne 'recovery_proof_predecessor'){throw}
    }
    if($script:observed -ne [int]($invalid -ne 1)){throw 'fixture_order'}
    if($script:checks -ne (2-[int]($invalid -eq 1))){throw 'fixture_reads'}
}
exit 0
"""
        result = subprocess.run((runtime, "-NoProfile", "-NonInteractive", "-Command", source),
                                env=os.environ | {"RECOVERY_PROOF_PROTOCOL": str(protocol)},
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

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
                    *(letter*64 for letter in "abcdef123"), "S-1-5-80-1-2-3-4-5", purpose)
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
        pins = pipe.RecoveryProofPins(*(letter*64 for letter in "abcdef123"), sid, "held_admission")
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
                         challenge | {"predecessor_service_sha256": "0"*64},
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
        recovery = pipe.RecoveryProofPins(*(letter*64 for letter in "abcdef123"), sid, "provider_isolation")
        args = {"server_pid": 5, "key_path": None, "key_sha256": "1"*64, "powershell": None,
                "client": None, "client_sha256": "2"*64, "compiler_root": None}
        with patch.object(pipe.sys, "platform", "win32"):
            for pins, name in ((legacy, "n100ha-recovery-proof-"), (recovery, "n100ha-lease-proof-")):
                with self.assertRaises(ValueError):
                    pipe.PersistentLeaseProofClient(pins=pins, pipe_name=name+"a"*32, **args)


class WindowsLeaseResponseBudgetTests(unittest.TestCase):
    def test_offline_observer_checks_exact_vms_and_rechecks_wsl_before_return(self):
        runtime = shutil.which("pwsh") or "/private/tmp/n100-ha-powershell/runtime/pwsh"
        observer = Path(__file__).resolve().parents[1] / "infra/k8s/ha_control/windows-ha-wsl-lease-observer.ps1"
        command = r"""
$ErrorActionPreference='Stop'
. $env:HA_OFFLINE_OBSERVER
try {
    Initialize-HaLeaseObservation ([pscustomobject]@{closure_mode=@('windows-offline-v1')}) '' '' '' ''
    throw 'fixture_array_mode'
} catch {
    if ($_.Exception.Message -cne 'lease_proof_callback_scope'){throw}
}
$script:ClosureOffline=$true
$script:ClosureInput=[pscustomobject]@{installation_id='a'*64;a_disk_paths_sha256='b'*64;b_disk_paths_sha256='c'*64}
$script:ClosureMarker=@{sha256='d'*64}
$script:ClosureBoot='e'*64
$script:ClosureVmA=[guid]::NewGuid()
$script:ClosureVmB=[guid]::NewGuid()
$script:ClosurePlan=@{boot_ticks='fixture'}
$challenge=[pscustomobject]@{nonce='1'*32;plan_sha256='f'*64;attempt_id='2'*64;generation=1;
    windows_boot_sha256=$script:ClosureBoot;wsl_boot_sha256='3'*64;collector_sha256='4'*64}
function Get-HaReactivationHash { return '5'*64 }
function Assert-HaOwnedCutoverMarker($Marker,[switch]$ReadContent) {
    if($ReadContent){return (ConvertTo-Json -InputObject $challenge -Compress)}
}
function Assert-HaWslOfflineReactivationClosed { $script:reads++ }
function Assert-HaWslWorkloadsAbsent {
    $script:reads++
    if($script:reappeared){throw 'fixture_wsl_restarted'}
}
$vm=[RunspaceFactory]::CreateRunspace()
$vm.Open()
try {
    $warm=[PowerShell]::Create()
    $warm.Runspace=$vm
    try {
        $null=$warm.AddScript({
            function Get-FixedVmObserverSnapshot {
                $last=if($global:drift){'bad'}else{'c'*64}
                return ,@(
                    [pscustomobject]@{node='A';state='Running';disk_paths_sha256='b'*64},
                    [pscustomobject]@{node='B';state='Running';disk_paths_sha256=$last}
                )
            }
        }.ToString(),$false)
        $null=$warm.Invoke()
        if($warm.HadErrors){throw 'fixture_warm'}
    } finally { $warm.Dispose() }
    foreach($mode in @('positive','wsl','vm')){
        $vm.SessionStateProxy.SetVariable('drift',($mode -ceq 'vm'))
        $script:reappeared=$mode -ceq 'wsl'
        $script:reads=0
        $context=[pscustomobject]@{Challenge=$challenge;Progress=@{};MarkerSha256=$script:ClosureMarker.sha256;
            ReactivationPlanSha256=$challenge.plan_sha256;VmRunspace=$vm;
            DeadlineTimestamp=[Diagnostics.Stopwatch]::GetTimestamp()+[long]([Diagnostics.Stopwatch]::Frequency*2.5)}
        try {
            $reply=Get-HaLeaseProof $context
            if($mode -cne 'positive' -or -not $reply.wsl_writers_closed -or $reply.nonce -cne $challenge.nonce){
                throw 'fixture_accepted_invalid'
            }
            if($script:reads -ne 2){throw 'fixture_missing_absence'}
        } catch {
            if($mode -ceq 'positive' -or $_.Exception.Message -cnotin @('fixture_wsl_restarted','lease_proof_vm_scope')){throw}
        }
    }
} finally { $vm.Dispose() }
exit 0
"""
        result = subprocess.run((runtime, "-NoProfile", "-NonInteractive", "-Command", command),
                                env=os.environ | {"HA_OFFLINE_OBSERVER": str(observer)},
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

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
        proof = callback.split("function Get-HaLeaseProof", 1)[1].split("function Get-HaOfflineLeaseProof", 1)[0]
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
