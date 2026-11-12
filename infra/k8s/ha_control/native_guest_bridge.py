"""NativeApplications VM adapter using the authenticated control bridge and mTLS data path."""

import hashlib
import json
from pathlib import Path
import secrets

from ha_runtime import seven_capture
from ha_runtime.config import SERVICES

from .frozen_ingress import send_to_vm
from .legacy_cutover import _pin
from .legacy_writers import _need
from .native_stdio_bridge import BridgeRejected


ALL_SERVICES = sorted(SERVICES)


class GuestBridgeRuntime:
    def __init__(self, client, journal_path, address, credentials, max_bytes):
        self.client = client
        self.journal_path = Path(journal_path)
        self.address = address
        self.credentials = Path(credentials)
        self.max_bytes = max_bytes
        self.target = {}
        self.closed = {}
        self.service_instance_sha256 = None

    def _assert_scope(self, plan, attempt):
        binding = self.client.binding
        _need(plan == binding.plan_sha256 and attempt == binding.attempt_id, 'guest_bridge_scope_mismatch')

    def _call(self, verb, node, **args):
        try:
            return self.client.call(verb, node, **args)
        except BridgeRejected:
            _need(False, 'guest_bridge_operation_unconfirmed')

    def _pass(self, verb, node, **args):
        result = self._call(verb, node, **args)
        _need(result.status == 'PASS', 'guest_bridge_operation_unconfirmed')
        return _pin(result.receipt_sha256)

    def _join(self, stage, **receipts):
        return hashlib.sha256(json.dumps({'stage': stage, **self.client.binding.fields(), **receipts},
                                         sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def _receipt(self, evidence, **facts):
        return dict(plan_sha256=self.client.binding.plan_sha256, attempt_id=self.client.binding.attempt_id,
                    evidence_sha256=_pin(evidence), **facts)

    def preflight(self, plan, attempt, identity):
        self._assert_scope(plan, attempt)
        _need(identity['plan_sha256'] == plan and not self.target, 'guest_bridge_target_unbound')
        system = hashlib.sha256(identity['target_system_id'].encode()).hexdigest()
        self.target = {node: self._pass('preflight', node, target_system_id_sha256=system) for node in ('A', 'B')}
        self.target_receipt = self._join('preflight', **self.target)
        return self._receipt(self.target_receipt, target_system_id_sha256=system, restore_ready=True,
                             target_writes_closed=True, target_never_opened=True)

    def _fence(self, plan, attempt):
        reader = seven_capture.Reader()
        state = seven_capture.parse(reader.read(seven_capture.descriptor(self.journal_path)))
        _need(type(state) is dict and state.get('phase') == 'restore_intent'
              and state.get('plan_sha256') == plan and state.get('attempt_id') == attempt,
              'guest_bridge_restore_intent_unbound')
        fence = _pin(state['receipts']['fence_writers'])
        reader.unchanged()
        return fence

    def restore(self, plan, attempt, source_dir, manifest, target):
        self._assert_scope(plan, attempt)
        _need(target == self.target_receipt, 'guest_bridge_target_unbound')
        fence = self._fence(plan, attempt)
        nonce = secrets.token_hex(32)
        receiver = self._pass('begin_receive', 'A', manifest_sha256=manifest, fence_sha256=fence,
                              transfer_nonce=nonce, max_bytes=self.max_bytes)

        def assert_target(address, observed_manifest):
            _need(address == self.address and observed_manifest == manifest and self._fence(plan, attempt) == fence,
                  'guest_bridge_receiver_unbound')

        delivered = send_to_vm(source_dir, manifest, self.address, self.credentials, nonce,
                               max_bytes=self.max_bytes, assert_owned_target=assert_target)
        _need(delivered == manifest, 'guest_bridge_transfer_unconfirmed')
        wire = self._join('transfer', manifest=manifest, receiver=receiver,
                          nonce_sha256=hashlib.sha256(nonce.encode()).hexdigest())
        self.transfer = self._pass('finish_receive', 'A', manifest_sha256=manifest, receiver_sha256=receiver,
                                   transfer_proof_sha256=wire)
        restored = self._pass('restore', 'A', manifest_sha256=manifest, transfer_receipt_sha256=self.transfer,
                              target_receipt_sha256=self.target['A'])
        return self._receipt(restored, manifest_sha256=manifest, target_sha256=target,
                             target_writes_closed=True, services=ALL_SERVICES)

    def verify_closed(self, plan, attempt, manifest, restored):
        self._assert_scope(plan, attempt)
        for node in ('A', 'B'):
            prepared = self._pass('prepare_runtime', node, manifest_sha256=manifest, restore_receipt_sha256=restored,
                                  transfer_receipt_sha256=self.transfer, target_receipt_sha256=self.target[node])
            self.closed[node] = self._pass('verify_closed', node, manifest_sha256=manifest,
                                           restore_receipt_sha256=restored, prepared_receipt_sha256=prepared)
        self.verified = self._join('verify_closed', **self.closed)
        return self._receipt(self.verified, manifest_sha256=manifest, restore_sha256=restored,
                             readiness=True, target_writes_closed=True, services=ALL_SERVICES)

    def assert_activation_closed(self, plan, attempt, manifest, restored, verified):
        self._assert_scope(plan, attempt)
        _need(verified == self.verified, 'guest_bridge_restore_receipt_unbound')
        proof = self._pass('assert_activation_closed', 'A', manifest_sha256=manifest, restore_receipt_sha256=restored,
                           closed_a_sha256=self.closed['A'], closed_b_sha256=self.closed['B'])
        return self._receipt(proof, manifest_sha256=manifest, restore_sha256=restored, verified_sha256=verified,
                             target_writes_closed=True, target_never_opened=True, restore_ready=True)

    def activate(self, plan, attempt, manifest, verified, settled, *, activation_check_sha256):
        self._assert_scope(plan, attempt)
        _need(verified == self.verified, 'guest_bridge_restore_receipt_unbound')
        result = self._call('activate', 'A', manifest_sha256=manifest, activation_check_sha256=activation_check_sha256,
                           settlement_sha256=settled)
        _need(result.status == 'PASS', 'guest_bridge_operation_unconfirmed')
        self.service_instance_sha256 = _pin(result.service_instance_sha256)
        self.guest_authority = _pin(result.receipt_sha256)
        self.activation_authority = self._join('activate', guest=self.guest_authority, instance=self.service_instance_sha256)
        return self._receipt(self.activation_authority, manifest_sha256=manifest, verified_sha256=verified, settlement_sha256=settled,
                             activation_check_sha256=activation_check_sha256, vm_write_authority=True)

    def assert_live(self, plan, attempt, authority, **references):
        self._assert_scope(plan, attempt)
        _need(self.service_instance_sha256 is not None and authority == self.activation_authority,
              'guest_bridge_authority_unbound')
        proof = self._pass('assert_live', 'A', authority_sha256=self.guest_authority, **references)
        return self._receipt(proof, authority_sha256=authority, ready=True, services=ALL_SERVICES, **references)

    def observe_rollback(self, plan, attempt, baseline, manifest, restore_attempted):
        self._assert_scope(plan, attempt)
        observed = {node: self._call('observe_rollback', node, baseline_sha256=baseline, manifest_sha256=manifest,
                                     restore_attempted=restore_attempted) for node in ('A', 'B')}
        a, b = observed['A'].status, observed['B'].status
        allowed = {('CLEAN', 'CLEAN'), ('CLEAN_FAILED', 'CLEAN'), ('COMMITTED_CLOSED', 'COMMITTED_CLOSED')}
        if (a, b) not in allowed:
            return {'status': 'UNKNOWN'}
        proof = self._join('observe_rollback', **{node: _pin(value.receipt_sha256) for node, value in observed.items()})
        return self._receipt(proof, status=a, baseline_sha256=baseline, manifest_sha256=manifest,
                             restore_attempted=restore_attempted, target_never_opened=True, target_writes_closed=True,
                             prior_mutations_settled=True)
