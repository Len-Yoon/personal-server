"""Pure live PostgreSQL privilege closure, shared by installer and native bundle.

No runtime package imports or filesystem/host effects. Arbitrary extensions and
server-native code are outside the supported untrusted application threat model;
known server-file functions and every executable SECURITY DEFINER are checked.
"""
# Shared by the trusted installer and live controller observation. Effective
# privileges include PUBLIC and per-column grants; names come from bound values.
AUTHORITY_UNSAFE_SQL = """(
    (SELECT count(*)<>5 OR NOT bool_and(c.relkind='r' AND c.relpersistence='p' AND owner.rolsuper)
     FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
     JOIN pg_catalog.pg_roles owner ON owner.oid=c.relowner
     WHERE n.nspname='ha' AND c.relname IN ('control','identities','controller_probes','enforced_namespaces','enforced_roles'))
    OR NOT EXISTS(SELECT 1 FROM pg_catalog.pg_namespace n JOIN pg_catalog.pg_roles r ON r.oid=n.nspowner
        WHERE n.nspname='ha' AND r.rolsuper)
    OR EXISTS(SELECT 1 FROM persistent_tables c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace,
        LATERAL pg_catalog.aclexplode(c.relacl) acl LEFT JOIN pg_catalog.pg_roles grantee ON grantee.oid=acl.grantee
        WHERE n.nspname='ha' AND c.relname IN ('control','identities','controller_probes','enforced_namespaces','enforced_roles')
          AND (acl.grantee=0 OR NOT grantee.rolsuper)
          AND acl.privilege_type IN ('INSERT','UPDATE','DELETE','TRUNCATE','TRIGGER','REFERENCES'))
    OR EXISTS(SELECT 1 FROM persistent_tables c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_catalog.pg_attribute column_acl ON column_acl.attrelid=c.oid,
        LATERAL pg_catalog.aclexplode(column_acl.attacl) acl LEFT JOIN pg_catalog.pg_roles grantee ON grantee.oid=acl.grantee
        WHERE n.nspname='ha' AND c.relname IN ('control','identities','controller_probes','enforced_namespaces','enforced_roles')
          AND (acl.grantee=0 OR NOT grantee.rolsuper) AND acl.privilege_type IN ('INSERT','UPDATE','REFERENCES'))
    OR EXISTS(SELECT 1 FROM persistent_tables c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace,
        authority_principals reachable JOIN pg_catalog.pg_roles principal ON principal.oid=reachable.oid
        WHERE n.nspname='ha'
          AND c.relname IN ('control','identities','controller_probes','enforced_namespaces','enforced_roles')
          AND (has_table_privilege(principal.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES')
            OR has_any_column_privilege(principal.oid,c.oid,'INSERT,UPDATE,REFERENCES')))
    OR EXISTS(SELECT 1 FROM pg_catalog.pg_namespace n,
        LATERAL pg_catalog.aclexplode(n.nspacl) acl LEFT JOIN pg_catalog.pg_roles grantee ON grantee.oid=acl.grantee
        WHERE n.nspname='ha' AND acl.privilege_type='CREATE' AND (acl.grantee=0 OR NOT grantee.rolsuper))
    OR EXISTS(SELECT 1 FROM authority_principals reachable JOIN pg_catalog.pg_roles principal ON principal.oid=reachable.oid
        WHERE has_schema_privilege(principal.oid,'ha','CREATE'))
)"""


# Start only from non-TCB LOGIN roles. Follow every membership edge regardless
# of INHERIT/SET options: this deliberately overapproximates SET ROLE reachability
# and includes indirect paths, without rejecting unreachable predefined roles.
PRIVILEGE_CLOSURE_SQL = """WITH RECURSIVE subject AS (
    SELECT unnest(%s::name[]) AS role_name,unnest(%s::text[]) AS service),
authority_principals(oid) AS (
    SELECT oid FROM pg_catalog.pg_roles WHERE rolcanlogin AND NOT rolsuper
    UNION
    SELECT membership.roleid FROM pg_catalog.pg_auth_members membership
        JOIN authority_principals principal ON principal.oid=membership.member),
persistent_tables AS MATERIALIZED (SELECT c.* FROM pg_catalog.pg_class c
    WHERE c.relpersistence<>'t' AND c.relkind IN ('r','p','f','v','m')),
persistent_sequences AS MATERIALIZED (SELECT c.* FROM pg_catalog.pg_class c
    WHERE c.relpersistence<>'t' AND c.relkind='S')
SELECT a.role_name::text,(
    """ + AUTHORITY_UNSAFE_SQL + """
    OR NOT EXISTS(SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname=a.role_name AND r.rolcanlogin
        AND NOT (r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication OR r.rolbypassrls)
        AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member=r.oid OR m.roleid=r.oid))
    OR EXISTS(SELECT 1 FROM pg_catalog.pg_database d
        WHERE d.datdba=(SELECT oid FROM pg_catalog.pg_roles WHERE rolname=a.role_name)
            OR has_database_privilege(a.role_name,d.oid,'CREATE'))
    OR EXISTS(SELECT 1 FROM pg_catalog.pg_namespace n
        WHERE n.nspname !~ '^pg_(toast_)?temp_[0-9]+$'
          AND has_schema_privilege(a.role_name,n.oid,'CREATE'))
    OR EXISTS(SELECT 1 FROM persistent_sequences c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE true
          AND (has_sequence_privilege(a.role_name,c.oid,'USAGE,UPDATE')
            OR EXISTS(SELECT 1 FROM pg_catalog.aclexplode(c.relacl) acl WHERE acl.privilege_type IN ('USAGE','UPDATE')
                AND (acl.grantee=0 OR acl.grantee=(SELECT oid FROM pg_catalog.pg_roles WHERE rolname=a.role_name)))))
    OR EXISTS(SELECT 1 FROM persistent_tables c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE true
          AND (has_table_privilege(a.role_name,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES')
              OR has_any_column_privilege(a.role_name,c.oid,'INSERT,UPDATE,REFERENCES')
              OR EXISTS(SELECT 1 FROM pg_catalog.aclexplode(c.relacl) acl
                  WHERE acl.privilege_type IN ('INSERT','UPDATE','DELETE','TRUNCATE','TRIGGER','REFERENCES')
                    AND (acl.grantee=0 OR acl.grantee=(SELECT oid FROM pg_catalog.pg_roles WHERE rolname=a.role_name)))
              OR EXISTS(SELECT 1 FROM pg_catalog.pg_attribute column_acl,LATERAL pg_catalog.aclexplode(column_acl.attacl) acl
                  WHERE column_acl.attrelid=c.oid AND acl.privilege_type IN ('INSERT','UPDATE','REFERENCES')
                    AND (acl.grantee=0 OR acl.grantee=(SELECT oid FROM pg_catalog.pg_roles WHERE rolname=a.role_name))))
          AND NOT (c.relkind='r' AND c.relpersistence='p' AND c.relrowsecurity AND c.relforcerowsecurity
            AND EXISTS(SELECT 1 FROM pg_catalog.pg_roles owner WHERE owner.oid=c.relowner AND owner.rolsuper)
            AND EXISTS(SELECT 1 FROM ha.enforced_namespaces e WHERE e.schema_name=n.nspname AND e.service=a.service)
            AND EXISTS(SELECT 1 FROM pg_catalog.pg_trigger t WHERE t.tgrelid=c.oid AND t.tgname='ha_enforced_write'
                AND t.tgenabled='A' AND t.tgfoid='ha.enforced_write_guard()'::regprocedure
                AND t.tgtype=62 AND t.tgqual IS NULL AND t.tgattr::text='' AND t.tgnargs=0)
            AND EXISTS(SELECT 1 FROM pg_catalog.pg_trigger t WHERE t.tgrelid=c.oid AND t.tgname='ha_enforced_commit'
                AND t.tgenabled='A' AND t.tgfoid='ha.enforced_write_guard()'::regprocedure
                AND t.tgtype=29 AND t.tgdeferrable AND t.tginitdeferred
                AND t.tgqual IS NULL AND t.tgattr::text='' AND t.tgnargs=0)))
    OR EXISTS(SELECT 1 FROM persistent_tables c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE true
          AND (has_table_privilege(a.role_name,c.oid,'TRIGGER,REFERENCES')
            OR has_any_column_privilege(a.role_name,c.oid,'REFERENCES')
            OR (c.relname IN ('ha_receipts','ha_barriers','ha_blob_objects','ha_blob_chunks')
                AND (has_table_privilege(a.role_name,c.oid,'UPDATE,DELETE,TRUNCATE')
                    OR has_any_column_privilege(a.role_name,c.oid,'UPDATE')))))
    OR EXISTS(SELECT 1 FROM pg_catalog.pg_parameter_acl parameter
        WHERE has_parameter_privilege(a.role_name,parameter.parname,'ALTER SYSTEM')
          OR (EXISTS(SELECT 1 FROM pg_catalog.pg_settings setting WHERE setting.name=parameter.parname
                AND setting.context IN ('superuser','superuser-backend'))
              AND has_parameter_privilege(a.role_name,parameter.parname,'SET')))
    OR EXISTS(SELECT 1 FROM pg_catalog.pg_largeobject_metadata m JOIN pg_catalog.pg_roles r ON r.oid=m.lomowner
        WHERE r.rolname=a.role_name)
    OR EXISTS(SELECT 1 FROM pg_catalog.pg_largeobject_metadata m,
        LATERAL pg_catalog.aclexplode(m.lomacl) acl
        WHERE acl.privilege_type IN ('SELECT','UPDATE')
          AND (acl.grantee=0 OR acl.grantee=(SELECT oid FROM pg_catalog.pg_roles WHERE rolname=a.role_name)))
    OR has_column_privilege(a.role_name,'pg_catalog.pg_authid','rolpassword','SELECT')
    OR has_column_privilege(a.role_name,'pg_catalog.pg_subscription','subconninfo','SELECT')
    OR has_column_privilege(a.role_name,'pg_catalog.pg_user_mapping','umoptions','SELECT')
    OR EXISTS(SELECT 1 FROM pg_catalog.pg_proc p WHERE p.proname IN
        ('lo_export','lo_import','lo_create','lo_creat','lowrite','lo_put','lo_truncate','lo_truncate64','lo_from_bytea','lo_unlink',
         'pg_read_file','pg_read_binary_file','pg_write_file','pg_write_binary_file','pg_file_write','pg_file_rename','pg_file_unlink',
         'pg_ls_dir','pg_stat_file','pg_logdir_ls','pg_file_sync')
        AND has_function_privilege(a.role_name,p.oid,'EXECUTE'))
    OR EXISTS(SELECT 1 FROM pg_catalog.pg_proc p WHERE p.prosecdef
        AND has_function_privilege(a.role_name,p.oid,'EXECUTE')
        AND p.oid NOT IN ('ha.observe()'::regprocedure,'ha.enforced_read_allowed(name)'::regprocedure,
            'ha.enforced_session_ready(name,text)'::regprocedure,'ha.enforced_vm_blob_ready()'::regprocedure))
) FROM subject a ORDER BY a.role_name
"""


def privilege_closures_unsafe(admin, identities):
    """One uncached server catalog/effective-privilege query for every role."""
    roles = [role for role, _ in identities]
    services = [service for _, service in identities]
    if not roles or len(set(roles)) != len(roles):
        return True
    rows = admin.execute(PRIVILEGE_CLOSURE_SQL, (roles, services)).fetchall()
    return (len(rows) != len(roles) or {role for role, _ in rows} != set(roles)
            or any(unsafe is not False for _, unsafe in rows))


def privilege_closure_unsafe(admin, role, service):
    return privilege_closures_unsafe(admin, [(role, service)])

