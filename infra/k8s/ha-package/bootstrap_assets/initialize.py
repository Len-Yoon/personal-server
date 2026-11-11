#!/usr/bin/env python3
"""Closed-catalog or frozen seven-service importer in the exact utility image.

Migration requires an externally pinned private frozen manifest and emits no
writer authority. Replication bootstrap is the sole
local-commit phase; catalog installation requires the synchronous standby first.
No password/SQL/provider payload is printed. All role secrets are private input.
"""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys


def need(value):
    if not value:
        raise RuntimeError('closed_bootstrap_rejected')


def private(path):
    path = Path(path)
    info = path.lstat()
    need(path.is_absolute() and stat.S_ISREG(info.st_mode) and info.st_nlink == 1
         and info.st_uid == os.geteuid() and not info.st_mode & 0o077
         and not any(p.is_symlink() for p in (path,*path.parents)))
    return path


def configuration(path):
    value = json.loads(private(path).read_bytes())
    need(type(value) is dict and type(value.get('schema_version')) is int and value['schema_version'] in (1,2))
    fields = {'schema_version','cluster','source_commit','plan_sha256','broker_instance','passwords'}
    need(set(value) == fields | ({'primary_node'} if value['schema_version']==2 else set()))
    need(value.get('primary_node','A') in ('A','B'))
    need(type(value['cluster']) is str and re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}',value['cluster']))
    need(re.fullmatch(r'[0-9a-f]{40}',value['source_commit']))
    need(all(re.fullmatch(r'[0-9a-f]{64}',value[k]) for k in ('plan_sha256','broker_instance')))
    from ha_runtime.config import SERVICES
    roles = {service.replace('-','_')+'_'+node for service in SERVICES for node in ('a','b')}
    need(type(value['passwords']) is dict and set(value['passwords']) == roles|{'ha_repl','ha_effect_broker'})
    need(all(type(secret) is str and re.fullmatch(r'[A-Za-z0-9_-]{32,128}',secret)
             for secret in value['passwords'].values()))
    need(len(set(value['passwords'].values())) == len(value['passwords']))
    return value


def prepare_closed_control(admin,value):
    from psycopg import sql
    from ha_runtime.config import SERVICES
    primary=value.get('primary_node','A');partner='B' if primary=='A' else 'A'
    admin.execute(Path('/opt/ha/ha_runtime/schema.sql').read_text())
    admin.execute('DROP FUNCTION ha.local_roles(text[],text,boolean)')
    admin.execute("INSERT INTO ha.control VALUES(1,%s,1,1,%s,%s,1,ARRAY[%s],%s,'AS',false,false,clock_timestamp()-interval '1 day')",(value['cluster'],primary,partner,primary,primary))
    for service in sorted(SERVICES):
        for node in ('a','b'):
            role=service.replace('-','_')+'_'+node
            admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOINHERIT PASSWORD {}').format(sql.Identifier(role),sql.Literal(value['passwords'][role])))
            admin.execute('INSERT INTO ha.identities VALUES(%s,%s,%s)',(role,service,node.upper()))


def install_guarded_catalog(admin,value,entries):
    from psycopg import sql
    from ha_runtime import enforcement,effects
    enforcement.install_namespaces(admin,entries,generation=1)
    admin.execute(sql.SQL('CREATE ROLE ha_effect_broker LOGIN NOINHERIT PASSWORD {}').format(sql.Literal(value['passwords']['ha_effect_broker'])))
    system_id=admin.execute('SELECT system_identifier::text FROM pg_control_system()').fetchone()[0]
    binding=effects.Binding(value['cluster'],system_id,value['source_commit'],value['plan_sha256'],'ha_effect_broker',value['broker_instance'])
    effects.install(admin,binding)
    effects.validate_catalog(admin,binding)
    need(admin.execute("SELECT NOT writes_open AND NOT history_safe AND expires_at<clock_timestamp() FROM ha.control").fetchone()==(True,))
    need(admin.execute("SELECT to_regprocedure('ha.local_roles(text[],text,boolean)') IS NULL").fetchone()==(True,))
    need(admin.execute('SELECT count(*) FROM ha.identities').fetchone()==(14,))
    need(admin.execute('SELECT count(*) FROM ha.enforced_roles').fetchone()==(14,))
    need(admin.execute('SELECT count(DISTINCT service) FROM ha.enforced_namespaces').fetchone()==(7,))
    need(admin.execute('SELECT count(*) FROM ha_effects.ledger').fetchone()==(0,))
    need(admin.execute('SELECT count(*) FROM ha_effects.private_state').fetchone()==(0,))
    return system_id


def bootstrap(admin, value, phase, *, migration=None):
    from psycopg import sql
    from ha_runtime import enforcement, effects, migrate
    from ha_runtime.config import SERVICES
    primary=value.get('primary_node','A');partner='B' if primary=='A' else 'A'
    need(admin.execute('SELECT pg_is_in_recovery(),current_setting(\'cluster_name\'),session_user').fetchone() == (False,'ha_'+primary,'postgres'))
    need(admin.execute("SELECT current_setting('fsync'),current_setting('full_page_writes'),current_setting('max_prepared_transactions')").fetchone() == ('on','on','0'))
    if phase == 'replication':
        # No app role/schema exists yet. Make one closed initial replication role
        # before a synchronous peer exists; never relax the server-wide setting.
        need(admin.execute("SELECT 1 FROM pg_namespace WHERE nspname IN ('ha','ha_effects')").fetchone() is None)
        need(admin.execute("SELECT 1 FROM pg_roles WHERE rolname='ha_repl'").fetchone() is None)
        need(admin.execute("SELECT 1 FROM pg_database WHERE datname='ha'").fetchone() is None)
        admin.execute("SET synchronous_commit='local'")
        admin.execute(sql.SQL('CREATE ROLE ha_repl LOGIN REPLICATION NOINHERIT PASSWORD {}').format(sql.Literal(value['passwords']['ha_repl'])))
        admin.execute('CREATE DATABASE ha')
        admin.execute("SET synchronous_commit='remote_apply'")
        return {'replication_role_created':True,'catalog_installed':False,'writer_authority_issued':False}
    need(phase in {'catalog','migration'})
    need(admin.info.dbname == 'ha')
    need(admin.execute("SELECT current_setting('synchronous_commit'),regexp_replace(current_setting('synchronous_standby_names'),'[[:space:]\"]','','g')").fetchone() == ('remote_apply','FIRST1(ha_'+partner.lower()+')'))
    need(admin.execute("SELECT count(*) FROM pg_stat_replication WHERE application_name=%s AND state='streaming' AND sync_state='sync'",('ha_'+partner.lower(),)).fetchone() == (1,))
    need(admin.execute("SELECT 1 FROM pg_namespace WHERE nspname IN ('ha','ha_effects') OR left(nspname,2)='s_'").fetchone() is None)
    if phase=='migration':
        from ha_runtime.seven_migration import apply
        need(migration is not None)
        return apply(admin,value,migration['manifest'],migration['sha256'],migration['receipt'],prepare_closed_control,install_guarded_catalog)
    from ha_runtime.seven_migration import CORE_COMPONENTS
    entries=[]
    with admin.transaction():
        prepare_closed_control(admin,value)
        for service in sorted(SERVICES):
            roles=[service.replace('-','_')+'_'+node for node in ('a','b')]
            for component in CORE_COMPONENTS[service]:
                schema=migrate.create_namespace(admin,service,component)
                entries.append((schema,service,roles))
        system_id=install_guarded_catalog(admin,value,entries)
    return {'catalog_installed':True,'guarded_services':7,'app_roles':14,'empty_namespaces':len(entries),
        'effects_closed':True,'provider_credentials_imported':False,'writer_authority_issued':False,
        'full_seven_app_restore_verified':False,'system_id':system_id}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True);parser.add_argument('--phase',choices=('replication','catalog','migration'),required=True)
    parser.add_argument('--migration-manifest');parser.add_argument('--migration-sha256');parser.add_argument('--migration-receipt')
    args = parser.parse_args()
    try:
        import psycopg
        value = configuration(args.config)
        migration=None
        supplied=(args.migration_manifest,args.migration_sha256,args.migration_receipt)
        need(all(supplied) if args.phase=='migration' else not any(supplied))
        if args.phase=='migration':migration={'manifest':args.migration_manifest,'sha256':args.migration_sha256,'receipt':args.migration_receipt}
        # Fixed Unix socket, peer-authenticated postgres. No DSN/host/password input.
        need(os.geteuid() in (70,999))
        with psycopg.connect(host='/var/run/postgresql',dbname='postgres' if args.phase=='replication' else 'ha',
                             user='postgres',autocommit=True,connect_timeout=3,
                             options='-c statement_timeout=10000 -c lock_timeout=3000') as admin:
            result = bootstrap(admin,value,args.phase,migration=migration)
        print(json.dumps({'status':'PASS',**result},sort_keys=True))
        return 0
    except Exception as error:
        from ha_runtime.seven_migration import MigrationRejected
        reason=str(error) if isinstance(error,MigrationRejected) else 'closed_bootstrap_rejected'
        print(json.dumps({'status':'FAIL','reason':reason}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
