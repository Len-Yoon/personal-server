"""Broker wrapper around the canonical vehicle client, with private PG state."""
import hashlib
import http.client
import io
from pathlib import Path
import ssl
import socket
import sys
import threading
from urllib.parse import urlsplit

from ha_runtime.effects import ProviderAck
from .providers import ProviderUnavailable, remaining


def canonical_client_type():
    source = Path(__file__).resolve().parent.parent / '_providers/car'
    expected = source / 'app/services/hyundai.py'
    if not expected.is_file() or expected.is_symlink():
        raise ProviderUnavailable()
    prior = sys.modules.get('app.services.hyundai')
    if prior is not None and Path(prior.__file__).resolve() != expected:
        raise ProviderUnavailable()
    package = sys.modules.get('app')
    if package is not None and Path(package.__file__).resolve() != source / 'app/__init__.py':
        raise ProviderUnavailable()
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    from app.services.hyundai import HyundaiClient
    if Path(sys.modules[HyundaiClient.__module__].__file__).resolve() != expected:
        raise ProviderUnavailable()
    return HyundaiClient


class BoundRequestTransport:
    """Fixed trusted origins, no redirects, one send, bounded response/deadline."""
    def __init__(self, origins, deadline, *, context=None, server_sha256=None):
        self.origins = frozenset(origins)
        self.deadline = deadline
        self.context = context
        self.server_sha256 = server_sha256

    def __call__(self, request, *, timeout):
        connection = None
        timer = None
        try:
            url = urlsplit(request.full_url)
            if (url.scheme != 'https' or (url.hostname, url.port or 443) not in self.origins
                    or url.username or url.password or url.fragment or request.get_method() not in {'GET','POST'}):
                raise ProviderUnavailable()
            body = request.data
            if body is not None and (type(body) is not bytes or len(body) > 65536):
                raise ProviderUnavailable()
            context = self.context or ssl.create_default_context()
            if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
                raise ProviderUnavailable()
            context.minimum_version = max(context.minimum_version, ssl.TLSVersion.TLSv1_2)
            connection = http.client.HTTPSConnection(url.hostname, url.port or 443,
                timeout=remaining(timeout, self.deadline), context=context)
            connection.connect()
            if (self.server_sha256 is not None
                    and hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest() != self.server_sha256):
                raise ProviderUnavailable()
            connection.sock.settimeout(remaining(timeout, self.deadline))
            wire = connection.sock
            def expire():
                try:
                    wire.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            timer = threading.Timer(remaining(timeout,self.deadline),expire)
            timer.daemon = True
            timer.start()
            path = url.path + ('?' + url.query if url.query else '')
            connection.request(request.get_method(), path, body, dict(request.header_items()))
            response = connection.getresponse()
            remaining(timeout, self.deadline)
            if response.status != 200:
                raise ProviderUnavailable()
            output = io.BytesIO()
            while True:
                # HTTP/1.0/Connection:close can close the saved socket on the last body byte.
                if response.isclosed() is True:
                    remaining(timeout, self.deadline)
                    if type(response.length) is int and response.length > 0:
                        raise ProviderUnavailable()
                    break
                wire.settimeout(remaining(timeout, self.deadline))
                chunk = response.read1(65536)
                remaining(timeout, self.deadline)
                if not chunk:
                    if type(response.length) is int and response.length > 0:
                        raise ProviderUnavailable()
                    break
                output.write(chunk)
                if output.tell() > 1048576:
                    raise ProviderUnavailable()
            output.seek(0)
            output.headers = response.headers
            output.status = response.status
            return output
        except ProviderUnavailable:
            raise
        except Exception:
            raise ProviderUnavailable() from None
        finally:
            if timer is not None:
                timer.cancel()
                timer.join(timeout=0.1)
            if connection is not None:
                connection.close()


class HyundaiProvider:
    def __init__(self, broker, *, client_id, client_secret, vehicle_id=None, redirect_uri=None,
                 oauth_base_url='https://prd.kr-ccapi.hyundai.com/api/v1/user/oauth2',
                 api_base_url='https://dev.kr-ccapi.hyundai.com/api/v1'):
        # This canonical package is copied from the exact app source into the
        # dedicated utility. No application-owned writable path is imported.
        HyundaiClient = canonical_client_type()
        self._local = threading.local()
        origins = []
        for base in (oauth_base_url, api_base_url):
            url = urlsplit(base)
            if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ProviderUnavailable()
            origins.append((url.hostname, url.port or 443))
        self._origins = frozenset(origins)
        self._client = HyundaiClient(client_id=client_id, client_secret=client_secret,
            vehicle_id=vehicle_id, redirect_uri=redirect_uri, oauth_base_url=oauth_base_url,
            api_base_url=api_base_url, state_backend=broker.credential_backend(), request_transport=self._open)

    def _open(self, request, *, timeout):
        deadline = getattr(self._local, 'deadline', None)
        if deadline is None:
            raise ProviderUnavailable()
        return BoundRequestTransport(self._origins, deadline)(request, timeout=timeout)

    def begin(self, payload, *, timeout, deadline):
        if payload != {}:
            raise ProviderUnavailable()
        self._local.deadline = deadline
        try:
            remaining(timeout, deadline)
            url = self._client.begin_authorization()
            remaining(timeout, deadline)
            return ProviderAck('hyundai-state:' + hashlib.sha256(url.encode()).hexdigest(), {'url': url})
        finally:
            del self._local.deadline

    def complete(self, payload, *, timeout, deadline):
        if (type(payload) is not dict or set(payload) != {'code','state'}
                or any(type(payload[key]) is not str or not 0 < len(payload[key]) <= 4096 for key in payload)):
            raise ProviderUnavailable()
        self._local.deadline = deadline
        try:
            remaining(timeout, deadline)
            self._client.complete_authorization(payload['code'], payload['state'])
            remaining(timeout, deadline)
            reference = hashlib.sha256((payload['code'] + '\0' + payload['state']).encode()).hexdigest()
            return ProviderAck('hyundai-connected:' + reference, {'connected': True})
        finally:
            del self._local.deadline

    def snapshot(self, payload, *, timeout, deadline):
        if payload != {}:
            raise ProviderUnavailable()
        self._local.deadline = deadline
        try:
            remaining(timeout, deadline)
            response = self._client.fetch_snapshot()
            remaining(timeout, deadline)
            if response.status == 'disabled':
                return ProviderAck('hyundai-disabled', {'status': 'disabled','snapshot':None})
            if response.status != 'success' or response.snapshot is None:
                raise ProviderUnavailable()
            value = response.snapshot
            snapshot = {'observed_at':value.observed_at.isoformat(), 'odometer_km':value.odometer_km,
                        'dte_km':value.dte_km,'warnings':sorted(value.warnings),
                        'unknown_warnings':sorted(value.unknown_warnings)}
            return ProviderAck('hyundai-snapshot:' + hashlib.sha256(value.observed_at.isoformat().encode()).hexdigest(),
                               {'status':'ok','snapshot':snapshot})
        finally:
            del self._local.deadline
