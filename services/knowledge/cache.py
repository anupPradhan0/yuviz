"""Caches for Knowledge Service.
Redis: write-through, expiring (7 d; a miss re-reads Postgres and refills): kbflag2:{len}:{tenant}:{agent} -> "1"/"0"; an outage degrades to a cache miss (Postgres).
In-process (TTLCache): the resolved retrieval plan and query embeddings, both on the retrieval hot path. Every
key carries the tenant (slug prefix or tenant/config UUID) so tests/test_rls_coverage.py's tripwire holds."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import unicodedata
from collections import OrderedDict
from typing import Any, Callable

import redis.asyncio as redis

log = logging.getLogger(__name__)

_client: redis.Redis | None = None


def get_client(url: str | None = None) -> redis.Redis:
    global _client
    if _client is None:
        url = url or os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        _client = redis.from_url(url, decode_responses=True)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# New namespace: the legacy "agent_kb:{tenant}:{agent}" keys are not injective (tenant "4" + agent "acme:bot"
# is byte-identical to the length-prefixed key for acme/bot), so nothing ever reads or writes that prefix again;
# purge_legacy_keys() deletes what is left.
_LEGACY_PREFIX = "agent_kb:"
_FLAG_TTL_S = int(os.environ.get("KNOWLEDGE_AGENT_KB_FLAG_TTL_S", str(7 * 24 * 3600)))


def _agent_kb_key(tenant_slug: str, agent_slug: str) -> str:
    return slug_key("kbflag2", tenant_slug, agent_slug)


async def purge_legacy_keys(batch: int = 500, *, client: "redis.Redis | None" = None,
                            prefix: str = _LEGACY_PREFIX) -> int:
    """Best-effort SCAN + DELETE of the pre-injective keys (they have no TTL). Returns how many were deleted.
    Only keys that really start with `prefix` (default the exact legacy "agent_kb:") are ever deleted: the SCAN
    pattern is re-checked per key, so a glob quirk cannot widen it. `client`/`prefix` exist so tests can run
    against their own database and namespace instead of whatever the process-wide client points at."""
    deleted = 0
    try:
        client = client or get_client()
        keys = [k async for k in client.scan_iter(match=prefix + "*", count=batch) if k.startswith(prefix)]
        for i in range(0, len(keys), batch):
            deleted += await client.delete(*keys[i:i + batch])
    except redis.RedisError:
        log.warning("cache.purge_legacy_keys: Redis unreachable, skipping")
    return deleted


async def set_has_enabled_kb(tenant_slug: str, agent_slug: str, enabled: bool) -> None:
    try:
        await get_client().set(
            _agent_kb_key(tenant_slug, agent_slug), "1" if enabled else "0", ex=_FLAG_TTL_S)
    except redis.RedisError:
        log.warning(
            "cache.set_has_enabled_kb: Redis unreachable, skipping write "
            "tenant=%s agent=%s", tenant_slug, agent_slug,
        )


async def get_has_enabled_kb(tenant_slug: str, agent_slug: str) -> bool | None:
    try:
        raw = await get_client().get(_agent_kb_key(tenant_slug, agent_slug))
    except redis.RedisError:
        log.warning(
            "cache.get_has_enabled_kb: Redis unreachable, treating as miss "
            "tenant=%s agent=%s", tenant_slug, agent_slug,
        )
        return None
    return None if raw is None else raw == "1"


class TTLCache:
    """Size-bounded, TTL-expiring, LRU-evicting in-process map. Single event loop, so no locking."""

    def __init__(self, *, ttl_s: float, max_entries: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_s
        self._max = max_entries
        self._clock = clock
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: Any) -> None:
        self._data[key] = (self._clock() + self._ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def delete_where(self, predicate: Callable[[Any], bool]) -> None:
        for key in [k for k, (_, v) in self._data.items() if predicate(v)]:
            del self._data[key]

    def delete_prefix(self, prefix: str) -> None:
        for key in [k for k in self._data if k.startswith(prefix)]:
            del self._data[key]

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


_WS_RE = re.compile(r"\s+")


def normalise_query(query: str) -> str:
    """Casefold + NFKC + collapsed whitespace, so trivially different spellings share one embedding."""
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", query)).strip().casefold()


def slug_key(prefix: str, tenant_slug: str, agent_slug: str) -> str:
    """Slugs have no charset constraint, so "acme" + "support:bot" and "acme:support" + "bot" must not share a key.
    The tenant slug is length-prefixed, which makes the encoding injective, and it still reads "...:{tenant}:..."
    for the tenant-safe tripwire."""
    return f"{prefix}:{len(tenant_slug)}:{tenant_slug}:{agent_slug}"


def plan_key(tenant_slug: str, agent_slug: str) -> str:
    return slug_key("plan", tenant_slug, agent_slug)


def plan_tenant_prefix(tenant_slug: str) -> str:
    return f"plan:{len(tenant_slug)}:{tenant_slug}:"


def embedding_key(tenant_id: Any, embedding_config_id: Any, query: str) -> str:
    """Tenant UUID + embedding config UUID + hash of the normalised text: tenant A's entry can never be
    addressed by tenant B, and a re-pointed KB (new config) never reads vectors from the old model."""
    digest = hashlib.sha256(normalise_query(query).encode("utf-8")).hexdigest()[:32]
    return f"qemb:{tenant_id}:{embedding_config_id}:{digest}"
