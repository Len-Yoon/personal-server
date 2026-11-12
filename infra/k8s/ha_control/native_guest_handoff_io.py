"""Read one prior A proof and publish its one-shot activation handoff over pinned SSH."""

from collections.abc import Mapping
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from threading import Lock
import time

from ha_runtime.config import SERVICES

from . import native_guest_ssh as ssh
from . import native_stdio_bridge as bridge


REMOTE_SOURCE = r'''
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys

ROOT = Path('/var/lib/n100-ha/cutover')
BOOT = Path('/proc/sys/kernel/random/boot_id')
INSTALLATION = Path('/etc/n100-ha-bootstrap-intent.json')
RUNTIME = Path('/etc/n100-ha')
APPS = __APPS__
UID = 0
LIMIT = 4096
PIN = re.compile(r'[a-f0-9]{64}\Z')
BINDING = {'plan_sha256','attempt_id','source_sha256','boot_a_sha256','boot_b_sha256'}
ARGS = {'manifest_sha256','activation_check_sha256','settlement_sha256'}
EVIDENCE = {'manifest_sha256','restore_receipt_sha256','runtime_receipt_sha256',
            'closed_a_sha256','closed_b_sha256','closed_postgres_sha256'}
ACTIVATION = {'coordinator_intent_sha256','primary_prepared_closed_sha256','standby_prepared_closed_sha256',
              'primary_runtime_receipt_sha256','windows_service_instance_sha256','first_write_proof_sha256'}

def need(value):
    if not value:
        raise ValueError('guest_handoff_unconfirmed')

def pin(value):
    return type(value) is str and PIN.fullmatch(value) is not None

def encoded(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':'), sort_keys=True).encode('ascii') + b'\n'

def pairs(values):
    result = dict(values)
    need(len(result) == len(values))
    return result

def decode(raw, canonical=True):
    value = json.loads(raw.decode('ascii'), object_pairs_hook=pairs, parse_constant=lambda _: need(False))
    need(type(value) is dict and (not canonical or encoded(value) == raw))
    return value

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)

def directory(path, private):
    for item in (path, *path.parents):
        info = item.lstat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == UID and not info.st_mode & 0o022)
    info = path.lstat()
    need(not private or not info.st_mode & 0o077)
    return info

@contextmanager
def opened(path, flags, parent=None):
    descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        yield descriptor
    finally:
        os.close(descriptor)

def read_file(parent, name, limit, modes=(0o400, 0o600)):
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    need(stat.S_ISREG(before.st_mode) and before.st_uid == UID and before.st_nlink == 1
         and stat.S_IMODE(before.st_mode) in modes and 0 < before.st_size <= limit)
    with opened(name, os.O_RDONLY, parent) as descriptor:
        need(signature(os.fstat(descriptor)) == signature(before))
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            raw = stream.read(limit + 1)
        need(len(raw) == before.st_size and signature(os.fstat(descriptor)) == signature(before)
             and signature(os.stat(name, dir_fd=parent, follow_symlinks=False)) == signature(before))
    return raw

def absent(directory_fd, name):
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    return False

def check_request(value):
    need(set(value) == {'version','binding','node','boot_sha256','verb','sequence','nonce','args',
                        'installation_id','operation','handoff'})
    binding, args = value['binding'], value['args']
    need(type(binding) is dict and set(binding) == BINDING and all(map(pin, binding.values()))
         and binding['boot_a_sha256'] != binding['boot_b_sha256']
         and type(args) is dict and set(args) == ARGS and all(map(pin, args.values())))
    need(type(value['version']) is int and value['version'] == 1 and value['node'] == 'A'
         and value['verb'] == 'activate' and type(value['sequence']) is int and 1 < value['sequence'] <= 64
         and pin(value['nonce']) and pin(value['installation_id']) and value['boot_sha256'] == binding['boot_a_sha256'])
    need((value['operation'] == 'read' and value['handoff'] is None)
         or (value['operation'] == 'publish' and type(value['handoff']) is dict))

def check_worker(stage, value):
    worker_raw = read_file(stage, 'worker-input.json', 65536)
    worker = decode(worker_raw, canonical=False)
    need(set(worker) == {'schema_version','binding','node','installation_id','restore','runtime'}
         and type(worker['schema_version']) is int and worker['schema_version'] == 1
         and worker['binding'] == value['binding'] and worker['node'] == 'A'
         and worker['installation_id'] == value['installation_id'])
    checked_raw = read_file(stage, 'worker-assert_activation_closed.json', LIMIT)
    need(digest(checked_raw) == value['args']['activation_check_sha256'])
    checked = decode(checked_raw)
    need(set(checked) == {'version','binding','node','boot_sha256','verb','sequence','nonce','worker_input_sha256','evidence'}
         and type(checked['version']) is int and checked['version'] == 1 and checked['binding'] == value['binding']
         and checked['node'] == 'A' and checked['boot_sha256'] == value['boot_sha256']
         and checked['verb'] == 'assert_activation_closed' and type(checked['sequence']) is int
         and 0 < checked['sequence'] < value['sequence'] and pin(checked['nonce']) and checked['nonce'] != value['nonce']
         and checked['worker_input_sha256'] == digest(worker_raw))
    evidence = checked['evidence']
    need(type(evidence) is dict and set(evidence) == EVIDENCE and all(map(pin, evidence.values()))
         and evidence['manifest_sha256'] == value['args']['manifest_sha256']
         and evidence['closed_a_sha256'] != evidence['closed_b_sha256'])
    return checked

def handoff_bytes(value, checked):
    handoff = value['handoff']
    evidence, pins = checked['evidence'], handoff.get('activation_pins')
    expected = {'schema_version':1,'binding':value['binding'],'node':'A','installation_id':value['installation_id'],
                **value['args'],'prepared_receipt_sha256':evidence['runtime_receipt_sha256'],'activation_pins':pins}
    need(encoded(handoff) == encoded(expected) and type(pins) is dict and set(pins) == ACTIVATION
         and all(map(pin, pins.values())) and pins['primary_prepared_closed_sha256'] == evidence['closed_a_sha256']
         and pins['standby_prepared_closed_sha256'] == evidence['closed_b_sha256']
         and pins['primary_runtime_receipt_sha256'] == evidence['runtime_receipt_sha256'])
    return encoded(expected)

def execute(value):
    check_request(value)
    need(os.geteuid() == UID and digest(BOOT.read_bytes()) == value['boot_sha256'])
    directory(INSTALLATION.parent, private=False)
    installed = decode(read_file(None, str(INSTALLATION), 65536), canonical=False)
    need(installed.get('node') == 'A' and installed.get('installation_id') == value['installation_id'])
    root_info = directory(ROOT, private=True)
    attempt = value['binding']['attempt_id']
    with ExitStack() as stack:
        root = stack.enter_context(opened(ROOT, os.O_RDONLY | os.O_DIRECTORY))
        need(signature(os.fstat(root)) == signature(root_info))
        lock = stack.enter_context(opened('mutation.lock', os.O_RDONLY, root))
        info = os.fstat(lock)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == UID and info.st_nlink == 1
             and stat.S_IMODE(info.st_mode) == 0o600
             and signature(info) == signature(os.stat('mutation.lock', dir_fd=root, follow_symlinks=False)))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stage = stack.enter_context(opened(attempt, os.O_RDONLY | os.O_DIRECTORY, root))
        info = os.fstat(stage)
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == UID and not info.st_mode & 0o077
             and signature(info) == signature(os.stat(attempt, dir_fd=root, follow_symlinks=False)))
        need(all(absent(stage, name) for name in ('activation-handoff.json','runtime-activation-intent.json',
                   'runtime-activation.json','worker-activate.intent.json','worker-activate.json'))
             and not any(os.path.lexists(RUNTIME / 'apps' / service / 'activation.json') for service in APPS)
             and not os.path.lexists(RUNTIME / 'broker' / 'authority.json'))
        checked = check_worker(stage, value)
        if value['operation'] == 'read':
            return {'checked': checked}
        raw = handoff_bytes(value, checked)
        need(digest(BOOT.read_bytes()) == value['boot_sha256'])
        out = os.open('activation-handoff.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                      0o600, dir_fd=stage)
        with os.fdopen(out, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(stage)
        need(read_file(stage, 'activation-handoff.json', LIMIT, modes=(0o600,)) == raw)
        return {'handoff_sha256': digest(raw)}

def main():
    signal.alarm(10)
    try:
        raw = sys.stdin.buffer.readline(LIMIT + 1)
        need(0 < len(raw) <= LIMIT and not sys.stdin.buffer.read(1))
        value = decode(raw)
        result = execute(value)
        scope = {key: value[key] for key in ('version','binding','node','boot_sha256','verb','sequence','nonce',
                                             'installation_id','operation')}
        answer = encoded(scope | {'args_sha256':digest(encoded(value['args'])),'status':'PASS'} | result)
        need(len(answer) <= LIMIT)
        sys.stdout.buffer.write(answer)
        sys.stdout.buffer.flush()
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return 1
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
'''.replace('__APPS__', repr(tuple(sorted(SERVICES))))

REMOTE_COMMAND = shlex.join(('/usr/bin/python3', '-I', '-B', '-S', '-c', REMOTE_SOURCE))
EVIDENCE_FIELDS = {'manifest_sha256', 'restore_receipt_sha256', 'runtime_receipt_sha256',
                   'closed_a_sha256', 'closed_b_sha256', 'closed_postgres_sha256'}
ACTIVATION_FIELDS = {'coordinator_intent_sha256', 'primary_prepared_closed_sha256', 'standby_prepared_closed_sha256',
                     'primary_runtime_receipt_sha256', 'windows_service_instance_sha256', 'first_write_proof_sha256'}


def _decode(raw):
    bridge._need(type(raw) is bytes and 0 < len(raw) <= bridge.FRAME_BYTES, 'guest_handoff_frame_rejected')
    value = json.loads(raw.decode('ascii'), object_pairs_hook=bridge._pairs, parse_constant=bridge._reject_constant)
    bridge._need(type(value) is dict and raw == bridge._bytes(value), 'guest_handoff_frame_rejected')
    return value


class GuestHandoffIO:
    """One read and one exclusive publication for the same A activation request."""

    def __init__(self, guest, installation_id):
        bridge._need(type(guest) is ssh.GuestSshDispatcher and bridge._hash(installation_id), 'guest_handoff_unbound')
        self.guest = guest
        self.installation_id = installation_id
        self.lock = Lock()
        self.read_attempted = self.publish_attempted = False
        self.checked_scope = self.checked_evidence = None

    def _request(self, request):
        bridge._need(type(request) is bridge.Request and request.binding == self.guest.binding and request.node == 'A'
                     and request.verb == 'activate' and type(request.sequence) is int
                     and 1 < request.sequence <= bridge.MAX_REQUESTS and bridge._hash(request.nonce)
                     and isinstance(request.args, Mapping), 'guest_handoff_unbound')
        args = dict(request.args)
        bridge._args(request.verb, request.node, args)
        return {'version': bridge.VERSION, 'binding': request.binding.fields(), 'node': 'A',
                'boot_sha256': request.binding.boot('A'), 'verb': 'activate', 'sequence': request.sequence,
                'nonce': request.nonce, 'installation_id': self.installation_id}, args

    def _exchange(self, scope, args, operation, handoff, deadline):
        bridge._need(type(deadline) in (int, float), 'guest_handoff_deadline_rejected')
        limit = min(deadline, time.monotonic() + 10)
        bridge._remaining(limit)
        payload = bridge._bytes(scope | {'args': args, 'operation': operation, 'handoff': handoff})
        bridge._need(len(payload) <= bridge.FRAME_BYTES, 'guest_handoff_frame_rejected')
        with self.guest.lock:
            bridge._need(not self.guest.inflight and self.guest.last_sequence < scope['sequence']
                         and scope['nonce'] not in self.guest.nonces, 'guest_handoff_unbound')
            self.guest.inflight = True
        try:
            output = self._transport(payload, limit)
            bridge._remaining(limit)
            response = _decode(output)
            result_key = 'checked' if operation == 'read' else 'handoff_sha256'
            bridge._need(result_key in response and bridge._matches(response, scope | {
                'operation': operation, 'args_sha256': bridge._digest(bridge._bytes(args)), 'status': 'PASS',
                result_key: response[result_key],
            }), 'guest_handoff_response_unbound')
            return response[result_key]
        except (OSError, ValueError, TypeError, KeyError, RecursionError, subprocess.SubprocessError):
            raise bridge.BridgeRejected('guest_handoff_outcome_unconfirmed') from None
        finally:
            with self.guest.lock:
                self.guest.inflight = False

    def _transport(self, payload, limit):
        jump, guest = self.guest.jump, self.guest.guests['A']
        stamps, records = {}, {}
        for label, endpoint in (('jump', jump), ('A', guest)):
            stamps.update(ssh._identity(endpoint, limit))
            path = self.guest.known_hosts[label]
            raw, stamp = ssh._checked_file(path, private=False, maximum=1048576)
            stamps[path] = stamp
            records[endpoint.alias] = ssh._host_record(raw, endpoint, guest=label == 'A')
        temporary_root = '/private/tmp' if sys.platform == 'darwin' else '/tmp'
        with tempfile.TemporaryDirectory(prefix='ha-native-handoff-', dir=temporary_root) as directory:
            config = ssh._ssh_config(Path(directory), jump, guest, records)
            bridge._remaining(limit)
            bridge._need(ssh._same_files(stamps), 'guest_handoff_ssh_identity_changed')
            output = self.guest.run((ssh.SSH, '-F', str(config), '-T', guest.alias, REMOTE_COMMAND), payload, limit)
            bridge._need(ssh._same_files(stamps), 'guest_handoff_ssh_identity_changed')
            return output

    def read_guest_check(self, request, deadline):
        scope, args = self._request(request)
        with self.lock:
            bridge._need(not self.read_attempted, 'guest_handoff_reconciliation_required')
            self.read_attempted = True
            checked = self._exchange(scope, args, 'read', None, deadline)
            bridge._need(type(checked) is dict and bridge._digest(bridge._bytes(checked)) == args['activation_check_sha256']
                         and set(checked) == {'version', 'binding', 'node', 'boot_sha256', 'verb', 'sequence', 'nonce',
                                              'worker_input_sha256', 'evidence'}
                         and type(checked['version']) is int and checked['version'] == bridge.VERSION
                         and all(checked[key] == scope[key] for key in ('binding', 'node', 'boot_sha256'))
                         and checked['verb'] == 'assert_activation_closed' and type(checked['sequence']) is int
                         and 0 < checked['sequence'] < scope['sequence'] and bridge._hash(checked['nonce'])
                         and checked['nonce'] != scope['nonce'] and bridge._hash(checked['worker_input_sha256']),
                         'guest_handoff_response_unbound')
            evidence = checked['evidence']
            bridge._need(type(evidence) is dict and set(evidence) == EVIDENCE_FIELDS
                         and all(map(bridge._hash, evidence.values())) and evidence['manifest_sha256'] == args['manifest_sha256']
                         and evidence['closed_a_sha256'] != evidence['closed_b_sha256'], 'guest_handoff_response_unbound')
            self.checked_scope = bridge._digest(bridge._bytes(scope | {'args': args}))
            self.checked_evidence = dict(evidence)
            return checked

    def publish_guest_handoff(self, request, payload, deadline):
        scope, args = self._request(request)
        with self.lock:
            bridge._need(self.checked_scope == bridge._digest(bridge._bytes(scope | {'args': args}))
                         and not self.publish_attempted, 'guest_handoff_reconciliation_required')
            try:
                value = _decode(payload)
            except (ValueError, TypeError, RecursionError):
                raise bridge.BridgeRejected('guest_handoff_payload_rejected') from None
            pins, evidence = value.get('activation_pins'), self.checked_evidence
            expected = {'schema_version': 1, 'binding': scope['binding'], 'node': 'A',
                        'installation_id': self.installation_id, **args,
                        'prepared_receipt_sha256': evidence['runtime_receipt_sha256'], 'activation_pins': pins}
            bridge._need(bridge._matches(value, expected) and type(pins) is dict and set(pins) == ACTIVATION_FIELDS
                         and all(map(bridge._hash, pins.values()))
                         and pins['primary_prepared_closed_sha256'] == evidence['closed_a_sha256']
                         and pins['standby_prepared_closed_sha256'] == evidence['closed_b_sha256']
                         and pins['primary_runtime_receipt_sha256'] == evidence['runtime_receipt_sha256'],
                         'guest_handoff_payload_rejected')
            self.publish_attempted = True
            result = self._exchange(scope, args, 'publish', value, deadline)
            bridge._need(result == bridge._digest(payload), 'guest_handoff_outcome_unconfirmed')
