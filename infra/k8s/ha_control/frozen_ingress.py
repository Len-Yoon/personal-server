"""One-attempt N100-internal migration channel through the isolated VM ingress."""
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler
import ipaddress
import os
from pathlib import Path
import re
import socket
from socketserver import UnixStreamServer
import ssl
import stat
import tempfile
import threading
import time

from ha_runtime import seven_capture

from .frozen_transport import END, MAGIC, receive_bundle, send_bundle
from .legacy_writers import TransitionBlocked, _need


HOST = "portal-web.ha.internal"
PORT = 10443
PATH = "/_ha/frozen-transfer"
SOCKET = Path("/run/n100-ha/ingress/portal-web/http.sock")
CHALLENGE = re.compile(r"[a-f0-9]{64}\Z")
OVERHEAD = len(MAGIC) + 8 + len(END)
TRANSFER_SECONDS = 600


def _authorize(challenge, max_bytes):
    _need(type(challenge) is str and CHALLENGE.fullmatch(challenge)
          and type(max_bytes) is int and 0 < max_bytes <= 8 * 1024**3,
          "frozen_ingress_binding_invalid")
    return "Bearer " + challenge


def _remaining(deadline):
    seconds = deadline - time.monotonic()
    _need(seconds > 0, "frozen_ingress_deadline_expired")
    return seconds


def _socket_deadline(stream, deadline):
    def stop():
        try:
            stream.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    timer = threading.Timer(_remaining(deadline), stop)
    timer.daemon = True
    timer.start()
    return timer


class _BoundedStream:
    def __init__(self, stream, budget, deadline):
        self.stream, self.budget, self.deadline = stream, budget, deadline

    def read(self, size):
        _remaining(self.deadline)
        if size > self.budget:
            return b""
        value = self.stream.read(size)
        _remaining(self.deadline)
        self.budget -= len(value)
        return value

    def write(self, value):
        _remaining(self.deadline)
        _need(len(value) <= self.budget, "frozen_ingress_size_invalid")
        self.budget -= len(value)
        return self.stream.write(value)

    def flush(self):
        self.stream.flush()


class _PinnedHttps(http.client.HTTPSConnection):
    def __init__(self, address, context, deadline):
        super().__init__(HOST, PORT, timeout=15, context=context)
        self.address = address
        self.deadline = deadline
        self.timer = None

    def connect(self):
        stream = socket.create_connection((self.address, PORT), min(self.timeout, _remaining(self.deadline)))
        try:
            self.sock = self._context.wrap_socket(stream, server_hostname=HOST, do_handshake_on_connect=False)
            self.timer = _socket_deadline(self.sock, self.deadline)
            self.sock.do_handshake()
        except Exception:
            stream.close()
            self.close()
            if self.timer is not None:
                self.timer.cancel()
            raise


def send_to_vm(source, manifest, address, credentials, challenge, *, max_bytes, assert_owned_target):
    deadline = time.monotonic() + TRANSFER_SECONDS
    authorization = _authorize(challenge, max_bytes)
    try:
        ip = ipaddress.IPv4Address(address)
    except (ValueError, TypeError):
        raise TransitionBlocked("frozen_ingress_destination_invalid") from None
    _need(ip.is_private and not any((ip.is_loopback, ip.is_link_local, ip.is_multicast, ip.is_unspecified,
                                    ip.is_reserved)),
          "frozen_ingress_destination_invalid")
    root = seven_capture.trusted(Path(credentials), directory=True)
    ca, cert, key = (seven_capture.trusted(root / name) for name in ("server-ca.crt", "client.crt", "client.key"))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls.load_verify_locations(cafile=str(ca))
    tls.minimum_version = ssl.TLSVersion.TLSv1_3
    tls.load_cert_chain(str(cert), str(key))
    frozen = seven_capture.trusted(Path(source), directory=True)
    with tempfile.TemporaryFile(dir=frozen) as body:
        wire_limit = max_bytes + OVERHEAD
        send_bundle(frozen, manifest, _BoundedStream(body, wire_limit, deadline), wire_limit=wire_limit)
        length = body.tell()
        _need(OVERHEAD < length <= max_bytes + OVERHEAD, "frozen_ingress_size_invalid")
        body.seek(0)
        assert_owned_target(str(ip), manifest)
        client = _PinnedHttps(str(ip), tls, deadline)
        try:
            client.request("POST", PATH, body, {"Host": HOST, "Authorization": authorization,
                                               "Content-Type": "application/octet-stream",
                                               "Content-Length": str(length), "Connection": "close"})
            with client.getresponse() as response:
                _need(response.status == 200 and response.read(65) == manifest.encode("ascii"),
                      "frozen_ingress_destination_unconfirmed")
            _remaining(deadline)
        except (OSError, http.client.HTTPException):
            raise TransitionBlocked("frozen_ingress_destination_unconfirmed") from None
        finally:
            client.close()
            if client.timer is not None:
                client.timer.cancel()
    return manifest


class _Receiver(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        self.request.settimeout(min(15, _remaining(self.server.deadline)))
        self.timer = _socket_deadline(self.request, self.server.deadline)
        try:
            super().setup()
        except Exception:
            self.timer.cancel()
            raise

    def finish(self):
        try:
            super().finish()
        finally:
            self.timer.cancel()

    def log_message(self, *unused):
        pass

    def _respond(self, status, body=b""):
        self.send_response_only(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.close_connection = True

    def handle_expect_100(self):
        self._respond(417)
        return False

    def do_POST(self):
        auth = self.headers.get_all("Authorization") or []
        length = self.headers.get_all("Content-Length") or []
        if (self.path != PATH or self.headers.get_all("Host") != [HOST]
                or len(auth) != 1 or not auth[0].isascii()
                or not hmac.compare_digest(auth[0], self.server.authorization)):
            self._respond(403)
            return
        if (len(length) != 1 or re.fullmatch(r"[1-9][0-9]{0,10}", length[0]) is None
                or self.headers.get_all("Content-Type") != ["application/octet-stream"]
                or "Transfer-Encoding" in self.headers or "Expect" in self.headers):
            self._respond(400)
            return
        size = int(length[0])
        if not OVERHEAD < size <= self.server.max_bytes + OVERHEAD:
            self._respond(413)
            return
        try:
            self.server.published = receive_bundle(self.server.destination, self.server.manifest,
                                                   _BoundedStream(self.rfile, size, self.server.deadline),
                                                   max_bytes=self.server.max_bytes, wire_bytes=size)
        except Exception:
            self.server.rejected = True
            self._respond(422)
            return
        self._respond(200, self.server.published.encode("ascii"))
        self.server.result = self.server.published


class _OneAttempt(UnixStreamServer):
    request_queue_size = 1

    def handle_error(self, *unused):
        self.rejected = True


def receive_once(destination, manifest, challenge, *, max_bytes, target_guard):
    """Receive under the caller's live VM app/authority exclusion."""
    with target_guard:
        return _receive_held(destination, manifest, challenge, max_bytes)


def _receive_held(destination, manifest, challenge, max_bytes):
    deadline = time.monotonic() + TRANSFER_SECONDS
    _need(os.geteuid() == 0, "frozen_ingress_owned_guest_required")
    authorization = _authorize(challenge, max_bytes)
    _need(type(manifest) is str and CHALLENGE.fullmatch(manifest), "frozen_ingress_binding_invalid")
    destination = Path(destination)
    seven_capture.trusted(destination.parent, directory=True)
    _need(not os.path.lexists(destination), "frozen_ingress_destination_exists")
    root = SOCKET.parent.parent
    _need(root.resolve(strict=True) == root, "frozen_ingress_socket_untrusted")
    root_info = root.lstat()
    _need(stat.S_ISDIR(root_info.st_mode) and root_info.st_uid == 0 and not root_info.st_mode & 0o022,
          "frozen_ingress_socket_untrusted")
    _need(not os.path.lexists(SOCKET.parent), "frozen_ingress_socket_untrusted")
    intent = destination.parent / "frozen-receive-intent.json"
    try:
        seven_capture._write(intent, seven_capture.encoded({
            "manifest_sha256": manifest, "challenge_sha256": hashlib.sha256(challenge.encode("ascii")).hexdigest(),
            "max_bytes": max_bytes,
        }))
    except FileExistsError:
        raise TransitionBlocked("frozen_ingress_reconciliation_required") from None
    fd = os.open(intent.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    SOCKET.parent.mkdir(mode=0o700)
    directory = SOCKET.parent.lstat()
    sock_identity = None
    previous_mask = os.umask(0o177)
    try:
        os.chown(SOCKET.parent, 0, 10004, follow_symlinks=False)
        os.chmod(SOCKET.parent, 0o750)
        with _OneAttempt(str(SOCKET), _Receiver) as server:
            info = SOCKET.lstat()
            _need(stat.S_ISSOCK(info.st_mode) and info.st_uid == 0, "frozen_ingress_socket_untrusted")
            sock_identity = info.st_dev, info.st_ino
            os.chown(SOCKET, 0, 10004, follow_symlinks=False)
            os.chmod(SOCKET, 0o660)
            server.authorization, server.manifest = authorization, manifest
            server.destination, server.max_bytes = destination, max_bytes
            server.result, server.published, server.rejected = None, None, False
            server.deadline = deadline
            server.timeout = _remaining(deadline)
            server.handle_request()
            _need(server.published is None or server.result == manifest, "frozen_ingress_ack_unconfirmed")
            _need(server.result == manifest and not server.rejected, "frozen_ingress_receive_unconfirmed")
    finally:
        os.umask(previous_mask)
        if sock_identity is not None:
            info = SOCKET.lstat()
            _need(stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == sock_identity,
                  "frozen_ingress_socket_reconciliation_required")
            SOCKET.unlink()
        current = SOCKET.parent.lstat()
        _need((current.st_dev, current.st_ino) == (directory.st_dev, directory.st_ino),
              "frozen_ingress_socket_reconciliation_required")
        SOCKET.parent.rmdir()
    return manifest
