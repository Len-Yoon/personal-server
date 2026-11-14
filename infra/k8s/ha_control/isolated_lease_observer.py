"""Run the challenge server on a caller-owned Windows loopback listener."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import base64
import hmac
import json
import os
from pathlib import Path
from queue import Empty, Full, Queue
import re
import secrets
import socket
import ssl
import subprocess
import sys
from threading import Thread
import time

from .scm_isolated_lease_probe import MAX_FRAME, SCOPE
from .scm_pg_auth_probe import require, strict_json
from .pg import LEASE_SECONDS
from .controller import TransitionBlocked


def verify_windows_snapshot(rows, pins):
    """Accept only the two pinned Hyper-V identities and their known disks."""
    require(type(rows) is list and len(rows) == 2 and set(pins) == {'A', 'B'})
    for node, row in zip(('A', 'B'), rows):
        expected = pins[node]
        require(type(row) is dict and set(row) == {'node', 'id', 'state', 'disk_paths_sha256'}
                and row['node'] == node and row['state'] == 'Running'
                and row['id'] == expected['id']
                and row['disk_paths_sha256'] == expected['disk_paths_sha256'])
    return True


class WslLeaseInventory:
    """Read real K3s and Docker inventory while keeping both tokens in WSL."""

    def __init__(self, context, directory_fd, owner):
        from .wsl_writer_proof import KubernetesListReader, read_rotating_token

        def current():
            return read_rotating_token(directory_fd, owner)

        previous = current()
        claims = previous.split('.')[1]
        claims = base64.urlsafe_b64decode(claims + '=' * (-len(claims) % 4))
        self.expiry = strict_json(claims)['exp']
        require(type(self.expiry) is int and 0 < self.expiry - time.time() < 660)
        self.current = KubernetesListReader(context, current)
        self.previous = KubernetesListReader(context, lambda: previous)

    def __call__(self, operation, deadline):
        from .wsl_writer_proof import _inventory

        if operation == 'inventory':
            _inventory(self.current, deadline)
            return {'inventory_verified': True, 'active_token_status': 200}
        require(operation == 'previous-token-status')
        return {'status': self.previous.probe_status(deadline)}

    def close(self):
        self.current.close()
        self.previous.close()


def wsl_main():
    """Serve only bounded inventory and token-status requests over stdio."""
    from ha_runtime import seven_capture
    from .native_wsl_launcher import checked

    owner = os.geteuid()
    require(owner != 0)
    directory = seven_capture.trusted(Path.home() / '.local/state/n100-ha/kubernetes-reader', directory=True)
    require(all(parent.lstat().st_uid in (0, owner) and not parent.lstat().st_mode & 0o022
                for parent in directory.parents))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=checked(directory / 'ca.pem', owner, 32768).decode('ascii'))
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(fd)
        require(metadata.st_uid == owner and not metadata.st_mode & 0o077)
        inventory = WslLeaseInventory(context, fd, owner)
        try:
            print(json.dumps({'expires_at': inventory.expiry}), flush=True)
            while frame := sys.stdin.buffer.readline(257):
                require(len(frame) <= 256 and frame.endswith(b'\n'))
                request = strict_json(frame)
                require(set(request) == {'operation', 'budget_ms'} and type(request['budget_ms']) is int
                        and 0 < request['budget_ms'] <= 1800)
                result = inventory(request['operation'], time.monotonic() + request['budget_ms'] / 1000)
                print(json.dumps(result), flush=True)
        finally:
            inventory.close()
    finally:
        os.close(fd)


def windows_snapshot_command(installation_id, pins):
    """Render an invocation of the existing fixed Windows VM observer."""
    from uuid import UUID

    require(re.fullmatch(r'[0-9a-f]{64}', installation_id) and set(pins) == {'A', 'B'})
    ids = [str(UUID(pins[node]['id'])) for node in ('A', 'B')]
    require(ids[0] != ids[1])
    return ("$inventory=Get-FixedVmObserverSnapshot "
            f"'{installation_id}' '{ids[0]}' '{ids[1]}';"
            "[ordered]@{rows=$inventory}|ConvertTo-Json -Compress -Depth 5")


def windows_inventory_script(source, installation_id, pins):
    """Run the fixed observer repeatedly without another PowerShell startup."""
    marker = '\nfunction New-FixedVmObserverPipe'
    require(source.count(marker) == 1)
    functions = source.split(marker, 1)[0]
    snapshot = windows_snapshot_command(installation_id, pins)
    return (functions + "\n$ErrorActionPreference='Stop'\nImport-Module Hyper-V\n"
            + snapshot + "\nwhile ($null -ne ($line=[Console]::ReadLine())) {\n"
            + "if ($line -cne '{\"operation\":\"inventory\"}') { throw 'fixture_inventory_request' }\n"
            + snapshot + "\n}\n")


class ObserverProcess:
    """Exchange bounded lines with one already-started observation child."""

    def __init__(self, argv):
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL)
        self.frames = Queue(maxsize=1)
        self.reader = Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        while self.process.stdout is not None:
            frame = self.process.stdout.readline(MAX_FRAME + 1)
            try:
                self.frames.put_nowait(frame)
            except Full:
                return
            if not frame or len(frame) > MAX_FRAME:
                return

    def receive(self, deadline):
        try:
            frame = self.frames.get(timeout=max(0, deadline - time.monotonic()))
            require(0 < len(frame) <= MAX_FRAME and frame.endswith(b'\n') and time.monotonic() < deadline)
            return strict_json(frame)
        except (Empty, ValueError):
            self.close()
            raise ValueError('isolated_observer_child_unconfirmed') from None

    def request(self, value, deadline):
        require(self.process.poll() is None and time.monotonic() < deadline)
        frame = json.dumps(value, separators=(',', ':'), allow_nan=False).encode('ascii') + b'\n'
        require(len(frame) <= 256)
        self.process.stdin.write(frame)
        self.process.stdin.flush()
        return self.receive(deadline)

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=2)
        self.reader.join(timeout=1)
        for stream in (self.process.stdin, self.process.stdout):
            stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def serve_fixture(listener, *, fixture_id, key, windows_argv, windows_pins, wsl_argv,
                  exclusion_held, connect, system_id, cluster, ready, deadline):
    """Keep both native observation children alive through the rehearsal."""
    with ObserverProcess(windows_argv) as windows, ObserverProcess(wsl_argv) as wsl:
        startup = min(deadline, time.monotonic() + 15)
        verify_windows_snapshot(windows.receive(startup)['rows'], windows_pins)
        expiry = wsl.receive(startup)['expires_at']
        require(type(expiry) is int and 2 < expiry - time.time() < 660)

        def windows_snapshot(stop):
            return windows.request({'operation': 'inventory'}, stop)['rows']

        def wsl_snapshot(operation, stop):
            budget = min(1800, int((stop - time.monotonic()) * 1000))
            require(budget > 0)
            return wsl.request({'operation': operation, 'budget_ms': budget}, stop)

        def writes(parameters, stop):
            return check_isolated_writes(parameters, stop, connect=connect,
                                         system_id=system_id, cluster=cluster)

        handlers = live_handlers(windows_snapshot=windows_snapshot, windows_pins=windows_pins,
                                 wsl_snapshot=wsl_snapshot, exclusion_held=exclusion_held, writes=writes)
        handlers['observe-isolated-writers']({}, min(deadline, time.monotonic() + 2))
        ready(expiry)
        serve(listener, fixture_id=fixture_id, key=key, handlers=handlers, deadline=deadline)


def serve_windows_fixture(authority, plan, *, windows_argv, windows_pins, wsl_argv,
                          exclusion_held, ready, deadline):
    """Start the private observer before arming the real SCM service."""
    import psycopg
    from .scm_pg_auth_probe import plain_file

    require(sys.platform == 'win32' and set(plan['pg_nodes']) == {'A', 'B'})
    authority = Path(authority)
    key = plain_file(authority / 'observer.key', 32).read_bytes()
    fixture_id = authority.name.removeprefix('lease-rehearsal-')
    require(authority.name == 'lease-rehearsal-' + fixture_id
            and re.fullmatch(r'[0-9a-f]{32}', fixture_id) and len(key) == 32)
    ca = plain_file(authority / 'ca.crt', 16384)
    passwords = plain_file(authority / 'postgres.pgpass', 4096)

    def connect(node, role):
        endpoint = plan['pg_nodes'][node]
        require(endpoint['port'] == 25432 and endpoint['dbname'] == 'ha_lease_rehearsal'
                and endpoint['sslmode'] == 'verify-full' and role in ('book_memo_a', 'book_memo_b')
                and Path(endpoint['sslrootcert']) == ca and Path(endpoint['passfile']) == passwords)
        return psycopg.connect(host=endpoint['host'], port=25432, dbname='ha_lease_rehearsal',
                               user=role, sslmode='verify-full', sslrootcert=str(ca),
                               passfile=str(passwords), require_auth='scram-sha-256', connect_timeout=2)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        require(49152 <= port <= 65535)
        listener.listen(4)
        serve_fixture(listener, fixture_id=fixture_id, key=key, windows_argv=windows_argv,
                      windows_pins=windows_pins, wsl_argv=wsl_argv,
                      exclusion_held=exclusion_held, connect=connect, system_id=plan['system_id'],
                      cluster=plan['cluster'], ready=lambda expiry: ready(port, expiry), deadline=deadline)


def fixture_documents(plan, *, fixture_id, port, key, expiry, retained_system_ids, ca_sha256, now):
    """Build immutable SCM input bytes from the observer's actual ready record."""
    require(re.fullmatch(r'[0-9a-f]{32}', fixture_id) and type(key) is bytes and len(key) == 32
            and type(port) is int and 49152 <= port <= 65535
            and type(expiry) is int and 2 < expiry - now < 660
            and retained_system_ids and str(plan['system_id']) not in retained_system_ids
            and re.fullmatch(r'[0-9a-f]{64}', ca_sha256))

    from datetime import timezone
    from hashlib import sha256

    def encode(value):
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')

    observer = encode({'fixture_id': fixture_id, 'port': port, 'key_sha256': sha256(key).hexdigest()})
    fixture = encode({'scope': SCOPE, 'backend': plan, 'retained_system_ids': sorted(retained_system_ids),
                      'ca_sha256': ca_sha256, 'fixture_id': fixture_id, 'observer_sha256': sha256(observer).hexdigest(),
                      'token_expiry': datetime.fromtimestamp(expiry, timezone.utc).isoformat()})
    approval = encode({'scope': SCOPE, 'plan_sha256': sha256(fixture).hexdigest(), 'expires_unix': expiry + 120})
    return {'observer.json': observer, 'plan.json': fixture, 'approval.json': approval}


def live_handlers(*, windows_snapshot, windows_pins, wsl_snapshot, exclusion_held, writes):
    def observe(parameters, deadline):
        require(not parameters)
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(windows_snapshot, deadline)
            try:
                wsl = wsl_snapshot('inventory', deadline)
                require(wsl == {'inventory_verified': True, 'active_token_status': 200})
            except (OSError, ValueError, TransitionBlocked):
                raise TransitionBlocked('isolated_wsl_inventory_unconfirmed') from None
            try:
                windows = pending.result(timeout=max(0, deadline - time.monotonic()))
                verify_windows_snapshot(windows, windows_pins)
            except (OSError, ValueError, TransitionBlocked):
                raise TransitionBlocked('isolated_windows_inventory_unconfirmed') from None
        return {'windows_fixture_observed': True, 'wsl_fixture_observed': True, 'active_token_accepted': True}

    def exclusion(parameters, deadline):
        require(not parameters)
        return {'held': exclusion_held(deadline) is True}

    def previous(parameters, deadline):
        require(not parameters)
        result = wsl_snapshot('previous-token-status', deadline)
        require(set(result) == {'status'} and result['status'] in (200, 401))
        return result

    return {'observe-isolated-writers': observe, 'check-isolated-exclusion': exclusion,
            'probe-previous-token': previous, 'check-isolated-writes': writes}


def check_isolated_writes(parameters, deadline, *, connect, system_id, cluster):
    """Probe guarded DML with an app login and always roll it back."""
    require(set(parameters) == {'expiry', 'writes_open'} and type(parameters['writes_open']) is bool)
    expected = parameters['writes_open']
    require((type(parameters['expiry']) is str) if expected else parameters['expiry'] is None)
    expiry = datetime.fromisoformat(parameters['expiry']) if expected else None
    require(expiry is None or expiry.utcoffset() is not None)
    accepted = {}
    for node, suffix in (('A', 'a'), ('A', 'b'), ('B', 'b')):
        require(time.monotonic() < deadline)
        role = 'book_memo_' + suffix
        with connect(node, role) as connection:
            try:
                connection.execute("SET LOCAL statement_timeout='400ms'")
                identity = connection.execute(
                    "SELECT current_database(), current_setting('cluster_name'), "
                    "pg_is_in_recovery(), session_user"
                ).fetchone()
                require(identity == ('ha_lease_rehearsal', 'ha_' + node, node == 'B', role))
                require(connection.execute(
                    'SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()'
                ).fetchone() == (True,))
                # This function exposes the fixture system ID and current
                # authority to the app role without granting catalog writes.
                while True:
                    stamp, sampled_at = connection.execute('SELECT ha.observe(), clock_timestamp()').fetchone()
                    require(stamp['system_id'] == system_id and stamp['cluster'] == cluster)
                    if node != 'B' or expected or not stamp['writes_open']:
                        break
                    require(time.monotonic() + 0.02 < deadline)
                    time.sleep(0.02)
                require(stamp['system_id'] == system_id and stamp['cluster'] == cluster
                        and stamp['writes_open'] is expected and stamp['history_safe'] is expected
                        and stamp['epoch'] == 1 and stamp['primary'] == 'A' and stamp['partner'] == 'B'
                        and stamp['serving'] == ['A'] and stamp['worker'] == 'A')
                if expected:
                    require(stamp['valid'] is True and 0 < stamp['remaining'] <= LEASE_SECONDS
                            and abs((expiry - sampled_at).total_seconds() - stamp['remaining']) < 0.1)
                try:
                    connection.execute(
                        'INSERT INTO s_book_memo_main.ha_receipts(receipt,payload_hash,result_hash) VALUES (%s,%s,%s)',
                        (secrets.token_hex(32), 'a' * 64, 'b' * 64),
                    )
                    connection.execute('SET CONSTRAINTS ALL IMMEDIATE')
                    accepted[node, suffix] = True
                except Exception as error:
                    rejected = getattr(error, 'sqlstate', None)
                    if (rejected == '25006' and node == 'B') or (
                        rejected == '42501' and getattr(getattr(error, 'diag', None), 'message_primary', None)
                        == 'enforced_writer_denied'
                    ):
                        accepted[node, suffix] = False
                    else:
                        raise
            finally:
                connection.rollback()
    return {'a_write_accepted': accepted['A', 'a'],
            'b_write_rejected': not accepted['A', 'b'] and not accepted['B', 'b']}


def serve(listener, *, fixture_id, key, handlers, deadline):
    require(listener.getsockname()[0] == '127.0.0.1'
            and re.fullmatch(r'[0-9a-f]{32}', fixture_id) and len(key) == 32)
    require(set(handlers) == {'observe-isolated-writers', 'check-isolated-exclusion',
                              'probe-previous-token', 'check-isolated-writes'})
    seen = set()
    while time.monotonic() < deadline:
        listener.settimeout(deadline - time.monotonic())
        try:
            connection, peer = listener.accept()
        except TimeoutError:
            return
        with connection:
            operation = None
            started = time.monotonic()
            try:
                require(peer[0] == '127.0.0.1')
                stop = min(deadline, time.monotonic() + 1)
                frame = bytearray()
                while not frame.endswith(b'\n'):
                    remaining = stop - time.monotonic()
                    require(remaining > 0 and len(frame) < MAX_FRAME)
                    connection.settimeout(remaining)
                    chunk = connection.recv(MAX_FRAME - len(frame))
                    require(bool(chunk))
                    frame.extend(chunk)
                request = strict_json(frame)
                require(type(request) is dict and set(request) == {'challenge', 'mac'}
                        and type(request['mac']) is str and type(request['challenge']) is dict)
                challenge = request['challenge']
                raw = json.dumps(challenge, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')
                require(hmac.compare_digest(request['mac'], hmac.digest(key, raw, 'sha256').hex()))
                require(set(challenge) == {'scope', 'fixture_id', 'operation', 'nonce', 'parameters', 'budget_ms'}
                        and challenge['scope'] == SCOPE and challenge['fixture_id'] == fixture_id
                        and type(challenge['operation']) is str and challenge['operation'] in handlers
                        and type(challenge['nonce']) is str and re.fullmatch(r'[0-9a-f]{64}', challenge['nonce'])
                        and type(challenge['parameters']) is dict
                        and type(challenge['budget_ms']) is int and 0 < challenge['budget_ms'] <= 1900)
                require(challenge['nonce'] not in seen and len(seen) < 4096)
                seen.add(challenge['nonce'])
                operation = challenge['operation']
                stop = min(deadline, started + challenge['budget_ms'] / 1000)
                result = handlers[challenge['operation']](challenge['parameters'], stop)
                require(type(result) is dict and time.monotonic() < stop)
                body = {'challenge': challenge, 'result': result}
                raw = json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')
                response = json.dumps(body | {'mac': hmac.digest(key, raw, 'sha256').hex()},
                                      separators=(',', ':'), allow_nan=False).encode('ascii')
                require(len(response) < MAX_FRAME)
                connection.settimeout(stop - time.monotonic())
                connection.sendall(response + b'\n')
            except (OSError, ValueError, TypeError, KeyError, TransitionBlocked) as error:
                if operation is not None:
                    reason = str(error) if isinstance(error, TransitionBlocked) else type(error).__name__
                    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,95}', reason):
                        reason = type(error).__name__
                    print(json.dumps({'state': 'fixture_request_failed', 'operation': operation,
                                      'reason': reason,
                                      'elapsed_ms': round((time.monotonic() - started) * 1000)}),
                          flush=True)
                continue
