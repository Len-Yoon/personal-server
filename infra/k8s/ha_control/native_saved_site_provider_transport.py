"""Pinned SSH archive transport with full fresh native closure facts in duplex.

Only explicit measure/create/observe connects. No provider retirement or replacement is
exposed. The caller owns durable operation issuance and the real Windows query.
"""

import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time

from . import native_guest_ssh as ssh, native_stdio_bridge as wire
from .controller import TransitionBlocked
from .native_saved_site_apps import MARKER, _scope_request, _request

ERROR = 'saved_provider_transport_unconfirmed'
ARCHIVE_BOUNDARY_SECONDS = 60
BOUNDARIES = {'measure': ('before', 'after'), 'create': ('before', 'dispatch', 'after'),
              'observe': ('before', 'after')}


def _need(value):
    if not value:
        raise TransitionBlocked(ERROR)


_GUEST = r'''
from ha_control import native_stdio_bridge as wire
from ha_control.native_saved_site_apps import _request
from ha_control.native_saved_site_provider_archive import ProviderRollbackArchive
import time
channel=wire.Stdio(sys.stdin.fileno(),sys.stdout.fileno())
deadline=time.monotonic()+CFG['seconds']
message=channel.read(deadline)
assert message=={'mode':CFG['mode'],'operation_nonce':CFG['archive']['operation_nonce'],
                 'config_sha256':CFG['config_sha256'],'expected_sha256':CFG['expected_sha256']}
options=CFG['archive'].copy()
options['previous_request']=_request(options['previous_request'])
adapter=ProviderRollbackArchive(**options)
def closed(challenge,boundary,limit):
    fields={'request_nonce':options['operation_nonce'],'challenge':challenge,'boundary':boundary}
    channel.write({'kind':'activation_boundary',**fields},min(limit,deadline))
    reply=channel.read(min(limit,deadline))
    assert type(reply) is dict and set(reply)=={'kind',*fields,'closed_pair'}
    assert reply['kind']=='activation_boundary_current' and all(reply[k]==v for k,v in fields.items())
    return reply['closed_pair']
if CFG['mode']=='measure':result=adapter.measure(closed,deadline)
elif CFG['mode']=='create':result=adapter.create(closed,deadline,expected_sha256=CFG['expected_sha256'])
else:result=adapter.observe(closed,deadline,expected_sha256=CFG['expected_sha256'])
channel.write({'kind':'archive_result','mode':CFG['mode'],'config_sha256':CFG['config_sha256'],
               'operation_nonce':options['operation_nonce'],'result':result},deadline)
'''


class ProviderArchiveTransport:
    """Reuse a pinned dispatcher identity and package; never alter its command.

    archive_options are ProviderRollbackArchive keyword arguments, with the
    previous_request as a Request. assert_closed(challenge,boundary,deadline)
    returns the actual WindowsPairClosure result plus challenge/windows_boot_sha256.
    The staged source manifest must include the exact local archive module.
    """

    def __init__(self, *, dispatcher, launcher, source_manifest, manifest_sha256,
                 archive_options, assert_guard, assert_closed):
        _need(type(dispatcher) is ssh.GuestSshDispatcher and type(launcher) is bytes
              and wire._digest(launcher) == dispatcher.launcher_sha256
              and launcher.count(MARKER) == 1 and wire._hash(manifest_sha256)
              and manifest_sha256.encode() in launcher and type(source_manifest) is bytes
              and wire._digest(source_manifest) == manifest_sha256
              and callable(assert_guard) and callable(assert_closed))
        manifest = json.loads(source_manifest, object_pairs_hook=wire._pairs)
        name = 'ha_control/native_saved_site_provider_archive.py'
        _need(type(manifest) is dict and manifest.get(name) == wire._digest(
            Path(__file__).with_name('native_saved_site_provider_archive.py').read_bytes()))
        options = dict(archive_options)
        previous = options['previous_request']
        _need(previous.binding == dispatcher.binding and previous.node == 'A')
        options['previous_request'] = _scope_request(previous)
        # Validate the complete local configuration without touching a guest.
        from .native_saved_site_provider_archive import ProviderRollbackArchive
        ProviderRollbackArchive(**(options | {'previous_request': _request(options['previous_request'])}))
        self.dispatcher, self.launcher = dispatcher, launcher
        self.options = json.loads(wire._bytes(options))
        self.config_pin = wire._digest(wire._bytes(self.options))
        self.guard, self.closed = assert_guard, assert_closed
        self.lock = threading.Lock()
        self.create_issued = False

    def _closed(self, challenge, boundary, deadline):
        self.guard()
        wire._remaining(deadline)
        result = self.closed(challenge, boundary, deadline)
        _need(type(result) is dict and set(result) == {
            'closed_nodes','synchronous','same_expiry','replay_verified','effects_isolated',
            'revision','terminal_sha256','closed_proof_sha256','challenge','windows_boot_sha256'}
            and result['challenge'] == challenge
            and result['windows_boot_sha256'] == self.options['windows_boot_sha256']
            and result['terminal_sha256'] == self.options['predecessor_terminal_sha256']
            and type(result['revision']) is int and result['revision'] == self.options['previous_revision'] + 1
            and result['closed_nodes'] == ['A','B'] and wire._hash(result['closed_proof_sha256'])
            and all(result[key] is True for key in ('synchronous','same_expiry','replay_verified','effects_isolated')))
        self.guard()
        wire._remaining(deadline)
        return result

    def _exchange(self, command, payload, deadline, mode, stamps):
        # Specialize native_guest_ssh._activation_exchange only to carry the
        # actual closure dictionary. A digest cannot encode those observations.
        wire._remaining(deadline)
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env={'PATH':'/usr/bin:/bin','LC_ALL':'C'}, start_new_session=True)
        complete = False
        try:
            channel = wire.Stdio(process.stdout.fileno(), process.stdin.fileno())
            channel.write(payload, deadline)
            seen = set()
            proofs = {}
            while True:
                frame = channel.read(deadline)
                if frame.get('kind') != 'activation_boundary':
                    _need(len(seen) == len(BOUNDARIES[mode]) and not channel.buffer
                          and process.wait(timeout=wire._remaining(deadline)) == 0
                          and not os.read(process.stdout.fileno(),1))
                    complete = True
                    return frame, proofs
                challenge, boundary = frame.get('challenge'), frame.get('boundary')
                fields = {'request_nonce':self.options['operation_nonce'],'challenge':challenge,'boundary':boundary}
                _need(wire._matches(frame, {'kind':'activation_boundary',**fields})
                      and wire._hash(challenge) and challenge not in seen
                      and len(seen) < len(BOUNDARIES[mode]) and boundary == BOUNDARIES[mode][len(seen)]
                      and ssh._same_files(stamps))
                seen.add(challenge)
                # Archive guards verify the full Windows runtime and both
                # closed DBs. No live writer lease is renewed here.
                limit = min(deadline, time.monotonic() + ARCHIVE_BOUNDARY_SECONDS)
                facts = self._closed(challenge, boundary, limit)
                if boundary in {'before','after'}:
                    proofs[boundary] = facts['closed_proof_sha256']
                _need(ssh._same_files(stamps))
                channel.write({'kind':'activation_boundary_current',**fields,'closed_pair':facts},limit)
        finally:
            if not complete:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                try:process.wait(timeout=.2)
                except subprocess.TimeoutExpired:pass
            process.stdin.close()
            process.stdout.close()

    def _run(self, mode, deadline, expected):
        _need(mode in BOUNDARIES and ((mode == 'measure' and expected is None)
                                     or (mode != 'measure' and wire._hash(expected))))
        with self.lock:
            if mode == 'create':
                _need(not self.create_issued)
                self.create_issued = True  # A lost reply is observed, never reissued.
            self.guard()
            remaining = wire._remaining(deadline)
            _need(remaining <= 600)
            cfg = {'mode':mode,'archive':self.options,'config_sha256':self.config_pin,
                   'expected_sha256':expected,'seconds':remaining}
            code = self.launcher.split(MARKER)[0].decode() + '\nimport json,sys\nCFG=json.loads(' + repr(wire._bytes(cfg).decode()) + ')\n' + _GUEST
            _need(len(code.encode()) <= 131072)
            compile(code,'<saved-provider-archive>','exec')
            stamps, records = {}, {}
            for role, endpoint in (('jump',self.dispatcher.jump),('A',self.dispatcher.guests['A'])):
                stamps.update(ssh._identity(endpoint,deadline))
                path=self.dispatcher.known_hosts[role]
                raw,stamps[path]=ssh._checked_file(path,private=False,maximum=1048576)
                records[endpoint.alias]=ssh._host_record(raw,endpoint,guest=role=='A')
            root='/private/tmp' if sys.platform=='darwin' else '/tmp'
            with tempfile.TemporaryDirectory(prefix='ha-provider-archive-',dir=root) as folder:
                config=ssh._ssh_config(Path(folder),self.dispatcher.jump,self.dispatcher.guests['A'],records)
                command=(ssh.SSH,'-F',str(config),'-T',self.dispatcher.guests['A'].alias,
                         shlex.join(('/usr/bin/python3','-I','-B','-S','-c',code)))
                _need(ssh._same_files(stamps))
                payload={'mode':mode,'operation_nonce':self.options['operation_nonce'],
                         'config_sha256':self.config_pin,'expected_sha256':expected}
                frame,proofs=self._exchange(command,payload,deadline,mode,stamps)
                _need(ssh._same_files(stamps))
            self.guard()
            wire._remaining(deadline)
            _need(type(frame) is dict and set(frame)=={'kind','mode','config_sha256','operation_nonce','result'}
                  and frame['kind']=='archive_result' and frame['mode']==mode
                  and frame['config_sha256']==self.config_pin and frame['operation_nonce']==self.options['operation_nonce'])
            result=frame['result']
            if mode == 'measure':
                _need(type(result) is dict and set(result)=={'archive_sha256'}
                      and wire._hash(result['archive_sha256']))
                return result
            _need(type(result) is dict and set(result)=={'archive_sha256','history_sha256','inventory_sha256',
                  'apps_running','latest_intent_sha256','terminal_sha256','closed_revision','observers',
                  'proofs','created','provider_mutated'} and wire._hash(result.get('archive_sha256'))
                  and result.get('created') is (mode=='create') and result.get('provider_mutated') is False
                  and result.get('history_sha256')==wire._digest(wire._bytes(self.options['history']))
                  and result['latest_intent_sha256']==self.options['history']['intent_sha256']
                  and wire._hash(result['inventory_sha256'])
                  and result['apps_running'] is self.options['expected_apps_running']
                  and result['proofs']==proofs
                  and result.get('terminal_sha256')==self.options['predecessor_terminal_sha256']
                  and type(result.get('closed_revision')) is int
                  and result['closed_revision']==self.options['previous_revision']+1
                  and (expected is None or result['archive_sha256']==expected))
            _need(type(result['observers']) is dict and set(result['observers'])=={'provider','held'})
            for role,observed in result['observers'].items():
                wanted=self.options['expected_'+role]
                _need(type(observed) is dict and set(observed)==set(wanted)|{'start_ticks'}
                      and all(observed[key]==value for key,value in wanted.items())
                      and type(observed['pid']) is int and type(observed['start_ticks']) is str
                      and observed['start_ticks'].isdecimal())
            return result

    def measure(self, deadline):
        """Read only; durably retain this SHA before issuing create."""
        return self._run('measure',deadline,None)

    def create(self, deadline, *, expected_sha256):
        return self._run('create',deadline,expected_sha256)

    def observe(self, deadline, *, expected_sha256):
        return self._run('observe',deadline,expected_sha256)
