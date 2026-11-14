"""Read-only batched closure proof for the exact approved Linux writer identities."""

from concurrent.futures import ThreadPoolExecutor, wait
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import stat
import sys
import time

from ha_runtime import seven_capture

from .controller import TransitionBlocked
from .legacy_writers import COMPOSE_ORDER, CONTAINER, DOCKER, KUBE, LinuxWriters, _need, _run
from .native_wsl_launcher import checked


NAMESPACE = 'personal-server'
SCOPE = {'Deployment': 'deployment', 'CronJob': 'cronjob', 'Job': 'jobs', 'ReplicaSet': 'replicasets', 'Pod': 'pods'}
SHA = re.compile(r'[a-f0-9]{64}\Z')
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')
RECOVERY_TOOL = re.compile(rb'(windows-bootstrap[.]sh|deploy-n100(?:-safe)?[.]sh|kubectl|systemctl|docker)')
RECOVERY_ACTION = re.compile(rb'(rollout|restart|start|stop|compose|apply|scale|patch|update|delete|replace|create)')
QUERY_NAMES = ('deployments', 'cronjobs', 'jobs', 'replicasets', 'pods', 'docker', 'autoscalers')
KUBE_SCOPES = {
    'Deployment': ('/apis/apps/v1', 'deployments'), 'CronJob': ('/apis/batch/v1', 'cronjobs'),
    'Job': ('/apis/batch/v1', 'jobs'), 'ReplicaSet': ('/apis/apps/v1', 'replicasets'),
    'Pod': ('/api/v1', 'pods'),
}
KUBE_LIST_PATHS = frozenset(
    group + '/namespaces/' + NAMESPACE + '/' + resource for group, resource in KUBE_SCOPES.values()
) | {'/apis/autoscaling/v2/horizontalpodautoscalers'}


class KubernetesListReader:
    """Six independent HTTPS connections for fixed, fresh Kubernetes lists."""

    def __init__(self, context, token_source):
        _need(context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
              and callable(token_source),
              'legacy_writer_api_identity_invalid')
        self._token_source = token_source
        self._connections = {
            path: http.client.HTTPSConnection('127.0.0.1', 6443, context=context) for path in KUBE_LIST_PATHS
        }

    def _authorization(self):
        try:
            token = self._token_source()
        except (OSError, ValueError):
            raise TransitionBlocked('legacy_writer_api_identity_invalid') from None
        _need(type(token) is str and re.fullmatch(r'[A-Za-z0-9._-]{1,16384}', token),
              'legacy_writer_api_identity_invalid')
        return 'Bearer ' + token

    def __call__(self, args, *, timeout):
        if args == (*DOCKER, 'ps', '-aq', '--no-trunc') or (
            args[:len(DOCKER) + 1] == (*DOCKER, 'inspect')
            and 1 <= len(args) - len(DOCKER) - 1 <= 64
            and all(CONTAINER.fullmatch(value) for value in args[len(DOCKER) + 1:])
        ):
            return _run(args, timeout=timeout)
        _need(len(args) == len(KUBE) + 2 and args[:-1] == (*KUBE, 'get') and args[-1].startswith('--raw='),
              'legacy_writer_api_scope_invalid')
        path = args[-1][6:]
        _need(path in self._connections, 'legacy_writer_api_scope_invalid')
        connection = self._connections[path]
        deadline = time.monotonic() + timeout
        try:
            authorization = self._authorization()
            connection.timeout = _deadline(deadline)
            if connection.sock is not None:
                connection.sock.settimeout(connection.timeout)
            connection.request('GET', path, headers={'Authorization': authorization, 'Accept': 'application/json'})
            transport = connection.sock
            transport.settimeout(_deadline(deadline))
            with connection.getresponse() as response:
                _need(response.status == 200, 'legacy_writer_api_read_failed')
                _need(response.chunked or response.length is not None, 'legacy_writer_api_read_failed')
                limit = 8 * 1024 * 1024
                body = bytearray()
                while True:
                    transport.settimeout(_deadline(deadline))
                    chunk = response.read1(min(65536, limit + 1 - len(body)))
                    body.extend(chunk)
                    _need(len(body) <= limit, 'legacy_writer_proof_response_invalid')
                    if not chunk or response.isclosed():
                        break
                _need(response.isclosed() and response.length in (None, 0), 'legacy_writer_api_read_failed')
                _deadline(deadline)
                return bytes(body)
        except (OSError, http.client.HTTPException, TransitionBlocked):
            connection.close()
            raise TransitionBlocked('legacy_writer_command_unconfirmed') from None

    def close(self):
        for connection in self._connections.values():
            connection.close()


def read_rotating_token(directory, owner):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open('token', flags, dir_fd=directory), 'rb') as stream:
        info = os.fstat(stream.fileno())
        # Atomic replacement can unlink the already-open file. Its contents
        # remain readable through this descriptor until the request finishes.
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner
                or info.st_nlink not in (0, 1) or info.st_mode & 0o077 or info.st_size > 16385):
            raise ValueError('legacy_writer_api_identity_invalid')
        raw = stream.read(16386)
        if len(raw) != info.st_size:
            raise ValueError('legacy_writer_api_identity_invalid')
    return raw.decode('ascii').removesuffix('\n')


def main_https_reader():
    """Start the persistent verifier using pre-provisioned read-only credentials."""
    directory = None
    try:
        owner = os.geteuid()
        _need(owner != 0, 'legacy_writer_api_identity_invalid')
        credential_dir = seven_capture.trusted(
            Path.home() / '.local/state/n100-ha/kubernetes-reader', directory=True)
        for parent in credential_dir.parents:
            mode = parent.lstat()
            _need(stat.S_ISDIR(mode.st_mode) and mode.st_uid in (0, owner) and not mode.st_mode & 0o022,
                  'legacy_writer_api_identity_invalid')
        ca = checked(credential_dir / 'ca.pem', owner, 32768).decode('ascii')
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=ca)
        directory = os.open(credential_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        metadata = os.fstat(directory)
        _need(metadata.st_uid == owner and not metadata.st_mode & 0o077,
              'legacy_writer_api_identity_invalid')
        runner = KubernetesListReader(context, lambda: read_rotating_token(directory, owner))
        return main(persistent=True, runner=runner)
    except (OSError, ValueError, TransitionBlocked, seven_capture.MigrationRejected):
        return 1
    finally:
        if directory is not None:
            os.close(directory)


class InventoryDeadline(TransitionBlocked):
    """Fixed query labels for collections that missed their shared deadline."""

    def __init__(self, *queries):
        self.queries = queries
        super().__init__(*queries)

    def reason(self):
        if not self.queries or any(query not in QUERY_NAMES for query in self.queries):
            return None
        if len(set(self.queries)) == 1:
            return 'deadline_' + self.queries[0]
        mask = sum(1 << index for index, name in enumerate(QUERY_NAMES) if name in self.queries)
        return f'deadline_q{mask:02x}'


def _payload(value):
    _need(type(value) is bytes and 0 < len(value) <= 8 * 1024 * 1024, 'legacy_writer_proof_response_invalid')
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        _need(False, 'legacy_writer_proof_response_invalid')


def _deadline(stop):
    remaining = stop - time.monotonic()
    _need(remaining > 0, 'legacy_writer_proof_deadline')
    return remaining


def _inventory(runner, stop):
    def run(command, name):
        try:
            result = runner(command, timeout=_deadline(stop))
            _deadline(stop)
        except TransitionBlocked as error:
            if time.monotonic() >= stop:
                raise InventoryDeadline(name) from error
            raise
        return result

    def raw(group, resource, *, namespace=True):
        location = group + ('/namespaces/' + NAMESPACE if namespace else '') + '/' + resource
        name = resource if namespace else 'autoscalers'
        result = _payload(run((*KUBE, 'get', '--raw=' + location), name))
        _need(type(result) is dict and type(result.get('items')) is list
              and type(result.get('metadata')) is dict and not result['metadata'].get('continue'),
              'legacy_writer_proof_response_invalid')
        return result

    def containers():
        ids = run((*DOCKER, 'ps', '-aq', '--no-trunc'), 'docker').decode('ascii').split()
        _need(1 <= len(ids) <= 64 and len(set(ids)) == len(ids)
              and all(CONTAINER.fullmatch(value) for value in ids), 'legacy_writer_proof_response_invalid')
        rows = _payload(run((*DOCKER, 'inspect', *ids), 'docker'))
        _need(len(rows) == len(ids) and {row['Id'] for row in rows} == set(ids), 'legacy_writer_proof_response_invalid')
        return rows

    pool = ThreadPoolExecutor(max_workers=7)
    wait_for_cleanup = True
    try:
        queries = {kind: pool.submit(raw, group, plural) for kind, (group, plural) in KUBE_SCOPES.items()}
        docker_request = pool.submit(containers)
        hpa_request = pool.submit(raw, '/apis/autoscaling/v2', 'horizontalpodautoscalers', namespace=False)
        requests = dict(zip(QUERY_NAMES, (*queries.values(), docker_request, hpa_request)))
        _, pending = wait(requests.values(), timeout=max(0, stop - time.monotonic()))
        expired = [name for name, query in requests.items()
                   if query in pending or isinstance(query.exception(), InventoryDeadline)]
        if expired:
            # Timed-out read commands still clean up, but the caller must be
            # able to send the rejection before the Windows deadline.
            wait_for_cleanup = False
            raise InventoryDeadline(*expired)
        resources = {}
        for kind, query in queries.items():
            rows = query.result()['items']
            _need(all(row.get('kind', kind) == kind and row['metadata']['namespace'] == NAMESPACE
                      for row in rows), 'legacy_writer_proof_response_invalid')
            resources[SCOPE[kind]] = [dict(row, kind=kind) for row in rows]
        return resources, docker_request.result(), hpa_request.result()
    finally:
        pool.shutdown(wait=wait_for_cleanup, cancel_futures=True)


def observe_closed(baseline, challenge, deadline, *, assert_external_closed, runner=_run):
    """Return a nonce-bound digest of a bounded live view of protected writer identities.

    The held native guard remains responsible for durable admission and restart
    exclusion. The external closure callback independently covers state aliases
    and actors outside the fixed Docker/K3s workload identities.
    """
    _need(type(challenge) is str and re.fullmatch(r'[a-f0-9]{64}', challenge) is not None
          and callable(assert_external_closed), 'legacy_writer_proof_scope_invalid')
    proof = assert_external_closed(challenge, deadline)
    _need(type(proof) is str and re.fullmatch(r'[a-f0-9]{64}', proof) is not None,
          'legacy_writer_external_proof_invalid')
    resources, containers, hpa = _inventory(runner, deadline)
    encoded = lambda value: json.dumps(value, separators=(',', ':'), sort_keys=True).encode()
    by_id = {row['Id']: row for row in containers}
    service_ids = {service: [] for service in COMPOSE_ORDER}
    for row in containers:
        name = (row['Config'].get('Labels') or {}).get('com.docker.compose.service')
        if name in service_ids:
            service_ids[name].append(row['Id'])

    def observed(args):
        if args == (*KUBE, 'get', 'hpa', '-A', '-o', 'json'):
            return hpa
        if args[:len(KUBE) + 3] == (*KUBE, '-n', NAMESPACE, 'get') and args[-2:] == ('-o', 'json'):
            scopes = args[len(KUBE) + 3].split(',')
            rows = [row for scope in scopes for row in resources[scope]]
            if len(args) == len(KUBE) + 7:
                name = args[len(KUBE) + 4]
                matches = [row for row in rows if row['metadata']['name'] == name]
                _need(len(matches) == 1, 'legacy_writer_identity_changed')
                return matches[0]
            _need(len(args) == len(KUBE) + 6, 'legacy_writer_proof_scope_invalid')
            return {'items': rows}
        if args[:len(DOCKER) + 4] == (*DOCKER, 'ps', '-aq', '--no-trunc', '--filter'):
            label = args[-1]
            expected = 'label=com.docker.compose.service='
            _need(len(args) == len(DOCKER) + 5 and label.startswith(expected), 'legacy_writer_proof_scope_invalid')
            return '\n'.join(service_ids[label[len(expected):]]).encode('ascii')
        if len(args) == len(DOCKER) + 2 and args[:len(DOCKER) + 1] == (*DOCKER, 'inspect'):
            return [by_id[args[-1]]]
        _need(False, 'legacy_writer_proof_scope_invalid')

    view = LinuxWriters(baseline, observed, decode_json=lambda value: value)
    view._no_autoscaler()
    claims = view._assert_schedule_graph()
    _need(view._deployments_closed() and not view._writers_active(claims, include_deployments=True),
          'legacy_writer_pods_still_active')
    for row in baseline['schedules']:
        _need(view._schedule(row)[1], 'legacy_writer_reactivation_not_blocked')
    for service in COMPOSE_ORDER:
        _need(view._container(service) == (False, 'no'), 'legacy_writer_container_reopened')
    _deadline(deadline)
    # These are already normalized, closed observations. Do not serialize any
    # Docker env, Kubernetes object content or private system paths.
    return hashlib.sha256(encoded({'challenge': challenge, 'external': proof, 'baseline': baseline,
                                    'workloads_closed': True})).hexdigest()


def _recovery_settled(challenge, deadline):
    for entry in Path('/proc').iterdir():
        _deadline(deadline)
        if not entry.name.isdecimal() or int(entry.name) == os.getpid():
            continue
        try:
            argv = (entry / 'cmdline').read_bytes().split(b'\0')
        except (FileNotFoundError, ProcessLookupError):
            continue
        _need(not any(b'windows-bootstrap.sh' in arg or b'deploy-n100' in arg for arg in argv)
              and not (any(RECOVERY_TOOL.search(arg) for arg in argv)
                       and any(RECOVERY_ACTION.fullmatch(arg) for arg in argv)), 'legacy_recovery_wsl_commands_unsettled')
    return hashlib.sha256((challenge + ':wsl-recovery-commands-settled').encode()).hexdigest()


def prove_runtime(runtime_path, runtime_sha256, plan, attempt, wsl_boot_sha256, challenge, deadline, *, runner=_run):
    """Use one immutable cutover baseline and return only a fresh, opaque WSL proof."""
    _need(all(type(value) is str and SHA.fullmatch(value) for value in (
        runtime_sha256, plan, attempt, wsl_boot_sha256, challenge)), 'legacy_writer_proof_scope_invalid')
    _need(hashlib.sha256(BOOT_ID.read_bytes()).hexdigest() == wsl_boot_sha256, 'legacy_writer_boot_changed')
    reader = seven_capture.Reader()
    path = reader.watch(runtime_path)
    # The signed request supplies the expected hash. Reader.read checks it
    # against the actual bytes while preserving the initial identity stamp.
    descriptor = {'path': str(path), 'sha256': runtime_sha256, 'bytes': path.stat().st_size}
    runtime = seven_capture.parse(reader.read(descriptor))
    _need(type(runtime) is dict and runtime.get('plan_sha256') == plan and runtime.get('attempt_id') == attempt,
          'legacy_writer_proof_scope_invalid')
    proof = observe_closed(runtime['writer_baseline'], challenge, deadline,
                           assert_external_closed=_recovery_settled, runner=runner)
    after = _recovery_settled(challenge, deadline)
    reader.unchanged()
    _need(hashlib.sha256(BOOT_ID.read_bytes()).hexdigest() == wsl_boot_sha256, 'legacy_writer_boot_changed')
    _deadline(deadline)
    return hashlib.sha256((proof + ':' + after + ':' + runtime_sha256).encode()).hexdigest()


def main(*, persistent=False, runner=_run):
    try:
        if persistent:
            # Initialize the local CLI and API paths before the first timed
            # challenge. Discard the result; each proof inventories them again.
            _inventory(runner, time.monotonic() + 3)
            print('ready', flush=True)
        while True:
            line = sys.stdin.buffer.readline(4097)
            if persistent and not line:
                return 0
            _need(0 < len(line) <= 4096 and line.endswith(b'\n'), 'legacy_writer_proof_scope_invalid')
            request = seven_capture.parse(line)
            fields = {'runtime_path', 'runtime_sha256', 'plan', 'attempt', 'wsl_boot_sha256', 'challenge', 'budget_ms'}
            _need(type(request) is dict and set(request) == fields, 'legacy_writer_proof_scope_invalid')
            budget = request.pop('budget_ms')
            _need(type(budget) is int and 0 < budget <= 1800, 'legacy_writer_proof_scope_invalid')
            deadline = time.monotonic() + budget / 1000
            try:
                digest = prove_runtime(**request, deadline=deadline, runner=runner)
            except Exception as error:
                if persistent and type(request['challenge']) is str and SHA.fullmatch(request['challenge']):
                    reason = 'internal'
                    if isinstance(error, TransitionBlocked):
                        reason = {
                            'legacy_writer_proof_deadline': 'deadline',
                            'legacy_writer_command_unconfirmed': 'inventory',
                            'legacy_writer_proof_response_invalid': 'inventory',
                            'legacy_writer_boot_changed': 'identity',
                            'legacy_recovery_wsl_commands_unsettled': 'recovery',
                        }.get(str(error), 'closure')
                    if time.monotonic() >= deadline:
                        reason = 'deadline'
                    if isinstance(error, InventoryDeadline):
                        reason = error.reason() or reason
                    print(request['challenge'] + ':reject:' + reason, flush=True)
                return 1
            if not persistent:
                print(digest, flush=True)
                return 0
            print(request['challenge'] + ':' + digest, flush=True)
    except Exception:
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
