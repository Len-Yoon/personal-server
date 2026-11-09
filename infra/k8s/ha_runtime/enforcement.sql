-- Installed only by an explicit, closed-authority administrator transaction.
CREATE TABLE IF NOT EXISTS ha.enforced_namespaces (
    schema_name name PRIMARY KEY, service text NOT NULL,
    definition_sha256 text NOT NULL CHECK(definition_sha256 ~ '^[0-9a-f]{64}$'),
    system_id text NOT NULL
);
CREATE TABLE IF NOT EXISTS ha.enforced_roles (
    role_name name PRIMARY KEY, service text NOT NULL, node text NOT NULL CHECK(node IN ('A','B')),
    generation bigint NOT NULL CHECK(generation>0)
);
REVOKE ALL ON ha.enforced_namespaces,ha.enforced_roles FROM PUBLIC;

CREATE OR REPLACE FUNCTION ha.require_enforced_writer(target_schema name) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,ha,pg_temp AS $$
DECLARE c ha.control; i ha.enforced_roles; n ha.enforced_namespaces; actual_node text; actual_timeline integer;
BEGIN
    -- Owners/superusers are the explicitly trusted migration/control plane.
    IF EXISTS(SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=session_user AND rolsuper) THEN RETURN; END IF;
    PERFORM pg_advisory_xact_lock_shared(7142026110601::bigint);
    SELECT * INTO STRICT i FROM ha.enforced_roles WHERE role_name=session_user;
    SELECT * INTO STRICT n FROM ha.enforced_namespaces WHERE schema_name=target_schema;
    -- Generation changes require the exclusive advisory lock. Renewal only extends
    -- this same authority and must coexist with admitted write transactions.
    SELECT * INTO STRICT c FROM ha.control WHERE singleton=1;
    actual_node := CASE current_setting('cluster_name') WHEN 'ha_A' THEN 'A' WHEN 'ha_B' THEN 'B' ELSE NULL END;
    IF NOT pg_is_in_recovery() THEN
        actual_timeline := ('x'||substring(pg_walfile_name(pg_current_wal_insert_lsn()),1,8))::bit(32)::integer;
    END IF;
    IF i.service<>n.service OR i.generation<>c.epoch OR pg_is_in_recovery() OR actual_node IS DISTINCT FROM c.primary_node
        OR current_setting('max_prepared_transactions')<>'0'
        OR current_setting('transaction_isolation')<>'read committed'
        OR n.system_id<>(SELECT system_identifier::text FROM pg_catalog.pg_control_system())
        OR actual_timeline IS DISTINCT FROM c.timeline
        OR NOT c.writes_open OR NOT c.history_safe OR c.expires_at<=clock_timestamp()
        OR c.expires_at>clock_timestamp()+interval '5 seconds'
        OR cardinality(c.serving) NOT BETWEEN 1 AND 2 OR array_position(c.serving,NULL) IS NOT NULL
        OR NOT c.serving<@ARRAY['A','B']::text[] OR cardinality(c.serving)<>(SELECT count(DISTINCT value) FROM unnest(c.serving) value)
        OR (c.mode='AS' AND cardinality(c.serving)<>1) OR (c.worker=ANY(c.serving)) IS NOT TRUE
        OR NOT EXISTS(SELECT 1 FROM ha.identities WHERE role_name=session_user AND service=i.service AND node=i.node)
        OR (CASE WHEN i.service IN ('portal-web','book-memo','youtube-memo','crawler-worker')
                THEN (i.node=ANY(c.serving)) IS NOT TRUE ELSE i.node IS DISTINCT FROM c.worker END) THEN
        RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='enforced_writer_denied';
    END IF;
END $$;
REVOKE ALL ON FUNCTION ha.require_enforced_writer(name) FROM PUBLIC;

CREATE OR REPLACE FUNCTION ha.enforced_write_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,ha,pg_temp AS $$
BEGIN
    PERFORM ha.require_enforced_writer(TG_TABLE_SCHEMA::name);
    RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION ha.enforced_write_guard() FROM PUBLIC;

CREATE OR REPLACE FUNCTION ha.enforced_read_allowed(target_schema name) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,ha,pg_temp AS $$
DECLARE i ha.enforced_roles; c ha.control; n ha.enforced_namespaces;
BEGIN
    SELECT * INTO STRICT i FROM ha.enforced_roles WHERE role_name=session_user;
    SELECT * INTO STRICT n FROM ha.enforced_namespaces WHERE schema_name=target_schema;
    SELECT * INTO STRICT c FROM ha.control WHERE singleton=1;
    RETURN current_setting('transaction_isolation')='read committed' AND i.service=n.service AND i.generation=c.epoch
        AND n.system_id=(SELECT system_identifier::text FROM pg_catalog.pg_control_system())
        AND c.expires_at>clock_timestamp() AND c.expires_at<=clock_timestamp()+interval '5 seconds'
        AND array_position(c.serving,NULL) IS NULL AND cardinality(c.serving) BETWEEN 1 AND 2
        AND c.serving<@ARRAY['A','B']::text[]
        AND CASE WHEN i.service IN ('portal-web','book-memo','youtube-memo','crawler-worker')
            THEN (i.node=ANY(c.serving)) IS TRUE ELSE i.node=c.worker END;
EXCEPTION WHEN NO_DATA_FOUND THEN RETURN false;
END $$;
REVOKE ALL ON FUNCTION ha.enforced_read_allowed(name) FROM PUBLIC;

CREATE OR REPLACE FUNCTION ha.enforced_session_ready(target_schema name,expected_definition text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,ha,pg_temp AS $$
DECLARE i ha.enforced_roles;
BEGIN
    SELECT * INTO STRICT i FROM ha.enforced_roles WHERE role_name=session_user;
    IF NOT EXISTS(SELECT 1 FROM ha.enforced_namespaces n,ha.control c WHERE n.schema_name=target_schema
        AND n.service=i.service AND n.definition_sha256=expected_definition AND c.singleton=1 AND c.epoch=i.generation
        AND n.system_id=(SELECT system_identifier::text FROM pg_catalog.pg_control_system())) THEN RETURN false; END IF;
    RETURN NOT EXISTS(SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND c.relkind IN ('r','p','f','v','m')
        AND (has_table_privilege(session_user,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES')
            OR has_any_column_privilege(session_user,c.oid,'INSERT,UPDATE,REFERENCES'))
        AND NOT EXISTS(SELECT 1 FROM ha.enforced_namespaces e WHERE e.schema_name=n.nspname AND e.service=i.service
            AND EXISTS(SELECT 1 FROM pg_catalog.pg_trigger t WHERE t.tgrelid=c.oid AND t.tgname='ha_enforced_write'
                AND t.tgenabled='A' AND t.tgfoid='ha.enforced_write_guard()'::regprocedure)))
        AND EXISTS(SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=target_schema AND c.relkind='r')
        AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=target_schema AND c.relkind IN ('r','p','f','v','m') AND (c.relkind<>'r' OR c.relpersistence<>'p'
        OR NOT EXISTS(SELECT 1 FROM pg_catalog.pg_trigger t WHERE t.tgrelid=c.oid AND t.tgname='ha_enforced_write'
            AND t.tgenabled='A' AND t.tgfoid='ha.enforced_write_guard()'::regprocedure)));
EXCEPTION WHEN NO_DATA_FOUND THEN RETURN false;
END $$;
REVOKE ALL ON FUNCTION ha.enforced_session_ready(name,text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION ha.enforced_vm_blob_ready() RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,ha,pg_temp AS $$
DECLARE s name; i ha.enforced_roles;
BEGIN
    SELECT * INTO STRICT i FROM ha.enforced_roles WHERE role_name=session_user;
    s := ('s_'||replace(i.service,'-','_')||'_state')::name;
    IF i.service<>'portal-web' OR NOT EXISTS(SELECT 1 FROM ha.enforced_namespaces WHERE schema_name=s AND service=i.service) THEN RETURN false; END IF;
    RETURN (SELECT count(*)=2 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=s AND c.relname IN ('ha_blob_objects','ha_blob_chunks') AND c.relkind='r'
        AND EXISTS(SELECT 1 FROM pg_catalog.pg_trigger t WHERE t.tgrelid=c.oid AND t.tgname='ha_enforced_write'
            AND t.tgenabled='A' AND t.tgfoid='ha.enforced_write_guard()'::regprocedure)
        AND EXISTS(SELECT 1 FROM pg_catalog.pg_trigger t WHERE t.tgrelid=c.oid AND t.tgname='ha_enforced_commit'
            AND t.tgenabled='A' AND t.tgfoid='ha.enforced_write_guard()'::regprocedure)
        AND EXISTS(SELECT 1 FROM pg_catalog.pg_trigger t WHERE t.tgrelid=c.oid AND t.tgname='ha_blob_quota'
            AND t.tgenabled='A' AND t.tgfoid='ha.enforced_blob_quota()'::regprocedure)
        AND (c.relname<>'ha_blob_objects' OR EXISTS(SELECT 1 FROM pg_catalog.pg_trigger t WHERE t.tgrelid=c.oid
            AND t.tgname='ha_blob_complete' AND t.tgenabled='A' AND t.tgdeferrable AND t.tginitdeferred
            AND t.tgfoid='ha.enforced_blob_complete()'::regprocedure)));
EXCEPTION WHEN NO_DATA_FOUND THEN RETURN false;
END $$;
REVOKE ALL ON FUNCTION ha.enforced_vm_blob_ready() FROM PUBLIC;

CREATE OR REPLACE FUNCTION ha.enforced_blob_quota() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,ha,pg_temp AS $$
DECLARE objects bigint; used bigint; item record;
BEGIN
    PERFORM ha.require_enforced_writer(TG_TABLE_SCHEMA::name);
    IF TG_TABLE_NAME='ha_blob_objects' THEN
        PERFORM pg_advisory_xact_lock(hashtextextended('blob-quota:'||TG_TABLE_SCHEMA,0));
        EXECUTE format('SELECT count(*),coalesce(sum(size),0) FROM %I.ha_blob_objects',TG_TABLE_SCHEMA) INTO objects,used;
        EXECUTE format('SELECT size FROM %I.ha_blob_objects WHERE sha256=$1',TG_TABLE_SCHEMA) INTO item USING NEW.sha256;
        IF item.size IS NULL AND (objects>=10000 OR used+NEW.size>1073741824) THEN
            RAISE EXCEPTION USING ERRCODE='54000',MESSAGE='enforced_blob_quota_exceeded';
        END IF;
    ELSE
        EXECUTE format('SELECT size,chunk_count FROM %I.ha_blob_objects WHERE sha256=$1 FOR UPDATE',TG_TABLE_SCHEMA) INTO STRICT item USING NEW.sha256;
        IF NEW.ordinal<0 OR NEW.ordinal>=item.chunk_count OR octet_length(NEW.payload)<>least(65536,item.size-NEW.ordinal::bigint*65536) THEN
            RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='enforced_blob_chunk_invalid';
        END IF;
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION ha.enforced_blob_quota() FROM PUBLIC;

CREATE OR REPLACE FUNCTION ha.enforced_blob_complete() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,ha,pg_temp AS $$
DECLARE item record;
BEGIN
    EXECUTE format('SELECT count(*) AS chunks,coalesce(sum(octet_length(payload)),0) AS bytes,encode(sha256(coalesce(string_agg(payload,''''::bytea ORDER BY ordinal),''''::bytea)),''hex'') AS digest FROM %I.ha_blob_chunks WHERE sha256=$1',TG_TABLE_SCHEMA) INTO item USING NEW.sha256;
    IF item.chunks<>NEW.chunk_count OR item.bytes<>NEW.size OR item.digest<>NEW.sha256 THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='enforced_blob_incomplete';
    END IF;
    RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION ha.enforced_blob_complete() FROM PUBLIC;
