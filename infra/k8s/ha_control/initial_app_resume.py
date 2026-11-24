"""Explicit app-only continuations under the unchanged published writer.

ROOT owns the authorization; it grants no database or Windows authority.
Original hold/intent/progress remain immutable. No new RPC or revision exists.
"""
import json
import os
from dataclasses import replace
from . import native_stdio_bridge as wire
from .controller import TransitionBlocked
from .initial_postboot import sameboot, scope, stopped_attempt

ERROR='initial_app_resume_unverified'
FIELDS={'schema_version','request','args','hold_sha256','service_instance_sha256',
        'prior_intent_sha256','prior_progress_sha256','containers','container_contracts',
        'protected_inventory_sha256','authority_files'}

def need(value):
    if not value:raise TransitionBlocked(ERROR)

def supports(request):
    return request.verb==wire.GUEST_START_INITIAL_UNPUBLISHED or (
        request.verb==wire.GUEST_START_INITIAL_POSTBOOT and sameboot(request.args.get('postboot')))

def prefix(request):
    need(supports(request) and request.node=='A')
    wire._args(request.verb,request.node,request.args)
    if request.verb==wire.GUEST_START_INITIAL_UNPUBLISHED:
        return 'unpublished-'+request.args['unpublished']['attempt_id']+'-start-'
    args=request.args
    value=scope({'initial_recovery_postboot':args['postboot'],
        'initial_cutover_recovery':{'attempt_id':args['recovery_attempt_id']},
        'initial_cutover_handoff':{'attempt_id':request.binding.attempt_id}})
    need(args['postboot_stopped_receipt_sha256']==value['stopped_receipt_sha256'])
    return 'postboot-'+value['attempt_id']+'-start-'

def path(request, *, partial=False):
    from . import native_guest_worker as w
    if request.verb==wire.GUEST_START_INITIAL_UNPUBLISHED:
        need(not partial)
        return w.ROOT/('initial-app-resume-'+request.args['unpublished']['attempt_id']+'.json')
    stem=prefix(request).removesuffix('-start-')
    suffix='-partial' if partial else ''
    return w.ROOT/('initial-app-resume-'+stem+suffix+'.json')

def decode(raw):
    def pairs(items):
        value=dict(items);need(len(value)==len(items));return value
    value=json.loads(raw,object_pairs_hook=pairs)
    need(type(value) is dict)
    return value

def prior(request,hold,active,intent_pin,progress_pins):
    from . import native_guest_worker as w
    stage=w.ROOT/request.binding.attempt_id;stem=prefix(request)
    if request.verb==wire.GUEST_START_INITIAL_POSTBOOT:
        need(type(progress_pins) is dict and not progress_pins)
    _prior_attempt(request,hold,active,stage,stem,intent_pin,progress_pins,'resume-')
    return stage,stem

def _prior_attempt(request,hold,active,stage,stem,intent_pin,progress_pins,successor,*,authorization_pin=None):
    from . import native_guest_worker as w
    need(wire._hash(intent_pin) and type(progress_pins) is dict
         and all(type(name) is str and wire._hash(pin) for name,pin in progress_pins.items()))
    names={p.name[len(stem):] for p in stage.iterdir() if p.name.startswith(stem) and not p.name.startswith(stem+successor)}
    need(names=={'intent.json',*progress_pins})
    raw=w.runtime._read(stage/(stem+'intent.json'),0,{0o600},limit=131072)
    need(wire._digest(raw)==intent_pin)
    intent=decode(raw);scope=w._scope(request)
    expected={'schema_version':1,'status':'INTENT',**scope,'args':request.args,'containers':hold['containers'],
        'authority_files':active['authority_files'],'proof':intent.get('proof'),'automatic_retry_permitted':False}
    if authorization_pin is None:
        need(intent.get('nonce')!=request.nonce)
        expected.update(nonce=intent.get('nonce'),sequence=intent.get('sequence'))
    else:
        expected['resume_authorization_sha256']=authorization_pin
    need(wire._bytes(intent)==wire._bytes(expected) and type(intent['schema_version']) is int and type(intent['sequence']) is int
         and 0<intent['sequence']<=wire.MAX_REQUESTS and wire._hash(intent['nonce'])
         and type(intent['proof']) is dict and set(intent['proof'])=={'windows_proof_sha256','guest_proof_sha256'}
         and all(wire._hash(value) for value in intent['proof'].values()) and intent['automatic_retry_permitted'] is False)
    order=w.RECOVERY_START_ORDER
    need(set(progress_pins)=={'progress-'+role+'.json' for role in order[:len(progress_pins)]})
    for index,role in enumerate(order[:len(progress_pins)]):
        name='progress-'+role+'.json';raw=w.runtime._read(stage/(stem+name),0,{0o600},limit=131072)
        value=decode(raw)
        need(wire._digest(raw)==progress_pins[name] and set(value)=={'intent_sha256','started','proof'}
             and value['intent_sha256']==intent_pin and value['started']==sorted(order[:index+1])
             and type(value['proof']) is dict and set(value['proof'])=={'windows_proof_sha256','guest_proof_sha256'}
             and all(wire._hash(pin) for pin in value['proof'].values()))
def _resume_lineage(request,raw,hold,active,stage,stem,original_intent,service_instance,lineage):
    from . import native_guest_worker as w
    need(request.verb==wire.GUEST_START_INITIAL_POSTBOOT and supports(request)
         and type(lineage) is dict and set(lineage)=={'authorization_sha256','intent_sha256','progress_sha256'}
         and wire._hash(lineage['authorization_sha256']) and wire._hash(lineage['intent_sha256'])
         and type(lineage['progress_sha256']) is dict
         and 0<len(lineage['progress_sha256'])<len(w.RECOVERY_START_ORDER))
    body=w.runtime._read(path(request),0,{0o400},limit=131072)
    need(wire._digest(body)==lineage['authorization_sha256'])
    authorization=decode(body)
    need(type(authorization.get('request')) is dict)
    nonce=authorization['request'].get('nonce')
    sequence=authorization['request'].get('sequence')
    need(wire._hash(nonce) and nonce!=request.nonce
         and type(sequence) is int and 0<sequence<=wire.MAX_REQUESTS)
    previous=replace(request,nonce=nonce,sequence=sequence)
    _validate_authorization(previous,raw,hold,active,authorization,partial=False)
    need(authorization['service_instance_sha256']==service_instance
         and authorization['prior_intent_sha256']==original_intent and authorization['prior_progress_sha256']=={})
    _prior_attempt(previous,hold,active,stage,stem+'resume-',lineage['intent_sha256'],
                   lineage['progress_sha256'],'partial-',authorization_pin=lineage['authorization_sha256'])
    return authorization['container_contracts']

def stopped(request,raw,hold,prepared,active,*,prior_baseline=None):
    from . import native_guest_worker as w
    if request.verb==wire.GUEST_START_INITIAL_POSTBOOT:
        from .native_guest_postboot import observe
        args=request.args
        record=observe(request,stopped_attempt(args['postboot']),args['postboot_stopped_receipt_sha256'],
                       require_stopped=prior_baseline is None)
        if prior_baseline is not None:
            need(prior_baseline==record['container_contracts'])
            return w._initial_recovery_inventory(request.binding,hold['containers'],
                record['protected_inventory_sha256'],None,
                (prior_baseline,record['protected_inventory_sha256']))
        return record['container_contracts'],record['protected_inventory_sha256']
    w._assert_car_route_blocked(request,raw,prepared,active)
    authorities={w.runtime.ROOT/name:pin for name,pin in active['authority_files'].items()}
    need(set(authorities)==set(w.live._authority_paths().values())
         and all(wire._digest(w.runtime._read(p,0,{0o444}))==pin for p,pin in authorities.items()))
    contracts,protected=w._initial_recovery_inventory(request.binding,hold['containers'],
        request.args['protected_inventory_sha256'],None,(hold['container_contracts'],request.args['protected_inventory_sha256']))
    need(all(row['running'] is False for row in contracts.values()))
    return contracts,protected

def _validate_authorization(request,raw,hold,active,value,*,partial):
    from . import native_guest_worker as w
    fields=FIELDS|({'prior_resume'} if partial else set())
    need(set(value)==fields and type(value['schema_version']) is int and value['schema_version']==1
         and wire._bytes(value['request'])==wire._bytes(w._scope(request)) and wire._bytes(value['args'])==wire._bytes(request.args)
         and value['hold_sha256']==wire._digest(raw)==request.args['hold_sha256']
         and wire._hash(value['service_instance_sha256']) and value['containers']==hold['containers']
         and value['authority_files']==active['authority_files'])

def _authorization_baseline(request,raw,hold,prepared,active,value,stage,stem):
    from . import native_guest_worker as w
    lineage=value.get('prior_resume')
    prior_baseline=None
    count=len(value['prior_progress_sha256'])
    if 'prior_resume' in value:
        prior_baseline=_resume_lineage(request,raw,hold,active,stage,stem,value['prior_intent_sha256'],
                                      value['service_instance_sha256'],lineage)
        need(type(prior_baseline) is dict)
        stem+='resume-partial-'
        count=len(lineage['progress_sha256'])
    else:
        stem+='resume-'
    need(not any(p.name.startswith(stem) for p in stage.iterdir()))
    contracts,protected=stopped(request,raw,hold,prepared,active,prior_baseline=prior_baseline)
    if prior_baseline is not None or request.verb==wire.GUEST_START_INITIAL_UNPUBLISHED:
        baseline=prior_baseline if prior_baseline is not None else hold['container_contracts']
        order=w.RECOVERY_START_ORDER
        need(set(contracts)==set(baseline)==set(order) and all(row['running'] is False for row in contracts.values()))
        changed={role for role,row in contracts.items() if row['started_at']!=baseline[role]['started_at']}
        need(set(order[:count])<=changed<=set(order[:count+1]))
    return contracts,protected,stem

def attest(request,*,service_instance_sha256,prior_intent_sha256,prior_progress_sha256,prior_resume=None):
    """Trusted operator calls after fresh current-publication proof; metadata only.

    Root must pin the returned authorization hash, this new request nonce, and
    the fixed publication.service_instance_sha256 in every callback. Nothing
    here substitutes for the fresh Native/paired/local-PG checks at dispatch.
    A partial postboot continuation also pins the first authorization, intent,
    and every recorded progress receipt. It can be admitted only once.
    """
    from . import native_guest_worker as w
    need(wire._hash(service_instance_sha256) and wire._hash(request.nonce)
         and type(request.sequence) is int and 0<request.sequence<=wire.MAX_REQUESTS)
    prefix(request)
    partial=prior_resume is not None
    target=path(request,partial=partial)
    with w._guest_mutation_descriptor():
        raw,hold,prepared,active=w._read_initial_recovery_hold(request)
        need(wire._digest(raw)==request.args['hold_sha256'] and not os.path.lexists(target))
        stage,stem=prior(request,hold,active,prior_intent_sha256,prior_progress_sha256)
        value={'schema_version':1,'request':w._scope(request),'args':request.args,
            'hold_sha256':wire._digest(raw),'service_instance_sha256':service_instance_sha256,
            'prior_intent_sha256':prior_intent_sha256,'prior_progress_sha256':prior_progress_sha256,
            'containers':hold['containers'],'authority_files':active['authority_files']}
        if partial:
            value['prior_resume']=prior_resume
        contracts,protected,_=_authorization_baseline(request,raw,hold,prepared,active,value,stage,stem)
        value.update(container_contracts=contracts,protected_inventory_sha256=protected)
        body=wire._bytes(value)
        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o400)
        with os.fdopen(fd,'wb') as out:out.write(body);out.flush();os.fsync(out.fileno())
        directory=os.open(w.ROOT,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(directory)
        finally:os.close(directory)
        need(w.runtime._read(target,0,{0o400},limit=131072)==body)
        return {'schema_version':1,'authorization_sha256':wire._digest(body),'request_nonce':request.nonce,
            'baseline_sha256':wire._digest(wire._bytes(contracts)),'prior_intent_sha256':prior_intent_sha256,
            'prior_progress_sha256':prior_progress_sha256,'service_instance_sha256':service_instance_sha256,
            'container_count':8,'containers_started':False,'database_changed':False}

def load(request,raw,hold,prepared,active,*,partial=False):
    """Called only under the installed worker mutation descriptor."""
    from . import native_guest_worker as w
    body=w.runtime._read(path(request,partial=partial),0,{0o400},limit=131072);value=decode(body)
    _validate_authorization(request,raw,hold,active,value,partial=partial)
    stage,stem=prior(request,hold,active,value['prior_intent_sha256'],value['prior_progress_sha256'])
    contracts,protected,stem=_authorization_baseline(request,raw,hold,prepared,active,value,stage,stem)
    need(value['container_contracts']==contracts and value['protected_inventory_sha256']==protected)
    return body,value,stem

def preserved(request,body,value,hold,active,raw):
    from . import native_guest_worker as w
    partial='prior_resume' in value
    need(w.runtime._read(path(request,partial=partial),0,{0o400},limit=131072)==body)
    stage,stem=prior(request,hold,active,value['prior_intent_sha256'],value['prior_progress_sha256'])
    if partial:
        _resume_lineage(request,raw,hold,active,stage,stem,value['prior_intent_sha256'],
                        value['service_instance_sha256'],value['prior_resume'])
