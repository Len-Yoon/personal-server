from __future__ import annotations

import ipaddress
from typing import Mapping


LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"}
PUBLIC_PORTAL_URL = "https://portal.len.pe.kr"
PUBLIC_SERVICE_URLS = {
    "NEWS_SERVICE_URL": "https://news.len.pe.kr",
    "YOUTUBE_MEMO_URL": "https://memo.len.pe.kr",
    "BOOK_MEMO_URL": "https://books.len.pe.kr",
}
LOCAL_SERVICE_URLS = {
    "NEWS_SERVICE_URL": "http://127.0.0.1:8001",
    "YOUTUBE_MEMO_URL": "http://127.0.0.1:8002",
    "BOOK_MEMO_URL": "http://127.0.0.1:8003",
}


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


def service_url(env_name: str, host: str, fallback: str | None = None) -> str:
    base_urls = LOCAL_SERVICE_URLS if is_local_host(host) else PUBLIC_SERVICE_URLS
    configured = (fallback or "").strip()

    if configured:
        if is_local_host(host) and any(local in configured for local in LOCAL_HOSTS):
            return configured
        if not is_local_host(host):
            return configured

    return base_urls[env_name]


def service_base_urls(host: str) -> dict[str, str]:
    return LOCAL_SERVICE_URLS if is_local_host(host) else PUBLIC_SERVICE_URLS
