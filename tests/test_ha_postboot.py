"""Isolated fixed postboot-contract tests. No live Windows/guest actions."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'infra/k8s'))
from ha_control.controller import TransitionBlocked
from ha_control.initial_pg_lease import InitialPgLease, InitialPgLeaseRecovery, InitialPgLeaseRearm, InitialPgLeasePostboot
from ha_control.initial_postboot import ACTION, FILES, scope, validate_seal
from tests import test_ha_pg_control as fixtures


def envelope():
    return dict(schema_version=1, attempt_id='0'*64, predecessor_terminal_sha256='1'*64,
        predecessor_binding_sha256='2'*64, predecessor_args_sha256='3'*64,
        predecessor_seal_sha256='4'*64, previous_windows_boot_sha256='5'*64, closure_mode='windows-offline-v1')


class PostbootPgTests(unittest.TestCase):
    setUp = fixtures.InitialPgLeaseTests.setUp
    legacy_closed = fixtures.InitialPgLeaseTests.legacy_closed
    connection = fixtures.InitialPgLeaseTests.connection
    observe = fixtures.InitialPgLeaseTests.observe
    execute = fixtures.InitialPgLeaseTests.execute
    commit = fixtures.InitialPgLeaseTests.commit
    recovery_lease = fixtures.InitialPgLeaseTests.recovery_lease

    def postboot_lease(self):
        callback = self.recovery_lease().assert_legacy_closed
        self.revision = 7
        self.backend.plan.update(_approval_action=ACTION, initial_recovery_postboot=envelope())
        return InitialPgLeasePostboot(self.backend, callback)

    def test_fixed_eight_renew_and_nine_revoke_preserve_prior_probes(self):
        lease = self.postboot_lease()
        old = {'recovery-a:'+'e'*64+':old:lease', 'rearm-a:'+'9'*64+':old:lease'}
        self.probes.update(old)
        self.assertRegex(lease.open(self.state), r'^[a-f0-9]{64}$')
        self.assertEqual(self.revision, 8)
        self.assertTrue(any(value.startswith('postboot-a:'+'0'*64+':') for value in self.probes))
        self.assertTrue(old <= self.probes)
        self.now += timedelta(seconds=1)
        lease.renew(self.state)
        self.assertTrue(lease.revoke(self.state))
        self.assertEqual(self.revision, 9)
        self.assertFalse(self.opened)
        with self.assertRaises(TransitionBlocked):
            InitialPgLeasePostboot(self.backend, lease.assert_legacy_closed).open(self.state)

    def test_old_classes_reject_even_with_their_old_action(self):
        for cls, action in ((InitialPgLease,'ha-initial-wsl-cutover'),
                            (InitialPgLeaseRecovery,'ha-recover-initial-writer'),
                            (InitialPgLeaseRearm,'ha-rearm-initial-writer')):
            self.setUp()
            lease = self.postboot_lease()
            self.backend.plan['_approval_action'] = action
            with self.assertRaisesRegex(TransitionBlocked, 'initial_postboot_scope_unverified'):
                cls(self.backend, lease.assert_legacy_closed).open(self.state)
            self.assertFalse(self.opened)

    def test_wrong_revision_and_foreign_expiry_cannot_be_adopted(self):
        for revision in (3, 5, 6, 8, 9):
            self.setUp()
            lease = self.postboot_lease()
            self.revision = revision
            with self.assertRaises(TransitionBlocked):
                lease.open(self.state)
            self.assertFalse(self.opened)
        self.setUp()
        lease = self.postboot_lease()
        lease.open(self.state)
        other = InitialPgLeasePostboot(self.backend, lease.assert_legacy_closed)
        self.assertFalse(other.revoke(self.state))
        with self.assertRaises(TransitionBlocked):
            other.renew(self.state)
        self.assertEqual(self.revision, 8)

    def test_changed_envelope_and_ambiguous_commit_fail_closed(self):
        lease = self.postboot_lease()
        self.lost_commit = True
        with self.assertRaisesRegex(TransitionBlocked, 'fixture_commit_response_lost'):
            lease.open(self.state)
        self.assertEqual(self.revision, 9)
        self.setUp()
        lease = self.postboot_lease()
        lease.open(self.state)
        self.backend.plan['initial_recovery_postboot']['attempt_id'] = '8'*64
        with self.assertRaises(TransitionBlocked):
            lease.renew(self.state)


class PostbootSealTests(unittest.TestCase):
    def fixture(self):
        gate, plan, oldfiles, oldseal = fixtures.InitialRearmSealTests().fixture()
        encode=lambda value:json.dumps(value,sort_keys=True).encode()
        digest=lambda value:hashlib.sha256(value).hexdigest()
        oldrearm=plan.pop('initial_recovery_rearm')
        terminal=json.loads(oldfiles['rearm-predecessor-state.json'])
        terminal.update(kind='initial-wsl-cutover-rearm',revision=6,phase='local_revoke_observed',rearm=oldrearm)
        terminal.pop('failure_code')
        files={FILES[0]:encode(terminal),FILES[1]:oldfiles['rearm-predecessor-binding.json'],
               FILES[2]:oldfiles['rearm-predecessor-args.txt']}
        value=envelope()
        for name,key in zip(FILES[:3],('predecessor_terminal_sha256','predecessor_binding_sha256','predecessor_args_sha256')):
            value[key]=digest(files[name])
        guests={node:{key:(str(index+1)*64) for index,key in enumerate((
            'vm_id_sha256','disks_sha256','switches_sha256','guest_boot_sha256','container_inventory_sha256'))} for node in ('A','B')}
        plan['windows_initial_observer']['windows_boot_sha256']='6'*64
        plan['windows_recovery_observers']={'guest_boot_sha256':guests['A']['guest_boot_sha256'],'hold_sha256':'7'*64}
        from ha_control.native_stdio_bridge import RECOVERY_START_ORDER, INITIAL_RECOVERY_ARGS
        plan['installation_id']='c'*64
        guests['B']['guest_boot_sha256']='d'*64
        guests['B']['vm_id_sha256']='6'*64
        guests['B']['disks_sha256']='7'*64
        stopargs={key:'e'*64 for key in INITIAL_RECOVERY_ARGS}
        stopargs.update(recovery_attempt_id=plan['initial_cutover_recovery']['attempt_id'],installation_id=plan['installation_id'],
            hold_sha256='7'*64,postboot_attempt_id=value['attempt_id'],predecessor_plan_sha256=terminal['controller_plan_sha256'],
            predecessor_rearm_attempt_id=oldrearm['attempt_id'])
        stopped=dict(schema_version=1,binding=dict(plan_sha256=plan['initial_cutover_handoff']['legacy_plan_sha256'],
            attempt_id=plan['initial_cutover_handoff']['attempt_id'],source_sha256='f'*64,
            boot_a_sha256=guests['A']['guest_boot_sha256'],boot_b_sha256=guests['B']['guest_boot_sha256']),args=stopargs,
            hold_sha256='7'*64,containers={role:dict(container_id=f'{index:064x}',image_id='sha256:'+'e'*64)
                for index,role in enumerate(RECOVERY_START_ORDER,1)},container_contracts={role:dict(layout_sha256='f'*64,
                started_at='after-rev6-start',running=False) for role in RECOVERY_START_ORDER},
            protected_inventory_sha256='e'*64,closed_revision=7,closed_proof_sha256='f'*64)
        files[FILES[6]]=encode(stopped)
        stopsha=digest(files[FILES[6]])
        guests['A']['container_inventory_sha256']=stopsha
        pre=dict(schema_version=1,windows_boot_sha256=value['previous_windows_boot_sha256'],service=oldseal['service'],
                 guests=guests,hold_sha256='7'*64,closed_revision=7,closed_pair_sha256='8'*64,postboot_stopped_receipt_sha256=stopsha,
                 **{key:value[key] for key in ('predecessor_terminal_sha256','predecessor_binding_sha256','predecessor_args_sha256')})
        files[FILES[3]]=encode(pre)
        value['predecessor_seal_sha256']=digest(files[FILES[3]])
        plan['initial_recovery_postboot']=value
        resume=dict(schema_version=1,plan_sha256='a'*64,windows_boot_sha256='6'*64,
                    previous_windows_boot_sha256=value['previous_windows_boot_sha256'],
                    predecessor_seal_sha256=value['predecessor_seal_sha256'],guests=guests,
                    saved={'A':'Saved','B':'Saved'},resumed={'A':'Running','B':'Running'},hold_sha256='7'*64,
                    guest_observation_sha256={'A':'b'*64,'B':'c'*64},closed_revision=7,closed_pair_sha256='d'*64,
                    clock_ca_validated={'A':True,'B':True},fresh_vsock={'A':True,'B':True},postboot_stopped_receipt_sha256=stopsha)
        files[FILES[4]]=encode(resume)
        acceptance=dict(schema_version=1,plan_sha256='a'*64,postboot=value,windows_boot_sha256='6'*64,
                        resume_evidence_sha256=digest(files[FILES[4]]),service=oldseal['service'])
        files[FILES[5]]=encode(acceptance)
        return plan,files

    def test_exact_saved_guests_and_new_windows_boot_accept(self):
        plan,files=self.fixture()
        self.assertEqual(validate_seal(plan,'a'*64,files),plan['initial_recovery_postboot'])

    def test_changed_raw_prior_files_or_missing_acceptance_reject(self):
        plan,files=self.fixture()
        for name in FILES[:5]:
            with self.subTest(name=name),self.assertRaises(TransitionBlocked):
                validate_seal(plan,'a'*64,files|{name:files[name]+b' '})
        with self.assertRaises(TransitionBlocked):
            validate_seal(plan,'a'*64,{k:v for k,v in files.items() if k!=FILES[5]})
        changed=json.loads(files[FILES[5]])
        changed['service']['process_id']=1
        with self.assertRaises(TransitionBlocked):
            validate_seal(plan,'a'*64,files|{FILES[5]:json.dumps(changed).encode()})

    def test_unchanged_windows_boot_mixed_modes_and_bad_types_reject(self):
        plan,files=self.fixture()
        for field,value in (('schema_version',True),('closure_mode','online'),('attempt_id','a'*64),
                            ('predecessor_seal_sha256',None),('previous_windows_boot_sha256',True)):
            changed=deepcopy(plan)
            changed['initial_recovery_postboot'][field]=value
            with self.subTest(field=field),self.assertRaises(TransitionBlocked):
                scope(changed)
        changed=deepcopy(plan)
        changed['initial_recovery_rearm']={}
        with self.assertRaises(TransitionBlocked):scope(changed)
        changed=deepcopy(plan)
        changed['windows_initial_observer']['windows_boot_sha256']=plan['initial_recovery_postboot']['previous_windows_boot_sha256']
        with self.assertRaises(TransitionBlocked):validate_seal(changed,'a'*64,files)

    def test_lease_lost_terminal_requires_its_exact_closed_seven_seal(self):
        plan,files=self.fixture()
        terminal=json.loads(files[FILES[0]])
        terminal['phase']='lease_lost'
        terminal['failure_code']='legacy_pipe_response_timeout_refresh'
        files[FILES[0]]=json.dumps(terminal,sort_keys=True).encode()
        plan['initial_recovery_postboot']['predecessor_terminal_sha256']=hashlib.sha256(files[FILES[0]]).hexdigest()
        pre=json.loads(files[FILES[3]])
        pre['predecessor_terminal_sha256']=plan['initial_recovery_postboot']['predecessor_terminal_sha256']
        files[FILES[3]]=json.dumps(pre,sort_keys=True).encode()
        plan['initial_recovery_postboot']['predecessor_seal_sha256']=hashlib.sha256(files[FILES[3]]).hexdigest()
        resumed=json.loads(files[FILES[4]])
        resumed['predecessor_seal_sha256']=plan['initial_recovery_postboot']['predecessor_seal_sha256']
        files[FILES[4]]=json.dumps(resumed,sort_keys=True).encode()
        acceptance=json.loads(files[FILES[5]])
        acceptance['postboot']=plan['initial_recovery_postboot']
        acceptance['resume_evidence_sha256']=hashlib.sha256(files[FILES[4]]).hexdigest()
        files[FILES[5]]=json.dumps(acceptance,sort_keys=True).encode()
        self.assertEqual(validate_seal(plan,'a'*64,files),plan['initial_recovery_postboot'])
        pre['closed_revision']=5
        files[FILES[3]]=json.dumps(pre).encode()
        with self.assertRaises(TransitionBlocked):validate_seal(plan,'a'*64,files)

    def test_resume_wrong_guest_disk_hold_clock_or_old_connection_reject(self):
        plan,files=self.fixture()
        for mutation in ('guestboot','disk','hold','saved','clock','vsock','closed','plan'):
            changed=json.loads(files[FILES[4]])
            if mutation=='guestboot':changed['guests']['B']['guest_boot_sha256']='f'*64
            elif mutation=='disk':changed['guests']['A']['disks_sha256']='f'*64
            elif mutation=='hold':changed['hold_sha256']='f'*64
            elif mutation=='saved':changed['saved']['A']='Off'
            elif mutation=='clock':changed['clock_ca_validated']['A']=1
            elif mutation=='vsock':changed['fresh_vsock']['B']=False
            elif mutation=='closed':changed['closed_revision']=5
            else:changed['plan_sha256']='f'*64
            with self.subTest(mutation=mutation),self.assertRaises(TransitionBlocked):
                validate_seal(plan,'a'*64,files|{FILES[4]:json.dumps(changed).encode()})

    def test_postboot_guest_dispatch_is_explicit_a_only_and_mutually_exclusive(self):
        from ha_control import native_stdio_bridge as wire
        args={key:'1'*64 for key in (*wire.INITIAL_RECOVERY_ARGS,'hold_sha256')}
        args.update(postboot=envelope(),postboot_plan_sha256='2'*64,postboot_stopped_receipt_sha256='3'*64)
        wire._args(wire.GUEST_START_INITIAL_POSTBOOT,'A',args)
        for verb,node in ((wire.GUEST_START_INITIAL_RECOVERY,'A'),(wire.GUEST_START_INITIAL_REARM,'A'),
                          (wire.GUEST_START_INITIAL_POSTBOOT,'B')):
            with self.subTest(verb=verb,node=node),self.assertRaises(wire.BridgeRejected):
                wire._args(verb,node,args)
        self.assertIn(wire.GUEST_START_INITIAL_POSTBOOT,wire.DUPLEX_GUEST_VERBS)
        args['postboot']['closure_mode']='online'
        with self.assertRaises(wire.BridgeRejected):wire._args(wire.GUEST_START_INITIAL_POSTBOOT,'A',args)

    def test_native_continuity_binds_postboot_pins_and_preserves_incarnation_checks(self):
        holder=fixtures.InitialLeaseServiceTests()
        for change in (None,lambda before,after,pair:after.update(service_incarnation_sha256='0'*64)):
            native,lease,recovery,windows,paired,clock=holder.continuity_fixture(change=change)
            with patch.object(native,'fixed_script',return_value='fixture') as script, \
                 patch.object(native.secrets,'token_hex',return_value='f'*64), \
                 patch.object(native.time,'monotonic',side_effect=lambda:clock[0]):
                if change is None:
                    self.assertRegex(lease.verify_recovery_continuity(windows,recovery,10.0,observe_pair=paired,
                        assert_guard=lambda:None,postboot=envelope()),r'^[a-f0-9]{64}$')
                    values=script.call_args.args[2]
                    self.assertEqual(values['PostbootAttemptId'],'0'*64)
                    self.assertEqual(values['PostbootPreviousWindowsBootSha256'],'5'*64)
                    self.assertNotIn('RearmAttemptId',values)
                else:
                    with self.assertRaises(native.bridge.BridgeRejected):
                        lease.verify_recovery_continuity(windows,recovery,10.0,observe_pair=paired,
                            assert_guard=lambda:None,postboot=envelope())

    def test_powershell_sources_parse(self):
        runtime=shutil.which('pwsh')
        self.assertIsNotNone(runtime)
        paths=[ROOT/'infra/k8s/ha_control'/name for name in (
            'windows-ha-start-initial-service.ps1','windows-ha-cutover-live-authority.ps1')]
        for path in paths:
            command="$errors=$null;$tokens=$null;$null=[Management.Automation.Language.Parser]::ParseFile('"+str(path)+"',[ref]$tokens,[ref]$errors);if($errors.Count){exit 1};exit 0"
            self.assertEqual(subprocess.run([runtime,'-NoProfile','-Command',command],capture_output=True).returncode,0)


class PostbootStoppedInventoryTests(unittest.TestCase):
    def exercise(self, failed=False):
        from contextlib import nullcontext
        from tempfile import TemporaryDirectory
        from ha_control import native_guest_postboot as stop, native_stdio_bridge as wire
        binding=wire.Binding('1'*64,'2'*64,'3'*64,'4'*64,'5'*64)
        args={key:'6'*64 for key in wire.INITIAL_RECOVERY_ARGS}
        args.update(hold_sha256=wire._digest(b'hold'),postboot_attempt_id='7'*64,
                    predecessor_plan_sha256='8'*64,predecessor_rearm_attempt_id='9'*64)
        request=wire.Request(binding,'A',wire.GUEST_QUIESCE_INITIAL_POSTBOOT,1,'a'*64,args)
        containers={role:{'container_id':f'{index:064x}','image_id':'sha256:'+'b'*64}
                    for index,role in enumerate(wire.RECOVERY_START_ORDER,1)}
        state={role:{'layout_sha256':'c'*64,'started_at':'new-rev6','running':True} for role in containers}
        hold={'containers':containers}
        records={};commands=[];calls=[]
        def current(_):return b'hold',hold,deepcopy(state),'6'*64
        def docker(*command,**options):
            commands.append(command)
            for role,item in containers.items():
                if item['container_id'] in command:state[role]['running']=False
        def read(path):return records[path],json.loads(records[path])
        def closed(challenge,boundary):
            calls.append((challenge,boundary))
            return None if failed else 'd'*64
        with TemporaryDirectory() as folder, patch.object(stop.worker,'ROOT',Path(folder)), \
             patch.object(stop.worker,'_guest_mutation_descriptor',return_value=nullcontext()), \
             patch.object(stop,'current',side_effect=current), \
             patch.object(stop.worker.runtime,'_docker',side_effect=docker), \
             patch.object(stop.worker.runtime,'_write',side_effect=lambda path,raw:records.update({path:raw})), \
             patch.object(stop.worker,'_read_json',side_effect=read):
            self.commands,self.records,self.calls=commands,records,calls
            if failed:
                with self.assertRaises(TransitionBlocked):stop.stop(request,closed)
                self.assertFalse(commands)
                self.assertFalse(records)
                return
            result=stop.stop(request,closed)
            self.assertEqual(result.status,'PASS')
            self.assertEqual([call[1] for call in calls],list(wire.POSTBOOT_STOP_BOUNDARIES))
            self.assertEqual(commands[0],('stop','--signal=SIGTERM','--timeout=-1',containers['ha-broker']['container_id']))
            self.assertEqual(set(commands[1][3:]),{item['container_id'] for role,item in containers.items() if role!='ha-broker'})
            self.assertTrue(all(path.name.startswith('postboot-'+'7'*64) for path in records))
            self.assertFalse(any(path.name==wire.INITIAL_RECOVERY_HOLD for path in records))
            observed=stop.observe(request,'7'*64,result.receipt_sha256)
            self.assertTrue(all(value['running'] is False for value in observed['container_contracts'].values()))
            state['book-memo']['started_at']='changed-after-seal'
            with self.assertRaises(TransitionBlocked):stop.observe(request,'7'*64,result.receipt_sha256)
            state['book-memo']['started_at']='new-rev6'
            state['book-memo']['running']=True
            with self.assertRaises(TransitionBlocked):stop.observe(request,'7'*64,result.receipt_sha256)
            with self.assertRaises(TransitionBlocked):stop.observe(request,'7'*64,'e'*64)

    def test_only_original_eight_are_stopped_and_new_started_times_are_sealed(self):self.exercise()
    def test_missing_fresh_closed_seven_proof_never_stops(self):self.exercise(failed=True)

    def test_dedicated_observer_uses_new_stopped_receipt_and_keeps_original_mode(self):
        from types import SimpleNamespace
        from ha_control import native_guest_recovery_observer as listener, native_stdio_bridge as wire
        binding=wire.Binding('1'*64,'2'*64,'3'*64,'4'*64,'5'*64)
        hold={key:'6'*64 for key in wire.INITIAL_RECOVERY_ARGS if key!='recovery_attempt_id'}
        hold.update(node='A',binding=binding.fields(),attempt_id='7'*64)
        frame={'version':1,'purpose':'held_admission','nonce':'8'*64,'hold_sha256':wire._digest(b'hold'),'guest_boot_sha256':binding.boot('A')}
        calls=[]
        def handle(request):
            calls.append(request)
            return wire.Receipt('PASS','9'*64 if request.verb==wire.GUEST_OBSERVE_INITIAL_POSTBOOT else wire._digest(b'hold'))
        boot=SimpleNamespace(read_bytes=lambda:b'boot')
        with patch.object(listener.worker,'_read_json',return_value=(b'hold',hold)), \
             patch.object(listener.worker,'handle',side_effect=handle), \
             patch.object(listener.worker.restore,'BOOT_ID',boot), \
             patch.object(listener.bridge,'_digest',side_effect=lambda value: binding.boot('A') if value==b'boot' else hashlib.sha256(value).hexdigest()):
            ordinary=listener.Observer(frame['hold_sha256'],binding.boot('A'))
            self.assertEqual(ordinary.observe(frame)['observation_sha256'],frame['hold_sha256'])
            self.assertEqual(calls[-1].verb,wire.GUEST_OBSERVE_INITIAL_RECOVERY_HOLD)
            postboot=listener.Observer(frame['hold_sha256'],binding.boot('A'),postboot_attempt_id='a'*64,postboot_stopped_receipt_sha256='9'*64)
            self.assertEqual(postboot.observe(frame)['observation_sha256'],'9'*64)
            self.assertEqual(calls[-1].verb,wire.GUEST_OBSERVE_INITIAL_POSTBOOT)
            self.assertIn(wire.GUEST_OBSERVE_INITIAL_POSTBOOT,listener.worker.VERBS)
            with self.assertRaises(wire.BridgeRejected):postboot.observe(frame)

    def test_held_admission_requires_exact_postboot_receipt_not_original_hold(self):
        from types import SimpleNamespace
        from ha_control.initial_lease_service import _HeldRecoveryAdmission
        import time
        for returned in ('1'*64,'2'*64):
            admission=_HeldRecoveryAdmission(SimpleNamespace(request=lambda deadline:returned),'2'*64)
            if returned=='2'*64:self.assertIs(admission.assert_held(time.monotonic()+1),True)
            else:
                with self.assertRaisesRegex(TransitionBlocked,'initial_postboot_stopped_unverified'):
                    admission.assert_held(time.monotonic()+1)


class UnpublishedSealTests(unittest.TestCase):
    def fixture(self):
        from ha_control import initial_windows_gate as gate
        from ha_control import initial_unpublished as unpub
        _,old,oldfiles,_=fixtures.InitialRearmSealTests().fixture()
        encode=lambda value:json.dumps(value,sort_keys=True).encode()
        digest=lambda raw:hashlib.sha256(raw).hexdigest()
        old.update(source_commit=unpub.PRIOR_COMMIT,installation_id='c'*64,
            windows_recovery_observers={'guest_boot_sha256':'4'*64,'hold_sha256':'6'*64},
            initial_service_start={'binding_path':r'C:\old\initial-recovery-service.json','service_image_path_sha256':'5'*64})
        old_raw=encode(old);old_pin=digest(old_raw)
        settings=gate._args(oldfiles['rearm-predecessor-args.txt']);settings['plan_sha256']=old_pin
        args=''.join(key+'='+settings[key]+'\n' for key in gate.ARGS_FIELDS).encode()
        terminal=json.loads(oldfiles['rearm-predecessor-state.json'])
        terminal.update(kind='initial-wsl-cutover-rearm',revision=6,phase='local_revoke_observed',sequence=1,
            failure_code='controller_journal_not_durable',rearm=old['initial_recovery_rearm'],controller_plan_sha256=old_pin)
        files={unpub.FILES[0]:encode(terminal),unpub.FILES[1]:args,unpub.FILES[2]:old_raw}
        value=dict(schema_version=1,attempt_id='0'*64,closure_mode='windows-offline-v1',
            predecessor_terminal_sha256=digest(files[unpub.FILES[0]]),predecessor_args_sha256=digest(args),
            predecessor_plan_sha256=old_pin)
        service={'service_name':old['windows_enforced']['service_name'],'service_sid':old['windows_enforced']['service_sid'],
            'state':'Stopped','start_mode':'Disabled','process_id':0,'service_image_path_sha256':'5'*64}
        seal=dict(schema_version=1,windows_boot_sha256='0'*64,service=service,
            binding_path=old['initial_service_start']['binding_path'],binding_absent=True,
            guest=dict(guest_boot_sha256='4'*64,hold_sha256='6'*64,app_start_intent_absent=True),
            **{key:value[key] for key in ('predecessor_terminal_sha256','predecessor_args_sha256','predecessor_plan_sha256')})
        files[unpub.FILES[3]]=encode(seal);value['predecessor_seal_sha256']=digest(files[unpub.FILES[3]])
        plan=deepcopy(old);plan.pop('initial_recovery_rearm');plan['initial_unpublished_recovery']=value
        plan['source_commit']='a'*40
        files[unpub.FILES[4]]=encode(dict(schema_version=1,plan_sha256='a'*64,unpublished=value,windows_boot_sha256='0'*64,service=service))
        return plan,files

    def rehash(self,plan,files):
        from ha_control import initial_unpublished as unpub
        encode=lambda value:json.dumps(value,sort_keys=True).encode()
        value=plan['initial_unpublished_recovery']
        for name,key in zip(unpub.FILES[:3],('predecessor_terminal_sha256','predecessor_args_sha256','predecessor_plan_sha256')):
            value[key]=hashlib.sha256(files[name]).hexdigest()
        seal=json.loads(files[unpub.FILES[3]])
        for key in ('predecessor_terminal_sha256','predecessor_args_sha256','predecessor_plan_sha256'):seal[key]=value[key]
        files[unpub.FILES[3]]=encode(seal)
        value['predecessor_seal_sha256']=hashlib.sha256(files[unpub.FILES[3]]).hexdigest()
        acceptance=json.loads(files[unpub.FILES[4]]);acceptance['unpublished']=value
        files[unpub.FILES[4]]=encode(acceptance)

    def test_only_exact_failed_first_save_no_binding_sameboot_seal_accepts(self):
        from ha_control import initial_unpublished as unpub
        plan,files=self.fixture()
        self.assertEqual(unpub.validate_seal(plan,'a'*64,files),plan['initial_unpublished_recovery'])
        for change in (('sequence',2),('failure_code','legacy_pipe_response_timeout_refresh'),('phase','lease_lost'),('revision',8)):
            changed_plan,changed_files=deepcopy(plan),dict(files)
            terminal=json.loads(files[unpub.FILES[0]]);terminal[change[0]]=change[1]
            changed_files[unpub.FILES[0]]=json.dumps(terminal).encode();self.rehash(changed_plan,changed_files)
            with self.subTest(change=change),self.assertRaises(TransitionBlocked):unpub.validate_seal(changed_plan,'a'*64,changed_files)

    def test_fabricated_binding_or_started_apps_and_new_boot_cannot_be_adopted(self):
        from ha_control import initial_unpublished as unpub
        for change in ('binding','app','boot','pid','oldsource'):
            plan,files=self.fixture();seal=json.loads(files[unpub.FILES[3]])
            if change=='binding':seal['binding_absent']=False
            elif change=='app':seal['guest']['app_start_intent_absent']=False
            elif change=='boot':plan['windows_initial_observer']['windows_boot_sha256']='f'*64
            elif change=='pid':seal['service']['process_id']=1
            else:
                old=json.loads(files[unpub.FILES[2]]);old['source_commit']='f'*40
                files[unpub.FILES[2]]=json.dumps(old).encode()
            files[unpub.FILES[3]]=json.dumps(seal).encode();self.rehash(plan,files)
            with self.subTest(change=change),self.assertRaises(TransitionBlocked):unpub.validate_seal(plan,'a'*64,files)


class UnpublishedPgTests(unittest.TestCase):
    setUp=fixtures.InitialPgLeaseTests.setUp
    legacy_closed=fixtures.InitialPgLeaseTests.legacy_closed
    connection=fixtures.InitialPgLeaseTests.connection
    observe=fixtures.InitialPgLeaseTests.observe
    execute=fixtures.InitialPgLeaseTests.execute
    commit=fixtures.InitialPgLeaseTests.commit
    recovery_lease=fixtures.InitialPgLeaseTests.recovery_lease

    def unpublished_lease(self):
        from ha_control.initial_unpublished import ACTION
        from ha_control.initial_pg_lease import InitialPgLeaseUnpublishedRecovery
        callback=self.recovery_lease().assert_legacy_closed
        self.revision=7
        value=UnpublishedSealTests().fixture()[0]['initial_unpublished_recovery']
        self.backend.plan.update(_approval_action=ACTION,initial_unpublished_recovery=value)
        return InitialPgLeaseUnpublishedRecovery(self.backend,callback)

    def test_exact_eight_nine_and_foreign_incarnation_never_adopted(self):
        lease=self.unpublished_lease();old={'rearm-a:old:lease','postboot-a:old:lease'};self.probes.update(old)
        lease.open(self.state);self.assertEqual(self.revision,8);self.assertTrue(old<=self.probes)
        self.assertTrue(any(pin.startswith('unpublished-a:') for pin in self.probes))
        foreign=type(lease)(self.backend,lease.assert_legacy_closed)
        self.assertFalse(foreign.revoke(self.state))
        with self.assertRaises(TransitionBlocked):foreign.renew(self.state)
        self.now+=timedelta(seconds=1);lease.renew(self.state);self.assertTrue(lease.revoke(self.state));self.assertEqual(self.revision,9)
        with self.assertRaises(TransitionBlocked):type(lease)(self.backend,lease.assert_legacy_closed).open(self.state)

    def test_old_modes_and_wrong_closed_revision_refuse_unpublished_field(self):
        for cls in (InitialPgLease,InitialPgLeaseRecovery,InitialPgLeaseRearm,InitialPgLeasePostboot):
            self.setUp();lease=self.unpublished_lease()
            with self.subTest(cls=cls),self.assertRaises(TransitionBlocked):cls(self.backend,lease.assert_legacy_closed).open(self.state)
        for revision in (5,6,8,9):
            self.setUp();lease=self.unpublished_lease();self.revision=revision
            with self.subTest(revision=revision),self.assertRaises(TransitionBlocked):lease.open(self.state)

    def test_typed_postboot_after_unpublished_is_only_nine_ten_eleven(self):
        from ha_control.initial_pg_lease import InitialPgLeasePostbootAfterUnpublished
        callback=self.recovery_lease().assert_legacy_closed;self.revision=9
        self.backend.plan['_approval_action']=ACTION
        value=envelope();value['predecessor_kind']='published-unpublished-recovery-v1'
        self.backend.plan['initial_recovery_postboot']=value
        lease=InitialPgLeasePostbootAfterUnpublished(self.backend,callback)
        lease.open(self.state);self.assertEqual(self.revision,10)
        self.assertTrue(lease.revoke(self.state));self.assertEqual(self.revision,11)
        self.setUp();callback=self.recovery_lease().assert_legacy_closed;self.revision=7
        self.backend.plan['_approval_action']=ACTION;self.backend.plan['initial_recovery_postboot']=value
        with self.assertRaisesRegex(TransitionBlocked,'initial_postboot_scope_unverified'):
            InitialPgLeasePostboot(self.backend,callback).open(self.state)


class PostbootAfterUnpublishedSealTests(unittest.TestCase):
    def test_only_published_retry_eight_and_closed_nine_choose_postboot_ten(self):
        plan,files=PostbootSealTests().fixture()
        encode=lambda value:json.dumps(value,sort_keys=True).encode()
        digest=lambda value:hashlib.sha256(value).hexdigest()
        prior=json.loads(files[FILES[0]]);prior.pop('rearm')
        unpublished=UnpublishedSealTests().fixture()[0]['initial_unpublished_recovery']
        unpublished['attempt_id']='8'*64
        prior.update(kind='initial-wsl-cutover-unpublished',revision=8,unpublished=unpublished)
        files[FILES[0]]=encode(prior)
        value=plan['initial_recovery_postboot'];value['predecessor_kind']='published-unpublished-recovery-v1'
        value['predecessor_terminal_sha256']=digest(files[FILES[0]])
        stopped=json.loads(files[FILES[6]]);stopped['closed_revision']=9
        stopped['args'].pop('predecessor_rearm_attempt_id')
        stopped['args']['predecessor_unpublished_attempt_id']=unpublished['attempt_id']
        files[FILES[6]]=encode(stopped);stopsha=digest(files[FILES[6]])
        pre=json.loads(files[FILES[3]]);pre.update(closed_revision=9,postboot_stopped_receipt_sha256=stopsha,
            predecessor_terminal_sha256=value['predecessor_terminal_sha256'])
        pre['guests']['A']['container_inventory_sha256']=stopsha
        files[FILES[3]]=encode(pre);value['predecessor_seal_sha256']=digest(files[FILES[3]])
        resumed=json.loads(files[FILES[4]]);resumed.update(closed_revision=9,postboot_stopped_receipt_sha256=stopsha,
            guests=pre['guests'],predecessor_seal_sha256=value['predecessor_seal_sha256'])
        files[FILES[4]]=encode(resumed)
        acceptance=json.loads(files[FILES[5]]);acceptance.update(postboot=value,resume_evidence_sha256=digest(files[FILES[4]]))
        files[FILES[5]]=encode(acceptance)
        self.assertEqual(validate_seal(plan,'a'*64,files),value)
        changed=deepcopy(plan);changed['initial_recovery_postboot'].pop('predecessor_kind')
        with self.assertRaises(TransitionBlocked):validate_seal(changed,'a'*64,files)
        changed=deepcopy(plan);changed['initial_recovery_postboot']['predecessor_kind']='arbitrary-revision'
        with self.assertRaises(TransitionBlocked):scope(changed)


class UnpublishedGuestObservationTests(unittest.TestCase):
    def test_only_fresh_unchanged_stopped_hold_without_old_attempt_intent_accepts(self):
        from contextlib import nullcontext
        from tempfile import TemporaryDirectory
        from ha_control import native_guest_worker as worker,native_guest_postboot as postboot,native_stdio_bridge as wire
        binding=wire.Binding('1'*64,'2'*64,'3'*64,'4'*64,'5'*64)
        args={key:'6'*64 for key in wire.INITIAL_RECOVERY_ARGS}
        args.update(hold_sha256=wire._digest(b'hold'),predecessor_rearm_attempt_id='7'*64,
                    predecessor_plan_sha256='8'*64,unpublished_attempt_id='9'*64)
        request=wire.Request(binding,'A',wire.GUEST_OBSERVE_UNPUBLISHED_PREDECESSOR,1,'a'*64,args)
        contracts={role:{'layout_sha256':'b'*64,'running':False,'started_at':'original'} for role in wire.RECOVERY_START_ORDER}
        hold={'container_contracts':deepcopy(contracts)}
        with TemporaryDirectory() as folder,patch.object(worker,'ROOT',Path(folder)), \
             patch.object(worker,'_guest_mutation_descriptor',return_value=nullcontext()), \
             patch.object(postboot,'current',return_value=(b'hold',hold,contracts,'6'*64)):
            stage=Path(folder)/binding.attempt_id;stage.mkdir()
            self.assertEqual(worker.handle(request).status,'PASS')
            intent=stage/('rearm-'+'7'*64+'-start-intent.json');intent.touch()
            self.assertEqual(worker.handle(request).status,'UNKNOWN')
            intent.unlink();contracts['book-memo']['started_at']='changed'
            self.assertEqual(worker.handle(request).status,'UNKNOWN')

if __name__=='__main__':unittest.main()
