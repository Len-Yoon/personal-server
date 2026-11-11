"""Import a hash-bound frozen seven-service snapshot into one fresh closed PG.

This is not a source freezer, legacy fence verifier, credential generator, or
writer activation tool. External operator evidence is bound, never promoted to
live fencing proof. The caller supplies the existing trusted admin connection.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
from datetime import datetime, timezone

from .config import SERVICES, DatabaseUnavailable
from . import blobs, migrate
from .proof import digest

CORE_COMPONENTS = {
    'portal-web': ('homeops','bookmarks','state'), 'book-memo': ('main','state'),
    'youtube-memo': ('main','state'), 'crawler-worker': ('state',),
    'car-care-worker': ('main','state'), 'homeops-executor': ('main','state'),
    'system-agent': ('state',),
}
DATABASES = {'portal-web': {'homeops','bookmarks'}, 'book-memo': {'main'},
             'youtube-memo': {'main'}, 'crawler-worker': {'main'},
             'car-care-worker': {'main'}, 'homeops-executor': {'main'}, 'system-agent': set()}
REQUIRED_DATABASES = {s: (DATABASES[s] if s in {'portal-web','book-memo','youtube-memo','car-care-worker'} else set()) for s in SERVICES}
REQUIRED_STATES = {'portal-web': {'auth_sessions','auth_failures','portfolio','file-namespace'},
                   'book-memo': {'auth-rate-limit','write-sessions'},
                   'youtube-memo': {'auth-rate-limit','write-sessions'},
                   'crawler-worker': {'archive','collection_status'},
                   'car-care-worker': set(), 'homeops-executor': {'restart_claims'}, 'system-agent': set()}
HEX = re.compile(r'[0-9a-f]{64}\Z')
IDENT = re.compile(r'[a-z][a-z0-9_.-]{0,95}\Z')
SECRET_KEYS = {'refresh_token','access_token','client_secret','bot_token','telegram_bot_token','private_key','authorization_state'}


class MigrationRejected(DatabaseUnavailable):
    pass


def need(condition, reason):
    if not condition:
        raise MigrationRejected(reason)


def _stamp(value):
    return (value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns,value.st_mode,value.st_uid,value.st_nlink)


def private_file(path):
    path=Path(path)
    need(path.is_absolute() and '..' not in path.parts,'private_frozen_input_required')
    info=path.lstat()
    need(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid in {0,os.geteuid()}
         and not info.st_mode & 0o077 and not any(p.is_symlink() for p in (path,*path.parents)),
         'private_frozen_input_required')
    return path


def read_verified(path, expected, size, *, load=True):
    path=private_file(path);before=path.lstat()
    need(type(size) is int and 0<=size<=(32*1024*1024 if load else blobs.MAX_NAMESPACE_BYTES)
         and isinstance(expected,str) and HEX.fullmatch(expected) and before.st_size==size,'frozen_artifact_binding_invalid')
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    hashed=hashlib.sha256();payload=bytearray()
    with os.fdopen(descriptor,'rb') as stream:
        need(_stamp(before)==_stamp(os.fstat(stream.fileno())),'frozen_artifact_changed')
        for chunk in iter(lambda:stream.read(65536),b''):
            hashed.update(chunk)
            if load:payload.extend(chunk)
        need(_stamp(before)==_stamp(os.fstat(stream.fileno())),'frozen_artifact_changed')
    need(hashed.hexdigest()==expected and _stamp(before)==_stamp(path.lstat()),'frozen_artifact_changed')
    return bytes(payload) if load else None


def utc(value):
    need(isinstance(value,str) and value.endswith('Z'),'freeze_timestamp_invalid')
    try:result=datetime.fromisoformat(value[:-1]+'+00:00')
    except ValueError:raise MigrationRejected('freeze_timestamp_invalid') from None
    need(result.tzinfo is not None,'freeze_timestamp_invalid')
    return result


def json_payload(raw):
    try:
        value=json.loads(raw)
        json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8')
        def has_nul(item):
            if isinstance(item,str):return '\x00' in item
            if isinstance(item,dict):return any(has_nul(k) or has_nul(v) for k,v in item.items())
            if isinstance(item,list):return any(has_nul(v) for v in item)
            return False
        need(not has_nul(value),'noncanonical_json_artifact')
        return value
    except (ValueError,UnicodeError,RecursionError):
        raise MigrationRejected('noncanonical_json_artifact') from None


def _provider_secret(value):
    if isinstance(value,dict):
        return any(key.casefold() in SECRET_KEYS or _provider_secret(item) for key,item in value.items())
    if isinstance(value,list):return any(_provider_secret(item) for item in value)
    return False


def validate_state(service,key,payload):
    need(type(payload) is dict and not _provider_secret(payload),'app_provider_credential_rejected')
    if service in {'book-memo','youtube-memo'} and key=='write-sessions':
        for token_hash,expiry in payload.items():
            need(HEX.fullmatch(token_hash) and isinstance(expiry,str),'typed_session_artifact_required')
            try:parsed=datetime.fromisoformat(expiry.replace('Z','+00:00'))
            except ValueError:raise MigrationRejected('typed_session_artifact_required') from None
            need(parsed.tzinfo is not None,'typed_session_artifact_required')
    if service=='portal-web' and key=='auth_sessions':
        need(all(type(payload.get(table)) is dict for table in ('sessions','confirmations')),'typed_session_artifact_required')
        for table in ('sessions','confirmations'):
            for token_hash,row in payload[table].items():
                need(HEX.fullmatch(token_hash) and type(row) is dict and isinstance(row.get('scope'),str)
                     and isinstance(row.get('payload_hash'),str) and HEX.fullmatch(row['payload_hash'])
                     and type(row.get('expires_at')) in (int,float),'typed_session_artifact_required')
    if service=='portal-web' and key=='portfolio':
        need(isinstance(payload.get('content'),str),'typed_portfolio_artifact_required')
    if service=='crawler-worker' and key=='archive':
        need(type(payload.get('articles')) is list and all(type(row) is dict for row in payload['articles']),'typed_archive_artifact_required')
    if service=='homeops-executor' and key=='restart_claims':
        for identifier,row in payload.items():
            need(bool(identifier) and type(row) is dict and isinstance(row.get('payload_hash'),str)
                 and HEX.fullmatch(row['payload_hash']) and type(row.get('created_at')) in (int,float)
                 and row.get('state') in {'inflight','completed','expired'} and 'result' in row,'typed_restart_artifact_required')


def _portal_validator(*, source_only_preflight=False):
    installed=Path(__file__).resolve().parents[1]/'_validators/portal_ha_files.py'
    if installed.is_file():source=installed
    else:
        need(source_only_preflight or (os.getenv('APP_ENV')=='test' and os.getenv('HA_LOCAL_RUNTIME')=='1'),'canonical_portal_validator_required')
        source=Path(__file__).resolve().parents[3]/'portal-web/app/services/ha_files.py'
    need(source.is_file() and not source.is_symlink(),'canonical_portal_validator_required')
    spec=importlib.util.spec_from_file_location('_canonical_portal_migration_validator',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


class FrozenSources:
    """Private input holder. Public receipts never include paths or payloads."""
    def __init__(self, path, expected_sha256, config, *, source_only_preflight=False):
        path=private_file(path);root=path.parent;info=root.lstat()
        need(info.st_uid in {0,os.geteuid()} and not info.st_mode & 0o077,'private_frozen_directory_required')
        need(isinstance(expected_sha256,str) and HEX.fullmatch(expected_sha256),'frozen_manifest_hash_mismatch')
        raw=read_verified(path,expected_sha256,path.lstat().st_size)
        if Path(__file__).with_name('release.json').exists():
            from .config import _release
            need(_release()['source_commit']==config['source_commit'],'migration_release_identity_mismatch')
        else:
            need(source_only_preflight or (os.getenv('APP_ENV')=='test' and os.getenv('HA_LOCAL_RUNTIME')=='1'),'packaged_migration_release_required')
        value=json_payload(raw)
        fields={'schema_version','source_commit','cluster','plan_sha256','target_system_id','freeze_receipt','artifacts','services','portal_objects','private_states'}
        need(type(value) is dict and set(value)==fields and type(value['schema_version']) is int and value['schema_version']==1,'frozen_manifest_fields_invalid')
        need(all(value[key]==config[key] for key in ('source_commit','cluster','plan_sha256')),'frozen_manifest_identity_mismatch')
        need(isinstance(value['target_system_id'],str) and re.fullmatch(r'[1-9][0-9]{0,19}',value['target_system_id']),'fresh_system_identity_required')
        self.root=root;self.manifest=value;self.manifest_sha256=expected_sha256
        self._stamps={path:_stamp(path.lstat())};self._used=set()
        self._manifest_path=path;self._manifest_bytes=len(raw)
        need(type(value['artifacts']) is dict and 1<=len(value['artifacts'])<=20000,'frozen_artifacts_required')
        for identifier,entry in value['artifacts'].items():
            need(isinstance(identifier,str) and IDENT.fullmatch(identifier),'frozen_artifact_id_invalid')
            self._entry(entry)
        receipt_entry=value['freeze_receipt'];receipt_raw=self._read_entry(receipt_entry)
        receipt=json_payload(receipt_raw)
        receipt_fields={'schema_version','capture_id','source_runtime_sha256','writer_graph_sha256','writer_fence_receipt_sha256',
                        'writers_closed_at','capture_started_at','capture_finished_at','services','target_system_id','cluster','plan_sha256','artifacts','capture_audit'}
        need(type(receipt) is dict and set(receipt)==receipt_fields and type(receipt['schema_version']) is int and receipt['schema_version']==1,'freeze_receipt_fields_invalid')
        need(isinstance(receipt['capture_id'],str) and re.fullmatch(r'[0-9a-f]{32}',receipt['capture_id'])
             and all(isinstance(receipt[key],str) and HEX.fullmatch(receipt[key]) for key in ('source_runtime_sha256','writer_graph_sha256','writer_fence_receipt_sha256')),'freeze_receipt_evidence_required')
        need(receipt['services']==sorted(SERVICES) and all(receipt[key]==value[key] for key in ('target_system_id','cluster','plan_sha256')),'freeze_receipt_target_mismatch')
        closed,start,end=map(utc,(receipt['writers_closed_at'],receipt['capture_started_at'],receipt['capture_finished_at']))
        need(closed<=start<=end<=datetime.now(timezone.utc),'freeze_capture_order_invalid')
        need(receipt['artifacts']=={key:{'sha256':entry['sha256'],'bytes':entry['bytes']} for key,entry in value['artifacts'].items()},'freeze_artifact_inventory_mismatch')
        self.freeze_receipt_sha256=receipt_entry['sha256']
        audit_entry=receipt['capture_audit'];audit=json_payload(self._read_entry(audit_entry))
        need(type(audit) is dict and type(audit.get('schema_version')) is int and audit['schema_version']==1,
             'capture_audit_required')
        evidence=audit.get('evidence')
        bindings={'runtime':'source_runtime_sha256','writer_graph':'writer_graph_sha256',
                  'writer_closure':'writer_fence_receipt_sha256'}
        need(type(evidence) is dict and set(evidence)==set(bindings)|{'dispositions'},'capture_audit_evidence_required')
        for key,entry in evidence.items():
            self._read_entry(entry,load=False)
            if key in bindings:need(entry['sha256']==receipt[bindings[key]],'capture_audit_evidence_mismatch')
        self.capture_audit_sha256=audit_entry['sha256']
        need(type(value['services']) is dict and set(value['services'])==set(SERVICES),'all_seven_sources_required')
        self.databases={};self.states={}
        for service,entry in value['services'].items():
            need(type(entry) is dict and set(entry)=={'databases','states'},'service_source_fields_invalid')
            databases,states=entry['databases'],entry['states']
            need(type(databases) is dict and REQUIRED_DATABASES[service]<=set(databases)<=DATABASES[service]
                 and type(states) is dict and REQUIRED_STATES[service]<=set(states),'service_source_coverage_incomplete')
            self.databases[service]={component:self.artifact(identifier,sqlite=True) for component,identifier in databases.items()}
            self.states[service]={}
            for key,identifier in states.items():
                need(isinstance(key,str) and 1<=len(key)<=200 and key!='hyundai_tokens','app_private_state_rejected')
                payload=json_payload(self.artifact(identifier))
                validate_state(service,key,payload)
                self.states[service][key]=payload
        private=value['private_states']
        need(type(private) is dict and set(private)=={'car-care-worker'} and type(private['car-care-worker']) is dict
             and set(private['car-care-worker'])=={'hyundai_tokens'},'private_credential_service_binding_required')
        self.private_payload=json_payload(self.artifact(private['car-care-worker']['hyundai_tokens']))
        need(type(self.private_payload) is dict,'private_credential_payload_invalid')
        secrets=[v for k,v in self.private_payload.items() if k.casefold() in SECRET_KEYS and isinstance(v,str) and v]
        for states in self.states.values():
            def exposed(item):
                if isinstance(item,str):return any(secret in item for secret in secrets)
                if isinstance(item,dict):return any(exposed(k) or exposed(v) for k,v in item.items())
                if isinstance(item,list):return any(exposed(v) for v in item)
                return False
            need(not exposed(states),'private_credential_cross_service_copy_rejected')
        for databases in self.databases.values():
            for database in databases.values():
                with sqlite3.connect(database.as_uri()+'?mode=ro&immutable=1',uri=True) as reader:
                    for table, in reader.execute("SELECT name FROM sqlite_master WHERE type='table' AND substr(name,1,7)<>'sqlite_'"):
                        need(re.fullmatch(r'[a-z_][a-z0-9_]*',table),'unsupported_source_table')
                        columns=[row[1] for row in reader.execute('PRAGMA table_info("'+table+'")')]
                        need(not set(columns)&SECRET_KEYS,'private_credential_sqlite_column_rejected')
                        cursor=reader.execute('SELECT * FROM "'+table+'"')
                        while True:
                            rows=cursor.fetchmany(1000)
                            if not rows:break
                            need(not any(any(secret in item for secret in secrets) for row in rows for item in row if isinstance(item,str)),
                                 'private_credential_sqlite_copy_rejected')
        namespace=self.states['portal-web']['file-namespace'];_portal_validator(source_only_preflight=source_only_preflight)._validate(namespace)
        references={}
        groups=[namespace['entries'],*(item['entries'] for item in namespace['trash'].values())]
        for group in groups:
            for entry in group.values():
                if entry['type']=='file':
                    identifier,size=entry['sha256'],entry['size']
                    need(size<=blobs.MAX_BLOB_BYTES and references.get(identifier,size)==size,'portal_blob_reference_invalid')
                    references[identifier]=size
        need(type(value['portal_objects']) is dict and set(value['portal_objects'])==set(references),'portal_blob_inventory_mismatch')
        need(sum(references.values())<=blobs.MAX_NAMESPACE_BYTES,'portal_blob_total_limit')
        self.objects={}
        for identifier,artifact in value['portal_objects'].items():
            need(artifact in value['artifacts'] and value['artifacts'][artifact]['bytes']==references[identifier],'portal_blob_hash_mismatch')
            raw=self.artifact(artifact)
            need(hashlib.sha256(raw).hexdigest()==identifier and len(raw)==references[identifier],'portal_blob_hash_mismatch')
            need(not any(secret.encode() in raw for secret in secrets),'private_credential_blob_copy_rejected')
            self.objects[identifier]=artifact
        need(self._used==set(value['artifacts']),'unconsumed_frozen_artifact')

    def _entry(self,entry):
        need(type(entry) is dict and set(entry)=={'file','sha256','bytes'},'artifact_descriptor_invalid')
        name=entry['file'];need(isinstance(name,str),'artifact_relative_file_required')
        relative=PurePosixPath(name)
        need(not relative.is_absolute() and relative.parts and all(part not in {'.','..'} and not part.startswith('.') for part in relative.parts)
             and '\\' not in name and '\x00' not in name,'artifact_relative_file_required')
        need(isinstance(entry['sha256'],str) and HEX.fullmatch(entry['sha256']) and type(entry['bytes']) is int,'artifact_descriptor_invalid')
        return self.root/name

    def _read_entry(self,entry,*,load=True):
        path=self._entry(entry)
        if path in self._stamps:need(self._stamps[path]==_stamp(path.lstat()),'frozen_source_metadata_changed')
        raw=read_verified(path,entry['sha256'],entry['bytes'],load=load)
        self._stamps.setdefault(path,_stamp(path.lstat()));return raw

    def artifact(self, identifier, *, sqlite=False):
        need(isinstance(identifier,str) and identifier in self.manifest['artifacts'],'unknown_frozen_artifact')
        entry=self.manifest['artifacts'][identifier];raw=self._read_entry(entry,load=not sqlite);self._used.add(identifier)
        if sqlite:
            path=self._entry(entry)
            need(not any(Path(str(path)+suffix).exists() for suffix in ('-wal','-shm','-journal')),'sqlite_snapshot_sidecar_rejected')
            with sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True) as source:
                need(source.execute('PRAGMA quick_check').fetchall()==[('ok',)] and not source.execute('PRAGMA foreign_key_check').fetchall(),'source_integrity_failed')
                need(source.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view')").fetchone() is None,'unsupported_source_objects')
                need(bool(migrate._source_tables(source)),'empty_sqlite_artifact_rejected')
                for sql, in source.execute("SELECT sql FROM sqlite_master WHERE type IN ('table','index') AND sql IS NOT NULL AND substr(name,1,7)<>'sqlite_'"):
                    migrate._safe_source_sql(sql)
            return path
        return raw

    def unchanged(self):
        for path,stamp in self._stamps.items():need(_stamp(path.lstat())==stamp,'frozen_source_metadata_changed')
        read_verified(self._manifest_path,self.manifest_sha256,self._manifest_bytes)
        for identifier in self._used:self._read_entry(self.manifest['artifacts'][identifier],load=False)


def fresh_target(admin, source, primary):
    need(getattr(admin.info,'dbname',None)=='ha','fresh_target_database_mismatch')
    row=admin.execute("SELECT pg_is_in_recovery(),current_setting('cluster_name'),system_identifier::text FROM pg_control_system()").fetchone()
    need(row==(False,'ha_'+primary,source.manifest['target_system_id']),'fresh_target_identity_mismatch')
    need(admin.execute("SELECT rolsuper FROM pg_roles WHERE rolname=session_user").fetchone()==(True,),'trusted_migration_admin_required')
    need(admin.execute("SELECT r.rolsuper FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba WHERE d.datname=current_database()").fetchone()==(True,),'trusted_target_database_owner_required')
    need(admin.execute('SELECT count(*) FROM pg_default_acl').fetchone()==(0,),'fresh_empty_target_required')
    need(admin.execute("SELECT count(*) FROM pg_namespace WHERE nspname NOT IN ('public','pg_catalog','pg_toast','information_schema')").fetchone()==(0,),'fresh_empty_target_required')
    for catalog,column in (('pg_class','relnamespace'),('pg_proc','pronamespace'),('pg_type','typnamespace')):
        need(admin.execute('SELECT count(*) FROM pg_catalog.'+catalog+' c JOIN pg_namespace n ON n.oid=c.'+column+" WHERE n.nspname NOT IN ('pg_catalog','pg_toast','information_schema')").fetchone()==(0,),'fresh_empty_target_required')
    for catalog in ('pg_largeobject_metadata','pg_foreign_server','pg_user_mapping','pg_foreign_data_wrapper',
                    'pg_event_trigger','pg_publication','pg_subscription','pg_replication_origin','pg_parameter_acl'):
        need(admin.execute('SELECT count(*) FROM pg_catalog.'+catalog).fetchone()==(0,),'fresh_empty_target_required')
    roles=[service.replace('-','_')+'_'+node for service in SERVICES for node in ('a','b')]+['ha_effect_broker']
    need(admin.execute('SELECT count(*) FROM pg_catalog.pg_roles WHERE rolname=ANY(%s)',(roles,)).fetchone()==(0,),'preexisting_migration_role_rejected')


def import_data(admin, source):
    """Called only inside the one outer closed-control transaction."""
    entries=[];receipts={}
    for service in sorted(SERVICES):
        converted={};manifests=[]
        for component,path in source.databases[service].items():
            manifests.append(migrate.copy_sqlite(admin,service,component,path));converted[component]=True
        for component in CORE_COMPONENTS[service]:
            if component not in converted:migrate.create_namespace(admin,service,component);converted[component]=True
        for key,payload in source.states[service].items():migrate.seed_state(admin,service,key,payload)
        roles=[service.replace('-','_')+'_'+node for node in ('a','b')]
        entries += [('s_'+service.replace('-','_')+'_'+component,service,roles) for component in converted]
        receipts[service]={'databases':manifests,'state_sha256':digest(source.states[service]),'state_keys':len(source.states[service])}
    for identifier,artifact in source.objects.items():
        payload=source.artifact(artifact);size=len(payload)
        admin.execute('INSERT INTO s_portal_web_state.ha_blob_objects VALUES(%s,%s,%s)',(identifier,size,(size+blobs.CHUNK_BYTES-1)//blobs.CHUNK_BYTES))
        for ordinal,start in enumerate(range(0,size,blobs.CHUNK_BYTES)):
            admin.execute('INSERT INTO s_portal_web_state.ha_blob_chunks VALUES(%s,%s,%s)',(identifier,ordinal,payload[start:start+blobs.CHUNK_BYTES]))
    return entries,receipts


def finalize_data(admin, source):
    from psycopg.types.json import Jsonb
    from .effects import canonical
    payload=source.private_payload
    admin.execute('INSERT INTO ha_effects.private_state VALUES(%s,%s,%s,1,%s)',
        ('car-care-worker','hyundai_tokens',Jsonb(payload),hashlib.sha256(canonical(payload)).hexdigest()))
    private=admin.execute("SELECT payload,revision,payload_sha256 FROM ha_effects.private_state WHERE service='car-care-worker' AND key='hyundai_tokens'").fetchone()
    need(private is not None and canonical(private[0])==canonical(payload) and private[1]==1
         and private[2]==hashlib.sha256(canonical(payload)).hexdigest(),'private_state_migration_mismatch')
    for service,states in source.states.items():
        rows=admin.execute('SELECT key,payload FROM '+ 's_'+service.replace('-','_')+'_state.shared_state').fetchall()
        need(digest(dict(rows))==digest(states),'state_logical_migration_mismatch')
    from .backup import _pg_blob
    for identifier,artifact in source.objects.items():
        entry=source.manifest['artifacts'][artifact]
        need(hashlib.sha256(_pg_blob(admin,identifier,entry['bytes'])).hexdigest()==identifier,'portal_blob_restore_mismatch')
    source.unchanged()
    need(admin.execute('SELECT NOT writes_open AND NOT history_safe AND expires_at<clock_timestamp() FROM ha.control').fetchone()==(True,),'migration_authority_must_remain_closed')


def write_receipt(path,value):
    path=Path(path);need(path.is_absolute() and '..' not in path.parts and not path.exists()
        and not any(p.is_symlink() for p in (path,*path.parents)),'new_migration_receipt_required')
    info=path.parent.lstat();need(info.st_uid in {0,os.geteuid()} and not info.st_mode&0o077,'private_receipt_directory_required')
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(descriptor,'w') as stream:
        json.dump(value,stream,sort_keys=True);stream.write('\n');stream.flush();os.fsync(stream.fileno())


def apply(admin,config,manifest_path,expected_sha256,receipt_path,prepare_control,install_guard):
    """No target cleanup, automatic retry, provider call, role opening or fallback."""
    # A nested psycopg transaction is only a savepoint; its successful exit must
    # never be reported as a committed migration. libpq IDLE has value zero.
    need(admin.autocommit is True and admin.info.transaction_status==0,
         'idle_autocommit_migration_connection_required')
    source=FrozenSources(manifest_path,expected_sha256,config)
    primary=config.get('primary_node','A');fresh_target(admin,source,primary)
    # Validate output before the transaction, retaining an initial receipt even
    # when the import fails. Each state is a new leaf; nothing overwrites history.
    base=Path(receipt_path)
    need(not any(p.exists() or p.is_symlink() for p in (base,base.with_suffix(base.suffix+'.prepared'),base.with_suffix(base.suffix+'.failed'))),'new_migration_receipt_required')
    common={'schema_version':1,'source_commit':config['source_commit'],'manifest_sha256':expected_sha256,
            'freeze_receipt_sha256':source.freeze_receipt_sha256,'services':sorted(SERVICES),
            'capture_audit_sha256':source.capture_audit_sha256,
            'target_system_id_sha256':hashlib.sha256(source.manifest['target_system_id'].encode()).hexdigest(),'primary_node':primary,
            'source_freeze_evidence_bound':True,'source_writer_fencing_verified_by_this_tool':False,
            'legacy_to_typed_artifact_normalization_verified_by_this_tool':False,
            'writer_authority_issued':False,'automatic_enable_performed':False,'external_effect_reconciliation_required':True}
    write_receipt(base.with_suffix(base.suffix+'.prepared'),dict(common,status='PREPARED'))
    commit_pending=False;stage='transaction_begin'
    try:
        with admin.transaction():
            admin.execute("SET LOCAL log_min_messages='panic'")
            admin.execute("SET LOCAL log_min_error_statement='panic'")
            admin.execute('SET LOCAL log_parameter_max_length_on_error=0')
            admin.execute('SELECT pg_advisory_xact_lock(%s)',(7142026110601,))
            fresh_target(admin,source,primary);stage='closed_control';prepare_control(admin,config)
            stage='frozen_data';entries,data=import_data(admin,source)
            stage='guard_effects'
            admin.execute('SET LOCAL search_path TO pg_catalog')
            install_guard(admin,config,entries)
            stage='logical_verification';finalize_data(admin,source)
            commit_pending=True;stage='commit'
        receipt=dict(common,status='PASS',data=data,portal_objects=len(source.objects),provider_private_state_imported=True,static_provider_configuration_imported=False)
        write_receipt(base,receipt);return receipt
    except Exception as error:
        failure=dict(common,status='UNKNOWN_COMMIT' if commit_pending else 'FAILED',error_type=type(error).__name__,stage=stage)
        code=getattr(error,'sqlstate',None)
        if isinstance(code,str) and re.fullmatch(r'[0-9A-Z]{5}',code):failure['sqlstate']=code
        write_receipt(base.with_suffix(base.suffix+'.failed'),failure)
        raise MigrationRejected('migration_commit_unknown' if commit_pending else 'frozen_seven_migration_failed') from None
