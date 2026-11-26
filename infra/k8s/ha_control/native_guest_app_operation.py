"""Typed attestation and completion frames for the Windows boot SSH key."""

from dataclasses import dataclass
import json

from . import native_stdio_bridge as wire
from .initial_postboot import published_postboot


KIND = 'guest_boot_app_operation'
ATTEST = 'attest'
READ_AUTH = 'read_authorization'
COMPLETE = 'completion'
MAX_REQUEST_BYTES = 16384
ERROR = 'guest_boot_app_operation_rejected'
AUTH_FIELDS = {'authorization_sha256', 'prior_intent_sha256', 'prior_progress_sha256'}


def _need(condition):
    wire._need(condition, ERROR)


def _progress(value):
    return (type(value) is dict and len(value) <= len(wire.RECOVERY_START_ORDER)
            and all(type(name) is str and 0 < len(name) <= 128 and wire._hash(pin)
                    for name, pin in value.items()))


def _scope(request):
    return {'binding': request.binding.fields(), 'node': request.node, 'verb': request.verb,
            'sequence': request.sequence, 'nonce': request.nonce, 'args': request.args}


@dataclass(frozen=True)
class Operation:
    name: str
    request: wire.Request
    inputs: dict
    frame_sha256: str


def decode_request(raw):
    _need(type(raw) is bytes and 0 < len(raw) <= MAX_REQUEST_BYTES)
    frame = json.loads(raw.decode('ascii'), object_pairs_hook=wire._pairs, parse_constant=wire._reject_constant)
    _need(type(frame) is dict and set(frame) == {'version', 'kind', 'operation', 'request', 'inputs'}
          and type(frame['version']) is int and frame['version'] == wire.VERSION
          and frame['kind'] == KIND and wire._bytes(frame) == raw)
    name, body, inputs = frame['operation'], frame['request'], frame['inputs']
    _need(type(name) is str and name in (ATTEST, READ_AUTH, COMPLETE) and type(body) is dict
          and set(body) == {'binding', 'node', 'verb', 'sequence', 'nonce', 'args'}
          and type(body['binding']) is dict and type(inputs) is dict)
    request = wire.Request(**(body | {'binding': wire.Binding(**body['binding'])}))
    request.binding.fields()
    _need(request.node == 'A' and request.verb == wire.GUEST_START_INITIAL_POSTBOOT
          and type(request.sequence) is int and 0 < request.sequence <= wire.MAX_REQUESTS and wire._hash(request.nonce))
    wire._args(request.verb, request.node, request.args)
    _need(published_postboot(request.args['postboot']) and wire._hash(request.args['postboot'].get('carryover_receipt_sha256')))

    if name in (ATTEST, READ_AUTH):
        _need(set(inputs) == {'instance', 'intent', 'progress', 'prior_resume'}
              and wire._hash(inputs['intent']) and _progress(inputs['progress'])
              and (name != READ_AUTH or inputs['prior_resume'] is None))
    else:
        _need(set(inputs) == {'instance', 'authorization', 'prior_resume', 'expected', 'budget'}
              and type(inputs['authorization']) is dict and set(inputs['authorization']) == AUTH_FIELDS
              and wire._hash(inputs['authorization']['authorization_sha256'])
              and wire._hash(inputs['authorization']['prior_intent_sha256'])
              and _progress(inputs['authorization']['prior_progress_sha256'])
              and (inputs['expected'] is None or wire._hash(inputs['expected']))
              and type(inputs['budget']) in (int, float) and 0 < inputs['budget'] <= 120)
    _need(wire._hash(inputs['instance']))
    prior = inputs['prior_resume']
    _need(prior is None or (type(prior) is dict and set(prior) == {'authorization_sha256', 'intent_sha256', 'progress_sha256'}
                            and wire._hash(prior['authorization_sha256']) and wire._hash(prior['intent_sha256'])
                            and _progress(prior['progress_sha256'])))
    return Operation(name, request, inputs, wire._digest(raw))


def encode_request(name, request, inputs):
    _need(type(request) is wire.Request)
    raw = wire._bytes({'version': wire.VERSION, 'kind': KIND, 'operation': name, 'request': _scope(request), 'inputs': inputs})
    decode_request(raw)
    return raw


def reply(operation):
    from . import initial_app_resume

    inputs = operation.inputs
    if operation.name == ATTEST:
        result = initial_app_resume.attest(operation.request, service_instance_sha256=inputs['instance'],
                                           prior_intent_sha256=inputs['intent'], prior_progress_sha256=inputs['progress'],
                                           prior_resume=inputs['prior_resume'])
    elif operation.name == READ_AUTH:
        result = read_authorization(operation.request, instance=inputs['instance'], intent=inputs['intent'],
                                    progress=inputs['progress'])
    else:
        _need(operation.name == COMPLETE)
        result = observe_completion(operation.request, **inputs)
    # Never copy raw journals or the typed request into the response.
    raw = wire._bytes({'version': wire.VERSION, 'kind': KIND, 'operation': operation.name,
                       'frame_sha256': operation.frame_sha256, 'result': result})
    _need(len(raw) <= wire.FRAME_BYTES)
    return raw


def decode_reply(raw, name, request_raw):
    _need(type(raw) is bytes and 0 < len(raw) <= wire.FRAME_BYTES)
    frame = json.loads(raw.decode('ascii'), object_pairs_hook=wire._pairs, parse_constant=wire._reject_constant)
    _need(type(frame) is dict and wire._bytes(frame) == raw and type(frame.get('result')) is dict
          and wire._matches(frame, {'version': wire.VERSION, 'kind': KIND, 'operation': name,
                                    'frame_sha256': wire._digest(request_raw), 'result': frame['result']}))
    return frame['result']


def read_authorization(request, *, instance, intent, progress):
    """Recover the exact persisted initial attestation without publishing it again."""
    import os

    from . import initial_app_resume as resume, native_guest_worker as worker

    _need(os.geteuid() == 0 and request.node == 'A' and request.verb == wire.GUEST_START_INITIAL_POSTBOOT
          and wire._digest(worker.runtime.BOOT_ID.read_bytes()) == request.binding.boot('A'))
    with worker._guest_mutation_descriptor(wait=2):
        raw, hold, _, active = worker._read_initial_recovery_hold(request)
        target = resume.path(request)
        body = worker.runtime._read(target, 0, {0o400}, limit=131072)
        saved = resume.decode(body)
        resume._validate_authorization(request, raw, hold, active, saved, partial=False)
        resume.preserved(request, body, saved, hold, active, raw)
        _need(saved['service_instance_sha256'] == instance and saved['prior_intent_sha256'] == intent
              and saved['prior_progress_sha256'] == progress
              and worker.runtime._read(target, 0, {0o400}, limit=131072) == body)
        return {'schema_version': 1, 'authorization_sha256': wire._digest(body), 'request_nonce': request.nonce,
                'baseline_sha256': wire._digest(wire._bytes(saved['container_contracts'])),
                'prior_intent_sha256': intent, 'prior_progress_sha256': progress, 'service_instance_sha256': instance,
                'container_count': 8, 'containers_started': False, 'database_changed': False}


def observe_completion(request, *, instance, authorization, prior_resume, expected, budget):
    """Read native A app completion, preserving every journal and authority record."""
    import os,time
    from ha_control import native_guest_worker as w,initial_app_resume as resume,native_stdio_bridge as wire
    deadline=time.monotonic()+budget
    def require(value):
        if not value:raise ValueError('successor_app_completion_unverified')
    require(os.geteuid()==0 and request.node=='A' and request.verb==wire.GUEST_START_INITIAL_POSTBOOT)
    wire._args(request.verb,request.node,request.args)
    require(wire._digest(w.runtime.BOOT_ID.read_bytes())==request.binding.boot('A'))
    with w._guest_mutation_descriptor(wait=2):
        raw,hold,prepared,active=w._read_initial_recovery_hold(request)
        stage=w.ROOT/request.binding.attempt_id
        numbered=resume.latest_attempt(request)
        continuation=bool(numbered) or resume.path(request,partial=True,prestart=True,continuation=True).exists()
        prestart=continuation or resume.path(request,partial=True,prestart=True).exists()
        partial=prestart or resume.path(request,partial=True).exists()
        target=resume.path(request,partial=partial,prestart=prestart,continuation=continuation,numbered_attempt=numbered)
        authorization_raw=w.runtime._read(target,0,{0o400},limit=131072)
        recorded_authorization=resume.decode(authorization_raw)
        require(wire._digest(authorization_raw)==authorization['authorization_sha256'])
        require(recorded_authorization['request']==w._scope(request) and recorded_authorization['args']==request.args
            and recorded_authorization['service_instance_sha256']==instance
            and recorded_authorization['prior_intent_sha256']==authorization['prior_intent_sha256']
            and recorded_authorization['prior_progress_sha256']==authorization['prior_progress_sha256']
            and recorded_authorization.get('prior_resume')==prior_resume)
        base=resume.prefix(request)
        stem=(resume._numbered_stem(base,numbered) if numbered else base+'resume-'+
              ('partial-' if partial else '')+('prestart-' if prestart else '')+('continuation-' if continuation else ''))
        order=w.RECOVERY_START_ORDER
        names=['intent.json','done.json']+['progress-'+role+'.json' for role in order]
        journals={name:w.runtime._read(stage/(stem+name),0,{0o600},limit=131072) for name in names}
        history={'authorization_sha256':wire._digest(authorization_raw),
                 'intent_sha256':wire._digest(journals['intent.json']),'done_sha256':wire._digest(journals['done.json']),
                 'progress_sha256':{name:wire._digest(journals[name]) for name in names if name.startswith('progress-')}}
        require(expected is None or history['done_sha256']==expected)
        require(resume._latest_history(request,raw,hold,active,history)==recorded_authorization)
        worker=w.GuestWorker(request)
        postgres,pins,_=worker._prepared_pins(prepared['manifest_sha256'])
        require(pins.installation_id==request.args['installation_id'] and pins.node=='A'
            and pins.attempt_id==request.binding.attempt_id)
        authorities={w.runtime.ROOT/name:pin for name,pin in active['authority_files'].items()}
        require(set(authorities)==set(w.live._authority_paths().values()))
        def inventory():
            require(time.monotonic()<deadline)
            objects=w.restore._observer_inventory(deadline)
            contracts,protected=w._initial_recovery_inventory(request.binding,hold['containers'],
                request.args['protected_inventory_sha256'],None,
                (recorded_authorization['container_contracts'],recorded_authorization['protected_inventory_sha256']),inventory=lambda:objects)
            require(protected==recorded_authorization['protected_inventory_sha256']
                and all(row['running'] is True and row['started_at']!=recorded_authorization['container_contracts'][role]['started_at']
                        for role,row in contracts.items()))
            instances={}
            for role,pin in hold['containers'].items():
                item=objects[pin['container_id']];state=item['State']
                require(item['Id']==pin['container_id'] and item['Image']==pin['image_id']
                    and type(state['Pid']) is int and state['Pid']>0)
                instances[role]=[pin['container_id'],pin['image_id'],state['Pid'],state['StartedAt']]
            w._assert_car_route_blocked(request,raw,prepared,active)
            require(all(wire._digest(w.runtime._read(path,0,{0o444}))==pin for path,pin in authorities.items()))
            return contracts,instances
        def lease(boundary):
            challenge=wire._digest(wire._bytes({'request':w._scope(request),'history':history,'boundary':boundary}))
            proof=w.live.observe_postboot_a_lease(pins,postgres,recovery_attempt_id=request.args['recovery_attempt_id'],
                postboot=request.args['postboot'],postboot_plan_sha256=request.args['postboot_plan_sha256'],
                challenge_sha256=challenge,deadline=min(deadline,time.monotonic()+.5),minimum_remaining_seconds=1)
            require(wire._hash(proof) and time.monotonic()<deadline)
            return proof
        before=inventory();lease('completion_before')
        context,leaf=w.activation._tls(prepared)
        spec_raw=w.runtime._read(stage/'guest-spec.json',0,{0o400,0o600})
        require(wire._digest(spec_raw)==pins.spec_sha256)
        spec=resume.decode(spec_raw)
        require(spec['node']=='A' and spec['guest_ip']==spec['pg_a'])
        readiness={role:w.activation._probe(context,leaf,spec['guest_ip'],role,deadline,expected_node='A')
                   for role in order if role!='ha-broker'}
        require(len(readiness)==7 and all(wire._hash(pin) for pin in readiness.values()))
        require(w.activation._tls(prepared)[1]==leaf)
        require(inventory()==before)
        require(w._read_initial_recovery_hold(request)==(raw,hold,prepared,active)
            and w.runtime._read(stage/'guest-spec.json',0,{0o400,0o600})==spec_raw
            and w.runtime._read(target,0,{0o400},limit=131072)==authorization_raw
            and all(w.runtime._read(stage/(stem+name),0,{0o600},limit=131072)==body for name,body in journals.items()))
        require(resume._latest_history(request,raw,hold,active,history)==recorded_authorization)
        final_lease=lease('completion_after')
        require(time.monotonic()<deadline)
        result={'status':'STARTED_VERIFIED','receipt_sha256':history['done_sha256'],
            'authorization_sha256':history['authorization_sha256'],'history':history,
            'request_sha256':wire._digest(wire._bytes(w._scope(request)|{'args':request.args})),
            'service_instance_sha256':instance,'instances_sha256':wire._digest(wire._bytes(before[1])),
            'readiness':readiness,'guest_lease_sha256':final_lease,'container_count':len(order),'mutations':False}
    return result
