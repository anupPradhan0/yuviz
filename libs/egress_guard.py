"""Egress guard for tenant-supplied provider URLs (issue #97).

A tenant admin controls `provider_configs.extra.base_url`, so the platform must never treat it as a URL to fetch.
It is reduced to an origin (scheme, host, port); the API path is fixed in code by the caller. Two checks:

* `validate_base_url` is syntactic and needs no network: it is what the Config service runs at write time and what
  the client constructor runs again. It refuses paths, queries, fragments, credentials, odd IP spellings, and literal
  IPs that are not public (unless the operator allow-listed them).
* `guarded_transport` is the connect-time check. It resolves the host ONCE, refuses the host if ANY resolved address
  is not public, and connects to a validated address itself, so a second DNS answer (rebinding) is never used. The
  Host header and TLS SNI/verification still use the original hostname because only the TCP connect is pinned.

The operator allow-list is `KNOWLEDGE_PROVIDER_ALLOWED_HOSTS` (comma-separated `host` or `host:port`). Unset, it holds
exactly the operator's own Ollama endpoint: the host:port of OLLAMA_BASE_URL, or localhost:11434 (the code's default),
plus 127.0.0.1 on the same port when that host is localhost. Set, it replaces that default. An entry without a port
allows every port on that host, so prefer host:port.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpcore
import httpx

log = logging.getLogger(__name__)

ALLOWED_HOSTS_ENV = "KNOWLEDGE_PROVIDER_ALLOWED_HOSTS"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
RESOLVE_TIMEOUT_S = 5.0
MAX_ADDRESSES_TRIED = 4
_DEFAULT_PORTS = {"http": 80, "https": 443}
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_FORBIDDEN_CHARS = re.compile(r"[\s\x00-\x1f\x7f\\]")
# Embedded-IPv4 IPv6 forms (6to4, Teredo, NAT64): refused whole, the embedded address decides nothing we can trust.
_V6_TUNNELS = tuple(ipaddress.ip_network(n) for n in ("2002::/16", "2001::/32", "64:ff9b::/96", "64:ff9b:1::/48"))


class UnsafeProviderUrl(ValueError):
    """The base_url is not acceptable. The message is safe to show the person who typed the URL."""


@dataclass(frozen=True)
class Origin:
    scheme: str
    host: str  # lowercase, no brackets
    port: int

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"


def is_unsafe_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for anything that is not an ordinary public unicast address."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_unsafe_address(ip.ipv4_mapped)
        if any(ip in net for net in _V6_TUNNELS):
            return True
    return (
        ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_multicast or ip.is_reserved
        or ip.is_unspecified or not ip.is_global or getattr(ip, "is_site_local", False)
    )


def _parse_entry(raw: str) -> tuple[str, int | None] | None:
    raw = raw.strip().lower()
    if not raw:
        return None
    parts = urlsplit(f"//{raw}")
    try:
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname
    return (host, port) if host else None


def allowed_endpoints() -> list[tuple[str, int | None]]:
    env = os.environ.get(ALLOWED_HOSTS_ENV)
    if env is not None:
        return [e for e in map(_parse_entry, env.split(",")) if e]
    own = urlsplit(os.environ.get("OLLAMA_BASE_URL") or DEFAULT_OLLAMA_URL)
    host, port = (own.hostname or "localhost").lower(), own.port or _DEFAULT_PORTS.get(own.scheme, 11434)
    entries = [(host, port)]
    if host == "localhost":
        entries.append(("127.0.0.1", port))
    return entries


def is_allowed(host: str, port: int) -> bool:
    return any(h == host and (p is None or p == port) for h, p in allowed_endpoints())


def _parse_host(host: str) -> str:
    """Canonical host: a DNS name, a dotted-quad IPv4, or an IPv6 literal. Everything else is an odd spelling."""
    if ":" in host:  # urlsplit strips the brackets
        try:
            return str(ipaddress.IPv6Address(host))
        except ValueError:
            raise UnsafeProviderUrl("base_url host is not a valid IPv6 address") from None
    labels = host.split(".")
    if len(host) > 253 or not all(_LABEL.match(label) for label in labels):
        raise UnsafeProviderUrl("base_url host must be a plain ASCII hostname or IP address")
    if all(label.isdigit() for label in labels) and len(labels) == 4:
        try:
            return str(ipaddress.IPv4Address(host))  # rejects leading zeros
        except ValueError:
            raise UnsafeProviderUrl("base_url host is not a canonical IPv4 address") from None
    # Decimal, hex, octal and short IPv4 spellings (2130706433, 0x7f.1, 127.1) are numeric to a resolver.
    if labels[-1].isdigit() or labels[-1].startswith("0x") or all(label.isdigit() for label in labels):
        raise UnsafeProviderUrl("base_url host must not be a numeric IP spelling other than dotted-quad IPv4")
    return host


def validate_base_url(url: object) -> Origin:
    """Syntax and literal-address check, no network. Raises UnsafeProviderUrl with a message for the config's author."""
    if not isinstance(url, str) or not url.strip():
        raise UnsafeProviderUrl("base_url must be a non-empty URL")
    raw = url
    if _FORBIDDEN_CHARS.search(raw):
        raise UnsafeProviderUrl("base_url must not contain whitespace, control characters or backslashes")
    if "?" in raw or "#" in raw:
        raise UnsafeProviderUrl("base_url must not carry a query or fragment; give only scheme://host[:port]")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise UnsafeProviderUrl("base_url is not a valid URL") from None
    if parts.scheme.lower() not in _DEFAULT_PORTS:
        raise UnsafeProviderUrl("base_url must use http or https")
    if "@" in parts.netloc:
        raise UnsafeProviderUrl("base_url must not contain credentials")
    if parts.path not in ("", "/"):
        raise UnsafeProviderUrl("base_url must not carry a path; give only scheme://host[:port]")
    if not parts.hostname:
        raise UnsafeProviderUrl("base_url has no host")
    if port == 0:
        raise UnsafeProviderUrl("base_url port is not valid")
    scheme = parts.scheme.lower()
    origin = Origin(scheme, _parse_host(parts.hostname.lower()), port or _DEFAULT_PORTS[scheme])
    if not is_allowed(origin.host, origin.port):
        try:
            literal = ipaddress.ip_address(origin.host)
        except ValueError:
            return origin  # a name: its addresses are checked when the connection is made
        if is_unsafe_address(literal):
            raise UnsafeProviderUrl(
                "base_url points at a private, loopback or otherwise non-public address, which is not allowed")
    return origin


async def _resolve(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), RESOLVE_TIMEOUT_S)
    return list(dict.fromkeys(info[4][0].split("%")[0] for info in infos))


async def resolve_validated(host: str, port: int) -> list[str]:
    """Resolve once; every address must be public unless the endpoint is allow-listed. Returns addresses to connect to."""
    addresses = await _resolve(host, port)
    if not addresses:
        raise OSError("no addresses")
    if not is_allowed(host, port):
        for address in addresses:
            if is_unsafe_address(ipaddress.ip_address(address)):
                raise OSError("refused: resolves to a non-public address")
    return addresses


class _GuardedBackend(httpcore.AnyIOBackend):
    """Resolves and validates inside the connect, then connects to the validated address (no second resolution)."""

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            addresses = await resolve_validated(host, port)
        except Exception as exc:  # noqa: BLE001 - refusal, DNS failure and timeout all look the same to the caller
            log.warning("provider egress refused host=%s port=%s reason=%s", host, port, exc)
            raise httpcore.ConnectError("provider unreachable") from None
        last: Exception | None = None
        for address in addresses[:MAX_ADDRESSES_TRIED]:
            try:
                return await super().connect_tcp(address, port, timeout, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
        raise last or httpcore.ConnectError("provider unreachable")


def guarded_client(*, timeout: float = 30.0) -> httpx.AsyncClient:
    """An AsyncClient for tenant-supplied origins: pinned connects, no redirects, no proxy or env trust."""
    transport = httpx.AsyncHTTPTransport(trust_env=False)
    transport._pool = httpcore.AsyncConnectionPool(  # httpx has no public hook for the network backend
        ssl_context=httpx.create_ssl_context(), network_backend=_GuardedBackend(),
    )
    return httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False)


# 408/425/429 are the 4xx codes worth retrying; the rest of 4xx, and redirects (never followed), will not change on retry.
_RETRYABLE_4XX = frozenset({408, 425, 429})


def failure_code(exc: BaseException) -> tuple[str, bool] | None:
    """(fixed code, permanent) for what an HTTP call to a tenant-chosen origin can raise; None for anything else.

    The code is all that may be stored, returned or logged where a tenant can read it: exception text carries the
    status line, host and port. Every connection-level failure (refused, timeout, DNS, TLS, guard refusal) is ONE code,
    so a tenant cannot read a port's state back. Only 401/403 are named, because the owner must fix the key."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return "provider_auth_failed", True
        if status == 429:
            return "provider_rate_limited", False
        permanent = 300 <= status < 500 and status not in _RETRYABLE_4XX
        return "provider_error", permanent
    if isinstance(exc, httpx.TransportError):
        return "provider_unreachable", False
    return None
