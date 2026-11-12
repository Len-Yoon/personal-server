"""Compose the native source and Caddy stages with authenticated guest and host observations."""

from contextlib import contextmanager
import hashlib
from pathlib import Path

from ha_runtime import seven_capture
from ha_runtime.config import SERVICES

from .legacy_cutover import LegacyCutover, _pin, _receipt
from .legacy_writers import _need
from .native_cutover import NativeCutover
from .native_frozen_capture import NativeFrozenCapture


ALL_SERVICES = sorted(SERVICES)
ROLLBACK_PHASES = {
    'close_admission_intent', 'close_admission_complete', 'block_reactivation_intent',
    'block_reactivation_complete', 'fence_writers_intent', 'fence_writers_complete',
    'capture_intent', 'capture_complete', 'restore_intent', 'restore_complete',
    'verify_closed_complete', 'activate_vm_intent',
}


def compose_native_cutover(journal_path, windows, admission, wsl_bootstrap, applications):
    """Keep the native host guard live inside the Caddy mutation boundary."""
    native = NativeCutover(windows, admission, applications, wsl_bootstrap=wsl_bootstrap)
    applications.bind_coordinator(Path(journal_path), native._verify_guard)
    return LegacyCutover(journal_path, native)


class NativeApplications:
    """Coordinate one attempt over externally supplied host and guest adapters.

    Each adapter receipt has exact plan_sha256, attempt_id and evidence_sha256
    fields plus the facts checked at its call site. Host observe is read-only;
    its boundary and prior_sha256 identify the live observation required. The
    host guard excludes other operators through forward work and rollback.
    For vm_live_authority the host adapter must verify a fresh Windows generation
    observation and the corresponding live PostgreSQL writer lease on A. The
    guest adapter must bind actual app readiness to that observation. Receipt
    checks here bind adapter results; authenticated transport and verification
    of the underlying Windows, PostgreSQL and guest evidence are external.
    """

    def __init__(self, *, capture_root, identity, converter_source_pins, external_writers,
                 caddy, active_ip, expected_mtls_sha256, vm, host):
        self.capture_root = Path(capture_root)
        self.identity = dict(identity)
        self.converter_source_pins = dict(converter_source_pins)
        self.external_writers = {service: tuple(ids) for service, ids in external_writers.items()}
        self.caddy = caddy
        self.active_ip = active_ip
        self.expected_mtls_sha256 = dict(expected_mtls_sha256)
        self.vm, self.host = vm, host
        self.journal_path = None
        self.attempt = None
        self.active = False

    def bind_coordinator(self, journal_path, verify_native_guard):
        _need(self.journal_path is None and self.attempt is None and callable(verify_native_guard),
              'native_application_coordinator_unbound')
        self.journal_path = journal_path
        self.verify_native_guard = verify_native_guard

    @contextmanager
    def guard(self, plan, attempt):
        _need(self.journal_path is not None and self.attempt is None and plan == self.identity['plan_sha256'],
              'native_application_coordinator_unbound')
        self.plan, self.attempt = _pin(plan), _pin(attempt)
        self.capture_side = NativeFrozenCapture(self.capture_root, self.identity, attempt, self.converter_source_pins,
                                                self.external_writers, self.host.observe_external_closed)
        self.baseline = self.blocked = self.fence = self.manifest = self.restored = self.verified = None
        self.activation_check = self.authority = self.ingress = self.safety = None
        self.live_generation = None
        self.live_observations = set()
        self.restore_attempted = self.activation_attempted = self.legacy_runtime_restored = False
        with self.host.guard(plan, attempt):
            self.active = True
            try:
                yield
            finally:
                self.active = False

    def _plan(self, plan):
        _need(self.active and self.plan == plan, 'native_application_attempt_unbound')

    def _phase(self, phase, **expected):
        reader = seven_capture.Reader()
        journal = seven_capture.parse(reader.read(seven_capture.descriptor(self.journal_path)))
        _need(type(journal) is dict and type(journal.get('version')) is int and journal['version'] == 1
              and type(journal.get('receipts')) is dict and journal.get('plan_sha256') == self.plan
              and journal.get('attempt_id') == self.attempt and journal.get('phase') == phase
              and all(journal['receipts'].get(key) == _pin(value) for key, value in expected.items()),
              'native_application_intent_unbound')
        reader.unchanged()
        return journal

    def _join(self, stage, **evidence):
        return seven_capture.sha(seven_capture.encoded({
            'stage': stage, 'plan_sha256': self.plan, 'attempt_id': self.attempt, **evidence,
        }))

    def _answer(self, evidence, **facts):
        return {'plan_sha256': self.plan, 'attempt_id': self.attempt, 'evidence_sha256': _pin(evidence), **facts}

    def _accept(self, value, **facts):
        return _receipt(value, self.plan, self.attempt, **facts)

    def _host(self, boundary, prior, **facts):
        value = self.host.observe(self.plan, self.attempt, boundary, _pin(prior))
        return self._accept(value, boundary=boundary, prior_sha256=prior, **facts)

    def preflight(self, plan):
        self._plan(plan)
        self._phase('preflight')
        host = self._host('preflight', plan, legacy_live=True, protected_healthy=True, direct_recovery_ready=True,
                          authority_storage_trusted=True, deployment_admission_ready=True)
        target = self.vm.preflight(plan, self.attempt, dict(self.identity))
        self.target = self._accept(target, target_system_id_sha256=hashlib.sha256(
            self.identity['target_system_id'].encode()).hexdigest(), restore_ready=True,
            target_writes_closed=True, target_never_opened=True)
        self.caddy_baseline = self.caddy.inspect()
        runtime = _pin(self.capture_side.prepare())
        caddy_dir = self.capture_side.root / 'caddy'
        caddy_dir.mkdir(mode=0o700)
        self.caddy_candidate = self.caddy.prepare(self.caddy_baseline, self.active_ip, caddy_dir,
                                                 expected_mtls_sha256=self.expected_mtls_sha256)
        self.baseline = self._join('preflight', host=host, target=self.target, runtime=runtime,
                                   caddy=self.caddy_baseline, candidate=_pin(self.caddy_candidate.candidate_sha256))
        return self._answer(self.baseline, capture_ready=True, restore_ready=True, ingress_ready=True,
                            legacy_live=True, target_writes_closed=True)

    def assert_direct_recovery_excluded(self, plan, proof):
        self._plan(plan)
        _need(self.baseline is not None, 'native_application_preflight_required')
        self._host('direct_recovery_excluded', proof, direct_recovery_excluded=True, recovery_commands_settled=True)

    def assert_authority_storage_trusted(self, plan, baseline):
        self._plan(plan)
        _need(baseline == self.baseline, 'native_application_preflight_required')
        self._host('authority_storage_trusted', baseline, authority_storage_trusted=True)

    def block_linux_reactivation(self, plan, baseline, windows):
        self._plan(plan)
        state = self._phase('block_reactivation_intent', preflight=self.baseline)
        _need(baseline == self.baseline and self.blocked is None, 'native_application_reactivation_unbound')
        windows = _pin(windows)
        admission = _pin(state['receipts']['close_admission'])
        self.capture_side.block_reactivation()
        self.blocked = self._join('block_linux_reactivation', baseline=baseline, admission=admission, windows=windows)
        return self.blocked

    def fence_writers(self, plan, blocked, settled):
        self._plan(plan)
        self._phase('fence_writers_intent', block_reactivation=self.blocked)
        _need(blocked == self.blocked and self.fence is None, 'native_application_reactivation_unbound')
        receipt = self.capture_side.fence(blocked, _pin(settled))
        self.fence = self._accept(receipt, reactivation_sha256=blocked, settlement_sha256=settled,
                                  writers_closed=True, services=ALL_SERVICES)
        return receipt

    def capture(self, plan, fence):
        self._plan(plan)
        self._phase('capture_intent', fence_writers=self.fence)
        _need(fence == self.fence and self.manifest is None, 'native_application_fence_unbound')
        receipt = self.capture_side.capture(fence)
        manifest = _pin(receipt['manifest_sha256'])
        self.capture_receipt = self._accept(receipt, fence_sha256=fence, manifest_sha256=manifest)
        self.manifest = manifest
        return receipt

    def restore(self, plan, manifest):
        self._plan(plan)
        self._phase('restore_intent', fence_writers=self.fence, capture=self.capture_receipt)
        _need(manifest == self.manifest and not self.restore_attempted, 'native_application_manifest_unbound')
        self.capture_side.assert_closed(self.fence)
        self.verify_native_guard(self.fence)
        self.restore_attempted = True
        receipt = self.vm.restore(plan, self.attempt, self.capture_side.root / 'frozen', manifest, self.target)
        self.restored = self._accept(receipt, manifest_sha256=manifest, target_sha256=self.target,
                                     target_writes_closed=True, services=ALL_SERVICES)
        return self._answer(self.restored, manifest_sha256=manifest, target_writes_closed=True, services=ALL_SERVICES)

    def verify_closed(self, plan, fence, restore):
        self._plan(plan)
        self._phase('restore_complete', fence_writers=self.fence, restore=self.restored)
        _need(fence == self.fence and restore == self.restored, 'native_application_restore_unbound')
        self.capture_side.assert_closed(fence)
        remote = self.vm.verify_closed(plan, self.attempt, self.manifest, restore)
        self.verified = self._accept(remote, manifest_sha256=self.manifest, restore_sha256=restore,
                                     readiness=True, target_writes_closed=True, services=ALL_SERVICES)
        return self._answer(self.verified, fence_sha256=fence, restore_sha256=restore, readiness=True,
                            target_writes_closed=True, services=ALL_SERVICES)

    def assert_activation_closed(self, plan, fence, restore):
        self._plan(plan)
        self._phase('activate_vm_intent', fence_writers=self.fence, restore=self.restored, verify_closed=self.verified)
        _need(fence == self.fence and restore == self.restored, 'native_application_restore_unbound')
        self.capture_side.assert_closed(fence)
        remote = self.vm.assert_activation_closed(plan, self.attempt, self.manifest, restore, self.verified)
        self.activation_check = self._accept(remote, manifest_sha256=self.manifest, restore_sha256=restore,
                                             verified_sha256=self.verified, target_writes_closed=True,
                                             target_never_opened=True, restore_ready=True)
        _need(self.activation_check != self.verified, 'native_application_activation_unbound')

    def activate_vm(self, plan, verified, settled):
        self._plan(plan)
        self._phase('activate_vm_intent', fence_writers=self.fence, restore=self.restored, verify_closed=self.verified,
                    recheck_reactivation=settled)
        _need(verified == self.verified and self.activation_check is not None and not self.activation_attempted,
              'native_application_activation_unbound')
        settled = _pin(settled)
        self.verify_native_guard(settled)
        self.capture_side.assert_closed(self.fence)
        self.activation_attempted = True
        remote = self.vm.activate(plan, self.attempt, self.manifest, verified, settled,
                                  activation_check_sha256=self.activation_check)
        activation = self._accept(remote, manifest_sha256=self.manifest, verified_sha256=verified,
                                  settlement_sha256=settled, activation_check_sha256=self.activation_check,
                                  vm_write_authority=True)
        self.authority = activation
        return self._answer(activation, verified_sha256=verified, settlement_sha256=settled, vm_write_authority=True)

    def switch_caddy(self, plan, authority):
        self._plan(plan)
        _need(authority == self.authority and self.ingress is None, 'native_application_authority_unbound')
        live = None

        def assert_guard():
            nonlocal live
            self._phase('switch_caddy_intent', activate_vm=self.authority)
            self.verify_native_guard(authority)
            self.capture_side.assert_closed(self.fence)
            live = self._vm_live(plan, authority)

        caddy = self.caddy.apply(self.caddy_candidate, assert_guard)
        _need(live is not None and type(caddy) is dict
              and set(caddy) == {'container_sha256', 'live_sha256', 'candidate_sha256'}
              and caddy['candidate_sha256'] == self.caddy_candidate.candidate_sha256, 'native_application_ingress_unbound')
        self.ingress = self._join('switch_caddy', authority=authority, vm_live=live,
                                  **{key: _pin(value) for key, value in caddy.items()})
        return self._answer(self.ingress, authority_sha256=authority, vm_route_live=True)

    def _vm_live(self, plan, authority):
        boundary = 'vm_live_authority'
        observation = self.host.observe(plan, self.attempt, boundary, _pin(authority))
        _need(type(observation) is dict and type(observation.get('windows_generation')) is int
              and observation['windows_generation'] > 0, 'native_application_live_generation_unbound')
        generation = observation['windows_generation']
        _need(self.live_generation in (None, generation), 'native_application_live_generation_changed')
        bindings = {
            'windows_generation': generation,
            'windows_generation_sha256': _pin(observation.get('windows_generation_sha256')),
            'live_lease_sha256': _pin(observation.get('live_lease_sha256')),
        }
        host = self._accept(observation, boundary=boundary, prior_sha256=authority, active_node='A', **bindings)
        _need(host != authority and host not in self.live_observations, 'native_application_live_authority_replayed')
        self.live_generation = generation
        self.live_observations.add(host)
        references = dict(bindings, live_authority_sha256=host)
        remote = self.vm.assert_live(plan, self.attempt, authority, **references)
        vm = self._accept(remote, authority_sha256=authority, ready=True, services=ALL_SERVICES, **references)
        return self._join('vm_live', authority=authority, host=host, vm=vm)

    def verify_live(self, plan, authority, ingress):
        self._plan(plan)
        self._phase('switch_caddy_complete', activate_vm=self.authority, switch_caddy=self.ingress)
        _need(authority == self.authority and ingress == self.ingress, 'native_application_ingress_unbound')
        vm = self._vm_live(plan, authority)
        host = self._host('verify_live', ingress, protected_healthy=True, external_healthy=True, services=ALL_SERVICES)
        after = self._vm_live(plan, authority)
        return self._answer(self._join('verify_live', host=host, vm=vm, vm_after=after, ingress=ingress), authority_sha256=authority,
                            ingress_sha256=ingress, services=ALL_SERVICES, ready=True)

    def assert_durable_legacy_exclusion(self, plan, authority):
        self._plan(plan)
        _need(authority == self.authority, 'native_application_authority_unbound')
        self.capture_side.assert_closed(self.fence)
        self._host('durable_legacy_exclusion', authority, legacy_writers_closed=True, exclusion_durable=True)

    def _rollback_checks(self, plan, baseline):
        _need(baseline == self.baseline and not self.activation_attempted, 'native_application_rollback_unbound')
        self.capture_side.assert_reader_settled()
        self.caddy.assert_original(self.caddy_baseline)
        host = self._host('rollback', baseline, readers_settled=True, prior_mutations_settled=True,
                          deployment_admission_reconciled=True, legacy_route_unchanged=True)
        remote = self.vm.observe_rollback(plan, self.attempt, baseline, self.manifest, self.restore_attempted)
        status = remote.get('status') if type(remote) is dict else None
        allowed = ('CLEAN', 'CLEAN_FAILED', 'COMMITTED_CLOSED') if self.restore_attempted else ('CLEAN',)
        _need(status in allowed, 'native_application_rollback_unconfirmed')
        vm = self._accept(remote, status=status, baseline_sha256=baseline, manifest_sha256=self.manifest,
                          restore_attempted=self.restore_attempted, target_never_opened=True,
                          target_writes_closed=True, prior_mutations_settled=True)
        return status, host, vm

    def assert_rollback_safe(self, plan, baseline):
        self._plan(plan)
        state = self._phase('rollback_inspection_intent', preflight=self.baseline)
        failure = state.get('failure_phase')
        _need(failure in ROLLBACK_PHASES, 'native_application_rollback_unbound')
        self.rollback_failure = failure
        self.rollback_status, host, vm = self._rollback_checks(plan, baseline)
        self.safety = self._join('rollback', baseline=baseline, failure=failure, host=host, vm=vm)
        return self._answer(self.safety, baseline_sha256=baseline, target_never_opened=True, target_writes_closed=True,
                            legacy_route_unchanged=True, prior_mutations_settled=True, deployment_admission_reconciled=True)

    def restore_legacy_runtime(self, plan, baseline, safety, linux_touched):
        self._plan(plan)
        state = self._phase('restore_legacy_intent', preflight=self.baseline, rollback_inspection=self.safety)
        _need(state.get('failure_phase') == self.rollback_failure and safety == self.safety
              and type(linux_touched) is bool and not self.legacy_runtime_restored,
              'native_application_rollback_unbound')
        status, host, vm = self._rollback_checks(plan, baseline)
        _need(status == self.rollback_status, 'native_application_rollback_unconfirmed')
        if linux_touched:
            self.capture_side.restore_writers()
        health = self._host('legacy_healthy', safety, legacy_healthy=True, protected_healthy=True)
        self.legacy_runtime_restored = True
        return self._join('restore_legacy_runtime', safety=safety, host=host, vm=vm, health=health)

    def assert_direct_recovery_release_safe(self, plan, safety):
        self._plan(plan)
        self._phase('restore_legacy_intent', rollback_inspection=self.safety)
        _need(safety == self.safety and self.legacy_runtime_restored, 'native_application_rollback_unbound')
        value = self.host.assert_direct_recovery_release_safe(plan, self.attempt, safety)
        self._accept(value, safety_sha256=safety, release_safe=True)

    def assert_legacy_runtime_healthy(self, plan, baseline):
        self._plan(plan)
        _need(baseline == self.baseline and self.legacy_runtime_restored, 'native_application_rollback_unbound')
        self.caddy.assert_original(self.caddy_baseline)
        self._host('legacy_healthy', baseline, legacy_healthy=True, protected_healthy=True)
