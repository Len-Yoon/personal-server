from __future__ import annotations

import ipaddress
from typing import Mapping


LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"}
PUBLIC_PORTAL_URL = "https://len.pe.kr"


def request_host_from_headers(headers: Mapping[str, str]) -> str:
    forwarded_host = headers.get("x-forwarded-host", "").strip()
    if forwarded_host:
        return forwarded_host.split(",")[0].strip().lower()

    host = headers.get("host", "").strip()
    if host:
        return host.split(":")[0].strip().lower()

    return ""


def is_local_host(host: str) -> bool:
    host = host.strip().lower()
    if not host:
        return False

    host = host.split(":")[0]
    if host in LOCAL_HOSTS:
        return True

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False

    return ip.is_private or ip.is_loopback or ip.is_link_local


def portal_home_url(host: str) -> str:
    return "http://127.0.0.1:8000/" if is_local_host(host) else f"{PUBLIC_PORTAL_URL}/"
