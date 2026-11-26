"""Concrete saved-VM site hooks for native quiescence and app journals.

Constructing the adapter performs no I/O. Calls require the coordinator's exact
private issued operation. Terminal and carryover evidence are append-only;
native stop/start remain owned by NativeSamebootDriver and SuccessorAppStage.
"""

import base64
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile
import time

from . import native_guest_ssh as ssh, native_stdio_bridge as wire
from .controller import TransitionBlocked
from .initial_app_resume_wiring import SuccessorAppStage
from .initial_unpublished import derive_predecessor
from .native_guest_postboot import DRAFT_PIN
from .native_sameboot_release import WindowsPairClosure
from .sameboot_transition import _closed, _fallback_attempt, _private_file, _sameboot_recovery, _scope

MARKER = b'from ha_control.native_guest_worker import main\nraise SystemExit(main())'


def _need(value):
    if not value:
        raise TransitionBlocked('saved_site_apps_unconfirmed')


def _scope_request(request):
    return {'version': wire.VERSION, 'binding': request.binding.fields(), 'node': request.node,
            'boot_sha256': request.binding.boot(request.node), 'verb': request.verb,
            'sequence': request.sequence, 'nonce': request.nonce, 'args': json.loads(wire._bytes(request.args))}


def _request(value):
    _need(type(value) is dict and set(value) == {'version', 'binding', 'node', 'boot_sha256', 'verb', 'sequence', 'nonce', 'args'})
    request = wire.Request(wire.Binding(**value['binding']), value['node'], value['verb'],
                           value['sequence'], value['nonce'], value['args'])
    wire._args(request.verb, request.node, request.args)
    _need(_scope_request(request) == value and type(request.sequence) is int
          and 0 < request.sequence <= wire.MAX_REQUESTS and wire._hash(request.nonce))
    return request


_GUEST = r'''
import base64,json,os
from ha_control import native_guest_worker as w,native_stdio_bridge as wire,initial_app_resume as resume,native_guest_postboot as post
assert os.geteuid()==0
previous=wire.Request(wire.Binding(**CFG['previous']['binding']),CFG['previous']['node'],CFG['previous']['verb'],
    CFG['previous']['sequence'],CFG['previous']['nonce'],CFG['previous']['args'])
request=wire.Request(wire.Binding(**CFG['request']['binding']),CFG['request']['node'],CFG['request']['verb'],
    CFG['request']['sequence'],CFG['request']['nonce'],CFG['request']['args'])
assert wire._digest(w.runtime.BOOT_ID.read_bytes())==previous.binding.boot('A')
assert request.binding==previous.binding and w._scope(request)|{'args':request.args}==CFG['request']
if CFG['action'] in {'archive','observe'}:
    assert callable(getattr(post,'observe_quiesce_completion',None))
if CFG['action']!='authorization':
    terminal=base64.b64decode(CFG['terminal'],validate=True)
    assert wire._digest(terminal)==request.args['predecessor_terminal_sha256']
    paths=(w.ROOT/('predecessor-plan-'+request.args['predecessor_plan_sha256']+'.json'),
           w.ROOT/('predecessor-terminal-'+request.args['predecessor_terminal_sha256']+'.json'))
def history():
    raw,hold,prepared,active=w._read_initial_recovery_hold(previous)
    auth=resume._latest_history(previous,raw,hold,active,CFG['history'])
    assert auth['service_instance_sha256']==CFG['previous_instance']
    assert wire._digest(wire._bytes(CFG['history']))==CFG['history_sha256']
    w._assert_car_route_blocked(previous,raw,prepared,active)
    return raw,hold,prepared,active
if CFG['action']=='authorization':
    # Reconstruct a lost local attestation response from the existing protected
    # guest record. No new attestation or request is issued.
    with w._guest_mutation_descriptor(wait=2):
        raw,hold,prepared,active=w._read_initial_recovery_hold(request)
        target=resume.path(request)
        body=w.runtime._read(target,0,{0o400},limit=131072);value=resume.decode(body)
        resume._validate_authorization(request,raw,hold,active,value,partial=False)
        resume.preserved(request,body,value,hold,active,raw)
        assert value['service_instance_sha256']==CFG['instance']
        assert value['prior_intent_sha256']==CFG['prior_intent'] and value['prior_progress_sha256']==CFG['prior_progress']
        assert w.runtime._read(target,0,{0o400},limit=131072)==body
        result={'schema_version':1,'authorization_sha256':wire._digest(body),'request_nonce':request.nonce,
            'baseline_sha256':wire._digest(wire._bytes(value['container_contracts'])),
            'prior_intent_sha256':value['prior_intent_sha256'],'prior_progress_sha256':value['prior_progress_sha256'],
            'service_instance_sha256':value['service_instance_sha256'],'container_count':8,
            'containers_started':False,'database_changed':False}
else:
    with w._guest_mutation_descriptor(wait=2):
        before=history()
        for path in paths:
            if os.path.lexists(path):assert w.runtime._read(path,0,{0o400},limit=65536)==terminal
        if CFG['action'] in {'archive','archive_terminal'}:
            _,_,contracts,_=post.current(previous)
            assert all(row['running'] is True for row in contracts.values())
            for path in paths:
                if not os.path.lexists(path):w.runtime._write(path,terminal,0o400)
        assert all(w.runtime._read(path,0,{0o400},limit=65536)==terminal for path in paths)
        assert post.closed_revision(request)==CFG['closed_revision']
        assert history()==before
    if CFG['action']=='observe':
        result=post.observe_quiesce_completion(request,CFG['history_sha256'],expected=CFG['expected'])
        with w._guest_mutation_descriptor(wait=2):
            assert history()==before
            assert all(w.runtime._read(path,0,{0o400},limit=65536)==terminal for path in paths)
    else:
        assert CFG['action'] in {'archive','archive_terminal'}
        result={'terminal_sha256':wire._digest(terminal),'metadata_copies':2,'existing_records_preserved':True}
print(json.dumps(result,sort_keys=True,separators=(',',':')))
'''

_CARRYOVER_GUEST = r'''
from dataclasses import replace
import json,os,sys
from ha_control import native_guest_worker as w,native_guest_postboot as post,native_stdio_bridge as wire

assert os.geteuid()==0
def decode(value):
    request=wire.Request(wire.Binding(**value['binding']),value['node'],value['verb'],
                         value['sequence'],value['nonce'],value['args'])
    wire._args(request.verb,request.node,request.args)
    assert w._scope(request)|{'args':request.args}==value
    return request

previous,stop,draft=(decode(CFG[key]) for key in ('previous','stop','draft'))
assert previous.binding==stop.binding==draft.binding and previous.node==stop.node==draft.node=='A'
assert wire._digest(w.runtime.BOOT_ID.read_bytes())==previous.binding.boot('A')
assert previous.verb==draft.verb==wire.GUEST_START_INITIAL_POSTBOOT
assert stop.verb==wire.GUEST_QUIESCE_SAMEBOOT_SUCCESSOR
assert len({previous.nonce,stop.nonce,draft.nonce})==3
plan=sys.stdin.buffer.read(65537)
assert 0<len(plan)<=65536 and wire._digest(plan)==previous.args['postboot_plan_sha256']
history=CFG['history']; history_pin=wire._digest(wire._bytes(history)); stop_receipt=CFG['stop_receipt']
postboot=draft.args['postboot']
assert draft.args['postboot_plan_sha256']==postboot['carryover_receipt_sha256']==postboot['predecessor_seal_sha256']==post.DRAFT_PIN
assert postboot['predecessor_plan_sha256']==stop.args['predecessor_plan_sha256']==wire._digest(plan)
terminal=postboot['predecessor_terminal_sha256']
assert terminal==stop.args['predecessor_terminal_sha256']
paths=(w.ROOT/('predecessor-plan-'+wire._digest(plan)+'.json'),
       w.ROOT/('predecessor-terminal-'+terminal+'.json'))
def verified():
    copies=[w.runtime._read(path,0,{0o400},limit=8192) for path in paths]
    assert copies[0]==copies[1] and wire._digest(copies[0])==terminal
    assert post.observe_quiesce_completion(stop,history_pin,expected=stop_receipt['stop_receipt_sha256'])==stop_receipt

verified()
target=post.carryover_path(draft,postboot['attempt_id'])
intent_path=target.with_name('provider-rollback-carryover-'+postboot['attempt_id']+'-issued.json')
intent=wire._bytes({'nonce':draft.nonce,'draft_sha256':wire._digest(wire._bytes(CFG['draft'])),
    'stop_request_sha256':stop_receipt['request_sha256'],'stop_receipt_sha256':stop_receipt['stop_receipt_sha256'],
    'predecessor_plan_sha256':wire._digest(plan),'history_sha256':history_pin,'automatic_retry':False})
with w._guest_mutation_descriptor(wait=2):
    if CFG['observe_only'] is False:
        assert not os.path.lexists(target)
        w.runtime._write(intent_path,intent,mode=0o400)
    assert w.runtime._read(intent_path,0,{0o400},limit=4096)==intent
receipt=None
if CFG['observe_only'] is False:
    receipt=post.attest_carryover(draft,predecessor_plan_raw=plan,predecessor_request=previous,history=history)
    assert receipt.status=='PASS'
body=w.runtime._read(target,0,{0o400},limit=262144)
pin=wire._digest(body)
assert receipt is None or receipt.receipt_sha256==pin
admitted=replace(draft,args=draft.args|{'postboot':postboot|{'carryover_receipt_sha256':pin}})
record=post.observe_carryover(admitted,postboot['attempt_id'],pin)
assert record['history']==history and record['predecessor_request']=={'scope':w._scope(previous),'args':previous.args}
verified()
assert w.runtime._read(intent_path,0,{0o400},limit=4096)==intent
assert w.runtime._read(target,0,{0o400},limit=262144)==body
print(json.dumps({'nonce':draft.nonce,'attempt':postboot['attempt_id'],'carryover_sha256':pin,
                  'history_sha256':history_pin,'stop_sha256':stop_receipt['stop_receipt_sha256']},
                 sort_keys=True,separators=(',',':')))
'''


class SavedSiteApps:
    """Platform hooks consumed directly by NativeSamebootDriver.

    Inputs contain no PostgreSQL credentials: the exact completed predecessor
    request/history, closed terminal, pinned guest package and existing pair
    verifier suffice. journal and coordinator_journal are distinct, existing
    private 0700 directories. boundary_powershell may be a caller-held
    WindowsSsh.live_authority_session callable for bounded guest stop checks.
    """

    def __init__(self, *, dispatcher, launcher, manifest_sha256, previous_request, history,
                 terminal, pair, journal, coordinator_journal, assert_guard,
                 boundary_powershell=None):
        _need(type(dispatcher) is ssh.GuestSshDispatcher and type(pair) is WindowsPairClosure
              and type(previous_request) is wire.Request and previous_request.binding == dispatcher.binding
              and previous_request.node == 'A' and previous_request.verb == wire.GUEST_START_INITIAL_POSTBOOT
              and type(launcher) is bytes and wire._digest(launcher) == dispatcher.launcher_sha256
              and launcher.count(MARKER) == 1 and manifest_sha256.encode() in launcher
              and wire._hash(manifest_sha256) and callable(assert_guard)
              and (boundary_powershell is None or callable(boundary_powershell)))
        self.previous = _request(_scope_request(previous_request))
        self.history = json.loads(wire._bytes(history))
        _need(type(self.history) is dict and set(self.history) == {'authorization_sha256', 'intent_sha256', 'progress_sha256', 'done_sha256'}
              and all(wire._hash(self.history[key]) for key in ('authorization_sha256', 'intent_sha256', 'done_sha256'))
              and type(self.history['progress_sha256']) is dict
              and set(self.history['progress_sha256']) == {'progress-' + role + '.json' for role in wire.RECOVERY_START_ORDER}
              and all(wire._hash(pin) for pin in self.history['progress_sha256'].values())
              and type(terminal) is bytes and 0 < len(terminal) <= 8192)
        self.terminal, self.terminal_pin = terminal, wire._digest(terminal)
        self.closed_revision, _, _ = derive_predecessor(terminal, self.terminal_pin, published=True)
        prior = json.loads(terminal, object_pairs_hook=wire._pairs)
        _need(prior['controller_plan_sha256'] == self.previous.args['postboot_plan_sha256'] == pair.config['plan_sha256']
              and prior['postboot'] == self.previous.args['postboot']
              and prior['attempt_id'] == dispatcher.binding.attempt_id
              and prior['legacy_plan_sha256'] == dispatcher.binding.plan_sha256
              and prior['recovery']['attempt_id'] == self.previous.args['recovery_attempt_id']
              and pair.config['revision'] + 1 == self.closed_revision)
        self.dispatcher, self.launcher, self.manifest = dispatcher, launcher, manifest_sha256
        self.pair, self.guard = pair, assert_guard
        self.boundary_call = pair.windows.powershell if boundary_powershell is None else boundary_powershell
        self.directory, self.coordinator = Path(journal), Path(coordinator_journal)
        _need(self.directory != self.coordinator)
        self.app = self.carryover = None

    def _private(self, directory):
        if sys.platform == 'win32':
            from .windows_protected_io import WindowsProtectedIO

            WindowsProtectedIO(admin_only=True).assert_directory(directory)
            return
        _need(directory.is_absolute() and directory.resolve(strict=True) == directory)
        info = directory.lstat()
        _need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700)
        for parent in directory.parents:
            info = parent.lstat()
            sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            _need(stat.S_ISDIR(info.st_mode) and info.st_uid in {0, os.geteuid()}
                  and (not info.st_mode & 0o022 or sticky_root))

    def _read(self, name, *, required=True):
        self._private(self.directory)
        try:raw = _private_file(self.directory / name, 16384)
        except FileNotFoundError:
            _need(not required)
            return None
        value = json.loads(raw, object_pairs_hook=wire._pairs)
        _need(raw == wire._bytes(value))
        return value

    def _write(self, name, value):
        self._private(self.directory)
        body = wire._bytes(value);_need(len(body) <= 16384)
        if sys.platform == 'win32':
            from .windows_protected_io import WindowsProtectedIO

            WindowsProtectedIO(admin_only=True).write_new(self.directory / name, body)
            return
        fd = os.open(self.directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:stream.write(body);stream.flush();os.fsync(stream.fileno())
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:os.fsync(fd)
        finally:os.close(fd)

    def _operation(self, operation, phase, deadline):
        self.guard();wire._remaining(deadline)
        _need(type(operation) is dict and set(operation) == {'scope', 'stage', 'previous_sha256', 'nonce', 'plan_material_sha256'}
              and operation['stage'] == phase and wire._hash(operation['nonce'])
              and wire._hash(operation['previous_sha256']))
        scope = _scope(operation['scope'])
        _need(scope.get('saved_reboot') is True and scope['previous_plan_sha256'] == self.pair.config['plan_sha256']
              and scope['previous_instance_sha256'] == self.pair.config['binding_sha256']
              and scope['previous_windows_boot_sha256'] == self.pair.config['boot_sha256']
              and scope['previous_revision'] + 1 == self.closed_revision
              and scope['history_sha256'] == wire._digest(wire._bytes(self.history)))
        self._private(self.coordinator)
        raw = _private_file(self.coordinator / (phase + '.issued.json'), 8192)
        _need(raw == wire._bytes(operation))
        return wire._digest(raw)

    def _guest(self, action, request, deadline, *, expected=None):
        _need(action in {'archive', 'archive_terminal', 'observe', 'authorization'})
        data = {'action': action, 'previous': _scope_request(self.previous), 'request': _scope_request(request),
            'terminal': base64.b64encode(self.terminal).decode(), 'history': self.history,
            'history_sha256': wire._digest(wire._bytes(self.history)), 'previous_instance': self.pair.config['binding_sha256'],
            'closed_revision': self.closed_revision, 'expected': expected}
        if action == 'authorization':
            _need(type(self.app) is SuccessorAppStage)
            data.update(instance=self.app.lease.service_instance_sha256, prior_intent=self.app.prior_intent,
                        prior_progress=self.app.prior_progress)
        return self._exchange_guest(data, _GUEST, b'', deadline)

    def _exchange_guest(self, data, body, payload, deadline):
        code = 'import json\nCFG=' + repr(data) + '\n' + self.launcher.split(MARKER)[0].decode() + body
        _need(len(code.encode()) <= 131072);compile(code, '<saved-site-apps>', 'exec')
        self.guard();wire._remaining(deadline)
        if self.dispatcher.direct_link is not None:
            output = self.dispatcher.exchange_direct_script('A', code, payload, deadline)
            self.guard();wire._remaining(deadline)
            return json.loads(output, object_pairs_hook=wire._pairs)
        stamps, records = {}, {}
        for role, endpoint in (('jump', self.dispatcher.jump), ('A', self.dispatcher.guests['A'])):
            stamps.update(ssh._identity(endpoint, deadline));path = self.dispatcher.known_hosts[role]
            raw, stamps[path] = ssh._checked_file(path, private=False, maximum=1048576)
            records[endpoint.alias] = ssh._host_record(raw, endpoint, guest=role == 'A')
        root = '/private/tmp' if sys.platform == 'darwin' else '/tmp'
        with tempfile.TemporaryDirectory(prefix='ha-saved-site-apps-', dir=root) as folder:
            config = ssh._ssh_config(Path(folder), self.dispatcher.jump, self.dispatcher.guests['A'], records)
            command = (ssh.SSH, '-F', str(config), '-T', self.dispatcher.guests['A'].alias,
                       shlex.join(('/usr/bin/python3', '-I', '-B', '-S', '-c', code)))
            _need(ssh._same_files(stamps))
            result = json.loads(ssh._exchange(command, payload, deadline), object_pairs_hook=wire._pairs)
            _need(ssh._same_files(stamps))
        self.guard();wire._remaining(deadline)
        return result

    def attest_carryover(self, operation, chain, deadline, *, draft, predecessor_plan, observe_only):
        self._operation(operation, 'carryover', deadline)
        scope = operation['scope']
        recovery = chain.get('verify_restored_pair')
        if recovery is None:
            _need(chain.get('replace_provider', {}).get('owned_port') is True)
            closed = chain['close_pair']
            expected_attempt = scope['attempt_sha256']
        else:
            closed = recovery['closed_pair']
            expected_attempt = _fallback_attempt(scope)
        _need(type(observe_only) is bool and type(predecessor_plan) is bytes and 0 < len(predecessor_plan) <= 65536
              and wire._digest(predecessor_plan) == scope['previous_plan_sha256']
              and _closed(closed, self.closed_revision, self.terminal_pin)
              and type(draft) is dict and draft['attempt_id'] == expected_attempt
              and draft['predecessor_terminal_sha256'] == self.terminal_pin)
        stop = self.read_quiesce_request()
        stopped = chain['quiesce_apps']
        _need(wire._digest(wire._bytes(_scope_request(stop))) == stopped['request_sha256']
              and stopped['history_sha256'] == scope['history_sha256']
              and stopped['terminal_sha256'] == self.terminal_pin and stopped['revision'] == self.closed_revision
              and operation['nonce'] not in {stop.nonce, self.previous.nonce})
        args = {key: self.previous.args[key] for key in (*wire.INITIAL_RECOVERY_ARGS, 'hold_sha256')}
        args.update(postboot=draft, postboot_plan_sha256=DRAFT_PIN,
                    postboot_stopped_receipt_sha256=self.previous.args['postboot_stopped_receipt_sha256'])
        request = wire.Request(self.previous.binding, 'A', wire.GUEST_START_INITIAL_POSTBOOT, 1,
                               operation['nonce'], args)
        wire._args(request.verb, request.node, request.args)
        data = {'observe_only': observe_only, 'previous': _scope_request(self.previous), 'stop': _scope_request(stop),
                'draft': _scope_request(request), 'history': self.history, 'stop_receipt': stopped}
        _need(_closed(self.pair.closed_pair(self.terminal_pin, deadline), self.closed_revision, self.terminal_pin))
        result = self._exchange_guest(data, _CARRYOVER_GUEST, predecessor_plan, deadline)
        _need(_closed(self.pair.closed_pair(self.terminal_pin, deadline), self.closed_revision, self.terminal_pin))
        _need(type(result) is dict and result == {'nonce': request.nonce, 'attempt': draft['attempt_id'],
              'carryover_sha256': result.get('carryover_sha256'), 'history_sha256': scope['history_sha256'],
              'stop_sha256': stopped['stop_receipt_sha256']} and wire._hash(result['carryover_sha256']))
        self.guard()
        return result['carryover_sha256']

    def _closed(self, deadline):
        # Cooperative release closes the writer before quiescence. The later
        # provider-retirement phase disables its SCM entry; until then the
        # native boundary permits the same stopped Manual or Disabled mode.
        self.guard();result = self.pair.closed_pair_before_disable(self.terminal_pin, deadline);self.guard()
        _need(_closed(result, self.closed_revision, self.terminal_pin))
        return result

    def read_quiesce_request(self):
        """Read the protected request for the issued app stop."""
        record = self._read('quiesce-issued.json')
        _need(record['automatic_retry'] is False)
        return _request(record['request'])

    def quiesce_operation(self, operation, chain, deadline, *, observe_only):
        operation_pin = self._operation(operation, 'quiesce_apps', deadline)
        _need(type(observe_only) is bool and _closed(chain.get('close_pair'), self.closed_revision, self.terminal_pin))
        args = {key: self.previous.args[key] for key in (*wire.INITIAL_RECOVERY_ARGS, 'hold_sha256')}
        args.update(postboot_attempt_id=operation['scope']['attempt_sha256'],
            predecessor_plan_sha256=operation['scope']['previous_plan_sha256'],
            predecessor_terminal_sha256=self.terminal_pin,
            predecessor_postboot_attempt_id=self.previous.args['postboot']['attempt_id'])
        sequence = getattr(self.dispatcher, 'last_sequence', 0) + 1
        if observe_only:
            sequence = self._read('quiesce-issued.json')['request']['sequence']
        _need(type(sequence) is int and 0 < sequence <= wire.MAX_REQUESTS)
        request = wire.Request(self.dispatcher.binding, 'A', wire.GUEST_QUIESCE_SAMEBOOT_SUCCESSOR, sequence, operation['nonce'], args)
        wire._args(request.verb, request.node, request.args)
        _need(request.nonce != self.previous.nonce)
        issued = {'operation_sha256': operation_pin, 'request': _scope_request(request), 'automatic_retry': False}
        if observe_only:
            _need(self._read('quiesce-issued.json') == issued)
        else:
            self._closed(deadline)
            self._write('quiesce-issued.json', issued)
            archived = self._guest('archive', request, deadline)
            _need(archived == {'terminal_sha256': self.terminal_pin, 'metadata_copies': 2, 'existing_records_preserved': True})
            self._closed(deadline)
        def windows_closed(challenge, boundary, limit):
            _need(not observe_only and boundary in wire.POSTBOOT_STOP_BOUNDARIES and time.monotonic() < limit <= deadline)
            _need(_scope_request(request) == issued['request'] and self._read('quiesce-issued.json') == issued)
            expected = wire._digest(wire._bytes({key: value for key, value in _scope_request(request).items() if key != 'args'} |
                {'args': request.args, 'boundary': boundary, 'closed_revision': self.closed_revision}))
            _need(challenge == expected)
            # Full sealed-runtime custody was checked before handing this
            # callback to the native dispatcher. Recheck every native closed
            # fact in its original bounded channel, without re-enumerating the
            # hundreds of immutable archived runtime files at each boundary.
            source, nonce = self.pair._command('closed_before_disable', self.terminal_pin)
            _need(source.count('if($cfg.verify_runtime){') == 1)
            source = source.replace('if($cfg.verify_runtime){', 'if($false){', 1)
            self.guard();result = self.boundary_call('try{\n' + source + '\n}catch{exit 1}', limit);self.guard()
            _need(time.monotonic() < limit and type(result) is dict
                  and result == {'challenge': nonce, 'status': 'CLOSED_PAIR_VERIFIED', 'terminal_sha256': self.terminal_pin,
                                 'proof_sha256': result.get('proof_sha256'), 'closed_revision': self.closed_revision}
                  and wire._hash(result['proof_sha256']))
            return wire._digest(wire._bytes({'challenge': challenge, 'boundary': boundary,
                'closed_pair_sha256': result['proof_sha256'], 'terminal_sha256': self.terminal_pin, 'revision': self.closed_revision}))
        def observe_stopped(item, expected, limit):
            _need(_scope_request(item) == issued['request'] and self._read('quiesce-issued.json') == issued)
            self._closed(limit)
            result = self._guest('observe', item, limit, expected=expected)
            self._closed(limit)
            _need(type(result) is dict and set(result) == {'stop_receipt_sha256','request_sha256','terminal_sha256',
                  'history_sha256','revision','stopped','closed_proof_sha256'}
                  and result.get('request_sha256') == wire._digest(wire._bytes(issued['request']))
                  and result.get('history_sha256') == operation['scope']['history_sha256']
                  and result.get('terminal_sha256') == self.terminal_pin and result.get('revision') == self.closed_revision
                  and result.get('stopped') == list(wire.RECOVERY_START_ORDER)
                  and wire._hash(result.get('stop_receipt_sha256')) and wire._hash(result.get('closed_proof_sha256'))
                  and (expected is None or result['stop_receipt_sha256'] == expected))
            return result
        return {'dispatcher': self.dispatcher, 'request': request, 'assert_windows_closed': windows_closed,
                'observe_stopped': observe_stopped}

    def bind_successor(self, stage, carryover_raw):
        """Called by apps_factory after the real publication has been bound."""
        from .native_guest_postboot import CARRYOVER_FIELDS, CARRYOVER_KIND
        _need(type(stage) is SuccessorAppStage and stage.dispatcher.binding == self.dispatcher.binding
              and stage.dispatcher.jump == self.dispatcher.jump and stage.dispatcher.guests == self.dispatcher.guests
              and stage.dispatcher.known_hosts == self.dispatcher.known_hosts
              and stage.dispatcher.launcher_sha256 == self.dispatcher.launcher_sha256
              and stage.launcher == self.launcher and stage.manifest == self.manifest
              and type(carryover_raw) is bytes and wire._digest(carryover_raw) == stage.postboot['carryover_receipt_sha256'])
        carryover = json.loads(carryover_raw, object_pairs_hook=wire._pairs)
        previous = _scope_request(self.previous)
        _need(type(carryover) is dict and set(carryover) == CARRYOVER_FIELDS
              and carryover['kind'] == CARRYOVER_KIND and carryover['binding'] == self.dispatcher.binding.fields()
              and carryover['postboot'] == {key: value for key, value in stage.postboot.items()
                                            if key not in {'carryover_receipt_sha256','predecessor_seal_sha256'}}
              and carryover['recovery_args'] == {key: self.previous.args[key] for key in (*wire.INITIAL_RECOVERY_ARGS,'hold_sha256')}
              and carryover['history'] == self.history and carryover['predecessor_request'] == {
            'scope': {key: value for key, value in previous.items() if key != 'args'}, 'args': previous['args']})
        self.app, self.carryover = stage, carryover_raw
        return stage

    def _app_bound(self, operation, chain, deadline):
        operation_pin = self._operation(operation, 'start_apps', deadline)
        _need(type(self.app) is SuccessorAppStage and type(self.carryover) is bytes
              and chain['publish']['instance_sha256'] == self.app.lease.service_instance_sha256
              and chain['publish']['plan_sha256'] == chain['arm_host']['plan_sha256'] == self.app.proof_inputs.plan_sha256
              and chain['publish']['revision'] == self.app.open_revision)
        sameboot = _sameboot_recovery(chain) is not None
        expected_attempt = _fallback_attempt(operation['scope']) if sameboot else operation['scope']['attempt_sha256']
        carryover = chain.get('fallback_carryover', chain['carryover'])
        _need(self.app.postboot['attempt_id'] == expected_attempt
              and self.app.postboot['carryover_receipt_sha256'] == carryover['carryover_sha256'])
        return operation_pin

    def _authorization(self, value, request):
        fields = {'schema_version','authorization_sha256','request_nonce','baseline_sha256','prior_intent_sha256',
                  'prior_progress_sha256','service_instance_sha256','container_count','containers_started','database_changed'}
        _need(type(value) is dict and set(value) == fields and type(value['schema_version']) is int and value['schema_version'] == 1
              and value['request_nonce'] == request.nonce and value['service_instance_sha256'] == self.app.lease.service_instance_sha256
              and value['prior_intent_sha256'] == self.app.prior_intent and value['prior_progress_sha256'] == self.app.prior_progress
              and value['container_count'] == 8 and type(value['container_count']) is int
              and value['containers_started'] is False and value['database_changed'] is False
              and wire._hash(value['authorization_sha256']) and wire._hash(value['baseline_sha256']))
        return value

    def app_operation(self, operation, chain, deadline):
        operation_pin = self._app_bound(operation, chain, deadline)
        _need(all(self._read(name, required=False) is None for name in ('apps-issued.json','apps-authorization.json','apps-started.json')))
        prior_stop = self._read('quiesce-issued.json', required=False)
        last = getattr(self.app.dispatcher, 'last_sequence', 0)
        _need(type(last) is int and last >= 0 and operation['nonce'] != self.previous.nonce)
        if prior_stop is not None:
            stop = _request(prior_stop['request'])
            _need(operation['nonce'] != stop.nonce)
            last = max(last, stop.sequence)
        _need(last < wire.MAX_REQUESTS)
        request = self.app.make_request(operation['nonce'], self.carryover, sequence=last + 1)
        issued = {'operation_sha256': operation_pin, 'request': _scope_request(request),
                  'plan_sha256': self.app.proof_inputs.plan_sha256, 'carryover_sha256': wire._digest(self.carryover),
                  'instance_sha256': self.app.lease.service_instance_sha256, 'automatic_retry': False}
        def reserve(item):
            _need(_scope_request(item) == issued['request'])
            self.guard();self._write('apps-issued.json', issued);self.guard()
        def record(kind, value):
            _need(self._read('apps-issued.json') == issued and kind in {'authorization','started'})
            if kind == 'authorization':
                self._authorization(value, request)
            else:
                saved = self._read('apps-authorization.json')
                _need(type(saved) is dict and set(saved) == {'issued_sha256','value'}
                      and saved['issued_sha256'] == wire._digest(wire._bytes(issued)))
                authorization = self._authorization(saved['value'], request)
                _need(type(value) is dict and set(value) == {'receipt_sha256','authorization_sha256','closed_revision','open_revision'}
                      and wire._hash(value['receipt_sha256']) and value['authorization_sha256'] == authorization['authorization_sha256']
                      and value['closed_revision'] == self.app.closed_revision and value['open_revision'] == self.app.open_revision)
            self.guard();self._write('apps-' + kind + '.json', {'issued_sha256': wire._digest(wire._bytes(issued)), 'value': value});self.guard()
        return {'request': request, 'assert_guard': self.guard, 'issued': reserve, 'record': record}

    def read_app_operation(self, operation, chain, deadline):
        operation_pin = self._app_bound(operation, chain, deadline)
        issued = self._read('apps-issued.json')
        _need(type(issued) is dict and set(issued) == {'operation_sha256','request','plan_sha256','carryover_sha256','instance_sha256','automatic_retry'}
              and issued['operation_sha256'] == operation_pin and issued['automatic_retry'] is False
              and issued['plan_sha256'] == self.app.proof_inputs.plan_sha256
              and issued['carryover_sha256'] == wire._digest(self.carryover)
              and issued['instance_sha256'] == self.app.lease.service_instance_sha256)
        request = _request(issued['request'])
        carryover = json.loads(self.carryover, object_pairs_hook=wire._pairs)
        expected_args = carryover['recovery_args'] | {'postboot': self.app.postboot,
            'postboot_plan_sha256': self.app.proof_inputs.plan_sha256,
            'postboot_stopped_receipt_sha256': self.app.postboot['stopped_receipt_sha256']}
        _need(request.nonce == operation['nonce'] and request.binding == self.dispatcher.binding
              and request.node == 'A' and request.verb == wire.GUEST_START_INITIAL_POSTBOOT and request.args == expected_args)
        authorization_record = self._read('apps-authorization.json', required=False)
        if authorization_record is None:
            authorization = self._guest('authorization', request, deadline)
        else:
            _need(type(authorization_record) is dict and set(authorization_record) == {'issued_sha256','value'}
                  and authorization_record['issued_sha256'] == wire._digest(wire._bytes(issued)))
            authorization = authorization_record['value']
        self._authorization(authorization, request)
        started = self._read('apps-started.json', required=False)
        expected = None
        if started is not None:
            _need(type(started) is dict and set(started) == {'issued_sha256','value'}
                  and type(started['value']) is dict and set(started['value']) == {'receipt_sha256','authorization_sha256','closed_revision','open_revision'}
                  and started['issued_sha256'] == wire._digest(wire._bytes(issued))
                  and started['value']['authorization_sha256'] == authorization['authorization_sha256']
                  and started['value']['closed_revision'] == self.app.closed_revision
                  and started['value']['open_revision'] == self.app.open_revision)
            expected = started['value']['receipt_sha256'];_need(wire._hash(expected))
        self.guard();wire._remaining(deadline)
        return {'request': request, 'authorization': authorization, 'assert_guard': self.guard,
                'prior_resume': None, 'expected_receipt_sha256': expected}
