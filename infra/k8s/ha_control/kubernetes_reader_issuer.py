"""Issue credentials for the fixed N100 Kubernetes observer account."""

from datetime import datetime, timedelta, timezone
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import ssl
import stat
import sys

from .legacy_linux import Budget, verify_kubeconfig

TLS = Path('/var/lib/rancher/k3s/server/tls')
TOKEN_PATH = '/api/v1/namespaces/personal-server/serviceaccounts/n100-ha-writer-observer/token'
LIST_PATH = '/api/v1/namespaces/personal-server/pods?limit=1'
DURATION = 600


def request_token():
    budget = Budget()
    verify_kubeconfig(budget)
    context = ssl.create_default_context(cafile=TLS / 'server-ca.crt')
    context.load_cert_chain(TLS / 'client-admin.crt', TLS / 'client-admin.key')
    connection = http.client.HTTPSConnection('127.0.0.1', 6443, timeout=2, context=context)
    try:
        payload = {'apiVersion': 'authentication.k8s.io/v1', 'kind': 'TokenRequest',
                   'spec': {'expirationSeconds': DURATION}}
        connection.request('POST', TOKEN_PATH, json.dumps(payload), {'Content-Type': 'application/json'})
        with connection.getresponse() as response:
            if response.status != 201:
                raise ValueError('observer_token_request_failed')
            raw = response.read(32769)
            if len(raw) > 32768:
                raise ValueError('observer_token_response_invalid')
            status = json.loads(raw)['status']
    finally:
        connection.close()
    token = status['token']
    expiry = datetime.fromisoformat(status['expirationTimestamp'])
    remaining = expiry - datetime.now(timezone.utc)
    if (not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,16384}', token)
            or not timedelta(seconds=300) <= remaining <= timedelta(seconds=DURATION + 30)):
        raise ValueError('observer_token_response_invalid')

    # Probe with a fresh TLS context so the administrator certificate cannot
    # accidentally authenticate the observer request.
    context = ssl.create_default_context(cafile=TLS / 'server-ca.crt')
    connection = http.client.HTTPSConnection('127.0.0.1', 6443, timeout=2, context=context)
    try:
        connection.request('GET', LIST_PATH, headers={'Authorization': 'Bearer ' + token})
        with connection.getresponse() as response:
            if response.status != 200:
                raise ValueError('observer_token_probe_failed')
    finally:
        connection.close()
    return token


def publish(account, token):
    destination = Path(account.pw_dir) / '.local/state/n100-ha/kubernetes-reader'
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    directory = os.open('/', flags)
    try:
        for part in destination.parts[1:]:
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
            info = os.fstat(directory)
            if info.st_uid not in (0, account.pw_uid) or info.st_mode & 0o022:
                raise ValueError('observer_token_directory_untrusted')
        if info.st_uid != account.pw_uid or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('observer_token_directory_untrusted')

        name = '.token-' + secrets.token_hex(16)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(token.encode('ascii') + b'\n')
                stream.flush()
                os.fchown(stream.fileno(), account.pw_uid, account.pw_gid)
                os.fsync(stream.fileno())
            os.replace(name, 'token', src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass
    finally:
        os.close(directory)


def main():
    try:
        if os.geteuid() != 0 or len(sys.argv) != 2 or not sys.argv[1].isdecimal():
            raise ValueError('observer_token_issuer_identity_invalid')
        account = pwd.getpwuid(int(sys.argv[1]))
        if account.pw_uid == 0 or not Path(account.pw_dir).is_absolute() or '..' in Path(account.pw_dir).parts:
            raise ValueError('observer_token_issuer_identity_invalid')
        publish(account, request_token())
        return 0
    except Exception:
        print('N100 observer token renewal failed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
