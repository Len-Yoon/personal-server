"""One explicit app-only continuation under the unchanged published writer.

ROOT owns the authorization; it grants no database or Windows authority.
Original hold/intent/progress remain immutable. No new RPC or revision exists.
"""
import json
import os
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

def path(request):
    from . import native_guest_worker as w
    if request.verb==wire.GUEST_START_INITIAL_UNPUBLISHED:
        return w.ROOT/('initial-app-resume-'+request.args['unpublished']['attempt_id']+'.json')
    return w.ROOT/('initial-app-resume-'+prefix(request).removesuffix('-start-')+'.json')

def decode(raw):
    def pairs(items):
        value=dict(items);need(len(value)==len(items));return value
    value=json.loads(raw,object_pairs_hook=pairs)
    need(type(value) is dict)
    return value

def prior(request,hold,active,intent_pin,progress_pins):
    from . import native_guest_worker as w
    stage=w.ROOT/request.binding.attempt_id;stem=prefix(request)
    need(wire._hash(intent_pin) and type(progress_pins) is dict
         and all(type(name) is str and wire._hash(pin) for name,pin in progress_pins.items()))
    if request.verb==wire.GUEST_START_INITIAL_POSTBOOT:
        need(not progress_pins)
    names={p.name[len(stem):] for p in stage.iterdir() if p.name.startswith(stem) and not p.name.startswith(stem+'resume-')}
    need(names=={'intent.json',*progress_pins})
    raw=w.runtime._read(stage/(stem+'intent.json'),0,{0o600},limit=131072)
    need(wire._digest(raw)==intent_pin)
    intent=decode(raw);scope=w._scope(request)
    expected={'schema_version':1,'status':'INTENT',**scope,'nonce':intent.get('nonce'),
        'sequence':intent.get('sequence'),'args':request.args,'containers':hold['containers'],
        'authority_files':active['authority_files'],'proof':intent.get('proof'),'automatic_retry_permitted':False}
    need(wire._bytes(intent)==wire._bytes(expected) and type(intent['schema_version']) is int and type(intent['sequence']) is int
         and intent['sequence']>0 and wire._hash(intent['nonce']) and intent['nonce']!=request.nonce
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
    return stage,stem

def stopped(request,raw,hold,prepared,active):
    from . import native_guest_worker as w
    if request.verb==wire.GUEST_START_INITIAL_POSTBOOT:
        from .native_guest_postboot import observe
        args=request.args
        record=observe(request,stopped_attempt(args['postboot']),args['postboot_stopped_receipt_sha256'])
        return record['container_contracts'],record['protected_inventory_sha256']
    w._assert_car_route_blocked(request,raw,prepared,active)
    authorities={w.runtime.ROOT/name:pin for name,pin in active['authority_files'].items()}
    need(set(authorities)==set(w.live._authority_paths().values())
         and all(wire._digest(w.runtime._read(p,0,{0o444}))==pin for p,pin in authorities.items()))
    contracts,protected=w._initial_recovery_inventory(request.binding,hold['containers'],
        request.args['protected_inventory_sha256'],None,(hold['container_contracts'],request.args['protected_inventory_sha256']))
    need(all(row['running'] is False for row in contracts.values()))
    return contracts,protected

def attest(request,*,service_instance_sha256,prior_intent_sha256,prior_progress_sha256):
    """Trusted operator calls after fresh current-publication proof; metadata only.

    Root must pin the returned authorization hash, this new request nonce, and
    the fixed publication.service_instance_sha256 in every callback. Nothing
    here substitutes for the fresh Native/paired/local-PG checks at dispatch.
    """
    from . import native_guest_worker as w
    need(wire._hash(service_instance_sha256) and wire._hash(request.nonce)
         and type(request.sequence) is int and 0<request.sequence<=wire.MAX_REQUESTS)
    prefix(request)
    with w._guest_mutation_descriptor():
        raw,hold,prepared,active=w._read_initial_recovery_hold(request)
        need(wire._digest(raw)==request.args['hold_sha256'] and not os.path.lexists(path(request)))
        stage,stem=prior(request,hold,active,prior_intent_sha256,prior_progress_sha256)
        need(not any(p.name.startswith(stem+'resume-') for p in stage.iterdir()))
        contracts,protected=stopped(request,raw,hold,prepared,active)
        if request.verb==wire.GUEST_START_INITIAL_UNPUBLISHED:
            order=w.RECOVERY_START_ORDER;changed={role for role,row in contracts.items()
                if row['started_at']!=hold['container_contracts'][role]['started_at']}
            need(set(order[:len(prior_progress_sha256)])<=changed<=set(order[:len(prior_progress_sha256)+1]))
        value={'schema_version':1,'request':w._scope(request),'args':request.args,
            'hold_sha256':wire._digest(raw),'service_instance_sha256':service_instance_sha256,
            'prior_intent_sha256':prior_intent_sha256,'prior_progress_sha256':prior_progress_sha256,
            'containers':hold['containers'],'container_contracts':contracts,'protected_inventory_sha256':protected,
            'authority_files':active['authority_files']}
        body=wire._bytes(value)
        fd=os.open(path(request),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o400)
        with os.fdopen(fd,'wb') as out:out.write(body);out.flush();os.fsync(out.fileno())
        directory=os.open(w.ROOT,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(directory)
        finally:os.close(directory)
        need(w.runtime._read(path(request),0,{0o400},limit=131072)==body)
        return {'schema_version':1,'authorization_sha256':wire._digest(body),'request_nonce':request.nonce,
            'baseline_sha256':wire._digest(wire._bytes(contracts)),'prior_intent_sha256':prior_intent_sha256,
            'prior_progress_sha256':prior_progress_sha256,'service_instance_sha256':service_instance_sha256,
            'container_count':8,'containers_started':False,'database_changed':False}

def load(request,raw,hold,prepared,active):
    """Called only under the installed worker mutation descriptor."""
    from . import native_guest_worker as w
    body=w.runtime._read(path(request),0,{0o400},limit=131072);value=decode(body)
    need(set(value)==FIELDS and type(value['schema_version']) is int and value['schema_version']==1
         and wire._bytes(value['request'])==wire._bytes(w._scope(request)) and wire._bytes(value['args'])==wire._bytes(request.args)
         and value['hold_sha256']==wire._digest(raw)==request.args['hold_sha256']
         and wire._hash(value['service_instance_sha256']) and value['containers']==hold['containers']
         and value['authority_files']==active['authority_files'])
    stage,stem=prior(request,hold,active,value['prior_intent_sha256'],value['prior_progress_sha256'])
    need(not any(p.name.startswith(stem+'resume-') for p in stage.iterdir()))
    contracts,protected=stopped(request,raw,hold,prepared,active)
    need(value['container_contracts']==contracts and value['protected_inventory_sha256']==protected)
    return body,value,stem+'resume-'

def preserved(request,body,value,hold,active):
    from . import native_guest_worker as w
    need(w.runtime._read(path(request),0,{0o400},limit=131072)==body)
    prior(request,hold,active,value['prior_intent_sha256'],value['prior_progress_sha256'])
