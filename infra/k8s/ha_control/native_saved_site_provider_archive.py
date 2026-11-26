"""Create/read one rollback archive from measured, scoped guest observers.

Authenticated transport and the fresh native Windows closed-pair query belong
to the caller. No transport, provider stop/start, or Windows mutation is exposed.
Raw argv and fragments stay inside the root-only archive, never in responses.
"""

import json
import os
from pathlib import Path
import secrets
import time

from . import initial_app_resume as resume
from . import native_guest_runtime as runtime, native_guest_worker as worker
from . import native_legacy_provider as provider, native_stdio_bridge as wire
from .controller import TransitionBlocked
from .initial_unpublished import derive_predecessor

ERROR = 'saved_provider_archive_unconfirmed'


def _need(value):
    if not value:
        raise TransitionBlocked(ERROR)


def _expected(value):
    _need(type(value) is dict and set(value) == {'pid', 'fragment_sha256', 'argv_sha256'}
          and type(value['pid']) is int and value['pid'] > 1
          and all(wire._hash(value[key]) for key in ('fragment_sha256', 'argv_sha256')))
    return dict(value)


class ProviderRollbackArchive:
    """Archive the current provider and preserved original positional held unit.

    The completed predecessor history and native terminal copies must already
    exist. Exact running/stopped application state is an explicit input, never
    changed here. Preflight can archive while the enrolled apps remain running.
    Existing
    archives are never rewritten; an uncertain create is reconciled by observe.
    The independently measured expected identities are mandatory inputs.
    """

    def __init__(self, *, previous_request, history, predecessor_terminal_sha256,
                 previous_instance_sha256, windows_boot_sha256, previous_revision,
                 operation_nonce, hold_sha256, provider_unit, held_unit,
                 provider_port, held_port, expected_provider, expected_held,
                 expected_apps_running):
        _need(type(previous_request) is wire.Request and previous_request.node == 'A'
              and previous_request.verb == wire.GUEST_START_INITIAL_POSTBOOT)
        wire._args(previous_request.verb, previous_request.node, previous_request.args)
        previous_request.binding.fields()
        _need(all(wire._hash(pin) for pin in (predecessor_terminal_sha256, previous_instance_sha256,
              windows_boot_sha256, operation_nonce, hold_sha256))
              and type(previous_revision) is int and 8 <= previous_revision <= 2147483644
              and previous_revision % 2 == 0 and previous_request.args['hold_sha256'] == hold_sha256
              and type(expected_apps_running) is bool)
        _need(type(history) is dict and set(history) == {
            'authorization_sha256', 'intent_sha256', 'progress_sha256', 'done_sha256'}
            and all(wire._hash(history[key]) for key in ('authorization_sha256', 'intent_sha256', 'done_sha256'))
            and type(history['progress_sha256']) is dict
            and set(history['progress_sha256']) == {'progress-' + role + '.json' for role in wire.RECOVERY_START_ORDER}
            and all(wire._hash(pin) for pin in history['progress_sha256'].values()))
        _need(type(provider_unit) is str and type(held_unit) is str
              and provider.UNIT.fullmatch(provider_unit) and provider.UNIT.fullmatch(held_unit)
              and provider_unit.startswith('n100ha-recovery-observer-')
              and held_unit.startswith('n100ha-postboot-observer-') and provider_unit != held_unit)
        self.previous = previous_request
        self.history = json.loads(wire._bytes(history))
        self.history_pin = wire._digest(wire._bytes(self.history))
        self.terminal_pin, self.instance_pin = predecessor_terminal_sha256, previous_instance_sha256
        self.windows_boot, self.revision, self.nonce = windows_boot_sha256, previous_revision, operation_nonce
        self.apps_running = expected_apps_running
        self.stage = worker.ROOT / previous_request.binding.attempt_id
        self.path = self.stage / ('provider-rollback-' + history['intent_sha256'] + '.json')
        self.tool = provider.ProviderRetirement(self.stage, previous_request.binding,
            latest_intent_sha256=history['intent_sha256'], rollback_sha256='0' * 64,
            hold_sha256=hold_sha256, provider_port=provider_port, held_port=held_port)
        self.units = {'provider': provider_unit, 'held': held_unit}
        self.expected = {'provider': _expected(expected_provider), 'held': _expected(expected_held)}
        _need(self.expected['provider']['pid'] != self.expected['held']['pid'])

    def _context(self):
        _need(os.geteuid() == 0
              and wire._digest(runtime.BOOT_ID.read_bytes()) == self.previous.binding.boot_a_sha256)
        runtime._directory(self.stage, 0)
        plan_pin = self.previous.args['postboot_plan_sha256']
        terminal = runtime._read(worker.ROOT / ('predecessor-plan-' + plan_pin + '.json'),
                                 0, {0o400}, limit=65536)
        _need(wire._digest(terminal) == self.terminal_pin
              and runtime._read(worker.ROOT / ('predecessor-terminal-' + self.terminal_pin + '.json'),
                                0, {0o400}, limit=65536) == terminal)
        prior = resume.decode(terminal)
        _need(derive_predecessor(terminal, self.terminal_pin, published=True)[0] == self.revision + 1
              and prior['revision'] == self.revision and prior['controller_plan_sha256'] == plan_pin
              and prior['postboot'] == self.previous.args['postboot']
              and prior['attempt_id'] == self.previous.binding.attempt_id
              and prior['legacy_plan_sha256'] == self.previous.binding.plan_sha256)
        raw, hold, prepared, active = worker._read_initial_recovery_hold(self.previous)
        _need(wire._digest(raw) == self.tool.hold)
        auth = resume._latest_history(self.previous, raw, hold, active, self.history)
        _need(auth['service_instance_sha256'] == self.instance_pin)
        worker._assert_car_route_blocked(self.previous, raw, prepared, active)
        pin = self.previous.args['protected_inventory_sha256']
        inventory, _ = worker._initial_recovery_inventory(self.previous.binding, hold['containers'], pin,
            None, (auth['container_contracts'], pin))
        _need(set(inventory) == set(wire.RECOVERY_START_ORDER)
              and all(row['running'] is self.apps_running for row in inventory.values()))
        if self.apps_running:
            # Completed-run reconciliation uses the actual running rows. The
            # authorization records the stopped baseline before that run;
            # every enrolled role must have started since that exact baseline.
            # _run compares the full measured rows across all proof boundaries,
            # so a subsequent restart or start-time drift is also rejected.
            baseline = auth['container_contracts']
            _need(type(baseline) is dict and set(baseline) == set(inventory))
            for role, current in inventory.items():
                original = baseline[role]
                _need(type(current) is dict and type(original) is dict
                      and all(set(row) == {'running', 'layout_sha256', 'started_at'}
                              and wire._hash(row['layout_sha256'])
                              and type(row['started_at']) is str and row['started_at']
                              for row in (current, original))
                      and current['running'] is True and original['running'] is False
                      and current['layout_sha256'] == original['layout_sha256']
                      and current['started_at'] != original['started_at'])
        else:
            resume._confirm_progress_drift(inventory, auth['container_contracts'], len(self.history['progress_sha256']))
        return wire._digest(wire._bytes(inventory))

    def _capture(self):
        entries, facts = {}, {}
        for role, unit in self.units.items():
            expected = self.expected[role]
            proc = Path('/proc') / str(expected['pid'])
            start = (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]
            _need(start.isdecimal())
            argv = (proc / 'cmdline').read_bytes()
            _need(0 < len(argv) <= 131072 and wire._digest(argv) == expected['argv_sha256'])
            fragment = runtime._read(provider.TRANSIENT / unit, 0, {0o644, 0o640, 0o600}, limit=131072)
            _need(wire._digest(fragment) == expected['fragment_sha256'])
            provider._profile(fragment)
            provider._argv(argv, role=role, port=self.tool.ports[role],
                           hold_sha256=self.tool.hold, boot_sha256=self.previous.binding.boot_a_sha256)
            entry = {'unit': unit, 'fragment': fragment.hex(), 'argv': argv.hex(),
                     'fragment_sha256': expected['fragment_sha256'], 'argv_sha256': expected['argv_sha256'],
                     'scope': 'exact-original-observer-profile'}
            _need(self.tool._live(entry, role) == expected
                  and (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19] == start)
            entries[role], facts[role] = entry, expected | {'start_ticks': start}
        document = {'scope': self.previous.binding.fields(), 'latest_intent': self.history['intent_sha256'],
                    'units': entries}
        raw = wire._bytes(document)
        _need(0 < len(raw) <= 262144)
        return raw, facts

    def _gate(self, boundary, assert_closed, deadline):
        _need(callable(assert_closed) and time.monotonic() < deadline)
        challenge = wire._digest(wire._bytes({'binding': self.previous.binding.fields(),
            'history_sha256': self.history_pin, 'terminal_sha256': self.terminal_pin,
            'previous_instance_sha256': self.instance_pin, 'windows_boot_sha256': self.windows_boot,
            'expected_apps_running': self.apps_running,
            'closed_revision': self.revision + 1, 'operation_nonce': self.nonce,
            'boundary': boundary, 'nonce': secrets.token_hex(32)}))
        result = assert_closed(challenge, boundary, deadline)
        _need(type(result) is dict and set(result) == {
            'closed_nodes', 'synchronous', 'same_expiry', 'replay_verified', 'effects_isolated',
            'revision', 'terminal_sha256', 'closed_proof_sha256', 'challenge', 'windows_boot_sha256'}
            and result['challenge'] == challenge and result['windows_boot_sha256'] == self.windows_boot
            and type(result['revision']) is int and result['revision'] == self.revision + 1
            and result['terminal_sha256'] == self.terminal_pin and result['closed_nodes'] == ['A', 'B']
            and all(result[key] is True for key in ('synchronous', 'same_expiry', 'replay_verified', 'effects_isolated'))
            and wire._hash(result['closed_proof_sha256']) and time.monotonic() < deadline)
        return result['closed_proof_sha256']

    def _run(self, assert_closed, deadline, *, create, expected_sha256=None, measure=False):
        _need(type(create) is bool and type(measure) is bool and not (create and measure)
              and ((measure and expected_sha256 is None) or (not measure and wire._hash(expected_sha256))))
        with worker._guest_mutation_descriptor(wait=2):
            if measure:
                _need(not os.path.lexists(self.path))
            context = self._context()
            raw, facts = self._capture()
            pin = wire._digest(raw)
            _need(expected_sha256 is None or pin == expected_sha256)
            before = self._gate('before', assert_closed, deadline)
            _need(self._context() == context and self._capture() == (raw, facts))
            if create:
                _need(not os.path.lexists(self.path))
                self._gate('dispatch', assert_closed, deadline)
                _need(self._context() == context and self._capture() == (raw, facts)
                      and time.monotonic() < deadline)
                runtime._write(self.path, raw, mode=0o400)
            if measure:
                _need(not os.path.lexists(self.path))
            else:
                _need(runtime._read(self.path, 0, {0o400}, limit=262144) == raw)
            after = self._gate('after', assert_closed, deadline)
            _need(self._context() == context and self._capture() == (raw, facts))
            if measure:
                _need(not os.path.lexists(self.path))
                return {'archive_sha256': pin}
            _need(runtime._read(self.path, 0, {0o400}, limit=262144) == raw)
            return {'archive_sha256': pin, 'history_sha256': self.history_pin,
                    'inventory_sha256': context, 'apps_running': self.apps_running,
                    'latest_intent_sha256': self.history['intent_sha256'],
                    'terminal_sha256': self.terminal_pin, 'closed_revision': self.revision + 1,
                    'observers': facts, 'proofs': {'before': before, 'after': after},
                    'created': create, 'provider_mutated': False}

    def measure(self, assert_closed, deadline):
        """Measure absent archive bytes under authenticated before/after proofs.

        Returns only their SHA; caller must durably retain it before create.
        Existing files require explicit observe with that previously retained pin.
        """
        return self._run(assert_closed, deadline, create=False, measure=True)

    def create(self, assert_closed, deadline, *, expected_sha256):
        """Create once, requiring the exact SHA retained from measure."""
        _need(wire._hash(expected_sha256))
        return self._run(assert_closed, deadline, create=True, expected_sha256=expected_sha256)

    def observe(self, assert_closed, deadline, *, expected_sha256):
        """Read-only reconciliation; missing or changed bytes fail closed."""
        _need(wire._hash(expected_sha256))
        return self._run(assert_closed, deadline, create=False, expected_sha256=expected_sha256)
