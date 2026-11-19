"""One explicitly approved saved-guest Windows reboot: closed 7, open 8, close 9."""
import re
from .controller import TransitionBlocked

ACTION = 'ha-resume-initial-writer'
FIELDS = {'schema_version', 'attempt_id', 'predecessor_terminal_sha256', 'predecessor_binding_sha256',
          'predecessor_args_sha256', 'predecessor_seal_sha256', 'previous_windows_boot_sha256', 'closure_mode'}
FILES = ('postboot-predecessor-state.json', 'postboot-predecessor-binding.json',
         'postboot-predecessor-args.txt', 'postboot-predecessor-seal.json', 'postboot-resume-evidence.json',
         'postboot-acceptance-seal.json', 'postboot-stopped-inventory.json')
PREDECESSOR_KIND='published-unpublished-recovery-v1'

def after_unpublished(value):
    return type(value) is dict and value.get('predecessor_kind')==PREDECESSOR_KIND

SHA = re.compile(r'[a-f0-9]{64}')

def need(value):
    if not value:
        raise TransitionBlocked('initial_postboot_scope_unverified')

def scope(plan):
    value = plan.get('initial_recovery_postboot')
    need(not {'initial_recovery_rearm','initial_unpublished_recovery'} & plan.keys() and type(value) is dict
         and (set(value)==FIELDS or (set(value)==FIELDS|{'predecessor_kind'} and after_unpublished(value)))
         and type(value['schema_version']) is int and value['schema_version'] == 1
         and value['closure_mode'] == 'windows-offline-v1'
         and all(type(value[key]) is str and SHA.fullmatch(value[key]) for key in FIELDS - {'schema_version', 'closure_mode'})
         and type(plan.get('initial_cutover_recovery')) is dict
         and value['attempt_id'] not in {plan['initial_cutover_recovery'].get('attempt_id'),
                                        plan.get('initial_cutover_handoff', {}).get('attempt_id')})
    return value.copy()

def validate_seal(plan, plan_pin, files):
    """All seven input handles must be TCB-writable only and held by the native gate.

    The predecessor seal is produced BEFORE Save-VM/host reboot. The resume
    record is produced after fresh authenticated guest observations, for this
    exact new plan. Neither record grants authority without live offline HMAC
    closure and the PostgreSQL paired closed-seven barrier.
    """
    from .initial_windows_gate import _decode, _digest, _args, _path
    from .initial_pg_lease import rearm_scope
    value = scope(plan)
    recovered=after_unpublished(value)
    closed_revision=9 if recovered else 7
    prior_revision=8 if recovered else 6
    prior_kind='initial-wsl-cutover-unpublished' if recovered else 'initial-wsl-cutover-rearm'
    prior_field='unpublished' if recovered else 'rearm'
    need(set(files) == set(FILES))
    for name, key in zip(FILES[:4], ('predecessor_terminal_sha256', 'predecessor_binding_sha256',
                                   'predecessor_args_sha256', 'predecessor_seal_sha256')):
        need(_digest(files[name]) == value[key])
    prior, binding, settings, seal, resumed = (_decode(files[FILES[0]]), _decode(files[FILES[1]]),
        _args(files[FILES[2]]), _decode(files[FILES[3]]), _decode(files[FILES[4]]))
    stable = {'schema_version', 'kind', 'generation', 'attempt_id', 'legacy_plan_sha256', 'restore_receipt_sha256',
              'activation_intent_sha256', 'controller_plan_sha256', 'recovery', prior_field, 'revision', 'phase', 'sequence'}
    need(type(prior) is dict and set(prior) in (stable, stable | {'failure_code'})
         and type(prior['schema_version']) is int and prior['schema_version'] == 1
         and type(prior['generation']) is int and prior['generation'] == 1
         and type(prior['revision']) is int and prior['revision'] == prior_revision
         and prior['kind'] == prior_kind and prior['phase'] in ('lease_lost', 'local_revoke_observed')
         and type(prior['sequence']) is int and prior['sequence'] > 0
         and prior['recovery'] == plan['initial_cutover_recovery']
         and all(prior[key] == plan['initial_cutover_handoff'][key] for key in (
             'attempt_id', 'legacy_plan_sha256', 'restore_receipt_sha256', 'activation_intent_sha256')))
    if recovered:
        from .initial_unpublished import scope as unpublished_scope
        previous_rearm=unpublished_scope({'initial_unpublished_recovery':prior['unpublished'],
            'initial_cutover_recovery':plan['initial_cutover_recovery'],
            'initial_cutover_handoff':plan['initial_cutover_handoff']})
    else:
        previous_rearm = rearm_scope({'initial_recovery_rearm': prior['rearm'],
                                     'initial_cutover_recovery': plan['initial_cutover_recovery'],
                                     'initial_cutover_handoff': plan['initial_cutover_handoff']})
    need(value['attempt_id'] != previous_rearm['attempt_id'])
    identity = plan['windows_enforced']
    keys = {'attempt_id', 'controller_plan_sha256', 'first_write_proof_sha256', 'legacy_plan_sha256',
            'service_created_ticks', 'service_image_path_sha256', 'service_name', 'service_pid', 'state_path'}
    need(type(binding) is dict and set(binding) == keys
         and binding['controller_plan_sha256'] == prior['controller_plan_sha256'] == settings['plan_sha256'] != plan_pin
         and binding['legacy_plan_sha256'] == prior['legacy_plan_sha256'] and binding['attempt_id'] == prior['attempt_id']
         and binding['service_name'] == settings['service_name'] == identity['service_name']
         and settings['service_sid'] == identity['service_sid'] and settings['host_sha256'] == identity['service_image_sha256']
         and type(binding['service_pid']) is int and binding['service_pid'] > 0
         and type(binding['service_created_ticks']) is str and re.fullmatch(r'[1-9][0-9]{0,19}', binding['service_created_ticks'])
         and all(type(binding[key]) is str and SHA.fullmatch(binding[key]) for key in (
             'controller_plan_sha256', 'first_write_proof_sha256', 'service_image_path_sha256'))
         and _path(settings['plan_path']).parent / 'journal' / 'controller-state.json' == _path(binding['state_path']))
    newboot = plan['windows_initial_observer']['windows_boot_sha256']
    oldboot = value['previous_windows_boot_sha256']
    need(type(newboot) is str and SHA.fullmatch(newboot) and newboot != oldboot)
    expected_service = {'service_name': identity['service_name'], 'service_sid': identity['service_sid'],
                        'state': 'Stopped', 'start_mode': 'Disabled', 'process_id': 0,
                        'service_image_path_sha256': binding['service_image_path_sha256']}
    seal_keys = {'schema_version', 'windows_boot_sha256', 'predecessor_terminal_sha256', 'predecessor_binding_sha256',
                 'predecessor_args_sha256', 'service', 'guests', 'hold_sha256', 'closed_revision', 'closed_pair_sha256', 'postboot_stopped_receipt_sha256'}
    need(type(seal) is dict and set(seal) == seal_keys and type(seal['schema_version']) is int and seal['schema_version'] == 1
         and type(seal['closed_revision']) is int and seal['closed_revision'] == closed_revision
         and type(seal['closed_pair_sha256']) is str and SHA.fullmatch(seal['closed_pair_sha256'])
         and type(seal['postboot_stopped_receipt_sha256']) is str and SHA.fullmatch(seal['postboot_stopped_receipt_sha256'])
         and seal['hold_sha256'] == plan['windows_recovery_observers']['hold_sha256']
         and seal['windows_boot_sha256'] == oldboot and seal['service'] == expected_service
         and type(seal['service']['process_id']) is int
         and all(seal[key] == value[key] for key in ('predecessor_terminal_sha256', 'predecessor_binding_sha256', 'predecessor_args_sha256')))
    guests = seal['guests']
    guest_fields = {'vm_id_sha256', 'disks_sha256', 'switches_sha256', 'guest_boot_sha256', 'container_inventory_sha256'}
    need(type(guests) is dict and set(guests) == {'A', 'B'}
         and all(type(guest) is dict and set(guest) == guest_fields
                 and all(type(pin) is str and SHA.fullmatch(pin) for pin in guest.values()) for guest in guests.values())
         and all(guests['A'][key] != guests['B'][key] for key in ('vm_id_sha256','disks_sha256','guest_boot_sha256'))
         and guests['A']['container_inventory_sha256'] == seal['postboot_stopped_receipt_sha256']
         and guests['A']['guest_boot_sha256'] == plan['windows_recovery_observers']['guest_boot_sha256'])
    resume_keys = {'schema_version', 'plan_sha256', 'windows_boot_sha256', 'previous_windows_boot_sha256',
                   'predecessor_seal_sha256', 'guests', 'saved', 'resumed', 'hold_sha256', 'guest_observation_sha256',
                   'closed_revision', 'closed_pair_sha256', 'clock_ca_validated', 'fresh_vsock', 'postboot_stopped_receipt_sha256'}
    need(type(resumed) is dict and set(resumed) == resume_keys and type(resumed['schema_version']) is int
         and resumed['schema_version'] == 1 and resumed['plan_sha256'] == plan_pin
         and resumed['windows_boot_sha256'] == newboot and resumed['previous_windows_boot_sha256'] == oldboot
         and resumed['predecessor_seal_sha256'] == value['predecessor_seal_sha256']
         and resumed['postboot_stopped_receipt_sha256'] == seal['postboot_stopped_receipt_sha256']
         and resumed['guests'] == guests and resumed['saved'] == {'A': 'Saved', 'B': 'Saved'}
         and resumed['resumed'] == {'A': 'Running', 'B': 'Running'}
         and resumed['clock_ca_validated'] == {'A': True, 'B': True}
         and resumed['fresh_vsock'] == {'A': True, 'B': True}
         and all(type(v) is bool for v in (*resumed['clock_ca_validated'].values(), *resumed['fresh_vsock'].values()))
         and resumed['hold_sha256'] == plan['windows_recovery_observers']['hold_sha256']
         and type(resumed['closed_revision']) is int and resumed['closed_revision'] == closed_revision
         and type(resumed['guest_observation_sha256']) is dict and set(resumed['guest_observation_sha256']) == {'A', 'B'}
         and all(type(pin) is str and SHA.fullmatch(pin) for pin in (*resumed['guest_observation_sha256'].values(), resumed['closed_pair_sha256'])))
    # Carry the actual immutable guest receipt, not an untyped assertion of
    # its digest. This binds the new attempt to the exact pre-save stop.
    stopped = _decode(files[FILES[6]])
    # The native Windows bundle intentionally excludes SSH/stdio modules.
    RECOVERY_START_ORDER = ('ha-broker','book-memo','car-care-worker','crawler-worker','homeops-executor','portal-web','system-agent','youtube-memo')
    INITIAL_RECOVERY_ARGS = ('recovery_attempt_id','installation_id','runtime_receipt_sha256','activation_receipt_sha256','protected_inventory_sha256','car_route_release_sha256')
    stopped_keys = {'schema_version','binding','args','hold_sha256','containers','container_contracts',
                    'protected_inventory_sha256','closed_revision','closed_proof_sha256'}
    need(_digest(files[FILES[6]]) == seal['postboot_stopped_receipt_sha256']
         and type(stopped) is dict and set(stopped) == stopped_keys
         and type(stopped['schema_version']) is int and stopped['schema_version'] == 1
         and type(stopped['closed_revision']) is int and stopped['closed_revision'] == closed_revision
         and stopped['hold_sha256'] == seal['hold_sha256']
         and type(stopped['closed_proof_sha256']) is str and SHA.fullmatch(stopped['closed_proof_sha256'])
         and type(stopped['binding']) is dict and set(stopped['binding']) == {
             'plan_sha256','attempt_id','source_sha256','boot_a_sha256','boot_b_sha256'}
         and all(type(pin) is str and SHA.fullmatch(pin) for pin in stopped['binding'].values())
         and stopped['binding']['plan_sha256'] == plan['initial_cutover_handoff']['legacy_plan_sha256']
         and stopped['binding']['attempt_id'] == plan['initial_cutover_handoff']['attempt_id']
         and stopped['binding']['boot_a_sha256'] == guests['A']['guest_boot_sha256']
         and stopped['binding']['boot_b_sha256'] == guests['B']['guest_boot_sha256'])
    args = stopped['args']
    predecessor_arg='predecessor_unpublished_attempt_id' if recovered else 'predecessor_rearm_attempt_id'
    need(type(args) is dict and set(args) == set(INITIAL_RECOVERY_ARGS) | {
        'hold_sha256','postboot_attempt_id','predecessor_plan_sha256',predecessor_arg}
         and all(type(pin) is str and SHA.fullmatch(pin) for pin in args.values())
         and args['postboot_attempt_id'] == value['attempt_id']
         and args['predecessor_plan_sha256'] == prior['controller_plan_sha256']
         and args[predecessor_arg] == previous_rearm['attempt_id']
         and args['recovery_attempt_id'] == plan['initial_cutover_recovery']['attempt_id']
         and args['installation_id'] == plan['installation_id']
         and args['hold_sha256'] == stopped['hold_sha256']
         and args['protected_inventory_sha256'] == stopped['protected_inventory_sha256']
         and type(stopped['containers']) is dict and set(stopped['containers']) == set(RECOVERY_START_ORDER)
         and type(stopped['container_contracts']) is dict and set(stopped['container_contracts']) == set(RECOVERY_START_ORDER)
         and all(type(row) is dict and set(row) == {'container_id','image_id'}
                 and type(row['container_id']) is str and SHA.fullmatch(row['container_id'])
                 and type(row['image_id']) is str and re.fullmatch(r'sha256:[a-f0-9]{64}',row['image_id'])
                 for row in stopped['containers'].values())
         and len({row['container_id'] for row in stopped['containers'].values()}) == 8
         and all(type(row) is dict and set(row) == {'layout_sha256','started_at','running'}
                 and type(row['layout_sha256']) is str and SHA.fullmatch(row['layout_sha256'])
                 and type(row['started_at']) is str and bool(row['started_at']) and row['running'] is False
                 for row in stopped['container_contracts'].values()))
    acceptance = _decode(files[FILES[5]])
    expected_acceptance = {'schema_version': 1, 'plan_sha256': plan_pin, 'postboot': value,
        'windows_boot_sha256': newboot, 'resume_evidence_sha256': _digest(files[FILES[4]]), 'service': expected_service}
    need(type(acceptance) is dict and acceptance == expected_acceptance
         and type(acceptance.get('schema_version')) is int
         and type(acceptance.get('service', {}).get('process_id')) is int)
    return value
