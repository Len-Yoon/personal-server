"""Bind an isolated renewal fixture to the existing Windows service launcher."""

import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import time
from types import SimpleNamespace

from .isolated_lease_rehearsal import DisposableLeaseBackend, run
from .scm_pg_auth_probe import plain_file, plain_path, require, strict_json

SCOPE = 'isolated-scm-lease-rehearsal'
MAX_FRAME = 8192


def loopback_request(port, challenge, deadline):
    frame = json.dumps(challenge, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    require(len(frame) < MAX_FRAME)
    timeout = deadline - time.monotonic()
    require(timeout > 0)
    with socket.create_connection(('127.0.0.1', port), timeout=timeout) as connection:
        connection.settimeout(max(0.001, deadline - time.monotonic()))
        connection.sendall(frame + b'\n')
        response = bytearray()
        while not response.endswith(b'\n'):
            timeout = deadline - time.monotonic()
            require(timeout > 0 and len(response) < MAX_FRAME)
            connection.settimeout(timeout)
            data = connection.recv(MAX_FRAME - len(response))
            require(bool(data))
            response.extend(data)
        return strict_json(response)


class FixtureObserver:
    """Keep the authenticated fixture transport separate from production proof."""

    def __init__(self, request, fixture_id, key):
        require(type(key) is bytes and len(key) == 32)
        self.request = request
        self.fixture_id = fixture_id
        self.key = key

    def _check(self, operation, deadline, **parameters):
        budget = min(1900, int((deadline - time.monotonic()) * 1000) - 40)
        require(budget > 0)
        challenge = {'scope': SCOPE, 'fixture_id': self.fixture_id, 'operation': operation,
                     'nonce': secrets.token_hex(32), 'parameters': parameters, 'budget_ms': budget}
        raw = json.dumps(challenge, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')
        request = {'challenge': challenge, 'mac': hmac.digest(self.key, raw, 'sha256').hex()}
        response = self.request(request, deadline)
        require(type(response) is dict and set(response) == {'challenge', 'result', 'mac'}
                and response['challenge'] == challenge and type(response['result']) is dict
                and type(response['mac']) is str and time.monotonic() < deadline)
        signed = json.dumps({'challenge': challenge, 'result': response['result']},
                            sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')
        require(hmac.compare_digest(response['mac'], hmac.digest(self.key, signed, 'sha256').hex()))
        return response['result']

    def observe(self, state, deadline):
        reply = self._check('observe-isolated-writers', deadline)
        return (reply.get('windows_fixture_observed') is True
                and reply.get('wsl_fixture_observed') is True
                and reply.get('active_token_accepted') is True)

    def exclusion(self, state, deadline):
        return self._check('check-isolated-exclusion', deadline).get('held') is True

    def previous_token_status(self):
        value = self._check('probe-previous-token', time.monotonic() + 1).get('status')
        require(type(value) is int and value in (200, 401))
        return value

    def assert_single_writer(self, state, expiry, *, writes_open):
        response = self._check('check-isolated-writes', time.monotonic() + 2,
                               expiry=expiry.isoformat() if expiry else None,
                               writes_open=writes_open)
        return (response.get('a_write_accepted') is writes_open
                and response.get('b_write_rejected') is True)


def launch(args, observer):
    """Consume only private fixture files supplied by the SCM wrapper."""
    path = plain_file(Path(args.plan), 65536)
    authority = path.parent
    approval_path = plain_file(Path(args.approval), 2048)
    journal = Path(args.journal)
    require(re.fullmatch(r'lease-rehearsal-[0-9a-f]{32}', authority.name)
            and path.name == 'plan.json' and approval_path == authority / 'approval.json'
            and journal == authority / 'journal/controller-state.json'
            and plain_path(journal.parent).is_dir() and not os.path.lexists(journal))
    raw = path.read_bytes()
    fixture = strict_json(raw)
    approval = strict_json(approval_path.read_bytes())
    now = datetime.now(timezone.utc).timestamp()
    digest = hashlib.sha256(raw).hexdigest()
    require(set(fixture) == {'scope', 'backend', 'retained_system_ids', 'ca_sha256', 'token_expiry',
                            'fixture_id', 'observer_sha256'}
            and fixture['scope'] == SCOPE
            and set(approval) == {'scope', 'plan_sha256', 'expires_unix'}
            and approval['scope'] == SCOPE and approval['plan_sha256'] == digest
            and type(approval['expires_unix']) is int and now < approval['expires_unix'] <= now + 900
            and not any(key.upper().startswith('PG') for key in os.environ))
    require(hashlib.sha256(plain_file(authority / 'observer.json', 2048).read_bytes()).hexdigest()
            == fixture['observer_sha256'])
    expiry = datetime.fromisoformat(fixture['token_expiry'])
    require(expiry.utcoffset() is not None and expiry.timestamp() + 100 < approval['expires_unix'])
    require(type(observer) is FixtureObserver and observer.fixture_id == fixture['fixture_id']
            and re.fullmatch(r'[0-9a-f]{32}', observer.fixture_id))
    plan = fixture['backend']
    sid = plan['windows_enforced']['service_sid']
    require(set(plan['pg_nodes']) == {'A', 'B'})
    ca = plain_file(authority / 'ca.crt', 16384)
    password = plain_file(authority / 'postgres.pgpass', 4096)
    require(hashlib.sha256(ca.read_bytes()).hexdigest() == fixture['ca_sha256'])
    hosts = set()
    for endpoint in plan['pg_nodes'].values():
        require(set(endpoint) == {'host', 'port', 'dbname', 'user', 'sslmode', 'sslrootcert', 'passfile'}
                and Path(endpoint['sslrootcert']) == ca and Path(endpoint['passfile']) == password)
        host = ipaddress.IPv4Address(endpoint['host'])
        require(host.is_private and not host.is_loopback and not host.is_link_local)
        hosts.add(host)
    require(len(hosts) == 2)
    backend = DisposableLeaseBackend(path, approval_path, fence=SimpleNamespace(plan_sha256=digest))
    backend.plan = plan
    backend._plan()
    backend.depth = 1
    state = {'cluster': plan['cluster'], 'generation': 1, 'timeline': 1, 'target': 'A', 'old': 'B'}
    evidence = run(backend, state=state, service_sid=sid, retained_system_ids=fixture['retained_system_ids'],
                   observe=observer.observe, exclusion=observer.exclusion,
                   previous_token_status=observer.previous_token_status, token_expires_at=expiry,
                   assert_single_writer=observer.assert_single_writer)
    require(time.time() < approval['expires_unix'])
    with journal.open('x', encoding='ascii') as stream:
        json.dump({'scope': SCOPE, 'plan_sha256': digest, **evidence}, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('authenticate',))
    for name in ('plan', 'approval', 'journal'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args(argv)
    try:
        authority = Path(args.plan).parent
        config = strict_json(plain_file(authority / 'observer.json', 2048).read_bytes())
        require(set(config) == {'fixture_id', 'port', 'key_sha256'} and type(config['port']) is int
                and 49152 <= config['port'] <= 65535)
        key = plain_file(authority / 'observer.key', 32).read_bytes()
        require(hashlib.sha256(key).hexdigest() == config['key_sha256'])
        observer = FixtureObserver(lambda challenge, deadline: loopback_request(config['port'], challenge, deadline),
                                   config['fixture_id'], key)
        launch(args, observer)
    except Exception as error:
        try:
            journal = Path(args.journal)
            if journal.name == 'controller-state.json' and journal.parent.name == 'journal':
                from .controller import TransitionBlocked
                reason = str(error) if isinstance(error, TransitionBlocked) else type(error).__name__
                if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,95}', reason):
                    with (plain_path(journal.parent) / 'failure.json').open('x', encoding='ascii') as stream:
                        import traceback
                        locations = [{'file': Path(frame.filename).name, 'line': frame.lineno}
                                     for frame in traceback.extract_tb(error.__traceback__)
                                     if Path(frame.filename).name in {
                                         'scm_isolated_lease_probe.py', 'isolated_lease_rehearsal.py',
                                         'initial_pg_lease.py', 'pg.py', 'scm_pg_auth_probe.py',
                                     }]
                        json.dump({'scope': SCOPE, 'reason': reason, 'locations': locations[-8:]}, stream)
        except (OSError, ValueError):
            pass
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
