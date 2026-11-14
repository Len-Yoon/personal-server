"""Bind the native service's first write proof to the prepared A guest."""

from contextlib import contextmanager
from dataclasses import asdict
import json
import time

from . import native_stdio_bridge as bridge
from .native_guest_activation import ActivationPins


def activation_intent(request, raw_state, restore_receipt):
    """Read the durable WSL journal at the last closed-to-open boundary."""
    bridge._need(request.verb == 'activate' and request.node == 'A' and bridge._hash(restore_receipt),
                 'activation_intent_unbound')
    bridge._need(type(raw_state) is dict and raw_state.get('phase') == 'activate_vm_intent'
                 and raw_state.get('plan_sha256') == request.binding.plan_sha256
                 and raw_state.get('attempt_id') == request.binding.attempt_id, 'activation_intent_unbound')
    receipts = raw_state.get('receipts')
    bridge._need(type(receipts) is dict and receipts.get('restore') == restore_receipt
                 and receipts.get('recheck_reactivation') == request.args['settlement_sha256']
                 and all(bridge._hash(receipts.get(key)) for key in ('fence_writers', 'capture', 'verify_closed')),
                 'activation_intent_unbound')
    return bridge._bytes({'schema_version': 1, 'phase': 'activate_vm_intent',
                          'attempt_id': request.binding.attempt_id, 'legacy_plan_sha256': request.binding.plan_sha256,
                          'restore_receipt_sha256': restore_receipt, 'fence_sha256': receipts['fence_writers'],
                          'activation_check_sha256': request.args['activation_check_sha256'],
                          'settlement_sha256': request.args['settlement_sha256']})


def _validate_guest_check(request, installation_id, checked_record, intent):
    bridge._need(request.verb == 'activate' and request.node == 'A' and bridge._hash(installation_id),
                 'activation_handoff_unbound')
    bridge._need(type(checked_record) is dict
                 and bridge._digest(bridge._bytes(checked_record)) == request.args['activation_check_sha256']
                 and checked_record.get('binding') == request.binding.fields()
                 and checked_record.get('node') == 'A' and checked_record.get('verb') == 'assert_activation_closed',
                 'activation_handoff_unbound')
    checked = checked_record.get('evidence')
    fields = {'manifest_sha256', 'restore_receipt_sha256', 'runtime_receipt_sha256',
              'closed_a_sha256', 'closed_b_sha256', 'closed_postgres_sha256'}
    bridge._need(type(checked) is dict and set(checked) == fields and all(map(bridge._hash, checked.values()))
                 and checked['manifest_sha256'] == request.args['manifest_sha256'], 'activation_handoff_unbound')
    document = json.loads(intent, object_pairs_hook=bridge._pairs, parse_constant=bridge._reject_constant)
    bridge._need(type(document) is dict and bridge._hash(document.get('fence_sha256')), 'activation_handoff_unbound')
    expected_intent = {'schema_version': 1, 'phase': 'activate_vm_intent',
                       'attempt_id': request.binding.attempt_id, 'legacy_plan_sha256': request.binding.plan_sha256,
                       'restore_receipt_sha256': checked['restore_receipt_sha256'], 'fence_sha256': document['fence_sha256'],
                       'activation_check_sha256': request.args['activation_check_sha256'],
                       'settlement_sha256': request.args['settlement_sha256']}
    bridge._need(intent == bridge._bytes(expected_intent), 'activation_handoff_unbound')
    return checked


def guest_handoff(request, installation_id, checked_record, intent, first_write_proof, service_instance):
    """Build the one-shot A input from independently read native receipts."""
    checked = _validate_guest_check(request, installation_id, checked_record, intent)
    pins = ActivationPins(coordinator_intent_sha256=bridge._digest(intent),
                          primary_prepared_closed_sha256=checked['closed_a_sha256'],
                          standby_prepared_closed_sha256=checked['closed_b_sha256'],
                          primary_runtime_receipt_sha256=checked['runtime_receipt_sha256'],
                          windows_service_instance_sha256=service_instance,
                          first_write_proof_sha256=first_write_proof)
    pins.validate()
    return bridge._bytes({'schema_version': 1, 'binding': request.binding.fields(), 'node': 'A',
                          'installation_id': installation_id, 'manifest_sha256': request.args['manifest_sha256'],
                          'settlement_sha256': request.args['settlement_sha256'],
                          'activation_check_sha256': request.args['activation_check_sha256'],
                          'prepared_receipt_sha256': checked['runtime_receipt_sha256'], 'activation_pins': asdict(pins)})


_SSH_TIMEOUTS = {'guest_ssh_deadline_exceeded', 'bridge_deadline_exceeded'}


@contextmanager
def _transport_stage(stage):
    try:
        yield
    except bridge.BridgeRejected as error:
        if str(error) not in _SSH_TIMEOUTS:
            raise
        raise bridge.BridgeRejected(f'activation_{stage}_ssh_deadline_exceeded') from None


class ActivationDispatcher:
    """Keep the native Windows custody open across the authenticated guest start."""

    def __init__(self, guest, custody):
        self.guest = guest
        self.custody = custody

    def __call__(self, request, deadline):
        if request.verb != 'activate':
            return self.guest(request, deadline)
        bridge._need(time.monotonic() < deadline and request.node == 'A', 'activation_dispatch_unbound')
        with _transport_stage("read_guest_check"):
            checked = self.custody.read_guest_check(request, deadline)
        restore_receipt = checked['evidence']['restore_receipt_sha256']
        with _transport_stage("read_wsl_intent"):
            intent = activation_intent(request, self.custody.read_wsl_intent(request, deadline), restore_receipt)
        # Validate the full receipt before the native service can grant writes.
        installation_id = self.custody.installation_id
        _validate_guest_check(request, installation_id, checked, intent)
        with _transport_stage("start_service"), self.custody.open_first_write(request, intent, deadline) as windows:
            payload = guest_handoff(request, installation_id, checked, intent, windows.first_write_proof_sha256,
                                    windows.service_instance_sha256)
            with _transport_stage("live_service_check"):
                try:
                    proof = windows.assert_current(deadline)
                except bridge.BridgeRejected as error:
                    if str(error) not in _SSH_TIMEOUTS:
                        raise
                    # Only this read can be repeated before the handoff exists.
                    # A new request samples the current lease after transport recovery.
                    bridge._remaining(deadline)
                    proof = windows.assert_current(deadline)
                bridge._need(bridge._hash(proof), 'activation_dispatch_unconfirmed')
            with _transport_stage("publish_handoff"):
                self.custody.publish_guest_handoff(request, payload, deadline)
            def check_guest_boundary(boundary_deadline):
                with _transport_stage("guest_boundary_check"):
                    return windows.assert_current(boundary_deadline)

            with _transport_stage("start_guest"):
                result = self.guest(request, deadline, assert_windows_current=check_guest_boundary)
            bridge._need(type(result) is bridge.Receipt and result.status == 'PASS', 'activation_dispatch_unconfirmed')
            result.fields(request.verb)
            bridge._need(result.service_instance_sha256 is None, 'activation_dispatch_unconfirmed')
            with _transport_stage("post_start_service_check"):
                bridge._need(bridge._hash(windows.assert_current(deadline)), 'activation_dispatch_unconfirmed')
            return bridge.Receipt('PASS', result.receipt_sha256, windows.service_instance_sha256)
