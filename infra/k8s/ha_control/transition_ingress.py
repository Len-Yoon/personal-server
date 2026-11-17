"""Switch both public routes for one independently verified serving generation."""

from dataclasses import replace
from collections.abc import Mapping
import os
from pathlib import Path
from subprocess import TimeoutExpired

from ha_runtime.seven_capture import encoded, parse

from . import caddy_runtime as caddy
from . import native_stdio_bridge as bridge
from .native_guest_activation import ServingGeneration
from .native_host_evidence import _public_health
from .native_windows_activation import open_transition_controller, prepare_transition_observer_plan
from .transition_execution import observer_callback_config


INTENT = "ingress-switch-intent.json"
RECEIPT = "ingress-switch-receipt.json"


def _route_scope(previous, relay, generation, target, route_pin, plan_sha256, attempt_id):
    caddy._need(type(generation) is ServingGeneration, "transition_ingress_scope_invalid")
    generation.validate()
    caddy._need(generation.epoch > 1 and type(target) is str and target
                and previous.primary_ip == relay.guest and previous.primary_ip != target
                and relay.route_pin != route_pin
                and all(type(pin) is str and caddy.ID.fullmatch(pin) for pin in (route_pin, plan_sha256, attempt_id)),
                "transition_ingress_scope_invalid")


def _scope(previous, relay, prepared, directory, generation, target, route_pin, plan_sha256, attempt_id):
    _route_scope(previous, relay, generation, target, route_pin, plan_sha256, attempt_id)
    caddy._need(prepared.override == directory / "main" / "caddy-ha-compose.json"
                and prepared.candidate == directory / "main" / "Caddyfile", "transition_ingress_scope_invalid")
    return {"plan_sha256": plan_sha256, "attempt_id": attempt_id, "generation": generation.epoch,
            "timeline": generation.timeline, "revision": generation.revision, "primary": generation.primary,
            "target_sha256": caddy._sha(target.encode()), "old_caddy_sha256": previous.receipt["container_sha256"],
            "old_relay_sha256": relay.container_sha256, "caddy_candidate_sha256": prepared.candidate_sha256,
            "overlay_sha256": prepared.overlay_sha256, "mtls_volume_sha256": prepared.mtls_volume_sha256,
            "mtls_files_sha256": prepared.mtls_files_sha256,
            "car_route_sha256": route_pin}


def _live(generation, target, assert_guard, verify_guest):
    assert_guard()
    proof = verify_guest(generation, target)
    caddy._need(type(proof) is str and caddy.ID.fullmatch(proof), "transition_ingress_guest_unconfirmed")
    return proof


def _finish(previous, relay, scope, prepared, directory, generation, target, route_pin, main_receipt, car_receipt,
            assert_guard, verify_guest, verify_public):
    _live(generation, target, assert_guard, verify_guest)
    verify_public(3)
    guest = _live(generation, target, assert_guard, verify_guest)
    receipt = {"scope": scope, "guest_live_sha256": guest, "caddy": main_receipt, "relay": car_receipt}
    completed = directory / RECEIPT
    if completed.exists():
        prior = parse(caddy._read(completed))
        caddy._need(prior["scope"] == scope and prior["caddy"] == main_receipt
                    and prior["relay"]["container_sha256"] == car_receipt["container_sha256"]
                    and prior["relay"]["candidate_sha256"] == car_receipt["candidate_sha256"],
                    "transition_ingress_receipt_changed")
        receipt = prior
    else:
        caddy._write(completed, encoded(receipt), 0o600)
        caddy._sync_dir(directory)
    relay = replace(relay, container_sha256=car_receipt["container_sha256"], guest=target, route_pin=route_pin,
                    path=directory / "relay" / "Caddyfile")
    active = replace(previous, previous=prepared, receipt=main_receipt, primary_ip=target,
                     relay_container_sha256=relay.container_sha256)
    return active, relay, receipt


def switch_ingress(main, car, previous, relay, generation, target, route_pin, directory, *,
                   plan_sha256, attempt_id, expected_mtls_sha256, assert_guard, verify_guest, verify_public=_public_health):
    """Prepare Caddy, separate the relay from its Compose scope, then apply Caddy."""
    directory = caddy._private_dir(Path(directory))
    caddy._need(not os.path.lexists(directory / INTENT), "transition_ingress_reconciliation_required")
    _live(generation, target, assert_guard, verify_guest)
    prepared = main.prepare(previous.previous.baseline, target, directory / "main",
                            expected_mtls_sha256=expected_mtls_sha256, active=previous)
    scope = _scope(previous, relay, prepared, directory, generation, target, route_pin, plan_sha256, attempt_id)
    caddy._write(directory / INTENT, encoded(scope), 0o600)
    caddy._sync_dir(directory)
    _live(generation, target, assert_guard, verify_guest)
    car_receipt = car.replace(relay, target, route_pin, directory / "relay", assert_guard=assert_guard)
    _live(generation, target, assert_guard, verify_guest)
    main_receipt = main.apply(prepared, assert_guard)
    return _finish(previous, relay, scope, prepared, directory, generation, target, route_pin,
                   main_receipt, car_receipt, assert_guard, verify_guest, verify_public)


def observe_ingress(main, car, previous, relay, prepared, generation, target, route_pin, directory, *,
                    plan_sha256, attempt_id, assert_guard, verify_guest, verify_public=_public_health):
    """Confirm both replacements through readback after an unclear response."""
    directory = caddy._private_dir(Path(directory))
    scope = _scope(previous, relay, prepared, directory, generation, target, route_pin, plan_sha256, attempt_id)
    caddy._need(parse(caddy._read(directory / INTENT)) == scope, "transition_ingress_intent_unconfirmed")
    _live(generation, target, assert_guard, verify_guest)
    main_receipt = main.observe_applied(prepared, previous, assert_guard)
    car_receipt = car.observe(relay, directory / "relay", assert_guard=assert_guard)
    return _finish(previous, relay, scope, prepared, directory, generation, target, route_pin,
                   main_receipt, car_receipt, assert_guard, verify_guest, verify_public)


def resume_ingress(main, car, previous, relay, prepared, generation, target, route_pin, directory, *,
                   plan_sha256, attempt_id, assert_guard, assert_settled, verify_guest, verify_public=_public_health):
    """Finish a partial relay switch only after proving settlement and an unsent main change."""
    directory = caddy._private_dir(Path(directory))
    scope = _scope(previous, relay, prepared, directory, generation, target, route_pin, plan_sha256, attempt_id)
    caddy._need(parse(caddy._read(directory / INTENT)) == scope, "transition_ingress_intent_unconfirmed")
    _live(generation, target, assert_guard, verify_guest)
    assert_settled()
    if os.path.lexists(prepared.override.parent / "caddy-apply-intent.json"):
        return observe_ingress(main, car, previous, relay, prepared, generation, target, route_pin, directory,
                               plan_sha256=plan_sha256, attempt_id=attempt_id, assert_guard=assert_guard,
                               verify_guest=verify_guest, verify_public=verify_public)
    car_receipt = car.resume(relay, directory / "relay", assert_guard=assert_guard, assert_settled=assert_settled)
    _live(generation, target, assert_guard, verify_guest)
    main.adopt_unapplied(prepared, previous, target, assert_guard)
    main_receipt = main.apply(prepared, assert_guard)
    return _finish(previous, relay, scope, prepared, directory, generation, target, route_pin,
                   main_receipt, car_receipt, assert_guard, verify_guest, verify_public)


def _rejoin_replacement(execution, guests, generation, target_request, start, observe, check_stage, check_sync, deadline):
    """Start only once, then establish the exact guest and Windows replication evidence."""
    pin = (start.args.get("rejoin_stage_sha256")
           if type(start) is bridge.Request and isinstance(start.args, Mapping) else None)
    caddy._need(type(start) is bridge.Request and type(observe) is bridge.Request
                and start.verb == bridge.GUEST_START_REPLACEMENT
                and observe.verb == bridge.GUEST_OBSERVE_REPLACEMENT
                and start.binding == observe.binding == target_request.binding
                and start.node == observe.node == generation.partner
                and start.args == observe.args
                and type(start.sequence) is int and type(observe.sequence) is int
                and target_request.sequence < start.sequence < observe.sequence <= bridge.MAX_REQUESTS
                and bridge._hash(start.nonce) and bridge._hash(observe.nonce)
                and len({target_request.nonce, start.nonce, observe.nonce}) == 3 and bridge._hash(pin)
                and callable(check_stage) and callable(check_sync),
                "transition_replacement_scope_invalid")
    bridge._args(start.verb, start.node, dict(start.args))
    directory = caddy._private_dir(execution.directory)
    intent = directory / "replacement-start-intent.json"
    expected = {
        "plan_sha256": execution.spec["plan_sha256"], "attempt_id": execution.spec["attempt_id"],
        "generation": generation.epoch, "timeline": generation.timeline,
        "replacement_node": start.node, "replacement_boot_sha256": start.binding.boot(start.node),
        "bootstrap_manifest_sha256": start.args["bootstrap_manifest_sha256"], "rejoin_stage_sha256": pin,
    }
    caddy._need(check_stage(deadline) == pin, "transition_replacement_stage_unconfirmed")
    if os.path.lexists(intent):
        caddy._need(parse(caddy._read(intent)) == expected, "transition_replacement_intent_unconfirmed")
    else:
        caddy._write(intent, encoded(expected), 0o600)
        caddy._sync_dir(directory)
        try:
            guests.start_replacement_standby(start, deadline, assert_rejoin_current=check_stage)
        except (bridge.BridgeRejected, TimeoutExpired, OSError):
            # The guest may have started. Only observation may settle that outcome.
            pass
    receipt = guests.confirm_replacement_rejoin(
        observe, deadline, assert_rejoin_current=check_stage, check_synchronized=check_sync,
    )
    caddy._need(receipt.status == "PASS" and bridge._hash(receipt.receipt_sha256),
                "transition_replacement_replication_unconfirmed")


def switch_with_observer(execution, main, car, previous, relay, generation, target, route_pin, *,
                         previous_runtime_sha256, legacy_plan, legacy_attempt, expected_mtls_sha256,
                         previous_callback, previous_callback_sha256, replacement_vm, observer_release,
                         observer_setup, controller_setup, assert_legacy_closed, activate_guest,
                         guests, target_request, verify_guest,
                         replacement_start, replacement_observe, check_rejoin_stage, check_rejoin_sync,
                         verify_public=_public_health):
    """Rejoin standby, activate the guest, then switch ingress under one lock.

    After a lost guest response, supply fresh request sequences and nonces
    against the same binding. The recorded start intent suppresses replay.
    """
    _route_scope(previous, relay, generation, target, route_pin, execution.spec["plan_sha256"], execution.spec["attempt_id"])
    raw = observer_setup["plan_raw"]
    caddy._need(type(raw) is bytes and 0 < len(raw) <= 1048576 and caddy._sha(raw) == observer_setup["plan_sha256"],
                "transition_ingress_controller_unbound")
    template = parse(raw)
    caddy._need(type(template) is dict and template.get("generation") == generation.epoch
                and template.get("node") == generation.partner, "transition_ingress_controller_unbound")
    caddy._need((target_request.binding.plan_sha256, target_request.binding.attempt_id)
                == (execution.spec["plan_sha256"], execution.spec["attempt_id"])
                and guests.verify_transition_target(target_request, generation, target, observer_setup["deadline"]) is True,
                "transition_ingress_guest_target_unconfirmed")
    with execution.guard(main, car):
        _rejoin_replacement(execution, guests, generation, target_request, replacement_start, replacement_observe,
                            check_rejoin_stage, check_rejoin_sync, observer_setup["deadline"])
        binding = execution.publish_observer_runtime(previous_sha256=previous_runtime_sha256,
                                                     legacy_plan=legacy_plan, legacy_attempt=legacy_attempt,
                                                     assert_legacy_closed=assert_legacy_closed)
        callback = observer_callback_config(previous_callback, previous_callback_sha256, binding,
                                            generation.primary, replacement_vm, observer_release)
        controller = prepare_transition_observer_plan(**observer_setup, binding=binding | callback)
        plan = parse(controller["plan"])
        caddy._need(plan["generation"] == generation.epoch and plan["node"] == generation.partner,
                    "transition_ingress_controller_unbound")
        with open_transition_controller(controller, generation, attempt=execution.spec["attempt_id"],
                                        **controller_setup) as (assert_guard, guest_boundary):
            activate_guest(generation, target, guest_boundary)
            return switch_ingress(main, car, previous, relay, generation, target, route_pin, execution.directory,
                                  plan_sha256=execution.spec["plan_sha256"], attempt_id=execution.spec["attempt_id"],
                                  expected_mtls_sha256=expected_mtls_sha256, assert_guard=assert_guard,
                                  verify_guest=verify_guest, verify_public=verify_public)
