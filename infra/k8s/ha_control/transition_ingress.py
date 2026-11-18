"""Switch both public routes for one independently verified serving generation."""

from dataclasses import asdict, dataclass, replace
from collections.abc import Mapping
import os
import secrets
from pathlib import Path
from subprocess import TimeoutExpired

from ha_runtime.seven_capture import encoded, parse

from . import caddy_runtime as caddy
from . import native_stdio_bridge as bridge
from .native_guest_activation import ServingGeneration, generation_handoff_request, validate_generation_handoff_identity
from .native_host_evidence import _public_health
from .native_windows_ssh import fixed_script
from .native_windows_activation import (close_previous_transition_service, open_transition_controller, prepare_staged_transition_service,
                                        prepare_transition_observer_plan, stage_approved_transition_service,
                                        validate_transition_approval)
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


def _rejoin_replacement(execution, guests, generation, target_request, start, observe, installation_id,
                        check_stage, check_sync, deadline):
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
                and target_request.sequence < start.sequence - 1 < start.sequence < observe.sequence <= bridge.MAX_REQUESTS
                and bridge._hash(start.nonce) and bridge._hash(observe.nonce)
                and len({target_request.nonce, start.nonce, observe.nonce}) == 3 and bridge._hash(pin)
                and bridge._hash(installation_id) and callable(check_stage) and callable(check_sync),
                "transition_replacement_scope_invalid")
    bridge._args(start.verb, start.node, dict(start.args))
    directory = caddy._private_dir(execution.directory)
    intent = directory / "replacement-start-intent.json"
    expected = {
        "plan_sha256": execution.spec["plan_sha256"], "attempt_id": execution.spec["attempt_id"],
        "generation": generation.epoch, "timeline": generation.timeline,
        "replacement_node": start.node, "replacement_boot_sha256": start.binding.boot(start.node),
        "replacement_installation_id": installation_id,
        "bootstrap_manifest_sha256": start.args["bootstrap_manifest_sha256"], "rejoin_stage_sha256": pin,
    }
    caddy._need(check_stage(deadline) == pin, "transition_replacement_stage_unconfirmed")
    if os.path.lexists(intent):
        caddy._need(parse(caddy._read(intent)) == expected, "transition_replacement_intent_unconfirmed")
    else:
        publication = replace(start, verb=bridge.GUEST_PUBLISH_REPLACEMENT, sequence=start.sequence - 1,
                              nonce=secrets.token_hex(32), args=dict(start.args) | {"installation_id": installation_id})
        guests.publish_replacement_standby(publication, deadline, assert_rejoin_current=check_stage)
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


def _bind_rejoin_controller(execution, controller, parameters, *, resume):
    directory = caddy._private_dir(execution.directory)
    path = directory / "windows-rejoin-controller.json"
    caddy._need(type(controller) is dict and bridge._hash(controller.get("plan_sha256"))
                and type(parameters) is dict and parameters.get("PlanSha256") == controller["plan_sha256"],
                "transition_ingress_controller_unbound")
    proof = {"plan_sha256": execution.spec["plan_sha256"], "attempt_id": execution.spec["attempt_id"],
             "controller_sha256": controller["plan_sha256"], "parameters_sha256": caddy._sha(encoded(parameters))}
    if resume:
        caddy._need(parse(caddy._read(path)) == proof, "transition_ingress_controller_unbound")
    else:
        caddy._write(path, encoded(proof), 0o400)
        caddy._sync_dir(directory)


def _windows_rejoin(starter, controller, parameters, *, live_sha256, root, deadline, resume, stage_required=False):
    """Use one pinned plan through staging and every fresh read-only check."""
    def run(mode, request_id, limit):
        return starter.run_rejoin(controller, parameters, mode, request_id,
                                  live_sha256=live_sha256, root=root, deadline=limit)

    def observe(mode, limit):
        return run(mode, secrets.token_hex(16), limit)

    stage_id = secrets.token_hex(16)
    if resume and starter.rejoin_stage_intent_present(controller, parameters, stage_id, deadline):
        starter.restore_rejoin_args(controller, parameters, "stage-rejoin", stage_id, deadline)
        staged = observe("observe-rejoin-stage", deadline)
    else:
        caddy._need(not stage_required, "transition_replacement_network_unconfirmed")
        init_id = secrets.token_hex(16)
        if resume:
            starter.restore_rejoin_args(controller, parameters, "initialize", init_id, deadline)
        else:
            run("initialize", init_id, deadline)
        staged = run("stage-rejoin", stage_id, deadline)
    pin, wal = staged["rejoin_stage_sha256"], staged.get("source_fence_wal")
    error = "transition_source_fence_wal_unconfirmed"
    caddy._need(bridge._wal_pin(wal), error)

    def current_stage(limit):
        observed = observe("observe-rejoin-stage", limit)
        caddy._need(observed["rejoin_stage_sha256"] == pin and observed.get("source_fence_wal") == wal, error)
        return pin

    def synchronized(limit):
        observed = observe("check-rejoin", limit)
        caddy._need(observed["rejoin_stage_sha256"] == pin and observed.get("source_fence_wal") == wal, error)
        return observed

    return pin, int(wal, 16), current_stage, synchronized


def _prepare_target_generation(guests, binding, sequence, generation, pins, stage, replay_floor, deadline, *,
                               check_sync, assert_legacy_closed):
    source_wal = f"{replay_floor:016x}"
    request = bridge.Request(binding, generation.primary, bridge.GUEST_PREPARE_GENERATION, sequence,
                             secrets.token_hex(32), {
        "runtime": asdict(pins["pins"]), "postgres": asdict(pins["postgres"]), "generation": asdict(generation),
        "rejoin_stage_sha256": stage, "source_fence_wal": source_wal,
    })

    def current_rejoin(challenge, boundary, limit):
        caddy._need(bridge._hash(challenge) and boundary in bridge.PREPARATION_BOUNDARIES + bridge.PREPARATION_OBSERVATION_BOUNDARIES,
                    "transition_replacement_scope_invalid")
        assert_legacy_closed()
        observed = check_sync(limit)
        caddy._need(type(observed) is dict and observed.get("rejoin_stage_sha256") == stage
                    and observed.get("source_fence_wal") == source_wal and observed.get("standby_rejoined") is True
                    and observed.get("authority_opened") is False,
                    "transition_replacement_replication_unconfirmed")
        bridge._remaining(limit)
        return bridge._digest(bridge._bytes({
            "binding": binding.fields(), "generation": asdict(generation), "request_nonce": request.nonce,
            "challenge": challenge, "boundary": boundary, "observation": observed,
        }))

    try:
        pin = guests.prepare_generation_runtime(request, generation, stage, replay_floor, deadline, assert_rejoin=current_rejoin)
    except (bridge.BridgeRejected, TimeoutExpired, OSError):
        observation = replace(request, verb=bridge.GUEST_OBSERVE_PREPARATION, sequence=request.sequence + 1,
                              nonce=secrets.token_hex(32))
        pin = guests.observe_generation_preparation(observation, deadline, assert_rejoin=current_rejoin)
    caddy._need(bridge._hash(pin), "transition_ingress_activation_unconfirmed")
    return pins | {"activation": replace(pins["activation"], primary_runtime_receipt_sha256=pin)}


def _activate_or_observe_generation(guests, request, deadline, *, assert_guard, guest_boundary):
    try:
        result = guests.activate_generation(request, deadline, assert_windows_current=guest_boundary)
    except (bridge.BridgeRejected, TimeoutExpired, OSError):
        result = None
    if type(result) is bridge.Receipt and result.status == "PASS":
        return result
    # Lost reply may follow a committed activation. Never dispatch a second start.
    observe = replace(request, verb=bridge.GUEST_OBSERVE_GENERATION, sequence=request.sequence + 1,
                      nonce=secrets.token_hex(32))
    bridge._remaining(deadline)
    return guests.observe_generation_activation(observe, deadline, assert_guard=assert_guard)


@dataclass(frozen=True)
class CompletedTransition:
    active: object
    relay: object
    receipt: dict
    controller: dict
    parameters: dict
    generation: ServingGeneration
    observer_binding: dict | None = None
    activation_sha256: str | None = None


def switch_with_observer(execution, main, car, previous, relay, generation, target, route_pin, *,
                         previous_runtime_sha256, legacy_plan, legacy_attempt, expected_mtls_sha256,
                         previous_callback, previous_callback_sha256, replacement_vm, observer_release,
                         observer_setup, controller_setup, assert_legacy_closed,
                         guests, target_request,
                         replacement_manifest_sha256, generation_handoff_pins, stage_service,
                         isolated_replacement, attach_replacement_network, resume_rejoin=None,
                         prepare_guest_runtime=False, verify_public=_public_health):
    """Rejoin standby, activate the guest, then switch ingress under one lock.

    After a lost guest response, supply fresh request sequences and nonces
    against the same binding. The recorded start intent suppresses replay.
    Preflight uses isolated transport with the same guest boot and host key.
    The network adapter must verify or adopt an already completed attachment
    on resume, without connecting the fenced VM.
    """
    _route_scope(previous, relay, generation, target, route_pin, execution.spec["plan_sha256"], execution.spec["attempt_id"])
    raw = observer_setup["plan_raw"]
    caddy._need(type(raw) is bytes and 0 < len(raw) <= 1048576 and caddy._sha(raw) == observer_setup["plan_sha256"],
                "transition_ingress_controller_unbound")
    template = parse(raw)
    caddy._need(type(template) is dict and template.get("generation") == generation.epoch
                and template.get("node") == generation.partner and template.get("source_lease_close_required") is True, "transition_ingress_controller_unbound")
    caddy._need(bridge._hash(replacement_manifest_sha256) and callable(generation_handoff_pins)
                and type(isolated_replacement) is dict
                and {"endpoint", "known_hosts"} <= set(isolated_replacement)
                and set(isolated_replacement) <= {"endpoint", "known_hosts", "jump", "jump_known_hosts"}
                and callable(attach_replacement_network)
                and callable(getattr(attach_replacement_network, "preflight", None))
                and type(prepare_guest_runtime) is bool
                and (resume_rejoin is None or type(resume_rejoin) is dict
                     and set(resume_rejoin) == {"controller", "parameters"}),
                "transition_replacement_scope_invalid")
    caddy._need(guests.assert_serving_endpoints(template.get("pg_nodes")) is True,
                "transition_ingress_guest_endpoints_unconfirmed")
    pins = generation_handoff_pins(generation)
    caddy._need(type(pins) is dict and set(pins) == {"pins", "postgres", "activation"},
                "transition_ingress_controller_unbound")
    validate_generation_handoff_identity(
        target_request.binding, generation.primary, pins["pins"], pins["postgres"], pins["activation"],
        generation, pins["pins"].plan_sha256,
    )
    caddy._need((target_request.binding.plan_sha256, target_request.binding.attempt_id)
                == (execution.spec["plan_sha256"], execution.spec["attempt_id"])
                and guests.verify_prepared_transition_target(
                    target_request, generation, target, pins["pins"],
                    None if prepare_guest_runtime else pins["activation"].primary_runtime_receipt_sha256,
                    observer_setup["deadline"],
                ) is True, "transition_ingress_guest_target_unconfirmed")
    caddy._need(type(target_request.sequence) is int and 0 < target_request.sequence <= bridge.MAX_REQUESTS - 15
                and bridge._hash(replacement_vm.get("installation_id"))
                and replacement_vm.get("node") == generation.partner, "transition_replacement_scope_invalid")
    preflight_replacement = guests.isolated_replacement_preflight(generation.partner, **isolated_replacement)
    preflight = bridge.Request(
        target_request.binding, generation.partner, bridge.GUEST_PREFLIGHT_REPLACEMENT, target_request.sequence + 1,
        secrets.token_hex(32), {"bootstrap_manifest_sha256": replacement_manifest_sha256,
                               "installation_id": replacement_vm["installation_id"]},
    )
    stage_fields = {"approval", "close_previous_service", "preflight_path", "scm_verifier_path"}
    caddy._need(type(stage_service) is dict and set(stage_service) == stage_fields
                and callable(stage_service["approval"]) and callable(stage_service["close_previous_service"])
                and all(type(stage_service[key]) is str and stage_service[key] for key in
                        ("preflight_path", "scm_verifier_path"))
                and not {"prepare_service", "publish_handoff"}.intersection(controller_setup),
                "transition_ingress_controller_unbound")
    with execution.guard(main, car):
        observer_binding = None
        deadline = min(observer_setup["deadline"], controller_setup["deadline"])
        start_intent = execution.directory / "replacement-start-intent.json"
        network_intent = execution.directory / "replacement-network-intent.json"
        attachment_pending = resume_rejoin is not None and os.path.lexists(network_intent)
        if not attachment_pending and (resume_rejoin is None or not os.path.lexists(start_intent)):
            caddy._need(attach_replacement_network.preflight(
                replacement_vm, controller_setup["old_vm_id"], preflight, deadline,
            ) is True, "transition_replacement_network_unconfirmed")
            caddy._need(preflight_replacement(preflight, deadline) is True,
                        "transition_replacement_scope_invalid")
        if resume_rejoin is None:
            binding = execution.publish_observer_runtime(previous_sha256=previous_runtime_sha256,
                                                         legacy_plan=legacy_plan, legacy_attempt=legacy_attempt,
                                                         assert_legacy_closed=assert_legacy_closed)
            callback = observer_callback_config(previous_callback, previous_callback_sha256, binding,
                                                generation.primary, replacement_vm, observer_release)
            observer_binding = binding | callback
            controller = prepare_transition_observer_plan(**observer_setup, binding=observer_binding)
            approval = stage_service["approval"](controller, generation, deadline)
            validate_transition_approval(controller, approval)
            fixed_script("windows-ha-stage-initial-activation.ps1", observer_setup["stage_sha256"], {})
            previous_args_sha256 = stage_service["close_previous_service"](controller, generation, deadline)
            caddy._need(bridge._hash(previous_args_sha256), "transition_ingress_controller_unbound")
            staged = stage_approved_transition_service(
                controller_setup["starter"], controller, generation, approval, root=controller_setup["root"],
                attempt=execution.spec["attempt_id"], old_vm_id=controller_setup["old_vm_id"],
                stage_sha256=observer_setup["stage_sha256"], manifest_sha256=observer_setup["manifest_sha256"],
                previous_args_sha256=previous_args_sha256, preflight_path=stage_service["preflight_path"],
                scm_verifier_path=stage_service["scm_verifier_path"], deadline=deadline,
            )
            parameters = prepare_staged_transition_service(
                controller_setup["starter"], controller, generation, root=controller_setup["root"],
                attempt=execution.spec["attempt_id"], old_vm_id=controller_setup["old_vm_id"],
                approval_sha256=staged["ApprovalSha256"], preflight_path=staged["PreflightPath"],
                scm_verifier_path=staged["ScmVerifierPath"], deadline=deadline,
            )
            caddy._need(parameters == staged, "transition_ingress_controller_unbound")
        else:
            controller, parameters = resume_rejoin["controller"], resume_rejoin["parameters"]
            caddy._need(type(controller) is dict and set(controller) == {"plan", "plan_sha256"}
                        and type(controller["plan"]) is bytes and 0 < len(controller["plan"]) <= 1048576
                        and caddy._sha(controller["plan"]) == controller["plan_sha256"],
                        "transition_ingress_controller_unbound")
            plan = parse(controller["plan"])
            observer = plan.get("windows_transition_observer") if type(plan) is dict else None
            caddy._need(type(observer) is dict and observer.get("legacy_plan_sha256") == legacy_plan
                        and observer.get("legacy_attempt_id") == legacy_attempt
                        and encoded({key: value for key, value in plan.items() if key != "windows_transition_observer"})
                        == encoded(template),
                        "transition_ingress_controller_unbound")
            assert_legacy_closed()
        _bind_rejoin_controller(execution, controller, parameters, resume=resume_rejoin is not None)
        stage, replay_floor, check_stage, check_sync = _windows_rejoin(
            controller_setup["starter"], controller, parameters, live_sha256=controller_setup["live_sha256"],
            root=controller_setup["root"], deadline=deadline, resume=resume_rejoin is not None,
            stage_required=attachment_pending,
        )
        network_error = "transition_replacement_network_unconfirmed"
        caddy._need(check_stage(deadline) == stage, network_error)
        network_scope = encoded({
            "binding": preflight.binding.fields(), "replacement_vm": replacement_vm,
            "old_vm_id": controller_setup["old_vm_id"], "rejoin_stage_sha256": stage,
            "bootstrap_manifest_sha256": replacement_manifest_sha256,
        })
        if attachment_pending:
            caddy._need(caddy._read(network_intent) == network_scope, network_error)
        else:
            caddy._write(network_intent, network_scope, 0o400)
            caddy._sync_dir(execution.directory)
        caddy._need(attach_replacement_network(
            replacement_vm, controller_setup["old_vm_id"], preflight, stage, deadline, assert_fenced=check_stage,
        ) is True, network_error)
        caddy._need(check_stage(deadline) == stage, network_error)
        # Confirm the same boot-bound guest through the serving route before
        # publication. An already-started attempt uses pinned readback instead.
        if not os.path.lexists(start_intent):
            serving_preflight = replace(preflight, nonce=secrets.token_hex(32))
            caddy._need(guests.preflight_replacement_standby(serving_preflight, deadline) is True, network_error)
        args = {"bootstrap_manifest_sha256": replacement_manifest_sha256, "rejoin_stage_sha256": stage}
        replacement_start = replace(preflight, verb=bridge.GUEST_START_REPLACEMENT, sequence=preflight.sequence + 2,
                                    nonce=secrets.token_hex(32), args=args)
        replacement_observe = replace(replacement_start, verb=bridge.GUEST_OBSERVE_REPLACEMENT,
                                      sequence=replacement_start.sequence + 1, nonce=secrets.token_hex(32))
        _rejoin_replacement(execution, guests, generation, target_request, replacement_start, replacement_observe,
                            replacement_vm["installation_id"], check_stage, check_sync, deadline)

        publish_sequence = replacement_observe.sequence + 1
        if prepare_guest_runtime:
            caddy._need(publish_sequence <= bridge.MAX_REQUESTS - 10, "transition_ingress_controller_unbound")
            pins = _prepare_target_generation(
                guests, target_request.binding, publish_sequence, generation, pins, stage, replay_floor, deadline,
                check_sync=check_sync, assert_legacy_closed=assert_legacy_closed,
            )
            publish_sequence += 2  # Reserve a sequence for an uncertain preparation readback.

        def same_service(plan, serving_generation, limit):
            caddy._need(plan == controller and serving_generation == generation,
                        "transition_ingress_controller_unbound")
            bridge._remaining(limit)
            return parameters

        activation_request = None

        def publish_handoff(serving_generation, instance, limit):
            nonlocal activation_request
            request = generation_handoff_request(
                target_request.binding, publish_sequence, serving_generation,
                instance, replay_floor, **pins,
            )
            caddy._need(activation_request is None and type(request) is bridge.Request
                        and request.binding == target_request.binding
                        and type(request.sequence) is int and replacement_observe.sequence < request.sequence <= bridge.MAX_REQUESTS - 8,
                        "transition_ingress_controller_unbound")
            pin = guests.publish_generation_handoff(request, serving_generation, instance, replay_floor, limit)
            caddy._need(bridge._hash(pin) and pin == request.args["generation_handoff_sha256"],
                        "transition_ingress_controller_unbound")
            activation_request = bridge.Request(
                request.binding, request.node, bridge.GUEST_ACTIVATE_GENERATION, request.sequence + 1,
                secrets.token_hex(32), {"generation_handoff_sha256": pin,
                                       "effects_plan_sha256": request.args["effects_plan_sha256"]},
            )
            return pin

        with open_transition_controller(controller, generation, attempt=execution.spec["attempt_id"],
                                        **(controller_setup | {"prepare_service": same_service,
                                                               "publish_handoff": publish_handoff})) as (assert_guard, guest_boundary):
            caddy._need(activation_request is not None, "transition_ingress_activation_unbound")
            activated = _activate_or_observe_generation(
                guests, activation_request, deadline, assert_guard=assert_guard, guest_boundary=guest_boundary,
            )
            caddy._need(type(activated) is bridge.Receipt and activated.status == "PASS"
                        and bridge._hash(activated.receipt_sha256), "transition_ingress_activation_unconfirmed")
            sequence = activation_request.sequence + 1

            def verify_guest(serving_generation, address):
                nonlocal sequence
                caddy._need(serving_generation == generation and address == target,
                            "transition_ingress_guest_unconfirmed")
                sequence += 1  # Reserve the prior sequence for an activation readback.
                request = replace(
                    activation_request, verb=bridge.GUEST_VERIFY_GENERATION, sequence=sequence,
                    nonce=secrets.token_hex(32), args=activation_request.args | {
                        "activation_receipt_sha256": activated.receipt_sha256,
                        "target_sha256": bridge._digest(address.encode("ascii")),
                    },
                )
                return guests.verify_live_generation(request, generation, address, deadline, assert_guard=assert_guard)

            active, next_relay, receipt = switch_ingress(main, car, previous, relay, generation, target, route_pin, execution.directory,
                                  plan_sha256=execution.spec["plan_sha256"], attempt_id=execution.spec["attempt_id"],
                                  expected_mtls_sha256=expected_mtls_sha256, assert_guard=assert_guard,
                                  verify_guest=verify_guest, verify_public=verify_public)
            return CompletedTransition(active, next_relay, receipt, controller, parameters, generation,
                                       observer_binding, activated.receipt_sha256)


def switch_roundtrip(main, car, previous, relay, *, failover, prepare_return):
    """Run A→B, then derive B→A from the verified serving generation."""
    error = "transition_roundtrip_unbound"
    caddy._need(type(failover) is dict and callable(prepare_return)
                and type(failover.get("generation")) is ServingGeneration
                and failover["generation"].primary == "B" and failover.get("resume_rejoin") is None,
                error)
    failover["generation"].validate()
    caddy._need(type(main) is caddy.CaddyRuntime and main.execution is None and not main.attempted, error)
    return_main = caddy.CaddyRuntime(main.root, main.project, run=main.run)
    first = switch_with_observer(main=main, car=car, previous=previous, relay=relay, **failover)
    caddy._need(type(first) is CompletedTransition and first.generation == failover["generation"]
                and type(first.observer_binding) is dict, error)
    returned = prepare_return(first)
    prior = failover["controller_setup"]
    old_starter = prior["starter"]
    caddy._need(type(returned) is dict and type(returned.get("generation")) is ServingGeneration
                and type(returned.get("stage_service")) is dict
                and "close_previous_service" not in returned["stage_service"]
                and set(returned["stage_service"]) == {"approval", "preflight_path", "scm_verifier_path"}
                and type(returned.get("controller_setup")) is dict and "starter" in returned["controller_setup"]
                and returned.get("resume_rejoin") is None,
                error)
    new_starter = returned["controller_setup"]["starter"]
    caddy._need(new_starter is not old_starter and new_starter.windows is old_starter.windows
                and new_starter.start_sha256 == old_starter.start_sha256 and new_starter.attempted is False,
                error)
    generation = returned["generation"]
    generation.validate()
    caddy._need(generation.primary == "A" and generation.epoch == first.generation.epoch + 1
                and generation.timeline == first.generation.timeline + 1
                and returned["execution"].spec["attempt_id"] != failover["execution"].spec["attempt_id"]
                and returned["target"] != failover["target"]
                and all(returned[key] == failover[key] for key in
                        ("previous_runtime_sha256", "legacy_plan", "legacy_attempt"))
                and returned["previous_callback"] == first.observer_binding["callback_config"]
                and returned["previous_callback_sha256"] == first.observer_binding["callback_config_sha256"],
                error)

    def close_previous(controller, successor, deadline):
        return close_previous_transition_service(
            prior["starter"], first.controller, first.parameters, first.generation, controller, successor,
            previous_root=prior["root"], previous_attempt=failover["execution"].spec["attempt_id"],
            previous_old_vm_id=prior["old_vm_id"], live_sha256=prior["live_sha256"], deadline=deadline,
        )

    returned = returned | {"stage_service": returned["stage_service"] | {"close_previous_service": close_previous}}
    second = switch_with_observer(main=return_main, car=car, previous=first.active, relay=first.relay, **returned)
    caddy._need(type(second) is CompletedTransition and second.generation == generation
                and type(second.observer_binding) is dict, error)
    return first, second
