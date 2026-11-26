"""App-only explicit continuation; no production or database access."""
import copy
from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'infra/k8s'))
from ha_control import initial_app_resume as resume, native_guest_worker as w, native_stdio_bridge as wire
from ha_control.controller import TransitionBlocked

REAL_INVENTORY=w._initial_recovery_inventory

class InitialAppResumeTests(unittest.TestCase):
    def setUp(self):
        from tests.test_ha_postboot import UnpublishedSealTests
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.root=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        binding=wire.Binding('1'*64,'2'*64,'3'*64,'4'*64,'5'*64)
        args={key:'6'*64 for key in wire.INITIAL_RECOVERY_ARGS}
        args.update(recovery_attempt_id='7'*64,hold_sha256=wire._digest(b'hold'),
            unpublished=UnpublishedSealTests().fixture()[0]['initial_unpublished_recovery'],unpublished_plan_sha256='a'*64)
        self.request=wire.Request(binding,'A',wire.GUEST_START_INITIAL_UNPUBLISHED,2,'8'*64,args)
        self.containers={role:{'container_id':f'{i:064x}','image_id':'sha256:'+'9'*64} for i,role in enumerate(w.RECOVERY_START_ORDER,1)}
        self.hold={'containers':self.containers,'container_contracts':{r:{'layout_sha256':'b'*64,'started_at':'old','running':False} for r in self.containers}}
        self.contracts=copy.deepcopy(self.hold['container_contracts'])
        for role in w.RECOVERY_START_ORDER[:2]:self.contracts[role]['started_at']='interrupted'
        self.authority=self.root/'authority';self.authority.write_bytes(b'authority')
        self.active={'authority_files':{str(self.authority):wire._digest(b'authority')}}
        self.prepared={'manifest_sha256':'a'*64}
        self.stage=self.root/binding.attempt_id;self.stage.mkdir()
        self.stem=resume.prefix(self.request)
        proof={'windows_proof_sha256':'c'*64,'guest_proof_sha256':'d'*64}
        old=replace(self.request,nonce='9'*64,sequence=1)
        intent={'schema_version':1,'status':'INTENT',**w._scope(old),'args':args,'containers':self.containers,
            'authority_files':self.active['authority_files'],'proof':proof,'automatic_retry_permitted':False}
        self.intent=wire._bytes(intent);self.intent_pin=wire._digest(self.intent)
        (self.stage/(self.stem+'intent.json')).write_bytes(self.intent)
        progress=wire._bytes({'intent_sha256':self.intent_pin,'started':[w.RECOVERY_START_ORDER[0]],'proof':proof})
        self.progress_pins={'progress-'+w.RECOVERY_START_ORDER[0]+'.json':wire._digest(progress)}
        (self.stage/(self.stem+next(iter(self.progress_pins)))).write_bytes(progress)
        self.original={p.name:p.read_bytes() for p in self.stage.iterdir()}
        self.spec=b'{"guest_ip":"127.0.0.1","pg_a":"127.0.0.1","node":"A"}'
        (self.stage/'guest-spec.json').write_bytes(self.spec)
        pins=SimpleNamespace(installation_id=args['installation_id'],node='A',attempt_id=binding.attempt_id,spec_sha256=wire._digest(self.spec))
        self.commands=[];self.challenges=[];self.failed_boundary=None;self.start_sequence=0
        def read(path,*a,**kw):return Path(path).read_bytes()
        def write(path,body,*a):
            with Path(path).open('xb') as out:out.write(body)
        def docker(*command,**kw):
            self.commands.append(command)
            if command[0]=='start':
                self.start_sequence+=1
                role=next(r for r,v in self.containers.items() if v['container_id']==command[1])
                self.contracts[role].update(running=True,started_at='started-'+str(self.start_sequence))
            if command[0]=='stop':
                for role,pin in self.containers.items():
                    if pin['container_id'] in command[3:]:self.contracts[role]['running']=False
        def inventory(*a,**kw):return copy.deepcopy(self.contracts),args['protected_inventory_sha256']
        for target,name,value in ((w,'ROOT',self.root),(w,'_guest_mutation_descriptor',lambda:ExitStack()),
            (w,'_read_initial_recovery_hold',lambda req:(b'hold',self.hold,self.prepared,self.active)),
            (w,'_initial_recovery_inventory',inventory),(w,'_assert_car_route_blocked',lambda *a:None),
            (w,'GuestWorker',lambda req:SimpleNamespace(_prepared_pins=lambda *_:(object(),pins,{}))),
            (w.live,'_authority_paths',lambda:{'test':self.authority}),
            (w.live,'observe_unpublished_a_lease',lambda *a,**kw:'d'*64),
            (w.live,'observe_postboot_a_lease',lambda *a,**kw:'d'*64),
            (w.runtime,'_read',read),(w.runtime,'_write',write),(w.runtime,'_docker',docker),
            (w.runtime,'_inventory',lambda:{v['container_id']:{'State':{'Running':self.contracts[r]['running']}} for r,v in self.containers.items()}),
            (w.activation,'_tls',lambda _: (object(),'leaf')),(w.activation,'_wait_for_app',lambda *a,**kw:None)):
            self.stack.enter_context(patch.object(target,name,value))
    def attest(self):
        return resume.attest(self.request,service_instance_sha256='e'*64,prior_intent_sha256=self.intent_pin,prior_progress_sha256=self.progress_pins)
    def windows(self,challenge,boundary):
        self.challenges.append((challenge,boundary))
        return None if boundary==self.failed_boundary else 'c'*64
    def load(self):return resume.load(self.request,b'hold',self.hold,self.prepared,self.active)
    def test_attested_resume_starts_exact_eight_and_preserves_original_records(self):
        result=self.attest();body,value,stem=self.load()
        self.assertEqual(resume.path(self.request).stat().st_mode&0o777,0o400)
        self.assertEqual(result['service_instance_sha256'],'e'*64)
        self.assertEqual(w.start_initial_recovery(self.request,self.windows).status,'PASS')
        self.assertEqual(self.commands,[('start',v['container_id']) for v in self.containers.values()])
        for name,raw in self.original.items():self.assertEqual((self.stage/name).read_bytes(),raw)
        record=json.loads((self.stage/(stem+'intent.json')).read_bytes())
        self.assertEqual(record['resume_authorization_sha256'],result['authorization_sha256'])
        for challenge,boundary in self.challenges:
            scope={'request':w._scope(self.request),'hold_sha256':self.request.args['hold_sha256'],'boundary':boundary,
                'unpublished':self.request.args['unpublished'],'unpublished_plan_sha256':self.request.args['unpublished_plan_sha256'],
                'resume_authorization_sha256':wire._digest(body)}
            self.assertEqual(challenge,wire._digest(wire._bytes(scope)))
        with self.assertRaises(TransitionBlocked):self.load()
        with self.assertRaises(TransitionBlocked):self.attest()
    def test_no_authorization_keeps_original_intent_rejection(self):
        with self.assertRaises(TransitionBlocked):w.start_initial_recovery(self.request,self.windows)
        self.assertEqual(self.commands,[])
    def test_prior_done_or_unknown_or_noncontiguous_progress_rejected(self):
        for suffix in ('done.json','unexpected.json','progress-book-memo.json'):
            p=self.stage/(self.stem+suffix);p.write_bytes(b'{}')
            with self.assertRaises(TransitionBlocked):self.attest()
            p.unlink()
    def test_changed_prior_record_and_extra_changed_container_rejected(self):
        p=self.stage/(self.stem+'intent.json');p.write_bytes(self.intent+b' ')
        with self.assertRaises(TransitionBlocked):self.attest()
        p.write_bytes(self.intent)
        self.contracts[w.RECOVERY_START_ORDER[-1]]['started_at']='other'
        with self.assertRaises(TransitionBlocked):self.attest()
    def test_request_nonce_baseline_and_authorization_tampering_rejected(self):
        self.attest()
        old=self.request;self.request=replace(old,nonce='f'*64)
        with self.assertRaises(TransitionBlocked):self.load()
        self.request=old
        self.contracts[w.RECOVERY_START_ORDER[0]]['started_at']='later'
        with self.assertRaises(TransitionBlocked):self.load()
        self.contracts[w.RECOVERY_START_ORDER[0]]['started_at']='interrupted'
        p=resume.path(self.request);v=json.loads(p.read_bytes());v['service_instance_sha256']=None;p.chmod(0o600);p.write_bytes(wire._bytes(v));p.chmod(0o400)
        with self.assertRaises(TransitionBlocked):self.load()
    def test_running_inventory_cannot_be_attested(self):
        self.contracts[w.RECOVERY_START_ORDER[0]]['running']=True
        with self.assertRaises(TransitionBlocked):self.attest()
    def test_failed_resume_preserves_fence_and_cleans_only_this_attempt(self):
        self.attest();self.failed_boundary='recovery_after:book-memo'
        with self.assertRaises(TransitionBlocked):w.start_initial_recovery(self.request,self.windows)
        self.assertEqual(self.commands[-1],('stop','--signal=SIGTERM','--timeout=-1',self.containers['book-memo']['container_id'],self.containers['ha-broker']['container_id']))
        self.assertFalse(any(p.name.endswith('resume-done.json') for p in self.stage.iterdir()))
        with self.assertRaises(TransitionBlocked):self.load()
        for name,raw in self.original.items():self.assertEqual((self.stage/name).read_bytes(),raw)
    def test_preserved_auth_or_progress_mutation_rejects(self):
        self.attest();body,value,_=self.load()
        p=self.stage/(self.stem+next(iter(self.progress_pins)));p.write_bytes(b'{}')
        with self.assertRaises(TransitionBlocked):resume.preserved(self.request,body,value,self.hold,self.active,b'hold')

    def prepare_sameboot_request(self):
        from tests.test_ha_postboot import SamebootPostbootSealTests
        value=SamebootPostbootSealTests().fixture()[0]['initial_recovery_postboot'].copy()
        args={key:self.request.args[key] for key in (*wire.INITIAL_RECOVERY_ARGS,'hold_sha256')}
        stopped_attempt=value['stopped_attempt_id']
        for row in self.contracts.values():
            row['started_at']='original-postboot-stop'
        record=wire._bytes({'schema_version':1,'binding':self.request.binding.fields(),
            'args':args|{'postboot_attempt_id':stopped_attempt},'hold_sha256':args['hold_sha256'],
            'containers':self.containers,'container_contracts':self.contracts,
            'protected_inventory_sha256':args['protected_inventory_sha256'],
            'closed_revision':7,'closed_proof_sha256':'a'*64})
        self.stopped_path=self.stage/('postboot-'+stopped_attempt+'-stopped.json')
        self.stopped_path.write_bytes(record)
        value['stopped_receipt_sha256']=wire._digest(record)
        args.update(postboot=value,postboot_plan_sha256='a'*64,postboot_stopped_receipt_sha256=wire._digest(record))
        self.request=replace(self.request,verb=wire.GUEST_START_INITIAL_POSTBOOT,args=args)
        self.stem=resume.prefix(self.request)
        self.progress_pins={}
        old=replace(self.request,nonce='9'*64,sequence=1)
        self.intent=wire._bytes({'schema_version':1,'status':'INTENT',**w._scope(old),'args':args,
            'containers':self.containers,'authority_files':self.active['authority_files'],
            'proof':{'windows_proof_sha256':'c'*64,'guest_proof_sha256':'d'*64},'automatic_retry_permitted':False})
        self.intent_pin=wire._digest(self.intent)
        self.intent_path=self.stage/(self.stem+'intent.json')
        self.intent_path.write_bytes(self.intent)

    def test_sameboot_resume_uses_original_stop_baseline_and_one_shot_namespace(self):
        self.prepare_sameboot_request()
        with self.assertRaises(TransitionBlocked):
            w.start_initial_recovery(self.request,self.windows)
        self.assertEqual(self.commands,[])
        result=self.attest()
        body,_,stem=self.load()
        self.assertEqual(stem,self.stem+'resume-')
        self.assertEqual(resume.path(self.request).name,'initial-app-resume-postboot-'+self.request.args['postboot']['attempt_id']+'.json')
        self.assertEqual(w.start_initial_recovery(self.request,self.windows).status,'PASS')
        self.assertEqual(self.commands,[('start',v['container_id']) for v in self.containers.values()])
        self.assertEqual(self.intent_path.read_bytes(),self.intent)
        self.assertEqual(wire._digest(self.stopped_path.read_bytes()),self.request.args['postboot_stopped_receipt_sha256'])
        self.assertEqual(wire._digest(body),result['authorization_sha256'])
        for challenge,boundary in self.challenges:
            self.assertEqual(challenge,wire._digest(wire._bytes({'request':w._scope(self.request),
                'hold_sha256':self.request.args['hold_sha256'],'boundary':boundary,
                'postboot':self.request.args['postboot'],'postboot_plan_sha256':self.request.args['postboot_plan_sha256'],
                'resume_authorization_sha256':wire._digest(body)})))
        with self.assertRaises(TransitionBlocked):
            w.start_initial_recovery(self.request,self.windows)
        self.assertEqual(len(self.commands),8)

    def test_sameboot_resume_rejects_any_original_progress_done_or_started_container(self):
        self.prepare_sameboot_request()
        role=w.RECOVERY_START_ORDER[0]
        name='progress-'+role+'.json'
        progress=wire._bytes({'intent_sha256':self.intent_pin,'started':[role],
            'proof':{'windows_proof_sha256':'c'*64,'guest_proof_sha256':'d'*64}})
        p=self.stage/(self.stem+name)
        p.write_bytes(progress)
        for pins in ({},{name:wire._digest(progress)}):
            self.progress_pins=pins
            with self.assertRaises(TransitionBlocked):
                self.attest()
        p.unlink()
        self.progress_pins={}
        done=self.stage/(self.stem+'done.json')
        done.write_bytes(b'{}')
        with self.assertRaises(TransitionBlocked):
            self.attest()
        done.unlink()
        self.contracts[role]['started_at']='changed-since-stop'
        with self.assertRaises(TransitionBlocked):
            self.attest()
        self.assertFalse(resume.path(self.request).exists())

    def test_sameboot_resume_rejects_wrong_stop_hash_and_tampered_receipt(self):
        self.prepare_sameboot_request()
        request=self.request
        self.request=replace(request,args=request.args|{'postboot_stopped_receipt_sha256':'f'*64})
        with self.assertRaises(TransitionBlocked):
            self.attest()
        self.request=request
        self.attest()
        self.stopped_path.write_bytes(self.stopped_path.read_bytes()+b' ')
        with self.assertRaises(TransitionBlocked):
            self.load()

    def prepare_partial_request(self):
        self.prepare_sameboot_request()
        first=self.attest()
        self.failed_boundary='recovery_after:car-care-worker'
        with self.assertRaises(TransitionBlocked):
            w.start_initial_recovery(self.request,self.windows)
        stem=self.stem+'resume-'
        paths={p.name.removeprefix(stem):p for p in self.stage.iterdir() if p.name.startswith(stem)}
        self.lineage={'authorization_sha256':first['authorization_sha256'],
            'intent_sha256':wire._digest(paths.pop('intent.json').read_bytes()),
            'progress_sha256':{name:wire._digest(p.read_bytes()) for name,p in paths.items()}}
        self.lineage_original={p:p.read_bytes() for p in self.stage.iterdir()}
        self.lineage_original[resume.path(self.request)]=resume.path(self.request).read_bytes()
        self.request=replace(self.request,nonce='f'*64,sequence=3)
        self.failed_boundary=None
        self.commands.clear()
        self.challenges.clear()

    def attest_partial(self, *, lineage=None, instance='e'*64):
        return resume.attest(self.request,service_instance_sha256=instance,prior_intent_sha256=self.intent_pin,
                             prior_progress_sha256={},prior_resume=self.lineage if lineage is None else lineage)

    def test_sameboot_partial_restarts_checked_prefix_and_preserves_both_attempts(self):
        self.prepare_partial_request()
        self.assertEqual(set(self.lineage['progress_sha256']),{'progress-ha-broker.json','progress-book-memo.json'})
        self.assertEqual({role for role,row in self.contracts.items() if row['started_at']!='original-postboot-stop'},
                         {'ha-broker','book-memo','car-care-worker'})
        result=self.attest_partial()
        body,value,stem=resume.load(self.request,b'hold',self.hold,self.prepared,self.active,partial=True)
        self.assertEqual(value['prior_resume'],self.lineage)
        self.assertEqual(wire._digest(body),result['authorization_sha256'])
        self.assertEqual(stem,self.stem+'resume-partial-')
        self.assertEqual(w.start_initial_recovery(self.request,self.windows).status,'PASS')
        self.assertEqual(self.commands,[('start',v['container_id']) for v in self.containers.values()])
        record=json.loads((self.stage/(stem+'intent.json')).read_bytes())
        self.assertEqual(record['resume_authorization_sha256'],result['authorization_sha256'])
        self.assertTrue((self.stage/(stem+'done.json')).exists())
        self.assertTrue(all(path.read_bytes()==original for path,original in self.lineage_original.items()))
        for action in (self.attest_partial,lambda:w.start_initial_recovery(self.request,self.windows)):
            with self.assertRaises(TransitionBlocked):
                action()
        self.assertEqual(len(self.commands),8)

    def test_sameboot_partial_rejects_wrong_lineage_writer_or_unexpected_runtime_state(self):
        self.prepare_partial_request()
        for key in ('authorization_sha256','intent_sha256'):
            with self.assertRaises(TransitionBlocked):
                self.attest_partial(lineage=self.lineage|{key:'4'*64})
        with self.assertRaises(TransitionBlocked):
            self.attest_partial(instance='4'*64)
        role='portal-web'
        self.contracts[role]['started_at']='unexpected-start'
        with self.assertRaises(TransitionBlocked):
            self.attest_partial()
        self.contracts[role]['started_at']='original-postboot-stop'
        self.contracts['car-care-worker']['running']=True
        with self.assertRaises(TransitionBlocked):
            self.attest_partial()
        self.contracts['car-care-worker']['running']=False
        self.contracts['book-memo']['started_at']='original-postboot-stop'
        with self.assertRaises(TransitionBlocked):
            self.attest_partial()
        self.assertEqual(self.commands,[])

    def test_sameboot_partial_rechecks_previous_progress_and_blocks_predecessor_completion(self):
        self.prepare_partial_request()
        stem=self.stem+'resume-'
        done=self.stage/(stem+'done.json')
        done.write_bytes(b'{}')
        with self.assertRaises(TransitionBlocked):
            self.attest_partial()
        done.unlink()
        self.attest_partial()
        p=self.stage/(stem+'progress-book-memo.json')
        body,value,_=resume.load(self.request,b'hold',self.hold,self.prepared,self.active,partial=True)
        p.write_bytes(p.read_bytes()+b' ')
        with self.assertRaises(TransitionBlocked):
            resume.preserved(self.request,body,value,self.hold,self.active,b'hold')
        with self.assertRaises(TransitionBlocked):
            w.start_initial_recovery(self.request,self.windows)
        self.assertEqual(self.commands,[])

    def prepare_prestart_request(self):
        self.prepare_partial_request()
        partial=self.attest_partial()
        self.failed_boundary='recovery_before:ha-broker'
        with self.assertRaises(TransitionBlocked):
            w.start_initial_recovery(self.request,self.windows)
        self.assertEqual(self.commands,[])
        self.partial_stem=self.stem+'resume-partial-'
        self.partial_intent=self.stage/(self.partial_stem+'intent.json')
        self.partial_authorization=resume.path(self.request,partial=True)
        self.prior_partial={'authorization_sha256':partial['authorization_sha256'],
                            'intent_sha256':wire._digest(self.partial_intent.read_bytes())}
        self.lineage_original.update({self.partial_intent:self.partial_intent.read_bytes(),
                                      self.partial_authorization:self.partial_authorization.read_bytes()})
        self.request=replace(self.request,nonce='0'*64,sequence=4)
        self.failed_boundary=None
        self.challenges.clear()

    def attest_prestart(self, *, lineage=None):
        return resume.attest(self.request,service_instance_sha256='e'*64,prior_intent_sha256=self.intent_pin,
                             prior_progress_sha256={},prior_resume=self.lineage,
                             prior_partial=self.prior_partial if lineage is None else lineage)

    def test_sameboot_prestart_uses_unchanged_partial_baseline_and_preserves_all_evidence(self):
        self.prepare_prestart_request()
        result=self.attest_prestart()
        body,value,stem=resume.load(self.request,b'hold',self.hold,self.prepared,self.active,
                                    partial=True,prestart=True)
        self.assertEqual(value['prior_partial'],self.prior_partial)
        self.assertEqual(wire._digest(body),result['authorization_sha256'])
        self.assertEqual(stem,self.partial_stem+'prestart-')
        self.assertEqual(w.start_initial_recovery(self.request,self.windows).status,'PASS')
        self.assertEqual(self.commands,[('start',v['container_id']) for v in self.containers.values()])
        self.assertTrue(all(path.read_bytes()==original for path,original in self.lineage_original.items()))
        self.assertTrue((self.stage/(stem+'done.json')).exists())
        for action in (self.attest_prestart,lambda:w.start_initial_recovery(self.request,self.windows)):
            with self.assertRaises(TransitionBlocked):
                action()
        self.assertEqual(len(self.commands),8)

    def test_sameboot_prestart_rejects_partial_progress_or_completion(self):
        self.prepare_prestart_request()
        for suffix in ('progress-ha-broker.json','done.json','unknown.json'):
            path=self.stage/(self.partial_stem+suffix)
            path.write_bytes(b'{}')
            with self.assertRaises(TransitionBlocked):
                self.attest_prestart()
            path.unlink()
        self.assertFalse(resume.path(self.request,partial=True,prestart=True).exists())

    def test_sameboot_prestart_rejects_started_at_drift_even_with_no_progress(self):
        self.prepare_prestart_request()
        self.contracts['ha-broker']['started_at']='after-partial-interruption'
        with self.assertRaises(TransitionBlocked):
            self.attest_prestart()
        self.assertEqual(self.commands,[])

    def test_sameboot_prestart_rejects_unbound_or_changed_predecessor_records(self):
        self.prepare_prestart_request()
        for key in ('authorization_sha256','intent_sha256'):
            with self.assertRaises(TransitionBlocked):
                self.attest_prestart(lineage=self.prior_partial|{key:'4'*64})
        self.attest_prestart()
        body,value,_=resume.load(self.request,b'hold',self.hold,self.prepared,self.active,
                                 partial=True,prestart=True)
        self.partial_intent.write_bytes(self.partial_intent.read_bytes()+b' ')
        with self.assertRaises(TransitionBlocked):
            resume.preserved(self.request,body,value,self.hold,self.active,b'hold')
        with self.assertRaises(TransitionBlocked):
            w.start_initial_recovery(self.request,self.windows)
        self.assertEqual(self.commands,[])

    def prepare_continuation_request(self):
        self.prepare_prestart_request()
        authorization=self.attest_prestart()
        self.prestart_baseline=copy.deepcopy(self.contracts)
        self.failed_boundary='recovery_after:book-memo'
        with self.assertRaises(TransitionBlocked):
            w.start_initial_recovery(self.request,self.windows)
        self.prestart_stem=self.partial_stem+'prestart-'
        self.prestart_authorization=resume.path(self.request,partial=True,prestart=True)
        self.prestart_intent=self.stage/(self.prestart_stem+'intent.json')
        self.prestart_progress=self.stage/(self.prestart_stem+'progress-ha-broker.json')
        self.prior_prestart={'authorization_sha256':authorization['authorization_sha256'],
                             'intent_sha256':wire._digest(self.prestart_intent.read_bytes()),
                             'progress_sha256':{'progress-ha-broker.json':wire._digest(self.prestart_progress.read_bytes())}}
        self.lineage_original.update({path:path.read_bytes() for path in
            (self.prestart_authorization,self.prestart_intent,self.prestart_progress)})
        self.request=replace(self.request,nonce='a'*64,sequence=5)
        self.failed_boundary=None
        self.commands.clear()
        self.challenges.clear()

    def attest_continuation(self, *, lineage=None):
        return resume.attest(self.request,service_instance_sha256='e'*64,prior_intent_sha256=self.intent_pin,
                             prior_progress_sha256={},prior_resume=self.lineage,prior_partial=self.prior_partial,
                             prior_prestart=self.prior_prestart if lineage is None else lineage)

    def test_sameboot_continuation_resumes_progress_relative_to_latest_baseline(self):
        self.prepare_continuation_request()
        result=self.attest_continuation()
        body,value,stem=resume.load(self.request,b'hold',self.hold,self.prepared,self.active,
                                    partial=True,prestart=True,continuation=True)
        self.assertEqual(value['prior_prestart'],self.prior_prestart)
        self.assertEqual(wire._digest(body),result['authorization_sha256'])
        self.assertEqual(stem,self.prestart_stem+'continuation-')
        self.assertEqual(w.start_initial_recovery(self.request,self.windows).status,'PASS')
        self.assertEqual(self.commands,[('start',v['container_id']) for v in self.containers.values()])
        self.assertTrue(all(path.read_bytes()==original for path,original in self.lineage_original.items()))
        self.assertTrue((self.stage/(stem+'done.json')).exists())
        for action in (self.attest_continuation,lambda:w.start_initial_recovery(self.request,self.windows)):
            with self.assertRaises(TransitionBlocked):
                action()
        self.assertEqual(len(self.commands),8)

    def test_sameboot_continuation_rejects_missing_progress_or_wrong_started_at(self):
        self.prepare_continuation_request()
        with self.assertRaises(TransitionBlocked):
            self.attest_continuation(lineage=self.prior_prestart|{'progress_sha256':{}})
        for role, timestamp in (('portal-web','unexpected'),('ha-broker',self.prestart_baseline['ha-broker']['started_at'])):
            before=self.contracts[role]['started_at']
            self.contracts[role]['started_at']=timestamp
            with self.assertRaises(TransitionBlocked):
                self.attest_continuation()
            self.contracts[role]['started_at']=before
        self.contracts['ha-broker']['running']=True
        with self.assertRaises(TransitionBlocked):
            self.attest_continuation()
        self.assertEqual(self.commands,[])

    def test_sameboot_continuation_rejects_wrong_pins_or_predecessor_completion(self):
        self.prepare_continuation_request()
        for key in ('authorization_sha256','intent_sha256'):
            with self.assertRaises(TransitionBlocked):
                self.attest_continuation(lineage=self.prior_prestart|{key:'4'*64})
        done=self.stage/(self.prestart_stem+'done.json')
        done.write_bytes(b'{}')
        with self.assertRaises(TransitionBlocked):
            self.attest_continuation()
        done.unlink()
        self.attest_continuation()
        body,value,_=resume.load(self.request,b'hold',self.hold,self.prepared,self.active,
                                 partial=True,prestart=True,continuation=True)
        self.prestart_progress.write_bytes(self.prestart_progress.read_bytes()+b' ')
        with self.assertRaises(TransitionBlocked):
            resume.preserved(self.request,body,value,self.hold,self.active,b'hold')
        with self.assertRaises(TransitionBlocked):
            w.start_initial_recovery(self.request,self.windows)
        self.assertEqual(self.commands,[])

    def prepare_numbered_request(self):
        self.prepare_continuation_request()
        authorization = self.attest_continuation()
        self.failed_boundary = 'recovery_after:book-memo'
        with self.assertRaises(TransitionBlocked):
            w.start_initial_recovery(self.request, self.windows)
        self.continuation_stem = self.prestart_stem + 'continuation-'
        self.continuation_intent = self.stage / (self.continuation_stem + 'intent.json')
        progress = self.stage / (self.continuation_stem + 'progress-ha-broker.json')
        self.prior_attempt = {
            'authorization_sha256': authorization['authorization_sha256'],
            'intent_sha256': wire._digest(self.continuation_intent.read_bytes()),
            'progress_sha256': {'progress-ha-broker.json': wire._digest(progress.read_bytes())},
        }
        files = (resume.path(self.request, partial=True, prestart=True, continuation=True),
                 self.continuation_intent, progress)
        self.lineage_original.update({path:path.read_bytes() for path in files})
        self.previous_request = self.request
        self.request = replace(self.request, nonce='b'*64, sequence=6)
        self.failed_boundary = None
        self.commands.clear()

    def attest_numbered(self, *, lineage=None):
        return resume.attest(
            self.request, service_instance_sha256='e'*64, prior_intent_sha256=self.intent_pin,
            prior_progress_sha256={}, prior_resume=self.lineage, prior_partial=self.prior_partial,
            prior_prestart=self.prior_prestart, prior_attempt=self.prior_attempt if lineage is None else lineage)

    def test_numbered_attempt_resumes_exact_latest_progress_and_keeps_all_old_records(self):
        self.prepare_numbered_request()
        result = self.attest_numbered()
        stem = self.continuation_stem + 'attempt-0001-'
        body, value, actual_stem = resume.load(self.request, b'hold', self.hold, self.prepared, self.active,
            partial=True, prestart=True, continuation=True, numbered_attempt=1)
        self.assertEqual((value['prior_attempt'], value['numbered_attempt'], actual_stem),
                         (self.prior_attempt, 1, stem))
        self.assertEqual(wire._digest(body), result['authorization_sha256'])
        with self.assertRaises(TransitionBlocked):
            w.start_initial_recovery(self.previous_request, self.windows)
        self.assertEqual(w.start_initial_recovery(self.request, self.windows).status, 'PASS')
        self.assertEqual(self.commands, [('start', v['container_id']) for v in self.containers.values()])
        self.assertTrue((self.stage / (stem + 'done.json')).is_file())
        self.assertTrue(all(path.read_bytes() == raw for path, raw in self.lineage_original.items()))

    def test_numbered_attempt_reuses_no_slot_and_verifies_transitive_history(self):
        self.prepare_numbered_request()
        result = self.attest_numbered()
        self.failed_boundary = 'recovery_before:ha-broker'
        with self.assertRaises(TransitionBlocked):
            w.start_initial_recovery(self.request, self.windows)
        original_intent = self.stage / (self.continuation_stem + 'attempt-0001-intent.json')
        prior = {'authorization_sha256': result['authorization_sha256'],
                 'intent_sha256': wire._digest(original_intent.read_bytes()), 'progress_sha256': {}}
        self.request = replace(self.request, nonce='c'*64, sequence=7)
        self.failed_boundary = None
        with patch.object(resume, 'MAX_NUMBERED_ATTEMPTS', 1):
            with self.assertRaises(TransitionBlocked):
                self.attest_numbered(lineage=prior)
        with self.assertRaises(TransitionBlocked):
            self.attest_numbered()  # The legacy C50 attempt is no longer the head.
        self.attest_numbered(lineage=prior)
        self.assertEqual(resume.latest_attempt(self.request), 2)
        self.continuation_intent.write_bytes(self.continuation_intent.read_bytes() + b' ')
        with self.assertRaises(TransitionBlocked):
            w.start_initial_recovery(self.request, self.windows)
        self.assertEqual(self.commands, [])

    def test_numbered_attempt_rejects_unknown_slots_and_out_of_prefix_drift(self):
        self.prepare_numbered_request()
        orphan = self.stage / (self.continuation_stem + 'attempt-0002-intent.json')
        orphan.write_bytes(b'{}')
        with self.assertRaises(TransitionBlocked):
            self.attest_numbered()
        orphan.unlink()
        skipped = resume.path(self.request, partial=True, prestart=True, continuation=True, numbered_attempt=2)
        skipped.write_bytes(b'{}')
        with self.assertRaises(TransitionBlocked):
            self.attest_numbered()
        skipped.unlink()
        self.contracts['portal-web']['started_at'] = 'outside-verified-prefix'
        with self.assertRaises(TransitionBlocked):
            self.attest_numbered()
        self.assertEqual(self.commands, [])

    def test_partial_stop_snapshot_allows_changed_start_times_under_real_inventory_contract(self):
        from ha_control import native_guest_postboot as postboot
        self.prepare_sameboot_request()
        binding=self.request.binding
        objects={pin['container_id']:{
            'Name':role,'Image':pin['image_id'],'Config':{'Labels':{
                'com.docker.compose.service':role,w.runtime.ATTEMPT_LABEL:binding.attempt_id}},
            'HostConfig':{'RestartPolicy':{'Name':'no'},'AutoRemove':False},'RestartCount':0,
            'Mounts':[],'State':{'Running':False,'Paused':False,'Restarting':False,'OOMKilled':False,
                'Dead':False,'Status':'exited','Pid':0,'ExitCode':143,'StartedAt':'1','FinishedAt':'3'},
        } for role,pin in self.containers.items()}
        protected=w.runtime.inventory_sha256({})
        with patch.object(w,'_initial_recovery_inventory',REAL_INVENTORY),patch.object(w.runtime,'_inventory',return_value=objects):
            original,_=REAL_INVENTORY(binding,self.containers,protected,None)
            postboot_record={'container_contracts':original,'protected_inventory_sha256':protected}
            for role in w.RECOVERY_START_ORDER[:3]:
                objects[self.containers[role]['container_id']]['State']['StartedAt']='2'
            with patch.object(postboot,'observe',return_value=postboot_record):
                current,current_protected=resume.stopped(self.request,b'hold',self.hold,self.prepared,self.active,prior_baseline=original)
            self.assertEqual(current_protected,protected)
            self.assertTrue(all(row['running'] is False for row in current.values()))
            self.assertEqual({role for role,row in current.items() if row['started_at']!=original[role]['started_at']},
                             set(w.RECOVERY_START_ORDER[:3]))


    def prepare_successor_carryover(self, *, numbered_attempts=1):
        from ha_control import native_guest_postboot as postboot
        original_prepare = self.prepare_sameboot_request
        def prepare():
            original_prepare()
            self.old_plan = {'installation_id': self.request.args['installation_id'],
                'initial_recovery_postboot': self.request.args['postboot'],
                'initial_cutover_recovery': {'attempt_id': self.request.args['recovery_attempt_id']},
                'initial_cutover_handoff': {'attempt_id': self.request.binding.attempt_id,
                                          'legacy_plan_sha256': self.request.binding.plan_sha256}}
            self.old_plan['connection_settings'] = {'private_endpoint': 'sensitive-marker-that-must-not-be-copied'}
            self.old_plan_raw = wire._bytes(self.old_plan)
            self.request = replace(self.request, args=self.request.args | {'postboot_plan_sha256': wire._digest(self.old_plan_raw)})
            intent = json.loads(self.intent)
            intent['args'] = self.request.args
            self.intent = wire._bytes(intent)
            self.intent_pin = wire._digest(self.intent)
            self.intent_path.write_bytes(self.intent)
        self.prepare_sameboot_request = prepare
        self.prepare_numbered_request()
        previous = self.prior_attempt
        for index in range(1, numbered_attempts + 1):
            auth = self.attest_numbered(lineage=previous)
            self.failed_boundary = ('recovery_before:ha-broker' if index == resume.MAX_NUMBERED_ATTEMPTS
                                    else 'recovery_after:book-memo')
            with self.assertRaises(TransitionBlocked):
                w.start_initial_recovery(self.request, self.windows)
            head = self.continuation_stem + f'attempt-{index:04d}-'
            progress = self.stage / (head + 'progress-ha-broker.json')
            previous = {'authorization_sha256': auth['authorization_sha256'],
                'intent_sha256': wire._digest((self.stage / (head + 'intent.json')).read_bytes()),
                'progress_sha256': {'progress-ha-broker.json': wire._digest(progress.read_bytes())}
                if progress.is_file() else {}}
            if index < numbered_attempts:
                self.request = replace(self.request, sequence=self.request.sequence + 1, nonce=f'{index:064x}')
        self.successor_history = previous
        self.old_request = self.request
        successor = self.request.args['postboot'] | {'attempt_id': 'd'*64,
            'predecessor_plan_sha256': wire._digest(self.old_plan_raw)}
        self.request = replace(self.request, nonce='f'*64, sequence=self.request.sequence + 1,
            args=self.request.args | {'postboot': successor, 'postboot_plan_sha256': 'e'*64})
        self.failed_boundary = None
        self.commands.clear()
        self.all_original = {p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.carryover = postboot.attest_carryover(self.request, predecessor_plan_raw=self.old_plan_raw,
            predecessor_request=self.old_request, history=self.successor_history)
        self.request = replace(self.request, args=self.request.args | {
            'postboot': successor | {'carryover_receipt_sha256': self.carryover.receipt_sha256}})
        return postboot

    def attest_successor(self, *, prior_resume=None, service_instance='1'*64):
        return resume.attest(self.request, service_instance_sha256=service_instance,
            prior_intent_sha256=self.successor_history['intent_sha256'],
            prior_progress_sha256=self.successor_history['progress_sha256'], prior_resume=prior_resume)

    def prepare_interrupted_successor(self):
        self.prepare_successor_carryover()
        authorization = self.attest_successor()
        self.failed_boundary = 'recovery_after:crawler-worker'
        with self.assertRaises(TransitionBlocked):
            w.start_initial_recovery(self.request, self.windows)
        self.failed_boundary = None
        stem = resume.prefix(self.request) + 'resume-'
        journal = {p.name.removeprefix(stem): wire._digest(p.read_bytes())
                   for p in self.stage.iterdir() if p.name.startswith(stem)}
        link = {'authorization_sha256': authorization['authorization_sha256'],
                'intent_sha256': journal.pop('intent.json'), 'progress_sha256': journal}
        self.assertEqual(len(journal), 3)
        self.assertFalse(any(row['running'] for row in self.contracts.values()))
        self.request = replace(self.request, nonce='0'*64, sequence=self.request.sequence+1)
        self.commands.clear()
        return link

    def test_interrupted_carryover_resumes_with_exact_lineage(self):
        link = self.prepare_interrupted_successor()
        before = {p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = self.attest_successor(prior_resume=link)
        self.assertEqual(w.start_initial_recovery(self.request,self.windows).status, 'PASS')
        self.assertEqual(self.commands,[('start',v['container_id']) for v in self.containers.values()])
        self.assertTrue(all(path.read_bytes()==raw for path,raw in before.items()))
        stem = resume.prefix(self.request)+'resume-partial-'
        new = resume.decode((self.stage/(stem+'intent.json')).read_bytes())
        self.assertEqual(new['resume_authorization_sha256'],result['authorization_sha256'])
        with self.assertRaises(TransitionBlocked):
            self.attest_successor(prior_resume=link)

    def test_completed_carryover_history_verifies_done_and_predecessors(self):
        partial = self.prepare_interrupted_successor()
        authorization = self.attest_successor(prior_resume=partial)
        self.assertEqual(w.start_initial_recovery(self.request,self.windows).status, 'PASS')
        stem = resume.prefix(self.request)+'resume-partial-'
        files = {p.name.removeprefix(stem):p for p in self.stage.iterdir() if p.name.startswith(stem)}
        artifact = {name:wire._digest(path.read_bytes()) for name,path in files.items()}
        link = {'authorization_sha256':authorization['authorization_sha256'],
                'intent_sha256':artifact.pop('intent.json'),
                'done_sha256':artifact.pop('done.json'),'progress_sha256':artifact}

        def verify(evidence):
            return resume._latest_history(self.request,b'hold',self.hold,self.active,evidence)

        self.assertEqual(verify(link)['prior_resume'],partial)
        with self.assertRaises(TransitionBlocked):
            verify({key:value for key,value in link.items() if key!='done_sha256'})
        with self.assertRaises(TransitionBlocked):
            verify(link|{'done_sha256':'a'*64})
        self.assertEqual(len(link['progress_sha256']),8)
        done=files['done.json']
        raw=done.read_bytes()
        record=resume.decode(raw)
        record['started']=record['started'][:-1]
        changed=wire._bytes(record)
        done.write_bytes(changed)
        with self.assertRaises(TransitionBlocked):
            verify(link|{'done_sha256':wire._digest(changed)})
        done.write_bytes(raw)
        earlier=self.stage/(resume.prefix(self.request)+'resume-intent.json')
        earlier.write_bytes(earlier.read_bytes()+b' ')
        with self.assertRaises(TransitionBlocked):
            verify(link)

    def test_completed_carryover_uses_a_new_record_only_after_apps_are_stopped(self):
        postboot = self.prepare_successor_carryover()
        current = self.request
        plan = {'installation_id':current.args['installation_id'],
                'initial_recovery_postboot':current.args['postboot'],
                'initial_cutover_recovery':{'attempt_id':current.args['recovery_attempt_id']},
                'initial_cutover_handoff':{'attempt_id':current.binding.attempt_id,
                                           'legacy_plan_sha256':current.binding.plan_sha256}}
        plan_raw=wire._bytes(plan)
        plan_pin=wire._digest(plan_raw)
        self.request=current=replace(current,args=current.args|{'postboot_plan_sha256':plan_pin})
        auth=self.attest_successor()
        self.assertEqual(w.start_initial_recovery(current,self.windows).status,'PASS')
        stem=resume.prefix(current)+'resume-'
        files={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        records={p.name.removeprefix(stem):wire._digest(raw) for p,raw in files.items() if p.name.startswith(stem)}
        link={'authorization_sha256':auth['authorization_sha256'],
              'intent_sha256':records.pop('intent.json'),
              'done_sha256':records.pop('done.json'),'progress_sha256':records}
        newer=replace(current,nonce='3'*64,args=current.args|{
            'postboot':current.args['postboot']|{
                'attempt_id':'c'*64,'predecessor_plan_sha256':plan_pin,
                'carryover_receipt_sha256':postboot.DRAFT_PIN,
                'predecessor_seal_sha256':postboot.DRAFT_PIN},
            'postboot_plan_sha256':postboot.DRAFT_PIN})

        def issue():
            return postboot.attest_carryover(newer,predecessor_plan_raw=plan_raw,
                    predecessor_request=current,history=link)

        with self.assertRaises(TransitionBlocked):
            issue()
        self.assertFalse(postboot.carryover_path(newer,'c'*64).exists())
        for container in self.contracts.values():
            container['running']=False
        receipt=issue()
        admitted=replace(newer,args=newer.args|{'postboot':newer.args['postboot']|{
            'carryover_receipt_sha256':receipt.receipt_sha256}})
        self.assertEqual(postboot.observe_carryover(admitted,'c'*64,receipt.receipt_sha256)['history'],link)
        self.assertTrue(all(path.read_bytes()==raw for path,raw in files.items()))

    def test_interrupted_carryover_rejects_wrong_instance_tampering_and_drift(self):
        link = self.prepare_interrupted_successor()
        with self.assertRaises(TransitionBlocked):
            self.attest_successor(prior_resume=link,service_instance='2'*64)
        with self.assertRaises(TransitionBlocked):
            self.attest_successor(prior_resume=link|{'intent_sha256':'9'*64})
        unexpected = self.stage/(resume.prefix(self.request)+'intent.json')
        unexpected.write_bytes(b'{}')
        with self.assertRaises(TransitionBlocked):
            self.attest_successor(prior_resume=link)
        unexpected.unlink()
        role = self.contracts['youtube-memo']
        started = role['started_at']
        role['started_at'] = 'unexpected'
        with self.assertRaises(TransitionBlocked):
            self.attest_successor(prior_resume=link)
        role['started_at'] = started
        self.attest_successor(prior_resume=link)
        role['started_at'] = 'after-attest'
        with self.assertRaises(TransitionBlocked):
            w.start_initial_recovery(self.request,self.windows)
        self.assertEqual(self.commands,[])

    def test_carryover_partial_baseline_reads_real_stopped_inventory_with_drift(self):
        self.prepare_successor_carryover()
        objects = {pin['container_id']:{
            'Name':role,'Image':pin['image_id'],'Config':{'Labels':{
                'com.docker.compose.service':role,w.runtime.ATTEMPT_LABEL:self.request.binding.attempt_id}},
            'HostConfig':{'RestartPolicy':{'Name':'no'},'AutoRemove':False},'RestartCount':0,
            'Mounts':[],'State':{'Running':False,'Paused':False,'Restarting':False,'OOMKilled':False,
                'Dead':False,'Status':'exited','Pid':0,'ExitCode':143,'StartedAt':'1','FinishedAt':'3'},
        } for role,pin in self.containers.items()}
        protected = w.runtime.inventory_sha256({})
        with patch.object(w,'_initial_recovery_inventory',REAL_INVENTORY), \
             patch.object(w.restore,'_observer_inventory',return_value=objects):
            baseline,_ = REAL_INVENTORY(self.request.binding,self.containers,protected,None,inventory=lambda:objects)
            for role in w.RECOVERY_START_ORDER[:4]:
                objects[self.containers[role]['container_id']]['State']['StartedAt'] = '2'
            stem = resume.prefix(self.request)
            lineage = {'progress_sha256': {role:'a'*64 for role in w.RECOVERY_START_ORDER[:3]}}
            carry = {'container_contracts':baseline,'protected_inventory_sha256':protected}
            with patch.object(resume,'_carryover_lineage',return_value=carry):
                current,actual,next_stem = resume._authorization_baseline(self.request,b'hold',self.hold,
                    self.prepared,self.active,{'carryover_receipt_sha256':'a'*64,'prior_resume':lineage},self.stage,stem)
            self.assertEqual(actual,protected)
            self.assertEqual(next_stem,stem+'resume-partial-')
            self.assertEqual({role for role,row in current.items() if row['started_at'] != baseline[role]['started_at']},
                             set(w.RECOVERY_START_ORDER[:4]))

    def test_successor_carryover_checks_exact_history_and_new_writer_authorization(self):
        postboot = self.prepare_successor_carryover()
        # Strict original observation still fails after attempted starts.
        with self.assertRaises(TransitionBlocked):
            postboot.observe(self.request, self.request.args['postboot']['stopped_attempt_id'],
                             self.request.args['postboot_stopped_receipt_sha256'])
        observed = postboot.observe_carryover(self.request, 'd'*64, self.carryover.receipt_sha256)
        self.assertEqual(observed['history'], self.successor_history)
        self.assertNotIn('predecessor_plan_json', observed)
        self.assertEqual(observed['predecessor_postboot'], self.old_plan['initial_recovery_postboot'])
        self.assertNotIn(b'sensitive-marker-that-must-not-be-copied',
            postboot.carryover_path(self.request, 'd'*64).read_bytes())
        with self.assertRaises(TransitionBlocked):
            w.start_initial_recovery(self.request, self.windows)
        self.assertEqual(self.commands, [])
        self.attest_successor()
        self.assertEqual(w.start_initial_recovery(self.request, self.windows).status, 'PASS')
        self.assertEqual(self.commands, [('start', value['container_id']) for value in self.containers.values()])
        self.assertTrue(all(path.read_bytes() == raw for path,raw in self.all_original.items()))
        with self.assertRaises(TransitionBlocked):
            w.start_initial_recovery(self.request, self.windows)

    def test_typed_completion_matches_both_observations_to_full_authorization(self):
        from ha_control import native_guest_app_operation as app_operation

        boot = self.root / 'boot-id'
        boot.write_bytes(b'current guest boot')
        self.request = replace(self.request, binding=replace(
            self.request.binding, boot_a_sha256=wire._digest(boot.read_bytes())))
        self.prepare_successor_carryover()
        persisted = self.attest_successor()
        projected = {name: persisted[name] for name in app_operation.AUTH_FIELDS}
        record, full, _ = self.load()
        self.assertNotEqual(full, projected)
        self.assertEqual(wire._digest(record), projected['authorization_sha256'])
        done = w.start_initial_recovery(self.request, self.windows)
        self.assertEqual(done.status, 'PASS')

        objects = {
            pin['container_id']: {'Id': pin['container_id'], 'Image': pin['image_id'],
                                  'State': {'Pid': index, 'StartedAt': self.contracts[role]['started_at']}}
            for index, (role, pin) in enumerate(self.containers.items(), 1)
        }
        inputs = {'instance': persisted['service_instance_sha256'], 'authorization': projected,
                  'prior_resume': None, 'expected': done.receipt_sha256, 'budget': 10}
        frame = app_operation.encode_request(app_operation.COMPLETE, self.request, inputs)
        operation = app_operation.decode_request(frame)
        files_before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        commands_before = self.commands.copy()
        with patch.object(w.os, 'geteuid', return_value=0), patch.object(w.runtime, 'BOOT_ID', boot), \
             patch.object(w, '_guest_mutation_descriptor', return_value=ExitStack()), \
             patch.object(w.restore, '_observer_inventory', return_value=objects), \
             patch.object(w.activation, '_probe', return_value='a' * 64) as probe, \
             patch.object(w.live, 'observe_postboot_a_lease', wraps=w.live.observe_postboot_a_lease) as lease, \
             patch.object(resume, '_latest_history', wraps=resume._latest_history) as history:
            response = app_operation.reply(operation)
            result = app_operation.decode_reply(response, app_operation.COMPLETE, frame)
        self.assertEqual(result['status'], 'STARTED_VERIFIED')
        self.assertEqual(result['receipt_sha256'], done.receipt_sha256)
        self.assertEqual(result['authorization_sha256'], projected['authorization_sha256'])
        self.assertEqual(result['container_count'], 8)
        self.assertIs(result['mutations'], False)
        self.assertEqual(probe.call_count, 7)
        self.assertEqual(lease.call_count, 2)
        self.assertEqual(sum(call.args[0] == self.request for call in history.call_args_list), 2)
        self.assertEqual(self.commands, commands_before)
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}, files_before)

    def test_successor_carryover_checks_only_one_inventory_and_preserves_without_rescan(self):
        postboot = self.prepare_successor_carryover()
        with patch.object(postboot, 'current', wraps=postboot.current) as current:
            postboot.observe_carryover(self.request, 'd'*64, self.carryover.receipt_sha256)
            self.assertEqual(current.call_count, 1)
        self.attest_successor()
        body,value,_ = self.load()
        with patch.object(postboot, 'current', wraps=postboot.current) as current:
            resume.preserved(self.request, body, value, self.hold, self.active, b'hold')
            self.assertEqual(current.call_count, 0)

    def test_successor_carryover_rejects_reused_new_attempt_namespace(self):
        self.prepare_successor_carryover()
        path = self.stage/(resume.prefix(self.request)+'intent.json')
        path.write_bytes(b'{}')
        with self.assertRaises(TransitionBlocked):self.attest_successor()
        self.assertEqual(self.commands, [])

    def test_carryover_rejects_modified_raw_plan_before_creating_receipt(self):
        postboot = self.prepare_successor_carryover()
        new = replace(self.request, args=self.request.args | {'postboot': self.request.args['postboot'] | {'attempt_id': 'c'*64}})
        with self.assertRaises(TransitionBlocked):
            postboot.attest_carryover(new, predecessor_plan_raw=self.old_plan_raw+b' ',
                predecessor_request=self.old_request, history=self.successor_history)
        self.assertFalse(postboot.carryover_path(new, 'c'*64).exists())

    def carryover_cli_inputs(self, *, numbered_attempts=1):
        import io
        postboot = self.prepare_successor_carryover(numbered_attempts=numbered_attempts)
        target = postboot.carryover_path(self.request, 'd'*64)
        target.unlink()
        pending = replace(self.request, args=self.request.args | {'postboot_plan_sha256': postboot.DRAFT_PIN,
            'postboot': self.request.args['postboot'] | {'carryover_receipt_sha256': postboot.DRAFT_PIN,
                'predecessor_seal_sha256': postboot.DRAFT_PIN}})
        values = {'request': wire._bytes(w._scope(pending) | {'args': pending.args}),
            'predecessor-request': wire._bytes(w._scope(self.old_request) | {'args': self.old_request.args}),
            'predecessor-plan': self.old_plan_raw, 'history': wire._bytes(self.successor_history)}
        argv = []
        for name, body in values.items():
            path = self.root / ('cli-' + name + '.json')
            path.write_bytes(body); path.chmod(0o400)
            argv += ['--' + name, str(path), '--' + name + '-sha256', wire._digest(body)]
        return postboot, target, argv, io.StringIO(), io.StringIO()

    def test_carryover_root_cli_authenticates_exact_files_and_preserves_records(self):
        postboot, target, argv, stdout, stderr = self.carryover_cli_inputs(numbered_attempts=8)
        with patch.object(postboot.os, 'geteuid', return_value=0), patch('sys.stdout', stdout), patch('sys.stderr', stderr):
            self.assertEqual(postboot.main(argv), 0)
        receipt = json.loads(stdout.getvalue())
        self.assertEqual(receipt, {'status': 'PASS', 'receipt_sha256': wire._digest(target.read_bytes())})
        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(self.commands, [])
        self.assertEqual(resume.latest_attempt(self.old_request), 8)
        self.assertEqual(self.successor_history['progress_sha256'], {})
        self.assertTrue(all(path.read_bytes() == raw for path, raw in self.all_original.items()))
        self.assertNotIn(b'sensitive-marker', target.read_bytes())
        original = target.read_bytes()
        with patch.object(postboot.os, 'geteuid', return_value=0), patch('sys.stdout', stdout), patch('sys.stderr', stderr):
            self.assertEqual(postboot.main(argv), 1)
        self.assertEqual(target.read_bytes(), original)

    def test_carryover_cli_rejects_nonroot_before_read_and_bad_pin_without_receipt(self):
        postboot, target, argv, stdout, stderr = self.carryover_cli_inputs()
        with patch.object(postboot.os, 'geteuid', return_value=1000), patch.object(w.runtime, '_read') as reader, patch('sys.stderr', stderr):
            self.assertEqual(postboot.main(argv), 1)
            reader.assert_not_called()
        argv[3] = '0'*64
        with patch.object(postboot.os, 'geteuid', return_value=0), patch('sys.stdout', stdout), patch('sys.stderr', stderr):
            self.assertEqual(postboot.main(argv), 1)
        self.assertFalse(target.exists())
        self.assertEqual(stdout.getvalue(), '')
        self.assertNotIn('sensitive-marker', stderr.getvalue())

    def test_carryover_cli_rejects_appended_duplex_frame(self):
        postboot, target, argv, stdout, stderr = self.carryover_cli_inputs()
        path = Path(argv[1]); body = path.read_bytes() + b'{}\n'
        path.chmod(0o600); path.write_bytes(body); path.chmod(0o400)
        argv[3] = wire._digest(body)
        with patch.object(postboot.os, 'geteuid', return_value=0), patch('sys.stdout', stdout), patch('sys.stderr', stderr):
            self.assertEqual(postboot.main(argv), 1)
        self.assertFalse(target.exists())
        self.assertEqual(self.commands, [])

    def test_carryover_cli_reader_requires_immutable_root_file_and_digest(self):
        from ha_control import native_guest_postboot as postboot
        with patch.object(w.runtime, '_read', return_value=b'{}') as reader:
            self.assertEqual(postboot._carryover_input('/trusted/input.json', wire._digest(b'{}'), limit=100), b'{}')
            reader.assert_called_once_with(Path('/trusted/input.json'), 0, {0o400}, limit=100)
            with self.assertRaises(TransitionBlocked):
                postboot._carryover_input('/trusted/input.json', '0'*64, limit=100)

    def test_carryover_cli_full_version_transition_draft_and_final_admission(self):
        postboot, target, argv, stdout, stderr = self.carryover_cli_inputs()
        draft_path = Path(argv[1]); draft = json.loads(draft_path.read_bytes())
        transition = {'previous_service_image_sha256': '1'*64,
            'successor_service_image_sha256': '2'*64, 'successor_service_image_path_sha256': '3'*64}
        draft['args']['postboot'].update(transition)
        body = wire._bytes(draft)
        draft_path.chmod(0o600); draft_path.write_bytes(body); draft_path.chmod(0o400)
        argv[3] = wire._digest(body)
        with patch.object(postboot.os, 'geteuid', return_value=0), patch('sys.stdout', stdout), patch('sys.stderr', stderr):
            self.assertEqual(postboot.main(argv), 0)
        record = json.loads(target.read_bytes())
        self.assertTrue(all(record['postboot'][key] == value for key, value in transition.items()))
        self.assertNotIn('predecessor_seal_sha256', record['postboot'])
        self.assertNotIn('carryover_receipt_sha256', record['postboot'])
        # Draft pins cannot be used as admission. Finalize receipt, seal and
        # plan pins before issuing a new service-instance authorization.
        draft_request = postboot._carryover_request(body)
        with self.assertRaises(TransitionBlocked):
            postboot.observe_carryover(draft_request, 'd'*64, postboot.DRAFT_PIN)
        pin = wire._digest(target.read_bytes())
        self.request = replace(self.request, args=self.request.args | {'postboot':
            self.request.args['postboot'] | transition | {'carryover_receipt_sha256': pin}})
        with self.assertRaises(TransitionBlocked):
            w.start_initial_recovery(self.request, self.windows)
        self.assertEqual(self.commands, [])
        self.attest_successor()
        self.assertEqual(w.start_initial_recovery(self.request, self.windows).status, 'PASS')

    def test_carryover_rejects_unbound_predecessor_nonce(self):
        postboot = self.prepare_successor_carryover()
        target = postboot.carryover_path(self.request, 'd'*64)
        original = target.read_bytes()
        # A different new attempt cannot authenticate a made-up old request.
        new = replace(self.request, args=self.request.args | {'postboot': self.request.args['postboot'] | {'attempt_id': 'c'*64}})
        with self.assertRaises(TransitionBlocked):
            postboot.attest_carryover(new, predecessor_plan_raw=self.old_plan_raw,
                predecessor_request=replace(self.old_request, nonce='0'*64), history=self.successor_history)
        self.assertEqual(target.read_bytes(), original)

    def test_successor_carryover_rejects_live_drift_and_changed_history(self):
        postboot = self.prepare_successor_carryover()
        self.contracts['portal-web']['started_at'] = 'unrecorded-start'
        with self.assertRaises(TransitionBlocked):
            postboot.observe_carryover(self.request, 'd'*64, self.carryover.receipt_sha256)
        self.contracts['portal-web']['started_at'] = 'original-postboot-stop'
        self.attest_successor()
        old_intent = self.stage/(self.continuation_stem+'attempt-0001-intent.json')
        old_intent.write_bytes(old_intent.read_bytes()+b' ')
        with self.assertRaises(TransitionBlocked):
            w.start_initial_recovery(self.request, self.windows)
        self.assertEqual(self.commands, [])

class SuccessorAppWiringTests(unittest.TestCase):
    def fixture(self):
        from tests.test_ha_postboot import SamebootPostbootSealTests
        from ha_control import initial_app_resume_wiring as app
        from ha_control.native_windows_activation import WindowsActivationLease
        from ha_control.native_guest_ssh import GuestSshDispatcher
        plan, files = SamebootPostbootSealTests().transition_fixture()
        # The real predecessor is revision 14. A scalar 14 supplied by a caller
        # cannot replace this exact complete terminal and its digest.
        terminal = json.loads(files['postboot-predecessor-state.json'])
        terminal['revision'] = 14
        raw = wire._bytes(terminal)
        postboot = plan['initial_recovery_postboot']
        postboot['predecessor_terminal_sha256'] = wire._digest(raw)
        binding = wire.Binding(plan['initial_cutover_handoff']['legacy_plan_sha256'],
                               plan['initial_cutover_handoff']['attempt_id'], '3'*64, '4'*64, '5'*64)
        lease = object.__new__(WindowsActivationLease)
        lease.custody = SimpleNamespace(binding=binding)
        lease.service_instance_sha256 = '9'*64
        dispatcher = object.__new__(GuestSshDispatcher)
        dispatcher.binding = binding
        launcher = b'fixture-launcher'
        dispatcher.launcher_sha256 = wire._digest(launcher)
        inputs = app.PairedProofInputs(r'C:\fixture\runtime', r'C:\fixture\plan.json', wire._digest(wire._bytes(plan)),
                                       r'C:\fixture\terminal.json', wire._digest(raw), 'a'*64, plan['source_commit'])
        stage = app.SuccessorAppStage(lease=lease, dispatcher=dispatcher, powershell=lambda *a: None,
            proof_inputs=inputs, plan=plan, predecessor_terminal=raw, launcher=launcher,
            manifest_sha256='b'*64, prior_intent_sha256='c'*64,
            prior_progress_sha256={'progress-ha-broker.json': 'd'*64})
        args = {key: 'e'*64 for key in wire.INITIAL_RECOVERY_ARGS}
        args.update(recovery_attempt_id=plan['initial_cutover_recovery']['attempt_id'],
                    installation_id=plan['installation_id'], hold_sha256=plan['windows_recovery_observers']['hold_sha256'],
                    postboot=postboot, postboot_plan_sha256=inputs.plan_sha256,
                    postboot_stopped_receipt_sha256=postboot['stopped_receipt_sha256'])
        request = wire.Request(binding, 'A', wire.GUEST_START_INITIAL_POSTBOOT, 1, 'f'*64, args)
        return app, stage, request

    def test_derives_15_closed_16_open_from_exact_terminal_and_rejects_changed_terminal(self):
        app, stage, request = self.fixture()
        self.assertEqual((stage.closed_revision, stage.open_revision, stage.revoked_revision), (15, 16, 17))
        stage.proof_inputs.validate()
        with self.assertRaises(TransitionBlocked):
            app.SuccessorAppStage(lease=stage.lease, dispatcher=stage.dispatcher, powershell=lambda *a: None,
                proof_inputs=stage.proof_inputs, plan=stage.plan, predecessor_terminal=stage.terminal+b' ',
                launcher=stage.launcher, manifest_sha256=stage.manifest, prior_intent_sha256=stage.prior_intent,
                prior_progress_sha256=stage.prior_progress)

    def test_preflight_is_read_only_and_start_pins_carryover_authorization_at_every_boundary(self):
        from unittest.mock import Mock
        import time
        app, stage, request = self.fixture()
        events = []
        authorization = {'authorization_sha256':'1'*64}
        with patch.object(stage, '_proof', return_value='2'*64) as proof, patch.object(app, 'guest_attest', return_value=authorization) as attest:
            self.assertEqual(stage.preflight(time.monotonic()+20, assert_guard=lambda: None), '2'*64)
            attest.assert_not_called()
            def start(req, deadline, *, assert_windows_current):
                self.assertEqual(req, request)
                scope = {'version':wire.VERSION, 'binding':req.binding.fields(), 'node':'A',
                         'boot_sha256':req.binding.boot('A'), 'verb':req.verb, 'sequence':req.sequence, 'nonce':req.nonce}
                for boundary in wire.RECOVERY_START_BOUNDARIES:
                    challenge = wire._digest(wire._bytes({'request':scope, 'hold_sha256':req.args['hold_sha256'],
                        'boundary':boundary, 'postboot':stage.postboot, 'postboot_plan_sha256':stage.proof_inputs.plan_sha256,
                        'resume_authorization_sha256':'1'*64}))
                    self.assertEqual(assert_windows_current(challenge,boundary,deadline), '2'*64)
                return wire.Receipt('PASS','3'*64)
            stage.dispatcher.start_initial_postboot = start
            result=stage.start(request,time.monotonic()+20,assert_guard=lambda:None,
                              issued=lambda req:events.append('issued'),record=lambda name,value:events.append(name))
            self.assertEqual(result,'3'*64)
            self.assertEqual(events,['issued','authorization','started'])
            self.assertEqual(attest.call_args.args[6],stage.prior_progress)
            self.assertEqual(proof.call_count,len(wire.RECOVERY_START_BOUNDARIES)+2)
            with self.assertRaises(TransitionBlocked):
                stage.start(request,time.monotonic()+20,assert_guard=lambda:None,issued=lambda _:None,record=lambda *a:None)

    def test_failure_after_issue_cannot_retry_and_wrong_boundary_is_rejected(self):
        import time
        app, stage, request = self.fixture()
        with patch.object(stage,'_proof',return_value='2'*64), patch.object(app,'guest_attest',side_effect=OSError('lost')):
            with self.assertRaises(OSError):
                stage.start(request,time.monotonic()+20,assert_guard=lambda:None,issued=lambda _:None,record=lambda *a:None)
            self.assertTrue(stage.consumed)
        app, stage, request = self.fixture()
        def start(req, deadline, *, assert_windows_current):
            return assert_windows_current('0'*64,'recovery_start_intent',deadline)
        stage.dispatcher.start_initial_postboot=start
        with patch.object(stage,'_proof',return_value='2'*64), patch.object(app,'guest_attest',return_value={'authorization_sha256':'1'*64}):
            with self.assertRaises(TransitionBlocked):
                stage.start(request,time.monotonic()+20,assert_guard=lambda:None,issued=lambda _:None,record=lambda *a:None)

    def test_make_request_uses_exact_carryover_history_and_operation_nonce(self):
        from ha_control.native_guest_postboot import CARRYOVER_KIND
        app, stage, previous = self.fixture()
        projection={key:value for key,value in stage.postboot.items() if key not in {'carryover_receipt_sha256','predecessor_seal_sha256'}}
        args={key:previous.args[key] for key in (*wire.INITIAL_RECOVERY_ARGS,'hold_sha256')}
        carryover={'schema_version':1,'kind':CARRYOVER_KIND,'binding':previous.binding.fields(),
            'postboot':projection,'recovery_args':args,'predecessor_postboot':{},
            'predecessor_request':{'scope':{'nonce':'0'*64},'args':{}},
            'history':{'intent_sha256':stage.prior_intent,'progress_sha256':stage.prior_progress},
            'hold_sha256':args['hold_sha256'],'containers':{},'container_contracts':{},
            'protected_inventory_sha256':args['protected_inventory_sha256']}
        raw=wire._bytes(carryover)
        stage.postboot['carryover_receipt_sha256']=wire._digest(raw)
        request=stage.make_request('f'*64,raw,sequence=7)
        self.assertEqual((request.nonce,request.sequence),('f'*64,7))
        self.assertEqual(request.args|{},args|{'postboot':stage.postboot,
            'postboot_plan_sha256':stage.proof_inputs.plan_sha256,
            'postboot_stopped_receipt_sha256':stage.postboot['stopped_receipt_sha256']})
        with self.assertRaises(TransitionBlocked):stage.make_request('f'*64,raw+b' ')
        carryover['history']['progress_sha256']={}
        raw=wire._bytes(carryover)
        stage.postboot['carryover_receipt_sha256']=wire._digest(raw)
        with self.assertRaises(TransitionBlocked):stage.make_request('f'*64,raw)

    def test_publication_binds_predecessor_pid_and_current_successor_path(self):
        from tests.test_ha_postboot import SamebootPostbootSealTests
        import time
        app, stage, request=self.fixture()
        old=SamebootPostbootSealTests().transition_fixture()[1]['postboot-predecessor-binding.json']
        calls=[]
        def powershell(source,deadline):
            calls.append(source)
            self.assertIn('successor_previous_instance_running',source)
            self.assertIn('service_created_ticks',source)
            self.assertIn('Get-CimInstance Win32_Service',source)
            return {'predecessor_instance_stopped':True}
        stage.powershell=powershell
        with patch.object(stage,'_proof',return_value='a'*64) as proof:
            publication=stage.observe_publication(time.monotonic()+20,assert_guard=lambda:None,predecessor_binding=old)
            self.assertEqual(publication,{'plan_sha256':stage.proof_inputs.plan_sha256,'revision':16,
                'writers':['A'],'standby_node':'B','synchronous':True,
                'instance_sha256':stage.lease.service_instance_sha256,'predecessor_instance_stopped':True})
            self.assertEqual(proof.call_count,2)
            with self.assertRaises(TransitionBlocked):
                stage.observe_publication(time.monotonic()+20,assert_guard=lambda:None,predecessor_binding=old+b' ')
        self.assertEqual(len(calls),1)

    def test_actual_pair_sampler_checks_both_read_only_nodes_and_exact_revision(self):
        from datetime import datetime, timezone
        from types import SimpleNamespace
        from ha_control import initial_postboot
        from ha_control.initial_pg_lease import InitialPgLeaseRecovery
        from ha_runtime import effects
        app, stage, request = self.fixture()
        plan = copy.deepcopy(stage.plan)
        plan.update(cluster='fixture', system_id='1234', pg_nodes={node:{'host':node,'sslmode':'verify-full'} for node in ('A','B')})
        deployment = {'source_commit':'a'*40,'plan_sha256':request.binding.plan_sha256}
        plan['pg_enforcement']={'effects':{'deployment_binding':deployment,'broker_role':'ha_effect_broker'}}
        expiry = datetime(2026,1,1,tzinfo=timezone.utc)
        traces=[]
        nonce='postboot-a:'+stage.postboot['attempt_id']+':'+'a'*32+':lease'
        bad_revision=False
        class Cursor:
            def __init__(self,value):self.value=value
            def fetchone(self):return self.value
            def fetchall(self):return self.value
        class Connection:
            def __init__(self,node):self.node=node
            def close(self):traces.append((self.node,'close'))
            def execute(self,query,params=None):
                traces.append((self.node,query))
                if 'FROM ha.control c' in query:
                    revision=14 if bad_revision else 16
                    return Cursor(('fixture','1234',1,revision,1,'A','B',['A'],'A','AS',True,True,expiry,4.5,
                        self.node=='B','ha_'+self.node,'remote_apply','on','on','0/20',
                        'FIRST1(ha_b)' if self.node=='A' else 'FIRST1(ha_a)',True,'on',1,self.node))
                if 'controller_probes' in query:return Cursor(([nonce],))
                if query=='SHOW transaction_read_only':return Cursor(('on',))
                if 'FROM ha_effects.binding' in query:return Cursor([('fixture','1234',deployment['source_commit'],deployment['plan_sha256'],'ha_effect_broker')])
                if 'FROM pg_stat_replication' in query:return Cursor([('ha_b','ha_repl','B','streaming','sync',True,'0/20')])
                if 'FROM pg_stat_wal_receiver' in query:return Cursor([('streaming',1)])
                return Cursor(None)
        def connect(**kwargs):
            self.assertTrue(kwargs['autocommit'])
            self.assertIn('default_transaction_read_only=on',kwargs['options'])
            return Connection(kwargs['host'])
        with patch.object(effects,'validate_catalog'), patch.object(InitialPgLeaseRecovery,'quarantine_binding',return_value=('q',None,'r','s')), patch.object(InitialPgLeaseRecovery,'_quarantine_observation',return_value=('q','r','s')):
            value=app.sample_pair(plan,stage.proof_inputs.plan_sha256,stage.terminal,'a'*64,'b'*64,connect=connect)
            self.assertTrue(wire._hash(value['proof_sha256']))
            self.assertEqual(set(value['observations']),{'A','B'})
            self.assertEqual(sum(query=='close' for _,query in traces),2)
            self.assertFalse(any(query.startswith(('INSERT','UPDATE','DELETE','ALTER')) for _,query in traces))
            legacy_postboot = SimpleNamespace(scope=initial_postboot.scope, sameboot=initial_postboot.sameboot)
            with patch('ha_control.initial_postboot', legacy_postboot):
                legacy = app.sample_pair(plan,stage.proof_inputs.plan_sha256,stage.terminal,'a'*64,'b'*64,connect=connect)
                self.assertEqual(legacy['lease_sha256'], value['lease_sha256'])
            bad_revision=True
            with self.assertRaisesRegex(ValueError,'successor_pair_unverified'):
                app.sample_pair(plan,stage.proof_inputs.plan_sha256,stage.terminal,'a'*64,'b'*64,connect=connect)

    def test_pair_bracket_rejects_nonmatching_lease_and_expiry_credited_after_transport(self):
        import time
        app, stage, request = self.fixture()
        now=time.monotonic()
        native={'windows_generation':1,'service_incarnation_sha256':'1'*64,'heartbeat_sequence':2,'live_lease_sha256':'2'*64}
        pair={'proof_sha256':'3'*64,'lease_sha256':'2'*64,'observations':{'A':[.1,4.0],'B':[.2,3.5]},'child_elapsed_seconds':.3}
        output={'before':native,'after':native.copy(),'pair':pair,'offset':.2,'elapsed':.6}
        boundary=app.PairedAuthorityBoundary(lambda *_:copy.deepcopy(output),stage.proof_inputs,now+6)
        source="native -RecoveryContinuity -PriorSha256 '"+'4'*64+"'"
        boundary.powershell(source,now+5.9)
        proof=boundary.observe_pair(challenge='4'*64,service_incarnation_sha256='1'*64,deadline=now+5.9)
        self.assertAlmostEqual(proof['valid_until_monotonic'],boundary.started+.2+.2-.05+3.5)
        self.assertEqual(boundary.powershell(source,now+5.9),native)
        output['pair']['lease_sha256']='f'*64
        boundary=app.PairedAuthorityBoundary(lambda *_:copy.deepcopy(output),stage.proof_inputs,time.monotonic()+6)
        boundary.powershell(source,time.monotonic()+5.9)
        with self.assertRaises(TransitionBlocked):
            boundary.observe_pair(challenge='4'*64,service_incarnation_sha256='1'*64,deadline=time.monotonic()+5.9)

class SuccessorAuthorityTimingTests(unittest.TestCase):
    def fixture(self):
        return SuccessorAppWiringTests().fixture()

    def test_proof_retains_full_timeout_and_does_not_repeat_transport_error(self):
        app, stage, _ = self.fixture()
        calls = []
        def verify(read, recovery, limit, **kwargs):
            calls.append(limit)
            raise wire.BridgeRejected('guest_ssh_deadline_exceeded')
        stage.lease.verify_recovery_continuity = verify
        with patch.object(app.time, 'monotonic', return_value=100):
            with self.assertRaises(wire.BridgeRejected):
                stage._proof(106, lambda: None)
        self.assertEqual(calls, [106])

    def test_windows_bracket_resamples_in_one_call_and_still_requires_matching_lease(self):
        app, stage, _ = self.fixture()
        native = {'windows_generation': 1, 'service_incarnation_sha256': '1'*64,
                  'heartbeat_sequence': 2, 'live_lease_sha256': '2'*64}
        output = {'before': native, 'after': native | {'heartbeat_sequence': 3},
                  'pair': {'proof_sha256': '3'*64, 'lease_sha256': '2'*64,
                           'observations': {'A': [.1, 4.0], 'B': [.2, 3.5]}, 'child_elapsed_seconds': .3},
                  'offset': .2, 'elapsed': .6}
        commands = []
        def powershell(command, deadline):
            commands.append((command,deadline))
            return copy.deepcopy(output)
        def verify(read, recovery, deadline, *, observe_pair, **kwargs):
            challenge = '4'*64
            source = "native -RecoveryContinuity -PriorSha256 '" + challenge + "'"
            before = read(source, deadline)
            paired = observe_pair(challenge=challenge, service_incarnation_sha256=before['service_incarnation_sha256'], deadline=deadline)
            self.assertEqual(read(source, deadline)['heartbeat_sequence'], 3)
            return paired
        stage.powershell = powershell
        stage.lease.verify_recovery_continuity = verify
        with patch.object(app.time, 'monotonic', return_value=100):
            proof = stage._proof(106, lambda: None)
            self.assertEqual(len(commands), 1)
            command, deadline = commands[0]
            self.assertEqual(deadline, 106)
            self.assertIn('for($readIndex=0;$readIndex -lt 2;', command)
            self.assertIn('$before=$after', command)
            self.assertLess(command.index('$offset='), command.index('$rows='))
            self.assertAlmostEqual(proof['valid_until_monotonic'], 103.85)
            output['pair']['lease_sha256'] = 'f'*64
            with self.assertRaises(TransitionBlocked):
                stage._proof(106, lambda: None)
            self.assertEqual(len(commands), 2)

    def test_start_snapshots_partial_lineage_before_callbacks(self):
        app, stage, request = self.fixture()
        lineage = {'authorization_sha256': '1'*64, 'intent_sha256': '2'*64,
                   'progress_sha256': {'progress-ha-broker.json': '3'*64}}
        expected = copy.deepcopy(lineage)
        def proof(*args):
            lineage['progress_sha256'].clear()
            return '4'*64
        stage.dispatcher.start_initial_postboot = lambda *args, **kwargs: wire.Receipt('PASS', '5'*64)
        with patch.object(stage, '_proof', side_effect=proof), patch.object(app, 'guest_attest', return_value={'authorization_sha256': '6'*64}) as attest:
            stage.start(request, 1e20, assert_guard=lambda: None, issued=lambda _: None,
                        record=lambda *args: None, prior_resume=lineage)
        self.assertEqual(attest.call_args.kwargs['prior_resume'], expected)

if __name__=='__main__':unittest.main()
