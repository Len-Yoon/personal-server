"""Fail-closed HA admission contracts; deliberately not connected to production.

Observations must come from authenticated, fresh, narrowly privileged adapters.
This module neither gathers PostgreSQL evidence nor fences processes. A PASS is
only a policy decision over supplied evidence, never a promotion authorization.
Monotonic timestamps belong to one verifier process/boot and are not serialized.
"""

from dataclasses import dataclass
import math
import re


SERVICES = frozenset({
    "portal-web", "book-memo", "youtube-memo", "crawler-worker",
    "car-care-worker", "homeops-executor", "system-agent",
})
WEB_SERVICES = frozenset({"portal-web", "book-memo", "youtube-memo", "crawler-worker"})
MAX_PROOF_AGE_SECONDS = 2.0
MAX_AUTHORITY_LIFETIME_SECONDS = 5.0
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_LSN = re.compile(r"([0-9A-F]{1,8})/([0-9A-F]{1,8})\Z")


class AdmissionDenied(ValueError):
    """Retryable unavailability: no protected success or effect may be released."""


class RequestConflict(ValueError):
    """An existing request ID is bound to a different payload or result."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise AdmissionDenied(reason)


def _positive(value: int) -> bool:
    return type(value) is int and value > 0


def _text(value: str) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 128 and value.isascii() and all(
        c.isalnum() or c in "-_.:" for c in value
    )


def lsn_number(value: str) -> int:
    """Parse a PostgreSQL LSN without lexicographic ordering or float rounding."""
    match = _LSN.fullmatch(value) if isinstance(value, str) else None
    _require(match is not None, "invalid_lsn")
    return (int(match[1], 16) << 32) | int(match[2], 16)


@dataclass(frozen=True)
class Authority:
    cluster: str
    epoch: int
    revision: int
    primary: str
    partner: str
    timeline: int
    serving: frozenset[str]
    worker: str
    mode: str
    writes_open: bool
    # A generation-specific local deadline; never restored after verifier restart.
    issued_at: float
    deadline: float


@dataclass(frozen=True)
class RequestBinding:
    service: str
    request_id: str
    operation: str
    payload_sha256: str
    result_sha256: str
    target_node: str | None = None


@dataclass(frozen=True)
class DurabilityEvidence:
    authority_before: Authority
    authority_after: Authority
    # Fresh nonce binds both direct observations and the post-commit watermark
    # to this verification attempt. Historical ledger rows alone are insufficient.
    attempt: str
    primary_attempt: str
    partner_attempt: str
    binding: RequestBinding
    primary_binding: RequestBinding
    partner_binding: RequestBinding
    watermark: str
    primary_flush: str
    partner_flush: str
    partner_replay: str
    primary: str
    partner: str
    cluster: str
    epoch: int
    timeline: int
    partner_source_timeline: int
    primary_in_recovery: bool
    partner_in_recovery: bool
    sync_state: str
    fsync: bool
    full_page_writes: bool
    partner_fsync: bool
    partner_full_page_writes: bool
    synchronous_commit: str
    # Adapter must confirm the exact configured partner, not any healthy replica.
    configured_partner: str
    protection_history_safe: bool
    observed_from: float
    observed_until: float


def _validate_authority(authority: Authority, now: float) -> None:
    _require(type(now) in {int, float} and math.isfinite(now) and now >= 0, "invalid_clock")
    _require(_text(authority.cluster), "invalid_cluster")
    _require(all(_positive(n) for n in (authority.epoch, authority.revision, authority.timeline)), "invalid_generation")
    _require(authority.primary in {"A", "B"} and authority.partner in {"A", "B"}
             and authority.primary != authority.partner, "invalid_db_pair")
    _require(authority.worker in {"A", "B"}, "invalid_worker")
    _require(type(authority.serving) is frozenset and bool(authority.serving)
             and authority.serving <= {"A", "B"}, "invalid_serving")
    _require(authority.mode in {"AS", "AA"}, "invalid_mode")
    _require(authority.mode == "AA" or len(authority.serving) == 1, "invalid_as_owners")
    _require(type(authority.writes_open) is bool, "invalid_write_state")
    _require(all(type(value) in {int, float} and math.isfinite(value)
                 for value in (authority.issued_at, authority.deadline))
             and 0 <= authority.issued_at <= now < authority.deadline
             and authority.deadline - authority.issued_at <= MAX_AUTHORITY_LIFETIME_SECONDS,
             "authority_expired_or_invalid")


def _validate_binding(binding: RequestBinding) -> None:
    _require(binding.service in SERVICES and _text(binding.request_id)
             and _text(binding.operation), "invalid_request")
    _require(all(isinstance(value, str) and _HASH.fullmatch(value)
                 for value in (binding.payload_sha256, binding.result_sha256)), "invalid_binding_hash")
    if binding.service in {"homeops-executor", "system-agent"}:
        _require(binding.target_node in {"A", "B"}, "explicit_target_required")
    else:
        _require(binding.target_node is None, "unexpected_target")


def require_protected_result(
    authority: Authority, binding: RequestBinding, evidence: DurabilityEvidence,
    *, attempt: str, now: float,
) -> None:
    """Admit a response/retry only with a fresh same-generation two-copy proof.

    Called after an uncertain commit as well as an ordinary commit. It must run
    again for a retry or outbox dispatch; a previous admission is not a permit.
    The adapter generates a new unpredictable attempt for each verification;
    this stateless validator cannot detect reuse of a supplied attempt/proof.
    A current-epoch adapter may re-protect an old ledger row using a NEW barrier.
    It must capture watermark after reading the exact committed result, then
    observe partner flush/replay covering it and the same request binding.
    """
    _validate_authority(authority, now)
    _require(authority.writes_open is True, "writes_closed")
    _validate_binding(binding)
    _require(evidence.authority_before == authority == evidence.authority_after, "authority_changed")
    _require(_text(attempt) and attempt == evidence.attempt == evidence.primary_attempt
             == evidence.partner_attempt, "attempt_mismatch")
    _require(evidence.binding == binding, "proof_request_mismatch")
    for observed in (evidence.primary_binding, evidence.partner_binding):
        _validate_binding(observed)
        _require((observed.service, observed.request_id) == (binding.service, binding.request_id), "ledger_request_mismatch")
        if observed != binding:
            raise RequestConflict("request_binding_conflict")
    _require((evidence.primary, evidence.partner, evidence.cluster, evidence.epoch, evidence.timeline)
             == (authority.primary, authority.partner, authority.cluster, authority.epoch, authority.timeline), "database_identity_mismatch")
    _require(_positive(evidence.epoch) and _positive(evidence.timeline)
             and _positive(evidence.partner_source_timeline)
             and evidence.partner_source_timeline == authority.timeline, "timeline_mismatch")
    _require(evidence.primary_in_recovery is False and evidence.partner_in_recovery is True, "database_role_mismatch")
    _require(evidence.sync_state == "sync" and evidence.configured_partner == authority.partner, "partner_not_synchronous")
    _require(evidence.fsync is True and evidence.full_page_writes is True
             and evidence.partner_fsync is True and evidence.partner_full_page_writes is True
             and evidence.synchronous_commit in {"on", "remote_apply"}, "unsafe_durability_settings")
    _require(evidence.protection_history_safe is True, "unsafe_or_unknown_history")
    _require(all(type(value) in {int, float} and math.isfinite(value)
                 for value in (evidence.observed_from, evidence.observed_until)), "invalid_observation_clock")
    _require(authority.issued_at <= evidence.observed_from <= evidence.observed_until <= now
             < authority.deadline and now - evidence.observed_from <= MAX_PROOF_AGE_SECONDS,
             "invalid_observation_window")
    watermark = lsn_number(evidence.watermark)
    _require(watermark > 0, "empty_watermark")
    _require(all(lsn_number(value) >= watermark for value in (
        evidence.primary_flush, evidence.partner_flush, evidence.partner_replay,
    )), "watermark_not_protected")


def require_effect_owner(
    authority: Authority, binding: RequestBinding, evidence: DurabilityEvidence,
    *, node: str, attempt: str, now: float, target_node: str | None = None,
) -> None:
    """Outbox, collector, refresh, notification and host-control dispatch gate.

    This does not fence an already-running effect. The executor must retain its
    generation lock/permit and provider idempotency until completion; migrations
    and process-start guards must also be implemented before enabling HA.
    """
    require_protected_result(authority, binding, evidence, attempt=attempt, now=now)
    _require(node == authority.worker, "not_effect_owner")
    if binding.service in {"homeops-executor", "system-agent"}:
        _require(target_node in {"A", "B"} and target_node == binding.target_node,
                 "explicit_matching_target_required")
    else:
        _require(target_node is None, "unexpected_target")


def require_serving_owner(
    authority: Authority, *, service: str, node: str, now: float, target_node: str | None = None,
) -> None:
    """Check serving admission only; this grants neither DB writes nor effects."""
    _validate_authority(authority, now)
    _require(service in SERVICES and node in {"A", "B"}, "invalid_service_or_node")
    if service in {"homeops-executor", "system-agent"}:
        _require(target_node == node, "explicit_matching_target_required")
    elif service in WEB_SERVICES:
        _require(target_node is None, "unexpected_target")
        _require(node in authority.serving, "not_serving_owner")
    else:
        _require(target_node is None, "unexpected_target")
        _require(node == authority.worker, "not_effect_owner")
