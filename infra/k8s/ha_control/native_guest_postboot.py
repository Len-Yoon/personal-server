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

def current(request, *, inventory=None):
    raw,hold,prepared,active=worker._read_initial_recovery_hold(request)
    worker._need(wire._digest(raw)==request.args['hold_sha256'],ERROR)
    worker._assert_car_route_blocked(request,raw,prepared,active)
    authorities={worker.runtime.ROOT/name:pin for name,pin in active['authority_files'].items()}
    worker._need(set(authorities)==set(worker.live._authority_paths().values())
        and all(wire._digest(worker.runtime._read(path,0,{0o444}))==pin for path,pin in authorities.items()),ERROR)
    contract,protected=worker._initial_recovery_inventory(request.binding,hold['containers'],
        request.args['protected_inventory_sha256'],None,(hold['container_contracts'],request.args['protected_inventory_sha256']),
        inventory=inventory)
    return raw,hold,contract,protected

def closed_revision(request,args=None):
    args=request.args if args is None else args
    if 'predecessor_postboot_attempt_id' in args:
        return _sameboot_closed_revision(request, args)
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


def _sameboot_closed_revision(request, args):
    from .initial_postboot import RESUME_KIND, stopped_attempt
    from .initial_unpublished import derive_predecessor

    plan_pin = args['predecessor_plan_sha256']
    terminal_pin = args['predecessor_terminal_sha256']
    worker._need(wire._hash(plan_pin) and wire._hash(terminal_pin), ERROR)
    terminal = worker.runtime._read(worker.ROOT / ('predecessor-plan-' + plan_pin + '.json'), 0, {0o400}, limit=65536)
    pinned = worker.runtime._read(worker.ROOT / ('predecessor-terminal-' + terminal_pin + '.json'), 0, {0o400}, limit=65536)
    worker._need(terminal == pinned, ERROR)
    closed, _, _ = derive_predecessor(terminal, terminal_pin, published=True)
    previous = json.loads(terminal)
    worker._need(previous['kind'] == RESUME_KIND
        and previous['controller_plan_sha256'] == plan_pin
        and previous['postboot']['attempt_id'] == args['predecessor_postboot_attempt_id']
        and args['postboot_attempt_id'] not in {previous['postboot']['attempt_id'], stopped_attempt(previous['postboot'])}
        and previous['recovery']['attempt_id'] == args['recovery_attempt_id']
        and previous['attempt_id'] == request.binding.attempt_id
        and previous['legacy_plan_sha256'] == request.binding.plan_sha256, ERROR)
    return closed


def stop(request, assert_closed):
    """Caller must freshly prove the exact derived closed A/B pair at every boundary.

    This includes current catalog/sync/replay and the entire unresolved effects
    ledger: only the original exact two quarantined car entries may remain.
    A digest of the control row alone does not satisfy this callback contract.
    Save-VM additionally requires another full paired read after this receipt.
    """
    worker._need(request.verb in wire.POSTBOOT_STOP_VERBS and callable(assert_closed),ERROR)
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

def observe(request, attempt, expected, *, require_stopped=True, _runtime=True):
    worker._need(wire._hash(expected),ERROR)
    _,done_path=paths(request,attempt)
    raw,record=worker._read_json(done_path)
    if _runtime:
        holdraw,hold,contracts,protected=current(request)
    else:
        worker._need(not require_stopped, ERROR)
        holdraw,hold,_,_=worker._read_initial_recovery_hold(request)
        contracts,protected=None,request.args['protected_inventory_sha256']
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


CARRYOVER_KIND = 'authenticated-postboot-app-carryover-v1'
DRAFT_PIN = '0' * 64
CARRYOVER_FIELDS = {'schema_version', 'kind', 'binding', 'postboot', 'recovery_args',
    'predecessor_postboot', 'predecessor_request', 'history', 'hold_sha256', 'containers',
    'container_contracts', 'protected_inventory_sha256'}


def carryover_path(request, attempt):
    worker._need(wire._hash(attempt), ERROR)
    return worker.ROOT / request.binding.attempt_id / ('postboot-' + attempt + '-carryover-stopped.json')


def _carryover_scope(request):
    from .initial_postboot import scope, published_postboot
    value = scope({'initial_recovery_postboot': request.args['postboot'],
                   'initial_cutover_recovery': {'attempt_id': request.args['recovery_attempt_id']},
                   'initial_cutover_handoff': {'attempt_id': request.binding.attempt_id}})
    worker._need(published_postboot(value), ERROR)
    value.pop('carryover_receipt_sha256', None)
    # The new predecessor seal contains this receipt's digest; exclude that
    # later seal pin to avoid a circular hash. The final plan pins both.
    value.pop('predecessor_seal_sha256', None)
    return value


def _carryover_history(request, record):
    """Revalidate the authenticated safe plan projection and exact old history pins."""
    from . import initial_app_resume as resume
    from .initial_postboot import scope, stopped_attempt
    from dataclasses import replace
    worker._need(set(record) == CARRYOVER_FIELDS and record['kind'] == CARRYOVER_KIND
        and type(record['schema_version']) is int and record['schema_version'] == 1
        and record['binding'] == request.binding.fields()
        and type(record['predecessor_postboot']) is dict
        and type(record['predecessor_request']) is dict, ERROR)
    postboot = record['postboot']
    worker._need(type(postboot) is dict and wire._hash(postboot['predecessor_plan_sha256']), ERROR)
    previous_postboot = scope({'initial_recovery_postboot': record['predecessor_postboot'],
        'initial_cutover_recovery': {'attempt_id': record['recovery_args']['recovery_attempt_id']},
        'initial_cutover_handoff': {'attempt_id': request.binding.attempt_id}})
    old = record['predecessor_request']
    worker._need(set(old) == {'scope', 'args'} and type(old['scope']) is dict and type(old['args']) is dict, ERROR)
    previous = replace(request, verb=wire.GUEST_START_INITIAL_POSTBOOT, args=old['args'],
                       nonce=old['scope'].get('nonce'), sequence=old['scope'].get('sequence'))
    wire._args(previous.verb, previous.node, previous.args)
    worker._need(worker._scope(previous) == old['scope']
        and wire._hash(previous.nonce) and type(previous.sequence) is int and 0 < previous.sequence <= wire.MAX_REQUESTS
        and previous.args['postboot'] == previous_postboot
        and previous.args['postboot_plan_sha256'] == postboot['predecessor_plan_sha256']
        and postboot['attempt_id'] != previous_postboot['attempt_id']
        and postboot['stopped_attempt_id'] == stopped_attempt(previous_postboot)
        and all(previous.args[key] == record['recovery_args'][key] for key in (*wire.INITIAL_RECOVERY_ARGS, 'hold_sha256'))
        and previous.args['postboot_stopped_receipt_sha256'] == postboot['stopped_receipt_sha256'], ERROR)
    raw, hold, prepared, active = worker._read_initial_recovery_hold(previous)
    worker._need(wire._digest(raw) == record['hold_sha256'] == record['recovery_args']['hold_sha256']
        and record['containers'] == hold['containers'], ERROR)
    auth = resume._latest_history(previous, raw, hold, active, record['history'])
    # This verifies the original stopped receipt with its original closed revision.
    original = observe(previous, postboot['stopped_attempt_id'], postboot['stopped_receipt_sha256'], require_stopped=False, _runtime=False)
    worker._need(auth['protected_inventory_sha256'] == original['protected_inventory_sha256']
        == record['protected_inventory_sha256'] == record['recovery_args']['protected_inventory_sha256'], ERROR)
    resume._confirm_progress_drift(record['container_contracts'], auth['container_contracts'],
                                  len(record['history']['progress_sha256']))
    return hold


def attest_carryover(request, *, predecessor_plan_raw, predecessor_request, history):
    """Trusted ROOT operation creates admission metadata, never a new stop or app start.

    The caller separately pins this digest in the new plan and observer. The
    original stopped receipt and every attempted writer record are preserved.
    The final plan hash and new writer publication do not exist at this stage.
    """
    worker._need(request.verb == wire.GUEST_START_INITIAL_POSTBOOT
        and type(predecessor_plan_raw) is bytes and type(predecessor_request) is wire.Request, ERROR)
    from . import initial_app_resume as resume
    from .initial_postboot import scope
    postboot = _carryover_scope(request)
    # Authenticate the exact input bytes in memory. Persist only the hash-only
    # postboot projection; the Windows plan can contain connection settings.
    worker._need(wire._digest(predecessor_plan_raw) == postboot['predecessor_plan_sha256']
        == predecessor_request.args['postboot_plan_sha256'], ERROR)
    old_plan = resume.decode(predecessor_plan_raw)
    previous_postboot = scope(old_plan)
    worker._need(previous_postboot == predecessor_request.args['postboot']
        and old_plan['installation_id'] == request.args['installation_id']
        and old_plan['initial_cutover_handoff']['attempt_id'] == request.binding.attempt_id
        and old_plan['initial_cutover_handoff']['legacy_plan_sha256'] == request.binding.plan_sha256
        and old_plan['initial_cutover_recovery']['attempt_id'] == request.args['recovery_attempt_id'], ERROR)
    with worker._guest_mutation_descriptor():
        raw, hold, contracts, protected = current(request)
        record = {'schema_version': 1, 'kind': CARRYOVER_KIND, 'binding': request.binding.fields(),
            'postboot': postboot, 'recovery_args': {key: request.args[key] for key in (*wire.INITIAL_RECOVERY_ARGS, 'hold_sha256')},
            'predecessor_postboot': previous_postboot,
            'predecessor_request': {'scope': worker._scope(predecessor_request), 'args': predecessor_request.args},
            'history': history, 'hold_sha256': wire._digest(raw), 'containers': hold['containers'],
            'container_contracts': contracts, 'protected_inventory_sha256': protected}
        _carryover_history(request, record)
        worker._need(all(row['running'] is False for row in contracts.values()), ERROR)
        target = carryover_path(request, postboot['attempt_id'])
        body = wire._bytes(record)
        worker._need(0 < len(body) <= 262144, ERROR)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(body); stream.flush(); os.fsync(stream.fileno())
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(directory)
        finally: os.close(directory)
        worker._need(worker.runtime._read(target, 0, {0o400}, limit=262144) == body, ERROR)
        return wire.Receipt('PASS', wire._digest(body))


def observe_carryover(request, attempt, expected, *, require_stopped=True, inventory=None):
    """Only an explicitly pinned new-attempt admission can relax original StartedAt."""
    from . import initial_app_resume as resume
    worker._need(wire._hash(expected), ERROR)
    raw = worker.runtime._read(carryover_path(request, attempt), 0, {0o400}, limit=262144)
    worker._need(wire._digest(raw) == expected, ERROR)
    record = resume.decode(raw)
    worker._need(record.get('postboot', {}).get('attempt_id') == attempt, ERROR)
    _carryover_history(request, record)
    worker._need(all(request.args[key] == record['recovery_args'][key]
        for key in (*wire.INITIAL_RECOVERY_ARGS, 'hold_sha256'))
        and request.args['postboot_stopped_receipt_sha256'] == record['postboot']['stopped_receipt_sha256'], ERROR)
    if request.verb == wire.GUEST_START_INITIAL_POSTBOOT:
        worker._need(_carryover_scope(request) == record['postboot']
            and request.args['postboot'].get('carryover_receipt_sha256') == expected, ERROR)
    if require_stopped:
        _, _, contracts, protected = current(request, inventory=inventory)
        worker._need(protected == record['protected_inventory_sha256'], ERROR)
        worker._need(contracts == record['container_contracts']
            and all(row['running'] is False for row in contracts.values()), ERROR)
    return record


def _carryover_input(path, expected, *, limit):
    """Only immutable ROOT inputs; pins refer to exact bytes, including newlines."""
    from pathlib import Path
    worker._need(wire._hash(expected), ERROR)
    raw = worker.runtime._read(Path(path), 0, {0o400}, limit=limit)
    worker._need(wire._digest(raw) == expected, ERROR)
    return raw


def _carryover_request(raw):
    import io
    stream = io.BytesIO(raw)
    request = worker.read_request(stream)
    # Start requests normally use a duplex stream. A local attestation input
    # must contain exactly one canonical request, without a second frame.
    worker._need(not stream.read(1) and request.node == 'A'
        and request.verb == wire.GUEST_START_INITIAL_POSTBOOT, ERROR)
    return request


def main(argv=None):
    """Issue only carryover metadata from a trusted, ROOT-owned installed package.

    Run as `python -m ha_control.native_guest_postboot` with that package on the
    trusted interpreter path. This is not an SSH worker verb and grants no new
    sudo entry. It never starts apps, changes a DB, or edits predecessor records.
    The new request is an explicit draft: final plan, predecessor seal and
    carryover receipt hashes must be DRAFT_PIN. Those hashes do not exist until
    after issuance; normal startup still authenticates the final real hashes.
    """
    import argparse
    import sys
    from .initial_app_resume import decode
    parser = argparse.ArgumentParser(description='Issue immutable postboot carryover metadata only.')
    for name in ('request', 'predecessor-request', 'predecessor-plan', 'history'):
        parser.add_argument('--' + name, required=True)
        parser.add_argument('--' + name + '-sha256', required=True)
    args = parser.parse_args(argv)
    try:
        worker._need(os.geteuid() == 0, ERROR)
        inputs = {name: _carryover_input(getattr(args, name), getattr(args, name + '_sha256'),
            limit=wire.FRAME_BYTES if name in {'request', 'predecessor_request'} else 131072)
            for name in ('request', 'predecessor_request', 'predecessor_plan', 'history')}
        request = _carryover_request(inputs['request'])
        predecessor = _carryover_request(inputs['predecessor_request'])
        worker._need(request.binding == predecessor.binding
            and request.args['postboot_plan_sha256'] == DRAFT_PIN
            and request.args['postboot'].get('carryover_receipt_sha256') == DRAFT_PIN
            and request.args['postboot']['predecessor_seal_sha256'] == DRAFT_PIN, ERROR)
        receipt = attest_carryover(request, predecessor_plan_raw=inputs['predecessor_plan'],
            predecessor_request=predecessor, history=decode(inputs['history']))
    except (TransitionBlocked, wire.BridgeRejected, OSError, KeyError, TypeError, ValueError):
        # Plans can contain connection settings. Never echo input bytes, paths,
        # or exception text, including failures in the underlying reader.
        print(ERROR, file=sys.stderr)
        return 1
    print(json.dumps({'status': receipt.status, 'receipt_sha256': receipt.receipt_sha256}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
