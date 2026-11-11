"""Frozen-source boundaries and failure receipts; no host/production access."""
import copy
from datetime import datetime,timedelta,timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'infra/k8s'))
from ha_runtime import migrate,seven_migration as M


def encoded(value):return json.dumps(value,sort_keys=True).encode()
def sha(raw):return hashlib.sha256(raw).hexdigest()


class FrozenFixture:
    def __init__(self,root):
        self.root=root;self.artifacts={}
        self.config={'source_commit':'a'*40,'cluster':'migration_fixture','plan_sha256':'b'*64}
        self.services={service:{'databases':{},'states':{}} for service in M.SERVICES}
        for service in sorted(M.SERVICES):
            for component in sorted(M.REQUIRED_DATABASES[service]):
                key=service+'.'+component
                path=root/(key+'.sqlite3')
                with sqlite3.connect(path) as db:
                    db.execute('CREATE TABLE records(id INTEGER PRIMARY KEY AUTOINCREMENT,value TEXT NOT NULL)')
                    db.execute('INSERT INTO records(value) VALUES(?)',('retained 한글',))
                path.chmod(0o600);self.add_file(key,path)
                self.services[service]['databases'][component]=key
            for key in sorted(M.REQUIRED_STATES[service]):
                payload={}
                if key=='portfolio':payload={'content':'retained portfolio'}
                if key=='auth_sessions':payload={'sessions':{},'confirmations':{}}
                if key=='archive':payload={'articles':[]}
                if key=='file-namespace':payload={'format':1,'version':0,'entries':{},'trash':{},'confirmations':{},'audit':[]}
                identifier=service+'.'+key
                self.add_json(identifier,payload)
                self.services[service]['states'][key]=identifier
        self.add_json('private-hyundai',{'refresh_token':'fixture-private-refresh-value'})
        now=datetime.now(timezone.utc)
        self.receipt={'schema_version':1,'capture_id':'1'*32,'source_runtime_sha256':'2'*64,'writer_graph_sha256':'3'*64,
            'writer_fence_receipt_sha256':'4'*64,'writers_closed_at':(now-timedelta(seconds=3)).isoformat().replace('+00:00','Z'),
            'capture_started_at':(now-timedelta(seconds=2)).isoformat().replace('+00:00','Z'),
            'capture_finished_at':(now-timedelta(seconds=1)).isoformat().replace('+00:00','Z'),
            'services':sorted(M.SERVICES),'target_system_id':'123456','cluster':self.config['cluster'],
            'plan_sha256':self.config['plan_sha256'],'artifacts':{}}
        evidence={}
        for key,field in (('runtime','source_runtime_sha256'),('writer_graph','writer_graph_sha256'),
                          ('writer_closure','writer_fence_receipt_sha256'),('dispositions',None)):
            path=root/(key+'.json');raw=encoded({'fixture':key});path.write_bytes(raw);path.chmod(0o600)
            evidence[key]={'file':path.name,'sha256':sha(raw),'bytes':len(raw)}
            if field:self.receipt[field]=sha(raw)
        path=root/'capture-audit.json';raw=encoded({'schema_version':1,'evidence':evidence})
        path.write_bytes(raw);path.chmod(0o600)
        self.receipt['capture_audit']={'file':path.name,'sha256':sha(raw),'bytes':len(raw)}
        self.value={'schema_version':1,**self.config,'target_system_id':'123456','freeze_receipt':{},'artifacts':self.artifacts,
            'services':self.services,'portal_objects':{},'private_states':{'car-care-worker':{'hyundai_tokens':'private-hyundai'}}}
        self.path=root/'manifest.json';self.save()

    def add_file(self,key,path):
        raw=path.read_bytes();self.artifacts[key]={'file':path.name,'sha256':sha(raw),'bytes':len(raw)}

    def add_json(self,key,value):
        path=self.root/(key+'.json');path.write_bytes(encoded(value));path.chmod(0o600);self.add_file(key,path)

    def save(self):
        self.receipt['artifacts']={key:{'sha256':value['sha256'],'bytes':value['bytes']} for key,value in self.artifacts.items()}
        raw=encoded(self.receipt);path=self.root/'freeze.json';path.write_bytes(raw);path.chmod(0o600)
        self.value['freeze_receipt']={'file':path.name,'sha256':sha(raw),'bytes':len(raw)}
        self.path.write_bytes(encoded(self.value));self.path.chmod(0o600);self.expected=sha(self.path.read_bytes())

    def prepare(self):return M.FrozenSources(self.path,self.expected,self.config)


class FrozenInputTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.env=patch.dict(os.environ,{'APP_ENV':'test','HA_LOCAL_RUNTIME':'1'});self.env.start()
        self.fixture=FrozenFixture(self.root)

    def tearDown(self):self.env.stop();self.temp.cleanup()

    def reject(self,reason=None):
        with self.assertRaises(M.MigrationRejected) as error:self.fixture.prepare()
        if reason:self.assertEqual(str(error.exception),reason)

    def test_all_seven_sources_remain_private_and_unchanged(self):
        source=self.fixture.prepare();source.unchanged()
        self.assertEqual(set(source.states),set(M.SERVICES))
        self.assertNotIn('hyundai_tokens',source.states['car-care-worker'])
        self.assertEqual(source.private_payload['refresh_token'],'fixture-private-refresh-value')

    def test_source_only_preflight_does_not_enable_unpackaged_import(self):
        with patch.dict(os.environ,{'APP_ENV':'production','HA_LOCAL_RUNTIME':'0'}):
            self.reject('packaged_migration_release_required')
            source=M.FrozenSources(self.fixture.path,self.fixture.expected,self.fixture.config,source_only_preflight=True)
            source.unchanged()

    def test_one_service_omission_is_never_defaulted_to_empty(self):
        del self.fixture.services['system-agent'];self.fixture.save();self.reject('all_seven_sources_required')

    def test_manifest_identity_and_external_hash_are_both_required(self):
        self.fixture.config['cluster']='another_cluster';self.reject('frozen_manifest_identity_mismatch')
        with self.assertRaises(M.MigrationRejected):M.FrozenSources(self.fixture.path,'0'*64,self.fixture.config)

    def test_freeze_must_follow_writer_closure_and_bind_exact_inventory(self):
        self.fixture.receipt['writers_closed_at']=self.fixture.receipt['capture_finished_at']
        self.fixture.save();self.reject('freeze_capture_order_invalid')

    def test_changed_artifact_and_late_mutation_are_rejected(self):
        source=self.fixture.prepare()
        path=self.root/self.fixture.artifacts['crawler-worker.archive']['file'];path.write_bytes(b'{} ')
        with self.assertRaises(M.MigrationRejected):source.unchanged()
        self.reject('frozen_artifact_binding_invalid')

    def test_external_evidence_bytes_are_bound_and_cannot_change_after_validation(self):
        source=self.fixture.prepare();path=self.root/'writer_closure.json'
        path.write_bytes(b'{"changed":true}')
        with self.assertRaises(M.MigrationRejected):source.unchanged()
        self.reject('frozen_artifact_binding_invalid')

    def test_rereading_same_bytes_cannot_reset_original_metadata_stamp(self):
        source=self.fixture.prepare()
        path=self.root/self.fixture.artifacts['crawler-worker.archive']['file'];path.chmod(0o400)
        with self.assertRaises(M.MigrationRejected):source.artifact('crawler-worker.archive')

    def test_linked_or_nonprivate_artifacts_are_rejected(self):
        path=self.root/self.fixture.artifacts['crawler-worker.archive']['file'];path.chmod(0o644)
        self.reject('private_frozen_input_required');path.chmod(0o600)
        link=self.root/'linked';os.link(path,link);self.reject('private_frozen_input_required')

    def test_traversal_and_unconsumed_artifacts_are_rejected(self):
        self.fixture.artifacts['crawler-worker.archive']['file']='../outside.json';self.fixture.save()
        self.reject('artifact_relative_file_required')

    def test_unused_source_is_not_silently_discarded(self):
        self.fixture.add_json('unconsumed-source',{'retained':True});self.fixture.save()
        self.reject('unconsumed_frozen_artifact')

    def test_live_wal_snapshot_and_executable_ddl_are_rejected_before_pg(self):
        path=self.root/self.fixture.artifacts['book-memo.main']['file']
        sidecar=Path(str(path)+'-wal');sidecar.write_bytes(b'uncheckpointed');sidecar.chmod(0o600)
        self.reject('sqlite_snapshot_sidecar_rejected')

    def test_provider_credential_cannot_move_to_another_service(self):
        self.fixture.add_json('crawler-worker.archive',{'articles':[],'alias':'fixture-private-refresh-value'})
        self.fixture.save();self.reject('private_credential_cross_service_copy_rejected')

    def test_app_credential_keys_and_wrong_private_service_are_rejected(self):
        for key in ('client_secret','authorization_state'):
            self.fixture.add_json('crawler-worker.archive',{key:'must-not-enter-app-state'})
            self.fixture.save();self.reject('app_provider_credential_rejected')

    def test_portal_object_set_requires_exact_reference_and_bytes(self):
        content=b'actual frozen object';identifier=sha(content)
        namespace={'format':1,'version':0,'entries':{'file.txt':{'type':'file','size':len(content),'sha256':identifier}},'trash':{},'confirmations':{},'audit':[]}
        self.fixture.add_json('portal-web.file-namespace',namespace);self.fixture.save()
        self.reject('portal_blob_inventory_mismatch')
        path=self.root/'blob';path.write_bytes(content);path.chmod(0o600);self.fixture.add_file('blob',path)
        self.fixture.value['portal_objects']={identifier:'blob'};self.fixture.save()
        self.assertEqual(self.fixture.prepare().objects,{identifier:'blob'})

    def test_json_database_rejection_context_cannot_receive_nonfinite_or_nul_data(self):
        for raw in (b'{"value":NaN}',b'{"value":1e999}',b'{"value":"\\u0000"}',b'{"value":"\\ud800"}'):
            with self.assertRaises(M.MigrationRejected):M.json_payload(raw)

    def test_raw_bearer_session_and_unknown_restart_state_never_enter_typed_state(self):
        self.fixture.add_json('book-memo.write-sessions',{'raw-legacy-bearer':'2026-11-11T00:00:00+00:00'})
        self.fixture.save();self.reject('typed_session_artifact_required')
        with self.assertRaises(M.MigrationRejected):
            M.validate_state('homeops-executor','restart_claims',{'request':{'payload_hash':'a'*64,'created_at':1,'state':'invented','result':None}})

    def test_noncanonical_hidden_portal_path_is_rejected_by_actual_app_validator(self):
        namespace={'format':1,'version':0,'entries':{'.private':{'type':'folder'}},'trash':{},'confirmations':{},'audit':[]}
        self.fixture.add_json('portal-web.file-namespace',namespace);self.fixture.save()
        with self.assertRaises(DatabaseErrorTypes):self.fixture.prepare()


DatabaseErrorTypes=(M.MigrationRejected,M.DatabaseUnavailable)


class SourceSqlTests(unittest.TestCase):
    def test_data_literals_survive_but_functions_cannot_execute_as_pg_admin(self):
        self.assertIn("'lo_export(1)'",migrate._ddl("CREATE TABLE good(id INTEGER PRIMARY KEY,value TEXT DEFAULT 'lo_export(1)')"))
        for source in ["CREATE TABLE x(value TEXT CHECK(lo_export(1,'x')=1))",
                       'CREATE TABLE x(value TEXT DEFAULT (pg_catalog.current_setting(\'data_directory\')))',
                       'CREATE TABLE x(value TEXT CHECK("pg_sleep"(1) IS NULL))',
                       'CREATE TABLE x(value TEXT); DROP TABLE anything',
                       'CREATE TABLE x(value TEXT GENERATED ALWAYS AS (lower(value)) STORED)']:
            with self.subTest(source=source):
                with self.assertRaises(M.DatabaseUnavailable):migrate._ddl(source)

    def test_sqlitex_is_a_user_table_not_the_sqlite_reserved_prefix(self):
        with sqlite3.connect(':memory:') as source:
            source.execute('CREATE TABLE sqlitexevil(value TEXT)')
            self.assertEqual(set(migrate._source_tables(source)),{'sqlitexevil'})

    def test_triggers_fail_before_any_pg_call(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory).resolve()/'source.sqlite3'
            with sqlite3.connect(path) as source:
                source.execute('CREATE TABLE records(value TEXT)')
                source.execute('CREATE TRIGGER t AFTER INSERT ON records BEGIN SELECT 1; END')
            admin=MagicMock()
            with self.assertRaises(M.DatabaseUnavailable):migrate.copy_sqlite(admin,'book-memo','main',path)
            admin.execute.assert_not_called()


class TransactionReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.admin=MagicMock();self.admin.autocommit=True;self.admin.info.transaction_status=0
        self.source=SimpleNamespace(manifest_sha256='a'*64,freeze_receipt_sha256='b'*64,capture_audit_sha256='d'*64,objects={},manifest={'target_system_id':'123'})
        self.config={'source_commit':'c'*40,'primary_node':'A'};self.receipt=self.root/'receipt.json'

    def tearDown(self):self.temp.cleanup()

    def run_apply(self,prepare,guard):
        return M.apply(self.admin,self.config,self.root/'unused.json','a'*64,self.receipt,prepare,guard)

    def test_existing_transaction_cannot_turn_outer_commit_into_savepoint(self):
        for autocommit,status in ((False,0),(True,2)):
            self.admin.autocommit=autocommit;self.admin.info.transaction_status=status
            with self.assertRaisesRegex(M.MigrationRejected,'idle_autocommit_migration_connection_required'):
                self.run_apply(MagicMock(),MagicMock())
        self.admin.transaction.assert_not_called();self.assertFalse(self.receipt.exists())

    def test_import_failure_rolls_back_without_guard_or_authority_and_keeps_receipts(self):
        prepare=MagicMock();guard=MagicMock()
        with patch.object(M,'FrozenSources',return_value=self.source),patch.object(M,'fresh_target'),patch.object(M,'import_data',side_effect=RuntimeError('data_invalid')):
            with self.assertRaises(M.MigrationRejected):self.run_apply(prepare,guard)
        prepare.assert_called_once();guard.assert_not_called()
        self.assertIs(self.admin.transaction.return_value.__exit__.call_args.args[0],RuntimeError)
        self.assertFalse(self.receipt.exists())
        value=json.loads(Path(str(self.receipt)+'.failed').read_text())
        self.assertEqual(value['status'],'FAILED');self.assertFalse(value['writer_authority_issued'])
        self.assertTrue(Path(str(self.receipt)+'.prepared').exists())

    def test_commit_acknowledgement_loss_is_unknown_not_retry_or_success(self):
        self.admin.transaction.return_value.__exit__.side_effect=OSError('connection_lost')
        with patch.object(M,'FrozenSources',return_value=self.source),patch.object(M,'fresh_target'),patch.object(M,'import_data',return_value=([],{})),patch.object(M,'finalize_data'):
            with self.assertRaises(M.MigrationRejected):self.run_apply(MagicMock(),MagicMock())
        self.assertFalse(self.receipt.exists())
        value=json.loads(Path(str(self.receipt)+'.failed').read_text());self.assertEqual(value['status'],'UNKNOWN_COMMIT')
        self.assertFalse(value['automatic_enable_performed'])

    def test_existing_receipt_is_preserved_before_any_prepare_or_write(self):
        self.receipt.write_text('preserve');prepare=MagicMock()
        with patch.object(M,'FrozenSources',return_value=self.source),patch.object(M,'fresh_target'):
            with self.assertRaises(M.MigrationRejected):self.run_apply(prepare,MagicMock())
        prepare.assert_not_called();self.assertEqual(self.receipt.read_text(),'preserve')

    def test_wrong_database_and_contamination_never_clean_up_target(self):
        admin=MagicMock();admin.info.dbname='postgres'
        with self.assertRaises(M.MigrationRejected):M.fresh_target(admin,self.source,'A')
        admin.execute.assert_not_called()
        admin.info.dbname='ha'
        admin.execute.return_value.fetchone.side_effect=[(False,'ha_A','123'),(True,),(True,),(0,),(1,)]
        with self.assertRaises(M.MigrationRejected):M.fresh_target(admin,self.source,'A')
        self.assertFalse(any('DROP ' in str(call.args[0]).upper() or 'DELETE ' in str(call.args[0]).upper() for call in admin.execute.call_args_list))


def postgres_integration(host):
    """Explicit disposable-PG probe, never discovered as an implicit CI test.

    The caller owns an isolated network and fresh PG17 server named by host.
    This exercises the real transaction/import/guards, not source capture or a
    synchronous pair; the initializer's replication preflight remains separate.
    """
    import importlib.util
    import secrets
    import psycopg
    if os.getenv('APP_ENV')!='test' or os.getenv('HA_LOCAL_RUNTIME')!='1':
        raise AssertionError('isolated_test_environment_required')
    spec=importlib.util.spec_from_file_location('migration_initializer',ROOT/'infra/k8s/ha-package/bootstrap_assets/initialize.py')
    initializer=importlib.util.module_from_spec(spec);spec.loader.exec_module(initializer)
    with psycopg.connect(host=host,dbname='postgres',user='postgres',autocommit=True) as setup:
        setup.execute('CREATE DATABASE ha')
    with tempfile.TemporaryDirectory() as directory,psycopg.connect(host=host,dbname='ha',user='postgres',autocommit=True) as admin:
        root=Path(directory);fixture=FrozenFixture(root)
        for service in ('crawler-worker','homeops-executor'):
            key=service+'.main';path=root/(key+'.sqlite3')
            with sqlite3.connect(path) as source:
                source.execute('CREATE TABLE retained(id INTEGER PRIMARY KEY,value TEXT)')
                source.execute('INSERT INTO retained VALUES(7,?)',('preserved '+service,))
            path.chmod(0o600);fixture.add_file(key,path);fixture.services[service]['databases']['main']=key
        content=b'real PG frozen blob';identifier=sha(content);path=root/'blob'
        path.write_bytes(content);path.chmod(0o600);fixture.add_file('blob',path)
        fixture.add_json('portal-web.file-namespace',{'format':1,'version':0,
            'entries':{'retained.txt':{'type':'file','size':len(content),'sha256':identifier}},
            'trash':{},'confirmations':{},'audit':[]})
        fixture.value['portal_objects']={identifier:'blob'}
        system=admin.execute('SELECT system_identifier::text FROM pg_control_system()').fetchone()[0]
        fixture.value['target_system_id']=system;fixture.receipt['target_system_id']=system;fixture.save()
        config={**fixture.config,'primary_node':'A','broker_instance':'e'*64,
                'passwords':{service.replace('-','_')+'_'+node:secrets.token_urlsafe(32)
                             for service in M.SERVICES for node in ('a','b')}}
        config['passwords']['ha_effect_broker']=secrets.token_urlsafe(32)
        def fail_after_data(connection,value,entries):
            assert connection.execute('SELECT count(*) FROM s_book_memo_main.records').fetchone()==(1,)
            raise RuntimeError('deliberate_mid_import_failure')
        try:
            M.apply(admin,config,fixture.path,fixture.expected,root/'rollback.json',initializer.prepare_closed_control,fail_after_data)
        except M.MigrationRejected:
            pass
        else:raise AssertionError('deliberate_failure_committed')
        assert admin.execute("SELECT to_regnamespace('ha'),to_regnamespace('s_book_memo_main')").fetchone()==(None,None)
        assert admin.execute("SELECT count(*) FROM pg_roles WHERE rolname='book_memo_a'").fetchone()==(0,)
        assert json.loads((root/'rollback.json.failed').read_text())['status']=='FAILED'
        receipt=M.apply(admin,config,fixture.path,fixture.expected,root/'success.json',initializer.prepare_closed_control,initializer.install_guarded_catalog)
        assert receipt['status']=='PASS' and receipt['writer_authority_issued'] is False
        assert admin.execute('SELECT NOT writes_open AND NOT history_safe FROM ha.control').fetchone()==(True,)
        assert admin.execute('SELECT count(*) FROM ha.enforced_roles').fetchone()==(14,)
        assert admin.execute('SELECT count(DISTINCT service) FROM ha.enforced_namespaces').fetchone()==(7,)
        for service in ('crawler-worker','homeops-executor'):
            assert admin.execute('SELECT id,value FROM s_'+service.replace('-','_')+'_main.retained').fetchone()==(7,'preserved '+service)
        from ha_runtime.backup import _pg_blob
        assert _pg_blob(admin,identifier,len(content))==content
        assert admin.execute("SELECT payload->>'refresh_token' FROM ha_effects.private_state WHERE service='car-care-worker' AND key='hyundai_tokens'").fetchone()==('fixture-private-refresh-value',)
        with psycopg.connect(host=host,dbname='ha',user='book_memo_a',autocommit=True) as app:
            try:app.execute("INSERT INTO s_book_memo_main.records(value) VALUES('blocked')")
            except psycopg.Error as error:assert error.sqlstate=='42501'
            else:raise AssertionError('closed_app_write_accepted')
            try:app.execute('SELECT payload FROM ha_effects.private_state')
            except psycopg.Error as error:assert error.sqlstate=='42501'
            else:raise AssertionError('app_read_private_state_accepted')
        return {'status':'PASS','real_postgresql':True,'mid_import_rollback':True,'seven_services':True,
                'portal_blob':True,'private_tokens_separated':True,'closed_app_write_denied':True,
                'source_capture_verified':False,'synchronous_pair_verified':False,'writer_authority_issued':False}


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--postgres-integration':
        print(json.dumps(postgres_integration(sys.argv[2]),sort_keys=True))
    else:unittest.main()
