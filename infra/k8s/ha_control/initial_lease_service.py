"""Durable Windows service lifecycle for the first A write lease."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from .controller import Journal, TransitionBlocked, exclusive
from .initial_pg_lease import INITIAL_CUTOVER, PROOF_SECONDS, InitialPgLease
from .enforced_windows import _powershell_path
from .windows_lease_pipe import PersistentLeaseProofClient, ProofPins


SHA = re.compile(r'[a-f0-9]{64}\Z')
HANDOFF_KEYS = {'attempt_id', 'legacy_plan_sha256', 'restore_receipt_sha256', 'activation_intent_sha256'}
OBSERVER_KEYS = {'pipe_name', 'server_pid', 'key_sha256', 'windows_boot_sha256',
                 'wsl_boot_sha256', 'collector_sha256', 'client_sha256', 'marker_sha256'}
MARKER = Path(r'C:\ProgramData\N100HA-Cutover\ha-controller-owner.json')


def _pin(value):
    if type(value) is not str or not SHA.fullmatch(value):
        raise TransitionBlocked('initial_service_handoff_unverified')
    return value


def _handoff(backend):
    plan = backend.plan
    approved = plan.get('initial_cutover_handoff')
    if (type(approved) is not dict or set(approved) != HANDOFF_KEYS
            or plan.get('initial_wsl_cutover') != INITIAL_CUTOVER
            or plan.get('_approval_action') != 'ha-initial-wsl-cutover'
            or backend.fence_adapter._installed_runtime is not True):
        raise TransitionBlocked('initial_service_handoff_unverified')
    handoff = {key: _pin(value) for key, value in approved.items()}
    _, deployment = backend._effects_deployment_binding()
    if handoff['legacy_plan_sha256'] != deployment:
        raise TransitionBlocked('initial_service_handoff_unverified')
    return handoff | {'controller_plan_sha256': _pin(backend.fence_adapter.plan_sha256)}


def _read_pinned(path, digest):
    def identity(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns, getattr(info, 'st_file_attributes', 0))

    try:
        if not path.is_absolute() or any(p.is_symlink() or getattr(p.lstat(), 'st_file_attributes', 0) & 1024
                                         for p in (path, *path.parents)):
            raise ValueError()
        with path.open('rb') as stream:
            before = os.fstat(stream.fileno())
            raw = stream.read(4097)
            after = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not before.st_ino
                or not 0 < len(raw) == before.st_size <= 4096
                or identity(before) != identity(after) or identity(after) != identity(path.lstat())
                or hashlib.sha256(raw).hexdigest() != digest):
            raise ValueError()
        return json.loads(raw), (before.st_dev, before.st_ino)
    except (OSError, ValueError, TypeError):
        raise TransitionBlocked('initial_service_handoff_unverified') from None


class WindowsInitialObserver:
    def __init__(self, client, intent_path, marker_sha256, cluster):
        self.client = client
        self.intent_path = intent_path
        self.marker_sha256 = marker_sha256
        self.cluster = cluster
        self.marker_identity = None

    def _marker(self, handoff):
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
        self._marker(handoff)

    def assert_legacy_closed(self, handoff, deadline):
        try:
            _pin(self.client.request(deadline))
            return True
        except ValueError:
            raise TransitionBlocked('initial_service_live_closure_unverified') from None

    def assert_exclusion_held(self, handoff, deadline):
        # The full authenticated writer observations bracket the transaction.
        # Inside the role lock, recheck the same protected admission marker.
        self._marker(handoff)
        return time.monotonic() < deadline


@contextmanager
def native_initial_observer(backend):
    handoff = _handoff(backend)
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
                                           client_sha256=_pin(params['client_sha256']))
    except (ValueError, OSError, TypeError):
        raise TransitionBlocked('initial_service_live_closure_unverified') from None
    with client:
        yield WindowsInitialObserver(client, backend.plan_path.with_name('initial-cutover-activation.json'),
                                     _pin(params['marker_sha256']), plan['cluster'])


def serve_initial(backend, journal_path, observer):
    """Never resume a started attempt. A heartbeat is evidence, not a permit.

    The supplied native observer must read the current protected owner intent,
    authenticated Windows/WSL writer closure, and exact runtime under the same
    plan and attempt. The coordinator independently checks PostgreSQL and the
    exact service process when consuming a heartbeat.
    """
    handoff = _handoff(backend)
    journal = Journal(journal_path)
    state = {'cluster': backend.plan['cluster'], 'generation': 1, 'timeline': 1, 'target': 'A', 'old': 'B'}
    with exclusive(Path(journal_path).with_suffix('.lock')):
        if os.path.lexists(journal.path):
            raise TransitionBlocked('initial_service_reconciliation_required')
        observer.assert_intent(handoff)
        stable = {'schema_version': 1, 'kind': 'initial-wsl-cutover', 'generation': 1, **handoff}
        journal.save(stable | {'phase': 'opening', 'sequence': 0})
        lease = InitialPgLease(backend, lambda _, deadline: observer.assert_legacy_closed(handoff, deadline),
                               assert_exclusion_held=lambda _, deadline: observer.assert_exclusion_held(handoff, deadline))
        sequence = 0
        try:
            started = time.monotonic()
            proof = lease.open(state)
            first_proof = _pin(proof)
            while True:
                elapsed = time.monotonic() - started
                if elapsed > PROOF_SECONDS:
                    raise TransitionBlocked('initial_service_proof_deadline_exceeded')
                sequence += 1
                journal.save(stable | {'phase': 'serving', 'sequence': sequence, 'lease_proof_sha256': _pin(proof),
                                       'first_write_proof_sha256': first_proof, 'proof_elapsed_ms': round(elapsed * 1000, 3)})
                time.sleep(max(0, 1 - (time.monotonic() - started)))
                started = time.monotonic()
                proof = lease.renew(state)
        finally:
            revoked = False
            try:
                revoked = lease.revoke(state) is True
            except (OSError, TransitionBlocked):
                pass
            journal.save(stable | {'phase': 'local_revoke_observed' if revoked else 'lease_lost',
                                   'sequence': sequence})
