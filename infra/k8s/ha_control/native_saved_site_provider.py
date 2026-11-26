"""Scoped guest provider replacement, reusing the archived native retirement.

Run only inside the authenticated guest worker. The coordinator supplies fresh
Windows closed-pair/no-successor proofs at every boundary; provider_ready must
also exercise the real native provider callback. This module supplies neither
transport nor a replacement proof. Import performs no host or guest operation.
"""

import json
import os
from pathlib import Path
import re
import secrets
import time

from . import native_guest_runtime as runtime, native_legacy_provider as native
from . import native_stdio_bridge as wire
from .controller import TransitionBlocked

ERROR = 'saved_site_provider_unconfirmed'
UNSETTLED = 'saved_site_provider_reconciliation_required'


def _need(value, error=ERROR):
    if not value:
        raise TransitionBlocked(error)


def _observation(value):
    _need(type(value) is dict and set(value) == {'pid', 'fragment_sha256', 'argv_sha256'}
          and type(value['pid']) is int and value['pid'] > 1
          and all(wire._hash(value[key]) for key in ('fragment_sha256', 'argv_sha256')))
    return dict(value)


class ProviderReplacement:
    """One nonce-named provider, one durable dispatch, explicit reconciliation.

    retirement must archive the *currently enrolled* provider, not a historical
    provider with the same port. expected_provider is its independently observed
    PID/fragment/argv triple before retirement; the retirement intent must match.
    Held ownership stays exact throughout. Rollback never stops another unit.
    """

    def __init__(self, retirement, *, operation_nonce, source_path, source_sha256,
                 retirement_sha256, original_unit, expected_provider, expected_held):
        _need(type(retirement) is native.ProviderRetirement and wire._hash(operation_nonce)
              and wire._hash(source_sha256) and wire._hash(retirement_sha256)
              and type(original_unit) is str and native.UNIT.fullmatch(original_unit))
        source = Path(source_path)
        _need(source.is_absolute() and '..' not in source.parts
              and source.is_relative_to(native.GUEST_ROOT)
              and source.name == 'native-guest-recovery-observer.py')
        self.retirement, self.nonce = retirement, operation_nonce
        self.source, self.source_pin = source, source_sha256
        self.retirement_pin, self.original_unit = retirement_sha256, original_unit
        self.original = _observation(expected_provider)
        self.held = _observation(expected_held)
        self.unit = 'n100ha-recovery-observer-saved-' + operation_nonce[:24] + '.service'
        self.intent = retirement.stage / ('provider-successor-' + operation_nonce + '-intent.json')
        self.done = retirement.stage / ('provider-successor-' + operation_nonce + '-done.json')
        self.stop_intent = retirement.stage / ('provider-successor-' + operation_nonce + '-stop-issued.json')

    def _material(self):
        adapter = self.retirement
        units = adapter._archive()  # root, protected archive, exact guest boot
        _need(units['provider']['unit'] == self.original_unit
              and self.unit not in {units['provider']['unit'], units['held']['unit']}
              and units['provider']['fragment_sha256'] == self.original['fragment_sha256']
              and units['provider']['argv_sha256'] == self.original['argv_sha256'])
        _need(adapter._receipt('retire') == self.retirement_pin)
        retired = json.loads(runtime._read(adapter._paths('retire')[0], 0, {0o400}, limit=131072))
        _need(retired.get('initial') == self.original and retired.get('held') == self.held)
        _need(wire._digest(runtime._read(self.source, 0, {0o400}, limit=131072)) == self.source_pin)
        argv = native._argv(bytes.fromhex(units['provider']['argv']), role='provider',
                            port=adapter.ports['provider'], hold_sha256=adapter.hold,
                            boot_sha256=adapter.binding.boot_a_sha256)
        replacement = list(argv)
        replacement[6:8] = [str(self.source), self.source_pin]
        raw = ('\0'.join(replacement) + '\0').encode()
        native._argv(raw, role='provider', port=adapter.ports['provider'],
                     hold_sha256=adapter.hold, boot_sha256=adapter.binding.boot_a_sha256)
        entry = dict(units['provider'], unit=self.unit, argv=raw.hex(), argv_sha256=wire._digest(raw))
        expected = {'schema_version': 1, 'binding': adapter.binding.fields(),
                    'operation_nonce_sha256': wire._digest(self.nonce.encode()),
                    'rollback_sha256': adapter.rollback, 'retirement_sha256': self.retirement_pin,
                    'original_unit': self.original_unit, 'original': self.original, 'held': self.held,
                    'unit': self.unit, 'source_sha256': self.source_pin,
                    'argv_sha256': entry['argv_sha256'], 'automatic_retry_permitted': False}
        return units, entry, replacement, expected

    def _held(self, units):
        _need(self.retirement._live(units['held'], 'held') == self.held)

    def _old_absent(self, units):
        props = native._properties(self.original_unit)
        _need(props['Id'] == self.original_unit and props['MainPID'] == '0'
              and props['ActiveState'] == 'inactive' and props['SubState'] == 'dead')
        process = Path('/proc') / str(self.original['pid'])
        if process.exists():
            _need((process / 'cmdline').read_bytes() != bytes.fromhex(units['provider']['argv']))

    def _own_absent(self, *, port_free=True):
        props = native._properties(self.unit)
        _need(props['Id'] == self.unit and props['MainPID'] == '0'
              and (props['ActiveState'], props['SubState']) in {('inactive', 'dead'), ('failed', 'failed')})
        if port_free:
            _need(self.retirement.ports['provider'] not in native._listeners())

    def _gate(self, mode, boundary, assert_safe, deadline):
        _need(callable(assert_safe) and time.monotonic() < deadline)
        self._material()
        frame = {'binding': self.retirement.binding.fields(), 'operation_nonce': self.nonce,
                 'rollback_sha256': self.retirement.rollback, 'retirement_sha256': self.retirement_pin,
                 'unit': self.unit, 'mode': mode, 'boundary': boundary,
                 'nonce': secrets.token_hex(32)}
        challenge = wire._digest(wire._bytes(frame))
        proof = assert_safe(challenge, boundary, deadline)
        _need(wire._hash(proof) and time.monotonic() < deadline)
        self._material()
        return proof

    def _intent(self, expected):
        _need(json.loads(runtime._read(self.intent, 0, {0o400}, limit=131072)) == expected)

    def _launch_pin(self, expected):
        if os.path.lexists(self.intent):
            self._intent(expected)
            return wire._digest(wire._bytes(expected))
        _need(not os.path.lexists(self.done))
        return None

    def _own_live(self, entry, *, ready):
        if ready:
            return self.retirement._live(entry, 'provider', original=False)
        # An exact launched process may have failed before binding its socket.
        # It is eligible for stop only after all other custody checks pass.
        props = native._properties(self.unit)
        _need(props['Id'] == self.unit and props['ActiveState'] == 'active'
              and props['SubState'] == 'running' and props['Transient'] == 'yes'
              and props['FragmentPath'] == str(native.TRANSIENT / self.unit)
              and all(props[key] == '' for key in native.EMPTY)
              and all(props[key] == value for key, value in {
                  'User': 'root', 'Restart': 'no', 'NoNewPrivileges': 'yes', 'PrivateTmp': 'yes'}.items()))
        pid = int(props['MainPID'])
        _need(pid > 1)
        proc = Path('/proc') / str(pid)
        _need(proc.stat().st_uid == 0 and (proc / 'cmdline').read_bytes() == bytes.fromhex(entry['argv'])
              and (proc / 'exe').resolve() == Path('/usr/bin/python3').resolve())
        fragment = runtime._read(native.TRANSIENT / self.unit, 0, {0o644, 0o640, 0o600}, limit=131072)
        native._profile(fragment)
        sockets = native._listeners()
        rows = sockets.get(self.retirement.ports['provider'], [])
        _need(not rows or (len(rows) == 1 and re.findall(r'pid=([0-9]+),', rows[0]) == [str(pid)]))
        _need(sum(bool(re.search(r'pid=' + str(pid) + r',', row))
                  for values in sockets.values() for row in values) == len(rows))
        _need(native._properties(self.unit) == props)
        return {'pid': pid, 'fragment_sha256': wire._digest(fragment), 'argv_sha256': entry['argv_sha256']}

    def _stop_record(self, expected):
        record = json.loads(runtime._read(self.stop_intent, 0, {0o400}, limit=131072))
        _need(type(record) is dict and set(record) == set(expected) | {
            'operation', 'provider', 'safe_proof_sha256', 'launch_intent_sha256'}
            and all(record.get(key) == value for key, value in expected.items())
            and record['operation'] == 'stop_replacement' and wire._hash(record['safe_proof_sha256'])
            and record['launch_intent_sha256'] == self._launch_pin(expected))
        if record['provider'] is not None:
            _observation(record['provider'])
            _need(record['launch_intent_sha256'] is not None
                  and record['provider']['argv_sha256'] == expected['argv_sha256'])
        return record

    def _restore_intent(self):
        path, done = self.retirement._paths('restore')
        if not os.path.lexists(path):
            _need(not os.path.lexists(done))
            return None
        raw = runtime._read(path, 0, {0o400}, limit=131072)
        record = json.loads(raw)
        expected = {'scope': self.retirement.scope, 'latest_intent': self.retirement.intent,
                    'rollback_sha256': self.retirement.rollback, 'hold_sha256': self.retirement.hold,
                    'operation': 'restore', 'boundary': 'before', 'initial': None,
                    'held': self.held, 'automatic_retry_permitted': False}
        _need(type(record) is dict and set(record) == set(expected) | {
            'safe_challenge_sha256', 'safe_proof_sha256', 'dispatch_proof_sha256'}
            and all(record.get(key) == value for key, value in expected.items())
            and all(wire._hash(record[key]) for key in (
                'safe_challenge_sha256', 'safe_proof_sha256', 'dispatch_proof_sha256')))
        return wire._digest(raw)

    def _restore_state(self, units):
        """Permit old live ownership only after the exact native restore intent."""
        restore_pin = self._restore_intent()
        self._own_absent(port_free=False)
        self._held(units)
        props = native._properties(self.original_unit)
        if props['ActiveState'] == 'active':
            _need(restore_pin is not None)
            current = self.retirement._live(units['provider'], 'provider', original=False)
            _need(self._restore_intent() == restore_pin)
            return restore_pin, current
        self._old_absent(units)
        self._own_absent()
        return restore_pin, None

    def start(self, assert_safe, deadline):
        """Dispatch once; a failed/lost response requires observe or rollback."""
        from .native_guest_worker import _guest_mutation_descriptor
        with _guest_mutation_descriptor(wait=2):
            units, entry, argv, expected = self._material()
            _need(not any(os.path.lexists(path) for path in (self.intent, self.done, self.stop_intent)), UNSETTLED)
            _need(not any(os.path.lexists(path) for path in self.retirement._paths('restore')))
            self._held(units)
            self._old_absent(units)
            self._own_absent()
            self._gate('start', 'before', assert_safe, deadline)
            self._gate('start', 'dispatch', assert_safe, deadline)
            # Protected O_EXCL + fsync precedes the sole launch. Never retry it.
            runtime._write(self.intent, wire._bytes(expected), mode=0o400)
            self._held(units)
            self._old_absent(units)
            self._own_absent()
            profile = native._profile(bytes.fromhex(units['provider']['fragment']))
            command = ['/usr/bin/systemd-run', '--quiet', '--expand-environment=no', '--unit=' + self.unit,
                       '--property=User=root', '--property=Restart=no', '--property=NoNewPrivileges=yes',
                       '--property=PrivateTmp=yes', '--property=Type=' + profile.get(('Service', 'Type'), 'simple'),
                       '--property=CollectMode=' + profile.get(('Unit', 'CollectMode'), 'inactive')]
            if ('Unit', 'Description') in profile:
                command.append('--description=' + profile[('Unit', 'Description')])
            _need(time.monotonic() < deadline)
            native._command((*command, '--', *argv))
            # A listener is necessary but never substitutes for native callback proof.
            until = min(deadline, time.monotonic() + 4)
            while self.retirement.ports['provider'] not in native._listeners():
                _need(time.monotonic() < until, UNSETTLED)
                time.sleep(.1)
            observed = self._observe('start', units, entry, expected, assert_safe, deadline)
            runtime._write(self.done, wire._bytes(observed), mode=0o400)
            return observed

    def _observe(self, mode, units, entry, expected, assert_safe, deadline):
        self._intent(expected)
        self._held(units)
        self._old_absent(units)
        current = self.retirement._live(entry, 'provider', original=False)
        before = self._gate(mode, 'before', assert_safe, deadline)
        ready = self._gate(mode, 'provider_ready', assert_safe, deadline)
        _need(self.retirement._live(entry, 'provider', original=False) == current)
        self._old_absent(units)
        self._held(units)
        after = self._gate(mode, 'after', assert_safe, deadline)
        _need(self.retirement._live(entry, 'provider', original=False) == current)
        self._old_absent(units)
        self._held(units)
        return expected | {'provider': current, 'proofs': {'before': before, 'provider_ready': ready, 'after': after}}

    def observe(self, assert_safe, deadline):
        """Reconcile exact live state without launching or writing any record."""
        from .native_guest_worker import _guest_mutation_descriptor
        with _guest_mutation_descriptor(wait=2):
            units, entry, _, expected = self._material()
            _need(not os.path.lexists(self.stop_intent)
                  and not any(os.path.lexists(path) for path in self.retirement._paths('restore')))
            return self._observe('observe', units, entry, expected, assert_safe, deadline)

    def rollback(self, assert_safe, deadline):
        """Stop only our exact live process; restore only the archived provider."""
        from .native_guest_worker import _guest_mutation_descriptor
        with _guest_mutation_descriptor(wait=2):
            units, entry, _, expected = self._material()
            launch_pin = self._launch_pin(expected)
            self._held(units)
            self._gate('rollback', 'before', assert_safe, deadline)
            if os.path.lexists(self.stop_intent):
                # Issued means never dispatch stop again, including a lost reply.
                self._stop_record(expected)
                self._own_absent(port_free=False)
            else:
                _need(self._restore_intent() is None)
                self._old_absent(units)
                props = native._properties(self.unit)
                current = None
                if props['ActiveState'] == 'active':
                    # No launch intent: never stop a same-named unrelated unit.
                    _need(launch_pin is not None)
                    current = self._own_live(entry, ready=False)
                else:
                    self._own_absent()
                proof = self._gate('rollback', 'dispatch', assert_safe, deadline)
                if current is not None:
                    _need(self._own_live(entry, ready=False) == current)
                else:
                    self._own_absent()
                self._held(units)
                self._old_absent(units)
                # Record even an absent replacement: this operation must never
                # gain permission to stop a subsequently appearing process.
                runtime._write(self.stop_intent, wire._bytes(expected | {
                    'operation': 'stop_replacement', 'provider': current,
                    'safe_proof_sha256': proof, 'launch_intent_sha256': launch_pin}), mode=0o400)
                if current is not None:
                    _need(self._own_live(entry, ready=False) == current and time.monotonic() < deadline)
                    native._command(('/usr/bin/systemctl', 'stop', self.unit))
                self._own_absent()
            self._restore_state(units)
            def closed(challenge, boundary):
                self._stop_record(expected)
                self._restore_state(units)
                _need(time.monotonic() < deadline)
                proof = assert_safe(challenge, boundary, deadline)
                _need(wire._hash(proof) and time.monotonic() < deadline)
                self._stop_record(expected)
                self._restore_state(units)
                return proof
            _need(time.monotonic() < deadline)
            result = self.retirement.restore(closed)
            _need(result.status == 'PASS' and wire._hash(result.receipt_sha256))
            current = self.retirement._live(units['provider'], 'provider', original=False)
            self._own_absent(port_free=False)
            self._held(units)
            self._gate('rollback', 'after', assert_safe, deadline)
            _need(self.retirement._live(units['provider'], 'provider', original=False) == current)
            self._own_absent(port_free=False)
            self._held(units)
            return {'restore_receipt_sha256': result.receipt_sha256, 'provider': current,
                    'replacement_unit': self.unit, 'held': self.held}

    def observe_restored(self, assert_safe, deadline):
        """Read-only reconciliation after an uncertain rollback response."""
        from .native_guest_worker import _guest_mutation_descriptor
        with _guest_mutation_descriptor(wait=2):
            units, _, _, expected = self._material()
            self._launch_pin(expected)
            self._stop_record(expected)
            restore_pin, current = self._restore_state(units)
            _need(restore_pin is not None and current is not None)
            self._gate('rollback-observe', 'before', assert_safe, deadline)
            done = self.retirement._paths('restore')[1]
            receipt = self.retirement._receipt('restore') if os.path.lexists(done) else None
            self._gate('rollback-observe', 'after', assert_safe, deadline)
            _need(self._restore_state(units) == (restore_pin, current))
            return {'status': 'RESTORED' if receipt is not None else 'RESTORED_PENDING_RECEIPT',
                    'restore_intent_sha256': restore_pin,
                    'restore_receipt_sha256': receipt, 'provider': current,
                    'replacement_unit': self.unit, 'held': self.held}
