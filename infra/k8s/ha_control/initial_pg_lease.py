"""Open the first A lease without changing the restored A/B replication history."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from datetime import datetime
import hashlib
import json
import re
import time
import uuid

from .controller import TransitionBlocked
from .pg import LEASE_SECONDS, PROOF_SECONDS, ROLE_LOCK, PgUnavailable, _deny


INITIAL_CUTOVER = {'schema_version': 1, 'primary_node': 'A', 'partner_node': 'B'}
# Closed preparation and the authority transaction each have a bounded
# window. The two fresh authority proofs share one window around the commit.
INITIAL_PROOF_SECONDS = 4
INITIAL_OBSERVATION_STATEMENT_MS = 1200
RETRYABLE_CATALOG_CANCELS = frozenset(
    f'initial_pg_{node}_{phase}_canceled'
    for node in ('a', 'b') for phase in ('effects_catalog', 'effects_binding')
)
INITIAL_REVISION = 1
OPEN_REVISION = 2
RECOVERY_CLOSED_REVISION = 3
RECOVERY_OPEN_REVISION = 4
RECOVERY_REVOKED_REVISION = 5
RECOVERY_ACTION = 'ha-recover-initial-writer'
REARM_ACTION = 'ha-rearm-initial-writer'
REARM_FIELDS = {'schema_version', 'attempt_id', 'predecessor_terminal_sha256', 'predecessor_binding_sha256',
                'predecessor_args_sha256', 'closure_mode'}
RECOVERY_FIELDS = ('attempt_id', 'predecessor_terminal_sha256', 'predecessor_service_sha256')
QUARANTINED_OPERATIONS = frozenset({'car.telegram.poll', 'car.hyundai.snapshot'})
STAMP = """singleton=1 AND cluster=%s AND epoch=1 AND revision=%s
    AND primary_node='A' AND partner='B' AND timeline=1 AND serving=ARRAY['A']::text[]
    AND worker='A' AND mode='AS' AND writes_open=%s AND history_safe=%s AND expires_at=%s
    AND current_setting('cluster_name')='ha_A' AND NOT pg_is_in_recovery()
    AND (SELECT system_identifier::text FROM pg_control_system())=%s
    AND ('x'||substring(pg_walfile_name(pg_current_wal_insert_lsn()),1,8))::bit(32)::integer=1"""
PROBE_READ = 'SELECT generation FROM ha.controller_probes WHERE nonce=%s'


def rearm_scope(plan):
    value = plan.get('initial_recovery_rearm')
    recovery = plan.get('initial_cutover_recovery')
    if (type(value) is not dict or set(value) != REARM_FIELDS
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['closure_mode'] != 'windows-offline-v1'
            or not all(type(value[key]) is str and re.fullmatch(r'[a-f0-9]{64}', value[key])
                       for key in REARM_FIELDS - {'schema_version', 'closure_mode'})
            or type(recovery) is not dict or value['attempt_id'] in {
                recovery.get('attempt_id'), plan.get('initial_cutover_handoff', {}).get('attempt_id')}):
        _deny('initial_rearm_scope_unverified')
    return value.copy()


class LegacyResponseTimeout(TransitionBlocked):
    """A Windows proof response timed out at a known initial lease boundary."""

    def __init__(self, phase):
        super().__init__('legacy_pipe_response_timeout')
        self.failure_code = {
            'precommit': 'legacy_pipe_response_timeout_precommit',
            'postcommit': 'legacy_pipe_response_timeout_postcommit',
            'refresh': 'legacy_pipe_response_timeout_refresh',
        }[phase]


@contextmanager
def _pg_phase(node, phase):
    try:
        yield
    except TransitionBlocked:
        raise
    except Exception as error:
        sqlstate = getattr(error, 'sqlstate', None)
        if sqlstate is None:
            raise
        raise PgUnavailable(node, phase, sqlstate) from None


class InitialPgLease:
    """Use a held PgBackend plan and a fresh asserted legacy closure callback.

    The caller durably authorizes an explicit open and holds one controller
    guard. Renewal cannot reopen a closed lease. This object keeps the exact
    expiry stamps it can revoke after an ambiguous response.
    """

    _approval_action = 'ha-initial-wsl-cutover'
    _closed_revision = INITIAL_REVISION
    _open_revision = OPEN_REVISION
    _revoked_revision = RECOVERY_CLOSED_REVISION
    _owned_renewal_only = False
    _explicit_proof_only = False
    _allow_rearm = False
    _allow_postboot = False

    def __init__(self, backend, assert_legacy_closed, *, assert_exclusion_held=None):
        if not callable(assert_legacy_closed) or (assert_exclusion_held is not None and not callable(assert_exclusion_held)):
            _deny('initial_legacy_closure_required')
        self.backend = backend
        self.assert_legacy_closed = assert_legacy_closed
        self.assert_exclusion_held = assert_exclusion_held or assert_legacy_closed
        self.independent_exclusion = assert_exclusion_held is not None
        self.owned_expiries = set()
        self.open_attempted = False
        self.scope = None
        self.proof_started = None
        self.rearm = None
        self.postboot = None

    def _bind(self, state):
        plan = self.backend.plan
        if not self.backend.depth or type(plan) is not dict:
            _deny('native_host_guard_required')
        if 'initial_recovery_postboot' in plan and not self._allow_postboot:
            _deny('initial_postboot_scope_unverified')
        if 'initial_recovery_rearm' in plan and not self._allow_rearm:
            _deny('initial_rearm_scope_unverified')
        mode = plan.get('initial_wsl_cutover')
        if (plan.get('_approval_action') != self._approval_action or plan.get('profile') != 'enforced-vm-v1'
                or type(mode) is not dict or mode != INITIAL_CUTOVER or type(mode['schema_version']) is not int):
            _deny('initial_cutover_approval_required')
        effects = plan.get('pg_enforcement', {}).get('effects', {})
        if type(effects) is not dict or 'deployment_binding' not in effects:
            _deny('initial_cutover_deployment_binding_required')
        deployment = self.backend._effects_deployment_binding()
        if (type(state) is not dict or not isinstance(state.get('cluster'), str) or not state['cluster']
                or state['cluster'] != plan.get('cluster') or state.get('target') != 'A' or state.get('old') != 'B'
                or plan.get('node') != 'A'
                or any(type(value) is not int or value != 1 for value in (
                    state.get('generation'), state.get('timeline'), plan.get('generation'), plan.get('timeline')))
                or not re.fullmatch(r'[1-9][0-9]{0,19}', str(plan.get('system_id', '')))):
            _deny('initial_cutover_state_unbound')
        controller = self.backend.fence_adapter.plan_sha256
        if type(controller) is not str or not re.fullmatch(r'[a-f0-9]{64}', controller):
            _deny('initial_cutover_state_unbound')
        scope = (controller, deployment, plan['cluster'], str(plan['system_id']), self._recovery_binding(plan))
        if self.scope is not None and self.scope != scope:
            _deny('initial_cutover_state_unbound')
        self.scope = scope

    def _recovery_binding(self, plan):
        if 'initial_cutover_recovery' in plan:
            _deny('initial_cutover_approval_required')
        return ()

    def _probe_prefix(self):
        return 'initial-a:'

    @staticmethod
    def _clear_probes(primary, prefix):
        primary.execute('DELETE FROM ha.controller_probes WHERE generation=1 AND left(nonce,%s)=%s',
                        (len(prefix), prefix))

    @staticmethod
    def _deadline(started):
        if time.monotonic() - started >= INITIAL_PROOF_SECONDS:
            _deny('initial_replication_deadline_exceeded')

    def _legacy(self, state, started, *, held_only=False, phase=None):
        self._deadline(started)
        check = self.assert_exclusion_held if held_only else self.assert_legacy_closed
        observed_from = time.monotonic()
        try:
            observed = check(state, started + INITIAL_PROOF_SECONDS)
        except TransitionBlocked as error:
            if phase is not None and str(error) == 'legacy_pipe_response_timeout':
                raise LegacyResponseTimeout(phase) from None
            raise
        if observed is not True and (self._explicit_proof_only or observed is not None):
            _deny('initial_legacy_closure_required')
        self._deadline(started)
        return observed_from

    def _postcommit_closure(self, state, started, observed_from, *, renewing):
        # Renewal retains the previously verified exclusion under the same
        # owner. A sample that already aged out needs a new full observation.
        reuse = (renewing and self.independent_exclusion
                 and time.monotonic() - observed_from < PROOF_SECONDS)
        self._legacy(state, started, held_only=reuse, phase='postcommit')
        return reuse

    def _assert_observed_node(self, value, node, state, *, opened):
        revision = self._open_revision if opened else self._closed_revision
        standby = 'B' if node == 'A' else 'A'
        fields = {'cluster': state['cluster'], 'epoch': 1, 'revision': revision, 'primary': 'A', 'partner': 'B',
                    'timeline': 1, 'actual_timeline': 1, 'serving': ['A'], 'worker': 'A', 'mode': 'AS',
                    'system_id': str(self.backend.plan['system_id']), 'writes': opened, 'history': opened,
                    'fsync': True, 'full_page_writes': True, 'node': 'ha_' + node,
                    'recovery': node == 'B', 'sync_names': 'FIRST1(ha_' + standby.lower() + ')'}
        if any(type(value.get(key)) is not type(item) or value[key] != item for key, item in fields.items()):
            _deny('initial_replication_identity_unverified')
        remaining = value['remaining']
        if (opened and not 0 < remaining <= LEASE_SECONDS) or (not opened and not remaining < 0):
            _deny('initial_lease_stamp_unverified')
        if node == 'A' and value['sync_peer'] != 'ha_b':
            _deny('initial_replication_identity_unverified')

    def _pair(self, primary, peer, state, *, opened):
        self._assert_observed_node(primary, 'A', state, opened=opened)
        self._assert_observed_node(peer, 'B', state, opened=opened)

    @staticmethod
    def _expiry(row):
        if not row or len(row) != 1 or not isinstance(row[0], datetime) or row[0].utcoffset() is None:
            _deny('initial_lease_stamp_unverified')
        return row[0]

    def _observe_pair(self, primary, peer, state, *, opened, started, after_commit=False, retry_catalog=False):
        sample = self.backend._observe_runtime if after_commit else self.backend._observe
        phase = 'postcommit_observe' if after_commit else 'observe'

        def observe(connection, node):
            with _pg_phase(node, phase):
                remaining_ms = int((started + INITIAL_PROOF_SECONDS - time.monotonic()) * 1000)
                if remaining_ms <= 0:
                    _deny('initial_replication_deadline_exceeded')
                timeout_ms = min(INITIAL_OBSERVATION_STATEMENT_MS, remaining_ms)
                connection.execute(f'SET statement_timeout={timeout_ms}')
                value = sample(connection, node, state)
                # The failed path closes both connections. Restore PgBackend's
                # shorter limit before the expiry read and any write or commit.
                connection.execute("SET statement_timeout='700ms'")
            with _pg_phase(node, 'read_expiry'):
                expiry = self._expiry(connection.execute('SELECT expires_at FROM ha.control WHERE singleton=1').fetchone())
            return value, expiry

        # Keep the primary transaction and its role lock on the caller thread.
        # Joining the standby read here keeps its connection alive on failure.
        with ThreadPoolExecutor(max_workers=1) as pool:
            standby = pool.submit(observe, peer, 'B')
            try:
                current, expires = observe(primary, 'A')
            except PgUnavailable as error:
                if retry_catalog and error.failure_code in RETRYABLE_CATALOG_CANCELS:
                    try:
                        replica, _ = standby.result()
                    except PgUnavailable as peer_error:
                        if peer_error.failure_code not in RETRYABLE_CATALOG_CANCELS:
                            raise
                    else:
                        self._assert_observed_node(replica, 'B', state, opened=opened)
                raise
            try:
                replica, replica_expires = standby.result()
            except PgUnavailable as error:
                if retry_catalog and error.failure_code in RETRYABLE_CATALOG_CANCELS:
                    self._assert_observed_node(current, 'A', state, opened=opened)
                raise
        self._deadline(started)
        self._pair(current, replica, state, opened=opened)
        if expires != replica_expires:
            _deny('initial_lease_stamp_unverified')
        return current, replica, expires

    def _parameters(self, state, revision, opened, expires):
        return (state['cluster'], revision, opened, opened, expires, str(self.backend.plan['system_id']))

    @staticmethod
    def _begin(connection, *, shared):
        with _pg_phase('A', 'begin'):
            connection.execute('BEGIN')
            connection.execute('SET LOCAL synchronous_commit=remote_apply')
        with _pg_phase('A', 'role_lock'):
            lock = 'SELECT pg_advisory_xact_lock_shared(%s)' if shared else 'SELECT pg_advisory_xact_lock(%s)'
            connection.execute(lock, (ROLE_LOCK,))

    def _proof(self, state, expires, nonce):
        payload = {'cluster': state['cluster'], 'epoch': 1, 'revision': self._open_revision, 'timeline': 1,
                   'primary': 'A', 'partner': 'B', 'serving': ['A'], 'worker': 'A', 'mode': 'AS',
                   'controller_sha256': self.scope[0], 'deployment_sha256': self.scope[1][1],
                   'system_id': self.scope[3], 'expires_at': expires.isoformat(), 'nonce': nonce}
        if self.scope[4]:
            payload['recovery'] = dict(zip(RECOVERY_FIELDS, self.scope[4]))
        if self.rearm is not None:
            payload['rearm'] = self.rearm
        if self.postboot is not None:
            payload['postboot'] = self.postboot
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def _commit_primary(self, primary):
        with _pg_phase('A', 'commit'):
            self.backend._commit(primary)

    def _effects_gate(self, primary, peer, watermark, started):
        self.backend._effects_open_gate(primary, peer, watermark)

    def _renewal_effects_gate(self, primary, peer, started):
        return

    def _lease(self, state, *, renewing, started):
        prefix = self._probe_prefix()
        nonce = prefix + uuid.uuid4().hex
        barrier, lease_probe = nonce + ':fence', nonce + ':lease'
        with ExitStack() as connections:
            # Only read PostgreSQL until the independent closure proof succeeds.
            with ThreadPoolExecutor(max_workers=1) as pool:
                closure = pool.submit(self._legacy, state, started, phase='precommit')
                primary = connections.enter_context(self.backend._connection('A'))
                peer = connections.enter_context(self.backend._connection('B'))
                self._begin(primary, shared=renewing)
                try:
                    current, _, previous = self._observe_pair(
                        primary, peer, state, opened=renewing, started=started, retry_catalog=renewing)
                except PgUnavailable as error:
                    if renewing and error.failure_code in RETRYABLE_CATALOG_CANCELS:
                        closure.result()
                    raise
                observed_from = closure.result()
            if renewing:
                self._renewal_effects_gate(primary, peer, started)
                if self._owned_renewal_only and previous not in self.owned_expiries:
                    _deny('initial_lease_reconciliation_required')
                self.owned_expiries = {previous}
                # The matching open stamp is already observed on both nodes.
                # Keep the shared role lock through the next remote_apply
                # commit, which carries the new stamp and its own nonce.
                watermark = self.backend._lsn(current['flush'])
            else:
                self._deadline(started)
                with _pg_phase('A', 'fence_probe'):
                    self._clear_probes(primary, prefix)
                    primary.execute('INSERT INTO ha.controller_probes(nonce,generation) VALUES(%s,1)', (barrier,))
                self._deadline(started)
                self._commit_primary(primary)
                with _pg_phase('A', 'read_watermark'):
                    watermark = self.backend._lsn(primary.execute('SELECT pg_current_wal_flush_lsn()::text').fetchone()[0])

                self._begin(primary, shared=False)
                _, replica, expires = self._observe_pair(primary, peer, state, opened=False, started=started)
                with _pg_phase('B', 'read_fence_probe'):
                    if (expires != previous or peer.execute(PROBE_READ, (barrier,)).fetchone() != (1,)
                            or self.backend._lsn(replica['replay']) < watermark):
                        _deny('initial_replication_barrier_unconfirmed')
                self._effects_gate(primary, peer, watermark, started)
                # No app writes are authorized during preparation. Start the
                # authority window under the same exclusive role lock only
                # after the closed catalog and replication checks succeeded.
                self._deadline(started)
                started = self.proof_started = time.monotonic()
                self._legacy(state, started, phase='precommit')
            self._legacy(state, started, held_only=True)
            change = '' if renewing else f'revision={self._open_revision}, writes_open=true, history_safe=true, '
            condition = 'expires_at>clock_timestamp()' if renewing else 'expires_at<clock_timestamp()'
            revision = self._open_revision if renewing else self._closed_revision
            with _pg_phase('A', 'update_expiry'):
                row = primary.execute(
                    'UPDATE ha.control SET ' + change + "expires_at=clock_timestamp()+interval '5 seconds' WHERE "
                    + STAMP + ' AND ' + condition + ' RETURNING expires_at',
                    self._parameters(state, revision, renewing, previous),
                ).fetchone()
            current_expiry = self._expiry(row)
            self.owned_expiries.add(current_expiry)
            with _pg_phase('A', 'write_lease_probe'):
                if renewing:
                    self._clear_probes(primary, prefix)
                primary.execute('INSERT INTO ha.controller_probes(nonce,generation) VALUES(%s,1)', (lease_probe,))
            self._deadline(started)
            self._commit_primary(primary)

            with ThreadPoolExecutor(max_workers=1) as pool:
                closure = pool.submit(self._postcommit_closure, state, started, observed_from, renewing=renewing)
                # The privilege catalogs were checked under the role lock
                # before this exact synchronous commit. Confirm that the
                # issued lease and nonce reached both nodes.
                current, replica, expires = self._observe_pair(
                    primary, peer, state, opened=True, started=started, after_commit=True)
                def probe_visible(connection, node):
                    with _pg_phase(node, 'read_lease_probe'):
                        return connection.execute(PROBE_READ, (lease_probe,)).fetchone() == (1,)

                if (expires != current_expiry or not probe_visible(primary, 'A') or not probe_visible(peer, 'B')
                        or min(self.backend._lsn(current['flush']), self.backend._lsn(replica['flush']),
                               self.backend._lsn(replica['replay'])) < watermark):
                    _deny('initial_replication_barrier_unconfirmed')
                reused = closure.result()
            if reused and time.monotonic() - observed_from >= PROOF_SECONDS:
                self._legacy(state, started, phase='refresh')
            self._deadline(started)
            self.owned_expiries = {current_expiry}
            return self._proof(state, current_expiry, lease_probe)

    def _change(self, state, *, renewing):
        self._bind(state)
        if not renewing:
            if self.open_attempted:
                _deny('initial_lease_reconciliation_required')
            self.open_attempted = True
        started = time.monotonic()
        self.proof_started = started if renewing else None
        try:
            try:
                return self._lease(state, renewing=renewing, started=started)
            except PgUnavailable as error:
                if not renewing or error.failure_code not in RETRYABLE_CATALOG_CANCELS:
                    raise
                # These codes originate in the full, read-only catalog check
                # before any renewal mutation. Use new connections and repeat
                # the closure and unexpired paired-stamp checks in this budget.
                self._deadline(started)
                return self._lease(state, renewing=True, started=started)
        except Exception:
            try:
                self.revoke(state)
            except Exception:
                # A lost/blocked revoke leaves only the existing DB-clock TTL.
                pass
            raise

    def open(self, state):
        """Explicitly open only the exact closed revision bound to the plan."""
        return self._change(state, renewing=False)

    def renew(self, state):
        """Extend an already open, unexpired initial A lease only."""
        return self._change(state, renewing=True)

    def observe_open(self, state):
        """Read both live replicas and legacy closure without changing authority."""
        self._bind(state)
        started = time.monotonic()
        self._legacy(state, started)
        with self.backend._connection('A') as primary, self.backend._connection('B') as peer:
            _, _, expires = self._observe_pair(primary, peer, state, opened=True, started=started)
        self._legacy(state, started)
        return self._proof(state, expires, None)

    def revoke(self, state):
        """Close only an expiry stamp seen or issued by this object on exact A."""
        self._bind(state)
        if not self.owned_expiries:
            return False
        with self.backend._connection('A') as connection:
            connection.execute('BEGIN')
            connection.execute('SET LOCAL synchronous_commit=local')
            connection.execute('SELECT pg_advisory_xact_lock(%s)', (ROLE_LOCK,))
            for expires in sorted(self.owned_expiries):
                result = connection.execute(
                    f'UPDATE ha.control SET revision={self._revoked_revision},writes_open=false,history_safe=false,'
                    "expires_at=clock_timestamp()-interval '1 second' WHERE " + STAMP,
                    self._parameters(state, self._open_revision, True, expires),
                )
                if result.rowcount == 1:
                    self.backend._commit(connection)
                    self.owned_expiries.clear()
                    return True
            connection.execute('ROLLBACK')
        return False


class InitialPgLeaseRecovery(InitialPgLease):
    """Open the same A only from the exact revoked initial lease revision.

    The caller must verify the distinct approved recovery attempt and its
    predecessor service and terminal record under the native controller lock.
    PostgreSQL also independently binds each observation and update to this
    attempt, and renewal cannot adopt another instance's open expiry.
    """

    _approval_action = RECOVERY_ACTION
    _closed_revision = RECOVERY_CLOSED_REVISION
    _open_revision = RECOVERY_OPEN_REVISION
    _revoked_revision = RECOVERY_REVOKED_REVISION
    _owned_renewal_only = True
    _explicit_proof_only = True

    def __init__(self, backend, assert_legacy_closed, *, assert_exclusion_held=None, assert_quarantine_held=None):
        super().__init__(backend, assert_legacy_closed, assert_exclusion_held=assert_exclusion_held)
        self.assert_quarantine_held = assert_quarantine_held
        self.quarantine = None

    def _bind(self, state):
        previously_bound = self.scope is not None
        super()._bind(state)
        selected = self.quarantine_binding(self.backend.plan)
        if previously_bound and selected != self.quarantine:
            _deny('initial_recovery_quarantine_unverified')
        self.quarantine = selected

    @staticmethod
    def quarantine_binding(plan):
        if 'initial_effect_quarantine' not in plan:
            return None
        value = plan['initial_effect_quarantine']
        if (type(value) is not dict or set(value) != {'rows', 'broker_scope_sha256', 'hyundai_state_sha256',
                                                    'resource_set_sha256'}
                or type(value['rows']) is not dict or set(value['rows']) != QUARANTINED_OPERATIONS
                or not all(type(pin) is str and re.fullmatch(r'[a-f0-9]{64}', pin) for pin in (
                    *value['rows'].values(), value['broker_scope_sha256'], value['hyundai_state_sha256'],
                    value['resource_set_sha256']))
                or len(set(value['rows'].values())) != len(QUARANTINED_OPERATIONS)):
            _deny('initial_recovery_quarantine_unverified')
        return (tuple(sorted(value['rows'].items())), value['broker_scope_sha256'],
                value['hyundai_state_sha256'], value['resource_set_sha256'])

    def _assert_provider_isolation(self, started):
        if self.quarantine is not None:
            self._deadline(started)
            if (not callable(self.assert_quarantine_held)
                    or self.assert_quarantine_held(self.quarantine[1], self.quarantine[3],
                                                   started + INITIAL_PROOF_SECONDS) is not True):
                _deny('initial_recovery_quarantine_unverified')
            self._deadline(started)

    def _legacy(self, state, started, **options):
        if self.quarantine is None:
            return super()._legacy(state, started, **options)
        # These observations use separate authenticated clients and share the
        # same deadline. Join both before any caller can authorize a write.
        with ThreadPoolExecutor(max_workers=1) as pool:
            isolation = pool.submit(self._assert_provider_isolation, started)
            observed_from = super()._legacy(state, started, **options)
            isolation.result()
        self._deadline(started)
        return observed_from

    @staticmethod
    def _quarantine_observation(connection, *, renewal=False):
        def digest(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

        query = """SELECT service,request_id,operation,resource,payload_sha256,node,
            generation,broker_instance,state,provider_ack IS NULL,result IS NULL,
            extract(epoch from created_at)::text,extract(epoch from updated_at)::text
            FROM ha_effects.ledger WHERE state IN ('pending','inflight','uncertain')"""
        if renewal:
            query += " AND service='car-care-worker' AND operation IN ('car.telegram.poll','car.hyundai.snapshot')"
        rows = connection.execute(query + " ORDER BY service,request_id LIMIT 3").fetchall()
        if len(rows) != 2 or {row[2] for row in rows} != QUARANTINED_OPERATIONS:
            _deny('external_effect_controller_unresolved')
        fingerprints = {}
        resources = set()
        for row in rows:
            service, request, operation, resource = row[:4]
            prefix = 'telegram' if operation == 'car.telegram.poll' else 'hyundai'
            if (service != 'car-care-worker' or not re.fullmatch(prefix + r':[a-f0-9]{64}', resource)
                    or resource in resources or row[5:7] != ('A', 1) or row[8:11] != ('uncertain', True, True)
                    or any(type(pin) is not str or not re.fullmatch(r'[a-f0-9]{64}', pin)
                           for pin in (row[4], row[7]))):
                _deny('external_effect_controller_unresolved')
            resources.add(resource)
            barriers = connection.execute("""SELECT phase,generation,proof_id FROM ha_effects.barriers
                WHERE service=%s AND request_id=%s ORDER BY phase,proof_id LIMIT 3""", (service, request)).fetchall()
            if ([(phase, generation) for phase, generation, _ in barriers] != [('inflight', 1), ('pending', 1)]
                    or len({pin for _, _, pin in barriers}) != 2
                    or any(not re.fullmatch(r'[a-f0-9]{32}', pin) for _, _, pin in barriers)):
                _deny('external_effect_controller_unresolved')
            fingerprints[operation] = digest([row, barriers])
        credential = connection.execute("""SELECT revision,payload_sha256,
            (payload->'refresh_uncertain'='true'::jsonb) FROM ha_effects.private_state
            WHERE service='car-care-worker' AND key='hyundai_tokens'""").fetchone()
        if (not credential or type(credential[0]) is not int or credential[0] < 1
                or not re.fullmatch(r'[a-f0-9]{64}', credential[1]) or credential[2] is not True):
            _deny('initial_recovery_quarantine_unverified')
        return tuple(sorted(fingerprints.items())), digest(credential), digest(sorted(resources))

    def _effects_gate(self, primary, peer, watermark, started):
        if self.quarantine is None:
            return super()._effects_gate(primary, peer, watermark, started)
        self._assert_provider_isolation(started)
        try:
            replay = peer.execute('SELECT pg_last_wal_replay_lsn()::text').fetchone()
            if not replay or self.backend._lsn(replay[0]) < watermark:
                _deny('external_effect_controller_peer_unproven')
        except TransitionBlocked:
            raise
        except Exception:
            _deny('external_effect_controller_ledger_unavailable')
        self._verify_quarantine_pair(primary, peer, started)

    def _renewal_effects_gate(self, primary, peer, started):
        if self.quarantine is not None:
            self._verify_quarantine_pair(primary, peer, started, renewal=True)

    def _verify_quarantine_pair(self, primary, peer, started, *, renewal=False):
        expected = self.quarantine[0], self.quarantine[2], self.quarantine[3]
        try:
            if any(self._quarantine_observation(connection, renewal=renewal) != expected for connection in (primary, peer)):
                _deny('initial_recovery_quarantine_unverified')
        except TransitionBlocked:
            raise
        except Exception:
            _deny('external_effect_controller_ledger_unavailable')
        self._deadline(started)

    def observe_closed(self, state, *, challenge, deadline):
        """Read the closed pair after provider isolation is in place."""
        return self._observe_closed(state, challenge, deadline, before_isolation=False)

    def observe_quiescence_prerequisites(self, state, *, challenge, deadline):
        """Read only the closed pair and exact effect pins before isolating the provider."""
        return self._observe_closed(state, challenge, deadline, before_isolation=True)

    def _observe_closed(self, state, challenge, deadline, *, before_isolation):
        self._bind(state)
        if (type(challenge) is not str or not re.fullmatch(r'[a-f0-9]{64}', challenge)
                or type(deadline) not in (float, int) or not time.monotonic() < deadline):
            _deny('initial_lease_reconciliation_required')
        started = min(time.monotonic(), deadline - INITIAL_PROOF_SECONDS)
        self._deadline(started)
        with self.backend._connection('A') as primary, self.backend._connection('B') as peer:
            with _pg_phase('A', 'role_lock'):
                primary.execute('BEGIN READ ONLY')
                primary.execute('SELECT pg_advisory_xact_lock(%s)', (ROLE_LOCK,))
            current, replica, expires = self._observe_pair(primary, peer, state, opened=False, started=started)
            watermark = self.backend._lsn(current['flush'])
            replay = self.backend._lsn(replica['replay'])
            if replay < watermark:
                _deny('initial_replication_barrier_unconfirmed')
            if before_isolation and self.quarantine is not None:
                self._verify_quarantine_pair(primary, peer, started)
            else:
                self._effects_gate(primary, peer, watermark, started)
            self._deadline(started)
            if time.monotonic() >= deadline:
                _deny('initial_replication_deadline_exceeded')
            payload = {'challenge': challenge, 'controller_sha256': self.scope[0],
                       'purpose': 'quiescence_prerequisites' if before_isolation else 'closed_observation',
                       'attempt_id': self.scope[4][0], 'cluster': state['cluster'], 'system_id': self.scope[3],
                       'primary': 'A', 'standby': 'B', 'generation': 1, 'timeline': 1, 'revision': self._closed_revision,
                       'expires_at': expires.isoformat(), 'flush': watermark, 'replay': replay}
            return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def observe_published_open(self, state, *, challenge, lease_sha256, deadline):
        """Read the exact lease advertised by the recovery service on both peers."""
        self._bind(state)
        started = time.monotonic()
        if (not all(type(pin) is str and re.fullmatch(r'[a-f0-9]{64}', pin) for pin in (challenge, lease_sha256))
                or type(deadline) not in (float, int) or not started < deadline):
            _deny('initial_lease_reconciliation_required')
        budget_start = min(started, deadline - INITIAL_PROOF_SECONDS)
        prefix = self._probe_prefix()
        query = """SELECT array(SELECT nonce FROM ha.controller_probes
            WHERE generation=1 AND left(nonce,%s)=%s AND right(nonce,6)=':lease'
            ORDER BY nonce LIMIT 2)"""
        with self.backend._connection('A') as primary, self.backend._connection('B') as peer:
            for connection in (primary, peer):
                connection.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
            current, replica, expires = self._observe_pair(primary, peer, state, opened=True, started=budget_start)
            a_nonce = primary.execute(query, (len(prefix), prefix)).fetchone()
            b_nonce = peer.execute(query, (len(prefix), prefix)).fetchone()
            if (not a_nonce or a_nonce != b_nonce or type(a_nonce[0]) is not list or len(a_nonce[0]) != 1
                    or type(a_nonce[0][0]) is not str
                    or not re.fullmatch(re.escape(prefix) + r'[a-f0-9]{32}:lease', a_nonce[0][0])
                    or self._proof(state, expires, a_nonce[0][0]) != lease_sha256):
                _deny('initial_replication_barrier_unconfirmed')
            if self.quarantine is not None:
                self._verify_quarantine_pair(primary, peer, budget_start, renewal=True)
        self._legacy(state, budget_start)
        finished = time.monotonic()
        if not finished < min(deadline, started + current['remaining'], started + replica['remaining']):
            _deny('initial_replication_deadline_exceeded')
        payload = {'challenge': challenge, 'lease_sha256': lease_sha256,
                   'expires_at': expires.isoformat(), 'observed_on': ['A', 'B']}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def _recovery_binding(self, plan):
        value = plan.get('initial_cutover_recovery')
        if (getattr(self.backend.fence_adapter, '_installed_runtime', None) is not True
                or type(value) is not dict or set(value) != set(RECOVERY_FIELDS)
                or not all(type(value[key]) is str and re.fullmatch(r'[a-f0-9]{64}', value[key])
                           for key in RECOVERY_FIELDS)):
            _deny('initial_cutover_approval_required')
        return tuple(value[key] for key in RECOVERY_FIELDS)

    def _probe_prefix(self):
        return 'recovery-a:' + self.scope[4][0] + ':'


class InitialPgLeaseRearm(InitialPgLeaseRecovery):
    """Issue only revision six after a signed, sealed revision-four predecessor."""

    _approval_action = REARM_ACTION
    _closed_revision = 5
    _open_revision = 6
    _revoked_revision = 7
    _allow_rearm = True

    def _bind(self, state):
        previous = self.rearm
        super()._bind(state)
        selected = rearm_scope(self.backend.plan)
        if previous is not None and previous != selected:
            _deny('initial_rearm_scope_unverified')
        self.rearm = selected

    def _probe_prefix(self):
        return 'rearm-a:' + self.rearm['attempt_id'] + ':'


class InitialPgLeasePostboot(InitialPgLeaseRecovery):
    """One approved Windows reboot with retained guest boots: exactly 7 -> 8 -> 9."""
    from .initial_postboot import ACTION as _approval_action
    _closed_revision = 7
    _open_revision = 8
    _revoked_revision = 9
    _allow_postboot = True

    def _bind(self, state):
        from .initial_postboot import scope
        previous = self.postboot
        super()._bind(state)
        selected = scope(self.backend.plan)
        if previous is not None and previous != selected:
            _deny('initial_postboot_scope_unverified')
        self.postboot = selected

    def _probe_prefix(self):
        return 'postboot-a:' + self.postboot['attempt_id'] + ':'
