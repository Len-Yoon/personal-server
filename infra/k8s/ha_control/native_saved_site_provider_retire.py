"""Host-only, one-shot retirement bridge for NativeSamebootRelease.

Uses the already pinned guest package's native retirement functions. No guest
package extension, provider replacement, Windows mutation or automatic retry.
"""

import json
from pathlib import Path
import shlex
import sys
import tempfile
import threading

from . import native_guest_ssh as ssh, native_stdio_bridge as wire
from .controller import TransitionBlocked
from .native_saved_site_apps import MARKER, _scope_request, _request
from .native_saved_site_provider_transport import ProviderArchiveTransport
from .sameboot_transition import _encoded, _scope, _validate

ERROR = 'saved_provider_retire_unconfirmed'
BOUNDARIES = {'retire': ('before','dispatch','after','after','after'),
              'observe': ('after','after'),
              'finish': ('after','after','before','after','after','after')}


def _need(value):
    if not value:
        raise TransitionBlocked(ERROR)


_GUEST = r'''
import json,os,time
from ha_control import native_guest_worker as w,native_stdio_bridge as wire,native_guest_postboot as post
from ha_control import native_legacy_provider as native
from ha_control.native_saved_site_apps import _request
from ha_control.native_saved_site_provider_archive import ProviderRollbackArchive
from ha_control.native_sameboot_release import retire_guest,observe_guest_retired
channel=wire.Stdio(sys.stdin.fileno(),sys.stdout.fileno())
limit=time.monotonic()+CFG['seconds']
assert channel.read(limit)==CFG['envelope']
options=CFG['archive'].copy();options['previous_request']=_request(options['previous_request'])
options['expected_apps_running']=False
inspector=ProviderRollbackArchive(**options)
q=_request(CFG['quiesce_request']);qr=CFG['quiesce_receipt']
assert q.binding==options['previous_request'].binding
assert q.verb==wire.GUEST_QUIESCE_SAMEBOOT_SUCCESSOR
assert wire._digest(wire._bytes(w._scope(q)|{'args':q.args}))==qr['request_sha256']
adapter=native.ProviderRetirement(inspector.stage,q.binding,latest_intent_sha256=options['history']['intent_sha256'],
 rollback_sha256=CFG['rollback_sha256'],hold_sha256=options['hold_sha256'],
 provider_port=options['provider_port'],held_port=options['held_port'])
def local(boundary):
 inspector._context()
 stop=post.observe(q,q.args['postboot_attempt_id'],qr['stop_receipt_sha256'])
 ip,_=post.paths(q,q.args['postboot_attempt_id']);ir,intent=w._read_json(ip)
 proof=intent.get('closed_proof_sha256')
 assert wire._hash(proof) and intent=={'schema_version':1,**w._scope(q),'args':q.args,
   'closed_proof_sha256':proof,'containers':stop['containers'],'automatic_retry_permitted':False}
 assert w._read_json(ip)[0]==ir and stop['args']==q.args
 assert stop['closed_revision']==qr['revision'] and stop['closed_proof_sha256']==qr['closed_proof_sha256']
 units=adapter._archive()
 for role in ('provider','held'):
  assert units[role]['unit']==options[role+'_unit']
  assert all(units[role][key]==options['expected_'+role][key] for key in ('fragment_sha256','argv_sha256'))
 assert adapter._live(units['held'],'held')==options['expected_held']
 assert not any(os.path.lexists(p) for p in adapter._paths('restore'))
 if CFG['mode']=='retire' and boundary in ('before','dispatch'):
  assert adapter._live(units['provider'],'provider')==options['expected_provider']
 else:adapter._absent(units['provider'])
def safe(challenge,boundary):
 local(boundary)
 deadline=min(limit,time.monotonic()+60)
 fields={'request_nonce':CFG['envelope']['operation_nonce'],'challenge':challenge,'boundary':boundary}
 channel.write({'kind':'activation_boundary',**fields},deadline)
 reply=channel.read(deadline)
 assert wire._matches(reply,{'kind':'activation_boundary_current',**fields,
                           'windows_proof_sha256':reply.get('windows_proof_sha256')})
 assert wire._hash(reply['windows_proof_sha256'])
 local(boundary)
 return reply['windows_proof_sha256']
def issued():
 ip,_=adapter._paths('retire')
 raw=w.runtime._read(ip,0,{0o400},limit=131072)
 prior=json.loads(raw,object_pairs_hook=wire._pairs)
 assert prior['initial']==options['expected_provider'] and prior['held']==options['expected_held']
 return raw
try:
 with w._guest_mutation_descriptor(wait=2):
  if CFG['mode']=='retire':
   assert not any(os.path.lexists(p) for p in adapter._paths('retire'))
   result=retire_guest(adapter,safe)
  elif CFG['mode']=='finish':
   ip,done=adapter._paths('retire')
   prior=issued()
   assert not os.path.lexists(done)
   observe_guest_retired(adapter,safe)
   assert w.runtime._read(ip,0,{0o400},limit=131072)==prior
   # The native implementation skips systemctl when this exact intent exists.
   adapter.retire(safe)
   result=observe_guest_retired(adapter,safe)
   assert w.runtime._read(ip,0,{0o400},limit=131072)==prior
  else:
   issued()
   result=observe_guest_retired(adapter,safe)
  issued()
  assert adapter._receipt('retire')==result['retirement_sha256']
 channel.write(CFG['envelope']|{'status':'OBSERVED','result':result},limit)
except Exception:
 channel.write(CFG['envelope']|{'status':'UNKNOWN'},limit)
'''


class SavedSiteProviderRetire:
    """guest.retire/observe_retired protocol, with real closure at each boundary.

    archive_transport supplies pinned launcher/SSH identities and the actual
    Windows closed-pair callback. custody_guard has NativeSamebootRelease's
    signature and must independently recheck the current stopped source scope.
    Caller owns durable coordinator issuance. Guest native intent prevents
    redispatch across host restarts; an unknown result permits observe only.
    """

    def __init__(self, *, archive_transport, scope, archive_sha256,
                 quiesce_request, custody_guard):
        _need(type(archive_transport) is ProviderArchiveTransport and wire._hash(archive_sha256)
              and callable(custody_guard))
        self.scope = _scope(scope)
        opts = archive_transport.options
        _need(self.scope.get('saved_reboot') is True
              and self.scope['previous_plan_sha256']==opts['previous_request']['args']['postboot_plan_sha256']
              and self.scope['previous_instance_sha256']==opts['previous_instance_sha256']
              and self.scope['previous_revision']==opts['previous_revision']
              and self.scope['previous_windows_boot_sha256']==opts['windows_boot_sha256']
              and self.scope['history_sha256']==wire._digest(wire._bytes(opts['history']))
              and (type(quiesce_request) is wire.Request or callable(quiesce_request)))
        self.read_quiesce = quiesce_request
        self.transport, self.rollback, self.custody = archive_transport, archive_sha256, custody_guard
        self.lock, self.issued = threading.Lock(), False

    @property
    def quiesce(self):
        request = self.read_quiesce() if callable(self.read_quiesce) else self.read_quiesce
        _need(type(request) is wire.Request and request.node == 'A'
              and request.verb == wire.GUEST_QUIESCE_SAMEBOOT_SUCCESSOR
              and request.binding == self.transport.dispatcher.binding)
        return _scope_request(_request(_scope_request(request)))

    def _parse(self, request_raw, receipts_raw):
        request=json.loads(request_raw,object_pairs_hook=wire._pairs)
        chain=json.loads(receipts_raw,object_pairs_hook=wire._pairs)
        _need(type(request) is dict and set(request)=={'scope','stage','previous_sha256','nonce','plan_material_sha256'}
              and request['scope']==self.scope and request['stage'] in {'retire_provider','close_pair'}
              and request['plan_material_sha256'] is None and wire._hash(request['nonce'])
              and wire._hash(request['previous_sha256']) and request_raw==_encoded(request)
              and type(chain) is dict and receipts_raw==_encoded(chain))
        for phase in ('preflight','release_writer','close_pair','quiesce_apps'):
            _validate(phase,chain.get(phase),self.scope,chain)
        _need(chain['preflight']['rollback_sha256']==self.rollback
              and chain['release_writer']['terminal_sha256']==self.transport.options['predecessor_terminal_sha256']
              and chain['quiesce_apps']['request_sha256']==wire._digest(wire._bytes(self.quiesce)))
        return request,chain

    def _run(self, mode, request_raw, receipts_raw, deadline):
        request,chain=self._parse(request_raw,receipts_raw)
        _need(mode in BOUNDARIES and (mode=='observe' or request['stage']=='retire_provider'))
        with self.lock:
            if mode=='retire':
                _need(not self.issued)
                self.issued=True
            _need(self.custody(request_raw,receipts_raw,deadline,observe_only=mode!='retire') is True)
            seconds=wire._remaining(deadline);_need(seconds<=600)
            envelope={'mode':mode,'operation_nonce':request['nonce'],'operation_sha256':wire._digest(request_raw),
                      'receipts_sha256':wire._digest(receipts_raw),'rollback_sha256':self.rollback}
            cfg={'mode':mode,'archive':self.transport.options,'rollback_sha256':self.rollback,
                 'quiesce_request':self.quiesce,'quiesce_receipt':chain['quiesce_apps'],
                 'envelope':envelope,'seconds':seconds}
            code=self.transport.launcher.split(MARKER)[0].decode()+'\nimport sys,json\nCFG=json.loads('+repr(wire._bytes(cfg).decode())+')\n'+_GUEST
            _need(len(code.encode())<=131072);compile(code,'<saved-provider-retire>','exec')
            stamps,records={},{}
            dispatcher=self.transport.dispatcher
            for role,endpoint in (('jump',dispatcher.jump),('A',dispatcher.guests['A'])):
                stamps.update(ssh._identity(endpoint,deadline));path=dispatcher.known_hosts[role]
                raw,stamps[path]=ssh._checked_file(path,private=False,maximum=1048576)
                records[endpoint.alias]=ssh._host_record(raw,endpoint,guest=role=='A')
            def fresh(challenge,boundary,limit):
                _need(ssh._same_files(stamps)
                      and self.custody(request_raw,receipts_raw,limit,observe_only=True) is True)
                facts=self.transport._closed(challenge,boundary,limit)
                _need(ssh._same_files(stamps)
                      and self.custody(request_raw,receipts_raw,limit,observe_only=True) is True)
                return wire._digest(wire._bytes(envelope|{'challenge':challenge,'boundary':boundary,'closed_pair':facts}))
            root='/private/tmp' if sys.platform=='darwin' else '/tmp'
            with tempfile.TemporaryDirectory(prefix='ha-provider-retire-',dir=root) as folder:
                config=ssh._ssh_config(Path(folder),dispatcher.jump,dispatcher.guests['A'],records)
                command=(ssh.SSH,'-F',str(config),'-T',dispatcher.guests['A'].alias,
                         shlex.join(('/usr/bin/python3','-I','-B','-S','-c',code)))
                _need(ssh._same_files(stamps));wire._remaining(deadline)
                raw=ssh._activation_exchange(command,wire._bytes(envelope),deadline,request['nonce'],fresh,
                    bind_boundary=True,boundaries=BOUNDARIES[mode],boundary_seconds=60)
                _need(ssh._same_files(stamps))
            frame=json.loads(raw,object_pairs_hook=wire._pairs)
            _need(type(frame) is dict and all(frame.get(k)==v for k,v in envelope.items())
                  and set(frame)==set(envelope)|{'status','result'} and frame['status']=='OBSERVED')
            result=frame['result']
            _need(type(result) is dict and set(result)=={'retirement_sha256','held_observation_sha256','rollback_sha256'}
                  and all(wire._hash(v) for v in result.values()) and result['rollback_sha256']==self.rollback
                  and result['held_observation_sha256']==wire._digest(wire._bytes(self.transport.options['expected_held'])))
            _need(self.custody(request_raw,receipts_raw,deadline,observe_only=True) is True)
            wire._remaining(deadline)
            return result

    def retire(self, request_raw, receipts_raw, deadline):
        return self._run('retire',request_raw,receipts_raw,deadline)

    def observe_retired(self, request_raw, receipts_raw, deadline):
        return self._run('observe',request_raw,receipts_raw,deadline)

    def finish_retirement(self, request_raw, receipts_raw, deadline):
        """Create a missing receipt after native checks confirm the issued stop finished."""
        return self._run('finish',request_raw,receipts_raw,deadline)
