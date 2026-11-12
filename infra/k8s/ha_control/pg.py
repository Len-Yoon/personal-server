"""Native Windows fencing plus authenticated PostgreSQL transition adapter.

Only an approved private native plan can arm this adapter. No automatic failure
threshold, destructive PGDATA clone, generic operator SQL or credential export
is implemented. All database/transport failures are static fail-closed labels.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
import uuid

from .controller import TransitionBlocked
from .windows import WindowsFence

ROLE_LOCK = 7142026110601
LEASE_SECONDS = 5
PROOF_SECONDS = 2
ENFORCED_SERVICES = frozenset({'portal-web','book-memo','youtube-memo','crawler-worker','car-care-worker','homeops-executor','system-agent'})


def _deny(reason):
    raise TransitionBlocked(reason) from None


def _private_file(value):
    path = Path(value)
    try:
        info = path.lstat()
        if (not path.is_absolute() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or any(p.is_symlink() for p in (path, *path.parents))
                or (sys.platform != "win32" and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
            _deny("private_pg_configuration_required")
    except OSError:
        _deny("private_pg_configuration_required")
    return path


class PgBackend:
    def __init__(self, plan, approval, *, fence=None):
        if fence is not None:
            self.fence_adapter = fence
        else:
            from .legacy_guard import pairs
            try:
                raw = _private_file(plan).read_bytes()
                if len(raw)>1048576:
                    _deny('private_configuration_required')
                candidate = json.loads(raw,object_pairs_hook=pairs)
                profile = candidate.get('profile')
                if 'initial_wsl_cutover' in candidate:
                    from .initial_windows_gate import InitialWindowsGate
                    self.fence_adapter = InitialWindowsGate(plan, approval)
                elif profile is None:
                    self.fence_adapter = WindowsFence(plan, approval)
                elif profile == 'enforced-vm-v1':
                    from .enforced_windows import EnforcedWindowsFence
                    self.fence_adapter = EnforcedWindowsFence(plan,approval)
                else:
                    _deny('unsupported_fence_profile')
            except TransitionBlocked:
                raise
            except Exception:
                _deny('private_configuration_required')
        self.plan_path = Path(plan)
        self.depth = 0
        self.plan = None
        self.validated = None

    @contextmanager
    def guard(self):
        # CLI owns an outer host guard for serve/plan binding. Controller's
        # nested guard must reuse that exact process and mutex, never restart it.
        if self.depth:
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1
            return
        with self.fence_adapter.guard():
            self.plan = self.fence_adapter.plan_data
            self._plan()
            self.depth = 1
            try:
                yield
            finally:
                self.depth = 0
                self.plan = None
                self.validated = None

    def _plan(self):
        value = self.plan
        # This marker is injected by WindowsFence only after its native guard
        # has bound the exact approval bytes/hash/action to the private plan.
        # A fence-only approval never authorizes PostgreSQL state changes.
        if isinstance(value, dict):
            expected = 'ha-initial-wsl-cutover' if 'initial_wsl_cutover' in value else 'ha-transition'
            if value.get('_approval_action') != expected:
                _deny('ha_transition_approval_required')
        if (not isinstance(value, dict) or not isinstance(value.get("cluster"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value["cluster"])
                or value.get("node") not in {"A", "B"} or type(value.get("generation")) is not int
                or value["generation"] < 1 or type(value.get("timeline")) is not int or value["timeline"] < 1
                or not re.fullmatch(r"[0-9]+", str(value.get("system_id", "")))
                or not isinstance(value.get("pg_nodes"), dict) or set(value["pg_nodes"]) != {"A", "B"}):
            _deny("invalid_pg_controller_plan")
        for endpoint in value["pg_nodes"].values():
            allowed = {"host", "port", "dbname", "user", "sslmode", "sslrootcert", "passfile"}
            if (not isinstance(endpoint, dict) or set(endpoint) != allowed
                    or endpoint["sslmode"] != "verify-full" or type(endpoint["port"]) is not int
                    or not 1 <= endpoint["port"] <= 65535
                    or any(not isinstance(endpoint[k], str) or not endpoint[k] for k in ("host", "dbname", "user"))
                    or not re.fullmatch(r"[A-Za-z0-9_.-]+", endpoint["host"])):
                _deny("authenticated_pg_endpoints_required")
            _private_file(endpoint["passfile"])
            # The Windows plan directory ACL/owner is also checked by the native
            # bridge. CA and passfile must be real, nonsymlink private files.
            _private_file(endpoint["sslrootcert"])
        if value["pg_nodes"]["A"] == value["pg_nodes"]["B"]:
            _deny("distinct_pg_endpoints_required")
        if value.get('profile') is not None:
            contract = value.get('pg_enforcement')
            expected = hashlib.sha256((Path(__file__).parent.parent/'ha_runtime/enforcement.sql').read_bytes()).hexdigest()
            if (value.get('profile')!='enforced-vm-v1' or value.get('schema_version')!=2
                    or not isinstance(contract,dict) or set(contract)!={'definition_sha256','role_sets','namespace_tables','effects'}
                    or contract['definition_sha256']!=expected or not isinstance(contract['role_sets'],dict)
                    or set(contract['role_sets'])!={'A','B'}):
                _deny('enforced_pg_plan_required')
            from .prepare import EFFECTS_SOURCES, TARGETS, effects_runtime_sha256
            try:
                source_files = {path: hashlib.sha256((Path(__file__).parents[1] / TARGETS[path]).read_bytes()).hexdigest()
                                for path in EFFECTS_SOURCES}
                effects = contract['effects']
                base_fields = {'code_sha256', 'broker_role'}
                if (type(effects) is not dict or set(effects) not in (base_fields, base_fields | {'deployment_binding'})
                        or effects['code_sha256'] != effects_runtime_sha256(source_files)
                        or type(effects['broker_role']) is not str
                        or not re.fullmatch('[a-z][a-z0-9_]{0,62}', effects['broker_role'])
                        or not re.fullmatch('[0-9a-f]{40}', str(value.get('source_commit','')))
                        or not re.fullmatch('[0-9a-f]{64}', str(getattr(self.fence_adapter,'plan_sha256','')))):
                    _deny('enforced_effects_plan_required')
                self._effects_deployment_binding()
            except TransitionBlocked:
                raise
            except Exception:
                _deny('enforced_effects_plan_required')
            self._namespace_contract(contract['namespace_tables'])
            roles=[]
            for identities in contract['role_sets'].values():
                if not isinstance(identities,dict) or set(identities)!=ENFORCED_SERVICES:
                    _deny('all_service_generation_roles_required')
                for role in identities.values():
                    if not isinstance(role,str) or not re.fullmatch('[a-z][a-z0-9_]{0,62}',role):
                        _deny('all_service_generation_roles_required')
                    roles.append(role)
            if effects['broker_role'] in roles:
                _deny('distinct_effects_broker_role_required')
            if len(set(roles))!=14:
                _deny('distinct_generation_roles_required')
        return value

    @staticmethod
    def _namespace_contract(value):
        if not isinstance(value,dict) or not 7 <= len(value) <= 256:
            _deny('exact_namespace_table_plan_required')
        services=set()
        total=0
        for schema,item in value.items():
            if (not isinstance(schema,str) or not re.fullmatch(r's_[a-z][a-z0-9_]{1,60}',schema)
                    or not isinstance(item,dict) or set(item)!={'service','tables'}
                    or not isinstance(item['service'],str) or item['service'] not in ENFORCED_SERVICES
                    or not schema.startswith('s_'+item['service'].replace('-','_')+'_')
                    or not isinstance(item['tables'],list) or not 1 <= len(item['tables']) <= 512
                    or any(not isinstance(table,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,62}',table) for table in item['tables'])
                    or item['tables']!=sorted(set(item['tables']))):
                _deny('exact_namespace_table_plan_required')
            services.add(item['service'])
            total+=len(item['tables'])
        if services!=ENFORCED_SERVICES or total>4096:
            _deny('exact_namespace_table_plan_required')
        return value

    def bind(self, state):
        if not self.depth or self.plan is None:
            _deny("native_host_guard_required")
        p = self.plan
        if p.get("_approval_action") != "ha-transition":
            _deny("ha_transition_approval_required")
        if (state["cluster"] != p["cluster"] or state["old"] != p["node"]
                or state["target"] not in {"A", "B"} or state["target"] == state["old"]
                or state["generation"] != p["generation"] or state["timeline"] != p["timeline"]):
            _deny("controller_plan_state_mismatch")

    @contextmanager
    def _connection(self, node):
        import psycopg
        connection = None
        try:
            connection = psycopg.connect(**self.plan["pg_nodes"][node], autocommit=True,
                                         connect_timeout=1, application_name="ha_native_controller")
            connection.execute("SET statement_timeout='700ms'")
            connection.execute("SET lock_timeout='500ms'")
            if connection.execute("SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()").fetchone() != (True,):
                _deny("authenticated_pg_observation_required")
            yield connection
        except TransitionBlocked:
            raise
        except Exception:
            _deny("authenticated_pg_unavailable")
        finally:
            if connection is not None:
                connection.close()

    def authenticate_isolated_nodes(self):
        config = self.plan.get("windows_enforced", {}).get("config_profile", {})
        if (self.plan.get("profile") != "enforced-vm-v1"
                or config.get("acceptance_scope") != "isolated-predeployment"
                or config.get("operational_cutover_accepted") is not False
                or any(endpoint["dbname"] != "ha" or endpoint["user"] != "postgres"
                       for endpoint in self.plan["pg_nodes"].values())):
            _deny("isolated_pg_authentication_required")
        for node in ("A", "B"):
            with self._connection(node) as connection, connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                row = connection.execute(
                    "SELECT current_database(), session_user, current_setting('cluster_name'), "
                    "system_identifier::text FROM pg_control_system()"
                ).fetchone()
                if row != ("ha", "postgres", "ha_" + node, str(self.plan["system_id"])):
                    _deny("pg_identity_mismatch")

    def _observe(self, connection, node, state):
        if self.plan.get('profile')=='enforced-vm-v1':
            self._enforced_observation(connection,state)
            self._effects_observation(connection)
        value = connection.execute("""SELECT jsonb_build_object(
            'cluster',c.cluster,'epoch',c.epoch,'revision',c.revision,
            'primary',c.primary_node,'partner',c.partner,'timeline',c.timeline,
            'serving',c.serving,'worker',c.worker,'mode',c.mode,'writes',c.writes_open,
            'history',c.history_safe,'remaining',extract(epoch FROM c.expires_at-clock_timestamp()),
            'system_id',(SELECT system_identifier::text FROM pg_control_system()),
            'node',current_setting('cluster_name'),'recovery',pg_is_in_recovery(),
            'actual_timeline',CASE WHEN pg_is_in_recovery() THEN (SELECT received_tli FROM pg_stat_wal_receiver)
                ELSE ('x'||substring(pg_walfile_name(pg_current_wal_insert_lsn()),1,8))::bit(32)::integer END,
            'flush',CASE WHEN pg_is_in_recovery() THEN pg_last_wal_receive_lsn() ELSE pg_current_wal_flush_lsn() END::text,
            'replay',CASE WHEN pg_is_in_recovery() THEN pg_last_wal_replay_lsn() ELSE pg_current_wal_flush_lsn() END::text,
            'sync_names',regexp_replace(current_setting('synchronous_standby_names'),'[[:space:]\"]','','g'),
            'fsync',current_setting('fsync')='on','full_page_writes',current_setting('full_page_writes')='on',
            'sync_peer',(SELECT application_name FROM pg_stat_replication WHERE state='streaming' AND sync_state='sync' AND application_name='ha_'||lower(c.partner)))
            FROM ha.control c WHERE singleton=1""").fetchone()
        value = value[0] if value else None
        if (not isinstance(value, dict) or value.get("cluster") != state["cluster"]
                or value.get("system_id") != str(self.plan["system_id"]) or value.get("node") != "ha_" + node
                or type(value.get("actual_timeline")) is not int or value["actual_timeline"] < state["timeline"]
                or type(value.get("epoch")) is not int or value["epoch"] > state["generation"]):
            _deny("pg_identity_mismatch")
        return value

    def _enforced_observation(self, connection, state):
        """Live server catalog/permit checks, inside the existing shared budget."""
        contract=self.plan['pg_enforcement']
        expected=self._namespace_contract(contract['namespace_tables'])
        rows=connection.execute('''SELECT n.schema_name::text,n.service,n.definition_sha256,n.system_id,
            (s.oid IS NOT NULL AND owner.rolsuper
             AND EXISTS(SELECT 1 FROM pg_catalog.pg_namespace ns JOIN pg_catalog.pg_roles r ON r.oid=ns.nspowner
                WHERE ns.nspname='ha' AND r.rolsuper)
             AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_class c WHERE c.relnamespace=s.oid
                AND c.relkind IN ('r','p','f','v','m') AND
                    (c.relkind<>'r' OR c.relpersistence<>'p' OR NOT c.relrowsecurity OR NOT c.relforcerowsecurity
                    OR NOT EXISTS(SELECT 1 FROM pg_catalog.pg_roles r WHERE r.oid=c.relowner AND r.rolsuper)
                    OR NOT EXISTS(SELECT 1 FROM pg_catalog.pg_trigger t WHERE t.tgrelid=c.oid AND t.tgname='ha_enforced_write'
                        AND t.tgenabled='A' AND t.tgfoid='ha.enforced_write_guard()'::regprocedure
                AND t.tgtype=62 AND t.tgqual IS NULL AND t.tgattr::text='' AND t.tgnargs=0)
                    OR NOT EXISTS(SELECT 1 FROM pg_catalog.pg_trigger t WHERE t.tgrelid=c.oid AND t.tgname='ha_enforced_commit'
                        AND t.tgenabled='A' AND t.tgfoid='ha.enforced_write_guard()'::regprocedure
                AND t.tgtype=29 AND t.tgdeferrable AND t.tginitdeferred
                AND t.tgqual IS NULL AND t.tgattr::text='' AND t.tgnargs=0)))
             AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace ns ON ns.oid=c.relnamespace
                JOIN pg_catalog.pg_roles r ON r.oid=c.relowner WHERE ns.nspname='ha'
                AND c.relname IN ('enforced_namespaces','enforced_roles') AND (c.relkind<>'r' OR NOT r.rolsuper))
             AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace ns ON ns.oid=p.pronamespace
                JOIN pg_catalog.pg_roles r ON r.oid=p.proowner WHERE ns.nspname='ha'
                AND p.proname IN ('observe','require_enforced_writer','enforced_write_guard','enforced_read_allowed',
                    'enforced_session_ready','enforced_vm_blob_ready','enforced_blob_quota','enforced_blob_complete') AND NOT r.rolsuper)),
            ARRAY(SELECT c.relname::text FROM pg_catalog.pg_class c WHERE c.relnamespace=s.oid
                AND c.relkind IN ('r','p','f','v','m') ORDER BY c.relname)
            FROM ha.enforced_namespaces n
            LEFT JOIN pg_catalog.pg_namespace s ON s.nspname=n.schema_name
            LEFT JOIN pg_catalog.pg_roles owner ON owner.oid=s.nspowner''').fetchall()
        if (len(rows)!=len(expected) or {r[0] for r in rows}!=set(expected)
                or any(r[2]!=contract['definition_sha256'] or r[3]!=str(self.plan['system_id'])
                    or r[4] is not True or not r[5]
                    or expected[r[0]]!={'service':r[1],'tables':r[5]} for r in rows)):
            _deny('server_enforcement_unverified')
        policies=connection.execute('''SELECT e.role_name::text,e.service,e.node,e.generation,
            r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication OR r.rolbypassrls
            OR EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member=r.oid OR m.roleid=r.oid)
            FROM ha.enforced_roles e JOIN pg_catalog.pg_roles r ON r.rolname=e.role_name
            JOIN ha.identities i ON i.role_name=e.role_name AND i.service=e.service AND i.node=e.node
            WHERE e.generation=%s''',(state['generation'],)).fetchall()
        if any(type(generation) is not int or generation!=state['generation'] or type(unsafe) is not bool
               for _,_,_,generation,unsafe in policies):
            _deny('generation_role_enforcement_unverified')
        actual={(service,logical_node):role for role,service,logical_node,generation,unsafe in policies if unsafe is False}
        wanted={(service,logical_node):role for logical_node,services in contract['role_sets'].items() for service,role in services.items()}
        if actual!=wanted or len(policies)!=14:
            _deny('generation_role_enforcement_unverified')
        from .privilege_closure import privilege_closures_unsafe
        if privilege_closures_unsafe(connection,[(role,service) for (service,_),role in wanted.items()]):
            _deny('generation_role_privilege_closure_unverified')

    def _effects_deployment_binding(self):
        """Keep the data-plane binding stable across controller plan rotations."""
        effects = self.plan['pg_enforcement']['effects']
        if 'deployment_binding' not in effects:
            return self.plan['source_commit'], self.fence_adapter.plan_sha256
        binding = effects['deployment_binding']
        if (type(binding) is not dict or set(binding) != {'source_commit', 'plan_sha256'}
                or type(binding['source_commit']) is not str or not re.fullmatch(r'[0-9a-f]{40}', binding['source_commit'])
                or type(binding['plan_sha256']) is not str or not re.fullmatch(r'[0-9a-f]{64}', binding['plan_sha256'])):
            _deny('enforced_effects_plan_required')
        return binding['source_commit'], binding['plan_sha256']

    def _effects_observation(self, connection):
        """Read actual storage, app ACLs and exact approved ledger binding."""
        try:
            from ha_runtime.effects import Binding, validate_catalog
            contract = self.plan['pg_enforcement']['effects']
            source_commit, plan_sha256 = self._effects_deployment_binding()
            expected = (self.plan['cluster'], str(self.plan['system_id']), source_commit,
                        plan_sha256, contract['broker_role'])
            binding = Binding(*expected, broker_instance='0' * 64)
            binding.validate()
            # validate_catalog includes validate_storage_catalog: no table names
            # or a matching binding row alone constitute storage acceptance.
            validate_catalog(connection, binding)
            rows = connection.execute('SELECT cluster,system_id,source_commit,plan_sha256,broker_role FROM ha_effects.binding').fetchall()
            if rows != [expected]:
                _deny('external_effect_controller_binding_mismatch')
        except TransitionBlocked:
            raise
        except Exception:
            _deny('external_effect_controller_catalog_unverified')

    def _effects_open_gate(self, primary, peer, watermark):
        """Only inside the exclusive ROLE_LOCK immediately before lease open.

        The first synchronous nonce commit supplies this attempt's watermark;
        reject a lagging peer and every unresolved record on either node. A
        shared renewal must never call this gate for a normal inflight effect.
        """
        try:
            replay = peer.execute('SELECT pg_last_wal_replay_lsn()::text').fetchone()
            if not replay or self._lsn(replay[0]) < watermark:
                _deny('external_effect_controller_peer_unproven')
            query = "SELECT 1 FROM ha_effects.ledger WHERE state IN ('pending','inflight','uncertain') LIMIT 1"
            unresolved = [connection.execute(query).fetchone() for connection in (primary, peer)]
            if any(row is not None for row in unresolved):
                _deny('external_effect_controller_unresolved')
        except TransitionBlocked:
            raise
        except Exception:
            _deny('external_effect_controller_ledger_unavailable')

    def _read(self, node, state):
        with self._connection(node) as connection:
            return self._observe(connection, node, state)

    def _candidate(self, value, state):
        if (not value["recovery"] or value["primary"] != state["old"]
                or value["timeline"] != state["timeline"] or value["actual_timeline"] != state["timeline"]
                or value["epoch"] >= state["generation"]):
            _deny("promotion_candidate_mismatch")

    @staticmethod
    def _recorded_primary(value, state):
        if (value["recovery"] or value["primary"] != state["target"] or value["partner"] != state["old"]
                or value["epoch"] != state["generation"] or value["timeline"] != value["actual_timeline"]
                or value["actual_timeline"] <= state["timeline"]):
            _deny("recorded_primary_history_required")

    def _promoted_history(self, value, state):
        self._promotion_intent(state)
        if value["actual_timeline"] <= state["timeline"]:
            _deny("promoted_timeline_required")
        if (state["phase"] == "promoting" and value["epoch"] < state["generation"]
                and value["primary"] == state["old"] and value["partner"] == state["target"]
                and value["timeline"] == state["timeline"]):
            return  # The one durable promotion intent may publish new history.
        self._recorded_primary(value, state)

    def _best_effort_revoke(self, state):
        self.validated = None
        try:
            self.bind(state)
            self._local_metadata_write(state["target"], state)
        except Exception:
            # Loss of connectivity/role lock may leave the previously issued
            # lease live until its DB-clock expiry. Never claim instant close.
            pass

    def _local_metadata(self, node, state, *, promoted=False):
        if not promoted:
            # Revocation must remain available when host observation fails.
            return self._local_metadata_write(node, state)
        started = time.monotonic()
        try:
            if not self.fenced(state):
                _deny("positive_fence_required")
            self._local_metadata_write(node, state, promoted=True)
            confirmed = self.fenced(state)
            if not confirmed or time.monotonic() - started > PROOF_SECONDS:
                _deny("fresh_legacy_guard_required")
        except Exception:
            self._best_effort_revoke(state)
            raise

    def _local_metadata_write(self, node, state, *, promoted=False):
        with self._connection(node) as connection:
            connection.execute("BEGIN")
            connection.execute("SET LOCAL synchronous_commit=local")
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (ROLE_LOCK,))
            value = self._observe(connection, node, state)
            if value["recovery"]:
                _deny("primary_metadata_required")
            if promoted:
                self._promoted_history(value, state)
                if state["phase"] == "promoting":
                    connection.execute("UPDATE ha.control SET primary_node=%s,partner=%s,timeline=%s,epoch=%s,revision=GREATEST(revision+1,%s),serving=ARRAY[%s]::text[],worker=%s,writes_open=false,history_safe=false,expires_at=clock_timestamp()-interval '1 second' WHERE singleton=1", (state["target"], state["old"], value["actual_timeline"], state["generation"], state["generation"], state["target"], state["target"]))
                else:
                    # Rejoin/validation/ready may revoke a lease, but must never
                    # adopt a later fork using an older transition approval.
                    connection.execute("UPDATE ha.control SET writes_open=false,history_safe=false,expires_at=clock_timestamp()-interval '1 second',revision=revision+1 WHERE singleton=1")
            else:
                connection.execute("UPDATE ha.control SET writes_open=false,history_safe=false,expires_at=clock_timestamp()-interval '1 second',revision=revision+1 WHERE singleton=1")
            connection.execute("COMMIT")

    def close(self, state):
        self.bind(state)
        self.validated = None
        candidate = self._read(state["target"], state)
        if not candidate["recovery"]:
            # Highest priority after a lost pg_promote response: close the real
            # primary before attempting old-host or replay observations.
            self._local_metadata(state["target"], state)
            self._promoted_history(candidate, state)
            self._local_metadata(state["target"], state, promoted=True)
            return
        self._candidate(candidate, state)
        try:
            self._local_metadata(state["old"], state)
        except TransitionBlocked as error:
            # Old primary may be unreachable/lock-busy. Positive host fencing,
            # not a successful write to a dead primary, is promotion's gate.
            if str(error) != "authenticated_pg_unavailable":
                raise
            self._candidate(self._read(state["target"], state), state)

    def fenced(self, state):
        self.bind(state)
        return self.fence_adapter.fenced(state)

    def fence(self, state):
        self.bind(state)
        self.fence_adapter.fence(state)

    def promoted(self, state):
        self.bind(state)
        self._promotion_intent(state)
        if not self.fenced(state):
            _deny("positive_fence_required")
        value = self._read(state["target"], state)
        if value["recovery"]:
            self._candidate(value, state)
            return False
        self._promoted_history(value, state)
        self._local_metadata(state["target"], state, promoted=True)
        return True

    def promote(self, state):
        self.bind(state)
        self._promotion_intent(state)
        if not self.fenced(state):
            _deny("positive_fence_required")
        with self._connection(state["target"]) as connection:
            value = self._observe(connection, state["target"], state)
            if value["recovery"]:
                self._candidate(value, state)
                started = time.monotonic()
                if not self.fenced(state):
                    _deny("positive_fence_required")
                try:
                    connection.execute("SELECT pg_promote(true,1)")
                except Exception:
                    pass  # Reconcile the actual recovery state, never repeat.
                try:
                    confirmed = self.fenced(state)
                    if not confirmed or time.monotonic() - started > PROOF_SECONDS:
                        _deny("fresh_legacy_guard_required")
                except Exception:
                    self._best_effort_revoke(state)
                    raise
        if not self.promoted(state):
            _deny("promotion_unconfirmed")

    @staticmethod
    def _promotion_intent(state):
        # Only a durably journaled promotion intent can reconcile an already
        # promoted target after response loss. An initial primary is a fork,
        # even when its cluster/system identifiers otherwise look familiar.
        if state["phase"] not in {"promoting", "rejoining", "validating", "ready"}:
            _deny("unexpected_primary_before_promotion_intent")

    def rejoined(self, state):
        self.bind(state)
        if not self.fenced(state):
            _deny("positive_fence_required")
        try:
            primary = self._read(state["target"], state)
            peer = self._read(state["old"], state)
            self._pair(primary, peer, state)
            return True
        except TransitionBlocked:
            return False

    def rejoin(self, state):
        self.bind(state)
        # Fence covers the old WSL/VM resource. Operator must provision a NEW
        # standby with fresh PGDATA and the approved old-node TLS endpoint.
        if not self.rejoined(state):
            _deny("new_standby_required")

    def _pair(self, primary, peer, state):
        expected = (state["cluster"], state["generation"], state["target"], state["old"])
        for value in (primary, peer):
            if ((value["cluster"], value["epoch"], value["primary"], value["partner"]) != expected
                    or value["timeline"] != value["actual_timeline"] or not value["fsync"] or not value["full_page_writes"]):
                _deny("strict_replication_required")
        if any(primary[k] != peer[k] for k in ("revision", "serving", "worker", "mode", "writes", "history")):
            _deny("authority_replica_mismatch")
        if primary["serving"] != [state["target"]] or primary["worker"] != state["target"]:
            _deny("authority_owner_mismatch")
        if (primary["recovery"] or not peer["recovery"] or primary["actual_timeline"] <= state["timeline"]
                or primary["actual_timeline"] != peer["actual_timeline"]
                or primary["sync_names"] != "FIRST1(ha_" + state["old"].lower() + ")"
                or primary["sync_peer"] != "ha_" + state["old"].lower()):
            _deny("strict_replication_required")

    @staticmethod
    def _lsn(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9A-F]+/[0-9A-F]+", value):
            _deny("fresh_replication_proof_required")
        high, low = value.split("/")
        return int(high, 16) * 2**32 + int(low, 16)

    @staticmethod
    def _commit(connection):
        def cancel():
            try:
                connection.cancel_safe(timeout=1)
            except Exception:
                pass
        timer = threading.Timer(0.7, cancel)
        timer.daemon = True
        timer.start()
        try:
            connection.execute("COMMIT")
        finally:
            timer.cancel()
            timer.join()

    @staticmethod
    def _authority_stamp(value):
        return tuple(value[k] for k in ("cluster", "epoch", "revision", "primary", "partner", "timeline",
                                        "serving", "worker", "mode", "writes", "history"))

    @staticmethod
    def _require_open(value):
        if not value["writes"] or not value["history"] or not 0 < value["remaining"] <= LEASE_SECONDS:
            _deny("closed_authority_requires_explicit_open")

    def _fresh(self, state, *, lease=False, shared=False):
        if shared and not lease:
            _deny("shared_lock_only_for_lease_renewal")
        try:
            self._fresh_observation(state, lease=lease, shared=shared)
        except Exception:
            self._best_effort_revoke(state)
            raise

    def _fresh_observation(self, state, *, lease=False, shared=False):
        self.bind(state)
        if shared and not lease:
            _deny("shared_lock_only_for_lease_renewal")
        started = time.monotonic()
        if not self.fenced(state):
            _deny("positive_fence_required")
        nonce = str(uuid.uuid4())
        with self._connection(state["target"]) as primary, self._connection(state["old"]) as peer:
            before, replica = self._observe(primary, state["target"], state), self._observe(peer, state["old"], state)
            self._pair(before, replica, state)
            if shared:
                self._require_open(before)
                self._require_open(replica)
            for suffix in ("", ":flush"):
                primary.execute("BEGIN")
                primary.execute("SET LOCAL synchronous_commit=on")
                primary.execute("SELECT pg_advisory_xact_lock_shared(%s)" if shared else "SELECT pg_advisory_xact_lock(%s)", (ROLE_LOCK,))
                current = self._observe(primary, state["target"], state)
                self._pair(current, self._observe(peer, state["old"], state), state)
                if shared:
                    self._require_open(current)
                    if self._authority_stamp(current) != self._authority_stamp(before):
                        _deny("authority_changed_during_renewal")
                if not suffix:
                    # These are ephemeral controller observations, never app
                    # receipts/barriers. Bound retention inside the same role
                    # lock as the first new nonce, preserving only this attempt.
                    primary.execute("DELETE FROM ha.controller_probes WHERE nonce<>%s AND nonce<>%s", (nonce, nonce + ":flush"))
                primary.execute("INSERT INTO ha.controller_probes(nonce,generation) VALUES(%s,%s)", (nonce + suffix, state["generation"]))
                if lease and suffix == ":flush":
                    if shared:
                        # A shared worker permit freezes role membership but
                        # must coexist with TTL renewal. Match the complete
                        # observed generation and live lease, and change ONLY
                        # its expiry; this can never reopen a closed authority.
                        updated = primary.execute("UPDATE ha.control SET expires_at=clock_timestamp()+interval '5 seconds' WHERE singleton=1 AND cluster=%s AND epoch=%s AND revision=%s AND primary_node=%s AND partner=%s AND timeline=%s AND serving=%s::text[] AND worker=%s AND mode=%s AND writes_open=true AND history_safe=true AND expires_at>clock_timestamp()", (before["cluster"], before["epoch"], before["revision"], before["primary"], before["partner"], before["timeline"], before["serving"], before["worker"], before["mode"]))
                        if updated.rowcount != 1:
                            _deny("authority_changed_during_renewal")
                    else:
                        if self.plan.get('profile') == 'enforced-vm-v1':
                            self._effects_open_gate(primary, peer, watermark)
                        primary.execute("UPDATE ha.control SET serving=ARRAY[%s]::text[],worker=%s,writes_open=true,history_safe=true,expires_at=clock_timestamp()+interval '5 seconds' WHERE singleton=1 AND epoch=%s AND primary_node=%s", (state["target"], state["target"], state["generation"], state["target"]))
                self._commit(primary)
                if not suffix:
                    watermark = self._lsn(primary.execute("SELECT pg_current_wal_insert_lsn()::text").fetchone()[0])
            after, replica = self._observe(primary, state["target"], state), self._observe(peer, state["old"], state)
            self._pair(after, replica, state)
            if shared and self._authority_stamp(after) != self._authority_stamp(before):
                _deny("authority_changed_during_renewal")
            if any(after[k] != before[k] for k in ("revision", "serving", "worker", "mode")) or (not lease and any(after[k] != before[k] for k in ("writes", "history"))):
                _deny("authority_changed_during_proof")
            rows = [c.execute("SELECT generation FROM ha.controller_probes WHERE nonce=%s", (nonce + ":flush",)).fetchone() for c in (primary, peer)]
            fence_confirmed = self.fenced(state)
            if (rows != [(state["generation"],), (state["generation"],)]
                    or min(self._lsn(after["flush"]), self._lsn(replica["flush"]), self._lsn(replica["replay"])) < watermark
                    or time.monotonic() - started > PROOF_SECONDS or not fence_confirmed):
                _deny("fresh_replication_proof_required")
            if lease and (not after["writes"] or not after["history"] or not 0 < after["remaining"] <= LEASE_SECONDS):
                _deny("authority_lease_not_open")
            self.validated = (state["cluster"], state["generation"], state["target"], after["actual_timeline"], time.monotonic())

    def validate(self, state):
        self.validated = None
        self._fresh(state)

    def open(self, state):
        self.bind(state)
        proof = self.validated
        if (not proof or proof[:3] != (state["cluster"], state["generation"], state["target"])
                or time.monotonic() - proof[4] > PROOF_SECONDS):
            _deny("fresh_replication_proof_required")
        try:
            self._fresh(state, lease=True)
        except Exception:
            # A replication response may be lost after local commit. Revoke the
            # lease locally rather than trusting a failed proof or response.
            try:
                self._local_metadata(state["target"], state)
            except Exception:
                pass
            raise

    def assert_ready(self, state):
        self.renew(state)

    def arm(self, state):
        """Explicitly start serving a recorded ready generation after expiry.

        The caller keeps the same approved host guard/controller lock through
        this exclusive rearming and the subsequent shared-renewal serve loop.
        """
        self.bind(state)
        self.validated = None
        if state["phase"] != "ready":
            _deny("ready_transition_required")
        if not self.fenced(state):
            _deny("positive_fence_required")
        current = self._read(state["target"], state)
        try:
            self._recorded_primary(current, state)
        except TransitionBlocked:
            # Revoke an unexpected writer without rewriting its epoch/timeline.
            self._local_metadata(state["target"], state)
            raise
        if current["writes"] and current["history"] and 0 < current["remaining"] <= LEASE_SECONDS:
            self.renew(state)
            return
        self.close(state)
        self.validate(state)
        self.open(state)

    def renew(self, state):
        """Extend an already open lease without revoking long worker permits.

        Closed/expired authority requires explicit validate/open under the
        exclusive role lock; lease serving never performs that transition.
        """
        self.validated = None
        self._fresh(state, lease=True, shared=True)
