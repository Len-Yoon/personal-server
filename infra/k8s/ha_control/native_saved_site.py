"""Bind the native saved-reboot coordinator to its authenticated site operations."""

import time

from .native_sameboot_driver import NativeSamebootDriver
from .native_sameboot_release import NativeSamebootRelease
from .native_saved_reboot import WindowsSavedReboot
from .native_saved_site_apps import SavedSiteApps
from .native_saved_site_host import SavedSiteHostPaths
from .native_saved_site_material import SavedSitePlanAssembly
from .native_saved_site_observers import SavedSiteObservers
from .native_saved_site_provider_replace import SavedSiteProviderReplace
from .native_saved_site_publication import SavedSitePublication
from .native_saved_site_runtime import WindowsSavedSiteRuntime
from .native_sameboot_successor import draft_postboot
from .sameboot_transition import ROLLBACK_RESUME_FIELD, _fallback_attempt, _pin, _scope, _validate
from .native_stdio_bridge import _need as require


def _need(value):
    require(value, 'saved_site_binding_unconfirmed')


class SavedSitePlatform:
    """Use native adapters for prepared hosts, runtime, Windows material and apps.

    The site's authenticated guest operations supply its provider and carryover
    commands and independent preflight/custody observations. No success receipt
    is generated in their place. The app adapter is created after reading the
    exact stopped predecessor and reused within that same scoped runner.
    """

    def __init__(self, scope, *, host_paths, runtime, observers, plan, guest_site,
                 applications, provider_replacement, stopped_terminal=None):
        self.scope = _scope(scope)
        _need(self.scope.get('saved_reboot') is True
              and type(host_paths) is SavedSiteHostPaths and type(runtime) is WindowsSavedSiteRuntime
              and type(observers) is SavedSiteObservers and type(plan) is SavedSitePlanAssembly
              and host_paths.scope == self.scope == plan.scope
              and host_paths.pair is runtime.pair is plan.pair
              and plan.runtime is runtime and observers.windows is runtime.pair.windows
              and all(callable(getattr(guest_site, name, None)) for name in
                      ('guard', 'preflight', 'carryover'))
              and callable(applications) and callable(provider_replacement)
              and (stopped_terminal is None or _pin(stopped_terminal)))
        self.host_paths, self.runtime, self.observers, self.plan = host_paths, runtime, observers, plan
        self.pair = runtime.pair
        self.guest_site, self.make_apps = guest_site, applications
        self.make_provider = provider_replacement
        self.stopped_terminal = stopped_terminal
        self.app_adapter = None
        self.provider_adapter = self.release = None

    def _check(self, operation, deadline):
        _need(operation.get('scope') == self.scope and time.monotonic() < deadline)

    def guard(self, operation, chain, deadline, *, observe_only):
        self._check(operation, deadline)
        return self.guest_site.guard(operation, chain, deadline, observe_only=observe_only) is True

    def _dispatch(self, operation, chain, deadline, *, observe_only):
        self._check(operation, deadline)
        phase = operation['stage']
        if phase == 'preflight':
            if self.scope.get(ROLLBACK_RESUME_FIELD) is True:
                verify = getattr(self.guest_site, 'preflight_provider_recovery', None)
                _need(callable(verify) and callable(getattr(self.guest_site, 'observe_held_carryover', None))
                      and verify(operation, deadline) is True)
            self.host_paths.verify(deadline, stopped_terminal=self.stopped_terminal)
            self.runtime.preflight(operation, deadline, stopped_terminal=self.stopped_terminal)
            evidence = self.guest_site.preflight(operation, deadline)
            _validate(phase, evidence, self.scope, chain)
            return {'status': 'PASS', 'evidence': evidence}
        if phase == 'stage_runtime':
            evidence = self.runtime.stage(operation, chain, deadline, observe_only=observe_only)
            return {'status': 'PASS', 'evidence': evidence}
        if phase == 'carryover' and 'verify_restored_pair' in chain:
            evidence = self._rollback_carryover(operation, chain, deadline, observe_only=observe_only)
            return {'status': 'PASS', 'evidence': evidence}
        if phase == 'connect_rollback_held':
            pin = chain['carryover']['carryover_sha256']
            evidence = self.guest_site.observe_held_carryover(operation, chain, pin, deadline, observe_only=observe_only)
            _validate(phase, evidence, self.scope, chain)
            _need(time.monotonic() < deadline)
            return {'status': 'PASS', 'evidence': evidence}
        if phase in {'carryover', 'fallback_carryover'}:
            evidence = self.guest_site.carryover(operation, chain, deadline, observe_only=observe_only)
            return {'status': 'PASS', 'evidence': evidence}
        _need(phase in {'replace_provider', 'restore_provider'})
        return getattr(self._provider(chain), phase)(operation, chain, deadline, observe_only=observe_only)

    def _rollback_carryover(self, operation, chain, deadline, *, observe_only):
        prior = self.plan.previous
        terminal = self.plan.read_terminal(operation, chain, deadline)
        draft = draft_postboot(prior['plan'], terminal, prior['binding'], prior['args'], prior['stopped'],
                              previous_sha256=self.scope['previous_plan_sha256'],
                              attempt=_fallback_attempt(self.scope),
                              successor_image_sha256=self.scope['successor_image_sha256'])
        pin = self.applications(operation, chain, deadline).attest_rollback_carryover(
            operation, chain, deadline, draft=draft, predecessor_plan=prior['plan'], observe_only=observe_only)
        return {'history_sha256': self.scope['history_sha256'], 'carryover_sha256': pin}

    def _provider(self, chain):
        pin = chain['retire_provider'].get('provider_retirement_sha256')
        _need(self.release is not None and _pin(pin))
        if self.provider_adapter is None:
            self.provider_adapter = self.make_provider(pin)
        _need(type(self.provider_adapter) is SavedSiteProviderReplace
              and self.provider_adapter.retirement is self.release.guest
              and self.provider_adapter.retirement_pin == pin)
        return self.provider_adapter

    def apply(self, operation, chain, deadline):
        return self._dispatch(operation, chain, deadline, observe_only=False)

    def observe(self, operation, chain, deadline):
        return self._dispatch(operation, chain, deadline, observe_only=True)

    def resume_provider_restore(self, operation, chain, deadline):
        self._check(operation, deadline)
        _need(operation['stage'] == 'restore_provider')
        return self._provider(chain).resume_provider_restore(operation, chain, deadline)

    def verify_restored_provider(self, operation, chain, deadline):
        self._check(operation, deadline)
        _need(operation['stage'] == 'verify_restored_pair')
        original = chain['restore_provider_operation']
        _need(original['stage'] == 'restore_provider' and original['scope'] == self.scope)
        predecessor = {field: value for field, value in chain.items()
                       if field not in {'restore_provider', 'restore_provider_operation'}}
        restored = self._provider(chain).restore_provider(original, predecessor, deadline, observe_only=True)
        _need(restored['status'] == 'PASS')
        _validate('restore_provider', restored['evidence'], self.scope, predecessor)
        return True

    def saved_observers(self, operation, chain, deadline):
        self._check(operation, deadline)
        return self.observers(operation, chain, deadline)

    def prepare_material(self, operation, chain, deadline):
        return self.plan.prepare_material(operation, chain, deadline)

    def saved_material(self, operation, chain, deadline):
        return self.plan.saved_material(operation, chain, deadline)

    def read_arm_material(self, operation, chain, deadline):
        return self.plan.read_arm_material(operation, chain, deadline)

    def successor_options(self, operation, chain, material, deadline):
        return self.plan.successor_options(operation, chain, material, deadline)

    def read_terminal(self, operation, chain, deadline):
        self._check(operation, deadline)
        return self.plan.read_terminal(operation, chain, deadline)

    def applications(self, operation, chain, deadline):
        self._check(operation, deadline)
        if self.app_adapter is None:
            self.app_adapter = self.make_apps(operation, chain, deadline)
        _need(type(self.app_adapter) is SavedSiteApps and self.app_adapter.pair is self.pair
              and self.app_adapter.terminal_pin == chain['close_pair']['terminal_sha256'])
        return self.app_adapter

    def quiesce_operation(self, operation, chain, deadline, *, observe_only):
        return self.applications(operation, chain, deadline).quiesce_operation(
            operation, chain, deadline, observe_only=observe_only)

    def app_operation(self, operation, chain, deadline):
        return self.applications(operation, chain, deadline).app_operation(operation, chain, deadline)

    def read_app_operation(self, operation, chain, deadline):
        return self.applications(operation, chain, deadline).read_app_operation(operation, chain, deadline)

    def driver(self, *, release, saved_boot, paired_publication=None, app_stage=None, live_authority_sha256=None):
        """Connect the app factory to the same scoped adapter as its journal."""
        _need(type(release) is NativeSamebootRelease and release.scope == self.scope and release.pair is self.pair
              and release.expected_stopped_terminal_sha256 == self.stopped_terminal
              and type(saved_boot) is WindowsSavedReboot and saved_boot.pair is self.pair)
        if live_authority_sha256 is not None:
            _need(paired_publication is None and app_stage is None)
            native = SavedSitePublication(self.plan, self.applications, live_authority_sha256=live_authority_sha256)
            paired_publication, app_stage = native.paired_publication, native.app_stage
        _need(callable(paired_publication) and callable(app_stage))
        self.release = release

        def create_app(operation, chain, publication, deadline):
            successor, carryover = app_stage(operation, chain, publication, deadline)
            self.applications(operation, chain, deadline).bind_successor(successor, carryover)
            return successor

        return NativeSamebootDriver(release=release, platform=self, windows=self.pair.windows,
                                    successor_options={}, paired_publication=paired_publication,
                                    apps_factory=create_app, saved_boot=saved_boot)
