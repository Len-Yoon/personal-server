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
        self.commands=[];self.challenges=[];self.failed_boundary=None
        def read(path,*a,**kw):return Path(path).read_bytes()
        def write(path,body,*a):
            with Path(path).open('xb') as out:out.write(body)
        def docker(*command,**kw):
            self.commands.append(command)
            if command[0]=='start':
                role=next(r for r,v in self.containers.items() if v['container_id']==command[1])
                self.contracts[role].update(running=True,started_at='resumed')
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
        with self.assertRaises(TransitionBlocked):resume.preserved(self.request,body,value,self.hold,self.active)

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

if __name__=='__main__':unittest.main()
