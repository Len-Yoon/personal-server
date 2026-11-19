"""Mac-side SSH transport for one fixed, hash-verified guest worker invocation."""

import base64
from collections import deque
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import shlex
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time

from ha_runtime.config import SERVICES

from . import native_stdio_bridge as bridge


SSH = '/usr/bin/ssh'
SSH_KEYGEN = '/usr/bin/ssh-keygen'
FINGERPRINT = re.compile(r'SHA256:[A-Za-z0-9+/]{43}\Z')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,252}\Z')
USER = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\Z')
LOOKUP = re.compile(r'(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,252}|\[[A-Za-z0-9][A-Za-z0-9_.-]{0,252}\]:[0-9]{1,5})\Z')
ALGORITHMS = {'ssh-ed25519': 'ssh-ed25519', 'ecdsa-sha2-nistp256': 'ecdsa-sha2-nistp256',
              'ssh-rsa': 'rsa-sha2-512,rsa-sha2-256'}
WORKER = '/var/lib/n100-ha/cutover/native-guest-worker.py'
ACTIVATION_BOUNDARIES = (
    'activation_intent', *(f'publish:apps/{service}/activation.json' for service in sorted(SERVICES)),
    'publish:broker/authority.json', 'start:ha-broker', *(f'start:{service}' for service in sorted(SERVICES)),
    'guest_started',
)
BOOTSTRAP = """import hashlib
import os
from pathlib import Path
import stat
import sys

if os.geteuid() != 0 or len(sys.argv) != 3:
    raise SystemExit(1)
path = Path(sys.argv[1])
for parent in path.parents:
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit(1)
with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as source:
    info = os.fstat(source.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o022:
        raise SystemExit(1)
    raw = source.read(131073)
    if len(raw) != info.st_size or not 0 < len(raw) <= 131072 or hashlib.sha256(raw).hexdigest() != sys.argv[2]:
        raise SystemExit(1)
sys.argv = [str(path)]
exec(compile(raw, str(path), 'exec'), {'__name__': '__main__', '__file__': str(path)})
"""


@dataclass(frozen=True)
class SshEndpoint:
    host: str
    port: int
    user: str
    alias: str
    identity_file: Path
    identity_fingerprint: str
    host_fingerprint: str
    known_host_name: str | None = None

    def validate(self, *, guest):
        bridge._need(type(self.host) is str and NAME.fullmatch(self.host)
                     and type(self.alias) is str and NAME.fullmatch(self.alias)
                     and type(self.user) is str and USER.fullmatch(self.user)
                     and type(self.port) is int and 1 <= self.port <= 65535
                     and type(self.identity_fingerprint) is str and FINGERPRINT.fullmatch(self.identity_fingerprint)
                     and type(self.host_fingerprint) is str and FINGERPRINT.fullmatch(self.host_fingerprint)
                     and (self.known_host_name is None or type(self.known_host_name) is str
                          and LOOKUP.fullmatch(self.known_host_name)), 'guest_ssh_endpoint_rejected')
        if guest:
            try:
                ip = ipaddress.IPv4Address(self.host)
                bridge._need(self.user == 'root' and str(ip) == self.host and ip.is_private and not ip.is_loopback
                             and not ip.is_link_local and not ip.is_multicast and not ip.is_reserved
                             and not ip.is_unspecified, 'guest_ssh_endpoint_rejected')
            except ValueError:
                raise bridge.BridgeRejected('guest_ssh_endpoint_rejected') from None


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _checked_file(path, *, private, maximum, read=True):
    path = Path(path)
    bridge._need(path.is_absolute() and '..' not in path.parts and path.resolve(strict=True) == path
                 and not any(char in str(path) for char in '\r\n%"\\$`'), 'guest_ssh_local_file_rejected')
    for parent in path.parents:
        item = parent.lstat()
        bridge._need(stat.S_ISDIR(item.st_mode) and item.st_uid in {0, os.geteuid()}
                     and (not item.st_mode & 0o022 or item.st_uid == 0 and item.st_mode & stat.S_ISVTX),
                     'guest_ssh_local_file_rejected')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        info = os.fstat(stream.fileno())
        bridge._need(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1
                     and not info.st_mode & (0o077 if private else 0o022)
                     and 0 < info.st_size <= maximum, 'guest_ssh_local_file_rejected')
        raw = stream.read(maximum + 1) if read else None
        bridge._need((raw is None or len(raw) == info.st_size) and _stamp(os.fstat(stream.fileno())) == _stamp(info)
                     and _stamp(path.lstat()) == _stamp(info), 'guest_ssh_local_file_changed')
    return raw, _stamp(info)


def _fingerprint(blob):
    return 'SHA256:' + base64.b64encode(hashlib.sha256(blob).digest()).decode('ascii').rstrip('=')


def _field(raw, offset):
    bridge._need(offset + 4 <= len(raw), 'guest_ssh_key_rejected')
    end = offset + 4 + struct.unpack_from('>I', raw, offset)[0]
    bridge._need(end <= len(raw), 'guest_ssh_key_rejected')
    return raw[offset + 4:end], end


def _identity(endpoint, deadline):
    path = Path(endpoint.identity_file)
    _, stamp = _checked_file(path, private=True, maximum=65536, read=False)
    # Derive the public key from the private file. Fingerprinting by filename
    # alone can otherwise use a different .pub sidecar.
    raw = _exchange((SSH_KEYGEN, '-y', '-P', '', '-f', str(path)), b'', deadline)
    fields = raw.split()
    bridge._need(len(fields) >= 2, 'guest_ssh_key_rejected')
    public = base64.b64decode(fields[1], validate=True)
    bridge._need(_fingerprint(public) == endpoint.identity_fingerprint, 'guest_ssh_identity_fingerprint_mismatch')
    sidecar = path.with_name(path.name + '.pub')
    stamps = {path: stamp, sidecar: None}
    if os.path.lexists(sidecar):
        raw, stamps[sidecar] = _checked_file(sidecar, private=False, maximum=65536)
        fields = raw.split()
        bridge._need(len(fields) >= 2 and base64.b64decode(fields[1], validate=True) == public,
                     'guest_ssh_identity_fingerprint_mismatch')
    return stamps


def _host_matches(hosts, lookup):
    if lookup in hosts.lower().split(','):
        return True
    if hosts.startswith('|1|'):
        _, version, salt, expected = hosts.split('|')
        if version == '1':
            wanted = hmac.digest(base64.b64decode(salt, validate=True), lookup.encode('ascii'), 'sha1')
            return hmac.compare_digest(wanted, base64.b64decode(expected, validate=True))
    return False


def _host_record(raw, endpoint, *, guest):
    lookup = (endpoint.known_host_name or endpoint.alias).lower()
    selected = set()
    for line in raw.decode('utf-8').splitlines():
        fields = line.split()
        if not fields or fields[0].startswith('#'):
            continue
        marker = fields.pop(0) if fields[0].startswith('@') else None
        if len(fields) < 3 or fields[1] not in ALGORITHMS:
            continue
        hosts, kind, encoded = fields[:3]
        # Only exact aliases are selected. This temporary trust store never
        # inherits wildcard entries, CAs or unpinned host keys.
        if marker != '@revoked' and (marker is not None or not _host_matches(hosts, lookup)):
            continue
        blob = base64.b64decode(encoded, validate=True)
        if _fingerprint(blob) != endpoint.host_fingerprint:
            continue
        algorithm, _ = _field(blob, 0)
        bridge._need(marker is None and algorithm == kind.encode('ascii') and (not guest or kind == 'ssh-ed25519'),
                     'guest_ssh_host_key_rejected')
        selected.add((kind, encoded))
    bridge._need(len(selected) == 1, 'guest_ssh_host_key_unconfirmed')
    return selected.pop()


def _same_files(stamps):
    return all(not os.path.lexists(path) if stamp is None else _stamp(path.lstat()) == stamp
               for path, stamp in stamps.items())


def _private_write(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as stream:
        stream.write(value.encode('ascii'))


def _ssh_config(directory, jump, guest, records):
    known_hosts = directory / 'known_hosts'
    endpoints = (jump,) if guest is None else (guest, jump)
    _private_write(known_hosts, ''.join(f'{item.alias} {records[item.alias][0]} {records[item.alias][1]}\n'
                                       for item in endpoints))
    stanzas = []
    for item in endpoints:
        options = {'HostName': item.host, 'HostKeyAlias': item.alias, 'User': item.user, 'Port': item.port,
                   'IdentityFile': '"' + str(item.identity_file) + '"',
                   'HostKeyAlgorithms': ALGORITHMS[records[item.alias][0]],
                   'UserKnownHostsFile': '"' + str(known_hosts) + '"'}
        if item is guest:
            options['ProxyJump'] = jump.alias
        stanzas.append('Host ' + item.alias + '\n' + ''.join(f'  {key} {value}\n' for key, value in options.items()))
    stanzas.append('''Host *
  BatchMode yes
  IdentitiesOnly yes
  IdentityAgent none
  CertificateFile none
  AddKeysToAgent no
  PreferredAuthentications publickey
  PasswordAuthentication no
  KbdInteractiveAuthentication no
  ForwardAgent no
  ForwardX11 no
  ClearAllForwardings yes
  PermitLocalCommand no
  RequestTTY no
  CanonicalizeHostname no
  ControlMaster no
  ControlPath none
  ControlPersist no
  StrictHostKeyChecking yes
  VerifyHostKeyDNS no
  UpdateHostKeys no
  GlobalKnownHostsFile /dev/null
  ConnectionAttempts 1
  ConnectTimeout 5
  ServerAliveInterval 2
  ServerAliveCountMax 1
  LogLevel ERROR
''')
    config = directory / 'config'
    _private_write(config, ''.join(stanzas))
    return config


def _exchange(command, payload, deadline):
    """Bound stdout and both pipe directions. Kill only this local SSH group."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}, start_new_session=True)
    completed = False
    try:
        with selectors.DefaultSelector() as selector:
            for stream, action in ((process.stdin, selectors.EVENT_WRITE), (process.stdout, selectors.EVENT_READ)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, action)
            sent, output = 0, bytearray()
            while selector.get_map():
                events = selector.select(bridge._remaining(deadline))
                bridge._need(events, 'guest_ssh_deadline_exceeded')
                for event, action in events:
                    stream = event.fileobj
                    try:
                        if action == selectors.EVENT_WRITE:
                            sent += os.write(event.fd, payload[sent:])
                            done = sent == len(payload)
                        else:
                            raw = os.read(event.fd, bridge.FRAME_BYTES + 1 - len(output))
                            output.extend(raw)
                            bridge._need(len(output) <= bridge.FRAME_BYTES, 'guest_ssh_frame_limit')
                            done = not raw
                    except BlockingIOError:
                        continue
                    if done:
                        selector.unregister(stream)
                        stream.close()
        bridge._need(process.wait(timeout=bridge._remaining(deadline)) == 0, 'guest_ssh_exit_unconfirmed')
        completed = True
        return bytes(output)
    finally:
        if not completed:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
        process.stdin.close()
        process.stdout.close()


class GuestSshDispatcher:
    """One local bridge binding, with independently pinned jump and guest identities."""

    def __init__(self, binding, *, jump, guests, known_hosts, launcher_sha256, run=_exchange):
        bridge._need(type(binding) is bridge.Binding and type(jump) is SshEndpoint
                     and isinstance(guests, Mapping) and set(guests) == set(bridge.NODES)
                     and all(type(guest) is SshEndpoint for guest in guests.values())
                     and bridge._hash(launcher_sha256) and callable(run), 'guest_ssh_binding_rejected')
        binding.fields()
        jump.validate(guest=False)
        for endpoint in guests.values():
            endpoint.validate(guest=True)
        endpoints = (jump, *guests.values())
        bridge._need(len({endpoint.alias.lower() for endpoint in endpoints}) == 3
                     and len({(endpoint.host.lower(), endpoint.port) for endpoint in endpoints}) == 3
                     and len({endpoint.host_fingerprint for endpoint in endpoints}) == 3, 'guest_ssh_endpoint_rejected')
        if isinstance(known_hosts, Mapping):
            bridge._need(set(known_hosts) == {'jump', *bridge.NODES}, 'guest_ssh_binding_rejected')
            self.known_hosts = {key: Path(value) for key, value in known_hosts.items()}
        else:
            self.known_hosts = {key: Path(known_hosts) for key in ('jump', *bridge.NODES)}
        self.binding, self.jump, self.guests = binding, jump, dict(guests)
        self.launcher_sha256 = launcher_sha256
        self.command = shlex.join(('/usr/bin/python3', '-I', '-B', '-S', '-c', BOOTSTRAP, WORKER, launcher_sha256))
        self.run = run
        self.lock = threading.Lock()
        self.last_sequence = 0
        self.nonces = set()
        self.inflight = False

    def __call__(self, request, deadline, *, assert_windows_current=None):
        bridge._need(type(request) is bridge.Request and request.verb not in
                     {bridge.GUEST_PREFLIGHT_REPLACEMENT, bridge.GUEST_PUBLISH_REPLACEMENT, bridge.GUEST_START_REPLACEMENT, bridge.GUEST_OBSERVE_REPLACEMENT,
                      bridge.GUEST_PREPARE_GENERATION, bridge.GUEST_OBSERVE_PREPARATION, bridge.GUEST_PUBLISH_GENERATION, bridge.GUEST_ACTIVATE_GENERATION, bridge.GUEST_OBSERVE_GENERATION,
                      bridge.GUEST_VERIFY_GENERATION, bridge.GUEST_VERIFY_PREPARED_GENERATION, bridge.GUEST_TRANSITION_TARGET,
                      bridge.GUEST_RETIRE_INITIAL_STANDBY_CONTAINERS, bridge.GUEST_QUIESCE_INITIAL_RECOVERY,
                      bridge.GUEST_OBSERVE_INITIAL_RECOVERY_HOLD, bridge.GUEST_OBSERVE_CAR_ISOLATION, bridge.GUEST_START_INITIAL_RECOVERY, bridge.GUEST_START_INITIAL_REARM, bridge.GUEST_START_INITIAL_POSTBOOT, bridge.GUEST_QUIESCE_INITIAL_POSTBOOT, bridge.GUEST_START_INITIAL_UNPUBLISHED, bridge.GUEST_QUIESCE_POSTBOOT_UNPUBLISHED},
                     'guest_ssh_replacement_guard_required')
        return self._dispatch(request, deadline, assert_windows_current=assert_windows_current)

    def isolated_replacement_preflight(self, node, *, endpoint, known_hosts, jump=None, jump_known_hosts=None):
        """Build a preflight-only connection with the same replacement host key."""
        error = "guest_ssh_isolated_replacement_unbound"
        bridge._need(node in self.guests and type(endpoint) is SshEndpoint, error)
        serving = self.guests[node]
        bridge._need(endpoint.host_fingerprint == serving.host_fingerprint
                     and endpoint.identity_fingerprint == serving.identity_fingerprint, error)
        if jump is None:
            bridge._need(jump_known_hosts is None and endpoint.host != serving.host, error)
            jump, jump_known_hosts = self.jump, self.known_hosts["jump"]
        else:
            bridge._need(type(jump) is SshEndpoint and jump_known_hosts is not None
                         and jump.host.lower() != self.jump.host.lower()
                         and jump.host_fingerprint != self.jump.host_fingerprint, error)
        isolated = GuestSshDispatcher(
            self.binding, jump=jump, guests=self.guests | {node: endpoint},
            known_hosts=self.known_hosts | {node: Path(known_hosts), "jump": Path(jump_known_hosts)},
            launcher_sha256=self.launcher_sha256, run=self.run,
        )

        def verify(request, deadline):
            bridge._need(type(request) is bridge.Request and request.node == node, error)
            return isolated.preflight_replacement_standby(request, deadline)

        return verify

    def assert_serving_endpoints(self, pg_nodes):
        """Bind subsequent SSH requests to both approved PostgreSQL hosts."""
        bridge._need(type(pg_nodes) is dict and set(pg_nodes) == set(self.guests)
                     and all(type(pg_nodes[node]) is dict and pg_nodes[node].get("host") == guest.host
                             for node, guest in self.guests.items()),
                     "guest_ssh_serving_endpoint_unbound")
        return True

    def retire_initial_standby_containers(self, request, deadline):
        """Apply an already reviewed B-only plan through the pinned guest transport."""
        error = "guest_ssh_initial_standby_retirement_unconfirmed"
        bridge._need(type(request) is bridge.Request and request.node == "B"
                     and request.verb == bridge.GUEST_RETIRE_INITIAL_STANDBY_CONTAINERS, error)
        result = self._dispatch(request, deadline)
        expected = bridge._digest(bridge._bytes({
            "status": "CONTAINERS_RETIRED", "plan_sha256": request.args["retirement_plan_sha256"],
            "count": len(SERVICES) + 1,
        }))
        bridge._need(result.status == "PASS" and result.receipt_sha256 == expected, error)
        return result

    def verify_transition_target(self, request, generation, target, deadline):
        """Check the exact route address via pinned SSH, node, installation and boot identity."""
        generation.validate()
        bridge._need(type(request) is bridge.Request and request.verb == bridge.GUEST_TRANSITION_TARGET
                     and generation.epoch > 1 and request.node == generation.primary
                     and target == self.guests[request.node].host, "guest_ssh_transition_target_unbound")
        receipt = self._dispatch(request, deadline)
        bridge._need(receipt.status == "PASS" and bridge._hash(receipt.receipt_sha256),
                     "guest_ssh_transition_target_unconfirmed")
        return True

    def verify_prepared_transition_target(self, request, generation, target, pins, runtime_receipt_sha256, deadline):
        """Bind the target to verified staged inputs or a stopped-runtime receipt."""
        generation.validate()
        error = "guest_ssh_prepared_generation_unbound"
        bridge._need(type(request) is bridge.Request and request.verb == bridge.GUEST_TRANSITION_TARGET
                     and request.binding == self.binding and request.node == generation.primary
                     and target == self.guests[request.node].host
                     and (runtime_receipt_sha256 is None or bridge._hash(runtime_receipt_sha256))
                     and pins.node == request.node and pins.generation == generation.epoch
                     and pins.installation_id == request.args.get("installation_id"), error)
        bridge._args(request.verb, request.node, dict(request.args))
        observation = replace(request, verb=bridge.GUEST_VERIFY_PREPARED_GENERATION, args={
            "runtime": asdict(pins), "runtime_receipt_sha256": runtime_receipt_sha256,
            "target_sha256": bridge._digest(target.encode("ascii")),
        })
        receipt = self._dispatch(observation, deadline)
        bridge._need(receipt.status == "PASS" and bridge._hash(receipt.receipt_sha256)
                     and (runtime_receipt_sha256 is None or receipt.receipt_sha256 == runtime_receipt_sha256), error)
        return True

    def prepare_generation_runtime(self, request, generation, stage_pin, replay_floor, deadline, *, assert_rejoin):
        generation.validate()
        error = "guest_ssh_prepare_generation_unbound"
        bridge._need(type(request) is bridge.Request and request.verb == bridge.GUEST_PREPARE_GENERATION
                     and request.node == generation.primary and request.binding == self.binding
                     and isinstance(request.args, Mapping) and bridge._hash(stage_pin)
                     and type(replay_floor) is int and 0 < replay_floor < 2**64 and callable(assert_rejoin), error)
        bridge._args(request.verb, request.node, dict(request.args))
        bridge._need(bridge._matches(request.args["generation"], asdict(generation))
                     and request.args["rejoin_stage_sha256"] == stage_pin
                     and f"{replay_floor:016x}" in bridge.rejoin_wal_evidence(request.args, error).values(), error)
        result = self._dispatch(request, deadline, assert_windows_current=assert_rejoin)
        bridge._need(result.status == "PASS" and bridge._hash(result.receipt_sha256), error)
        return result.receipt_sha256

    def observe_generation_preparation(self, request, deadline, *, assert_rejoin):
        bridge._need(type(request) is bridge.Request and request.verb == bridge.GUEST_OBSERVE_PREPARATION
                     and callable(assert_rejoin), "guest_ssh_prepare_generation_unbound")
        before, after = bridge.PREPARATION_OBSERVATION_BOUNDARIES
        bridge._need(bridge._hash(assert_rejoin(secrets.token_hex(32), before, deadline)),
                     "guest_ssh_prepare_generation_unbound")
        result = self._dispatch(request, deadline)
        bridge._need(bridge._hash(assert_rejoin(secrets.token_hex(32), after, deadline))
                     and result.status == "PASS" and bridge._hash(result.receipt_sha256),
                     "guest_ssh_prepare_generation_unbound")
        return result.receipt_sha256

    def publish_generation_handoff(self, request, generation, instance, replay_floor, deadline):
        """Publish only the approved boot-bound handoff for this generation and WAL floor."""
        generation.validate()
        error = "guest_ssh_generation_handoff_unbound"
        bridge._need(type(request) is bridge.Request and request.verb == bridge.GUEST_PUBLISH_GENERATION
                     and request.node == generation.primary and request.binding == self.binding
                     and isinstance(request.args, Mapping) and bridge._hash(instance)
                     and type(replay_floor) is int and 0 < replay_floor < 2**64, error)
        bridge._args(request.verb, request.node, dict(request.args))
        handoff = request.args["handoff"]
        observed = handoff.get("activation")
        expected_generation = {"primary": generation.primary, "epoch": generation.epoch,
                               "timeline": generation.timeline, "revision": generation.revision}
        digest = bridge._digest(bridge._bytes(handoff))
        bridge._need(handoff.get("binding") == request.binding.fields()
                     and bridge._matches(handoff.get("generation"), expected_generation)
                     and type(observed) is dict and observed.get("windows_service_instance_sha256") == instance
                     and type(handoff.get("replay_floor")) is int and handoff["replay_floor"] == replay_floor
                     and request.args["generation_handoff_sha256"] == digest, error)
        receipt = self._dispatch(request, deadline)
        bridge._need(receipt.status == "PASS" and receipt.receipt_sha256 == digest, error)
        return digest

    def activate_generation(self, request, deadline, *, assert_windows_current):
        """Require an authenticated Windows proof for each challenge and boundary."""
        bridge._need(type(request) is bridge.Request and request.verb == bridge.GUEST_ACTIVATE_GENERATION,
                     'guest_ssh_request_unbound')
        return self._dispatch(request, deadline, assert_windows_current=assert_windows_current)

    def quiesce_initial_recovery(self, request, deadline, *, assert_windows_closed):
        error = "guest_ssh_initial_recovery_unbound"
        bridge._need(type(request) is bridge.Request and request.verb == bridge.GUEST_QUIESCE_INITIAL_RECOVERY
                     and request.node == "A" and callable(assert_windows_closed), error)
        return self._dispatch(request, deadline, assert_windows_current=assert_windows_closed)

    def start_initial_recovery(self, request, deadline, *, assert_windows_current):
        error = "guest_ssh_initial_recovery_unbound"
        bridge._need(type(request) is bridge.Request and request.verb == bridge.GUEST_START_INITIAL_RECOVERY
                     and request.node == "A" and callable(assert_windows_current), error)
        return self._dispatch(request, deadline, assert_windows_current=assert_windows_current)

    def start_initial_rearm(self, request, deadline, *, assert_windows_current):
        error = "guest_ssh_initial_rearm_unbound"
        bridge._need(type(request) is bridge.Request and request.verb == bridge.GUEST_START_INITIAL_REARM
                     and request.node == "A" and callable(assert_windows_current), error)
        return self._dispatch(request, deadline, assert_windows_current=assert_windows_current)

    def start_initial_postboot(self, request, deadline, *, assert_windows_current):
        error = "guest_ssh_initial_postboot_unbound"
        bridge._need(type(request) is bridge.Request and request.verb == bridge.GUEST_START_INITIAL_POSTBOOT
                     and request.node == "A" and callable(assert_windows_current), error)
        return self._dispatch(request, deadline, assert_windows_current=assert_windows_current)

    def quiesce_initial_postboot(self, request, deadline, *, assert_windows_closed):
        bridge._need(type(request) is bridge.Request and request.verb == bridge.GUEST_QUIESCE_INITIAL_POSTBOOT
                     and request.node == "A" and callable(assert_windows_closed),'guest_postboot_stop_unbound')
        return self._dispatch(request,deadline,assert_windows_current=assert_windows_closed)

    def observe_initial_postboot(self, request, deadline):
        bridge._need(type(request) is bridge.Request and request.verb == bridge.GUEST_OBSERVE_INITIAL_POSTBOOT
                     and request.node == 'A','guest_postboot_observation_unbound')
        return self._dispatch(request,deadline)

    def start_initial_unpublished(self,request,deadline,*,assert_windows_current):
        bridge._need(type(request) is bridge.Request and request.verb==bridge.GUEST_START_INITIAL_UNPUBLISHED
                     and request.node=='A' and callable(assert_windows_current),'guest_unpublished_start_unbound')
        return self._dispatch(request,deadline,assert_windows_current=assert_windows_current)

    def observe_unpublished_predecessor(self,request,deadline):
        bridge._need(type(request) is bridge.Request and request.verb==bridge.GUEST_OBSERVE_UNPUBLISHED_PREDECESSOR
                     and request.node=='A','guest_unpublished_predecessor_unbound')
        return self._dispatch(request,deadline)

    def quiesce_postboot_unpublished(self,request,deadline,*,assert_windows_closed):
        bridge._need(type(request) is bridge.Request and request.verb==bridge.GUEST_QUIESCE_POSTBOOT_UNPUBLISHED
                     and request.node=='A' and callable(assert_windows_closed),'guest_postboot_stop_unbound')
        return self._dispatch(request,deadline,assert_windows_current=assert_windows_closed)

    def observe_initial_recovery_hold(self, request, deadline):
        bridge._need(type(request) is bridge.Request and request.verb == bridge.GUEST_OBSERVE_INITIAL_RECOVERY_HOLD
                     and request.node == "A", 'guest_ssh_initial_recovery_unbound')
        return self._dispatch(request, deadline)

    def observe_car_isolation(self, request, deadline):
        error = "guest_ssh_car_isolation_unconfirmed"
        bridge._need(type(request) is bridge.Request and request.verb == bridge.GUEST_OBSERVE_CAR_ISOLATION
                     and request.node == "A", error)
        receipt = self._dispatch(request, deadline)
        bridge._need(type(receipt) is bridge.Receipt and receipt.status == "PASS"
                     and bridge._hash(receipt.receipt_sha256), error)
        return receipt.receipt_sha256

    def verify_initial_recovery_hold(self, args, sequence, challenge_sha256, hold_sha256, deadline):
        """Bind a fresh authenticated guest readback to a specific recovery challenge."""
        error = "guest_ssh_initial_recovery_hold_unconfirmed"
        bridge._need(bridge._hash(challenge_sha256) and bridge._hash(hold_sha256)
                     and type(sequence) is int and 0 < sequence <= bridge.MAX_REQUESTS
                     and type(args) is dict, error)
        args = args.copy()
        verb = bridge.GUEST_OBSERVE_INITIAL_RECOVERY_HOLD
        bridge._args(verb, "A", args)
        evidence = {"binding": self.binding.fields(), "node": "A", "verb": verb,
                    "sequence": sequence, "args": args,
                    "challenge_sha256": challenge_sha256, "hold_sha256": hold_sha256}
        # On the SSH wire the guest echoes this nonce in its authenticated reply.
        nonce = bridge._digest(bridge._bytes(evidence))
        request = bridge.Request(self.binding, "A", verb, sequence, nonce, args)
        bridge._remaining(deadline)
        readback = self.observe_initial_recovery_hold(request, deadline)
        bridge._need(type(readback) is bridge.Receipt and readback.status == "PASS"
                     and readback.receipt_sha256 == hold_sha256, error)
        bridge._remaining(deadline)
        return nonce

    def observe_generation_activation(self, request, deadline, *, assert_guard):
        bridge._need(type(request) is bridge.Request and request.verb == bridge.GUEST_OBSERVE_GENERATION
                     and callable(assert_guard), 'guest_ssh_request_unbound')
        assert_guard()
        receipt = self._dispatch(request, deadline)
        assert_guard()
        return receipt

    def verify_live_generation(self, request, generation, target, deadline, *, assert_guard):
        generation.validate()
        bridge._need(type(request) is bridge.Request and request.verb == bridge.GUEST_VERIFY_GENERATION
                     and request.node == generation.primary and target == self.guests[request.node].host
                     and request.args.get("target_sha256") == bridge._digest(target.encode("ascii"))
                     and callable(assert_guard), "guest_ssh_live_generation_unbound")
        assert_guard()
        result = self._dispatch(request, deadline)
        assert_guard()
        bridge._need(result.status == "PASS" and bridge._hash(result.receipt_sha256),
                     "guest_ssh_live_generation_unconfirmed")
        return result.receipt_sha256

    def preflight_replacement_standby(self, request, deadline):
        error = "guest_ssh_replacement_preflight_unconfirmed"
        bridge._need(type(request) is bridge.Request and request.verb == bridge.GUEST_PREFLIGHT_REPLACEMENT, error)
        receipt = self._dispatch(request, deadline)
        expected = {"version": bridge.VERSION, "binding": request.binding.fields(), "node": request.node,
                    "boot_sha256": request.binding.boot(request.node), "verb": request.verb,
                    "sequence": request.sequence, "nonce": request.nonce, "args": request.args}
        bridge._need(receipt.status == "PASS" and receipt.receipt_sha256 == bridge._digest(bridge._bytes(expected)), error)
        return True

    def publish_replacement_standby(self, request, deadline, *, assert_rejoin_current):
        receipt = self._replacement(request, deadline, bridge.GUEST_PUBLISH_REPLACEMENT, assert_rejoin_current)
        expected = bridge._digest(bridge._bytes({
            "schema_version": 1, "binding": request.binding.fields(), "node": request.node, **request.args,
        }))
        bridge._need(receipt.receipt_sha256 == expected, "guest_ssh_replacement_grant_unconfirmed")
        return receipt

    def start_replacement_standby(self, request, deadline, *, assert_rejoin_current):
        return self._replacement(request, deadline, bridge.GUEST_START_REPLACEMENT, assert_rejoin_current)

    def observe_replacement_standby(self, request, deadline, *, assert_rejoin_current):
        return self._replacement(request, deadline, bridge.GUEST_OBSERVE_REPLACEMENT, assert_rejoin_current)

    def confirm_replacement_rejoin(self, request, deadline, *, assert_rejoin_current, check_synchronized):
        """Join the exact guest-container observation with the Windows-side PostgreSQL proof."""
        bridge._need(callable(check_synchronized), 'guest_ssh_replacement_guard_required')
        receipt = self.observe_replacement_standby(request, deadline, assert_rejoin_current=assert_rejoin_current)
        result = check_synchronized(deadline)
        expected = {
            'status': 'PASS', 'phase': 'rejoining', 'authority_opened': False, 'standby_rejoined': True,
            'rejoin_stage_sha256': request.args['rejoin_stage_sha256'],
        }
        revision = result.get('revision') if type(result) is dict else None
        bridge._need(type(revision) is int and 0 < revision < 2**63, 'guest_ssh_replacement_replication_unconfirmed')
        expected['revision'] = revision
        if type(result) is dict:
            expected |= bridge.rejoin_wal_evidence(result, 'guest_ssh_replacement_replication_unconfirmed', optional=True)
        bridge._need(bridge._matches(result, expected), 'guest_ssh_replacement_replication_unconfirmed')
        return receipt, revision

    def _replacement(self, request, deadline, verb, assert_rejoin_current):
        bridge._need(type(request) is bridge.Request and request.verb == verb
                     and request.binding == self.binding and isinstance(request.args, Mapping)
                     and callable(assert_rejoin_current),
                     'guest_ssh_request_unbound')
        bridge._args(request.verb, request.node, dict(request.args))
        stage = request.args['rejoin_stage_sha256']
        bridge._need(bridge._hash(stage) and assert_rejoin_current(deadline) == stage,
                     'guest_ssh_replacement_guard_required')
        receipt = self._dispatch(request, deadline)
        bridge._need(receipt.status == 'PASS' and assert_rejoin_current(deadline) == stage,
                     'guest_ssh_replacement_start_unconfirmed')
        return receipt

    def _dispatch(self, request, deadline, *, assert_windows_current=None):
        bridge._need(type(request) is bridge.Request and request.binding == self.binding
                     and type(request.sequence) is int and 0 < request.sequence <= bridge.MAX_REQUESTS
                     and bridge._hash(request.nonce) and isinstance(request.args, Mapping), 'guest_ssh_request_unbound')
        args = dict(request.args)
        bridge._args(request.verb, request.node, args)
        bridge._need((request.verb in bridge.DUPLEX_GUEST_VERBS and callable(assert_windows_current)) or
                     (request.verb not in bridge.DUPLEX_GUEST_VERBS and assert_windows_current is None),
                     'guest_ssh_live_windows_required')
        envelope = {'version': bridge.VERSION, 'binding': self.binding.fields(), 'node': request.node,
                    'boot_sha256': self.binding.boot(request.node), 'verb': request.verb,
                    'sequence': request.sequence, 'nonce': request.nonce}
        payload = bridge._bytes(envelope | {'args': args})
        frame_limit = (bridge.GENERATION_HANDOFF_FRAME_BYTES
                       if request.verb == bridge.GUEST_PUBLISH_GENERATION else bridge.FRAME_BYTES)
        bridge._need(len(payload) <= frame_limit and type(deadline) in (int, float), 'guest_ssh_request_unbound')
        limit = min(deadline, time.monotonic() + bridge.VERBS[request.verb][1])
        bridge._remaining(limit)
        with self.lock:
            bridge._need(not self.inflight and self.last_sequence < request.sequence and request.nonce not in self.nonces,
                         'guest_ssh_replay_rejected')
            self.last_sequence = request.sequence
            self.nonces.add(request.nonce)
            self.inflight = True
        try:
            result = self._invoke(request.node, payload, limit, request, assert_windows_current)
            bridge._remaining(limit)
            return self._receipt(result, envelope)
        except (OSError, ValueError, TypeError, RecursionError, subprocess.SubprocessError):
            raise bridge.BridgeRejected('guest_ssh_dispatch_unconfirmed') from None
        finally:
            with self.lock:
                self.inflight = False

    @staticmethod
    def _receipt(result, envelope):
        bridge._need(type(result) is bytes and 0 < len(result) <= bridge.FRAME_BYTES,
                     'guest_ssh_frame_limit')
        value = json.loads(result.decode('ascii'), object_pairs_hook=bridge._pairs,
                           parse_constant=bridge._reject_constant)
        bridge._need(type(value) is dict and bridge._bytes(value) == result, 'guest_ssh_response_unbound')
        receipt = bridge.Receipt(value.get('status'), value.get('receipt_sha256'))
        bridge._need(bridge._matches(value, envelope | receipt.fields(envelope['verb'])),
                     'guest_ssh_response_unbound')
        return receipt

    def _invoke(self, node, payload, limit, request, assert_windows_current):
        guest = self.guests[node]
        stamps, records = {}, {}
        for key, endpoint in (('jump', self.jump), (node, guest)):
            stamps.update(_identity(endpoint, limit))
            known = self.known_hosts[key]
            raw, stamp = _checked_file(known, private=False, maximum=1048576)
            stamps[known] = stamp
            records[endpoint.alias] = _host_record(raw, endpoint, guest=key != 'jump')
        temporary_root = '/private/tmp' if sys.platform == 'darwin' else '/tmp'
        with tempfile.TemporaryDirectory(prefix='ha-native-ssh-', dir=temporary_root) as directory:
            config = _ssh_config(Path(directory), self.jump, guest, records)
            bridge._remaining(limit)
            bridge._need(_same_files(stamps), 'guest_ssh_local_file_changed')
            command = (SSH, '-F', str(config), '-T', guest.alias, self.command)
            if assert_windows_current is None:
                try:
                    output = self.run(command, payload, limit)
                except bridge.BridgeRejected as error:
                    if request.verb != 'restore' or str(error) != 'guest_ssh_exit_unconfirmed':
                        raise
                    bridge._need(_same_files(stamps), 'guest_ssh_local_file_changed')
                    readback_verb = bridge.GUEST_RESTORE_READBACK
                    readback_limit = min(limit, time.monotonic() + bridge.VERBS[readback_verb][1])
                    bridge._remaining(readback_limit)
                    envelope = json.loads(payload)
                    args = envelope.pop('args')
                    readback = envelope | {'verb': readback_verb}
                    result = self.run(command, bridge._bytes(readback | {'args': args}), readback_limit)
                    receipt = self._receipt(result, readback)
                    bridge._need(receipt.status == 'PASS', 'guest_ssh_restore_readback_unconfirmed')
                    output = bridge._bytes(envelope | receipt.fields('restore'))
            else:
                preparation = request.verb == bridge.GUEST_PREPARE_GENERATION
                boundaries = ACTIVATION_BOUNDARIES
                if preparation:
                    boundaries = bridge.PREPARATION_BOUNDARIES
                elif request.verb in {bridge.GUEST_QUIESCE_INITIAL_POSTBOOT,bridge.GUEST_QUIESCE_POSTBOOT_UNPUBLISHED}:
                    boundaries = bridge.POSTBOOT_STOP_BOUNDARIES
                elif request.verb == bridge.GUEST_QUIESCE_INITIAL_RECOVERY:
                    boundaries = bridge.QUIESCENCE_BOUNDARIES
                elif request.verb in {bridge.GUEST_START_INITIAL_RECOVERY, bridge.GUEST_START_INITIAL_REARM, bridge.GUEST_START_INITIAL_POSTBOOT, bridge.GUEST_QUIESCE_INITIAL_POSTBOOT, bridge.GUEST_START_INITIAL_UNPUBLISHED, bridge.GUEST_QUIESCE_POSTBOOT_UNPUBLISHED}:
                    boundaries = bridge.RECOVERY_START_BOUNDARIES
                output = _activation_exchange(
                    command, payload, limit, request.nonce, assert_windows_current,
                    bind_boundary=request.verb != "activate",
                    boundaries=boundaries,
                    boundary_seconds=bridge.PREPARATION_BOUNDARY_SECONDS if preparation else bridge.ACTIVATION_BOUNDARY_SECONDS,
                )
            bridge._need(_same_files(stamps), 'guest_ssh_local_file_changed')
        return output


class RecoveryGuestProofReader:
    """Renew read-only guest observations without resetting any mutating dispatch session."""

    def __init__(self, binding, args, hold_sha256, *, jump, guests, known_hosts, launcher_sha256):
        error = "guest_ssh_recovery_proof_unconfirmed"
        bridge._need(type(args) is dict and bridge._hash(hold_sha256), error)
        self.args = args.copy()
        bridge._args(bridge.GUEST_OBSERVE_INITIAL_RECOVERY_HOLD, "A", self.args)
        self.binding = binding
        self.hold_sha256 = hold_sha256
        self.settings = dict(jump=jump, guests=dict(guests), known_hosts=dict(known_hosts),
                             launcher_sha256=launcher_sha256)
        self.dispatcher = GuestSshDispatcher(binding, **self.settings)
        self.sequence = 0
        self.window = 0
        self.recent = deque(maxlen=1024)
        self.lock = threading.Lock()

    def read(self, purpose, challenge_sha256, deadline):
        error = "guest_ssh_recovery_proof_unconfirmed"
        bridge._need(purpose in ("held_admission", "provider_isolation") and bridge._hash(challenge_sha256)
                     and type(deadline) in (int, float) and self.lock.acquire(blocking=False), error)
        try:
            bridge._remaining(deadline)
            identity = (purpose, challenge_sha256)
            bridge._need(identity not in self.recent, error)
            self.recent.append(identity)
            if self.sequence == bridge.MAX_REQUESTS:
                self.dispatcher = GuestSshDispatcher(self.binding, **self.settings)
                self.sequence = 0
                self.window += 1
            self.sequence += 1
            challenge = bridge._digest(bridge._bytes({
                "purpose": purpose, "challenge_sha256": challenge_sha256, "window": self.window,
                "binding": self.binding.fields(), "hold_sha256": self.hold_sha256,
            }))
            if purpose == "held_admission":
                return self.dispatcher.verify_initial_recovery_hold(
                    self.args, self.sequence, challenge, self.hold_sha256, deadline)
            request = bridge.Request(self.binding, "A", bridge.GUEST_OBSERVE_CAR_ISOLATION, self.sequence,
                                     challenge, self.args | {"hold_sha256": self.hold_sha256})
            return self.dispatcher.observe_car_isolation(request, deadline)
        finally:
            self.lock.release()


def _activation_exchange(command, payload, deadline, request_nonce, assert_windows_current, *, bind_boundary=False,
                         boundaries=ACTIVATION_BOUNDARIES, boundary_seconds=bridge.ACTIVATION_BOUNDARY_SECONDS):
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}, start_new_session=True)
    complete = False
    try:
        channel = bridge.Stdio(process.stdout.fileno(), process.stdin.fileno())
        channel.write(json.loads(payload), deadline)
        challenges = set()
        while True:
            frame = channel.read(deadline)
            if frame.get('kind') != 'activation_boundary':
                bridge._need(len(challenges) == len(boundaries) or frame.get('status') == 'UNKNOWN',
                             'guest_ssh_live_windows_required')
                bridge._need(not channel.buffer and process.wait(timeout=bridge._remaining(deadline)) == 0,
                             'guest_ssh_exit_unconfirmed')
                bridge._need(not os.read(process.stdout.fileno(), 1), 'guest_ssh_frame_limit')
                complete = True
                return bridge._bytes(frame)
            current_deadline = min(deadline, time.monotonic() + boundary_seconds)
            challenge, boundary = frame.get('challenge'), frame.get('boundary')
            fields = {'request_nonce': request_nonce, 'challenge': challenge, 'boundary': boundary}
            bridge._need(bridge._matches(frame, {'kind': 'activation_boundary', **fields})
                         and bridge._hash(challenge) and challenge not in challenges
                         and len(challenges) < len(boundaries)
                         and boundary == boundaries[len(challenges)],
                         'guest_ssh_activation_boundary_unbound')
            challenges.add(challenge)
            proof = (assert_windows_current(challenge, boundary, current_deadline) if bind_boundary
                     else assert_windows_current(current_deadline))
            bridge._need(bridge._hash(proof), 'guest_ssh_live_windows_unconfirmed')
            channel.write({'kind': 'activation_boundary_current', 'windows_proof_sha256': proof, **fields}, current_deadline)
    finally:
        if not complete:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
        process.stdin.close()
        process.stdout.close()
