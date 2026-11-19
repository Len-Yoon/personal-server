"""Sealed same-boot continuation after an unpublished first authority attempt."""
import hashlib
import json
import re
from .controller import TransitionBlocked

ACTION = 'ha-recover-unpublished-initial-writer'
KIND = 'initial-wsl-cutover-unpublished'
PRIOR_COMMIT = '7cdb958bd2db88d60be40336d5d7a172a94f4115'
FIELDS = {'schema_version','attempt_id','predecessor_terminal_sha256','predecessor_args_sha256',
          'predecessor_plan_sha256','predecessor_seal_sha256','closure_mode'}
FILES = ('unpublished-predecessor-state.json','unpublished-predecessor-args.txt',
         'unpublished-predecessor-plan.json','unpublished-predecessor-seal.json','unpublished-acceptance-seal.json')
SHA = re.compile(r'[a-f0-9]{64}')

def need(value):
    if not value:raise TransitionBlocked('initial_unpublished_scope_unverified')

def scope(plan):
    value=plan.get('initial_unpublished_recovery')
    need(not {'initial_recovery_rearm','initial_recovery_postboot'} & plan.keys()
         and type(value) is dict and set(value)==FIELDS
         and type(value['schema_version']) is int and value['schema_version']==1
         and value['closure_mode']=='windows-offline-v1'
         and all(type(value[key]) is str and SHA.fullmatch(value[key]) for key in FIELDS-{'schema_version','closure_mode'})
         and type(plan.get('initial_cutover_recovery')) is dict
         and value['attempt_id'] not in {plan['initial_cutover_recovery'].get('attempt_id'),
             plan.get('initial_cutover_handoff',{}).get('attempt_id')})
    return value.copy()

def derive_predecessor(raw, expected_sha256, *, published):
    """Derive one successor only from a hash-bound terminal, never a scalar revision.

    Callers must separately authenticate custody/provenance and prove the exact
    derived closed stamp. A terminal digest alone grants no writer authority.
    """
    need(type(raw) is bytes and 0 < len(raw) <= 65536 and type(expected_sha256) is str
         and SHA.fullmatch(expected_sha256) and hashlib.sha256(raw).hexdigest()==expected_sha256
         and type(published) is bool)
    def pairs(items):
        value=dict(items);need(len(value)==len(items));return value
    try:
        prior=json.loads(raw.decode('utf-8'),object_pairs_hook=pairs)
    except (ValueError,UnicodeError,RecursionError):
        need(False)
    field={'initial-wsl-cutover-rearm':'rearm',KIND:'unpublished'}.get(prior.get('kind')) if type(prior) is dict else None
    common={'schema_version','kind','generation','attempt_id','legacy_plan_sha256','restore_receipt_sha256',
            'activation_intent_sha256','controller_plan_sha256','recovery','revision','phase','sequence'}
    need(field is not None and set(prior) in (common|{field},common|{field,'failure_code'})
         and type(prior['schema_version']) is int and prior['schema_version']==1
         and type(prior['generation']) is int and prior['generation']==1
         and all(type(prior[key]) is str and SHA.fullmatch(prior[key]) for key in (
             'attempt_id','legacy_plan_sha256','restore_receipt_sha256','activation_intent_sha256','controller_plan_sha256'))
         and type(prior['recovery']) is dict and set(prior['recovery'])=={
             'attempt_id','predecessor_terminal_sha256','predecessor_service_sha256'}
         and all(type(pin) is str and SHA.fullmatch(pin) for pin in prior['recovery'].values())
         and type(prior['revision']) is int and 6<=prior['revision']<=2147483644
         and prior['revision']%2==0 and type(prior['sequence']) is int
         and prior['phase'] in ('local_revoke_observed','lease_lost')
         and ('failure_code' not in prior or (type(prior['failure_code']) is str
             and re.fullmatch('[a-z][a-z0-9_]{0,95}',prior['failure_code']))))
    context={'initial_cutover_recovery':prior['recovery'],'initial_cutover_handoff':{'attempt_id':prior['attempt_id']}}
    if field=='rearm':
        from .initial_pg_lease import rearm_scope
        rearm_scope(context|{'initial_recovery_rearm':prior[field]})
        need(prior['revision']==6)
    else:
        scope(context|{'initial_unpublished_recovery':prior[field]})
        need(prior['revision']>=8)
    if published:
        need(prior['sequence']>0)
    elif field=='rearm':
        need(prior['sequence']==1 and prior['phase']=='local_revoke_observed'
             and prior.get('failure_code')=='controller_journal_not_durable')
    else:
        need((prior['sequence']==0 and prior['phase']=='lease_lost' and prior.get('failure_code')=='legacy_pipe_response_timeout_postcommit')
             or (prior['sequence']==1 and prior['phase']=='local_revoke_observed' and prior.get('failure_code')=='controller_journal_not_durable'))
    revision=prior['revision']
    return revision+1,revision+2,revision+3


def validate_seal(plan,plan_pin,files):
    from .initial_windows_gate import _decode,_digest,_args,_path
    from .initial_pg_lease import rearm_scope
    value=scope(plan)
    need(set(files)==set(FILES))
    for name,key in zip(FILES[:4],('predecessor_terminal_sha256','predecessor_args_sha256',
                                 'predecessor_plan_sha256','predecessor_seal_sha256')):
        need(_digest(files[name])==value[key])
    prior,settings,old,seal,acceptance=(_decode(files[FILES[0]]),_args(files[FILES[1]]),
        _decode(files[FILES[2]]),_decode(files[FILES[3]]),_decode(files[FILES[4]]))
    need(type(old) is dict)
    derive_predecessor(files[FILES[0]],value['predecessor_terminal_sha256'],published=False)
    prior_field='rearm' if prior['kind']=='initial-wsl-cutover-rearm' else 'unpublished'
    if prior_field=='rearm':
        need(old.get('source_commit')==PRIOR_COMMIT and not {'initial_unpublished_recovery','initial_recovery_postboot'} & old.keys())
        previous_scope=rearm_scope(old)
    else:
        need(type(old.get('source_commit')) is str and re.fullmatch('[a-f0-9]{40}',old['source_commit']))
        previous_scope=scope(old)
    need(type(old) is dict and old.get('initial_cutover_handoff')==plan['initial_cutover_handoff']
         and old.get('initial_cutover_recovery')==plan['initial_cutover_recovery']
         and old.get('installation_id')==plan['installation_id']
         and value['attempt_id']!=previous_scope['attempt_id']
         and prior['controller_plan_sha256']==value['predecessor_plan_sha256']!=plan_pin
         and prior[prior_field]==previous_scope and prior['recovery']==plan['initial_cutover_recovery']
         and all(prior[key]==plan['initial_cutover_handoff'][key] for key in (
             'attempt_id','legacy_plan_sha256','restore_receipt_sha256','activation_intent_sha256')))
    identity=plan['windows_enforced']; old_identity=old['windows_enforced']
    boot=plan['windows_initial_observer']['windows_boot_sha256']
    need(type(boot) is str and SHA.fullmatch(boot)
         and old['windows_initial_observer']['windows_boot_sha256']==boot
         and all(identity[key]==old_identity[key] for key in ('service_name','service_sid','service_image_sha256'))
         and settings['service_name']==identity['service_name'] and settings['service_sid']==identity['service_sid']
         and settings['plan_sha256']==value['predecessor_plan_sha256']
         and settings['host_sha256']==identity['service_image_sha256'])
    binding_path=old['initial_service_start']['binding_path']
    need(_path(binding_path).parent==_path(settings['plan_path']).parent)
    service={'service_name':identity['service_name'],'service_sid':identity['service_sid'],'state':'Stopped',
             'start_mode':'Disabled','process_id':0,
             'service_image_path_sha256':old['initial_service_start']['service_image_path_sha256']}
    guest={'guest_boot_sha256':plan['windows_recovery_observers']['guest_boot_sha256'],
           'hold_sha256':plan['windows_recovery_observers']['hold_sha256'],'app_start_intent_absent':True}
    need(old['windows_recovery_observers']['guest_boot_sha256']==guest['guest_boot_sha256']
         and old['windows_recovery_observers']['hold_sha256']==guest['hold_sha256'])
    expected={'schema_version':1,'windows_boot_sha256':boot,'service':service,'binding_path':binding_path,
              'binding_absent':True,'guest':guest,
              **{key:value[key] for key in ('predecessor_terminal_sha256','predecessor_args_sha256','predecessor_plan_sha256')}}
    need(type(seal) is dict and seal==expected and type(seal.get('schema_version')) is int
         and type(seal.get('service',{}).get('process_id')) is int
         and seal.get('binding_absent') is True and seal.get('guest',{}).get('app_start_intent_absent') is True)
    expected_acceptance={'schema_version':1,'plan_sha256':plan_pin,'unpublished':value,
                         'windows_boot_sha256':boot,'service':service}
    need(type(acceptance) is dict and acceptance==expected_acceptance
         and type(acceptance.get('schema_version')) is int
         and type(acceptance.get('service',{}).get('process_id')) is int)
    return value
