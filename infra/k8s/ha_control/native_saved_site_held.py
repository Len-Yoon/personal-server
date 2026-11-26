"""One-shot same-boot held observer installation and read-only reconciliation.

The installed native HMAC server is reused unchanged. Before returning its
endpoint, the host exercises the pinned native Hyper-V callback with a fresh
challenge while the guest independently checks the carryover and exact process
ownership. This callback probe is not a service-token named-pipe HMAC test.
No application, database, existing observer, key or authority is changed.
"""

import base64
from copy import deepcopy
import gzip
import json
from pathlib import Path, PureWindowsPath
import re
import secrets
import shlex
import sys
import tempfile
import threading
import time

from . import native_guest_ssh as ssh, native_stdio_bridge as wire
from .controller import TransitionBlocked
from .native_sameboot_release import WindowsPairClosure
from .native_saved_observers import NATIVE_SOURCE as OBSERVER_SOURCE, _json, _path, _sha
from .native_saved_site_apps import MARKER, SavedSiteApps, _scope_request
from .native_saved_site_provider_transport import ProviderArchiveTransport
from .sameboot_transition import _encoded, _fallback_attempt, _scope, _validate

ERROR = 'saved_site_held_unconfirmed'
BOUNDARIES = {'apply': ('before', 'dispatch', 'after'), 'observe': ('after',)}


def _need(value):
    if not value:
        raise TransitionBlocked(ERROR)


def prepare_held(spec, *, boot, nonce, attempt, stopped, carryover, port, mapping_label):
    """Derive one new endpoint from the exact current descriptor, without fake boots."""
    _need(type(spec) is dict and set(spec) == {'descriptor_path', 'descriptor_raw',
          'descriptor_sha256', 'callback_raw', 'callback_sha256'}
          and all(wire._hash(v) for v in (boot, nonce, attempt, stopped, carryover))
          and type(port) is int and 0 < port < 1024
          and type(mapping_label) is str and re.fullmatch(r'[A-Za-z0-9_. :+-]{1,160}', mapping_label))
    for prefix in ('descriptor', 'callback'):
        _need(type(spec[prefix + '_raw']) is bytes
              and _sha(spec[prefix + '_raw']) == spec[prefix + '_sha256'])
    descriptor, callback = _json(spec['descriptor_raw']), _json(spec['callback_raw'])
    _need(set(descriptor) in ({'server_path', 'server_sha256', 'arguments'},
                              {'server_path', 'server_sha256', 'arguments', 'instance_sha256'}))
    args = deepcopy(descriptor['arguments'])
    root = _path(spec['descriptor_path']).parent
    _need(type(args) is dict and type(args.get('Generation')) is int and args['Generation'] == 1
          and args.get('WindowsBootSha256') == boot and args.get('CallbackConfigSha256') == spec['callback_sha256']
          and callback.get('windows_boot_sha256') == boot and callback.get('purpose') == 'held_admission'
          and type(callback.get('guest_vsock_port')) is int and callback['guest_vsock_port'] != port
          and _path(descriptor['server_path']).parent == root and wire._hash(descriptor['server_sha256']))
    for field in ('CallbackPath', 'CallbackConfigPath', 'KeyPath', 'ReadyPath', 'RecoveryProtocolPath'):
        _need(_path(args[field]).parent == root)
    for field in ('CallbackSha256', 'KeySha256', 'PlanSha256', 'AttemptId', 'RecoveryProtocolSha256'):
        _need(wire._hash(args[field]))
    # Only the admitted scope and new dedicated port differ. Existing key,
    # protocol, native server, principal and predecessor transport remain exact.
    callback.update(guest_vsock_port=port, postboot_attempt_id=attempt,
                    postboot_stopped_receipt_sha256=stopped,
                    postboot_carryover_receipt_sha256=carryover)
    identity = _sha(_encoded({'domain': 'n100ha-sameboot-held-v1', 'operation_nonce': nonce,
                              'boot': boot, 'attempt': attempt, 'carryover': carryover}))[:32]
    suffix = bytes(9 if bit == '1' else 32 for bit in f'{int(identity, 16):0128b}')
    callback_raw = _encoded(callback) + b'\n' + suffix + b'\n'
    config_pin = _sha(callback_raw)
    config_path = str(root / ('observer-sameboot-held-config-' + config_pin + '.json'))
    input_path = str(root / ('observer-sameboot-held-task-' + config_pin + '.json'))
    args.update(CallbackConfigPath=config_path, CallbackConfigSha256=config_pin,
                PipeName='n100ha-recovery-proof-' + identity,
                ReadyPath=str(root / ('lease-observer-recovery-ready-' + config_pin + '.json')))
    descriptor = descriptor | {'instance_sha256': config_pin, 'arguments': args}
    input_raw = _encoded(descriptor) + b'\n'
    _need(len(callback_raw) <= 8192 and len(input_raw) <= 8192)
    return {'role': 'held_admission', 'descriptor': descriptor,
            'source_input_path': spec['descriptor_path'], 'source_input_sha256': spec['descriptor_sha256'],
            'source_callback_path': _json(spec['descriptor_raw'])['arguments']['CallbackConfigPath'],
            'source_callback_sha256': spec['callback_sha256'], 'config_path': config_path,
            'config_sha256': config_pin, 'config_base64': base64.b64encode(callback_raw).decode(),
            'input_path': input_path, 'input_sha256': _sha(input_raw),
            'input_base64': base64.b64encode(input_raw).decode(),
            'task_name': 'N100HA_lease_observer_recovery_' + config_pin,
            'mapping': {'id': f'{port:08x}-facb-11e6-bd58-64006a7986d3', 'label': mapping_label}}


def _windows_source():
    # Reuse native custody/CreateNew volatile mapping/Task/PID/ready checks.
    # The saved-reboot Guard requires a different boot, so replace only that
    # function with the real same-boot closed-pair command, not a fabricated boot.
    start = 'function Guard {'
    end = 'function NewPrivate('
    body = "try{\n Need ($PSVersionTable.PSEdition"
    _need(OBSERVER_SOURCE.count(start) == OBSERVER_SOURCE.count(end) == OBSERVER_SOURCE.count(body) == 1)
    prefix = OBSERVER_SOURCE[:OBSERVER_SOURCE.index(start)]
    helpers = OBSERVER_SOURCE[OBSERVER_SOURCE.index(end):OBSERVER_SOURCE.index(body)]
    registry_start = OBSERVER_SOURCE.index(" Add-Type -TypeDefinition @'", OBSERVER_SOURCE.index(body))
    registry_end = OBSERVER_SOURCE.index(" $starter=File", registry_start)
    return prefix + _WINDOWS_GUARD + helpers + OBSERVER_SOURCE[registry_start:registry_end] + _WINDOWS_BODY


class SavedSiteHeldAdmission:
    """Concrete guest_site.observe_held_carryover hook for connect_rollback_held.

    applications supplies exact durable issued-operation custody; transport
    supplies the already authenticated guest package, rollback archive and real
    closed-pair query. spec contains exact installed descriptor/config bytes.
    No secret/key bytes are accepted. source_path must be the staged native
    guest observer wrapper; callback_source is the pinned Windows native callback.

    apply is one-shot at host, guest and Windows boundaries. On uncertainty use
    observe only; it never fills missing metadata or restarts either component.
    """

    def __init__(self, *, applications, archive_transport, scope, pair, observer_spec,
                 source_path, source_sha256, manifest_sha256, port, mapping_label,
                 starter_path, starter_sha256, callback_source):
        _need(type(applications) is SavedSiteApps and type(archive_transport) is ProviderArchiveTransport
              and type(pair) is WindowsPairClosure and applications.pair is pair
              and archive_transport.dispatcher is applications.dispatcher
              and applications.manifest == manifest_sha256 and wire._hash(source_sha256)
              and wire._hash(manifest_sha256) and wire._hash(starter_sha256)
              and type(callback_source) is bytes and 0 < len(callback_source) <= 65536)
        self.scope = _scope(scope)
        opts = archive_transport.options
        _need(self.scope.get('saved_reboot') is True
              and self.scope['previous_plan_sha256'] == pair.config['plan_sha256']
              and self.scope['previous_instance_sha256'] == pair.config['binding_sha256']
              and self.scope['previous_windows_boot_sha256'] == pair.config['boot_sha256']
              and self.scope['previous_revision'] == pair.config['revision']
              and self.scope['history_sha256'] == _sha(_encoded(applications.history))
              and opts['previous_request'] == _scope_request(applications.previous)
              and opts['history'] == applications.history
              and opts['predecessor_terminal_sha256'] == applications.terminal_pin
              and opts['previous_instance_sha256'] == self.scope['previous_instance_sha256']
              and opts['windows_boot_sha256'] == self.scope['previous_windows_boot_sha256']
              and opts['previous_revision'] == self.scope['previous_revision']
              and type(port) is int and 0 < port < 1024
              and port not in (opts['provider_port'], opts['held_port']))
        source = Path(source_path)
        _need(source == Path('/var/lib/n100-ha/cutover') / ('recovery-source-' + manifest_sha256)
              / 'native-guest-recovery-observer.py')
        _path(starter_path)
        _need(_sha(Path(__file__).with_name('windows-ha-start-lease-observer.ps1').read_bytes()) == starter_sha256)
        self.apps, self.transport, self.pair = applications, archive_transport, pair
        self.spec, self.source, self.source_pin = deepcopy(observer_spec), str(source), source_sha256
        descriptor = _json(self.spec['descriptor_raw'])
        _need(descriptor['arguments']['CallbackSha256'] == _sha(callback_source))
        self.manifest, self.port, self.label = manifest_sha256, port, mapping_label
        self.callback, self.starter = callback_source, {'path': starter_path, 'sha256': starter_sha256}
        self.lock, self.issued = threading.Lock(), False
        self.native_windows = _windows_source()

    def _prepare(self, operation, chain, carryover, deadline):
        self.apps._operation(operation, 'connect_rollback_held', deadline)
        _need(operation['scope'] == self.scope and type(chain) is dict
              and operation['plan_material_sha256'] is None and wire._hash(carryover))
        for phase in ('preflight', 'close_pair', 'quiesce_apps', 'carryover', 'verify_restored_pair'):
            _validate(phase, chain.get(phase), self.scope, chain)
        _need(chain['carryover']['carryover_sha256'] == carryover
              and chain['close_pair']['terminal_sha256'] == self.apps.terminal_pin)
        quiesce = self.apps.read_quiesce_request()
        _need(_sha(wire._bytes(_scope_request(quiesce))) == chain['quiesce_apps']['request_sha256'])
        attempt = _fallback_attempt(self.scope)
        return prepare_held(self.spec, boot=self.scope['previous_windows_boot_sha256'],
            nonce=operation['nonce'], attempt=attempt,
            stopped=self.apps.previous.args['postboot_stopped_receipt_sha256'],
            carryover=carryover, port=self.port, mapping_label=self.label)

    def _host(self, mode, operation, chain, carryover, spec, challenge, deadline):
        _need(mode in {'preflight', 'apply', 'observe'} and wire._hash(challenge))
        terminal = chain['close_pair']['terminal_sha256']
        guard_source, guard_challenge = self.pair._command('closed', terminal)
        cfg = {'mode': mode, 'writer': {'windows_boot_sha256': self.scope['previous_windows_boot_sha256']},
               'plan_path': self.pair.config['plan'], 'plan_sha256': self.scope['previous_plan_sha256'],
               'binding_sha256': self.scope['previous_instance_sha256'],
               'terminal_sha256': terminal, 'revision': self.scope['previous_revision'] + 1,
               'guard': base64.b64encode(guard_source.encode()).decode(), 'guard_challenge': guard_challenge,
               'operation_nonce': operation['nonce'], 'intent_sha256': _sha(_encoded(operation | {'held': spec})),
               'spec': spec, 'starter': self.starter, 'carryover': carryover,
               'callback': base64.b64encode(self.callback).decode(), 'probe': challenge}
        blob = base64.b64encode(gzip.compress(_encoded(cfg), mtime=0)).decode()
        source = self.native_windows.replace('CONFIG_GZIP_BASE64', blob)
        _need(len(source.encode()) <= 131072)
        result = self.pair.windows.powershell(source, deadline)
        if mode == 'preflight':
            _need(result == {'status': 'HELD_INTAKE_READY', 'challenge': challenge})
            return result
        _need(type(result) is dict and set(result) == {'status', 'challenge', 'observation_sha256',
              'challenge_sha256', 'observer'} and result['status'] == 'HELD_CALLBACK_VERIFIED'
              and result['challenge'] == challenge and result['observation_sha256'] == carryover
              and wire._hash(result['challenge_sha256']))
        callback = json.loads(base64.b64decode(spec['config_base64']), object_pairs_hook=wire._pairs)
        probe_wire = json.dumps({'guest_boot_sha256': callback['guest_boot_sha256'],
            'hold_sha256': callback['hold_sha256'], 'purpose': 'held_admission', 'probe': challenge},
            separators=(',', ':'), ensure_ascii=True).encode()
        _need(result['challenge_sha256'] == _sha(probe_wire))
        fact, args = result['observer'], spec['descriptor']['arguments']
        _need(type(fact) is dict and set(fact) == {'server_pid', 'pipe_name', 'key_sha256',
              'creation_filetime', 'ready_sha256', 'config_sha256', 'input_sha256'}
              and type(fact['server_pid']) is int and 1 < fact['server_pid'] < 2**31
              and fact['pipe_name'] == args['PipeName'] and fact['key_sha256'] == args['KeySha256']
              and fact['config_sha256'] == spec['config_sha256'] and fact['input_sha256'] == spec['input_sha256']
              and wire._hash(fact['ready_sha256']) and type(fact['creation_filetime']) is str
              and re.fullmatch('[1-9][0-9]{0,19}', fact['creation_filetime']))
        return result

    def _guest(self, mode, operation, chain, carryover, spec, deadline):
        transport, dispatcher = self.transport, self.transport.dispatcher
        seconds = wire._remaining(deadline)
        _need(0 < seconds <= 600)
        envelope = {'mode': mode, 'operation_nonce': operation['nonce'],
                    'operation_sha256': _sha(_encoded(operation)), 'carryover_sha256': carryover}
        cfg = {'envelope': envelope, 'seconds': seconds, 'archive': transport.options,
               'attempt': _fallback_attempt(self.scope), 'source': self.source, 'source_pin': self.source_pin,
               'port': self.port, 'manifest': self.manifest,
               'stopped': self.apps.previous.args['postboot_stopped_receipt_sha256'],
               'quiesce': _scope_request(self.apps.read_quiesce_request()),
               'quiesce_receipt': chain['quiesce_apps'],
               'rollback': chain['preflight']['rollback_sha256'], 'bootstrap': _BOOTSTRAP}
        code = transport.launcher.split(MARKER)[0].decode() + '\nimport json,sys\nCFG=json.loads(' + repr(_encoded(cfg).decode()) + ')\n' + _GUEST
        _need(len(code.encode()) <= 131072)
        compile(code, '<saved-site-held>', 'exec')
        stamps, records = {}, {}
        for role, endpoint in (('jump', dispatcher.jump), ('A', dispatcher.guests['A'])):
            stamps.update(ssh._identity(endpoint, deadline))
            path = dispatcher.known_hosts[role]
            raw, stamps[path] = ssh._checked_file(path, private=False, maximum=1048576)
            records[endpoint.alias] = ssh._host_record(raw, endpoint, guest=role == 'A')
        def fresh(challenge, boundary, limit):
            _need(ssh._same_files(stamps))
            self.apps._operation(operation, 'connect_rollback_held', limit)
            closed = transport._closed(challenge, boundary, limit)
            after = transport._closed(challenge, boundary, limit)
            _need(ssh._same_files(stamps))
            return _sha(_encoded({'operation': envelope, 'challenge': challenge, 'boundary': boundary,
                                 'closed_pair': closed, 'closed_pair_after': after}))

        root = '/private/tmp' if sys.platform == 'darwin' else '/tmp'
        with tempfile.TemporaryDirectory(prefix='ha-sameboot-held-', dir=root) as folder:
            config = ssh._ssh_config(Path(folder), dispatcher.jump, dispatcher.guests['A'], records)
            command = (ssh.SSH, '-F', str(config), '-T', dispatcher.guests['A'].alias,
                       shlex.join(('/usr/bin/python3', '-I', '-B', '-S', '-c', code)))
            _need(ssh._same_files(stamps))
            raw = ssh._activation_exchange(command, _encoded(envelope), deadline, operation['nonce'], fresh,
                bind_boundary=True, boundaries=BOUNDARIES[mode], boundary_seconds=90)
            _need(ssh._same_files(stamps))
        result = json.loads(raw, object_pairs_hook=wire._pairs)
        _need(type(result) is dict and set(result) == set(envelope) | {'status', 'guest'}
              and all(result.get(k) == v for k, v in envelope.items()) and result['status'] == 'OBSERVED')
        fact = result['guest']
        _need(type(fact) is dict and set(fact) == {'pid', 'fragment_sha256', 'argv_sha256', 'start_ticks'}
              and type(fact['pid']) is int and fact['pid'] > 1
              and all(wire._hash(fact[k]) for k in ('fragment_sha256', 'argv_sha256'))
              and type(fact['start_ticks']) is str and fact['start_ticks'].isdecimal())
        return fact

    def observe_held_carryover(self, operation, chain, carryover_sha256, deadline, *, observe_only):
        """Create exactly once, or only read an already issued/started exact pair."""
        _need(type(observe_only) is bool)
        with self.lock:
            spec = self._prepare(operation, chain, carryover_sha256, deadline)
            if not observe_only:
                _need(not self.issued)
                self.issued = True
                self._host('preflight', operation, chain, carryover_sha256, spec, secrets.token_hex(32), deadline)
            guest = self._guest('observe' if observe_only else 'apply', operation, chain,
                                carryover_sha256, spec, deadline)
            # The native VSOCK observer acquires the same guest mutation lock.
            # Finish the SSH command and release that lock before probing it.
            fact = self._host('observe' if observe_only else 'apply', operation, chain,
                              carryover_sha256, spec, secrets.token_hex(32), deadline)
            _need(self._guest('observe', operation, chain, carryover_sha256, spec, deadline) == guest)
            if not observe_only:
                final = self._host('observe', operation, chain, carryover_sha256, spec,
                                   secrets.token_hex(32), deadline)
                _need(final['observer'] == fact['observer'])
            self.apps._operation(operation, 'connect_rollback_held', deadline)
            endpoint = {key: fact['observer'][key] for key in ('server_pid', 'pipe_name', 'key_sha256')}
            result = {'held_observation_sha256': carryover_sha256, 'held_endpoint': endpoint}
            _validate('connect_rollback_held', result, self.scope, chain)
            return result


_BOOTSTRAP = '''import hashlib,os,stat,sys
from pathlib import Path
path=Path(sys.argv[1])
for parent in path.parents:
    st=parent.lstat()
    assert stat.S_ISDIR(st.st_mode) and st.st_uid==0 and not st.st_mode&0o022
with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),'rb') as source:
    st=os.fstat(source.fileno())
    assert stat.S_ISREG(st.st_mode) and st.st_uid==0 and st.st_nlink==1 and stat.S_IMODE(st.st_mode)==0o400
    raw=source.read(131073)
    after=os.fstat(source.fileno())
    assert all(getattr(st,k)==getattr(after,k) for k in ('st_dev','st_ino','st_mode','st_uid','st_gid','st_nlink','st_size','st_mtime_ns','st_ctime_ns'))
    assert 0<len(raw)==st.st_size<=131072 and hashlib.sha256(raw).hexdigest()==sys.argv[2]
sys.argv=[str(path),*sys.argv[3:]]
exec(compile(raw,str(path),'exec'),{'__name__':'__main__','__file__':str(path)})
'''


_GUEST = r'''
import os,time,secrets
from pathlib import Path
from ha_control import native_guest_worker as w,native_guest_postboot as post,native_legacy_provider as native,native_stdio_bridge as wire
from ha_control.native_saved_site_apps import _request
channel=wire.Stdio(sys.stdin.fileno(),sys.stdout.fileno());limit=time.monotonic()+CFG['seconds']
assert channel.read(limit)==CFG['envelope'] and os.geteuid()==0
envelope=CFG['envelope'];options=CFG['archive'];binding=wire.Binding(**options['previous_request']['binding'])
assert wire._digest(w.runtime.BOOT_ID.read_bytes())==binding.boot_a_sha256
stage=w.ROOT/binding.attempt_id
adapter=native.ProviderRetirement(stage,binding,latest_intent_sha256=options['history']['intent_sha256'],
 rollback_sha256=CFG['rollback'],
 hold_sha256=options['hold_sha256'],provider_port=options['provider_port'],held_port=options['held_port'])
units=adapter._archive()
restore_pin=adapter._receipt('restore')
restore_path=adapter._paths('restore')[1]
restore_raw=w.runtime._read(restore_path,0,{0o400},limit=131072);restored=json.loads(restore_raw,object_pairs_hook=wire._pairs)
assert wire._digest(restore_raw)==restore_pin and restored['held']==options['expected_held']
assert restored['provider']['argv_sha256']==units['provider']['argv_sha256']
carry_path=post.carryover_path(wire.Request(binding,'A',wire.GUEST_OBSERVE_INITIAL_POSTBOOT,1,envelope['operation_nonce'],{}),CFG['attempt'])
carry_raw=w.runtime._read(carry_path,0,{0o400},limit=262144)
assert wire._digest(carry_raw)==envelope['carryover_sha256']
carry=json.loads(carry_raw,object_pairs_hook=wire._pairs)
assert carry['history']==options['history'] and carry['postboot']['stopped_receipt_sha256']==CFG['stopped']
quiesce=_request(CFG['quiesce']);quiesce_receipt=CFG['quiesce_receipt']
assert quiesce.binding==binding and quiesce.verb==wire.GUEST_QUIESCE_SAMEBOOT_SUCCESSOR
assert wire._digest(wire._bytes(CFG['quiesce']))==quiesce_receipt['request_sha256']
# This public native reader owns the non-reentrant guest lock itself. Its
# full proof brackets the command; under our lock use its lock-free observe
# core and retain the exact protected intent/done bytes instead.
assert post.observe_quiesce_completion(quiesce,wire._digest(wire._bytes(options['history'])),
 expected=quiesce_receipt['stop_receipt_sha256'])==quiesce_receipt
quiesce_intent,quiesce_done=post.paths(quiesce,quiesce.args['postboot_attempt_id'])
quiesce_intent_raw=w.runtime._read(quiesce_intent,0,{0o400},limit=262144)
quiesce_done_raw=w.runtime._read(quiesce_done,0,{0o400},limit=262144)
args=carry['recovery_args']|{'postboot_attempt_id':CFG['attempt'],
 'postboot_stopped_receipt_sha256':CFG['stopped'],'postboot_carryover_receipt_sha256':envelope['carryover_sha256']}
request=wire.Request(binding,'A',wire.GUEST_OBSERVE_INITIAL_POSTBOOT,1,envelope['operation_nonce'],args)
source=Path(CFG['source'])
assert source==w.ROOT/('recovery-source-'+CFG['manifest'])/'native-guest-recovery-observer.py'
assert wire._digest(w.runtime._read(source,0,{0o400},limit=131072))==CFG['source_pin']
argv=['/usr/bin/python3','-I','-B','-S','-c',CFG['bootstrap'],str(source),CFG['source_pin'],'--port',str(CFG['port']),
 '--hold-sha256',options['hold_sha256'],'--guest-boot-sha256',binding.boot_a_sha256,
 '--postboot-attempt-id',CFG['attempt'],'--postboot-stopped-receipt-sha256',CFG['stopped'],
 '--postboot-carryover-receipt-sha256',envelope['carryover_sha256']]
argvraw=('\0'.join(argv)+'\0').encode();unit='n100ha-postboot-observer-'+CFG['attempt'][:32]+'.service'
assert unit not in {units['held']['unit'],units['provider']['unit']}
entry={'unit':unit,'argv':argvraw.hex(),'argv_sha256':wire._digest(argvraw)}
new=native.ProviderRetirement(stage,binding,latest_intent_sha256=adapter.intent,rollback_sha256=adapter.rollback,
 hold_sha256=adapter.hold,provider_port=adapter.ports['provider'],held_port=CFG['port'])
expected={'schema_version':1,'operation_sha256':envelope['operation_sha256'],'operation_nonce':envelope['operation_nonce'],
 'attempt':CFG['attempt'],'carryover_sha256':envelope['carryover_sha256'],'source_sha256':CFG['source_pin'],
 'argv_sha256':entry['argv_sha256'],'port':CFG['port'],'restore_receipt_sha256':restore_pin,'automatic_retry_permitted':False}
issued=stage/('held-sameboot-'+CFG['attempt']+'-issued.json');done=stage/('held-sameboot-'+CFG['attempt']+'-done.json')
def guard():
 assert time.monotonic()<limit
 assert adapter._archive()==units
 assert adapter._receipt('restore')==restore_pin
 assert wire._digest(w.runtime._read(source,0,{0o400},limit=131072))==CFG['source_pin']
 assert w.runtime._read(restore_path,0,{0o400},limit=131072)==restore_raw
 assert w.runtime._read(carry_path,0,{0o400},limit=262144)==carry_raw
 assert post.observe_carryover(request,CFG['attempt'],envelope['carryover_sha256'])==carry
 assert w.runtime._read(quiesce_intent,0,{0o400},limit=262144)==quiesce_intent_raw
 assert w.runtime._read(quiesce_done,0,{0o400},limit=262144)==quiesce_done_raw
 stopped=post.observe(quiesce,quiesce.args['postboot_attempt_id'],quiesce_receipt['stop_receipt_sha256'])
 intent=json.loads(quiesce_intent_raw,object_pairs_hook=wire._pairs)
 proof=intent.get('closed_proof_sha256')
 assert wire._hash(proof) and stopped['args']==quiesce.args
 assert intent=={'schema_version':1,**w._scope(quiesce),'args':quiesce.args,
  'closed_proof_sha256':proof,'containers':stopped['containers'],'automatic_retry_permitted':False}
 assert stopped['closed_revision']==quiesce_receipt['revision']
 assert stopped['closed_proof_sha256']==quiesce_receipt['closed_proof_sha256']
 assert adapter._live(units['held'],'held')==options['expected_held']
 assert adapter._live(units['provider'],'provider',original=False)==restored['provider']
 assert wire._digest(w.runtime.BOOT_ID.read_bytes())==binding.boot_a_sha256
def absent():
 assert not os.path.lexists(native.TRANSIENT/unit) and CFG['port'] not in native._listeners()
 props=native._properties(unit)
 assert props['Id']==unit and props['MainPID']=='0' and props['ActiveState'] in ('inactive','failed')
def own():
 value=new._live(entry,'held',original=False)
 ticks=(Path('/proc')/str(value['pid'])/'stat').read_text().rsplit(')',1)[1].split()[19]
 assert ticks.isdecimal() and value['pid'] not in (options['expected_held']['pid'],restored['provider']['pid'])
 assert new._live(entry,'held',original=False)==value
 return value|{'start_ticks':ticks}
def gate(boundary):
 guard();challenge=wire._digest(wire._bytes(expected|{'boundary':boundary,'nonce':secrets.token_hex(32)}))
 fields={'request_nonce':envelope['operation_nonce'],'challenge':challenge,'boundary':boundary}
 channel.write({'kind':'activation_boundary',**fields},limit);reply=channel.read(limit)
 assert wire._matches(reply,{'kind':'activation_boundary_current',**fields,'windows_proof_sha256':reply.get('windows_proof_sha256')})
 assert wire._hash(reply['windows_proof_sha256']);guard()
try:
 with w._guest_mutation_descriptor(wait=2):
  guard()
  if envelope['mode']=='apply':
   assert not os.path.lexists(issued) and not os.path.lexists(done)
   absent();gate('before');absent();gate('dispatch');absent()
   w.runtime._write(issued,wire._bytes(expected),mode=0o400)
   native._command(('/usr/bin/systemd-run','--quiet','--collect','--expand-environment=no','--unit='+unit,
    '--property=User=root','--property=Restart=no','--property=NoNewPrivileges=yes','--property=PrivateTmp=yes','--',*argv))
   until=min(limit,time.monotonic()+6)
   while CFG['port'] not in native._listeners():
    assert time.monotonic()<until;time.sleep(.1)
  else:assert w.runtime._read(issued,0,{0o400},limit=8192)==wire._bytes(expected)
  initial=own();gate('after');assert own()==initial
  if envelope['mode']=='apply':w.runtime._write(done,wire._bytes(expected|{'guest':initial}),mode=0o400)
  elif os.path.lexists(done):assert json.loads(w.runtime._read(done,0,{0o400},limit=8192))==expected|{'guest':initial}
  guard()
 assert post.observe_quiesce_completion(quiesce,wire._digest(wire._bytes(options['history'])),
  expected=quiesce_receipt['stop_receipt_sha256'])==quiesce_receipt
 channel.write(envelope|{'status':'OBSERVED','guest':initial},limit)
except Exception:channel.write(envelope|{'status':'UNKNOWN'},limit)
'''


_WINDOWS_GUARD = r'''
function Guard {
 $rows=@(& ([scriptblock]::Create($enc.GetString([Convert]::FromBase64String($cfg.guard)))))
 Need ($rows.Count-eq 1);$v=ConvertFrom-Json $rows[0]
 Need ($v.challenge-ceq $cfg.guard_challenge -and $v.status-ceq 'CLOSED_PAIR_VERIFIED' -and
       $v.terminal_sha256-ceq $cfg.terminal_sha256 -and $v.closed_revision-eq $cfg.revision -and
       $v.proof_sha256-cmatch '\A[a-f0-9]{64}\z')
}
function Preserved {
 $r=[ordered]@{}
 foreach($role in @('legacy','held_admission','provider_isolation')){
  $endpoint=if($role-ceq 'legacy'){$plan.windows_initial_observer}else{$plan.windows_recovery_observers.$role}
  $p=Get-Process -Id $endpoint.server_pid -ErrorAction Stop
  try{Need (-not $p.HasExited -and $p.SessionId-eq 0)
      $r[$role]=@($p.Id,$p.StartTime.ToFileTimeUtc().ToString(),$p.MainModule.FileName)}finally{$p.Dispose()}
 }
 return (ConvertTo-Json -InputObject $r -Depth 5 -Compress)
}
'''


_WINDOWS_BODY = r'''
try{
 Need ($PSVersionTable.PSEdition-ceq 'Desktop' -and [Environment]::Is64BitProcess -and $cfg.mode-cin @('preflight','apply','observe'))
 try{$locked=$mutex.WaitOne(0)}catch [Threading.AbandonedMutexException]{$locked=$true;throw 'held_lock_abandoned'};Need $locked
 Guard
 $s=$cfg.spec;$a=$s.descriptor.arguments
 $plan=ConvertFrom-Json ($enc.GetString((File $cfg.plan_path $cfg.plan_sha256 65536 $a.ServiceSid)))
 $decisionRoot=[IO.Path]::GetDirectoryName($cfg.plan_path)
 $decision=Join-Path $decisionRoot ('held-sameboot-'+$cfg.operation_nonce+'.issued.json');$intent=$enc.GetBytes($cfg.intent_sha256)
 $old=ConvertFrom-Json ($enc.GetString((File $s.source_input_path $s.source_input_sha256 8192 $a.ServiceSid)))
 $oldConfig=ConvertFrom-Json ($enc.GetString((File $s.source_callback_path $s.source_callback_sha256 8192 $a.ServiceSid)))
 Need ($old.arguments.PipeName-ceq $plan.windows_recovery_observers.held_admission.pipe_name -and
       $old.arguments.KeySha256-ceq $plan.windows_recovery_observers.held_admission.key_sha256 -and
       $oldConfig.purpose-ceq 'held_admission' -and $oldConfig.windows_boot_sha256-ceq $cfg.writer.windows_boot_sha256 -and
       $old.server_path-ceq $s.descriptor.server_path -and $old.server_sha256-ceq $s.descriptor.server_sha256)
 foreach($field in @('PlanSha256','AttemptId','WindowsBootSha256','CallbackPath','CallbackSha256','KeyPath','KeySha256','RecoveryProtocolPath','RecoveryProtocolSha256','OperatorSid','ServiceSid','Generation')){Need ($old.arguments.$field-ceq $a.$field)}
 $starter=File $cfg.starter.path $cfg.starter.sha256 65536
 $null=File $s.descriptor.server_path $s.descriptor.server_sha256 65536 $a.ServiceSid
 $callback=File $a.CallbackPath $a.CallbackSha256 65536 $a.ServiceSid
 Need ((Pin ([Convert]::FromBase64String($cfg.callback)))-ceq $a.CallbackSha256)
 $null=File $a.RecoveryProtocolPath $a.RecoveryProtocolSha256 65536 $a.ServiceSid
 $key=File $a.KeyPath $a.KeySha256 64 $a.ServiceSid;Need ($key.Length-eq 32);[Array]::Clear($key,0,$key.Length)
 $preserved=Preserved;$action=ExpectedAction $s
 if($cfg.mode-cin @('preflight','apply')){
  Need (-not (Test-Path -LiteralPath $decision) -and -not (Test-Path -LiteralPath $s.config_path) -and
        -not (Test-Path -LiteralPath $s.input_path) -and -not (Test-Path -LiteralPath $a.ReadyPath) -and
        -not (Test-Path -LiteralPath ($a.ReadyPath+'.outcome.json')) -and
        $null-eq (Get-ScheduledTask -TaskPath '\' -TaskName $s.task_name -ErrorAction SilentlyContinue))
  Mapping $s.mapping -CheckAbsent
 }
 if($cfg.mode-ceq 'preflight'){
  Guard;Need ((Preserved)-ceq $preserved)
  @{status='HELD_INTAKE_READY';challenge=$cfg.probe}|ConvertTo-Json -Compress
  return
 }
 if($cfg.mode-ceq 'apply'){
  Guard;NewPrivate $decision $intent
  Mapping $s.mapping
  Guard;NewPrivate $s.config_path ([Convert]::FromBase64String($s.config_base64))
  NewPrivate $s.input_path ([Convert]::FromBase64String($s.input_base64))
  Guard
  $rows=@(& ([scriptblock]::Create($enc.GetString($starter))) -InputPath $s.input_path -InputSha256 $s.input_sha256 -Go)
  Need ($rows.Count-eq 1);$started=ConvertFrom-Json $rows[0];Need ($started.observer_ready-ceq $true)
 }
 $null=File $decision (Pin $intent) 4096;Mapping $s.mapping -ReadOnly
 $null=File $s.input_path $s.input_sha256 8192
 $config=ConvertFrom-Json ($enc.GetString((File $s.config_path $s.config_sha256 8192)))
 $fact=Observe $s $action
 if($cfg.mode-ceq 'apply'){Need ($fact.server_pid-eq $started.observer_pid)}
 # Invoke the exact installed native callback. Its nonce binds the full fresh
 # request; native VSOCK reply comparison rejects a bare echoed receipt digest.
 . ([scriptblock]::Create($enc.GetString($callback)))
 Initialize-HaRecoveryObservation $config
 $raw=ConvertTo-Json -Compress -InputObject ([ordered]@{guest_boot_sha256=$config.guest_boot_sha256;hold_sha256=$config.hold_sha256;purpose='held_admission';probe=$cfg.probe})
 $deadline=[Diagnostics.Stopwatch]::GetTimestamp()+[long]([Diagnostics.Stopwatch]::Frequency*5)
 $proof=Get-HaRecoveryObservation $raw $deadline
 Need ($proof.closed -is [bool] -and $proof.closed -and $proof.observation_sha256-ceq $cfg.carryover -and $proof.challenge_sha256-ceq (Pin ($enc.GetBytes($raw))))
 Guard;Need ((Preserved)-ceq $preserved -and (ConvertTo-Json -Compress -InputObject (Observe $s $action))-ceq (ConvertTo-Json -Compress -InputObject $fact))
 @{status='HELD_CALLBACK_VERIFIED';challenge=$cfg.probe;observation_sha256=$proof.observation_sha256;challenge_sha256=$proof.challenge_sha256;observer=$fact}|ConvertTo-Json -Depth 5 -Compress
}catch{exit 1}finally{foreach($h in $held){$h.Dispose()};if($locked){$mutex.ReleaseMutex()};$mutex.Dispose()}
'''
