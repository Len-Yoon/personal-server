"""Coordinate one WSL-to-VM migration under an exclusive live host guard.

The native backend owns authenticated observations, operating-system mutations,
and the private capture and restore tools. A backend's guard must exclude other
operators and deployment/recovery actors through forward work and rollback.
This module never treats an ambiguous activation as permission to reopen WSL.
"""
import os
from pathlib import Path
import re
import secrets
import stat

from ha_runtime.config import SERVICES

from .controller import Journal, TransitionBlocked, exclusive


SHA = re.compile(r"[a-f0-9]{64}\Z")


def _pin(value):
    if type(value) is not str or SHA.fullmatch(value) is None:
        raise TransitionBlocked("legacy_cutover_receipt_rejected")
    return value


def _receipt(value, plan, attempt, **expected):
    fields = {"plan_sha256", "attempt_id", "evidence_sha256", *expected}
    if (type(value) is not dict or set(value) != fields
            or value["plan_sha256"] != plan or value["attempt_id"] != attempt
            or any(type(value[key]) is not type(item) or value[key] != item for key, item in expected.items())):
        raise TransitionBlocked("legacy_cutover_receipt_rejected")
    return _pin(value["evidence_sha256"])


def _private_directory(path):
    if os.name == 'nt' or not path.is_absolute() or ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise TransitionBlocked("legacy_cutover_private_journal_required")
    try:
        mode = path.stat()
    except OSError:
        raise TransitionBlocked("legacy_cutover_private_journal_required") from None
    if (not stat.S_ISDIR(mode.st_mode)
            or mode.st_uid != os.geteuid() or mode.st_mode & 0o077):
        raise TransitionBlocked("legacy_cutover_private_journal_required")


class LegacyCutover:
    """Execute one attempt. An existing journal requires external reconciliation.

    Backend methods return fresh, plan- and attempt-bound receipt summaries backed
    by private native evidence. `assert_rollback_safe` must obtain positive
    evidence of settled prior mutations, closed VM writes, and unchanged routing.
    `restore_legacy` must restore the exact preflight state and confirm health.
    """

    def __init__(self, journal_path, backend):
        self.journal = Journal(journal_path)
        self.backend = backend

    def run(self, plan_sha256):
        plan = _pin(plan_sha256)
        path = self.journal.path
        _private_directory(path.parent)
        with exclusive(path.with_suffix(".lock")):
            if os.path.lexists(path):
                raise TransitionBlocked("legacy_cutover_reconciliation_required")
            attempt = secrets.token_hex(32)
            try:
                with self.backend.guard(plan, attempt):
                    return self._execute(plan, attempt)
                raise TransitionBlocked('legacy_cutover_guard_or_journal_failed')
            except TransitionBlocked as error:
                if str(error) in {
                    'legacy_cutover_receipt_rejected', 'legacy_cutover_preflight_failed',
                    'legacy_cutover_rolled_back', 'legacy_cutover_forward_recovery_required',
                    'legacy_cutover_rollback_reconciliation_required',
                }:
                    raise
                raise TransitionBlocked('legacy_cutover_guard_or_journal_failed') from None
            except Exception:
                raise TransitionBlocked('legacy_cutover_guard_or_journal_failed') from None

    def _execute(self, plan, attempt):
        backend = self.backend
        state = {"version": 1, "plan_sha256": plan, "attempt_id": attempt, "phase": "preflight", "receipts": {}}
        self.journal.save(state)

        def phase(name):
            state["phase"] = name
            self.journal.save(state)

        def accept(name, value, **facts):
            pin = _receipt(value, plan, attempt, **facts)
            state["receipts"][name] = pin
            phase(name + "_complete")
            return pin

        try:
            pre = backend.preflight(plan)
        except Exception:
            raise TransitionBlocked('legacy_cutover_preflight_failed') from None
        baseline = accept("preflight", pre, capture_ready=True, restore_ready=True, ingress_ready=True,
                          legacy_live=True, target_writes_closed=True)
        phase("block_reactivation_intent")
        activated = False
        try:
            blocked = accept("block_reactivation", backend.block_reactivation(plan, baseline),
                             baseline_sha256=baseline, blocked=True)
            phase("fence_writers_intent")
            fence = accept("fence_writers", backend.fence_writers(plan, blocked),
                           reactivation_sha256=blocked, writers_closed=True, services=sorted(SERVICES))
            phase("capture_intent")
            capture = backend.capture(plan, fence)
            manifest = _pin(capture.get("manifest_sha256") if type(capture) is dict else None)
            accept("capture", capture, fence_sha256=fence, manifest_sha256=manifest)
            phase("restore_intent")
            restored = accept("restore", backend.restore(plan, manifest),
                              manifest_sha256=manifest, target_writes_closed=True, services=sorted(SERVICES))
            verified = accept("verify_closed", backend.verify_closed(plan, fence, restored),
                              fence_sha256=fence, restore_sha256=restored, readiness=True,
                              target_writes_closed=True, services=sorted(SERVICES))
            phase("activate_vm_intent")
            # Once activation has been attempted the old WSL writer may never
            # be reopened by this transaction, including on a lost response.
            activated = True
            authority = accept("activate_vm", backend.activate_vm(plan, verified),
                               verified_sha256=verified, vm_write_authority=True)
            phase("switch_caddy_intent")
            ingress = accept("switch_caddy", backend.switch_caddy(plan, authority),
                             authority_sha256=authority, vm_route_live=True)
            accept("verify_live", backend.verify_live(plan, authority, ingress),
                   authority_sha256=authority, ingress_sha256=ingress, services=sorted(SERVICES), ready=True)
            phase("complete")
        except Exception:
            if activated:
                phase("forward_recovery_required")
                raise TransitionBlocked("legacy_cutover_forward_recovery_required") from None
            self._rollback(plan, baseline, state, phase, accept)
            raise TransitionBlocked("legacy_cutover_rolled_back") from None
        return state

    def _rollback(self, plan, baseline, state, phase, accept):
        failure_phase = state["phase"]
        state["failure_phase"] = failure_phase
        try:
            phase("rollback_inspection_intent")
            safety = accept("rollback_inspection", self.backend.assert_rollback_safe(plan, baseline),
                            baseline_sha256=baseline, target_never_opened=True, target_writes_closed=True,
                            legacy_route_unchanged=True, prior_mutations_settled=True)
            phase("restore_legacy_intent")
            accept("restore_legacy", self.backend.restore_legacy(plan, baseline, safety),
                   baseline_sha256=baseline, safety_sha256=safety, legacy_state_restored=True, ready=True)
            phase("rolled_back")
        except Exception:
            phase("rollback_reconciliation_required")
            raise TransitionBlocked("legacy_cutover_rollback_reconciliation_required") from None
