#!/usr/bin/env python3
"""Owned local PG17 effect-ledger acceptance; provider/network ownership excluded.

Mac: --host creates a new run.Fixture, seeds all seven real app namespaces and
runs this file as its unprivileged Linux runner. Linux: no --host. External
providers are counters only. No production connections, secrets or HA cutover.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import threading
import time
import traceback
import uuid

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'infra/k8s'))
from ha_runtime import effects, enforcement, backup
from ha_runtime.config import SERVICES, DatabaseUnavailable

PHASE='preflight'


def need(value):
    if not value:raise AssertionError('isolated_effects_check_failed')


def connect(endpoint):
    import psycopg
    return psycopg.connect(**endpoint,autocommit=True,connect_timeout=3)


def lease(admin,opened=True):
    with admin.transaction():
        admin.execute('SELECT pg_advisory_xact_lock(%s)',(effects.ROLE_LOCK,))
        admin.execute("UPDATE ha.control SET serving=ARRAY['A'],worker='A',writes_open=%s,history_safe=true,expires_at=clock_timestamp()+(%s * interval '1 second') WHERE singleton=1",(opened,4 if opened else -1))


def blocked(function):
    try:function()
    except effects.EffectsBlocked:return
    raise AssertionError('effect_should_have_been_blocked')


def pg_denied(endpoint,sql):
    import psycopg
    with connect(endpoint) as connection:
        try:connection.execute(sql)
        except psycopg.Error as error:
            need(error.sqlstate=='42501');return
    raise AssertionError('raw_effect_ledger_access_admitted')


def controller_checks(admin, peer, binding, *, clear):
    """Actual PG controller component only; no native fence/budget claim."""
    from types import SimpleNamespace
    from ha_control.pg import PgBackend
    from ha_control.controller import TransitionBlocked
    from ha_control.prepare import EFFECTS_SOURCES,effects_runtime_sha256
    code_hash=effects_runtime_sha256({name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in EFFECTS_SOURCES})
    backend=object.__new__(PgBackend)
    backend.plan={'cluster':binding.cluster,'system_id':binding.system_id,'source_commit':binding.source_commit,
                  'pg_enforcement':{'effects':{'broker_role':binding.broker_role,'code_sha256':code_hash}}}
    backend.fence_adapter=SimpleNamespace(plan_sha256=binding.plan_sha256)
    for connection in (admin,peer):backend._effects_observation(connection)
    watermark=PgBackend._lsn(admin.execute('SELECT pg_current_wal_insert_lsn()::text').fetchone()[0])
    with admin.transaction():
        admin.execute("SET LOCAL synchronous_commit='remote_apply'")
        admin.execute('SELECT pg_advisory_xact_lock(%s)',(effects.ROLE_LOCK,))
        admin.execute('INSERT INTO ha_effects.barriers VALUES(%s,%s,%s,%s,%s)',(uuid.uuid4().hex,'car-care-worker','controller','component-sync',1))
    before_control=admin.execute('SELECT to_jsonb(c) FROM ha.control c').fetchone()[0]
    before_rows=admin.execute('SELECT to_jsonb(t) FROM ha_effects.ledger t ORDER BY service,request_id').fetchall()
    def denied_gate():
        try:backend._effects_open_gate(admin,peer,watermark)
        except TransitionBlocked as error:
            need(str(error)=='external_effect_controller_unresolved');return
        raise AssertionError('controller_unresolved_gate_admitted')
    if clear:
        with admin.transaction():
            admin.execute('SELECT pg_advisory_xact_lock(%s)',(effects.ROLE_LOCK,))
            backend._effects_open_gate(admin,peer,watermark)
        for state in ('pending','inflight','uncertain'):
            with admin.transaction(force_rollback=True):
                admin.execute('SELECT pg_advisory_xact_lock(%s)',(effects.ROLE_LOCK,))
                admin.execute('INSERT INTO ha_effects.ledger(service,request_id,operation,resource,payload_sha256,node,generation,broker_instance,state) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                              ('car-care-worker','controller-probe','probe','controller-probe','a'*64,'A',1,binding.broker_instance,state))
                denied_gate()
        mutations=["UPDATE ha_effects.binding SET cluster='different'", "UPDATE ha_effects.binding SET system_id='999'",
                   "UPDATE ha_effects.binding SET source_commit=repeat('b',40)", "UPDATE ha_effects.binding SET plan_sha256=repeat('c',64)",
                   "UPDATE ha_effects.binding SET broker_role='missing_broker'", "ALTER TABLE ha_effects.ledger RENAME TO ledger_hidden"]
        for statement in mutations:
            with admin.transaction(force_rollback=True):
                admin.execute('SELECT pg_advisory_xact_lock(%s)',(effects.ROLE_LOCK,));admin.execute(statement)
                try:backend._effects_observation(admin)
                except TransitionBlocked:pass
                else:raise AssertionError('controller_binding_or_catalog_mutation_admitted')
            backend._effects_observation(admin)
    else:
        with admin.transaction():
            admin.execute('SELECT pg_advisory_xact_lock(%s)',(effects.ROLE_LOCK,));denied_gate()
        need(peer.execute("SELECT count(*) FROM ha_effects.ledger WHERE state IN ('pending','inflight','uncertain')").fetchone()[0]>=1)
    need(admin.execute('SELECT to_jsonb(c) FROM ha.control c').fetchone()[0]==before_control)
    need(admin.execute('SELECT to_jsonb(t) FROM ha_effects.ledger t ORDER BY service,request_id').fetchall()==before_rows)
    return {'both_node_catalog_and_binding_observed':True,'completed_only_gate_pass':clear,
            'primary_transient_three_unresolved_states_denied':3 if clear else 0,
            'binding_five_fields_and_missing_ledger_denied':6 if clear else 0,
            'durable_unresolved_both_nodes_gate_denied':not clear,'lease_and_ledger_unchanged':True,
            'scope':'local_pg_controller_component','transitive_effect_code_sha256':code_hash,'native_fence_and_full_budget_verified':False}


def readonly_checks(admin, primary, binding, identity):
    """TCB diagnostic doubles only; successful/lost reads never mutate state."""
    before=effects._content_fingerprints(admin);calls=[];mode=['ok'];done=threading.Event()
    def read(payload,*,timeout,deadline):
        current=mode[0];calls.append(current)
        try:
            need(0<timeout<=1 and time.monotonic()<deadline)
            if current in {'timeout','expiry'}:
                done.wait(.3)
            if current=='unknown':return None
            if current=='private':
                blocked(lambda:broker.credential_backend().read_state('car-care-worker','hyundai_tokens',{}))
            return effects.ProviderAck('diagnostic-read',{'observed':True})
        finally:
            if current in {'timeout','expiry'}:done.set()
    operation=effects.Operation('car-care-worker','fixed-read-diagnostic',frozenset(),frozenset({'observed'}),read,1,True)
    def forbidden_peer():raise AssertionError('readonly_opened_peer')
    broker=effects.Broker(lambda:connect(primary),binding,{'diagnostic':operation},peer_factory=forbidden_peer)
    request={'operation':'diagnostic','request_id':'read-only-same-id','payload':{}}
    for _ in range(2):
        lease(admin);need(broker.dispatch(identity,request)['status']=='completed')
    mode[0]='unknown'
    for _ in range(2):
        lease(admin);blocked(lambda:broker.dispatch(identity,request))
    mode[0]='timeout';done.clear()
    broker.operations['diagnostic']=effects.Operation('car-care-worker',operation.resource,frozenset(),operation.result_keys,read,.05,True)
    lease(admin);blocked(lambda:broker.dispatch(identity,request));need(done.wait(1))
    mode[0]='expiry';done.clear();broker.operations['diagnostic']=operation
    lease(admin)
    with admin.transaction():
        admin.execute('SELECT pg_advisory_xact_lock(%s)',(effects.ROLE_LOCK,))
        admin.execute("UPDATE ha.control SET expires_at=clock_timestamp()+interval '0.15 seconds' WHERE singleton=1")
    blocked(lambda:broker.dispatch(identity,request));need(done.wait(1))
    mode[0]='private';lease(admin)
    need(broker.dispatch(identity,request)['status']=='completed')
    attempted=len(calls);lease(admin)
    admin.execute('GRANT SELECT ON ha_effects.private_state TO effects_fixture_outsider')
    try:blocked(lambda:broker.dispatch(identity,request))
    finally:admin.execute('REVOKE SELECT ON ha_effects.private_state FROM effects_fixture_outsider')
    need(len(calls)==attempted and effects._content_fingerprints(admin)==before)
    need(effects.validate_storage_catalog(admin)['storage_catalog_verified'])
    return {'fresh_repeat_reads_completed':2,'unknown_responses_no_automatic_retry':2,
            'deadline_and_lease_expiry_denied':2,'private_credential_context_denied':True,
            'catalog_privilege_escape_denied_before_adapter':True,'ledger_barriers_private_content_unchanged':True,
            'peer_connections':0,'actual_external_requests':0,'scope':'trusted_read_only_diagnostic_double'}


def public_get_size_checks(admin, primary, peer, binding):
    """Result-only named policy; request and mutating result bounds unchanged."""
    before=effects._content_fingerprints(admin);policy_calls=[]
    for name,service in effects.PUBLIC_READ_SERVICES.items():
        def adapter(payload,*,timeout,deadline):
            policy_calls.append('read');return effects.ProviderAck('public-get-read',{'body':'x'*70000})
        operation=effects.Operation(service,'public-size-read',frozenset({'value'}),frozenset({'body'}),adapter,read_only=True)
        broker=effects.Broker(lambda:connect(primary),binding,{name:operation},peer_factory=lambda:connect(peer))
        identity=effects.TrustedIdentity(service,'A',1)
        request={'operation':name,'request_id':'public-get-size-read','payload':{'value':''}}
        lease(admin);need(len(broker.dispatch(identity,request)['result']['body'])==70000)
    def exact(payload,*,timeout,deadline):return effects.ProviderAck('exact-public-read',{'body':'x'*(effects.READ_RESULT_BYTES-11)})
    operation=effects.Operation('book-memo','public-size-read',frozenset({'value'}),frozenset({'body'}),exact,read_only=True)
    identity=effects.TrustedIdentity('book-memo','A',1)
    broker=effects.Broker(lambda:connect(primary),binding,{'book.http.get':operation},peer_factory=lambda:connect(peer))
    request={'operation':'book.http.get','request_id':'public-get-boundary','payload':{'value':''}}
    lease(admin);accepted=broker.dispatch(identity,request)
    need(len(effects.canonical(accepted['result'],limit=effects.READ_RESULT_BYTES))==effects.READ_RESULT_BYTES)
    def oversize(payload,*,timeout,deadline):return effects.ProviderAck('too-large',{'body':'x'*effects.READ_RESULT_BYTES})
    broker.operations['book.http.get']=effects.Operation('book-memo',operation.resource,operation.payload_keys,operation.result_keys,oversize,read_only=True)
    lease(admin);blocked(lambda:broker.dispatch(identity,request))
    # Request bytes stay at 64 KiB even for an eligible public GET result.
    lease(admin);blocked(lambda:broker.dispatch(identity,request|{'payload':{'value':'x'*70000}}))
    def medium(payload,*,timeout,deadline):return effects.ProviderAck('medium',{'body':'x'*70000})
    broker.operations={'other.read':effects.Operation('book-memo',operation.resource,operation.payload_keys,operation.result_keys,medium,read_only=True)}
    lease(admin);blocked(lambda:broker.dispatch(identity,request|{'operation':'other.read'}))
    need(effects._content_fingerprints(admin)==before)
    # A real mutating invocation with an oversized acknowledgement must remain
    # uncertain. Never erase it to make a later open-gate test pass.
    mutating_calls=[]
    def large_write(payload,*,timeout,deadline):
        mutating_calls.append('write-double');return effects.ProviderAck('large-write',{'body':'x'*70000})
    broker.operations={'book.http.get':effects.Operation('book-memo','http-size-write-fixture',operation.payload_keys,operation.result_keys,large_write)}
    lease(admin);blocked(lambda:broker.dispatch(identity,request|{'request_id':'large-mutating-result'}))
    need(mutating_calls==['write-double'])
    need(admin.execute("SELECT state FROM ha_effects.ledger WHERE service='book-memo' AND request_id='large-mutating-result'").fetchone()==('uncertain',))
    need(effects._content_fingerprints(admin)['private_state']==before['private_state'])
    return {'three_exact_public_get_pairs_large_result_pass':len(policy_calls),'exact_three_mib_result_pass':True,
            'over_three_mib_and_large_request_and_other_read_denied':3,
            'readonly_ledger_barrier_private_hash_unchanged':True,'large_mutating_result_denied_and_uncertain_preserved':True,
            'mutating_size_provider_double_calls':len(mutating_calls),'private_hash_unchanged':True,'actual_external_requests':0}


def linux_main():
    global PHASE
    need(sys.platform=='linux' and os.geteuid()!=0 and os.getenv('APP_ENV')=='test' and os.getenv('HA_LOCAL_RUNTIME')=='1')
    endpoint=json.loads(Path('/fixture/admin.json').read_bytes())
    need(endpoint['host']=='ha-a' and endpoint['dbname']=='ha' and endpoint['sslmode']=='verify-full' and endpoint['sslrootcert']=='/fixture/ca.crt')
    results={};calls=[]
    with connect(endpoint) as admin:
        PHASE='closed_install'
        lease(admin,False)
        schemas=admin.execute("SELECT nspname FROM pg_namespace WHERE left(nspname,2)='s_' ORDER BY nspname").fetchall()
        entries=[]
        for (schema,) in schemas:
            service=next((name for name in SERVICES if schema.startswith('s_'+name.replace('-','_')+'_')),None)
            need(service is not None)
            entries.append((schema,service,[service.replace('-','_')+'_'+node for node in ('a','b')]))
        need({entry[1] for entry in entries}==SERVICES)
        enforcement.install_namespaces(admin,entries)
        from psycopg import sql
        password=secrets.token_urlsafe(32)
        admin.execute(sql.SQL('CREATE ROLE effect_broker LOGIN NOINHERIT PASSWORD {}').format(sql.Literal(password)))
        pgpass=Path('/fixture/effect-broker.pgpass')
        pgpass.write_text('ha-a:5432:ha:effect_broker:'+password+'\nha-b:5432:ha:effect_broker:'+password+'\n');pgpass.chmod(0o600)
        primary=dict(endpoint,user='effect_broker',passfile=str(pgpass))
        peer=dict(primary,host='ha-b')
        cluster,system_id=admin.execute('SELECT c.cluster,(SELECT system_identifier::text FROM pg_control_system()) FROM ha.control c WHERE singleton=1').fetchone()
        source_sha=hashlib.sha256(Path(effects.__file__).read_bytes()).hexdigest()
        binding=effects.Binding(cluster,system_id,'a'*40,source_sha,'effect_broker',uuid.uuid4().hex+uuid.uuid4().hex)
        installed=effects.install(admin,binding)
        need(installed['installed'] and not installed['provider_called'] and not installed['network_enforced'])
        results['real_install_closed_authority']=True
        PHASE='storage_catalog_negative_boundaries'
        need(effects.validate_storage_catalog(admin)['storage_catalog_verified'])
        admin.execute('CREATE ROLE effects_fixture_outsider LOGIN NOINHERIT')
        class RollbackProbe(Exception):pass
        mutations=[
            'GRANT USAGE ON SCHEMA ha_effects TO PUBLIC',
            'GRANT UPDATE ON ha_effects.ledger TO effects_fixture_outsider',
            'GRANT UPDATE(state) ON ha_effects.ledger TO effects_fixture_outsider',
            'GRANT EXECUTE ON FUNCTION ha_effects.peer_observe() TO effects_fixture_outsider',
            'GRANT pg_write_all_data TO effects_fixture_outsider',
            'GRANT pg_read_all_data TO effects_fixture_outsider',
            'DROP INDEX ha_effects.effect_unresolved_resource; CREATE INDEX effect_unresolved_resource ON ha_effects.ledger(resource)',
            'ALTER TABLE ha_effects.ledger DROP CONSTRAINT ledger_pkey; CREATE INDEX ledger_pkey ON ha_effects.ledger(service,request_id)',
            'ALTER TABLE ha_effects.ledger DROP CONSTRAINT ledger_state_check',
            "ALTER TABLE ha_effects.ledger DROP CONSTRAINT ledger_check; ALTER TABLE ha_effects.ledger ADD CHECK (((state='completed')=(provider_ack IS NOT NULL)) AND result IS NOT NULL)",
            "DROP INDEX ha_effects.effect_unresolved_resource; CREATE UNIQUE INDEX effect_unresolved_resource ON ha_effects.ledger(resource) WHERE state='pending'",
            "DROP INDEX ha_effects.effect_unresolved_resource; CREATE UNIQUE INDEX effect_unresolved_resource ON ha_effects.ledger(resource) WHERE state IN ('pen ding','inflight','uncertain')",
            'ALTER TABLE ha_effects.ledger OWNER TO effects_fixture_outsider',
            'ALTER TABLE ha_effects.ledger SET UNLOGGED',
            'ALTER FUNCTION ha_effects.peer_observe() SET search_path=public',
            "CREATE OR REPLACE FUNCTION ha_effects.peer_observe() RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,ha,ha_effects,pg_temp AS $$SELECT NULL::jsonb$$",
            'CREATE TABLE ha_effects.extra_object(id integer)']
        for number,statement in enumerate(mutations,1):
            PHASE='storage_catalog_negative_'+str(number)
            try:
                with admin.transaction():
                    admin.execute(statement)
                    blocked(lambda:effects.validate_storage_catalog(admin))
                    raise RollbackProbe()
            except RollbackProbe:pass
            need(effects.validate_storage_catalog(admin)['storage_catalog_verified'])
        results['actual_storage_owner_logged_function_public_column_and_third_role_negative_checks']=len(mutations)
        PHASE='app_role_privilege_denial'
        for service in sorted(SERVICES):
            for node in ('A','B'):
                app=json.loads(Path('/fixture/config/'+service+'-'+node+'.json').read_bytes())['primary']
                for statement in ('SELECT * FROM ha_effects.ledger',
                    "INSERT INTO ha_effects.ledger(service) VALUES('x')",
                    "UPDATE ha_effects.ledger SET state='completed'",
                    'SELECT * FROM ha_effects.binding','SELECT * FROM ha_effects.barriers',
                    'SELECT payload FROM ha_effects.private_state',
                    "UPDATE ha_effects.private_state SET revision=revision+1",
                    "SELECT ha_effects.observe('car-care-worker','A',1)",'SELECT ha_effects.peer_observe()'):
                    pg_denied(app,statement)
        results['fourteen_app_roles_ledger_and_observers_denied']=True
        identity=effects.TrustedIdentity('car-care-worker','A',1)
        def adapter(payload,*,timeout,deadline):
            need(0<timeout<=2 and time.monotonic()<deadline)
            # Verify an actual durable peer row BEFORE observing the provider.
            with connect(peer) as client:
                row=client.execute('SELECT state,payload_sha256 FROM ha_effects.ledger WHERE service=%s AND request_id=%s',('car-care-worker',payload['event'])).fetchone()
                need(row is not None and row[0]=='inflight')
            private=broker.credential_backend()
            with private.worker_scope('car-care-worker'):
                with private.state_transaction('car-care-worker','hyundai_tokens',{}) as state:
                    state['refresh_token']='fixture-private-initial'
                    state['refresh_uncertain']=True
                    need(private.read_state('car-care-worker','hyundai_tokens',{})==state)
                with connect(peer) as client:
                    revision,digest=client.execute("SELECT revision,payload_sha256 FROM ha_effects.private_state WHERE service='car-care-worker' AND key='hyundai_tokens'").fetchone()
                    need(revision==1 and digest==hashlib.sha256(effects.canonical({'refresh_token':'fixture-private-initial','refresh_uncertain':True})).hexdigest())
                calls.append(payload['event'])
                with private.state_transaction('car-care-worker','hyundai_tokens',{}) as state:
                    need(state.get('refresh_uncertain') is True)
                    state['refresh_token']='fixture-private-rotated';state.pop('refresh_uncertain')
            return effects.ProviderAck('fixture-ack-'+payload['event'],{'accepted':True})
        def unknown(payload,*,timeout,deadline):
            need(time.monotonic()<deadline);calls.append('unknown');return None
        policy=dict(service='car-care-worker',resource='fixed-provider-account',payload_keys=frozenset({'event'}),result_keys=frozenset({'accepted'}))
        broker=effects.Broker(lambda:connect(primary),binding,{'send':effects.Operation(**policy,adapter=adapter),'unknown':effects.Operation(**policy,adapter=unknown)},peer_factory=lambda:connect(peer))
        request={'operation':'send','request_id':'confirmed','payload':{'event':'confirmed'}}
        PHASE='real_dispatch_and_replay'
        lease(admin)
        accepted=broker.dispatch(identity,request)
        need(accepted=={'status':'completed','provider_ack':'fixture-ack-confirmed','result':{'accepted':True},'replayed':False})
        lease(admin)
        need(broker.dispatch(identity,request)['replayed'] is True and calls==['confirmed'])
        results['real_pending_inflight_peer_wal_and_completed_replay']=True
        results['private_refresh_fence_commit_peer_hash_revision_and_rotation']=True
        need('fixture-private' not in json.dumps(accepted))
        PHASE='read_only_diagnostic_boundaries'
        results['read_only_diagnostic_boundaries']=readonly_checks(admin,primary,binding,identity)
        PHASE='actual_gate'
        lease(admin,False)
        with admin.transaction():
            admin.execute('SELECT pg_advisory_xact_lock(%s)',(effects.ROLE_LOCK,))
            watermark=admin.execute('SELECT pg_current_wal_flush_lsn()::text').fetchone()[0]
            need(broker.has_unresolved(generation=1,primary_node='A',watermark=watermark) is False)
        results['fresh_two_node_gate_closed_lease_clear']=True
        with connect(dict(endpoint,host='ha-b')) as peer_admin:
            results['controller_completed_gate_and_mutation_checks']=controller_checks(admin,peer_admin,binding,clear=True)
        PHASE='uncertain_resource_never_retried'
        lease(admin)
        blocked(lambda:broker.dispatch(identity,{'operation':'unknown','request_id':'ambiguous','payload':{'event':'ambiguous'}}))
        lease(admin)
        blocked(lambda:broker.dispatch(identity,{'operation':'send','request_id':'new-id','payload':{'event':'new-id'}}))
        # New broker instance does not erase inflight/uncertain ownership.
        restarted=effects.Broker(lambda:connect(primary),binding,broker.operations,peer_factory=lambda:connect(peer))
        lease(admin)
        blocked(lambda:restarted.dispatch(identity,{'operation':'send','request_id':'restart-id','payload':{'event':'restart-id'}}))
        need(calls==['confirmed','unknown'])
        row=admin.execute("SELECT state,provider_ack,result FROM ha_effects.ledger WHERE request_id='ambiguous'").fetchone()
        need(row==('uncertain',None,None))
        lease(admin,False)
        with admin.transaction():
            admin.execute('SELECT pg_advisory_xact_lock(%s)',(effects.ROLE_LOCK,))
            watermark=admin.execute('SELECT pg_current_wal_flush_lsn()::text').fetchone()[0]
            need(broker.has_unresolved(generation=1,primary_node='A',watermark=watermark) is True)
        results['uncertain_resource_blocks_other_operation_id_and_restart']=True
        PHASE='closed_and_stale_identity'
        blocked(lambda:broker.dispatch(identity,request))
        lease(admin)
        blocked(lambda:broker.dispatch(effects.TrustedIdentity('car-care-worker','B',1),request))
        blocked(lambda:broker.dispatch(effects.TrustedIdentity('car-care-worker','A',2),request))
        need(calls==['confirmed','unknown'])
        results['closed_authority_stale_node_generation_no_provider_call']=True
        PHASE='public_get_size_boundaries'
        results['public_get_size_boundaries']=public_get_size_checks(admin,primary,peer,binding)
        PHASE='private_uncertainty_backup_seed'
        def fenced(payload,*,timeout,deadline):
            private=fenced_broker.credential_backend()
            with private.state_transaction('car-care-worker','hyundai_tokens',{}) as state:
                state['refresh_uncertain']=True
            return None
        fenced_broker=effects.Broker(lambda:connect(primary),binding,{'fence':effects.Operation('car-care-worker','oauth-private-fence',frozenset(),frozenset(),fenced)},peer_factory=lambda:connect(peer))
        lease(admin)
        blocked(lambda:fenced_broker.dispatch(identity,{'operation':'fence','request_id':'private-fence','payload':{}}))
        lease(admin,False)
        with connect(dict(endpoint,host='ha-b')) as peer_admin:
            results['controller_durable_unresolved_gate']=controller_checks(admin,peer_admin,binding,clear=False)
        # Import existing synthetic immutable objects only; all seven source
        # namespaces and original filesystem objects remain intact.
        from ha_runtime.blobs import CHUNK_BYTES
        references=backup.object_references(admin)
        for identifier,size in references.items():
            raw=backup._blob(Path('/fixture/objects'),identifier,size)
            with admin.transaction():
                admin.execute('INSERT INTO s_portal_web_state.ha_blob_objects VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(identifier,size,(size+CHUNK_BYTES-1)//CHUNK_BYTES))
                for ordinal,start in enumerate(range(0,size,CHUNK_BYTES)):
                    admin.execute('INSERT INTO s_portal_web_state.ha_blob_chunks VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(identifier,ordinal,raw[start:start+CHUNK_BYTES]))
        private=admin.execute("SELECT revision,payload_sha256 FROM ha_effects.private_state WHERE service='car-care-worker' AND key='hyundai_tokens'").fetchone()
        unresolved=admin.execute("SELECT service,request_id,state,payload_sha256 FROM ha_effects.ledger WHERE state<>'completed' ORDER BY service,request_id").fetchall()
        need(len(unresolved)>=2 and private[0]==3)
        metadata={'binding':binding.__dict__,'private_revision_hash':list(private),'unresolved':unresolved,'cluster':cluster}
        backup._write_new('/fixture/results/effects-source.json',json.dumps(metadata,sort_keys=True).encode())
    PHASE='encrypted_whole_backup'
    artifact='/fixture/results/effects-whole.aesgcm';key='/fixture/results/effects-whole.key'
    backup._write_new(key,secrets.token_bytes(32))
    archived=backup.create_backup(endpoint,'/fixture/objects',artifact,key,expected_cluster=cluster)
    need(archived['pass'])
    manifest,_,_=backup._unpack(backup._open(Path(artifact).read_bytes(),backup._key(key)))
    need(manifest['required_broker_roles'][0]['name']=='effect_broker' and manifest['blob_backend']=='postgresql')
    need(set(name for name in manifest['logical']['tables'] if name.startswith('ha_effects.'))=={'ha_effects.binding','ha_effects.ledger','ha_effects.barriers','ha_effects.private_state'})
    need(b'fixture-private' not in Path(artifact).read_bytes())
    results['encrypted_whole_backup_includes_four_effect_tables_and_private_uncertainty']=True
    return {'status':'PASS' ,'scope':'owned_local_actual_pg17_effects_with_provider_double',
        'results':results,'provider_double_calls':len(calls)+results['public_get_size_boundaries']['mutating_size_provider_double_calls'],'actual_external_requests':0,
        'production_access':False,'native_windows_acceptance':False,'sole_egress_or_credential_ownership':False,
        'backup_restore':False,'ready':False}


def restore_main():
    global PHASE
    need(sys.platform=='linux' and os.geteuid()!=0 and os.getenv('APP_ENV')=='test' and os.getenv('HA_LOCAL_RUNTIME')=='1')
    from psycopg import sql
    endpoint=json.loads(Path('/fixture/admin.json').read_bytes())
    backup._guard(endpoint)
    source=json.loads(Path('/fixture/results/effects-source.json').read_bytes())
    artifact='/fixture/results/effects-whole.aesgcm';key='/fixture/results/effects-whole.key'
    objects=Path('/fixture/results/effects-restored-objects');objects.mkdir(mode=0o700)
    def restore():return backup.restore_backup(endpoint,str(objects),artifact,key,expected_cluster=source['cluster'])
    def denied_restore():
        try:restore()
        except DatabaseUnavailable as error:
            need(str(error)=='restore_broker_role_preprovision_required');return
        raise AssertionError('restore_broker_role_gate_admitted')
    PHASE='restore_preprovision_app_roles'
    with connect(endpoint) as admin:
        for service in sorted(SERVICES):
            for node in ('a','b'):
                name=service.replace('-','_')+'_'+node
                password=Path('/fixture/'+name+'.pgpass').read_text().splitlines()[0].split(':',4)[4]
                admin.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(name),sql.Literal(password)))
        password=Path('/fixture/repl.pgpass').read_text().splitlines()[0].split(':',4)[4]
        admin.execute(sql.SQL('CREATE ROLE ha_repl LOGIN REPLICATION PASSWORD {}').format(sql.Literal(password)))
    PHASE='restore_missing_broker_role_negative'
    denied_restore()
    with connect(endpoint) as admin:
        password=Path('/fixture/effect-broker.pgpass').read_text().splitlines()[0].split(':',4)[4]
        admin.execute(sql.SQL('CREATE ROLE effect_broker LOGIN NOINHERIT CREATEROLE PASSWORD {}').format(sql.Literal(password)))
    PHASE='restore_overprivileged_broker_role_negative'
    denied_restore()
    with connect(endpoint) as admin:
        admin.execute('ALTER ROLE effect_broker NOCREATEROLE')
    PHASE='encrypted_whole_fresh_system_restore'
    restored=restore();need(restored['pass'] and restored['authority_closed'] and restored['old_generation_invalidated'])
    manifest,_,_=backup._unpack(backup._open(Path(artifact).read_bytes(),backup._key(key)))
    with connect(endpoint) as admin:
        need(effects.validate_storage_catalog(admin)['storage_catalog_verified'])
        system=admin.execute('SELECT system_identifier::text FROM pg_control_system()').fetchone()[0]
        need(system!=source['binding']['system_id'])
        binding=admin.execute('SELECT cluster,system_id,source_commit,plan_sha256,broker_role FROM ha_effects.binding').fetchone()
        old=effects.Binding(**source['binding'])
        need(binding==tuple(effects._expected(old).values()))
        control=admin.execute('SELECT epoch,writes_open,history_safe,expires_at<clock_timestamp() FROM ha.control').fetchone()
        need(control==(manifest['epoch']+1,False,False,True))
        unresolved=admin.execute("SELECT service,request_id,state,payload_sha256 FROM ha_effects.ledger WHERE state<>'completed' ORDER BY service,request_id").fetchall()
        need([list(row) for row in unresolved]==source['unresolved'])
        private=admin.execute("SELECT revision,payload_sha256 FROM ha_effects.private_state WHERE service='car-care-worker' AND key='hyundai_tokens'").fetchone()
        need(list(private)==source['private_revision_hash'])
        need(admin.execute("SELECT payload->'refresh_uncertain' FROM ha_effects.private_state WHERE service='car-care-worker' AND key='hyundai_tokens'").fetchone()==(True,))
        logical=backup.logical_manifest(admin)
        for name,value in manifest['logical']['tables'].items():
            if name.startswith('ha_effects.'):
                need(logical['tables'][name]==value)
    PHASE='restore_old_live_binding_dispatch_negative'
    primary=dict(endpoint,user='effect_broker',passfile='/fixture/effect-broker.pgpass')
    calls=[]
    def forbidden(payload,*,timeout,deadline):calls.append('forbidden');return effects.ProviderAck('forbidden',{})
    operation=effects.Operation('car-care-worker','fixed-provider-account',frozenset(),frozenset(),forbidden)
    # Both connections intentionally target the restored primary. Rejection
    # must occur at the old authority/binding, before any peer proof or adapter.
    broker=effects.Broker(lambda:connect(primary),old,{'send':operation},peer_factory=lambda:connect(primary))
    identity=effects.TrustedIdentity('car-care-worker','A',manifest['epoch'])
    blocked(lambda:broker.dispatch(identity,{'operation':'send','request_id':'after-restore','payload':{}}))
    need(calls==[])
    with connect(endpoint) as admin:
        need(admin.execute("SELECT count(*) FROM ha_effects.ledger WHERE request_id='after-restore'").fetchone()==(0,))
        need([list(row) for row in admin.execute("SELECT service,request_id,state,payload_sha256 FROM ha_effects.ledger WHERE state<>'completed' ORDER BY service,request_id").fetchall()]==source['unresolved'])
    PHASE='restored_tcb_metadata_rebind'
    new=effects.Binding(old.cluster,system,'d'*40,'e'*64,'effect_broker_recovered',uuid.uuid4().hex+uuid.uuid4().hex)
    with connect(endpoint) as admin:
        fingerprint=effects._content_fingerprints(admin)
        control=admin.execute('SELECT to_jsonb(c) FROM ha.control c').fetchone()
        # Missing, same-identity, and overprivileged targets must never mutate.
        blocked(lambda:effects.rebind_restored(admin,old,new))
        blocked(lambda:effects.rebind_restored(admin,old,effects.Binding(old.cluster,system,new.source_commit,new.plan_sha256,old.broker_role,new.broker_instance)))
        password=secrets.token_urlsafe(32)
        admin.execute(sql.SQL('CREATE ROLE effect_broker_recovered LOGIN NOINHERIT CREATEROLE PASSWORD {}').format(sql.Literal(password)))
        blocked(lambda:effects.rebind_restored(admin,old,new))
        admin.execute('ALTER ROLE effect_broker_recovered NOCREATEROLE')
        with admin.transaction(force_rollback=True):
            admin.execute("UPDATE ha.control SET writes_open=true WHERE singleton=1")
            blocked(lambda:effects.rebind_restored(admin,old,new))
        need(effects._content_fingerprints(admin)==fingerprint and admin.execute('SELECT to_jsonb(c) FROM ha.control c').fetchone()==control)
        rebound=effects.rebind_restored(admin,old,new)
        need(rebound['rebound'] and not rebound['activation_verified'] and not rebound['external_fence_verified'] and not rebound['roles_or_passwords_created'])
        need(effects._content_fingerprints(admin)==fingerprint and admin.execute('SELECT to_jsonb(c) FROM ha.control c').fetchone()==control)
        need(admin.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname='effect_broker'").fetchone()==(False,))
        need(admin.execute(effects.BINDING_SQL).fetchone()==tuple(effects._expected(new).values()))
        # Existing installer is reused with fourteen fresh fixture-only roles.
        # Reinstallation changes policy ownership metadata, never service rows.
        before_services={name:(value['rows'],value['sha256']) for name,value in backup.logical_manifest(admin)['tables'].items() if name.startswith('s_')}
        entries=[]
        for service in sorted(SERVICES):
            for node in ('A','B'):
                name=service.replace('-','_')+'_recovered_'+node.lower()
                admin.execute(sql.SQL('CREATE ROLE {} LOGIN').format(sql.Identifier(name)))
                admin.execute('INSERT INTO ha.identities VALUES(%s,%s,%s)',(name,service,node))
        for (schema,) in admin.execute("SELECT nspname FROM pg_namespace WHERE left(nspname,2)='s_' ORDER BY nspname").fetchall():
            service=next(name for name in SERVICES if schema.startswith('s_'+name.replace('-','_')+'_'))
            entries.append((schema,service,[service.replace('-','_')+'_recovered_'+node for node in ('a','b')]))
        generation=admin.execute('SELECT epoch FROM ha.control').fetchone()[0]
        enforcement.install_namespaces(admin,entries,generation=generation)
        after_services={name:(value['rows'],value['sha256']) for name,value in backup.logical_manifest(admin)['tables'].items() if name.startswith('s_')}
        need(after_services==before_services and effects._content_fingerprints(admin)==fingerprint)
        effects.validate_catalog(admin,new)
    backup._write_new('/fixture/results/effects-rebound.json',json.dumps({'binding':new.__dict__,'content_fingerprints':fingerprint,'generation':generation,'unresolved_count':len(unresolved)},sort_keys=True).encode())
    return {'status':'PASS','scope':'whole_encrypted_four_effect_tables_fresh_system_restore','results':{
        'required_broker_missing_and_overprivileged_roles_denied':2,'seven_service_full_logical_restore':True,
        'four_effect_table_logical_hashes_preserved':True,'private_revision_hash_and_refresh_uncertainty_preserved':True,
        'old_binding_preserved_new_system_epoch_advanced_closed':True,'old_live_dispatch_provider_calls':0,
        'old_uncertain_rows_preserved':len(unresolved),
        'rebind_four_invalid_target_or_authority_cases_denied':4,'rebind_old_role_disabled_all_content_preserved':True,
        'fourteen_fresh_fixture_roles_seven_service_rows_preserved':True,'rebind_external_fence_or_activation_issued':False},'actual_external_requests':0,'production_access':False,
        'native_windows_acceptance':False,'activation_verified':False,'ready':False}


def post_rebind_main():
    global PHASE
    PHASE='rebound_fresh_pair_unresolved_open_gate'
    need(sys.platform=='linux' and os.geteuid()!=0 and os.getenv('APP_ENV')=='test' and os.getenv('HA_LOCAL_RUNTIME')=='1')
    endpoint=json.loads(Path('/fixture/admin.json').read_bytes())
    snapshot=json.loads(Path('/fixture/results/effects-rebound.json').read_bytes())
    binding=effects.Binding(**snapshot['binding'])
    with connect(endpoint) as admin,connect(dict(endpoint,host='ha-b')) as peer:
        need({name:list(value) for name,value in effects._content_fingerprints(admin).items()}==snapshot['content_fingerprints'])
        result=controller_checks(admin,peer,binding,clear=False)
        # controller_checks writes one non-secret proof barrier; all pending
        # ownership rows/private revisions remain exactly as before the gate.
        need(admin.execute("SELECT count(*) FROM ha_effects.ledger WHERE state<>'completed'").fetchone()==(snapshot['unresolved_count'],))
        need(admin.execute('SELECT writes_open,history_safe,expires_at<clock_timestamp() FROM ha.control').fetchone()==(False,False,True))
    return {'status':'PASS','scope':'rebound_local_pg_component_only','results':result,
            'new_binding_unresolved_open_denied':True,'lease_open_updates':0,'provider_calls':0,
            'actual_external_requests':0,'production_access':False,'native_windows_acceptance':False,
            'positive_old_vm_fence_verified':False,'activation_verified':False,'ready':False}


def host_main(output, docker_context="orbstack"):
    spec=importlib.util.spec_from_file_location('effect_fixture_driver',Path(__file__).with_name('run.py'))
    driver=importlib.util.module_from_spec(spec);sys.modules[spec.name]=driver;spec.loader.exec_module(driver)
    directory=Path(tempfile.mkdtemp(prefix='n100-effects-')).resolve();directory.chmod(0o700)
    fixture=driver.Fixture(directory,runtime_context=docker_context)
    names=['infra/k8s/ha-lab/run.py','infra/k8s/ha-lab/effects_smoke.py','infra/k8s/ha_runtime/effects.py',
           'infra/k8s/ha_control/pg.py','infra/k8s/ha_control/prepare.py','infra/k8s/ha_control/cli.py','infra/k8s/ha_control/privilege_closure.py',
           'infra/k8s/ha_runtime/backup.py','infra/k8s/ha_runtime/config.py',
           'infra/k8s/ha_runtime/schema.sql','infra/k8s/ha_runtime/enforcement.sql','infra/k8s/ha_runtime/enforcement.py']
    from ha_control.prepare import EFFECTS_SOURCES
    names=list(dict.fromkeys([*names,*EFFECTS_SOURCES]))
    pins={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names}
    report={'status':'RUNNING','owner':fixture.owner,'label':fixture.label,'directory':str(directory),
            'scope':'new_owned_local_pg17_effect_fixture','source_files':pins,'production_access':False,
            'runtime_context':docker_context,'runtime_identity_verified':False}
    output=Path(output) if output else directory/'summary.json'
    owner_directory=output.parent/('effects-owner-'+fixture.owner)
    owner_directory.mkdir(mode=0o700)
    owner_sequence=0
    runtime_identity=None
    def private_json(path,value):
        with tempfile.NamedTemporaryFile('w',dir=path.parent,prefix='.effects-state-',delete=False) as stream:
            temporary=Path(stream.name)
            json.dump(value,stream,sort_keys=True,indent=2);stream.write('\n')
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    def save():
        nonlocal owner_sequence
        report['owned_resources']={key:getattr(fixture,key,None) for key in ('network','a','b','runners')}
        owner={'owner':fixture.owner,'label':fixture.label,'directory':str(directory),
               'runtime_context':docker_context,'runtime_identity':runtime_identity,
               'source_files':pins,'resources':report['owned_resources'],
               'phase':fixture.phase,'sequence':owner_sequence}
        private_json(directory/'owner.json',owner)
        private_json(owner_directory/'owner.json',owner)
        private_json(owner_directory/('%03d-owner.json'%owner_sequence),owner)
        owner_sequence+=1
        report['durable_owner_directory']=str(owner_directory)
        private_json(output,report)
    save();print(json.dumps({'status':'STARTING','owner':fixture.owner,'source_files':pins,'summary':str(output)}),flush=True)
    error=None
    try:
        runtime_identity=fixture.validate_runtime()
        need(type(runtime_identity) is dict and bool(runtime_identity))
        report['runtime_identity_verified']=True
        report['runtime_identity_sha256']=hashlib.sha256(json.dumps(runtime_identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        save()  # Runtime identity is pinned before the first resource mutation.
        fixture.prepare();save()
        fixture.databases();save()
        fixture.build()
        report['built_files']={name:hashlib.sha256((directory/'context/source'/name).read_bytes()).hexdigest() for name in names}
        need(report['built_files']==pins);save()
        fixture.phase='seven_app_migrations'
        for service in driver.SERVICES:
            fixture.execute(service,'A','seed')
            save()
        fixture.phase='fixture_synthetic_portal_objects_transfer'
        donor=fixture.runners['311'];runner=fixture.runners['312']
        fixture.owned('container',donor);fixture.owned('container',runner)
        transfer=directory/'synthetic-portal-objects';transfer.mkdir(mode=0o700)
        fixture.docker('cp',donor+':/fixture/objects/.',str(transfer))
        fixture.docker('cp',str(transfer)+'/.',runner+':/fixture/objects')
        fixture.docker('exec','--user','0:0',runner,'chown','-R','10001:10001','/fixture/objects')
        fixture.phase='effects_smoke'
        runner=fixture.runners['312'];fixture.owned('container',runner)
        raw=fixture.docker('exec','-e','APP_ENV=test','-e','HA_LOCAL_RUNTIME=1','-e','PYTHONPATH=/source/infra/k8s',runner,'python','/source/infra/k8s/ha-lab/effects_smoke.py',timeout=90)
        result=json.loads(raw.splitlines()[-1]);need(result['status']=='PASS');report['acceptance']=result
        report['source_owned_resources']={key:getattr(fixture,key,None) for key in ('network','a','b','runners')};save()
        fixture.phase='fresh_restore_system'
        previous=fixture.system_id
        for node in (fixture.b,fixture.a):
            fixture.owned('container',node);fixture.docker('rm','-f','-v',node)
        del fixture.b
        fixture.a=fixture.start_pg('A')
        save()
        fixture.wait(lambda:fixture.docker('exec',fixture.a,'cat','/proc/1/comm')=='postgres' and fixture.psql('SELECT 1','postgres')=='1')
        fixture.psql('CREATE DATABASE ha','postgres')
        need(fixture.psql('SELECT system_identifier::text FROM pg_control_system()')!=previous)
        fixture.phase='four_effect_table_whole_restore'
        raw=fixture.docker('exec','-e','APP_ENV=test','-e','HA_LOCAL_RUNTIME=1','-e','PYTHONPATH=/source/infra/k8s',runner,'python','/source/infra/k8s/ha-lab/effects_smoke.py','--restore',timeout=150)
        result=json.loads(raw.splitlines()[-1]);need(result['status']=='PASS');report['restore_acceptance']=result
        fixture.phase='rebound_fresh_sync_pair'
        fixture.docker('exec',fixture.a,'bash','-ceu',"printf '%s\\n' 'hostssl replication ha_repl all scram-sha-256' >> /var/lib/postgresql/data/pg_hba.conf")
        fixture.psql('SELECT pg_reload_conf()')
        fixture.b=fixture.start_pg('B')
        save()
        fixture.wait(lambda:fixture.docker('exec',fixture.b,'psql','-XAt','-U','postgres','-d','ha','-c','SELECT pg_is_in_recovery()')=='t')
        fixture.psql("ALTER SYSTEM SET synchronous_standby_names='FIRST 1 (ha_b)'")
        fixture.psql('SELECT pg_reload_conf()')
        fixture.wait(lambda:fixture.psql("SELECT count(*) FROM pg_stat_replication WHERE application_name='ha_b' AND sync_state='sync'")=='1')
        fixture.phase='rebound_unresolved_open_gate'
        raw=fixture.docker('exec','-e','APP_ENV=test','-e','HA_LOCAL_RUNTIME=1','-e','PYTHONPATH=/source/infra/k8s',runner,'python','/source/infra/k8s/ha-lab/effects_smoke.py','--post-rebind',timeout=90)
        result=json.loads(raw.splitlines()[-1]);need(result['status']=='PASS');report['rebound_acceptance']=result


    except Exception as failure:
        error=type(failure).__name__
    finally:
        report['owned_resources']={key:getattr(fixture,key,None) for key in ('network','a','b','runners')}
        if getattr(fixture,'image',None):report['pg_image_id']=fixture.image
        report['fixture_report']=fixture.report;report['phase']=fixture.phase
        save()  # Persist exact UUID/IDs before any owned resource is removed.
        report['owned_fixture_removed']=fixture.cleanup()
        report['status']='PASS' if error is None and report['owned_fixture_removed'] else 'FAIL'
        report['error_class']=error;save()
    print(json.dumps(report,sort_keys=True),flush=True)
    return int(report['status']!='PASS')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--host',action='store_true');parser.add_argument('--output');parser.add_argument('--restore',action='store_true');parser.add_argument('--post-rebind',action='store_true');parser.add_argument('--docker-context',default='orbstack')
    args=parser.parse_args()
    if args.host:raise SystemExit(host_main(args.output,args.docker_context))
    try:print(json.dumps(post_rebind_main() if args.post_rebind else (restore_main() if args.restore else linux_main()),sort_keys=True),flush=True)
    except Exception as error:
        code=getattr(error,'sqlstate',None);code=code if isinstance(code,str) and re.fullmatch('[0-9A-Z]{5}',code) else 'fixed_check_failed'
        if isinstance(error,DatabaseUnavailable) and re.fullmatch('[a-z_]{1,100}',str(error)):code=str(error)
        frames=[{'file':Path(frame.filename).name,'function':frame.name,'line':frame.lineno} for frame in traceback.extract_tb(error.__traceback__) if Path(frame.filename).name in {'effects_smoke.py','effects.py','enforcement.py'}]
        print(json.dumps({'status':'FAIL','scope':'owned_local_actual_pg17_effects','error_class':type(error).__name__,
            'runtime_reason':PHASE,'reason':code,'frames':frames,'raw_suppressed':True}),flush=True)
        raise SystemExit(1) from None
