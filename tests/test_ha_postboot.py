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
        plan,files=PostbootSealTests().fixture()
        self.backend.fence_adapter._predecessor_terminal=files[FILES[0]]
        self.backend.plan.update(_approval_action=ACTION, initial_recovery_postboot=plan['initial_recovery_postboot'])
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
            plan,files=self.fixture();prior=json.loads(files[FILES[0]])
            prior.update(recovery=recovery,attempt_id=lease.custody.binding.attempt_id,legacy_plan_sha256=lease.custody.binding.plan_sha256)
            raw=json.dumps(prior).encode();value=envelope();value['predecessor_terminal_sha256']=hashlib.sha256(raw).hexdigest()
            with patch.object(native,'fixed_script',return_value='fixture') as script, \
                 patch.object(native.secrets,'token_hex',return_value='f'*64), \
                 patch.object(native.time,'monotonic',side_effect=lambda:clock[0]):
                if change is None:
                    self.assertRegex(lease.verify_recovery_continuity(windows,recovery,10.0,observe_pair=paired,
                        assert_guard=lambda:None,postboot=value,predecessor_terminal=raw),r'^[a-f0-9]{64}$')
                    values=script.call_args.args[2]
                    self.assertEqual(values['PostbootAttemptId'],'0'*64)
                    self.assertEqual(values['PostbootPreviousWindowsBootSha256'],'5'*64)
                    self.assertNotIn('RearmAttemptId',values)
                else:
                    with self.assertRaises(native.bridge.BridgeRejected):
                        lease.verify_recovery_continuity(windows,recovery,10.0,observe_pair=paired,
                            assert_guard=lambda:None,postboot=value,predecessor_terminal=raw)

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
        def handle(request, **kwargs):
            calls.append(request)
            if "postboot_carryover_receipt_sha256" in request.args:
                self.assertIsNotNone(kwargs.get("provider_inventory"))
                wire._args(request.verb, request.node, request.args)
                return wire.Receipt("PASS", request.args["postboot_carryover_receipt_sha256"])
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
            carried=listener.Observer(frame['hold_sha256'],binding.boot('A'),postboot_attempt_id='b'*64,
                postboot_stopped_receipt_sha256='9'*64,postboot_carryover_receipt_sha256='c'*64)
            self.assertEqual(carried.observe(frame)['observation_sha256'],'c'*64)
            self.assertEqual(calls[-1].args['postboot_stopped_receipt_sha256'],'9'*64)
            self.assertEqual(calls[-1].args['postboot_attempt_id'],'b'*64)


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

    def continuation_fixture(self,revision=8):
        from ha_control import initial_unpublished as unpub,initial_windows_gate as gate
        old,first=self.fixture()
        encode=lambda value:json.dumps(value,sort_keys=True).encode()
        digest=lambda raw:hashlib.sha256(raw).hexdigest()
        old_raw=encode(old);oldpin=digest(old_raw)
        prior=json.loads(first[unpub.FILES[0]]);prior.pop('rearm')
        prior.update(kind=unpub.KIND,revision=revision,sequence=0,phase='lease_lost',
            failure_code='legacy_pipe_response_timeout_postcommit',unpublished=old['initial_unpublished_recovery'],
            controller_plan_sha256=oldpin)
        args=gate._args(first[unpub.FILES[1]]);args['plan_sha256']=oldpin
        argsraw=''.join(key+'='+args[key]+'\n' for key in gate.ARGS_FIELDS).encode()
        value=deepcopy(old['initial_unpublished_recovery']);value.update(attempt_id='1'*64,
            predecessor_terminal_sha256=digest(encode(prior)),predecessor_args_sha256=digest(argsraw),predecessor_plan_sha256=oldpin)
        seal=json.loads(first[unpub.FILES[3]])
        for key in ('predecessor_terminal_sha256','predecessor_args_sha256','predecessor_plan_sha256'):seal[key]=value[key]
        sealraw=encode(seal);value['predecessor_seal_sha256']=digest(sealraw)
        plan=deepcopy(old);plan['initial_unpublished_recovery']=value
        acceptance=json.loads(first[unpub.FILES[4]]);acceptance['unpublished']=value
        files=dict(zip(unpub.FILES,(encode(prior),argsraw,old_raw,sealraw,encode(acceptance))))
        return plan,files

    def test_actual_failed_unpublished_terminal_continues_without_new_mode_or_revision_field(self):
        from ha_control import initial_unpublished as unpub
        for revision in (8,10):
            plan,files=self.continuation_fixture(revision)
            self.assertEqual(unpub.validate_seal(plan,'a'*64,files),plan['initial_unpublished_recovery'])
            self.assertEqual(unpub.derive_predecessor(files[unpub.FILES[0]],plan['initial_unpublished_recovery']['predecessor_terminal_sha256'],published=False),
                (revision+1,revision+2,revision+3))
            changed=deepcopy(plan);changed['initial_unpublished_recovery']['revision']=revision+2
            with self.assertRaises(TransitionBlocked):unpub.scope(changed)
            for field,wrong in (('binding_absent',False),('windows_boot_sha256','f'*64)):
                altered=dict(files);seal=json.loads(altered[unpub.FILES[3]]);seal[field]=wrong
                altered[unpub.FILES[3]]=json.dumps(seal).encode();self.rehash(plan,altered)
                with self.assertRaises(TransitionBlocked):unpub.validate_seal(plan,'a'*64,altered)

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
        plan,files=UnpublishedSealTests().fixture()
        from ha_control.initial_unpublished import FILES as UFILES
        self.backend.fence_adapter._predecessor_terminal=files[UFILES[0]]
        value=plan['initial_unpublished_recovery']
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

    def continuation_lease(self,prior_revision=8):
        from ha_control import initial_unpublished as unpub
        from ha_control.initial_pg_lease import InitialPgLeaseUnpublishedRecovery
        callback=self.recovery_lease().assert_legacy_closed
        plan,files=UnpublishedSealTests().continuation_fixture(prior_revision)
        self.revision=prior_revision+1
        self.backend.fence_adapter._predecessor_terminal=files[unpub.FILES[0]]
        self.backend.plan.update(_approval_action=unpub.ACTION,initial_unpublished_recovery=plan['initial_unpublished_recovery'])
        return InitialPgLeaseUnpublishedRecovery(self.backend,callback)

    def test_continuation_opens_only_derived_closed_stamp_and_revokes_only_owned_expiry(self):
        for prior in (8,10):
            self.setUp();lease=self.continuation_lease(prior)
            old={'unpublished-a:older:lease'};self.probes.update(old)
            lease.open(self.state);self.assertEqual(self.revision,prior+2);self.assertTrue(old<=self.probes)
            foreign=type(lease)(self.backend,lease.assert_legacy_closed)
            self.assertFalse(foreign.revoke(self.state))
            with self.assertRaises(TransitionBlocked):foreign.renew(self.state)
            self.now+=timedelta(seconds=1);lease.renew(self.state)
            self.assertTrue(lease.revoke(self.state));self.assertEqual(self.revision,prior+3)
        for wrong in (7,8,10,11):
            self.setUp();lease=self.continuation_lease();self.revision=wrong
            with self.assertRaises(TransitionBlocked):lease.open(self.state)
            self.assertFalse(self.opened)

    def test_changed_or_missing_terminal_bytes_cannot_choose_or_adopt_a_revision(self):
        for bad in (None,b'{}',8):
            self.setUp();lease=self.continuation_lease();self.backend.fence_adapter._predecessor_terminal=bad
            with self.assertRaises(TransitionBlocked):lease.open(self.state)
            self.assertFalse(self.opened)
        self.setUp();lease=self.continuation_lease();lease.open(self.state)
        raw=self.backend.fence_adapter._predecessor_terminal
        self.backend.fence_adapter._predecessor_terminal=raw+b' '
        with self.assertRaises(TransitionBlocked):lease.renew(self.state)

    def test_old_modes_and_wrong_closed_revision_refuse_unpublished_field(self):
        for cls in (InitialPgLease,InitialPgLeaseRecovery,InitialPgLeaseRearm,InitialPgLeasePostboot):
            self.setUp();lease=self.unpublished_lease()
            with self.subTest(cls=cls),self.assertRaises(TransitionBlocked):cls(self.backend,lease.assert_legacy_closed).open(self.state)
        for revision in (5,6,8,9):
            self.setUp();lease=self.unpublished_lease();self.revision=revision
            with self.subTest(revision=revision),self.assertRaises(TransitionBlocked):lease.open(self.state)

    def test_published_continuation_reboots_using_same_postboot_type_for_eleven_twelve_thirteen(self):
        from ha_control.initial_pg_lease import InitialPgLeasePostbootAfterUnpublished
        callback=self.recovery_lease().assert_legacy_closed
        plan,files=PostbootAfterUnpublishedSealTests().fixture(10)
        self.backend.plan.update(_approval_action=ACTION,initial_recovery_postboot=plan['initial_recovery_postboot'])
        self.backend.fence_adapter._predecessor_terminal=files[FILES[0]]
        self.revision=11
        lease=InitialPgLeasePostbootAfterUnpublished(self.backend,callback)
        lease.open(self.state);self.assertEqual(self.revision,12)
        self.assertTrue(lease.revoke(self.state));self.assertEqual(self.revision,13)

    def test_typed_postboot_after_unpublished_is_only_nine_ten_eleven(self):
        from ha_control.initial_pg_lease import InitialPgLeasePostbootAfterUnpublished
        callback=self.recovery_lease().assert_legacy_closed;self.revision=9
        self.backend.plan['_approval_action']=ACTION
        value=envelope();value['predecessor_kind']='published-unpublished-recovery-v1'
        oldplan,oldfiles=UnpublishedSealTests().fixture()
        prior=json.loads(oldfiles['unpublished-predecessor-state.json']);prior.pop('rearm')
        prior.update(kind='initial-wsl-cutover-unpublished',revision=8,unpublished=oldplan['initial_unpublished_recovery'])
        raw=json.dumps(prior).encode();value['predecessor_terminal_sha256']=hashlib.sha256(raw).hexdigest()
        self.backend.fence_adapter._predecessor_terminal=raw
        self.backend.plan['initial_recovery_postboot']=value
        lease=InitialPgLeasePostbootAfterUnpublished(self.backend,callback)
        lease.open(self.state);self.assertEqual(self.revision,10)
        self.assertTrue(lease.revoke(self.state));self.assertEqual(self.revision,11)
        self.setUp();callback=self.recovery_lease().assert_legacy_closed;self.revision=7
        self.backend.plan['_approval_action']=ACTION;self.backend.plan['initial_recovery_postboot']=value
        with self.assertRaisesRegex(TransitionBlocked,'initial_postboot_scope_unverified'):
            InitialPgLeasePostboot(self.backend,callback).open(self.state)


class PostbootAfterUnpublishedSealTests(unittest.TestCase):
    def fixture(self,revision=8):
        plan,files=PostbootSealTests().fixture()
        encode=lambda value:json.dumps(value,sort_keys=True).encode()
        digest=lambda value:hashlib.sha256(value).hexdigest()
        prior=json.loads(files[FILES[0]]);prior.pop('rearm')
        unpublished=UnpublishedSealTests().fixture()[0]['initial_unpublished_recovery']
        unpublished['attempt_id']='8'*64
        prior.update(kind='initial-wsl-cutover-unpublished',revision=revision,unpublished=unpublished)
        files[FILES[0]]=encode(prior)
        value=plan['initial_recovery_postboot'];value['predecessor_kind']='published-unpublished-recovery-v1'
        value['predecessor_terminal_sha256']=digest(files[FILES[0]])
        stopped=json.loads(files[FILES[6]]);stopped['closed_revision']=revision+1
        stopped['args'].pop('predecessor_rearm_attempt_id')
        stopped['args']['predecessor_unpublished_attempt_id']=unpublished['attempt_id']
        files[FILES[6]]=encode(stopped);stopsha=digest(files[FILES[6]])
        pre=json.loads(files[FILES[3]]);pre.update(closed_revision=revision+1,postboot_stopped_receipt_sha256=stopsha,
            predecessor_terminal_sha256=value['predecessor_terminal_sha256'])
        pre['guests']['A']['container_inventory_sha256']=stopsha
        files[FILES[3]]=encode(pre);value['predecessor_seal_sha256']=digest(files[FILES[3]])
        resumed=json.loads(files[FILES[4]]);resumed.update(closed_revision=revision+1,postboot_stopped_receipt_sha256=stopsha,
            guests=pre['guests'],predecessor_seal_sha256=value['predecessor_seal_sha256'])
        files[FILES[4]]=encode(resumed)
        acceptance=json.loads(files[FILES[5]]);acceptance.update(postboot=value,resume_evidence_sha256=digest(files[FILES[4]]))
        files[FILES[5]]=encode(acceptance)
        return plan,files

    def test_only_published_retry_eight_and_closed_nine_choose_postboot_ten(self):
        plan,files=self.fixture();value=plan['initial_recovery_postboot']
        self.assertEqual(validate_seal(plan,'a'*64,files),value)
        changed=deepcopy(plan);changed['initial_recovery_postboot'].pop('predecessor_kind')
        with self.assertRaises(TransitionBlocked):validate_seal(changed,'a'*64,files)
        changed=deepcopy(plan);changed['initial_recovery_postboot']['predecessor_kind']='arbitrary-revision'
        with self.assertRaises(TransitionBlocked):scope(changed)


    def test_same_postboot_tag_uses_real_published_ten_terminal_for_twelve(self):
        plan,files=self.fixture(10)
        self.assertEqual(validate_seal(plan,'a'*64,files),plan['initial_recovery_postboot'])
        changed=dict(files);stopped=json.loads(files[FILES[6]]);stopped['closed_revision']=9
        changed[FILES[6]]=json.dumps(stopped).encode()
        with self.assertRaises(TransitionBlocked):validate_seal(plan,'a'*64,changed)


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

        _,oldfiles=UnpublishedSealTests().fixture()
        prior=json.loads(oldfiles['unpublished-predecessor-state.json'])
        prior.update(controller_plan_sha256=args['predecessor_plan_sha256'],attempt_id=binding.attempt_id,
            legacy_plan_sha256=binding.plan_sha256,recovery=prior['recovery']|{'attempt_id':args['recovery_attempt_id']})
        prior['rearm']['attempt_id']=args['predecessor_rearm_attempt_id']
        terminal=json.dumps(prior).encode()

        contracts={role:{'layout_sha256':'b'*64,'running':False,'started_at':'original'} for role in wire.RECOVERY_START_ORDER}
        hold={'container_contracts':deepcopy(contracts)}
        with TemporaryDirectory() as folder,patch.object(worker,'ROOT',Path(folder)), \
             patch.object(worker,'_guest_mutation_descriptor',return_value=nullcontext()), \
             patch.object(postboot,'current',return_value=(b'hold',hold,contracts,'6'*64)), \
             patch.object(worker.runtime,'_read',return_value=terminal):
            stage=Path(folder)/binding.attempt_id;stage.mkdir()
            self.assertEqual(worker.handle(request).status,'PASS')
            intent=stage/('rearm-'+'7'*64+'-start-intent.json');intent.touch()
            self.assertEqual(worker.handle(request).status,'UNKNOWN')
            intent.unlink()
            intent=stage/('unpublished-'+'7'*64+'-start-intent.json');intent.touch()
            self.assertEqual(worker.handle(request).status,'UNKNOWN')
            intent.unlink();contracts['book-memo']['started_at']='changed'
            self.assertEqual(worker.handle(request).status,'UNKNOWN')




class ContinuationDerivationTests(unittest.TestCase):
    def test_only_exact_raw_terminal_bytes_and_bounded_typed_revision_derive(self):
        from ha_control.initial_unpublished import derive_predecessor
        plan,files=UnpublishedSealTests().continuation_fixture()
        raw=files['unpublished-predecessor-state.json'];pin=hashlib.sha256(raw).hexdigest()
        self.assertEqual(derive_predecessor(raw,pin,published=False),(9,10,11))
        for wrong in (None,8,True,b'{}',raw+b' '):
            with self.subTest(wrong=type(wrong)),self.assertRaises(TransitionBlocked):derive_predecessor(wrong,pin,published=False)
        for field,wrong in (('revision',True),('revision',9),('revision',2147483646),('revision',4),
            ('sequence',2),('sequence',True),('phase','serving'),('phase','local_revoke_observed'),
            ('generation',2),('failure_code','legacy_pipe_response_timeout_refresh'),('kind','arbitrary')):
            prior=json.loads(raw);prior[field]=wrong;bad=json.dumps(prior).encode()
            with self.subTest(field=field,wrong=wrong),self.assertRaises(TransitionBlocked):
                derive_predecessor(bad,hashlib.sha256(bad).hexdigest(),published=False)
        duplicate=raw[:-1]+b',"revision":8}'
        with self.assertRaises(TransitionBlocked):derive_predecessor(duplicate,hashlib.sha256(duplicate).hexdigest(),published=False)

    def test_published_terminal_can_derive_postboot_but_cannot_be_unpublished_adoption(self):
        from ha_control.initial_unpublished import derive_predecessor
        _,files=UnpublishedSealTests().continuation_fixture(10)
        prior=json.loads(files['unpublished-predecessor-state.json']);prior['sequence']=12
        raw=json.dumps(prior).encode();pin=hashlib.sha256(raw).hexdigest()
        self.assertEqual(derive_predecessor(raw,pin,published=True),(11,12,13))
        with self.assertRaises(TransitionBlocked):derive_predecessor(raw,pin,published=False)

    def test_service_terminal_journal_uses_same_derived_revision_as_the_open_class(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        from tempfile import TemporaryDirectory
        from unittest.mock import MagicMock
        from ha_control import initial_lease_service as service
        plan,files=UnpublishedSealTests().continuation_fixture(8)
        raw=files['unpublished-predecessor-state.json'];prior=json.loads(raw)
        plan['cluster']='fixture'
        admission=SimpleNamespace(assert_held=MagicMock(return_value=True),publish=MagicMock())
        @contextmanager
        def admitted(*args):yield admission
        observer=SimpleNamespace(assert_intent=MagicMock(),recovery_admission=admitted)
        lease=SimpleNamespace(proof_started=service.time.monotonic(),open=MagicMock(return_value='f'*64),
            renew=MagicMock(side_effect=TransitionBlocked('synthetic_stop')),revoke=MagicMock(return_value=True))
        handoff={key:prior[key] for key in ('attempt_id','legacy_plan_sha256','restore_receipt_sha256','activation_intent_sha256')}
        handoff['controller_plan_sha256']='a'*64
        with TemporaryDirectory() as folder:
            backend=SimpleNamespace(plan=plan,plan_path=Path(folder)/'plan.json',fence_adapter=SimpleNamespace(
                _unpublished_verified=plan['initial_unpublished_recovery'],_predecessor_terminal=raw))
            journal=Path(folder)/'journal/controller-state.json';journal.parent.mkdir()
            with patch.object(service,'_recovery_predecessor',return_value=prior['recovery']), \
                 patch.object(service,'InitialPgLeaseUnpublishedRecovery',return_value=lease),patch.object(service.time,'sleep'):
                with self.assertRaisesRegex(TransitionBlocked,'synthetic_stop'):service._serve(backend,journal,observer,handoff,recovering=True)
            record=json.loads(journal.read_bytes())
            self.assertEqual((record['revision'],record['kind'],record['sequence']),(10,'initial-wsl-cutover-unpublished',1))
            admission.publish.assert_called_once();lease.revoke.assert_called_once()

    def test_sdk_publication_and_continuity_require_actual_hash_bound_terminal_bytes(self):
        from types import SimpleNamespace
        from ha_control import native_windows_activation as native
        plan,files=UnpublishedSealTests().continuation_fixture(8)
        value=plan['initial_unpublished_recovery'];raw=files['unpublished-predecessor-state.json'];prior=json.loads(raw)
        custody=SimpleNamespace(binding=SimpleNamespace(plan_sha256=prior['legacy_plan_sha256'],attempt_id=prior['attempt_id']),live_sha256='3'*64)
        lease=native.WindowsActivationLease(custody,r'C:\fixture\binding.json',
            {'first_write_proof_sha256':'4'*64,'service_instance_sha256':'5'*64})
        recovery=prior['recovery']
        heartbeat={'windows_generation':1,'windows_generation_sha256':'a'*64,'live_lease_sha256':'b'*64}
        with patch.object(native,'fixed_script',return_value='fixture') as script:
            self.assertRegex(lease.verify_recovery_publication(lambda *args:heartbeat,recovery,native.time.monotonic()+2,
                observe_pair=lambda **kwargs:'c'*64,assert_guard=lambda:None,unpublished=value,predecessor_terminal=raw),r'^[a-f0-9]{64}$')
            self.assertEqual(script.call_args.args[2]['UnpublishedPredecessorTerminalSha256'],hashlib.sha256(raw).hexdigest())
            for method in (lease.verify_recovery_publication,lease.verify_recovery_continuity):
                for bad in (None,8,raw+b' '):
                    with self.subTest(method=method.__name__,bad=type(bad)),self.assertRaises(TransitionBlocked):
                        method(lambda *args:heartbeat,recovery,native.time.monotonic()+2,observe_pair=lambda **kwargs:'c'*64,
                            assert_guard=lambda:None,unpublished=value,predecessor_terminal=bad)

    def test_guest_local_database_requires_the_raw_derived_open_stamp(self):
        from types import SimpleNamespace
        from datetime import datetime,timezone
        from ha_control import native_guest_live as live
        _,files=UnpublishedSealTests().continuation_fixture(8)
        raw=files['unpublished-predecessor-state.json'];pin=hashlib.sha256(raw).hexdigest()
        pins=SimpleNamespace(cluster='test',system_id='123',release={'source_commit':'a'*40},plan_sha256='b'*64)
        pg=SimpleNamespace(peer_ipv4='peer')
        now=datetime.now(timezone.utc)
        observed=dict(identity=['postgres','ha','ha_A',False,'123','on','remote_apply','FIRST1(ha_b)','on','on',1],
            authority=['test',1,10,'A','B',1,['A'],'A','AS',True,True],
            lease_clock=[now.isoformat(),(now+timedelta(seconds=4)).isoformat()],
            binding=[['test','123','a'*40,'b'*64,live.runtime.config.BROKER_ROLE]],peers=[['peer','0/10']])
        with patch.object(live.restore,'_pg',return_value=observed):
            live._lease(pins,pg,recovery_deadline=live.time.monotonic()+2,unpublished=True,
                predecessor_terminal=raw,predecessor_sha256=pin)
            for wrong in (8,9,11):
                observed['authority'][2]=wrong
                with self.assertRaises(TransitionBlocked):live._lease(pins,pg,recovery_deadline=live.time.monotonic()+2,
                    unpublished=True,predecessor_terminal=raw,predecessor_sha256=pin)
            observed['authority'][2]=10
            with self.assertRaises(TransitionBlocked):live._lease(pins,pg,recovery_deadline=live.time.monotonic()+2,
                unpublished=True,predecessor_terminal=raw+b' ',predecessor_sha256=pin)

    def test_before_save_uses_current_published_terminal_and_both_independent_copies(self):
        from types import SimpleNamespace
        from ha_control import native_guest_postboot as guest,native_stdio_bridge as wire
        _,files=UnpublishedSealTests().continuation_fixture(10)
        prior=json.loads(files['unpublished-predecessor-state.json']);prior['sequence']=12
        raw=json.dumps(prior).encode();pin=hashlib.sha256(raw).hexdigest()
        args={'predecessor_plan_sha256':prior['controller_plan_sha256'],
            'predecessor_unpublished_attempt_id':prior['unpublished']['attempt_id'],
            'recovery_attempt_id':prior['recovery']['attempt_id']}
        binding=wire.Binding(prior['legacy_plan_sha256'],prior['attempt_id'],'3'*64,'4'*64,'5'*64)
        request=SimpleNamespace(args=args,binding=binding)
        with patch.object(guest.worker.runtime,'_read',return_value=raw) as read:
            self.assertEqual(guest.closed_revision(request),11)
            self.assertEqual([call.args[0].name for call in read.call_args_list],[
                'predecessor-plan-'+prior['controller_plan_sha256']+'.json','predecessor-terminal-'+pin+'.json'])
            self.assertTrue(all(call.args[1:]==(0,{0o400}) for call in read.call_args_list))
        for field in ('predecessor_plan_sha256','predecessor_unpublished_attempt_id'):
            changed=SimpleNamespace(args=args|{field:'f'*64},binding=binding)
            with patch.object(guest.worker.runtime,'_read',return_value=raw),self.assertRaises(TransitionBlocked):guest.closed_revision(changed)
        with patch.object(guest.worker.runtime,'_read',side_effect=(raw,raw+b' ')),self.assertRaises(TransitionBlocked):guest.closed_revision(request)

class SamebootPostbootSealTests(unittest.TestCase):
    def fixture(self,chain=False):
        from ha_control.initial_postboot import SAMEBOOT_KIND,RESUME_KIND
        encode=lambda value:json.dumps(value,sort_keys=True).encode()
        digest=lambda raw:hashlib.sha256(raw).hexdigest()
        if chain:
            oldplan,oldfiles=self.fixture()
            oldrevision=14
        else:
            oldplan,oldfiles=PostbootAfterUnpublishedSealTests().fixture(10)
            oldrevision=12
        oldplan=deepcopy(oldplan);oldplan['source_commit']='c'*40
        oldbinding=json.loads(oldfiles[FILES[1]])
        settings=dict(line.split('=',1) for line in oldfiles[FILES[2]].decode().splitlines())
        from pathlib import PureWindowsPath
        oldplan['initial_service_start']={'service_image_path_sha256':oldbinding['service_image_path_sha256'],
            'binding_path':str(PureWindowsPath(settings['plan_path']).parent/('initial-recovery-service-'+oldplan['initial_cutover_recovery']['attempt_id']+'.json'))}
        oldvalue=oldplan['initial_recovery_postboot']
        oldpin=digest(encode(oldplan))
        prior=json.loads(oldfiles[FILES[0]])
        prior.pop('unpublished',None)
        prior.update(kind=RESUME_KIND if chain else 'initial-wsl-cutover-postboot-unpublished',
            postboot=oldvalue,controller_plan_sha256=oldpin,revision=oldrevision,sequence=200,
            phase='lease_lost',failure_code='initial_service_handoff_unverified')
        binding=json.loads(oldfiles[FILES[1]]);binding['controller_plan_sha256']=oldpin
        args=oldfiles[FILES[2]].decode().splitlines()
        args=[('plan_sha256='+oldpin) if line.startswith('plan_sha256=') else line for line in args]
        plan=deepcopy(oldplan)
        value=envelope()|dict(attempt_id='1234'*16,predecessor_kind=SAMEBOOT_KIND,
            predecessor_plan_sha256=oldpin,stopped_attempt_id=oldvalue.get('stopped_attempt_id',oldvalue['attempt_id']),
            stopped_receipt_sha256=digest(oldfiles[FILES[6]]),previous_windows_boot_sha256='6'*64)
        if chain:value['attempt_id']='4321'*16
        plan['initial_recovery_postboot']=value
        files={FILES[0]:encode(prior),FILES[1]:encode(binding),FILES[2]:('\n'.join(args)+'\n').encode(),
               FILES[6]:oldfiles[FILES[6]],'postboot-predecessor-plan.json':encode(oldplan)}
        for name,key in zip(FILES[:3],('predecessor_terminal_sha256','predecessor_binding_sha256','predecessor_args_sha256')):
            value[key]=digest(files[name])
        pre=json.loads(oldfiles[FILES[3]])
        pre.update(windows_boot_sha256='6'*64,closed_revision=oldrevision+1,
            predecessor_terminal_sha256=value['predecessor_terminal_sha256'],predecessor_binding_sha256=value['predecessor_binding_sha256'],
            predecessor_args_sha256=value['predecessor_args_sha256'])
        files[FILES[3]]=encode(pre);value['predecessor_seal_sha256']=digest(files[FILES[3]])
        resumed=json.loads(oldfiles[FILES[4]])
        resumed.pop('saved',None);resumed.pop('resumed',None)
        resumed.update(previous_windows_boot_sha256='6'*64,closed_revision=oldrevision+1,
            observed={'A':'Running','B':'Running'},closed_pair_sha256=pre['closed_pair_sha256'],
            predecessor_seal_sha256=value['predecessor_seal_sha256'])
        files[FILES[4]]=encode(resumed)
        acceptance=json.loads(oldfiles[FILES[5]])
        acceptance.update(postboot=value,resume_evidence_sha256=digest(files[FILES[4]]))
        files[FILES[5]]=encode(acceptance)
        return plan,files

    def test_carryover_seal_keeps_original_stop_and_binds_fresh_admission_digest(self):
        plan,files=self.fixture()
        encode=lambda value:json.dumps(value,sort_keys=True).encode()
        digest=lambda raw:hashlib.sha256(raw).hexdigest()
        value=plan['initial_recovery_postboot']
        original_stop=files[FILES[6]]
        value['carryover_receipt_sha256']='e'*64
        pre=json.loads(files[FILES[3]])
        pre['guests']['A']['container_inventory_sha256']='e'*64
        files[FILES[3]]=encode(pre)
        value['predecessor_seal_sha256']=digest(files[FILES[3]])
        resumed=json.loads(files[FILES[4]])
        resumed.update(guests=pre['guests'],predecessor_seal_sha256=value['predecessor_seal_sha256'])
        resumed['guest_observation_sha256']['A']='e'*64
        files[FILES[4]]=encode(resumed)
        acceptance=json.loads(files[FILES[5]])
        acceptance.update(postboot=value,resume_evidence_sha256=digest(files[FILES[4]]))
        files[FILES[5]]=encode(acceptance)
        self.assertEqual(validate_seal(plan,'a'*64,files),value)
        self.assertEqual(files[FILES[6]],original_stop)
        resumed['guest_observation_sha256']['A']=value['stopped_receipt_sha256']
        files[FILES[4]]=encode(resumed)
        acceptance['resume_evidence_sha256']=digest(files[FILES[4]])
        files[FILES[5]]=encode(acceptance)
        with self.assertRaises(TransitionBlocked):validate_seal(plan,'a'*64,files)

    def test_same_boot_closed_thirteen_and_original_closed_eleven_are_separate(self):
        from ha_control.initial_unpublished import derive_predecessor
        plan,files=self.fixture()
        self.assertEqual(validate_seal(plan,'a'*64,files),plan['initial_recovery_postboot'])
        self.assertEqual(json.loads(files[FILES[6]])['closed_revision'],11)
        self.assertEqual(json.loads(files[FILES[3]])['closed_revision'],13)
        self.assertEqual(derive_predecessor(files[FILES[0]],plan['initial_recovery_postboot']['predecessor_terminal_sha256'],published=True),(13,14,15))
        with self.assertRaises(TransitionBlocked):
            derive_predecessor(files[FILES[0]],plan['initial_recovery_postboot']['predecessor_terminal_sha256'],published=False)

    def test_same_contract_can_continue_with_original_stop_tuple_preserved(self):
        plan,files=self.fixture(chain=True)
        self.assertEqual(validate_seal(plan,'a'*64,files),plan['initial_recovery_postboot'])
        self.assertEqual(json.loads(files[FILES[6]])['closed_revision'],11)
        self.assertEqual(json.loads(files[FILES[3]])['closed_revision'],15)
        self.assertEqual(plan['initial_recovery_postboot']['stopped_attempt_id'],'0'*64)

    def test_tag_and_all_pins_are_required_and_other_modes_stay_exclusive(self):
        from ha_control.initial_postboot import SAMEBOOT_FIELDS
        plan,files=self.fixture()
        for key in SAMEBOOT_FIELDS|{'predecessor_kind'}:
            changed=deepcopy(plan);changed['initial_recovery_postboot'].pop(key)
            with self.subTest(key=key),self.assertRaises(TransitionBlocked):scope(changed)
        for key in ('initial_unpublished_recovery','initial_recovery_rearm'):
            changed=deepcopy(plan);changed[key]={}
            with self.subTest(key=key),self.assertRaises(TransitionBlocked):scope(changed)
        for key in ('stopped_attempt_id','stopped_receipt_sha256','predecessor_plan_sha256'):
            changed=deepcopy(plan);changed['initial_recovery_postboot'][key]='f'*64
            with self.subTest(key=key),self.assertRaises(TransitionBlocked):validate_seal(changed,'a'*64,files)

    def test_boot_and_raw_identity_changes_refuse_before_authority(self):
        plan,files=self.fixture()
        for key in ('windows_boot_sha256',):
            changed=deepcopy(plan);changed['windows_initial_observer'][key]='f'*64
            with self.assertRaises(TransitionBlocked):validate_seal(changed,'a'*64,files)
        for name in set(files)-{FILES[5]}:
            with self.subTest(name=name),self.assertRaises(TransitionBlocked):
                validate_seal(plan,'a'*64,files|{name:files[name]+b' '})
        changed=deepcopy(plan);changed['initial_recovery_postboot']['predecessor_kind']='published-unpublished-recovery-v1'
        with self.assertRaises(TransitionBlocked):scope(changed)

    def test_repin_cannot_rewrite_original_stop_revision_or_current_pair(self):
        plan,files=self.fixture()
        # A changed receipt cannot be substituted even by updating just the
        # successor scope: predecessor custody/seal/acceptance remain bound.
        stopped=json.loads(files[FILES[6]]);stopped['closed_revision']=13
        files[FILES[6]]=json.dumps(stopped).encode()
        plan['initial_recovery_postboot']['stopped_receipt_sha256']=hashlib.sha256(files[FILES[6]]).hexdigest()
        with self.assertRaises(TransitionBlocked):validate_seal(plan,'a'*64,files)


    def test_native_sdk_passes_exact_tag_and_original_stop_pins(self):
        from types import SimpleNamespace
        from ha_control import native_windows_activation as native
        plan,files=self.fixture();value=plan['initial_recovery_postboot'];prior=json.loads(files[FILES[0]])
        custody=SimpleNamespace(binding=SimpleNamespace(plan_sha256=prior['legacy_plan_sha256'],attempt_id=prior['attempt_id']),live_sha256='3'*64)
        lease=native.WindowsActivationLease(custody,r'C:\fixture\binding.json',
            {'first_write_proof_sha256':'4'*64,'service_instance_sha256':'5'*64})
        heartbeat={'windows_generation':1,'windows_generation_sha256':'a'*64,'live_lease_sha256':'b'*64}
        with patch.object(native,'fixed_script',return_value='fixture') as script:
            lease.verify_recovery_publication(lambda *args:heartbeat,prior['recovery'],native.time.monotonic()+2,
                observe_pair=lambda **kwargs:'c'*64,assert_guard=lambda:None,postboot=value,predecessor_terminal=files[FILES[0]])
            values=script.call_args.args[2]
            self.assertEqual(values['PostbootPredecessorKind'],'published-postboot-sameboot-v1')
            self.assertEqual(values['PostbootStoppedAttemptId'],value['stopped_attempt_id'])
            self.assertEqual(values['PostbootStoppedReceiptSha256'],value['stopped_receipt_sha256'])
            self.assertEqual(values['PostbootPredecessorPlanSha256'],value['predecessor_plan_sha256'])

    def test_postboot_acceptance_holds_configured_stopped_receipt_on_executed_path(self):
        from tempfile import TemporaryDirectory
        runtime=shutil.which('pwsh');self.assertIsNotNone(runtime)
        plan,files=self.fixture();value=plan['initial_recovery_postboot']
        old=json.loads(files['postboot-predecessor-plan.json']);binding=json.loads(files[FILES[1]])
        source=ROOT/'infra/k8s/ha_control/windows-ha-start-initial-service.ps1'
        with TemporaryDirectory() as folder:
            base=Path(folder);host=base/'host';host.mkdir()
            args=files[FILES[2]].decode().splitlines()
            changes={'plan_path':str(base/'prior'/'plan.json'),'service_name':plan['windows_enforced']['service_name'],
                     'service_sid':plan['windows_enforced']['service_sid'],'host_sha256':plan['windows_enforced']['service_image_sha256']}
            args=[key+'='+changes.get(key,val) for key,val in (line.split('=',1) for line in args)]
            raw=('\n'.join(args)+'\n').encode();(host/'service.args').write_bytes(raw)
            (base/'postboot-predecessor-args.txt').write_bytes(raw)
            value['predecessor_args_sha256']=hashlib.sha256(raw).hexdigest()
            binding['state_path']=str(base/'prior'/'journal'/'controller-state.json')
            fixture=base/'fixture.json'
            fixture.write_text(json.dumps({'plan':plan,'binding':binding,'terminal':json.loads(files[FILES[0]])}))
            script=base/'exercise.ps1'
            script.write_text(r"""$ErrorActionPreference='Stop'
$fixture=ConvertFrom-Json ([IO.File]::ReadAllText($args[0]))
$root=[IO.Path]::GetDirectoryName($args[0]);$planPath=Join-Path $root 'plan.json'
$fixtureService=[pscustomobject]@{Name=$fixture.plan.windows_enforced.service_name;State='Stopped';StartMode='Disabled';ProcessId=0;
    StartName=('NT SERVICE\'+$fixture.plan.windows_enforced.service_name);ServiceType='Own Process';DesktopInteract=$false;
    PathName=('"'+(Join-Path $root 'host/host.exe')+'"')}
function FixtureDigest([string]$text){$h=[Security.Cryptography.SHA256]::Create();try{[BitConverter]::ToString($h.ComputeHash([Text.Encoding]::UTF8.GetBytes($text))).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}}
$fixture.binding.service_image_path_sha256=FixtureDigest $fixtureService.PathName
$boot=[datetime]::Parse('2026-11-20T00:00:00Z').ToUniversalTime()
$fixture.plan.windows_initial_observer.windows_boot_sha256=FixtureDigest ($boot.Ticks.ToString())
$script:checked=[Collections.Generic.List[string]]::new()
function Assert-UnpublishedTcbPath([string]$Path){$script:checked.Add($Path)}
function Assert-Executable([string]$Path,[string]$Pin){}
function Get-CimInstance([string]$ClassName,[string]$Filter,[int]$OperationTimeoutSec){if($ClassName -ceq 'Win32_OperatingSystem'){return [pscustomobject]@{LastBootUpTime=$boot}};return $fixtureService}
function Read-PinnedJson([string]$Path,[string]$Pin){if($Path -ceq $planPath){return $fixture.plan};if($Path.EndsWith('postboot-predecessor-binding.json')){return $fixture.binding};if($Path.EndsWith('postboot-predecessor-state.json')){return $fixture.terminal};throw 'fixture_unexpected_read'}
$tokens=$null;$errors=$null;$ast=[Management.Automation.Language.Parser]::ParseFile($args[1],[ref]$tokens,[ref]$errors)
foreach($name in @('Assert-PostbootEnvelope','Seal-HaInitialPostbootAcceptance')){
    $node=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$true)
    $text=$node.Extent.Text
    if($name -ceq 'Seal-HaInitialPostbootAcceptance'){
        $text=$text.Replace('foreach($entry in $actual){',"[pscustomobject]@{actual=@(`$actual);checked=@(`$script:checked)}|ConvertTo-Json -Depth 8 -Compress;return;foreach(`$entry in `$actual){")
    }
    . ([scriptblock]::Create($text))
}
$hashPattern='\A[a-f0-9]{64}\z'
Seal-HaInitialPostbootAcceptance $planPath ('a'*64)
""")
            result=subprocess.run([runtime,'-NoProfile','-File',str(script),str(fixture),str(source)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            observed=json.loads(result.stdout)
            self.assertEqual(len(observed['actual']),5)
            self.assertEqual(observed['actual'][-1],{'path':str(base/'prior'/'postboot-stopped-inventory.json'),'pin':value['stopped_receipt_sha256']})
            self.assertEqual(set(observed['checked']),{row['path'] for row in observed['actual']})

    def test_powershell_derivation_matches_python_and_rejects_malformed_terminal(self):
        from tempfile import TemporaryDirectory
        from ha_control.initial_unpublished import derive_predecessor
        runtime=shutil.which('pwsh');self.assertIsNotNone(runtime)
        _,files=self.fixture();prior=json.loads(files[FILES[0]])
        variants=[prior,prior|{'revision':True},prior|{'revision':13},prior|{'sequence':0},
                  prior|{'kind':'initial-wsl-cutover-unpublished'},prior|{'extra':'1'*64}]
        changed=deepcopy(prior);changed['postboot']['predecessor_kind']='published-postboot-sameboot-v1';variants.append(changed)
        with TemporaryDirectory() as folder:
            rawpath=Path(folder)/'terminal.json'
            for filename in ('windows-ha-start-initial-service.ps1','windows-ha-cutover-live-authority.ps1'):
                source=ROOT/'infra/k8s/ha_control'/filename
                for index,value in enumerate(variants):
                    raw=json.dumps(value).encode();pin=hashlib.sha256(raw).hexdigest();rawpath.write_bytes(raw)
                    command=("$ErrorActionPreference='Stop';$tokens=$null;$errors=$null;"
                        "$ast=[Management.Automation.Language.Parser]::ParseFile('"+str(source)+"',[ref]$tokens,[ref]$errors);"
                        "$node=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq 'Get-InitialPredecessorNumbers'},$true);"
                        "Invoke-Expression $node.Extent.Text;try{$r=Get-InitialPredecessorNumbers ([IO.File]::ReadAllBytes('"+str(rawpath)+"')) '"+pin+"' $true;"
                        "$r|Select-Object closed,opened,revoked|ConvertTo-Json -Compress}catch{exit 3}")
                    result=subprocess.run([runtime,'-NoProfile','-Command',command],capture_output=True,text=True)
                    with self.subTest(source=filename,index=index):
                        if index==0:
                            self.assertEqual(result.returncode,0,result.stderr)
                            self.assertEqual(json.loads(result.stdout),dict(zip(('closed','opened','revoked'),derive_predecessor(raw,pin,published=True))))
                        else:self.assertEqual(result.returncode,3,result.stderr)


class SamebootPostbootPgTests(PostbootPgTests):
    def postboot_lease(self):
        from ha_control.initial_pg_lease import InitialPgLeasePostbootResume
        callback=self.recovery_lease().assert_legacy_closed
        plan,files=SamebootPostbootSealTests().fixture()
        self.revision=13
        self.backend.fence_adapter._predecessor_terminal=files[FILES[0]]
        self.backend.plan.update(_approval_action=ACTION,initial_recovery_postboot=plan['initial_recovery_postboot'])
        return InitialPgLeasePostbootResume(self.backend,callback)

    # Reuse fixtures, not inherited old-mode assertions with fixed 7/8 stamps.
    def test_fixed_eight_renew_and_nine_revoke_preserve_prior_probes(self):
        lease=self.postboot_lease();lease.open(self.state);self.assertEqual(self.revision,14)
        self.now+=timedelta(seconds=1);lease.renew(self.state)
        self.assertTrue(lease.revoke(self.state));self.assertEqual(self.revision,15)
        self.assertFalse(self.opened)

    def test_wrong_revision_and_foreign_expiry_cannot_be_adopted(self):
        from ha_control.initial_pg_lease import InitialPgLeasePostbootResume
        for wrong in (7,9,11,12,14,15):
            self.setUp();lease=self.postboot_lease();self.revision=wrong
            with self.subTest(wrong=wrong),self.assertRaises(TransitionBlocked):lease.open(self.state)
        self.setUp();lease=self.postboot_lease();lease.open(self.state)
        self.assertFalse(InitialPgLeasePostbootResume(self.backend,lease.assert_legacy_closed).revoke(self.state))

    def test_changed_envelope_and_ambiguous_commit_fail_closed(self):
        lease=self.postboot_lease();self.lost_commit=True
        with self.assertRaisesRegex(TransitionBlocked,'fixture_commit_response_lost'):lease.open(self.state)
        self.assertEqual(self.revision,15)
        self.setUp();lease=self.postboot_lease();lease.open(self.state)
        self.backend.plan['initial_recovery_postboot']['stopped_attempt_id']='f'*64
        with self.assertRaises(TransitionBlocked):lease.renew(self.state)

if __name__=='__main__':unittest.main()
