"""Render the existing Tunnel origin through the active Car Care VM."""

import hashlib
from http.client import HTTPSConnection, HTTPException
import ipaddress
import re
import secrets

from .caddy_cutover import MOUNT
from .controller import TransitionBlocked


PUBLIC_HOST = "car.len.pe.kr"
ROUTE_HEADER = "X-N100-Car-Route"


def _route_pin(value):
    if type(value) is not str or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise TransitionBlocked("car_callback_route_pin_invalid")
    return value


def render(active_ip, *, route_pin=None):
    """Serve only the registered Hyundai callback paths through guest mTLS."""
    try:
        ip = ipaddress.IPv4Address(active_ip)
    except (ipaddress.AddressValueError, TypeError):
        raise TransitionBlocked("car_callback_address_unconfirmed") from None
    if (not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified
            or ip.is_multicast or ip.is_reserved):
        raise TransitionBlocked("car_callback_address_unconfirmed")
    health_marker = ""
    if route_pin is not None:
        health_marker = (f"    @health {{\n        method GET\n        path /health\n    }}\n"
                         f"    header @health {{\n        {ROUTE_HEADER} {_route_pin(route_pin)}\n"
                         "        Cache-Control no-store\n        match status 200\n    }\n")

    return f"""{{
    admin off
    auto_https off
    persist_config off
    log {{
        output discard
    }}
}}

http://{PUBLIC_HOST}:8015 {{
{health_marker}    @allowed {{
        expression `(method('GET') && path('/health', '/oauth/hyundai/callback', '/oauth/hyundai/data-redirect')) || (method('POST') && path('/oauth/hyundai/data-redirect'))`
    }}
    handle @allowed {{
        reverse_proxy https://{ip}:10443 {{
            header_up Host car-care-worker.ha.internal
            transport http {{
                tls_server_name car-care-worker.ha.internal
                tls_trust_pool file {MOUNT}/server-ca.crt
                tls_client_auth {MOUNT}/client.crt {MOUNT}/client.key
                dial_timeout 2s
            }}
        }}
    }}
    respond 404
}}

http://:8015 {{
    respond 404
}}
""".encode("ascii")


def verify_public_origin(route_pin):
    """Prove public HTTPS reached the marked origin and its healthy VM upstream."""
    expected = _route_pin(route_pin)
    nonce = secrets.token_hex(16)
    connection = HTTPSConnection(PUBLIC_HOST, timeout=5)
    try:
        connection.request("GET", f"/health?ha_route_check={nonce}", headers={"Cache-Control": "no-cache"})
        with connection.getresponse() as response:
            markers = response.headers.get_all(ROUTE_HEADER, [])
            if response.status != 200 or markers != [expected] or response.read(3) != b"ok":
                raise TransitionBlocked("car_callback_public_origin_unconfirmed")
    except (OSError, HTTPException):
        raise TransitionBlocked("car_callback_public_origin_unconfirmed") from None
    finally:
        connection.close()
    return hashlib.sha256(f"{PUBLIC_HOST}\n{expected}\n{nonce}\n".encode("ascii")).hexdigest()
