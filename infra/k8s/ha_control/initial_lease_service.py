"""Durable Windows service lifecycle for the first A write lease."""

from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from .controller import Journal, TransitionBlocked, exclusive
from .initial_pg_lease import (
    INITIAL_CUTOVER, INITIAL_PROOF_SECONDS, RECOVERY_ACTION, RECOVERY_FIELDS, RECOVERY_OPEN_REVISION,
    REARM_ACTION, InitialPgLeaseRearm, rearm_scope,
    InitialPgLease, InitialPgLeaseRecovery, LegacyResponseTimeout,
)
from .enforced_windows import _powershell_path
from .initial_windows_gate import initial_service_identity
from .pg import PgUnavailable
from .windows_lease_pipe import CLIENT_FAILURES, PersistentLeaseProofClient, ProofPins, RecoveryProofPins


SHA = re.compile(r'[a-f0-9]{64}\Z')
HANDOFF_KEYS = {'attempt_id', 'legacy_plan_sha256', 'restore_receipt_sha256', 'activation_intent_sha256'}
OBSERVER_KEYS = {'pipe_name', 'server_pid', 'key_sha256', 'windows_boot_sha256',
                 'wsl_boot_sha256', 'collector_sha256', 'client_sha256', 'marker_sha256'}
RECOVERY_OBSERVER_KEYS = {'guest_boot_sha256', 'hold_sha256', 'broker_scope_sha256', 'resource_set_sha256',
                          'held_admission', 'provider_isolation'}
MARKER = Path(r'C:\ProgramData\N100HA-Cutover\ha-controller-owner.json')
CLOSURE_FAILURE_CODES = frozenset({
    'legacy_pipe_client_unavailable', 'legacy_pipe_request_rejected',
    'legacy_pipe_response_timeout', 'legacy_pipe_response_missing', 'legacy_pipe_response_invalid',
}) | frozenset(CLIENT_FAILURES.values())
LEASE_FAILURE_CODES = frozenset({
    'authenticated_pg_observation_required', 'authenticated_pg_unavailable',
    'controller_journal_invalid', 'controller_journal_not_durable',
    'external_effect_controller_binding_mismatch', 'external_effect_controller_catalog_unverified',
    'external_effect_controller_ledger_unavailable', 'external_effect_controller_peer_unproven',
    'external_effect_controller_unresolved', 'generation_role_enforcement_unverified',
    'generation_role_privilege_closure_unverified', 'fresh_replication_proof_required',
    'initial_cutover_approval_required', 'initial_cutover_deployment_binding_required',
    'initial_cutover_state_unbound', 'initial_lease_reconciliation_required',
    'initial_lease_stamp_unverified', 'initial_legacy_closure_required',
    'initial_replication_barrier_unconfirmed', 'initial_replication_deadline_exceeded',
    'initial_replication_identity_unverified', 'initial_service_handoff_unverified',
    'initial_service_live_closure_unverified', 'initial_service_proof_deadline_exceeded',
    'initial_recovery_admission_unverified',
    'initial_recovery_quarantine_unverified',
    'native_host_guard_required', 'pg_identity_mismatch', 'server_enforcement_unverified',
}) | CLOSURE_FAILURE_CODES


def _pin(value):
    if type(value) is not str or not SHA.fullmatch(value):
        raise TransitionBlocked('initial_service_handoff_unverified')
    return value


def _handoff(backend, *, action='ha-initial-wsl-cutover'):
    plan = backend.plan
    approved = plan.get('initial_cutover_handoff')
    if (type(approved) is not dict or set(approved) != HANDOFF_KEYS
            or plan.get('initial_wsl_cutover') != INITIAL_CUTOVER
            or plan.get('_approval_action') != action
            or backend.fence_adapter._installed_runtime is not True):
        raise TransitionBlocked('initial_service_handoff_unverified')
    handoff = {key: _pin(value) for key, value in approved.items()}
    _, deployment = backend._effects_deployment_binding()
    if handoff['legacy_plan_sha256'] != deployment:
        raise TransitionBlocked('initial_service_handoff_unverified')
    return handoff | {'controller_plan_sha256': _pin(backend.fence_adapter.plan_sha256)}


def _read_pinned(path, digest):
    def unique(pairs):
        data = dict(pairs)
        if len(data) != len(pairs):
            raise ValueError()
        return data

    def identity(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
                info.st_mtime_ns, getattr(info, 'st_file_attributes', 0))

    def stable(before, after):
        return identity(before) == identity(after) and before.st_ctime_ns == after.st_ctime_ns

    try:
        if not path.is_absolute():
            raise ValueError()
        named_before = path.lstat()
        if (not stat.S_ISREG(named_before.st_mode) or named_before.st_nlink != 1 or not named_before.st_ino
                or getattr(named_before, 'st_file_attributes', 0) & 1024
                or any(p.is_symlink() or getattr(p.lstat(), 'st_file_attributes', 0) & 1024
                       for p in path.parents)):
            raise ValueError()
        with path.open('rb') as stream:
            before = os.fstat(stream.fileno())
            raw = stream.read(4097)
            after = os.fstat(stream.fileno())
            named_after = path.lstat()
        # Windows can report different ctime values for the same file through
        # a handle and a path. Require ctime stability within each source.
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not before.st_ino
                or not 0 < len(raw) == before.st_size <= 4096
                or not stable(before, after) or not stable(named_before, named_after)
                or identity(after) != identity(named_after)
                or hashlib.sha256(raw).hexdigest() != digest):
            raise ValueError()
        return json.loads(raw, object_pairs_hook=unique), (before.st_dev, before.st_ino)
    except (OSError, ValueError, TypeError):
        raise TransitionBlocked('initial_service_handoff_unverified') from None


class WindowsInitialObserver:
    def __init__(self, client, intent_path, marker_sha256, cluster, *, broker_marker=False):
        self.client = client
        self.intent_path = intent_path
        self.marker_sha256 = marker_sha256
        self.cluster = cluster
        self.marker_identity = None
        self.broker_marker = broker_marker

    def _marker(self, handoff, deadline):
        if self.broker_marker:
            pins = self.client.pins
            if pins.attempt_id != handoff['attempt_id'] or pins.plan_sha256 != handoff['legacy_plan_sha256']:
                raise TransitionBlocked('initial_service_handoff_unverified')
            try:
                _pin(self.client.request_marker(deadline, self.marker_sha256))
            except (ValueError, OSError, TransitionBlocked):
                raise TransitionBlocked('initial_service_handoff_unverified') from None
            if time.monotonic() >= deadline:
                raise TransitionBlocked('initial_service_handoff_unverified')
            return
        value, identity = _read_pinned(MARKER, self.marker_sha256)
        if (type(value) is not dict or set(value) != {'cluster', 'generation', 'source_commit', 'plan_sha256', 'attempt_id'}
                or value['cluster'] != self.cluster or type(value['generation']) is not int or value['generation'] != 1
                or value['plan_sha256'] != handoff['legacy_plan_sha256'] or value['attempt_id'] != handoff['attempt_id']
                or type(value['source_commit']) is not str or not re.fullmatch(r'[a-f0-9]{40}', value['source_commit'])
                or self.marker_identity not in (None, identity)):
            raise TransitionBlocked('initial_service_handoff_unverified')
        self.marker_identity = identity

    def assert_intent(self, handoff):
        actual, _ = _read_pinned(self.intent_path, handoff['activation_intent_sha256'])
        expected = {key: handoff[key] for key in ('attempt_id', 'legacy_plan_sha256', 'restore_receipt_sha256')}
        expected.update(schema_version=1, phase='activate_vm_intent')
        if type(actual) is not dict:
            raise TransitionBlocked('initial_service_handoff_unverified')
        expected.update({key: _pin(actual.get(key)) for key in ('fence_sha256', 'activation_check_sha256', 'settlement_sha256')})
        if type(actual) is not dict or actual != expected or type(actual.get('schema_version')) is not int:
            raise TransitionBlocked('initial_service_handoff_unverified')
        self._marker(handoff, time.monotonic() + INITIAL_PROOF_SECONDS)

    def assert_legacy_closed(self, handoff, deadline):
        try:
            _pin(self.client.request(deadline))
            return True
        except ValueError as error:
            code = str(error)
            raise TransitionBlocked(
                code if code in CLOSURE_FAILURE_CODES else 'initial_service_live_closure_unverified'
            ) from None

    def assert_exclusion_held(self, handoff, deadline):
        # Keep the same protected admission marker pinned across the commit.
        # A renewal may reuse only its own still-fresh full writer observation.
        self._marker(handoff, deadline)
        return time.monotonic() < deadline


@contextmanager
def native_initial_observer(backend, *, action='ha-initial-wsl-cutover'):
    handoff = _handoff(backend, action=action)
    plan = backend.plan
    params = plan.get('windows_initial_observer')
    if type(params) is not dict or set(params) != OBSERVER_KEYS:
        raise TransitionBlocked('initial_service_handoff_unverified')
    pins = ProofPins(handoff['legacy_plan_sha256'], handoff['attempt_id'], 1,
                     _pin(params['windows_boot_sha256']), plan['windows_enforced']['service_sid'],
                     _pin(params['wsl_boot_sha256']), _pin(params['collector_sha256']))
    process_fields = ('pipe_name', 'server_pid')
    try:
        client = PersistentLeaseProofClient(pins=pins, **{key: params[key] for key in process_fields},
                                           powershell=_powershell_path(),
                                           client=Path(__file__).with_name('windows-ha-lease-proof-client.ps1'),
                                           key_path=backend.plan_path.with_name('initial-cutover-proof.key'),
                                           key_sha256=_pin(params['key_sha256']),
                                           compiler_root=backend.plan_path.parent / 'journal',
                                           client_sha256=_pin(params['client_sha256']))
    except (ValueError, OSError, TypeError):
        raise TransitionBlocked('initial_service_live_closure_unverified') from None
    with client:
        yield WindowsInitialObserver(client, backend.plan_path.with_name('initial-cutover-activation.json'),
                                     _pin(params['marker_sha256']), plan['cluster'],
                                     broker_marker=(action in {RECOVERY_ACTION, REARM_ACTION}))


def _request_recovery_proof(client, deadline, failure):
    try:
        _pin(client.request(deadline))
    except (ValueError, OSError, TransitionBlocked):
        raise TransitionBlocked(failure) from None
    if time.monotonic() >= deadline:
        raise TransitionBlocked(failure)


class _HeldRecoveryAdmission:
    """Observe the durable guest hold without authorizing its removal."""
    def __init__(self, client):
        self.client = client
        self.deadline = 0

    def assert_held(self, deadline):
        self.deadline = 0
        _request_recovery_proof(self.client, deadline, 'initial_recovery_admission_unverified')
        self.deadline = deadline
        return True

    def publish(self):
        # The journal carries the fresh first heartbeat. Guest release has a
        # separate readback of that publication and the replicated DB stamp.
        if time.monotonic() >= self.deadline:
            raise TransitionBlocked('initial_recovery_admission_unverified')
        self.deadline = 0


class WindowsRecoveryObserver:
    def __init__(self, legacy, held, provider, handoff, recovery):
        self.legacy, self.held, self.provider = legacy, held, provider
        self.handoff, self.recovery = handoff.copy(), recovery.copy()

    def assert_intent(self, handoff):
        return self.legacy.assert_intent(handoff)

    def assert_legacy_closed(self, handoff, deadline):
        return self.legacy.assert_legacy_closed(handoff, deadline)

    def assert_exclusion_held(self, handoff, deadline):
        return self.legacy.assert_exclusion_held(handoff, deadline)

    @contextmanager
    def recovery_admission(self, handoff, recovery):
        if handoff != self.handoff or recovery != self.recovery:
            raise TransitionBlocked('initial_recovery_admission_unverified')
        yield _HeldRecoveryAdmission(self.held)

    def assert_quarantine_held(self, handoff, recovery, broker, resources, deadline):
        failure = 'initial_recovery_quarantine_unverified'
        if (handoff != self.handoff or recovery != self.recovery or self.provider is None
                or broker != self.provider.pins.broker_scope_sha256
                or resources != self.provider.pins.resource_set_sha256):
            raise TransitionBlocked(failure)
        _request_recovery_proof(self.provider, deadline, failure)
        return True


@contextmanager
def native_recovery_observer(backend):
    """Use distinct authenticated pipes for admission and provider isolation."""
    failure = 'initial_recovery_native_observer_required'
    rearming = 'initial_recovery_rearm' in backend.plan
    handoff = _handoff(backend, action=REARM_ACTION if rearming else RECOVERY_ACTION)
    if rearming:
        params = backend.plan.get('windows_initial_observer', {})
        expected = hashlib.sha256(Path(__file__).with_name('windows-ha-wsl-lease-observer.ps1').read_bytes()).hexdigest()
        if params.get('collector_sha256') != expected or backend.fence_adapter._rearm_verified != rearm_scope(backend.plan):
            raise TransitionBlocked('initial_rearm_offline_observer_required')
    recovery = _recovery_predecessor(backend, handoff)
    plan = backend.plan
    params = plan.get('windows_recovery_observers')
    legacy_params = plan.get('windows_initial_observer')
    if (type(params) is not dict or set(params) != RECOVERY_OBSERVER_KEYS
            or type(legacy_params) is not dict or set(legacy_params) != OBSERVER_KEYS):
        raise TransitionBlocked(failure)
    quarantine = InitialPgLeaseRecovery.quarantine_binding(plan)
    if (quarantine is None and params['provider_isolation'] is not None
            or quarantine is not None and (quarantine[1], quarantine[3]) !=
                (params['broker_scope_sha256'], params['resource_set_sha256'])):
        raise TransitionBlocked(failure)
    purposes = ('held_admission', 'provider_isolation') if quarantine is not None else ('held_admission',)
    connections = {}
    for purpose in purposes:
        endpoint = params[purpose]
        if type(endpoint) is not dict or set(endpoint) != {'pipe_name', 'server_pid', 'key_sha256'}:
            raise TransitionBlocked(failure)
        # The stable deployment digest predates the observing processes. The
        # final SCM plan pins their PIDs/keys separately under its approval.
        pins = RecoveryProofPins(handoff['legacy_plan_sha256'], recovery['attempt_id'],
                                 _pin(legacy_params['windows_boot_sha256']), _pin(params['guest_boot_sha256']),
                                 recovery['predecessor_service_sha256'], recovery['predecessor_terminal_sha256'],
                                 _pin(params['hold_sha256']),
                                 _pin(params['broker_scope_sha256']), _pin(params['resource_set_sha256']),
                                 plan['windows_enforced']['service_sid'], purpose)
        connections[purpose] = (pins, endpoint.copy())
    endpoints = [legacy_params, *(item[1] for item in connections.values())]
    for field in ('pipe_name', 'server_pid', 'key_sha256'):
        values = [endpoint[field] for endpoint in endpoints]
        if any(type(value) is not (int if field == 'server_pid' else str) for value in values) or len(set(values)) != len(values):
            raise TransitionBlocked(failure)
    with ExitStack() as stack:
        legacy = stack.enter_context(native_initial_observer(backend, action=REARM_ACTION if rearming else RECOVERY_ACTION))
        clients = {}
        for purpose, (pins, endpoint) in connections.items():
            try:
                client = PersistentLeaseProofClient(
                    pins=pins, **endpoint, powershell=_powershell_path(),
                    client=Path(__file__).with_name('windows-ha-lease-proof-client.ps1'),
                    key_path=backend.plan_path.with_name(f'initial-recovery-{purpose}.key'),
                    compiler_root=backend.plan_path.parent / 'journal',
                    client_sha256=_pin(legacy_params['client_sha256']),
                )
            except (ValueError, OSError, TypeError):
                raise TransitionBlocked(failure) from None
            clients[purpose] = stack.enter_context(client)
        yield WindowsRecoveryObserver(legacy, clients['held_admission'], clients.get('provider_isolation'),
                                      handoff, recovery)


@contextmanager
def native_transition_observer(backend):
    plan = backend.plan
    params = plan["windows_transition_observer"]
    fields = (OBSERVER_KEYS - {"marker_sha256"}) | {"legacy_plan_sha256", "legacy_attempt_id", "callback_config_sha256"}
    failure = "transition_legacy_closure_unverified"
    if (type(params) is not dict or set(params) != fields or plan.get("profile") != "enforced-vm-v1"
            or plan["generation"] < 2 or not all(
                type(params[key]) is str and SHA.fullmatch(params[key])
                for key in fields - {"pipe_name", "server_pid"}
            )):
        raise TransitionBlocked(failure)
    pins = ProofPins(params["legacy_plan_sha256"], params["legacy_attempt_id"], 1, params["windows_boot_sha256"],
                     plan["windows_enforced"]["service_sid"], params["wsl_boot_sha256"], params["collector_sha256"])
    pin = params["callback_config_sha256"]
    try:
        client = PersistentLeaseProofClient(
            pins=pins, pipe_name=params["pipe_name"], server_pid=params["server_pid"],
            powershell=_powershell_path(), client=Path(__file__).with_name("windows-ha-lease-proof-client.ps1"),
            key_path=backend.plan_path.with_name(f"transition-observer-key-{pin}.key"),
            key_sha256=params["key_sha256"], compiler_root=backend.plan_path.parent / "journal",
            client_sha256=params["client_sha256"],
        )
    except (ValueError, OSError, TypeError):
        raise TransitionBlocked(failure) from None
    with client:
        yield client.request


def serve_initial(backend, journal_path, observer):
    """Never resume a started attempt. A heartbeat is evidence, not a permit.

    The supplied native observer must read the current protected owner intent,
    authenticated Windows/WSL writer closure, and exact runtime under the same
    plan and attempt. The coordinator independently checks PostgreSQL and the
    exact service process when consuming a heartbeat.
    """
    handoff = _handoff(backend)
    return _serve(backend, journal_path, observer, handoff)


def _recovery_predecessor(backend, handoff):
    recovery = backend.plan.get('initial_cutover_recovery')
    error = 'initial_recovery_predecessor_unverified'
    if type(recovery) is not dict or set(recovery) != set(RECOVERY_FIELDS):
        raise TransitionBlocked(error)
    recovery = {key: _pin(recovery[key]) for key in RECOVERY_FIELDS}
    original_name, _ = initial_service_identity(backend.plan.get('installation_id'))
    recovery_name, recovery_sid = initial_service_identity(recovery['attempt_id'])
    windows = backend.plan.get('windows_enforced')
    if (original_name == recovery_name or type(windows) is not dict
            or windows.get('service_name') != recovery_name or windows.get('service_sid') != recovery_sid):
        raise TransitionBlocked(error)
    root = backend.plan_path.parent
    previous, _ = _read_pinned(root / 'initial-predecessor-state.json', recovery['predecessor_terminal_sha256'])
    service, _ = _read_pinned(root / 'initial-predecessor-service.json', recovery['predecessor_service_sha256'])
    old_scope = {key: handoff[key] for key in HANDOFF_KEYS}
    terminal_fields = set(HANDOFF_KEYS) | {'controller_plan_sha256', 'schema_version', 'kind',
                                           'phase', 'generation', 'sequence'}
    service_fields = {'attempt_id', 'controller_plan_sha256', 'first_write_proof_sha256', 'legacy_plan_sha256',
                      'service_created_ticks', 'service_image_path_sha256', 'service_name', 'service_pid', 'state_path'}
    if (type(previous) is not dict or type(service) is not dict
            or set(previous) not in (terminal_fields, terminal_fields | {'failure_code'})
            or set(service) != service_fields
            or type(previous.get('schema_version')) is not int or previous['schema_version'] != 1
            or previous.get('kind') != 'initial-wsl-cutover'
            or previous.get('phase') not in ('lease_lost', 'local_revoke_observed')
            or type(previous.get('generation')) is not int or previous['generation'] != 1
            or type(previous.get('sequence')) is not int or previous['sequence'] < 1
            or any(previous.get(key) != value for key, value in old_scope.items())
            or any(service.get(key) != previous.get(key)
                   for key in ('attempt_id', 'legacy_plan_sha256', 'controller_plan_sha256'))
            or service.get('service_name') != original_name
            or type(service.get('service_pid')) is not int or service['service_pid'] < 1
            or type(service['service_created_ticks']) is not str
            or not re.fullmatch(r'[1-9][0-9]{0,18}', service['service_created_ticks'])
            or type(service['state_path']) is not str
            or not re.fullmatch(r'[A-Za-z]:\\(?:[A-Za-z0-9_. -]+\\)+journal\\controller-state\.json', service['state_path'])
            or any(type(service[key]) is not str or not SHA.fullmatch(service[key])
                   for key in ('first_write_proof_sha256', 'service_image_path_sha256'))
            or recovery['attempt_id'] == handoff['attempt_id']
            or _pin(previous.get('controller_plan_sha256')) == handoff['controller_plan_sha256']):
        raise TransitionBlocked(error)
    return recovery


def serve_initial_recovery(backend, journal_path, observer):
    """Serve under native admission, which remains closed durably until published.

    The admission context must leave application activation disabled on any
    exit before publish, including process termination or uncertain revoke.
    """
    action = REARM_ACTION if 'initial_recovery_rearm' in backend.plan else RECOVERY_ACTION
    return _serve(backend, journal_path, observer, _handoff(backend, action=action), recovering=True)


def _serve(backend, journal_path, observer, handoff, *, recovering=False):
    journal = Journal(journal_path)
    state = {'cluster': backend.plan['cluster'], 'generation': 1, 'timeline': 1, 'target': 'A', 'old': 'B'}
    if not recovering and 'initial_effect_quarantine' in backend.plan:
        raise TransitionBlocked('initial_recovery_quarantine_unverified')
    if recovering:
        expected = backend.plan_path.parent / 'journal/controller-state.json'
        if journal.path != expected or not expected.is_absolute() or expected.parent.is_symlink():
            raise TransitionBlocked('initial_recovery_journal_unverified')
    with exclusive(Path(journal_path).with_suffix('.lock')), ExitStack() as admission:
        if os.path.lexists(journal.path):
            raise TransitionBlocked('initial_service_reconciliation_required')
        observer.assert_intent(handoff)
        stable = {'schema_version': 1, 'kind': 'initial-wsl-cutover', 'generation': 1, **handoff}
        rearm = rearm_scope(backend.plan) if 'initial_recovery_rearm' in backend.plan else None
        if rearm is not None and (not recovering or backend.fence_adapter._rearm_verified != rearm):
            raise TransitionBlocked('initial_rearm_scope_unverified')
        recovery = _recovery_predecessor(backend, handoff) if recovering else None
        lease_options = {}
        if recovery is not None:
            if not callable(getattr(observer, 'recovery_admission', None)):
                raise TransitionBlocked('initial_recovery_admission_unverified')
            if 'initial_effect_quarantine' in backend.plan:
                InitialPgLeaseRecovery.quarantine_binding(backend.plan)
                verify_quarantine = getattr(observer, 'assert_quarantine_held', None)
                if not callable(verify_quarantine):
                    raise TransitionBlocked('initial_recovery_quarantine_unverified')
                lease_options['assert_quarantine_held'] = lambda pin, resources, deadline: verify_quarantine(
                    handoff, recovery, pin, resources, deadline)
            admission_handle = admission.enter_context(observer.recovery_admission(handoff, recovery))
            stable.update(kind='initial-wsl-cutover-recovery', recovery=recovery, revision=RECOVERY_OPEN_REVISION)
            if rearm is not None:
                stable.update(kind='initial-wsl-cutover-rearm', rearm=rearm, revision=6)

        first_publication = False

        def assert_admission(deadline):
            if recovery is not None and not first_publication:
                if admission_handle.assert_held(deadline) is not True:
                    raise TransitionBlocked('initial_recovery_admission_unverified')

        def closure(_, deadline):
            assert_admission(deadline)
            return observer.assert_legacy_closed(handoff, deadline)

        def exclusion(_, deadline):
            assert_admission(deadline)
            return observer.assert_exclusion_held(handoff, deadline)

        journal.save(stable | {'phase': 'opening', 'sequence': 0})
        lease_type = InitialPgLeaseRearm if rearm is not None else (InitialPgLeaseRecovery if recovering else InitialPgLease)
        lease = lease_type(backend, closure, assert_exclusion_held=exclusion, **lease_options)
        sequence = 0
        failure_code = None
        try:
            proof = lease.open(state)
            first_proof = _pin(proof)
            while True:
                started = lease.proof_started
                elapsed = time.monotonic() - started
                if elapsed >= INITIAL_PROOF_SECONDS:
                    raise TransitionBlocked('initial_service_proof_deadline_exceeded')
                sequence += 1
                journal.save(stable | {'phase': 'serving', 'sequence': sequence, 'lease_proof_sha256': _pin(proof),
                                       'first_write_proof_sha256': first_proof, 'proof_elapsed_ms': round(elapsed * 1000, 3)},
                             windows_retry_until=min(started + INITIAL_PROOF_SECONDS, time.monotonic() + 0.15))
                if recovery is not None and not first_publication:
                    admission_handle.publish()
                    admission.close()
                first_publication = True
                time.sleep(max(0, 1 - (time.monotonic() - started)))
                proof = lease.renew(state)
        except Exception as error:
            reason = str(error) if isinstance(error, TransitionBlocked) else None
            if isinstance(error, (PgUnavailable, LegacyResponseTimeout)):
                failure_code = error.failure_code
            else:
                failure_code = reason if reason in LEASE_FAILURE_CODES else 'initial_service_lease_unclassified'
            raise
        finally:
            revoked = False
            try:
                revoked = lease.revoke(state) is True
            except Exception:
                # No successful publication: admission stays durably closed
                # even if the database reply was lost and revoke is uncertain.
                pass
            terminal = stable | {'phase': 'local_revoke_observed' if revoked else 'lease_lost',
                                 'sequence': sequence}
            if failure_code is not None:
                terminal['failure_code'] = failure_code
            journal.save(terminal, windows_retry_until=time.monotonic() + 0.15)
