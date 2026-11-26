"""Explicit one-shot B crash recovery using the existing native transition.

This is an execution trigger, not a failure detector. A timeout never proves
fencing. The exact old A VM must already be Off with automatic start disabled;
the pinned Windows reader verifies that fact and the armed host boot afresh.
The existing transition still owns promotion, rejoin, authority, eight starts,
readiness and ingress. No A SSH, synthetic proof or automatic redispatch exists.
"""

import base64
from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import inspect
import json
import os
from pathlib import Path
import secrets
import shlex
import stat
import sys
import time
from types import ModuleType
import uuid

from . import native_stdio_bridge as wire, transition_ingress as transition
from .caddy_runtime import ActiveCaddy, CaddyRuntime
from .car_callback_runtime import CallbackRuntime, Relay
from .controller import TransitionBlocked, exclusive
from .native_guest_activation import ServingGeneration
from . import native_guest_ssh as guest_ssh
from .native_guest_ssh import GuestSshDispatcher, SshEndpoint
from .native_replacement_network import WindowsReplacementNetwork
from .native_windows_activation import WindowsTransitionServiceStart
from .native_windows_ssh import WindowsSsh, fixed_script
from .transition_execution import TransitionExecution

ERROR = 'b_recovery_dispatch_unconfirmed'
UNKNOWN = 'b_recovery_dispatch_requires_reconciliation'
FILES = {'lock', 'armed.json', 'fenced.json', 'issued.json', 'observed.json'}
REQUIRED_SWITCH = frozenset(name for name, parameter in inspect.signature(transition.switch_with_observer).parameters.items()
                            if parameter.default is inspect.Parameter.empty)
CORE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class ReviewedBRecoverySite:
    """Independently pinned caller source/config and disabled VM boot-task XML.

    External callbacks must be plain functions from driver_path, use the exact
    module global B_RECOVERY_SITE_CONFIG, and retain serializable closures.
    Site bound receivers/hidden state fail closed. config_raw is memory-only.
    """
    driver_path: Path
    driver_sha256: str
    config_raw: bytes
    config_sha256: str
    boot_task_xml_sha256: str


def _need(value, reason=ERROR):
    if not value:
        raise TransitionBlocked(reason)


def _bytes(value):
    return wire._bytes(value)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _guid(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value and uuid.UUID(value).int != 0
    except ValueError:
        return False


def _code(code):
    def constant(value):
        if inspect.iscode(value): return _code(value)
        if type(value) is frozenset:
            return {'frozenset': sorted((_hashable(v) for v in value), key=lambda v: _bytes(v))}
        return _hashable(value)
    return {'bytecode': code.co_code.hex(), 'constants': [constant(v) for v in code.co_consts],
            'names': code.co_names, 'variables': code.co_varnames, 'freevars': code.co_freevars,
            'cellvars': code.co_cellvars, 'flags': code.co_flags, 'argcount': code.co_argcount,
            'posonly': code.co_posonlyargcount, 'kwonly': code.co_kwonlyargcount,
            'exceptiontable': code.co_exceptiontable.hex()}


def _hashable(value):
    """Hash caller preparation without persisting connection/configuration data."""
    if type(value) in (str, int, bool, float) or value is None:
        return value
    if type(value) is bytes:
        return {'bytes_sha256': _sha(value)}
    if isinstance(value, Path):
        return {'path_sha256': _sha(os.fsencode(value))}
    if type(value) is dict:
        _need(all(type(key) is str for key in value))
        return {key: _hashable(item) for key, item in value.items()}
    if type(value) in (list, tuple):
        return [_hashable(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return _hashable(asdict(value))
    if isinstance(value, ModuleType):
        name = value.__name__.split('.')[0]
        _need(name in sys.stdlib_module_names or name in {'ha_control', 'ha_runtime', 'ha_egress'})
        source = getattr(value, '__file__', None)
        return {'module': value.__name__, 'source_sha256': _sha(Path(source).read_bytes()) if source else None}
    if isinstance(value, type):
        source = inspect.getsourcefile(value) if value.__module__ != 'builtins' else None
        return {'class': value.__module__ + '.' + value.__qualname__,
                'source_sha256': _sha(Path(source).read_bytes()) if source else None}
    if callable(value):
        implementation = value if inspect.isfunction(value) or inspect.ismethod(value) else type(value).__call__
        source = inspect.getsourcefile(implementation)
        _need(source is not None)
        info = inspect.getclosurevars(implementation)
        # Native libraries are the reviewed implementation, not site config.
        # Site globals/nonlocals and every bound receiver remain explicit.
        globals_used = {} if Path(source).resolve().is_relative_to(CORE.parent) else info.globals
        receiver = getattr(value, '__self__', None) if inspect.isfunction(value) or inspect.ismethod(value) else value
        # Arbitrary site receivers can hide state in slots, properties or
        # extension code. Require plain reviewed functions with explicit
        # captured/global config instead of silently dropping that custody.
        _need(receiver is None or Path(source).resolve().is_relative_to(CORE.parent))
        state = vars(receiver) if receiver is not None else None
        return {'callable': implementation.__module__ + '.' + implementation.__qualname__,
                'source_sha256': _sha(Path(source).read_bytes()),
                'code_sha256': _sha(_bytes(_code(implementation.__code__))),
                'defaults': _hashable(implementation.__defaults__),
                'kwdefaults': _hashable(implementation.__kwdefaults__),
                'nonlocals': _hashable(info.nonlocals), 'globals': _hashable(globals_used),
                'receiver': _hashable(state)}
    _need(False)


def _transport(inputs):
    guests = inputs['guests']
    windows = inputs['attach_replacement_network'].windows
    # This adapter supports only the original pinned jump transport. The
    # newer WindowsGuestLink has separate consumed state and is not covered
    # by this ticket. Missing/added direct-link scope cannot silently select it.
    _need(type(guests) is GuestSshDispatcher and type(windows) is WindowsSsh
          and set(vars(windows)) == {'endpoint', 'known_hosts'}
          and type(windows.endpoint) is SshEndpoint and isinstance(windows.known_hosts, Path)
          and set(vars(guests)) == {'binding', 'jump', 'guests', 'known_hosts', 'direct_link',
                                   'launcher_sha256', 'command', 'run', 'lock', 'last_sequence', 'nonces', 'inflight'}
          and guests.direct_link is None and type(guests.jump) is SshEndpoint
          and type(guests.guests) is dict and set(guests.guests) == {'A', 'B'}
          and all(type(endpoint) is SshEndpoint for endpoint in guests.guests.values())
          and type(guests.known_hosts) is dict and set(guests.known_hosts) == {'jump', 'A', 'B'}
          and all(isinstance(path, Path) for path in guests.known_hosts.values())
          and wire._hash(guests.launcher_sha256))
    windows.endpoint.validate(guest=False)
    guests.jump.validate(guest=False)
    for endpoint in guests.guests.values(): endpoint.validate(guest=True)
    endpoints = (guests.jump, *guests.guests.values())
    _need(len({endpoint.alias.lower() for endpoint in endpoints}) == 3
          and len({(endpoint.host.lower(), endpoint.port) for endpoint in endpoints}) == 3
          and len({endpoint.host_fingerprint for endpoint in endpoints}) == 3)
    canonical = shlex.join(('/usr/bin/python3', '-I', '-B', '-S', '-c', guest_ssh.BOOTSTRAP,
                            guest_ssh.WORKER, guests.launcher_sha256))
    _need(type(guests.command) is str and guests.command == canonical)
    return _sha(_bytes(_hashable({'windows': windows.endpoint, 'windows_known_hosts': windows.known_hosts,
        'jump': guests.jump, 'guests': guests.guests, 'known_hosts': guests.known_hosts,
        'command': guests.command, 'direct_link': None,
        'launcher_sha256': guests.launcher_sha256, 'guest_transport': guests.run})))


def _site_custody(inputs, site):
    _need(type(site) is ReviewedBRecoverySite and type(site.config_raw) is bytes
          and 0 < len(site.config_raw) <= 65536 and _sha(site.config_raw) == site.config_sha256
          and wire._hash(site.driver_sha256) and wire._hash(site.boot_task_xml_sha256))
    path = Path(site.driver_path)
    _need(path.is_absolute() and path.resolve(strict=True) == path)
    source_raw = path.read_bytes()
    _need(_sha(source_raw) == site.driver_sha256)
    config = json.loads(site.config_raw, object_pairs_hook=wire._pairs, parse_constant=wire._reject_constant)
    _need(type(config) is dict)
    callbacks = [inputs['assert_legacy_closed'], inputs['generation_handoff_pins'], inputs.get('verify_crash_source'),
                 inputs['guests'].run, inputs['main'].run, inputs['car'].docker,
                 *inputs['stage_service'].values(), inputs.get('verify_public')]
    for callback in callbacks:
        if not callable(callback): continue
        implementation = callback if inspect.isfunction(callback) or inspect.ismethod(callback) else type(callback).__call__
        source = inspect.getsourcefile(implementation)
        _need(source is not None)
        if Path(source).resolve().is_relative_to(CORE.parent): continue
        _need(Path(source).resolve() == path)
        used = inspect.getclosurevars(implementation)
        _need('B_RECOVERY_SITE_CONFIG' in used.globals
              and _bytes(used.globals['B_RECOVERY_SITE_CONFIG']) == _bytes(config))
        # A forged co_filename is insufficient: the code must be precisely a
        # function present in the independently pinned source, including its
        # nested code objects. Captured values are separately armed above.
        compiled = compile(source_raw, implementation.__code__.co_filename, 'exec', dont_inherit=True)
        pending = [compiled];matching = []
        while pending:
            code = pending.pop()
            if code.co_qualname == implementation.__code__.co_qualname: matching.append(code)
            pending.extend(c for c in code.co_consts if inspect.iscode(c))
        _need(len(matching) == 1 and matching[0] == implementation.__code__)
    return {'driver_sha256': site.driver_sha256, 'config_sha256': site.config_sha256,
            'boot_task_xml_sha256': site.boot_task_xml_sha256}


def _describe(inputs, boot, site, journal):
    _need(type(inputs) is dict and wire._hash(boot))
    optional = {'resume_rejoin', 'prepare_guest_runtime', 'verify_crash_source', 'verify_public'}
    _need(REQUIRED_SWITCH <= inputs.keys() <= REQUIRED_SWITCH | optional
          and inputs.get('resume_rejoin') is None and inputs.get('prepare_guest_runtime') is True)
    custody = _site_custody(inputs, site)
    directory = Path(journal)
    _need(directory.is_absolute() and directory.resolve(strict=True) == directory)
    generation, execution = inputs['generation'], inputs['execution']
    guests, request = inputs['guests'], inputs['target_request']
    network = inputs['attach_replacement_network']
    _need(type(guests) is GuestSshDispatcher and type(network) is WindowsReplacementNetwork
          and type(network.windows) is WindowsSsh)
    transport_pin = _transport(inputs)
    _need(type(generation) is ServingGeneration and type(execution) is TransitionExecution
          and type(guests) is GuestSshDispatcher and type(request) is wire.Request
          and type(network) is WindowsReplacementNetwork and type(network.windows) is WindowsSsh
          and type(inputs['main']) is CaddyRuntime and type(inputs['car']) is CallbackRuntime
          and type(inputs['previous']) is ActiveCaddy and type(inputs['relay']) is Relay
          and inputs['previous'].primary_ip == inputs['relay'].guest == guests.guests['A'].host
          and inputs['main'].project == execution.spec['project'])
    generation.validate()
    _need(generation.primary == 'B' and generation.epoch > 1 and generation.timeline > 1
          and request.node == 'B' and request.verb == wire.GUEST_TRANSITION_TARGET
          and request.binding == guests.binding and request.sequence <= wire.MAX_REQUESTS - 15
          and type(request.sequence) is int and request.sequence > 0 and wire._hash(request.nonce)
          and request.binding.plan_sha256 == execution.spec['plan_sha256']
          and request.binding.attempt_id == execution.spec['attempt_id']
          and inputs['target'] == guests.guests['B'].host)
    wire._args(request.verb, request.node, request.args)
    observer, controller = inputs['observer_setup'], inputs['controller_setup']
    _need(type(observer) is dict and type(controller) is dict
          and observer['windows'] is network.windows
          and type(controller['starter']) is WindowsTransitionServiceStart
          and controller['starter'].windows is network.windows)
    source_id, target_id = controller['old_vm_id'], network.pins['PrimaryVmId']
    replacement = inputs['replacement_vm']
    _need(_guid(source_id) and _guid(target_id) and source_id != target_id
          and type(replacement) is dict and replacement.get('node') == 'A'
          and _guid(replacement.get('vm_id')) and replacement['vm_id'] not in {source_id, target_id}
          and wire._hash(network.pins['OldDiskPathsSha256']) and wire._hash(network.script_sha256))
    raw = observer['plan_raw']
    _need(type(raw) is bytes and 0 < len(raw) <= 1048576 and _sha(raw) == observer['plan_sha256'])
    template = json.loads(raw, object_pairs_hook=wire._pairs, parse_constant=wire._reject_constant)
    _need(type(template) is dict and template.get('node') == 'A'
          and template.get('generation') == generation.epoch
          and template.get('source_lease_close_required') is False
          and wire._hash(template.get('crash_source_sha256'))
          and template.get('windows_enforced', {}).get('vm_id') == source_id
          and callable(inputs.get('verify_crash_source')))
    guests.assert_serving_endpoints(template.get('pg_nodes'))
    digest_inputs = {key: value for key, value in inputs.items() if key not in {
        'execution', 'main', 'car', 'previous', 'relay', 'generation', 'guests', 'target_request',
        'attach_replacement_network', 'observer_setup', 'controller_setup'}}
    observer_pins = {key: value for key, value in observer.items() if key not in {'windows', 'deadline'}}
    controller_pins = {key: value for key, value in controller.items() if key not in {'starter', 'deadline'}}
    # The native engine independently validates the previous ingress and all
    # caller callbacks; their source hashes do not replace any live proof.
    preparation = {'inputs': _hashable(digest_inputs), 'observer': _hashable(observer_pins),
                   'controller': _hashable(controller_pins), 'network': _hashable(network.pins),
                   'previous': _hashable(inputs['previous']), 'relay': _hashable(inputs['relay']),
                   'main': _hashable({'root': inputs['main'].root, 'project': inputs['main'].project,
                                      'run': inputs['main'].run}),
                   'car': _hashable(inputs['car'].docker),
                   'execution': _hashable(execution.spec), 'starter_sha256': controller['starter'].start_sha256,
                   'target_request': {'binding': request.binding.fields(), 'node': request.node,
                       'verb': request.verb, 'sequence': request.sequence, 'nonce': request.nonce, 'args': request.args}}
    result = {'schema_version': 1, 'action': 'execute-b-crash-recovery-v1',
        'windows_boot_sha256': boot, 'generation': asdict(generation),
        'plan_sha256': execution.spec['plan_sha256'], 'attempt_id': execution.spec['attempt_id'],
        'controller_template_sha256': observer['plan_sha256'], 'crash_source_sha256': template['crash_source_sha256'],
        'source_vm_id_sha256': _sha(source_id.encode()), 'target_vm_id_sha256': _sha(target_id.encode()),
        'source_disk_paths_sha256': network.pins['OldDiskPathsSha256'],
        'target_sha256': _sha(inputs['target'].encode('ascii')),
        'transport_sha256': transport_pin,
        'network_script_sha256': network.script_sha256, 'preparation_sha256': _sha(_bytes(preparation))}
    result.update(reviewed_site=custody, journal_sha256=_sha(os.fsencode(directory)))
    return result, template


def _unissued(inputs):
    _need(inputs['main'].execution is None and inputs['car'].execution is None
          and inputs['main'].attempted is False and inputs['controller_setup']['starter'].attempted is False,
          UNKNOWN)


def arm_b_recovery(transition_inputs, *, windows_boot_sha256, reviewed_site, journal):
    """Return hash-only armed bytes. No remote operation or local write occurs.

    Caller pins these exact bytes in its reviewed durable site configuration.
    transition_inputs are the complete kwargs for switch_with_observer, using
    real native network/SSH/execution/service objects and a crash-mode plan.
    This does not manufacture a missing crash-source or approve a new plan.
    """
    value, _ = _describe(transition_inputs, windows_boot_sha256, reviewed_site, journal)
    _unissued(transition_inputs)
    return _bytes(value)


class BRecoveryDispatcher:
    """Execute the exact armed native transition once; observe never executes it.

    journal is a separate existing owner-only 0700 directory. Existing issued
    records always prohibit dispatch, including after host restart or unknown
    response. Partial native operations must be reconciled with the existing
    transition engine; this wrapper does not replay the broad switch function.
    """

    def __init__(self, *, armed_raw, armed_sha256, transition_inputs, journal, reviewed_site):
        _need(type(armed_raw) is bytes and 0 < len(armed_raw) <= 8192
              and _sha(armed_raw) == armed_sha256)
        armed = json.loads(armed_raw, object_pairs_hook=wire._pairs, parse_constant=wire._reject_constant)
        _need(type(armed) is dict and wire._hash(armed.get('windows_boot_sha256')))
        actual, template = _describe(transition_inputs, armed['windows_boot_sha256'], reviewed_site, journal)
        _need(armed_raw == _bytes(actual))
        self.armed, self.armed_raw, self.pin = armed, armed_raw, armed_sha256
        self.inputs, self.template = dict(transition_inputs), template
        self.site = reviewed_site
        self.directory = Path(journal)
        # Caddy/ingress execution and its lock live in the POSIX coordinator;
        # native Windows operations go through the pinned WindowsSsh instance.
        _need(os.name == 'posix' and self.directory != self.inputs['execution'].directory)

    def _private(self):
        directory = self.directory
        _need(directory.is_absolute() and directory.resolve(strict=True) == directory)
        st = directory.lstat()
        _need(stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid() and stat.S_IMODE(st.st_mode) == 0o700)
        for parent in directory.parents:
            st = parent.lstat()
            sticky = st.st_uid == 0 and bool(st.st_mode & stat.S_ISVTX)
            _need(stat.S_ISDIR(st.st_mode) and st.st_uid in {0, os.geteuid()}
                  and (not st.st_mode & 0o022 or sticky))
        _need({p.name for p in directory.iterdir()} <= FILES)

    def _read(self, name):
        try:
            fd = os.open(self.directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            _need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                  and before.st_uid == os.geteuid() and stat.S_IMODE(before.st_mode) == 0o600)
            raw = stream.read(16385)
            after = os.fstat(stream.fileno())
            _need(0 < len(raw) == before.st_size <= 16384 and all(getattr(before, key) == getattr(after, key)
                for key in ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_uid', 'st_gid',
                            'st_size', 'st_mtime_ns', 'st_ctime_ns')))
            return raw

    def _decode(self, raw, fields):
        value = json.loads(raw, object_pairs_hook=wire._pairs, parse_constant=wire._reject_constant)
        _need(type(value) is dict and set(value) == fields and _bytes(value) == raw)
        return value

    def _check_fence(self, value):
        fields = {'armed_sha256', 'challenge_sha256', 'windows_boot_sha256', 'source_vm_id_sha256',
                  'target_vm_id_sha256', 'disk_paths_sha256', 'source_off', 'automatic_start_disabled',
                  'target_running', 'native_observation_sha256'}
        _need(type(value) is dict and set(value) == fields and value['armed_sha256'] == self.pin
              and value['windows_boot_sha256'] == self.armed['windows_boot_sha256']
              and value['source_vm_id_sha256'] == self.armed['source_vm_id_sha256']
              and value['target_vm_id_sha256'] == self.armed['target_vm_id_sha256']
              and value['disk_paths_sha256'] == self.armed['source_disk_paths_sha256']
              and all(value[k] is True for k in ('source_off', 'automatic_start_disabled', 'target_running'))
              and all(wire._hash(value[k]) for k in ('challenge_sha256', 'native_observation_sha256')))
        return value

    def _write(self, name, raw):
        _need(type(raw) is bytes and 0 < len(raw) <= 16384)
        fd = os.open(self.directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(raw);out.flush();os.fsync(out.fileno())
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(fd)
        finally: os.close(fd)

    def _bound(self):
        # A completed switch changes the starter's one-shot attempted flag.
        # Check the immutable preparation before dispatch; readback uses the
        # exact stored armed record and never re-arms the controller.
        actual, _ = _describe(self.inputs, self.armed['windows_boot_sha256'], self.site, self.directory)
        _need(_bytes(actual) == self.armed_raw)
        _unissued(self.inputs)

    def _fence(self, deadline):
        wire._remaining(deadline)
        network = self.inputs['attach_replacement_network']
        _need(type(network) is WindowsReplacementNetwork and type(network.windows) is WindowsSsh
              and self.site.boot_task_xml_sha256 == self.armed['reviewed_site']['boot_task_xml_sha256']
              and _sha(self.inputs['controller_setup']['old_vm_id'].encode()) == self.armed['source_vm_id_sha256']
              and _sha(network.pins['PrimaryVmId'].encode()) == self.armed['target_vm_id_sha256']
              and network.pins['OldDiskPathsSha256'] == self.armed['source_disk_paths_sha256']
              and network.script_sha256 == self.armed['network_script_sha256']
              and _transport(self.inputs) == self.armed['transport_sha256'])
        nonce = secrets.token_hex(32)
        cfg = {'nonce': nonce, 'boot': self.armed['windows_boot_sha256'],
               'target_vm_id': network.pins['PrimaryVmId'],
               'old_vm_id': self.inputs['controller_setup']['old_vm_id'],
               'disks': self.armed['source_disk_paths_sha256'],
               'boot_task_xml_sha256': self.site.boot_task_xml_sha256}
        native = fixed_script('windows-ha-replacement-network.ps1', network.script_sha256,
            {'ReadCrashSource': True, 'OldVmId': cfg['old_vm_id'], 'OldDiskPathsSha256': cfg['disks']})
        blob = base64.b64encode(_bytes(cfg)).decode()
        source = _FENCE.replace('CONFIG_BASE64', blob).replace('NATIVE_SOURCE_BASE64',
            base64.b64encode(native.encode()).decode())
        facts = network.windows.powershell(source, deadline)
        expected = {'challenge': nonce, 'windows_boot_sha256': cfg['boot'], 'vm_id': cfg['old_vm_id'],
                    'disk_paths_sha256': cfg['disks'], 'state': 'Off', 'automatic_start': 'Nothing',
                    'target_vm_id': cfg['target_vm_id'], 'target_state': 'Running',
                    'boot_task_disabled': True, 'boot_task_instances': 0,
                    'boot_task_xml_sha256': cfg['boot_task_xml_sha256']}
        _need(wire._matches(facts, expected))
        wire._remaining(deadline)
        return {'armed_sha256': self.pin, 'challenge_sha256': _sha(nonce.encode()),
                'windows_boot_sha256': cfg['boot'], 'source_vm_id_sha256': _sha(cfg['old_vm_id'].encode()),
                'target_vm_id_sha256': _sha(cfg['target_vm_id'].encode()),
                'disk_paths_sha256': cfg['disks'], 'source_off': True, 'automatic_start_disabled': True,
                'target_running': True, 'native_observation_sha256': _sha(_bytes(facts))}

    def _crash_record(self, deadline):
        i = self.inputs
        scope = {'attempt_id': self.armed['attempt_id'], 'legacy_plan': i['legacy_plan'],
                 'legacy_attempt': i['legacy_attempt'], 'previous_runtime_sha256': i['previous_runtime_sha256'],
                 'source_vm_id': i['controller_setup']['old_vm_id'], 'source_node': 'A',
                 'next_generation': self.armed['generation']['epoch'], 'next_timeline': self.armed['generation']['timeline']}
        pin = i['execution'].verify_crash_source_record(scope, self.template, deadline)
        _need(pin == self.armed['crash_source_sha256'])

    def dispatch(self, deadline):
        """After fresh positive fence, durably issue and run the existing B switch."""
        self._private()
        with exclusive(self.directory / 'lock'):
            _need(self._read('issued.json') is None and self._read('observed.json') is None, UNKNOWN)
            self._bound()
            old = self._read('armed.json')
            if old is None: self._write('armed.json', self.armed_raw)
            else: _need(old == self.armed_raw)
            fence = self._fence(deadline)
            self._crash_record(deadline)
            if self._read('fenced.json') is None:
                self._write('fenced.json', _bytes(fence))
            else:
                raw = self._read('fenced.json')
                prior = json.loads(raw, object_pairs_hook=wire._pairs)
                _need(_bytes(prior) == raw)
                self._check_fence(prior)
            # The second actual read follows durability I/O and precedes the
            # dispatch fence. An old saved receipt never substitutes for it.
            current = self._fence(deadline)
            self._bound()
            issued = {'schema_version': 1, 'armed_sha256': self.pin,
                      'fenced_record_sha256': _sha(self._read('fenced.json')),
                      'fresh_fence': current, 'automatic_retry_permitted': False}
            self._write('issued.json', _bytes(issued))
            inputs = dict(self.inputs)
            inputs['observer_setup'] = dict(inputs['observer_setup'], deadline=deadline)
            inputs['controller_setup'] = dict(inputs['controller_setup'], deadline=deadline)
            try:
                wire._remaining(deadline)
                result = transition.switch_with_observer(**inputs)
                _need(type(result) is transition.CompletedTransition and result.generation.primary == 'B'
                      and result.generation.epoch == self.armed['generation']['epoch']
                      and result.generation.timeline == self.armed['generation']['timeline']
                      and result.generation.revision >= self.armed['generation']['revision']
                      and wire._hash(result.activation_sha256))
                scope = result.receipt['scope']
                _need(scope['primary'] == 'B' and scope['target_sha256'] == self.armed['target_sha256']
                      and scope['attempt_id'] == self.armed['attempt_id']
                      and scope['plan_sha256'] == self.armed['plan_sha256'])
                after = self._fence(deadline)
                self._write('observed.json', _bytes({'schema_version': 1, 'armed_sha256': self.pin,
                    'issued_sha256': _sha(_bytes(issued)), 'generation': asdict(result.generation),
                    'activation_sha256': result.activation_sha256,
                    'transition_receipt_sha256': _sha(_bytes(result.receipt)),
                    'controller_sha256': result.controller['plan_sha256'], 'fresh_fence': after}))
                return result
            except Exception:
                raise TransitionBlocked(UNKNOWN) from None

    def observe(self, deadline):
        """Read status and actual fence only; never calls switch or fills receipts."""
        self._private()
        with exclusive(self.directory / 'lock'):
            armed = self._read('armed.json')
            _need(armed is None or armed == self.armed_raw)
            fence = self._fence(deadline)
            issued, observed = self._read('issued.json'), self._read('observed.json')
            _need(observed is None or issued is not None)
            state = 'NOT_ISSUED' if issued is None else 'RECONCILIATION_REQUIRED'
            if issued is not None:
                record = self._decode(issued, {'schema_version', 'armed_sha256', 'fenced_record_sha256',
                                              'fresh_fence', 'automatic_retry_permitted'})
                fenced = self._read('fenced.json')
                _need(fenced is not None and record['schema_version'] == 1
                      and type(record['schema_version']) is int and record['armed_sha256'] == self.pin
                      and record['automatic_retry_permitted'] is False
                      and record['fenced_record_sha256'] == _sha(fenced))
                self._check_fence(record['fresh_fence'])
                self._check_fence(json.loads(fenced, object_pairs_hook=wire._pairs))
            if observed is not None:
                done = self._decode(observed, {'schema_version', 'armed_sha256', 'issued_sha256', 'generation',
                    'activation_sha256', 'transition_receipt_sha256', 'controller_sha256', 'fresh_fence'})
                generation = ServingGeneration(**done['generation'])
                generation.validate()
                _need(type(done['schema_version']) is int and done['schema_version'] == 1
                      and done['armed_sha256'] == self.pin and done['issued_sha256'] == _sha(issued)
                      and generation.primary == 'B' and generation.epoch == self.armed['generation']['epoch']
                      and generation.timeline == self.armed['generation']['timeline']
                      and generation.revision >= self.armed['generation']['revision']
                      and all(wire._hash(done[k]) for k in
                              ('activation_sha256', 'transition_receipt_sha256', 'controller_sha256')))
                self._check_fence(done['fresh_fence'])
                state = 'COMPLETED_RECEIPT_PRESENT'
            return {'status': state, 'armed_sha256': self.pin, 'fresh_fence_sha256': _sha(_bytes(fence)),
                    'issued_sha256': _sha(issued) if issued else None,
                    'observed_sha256': _sha(observed) if observed else None}


_FENCE = r'''
$ErrorActionPreference='Stop';Set-StrictMode -Version Latest
$enc=[Text.UTF8Encoding]::new($false,$true)
$cfg=ConvertFrom-Json ($enc.GetString([Convert]::FromBase64String('CONFIG_BASE64')))
function Boot {
 $ticks=(Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 5).LastBootUpTime.ToUniversalTime().Ticks.ToString()
 $h=[Security.Cryptography.SHA256]::Create()
 try{return [BitConverter]::ToString($h.ComputeHash($enc.GetBytes($ticks))).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}
}
if((Boot)-cne $cfg.boot){throw 'b_recovery_boot'}
$native=$enc.GetString([Convert]::FromBase64String('NATIVE_SOURCE_BASE64'))
$rows=@(& ([scriptblock]::Create($native)))
if($rows.Count-ne 1){throw 'b_recovery_fence'}
$v=ConvertFrom-Json $rows[0]
if($v.vm_id-cne $cfg.old_vm_id -or $v.disk_paths_sha256-cne $cfg.disks -or $v.state-cne 'Off' -or
   $v.automatic_start-cne 'Nothing'){throw 'b_recovery_fence'}
$target=Get-VM -Id ([guid]$cfg.target_vm_id)
if($target.Id.ToString().ToLowerInvariant()-cne $cfg.target_vm_id -or $target.State-cne 'Running' -or
   (Boot)-cne $cfg.boot){throw 'b_recovery_target'}
$task=Get-ScheduledTask -TaskPath '\' -TaskName 'N100HA-Start-VM-Guests' -ErrorAction Stop
if($task.State-cne 'Disabled' -or $task.Settings.Enabled -isnot [bool] -or $task.Settings.Enabled){throw 'b_recovery_boot_task'}
$xml=[string](Export-ScheduledTask -TaskPath '\' -TaskName 'N100HA-Start-VM-Guests' -ErrorAction Stop)
$h=[Security.Cryptography.SHA256]::Create()
try{$xmlPin=[BitConverter]::ToString($h.ComputeHash($enc.GetBytes($xml))).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}
if($xmlPin-cne $cfg.boot_task_xml_sha256){throw 'b_recovery_boot_task'}
$scheduler=New-Object -ComObject 'Schedule.Service';$scheduler.Connect()
$instances=@($scheduler.GetFolder('\').GetTask('N100HA-Start-VM-Guests').GetInstances(0))
if($instances.Count-ne 0 -or (Boot)-cne $cfg.boot){throw 'b_recovery_boot_task_running'}
@{challenge=$cfg.nonce;windows_boot_sha256=$cfg.boot;vm_id=$v.vm_id;disk_paths_sha256=$v.disk_paths_sha256;
  state=$v.state;automatic_start=$v.automatic_start;target_vm_id=$cfg.target_vm_id;target_state='Running';
  boot_task_disabled=$true;boot_task_instances=0;boot_task_xml_sha256=$xmlPin}|ConvertTo-Json -Compress
'''
