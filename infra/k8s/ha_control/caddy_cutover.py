"""Render the HA cutover candidate for the four public Caddy backends."""
import hashlib
import ipaddress
import re

from .controller import TransitionBlocked

UPSTREAMS = {
    "PORTAL_UPSTREAM": ("portal-web", 4),
    "BOOK_MEMO_UPSTREAM": ("book-memo", 1),
    "YOUTUBE_MEMO_UPSTREAM": ("youtube-memo", 1),
    "CRAWLER_WORKER_UPSTREAM": ("crawler-worker", 1),
}
MOUNT = "/run/n100-ha/caddy-mtls"
LINE = re.compile(r"^( +)reverse_proxy \{env\.([A-Z_]+)\}$", re.MULTILINE)


def render(source, expected_sha256, active_ip):
    """Replace only known upstreams, preserving the public HTTP Host."""
    if type(source) is not bytes or hashlib.sha256(source).hexdigest() != expected_sha256:
        raise TransitionBlocked("caddy_cutover_source_unconfirmed")
    try:
        ip = ipaddress.IPv4Address(active_ip)
        original = source.decode("utf-8")
    except (ValueError, TypeError):
        raise TransitionBlocked("caddy_cutover_input_invalid") from None
    crlf = "\r\n" in original
    if crlf:
        without_pairs = original.replace("\r\n", "")
        if "\r" in without_pairs or "\n" in without_pairs:
            raise TransitionBlocked("caddy_cutover_input_invalid")
        original = original.replace("\r\n", "\n")
    if (not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified
            or ip.is_multicast or ip.is_reserved or "\r" in original or not original.endswith("\n")):
        raise TransitionBlocked("caddy_cutover_input_invalid")
    replaced = dict.fromkeys(UPSTREAMS, 0)

    def apply(match):
        indent, key = match.groups()
        if key not in UPSTREAMS:
            return match.group()
        service, _ = UPSTREAMS[key]
        replaced[key] += 1
        return "\n".join([
            f"{indent}reverse_proxy https://{ip}:10443 {{",
            f"{indent}    header_up Host {{hostport}}",
            f"{indent}    transport http {{",
            f"{indent}        tls_server_name {service}.ha.internal",
            f"{indent}        tls_trust_pool file {MOUNT}/server-ca.crt",
            f"{indent}        tls_client_auth {MOUNT}/client.crt {MOUNT}/client.key",
            f"{indent}    }}",
            f"{indent}}}",
        ])

    candidate = LINE.sub(apply, original)
    if any(replaced[key] != count for key, (_, count) in UPSTREAMS.items()):
        raise TransitionBlocked("caddy_cutover_route_set_changed")
    return (candidate.replace("\n", "\r\n") if crlf else candidate).encode("utf-8")
