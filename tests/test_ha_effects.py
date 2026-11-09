"""Stateful transaction/replica/provider doubles; no real PG/network acceptance."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'infra/k8s'))
from ha_runtime import effects as E

BINDING=E.Binding('fixture','123','a'*40,'b'*64,'effect_broker','c'*64)
IDENTITY=E.TrustedIdentity('car-care-worker','A',7)
REQUEST={'operation':'send','request_id':'event-1','payload':{'text':'hello'}}


class Cursor:
    def __init__(self,rows):self.rows=rows
    def fetchone(self):return self.rows[0] if self.rows else None
    def fetchall(self):return self.rows


class Store:
    def __init__(self):
        self.ledger={};self.barriers={};self.replica={};self.replica_barriers={}
        self.events=[];self.commit_count=0;self.fail_commit=None;self.warning_commit=None
        self.commit_lost=False;self.peer_lag=False;self.peer_corrupt=False
        self.primary_patch={};self.peer_patch={};self.before_probe=None;self.probes=0
        self.roles=[(f'app_{i}_{node.lower()}',service,node,7) for i,service in enumerate(sorted(E.SERVICES)) for node in ('A','B')]
        self.unsafe=set();self.connections=[];self.fail_setup=False;self.storage_changes={}

    def connect(self,peer=False):
        connection=Connection(self,peer);self.connections.append(connection);return connection

    def authority(self):
        value={'role':BINDING.broker_role,'binding':E._expected(BINDING),'cluster':'fixture','system_id':'123',
            'primary':'A','partner':'B','node':'A','generation':7,'worker':'A','timeline':3,'actual_timeline':3,
            'recovery':False,'writes_open':True,'history_safe':True,'remaining':4.9,'identity':True,
            'isolation':'read committed','commit':'remote_apply','fsync':'on','full_page_writes':'on',
            'prepared':'0','peer_config':'FIRST1(ha_b)','peer_expected':'FIRST1(ha_b)','peer_sync':True}
        value.update(self.primary_patch);return value


class Connection:
    autocommit=True
    def __init__(self,store,peer):
        self.store=store;self.peer=peer;self.closed=False;self.broken=False
        self.handlers=[];self.snapshot=None;self.pending_events=[]
    def add_notice_handler(self,handler):self.handlers.append(handler)
    def remove_notice_handler(self,handler):self.handlers.remove(handler)
    def close(self):self.closed=True;self.store.events.append(('close',self.peer))
    def cancel(self):self.broken=True
    @contextmanager
    def transaction(self):
        self.execute('BEGIN')
        try:yield
        except Exception:self.execute('ROLLBACK');raise
        else:self.execute('COMMIT')
    def execute(self,sql,args=None):
        if self.broken:raise OSError('fixture_disconnected')
        s=self.store;args=args or ()
        if sql=='BEGIN':self.snapshot=(deepcopy(s.ledger),deepcopy(s.barriers));return Cursor([])
        if sql=='ROLLBACK':
            if self.snapshot is not None:s.ledger,s.barriers=self.snapshot;self.snapshot=None
            return Cursor([])
        if sql=='COMMIT':
            s.commit_count+=1;self.snapshot=None
            if not s.peer_lag:s.replica=deepcopy(s.ledger);s.replica_barriers=deepcopy(s.barriers)
            s.events.append(('commit',deepcopy(s.ledger)))
            if s.warning_commit==s.commit_count:
                for handler in self.handlers:handler(SimpleNamespace(severity_nonlocalized='WARNING'))
            if s.fail_commit==s.commit_count:
                self.broken=True;raise OSError('fixture_commit_ack_lost')
            return Cursor([])
        if sql.startswith('SET '):
            if s.fail_setup:raise OSError('fixture_setup_failed')
            return Cursor([])
        if sql.startswith('SELECT ssl'):return Cursor([(True,)])
        if sql.startswith('SELECT pg_try_advisory_'):return Cursor([(True,)])
        if sql.startswith('SELECT ha_effects.observe'):
            s.probes+=1
            if s.before_probe:s.before_probe(s,s.probes)
            value=s.authority();value['identity']=value['identity'] and args in {('car-care-worker','A',7),getattr(s,'extra_identity',None)}
            return Cursor([(value,)])
        if sql.startswith('SELECT ha_effects.peer_observe'):
            value=s.authority();value.update(node='B' if self.peer else 'A',recovery=self.peer,replay='0/100')
            if self.peer:value.update(s.peer_patch)
            return Cursor([(value,)])
        if sql==E.BINDING_SQL:return Cursor([tuple(E._expected(BINDING).values())])
        if sql.startswith('SELECT cluster,system_id,source_commit,plan_sha256,broker_role FROM ha_effects.binding'):
            return Cursor([tuple(E._expected(BINDING).values())])
        if sql.startswith('SELECT r.rolsuper FROM pg_namespace'):
            return Cursor([(s.storage_changes.get('schema_owner',True),)])
        if sql==E.STORAGE_RELATIONS_SQL:
            tables=set(E.STORAGE_COLUMNS);indices={'binding_pkey','ledger_pkey','barriers_pkey','private_state_pkey','effect_unresolved_resource'}
            rows=[(name,'r' if name in tables else 'i','p',True,False,False,False,False,False,False) for name in sorted(tables|indices)]
            if 'relation' in s.storage_changes:rows[0]=s.storage_changes['relation']
            return Cursor(rows)
        if sql.startswith('/* effects_storage_columns */'):
            return Cursor([(name,*column) for name,columns in E.STORAGE_COLUMNS.items() for column in columns])
        if sql==E.STORAGE_INDEXES_SQL:
            rows=[(name+'_pkey',name,True,True,True,True,True,False,False,'btree',keys,True,True,None,['pg_catalog.int4_ops'] if name=='binding' else ['pg_catalog.text_ops']*len(keys),' '.join(['0']*len(keys))) for name,keys in E.STORAGE_PRIMARY_KEYS.items()]
            rows.append(('effect_unresolved_resource','ledger',True,False,True,True,True,False,False,'btree',['resource'],True,True,"state=ANY(ARRAY['pending','inflight','uncertain'])",['pg_catalog.text_ops'],'0'))
            if 'index' in s.storage_changes:rows[-1]=s.storage_changes['index']
            return Cursor(rows)
        if sql==E.STORAGE_CONSTRAINTS_SQL:
            rows=[(name,'p',True,False,False,keys,None) for name,keys in E.STORAGE_PRIMARY_KEYS.items()]
            rows.extend((name,'c',True,False,False,[],expression) for name,expressions in E.STORAGE_CHECKS.items() for expression in expressions)
            return Cursor(s.storage_changes.get('constraints',rows))
        if sql==E.STORAGE_FUNCTIONS_SQL:
            rows=[(name,args,3802,True,'sql','f',True,'v','u',False,False,False,0,['search_path=pg_catalog,ha,ha_effects,pg_temp'],body) for name,args,body in [('observe','25 25 20',E.observer_body()),('peer_observe','',E.PEER_SQL)]]
            if 'function' in s.storage_changes:rows[0]=s.storage_changes['function']
            return Cursor(rows)
        if sql==E.STORAGE_ACL_SQL:
            permissions={'schema/ha_effects':['USAGE'],'table/binding':['SELECT'],'table/ledger':['SELECT','INSERT','UPDATE'],'table/barriers':['SELECT','INSERT'],'table/private_state':['SELECT','INSERT','UPDATE'],'function/observe':['EXECUTE'],'function/peer_observe':['EXECUTE']}
            rows=[(name,'effect_broker',False,privilege,False) for name,grants in permissions.items() for privilege in grants]
            rows+=s.storage_changes.get('acl',[])
            return Cursor(rows)
        if sql.startswith('/* effects_storage_login_effective */'):
            names=args[1] if isinstance(args[1],list) else [name for name,_,_,_ in s.roles]
            return Cursor([(name,name in s.unsafe) for name in sorted(names)])
        if sql.startswith('SELECT NOT (rolsuper'):return Cursor([(True,)])
        if sql.startswith('SELECT role_name FROM ha.identities'):return Cursor([(name,) for name,_,_,_ in sorted(s.roles)])
        if sql.startswith('SELECT i.role_name'):return Cursor(sorted(s.roles))
        if sql.startswith('SELECT epoch'):return Cursor([(7,)])
        if sql.startswith('SELECT r.rolname,'):return Cursor([(name,name in s.unsafe) for name in sorted(args[1])])
        if sql.startswith('SELECT pg_current_wal_insert'):return Cursor([('0/100',)])
        if sql.startswith('INSERT INTO ha_effects.barriers'):
            s.barriers[args[0]]=tuple(args[1:]);return Cursor([])
        if sql.startswith('SELECT service,request_id,phase,generation FROM ha_effects.barriers'):
            row=(s.replica_barriers if self.peer else s.barriers).get(args[0]);return Cursor([row] if row else [])
        rows=s.replica if self.peer else s.ledger
        if sql.startswith('SELECT payload_sha256,resource,operation,state,provider_ack,result'):
            row=rows.get(tuple(args));return Cursor([tuple(row[k] for k in ('hash','resource','operation','state','ack','result'))] if row else [])
        if sql.startswith('SELECT operation,resource,payload_sha256,node,generation,broker_instance,state,provider_ack,result'):
            row=rows.get(tuple(args))
            if row and self.peer and s.peer_corrupt:row=dict(row,hash='0'*64)
            return Cursor([tuple(row[k] for k in ('operation','resource','hash','node','generation','instance','state','ack','result'))] if row else [])
        if sql.startswith('SELECT 1 FROM ha_effects.ledger'):
            resource=args[0] if args else None
            return Cursor([(1,)] if any(r['state']!='completed' and (resource is None or r['resource']==resource) for r in rows.values()) else [])
        if sql.startswith('INSERT INTO ha_effects.ledger'):
            service,request,operation,resource,digest,node,generation,instance=args
            if (service,request) in s.ledger or any(r['resource']==resource and r['state']!='completed' for r in s.ledger.values()):raise ValueError('fixture_unique_violation')
            s.ledger[(service,request)]=dict(operation=operation,resource=resource,hash=digest,node=node,generation=generation,instance=instance,state='pending',ack=None,result=None)
            return Cursor([])
        if sql.startswith('UPDATE ha_effects.ledger'):
            key=tuple(args[-2:]);row=s.ledger.get(key)
            if not row:return Cursor([])
            if "state='completed'" in sql:
                if row['state']!='inflight':return Cursor([])
                row.update(state='completed',ack=args[0],result=json.loads(args[1]));return Cursor([('completed',)])
            if "state='uncertain'" in sql:
                if row['state'] in {'pending','inflight'}:row['state']='uncertain'
                return Cursor([])
            if row['state']!='pending':return Cursor([])
            row['state']='inflight';return Cursor([('inflight',)])
        raise AssertionError('unexpected fixture SQL: '+sql)


class EffectsTests(unittest.TestCase):
    def fixture(self,adapter=None,timeout=1):
        store=Store();calls=[]
        def default(payload,timeout,deadline):
            calls.append((deepcopy(payload),timeout))
            # Both durable replicated states must precede the first callback.
            self.assertEqual(store.ledger[('car-care-worker','event-1')]['state'],'inflight')
            self.assertEqual(store.replica[('car-care-worker','event-1')]['state'],'inflight')
            self.assertEqual([event[1][('car-care-worker','event-1')]['state'] for event in store.events if event[0]=='commit'][:4],['pending','pending','pending','inflight'])
            return E.ProviderAck('provider-123',{'delivered':True})
        operation=E.Operation('car-care-worker','telegram-account',frozenset({'text'}),frozenset({'delivered'}),adapter or default,timeout)
        broker=E.Broker(store.connect,BINDING,{'send':operation,'send_again':operation},peer_factory=lambda:store.connect(True))
        return store,broker,calls

    def test_durable_pending_inflight_before_call_and_atomic_ack_replay(self):
        store,broker,calls=self.fixture()
        result=broker.dispatch(IDENTITY,REQUEST)
        self.assertEqual(result,{'status':'completed','provider_ack':'provider-123','result':{'delivered':True},'replayed':False})
        self.assertEqual(store.ledger,store.replica)
        self.assertEqual(store.ledger[('car-care-worker','event-1')]['state'],'completed')
        self.assertTrue(broker.dispatch(IDENTITY,REQUEST)['replayed']);self.assertEqual(len(calls),1)
        self.assertTrue(all(connection.closed for connection in store.connections))

    def test_identity_and_request_cannot_select_resource_or_transport_principal(self):
        for mutation in ({'resource':'other'},{'service':'portal-web'},{'node':'B'},{'generation':8}):
            store,broker,calls=self.fixture()
            with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST|mutation)
            self.assertEqual(calls,[]);self.assertEqual(store.connections,[])
        for identity in (replace(IDENTITY,node='B'),replace(IDENTITY,generation=8),replace(IDENTITY,service='portal-web')):
            _,broker,calls=self.fixture()
            with self.assertRaises(E.EffectsBlocked):broker.dispatch(identity,REQUEST)
            self.assertEqual(calls,[])

    def test_live_authority_failures_never_call_provider(self):
        for field,value in [('role','app_1_a'),('system_id','999'),('generation',8),('worker','B'),('recovery',True),('actual_timeline',4),('writes_open',False),('history_safe',False),('remaining',0),('remaining',6),('peer_sync',False),('peer_config',''),('fsync','off'),('prepared','1'),('identity',False)]:
            with self.subTest(field=field):
                store,broker,calls=self.fixture();store.primary_patch[field]=value
                with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST)
                self.assertEqual(calls,[])

    def test_pending_or_inflight_lost_commit_ack_never_calls_or_retries(self):
        # Pending commit=1; its barrier=2; post-read=3; inflight commit=4.
        for number in (1,2,3,4):
            store,broker,calls=self.fixture();store.fail_commit=number
            with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST)
            self.assertEqual(calls,[])
            store.fail_commit=None
            for operation in ('send','send_again'):
                with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST|{'operation':operation,'request_id':'other'})
            self.assertEqual(calls,[])

    def test_warning_local_commit_success_is_not_replication_acceptance(self):
        for number in (1,2,4,5):
            store,broker,calls=self.fixture();store.warning_commit=number
            with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST)
            self.assertEqual(calls,[])

    def test_stale_or_wrong_peer_or_nonidentical_receipt_never_calls(self):
        variants=[{'system_id':'999'},{'node':'A'},{'recovery':False},{'generation':6},{'actual_timeline':2},{'replay':'0/FF'}]
        for patch in variants:
            store,broker,calls=self.fixture();store.peer_patch=patch
            with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST)
            self.assertEqual(calls,[])
        for field in ('peer_lag','peer_corrupt'):
            store,broker,calls=self.fixture();setattr(store,field,True)
            with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST)
            self.assertEqual(calls,[])

    def test_fresh_remote_precheck_blocks_lease_expiring_after_inflight(self):
        store,broker,calls=self.fixture()
        def expire(store,count):
            if count>=7:store.primary_patch['remaining']=0
        store.before_probe=expire
        with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST)
        self.assertEqual(calls,[])

    def test_authority_query_latency_cannot_create_a_fresh_lease_budget(self):
        store,broker,calls=self.fixture()
        def delay(store,count):
            store.primary_patch['remaining']=.002
            time.sleep(.005)
        store.before_probe=delay
        with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST)
        self.assertEqual(calls,[])

    def test_unknown_provider_outcome_and_timeout_hold_canonical_resource(self):
        for adapter in (lambda payload,timeout,deadline:None, lambda payload,timeout,deadline:E.ProviderAck('',{})):
            calls=[]
            def fail(payload,timeout,deadline):calls.append(1);return adapter(payload,timeout,deadline)
            store,broker,_=self.fixture(fail)
            with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST)
            with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST|{'operation':'send_again','request_id':'different'})
            self.assertEqual(calls,[1]);self.assertEqual(store.ledger[('car-care-worker','event-1')]['state'],'uncertain')
        gate=threading.Event();calls=[]
        def slow(payload,timeout,deadline):calls.append(1);gate.wait(1);return E.ProviderAck('late',{'delivered':True})
        store,broker,_=self.fixture(slow,.02);start=time.monotonic()
        try:
            with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST)
            self.assertLess(time.monotonic()-start,.5)
            with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST|{'request_id':'later'})
            self.assertEqual(calls,[1])
        finally:gate.set()

    def test_result_secrets_or_bad_ack_cannot_be_returned_or_completed(self):
        for result in ({'delivered':{'refresh_token':'hidden'}},{'delivered':{'oauthToken':'hidden'}},{'delivered':float('nan')},{'unexpected':True}):
            store,broker,_=self.fixture(lambda payload,timeout,deadline:E.ProviderAck('reference',result))
            with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST)
            row=store.ledger[('car-care-worker','event-1')]
            self.assertNotEqual(row['state'],'completed');self.assertIsNone(row['result']);self.assertIsNone(row['ack'])

    def test_completed_commit_ack_loss_and_peer_receipt_loss_never_resend(self):
        store,broker,calls=self.fixture();store.fail_commit=8
        with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST)
        self.assertEqual(len(calls),1)
        store.fail_commit=None
        self.assertTrue(broker.dispatch(IDENTITY,REQUEST)['replayed']);self.assertEqual(len(calls),1)
        with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST|{'payload':{'text':'changed'}})

    def test_gate_requires_fresh_actual_two_node_authority_and_wal(self):
        store,broker,_=self.fixture()
        self.assertFalse(broker.has_unresolved(generation=7,primary_node='A',watermark='0/100'))
        store.peer_patch={'system_id':'other'}
        with self.assertRaises(E.EffectsBlocked):broker.has_unresolved(generation=7,primary_node='A',watermark='0/100')
        store.peer_patch={};store.primary_patch={'recovery':True}
        # Fixture peer_observe uses its actual endpoint recovery bit; test wrong source node.
        store.primary_patch={'node':'B','generation':6}
        with self.assertRaises(E.EffectsBlocked):broker.has_unresolved(generation=7,primary_node='A',watermark='0/100')
        store.primary_patch={}
        with self.assertRaises(E.EffectsBlocked):broker.has_unresolved(generation=7,primary_node='A',watermark='0/101')

    def test_old_generation_roles_retained_but_all_ledger_privileges_checked(self):
        store,broker,calls=self.fixture()
        store.roles += [(name+'_old',service,node,6) for name,service,node,_ in list(store.roles)]
        E.validate_catalog(store.connect(),BINDING)
        store.unsafe.add(store.roles[-1][0])
        with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST)
        self.assertEqual(calls,[])

    def test_live_role_inventory_or_setup_failure_is_closed_and_connection_closed(self):
        store,broker,calls=self.fixture();store.roles.pop()
        with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST)
        self.assertEqual(calls,[])
        store,broker,calls=self.fixture();store.fail_setup=True
        with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST)
        self.assertTrue(store.connections[0].closed);self.assertEqual(calls,[])

    def test_peer_connection_failure_closes_owned_primary_gate_session(self):
        store,broker,_=self.fixture()
        def unavailable():raise OSError('fixture_peer_unavailable')
        broker.peer_factory=unavailable
        with self.assertRaises(E.EffectsBlocked):broker.has_unresolved(generation=7,primary_node='A',watermark='0/100')
        self.assertEqual(len(store.connections),1)
        self.assertTrue(store.connections[0].closed)

    def test_storage_catalog_rejects_owner_unlogged_function_and_public_third_role_grants(self):
        mutations=[{'schema_owner':False},
            {'relation':('barriers','r','u',True,False,False,False,False,False,False)},
            {'acl':[('table/ledger','PUBLIC',False,'UPDATE',False)]},
            {'acl':[('function/observe','outsider',False,'EXECUTE',False)]},
            {'acl':[('column/ledger/state','outsider',False,'UPDATE',False)]},
            {'acl':[('table/ledger','effect_broker',False,'DELETE',False)]},
            {'constraints':[]},
            {'index':('effect_unresolved_resource','ledger',False,False,True,True,True,False,False,'btree',['resource'],True,True,None,['pg_catalog.text_ops'],'0')}]
        for mutation in mutations:
            store,_,_=self.fixture();store.storage_changes=mutation
            with self.assertRaises(E.EffectsBlocked):E.validate_storage_catalog(store.connect())
        store,_,_=self.fixture()
        row=list(store.connect().execute(E.STORAGE_FUNCTIONS_SQL).fetchone());row[-1]='SELECT null::jsonb'
        store.storage_changes={'function':tuple(row)}
        with self.assertRaises(E.EffectsBlocked):E.validate_storage_catalog(store.connect())
        # Storage validation tolerates a restore with no current-generation roles.
        store,_,_=self.fixture();store.roles=[(*row[:3],6) for row in store.roles]
        self.assertTrue(E.validate_storage_catalog(store.connect())['storage_catalog_verified'])
        with self.assertRaises(E.EffectsBlocked):E.validate_catalog(store.connect(),BINDING)

    def test_storage_constraint_normalization_keeps_operator_precedence(self):
        self.assertEqual(E._catalog_expression(" ((state = 'completed'::text) = ((provider_ack IS NOT NULL) AND (result IS NOT NULL))) "), "(state='completed')=((provider_ackISNOTNULL)AND(resultISNOTNULL))")
        self.assertNotEqual(E._catalog_expression("((state='completed')=(provider_ack IS NOT NULL)) AND (result IS NOT NULL)"), E._catalog_expression("(state='completed')=((provider_ack IS NOT NULL) AND (result IS NOT NULL))"))

    def test_catalog_literals_preserve_internal_spaces_and_escaped_quotes(self):
        self.assertNotEqual(E._catalog_expression("state = ANY (ARRAY['pen ding'::text,'inflight'::text,'uncertain'::text])"), "state=ANY(ARRAY['pending','inflight','uncertain'])")
        self.assertEqual(E._catalog_expression(" ('x y'' z'::text = 'x y'' z'::text) "), "'x y'' z'='x y'' z'")

    def test_read_only_diagnostics_do_not_open_peer_or_write_ledger_and_explicit_repeat_is_fresh(self):
        calls=[]
        def adapter(payload,*,timeout,deadline):
            calls.append(payload);return E.ProviderAck('diagnostic',{'delivered':True})
        store,broker,_=self.fixture(adapter)
        broker.operations['send']=replace(broker.operations['send'],read_only=True)
        broker.peer_factory=lambda:(_ for _ in ()).throw(AssertionError('read opened peer'))
        for _ in range(2):self.assertFalse(broker.dispatch(IDENTITY,REQUEST)['replayed'])
        self.assertEqual(len(calls),2);self.assertEqual(store.ledger,{})
        self.assertEqual(store.barriers,{})
        self.assertTrue(all(not connection.peer and connection.closed for connection in store.connections))

    def test_read_only_unknown_result_or_post_call_authority_loss_has_no_durable_blocker(self):
        for mode in ('unknown','authority','invalid'):
            calls=[]
            store,broker,_=self.fixture()
            def adapter(payload,*,timeout,deadline):
                calls.append('read')
                if mode=='authority':store.primary_patch['writes_open']=False
                return None if mode=='unknown' else E.ProviderAck('diagnostic',{'delivered':True} if mode!='invalid' else {'other':True})
            broker.operations['send']=replace(broker.operations['send'],adapter=adapter,read_only=True)
            with self.assertRaisesRegex(E.EffectsBlocked,'external_effect_read_only_unavailable'):broker.dispatch(IDENTITY,REQUEST)
            self.assertEqual(calls,['read']);self.assertEqual(store.ledger,{})
            self.assertEqual(store.barriers,{})

    def test_only_three_exact_public_get_service_pairs_receive_large_result_limit(self):
        body={'body':'x'*70000}
        for name,service in E.PUBLIC_READ_SERVICES.items():
            store,broker,_=self.fixture(lambda payload,**_:E.ProviderAck('public-read',body))
            identity=E.TrustedIdentity(service,'A',7);store.extra_identity=(service,'A',7)
            operation=E.Operation(service,'public-read',frozenset({'text'}),frozenset({'body'}),lambda payload,**_:E.ProviderAck('public-read',body),read_only=True)
            broker.operations={name:operation}
            result=broker.dispatch(identity,REQUEST|{'operation':name})
            self.assertEqual(result['result'],body);self.assertEqual(store.ledger,{})
            for wrong in (replace(operation,service='car-care-worker'),replace(operation,read_only=False)):
                self.assertEqual(E._result_limit(name,wrong),65536)
            self.assertEqual(E._result_limit(name+'.extra',operation),65536)
        with self.assertRaises(E.EffectsBlocked):E.canonical(body)
        self.assertEqual(len(E.canonical(body,limit=E.READ_RESULT_BYTES)),70011)

    def test_large_public_get_result_has_exact_bound_and_request_and_mutating_results_stay_small(self):
        limit=E.READ_RESULT_BYTES
        # JSON bytes include the complete result object, not only its body.
        self.assertEqual(len(E.canonical({'body':'x'*(limit-11)},limit=limit)),limit)
        with self.assertRaises(E.EffectsBlocked):E.canonical({'body':'x'*(limit-10)},limit=limit)
        for readonly,name,size in ((True,'book.http.get',limit),(True,'other.read',70000),(False,'book.http.get',70000)):
            store,broker,_=self.fixture(lambda payload,**_:E.ProviderAck('oversized',{'body':'x'*size}))
            service='book-memo' if name=='book.http.get' else 'car-care-worker'
            store.extra_identity=(service,'A',7)
            operation=replace(broker.operations['send'],service=service,result_keys=frozenset({'body'}),read_only=readonly)
            broker.operations={name:operation}
            with self.assertRaises(E.EffectsBlocked):broker.dispatch(E.TrustedIdentity(service,'A',7),REQUEST|{'operation':name})
            self.assertEqual(store.barriers=={},readonly)
            if readonly:self.assertEqual(store.ledger,{})
            else:self.assertEqual(store.ledger[(service,'event-1')]['state'],'uncertain')
        calls=[];store,broker,_=self.fixture(lambda payload,**_:calls.append('forbidden'))
        broker.operations['send']=replace(broker.operations['send'],read_only=True)
        with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST|{'payload':{'text':'x'*70000}})
        self.assertEqual(calls,[]);self.assertEqual(store.connections,[])

    def test_read_only_policy_flag_is_strict_and_cannot_come_from_request(self):
        store,broker,_=self.fixture()
        with self.assertRaises(E.EffectsBlocked):replace(broker.operations['send'],read_only=1).validate()
        broker.operations['send']=replace(broker.operations['send'],read_only=True)
        with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST|{'read_only':True})
        self.assertEqual(store.connections,[])

    def test_reserved_executor_inspect_slots_are_independent_bounded_and_released_only_on_thread_exit(self):
        import threading
        ordinary_release=threading.Event();internal_release=threading.Event()
        finished=threading.Event();guard=threading.Lock();counts={'ordinary':0,'internal':0,'finished':0}
        mode=['fast']
        def ordinary(payload,*,timeout,deadline):
            with guard:counts['ordinary']+=1
            ordinary_release.wait(2)
            with guard:
                counts['finished']+=1
                if counts['finished']==6:finished.set()
            return E.ProviderAck('ordinary',{'delivered':True})
        def internal(payload,*,timeout,deadline):
            with guard:counts['internal']+=1
            if mode[0]=='hold':
                internal_release.wait(2)
                with guard:
                    counts['finished']+=1
                    if counts['finished']==6:finished.set()
            return E.ProviderAck('internal',{'delivered':True})
        store,broker,_=self.fixture(ordinary)
        broker.operations['send']=replace(broker.operations['send'],read_only=True,timeout=.02)
        key='executor.docker.inspect.book-memo'
        operation=E.Operation('homeops-executor','fixed-docker',frozenset({'text'}),frozenset({'delivered'}),internal,.02,True)
        broker.operations[key]=operation
        identity=E.TrustedIdentity('homeops-executor','A',7);store.extra_identity=('homeops-executor','A',7)
        internal_request=REQUEST|{'operation':key}
        try:
            for _ in range(4):
                with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST)
            # A normal internal read still completes when every outer slot is held.
            self.assertEqual(broker.dispatch(identity,internal_request)['provider_ack'],'internal')
            # Merely naming an ordinary action 'executor' is insufficient.
            broker.operations['executor.other']=operation
            with self.assertRaises(E.EffectsBlocked):broker.dispatch(identity,REQUEST|{'operation':'executor.other'})
            broker.operations['executor.docker.inspect.spoof']=replace(operation,service='car-care-worker')
            with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST|{'operation':'executor.docker.inspect.spoof'})
            mode[0]='hold'
            for _ in range(3):
                with self.assertRaises(E.EffectsBlocked):broker.dispatch(identity,internal_request)
            self.assertEqual(counts['ordinary'],4);self.assertEqual(counts['internal'],3)
            self.assertEqual(store.ledger,{});self.assertEqual(store.barriers,{})
        finally:
            ordinary_release.set();internal_release.set()
        self.assertTrue(finished.wait(1))
        mode[0]='fast';broker.operations[key]=replace(operation,timeout=1)
        self.assertEqual(broker.dispatch(identity,internal_request)['provider_ack'],'internal')

    def test_read_only_timed_out_threads_keep_four_slot_limit_until_they_exit(self):
        import threading
        release=threading.Event();finished=threading.Event();counts=[0,0]
        guard=threading.Lock()
        def adapter(payload,*,timeout,deadline):
            with guard:counts[0]+=1
            release.wait(1)
            with guard:
                counts[1]+=1
                if counts[1]==4:finished.set()
            return E.ProviderAck('diagnostic',{'delivered':True})
        store,broker,_=self.fixture(adapter)
        broker.operations['send']=replace(broker.operations['send'],read_only=True,timeout=.02)
        try:
            for _ in range(5):
                with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST)
            self.assertEqual(counts[0],4);self.assertEqual(store.ledger,{})
        finally:release.set()
        self.assertTrue(finished.wait(1))
        broker.operations['send']=replace(broker.operations['send'],timeout=1)
        self.assertEqual(broker.dispatch(IDENTITY,REQUEST)['status'],'completed')

    def test_installer_rejects_nonadmin_or_open_authority_without_creating_schema(self):
        for responses in ([(False,)],[(True,),(True,)]):
            connection=MagicMock();connection.execute.return_value.fetchone.side_effect=responses
            with self.assertRaises(E.EffectsBlocked):E.install(connection,BINDING)
            self.assertFalse(any('CREATE SCHEMA' in call.args[0] for call in connection.execute.call_args_list))


if __name__=='__main__':unittest.main()
