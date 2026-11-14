"""SSRF & egress URL validation for repository remotes, model gateways, and webhooks."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_METADATA_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata",
        "169.254.169.254",
        "fd00:ec2::254",
    }
)


def _is_forbidden_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_local: bool) -> bool:
    if ip_obj.is_link_local or ip_obj.is_multicast or ip_obj.is_unspecified:
        return True
    if not allow_local and (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_reserved):
        return True
    return False


def validate_external_http_url(
    url: str,
    *,
    allow_local: bool = False,
    resolve_dns: bool = False,
) -> str:
    """Validate an outbound HTTP(S) URL against SSRF, embedded credentials, and metadata endpoints."""
    cleaned = (url or "").strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme {parsed.scheme!r}; only http/https allowed.")
    if not allow_local and parsed.scheme != "https":
        raise ValueError("Outbound URLs in staging/production must use https://.")
    if not parsed.hostname:
        raise ValueError("URL must include a hostname.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL must not contain embedded username/password credentials.")

    host = parsed.hostname.lower()
    if host in _METADATA_HOSTS:
        raise ValueError(f"Blocked metadata host: {host!r}.")
    if not allow_local and host in ("localhost", "localhost.localdomain"):
        raise ValueError("Localhost endpoints are blocked outside local development mode.")

    try:
        ip_obj = ipaddress.ip_address(host)
        if _is_forbidden_ip(ip_obj, allow_local=allow_local):
            raise ValueError(f"Blocked IP address {host!r}.")
    except ValueError as exc:
        if "Blocked" in str(exc):
            raise
        if resolve_dns:
            try:
                infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
                for _, _, _, _, sockaddr in infos:
                    resolved_ip = ipaddress.ip_address(sockaddr[0])
                    if _is_forbidden_ip(resolved_ip, allow_local=allow_local):
                        raise ValueError(f"Hostname {host!r} resolved to blocked IP {resolved_ip}.")
            except socket.gaierror:
                pass

    return cleaned.rstrip("/")
