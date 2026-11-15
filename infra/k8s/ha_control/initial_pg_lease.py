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
STAMP = """singleton=1 AND cluster=%s AND epoch=1 AND revision=%s
    AND primary_node='A' AND partner='B' AND timeline=1 AND serving=ARRAY['A']::text[]
    AND worker='A' AND mode='AS' AND writes_open=%s AND history_safe=%s AND expires_at=%s
    AND current_setting('cluster_name')='ha_A' AND NOT pg_is_in_recovery()
    AND (SELECT system_identifier::text FROM pg_control_system())=%s
    AND ('x'||substring(pg_walfile_name(pg_current_wal_insert_lsn()),1,8))::bit(32)::integer=1"""
PROBE_READ = 'SELECT generation FROM ha.controller_probes WHERE nonce=%s'


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

    def _bind(self, state):
        plan = self.backend.plan
        if not self.backend.depth or type(plan) is not dict:
            _deny('native_host_guard_required')
        mode = plan.get('initial_wsl_cutover')
        if (plan.get('_approval_action') != 'ha-initial-wsl-cutover' or plan.get('profile') != 'enforced-vm-v1'
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
        scope = (controller, deployment, plan['cluster'], str(plan['system_id']))
        if self.scope is not None and self.scope != scope:
            _deny('initial_cutover_state_unbound')
        self.scope = scope

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
        if observed is not None and observed is not True:
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
        revision = OPEN_REVISION if opened else INITIAL_REVISION
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
        payload = {'cluster': state['cluster'], 'epoch': 1, 'revision': OPEN_REVISION, 'timeline': 1,
                   'primary': 'A', 'partner': 'B', 'serving': ['A'], 'worker': 'A', 'mode': 'AS',
                   'controller_sha256': self.scope[0], 'deployment_sha256': self.scope[1][1],
                   'system_id': self.scope[3], 'expires_at': expires.isoformat(), 'nonce': nonce}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def _commit_primary(self, primary):
        with _pg_phase('A', 'commit'):
            self.backend._commit(primary)

    def _lease(self, state, *, renewing, started):
        nonce = 'initial-a:' + uuid.uuid4().hex
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
                self.owned_expiries = {previous}
                # The matching open stamp is already observed on both nodes.
                # Keep the shared role lock through the next remote_apply
                # commit, which carries the new stamp and its own nonce.
                watermark = self.backend._lsn(current['flush'])
            else:
                self._deadline(started)
                with _pg_phase('A', 'fence_probe'):
                    primary.execute("DELETE FROM ha.controller_probes WHERE generation=1 AND left(nonce,10)='initial-a:'")
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
                self.backend._effects_open_gate(primary, peer, watermark)
                # No app writes are authorized during preparation. Start the
                # authority window under the same exclusive role lock only
                # after the closed catalog and replication checks succeeded.
                self._deadline(started)
                started = self.proof_started = time.monotonic()
                self._legacy(state, started, phase='precommit')
            self._legacy(state, started, held_only=True)
            change = '' if renewing else 'revision=2, writes_open=true, history_safe=true, '
            condition = 'expires_at>clock_timestamp()' if renewing else 'expires_at<clock_timestamp()'
            revision = OPEN_REVISION if renewing else INITIAL_REVISION
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
                    primary.execute("DELETE FROM ha.controller_probes WHERE generation=1 AND left(nonce,10)='initial-a:'")
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
        """Explicitly open only the pristine restored revision 1."""
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
                    'UPDATE ha.control SET revision=3,writes_open=false,history_safe=false,'
                    "expires_at=clock_timestamp()-interval '1 second' WHERE " + STAMP,
                    self._parameters(state, OPEN_REVISION, True, expires),
                )
                if result.rowcount == 1:
                    self.backend._commit(connection)
                    self.owned_expiries.clear()
                    return True
            connection.execute('ROLLBACK')
        return False
