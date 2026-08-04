"""
Redis cache-aside helpers for Config Service.

TTL is the acceptable-propagation-delay fallback for Phase 5 (instant
invalidation via Pub/Sub is deferred to Phase 7 — see
project_phase5_schema_design.md). Every write path must call invalidate()
for the keys it touches in the same operation that commits to Postgres, so
staleness is bounded by min(TTL, time-to-next-write), not just TTL alone.

Never cache resolved secrets — callers pass already-sanitized dicts (an
api_key_ref/auth_token_ref *path* is fine to cache; a resolved key value is
never constructed here in the first place).

A Redis outage must degrade to "every read hits Postgres" (slower, not
broken) — never propagate a connection error up through get_tenant()/
get_agent()/get_provider_config() and fail the request. This mirrors the
Gateway's C++ RedisClient::get(), which returns std::nullopt rather than
throwing on any failure — same contract on both sides of the config plane.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import redis.asyncio as redis

log = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 60

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


async def get_json(key: str) -> dict[str, Any] | None:
    try:
        raw = await get_client().get(key)
    except redis.RedisError:
        log.warning("cache.get_json: Redis unreachable, treating as cache miss key=%s", key)
        return None
    return json.loads(raw) if raw is not None else None


async def set_json(key: str, value: dict[str, Any], ttl: int | None = DEFAULT_TTL_SECONDS) -> None:
    # default=str covers UUID/datetime fields coming straight out of asyncpg
    # Records — Redis-cached JSON is a display/lookup snapshot, not something
    # deserialized back into typed Python objects.
    #
    # ttl=None means no expiry at all — for data where every write path
    # already writes the fresh value straight into Redis (not just
    # invalidates it), a TTL adds nothing but risk: see
    # phone_numbers.py's DID routing cache, which relies on this.
    try:
        if ttl is None:
            await get_client().set(key, json.dumps(value, default=str))
        else:
            await get_client().set(key, json.dumps(value, default=str), ex=ttl)
    except redis.RedisError:
        log.warning("cache.set_json: Redis unreachable, skipping cache populate key=%s", key)


async def invalidate(*keys: str) -> None:
    if not keys:
        return
    try:
        await get_client().delete(*keys)
    except redis.RedisError:
        # Worst case: a stale entry lives out its TTL (<=60s) instead of being
        # evicted immediately — bounded staleness, not a broken write.
        log.warning("cache.invalidate: Redis unreachable, stale entries will expire via TTL keys=%s", keys)


async def publish(channel: str, message: str) -> None:
    """Instant invalidation via Pub/Sub — the thing this module's own
    docstring flagged as "deferred to Phase 7" back when TTL-based cache-
    aside was built. Built 2026-07-29: Conversation Service subscribes to
    provider_config_changed so a config edit evicts that one cached
    provider client immediately, instead of requiring a full process
    restart (which drops every live call on that instance — see project
    history). Same degrade-to-Postgres-speed posture as invalidate(): if
    Redis is unreachable, the publish is just skipped — the config change
    still lands in Postgres, subscribers just won't hear about it until
    their own next resolution (worse latency-to-effect, never a broken
    write)."""
    try:
        await get_client().publish(channel, message)
    except redis.RedisError:
        log.warning("cache.publish: Redis unreachable, subscribers will not be notified channel=%s", channel)
