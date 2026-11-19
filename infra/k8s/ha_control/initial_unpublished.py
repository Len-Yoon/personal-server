"""One failed first publication only: exact seven -> eight -> nine, on the same boot."""
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
    need(type(old) is dict and old.get('source_commit')==PRIOR_COMMIT
         and not {'initial_unpublished_recovery','initial_recovery_postboot'} & old.keys()
         and old.get('initial_cutover_handoff')==plan['initial_cutover_handoff']
         and old.get('initial_cutover_recovery')==plan['initial_cutover_recovery']
         and old.get('installation_id')==plan['installation_id'])
    rearm=rearm_scope(old)
    need(value['attempt_id']!=rearm['attempt_id'])
    stable={'schema_version','kind','generation','attempt_id','legacy_plan_sha256','restore_receipt_sha256',
            'activation_intent_sha256','controller_plan_sha256','recovery','rearm','revision','phase','sequence','failure_code'}
    need(type(prior) is dict and set(prior)==stable
         and type(prior['schema_version']) is int and prior['schema_version']==1
         and type(prior['generation']) is int and prior['generation']==1
         and type(prior['revision']) is int and prior['revision']==6
         and type(prior['sequence']) is int and prior['sequence']==1
         and prior['kind']=='initial-wsl-cutover-rearm' and prior['phase']=='local_revoke_observed'
         and prior['failure_code']=='controller_journal_not_durable'
         and prior['controller_plan_sha256']==value['predecessor_plan_sha256']!=plan_pin
         and prior['rearm']==rearm and prior['recovery']==plan['initial_cutover_recovery']
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
