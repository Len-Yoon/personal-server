"""Owned PG fixture phases for seven enforced apps and a fresh-system restore.

No production entrypoint: endpoint/UID/environment checks precede any database
work. Generated credentials belong only to the disposable fixture.
"""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ha_runtime import backup, enforcement
from ha_runtime.config import SERVICES, DatabaseUnavailable
from enforcement_smoke import connection, need, LOCK


def fresh_password(role):
    return hmac.new(Path('/fixture/postgres-password').read_bytes(),
                    ('enforced-next:' + role).encode(), hashlib.sha256).hexdigest()


def configure(fresh=False, recovered=False):
    for service in SERVICES:
        for node in ('A', 'B'):
            path = Path('/fixture/config') / (service + '-' + node + '.json')
            value = json.loads(path.read_text())
            value.update(profile='enforced-vm-v1', enforcement_sha256=enforcement.definition_sha256())
            if fresh or recovered:
                role = service.replace('-', '_') + ('_recovered_' if recovered else '_next_') + node.lower()
                passfile = Path('/fixture') / (role + '.pgpass')
                passfile.write_text(''.join(host + ':5432:ha:' + role + ':' + fresh_password(role) + '\n'
                                            for host in ('ha-a', 'ha-b')))
                passfile.chmod(0o600)
                for endpoint in ('primary', 'partner'):
                    value[endpoint].update(user=role, passfile=str(passfile))
            if recovered:
                with connection(json.loads(Path('/fixture/admin.json').read_text())) as admin:
                    value['system_id']=admin.execute('SELECT system_identifier::text FROM pg_control_system()').fetchone()[0]
            path.write_text(json.dumps(value)); path.chmod(0o600)


def schemas(admin):
    result = []
    for schema, in admin.execute("SELECT nspname FROM pg_namespace WHERE left(nspname,2)='s_' ORDER BY nspname"):
        service = next((s for s in SERVICES if schema.startswith('s_' + s.replace('-', '_') + '_')), None)
        need(service is not None)
        result.append((schema, service))
    need(set(service for _, service in result) == set(SERVICES))
    return result


def authority(admin, node=None):
    with admin.transaction():
        admin.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK,))
        admin.execute("UPDATE ha.control SET serving=ARRAY[%s],worker=%s,writes_open=%s,history_safe=%s,expires_at=clock_timestamp()+(%s*interval '1 second') WHERE singleton=1",
                      (node or 'A', node or 'A', bool(node), bool(node), 4 if node else -1))


def install(admin):
    authority(admin)
    inventory = schemas(admin)
    entries = [(schema, service, [service.replace('-', '_') + '_' + n for n in ('a', 'b')])
               for schema, service in inventory]
    enforcement.install_namespaces(admin, entries)
    # Preserve the original namespace and immutable filesystem objects. Import
    # only hash/size-verified referenced bytes under the trusted fixture admin.
    objects = backup.object_references(admin)
    from ha_runtime.blobs import CHUNK_BYTES
    for identifier, size in objects.items():
        payload = backup._blob(Path('/fixture/objects'), identifier, size)
        with admin.transaction():
            admin.execute('INSERT INTO s_portal_web_state.ha_blob_objects VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',
                          (identifier, size, (size + CHUNK_BYTES - 1) // CHUNK_BYTES))
            for ordinal, start in enumerate(range(0, size, CHUNK_BYTES)):
                admin.execute('INSERT INTO s_portal_web_state.ha_blob_chunks VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',
                              (identifier, ordinal, payload[start:start + CHUNK_BYTES]))
        need(backup._pg_blob(admin, identifier, size) == payload)
    return {'guarded_namespaces': len(inventory), 'imported_verified_references': len(objects),
            'original_namespace_and_fs_preserved': True}


def advance(admin):
    from psycopg import sql
    authority(admin)
    generation = admin.execute('SELECT epoch+1 FROM ha.control WHERE singleton=1').fetchone()[0]
    for service in SERVICES:
        for node in ('A', 'B'):
            role = service.replace('-', '_') + '_next_' + node.lower()
            admin.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(role), sql.Literal(fresh_password(role))))
            admin.execute('INSERT INTO ha.identities VALUES(%s,%s,%s)', (role, service, node))
    entries = [(schema, service, [service.replace('-', '_') + '_next_' + n for n in ('a', 'b')])
               for schema, service in schemas(admin)]
    enforcement.install_namespaces(admin, entries, generation=generation)
    with admin.transaction():
        admin.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK,))
        admin.execute('UPDATE ha.control SET epoch=%s,revision=revision+1 WHERE singleton=1', (generation,))
    return {'fresh_roles': 14, 'next_generation_installed_before_switch': True}


def recover(admin):
    from psycopg import sql
    authority(admin)
    generation=admin.execute('SELECT epoch FROM ha.control WHERE singleton=1').fetchone()[0]
    need(generation==3)
    for service in SERVICES:
        for node in ('A','B'):
            role=service.replace('-','_')+'_recovered_'+node.lower()
            admin.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(role),sql.Literal(fresh_password(role))))
            admin.execute('INSERT INTO ha.identities VALUES(%s,%s,%s)',(role,service,node))
    entries=[(schema,service,[service.replace('-','_')+'_recovered_'+node for node in ('a','b')]) for schema,service in schemas(admin)]
    enforcement.install_namespaces(admin,entries,generation=generation)
    return {'fresh_generation_three_roles':14,'namespace_system_rebound':True,'settings_update_restricted_again':True}


def live_observation(admin):
    import time
    from psycopg import sql
    from ha_control.pg import PgBackend
    from ha_control.controller import TransitionBlocked
    backend=object.__new__(PgBackend)
    inventory={schema:{'service':service,'tables':[row[0] for row in admin.execute(
        "SELECT relname FROM pg_class WHERE relnamespace=%s::regnamespace AND relkind IN ('r','p','f','v','m') ORDER BY relname",(schema,))]} for schema,service in schemas(admin)}
    roles={node:{service:service.replace('-','_')+'_recovered_'+node.lower() for service in SERVICES} for node in ('A','B')}
    generation=admin.execute('SELECT epoch FROM ha.control WHERE singleton=1').fetchone()[0]
    backend.plan={'system_id':admin.execute('SELECT system_identifier::text FROM pg_control_system()').fetchone()[0],
        'pg_enforcement':{'definition_sha256':enforcement.definition_sha256(),'namespace_tables':inventory,'role_sets':roles}}
    path=Path('/fixture/results/recovered-pg-plan.json');path.write_text(json.dumps(backend.plan,sort_keys=True));path.chmod(0o600)
    started=time.monotonic();backend._enforced_observation(admin,{'generation':generation});elapsed=time.monotonic()-started
    schema=next(iter(inventory));namespace=sql.Identifier(schema);table=sql.Identifier(schema,'ha_receipts')
    drifts=[
        sql.SQL('ALTER SCHEMA {} RENAME TO s_missing_probe').format(namespace),
        sql.SQL('ALTER SCHEMA {} RENAME TO s_empty_probe; CREATE SCHEMA {}').format(namespace,namespace),
        sql.SQL('CREATE TABLE {}.recovery_extra_probe(value integer)').format(namespace),
        sql.SQL('ALTER TABLE {} DISABLE TRIGGER ha_enforced_write').format(table),
        sql.SQL('ALTER TABLE {} RENAME TO ha_receipts_hidden').format(table),
        sql.SQL('GRANT UPDATE ON pg_catalog.pg_authid TO {}').format(sql.Identifier(roles['A']['book-memo']))]
    for mutation in drifts:
        with admin.transaction(force_rollback=True):
            admin.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK,));admin.execute(mutation)
            try:backend._enforced_observation(admin,{'generation':generation})
            except TransitionBlocked:pass
            else:raise AssertionError('live_catalog_drift_accepted')
        backend._enforced_observation(admin,{'generation':generation})
    return {'actual_pg_component_observation':True,'scope':'pg_live_component_only','includes_connection_authentication':False,'rolled_back_drift_denials':len(drifts),
        'positive_observation_ms':round(elapsed*1000,3),'windows_full_budget_validated':False,
        'frozen_plan_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


def peer_blobs(admin, endpoint):
    objects=admin.execute('SELECT sha256,size FROM s_portal_web_state.ha_blob_objects ORDER BY sha256').fetchall()
    with connection(dict(endpoint,host='ha-b')) as peer:
        for identifier,size in objects:
            payload=backup._pg_blob(admin,identifier,size)
            need(payload==backup._pg_blob(peer,identifier,size) and hashlib.sha256(payload).hexdigest()==identifier)
    need(len(objects)>=3)
    return {'all_blob_objects_actual_sync_peer_hash_match':len(objects)}


def old_roles_denied(admin):
    import psycopg
    from psycopg import sql
    count = 0
    inventory = schemas(admin)
    for service in SERVICES:
        schema = next(schema for schema, item in inventory if item == service)
        for generation in ('', '_next'):
            for node in ('a', 'b'):
                role = service.replace('-', '_') + generation + '_' + node
                endpoint = dict(json.loads(Path('/fixture/admin.json').read_text()),
                                user=role, passfile='/fixture/' + role + '.pgpass')
                with connection(endpoint) as client:
                    need(client.execute(sql.SQL('SELECT count(*) FROM {}.ha_receipts').format(sql.Identifier(schema))).fetchone() == (0,))
                    try:
                        client.execute(sql.SQL('INSERT INTO {}.ha_receipts VALUES(%s,%s,%s)').format(sql.Identifier(schema)),
                                       ('stale-probe', 'stale-probe', 'stale-probe'))
                    except psycopg.Error as error:
                        need(error.sqlstate == '42501')
                    else:
                        raise AssertionError('stale_role_write_accepted')
                count += 1
    return {'closed_roles_read_and_write_denied': count}


def provision(admin):
    from psycopg import sql
    for service in SERVICES:
        for node in ('a', 'b'):
            old = service.replace('-', '_') + '_' + node
            password = Path('/fixture/' + old + '.pgpass').read_text().splitlines()[0].split(':', 4)[4]
            fresh = service.replace('-', '_') + '_next_' + node
            for role, secret in ((old, password), (fresh, fresh_password(fresh))):
                admin.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(role), sql.Literal(secret)))
    # pg_dump grants/functions reference the fixture replication principal too.
    password = Path('/fixture/repl.pgpass').read_text().splitlines()[0].split(':', 4)[4]
    admin.execute(sql.SQL('CREATE ROLE ha_repl LOGIN REPLICATION PASSWORD {}').format(sql.Literal(password)))
    return {'preprovisioned_roles': 29}


def host_run():
    """Own a new Mac-only fixture, with durable sanitized evidence on failure."""
    import tempfile
    from run import Fixture, FixtureError, ROOT
    need(sys.platform == 'darwin')
    parser = argparse.ArgumentParser()
    parser.add_argument('--host-run', action='store_true', required=True)
    parser.add_argument('--expected-runtime-sha256', required=True)
    parser.add_argument('--direct-sql-only', action='store_true')
    parser.add_argument('--reopen-restored',action='store_true')
    parser.add_argument('--exclude-unintegrated-runtime',action='append',choices=['effects.py'],default=[])
    args = parser.parse_args()
    files = sorted(path for path in (ROOT / 'infra/k8s/ha_runtime').iterdir()
                   if path.is_file() and path.suffix in {'.py', '.sql'} and path.name not in args.exclude_unintegrated_runtime)
    files = sorted([*files, ROOT / 'infra/k8s/ha_control/privilege_closure.py'])
    runtime_hash = hashlib.sha256()
    for path in files:
        runtime_hash.update(path.name.encode() + b'\0' + path.read_bytes())
    need(runtime_hash.hexdigest() == args.expected_runtime_sha256)
    directory = Path(tempfile.mkdtemp(prefix='ha-enforced-restore-', dir='/private/tmp')).resolve()
    directory.chmod(0o700)
    fixture = Fixture(directory)
    marker = {'owner': fixture.owner, 'directory': str(directory),
              'runtime_sha256': runtime_hash.hexdigest(),
              'lab_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'enforcement_sql_sha256': enforcement.definition_sha256(), 'resources': {}}

    def private_json(path, value):
        temporary = path.with_suffix(path.suffix + '.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)

    private_json(directory / 'owner.json', marker)
    report = {'scope': 'owned_mac_enforced_apps_restore_only', 'production_access': False,
              'runtime_sha256': runtime_hash.hexdigest(), 'steps': {}, 'status': 'RUNNING', 'ready': False,
              'excluded_unintegrated_runtime':args.exclude_unintegrated_runtime,
              'pinned_runtime_files':{str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in files}}

    def persist():
        current={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
        report['live_pinned_source_drift']=current!=report['pinned_runtime_files']
        marker['resources'] = {name: getattr(fixture, name) for name in ('a', 'b', 'network') if hasattr(fixture, name)}
        if hasattr(fixture, 'runners'):
            marker['resources']['runners'] = fixture.runners
        if 'source_sha256' in fixture.report:
            marker['source_sha256'] = fixture.report['source_sha256']
        private_json(directory / 'owner.json', marker)
        private_json(directory / 'summary.json', report)

    def phase(name, group='311'):
        fixture.phase = name
        runner = fixture.runners[group]
        fixture.owned('container', runner)
        output = fixture.docker('exec', '-w', '/source', '-e', 'PYTHONPATH=/source/infra/k8s',
            '-e', 'APP_ENV=test', '-e', 'HA_LOCAL_RUNTIME=1', runner,
            '/venvs/' + ('book-memo' if group == '311' else 'system-agent') + '/bin/python',
            '/source/infra/k8s/ha-lab/enforcement_restore_smoke.py', '--phase', name, timeout=150)
        value = json.loads(output.splitlines()[-1])
        need(value.get('status') == 'PASS')
        report['steps'][name + '-' + group] = value['checks']
        persist()
        return value

    error = None
    cleaned = False
    try:
        persist()
        fixture.prepare(); persist()
        fixture.databases(); persist()
        fixture.build(); persist()
        snapshot_hash = hashlib.sha256()
        for path in files:
            copied = directory / 'context/source' / path.relative_to(ROOT)
            snapshot_hash.update(path.name.encode() + b'\0' + copied.read_bytes())
        need(snapshot_hash.hexdigest() == args.expected_runtime_sha256)
        report['source_sha256'] = fixture.report['source_sha256']
        # Legacy app implementations create synthetic inputs before enforcement.
        for service in SERVICES:
            report['steps']['seed-' + service] = fixture.execute(service, 'A', 'seed')
            persist()
        if args.direct_sql_only:
            fixture.phase = 'direct_sql_enforcement'
            runner = fixture.runners['311']
            fixture.owned('container',runner)
            output=fixture.docker('exec','-w','/source','-e','PYTHONPATH=/source/infra/k8s',
                '-e','APP_ENV=test','-e','HA_LOCAL_RUNTIME=1',runner,
                '/venvs/book-memo/bin/python','/source/infra/k8s/ha-lab/enforcement_smoke.py',timeout=180)
            value=json.loads(output.splitlines()[-1])
            need(value.get('status')=='PASS')
            report['steps']['direct_sql_enforcement']=value
            persist()
        else:
            phase('install')
            for group in fixture.runners:
                phase('configure', group)
            for service in SERVICES:
                phase('lease-A')
                report['steps']['enforced-A-' + service] = fixture.execute(service, 'A', 'smoke')
                persist()
            phase('advance')
            for group in fixture.runners:
                phase('configure-next', group)
            for service in SERVICES:
                phase('lease-B')
                report['steps']['enforced-B-' + service] = fixture.execute(service, 'B', 'smoke')
                persist()
            phase('closed')
            phase('stale')
            phase('backup')
            # The source database pair is removed only after the verified encrypted
            # backup. Runners retain the artifact and synthetic role passfiles.
            fixture.phase = 'fresh_system'
            previous_system = fixture.system_id
            for node in (fixture.b, fixture.a):
                fixture.owned('container', node)
                fixture.docker('rm', '-f', '-v', node)
            del fixture.b
            fixture.phase='fresh_system_start'
            fixture.a = fixture.start_pg('A')
            fixture.wait(lambda: fixture.docker('exec',fixture.a,'cat','/proc/1/comm')=='postgres' and fixture.psql('SELECT 1', 'postgres') == '1')
            fixture.phase='fresh_empty_database'
            fixture.psql('CREATE DATABASE ha', 'postgres')
            fresh_system = fixture.psql('SELECT system_identifier::text FROM pg_control_system()')
            need(previous_system != fresh_system)
            report['steps']['fresh-empty-system'] = {'different_system_id': True, 'source_pair_removed': True}
            persist()
            phase('provision')
            phase('restore')
            report['steps']['old-runtime-permit'] = fixture.permit_check('stale')
            if args.reopen_restored:
                phase('recover')
                for group in fixture.runners:phase('configure-recovered',group)
                fixture.phase='recovered_sync_pair'
                fixture.docker('exec',fixture.a,'bash','-ceu',"printf '%s\\n' 'hostssl replication ha_repl all scram-sha-256' >> /var/lib/postgresql/data/pg_hba.conf")
                fixture.psql('SELECT pg_reload_conf()')
                fixture.b=fixture.start_pg('B')
                fixture.wait(lambda:fixture.docker('exec',fixture.b,'psql','-XAt','-U','postgres','-d','ha','-c','SELECT pg_is_in_recovery()')=='t')
                fixture.psql("ALTER SYSTEM SET synchronous_standby_names='FIRST 1 (ha_b)'")
                fixture.psql('SELECT pg_reload_conf()')
                fixture.wait(lambda:fixture.psql("SELECT count(*) FROM pg_stat_replication WHERE application_name='ha_b' AND sync_state='sync'")=='1')
                phase('live-observation')
                for node in ('A','B'):
                    for service in SERVICES:
                        phase('lease-'+node)
                        # Reuse canonical execution routing with a synthetic run
                        # suffix, preserving baseline fixture behavior by default.
                        original_docker=fixture.docker
                        def tagged_docker(*arguments,**kwargs):
                            values=list(arguments)
                            if values and values[0]=='exec':values[1:1]=['-e','HA_SMOKE_RUN_TAG=recovered']
                            return original_docker(*values,**kwargs)
                        fixture.docker=tagged_docker
                        try:report['steps']['recovered-'+node+'-'+service]=fixture.execute(service,node,'smoke')
                        finally:fixture.docker=original_docker
                        persist()
                phase('closed');phase('stale');phase('peer-blobs')
        report['status'] = 'PASS'
    except Exception as value:
        error = type(value).__name__
        report.update(status='FAIL', error_class=error, failed_phase=fixture.phase)
        if isinstance(value,FixtureError):
            report['error_reason']=str(value)
        if 'startup_flags' in fixture.report:
            report['startup_flags']=fixture.report['startup_flags']
        if 'failure_location' in fixture.report:
            report['failure_location'] = fixture.report['failure_location']
    finally:
        # Persist before cleanup as well: an interrupted daemon must not erase
        # which exact UUID owns remaining resources.
        persist()
        try:
            cleaned = fixture.cleanup()
        except Exception:
            cleaned = False
        report['owned_fixture_removed'] = cleaned
        if not cleaned:
            report['status'] = 'FAIL'
        persist()
    print(json.dumps({'status': report['status'], 'scope': report['scope'],
                      'summary': str(directory / 'summary.json'),
                      'owned_fixture_removed': cleaned}), flush=True)
    return int(error is not None or not cleaned)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', required=True, choices=('install', 'configure', 'configure-next', 'configure-recovered', 'recover', 'live-observation', 'peer-blobs', 'advance', 'lease-A', 'lease-B', 'closed', 'stale', 'backup', 'provision', 'restore'))
    phase = parser.parse_args().phase
    need(sys.platform == 'linux' and os.geteuid() != 0 and os.getenv('APP_ENV') == 'test' and os.getenv('HA_LOCAL_RUNTIME') == '1')
    endpoint = json.loads(Path('/fixture/admin.json').read_text())
    backup._guard(endpoint)
    if phase in {'configure', 'configure-next','configure-recovered'}:
        configure(phase == 'configure-next',phase=='configure-recovered'); return {'configured_services': 7}
    if phase == 'backup':
        key = '/fixture/results/enforced-backup.key'; artifact = '/fixture/results/enforced-backup.aesgcm'
        backup._write_new(key, secrets.token_bytes(32))
        with connection(endpoint) as admin:
            authority(admin)
            cluster = admin.execute('SELECT cluster FROM ha.control').fetchone()[0]
        backup._write_new('/fixture/results/enforced-cluster.json', json.dumps({'cluster': cluster}).encode())
        result = backup.create_backup(endpoint, '/fixture/objects', artifact, key, expected_cluster=cluster)
        manifest, _, _ = backup._unpack(backup._open(Path(artifact).read_bytes(), backup._key(key)))
        need(manifest['blob_backend'] == 'postgresql' and bool(manifest['logical']['triggers']) and bool(manifest['logical']['policies']))
        result['postgresql_blob_backend'] = True
        encrypted = Path(artifact).read_bytes()
        try:
            backup._open(encrypted[:-1] + bytes([encrypted[-1] ^ 1]), backup._key(key))
        except DatabaseUnavailable:
            result['tampered_artifact_refused'] = True
        else:
            raise AssertionError('tampered_backup_accepted')
        return result
    if phase == 'restore':
        Path('/fixture/restored-objects').mkdir(mode=0o700)
        cluster = json.loads(Path('/fixture/results/enforced-cluster.json').read_text())['cluster']
        result = backup.restore_backup(endpoint, '/fixture/restored-objects', '/fixture/results/enforced-backup.aesgcm', '/fixture/results/enforced-backup.key', expected_cluster=cluster)
        with connection(endpoint) as admin:
            result.update(old_roles_denied(admin))
        return result
    with connection(endpoint) as admin:
        if phase == 'recover':return recover(admin)
        if phase == 'live-observation':return live_observation(admin)
        if phase == 'peer-blobs':return peer_blobs(admin,endpoint)
        if phase == 'install': return install(admin)
        if phase == 'advance': return advance(admin)
        if phase == 'provision': return provision(admin)
        if phase == 'stale': return old_roles_denied(admin)
        authority(admin, phase[-1] if phase.startswith('lease-') else None)
        return {'authority_closed': phase == 'closed', 'lease_seconds': 4 if phase.startswith('lease-') else 0}


if __name__ == '__main__':
    if '--host-run' in sys.argv:
        raise SystemExit(host_run())
    try:
        result = main()
        print(json.dumps({'status': 'PASS', 'checks': result, 'ready': False}), flush=True)
    except Exception as error:
        code = getattr(error, 'sqlstate', None)
        frames = [{'file': Path(f.filename).name, 'function': f.name, 'line': f.lineno} for f in traceback.extract_tb(error.__traceback__)]
        reason = str(error) if isinstance(error, DatabaseUnavailable) and str(error).replace('_', '').isalnum() else 'isolated_enforcement_restore_failed'
        print(json.dumps({'status': 'FAIL', 'runtime_reason': reason, 'reason': code, 'frames': frames}), flush=True)
        raise SystemExit(1) from None
