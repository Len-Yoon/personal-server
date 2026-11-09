-- Isolated-development authority. No promotion/fencing/production bootstrap.
CREATE SCHEMA ha;
REVOKE ALL ON SCHEMA ha FROM PUBLIC;
CREATE TABLE ha.control (
    singleton integer PRIMARY KEY CHECK(singleton=1),
    cluster text NOT NULL, epoch bigint NOT NULL, revision bigint NOT NULL,
    primary_node text NOT NULL CHECK(primary_node IN ('A','B')),
    partner text NOT NULL CHECK(partner IN ('A','B') AND partner<>primary_node),
    timeline integer NOT NULL, serving text[] NOT NULL,
    worker text NOT NULL CHECK(worker IN ('A','B')), mode text NOT NULL CHECK(mode IN ('AS','AA')),
    writes_open boolean NOT NULL, history_safe boolean NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE TABLE ha.identities (role_name text PRIMARY KEY, service text NOT NULL, node text NOT NULL);
-- Host coordinator only; application roles never receive table privileges.
CREATE TABLE ha.controller_probes (nonce text PRIMARY KEY, generation bigint NOT NULL);
REVOKE ALL ON ALL TABLES IN SCHEMA ha FROM PUBLIC;

CREATE FUNCTION ha.observe() RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,ha,pg_temp AS $$
DECLARE
    c ha.control; i ha.identities; recovery boolean; local_node text;
    flush_lsn pg_lsn; replay_lsn pg_lsn; source_tli integer; actual_tli integer;
    peer_sync text; expected_names text; actual_names text; systemid text;
BEGIN
    SELECT * INTO STRICT c FROM ha.control WHERE singleton=1;
    SELECT * INTO STRICT i FROM ha.identities WHERE role_name=session_user;
    recovery := pg_is_in_recovery();
    local_node := CASE current_setting('cluster_name') WHEN 'ha_A' THEN 'A' WHEN 'ha_B' THEN 'B' ELSE NULL END;
    SELECT system_identifier::text INTO systemid FROM pg_control_system();
    IF recovery THEN
        flush_lsn := pg_last_wal_receive_lsn();
        replay_lsn := pg_last_wal_replay_lsn();
        SELECT received_tli INTO source_tli FROM pg_catalog.pg_stat_wal_receiver;
        actual_tli := source_tli;
    ELSE
        flush_lsn := pg_current_wal_flush_lsn(); replay_lsn := flush_lsn;
        actual_tli := ('x'||substring(pg_walfile_name(pg_current_wal_insert_lsn()),1,8))::bit(32)::integer;
        source_tli := actual_tli;
        SELECT sync_state INTO peer_sync FROM pg_catalog.pg_stat_replication
          WHERE application_name='ha_'||lower(c.partner) AND state='streaming';
    END IF;
    expected_names := 'FIRST1(ha_'||lower(c.partner)||')';
    actual_names := regexp_replace(current_setting('synchronous_standby_names'),'[[:space:]"]','','g');
    RETURN jsonb_build_object(
        'cluster',c.cluster,'system_id',systemid,'epoch',c.epoch,'revision',c.revision,
        'primary',c.primary_node,'partner',c.partner,'timeline',c.timeline,
        'actual_timeline',actual_tli,'source_timeline',source_tli,
        'serving',c.serving,'worker',c.worker,'mode',c.mode,'writes_open',c.writes_open,
        'history_safe',c.history_safe,'remaining',extract(epoch FROM c.expires_at-clock_timestamp()),
        'valid',c.expires_at>clock_timestamp() AND local_node IS NOT NULL,
        'node',local_node,'identity_service',i.service,'identity_node',i.node,
        'recovery',recovery,'flush',flush_lsn::text,'replay',replay_lsn::text,
        'sync_state',peer_sync,'configured_partner',CASE WHEN actual_names=expected_names THEN c.partner ELSE NULL END,
        'fsync',current_setting('fsync')='on','full_page_writes',current_setting('full_page_writes')='on'
    );
END $$;
REVOKE ALL ON FUNCTION ha.observe() FROM PUBLIC;

-- Only the isolated fixture owner may use this; app roles receive no EXECUTE.
-- Taking the exclusive lock is mandatory for every authority change. Windows
-- coordinator, fencing, stale-node rejoin and production permits remain absent.
CREATE FUNCTION ha.local_roles(new_serving text[], new_worker text, new_writes boolean)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,ha,pg_temp AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(7142026110601);
    IF cardinality(new_serving)<>1 OR NOT new_serving<@ARRAY['A','B']::text[] OR new_worker NOT IN ('A','B') THEN
        RAISE EXCEPTION 'invalid isolated role assignment';
    END IF;
    UPDATE ha.control SET serving=new_serving,worker=new_worker,writes_open=new_writes,
        epoch=epoch+1,revision=revision+1 WHERE singleton=1;
END $$;
REVOKE ALL ON FUNCTION ha.local_roles(text[],text,boolean) FROM PUBLIC;
