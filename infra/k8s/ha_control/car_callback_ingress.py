"""Render the existing Tunnel origin through the active Car Care VM."""

import ipaddress

from .caddy_cutover import MOUNT
from .controller import TransitionBlocked


def render(active_ip):
    """Serve only the registered Hyundai callback paths through guest mTLS."""
    try:
        ip = ipaddress.IPv4Address(active_ip)
    except (ipaddress.AddressValueError, TypeError):
        raise TransitionBlocked("car_callback_address_unconfirmed") from None
    if (not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified
            or ip.is_multicast or ip.is_reserved):
        raise TransitionBlocked("car_callback_address_unconfirmed")

    return f"""{{
    admin off
    auto_https off
    persist_config off
    log {{
        output discard
    }}
}}

http://car.len.pe.kr:8015 {{
    @allowed {{
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
