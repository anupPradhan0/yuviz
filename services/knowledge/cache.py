"""
Redis client for Knowledge Service — its own process-wide client, same
"own instance per service" boundary as db.py. Two cache uses in this
service, both write-through (no TTL, one writer per key — same principle
Config Service's phone_numbers.py DID cache and this project's
architecture decisions already establish):

  agent_kb:{tenant_slug}:{agent_slug} -> "1"/"0"   (has_enabled_kb flag,
      written by agent_kb.py whenever an assignment is created/enabled/
      disabled/detached — read by libs.knowledge_sdk.RedisKnowledgeRepository)

A Redis outage degrades to "treat as a miss, ask Postgres" — same contract
as services/config/cache.py.
"""

from __future__ import annotations

import logging
import os

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


def _agent_kb_key(tenant_slug: str, agent_slug: str) -> str:
    return f"agent_kb:{tenant_slug}:{agent_slug}"


async def set_has_enabled_kb(tenant_slug: str, agent_slug: str, enabled: bool) -> None:
    try:
        await get_client().set(_agent_kb_key(tenant_slug, agent_slug), "1" if enabled else "0")
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
