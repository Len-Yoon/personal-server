"""Open the first A lease without changing the restored A/B replication history."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime
import hashlib
import json
import re
import time
import uuid

from .pg import LEASE_SECONDS, PROOF_SECONDS, ROLE_LOCK, _deny


INITIAL_CUTOVER = {'schema_version': 1, 'primary_node': 'A', 'partner_node': 'B'}
INITIAL_REVISION = 1
OPEN_REVISION = 2
STAMP = """singleton=1 AND cluster=%s AND epoch=1 AND revision=%s
    AND primary_node='A' AND partner='B' AND timeline=1 AND serving=ARRAY['A']::text[]
    AND worker='A' AND mode='AS' AND writes_open=%s AND history_safe=%s AND expires_at=%s
    AND current_setting('cluster_name')='ha_A' AND NOT pg_is_in_recovery()
    AND (SELECT system_identifier::text FROM pg_control_system())=%s
    AND ('x'||substring(pg_walfile_name(pg_current_wal_insert_lsn()),1,8))::bit(32)::integer=1"""
PROBE_READ = 'SELECT generation FROM ha.controller_probes WHERE nonce=%s'


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
        self.owned_expiries = set()
        self.open_attempted = False
        self.scope = None

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
        if time.monotonic() - started > PROOF_SECONDS:
            _deny('initial_replication_deadline_exceeded')

    def _legacy(self, state, started, *, held_only=False):
        self._deadline(started)
        check = self.assert_exclusion_held if held_only else self.assert_legacy_closed
        observed = check(state, started + PROOF_SECONDS)
        if observed is not None and observed is not True:
            _deny('initial_legacy_closure_required')
        self._deadline(started)

    def _pair(self, primary, peer, state, *, opened):
        revision = OPEN_REVISION if opened else INITIAL_REVISION
        expected = {'cluster': state['cluster'], 'epoch': 1, 'revision': revision, 'primary': 'A', 'partner': 'B',
                    'timeline': 1, 'actual_timeline': 1, 'serving': ['A'], 'worker': 'A', 'mode': 'AS',
                    'system_id': str(self.backend.plan['system_id']), 'writes': opened, 'history': opened,
                    'fsync': True, 'full_page_writes': True}
        for value, node, standby in ((primary, 'A', 'B'), (peer, 'B', 'A')):
            fields = dict(expected, node='ha_' + node, recovery=node == 'B', sync_names='FIRST1(ha_' + standby.lower() + ')')
            if any(type(value.get(key)) is not type(item) or value[key] != item for key, item in fields.items()):
                _deny('initial_replication_identity_unverified')
            remaining = value['remaining']
            if (opened and not 0 < remaining <= LEASE_SECONDS) or (not opened and not remaining < 0):
                _deny('initial_lease_stamp_unverified')
        if primary['sync_peer'] != 'ha_b':
            _deny('initial_replication_identity_unverified')

    @staticmethod
    def _expiry(row):
        if not row or len(row) != 1 or not isinstance(row[0], datetime) or row[0].utcoffset() is None:
            _deny('initial_lease_stamp_unverified')
        return row[0]

    def _observe_pair(self, primary, peer, state, *, opened):
        def observe(connection, node):
            value = self.backend._observe(connection, node, state)
            expiry = self._expiry(connection.execute('SELECT expires_at FROM ha.control WHERE singleton=1').fetchone())
            return value, expiry

        # Keep the primary transaction and its role lock on the caller thread.
        # Joining the standby read here keeps its connection alive on failure.
        with ThreadPoolExecutor(max_workers=1) as pool:
            standby = pool.submit(observe, peer, 'B')
            current, expires = observe(primary, 'A')
            replica, replica_expires = standby.result()
        self._pair(current, replica, state, opened=opened)
        if expires != replica_expires:
            _deny('initial_lease_stamp_unverified')
        return current, replica, expires

    def _parameters(self, state, revision, opened, expires):
        return (state['cluster'], revision, opened, opened, expires, str(self.backend.plan['system_id']))

    @staticmethod
    def _begin(connection, *, shared):
        connection.execute('BEGIN')
        connection.execute('SET LOCAL synchronous_commit=remote_apply')
        lock = 'SELECT pg_advisory_xact_lock_shared(%s)' if shared else 'SELECT pg_advisory_xact_lock(%s)'
        connection.execute(lock, (ROLE_LOCK,))

    def _proof(self, state, expires, nonce):
        payload = {'cluster': state['cluster'], 'epoch': 1, 'revision': OPEN_REVISION, 'timeline': 1,
                   'primary': 'A', 'partner': 'B', 'serving': ['A'], 'worker': 'A', 'mode': 'AS',
                   'controller_sha256': self.scope[0], 'deployment_sha256': self.scope[1][1],
                   'system_id': self.scope[3], 'expires_at': expires.isoformat(), 'nonce': nonce}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def _lease(self, state, *, renewing):
        started = time.monotonic()
        nonce = 'initial-a:' + uuid.uuid4().hex
        barrier, lease_probe = nonce + ':fence', nonce + ':lease'
        with ExitStack() as connections:
            # Only read PostgreSQL until the independent closure proof succeeds.
            with ThreadPoolExecutor(max_workers=1) as pool:
                closure = pool.submit(self._legacy, state, started)
                primary = connections.enter_context(self.backend._connection('A'))
                peer = connections.enter_context(self.backend._connection('B'))
                self._begin(primary, shared=renewing)
                _, _, previous = self._observe_pair(primary, peer, state, opened=renewing)
                closure.result()
            if renewing:
                self.owned_expiries = {previous}
            self._deadline(started)
            primary.execute("DELETE FROM ha.controller_probes WHERE generation=1 AND left(nonce,10)='initial-a:'")
            primary.execute('INSERT INTO ha.controller_probes(nonce,generation) VALUES(%s,1)', (barrier,))
            self._deadline(started)
            self.backend._commit(primary)
            watermark = self.backend._lsn(primary.execute('SELECT pg_current_wal_flush_lsn()::text').fetchone()[0])

            self._begin(primary, shared=renewing)
            _, replica, expires = self._observe_pair(primary, peer, state, opened=renewing)
            if (expires != previous or peer.execute(PROBE_READ, (barrier,)).fetchone() != (1,)
                    or self.backend._lsn(replica['replay']) < watermark):
                _deny('initial_replication_barrier_unconfirmed')
            if not renewing:
                self.backend._effects_open_gate(primary, peer, watermark)
            self._legacy(state, started, held_only=True)
            change = '' if renewing else 'revision=2, writes_open=true, history_safe=true, '
            condition = 'expires_at>clock_timestamp()' if renewing else 'expires_at<clock_timestamp()'
            revision = OPEN_REVISION if renewing else INITIAL_REVISION
            row = primary.execute(
                'UPDATE ha.control SET ' + change + "expires_at=clock_timestamp()+interval '5 seconds' WHERE "
                + STAMP + ' AND ' + condition + ' RETURNING expires_at',
                self._parameters(state, revision, renewing, previous),
            ).fetchone()
            current_expiry = self._expiry(row)
            self.owned_expiries.add(current_expiry)
            primary.execute('INSERT INTO ha.controller_probes(nonce,generation) VALUES(%s,1)', (lease_probe,))
            self._deadline(started)
            self.backend._commit(primary)

            with ThreadPoolExecutor(max_workers=1) as pool:
                closure = pool.submit(self._legacy, state, started)
                current, replica, expires = self._observe_pair(primary, peer, state, opened=True)
                if (expires != current_expiry
                        or any(connection.execute(PROBE_READ, (lease_probe,)).fetchone() != (1,)
                               for connection in (primary, peer))
                        or min(self.backend._lsn(current['flush']), self.backend._lsn(replica['flush']),
                               self.backend._lsn(replica['replay'])) < watermark):
                    _deny('initial_replication_barrier_unconfirmed')
                closure.result()
            self._deadline(started)
            self.owned_expiries = {current_expiry}
            return self._proof(state, current_expiry, lease_probe)

    def _change(self, state, *, renewing):
        self._bind(state)
        if not renewing:
            if self.open_attempted:
                _deny('initial_lease_reconciliation_required')
            self.open_attempted = True
        try:
            return self._lease(state, renewing=renewing)
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
            _, _, expires = self._observe_pair(primary, peer, state, opened=True)
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
