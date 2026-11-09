"""Disposable PG17 enforcement/blob evidence; never a host/VM acceptance test.

Run only inside run.Fixture's owned, private-network unprivileged runner after
its seven-app migration and node A/B tests. All credentials are synthetic.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
from unittest.mock import patch
import io
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ha_runtime import blobs, db, enforcement, proof
from ha_runtime.config import DatabaseUnavailable, SERVICES

LOCK = 7142026110601
SCHEMA = 's_book_memo_probe'
PHASE = 'preflight'


def need(value):
    if not value:
        raise AssertionError('isolated_enforcement_check_failed')


def connection(endpoint, **kwargs):
    import psycopg
    return psycopg.connect(**endpoint, autocommit=True, connect_timeout=3, **kwargs)


def lease(admin, *, opened=True, seconds=4):
    need(0 < seconds <= 5)
    with admin.transaction():
        admin.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK,))
        admin.execute("UPDATE ha.control SET serving=ARRAY['A'],worker='A',writes_open=%s,history_safe=%s,expires_at=clock_timestamp()+(%s * interval '1 second') WHERE singleton=1",
                      (opened, opened, seconds if opened else -1))


def denied(endpoint, statement, *, copy_row=None, codes=('42501',)):
    import psycopg
    with connection(endpoint) as client:
        try:
            if copy_row is None:
                client.execute(statement)
            else:
                with client.cursor().copy(statement) as stream:
                    stream.write_row(copy_row)
        except psycopg.Error as error:
            need(error.sqlstate in codes)
            return True
    raise AssertionError('raw_mutation_was_not_denied')


CLOSURE_UNSAFE = []
CLOSURE_RELATIONS = []


def captured_closure_check(admin, identities):
    """Keep static failure categories only, while the failed batch still exists."""
    from ha_control.privilege_closure import PRIVILEGE_CLOSURE_SQL, privilege_closures_unsafe
    result = privilege_closures_unsafe(admin, identities)
    CLOSURE_UNSAFE.clear();CLOSURE_RELATIONS.clear()
    if result:
        prefix, body = PRIVILEGE_CLOSURE_SQL.split('SELECT a.role_name::text,(\n',1)
        body = body.rsplit('\n) FROM subject a ORDER BY a.role_name',1)[0]
        pieces=[];depth=0;quoted=False;start=0;position=0
        while position<len(body):
            char=body[position]
            if char=="'":
                if quoted and position+1<len(body) and body[position+1]=="'":
                    position+=2;continue
                quoted=not quoted
            if not quoted:
                if char=='(':depth+=1
                elif char==')':depth-=1
                elif depth==0 and body.startswith('\n    OR ',position):
                    pieces.append(body[start:position]);position+=8;start=position;continue
            position+=1
        pieces.append(body[start:])
        names=['authority','role_attributes','database_create','schema_create','sequence_write',
               'relation_write','immutable_trigger_reference','parameter_acl','lo_owner','lo_acl',
               'secret_authid','secret_subscription','secret_mapping','dangerous_function','definer']
        need(len(pieces)==len(names))
        query=prefix+'SELECT '+','.join('bool_or('+piece+')' for piece in pieces)+' FROM subject a'
        flags=admin.execute(query,([r for r,_ in identities],[service for _,service in identities])).fetchone()
        CLOSURE_UNSAFE[:]=[name for name,value in zip(names,flags) if value is not False]
        if 'relation_write' in CLOSURE_UNSAFE:
            inner=pieces[5].strip()
            need(inner.startswith('EXISTS(SELECT 1 FROM ') and inner.endswith(')'))
            relation_from=inner[len('EXISTS(SELECT 1 FROM '):-1]
            columns='''n.nspname,c.relname,a.service,c.relrowsecurity,c.relforcerowsecurity,
                EXISTS(SELECT 1 FROM ha.enforced_namespaces e WHERE e.schema_name=n.nspname AND e.service=a.service),
                (SELECT t.tgtype FROM pg_trigger t WHERE t.tgrelid=c.oid AND t.tgname='ha_enforced_write'),
                (SELECT t.tgtype FROM pg_trigger t WHERE t.tgrelid=c.oid AND t.tgname='ha_enforced_commit'),
                (SELECT t.tgattr::text FROM pg_trigger t WHERE t.tgrelid=c.oid AND t.tgname='ha_enforced_write'),
                EXISTS(SELECT 1 FROM pg_roles r WHERE r.oid=c.relowner AND r.rolsuper),
                EXISTS(SELECT 1 FROM pg_trigger t WHERE t.tgrelid=c.oid AND t.tgname='ha_enforced_write'
                  AND t.tgenabled='A' AND t.tgfoid='ha.enforced_write_guard()'::regprocedure AND t.tgtype=62
                  AND t.tgqual IS NULL AND t.tgattr::text='' AND t.tgnargs=0),
                EXISTS(SELECT 1 FROM pg_trigger t WHERE t.tgrelid=c.oid AND t.tgname='ha_enforced_commit'
                  AND t.tgenabled='A' AND t.tgfoid='ha.enforced_write_guard()'::regprocedure AND t.tgtype=29
                  AND t.tgdeferrable AND t.tginitdeferred AND t.tgqual IS NULL AND t.tgattr::text='' AND t.tgnargs=0)'''
            rows=admin.execute(prefix+'SELECT '+columns+' FROM subject a,'+relation_from,
                ([r for r,_ in identities],[service for _,service in identities])).fetchall()
            CLOSURE_RELATIONS[:]=list(dict.fromkeys(tuple(row) for row in rows))
    return result


def main():
    global PHASE
    need(sys.platform == 'linux' and os.geteuid() != 0 and os.getenv('APP_ENV') == 'test'
         and os.getenv('HA_LOCAL_RUNTIME') == '1')
    admin_endpoint = json.loads(Path('/fixture/admin.json').read_text())
    need(admin_endpoint['host'] == 'ha-a' and admin_endpoint['dbname'] == 'ha'
         and admin_endpoint['sslmode'] == 'verify-full' and admin_endpoint['sslrootcert'] == '/fixture/ca.crt')
    config_a = json.loads(Path('/fixture/config/book-memo-A.json').read_text())
    config_b = json.loads(Path('/fixture/config/book-memo-B.json').read_text())
    endpoint_a, endpoint_b = config_a['primary'], config_b['primary']
    results = {}
    with connection(admin_endpoint) as admin:
        PHASE = 'closed_install'
        lease(admin, opened=False)
        from ha_runtime import migrate
        with admin.transaction():
            migrate.create_namespace(admin, 'book-memo', 'probe')
            admin.execute('CREATE TABLE s_book_memo_probe.probe(id integer PRIMARY KEY,value text)')
            admin.execute('CREATE TABLE s_book_memo_probe.identity_probe(id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY)')
            migrate.grant_namespace(admin, SCHEMA, [endpoint_a['user'], endpoint_b['user']])
        schemas = [row[0] for row in admin.execute("SELECT nspname FROM pg_namespace WHERE left(nspname,2)='s_' ORDER BY nspname")]
        entries = []
        for schema in schemas:
            service = next((s for s in SERVICES if schema.startswith('s_' + s.replace('-', '_') + '_')), None)
            need(service is not None)
            roles = [service.replace('-', '_') + '_' + node for node in ('a', 'b')]
            entries.append((schema, service, roles))
        from psycopg import sql as pgsql
        admin.execute(pgsql.SQL('GRANT UPDATE(setting) ON pg_catalog.pg_settings TO {}').format(pgsql.Identifier(endpoint_a['user'])))
        # A partial install must not commit grants while the same identity can
        # still write through another persistent unguarded namespace.
        try:
            enforcement.install_namespace(admin, SCHEMA, 'book-memo', [endpoint_a['user'],endpoint_b['user']])
        except DatabaseUnavailable:
            pass
        else:
            raise AssertionError('partial_namespace_install_committed')
        need(admin.execute("SELECT to_regclass('ha.enforced_namespaces')").fetchone() == (None,))
        need(admin.execute("SELECT count(*) FROM pg_trigger WHERE tgname='ha_enforced_write'").fetchone() == (0,))
        need(admin.execute("SELECT has_table_privilege(%s,'pg_catalog.pg_settings','UPDATE') OR has_any_column_privilege(%s,'pg_catalog.pg_settings','UPDATE')",
            (endpoint_a['user'],endpoint_a['user'])).fetchone()==(True,))
        need(admin.execute('''SELECT EXISTS(SELECT 1 FROM pg_attribute column_acl,LATERAL aclexplode(column_acl.attacl) acl
            WHERE column_acl.attrelid='pg_catalog.pg_settings'::regclass AND column_acl.attname='setting'
              AND acl.grantee=(SELECT oid FROM pg_roles WHERE rolname=%s) AND acl.privilege_type='UPDATE')''',
            (endpoint_a['user'],)).fetchone()==(True,))
        results['failed_batch_restores_public_and_explicit_settings_update_acl'] = True
        enforcement.install_namespaces(admin, entries)
        results['atomic_batch_rejects_partial_namespace_grants'] = True
        results['all_existing_namespaces_guarded'] = len(schemas)
        lease(admin)
        with connection(endpoint_a) as client:
            client.execute("SET work_mem='4MB'")
            need(client.execute("SHOW work_mem").fetchone()==('4MB',))
        denied(endpoint_a,"UPDATE pg_catalog.pg_settings SET setting='8192' WHERE name='work_mem'")
        lease(admin,opened=False)
        results['pg_settings_update_denied_but_ordinary_session_set_preserved'] = True
        results['two_phase_transactions_disabled'] = admin.execute("SHOW max_prepared_transactions").fetchone() == ('0',)
        need(results['two_phase_transactions_disabled'])

        PHASE = 'installer_privilege_regressions'
        from psycopg import sql
        def install_denied():
            try:
                enforcement.install_namespace(admin, SCHEMA, 'book-memo', [endpoint_a['user'], endpoint_b['user']])
            except DatabaseUnavailable:
                return
            raise AssertionError('unsafe_installer_input_accepted')
        # Failed installation must not rewrite the preexisting authority grant.
        admin.execute('GRANT UPDATE(serving) ON ha.control TO PUBLIC')
        install_denied()
        need(admin.execute("SELECT has_column_privilege(%s,'ha.control','serving','UPDATE')", (endpoint_a['user'],)).fetchone() == (True,))
        admin.execute('REVOKE UPDATE(serving) ON ha.control FROM PUBLIC')
        admin.execute(sql.SQL('ALTER TABLE s_book_memo_probe.probe OWNER TO {}').format(sql.Identifier(endpoint_a['user'])))
        install_denied()
        admin.execute('ALTER TABLE s_book_memo_probe.probe OWNER TO CURRENT_USER')
        # The immutable table must shed both PUBLIC and per-column grants.
        admin.execute('GRANT UPDATE(receipt) ON s_book_memo_probe.ha_receipts TO PUBLIC')
        admin.execute(sql.SQL('GRANT UPDATE(receipt) ON s_book_memo_probe.ha_receipts TO {}').format(sql.Identifier(endpoint_a['user'])))
        enforcement.install_namespace(admin, SCHEMA, 'book-memo', [endpoint_a['user'], endpoint_b['user']])
        need(admin.execute("SELECT has_any_column_privilege(%s,'s_book_memo_probe.ha_receipts','UPDATE,REFERENCES')", (endpoint_a['user'],)).fetchone() == (False,))
        results['public_column_privilege_bad_owner_and_failed_install_rollback'] = True

        PHASE = 'global_persistent_privilege_closure'
        admin.execute('CREATE SCHEMA pgxevil')
        admin.execute('CREATE TABLE pgxevil.outside_guard(value integer)')
        admin.execute('CREATE SEQUENCE pgxevil.outside_sequence')
        subject = sql.Identifier(endpoint_a['user'])
        cases = [
            ('GRANT INSERT ON pgxevil.outside_guard TO {}', 'REVOKE INSERT ON pgxevil.outside_guard FROM {}'),
            ('GRANT UPDATE(value) ON pgxevil.outside_guard TO {}', 'REVOKE UPDATE(value) ON pgxevil.outside_guard FROM {}'),
            ('GRANT USAGE ON SEQUENCE pgxevil.outside_sequence TO {}', 'REVOKE USAGE ON SEQUENCE pgxevil.outside_sequence FROM {}'),
            ('GRANT UPDATE ON SEQUENCE pgxevil.outside_sequence TO {}', 'REVOKE UPDATE ON SEQUENCE pgxevil.outside_sequence FROM {}'),
            ('GRANT CREATE ON SCHEMA pgxevil TO {}', 'REVOKE CREATE ON SCHEMA pgxevil FROM {}'),
            ('GRANT CREATE ON DATABASE ha TO {}', 'REVOKE CREATE ON DATABASE ha FROM {}'),
        ]
        for grant, revoke in cases:
            admin.execute(sql.SQL(grant).format(subject))
            install_denied()
            admin.execute(sql.SQL(revoke).format(subject))
        admin.execute('GRANT INSERT(value) ON pgxevil.outside_guard TO PUBLIC')
        install_denied()
        need(admin.execute("SELECT has_column_privilege(%s,'pgxevil.outside_guard','value','INSERT')", (endpoint_a['user'],)).fetchone() == (True,))
        admin.execute('REVOKE INSERT(value) ON pgxevil.outside_guard FROM PUBLIC')
        admin.execute(sql.SQL('ALTER DATABASE ha OWNER TO {}').format(subject))
        install_denied()
        admin.execute('ALTER DATABASE ha OWNER TO CURRENT_USER')
        admin.execute('DROP SCHEMA pgxevil CASCADE')
        enforcement.install_namespace(admin, SCHEMA, 'book-memo', [endpoint_a['user'],endpoint_b['user']])
        results['outside_namespace_table_column_sequence_schema_database_closure'] = 8

        PHASE = 'catalog_lo_serverfile_privilege_closure'
        for index,(grant,revoke) in enumerate([
                ('GRANT UPDATE ON pg_catalog.pg_authid TO {}','REVOKE UPDATE ON pg_catalog.pg_authid FROM {}'),
                ('GRANT UPDATE(rolpassword) ON pg_catalog.pg_authid TO {}','REVOKE UPDATE(rolpassword) ON pg_catalog.pg_authid FROM {}'),
                ('GRANT SELECT(rolpassword) ON pg_catalog.pg_authid TO {}','REVOKE SELECT(rolpassword) ON pg_catalog.pg_authid FROM {}'),
                ('GRANT CREATE ON SCHEMA pg_catalog TO {}','REVOKE CREATE ON SCHEMA pg_catalog FROM {}')]):
            PHASE='catalog_privilege_case_'+str(index)
            admin.execute(sql.SQL(grant).format(subject))
            install_denied()
            admin.execute(sql.SQL(revoke).format(subject))
        unsafe_functions=admin.execute("SELECT p.oid::regprocedure::text FROM pg_proc p WHERE p.proname=ANY(%s)",
            (['lo_export','pg_read_file','pg_read_binary_file','pg_ls_dir','pg_stat_file'],)).fetchall()
        need(bool(unsafe_functions))
        for signature, in unsafe_functions:
            admin.execute(sql.SQL('GRANT EXECUTE ON FUNCTION {} TO {}').format(sql.SQL(signature),subject))
            install_denied()
            admin.execute(sql.SQL('REVOKE EXECUTE ON FUNCTION {} FROM {}').format(sql.SQL(signature),subject))
        large_object=admin.execute('SELECT lo_create(0)').fetchone()[0]
        for permission in ('SELECT','UPDATE'):
            admin.execute(sql.SQL('GRANT '+permission+' ON LARGE OBJECT {} TO {}').format(sql.Literal(large_object),subject))
            install_denied()
            admin.execute(sql.SQL('REVOKE '+permission+' ON LARGE OBJECT {} FROM {}').format(sql.Literal(large_object),subject))
        admin.execute(sql.SQL('ALTER LARGE OBJECT {} OWNER TO {}').format(sql.Literal(large_object),subject))
        install_denied()
        admin.execute('SELECT lo_unlink(%s)',(large_object,))
        enforcement.install_namespace(admin, SCHEMA, 'book-memo', [endpoint_a['user'],endpoint_b['user']])
        results['catalog_dml_secret_reads_schema_create_serverfile_exec_lo_owner_rejected'] = 7+len(unsafe_functions)
        results['pgxevil_literal_prefix_does_not_escape_closure'] = True

        PHASE = 'parameter_privilege_closure'
        for permission,parameter in [('ALTER SYSTEM','work_mem'),('SET','session_replication_role')]:
            for grantee in (subject,sql.SQL('PUBLIC')):
                admin.execute(sql.SQL('GRANT '+permission+' ON PARAMETER {} TO {}').format(sql.Identifier(parameter),grantee))
                install_denied()
                need(admin.execute('SELECT has_parameter_privilege(%s,%s,%s)',
                    (endpoint_a['user'],parameter,permission)).fetchone() == (True,))
                admin.execute(sql.SQL('REVOKE '+permission+' ON PARAMETER {} FROM {}').format(sql.Identifier(parameter),grantee))
        enforcement.install_namespace(admin, SCHEMA, 'book-memo', [endpoint_a['user'],endpoint_b['user']])
        results['direct_and_public_alter_system_and_superuser_set_acl_rejected'] = 4

        PHASE = 'authority_global_owner_and_privilege_closure'
        admin.execute('CREATE ROLE authority_probe NOLOGIN')
        admin.execute('CREATE ROLE authority_login LOGIN')
        outsider=sql.Identifier('authority_probe')
        for name in ('control','identities','controller_probes','enforced_namespaces','enforced_roles'):
            table=sql.Identifier('ha',name)
            admin.execute(sql.SQL('ALTER TABLE {} OWNER TO {}').format(table,outsider))
            install_denied()
            admin.execute(sql.SQL('ALTER TABLE {} OWNER TO CURRENT_USER').format(table))
            admin.execute(sql.SQL('GRANT UPDATE ON {} TO {}').format(table,outsider))
            install_denied()
            admin.execute(sql.SQL('REVOKE UPDATE ON {} FROM {}').format(table,outsider))
        admin.execute('GRANT UPDATE(generation) ON ha.controller_probes TO PUBLIC')
        install_denied()
        admin.execute('REVOKE UPDATE(generation) ON ha.controller_probes FROM PUBLIC')
        admin.execute('GRANT pg_write_all_data TO authority_login')
        install_denied()
        admin.execute('REVOKE pg_write_all_data FROM authority_login')
        admin.execute('ALTER ROLE authority_login NOINHERIT')
        admin.execute('GRANT pg_write_all_data TO authority_login')
        need(admin.execute("SELECT has_table_privilege('authority_login','ha.control','UPDATE')").fetchone() == (False,))
        # Exercise the actual NOINHERIT escape before checking its rejection.
        # SET LOCAL identity changes and the no-row UPDATE end with this transaction.
        with admin.transaction():
            admin.execute('SET LOCAL SESSION AUTHORIZATION authority_login')
            admin.execute('SET LOCAL ROLE pg_write_all_data')
            need(admin.execute('SELECT current_user').fetchone() == ('pg_write_all_data',))
            admin.execute('UPDATE ha.control SET epoch=1 WHERE false')
        install_denied()
        admin.execute('REVOKE pg_write_all_data FROM authority_login')
        admin.execute('GRANT pg_write_all_data TO authority_probe')
        admin.execute('GRANT authority_probe TO authority_login')
        install_denied()
        admin.execute('REVOKE pg_write_all_data FROM authority_probe')
        admin.execute('GRANT CREATE ON SCHEMA ha TO authority_probe')
        install_denied()
        admin.execute('REVOKE CREATE ON SCHEMA ha FROM authority_probe')
        admin.execute('REVOKE authority_probe FROM authority_login')
        admin.execute('DROP ROLE authority_login,authority_probe')
        enforcement.install_namespace(admin, SCHEMA, 'book-memo', [endpoint_a['user'],endpoint_b['user']])
        results['five_authority_tables_non_tcb_owner_grants_public_column_and_effective_role_rejected'] = 12
        results['noinherit_login_actual_set_role_write_and_indirect_table_schema_paths_rejected'] = 3

        PHASE = 'raw_mutation_baseline'
        lease(admin)
        with connection(endpoint_a) as client:
            client.execute("INSERT INTO s_book_memo_probe.probe VALUES(1,'baseline')")
            client.execute("WITH added AS (INSERT INTO s_book_memo_probe.probe VALUES(2,'cte') RETURNING id) SELECT id FROM added")
            client.execute("INSERT INTO s_book_memo_probe.probe VALUES(1,'upsert') ON CONFLICT(id) DO UPDATE SET value=excluded.value")
        # PG17 rejects COPY FROM on RLS tables even for an admitted app role.
        # This remains a bypass-denial test, never an unsupported success claim.
        denied(endpoint_a, 'COPY s_book_memo_probe.probe(id,value) FROM STDIN', copy_row=(3, 'copy'), codes=('0A000',))
        results['valid_raw_insert_cte_upsert'] = True
        results['copy_from_rls_always_rejected'] = True

        PHASE = 'closed_expired_wrong_node'
        mutations = ["INSERT INTO s_book_memo_probe.probe VALUES(10,'denied')",
                     "WITH changed AS (INSERT INTO s_book_memo_probe.probe VALUES(11,'denied') RETURNING id) SELECT id FROM changed",
                     "INSERT INTO s_book_memo_probe.probe VALUES(1,'denied') ON CONFLICT(id) DO UPDATE SET value=excluded.value"]
        lease(admin, opened=False)
        with connection(endpoint_a) as client:
            need(client.execute('SELECT count(*) FROM s_book_memo_probe.probe').fetchone() == (0,))
        for sql in mutations:
            denied(endpoint_a, sql)
        denied(endpoint_a, 'COPY s_book_memo_probe.probe(id,value) FROM STDIN', copy_row=(12, 'denied'), codes=('0A000',))
        lease(admin, seconds=0.05); time.sleep(0.07)
        denied(endpoint_a, mutations[0])
        lease(admin)
        denied(endpoint_b, mutations[0])
        with admin.transaction():
            admin.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK,))
            admin.execute("UPDATE ha.control SET serving=ARRAY[NULL]::text[] WHERE singleton=1")
        denied(endpoint_a, mutations[0])
        with connection(endpoint_a) as client:
            need(client.execute('SELECT count(*) FROM s_book_memo_probe.probe').fetchone() == (0,))
        lease(admin)
        results['closed_expired_wrong_node_denied'] = True
        results['closed_and_null_serving_reads_fail_closed'] = True

        PHASE = 'temporary_catalog_shadow'
        lease(admin, opened=False)
        with connection(endpoint_a) as client:
            client.execute('CREATE TEMP TABLE pg_roles(rolname name,rolsuper boolean)')
            client.execute('INSERT INTO pg_temp.pg_roles VALUES(session_user,true)')
            try:
                client.execute(mutations[0])
            except Exception as error:
                need(getattr(error, 'sqlstate', None) == '42501')
            else:
                raise AssertionError('temporary_catalog_shadow_bypass')
        results['pg_temp_catalog_shadow_denied'] = True

        PHASE = 'privilege_bypass'
        lease(admin)
        for sql in ['TRUNCATE s_book_memo_probe.probe',
                    'ALTER TABLE s_book_memo_probe.probe DISABLE TRIGGER ALL',
                    'SET ROLE ' + endpoint_b['user'],
                    "SELECT nextval('s_book_memo_probe.identity_probe_id_seq')",
                    "SELECT setval('s_book_memo_probe.identity_probe_id_seq',99)",
                    'SELECT lo_create(99999)']:
            denied(endpoint_a, sql)
        denied(endpoint_a, "BEGIN; PREPARE TRANSACTION 'isolated_enforcement_probe'", codes=('55000', '0A000'))
        results['truncate_ddl_role_sequence_large_object_and_2pc_denied'] = True

        PHASE = 'transaction_lock_and_savepoint'
        import psycopg
        lease(admin)
        denied(endpoint_a, "BEGIN ISOLATION LEVEL REPEATABLE READ; INSERT INTO s_book_memo_probe.probe VALUES(19,'rr_denied')")
        results['repeatable_read_mutation_rejected'] = True
        with connection(endpoint_a) as client:
            client.execute('BEGIN ISOLATION LEVEL READ COMMITTED')
            client.execute('SELECT count(*) FROM s_book_memo_probe.probe')
            client.execute('SELECT count(*) FROM s_book_memo_probe.probe')
            client.execute('SAVEPOINT before_write')
            client.execute("INSERT INTO s_book_memo_probe.probe VALUES(20,'rolled_back')")
            client.execute('ROLLBACK TO SAVEPOINT before_write')
            client.execute("INSERT INTO s_book_memo_probe.probe VALUES(21,'held')")
            # A writer holds the generation's shared advisory lock, but must
            # not retain a row SHARE lock which prevents expiry-only renewal.
            with admin.transaction():
                admin.execute("SET LOCAL lock_timeout='100ms'")
                admin.execute('SELECT pg_advisory_xact_lock_shared(%s)',(LOCK,))
                need(admin.execute("UPDATE ha.control SET expires_at=clock_timestamp()+interval '4 seconds' WHERE singleton=1 RETURNING singleton").fetchone()==(1,))
            results['active_writer_allows_same_authority_shared_renewal'] = True
            try:
                with admin.transaction():
                    admin.execute("SET LOCAL lock_timeout='100ms'")
                    admin.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK,))
            except psycopg.errors.LockNotAvailable:
                pass
            else:
                raise AssertionError('generation_advanced_over_live_transaction')
            client.execute('COMMIT')
        need(admin.execute('SELECT count(*) FROM s_book_memo_probe.probe WHERE id=20').fetchone() == (0,))
        results['read_committed_savepoint_and_generation_lock'] = True

        PHASE = 'admitted_transaction_commit_expiry_limit'
        lease(admin,seconds=0.5)
        with connection(endpoint_a) as client:
            client.execute('BEGIN ISOLATION LEVEL READ COMMITTED')
            client.execute("INSERT INTO s_book_memo_probe.probe VALUES(24,'admitted_before_expiry')")
            client.execute('SET CONSTRAINTS ALL IMMEDIATE')
            time.sleep(0.6)
            client.execute('COMMIT')
        need(admin.execute('SELECT count(*) FROM s_book_memo_probe.probe WHERE id=24').fetchone()==(1,))
        denied(endpoint_a,"INSERT INTO s_book_memo_probe.probe VALUES(25,'expired_new_statement')")
        results['early_immediate_constraints_allow_prior_admitted_commit_after_expiry_limit'] = True
        results['expired_new_statement_after_delayed_commit_denied'] = True

        PHASE = 'portal_blob_transaction'
        portal = json.loads(Path('/fixture/config/portal-web-A.json').read_text())
        portal.update(profile='enforced-vm-v1', enforcement_sha256=enforcement.definition_sha256())
        path = Path('/fixture/config/enforced-portal-probe.json'); path.write_text(json.dumps(portal)); path.chmod(0o600)
        os.environ['HA_PG_CONFIG'] = str(path)
        payload = b'isolated-transactional-pg-blob' * 3000
        identifier = hashlib.sha256(payload).hexdigest()
        lease(admin)
        with db.state_transaction('portal-web', 'enforcement-blob-probe', {}) as state:
            state['reference'] = blobs.put('portal-web', io.BytesIO(payload))
            need(blobs.get('portal-web', identifier, len(payload)) == payload)
        peer_endpoint = dict(admin_endpoint, host='ha-b')
        with connection(peer_endpoint) as peer:
            rows = peer.execute('SELECT payload FROM s_portal_web_state.ha_blob_chunks WHERE sha256=%s ORDER BY ordinal', (identifier,)).fetchall()
            need(b''.join(bytes(row[0]) for row in rows) == payload)
        results['actual_sync_peer_blob_bytes_match'] = True
        rollback_sha = hashlib.sha256(b'rollback-probe').hexdigest()
        lease(admin)
        try:
            with db.state_transaction('portal-web', 'enforcement-blob-rollback', {}) as state:
                state['reference'] = blobs.put('portal-web', io.BytesIO(b'rollback-probe'))
                raise RuntimeError('injected_precommit_failure')
        except RuntimeError:
            pass
        need(admin.execute('SELECT count(*) FROM s_portal_web_state.ha_blob_objects WHERE sha256=%s', (rollback_sha,)).fetchone() == (0,))
        need(admin.execute("SELECT count(*) FROM s_portal_web_state.shared_state WHERE key='enforcement-blob-rollback'").fetchone() == (0,))
        results['actual_blob_and_namespace_rollback'] = True
        lease(admin)
        uncertain = b'committed-with-injected-lost-proof'; uncertain_sha = hashlib.sha256(uncertain).hexdigest()
        with patch.object(proof, 'protect', side_effect=DatabaseUnavailable('injected_postcommit_proof_failure')):
            try:
                with db.state_transaction('portal-web', 'enforcement-blob-uncertain', {}) as state:
                    state['reference'] = blobs.put('portal-web', io.BytesIO(uncertain))
            except DatabaseUnavailable:
                pass
            else:
                raise AssertionError('uncertain_proof_reported_success')
        need(admin.execute('SELECT count(*) FROM s_portal_web_state.ha_blob_objects WHERE sha256=%s', (uncertain_sha,)).fetchone() == (1,))
        need(admin.execute("SELECT count(*) FROM s_portal_web_state.shared_state WHERE key='enforcement-blob-uncertain'").fetchone() == (1,))
        results['injected_postcommit_proof_failure_keeps_atomic_data_without_success'] = True

        PHASE = 'fresh_generation_credentials'
        lease(admin, opened=False)
        with admin.transaction():
            admin.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK,))
            admin.execute('UPDATE ha.control SET epoch=epoch+1,revision=revision+1 WHERE singleton=1')
        try:
            enforcement.install_namespace(admin, SCHEMA, 'book-memo', [endpoint_a['user'], endpoint_b['user']])
        except DatabaseUnavailable:
            pass
        else:
            raise AssertionError('old_generation_role_reenrolled')
        lease(admin)
        denied(endpoint_a, mutations[0])
        lease(admin, opened=False)
        fresh_roles = []
        from psycopg import sql
        for node, endpoint in [('A', endpoint_a), ('B', endpoint_b)]:
            role = 'book_memo_fresh_' + node.lower(); password = secrets.token_urlsafe(32)
            admin.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(role), sql.Literal(password)))
            admin.execute('INSERT INTO ha.identities VALUES(%s,%s,%s)', (role, 'book-memo', node))
            migrate.grant_namespace(admin, SCHEMA, [role])
            admin.execute(sql.SQL('GRANT USAGE ON SCHEMA ha TO {}; GRANT EXECUTE ON FUNCTION ha.observe() TO {}').format(sql.Identifier(role), sql.Identifier(role)))
            fresh_roles.append(role)
            if node == 'A':
                passfile = Path('/fixture/fresh-probe.pgpass')
                passfile.write_text('ha-a:5432:ha:' + role + ':' + password + '\n'); passfile.chmod(0o600)
                fresh_endpoint = dict(endpoint, user=role, passfile=str(passfile))
        enforcement.install_namespace(admin, SCHEMA, 'book-memo', fresh_roles)
        lease(admin)
        with connection(fresh_endpoint) as client:
            client.execute("INSERT INTO s_book_memo_probe.probe VALUES(30,'fresh_generation')")
        denied(endpoint_a, "INSERT INTO s_book_memo_probe.probe VALUES(31,'stale_generation')")
        results['same_node_stale_role_denied_fresh_role_admitted'] = True
        lease(admin, opened=False)
    print(json.dumps({'status': 'PASS', 'scope': 'owned_mac_pg_fixture_only', 'results': results,
                      'production_deployed': False, 'windows_or_hyperv_validated': False,
                      'full_backup_restore': 'not_executed_in_this_smoke', 'ready': False}), flush=True)


if __name__ == '__main__':
    try:
        with patch.object(enforcement,'privilege_closures_unsafe',captured_closure_check):
            main()
    except Exception as error:
        code = getattr(error, 'sqlstate', None)
        code = code if isinstance(code, str) and re.fullmatch('[0-9A-Z]{5}', code) else 'fixed_check_failed'
        frames = [{'file': Path(frame.filename).name, 'function': frame.name, 'line': frame.lineno}
                  for frame in traceback.extract_tb(error.__traceback__)
                  if Path(frame.filename).name in {'enforcement_smoke.py', 'enforcement.py', 'migrate.py', 'blobs.py', 'db.py'}]
        frames.extend({'file':'closure_diagnostic','function':name,'line':1} for name in CLOSURE_UNSAFE)
        frames.extend({'file':'closure_relation','function':':'.join(str(value) for value in row),'line':1} for row in CLOSURE_RELATIONS)
        position = getattr(getattr(error, 'diag', None), 'statement_position', None)
        # Positions are numeric metadata; never retain the failing SQL or message.
        if position is not None and str(position).isdigit():
            frames.append({'file': 'fixed_sql_statement', 'function': 'position', 'line': int(position)})
        category = next((label for phrase,label in [('is not a sequence','sequence_expected'),('is not a table','table_expected'),('is not a view','view_expected')] if phrase in str(getattr(getattr(error,'diag',None),'message_primary',''))),None)
        print(json.dumps({'status': 'FAIL', 'error_class': 'PGSmokeFailure' + (':' + category if category else ''), 'runtime_reason': PHASE,
                          'reason': code, 'frames': frames, 'raw_suppressed': True}), flush=True)
        raise SystemExit(1) from None
