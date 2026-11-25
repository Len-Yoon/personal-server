"""Coordinate one same-boot authority replacement without replaying an issued action."""

import argparse
import hashlib
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time

from .controller import TransitionBlocked, exclusive
from .native_stdio_bridge import RECOVERY_START_ORDER

STAGES = (
    'preflight', 'retire_provider', 'close_pair', 'replace_provider',
    'carryover', 'stage_runtime', 'arm_host', 'publish', 'start_apps', 'portal',
)
READ_ONLY = {'preflight', 'close_pair', 'portal'}
HASH = re.compile(r'[0-9a-f]{64}')
SCOPE_FIELDS = {'attempt_sha256', 'previous_plan_sha256', 'previous_instance_sha256',
                'history_sha256', 'successor_image_sha256', 'runtime_manifest_sha256',
                'previous_revision', 'driver_sha256'}
ERROR = 'sameboot_transition_unconfirmed'


def _need(condition, reason=ERROR):
    if not condition:
        raise TransitionBlocked(reason)


def _pin(value):
    return type(value) is str and HASH.fullmatch(value) is not None


def _private_file(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        _need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
              and before.st_uid == os.geteuid() and stat.S_IMODE(before.st_mode) == 0o600)
        raw = stream.read(limit + 1)
        _need(0 < len(raw) <= limit and os.fstat(stream.fileno()) == before)
    return raw


def _encoded(value):
    return (json.dumps(value, sort_keys=True, allow_nan=False, separators=(',', ':')) + '\n').encode()


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _scope(value):
    _need(type(value) is dict and set(value) == SCOPE_FIELDS)
    _need(all(_pin(value[key]) for key in SCOPE_FIELDS - {'previous_revision'}))
    revision = value['previous_revision']
    _need(type(revision) is int and 0 < revision < 2**63 - 3 and revision % 2 == 0)
    return value.copy()


def _closed(value, revision, terminal):
    return (value.get('closed_nodes') == ['A', 'B']
            and value.get('synchronous') is True
            and value.get('same_expiry') is True
            and value.get('replay_verified') is True
            and value.get('effects_isolated') is True
            and type(value.get('revision')) is int and value['revision'] == revision
            and value.get('terminal_sha256') == terminal
            and _pin(value.get('closed_proof_sha256')))


def bind_successor_plan(candidate, previous_raw, plan_pin, files, *, allow_saved_reboot=False):
    """Validate final native files before handing a candidate to the Windows armer."""
    from .initial_postboot import CARRYOVER_FIELD, FILES, TRANSITION_FIELDS, sameboot, saved_reboot, scope, validate_seal

    _need(type(previous_raw) is bytes and 0 < len(previous_raw) <= 65536)
    _need(type(candidate) is dict and type(files) is dict)
    value = scope(candidate)
    _need((sameboot(value) or (allow_saved_reboot is True and saved_reboot(value)))
          and TRANSITION_FIELDS <= value.keys() and CARRYOVER_FIELD in value)
    if saved_reboot(value):
        runtime_pin = candidate.get('saved_reboot_runtime_manifest_sha256')
        runtime = files.get('postboot-runtime-manifest.json')
        _need(_pin(runtime_pin) and type(runtime) is bytes and 0 < len(runtime) <= 1048576
              and hashlib.sha256(runtime).hexdigest() == runtime_pin)
        files = {name: data for name, data in files.items() if name != 'postboot-runtime-manifest.json'}
    _need(_pin(plan_pin) and hashlib.sha256(_encoded(candidate)).hexdigest() == plan_pin)
    _need(files.get('postboot-predecessor-plan.json') == previous_raw
          and hashlib.sha256(previous_raw).hexdigest() == value['predecessor_plan_sha256'])
    checked = validate_seal(candidate, plan_pin, files)
    _need(checked == value)
    return {'plan_sha256': plan_pin, 'carryover_sha256': value[CARRYOVER_FIELD],
            'successor_image_sha256': value['successor_service_image_sha256'],
            'terminal_sha256': value['predecessor_terminal_sha256'],
            'closed_proof_sha256': json.loads(files[FILES[4]])['closed_pair_sha256'],
            'previous_plan_sha256': value['predecessor_plan_sha256']}


def observe_portal(deadline):
    """Check the public hostname directly and leave redirects as non-200 results."""
    def status(path):
        remaining = deadline - time.monotonic()
        _need(remaining > 0)
        connection = HTTPSConnection('len.pe.kr', timeout=min(6, remaining))
        try:
            connection.request('GET', path)
            return connection.getresponse().status
        finally:
            connection.close()

    landing = status('/')
    started = []
    health = []
    for _ in range(3):
        if started:
            wait = max(0, started[-1] + 10 - time.monotonic())
            _need(time.monotonic() + wait < deadline)
            time.sleep(wait)
        started.append(time.monotonic())
        health.append(status('/health'))
    return {'landing_http': landing, 'health_http': health, 'health_started_monotonic': started}


def _validate(stage, value, scope, receipts):
    _need(type(value) is dict)
    revision = scope['previous_revision']
    if stage == 'preflight':
        _need(set(value) == {'history_sha256', 'rollback_sha256', 'previous_instance_sha256', 'revision',
                             'all_apps_stopped', 'successor_runtime_verified', 'successor_provider_verified',
                             'handoff_driver_verified', 'old_provider_only_retirement'})
        _need(value['history_sha256'] == scope['history_sha256']
              and value['previous_instance_sha256'] == scope['previous_instance_sha256']
              and type(value['revision']) is int and value['revision'] == revision
              and _pin(value['rollback_sha256'])
              and all(value[key] is True for key in ('all_apps_stopped', 'successor_runtime_verified',
                    'successor_provider_verified', 'handoff_driver_verified', 'old_provider_only_retirement')))
    elif stage == 'retire_provider':
        _need(set(value) == {'previous_instance_sha256', 'terminal_sha256', 'other_observers_preserved',
                             'old_service_stopped_disabled', 'native_terminal_preserved'})
        _need(value['previous_instance_sha256'] == scope['previous_instance_sha256']
              and _pin(value['terminal_sha256'])
              and all(value[key] is True for key in ('other_observers_preserved', 'old_service_stopped_disabled',
                                                     'native_terminal_preserved')))
    elif stage == 'close_pair':
        _need(set(value) == {'closed_nodes', 'synchronous', 'same_expiry', 'replay_verified',
                             'effects_isolated', 'revision', 'terminal_sha256', 'closed_proof_sha256'})
        _need(_closed(value, revision + 1, receipts['retire_provider']['terminal_sha256']))
    elif stage == 'replace_provider':
        _need(set(value) == {'owned_port', 'provider_proof_sha256'})
        _need(value['owned_port'] is True and _pin(value['provider_proof_sha256']))
    elif stage == 'restore_provider':
        _need(set(value) == {'old_port_owner_verified', 'old_provider_proof_sha256', 'rollback_sha256'})
        _need(value['old_port_owner_verified'] is True and _pin(value['old_provider_proof_sha256'])
              and value['rollback_sha256'] == receipts['preflight']['rollback_sha256'])
    elif stage == 'carryover':
        _need(set(value) == {'history_sha256', 'carryover_sha256', 'held_observation_sha256'})
        _need(value['history_sha256'] == scope['history_sha256'] and _pin(value['carryover_sha256'])
              and value['held_observation_sha256'] == value['carryover_sha256'])
    elif stage == 'stage_runtime':
        _need(set(value) == {'runtime_stage_sha256', 'manifest_sha256', 'old_runtime_preserved', 'authority_issued'})
        _need(_pin(value['runtime_stage_sha256']) and value['manifest_sha256'] == scope['runtime_manifest_sha256']
              and value['old_runtime_preserved'] is True
              and value['authority_issued'] is False)
    elif stage == 'arm_host':
        _need(set(value) == {'successor_image_sha256', 'plan_sha256', 'carryover_sha256', 'service_stopped'})
        _need(value['successor_image_sha256'] == scope['successor_image_sha256'] and _pin(value['plan_sha256'])
              and value['carryover_sha256'] == receipts['carryover']['carryover_sha256']
              and value['service_stopped'] is True)
    elif stage == 'publish':
        _need(set(value) == {'plan_sha256', 'instance_sha256', 'publication_sha256', 'revision',
                             'writers', 'standby_node', 'synchronous', 'predecessor_instance_stopped'})
        _need(value['plan_sha256'] == receipts['arm_host']['plan_sha256']
              and _pin(value['instance_sha256']) and value['instance_sha256'] != scope['previous_instance_sha256']
              and _pin(value['publication_sha256']) and type(value['revision']) is int and value['revision'] == revision + 2
              and value['writers'] == ['A'] and value['standby_node'] == 'B' and value['synchronous'] is True
              and value['predecessor_instance_stopped'] is True)
    elif stage == 'start_apps':
        _need(set(value) == {'instance_sha256', 'ready', 'startup_sha256'})
        _need(value['instance_sha256'] == receipts['publish']['instance_sha256'] and _pin(value['startup_sha256'])
              and value['ready'] == list(RECOVERY_START_ORDER))
    elif stage == 'portal':
        _need(set(value) == {'landing_http', 'health_http', 'health_started_monotonic'})
        started = value['health_started_monotonic']
        now = time.monotonic()
        _need(type(started) is list and len(started) == 3
              and all(type(stamp) in (int, float) and 0 < stamp <= now for stamp in started)
              and all(right-left >= 10 for left, right in zip(started, started[1:]))
              and type(value['landing_http']) is int and value['landing_http'] == 200
              and type(value['health_http']) is list and len(value['health_http']) == 3
              and all(type(status) is int and status == 200 for status in value['health_http']))
    else:
        _need(False)


class SamebootTransition:
    """The trusted driver owns native I/O and must authenticate every live read.

    The driver receives an immutable request (scope, phase, nonce, previous
    receipt) and a fixed deadline. Its `guard` must verify fresh custody
    before dispatch. From replacement through publication and during rollback
    it must reobserve the same authenticated A/B closure and confirm that no
    successor is serving. On reconciliation it must permit read-only evidence
    from an already applied phase and require the exact issued identity.
    `arm_material` provides the authenticated native plan and seal inputs so
    this coordinator can reject a mismatched successor before arming it.
    The journal coordinates those tools; its contents never authorize a writer.
    """

    def __init__(self, directory, scope, driver):
        self.directory = Path(directory)
        self.scope = _scope(scope)
        self.driver = driver

    def _private(self):
        info = self.directory.lstat()
        _need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
              and stat.S_IMODE(info.st_mode) == 0o700, 'sameboot_transition_journal_untrusted')

    def _read(self, name):
        try:
            raw = _private_file(self.directory / name, 8192)
        except FileNotFoundError:
            return None
        value = json.loads(raw)
        _need(type(value) is dict and _encoded(value) == raw)
        return value

    def _write(self, name, value):
        data = _encoded(value)
        _need(len(data) <= 8192)
        fd = os.open(self.directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _position(self):
        allowed = {'lock'}
        receipts = {}
        previous = None
        for stage in STAGES:
            allowed.update({stage + '.issued.json', stage + '.observed.json'})
            issued = self._read(stage + '.issued.json')
            result = self._read(stage + '.observed.json')
            if issued is None:
                _need(result is None)
                self._check_entries(allowed)
                return stage, previous, receipts, None
            self._check_issued(issued, stage, previous)
            if result is None:
                self._check_entries(allowed)
                return stage, previous, receipts, issued
            _need(set(result) == {'issued_sha256', 'status', 'evidence'}
                  and result['issued_sha256'] == _digest(issued))
            if result['status'] == 'REJECTED':
                _need(stage == 'replace_provider' and result['evidence'] == {'replacement_failed': True})
                self._check_entries(allowed | {'restore_provider.issued.json', 'restore_provider.observed.json'})
                return 'restore_provider', _digest(result), receipts, self._read('restore_provider.issued.json')
            _need(result['status'] == 'PASS')
            _validate(stage, result['evidence'], self.scope, receipts)
            if stage == 'arm_host':
                _need(result['evidence']['plan_sha256'] == issued['plan_material_sha256'])
            receipts[stage] = result['evidence']
            previous = _digest(result)
        self._check_entries(allowed)
        return None, previous, receipts, None

    def _check_entries(self, allowed):
        _need(all(path.name in allowed for path in self.directory.iterdir()))

    def status(self):
        self._private()
        with exclusive(self.directory / 'lock'):
            stage, previous, receipts, issued = self._position()
            if stage == 'restore_provider':
                record = self._restored(previous, receipts, issued)
                if record:
                    return record
            return {'status': 'COMPLETE' if stage is None else 'PENDING', 'stage': stage,
                    'observe_only': issued is not None, 'previous_sha256': previous}

    def _check_issued(self, issued, stage, previous):
        _need(type(issued) is dict and set(issued) == {'scope', 'stage', 'previous_sha256', 'nonce',
                                                     'plan_material_sha256'}
              and issued['scope'] == self.scope and issued['stage'] == stage
              and issued['previous_sha256'] == previous and _pin(issued['nonce'])
              and (_pin(issued['plan_material_sha256']) if stage == 'arm_host'
                   else issued['plan_material_sha256'] is None))

    def _restored(self, previous, receipts, issued):
        record = self._read('restore_provider.observed.json')
        if record is None:
            return None
        self._check_issued(issued, 'restore_provider', previous)
        _need(set(record) == {'issued_sha256', 'status', 'evidence'}
              and record['issued_sha256'] == _digest(issued) and record['status'] == 'PASS')
        _validate('restore_provider', record['evidence'], self.scope, receipts)
        return {'status': 'PROVIDER_RESTORED', 'receipt_sha256': _digest(record)}

    def step(self, deadline, *, observe_only=False):
        """Run the next phase once, or reconcile its prior request without reissuing it."""
        self._private()
        with exclusive(self.directory / 'lock'):
            stage, previous, receipts, issued = self._position()
            if stage is None:
                return {'status': 'COMPLETE', 'receipt_sha256': previous}
            if stage == 'restore_provider':
                record = self._restored(previous, receipts, issued)
                if record:
                    return record
            if issued is not None:
                _need(observe_only, 'sameboot_transition_reconciliation_required')
            else:
                _need(not observe_only, 'sameboot_transition_no_action_to_observe')
                issued = {'scope': self.scope, 'stage': stage, 'previous_sha256': previous,
                          'nonce': secrets.token_hex(32), 'plan_material_sha256': None}
            _need(time.monotonic() < deadline)
            arm = None
            if stage == 'arm_host':
                material = self.driver.arm_material(_encoded(issued), _encoded(receipts), deadline)
                _need(type(material) is dict and set(material) == {'candidate', 'previous_raw', 'plan_pin', 'files'})
                arm = bind_successor_plan(**material)
                _need(arm == {'plan_sha256': material['plan_pin'],
                              'carryover_sha256': receipts['carryover']['carryover_sha256'],
                              'successor_image_sha256': self.scope['successor_image_sha256'],
                              'terminal_sha256': receipts['retire_provider']['terminal_sha256'],
                              'closed_proof_sha256': receipts['close_pair']['closed_proof_sha256'],
                              'previous_plan_sha256': self.scope['previous_plan_sha256']})
                if observe_only:
                    _need(issued['plan_material_sha256'] == arm['plan_sha256'])
                else:
                    issued['plan_material_sha256'] = arm['plan_sha256']
            self._check_issued(issued, stage, previous)
            _need(time.monotonic() < deadline)
            _need(self.driver.guard(_encoded(issued), _encoded(receipts), deadline,
                                    observe_only=observe_only) is True)
            _need(time.monotonic() < deadline)
            if not observe_only:
                self._write(stage + '.issued.json', issued)
            if stage == 'portal':
                reply = {'issued_sha256': _digest(issued), 'status': 'PASS', 'evidence': observe_portal(deadline)}
            else:
                perform = self.driver.observe if observe_only or stage in READ_ONLY else self.driver.apply
                reply = perform(_encoded(issued), _encoded(receipts), deadline)
            _need(time.monotonic() < deadline)
            _need(type(reply) is dict and set(reply) == {'issued_sha256', 'status', 'evidence'}
                  and reply['issued_sha256'] == _digest(issued))
            if reply['status'] == 'REJECTED':
                _need(stage == 'replace_provider' and reply['evidence'] == {'replacement_failed': True})
            else:
                _need(reply['status'] == 'PASS')
                _validate(stage, reply['evidence'], self.scope, receipts)
                _need(arm is None or reply['evidence']['plan_sha256'] == arm['plan_sha256'])
            self._write(stage + '.observed.json', reply)
            if stage == 'restore_provider':
                return {'status': 'PROVIDER_RESTORED', 'receipt_sha256': _digest(reply)}
            return {'status': reply['status'], 'stage': stage, 'receipt_sha256': _digest(reply)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('status', 'step', 'observe'))
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--scope', type=Path, required=True)
    parser.add_argument('--scope-sha256', required=True)
    parser.add_argument('--driver', type=Path)
    parser.add_argument('--seconds', type=int, choices=range(1, 901), default=300,
                        metavar='[1-900]')
    args = parser.parse_args(argv)
    try:
        raw = _private_file(args.scope, 8192)
        _need(hashlib.sha256(raw).hexdigest() == args.scope_sha256)
        scope = _scope(json.loads(raw))
        driver = None
        if args.mode != 'status':
            _need(args.driver is not None)
            source = _private_file(args.driver, 131072)
            _need(hashlib.sha256(source).hexdigest() == scope['driver_sha256'])
            # Load only the independently reviewed and explicitly pinned local driver.
            namespace = {'__name__': 'sameboot_transition_driver', '__file__': str(args.driver)}
            exec(compile(source, str(args.driver), 'exec'), namespace)
            driver = namespace['DRIVER']
        transition = SamebootTransition(args.journal, scope, driver)
        result = (transition.status() if args.mode == 'status'
                  else transition.step(time.monotonic() + args.seconds, observe_only=args.mode == 'observe'))
    except TransitionBlocked as error:
        reason = str(error)
        if reason not in {ERROR, 'sameboot_transition_journal_untrusted',
                          'sameboot_transition_reconciliation_required', 'sameboot_transition_no_action_to_observe'}:
            reason = ERROR
        result = {'status': 'BLOCKED', 'reason': reason}
    except Exception:
        result = {'status': 'BLOCKED', 'reason': ERROR}
    print(json.dumps(result, sort_keys=True))
    return 1 if result['status'] in {'BLOCKED', 'REJECTED'} else 0


if __name__ == '__main__':
    raise SystemExit(main())
