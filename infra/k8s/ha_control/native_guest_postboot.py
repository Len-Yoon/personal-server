"""Bounded pre-save stop and immutable postboot-only admission; never edits the original hold."""
import json
import os
import time
from . import native_guest_worker as worker, native_stdio_bridge as wire
from .controller import TransitionBlocked

ERROR='guest_postboot_stopped_unconfirmed'

def paths(request, attempt):
    worker._need(wire._hash(attempt),ERROR)
    stage=worker.ROOT/request.binding.attempt_id
    return stage/('postboot-'+attempt+'-stop-intent.json'),stage/('postboot-'+attempt+'-stopped.json')

def current(request):
    raw,hold,prepared,active=worker._read_initial_recovery_hold(request)
    worker._need(wire._digest(raw)==request.args['hold_sha256'],ERROR)
    worker._assert_car_route_blocked(request,raw,prepared,active)
    authorities={worker.runtime.ROOT/name:pin for name,pin in active['authority_files'].items()}
    worker._need(set(authorities)==set(worker.live._authority_paths().values())
        and all(wire._digest(worker.runtime._read(path,0,{0o444}))==pin for path,pin in authorities.items()),ERROR)
    contract,protected=worker._initial_recovery_inventory(request.binding,hold['containers'],
        request.args['protected_inventory_sha256'],None,(hold['container_contracts'],request.args['protected_inventory_sha256']))
    return raw,hold,contract,protected

def closed_revision(request,args=None):
    args=request.args if args is None else args
    if 'predecessor_unpublished_attempt_id' not in args:
        return 7
    from .initial_unpublished import derive_predecessor
    plan_pin=args['predecessor_plan_sha256']
    worker._need(wire._hash(plan_pin),ERROR)
    # This is the CURRENT published writer's terminal after close, not the
    # older terminal used to start that writer. Both copies are CreateNew,
    # independent regular ROOT-owned 0400 files (runtime._read rejects links).
    raw=worker.runtime._read(worker.ROOT/('predecessor-plan-'+plan_pin+'.json'),0,{0o400},limit=65536)
    pin=wire._digest(raw)
    worker._need(worker.runtime._read(worker.ROOT/('predecessor-terminal-'+pin+'.json'),0,{0o400},limit=65536)==raw,ERROR)
    revision=derive_predecessor(raw,pin,published=True)[0]
    prior=json.loads(raw)
    worker._need(prior['kind']=='initial-wsl-cutover-unpublished'
        and prior['controller_plan_sha256']==plan_pin
        and prior['unpublished']['attempt_id']==args['predecessor_unpublished_attempt_id']
        and prior['recovery']['attempt_id']==args['recovery_attempt_id']
        and prior['attempt_id']==request.binding.attempt_id
        and prior['legacy_plan_sha256']==request.binding.plan_sha256,ERROR)
    return revision


def stop(request, assert_closed):
    """Caller must freshly prove the exact derived closed A/B pair at every boundary.

    This includes current catalog/sync/replay and the entire unresolved effects
    ledger: only the original exact two quarantined car entries may remain.
    A digest of the control row alone does not satisfy this callback contract.
    Save-VM additionally requires another full paired read after this receipt.
    """
    worker._need(request.verb in {wire.GUEST_QUIESCE_INITIAL_POSTBOOT,wire.GUEST_QUIESCE_POSTBOOT_UNPUBLISHED} and callable(assert_closed),ERROR)
    args=request.args
    expected_closed=closed_revision(request)
    with worker._guest_mutation_descriptor():
        raw,hold,initial,protected=current(request)
        intent_path,done_path=paths(request,args['postboot_attempt_id'])
        worker._need(not any(os.path.lexists(path) for path in (intent_path,done_path)),ERROR)
        def check(boundary):
            deadline=time.monotonic()+wire.ACTIVATION_GUEST_CHECK_SECONDS
            current(request)
            challenge=wire._digest(wire._bytes(worker._scope(request)|{'args':args,'boundary':boundary,'closed_revision':expected_closed}))
            proof=assert_closed(challenge,boundary)
            worker._need(wire._hash(proof) and time.monotonic()<deadline,ERROR)
            current(request)
            worker._need(time.monotonic()<deadline,ERROR)
            return proof
        proof=check(wire.POSTBOOT_STOP_BOUNDARIES[0])
        worker.runtime._write(intent_path,wire._bytes({'schema_version':1,**worker._scope(request),'args':args,
            'closed_proof_sha256':proof,'containers':hold['containers'],'automatic_retry_permitted':False}))
        broker=hold['containers']['ha-broker']['container_id']
        worker.runtime._docker('stop','--signal=SIGTERM','--timeout=-1',broker,timeout=25)
        check(wire.POSTBOOT_STOP_BOUNDARIES[1])
        ids=sorted(value['container_id'] for role,value in hold['containers'].items() if role!='ha-broker')
        worker.runtime._docker('stop','--signal=SIGTERM','--timeout=-1',*ids,timeout=25)
        proof=check(wire.POSTBOOT_STOP_BOUNDARIES[2])
        _,_,contracts,_=current(request)
        worker._need(all(record['running'] is False for record in contracts.values()),ERROR)
        receipt=wire._bytes({'schema_version':1,'binding':request.binding.fields(),'args':args,
            'hold_sha256':wire._digest(raw),'containers':hold['containers'],'container_contracts':contracts,
            'protected_inventory_sha256':protected,'closed_revision':expected_closed,'closed_proof_sha256':proof})
        worker.runtime._write(done_path,receipt)
        observe(request,args['postboot_attempt_id'],wire._digest(receipt))
        return wire.Receipt('PASS',wire._digest(receipt))

def observe(request, attempt, expected, *, require_stopped=True):
    worker._need(wire._hash(expected),ERROR)
    _,done_path=paths(request,attempt)
    raw,record=worker._read_json(done_path)
    holdraw,hold,contracts,protected=current(request)
    keys={'schema_version','binding','args','hold_sha256','containers','container_contracts',
          'protected_inventory_sha256','closed_revision','closed_proof_sha256'}
    worker._need(wire._digest(raw)==expected and type(record) is dict and set(record)==keys
        and type(record['schema_version']) is int and record['schema_version']==1
        and record['binding']==request.binding.fields() and type(record['args']) is dict
        and record['args'].get('postboot_attempt_id')==attempt
        and all(record['args'].get(key)==request.args[key] for key in (*wire.INITIAL_RECOVERY_ARGS,'hold_sha256'))
        and record['hold_sha256']==wire._digest(holdraw) and record['containers']==hold['containers']
        and record['protected_inventory_sha256']==protected and type(record['closed_revision']) is int
        and record['closed_revision']==closed_revision(request,record['args']) and wire._hash(record['closed_proof_sha256'])
        and type(record['container_contracts']) is dict
        and set(record['container_contracts'])==set(hold['containers']),ERROR)
    if require_stopped:
        worker._need(record['container_contracts']==contracts and all(value['running'] is False for value in contracts.values()),ERROR)
    return record
