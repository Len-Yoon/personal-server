"""Exercise the native lease algorithm on an explicitly disposable A/B pair."""

from contextlib import contextmanager
from datetime import datetime, timezone
import re
import time

from .controller import TransitionBlocked
from .initial_pg_lease import InitialPgLease
from .pg import LEASE_SECONDS, PgBackend
from .scm_pg_auth_probe import service_identity


class DisposableLeaseBackend(PgBackend):
    @contextmanager
    def _connection(self, node):
        import psycopg

        endpoint = self.plan['pg_nodes'][node]
        if (endpoint.get('dbname') != 'ha_lease_rehearsal' or endpoint.get('port') != 25432
                or endpoint.get('sslmode') != 'verify-full'):
            raise TransitionBlocked('isolated_lease_scope_invalid')
        try:
            connection = psycopg.connect(**endpoint, autocommit=True, connect_timeout=2,
                                         gssencmode='disable', require_auth='scram-sha-256',
                                         application_name='ha_isolated_lease')
        except psycopg.Error:
            raise TransitionBlocked('isolated_lease_pg_connect_' + node.lower()) from None
        with connection:
            connection.execute("SET statement_timeout='700ms'")
            connection.execute("SET lock_timeout='500ms'")
            if connection.execute('SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()').fetchone() != (True,):
                raise TransitionBlocked('isolated_lease_tls_invalid')
            identity = connection.execute(
                "SELECT current_database(), current_setting('cluster_name'), "
                "pg_is_in_recovery(), system_identifier::text FROM pg_control_system()"
            ).fetchone()
            if identity != ('ha_lease_rehearsal', 'ha_' + node, node == 'B', str(self.plan['system_id'])):
                raise TransitionBlocked('isolated_lease_identity_invalid')
            try:
                yield connection
            except psycopg.Error as error:
                state = error.sqlstate
                if not state or not re.fullmatch('[A-Z0-9]{5}', state):
                    state = 'unknown'
                raise TransitionBlocked('isolated_lease_pg_' + node.lower() + '_' + state) from None


def run(backend, *, state, service_sid, retained_system_ids, observe, exclusion,
        previous_token_status, token_expires_at, assert_single_writer):
    """Run under SCM with isolated endpoints and observable real token expiry.

    The caller owns the disposable fixture and supplies the authenticated
    Windows/WSL observation. The token status callback returns HTTP status
    only. Secrets must stay in the fixture observer.
    """
    plan = backend.plan
    profile = plan.get('windows_enforced', {}).get('config_profile', {})
    system_id = str(plan.get('system_id'))
    endpoints = plan.get('pg_nodes', {})
    if (type(backend) is not DisposableLeaseBackend
            or service_identity() != service_sid
            or plan.get('windows_enforced', {}).get('service_sid') != service_sid
            or profile.get('acceptance_scope') != 'isolated-predeployment'
            or profile.get('operational_cutover_accepted') is not False
            or not re.fullmatch(r'ha_lease_rehearsal_[0-9a-f]{16}', plan.get('cluster', ''))
            or not re.fullmatch(r'[1-9][0-9]{0,19}', system_id)
            or not retained_system_ids or system_id in {str(item) for item in retained_system_ids}
            or set(endpoints) != {'A', 'B'}
            or any(endpoint.get('dbname') != 'ha_lease_rehearsal' or endpoint.get('port') != 25432
                   or endpoint.get('sslmode') != 'verify-full' for endpoint in endpoints.values())):
        raise TransitionBlocked('isolated_lease_scope_invalid')

    remaining = (token_expires_at - datetime.now(timezone.utc)).total_seconds()
    if not 2 < remaining < 660 or previous_token_status() != 200:
        raise TransitionBlocked('isolated_lease_token_window_invalid')

    lease = InitialPgLease(backend, observe, assert_exclusion_held=exclusion)
    stop = time.monotonic() + remaining + 100
    maximum_proof = maximum_gap = 0
    previous_expiry = None
    rejected = False
    rounds = 0
    try:
        while time.monotonic() < stop:
            started = time.monotonic()
            (lease.open if rounds == 0 else lease.renew)(state)
            maximum_proof = max(maximum_proof, time.monotonic() - started)
            expiry, = lease.owned_expiries
            if previous_expiry is not None:
                gap = (expiry - previous_expiry).total_seconds()
                if not 0 < gap < LEASE_SECONDS:
                    raise TransitionBlocked('isolated_lease_continuity_invalid')
                with backend._connection('A') as connection:
                    observed = connection.execute(
                        'SELECT expires_at=%s AND clock_timestamp()<%s AND writes_open AND history_safe '
                        'FROM ha.control WHERE singleton=1', (expiry, previous_expiry),
                    ).fetchone()
                if observed != (True,):
                    raise TransitionBlocked('isolated_lease_continuity_invalid')
                maximum_gap = max(maximum_gap, gap)
            previous_expiry = expiry
            rounds += 1
            if rounds == 1 or rejected:
                if assert_single_writer(state, expiry, writes_open=True) is not True:
                    raise TransitionBlocked('isolated_lease_single_writer_invalid')
            if rejected:
                break
            if datetime.now(timezone.utc) >= token_expires_at:
                status = previous_token_status()
                if status not in (200, 401):
                    raise TransitionBlocked('isolated_lease_token_status_invalid')
                rejected = status == 401
            time.sleep(max(0, 1 - (time.monotonic() - started)))
        else:
            raise TransitionBlocked('isolated_lease_expiry_unconfirmed')
    finally:
        if lease.owned_expiries and not lease.revoke(state):
            raise TransitionBlocked('isolated_lease_revoke_unconfirmed')
    if assert_single_writer(state, None, writes_open=False) is not True:
        raise TransitionBlocked('isolated_lease_revoke_unconfirmed')
    return {'renewals': rounds - 1, 'old_token_rejected': True,
            'max_proof_ms': round(maximum_proof * 1000),
            'max_lease_gap_ms': round(maximum_gap * 1000)}
