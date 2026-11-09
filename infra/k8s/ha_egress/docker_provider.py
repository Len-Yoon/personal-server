"""Pinned, one-attempt Docker restart; daemon/socket ownership is provisioned outside.

Only trusted broker policy constructs adapters. This is not a socket-access or
network boundary, and never discovers containers by name or retries an effect.
"""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import http.client
import json
import math
from pathlib import PurePosixPath
import re
import socket
import time

from ha_runtime.config import SERVICES
from ha_runtime.effects import ProviderAck


class DockerUnavailable(OSError):
    def __init__(self):
        super().__init__('effect_docker_unavailable')


def _remaining(timeout, deadline):
    if (type(timeout) not in {int, float} or type(deadline) not in {int, float}
            or not math.isfinite(timeout) or not math.isfinite(deadline)
            or not 0 < timeout <= 10):
        raise DockerUnavailable()
    budget = min(timeout, deadline - time.monotonic())
    if budget <= 0:
        raise DockerUnavailable()
    return budget


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise DockerUnavailable()
        value[key] = item
    return value


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__('localhost', timeout=timeout)
        self._path = path

    def connect(self):
        # No TCP/DNS/proxy fallback. The caller closes this socket on every path.
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def _request(connection, method, path, expected, timeout, deadline):
    # An HTTPConnection may reconnect implicitly when a peer closes.
    # Explicit connection liveness prevents that retry/fallback path.
    if connection.sock is None:
        raise DockerUnavailable()
    connection.sock.settimeout(_remaining(timeout, deadline))
    connection.request(method, path, body=b'', headers={'Content-Length': '0'})
    response = connection.getresponse()
    _remaining(timeout, deadline)
    if response.status != expected:
        raise DockerUnavailable()
    chunks = []
    size = 0
    while True:
        connection.sock.settimeout(_remaining(timeout, deadline))
        chunk = response.read1(min(65536, 1048577 - size))
        _remaining(timeout, deadline)
        size += len(chunk)
        if size > 1048576:
            raise DockerUnavailable()
        if not chunk:
            break
        chunks.append(chunk)
    raw = b''.join(chunks)
    if expected == 204 and raw:
        raise DockerUnavailable()
    if expected == 204:
        return None
    return json.loads(raw, object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(DockerUnavailable()))


@dataclass(frozen=True, repr=False)
class DockerRestart:
    socket_path: str
    container_id: str
    image_id: str
    project: str
    service: str

    def __post_init__(self):
        if (type(self.socket_path) is not str or not self.socket_path.startswith('/')
                or '\0' in self.socket_path or len(self.socket_path.encode()) > 107
                or '..' in PurePosixPath(self.socket_path).parts
                or type(self.container_id) is not str or not re.fullmatch('[0-9a-f]{64}', self.container_id)
                or type(self.image_id) is not str or not re.fullmatch('sha256:[0-9a-f]{64}', self.image_id)
                or type(self.project) is not str or not re.fullmatch('[a-z0-9][a-z0-9_-]{0,62}', self.project)
                or type(self.service) is not str or self.service not in SERVICES):
            raise DockerUnavailable()

    def __repr__(self):
        return 'DockerRestart(<pinned>)'

    def _inspect(self, value, *, require_running=True):
        if (type(value) is not dict or value.get('Id') != self.container_id
                or value.get('Image') != self.image_id or type(value.get('Config')) is not dict
                or type(value['Config'].get('Labels')) is not dict):
            raise DockerUnavailable()
        labels = value['Config']['Labels']
        if (labels.get('com.docker.compose.project') != self.project
                or labels.get('com.docker.compose.service') != self.service):
            raise DockerUnavailable()
        state = value.get('State')
        if (type(state) is not dict or type(state.get('Running')) is not bool
                or type(state.get('Status')) is not str
                or state['Status'] not in {'created','running','paused','restarting','removing','exited','dead'}
                or type(state.get('Paused')) is not bool or type(state.get('Restarting')) is not bool
                or (state['Status'] == 'running' and (not state['Running'] or state['Paused'] or state['Restarting']))
                or (require_running and (state['Running'] is not True or state['Status'] != 'running'
                    or state['Paused'] is not False or state['Restarting'] is not False))):
            raise DockerUnavailable()
        started = state.get('StartedAt')
        if (type(started) is not str or not re.fullmatch(
                r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z', started)
                or (require_running and not 1970 <= datetime.fromisoformat(started.replace('Z', '+00:00')).year)):
            raise DockerUnavailable()
        health = state.get('Health')
        if health is None:
            status = 'none'
        elif type(health) is dict and health.get('Status') in {'starting', 'healthy', 'unhealthy'}:
            status = health['Status']
        else:
            raise DockerUnavailable()
        datetime.fromisoformat(started.replace('Z', '+00:00'))
        return {'service': self.service, 'status': state['Status'], 'started_at': started, 'health': status}

    def __call__(self, payload, *, timeout, deadline):
        connection = None
        try:
            if type(payload) is not dict or payload:
                raise DockerUnavailable()
            connection = _UnixConnection(self.socket_path, _remaining(timeout, deadline))
            connection.connect()
            base = '/v1.44/containers/' + self.container_id

            def request(method, path, expected):
                return _request(connection, method, path, expected, timeout, deadline)

            before = self._inspect(request('GET', base + '/json', 200))
            # Reserve one second for acknowledgement/inspection. Never use the
            # daemon's unbounded/default stop timeout or request a zero grace.
            budget = _remaining(timeout, deadline)
            if budget <= 1:
                raise DockerUnavailable()
            grace = max(1, math.floor(budget) - 1)
            request('POST', base + '/restart?t=' + str(grace), 204)
            after = self._inspect(request('GET', base + '/json', 200))
            if after['started_at'] == before['started_at']:
                raise DockerUnavailable()
            _remaining(timeout, deadline)
            reference = hashlib.sha256(json.dumps(
                [self.container_id, self.image_id, self.project, after],
                sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            return ProviderAck('docker-restart:' + reference, after)
        except Exception:
            raise DockerUnavailable() from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


class DockerInspect(DockerRestart):
    """Read-only exact-target state projection; never logs/stats or daemon lists."""
    def __repr__(self):
        return 'DockerInspect(<pinned>)'

    def __call__(self, payload, *, timeout, deadline):
        connection = None
        try:
            if type(payload) is not dict or payload:
                raise DockerUnavailable()
            connection = _UnixConnection(self.socket_path, _remaining(timeout, deadline))
            connection.connect()
            value = _request(connection, 'GET', '/v1.44/containers/' + self.container_id + '/json',
                             200, timeout, deadline)
            result = self._inspect(value, require_running=False)
            _remaining(timeout, deadline)
            reference = hashlib.sha256(json.dumps(
                [self.container_id, self.image_id, self.project, result],
                sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            return ProviderAck('docker-inspect:' + reference, result)
        except Exception:
            raise DockerUnavailable() from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
