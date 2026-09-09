"""Security helpers: SSRF prevention and ID validation.

Adapted from Shri AI's runtime/security.py (self-contained, no changes to
the actual logic — this exact SSRF guard is already proven and tested).
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

TENANT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_HTTP_RESPONSE_BYTES = 5 * 1024 * 1024

BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
    }
)

PRIVATE_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


class UnsafeUrlError(ValueError):
    """Raised when a URL targets a blocked or private network destination."""


class InvalidTenantIdError(ValueError):
    """Raised when a tenant identifier fails validation."""


def validate_tenant_id(tenant_id: str) -> str:
    if not tenant_id or not isinstance(tenant_id, str):
        raise InvalidTenantIdError("Tenant identifier must be a non-empty string.")
    candidate = tenant_id.strip().lower()
    if not TENANT_ID_PATTERN.fullmatch(candidate):
        raise InvalidTenantIdError(
            "Tenant identifier must be lowercase alphanumeric with - or _, 2-64 chars."
        )
    return candidate


def assert_public_http_url(url: str) -> None:
    """Reject URLs that may be used for SSRF against internal networks."""
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeUrlError("Only HTTP and HTTPS protocols are allowed.")
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeUrlError("URL hostname is required.")

    lowered = hostname.lower().rstrip(".")
    if lowered in BLOCKED_HOSTNAMES or lowered.endswith(".localhost"):
        raise UnsafeUrlError("Local or metadata hostnames are not allowed.")

    for address in _resolve_host_addresses(hostname):
        if _is_blocked_ip(address):
            raise UnsafeUrlError("Private or link-local addresses are not allowed.")


def read_limited_response(response, *, max_bytes: int = MAX_HTTP_RESPONSE_BYTES) -> bytes:
    """Read an HTTP response body up to a fixed byte limit."""
    chunks: list[bytes] = []
    total = 0
    while True:
        remaining = max_bytes - total
        if remaining <= 0:
            raise ValueError("HTTP response exceeds the maximum allowed size.")
        chunk = response.read(min(1024 * 1024, remaining + 1))
        if not chunk:
            break
        if len(chunk) > remaining:
            raise ValueError("HTTP response exceeds the maximum allowed size.")
        total += len(chunk)
        chunks.append(chunk)
    return b"".join(chunks)


def _resolve_host_addresses(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrlError("Hostname could not be resolved.") from exc

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        addresses.append(ipaddress.ip_address(sockaddr[0]))
    if not addresses:
        raise UnsafeUrlError("Hostname could not be resolved.")
    return addresses


def _is_blocked_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
    ):
        return True
    return any(address in network for network in PRIVATE_NETWORKS)
