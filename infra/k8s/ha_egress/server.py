"""Bounded mTLS dispatcher. Callers supply a trusted fingerprint identity map.

This library does not install firewall rules, migrate secrets or claim that an
app possessing a provider credential has been fenced. No listener starts on import.
"""
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import socket
import ssl
import threading
from types import MappingProxyType

from .client import _pairs, LARGE_READ_OPERATIONS, response_limit


class BrokerServer(HTTPServer):
    allow_reuse_address = False

    def __init__(self, address, context, identities, dispatch, *, plan_sha256, workers=4, large_read_operations=()):
        if (not isinstance(context, ssl.SSLContext) or context.verify_mode != ssl.CERT_REQUIRED
                or context.minimum_version < ssl.TLSVersion.TLSv1_3
                or not identities or type(workers) is not int or not 1 <= workers <= 8):
            raise ValueError("trusted_broker_transport_required")
        if (type(plan_sha256) is not str or len(plan_sha256) != 64
                or any(c not in "0123456789abcdef" for c in plan_sha256)):
            raise ValueError("trusted_broker_transport_required")
        self.plan_sha256 = plan_sha256
        self.large_read_operations = frozenset(large_read_operations)
        if not self.large_read_operations <= LARGE_READ_OPERATIONS:
            raise ValueError('trusted_broker_transport_required')
        self.identities = MappingProxyType(dict(identities))
        if any(type(key) is not str or len(key) != 64 or any(c not in "0123456789abcdef" for c in key)
               for key in self.identities):
            raise ValueError("trusted_broker_transport_required")
        self.dispatch = dispatch
        self.slots = threading.BoundedSemaphore(workers)
        self.active = set()
        self.active_lock = threading.Lock()
        super().__init__(address, Handler)
        self.socket = context.wrap_socket(self.socket, server_side=True, do_handshake_on_connect=False)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        def run():
            def expire():
                try:
                    request.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            deadline = threading.Timer(4, expire)
            deadline.daemon = True
            deadline.start()
            try:
                self.finish_request(request, address)
            except Exception:
                pass  # TLS/client disconnect errors never expose private details.
            finally:
                deadline.cancel()
                self.shutdown_request(request)
                with self.active_lock:
                    self.active.discard(threading.current_thread())
                self.slots.release()
        thread = threading.Thread(target=run, daemon=True)
        with self.active_lock:
            self.active.add(thread)
        try:
            thread.start()
        except Exception:
            with self.active_lock:
                self.active.discard(thread)
            self.slots.release()
            self.shutdown_request(request)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def setup(self):
        self.request.settimeout(2)
        self.request.do_handshake()
        super().setup()

    def log_message(self, *_):
        pass

    def do_POST(self):
        self.close_connection = True
        try:
            identity = self.server.identities[hashlib.sha256(self.connection.getpeercert(binary_form=True)).hexdigest()]
            lengths = self.headers.get_all("Content-Length") or []
            if (self.path != "/v1/dispatch" or len(lengths) != 1 or not lengths[0].isascii()
                    or not lengths[0].isdigit() or self.headers.get("Transfer-Encoding") is not None
                    or self.headers.get_all("Content-Type") != ["application/json"]
                    or self.headers.get_all("X-HA-Plan") != [self.server.plan_sha256]):
                raise ValueError()
            size = int(lengths[0])
            if not 0 < size <= 65536:
                raise ValueError()
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError()
            request = json.loads(raw, object_pairs_hook=_pairs,
                                 parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if type(request) is not dict or set(request) != {"operation", "request_id", "payload"}:
                raise ValueError()
            result = self.server.dispatch(identity, request)
            response = json.dumps(result, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            operation = request['operation'] if request['operation'] in self.server.large_read_operations else ''
            if len(response) > response_limit(operation):
                raise ValueError()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
        except Exception:
            # A failure after dispatch may mean the effect happened. Never retry.
            self.send_error(503, "effect_broker_unavailable")

    def send_error(self, code, message=None, explain=None):
        self.close_connection = True
        body = b'{"error":"effect_broker_unavailable"}\n'
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
