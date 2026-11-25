"""Native provider retirement, stopped-only SCM disable and exact paired closure.

The enclosing driver authenticates guest transport and fresh custody. This module
never stops a Windows service; it disables only an already stopped exact writer.
It does not publish, stage a successor, modify apps, or reopen database writes.
"""

import base64
import gzip
import json
from pathlib import Path, PureWindowsPath
import secrets
import time

from . import native_guest_runtime as runtime, native_stdio_bridge as wire
from .controller import TransitionBlocked
from .sameboot_transition import _digest, _encoded, _scope

ERROR = "sameboot_release_unconfirmed"


def _need(value):
    if not value:
        raise TransitionBlocked(ERROR)


def _json(raw):
    return json.loads(raw, object_pairs_hook=wire._pairs, parse_constant=wire._reject_constant)


def observe_guest_retired(provider, assert_safe):
    """Read-only reconciliation under the caller's guest mutation descriptor.

    The protected adapter intent must exist. A lost done response is resolved
    through archived identity, absent provider port, preserved held observer and
    a fresh custody proof; neither the stop nor any journal write is repeated.
    """
    units = provider._archive()
    provider._gate("retire", "after", assert_safe)
    intent, done = provider._paths("retire")
    raw = runtime._read(intent, 0, {0o400}, limit=131072)
    issued = _json(raw)
    _need(type(issued) is dict and set(issued) == {
        "scope", "latest_intent", "rollback_sha256", "hold_sha256", "operation", "boundary",
        "safe_challenge_sha256", "safe_proof_sha256", "initial", "held", "dispatch_proof_sha256",
        "automatic_retry_permitted"})
    _need(issued["scope"] == provider.scope and issued["latest_intent"] == provider.intent
          and issued["rollback_sha256"] == provider.rollback and issued["hold_sha256"] == provider.hold
          and issued["operation"] == "retire" and issued["boundary"] == "before"
          and issued["automatic_retry_permitted"] is False
          and all(wire._hash(issued[key]) for key in (
              "safe_challenge_sha256", "safe_proof_sha256", "dispatch_proof_sha256")))
    # A completed or issued restore terminates the one-shot retirement scope.
    _need(not any(path.exists() for path in provider._paths("restore")))
    held = provider._live(units["held"], "held")
    _need(held == issued["held"])
    provider._absent(units["provider"])
    receipt = provider._receipt("retire") if done.exists() else wire._digest(wire._bytes({
        "retirement_intent_sha256": wire._digest(raw), "provider_absent": True, "held": held}))
    provider._gate("retire", "after", assert_safe)
    _need(provider._live(units["held"], "held") == held)
    return {"retirement_sha256": receipt, "held_observation_sha256": wire._digest(wire._bytes(held)),
            "rollback_sha256": provider.rollback}


def retire_guest(provider, assert_safe):
    """Invoke only from an authenticated source package while holding its lock."""
    provider.retire(assert_safe)
    return observe_guest_retired(provider, assert_safe)


class WindowsPairClosure:
    """Read exact native terminal and A/B facts over authenticated Windows SSH.

    runtime_manifest is the *old* code package manifest, supplied as pinned
    bytes. Every file is checked before starting its Python interpreter.
    Config paths are kept in memory and never returned in public evidence.
    """

    FIELDS = {"plan", "plan_sha256", "binding", "binding_sha256", "runtime",
              "python_sha256", "boot_sha256", "revision"}

    def __init__(self, windows, config, *, runtime_manifest, runtime_manifest_sha256):
        _need(type(config) is dict and set(config) == self.FIELDS)
        _need(type(runtime_manifest) is bytes and wire._hash(runtime_manifest_sha256)
              and wire._digest(runtime_manifest) == runtime_manifest_sha256)
        manifest = _json(runtime_manifest)
        _need(type(manifest) is dict and type(manifest.get("files")) is dict and manifest["files"])
        for key in self.FIELDS - {"plan", "binding", "runtime", "revision"}:
            _need(wire._hash(config[key]))
        for key in ("plan", "binding", "runtime"):
            value = config[key]
            _need(type(value) is str and value.isascii() and 0 < len(value) <= 240
                  and "\x00" not in value and "\r" not in value and "\n" not in value
                  and PureWindowsPath(value).is_absolute() and ".." not in PureWindowsPath(value).parts)
        revision = config["revision"]
        _need(type(revision) is int and 0 < revision < 2**63 - 2 and revision % 2 == 0)
        files = {}
        for name, spec in manifest["files"].items():
            _need(type(name) is str and name and "\\" not in name and ":" not in name
                  and all(part not in {"", ".", ".."} for part in name.split("/"))
                  and type(spec) is dict and spec.get("filename") == name
                  and type(spec.get("bytes")) is int and 0 <= spec["bytes"] <= 67108864
                  and wire._hash(spec.get("sha256")))
            files[name] = {"bytes": spec["bytes"], "sha256": spec["sha256"]}
        _need("python/python.exe" in files
              and files["python/python.exe"]["sha256"] == config["python_sha256"]
              and "controller/files/ha_control/cli.py" in files
              and "controller/files/ha_control/initial_pg_lease.py" in files)
        self.windows = windows
        self.config = config.copy()
        self.files = files

    def _call(self, mode, deadline, terminal_sha256=None, *, successor=None, verify_runtime=False,
              resumed_windows_boot=None):
        _need(mode in {"snapshot", "disable", "closed", "closed_after_arm", "closed_before_disable"}
              and time.monotonic() < deadline)
        if mode != "snapshot":
            _need(wire._hash(terminal_sha256))
        if mode == "closed_after_arm":
            _need(type(successor) is dict and set(successor) == {"path_sha256", "image_sha256"}
                  and all(wire._hash(pin) for pin in successor.values()))
        else:
            _need(successor is None)
        previous_boot = self.config["boot_sha256"]
        if resumed_windows_boot is not None:
            _need(mode == "closed" and wire._hash(resumed_windows_boot)
                  and resumed_windows_boot != previous_boot)
        live_boot = resumed_windows_boot if resumed_windows_boot is not None else previous_boot
        challenge = wire._digest(bytes.fromhex(secrets.token_hex(32) + live_boot))
        config = self.config | {"mode": mode, "terminal_sha256": terminal_sha256,
            "challenge": challenge, "live_boot_sha256": live_boot, "files": self.files,
            "sql": base64.b64encode(PAIRED_SQL.encode()).decode("ascii"), "successor": successor,
            "verify_runtime": verify_runtime or mode in {"closed", "closed_after_arm", "closed_before_disable"}}
        # The real old manifest contains hundreds of files. Compress only the
        # in-memory config so the fixed SSH source-size limit is preserved.
        blob = base64.b64encode(gzip.compress(_encoded(config), mtime=0)).decode("ascii")
        loader = "$m=[IO.MemoryStream]::new([Convert]::FromBase64String('" + blob + "'));" + (
            "$z=[IO.Compression.GZipStream]::new($m,[IO.Compression.CompressionMode]::Decompress);"
            "$r=[IO.StreamReader]::new($z,$enc);try{$cfg=ConvertFrom-Json ($r.ReadToEnd())}finally{$r.Dispose()}")
        header = WINDOWS_GUARD.replace("$cfg=ConvertFrom-Json ($enc.GetString([Convert]::FromBase64String('CFG')))", loader)
        script = "try{\n" + header + WINDOWS_BODY + "\n}catch{exit 1}"
        result = self.windows.powershell(script, deadline)
        _need(type(result) is dict and result.get("challenge") == config["challenge"])
        if mode == "snapshot":
            _need(set(result) == {"challenge", "status", "terminal_base64", "terminal_sha256", "disabled"}
                  and result["status"] in {"RUNNING", "STOPPED"} and type(result["disabled"]) is bool)
            raw = base64.b64decode(result["terminal_base64"], validate=True)
            _need(0 < len(raw) <= 8192 and wire._digest(raw) == result["terminal_sha256"])
            terminal = _json(raw)
            _need(type(terminal) is dict and terminal.get("controller_plan_sha256") == self.config["plan_sha256"]
                  and type(terminal.get("revision")) is int and terminal["revision"] == self.config["revision"]
                  and type(terminal.get("sequence")) is int and terminal["sequence"] > 0)
            if result["status"] == "STOPPED":
                _need(terminal.get("phase") in {"lease_lost", "local_revoke_observed"})
            else:
                _need(terminal.get("phase") == "serving" and result["disabled"] is False)
            result.pop("terminal_base64")
            return result
        if mode == "disable":
            _need(result == {"challenge": config["challenge"], "status": "STOPPED_DISABLED",
                             "terminal_sha256": terminal_sha256})
            return result
        _need(set(result) == {"challenge", "status", "terminal_sha256", "proof_sha256", "closed_revision"}
              and result["status"] == "CLOSED_PAIR_VERIFIED" and result["terminal_sha256"] == terminal_sha256
              and type(result["closed_revision"]) is int and result["closed_revision"] == self.config["revision"] + 1
              and wire._hash(result["proof_sha256"]))
        return {"closed_nodes": ["A", "B"], "synchronous": True, "same_expiry": True,
                "replay_verified": True, "effects_isolated": True, "revision": result["closed_revision"],
                "terminal_sha256": terminal_sha256, "closed_proof_sha256": result["proof_sha256"]}

    def snapshot(self, deadline, *, verify_runtime=False):
        return self._call("snapshot", deadline, verify_runtime=verify_runtime)

    def disable_stopped(self, terminal_sha256, deadline):
        return self._call("disable", deadline, terminal_sha256)

    def closed_pair(self, terminal_sha256, deadline):
        return self._call("closed", deadline, terminal_sha256)

    def closed_pair_after_reboot(self, terminal_sha256, observed_windows_boot_sha256, deadline):
        """Verify both still-closed DBs using the archived plan in the exact new Windows boot."""
        _need(wire._hash(observed_windows_boot_sha256))
        return self._call("closed", deadline, terminal_sha256, resumed_windows_boot=observed_windows_boot_sha256)

    def closed_pair_before_disable(self, terminal_sha256, deadline):
        """Read natural native closure before changing a stopped Manual SCM."""
        return self._call("closed_before_disable", deadline, terminal_sha256)

    def closed_pair_after_arm(self, terminal_sha256, successor_image_path_sha256,
                              successor_image_sha256, deadline):
        """Read the original closed pair while the same stopped SCM is armed.

        The old protected plan/binding/terminal remain the query authority. Only
        the actual SCM image is allowed to differ, and both its exact PathName
        and protected binary must match independently approved successor pins.
        """
        return self._call("closed_after_arm", deadline, terminal_sha256, successor={
            "path_sha256": successor_image_path_sha256, "image_sha256": successor_image_sha256})


class NativeSamebootRelease:
    """Release-phase protocol implementation for SamebootTransition.

    guest.retire and guest.observe_retired authenticate the exact issued bytes
    and return retire_guest/observe_guest_retired results from the guest package.
    custody_guard must freshly bind boot, stopped app inventory, latest intent,
    protected resources, original writer lineage and absence of a successor.
    Those safety observations are required again inside every guest boundary;
    a static hash or coordinator journal is never a substitute.
    """

    def __init__(self, scope, guest, pair, custody_guard, *, expected_stopped_terminal_sha256=None):
        self.scope = _scope(scope)
        _need(callable(custody_guard) and type(pair) is WindowsPairClosure
              and pair.config["plan_sha256"] == self.scope["previous_plan_sha256"]
              and pair.config["binding_sha256"] == self.scope["previous_instance_sha256"]
              and pair.config["revision"] == self.scope["previous_revision"])
        self.guest, self.pair, self.custody_guard = guest, pair, custody_guard
        _need(expected_stopped_terminal_sha256 is None or wire._hash(expected_stopped_terminal_sha256))
        self.expected_stopped_terminal_sha256 = expected_stopped_terminal_sha256

    def _request(self, request_raw, receipts_raw):
        request, receipts = _json(request_raw), _json(receipts_raw)
        _need(type(request) is dict and set(request) == {
            "scope", "stage", "previous_sha256", "nonce", "plan_material_sha256"}
            and request["scope"] == self.scope and request["stage"] in {"retire_provider", "close_pair"}
            and wire._hash(request["nonce"]) and wire._hash(request["previous_sha256"])
            and request["plan_material_sha256"] is None and type(receipts) is dict)
        _need(request_raw == _encoded(request) and receipts_raw == _encoded(receipts))
        _need(type(receipts.get("preflight")) is dict and wire._hash(receipts["preflight"].get("rollback_sha256")))
        if request["stage"] == "close_pair":
            _need(type(receipts.get("retire_provider")) is dict
                  and wire._hash(receipts["retire_provider"].get("terminal_sha256")))
        return request, receipts

    def guard(self, request_raw, receipts_raw, deadline, *, observe_only=False):
        self._request(request_raw, receipts_raw)
        _need(type(observe_only) is bool and time.monotonic() < deadline)
        return self.custody_guard(request_raw, receipts_raw, deadline, observe_only=observe_only) is True

    def _guard(self, request, receipts, deadline, observe_only):
        _need(self.guard(request, receipts, deadline, observe_only=observe_only))

    def _retired_guest(self, value, receipts):
        _need(type(value) is dict and set(value) == {
            "retirement_sha256", "held_observation_sha256", "rollback_sha256"}
            and all(wire._hash(pin) for pin in value.values())
            and value["rollback_sha256"] == receipts["preflight"]["rollback_sha256"])

    def apply(self, request_raw, receipts_raw, deadline):
        request, receipts = self._request(request_raw, receipts_raw)
        _need(request["stage"] == "retire_provider")
        self._guard(request_raw, receipts_raw, deadline, False)
        # Validate the old code package and still-running original before any
        # guest stop. A changed runtime fails before changing a provider.
        before = self.pair.snapshot(deadline, verify_runtime=True)
        if self.expected_stopped_terminal_sha256 is None:
            _need(before["status"] == "RUNNING")
        else:
            _need(before["status"] == "STOPPED"
                  and before["terminal_sha256"] == self.expected_stopped_terminal_sha256)
            # An independently stopped predecessor is permitted only with this
            # exact preobserved native terminal and fresh paired closure. Its
            # provider and held observer are still verified separately before
            # retiring the exact archived provider.
            self.pair.closed_pair_before_disable(self.expected_stopped_terminal_sha256, deadline)
        retired = self.guest.retire(request_raw, receipts_raw, deadline)
        self._retired_guest(retired, receipts)
        self._guard(request_raw, receipts_raw, deadline, True)
        while True:
            stopped = self.pair.snapshot(deadline)
            if stopped["status"] == "STOPPED":
                break
            _need(time.monotonic() + 1 < deadline)
            time.sleep(1)
        self._guard(request_raw, receipts_raw, deadline, True)
        self.pair.disable_stopped(stopped["terminal_sha256"], deadline)
        return self.observe(request_raw, receipts_raw, deadline)

    def observe(self, request_raw, receipts_raw, deadline):
        request, receipts = self._request(request_raw, receipts_raw)
        self._guard(request_raw, receipts_raw, deadline, True)
        retired = self.guest.observe_retired(request_raw, receipts_raw, deadline)
        self._retired_guest(retired, receipts)
        stopped = self.pair.snapshot(deadline)
        _need(stopped["status"] == "STOPPED" and stopped["disabled"] is True)
        terminal = stopped["terminal_sha256"]
        if request["stage"] == "close_pair":
            _need(terminal == receipts["retire_provider"]["terminal_sha256"])
        closed = self.pair.closed_pair(terminal, deadline)
        self._guard(request_raw, receipts_raw, deadline, True)
        evidence = closed if request["stage"] == "close_pair" else {
            "previous_instance_sha256": self.scope["previous_instance_sha256"], "terminal_sha256": terminal,
            "other_observers_preserved": True, "old_service_stopped_disabled": True, "native_terminal_preserved": True}
        return {"issued_sha256": _digest(request), "status": "PASS", "evidence": evidence}

    def retire_provider(self, request, receipts, deadline):
        return self.apply(_encoded(request), _encoded(receipts), deadline)["evidence"]

    def observe_retired(self, request, receipts, deadline):
        _need(request.get("stage") == "retire_provider")
        return self.observe(_encoded(request), _encoded(receipts), deadline)["evidence"]

    def observe_closed(self, request, receipts, deadline):
        if request.get("stage") == "close_pair":
            return self.observe(_encoded(request), _encoded(receipts), deadline)["evidence"]
        # After replacement the provider port legitimately has a different
        # owner. Recheck the original Windows terminal and exact closed A/B
        # facts, while the enclosing custody guard verifies that new owner.
        _need(type(request) is dict and request.get("scope") == self.scope
              and request.get("stage") in {"replace_provider", "restore_provider", "carryover", "stage_runtime", "arm_host"}
              and wire._hash(request.get("nonce")) and wire._hash(request.get("previous_sha256"))
              and type(receipts) is dict and type(receipts.get("retire_provider")) is dict
              and type(receipts.get("close_pair")) is dict)
        terminal = receipts["retire_provider"].get("terminal_sha256")
        _need(wire._hash(terminal) and receipts["close_pair"].get("terminal_sha256") == terminal)
        raw, chain = _encoded(request), _encoded(receipts)
        _need(self.custody_guard(raw, chain, deadline, observe_only=True) is True)
        closed = self.pair.closed_pair(terminal, deadline)
        _need(self.custody_guard(raw, chain, deadline, observe_only=True) is True)
        return closed


WINDOWS_GUARD = r'''
$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest
$enc=[Text.UTF8Encoding]::new($false,$true)
$cfg=ConvertFrom-Json ($enc.GetString([Convert]::FromBase64String('CFG')))
$system='S-1-5-18';$admins='S-1-5-32-544';$allowed=@($system,$admins)
function Pin([byte[]]$bytes){
 $sha=[Security.Cryptography.SHA256]::Create()
 try{[BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
}
function Assert-Path([string]$p,[bool]$directory,[string]$reader=''){
 $item=Get-Item -LiteralPath $p -Force
 if(($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.PSIsContainer -ne $directory){throw 'stage_path'}
 for($entry=$item;$null -ne $entry;$entry=if($entry -is [IO.FileInfo]){$entry.Directory}else{$entry.Parent}){
  if($entry.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'stage_ancestor'}
  $sd=[Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $entry.FullName).Sddl)
  $osRoot=$entry.FullName -ceq 'C:\' -and $sd.Owner.Value -ceq 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
  if(($sd.Owner.Value -cnotin $allowed -and -not $osRoot) -or $null -eq $sd.DiscretionaryAcl){throw 'stage_acl'}
  foreach($ace in $sd.DiscretionaryAcl){
   if($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback){throw 'stage_acl'}
   if($ace.AceQualifier -ne 'AccessAllowed' -or ([int]$ace.AceFlags -band 8) -ne 0){continue}
   $sid=$ace.SecurityIdentifier.Value
   if($entry.FullName -ceq $item.FullName -and $sid -cnotin $allowed -and ($sid -cne $reader -or ([long]$ace.AccessMask -band 269287488) -ne 0)){throw 'stage_acl'}
   if($sid -cnotin $allowed -and ([long]$ace.AccessMask -band 269287488) -ne 0){throw 'stage_ancestor_acl'}
  }
 }
 return $item
}
function FilePin([string]$p,[long]$max,[string]$reader=''){
 $f=Assert-Path $p $false $reader
 if($f.Length -le 0 -or $f.Length -gt $max){throw 'stage_size'}
 return (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant()
}
function Current([string]$p,[long]$max,[string]$want,[string]$reader=''){
 if((FilePin $p $max $reader) -cne $want){throw 'stage_pin'}
}
'''

PAIRED_SQL = Path(__file__).with_name('initial_closed_pair.py').read_text(encoding='utf-8')

WINDOWS_BODY = r'''
function BoundService {
 $service=Get-CimInstance Win32_Service -Filter ("Name='"+$binding.service_name+"'") -OperationTimeoutSec 5
 if($null -eq $service -or $service.Name -cne $plan.windows_enforced.service_name -or
    $service.StartName -ine ('NT SERVICE\'+$service.Name) -or $service.ServiceType -cne 'Own Process' -or
    $service.DesktopInteract -or
    $binding.service_image_path_sha256 -cne $plan.initial_service_start.service_image_path_sha256){throw 'release_service'}
 if($cfg.mode -ceq 'closed_after_arm'){
  if($service.State -cne 'Stopped' -or $service.ProcessId -ne 0 -or $service.StartMode -cne 'Manual' -or
     (Pin ($enc.GetBytes($service.PathName))) -cne $cfg.successor.path_sha256 -or
     $service.PathName -cnotmatch '^"([A-Za-z]:\\[^"\r\n]+\.exe)"$'){throw 'release_successor'}
  Current $Matches[1] 67108864 $cfg.successor.image_sha256 $sid
 }else{
  if((Pin ($enc.GetBytes($service.PathName))) -cne $binding.service_image_path_sha256 -or
     $service.PathName -cnotmatch '^"([A-Za-z]:\\[^"\r\n]+\.exe)"$'){throw 'release_service'}
  Current $Matches[1] 67108864 $plan.windows_enforced.service_image_sha256 $sid
 }
 return $service
}
function SameScope($actual,$expected){
 $left=@($actual.PSObject.Properties.Name|Sort-Object -CaseSensitive)
 $right=@($expected.PSObject.Properties.Name|Sort-Object -CaseSensitive)
 if(($left -join ',') -cne ($right -join ',')){throw 'release_scope'}
 foreach($key in $right){if($actual.$key -cne $expected.$key -or $actual.$key.GetType() -ne $expected.$key.GetType()){throw 'release_scope'}}
}
function JournalRaw {
 $planParent=[IO.Path]::GetDirectoryName($cfg.plan)
 if($binding.state_path -cne (Join-Path $planParent 'journal\controller-state.json')){throw 'release_terminal'}
 $null=Assert-Path $planParent $true $sid
 $entry=Get-Item -LiteralPath $binding.state_path -Force
 if($entry -isnot [IO.FileInfo] -or $entry.Length -lt 1 -or $entry.Length -gt 8192){throw 'release_terminal'}
 foreach($item in @($entry,$entry.Directory)){
  if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'release_terminal'}
  $acl=[Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $item.FullName).Sddl)
  if($acl.Owner.Value -cnotin ($allowed+@($sid)) -or $null -eq $acl.DiscretionaryAcl){throw 'release_terminal'}
  foreach($ace in $acl.DiscretionaryAcl){
   if($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback){throw 'release_terminal'}
   if($ace.AceQualifier -ceq 'AccessAllowed' -and ($ace.AccessMask -band 1343029590L) -ne 0 -and
      $ace.SecurityIdentifier.Value -cnotin ($allowed+@($sid))){throw 'release_terminal'}
  }
 }
 $stream=[IO.File]::Open($entry.FullName,'Open','Read',([IO.FileShare]::Read -bor [IO.FileShare]::Delete))
 try{
  $reader=[IO.BinaryReader]::new($stream)
  try{$raw=$reader.ReadBytes(8193);if($raw.Length -lt 1 -or $raw.Length -gt 8192){throw 'release_terminal'};return ,$raw}
  finally{$reader.Dispose()}
 }finally{$stream.Dispose()}
}
function Stopped($service){
 if($service.State -cne 'Stopped' -or $service.ProcessId -ne 0 -or
    $service.StartMode -cnotin @('Manual','Disabled')){throw 'release_stopped'}
}
function Confirm-WindowsBoot {
 if((Pin ($enc.GetBytes((Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 5).LastBootUpTime.ToUniversalTime().Ticks.ToString()))) -cne $cfg.live_boot_sha256){throw 'release_boot'}
}
Confirm-WindowsBoot
# The untrusted preliminary parse supplies only a read-only ACL exception.
# Full bytes and ACL ownership are pinned before using any plan authority.
$planRaw=[IO.File]::ReadAllBytes($cfg.plan)
if((Pin $planRaw) -cne $cfg.plan_sha256){throw 'release_plan'}
$plan=ConvertFrom-Json ($enc.GetString($planRaw))
$sid=$plan.windows_enforced.service_sid
Current $cfg.plan 65536 $cfg.plan_sha256 $sid
Current $cfg.binding 8192 $cfg.binding_sha256 $sid
$bindingRaw=[IO.File]::ReadAllBytes($cfg.binding)
if((Pin $bindingRaw) -cne $cfg.binding_sha256){throw 'release_binding'}
$binding=ConvertFrom-Json ($enc.GetString($bindingRaw))
$actualSid=[Security.Principal.NTAccount]::new('NT SERVICE',$binding.service_name).Translate([Security.Principal.SecurityIdentifier]).Value
if($sid -cne $actualSid){throw 'release_identity'}
if($binding.controller_plan_sha256 -cne $cfg.plan_sha256 -or
   $binding.attempt_id -cne $plan.initial_cutover_handoff.attempt_id -or
   $binding.legacy_plan_sha256 -cne $plan.initial_cutover_handoff.legacy_plan_sha256 -or
   $plan.windows_initial_observer.windows_boot_sha256 -cne $cfg.boot_sha256 -or
   $plan.initial_recovery_postboot.predecessor_kind -cnotin @('published-postboot-sameboot-v1','published-postboot-saved-reboot-v1')){throw 'release_binding'}
# Validate every archived code/data file and Python dependency before executing
# the old package. Paths and hashes originate in the independently pinned manifest.
if($cfg.verify_runtime){
 $expected=@{}
 foreach($property in $cfg.files.PSObject.Properties){
  if($expected.ContainsKey($property.Name)){throw 'release_runtime'}
  $expected.Add($property.Name,$property.Value)
 }
 $count=0;$pending=[Collections.Generic.Stack[string]]::new();$pending.Push($cfg.runtime)
 while($pending.Count -gt 0){
  $directory=$pending.Pop();$null=Assert-Path $directory $true $sid
  foreach($path in [IO.Directory]::GetFileSystemEntries($directory)){
   $entry=Get-Item -LiteralPath $path -Force
   if($entry.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'release_runtime'}
   if($entry.PSIsContainer){$pending.Push($path);continue}
   $name=$path.Substring($cfg.runtime.TrimEnd('\').Length+1).Replace('\','/')
   if(-not $expected.ContainsKey($name)){throw 'release_runtime'}
   $spec=$expected[$name];$entry=Assert-Path $path $false $sid
   if($entry.Length -ne $spec.bytes -or -not ($entry.Attributes -band [IO.FileAttributes]::ReadOnly) -or
      (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $spec.sha256){throw 'release_runtime'}
   $count++
  }
 }
 if($count -ne $expected.Count){throw 'release_runtime'}
}
$exe=Join-Path $cfg.runtime 'python\python.exe'
Current $exe 50000000 $cfg.python_sha256 $sid
$service=BoundService
$raw=JournalRaw
$record=ConvertFrom-Json ($enc.GetString($raw))
if($record.controller_plan_sha256 -cne $cfg.plan_sha256 -or $record.revision -ne $cfg.revision -or
   $record.sequence -lt 1 -or $record.schema_version -ne 1 -or $record.generation -ne 1 -or
   $record.attempt_id -cne $binding.attempt_id -or $record.legacy_plan_sha256 -cne $binding.legacy_plan_sha256 -or
   $record.kind -cne 'initial-wsl-cutover-postboot-resume'){throw 'release_terminal'}
SameScope $record.recovery $plan.initial_cutover_recovery
SameScope $record.postboot $plan.initial_recovery_postboot
$terminalPin=Pin $raw
if($service.State -ceq 'Running'){
 if($cfg.mode -cne 'snapshot' -or $service.ProcessId -ne $binding.service_pid -or
    $service.StartMode -cne 'Manual' -or $record.phase -cne 'serving'){throw 'release_still_running'}
 $process=Get-Process -Id $binding.service_pid -ErrorAction Stop
 try{
  if($process.HasExited -or $process.SessionId -ne 0 -or ('"'+$process.MainModule.FileName+'"') -ine $service.PathName -or
     $process.StartTime.ToUniversalTime().Ticks.ToString() -cne $binding.service_created_ticks){throw 'release_instance'}
 }finally{$process.Dispose()}
 @{challenge=$cfg.challenge;status='RUNNING';terminal_base64=[Convert]::ToBase64String($raw);terminal_sha256=$terminalPin;disabled=$false}|ConvertTo-Json -Compress
 return
}
Stopped $service
if($record.phase -cnotin @('lease_lost','local_revoke_observed')){throw 'release_terminal'}
$keys=@('activation_intent_sha256','attempt_id','controller_plan_sha256','generation','kind','legacy_plan_sha256',
        'phase','postboot','recovery','restore_receipt_sha256','revision','schema_version','sequence')
if('failure_code' -cin $record.PSObject.Properties.Name){
 # The journal retains the failure code when local revocation succeeds too.
 if($record.failure_code -isnot [string] -or $record.failure_code -cnotmatch '\A[a-z][a-z0-9_]{0,95}\z'){throw 'release_terminal'}
 $keys+='failure_code'
}
if((@($record.PSObject.Properties.Name|Sort-Object -CaseSensitive)-join ',') -cne (@($keys|Sort-Object -CaseSensitive)-join ',')){throw 'release_terminal'}
if($cfg.mode -ceq 'snapshot'){
 @{challenge=$cfg.challenge;status='STOPPED';terminal_base64=[Convert]::ToBase64String($raw);terminal_sha256=$terminalPin;disabled=($service.StartMode -ceq 'Disabled')}|ConvertTo-Json -Compress
 return
}
if($terminalPin -cne $cfg.terminal_sha256){throw 'release_terminal'}
if($cfg.mode -ceq 'disable'){
 # This is deliberately the only SCM mutation. A running process is never
 # stopped here; provider loss must first finish its native terminal path.
 if($service.StartMode -ceq 'Manual'){
  $changed=Invoke-CimMethod -InputObject $service -MethodName ChangeStartMode -Arguments @{StartMode='Disabled'} -OperationTimeoutSec 5 -ErrorAction Stop
  if($changed.ReturnValue -ne 0){throw 'release_disable'}
 }
 $after=BoundService;Stopped $after
 if($after.StartMode -cne 'Disabled' -or (Pin (JournalRaw)) -cne $terminalPin){throw 'release_disable'}
 @{challenge=$cfg.challenge;status='STOPPED_DISABLED';terminal_sha256=$terminalPin}|ConvertTo-Json -Compress
 return
}
if($cfg.mode -cnotin @('closed_after_arm','closed_before_disable') -and $service.StartMode -cne 'Disabled'){throw 'release_disabled'}
$body=$enc.GetString([Convert]::FromBase64String($cfg.sql))
$rows=@($body|& $exe -I -B - $cfg.runtime $cfg.plan $cfg.plan_sha256 $binding.state_path $terminalPin $cfg.challenge $cfg.revision 2>$null)
if($LASTEXITCODE -ne 0 -or $rows.Count -ne 1){throw 'release_pair'}
if($cfg.live_boot_sha256 -cne $cfg.boot_sha256){Confirm-WindowsBoot}
$after=BoundService;Stopped $after
if(($cfg.mode -ceq 'closed_after_arm' -and $after.StartMode -cne 'Manual') -or
   ($cfg.mode -cnotin @('closed_after_arm','closed_before_disable') -and $after.StartMode -cne 'Disabled') -or
   ($cfg.mode -ceq 'closed_before_disable' -and $after.StartMode -cne $service.StartMode) -or
   (Pin (JournalRaw)) -cne $terminalPin){throw 'release_terminal'}
$result=ConvertFrom-Json $rows[0]
if($result.closed_revision -ne ($cfg.revision+1) -or $result.full_closed_pair_verified -isnot [bool] -or
   -not $result.full_closed_pair_verified -or $result.candidate_effects_binding_matches_both_databases -isnot [bool] -or
   -not $result.candidate_effects_binding_matches_both_databases -or $result.exact_quarantine_matches -isnot [bool] -or
   -not $result.exact_quarantine_matches){throw 'release_pair'}
@{challenge=$cfg.challenge;status='CLOSED_PAIR_VERIFIED';terminal_sha256=$terminalPin;
  proof_sha256=$result.proof_sha256;closed_revision=$result.closed_revision}|ConvertTo-Json -Compress
'''
