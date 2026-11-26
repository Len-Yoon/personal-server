"""Host-only authenticated provider replacement and rollback.

Uses the staged native ProviderReplacement. A completed durable native retire
receipt is mandatory. Actual Windows closure and native provider readiness are
separate callbacks; neither can be replaced with a fixed successful receipt.
"""

import json
from pathlib import Path
import shlex
import sys
import tempfile
import threading

from . import native_guest_ssh as ssh, native_stdio_bridge as wire
from .controller import TransitionBlocked
from .native_saved_site_apps import MARKER
from .native_saved_site_provider_retire import SavedSiteProviderRetire, _GUEST as RETIRE_GUEST
from .sameboot_transition import _encoded, _validate

ERROR = 'saved_provider_replace_unconfirmed'
BOUNDARIES = {'start':('before','dispatch','before','provider_ready','after'),
               'observe':('before','provider_ready','after'),
              'failure':('before','after'),
              'restore':(('before','dispatch','before','dispatch','after','after','original_ready'),
                         ('before','before','dispatch','after','after','original_ready'),
                         ('before','before','after','after','original_ready')),
              'observe_restore':('before','after','original_ready')}
RESTORE_MODES = {'restore', 'observe_restore'}


def _need(value):
    if not value:
        raise TransitionBlocked(ERROR)


def _guest_source():
    # Reuse precisely the retirement bridge's real history/quiesce/archive
    # readback. Only the original provider's expected state changes to absent.
    marker = 'def safe(challenge,boundary):'
    gate = " if CFG['mode']=='retire' and boundary in ('before','dispatch'):\n  assert adapter._live(units['provider'],'provider')==options['expected_provider']\n else:adapter._absent(units['provider'])"
    absence = " assert not any(os.path.lexists(p) for p in adapter._paths('restore'))\n"
    _need(RETIRE_GUEST.count(marker)==1 and RETIRE_GUEST.count(absence+gate)==1)
    base=RETIRE_GUEST.split(marker)[0].replace(absence+gate,
        " if CFG['mode'] not in ('restore','observe_restore'):\n " + absence + "  replacement._old_absent(units)")
    return base + r'''
from ha_control.native_saved_site_provider import ProviderReplacement
assert adapter._receipt('retire')==CFG['retirement_sha256']
replacement=ProviderReplacement(adapter,operation_nonce=CFG['replacement_nonce'],
 source_path=CFG['source_path'],source_sha256=CFG['source_sha256'],
 retirement_sha256=CFG['retirement_sha256'],original_unit=options['provider_unit'],
 expected_provider=options['expected_provider'],expected_held=options['expected_held'])
def safe(challenge,boundary,deadline):
 local(boundary)
 deadline=min(limit,deadline,time.monotonic()+60)
 fields={'request_nonce':CFG['envelope']['operation_nonce'],'challenge':challenge,'boundary':boundary}
 channel.write({'kind':'activation_boundary',**fields},deadline)
 reply=channel.read(deadline)
 assert wire._matches(reply,{'kind':'activation_boundary_current',**fields,
                           'windows_proof_sha256':reply.get('windows_proof_sha256')})
 assert wire._hash(reply['windows_proof_sha256'])
 local(boundary)
 return reply['windows_proof_sha256']
def failed():
 units,entry,_,expected=replacement._material()
 replacement._held(units)
 replacement._old_absent(units)
 assert not os.path.lexists(replacement.stop_intent) and replacement._restore_intent() is None
 launch=replacement._launch_pin(expected)
 if native._properties(replacement.unit)['ActiveState']=='active':
  assert launch is not None
  current=replacement._own_live(entry,ready=False)
  assert adapter.ports['provider'] not in native._listeners()
  return current
 replacement._own_absent()
 return None
def restored():
 units,_,_,expected=replacement._material()
 replacement._stop_record(expected)
 _,live=replacement._restore_state(units)
 _,done=adapter._paths('restore')
 native_receipt=adapter._receipt('restore')
 record=json.loads(w.runtime._read(done,0,{0o400},limit=131072),object_pairs_hook=wire._pairs)
 assert live is not None and record['provider']==live and record['held']==options['expected_held']
 return {'restore_receipt_sha256':native_receipt,'provider':live,
         'replacement_unit':replacement.unit,'held':record['held']}
try:
 mode=CFG['mode']
 if mode=='failure':
  with w._guest_mutation_descriptor(wait=2):
   initial=failed()
   replacement._gate('failure','before',safe,limit)
   assert failed()==initial
   replacement._gate('failure','after',safe,limit)
   assert failed()==initial
  result={'replacement_failed':True}
 elif mode in ('restore','observe_restore'):
  if mode=='restore':replacement.rollback(safe,limit)
  else:replacement.observe_restored(safe,limit)
  with w._guest_mutation_descriptor(wait=2):
   result=restored()
   replacement._gate(mode,'original_ready',safe,limit)
   assert restored()==result
 else:
  result=(replacement.start if mode=='start' else replacement.observe)(safe,limit)
 channel.write(CFG['envelope']|{'status':'OBSERVED','result':result},limit)
except Exception:
 channel.write(CFG['envelope']|{'status':'UNKNOWN'},limit)
'''


class SavedSiteProviderReplace:
    """Start once or observe under an exact replace_provider issued operation.

    provider_ready(challenge,deadline) must exercise the Windows native callback
    twice and return exactly status=C92_NATIVE_PROVIDER_CALLBACK, challenge,
    provider_port, rounds=2 and proof_sha256. The port is simultaneously owned
    by the exact staged guest process, checked before/after that callback.
    """

    def __init__(self, *, retirement, retirement_sha256, source_path, source_sha256,
                 provider_ready):
        _need(type(retirement) is SavedSiteProviderRetire and wire._hash(retirement_sha256)
              and wire._hash(source_sha256) and callable(provider_ready)
              and type(source_path) is str and source_path.startswith('/var/lib/n100-ha/cutover/')
              and '..' not in Path(source_path).parts
              and Path(source_path).name=='native-guest-recovery-observer.py')
        self.retirement,self.retirement_pin=retirement,retirement_sha256
        self.source,self.source_pin,self.ready=source_path,source_sha256,provider_ready
        self.lock,self.issued=threading.Lock(),False
        self.guest=_guest_source()
        compile(self.guest,'<saved-provider-replace>','exec')

    def _parse(self, mode, request_raw, receipts_raw):
        request=json.loads(request_raw,object_pairs_hook=wire._pairs)
        phase='restore_provider' if mode in RESTORE_MODES else 'replace_provider'
        _need(type(request) is dict and request.get('stage')==phase
              and request_raw==_encoded(request))
        # The retirement parser authenticates the same scope, exact terminal,
        # quiesce request and full prior chain without accepting a new contract.
        adapted=request|{'stage':'retire_provider'}
        _,chain=self.retirement._parse(_encoded(adapted),receipts_raw)
        _validate('retire_provider',chain.get('retire_provider'),self.retirement.scope,chain)
        _need(chain['retire_provider'].get('provider_retirement_sha256')==self.retirement_pin)
        nonce=request['nonce']
        if mode in RESTORE_MODES:
            failed=chain.get('replace_provider_failed')
            _need(type(failed) is dict and set(failed)=={'nonce','receipt_sha256'}
                  and wire._hash(failed['nonce']) and wire._hash(failed['receipt_sha256'])
                  and failed['receipt_sha256']==request['previous_sha256'] and failed['nonce']!=nonce)
            nonce=failed['nonce']
        return request,chain,nonce

    def _run(self, mode, request_raw, receipts_raw, deadline):
        _need(mode in BOUNDARIES)
        request,chain,replacement_nonce=self._parse(mode,request_raw,receipts_raw)
        retired=self.retirement;transport=retired.transport;opts=transport.options
        with self.lock:
            if mode=='start':
                _need(not self.issued);self.issued=True
            _need(retired.custody(request_raw,receipts_raw,deadline,observe_only=mode not in {'start','restore'}) is True)
            seconds=wire._remaining(deadline);_need(seconds<=600)
            envelope={'mode':mode,'operation_nonce':request['nonce'],'operation_sha256':wire._digest(request_raw),
                      'receipts_sha256':wire._digest(receipts_raw),'rollback_sha256':retired.rollback}
            if mode in RESTORE_MODES:
                envelope['replacement_nonce']=replacement_nonce
            cfg={'replacement_nonce':replacement_nonce,'mode':mode,'archive':opts,'rollback_sha256':retired.rollback,'retirement_sha256':self.retirement_pin,
                 'quiesce_request':retired.quiesce,'quiesce_receipt':chain['quiesce_apps'],
                 'source_path':self.source,'source_sha256':self.source_pin,'envelope':envelope,'seconds':seconds}
            code=transport.launcher.split(MARKER)[0].decode()+'\nimport sys,json\nCFG=json.loads('+repr(wire._bytes(cfg).decode())+')\n'+self.guest
            _need(len(code.encode())<=131072);compile(code,'<saved-provider-replace>','exec')
            stamps,records={},{};dispatcher=transport.dispatcher
            for role,endpoint in (('jump',dispatcher.jump),('A',dispatcher.guests['A'])):
                stamps.update(ssh._identity(endpoint,deadline));path=dispatcher.known_hosts[role]
                raw,stamps[path]=ssh._checked_file(path,private=False,maximum=1048576)
                records[endpoint.alias]=ssh._host_record(raw,endpoint,guest=role=='A')
            proofs={};ready_fact=None
            def fresh(challenge,boundary,limit):
                nonlocal ready_fact
                _need(ssh._same_files(stamps) and retired.custody(request_raw,receipts_raw,limit,observe_only=True) is True)
                closed=transport._closed(challenge,boundary,limit)
                evidence={'challenge':challenge,'boundary':boundary,'closed_pair':closed}
                if boundary in {'provider_ready','original_ready'}:
                    fact=self.ready(challenge,limit)
                    _need(type(fact) is dict and set(fact)=={'status','challenge','provider_port','rounds','proof_sha256'}
                          and fact['status']=='C92_NATIVE_PROVIDER_CALLBACK' and fact['challenge']==challenge
                          and type(fact['provider_port']) is int and fact['provider_port']==opts['provider_port']
                          and type(fact['rounds']) is int and fact['rounds']==2 and wire._hash(fact['proof_sha256']))
                    ready_fact=fact
                    evidence.update(provider_ready=fact,closed_pair_after=transport._closed(challenge,boundary,limit))
                _need(ssh._same_files(stamps) and retired.custody(request_raw,receipts_raw,limit,observe_only=True) is True)
                wire._remaining(limit)
                proof=wire._digest(wire._bytes(envelope|evidence));proofs[boundary]=proof
                return proof
            root='/private/tmp' if sys.platform=='darwin' else '/tmp'
            with tempfile.TemporaryDirectory(prefix='ha-provider-replace-',dir=root) as folder:
                config=ssh._ssh_config(Path(folder),dispatcher.jump,dispatcher.guests['A'],records)
                command=(ssh.SSH,'-F',str(config),'-T',dispatcher.guests['A'].alias,
                         shlex.join(('/usr/bin/python3','-I','-B','-S','-c',code)))
                _need(ssh._same_files(stamps));wire._remaining(deadline)
                raw=ssh._activation_exchange(command,wire._bytes(envelope),deadline,request['nonce'],fresh,
                    bind_boundary=True,boundaries=BOUNDARIES[mode],boundary_seconds=60)
                _need(ssh._same_files(stamps))
            frame=json.loads(raw,object_pairs_hook=wire._pairs)
            _need(type(frame) is dict and set(frame)==set(envelope)|{'status','result'}
                  and all(frame.get(k)==v for k,v in envelope.items()) and frame['status']=='OBSERVED')
            result=frame['result']
            if mode == 'failure' or mode in RESTORE_MODES:
                _need(retired.custody(request_raw,receipts_raw,deadline,observe_only=True) is True)
                wire._remaining(deadline)
            if mode == 'failure':
                _need(result=={'replacement_failed':True})
                return result
            _need(ready_fact is not None)
            if mode in RESTORE_MODES:
                _need(type(result) is dict and set(result)=={
                    'restore_receipt_sha256','provider','replacement_unit','held'}
                    and wire._hash(result['restore_receipt_sha256']) and result['held']==opts['expected_held']
                    and result['replacement_unit']=='n100ha-recovery-observer-saved-'+replacement_nonce[:24]+'.service')
                current=result['provider']
                _need(type(current) is dict and set(current)=={'pid','fragment_sha256','argv_sha256'}
                      and type(current['pid']) is int and current['pid']>1
                      and current['pid']!=opts['expected_held']['pid'] and wire._hash(current['fragment_sha256'])
                      and current['argv_sha256']==opts['expected_provider']['argv_sha256'])
                return {'old_port_owner_verified':True,'rollback_sha256':retired.rollback,
                        'old_provider_proof_sha256':wire._digest(wire._bytes({
                            'operation':envelope,'observation':result,'native_provider':ready_fact}))}
            expected={'schema_version':1,'binding':dispatcher.binding.fields(),
                'operation_nonce_sha256':wire._digest(request['nonce'].encode()),'rollback_sha256':retired.rollback,
                'retirement_sha256':self.retirement_pin,'original_unit':opts['provider_unit'],
                'original':opts['expected_provider'],'held':opts['expected_held'],
                'unit':'n100ha-recovery-observer-saved-'+request['nonce'][:24]+'.service',
                'source_sha256':self.source_pin,'automatic_retry_permitted':False}
            _need(type(result) is dict and set(result)==set(expected)|{'argv_sha256','provider','proofs'}
                  and all(result.get(k)==v for k,v in expected.items()) and wire._hash(result['argv_sha256'])
                  and result['proofs']=={k:proofs[k] for k in ('before','provider_ready','after')})
            current=result['provider']
            _need(type(current) is dict and set(current)=={'pid','fragment_sha256','argv_sha256'}
                  and type(current['pid']) is int and current['pid']>1
                  and current['pid'] not in (opts['expected_provider']['pid'],opts['expected_held']['pid'])
                  and current['argv_sha256']==result['argv_sha256'] and wire._hash(current['fragment_sha256']))
            _need(retired.custody(request_raw,receipts_raw,deadline,observe_only=True) is True)
            wire._remaining(deadline)
            return {'owned_port':True,'provider_proof_sha256':wire._digest(wire._bytes({
                'operation':envelope,'observation':result,'native_provider':ready_fact}))}

    def start(self, request_raw, receipts_raw, deadline):
        return self._run('start',request_raw,receipts_raw,deadline)

    def observe(self, request_raw, receipts_raw, deadline):
        return self._run('observe',request_raw,receipts_raw,deadline)

    def replace_provider(self, operation, receipts, deadline, *, observe_only):
        request=_encoded(operation)
        chain=_encoded(receipts)
        try:
            evidence=self._run('observe' if observe_only else 'start',request,chain,deadline)
        except (TransitionBlocked, wire.BridgeRejected, OSError):
            evidence=self._run('failure',request,chain,deadline)
            return {'status':'REJECTED','evidence':evidence}
        return {'status':'PASS','evidence':evidence}

    def restore_provider(self, operation, receipts, deadline, *, observe_only):
        result=self._run('observe_restore' if observe_only else 'restore',
                         _encoded(operation),_encoded(receipts),deadline)
        return {'status':'PASS','evidence':result}

    def resume_provider_restore(self, operation, receipts, deadline):
        """Resume the issued rollback using the native stop/start intent fences."""
        try:
            return self._run('observe_restore',_encoded(operation),_encoded(receipts),deadline)
        except TransitionBlocked:
            return self._run('restore',_encoded(operation),_encoded(receipts),deadline)
