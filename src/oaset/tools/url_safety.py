"""URL safety for web_fetch — SSRF floor that no toggle may disable.

`web_fetch` is the one read tool that can be pointed at an arbitrary host, so it
needs a floor independent of any prompt or policy: reaching the cloud metadata
endpoint or an internal admin panel is not something a user consents to by
asking for a page.

Always-blocked metadata IPs that survive every opt-out, explicit CGNAT
handling, and a **fail-closed** default when DNS cannot be resolved.

There is no rebinding window left: :func:`resolve_allowed_ips` resolves ONCE
and hands the checked addresses to web_fetch, which connects to one of them
instead of letting the HTTP client resolve the name a second time. A
per-query DNS server can no longer answer a public IP to the check and an
internal one to the connect — both use the same lookup. Redirects are
re-resolved and re-checked per hop the same way.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from typing import Any
from urllib.parse import urlparse

# Cloud metadata services. Compromising these hands over credentials, so they
# are blocked even when the user opts into private addresses.
_ALWAYS_BLOCKED_IPS = frozenset({
    ipaddress.ip_address("169.254.169.254"),  # AWS/GCP/Azure/DO/Oracle metadata
    ipaddress.ip_address("169.254.170.2"),    # AWS ECS task metadata (IAM creds)
    ipaddress.ip_address("169.254.169.253"),  # Azure IMDS wire server
    ipaddress.ip_address("fd00:ec2::254"),    # AWS metadata over IPv6
    ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud metadata
})

_ALLOWED_SCHEMES = ("http", "https")


def _env_allows_private() -> bool:
    return os.environ.get("OASET_ALLOW_PRIVATE_URLS", "").strip().lower() in (
        "1", "true", "yes", "on")


def _blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip in _ALWAYS_BLOCKED_IPS:
        return True
    # 100.64.0.0/10 (CGNAT) is not flagged private by ipaddress on all versions.
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def resolve_allowed_ips(url: str, *, allow_private: bool | None = None,
                        resolver: Any = None) -> tuple[list[str], str | None]:
    """Resolve ONCE and return (checked addresses, refusal-or-None).

    The caller connects to one of the returned addresses instead of letting
    its HTTP stack resolve the hostname again — that second lookup is the
    DNS-rebinding window (check passes on a public answer, connect dials the
    internal one). When ``allow_private`` opts in, private addresses are
    returned too; the metadata addresses stay refused regardless.

    Empty list + None refusal means "allowed, nothing to pin" (a literal
    localhost name resolves without a lookup the caller can pin against).
    """
    if resolver is None:
        resolver = socket.getaddrinfo   # late-bound: injectable in tests/policy
    if allow_private is None:
        allow_private = _env_allows_private()
    try:
        parsed = urlparse(url)
    except ValueError:
        return [], f"Blocked: could not parse URL '{url}'."
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return [], f"Blocked: unsupported scheme '{parsed.scheme}' (only http/https)."
    host = (parsed.hostname or "").strip()
    if not host:
        return [], "Blocked: URL has no host."
    if host.lower() in ("localhost", "localhost.localdomain"):
        if allow_private:
            return [], None
        return [], "Blocked: URL targets localhost (set [search] allow_private_urls to opt in)."

    try:
        infos = resolver(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        # Fail closed: an unresolvable host cannot be proven safe.
        return [], f"Blocked: could not resolve host '{host}'."

    allowed: list[str] = []
    for info in infos:
        address = info[4][0] if len(info) > 4 else ""
        try:
            ip = ipaddress.ip_address(address.split("%")[0])
        except ValueError:
            continue
        if ip in _ALWAYS_BLOCKED_IPS:
            return [], f"Blocked: '{host}' resolves to a cloud metadata address."
        if _blocked_ip(ip):
            if allow_private:
                allowed.append(str(ip))
            continue
        allowed.append(str(ip))
    if not allowed and infos:
        # every answer parsed, none allowed: say so instead of a vague pass
        return [], (
            f"Blocked: '{host}' has no fetchable address "
            "(set [search] allow_private_urls to opt in)."
        )
    return allowed, None


def check_url(url: str, *, allow_private: bool | None = None,
              resolver: Any = None) -> str | None:
    """Return a user-facing refusal string, or None when the URL is allowed.

    ``allow_private`` is the explicit opt-out (config
    ``[search] allow_private_urls`` / ``OASET_ALLOW_PRIVATE_URLS``) for people
    who genuinely fetch from localhost — the metadata addresses stay blocked
    regardless. When None, the environment decides.
    """
    if resolver is None:
        resolver = socket.getaddrinfo   # late-bound, same rule as resolve_allowed_ips
    _ips, refusal = resolve_allowed_ips(url, allow_private=allow_private, resolver=resolver)
    return refusal


async def resolve_allowed_ips_async(url: str, *, allow_private: bool | None = None,
                                    timeout: float = 10.0,
                                    ) -> tuple[list[str], str | None]:
    """Awaitable :func:`resolve_allowed_ips`.

    DNS resolution is a blocking syscall (``socket.getaddrinfo``) that can stall
    for many seconds on a bad network; run it in a worker thread so the UI
    event loop stays responsive (S2 in docs/PLAN.zh-CN.md), with a hard cap.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(resolve_allowed_ips, url, allow_private=allow_private),
            timeout)
    except asyncio.TimeoutError:
        return [], f"Blocked: resolving '{url}' exceeded the {timeout:.0f}s DNS budget."


async def check_url_async(url: str, *, allow_private: bool | None = None,
                          timeout: float = 10.0) -> str | None:
    """Awaitable variant of :func:`check_url`."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(check_url, url, allow_private=allow_private), timeout)
    except asyncio.TimeoutError:
        return f"Blocked: resolving '{url}' exceeded the {timeout:.0f}s DNS budget."
