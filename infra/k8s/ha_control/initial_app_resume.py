"""Explicit app-only continuations under the unchanged published writer.

ROOT owns the authorization; it grants no database or Windows authority.
Original hold/intent/progress remain immutable. No new RPC or revision exists.
"""
import json
import os
import re
import time
from dataclasses import replace
from . import native_stdio_bridge as wire
from .controller import TransitionBlocked
from .initial_postboot import published_postboot, scope, stopped_attempt

ERROR='initial_app_resume_unverified'
FIELDS={'schema_version','request','args','hold_sha256','service_instance_sha256',
        'prior_intent_sha256','prior_progress_sha256','containers','container_contracts',
        'protected_inventory_sha256','authority_files'}
MAX_NUMBERED_ATTEMPTS = 8
CONTINUATION = 'resume-partial-prestart-continuation-'
LINK_FIELDS = {'authorization_sha256', 'intent_sha256', 'progress_sha256'}
COMPLETED_LINK_FIELDS = LINK_FIELDS | {'done_sha256'}

def need(value):
    if not value:raise TransitionBlocked(ERROR)

def supports(request):
    return request.verb==wire.GUEST_START_INITIAL_UNPUBLISHED or (
        request.verb==wire.GUEST_START_INITIAL_POSTBOOT and published_postboot(request.args.get('postboot')))

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

def path(request, *, partial=False, prestart=False, continuation=False, numbered_attempt=0):
    from . import native_guest_worker as w
    need((not prestart or partial) and (not continuation or prestart))
    need(type(numbered_attempt) is int and 0 <= numbered_attempt <= MAX_NUMBERED_ATTEMPTS
         and (not numbered_attempt or continuation))
    if request.verb==wire.GUEST_START_INITIAL_UNPUBLISHED:
        need(not partial)
        return w.ROOT/('initial-app-resume-'+request.args['unpublished']['attempt_id']+'.json')
    stem=prefix(request).removesuffix('-start-')
    suffix='-continuation' if continuation else '-partial-prestart' if prestart else '-partial' if partial else ''
    if numbered_attempt:
        suffix=f'-attempt-{numbered_attempt:04d}'
    return w.ROOT/('initial-app-resume-'+stem+suffix+'.json')

def _numbered_stem(stem, attempt):
    return f'{stem}{CONTINUATION}attempt-{attempt:04d}-'

def latest_attempt(request):
    """A numbered attempt may only extend the latest immutable slot."""
    from . import native_guest_worker as w
    if request.verb != wire.GUEST_START_INITIAL_POSTBOOT:
        return 0
    stem = prefix(request)
    authorization_prefix = 'initial-app-resume-' + stem.removesuffix('-start-') + '-attempt-'
    slots = set()
    for entry in w.ROOT.iterdir():
        if not entry.name.startswith(authorization_prefix):
            continue
        suffix = entry.name.removeprefix(authorization_prefix)
        need(re.fullmatch(r'[0-9]{4}\.json', suffix))
        slots.add(int(suffix[:4]))
    latest = max(slots, default=0)
    need(latest <= MAX_NUMBERED_ATTEMPTS and slots == set(range(1, latest + 1)))
    journal_prefix = stem + CONTINUATION + 'attempt-'
    try:
        entries = list((w.ROOT / request.binding.attempt_id).iterdir())
    except FileNotFoundError:
        need(latest == 0)
        return 0
    for entry in entries:
        if not entry.name.startswith(journal_prefix):
            continue
        slot = re.match(r'([0-9]{4})-', entry.name.removeprefix(journal_prefix))
        need(slot is not None and int(slot[1]) in slots)
    return latest

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

def _prior_attempt(request,hold,active,stage,stem,intent_pin,progress_pins,successor,*,authorization_pin=None,
                   done_pin=None):
    from . import native_guest_worker as w
    need(wire._hash(intent_pin) and type(progress_pins) is dict
         and all(type(name) is str and wire._hash(pin) for name,pin in progress_pins.items()))
    names={p.name[len(stem):] for p in stage.iterdir()
           if p.name.startswith(stem) and (successor is None or not p.name.startswith(stem+successor))}
    expected_names = {'intent.json', *progress_pins}
    if done_pin is not None:
        need(wire._hash(done_pin) and authorization_pin is not None)
        expected_names.add('done.json')
    need(names==expected_names)
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
    if done_pin is not None:
        need(len(progress_pins)==len(order))
        done_raw=w.runtime._read(stage/(stem+'done.json'),0,{0o600},limit=131072)
        need(wire._digest(done_raw)==done_pin)
        done=decode(done_raw)
        proof=done.get('proof')
        expected_done={'schema_version':1,'status':'STARTED','intent_sha256':intent_pin,
                     'hold_sha256':request.args['hold_sha256'],'started':list(order),
                     'proof':proof,'automatic_retry_permitted':False}
        need(wire._bytes(done)==wire._bytes(expected_done) and type(proof) is dict
             and set(proof)=={'windows_proof_sha256','guest_proof_sha256'}
             and all(wire._hash(pin) for pin in proof.values()))
def _read_predecessor_authorization(request,raw,hold,active,pin,*,partial=False,prestart=False,
                                    continuation=False,numbered_attempt=0):
    from . import native_guest_worker as w
    need(wire._hash(pin))
    body=w.runtime._read(path(request,partial=partial,prestart=prestart,continuation=continuation,
                              numbered_attempt=numbered_attempt),0,{0o400},limit=131072)
    need(wire._digest(body)==pin)
    authorization=decode(body)
    need(type(authorization.get('request')) is dict)
    nonce=authorization['request'].get('nonce')
    sequence=authorization['request'].get('sequence')
    need(wire._hash(nonce) and nonce!=request.nonce
         and type(sequence) is int and 0<sequence<=wire.MAX_REQUESTS)
    previous=replace(request,nonce=nonce,sequence=sequence)
    _validate_authorization(previous,raw,hold,active,authorization,partial=partial,prestart=prestart,
                            continuation=continuation,numbered_attempt=numbered_attempt)
    return previous,authorization

def _resume_lineage(request,raw,hold,active,stage,stem,original_intent,service_instance,lineage,*,carryover=None):
    from . import native_guest_worker as w
    need(request.verb==wire.GUEST_START_INITIAL_POSTBOOT and supports(request)
         and type(lineage) is dict and set(lineage)=={'authorization_sha256','intent_sha256','progress_sha256'}
         and wire._hash(lineage['intent_sha256']) and type(lineage['progress_sha256']) is dict
         and 0<len(lineage['progress_sha256'])<len(w.RECOVERY_START_ORDER))
    previous,authorization=_read_predecessor_authorization(request,raw,hold,active,lineage['authorization_sha256'])
    progress = {} if carryover is None else carryover['history']['progress_sha256']
    need(authorization['service_instance_sha256']==service_instance
         and authorization['prior_intent_sha256']==original_intent and authorization['prior_progress_sha256']==progress)
    if carryover is not None:
        need(authorization['container_contracts']==carryover['container_contracts']
             and authorization['protected_inventory_sha256']==carryover['protected_inventory_sha256'])
    _prior_attempt(previous,hold,active,stage,stem+'resume-',lineage['intent_sha256'],
                   lineage['progress_sha256'],'partial-',authorization_pin=lineage['authorization_sha256'])
    return authorization['container_contracts']

def _prestart_lineage(request,raw,hold,active,stage,stem,value):
    lineage=value['prior_partial']
    need(request.verb==wire.GUEST_START_INITIAL_POSTBOOT and supports(request)
         and type(lineage) is dict and set(lineage)=={'authorization_sha256','intent_sha256'}
         and wire._hash(lineage['intent_sha256']))
    previous,authorization=_read_predecessor_authorization(request,raw,hold,active,
                                                            lineage['authorization_sha256'],partial=True)
    need(authorization['service_instance_sha256']==value['service_instance_sha256']
         and authorization['prior_intent_sha256']==value['prior_intent_sha256']
         and authorization['prior_progress_sha256']=={}
         and authorization['prior_resume']==value['prior_resume'])
    _prior_attempt(previous,hold,active,stage,stem+'resume-partial-',lineage['intent_sha256'],
                   {},'prestart-',authorization_pin=lineage['authorization_sha256'])
    return authorization

def _continuation_lineage(request,raw,hold,active,stage,stem,value,partial_authorization):
    from . import native_guest_worker as w
    lineage=value['prior_prestart']
    need(type(lineage) is dict and set(lineage)=={'authorization_sha256','intent_sha256','progress_sha256'}
         and wire._hash(lineage['intent_sha256']) and type(lineage['progress_sha256']) is dict
         and 0<len(lineage['progress_sha256'])<len(w.RECOVERY_START_ORDER))
    previous,authorization=_read_predecessor_authorization(request,raw,hold,active,
        lineage['authorization_sha256'],partial=True,prestart=True)
    need(authorization['service_instance_sha256']==value['service_instance_sha256']
         and authorization['prior_intent_sha256']==value['prior_intent_sha256']
         and authorization['prior_progress_sha256']=={}
         and authorization['prior_resume']==value['prior_resume']
         and authorization['prior_partial']==value['prior_partial']
         and authorization['container_contracts']==partial_authorization['container_contracts']
         and authorization['protected_inventory_sha256']==partial_authorization['protected_inventory_sha256'])
    _prior_attempt(previous,hold,active,stage,stem+'resume-partial-prestart-',lineage['intent_sha256'],
                   lineage['progress_sha256'],'continuation-',authorization_pin=lineage['authorization_sha256'])
    return authorization

def _confirm_progress_drift(current, previous, count):
    from . import native_guest_worker as w
    order = w.RECOVERY_START_ORDER
    need(type(current) is dict and type(previous) is dict and set(current) == set(previous) == set(order))
    changed = set()
    for role in order:
        newest, oldest = current[role], previous[role]
        need(type(newest) is dict and type(oldest) is dict and all(
            set(row) == {'running', 'layout_sha256', 'started_at'} and row['running'] is False
            and wire._hash(row['layout_sha256']) and type(row['started_at']) is str and row['started_at']
            for row in (newest, oldest)) and newest['layout_sha256'] == oldest['layout_sha256'])
        if newest['started_at'] != oldest['started_at']:
            changed.add(role)
    need(set(order[:count]) <= changed <= set(order[:count+1]))

def _numbered_lineage(request, raw, hold, active, stage, stem, value, prestart_authorization):
    """Verify the exact immediate predecessor and every hash-linked history slot."""
    from . import native_guest_worker as w
    need(request.verb == wire.GUEST_START_INITIAL_POSTBOOT and supports(request))
    latest = value['numbered_attempt']
    need(type(latest) is int and 1 <= latest <= MAX_NUMBERED_ATTEMPTS)
    nonces = {request.nonce}
    newer = value
    for attempt in range(latest - 1, -1, -1):
        link = newer['prior_attempt']
        need(type(link) is dict and set(link) == LINK_FIELDS and wire._hash(link['intent_sha256'])
             and type(link['progress_sha256']) is dict
             and len(link['progress_sha256']) < len(w.RECOVERY_START_ORDER))
        previous, auth = _read_predecessor_authorization(
            request, raw, hold, active, link['authorization_sha256'], partial=True, prestart=True,
            continuation=True, numbered_attempt=attempt)
        need(previous.nonce not in nonces and all(auth[key] == value[key] for key in (
            'service_instance_sha256', 'prior_intent_sha256', 'prior_progress_sha256', 'prior_resume',
            'prior_partial', 'prior_prestart'))
            and auth['protected_inventory_sha256'] == prestart_authorization['protected_inventory_sha256'])
        nonces.add(previous.nonce)
        # Legacy C50 ignores the numbered child namespace only. Numbered slots
        # are disjoint, and every unexpected record in a predecessor blocks.
        prior_stem = _numbered_stem(stem, attempt) if attempt else stem + CONTINUATION
        _prior_attempt(previous, hold, active, stage, prior_stem, link['intent_sha256'],
                       link['progress_sha256'], None if attempt else 'attempt-',
                       authorization_pin=link['authorization_sha256'])
        if 'container_contracts' in newer:
            _confirm_progress_drift(newer['container_contracts'], auth['container_contracts'],
                                    len(link['progress_sha256']))
        if attempt == latest - 1:
            immediate = auth['container_contracts'], len(link['progress_sha256'])
        newer = auth
    _confirm_progress_drift(newer['container_contracts'], prestart_authorization['container_contracts'],
                            len(value['prior_prestart']['progress_sha256']))
    return immediate

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

def _validate_authorization(request,raw,hold,active,value,*,partial,prestart=False,continuation=False,
                            numbered_attempt=0):
    from . import native_guest_worker as w
    fields=FIELDS|({'prior_resume'} if partial else set())
    if request.args.get('postboot', {}).get('carryover_receipt_sha256'):
        fields = fields | {'carryover_receipt_sha256'}
        need(value.get('carryover_receipt_sha256') == request.args['postboot']['carryover_receipt_sha256']
             and not prestart and not continuation and not numbered_attempt)
    if prestart:
        fields=fields|{'prior_partial'}
    if continuation:
        fields=fields|{'prior_prestart'}
    if numbered_attempt:
        fields=fields|{'prior_attempt','numbered_attempt'}
        need(type(value.get('numbered_attempt')) is int and value['numbered_attempt'] == numbered_attempt)
    need((not prestart or partial) and (not continuation or prestart) and set(value)==fields
         and type(value['schema_version']) is int and value['schema_version']==1
         and wire._bytes(value['request'])==wire._bytes(w._scope(request)) and wire._bytes(value['args'])==wire._bytes(request.args)
         and value['hold_sha256']==wire._digest(raw)==request.args['hold_sha256']
         and wire._hash(value['service_instance_sha256']) and value['containers']==hold['containers']
         and value['authority_files']==active['authority_files'])

def _carryover_lineage(request,raw,hold,active,value,stage,stem,*,require_stopped):
    from .native_guest_postboot import observe_carryover
    partial = 'prior_resume' in value
    receipt = observe_carryover(request, request.args['postboot']['attempt_id'], value['carryover_receipt_sha256'],
                                require_stopped=require_stopped and not partial)
    need(value['prior_intent_sha256']==receipt['history']['intent_sha256']
         and value['prior_progress_sha256']==receipt['history']['progress_sha256'])
    if partial:
        need(not any(p.name.startswith(stem) and not p.name.startswith(stem+'resume-')
                     for p in stage.iterdir()))
        _resume_lineage(request,raw,hold,active,stage,stem,value['prior_intent_sha256'],
                        value['service_instance_sha256'],value['prior_resume'],carryover=receipt)
    return receipt

def _authorization_baseline(request,raw,hold,prepared,active,value,stage,stem):
    from . import native_guest_worker as w
    if 'carryover_receipt_sha256' in value:
        receipt = _carryover_lineage(request,raw,hold,active,value,stage,stem,require_stopped=True)
        if 'prior_resume' not in value:
            need(not any(p.name.startswith(stem) for p in stage.iterdir()))
            return receipt['container_contracts'], receipt['protected_inventory_sha256'], stem+'resume-'
        resume_stem = stem+'resume-partial-'
        need(not any(p.name.startswith(resume_stem) for p in stage.iterdir()))
        w._assert_car_route_blocked(request,raw,prepared,active)
        contracts,protected = w._initial_recovery_inventory(
            request.binding,hold['containers'],receipt['protected_inventory_sha256'],None,
            (receipt['container_contracts'],receipt['protected_inventory_sha256']),
            inventory=lambda:w.restore._observer_inventory(time.monotonic()+wire.ACTIVATION_BOUNDARY_SECONDS))
        need(protected==receipt['protected_inventory_sha256'])
        _confirm_progress_drift(contracts,receipt['container_contracts'],len(value['prior_resume']['progress_sha256']))
        return contracts,protected,resume_stem
    if 'numbered_attempt' in value:
        original = _resume_lineage(request, raw, hold, active, stage, stem, value['prior_intent_sha256'],
                                   value['service_instance_sha256'], value['prior_resume'])
        partial_authorization = _prestart_lineage(request, raw, hold, active, stage, stem, value)
        prestart_authorization = _continuation_lineage(
            request, raw, hold, active, stage, stem, value, partial_authorization)
        baseline, count = _numbered_lineage(request, raw, hold, active, stage, stem, value,
                                            prestart_authorization)
        stem = _numbered_stem(stem, value['numbered_attempt'])
        need(not any(p.name.startswith(stem) for p in stage.iterdir()))
        contracts, protected = stopped(request, raw, hold, prepared, active, prior_baseline=original)
        need(protected == prestart_authorization['protected_inventory_sha256'])
        _confirm_progress_drift(contracts, baseline, count)
        return contracts, protected, stem

    lineage=value.get('prior_resume')
    prior_baseline=None
    predecessor=None
    latest=None
    count=len(value['prior_progress_sha256'])
    if 'prior_resume' in value:
        prior_baseline=_resume_lineage(request,raw,hold,active,stage,stem,value['prior_intent_sha256'],
                                      value['service_instance_sha256'],lineage)
        need(type(prior_baseline) is dict)
        if 'prior_partial' in value:
            predecessor=_prestart_lineage(request,raw,hold,active,stage,stem,value)
        if 'prior_prestart' in value:
            latest=_continuation_lineage(request,raw,hold,active,stage,stem,value,predecessor)
        stem+='resume-partial-'
        if predecessor is not None:
            stem+='prestart-'
        if latest is not None:
            stem+='continuation-'
        count=len(lineage['progress_sha256'])
    else:
        stem+='resume-'
    need(not any(p.name.startswith(stem) for p in stage.iterdir()))
    contracts,protected=stopped(request,raw,hold,prepared,active,prior_baseline=prior_baseline)
    if prior_baseline is not None or request.verb==wire.GUEST_START_INITIAL_UNPUBLISHED:
        baseline=prior_baseline if prior_baseline is not None else hold['container_contracts']
        if latest is not None:
            baseline=latest['container_contracts']
            count=len(value['prior_prestart']['progress_sha256'])
            need(protected==latest['protected_inventory_sha256'])
        order=w.RECOVERY_START_ORDER
        need(set(contracts)==set(baseline)==set(order) and all(row['running'] is False for row in contracts.values()))
        changed={role for role,row in contracts.items() if row['started_at']!=baseline[role]['started_at']}
        need(set(order[:count])<=changed<=set(order[:count+1]))
    if predecessor is not None and latest is None:
        need(contracts==predecessor['container_contracts'] and protected==predecessor['protected_inventory_sha256'])
    return contracts,protected,stem

def attest(request,*,service_instance_sha256,prior_intent_sha256,prior_progress_sha256,
           prior_resume=None,prior_partial=None,prior_prestart=None,prior_attempt=None):
    """Trusted operator calls after fresh current-publication proof; metadata only.

    Root must pin the returned authorization hash, this new request nonce, and
    the fixed publication.service_instance_sha256 in every callback. Nothing
    here substitutes for the fresh Native/paired/local-PG checks at dispatch.
    A partial postboot continuation also pins the first authorization, intent,
    and every recorded progress receipt. One further authorization requires
    a partial intent without progress or a subsequent container start. A
    continuation pins the prestart attempt's exact progress and its baseline.
    Numbered attempts require an explicit pin of the immediately preceding
    authorization, intent, and progress. The previous format remains valid.
    """
    from . import native_guest_worker as w
    need(wire._hash(service_instance_sha256) and wire._hash(request.nonce)
         and type(request.sequence) is int and 0<request.sequence<=wire.MAX_REQUESTS)
    prefix(request)
    partial=prior_resume is not None
    with w._guest_mutation_descriptor():
        previous_attempt = latest_attempt(request)
        need(prior_attempt is not None or previous_attempt == 0)
        numbered_attempt = previous_attempt + 1 if prior_attempt is not None else 0
        target=path(request,partial=partial,prestart=prior_partial is not None,
                    continuation=prior_prestart is not None,numbered_attempt=numbered_attempt)
        raw,hold,prepared,active=w._read_initial_recovery_hold(request)
        need(wire._digest(raw)==request.args['hold_sha256'] and not os.path.lexists(target))
        carryover_pin = request.args.get('postboot', {}).get('carryover_receipt_sha256')
        if carryover_pin:
            need(prior_partial is None and prior_prestart is None and prior_attempt is None)
            stage, stem = w.ROOT/request.binding.attempt_id, prefix(request)
        else:
            stage,stem=prior(request,hold,active,prior_intent_sha256,prior_progress_sha256)
        value={'schema_version':1,'request':w._scope(request),'args':request.args,
            'hold_sha256':wire._digest(raw),'service_instance_sha256':service_instance_sha256,
            'prior_intent_sha256':prior_intent_sha256,'prior_progress_sha256':prior_progress_sha256,
            'containers':hold['containers'],'authority_files':active['authority_files']}
        if carryover_pin:
            value['carryover_receipt_sha256'] = carryover_pin
        if partial:
            value['prior_resume']=prior_resume
        if prior_partial is not None:
            value['prior_partial']=prior_partial
        if prior_prestart is not None:
            value['prior_prestart']=prior_prestart
        if numbered_attempt:
            value.update(prior_attempt=prior_attempt, numbered_attempt=numbered_attempt)
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

def load(request,raw,hold,prepared,active,*,partial=False,prestart=False,continuation=False,
         numbered_attempt=0):
    """Called only under the installed worker mutation descriptor."""
    from . import native_guest_worker as w
    need(latest_attempt(request) == numbered_attempt)
    target=path(request,partial=partial,prestart=prestart,continuation=continuation,
                numbered_attempt=numbered_attempt)
    body=w.runtime._read(target,0,{0o400},limit=131072);value=decode(body)
    _validate_authorization(request,raw,hold,active,value,partial=partial,prestart=prestart,
                            continuation=continuation,numbered_attempt=numbered_attempt)
    if 'carryover_receipt_sha256' in value:
        stage, stem = w.ROOT/request.binding.attempt_id, prefix(request)
    else:
        stage,stem=prior(request,hold,active,value['prior_intent_sha256'],value['prior_progress_sha256'])
    contracts,protected,stem=_authorization_baseline(request,raw,hold,prepared,active,value,stage,stem)
    need(value['container_contracts']==contracts and value['protected_inventory_sha256']==protected)
    return body,value,stem

def preserved(request,body,value,hold,active,raw):
    from . import native_guest_worker as w
    partial='prior_resume' in value
    prestart='prior_partial' in value
    continuation='prior_prestart' in value
    numbered_attempt=value.get('numbered_attempt', 0)
    need(latest_attempt(request) == numbered_attempt)
    target=path(request,partial=partial,prestart=prestart,continuation=continuation,
                numbered_attempt=numbered_attempt)
    need(w.runtime._read(target,0,{0o400},limit=131072)==body)
    if 'carryover_receipt_sha256' in value:
        _carryover_lineage(request,raw,hold,active,value,w.ROOT/request.binding.attempt_id,prefix(request),
                           require_stopped=False)
        return
    stage,stem=prior(request,hold,active,value['prior_intent_sha256'],value['prior_progress_sha256'])
    if partial:
        _resume_lineage(request,raw,hold,active,stage,stem,value['prior_intent_sha256'],
                        value['service_instance_sha256'],value['prior_resume'])
    if prestart:
        predecessor=_prestart_lineage(request,raw,hold,active,stage,stem,value)
        if continuation:
            prestart_authorization = _continuation_lineage(request,raw,hold,active,stage,stem,value,predecessor)
            if numbered_attempt:
                _numbered_lineage(request,raw,hold,active,stage,stem,value,prestart_authorization)


def _latest_history(request, raw, hold, active, link):
    """Authenticate the previous app run and every immutable predecessor."""
    from . import native_guest_worker as w
    need(type(link) is dict and set(link) in (LINK_FIELDS, COMPLETED_LINK_FIELDS))
    completed='done_sha256' in link
    if completed:
        need(wire._hash(link['done_sha256']))
    numbered = latest_attempt(request)
    continuation = bool(numbered) or path(request, partial=True, prestart=True, continuation=True).exists()
    prestart = continuation or path(request, partial=True, prestart=True).exists()
    partial = prestart or path(request, partial=True).exists()
    body = w.runtime._read(path(request, partial=partial, prestart=prestart,
        continuation=continuation, numbered_attempt=numbered), 0, {0o400}, limit=131072)
    need(wire._digest(body) == link['authorization_sha256'])
    auth = decode(body)
    scope_record = auth.get('request', {})
    need(scope_record == w._scope(request))
    previous = replace(request, nonce=scope_record.get('nonce'), sequence=scope_record.get('sequence'))
    _validate_authorization(previous, raw, hold, active, auth, partial=partial, prestart=prestart,
                            continuation=continuation, numbered_attempt=numbered)
    if 'carryover_receipt_sha256' in auth:
        need(completed)
        stage,stem=w.ROOT/previous.binding.attempt_id,prefix(previous)
        _carryover_lineage(previous,raw,hold,active,auth,stage,stem,require_stopped=False)
    else:
        stage, stem = prior(previous, hold, active, auth['prior_intent_sha256'], auth['prior_progress_sha256'])
        if partial:
            _resume_lineage(previous, raw, hold, active, stage, stem, auth['prior_intent_sha256'],
                            auth['service_instance_sha256'], auth['prior_resume'])
        if prestart:
            partial_auth = _prestart_lineage(previous, raw, hold, active, stage, stem, auth)
        if continuation:
            prestart_auth = _continuation_lineage(previous, raw, hold, active, stage, stem, auth, partial_auth)
        if numbered:
            _numbered_lineage(previous, raw, hold, active, stage, stem, auth, prestart_auth)
    head = (_numbered_stem(stem, numbered) if numbered else stem + 'resume-' +
            ('partial-' if partial else '') + ('prestart-' if prestart else '') +
            ('continuation-' if continuation else ''))
    _prior_attempt(previous, hold, active, stage, head, link['intent_sha256'], link['progress_sha256'],
                   None, authorization_pin=link['authorization_sha256'],done_pin=link.get('done_sha256'))
    if not completed:
        need(len(link['progress_sha256']) < len(w.RECOVERY_START_ORDER))
    return auth
