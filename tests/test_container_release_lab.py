"""Controller contracts; real Docker execution is separately evidenced by the lab."""
import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
TOOLS=ROOT/'book-memo/tools'
CLI=TOOLS/'container_release_lab.py'


def module():
    if str(TOOLS) not in sys.path:sys.path.insert(0,str(TOOLS))
    return importlib.import_module('container_release_lab')


def descriptor(role,commit,build_commit):
    digit='1' if role=='v1' else '2'
    return {'schema_version':1,'app_commit':commit,'build_commit':build_commit,
            'files':{'app/main.py':digit*64,'app/services/book_service.py':digit*64},
            'payload_digest':digit*64,'main_sha':digit*64,'service_sha':digit*64}


class FakeDocker:
    instances=[]
    fail_stop=False
    fail_rollback=False
    schema_drift=False
    wrong_identity=False
    cleanup_ok=True
    def __init__(self,owner,deadline,interrupted):
        self.owner=owner;self.calls=[];self.active=None;self.images={};self.volumes={};self.count=0
        self.instances.append(self)
    def info(self):return {'engine_version':'29.8.2','os':'linux','architecture':'aarch64'}
    def build(self,path,desc,role):
        image={'id':'sha256:'+('1' if role=='v1' else '2')*64,'role':role,'os':'linux','architecture':'arm64'}
        image.update({k:desc[k] for k in ('app_commit','build_commit','main_sha','service_sha')})
        self.images[role]=image;return image
    def volume(self):
        name='volume'+str(len(self.volumes));self.volumes[name]=0;return name
    def start(self,image,volume,role):
        self.calls.append(('start',role))
        if self.active:raise RuntimeError('overlapping_versions')
        if self.fail_rollback and role=='v1' and self.volumes[volume]>0:raise RuntimeError('rollback_failed')
        self.count+=1;self.active=(str(self.count),volume,role);return str(self.count)
    def stop(self,container):
        self.calls.append(('stop',self.active[2]))
        if self.fail_stop:raise RuntimeError('stop_unknown')
        self.active=None
    def probe(self,container,op):
        _,volume,role=self.active;self.calls.append(('probe',op))
        if op in ('health','ready','read'):return {'http_status':200,'valid':True}
        if op=='missing':return {'http_status':404,'valid':False}
        if op=='identity':
            i=self.images[role]
            return {'http_status':200,'main_sha':('f'*64 if self.wrong_identity else i['main_sha']),
                    'service_sha':i['service_sha'],'trash_purge_route':role=='v2'}
        if op=='seed':self.volumes[volume]=1
        if op=='write_v2':self.volumes[volume]=2
        if op=='write_v1':self.volumes[volume]=3
        version=self.volumes[volume]
        return {'snapshot_hash':str(version)*64,'schema_hash':('f' if self.schema_drift and role=='v2' else 'a')*64,
                'book_hash':'b'*64,'supporting_hash':'c'*64,'integrity_ok':True,'book_count':1,'memo_count':1,
                'memo_version':version,'content_valid':True,'revision_count':version-1,'request_count':version-1,
                'revisions_valid':True,'replay_verified':op in ('write_v1','write_v2')}
    def cleanup(self):
        self.active=None
        return {'ok':self.cleanup_ok,'containers_removed':self.count,'volumes_removed':len(self.volumes),
                'images_removed':2,'residual_counts':{'containers':0 if self.cleanup_ok else 1,'volumes':0,'images':0}}


class ContainerReleaseTests(unittest.TestCase):
    def setUp(self):
        FakeDocker.instances=[]
        for name in ('fail_stop','fail_rollback','schema_drift','wrong_identity'):setattr(FakeDocker,name,False)
        FakeDocker.cleanup_ok=True
    def run_fake(self,scenario,interrupted=lambda:False):
        lab=module()
        def prepare(path,commit,build_commit,deadline,interrupted):return descriptor(path.name,commit,build_commit)
        with patch.object(lab,'prepare_context',side_effect=prepare):
            return lab.run_lab([scenario],1,time.monotonic()+20,interrupted,engine_factory=FakeDocker)
    def test_interrupt_during_cleanup_is_not_reported_as_success(self):
        flag=[False];original=FakeDocker.cleanup
        def cleanup(engine):
            result=original(engine);flag[0]=True;return result
        with patch.object(FakeDocker,'cleanup',cleanup):
            report=self.run_fake('upgrade',lambda:flag[0])
        self.assertFalse(report['valid']);self.assertEqual(report['reason'],'interrupted')
        self.assertTrue(report['cleanup']['ok'] and report['fixture_removed'])
    def test_real_state_contract_upgrade_and_backward_write(self):
        report=self.run_fake('roundtrip');self.assertTrue(report['valid'],report['reason'])
        case=report['cases'][0]
        self.assertTrue(case['deployment_succeeded'] and case['rollback_succeeded'])
        self.assertEqual(case['rollback_attempts'],1)
        self.assertTrue(case['v1_reads_v2'] and case['v1_writes_after_rollback'])
        self.assertEqual(case['observations']['v1_after_write']['memo_version'],3)
        self.assertTrue(report['cleanup']['ok'] and report['fixture_removed'])
    def test_readiness_failure_never_sends_candidate_write(self):
        report=self.run_fake('readiness_failure');self.assertTrue(report['valid'],report['reason'])
        case=report['cases'][0];self.assertFalse(case['deployment_succeeded']);self.assertTrue(case['rollback_succeeded'])
        self.assertNotIn(('probe','write_v2'),FakeDocker.instances[-1].calls)
        self.assertEqual(case['observations']['rejected_probe']['http_status'],404)
    def test_normal_upgrade_records_data_and_exact_replay(self):
        report=self.run_fake('upgrade');case=report['cases'][0]
        self.assertTrue(report['valid'] and case['deployment_succeeded'])
        self.assertEqual(case['observations']['v2_after_write']['memo_version'],2)
        self.assertTrue(case['observations']['v2_after_write']['replay_verified'])
    def test_wrong_runtime_identity_is_not_accepted(self):
        FakeDocker.wrong_identity=True;report=self.run_fake('upgrade')
        self.assertFalse(report['valid'])
        self.assertNotIn(('probe','seed'),FakeDocker.instances[-1].calls)
    def test_schema_change_is_not_accepted_or_automatically_downgraded(self):
        FakeDocker.schema_drift=True;report=self.run_fake('roundtrip')
        self.assertFalse(report['valid'])
        self.assertNotIn(('probe','write_v2'),FakeDocker.instances[-1].calls)
    def test_unknown_old_stop_never_starts_new_version(self):
        FakeDocker.fail_stop=True;report=self.run_fake('upgrade')
        self.assertFalse(report['valid']);self.assertEqual([c for c in FakeDocker.instances[-1].calls if c[0]=='start'],[('start','v1')])
    def test_failed_rollback_is_not_retried(self):
        FakeDocker.fail_rollback=True;report=self.run_fake('roundtrip')
        self.assertFalse(report['valid'])
        self.assertEqual([c for c in FakeDocker.instances[-1].calls if c[0]=='start'],[('start','v1'),('start','v2'),('start','v1')])
        self.assertEqual(report['cases'][0]['rollback_attempts'],1)
    def test_cleanup_failure_remains_failure_and_fixture_retained(self):
        lab=module();FakeDocker.cleanup_ok=False
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'owned';root.mkdir()
            with patch.object(lab.tempfile,'mkdtemp',return_value=str(root)):
                report=self.run_fake('upgrade')
            self.assertFalse(report['valid']);self.assertEqual(report['reason'],'cleanup_failed')
            self.assertFalse(report['fixture_removed']);shutil.rmtree(root)
    def test_expired_budget_does_not_connect_to_docker(self):
        lab=module()
        report=lab.run_lab(['upgrade'],1,time.monotonic()-1,lambda:False,engine_factory=FakeDocker)
        self.assertFalse(report['valid']);self.assertEqual(report['reason'],'deadline');self.assertFalse(FakeDocker.instances)
    def test_probe_rejects_arbitrary_operation_and_host_database(self):
        from container_probe import PROBE_SCRIPT
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'db';env={'PATH':os.defpath,'BOOK_MEMO_DB_PATH':str(db)}
            p=subprocess.run([sys.executable,'-c',PROBE_SCRIPT,'seed'],env=env,capture_output=True,text=True)
            self.assertNotEqual(p.returncode,0);self.assertFalse(db.exists())
            self.assertEqual(json.loads(p.stdout),{'probe_error':'fixed_probe_failed'})
    def test_cli_requires_explicit_run_and_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'old.json';out.write_text('original');link=Path(tmp)/'link.json';link.symlink_to(out)
            for args in [[],['--run'],['--run','--deadline','nan'],['--run','--repeats','4']]:
                p=subprocess.run([sys.executable,str(CLI),'--output',str(out),*args],capture_output=True,text=True)
                self.assertNotEqual(p.returncode,0)
            p=subprocess.run([sys.executable,str(CLI),'--run','--output',str(link)],capture_output=True,text=True)
            self.assertNotEqual(p.returncode,0);self.assertEqual(out.read_text(),'original')
    def test_state_evidence_rejects_boolean_counts(self):
        lab=module();report=self.run_fake('upgrade')
        state=report['cases'][0]['observations']['baseline'];state['book_count']=True
        self.assertFalse(lab.valid_state(state,1))
    def test_cli_deadline_saves_partial_evidence_without_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'partial.json'
            p=subprocess.run([sys.executable,str(CLI),'--run','--output',str(out),'--deadline','.000001'],capture_output=True,text=True)
            self.assertNotEqual(p.returncode,0);report=json.loads(out.read_text())
            self.assertEqual(report['reason'],'deadline');self.assertFalse(report['valid'])
            self.assertEqual(report['cleanup']['containers_removed'],0)
    @unittest.skipUnless(os.name=='posix','POSIX signals')
    def test_sigterm_partial_report_and_unrelated_process_preserved(self):
        script="""import sys,time
sys.path.insert(0,sys.argv.pop(1))
import container_release_lab as lab
class WaitingEngine:
    def __init__(self,owner,deadline,interrupted):
        while not interrupted():time.sleep(.01)
        raise RuntimeError('cancelled')
lab.LocalDocker=WaitingEngine
raise SystemExit(lab.main())
"""
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'partial.json'
            other=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
            p=subprocess.Popen([sys.executable,'-c',script,str(TOOLS),'--run','--output',str(out)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                for _ in range(100):
                    if out.exists() and out.stat().st_size:break
                    time.sleep(.02)
                p.send_signal(signal.SIGTERM);p.communicate(timeout=15)
                report=json.loads(out.read_text());self.assertFalse(report['valid'])
                self.assertEqual(report['reason'],'interrupted');self.assertIsNone(other.poll())
                self.assertTrue(report['cleanup']['ok'])
            finally:
                if p.poll() is None:p.kill();p.communicate()
                other.terminate();other.wait(timeout=5)


class ContainerContextTests(unittest.TestCase):
    def test_two_actual_git_versions_and_current_build_environment(self):
        lab=module()
        import release_artifact
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();repo=root/'repo';repo.mkdir()
            def git(*args):return subprocess.check_output(['git',*args],cwd=repo,text=True,stderr=subprocess.DEVNULL,env={'PATH':os.defpath}).strip()
            git('init','-q');git('config','user.email','fixture@example.invalid');git('config','user.name','Fixture')
            for name,body in {'app/main.py':'OLD=True','app/services/book_service.py':'OLD=True','Dockerfile':'FROM scratch','requirements.txt':'old==1'}.items():
                p=repo/'book-memo'/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(body)
            git('add','.');git('commit','-qm','old');old=git('rev-parse','HEAD')
            (repo/'book-memo/app/main.py').write_text('NEW=True');(repo/'book-memo/requirements.txt').write_text('new==2')
            git('add','.');git('commit','-qm','new');new=git('rev-parse','HEAD')
            contexts=root/'contexts';contexts.mkdir();(contexts/'.container-lab-owner').write_text('a'*32)
            with patch.object(release_artifact,'ROOT',repo),patch.object(lab,'OLD_COMMIT',old):
                first=lab.prepare_context(contexts/'v1',old,new,time.monotonic()+10,lambda:False)
                second=lab.prepare_context(contexts/'v2',new,new,time.monotonic()+10,lambda:False)
                self.assertNotEqual(first['main_sha'],second['main_sha'])
                self.assertEqual((contexts/'v1/app/main.py').read_text(),'OLD=True')
                self.assertEqual((contexts/'v1/requirements.txt').read_text(),'new==2')
                self.assertEqual(first['files']['Dockerfile'],second['files']['Dockerfile'])
                with self.assertRaises(ValueError):lab.prepare_context(contexts/'v1',old,new,time.monotonic()+10,lambda:False)


def restore_module():
    module()
    return importlib.import_module('independent_restore_lab')


class RestoreDocker(FakeDocker):
    wrong_mount=False
    wrong_uid=False
    corrupt_restore=False
    restore_failure=False
    def payload(self):
        lab=restore_module()
        tables={name:[] for name in lab.TABLES};tables['books']=[[1,'synthetic']]
        schema=[['table','books','books','CREATE TABLE books(id,name)']]
        state=super().probe(self.active[0],'snapshot')
        state['snapshot_hash']=lab.digest(tables);state['schema_hash']=lab.digest(schema)
        return {'schema_version':1,'tables':tables,'schema':schema,'state':state}
    def export_snapshot(self,container):return self.payload()
    def probe(self,container,op):
        result=super().probe(container,op)
        if op in ('seed','snapshot') and self.volumes[self.active[1]]==1:
            result=self.payload()['state']
        return result
    def restore_snapshot(self,container,payload):
        self.calls.append(('restore',container))
        if self.restore_failure:raise RuntimeError('fixture_restore_failed')
        self.volumes[self.active[1]]=1
        result=self.probe(container,'snapshot')
        if self.corrupt_restore:result['snapshot_hash']='f'*64
        return result
    def restore_evidence(self,container):
        _,volume,role=self.active
        return {'container_id':container,'image_id':self.images[role]['id'],'volume_name':volume,
                'mount_volume_names':['volume0' if self.wrong_mount and volume=='volume1' else volume],
                'owner_verified':True,'user':'0:0' if self.wrong_uid else '10001:10001',
                'network':'none','readonly_root':True,'published_ports':False}


class IndependentRestoreTests(unittest.TestCase):
    def setUp(self):
        RestoreDocker.instances=[]
        RestoreDocker.cleanup_ok=True
        for name in ('wrong_mount','wrong_uid','corrupt_restore','restore_failure','fail_stop','fail_rollback','schema_drift','wrong_identity'):
            setattr(RestoreDocker,name,False)
    def run_fake(self,interrupted=lambda:False):
        lab=restore_module()
        with patch.object(lab,'prepare_context',side_effect=lambda path,commit,build_commit,*args:descriptor('v2',commit,build_commit)):
            return lab.run_lab(time.monotonic()+20,interrupted,engine_factory=RestoreDocker)
    def test_restore_uses_fresh_destination_and_accounts_lost_ack_write(self):
        report=self.run_fake()
        self.assertTrue(report['valid'],report)
        self.assertNotEqual(report['source']['container_id'],report['destination']['container_id'])
        self.assertNotEqual(report['source']['volume_name'],report['destination']['volume_name'])
        self.assertNotIn(report['source']['volume_name'],report['destination']['mount_volume_names'])
        self.assertEqual(report['recovery']['lost_acknowledged_writes'],1)
        self.assertEqual(report['restored_state']['memo_version'],1)
        self.assertEqual(report['additional_acknowledged_write']['state']['memo_version'],2)
        self.assertGreaterEqual(report['recovery']['rto_seconds'],0)
        self.assertTrue(report['cleanup']['ok'] and report['fixture_removed'])
        self.assertEqual(report['image_archive_export_import'],'not_exercised')
    def test_restore_wrong_uid_or_source_mount_fails_closed(self):
        for name in ('wrong_uid','wrong_mount'):
            with self.subTest(name=name):
                setattr(RestoreDocker,name,True)
                report=self.run_fake()
                self.assertFalse(report['valid'])
                self.assertFalse(any(call[0]=='restore' for call in RestoreDocker.instances[-1].calls))
                setattr(RestoreDocker,name,False)
    def test_restore_corrupt_or_failed_restore_still_cleans_owned_resources(self):
        for name in ('corrupt_restore','restore_failure'):
            with self.subTest(name=name):
                setattr(RestoreDocker,name,True)
                report=self.run_fake()
                self.assertFalse(report['valid'])
                self.assertTrue(report['cleanup']['ok'] and report['fixture_removed'])
                self.assertEqual(len([c for c in RestoreDocker.instances[-1].calls if c[0]=='restore']),1)
                setattr(RestoreDocker,name,False)
    def test_tampered_bundle_is_rejected_before_destination_creation(self):
        lab=restore_module();original=lab.read_bundle
        def tamper(path,expected):
            path.write_bytes(path.read_bytes()+b' ')
            return original(path,expected)
        with patch.object(lab,'read_bundle',side_effect=tamper):report=self.run_fake()
        self.assertFalse(report['valid']);self.assertEqual(report['reason'],'snapshot_bundle_hash_mismatch')
        self.assertEqual(len(RestoreDocker.instances[-1].volumes),1)
        self.assertTrue(report['cleanup']['ok'])
    def test_corrupt_release_artifact_is_rejected_before_source_creation(self):
        original=RestoreDocker.build
        def corrupt(engine,*args):
            image=original(engine,*args);image['main_sha']='f'*64;return image
        with patch.object(RestoreDocker,'build',corrupt):report=self.run_fake()
        self.assertFalse(report['valid']);self.assertEqual(report['reason'],'image_provenance_mismatch')
        self.assertFalse(RestoreDocker.instances[-1].volumes)
    def test_restore_cleanup_failure_preserves_fixture_and_fails(self):
        lab=restore_module();RestoreDocker.cleanup_ok=False
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'owned';root.mkdir()
            with patch.object(lab.tempfile,'mkdtemp',return_value=str(root)):report=self.run_fake()
            self.assertFalse(report['valid']);self.assertEqual(report['reason'],'cleanup_failed')
            self.assertTrue(root.exists());self.assertFalse(report['fixture_removed'])
    def test_report_write_failure_never_bypasses_owned_cleanup(self):
        lab=restore_module()
        for failed_stage in ('additional_write_acknowledged','cleanup','finished'):
            with self.subTest(stage=failed_stage):
                stages=[]
                def broken_progress(report):
                    stages.append(report['stage'])
                    if report['stage']==failed_stage:raise OSError('fixture_disk_full')
                with patch.object(lab,'prepare_context',side_effect=lambda path,commit,build_commit,*args:descriptor('v2',commit,build_commit)):
                    report=lab.run_lab(time.monotonic()+20,lambda:False,broken_progress,RestoreDocker)
                self.assertFalse(report['valid']);self.assertTrue(report['reporting_failed'])
                self.assertEqual(report['reason'],'report_output_failed')
                self.assertTrue(report['cleanup']['ok'] and report['fixture_removed'])
                self.assertIsNone(RestoreDocker.instances[-1].active)
                self.assertIn('cleanup',stages);self.assertIn('finished',stages)
    def test_bundle_rejects_link_boolean_version_and_bad_hash(self):
        lab=restore_module()
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'data';p.write_text('{}');link=Path(tmp)/'link';link.symlink_to(p)
            with self.assertRaises(lab.Stopped):lab.read_bundle(link,lab.digest({}))
        for payload in ({'schema_version':True}, {'schema_version':1,'tables':{},'schema':[],'state':{}}):
            with self.assertRaises(lab.Stopped):lab.verify_payload(payload)
    def test_restore_cli_deadline_records_json_and_jsonl_without_docker(self):
        cli=TOOLS/'independent_restore_lab.py'
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'report.json';events=Path(tmp)/'events.jsonl'
            result=subprocess.run([sys.executable,str(cli),'--run','--output',str(out),'--jsonl',str(events),'--deadline','.000001'],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            report=json.loads(out.read_text());self.assertEqual(report['reason'],'deadline')
            self.assertTrue(all(json.loads(line)['valid'] is False for line in events.read_text().splitlines()))
    def test_restore_probe_rejects_nonfixture_uid_and_host_database(self):
        from container_probe import RESTORE_SCRIPT
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'db'
            result=subprocess.run([sys.executable,'-c',RESTORE_SCRIPT,'export'],env={'PATH':os.defpath,'BOOK_MEMO_DB_PATH':str(db)},capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0);self.assertFalse(db.exists())
            self.assertEqual(json.loads(result.stdout),{'probe_error':'fixed_restore_probe_failed'})
    def test_fixed_restore_transaction_preserves_exact_data_and_rolls_back_bad_foreign_key(self):
        # The transfer algorithm runs against real isolated SQLite files. Only
        # the container path/UID boundary is replaced here; Docker proves it live.
        script=r'''
import base64,copy,hashlib,json,os,sys,tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[2])
from container_probe import RESTORE_SCRIPT
from app.services import book_service as service
namespace={}
exec(RESTORE_SCRIPT.rsplit('\ntry:\n',1)[0],namespace)
def encode(value):return base64.b64encode(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).decode()
with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp)
    namespace['database']=lambda module: module.DB_PATH.parent==root or (_ for _ in ()).throw(ValueError('fixture_boundary'))
    with patch.dict(os.environ,{'BOOK_MEMO_DB_PATH':'/app/data/fixture.sqlite3'}),patch('os.getuid',return_value=10001),patch('os.getgid',return_value=10001):
        service.DB_PATH=root/'source.sqlite3';service.init_db()
        namespace['execute']('seed')
        sys.argv=['probe','export'];payload=namespace['transfer']('export')
        service.DB_PATH=root/'restore.sqlite3';service.init_db()
        sys.argv=['probe','restore',encode(payload)];restored=namespace['transfer']('restore')
        assert restored==payload['state'],(restored,payload['state'])
        service.DB_PATH=root/'rollback.sqlite3';service.init_db()
        bad=copy.deepcopy(payload);bad['tables']['book_memos'][0][1]=999
        bad['state']['snapshot_hash']=namespace['digest'](bad['tables'])
        sys.argv=['probe','restore',encode(bad)]
        try:namespace['transfer']('restore')
        except ValueError:pass
        else:raise AssertionError('foreign key damage accepted')
        with service._connect() as db:
            assert all(db.execute('SELECT count(*) FROM '+name).fetchone()[0]==0 for name in namespace['TABLES'])
print('fixture_transfer=PASS')
'''
        result=subprocess.run([sys.executable,'-c',script,str(TOOLS),str(ROOT/'book-memo')],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'fixture_transfer=PASS')
    def test_transfer_adapter_rechecks_runtime_and_never_executes_foreign_container(self):
        from container_runtime import LocalDocker
        runtime=LocalDocker.__new__(LocalDocker)
        with patch.object(runtime,'_container',side_effect=RuntimeError('wrong_owner')),patch.object(runtime,'_run') as execute:
            with self.assertRaises(RuntimeError):runtime.export_snapshot('foreign')
            with self.assertRaises(RuntimeError):runtime.restore_snapshot('foreign',{})
            execute.assert_not_called()
        with patch.object(runtime,'_container',return_value={'Id':'a'*64,'State':{'Running':True}}),patch.object(runtime,'_record',return_value=('owned',{})),patch.object(runtime,'_verify_runtime',side_effect=RuntimeError('wrong_uid')),patch.object(runtime,'_run') as execute:
            with self.assertRaises(RuntimeError):runtime.restore_snapshot('a'*64,{})
            execute.assert_not_called()
