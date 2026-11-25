"""Bind the same-boot coordinator to the native stopped-host and app operations."""

import hashlib
import json
import time

from .controller import TransitionBlocked
from .initial_postboot import saved_reboot, scope as postboot_scope
from .initial_unpublished import derive_predecessor
from .native_sameboot_successor import WindowsSamebootHost
from .native_stdio_bridge import RECOVERY_START_ORDER

ERROR = 'sameboot_native_driver_unconfirmed'
PRE_PUBLISH_CLOSED = {'replace_provider', 'restore_provider', 'carryover', 'stage_runtime',
                      'arm_host', 'publish'}


def _need(condition):
    if not condition:
        raise TransitionBlocked(ERROR)


def _decoded(request, receipts):
    _need(type(request) is bytes and type(receipts) is bytes)
    return json.loads(request), json.loads(receipts)


def _reply(request, evidence, *, status='PASS'):
    return {'issued_sha256': hashlib.sha256(request).hexdigest(), 'status': status, 'evidence': evidence}


class NativeSamebootDriver:
    """Coordinate native commands with signed site-specific preparation and readback.

    Platform operations install the verified provider and Windows material and
    obtain authenticated live receipts. Native start/arm must not be delegated
    to these operations. The independent pair observer authenticates both
    databases after either initial publication or a lost publication reply.
    """

    def __init__(self, *, release, platform, windows, successor_options, paired_publication, apps_factory):
        self.release = release
        self.platform = platform
        self.windows = windows
        self.successor_options = dict(successor_options)
        self.paired_publication = paired_publication
        self.apps_factory = apps_factory

    def guard(self, request, receipts, deadline, *, observe_only):
        operation, chain = _decoded(request, receipts)
        phase = operation['stage']
        _need(time.monotonic() < deadline and self.platform.guard(
            operation, chain, deadline, observe_only=observe_only) is True)
        if phase in PRE_PUBLISH_CLOSED and not (observe_only and phase == 'publish'):
            original = chain['close_pair']
            after_arm = phase == 'publish' or (observe_only and phase == 'arm_host')
            reboot = operation.get('scope', {}).get('saved_reboot', False)
            if after_arm or (reboot and phase == 'arm_host'):
                host = self._host(operation, chain, deadline)
                expected = (chain['arm_host']['plan_sha256'] if phase == 'publish'
                            else operation['plan_material_sha256'])
                _need(host.checked['plan_sha256'] == expected)
                if after_arm:
                    boot = {'resumed_windows_boot': host.config['boot_pin']} if reboot else {}
                    closed = self.release.pair.closed_pair_after_arm(
                        original['terminal_sha256'], host.config['image_path_pin'],
                        host.checked['successor_image_sha256'], deadline, **boot)
                else:
                    closed = self.release.pair.closed_pair_after_reboot(
                        original['terminal_sha256'], host.config['boot_pin'], deadline)
            else:
                closed = self.release.observe_closed(operation, chain, deadline)
            _need(closed['terminal_sha256'] == original['terminal_sha256']
                  and closed['revision'] == original['revision']
                  and closed['closed_nodes'] == ['A', 'B']
                  and all(closed[field] is True for field in ('synchronous', 'same_expiry',
                                                           'replay_verified', 'effects_isolated')))
        return time.monotonic() < deadline

    def arm_material(self, request, receipts, deadline):
        operation, chain = _decoded(request, receipts)
        material = self.platform.read_arm_material(operation, chain, deadline)
        _need(operation['stage'] == 'arm_host')
        return material

    def _host(self, operation, chain, deadline):
        material = self.platform.read_arm_material(operation, chain, deadline)
        reboot = saved_reboot(postboot_scope(material['candidate']))
        _need(reboot == operation['scope'].get('saved_reboot', False))
        bridge = {'reboot_pair': self.release.pair} if reboot else {}
        return WindowsSamebootHost(self.windows, material=material, **self.successor_options, **bridge)

    def _publication(self, host, published, operation, chain, deadline):
        armed = chain['arm_host']
        _need(host.checked['plan_sha256'] == armed['plan_sha256'])
        terminal = self.platform.read_terminal(operation, chain, deadline)
        terminal_sha256 = chain['retire_provider']['terminal_sha256']
        closed, opened, _ = derive_predecessor(terminal, terminal_sha256, published=True)
        _need(closed == chain['close_pair']['revision'] and opened == operation['scope']['previous_revision'] + 2)
        observed = self.paired_publication(published, terminal, operation, chain, deadline)
        _need(observed == {'plan_sha256': armed['plan_sha256'], 'revision': opened,
                          'writers': ['A'], 'standby_node': 'B', 'synchronous': True,
                          'instance_sha256': published['service_instance_sha256'],
                          'predecessor_instance_stopped': True})
        return observed | {'publication_sha256': published['first_write_proof_sha256']}

    def _apps(self, operation, chain, deadline, *, observe_only):
        host = self._host(operation, chain, deadline)
        published = host.observe_publication(deadline)
        previous = chain['publish']
        _need(published['service_instance_sha256'] == previous['instance_sha256'] and
              published['first_write_proof_sha256'] == previous['publication_sha256'])
        _need(self._publication(host, published, operation, chain, deadline) == previous)
        if observe_only:
            # The platform watches the already-issued guest nonce and mTLS
            # readiness. Another dispatcher never receives the same start.
            receipt = self.platform.observe_apps(operation, chain, deadline)
        else:
            app = self.apps_factory(operation, chain, published, deadline)
            arguments = self.platform.app_operation(operation, chain, deadline)
            _need(type(arguments) is dict and set(arguments) == {'request', 'assert_guard', 'issued', 'record'}
                  and arguments['request'].nonce == operation['nonce'])
            startup = app.start(arguments['request'], deadline, assert_guard=arguments['assert_guard'],
                                issued=arguments['issued'], record=arguments['record'])
            receipt = {'instance_sha256': published['service_instance_sha256'], 'startup_sha256': startup,
                       'ready': list(RECOVERY_START_ORDER)}
        _need(type(receipt) is dict and set(receipt) == {'instance_sha256', 'ready', 'startup_sha256'}
              and receipt['instance_sha256'] == published['service_instance_sha256']
              and receipt['ready'] == list(RECOVERY_START_ORDER))
        return receipt

    def _dispatch(self, request, receipts, deadline, *, observe_only):
        operation, chain = _decoded(request, receipts)
        phase = operation['stage']
        if phase == 'retire_provider':
            evidence = (self.release.observe_retired if observe_only else self.release.retire_provider)(
                operation, chain, deadline)
        elif phase == 'close_pair':
            evidence = self.release.observe_closed(operation, chain, deadline)
        elif phase == 'arm_host':
            host = self._host(operation, chain, deadline)
            _need(host.checked['plan_sha256'] == operation['plan_material_sha256'])
            evidence = (host.observe_armed if observe_only else host.arm)(deadline)
        elif phase == 'publish':
            host = self._host(operation, chain, deadline)
            _need(host.checked['plan_sha256'] == chain['arm_host']['plan_sha256'])
            published = (host.observe_publication if observe_only else host.start)(deadline)
            evidence = self._publication(host, published, operation, chain, deadline)
        elif phase == 'start_apps':
            evidence = self._apps(operation, chain, deadline, observe_only=observe_only)
        else:
            _need(phase in {'preflight', 'replace_provider', 'restore_provider', 'carryover', 'stage_runtime'})
            result = (self.platform.observe if observe_only else self.platform.apply)(operation, chain, deadline)
            _need(type(result) is dict and set(result) == {'status', 'evidence'})
            return _reply(request, result['evidence'], status=result['status'])
        return _reply(request, evidence)

    def apply(self, request, receipts, deadline):
        return self._dispatch(request, receipts, deadline, observe_only=False)

    def observe(self, request, receipts, deadline):
        return self._dispatch(request, receipts, deadline, observe_only=True)
