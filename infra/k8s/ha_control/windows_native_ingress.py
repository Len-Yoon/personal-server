"""Stage Caddy and cloudflared routes for a pinned Windows ingress candidate."""

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re

from . import caddy_cutover, car_callback_ingress
from .controller import TransitionBlocked
from .initial_pg_lease import RECOVERY_OPEN_REVISION
from .native_guest_activation import INITIAL_GENERATION, ServingGeneration


MAIN_ORIGIN_PORT = 18080
CAR_ORIGIN_PORT = 18015
LOOPBACK = "127.0.0.1"
WINDOWS_TLS = "C:/ProgramData/N100HA/ingress/tls"
ROUTE_HEADER = "X-N100-Ingress-Route"

LEGACY_OPTIONS = "{\n    email {env.CADDY_EMAIL}\n}\n"
LEGACY_TLS = """(common_tls) {
    tls {
        dns cloudflare {env.CLOUDFLARE_API_TOKEN}
        resolvers 1.1.1.1 1.0.0.1
    }
    encode zstd gzip
}
"""
UPLOAD_LIMIT = """(portal_file_upload_limit) {
    @file_uploads path /files/uploads
    request_body @file_uploads {
        max_size 30MB
    }
}
"""
NATIVE_OPTIONS = """{
    admin off
    auto_https off
    persist_config off
    log {
        output discard
    }
}
"""
NATIVE_ENCODING = "(common_tls) {\n    encode zstd gzip\n}\n"
PUBLIC_ROUTES = {
    "portal.len.pe.kr": None,
    "len.pe.kr": "PORTAL_UPSTREAM",
    "portfolio.len.pe.kr": "PORTAL_UPSTREAM",
    "file.len.pe.kr": "PORTAL_UPSTREAM",
    "admin.len.pe.kr": "PORTAL_UPSTREAM",
    "news.len.pe.kr": "CRAWLER_WORKER_UPSTREAM",
    "memo.len.pe.kr": "YOUTUBE_MEMO_UPSTREAM",
    "books.len.pe.kr": "BOOK_MEMO_UPSTREAM",
}
PIN = re.compile(r"[a-f0-9]{64}")
SITE = re.compile(
    r"(?P<host>[a-z0-9.-]+) \{\n"
    r"    import common_tls\n"
    r"(?P<limit>    import portal_file_upload_limit\n)?"
    r"    (?:reverse_proxy \{env\.(?P<upstream>[A-Z_]+)\}|redir https://len\.pe\.kr\{uri\} permanent)\n"
    r"\}\n(?:\n|$)"
)


@dataclass(frozen=True)
class WindowsIngressCandidate:
    caddyfile: bytes
    tunnel_ingress: bytes
    route_pin: str


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def _validate_routes(source):
    prefix = "\n".join((LEGACY_OPTIONS, LEGACY_TLS, UPLOAD_LIMIT, ""))
    if not source.startswith(prefix):
        raise TransitionBlocked("native_ingress_source_shape_changed")

    hosts = set()
    position = len(prefix)
    while position < len(source):
        block = SITE.match(source, position)
        if block is None:
            raise TransitionBlocked("native_ingress_source_shape_changed")
        host, limit, upstream = block.groups()
        if (host in hosts or host not in PUBLIC_ROUTES or PUBLIC_ROUTES[host] != upstream
                or bool(limit) != (upstream == "PORTAL_UPSTREAM")):
            raise TransitionBlocked("native_ingress_host_contract_changed")
        hosts.add(host)
        position = block.end()

    if hosts != PUBLIC_ROUTES.keys():
        raise TransitionBlocked("native_ingress_host_contract_changed")


def render_candidate(source, *, source_sha256, active_ip, generation, plan_sha256, tls_files_sha256,
                     recovery_attempt_id=None):
    """Produce only route files. The caller must independently verify live authority and mTLS files."""
    if (type(generation) is not ServingGeneration or type(active_ip) is not str
            or any(type(pin) is not str or not PIN.fullmatch(pin) for pin in (plan_sha256, tls_files_sha256))):
        raise TransitionBlocked("native_ingress_scope_invalid")
    if recovery_attempt_id is None:
        generation.validate()
    elif (type(recovery_attempt_id) is not str or not PIN.fullmatch(recovery_attempt_id)
          or generation.primary != INITIAL_GENERATION.primary
          or any(type(value) is not int for value in (generation.epoch, generation.timeline, generation.revision))
          or (generation.epoch, generation.timeline, generation.revision)
          != (INITIAL_GENERATION.epoch, INITIAL_GENERATION.timeline, RECOVERY_OPEN_REVISION)):
        raise TransitionBlocked("native_ingress_recovered_generation_invalid")

    main = caddy_cutover.render(source, source_sha256, active_ip).decode("utf-8").replace("\r\n", "\n")
    _validate_routes(source.decode("utf-8").replace("\r\n", "\n"))
    pin = sha256(_canonical({"generation": asdict(generation), "plan_sha256": plan_sha256,
                             "source_sha256": source_sha256, "active_ip": active_ip,
                             "tls_files_sha256": tls_files_sha256,
                             "recovery_attempt_id": recovery_attempt_id})).hexdigest()

    main = main.replace(LEGACY_OPTIONS, NATIVE_OPTIONS, 1).replace(LEGACY_TLS, NATIVE_ENCODING, 1)
    marker = "\n".join([
        "    @native_health {", "        method GET", "        path /health", "    }",
        "    header @native_health {", f"        {ROUTE_HEADER} {pin}",
        "        Cache-Control no-store", "        match status 200", "    }",
    ])
    for host in sorted(PUBLIC_ROUTES):
        main = main.replace(f"\n{host} {{\n", f"\nhttp://{host}:{MAIN_ORIGIN_PORT} {{\n"
                            f"    bind {LOOPBACK}\n{marker}\n", 1)
    main += f"\nhttp://:{MAIN_ORIGIN_PORT} {{\n    bind {LOOPBACK}\n    respond 404\n}}\n"

    car_host = car_callback_ingress.PUBLIC_HOST
    _, car = car_callback_ingress.render(active_ip, route_pin=pin).decode("ascii").split("\n\n", 1)
    car = car.replace(f":8015 {{\n", f":{CAR_ORIGIN_PORT} {{\n    bind {LOOPBACK}\n")
    config = (main + "\n" + car).replace(caddy_cutover.MOUNT, WINDOWS_TLS).encode("ascii")

    ingress = [
        {"hostname": host, "service": f"http://{LOOPBACK}:{port}", "originRequest": {"httpHostHeader": host}}
        for host, port in [(host, MAIN_ORIGIN_PORT) for host in sorted(PUBLIC_ROUTES)] + [(car_host, CAR_ORIGIN_PORT)]
    ]
    ingress.append({"service": "http_status:404"})
    return WindowsIngressCandidate(config, _canonical({"ingress": ingress}) + b"\n", pin)
