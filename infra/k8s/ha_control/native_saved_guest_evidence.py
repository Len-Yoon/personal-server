"""Read-only saved-VM evidence, with independent authenticated guest/VSock readers.

Import performs no I/O. ``collect`` explicitly uses WindowsSsh and caller-owned
pinned guest readers. Those readers must perform their real checks; an SSH
inventory alone is neither a CA/clock proof nor an AF_HYPERV roundtrip. No
reboot, mapping creation, container operation, or evidence file write exists.
"""

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import secrets
import time
from types import MappingProxyType
from typing import Mapping

from . import native_stdio_bridge as wire
from .controller import TransitionBlocked
from .native_windows_ssh import WindowsSsh

NODES = ('A', 'B')
HARDWARE_FIELDS = {'vm_id_sha256', 'disks_sha256', 'switches_sha256'}
GUEST_FIELDS = HARDWARE_FIELDS | {'guest_boot_sha256', 'container_inventory_sha256'}


def _need(value):
    if not value:
        raise TransitionBlocked('saved_guest_evidence_unconfirmed')


def _integer(value):
    return type(value) is int and value > 0


@dataclass(frozen=True)
class VmHardware:
    vm_id_sha256: str
    disks_sha256: str
    switches_sha256: str

    def __post_init__(self):
        _need(all(wire._hash(value) for value in self.fields().values()))

    def fields(self):
        return {name: getattr(self, name) for name in sorted(HARDWARE_FIELDS)}


@dataclass(frozen=True)
class WindowsVmSnapshot:
    challenge: str
    windows_boot_sha256: str
    utc_before_ns: int
    utc_after_ns: int
    states: Mapping[str, str]
    hardware: Mapping[str, VmHardware]

    def __post_init__(self):
        _need(wire._hash(self.challenge) and wire._hash(self.windows_boot_sha256)
              and _integer(self.utc_before_ns) and _integer(self.utc_after_ns)
              and self.utc_before_ns <= self.utc_after_ns
              and set(self.states) == set(self.hardware) == set(NODES)
              and all(state in {'Running', 'Saved'} for state in self.states.values())
              and all(type(row) is VmHardware for row in self.hardware.values())
              and all(getattr(self.hardware['A'], key) != getattr(self.hardware['B'], key)
                      for key in ('vm_id_sha256', 'disks_sha256')))
        object.__setattr__(self, 'states', MappingProxyType(dict(self.states)))
        object.__setattr__(self, 'hardware', MappingProxyType(dict(self.hardware)))


@dataclass(frozen=True)
class GuestObservation:
    """A pinned SSH reader's native validation, including real CA/clock checks.

    A uses the authenticated carryover digest as its admission inventory; B
    uses its exact Docker inventory. hold_sha256 is required on A only. The
    source and CA/clock proof pins name the independently checked read path.
    """
    node: str
    challenge: str
    windows_boot_sha256: str
    guest_boot_sha256: str
    container_inventory_sha256: str
    docker_inventory_sha256: str
    container_count: int
    guest_utc_ns: int
    source_sha256: str
    clock_ca_proof_sha256: str
    hold_sha256: str | None = None

    def __post_init__(self):
        _need(self.node in NODES and _integer(self.guest_utc_ns)
              and type(self.container_count) is int and self.container_count >= 0
              and (self.node != 'A' or self.container_count == len(wire.RECOVERY_START_ORDER))
              and all(wire._hash(getattr(self, name)) for name in (
                  'challenge', 'windows_boot_sha256', 'guest_boot_sha256', 'container_inventory_sha256',
                  'docker_inventory_sha256', 'source_sha256', 'clock_ca_proof_sha256'))
              and (wire._hash(self.hold_sha256) if self.node == 'A' else self.hold_sha256 is None)
              and (self.node == 'A' or self.container_inventory_sha256 == self.docker_inventory_sha256))

    def digest(self):
        return wire._digest(wire._bytes(self.__dict__))


@dataclass(frozen=True)
class VsockObservation:
    """Receipt of an actual challenge-bound Windows AF_HYPERV roundtrip.

    Its authenticated reader must verify the exact endpoint and returned
    challenge; do not construct this from SSH reachability or a past receipt.
    """
    node: str
    challenge: str
    windows_boot_sha256: str
    vm_id_sha256: str
    guest_boot_sha256: str
    observed_utc_ns: int
    proof_sha256: str
    transport: str = 'AF_HYPERV'

    def __post_init__(self):
        _need(self.node in NODES and self.transport == 'AF_HYPERV' and _integer(self.observed_utc_ns)
              and all(wire._hash(getattr(self, name)) for name in (
                  'challenge', 'windows_boot_sha256', 'vm_id_sha256', 'guest_boot_sha256', 'proof_sha256')))


@dataclass(frozen=True)
class GuestPairCapture:
    before: WindowsVmSnapshot
    after: WindowsVmSnapshot
    observations: Mapping[str, GuestObservation]
    vsock: Mapping[str, VsockObservation]

    def __post_init__(self):
        _need(type(self.before) is WindowsVmSnapshot and type(self.after) is WindowsVmSnapshot
              and self.before.challenge == self.after.challenge
              and self.before.windows_boot_sha256 == self.after.windows_boot_sha256
              and self.before.hardware == self.after.hardware
              and dict(self.before.states) == dict(self.after.states) == dict.fromkeys(NODES, 'Running')
              and 0 < self.after.utc_after_ns - self.before.utc_before_ns <= 30_000_000_000
              and self.before.utc_after_ns <= self.after.utc_before_ns
              and set(self.observations) == set(self.vsock) == set(NODES))
        for node in NODES:
            guest, vsock = self.observations[node], self.vsock[node]
            _need(type(guest) is GuestObservation and type(vsock) is VsockObservation
                  and guest.node == vsock.node == node
                  and guest.challenge == vsock.challenge == self.before.challenge
                  and guest.windows_boot_sha256 == vsock.windows_boot_sha256 == self.before.windows_boot_sha256
                  and vsock.vm_id_sha256 == self.before.hardware[node].vm_id_sha256
                  and vsock.guest_boot_sha256 == guest.guest_boot_sha256
                  and all(self.before.utc_before_ns <= stamp <= self.after.utc_after_ns
                          for stamp in (guest.guest_utc_ns, vsock.observed_utc_ns)))
        _need(self.observations['A'].guest_boot_sha256 != self.observations['B'].guest_boot_sha256
              and self.vsock['A'].proof_sha256 != self.vsock['B'].proof_sha256)
        object.__setattr__(self, 'observations', MappingProxyType(dict(self.observations)))
        object.__setattr__(self, 'vsock', MappingProxyType(dict(self.vsock)))

    def guests(self):
        """Exact five-hash rows for the protected pre-save seal/successor input."""
        return {node: self.before.hardware[node].fields() | {
            'guest_boot_sha256': self.observations[node].guest_boot_sha256,
            'container_inventory_sha256': self.observations[node].container_inventory_sha256} for node in NODES}


# Hash exactly the historical ReadExactVm disk/NIC row formats. Raw IDs, disk
# paths, switch IDs and MAC addresses stay inside the authenticated Windows read.
_WINDOWS_READ = r'''
$ErrorActionPreference='Stop';Set-StrictMode -Version Latest
$utf=[Text.UTF8Encoding]::new($false,$true)
$c=ConvertFrom-Json ($utf.GetString([Convert]::FromBase64String('__CONFIG__')))
function Need([bool]$v){if(-not $v){throw 'saved_guest_hardware_unconfirmed'}}
function HashText([string]$v){$s=[Security.Cryptography.SHA256]::Create();try{[BitConverter]::ToString($s.ComputeHash($utf.GetBytes($v))).Replace('-','').ToLowerInvariant()}finally{$s.Dispose()}}
function Boot {HashText ((Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 5).LastBootUpTime.ToUniversalTime().Ticks.ToString())}
$before=[long](([DateTime]::UtcNow.Ticks-621355968000000000)*100)
Need ((Boot) -ceq $c.boot)
$inventory=@(Get-VM -ErrorAction Stop);$hardware=@{};$states=@{}
foreach($node in @('A','B')){
 $found=@($inventory|Where-Object {(HashText $_.Id.ToString('D').ToLowerInvariant()) -ceq $c.ids.$node})
 Need ($found.Count -eq 1);$vm=$found[0]
 Need ($vm.Generation -eq 2 -and $vm.State.ToString() -ceq $c.states.$node -and $vm.AutomaticStartAction.ToString() -ceq 'Nothing' -and $vm.AutomaticStopAction.ToString() -ceq 'TurnOff')
 $drives=@(Get-VMHardDiskDrive -VM $vm -ErrorAction Stop|Sort-Object ControllerType,ControllerNumber,ControllerLocation)
 Need ($drives.Count -gt 0);$disks=@()
 foreach($drive in $drives){
  Need ($null -eq $drive.DiskNumber)
  $file=Get-Item -LiteralPath $drive.Path -Force
  Need ($file -is [IO.FileInfo] -and -not ($file.Attributes -band [IO.FileAttributes]::ReparsePoint))
  for($p=$file.Directory;$null -ne $p;$p=$p.Parent){Need (-not ($p.Attributes -band [IO.FileAttributes]::ReparsePoint))}
  $disks+=,[ordered]@{path=$file.FullName;controller_type=$drive.ControllerType.ToString();controller_number=[int]$drive.ControllerNumber;controller_location=[int]$drive.ControllerLocation}
 }
 $nics=@(Get-VMNetworkAdapter -VM $vm -ErrorAction Stop|Sort-Object Id);Need ($nics.Count -gt 0);$networks=@()
 foreach($nic in $nics){
  $switchId=''
  if($null -ne $nic.SwitchId -and [string]$nic.SwitchId -cne ''){
   Need ($nic.SwitchId -is [guid] -and $nic.SwitchId -ne [guid]::Empty)
   $switch=@(Get-VMSwitch -Id $nic.SwitchId -ErrorAction Stop)
   Need ($switch.Count -eq 1 -and $switch[0].Id -eq $nic.SwitchId);$switchId=$nic.SwitchId.ToString('D')
  }
  $networks+=,[ordered]@{adapter_id=$nic.Id.ToString();switch_id=$switchId;mac_sha256=(HashText $nic.MacAddress)}
 }
 Need ([bool](Get-Command Get-VMAssignableDevice -ErrorAction SilentlyContinue))
 Need (@(Get-VMAssignableDevice -VM $vm -ErrorAction Stop).Count -eq 0)
 $hardware[$node]=@{vm_id_sha256=$c.ids.$node;disks_sha256=(HashText (ConvertTo-Json -InputObject $disks -Depth 5 -Compress));switches_sha256=(HashText (ConvertTo-Json -InputObject $networks -Depth 5 -Compress))}
 $states[$node]=$vm.State.ToString()
}
Need ((Boot) -ceq $c.boot)
$after=[long](([DateTime]::UtcNow.Ticks-621355968000000000)*100)
@{challenge=$c.challenge;windows_boot_sha256=$c.boot;utc_before_ns=$before;utc_after_ns=$after;states=$states;hardware=$hardware}|ConvertTo-Json -Depth 5 -Compress
'''


def read_windows_snapshot(windows, *, vm_id_sha256, expected_windows_boot_sha256,
                          challenge, states, deadline, assert_guard):
    """Explicit read-only Hyper-V query over the existing pinned Windows SSH."""
    _need(type(windows) is WindowsSsh and callable(assert_guard)
          and set(vm_id_sha256) == set(states) == set(NODES)
          and all(wire._hash(pin) for pin in vm_id_sha256.values())
          and vm_id_sha256['A'] != vm_id_sha256['B']
          and all(state in {'Running', 'Saved'} for state in states.values())
          and wire._hash(expected_windows_boot_sha256) and wire._hash(challenge))
    config = {'ids': dict(vm_id_sha256), 'boot': expected_windows_boot_sha256,
              'challenge': challenge, 'states': dict(states)}
    source = _WINDOWS_READ.replace('__CONFIG__', base64.b64encode(wire._bytes(config)).decode('ascii'))
    assert_guard();wire._remaining(deadline)
    result = windows.powershell(source, deadline)
    assert_guard();wire._remaining(deadline)
    _need(type(result) is dict and set(result) == {'challenge', 'windows_boot_sha256', 'utc_before_ns',
          'utc_after_ns', 'states', 'hardware'} and result['challenge'] == challenge
          and result['windows_boot_sha256'] == expected_windows_boot_sha256
          and result['states'] == config['states'] and type(result['hardware']) is dict
          and set(result['hardware']) == set(NODES)
          and all(type(row) is dict and set(row) == HARDWARE_FIELDS for row in result['hardware'].values()))
    snapshot = WindowsVmSnapshot(**(result | {'hardware': {
        node: VmHardware(**row) for node, row in result['hardware'].items()}}))
    _need(all(snapshot.hardware[node].vm_id_sha256 == config['ids'][node] for node in NODES))
    return snapshot


def collect(windows, *, vm_id_sha256, expected_windows_boot_sha256, guest_reader,
            vsock_reader, deadline, assert_guard):
    """Bracket real readers with Windows hardware/clock checks, on either boot.

    Each callback receives keyword arguments node, challenge,
    windows_boot_sha256, vm_id_sha256 and deadline. guest_reader returns a
    GuestObservation after native inventory/hold and independent CA/clock
    validation; vsock_reader returns an actual VsockObservation. Neither may
    replace missing proof with a boolean. Callbacks own their pinned transport.
    """
    _need(callable(guest_reader) and callable(vsock_reader) and callable(assert_guard))
    vm_id_sha256 = dict(vm_id_sha256)
    challenge = secrets.token_hex(32)
    kwargs = dict(vm_id_sha256=vm_id_sha256, expected_windows_boot_sha256=expected_windows_boot_sha256,
                  challenge=challenge, states=dict.fromkeys(NODES, 'Running'), deadline=deadline, assert_guard=assert_guard)
    before = read_windows_snapshot(windows, **kwargs)
    def node_read(node):
        scope = dict(node=node, challenge=challenge, windows_boot_sha256=expected_windows_boot_sha256,
                     vm_id_sha256=vm_id_sha256[node], deadline=deadline)
        assert_guard();wire._remaining(deadline)
        guest = guest_reader(**scope)
        assert_guard();wire._remaining(deadline)
        return guest
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = dict(zip(NODES, pool.map(node_read, NODES), strict=True))
    # Guest SSH reads are independent; Windows VSock callbacks share the
    # caller's authenticated host transport and are deliberately sequential.
    vsock = {}
    for node in NODES:
        assert_guard();wire._remaining(deadline)
        vsock[node] = vsock_reader(node=node, challenge=challenge,
            windows_boot_sha256=expected_windows_boot_sha256,
            vm_id_sha256=vm_id_sha256[node], deadline=deadline)
        assert_guard();wire._remaining(deadline)
    after = read_windows_snapshot(windows, **kwargs)
    return GuestPairCapture(before, after, rows, vsock)


def assemble_saved_evidence(before, resumed, *, saved_snapshot, pre_save_seal, native_resume, carryover_sha256):
    """Produce exactly assemble_material's saved-reboot evidence contract.

    The caller supplies the genuine protected pre-save seal and fresh native
    resume receipt including its new boot and closed-pair result. No
    historical proof or SSH success grants fresh_vsock.
    The downstream native assembler still authenticates the entire seal against
    the exact predecessor bytes, stopped service and original recovery scope.
    """
    _need(type(before) is GuestPairCapture and type(resumed) is GuestPairCapture
          and type(saved_snapshot) is WindowsVmSnapshot
          and dict(saved_snapshot.states) == dict.fromkeys(NODES, 'Saved')
          and saved_snapshot.windows_boot_sha256 == before.before.windows_boot_sha256
          and saved_snapshot.hardware == before.before.hardware
          and saved_snapshot.challenge not in {before.before.challenge, resumed.before.challenge}
          and before.after.utc_after_ns <= saved_snapshot.utc_before_ns
          and saved_snapshot.utc_after_ns < resumed.before.utc_before_ns
          and type(pre_save_seal) is bytes and 0 < len(pre_save_seal) <= 8192
          and wire._hash(carryover_sha256))
    seal = json.loads(pre_save_seal, object_pairs_hook=wire._pairs)
    _need(type(seal) is dict and seal.get('schema_version') == 1
          and type(seal['schema_version']) is int
          and seal.get('windows_boot_sha256') == before.before.windows_boot_sha256
          and seal.get('guests') == before.guests() == resumed.guests()
          and seal.get('saved') == dict.fromkeys(NODES, 'Saved')
          and seal.get('hold_sha256') == before.observations['A'].hold_sha256 == resumed.observations['A'].hold_sha256
          and before.observations['A'].container_inventory_sha256 == carryover_sha256
          and resumed.before.windows_boot_sha256 != before.before.windows_boot_sha256
          and before.before.challenge != resumed.before.challenge
          and before.after.utc_after_ns < resumed.before.utc_before_ns
          and all(before.observations[node].docker_inventory_sha256 == resumed.observations[node].docker_inventory_sha256
                  and before.observations[node].container_count == resumed.observations[node].container_count
                  and before.observations[node].clock_ca_proof_sha256 != resumed.observations[node].clock_ca_proof_sha256
                  and before.vsock[node].proof_sha256 != resumed.vsock[node].proof_sha256 for node in NODES))
    _need(type(native_resume) is dict and set(native_resume) == {'windows_boot_sha256', 'vm_id_sha256', 'states',
          'plan_sha256', 'binding_sha256', 'terminal_sha256', 'closed_pair'}
          and native_resume['windows_boot_sha256'] == resumed.before.windows_boot_sha256
          and native_resume['states'] == dict.fromkeys(NODES, 'Running')
          and native_resume['vm_id_sha256'] == {node: resumed.before.hardware[node].vm_id_sha256 for node in NODES}
          and wire._hash(native_resume['plan_sha256'])
          and native_resume['binding_sha256'] == seal.get('predecessor_binding_sha256')
          and wire._hash(native_resume['binding_sha256'])
          and native_resume['terminal_sha256'] == seal.get('predecessor_terminal_sha256'))
    closed_pair = native_resume['closed_pair']
    _need(type(closed_pair) is dict and closed_pair.get('closed_nodes') == list(NODES)
          and all(closed_pair.get(key) is True for key in ('synchronous', 'same_expiry', 'replay_verified', 'effects_isolated'))
          and wire._hash(closed_pair.get('closed_proof_sha256'))
          and closed_pair.get('terminal_sha256') == seal.get('predecessor_terminal_sha256')
          and wire._hash(closed_pair.get('terminal_sha256'))
          and type(closed_pair.get('revision')) is int and closed_pair['revision'] == seal.get('closed_revision')
          and closed_pair['revision'] > 0 and closed_pair['revision'] % 2 == 1
          and wire._hash(seal.get('closed_pair_sha256'))
          and closed_pair['closed_proof_sha256'] != seal['closed_pair_sha256'])
    return {'closed_pair_sha256': closed_pair['closed_proof_sha256'], 'closed_revision': closed_pair['revision'],
            'carryover_sha256': carryover_sha256, 'guests': resumed.guests(),
            'saved': dict.fromkeys(NODES, 'Saved'), 'resumed': dict.fromkeys(NODES, 'Running'),
            'new_windows_boot_sha256': resumed.before.windows_boot_sha256,
            'guest_observation_sha256': {'A': carryover_sha256, 'B': resumed.observations['B'].digest()},
            'clock_ca_validated': dict.fromkeys(NODES, True), 'fresh_vsock': dict.fromkeys(NODES, True)}
