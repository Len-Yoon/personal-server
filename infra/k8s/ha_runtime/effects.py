"""TCB-only PostgreSQL external-effect ledger, not a network/credential boundary.

The caller must authenticate transport identities, pin adapters and the database
factory, and enforce sole egress/credential ownership outside this module.
Unresolved records never cause an automatic provider retry, including after a
broker restart. Request payloads are never persisted in the ledger. The private credential
state table is accessible only inside an authenticated broker adapter call.
"""
from dataclasses import dataclass
from contextlib import contextmanager
import copy
import hashlib
import json
import math
import re
import threading
import time
import uuid

from .config import SERVICES, DatabaseUnavailable
from .proof import bounded_commit

ROLE_LOCK = 7142026110601
HEX = re.compile(r'[0-9a-f]{64}\Z')
NAME = re.compile(r'[a-z][a-z0-9_]{0,62}\Z')
KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
SECRET_KEYS = frozenset({'token', 'accesstoken', 'refreshtoken', 'secret', 'clientsecret',
                         'password', 'credential', 'credentials', 'authorization', 'privatekey', 'passfile'})


class EffectsBlocked(DatabaseUnavailable):
    pass


def deny(reason='external_effect_blocked'):
    raise EffectsBlocked(reason)


READ_RESULT_BYTES = 3 * 1024 * 1024
PUBLIC_READ_SERVICES = {'book.http.get':'book-memo','youtube.http.get':'youtube-memo','crawler.http.get':'crawler-worker'}


def _result_limit(operation_name, operation):
    """TCB-only named public GET policy; never a request/config option."""
    return READ_RESULT_BYTES if operation.read_only is True and PUBLIC_READ_SERVICES.get(operation_name)==operation.service else 65536


def canonical(value, *, limit=65536):
    if type(limit) is not int or limit not in {65536,READ_RESULT_BYTES}:
        deny('invalid_external_effect_request')
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                         allow_nan=False).encode()
    except (TypeError, ValueError, OverflowError, RecursionError):
        deny('invalid_external_effect_request')
    if len(raw) > limit:
        deny('invalid_external_effect_request')
    return raw


def no_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if (type(key) is not str or any(part in re.sub('[^a-z]', '', key.lower()) for part in SECRET_KEYS)):
                deny('external_effect_secret_result_forbidden')
            no_secrets(item)
    elif isinstance(value, list):
        for item in value:
            no_secrets(item)


@dataclass(frozen=True)
class Binding:
    cluster: str
    system_id: str
    source_commit: str
    plan_sha256: str
    broker_role: str
    broker_instance: str

    def validate(self):
        if (not KEY.fullmatch(str(self.cluster)) or not re.fullmatch(r'[1-9][0-9]{0,19}', str(self.system_id))
                or not re.fullmatch(r'[0-9a-f]{40}', str(self.source_commit))
                or not HEX.fullmatch(str(self.plan_sha256)) or not NAME.fullmatch(str(self.broker_role))
                or not HEX.fullmatch(str(self.broker_instance))):
            deny('invalid_external_effect_binding')


@dataclass(frozen=True)
class TrustedIdentity:
    """Transport-authenticated input; constructing this type authenticates nothing."""
    service: str
    node: str
    generation: int

    def validate(self):
        if (self.service not in SERVICES or self.node not in {'A', 'B'}
                or type(self.generation) is not int or not 1 <= self.generation <= 9223372036854775807):
            deny('invalid_external_effect_identity')


@dataclass(frozen=True)
class ProviderAck:
    """Public acknowledgement/reference only; never credential material."""
    reference: str
    result: dict


@dataclass(frozen=True)
class Operation:
    """Trusted static policy; the request cannot choose credential/resource.

    The adapter must enforce the supplied monotonic deadline through its first
    network send and response handling. A nonconforming adapter is outside the
    guarantee; a Python timeout cannot recall an already issued remote effect.
    """
    service: str
    resource: str
    payload_keys: frozenset
    result_keys: frozenset
    adapter: object
    timeout: float = 2.0
    # Trusted policy only. The adapter must perform a bounded read with no
    # externally observable mutation; using a side-effecting adapter here is
    # a TCB contract violation. Apps cannot select this flag in a request.
    read_only: bool = False

    def validate(self):
        if (self.service not in SERVICES or not KEY.fullmatch(str(self.resource))
                or type(self.payload_keys) is not frozenset or type(self.result_keys) is not frozenset
                or any(type(key) is not str or not KEY.fullmatch(key) for key in self.payload_keys | self.result_keys)
                or not callable(self.adapter) or type(self.timeout) not in {int, float}
                or not math.isfinite(self.timeout) or not 0 < self.timeout <= 10 or type(self.read_only) is not bool):
            deny('invalid_external_effect_policy')
        no_secrets({key: None for key in self.result_keys})


# Both binding and source of authority are read on the actual dispatch session.
AUTHORITY_SQL = """SELECT jsonb_build_object(
 'role',session_user,'cluster',c.cluster,'system_id',(SELECT system_identifier::text FROM pg_control_system()),
 'primary',c.primary_node,'partner',c.partner,'node',CASE current_setting('cluster_name') WHEN 'ha_A' THEN 'A' WHEN 'ha_B' THEN 'B' END,
 'generation',c.epoch,'worker',c.worker,'timeline',c.timeline,
 'actual_timeline',CASE WHEN NOT pg_is_in_recovery() THEN ('x'||substring(pg_walfile_name(pg_current_wal_insert_lsn()),1,8))::bit(32)::integer END,
 'recovery',pg_is_in_recovery(),'writes_open',c.writes_open,'history_safe',c.history_safe,
 'remaining',extract(epoch FROM c.expires_at-clock_timestamp()),
 'isolation',current_setting('transaction_isolation'),'commit',current_setting('synchronous_commit'),
 'fsync',current_setting('fsync'),'full_page_writes',current_setting('full_page_writes'),
 'prepared',current_setting('max_prepared_transactions'),
 'peer_config',regexp_replace(current_setting('synchronous_standby_names'),'[[:space:]\"]','','g'),
 'peer_expected','FIRST1(ha_'||lower(c.partner)||')',
 'peer_sync',EXISTS(SELECT 1 FROM pg_stat_replication WHERE application_name='ha_'||lower(c.partner) AND state='streaming' AND sync_state='sync'),
 'identity',EXISTS(SELECT 1 FROM ha.enforced_roles r JOIN ha.identities i USING(role_name)
     WHERE r.service=%s AND r.node=%s AND r.generation=%s AND i.service=r.service AND i.node=r.node),
 'binding',jsonb_build_object('cluster',b.cluster,'system_id',b.system_id,'source_commit',b.source_commit,
     'plan_sha256',b.plan_sha256,'broker_role',b.broker_role))
 FROM ha.control c CROSS JOIN ha_effects.binding b WHERE c.singleton=1 AND b.singleton=1"""

BINDING_SQL = 'SELECT cluster,system_id,source_commit,plan_sha256,broker_role FROM ha_effects.binding WHERE singleton=1'


def _expected(binding):
    return {key: getattr(binding, key) for key in ('cluster', 'system_id', 'source_commit', 'plan_sha256', 'broker_role')}


def _binding(connection, binding):
    row = connection.execute(BINDING_SQL).fetchone()
    if row is None or tuple(row) != tuple(_expected(binding).values()):
        deny('external_effect_binding_changed')


def _authority(connection, binding, identity):
    started = time.monotonic()
    value = connection.execute('SELECT ha_effects.observe(%s,%s,%s)', (identity.service, identity.node, identity.generation)).fetchone()
    if value is None or type(value[0]) is not dict:
        deny('external_effect_authority_unavailable')
    value = value[0]
    remaining = value.get('remaining')
    if (value.get('role') != binding.broker_role or value.get('binding') != _expected(binding)
            or value.get('cluster') != binding.cluster or value.get('system_id') != binding.system_id
            or value.get('recovery') is not False or value.get('primary') not in {'A', 'B'}
            or value.get('node') != value.get('primary') or value.get('worker') != identity.node
            or type(value.get('generation')) is not int or value['generation'] != identity.generation
            or type(value.get('timeline')) is not int or value['timeline'] < 1
            or value.get('timeline') != value.get('actual_timeline')
            or value.get('writes_open') is not True or value.get('history_safe') is not True
            or type(remaining) not in {int, float} or not math.isfinite(remaining) or not 0 < remaining <= 5
            or value.get('identity') is not True or value.get('isolation') != 'read committed'
            or value.get('commit') != 'remote_apply' or value.get('fsync') != 'on'
            or value.get('full_page_writes') != 'on' or value.get('prepared') != '0'
            or value.get('peer_config') != value.get('peer_expected') or value.get('peer_sync') is not True):
        deny('external_effect_authority_unavailable')
    value['_lease_deadline'] = started + remaining
    if time.monotonic() >= value['_lease_deadline']:
        deny('external_effect_authority_unavailable')
    return value


PEER_SQL = """SELECT jsonb_build_object('role',session_user,
 'node',CASE current_setting('cluster_name') WHEN 'ha_A' THEN 'A' WHEN 'ha_B' THEN 'B' END,
 'system_id',(SELECT system_identifier::text FROM pg_control_system()),'recovery',pg_is_in_recovery(),
 'cluster',c.cluster,'generation',c.epoch,'primary',c.primary_node,'worker',c.worker,
 'timeline',c.timeline,'history_safe',c.history_safe,
 'actual_timeline',CASE WHEN pg_is_in_recovery() THEN (SELECT received_tli FROM pg_stat_wal_receiver) ELSE ('x'||substring(pg_walfile_name(pg_current_wal_insert_lsn()),1,8))::bit(32)::integer END,
 'replay',pg_last_wal_replay_lsn()::text,
 'binding',jsonb_build_object('cluster',b.cluster,'system_id',b.system_id,'source_commit',b.source_commit,
 'plan_sha256',b.plan_sha256,'broker_role',b.broker_role))
 FROM ha.control c CROSS JOIN ha_effects.binding b WHERE c.singleton=1 AND b.singleton=1"""


def _lsn(value):
    if type(value) is not str or not re.fullmatch(r'[0-9A-F]{1,8}/[0-9A-F]{1,8}', value):
        deny('external_effect_peer_receipt_unavailable')
    high, low = value.split('/')
    return (int(high, 16) << 32) | int(low, 16)


@contextmanager
def _tx(connection):
    notices = []
    handler = lambda notice: notices.append(notice.severity_nonlocalized)
    connection.add_notice_handler(handler)
    try:
        connection.execute('BEGIN')
        connection.execute("SET LOCAL synchronous_commit='remote_apply'")
        yield
        bounded_commit(connection)
        if any(level in {'WARNING','ERROR','FATAL','PANIC'} for level in notices):
            deny('external_effect_commit_uncertain')
    except Exception:
        try:
            connection.execute('ROLLBACK')
        except Exception:
            pass
        raise
    finally:
        connection.remove_notice_handler(handler)


class Broker:
    def __init__(self, factory, binding, operations, *, peer_factory):
        binding.validate()
        if not callable(factory) or not callable(peer_factory) or type(operations) is not dict or not 1 <= len(operations) <= 64:
            deny('invalid_external_effect_policy')
        for name, operation in operations.items():
            if type(name) is not str or not KEY.fullmatch(name) or type(operation) is not Operation:
                deny('invalid_external_effect_policy')
            operation.validate()
        self.factory, self.peer_factory, self.binding, self.operations = factory, peer_factory, binding, dict(operations)
        self._dispatch_local = threading.local()
        self._read_slots = threading.BoundedSemaphore(4)
        self._internal_read_slots = threading.BoundedSemaphore(2)

    def credential_backend(self):
        """Trusted adapter dependency; grants no authority outside dispatch."""
        return CredentialStateBackend(self)

    def _session(self, peer=False):
        connection = (self.peer_factory if peer else self.factory)()
        try:
            if connection.autocommit is not True:
                deny('external_effect_autocommit_required')
            connection.execute("SET statement_timeout='2000ms'")
            connection.execute("SET lock_timeout='1000ms'")
            if connection.execute('SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()').fetchone() != (True,):
                deny('external_effect_tls_required')
            return connection
        except Exception:
            connection.close()
            raise

    def _begin(self, connection, identity):
        connection.execute("SET LOCAL synchronous_commit='remote_apply'")
        value = _authority(connection, self.binding, identity)
        validate_catalog(connection, self.binding)
        if time.monotonic() >= value['_lease_deadline']:
            deny('external_effect_authority_unavailable')
        return value

    def dispatch(self, identity, request):
        """At most one adapter invocation; no connection/provider retry path."""
        if type(identity) is not TrustedIdentity:
            deny('invalid_external_effect_identity')
        identity.validate()
        if (type(request) is not dict or set(request) != {'operation', 'request_id', 'payload'}
                or type(request['operation']) is not str or request['operation'] not in self.operations
                or type(request['request_id']) is not str or not KEY.fullmatch(request['request_id'])):
            deny('invalid_external_effect_request')
        operation = self.operations[request['operation']]
        if (operation.service != identity.service or type(request['payload']) is not dict
                or set(request['payload']) != operation.payload_keys):
            deny('invalid_external_effect_request')
        payload = copy.deepcopy(request['payload'])
        digest = hashlib.sha256(canonical({'operation': request['operation'], 'payload': payload})).hexdigest()
        if operation.read_only:
            return self._dispatch_read_only(identity,request['operation'],operation,payload)
        connection = None
        peer = None
        reserved = False
        key = (identity.service, request['request_id'])
        try:
            connection = self._session()
            if connection.execute('SELECT pg_try_advisory_lock_shared(%s)', (ROLE_LOCK,)).fetchone() != (True,):
                deny('external_effect_role_guard_unavailable')
            if connection.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0))', ('effect:' + operation.resource,)).fetchone() != (True,):
                deny('external_effect_resource_busy')
            peer = self._session(peer=True)
            with _tx(connection):
                self._begin(connection, identity)
                existing = connection.execute('SELECT payload_sha256,resource,operation,state,provider_ack,result FROM ha_effects.ledger WHERE service=%s AND request_id=%s FOR UPDATE', key).fetchone()
                if existing is not None:
                    if tuple(existing[:3]) != (digest, operation.resource, request['operation']):
                        deny('external_effect_request_conflict')
                    if existing[3] != 'completed':
                        deny('external_effect_manual_reconciliation_required')
                else:
                    if connection.execute("SELECT 1 FROM ha_effects.ledger WHERE resource=%s AND state IN ('pending','inflight','uncertain') LIMIT 1", (operation.resource,)).fetchone():
                        deny('external_effect_manual_reconciliation_required')
                    connection.execute("INSERT INTO ha_effects.ledger(service,request_id,operation,resource,payload_sha256,node,generation,broker_instance,state) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'pending')", (*key, request['operation'], operation.resource, digest, identity.node, identity.generation, self.binding.broker_instance))
            if existing is not None:
                # Replay also proves the completed record on the future peer;
                # a local-only completed row is never replicated acceptance.
                self._durable(connection, peer, identity, key, 'completed')
                no_secrets(existing[5])
                return {'status': 'completed', 'provider_ack': existing[4], 'result': existing[5], 'replayed': True}
            reserved = True
            self._durable(connection, peer, identity, key, 'pending')
            with _tx(connection):
                self._begin(connection, identity)
                row = connection.execute("UPDATE ha_effects.ledger SET state='inflight',updated_at=clock_timestamp() WHERE service=%s AND request_id=%s AND state='pending' RETURNING state", key).fetchone()
                if row != ('inflight',):
                    deny('external_effect_manual_reconciliation_required')
            self._durable(connection, peer, identity, key, 'inflight')
            # This is a fresh authority read after durable inflight acknowledgement.
            with _tx(connection):
                authority = self._begin(connection, identity)
            deadline = min(time.monotonic() + operation.timeout, authority['_lease_deadline'])
            budget = deadline - time.monotonic()
            if budget <= 0:
                deny('external_effect_authority_unavailable')
            done = threading.Event()
            answer = []
            def invoke():
                try:
                    if time.monotonic() >= deadline:
                        return
                    self._dispatch_local.context = (identity, operation.resource, deadline)
                    try:
                        answer.append(operation.adapter(payload, timeout=budget, deadline=deadline))
                    finally:
                        del self._dispatch_local.context
                except Exception:
                    pass
                finally:
                    done.set()
            # A nonconforming adapter may outlive its timeout. Its resource stays
            # uncertain and blocked, even while that owned thread is still alive.
            threading.Thread(target=invoke, daemon=True).start()
            if not done.wait(budget) or time.monotonic() >= deadline or len(answer) != 1:
                deny('external_effect_manual_reconciliation_required')
            ack = answer[0]
            if (type(ack) is not ProviderAck or type(ack.reference) is not str or not 1 <= len(ack.reference) <= 256
                    or any(ord(char) < 32 for char in ack.reference) or type(ack.result) is not dict
                    or set(ack.result) != operation.result_keys):
                deny('external_effect_manual_reconciliation_required')
            no_secrets(ack.result)
            result_raw = canonical(ack.result).decode()
            with _tx(connection):
                self._begin(connection, identity)
                row = connection.execute("UPDATE ha_effects.ledger SET state='completed',provider_ack=%s,result=%s::jsonb,updated_at=clock_timestamp() WHERE service=%s AND request_id=%s AND state='inflight' RETURNING state", (ack.reference, result_raw, *key)).fetchone()
                if row != ('completed',):
                    deny('external_effect_manual_reconciliation_required')
            self._durable(connection, peer, identity, key, 'completed')
            return {'status': 'completed', 'provider_ack': ack.reference, 'result': copy.deepcopy(ack.result), 'replayed': False}
        except Exception:
            if connection is not None and reserved:
                try:
                    # Safe downgrade only. This never starts another remote call
                    # or marks an unknown commit/provider outcome completed.
                    with _tx(connection):
                        _binding(connection, self.binding)
                        connection.execute("UPDATE ha_effects.ledger SET state='uncertain',updated_at=clock_timestamp() WHERE service=%s AND request_id=%s AND state IN ('pending','inflight')", key)
                except Exception:
                    pass  # Existing pending/inflight remains a durable blocker.
            deny('external_effect_manual_reconciliation_required')
        finally:
            if peer is not None:
                peer.close()
            if connection is not None:
                connection.close()  # Own session locks, including ROLE_LOCK.

    def _dispatch_read_only(self, identity, operation_name, operation, payload):
        """One bounded TCB diagnostic read; no durable-effect or private state.

        Unknown responses are harmless to repeat as a new explicit read. This
        function never retries, starts credential context or clears an existing
        uncertain effect. A timed-out adapter keeps its worker slot until exit.
        """
        connection=None
        acquired=False
        started=False
        # Authenticated internal Executor inspect calls need independent slots:
        # Portal -> Executor -> broker must not wait behind its outer reads.
        slots = (self._internal_read_slots if identity.service=='homeops-executor'
                 and operation_name.startswith('executor.docker.inspect.') else self._read_slots)
        try:
            if not slots.acquire(blocking=False):
                deny('external_effect_read_only_unavailable')
            acquired=True
            connection=self._session()
            if connection.execute('SELECT pg_try_advisory_lock_shared(%s)',(ROLE_LOCK,)).fetchone()!=(True,):
                deny('external_effect_read_only_unavailable')
            with _tx(connection):
                before=self._begin(connection,identity)
            deadline=min(time.monotonic()+operation.timeout,before['_lease_deadline'])
            budget=deadline-time.monotonic()
            if budget<=0:deny('external_effect_read_only_unavailable')
            done=threading.Event();answer=[]
            def invoke_read():
                try:
                    if time.monotonic()<deadline:
                        # Deliberately no authenticated credential context here.
                        answer.append(operation.adapter(payload,timeout=budget,deadline=deadline))
                except Exception:
                    pass
                finally:
                    slots.release()
                    done.set()
            thread=threading.Thread(target=invoke_read,daemon=True)
            thread.start();started=True
            if not done.wait(budget) or time.monotonic()>=deadline or len(answer)!=1:
                deny('external_effect_read_only_unavailable')
            ack=answer[0]
            if (type(ack) is not ProviderAck or type(ack.reference) is not str or not 1<=len(ack.reference)<=256
                    or any(ord(char)<32 for char in ack.reference) or type(ack.result) is not dict
                    or set(ack.result)!=operation.result_keys):
                deny('external_effect_read_only_unavailable')
            no_secrets(ack.result)
            canonical(ack.result,limit=_result_limit(operation_name,operation))
            with _tx(connection):
                after=self._begin(connection,identity)
                if (time.monotonic()>=deadline
                        or any(after[field]!=before[field] for field in ('generation','worker','primary','partner','timeline'))):
                    deny('external_effect_read_only_unavailable')
            return {'status':'completed','provider_ack':ack.reference,'result':copy.deepcopy(ack.result),'replayed':False}
        except Exception:
            raise EffectsBlocked('external_effect_read_only_unavailable') from None
        finally:
            if connection is not None:connection.close()
            if acquired and not started:slots.release()

    def _durable(self, connection, peer, identity, key, phase):
        # Capture AFTER the acknowledged record commit, then cover that exact
        # watermark with another synchronous barrier. Never chase a moving LSN.
        watermark = connection.execute('SELECT pg_current_wal_insert_lsn()::text').fetchone()[0]
        attempt = uuid.uuid4().hex
        with _tx(connection):
            before = self._begin(connection, identity)
            connection.execute('INSERT INTO ha_effects.barriers(proof_id,service,request_id,phase,generation) VALUES(%s,%s,%s,%s,%s)', (attempt, *key, phase, identity.generation))
        observed = peer.execute('SELECT ha_effects.peer_observe()').fetchone()
        value = observed[0] if observed else None
        if (type(value) is not dict or value.get('role') != self.binding.broker_role
                or value.get('binding') != _expected(self.binding) or value.get('recovery') is not True
                or value.get('system_id') != self.binding.system_id or value.get('cluster') != self.binding.cluster
                or value.get('node') != before['partner'] or value.get('primary') != before['primary']
                or type(value.get('generation')) is not int or value['generation'] != identity.generation
                or value.get('worker') != identity.node or value.get('timeline') != before['timeline']
                or value.get('actual_timeline') != before['timeline']
                or _lsn(value.get('replay')) < _lsn(watermark)):
            deny('external_effect_peer_receipt_unavailable')
        query = 'SELECT operation,resource,payload_sha256,node,generation,broker_instance,state,provider_ack,result FROM ha_effects.ledger WHERE service=%s AND request_id=%s'
        own = connection.execute(query, key).fetchone()
        replica = peer.execute(query, key).fetchone()
        barrier = peer.execute('SELECT service,request_id,phase,generation FROM ha_effects.barriers WHERE proof_id=%s', (attempt,)).fetchone()
        if (own is None or tuple(own) != tuple(replica or ()) or own[6] != phase
                or barrier != (*key, phase, identity.generation)):
            deny('external_effect_peer_receipt_unavailable')
        # The peer receipt is not reused for the next phase or request.
        with _tx(connection):
            after = self._begin(connection, identity)
            if any(after[field] != before[field] for field in ('generation','worker','primary','partner','timeline')):
                deny('external_effect_authority_unavailable')

    def has_unresolved(self, *, generation, primary_node, watermark):
        """Fresh two-node read inside the caller's exclusive coordinator guard.

        The caller must hold ROLE_LOCK exclusively and supply its freshly proven
        WAL watermark. False is not a guard token or permission to transition.
        No lock is acquired here: another shared-lock session would deadlock the
        coordinator. No lease is opened or unresolved record cleared here.
        """
        if type(generation) is not int or generation < 1 or primary_node not in {'A','B'}:
            deny('external_effect_gate_identity_required')
        _lsn(watermark)
        primary = peer = None
        try:
            primary = self._session()
            peer = self._session(peer=True)
            values = []
            for connection, recovery in ((primary, False), (peer, True)):
                _binding(connection, self.binding)
                row = connection.execute('SELECT ha_effects.peer_observe()').fetchone()
                value = row[0] if row else None
                if (type(value) is not dict or value.get('role') != self.binding.broker_role
                        or value.get('binding') != _expected(self.binding)
                        or value.get('system_id') != self.binding.system_id or value.get('cluster') != self.binding.cluster
                        or value.get('recovery') is not recovery or value.get('generation') != generation
                        or type(value.get('generation')) is not int or value.get('primary') != primary_node
                        or value.get('node') != (('B' if primary_node == 'A' else 'A') if recovery else primary_node)
                        or type(value.get('timeline')) is not int or value['timeline'] < 1
                        or value.get('actual_timeline') != value['timeline'] or value.get('history_safe') is not True):
                    deny('external_effect_gate_authority_unavailable')
                values.append(value)
                validate_catalog(connection, self.binding)
            if (values[0]['timeline'] != values[1]['timeline'] or values[0].get('worker') != values[1].get('worker')
                    or _lsn(values[1].get('replay')) < _lsn(watermark)):
                deny('external_effect_peer_receipt_unavailable')
            query = "SELECT 1 FROM ha_effects.ledger WHERE state IN ('pending','inflight','uncertain') LIMIT 1"
            return any(connection.execute(query).fetchone() is not None for connection in (primary, peer))
        except Exception:
            deny('external_effect_ledger_unavailable')
        finally:
            if peer is not None:
                peer.close()
            if primary is not None:
                primary.close()


def install(connection, binding):
    """Explicit administrator-only, closed-authority first installation.

    Creates no role, network rule or credential. Existing schema is rejected;
    installer/config/catalog acceptance must precede a real deployment.
    """
    binding.validate()
    role = '"' + binding.broker_role + '"'
    try:
        with connection.transaction():
            connection.execute('SELECT pg_advisory_xact_lock(%s)', (ROLE_LOCK,))
            if connection.execute('SELECT rolsuper FROM pg_roles WHERE rolname=session_user').fetchone() != (True,):
                deny('external_effect_admin_required')
            if connection.execute('SELECT writes_open FROM ha.control WHERE singleton=1').fetchone() != (False,):
                deny('external_effect_closed_authority_required')
            # A broker must not inherit or SET ROLE to any other database role.
            if connection.execute("SELECT NOT (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls OR rolinherit) AND rolcanlogin AND NOT EXISTS(SELECT 1 FROM pg_roles p WHERE p.rolname<>r.rolname AND pg_has_role(r.oid,p.oid,'MEMBER')) FROM pg_roles r WHERE rolname=%s", (binding.broker_role,)).fetchone() != (True,):
                deny('external_effect_private_broker_role_required')
            connection.execute('CREATE SCHEMA ha_effects')
            connection.execute('REVOKE ALL ON SCHEMA ha_effects FROM PUBLIC')
            connection.execute('CREATE TABLE ha_effects.binding(singleton integer PRIMARY KEY CHECK(singleton=1),cluster text NOT NULL,system_id text NOT NULL,source_commit text NOT NULL,plan_sha256 text NOT NULL,broker_role name NOT NULL)')
            connection.execute('INSERT INTO ha_effects.binding VALUES(1,%s,%s,%s,%s,%s)', tuple(_expected(binding).values()))
            connection.execute("""CREATE TABLE ha_effects.ledger(
 service text NOT NULL,request_id text NOT NULL,operation text NOT NULL,resource text NOT NULL,
 payload_sha256 text NOT NULL CHECK(payload_sha256 ~ '^[0-9a-f]{64}$'),node text NOT NULL CHECK(node IN ('A','B')),
 generation bigint NOT NULL CHECK(generation>0),broker_instance text NOT NULL,
 state text NOT NULL CHECK(state IN ('pending','inflight','uncertain','completed')),
 provider_ack text,result jsonb,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(service,request_id),
 CHECK((state='completed')=(provider_ack IS NOT NULL AND result IS NOT NULL)))""")
            connection.execute("CREATE UNIQUE INDEX effect_unresolved_resource ON ha_effects.ledger(resource) WHERE state IN ('pending','inflight','uncertain')")
            connection.execute('CREATE TABLE ha_effects.barriers(proof_id text PRIMARY KEY,service text NOT NULL,request_id text NOT NULL,phase text NOT NULL,generation bigint NOT NULL)')
            connection.execute("CREATE TABLE ha_effects.private_state(service text NOT NULL,key text NOT NULL,payload jsonb NOT NULL,revision bigint NOT NULL CHECK(revision>0),payload_sha256 text NOT NULL CHECK(payload_sha256 ~ '^[0-9a-f]{64}$'),PRIMARY KEY(service,key))")
            connection.execute('REVOKE ALL ON ALL TABLES IN SCHEMA ha_effects FROM PUBLIC')
            connection.execute('GRANT USAGE ON SCHEMA ha_effects,ha TO ' + role)
            connection.execute('GRANT SELECT ON ha_effects.binding,ha.control,ha.enforced_roles,ha.identities TO ' + role)
            connection.execute('GRANT SELECT,INSERT,UPDATE ON ha_effects.ledger TO ' + role)
            connection.execute('GRANT SELECT,INSERT ON ha_effects.barriers TO ' + role)
            connection.execute('GRANT SELECT,INSERT,UPDATE ON ha_effects.private_state TO ' + role)
            # Restricted statistics/control functions remain in this fixed,
            # administrator-owned observer; do not grant broad monitor roles.
            observe_sql = observer_body()
            connection.execute('CREATE FUNCTION ha_effects.observe(text,text,bigint) RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,ha,ha_effects,pg_temp AS $effect$' + observe_sql + '$effect$')
            connection.execute('CREATE FUNCTION ha_effects.peer_observe() RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,ha,ha_effects,pg_temp AS $effect$' + PEER_SQL + '$effect$')
            connection.execute('REVOKE ALL ON FUNCTION ha_effects.peer_observe() FROM PUBLIC')
            connection.execute('GRANT EXECUTE ON FUNCTION ha_effects.peer_observe() TO ' + role)
            connection.execute('REVOKE ALL ON FUNCTION ha_effects.observe(text,text,bigint) FROM PUBLIC')
            connection.execute('GRANT EXECUTE ON FUNCTION ha_effects.observe(text,text,bigint) TO ' + role)
            validate_catalog(connection, binding)
    except EffectsBlocked:
        raise
    except Exception:
        deny('external_effect_installation_failed')
    return {'installed': True, 'provider_called': False, 'network_enforced': False, 'credentials_migrated': False}


def validate_catalog(connection, binding):
    """Effective app privileges, including inherited grants, must all be absent."""
    validate_storage_catalog(connection)
    identities = connection.execute('SELECT i.role_name,i.service,i.node,r.generation FROM ha.identities i LEFT JOIN ha.enforced_roles r USING(role_name) ORDER BY i.role_name').fetchall()
    generation = connection.execute('SELECT epoch FROM ha.control WHERE singleton=1').fetchone()
    live = [row for row in identities if generation and row[3] == generation[0]]
    if (not 14 <= len(identities) <= 4096 or len({row[0] for row in identities}) != len(identities)
            or len(live) != 14 or {(row[1],row[2]) for row in live} != {(service,node) for service in SERVICES for node in ('A','B')}
            or any(row[1] not in SERVICES or row[2] not in {'A','B'} or type(row[3]) is not int or row[3] < 1 for row in identities)
            or binding.broker_role in {row[0] for row in identities}):
        deny('external_effect_app_inventory_required')
    rows = connection.execute("""SELECT r.rolname,
 r.rolsuper OR pg_has_role(r.oid,%s,'MEMBER') OR has_schema_privilege(r.oid,'ha_effects','USAGE,CREATE')
 OR has_table_privilege(r.oid,'ha_effects.ledger','SELECT,INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES')
 OR has_any_column_privilege(r.oid,'ha_effects.ledger','SELECT,INSERT,UPDATE,REFERENCES')
 OR has_table_privilege(r.oid,'ha_effects.binding','SELECT,INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES')
 OR has_any_column_privilege(r.oid,'ha_effects.binding','SELECT,INSERT,UPDATE,REFERENCES')
 OR has_function_privilege(r.oid,'ha_effects.observe(text,text,bigint)','EXECUTE')
 OR has_function_privilege(r.oid,'ha_effects.peer_observe()','EXECUTE')
 OR has_table_privilege(r.oid,'ha_effects.barriers','SELECT,INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES')
 OR has_any_column_privilege(r.oid,'ha_effects.barriers','SELECT,INSERT,UPDATE,REFERENCES')
 FROM pg_roles r WHERE r.rolname=ANY(%s) ORDER BY r.rolname""", (binding.broker_role, [row[0] for row in identities])).fetchall()
    if ([row[0] for row in rows] != [row[0] for row in identities] or any(row[1] is not False for row in rows)):
        deny('external_effect_app_privilege_escape')


STORAGE_COLUMNS = {
    'binding': [('singleton','integer',True),('cluster','text',True),('system_id','text',True),
                ('source_commit','text',True),('plan_sha256','text',True),('broker_role','name',True)],
    'ledger': [('service','text',True),('request_id','text',True),('operation','text',True),
               ('resource','text',True),('payload_sha256','text',True),('node','text',True),
               ('generation','bigint',True),('broker_instance','text',True),('state','text',True),
               ('provider_ack','text',False),('result','jsonb',False),
               ('created_at','timestamp with time zone',True),('updated_at','timestamp with time zone',True)],
    'private_state': [('service','text',True),('key','text',True),('payload','jsonb',True),('revision','bigint',True),('payload_sha256','text',True)],
    'barriers': [('proof_id','text',True),('service','text',True),('request_id','text',True),
                 ('phase','text',True),('generation','bigint',True)]}


def observer_body():
    return AUTHORITY_SQL.replace('%s','$1',1).replace('%s','$2',1).replace('%s','$3',1)


STORAGE_RELATIONS_SQL = """/* effects_storage_relations */
 SELECT c.relname,c.relkind,c.relpersistence,r.rolsuper,c.relrowsecurity,c.relforcerowsecurity,c.relispartition,
 EXISTS(SELECT 1 FROM pg_inherits h WHERE h.inhrelid=c.oid OR h.inhparent=c.oid),
 EXISTS(SELECT 1 FROM pg_trigger t WHERE t.tgrelid=c.oid),
 EXISTS(SELECT 1 FROM pg_rewrite w WHERE w.ev_class=c.oid)
 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_roles r ON r.oid=c.relowner
 WHERE n.nspname='ha_effects' ORDER BY c.relname"""
STORAGE_FUNCTIONS_SQL = """/* effects_storage_functions */
 SELECT p.proname,p.proargtypes::text,p.prorettype::integer,r.rolsuper,l.lanname,p.prokind,
 p.prosecdef,p.provolatile,p.proparallel,p.proleakproof,p.proisstrict,p.proretset,p.pronargdefaults,
 p.proconfig,p.prosrc
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_roles r ON r.oid=p.proowner
 JOIN pg_language l ON l.oid=p.prolang WHERE n.nspname='ha_effects' ORDER BY p.proname"""
STORAGE_ACL_SQL = """/* effects_storage_direct_acl */
 WITH objects AS (
 SELECT 'schema/ha_effects' object,n.nspowner owner,n.nspacl acl,'n'::\"char\" kind FROM pg_namespace n WHERE n.nspname='ha_effects'
 UNION ALL SELECT 'table/'||c.relname,c.relowner,c.relacl,'r'::\"char\" FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='ha_effects' AND c.relkind='r'
 UNION ALL SELECT 'function/'||p.proname,p.proowner,p.proacl,'f'::\"char\" FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='ha_effects')
 SELECT o.object,coalesce(r.rolname,'PUBLIC'),coalesce(r.rolsuper,false),a.privilege_type,a.is_grantable
 FROM objects o CROSS JOIN LATERAL aclexplode(coalesce(o.acl,acldefault(o.kind,o.owner))) a LEFT JOIN pg_roles r ON r.oid=a.grantee
 UNION ALL SELECT 'column/'||c.relname||'/'||at.attname,coalesce(r.rolname,'PUBLIC'),coalesce(r.rolsuper,false),a.privilege_type,a.is_grantable
 FROM pg_attribute at JOIN pg_class c ON c.oid=at.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
 CROSS JOIN LATERAL aclexplode(at.attacl) a LEFT JOIN pg_roles r ON r.oid=a.grantee WHERE n.nspname='ha_effects' AND at.attnum>0"""
STORAGE_INDEXES_SQL = """/* effects_storage_indexes */
 SELECT x.relname,t.relname,i.indisunique,i.indisprimary,i.indisvalid,i.indisready,
 i.indimmediate,i.indisexclusion,i.indnullsnotdistinct,am.amname,
 ARRAY(SELECT a.attname::text FROM unnest(i.indkey) WITH ORDINALITY k(num,pos)
 JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.num ORDER BY k.pos),
 i.indnatts=i.indnkeyatts,i.indexprs IS NULL,pg_get_expr(i.indpred,i.indrelid),
 ARRAY(SELECT ns.nspname||'.'||op.opcname FROM unnest(i.indclass) WITH ORDINALITY k(id,pos)
 JOIN pg_opclass op ON op.oid=k.id JOIN pg_namespace ns ON ns.oid=op.opcnamespace ORDER BY k.pos),
 i.indoption::text
 FROM pg_index i JOIN pg_class x ON x.oid=i.indexrelid JOIN pg_class t ON t.oid=i.indrelid
 JOIN pg_namespace n ON n.oid=t.relnamespace JOIN pg_am am ON am.oid=x.relam
 WHERE n.nspname='ha_effects' ORDER BY x.relname"""
STORAGE_CONSTRAINTS_SQL = """/* effects_storage_constraints */
 SELECT t.relname,c.contype,c.convalidated,c.condeferrable,c.condeferred,
 ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY k(num,pos)
 JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.num ORDER BY k.pos),
 pg_get_expr(c.conbin,c.conrelid)
 FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace
 WHERE n.nspname='ha_effects' ORDER BY t.relname,c.conname"""
STORAGE_LOGIN_SQL = """/* effects_storage_login_effective */
 SELECT r.rolname, r.rolsuper OR EXISTS(
 SELECT 1 FROM pg_roles reachable WHERE (reachable.oid=r.oid OR pg_has_role(r.oid,reachable.oid,'MEMBER')) AND (
 reachable.rolname=%s OR reachable.rolsuper
 OR has_schema_privilege(reachable.oid,'ha_effects','USAGE,CREATE')
 OR EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='ha_effects' AND c.relkind='r'
   AND (has_table_privilege(reachable.oid,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES')
     OR has_any_column_privilege(reachable.oid,c.oid,'SELECT,INSERT,UPDATE,REFERENCES')))
 OR EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='ha_effects' AND has_function_privilege(reachable.oid,p.oid,'EXECUTE'))))
 FROM pg_roles r WHERE r.rolcanlogin AND NOT r.rolsuper AND r.rolname<>%s ORDER BY r.rolname"""


def _catalog_expression(expression):
    # Strip formatting and fully enclosing parentheses only. Parentheses inside
    # CHECK expressions encode precedence and must survive canonicalization.
    source = expression or ''
    pieces = []; quoted = False; index = 0
    while index < len(source):
        char = source[index]
        if char=="'":
            pieces.append(char)
            if quoted and index+1<len(source) and source[index+1]=="'":
                pieces.append("'");index+=2;continue
            quoted=not quoted
        elif not quoted and source.startswith('::text',index):
            index+=6;continue
        elif quoted or not char.isspace():
            pieces.append(char)
        index+=1
    value=''.join(pieces)
    while value.startswith('(') and value.endswith(')'):
        depth = 0
        enclosing = True
        quoted = False
        for index,char in enumerate(value):
            if char=="'":
                quoted=not quoted
            if not quoted:
                depth += (char=='(')-(char==')')
                if depth==0 and index != len(value)-1:
                    enclosing=False
                    break
        if not enclosing:
            break
        value=value[1:-1]
    return value


STORAGE_CHECKS = {
    'binding': {'singleton=1'},
    'ledger': {"payload_sha256~'^[0-9a-f]{64}$'", "node=ANY(ARRAY['A','B'])",
               'generation>0', "state=ANY(ARRAY['pending','inflight','uncertain','completed'])",
               "(state='completed')=((provider_ackISNOTNULL)AND(resultISNOTNULL))"},
    'barriers': set(),
    'private_state': {'revision>0',"payload_sha256~'^[0-9a-f]{64}$'"}}
STORAGE_PRIMARY_KEYS = {'binding':['singleton'],'ledger':['service','request_id'],'barriers':['proof_id'],'private_state':['service','key']}


def _validate_storage_keys(connection):
    expected_indices = {name+'_pkey':(name,keys,None) for name,keys in STORAGE_PRIMARY_KEYS.items()}
    expected_indices['effect_unresolved_resource'] = ('ledger',['resource'],"state=ANY(ARRAY['pending','inflight','uncertain'])")
    rows = connection.execute(STORAGE_INDEXES_SQL).fetchall()
    if len(rows) != len(expected_indices) or {row[0] for row in rows} != set(expected_indices):
        deny('external_effect_storage_index_invalid')
    for name,table,unique,primary,valid,ready,immediate,exclusion,nulls,method,keys,no_include,no_expr,predicate,opclasses,options in rows:
        expected_table,expected_keys,expected_predicate = expected_indices[name]
        expected_ops = ['pg_catalog.int4_ops'] if name=='binding_pkey' else ['pg_catalog.text_ops']*len(expected_keys)
        if ((table,unique,primary,valid,ready,immediate,exclusion,nulls,method,keys,no_include,no_expr,opclasses,options)
                != (expected_table,True,name.endswith('_pkey'),True,True,True,False,False,'btree',expected_keys,True,True,expected_ops,' '.join(['0']*len(expected_keys)))
                or (_catalog_expression(predicate) if predicate is not None else None) != expected_predicate):
            deny('external_effect_storage_index_invalid')
    checks = {name:set() for name in STORAGE_CHECKS};primary_keys = {}
    for table,kind,valid,deferrable,deferred,keys,expression in connection.execute(STORAGE_CONSTRAINTS_SQL).fetchall():
        if table not in checks or (valid,deferrable,deferred)!=(True,False,False):
            deny('external_effect_storage_constraint_invalid')
        if kind=='p' and table not in primary_keys and expression is None:
            primary_keys[table]=keys
        elif kind=='c' and _catalog_expression(expression) not in checks[table]:
            checks[table].add(_catalog_expression(expression))
        else:
            deny('external_effect_storage_constraint_invalid')
    if checks != STORAGE_CHECKS or primary_keys != STORAGE_PRIMARY_KEYS:
        deny('external_effect_storage_constraint_invalid')


def validate_storage_catalog(connection):
    """Read-only structural/privilege acceptance, including after a restore.

    Does not require a live lease/current system ID/current-generation roles and
    never rebinds a restored system or clears any unresolved operation. This is
    storage acceptance, not an activation/transition token.
    """
    binding = connection.execute('SELECT cluster,system_id,source_commit,plan_sha256,broker_role FROM ha_effects.binding').fetchall()
    if len(binding) != 1 or len(binding[0]) != 5:
        deny('external_effect_storage_binding_invalid')
    stored = Binding(*binding[0],broker_instance='0'*64)
    stored.validate()
    broker = stored.broker_role
    if connection.execute("SELECT r.rolsuper FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner WHERE n.nspname='ha_effects'").fetchone() != (True,):
        deny('external_effect_storage_owner_invalid')
    relations = connection.execute(STORAGE_RELATIONS_SQL).fetchall()
    tables = set(STORAGE_COLUMNS)
    indices = {'binding_pkey','ledger_pkey','barriers_pkey','private_state_pkey','effect_unresolved_resource'}
    if {row[0] for row in relations} != tables | indices or len(relations) != 9:
        deny('external_effect_storage_inventory_invalid')
    for name,kind,persistence,owner,rls,forced,partition,inherited,triggers,rules in relations:
        if ((kind != ('r' if name in tables else 'i')) or persistence != 'p' or owner is not True
                or any(flag is not False for flag in (rls,forced,partition,inherited,triggers,rules))):
            deny('external_effect_storage_relation_invalid')
    columns = connection.execute("""/* effects_storage_columns */ SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull
 FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='ha_effects' AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum""").fetchall()
    actual = {name: [] for name in tables}
    for name,*column in columns:
        if name not in actual:
            deny('external_effect_storage_columns_invalid')
        actual[name].append(tuple(column))
    if actual != STORAGE_COLUMNS:
        deny('external_effect_storage_columns_invalid')
    _validate_storage_keys(connection)
    functions = connection.execute(STORAGE_FUNCTIONS_SQL).fetchall()
    bodies = {'observe':('25 25 20',observer_body()),'peer_observe':('',PEER_SQL)}
    if len(functions) != 2 or {row[0] for row in functions} != set(bodies):
        deny('external_effect_storage_observer_invalid')
    for name,args,returns,owner,language,kind,definer,volatility,parallel,leak,strict,retset,defaults,config,body in functions:
        if ((args,returns,owner,language,kind,definer,volatility,parallel,leak,strict,retset,defaults)
                != (bodies[name][0],3802,True,'sql','f',True,'v','u',False,False,False,0)
                or config is None or [re.sub(r'\s+','',item) for item in config] != ['search_path=pg_catalog,ha,ha_effects,pg_temp']
                or body != bodies[name][1]):
            deny('external_effect_storage_observer_invalid')
    expected = {'schema/ha_effects':{'USAGE'},'table/binding':{'SELECT'},
                'table/ledger':{'SELECT','INSERT','UPDATE'},'table/barriers':{'SELECT','INSERT'},
                'table/private_state':{'SELECT','INSERT','UPDATE'},
                'function/observe':{'EXECUTE'},'function/peer_observe':{'EXECUTE'}}
    granted = {name:set() for name in expected}
    for object_name,role,superuser,privilege,grantable in connection.execute(STORAGE_ACL_SQL).fetchall():
        if superuser is True:
            continue
        if (role != broker or object_name not in expected or privilege not in expected[object_name] or grantable is not False):
            deny('external_effect_storage_acl_escape')
        granted[object_name].add(privilege)
    if granted != expected:
        deny('external_effect_storage_broker_grants_invalid')
    # Built-in NONLOGIN capability roles such as pg_write_all_data are not
    # themselves attackers. A LOGIN inheriting/SET-ROLEing to them is rejected.
    if any(row[1] is not False for row in connection.execute(STORAGE_LOGIN_SQL,(broker,broker)).fetchall()):
        deny('external_effect_storage_effective_privilege_escape')
    if connection.execute("SELECT NOT (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls OR rolinherit) AND rolcanlogin AND NOT EXISTS(SELECT 1 FROM pg_roles p WHERE p.rolname<>r.rolname AND pg_has_role(r.oid,p.oid,'MEMBER')) FROM pg_roles r WHERE rolname=%s",(broker,)).fetchone() != (True,):
        deny('external_effect_private_broker_role_required')
    identities = connection.execute('SELECT role_name FROM ha.identities ORDER BY role_name').fetchall()
    if (not 14 <= len(identities) <= 4096 or len({row[0] for row in identities}) != len(identities)
            or broker in {row[0] for row in identities}):
        deny('external_effect_app_inventory_required')
    # Include historical and NOLOGIN app roles, which may be reached by SET ROLE.
    query = STORAGE_LOGIN_SQL.replace("WHERE r.rolcanlogin AND NOT r.rolsuper AND r.rolname<>%s", "WHERE r.rolname=ANY(%s)")
    names = [row[0] for row in identities]
    rows = connection.execute(query,(broker,names)).fetchall()
    if [row[0] for row in rows] != names or any(row[1] is not False for row in rows):
        deny('external_effect_app_privilege_escape')
    return {'storage_catalog_verified':True,'activation_verified':False,'unresolved_records_cleared':False}


class CredentialStateBackend:
    """Private OAuth state; usable only by an authenticated dispatch adapter.

    No token/state payload enters the ledger or a peer proof result. The caller
    must never expose this trusted dependency through an app request route.
    """
    def __init__(self, broker):
        self._broker = broker
        self._local = threading.local()

    def _context(self, service, key='hyundai_tokens'):
        context = getattr(self._broker._dispatch_local,'context',None)
        if (context is None or service != 'car-care-worker' or key != 'hyundai_tokens'
                or context[0].service != service or time.monotonic() >= context[2]):
            deny('external_effect_private_state_context_required')
        return context

    def _probe(self, connection, service):
        identity,resource,deadline = self._context(service)
        value = self._broker._begin(connection,identity)
        if time.monotonic() >= min(deadline,value['_lease_deadline']):
            deny('external_effect_private_state_context_required')
        return value

    def require_worker(self, service):
        self._context(service)
        connection = self._broker._session()
        try:
            with _tx(connection):
                self._probe(connection,service)
        finally:
            connection.close()

    @contextmanager
    def worker_scope(self, service):
        # Dispatch owns ROLE_LOCK and the resource lock. Never release or
        # reacquire them from a nested credential backend session.
        self.require_worker(service)
        yield
        self.require_worker(service)

    @contextmanager
    def state_transaction(self, service, key, default):
        self._context(service,key)
        if getattr(self._local,'active',None) is not None:
            deny('external_effect_nested_private_state_transaction')
        connection = peer = None
        try:
            connection = self._broker._session()
            if connection.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0))',('private-state:'+service+':'+key,)).fetchone() != (True,):
                deny('external_effect_private_state_busy')
            peer = self._broker._session(peer=True)
            with _tx(connection):
                self._probe(connection,service)
                row = connection.execute('SELECT payload,revision,payload_sha256 FROM ha_effects.private_state WHERE service=%s AND key=%s FOR UPDATE',(service,key)).fetchone()
                payload = copy.deepcopy(row[0] if row else default)
                if type(payload) is not dict or (row and hashlib.sha256(canonical(payload)).hexdigest()!=row[2]):
                    deny('external_effect_private_state_invalid')
                revision = row[1] if row else 0
                # Prove old credentials before they can reach a provider.
                if row:
                    receipt = peer.execute('SELECT revision,payload_sha256 FROM ha_effects.private_state WHERE service=%s AND key=%s',(service,key)).fetchone()
                    if receipt != (revision,row[2]):
                        deny('external_effect_private_state_peer_unavailable')
                self._local.active = (service,key,payload)
                try:
                    yield payload
                finally:
                    del self._local.active
                self._probe(connection,service)
                digest = hashlib.sha256(canonical(payload)).hexdigest()
                if row:
                    changed = connection.execute('UPDATE ha_effects.private_state SET payload=%s::jsonb,revision=revision+1,payload_sha256=%s WHERE service=%s AND key=%s AND revision=%s RETURNING revision',(canonical(payload).decode(),digest,service,key,revision)).fetchone()
                    if changed != (revision+1,):
                        deny('external_effect_private_state_revision_conflict')
                else:
                    connection.execute('INSERT INTO ha_effects.private_state(service,key,payload,revision,payload_sha256) VALUES(%s,%s,%s::jsonb,1,%s)',(service,key,canonical(payload).decode(),digest))
            self._durable(connection,peer,service,key,revision+1,digest)
        except EffectsBlocked:
            raise
        except Exception:
            # Never include the original database/provider error: it can carry
            # token-bearing SQL parameters or JSON in a server error detail.
            deny('external_effect_private_state_unavailable')
        finally:
            if peer is not None:peer.close()
            if connection is not None:connection.close()

    def _durable(self, connection, peer, service, key, revision, digest):
        identity,resource,deadline = self._context(service,key)
        watermark = connection.execute('SELECT pg_current_wal_insert_lsn()::text').fetchone()[0]
        attempt = uuid.uuid4().hex
        with _tx(connection):
            before = self._probe(connection,service)
            connection.execute('INSERT INTO ha_effects.barriers(proof_id,service,request_id,phase,generation) VALUES(%s,%s,%s,%s,%s)',(attempt,service,key,'private-state',identity.generation))
        row = peer.execute('SELECT ha_effects.peer_observe()').fetchone()
        value = row[0] if row else None
        if (type(value) is not dict or value.get('role') != self._broker.binding.broker_role
                or value.get('binding') != _expected(self._broker.binding) or value.get('recovery') is not True
                or value.get('system_id') != self._broker.binding.system_id or value.get('cluster') != self._broker.binding.cluster
                or value.get('node') != before['partner'] or value.get('primary') != before['primary']
                or type(value.get('generation')) is not int or value['generation'] != identity.generation
                or value.get('worker') != identity.node or value.get('timeline') != before['timeline']
                or value.get('actual_timeline') != before['timeline'] or _lsn(value.get('replay')) < _lsn(watermark)):
            deny('external_effect_private_state_peer_unavailable')
        receipt = peer.execute('SELECT revision,payload_sha256 FROM ha_effects.private_state WHERE service=%s AND key=%s',(service,key)).fetchone()
        own = connection.execute('SELECT payload,revision,payload_sha256 FROM ha_effects.private_state WHERE service=%s AND key=%s',(service,key)).fetchone()
        barrier = peer.execute('SELECT service,request_id,phase,generation FROM ha_effects.barriers WHERE proof_id=%s',(attempt,)).fetchone()
        if (receipt != (revision,digest) or own is None or own[1:] != (revision,digest)
                or hashlib.sha256(canonical(own[0])).hexdigest()!=digest
                or barrier != (service,key,'private-state',identity.generation)):
            deny('external_effect_private_state_peer_unavailable')
        with _tx(connection):
            after = self._probe(connection,service)
            if any(after[field] != before[field] for field in ('generation','worker','primary','partner','timeline')):
                deny('external_effect_private_state_context_required')

    def read_state(self, service, key, default):
        self._context(service,key)
        active = getattr(self._local,'active',None)
        if active is not None:
            if active[:2] != (service,key):
                deny('external_effect_private_state_context_required')
            return copy.deepcopy(active[2])
        # Same path proves the existing token revision and cannot leak a local
        # unreplicated token. No external call occurs inside the transaction.
        result = None
        with self.state_transaction(service,key,default) as payload:
            result=copy.deepcopy(payload)
        return result


def _content_fingerprints(connection):
    """TCB-only comparison; no private row or token material leaves memory."""
    result={}
    for table,order in (('ledger','service,request_id'),('barriers','proof_id'),('private_state','service,key')):
        digest=hashlib.sha256();count=0
        for row in connection.execute('SELECT to_jsonb(t) FROM ha_effects.'+table+' t ORDER BY '+order).fetchall():
            raw=json.dumps(row[0],sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
            digest.update(len(raw).to_bytes(8,'big'));digest.update(raw);count+=1
        result[table]=(count,digest.hexdigest())
    return result


def rebind_restored(connection, old_binding, new_binding):
    """Administrator-only metadata replacement on a closed restored system.

    This is not old-VM fencing, credential provisioning, reconciliation or a
    permission to open service. A controller must separately prove positive
    fencing, fresh generation roles and the two-node unresolved-effect gate.
    """
    if type(old_binding) is not Binding or type(new_binding) is not Binding:
        deny('external_effect_rebind_identity_required')
    old_binding.validate();new_binding.validate()
    if (new_binding.cluster!=old_binding.cluster or new_binding.system_id==old_binding.system_id
            or new_binding.broker_role==old_binding.broker_role):
        deny('external_effect_rebind_fresh_identity_required')
    try:
        from psycopg import sql
        with connection.transaction():
            connection.execute('SELECT pg_advisory_xact_lock(%s)',(ROLE_LOCK,))
            if connection.execute('SELECT rolsuper FROM pg_roles WHERE rolname=session_user AND session_user=current_user').fetchone()!=(True,):
                deny('external_effect_admin_required')
            observed=connection.execute('SELECT pg_is_in_recovery(),system_identifier::text FROM pg_control_system()').fetchone()
            if observed!=(False,new_binding.system_id):
                deny('external_effect_rebind_fresh_system_required')
            closed=connection.execute('SELECT cluster,writes_open,history_safe,expires_at<clock_timestamp() FROM ha.control WHERE singleton=1').fetchone()
            if closed!=(old_binding.cluster,False,False,True):
                deny('external_effect_rebind_closed_expired_authority_required')
            validate_storage_catalog(connection)
            if connection.execute(BINDING_SQL).fetchone()!=tuple(_expected(old_binding).values()):
                deny('external_effect_rebind_old_binding_mismatch')
            role=connection.execute('''SELECT rolcanlogin,rolinherit,rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls,
 NOT EXISTS(SELECT 1 FROM pg_auth_members m WHERE m.member=r.oid OR m.roleid=r.oid),
 NOT EXISTS(SELECT 1 FROM ha.identities i WHERE i.role_name=r.rolname)
 FROM pg_roles r WHERE rolname=%s''',(new_binding.broker_role,)).fetchone()
            if role!=(True,False,False,False,False,False,False,True,True):
                deny('external_effect_rebind_preprovisioned_role_required')
            before=_content_fingerprints(connection)
            control=connection.execute('SELECT to_jsonb(c) FROM ha.control c WHERE singleton=1').fetchone()
            old=sql.Identifier(old_binding.broker_role);new=sql.Identifier(new_binding.broker_role)
            for statement in ('REVOKE ALL ON ALL TABLES IN SCHEMA ha_effects FROM {}',
                              'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA ha_effects FROM {}',
                              'REVOKE ALL ON SCHEMA ha_effects,ha FROM {}',
                              'REVOKE ALL ON ha.control,ha.enforced_roles,ha.identities FROM {}',
                              'ALTER ROLE {} NOLOGIN'):
                connection.execute(sql.SQL(statement).format(old))
            for statement in ('GRANT USAGE ON SCHEMA ha_effects,ha TO {}',
                              'GRANT SELECT ON ha_effects.binding,ha.control,ha.enforced_roles,ha.identities TO {}',
                              'GRANT SELECT,INSERT,UPDATE ON ha_effects.ledger,ha_effects.private_state TO {}',
                              'GRANT SELECT,INSERT ON ha_effects.barriers TO {}',
                              'GRANT EXECUTE ON FUNCTION ha_effects.observe(text,text,bigint),ha_effects.peer_observe() TO {}'):
                connection.execute(sql.SQL(statement).format(new))
            changed=connection.execute('UPDATE ha_effects.binding SET cluster=%s,system_id=%s,source_commit=%s,plan_sha256=%s,broker_role=%s WHERE singleton=1 RETURNING singleton',tuple(_expected(new_binding).values())).fetchone()
            if changed!=(1,):deny('external_effect_rebind_binding_update_failed')
            validate_storage_catalog(connection)
            if _content_fingerprints(connection)!=before or connection.execute('SELECT to_jsonb(c) FROM ha.control c WHERE singleton=1').fetchone()!=control:
                deny('external_effect_rebind_content_changed')
    except EffectsBlocked:
        raise
    except Exception:
        raise EffectsBlocked('external_effect_rebind_unavailable') from None
    return {'rebound':True,'content_preserved':True,'old_broker_login_disabled':True,
            'activation_verified':False,'external_fence_verified':False,'unresolved_records_cleared':False,
            'roles_or_passwords_created':False}
