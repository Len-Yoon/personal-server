from __future__ import annotations

import ipaddress
import os
from typing import Mapping


LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"}
SECOND_LEVEL_KR_DOMAINS = {"co", "or", "go", "ac", "ne", "re", "pe"}
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
LOCAL_FILE_URL = "http://127.0.0.1:8000/files"
LOCAL_ADMIN_URL = "http://127.0.0.1:8000/admin/status"
PUBLIC_FILE_URL = "https://file.len.pe.kr/"
PUBLIC_ADMIN_URL = "https://admin.len.pe.kr/"


def public_portal_url(host: str) -> str:
    configured = os.getenv("PORTAL_HOME_URL", "").strip().rstrip("/")

    if configured:
        return configured

    labels = host.strip().lower().split(".")

    if len(labels) >= 3 and labels[-1] == "kr" and labels[-2] in SECOND_LEVEL_KR_DOMAINS:
        return f"https://{'.'.join(labels[-3:])}"

    if len(labels) >= 2:
        return f"https://{'.'.join(labels[-2:])}"

    return "/"


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
    if is_local_host(host):
        return "http://127.0.0.1:8000/"

    base_url = public_portal_url(host)
    return "/" if base_url == "/" else f"{base_url}/"


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


def file_entry_url(host: str) -> str:
    return LOCAL_FILE_URL if is_local_host(host) else PUBLIC_FILE_URL


def admin_entry_url(host: str) -> str:
    return LOCAL_ADMIN_URL if is_local_host(host) else PUBLIC_ADMIN_URL
