"""
RedisKnowledgeRepository — read-only, boolean-only. Key format
agent_kb:{tenant_slug}:{agent_slug} -> "1" | "0", written by
services/knowledge's agent_knowledge_bases CRUD as a side effect of the
write (create/enable/disable/detach), the same cache-aside convention
RedisConfigRepository documents. This repository never writes.

Existing entirely so a non-RAG agent's turn pays zero added latency beyond
one Redis GET: no HTTP round trip to Knowledge Service unless this comes
back True.
"""

from __future__ import annotations

import logging

import redis.asyncio as redis

log = logging.getLogger(__name__)


class RedisKnowledgeRepository:
    def __init__(self, redis_url: str) -> None:
        self._client = redis.from_url(redis_url, decode_responses=True)

    async def close(self) -> None:
        await self._client.aclose()

    async def has_enabled_kb(self, tenant_slug: str, agent_slug: str) -> bool | None:
        try:
            raw = await self._client.get(f"agent_kb:{tenant_slug}:{agent_slug}")
        except redis.RedisError:
            log.warning(
                "RedisKnowledgeRepository: Redis unreachable, treating as miss "
                "tenant=%s agent=%s", tenant_slug, agent_slug,
            )
            return None
        return None if raw is None else raw == "1"
