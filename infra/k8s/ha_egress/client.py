"""One-attempt mTLS client. No proxies, redirects, credentials or retry fallback."""
import hashlib
import http.client
import json
import re
import ssl
import uuid
from urllib.parse import urlsplit


class BrokerUnavailable(OSError):
    def __init__(self):
        super().__init__("effect_broker_unavailable")


LARGE_READ_OPERATIONS = frozenset({'book.http.get','youtube.http.get','crawler.http.get'})


def response_limit(operation):
    # Result policy is enforced by Broker. Reserve bounded envelope/ack space.
    return (3 * 1024 * 1024 if operation in LARGE_READ_OPERATIONS else 65536) + 2048


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise BrokerUnavailable()
        value[key] = item
    return value


def validate_binding(config):
    if (type(config) is not dict or set(config) != {"url", "ca", "certificate", "key", "server_sha256", "plan_sha256"}
            or any(type(config[key]) is not str or not re.fullmatch(r"[0-9a-f]{64}", config[key])
                   for key in ("server_sha256", "plan_sha256"))):
        raise BrokerUnavailable()
    url = urlsplit(config["url"])
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", url.hostname)
            or not 1 <= (url.port or 8443) <= 65535 or url.netloc.endswith(":")
            or url.path not in {"", "/"} or url.query or url.fragment):
        raise BrokerUnavailable()
    return url


def configuration(service):
    """Return a signed enforced endpoint, or None for the existing local mode."""
    try:
        from ha_runtime import config
        if not config.enabled():
            return None
        value = config.load(service)
        if value.get('profile') != 'enforced-vm-v1':
            return None
        if 'effects' not in value and value['scope'] == 'local-isolated':
            # The isolated DB-only fixture uses provider doubles. It does not
            # attest production egress or credential ownership.
            return None
        endpoint = value.get('effects')
        validate_binding(endpoint)
        return endpoint
    except BrokerUnavailable:
        raise
    except Exception:
        raise BrokerUnavailable() from None


def route(service, operation, payload, *, request_id=None):
    endpoint = configuration(service)
    if endpoint is None:
        return None
    response = dispatch(endpoint, operation, request_id or uuid.uuid4().hex, payload)
    if (set(response) != {'status','provider_ack','result','replayed'}
            or response['status'] != 'completed' or type(response['provider_ack']) is not str
            or not 0 < len(response['provider_ack']) <= 256 or type(response['result']) is not dict
            or type(response['replayed']) is not bool):
        raise BrokerUnavailable()
    return response['result']


def dispatch(config, operation, request_id, payload):
    """Config must come from the release-bound trusted activation authority."""
    connection = None
    try:
        url = validate_binding(config)
        from ha_runtime.config import _authority_file, _private
        context = ssl.create_default_context(cafile=str(_authority_file(config["ca"])))
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.load_cert_chain(str(_authority_file(config["certificate"])), str(_private(config["key"])))
        body = json.dumps({"operation": operation, "request_id": request_id, "payload": payload},
                          sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if not 0 < len(body) <= 65536:
            raise BrokerUnavailable()
        connection = http.client.HTTPSConnection(url.hostname, url.port or 8443, context=context, timeout=2)
        connection.connect()
        if hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest() != config["server_sha256"]:
            raise BrokerUnavailable()
        connection.request("POST", "/v1/dispatch", body, {"Content-Type": "application/json",
                                                         "X-HA-Plan": config["plan_sha256"]})
        response = connection.getresponse()
        if (response.status != 200 or response.getheader("Content-Type") != "application/json"
                or response.getheader("Transfer-Encoding") is not None):
            raise BrokerUnavailable()
        limit = response_limit(operation)
        raw = response.read(limit + 1)
        if not 0 < len(raw) <= limit:
            raise BrokerUnavailable()
        result = json.loads(raw, object_pairs_hook=_pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(BrokerUnavailable()))
        if type(result) is not dict:
            raise BrokerUnavailable()
        return result
    except BrokerUnavailable:
        raise
    except Exception:
        # Never expose TLS paths, provider responses, tokens or transport errors.
        raise BrokerUnavailable() from None
    finally:
        if connection is not None:
            connection.close()
