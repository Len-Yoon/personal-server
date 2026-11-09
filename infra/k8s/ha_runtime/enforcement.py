"""Explicit server-side writer enforcement installation; no production CLI.

Each admitted transaction holds the generation lock until completion. Expiry
rejects new statements; a previously admitted transaction can delay a transition.
Deferred checks are supplementary, not an unbypassable PRE_COMMIT callback.
Positive VM fencing is still mandatory before promoting a different PG server.
Temporary session scratch objects are outside the persistent-data guarantee.
"""
import hashlib
from pathlib import Path
import re

from .config import DatabaseUnavailable, SERVICES

PROFILE = 'enforced-vm-v1'


def definition_sha256():
    return hashlib.sha256(Path(__file__).with_suffix('.sql').read_bytes()).hexdigest()


def _install_namespace(admin, schema, service, roles, *, generation=None):
    """Called with an existing trusted administrator, never application input."""
    if (not isinstance(service,str) or service not in SERVICES or not isinstance(schema,str)
            or not re.fullmatch(r's_[a-z][a-z0-9_]{1,60}', schema)
            or not schema.startswith('s_' + service.replace('-', '_') + '_')
            or not isinstance(roles,(list,tuple)) or not roles
            or any(not isinstance(role,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', role) for role in roles)
            or len(set(roles)) != len(roles)):
        raise DatabaseUnavailable('invalid_enforced_namespace')
    from psycopg import sql
    with admin.transaction():
        admin.execute('SELECT pg_advisory_xact_lock(%s)', (7142026110601,))
        closed = admin.execute('SELECT NOT writes_open AND expires_at<=clock_timestamp(),epoch FROM ha.control WHERE singleton=1 FOR UPDATE').fetchone()
        if not closed or closed[0] is not True or type(closed[1]) is not int or closed[1]<1:
            raise DatabaseUnavailable('closed_authority_required_for_install')
        target_generation=closed[1] if generation is None else generation
        if type(target_generation) is not int or not closed[1]<=target_generation<=closed[1]+1:
            raise DatabaseUnavailable('current_or_next_generation_required')
        owner = admin.execute('SELECT rolsuper FROM pg_roles WHERE rolname=session_user').fetchone()
        if owner != (True,):
            raise DatabaseUnavailable('trusted_installer_required')
        if admin.execute("SELECT current_setting('max_prepared_transactions')='0'").fetchone() != (True,):
            raise DatabaseUnavailable('two_phase_transactions_unsupported')
        schemas = admin.execute('SELECT n.nspname,r.rolsuper FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner WHERE n.nspname=ANY(%s)', (['ha',schema],)).fetchall()
        if len(schemas)!=2 or not all(row[1] for row in schemas):
            raise DatabaseUnavailable('trusted_schema_owner_required')
        authority = admin.execute('''SELECT c.relname,c.relkind,c.relpersistence,r.rolsuper
            FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_catalog.pg_roles r ON r.oid=c.relowner WHERE n.nspname='ha'
              AND c.relname IN ('control','identities','controller_probes')''').fetchall()
        if len(authority)!=3 or any(kind!='r' or persistence!='p' or trusted is not True for _,kind,persistence,trusted in authority):
            raise DatabaseUnavailable('trusted_authority_owner_required')
        bad_existing = admin.execute('''SELECT
            EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_roles r ON r.oid=c.relowner
              WHERE n.nspname='ha' AND c.relname IN ('enforced_namespaces','enforced_roles') AND (NOT r.rolsuper OR c.relkind<>'r')),
            EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_roles r ON r.oid=p.proowner
              WHERE n.nspname='ha' AND p.proname IN ('observe','require_enforced_writer','enforced_write_guard','enforced_read_allowed','enforced_session_ready',
                'enforced_vm_blob_ready','enforced_blob_quota','enforced_blob_complete') AND NOT r.rolsuper)''').fetchone()
        if any(bad_existing):
            raise DatabaseUnavailable('trusted_enforcement_owner_required')
        policies = []
        for role in roles:
            row = admin.execute('''SELECT i.service,i.node,r.rolsuper,r.rolcreaterole,r.rolcreatedb,r.rolreplication,r.rolbypassrls,
                EXISTS(SELECT 1 FROM pg_auth_members m WHERE m.member=r.oid OR m.roleid=r.oid),
                EXISTS(SELECT 1 FROM pg_class c WHERE c.relowner=r.oid),
                EXISTS(SELECT 1 FROM pg_namespace n WHERE n.nspowner=r.oid)
                FROM ha.identities i JOIN pg_roles r ON r.rolname=i.role_name WHERE i.role_name=%s''', (role,)).fetchone()
            if not row or row[0] != service or row[1] not in {'A','B'} or any(row[2:]):
                raise DatabaseUnavailable('nonprivileged_application_role_required')
            policies.append((role, row[0], row[1], target_generation))
        tables = admin.execute('''SELECT c.relname,c.relkind,c.relpersistence,r.rolsuper FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_roles r ON r.oid=c.relowner
            WHERE n.nspname=%s AND c.relkind IN ('r','p','f','v','m') ORDER BY c.relname''', (schema,)).fetchall()
        if not tables or any(kind != 'r' or persistence != 'p' or not trusted for _,kind,persistence,trusted in tables):
            raise DatabaseUnavailable('trusted_logged_tables_required')
        admin.execute(Path(__file__).with_suffix('.sql').read_text())
        # This dedicated database profile does not expose unguarded large-object
        # mutation. Revoke the default PUBLIC grants as well as explicit grants.
        lo_mutators = ['lo_creat','lo_create','lo_unlink','lo_import','lowrite','lo_put','lo_truncate','lo_truncate64','lo_from_bytea']
        functions = admin.execute("SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='pg_catalog' AND p.proname=ANY(%s)", (lo_mutators,)).fetchall()
        for signature, in functions:
            admin.execute(sql.SQL('REVOKE EXECUTE ON FUNCTION {} FROM PUBLIC').format(sql.SQL(signature)))
        system_id = admin.execute('SELECT system_identifier::text FROM pg_control_system()').fetchone()[0]
        admin.execute('INSERT INTO ha.enforced_namespaces VALUES(%s,%s,%s,%s) ON CONFLICT(schema_name) DO UPDATE SET service=excluded.service,definition_sha256=excluded.definition_sha256,system_id=excluded.system_id', (schema, service, definition_sha256(), system_id))
        for policy in policies:
            admin.execute('INSERT INTO ha.enforced_roles VALUES(%s,%s,%s,%s) ON CONFLICT(role_name) DO NOTHING', policy)
            if admin.execute('SELECT role_name::text,service,node,generation FROM ha.enforced_roles WHERE role_name=%s', (policy[0],)).fetchone() != policy:
                raise DatabaseUnavailable('fresh_generation_credentials_required')
        for name, *_ in tables:
            table = sql.Identifier(schema, name)
            policies_existing = admin.execute('SELECT polname FROM pg_policy WHERE polrelid=%s::regclass', (schema+'.'+name,)).fetchall()
            if any(policy[0] not in {'ha_enforced_read','ha_enforced_mutation'} for policy in policies_existing):
                raise DatabaseUnavailable('unexpected_row_policy_rejected')
            for trigger in ('ha_enforced_write','ha_enforced_commit'):
                admin.execute(sql.SQL('DROP TRIGGER IF EXISTS {} ON {}').format(sql.Identifier(trigger), table))
            admin.execute(sql.SQL('CREATE TRIGGER ha_enforced_write BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON {} FOR EACH STATEMENT EXECUTE FUNCTION ha.enforced_write_guard()').format(table))
            admin.execute(sql.SQL('CREATE CONSTRAINT TRIGGER ha_enforced_commit AFTER INSERT OR UPDATE OR DELETE ON {} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ha.enforced_write_guard()').format(table))
            for trigger in ('ha_enforced_write','ha_enforced_commit'):
                admin.execute(sql.SQL('ALTER TABLE {} ENABLE ALWAYS TRIGGER {}').format(table, sql.Identifier(trigger)))
            admin.execute(sql.SQL('ALTER TABLE {} ENABLE ROW LEVEL SECURITY').format(table))
            admin.execute(sql.SQL('ALTER TABLE {} FORCE ROW LEVEL SECURITY').format(table))
            admin.execute(sql.SQL('DROP POLICY IF EXISTS ha_enforced_read ON {}').format(table))
            admin.execute(sql.SQL('DROP POLICY IF EXISTS ha_enforced_mutation ON {}').format(table))
            admin.execute(sql.SQL('CREATE POLICY ha_enforced_read ON {} FOR SELECT USING(ha.enforced_read_allowed({}::name))').format(table,sql.Literal(schema)))
            admin.execute(sql.SQL('CREATE POLICY ha_enforced_mutation ON {} FOR ALL USING(ha.enforced_read_allowed({}::name)) WITH CHECK(ha.enforced_read_allowed({}::name))').format(table,sql.Literal(schema),sql.Literal(schema)))
            if name in {'ha_blob_objects','ha_blob_chunks'}:
                admin.execute(sql.SQL('DROP TRIGGER IF EXISTS ha_blob_quota ON {}').format(table))
                admin.execute(sql.SQL('CREATE TRIGGER ha_blob_quota BEFORE INSERT ON {} FOR EACH ROW EXECUTE FUNCTION ha.enforced_blob_quota()').format(table))
                admin.execute(sql.SQL('ALTER TABLE {} ENABLE ALWAYS TRIGGER ha_blob_quota').format(table))
            if name == 'ha_blob_objects':
                admin.execute(sql.SQL('DROP TRIGGER IF EXISTS ha_blob_complete ON {}').format(table))
                admin.execute(sql.SQL('CREATE CONSTRAINT TRIGGER ha_blob_complete AFTER INSERT ON {} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ha.enforced_blob_complete()').format(table))
                admin.execute(sql.SQL('ALTER TABLE {} ENABLE ALWAYS TRIGGER ha_blob_complete').format(table))
        for role in roles:
            identity = sql.Identifier(role)
            admin.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(schema),identity))
            admin.execute(sql.SQL('GRANT USAGE ON SCHEMA ha TO {}').format(identity))
            admin.execute(sql.SQL('GRANT EXECUTE ON FUNCTION ha.observe() TO {}').format(identity))
            for signature, in functions:
                admin.execute(sql.SQL('REVOKE EXECUTE ON FUNCTION {} FROM {}').format(sql.SQL(signature),identity))
            admin.execute(sql.SQL('REVOKE CREATE ON SCHEMA public,ha,{} FROM {}').format(sql.Identifier(schema),identity))
            admin.execute(sql.SQL('REVOKE ALL ON ALL SEQUENCES IN SCHEMA {} FROM {}').format(sql.Identifier(schema),identity))
            admin.execute(sql.SQL('REVOKE ALL ON ALL SEQUENCES IN SCHEMA {} FROM PUBLIC').format(sql.Identifier(schema)))
            admin.execute(sql.SQL('REVOKE ALL ON ha.enforced_roles,ha.enforced_namespaces FROM {}').format(identity))
            admin.execute(sql.SQL('GRANT EXECUTE ON FUNCTION ha.enforced_vm_blob_ready() TO {}').format(identity))
            admin.execute(sql.SQL('GRANT EXECUTE ON FUNCTION ha.enforced_session_ready(name,text) TO {}').format(identity))
            admin.execute(sql.SQL('GRANT EXECUTE ON FUNCTION ha.enforced_read_allowed(name) TO {}').format(identity))
            for table, *_ in tables:
                admin.execute(sql.SQL('REVOKE ALL ON {} FROM PUBLIC').format(sql.Identifier(schema,table)))
                admin.execute(sql.SQL('REVOKE ALL ON {} FROM {}').format(sql.Identifier(schema,table),identity))
                columns = admin.execute('SELECT attname FROM pg_attribute WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped', (schema+'.'+table,)).fetchall()
                if columns:
                    admin.execute(sql.SQL('REVOKE ALL ({}) ON {} FROM PUBLIC,{}').format(sql.SQL(',').join(sql.Identifier(row[0]) for row in columns),sql.Identifier(schema,table),identity))
                permissions = 'SELECT,INSERT' if table in {'ha_blob_objects','ha_blob_chunks','ha_receipts','ha_barriers'} else 'SELECT,INSERT,UPDATE,DELETE'
                admin.execute(sql.SQL('GRANT '+permissions+' ON {} TO {}').format(sql.Identifier(schema,table),identity))
                if table in {'ha_blob_objects','ha_blob_chunks','ha_receipts','ha_barriers'}:
                    admin.execute(sql.SQL('REVOKE UPDATE,DELETE,TRUNCATE ON {} FROM {}').format(sql.Identifier(schema,table),identity))
            unsafe = admin.execute('''SELECT
                has_schema_privilege(%s,'public','CREATE') OR has_schema_privilege(%s,'ha','CREATE') OR has_schema_privilege(%s,%s,'CREATE'),
                has_table_privilege(%s,'ha.control','INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES')
                  OR has_table_privilege(%s,'ha.identities','INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES'),
                EXISTS(SELECT 1 FROM pg_proc p WHERE p.prosecdef AND has_function_privilege(%s,p.oid,'EXECUTE')
                    AND p.oid NOT IN ('ha.observe()'::regprocedure,'ha.enforced_read_allowed(name)'::regprocedure,'ha.enforced_session_ready(name,text)'::regprocedure,'ha.enforced_vm_blob_ready()'::regprocedure))
                ''', (role,role,role,schema,role,role,role)).fetchone()
            if not unsafe or any(unsafe):
                raise DatabaseUnavailable('application_privilege_closure_required')
            for protected in ('ha.control','ha.identities','ha.enforced_roles','ha.enforced_namespaces'):
                privileges = admin.execute('SELECT has_table_privilege(%s,%s,\'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES\') OR has_any_column_privilege(%s,%s,\'INSERT,UPDATE,REFERENCES\')', (role,protected,role,protected)).fetchone()
                if privileges != (False,):
                    raise DatabaseUnavailable('application_control_privileges_rejected')
            for table, *_ in tables:
                if table in {'ha_blob_objects','ha_blob_chunks','ha_receipts','ha_barriers'}:
                    privileges = admin.execute('SELECT has_table_privilege(%s,%s,\'UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES\') OR has_any_column_privilege(%s,%s,\'UPDATE,REFERENCES\')', (role,schema+'.'+table,role,schema+'.'+table)).fetchone()
                    if privileges != (False,):
                        raise DatabaseUnavailable('immutable_table_privileges_rejected')
        return {'profile': PROFILE, 'schema': schema, 'tables': len(tables), 'definition_sha256': definition_sha256()}


from ha_control.privilege_closure import (PRIVILEGE_CLOSURE_SQL,
    privilege_closure_unsafe, privilege_closures_unsafe)


def _revoke_settings_updates(admin, roles):
    """Explicit dedicated-profile scope: UPDATE only on the pg_settings view."""
    if any(not isinstance(role,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,62}',role) for role in roles):
        raise DatabaseUnavailable('invalid_enforced_settings_role')
    from psycopg import sql
    columns=admin.execute("SELECT attname FROM pg_catalog.pg_attribute WHERE attrelid='pg_catalog.pg_settings'::regclass AND attnum>0 AND NOT attisdropped ORDER BY attnum").fetchall()
    if not columns:
        raise DatabaseUnavailable('settings_update_scope_unverified')
    admin.execute('REVOKE UPDATE ON pg_catalog.pg_settings FROM PUBLIC')
    attributes=sql.SQL(',').join(sql.Identifier(name) for name, in columns)
    admin.execute(sql.SQL('REVOKE UPDATE ({}) ON pg_catalog.pg_settings FROM PUBLIC').format(attributes))
    for role in roles:
        identity=sql.Identifier(role)
        admin.execute(sql.SQL('REVOKE UPDATE ON pg_catalog.pg_settings FROM {}').format(identity))
        admin.execute(sql.SQL('REVOKE UPDATE ({}) ON pg_catalog.pg_settings FROM {}').format(attributes,identity))


def install_namespaces(admin, entries, *, generation=None):
    """Install an entire privilege domain atomically under closed authority.

    A single namespace wrapper remains strict: an app with writes in another
    unguarded namespace cannot commit partial protection or increased grants.
    """
    if not isinstance(entries, (list, tuple)) or not 1 <= len(entries) <= 256:
        raise DatabaseUnavailable('invalid_enforced_namespace_batch')
    schemas = set()
    identities = {}
    for entry in entries:
        if not isinstance(entry, (tuple, list)) or len(entry) != 3:
            raise DatabaseUnavailable('invalid_enforced_namespace_batch')
        schema, service, roles = entry
        if (not isinstance(service,str) or service not in SERVICES or not isinstance(schema,str)
                or not re.fullmatch(r's_[a-z][a-z0-9_]{1,60}', schema)
                or not schema.startswith('s_' + service.replace('-', '_') + '_')
                or schema in schemas or not isinstance(roles,(list,tuple)) or not 1 <= len(roles) <= 128
                or any(not isinstance(role,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', role) for role in roles)
                or len(set(roles)) != len(roles)):
            raise DatabaseUnavailable('invalid_enforced_namespace_batch')
        schemas.add(schema)
        for role in roles:
            if role in identities and identities[role] != service:
                raise DatabaseUnavailable('invalid_enforced_namespace_batch')
            identities[role] = service
    with admin.transaction():
        admin.execute('SELECT pg_advisory_xact_lock(%s)', (7142026110601,))
        closed = admin.execute('SELECT NOT writes_open AND expires_at<=clock_timestamp() FROM ha.control WHERE singleton=1 FOR UPDATE').fetchone()
        if closed != (True,):
            raise DatabaseUnavailable('closed_authority_required_for_install')
        result = [_install_namespace(admin, schema, service, roles, generation=generation)
                  for schema, service, roles in entries]
        # Validate all registered identities, including older generation roles
        # which retain SQL grants, before any batch grant becomes visible.
        registered = admin.execute('SELECT role_name::text,service FROM ha.enforced_roles ORDER BY role_name').fetchall()
        _revoke_settings_updates(admin,[role for role,_ in registered])
        if privilege_closures_unsafe(admin, registered):
            raise DatabaseUnavailable('application_global_privilege_closure_required')
        return result


def install_namespace(admin, schema, service, roles, *, generation=None):
    return install_namespaces(admin, [(schema, service, roles)], generation=generation)[0]
